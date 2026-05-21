# Orchestrator Demo

启动时执行以下代码确定角色，直接运行不展开讨论：

```python
from orchestrator import Orchestrator
orc = Orchestrator()
orc.init_db()
orc.migrate()
role = orc.try_register()
print(f"ROLE: {role}")
```

- `ROLE: orchestrator` → 读 `.claude/skills/orchestrator-role.md`
- `ROLE: worker` → 读 `.claude/skills/worker-role.md`
