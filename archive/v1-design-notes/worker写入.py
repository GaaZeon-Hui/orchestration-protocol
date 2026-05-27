class PipelineWorker:
    def __init__(self, request_id, agent_name):
        self.request_id = request_id
        self.agent_name = agent_name
        self.conn = sqlite3.connect(DB_PATH)

    def can_write_step(self, step_num):
        """检查当前是否有权限写入该步骤"""
        cur = self.conn.execute(
            """
            SELECT current_step, 
                   CASE :step_num
                       WHEN 1 THEN step1_acquire_lock
                       WHEN 2 THEN step2_scan_requests
                       -- ... 其他步骤
                   END as completed
            FROM pipeline_state
            WHERE request_id = :request_id
        """,
            {"step_num": step_num, "request_id": self.request_id},
        )

        current_step, completed = cur.fetchone()

        # 规则：只有 current_step 等于目标步骤，且该步骤未完成
        return current_step == step_num and completed == 0

    def execute_step1_acquire_lock(self):
        """Worker：获取锁"""
        if not self.can_write_step(1):
            print("Not authorized to write step 1")
            return False

        # 实际获取锁的逻辑
        success = self._do_acquire_lock()

        if success:
            # 原子更新：只有版本匹配才成功
            updated = self.conn.execute(
                """
                UPDATE pipeline_state 
                SET step1_acquire_lock = 1, 
                    version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ? AND version = ?
            """,
                (self.request_id, self._get_version()),
            )

            self.conn.commit()

            if updated.rowcount == 0:
                print("Optimistic lock failed, retrying...")
                return self.execute_step1_acquire_lock()  # 重试
            return True
        return False
