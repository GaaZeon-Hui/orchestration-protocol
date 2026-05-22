"""
Orchestration Protocol — Pipeline State Machine edition.
DB initialisation, registration, queries, and heartbeat.
Stage transitions delegated to pipeline.transition_stage().
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from pipeline import (
    transition_stage,
    VALID_TRANSITIONS,
    ROLE_PERMISSIONS,
    ALLOWED_COLUMNS,
    TERMINAL_STAGES,
)


def _tz_utc():
    return timezone.utc


class Orchestrator:
    """Pipeline-based multi-agent coordination library.

    DB initialisation, registration, queries, and heartbeat.
    Stage transitions are handled by pipeline.transition_stage().
    Worker agents: init_pipeline / get_pipeline / recover_pipeline.
    Orchestrator agents: check_and_heartbeat / get_requests_by_stage.
    """

    def __init__(self, db_path=".claude/orchestrator/orchestrator.db"):
        self.db_path = db_path

    def _connect(self):
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ── Database initialisation ──────────────────────────────

    def init_db(self):
        """Create pipeline_state + context tables and transition trigger."""
        conn = self._connect()
        conn.executescript("""
            PRAGMA synchronous=NORMAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS pipeline_state (
                request_id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'request_submitted' CHECK(stage IN (
                    'request_submitted',
                    'conflict_analysis_done',
                    'boundary_analysis_done',
                    'logic_analysis_done',
                    'approved',
                    'rejected',
                    'modifying',
                    'self_review_done',
                    'completion_submitted',
                    'completed',
                    'lock_released'
                )),
                revision INTEGER NOT NULL DEFAULT 0,
                reason TEXT,
                scope_json TEXT,
                plan_json TEXT,
                self_review_json TEXT,
                constraints_json TEXT,
                approval_status TEXT,
                granted_scope_json TEXT,
                rejection_reason TEXT,
                reviewed_by TEXT,
                completed_at TEXT,
                commits_json TEXT,
                sync_notes TEXT,
                context_updates_json TEXT,
                conflict_analysis_json TEXT,
                boundary_analysis_json TEXT,
                logic_analysis_json TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_pipeline_stage ON pipeline_state(stage);
            CREATE INDEX IF NOT EXISTS idx_pipeline_agent ON pipeline_state(agent);

            CREATE TABLE IF NOT EXISTS context (
                id INTEGER PRIMARY KEY CHECK(id=1),
                last_commit TEXT,
                agent_history_json TEXT DEFAULT '[]',
                warnings_json TEXT DEFAULT '[]',
                boundaries_json TEXT,
                pipeline TEXT,
                api_contract TEXT,
                meta_fields TEXT,
                orchestrator_id TEXT,
                orchestrator_heartbeat TEXT,
                orchestrator_started_at TEXT,
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );
            INSERT OR IGNORE INTO context (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                role TEXT NOT NULL,
                stage_from TEXT NOT NULL,
                stage_to TEXT NOT NULL,
                revision_before INTEGER,
                revision_after INTEGER,
                payload_json TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log(request_id);
            CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);

            CREATE TRIGGER IF NOT EXISTS tr_stage_transition
            BEFORE UPDATE ON pipeline_state
            WHEN OLD.stage != NEW.stage
            BEGIN
                SELECT CASE
                    WHEN NEW.revision != OLD.revision + 1
                    THEN RAISE(ABORT, 'CAS failure: revision must increment by 1')
                END;

                SELECT CASE
                    WHEN (OLD.stage='request_submitted' AND NEW.stage IN ('conflict_analysis_done','rejected'))
                      OR (OLD.stage='conflict_analysis_done' AND NEW.stage='boundary_analysis_done')
                      OR (OLD.stage='boundary_analysis_done' AND NEW.stage='logic_analysis_done')
                      OR (OLD.stage='logic_analysis_done' AND NEW.stage IN ('approved','rejected'))
                      OR (OLD.stage='approved' AND NEW.stage='modifying')
                      OR (OLD.stage='modifying' AND NEW.stage='self_review_done')
                      OR (OLD.stage='self_review_done' AND NEW.stage='completion_submitted')
                      OR (OLD.stage='completion_submitted' AND NEW.stage='completed')
                      OR (OLD.stage='completed' AND NEW.stage='lock_released')
                    THEN NULL
                    ELSE RAISE(ABORT, 'Invalid transition: ' || OLD.stage || ' -> ' || NEW.stage)
                END;
            END;
        """)
        conn.commit()
        conn.close()

    def migrate(self):
        """Add missing columns and tables from older versions."""
        conn = self._connect()
        for col in ["orchestrator_id", "orchestrator_heartbeat",
                     "orchestrator_started_at", "boundaries_json"]:
            try:
                conn.execute("ALTER TABLE context ADD COLUMN {} TEXT".format(col))
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise
        for col in ["conflict_analysis_json", "boundary_analysis_json",
                     "logic_analysis_json"]:
            try:
                conn.execute("ALTER TABLE pipeline_state ADD COLUMN {} TEXT".format(col))
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise
        # audit_log table (added post pipeline.py extraction)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                role TEXT NOT NULL,
                stage_from TEXT NOT NULL,
                stage_to TEXT NOT NULL,
                revision_before INTEGER,
                revision_after INTEGER,
                payload_json TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log(request_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)"
        )
        conn.commit()
        conn.close()

    # ── Role registration (uses context singleton) ───────────

    def check_orchestrator_alive(self):
        """Return (is_alive: bool, orchestrator_id: str|None)."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT orchestrator_id, orchestrator_heartbeat FROM context WHERE id=1"
        )
        row = cur.fetchone()
        conn.close()

        if not row or not row[1]:
            return False, None

        heartbeat = row[1]
        conn2 = self._connect()
        cur2 = conn2.cursor()
        cur2.execute(
            "SELECT datetime(?) < datetime('now','localtime','-90 seconds')",
            (heartbeat,),
        )
        expired = bool(cur2.fetchone()[0])
        conn2.close()
        return (not expired), row[0]

    def try_register(self, session_id=None):
        """Attempt to register as orchestrator. Returns 'orchestrator' or 'worker'."""
        if session_id is None:
            session_id = str(uuid.uuid4())[:8]

        alive, _ = self.check_orchestrator_alive()
        if alive:
            return "worker"

        conn = self._connect()
        cur = conn.cursor()
        cur.execute("""
            UPDATE context SET
                orchestrator_id = ?,
                orchestrator_heartbeat = datetime('now','localtime'),
                orchestrator_started_at = datetime('now','localtime')
            WHERE id = 1
              AND (orchestrator_heartbeat IS NULL
                   OR datetime(orchestrator_heartbeat) < datetime('now','localtime','-90 seconds'))
        """, (session_id,))
        conn.commit()
        role = "orchestrator" if cur.rowcount == 1 else "worker"
        conn.close()
        return role

    def send_heartbeat(self, orchestrator_id):
        """Update heartbeat in context. Returns True if still registered."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("""
            UPDATE context SET orchestrator_heartbeat = datetime('now','localtime')
            WHERE id = 1 AND orchestrator_id = ?
        """, (orchestrator_id,))
        conn.commit()
        ok = cur.rowcount == 1
        conn.close()
        return ok

    def check_and_heartbeat(self, orchestrator_id):
        """One-shot check for new pipeline events + heartbeat refresh.

        Replaces ``run_monitor()`` for Claude Code agent workflows that
        cannot run a persistent blocking loop.  Call this once per
        invocation (e.g. via ``/loop``) and act on the returned items.

        Returns:
            {"status": "ok", "items": [...]}
            {"status": "takeover", "items": []}
        """
        if not self.send_heartbeat(orchestrator_id):
            return {"status": "takeover", "items": []}

        items = []
        for r in self.get_requests_by_stage("request_submitted"):
            items.append({
                "type": "new_request",
                "request_id": r["request_id"],
                "agent": r["agent"],
            })
        for c in self.get_requests_by_stage("completion_submitted"):
            items.append({
                "type": "new_completion",
                "request_id": c["request_id"],
                "agent": c["agent"],
            })
        return {"status": "ok", "items": items}

    # ── Pipeline operations ──────────────────────────────────

    def init_pipeline(self, agent, reason, scope, plan, self_review, constraints,
                      tz=None):
        """Create a new pipeline row. Returns request_id.

        *scope*, *plan*, *self_review*, and *constraints* are Python
        dicts/lists — serialised to JSON internally.

        Raises RuntimeError if *agent* already has an active
        (non-terminal) pipeline.
        """
        active = self.get_pending_requests(agent)
        if active:
            raise RuntimeError(
                "Agent '{}' already has active pipeline(s): {}. "
                "Complete or reject them before starting a new one.".format(
                    agent, ", ".join(r["request_id"] for r in active)
                )
            )
        if tz is None:
            tz = _tz_utc()
        req_id = "{}-{}-{}".format(
            agent,
            datetime.now(tz).strftime('%Y%m%d-%H%M%S'),
            str(uuid.uuid4())[:6],
        )

        conn = self._connect()
        conn.execute("""
            INSERT INTO pipeline_state
                (request_id, agent, stage, reason,
                 scope_json, plan_json, self_review_json, constraints_json)
            VALUES (?, ?, 'request_submitted', ?, ?, ?, ?, ?)
        """, (
            req_id, agent, reason,
            json.dumps(scope, ensure_ascii=False),
            json.dumps(plan, ensure_ascii=False),
            json.dumps(self_review, ensure_ascii=False),
            json.dumps(constraints, ensure_ascii=False),
        ))
        conn.commit()
        conn.close()
        return req_id

    def get_pipeline(self, request_id):
        """Return full pipeline row as dict, or None.

        JSON columns are deserialised back to Python objects.
        """
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM pipeline_state WHERE request_id=?", (request_id,)
        )
        row = cur.fetchone()
        if not row:
            conn.close()
            return None
        cols = [desc[0] for desc in cur.description]
        conn.close()

        result = dict(zip(cols, row))
        for key in (
            'scope_json', 'plan_json', 'self_review_json', 'constraints_json',
            'granted_scope_json', 'commits_json', 'context_updates_json',
            'conflict_analysis_json', 'boundary_analysis_json', 'logic_analysis_json',
        ):
            if result.get(key):
                try:
                    result[key] = json.loads(result[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return result

    def get_pending_requests(self, agent):
        """Return non-terminal pipelines for *agent*."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT request_id, stage FROM pipeline_state
            WHERE agent=? AND stage NOT IN ('rejected','lock_released')
            ORDER BY rowid DESC
        """, (agent,))
        rows = cur.fetchall()
        conn.close()
        return [{"request_id": r[0], "stage": r[1]} for r in rows]

    def get_requests_by_stage(self, stage):
        """Return all pipelines at *stage*."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT request_id, agent, stage FROM pipeline_state
            WHERE stage=?
            ORDER BY updated_at
        """, (stage,))
        rows = cur.fetchall()
        conn.close()
        return [{"request_id": r[0], "agent": r[1], "stage": r[2]} for r in rows]

    def recover_pipeline(self, agent):
        """Return (request_id, stage) of the latest non-terminal pipeline for
        *agent*, or (None, None) if nothing to recover."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT request_id, stage FROM pipeline_state
            WHERE agent=? AND stage NOT IN ('rejected','lock_released')
            ORDER BY rowid DESC LIMIT 1
        """, (agent,))
        row = cur.fetchone()
        conn.close()
        if row:
            return row[0], row[1]
        return None, None

    # ── Context & boundaries ─────────────────────────────────

    def get_boundaries(self):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT boundaries_json FROM context WHERE id=1")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return json.loads(row[0])
        return None

    def set_boundaries(self, boundaries):
        conn = self._connect()
        conn.execute(
            "UPDATE context SET boundaries_json=?, "
            "updated_at=datetime('now','localtime') WHERE id=1",
            (json.dumps(boundaries, ensure_ascii=False),),
        )
        conn.commit()
        conn.close()

    def update_context(self, last_commit=None, agent_history=None, warnings=None):
        conn = self._connect()
        cur = conn.cursor()
        parts = []
        params = []
        if last_commit is not None:
            parts.append("last_commit=?")
            params.append(last_commit)
        if agent_history is not None:
            parts.append("agent_history_json=?")
            params.append(json.dumps(agent_history, ensure_ascii=False))
        if warnings is not None:
            parts.append("warnings_json=?")
            params.append(json.dumps(warnings, ensure_ascii=False))
        if parts:
            parts.append("updated_at=datetime('now','localtime')")
            cur.execute(
                "UPDATE context SET {} WHERE id=1".format(','.join(parts)),
                params,
            )
            conn.commit()
        conn.close()

    # ── Utilities ─────────────────────────────────────────────

    @staticmethod
    def make_request_id(agent, tz=None):
        if tz is None:
            tz = _tz_utc()
        return "{}-{}-{}".format(
            agent,
            datetime.now(tz).strftime('%Y%m%d-%H%M%S'),
            str(uuid.uuid4())[:6],
        )
