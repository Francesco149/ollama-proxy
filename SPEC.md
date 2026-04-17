# stream_handler

## Purpose
Intercepts the SSE stream from llama.cpp, buffers fragmented tool-call chunks, and executes them via an injected callable — then resumes streaming the result.

## Exports
```python
async def handle_non_streaming_chat(
    openai_body: dict,
    model_name: str,
    llama_base: str,
    execute_tool_fn: Callable[[str, dict], Awaitable[str]],
) -> JSONResponse

async def generate_streaming_chat(
    openai_body: dict,
    model_name: str,
    llama_base: str,
    execute_tool_fn: Callable[[str, dict], Awaitable[str]],
) -> AsyncGenerator[str, None]
```

## Imports From
- `fastapi.responses`: `JSONResponse`
- No internal module imports — `execute_tool_fn` is injected by `proxy.py`

## Behavior Rules
- Streaming: accumulates `delta.tool_calls` chunks (name + arguments JSON string) until `finish_reason == "tool_calls"` or `"stop"` while in buffering state
- On tool-call completion: yield `⏳ Processing...` frame → call `execute_tool_fn` → yield result → break stream
- Non-streaming: iterates all `msg["tool_calls"]`, calls `execute_tool_fn` for each, joins results
- Always yields a final `{"done": true}` frame to close the Ollama stream
- Owns its own logger: `log = logging.getLogger("stream-handler")`

## Must NOT
- Import from `tool_manager` — tool execution is injected, this module has no knowledge of how tools work
- Hold any state between requests (fully stateless per-call)
- Accept `log` as a parameter — it owns its own logger
