"""Spawn reviewer and worker Claude Code terminals.

Run by the orchestrator after registration to spin up the other two roles.
Usage: python spawn_agents.py
"""
import json
import os
import subprocess
import sys

ID_FILE = ".claude/agent_id.json"

from orchestrator import Orchestrator


def get_my_role():
    """Read agent_id from disk, look up current role."""
    if not os.path.exists(ID_FILE):
        return None
    with open(ID_FILE) as f:
        agent_id = json.load(f)["agent_id"]
    orc = Orchestrator()
    orc.init_db()
    orc.migrate()
    return orc.try_register(agent_id)


def spawn_terminal(label):
    """Open a new terminal window running Claude Code."""
    if sys.platform == "win32":
        # Windows Terminal / cmd
        try:
            subprocess.Popen(
                ["wt", "-w", "0", "nt", "--title", label, "claude"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except FileNotFoundError:
            subprocess.Popen(
                ["cmd", "/k", "start", "claude"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
    else:
        # Linux / macOS
        for term in ["gnome-terminal", "xterm", "konsole"]:
            try:
                subprocess.Popen([term, "-e", "claude"])
                break
            except FileNotFoundError:
                continue


def main():
    role = get_my_role()

    if role is None:
        print("No agent_id.json found. Run `python setup.py` first.")
        return

    print("Current role: {}".format(role))

    if role != "orchestrator":
        print("Only the orchestrator spawns terminals.")
        print("Agent ready. Waiting for pipeline events...")
        return

    # ── Orchestrator: auto-spawn agents ──
    print()
    print("Opening reviewer terminal...")
    spawn_terminal("Reviewer")

    print("Opening worker terminal...")
    spawn_terminal("Worker")

    print()
    print("Both terminals launched. Each will auto-register via CLAUDE.md.")
    print("Orchestrator ready. Run `python status.py` to monitor.")


if __name__ == "__main__":
    main()
