"""
Pipeline status dashboard — read-only monitor for orchestration protocol.
stdlib only. Run: python status.py [poll_interval_seconds]
"""

import os
import sqlite3
import sys
import time


def status(db_path=".claude/orchestrator/orchestrator.db", interval=3):
    conn = _open_ro(db_path)

    # Track last audit_log id to detect writes
    last_audit_id = _max_audit_id(conn)
    conn.close()

    while True:
        conn = _open_ro(db_path)
        pipelines = _fetch_all(conn)
        current_audit_id = _max_audit_id(conn)
        conn.close()

        _render(pipelines, current_audit_id, last_audit_id)

        if current_audit_id is not None and current_audit_id != last_audit_id:
            # Write detected — cooldown then immediate refresh
            time.sleep(2)
            last_audit_id = current_audit_id
        else:
            last_audit_id = current_audit_id
            time.sleep(interval)


def _open_ro(db_path):
    """Open read-only connection. Returns None if DB doesn't exist yet."""
    if not os.path.exists(db_path):
        return None
    uri = "file:{}?mode=ro".format(db_path.replace("\\", "/"))
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError:
        return None


def _max_audit_id(conn):
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT MAX(id) FROM audit_log").fetchone()
        return row[0]
    except sqlite3.OperationalError:
        return None


def _fetch_all(conn):
    if conn is None:
        return []
    try:
        rows = conn.execute("""
            SELECT request_id, agent, stage, revision,
                   reason, approval_status, rejection_reason,
                   updated_at
            FROM pipeline_state
            ORDER BY updated_at DESC
        """).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []


# ── Display ────────────────────────────────────────────────────

STAGE_ICONS = {
    "request_submitted":       "○",  # ○
    "conflict_analysis_done":  "◐",  # ◐
    "boundary_analysis_done":  "◑",  # ◑
    "logic_analysis_done":     "◕",  # ◕
    "approved":                "✔",  # ✔
    "rejected":                "✘",  # ✘
    "modifying":               "⚒",  # ⚒
    "self_review_done":        "◉",  # ◉
    "completion_submitted":    "⏳",  # ⏳
    "completed":               "★",  # ★
    "lock_released":           "☑",  # ☑
}

STAGE_WIDTH = 22


def _render(pipelines, audit_id, last_audit_id):
    _clear()
    changed = audit_id is not None and last_audit_id is not None and audit_id != last_audit_id

    print(" Pipeline Status Dashboard")
    print("=" * 80)
    print(" DB: .claude/orchestrator/orchestrator.db | "
          "audit #{:>6} | {}".format(
              audit_id or 0,
              "[CHANGED — refreshed after 2s cooldown]" if changed
              else "polling every few seconds...",
          ))
    print("=" * 80)

    if not pipelines:
        print("\n  (no pipelines yet — waiting for worker to init)\n")
        return

    for p in pipelines:
        icon = STAGE_ICONS.get(p["stage"], "?")
        stage_label = "{}{}".format(icon, p["stage"])
        reason = (p["reason"] or "")[:48]

        # Build status tags
        tags = ""
        if p["stage"] == "rejected":
            tags = " REASON: {}".format((p["rejection_reason"] or "")[:40])
        elif p["stage"] == "approved":
            tags = " APPROVED"
        elif p["stage"] == "completed":
            tags = " AWAITING LOCK RELEASE"

        print(" {:<6} | {:<24} | rev {:>2} | {:<50} | {}".format(
            p["agent"][:6],
            stage_label[:24],
            p["revision"],
            reason[:50],
            p["updated_at"] or "",
        ))
        if tags:
            print("        | {:<24} |       | {}".format("", tags))

    print("\n" + "-" * 80)
    stage_counts = {}
    for p in pipelines:
        stage_counts[p["stage"]] = stage_counts.get(p["stage"], 0) + 1
    summary = " | ".join(
        "{}:{}{}".format(STAGE_ICONS.get(s, "?"), c, s)
        for s, c in sorted(stage_counts.items())
    )
    print(" {}".format(summary))
    print(" Ctrl+C to exit\n")


def once(db_path=".claude/orchestrator/orchestrator.db"):
    """Print status once and exit — for agent invocation."""
    conn = _open_ro(db_path)
    pipelines = _fetch_all(conn)
    audit_id = _max_audit_id(conn)
    conn.close()
    _render(pipelines, audit_id, None)
    return pipelines


def _clear():
    os.system("cls" if os.name == "nt" else "clear")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        db = sys.argv[2] if len(sys.argv) > 2 else ".claude/orchestrator/orchestrator.db"
        once(db)
    else:
        db = sys.argv[1] if len(sys.argv) > 1 else ".claude/orchestrator/orchestrator.db"
        sec = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        try:
            status(db, sec)
        except KeyboardInterrupt:
            print()
