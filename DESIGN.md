# ollama-proxy

Proxy between Open-WebUI and llama.cpp that adds: per-session skill injection, tool execution (shell + python + knowledge base ingest + sub-agent), Ollama API compatibility, and an optional agentic auto-run loop.

---

## Data Flow

```
Open-WebUI
    │
    ▼
proxy.py ──dot-command?──► tool_manager.process_manual_command
    │
    ├─► skill_engine.process_message        (injects active skills into system prompt)
    │       └─► session_manager             (tracks active skills per session)
    │
    ├─► vision_module.to_openai_messages    (translates Ollama image payloads → OpenAI)
    │
    └─► [autorun enabled?]
            ├─YES─► auto_runner.run_agentic_chat   (loop: stream → detect → execute → inject → repeat)
            │           ├─► stream_handler          (SSE interception + tool-call buffering)
            │           └─► execute_tool [injected] (passed in by proxy)
            │                   └─► shell_server    (HTTP, port 8000)
            │
            └─NO──► stream_handler                  (SSE interception + tool-call buffering)
                        └─► execute_tool [injected]
                                └─► shell_server    (HTTP, port 8000)
```

---

## Module Index

| Module | Role |
| --- | --- |
| `proxy.py` | Thin router, Ollama API stubs, module wiring |
| `skill_engine.py` | Skill scoring + system prompt injection |
| `session_manager.py` | Session identity and active skill state |
| `stream_handler.py` | SSE buffering, tool-call interception |
| `auto_runner.py` | Agentic loop: auto-executes command blocks, injects results, loops |
| `tool_manager.py` | Tool schemas, execution, shell URL state, LLM config state, dot-commands |
| `vision_module.py` | Ollama → OpenAI image format translation |
| `config_loader.py` | TOML config singleton |
| `shell_server.py` | Standalone shell and Python execution agent |

Spec for each module: `modules/<module>/SPEC.md`

---

## Ports

| Port | Service |
| --- | --- |
| 11434 | `proxy.py` (Ollama-compatible API) |
| 8000 | `shell_server.py` |
| 8080 | llama.cpp |
| 8083 | Ingest service |

---

## Auto-run loop

When `[autorun] enabled = true` in `config.toml`, each chat turn is routed
through `auto_runner.run_agentic_chat` instead of `stream_handler` directly.

The loop:
1. Streams the LLM response live to Open WebUI
2. Accumulates the full content; holds back the final `done` chunk
3. Scans for `<run-shell>` / `<run-python>` blocks
4. If none → releases `done`, returns
5. If found → executes them, streams progress + results inline to Open WebUI
6. Injects `{assistant: full_response}` + `{user: <auto-run-results>…}` into
   the internal message list (never visible as separate bubbles in Open WebUI)
7. Loops from step 1 with the updated messages

Circuit-breaker: stops after `max_consecutive_failures` iterations where any
command exits non-zero. Configurable in `[autorun]`.

Manual `.run` still works when autorun is disabled or to manually replay commands.

---

## Sub-agent tool

The `spawn_agent` LLM tool lets the orchestrator delegate focused analysis to
a fresh LLM call with a full file loaded — without bloating the main context.

Flow: LLM emits a `spawn_agent` tool call → `tool_manager._execute_spawn_agent`
reads the file via `shell_server /exec` (raw cat), builds a focused prompt,
calls llama.cpp non-streaming, returns the sub-agent's concise response.

Use cases: interface assessment, refactor planning, summarizing findings from
a file the orchestrator hasn't read directly.
