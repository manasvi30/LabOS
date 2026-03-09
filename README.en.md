# LabOS

Next-generation fully automated research platform. Covers the complete research lifecycle: idea generation, literature review, hypothesis design, experiment execution, and paper writing. Local-first, multi-model, fully open-source.

[中文版 README.md](./README.md)

### ✦ Core Capabilities

> **Research Pipeline** · 4-stage automation &nbsp;│&nbsp; **AI Chat** · Multi-model streaming &nbsp;│&nbsp; **Experiment Execution** · SSH to remote GPU servers &nbsp;│&nbsp; **Codex Integration** · AI writes experiment code
>
> **Memory System** · Cross-experiment knowledge persistence &nbsp;│&nbsp; **Multi-LLM Profiles** · General / Code / Paper / Experiment &nbsp;│&nbsp; **Stage Reports** · Research → Analysis → Experiment reports &nbsp;│&nbsp; **Local-first** · Your data stays with you

## Screenshots

### Project Management
Multi-project management with independent experiments, memory, and paper library per project.

![LabOS Projects](docs/screenshots/screenshot-projects.jpg)

### Experiment Pipeline
4-stage pipeline with approval gates: approve → next stage / revise & rerun / reject & stop.

![LabOS Experiment](docs/screenshots/screenshot-experiment.jpg)

### Dashboard
Quick overview with shortcuts to chat, projects, and new experiments.

![LabOS Dashboard](docs/screenshots/screenshot-dashboard.png)

## Features

- **4-Stage Research Pipeline** — Ideation → Planning → Experiment → Writing, each stage produces independent reports
- **Chat-Driven** — AI assistant with streaming, create projects directly from conversations
- **Multi-LLM Profiles** — Separate model configs for general chat, code analysis, paper writing, and experiment design
- **Remote Experiment Execution** — SSH to GPU servers (AutoDL, etc.) with real-time log streaming via SSE
- **Codex CLI Integration** — Full-auto mode with JSONL streaming output
- **Memory System** — Cross-experiment knowledge persistence with project-level memory retrieval
- **Stage Approval Gates** — Each pipeline stage supports: approve / revise & rerun / reject
- **Fully Configurable** — All settings via web UI, any OpenAI-compatible API
- **Local-First** — SQLite storage, all data on your machine

## Quick Start

```bash
git clone https://github.com/YUANXICHE98/LabOS.git
cd LabOS
bash start.sh
```

Or manually:

```bash
pip install -r requirements.txt
cd src && python api_server.py
```

Open `http://localhost:8000` in your browser.

### First-Time Setup

1. Go to **Settings** → configure your LLM API endpoint and key (any OpenAI-compatible API)
2. (Optional) Configure SSH server for remote experiment execution
3. Go to **Chat** → start a conversation → create a project from chat
4. Or go to **Projects** → create a project → launch experiments

## Project Structure

```
LabOS/
├── src/
│   ├── api_server.py      # FastAPI backend — all API endpoints & pipeline logic
│   ├── index.html          # Main page (single-page app)
│   ├── app.js              # Frontend — UI logic, API calls, SSE streaming
│   └── style.css           # Styles
├── docs/
│   ├── screenshots/        # Screenshots
│   └── videos/             # Demo videos
├── start.sh                # One-click start script
├── requirements.txt        # Python dependencies
├── CONTRIBUTING.md         # Contribution guide
├── GOVERNANCE.md           # Contributor governance & incentives
└── LICENSE                 # AGPL-3.0
```

## Tech Stack

- **Backend** — Python / FastAPI / uvicorn
- **Database** — SQLite (zero-config, local file)
- **Frontend** — Vanilla HTML + CSS + JavaScript (no build step)
- **Remote Execution** — Paramiko (SSH)
- **LLM Calls** — httpx (OpenAI-compatible protocol)
- **Real-time** — Server-Sent Events (SSE)

## Open Core Model

### 🆓 Free & Open Source (this repo)
Everything here is permanently free: full research pipeline, chat, project management, LLM config, memory system, experiment execution.

### 💎 Premium (coming soon)
- **Skill Library** — Curated research methodologies, proven experiment paths, best practices
- **Advanced Integrations** — Pre-built connectors for cloud GPU platforms, HPC clusters
- **Priority Support** — Direct access to the dev team

## Contributor Incentives

See **[GOVERNANCE.md](./GOVERNANCE.md)** for the full program.

| Level | Requirement | Reward |
|-------|------------|--------|
| Contributor | 1 merged PR | Name in Contributors Wall |
| Active Contributor | 3+ PRs | Free Skill Library access + beta |
| Core Contributor | 10+ PRs or 1 major feature | **30% revenue share** + paper co-authorship |
| Bounty Issues | `💰 bounty` label | Crypto / Sponsors cash |

All contribution types count equally. See [CONTRIBUTING.md](./CONTRIBUTING.md) and [Issues](https://github.com/YUANXICHE98/LabOS/issues).

## Support

| Channel | Address |
|---------|---------|
| **ETH / ERC-20** | `0xc6B4720835E6C3CB58618B4df26B64F595C30202` |
| **GitHub Sponsors** | Click the **Sponsor** button at the top of this repo |

## Contributors

<a href="https://github.com/YUANXICHE98/LabOS/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=YUANXICHE98/LabOS" />
</a>

## License

[AGPL-3.0](./LICENSE) — Modified versions must be open-sourced. Network services must provide source code.
