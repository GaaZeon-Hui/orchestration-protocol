//! Pipeline state machine — Stage enum, Role enum, transition_stage().
//!
//! Replaces Python `pipeline.py`.  The Rust type system makes the SQL trigger
//! redundant (Stage::from_str rejects invalid stage strings at the boundary),
//! but the trigger is kept in db.rs for defence-in-depth against raw SQL.

use std::collections::{HashMap, HashSet};

// ── Stage ────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Stage {
    Init,
    OrchestratorGate,
    WorkerModify,
    ReviewerCheck,
    OrchestratorArbiter,
    Verified,
    Rejected,
    LockReleased,
}

impl Stage {
    pub fn as_str(&self) -> &'static str {
        match self {
            Stage::Init => "init",
            Stage::OrchestratorGate => "orchestrator_gate",
            Stage::WorkerModify => "worker_modify",
            Stage::ReviewerCheck => "reviewer_check",
            Stage::OrchestratorArbiter => "orchestrator_arbiter",
            Stage::Verified => "verified",
            Stage::Rejected => "rejected",
            Stage::LockReleased => "lock_released",
        }
    }

    pub fn from_str(s: &str) -> Option<Stage> {
        match s {
            "init" => Some(Stage::Init),
            "orchestrator_gate" => Some(Stage::OrchestratorGate),
            "worker_modify" => Some(Stage::WorkerModify),
            "reviewer_check" => Some(Stage::ReviewerCheck),
            "orchestrator_arbiter" => Some(Stage::OrchestratorArbiter),
            "verified" => Some(Stage::Verified),
            "rejected" => Some(Stage::Rejected),
            "lock_released" => Some(Stage::LockReleased),
            _ => None,
        }
    }

    pub fn is_terminal(&self) -> bool {
        matches!(self, Stage::Rejected | Stage::LockReleased)
    }
}

// ── Role ─────────────────────────────────────────────────────

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Role {
    Worker,
    Orchestrator,
    Reviewer,
}

impl Role {
    pub fn as_str(&self) -> &'static str {
        match self {
            Role::Worker => "worker",
            Role::Orchestrator => "orchestrator",
            Role::Reviewer => "reviewer",
        }
    }

    pub fn from_str(s: &str) -> Option<Role> {
        match s {
            "worker" => Some(Role::Worker),
            "orchestrator" => Some(Role::Orchestrator),
            "reviewer" => Some(Role::Reviewer),
            _ => None,
        }
    }
}

// ── Constants (mirrors Python VALID_TRANSITIONS / ROLE_PERMISSIONS / ALLOWED_COLUMNS) ──

pub fn valid_transitions() -> HashMap<Stage, Vec<Stage>> {
    use Stage::*;
    HashMap::from([
        (Init, vec![OrchestratorGate]),
        (OrchestratorGate, vec![WorkerModify, Rejected]),
        (WorkerModify, vec![ReviewerCheck]),
        (ReviewerCheck, vec![OrchestratorArbiter]),
        (OrchestratorArbiter, vec![Verified, WorkerModify]),
        (Verified, vec![LockReleased]),
    ])
}

pub fn role_permissions() -> HashMap<Role, HashSet<Stage>> {
    use Role::*;
    use Stage::*;
    HashMap::from([
        (Worker, HashSet::from([Init, WorkerModify, Verified])),
        (
            Orchestrator,
            HashSet::from([Init, OrchestratorGate, OrchestratorArbiter, Verified, WorkerModify]),
        ),
        (Reviewer, HashSet::from([ReviewerCheck])),
    ])
}

pub fn terminal_stages() -> HashSet<Stage> {
    use Stage::*;
    HashSet::from([Rejected, LockReleased])
}

pub fn allowed_columns() -> HashSet<&'static str> {
    HashSet::from([
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
    ])
}

// ── Errors ───────────────────────────────────────────────────

#[derive(Debug)]
pub enum TransitionError {
    PipelineNotFound(String),
    InvalidStageDb(String),
    TerminalStage(Stage),
    InvalidTransition(Stage, Stage),
    RevisionMismatch { expected: i64, actual: i64 },
    PermissionDenied(Role, Stage),
    CasFailed,
    Db(rusqlite::Error),
}

impl std::fmt::Display for TransitionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            TransitionError::PipelineNotFound(id) => write!(f, "Pipeline not found: {}", id),
            TransitionError::InvalidStageDb(s) => write!(f, "Invalid stage in DB: {}", s),
            TransitionError::TerminalStage(s) => {
                write!(f, "Stage {:?} is terminal — no outgoing transitions", s)
            }
            TransitionError::InvalidTransition(from, to) => {
                write!(f, "Invalid transition: {:?} -> {:?}", from, to)
            }
            TransitionError::RevisionMismatch { expected, actual } => {
                write!(
                    f,
                    "Revision mismatch: expected {}, actual {}",
                    expected, actual
                )
            }
            TransitionError::PermissionDenied(role, stage) => {
                write!(f, "Role {:?} cannot advance from stage {:?}", role, stage)
            }
            TransitionError::CasFailed => write!(f, "CAS failed: concurrent modification"),
            TransitionError::Db(e) => write!(f, "Database error: {}", e),
        }
    }
}

impl std::error::Error for TransitionError {}

impl From<rusqlite::Error> for TransitionError {
    fn from(e: rusqlite::Error) -> Self {
        TransitionError::Db(e)
    }
}

// ── Core function ────────────────────────────────────────────

/// Advance pipeline stage with permission check, CAS update, and audit log.
///
/// Same semantics as Python `transition_stage()`:
///   1. Read current stage + revision
///   2. Validate transition path
///   3. Check revision matches caller's expectation
///   4. Check role permission
///   5. Filter kwargs through ALLOWED_COLUMNS
///   6. BEGIN → UPDATE (CAS) → INSERT audit_log → COMMIT
///
/// Returns `(new_revision, new_stage)` on success.
pub fn transition_stage(
    conn: &rusqlite::Connection,
    request_id: &str,
    new_stage: Stage,
    role: Role,
    revision: i64,
    kwargs: &HashMap<String, String>,
) -> Result<(i64, Stage), TransitionError> {
    // 1. Read current state
    let mut stmt = conn.prepare(
        "SELECT stage, revision FROM pipeline_state WHERE request_id=?",
    )?;
    let row = stmt
        .query_row(rusqlite::params![request_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })
        .map_err(|_| TransitionError::PipelineNotFound(request_id.to_string()))?;
    let (current_stage_str, current_revision) = row;

    let current_stage = Stage::from_str(&current_stage_str)
        .ok_or(TransitionError::InvalidStageDb(current_stage_str))?;

    // 2. Transition path validation
    let vt = valid_transitions();
    let allowed = vt
        .get(&current_stage)
        .ok_or(TransitionError::TerminalStage(current_stage))?;
    if !allowed.contains(&new_stage) {
        return Err(TransitionError::InvalidTransition(current_stage, new_stage));
    }

    // 3. Revision match
    if current_revision != revision {
        return Err(TransitionError::RevisionMismatch {
            expected: revision,
            actual: current_revision,
        });
    }

    // 4. Permission check
    let rp = role_permissions();
    let role_stages = rp.get(&role).unwrap();
    if !role_stages.contains(&current_stage) {
        return Err(TransitionError::PermissionDenied(role, current_stage));
    }

    // 5. Whitelist filter kwargs
    let ac = allowed_columns();
    let filtered: Vec<(&String, &String)> = kwargs
        .iter()
        .filter(|(k, _)| ac.contains(k.as_str()))
        .collect();

    // 6. Build UPDATE with dynamic params
    let new_rev = current_revision + 1;
    let mut set_parts = vec![
        "stage = ?".to_string(),
        "revision = revision + 1".to_string(),
        "updated_at = datetime('now','localtime')".to_string(),
    ];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = vec![
        Box::new(new_stage.as_str().to_string()),
    ];

    for (key, value) in &filtered {
        set_parts.push(format!("{} = ?", key));
        params.push(Box::new(value.to_string()));
    }

    params.push(Box::new(request_id.to_string()));
    params.push(Box::new(current_stage.as_str().to_string()));
    params.push(Box::new(current_revision.to_string()));

    let sql = format!(
        "UPDATE pipeline_state SET {} WHERE request_id=? AND stage=? AND revision=?",
        set_parts.join(", "),
    );

    // 7. BEGIN → UPDATE (CAS) → INSERT audit_log → COMMIT
    conn.execute_batch("BEGIN")?;

    let param_refs: Vec<&dyn rusqlite::types::ToSql> = params.iter().map(|p| p.as_ref()).collect();
    let affected = conn.execute(&sql, param_refs.as_slice())?;

    if affected == 0 {
        conn.execute_batch("ROLLBACK")?;
        return Err(TransitionError::CasFailed);
    }

    // audit_log
    let payload = if filtered.is_empty() {
        None
    } else {
        let map: HashMap<String, String> = filtered
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        serde_json::to_string(&map).ok()
    };

    conn.execute(
        "INSERT INTO audit_log (request_id, role, stage_from, stage_to, revision_before, revision_after, payload_json)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        rusqlite::params![
            request_id,
            role.as_str(),
            current_stage.as_str(),
            new_stage.as_str(),
            current_revision,
            new_rev,
            payload,
        ],
    )?;

    conn.execute_batch("COMMIT")?;
    Ok((new_rev, new_stage))
}

// ── Tests ────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    fn setup_test_db() -> (rusqlite::Connection, String) {
        let conn = rusqlite::Connection::open_in_memory().unwrap();
        crate::db::init_db(&conn);
        let req_id = format!("py-agent-20260101-000000-xxxxxx");
        conn.execute(
            "INSERT INTO pipeline_state (request_id, agent, stage, reason_json, plan_json, review_round)
             VALUES (?1, 'py-agent', 'init', '{}', '{}', 1)",
            rusqlite::params![req_id],
        )
        .unwrap();
        (conn, req_id)
    }

    #[test]
    fn test_valid_transition() {
        let (conn, req_id) = setup_test_db();
        let result = transition_stage(
            &conn,
            &req_id,
            Stage::OrchestratorGate,
            Role::Orchestrator,
            0,
            &HashMap::new(),
        );
        assert!(result.is_ok());
        let (rev, stage) = result.unwrap();
        assert_eq!(rev, 1);
        assert_eq!(stage, Stage::OrchestratorGate);
    }

    #[test]
    fn test_permission_denied() {
        let (conn, req_id) = setup_test_db();
        let result = transition_stage(
            &conn,
            &req_id,
            Stage::OrchestratorGate,
            Role::Reviewer,
            0,
            &HashMap::new(),
        );
        assert!(matches!(
            result,
            Err(TransitionError::PermissionDenied(Role::Reviewer, Stage::Init))
        ));
    }

    #[test]
    fn test_revision_mismatch() {
        let (conn, req_id) = setup_test_db();
        let result = transition_stage(
            &conn,
            &req_id,
            Stage::OrchestratorGate,
            Role::Orchestrator,
            99,
            &HashMap::new(),
        );
        assert!(matches!(result, Err(TransitionError::RevisionMismatch { .. })));
    }

    #[test]
    fn test_invalid_transition() {
        let (conn, req_id) = setup_test_db();
        let result = transition_stage(
            &conn,
            &req_id,
            Stage::LockReleased,
            Role::Orchestrator,
            0,
            &HashMap::new(),
        );
        assert!(matches!(
            result,
            Err(TransitionError::InvalidTransition(Stage::Init, Stage::LockReleased))
        ));
    }

    #[test]
    fn test_terminal_stages_have_no_exits() {
        let v = valid_transitions();
        for ts in terminal_stages() {
            assert!(
                !v.contains_key(&ts),
                "{:?} should not have outgoing transitions",
                ts
            );
        }
    }

    #[test]
    fn test_all_stages_covered() {
        let v = valid_transitions();
        let sources: HashSet<_> = v.keys().cloned().collect();
        let targets: HashSet<_> = v.values().flat_map(|v| v.iter()).cloned().collect();
        let all: HashSet<_> = sources.union(&targets).cloned().collect();
        assert!(all.contains(&Stage::Init));
        assert!(all.contains(&Stage::LockReleased));
        assert!(all.contains(&Stage::Rejected));
    }

    #[test]
    fn test_role_permissions_in_valid_transitions() {
        let v = valid_transitions();
        for (_, stages) in role_permissions() {
            for s in &stages {
                assert!(v.contains_key(s), "{:?} not in VALID_TRANSITIONS", s);
            }
        }
    }

    #[test]
    fn test_stage_roundtrip() {
        for (s, name) in [
            (Stage::Init, "init"),
            (Stage::LockReleased, "lock_released"),
            (Stage::Rejected, "rejected"),
        ] {
            assert_eq!(Stage::from_str(name), Some(s));
            assert_eq!(s.as_str(), name);
        }
    }

    #[test]
    fn test_role_roundtrip() {
        for (r, name) in [
            (Role::Worker, "worker"),
            (Role::Orchestrator, "orchestrator"),
            (Role::Reviewer, "reviewer"),
        ] {
            assert_eq!(Role::from_str(name), Some(r));
            assert_eq!(r.as_str(), name);
        }
    }
}
