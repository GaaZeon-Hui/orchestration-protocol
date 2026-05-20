---
name: worker-role
description: Worker Agent role. Handles submitting requests, monitoring approvals via SQLite incremental polling, executing modifications, self-review, completion reporting, and persistent task memory for cross-session recovery.
---

# Worker Agent Role

> 此后每次启动直接加载本文件，不再读 `orchestration-protocol` 入口。

## Quick Reference

| Step | What |
|------|------|
| 0. Restore | `mem_search` 查未完成任务 |
| 1. Pull + Read | git pull, CLAUDE.md, lock state |
| 2. Check lock | `SELECT state FROM lock WHERE id=1` → 必须 `idle` |
| 3. Request | `INSERT INTO requests` + `mem_save` |
| 4. Monitor | SQL 增量轮询 → 检测 status 变更 |
| 5. Read approval | 查 `approvals` 表 |
| 6. Modify | 仅修改授权文件 |
| 7. Self-review | 自审越界和逻辑变更 |
| 8. Completion | `INSERT INTO completions` + `mem_update` |
| 9. Release | `UPDATE lock SET state='idle'` |

## Step 2: 检查锁

```python
import sqlite3
conn = sqlite3.connect('.claude/orchestrator/orchestrator.db')
cur = conn.cursor()
cur.execute("SELECT state, holder, expires_at FROM lock WHERE id=1")
row = cur.fetchone()
if row and row[0] == 'locked':
    print(f"LOCK_BUSY: held by {row[1]}, expires {row[2]}")
```

## Step 3: 提交请求

**Agent types:** `engine-agent`, `service-agent`, `ui-agent`

```python
import json, sqlite3
from datetime import datetime, timezone, timedelta

tz = timezone(timedelta(hours=8))
req_id = f"{agent}-{datetime.now(tz).strftime('%Y%m%d-%H%M%S')}"

conn = sqlite3.connect('.claude/orchestrator/orchestrator.db')
cur = conn.cursor()
cur.execute("""
    INSERT INTO requests (id, agent, status, reason, scope_json, plan_json,
                          self_review_json, constraints_json)
    VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)
""", (req_id, agent, reason,
      json.dumps(scope, ensure_ascii=False),
      json.dumps(plan, ensure_ascii=False),
      json.dumps(self_review, ensure_ascii=False),
      json.dumps(constraints, ensure_ascii=False)))
conn.commit()
conn.close()
```

### Request JSON

```json
{
  "request_id": "{agent}-{YYYYMMDD}-{HHMMSS}",
  "agent": "engine-agent",
  "reason": "一句话原因",
  "scope": {
    "modules": ["拆分-打包/", "service/"],
    "files": ["文件相对路径"],
    "excluded": ["排除的文件"]
  },
  "plan": {
    "summary": "修改方案概述",
    "steps": ["第1步：文件+行号+改动内容"],
    "breaking_changes": false,
    "affects_contract": ["影响的 API 字段"]
  },
  "self_review": {
    "potential_issues": ["预判问题 — 为什么安全"]
  },
  "constraints_self_declared": ["不新建函数", "不改某文件", "仅追加 N 行"]
}
```

**`self_review.potential_issues` 强制：每个预判问题必须附分析。**

## Step 4: 监视审批（SQL 增量轮询）

```python
import sqlite3, time

conn = sqlite3.connect('.claude/orchestrator/orchestrator.db')
last_check = ''
while True:
    cur = conn.cursor()
    cur.execute(
        "SELECT status, updated_at FROM requests WHERE id=? AND updated_at > ?",
        (req_id, last_check)
    )
    row = cur.fetchone()
    if row:
        print(f"APPROVAL_STATUS: {row[0]}")
        break
    time.sleep(2)
```

WAL 模式下写不阻塞读——即使 Orchestrator 正在 UPDATE 也能即时读到新状态。

**收到结果：**
- `approved` / `approved-with-warning` → 读 `approvals` 表，继续修改
- `rejected` → 停止工作，汇报 rejection_reason
- 锁过期未审批 → 报告用户

## Step 8: 提交 Completion

```python
cur.execute("""INSERT INTO completions (request_id, agent, completed_at,
    self_review_json, commits_json, sync_notes, context_updates_json)
    VALUES (?,?,?,?,?,?,?)""",
    (req_id, agent, datetime.now(tz).isoformat(),
     json.dumps(self_review, ensure_ascii=False),
     json.dumps(commits), sync_notes,
     json.dumps(context_updates, ensure_ascii=False)))
conn.commit()
```

### Completion JSON

```json
{
  "request_id": "同上", "agent": "同上", "completed_at": "ISO 8601",
  "self_review": {
    "all_steps_completed": true,
    "files_modified": ["实际改的文件"],
    "files_not_in_scope": [],
    "new_functions_created": [],
    "new_parameters_created": [],
    "breaking_changes": [],
    "constraints_violated": [],
    "engine_files_touched": []
  },
  "commits": ["commit hash"],
  "sync_notes": "签名/合约变更、给下一个 Agent 的信息",
  "context_updates": { "pipeline": "", "api_contract": "", "meta_fields": "" }
}
```

## Step 9: 释放锁

```python
cur.execute("""UPDATE lock SET state='idle', holder=NULL, request_id=NULL,
                scope_json=NULL, acquired_at=NULL, expires_at=NULL WHERE id=1""")
conn.commit()
```

## 模块边界

| Agent | Can touch | Forbidden |
|-------|-----------|-----------|
| `engine-agent` | `拆分-打包/`, `*.py` root engine | `app/`, `service/` |
| `service-agent` | `service/` | `app/`, engine `*.py` |
| `ui-agent` | `app/` | `service/`, engine `*.py` |

---

## 持久记忆：跨会话任务恢复

通过 `mcp-simple-memory` MCP 工具保存任务状态。

**安装：** `npx mcp-simple-memory init`（一次）

### Step 0: 恢复上下文

```
Tool: mem_search
  query: "{agent} pending tasks"
  tag: "pending"
  project: "orchestrator-demo"
```

返回 pending 任务 → 提取 `req:` tag 中的 `req_id` → 查 `orchestrator.db`：
- `approved` → 继续修改
- 仍 `pending` → 继续等待
- `rejected` → 汇报用户

### 提交时保存

```
Tool: mem_save
  text: "{agent} 提交 {req_id}: {reason}。scope: {scope}"
  type: "task"
  tags: [{agent}, "pending", "req:{req_id}"]
  project: "orchestrator-demo"
→ 保存返回的 entry_id
```

### 完成时更新

```
Tool: mem_update
  id: {entry_id}
  text: "{agent} 完成 {req_id}。commits: {commits}"
  tags: [{agent}, "completed", "req:{req_id}"]
```
