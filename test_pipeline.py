"""Tests for pipeline.py — standalone transition_stage with audit_log."""
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
    ROLE_PERMISSIONS,
    ALLOWED_COLUMNS,
    TERMINAL_STAGES,
)

TEST_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_pipeline.db")


class TestTransitionStage(unittest.TestCase):

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

    def _init(self):
        return self.orc.init_pipeline(
            "py-agent", "test",
            {"files": ["x.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )

    # ── 1. normal transition ─────────────────────────────────

    def test_valid_transition(self):
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        new_rev, new_stage = transition_stage(
            req_id, "conflict_analysis_done", "orchestrator",
            p["revision"], self.orc.db_path,
        )
        self.assertEqual(new_stage, "conflict_analysis_done")
        self.assertEqual(new_rev, 1)
        p = self.orc.get_pipeline(req_id)
        self.assertEqual(p["stage"], "conflict_analysis_done")
        self.assertEqual(p["revision"], 1)

    # ── 2. role permission ───────────────────────────────────

    def test_worker_blocked_from_orchestrator_stage(self):
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        with self.assertRaises(PermissionError):
            transition_stage(
                req_id, "conflict_analysis_done", "worker",
                p["revision"], self.orc.db_path,
            )

    def test_orchestrator_blocked_from_worker_stage(self):
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        # Advance to approved (orchestrator does this)
        rev, _ = transition_stage(
            req_id, "conflict_analysis_done", "orchestrator",
            p["revision"], self.orc.db_path,
        )
        rev, _ = transition_stage(
            req_id, "boundary_analysis_done", "orchestrator",
            rev, self.orc.db_path,
        )
        rev, _ = transition_stage(
            req_id, "logic_analysis_done", "orchestrator",
            rev, self.orc.db_path,
        )
        rev, _ = transition_stage(
            req_id, "approved", "orchestrator",
            rev, self.orc.db_path,
            approval_status="approved",
            granted_scope_json=json.dumps({"files": ["x.py"]}),
        )
        # Now try orchestrator to advance from "approved" (worker-only stage)
        with self.assertRaises(PermissionError):
            transition_stage(
                req_id, "modifying", "orchestrator",
                rev, self.orc.db_path,
            )

    # ── 3. revision mismatch ─────────────────────────────────

    def test_revision_mismatch(self):
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        with self.assertRaises(RuntimeError):
            transition_stage(
                req_id, "conflict_analysis_done", "orchestrator",
                99,  # wrong revision
                self.orc.db_path,
            )

    # ── 4. kwargs whitelist ──────────────────────────────────

    def test_kwargs_whitelist(self):
        """Allowed columns are written through."""
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        rev, _ = transition_stage(
            req_id, "conflict_analysis_done", "orchestrator",
            p["revision"], self.orc.db_path,
            reason="updated_reason",
        )
        p = self.orc.get_pipeline(req_id)
        self.assertEqual(p["reason"], "updated_reason")

    def test_kwargs_filtered_unknown(self):
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        rev, _ = transition_stage(
            req_id, "conflict_analysis_done", "orchestrator",
            p["revision"], self.orc.db_path,
            not_a_real_column="SHOULD_NOT_APPEAR",
        )
        # Check audit_log — payload_json should NOT contain the unknown column
        conn = self.orc._connect()
        row = conn.execute(
            "SELECT payload_json FROM audit_log WHERE request_id=? ORDER BY id DESC LIMIT 1",
            (req_id,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        payload = json.loads(row[0]) if row[0] else {}
        self.assertNotIn("not_a_real_column", payload)

    # ── 5. audit_log ─────────────────────────────────────────

    def test_audit_log_written(self):
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        rev, stage = transition_stage(
            req_id, "conflict_analysis_done", "orchestrator",
            p["revision"], self.orc.db_path,
            approval_status="testing",
        )
        conn = self.orc._connect()
        row = conn.execute(
            "SELECT role, stage_from, stage_to, revision_before, revision_after, payload_json "
            "FROM audit_log WHERE request_id=? ORDER BY id DESC LIMIT 1",
            (req_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(row[0], "orchestrator")
        self.assertEqual(row[1], "request_submitted")
        self.assertEqual(row[2], "conflict_analysis_done")
        self.assertEqual(row[3], 0)
        self.assertEqual(row[4], 1)
        payload = json.loads(row[5])
        self.assertEqual(payload["approval_status"], "testing")

    def test_audit_log_complete_trail(self):
        """Full pipeline flow produces correct audit trail."""
        req_id = self._init()
        p = self.orc.get_pipeline(req_id)
        rev = p["revision"]

        flow = [
            ("conflict_analysis_done", "orchestrator"),
            ("boundary_analysis_done", "orchestrator"),
            ("logic_analysis_done", "orchestrator"),
            ("approved", "orchestrator", {"approval_status": "approved",
                                           "granted_scope_json": json.dumps({"files": ["x.py"]})}),
            ("modifying", "worker"),
            ("self_review_done", "worker", {"self_review_json": json.dumps({"done": True})}),
            ("completion_submitted", "worker", {"commits_json": json.dumps(["abc"])}),
            ("completed", "orchestrator"),
            ("lock_released", "worker"),
        ]
        for item in flow:
            new_stage = item[0]
            role = item[1]
            kwargs = item[2] if len(item) > 2 else {}
            rev, _ = transition_stage(req_id, new_stage, role, rev, self.orc.db_path, **kwargs)

        conn = self.orc._connect()
        rows = conn.execute(
            "SELECT role, stage_from, stage_to FROM audit_log WHERE request_id=? ORDER BY id",
            (req_id,),
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 9)
        expected_roles = ["orchestrator", "orchestrator", "orchestrator", "orchestrator",
                          "worker", "worker", "worker", "orchestrator", "worker"]
        self.assertEqual([r[0] for r in rows], expected_roles)

    # ── 6. CAS concurrency ───────────────────────────────────

    def test_concurrent_cas_failure(self):
        req_id = self._init()
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
        self.assertEqual(final["revision"], 1)

    # ── 7. constants self-consistency ────────────────────────

    def test_valid_transitions_complete(self):
        all_stages = set(VALID_TRANSITIONS.keys()) | {
            t for targets in VALID_TRANSITIONS.values() for t in targets
        }
        self.assertIn("lock_released", all_stages)
        for ts in TERMINAL_STAGES:
            self.assertNotIn(ts, VALID_TRANSITIONS)

    def test_role_permissions_subset_of_valid_transitions(self):
        for role, stages in ROLE_PERMISSIONS.items():
            for stage in stages:
                self.assertIn(stage, VALID_TRANSITIONS,
                              "{} can from '{}' but it's not in VALID_TRANSITIONS".format(role, stage))

    def test_allowed_columns_are_real(self):
        conn = self.orc._connect()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pipeline_state)").fetchall()}
        conn.close()
        for col in ALLOWED_COLUMNS:
            self.assertIn(col, cols, "{} not a real pipeline_state column".format(col))

    def test_self_review_done_requires_self_review_json(self):
        """modifying → self_review_done must include self_review_json kwarg."""
        req_id = self.orc.init_pipeline(
            "py-agent", "r",
            {"files": ["x.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        p = self.orc.get_pipeline(req_id)
        rev = p["revision"]

        # Advance to modifying (approval flow)
        rev, _ = transition_stage(req_id, "conflict_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(req_id, "boundary_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(req_id, "logic_analysis_done", "orchestrator", rev, self.orc.db_path)
        rev, _ = transition_stage(
            req_id, "approved", "orchestrator", rev, self.orc.db_path,
            approval_status="approved",
        )
        rev, _ = transition_stage(req_id, "modifying", "worker", rev, self.orc.db_path)

        # Should fail without self_review_json
        with self.assertRaises(ValueError) as ctx:
            transition_stage(req_id, "self_review_done", "worker", rev, self.orc.db_path)
        self.assertIn("self_review_json is required", str(ctx.exception))

    def test_analysis_columns_persist(self):
        """Analysis JSON columns are writable via transition_stage kwargs."""
        req_id = self.orc.init_pipeline(
            "py-agent", "r",
            {"files": ["x.py"], "modules": [], "excluded": []},
            {"summary": "x", "steps": [], "breaking_changes": False},
            {"potential_issues": []}, [], self.tz,
        )
        p = self.orc.get_pipeline(req_id)
        rev = p["revision"]

        conflict_data = json.dumps({"conflicts": ["a.py"]}, ensure_ascii=False)
        rev, _ = transition_stage(
            req_id, "conflict_analysis_done", "orchestrator", rev, self.orc.db_path,
            conflict_analysis_json=conflict_data,
        )

        p2 = self.orc.get_pipeline(req_id)
        self.assertEqual(p2["conflict_analysis_json"], {"conflicts": ["a.py"]})

        boundary_data = json.dumps({"warnings": ["edge case in core"]}, ensure_ascii=False)
        rev, _ = transition_stage(
            req_id, "boundary_analysis_done", "orchestrator", rev, self.orc.db_path,
            boundary_analysis_json=boundary_data,
        )

        logic_data = json.dumps({"new_functions": ["foo"]}, ensure_ascii=False)
        rev, _ = transition_stage(
            req_id, "logic_analysis_done", "orchestrator", rev, self.orc.db_path,
            logic_analysis_json=logic_data,
        )

        p3 = self.orc.get_pipeline(req_id)
        self.assertEqual(p3["boundary_analysis_json"], {"warnings": ["edge case in core"]})
        self.assertEqual(p3["logic_analysis_json"], {"new_functions": ["foo"]})


if __name__ == "__main__":
    unittest.main()
