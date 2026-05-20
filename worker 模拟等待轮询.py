while True:
    cur.execute("SELECT status FROM requests WHERE id=?", (req_id,))
    status = cur.fetchone()[0]
    if status != "pending":
        break
    time.sleep(2)
