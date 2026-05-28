//! Database initialisation and migration.
//!
//! Replaces Python `orchestrator.py` init_db / migrate.  Table schemas are
//! identical to the Python version; the SQL trigger provides defence-in-depth
//! against raw-SQL bypass of the Rust state machine.

use rusqlite::Connection;

/// Create all tables and the transition trigger if they do not exist.
pub fn init_db(conn: &Connection) {
    conn.execute_batch(
        "
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA foreign_keys=ON;
        PRAGMA busy_timeout=5000;

        CREATE TABLE IF NOT EXISTS pipeline_state (
            request_id TEXT PRIMARY KEY,
            agent TEXT NOT NULL,
            stage TEXT NOT NULL DEFAULT 'init' CHECK(stage IN (
                'init','orchestrator_gate','worker_modify','reviewer_check',
                'orchestrator_arbiter','verified','rejected','lock_released'
            )),
            revision INTEGER NOT NULL DEFAULT 0,
            reason_json TEXT,
            plan_json TEXT,
            plan_r2 TEXT, plan_r3 TEXT, plan_r4 TEXT,
            commits_json TEXT,
            approval_status TEXT, rejection_reason TEXT,
            feedback_r1 TEXT, feedback_r2 TEXT, feedback_r3 TEXT, feedback_r4 TEXT,
            completion_r1 TEXT, completion_r2 TEXT, completion_r3 TEXT, completion_r4 TEXT,
            human_intervention TEXT,
            review_round INTEGER DEFAULT 1,
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
                ELSE RAISE(ABORT, 'Invalid transition')
            END;
        END;
    ",
    )
    .unwrap();
}

/// Add tables and columns that may be missing from older schema versions.
pub fn migrate(conn: &Connection) {
    // Tables that may not exist in older DBs
    for sql in [
        "CREATE TABLE IF NOT EXISTS project (id TEXT PRIMARY KEY, content TEXT, file_index TEXT, change_time TEXT, file_use TEXT, agent_status TEXT)",
        "CREATE TABLE IF NOT EXISTS register (agent_id TEXT PRIMARY KEY, role TEXT NOT NULL, schema_json TEXT)",
        "CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, role TEXT NOT NULL, stage_from TEXT NOT NULL, stage_to TEXT NOT NULL, revision_before INTEGER, revision_after INTEGER, payload_json TEXT, created_at TEXT DEFAULT (datetime('now','localtime')))",
    ] {
        conn.execute(sql, []).ok();
    }

    // Round-based columns added post v0.1
    for col in [
        "reason_json",
        "plan_json",
        "plan_r2",
        "plan_r3",
        "plan_r4",
        "commits_json",
        "approval_status",
        "rejection_reason",
        "feedback_r1",
        "feedback_r2",
        "feedback_r3",
        "feedback_r4",
        "completion_r1",
        "completion_r2",
        "completion_r3",
        "completion_r4",
        "human_intervention",
        "review_round",
    ] {
        conn.execute(
            &format!("ALTER TABLE pipeline_state ADD COLUMN {} TEXT", col),
            [],
        )
        .ok(); // "duplicate column name" is harmless
    }

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log(request_id)",
        [],
    )
    .ok();
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)",
        [],
    )
    .ok();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_init_db_tables_exist() {
        let conn = Connection::open_in_memory().unwrap();
        init_db(&conn);
        let tables: Vec<String> = conn
            .prepare("SELECT name FROM sqlite_master WHERE type='table'")
            .unwrap()
            .query_map([], |row| row.get(0))
            .unwrap()
            .filter_map(|r| r.ok())
            .collect();

        for t in ["pipeline_state", "project", "register", "audit_log"] {
            assert!(
                tables.contains(&t.to_string()),
                "Table '{}' missing",
                t
            );
        }

        let trigger: String = conn
            .query_row(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name='tr_stage_transition'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(trigger, "tr_stage_transition");
    }

    #[test]
    fn test_migrate_idempotent() {
        let conn = Connection::open_in_memory().unwrap();
        init_db(&conn);
        migrate(&conn);
        migrate(&conn);
        migrate(&conn);
        // No panic = pass
    }
}
