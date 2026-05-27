"""
Lint layer for orchestration protocol.
Blocking boundary checks + file-conflict hints + AST change extraction for LLM.
stdlib only — mirrors pipeline.py's zero-dependency constraint.
"""

import ast
import fnmatch
import json
import os
import subprocess
import sys


def run_lint(files_changed, boundaries, agent, base_ref="HEAD~5"):
    """Entry point. Run all checks and return structured result.

    Args:
        files_changed: list of file paths (e.g. from `git diff --name-only`)
        boundaries: dict from orc.get_boundaries(), or None
        agent: agent type string (e.g. 'py-agent')
        base_ref: git ref for conflict diff baseline

    Returns:
        {"blocked": bool, "reason": str|None, "hints": dict|None}
    """
    # 1. Blocking: boundary check
    blocked, reason = _check_boundaries(files_changed, boundaries, agent)
    if blocked:
        return {"blocked": True, "reason": reason, "hints": None}

    # 2. Informational: file-level conflicts with recent commits
    conflict_info = _check_conflicts(files_changed, base_ref)

    # 3. Informational: AST-level change hints for LLM
    ast_hints = _extract_hints(files_changed)

    hints = {}
    if conflict_info:
        hints["conflicts"] = conflict_info
    if ast_hints:
        hints["ast"] = ast_hints

    return {"blocked": False, "reason": None, "hints": hints if hints else None}


# ── Boundary check (blocking) ──────────────────────────────────

def _check_boundaries(files_changed, boundaries, agent):
    """Check each file against agent boundary rules.

    Returns (blocked: bool, reason: str|None).
    """
    if not boundaries:
        return (True, "boundaries not configured — worker must set boundaries first")

    agent_rules = boundaries.get(agent)
    if not agent_rules:
        return (True, "agent '{}' not found in boundaries config".format(agent))

    forbidden = agent_rules.get("forbidden", [])
    can_touch = agent_rules.get("can_touch", [])

    violations = []
    for path in files_changed:
        norm = path.replace("\\", "/")

        for pattern in forbidden:
            if _match_pattern(norm, pattern):
                violations.append((path, "forbidden", pattern))
                break

        if not violations or _match_pattern(norm, violations[-1][2]):
            # Only check can_touch if file passed forbidden check
            # and can_touch is actually defined
            if can_touch and not any(
                _match_pattern(norm, p) for p in can_touch
            ):
                violations.append((path, "not in can_touch", str(can_touch)))

    if violations:
        detail = "; ".join(
            "{} ({})".format(v[0], v[1]) for v in violations
        )
        return (True, "boundary violation: {}".format(detail))

    return (False, None)


# ── Conflict detection (informational) ─────────────────────────

def _check_conflicts(files_changed, base_ref):
    """Check overlap between changed files and files touched in recent commits.

    Returns list of conflict descriptors, or empty list.
    """
    try:
        raw = subprocess.check_output(
            ["git", "diff", "--name-only", base_ref, "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    if not raw:
        return []

    recent_files = set(raw.splitlines())
    changed_set = {f.replace("\\", "/") for f in files_changed}
    overlap = changed_set & recent_files

    if not overlap:
        return []

    return [
        {"file": f, "source": "recent commits (vs {})".format(base_ref)}
        for f in sorted(overlap)
    ]


# ── AST change extraction (informational, for LLM) ─────────────

def _extract_hints(files_changed):
    """Extract structured AST-level change summary for Python files.

    Returns dict with keys: new_functions, signature_changes, deleted_symbols,
    new_classes — each a list of {file, name, ...} dicts.
    """
    py_files = [f for f in files_changed if f.endswith(".py") and os.path.isfile(f)]
    if not py_files:
        return None

    hints = {
        "new_functions": [],
        "signature_changes": [],
        "deleted_symbols": [],
        "new_classes": [],
    }

    for filepath in py_files:
        old_src = _get_old_source(filepath)
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                new_src = fh.read()
        except (OSError, UnicodeDecodeError):
            continue

        diff = _compare_asts(old_src, new_src)
        for key in hints:
            for item in diff.get(key, []):
                item["file"] = filepath
                hints[key].append(item)

    # Only return if we found something
    if any(hints[k] for k in hints):
        return hints
    return None


def _get_old_source(filepath):
    """Get the HEAD version of a file. Returns '' for new files."""
    try:
        return subprocess.check_output(
            ["git", "show", "HEAD:{}".format(filepath.replace("\\", "/"))],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _compare_asts(old_src, new_src):
    """Diff two Python source strings at the AST level.

    Returns dict of changes — see _extract_hints for schema.
    """
    old_tree = _parse_safe(old_src)
    new_tree = _parse_safe(new_src)

    if old_tree is None or new_tree is None:
        return {}

    old_symbols = _collect_defs(old_tree)
    new_symbols = _collect_defs(new_tree)

    result = {"new_functions": [], "signature_changes": [], "deleted_symbols": [],
              "new_classes": []}

    old_names = {s["name"]: s for s in old_symbols}
    new_names = {s["name"]: s for s in new_symbols}

    for name, info in new_names.items():
        if name not in old_names:
            if info["kind"] == "function":
                result["new_functions"].append({"name": name, "params": info["params"]})
            elif info["kind"] == "class":
                result["new_classes"].append({"name": name, "methods": info.get("methods", [])})

    for name, info in new_names.items():
        if name in old_names and info["kind"] == "function" and old_names[name]["kind"] == "function":
            if info["params"] != old_names[name]["params"]:
                result["signature_changes"].append({
                    "name": name,
                    "old_params": old_names[name]["params"],
                    "new_params": info["params"],
                })

    for name, info in old_names.items():
        if name not in new_names:
            result["deleted_symbols"].append({"name": name, "kind": info["kind"]})

    return {k: v for k, v in result.items() if v}


def _parse_safe(source):
    """Try to parse source. Return AST or None on any failure."""
    if not source.strip():
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _collect_defs(tree):
    """Walk an AST and collect top-level function/class definitions."""
    symbols = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef):
            params = [arg.arg for arg in node.args.args]
            symbols.append({"name": node.name, "kind": "function", "params": params})
        elif isinstance(node, ast.ClassDef):
            methods = []
            for body_node in node.body:
                if isinstance(body_node, ast.FunctionDef):
                    m_params = [arg.arg for arg in body_node.args.args
                                if arg.arg != "self"]
                    methods.append({"name": body_node.name, "params": m_params})
            symbols.append({"name": node.name, "kind": "class", "methods": methods})
    return symbols


# ── Pattern matching ───────────────────────────────────────────

def _match_pattern(path, pattern):
    """Match a normalised file path against a boundary rule pattern.

    Supports:
        '*.py'    — basename glob (fnmatch, anchored via re.match)
        'app/'    — directory prefix (converted to 'app/*')
        'app/**'  — explicit full-path glob
    """
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")

    if pattern.endswith("/"):
        return path.startswith(pattern)

    return fnmatch.fnmatch(path, pattern)


# ── Standalone helper ──────────────────────────────────────────

def lint_changed_files(boundaries, agent, base_ref="HEAD~5"):
    """Convenience: run git diff --name-only and feed into run_lint()."""
    try:
        raw = subprocess.check_output(
            ["git", "diff", "--name-only"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"blocked": True, "reason": "git diff failed", "hints": None}

    if not raw:
        return {"blocked": False, "reason": None, "hints": None}

    files = raw.splitlines()
    return run_lint(files, boundaries, agent, base_ref=base_ref)


# ── Plan validation (for orchestrator_gate stage) ──────────────

def validate_plan(plan_json_str):
    """Validate Worker's plan_json structure.

    Returns (ok: bool, result: dict|str).
    On failure, result is an error string.
    On success, result is the parsed dict.
    """
    try:
        p = json.loads(plan_json_str)
    except (json.JSONDecodeError, TypeError):
        return False, "plan_json is not valid JSON"

    if not isinstance(p.get("files"), list) or len(p["files"]) == 0:
        return False, "files is missing or empty"

    for f in p["files"]:
        if not isinstance(f, str):
            return False, "files contains non-string entry: {}".format(f)
        if f.startswith("/") or ".." in f:
            return False, "path traversal in files: {}".format(f)

    return True, p


def lint_plan(plan_json_str, boundaries, agent):
    """Entry point for orchestrator_gate stage.

    Validates plan structure, then runs boundary check on declared files.
    Returns same format as run_lint().
    """
    ok, result = validate_plan(plan_json_str)
    if not ok:
        return {"blocked": True, "reason": result, "hints": None}
    files = result["files"]
    return run_lint(files, boundaries, agent)
