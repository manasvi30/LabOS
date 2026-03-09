#!/usr/bin/env python3
"""
LabOS v3.4.1 — 交互式科研自动化平台后端
新增：Multi-LLM配置 · Codex全自动化实验 · 阶段报告系统 · 任务类型感知聊天
v3.4.1: 实验阶段重构 — clone仓库→Codex写代码→运行→自动修复→综合审批
"""
import asyncio
import json
import sqlite3
import time
import uuid
import os
import re
import math
import hashlib
import subprocess
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Optional, List

try:
    import paramiko
except ImportError:
    paramiko = None

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import httpx

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = "/home/user/workspace/memrl-fars-v2/fars_v2.db"

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        -- 系统配置（LLM/服务器/AutoDL等）
        CREATE TABLE IF NOT EXISTS configs (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            category TEXT DEFAULT 'general',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 项目（每个IDEA/仓库一个项目）
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            repo_url TEXT DEFAULT '',
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 项目记忆（长期知识库，按project分区）
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            content TEXT NOT NULL,
            source TEXT DEFAULT 'system',
            relevance_score REAL DEFAULT 1.0,
            embedding TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        -- 对话历史（按project + session分区）
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        -- FARS实验（项目队列中的实验任务）
        CREATE TABLE IF NOT EXISTS experiments (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            name TEXT NOT NULL,
            hypothesis TEXT DEFAULT '',
            plan TEXT DEFAULT '',
            status TEXT DEFAULT 'queued',
            current_stage TEXT DEFAULT '',
            priority INTEGER DEFAULT 0,
            result_summary TEXT DEFAULT '',
            metrics TEXT DEFAULT '{}',
            report TEXT DEFAULT '',
            feedback TEXT DEFAULT '',
            execution_mode TEXT DEFAULT 'simulate',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        -- 流水线步骤
        CREATE TABLE IF NOT EXISTS pipeline_steps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            step_index INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            input_data TEXT DEFAULT '',
            output_data TEXT DEFAULT '',
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY(experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
        );

        -- 实时日志
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT,
            project_id TEXT,
            stage TEXT DEFAULT '',
            level TEXT DEFAULT 'INFO',
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 知识发现
        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            category TEXT DEFAULT 'observation',
            content TEXT NOT NULL,
            confidence REAL DEFAULT 0.5,
            source_stage TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
        );

        -- 论文缓存
        CREATE TABLE IF NOT EXISTS papers (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            title TEXT NOT NULL,
            authors TEXT DEFAULT '',
            year INTEGER DEFAULT 0,
            abstract TEXT DEFAULT '',
            url TEXT DEFAULT '',
            citation_count INTEGER DEFAULT 0,
            venue TEXT DEFAULT '',
            relevance_note TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        -- GitHub 代码分析缓存
        CREATE TABLE IF NOT EXISTS code_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL,
            repo_url TEXT NOT NULL,
            file_tree TEXT DEFAULT '',
            analysis TEXT DEFAULT '',
            readme_content TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        -- 阶段审批记录
        CREATE TABLE IF NOT EXISTS stage_approvals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            stage_output TEXT DEFAULT '',
            reviewer_comment TEXT DEFAULT '',
            decided_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
        );

        -- Multi-LLM 配置档案
        CREATE TABLE IF NOT EXISTS llm_profiles (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            task_type TEXT NOT NULL,
            api_url TEXT NOT NULL,
            api_key TEXT NOT NULL,
            model TEXT NOT NULL,
            system_prompt TEXT DEFAULT '',
            is_default INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- 阶段报告（持久化，不被覆盖）
        CREATE TABLE IF NOT EXISTS stage_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            report_type TEXT NOT NULL,
            title TEXT DEFAULT '',
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
        );

        -- 创建索引
        CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_id);
        CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(project_id, session_id);
        CREATE INDEX IF NOT EXISTS idx_experiments_project ON experiments(project_id);
        CREATE INDEX IF NOT EXISTS idx_logs_experiment ON logs(experiment_id);
        CREATE INDEX IF NOT EXISTS idx_findings_experiment ON findings(experiment_id);
        CREATE INDEX IF NOT EXISTS idx_papers_project ON papers(project_id);
        CREATE INDEX IF NOT EXISTS idx_stage_approvals_exp ON stage_approvals(experiment_id);
        CREATE INDEX IF NOT EXISTS idx_stage_reports_exp ON stage_reports(experiment_id);
    """)
    # Ensure embedding column exists (migration)
    try:
        conn.execute("SELECT embedding FROM memories LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE memories ADD COLUMN embedding TEXT DEFAULT ''")
    # Migration: add execution_mode to experiments
    try:
        conn.execute("SELECT execution_mode FROM experiments LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE experiments ADD COLUMN execution_mode TEXT DEFAULT 'simulate'")
    # Migration: add llm_profiles table (for upgrades from older versions)
    try:
        conn.execute("SELECT id FROM llm_profiles LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_profiles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                task_type TEXT NOT NULL,
                api_url TEXT NOT NULL,
                api_key TEXT NOT NULL,
                model TEXT NOT NULL,
                system_prompt TEXT DEFAULT '',
                is_default INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
    # Migration: add stage_reports table (for upgrades from older versions)
    try:
        conn.execute("SELECT id FROM stage_reports LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stage_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                report_type TEXT NOT NULL,
                title TEXT DEFAULT '',
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(experiment_id) REFERENCES experiments(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stage_reports_exp ON stage_reports(experiment_id)")
    conn.commit()
    conn.close()

# ---------------------------------------------------------------------------
# LLM Profile Helpers
# ---------------------------------------------------------------------------
def get_llm_profile_for_task(task_type: str) -> Optional[dict]:
    """Return the default LLM profile for a given task_type, or None if not found."""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM llm_profiles WHERE task_type=? AND is_default=1 LIMIT 1",
            (task_type,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def seed_default_llm_profiles():
    """
    Migrate existing global LLM config to a '\u9ed8\u8ba4\u901a\u7528' profile.
    Only runs if no profiles exist yet.
    """
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) c FROM llm_profiles").fetchone()["c"]
    if count > 0:
        conn.close()
        return

    # Read existing global LLM config
    rows = conn.execute("SELECT key, value FROM configs WHERE category='llm'").fetchall()
    config = {r["key"]: r["value"] for r in rows}
    api_url = config.get("llm_api_url", "").strip()
    api_key = config.get("llm_api_key", "").strip()
    model = config.get("llm_model", "deepseek-chat").strip() or "deepseek-chat"

    if not api_url:
        conn.close()
        return

    now = datetime.now(timezone.utc).isoformat()
    task_types = [
        ("general",     "\u9ed8\u8ba4\u901a\u7528",   "\u4f60\u662f LabOS \u5e73\u53f0\u7684AI\u79d1\u7814\u52a9\u624b\u3002"),
        ("code",        "\u4ee3\u7801\u5206\u6790",   "\u4f60\u662f\u4e13\u4e1a\u7684\u4ee3\u7801\u5206\u6790\u548c\u8c03\u8bd5\u4e13\u5bb6\u3002\u64c5\u957fPython/PyTorch/ML\u6846\u67b6\u3002"),
        ("paper",       "\u8bba\u6587\u5206\u6790",   "\u4f60\u662f\u5b66\u672f\u8bba\u6587\u5206\u6790\u4e13\u5bb6\u3002\u64c5\u957f\u6587\u732e\u7efc\u8ff0\u3001\u521b\u65b0\u5ea6\u8bc4\u4f30\u3001\u65b9\u6cd5\u8bba\u5206\u6790\u3002"),
        ("experiment",  "\u5b9e\u9a8c\u8bbe\u8ba1",   "\u4f60\u662f\u5b9e\u9a8c\u8bbe\u8ba1\u4e13\u5bb6\u3002\u64c5\u957f\u5047\u8bbe\u751f\u6210\u3001\u5b9e\u9a8c\u89c4\u5212\u3001\u7ed3\u679c\u5206\u6790\u3002"),
    ]

    for task_type, name, system_prompt in task_types:
        pid = f"llmprof_{uuid.uuid4().hex[:8]}"
        conn.execute(
            "INSERT INTO llm_profiles (id, name, task_type, api_url, api_key, model, system_prompt, is_default, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pid, name, task_type, api_url, api_key, model, system_prompt, 1, now, now),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# LLM Client (configurable, OpenAI-compatible)
# ---------------------------------------------------------------------------
async def call_llm(messages: list, system_prompt: str = "", stream: bool = False, task_type: Optional[str] = None):
    """Call configured LLM API (OpenAI-compatible). Optionally uses a task-specific profile."""
    # Try to resolve a task-specific LLM profile first
    profile = None
    if task_type:
        profile = get_llm_profile_for_task(task_type)

    if profile:
        api_url = profile["api_url"].strip()
        api_key = profile["api_key"].strip()
        model = profile["model"].strip()
        # Merge profile's system_prompt with caller's system_prompt (caller wins if provided)
        effective_system = system_prompt if system_prompt else profile.get("system_prompt", "")
    else:
        # Fall back to global configs table
        conn = get_db()
        rows = conn.execute("SELECT key, value FROM configs WHERE category='llm'").fetchall()
        conn.close()
        config = {r["key"]: r["value"] for r in rows}

        api_url = config.get("llm_api_url", "").strip()
        api_key = config.get("llm_api_key", "").strip()
        model = config.get("llm_model", "").strip()
        effective_system = system_prompt

    if not api_url or not api_key:
        raise HTTPException(status_code=400, detail="LLM 未配置。请先在设置中配置 LLM API 地址和密钥。")

    if not model:
        model = "deepseek-chat"

    base_url = api_url.rstrip("/")
    if not base_url.endswith("/chat/completions"):
        if base_url.endswith("/v1"):
            base_url += "/chat/completions"
        else:
            base_url += "/v1/chat/completions"

    full_messages = []
    if effective_system:
        full_messages.append({"role": "system", "content": effective_system})
    full_messages.extend(messages)

    payload = {
        "model": model,
        "messages": full_messages,
        "temperature": 0.7,
        "max_tokens": 4096,
        "stream": stream,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if stream:
        async def stream_generator():
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream("POST", base_url, json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        yield f'data: {json.dumps({"error": f"LLM API 错误 ({resp.status_code}): {body.decode()}"})}\\n\\n'
                        return
                    async for line in resp.aiter_lines():
                        if line.startswith("data: "):
                            chunk = line[6:]
                            if chunk.strip() == "[DONE]":
                                yield "data: [DONE]\n\n"
                                return
                            yield f"data: {chunk}\n\n"
        return stream_generator()
    else:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(base_url, json=payload, headers=headers)
            if resp.status_code != 200:
                raise HTTPException(status_code=422, detail=f"LLM API 错误 ({resp.status_code}): {resp.text[:500]}")
            data = resp.json()
            return data["choices"][0]["message"]["content"]

# ---------------------------------------------------------------------------
# Embedding Client
# ---------------------------------------------------------------------------
async def call_embedding(texts: list[str]) -> list[list[float]]:
    """Call configured Embedding API (OpenAI-compatible)."""
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM configs WHERE category='embedding'").fetchall()
    conn.close()
    config = {r["key"]: r["value"] for r in rows}

    api_url = config.get("embedding_api_url", "").strip()
    api_key = config.get("embedding_api_key", "").strip()
    model = config.get("embedding_model", "").strip() or "text-embedding-3-small"

    if not api_url or not api_key:
        return []

    base_url = api_url.rstrip("/")
    if not base_url.endswith("/embeddings"):
        if base_url.endswith("/v1"):
            base_url += "/embeddings"
        else:
            base_url += "/v1/embeddings"

    payload = {"model": model, "input": texts}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(base_url, json=payload, headers=headers)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [item["embedding"] for item in data.get("data", [])]

def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

# ---------------------------------------------------------------------------
# Memory Engine (with semantic search)
# ---------------------------------------------------------------------------
def store_memory(project_id: str, content: str, category: str = "general", source: str = "system"):
    conn = get_db()
    conn.execute(
        "INSERT INTO memories (project_id, category, content, source) VALUES (?,?,?,?)",
        (project_id, category, content, source),
    )
    conn.commit()
    conn.close()

async def store_memory_with_embedding(project_id: str, content: str, category: str = "general", source: str = "system"):
    """Store memory and compute embedding if available."""
    conn = get_db()
    embedding_json = ""
    try:
        embeddings = await call_embedding([content])
        if embeddings:
            embedding_json = json.dumps(embeddings[0])
    except Exception:
        pass
    conn.execute(
        "INSERT INTO memories (project_id, category, content, source, embedding) VALUES (?,?,?,?,?)",
        (project_id, category, content, source, embedding_json),
    )
    conn.commit()
    conn.close()

def retrieve_memories(project_id: str, limit: int = 20, category: str = None):
    conn = get_db()
    if category:
        rows = conn.execute(
            "SELECT * FROM memories WHERE project_id=? AND category=? ORDER BY relevance_score DESC, created_at DESC LIMIT ?",
            (project_id, category, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memories WHERE project_id=? ORDER BY relevance_score DESC, created_at DESC LIMIT ?",
            (project_id, limit),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

async def semantic_search_memories(project_id: str, query: str, top_k: int = 10) -> list[dict]:
    """Search memories by semantic similarity."""
    query_embeddings = await call_embedding([query])
    if not query_embeddings:
        # Fallback: keyword search
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM memories WHERE project_id=? AND content LIKE ? ORDER BY relevance_score DESC LIMIT ?",
            (project_id, f"%{query}%", top_k),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    query_vec = query_embeddings[0]
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM memories WHERE project_id=? AND embedding != ''",
        (project_id,),
    ).fetchall()
    conn.close()

    scored = []
    for r in rows:
        d = dict(r)
        try:
            mem_vec = json.loads(d["embedding"])
            sim = cosine_similarity(query_vec, mem_vec)
            d["similarity"] = sim
            scored.append(d)
        except (json.JSONDecodeError, KeyError):
            pass

    # Also add keyword-matched memories without embeddings
    conn = get_db()
    no_embed_rows = conn.execute(
        "SELECT * FROM memories WHERE project_id=? AND (embedding = '' OR embedding IS NULL) AND content LIKE ?",
        (project_id, f"%{query}%"),
    ).fetchall()
    conn.close()
    for r in no_embed_rows:
        d = dict(r)
        d["similarity"] = 0.5  # moderate score for keyword match
        scored.append(d)

    scored.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    return scored[:top_k]

def build_memory_context(project_id: str) -> str:
    """Build a context string from project memories for LLM."""
    memories = retrieve_memories(project_id, limit=30)
    if not memories:
        return ""
    sections = {}
    for m in memories:
        cat = m["category"]
        if cat not in sections:
            sections[cat] = []
        sections[cat].append(m["content"])
    parts = []
    for cat, items in sections.items():
        parts.append(f"## {cat}")
        for item in items:
            parts.append(f"- {item}")
    return "\n".join(parts)

# ---------------------------------------------------------------------------
# SSE Event Bus
# ---------------------------------------------------------------------------
event_subscribers: dict[str, list[asyncio.Queue]] = {}

def emit_event(channel: str, event_type: str, data: dict):
    if channel in event_subscribers:
        payload = json.dumps({"type": event_type, **data}, ensure_ascii=False)
        for q in event_subscribers[channel]:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

# ---------------------------------------------------------------------------
# FARS Workflow Engine
# ---------------------------------------------------------------------------
STAGES = ["ideation", "planning", "experiment", "writing"]
STAGE_LABELS = {
    "ideation": "假设生成",
    "planning": "实验规划",
    "experiment": "实验执行",
    "writing": "报告撰写",
}

running_experiments: dict[str, bool] = {}

# Auto-debug configuration
AUTO_DEBUG_ENABLED = True     # Global toggle


def log_to_db(experiment_id: str, project_id: str, stage: str, message: str, level: str = "INFO"):
    """Synchronous logging helper for use outside the pipeline async context."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO logs (experiment_id, project_id, stage, level, message, created_at) VALUES (?,?,?,?,?,?)",
        (experiment_id, project_id, stage, level, message, now),
    )
    conn.commit()
    conn.close()
    emit_event(f"exp_{experiment_id}", "log", {
        "experiment_id": experiment_id, "stage": stage, "level": level,
        "message": message, "created_at": now,
    })


def get_prev_stage(stage: str) -> str:
    """Return the stage before the given stage, or '' if already first."""
    try:
        idx = STAGES.index(stage)
        return STAGES[idx - 1] if idx > 0 else ""
    except ValueError:
        return ""


async def ensure_repo_on_server(
    repo_url: str,
    project_id: str,
    experiment_id: str,
    log_fn,
    work_dir: str = "/root",
) -> dict:
    """
    Ensure the GitHub repo is cloned on the remote server.
    If already present, do a git pull. Returns {"repo_dir": str, "success": bool}.
    """
    if not repo_url:
        return {"repo_dir": "", "success": False, "error": "项目未配置 GitHub 仓库 URL"}

    # Extract repo name from URL
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", repo_url.strip("/"))
    if not match:
        return {"repo_dir": "", "success": False, "error": f"无效的 GitHub URL: {repo_url}"}
    repo_name = match.group(2)
    repo_dir = f"{work_dir}/{repo_name}"

    # Check if repo dir exists
    log_fn("experiment", f"[仓库准备] 检查服务器上是否存在 {repo_name}...")
    check_cmd = f"test -d {repo_dir}/.git && echo 'EXISTS' || echo 'NOT_FOUND'"
    check_result = await ssh_execute(check_cmd, project_id, experiment_id)
    check_output = (check_result.get("output", "") or "").strip()

    if "EXISTS" in check_output:
        log_fn("experiment", f"[仓库准备] 仓库已存在，执行 git pull...")
        pull_cmd = f"cd {repo_dir} && git pull --ff-only 2>&1 | tail -5"
        pull_result = await ssh_execute(pull_cmd, project_id, experiment_id)
        log_fn("experiment", f"[仓库准备] git pull 完成 (exit={pull_result.get('exit_code', '?')})")
        return {"repo_dir": repo_dir, "success": True, "repo_name": repo_name}
    else:
        log_fn("experiment", f"[仓库准备] 仓库不存在，克隆 {repo_url}...")
        clone_cmd = f"cd {work_dir} && git clone {repo_url} 2>&1 | tail -5"
        clone_result = await ssh_execute(clone_cmd, project_id, experiment_id)
        if clone_result.get("exit_code", -1) != 0:
            error_msg = clone_result.get("error", "") or clone_result.get("output", "")
            log_fn("experiment", f"[仓库准备] 克隆失败: {error_msg[:300]}", "ERROR")
            return {"repo_dir": "", "success": False, "error": error_msg}
        log_fn("experiment", f"[仓库准备] ✓ 仓库克隆成功: {repo_dir}")
        # Install dependencies if requirements.txt exists
        install_cmd = f"cd {repo_dir} && test -f requirements.txt && pip install -r requirements.txt 2>&1 | tail -3 || echo 'no requirements.txt'"
        install_result = await ssh_execute(install_cmd, project_id, experiment_id)
        log_fn("experiment", f"[仓库准备] 依赖安装完成")
        return {"repo_dir": repo_dir, "success": True, "repo_name": repo_name}


async def codex_experiment_run(
    experiment_id: str,
    project_id: str,
    repo_dir: str,
    exp_name: str,
    hypothesis_ctx: str,
    plan_ctx: str,
    memory_ctx: str,
    revision_note: str,
    log_fn,
) -> dict:
    """
    Full experiment cycle:
    1. Check if Codex CLI is available on the server
    2a. If yes → Codex full-auto mode
    2b. If no → LLM generates experiment script, SSH executes, auto-fix loop
    3. Returns comprehensive results for approval

    LabOS NEVER modifies Codex — only invokes and reads results.
    """
    log_fn("experiment", "[实验执行] 🔧 准备实验环境...")

    # ---- Step 0: Wake up Codex CLI ----
    # All SSH commands now run via bash -l (login shell) which loads .bashrc/.profile
    # So npm global binaries should be in PATH automatically
    log_fn("experiment", "[实验执行] 唤醒 Codex CLI...")
    codex_check = await ssh_execute("which codex 2>/dev/null && codex --version 2>/dev/null || echo 'CODEX_NOT_FOUND'", project_id, experiment_id)
    codex_output = (codex_check.get("output", "") or "") + (codex_check.get("error", "") or "")
    codex_available = "CODEX_NOT_FOUND" not in codex_output

    # Resolve actual codex path for later use
    codex_path = "codex"  # default
    if codex_available:
        path_result = await ssh_execute("which codex", project_id, experiment_id)
        resolved = (path_result.get("output", "") or "").strip()
        if resolved and "/" in resolved:
            codex_path = resolved
        log_fn("experiment", f"[实验执行] ✓ Codex CLI 找到: {codex_path}")
    else:
        log_fn("experiment", "[实验执行] Codex CLI 未找到，将使用 LLM+SSH 模式")

    # ---- Step 1: Get repo structure for context ----
    log_fn("experiment", "[实验执行] 📂 读取仓库结构...")
    tree_cmd = f"cd {repo_dir} && find . -maxdepth 3 -type f -name '*.py' -o -name '*.yaml' -o -name '*.yml' -o -name '*.json' -o -name '*.txt' -o -name '*.md' -o -name '*.sh' -o -name '*.cfg' | head -80"
    tree_result = await ssh_execute(tree_cmd, project_id, experiment_id)
    file_tree = tree_result.get("output", "")[:2000]
    log_fn("experiment", f"[实验执行] 找到文件 {len(file_tree.split(chr(10)))} 个")

    # Read README and key files for context
    readme_cmd = f"cd {repo_dir} && cat README.md 2>/dev/null | head -150 || cat readme.md 2>/dev/null | head -150 || echo 'NO_README'"
    readme_result = await ssh_execute(readme_cmd, project_id, experiment_id)
    readme_text = readme_result.get("output", "")[:3000]

    # Check for common entry points
    entry_cmd = f"cd {repo_dir} && head -50 train.py 2>/dev/null || head -50 main.py 2>/dev/null || head -50 run.py 2>/dev/null || echo 'NO_ENTRY_SCRIPT'"
    entry_result = await ssh_execute(entry_cmd, project_id, experiment_id)
    entry_text = entry_result.get("output", "")[:2000]

    # Check GPU availability
    gpu_cmd = "nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo 'NO_GPU'"
    gpu_result = await ssh_execute(gpu_cmd, project_id, experiment_id)
    gpu_info = gpu_result.get("output", "").strip()

    # Build context
    revision_section = ""
    if revision_note:
        revision_section = f"""\n\n## 用户审批反馈（必须整合）\n用户对上一轮实验给出了以下修改意见，你必须根据这些意见调整实验：\n{revision_note}\n"""

    repo_context = f"""## 仓库结构\n{file_tree}\n\n## README\n{readme_text}\n\n## 入口脚本预览\n{entry_text}\n\n## GPU信息\n{gpu_info}"""

    # ---- Branch A: Codex full-auto ----
    if codex_available:
        log_fn("experiment", "[实验执行] ✓ Codex CLI 可用，启动全自动模式")
        return await _run_with_codex(
            experiment_id, project_id, repo_dir, exp_name,
            hypothesis_ctx, plan_ctx, memory_ctx, revision_section,
            repo_context, log_fn, codex_path=codex_path,
        )

    # ---- Branch B: LLM + SSH fallback ----
    log_fn("experiment", "[实验执行] Codex CLI 不可用，使用 LLM + SSH 执行模式")
    return await _run_with_llm_ssh(
        experiment_id, project_id, repo_dir, exp_name,
        hypothesis_ctx, plan_ctx, memory_ctx, revision_section,
        repo_context, gpu_info, log_fn,
    )


async def _run_with_codex(
    experiment_id, project_id, repo_dir, exp_name,
    hypothesis_ctx, plan_ctx, memory_ctx, revision_section,
    repo_context, log_fn, codex_path: str = "codex",
) -> dict:
    """Execute experiment via Codex CLI (full-auto mode). LabOS never modifies Codex."""
    codex_prompt = f"""You are running a scientific experiment for the LabOS platform.
The project repository is at: {repo_dir}

## Experiment: {exp_name}

## Research Hypothesis
{hypothesis_ctx[:2000]}

## Experiment Plan
{plan_ctx[:2000]}

## Project Context
{memory_ctx[:1000]}

{repo_context[:2000]}{revision_section}

## Your Task (FULL AUTO)
1. Read the project code structure to understand the codebase
2. Write the experiment script based on hypothesis and plan
3. Run the experiment
4. If it fails, fix and re-run
5. Write summary to /tmp/labos_experiment_result.txt

IMPORTANT: Work inside {repo_dir}. Use existing project structure.
"""
    # ---- Write prompt to remote temp file to avoid shell escaping issues ----
    # Using SFTP to write directly — no shell escaping needed at all
    try:
        cfg = get_ssh_config()
        sftp_client = paramiko.SSHClient()
        sftp_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: sftp_client.connect(
            hostname=cfg["host"], port=cfg["port"],
            username=cfg["user"], password=cfg["password"], timeout=30,
        ))
        sftp = sftp_client.open_sftp()
        with sftp.file("/tmp/labos_codex_prompt.txt", "w") as f:
            f.write(codex_prompt)
        sftp.close()
        sftp_client.close()
        log_fn("experiment", f"[Codex] ✅ Prompt 已通过 SFTP 写入远程 /tmp/labos_codex_prompt.txt ({len(codex_prompt)} 字符)")
    except Exception as e:
        log_fn("experiment", f"[Codex] ⚠ SFTP 写入失败: {e}，尝试 base64 方法", "WARN")
        import base64 as _b64
        prompt_b64 = _b64.b64encode(codex_prompt.encode("utf-8")).decode("ascii")
        # Split into chunks to avoid shell line length limits
        chunk_size = 4000
        chunks = [prompt_b64[i:i+chunk_size] for i in range(0, len(prompt_b64), chunk_size)]
        await ssh_execute(f"echo -n '' > /tmp/labos_codex_prompt_b64.txt", project_id, experiment_id)
        for chunk in chunks:
            await ssh_execute(f"echo -n '{chunk}' >> /tmp/labos_codex_prompt_b64.txt", project_id, experiment_id)
        await ssh_execute("base64 -d /tmp/labos_codex_prompt_b64.txt > /tmp/labos_codex_prompt.txt", project_id, experiment_id)
        log_fn("experiment", f"[Codex] ✅ Prompt 已通过 base64 写入 ({len(codex_prompt)} 字符, {len(chunks)} 块)")

    codex_command = (
        f"cd {repo_dir} && cat /tmp/labos_codex_prompt.txt | {codex_path} exec --full-auto --json "
        f"--skip-git-repo-check "
        f"-o /tmp/labos_experiment_result.txt "
        f"- 2>&1"
    )
    log_fn("experiment", f"[Codex] 🚀 启动 Codex CLI (full-auto)... prompt长度={len(codex_prompt)}")

    # Use streaming SSH that parses JSONL events in real-time
    result = await ssh_execute_codex_streaming(
        codex_command, project_id, experiment_id, log_fn
    )

    codex_output = result.get("output", "")
    codex_events = result.get("codex_events", [])
    last_agent_message = result.get("last_agent_message", "")
    all_agent_messages = result.get("all_agent_messages", [])
    all_commands = result.get("all_commands", [])

    read_cmd = "cat /tmp/labos_experiment_result.txt 2>/dev/null"
    result_file = await ssh_execute(read_cmd, project_id, experiment_id)
    result_text = result_file.get("output", "")

    metrics_cmd = f"cd {repo_dir} && cat results.json experiment_results.json metrics.json 2>/dev/null | head -100"
    metrics_file = await ssh_execute(metrics_cmd, project_id, experiment_id)
    metrics_text = metrics_file.get("output", "")

    codex_exit = result.get("exit_code", -1)
    success = codex_exit == 0 and len(result_text) > 0

    experiment_result = {
        "status": "real", "method": "codex_full_auto",
        "codex_exit_code": codex_exit, "success": success,
        "result_summary": result_text[:3000] if result_text else "Codex未生成结果文件",
        "metrics_raw": metrics_text[:1000] if metrics_text else "",
        "total_codex_events": len(codex_events), "repo_dir": repo_dir,
        "codex_last_feedback": last_agent_message[:3000] if last_agent_message else "",
        "all_agent_messages": all_agent_messages[-10:],  # keep last 10 messages
        "all_commands": all_commands[-20:],  # keep last 20 commands
    }
    _extract_metrics(codex_output + "\n" + result_text + "\n" + metrics_text, experiment_result)

    if success:
        log_fn("experiment", "[Codex] ✅ 实验完成")
    else:
        log_fn("experiment", f"[Codex] ⚠ 实验未完全成功 (exit={codex_exit})", "WARN")
    emit_event(f"exp_{experiment_id}", "codex_complete", {"experiment_id": experiment_id, "success": success, "method": "codex_full_auto"})
    return experiment_result


async def _run_with_llm_ssh(
    experiment_id, project_id, repo_dir, exp_name,
    hypothesis_ctx, plan_ctx, memory_ctx, revision_section,
    repo_context, gpu_info, log_fn,
) -> dict:
    """Fallback: use LLM to generate experiment code, then execute via SSH with auto-fix loop."""
    MAX_ATTEMPTS = 3
    has_gpu = "NO_GPU" not in gpu_info

    # Step 1: Ask LLM to generate an experiment script
    log_fn("experiment", "[LLM+SSH] 📝 请求 LLM 生成实验脚本...")
    gen_prompt = f"""你是一个科研实验自动化专家。请根据以下信息，生成一个可以直接在服务器上运行的 Python 实验脚本。

## 实验名称
{exp_name}

## 研究假设
{hypothesis_ctx[:1500]}

## 实验计划
{plan_ctx[:1500]}

## 项目上下文
{memory_ctx[:800]}

{repo_context[:3000]}{revision_section}

## 服务器环境
- 工作目录: {repo_dir}
- GPU: {gpu_info}
- Python 已安装

## 要求
1. 生成一个完整的 Python 脚本（labos_experiment.py），可以直接执行
2. 脚本必须基于仓库现有代码结构（import 仓库中的模块）
3. 脚本末尾必须打印 "=== LABOS_RESULT_START ===" 然后打印一个JSON对象包含实验结果（metrics、findings等），然后打印 "=== LABOS_RESULT_END ==="
4. 如果需要训练，先用小规模数据做快速验证（{'使用GPU' if has_gpu else '仅CPU'}）
5. 处理好所有 import 和路径问题

只返回Python代码，不要额外解释。代码用 ```python 和 ``` 包裹。"""

    all_outputs = []
    experiment_script = ""
    last_error = ""
    final_exit = -1
    attempt_logs = []

    try:
        script_response = await call_llm(
            [{"role": "user", "content": gen_prompt}],
            system_prompt="你是科研实验代码生成专家。只输出Python代码，用```python包裹。",
            task_type="code",
        )
        # Extract code block
        code_match = re.search(r"```python\s*\n(.*?)\n```", script_response, re.DOTALL)
        if code_match:
            experiment_script = code_match.group(1)
        else:
            experiment_script = script_response.strip()
        log_fn("experiment", f"[LLM+SSH] ✓ LLM 生成了实验脚本 ({len(experiment_script)} 字符)")
    except Exception as e:
        log_fn("experiment", f"[LLM+SSH] LLM 生成脚本失败: {str(e)}", "ERROR")
        return {
            "status": "real", "method": "llm_ssh", "success": False,
            "result_summary": f"LLM 生成实验脚本失败: {str(e)}",
            "repo_dir": repo_dir,
        }

    # Step 2: Execute with auto-fix loop
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log_fn("experiment", f"[LLM+SSH] 🔄 第 {attempt}/{MAX_ATTEMPTS} 次执行...")

        # Write script to server
        escaped_script = experiment_script.replace("'", "'\\'\''")
        write_cmd = f"cat > {repo_dir}/labos_experiment.py << 'LABOS_SCRIPT_EOF'\n{experiment_script}\nLABOS_SCRIPT_EOF"
        await ssh_execute(write_cmd, project_id, experiment_id)

        # Execute script
        run_cmd = f"cd {repo_dir} && python labos_experiment.py 2>&1 | tail -200"
        run_result = await ssh_execute(run_cmd, project_id, experiment_id)
        run_exit = run_result.get("exit_code", -1)
        run_output = run_result.get("output", "")
        run_error = run_result.get("error", "")
        all_outputs.append(run_output)
        final_exit = run_exit

        attempt_log = f"第{attempt}次: exit={run_exit}"
        if run_exit == 0:
            log_fn("experiment", f"[LLM+SSH] ✅ 第 {attempt} 次执行成功 (exit=0)")
            attempt_logs.append(f"{attempt_log} ✅成功")
            break
        else:
            error_preview = (run_error or run_output)[-500:]
            log_fn("experiment", f"[LLM+SSH] ⚠ 第 {attempt} 次失败 (exit={run_exit})", "WARN")
            log_fn("experiment", f"[LLM+SSH] 错误预览: {error_preview[:200]}")
            last_error = error_preview
            attempt_logs.append(f"{attempt_log} ❌ {error_preview[:100]}")

            if attempt < MAX_ATTEMPTS:
                # Ask LLM to fix
                log_fn("experiment", f"[LLM+SSH] 🔧 请求 LLM 修复脚本 (第{attempt}次错误)...")
                try:
                    fix_prompt = f"""以下 Python 实验脚本执行失败。请修复它。

## 当前脚本
```python
{experiment_script[:3000]}
```

## 错误输出
```
{error_preview[:1500]}
```

## 工作目录
{repo_dir}

请返回修复后的完整脚本（用```python包裹），不要解释。"""
                    fix_response = await call_llm(
                        [{"role": "user", "content": fix_prompt}],
                        system_prompt="你是Debug专家。只输出修复后的完整Python代码，用```python包裹。",
                        task_type="code",
                    )
                    code_match = re.search(r"```python\s*\n(.*?)\n```", fix_response, re.DOTALL)
                    if code_match:
                        experiment_script = code_match.group(1)
                    else:
                        experiment_script = fix_response.strip()
                    log_fn("experiment", "[LLM+SSH] ✓ LLM 返回修复后脚本")
                except Exception as e:
                    log_fn("experiment", f"[LLM+SSH] LLM 修复失败: {str(e)}", "WARN")
                    break

    # Step 3: Parse results from output
    combined_output = "\n".join(all_outputs)
    result_json_text = ""
    result_match = re.search(
        r"=== LABOS_RESULT_START ===\s*\n(.*?)\n\s*=== LABOS_RESULT_END ===",
        combined_output, re.DOTALL,
    )
    parsed_metrics = {}
    if result_match:
        try:
            parsed_metrics = json.loads(result_match.group(1).strip())
            result_json_text = result_match.group(1).strip()
        except json.JSONDecodeError:
            result_json_text = result_match.group(1).strip()

    success = final_exit == 0
    experiment_result = {
        "status": "real",
        "method": "llm_ssh",
        "success": success,
        "exit_code": final_exit,
        "total_attempts": min(len(attempt_logs), MAX_ATTEMPTS),
        "attempt_logs": attempt_logs,
        "result_summary": result_json_text[:2000] if result_json_text else combined_output[-2000:],
        "parsed_metrics": parsed_metrics,
        "last_error": last_error[:500] if not success else "",
        "repo_dir": repo_dir,
    }
    _extract_metrics(combined_output, experiment_result)

    status_emoji = "✅" if success else "⚠"
    log_fn("experiment", f"[LLM+SSH] {status_emoji} 实验{'成功' if success else '未完全成功'} (共{len(attempt_logs)}次尝试)")
    emit_event(f"exp_{experiment_id}", "codex_complete", {
        "experiment_id": experiment_id, "success": success, "method": "llm_ssh",
    })
    return experiment_result


def _extract_metrics(all_output: str, result_dict: dict):
    """Extract common ML metrics from output text into result_dict."""
    for line in all_output.split("\n"):
        for pat in [r"(?:acc|accuracy)[\s:=]+([\d.]+)", r"(?:loss)[\s:=]+([\d.]+)",
                    r"(?:reward)[\s:=]+([\d.]+)", r"(?:score)[\s:=]+([\d.]+)"]:
            m = re.search(pat, line, re.IGNORECASE)
            if m:
                key = re.split(r'[\s:=]', pat)[0].lstrip("(?:").rstrip(")")
                try:
                    result_dict[key] = float(m.group(1))
                except ValueError:
                    pass


async def _build_experiment_approval_summary(metrics_json: str, exp_name: str, use_llm: bool, log_fn) -> str:
    """Build a human-readable approval summary from experiment results.
    Used in the approval card so users see meaningful text, not raw JSON."""
    # Try to parse the JSON
    try:
        data = json.loads(metrics_json)
    except (json.JSONDecodeError, TypeError):
        data = {}

    # Build a basic structured summary first
    lines = []
    lines.append(f"## 实验结果: {exp_name}\n")

    method = data.get("method", "")
    success = data.get("success", None)
    status = data.get("status", "")

    if status == "simulated":
        lines.append("📝 **模式**: 模拟执行（未连接服务器）\n")
        lines.append(data.get("note", "") + "\n")
    else:
        method_label = "Codex 全自动" if method == "codex_full_auto" else "LLM + SSH"
        status_emoji = "✅" if success else "⚠️"
        lines.append(f"{status_emoji} **状态**: {'**成功**' if success else '**未完全成功**'}")
        lines.append(f"🔧 **执行方式**: {method_label}")

        # === Codex Last Feedback — MOST IMPORTANT, shown first ===
        codex_feedback = data.get("codex_last_feedback", "")
        if codex_feedback:
            lines.append("\n---")
            lines.append("### 🤖 Codex 最终反馈")
            lines.append(codex_feedback[:2000])
            lines.append("---\n")

        if data.get("total_attempts"):
            lines.append(f"🔄 **尝试次数**: {data['total_attempts']}")

        if data.get("exit_code") is not None:
            lines.append(f"**退出码**: {data['exit_code']}")

        if data.get("codex_exit_code") is not None:
            lines.append(f"**Codex 退出码**: {data['codex_exit_code']}")

        if data.get("total_codex_events"):
            lines.append(f"**Codex 事件数**: {data['total_codex_events']}")

        # Show metrics if found
        metric_keys = ["acc", "accuracy", "loss", "reward", "score"]
        found_metrics = {k: v for k, v in data.items() if k in metric_keys}
        if found_metrics:
            lines.append("\n### 关键指标")
            for k, v in found_metrics.items():
                lines.append(f"- **{k}**: {v}")

        if data.get("parsed_metrics") and isinstance(data["parsed_metrics"], dict):
            lines.append("\n### 解析指标")
            for k, v in data["parsed_metrics"].items():
                lines.append(f"- **{k}**: {v}")

        # Show executed commands summary (Codex mode)
        all_cmds = data.get("all_commands", [])
        if all_cmds:
            lines.append("\n### 执行的命令")
            for cmd_info in all_cmds[-10:]:  # last 10 commands
                cmd_str = cmd_info.get("cmd", "")
                exit_c = cmd_info.get("exit", "")
                icon = "✅" if str(exit_c) == "0" else "❌"
                lines.append(f"- {icon} `{cmd_str}` (exit={exit_c})")

        # Show attempt logs (LLM+SSH mode)
        if data.get("attempt_logs"):
            lines.append("\n### 执行日志")
            for al in data["attempt_logs"]:
                lines.append(f"- {al}")

        # Show result summary
        summary = data.get("result_summary", "")
        if summary and summary != "Codex未生成结果文件":
            lines.append("\n### 实验输出摘要")
            lines.append(summary[:1500])

        # Show all agent messages if available (gives full Codex conversation trail)
        all_msgs = data.get("all_agent_messages", [])
        if all_msgs and len(all_msgs) > 1:
            lines.append("\n### Codex 完整对话")
            for i, msg in enumerate(all_msgs[-5:], 1):  # last 5 messages
                lines.append(f"**[{i}]** {msg[:400]}")

        # Show errors
        if data.get("last_error"):
            lines.append("\n### 最后错误")
            lines.append(f"```\n{data['last_error'][:500]}\n```")

    basic_summary = "\n".join(lines)

    # If LLM is available, enhance with semantic analysis
    if use_llm and status != "simulated":
        try:
            analysis_prompt = f"""以下是一个科研实验的执行结果。请生成一个简洁的中文分析摘要，包括：
1. 实验做了什么（一句话）
2. 主要发现/结果
3. 问题和风险
4. 建议的下一步

原始数据:
{metrics_json[:2500]}

请用 Markdown 格式，不超过 300 字。"""
            analysis = await call_llm(
                [{"role": "user", "content": analysis_prompt}],
                system_prompt="你是科研实验分析专家。用中文简洁回答。",
            )
            return basic_summary + "\n\n---\n### AI 分析\n" + analysis[:1000]
        except Exception:
            pass  # Fall back to basic summary

    return basic_summary[:4000]

def store_stage_report(
    experiment_id: str,
    project_id: str,
    stage: str,
    report_type: str,
    title: str,
    content: str,
):
    """Persist a stage report. Reports are NEVER overwritten — new runs accumulate."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO stage_reports (experiment_id, project_id, stage, report_type, title, content, created_at) VALUES (?,?,?,?,?,?,?)",
        (experiment_id, project_id, stage, report_type, title, content, now),
    )
    conn.commit()
    conn.close()


async def generate_stage_report(
    stage: str,
    stage_output: str,
    exp_name: str,
    hypothesis: str = "",
    memory_ctx: str = "",
) -> str:
    """Generate a stage-specific report using LLM. Returns markdown string."""
    stage_prompts = {
        "ideation": f"""\u57fa\u4e8e\u4ee5\u4e0b\u5047\u8bbe\u751f\u6210\u7ed3\u679c\uff0c\u64b0\u5199\u4e00\u4efd\u8c03\u7814\u62a5\u544a\u3002

## \u5b9e\u9a8c\u540d\u79f0
{exp_name}

## \u5047\u8bbe\u751f\u6210\u7ed3\u679c
{stage_output[:2000]}

## \u9879\u76ee\u8bb0\u5fc6\u4e0a\u4e0b\u6587
{memory_ctx[:800]}

\u8bf7\u751f\u6210\u5305\u542b\u4ee5\u4e0b\u5185\u5bb9\u7684\u8c03\u7814\u62a5\u544a\uff1a
1. \u6838\u5fc3\u5047\u8bbe\u5206\u6790
2. \u76f8\u5173\u5de5\u4f5c\u8c03\u7814
3. \u65b0\u9896\u6027\u8bc4\u4f30
4. \u9884\u671f\u6548\u679c\u548c\u98ce\u9669
\nMarkdown\u683c\u5f0f\u3002""",
        "planning": f"""\u57fa\u4e8e\u4ee5\u4e0b\u5b9e\u9a8c\u8ba1\u5212\uff0c\u64b0\u5199\u4e00\u4efd\u5206\u6790\u62a5\u544a\u3002

## \u5b9e\u9a8c\u540d\u79f0
{exp_name}

## \u5b9e\u9a8c\u8ba1\u5212
{stage_output[:2000]}

\u8bf7\u751f\u6210\u5305\u542b\u4ee5\u4e0b\u5185\u5bb9\u7684\u5206\u6790\u62a5\u544a\uff1a
1. \u5b9e\u9a8c\u8bbe\u8ba1\u5206\u6790
2. \u53ef\u884c\u6027\u8bc4\u4f30
3. \u521b\u65b0\u6027\u5206\u6790
4. \u8d44\u6e90\u9700\u6c42\u548c\u98ce\u9669
\nMarkdown\u683c\u5f0f\u3002""",
        "experiment": f"""\u57fa\u4e8e\u4ee5\u4e0b\u5b9e\u9a8c\u6267\u884c\u7ed3\u679c\uff0c\u64b0\u5199\u4e00\u4efd\u5b9e\u9a8c\u62a5\u544a\u3002

## \u5b9e\u9a8c\u540d\u79f0
{exp_name}

## \u5b9e\u9a8c\u6267\u884c\u7ed3\u679c/\u6307\u6807
{stage_output[:2000]}

\u8bf7\u751f\u6210\u5305\u542b\u4ee5\u4e0b\u5185\u5bb9\u7684\u5b9e\u9a8c\u62a5\u544a\uff1a
1. \u6267\u884c\u7ed3\u679c\u6458\u8981
2. \u5173\u952e\u6307\u6807\u5206\u6790
3. \u9519\u8bef\u5206\u6790\uff08\u5982\u9002\u7528\uff09
4. \u7ed3\u8bba\u4e0e\u4e0b\u4e00\u6b65\u5efa\u8bae
\nMarkdown\u683c\u5f0f\u3002""",
    }

    prompt = stage_prompts.get(stage, f"\u8bf7\u603b\u7ed3\u4ee5\u4e0b\u9636\u6bb5\u8f93\u51fa\uff1a\n{stage_output[:2000]}")

    try:
        report = await call_llm(
            [{"role": "user", "content": prompt}],
            system_prompt="\u4f60\u662f\u4e13\u4e1a\u7684\u79d1\u7814\u62a5\u544a\u64b0\u5199\u4e13\u5bb6\u3002\u8bf7\u7528\u4e2d\u6587\u64b0\u5199\u9ad8\u8d28\u91cf\u62a5\u544a\u3002",
            task_type="paper",
        )
        return report
    except Exception as e:
        return f"# {exp_name} - {stage}\u9636\u6bb5\u62a5\u544a\n\n(LLM\u8c03\u7528\u5931\u8d25: {str(e)})\n\n## \u539f\u59cb\u6570\u636e\n{stage_output[:1000]}"


async def generate_full_report(
    exp_name: str,
    ideation_output: str,
    plan_output: str,
    metrics_output: str,
    report_output: str,
) -> str:
    """Generate a cumulative full report (Stage 4 综合报告)."""
    prompt = f"""\u8bf7\u4e3a\u4ee5\u4e0b\u5b9e\u9a8c\u64b0\u5199\u4e00\u4efd\u5168\u9762\u7684\u7efc\u5408\u62a5\u544a\u3002

## \u5b9e\u9a8c\u540d\u79f0
{exp_name}

## \u9636\u6bb5\u4e00\uff1a\u5047\u8bbe\u751f\u6210
{ideation_output[:800]}

## \u9636\u6bb5\u4e8c\uff1a\u5b9e\u9a8c\u89c4\u5212
{plan_output[:800]}

## \u9636\u6bb5\u4e09\uff1a\u5b9e\u9a8c\u6267\u884c
{metrics_output[:800]}

## \u9636\u6bb5\u56db\uff1a\u62a5\u544a\u64b0\u5199
{report_output[:800]}

\u8bf7\u751f\u6210\u5305\u542b\u4ee5\u4e0b\u90e8\u5206\u7684\u7efc\u5408\u62a5\u544a\uff1a
1. \u6458\u8981
2. \u80cc\u666f\u4e0e\u52a8\u673a
3. \u65b9\u6cd5
4. \u5b9e\u9a8c\u8bbe\u8ba1
5. \u7ed3\u679c\u4e0e\u5206\u6790
6. \u7ed3\u8bba\u4e0e\u672a\u6765\u5de5\u4f5c
7. \u4e0b\u4e00\u6b65\u884c\u52a8\u5efa\u8bae

\u4f7f\u7528Markdown\u683c\u5f0f\u3002"""
    try:
        return await call_llm(
            [{"role": "user", "content": prompt}],
            system_prompt="\u4f60\u662f\u5b66\u672f\u5199\u4f5c\u4e13\u5bb6\u3002\u8bf7\u7528\u4e2d\u6587\u64b0\u5199\u9ad8\u8d28\u91cf\u6280\u672f\u62a5\u544a\u3002",
            task_type="paper",
        )
    except Exception as e:
        return f"# {exp_name} \u7efc\u5408\u62a5\u544a\n\n(LLM\u8c03\u7528\u5931\u8d25: {str(e)})"


async def run_fars_pipeline(experiment_id: str, project_id: str, start_from_stage: int = 0, revision_note: str = "", execution_mode: str = "simulate"):
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

            # Generate and store ideation stage report (BEFORE approval)
            try:
                stage_rpt = await generate_stage_report(
                    "ideation",
                    result if isinstance(result, str) else str(result),
                    exp_name,
                    hypothesis=hypothesis,
                    memory_ctx=memory_ctx,
                )
                store_stage_report(experiment_id, project_id, "ideation", "stage", "调研报告", stage_rpt)
                log("ideation", "[假设生成] 调研报告已生成并存储")
            except Exception as _rpt_err:
                log("ideation", f"[假设生成] 报告生成失败: {str(_rpt_err)}", "WARN")

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

            # Generate and store planning stage report (BEFORE approval)
            try:
                stage_rpt = await generate_stage_report(
                    "planning",
                    plan_result if isinstance(plan_result, str) else str(plan_result),
                    exp_name,
                )
                store_stage_report(experiment_id, project_id, "planning", "stage", "分析报告", stage_rpt)
                log("planning", "[实验规划] 分析报告已生成并存储")
            except Exception as _rpt_err:
                log("planning", f"[实验规划] 报告生成失败: {str(_rpt_err)}", "WARN")

            # Approval check after planning
            if get_approval_enabled():
                pause_for_approval("planning", plan_result[:4000] if isinstance(plan_result, str) else str(plan_result)[:4000])
                running_experiments.pop(experiment_id, None)
                return

        # ---------------------------------------------------------------
        # Stage 3: Experiment execution (Codex full-auto)
        # ---------------------------------------------------------------
        if start_from_stage <= 2:
            if not running_experiments.get(experiment_id):
                return
            update_exp("running", "experiment")
            update_step("experiment", "running")
            log("experiment", "[实验执行] 启动")
            if revision_note and start_from_stage == 2:
                log("experiment", f"[实验执行] 修改说明: {revision_note}")

            # Use previous stage outputs if resuming
            plan_ctx = plan_result if (start_from_stage <= 1 and isinstance(plan_result, str) and plan_result) else prev_outputs.get("planning", "")
            ideation_ctx = result if (start_from_stage <= 0 and isinstance(result, str) and result) else prev_outputs.get("ideation", "")

            log("experiment", f"[实验执行] 执行模式: {execution_mode}")

            if execution_mode == "real":
                # ---- Real execution: Codex full-auto on remote server ----
                log("experiment", "[实验执行] 真实执行模式 — 检查 SSH 配置...")
                ssh_cfg = get_ssh_config()
                if not ssh_cfg["host"] or not ssh_cfg["password"]:
                    log("experiment", "[实验执行] SSH 未配置，回退到模拟模式", "WARN")
                    execution_mode = "simulate"
                else:
                    # Step 1: Get repo_url from project
                    conn_repo = get_db()
                    proj_row = conn_repo.execute("SELECT repo_url FROM projects WHERE id=?", (project_id,)).fetchone()
                    conn_repo.close()
                    repo_url = proj_row["repo_url"] if proj_row and proj_row["repo_url"] else ""
                    if not repo_url:
                        log("experiment", "[实验执行] 项目未配置仓库 URL，回退到模拟模式", "WARN")
                        execution_mode = "simulate"
                    else:
                        # Step 2: Clone / pull repo on server
                        log("experiment", f"[实验执行] 准备仓库: {repo_url}")
                        repo_result = await ensure_repo_on_server(
                            repo_url=repo_url,
                            project_id=project_id,
                            experiment_id=experiment_id,
                            log_fn=log,
                        )
                        if not repo_result.get("success"):
                            log("experiment", f"[实验执行] 仓库准备失败: {repo_result.get('error', '未知错误')}", "ERROR")
                            metrics_json = json.dumps({
                                "status": "real",
                                "success": False,
                                "error": f"仓库准备失败: {repo_result.get('error', '')}",
                            }, ensure_ascii=False)
                            update_exp("running", "experiment", metrics=metrics_json)
                        else:
                            repo_dir = repo_result["repo_dir"]
                            log("experiment", f"[实验执行] ✓ 仓库就绪: {repo_dir}")

                            # Step 3: Codex full-auto experiment
                            log("experiment", "[实验执行] 🚀 启动 Codex 全自动实验...")
                            codex_result = await codex_experiment_run(
                                experiment_id=experiment_id,
                                project_id=project_id,
                                repo_dir=repo_dir,
                                exp_name=exp_name,
                                hypothesis_ctx=ideation_ctx[:2000],
                                plan_ctx=plan_ctx[:2000],
                                memory_ctx=memory_ctx[:1000],
                                revision_note=revision_note if start_from_stage == 2 else "",
                                log_fn=log,
                            )

                            # Step 4: Store metrics
                            metrics_json = json.dumps(codex_result, ensure_ascii=False, default=str)
                            update_exp("running", "experiment", metrics=metrics_json)
                            if codex_result.get("success"):
                                log("experiment", "[实验执行] ✅ Codex 实验完成")
                            else:
                                log("experiment", "[实验执行] ⚠ Codex 实验未完全成功", "WARN")

                            # Store key findings in memory
                            summary = codex_result.get("result_summary", "")[:500]
                            if summary:
                                store_memory(project_id, f"实验 {exp_name} Codex结果: {summary}", "experiment_result", "experiment")

            if execution_mode == "simulate":
                # ---- Simulate execution (LLM analysis + mock metrics) ----
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

            # Generate and store experiment stage report (BEFORE approval)
            try:
                stage_rpt = await generate_stage_report(
                    "experiment",
                    metrics_json,
                    exp_name,
                )
                store_stage_report(experiment_id, project_id, "experiment", "stage", "实验报告", stage_rpt)
                log("experiment", "[实验执行] 实验报告已生成并存储")
            except Exception as _rpt_err:
                log("experiment", f"[实验执行] 报告生成失败: {str(_rpt_err)}", "WARN")

            # Approval check after experiment — generate readable summary
            if get_approval_enabled():
                # Generate human-readable approval summary from raw metrics
                approval_text = await _build_experiment_approval_summary(metrics_json, exp_name, use_llm, log)
                pause_for_approval("experiment", approval_text)
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

            # Generate and store full cumulative report (Stage 4)
            try:
                full_rpt = await generate_full_report(
                    exp_name=exp_name,
                    ideation_output=final_ideation,
                    plan_output=final_plan,
                    metrics_output=final_metrics,
                    report_output=report,
                )
                store_stage_report(experiment_id, project_id, "writing", "full", "综合报告", full_rpt)
                log("writing", "[报告撰写] 综合报告已生成并存储")
            except Exception as _rpt_err:
                log("writing", f"[报告撰写] 综合报告生成失败: {str(_rpt_err)}", "WARN")

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
    # Read execution_mode from the experiment record
    conn = get_db()
    exp = conn.execute("SELECT execution_mode FROM experiments WHERE id=?", (experiment_id,)).fetchone()
    conn.close()
    exp_mode = exp["execution_mode"] if exp and exp["execution_mode"] else "simulate"

    try:
        stage_idx = STAGES.index(completed_stage) if completed_stage in STAGES else -1
    except ValueError:
        stage_idx = -1
    next_idx = stage_idx + 1
    if next_idx >= len(STAGES):
        return  # All stages already done
    await run_fars_pipeline(experiment_id, project_id, start_from_stage=next_idx, revision_note=revision_note, execution_mode=exp_mode)



# ---------------------------------------------------------------------------
# GitHub Code Analysis
# ---------------------------------------------------------------------------
async def analyze_github_repo(repo_url: str) -> dict:
    """Fetch repo structure and README from GitHub API."""
    # Parse owner/repo from URL
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", repo_url.strip("/"))
    if not match:
        return {"error": "无效的 GitHub 仓库 URL"}

    owner, repo = match.group(1), match.group(2)
    api_base = f"https://api.github.com/repos/{owner}/{repo}"

    results = {"repo_url": repo_url, "owner": owner, "repo": repo}
    headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "LabOS"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get repo info
        try:
            resp = await client.get(api_base, headers=headers)
            if resp.status_code == 200:
                info = resp.json()
                results["description"] = info.get("description", "")
                results["stars"] = info.get("stargazers_count", 0)
                results["language"] = info.get("language", "")
                results["topics"] = info.get("topics", [])
                results["default_branch"] = info.get("default_branch", "main")
        except Exception:
            pass

        # Get file tree
        branch = results.get("default_branch", "main")
        try:
            resp = await client.get(f"{api_base}/git/trees/{branch}?recursive=1", headers=headers)
            if resp.status_code == 200:
                tree_data = resp.json()
                paths = [item["path"] for item in tree_data.get("tree", []) if item["type"] in ("blob", "tree")]
                results["file_tree"] = paths[:500]  # Limit
                results["file_count"] = len(paths)
            else:
                results["file_tree"] = []
        except Exception:
            results["file_tree"] = []

        # Get README
        try:
            resp = await client.get(f"{api_base}/readme", headers=headers)
            if resp.status_code == 200:
                readme_data = resp.json()
                import base64
                readme_b64 = readme_data.get("content", "")
                results["readme"] = base64.b64decode(readme_b64).decode("utf-8", errors="replace")[:8000]
            else:
                results["readme"] = ""
        except Exception:
            results["readme"] = ""

    return results


# ---------------------------------------------------------------------------
# Paper Search (Semantic Scholar)
# ---------------------------------------------------------------------------
async def search_papers(query: str, limit: int = 10) -> list[dict]:
    """Search papers using Semantic Scholar API."""
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": min(limit, 20),
        "fields": "title,authors,year,abstract,url,citationCount,venue,externalIds",
    }
    headers = {"User-Agent": "LabOS"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                papers = []
                for p in data.get("data", []):
                    authors_str = ", ".join([a.get("name", "") for a in p.get("authors", [])[:5]])
                    papers.append({
                        "paper_id": p.get("paperId", ""),
                        "title": p.get("title", ""),
                        "authors": authors_str,
                        "year": p.get("year", 0),
                        "abstract": (p.get("abstract") or "")[:500],
                        "url": p.get("url", ""),
                        "citation_count": p.get("citationCount", 0),
                        "venue": p.get("venue", ""),
                    })
                return papers
            elif resp.status_code == 429:
                return [{"error": "API 速率限制，请稍后重试"}]
            else:
                return [{"error": f"搜索失败 ({resp.status_code})"}]
        except Exception as e:
            return [{"error": f"搜索失败: {str(e)}"}]


# ---------------------------------------------------------------------------
# Dify Integration
# ---------------------------------------------------------------------------
async def call_dify_workflow(inputs: dict, query: str = "") -> dict:
    """Call Dify workflow/chatbot API."""
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM configs WHERE category='dify'").fetchall()
    conn.close()
    config = {r["key"]: r["value"] for r in rows}

    api_url = config.get("dify_api_url", "").strip()
    api_key = config.get("dify_api_key", "").strip()

    if not api_url or not api_key:
        return {"error": "Dify 未配置。请在设置中配置 Dify API 地址和密钥。"}

    base_url = api_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Try workflow endpoint first
    payload = {
        "inputs": inputs,
        "response_mode": "blocking",
        "user": "labos",
    }
    if query:
        payload["query"] = query

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Try chat-messages endpoint (chatbot mode)
        try:
            resp = await client.post(f"{base_url}/chat-messages", json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return {"answer": data.get("answer", ""), "metadata": data.get("metadata", {})}
        except Exception:
            pass

        # Try workflows/run endpoint (workflow mode)
        try:
            resp = await client.post(f"{base_url}/workflows/run", json=payload, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                outputs = data.get("data", {}).get("outputs", {})
                return {"outputs": outputs, "status": data.get("data", {}).get("status", "")}
        except Exception as e:
            return {"error": f"Dify 调用失败: {str(e)}"}

    return {"error": "Dify 调用失败: 无法连接"}


# ---------------------------------------------------------------------------
# SSH Remote Executor
# ---------------------------------------------------------------------------
def get_ssh_config():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM configs WHERE category='server'").fetchall()
    conn.close()
    config = {r["key"]: r["value"] for r in rows}
    return {
        "host": config.get("ssh_host", "").strip(),
        "port": int(config.get("ssh_port", "22").strip() or "22"),
        "user": config.get("ssh_user", "").strip() or "root",
        "password": config.get("ssh_password", "").strip(),
    }


def _make_ssh_logger(experiment_id: str, project_id: str, stage: str = "ssh"):
    """Create a log helper that writes to DB + emits SSE."""
    def log_ssh(msg, level="INFO"):
        if experiment_id:
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
    return log_ssh


async def _ssh_connect(cfg: dict) -> "paramiko.SSHClient":
    """Open an SSH connection using the given config."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: client.connect(
        hostname=cfg["host"],
        port=cfg["port"],
        username=cfg["user"],
        password=cfg["password"],
        timeout=30,
    ))
    return client


SSH_ENV_PREFIX = 'source /etc/profile 2>/dev/null; source ~/.bashrc 2>/dev/null; source ~/.profile 2>/dev/null; '


async def ssh_execute(command: str, project_id: str = "", experiment_id: str = "") -> dict:
    """Execute a command on the remote server via SSH (bash login shell)."""
    if not paramiko:
        return {"error": "paramiko 未安装。请运行: pip install paramiko"}

    cfg = get_ssh_config()
    if not cfg["host"] or not cfg["password"]:
        return {"error": "SSH 未配置。请在设置中配置服务器信息。"}

    log_ssh = _make_ssh_logger(experiment_id, project_id)

    try:
        log_ssh(f"[SSH] 连接 {cfg['host']}:{cfg['port']}...")
        client = await _ssh_connect(cfg)
        log_ssh(f"[SSH] 已连接")

        full_command = SSH_ENV_PREFIX + command
        loop = asyncio.get_event_loop()
        stdin, stdout, stderr = await loop.run_in_executor(
            None, lambda: client.exec_command(full_command, timeout=7200)
        )

        output_lines = []
        for line in stdout:
            line_text = line.strip()
            output_lines.append(line_text)
            log_ssh(f"[SSH] {line_text}")

        err_text = stderr.read().decode("utf-8", errors="replace").strip()
        exit_code = stdout.channel.recv_exit_status()

        client.close()
        log_ssh(f"[SSH] 命令完成，退出码: {exit_code}")

        return {
            "output": "\n".join(output_lines),
            "error": err_text,
            "exit_code": exit_code,
        }
    except Exception as e:
        log_ssh(f"[SSH] 执行失败: {str(e)}", "ERROR")
        return {"error": f"SSH 执行失败: {str(e)}"}


async def ssh_execute_codex_streaming(
    command: str, project_id: str, experiment_id: str, log_fn
) -> dict:
    """
    Execute a Codex CLI command on the remote server with real-time JSONL parsing.
    Instead of waiting for the entire command to complete, this reads stdout line-by-line
    and parses Codex JSONL events as they arrive, emitting structured logs and SSE events
    so the frontend can show incremental progress.

    Returns the same dict shape as ssh_execute (output, error, exit_code)
    plus parsed Codex event data.
    """
    if not paramiko:
        return {"error": "paramiko 未安装"}

    cfg = get_ssh_config()
    if not cfg["host"] or not cfg["password"]:
        return {"error": "SSH 未配置"}

    log_ssh = _make_ssh_logger(experiment_id, project_id, "codex")

    # Codex parsed data
    codex_events = []
    all_agent_messages = []
    all_commands = []
    last_agent_message = ""
    round_counter = 0  # Track AUTO rounds

    def parse_codex_line(line: str):
        nonlocal last_agent_message, round_counter
        """Parse a single JSONL line from Codex output and emit structured events."""
        line = line.strip()
        if not line:
            return
        try:
            event = json.loads(line)
            codex_events.append(event)

            if event.get("type") == "item.completed":
                item = event.get("item", {})

                if item.get("type") == "agent_message":
                    msg_text = item.get("text", "")
                    all_agent_messages.append(msg_text)
                    last_agent_message = msg_text
                    round_counter += 1
                    # Emit structured log — user sees each Codex message as it arrives
                    log_fn("experiment", f"[Codex 回合 {round_counter}] 💬 {msg_text[:500]}")
                    emit_event(f"exp_{experiment_id}", "codex_message", {
                        "experiment_id": experiment_id,
                        "round": round_counter,
                        "type": "agent_message",
                        "text": msg_text[:2000],
                    })

                elif item.get("type") == "command_execution":
                    cmd_text = item.get("command", "")[:300]
                    exit_c = item.get("exit_code", "")
                    cmd_out = item.get("output", "")[:500] if item.get("output") else ""
                    all_commands.append({"cmd": cmd_text, "exit": exit_c, "output": cmd_out})
                    icon = "✅" if str(exit_c) == "0" else "❌"
                    # Emit structured log — user sees each command + result
                    log_fn("experiment", f"[Codex 回合 {round_counter}] {icon} `{cmd_text}` (exit={exit_c})")
                    if cmd_out and str(exit_c) != "0":
                        log_fn("experiment", f"[Codex 回合 {round_counter}] 错误输出: {cmd_out[:300]}")
                    emit_event(f"exp_{experiment_id}", "codex_command", {
                        "experiment_id": experiment_id,
                        "round": round_counter,
                        "type": "command_execution",
                        "command": cmd_text,
                        "exit_code": exit_c,
                        "output": cmd_out,
                    })

                elif item.get("type") == "tool_call":
                    tool_name = item.get("name", "")
                    log_fn("experiment", f"[Codex 回合 {round_counter}] 🔧 工具调用: {tool_name}")

            elif event.get("type") == "item.created":
                # New item starting — lightweight log
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    log_fn("experiment", f"[Codex] ⏳ Codex 正在思考...")

        except json.JSONDecodeError:
            # Non-JSON output from Codex — raw text
            if line and not line.startswith("{"):
                log_fn("experiment", f"[Codex] {line[:300]}")
                if not codex_events:
                    # If no JSONL events yet, accumulate as raw feedback
                    all_agent_messages.append(line)

    try:
        log_ssh(f"[SSH] 连接 {cfg['host']}:{cfg['port']}...")
        client = await _ssh_connect(cfg)
        log_ssh(f"[SSH] 已连接")

        full_command = SSH_ENV_PREFIX + command
        loop = asyncio.get_event_loop()
        stdin, stdout, stderr = await loop.run_in_executor(
            None, lambda: client.exec_command(full_command, timeout=7200)
        )

        log_fn("experiment", "[Codex] 🚀 Codex CLI 已启动，等待输出...")

        # Read stdout line-by-line with async executor to avoid blocking
        output_lines = []

        def _read_lines():
            lines = []
            for raw_line in stdout:
                line_text = raw_line.strip()
                lines.append(line_text)
            return lines

        # Use a thread to read lines, yielding back to event loop periodically
        # This approach reads line-by-line but in a thread so we don't block
        import threading
        import queue as thread_queue

        line_queue = thread_queue.Queue()
        read_done = threading.Event()

        def _reader_thread():
            try:
                for raw_line in stdout:
                    line_text = raw_line.strip()
                    line_queue.put(line_text)
            except Exception as e:
                line_queue.put(f"__ERROR__:{str(e)}")
            finally:
                read_done.set()

        reader = threading.Thread(target=_reader_thread, daemon=True)
        reader.start()

        # Process lines as they arrive
        while not read_done.is_set() or not line_queue.empty():
            try:
                # Non-blocking get with short timeout
                line_text = line_queue.get(timeout=0.5)
                if line_text.startswith("__ERROR__:"):
                    log_fn("experiment", f"[Codex] 读取错误: {line_text[10:]}", "ERROR")
                    continue
                output_lines.append(line_text)
                parse_codex_line(line_text)
            except thread_queue.Empty:
                # No new lines — yield back to event loop
                await asyncio.sleep(0.2)
                continue

        reader.join(timeout=5)

        err_text = await loop.run_in_executor(
            None, lambda: stderr.read().decode("utf-8", errors="replace").strip()
        )
        exit_code = stdout.channel.recv_exit_status()

        client.close()

        total_output = "\n".join(output_lines)
        log_fn("experiment", f"[Codex] 🏁 Codex CLI 执行完成 (exit={exit_code}, 共 {len(codex_events)} 个事件, {round_counter} 个回合)")
        if exit_code != 0 and err_text:
            log_fn("experiment", f"[Codex] ⚠ STDERR: {err_text[:1000]}", "WARN")
        if exit_code != 0 and not codex_events:
            log_fn("experiment", f"[Codex] ⚠ 无JSONL事件输出，原始stdout: {total_output[:500]}", "WARN")

        return {
            "output": total_output,
            "error": err_text,
            "exit_code": exit_code,
            "codex_events": codex_events,
            "all_agent_messages": all_agent_messages,
            "all_commands": all_commands,
            "last_agent_message": last_agent_message,
            "round_count": round_counter,
        }
    except Exception as e:
        log_fn("experiment", f"[Codex] SSH 执行失败: {str(e)}", "ERROR")
        return {"error": f"SSH 执行失败: {str(e)}"}


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
def seed_default_llm_config():
    """Set DeepSeek as default LLM if no LLM config exists."""
    conn = get_db()
    existing = conn.execute("SELECT value FROM configs WHERE key='llm_api_url'").fetchone()
    if existing and existing["value"].strip():
        conn.close()
        return

    now = datetime.now(timezone.utc).isoformat()
    defaults = [
        ("llm_api_url", "https://api.deepseek.com", "llm"),
        ("llm_model", "deepseek-chat", "llm"),
    ]
    for key, value, category in defaults:
        conn.execute(
            "INSERT OR REPLACE INTO configs (key, value, category, updated_at) VALUES (?,?,?,?)",
            (key, value, category, now),
        )
    conn.commit()
    conn.close()


def seed_initial_data():
    """Auto-create MemRL project with 4 experiment branches on first boot."""
    conn = get_db()
    existing = conn.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"]
    if existing > 0:
        conn.close()
        return

    now = datetime.now(timezone.utc).isoformat()
    pid = "proj_memrl001"

    # Create MemRL project
    conn.execute(
        "INSERT INTO projects (id, name, repo_url, description, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (pid, "MemRL — 记忆增强强化学习", "https://github.com/MemTensor/MemRL",
         "Memory-augmented Reinforcement Learning 研究项目。探索分层记忆、检索增强、过程奖励和多步优化等方向。", now, now),
    )

    # Store project memories
    memories = [
        ("project_info", "项目仓库: https://github.com/MemTensor/MemRL"),
        ("project_info", "MemRL 核心方向: 记忆增强的强化学习，结合知识图谱、检索机制、过程奖励等技术"),
        ("reference", "核心基准: ScienceWorld, DiscoveryWorld, SWE-bench, WebQA, Mind2Web"),
        ("reference", "关键技术: Hierarchical Memory, Knowledge Graphs, Retrieval-Augmented RL, Process Reward Models, Distributional Q-Learning"),
    ]
    for cat, content in memories:
        conn.execute(
            "INSERT INTO memories (project_id, category, content, source) VALUES (?,?,?,?)",
            (pid, cat, content, "system"),
        )

    # Create 4 experiment branches
    experiments = [
        {
            "id": "exp_memrl_01",
            "name": "分层记忆 + 知识图谱 (ScienceWorld)",
            "hypothesis": "假设：在 ScienceWorld 环境中，结合分层记忆结构（工作记忆 + 长期记忆）与知识图谱表征，能够显著提升 agent 的多步科学推理能力。\n预期：相比 flat memory baseline，任务完成率提升 15-25%，尤其在需要长距离依赖的复杂任务上。\n关键变量：记忆层级数、知识图谱更新频率、检索策略。",
            "priority": 2,
        },
        {
            "id": "exp_memrl_02",
            "name": "检索增强记忆 (DiscoveryWorld)",
            "hypothesis": "假设：在 DiscoveryWorld 的开放探索任务中，基于 dense retrieval 的记忆检索机制能让 agent 更有效地利用历史经验。\n预期：exploration efficiency 提升 20%+，通过 retrieval 减少重复探索。\n关键变量：retrieval top-k、memory bank 大小、encoding 策略。",
            "priority": 1,
        },
        {
            "id": "exp_memrl_03",
            "name": "过程奖励 + 密集分配 (SWE-bench)",
            "hypothesis": "假设：在 SWE-bench 代码修复任务中，过程奖励模型（PRM）配合密集奖励分配策略，能够引导 agent 学习更细粒度的代码修改决策。\n预期：patch 通过率提升 10-18%，同时减少无效编辑次数。\n关键变量：PRM 粒度、奖励密度、rollout 策略。",
            "priority": 1,
        },
        {
            "id": "exp_memrl_04",
            "name": "多步优化 + 分布式Q学习 (WebQA/Mind2Web)",
            "hypothesis": "假设：在 WebQA 和 Mind2Web 的 web 交互任务中，结合多步优化目标与分布式 Q-learning 能让 agent 更好地处理 web 页面的复杂状态空间。\n预期：任务成功率提升 12-20%，decision latency 降低。\n关键变量：Q分布参数化方式、multi-step n值、状态表征方法。",
            "priority": 0,
        },
    ]

    for exp in experiments:
        conn.execute(
            "INSERT INTO experiments (id, project_id, name, hypothesis, priority, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (exp["id"], pid, exp["name"], exp["hypothesis"], exp["priority"], now, now),
        )
        for i, stage in enumerate(STAGES):
            conn.execute(
                "INSERT INTO pipeline_steps (experiment_id, stage, step_index, description, status) VALUES (?,?,?,?,?)",
                (exp["id"], stage, i, STAGE_LABELS[stage], "pending"),
            )

    conn.commit()
    conn.close()


def seed_pipeline_configs():
    """Seed default pipeline execution/approval configs if not present."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    defaults = [
        ("approval_enabled", "true", "pipeline"),
    ]
    for key, value, category in defaults:
        existing = conn.execute("SELECT value FROM configs WHERE key=?", (key,)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO configs (key, value, category, updated_at) VALUES (?,?,?,?)",
                (key, value, category, now),
            )
    # Ensure __global__ project exists for project-less conversations
    global_proj = conn.execute("SELECT id FROM projects WHERE id='__global__'").fetchone()
    if not global_proj:
        conn.execute(
            "INSERT INTO projects (id, name, repo_url, description, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("__global__", "__global__", "", "全局对话（未关联项目\uff09", "system", now, now),
        )
    conn.commit()
    conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_default_llm_config()
    seed_initial_data()
    seed_pipeline_configs()
    seed_default_llm_profiles()
    yield

app = FastAPI(title="LabOS v3.4", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class ConfigPayload(BaseModel):
    configs: dict = Field(default_factory=dict)

class ProjectCreate(BaseModel):
    name: str
    repo_url: str = ""
    description: str = ""

class ChatMessage(BaseModel):
    project_id: str = ""
    session_id: str = ""
    message: str
    task_type: Optional[str] = "general"

class ExperimentCreate(BaseModel):
    project_id: str
    name: str
    hypothesis: str = ""
    priority: int = 0

class FeedbackPayload(BaseModel):
    feedback: str

class MemoryCreate(BaseModel):
    project_id: str
    content: str
    category: str = "general"

class MemorySearch(BaseModel):
    project_id: str
    query: str
    top_k: int = 10

class PaperSearchQuery(BaseModel):
    query: str
    project_id: str = ""
    limit: int = 10

class DifyRequest(BaseModel):
    inputs: dict = Field(default_factory=dict)
    query: str = ""

class CompareRequest(BaseModel):
    experiment_ids: list[str]

class SSHCommand(BaseModel):
    command: str
    project_id: str = ""
    experiment_id: str = ""

class ApprovalAction(BaseModel):
    stage: str
    action: str  # "approve", "reject", "revise"
    comment: str = ""

class PipelineSettings(BaseModel):
    approval_enabled: str = "true"
    auto_debug_enabled: str = "true"

class LLMProfileCreate(BaseModel):
    name: str
    task_type: str
    api_url: str
    api_key: str
    model: str
    system_prompt: str = ""
    is_default: int = 0

class LLMProfileUpdate(BaseModel):
    name: Optional[str] = None
    task_type: Optional[str] = None
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    is_default: Optional[int] = None

class ExperimentStart(BaseModel):
    execution_mode: str = "simulate"  # "simulate" or "real"

# ---------------------------------------------------------------------------
# Config API
# ---------------------------------------------------------------------------
@app.get("/api/config")
def get_config():
    conn = get_db()
    rows = conn.execute("SELECT key, value, category FROM configs").fetchall()
    conn.close()
    result = {}
    for r in rows:
        key, val, cat = r["key"], r["value"], r["category"]
        if "key" in key.lower() or "password" in key.lower() or "secret" in key.lower():
            display_val = (val[:4] + "****" + val[-4:]) if val and len(val) > 8 else ("****" if val else "")
        else:
            display_val = val
        result[key] = {"value": display_val, "category": cat}
    return result

@app.post("/api/config")
def save_config(payload: ConfigPayload):
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    for key, info in payload.configs.items():
        value = info.get("value", "") if isinstance(info, dict) else str(info)
        category = info.get("category", "general") if isinstance(info, dict) else "general"
        if "****" in value:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO configs (key, value, category, updated_at) VALUES (?,?,?,?)",
            (key, value, category, now),
        )
    conn.commit()
    conn.close()
    return {"status": "ok", "message": "配置已保存"}

@app.post("/api/config/test-llm")
async def test_llm():
    try:
        result = await call_llm([{"role": "user", "content": "你好，请回复'连接成功'两个字。"}])
        return {"status": "ok", "message": f"LLM 连接成功: {result[:200]}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/config/test-embedding")
async def test_embedding():
    try:
        result = await call_embedding(["测试向量化"])
        if result and len(result) > 0:
            dim = len(result[0])
            return {"status": "ok", "message": f"Embedding 连接成功 (维度: {dim})"}
        return {"status": "error", "message": "未返回有效向量"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/config/test-dify")
async def test_dify():
    try:
        result = await call_dify_workflow({"test": "hello"}, query="测试连接")
        if "error" in result:
            return {"status": "error", "message": result["error"]}
        return {"status": "ok", "message": "Dify 连接成功"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ---------------------------------------------------------------------------
# LLM Profile API
# ---------------------------------------------------------------------------
@app.get("/api/llm-profiles")
def list_llm_profiles():
    conn = get_db()
    rows = conn.execute("SELECT * FROM llm_profiles ORDER BY task_type, name").fetchall()
    conn.close()
    result = []
    for r in rows:
        p = dict(r)
        key = p.get("api_key", "")
        p["api_key"] = (key[:4] + "****" + key[-4:]) if key and len(key) > 8 else ("****" if key else "")
        result.append(p)
    return result

@app.post("/api/llm-profiles")
def create_llm_profile(payload: LLMProfileCreate):
    pid = f"llmprof_{uuid.uuid4().hex[:8]}"
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    # If this profile is marked as default, unset other defaults for the same task_type
    if payload.is_default:
        conn.execute("UPDATE llm_profiles SET is_default=0 WHERE task_type=?", (payload.task_type,))
    conn.execute(
        "INSERT INTO llm_profiles (id, name, task_type, api_url, api_key, model, system_prompt, is_default, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pid, payload.name, payload.task_type, payload.api_url, payload.api_key,
         payload.model, payload.system_prompt, payload.is_default, now, now),
    )
    conn.commit()
    conn.close()
    return {"id": pid, "status": "ok"}

@app.put("/api/llm-profiles/{profile_id}")
def update_llm_profile(profile_id: str, payload: LLMProfileUpdate):
    conn = get_db()
    row = conn.execute("SELECT * FROM llm_profiles WHERE id=?", (profile_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="LLM 配置不存在")
    now = datetime.now(timezone.utc).isoformat()
    updates = payload.dict(exclude_none=True)
    # Skip masked api_key values
    if "api_key" in updates and "****" in updates["api_key"]:
        del updates["api_key"]
    if not updates:
        conn.close()
        return {"status": "ok", "message": "无变更"}
    # If setting as default, unset others for same task_type
    task_type = updates.get("task_type", row["task_type"])
    if updates.get("is_default") == 1:
        conn.execute("UPDATE llm_profiles SET is_default=0 WHERE task_type=? AND id!=?", (task_type, profile_id))
    sets = ", ".join([f"{k}=?" for k in updates.keys()] + ["updated_at=?"])
    vals = list(updates.values()) + [now, profile_id]
    conn.execute(f"UPDATE llm_profiles SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.delete("/api/llm-profiles/{profile_id}")
def delete_llm_profile(profile_id: str):
    conn = get_db()
    conn.execute("DELETE FROM llm_profiles WHERE id=?", (profile_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.post("/api/llm-profiles/{profile_id}/test")
async def test_llm_profile(profile_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM llm_profiles WHERE id=?", (profile_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="LLM 配置不存在")
    profile = dict(row)
    try:
        result = await call_llm(
            [{"role": "user", "content": "你好，请回复'连接成功'。"}],
            task_type=profile["task_type"],
        )
        return {"status": "ok", "message": f"LLM Profile 连接成功: {str(result)[:200]}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ---------------------------------------------------------------------------
# Project API
# ---------------------------------------------------------------------------
@app.get("/api/projects")
def list_projects():
    conn = get_db()
    rows = conn.execute("SELECT * FROM projects WHERE id != '__global__' ORDER BY updated_at DESC").fetchall()
    result = []
    for r in rows:
        p = dict(r)
        exp_count = conn.execute("SELECT COUNT(*) as c FROM experiments WHERE project_id=?", (r["id"],)).fetchone()["c"]
        mem_count = conn.execute("SELECT COUNT(*) as c FROM memories WHERE project_id=?", (r["id"],)).fetchone()["c"]
        p["experiment_count"] = exp_count
        p["memory_count"] = mem_count
        result.append(p)
    conn.close()
    return result

@app.post("/api/projects")
def create_project(payload: ProjectCreate):
    pid = f"proj_{uuid.uuid4().hex[:8]}"
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO projects (id, name, repo_url, description, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (pid, payload.name, payload.repo_url, payload.description, now, now),
    )
    if payload.repo_url:
        conn.execute(
            "INSERT INTO memories (project_id, category, content, source) VALUES (?,?,?,?)",
            (pid, "project_info", f"项目仓库: {payload.repo_url}", "system"),
        )
    if payload.description:
        conn.execute(
            "INSERT INTO memories (project_id, category, content, source) VALUES (?,?,?,?)",
            (pid, "project_info", f"项目描述: {payload.description}", "system"),
        )
    conn.commit()
    conn.close()
    return {"id": pid, "status": "ok"}

@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    conn = get_db()
    p = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not p:
        conn.close()
        raise HTTPException(status_code=404, detail="项目不存在")
    result = dict(p)
    result["experiments"] = [dict(e) for e in conn.execute(
        "SELECT * FROM experiments WHERE project_id=? ORDER BY priority DESC, created_at DESC", (project_id,)
    ).fetchall()]
    result["memories"] = retrieve_memories(project_id, limit=50)
    conn.close()
    return result

@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    if project_id == "__global__":
        return {"error": "Cannot delete system project"}
    conn = get_db()
    conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------
@app.post("/api/chat")
async def chat(payload: ChatMessage):
    project_id = payload.project_id or "__global__"
    session_id = payload.session_id or f"sess_{uuid.uuid4().hex[:8]}"
    message = payload.message

    conn = get_db()
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    proj_name = dict(proj)["name"] if proj else "LabOS"
    proj_context = ""

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO conversations (project_id, session_id, role, content, created_at) VALUES (?,?,?,?,?)",
        (project_id, session_id, "user", message, now),
    )
    conn.commit()

    history = conn.execute(
        "SELECT role, content FROM conversations WHERE project_id=? AND session_id=? ORDER BY id DESC LIMIT 20",
        (project_id, session_id),
    ).fetchall()
    history = list(reversed([{"role": r["role"], "content": r["content"]} for r in history]))

    memory_ctx = build_memory_context(project_id)

    exps = conn.execute(
        "SELECT name, status, hypothesis, result_summary FROM experiments WHERE project_id=? ORDER BY updated_at DESC LIMIT 5",
        (project_id,),
    ).fetchall()
    exp_ctx = "\n".join([f"- {e['name']} [{e['status']}]: {e['hypothesis'][:100] if e['hypothesis'] else ''}" for e in exps])

    # Get papers context
    papers = conn.execute(
        "SELECT title, authors, year, venue FROM papers WHERE project_id=? ORDER BY citation_count DESC LIMIT 5",
        (project_id,),
    ).fetchall()
    paper_ctx = "\n".join([f"- {p['title']} ({p['year']}, {p['venue']})" for p in papers]) if papers else ""

    conn.close()

    system_prompt = f"""你是 LabOS 科研助手，专注于帮助用户进行自动化科研实验。

## 项目信息
- 项目名称: {proj_name}
- 仓库: {(dict(proj).get('repo_url', '') if proj else '') or '未设置'}
- 描述: {(dict(proj).get('description', '') if proj else '') or '未设置'}

## 项目记忆
{memory_ctx or '(暂无记忆)'}

## 近期实验
{exp_ctx or '(暂无实验)'}

## 相关论文
{paper_ctx or '(暂无论文)'}

## 你的能力
1. 分析用户的研究想法，提供建议
2. 帮助设计实验假设和计划
3. 基于项目记忆提供上下文相关的建议
4. 生成实验报告和总结
5. 记住之前的对话内容并持续改进建议
6. 查找相关论文和代码

## 行为准则
- 始终用中文回答
- 提供具体、可执行的建议
- 如果用户提到新的想法或发现，主动建议创建实验
- 引用项目记忆中的相关信息
- 报告中发现问题时，给出修正建议"""

    try:
        generator = await call_llm(history, system_prompt=system_prompt, stream=True, task_type=payload.task_type or "general")

        async def response_stream():
            full_response = []
            async for chunk in generator:
                yield chunk
                if chunk.startswith("data: ") and chunk.strip() != "data: [DONE]":
                    try:
                        d = json.loads(chunk[6:])
                        delta = d.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta:
                            full_response.append(delta)
                    except Exception:
                        pass

            assistant_msg = "".join(full_response)
            if assistant_msg:
                conn2 = get_db()
                conn2.execute(
                    "INSERT INTO conversations (project_id, session_id, role, content, metadata, created_at) VALUES (?,?,?,?,?,?)",
                    (project_id, session_id, "assistant", assistant_msg, json.dumps({"model": "configured"}), datetime.now(timezone.utc).isoformat()),
                )
                conn2.commit()
                conn2.close()

                if len(assistant_msg) > 100:
                    store_memory(project_id, f"对话摘要: {assistant_msg[:200]}", "conversation", "chat")

        return StreamingResponse(
            response_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"对话失败: {str(e)}")

@app.get("/api/chat/history")
def get_chat_history(project_id: str = "", session_id: str = "", limit: int = 50):
    conn = get_db()
    pid = project_id if project_id else "__global__"
    if session_id:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE project_id=? AND session_id=? ORDER BY id DESC LIMIT ?",
            (pid, session_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM conversations WHERE project_id=? ORDER BY id DESC LIMIT ?",
            (pid, limit),
        ).fetchall()
    conn.close()
    return list(reversed([dict(r) for r in rows]))

@app.get("/api/chat/sessions")
def list_sessions(project_id: str = ""):
    conn = get_db()
    pid = project_id if project_id else "__global__"
    rows = conn.execute(
        "SELECT session_id, MIN(created_at) as started, MAX(created_at) as last_msg, COUNT(*) as msg_count FROM conversations WHERE project_id=? GROUP BY session_id ORDER BY last_msg DESC",
        (pid,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/chat/sessions/{session_id}/link")
def link_session_to_project(session_id: str, project_id: str = ""):
    """Move a chat session from __global__ to a real project."""
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")
    conn = get_db()
    conn.execute(
        "UPDATE conversations SET project_id=? WHERE session_id=? AND project_id='__global__'",
        (project_id, session_id),
    )
    conn.commit()
    cnt = conn.execute(
        "SELECT COUNT(*) c FROM conversations WHERE session_id=? AND project_id=?",
        (session_id, project_id),
    ).fetchone()["c"]
    conn.close()
    return {"status": "ok", "migrated": cnt}

@app.delete("/api/chat/sessions/{session_id}")
def delete_session(session_id: str, project_id: str = ""):
    conn = get_db()
    if project_id:
        conn.execute("DELETE FROM conversations WHERE session_id=? AND project_id=?", (session_id, project_id))
    else:
        conn.execute("DELETE FROM conversations WHERE session_id=?", (session_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# Experiment API
# ---------------------------------------------------------------------------
@app.get("/api/experiments")
def list_experiments(project_id: str = ""):
    conn = get_db()
    if project_id:
        rows = conn.execute("SELECT * FROM experiments WHERE project_id=? ORDER BY priority DESC, created_at DESC", (project_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM experiments ORDER BY priority DESC, created_at DESC").fetchall()
    result = []
    for r in rows:
        e = dict(r)
        steps = conn.execute("SELECT * FROM pipeline_steps WHERE experiment_id=? ORDER BY step_index", (r["id"],)).fetchall()
        e["steps"] = [dict(s) for s in steps]
        result.append(e)
    conn.close()
    return result

@app.post("/api/experiments")
def create_experiment(payload: ExperimentCreate):
    eid = f"exp_{uuid.uuid4().hex[:8]}"
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO experiments (id, project_id, name, hypothesis, priority, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (eid, payload.project_id, payload.name, payload.hypothesis, payload.priority, now, now),
    )
    for i, stage in enumerate(STAGES):
        conn.execute(
            "INSERT INTO pipeline_steps (experiment_id, stage, step_index, description, status) VALUES (?,?,?,?,?)",
            (eid, stage, i, STAGE_LABELS[stage], "pending"),
        )
    conn.commit()
    conn.close()
    emit_event(f"project_{payload.project_id}", "experiment_created", {"experiment_id": eid, "name": payload.name})
    return {"id": eid, "status": "ok"}

@app.post("/api/experiments/{exp_id}/start")
async def start_experiment(exp_id: str, payload: ExperimentStart = None):
    if payload is None:
        payload = ExperimentStart()
    conn = get_db()
    exp = conn.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
    if not exp:
        conn.close()
        raise HTTPException(status_code=404, detail="实验不存在")
    if exp["status"] == "running":
        conn.close()
        raise HTTPException(status_code=400, detail="实验正在运行")

    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE pipeline_steps SET status='pending', started_at=NULL, completed_at=NULL, output_data='' WHERE experiment_id=?", (exp_id,))
    conn.execute("UPDATE experiments SET status='queued', current_stage='', execution_mode=?, updated_at=? WHERE id=?", (payload.execution_mode, now, exp_id))
    conn.commit()
    conn.close()

    asyncio.create_task(run_fars_pipeline(exp_id, exp["project_id"], execution_mode=payload.execution_mode))
    mode_label = "模拟执行" if payload.execution_mode == "simulate" else "真实执行"
    return {"status": "ok", "message": f"实验 {exp_id} 已启动（{mode_label}）"}

@app.post("/api/experiments/{exp_id}/stop")
def stop_experiment(exp_id: str):
    if exp_id in running_experiments:
        running_experiments[exp_id] = False
    conn = get_db()
    conn.execute("UPDATE experiments SET status='stopped', updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), exp_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/api/experiments/{exp_id}")
def get_experiment(exp_id: str):
    conn = get_db()
    exp = conn.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
    if not exp:
        conn.close()
        raise HTTPException(status_code=404, detail="实验不存在")
    result = dict(exp)
    result["steps"] = [dict(s) for s in conn.execute(
        "SELECT * FROM pipeline_steps WHERE experiment_id=? ORDER BY step_index", (exp_id,)
    ).fetchall()]
    result["logs"] = [dict(l) for l in conn.execute(
        "SELECT * FROM logs WHERE experiment_id=? ORDER BY id DESC LIMIT 100", (exp_id,)
    ).fetchall()]
    result["findings"] = [dict(f) for f in conn.execute(
        "SELECT * FROM findings WHERE experiment_id=? ORDER BY id DESC", (exp_id,)
    ).fetchall()]
    conn.close()
    return result

@app.post("/api/experiments/{exp_id}/feedback")
def add_feedback(exp_id: str, payload: FeedbackPayload):
    conn = get_db()
    exp = conn.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
    if not exp:
        conn.close()
        raise HTTPException(status_code=404, detail="实验不存在")
    now = datetime.now(timezone.utc).isoformat()
    existing = exp["feedback"] or ""
    new_feedback = f"{existing}\n[{now}] {payload.feedback}" if existing else f"[{now}] {payload.feedback}"
    conn.execute("UPDATE experiments SET feedback=?, updated_at=? WHERE id=?", (new_feedback, now, exp_id))
    conn.commit()
    store_memory(exp["project_id"], f"实验 {exp['name']} 反馈: {payload.feedback}", "feedback", "user")
    conn.close()
    return {"status": "ok"}

@app.delete("/api/experiments/{exp_id}")
def delete_experiment(exp_id: str):
    conn = get_db()
    conn.execute("DELETE FROM experiments WHERE id=?", (exp_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# Stage Reports API
# ---------------------------------------------------------------------------
@app.get("/api/experiments/{exp_id}/reports")
def get_experiment_reports(exp_id: str):
    """List all stage reports for an experiment, ordered by created_at DESC."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM stage_reports WHERE experiment_id=? ORDER BY created_at DESC",
        (exp_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.delete("/api/reports/{report_id}")
def delete_report(report_id: int):
    """Manually delete a specific stage report."""
    conn = get_db()
    conn.execute("DELETE FROM stage_reports WHERE id=?", (report_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# Experiment Comparison API
# ---------------------------------------------------------------------------
@app.post("/api/experiments/compare")
async def compare_experiments(payload: CompareRequest):
    """Compare multiple experiments side-by-side."""
    conn = get_db()
    experiments = []
    for eid in payload.experiment_ids[:5]:  # Max 5
        exp = conn.execute("SELECT * FROM experiments WHERE id=?", (eid,)).fetchone()
        if exp:
            e = dict(exp)
            steps = conn.execute("SELECT stage, status, output_data FROM pipeline_steps WHERE experiment_id=? ORDER BY step_index", (eid,)).fetchall()
            e["steps"] = [dict(s) for s in steps]
            experiments.append(e)
    conn.close()

    if len(experiments) < 2:
        return {"error": "至少需要 2 个实验来对比"}

    # Try LLM-powered comparison
    try:
        summaries = []
        for e in experiments:
            summaries.append(f"### {e['name']} [{e['status']}]\n- 假设: {e.get('hypothesis', '')[:300]}\n- 指标: {e.get('metrics', '{}')[:300]}\n- 结论: {e.get('result_summary', '')[:300]}")

        comparison_prompt = f"""请对以下 {len(experiments)} 个实验进行对比分析。

{chr(10).join(summaries)}

请生成对比报告，包括：
1. 各实验关键差异表格（Markdown格式）
2. 共同发现
3. 矛盾之处
4. 最优方案推荐
5. 下一步建议

用中文回答，Markdown格式。"""
        comparison = await call_llm(
            [{"role": "user", "content": comparison_prompt}],
            system_prompt="你是科研分析专家。请用中文进行深入的实验对比分析。"
        )
    except Exception:
        comparison = "LLM 未配置或调用失败。以下为原始数据对比。"

    return {
        "experiments": [{
            "id": e["id"],
            "name": e["name"],
            "status": e["status"],
            "hypothesis": e.get("hypothesis", ""),
            "metrics": e.get("metrics", "{}"),
            "result_summary": e.get("result_summary", ""),
            "created_at": e.get("created_at", ""),
        } for e in experiments],
        "comparison": comparison,
    }

# ---------------------------------------------------------------------------
# SSH Remote Execution API
# ---------------------------------------------------------------------------
@app.post("/api/ssh/execute")
async def ssh_exec(payload: SSHCommand):
    result = await ssh_execute(payload.command, payload.project_id, payload.experiment_id)
    return result

@app.post("/api/ssh/test")
async def ssh_test():
    result = await ssh_execute("echo 'LabOS SSH 连接成功' && hostname && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo 'No GPU detected'")
    return result

@app.post("/api/experiments/{exp_id}/execute")
async def execute_experiment_ssh(exp_id: str):
    """Execute experiment training script on remote server via SSH."""
    conn = get_db()
    exp = conn.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
    if not exp:
        conn.close()
        raise HTTPException(status_code=404, detail="实验不存在")

    project_id = exp["project_id"]
    proj = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    conn.close()

    # Build execution command based on experiment info
    exp_name_safe = re.sub(r'[^a-zA-Z0-9_-]', '_', exp["name"][:50])
    cmd = f"cd /root && echo '=== LabOS 远程执行 ===' && echo '实验: {exp['name']}' && echo '时间: $(date)' && nvidia-smi 2>/dev/null && echo '=== 环境就绪 ==='"

    # Log the execution start
    conn2 = get_db()
    now = datetime.now(timezone.utc).isoformat()
    conn2.execute("UPDATE experiments SET status='running', current_stage='ssh_execute', updated_at=? WHERE id=?", (now, exp_id))
    conn2.commit()
    conn2.close()
    emit_event(f"project_{project_id}", "experiment_update", {"experiment_id": exp_id, "status": "running", "stage": "ssh_execute"})

    result = await ssh_execute(cmd, project_id, exp_id)
    return {"status": "ok", "experiment_id": exp_id, "ssh_result": result}

# ---------------------------------------------------------------------------
# Pipeline Settings API
# ---------------------------------------------------------------------------
@app.get("/api/pipeline/settings")
def get_pipeline_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM configs WHERE category='pipeline'").fetchall()
    conn.close()
    config = {r["key"]: r["value"] for r in rows}
    return {
        "approval_enabled": config.get("approval_enabled", "true"),
        "auto_debug_enabled": config.get("auto_debug_enabled", "true"),
    }

@app.post("/api/pipeline/settings")
def save_pipeline_settings(payload: PipelineSettings):
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    data = payload.dict()
    for key in ["approval_enabled", "auto_debug_enabled"]:
        if key in data:
            conn.execute(
                "INSERT OR REPLACE INTO configs (key, value, category, updated_at) VALUES (?,?,?,?)",
                (key, data[key], "pipeline", now),
            )
    conn.commit()
    conn.close()
    # Update global auto-debug setting
    global AUTO_DEBUG_ENABLED
    AUTO_DEBUG_ENABLED = data.get("auto_debug_enabled", "true") == "true"
    return {"status": "ok"}

@app.get("/api/experiments/{exp_id}/approvals")
def get_approvals(exp_id: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM stage_approvals WHERE experiment_id=? ORDER BY created_at", (exp_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/experiments/{exp_id}/approve")
async def approve_stage(exp_id: str, payload: ApprovalAction):
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()

    stage = payload.stage
    action = payload.action
    comment = payload.comment

    # Determine the status string to store (approved, rejected, revised)
    status_str = action + "d"  # "approved", "rejected", "revised"

    # Update approval record
    conn.execute(
        "UPDATE stage_approvals SET status=?, reviewer_comment=?, decided_at=? WHERE experiment_id=? AND stage=? AND status='pending'",
        (status_str, comment, now, exp_id, stage),
    )

    exp = conn.execute("SELECT * FROM experiments WHERE id=?", (exp_id,)).fetchone()
    if not exp:
        conn.close()
        raise HTTPException(status_code=404, detail="实验不存在")

    if action == "approve":
        # Continue to next stage
        conn.execute("UPDATE experiments SET status='running', updated_at=? WHERE id=?", (now, exp_id))
        conn.commit()
        conn.close()
        asyncio.create_task(resume_pipeline(exp_id, exp["project_id"], stage))
        return {"status": "ok", "message": "已批准，继续执行下一阶段"}

    elif action == "reject":
        conn.execute("UPDATE experiments SET status='rejected', updated_at=? WHERE id=?", (now, exp_id))
        conn.commit()
        conn.close()
        log_to_db(exp_id, exp["project_id"], "approval", f"[审批] 阶段 {stage} 被拒绝: {comment}")
        return {"status": "ok", "message": "实验已拒绝"}

    elif action == "revise":
        # Reset the current stage to re-run it
        conn.execute("UPDATE experiments SET status='running', updated_at=? WHERE id=?", (now, exp_id))
        conn.execute(
            "UPDATE pipeline_steps SET status='pending', output_data='', started_at=NULL, completed_at=NULL WHERE experiment_id=? AND stage=?",
            (exp_id, stage),
        )
        conn.commit()
        conn.close()
        # Re-run from this stage with the comment as additional context
        prev = get_prev_stage(stage)
        asyncio.create_task(resume_pipeline(exp_id, exp["project_id"], prev, revision_note=comment))
        return {"status": "ok", "message": "已标记修改，重新执行该阶段"}

    conn.close()
    return {"status": "error", "message": "未知操作"}

# ---------------------------------------------------------------------------
# Memory API (with semantic search)
# ---------------------------------------------------------------------------
@app.get("/api/memories")
def list_memories(project_id: str, category: str = ""):
    return retrieve_memories(project_id, limit=100, category=category or None)

@app.post("/api/memories")
async def add_memory(payload: MemoryCreate):
    await store_memory_with_embedding(payload.project_id, payload.content, payload.category, "user")
    return {"status": "ok"}

@app.post("/api/memories/search")
async def search_memories(payload: MemorySearch):
    results = await semantic_search_memories(payload.project_id, payload.query, payload.top_k)
    return results

@app.post("/api/memories/reindex")
async def reindex_memories(project_id: str = ""):
    """Recompute embeddings for all memories that don't have them."""
    conn = get_db()
    if project_id:
        rows = conn.execute("SELECT id, content FROM memories WHERE project_id=? AND (embedding='' OR embedding IS NULL)", (project_id,)).fetchall()
    else:
        rows = conn.execute("SELECT id, content FROM memories WHERE embedding='' OR embedding IS NULL").fetchall()
    conn.close()

    if not rows:
        return {"status": "ok", "message": "无需重建索引", "processed": 0}

    batch_size = 20
    processed = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        texts = [r["content"] for r in batch]
        try:
            embeddings = await call_embedding(texts)
            if embeddings and len(embeddings) == len(batch):
                conn = get_db()
                for j, r in enumerate(batch):
                    conn.execute("UPDATE memories SET embedding=? WHERE id=?", (json.dumps(embeddings[j]), r["id"]))
                conn.commit()
                conn.close()
                processed += len(batch)
        except Exception:
            pass

    return {"status": "ok", "message": f"已重建 {processed}/{len(rows)} 条记忆的向量索引", "processed": processed}

@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int):
    conn = get_db()
    conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# GitHub Analysis API
# ---------------------------------------------------------------------------
@app.post("/api/github/analyze")
async def analyze_repo(project_id: str = "", repo_url: str = ""):
    """Analyze a GitHub repository."""
    if not repo_url:
        if project_id:
            conn = get_db()
            proj = conn.execute("SELECT repo_url FROM projects WHERE id=?", (project_id,)).fetchone()
            conn.close()
            if proj and proj["repo_url"]:
                repo_url = proj["repo_url"]
            else:
                return {"error": "项目无仓库 URL"}
        else:
            return {"error": "请提供 repo_url 或 project_id"}

    result = await analyze_github_repo(repo_url)
    if "error" in result:
        return result

    # LLM analysis of code structure
    file_tree_str = "\n".join(result.get("file_tree", [])[:200])
    readme = result.get("readme", "")[:4000]

    analysis_text = ""
    try:
        analysis_text = await call_llm([{"role": "user", "content": f"""请分析以下 GitHub 仓库的代码架构。

## 仓库信息
- URL: {repo_url}
- 语言: {result.get('language', '未知')}
- Stars: {result.get('stars', 0)}
- 描述: {result.get('description', '')}

## 文件结构
{file_tree_str}

## README (节选)
{readme}

请分析：
1. 项目架构概述
2. 核心模块和它们的职责
3. 技术栈和依赖
4. 代码组织风格
5. 潜在的改进点
6. 适合做实验的切入点

用中文回答，Markdown格式。"""}], system_prompt="你是资深代码架构分析师。")
    except Exception:
        analysis_text = f"# 仓库分析\n\n仓库: {repo_url}\n语言: {result.get('language', '')}\n文件数: {result.get('file_count', 0)}\n\nLLM 未配置，无法生成深度分析。"

    # Cache result
    if project_id:
        conn = get_db()
        conn.execute(
            "INSERT INTO code_analyses (project_id, repo_url, file_tree, analysis, readme_content) VALUES (?,?,?,?,?)",
            (project_id, repo_url, json.dumps(result.get("file_tree", [])[:200]), analysis_text[:10000], readme[:5000]),
        )
        conn.commit()
        # Store as memory
        conn.execute(
            "INSERT INTO memories (project_id, category, content, source) VALUES (?,?,?,?)",
            (project_id, "code_analysis", f"代码分析: {analysis_text[:500]}", "github"),
        )
        conn.commit()
        conn.close()

    return {
        "repo_url": repo_url,
        "description": result.get("description", ""),
        "language": result.get("language", ""),
        "stars": result.get("stars", 0),
        "file_count": result.get("file_count", 0),
        "file_tree": result.get("file_tree", [])[:100],
        "analysis": analysis_text,
    }

@app.get("/api/github/analyses")
def get_code_analyses(project_id: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM code_analyses WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# Paper Search API
# ---------------------------------------------------------------------------
@app.post("/api/papers/search")
async def search_papers_api(payload: PaperSearchQuery):
    papers = await search_papers(payload.query, payload.limit)

    if payload.project_id and papers and "error" not in papers[0]:
        conn = get_db()
        for p in papers:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO papers (id, project_id, title, authors, year, abstract, url, citation_count, venue) VALUES (?,?,?,?,?,?,?,?,?)",
                    (p["paper_id"], payload.project_id, p["title"], p["authors"], p["year"], p["abstract"], p["url"], p["citation_count"], p["venue"]),
                )
            except Exception:
                pass
        conn.commit()
        conn.close()

    return papers

@app.get("/api/papers")
def list_papers(project_id: str):
    conn = get_db()
    rows = conn.execute("SELECT * FROM papers WHERE project_id=? ORDER BY citation_count DESC", (project_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.delete("/api/papers/{paper_id}")
def delete_paper(paper_id: str):
    conn = get_db()
    conn.execute("DELETE FROM papers WHERE id=?", (paper_id,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

# ---------------------------------------------------------------------------
# Dify API
# ---------------------------------------------------------------------------
@app.post("/api/dify/run")
async def run_dify(payload: DifyRequest):
    result = await call_dify_workflow(payload.inputs, payload.query)
    return result

# ---------------------------------------------------------------------------
# SSE Event Stream
# ---------------------------------------------------------------------------
@app.get("/api/events/{channel}")
async def event_stream(channel: str):
    queue = asyncio.Queue(maxsize=200)
    if channel not in event_subscribers:
        event_subscribers[channel] = []
    event_subscribers[channel].append(queue)

    async def generator():
        try:
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if channel in event_subscribers and queue in event_subscribers[channel]:
                event_subscribers[channel].remove(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )

# ---------------------------------------------------------------------------
# Logs API
# ---------------------------------------------------------------------------
@app.get("/api/logs")
def get_logs(experiment_id: str = "", project_id: str = "", limit: int = 100):
    conn = get_db()
    if experiment_id:
        rows = conn.execute("SELECT * FROM logs WHERE experiment_id=? ORDER BY id DESC LIMIT ?", (experiment_id, limit)).fetchall()
    elif project_id:
        rows = conn.execute("SELECT * FROM logs WHERE project_id=? ORDER BY id DESC LIMIT ?", (project_id, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return list(reversed([dict(r) for r in rows]))

# ---------------------------------------------------------------------------
# Stats API (enhanced)
# ---------------------------------------------------------------------------
@app.get("/api/stats")
def get_stats(project_id: str = ""):
    conn = get_db()
    if project_id:
        total_exp = conn.execute("SELECT COUNT(*) c FROM experiments WHERE project_id=?", (project_id,)).fetchone()["c"]
        running = conn.execute("SELECT COUNT(*) c FROM experiments WHERE project_id=? AND status='running'", (project_id,)).fetchone()["c"]
        completed = conn.execute("SELECT COUNT(*) c FROM experiments WHERE project_id=? AND status='completed'", (project_id,)).fetchone()["c"]
        failed = conn.execute("SELECT COUNT(*) c FROM experiments WHERE project_id=? AND status='failed'", (project_id,)).fetchone()["c"]
        memories = conn.execute("SELECT COUNT(*) c FROM memories WHERE project_id=?", (project_id,)).fetchone()["c"]
        findings_count = conn.execute("SELECT COUNT(*) c FROM findings WHERE project_id=?", (project_id,)).fetchone()["c"]
        papers_count = conn.execute("SELECT COUNT(*) c FROM papers WHERE project_id=?", (project_id,)).fetchone()["c"]
        conversations_count = conn.execute("SELECT COUNT(*) c FROM conversations WHERE project_id=?", (project_id,)).fetchone()["c"]
        sessions_count = conn.execute("SELECT COUNT(DISTINCT session_id) c FROM conversations WHERE project_id=?", (project_id,)).fetchone()["c"]
    else:
        total_exp = conn.execute("SELECT COUNT(*) c FROM experiments").fetchone()["c"]
        running = conn.execute("SELECT COUNT(*) c FROM experiments WHERE status='running'").fetchone()["c"]
        completed = conn.execute("SELECT COUNT(*) c FROM experiments WHERE status='completed'").fetchone()["c"]
        failed = conn.execute("SELECT COUNT(*) c FROM experiments WHERE status='failed'").fetchone()["c"]
        memories = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
        findings_count = conn.execute("SELECT COUNT(*) c FROM findings").fetchone()["c"]
        papers_count = conn.execute("SELECT COUNT(*) c FROM papers").fetchone()["c"]
        conversations_count = conn.execute("SELECT COUNT(*) c FROM conversations").fetchone()["c"]
        sessions_count = conn.execute("SELECT COUNT(DISTINCT session_id) c FROM conversations").fetchone()["c"]
    projects = conn.execute("SELECT COUNT(*) c FROM projects WHERE id != '__global__'").fetchone()["c"]

    # Experiment timeline data (last 30 days)
    timeline = []
    rows = conn.execute("""
        SELECT DATE(created_at) as day, status, COUNT(*) as cnt
        FROM experiments
        WHERE created_at > datetime('now', '-30 days')
        GROUP BY DATE(created_at), status
        ORDER BY day
    """).fetchall()
    for r in rows:
        timeline.append({"day": r["day"], "status": r["status"], "count": r["cnt"]})

    # Memory category breakdown
    mem_categories = []
    cat_rows = conn.execute("""
        SELECT category, COUNT(*) as cnt FROM memories
        {} GROUP BY category ORDER BY cnt DESC
    """.format(f"WHERE project_id='{project_id}'" if project_id else "")).fetchall()
    for r in cat_rows:
        mem_categories.append({"category": r["category"], "count": r["cnt"]})

    conn.close()
    return {
        "projects": projects,
        "total_experiments": total_exp,
        "running": running,
        "completed": completed,
        "failed": failed,
        "memories": memories,
        "findings": findings_count,
        "papers": papers_count,
        "conversations": conversations_count,
        "sessions": sessions_count,
        "timeline": timeline,
        "memory_categories": mem_categories,
    }

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "3.4.1", "timestamp": datetime.now(timezone.utc).isoformat()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
