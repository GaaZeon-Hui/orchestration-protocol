---
name: worker-role
description: Worker Agent role for the orchestration protocol. Loaded after role registration. Handles submitting requests, monitoring approvals, executing modifications, self-review, and completion reporting.
---

# Worker Agent Role

> **此后每次启动直接加载本文件，不再读 `orchestration-protocol` 入口。**
> 入口仅用于首次角色注册和编排者更替场景。

---

## Quick Reference

| Step | What | Where |
|------|------|-------|
| 0. Restore context | `mem_search` 查上次未完成任务（见下方持久记忆章节） | mcp-simple-memory |
| 1. Pull + Read | git pull, CLAUDE.md, context, lock state | Root + orchestrator.db |
| 2. Check lock | `SELECT state FROM lock WHERE id=1` → must be `idle` | orchestrator.db |
| 3. Request | `INSERT INTO requests` + `mem_save` 保存任务（见下方持久记忆章节） | orchestrator.db + memory |
| 4. Monitor approval | 监听 DB 文件变化 → 查 `requests` 表 status 变更 | orchestrator.db |
| 5. Read approval | 查 `approvals` 表，读 granted_scope / forbidden | orchestrator.db |
| 6. Modify | 仅修改授权文件 | As authorized |
| 7. Self-review | 自审越界和逻辑变更，commit | — |
| 8. Completion | `INSERT INTO completions` + `mem_update` 标记完成（见下方持久记忆章节） | orchestrator.db + memory |
| 9. Release lock | `UPDATE lock SET state='idle'` | orchestrator.db |

## Step 2: 检查锁状态

```python
import sqlite3
conn = sqlite3.connect('.claude/orchestrator/orchestrator.db')
cur = conn.cursor()
cur.execute("SELECT state, holder, expires_at FROM lock WHERE id=1")
row = cur.fetchone()
if row and row[0] == 'locked':
    # 锁被占用 → 汇报给用户，等待
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
""", (
    req_id, agent, reason,
    json.dumps(scope, ensure_ascii=False),
    json.dumps(plan, ensure_ascii=False),
    json.dumps(self_review, ensure_ascii=False),
    json.dumps(constraints, ensure_ascii=False),
))
conn.commit()
conn.close()
```

### Request JSON 字段说明

```json
{
  "request_id": "{agent}-{YYYYMMDD}-{HHMMSS}",
  "agent": "engine-agent",
  "reason": "一句话原因",
  "scope": {
    "modules": ["拆分-打包/", "service/"],
    "files": ["每个文件的完整相对路径"],
    "excluded": ["明确排除的文件"]
  },
  "plan": {
    "summary": "修改方案概述",
    "steps": ["第1步：精确到文件+行号+改动内容"],
    "related_commits": ["相关 commit hash"],
    "breaking_changes": false,
    "affects_contract": ["影响的 API 合约字段"]
  },
  "self_review": {
    "potential_issues": [
      "预判问题 — 为什么它实际上安全"
    ]
  },
  "constraints_self_declared": [
    "不新建函数", "不改某文件", "仅追加 N 行"
  ]
}
```

**self_review.potential_issues 是强制的** — 每个预判问题必须附分析。

## Step 4: 提交后自动监视审批

**两种监听方式，按环境选用：**

### 方式 A：inotifywait（Linux，事件驱动，零轮询开销）

```bash
request_id="$1"
db=".claude/orchestrator/orchestrator.db"
inotifywait -m -e modify "$db" 2>/dev/null | while read; do
  status=$(python3 -c "
import sqlite3
conn = sqlite3.connect('$db')
cur = conn.cursor()
cur.execute('SELECT status FROM requests WHERE id=?', ('$request_id',))
row = cur.fetchone()
print(row[0] if row else 'pending')
")
  [ "$status" != "pending" ] && echo "APPROVAL_STATUS: $status" && break
done
```

### 方式 B：SQL 增量轮询（跨平台，WHERE updated_at 过滤）

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
    time.sleep(2)  # 仅2秒轮询，updated_at 索引保证轻量
```

> WAL 模式下写不阻塞读 — 即使 Orchestrator 正在 UPDATE，Worker 也能立即读到新状态。

**收到 APPROVAL_STATUS 事件时：**
- `approved` / `approved-with-warning` → 读 `approvals` 表，继续修改
- `rejected` → **停止工作**，汇报 rejection_reason，等待人工干预
- 锁过期仍未审批 → 报告用户，等待指令

## Step 8: 提交 Completion

```python
cur.execute("""
    INSERT INTO completions (request_id, agent, completed_at, self_review_json,
                             commits_json, sync_notes, context_updates_json)
    VALUES (?, ?, ?, ?, ?, ?, ?)
""", (
    req_id, agent, datetime.now(tz).isoformat(),
    json.dumps(self_review, ensure_ascii=False),
    json.dumps(commits),
    sync_notes,
    json.dumps(context_updates, ensure_ascii=False),
))
conn.commit()
```

### Completion JSON 字段说明

```json
{
  "request_id": "同上",
  "agent": "同上",
  "completed_at": "ISO 8601",
  "self_review": {
    "all_steps_completed": true,
    "files_modified": ["实际改的文件"],
    "files_not_in_scope": [],
    "new_functions_created": [],
    "new_parameters_created": [],
    "breaking_changes": [],
    "constraints_violated": [],
    "engine_files_touched": ["如有"]
  },
  "commits": ["commit hash"],
  "sync_notes": "签名变更、合约变更、给下一个 Agent 的信息",
  "context_updates": {
    "pipeline": "如有变更",
    "api_contract": "如有变更",
    "meta_fields": "如有变更"
  }
}
```

## Step 9: 释放锁

```python
cur.execute("""
    UPDATE lock SET state='idle', holder=NULL, request_id=NULL,
                    scope_json=NULL, acquired_at=NULL, expires_at=NULL
    WHERE id=1
""")
conn.commit()
```

## 模块边界

| Agent | Can touch | Forbidden |
|-------|-----------|-----------|
| `engine-agent` | `拆分-打包/`, `*.py` root engine | `app/`, `service/` (without approval) |
| `service-agent` | `service/` | `app/`, engine `*.py` |
| `ui-agent` | `app/` | `service/`, engine `*.py` |

---

## 持久记忆：跨会话任务恢复

`mcp-simple-memory` 提供跨会话的语义记忆，让 Worker 能记住自己的任务状态，中断后可以恢复。

### 安装

```bash
npx mcp-simple-memory init
# 重启 Claude Code，获得 7 个记忆工具：
# mem_save, mem_search, mem_get, mem_list, mem_update, mem_delete, mem_tags
```

### 提交请求时保存

提交到 `orchestrator.db` 后，通过 MCP 工具写入持久记忆（`mem_save` / `mem_search` / `mem_update` 是 Claude Code 调用 mcp-simple-memory 的工具，不是 Python 函数）：

```
Tool: mem_save
Args:
  text: "{agent} 提交了请求 {req_id}: {reason}。scope: {scope}。plan: {plan_summary}"
  type: "task"
  tags: [{agent}, "pending", "req:{req_id}"]
  project: "orchestrator-demo"

→ 保存返回的 entry_id，供完成后 mem_update 使用
```

### 会话中断后恢复（Step 0）

Agent 再次启动时，先查自己有没有未完成的任务（在 Step 1 Pull+Read 之前执行）：

```
Tool: mem_search
Args:
  query: "{agent} pending tasks"
  tag: "pending"
  project: "orchestrator-demo"
```

返回结果后：
- 有 pending 任务 → 提取 `req:` tag 中的 `req_id` → 查 `orchestrator.db` 中 requests 表的当前 status
  - `approved` → 继续执行修改
  - 仍 `pending` → 继续等待审批
  - `rejected` → 汇报用户
- 无 pending → 正常启动

### 任务完成后更新

```
Tool: mem_update
Args:
  id: {entry_id}             (mem_save 返回的 ID)
  text: "{agent} 完成了请求 {req_id}。commits: {commits}"
  tags: [{agent}, "completed", "req:{req_id}"]
```

### 记忆类型规范

| type | 用途 |
|------|------|
| `task` | 任务生命周期（pending → completed） |
