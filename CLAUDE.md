# Orchestrator Demo

启动时执行以下代码确定角色，直接运行不展开讨论：

```python
import sqlite3, uuid, os

DB = os.path.join(os.getcwd(), '.claude', 'orchestrator', 'orchestrator.db')
os.makedirs(os.path.dirname(DB), exist_ok=True)
conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("PRAGMA journal_mode=WAL")
cur.execute("PRAGMA busy_timeout=5000")

cur.executescript("""
    CREATE TABLE IF NOT EXISTS lock (
        id INTEGER PRIMARY KEY CHECK(id=1), state TEXT DEFAULT 'idle',
        holder TEXT, request_id TEXT, scope_json TEXT, acquired_at TEXT, expires_at TEXT,
        orchestrator_id TEXT, orchestrator_heartbeat TEXT, orchestrator_started_at TEXT
    );
    INSERT OR IGNORE INTO lock (id) VALUES (1);
    CREATE TABLE IF NOT EXISTS requests (
        id TEXT PRIMARY KEY, agent TEXT NOT NULL, status TEXT DEFAULT 'pending',
        reason TEXT, scope_json TEXT, plan_json TEXT, self_review_json TEXT,
        constraints_json TEXT, created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
    CREATE INDEX IF NOT EXISTS idx_requests_updated ON requests(updated_at);
    CREATE TABLE IF NOT EXISTS approvals (
        request_id TEXT PRIMARY KEY, status TEXT NOT NULL, granted_scope_json TEXT,
        rejection_reason TEXT, reviewed_by TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (request_id) REFERENCES requests(id)
    );
    CREATE TABLE IF NOT EXISTS completions (
        request_id TEXT PRIMARY KEY, agent TEXT NOT NULL, completed_at TEXT,
        self_review_json TEXT, commits_json TEXT, sync_notes TEXT,
        context_updates_json TEXT, created_at TEXT DEFAULT (datetime('now','localtime')),
        FOREIGN KEY (request_id) REFERENCES requests(id)
    );
    CREATE TABLE IF NOT EXISTS context (
        id INTEGER PRIMARY KEY CHECK(id=1), last_commit TEXT,
        agent_history_json TEXT DEFAULT '[]', warnings_json TEXT DEFAULT '[]',
        pipeline TEXT, api_contract TEXT, meta_fields TEXT,
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    );
    INSERT OR IGNORE INTO context (id) VALUES (1);
    CREATE TABLE IF NOT EXISTS processed (
        id TEXT PRIMARY KEY, type TEXT NOT NULL, status TEXT NOT NULL,
        processed_at TEXT DEFAULT (datetime('now','localtime'))
    );
""")

for col in ["orchestrator_id","orchestrator_heartbeat","orchestrator_started_at"]:
    try: cur.execute(f"ALTER TABLE lock ADD COLUMN {col} TEXT")
    except: pass
conn.commit()

cur.execute("SELECT orchestrator_heartbeat FROM lock WHERE id=1")
row = cur.fetchone()
heartbeat = row[0] if row else None
seat_vacant = heartbeat is None
if not seat_vacant:
    cur.execute("SELECT datetime(?) < datetime('now','localtime','-90 seconds')", (heartbeat,))
    seat_vacant = bool(cur.fetchone()[0])

if seat_vacant:
    my_id = str(uuid.uuid4())[:8]
    cur.execute("""
        UPDATE lock SET orchestrator_id=?, orchestrator_heartbeat=datetime('now','localtime'),
                        orchestrator_started_at=datetime('now','localtime')
        WHERE id=1 AND (orchestrator_heartbeat IS NULL
                        OR datetime(orchestrator_heartbeat) < datetime('now','localtime','-90 seconds'))
    """, (my_id,))
    conn.commit()
    role = 'orchestrator' if cur.rowcount == 1 else 'worker'
else:
    role = 'worker'

conn.close()
print(f"ROLE: {role}" + (f" (id={my_id})" if 'my_id' in dir() else ""))
```

- `ROLE: orchestrator` → 读 `.claude/skills/orchestrator-role.md`
- `ROLE: worker` → 读 `.claude/skills/worker-role.md`
