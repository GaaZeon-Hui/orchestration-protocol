---
name: orchestrator-role
description: Orchestrator role. Handles sentinel checks, three-pronged analysis, lock issuance, completion verification, heartbeat maintenance, and persistent memory for approval patterns.
---

# Orchestrator Role

> 此后每次启动直接加载本文件，不再读 `orchestration-protocol` 入口。

所有状态在 `.claude/orchestrator/orchestrator.db`（SQLite WAL 模式，行级锁自动序列化，`updated_at` 索引支持增量查询）。

## 启动

**加载后立即执行：**

1. 确保数据库已初始化
2. 哨兵检查：`git log --oneline -5; git diff --stat`
3. 查待处理项：
   - `SELECT id FROM requests WHERE status='pending' AND id NOT IN (SELECT id FROM processed WHERE type='request')`
   - `SELECT request_id FROM completions WHERE request_id NOT IN (SELECT id FROM processed WHERE type='completion')`
4. 如有待处理 → 立即审查
5. 启动后台 Monitor（含心跳维护）

### Monitor + 心跳维护

```python
import sqlite3, time
from datetime import datetime

conn = sqlite3.connect('.claude/orchestrator/orchestrator.db')
last_check = ''
last_heartbeat = 0

while True:
    cur = conn.cursor()

    # 每 30s 更新心跳
    now_ts = time.time()
    if now_ts - last_heartbeat >= 30:
        cur.execute("""
            UPDATE lock SET orchestrator_heartbeat=datetime('now','localtime')
            WHERE id=1 AND orchestrator_id=?
        """, (my_id,))
        if cur.rowcount == 0:
            print('HEARTBEAT_FAILED: 被接管，降级')
            break
        last_heartbeat = now_ts

    # 增量查询（仅查上次检查后变更的行）
    cur.execute("""
        SELECT id, 'request' as type FROM requests
        WHERE status='pending' AND updated_at > ?
          AND id NOT IN (SELECT id FROM processed WHERE type='request')
        UNION ALL
        SELECT request_id, 'completion' FROM completions
        WHERE created_at > ?
          AND request_id NOT IN (SELECT id FROM processed WHERE type='completion')
    """, (last_check, last_check))
    for row in cur.fetchall():
        tag = 'NEW_REQUEST' if row[1] == 'request' else 'NEW_COMPLETION'
        print(f'{tag}: {row[0]}')
    conn.commit()
    last_check = datetime.now().isoformat()
    time.sleep(2)
```

`WHERE updated_at > ?` 只扫描增量行，O(log n)。

### 去重

```python
cur.execute("SELECT status FROM processed WHERE id=? AND type=?", (req_id, 'request'))
if cur.fetchone():
    print(f"⚠️ 重复: {req_id} 已处理，跳过")
    return

# 审查后记录
cur.execute("INSERT INTO processed (id, type, status) VALUES (?, 'request', ?)", (req_id, decision))
conn.commit()

# 审批决策写入持久记忆（MCP 工具 mem_save，见下方）
```

## 哨兵检查

```bash
git log --oneline -5; git diff --stat
```

## 三项核心分析

### 1. 冲突
- vs 已提交 commit、vs 脏文件、vs agent_history 上轮
- 依赖链追踪：链上任一环节断开即失效

### 2. 越界（判断方向，不只贴标签）
- engine-agent 改 `service/` → 形式越界。若让 service 调引擎统一入口（收敛），方向正确
- 非 engine-agent 改引擎文件 → 真正越界

### 3. 逻辑变更
- 新增函数/参数？签名变更向后兼容？API 字段退化？
- 硬编码替代计算？（`None` 替代 `get_ordinal()` — 数据丢失）
- **入口复用时字段映射遗漏** ← 高频

## 签发锁（事务）

```python
import json, sqlite3
from datetime import datetime, timezone, timedelta

tz = timezone(timedelta(hours=8))
conn = sqlite3.connect('.claude/orchestrator/orchestrator.db')
cur = conn.cursor()
cur.execute("BEGIN")
try:
    cur.execute("""
        UPDATE lock SET state='locked', holder=?, request_id=?,
                        scope_json=?, acquired_at=?, expires_at=?
        WHERE id=1 AND state='idle'
    """, (agent, req_id, json.dumps(granted_scope),
          datetime.now(tz).isoformat(),
          (datetime.now(tz) + timedelta(minutes=60)).isoformat()))
    if cur.rowcount == 0:
        raise Exception("锁已被占用")

    cur.execute("UPDATE requests SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
                (decision, req_id))

    cur.execute("""INSERT INTO approvals (request_id, status, granted_scope_json, rejection_reason, reviewed_by)
                   VALUES (?,?,?,?,?)""",
                (req_id, decision, json.dumps(granted_scope), rejection_reason, 'orchestrator'))

    cur.execute("INSERT INTO processed (id, type, status) VALUES (?, 'request', ?)", (req_id, decision))
    conn.commit()
except Exception as e:
    conn.rollback()
    raise
finally:
    conn.close()
```

事务保证四步原子：锁+状态+审批+去重。中途崩溃全回滚，不会出现"锁已占但审批未写"。

### 审批结果格式

```json
{
  "state": "locked", "holder": "agent名", "request_id": "请求ID",
  "scope": { "files": ["白名单"], "forbidden": ["黑名单"] },
  "acquired_at": "ISO 8601", "expires_at": "acquired_at + 60min"
}
```

## 验证 Completion

逐条对照 `git diff`，不信自审：

```
□ files_modified == git diff --name-only？
□ new_functions_created 准确？
□ breaking_changes 准确？
□ 关键字段有实测数据？(如 ordinal_verified)
□ engine_files_touched 在授权范围内？
```

常见漏报：新建函数未声明、字段丢失未声明、入口复用字段映射遗漏。

```python
cur.execute("BEGIN")
cur.execute("INSERT INTO processed (id, type, status) VALUES (?, 'completion', 'verified')", (req_id,))
conn.commit()
```

## 释锁 + 更新 Context

```python
cur.execute("BEGIN")
cur.execute("""UPDATE lock SET state='idle', holder=NULL, request_id=NULL,
                scope_json=NULL, acquired_at=NULL, expires_at=NULL WHERE id=1""")
cur.execute("""UPDATE context SET last_commit=?, agent_history_json=?, warnings_json=?,
               updated_at=datetime('now','localtime') WHERE id=1""",
            (new_commit, json.dumps(agent_history), json.dumps(warnings)))
conn.commit()
```

## 异常处理

| 情况 | 操作 |
|------|------|
| 锁超时 | `UPDATE lock SET state='idle'` 强制释放，通知 Agent 重申请 |
| 脏文件冲突 | 阻塞，汇报冲突文件清单 |
| 越界 | 阻塞 + 分析方向（收敛/侵入）→ 汇报 |
| completion 漏报 | 报告漏报项 + 影响链 → 等用户裁决 |
| 两请求同时 | SQLite 行级锁序列化，第二个 `rowcount=0` 返回失败 |
| 重复请求 | `processed` 表查重，跳过 |
| WAL 文件过大 | `PRAGMA wal_checkpoint(TRUNCATE)` |
| 心跳失败 | `rowcount=0` → 被接管，降级或退出 |

## 关键原则

- **不裁决** — 分析完汇报，用户决定
- **不执行代码** — 不改代码、不 commit、不 revert
- **脉络优先** — 理解依赖链，不只贴标签
- **事务包围** — 所有多表写入在 `BEGIN/COMMIT` 内

---

## 持久记忆：审批决策

通过 `mcp-simple-memory` MCP 工具保存跨会话决策记录。

**安装：** `npx mcp-simple-memory init`（一次）

### 审批后记录

```
Tool: mem_save
  text: "审批 {req_id}: {decision}。{conflict_summary}。{boundary_summary}。granted: {granted_scope}"
  type: "decision"
  tags: ["orchestrator", {decision}, "agent:{agent}", "req:{req_id}"]
  project: "orchestrator-demo"
```

### 审查前参考历史

```
Tool: mem_search
  query: "agent:{agent} decision"
  project: "orchestrator-demo"
```

### 验证后记录

```
Tool: mem_save
  text: "验证 {req_id}: {verdict}。漏报: {missing_items}。影响: {impact_chain}"
  type: "verification"
  tags: ["orchestrator", "verified", "agent:{agent}"]
  project: "orchestrator-demo"
```

| type | 用途 |
|------|------|
| `decision` | 审批决策 + 分析摘要 |
| `verification` | Completion 验证结果 |
| `pattern` | 积累的审批模式 |

---

## 会话审计

`ccrecall` 将会话转录同步到 SQLite 用于追溯分析。

```bash
npx ccrecall sync   # 定期同步
```

**追溯锁获取失败：**
```bash
npx ccrecall search "lock acquisition failed"
npx ccrecall query "SELECT session_id, timestamp, content_text FROM messages WHERE content_text LIKE '%LOCK_BUSY%' OR content_text LIKE '%锁已被占用%' ORDER BY timestamp DESC"
```

**审批效率概览：**
```bash
npx ccrecall query "
SELECT date(timestamp) as day,
       COUNT(CASE WHEN content_text LIKE '%approved%' THEN 1 END) as approved,
       COUNT(CASE WHEN content_text LIKE '%rejected%' THEN 1 END) as rejected
FROM messages WHERE content_text LIKE '%orchestrator%'
GROUP BY day ORDER BY day DESC"
```

| 指标 | 告警条件 |
|------|---------|
| 审批延迟 | request → approval > 30min |
| 锁竞争频率 | 连续 3 次 `LOCK_BUSY` |
| 拒绝率 | 某 agent > 50% |
| Completion 漏报 | > 0 |
