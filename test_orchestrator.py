"""Tests for orchestrator.py — pipeline state machine edition."""
import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from orchestrator import Orchestrator, _tz_utc
from pipeline import (
    transition_stage,
    VALID_TRANSITIONS,
    TERMINAL_STAGES,
)

TEST_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_orchestrator.db")


class TestPipelineOrchestrator(unittest.TestCase):

    def setUp(self):
        for path in [TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"]:
            for _ in range(5):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                    break
                except PermissionError:
                    time.sleep(0.05)
        self.orc = Orchestrator(TEST_DB)
        self.orc.init_db()
        self.orc.migrate()
        self.tz = _tz_utc()

    def tearDown(self):
        self.orc = None
        for path in [TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"]:
            for _ in range(5):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                    break
                except PermissionError:
                    time.sleep(0.05)

    # ── 1. init_db ──────────────────────────────────────────

    def test_init_db_idempotent(self):
        self.orc.init_db()
        self.orc.init_db()

    def test_tables_and_trigger_exist(self):
        conn = self.orc._connect()
        tables = ["pipeline_state", "context", "audit_log"]
        for t in tables:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone()
            self.assertIsNotNone(row, "Table {} missing".format(t))
        trigger = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name='tr_stage_transition'"
        ).fetchone()
        self.assertIsNotNone(trigger, "Trigger missing")
        conn.close()

    def test_migrate_idempotent(self):
        """Repeated migrate() calls should not fail (duplicate columns OK)."""
        self.orc.migrate()
        self.orc.migrate()
        self.orc.migrate()

    # ── 2. Registration ─────────────────────────────────────

    def test_registration_first_agent(self):
        alive, oid = self.orc.check_orchestrator_alive()
        self.assertFalse(alive)
        role = self.orc.try_register("agent-A-0001")
        self.assertEqual(role, "orchestrator")
        alive, oid = self.orc.check_orchestrator_alive()
        self.assertTrue(alive)
        self.assertEqual(oid, "agent-A-0001")

    def test_registration_second_agent(self):
        self.orc.try_register("agent-A-0001")
        alive, _ = self.orc.check_orchestrator_alive()
        self.assertTrue(alive)
        role = self.orc.try_register("agent-B-0002")
        self.assertEqual(role, "worker")

    def test_registration_race(self):
        results = []

        def register(name):
            o = Orchestrator(TEST_DB)
            results.append(o.try_register("{}-sid".format(name)))

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
        conn = self.orc._connect()
        conn.execute(
            "UPDATE context SET orchestrator_heartbeat=datetime('now','localtime','-120 seconds')"
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

    # ── 3. Pipeline init + get ──────────────────────────────

    def test_init_pipeline(self):
        req_id = self.orc.init_pipeline(
            "py-agent", "test reason",
            {"modules": ["core/"], "files": ["a.py"], "excluded": []},
            {"summary": "refactor", "steps": ["step1"], "breaking_changes": False},
            {"potential_issues": ["safe"]},
            ["c1"],
            self.tz,
        )
        self.assertIn("py-agent-", req_id)
        p = self.orc.get_pipeline(req_id)
        self.assertEqual(p["stage"], "request_submitted")
        self.assertEqual(p["revision"], 0)
        self.assertEqual(p["reason"], "test reason")
        self.assertEqual(p["scope_json"]["modules"], ["core/"])
        self.assertEqual(p["constraints_json"], ["c1"])

    def test_get_pipeline_none(self):
        self.assertIsNone(self.orc.get_pipeline("nonexistent"))

    def test_init_pipeline_rejects_duplicate_agent(self):
        """Same agent cannot create two active pipelines concurrently."""
        self.orc.init_pipeline(
            "py-agent", "first",
            {"files": ["a.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        with self.assertRaises(RuntimeError) as ctx:
            self.orc.init_pipeline(
                "py-agent", "second",
                {"files": ["b.py"], "modules": [], "excluded": []},
                {"summary": "x", "steps": [], "breaking_changes": False},
                {"potential_issues": []}, [], self.tz,
            )
        self.assertIn("already has active pipeline", str(ctx.exception))

    # ── 4. Stage transitions (via pipeline.transition_stage) ─

    def test_full_pipeline_flow(self):
        req_id = self.orc.init_pipeline(
            "py-agent", "reason",
            {"files": ["a.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )

        p = self.orc.get_pipeline(req_id)
        rev = p["revision"]

        # Orchestrator: analysis chain
        rev, _ = transition_stage(req_id, "conflict_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(req_id, "boundary_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(req_id, "logic_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(
            req_id, "approved", "orchestrator", rev, self.orc.db_path,
            approval_status="approved",
            granted_scope_json=json.dumps({"files": ["a.py"]}),
            reviewed_by="orchestrator",
        )
        # Worker: modify chain
        rev, _ = transition_stage(req_id, "modifying", "worker", rev, self.orc.db_path)
        rev, _ = transition_stage(req_id, "self_review_done", "worker", rev, self.orc.db_path,
                                      self_review_json=json.dumps({"all_steps_completed": True}))
        rev, _ = transition_stage(
            req_id, "completion_submitted", "worker", rev, self.orc.db_path,
            self_review_json=json.dumps({"all_steps_completed": True}),
            commits_json=json.dumps(["abc123"]),
        )
        # Orchestrator: verify
        rev, _ = transition_stage(req_id, "completed", "orchestrator", rev, self.orc.db_path)
        # Worker: release
        rev, _ = transition_stage(req_id, "lock_released", "worker", rev, self.orc.db_path)

        p = self.orc.get_pipeline(req_id)
        self.assertEqual(p["stage"], "lock_released")
        self.assertEqual(p["revision"], 9)
        self.assertIsNotNone(p["granted_scope_json"])

    def test_valid_transitions_map_consistent(self):
        all_stages = set(VALID_TRANSITIONS.keys()) | {
            t for targets in VALID_TRANSITIONS.values() for t in targets
        }
        self.assertIn("request_submitted", all_stages)
        self.assertIn("lock_released", all_stages)
        for ts in TERMINAL_STAGES:
            self.assertNotIn(ts, VALID_TRANSITIONS)

    def test_revision_mismatch_raises_runtime_error(self):
        req_id = self.orc.init_pipeline(
            "py-agent", "r",
            {"files": ["x.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        with self.assertRaises(RuntimeError):
            transition_stage(req_id, "conflict_analysis_done", "orchestrator",
                           99, self.orc.db_path)

    def test_reject_flow(self):
        req_id = self.orc.init_pipeline(
            "py-agent", "bad",
            {"files": ["engine.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        p = self.orc.get_pipeline(req_id)
        rev = p["revision"]
        rev, _ = transition_stage(req_id, "conflict_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(req_id, "boundary_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(req_id, "logic_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(
            req_id, "rejected", "orchestrator", rev, self.orc.db_path,
            approval_status="rejected",
            rejection_reason="out of bounds",
        )
        p = self.orc.get_pipeline(req_id)
        self.assertEqual(p["stage"], "rejected")
        self.assertEqual(p["rejection_reason"], "out of bounds")

    # ── 5. Trigger enforcement ──────────────────────────────

    def test_trigger_blocks_invalid_transition(self):
        req_id = self.orc.init_pipeline(
            "py-agent", "r",
            {"files": ["x.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        conn = self.orc._connect()
        with self.assertRaises(Exception):
            conn.execute(
                "UPDATE pipeline_state SET stage='approved', revision=revision+1 "
                "WHERE request_id=?",
                (req_id,),
            )
            conn.commit()
        conn.rollback()
        conn.close()

    def test_trigger_blocks_revision_jump(self):
        req_id = self.orc.init_pipeline(
            "py-agent", "r",
            {"files": ["x.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        conn = self.orc._connect()
        with self.assertRaises(Exception):
            conn.execute(
                "UPDATE pipeline_state SET stage='conflict_analysis_done', revision=5 "
                "WHERE request_id=?",
                (req_id,),
            )
            conn.commit()
        conn.rollback()
        conn.close()

    # ── 6. Recovery ─────────────────────────────────────────

    def test_recover_pipeline_returns_latest(self):
        r1 = self.orc.init_pipeline(
            "py-agent", "first",
            {"files": ["a.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        # Terminate r1 so r2 can be created (same-agent guard)
        p = self.orc.get_pipeline(r1)
        rev = p["revision"]
        rev, _ = transition_stage(r1, "conflict_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(r1, "boundary_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(r1, "logic_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(
            r1, "rejected", "orchestrator", rev, self.orc.db_path,
            approval_status="rejected", rejection_reason="done",
        )
        r2 = self.orc.init_pipeline(
            "py-agent", "second",
            {"files": ["b.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        rid, stage = self.orc.recover_pipeline("py-agent")
        self.assertEqual(rid, r2)
        self.assertEqual(stage, "request_submitted")

    def test_recover_pipeline_ignores_terminal(self):
        # Create a rejected pipeline
        r1 = self.orc.init_pipeline(
            "py-agent", "rejected task",
            {"files": ["a.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        p = self.orc.get_pipeline(r1)
        rev = p["revision"]
        rev, _ = transition_stage(r1, "conflict_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(r1, "boundary_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(r1, "logic_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(
            r1, "rejected", "orchestrator", rev, self.orc.db_path,
            approval_status="rejected", rejection_reason="bad",
        )
        # Create and complete another pipeline
        req_id = self.orc.init_pipeline(
            "py-agent", "will complete",
            {"files": ["b.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        p = self.orc.get_pipeline(req_id)
        rev = p["revision"]
        flow = [
            ("conflict_analysis_done", "orchestrator"),
            ("boundary_analysis_done", "orchestrator"),
            ("logic_analysis_done", "orchestrator"),
            ("approved", "orchestrator", {"approval_status": "approved"}),
            ("modifying", "worker"),
            ("self_review_done", "worker", {"self_review_json": json.dumps({"done": True})}),
            ("completion_submitted", "worker"),
            ("completed", "orchestrator"),
            ("lock_released", "worker"),
        ]
        for item in flow:
            new_stage = item[0]
            role = item[1]
            kwargs = item[2] if len(item) > 2 else {}
            rev, _ = transition_stage(req_id, new_stage, role, rev, self.orc.db_path, **kwargs)

        rid, stage = self.orc.recover_pipeline("py-agent")
        self.assertIsNone(rid)
        self.assertIsNone(stage)

    # ── 7. Queries ──────────────────────────────────────────

    def test_get_pending_requests(self):
        self.orc.init_pipeline(
            "py-agent", "a",
            {"files": ["a.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        self.orc.init_pipeline(
            "md-agent", "b",
            {"files": ["b.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        pending_a = self.orc.get_pending_requests("py-agent")
        self.assertEqual(len(pending_a), 1)
        pending_b = self.orc.get_pending_requests("md-agent")
        self.assertEqual(len(pending_b), 1)

    def test_get_requests_by_stage(self):
        self.orc.init_pipeline(
            "py-agent", "r",
            {"files": ["x.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        items = self.orc.get_requests_by_stage("request_submitted")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["agent"], "py-agent")

    # ── 8. Boundaries ───────────────────────────────────────

    def test_boundaries_crud(self):
        self.assertIsNone(self.orc.get_boundaries())
        boundaries = {
            "py-agent": {"can_touch": ["*.py"], "forbidden": ["*.md"]},
            "md-agent": {"can_touch": ["*.md"], "forbidden": ["*.py"]},
        }
        self.orc.set_boundaries(boundaries)
        result = self.orc.get_boundaries()
        self.assertEqual(result["py-agent"]["can_touch"], ["*.py"])

    # ── 9. Context ──────────────────────────────────────────

    def test_context_update(self):
        self.orc.update_context(last_commit="abc123")
        conn = self.orc._connect()
        row = conn.execute("SELECT last_commit FROM context WHERE id=1").fetchone()
        conn.close()
        self.assertEqual(row[0], "abc123")

    # ── 10. CAS concurrency ─────────────────────────────────

    def test_cas_concurrent_advance(self):
        req_id = self.orc.init_pipeline(
            "py-agent", "r",
            {"files": ["x.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        p = self.orc.get_pipeline(req_id)
        results = []

        def advance():
            try:
                transition_stage(
                    req_id, "conflict_analysis_done", "orchestrator",
                    p["revision"], self.orc.db_path,
                )
                results.append("ok")
            except (RuntimeError, ValueError):
                results.append("conflict")

        t1 = threading.Thread(target=advance)
        t2 = threading.Thread(target=advance)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(results.count("ok"), 1)
        self.assertIn("conflict", results)

        final = self.orc.get_pipeline(req_id)
        self.assertEqual(final["stage"], "conflict_analysis_done")
        self.assertEqual(final["revision"], 1)

    # ── 12. Utility ─────────────────────────────────────────

    def test_make_request_id(self):
        rid = Orchestrator.make_request_id("py-agent", self.tz)
        self.assertTrue(rid.startswith("py-agent-"))
        self.assertEqual(len(rid.split("-")), 5)


if __name__ == "__main__":
    unittest.main()
