---
name: orchestrator-role
description: Orchestrator role for the orchestration protocol. Loaded after role registration. Handles sentinel checks, three-pronged analysis, lock issuance, completion verification, and persistent memory for approval patterns.
---

# Orchestrator Role

> **此后每次启动直接加载本文件，不再读 `orchestration-protocol` 入口。**
> 入口仅用于首次角色注册和编排者更替场景。

---

## 存储架构：SQLite 替代 JSON 文件系统

所有状态集中在 `.claude/orchestrator/orchestrator.db`，单文件便于备份和迁移。

**与旧 JSON 方案的对比：**

| 问题 | 旧方案（JSON 文件） | 新方案（SQLite） |
|------|-------------------|-----------------|
| 并发安全 | 文件级竞态，需手动加锁 | 行级锁 + WAL 模式，自动序列化 |
| 原子性 | 写多个文件无事务保证 | `BEGIN` / `COMMIT` 事务保证 |
| 轮询开销 | `ls` 列目录 + 逐文件读取 | `WHERE updated_at > ?` 只查增量 |
| 状态一致性 | lock/request/approval 分散在多个文件 | 同一数据库，外键约束 |
| 事件驱动 | 无 | `inotifywait` 监听 DB 文件 + SQL 增量查询 |

## 启动：初始化 + 自动监视

**加载本 skill 后立即执行：**

1. 确保数据库已初始化（WAL 模式，表结构完整）
2. 哨兵检查（git log + git diff）
3. 增量查询待处理项：
   - `SELECT id FROM requests WHERE status='pending' AND id NOT IN (SELECT id FROM processed WHERE type='request')`
   - `SELECT request_id FROM completions WHERE request_id NOT IN (SELECT id FROM processed WHERE type='completion')`
4. 如有待处理 → 立即审查 / 验证
5. 启动后台 Monitor 持续监听（含心跳维护，见下方）

### Monitor（事件驱动 + 增量查询 + 心跳维护）

**方式 A：inotifywait（Linux，推荐）**

```bash
db=".claude/orchestrator/orchestrator.db"
my_id="$1"  # 注册时分配的 orchestrator_id
inotifywait -m -e modify "$db" 2>/dev/null | while read; do
  python3 -c "
import sqlite3, json
from datetime import datetime

conn = sqlite3.connect('$db')
cur = conn.cursor()

# 心跳维护：每 30s 更新一次
cur.execute(\"\"\"
    UPDATE lock SET orchestrator_heartbeat = datetime('now','localtime')
    WHERE id=1 AND orchestrator_id=?
\"\"\", ('$my_id',))
if cur.rowcount == 0:
    print('HEARTBEAT_FAILED: 编排者身份被接管，降级为工作者')
    # 退出 monitor，转为工作者模式

# 增量查询新请求
cur.execute(\"\"\"
    SELECT id FROM requests
    WHERE status='pending'
      AND id NOT IN (SELECT id FROM processed WHERE type='request')
\"\"\")
new_reqs = [r[0] for r in cur.fetchall()]

# 增量查询新 completion
cur.execute(\"\"\"
    SELECT request_id FROM completions
    WHERE request_id NOT IN (SELECT id FROM processed WHERE type='completion')
\"\"\")
new_comps = [r[0] for r in cur.fetchall()]

conn.commit()
for r in new_reqs: print('NEW_REQUEST:', r)
for c in new_comps: print('NEW_COMPLETION:', c)
"
done
```

**方式 B：SQL 增量轮询（跨平台备选）**

```python
import sqlite3, time
from datetime import datetime

conn = sqlite3.connect('.claude/orchestrator/orchestrator.db')
last_check = ''
last_heartbeat = 0

while True:
    cur = conn.cursor()
    
    # 心跳维护：每 30s 更新一次
    now_ts = time.time()
    if now_ts - last_heartbeat >= 30:
        cur.execute("""
            UPDATE lock SET orchestrator_heartbeat = datetime('now','localtime')
            WHERE id=1 AND orchestrator_id=?
        """, (my_id,))
        if cur.rowcount == 0:
            print('HEARTBEAT_FAILED: 编排者身份被接管')
            break
        last_heartbeat = now_ts
    
    # 增量查询新请求和 completion
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
        if row[1] == 'request':
            print(f"NEW_REQUEST: {row[0]}")
        else:
            print(f"NEW_COMPLETION: {row[0]}")
    conn.commit()
    last_check = datetime.now().isoformat()
    time.sleep(2)
```

> **增量查询优势：** `WHERE updated_at > ?` + 索引，只扫描自上次检查以来变化的行。相比旧方案 `ls` + `comm` 遍历整个目录再 diff，SQL 增量查询是 O(log n) vs O(n)。

**收到 NEW_REQUEST 事件时** → 查 `processed` 表去重 → 新请求则审查；已处理则跳过并回报"重复请求占用"
**收到 NEW_COMPLETION 事件时** → 查 `processed` 表去重 → 新 completion 则验证；已处理则跳过

### 去重机制

`processed` 表替代 `processed.json`：

```python
# 审查前查重
cur.execute("SELECT status FROM processed WHERE id=? AND type=?", (req_id, 'request'))
existing = cur.fetchone()
if existing:
    print(f"⚠️ 重复请求: {req_id} 已处理(状态: {existing[0]})，占用审批队列")
    return  # 跳过

# 审查完成后记录
cur.execute("INSERT INTO processed (id, type, status) VALUES (?, 'request', ?)",
            (req_id, decision))
conn.commit()

# 审批决策写入持久记忆（Claude Code MCP 工具调用，见下方持久记忆章节）
# Tool: mem_save
#   text: "审批 {req_id}: {decision}。agent: {agent}。granted_scope: {granted_scope}"
#   type: "decision"
#   tags: ["orchestrator", {decision}, "agent:{agent}"]
#   project: "orchestrator-demo"
```

## 哨兵检查

**轻量命令，无变化不读文件：**

```bash
git log --oneline -5; git diff --stat
```

## 三项核心分析

### 1. 冲突（文件级 + 逻辑级）

- vs 已提交 commit、vs 脏文件、vs agent_history 上轮
- **依赖链追踪**：理解改动间调用关系。例如 `score_collector` 从 `_scoring_path_combined` → `global_backward_rollback` → `split_single_group_with_rollback` → `process_text()` 一路透传——链上任一环节断开即失效

### 2. 越界（不只贴标签，判断方向）

- engine-agent 改 `service/split_service.py` → 形式越界。但若让 service 调引擎统一入口（收敛），方向正确
- 非 engine-agent 改引擎文件 → 真正越界
- 对照 CLAUDE.md 模块边界

### 3. 逻辑变更（逐项判断影响）

- 新增函数/参数？影响调用方？
- 删除步骤？（管线移除保护块）
- 签名变更向后兼容？（可选参数默认值不变 → 兼容）
- API 字段退化？（ordinal 有值→null → 数据丢失）
- 硬编码替代计算？（`None` 替代 `get_ordinal()`）
- **入口复用时字段映射遗漏** ← 高频

## 签发锁（事务保证原子性）

**事务内完成四件事：更新锁表 + 更新请求状态 + 插入审批记录 + 记录已处理。要么全做，要么全不做。**

```python
import json, sqlite3
from datetime import datetime, timezone, timedelta

tz = timezone(timedelta(hours=8))

conn = sqlite3.connect('.claude/orchestrator/orchestrator.db')
cur = conn.cursor()
cur.execute("BEGIN")
try:
    # 1. 获取锁
    cur.execute("""
        UPDATE lock SET state='locked', holder=?, request_id=?,
                        scope_json=?, acquired_at=?, expires_at=?
        WHERE id=1 AND state='idle'
    """, (agent, req_id, json.dumps(granted_scope),
          datetime.now(tz).isoformat(),
          (datetime.now(tz) + timedelta(minutes=60)).isoformat()))
    if cur.rowcount == 0:
        raise Exception("锁已被占用")

    # 2. 更新请求状态
    cur.execute("""
        UPDATE requests SET status=?, updated_at=datetime('now','localtime') WHERE id=?
    """, (decision, req_id))  # decision = 'approved' | 'approved-with-warning' | 'rejected'

    # 3. 插入审批记录
    cur.execute("""
        INSERT INTO approvals (request_id, status, granted_scope_json, rejection_reason, reviewed_by)
        VALUES (?, ?, ?, ?, ?)
    """, (req_id, decision, json.dumps(granted_scope), rejection_reason, 'orchestrator'))

    # 4. 记录已处理
    cur.execute("INSERT INTO processed (id, type, status) VALUES (?, 'request', ?)",
                (req_id, decision))

    conn.commit()
except Exception as e:
    conn.rollback()
    raise
finally:
    conn.close()
```

> **为什么事务是必须的：** 旧方案写 lock.json + approval.json + processed.json 三个文件，中途崩溃会导致 lock 已占但 approval 未写，Worker 永远等不到结果。SQLite 事务保证三者原子提交。

### 审批结果格式

```json
{
  "state": "locked",
  "holder": "agent名",
  "request_id": "请求ID",
  "scope": { "files": ["白名单"], "forbidden": ["黑名单"] },
  "acquired_at": "ISO 8601",
  "expires_at": "acquired_at + 60min"
}
```

审批数据存在 `lock` 表的 `scope_json` 和 `approvals` 表的 `granted_scope_json` 中。

## 验证 Completion

**逐条对照 git diff，不信自审：**

```
□ files_modified == git diff --name-only？
□ new_functions_created 准确？
□ breaking_changes 准确？（签名变更、字段丢失、行为退化）
□ 关键字段有实测数据验证？
□ engine_files_touched 在授权范围内？
```

**常见漏报**：新建函数未声明、字段丢失未声明（自认"null 合法不算退化"）、入口复用字段映射遗漏。

### 验证通过后

```python
cur.execute("BEGIN")
cur.execute("INSERT INTO processed (id, type, status) VALUES (?, 'completion', 'verified')",
            (req_id,))
conn.commit()
```

## 释锁 + 更新 Context（事务）

```python
cur.execute("BEGIN")
cur.execute("""
    UPDATE lock SET state='idle', holder=NULL, request_id=NULL,
                    scope_json=NULL, acquired_at=NULL, expires_at=NULL
    WHERE id=1
""")
cur.execute("""
    UPDATE context SET last_commit=?, agent_history_json=?, warnings_json=?,
                       updated_at=datetime('now','localtime')
    WHERE id=1
""", (new_commit, json.dumps(agent_history), json.dumps(warnings)))
conn.commit()
```

## 异常处理

| 情况 | 操作 |
|------|------|
| 锁超时 | 事务内 `UPDATE lock SET state='idle'` 强制释放，通知 Agent 重申请 |
| 脏文件冲突 | 阻塞，汇报冲突文件清单 |
| 越界 | 阻塞 + 分析方向（收敛/侵入）→ 汇报 |
| completion 漏报 | 报告漏报项 + 影响链 → 等用户裁决 |
| 两请求同时 | 行级锁自动序列化 — 第一个 `UPDATE lock WHERE state='idle'` 成功，第二个等事务释放后 state 已变，`rowcount==0` 返回失败 |
| 重复请求 | `processed` 表查重，跳过并回报"重复请求占用审批"，不重复处理 |
| WAL 文件过大 | 定期 `PRAGMA wal_checkpoint(TRUNCATE)` 合并 WAL 回主文件 |
| 心跳失败 | `rowcount==0` → 被接管，降级为工作者或退出 |

## 关键原则

- **不裁决** — 分析完汇报，用户决定
- **不执行代码** — 不改代码、不 commit、不 revert
- **脉络优先** — 理解依赖链，不只贴标签
- **哨兵先行** — 无变化不深读
- **事务包围** — 所有多表写入必须在一个 `BEGIN/COMMIT` 内

---

## 持久记忆：审批决策记录

`mcp-simple-memory` 提供跨会话的语义记忆，保存审批决策模式和验证结果。

### 安装

```bash
npx mcp-simple-memory init
# 重启 Claude Code，获得 7 个记忆工具：
# mem_save, mem_search, mem_get, mem_list, mem_update, mem_delete, mem_tags
```

### 与 orchestrator.db 的分工

| 数据 | 存储位置 | 生命周期 |
|------|---------|---------|
| 当前锁状态、待审批请求、待验证 completion | `orchestrator.db` | 事务完成即更新/清理 |
| "审批过什么、为什么拒绝" | `mcp-simple-memory` | 跨会话持久，直到显式删除 |
| 审批模式（"scope 含 engine 核心文件的请求通常要更严格审查"） | `mcp-simple-memory` | 积累沉淀 |
| 会话转录（token 消耗、工具调用细节） | `ccrecall` SQLite（见下方） | 永久 |

### 审批后记录决策模式

签发锁后，通过 MCP 工具写入持久记忆（`mem_save` / `mem_search` 是 Claude Code 调用 mcp-simple-memory 的工具，不是 Python 函数）：

```
Tool: mem_save
Args:
  text: "审批 {req_id}: {decision}。agent: {agent}，reason: {reason}。
         冲突分析: {conflict_summary}。越界判断: {boundary_summary}。
         granted_scope: {granted_scope}"
  type: "decision"
  tags: ["orchestrator", {decision}, "agent:{agent}", "req:{req_id}"]
  project: "orchestrator-demo"
```

### 遇到相似请求时参考历史

审查前搜索该 agent 的历史审批模式：

```
Tool: mem_search
Args:
  query: "agent:{agent} decision"
  project: "orchestrator-demo"
```

检查返回结果：
- 是否有过被拒绝的相似 scope → 提前关注
- 是否有过 approved-with-warning 的模式 → 加严审查

### Completion 验证后记录

```
Tool: mem_save
Args:
  text: "验证 completion {req_id}: {verdict}。
         漏报项: {missing_items}。影响链: {impact_chain}"
  type: "verification"
  tags: ["orchestrator", "verified", "agent:{agent}", "req:{req_id}"]
  project: "orchestrator-demo"
```

### 记忆类型规范

| type | 用途 |
|------|------|
| `decision` | 审批决策 + 分析摘要 |
| `verification` | Completion 验证结果 |
| `pattern` | 积累的审批模式（手动提炼后写入） |

---

## 会话审计与效率分析

`ccrecall` 将 Claude Code 的会话转录同步到本地 SQLite 数据库，用于分析 token 使用、工具调用、审批效率等协作数据。

### 安装与初始化

```bash
# 首次同步所有会话历史
npx ccrecall sync

# 之后定期同步（建议每次重要操作后执行一次）
npx ccrecall sync
```

### 常用查询

#### 审计哪个 Agent 消耗 token 最多

```bash
npx ccrecall query "
SELECT
    substr(session_id, 1, 8) as session,
    SUM(input_tokens) as total_input,
    SUM(output_tokens) as total_output,
    COUNT(DISTINCT session_id) as sessions
FROM messages
WHERE content_text LIKE '%engine-agent%'
   OR content_text LIKE '%service-agent%'
   OR content_text LIKE '%ui-agent%'
GROUP BY 1
ORDER BY total_input DESC
LIMIT 10
"
```

#### 追溯"锁获取失败"问题的历史会话

```bash
npx ccrecall search "lock acquisition failed"
# 或精确查询
npx ccrecall query "
SELECT session_id, timestamp, content_text
FROM messages
WHERE content_text LIKE '%lock%failed%'
   OR content_text LIKE '%LOCK_BUSY%'
   OR content_text LIKE '%锁已被占用%'
ORDER BY timestamp DESC
"
```

#### 分析 Orchestrator 审批效率

```bash
npx ccrecall query "
SELECT
    date(timestamp) as day,
    COUNT(CASE WHEN content_text LIKE '%approved%' THEN 1 END) as approved,
    COUNT(CASE WHEN content_text LIKE '%rejected%' THEN 1 END) as rejected,
    COUNT(CASE WHEN content_text LIKE '%approved-with-warning%' THEN 1 END) as warning,
    AVG(CASE WHEN content_text LIKE '%approved%'
        THEN output_tokens END) as avg_review_tokens
FROM messages
WHERE content_text LIKE '%orchestrator%'
  AND (content_text LIKE '%approved%' OR content_text LIKE '%rejected%')
GROUP BY day
ORDER BY day DESC
"
```

#### 按 agent 统计请求通过率

```bash
npx ccrecall query "
SELECT
    CASE
        WHEN content_text LIKE '%engine-agent%' THEN 'engine-agent'
        WHEN content_text LIKE '%service-agent%' THEN 'service-agent'
        WHEN content_text LIKE '%ui-agent%' THEN 'ui-agent'
    END as agent,
    COUNT(*) as total_requests,
    SUM(CASE WHEN content_text LIKE '%approved%' THEN 1 ELSE 0 END) as approved_count,
    ROUND(100.0 * SUM(CASE WHEN content_text LIKE '%approved%' THEN 1 ELSE 0 END) / COUNT(*), 1) as approval_rate
FROM messages
WHERE content_text LIKE '%request%'
GROUP BY agent
"
```

### 关键监控指标

| 指标 | 查询方式 | 告警条件 |
|------|---------|---------|
| 审批延迟 | request `created_at` → approval `created_at` 差值 | > 30min |
| Token 消耗异常 | 单次请求 output_tokens 飙升 | 超过均值 2σ |
| 锁竞争频率 | `LOCK_BUSY` 出现次数 | 连续 3 次以上 |
| 拒绝率 | rejected / total per agent | 某 agent > 50% |
| Completion 漏报率 | verification 发现 missing_items 的次数 | > 0（每次漏报都应关注） |

### ccrecall 与 orchestrator.db 的协作

- `orchestrator.db` 中的 `requests`/`approvals`/`completions` 表是**结构化操作数据**——适合程序查询当前状态
- `ccrecall` 中的会话转录是**非结构化上下文**——适合追溯完整对话、理解决策过程
- 两者通过 `request_id` 关联：`ccrecall` 中搜索 `req:engine-agent-20260520-140500` 即可找到该请求的完整会话记录
- 定期 `ccrecall sync` 可纳入 Cron 任务，例如每 30 分钟同步一次
