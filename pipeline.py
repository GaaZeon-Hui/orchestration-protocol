"""
Pipeline Protocol — standalone state machine.
All agents use transition_stage(), not bare SQL.
"""

import json
import sqlite3

# ── Constants ───────────────────────────────────────────────

VALID_TRANSITIONS = {
    'init':                 ['orchestrator_gate'],
    'orchestrator_gate':    ['worker_modify', 'rejected'],
    'worker_modify':        ['reviewer_check'],
    'reviewer_check':       ['orchestrator_arbiter'],
    'orchestrator_arbiter': ['verified', 'worker_modify'],
    'verified':             ['lock_released'],
}

# role -> set of from_stage values this role is allowed to advance
ROLE_PERMISSIONS = {
    'worker':        {'init', 'worker_modify', 'verified'},
    'orchestrator':  {'init', 'orchestrator_gate', 'orchestrator_arbiter', 'verified', 'worker_modify'},
    'reviewer':      {'reviewer_check'},
}

ALLOWED_COLUMNS = {
    # Worker init
    'reason_json', 'plan_json',
    # Worker correction rounds
    'plan_r2', 'plan_r3', 'plan_r4',
    # Worker execution
    'commits_json',
    # Orch gate
    'approval_status', 'rejection_reason',
    # Orch arbiter
    'feedback_r1', 'feedback_r2', 'feedback_r3', 'feedback_r4',
    # Reviewer
    'completion_r1', 'completion_r2', 'completion_r3', 'completion_r4',
    # Orch human intervention
    'human_intervention',
    # Counter
    'review_round',
}

TERMINAL_STAGES = {'rejected', 'lock_released'}


# ── Core function ───────────────────────────────────────────

def transition_stage(request_id, new_stage, role, revision, db_path, **kwargs):
    """Advance pipeline stage with permission check, CAS update, and audit log.

    Args:
        request_id: pipeline request ID
        new_stage: target stage
        role: 'worker' | 'orchestrator'
        revision: expected current revision (CAS)
        db_path: path to orchestrator.db
        **kwargs: column values to write (whitelist-filtered by ALLOWED_COLUMNS)

    Returns:
        (new_revision, new_stage) tuple

    Raises:
        LookupError: pipeline not found
        RuntimeError: revision mismatch or CAS failure
        PermissionError: role cannot advance from current stage
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute(
        "SELECT stage, revision FROM pipeline_state WHERE request_id=?",
        (request_id,),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        raise LookupError("Pipeline not found: {}".format(request_id))

    current_stage = row['stage']
    current_revision = row['revision']

    # Transition path validation (defence in depth — trigger also enforces)
    if new_stage not in VALID_TRANSITIONS.get(current_stage, []):
        conn.close()
        raise ValueError(
            "Invalid transition: {} -> {}".format(current_stage, new_stage)
        )

    # Revision match
    if current_revision != revision:
        conn.close()
        raise RuntimeError(
            "Revision mismatch: expected {}, actual {}".format(
                revision, current_revision,
            )
        )

    # Permission check
    if current_stage not in ROLE_PERMISSIONS.get(role, set()):
        conn.close()
        raise PermissionError(
            "Role '{}' cannot advance from stage '{}'".format(
                role, current_stage,
            )
        )

    # Whitelist filter kwargs
    filtered = {k: v for k, v in kwargs.items() if k in ALLOWED_COLUMNS}

    # Build UPDATE
    set_parts = [
        "stage=?", "revision=revision+1",
        "updated_at=datetime('now','localtime')",
    ]
    params = [new_stage]

    for key, value in filtered.items():
        set_parts.append("{} = ?".format(key))
        params.append(value)

    params.extend([request_id, current_stage, current_revision])

    new_rev = current_revision + 1

    try:
        cur.execute("BEGIN")
        cur.execute(
            "UPDATE pipeline_state SET {} "
            "WHERE request_id=? AND stage=? AND revision=?".format(
                ', '.join(set_parts),
            ),
            params,
        )
        if cur.rowcount == 0:
            raise RuntimeError("CAS failed: concurrent modification")

        cur.execute(
            """INSERT INTO audit_log
               (request_id, role, stage_from, stage_to,
                revision_before, revision_after, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                request_id, role, current_stage, new_stage,
                current_revision, new_rev,
                json.dumps(
                    {k: v for k, v in filtered.items()},
                    ensure_ascii=False,
                ) if filtered else None,
            ),
        )

        conn.commit()
        conn.close()
        return (new_rev, new_stage)

    except RuntimeError:
        conn.rollback()
        conn.close()
        raise
    except Exception:
        conn.rollback()
        conn.close()
        raise
