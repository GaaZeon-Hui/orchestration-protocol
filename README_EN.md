# Orchestration Protocol — Lightweight Multi-Agent Orchestration

Pure Python stdlib, zero external dependencies. A SQLite state machine with CAS optimistic locking for 3-role agent coordination, built for Claude Code.

**Worker** submits a plan → **Orchestrator** vets it via lint gate → **Reviewer** independently verifies the commit matches the plan → **Orchestrator** arbitrates → lock released. A `review_round` counter supports up to 4 correction loops, escalating to human intervention when exhausted.

## Architecture

```
  Worker ── reason + plan ──▶ Orchestrator (gate)
                                  │ lint passes
  Worker ◀── approved ───────────┘
    │ modifies code
    ▼
  Reviewer ── completion ──▶ Orchestrator (arbiter)
                                  │
              ┌───────────────────┤
              ▼                   ▼
          verified          worker_modify (correction loop)
              │                   │
              ▼                   └──▶ Reviewer ──▶ Orch ──▶ ...
          lock_released
```

## Core Design

| Aspect | Mechanism |
|--------|-----------|
| **Stages** | 7 named stages: init → orch_gate → worker_modify → reviewer_check → orch_arbiter → verified → lock_released |
| **Correction** | `review_round` counter, 4-round limit, then human intervention |
| **CAS** | `WHERE stage=? AND revision=?` atomic advance; rowcount=0 → concurrent conflict |
| **Permissions** | `ROLE_PERMISSIONS` matrix limits each role to specific stages |
| **Whitelist** | `ALLOWED_COLUMNS` silently filters illegal column names |
| **Trigger** | `tr_stage_transition` DB-level guard — raw SQL cannot bypass transition rules |
| **WAL** | Writers never block readers; multiple agents poll via `/loop` concurrently |
| **Audit** | `audit_log` — immutable record appended on every stage transition |
| **Lint** | 3 modules — `lint_gate` (lightweight gate) · `lint_full` (full reviewer) · `lint_crossref` (3-way cross-reference) |
| **Human** | `human_intervention` column; status.py highlights needs-human pipelines |

## Install

```bash
git clone https://github.com/GaaZeon-Hui/orchestration-protocol.git
cd orchestration-protocol
```

Requires only Python 3 (`sqlite3` stdlib).

## Usage

Launch Claude Code in this directory. The agent reads `CLAUDE.md`, registers its role, and follows SKILL.md instructions.

## File Structure

```
pipeline.py           — state machine core · transition_stage()
orchestrator.py       — DB layer · CRUD · heartbeat · orphan locks
lint_core.py          — boundary glob matching (shared)
lint_gate.py          — plan validation · for orchestrator_gate
lint_full.py          — AST + conflicts + lint_crossref · for reviewer_check
status.py             — terminal dashboard
test_*.py             — 90 unit/integration tests
.claude/skills/       — 3 role-specific LLM behavior instructions
docs/                 — architecture docs · HTML analysis · implementation plans
archive/v1/           — legacy design notes
```

## Permission Matrix

| Role | Can advance from |
|------|-----------------|
| **Worker** | `init` · `worker_modify` · `verified` |
| **Orchestrator** | `init` · `orchestrator_gate` · `orchestrator_arbiter` · `verified` · `worker_modify` |
| **Reviewer** | `reviewer_check` |

## Tests

```bash
python -m pytest test_pipeline.py test_orchestrator.py test_lint.py -v
# 90 passed in ~2s
```

## License

MIT License.
