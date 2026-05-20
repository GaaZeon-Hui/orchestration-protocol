"""
测试编排者注册流程

模拟场景：
1. 无编排者时，第一个 agent 抢注成功 → orchestrator
2. 编排者存活时，第二个 agent 自动注册为 worker
3. 两个 agent 同时启动 → 行级锁保证只有一个成为 orchestrator
4. 编排者心跳过期 → 新 agent 接任
"""
import sqlite3, uuid, time, threading, os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_orchestrator.db")

def setup_db():
    """初始化测试数据库"""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA busy_timeout=5000;

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

        CREATE TABLE IF NOT EXISTS processed (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            status TEXT NOT NULL,
            processed_at TEXT DEFAULT (datetime('now','localtime'))
        );
    """)
    conn.commit()
    conn.close()
    print("[SETUP] 测试数据库已初始化")

def try_register(agent_name):
    """模拟一个 agent 尝试注册角色"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 迁移兼容
    for col in ["orchestrator_id", "orchestrator_heartbeat", "orchestrator_started_at"]:
        try:
            cur.execute(f"ALTER TABLE lock ADD COLUMN {col} TEXT")
        except:
            pass

    # 检查心跳
    cur.execute("SELECT orchestrator_id, orchestrator_heartbeat FROM lock WHERE id=1")
    row = cur.fetchone()

    seat_vacant = True
    existing_orch = None
    if row and row[1]:
        existing_orch = row[0]
        cur.execute("""
            SELECT datetime(?) < datetime('now','localtime','-90 seconds')
        """, (row[1],))
        seat_vacant = bool(cur.fetchone()[0])
        print(f"  [{agent_name}] 当前编排者: {row[0]}, 心跳: {row[1]}, 过期: {seat_vacant}")

    if seat_vacant:
        my_id = f"{agent_name}-{str(uuid.uuid4())[:4]}"
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
            print(f"  [{agent_name}] ✓ 注册为 ORCHESTRATOR (id={my_id})")
            conn.close()
            return "orchestrator", my_id
        else:
            print(f"  [{agent_name}] ✗ 抢注失败，降级为 WORKER")
            conn.close()
            return "worker", None
    else:
        print(f"  [{agent_name}] → 编排者存活，注册为 WORKER")
        conn.close()
        return "worker", None

def send_heartbeat(orchestrator_id):
    """编排者发送心跳"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE lock SET orchestrator_heartbeat = datetime('now','localtime')
        WHERE id=1 AND orchestrator_id=?
    """, (orchestrator_id,))
    result = cur.rowcount
    conn.commit()
    conn.close()
    return result == 1

def simulate_slow_heartbeat(orchestrator_id):
    """模拟心跳过期：将心跳设为 120 秒前"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        UPDATE lock SET orchestrator_heartbeat = datetime('now','localtime','-120 seconds')
        WHERE id=1 AND orchestrator_id=?
    """, (orchestrator_id,))
    conn.commit()
    conn.close()
    print("[SIM] 编排者心跳已设为 120 秒前（模拟过期）")


# ===== 测试场景 =====
if __name__ == "__main__":
    print("=" * 60)
    print("场景 1: 第一个 agent 启动 → 应成为 orchestrator")
    print("=" * 60)
    setup_db()
    role, orch_id = try_register("agent-A")
    assert role == "orchestrator", f"FAIL: 预期 orchestrator, 得到 {role}"
    assert orch_id is not None
    print("  PASS\n")

    print("=" * 60)
    print("场景 2: 编排者存活时，第二个 agent 启动 → 应为 worker")
    print("=" * 60)
    # 编排者先发一个心跳
    assert send_heartbeat(orch_id), "心跳发送失败"
    role, _ = try_register("agent-B")
    assert role == "worker", f"FAIL: 预期 worker, 得到 {role}"
    print("  PASS\n")

    print("=" * 60)
    print("场景 3: 两个 agent 同时启动（模拟竞态）→ 只有一个 orchestrator")
    print("=" * 60)
    # 先清除编排者
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE lock SET orchestrator_id=NULL, orchestrator_heartbeat=NULL")
    conn.commit()
    conn.close()

    results = []
    def concurrent_register(name):
        results.append(try_register(name))

    t1 = threading.Thread(target=concurrent_register, args=("agent-X",))
    t2 = threading.Thread(target=concurrent_register, args=("agent-Y",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    orch_count = sum(1 for r in results if r[0] == "orchestrator")
    assert orch_count == 1, f"FAIL: 应有恰好 1 个 orchestrator, 实际 {orch_count}"
    print(f"  orchestrator 数量: {orch_count}")
    print("  PASS\n")

    print("=" * 60)
    print("场景 4: 编排者心跳过期 → 新 agent 接任")
    print("=" * 60)
    # 找到当前编排者
    orch_role, orch_id = None, None
    for r in results:
        if r[0] == "orchestrator":
            orch_role, orch_id = r
            break
    assert orch_id is not None

    # 模拟心跳过期
    simulate_slow_heartbeat(orch_id)

    # 新 agent 尝试注册
    role, new_id = try_register("agent-Z")
    assert role == "orchestrator", f"FAIL: 预期 orchestrator (接任), 得到 {role}"
    assert new_id != orch_id, "FAIL: 新编排者 ID 应与旧的不同"
    print(f"  旧编排者: {orch_id}, 新编排者: {new_id}")
    print("  PASS\n")

    # 清理
    os.remove(DB_PATH)
    print("=" * 60)
    print("所有测试通过")
    print("=" * 60)
