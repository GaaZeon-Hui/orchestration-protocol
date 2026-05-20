---
name: orchestration-protocol
description: Entry point for the orchestration system. On first load, determines role (orchestrator or worker) via heartbeat check and redirects to the role-specific file. After role is registered, agents should load their role file directly instead of this entry.
---

# Orchestration Protocol — Entry（角色分流）

两个角色共用此系统：

| 角色 | 职责 | 角色文件 |
|------|------|---------|
| **Orchestrator** | 哨兵检查 → 三项分析 → 阻塞汇报 → 签发锁 → 验证释锁 | `orchestrator-role.md` |
| **Worker Agent** | 提交请求 → 等锁 → 修改 → 自审 → completion | `worker-role.md` |

---

## 首次加载：角色注册

**本文件只在首次加载或编排者更替时执行。角色注册完成后，后续每次启动应直接加载对应角色 md，不再重复加载本入口。**

### 注册流程

```
1. 连接 orchestrator.db，执行 DDL（如不存在则建表，如缺少新列则 ALTER TABLE）
2. SELECT orchestrator_heartbeat FROM lock WHERE id=1
3. 判断心跳：
   - heartbeat IS NULL → 席位空置
   - heartbeat 距今 > 90 秒 → 编排者已死，席位空置
   - heartbeat 距今 ≤ 90 秒 → 编排者存活，注册为工作者
4. 如席位空置 → 尝试抢注：
   UPDATE lock SET orchestrator_id=?, orchestrator_heartbeat=now, orchestrator_started_at=now
   WHERE id=1 AND (heartbeat IS NULL OR heartbeat < now - 90s)
   → rowcount=1 → 注册为编排者
   → rowcount=0 → 被其他 agent 抢先，降级为工作者
5. 注册为编排者 → **立即读 orchestrator-role.md**
   注册为工作者 → **立即读 worker-role.md**
```

### 抢注实现

```python
import sqlite3, uuid

DB = '.claude/orchestrator/orchestrator.db'
conn = sqlite3.connect(DB)
cur = conn.cursor()

# 1. 确保数据库结构存在（执行下方 DDL）
# 2. 迁移旧表：如果 lock 表缺少新列
try:
    cur.execute("ALTER TABLE lock ADD COLUMN orchestrator_id TEXT")
except: pass
try:
    cur.execute("ALTER TABLE lock ADD COLUMN orchestrator_heartbeat TEXT")
except: pass
try:
    cur.execute("ALTER TABLE lock ADD COLUMN orchestrator_started_at TEXT")
except: pass

# 3. 检查心跳
cur.execute("SELECT orchestrator_heartbeat FROM lock WHERE id=1")
row = cur.fetchone()
heartbeat = row[0] if row else None

# 4. 判断席位是否空置
seat_vacant = heartbeat is None
if not seat_vacant:
    cur.execute("""
        SELECT datetime(?) < datetime('now','localtime','-90 seconds')
    """, (heartbeat,))
    seat_vacant = bool(cur.fetchone()[0])

# 5. 尝试抢注
if seat_vacant:
    my_id = str(uuid.uuid4())[:8]
    cur.execute("""
        UPDATE lock SET
            orchestrator_id = ?,
            orchestrator_heartbeat = datetime('now','localtime'),
            orchestrator_started_at = datetime('now','localtime')
        WHERE id=1
          AND (orchestrator_heartbeat IS NULL
               OR datetime(orchestrator_heartbeat) < datetime('now','localtime','-90 seconds'))
    """, (my_id,))
    conn.commit()
    if cur.rowcount == 1:
        role = 'orchestrator'
        print(f"REGISTERED AS ORCHESTRATOR (id={my_id})")
    else:
        role = 'worker'
        print("REGISTERED AS WORKER (lost race)")
else:
    role = 'worker'
    print("REGISTERED AS WORKER (orchestrator alive)")

conn.close()
# → role == 'orchestrator' → 读 orchestrator-role.md
# → role == 'worker'       → 读 worker-role.md
```

> 两个 agent 同时启动、同时认为席位空置时，SQLite 行级锁自动序列化两个 UPDATE。第一个 `rowcount=1` 成功抢注，第二个 `rowcount=0` 降级为工作者。

---

## 共享基础设施：数据库 Schema

所有状态集中在 `.claude/orchestrator/orchestrator.db`，WAL 模式支持高并发读写。

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=5000;

-- 锁表（单行 id=1，同时承载任务锁 + 角色注册）
CREATE TABLE IF NOT EXISTS lock (
    id INTEGER PRIMARY KEY CHECK(id=1),
    state TEXT NOT NULL DEFAULT 'idle',         -- 'idle' | 'locked'
    holder TEXT,                                 -- 任务锁持有者 (worker agent)
    request_id TEXT,
    scope_json TEXT,                             -- {"files": [...], "forbidden": [...]}
    acquired_at TEXT,
    expires_at TEXT,                             -- acquired_at + 60min
    -- 角色注册（以下三列）
    orchestrator_id TEXT,                        -- 编排者 session ID
    orchestrator_heartbeat TEXT,                 -- 最后心跳时间
    orchestrator_started_at TEXT                 -- 编排者启动时间
);

CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,                         -- {agent}-{YYYYMMDD}-{HHMMSS}
    agent TEXT NOT NULL,                         -- engine-agent | service-agent | ui-agent
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | approved | approved-with-warning | rejected
    reason TEXT,
    scope_json TEXT,                             -- {"modules": [...], "files": [...], "excluded": [...]}
    plan_json TEXT,                              -- {"summary": "...", "steps": [...], ...}
    self_review_json TEXT,
    constraints_json TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_updated ON requests(updated_at);
CREATE INDEX IF NOT EXISTS idx_requests_agent ON requests(agent);

CREATE TABLE IF NOT EXISTS approvals (
    request_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,                        -- approved | approved-with-warning | rejected
    granted_scope_json TEXT,
    rejection_reason TEXT,
    reviewed_by TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
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
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
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
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS processed (
    id TEXT PRIMARY KEY,                         -- request_id 或 completion_id
    type TEXT NOT NULL,                          -- 'request' | 'completion'
    status TEXT NOT NULL,                        -- approved | rejected | verified
    processed_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_processed_type ON processed(type);
```

> 所有 JSON 字段使用 `json.dumps()` 写入、`json.loads()` 读取。

---

## 模块边界（双方遵守）

| Agent | Can touch | Forbidden |
|-------|-----------|-----------|
| `engine-agent` | `拆分-打包/`, `*.py` root engine | `app/`, `service/` (without approval) |
| `service-agent` | `service/` | `app/`, engine `*.py` |
| `ui-agent` | `app/` | `service/`, engine `*.py` |

---

## 角色重定向

> **注册角色后，编排者每次直接加载 `orchestrator-role.md`，工作者每次直接加载 `worker-role.md`。**
>
> 不要重复加载本入口文件，除非需要触发重新注册（编排者更替场景）。
