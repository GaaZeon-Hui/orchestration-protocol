"""
Lightweight lint for orchestrator_gate stage (~60 lines).

Validates plan_json structure and runs boundary check on declared files.
No AST parsing, no git diff — the gate only checks what the Worker declares
in the plan, not actual code changes (which don't exist yet at this stage).
"""
import json

from lint_core import run_lint


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
    Returns {"blocked": bool, "reason": str|None}.
    """
    ok, result = validate_plan(plan_json_str)
    if not ok:
        return {"blocked": True, "reason": result}
    files = result["files"]
    return run_lint(files, boundaries, agent)
