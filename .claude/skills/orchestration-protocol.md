---
name: orchestration-protocol
description: Entry point for the orchestration system. Determines role (orchestrator or worker) via heartbeat check and redirects to the role-specific file.
---

# Orchestration Protocol — Entry

| 角色 | 职责 | 角色文件 |
|------|------|---------|
| **Orchestrator** | 哨兵检查 → 三项分析 → 阻塞汇报 → 签发锁 → 验证释锁 | `orchestrator-role.md` |
| **Worker Agent** | 提交请求 → 等锁 → 修改 → 自审 → completion | `worker-role.md` |

---

## 角色注册

**本文件仅首次加载或编排者更替时执行。注册后每次启动直接加载角色 md。**

```python
import sqlite3, uuid, os

DB = '.claude/orchestrator/orchestrator.db'
os.makedirs(os.path.dirname(DB), exist_ok=True)
conn = sqlite3.connect(DB)
cur = conn.cursor()

# 迁移旧表（如果 lock 缺少角色字段）
for col in ["orchestrator_id","orchestrator_heartbeat","orchestrator_started_at"]:
    try: cur.execute(f"ALTER TABLE lock ADD COLUMN {col} TEXT")
    except: pass

# 检查心跳
cur.execute("SELECT orchestrator_heartbeat FROM lock WHERE id=1")
row = cur.fetchone()
heartbeat = row[0] if row else None
seat_vacant = heartbeat is None
if not seat_vacant:
    cur.execute("SELECT datetime(?) < datetime('now','localtime','-90 seconds')", (heartbeat,))
    seat_vacant = bool(cur.fetchone()[0])

# 抢注
if seat_vacant:
    my_id = str(uuid.uuid4())[:8]
    cur.execute("""
        UPDATE lock SET
            orchestrator_id=?, orchestrator_heartbeat=datetime('now','localtime'),
            orchestrator_started_at=datetime('now','localtime')
        WHERE id=1 AND (orchestrator_heartbeat IS NULL
                        OR datetime(orchestrator_heartbeat) < datetime('now','localtime','-90 seconds'))
    """, (my_id,))
    conn.commit()
    if cur.rowcount == 1:
        role = 'orchestrator'
        print(f"ROLE: orchestrator (id={my_id})")
    else:
        role = 'worker'
        print("ROLE: worker (lost race)")
else:
    role = 'worker'
    print("ROLE: worker (orchestrator alive)")

conn.close()
# → orchestrator → 读 orchestrator-role.md
# → worker       → 读 worker-role.md
```

两个 agent 同时抢注时，SQLite 行级锁保证仅一个成功（`rowcount=1`），另一个自动降级（`rowcount=0`）。

---

## 数据库 Schema

所有状态在 `.claude/orchestrator/orchestrator.db`，WAL 模式。

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS lock (
    id INTEGER PRIMARY KEY CHECK(id=1),
    state TEXT NOT NULL DEFAULT 'idle',          -- 'idle' | 'locked'
    holder TEXT,
    request_id TEXT,
    scope_json TEXT,                              -- {"files":[...],"forbidden":[...]}
    acquired_at TEXT,
    expires_at TEXT,
    orchestrator_id TEXT,                         -- 编排者 session ID
    orchestrator_heartbeat TEXT,                  -- 心跳时间
    orchestrator_started_at TEXT
);
INSERT OR IGNORE INTO lock (id) VALUES (1);

CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,                          -- {agent}-{YYYYMMDD}-{HHMMSS}
    agent TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',       -- pending|approved|approved-with-warning|rejected
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
    pipeline TEXT,
    api_contract TEXT,
    meta_fields TEXT,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
INSERT OR IGNORE INTO context (id) VALUES (1);

CREATE TABLE IF NOT EXISTS processed (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,                           -- 'request' | 'completion'
    status TEXT NOT NULL,
    processed_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_processed_type ON processed(type);
```

---

## 模块边界

| Agent | Can touch | Forbidden |
|-------|-----------|-----------|
| `engine-agent` | `拆分-打包/`, `*.py` root engine | `app/`, `service/` |
| `service-agent` | `service/` | `app/`, engine `*.py` |
| `ui-agent` | `app/` | `service/`, engine `*.py` |
