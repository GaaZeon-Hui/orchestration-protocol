//! Pipeline CRUD, role registration, project operations.
//!
//! Replaces Python `orchestrator.py`.  Stage transitions are delegated to
//! `pipeline::transition_stage()`.

use rusqlite::{params, Connection};
use serde_json::Value as JsonValue;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::pipeline::{transition_stage, Role, Stage};

/// Counter for unique request_id suffixes (no external uuid crate).
static REQ_COUNTER: AtomicU64 = AtomicU64::new(0);

pub struct Orchestrator {
    pub db_path: String,
}

impl Orchestrator {
    pub fn new(db_path: &str) -> Self {
        Self {
            db_path: db_path.to_string(),
        }
    }

    pub fn connect(&self) -> Connection {
        let conn = Connection::open(&self.db_path).unwrap();
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;")
            .unwrap();
        conn
    }

    // ── Pipeline operations ──────────────────────────────

    /// Create a new pipeline row.  Returns `request_id`.
    ///
    /// *reason* and *plan* are JSON values serialised to TEXT columns.
    /// Uses `SystemTime` + atomic counter instead of external uuid/chrono crates.
    /// Raises `Err` if *agent* already has an active (non-terminal) pipeline.
    pub fn init_pipeline(
        &self,
        agent: &str,
        reason: &JsonValue,
        plan: &JsonValue,
    ) -> Result<String, String> {
        let pending = self.get_pending_requests(agent);
        if !pending.is_empty() {
            return Err(format!(
                "Agent '{}' already has active pipeline(s): {:?}",
                agent,
                pending.iter().map(|r| &r.request_id).collect::<Vec<_>>()
            ));
        }

        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let counter = REQ_COUNTER.fetch_add(1, Ordering::Relaxed);
        let req_id = format!("{}-{}-{:06x}", agent, ts, counter % 0xFFFFFF);

        let conn = self.connect();
        conn.execute(
            "INSERT INTO pipeline_state (request_id, agent, stage, reason_json, plan_json, review_round)
             VALUES (?1, ?2, 'init', ?3, ?4, 1)",
            params![
                req_id,
                agent,
                serde_json::to_string(reason).map_err(|e| e.to_string())?,
                serde_json::to_string(plan).map_err(|e| e.to_string())?,
            ],
        )
        .map_err(|e| e.to_string())?;

        Ok(req_id)
    }

    /// Return full pipeline row as a `HashMap`, with JSON columns deserialised.
    pub fn get_pipeline(&self, request_id: &str) -> Option<HashMap<String, JsonValue>> {
        let conn = self.connect();
        let mut stmt = conn
            .prepare("SELECT * FROM pipeline_state WHERE request_id=?1")
            .ok()?;
        let cols: Vec<String> = stmt
            .column_names()
            .iter()
            .map(|c| c.to_string())
            .collect();

        let row_values: Vec<Option<String>> = stmt
            .query_row(params![request_id], |row| {
                let mut vals = Vec::new();
                for i in 0..cols.len() {
                    let v: Option<String> = row.get(i).ok();
                    vals.push(v);
                }
                Ok(vals)
            })
            .ok()?;

        let json_cols: std::collections::HashSet<&str> = [
            "reason_json",
            "plan_json",
            "plan_r2",
            "plan_r3",
            "plan_r4",
            "commits_json",
            "feedback_r1",
            "feedback_r2",
            "feedback_r3",
            "feedback_r4",
            "completion_r1",
            "completion_r2",
            "completion_r3",
            "completion_r4",
            "human_intervention",
        ]
        .iter()
        .cloned()
        .collect();

        let mut result = HashMap::new();
        for (i, col) in cols.iter().enumerate() {
            if let Some(ref v) = row_values[i] {
                if json_cols.contains(col.as_str()) {
                    if let Ok(j) = serde_json::from_str(v) {
                        result.insert(col.clone(), j);
                        continue;
                    }
                }
                if col == "review_round" {
                    if let Ok(n) = v.parse::<i64>() {
                        result.insert(col.clone(), JsonValue::Number(n.into()));
                        continue;
                    }
                }
                if col == "revision" {
                    if let Ok(n) = v.parse::<i64>() {
                        result.insert(col.clone(), JsonValue::Number(n.into()));
                        continue;
                    }
                }
                result.insert(col.clone(), JsonValue::String(v.clone()));
            }
        }
        Some(result)
    }

    /// Return non-terminal pipelines for *agent*.
    pub fn get_pending_requests(&self, agent: &str) -> Vec<PendingInfo> {
        let conn = self.connect();
        let mut stmt = conn
            .prepare(
                "SELECT request_id, stage FROM pipeline_state
                 WHERE agent=?1 AND stage NOT IN ('rejected','lock_released')
                 ORDER BY rowid DESC",
            )
            .unwrap();
        stmt.query_map(params![agent], |row| {
            Ok(PendingInfo {
                request_id: row.get(0)?,
                stage: row.get(1)?,
            })
        })
        .unwrap()
        .filter_map(|r| r.ok())
        .collect()
    }

    /// Return all pipelines at *stage*.
    pub fn get_requests_by_stage(&self, stage: &str) -> Vec<RequestInfo> {
        let conn = self.connect();
        let mut stmt = conn
            .prepare(
                "SELECT request_id, agent, stage FROM pipeline_state
                 WHERE stage=?1 ORDER BY updated_at",
            )
            .unwrap();
        stmt.query_map(params![stage], |row| {
            Ok(RequestInfo {
                request_id: row.get(0)?,
                agent: row.get(1)?,
                stage: row.get(2)?,
            })
        })
        .unwrap()
        .filter_map(|r| r.ok())
        .collect()
    }

    /// Return (request_id, stage) of the latest non-terminal pipeline for *agent*.
    pub fn recover_pipeline(&self, agent: &str) -> Option<(String, String)> {
        let conn = self.connect();
        conn.query_row(
            "SELECT request_id, stage FROM pipeline_state
             WHERE agent=?1 AND stage NOT IN ('rejected','lock_released')
             ORDER BY rowid DESC LIMIT 1",
            params![agent],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .ok()
    }

    /// Resolve orphan locks on verified pipelines.
    pub fn resolve_orphan_locks(&self, timeout_seconds: i64) -> Vec<String> {
        let conn = self.connect();
        let sql = format!(
            "SELECT request_id, agent, revision FROM pipeline_state
             WHERE stage='verified'
               AND updated_at < datetime('now','localtime','-{} seconds')",
            timeout_seconds
        );
        let mut stmt = conn.prepare(&sql).unwrap();
        let rows: Vec<(String, String, i64)> = stmt
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
            .unwrap()
            .filter_map(|r| r.ok())
            .collect();

        let mut resolved = Vec::new();
        for (req_id, _agent, rev) in rows {
            let kwargs = HashMap::new();
            match transition_stage(&conn, &req_id, Stage::LockReleased, Role::Orchestrator, rev, &kwargs) {
                Ok(_) => resolved.push(req_id),
                Err(_) => {}
            }
        }
        resolved
    }

    // ── Role registration ────────────────────────────────

    /// Consult register table for role.
    pub fn try_register(&self, agent_id: &str) -> String {
        let conn = self.connect();
        conn.query_row(
            "SELECT role FROM register WHERE agent_id=?1",
            params![agent_id],
            |row| row.get::<_, String>(0),
        )
        .unwrap_or_else(|_| "worker".to_string())
    }

    // ── Project table ────────────────────────────────────

    pub fn create_project(&self, project_id: &str, content: &str) {
        let conn = self.connect();
        conn.execute(
            "INSERT INTO project (id, content) VALUES (?1, ?2)",
            params![project_id, content],
        )
        .ok();
    }

    pub fn get_project(&self, project_id: &str) -> Option<HashMap<String, String>> {
        let conn = self.connect();
        let mut stmt = conn
            .prepare("SELECT id, content, file_index, change_time, file_use, agent_status FROM project WHERE id=?1")
            .ok()?;
        stmt.query_row(params![project_id], |row| {
            let mut map = HashMap::new();
            let cols = ["id", "content", "file_index", "change_time", "file_use", "agent_status"];
            for (i, col) in cols.iter().enumerate() {
                if let Ok(Some(v)) = row.get::<_, Option<String>>(i) {
                    map.insert(col.to_string(), v);
                }
            }
            Ok(map)
        })
        .ok()
    }

    // ── Register table ───────────────────────────────────

    pub fn get_register(&self, agent_id: &str) -> Option<HashMap<String, JsonValue>> {
        let conn = self.connect();
        conn.query_row(
            "SELECT agent_id, role, schema_json FROM register WHERE agent_id=?1",
            params![agent_id],
            |row| {
                let schema_str: Option<String> = row.get(2)?;
                let schema = schema_str
                    .and_then(|s| serde_json::from_str(&s).ok())
                    .unwrap_or(JsonValue::Null);
                let mut map = HashMap::new();
                map.insert("agent_id".to_string(), JsonValue::String(row.get(0)?));
                map.insert("role".to_string(), JsonValue::String(row.get(1)?));
                map.insert("schema_json".to_string(), schema);
                Ok(map)
            },
        )
        .ok()
    }
}

#[derive(Debug)]
pub struct PendingInfo {
    pub request_id: String,
    pub stage: String,
}

#[derive(Debug)]
pub struct RequestInfo {
    pub request_id: String,
    pub agent: String,
    pub stage: String,
}

// ── Tests ────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::Ordering;

    fn setup() -> Orchestrator {
        let db_path = format!("/tmp/test_orch_{}.db", 
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos());
        let orch = Orchestrator { db_path: db_path.clone() };
        // Ensure clean DB
        let _ = std::fs::remove_file(&db_path);
        let conn = orch.connect();
        crate::db::init_db(&conn);
        conn.execute(
            "INSERT INTO register (agent_id, role, schema_json) VALUES ('py-agent', 'worker', '{}')",
            [],
        )
        .ok();
        drop(conn);
        orch
    }

    fn teardown(orch: &Orchestrator) {
        let _ = std::fs::remove_file(&orch.db_path);
    }

    #[test]
    fn test_init_pipeline() {
        let orch = setup();
        let req_id = orch
            .init_pipeline(
                "py-agent",
                &serde_json::json!({"reason": "test"}),
                &serde_json::json!({"files": ["a.py"]}),
            )
            .unwrap();
        assert!(req_id.starts_with("py-agent-"));

        let p = orch.get_pipeline(&req_id).unwrap();
        // Keys may differ if column_names() returns different casing
        let stage = p.get("stage").or_else(|| p.get("Stage")).unwrap();
        assert_eq!(stage, "init");
        teardown(&orch);
    }

    #[test]
    fn test_get_pipeline_none() {
        let orch = setup();
        assert!(orch.get_pipeline("nonexistent").is_none());
        teardown(&orch);
    }

    #[test]
    fn test_init_pipeline_rejects_duplicate() {
        let orch = setup();
        orch.init_pipeline(
            "py-agent",
            &serde_json::json!({"reason": "first"}),
            &serde_json::json!({"files": ["a.py"]}),
        )
        .unwrap();
        let result = orch.init_pipeline(
            "py-agent",
            &serde_json::json!({"reason": "second"}),
            &serde_json::json!({"files": ["b.py"]}),
        );
        assert!(result.is_err());
        teardown(&orch);
    }

    #[test]
    fn test_create_and_get_project() {
        let orch = setup();
        orch.create_project("proj-1", "test project");
        let p = orch.get_project("proj-1").unwrap();
        assert_eq!(p.get("id").map(|s| s.as_str()), Some("proj-1"));
        assert_eq!(p.get("content").map(|s| s.as_str()), Some("test project"));
        teardown(&orch);
    }

    #[test]
    fn test_try_register() {
        let orch = setup();
        assert_eq!(orch.try_register("py-agent"), "worker");
        assert_eq!(orch.try_register("unknown"), "worker");
        teardown(&orch);
    }
}
