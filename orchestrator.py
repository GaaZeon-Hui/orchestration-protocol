"""
Orchestration Protocol — SQLite-based multi-agent coordination library.

Single Orchestrator class wrapping all protocol operations:
database init, role registration, locking, requests, approvals,
completions, context/boundaries, deduplication, and monitor loop.
"""

import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone


def _tz_plus8():
    return timezone(timedelta(hours=8))


class Orchestrator:
    def __init__(self, db_path=".claude/orchestrator/orchestrator.db"):
        self.db_path = db_path

    def _connect(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    # ── Database ────────────────────────────────────────────

    def init_db(self):
        conn = self._connect()
        conn.executescript("""
            PRAGMA synchronous=NORMAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS lock (
                id INTEGER PRIMARY KEY CHECK(id=1),
                state TEXT NOT NULL DEFAULT 'idle',
                holder TEXT,
                request_id TEXT,
                scope_json TEXT,
                acquired_at TEXT,
                expires_at TEXT,
                orchestrator_id TEXT,
                orchestrator_heartbeat TEXT,
                orchestrator_started_at TEXT
            );
            INSERT OR IGNORE INTO lock (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT,
                scope_json TEXT,
                plan_json TEXT,
                self_review_json TEXT,
                constraints_json TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
            CREATE INDEX IF NOT EXISTS idx_requests_updated ON requests(updated_at);
            CREATE INDEX IF NOT EXISTS idx_requests_agent ON requests(agent);

            CREATE TABLE IF NOT EXISTS approvals (
                request_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                granted_scope_json TEXT,
                rejection_reason TEXT,
                reviewed_by TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (request_id) REFERENCES requests(id)
            );

            CREATE TABLE IF NOT EXISTS completions (
                request_id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                completed_at TEXT,
                self_review_json TEXT,
                commits_json TEXT,
                sync_notes TEXT,
                context_updates_json TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (request_id) REFERENCES requests(id)
            );

            CREATE TABLE IF NOT EXISTS context (
                id INTEGER PRIMARY KEY CHECK(id=1),
                last_commit TEXT,
                agent_history_json TEXT DEFAULT '[]',
                warnings_json TEXT DEFAULT '[]',
                boundaries_json TEXT,
                pipeline TEXT,
                api_contract TEXT,
                meta_fields TEXT,
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );
            INSERT OR IGNORE INTO context (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS processed (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                status TEXT NOT NULL,
                processed_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE INDEX IF NOT EXISTS idx_processed_type ON processed(type);
        """)
        conn.commit()
        conn.close()

    def migrate(self):
        """Add missing columns to existing lock table."""
        conn = self._connect()
        for col in ["orchestrator_id", "orchestrator_heartbeat", "orchestrator_started_at"]:
            try:
                conn.execute(f"ALTER TABLE lock ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass
        try:
            conn.execute("ALTER TABLE context ADD COLUMN boundaries_json TEXT")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()

    # ── Role Registration ──────────────────────────────────

    def check_orchestrator_alive(self):
        """Return (is_alive: bool, orchestrator_id: str|None)."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT orchestrator_id, orchestrator_heartbeat FROM lock WHERE id=1")
        row = cur.fetchone()
        conn.close()

        if not row or not row[1]:
            return False, None

        heartbeat = row[1]
        conn2 = self._connect()
        cur2 = conn2.cursor()
        cur2.execute("SELECT datetime(?) < datetime('now','localtime','-90 seconds')", (heartbeat,))
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
            UPDATE lock SET
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
        """Update heartbeat. Returns True if still registered, False if taken over."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("""
            UPDATE lock SET orchestrator_heartbeat = datetime('now','localtime')
            WHERE id = 1 AND orchestrator_id = ?
        """, (orchestrator_id,))
        conn.commit()
        ok = cur.rowcount == 1
        conn.close()
        return ok

    # ── Lock ────────────────────────────────────────────────

    def check_lock(self):
        """Return lock dict if locked, None if idle."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT state, holder, request_id, scope_json, acquired_at, expires_at FROM lock WHERE id=1")
        row = cur.fetchone()
        conn.close()
        if row and row[0] == "locked":
            return {
                "state": row[0], "holder": row[1], "request_id": row[2],
                "scope_json": row[3], "acquired_at": row[4], "expires_at": row[5],
            }
        return None

    def acquire_lock(self, agent, req_id, scope, tz=None, timeout_minutes=60):
        """Acquire the task lock. Returns True on success. Raises on failure."""
        if tz is None:
            tz = _tz_plus8()
        now = datetime.now(tz).isoformat()
        expires = (datetime.now(tz) + timedelta(minutes=timeout_minutes)).isoformat()

        conn = self._connect()
        cur = conn.cursor()
        cur.execute("BEGIN")
        try:
            cur.execute("""
                UPDATE lock SET state='locked', holder=?, request_id=?,
                                scope_json=?, acquired_at=?, expires_at=?
                WHERE id=1 AND state='idle'
            """, (agent, req_id, json.dumps(scope, ensure_ascii=False), now, expires))
            if cur.rowcount == 0:
                raise RuntimeError("Lock is already held")
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        conn.close()
        return True

    def release_lock(self):
        conn = self._connect()
        conn.execute("""
            UPDATE lock SET state='idle', holder=NULL, request_id=NULL,
                            scope_json=NULL, acquired_at=NULL, expires_at=NULL
            WHERE id=1
        """)
        conn.commit()
        conn.close()

    # ── Requests ────────────────────────────────────────────

    def submit_request(self, agent, reason, scope, plan, self_review, constraints, tz=None):
        """Insert a new request. Returns the generated request_id."""
        if tz is None:
            tz = _tz_plus8()
        req_id = f"{agent}-{datetime.now(tz).strftime('%Y%m%d-%H%M%S')}"

        conn = self._connect()
        conn.execute("""
            INSERT INTO requests (id, agent, status, reason, scope_json, plan_json,
                                  self_review_json, constraints_json)
            VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
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

    def get_request_status(self, req_id):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT status FROM requests WHERE id=?", (req_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None

    def wait_for_approval(self, req_id, poll_interval=2):
        """Block until request status changes from 'pending'. Returns final status."""
        conn = self._connect()
        last_check = ""
        while True:
            now_check = datetime.now().isoformat()
            cur = conn.cursor()
            cur.execute(
                "SELECT status, updated_at FROM requests WHERE id=? AND updated_at > ?",
                (req_id, last_check),
            )
            row = cur.fetchone()
            if row and row[0] != "pending":
                conn.close()
                return row[0]
            last_check = now_check
            time.sleep(poll_interval)

    def get_approval(self, req_id):
        """Retrieve approval record for a request."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT status, granted_scope_json, rejection_reason FROM approvals WHERE request_id=?", (req_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "status": row[0],
            "granted_scope": json.loads(row[1]) if row[1] else None,
            "rejection_reason": row[2],
        }

    # ── Approvals (transactional) ──────────────────────────

    def issue_approval(self, agent, req_id, granted_scope, decision,
                       rejection_reason=None, tz=None):
        """
        Transactional: acquire lock + update request + insert approval + mark processed.
        decision: 'approved' | 'approved-with-warning' | 'rejected'
        """
        if tz is None:
            tz = _tz_plus8()
        now = datetime.now(tz).isoformat()
        expires = (datetime.now(tz) + timedelta(minutes=60)).isoformat()

        conn = self._connect()
        cur = conn.cursor()
        cur.execute("BEGIN")
        try:
            cur.execute("""
                UPDATE lock SET state='locked', holder=?, request_id=?,
                                scope_json=?, acquired_at=?, expires_at=?
                WHERE id=1 AND state='idle'
            """, (agent, req_id, json.dumps(granted_scope, ensure_ascii=False), now, expires))
            if cur.rowcount == 0:
                raise RuntimeError("Lock is already held")

            cur.execute(
                "UPDATE requests SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
                (decision, req_id),
            )

            cur.execute(
                """INSERT INTO approvals (request_id, status, granted_scope_json,
                                           rejection_reason, reviewed_by)
                   VALUES (?,?,?,?,?)""",
                (req_id, decision, json.dumps(granted_scope, ensure_ascii=False),
                 rejection_reason, "orchestrator"),
            )

            cur.execute(
                "INSERT INTO processed (id, type, status) VALUES (?, 'request', ?)",
                (req_id, decision),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        conn.close()

    # ── Completions ─────────────────────────────────────────

    def submit_completion(self, req_id, agent, completed_at, self_review,
                          commits, sync_notes, context_updates):
        conn = self._connect()
        conn.execute("""
            INSERT INTO completions (request_id, agent, completed_at, self_review_json,
                                     commits_json, sync_notes, context_updates_json)
            VALUES (?,?,?,?,?,?,?)
        """, (
            req_id, agent, completed_at,
            json.dumps(self_review, ensure_ascii=False),
            json.dumps(commits, ensure_ascii=False),
            sync_notes,
            json.dumps(context_updates, ensure_ascii=False),
        ))
        conn.commit()
        conn.close()

    def verify_completion(self, req_id):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute(
            "INSERT INTO processed (id, type, status) VALUES (?, 'completion', 'verified')",
            (req_id,),
        )
        conn.commit()
        conn.close()

    # ── Context & Boundaries ────────────────────────────────

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
            "UPDATE context SET boundaries_json=?, updated_at=datetime('now','localtime') WHERE id=1",
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
            cur.execute(f"UPDATE context SET {','.join(parts)} WHERE id=1", params)
            conn.commit()
        conn.close()

    # ── Processed (dedup) ───────────────────────────────────

    def is_processed(self, id, ptype):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT status FROM processed WHERE id=? AND type=?", (id, ptype))
        row = cur.fetchone()
        conn.close()
        return row is not None

    def mark_processed(self, id, ptype, status):
        conn = self._connect()
        conn.execute(
            "INSERT OR IGNORE INTO processed (id, type, status) VALUES (?,?,?)",
            (id, ptype, status),
        )
        conn.commit()
        conn.close()

    # ── Monitor ─────────────────────────────────────────────

    def run_monitor(self, orchestrator_id, heartbeat_interval=30, poll_interval=2):
        """Generator yielding ('NEW_REQUEST', req_id) or ('NEW_COMPLETION', req_id)."""
        conn = self._connect()
        last_check = ""
        last_heartbeat = 0

        try:
            while True:
                # Capture now before query so next iteration finds items inserted during this one
                now_check = datetime.now().isoformat()
                cur = conn.cursor()

                # Heartbeat
                now_ts = time.time()
                if now_ts - last_heartbeat >= heartbeat_interval:
                    cur.execute("""
                        UPDATE lock SET orchestrator_heartbeat=datetime('now','localtime')
                        WHERE id=1 AND orchestrator_id=?
                    """, (orchestrator_id,))
                    if cur.rowcount == 0:
                        yield ("HEARTBEAT_FAILED", None)
                        return
                    last_heartbeat = now_ts

                # Incremental query using the previous iteration's checkpoint
                cur.execute("""
                    SELECT id, 'request' AS type FROM requests
                    WHERE status='pending' AND updated_at > ?
                      AND id NOT IN (SELECT id FROM processed WHERE type='request')
                    UNION ALL
                    SELECT request_id, 'completion' FROM completions
                    WHERE created_at > ?
                      AND request_id NOT IN (SELECT id FROM processed WHERE type='completion')
                """, (last_check, last_check))

                for row in cur.fetchall():
                    yield ("NEW_REQUEST" if row[1] == "request" else "NEW_COMPLETION", row[0])

                conn.commit()
                last_check = now_check
                time.sleep(poll_interval)
        finally:
            conn.close()

    # ── Utilities ───────────────────────────────────────────

    @staticmethod
    def make_request_id(agent, tz=None):
        if tz is None:
            tz = _tz_plus8()
        return f"{agent}-{datetime.now(tz).strftime('%Y%m%d-%H%M%S')}"
