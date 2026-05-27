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
                stage TEXT NOT NULL DEFAULT 'init' CHECK(stage IN (
                    'init',
                    'orchestrator_gate',
                    'worker_modify',
                    'reviewer_check',
                    'orchestrator_arbiter',
                    'verified',
                    'rejected',
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

            CREATE TABLE IF NOT EXISTS project (
                id TEXT PRIMARY KEY,
                content TEXT,
                file_index TEXT,
                change_time TEXT,
                file_use TEXT,
                agent_status TEXT
            );

            CREATE TABLE IF NOT EXISTS register (
                agent_id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                schema_json TEXT
            );

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
                    WHEN (OLD.stage='init' AND NEW.stage='orchestrator_gate')
                      OR (OLD.stage='orchestrator_gate' AND NEW.stage IN ('worker_modify','rejected'))
                      OR (OLD.stage='worker_modify' AND NEW.stage='reviewer_check')
                      OR (OLD.stage='reviewer_check' AND NEW.stage='orchestrator_arbiter')
                      OR (OLD.stage='orchestrator_arbiter' AND NEW.stage IN ('verified','worker_modify'))
                      OR (OLD.stage='verified' AND NEW.stage='lock_released')
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
        # project and register tables (new in 3-role architecture)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS project (
                id TEXT PRIMARY KEY,
                content TEXT,
                file_index TEXT,
                change_time TEXT,
                file_use TEXT,
                agent_status TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS register (
                agent_id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                schema_json TEXT
            )
        """)
        # New pipeline columns (round-based + human intervention)
        new_pipeline_cols = [
            'reason_json', 'plan_json',
            'plan_r2', 'plan_r3', 'plan_r4',
            'commits_json',
            'approval_status', 'rejection_reason',
            'feedback_r1', 'feedback_r2', 'feedback_r3', 'feedback_r4',
            'completion_r1', 'completion_r2', 'completion_r3', 'completion_r4',
            'human_intervention',
            'review_round',
        ]
        for col in new_pipeline_cols:
            try:
                conn.execute(
                    "ALTER TABLE pipeline_state ADD COLUMN {} TEXT".format(col)
                )
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise
        # audit_log table
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

    # ── Role registration (uses register table) ─────────────

    def try_register(self, agent_id=None):
        """Consult register table for role. Returns 'orchestrator', 'worker', or 'reviewer'."""
        if agent_id is None:
            agent_id = str(uuid.uuid4())[:8]

        conn = self._connect()
        row = conn.execute(
            "SELECT role FROM register WHERE agent_id=?",
            (agent_id,)
        ).fetchone()
        conn.close()

        if row:
            return row[0]
        return "worker"

    def check_and_heartbeat(self, orchestrator_id):
        """Scan for new pipeline events.

        In the new architecture, heartbeat is managed via pipeline stage
        scanning. Orch checks for init-stage requests and arbiter-stage items.

        Returns:
            {"status": "ok", "items": [...]}
        """
        items = []
        for r in self.get_requests_by_stage("init"):
            items.append({
                "type": "new_request",
                "request_id": r["request_id"],
                "agent": r["agent"],
            })
        for c in self.get_requests_by_stage("orchestrator_arbiter"):
            items.append({
                "type": "awaiting_arbitration",
                "request_id": c["request_id"],
                "agent": c["agent"],
            })
        return {"status": "ok", "items": items}

    # ── Project table operations ──────────────────────────

    def create_project(self, project_id, content):
        conn = self._connect()
        conn.execute(
            "INSERT INTO project (id, content) VALUES (?, ?)",
            (project_id, content)
        )
        conn.commit()
        conn.close()

    def get_project(self, project_id):
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM project WHERE id=?", (project_id,)
        ).fetchone()
        conn.close()
        if row:
            cols = ['id', 'content', 'file_index', 'change_time', 'file_use', 'agent_status']
            return dict(zip(cols, row))
        return None

    # ── Register table operations ──────────────────────────

    def get_register(self, agent_id):
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM register WHERE agent_id=?", (agent_id,)
        ).fetchone()
        conn.close()
        if row:
            cols = ['agent_id', 'role', 'schema_json']
            result = dict(zip(cols, row))
            if result.get('schema_json'):
                try:
                    result['schema_json'] = json.loads(result['schema_json'])
                except (json.JSONDecodeError, TypeError):
                    pass
            return result
        return None

    def resolve_orphan_locks(self, timeout_seconds=120):
        """Release locks on completed pipelines that have not been
        released within *timeout_seconds*.  Called by orchestrator
        to prevent indefinite lock leaks when a Worker disappears.

        Returns list of request_ids that were resolved.
        """
        conn = self._connect()
        rows = conn.execute("""
            SELECT request_id, agent, revision
            FROM pipeline_state
            WHERE stage = 'verified'
              AND updated_at < datetime('now','localtime','-%d seconds')
        """ % timeout_seconds).fetchall()
        conn.close()

        resolved = []
        for row in rows:
            req_id, agent, rev = row[0], row[1], row[2]
            try:
                transition_stage(
                    req_id, "lock_released", "orchestrator",
                    rev, self.db_path,
                )
                resolved.append(req_id)
            except (RuntimeError, ValueError, PermissionError):
                pass
        return resolved

    # ── Pipeline operations ──────────────────────────────────

    def init_pipeline(self, agent, reason, plan, tz=None):
        """Create a new pipeline row. Returns request_id.

        *reason* and *plan* are Python dicts — serialised to JSON internally.

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
                (request_id, agent, stage, reason_json, plan_json)
            VALUES (?, ?, 'init', ?, ?)
        """, (
            req_id, agent,
            json.dumps(reason, ensure_ascii=False),
            json.dumps(plan, ensure_ascii=False),
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
            'reason_json', 'plan_json',
            'plan_r2', 'plan_r3', 'plan_r4',
            'commits_json',
            'feedback_r1', 'feedback_r2', 'feedback_r3', 'feedback_r4',
            'completion_r1', 'completion_r2', 'completion_r3', 'completion_r4',
            'human_intervention',
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
