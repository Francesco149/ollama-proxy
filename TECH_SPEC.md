# TECH_SPEC.md: System Structural Specification

## 1. Module Responsibility Map

| Module | Responsibility |
| :--- | :--- |
| `proxy.py` | **Thin Router:** Entry point. Handles Ollama API routing, version stubs, embedding protocol translation, and the `/register_shell` endpoint. |
| `skill_engine.py` | **Intent Middleware:** Scans messages for skill triggers, manages scoring, and performs system prompt injection. |
| `vision_module.py` | **Vision Translator:** Converts Ollama-style image payloads into OpenAI-compatible multipart/base64 requests. |
| `tool_manager.py` | **Execution Authority:** Manages the `SHELL_SERVER_URL`, executes `ingest_url` (knowledge base) and `run_shell` (remote execution) commands, and acts as a command dispatcher for shorthands (e.g., `.diff`). |
| `session_manager.py` | **State Authority:** Maintains deterministic `session_id` via SHA-256 and manages the set of active skills per session. |
| `stream_handler.py` | **Stream Interceptor:** Manages SSE buffering, detects `tool_calls` in the stream, and triggers execution before yielding results. |
| `shell_server.py` | **Execution Agent:** Discovers local IP, registers itself via UDP/HTTP handshake, and executes arbitrary shell commands. |

---

## 2. The Request/Response Pipeline

Trace of a single user message from Open-WebUI:

1.  **Ingress:** `proxy.py` receives `POST /api/chat`.
2.  **Command Interception:** `proxy.py` checks if the message is a manual command (starts with `.`). If so, it bypasses the LLM and calls `tool_manager.process_manual_command`.
3.  **Skill Injection:** `skill_engine.process_message` is called. It calculates scores, updates `session_manager`, and prepends the "Active Skill" system prompt to the message history.
4.  **Translation:** `vision_module.to_openai_messages` transforms the message history (including images) into OpenAI format.
5.  **LLM Dispatch:** `proxy.py` forwards the translated payload to the downstream LLM (e.g., `llama.cpp`).
6.  **Stream Interception:** `stream_handler.generate_streaming_chat` intercepts the SSE stream.
    -   If `delta.content` is found: Yield to client.
    -   If `delta.tool_calls` is found: Enter **Buffering State**.
7.  **Tool Execution:** Upon `finish_reason: tool_calls`, the buffer is parsed and sent to `tool_manager.execute_tool`.
8.  **Result Injection:** The tool output is yielded back to the client as a new assistant message.
9.  **Egress:** The final stream/response is sent back to Open-WebUI.

---

## 3. Protocol Mapping Table

| Ollama API Endpoint | Proxy Logic | OpenAI API Equivalent |
| :--- | :--- | :--- |
| `POST /api/chat` | `proxy.py:chat` $\to$ `skill_engine` $\to$ `stream_handler` | `POST /v1/chat/completions` |
| `POST /api/embed` | `proxy.py:embeddings` (Prompt $\to$ Input) | `POST /v1/embeddings` |
| `GET /api/tags` | `proxy.py:tags` (Stubbed) | N/A |
| `POST /register_shell` | `proxy.py:register_shell` $\to$ `tool_manager` | N/A |

---

## 4. System Environment

### Configuration File
- **`config.toml`**: Contains the following service settings:
    - `llama_base`: URL of the LLM engine (e.g., `http://localhost:8080`).
    - `ingest_base`: URL of the knowledge base ingestion service.
    - `embedding_base`: URL of the embedding service.
    - `model_name`: The name of the model to use.
- The configuration is loaded from `/opt/ai-lab/ollama-proxy/config.toml` by default, or from the path specified in the `OLLAMA_PROXY_CONFIG` environment variable.

### Required Environment Variables
- `PROXY_URL`: Required specifically for the standalone `shell_server` process (used for registration).

### Port Mapping
- `11434`: `proxy.py` (Ollama API compatibility).
- `8000`: `shell_server.py` (Command execution).
- `8083`: Ingestion Service.
- `8080`: LLM Engine.

---

## 5. Refactor Status

- [DONE] `proxy.py` (Thin Router)
- [DONE] `skill_engine.py` (Intent Middleware)
- [DONE] `vision_module.py` (Vision Translator)
- [DONE] `tool_manager.py` (Execution Authority)
- [DONE] `session_manager.py` (State Authority)
- [DONE] `stream_handler.py` (Stream Interceptor)
- [DONE] `shell_server.py` (Execution Agent)
