"""
Full-featured lint for reviewer_check stage (~200 lines).

Produces a detailed report consumed by the Reviewer LLM before it writes
completion_rN.  Does NOT replace Reviewer judgment — only provides the most
precise structured input possible.

Capabilities:
  - Boundary check (via lint_core) — blocked files outside can_touch
  - File conflict detection — overlap with recent commits
  - AST symbol change extraction — new/deleted functions, signature changes
  - Cross-reference: plan.changes vs actual git diff vs AST hints
"""
import ast
import json
import os
import subprocess

from lint_core import run_lint


# ── Conflict detection ──────────────────────────────────────

def _check_conflicts(files_changed, base_ref="HEAD~5"):
    """Check overlap between changed files and files touched in recent commits."""
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


# ── AST extraction ──────────────────────────────────────────

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


def _compare_asts(old_src, new_src):
    """Diff two Python source strings at the AST level."""
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


def _extract_hints(files_changed):
    """Extract structured AST-level change summary for Python files."""
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

    if any(hints[k] for k in hints):
        return hints
    return None


# ── Public entry points ─────────────────────────────────────

def lint_changed_files(boundaries, agent, base_ref="HEAD~5"):
    """Convenience: run git diff --name-only, boundary check, and AST hints.

    Used by Reviewer at reviewer_check stage for cross-validation.
    """
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

    # Boundary check
    boundary_result = run_lint(files, boundaries, agent)
    if boundary_result["blocked"]:
        return {"blocked": True, "reason": boundary_result["reason"], "hints": None}

    # Conflict hints
    conflicts = _check_conflicts(files, base_ref)

    # AST hints
    ast_hints = _extract_hints(files)

    hints = {}
    if conflicts:
        hints["conflicts"] = conflicts
    if ast_hints:
        hints["ast"] = ast_hints

    return {"blocked": False, "reason": None, "hints": hints if hints else None}


def lint_crossref(plan_json_str, boundaries, agent, base_ref="HEAD~5"):
    """Full cross-reference report for Reviewer consumption.

    Runs boundary check + conflict detection + AST extraction.
    Then cross-references plan.declared files vs actual git diff files.

    Returns a structured dict the Reviewer reads before writing completion_rN:
    {
      "blocked": bool,
      "boundary": {"ok": bool, "reason": str|None},
      "conflicts": [...],
      "ast": {...},
      "crossref": {
        "declared_files": [...],      # from plan.files
        "actual_files": [...],        # from git diff --name-only
        "extra_files": [...],         # in git diff but not in plan
        "missing_files": [...],       # in plan but not in git diff
      }
    }
    """
    # 1. Parse plan
    plan = None
    declared_files = []
    try:
        plan = json.loads(plan_json_str)
        declared_files = plan.get("files", [])
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. Get actual changes
    try:
        raw = subprocess.check_output(
            ["git", "diff", "--name-only"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"blocked": True, "reason": "git diff failed", "hints": None}

    actual_files = raw.splitlines() if raw else []

    # 3. Boundary check on actual files
    boundary = run_lint(actual_files, boundaries, agent) if actual_files else {"blocked": False, "reason": None}

    # 4. Conflicts
    all_files = list(set(actual_files) | set(declared_files))
    conflicts = _check_conflicts(all_files, base_ref) if all_files else []

    # 5. AST hints on actual modified files
    ast_hints = _extract_hints(actual_files) if actual_files else None

    # 6. Cross-reference: plan vs actual
    declared_set = {f.replace("\\", "/") for f in declared_files}
    actual_set = {f.replace("\\", "/") for f in actual_files}
    crossref = {
        "declared_files": sorted(declared_set),
        "actual_files": sorted(actual_set),
        "extra_files": sorted(actual_set - declared_set),
        "missing_files": sorted(declared_set - actual_set),
    }

    hints = {}
    if conflicts:
        hints["conflicts"] = conflicts
    if ast_hints:
        hints["ast"] = ast_hints

    return {
        "blocked": boundary["blocked"],
        "boundary": boundary,
        "crossref": crossref,
        "hints": hints if hints else None,
    }
