# ollama-proxy

Proxy between Open-WebUI and llama.cpp that adds: per-session skill injection, tool execution (shell + knowledge base ingest), and Ollama API compatibility.

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
    └─► stream_handler                      (SSE interception + tool-call buffering)
            └─► execute_tool [injected]     (passed in by proxy, not imported)
                    └─► shell_server        (HTTP, port 8000)
```

---

## Module Index

| Module | Role |
| --- | --- |
| `proxy.py` | Thin router, Ollama API stubs, module wiring |
| `skill_engine.py` | Skill scoring + system prompt injection |
| `session_manager.py` | Session identity and active skill state |
| `stream_handler.py` | SSE buffering, tool-call interception |
| `tool_manager.py` | Tool schemas, execution, shell URL state, dot-commands |
| `vision_module.py` | Ollama → OpenAI image format translation |
| `config_loader.py` | TOML config singleton |
| `shell_server.py` | Standalone shell execution agent |

Spec for each module: `modules/<module>/SPEC.md`

---

## Ports

| Port | Service |
| --- | --- |
| 11434 | `proxy.py` (Ollama-compatible API) |
| 8000 | `shell_server.py` |
| 8080 | llama.cpp |
| 8083 | Ingest service |
