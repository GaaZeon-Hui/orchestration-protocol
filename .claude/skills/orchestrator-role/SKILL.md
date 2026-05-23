---
name: orchestrator-role
description: Orchestrator role. Uses pipeline.py for stage transitions and orchestrator.py for queries. Handles sentinel checks, three-pronged analysis, transition_stage approval, completion verification, heartbeat, and crash recovery.
---

# Orchestrator Role

> 此后每次启动直接加载本文件，不再读 `orchestration-protocol` 入口。

**所有 DB 操作通过 `orchestrator.py`，stage 推进统一走 `pipeline.transition_stage()`。** 导入：

```python
import json

from orchestrator import Orchestrator
from pipeline import transition_stage, VALID_TRANSITIONS, ROLE_PERMISSIONS
from lint import lint_changed_files

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
| `completed` | `lock_released`（孤儿锁接管） |

尝试从 Worker 专属 stage（`approved`, `modifying`, `self_review_done`, `completed`）推进 → `PermissionError`。

## 启动

加载后立即执行：

1. 初始化：`orc.init_db()` → `orc.migrate()`
2. **恢复工作现场**：`task = orc.get_current_task()` — 若有未完成任务，先处理完
3. 读取模块边界：`orc.get_boundaries()`，若未配置则等待 Worker 配置
4. **打开任务板**：建议用户在另一个终端运行 `python status.py` 持续监控 pipeline 状态
5. 哨兵检查：`git log --oneline -5; git diff --stat`
6. 查待处理项：`orc.get_requests_by_stage('request_submitted')` + `orc.get_requests_by_stage('completion_submitted')`
7. 如有待处理 → 立即审查（跑 lint → 三项分析 → 签发）
8. **自验证**：`python status.py --check-stuck` — 有 stuck 项说明自己漏了，立即补处理
9. 处理完毕后，输出当前无待处理项，建议 `/loop 60s` 持续监控

### 崩溃恢复

若 Orchestrator 重启（非首次启动），先调用 `orc.recover_pipeline(agent)` 检查是否有遗留 pipeline：

```
对每个已知 agent：
  pending = orc.get_pending_requests(agent)
  for req in pending:
      读取 req 中的 *_analysis_json 字段
      缺失某阶段分析 → 补做该阶段
      已保存 → 跳过，从下一阶段继续
      按 req['stage'] 从断点继续审查
```

### 持续监控 + 心跳（替代 `run_monitor()`）

`run_monitor()` 是阻塞 generator，Claude Code agent 无法在后台持续运行。改用 **单次检查 + `/loop` 定时复invoke**：

```python
result = orc.check_and_heartbeat(orchestrator_id)

if result['status'] == 'takeover':
    print("心跳失败：已被其他 orchestrator 接管，退出")
    return

# 孤儿锁检测 — 仅查 stage='completed' 超时行，轻量
orphans = orc.resolve_orphan_locks(timeout_seconds=120)
if orphans:
    print("释放了 {} 个孤儿锁: {}".format(len(orphans), orphans))

for item in result['items']:
    if item['type'] == 'new_request':
        orc.set_current_task(item['request_id'], 'review')
        # 拉取 pipeline，跑 lint → 三项分析 → 审批
        orc.clear_current_task()
    elif item['type'] == 'new_completion':
        orc.set_current_task(item['request_id'], 'verify')
        # 拉取 pipeline，验证 completion
        orc.clear_current_task()

# 自验证 — 查自己有没有漏掉的 completion_submitted
import subprocess, json
r = subprocess.run(
    ["python", "status.py", "--check-stuck", orc.db_path, "10"],
    capture_output=True, text=True
)
stuck = json.loads(r.stdout).get("stuck", [])
for s in stuck:
    # 漏了 — 立即补处理
    orc.set_current_task(s['request_id'], 'verify-stuck')
    # 验证 completion
    orc.clear_current_task()

if not result['items'] and not orphans and not stuck:
    print("无新事项")
```

- 每次 `/loop` 复invoke 时，从启动步骤 1 开始，`recover_pipeline()` + `get_requests_by_stage()` 定位到未完成的审查
- `check_and_heartbeat()` 内部自动调用 `send_heartbeat()` — **每次检查即心跳**
- `set_current_task()` / `clear_current_task()` — 崩溃恢复知道"我当时在处理什么"
- `--check-stuck` 只查 `stage='completion_submitted' AND updated_at < now-10min`，零开销
- `resolve_orphan_locks()` 只查 `stage='completed' AND updated_at < now-120s`，零开销
- 心跳超时 90s，`/loop 60s` 间隔足够在超时前刷新
- 返回 `status: takeover` 表示被接管，应退出

## 哨兵检查

```bash
git log --oneline -5; git diff --stat
```

## Lint 层（程序化预检 — 先于 LLM 分析）

**拿到 request 后，第一步先跑 lint**。Lint 做机械不可漏的检查，不过的直接 reject，不喂给 LLM。

```python
from lint import lint_changed_files

result = lint_changed_files(
    boundaries=orc.get_boundaries(),
    agent=p['agent'],
    base_ref='HEAD~5'
)
if result['blocked']:
    # 直接 reject，不消耗 LLM context
    transition_stage(req_id, 'rejected',
        role='orchestrator', revision=rev, db_path=orc.db_path,
        approval_status='rejected',
        rejection_reason=result['reason']
    )
    return  # 跳过三项分析
# 通过后 hints 喂给 LLM 辅助分析
hints = result['hints']
```

Lint 做了两件事：

| 层级 | 内容 | 机制 | 行为 |
|------|------|------|------|
| **阻塞级** | 越界检查 | `can_touch` / `forbidden` glob 匹配 | 命中 → 直接 reject |
| **信息级** | 文件冲突 | `git diff base_ref..HEAD` 文件交集 | 提供冲突清单给 LLM 判断 |
| **信息级** | AST 变更 | 函数签名、新增/删除符号、新类 | 结构化输入给逻辑分析 |

Lint 不过的条件：
- `boundaries` 未配置 → 阻塞
- `agent` 不在 boundaries 中 → 阻塞
- 任一文件命中 `forbidden` → 阻塞
- 任一文件不匹配 `can_touch` → 阻塞

---

## 三项核心分析

lint 通过后才进入 LLM 分析阶段。`hints` 已包含结构化的冲突清单和 AST 变更摘要，直接引用即可。

### 1. 冲突
- 对照 `hints.conflicts` 中 lint 检测出的文件级冲突
- vs 已提交 commit、vs 脏文件、vs agent_history 上轮
- 依赖链追踪：链上任一环节断开即失效

### 2. 越界（边缘 case 判断）

lint 已处理程序化越界。此阶段仅处理 lint 无法判断的边缘 case：
- 形式上越过边界但方向收敛（让下层调统一入口）→ 可放行
- lint 通过但语义上存在越界风险 → 标记 warning

### 3. 逻辑变更
- 对照 `hints.ast` 中的 `new_functions`、`signature_changes`、`deleted_symbols`
- 新增函数/参数？签名变更向后兼容？API 字段退化？
- 硬编码替代计算？（`None` 替代 `get_ordinal()` — 数据丢失）
- **入口复用时字段映射遗漏** ← 高频

分析过程中推进 pipeline stage。**每次推进前先读当前 revision，调用 `transition_stage()` 后取回新 revision。每步分析结论序列化为 JSON 存入对应字段，供崩溃恢复复用：**

```python
# 先读
p = orc.get_pipeline(req_id)
rev, stage = p['revision'], p['stage']

# 冲突分析 — 保存结论
rev, stage = transition_stage(
    req_id, 'conflict_analysis_done',
    role='orchestrator', revision=rev, db_path=orc.db_path,
    conflict_analysis_json=json.dumps({
        'conflicts_found': [...],
        'resolution': '...',
    }, ensure_ascii=False),
)

# 边界分析 — 保存结论
rev, stage = transition_stage(
    req_id, 'boundary_analysis_done',
    role='orchestrator', revision=rev, db_path=orc.db_path,
    boundary_analysis_json=json.dumps({
        'edge_cases': [...],
        'released': [...],
        'warnings': [...],
    }, ensure_ascii=False),
)

# 逻辑分析 — 保存结论
rev, stage = transition_stage(
    req_id, 'logic_analysis_done',
    role='orchestrator', revision=rev, db_path=orc.db_path,
    logic_analysis_json=json.dumps({
        'new_functions': [...],
        'signature_changes': [...],
        'deleted_symbols': [...],
        'breaking_changes': [...],
        'field_mapping_issues': [...],
    }, ensure_ascii=False),
)
```

崩溃恢复时读取这些字段，跳过已完成的分析阶段，直接续跑。若字段缺失（旧 pipeline 无此列），则从该阶段补做。

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

## 任务板

当用户说 **"看看任务板"** / **"看看状态"** / **"任务面板"** 时，直接执行：

```bash
python status.py --once
```

脚本输出即任务板内容，不用再总结。若用户说 **"打开任务板"**，提示在另一终端运行 `python status.py`。

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
| 心跳失败 | `check_and_heartbeat()` 返回 `status: takeover` → 退出 |
| 崩溃后重启 | `orc.recover_pipeline(agent)` → 按断点 stage 续跑 |

## 关键原则

- **不裁决** — 分析完汇报，用户决定
- **不执行代码** — 不改代码、不 commit、不 revert
- **脉络优先** — 理解依赖链，不只贴标签
- **读-改-写** — 每次调用 `transition_stage()` 前先读取最新 revision
