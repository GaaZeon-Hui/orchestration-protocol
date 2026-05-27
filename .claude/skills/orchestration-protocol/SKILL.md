---
name: orchestration-protocol
description: Entry point. Determines role via register table and redirects to role-specific file.
---

# Orchestration Protocol — Entry

| 角色 | 职责 | 角色文件 |
|------|------|---------|
| **Orchestrator** | Gate 准入 → 仲裁 → 人工介入协调 | `orchestrator-role` |
| **Worker** | 前置准备 → 声明计划 → 改码 → 修正回路 → 释锁 | `worker-role` |
| **Reviewer** | 验证 commit 是否符合 plan → 超时检测 → 文件复原 | `reviewer-role` |

## 架构

4 表：`pipeline_state` + `project` + `register` + `audit_log`。
`pipeline.py` 提供 `transition_stage()`（CAS + 权限校验 + 审计）。
`lint.py` 提供 `lint_plan()`（gate 用）和 `lint_changed_files()`（reviewer 用）。

## 角色注册

```python
from orchestrator import Orchestrator
orc = Orchestrator()
orc.init_db()
orc.migrate()
role = orc.try_register()
print(f"ROLE: {role}")
```

- `ROLE: orchestrator` → 读 `.claude/skills/orchestrator-role/SKILL.md`
- `ROLE: worker` → 读 `.claude/skills/worker-role/SKILL.md`
- `ROLE: reviewer` → 读 `.claude/skills/reviewer-role/SKILL.md`

## 状态转移

```
init → orchestrator_gate → worker_modify → reviewer_check → orchestrator_arbiter → verified → lock_released
                                                                                ↘ rejected
```

修正回路：`orchestrator_arbiter → worker_modify`（review_round += 1，最多 4 轮）。

## 权限矩阵

| Role | 可发起的 from_stage |
|------|---------------------|
| orchestrator | init, orchestrator_gate, orchestrator_arbiter, verified, worker_modify |
| worker | init, worker_modify, verified |
| reviewer | reviewer_check |
