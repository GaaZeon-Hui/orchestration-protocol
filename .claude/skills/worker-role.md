---
name: worker-role
description: Worker Agent role. Uses orchestrator.py for all DB operations. Handles request submission, approval monitoring, modifications, self-review, completion, and cross-session task recovery.
---

# Worker Agent Role

> 此后每次启动直接加载本文件，不再读 `orchestration-protocol` 入口。

**所有数据库操作通过 `orchestrator.py` 中的 `Orchestrator` 类完成。** 导入：`from orchestrator import Orchestrator; orc = Orchestrator()`

## Quick Reference

| Step | What | 调用 |
|------|------|------|
| 0. Configure | 检查模块边界，未配置则要求用户设定 | `orc.get_boundaries()` → `orc.set_boundaries()` |
| 1. Restore | `mem_search` 查未完成任务 | MCP 工具 |
| 2. Pull + Read | git pull, lock state, boundaries | `orc.check_lock()` / `orc.get_boundaries()` |
| 3. Check lock | 锁必须 idle | `orc.check_lock()` |
| 4. Request | 提交请求 + mem_save | `orc.submit_request(...)` |
| 5. Monitor | 轮询等待审批 | `orc.wait_for_approval(req_id)` |
| 6. Read approval | 查审批结果 | `orc.get_approval(req_id)` |
| 7. Modify | 仅修改授权文件，对照模块边界 | — |
| 8. Self-review | 自审越界和逻辑变更 | — |
| 9. Completion | 提交 completion + mem_update | `orc.submit_completion(...)` |
| 10. Release | 释放锁 | `orc.release_lock()` |

## Step 0: 配置模块边界

**首次启动必须执行，后续每次确认。**

调用 `orc.get_boundaries()` → 返回 `dict` 或 `None`。

若 `None`：要求用户为 `engine-agent`、`service-agent`、`ui-agent` 分别指定可修改和禁止的目录。

配置格式（写入 `context.boundaries_json`）：
```json
{
  "engine-agent": { "can_touch": ["core/"], "forbidden": ["app/", "service/"] },
  "service-agent": { "can_touch": ["service/"], "forbidden": ["app/", "*.py"] },
  "ui-agent": { "can_touch": ["app/"], "forbidden": ["service/", "*.py"] }
}
```

保存：`orc.set_boundaries(boundaries)`

若已有值：显示当前配置，询问用户是否需要修改。修改和自审时必须对照此配置。

## Step 1: 恢复上下文

```
Tool: mem_search
  query: "{agent} pending tasks"
  tag: "pending"
  project: "orchestrator-demo"
```

有 pending 任务 → 提取 `req:` tag → 查 `orchestrator.db` 的 status。

## Step 2: 检查锁

调用 `orc.check_lock()` → 返回锁信息 dict 或 `None`。

若锁被占用（`state == "locked"`），汇报持有者和过期时间，等待。

## Step 3: 提交请求

调用 `orc.submit_request(agent, reason, scope, plan, self_review, constraints, tz)` → 返回 `req_id`。

Agent types: `engine-agent` | `service-agent` | `ui-agent`

scope 格式：`{"modules": [...], "files": [...], "excluded": [...]}`
plan 格式：`{"summary": "...", "steps": [...], "breaking_changes": bool, "affects_contract": [...]}`
self_review 格式：`{"potential_issues": ["预判问题 — 为什么安全"]}`（**强制**：每个预判问题必须附分析）
constraints 格式：`["不新建函数", "不改某文件", "仅追加 N 行"]`

提交后通过 MCP 工具保存任务：
```
Tool: mem_save
  text: "{agent} 提交 {req_id}: {reason}。scope: {scope}"
  type: "task"
  tags: [{agent}, "pending", "req:{req_id}"]
  project: "orchestrator-demo"
```
→ 保存返回的 entry_id 供完成后更新

## Step 4: 监视审批

调用 `orc.wait_for_approval(req_id, poll_interval=2)` → 阻塞直到 status ≠ "pending"，返回最终状态。

WAL 模式下 Orchestrator 的 UPDATE 不会阻塞 Worker 的 SELECT。

**收到结果：**
- `approved` / `approved-with-warning` → 调用 `orc.get_approval(req_id)` 获取授权 scope
- `rejected` → 停止工作，汇报 rejection_reason
- 锁过期未审批 → 报告用户

## Step 5-7: 执行修改 + 自审

仅修改授权 scope 中的文件。自审越界（对照 `orc.get_boundaries()`）和逻辑变更。

## Step 8: 提交 Completion

调用 `orc.submit_completion(req_id, agent, completed_at, self_review, commits, sync_notes, context_updates)`

self_review 格式：
```json
{
  "all_steps_completed": true, "files_modified": [...],
  "files_not_in_scope": [], "new_functions_created": [],
  "breaking_changes": [], "constraints_violated": [], "engine_files_touched": []
}
```

完成后更新记忆：
```
Tool: mem_update
  id: {entry_id}
  text: "{agent} 完成 {req_id}。commits: {commits}"
  tags: [{agent}, "completed", "req:{req_id}"]
```

## Step 9: 释放锁

调用 `orc.release_lock()` → lock 表 state 重置为 idle。

---

## 持久记忆工具

通过 `mcp-simple-memory` MCP 工具保存任务状态。安装：`npx mcp-simple-memory init`（一次）

| type | 用途 |
|------|------|
| `task` | 任务生命周期（pending → completed） |
