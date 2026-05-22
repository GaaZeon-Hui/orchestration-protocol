---
name: orchestration-protocol
description: Entry point. Determines role via heartbeat and redirects to role-specific file.
---

# Orchestration Protocol — Entry

| 角色 | 职责 | 角色文件 |
|------|------|---------|
| **Orchestrator** | 哨兵检查 → 三项分析 → transition_stage 审批 → 验证释锁 | `orchestrator-role.md` |
| **Worker Agent** | init_pipeline → 等审批 → transition_stage 推进 → 自审 → completion | `worker-role.md` |

## 架构

单表 `pipeline_state` + `context` + `audit_log` 三表。独立模块 `pipeline.py` 提供 `transition_stage()`（CAS + 权限校验 + 审计）。旧 5 表（`lock`, `requests`, `approvals`, `completions`, `processed`）已移除。

## 角色注册

首次加载时执行。之后每次启动直接加载对应的角色 md。

1. 导入库：`from orchestrator import Orchestrator; orc = Orchestrator()`
2. 初始化：`orc.init_db()` → `orc.migrate()`
3. 检查存活：`orc.check_orchestrator_alive()` → `(is_alive, orch_id)`
4. 存活 → **worker**。否则调用 `orc.try_register()` 抢注。
5. 抢注成功 → **orchestrator**，立即读 `orchestrator-role.md`
   抢注失败/已存活 → **worker**，立即读 `worker-role.md`

两个 agent 同时抢注时，SQLite 行级锁保证仅一个成功。

## 状态转移（`pipeline.VALID_TRANSITIONS`）

```
request_submitted → conflict_analysis_done → boundary_analysis_done
    → logic_analysis_done → approved → modifying → self_review_done
    → completion_submitted → completed → lock_released
                         ↘ rejected
```

`transition_stage()` 通过 CAS（`WHERE stage=? AND revision=?`）保证并发安全。SQL trigger `tr_stage_transition` 兜底校验转移合法性。

## 权限矩阵（`pipeline.ROLE_PERMISSIONS`）

| Role | 可发起的 from_stage | → to_stage |
|------|---------------------|------------|
| **orchestrator** | `request_submitted` | `conflict_analysis_done` |
| | `conflict_analysis_done` | `boundary_analysis_done` |
| | `boundary_analysis_done` | `logic_analysis_done` |
| | `logic_analysis_done` | `approved` / `rejected` |
| | `completion_submitted` | `completed` |
| **worker** | `approved` | `modifying` |
| | `modifying` | `self_review_done` |
| | `self_review_done` | `completion_submitted` |
| | `completed` | `lock_released` |

## 数据库 Schema（参考，由 `orc.init_db()` 自动创建）

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS pipeline_state (
    request_id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'request_submitted' CHECK(stage IN (
        'request_submitted', 'conflict_analysis_done',
        'boundary_analysis_done', 'logic_analysis_done',
        'approved', 'rejected',
        'modifying', 'self_review_done',
        'completion_submitted', 'completed', 'lock_released'
    )),
    revision INTEGER NOT NULL DEFAULT 0,

    -- 请求数据
    reason TEXT, scope_json TEXT, plan_json TEXT,
    self_review_json TEXT, constraints_json TEXT,

    -- 审批数据
    approval_status TEXT, granted_scope_json TEXT,
    rejection_reason TEXT, reviewed_by TEXT,

    -- Completion 数据
    completed_at TEXT, commits_json TEXT,
    sync_notes TEXT, context_updates_json TEXT,

    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
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

CREATE TABLE IF NOT EXISTS context (
    id INTEGER PRIMARY KEY CHECK(id=1),
    last_commit TEXT, agent_history_json TEXT DEFAULT '[]',
    warnings_json TEXT DEFAULT '[]', boundaries_json TEXT,
    pipeline TEXT, api_contract TEXT, meta_fields TEXT,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
INSERT OR IGNORE INTO context (id) VALUES (1);
```

## 模块边界

由用户首次启动时配置，存储在 `context.boundaries_json`。所有角色通过 `orc.get_boundaries()` 读取。

## 核心 API

| 函数 | 模块 | 说明 |
|------|------|------|
| `transition_stage(req_id, new_stage, role, revision, db_path, **kwargs)` | `pipeline.py` | 唯一 stage 推进入口，含权限+审计 |
| `init_pipeline(agent, reason, scope, plan, self_review, constraints, tz)` | `orchestrator.py` | 创建 pipeline |
| `get_pipeline(request_id)` | `orchestrator.py` | 查询单条 pipeline |
| `get_pending_requests(agent)` | `orchestrator.py` | 查未完成 pipeline |
| `get_requests_by_stage(stage)` | `orchestrator.py` | 按 stage 查询 |
| `recover_pipeline(agent)` | `orchestrator.py` | 崩溃恢复 |
| `get_boundaries()` / `set_boundaries(b)` | `orchestrator.py` | 模块边界 |
