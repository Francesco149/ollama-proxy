# proxy

## Purpose
Thin router and application entry point — wires all modules together, owns the request lifecycle, and provides Ollama API compatibility stubs.

## Exports
FastAPI `app` instance (consumed by uvicorn on port 11434).

## Imports From
- `vision_module`: `to_openai_messages`
- `tool_manager`: `TOOLS`, `execute_tool`, `set_shell_url`, `process_manual_command`
- `session_manager`: `SessionManager`
- `skill_engine`: `SkillEngine`
- `stream_handler`: `handle_non_streaming_chat`, `generate_streaming_chat`
- `config_loader`: `get_config`

## Behavior Rules
- `REAL_MODEL` is resolved once on startup via `GET {llama_base}/v1/models`
- All config constants (`LLAMA_BASE`, `INGEST_BASE`, `EMBEDDING_BASE`, `MODEL_NAME`) are read from `get_config()` on startup and stored as module-level vars
- `POST /api/chat`: intercept dot-commands first → skill injection → vision translation → dispatch to `stream_handler`, passing `execute_tool` as the injected callable
- `POST /api/embed`: translate Ollama `prompt` key → OpenAI `input`; normalize response to `{"embeddings": [[...]]}`
- `POST /register_shell`: delegate to `set_shell_url()`, return `{"status": "ok"}`
- `execute_tool` is **passed into** `stream_handler` calls as `execute_tool_fn` — never imported by `stream_handler` directly

## Must NOT
- Contain business logic beyond routing and wiring
- Perform skill scoring, session management, or tool execution inline
- Import from `stream_handler` in a way that creates a circular dependency chain through `tool_manager`
