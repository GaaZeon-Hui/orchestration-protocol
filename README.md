# Orchestration Protocol — Claude Code 多 Agent 编排系统

基于 SQLite 的分布式编排协议。**Orchestrator**（编排者）和 **Worker**（工作者）通过单个数据库文件 `orchestrator.db` 完成请求审批、锁管理、任务追踪的完整闭环。

## 架构

```
┌─────────────────────┐         ┌─────────────────────┐
│   Orchestrator      │         │   Worker Agent      │
│                     │  SQLite │                     │
│ • 哨兵检查          │◄───────►│ • 提交请求 + 自审   │
│ • 冲突/越界/逻辑    │   WAL   │ • 增量轮询等待审批  │
│   三项分析          │         │ • 仅改授权文件      │
│ • 事务签发锁        │         │ • 提交 completion   │
│ • 逐条验证 completion│        │ • 跨会话任务恢复    │
│ • 心跳维护          │         │                     │
└─────────────────────┘         └─────────────────────┘
```

## 核心设计

| 决策 | 理由 |
|------|------|
| SQLite 替代 JSON 文件 | ACID 事务、行级锁自动序列化、`WHERE updated_at > ?` 增量查询 |
| WAL 模式 | 读不阻塞写，多个 Worker 可同时读写不同行 |
| lock 表单行设计 | 任务锁 + 角色注册同一条 UPDATE，抢注失败自动降级 |
| 心跳自动接任 | 编排者死亡 90s 内自动检测，下个 agent 接替 |
| 全状态单文件 | 请求、审批、completion、锁、上下文全部在 `orchestrator.db` |

## 安装

```bash
git clone https://github.com/GaaZeon-Hui/orchestration-protocol.git
cd orchestration-protocol
```

仅依赖 Python 3 标准库（`sqlite3`）。可选增强：

```bash
npx mcp-simple-memory init   # 跨会话持久记忆
npx ccrecall sync             # 会话转录分析
```

## 使用

在此目录下启动 Claude Code 会话。Agent 启动时自动读取 `CLAUDE.md` 并执行角色注册——无需手动输入任何命令。

**角色自动分配：**
1. 第一个启动的 agent 注册为 **Orchestrator**
2. 后续启动的 agent 注册为 **Worker**
3. Orchestrator 退出超过 90 秒后，下一个 Worker 自动接任

## 文件结构

```
├── CLAUDE.md                      # 启动入口（自动角色检测）
├── orchestration-protocol.md      # 角色注册 + 完整 DB Schema
├── orchestrator-role.md           # 编排者完整指令
├── worker-role.md                 # 工作者完整指令
├── .claude/skills/                # Claude Code skill 文件
│   ├── orchestration-protocol.md
│   ├── orchestrator-role.md
│   └── worker-role.md
├── .gitignore
└── README.md
```

## 工作流程

### Worker（工作者）

```
Step 0  恢复记忆 → 查上次未完成任务
Step 1  Pull + 读上下文
Step 2  检查锁（必须 idle）
Step 3  提交请求 INSERT INTO requests + mem_save
Step 4  增量轮询 SELECT ... WHERE updated_at > ? → 等审批
Step 5  读审批结果 → 获取授权 scope
Step 6  仅修改授权文件
Step 7  自审越界和逻辑变更
Step 8  提交 completion + mem_update
Step 9  释放锁 UPDATE lock SET state='idle'
```

### Orchestrator（编排者）

```
启动    初始化 DB + 哨兵检查 git log/diff
监听    增量查询新请求和 completion + 每 30s 心跳
审批    三项分析（冲突/越界/逻辑变更）→ 事务签发锁
验证    逐条对照 git diff 验证 completion → 释锁
记录    持久记忆保存决策 + ccrecall 审计
```

## 模块边界

| Agent | 可修改 | 禁止 |
|-------|--------|------|
| `engine-agent` | `拆分-打包/`, engine `*.py` | `app/`, `service/` |
| `service-agent` | `service/` | `app/`, engine `*.py` |
| `ui-agent` | `app/` | `service/`, engine `*.py` |
