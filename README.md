<p align="center">
  <h1 align="center">🔬 LabOS</h1>
  <p align="center"><b>Fully Automated Research System — 全自动科研实验平台</b></p>
  <p align="center">
    <a href="#features">Features</a> •
    <a href="#architecture">Architecture</a> •
    <a href="#quick-start">Quick Start</a> •
    <a href="#configuration">Configuration</a> •
    <a href="#contributing">Contributing</a> •
    <a href="#license">License</a>
  </p>
  <p align="center">
    <img src="https://img.shields.io/badge/license-AGPL--3.0-blue.svg" alt="License">
    <img src="https://img.shields.io/badge/python-3.10+-green.svg" alt="Python">
    <img src="https://img.shields.io/badge/status-active%20development-orange.svg" alt="Status">
  </p>
</p>

---

## What is LabOS?

**LabOS** is an open-source, self-hosted platform for **fully automated research experiments**. It manages the entire research lifecycle — from idea generation and literature review, through hypothesis design and experiment execution, to final paper writing — with minimal human intervention.

**LabOS** 是一个开源的、可自部署的**全自动科研实验平台**。它管理完整的研究生命周期：从创意生成、文献调研，到假设设计、实验执行，再到论文撰写，全程最少人工干预。

### Why LabOS?

| Pain Point | LabOS Solution |
|---|---|
| 手动跑实验、反复调参 | 自动化流水线，连接远程 GPU 服务器，全自动执行 |
| 实验结果散落各处 | 统一 Dashboard，实验日志 + 报告 + 记忆系统 |
| 想法到验证周期太长 | 对话即创建项目，AI 辅助快速迭代 |
| 代码实现门槛高 | 集成 Codex CLI，AI 自动写实验代码 |

---

## Features

### 🧪 Four-Stage Research Pipeline
```
创意生成 (Ideation) → 方案设计 (Planning) → 实验执行 (Experiment) → 论文撰写 (Writing)
```
Each stage produces reports and supports human review / approval gates.

### 💬 Chat-Driven Interface
- Conversational AI assistant for research brainstorming
- Create projects directly from chat conversations
- Multiple LLM profiles: general / code / paper / experiment
- Configurable LLM backends (bring your own API key & endpoint)

### 🖥️ Remote Experiment Execution
- SSH into GPU servers (AutoDL, etc.) for experiment runs
- Real-time log streaming via SSE (Server-Sent Events)
- Codex CLI integration with full-auto mode and JSONL streaming
- Live experiment monitoring from the web dashboard

### 📊 Unified Dashboard
- Project management with experiment tracking
- Memory system for cross-experiment knowledge persistence
- Paper & findings library
- Per-stage reports (research report → analysis report → experiment report)

### ⚙️ Fully Configurable
- Multiple LLM configurations (different models for different tasks)
- Custom API endpoints (OpenAI-compatible)
- SSH server settings
- Embedding model configuration
- All settings exposed via web UI — no code changes needed

---

## Architecture

```
┌─────────────────────────────────────────────┐
│                  Frontend                    │
│         HTML + CSS + Vanilla JS             │
│    Dashboard / Projects / Chat / Settings    │
└──────────────────┬──────────────────────────┘
                   │ REST + SSE
┌──────────────────▼──────────────────────────┐
│              FastAPI Backend                  │
│                                              │
│  ┌──────────┐ ┌──────────┐ ┌──────────────┐ │
│  │ Pipeline │ │ Chat API │ │ Experiment   │ │
│  │ Engine   │ │ (Stream) │ │ Runner (SSH) │ │
│  └──────────┘ └──────────┘ └──────────────┘ │
│  ┌──────────┐ ┌──────────┐ ┌──────────────┐ │
│  │ Memory   │ │ LLM      │ │ Codex CLI    │ │
│  │ System   │ │ Profiles │ │ Integration  │ │
│  └──────────┘ └──────────┘ └──────────────┘ │
└──────────────────┬──────────────────────────┘
                   │
        ┌──────────┼──────────┐
        ▼          ▼          ▼
   SQLite DB   LLM APIs   GPU Servers
              (configurable)  (via SSH)
```

---

## Quick Start

### Prerequisites
- Python 3.10+
- `pip install fastapi uvicorn paramiko httpx`

### Run

```bash
git clone https://github.com/YUANXICHE98/LabOS.git
cd LabOS
pip install fastapi uvicorn paramiko httpx
python api_server.py
```

Open `http://localhost:8000` in your browser.

### First Steps
1. Go to **Settings** → configure your LLM API endpoint and key
2. (Optional) Configure SSH for remote experiment execution
3. Go to **Chat** → start a conversation → create a project from chat
4. Or go to **Projects** → create a project manually → run experiments

---

## Configuration

All configuration is done through the web UI under **Settings**:

| Config | Description |
|--------|-------------|
| **LLM API** | OpenAI-compatible endpoint URL + API key + model name |
| **LLM Profiles** | Separate configs for general / code / paper / experiment tasks |
| **SSH Server** | Host, port, username, key for remote GPU servers |
| **Embedding** | Embedding model endpoint for memory retrieval |

You can use any OpenAI-compatible API: OpenAI, DeepSeek, Claude (via adapter), local models (vLLM, Ollama), etc.

---

## Contributing

We welcome contributions! LabOS is in active development and there's plenty to work on:

### Areas We Need Help With
- 🧠 **Memory System** — better retrieval algorithms, knowledge graph integration
- 🔬 **Pipeline Stages** — more experiment templates, domain-specific pipelines
- 📝 **Paper Writing** — LaTeX generation, citation management
- 🌐 **Frontend** — UI/UX improvements, visualization
- 🔌 **Integrations** — more LLM providers, cloud GPU platforms, arXiv API
- 📖 **Documentation** — tutorials, examples, translations

### How to Contribute
1. Fork this repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

All contributions must be compatible with the AGPL-3.0 license. By contributing, you agree that your contributions will be licensed under the same license.

---

## Roadmap

- [x] Multi-project management
- [x] Chat-to-project creation
- [x] Codex CLI real-time streaming
- [x] Multiple LLM profile configuration
- [x] SSH remote experiment execution
- [ ] arXiv paper search integration
- [ ] Experiment result visualization (charts, plots)
- [ ] Multi-user support
- [ ] Docker deployment
- [ ] Plugin system for custom pipeline stages
- [ ] Knowledge graph memory backend

---

## License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.

This means:
- ✅ You can use, modify, and distribute this software freely
- ✅ You can use it for commercial purposes
- ⚠️ Any modified version **must** also be open-sourced under AGPL-3.0
- ⚠️ If you run a modified version as a network service, you **must** make the source code available to users
- ⚠️ All derivative works must reference this upstream repository

See [LICENSE](./LICENSE) for the full text.

---

<p align="center">
  <b>Built for researchers, by researchers.</b><br>
  为科研人打造的自动化实验平台。
</p>
