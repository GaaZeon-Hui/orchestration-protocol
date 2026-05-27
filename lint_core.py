"""
Shared lint core — pattern matching and boundary checks.
Used by both lint_gate (orchestrator_gate stage) and lint_full (reviewer_check stage).
"""
import fnmatch


def _match_pattern(path, pattern):
    """Match a normalised file path against a boundary rule pattern.

    Supports:
        '*.py'    — basename glob (fnmatch)
        'app/'    — directory prefix
        'app/**'  — explicit full-path glob
    """
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")

    if pattern.endswith("/"):
        return path.startswith(pattern)

    return fnmatch.fnmatch(path, pattern)


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


def run_lint(files_changed, boundaries, agent):
    """Run boundary check on file list.

    Returns:
        {"blocked": bool, "reason": str|None}
    """
    blocked, reason = _check_boundaries(files_changed, boundaries, agent)
    return {"blocked": blocked, "reason": reason}
