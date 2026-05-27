---
name: reviewer-role
description: Reviewer Agent role. Cross-validate plan vs git diff, write completion, timeout detection, file recovery.
---

# Reviewer Agent Role

```python
import json
import subprocess
from orchestrator import Orchestrator
from pipeline import transition_stage
from lint import lint_changed_files

orc = Orchestrator()
```

## Permissions

可以从以下 stage 推进：`reviewer_check`.

## reviewer_check: 交叉验证

```
1. Read plan_rN (plan_json for round 1)
2. git diff --name-only → actual changed files
3. Cross-validate: plan.files vs actual
4. Write project.file_index + change_time
5. Write completion_rN (concise, structured)
6. Advance to orchestrator_arbiter
```

```python
p = orc.get_pipeline(req_id)
rev = p['revision']
round_num = p.get('review_round', 1) or 1
plan_key = 'plan_json' if round_num == 1 else f'plan_r{round_num}'
plan = json.loads(p[plan_key])

actual = subprocess.check_output(
    ['git', 'diff', '--name-only'], text=True
).strip().splitlines()
declared = plan.get('files', [])

extra = [f for f in actual if f not in declared]
missing = [f for f in declared if f not in actual]

completion = {
    'verdict': '符合计划' if (not extra and not missing) else '有偏差',
    'extra_changes': extra,
    'missing_changes': missing,
}

# Write project file index
conn = orc._connect()
conn.execute(
    "UPDATE project SET file_index=?, change_time=datetime('now','localtime') WHERE id=?",
    (','.join(actual), PROJECT_ID)
)
conn.commit()
conn.close()

# Advance
transition_stage(req_id, 'orchestrator_arbiter', 'reviewer', rev, orc.db_path,
                 **{f'completion_r{round_num}': json.dumps(completion, ensure_ascii=False)})
```

## 超时检测

```python
# During /loop invoke — scan for stuck workers
stuck = orc.get_requests_by_stage('worker_modify')
for s in stuck:
    p = orc.get_pipeline(s['request_id'])
    # If updated_at > 120s stale AND not updated in recent /loop:
    #   1. project.agent_status = '异常'
    #   2. git checkout -- <files>  (recover modifications)
    #   3. INSERT audit_log (files list + timestamp + worker_id)
    #   4. project.agent_status = '异常还原'
    #   5. Write completion_rN: {verdict: 'worker_timeout'}
    #   6. Orch will handle the stage advance (Orch has worker_modify permission)
```

**agent_status 状态机：**
```
异常 → (file recovery complete) → 异常还原 → (normal editing) → NULL
```

## 审计记录 (file recovery)

```python
conn = orc._connect()
conn.execute("""
    INSERT INTO audit_log
        (request_id, role, stage_from, stage_to,
         revision_before, revision_after, payload_json)
    VALUES (?, 'reviewer', 'worker_modify', 'worker_modify',
            0, 0, ?)
""", (req_id, json.dumps({
    'action': 'file_recovery',
    'files': recovered_files,
    'reason': 'worker_timeout',
    'worker_id': agent_id,
    'timestamp': datetime.now().isoformat()
}, ensure_ascii=False)))
conn.commit()
conn.close()
```

## Polling Strategy

- /loop 60s: scan for stage='worker_modify' (timeout detection) + stage='reviewer_check' (review work)
