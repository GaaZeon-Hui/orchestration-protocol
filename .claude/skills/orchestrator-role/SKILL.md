---
name: orchestrator-role
description: Orchestrator role. Gate approval, arbitration, human intervention, Reviewer timeout scan.
---

# Orchestrator Role

```python
import json
from orchestrator import Orchestrator
from pipeline import transition_stage
from lint_gate import lint_plan

orc = Orchestrator()
```

## Permissions

可以从以下 stage 推进：`init`, `orchestrator_gate`, `orchestrator_arbiter`, `verified`, `worker_modify`.

## Startup

```python
orc.init_db()
orc.migrate()
role = orc.try_register()
result = orc.check_and_heartbeat(role)
for item in result['items']:
    if item['type'] == 'new_request':
        # pipeline at stage='init' → gate review
    elif item['type'] == 'awaiting_arbitration':
        # pipeline at stage='orchestrator_arbiter' → arbitrate
```

## orchestrator_gate: 准入审批

```python
p = orc.get_pipeline(req_id)
rev = p['revision']

# Step 1: Get boundaries from register
reg = orc.get_register(p['agent'])
if not reg or not reg.get('schema_json'):
    transition_stage(req_id, 'rejected', 'orchestrator', rev, orc.db_path,
                     approval_status='rejected',
                     rejection_reason='Agent not registered')
    return

boundaries = {p['agent']: reg['schema_json']}

# Step 2: Lint the plan — programmatic, no LLM
result = lint_plan(p['plan_json'], boundaries, p['agent'])
if result['blocked']:
    transition_stage(req_id, 'rejected', 'orchestrator', rev, orc.db_path,
                     approval_status='rejected',
                     rejection_reason=result['reason'])
    return

# Step 3: LLM reads reason_json, evaluates reasonableness
# If unreasonable → rejected with explanation
# If reasonable:
transition_stage(req_id, 'worker_modify', 'orchestrator', rev, orc.db_path,
                 approval_status='approved')
```

## orchestrator_arbiter: 仲裁

```python
p = orc.get_pipeline(req_id)
rev = p['revision']
round_num = p.get('review_round', 1) or 1
completion_key = f'completion_r{round_num}'
completion = p.get(completion_key, {})
if isinstance(completion, str):
    completion = json.loads(completion)
verdict = completion.get('verdict', '')

if verdict == '符合计划':
    transition_stage(req_id, 'verified', 'orchestrator', rev, orc.db_path)

elif verdict == 'worker_timeout':
    transition_stage(req_id, 'verified', 'orchestrator', rev, orc.db_path,
                     human_intervention=json.dumps({
                         'needs_human': True, 'trigger': 'worker_timeout',
                         'summary': 'Worker timed out, files recovered'
                     }, ensure_ascii=False))
    p2 = orc.get_pipeline(req_id)
    transition_stage(req_id, 'lock_released', 'orchestrator', p2['revision'], orc.db_path)

elif round_num < 4:
    feedback = {}  # LLM: concise fix instructions
    new_round = round_num + 1
    transition_stage(req_id, 'worker_modify', 'orchestrator', rev, orc.db_path,
                     **{f'feedback_r{round_num}': json.dumps(feedback, ensure_ascii=False)},
                     review_round=new_round)

else:
    transition_stage(req_id, 'orchestrator_arbiter', 'orchestrator', rev, orc.db_path,
                     human_intervention=json.dumps({
                         'needs_human': True, 'round': round_num,
                         'trigger': 'max_round',
                         'summary': f'Round {round_num} still not passing'
                     }, ensure_ascii=False))
    # status.py flags this. Wait for user.
```

## Reviewer 超时扫描

```python
stuck = orc.get_requests_by_stage('reviewer_check')
for s in stuck:
    p = orc.get_pipeline(s['request_id'])
    # If updated_at > 120s stale → Reviewer may be dead
    # Write human_intervention, report to user, do NOT advance stage
```

## 人工介入

```python
p = orc.get_pipeline(req_id)
rev = p['revision']

if user_decision == 'verify':
    transition_stage(req_id, 'verified', 'orchestrator', rev, orc.db_path,
                     human_intervention=json.dumps({...原有字段...,
                         'decision': 'verify', 'decided_by': 'user'}, ensure_ascii=False))
elif user_decision == 'continue':
    transition_stage(req_id, 'worker_modify', 'orchestrator', rev, orc.db_path,
                     human_intervention=json.dumps({...原有字段...,
                         'decision': 'continue_r4', 'decided_by': 'user'}, ensure_ascii=False),
                     review_round=4)
elif user_decision == 'reject_restart':
    transition_stage(req_id, 'verified', 'orchestrator', rev, orc.db_path,
                     human_intervention=json.dumps({...原有字段...,
                         'decision': 'reject_restart', 'decided_by': 'user'}, ensure_ascii=False))
```
