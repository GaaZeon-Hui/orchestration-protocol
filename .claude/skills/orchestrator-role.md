---
name: orchestrator-role
description: Orchestrator role. Uses orchestrator.py for all DB operations. Handles sentinel checks, three-pronged analysis, lock issuance, completion verification, heartbeat, and persistent memory.
---

# Orchestrator Role

> 此后每次启动直接加载本文件，不再读 `SKILL.md` 入口。

**所有数据库操作通过 `orchestrator.py` 中的 `Orchestrator` 类完成。** 导入：`from orchestrator import Orchestrator; orc = Orchestrator()`

## 启动

加载后立即执行：

1. 初始化：`orc.init_db()` → `orc.migrate()`
2. 读取模块边界：`orc.get_boundaries()`，若未配置则等待 Worker 配置
3. 哨兵检查：`git log --oneline -5; git diff --stat`
4. 查待处理项：对 `requests` 和 `completions` 表执行去重查询（见 `orc.run_monitor()` 内部逻辑）
5. 如有待处理 → 立即审查
6. 启动后台 Monitor

### Monitor + 心跳

运行 `orc.run_monitor(orchestrator_id, heartbeat_interval=30, poll_interval=2)`。

此方法是一个生成器，每次 yield `("NEW_REQUEST", req_id)` 或 `("NEW_COMPLETION", req_id)`。

- 收到 NEW_REQUEST → 调用 `orc.is_processed(req_id, 'request')` 去重 → 未处理则审查
- 收到 NEW_COMPLETION → 调用 `orc.is_processed(req_id, 'completion')` 去重 → 未处理则验证
- 内部每 30s 自动发送心跳。yield `("HEARTBEAT_FAILED", None)` 表示被接管

### 去重

- 审查前：`orc.is_processed(req_id, 'request')` → 如 True 则跳过
- 审查后：`orc.mark_processed(req_id, 'request', decision)`

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

## 签发锁

调用 `orc.issue_approval(agent, req_id, granted_scope, decision, rejection_reason, tz)`。

此方法在事务内完成四步：获取锁 → 更新请求状态 → 插入审批记录 → 记录已处理。中途崩溃全回滚。

decision 取值：`approved` | `approved-with-warning` | `rejected`

审批数据格式：
```json
{"files": ["白名单"], "forbidden": ["黑名单"]}
```

## 验证 Completion

逐条对照 `git diff`，不信自审：

```
□ files_modified == git diff --name-only？
□ new_functions_created 准确？
□ breaking_changes 准确？
□ 关键字段有实测数据？
□ engine_files_touched 在授权范围内？
```

常见漏报：新建函数未声明、字段丢失未声明、入口复用字段映射遗漏。

验证通过后：`orc.verify_completion(req_id)` → 记录到 processed 表。

## 释锁 + 更新 Context

- 释锁：`orc.release_lock()`
- 更新上下文：`orc.update_context(last_commit=..., agent_history=..., warnings=...)`

## 异常处理

| 情况 | 操作 |
|------|------|
| 锁超时 | 调用 `orc.release_lock()` 强制释放，通知 Agent 重申请 |
| 脏文件冲突 | 阻塞，汇报冲突文件清单 |
| 越界 | 阻塞 + 分析方向 → 汇报 |
| completion 漏报 | 报告漏报项 + 影响链 → 等用户裁决 |
| 两请求同时 | SQLite 行级锁序列化，`issue_approval()` 会抛出 RuntimeError |
| 重复请求 | `orc.is_processed()` 返回 True → 跳过 |
| WAL 文件过大 | `PRAGMA wal_checkpoint(TRUNCATE)` |
| 心跳失败 | `run_monitor()` yield HEARTBEAT_FAILED → 降级或退出 |

## 关键原则

- **不裁决** — 分析完汇报，用户决定
- **不执行代码** — 不改代码、不 commit、不 revert
- **脉络优先** — 理解依赖链，不只贴标签
- **事务包围** — `issue_approval()` 内部已保证，其他多表写入同样需要事务

---

## 持久记忆：审批决策

通过 `mcp-simple-memory` MCP 工具保存跨会话决策记录。安装：`npx mcp-simple-memory init`（一次）

### 审批后记录

```
Tool: mem_save
  text: "审批 {req_id}: {decision}。{conflict_summary}。{boundary_summary}。granted: {granted_scope}"
  type: "decision"
  tags: ["orchestrator", {decision}, "agent:{agent}", "req:{req_id}"]
  project: "orchestrator-demo"
```

### 审查前参考历史

```
Tool: mem_search
  query: "agent:{agent} decision"
  project: "orchestrator-demo"
```

### 验证后记录

```
Tool: mem_save
  text: "验证 {req_id}: {verdict}。漏报: {missing_items}"
  type: "verification"
  tags: ["orchestrator", "verified", "agent:{agent}"]
  project: "orchestrator-demo"
```

| type | 用途 |
|------|------|
| `decision` | 审批决策 + 分析摘要 |
| `verification` | Completion 验证结果 |
| `pattern` | 积累的审批模式 |

---

## 会话审计

`ccrecall` 将会话转录同步到 SQLite 用于追溯。`npx ccrecall sync` 定期同步。

**追溯锁获取失败：**
```bash
npx ccrecall search "lock acquisition failed"
```

**审批效率概览：**
```bash
npx ccrecall query "SELECT date(timestamp) as day, COUNT(CASE WHEN content_text LIKE '%approved%' THEN 1 END) as approved, COUNT(CASE WHEN content_text LIKE '%rejected%' THEN 1 END) as rejected FROM messages WHERE content_text LIKE '%orchestrator%' GROUP BY day ORDER BY day DESC"
```

| 指标 | 告警条件 |
|------|---------|
| 审批延迟 | request → approval > 30min |
| 锁竞争频率 | 连续 3 次锁冲突 |
| 拒绝率 | 某 agent > 50% |
| Completion 漏报 | > 0 |
