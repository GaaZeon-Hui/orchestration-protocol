cur.execute("BEGIN")
cur.execute(
    "UPDATE requests SET status='approved', updated_at=CURRENT_TIMESTAMP WHERE id=?",
    (req_id,),
)
cur.execute(
    "INSERT INTO approvals (request_id, status, granted_scope_json) VALUES (?, 'approved', ?)",
    (req_id, json.dumps(granted_scope)),
)
cur.execute(
    "UPDATE lock SET state='locked', holder=?, request_id=? WHERE id=1", (agent, req_id)
)
conn.commit()
