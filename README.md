# ZEUS

**ZEUS** is a local, offline-first AI desktop assistant and industrial workbench prototype.

It runs entirely on your machine using local models (via Ollama). No cloud services are required after the initial setup.

> **Status:** Active prototype / work-in-progress  
> Built by a first-year B.Tech student exploring systems design, local AI, and quantitative finance.

---

## What ZEUS currently does

- Local conversational AI using Ollama models
- Desktop automation (open apps, type, screenshots, basic system control)
- Document generation (Word, PowerPoint, Excel)
- Offline voice input/output (optional)
- Local knowledge base / RAG over sample industrial documents
- Basic security features (local login, confirmation for risky actions)
- Modular tool system and decision routing

Many advanced features (industrial agents, face login, firewall tools, etc.) exist in the codebase but are still experimental.

---

## Demo Login

| Field     | Value     |
|-----------|-----------|
| Username  | `admin`   |
| Password  | `zeuscore`|

Face login is disabled by default.

---

## Requirements

- Windows 10 or 11 (64-bit)
- Python 3.11 or 3.12
- [Ollama](https://ollama.com) installed
- At least 16 GB RAM recommended (8 GB minimum)
- NVIDIA GPU optional but helpful

---

## Installation

### 1. Install prerequisites

1. Install **Python 3.11 or 3.12** from [python.org](https://www.python.org/downloads/windows/)  
   → Check the box **"Add python.exe to PATH"**

2. Install **Ollama** from [ollama.com](https://ollama.com)

### 2. Set up the project

```bash
# Open a terminal in the ZEUS folder
python -m venv .venv
.venv\Scripts\activate

pip install -r config\requirements.txt
```

(If you have `requirements-industrial.txt` or `requirements-voice.txt` in `config\`, you can install them later if needed, or simply double-click `STEP 1 ꞉ INSTALL.bat`.)

### 3. Pull recommended models

Open a new terminal and run:

```bash
ollama pull qwen2.5:7b
ollama pull nomic-embed-text
```

You can pull smaller or larger models later depending on your hardware.

### 4. Run ZEUS

Double-click:

- `STEP 2 ꞉ START.bat`  
  **or**

```bash
.venv\Scripts\activate
python "User Editable Files\main.py"
```

The first run may take longer while databases and components initialize.

---

## Important Notes & Limitations

- This is a **prototype**. Not all features are fully stable or production-ready.
- Large parts of the system were developed with significant AI assistance.
- Models are **not** included in this repository. You must pull them with Ollama.
- Face login, advanced security modules, and the full industrial stack are experimental.
- The included knowledge data is sample/demo data only.
- Tested primarily on Windows with an NVIDIA GPU.

---

## Project Structure (high level)

```
ZEUS/
├── User Editable Files/     # main.py, password tools, launcher
├── core/                    # Decision engine, tools, model routing
├── auth/                    # Login & session management
├── industrial/              # Document intelligence & industrial features
├── memory/                  # Local memory and databases
├── security/                # Policy, audit, path guards
├── voice/                   # Offline speech components
├── ui/                      # Desktop interface
├── knowledge/               # Sample knowledge packs
├── config/                  # Settings and requirements
└── README.md                # Project overview & setup guide
```

---

## Future Direction

I am actively improving ZEUS while also building projects focused on **market data analysis** and quantitative tools, along with consistent algorithm practice.

---

## License

MIT License – feel free to explore and build upon it.
