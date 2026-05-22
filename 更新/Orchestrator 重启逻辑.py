def orchestrator_recover():
    """扫描所有卡在步骤 2-7 之间的请求"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("""
        SELECT request_id, current_step,
               step2_scan_requests, step3_conflict_analysis,
               step4_boundary_analysis, step5_logic_analysis,
               step6_write_approval, step7_issue_lock
        FROM pipeline_state
        WHERE status = 'active' 
          AND current_step BETWEEN 2 AND 7
          AND updated_at < datetime('now', '-5 minutes')  -- 卡住超过 5 分钟
    """)

    for row in cur.fetchall():
        request_id, current_step = row[0], row[1]
        print(f"Recovering stuck pipeline: {request_id} at step {current_step}")

        # 根据 current_step 恢复
        orchestrator = PipelineOrchestrator(request_id)
        if current_step == 2:
            orchestrator.execute_from_step2()
        elif current_step == 3:
            orchestrator.execute_from_step3()
        # ...
