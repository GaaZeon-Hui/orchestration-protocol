# Orchestration Protocol — Claude Code 多 Agent 编排系统

基于 SQLite 的分布式编排协议。**Orchestrator**（编排者）和 **Worker**（工作者）通过单个数据库文件 `orchestrator.db` 完成请求审批、锁管理、任务追踪的完整闭环。

## 架构

```
┌─────────────────────────┐         ┌─────────────────────────┐
│     Orchestrator        │         │     Worker Agent        │
│                         │  SQLite │                         │
│ • 哨兵检查              │◄───────►│ • 提交请求 + 自审       │
│ • 冲突/越界/逻辑分析    │   WAL   │ • 增量轮询等待审批      │
│ • 事务签发锁            │         │ • 仅改授权文件          │
│ • 逐条验证 completion   │         │ • 提交 completion       │
│ • 心跳维护              │         │ • 跨会话任务恢复        │
└─────────────────────────┘         └─────────────────────────┘
```

## 核心设计

| 决策 | 理由 |
|------|------|
| SQLite 替代 JSON 文件 | ACID 事务、行级锁自动序列化、`WHERE updated_at > ?` 增量查询 |
| WAL 模式 | 读不阻塞写，多个 Worker 可同时读写不同行 |
| lock 表单行设计 | 任务锁 + 角色注册同一条 UPDATE，抢注失败自动降级 |
| 心跳自动接任 | 编排者死亡 90s 内自动检测，下个 agent 接替 |
| 独立 Python 库 | `orchestrator.py` — 所有 DB 操作封装为 `Orchestrator` 类 |

## 安装

```bash
git clone https://github.com/GaaZeon-Hui/orchestration-protocol.git
cd orchestration-protocol
```

仅依赖 Python 3 标准库（`sqlite3`）。可选：`mcp-simple-memory`（跨会话记忆）、`ccrecall`（会话转录分析）。

## 使用

在此目录下启动 Claude Code 会话。Agent 读取 `CLAUDE.md` → 导入 `orchestrator.py` → 自动执行角色注册。

## 文件结构

```
├── orchestrator.py                # Python 库 — 所有 DB 操作
├── test_orchestrator.py           # 20 个测试
├── CLAUDE.md                      # 启动入口
├── README.md
├── .gitignore
└── .claude/skills/
    ├── orchestration-protocol.md  # 角色注册指引 + DB Schema
    ├── orchestrator-role.md       # 编排者完整指令
    └── worker-role.md             # 工作者完整指令
```

## 工作流程

### Worker
```
Step 0  配置边界 → orc.get_boundaries() / orc.set_boundaries()
Step 1  恢复记忆 → mem_search
Step 2  检查锁   → orc.check_lock()
Step 3  提交请求 → orc.submit_request() + mem_save
Step 4  等待审批 → orc.wait_for_approval(req_id)
Step 5  读审批   → orc.get_approval(req_id)
Step 6  执行修改
Step 7  自审
Step 8  提交完成 → orc.submit_completion() + mem_update
Step 9  释放锁   → orc.release_lock()
```

### Orchestrator
```
1. orc.init_db() + orc.migrate()
2. orc.get_boundaries()
3. 哨兵检查 git log/diff
4. 监听 orc.run_monitor(id) — 增量查询 + 心跳
5. 审批 → orc.issue_approval()
6. 验证 → orc.verify_completion()
7. 释锁 → orc.release_lock() + orc.update_context()
```

## 模块边界（用户配置）

首次启动时 Worker 提示用户设定各 agent 边界，存储在 `context.boundaries_json`。

```json
{
  "engine-agent": { "can_touch": ["core/"], "forbidden": ["app/", "service/"] },
  "service-agent": { "can_touch": ["service/"], "forbidden": ["app/", "*.py"] },
  "ui-agent": { "can_touch": ["app/"], "forbidden": ["service/", "*.py"] }
}
```

## 测试

```bash
python3 -m pytest test_orchestrator.py -v   # 20 tests, ~1s
```
