# Orchestration Protocol for Claude Code

A SQLite-based multi-agent orchestration system where **Orchestrator** and **Worker** agents coordinate through a single database file (`orchestrator.db`).

## How It Works

```
┌─────────────────────┐         ┌─────────────────────┐
│   Orchestrator      │         │   Worker Agent      │
│                     │         │                     │
│ • 哨兵检查          │  SQLite │ • 提交请求          │
│ • 三项分析          │◄───────►│ • 等待审批          │
│ • 签发锁            │   WAL   │ • 执行修改          │
│ • 验证 Completion   │         │ • 自审 + 完成报告   │
│ • 心跳维护          │         │ • 跨会话任务恢复    │
└─────────────────────┘         └─────────────────────┘
```

- **Role registration**: First agent to start becomes Orchestrator (heartbeat-based). Subsequent agents become Workers. If Orchestrator dies, next Worker auto-promotes.
- **Locking**: SQLite row-level locks + WAL mode. Multiple Workers can read/write different rows concurrently.
- **Persistence**: All state (requests, approvals, completions, locks) in one `orchestrator.db` file.
- **Memory**: Cross-session task memory via `mcp-simple-memory`. Session transcripts via `ccrecall`.

## Installation

```bash
git clone <this-repo>
cd orchestration-protocol
```

No dependencies beyond Python 3 standard library (`sqlite3`).

Optional tools:
```bash
npx mcp-simple-memory init   # Cross-session memory
npx ccrecall sync             # Session transcript analysis
```

## Usage

Start a Claude Code session in this directory. The agent reads `CLAUDE.md` on startup and automatically runs role registration — no slash command needed.

Or manually invoke the entry skill:
```
/orchestration-protocol
```

## File Structure

```
├── CLAUDE.md                          # Startup: auto role detection
├── orchestration-protocol.md          # Entry: role registration + DB schema
├── orchestrator-role.md               # Orchestrator role instructions
├── worker-role.md                     # Worker role instructions
├── test-registration.py               # Registration flow tests (4 scenarios)
├── .claude/skills/                    # Skill files (Claude Code format)
├── worker agent 模拟提交请求.py        # Worker request simulation
├── Orchestrator 模拟审批.py            # Orchestrator approval simulation
├── worker 模拟等待轮询.py              # Worker polling simulation
├── 持久记忆服务器                       # mcp-simple-memory usage notes
└── 会话转录分析                         # ccrecall usage notes
```

## Testing

```bash
python3 test-registration.py
# 4 scenarios: first agent → orchestrator, second → worker,
# concurrent race, heartbeat timeout takeover
```

## Key Design Decisions

| Choice | Why |
|--------|-----|
| SQLite not JSON files | ACID transactions, row-level locking, `WHERE updated_at > ?` incremental queries |
| WAL mode | Writers don't block readers — multiple agents work concurrently |
| Single `lock` table row | Atomic role registration + task locking in one UPDATE |
| Heartbeat auto-promotion | Orchestrator death detected within 90s, no manual intervention |
| `updated_at` indexes | Monitor only scans changed rows, O(log n) vs O(n) |
