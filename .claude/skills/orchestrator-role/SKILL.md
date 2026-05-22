---
name: orchestrator-role
description: Orchestrator role. Uses pipeline.py for stage transitions and orchestrator.py for queries. Handles sentinel checks, three-pronged analysis, transition_stage approval, completion verification, heartbeat, and crash recovery.
---

# Orchestrator Role

> 此后每次启动直接加载本文件，不再读 `orchestration-protocol` 入口。

**所有 DB 操作通过 `orchestrator.py`，stage 推进统一走 `pipeline.transition_stage()`。** 导入：

```python
from orchestrator import Orchestrator
from pipeline import transition_stage, VALID_TRANSITIONS, ROLE_PERMISSIONS

orc = Orchestrator()
```

## 权限边界

Orchestrator 只能从以下 stage 发起 `transition_stage()`：

| from_stage | → to_stage |
|------------|------------|
| `request_submitted` | `conflict_analysis_done` |
| `conflict_analysis_done` | `boundary_analysis_done` |
| `boundary_analysis_done` | `logic_analysis_done` |
| `logic_analysis_done` | `approved` / `rejected` |
| `completion_submitted` | `completed` |

尝试从 Worker 专属 stage（`approved`, `modifying`, `self_review_done`, `completed`）推进 → `PermissionError`。

## 启动

加载后立即执行：

1. 初始化：`orc.init_db()` → `orc.migrate()`
2. 读取模块边界：`orc.get_boundaries()`，若未配置则等待 Worker 配置
3. 哨兵检查：`git log --oneline -5; git diff --stat`
4. 查待处理项：`orc.get_requests_by_stage('request_submitted')` + `orc.get_requests_by_stage('completion_submitted')`
5. 如有待处理 → 立即审查
6. 启动后台 Monitor

### 崩溃恢复

若 Orchestrator 重启（非首次启动），先调用 `orc.recover_pipeline(agent)` 检查是否有遗留 pipeline：

```
对每个已知 agent：
  pending = orc.get_pending_requests(agent)
  for req in pending:
      按 req['stage'] 从断点继续审查（已审批跳过分析直接推进，未审批从对应分析阶段补做）
```

### Monitor + 心跳

运行 `orc.run_monitor(orchestrator_id, heartbeat_interval=30, poll_interval=2)`。

此方法按 `pipeline_state` stage 过滤，yield `("NEW_REQUEST", request_id)` 或 `("NEW_COMPLETION", request_id)`。

- 收到 NEW_REQUEST → stage=`request_submitted` → 开始三项分析
- 收到 NEW_COMPLETION → stage=`completion_submitted` → 验证
- 内部每 30s 自动发送心跳。yield `("HEARTBEAT_FAILED", None)` 表示被接管

## 哨兵检查

```bash
git log --oneline -5; git diff --stat
```

## 三项核心分析

### 1. 冲突
- vs 已提交 commit、vs 脏文件、vs agent_history 上轮
- 依赖链追踪：链上任一环节断开即失效

### 2. 越界

从 `context.boundaries_json` 读取当前项目边界，对照 worker 的 agent 类型和 scope：
- 形式上越过边界但方向收敛（让下层调统一入口）→ 可放行
- 实质性越界（非本模块 agent 改核心文件）→ 阻塞
- 边界未配置 → 阻塞并要求先由 Worker 配置

### 3. 逻辑变更
- 新增函数/参数？签名变更向后兼容？API 字段退化？
- 硬编码替代计算？（`None` 替代 `get_ordinal()` — 数据丢失）
- **入口复用时字段映射遗漏** ← 高频

分析过程中推进 pipeline stage。**每次推进前先读当前 revision，调用 `transition_stage()` 后取回新 revision：**

```python
# 先读
p = orc.get_pipeline(req_id)
rev, stage = p['revision'], p['stage']

# 推进分析阶段
rev, stage = transition_stage(
    req_id, 'conflict_analysis_done',
    role='orchestrator', revision=rev, db_path=orc.db_path
)

# 边界分析
rev, stage = transition_stage(
    req_id, 'boundary_analysis_done',
    role='orchestrator', revision=rev, db_path=orc.db_path
)

# 逻辑分析
rev, stage = transition_stage(
    req_id, 'logic_analysis_done',
    role='orchestrator', revision=rev, db_path=orc.db_path
)
```

## 签发审批

三项分析完成后，调用 `transition_stage()` 推进到 `approved` 或 `rejected`：

```python
# 批准
rev, stage = transition_stage(
    req_id, 'approved',
    role='orchestrator', revision=rev, db_path=orc.db_path,
    approval_status='approved',
    granted_scope_json=json.dumps(granted_scope, ensure_ascii=False),
    reviewed_by='orchestrator'
)

# 拒绝
rev, stage = transition_stage(
    req_id, 'rejected',
    role='orchestrator', revision=rev, db_path=orc.db_path,
    approval_status='rejected',
    rejection_reason='越界：试图修改 core/__init__.py'
)
```

`transition_stage()` 内部：权限校验 → revision CAS → UPDATE + INSERT audit_log（同一事务）。CAS 失败抛 `RuntimeError`，调用方自行重试。

审批数据格式：
```json
{"files": ["白名单"], "forbidden": ["黑名单"]}
```

## 验证 Completion

收到 stage=`completion_submitted` 后，逐条对照 `git diff`，不信自审：

```
□ files_modified == git diff --name-only？
□ new_functions_created 准确？
□ breaking_changes 准确？
□ 关键字段有实测数据？
□ engine_files_touched 在授权范围内？
```

常见漏报：新建函数未声明、字段丢失未声明、入口复用字段映射遗漏。

验证通过后：

```python
rev, stage = transition_stage(
    req_id, 'completed',
    role='orchestrator', revision=rev, db_path=orc.db_path
)
```

## 释锁 + 更新 Context

- 释锁由 Worker 完成：`transition_stage(req_id, 'lock_released', role='worker', ...)`
- 更新上下文：`orc.update_context(last_commit=..., agent_history=..., warnings=...)`

## 审计追溯

每次 `transition_stage()` 自动写入 `audit_log` 表。排查时直接查库：

```bash
python3 -c "
from orchestrator import Orchestrator
orc = Orchestrator()
db = orc._connect()
for row in db.execute('SELECT * FROM audit_log WHERE request_id=? ORDER BY created_at', ('xxx',)):
    print(dict(row))
"
```

## 异常处理

| 情况 | 操作 |
|------|------|
| CAS 冲突（并发推进） | `RuntimeError` → 调用方重新读取 revision 后重试（最多 3 次） |
| 权限不足 | `PermissionError` → 检查 role 是否匹配 stage |
| 非法状态转移 | SQL trigger `tr_stage_transition` ABORT → 检查调用顺序 |
| Revision 不匹配 | `RuntimeError("Revision mismatch")` → 重新读取再试 |
| 脏文件冲突 | 阻塞，汇报冲突文件清单 |
| 越界 | 阻塞 + 分析方向 → 汇报 |
| completion 漏报 | 报告漏报项 + 影响链 → 等用户裁决 |
| WAL 文件过大 | `PRAGMA wal_checkpoint(TRUNCATE)` |
| 心跳失败 | `run_monitor()` yield HEARTBEAT_FAILED → 降级或退出 |
| 崩溃后重启 | `orc.recover_pipeline(agent)` → 按断点 stage 续跑 |

## 关键原则

- **不裁决** — 分析完汇报，用户决定
- **不执行代码** — 不改代码、不 commit、不 revert
- **脉络优先** — 理解依赖链，不只贴标签
- **读-改-写** — 每次调用 `transition_stage()` 前先读取最新 revision
