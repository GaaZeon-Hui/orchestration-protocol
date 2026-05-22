# Orchestration Protocol — Claude Code 多 Agent 编排系统

基于 SQLite 的 pipeline 状态机编排协议。**Orchestrator**（编排者）和 **Worker**（工作者）通过 `pipeline_state` 单表 + `transition_stage()` CAS 推进完成请求审批、修改、验证的完整闭环。

## 架构

```
┌─────────────────────────┐         ┌─────────────────────────┐
│     Orchestrator        │         │     Worker Agent        │
│                         │  SQLite │                         │
│ • 哨兵检查              │◄───────►│ • init_pipeline         │
│ • 冲突/越界/逻辑分析    │   WAL   │ • 轮询等待审批          │
│ • transition_stage()    │         │ • transition_stage()    │
│   审批/reject           │         │   推进 modifying → done │
│ • 验证 completion       │         │ • 自审 + completion    │
│ • 心跳维护              │         │ • 跨会话崩溃恢复       │
└──────────┬──────────────┘         └───────────┬─────────────┘
           │                                    │
           │        pipeline.py                 │
           │   ┌──────────────────┐             │
           └──►│ transition_stage │◄────────────┘
               │ VALID_TRANSITIONS│
               │ ROLE_PERMISSIONS │
               │ + audit_log      │
               └──────────────────┘
```

## 核心设计

| 决策 | 理由 |
|------|------|
| `pipeline_state` 单表替代 5 表 | 一表承载完整生命周期，无需 JOIN |
| `pipeline.py` 独立模块 | `transition_stage()` 脱离 Orchestrator 类，Worker 也可调用 |
| `lint.py` 程序化预检 | 越界 glob match（阻塞级）+ 冲突 git diff 交集 + AST 变更提取（信息级），不过的直接 reject |
| `transition_stage()` CAS | `WHERE stage=? AND revision=?` 保证并发安全 |
| `ROLE_PERMISSIONS` 权限矩阵 | Python 层拦截越权推进，role 只能从授权 stage 发起转移 |
| `audit_log` 自动审计 | 每次 stage 转移追加一行，payload_json 仅存变更列，不可跳过 |
| SQL trigger `tr_stage_transition` | DB 层兜底校验转移合法性+CAS |
| WAL 模式 | 读不阻塞写，多个 Worker 可同时读写不同行 |
| 心跳自动接任 | 编排者死亡 90s 内自动检测，下个 agent 接替 |
| `recover_pipeline()` | 崩溃后按断点 stage 续跑，无需 MCP 外部记忆 |
| 分析中间态持久化 | `conflict/boundary/logic_analysis_json` 列保存审查结论，崩溃恢复不重做 |
| `init_pipeline()` 同 agent 互斥 | 同 agent 有活跃 pipeline 时禁止创建新的，防止并发冲突 |

## 安装

```bash
git clone https://github.com/GaaZeon-Hui/orchestration-protocol.git
cd orchestration-protocol
```

仅依赖 Python 3 标准库（`sqlite3`）。

## 使用

在此目录下启动 Claude Code 会话。Agent 读取 `CLAUDE.md` → 导入 `orchestrator.py` → 自动执行角色注册。

## 文件结构

```
├── pipeline.py                     # 独立协议模块 — transition_stage() + 常量
├── orchestrator.py                 # Python 库 — 查询 + 角色注册 + heartbeat
├── lint.py                         # 程序化预检 — 越界检查 + 冲突检测 + AST 提取
├── test_pipeline.py                # pipeline.py 单元测试
├── test_orchestrator.py            # orchestrator.py 集成测试
├── test_lint.py                    # lint.py 单元测试
├── CLAUDE.md                       # 启动入口
├── README.md
├── README_EN.md
├── 审查报告.md                     # 架构审查与问题追踪
├── .gitignore
├── .claude/
│   ├── settings.json               # 插件配置（superpowers）
│   └── skills/
│       ├── orchestration-protocol/
│       │   └── SKILL.md            # 角色注册 + Schema + 权限矩阵
│       ├── orchestrator-role/
│       │   └── SKILL.md            # 编排者完整指令
│       └── worker-role/
│           └── SKILL.md            # 工作者完整指令
```

## 工作流程

### Worker（pipeline stage 驱动）

```
Step 0  配置边界     → orc.get_boundaries() / orc.set_boundaries()
Step 1  恢复上下文   → orc.get_pending_requests() / orc.recover_pipeline()
Step 2  Pull + 读   → git pull, get_pipeline(), get_boundaries()
Step 3  冲突检查     → orc.get_pending_requests(agent) 确认无活跃 pipeline
Step 4  创建 pipeline → orc.init_pipeline()
Step 5  等待审批     → get_pipeline() 单次检查，未审批则 /loop 复invoke
Step 6  读审批       → get_pipeline() → granted_scope_json
Step 7  修改         → transition_stage(approved→modifying, role='worker')
                    → 仅改授权文件
Step 8  自审         → 对照 boundaries + lint 检查
                    → transition_stage(modifying→self_review_done, self_review_json=...)
Step 9  Completion   → transition_stage(self_review_done→completion_submitted, ...)
        + 释锁       → 等 completed → transition_stage(completed→lock_released)
```

### Orchestrator

```
1. orc.init_db() + orc.migrate()
2. orc.get_boundaries()
3. 哨兵检查 git log/diff
4. 单次检查 orc.check_and_heartbeat(id) — 心跳 + 扫描新请求/completion
   → 无事项时建议 /loop 60s 持续监控
5. Lint 预检（越界→直接 reject；冲突+AST hints 喂给 LLM）
6. 三项分析 → transition_stage(role='orchestrator') 逐级推进，每步保存分析 JSON
7. 审批 → transition_stage(logic_analysis_done → approved/rejected)
8. 验证 → 对照 git diff + lint hints
9. 完成 → transition_stage(completion_submitted → completed)
```

## 权限矩阵（`pipeline.ROLE_PERMISSIONS`）

| Role | 可发起 stage |
|------|-------------|
| **orchestrator** | `request_submitted`, `conflict_analysis_done`, `boundary_analysis_done`, `logic_analysis_done`, `completion_submitted` |
| **worker** | `approved`, `modifying`, `self_review_done`, `completed` |

## 模块边界（用户配置）

首次启动时 Worker 提示用户设定各 agent 边界，存储在 `context.boundaries_json`。

```json
{
  "py-agent": { "can_touch": ["*.py"], "forbidden": ["*.md", "app/", "service/"] },
  "service-agent": { "can_touch": ["service/"], "forbidden": ["app/", "*.py", "*.md"] },
  "ui-agent": { "can_touch": ["app/"], "forbidden": ["service/", "*.py", "*.md"] },
  "md-agent": { "can_touch": ["*.md"], "forbidden": ["*.py", "app/", "service/"] }
}
```

## 测试

```bash
python3 -m pytest test_pipeline.py -v       # transition_stage + 权限 + CAS + 分析持久化
python3 -m pytest test_orchestrator.py -v   # 注册/心跳/全流程/崩溃恢复/并发互斥
python3 -m pytest test_lint.py -v           # 越界检查/冲突检测/AST 解析/集成
python3 -m pytest test_pipeline.py test_orchestrator.py test_lint.py -v  # 全部 74 测试
```
