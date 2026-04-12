# ollama-proxy — SPEC

Fake Ollama server that proxies to llama.cpp, injects skills, and executes tools.
Source: `/opt/ai-lab/ollama-proxy/proxy.py`
Venv: `/opt/ai-lab/ollama-proxy/venv`
Systemd: `ollama-proxy.service`
Port: `11434` (standard Ollama port — Open-WebUI connects here)

---

## Purpose

Open-WebUI expects Ollama protocol. llama.cpp speaks OpenAI protocol.
This proxy bridges them and adds:
1. **Vision** — translates Ollama image format to OpenAI multipart
2. **Skill injection** — injects relevant skill markdown into system prompt
3. **Session persistence** — active skills persist for entire conversation
4. **Tool execution** — intercepts tool calls and executes them locally

---

## Ollama API Stubs

These endpoints are called by Open-WebUI on startup and model selection:

| method | path | purpose |
|--------|------|---------|
| GET | `/` | version check |
| GET | `/api/version` | version check |
| GET | `/api/tags` | model list |
| POST | `/api/show` | model capabilities (must include `"clip"` in families for vision) |
| GET | `/api/ps` | currently loaded models (must return non-empty for vision to work) |
| POST | `/api/chat` | main chat endpoint |

### Critical: vision capability detection
Open-WebUI checks `details.families` for `"clip"` to enable vision UI.
`/api/tags`, `/api/show`, `/api/ps` must all return:
```json
{ "details": { "families": ["gemma", "clip"] } }
```
If `"clip"` is missing, Open-WebUI silently drops image attachments before the API call.

---

## Message Format Translation

### Ollama → OpenAI (inlet)
Ollama sends images as a separate `images: [base64...]` array or per-message.
OpenAI expects images inline in message content as `image_url` parts.

```python
# Ollama format
{"role": "user", "content": "what's in this?", "images": ["<base64>"]}

# OpenAI format
{"role": "user", "content": [
    {"type": "text", "text": "what's in this?"},
    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<base64>"}}
]}
```

Images without `data:` prefix get `data:image/jpeg;base64,` prepended.
Top-level `body.images` array is attached to the last user message.

### OpenAI streaming → Ollama streaming
llama.cpp streams SSE: `data: {"choices":[{"delta":{"content":"..."}}]}`
Ollama streams NDJSON: `{"model":"...","message":{"role":"assistant","content":"..."},"done":false}`
Final chunk: `{"done": true}`

---

## Skill Router

### Skill files
Location: `/opt/ai-lab/skills/*.md`
Format:
```markdown
---
triggers: word1 word2 bigram phrase ...
---
# skill-name
...content...
```

### Scoring
```python
words    = message.lower().split()
msg_set  = set(words) | {f"{words[i]} {words[i+1]}" for i in range(len(words)-1)}
overlap  = msg_set & trigger_words
score    = len(overlap) / max(len(trigger_words), 1)
```
Trigger-coverage ratio — long messages not penalized.
Thresholds: `MIN_SCORE = 0.15`, `MAX_SKILLS = 2`

### Session persistence
- Session ID = sha256[:16] of first user message content
- `active_skills: dict[session_id, set[skill_name]]` — module-level, in memory
- Skills accumulate over conversation — once active, always injected
- Cleared on proxy restart
- Skill files reloaded from disk on every request — live editing, no restart needed

### Injection
Active skill content prepended to system prompt as:
```
# Active workflow skills for this conversation:

## Active Skill: {name}
{full skill file content}

---
These skills remain active for the entire conversation.
```
If no system message exists, one is inserted at position 0.

---

## Tool Execution

### Defined tools
```python
TOOLS = [{
    "type": "function",
    "function": {
        "name": "ingest_url",
        "description": "Save one or more URLs to the knowledge base...",
        "parameters": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}},
                "note": {"type": "string"}
            },
            "required": ["urls"]
        }
    }
}]
```
Passed to llama.cpp as `tools` + `tool_choice: "auto"` on every request.

### Streaming tool call detection
llama.cpp streams tool calls as delta chunks with `delta.tool_calls`.
Proxy buffers `function.name` and `function.arguments` across chunks.
On `finish_reason == "tool_calls"` (or `"stop"` while in_tool_call):
1. Parse buffered JSON arguments
2. Execute tool
3. Stream result back as assistant content

### Tool: ingest_url
```python
args = {"urls": [...], "note": "..."}
```
- Handles `"url"` (singular, string) as fallback if model ignores schema
- Runs all URLs concurrently via `asyncio.gather`
- Limited by `_ingest_sem = asyncio.Semaphore(2)`
- Each URL: `POST http://localhost:8083/ingest {"url": ..., "note": ...}`
- Returns formatted result string to stream back to user

---

## Request Flow

```
Open-WebUI POST /api/chat
    → inject_skills(messages)        # score + inject relevant skills
    → to_openai_messages(messages)   # translate format + embed images
    → POST llama.cpp /v1/chat/completions  (stream=True)
        ├─ normal text delta → forward as Ollama NDJSON
        └─ tool_calls delta  → buffer args
                              → execute_tool()
                              → stream result as assistant content
    → final done:true chunk
```

---

## Config Constants

```python
LLAMA_BASE    = "http://localhost:8080"
INGEST_BASE   = "http://localhost:8083"
MODEL_NAME    = "gemma4"              # reported to Open-WebUI
SKILLS_DIR    = "/opt/ai-lab/skills"
MAX_SKILLS    = 2
MIN_SCORE     = 0.15
```
`REAL_MODEL` is auto-detected from `GET /v1/models` on startup.

---

## Known Limitations

- Session memory cleared on restart (in-memory dict)
- Tool call streaming: result sent as plain text, not as proper Ollama tool_result message
  (Open-WebUI doesn't render tool_result blocks anyway)
- Single model only — multi-model routing not implemented
- `_ingest_sem` must be module-level asyncio.Semaphore — if defined inside a function
  a new semaphore is created per call and never blocks
