# Orchestration Protocol — Claude Code Multi-Agent Orchestration System

A SQLite-based pipeline state machine for orchestrating multiple Claude Code agents. **Orchestrator** and **Worker** agents coordinate through a single `pipeline_state` table with `transition_stage()` CAS progression, completing the full cycle of request submission, review, modification, and verification.

## Architecture

```
┌─────────────────────────┐         ┌─────────────────────────┐
│     Orchestrator        │         │     Worker Agent        │
│                         │  SQLite │                         │
│ • Sentinel check        │◄───────►│ • init_pipeline         │
│ • Conflict / boundary / │   WAL   │ • Poll for approval     │
│   logic analysis        │         │ • transition_stage()    │
│ • transition_stage()    │         │   advancing the pipeline│
│   approve / reject      │         │ • Self-review +         │
│ • Verify completion     │         │   completion            │
│ • Heartbeat             │         │ • Cross-session recovery│
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

## Core Design

| Decision | Rationale |
|----------|-----------|
| `pipeline_state` replaces 5 tables | Single table carries the full lifecycle, no JOINs needed |
| `pipeline.py` standalone module | `transition_stage()` decoupled from Orchestrator class, usable by Worker too |
| `transition_stage()` CAS | `WHERE stage=? AND revision=?` guarantees concurrent safety |
| `ROLE_PERMISSIONS` matrix | Python-layer enforcement: a role can only advance from its authorized stages |
| `audit_log` auto-audit | One row appended per transition; payload_json captures changed columns only |
| SQL trigger `tr_stage_transition` | DB-layer backstop validating transition legality + revision increment |
| WAL mode | Reads never block writes; multiple Workers can read/write different rows |
| Heartbeat auto-takeover | Orchestrator death detected within 90s; next agent assumes the role |
| `recover_pipeline()` | Crash recovery by stage checkpoint, no external MCP memory needed |

## Installation

```bash
git clone https://github.com/GaaZeon-Hui/orchestration-protocol.git
cd orchestration-protocol
```

Python 3 standard library only (`sqlite3`). No external dependencies.

## Usage

Start a Claude Code session in this directory. The agent reads `CLAUDE.md` → imports `orchestrator.py` → auto-registers its role.

**Role auto-assignment:**
1. The first agent to launch registers as **Orchestrator**
2. Subsequent agents register as **Worker**
3. If the Orchestrator is absent for > 90 seconds, the next Worker auto-promotes

## File Structure

```
├── pipeline.py                     # Standalone protocol — transition_stage() + constants
├── orchestrator.py                 # Python library — queries + role registration + monitor
├── test_pipeline.py                # pipeline.py unit tests
├── test_orchestrator.py            # orchestrator.py integration tests
├── CLAUDE.md                       # Launch entry point
├── README.md                       # Chinese documentation
├── README_EN.md                    # This file
├── .gitignore
└── .claude/skills/
    ├── orchestration-protocol/
    │   └── SKILL.md                # Role registration + Schema + Permission matrix
    ├── orchestrator-role/
    │   └── SKILL.md                # Orchestrator full instructions
    └── worker-role/
        └── SKILL.md                # Worker full instructions
```

## Workflow

### Worker (pipeline stage driven)

```
Step 0  Configure      → orc.get_boundaries() / orc.set_boundaries()
Step 1  Recover        → orc.get_pending_requests() / orc.recover_pipeline()
Step 2  Pull + read    → git pull, get_pipeline(), get_boundaries()
Step 3  Conflict check → ensure no modifying pipeline exists for the same agent
Step 4  Init pipeline  → orc.init_pipeline()
Step 5  Wait approval  → poll get_pipeline() until approved / rejected
Step 6  Read approval  → get_pipeline() → granted_scope_json
Step 7  Modify         → transition_stage(approved → modifying, role='worker')
                       → modify only authorized files
                       → transition_stage(modifying → self_review_done, role='worker')
Step 8  Self-review    → check against boundaries
Step 9  Complete       → transition_stage(self_review_done → completion_submitted, …)
        + Release      → wait for completed → transition_stage(completed → lock_released)
```

### Orchestrator

```
1. orc.init_db() + orc.migrate()
2. orc.get_boundaries()
3. Sentinel check: git log / diff
4. Monitor: orc.run_monitor(id) — filters by pipeline stage
5. Three-pronged analysis → transition_stage(role='orchestrator') step by step
6. Approval → transition_stage(logic_analysis_done → approved / rejected)
7. Verify → compare against git diff
8. Complete → transition_stage(completion_submitted → completed)
```

## Permission Matrix (`pipeline.ROLE_PERMISSIONS`)

| Role | Authorized from_stage |
|------|----------------------|
| **orchestrator** | `request_submitted`, `conflict_analysis_done`, `boundary_analysis_done`, `logic_analysis_done`, `completion_submitted` |
| **worker** | `approved`, `modifying`, `self_review_done`, `completed` |

## Stage Machine

```
request_submitted → conflict_analysis_done → boundary_analysis_done
    → logic_analysis_done → approved → modifying → self_review_done
    → completion_submitted → completed → lock_released
                         ↘ rejected
```

Each transition enforced by three-layer defense:
1. Python path validation (`VALID_TRANSITIONS`)
2. Python role permission (`ROLE_PERMISSIONS`)
3. SQL trigger (`tr_stage_transition` — revision CAS + legality)

## Module Boundaries (user-configured)

On first launch, the Worker prompts the user to set per-agent module boundaries, stored in `context.boundaries_json`.

```json
{
  "py-agent":  { "can_touch": ["*.py"],  "forbidden": ["*.md", "app/", "service/"] },
  "md-agent":  { "can_touch": ["*.md"],  "forbidden": ["*.py", "app/", "service/"] },
  "service-agent": { "can_touch": ["service/"], "forbidden": ["app/", "*.py", "*.md"] },
  "ui-agent":  { "can_touch": ["app/"],  "forbidden": ["service/", "*.py", "*.md"] }
}
```

## Core API

| Function | Module | Description |
|----------|--------|-------------|
| `transition_stage(req_id, new_stage, role, revision, db_path, **kwargs)` | `pipeline.py` | Single stage-progression entry point with permission + audit |
| `init_pipeline(agent, reason, scope, plan, self_review, constraints, tz)` | `orchestrator.py` | Create a new pipeline |
| `get_pipeline(request_id)` | `orchestrator.py` | Fetch a single pipeline |
| `get_pending_requests(agent)` | `orchestrator.py` | List non-terminal pipelines |
| `get_requests_by_stage(stage)` | `orchestrator.py` | Query pipelines by stage |
| `recover_pipeline(agent)` | `orchestrator.py` | Crash recovery |
| `get_boundaries()` / `set_boundaries(b)` | `orchestrator.py` | Module boundaries |

## Tests

```bash
pytest test_pipeline.py -v       # transition_stage + permissions + CAS
pytest test_orchestrator.py -v   # integration tests
```
