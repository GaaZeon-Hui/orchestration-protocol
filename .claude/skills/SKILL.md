---
name: orchestration-protocol
description: Entry point. Determines role via heartbeat and redirects to role-specific file.
---

# Orchestration Protocol — Entry

| 角色 | 职责 | 角色文件 |
|------|------|---------|
| **Orchestrator** | 哨兵检查 → 三项分析 → 签发锁 → 验证释锁 | `orchestrator-role.md` |
| **Worker Agent** | 提交请求 → 等锁 → 修改 → 自审 → completion | `worker-role.md` |

## 角色注册

首次加载时执行。之后每次启动直接加载对应的角色 md。

1. 导入库：`from orchestrator import Orchestrator; orc = Orchestrator()`
2. 初始化：`orc.init_db()` → `orc.migrate()`
3. 检查存活：`orc.check_orchestrator_alive()` → `(is_alive, orch_id)`
4. 存活 → **worker**。否则调用 `orc.try_register()` 抢注。
5. 抢注成功 → **orchestrator**，立即读 `orchestrator-role.md`
   抢注失败/已存活 → **worker**，立即读 `worker-role.md`

两个 agent 同时抢注时，SQLite 行级锁保证仅一个成功。

## 数据库 Schema（参考，由 `orc.init_db()` 自动创建）

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS lock (
    id INTEGER PRIMARY KEY CHECK(id=1),
    state TEXT NOT NULL DEFAULT 'idle',
    holder TEXT, request_id TEXT, scope_json TEXT,
    acquired_at TEXT, expires_at TEXT,
    orchestrator_id TEXT, orchestrator_heartbeat TEXT, orchestrator_started_at TEXT
);
INSERT OR IGNORE INTO lock (id) VALUES (1);

CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY, agent TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT, scope_json TEXT, plan_json TEXT,
    self_review_json TEXT, constraints_json TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_updated ON requests(updated_at);
CREATE INDEX IF NOT EXISTS idx_requests_agent ON requests(agent);

CREATE TABLE IF NOT EXISTS approvals (
    request_id TEXT PRIMARY KEY, status TEXT NOT NULL,
    granted_scope_json TEXT, rejection_reason TEXT, reviewed_by TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (request_id) REFERENCES requests(id)
);

CREATE TABLE IF NOT EXISTS completions (
    request_id TEXT PRIMARY KEY, agent TEXT NOT NULL, completed_at TEXT,
    self_review_json TEXT, commits_json TEXT,
    sync_notes TEXT, context_updates_json TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (request_id) REFERENCES requests(id)
);

CREATE TABLE IF NOT EXISTS context (
    id INTEGER PRIMARY KEY CHECK(id=1),
    last_commit TEXT, agent_history_json TEXT DEFAULT '[]',
    warnings_json TEXT DEFAULT '[]', boundaries_json TEXT,
    pipeline TEXT, api_contract TEXT, meta_fields TEXT,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
INSERT OR IGNORE INTO context (id) VALUES (1);

CREATE TABLE IF NOT EXISTS processed (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL,
    processed_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_processed_type ON processed(type);
```

## 模块边界

由用户首次启动时配置，存储在 `context.boundaries_json`。所有角色通过 `orc.get_boundaries()` 读取。
