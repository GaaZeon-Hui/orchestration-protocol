import json
import sqlite3

conn = sqlite3.connect("orchestrator.db")
cur = conn.cursor()
cur.execute(
    """
    INSERT INTO requests (id, agent, status, reason, scope_json, plan_json, self_review_json)
    VALUES (?, ?, 'pending', ?, ?, ?, ?)
""",
    (
        req_id,
        agent,
        reason,
        json.dumps(scope),
        json.dumps(plan),
        json.dumps(self_review),
    ),
)
conn.commit()
