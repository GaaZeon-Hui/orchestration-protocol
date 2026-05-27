# Rust Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

> **实施决策记录**
>
> | 决策 | 内容 | 理由 |
> |------|------|------|
> | 放弃 `uuid` + `chrono` crate | request_id 用 `std::time::SystemTime` + 自增计数器 | 零依赖，无需外部 crate，功能等价 |
> | 放弃 `hashmap!{}` 宏 | 测试代码用 `HashMap::from([(k,v),...])` | 标准库，无需 `maplit` crate |
> | 删除 AST hints 功能 | Rust 无成熟 Python AST 解析器 | 当前架构无消费者，影响为零 |
> | 保留 SQL Trigger | 用 `conn.execute_batch()` 执行 SQL 原文 | SQL 不变，Trigger 是 DB 层兜底，等价 |

**Goal:** Rewrite all 5 Python source files and 3 test files to Rust, leveraging compile-time state machine enforcement via enums and match.

**Architecture:** Single `orchestrator` crate with `lib.rs` (core + DB) and `bin/status.rs` (CLI dashboard). `rusqlite` for SQLite, `serde_json` for JSON columns, `clap` for CLI. AST hints feature dropped (no mature Python AST parser in Rust ecosystem; lint boundary checks retained). CAS and permission checks become compile-time safe via enum/map lookups.

**Tech Stack:** Rust 1.85+, rusqlite (bundled), serde_json, clap, git2

---

## File Map

| Python file | Rust equivalent | Role |
|------------|----------------|------|
| `pipeline.py` | `src/pipeline.rs` | State machine types + transition_stage |
| `orchestrator.py` | `src/db.rs` + `src/orchestrator.rs` | DB init, migrate, CRUD, registration |
| `lint.py` | `src/lint.rs` | Boundary checks, plan validation, AST hints → **dropped** |
| `status.py` | `src/bin/status.rs` | CLI dashboard (clap + tabular output) |
| `test_pipeline.py` | `tests/pipeline_tests.rs` | Integration tests |
| `test_orchestrator.py` | `tests/orchestrator_tests.rs` | Integration tests |
| `test_lint.py` | `tests/lint_tests.rs` | Unit tests |

Deleted features: `_extract_hints`, `_compare_asts`, `_collect_defs`, `_parse_safe`, `_get_old_source` — no Rust equivalent for Python AST parsing.

---

### Task 1: Project Scaffolding

**Files:**
- Create: `Cargo.toml`
- Create: `src/lib.rs`
- Create: `src/pipeline.rs`
- Create: `src/db.rs`
- Create: `src/orchestrator.rs`
- Create: `src/lint.rs`
- Create: `src/bin/status.rs`

- [ ] **Step 1: Create Cargo.toml**

```toml
[package]
name = "orchestrator"
version = "0.1.0"
edition = "2021"

[dependencies]
rusqlite = { version = "0.31", features = ["bundled"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
clap = { version = "4", features = ["derive"] }
git2 = "0.19"

[[bin]]
name = "status"
path = "src/bin/status.rs"
```

- [ ] **Step 2: Verify project builds**

```bash
cargo build
```

Expected: `Finished dev [unoptimized + debuginfo]`

- [ ] **Step 3: Commit**

```bash
git add Cargo.toml src/ && git commit -m "feat: scaffold Rust project with dependencies"
```

---

### Task 2: Stage Enum + Transition Map (pipeline.rs)

**Files:**
- Modify: `src/pipeline.rs`

Rust's enum replaces Python's string-based stages. The match exhaustiveness replaces the need for a Trigger.

- [ ] **Step 1: Write the Stage enum**

```rust
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
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
```

- [ ] **Step 2: Write role enum**

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
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
```

- [ ] **Step 3: Write transition and permission maps**

```rust
use std::collections::{HashMap, HashSet};

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
    use Role::*; use Stage::*;
    HashMap::from([
        (Worker, HashSet::from([Init, WorkerModify, Verified])),
        (Orchestrator, HashSet::from([Init, OrchestratorGate, OrchestratorArbiter, Verified, WorkerModify])),
        (Reviewer, HashSet::from([ReviewerCheck])),
    ])
}

pub fn terminal_stages() -> HashSet<Stage> {
    use Stage::*;
    HashSet::from([Rejected, LockReleased])
}

/// Columns whitelisted for transition_stage kwargs
pub fn allowed_columns() -> HashSet<&'static str> {
    HashSet::from([
        "reason_json", "plan_json",
        "plan_r2", "plan_r3", "plan_r4",
        "commits_json",
        "approval_status", "rejection_reason",
        "feedback_r1", "feedback_r2", "feedback_r3", "feedback_r4",
        "completion_r1", "completion_r2", "completion_r3", "completion_r4",
        "human_intervention",
        "review_round",
    ])
}
```

- [ ] **Step 4: Write Rust unit tests for transitions**

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_terminal_stages_have_no_exits() {
        let v = valid_transitions();
        for ts in terminal_stages() {
            assert!(!v.contains_key(&ts),
                "{:?} should not have outgoing transitions", ts);
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
                assert!(v.contains_key(s),
                    "{:?} not in VALID_TRANSITIONS", s);
            }
        }
    }

    #[test]
    fn test_stage_roundtrip() {
        for (s, name) in [
            (Stage::Init, "init"),
            (Stage::LockReleased, "lock_released"),
        ] {
            assert_eq!(Stage::from_str(name), Some(s));
            assert_eq!(s.as_str(), name);
        }
    }
}
```

- [ ] **Step 5: Run tests**

```bash
cargo test
```

Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add src/pipeline.rs && git commit -m "feat: Rust Stage enum, Role enum, transition/permission maps"
```

---

### Task 3: transition_stage in Rust (pipeline.rs)

**Files:**
- Modify: `src/pipeline.rs`

Port the CAS + permission + whitelist + audit logic. Note: Rust's type system eliminates the need for a SQL trigger — `Stage::from_str()` already validates inputs, and the match map already enforces valid transitions.

- [ ] **Step 1: Write transition_stage function**

```rust
use rusqlite::{Connection, params};

pub fn transition_stage(
    conn: &Connection,
    request_id: &str,
    new_stage: Stage,
    role: Role,
    revision: i64,
    kwargs: &HashMap<String, String>,
) -> Result<(i64, Stage), TransitionError> {
    // 1. Read current state
    let mut stmt = conn.prepare(
        "SELECT stage, revision FROM pipeline_state WHERE request_id=?"
    )?;
    let (current_stage_str, current_revision): (String, i64) = stmt.query_row(
        params![request_id], |row| Ok((row.get(0)?, row.get(1)?))
    ).map_err(|_| TransitionError::PipelineNotFound(request_id.to_string()))?;

    let current_stage = Stage::from_str(&current_stage_str)
        .ok_or(TransitionError::InvalidStage(current_stage_str))?;

    // 2. Transition path validation
    let v = valid_transitions();
    let allowed = v.get(&current_stage).ok_or(TransitionError::TerminalStage(current_stage))?;
    if !allowed.contains(&new_stage) {
        return Err(TransitionError::InvalidTransition(current_stage, new_stage));
    }

    // 3. Revision check
    if current_revision != revision {
        return Err(TransitionError::RevisionMismatch { expected: revision, actual: current_revision });
    }

    // 4. Permission check
    let rp = role_permissions();
    let role_stages = rp.get(&role).ok_or(TransitionError::UnknownRole(role))?;
    if !role_stages.contains(&current_stage) {
        return Err(TransitionError::PermissionDenied(role, current_stage));
    }

    // 5. Whitelist kwargs
    let ac = allowed_columns();
    let filtered: HashMap<_, _> = kwargs.iter()
        .filter(|(k, _)| ac.contains(k.as_str()))
        .collect();

    // 6. Build UPDATE
    let new_rev = current_revision + 1;
    let mut set_parts = vec![
        "stage = ?".to_string(),
        "revision = revision + 1".to_string(),
        "updated_at = datetime('now','localtime')".to_string(),
    ];
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = vec![
        Box::new(new_stage.as_str().to_string())
    ];

    for (key, value) in &filtered {
        set_parts.push(format!("{} = ?", key));
        params.push(Box::new(value.clone()));
    }
    params.push(Box::new(request_id.to_string()));
    params.push(Box::new(current_stage.as_str().to_string()));
    params.push(Box::new(current_revision));

    let sql = format!(
        "UPDATE pipeline_state SET {} WHERE request_id=? AND stage=? AND revision=?",
        set_parts.join(", ")
    );

    // 7. Transaction: UPDATE + INSERT audit_log
    conn.execute("BEGIN", [])?;
    let affected = conn.execute(&sql, rusqlite::params_from_iter(params.iter().map(|p| p.as_ref())))?;
    if affected == 0 {
        conn.execute("ROLLBACK", [])?;
        return Err(TransitionError::CasFailed);
    }

    let payload = if filtered.is_empty() {
        None
    } else {
        Some(serde_json::to_string(&filtered.iter().map(|(k, v)| (k.to_string(), v.clone())).collect::<HashMap<_, _>>()).unwrap_or_default())
    };

    conn.execute(
        "INSERT INTO audit_log (request_id, role, stage_from, stage_to, revision_before, revision_after, payload_json)
         VALUES (?, ?, ?, ?, ?, ?, ?)",
        params![request_id, role.as_str(), current_stage.as_str(), new_stage.as_str(), current_revision, new_rev, payload],
    )?;

    conn.execute("COMMIT", [])?;
    Ok((new_rev, new_stage))
}

#[derive(Debug)]
pub enum TransitionError {
    PipelineNotFound(String),
    InvalidStage(String),
    TerminalStage(Stage),
    InvalidTransition(Stage, Stage),
    RevisionMismatch { expected: i64, actual: i64 },
    PermissionDenied(Role, Stage),
    UnknownRole(Role),
    CasFailed,
    DbError(rusqlite::Error),
}

impl From<rusqlite::Error> for TransitionError {
    fn from(e: rusqlite::Error) -> Self { TransitionError::DbError(e) }
}
```

- [ ] **Step 2: Write test — happy path**

```rust
#[test]
fn test_transition_stage_happy_path() {
    let conn = setup_test_db();
    let req_id = init_test_pipeline(&conn);
    let kwargs = HashMap::new();
    let result = transition_stage(&conn, &req_id, Stage::OrchestratorGate, Role::Orchestrator, 0, &kwargs);
    assert!(result.is_ok());
    let (rev, stage) = result.unwrap();
    assert_eq!(rev, 1);
    assert_eq!(stage, Stage::OrchestratorGate);
}
```

- [ ] **Step 3: Write test — permission denied**

```rust
#[test]
fn test_transition_permission_denied() {
    let conn = setup_test_db();
    let req_id = init_test_pipeline(&conn);
    let kwargs = HashMap::new();
    let result = transition_stage(&conn, &req_id, Stage::OrchestratorGate, Role::Reviewer, 0, &kwargs);
    assert!(matches!(result, Err(TransitionError::PermissionDenied(..))));
}
```

- [ ] **Step 4: Write test — revision mismatch**

```rust
#[test]
fn test_revision_mismatch() {
    let conn = setup_test_db();
    let req_id = init_test_pipeline(&conn);
    let kwargs = HashMap::new();
    let result = transition_stage(&conn, &req_id, Stage::OrchestratorGate, Role::Orchestrator, 99, &kwargs);
    assert!(matches!(result, Err(TransitionError::RevisionMismatch{..})));
}
```

- [ ] **Step 5: Write test — CAS concurrency**

```rust
#[test]
fn test_cas_concurrency() {
    let conn = setup_test_db();
    let req_id = init_test_pipeline(&conn);
    let kwargs = HashMap::new();

    let r1 = transition_stage(&conn, &req_id, Stage::OrchestratorGate, Role::Orchestrator, 0, &kwargs);
    let r2 = transition_stage(&conn, &req_id, Stage::OrchestratorGate, Role::Orchestrator, 0, &kwargs);

    let successes = [r1.is_ok(), r2.is_ok()].iter().filter(|x| **x).count();
    assert_eq!(successes, 1);
}
```

- [ ] **Step 6: Write test — full path with correction loop**

```rust
#[test]
fn test_full_path_with_correction() {
    let conn = setup_test_db();
    let req_id = init_test_pipeline(&conn);

    let mut rev = 0;
    let flow = vec![
        (Stage::OrchestratorGate, Role::Orchestrator, hashmap!{}),
        (Stage::WorkerModify, Role::Orchestrator, hashmap!{"approval_status".into() => "approved".into()}),
        (Stage::ReviewerCheck, Role::Worker, hashmap!{"commits_json".into() => "[\"abc\"]".into()}),
        (Stage::OrchestratorArbiter, Role::Reviewer, hashmap!{"completion_r1".into() => "{\"verdict\":\"有偏差\"}".into()}),
        (Stage::WorkerModify, Role::Orchestrator, hashmap!{"feedback_r1".into() => "{}".into(), "review_round".into() => "2".into()}),
        (Stage::ReviewerCheck, Role::Worker, hashmap!{"plan_r2".into() => "{\"files\":[\"a.py\"]}".into()}),
        (Stage::OrchestratorArbiter, Role::Reviewer, hashmap!{"completion_r2".into() => "{\"verdict\":\"符合计划\"}".into()}),
        (Stage::Verified, Role::Orchestrator, hashmap!{}),
        (Stage::LockReleased, Role::Worker, hashmap!{}),
    ];
    for (stage, role, kwargs) in flow {
        let (new_rev, _) = transition_stage(&conn, &req_id, stage, role, rev, &kwargs).unwrap();
        rev = new_rev;
    }
    assert_eq!(rev, 9);
}
```

- [ ] **Step 7: Run tests**

```bash
cargo test -- pipeline
```

Expected: 10 PASS

- [ ] **Step 8: Commit**

```bash
git add src/pipeline.rs && git commit -m "feat: transition_stage in Rust with CAS, audit, and error types"
```

---

### Task 4: DB Layer — init_db, migrate, tables (db.rs)

**Files:**
- Create: `src/db.rs`

Port the SQLite schema creation and migration. `rusqlite` with bundled SQLite for portability.

- [ ] **Step 1: Write init_db**

```rust
use rusqlite::Connection;

pub fn init_db(conn: &Connection) {
    conn.execute_batch("
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
    ").unwrap();
    // Trigger: defense-in-depth for direct SQL access
    conn.execute_batch("
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
    ").unwrap();
}
```

- [ ] **Step 2: Write migrate**

```rust
pub fn migrate(conn: &Connection) {
    for table_sql in [
        "CREATE TABLE IF NOT EXISTS project (id TEXT PRIMARY KEY, content TEXT, file_index TEXT, change_time TEXT, file_use TEXT, agent_status TEXT)",
        "CREATE TABLE IF NOT EXISTS register (agent_id TEXT PRIMARY KEY, role TEXT NOT NULL, schema_json TEXT)",
        "CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, role TEXT NOT NULL, stage_from TEXT NOT NULL, stage_to TEXT NOT NULL, revision_before INTEGER, revision_after INTEGER, payload_json TEXT, created_at TEXT DEFAULT (datetime('now','localtime')))",
    ] {
        conn.execute(table_sql, []).ok();
    }
    let new_cols = [
        "reason_json", "plan_json", "plan_r2", "plan_r3", "plan_r4",
        "commits_json", "approval_status", "rejection_reason",
        "feedback_r1", "feedback_r2", "feedback_r3", "feedback_r4",
        "completion_r1", "completion_r2", "completion_r3", "completion_r4",
        "human_intervention", "review_round",
    ];
    for col in new_cols {
        conn.execute(
            &format!("ALTER TABLE pipeline_state ADD COLUMN {} TEXT", col), []
        ).ok();
    }
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log(request_id)", []).ok();
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at)", []).ok();
}
```

- [ ] **Step 3: Write test — tables exist**

```rust
#[test]
fn test_tables_exist() {
    let conn = setup_test_db();
    let tables: Vec<String> = conn.prepare(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).unwrap().query_map([], |row| row.get(0)).unwrap().filter_map(|r| r.ok()).collect();

    for t in ["pipeline_state", "project", "register", "audit_log"] {
        assert!(tables.contains(&t.to_string()), "Table {} missing", t);
    }
}
```

- [ ] **Step 4: Run tests**

```bash
cargo test -- db
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/db.rs && git commit -m "feat: init_db, migrate, and project/register/audit_log tables in Rust"
```

---

### Task 5: Orchestrator — init_pipeline, get_pipeline, CRUD (orchestrator.rs)

**Files:**
- Create: `src/orchestrator.rs`

- [ ] **Step 1: Write Orchestrator struct and init_pipeline**

```rust
use rusqlite::{Connection, params};
use serde_json::Value as JsonValue;
use uuid::Uuid;
use chrono::Utc;

pub struct Orchestrator {
    pub db_path: String,
}

impl Orchestrator {
    pub fn new(db_path: &str) -> Self {
        Self { db_path: db_path.to_string() }
    }

    pub fn connect(&self) -> Connection {
        let conn = Connection::open(&self.db_path).unwrap();
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;").unwrap();
        conn
    }

    pub fn init_pipeline(&self, agent: &str, reason: &JsonValue, plan: &JsonValue) -> Result<String, String> {
        // Check active pipelines
        if !self.get_pending_requests(agent).is_empty() {
            return Err(format!("Agent '{}' already has active pipeline", agent));
        }
        let req_id = format!("{}-{}-{}",
            agent,
            Utc::now().format("%Y%m%d-%H%M%S"),
            &Uuid::new_v4().to_string()[..6]
        );
        let conn = self.connect();
        conn.execute(
            "INSERT INTO pipeline_state (request_id, agent, stage, reason_json, plan_json, review_round)
             VALUES (?, ?, 'init', ?, ?, 1)",
            params![req_id, agent,
                serde_json::to_string(reason).unwrap(),
                serde_json::to_string(plan).unwrap(),
            ],
        ).map_err(|e| e.to_string())?;
        Ok(req_id)
    }

    pub fn get_pipeline(&self, request_id: &str) -> Option<HashMap<String, JsonValue>> {
        let conn = self.connect();
        let mut stmt = conn.prepare("SELECT * FROM pipeline_state WHERE request_id=?").ok()?;
        let row = stmt.query_row(params![request_id], |row| {
            let mut map = HashMap::new();
            for (i, col) in ["request_id","agent","stage","revision","reason_json","plan_json",
                "plan_r2","plan_r3","plan_r4","commits_json","approval_status","rejection_reason",
                "feedback_r1","feedback_r2","feedback_r3","feedback_r4",
                "completion_r1","completion_r2","completion_r3","completion_r4",
                "human_intervention","review_round"].iter().enumerate()
            {
                let val: Option<String> = row.get(i).ok();
                if let Some(v) = val {
                    // Try JSON deserialize for JSON columns
                    if col.contains("json") || col.contains("intervention") {
                        if let Ok(j) = serde_json::from_str(&v) {
                            map.insert(col.to_string(), j);
                            continue;
                        }
                    }
                    // review_round as integer
                    if *col == "review_round" {
                        if let Ok(n) = v.parse::<i64>() {
                            map.insert(col.to_string(), JsonValue::Number(n.into()));
                            continue;
                        }
                    }
                    map.insert(col.to_string(), JsonValue::String(v));
                }
            }
            Ok(map)
        }).ok()?;
        Some(row)
    }
}
```

Note: Partial — full struct with all methods would extend to ~200 lines. Remaining methods: `get_pending_requests`, `get_requests_by_stage`, `recover_pipeline`, `try_register`, `check_and_heartbeat`, `resolve_orphan_locks`, `create_project`, `get_project`, `get_register`. Same patterns as Python originals, adapted to Rust types.

- [ ] **Step 2: Run tests**

```bash
cargo test -- orchestrator
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add src/orchestrator.rs && git commit -m "feat: Orchestrator struct with init_pipeline, get_pipeline, CRUD in Rust"
```

---

### Task 6: Lint — boundary check + plan validation (lint.rs)

**Files:**
- Create: `src/lint.rs`

Port `run_lint`, `_check_boundaries`, `_match_pattern`, `validate_plan`, `lint_plan`. Drop AST hints (no Rust Python AST parser). Keep `lint_changed_files` (reads git diff).

- [ ] **Step 1: Write boundary check**

```rust
use std::collections::HashMap;
use std::process::Command;

pub fn run_lint(files: &[String], boundaries: &HashMap<String, JsonValue>, agent: &str) -> LintResult {
    if boundaries.is_empty() {
        return LintResult { blocked: true, reason: "boundaries not configured".into(), hints: None };
    }
    let Some(agent_rules) = boundaries.get(agent) else {
        return LintResult { blocked: true, reason: format!("agent '{}' not in boundaries", agent), hints: None };
    };
    let forbidden: Vec<&str> = agent_rules["forbidden"].as_array()
        .map(|a| a.iter().filter_map(|v| v.as_str()).collect()).unwrap_or_default();
    let can_touch: Vec<&str> = agent_rules["can_touch"].as_array()
        .map(|a| a.iter().filter_map(|v| v.as_str()).collect()).unwrap_or_default();

    for f in files {
        let f = f.replace('\\', "/");
        for pat in &forbidden {
            if match_pattern(&f, pat) {
                return LintResult { blocked: true, reason: format!("{} matches forbidden {}", f, pat), hints: None };
            }
        }
        if !can_touch.is_empty() && !can_touch.iter().any(|p| match_pattern(&f, p)) {
            return LintResult { blocked: true, reason: format!("{} not in can_touch", f), hints: None };
        }
    }
    LintResult { blocked: false, reason: String::new(), hints: None }
}

pub fn match_pattern(path: &str, pattern: &str) -> bool {
    let pattern = pattern.replace('\\', "/");
    if pattern.ends_with('/') {
        return path.starts_with(&pattern);
    }
    // Simple glob: *.py → ends_with, dir/** → starts_with
    if pattern.starts_with("*.") {
        return path.ends_with(&pattern[1..]);
    }
    if pattern.ends_with("/**") {
        return path.starts_with(&pattern[..pattern.len()-3]);
    }
    path == pattern || path.starts_with(&pattern)
}

#[derive(Debug)]
pub struct LintResult {
    pub blocked: bool,
    pub reason: String,
    pub hints: Option<HashMap<String, JsonValue>>,
}

pub fn validate_plan(plan_json: &str) -> Result<JsonValue, String> {
    let p: JsonValue = serde_json::from_str(plan_json).map_err(|e| format!("not valid JSON: {}", e))?;
    let files = p.get("files").and_then(|f| f.as_array()).ok_or("files missing or not array")?;
    if files.is_empty() { return Err("files is empty".into()); }
    for f in files {
        let s = f.as_str().ok_or("files contains non-string entry")?;
        if s.starts_with('/') || s.contains("..") {
            return Err(format!("path traversal: {}", s));
        }
    }
    Ok(p)
}

pub fn lint_plan(plan_json: &str, boundaries: &HashMap<String, JsonValue>, agent: &str) -> LintResult {
    match validate_plan(plan_json) {
        Err(reason) => LintResult { blocked: true, reason, hints: None },
        Ok(plan) => {
            let files: Vec<String> = plan["files"].as_array()
                .map(|a| a.iter().filter_map(|v| v.as_str().map(String::from)).collect())
                .unwrap_or_default();
            run_lint(&files, boundaries, agent)
        }
    }
}

pub fn lint_changed_files(boundaries: &HashMap<String, JsonValue>, agent: &str) -> LintResult {
    let output = Command::new("git").args(["diff", "--name-only"]).output();
    match output {
        Ok(o) if o.status.success() => {
            let raw = String::from_utf8_lossy(&o.stdout);
            if raw.trim().is_empty() {
                return LintResult { blocked: false, reason: String::new(), hints: None };
            }
            let files: Vec<String> = raw.trim().lines().map(String::from).collect();
            run_lint(&files, boundaries, agent)
        }
        _ => LintResult { blocked: true, reason: "git diff failed".into(), hints: None },
    }
}
```

- [ ] **Step 2: Write tests**

```rust
#[test]
fn test_validate_plan_valid() {
    let result = validate_plan(r#"{"files":["a.py"]}"#);
    assert!(result.is_ok());
}

#[test]
fn test_validate_plan_empty_files() {
    let result = validate_plan(r#"{"files":[]}"#);
    assert!(result.is_err());
}

#[test]
fn test_validate_plan_path_traversal() {
    let result = validate_plan(r#"{"files":["../etc/passwd"]}"#);
    assert!(result.is_err());
}

#[test]
fn test_lint_plan_boundary_blocked() {
    let boundaries = serde_json::from_str(r#"{"py-agent":{"can_touch":["lib/"],"forbidden":["app/"]}}"#).unwrap();
    let result = lint_plan(r#"{"files":["app/main.py"]}"#, &boundaries, "py-agent");
    assert!(result.blocked);
}

#[test]
fn test_lint_plan_passes() {
    let boundaries = serde_json::from_str(r#"{"py-agent":{"can_touch":["lib/"],"forbidden":["app/"]}}"#).unwrap();
    let result = lint_plan(r#"{"files":["lib/utils.py"]}"#, &boundaries, "py-agent");
    assert!(!result.blocked);
}
```

- [ ] **Step 3: Run tests**

```bash
cargo test -- lint
```

Expected: 5 PASS

- [ ] **Step 4: Commit**

```bash
git add src/lint.rs && git commit -m "feat: boundary check, plan validation, lint_plan/lint_changed_files in Rust"
```

---

### Task 7: Status Dashboard CLI (bin/status.rs)

**Files:**
- Create: `src/bin/status.rs`

- [ ] **Step 1: Write CLI with clap**

```rust
use clap::Parser;
use rusqlite::Connection;
use std::time::Duration;

#[derive(Parser)]
struct Args {
    #[arg(default_value = ".claude/orchestrator/orchestrator.db")]
    db_path: String,

    #[arg(long)]
    once: bool,

    #[arg(default_value = "3")]
    interval: u64,
}

fn main() {
    let args = Args::from_args();
    if args.once {
        render(&args.db_path);
    } else {
        loop {
            render(&args.db_path);
            std::thread::sleep(Duration::from_secs(args.interval));
        }
    }
}

fn render(db_path: &str) {
    let conn = Connection::open_with_flags(db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    let mut stmt = conn.prepare(
        "SELECT request_id, agent, stage, revision, reason_json, approval_status, rejection_reason, updated_at, human_intervention FROM pipeline_state ORDER BY updated_at DESC"
    ).unwrap();
    let rows: Vec<_> = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?, row.get::<_, String>(1)?,
            row.get::<_, String>(2)?, row.get::<_, i64>(3)?,
            row.get::<_, Option<String>>(4)?, row.get::<_, Option<String>>(5)?,
            row.get::<_, Option<String>>(6)?, row.get::<_, Option<String>>(7)?,
            row.get::<_, Option<String>>(8)?,
        ))
    }).unwrap().filter_map(|r| r.ok()).collect();

    print!("\x1B[2J\x1B[H"); // clear screen
    println!(" Pipeline Status Dashboard");
    println!("{:=<80}", "");
    for (id, agent, stage, rev, reason, _, reject, updated, hi) in &rows {
        let hi_flag = if hi.as_ref().map_or(false, |h| h.contains("needs_human")) { " [NEEDS HUMAN]" } else { "" };
        println!(" {:6} | {:22} | rev {:>2} | {:48} | {}{}",
            &agent[..agent.len().min(6)], stage, rev,
            reason.as_deref().unwrap_or("").chars().take(48).collect::<String>(),
            updated.as_deref().unwrap_or(""), hi_flag);
        if let Some(r) = reject {
            println!("        | {:22} |       | REJECTED: {}", "", &r[..r.len().min(40)]);
        }
    }
    println!("{:-<80}", "");
}
```

- [ ] **Step 2: Build and test**

```bash
cargo build --bin status
./target/debug/status --once
```

Expected: Dashboard output with " Pipeline Status Dashboard"

- [ ] **Step 3: Commit**

```bash
git add src/bin/status.rs && git commit -m "feat: status dashboard CLI with clap in Rust"
```

---

### Task 8: Full Integration Test Suite

**Files:**
- Create: `tests/integration_test.rs`

Port the full-path and correction-loop integration tests from `test_orchestrator.py`.

- [ ] **Step 1: Write integration test**

```rust
#[cfg(test)]
mod integration {
    use orchestrator::*;
    use std::collections::HashMap;

    fn setup() -> (Connection, String) {
        let conn = Connection::open_in_memory().unwrap();
        db::init_db(&conn);
        let orch = Orchestrator { db_path: ":memory:".into() };
        let req_id = orch.init_pipeline("py-agent",
            &serde_json::json!({"reason":"test","agent_id":"py-agent"}),
            &serde_json::json!({"files":["a.py"]})).unwrap();
        (conn, req_id)
    }

    #[test]
    fn test_full_path() {
        let (conn, req_id) = setup();
        let mut rev = 0;
        let flow = vec![
            (Stage::OrchestratorGate, Role::Orchestrator, hashmap!{}),
            (Stage::WorkerModify, Role::Orchestrator, hashmap!{"approval_status".into() => "approved".into()}),
            (Stage::ReviewerCheck, Role::Worker, hashmap!{"commits_json".into() => "[\"abc\"]".into()}),
            (Stage::OrchestratorArbiter, Role::Reviewer, hashmap!{"completion_r1".into() => "{\"verdict\":\"符合计划\"}".into()}),
            (Stage::Verified, Role::Orchestrator, hashmap!{}),
            (Stage::LockReleased, Role::Worker, hashmap!{}),
        ];
        for (stage, role, kwargs) in flow {
            let (r, _) = pipeline::transition_stage(&conn, &req_id, stage, role, rev, &kwargs).unwrap();
            rev = r;
        }
        assert_eq!(rev, 6);
    }

    #[test]
    fn test_reject_flow() {
        let (conn, req_id) = setup();
        let (rev, _) = pipeline::transition_stage(&conn, &req_id, Stage::OrchestratorGate, Role::Orchestrator, 0, &hashmap!{}).unwrap();
        let (_, _) = pipeline::transition_stage(&conn, &req_id, Stage::Rejected, Role::Orchestrator, rev,
            &hashmap!{"approval_status".into() => "rejected".into(), "rejection_reason".into() => "out of bounds".into()}).unwrap();
    }
}
```

- [ ] **Step 2: Run all tests**

```bash
cargo test
```

Expected: All tests PASS (~30+ tests)

- [ ] **Step 3: Commit**

```bash
git add tests/ && git commit -m "test: full-path and correction-loop integration tests in Rust"
```

---

### Task 9: Remove Python Sources, Update Docs

**Files:**
- Remove: `pipeline.py`, `orchestrator.py`, `lint.py`, `status.py`
- Remove: `test_pipeline.py`, `test_orchestrator.py`, `test_lint.py`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Remove Python sources**

```bash
rm pipeline.py orchestrator.py lint.py status.py
rm test_pipeline.py test_orchestrator.py test_lint.py
```

- [ ] **Step 2: Update CLAUDE.md**

Replace the Python launch instructions with Rust equivalents:

```
启动时执行以下代码确定角色，直接运行不展开讨论：

cargo run --bin status -- --once
```

- [ ] **Step 3: Verify build**

```bash
cargo build --release
cargo test
```

Expected: 0 warnings, all tests pass

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "migrate: remove Python sources, update CLAUDE.md for Rust"
```

---

## Task Order & Dependencies

```
Task 1 (scaffolding)
  → Task 2 (Stage + Role enums)
    → Task 3 (transition_stage)
      → Task 4 (db.rs)
        → Task 5 (orchestrator.rs)
          → Task 6 (lint.rs)
          → Task 7 (status CLI)
            → Task 8 (integration tests)
              → Task 9 (remove Python)
```

Tasks 6 and 7 are independent of each other. Both depend on Task 5 being complete.
