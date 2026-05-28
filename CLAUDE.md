# Orchestration Protocol

启动时执行以下代码确定角色，直接运行不展开讨论：

```python
import json, os, uuid
from orchestrator import Orchestrator

orc = Orchestrator()
orc.init_db()
orc.migrate()

# ── Load or create persistent agent identity ──
ID_FILE = ".claude/agent_id.json"
if os.path.exists(ID_FILE):
    with open(ID_FILE) as f:
        agent_id = json.load(f)["agent_id"]
else:
    agent_id = input("Enter your agent name (e.g. orch-01): ").strip()
    if not agent_id:
        agent_id = "agent-" + str(uuid.uuid4())[:6]
    os.makedirs(".claude", exist_ok=True)
    with open(ID_FILE, "w") as f:
        json.dump({"agent_id": agent_id}, f)

role = orc.try_register(agent_id)
print(f"ROLE: {role}")
```

- `ROLE: orchestrator` → 读 `.claude/skills/orchestrator-role/SKILL.md`
- `ROLE: worker` → 读 `.claude/skills/worker-role/SKILL.md`
- `ROLE: reviewer` → 读 `.claude/skills/reviewer-role/SKILL.md`

架构：`pipeline.py` 提供 `transition_stage()`（CAS + 权限 + 审计），`orchestrator.py` 提供查询与角色注册，`lint_core.py` / `lint_gate.py` / `lint_full.py` 提供分层程序化预检。四表 `pipeline_state` + `project` + `register` + `audit_log`。

## 角色注册规则

首次启动时输入 agent 名称，保存到 `.claude/agent_id.json`。此后每次启动自动复用同一个 ID，角色不再变化。
