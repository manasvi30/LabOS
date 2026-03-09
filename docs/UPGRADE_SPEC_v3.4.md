# LabOS v3.4 Upgrade Specification

## Overview
Comprehensive upgrade with 4 major features. ALL changes must be backward-compatible with existing database data.

---

## Feature 1: Multi-LLM Profile System

### Database
Add new table `llm_profiles`:
```sql
CREATE TABLE IF NOT EXISTS llm_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,           -- e.g. "DeepSeek通用", "GPT代码分析", "Claude论文"
    task_type TEXT NOT NULL,      -- "general", "code", "paper", "experiment", "debug"
    api_url TEXT NOT NULL,        -- BaseURL
    api_key TEXT NOT NULL,
    model TEXT NOT NULL,
    system_prompt TEXT DEFAULT '', -- Base system prompt for this profile
    is_default INTEGER DEFAULT 0, -- 1 = default for this task_type
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Backend API
- `GET /api/llm-profiles` — list all profiles
- `POST /api/llm-profiles` — create profile
- `PUT /api/llm-profiles/{id}` — update profile  
- `DELETE /api/llm-profiles/{id}` — delete profile
- `POST /api/llm-profiles/{id}/test` — test connection

### call_llm refactor
Add optional `task_type` parameter to `call_llm()`. When provided, looks up the default profile for that task_type. Falls back to global LLM config (existing behavior) if no profile matches.

```python
async def call_llm(messages, system_prompt="", stream=False, task_type=None):
    if task_type:
        profile = get_llm_profile_for_task(task_type)
        if profile:
            # use profile's url/key/model/system_prompt
    # else fall back to existing configs table behavior
```

### Frontend Settings UI
In the settings page, replace the single "LLM 配置" section with a new section "LLM 配置" that shows a list of profiles as cards. Each card shows: name, task_type badge, model name, URL (masked). Actions: edit, delete, test.
- "添加配置" button opens modal with fields: name, task_type(dropdown), api_url, api_key, model, system_prompt(textarea), is_default(checkbox)
- Keep the existing single LLM config section but relabel it as "默认 LLM（全局回退）"

### Task types and their system prompts:
- `general`: 通用对话 — "你是 LabOS 平台的AI科研助手。"
- `code`: 代码分析 — "你是专业的代码分析和调试专家。擅长Python/PyTorch/ML框架。"  
- `paper`: 论文分析 — "你是学术论文分析专家。擅长文献综述、创新度评估、方法论分析。"
- `experiment`: 实验设计 — "你是实验设计专家。擅长假设生成、实验规划、结果分析。"

---

## Feature 2: Chat Dialogue Refactor

### Current state
Chat has 3 hint-chips: "分析架构", "设计实验", "规划优先级" — they just insert text.

### New design
Replace with task-type-aware chips that:
1. Set the active `task_type` for the session
2. Auto-select the corresponding LLM profile
3. Insert a smart base prompt

New chips:
- "代码分析" → task_type="code", prompt="分析项目代码架构，梳理关键模块和依赖关系"
- "论文调研" → task_type="paper", prompt="围绕当前项目方向，检索和分析相关论文"
- "实验设计" → task_type="experiment", prompt="基于当前假设，设计实验方案和评估指标"
- "通用对话" → task_type="general", prompt=""

### Frontend changes
- Add a task_type selector bar above the chat input (small pill buttons)
- When a chip is clicked, set `currentTaskType` and show which LLM profile is active
- The `llm-badge` at bottom should show the active profile name + model

### Backend changes  
- `POST /api/chat` already exists; add optional `task_type` field to ChatMessage model
- Pass `task_type` to `call_llm()` so the right profile is used

---

## Feature 3: Codex CLI Integration for Auto-Debug

### Core principle
LabOS treats Codex CLI as a READ-ONLY external plugin. LabOS must NEVER modify, upgrade, configure, or manage Codex. It only invokes `codex exec` via SSH and reads the output.

### Implementation
Replace the entire `auto_debug_loop()` function. New flow:

```python
async def auto_debug_with_codex(experiment_id, project_id, ssh_result, original_command, 
                                 plan_ctx, exp_name, log_fn, max_attempts=5):
    """
    Invoke Codex CLI on the remote server to debug failed experiment code.
    LabOS only reads Codex output — never modifies Codex itself.
    """
    error_info = ssh_result.get("error", "") or ssh_result.get("output", "")[-2000:]
    
    # Build the codex exec command
    codex_prompt = f"""The following experiment code failed. Analyze the error, fix the code, 
run the tests, and make it pass. Do NOT modify any tool configurations.

Experiment: {exp_name}
Original command: {original_command}
Error output:
{error_info[:3000]}

Plan context:
{plan_ctx[:1000]}

Fix the code and re-run: {original_command}
Stop when tests pass or after {max_attempts} attempts."""

    # Escape for shell
    escaped_prompt = codex_prompt.replace("'", "'\\''")
    
    codex_command = (
        f"cd /root && codex exec --full-auto --json "
        f"--sandbox workspace-write "
        f"-o /tmp/labos_debug_result.txt "
        f"'{escaped_prompt}'"
    )
    
    log_fn("debug", f"[自动Debug] 🔧 调用 Codex CLI 进行自动修复...")
    log_fn("debug", f"[自动Debug] 命令: codex exec --full-auto --json ...")
    
    # Execute via SSH (reuse existing ssh_execute)
    result = await ssh_execute(codex_command, project_id, experiment_id)
    
    # Parse JSONL output from codex
    codex_output = result.get("output", "")
    codex_exit = result.get("exit_code", -1)
    
    # Log codex events
    for line in codex_output.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            event_type = event.get("type", "")
            if event_type == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    log_fn("debug", f"[自动Debug] Codex: {item.get('text', '')[:300]}")
                elif item.get("type") == "command_execution":
                    log_fn("debug", f"[自动Debug] Codex执行: {item.get('command', '')[:200]}")
        except json.JSONDecodeError:
            if line and not line.startswith("{"):
                log_fn("debug", f"[自动Debug] {line[:200]}")
    
    # Read the final result file
    read_result_cmd = "cat /tmp/labos_debug_result.txt 2>/dev/null"
    final = await ssh_execute(read_result_cmd, project_id, experiment_id)
    final_text = final.get("output", "")
    
    # Now re-run the original command to verify
    log_fn("debug", f"[自动Debug] 验证修复: 重新执行原始命令...")
    verify_result = await ssh_execute(original_command, project_id, experiment_id)
    verify_exit = verify_result.get("exit_code", -1)
    
    if verify_exit == 0:
        log_fn("debug", f"[自动Debug] ✅ Codex 修复成功！")
        emit_event(f"exp_{experiment_id}", "debug_complete", {
            "experiment_id": experiment_id, "success": True,
            "method": "codex_cli", "codex_summary": final_text[:500],
        })
        return verify_result
    else:
        log_fn("debug", f"[自动Debug] ❌ Codex 修复后验证仍失败 (exit_code={verify_exit})", "WARN")
        verify_result["debug_summary"] = {
            "auto_debug": True, "method": "codex_cli",
            "success": False, "codex_output": final_text[:1000],
        }
        emit_event(f"exp_{experiment_id}", "debug_complete", {
            "experiment_id": experiment_id, "success": False,
            "method": "codex_cli",
        })
        return verify_result
```

### Settings UI changes
- Change the "自动Debug" section description to: "启用后，实验执行阶段若代码出错，将通过服务器上的 Codex CLI 自动修复（需服务器已安装 codex）"
- Remove "最大重试次数" field (Codex manages its own retry logic)
- Add a note: "LabOS 不会修改或升级 Codex，仅调用 codex exec 读取结果"

### Integration point
In `run_fars_pipeline()` Stage 3, replace:
```python
if needs_debug and AUTO_DEBUG_ENABLED and use_llm:
    ssh_result = await auto_debug_loop(...)
```
with:
```python
if needs_debug and AUTO_DEBUG_ENABLED:
    ssh_result = await auto_debug_with_codex(...)
```
Note: remove `use_llm` check — Codex doesn't need LabOS's LLM config.

---

## Feature 4: Report System Refactor

### Core requirements
1. Each pipeline stage (1-3) generates its own stage report immediately upon completion
2. Stage 4 (writing) generates a cumulative FULL report that includes everything  
3. Reports are PERSISTENT — refresh, restart, or re-running experiments must NOT overwrite or delete them
4. Reports accumulate by timestamp — new runs add new reports, old ones stay
5. Reports can be manually deleted by user

### Database
Add new table `stage_reports`:
```sql
CREATE TABLE IF NOT EXISTS stage_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    stage TEXT NOT NULL,           -- "ideation", "planning", "experiment", "writing"
    report_type TEXT NOT NULL,     -- "stage" for stages 1-3, "full" for stage 4
    title TEXT DEFAULT '',
    content TEXT NOT NULL,         -- Markdown content
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_stage_reports_exp ON stage_reports(experiment_id);
```

### Report generation per stage
- Stage 1 (ideation): Generate "调研报告" — hypothesis analysis, related work survey, novelty assessment
- Stage 2 (planning): Generate "分析报告" — experiment design analysis, feasibility, innovation assessment
- Stage 3 (experiment): Generate "实验报告" — execution results, metrics, error analysis
- Stage 4 (writing): Generate "综合报告" — full cumulative report summarizing the entire experimental cycle

### Backend API
- `GET /api/experiments/{id}/reports` — list all reports for an experiment (ordered by created_at)
- `DELETE /api/reports/{id}` — delete a specific report

### Pipeline integration
After each stage completes (BEFORE approval check), generate and store a stage report:
```python
# After ideation completes
stage_report = await generate_stage_report("ideation", result, exp_name, ...)
store_stage_report(experiment_id, project_id, "ideation", "stage", "调研报告", stage_report)

# After planning completes  
stage_report = await generate_stage_report("planning", plan_result, exp_name, ...)
store_stage_report(experiment_id, project_id, "planning", "stage", "分析报告", stage_report)

# After experiment completes
stage_report = await generate_stage_report("experiment", metrics_json, exp_name, ...)
store_stage_report(experiment_id, project_id, "experiment", "stage", "实验报告", stage_report)

# Stage 4: full report
full_report = await generate_full_report(all_stages_data, exp_name, ...)
store_stage_report(experiment_id, project_id, "writing", "full", "综合报告", full_report)
```

### Frontend changes
In the experiment detail panel, change the "报告" tab:
- Show a timeline of all reports, newest first
- Each report card: [timestamp] [stage badge] [title] — expandable to show full content
- Delete button (trash icon) on each report card
- Reports from different runs of the same experiment all appear in the list

---

## Version bump
- api_server.py docstring: v3.4.0
- index.html title: v3.4
- sidebar logo-sub: v3.4  
- FastAPI title: "LabOS v3.4"
