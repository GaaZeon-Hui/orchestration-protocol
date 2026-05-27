# Orchestration Protocol — 轻量多 Agent 编排协议

纯 Python 标准库实现，零外部依赖。基于 SQLite 状态机 + CAS 乐观锁的三角色协作编排工具，专为 Claude Code 设计。

**Worker** 发起修改请求 → **Orchestrator** 准入审批 → **Reviewer** 独立验证 commit 是否符合计划 → **Orchestrator** 仲裁 → 释锁闭环。`review_round` 计数器支持最多 4 轮修正回路，超出自动触发人工介入机制。

## 架构

```
  Worker ── reason + plan ──▶ Orchestrator (gate)
                                  │ Lint 过
  Worker ◀── approved ───────────┘
    │ 修改代码
    ▼
  Reviewer ── completion ──▶ Orchestrator (arbiter)
                                  │
              ┌───────────────────┤
              ▼                   ▼
          verified          worker_modify (修正回路)
              │                   │
              ▼                   └──▶ Reviewer ──▶ Orch ──▶ ...
          lock_released
```

## 核心设计

| 维度 | 机制 |
|------|------|
| **Stage** | 7 个命名 stage（init → orch_gate → worker_modify → reviewer_check → orch_arbiter → verified → lock_released） |
| **修正回路** | `review_round` 计数器，4 轮上限，超限触发人工介入 |
| **CAS** | `WHERE stage=? AND revision=?` 原子推进，rowcount=0 即并发冲突 |
| **权限** | `ROLE_PERMISSIONS` 矩阵——Worker/Orch/Reviewer 各有专属 stage |
| **白名单** | `ALLOWED_COLUMNS` 静默过滤非法字段 |
| **Trigger** | `tr_stage_transition` DB 层兜底——裸 SQL 也无法非法转移 |
| **WAL** | 写不阻塞读，多 Agent 并行 `/loop` 轮询 |
| **审计** | `audit_log` 每次转移追加不可变记录 |
| **Lint** | 三模块——`lint_gate`（Gate 轻量）· `lint_full`（Reviewer 完整）· `lint_crossref`（三方交叉验证） |
| **人工介入** | `human_intervention` 列，Orch 标记后 status.py 面板标红 |

## 安装

```bash
git clone https://github.com/GaaZeon-Hui/orchestration-protocol.git
cd orchestration-protocol
```

仅需 Python 3（`sqlite3` 标准库）。

## 使用

在此目录下启动 Claude Code。Agent 读取 `CLAUDE.md` → 注册角色 → 按 SKILL.md 指令执行。

## 文件结构

```
pipeline.py           — 状态机核心 · transition_stage()
orchestrator.py       — DB 层 · CRUD · 心跳 · 孤儿锁
lint_core.py          — 越界 glob 匹配 (共用)
lint_gate.py          — plan 校验 · orchestrator_gate 用
lint_full.py          — AST + 冲突 + lint_crossref · reviewer_check 用
status.py             — 终端监控面板
test_*.py             — 90 个单元/集成测试
.claude/skills/       — 3 角色 LLM 行为指令
docs/                 — 架构文档 · HTML 分析 · 实施计划
archive/v1/           — 旧版设计笔记
```

## 权限矩阵

| Role | 可推进的 from_stage |
|------|-------------------|
| **Worker** | `init` · `worker_modify` · `verified` |
| **Orchestrator** | `init` · `orchestrator_gate` · `orchestrator_arbiter` · `verified` · `worker_modify` |
| **Reviewer** | `reviewer_check` |

## 测试

```bash
python -m pytest test_pipeline.py test_orchestrator.py test_lint.py -v
# 90 passed in ~2s
```

## License

MIT License.
