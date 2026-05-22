Orchestrator 连续执行步骤 2-7，每个步骤完成后自动推进 current_step：
class PipelineOrchestrator:
    def __init__(self, request_id):
        self.request_id = request_id
        self.conn = sqlite3.connect(DB_PATH)
    
    def execute_pipeline(self):
        """连续执行步骤 2-7（扫描到签发锁）"""
        
        # Step 2: 扫描请求
        if not self._execute_step(2, "step2_scan_requests", self._scan_requests):
            return False
        
        # Step 3: 冲突分析
        if not self._execute_step(3, "step3_conflict_analysis", self._analyze_conflict):
            return False
        
        # Step 4: 越界分析
        if not self._execute_step(4, "step4_boundary_analysis", self._analyze_boundary):
            return False
        
        # Step 5: 逻辑分析
        if not self._execute_step(5, "step5_logic_analysis", self._analyze_logic):
            return False
        
        # Step 6: 写入审批
        if not self._execute_step(6, "step6_write_approval", self._write_approval):
            return False
        
        # Step 7: 签发锁
        if not self._execute_step(7, "step7_issue_lock", self._issue_lock):
            return False
        
        # Step 8: 释放锁（由 Worker 在修改完成后执行）
        return True
    
    def _execute_step(self, step_num, column_name, step_func):
        """执行单个步骤，带乐观锁重试"""
        max_retries = 3
        for attempt in range(max_retries):
            # 1. 检查是否有权限
            cur = self.conn.execute(f"""
                SELECT current_step, {column_name}, version
                FROM pipeline_state
                WHERE request_id = ?
            """, (self.request_id,))
            current_step, completed, version = cur.fetchone()
            
            if completed == 1:
                print(f"Step {step_num} already completed")
                return True
            
            if current_step != step_num:
                print(f"Cannot execute step {step_num}, current step is {current_step}")
                return False
            
            # 2. 执行实际逻辑
            result = step_func()
            if not result:
                return False
            
            # 3. 原子更新步骤完成标志
            updated = self.conn.execute(f"""
                UPDATE pipeline_state 
                SET {column_name} = 1, 
                    version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ? AND version = ?
            """, (self.request_id, version))
            
            self.conn.commit()
            
            if updated.rowcount > 0:
                return True
            else:
                print(f"Optimistic lock conflict at step {step_num}, retrying...")
                time.sleep(0.1)
        
        return False