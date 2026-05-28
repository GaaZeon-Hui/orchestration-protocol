# Orchestrator Demo

启动时执行以下代码确定角色，直接运行不展开讨论：

**Python (master):**
```python
from orchestrator import Orchestrator
orc = Orchestrator()
orc.init_db()
orc.migrate()
role = orc.try_register()
print(f"ROLE: {role}")
```

**Rust (rust-rewrite):**
```bash
cargo run --bin orchestrator
```

- `ROLE: orchestrator` → 读 `.claude/skills/orchestrator-role/SKILL.md`
- `ROLE: worker` → 读 `.claude/skills/worker-role/SKILL.md`
- `ROLE: reviewer` → 读 `.claude/skills/reviewer-role/SKILL.md`

架构：`pipeline.py`/`pipeline.rs` 提供 `transition_stage()`（CAS + 权限 + 审计），`orchestrator.py`/`orchestrator.rs` 提供查询与角色注册，`lint.py`/`lint.rs` 提供程序化预检。SQLite 状态机 + WAL + Trigger 兜底。
