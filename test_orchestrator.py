"""Tests for orchestrator.py — new 3-role architecture."""
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

    def test_tables_exist(self):
        conn = self.orc._connect()
        tables = ["pipeline_state", "project", "register", "audit_log"]
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
        self.orc.migrate()
        self.orc.migrate()
        self.orc.migrate()

    # ── 2. init_pipeline ────────────────────────────────────

    def test_init_pipeline(self):
        req_id = self.orc.init_pipeline(
            "py-agent",
            {"reason": "test", "agent_id": "py-agent"},
            {"files": ["a.py"]},
            self.tz,
        )
        self.assertIn("py-agent-", req_id)
        p = self.orc.get_pipeline(req_id)
        self.assertEqual(p["stage"], "init")
        self.assertEqual(p["revision"], 0)
        self.assertEqual(p["reason_json"]["reason"], "test")
        self.assertEqual(p["plan_json"]["files"], ["a.py"])

    def test_get_pipeline_none(self):
        self.assertIsNone(self.orc.get_pipeline("nonexistent"))

    def test_init_pipeline_rejects_duplicate_agent(self):
        self.orc.init_pipeline(
            "py-agent", {"reason": "first"}, {"files": ["a.py"]}, self.tz,
        )
        with self.assertRaises(RuntimeError) as ctx:
            self.orc.init_pipeline(
                "py-agent", {"reason": "second"}, {"files": ["b.py"]}, self.tz,
            )
        self.assertIn("already has active pipeline", str(ctx.exception))

    # ── 3. Full path ────────────────────────────────────────

    def _init(self):
        return self.orc.init_pipeline(
            "py-agent",
            {"reason": "test", "agent_id": "py-agent"},
            {"files": ["a.py"]},
            self.tz,
        )

    def test_full_new_path(self):
        """init → orch_gate → worker_modify → reviewer_check → orch_arbiter → verified → lock_released"""
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        self.assertEqual(p['stage'], 'init')
        rev = p['revision']

        rev, _ = transition_stage(req_id, 'orchestrator_gate', 'orchestrator', rev, self.orc.db_path)
        rev, _ = transition_stage(req_id, 'worker_modify', 'orchestrator', rev, self.orc.db_path,
                                   approval_status='approved')
        rev, _ = transition_stage(req_id, 'reviewer_check', 'worker', rev, self.orc.db_path,
                                   commits_json=json.dumps(['abc']))
        rev, _ = transition_stage(req_id, 'orchestrator_arbiter', 'reviewer', rev, self.orc.db_path,
                                   completion_r1=json.dumps({'verdict': '符合计划'}))
        rev, _ = transition_stage(req_id, 'verified', 'orchestrator', rev, self.orc.db_path)
        rev, _ = transition_stage(req_id, 'lock_released', 'worker', rev, self.orc.db_path)

        p = self.orc.get_pipeline(req_id)
        self.assertEqual(p['stage'], 'lock_released')
        self.assertEqual(p['revision'], 6)

    def test_correction_loop(self):
        """Full path with 1 correction round."""
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        rev = p['revision']

        # Gate
        rev, _ = transition_stage(req_id, 'orchestrator_gate', 'orchestrator', rev, self.orc.db_path)
        rev, _ = transition_stage(req_id, 'worker_modify', 'orchestrator', rev, self.orc.db_path,
                                   approval_status='approved')
        # Round 1: fail
        rev, _ = transition_stage(req_id, 'reviewer_check', 'worker', rev, self.orc.db_path,
                                   commits_json=json.dumps(['abc']))
        rev, _ = transition_stage(req_id, 'orchestrator_arbiter', 'reviewer', rev, self.orc.db_path,
                                   completion_r1=json.dumps({'verdict': '有偏差', 'extra_changes': ['b.py']}))
        # Orch: feedback → correction
        rev, _ = transition_stage(req_id, 'worker_modify', 'orchestrator', rev, self.orc.db_path,
                                   feedback_r1=json.dumps({'fix': 'remove b.py'}),
                                   review_round=2)
        self.assertEqual(self.orc.get_pipeline(req_id)['review_round'], 2)

        # Round 2: pass
        rev, _ = transition_stage(req_id, 'reviewer_check', 'worker', rev, self.orc.db_path,
                                   plan_r2=json.dumps({'files': ['a.py']}))
        rev, _ = transition_stage(req_id, 'orchestrator_arbiter', 'reviewer', rev, self.orc.db_path,
                                   completion_r2=json.dumps({'verdict': '符合计划'}))
        rev, _ = transition_stage(req_id, 'verified', 'orchestrator', rev, self.orc.db_path)
        rev, _ = transition_stage(req_id, 'lock_released', 'worker', rev, self.orc.db_path)

        p = self.orc.get_pipeline(req_id)
        self.assertEqual(p['stage'], 'lock_released')

    def test_reject_flow(self):
        """Orch gate → rejected."""
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        rev = p['revision']
        rev, _ = transition_stage(req_id, 'orchestrator_gate', 'orchestrator', rev, self.orc.db_path)
        rev, _ = transition_stage(req_id, 'rejected', 'orchestrator', rev, self.orc.db_path,
                                   approval_status='rejected',
                                   rejection_reason='out of bounds')
        p = self.orc.get_pipeline(req_id)
        self.assertEqual(p['stage'], 'rejected')
        self.assertEqual(p['rejection_reason'], 'out of bounds')

    # ── 4. Permissions ──────────────────────────────────────

    def test_worker_blocked_from_orchestrator_arbiter(self):
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        rev = p['revision']
        rev, _ = transition_stage(req_id, 'orchestrator_gate', 'orchestrator', rev, self.orc.db_path)
        rev, _ = transition_stage(req_id, 'worker_modify', 'orchestrator', rev, self.orc.db_path,
                                   approval_status='approved')
        rev, _ = transition_stage(req_id, 'reviewer_check', 'worker', rev, self.orc.db_path,
                                   commits_json=json.dumps(['abc']))
        rev, _ = transition_stage(req_id, 'orchestrator_arbiter', 'reviewer', rev, self.orc.db_path,
                                   completion_r1=json.dumps({'verdict': '符合计划'}))
        with self.assertRaises(PermissionError):
            transition_stage(req_id, 'verified', 'worker', rev, self.orc.db_path)

    def test_reviewer_blocked_from_orchestrator_gate(self):
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        rev, _ = transition_stage(req_id, 'orchestrator_gate', 'orchestrator', p['revision'], self.orc.db_path)
        with self.assertRaises(PermissionError):
            transition_stage(req_id, 'worker_modify', 'reviewer', rev, self.orc.db_path)

    # ── 5. Revision + CAS ───────────────────────────────────

    def test_revision_mismatch_raises_runtime_error(self):
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        with self.assertRaises(RuntimeError):
            transition_stage(req_id, 'orchestrator_gate', 'orchestrator', 99, self.orc.db_path)

    def test_cas_concurrent_advance(self):
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        results = []

        def advance():
            try:
                transition_stage(
                    req_id, 'orchestrator_gate', 'orchestrator',
                    p['revision'], self.orc.db_path,
                )
                results.append("ok")
            except (RuntimeError, ValueError):
                results.append("conflict")

        t1 = threading.Thread(target=advance)
        t2 = threading.Thread(target=advance)
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(results.count("ok"), 1)
        self.assertIn("conflict", results)
        final = self.orc.get_pipeline(req_id)
        self.assertEqual(final['stage'], 'orchestrator_gate')
        self.assertEqual(final['revision'], 1)

    # ── 6. Trigger enforcement ──────────────────────────────

    def test_trigger_blocks_invalid_transition(self):
        req_id = self._init()
        conn = self.orc._connect()
        with self.assertRaises(Exception):
            conn.execute(
                "UPDATE pipeline_state SET stage='lock_released', revision=revision+1 "
                "WHERE request_id=?",
                (req_id,),
            )
            conn.commit()
        conn.rollback()
        conn.close()

    def test_trigger_blocks_revision_jump(self):
        req_id = self._init()
        conn = self.orc._connect()
        with self.assertRaises(Exception):
            conn.execute(
                "UPDATE pipeline_state SET stage='orchestrator_gate', revision=5 "
                "WHERE request_id=?",
                (req_id,),
            )
            conn.commit()
        conn.rollback()
        conn.close()

    # ── 7. Queries ──────────────────────────────────────────

    def test_get_pending_requests(self):
        self.orc.init_pipeline("py-agent", {"reason": "a"}, {"files": ["a.py"]}, self.tz)
        self.orc.init_pipeline("md-agent", {"reason": "b"}, {"files": ["b.py"]}, self.tz)
        self.assertEqual(len(self.orc.get_pending_requests("py-agent")), 1)
        self.assertEqual(len(self.orc.get_pending_requests("md-agent")), 1)

    def test_get_requests_by_stage(self):
        self.orc.init_pipeline("py-agent", {"reason": "r"}, {"files": ["x.py"]}, self.tz)
        items = self.orc.get_requests_by_stage("init")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["agent"], "py-agent")

    # ── 8. Recovery ─────────────────────────────────────────

    def test_recover_pipeline_returns_latest(self):
        r1 = self.orc.init_pipeline("py-agent", {"reason": "first"}, {"files": ["a.py"]}, self.tz)
        p = self.orc.get_pipeline(r1)
        rev = p['revision']
        rev, _ = transition_stage(r1, 'orchestrator_gate', 'orchestrator', rev, self.orc.db_path)
        rev, _ = transition_stage(r1, 'rejected', 'orchestrator', rev, self.orc.db_path,
                                   approval_status='rejected', rejection_reason='done')
        r2 = self.orc.init_pipeline("py-agent", {"reason": "second"}, {"files": ["b.py"]}, self.tz)
        rid, stage = self.orc.recover_pipeline("py-agent")
        self.assertEqual(rid, r2)
        self.assertEqual(stage, "init")

    def test_recover_pipeline_ignores_terminal(self):
        r1 = self.orc.init_pipeline("py-agent", {"reason": "r"}, {"files": ["a.py"]}, self.tz)
        p = self.orc.get_pipeline(r1)
        rev = p['revision']
        rev, _ = transition_stage(r1, 'orchestrator_gate', 'orchestrator', rev, self.orc.db_path)
        rev, _ = transition_stage(r1, 'rejected', 'orchestrator', rev, self.orc.db_path,
                                   approval_status='rejected', rejection_reason='bad')

        req_id = self.orc.init_pipeline("py-agent", {"reason": "r"}, {"files": ["b.py"]}, self.tz)
        p = self.orc.get_pipeline(req_id)
        rev = p['revision']
        flow = [
            ('orchestrator_gate', 'orchestrator'),
            ('worker_modify', 'orchestrator', {'approval_status': 'approved'}),
            ('reviewer_check', 'worker', {'commits_json': json.dumps(['abc'])}),
            ('orchestrator_arbiter', 'reviewer', {'completion_r1': json.dumps({'verdict': '符合计划'})}),
            ('verified', 'orchestrator'),
            ('lock_released', 'worker'),
        ]
        for item in flow:
            new_stage = item[0]; role = item[1]
            kwargs = item[2] if len(item) > 2 else {}
            rev, _ = transition_stage(req_id, new_stage, role, rev, self.orc.db_path, **kwargs)

        rid, stage = self.orc.recover_pipeline("py-agent")
        self.assertIsNone(rid)
        self.assertIsNone(stage)

    # ── 9. Project + Register ───────────────────────────────

    def test_create_and_get_project(self):
        self.orc.create_project("proj-1", "test project")
        p = self.orc.get_project("proj-1")
        self.assertEqual(p['id'], 'proj-1')
        self.assertEqual(p['content'], 'test project')

    def test_get_project_none(self):
        self.assertIsNone(self.orc.get_project("nonexistent"))

    def test_get_register_none(self):
        self.assertIsNone(self.orc.get_register("unregistered"))

    # ── 10. Valid transitions consistency ───────────────────

    def test_valid_transitions_map_consistent(self):
        all_stages = set(VALID_TRANSITIONS.keys()) | {
            t for targets in VALID_TRANSITIONS.values() for t in targets
        }
        self.assertIn("init", all_stages)
        self.assertIn("lock_released", all_stages)
        for ts in TERMINAL_STAGES:
            self.assertNotIn(ts, VALID_TRANSITIONS)

    # ── 11. Orphan lock resolution ──────────────────────────

    def test_resolve_orphan_locks_releases_stale_verified(self):
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        rev = p['revision']
        flow = [
            ('orchestrator_gate', 'orchestrator'),
            ('worker_modify', 'orchestrator', {'approval_status': 'approved'}),
            ('reviewer_check', 'worker', {'commits_json': json.dumps(['abc'])}),
            ('orchestrator_arbiter', 'reviewer', {'completion_r1': json.dumps({'verdict': '符合计划'})}),
            ('verified', 'orchestrator'),
        ]
        for item in flow:
            new_stage = item[0]; role = item[1]
            kwargs = item[2] if len(item) > 2 else {}
            rev, _ = transition_stage(req_id, new_stage, role, rev, self.orc.db_path, **kwargs)

        # Artificially age the updated_at
        conn = self.orc._connect()
        conn.execute(
            "UPDATE pipeline_state SET updated_at=datetime('now','localtime','-300 seconds') "
            "WHERE request_id=?", (req_id,)
        )
        conn.commit()
        conn.close()

        resolved = self.orc.resolve_orphan_locks(timeout_seconds=120)
        self.assertIn(req_id, resolved)
        p = self.orc.get_pipeline(req_id)
        self.assertEqual(p['stage'], 'lock_released')


if __name__ == "__main__":
    unittest.main()
