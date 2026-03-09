async def run_fars_pipeline(experiment_id: str, project_id: str, start_from_stage: int = 0, revision_note: str = ""):
    """Execute the FARS pipeline with optional stage resumption and approval flow."""
    running_experiments[experiment_id] = True
    conn = get_db()
    exp = conn.execute("SELECT * FROM experiments WHERE id=?", (experiment_id,)).fetchone()
    conn.close()
    if not exp:
        return

    exp_name = exp["name"]
    hypothesis = exp["hypothesis"]
    memory_ctx = build_memory_context(project_id)

    def log(stage, msg, level="INFO"):
        conn2 = get_db()
        now = datetime.now(timezone.utc).isoformat()
        conn2.execute(
            "INSERT INTO logs (experiment_id, project_id, stage, level, message, created_at) VALUES (?,?,?,?,?,?)",
            (experiment_id, project_id, stage, level, msg, now),
        )
        conn2.commit()
        conn2.close()
        emit_event(f"exp_{experiment_id}", "log", {
            "experiment_id": experiment_id, "stage": stage, "level": level,
            "message": msg, "created_at": now,
        })

    def update_exp(status, stage="", **kwargs):
        conn2 = get_db()
        now = datetime.now(timezone.utc).isoformat()
        sets = ["status=?", "current_stage=?", "updated_at=?"]
        vals = [status, stage, now]
        for k, v in kwargs.items():
            sets.append(f"{k}=?")
            vals.append(v)
        vals.append(experiment_id)
        conn2.execute(f"UPDATE experiments SET {','.join(sets)} WHERE id=?", vals)
        conn2.commit()
        conn2.close()
        emit_event(f"project_{project_id}", "experiment_update", {
            "experiment_id": experiment_id, "status": status, "stage": stage,
        })

    def update_step(stage, status, output=""):
        conn2 = get_db()
        now = datetime.now(timezone.utc).isoformat()
        if status == "running":
            conn2.execute(
                "UPDATE pipeline_steps SET status=?, started_at=? WHERE experiment_id=? AND stage=?",
                (status, now, experiment_id, stage),
            )
        else:
            conn2.execute(
                "UPDATE pipeline_steps SET status=?, output_data=?, completed_at=? WHERE experiment_id=? AND stage=?",
                (status, output, now, experiment_id, stage),
            )
        conn2.commit()
        conn2.close()

    def get_approval_enabled():
        conn2 = get_db()
        row = conn2.execute("SELECT value FROM configs WHERE key='approval_enabled'").fetchone()
        conn2.close()
        return (row["value"] if row else "true") == "true"

    def get_execution_mode():
        conn2 = get_db()
        row = conn2.execute("SELECT value FROM configs WHERE key='execution_mode'").fetchone()
        conn2.close()
        return row["value"] if row else "simulate"

    def pause_for_approval(stage, stage_output):
        """Create approval record, update experiment status to pending_approval."""
        conn2 = get_db()
        now2 = datetime.now(timezone.utc).isoformat()
        stage_label = STAGE_LABELS.get(stage, stage)
        # Upsert: delete existing pending for this stage then insert fresh
        conn2.execute(
            "DELETE FROM stage_approvals WHERE experiment_id=? AND stage=? AND status='pending'",
            (experiment_id, stage),
        )
        conn2.execute(
            "INSERT INTO stage_approvals (experiment_id, stage, status, stage_output, created_at) VALUES (?,?,?,?,?)",
            (experiment_id, stage, "pending", stage_output[:4000] if stage_output else "", now2),
        )
        conn2.execute(
            "UPDATE experiments SET status='pending_approval', current_stage=?, updated_at=? WHERE id=?",
            (stage, now2, experiment_id),
        )
        conn2.commit()
        conn2.close()
        emit_event(f"project_{project_id}", "experiment_update", {
            "experiment_id": experiment_id, "status": "pending_approval", "stage": stage,
        })
        log("approval", f"[审批] 阶段\"{stage_label}\"已完成，等待审批...")

    # Load outputs from previously completed stages (for resumption)
    conn = get_db()
    prev_steps = conn.execute(
        "SELECT stage, output_data FROM pipeline_steps WHERE experiment_id=?", (experiment_id,)
    ).fetchall()
    conn.close()
    prev_outputs = {s["stage"]: s["output_data"] for s in prev_steps}

    # Restore stage outputs for context when resuming
    result = prev_outputs.get("ideation", "")
    plan_result = prev_outputs.get("planning", "")
    metrics_json = prev_outputs.get("experiment", "")

    # Check if LLM is configured
    conn = get_db()
    llm_configured = conn.execute("SELECT value FROM configs WHERE key='llm_api_key'").fetchone()
    conn.close()
    use_llm = llm_configured and llm_configured["value"].strip()

    try:
        # ---------------------------------------------------------------
        # Stage 1: Ideation
        # ---------------------------------------------------------------
        if start_from_stage <= 0:
            if not running_experiments.get(experiment_id):
                return
            update_exp("running", "ideation")
            update_step("ideation", "running")
            log("ideation", f"[假设生成] 启动 — 实验: {exp_name}")
            if revision_note and start_from_stage == 0:
                log("ideation", f"[假设生成] 修改说明: {revision_note}")

            if use_llm:
                log("ideation", "[假设生成] 正在调用 LLM 生成研究假设...")
                revision_ctx = f"\n\n## 修改要求\n{revision_note}" if revision_note else ""
                ideation_prompt = f"""你是一个AI科研助手，正在为以下实验生成研究假设。

## 项目记忆上下文
{memory_ctx}

## 实验信息
- 实验名称: {exp_name}
- 初始假设方向: {hypothesis or '待生成'}{revision_ctx}

请生成一个具体、可验证的研究假设，包括：
1. 核心假设陈述
2. 预期效果（量化）
3. 关键变量
4. 验证指标

以JSON格式返回：{{"hypothesis": "...", "expected_effect": "...", "variables": [...], "metrics": [...]}}"""
                try:
                    result = await call_llm([{"role": "user", "content": ideation_prompt}],
                        system_prompt="你是 LabOS 平台的AI科研助手。请用中文回答。")
                    log("ideation", "[假设生成] LLM 返回假设")
                    update_exp("running", "ideation", hypothesis=result[:2000])
                    store_memory(project_id, f"实验 {exp_name} 假设: {result[:500]}", "hypothesis", "ideation")
                except Exception as e:
                    log("ideation", f"[假设生成] LLM 调用失败: {str(e)}", "WARN")
                    result = f"默认假设: {hypothesis or exp_name}"
            else:
                log("ideation", "[假设生成] LLM 未配置，使用模拟模式")
                await asyncio.sleep(2)
                result = hypothesis or f"关于 {exp_name} 的默认假设"

            update_step("ideation", "completed", result[:2000] if isinstance(result, str) else str(result)[:2000])
            log("ideation", "[假设生成] ✓ 完成")

            # Approval check after ideation
            if get_approval_enabled():
                pause_for_approval("ideation", result[:4000] if isinstance(result, str) else str(result)[:4000])
                running_experiments.pop(experiment_id, None)
                return

        # ---------------------------------------------------------------
        # Stage 2: Planning
        # ---------------------------------------------------------------
        if start_from_stage <= 1:
            if not running_experiments.get(experiment_id):
                return
            update_exp("running", "planning")
            update_step("planning", "running")
            log("planning", "[实验规划] 启动")
            if revision_note and start_from_stage == 1:
                log("planning", f"[实验规划] 修改说明: {revision_note}")

            # Use previous ideation output if resuming
            ideation_output = result if (start_from_stage <= 0 and isinstance(result, str) and result) else prev_outputs.get("ideation", hypothesis or exp_name)

            if use_llm:
                log("planning", "[实验规划] 正在生成实验计划...")
                revision_ctx = f"\n\n## 修改要求\n{revision_note}" if (revision_note and start_from_stage == 1) else ""
                plan_prompt = f"""基于以下假设，生成详细的实验计划。

## 假设
{ideation_output[:1500]}

## 项目上下文
{memory_ctx[:1500]}{revision_ctx}

请生成实验计划，包括：
1. 具体实验步骤（5-8步）
2. 所需资源（GPU、数据集等）
3. 评估方案
4. 预计时间

以JSON格式返回：{{"steps": [...], "resources": {{...}}, "evaluation": "...", "estimated_hours": N}}"""
                try:
                    plan_result = await call_llm([{"role": "user", "content": plan_prompt}],
                        system_prompt="你是实验规划专家。请用中文回答。")
                    log("planning", "[实验规划] 计划已生成")
                    update_exp("running", "planning", plan=plan_result[:3000])
                    store_memory(project_id, f"实验 {exp_name} 计划: {plan_result[:500]}", "plan", "planning")
                except Exception as e:
                    log("planning", f"[实验规划] LLM 调用失败: {str(e)}", "WARN")
                    plan_result = "模拟计划"
            else:
                log("planning", "[实验规划] 使用模拟模式")
                await asyncio.sleep(2)
                plan_result = "模拟实验计划"

            update_step("planning", "completed", plan_result[:2000] if isinstance(plan_result, str) else "")
            log("planning", "[实验规划] ✓ 完成")

            # Approval check after planning
            if get_approval_enabled():
                pause_for_approval("planning", plan_result[:4000] if isinstance(plan_result, str) else str(plan_result)[:4000])
                running_experiments.pop(experiment_id, None)
                return

        # ---------------------------------------------------------------
        # Stage 3: Experiment execution
        # ---------------------------------------------------------------
        if start_from_stage <= 2:
            if not running_experiments.get(experiment_id):
                return
            update_exp("running", "experiment")
            update_step("experiment", "running")
            log("experiment", "[实验执行] 启动")
            if revision_note and start_from_stage == 2:
                log("experiment", f"[实验执行] 修改说明: {revision_note}")

            # Use previous planning output if resuming
            plan_ctx = plan_result if (start_from_stage <= 1 and isinstance(plan_result, str) and plan_result) else prev_outputs.get("planning", "")
            ideation_ctx = result if (start_from_stage <= 0 and isinstance(result, str) and result) else prev_outputs.get("ideation", "")

            execution_mode = get_execution_mode()
            log("experiment", f"[实验执行] 执行模式: {execution_mode}")

            if execution_mode == "real":
                # Real execution via SSH
                log("experiment", "[实验执行] 真实执行模式 — 检查 SSH 配置...")
                ssh_cfg = get_ssh_config()
                if not ssh_cfg["host"] or not ssh_cfg["password"]:
                    log("experiment", "[实验执行] SSH 未配置，回退到模拟模式", "WARN")
                    execution_mode = "simulate"
                else:
                    # Build training command from experiment info
                    training_script = ""
                    # Try to parse training script from plan JSON
                    if plan_ctx:
                        try:
                            plan_data = json.loads(plan_ctx)
                            training_script = plan_data.get("training_script", "")
                        except (json.JSONDecodeError, KeyError):
                            pass
                    if not training_script:
                        training_script = "python train.py"
                    training_command = f"cd /root && echo '=== LabOS 真实训练启动 ===' && echo '实验: {exp_name}' && echo '时间: $(date)' && nvidia-smi 2>/dev/null && {training_script}"
                    log("experiment", f"[实验执行] SSH 命令: {training_command}")
                    ssh_result = await ssh_execute(training_command, project_id, experiment_id)
                    if "error" in ssh_result and ssh_result["error"]:
                        log("experiment", f"[实验执行] SSH 错误: {ssh_result['error']}", "WARN")
                    ssh_output = ssh_result.get("output", "")
                    log("experiment", "[实验执行] SSH 输出记录完成")
                    # Try to parse metrics from SSH output
                    metrics_parsed = {"status": "real", "exit_code": ssh_result.get("exit_code", -1)}
                    for line in ssh_output.split("\n"):
                        for pat in [r"(?:acc|accuracy)[\s:=]+([\d.]+)", r"(?:loss)[\s:=]+([\d.]+)",
                                    r"(?:reward)[\s:=]+([\d.]+)", r"(?:score)[\s:=]+([\d.]+)"]:
                            m = re.search(pat, line, re.IGNORECASE)
                            if m:
                                key = re.split(r'[\s:=]', pat)[0].lstrip("(?:").rstrip(")")
                                metrics_parsed[key] = float(m.group(1))
                    metrics_json = json.dumps(metrics_parsed, ensure_ascii=False)
                    update_exp("running", "experiment", metrics=metrics_json)

            if execution_mode == "simulate":
                # Simulate execution (LLM analysis + mock metrics)
                log("experiment", "[实验执行] 模拟执行模式 — 记录实验框架和预期指标")
                await asyncio.sleep(3)
                mock_metrics = {"status": "simulated", "note": "连接AutoDL后可执行真实训练"}
                metrics_json = json.dumps(mock_metrics, ensure_ascii=False)

                if use_llm:
                    try:
                        exp_analysis = await call_llm([{"role": "user", "content": f"""基于以下实验计划，分析可能的实验结果和关键指标。

计划: {plan_ctx[:1500]}
假设: {ideation_ctx[:1000]}

请预测：
1. 关键指标及其预期范围
2. 可能的风险和失败模式
3. 建议的消融实验

用JSON返回。"""}], system_prompt="你是实验分析专家。请用中文回答。")
                        metrics_json = exp_analysis[:2000]
                        log("experiment", "[实验执行] LLM 分析完成")
                    except Exception as e:
                        log("experiment", f"[实验执行] 分析失败: {str(e)}", "WARN")

                update_exp("running", "experiment", metrics=metrics_json)

            update_step("experiment", "completed", metrics_json)
            log("experiment", "[实验执行] ✓ 完成")

            # Approval check after experiment
            if get_approval_enabled():
                pause_for_approval("experiment", metrics_json[:4000])
                running_experiments.pop(experiment_id, None)
                return

        # ---------------------------------------------------------------
        # Stage 4: Writing (final stage — no approval after)
        # ---------------------------------------------------------------
        if start_from_stage <= 3:
            if not running_experiments.get(experiment_id):
                return
            update_exp("running", "writing")
            update_step("writing", "running")
            log("writing", "[报告撰写] 启动")

            # Gather all available stage outputs for the report
            final_ideation = result if (start_from_stage <= 0 and isinstance(result, str) and result) else prev_outputs.get("ideation", "")
            final_plan = plan_result if (start_from_stage <= 1 and isinstance(plan_result, str) and plan_result) else prev_outputs.get("planning", "")
            final_metrics = metrics_json if (start_from_stage <= 2 and metrics_json) else prev_outputs.get("experiment", "")

            report = ""
            if use_llm:
                log("writing", "[报告撰写] 正在生成实验报告...")
                try:
                    report = await call_llm([{"role": "user", "content": f"""请为以下实验撰写一份完整的技术报告。

## 实验名称
{exp_name}

## 假设
{final_ideation[:1000]}

## 实验计划
{final_plan[:1000]}

## 实验结果/分析
{final_metrics[:1000]}

请撰写包含以下部分的报告：
1. 摘要
2. 背景与动机
3. 方法
4. 实验设计
5. 结果与分析
6. 结论与未来工作
7. 下一步行动建议

使用Markdown格式。"""}], system_prompt="你是学术写作专家。请用中文撰写高质量技术报告。")
                    log("writing", "[报告撰写] 报告已生成")
                except Exception as e:
                    log("writing", f"[报告撰写] 生成失败: {str(e)}", "WARN")
                    report = f"# {exp_name} 实验报告\n\n(LLM调用失败，请检查配置)"
            else:
                log("writing", "[报告撰写] 使用模拟模式")
                await asyncio.sleep(2)
                report = f"# {exp_name} 实验报告\n\n## 摘要\n模拟报告。请配置LLM以获得真实报告生成。"

            update_exp("completed", "writing", report=report, result_summary=report[:500])
            update_step("writing", "completed", report[:3000])
            store_memory(project_id, f"实验 {exp_name} 完成，报告摘要: {report[:300]}", "result", "writing")
            log("writing", "[报告撰写] ✓ 完成")
            log("pipeline", f"[流水线] ✓ 实验 {exp_name} 全部阶段完成")

            # Store finding
            conn = get_db()
            conn.execute(
                "INSERT INTO findings (experiment_id, project_id, category, content, confidence, source_stage) VALUES (?,?,?,?,?,?)",
                (experiment_id, project_id, "result", f"实验 {exp_name} 完成", 0.8, "writing"),
            )
            conn.commit()
            conn.close()

    except Exception as e:
        log("pipeline", f"[流水线] 执行错误: {str(e)}", "ERROR")
        update_exp("failed", "")
    finally:
        running_experiments.pop(experiment_id, None)


async def resume_pipeline(experiment_id: str, project_id: str, completed_stage: str, revision_note: str = ""):
    """Resume the FARS pipeline after approval."""
    try:
        stage_idx = STAGES.index(completed_stage) if completed_stage in STAGES else -1
    except ValueError:
        stage_idx = -1
    next_idx = stage_idx + 1
    if next_idx >= len(STAGES):
        return  # All stages already done
    await run_fars_pipeline(experiment_id, project_id, start_from_stage=next_idx, revision_note=revision_note)

