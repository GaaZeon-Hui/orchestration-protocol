"""Bootstrap script for first-time setup.

Usage:
    python setup.py

Creates the database, registers the current user as orchestrator,
opens the status dashboard, and prints instructions to create
worker and reviewer agents.
"""
import json
import os
import subprocess
import sys
import uuid

from orchestrator import Orchestrator

DB_PATH = ".claude/orchestrator/orchestrator.db"


def main():
    orc = Orchestrator(DB_PATH)
    orc.init_db()
    orc.migrate()

    # ── Register orchestrator ────────────────────────────
    agent_id = input("Enter your agent name (e.g. orch-01): ").strip()
    if not agent_id:
        agent_id = "orch-" + str(uuid.uuid4())[:6]
        print("  Using auto-generated ID: {}".format(agent_id))

    conn = orc._connect()
    existing = conn.execute(
        "SELECT role FROM register WHERE agent_id=?", (agent_id,)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO register (agent_id, role, schema_json, heartbeat) "
            "VALUES (?, 'orchestrator', '{}', datetime('now','localtime'))",
            (agent_id,),
        )
        conn.commit()
        print("  Registered '{}' as orchestrator.".format(agent_id))
    else:
        print("  '{}' already registered as {}.".format(agent_id, existing[0]))
    conn.close()

    # ── Save agent_id so CLAUDE.md skips the prompt ──────
    os.makedirs(".claude", exist_ok=True)
    with open(".claude/agent_id.json", "w") as f:
        json.dump({"agent_id": agent_id}, f)
    print("  Saved agent_id to .claude/agent_id.json")

    # ── Create default project if none exist ─────────────
    conn = orc._connect()
    count = conn.execute("SELECT COUNT(*) FROM project").fetchone()[0]
    if count == 0:
        conn.execute(
            "INSERT INTO project (id, content) VALUES (?, ?)",
            ("demo-project", "Demo orchestration protocol project"),
        )
        conn.commit()
        print("  Created default project 'demo-project'.")
    conn.close()

    # ── Open status dashboard ────────────────────────────
    print()
    print("  Opening status dashboard in a new terminal...")
    status_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status.py")
    try:
        subprocess.Popen(
            ["python", status_path],
            creationflags=subprocess.CREATE_NEW_CONSOLE
            if sys.platform == "win32" else 0,
        )
    except Exception:
        print("  (could not open status automatically — run 'python status.py' manually)")

    # ── Instructions ─────────────────────────────────────
    print()
    print("=" * 60)
    print("  Setup complete!")
    print()
    print("  Next step:")
    print("    claude")
    print()
    print("  The orchestrator will detect missing reviewer/worker")
    print("  and run `python spawn_agents.py` to open their terminals.")
    print()
    print("  Agent auto-promotion rules:")
    print("    - If orch is absent >90s, next agent auto-promotes")
    print("    - Reviewer auto-promotes when absent >90s (same rule)")
    print("    - If both orch + reviewer dead: orch first, then reviewer")
    print("=" * 60)


if __name__ == "__main__":
    main()
