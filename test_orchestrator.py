"""Tests for orchestrator.py — 14 scenarios."""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orchestrator import Orchestrator, _tz_plus8


TEST_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_orchestrator.db")


class TestOrchestrator(unittest.TestCase):

    def setUp(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        for ext in ("-wal", "-shm"):
            if os.path.exists(TEST_DB + ext):
                os.remove(TEST_DB + ext)
        self.orc = Orchestrator(TEST_DB)
        self.orc.init_db()
        self.orc.migrate()
        self.tz = _tz_plus8()

    def tearDown(self):
        if os.path.exists(TEST_DB):
            os.remove(TEST_DB)
        for ext in ("-wal", "-shm"):
            if os.path.exists(TEST_DB + ext):
                os.remove(TEST_DB + ext)

    # ── 1. init_db ──────────────────────────────────────────

    def test_init_db_idempotent(self):
        """init_db is safe to call multiple times."""
        self.orc.init_db()
        self.orc.init_db()  # no error

    def test_all_tables_exist(self):
        conn = self.orc._connect()
        tables = ["lock", "requests", "approvals", "completions", "context", "processed"]
        for t in tables:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            self.assertIsNotNone(row, f"Table {t} missing")
        conn.close()

    # ── 2. Registration ─────────────────────────────────────

    def test_registration_first_agent(self):
        alive, oid = self.orc.check_orchestrator_alive()
        self.assertFalse(alive)
        role = self.orc.try_register("agent-A-0001")
        self.assertEqual(role, "orchestrator")
        alive, oid = self.orc.check_orchestrator_alive()
        self.assertTrue(alive)

    def test_registration_second_agent(self):
        self.orc.try_register("agent-A-0001")
        alive, _ = self.orc.check_orchestrator_alive()
        self.assertTrue(alive)
        role = self.orc.try_register("agent-B-0002")
        self.assertEqual(role, "worker")

    def test_registration_race(self):
        """Two concurrent registrations: exactly one orchestrator."""
        results = []

        def register(name):
            o = Orchestrator(TEST_DB)
            results.append(o.try_register(f"{name}-sid"))

        t1 = threading.Thread(target=register, args=("agent-X",))
        t2 = threading.Thread(target=register, args=("agent-Y",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        orch_count = sum(1 for r in results if r == "orchestrator")
        self.assertEqual(orch_count, 1)

    def test_heartbeat_timeout_takeover(self):
        self.orc.try_register("agent-A-0001")
        # Artificially age heartbeat
        conn = self.orc._connect()
        conn.execute(
            "UPDATE lock SET orchestrator_heartbeat=datetime('now','localtime','-120 seconds')"
        )
        conn.commit()
        conn.close()

        alive, _ = self.orc.check_orchestrator_alive()
        self.assertFalse(alive)
        role = self.orc.try_register("agent-C-0003")
        self.assertEqual(role, "orchestrator")

    def test_send_heartbeat(self):
        self.orc.try_register("agent-A-0001")
        self.assertTrue(self.orc.send_heartbeat("agent-A-0001"))
        self.assertFalse(self.orc.send_heartbeat("wrong-id"))

    # ── 3. Lock ─────────────────────────────────────────────

    def test_lock_acquire_release(self):
        self.assertIsNone(self.orc.check_lock())
        self.orc.acquire_lock("engine-agent", "req-001", {"files": ["a.py"]})
        lock = self.orc.check_lock()
        self.assertIsNotNone(lock)
        self.assertEqual(lock["holder"], "engine-agent")
        self.orc.release_lock()
        self.assertIsNone(self.orc.check_lock())

    def test_lock_busy(self):
        self.orc.acquire_lock("engine-agent", "req-001", {"files": ["a.py"]})
        with self.assertRaises(RuntimeError):
            self.orc.acquire_lock("service-agent", "req-002", {"files": ["b.py"]})

    # ── 4. Request flow ─────────────────────────────────────

    def test_submit_and_get_status(self):
        req_id = self.orc.submit_request(
            "engine-agent", "test reason",
            {"modules": ["core/"], "files": ["core/a.py"], "excluded": []},
            {"summary": "refactor", "steps": ["step1"], "breaking_changes": False},
            {"potential_issues": ["预判 — 安全"]},
            ["不新建函数"],
            self.tz,
        )
        self.assertIn("engine-agent-", req_id)
        self.assertEqual(self.orc.get_request_status(req_id), "pending")

    def test_wait_for_approval(self):
        req_id = self.orc.submit_request(
            "engine-agent", "test",
            {"modules": [], "files": ["x.py"], "excluded": []},
            {"summary": "fix", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        # Approve via separate connection (simulates orchestrator in another process)
        o2 = Orchestrator(TEST_DB)
        o2.issue_approval("engine-agent", req_id,
                          {"files": ["x.py"], "forbidden": []},
                          "approved", tz=self.tz)
        # wait_for_approval should return immediately since already approved
        status = self.orc.wait_for_approval(req_id, poll_interval=0.1)
        self.assertEqual(status, "approved")

    # ── 5. Approval flow ────────────────────────────────────

    def test_approve_flow(self):
        req_id = self.orc.submit_request(
            "engine-agent", "reason",
            {"modules": [], "files": ["a.py"], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        self.orc.issue_approval(
            "engine-agent", req_id,
            {"files": ["a.py"], "forbidden": []}, "approved", tz=self.tz,
        )
        self.assertEqual(self.orc.get_request_status(req_id), "approved")
        self.assertTrue(self.orc.is_processed(req_id, "request"))

    def test_reject_flow(self):
        req_id = self.orc.submit_request(
            "service-agent", "bad idea",
            {"modules": [], "files": ["engine.py"], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        self.orc.issue_approval(
            "service-agent", req_id,
            {}, "rejected", rejection_reason="越界", tz=self.tz,
        )
        self.assertEqual(self.orc.get_request_status(req_id), "rejected")
        approval = self.orc.get_approval(req_id)
        self.assertIsNotNone(approval)
        self.assertEqual(approval["rejection_reason"], "越界")

    # ── 6. Completion ───────────────────────────────────────

    def test_completion_flow(self):
        req_id = self.orc.submit_request(
            "engine-agent", "r",
            {"modules": [], "files": ["a.py"], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        self.orc.submit_completion(
            req_id, "engine-agent", "2026-05-21T10:00:00+08:00",
            {"all_steps_completed": True, "files_modified": ["a.py"],
             "files_not_in_scope": [], "new_functions_created": [],
             "breaking_changes": [], "constraints_violated": []},
            ["abc123"], "no sync notes", {"pipeline": ""},
        )
        self.orc.verify_completion(req_id)
        self.assertTrue(self.orc.is_processed(req_id, "completion"))

    # ── 7. Dedup ────────────────────────────────────────────

    def test_dedup(self):
        self.assertFalse(self.orc.is_processed("req-X", "request"))
        self.orc.mark_processed("req-X", "request", "approved")
        self.assertTrue(self.orc.is_processed("req-X", "request"))
        self.assertFalse(self.orc.is_processed("req-X", "completion"))

    # ── 8. Boundaries ───────────────────────────────────────

    def test_boundaries_crud(self):
        self.assertIsNone(self.orc.get_boundaries())
        boundaries = {
            "engine-agent": {"can_touch": ["core/"], "forbidden": ["app/", "service/"]},
            "service-agent": {"can_touch": ["service/"], "forbidden": ["app/", "*.py"]},
        }
        self.orc.set_boundaries(boundaries)
        result = self.orc.get_boundaries()
        self.assertEqual(result["engine-agent"]["can_touch"], ["core/"])

    # ── 9. Context ──────────────────────────────────────────

    def test_context_update(self):
        self.orc.update_context(last_commit="abc123")
        conn = self.orc._connect()
        row = conn.execute("SELECT last_commit FROM context WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(row[0], "abc123")

    # ── 10. Monitor ─────────────────────────────────────────

    def test_monitor_detects_new_request(self):
        self.orc.try_register("orch-session-1")

        # Submit request first, then verify monitor detects it
        self.orc.submit_request(
            "engine-agent", "test",
            {"modules": [], "files": ["x.py"], "excluded": []},
            {"summary": "fix", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )

        gen = self.orc.run_monitor("orch-session-1", heartbeat_interval=60, poll_interval=0.2)
        events = []
        for _ in range(5):
            try:
                event = next(gen)
                events.append(event)
                if event[0] == "NEW_REQUEST":
                    break
            except StopIteration:
                break
        self.assertTrue(any(e[0] == "NEW_REQUEST" for e in events))

    def test_monitor_heartbeat_fails_when_taken_over(self):
        self.orc.try_register("orch-A")
        # Another agent takes over
        conn = self.orc._connect()
        conn.execute(
            "UPDATE lock SET orchestrator_id='usurper', orchestrator_heartbeat=datetime('now','localtime')"
        )
        conn.commit()
        conn.close()

        gen = self.orc.run_monitor("orch-A", heartbeat_interval=0, poll_interval=0.2)
        event = next(gen)
        self.assertEqual(event[0], "HEARTBEAT_FAILED")

    # ── 11. Utility ─────────────────────────────────────────

    def test_make_request_id(self):
        rid = Orchestrator.make_request_id("engine-agent", self.tz)
        self.assertTrue(rid.startswith("engine-agent-"))
        self.assertEqual(len(rid.split("-")), 4)  # agent-YYYYMMDD-HHMMSS


if __name__ == "__main__":
    unittest.main()
