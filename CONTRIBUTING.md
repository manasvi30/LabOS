# Contributing to LabOS | 贡献指南

Thank you for your interest in contributing to LabOS! Whether it's a bug fix, new feature, documentation, or translation — every contribution matters.

感谢你对 LabOS 的关注！无论是修 bug、新功能、文档还是翻译，每一份贡献都很重要。

---

## 🚀 Getting Started | 快速开始

### Prerequisites | 前置条件

- Python 3.10+
- Git

### Setup | 搭建开发环境

```bash
# 1. Fork and clone
git clone https://github.com/<your-username>/LabOS.git
cd LabOS

# 2. Install dependencies
pip install fastapi uvicorn paramiko httpx

# 3. Run the server
python api_server.py

# 4. Open http://localhost:8000
```

### First-time Configuration | 首次配置

1. Open **Settings** in the web UI
2. Configure your LLM API endpoint (any OpenAI-compatible API works)
3. (Optional) Configure SSH for remote experiment execution

---

## 📁 Project Structure | 项目结构

```
LabOS/
├── api_server.py      # FastAPI backend — all API endpoints and pipeline logic
├── index.html         # Main HTML page (single-page app)
├── app.js             # Frontend JavaScript — UI logic, API calls, SSE
├── style.css          # All styles
├── LICENSE            # AGPL-3.0
└── README.md
```

Key areas in `api_server.py`:
- **Database schema**: `init_db()` — SQLite tables for projects, experiments, conversations, memories, etc.
- **Pipeline engine**: `run_fars_pipeline()` — the 4-stage research automation pipeline
- **Chat API**: `/api/chat` — streaming LLM chat with memory integration
- **Experiment runner**: `ssh_execute()`, `ssh_execute_codex_streaming()` — remote execution via SSH
- **Memory system**: `store_memory()`, `retrieve_memories()` — project knowledge persistence

---

## 🔧 How to Contribute | 如何贡献

### 1. Find Something to Work On | 找到要做的事

- Browse [open issues](https://github.com/YUANXICHE98/LabOS/issues)
- Look for `good first issue` labels — these are beginner-friendly
- Look for `help wanted` labels — these are where we need the most help
- Or propose your own idea by opening a new issue

### 2. Development Flow | 开发流程

```bash
# Create a feature branch
git checkout -b feature/your-feature-name

# Make your changes
# ...

# Test locally
python api_server.py
# Open http://localhost:8000 and verify your changes

# Commit with a clear message
git commit -m "Add: your feature description"

# Push and open a PR
git push origin feature/your-feature-name
```

### 3. Pull Request Guidelines | PR 要求

- Keep PRs focused — one feature or fix per PR
- Add a clear description of what changed and why
- Include screenshots for UI changes
- Test your changes locally before submitting

---

## 🏗️ Development Tips | 开发提示

### Backend (Python / FastAPI)

- The server auto-creates the SQLite database on first run
- Seed data (default configs, LLM profiles) is inserted in `seed_pipeline_configs()` and `seed_default_llm_profiles()`
- SSE streaming: use `StreamingResponse` with `text/event-stream` content type
- Error codes: use 4xx (not 5xx) — the proxy clamps 5xx to 422

### Frontend (Vanilla JS)

- No build step required — edit and refresh
- API base URL uses `__PORT_8000__` placeholder (replaced at deploy time)
- For local development, the frontend connects directly to `localhost:8000`
- SSE connections use `EventSource` API

### Adding a New API Endpoint

1. Define your Pydantic model in `api_server.py`
2. Add your route with `@app.get/post/delete`
3. Update the frontend in `app.js` to call it
4. Test with `curl` first, then in the browser

---

## 📜 License | 许可

By contributing to LabOS, you agree that your contributions will be licensed under the [AGPL-3.0 License](./LICENSE).

贡献代码即表示你同意你的贡献将以 [AGPL-3.0](./LICENSE) 协议授权。

---

## 💬 Questions? | 有问题？

Open an [issue](https://github.com/YUANXICHE98/LabOS/issues) — we're happy to help!

有任何问题，欢迎开 [Issue](https://github.com/YUANXICHE98/LabOS/issues) 讨论。
