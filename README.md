> **Disclaimer:** This README was written with AI assistance. This is a personal, ad-hoc project built for my own use — expect rough edges and fast-moving changes.

# ollama-proxy

A FastAPI proxy that sits between [Open WebUI](https://github.com/open-webui/open-webui) and a local [llama.cpp](https://github.com/ggerganov/llama.cpp) server and turns a chat interface into a capable agentic coding assistant.

![Session screenshot](Screenshot_20260429_023155.png)

The proxy intercepts chat requests, detects when the model wants to use tools (via the native tool-call protocol), executes them in a sandboxed Docker container, and feeds results back — all transparently within Open WebUI's existing UI. No plugins, no custom frontend.

---

## What it does

- **Native tool-call protocol** — uses the model's own function-calling format, not XML hacks
- **Agentic loop** — automatically chains tool calls up to a configurable iteration limit
- **Working Document** — a persistent structured document (Project / Task / Findings / Plan / Decisions) injected into every turn so the model maintains context across a long session
- **Context eviction** — old tool results are compressed out of the shadow context; findings go into the Working Document first so nothing is lost
- **Stuck detection** — detects repeated identical tool calls across requests, injects a redirect nudge, and temporarily suppresses the offending tool from the schema
- **Branch-aware shadow context** — editing a prior message automatically creates a new branch; the model never sees Open WebUI's display-polluted history

### Tools available to the model

| Tool | What it does |
|------|-------------|
| `run_shell` | Execute shell commands in the sandbox |
| `run_python` | Execute Python snippets in the sandbox |
| `spawn_agent` | Focused sub-agent call with one or more files loaded — for reading and understanding code without bloating main context |
| `patch_file` | Surgical file edit via SEARCH/REPLACE — sub-agent sees full file, proxy applies the change |
| `write_file` | Generate a complete new file via sub-agent |
| `run_test` | Write and run a test hypothesis script in `/tmp` |
| `git_commit` | Stage all changes and commit, co-signed with the model name |
| `update_document` | Update a section of the Working Document |

---

## Quick start

### Prerequisites

- Docker
- [llama.cpp](https://github.com/ggerganov/llama.cpp) server running with a tool-call capable model (tested with Gemma 4 27B A4B)
- [Open WebUI](https://github.com/open-webui/open-webui) pointed at the proxy

### 1. Configure

```bash
cp config.example.toml config.toml
# Edit config.toml — set llama_base, proxy_port, model_name
```

### 2. Install proxy dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install fastapi uvicorn httpx toml aiofiles
```

### 3. Start the proxy

```bash
python3 proxy.py
```

### 4. Start the sandbox

Put the `sandbox` script in your PATH, then from any project directory:

```bash
sandbox build   # once — builds the Docker image (~2 min)
sandbox         # start (registers automatically with the proxy)
```

### 5. Point Open WebUI at the proxy

Set the Ollama API base in Open WebUI to `http://localhost:<proxy_port>`.

### 6. Load a skill

Start a chat with `#code` to activate the coding skill:

```
#code

Let's look at the task polling loop in modules/task_manager/task_manager.py
```

---

## Example session

A short session fixing a bug where tasks queued at runtime weren't picked up:

```
User:   #code fix the polling loop in task_manager — tasks added at runtime
        are never picked up until restart

Model:  [update_document: Task = "Fix runtime task polling in task_manager.py"]
        [run_shell: git ls-files modules/task_manager/]
        [spawn_agent: "List public functions with signatures..." → task_manager.py]
        [update_document: Findings = "_poll_loop() line 47: queries DB once at
         startup. No asyncio.Event. TaskQueue.add() writes DB but doesn't notify."]
        [update_document: Plan = "[ ] patch _poll_loop to re-query in a loop
         [ ] add asyncio.sleep(poll_interval)  [ ] verify with run_test"]
        [patch_file: "_poll_loop: change single startup query to continuous loop
         with asyncio.sleep(self.config['poll_interval'])"]

        Patch applied. Here's the diff: ...
        Standing by for you to review.

User:   looks good, commit it

Model:  [git_commit: "fix task poll loop to re-query at runtime"]
        ✓ Committed.
```

The Working Document stays visible at the top of every turn — the model always knows where it is in a task even after many tool calls and context evictions.

---

## Coding skill

Send `#code` at the start of any message to activate the agentic coding protocol. The model will:

1. Fill in the Working Document (Project / Task / Scope)
2. Explore the codebase with `spawn_agent` and `git grep` via `run_shell`
3. Write an explicit plan as a checklist
4. Apply changes with `patch_file` (for edits) or `write_file` (for new files)
5. Stand by after each change for you to review before committing

Key rules baked into the skill:
- Never uses `ls` or `find` — always `git ls-files` / `git grep`
- Never uses `write_file` on an existing file (silently drops other functions)
- Always verifies with `spawn_agent` after a patch
- Waits for user confirmation before committing unless told otherwise

---

## Architecture

```
Open WebUI
    │  POST /api/chat (Ollama format)
    ▼
proxy.py          — request routing, skill injection, shadow context management
    │
    ├─ skill_engine.py    — detects #triggers, injects skill system prompts
    ├─ session_manager.py — branch-aware shadow context, Working Document, eviction
    │
    └─ auto_runner.py     — agentic loop (stream → tool calls → execute → loop)
           │
           └─ tool_manager.py   — tool schemas, execution, sub-agent calls
                  │
                  └─ shell_server.py  — FastAPI inside Docker sandbox
                         (exec, run_test, patch_file, apply_patch, write_file)
```

---

## Configuration

See `config.example.toml` for all options. Key sections:

```toml
[server]
llama_base  = "http://localhost:8080"
proxy_port  = 11434
model_name  = "gemma4"

[autorun]
enabled              = true
max_iterations       = 20
tool_suppression     = true  # suppress stuck tools temporarily

[context]
eviction_keep_turns  = 8
token_budget         = 12000

[tools.context]
patch_file  = "full"      # inject full session context into sub-agent
write_file  = "summarize" # summarise session as handoff first
spawn_agent = "none"
```

---

## Sandbox

The sandbox is a Docker container that runs `shell_server.py` and executes all shell/Python commands. It:

- Runs as your host UID/GID — no root-owned files in your project
- Mounts the project at its real path — git and imports work identically to the host
- Has `uv` pre-installed globally — the model can `uv pip install` as needed
- Has `sudo apt install` available — for anything not in the venv
- Mounts your `~/.gitconfig` and `~/.git-credentials` read-only for git identity
- Ephemeral `/tmp` — test scripts are written and deleted there automatically
