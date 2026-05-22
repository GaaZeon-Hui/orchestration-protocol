def worker_recover(request_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """
        SELECT 
            step1_acquire_lock,
            step2_scan_requests,
            step3_conflict_analysis,
            step4_boundary_analysis,
            step5_logic_analysis,
            step6_write_approval,
            step7_issue_lock,
            step8_release_lock,
            current_step
        FROM pipeline_state
        WHERE request_id = ?
    """,
        (request_id,),
    )

    row = cur.fetchone()
    steps_completed = [i for i, val in enumerate(row[:8], 1) if val == 1]

    if not steps_completed:
        # 没有任何步骤完成，从头开始
        return execute_from_step1()

    last_step = max(steps_completed)

    # 根据最后完成的步骤决定恢复点
    if last_step == 0:
        return execute_from_step1()
    elif last_step == 1:
        # 锁已获取，但请求未提交 → 检查是否真的有锁
        if lock_is_held():
            return execute_from_step2()  # 继续提交请求
        else:
            return execute_from_step1()  # 重新获取锁
    elif last_step == 7:
        # 锁已签发，但 Worker 崩溃在修改代码阶段
        if code_modified():
            return execute_from_step8()  # 直接写 completion 并释放锁
        else:
            return execute_from_step7()  # 重新修改代码
    # ... 其他恢复逻辑
