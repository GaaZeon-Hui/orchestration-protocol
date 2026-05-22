---
name: worker-role
description: Worker Agent role. Uses orchestrator.py for queries and pipeline.transition_stage() for stage progression. Handles pipeline init, approval monitoring, transition_stage progression, self-review, completion, and crash recovery.
---

# Worker Agent Role

> 此后每次启动直接加载本文件，不再读 `orchestration-protocol` 入口。

**查询用 `orchestrator.py`，stage 推进统一走 `pipeline.transition_stage()`。** 导入：

```python
from orchestrator import Orchestrator
from pipeline import transition_stage

orc = Orchestrator()
```

## 权限边界

Worker 只能从以下 stage 发起 `transition_stage()`：

| from_stage | → to_stage |
|------------|------------|
| `approved` | `modifying` |
| `modifying` | `self_review_done` |
| `self_review_done` | `completion_submitted` |
| `completed` | `lock_released` |

尝试从 Orchestrator 专属 stage（`request_submitted`, `conflict_analysis_done`, `boundary_analysis_done`, `logic_analysis_done`, `completion_submitted`）推进 → `PermissionError`。

## Quick Reference

| Step | What | 调用 |
|------|------|------|
| 0. Configure | 检查模块边界，未配置则要求用户设定 | `orc.get_boundaries()` → `orc.set_boundaries()` |
| 1. Restore | 查未完成 pipeline + 崩溃恢复 | `orc.get_pending_requests(agent)` / `orc.recover_pipeline(agent)` |
| 2. Pull + Read | git pull, 当前状态, 边界 | `orc.get_pipeline(req_id)` / `orc.get_boundaries()` |
| 3. Check | 确认同 agent 无 modifying 冲突 | `orc.get_pending_requests(agent)` |
| 4. Init | 创建 pipeline | `orc.init_pipeline(...)` |
| 5. Monitor | 轮询等待审批 | poll `orc.get_pipeline(req_id)` 直至 stage=approved/rejected |
| 6. Read approval | 查审批结果 | `orc.get_pipeline(req_id)` → `granted_scope_json` |
| 7. Modify | 仅修改授权文件，对照模块边界 | `transition_stage(req_id, 'modifying', role='worker', ...)` |
| 8. Self-review | 自审越界和逻辑变更 | → `transition_stage(req_id, 'self_review_done', role='worker', ...)` |
| 9a. Submit completion | 提交 completion，等 Orchestrator 验证 | `transition_stage(req_id, 'completion_submitted', role='worker', ...)` |
| 9b. Release lock | Orchestrator 验证通过后释锁 | `transition_stage(req_id, 'lock_released', role='worker', ...)` |

## Step 0: 配置模块边界

**首次启动必须执行，后续每次确认。**

调用 `orc.get_boundaries()` → 返回 `dict` 或 `None`。

若 `None`：要求用户为各 agent 分别指定可修改和禁止的目录。

配置格式（写入 `context.boundaries_json`）：
```json
{
  "py-agent": { "can_touch": ["*.py"], "forbidden": ["*.md", "app/", "service/"] },
  "service-agent": { "can_touch": ["service/"], "forbidden": ["app/", "*.py", "*.md"] },
  "ui-agent": { "can_touch": ["app/"], "forbidden": ["service/", "*.py", "*.md"] },
  "md-agent": { "can_touch": ["*.md"], "forbidden": ["*.py", "app/", "service/"] }
}
```

保存：`orc.set_boundaries(boundaries)`

若已有值：显示当前配置，询问用户是否需要修改。修改和自审时必须对照此配置。

## Step 1: 恢复上下文 + 崩溃恢复

```python
# 查未完成的 pipeline
pending = orc.get_pending_requests('md-agent')
# 或按断点恢复
pipeline = orc.recover_pipeline('md-agent')
```

有未完成 pipeline → 从 `stage` 断点续跑：
- `request_submitted` → 跳到 Step 5（等审批）
- `approved` → 跳到 Step 7（执行修改）
- `modifying` / `self_review_done` → 从对应 stage 继续
- `completion_submitted` / `completed` → 跳到 Step 9（释锁）

## Step 2: Pull + 读取状态

```bash
git pull
```

读取当前 pipeline 状态和边界：`orc.get_pipeline(req_id)` / `orc.get_boundaries()`

## Step 3: 检查冲突

调用 `orc.get_pending_requests(agent)` 确认同 agent 无 pipeline 处于 `modifying` 状态。

## Step 4: 初始化 Pipeline

```python
req_id = orc.init_pipeline(
    agent='md-agent',
    reason='变更原因',
    scope={'modules': [...], 'files': [...], 'excluded': [...]},
    plan={'summary': '...', 'steps': [...], 'breaking_changes': False, 'affects_contract': []},
    self_review={'potential_issues': ['预判问题 — 为什么安全']},
    constraints=['约束1', '约束2'],
    tz=None
)
```

Agent types: `engine-agent` | `service-agent` | `ui-agent` | `md-agent` | `py-agent`

## Step 5: 监视审批

```python
while True:
    p = orc.get_pipeline(req_id)
    if p['stage'] in ('approved', 'approved-with-warning'):
        break
    if p['stage'] == 'rejected':
        print(f"被拒: {p.get('rejection_reason')}")
        return
    time.sleep(2)
```

WAL 模式下 Orchestrator 的 `transition_stage()` 不会阻塞 Worker 的 `get_pipeline()` SELECT。

## Step 6: 读取审批详情

```python
p = orc.get_pipeline(req_id)
granted_scope = json.loads(p['granted_scope_json'])
rev = p['revision']
```

## Step 7: 执行修改

```python
rev, stage = transition_stage(
    req_id, 'modifying',
    role='worker', revision=rev, db_path=orc.db_path
)
```

仅修改授权 scope 中的文件。完成后：

```python
rev, stage = transition_stage(
    req_id, 'self_review_done',
    role='worker', revision=rev, db_path=orc.db_path
)
```

## Step 8: 自审

自审越界（对照 `orc.get_boundaries()`）和逻辑变更。

## Step 9: 提交 Completion + 释锁

```python
rev, stage = transition_stage(
    req_id, 'completion_submitted',
    role='worker', revision=rev, db_path=orc.db_path,
    self_review_json=json.dumps({
        'all_steps_completed': True, 'files_modified': [...],
        'files_not_in_scope': [], 'new_functions_created': [],
        'breaking_changes': [], 'constraints_violated': [], 'engine_files_touched': []
    }, ensure_ascii=False),
    commits_json=json.dumps([...], ensure_ascii=False),
    sync_notes='...',
    context_updates_json=json.dumps({...}, ensure_ascii=False)
)

# 等待 Orchestrator 验证 completion 后释锁
while True:
    p = orc.get_pipeline(req_id)
    if p['stage'] == 'completed':
        rev = p['revision']
        transition_stage(
            req_id, 'lock_released',
            role='worker', revision=rev, db_path=orc.db_path
        )
        break
    time.sleep(2)
```

self_review 格式：
```json
{
  "all_steps_completed": true, "files_modified": [...],
  "files_not_in_scope": [], "new_functions_created": [],
  "breaking_changes": [], "constraints_violated": [], "engine_files_touched": []
}
```

---

## 崩溃恢复

每次 Worker 启动时（Step 1），先调用 `orc.recover_pipeline(agent)` 检查断点：

| 断点 stage | 操作 |
|------------|------|
| `request_submitted` | 跳回 Step 5 等审批 |
| `approved` | 跳回 Step 7 执行修改 |
| `modifying` | 检查 git status，有未提交修改则继续，否则从 Step 7 重做 |
| `self_review_done` | 跳回 Step 8 重做自审，然后 Step 9 |
| `completion_submitted` | 跳回 Step 9 等 Orchestrator 验证 |
| `completed` | 直接 `transition_stage(req_id, 'lock_released', role='worker', ...)` |
| `lock_released` | 已完成，跳过 |
