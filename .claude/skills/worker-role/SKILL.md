---
name: worker-role
description: Worker Agent role. Pre-init project/register lookup, plan submission, code modification, correction loop, lock release.
---

# Worker Agent Role

```python
import json
from orchestrator import Orchestrator
from pipeline import transition_stage

orc = Orchestrator()
AGENT_ID = "py-agent"  # set per agent
```

## Permissions

可以从以下 stage 推进：`init`, `worker_modify`, `verified`.

## Pre-Init Phase (before pipeline creation)

```
1. R: project table → find target file index
   - If file_use is occupied → wait (/loop)
   - If file_use is NULL → write own ID
   - If agent_status == '异常还原' → take over (write own ID to file_use)
2. R: register table → get own schema via agent_id
3. R: project context
4. Produce reason_json and plan_json
```

**reason_json format:**
```json
{"reason": "修复空指针", "agent_id": "py-agent-001", "schema": {...}}
```

**plan_json format:**
```json
{"files": ["a.py"], "changes": [{"file": "a.py", "type": "modify", "hint": "第42行添加None检查"}]}
```

## init: Pipeline Creation

```python
req_id = orc.init_pipeline(
    agent=AGENT_ID,
    reason={'reason': '修复空指针', 'agent_id': AGENT_ID, 'schema': schema},
    plan={'files': ['a.py'], 'changes': [{'file': 'a.py', 'type': 'modify', 'hint': '添加None检查'}]}
)
```

## worker_modify: 修改代码

```
Round 1: read plan_json → modify code → write commits_json → advance to reviewer_check
Round N (correction): read feedback_r{N-1} → write plan_rN → modify code → advance to reviewer_check
```

```python
p = orc.get_pipeline(req_id)
rev = p['revision']
round_num = p.get('review_round', 1) or 1
plan_key = 'plan_json' if round_num == 1 else f'plan_r{round_num}'

# On correction rounds, read Orch feedback
if round_num > 1:
    feedback = json.loads(p.get(f'feedback_r{round_num - 1}', '{}'))
    # feedback tells what to fix — follow it

plan = json.loads(p[plan_key])
# Modify code per plan
# git add + git commit

transition_stage(req_id, 'reviewer_check', 'worker', rev, orc.db_path,
                 commits_json=json.dumps(['abc123'], ensure_ascii=False),
                 **{plan_key: json.dumps(plan, ensure_ascii=False)})
```

## Polling for Approval

```python
p = orc.get_pipeline(req_id)
if p['stage'] == 'init':
    # Waiting for orch gate → /loop 30s
elif p['stage'] == 'worker_modify':
    # Approved → do work
elif p['stage'] == 'verified':
    # Go to lock release
```

## verified: Lock Release

```python
p = orc.get_pipeline(req_id)
rev = p['revision']
transition_stage(req_id, 'lock_released', 'worker', rev, orc.db_path)

# Read decision
hi = json.loads(p.get('human_intervention', 'null') or 'null') or {}
decision = hi.get('decision', None) if isinstance(hi, dict) else None

# Clear file occupation
conn = orc._connect()
conn.execute("UPDATE project SET file_use=NULL WHERE file_use=?", (AGENT_ID,))
conn.commit()
conn.close()

if decision == 'reject_restart':
    # Create new pipeline — go back to Pre-Init Phase
```

## 崩溃恢复

```python
req_id, stage = orc.recover_pipeline(AGENT_ID)
# init → wait for gate, /loop
# worker_modify → continue modifying
# verified → lock release
# None → no active pipeline, wait for new task
```
