# Orchestration Protocol

启动时执行以下代码确定角色，直接运行不展开讨论：

```python
from orchestrator import Orchestrator
orc = Orchestrator()
orc.init_db()
orc.migrate()
role = orc.try_register()
print(f"ROLE: {role}")
```

- `ROLE: orchestrator` → 读 `.claude/skills/orchestrator-role/SKILL.md`
- `ROLE: worker` → 读 `.claude/skills/worker-role/SKILL.md`
- `ROLE: reviewer` → 读 `.claude/skills/reviewer-role/SKILL.md`

架构：`pipeline.py` 提供 `transition_stage()`（CAS + 权限 + 审计），`orchestrator.py` 提供查询与角色注册，`lint_core.py` / `lint_gate.py` / `lint_full.py` 提供分层程序化预检。四表 `pipeline_state` + `project` + `register` + `audit_log`。
