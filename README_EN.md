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
| `lint.py` mechanical pre-check | Boundary glob match (blocking) + conflict detection + AST hints (informational); reject before LLM |
| `transition_stage()` CAS | `WHERE stage=? AND revision=?` guarantees concurrent safety |
| `ROLE_PERMISSIONS` matrix | Python-layer enforcement: a role can only advance from its authorized stages |
| `audit_log` auto-audit | One row appended per transition; payload_json captures changed columns only |
| SQL trigger `tr_stage_transition` | DB-layer backstop validating transition legality + revision increment |
| WAL mode | Reads never block writes; multiple Workers can read/write different rows |
| Heartbeat auto-takeover | Orchestrator death detected within 90s; next agent assumes the role |
| `recover_pipeline()` | Crash recovery by stage checkpoint, no external MCP memory needed |
| Analysis state persistence | `conflict/boundary/logic_analysis_json` columns save review conclusions for crash recovery |
| `init_pipeline()` same-agent guard | Prevents concurrent conflicting requests from the same agent type |

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
├── orchestrator.py                 # Python library — queries + role registration + heartbeat
├── lint.py                         # Mechanical pre-check — boundaries + conflicts + AST hints
├── test_pipeline.py                # pipeline.py unit tests
├── test_orchestrator.py            # orchestrator.py integration tests
├── test_lint.py                    # lint.py unit tests
├── CLAUDE.md                       # Launch entry point
├── README.md                       # Chinese documentation
├── README_EN.md                    # This file
├── 审查报告.md                     # Architecture review & issue tracker
├── .gitignore
├── .claude/
│   ├── settings.json               # Plugin config (superpowers)
│   └── skills/
│       ├── orchestration-protocol/
│       │   └── SKILL.md            # Role registration + Schema + Permission matrix
│       ├── orchestrator-role/
│       │   └── SKILL.md            # Orchestrator full instructions
│       └── worker-role/
│           └── SKILL.md            # Worker full instructions
```

## Workflow

### Worker (pipeline stage driven)

```
Step 0  Configure      → orc.get_boundaries() / orc.set_boundaries()
Step 1  Recover        → orc.get_pending_requests() / orc.recover_pipeline()
Step 2  Pull + read    → git pull, get_pipeline(), get_boundaries()
Step 3  Conflict check → orc.get_pending_requests(agent) ensures no active pipeline
Step 4  Init pipeline  → orc.init_pipeline() (raises RuntimeError if same agent busy)
Step 5  Wait approval  → get_pipeline() one-shot check; suggest /loop if not yet approved
Step 6  Read approval  → get_pipeline() → granted_scope_json
Step 7  Modify         → transition_stage(approved → modifying, role='worker')
                       → modify only authorized files
Step 8  Self-review    → check against boundaries + lint
                       → transition_stage(modifying → self_review_done, self_review_json=…)
Step 9  Complete       → transition_stage(self_review_done → completion_submitted, …)
        + Release      → wait for completed → transition_stage(completed → lock_released)
```

### Orchestrator

```
1. orc.init_db() + orc.migrate()
2. orc.get_boundaries()
3. Sentinel check: git log / diff
4. One-shot check: orc.check_and_heartbeat(id) — heartbeat + scan for new requests/completions
   → if no items, suggest /loop 60s for continuous monitoring
5. Lint pre-check (boundary violations → reject immediately; conflicts + AST hints feed LLM)
6. Three-pronged analysis → transition_stage(role='orchestrator') step by step, saving JSON each stage
7. Approval → transition_stage(logic_analysis_done → approved / rejected)
8. Verify → compare against git diff + lint hints
9. Complete → transition_stage(completion_submitted → completed)
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
| `transition_stage(req_id, new_stage, role, revision, db_path, **kwargs)` | `pipeline.py` | Single stage-progression entry point with permission + audit + CAS |
| `run_lint(files_changed, boundaries, agent, base_ref)` | `lint.py` | Boundary check + conflict detection + AST hints |
| `lint_changed_files(boundaries, agent, base_ref)` | `lint.py` | Convenience: git diff --name-only → run_lint() |
| `init_pipeline(agent, reason, scope, plan, self_review, constraints, tz)` | `orchestrator.py` | Create a new pipeline (same-agent active pipeline guard) |
| `get_pipeline(request_id)` | `orchestrator.py` | Fetch a single pipeline with JSON deserialization |
| `get_pending_requests(agent)` | `orchestrator.py` | List non-terminal pipelines (excl. rejected/lock_released) |
| `get_requests_by_stage(stage)` | `orchestrator.py` | Query pipelines by stage |
| `recover_pipeline(agent)` | `orchestrator.py` | Crash recovery, returns (request_id, stage) |
| `check_and_heartbeat(orchestrator_id)` | `orchestrator.py` | One-shot heartbeat + scan for new items (replaces blocking run_monitor) |
| `get_boundaries()` / `set_boundaries(b)` | `orchestrator.py` | Module boundaries CRUD |

## Tests

```bash
pytest test_pipeline.py -v       # transition_stage + permissions + CAS + analysis persistence
pytest test_orchestrator.py -v   # registration/heartbeat/full flow/crash recovery/concurrency guard
pytest test_lint.py -v           # boundary check / conflict detection / AST parsing / integration
pytest test_pipeline.py test_orchestrator.py test_lint.py -v  # all 74 tests
```
