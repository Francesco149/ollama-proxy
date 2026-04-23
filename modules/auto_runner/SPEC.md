# auto_runner

## Purpose
Orchestrates the agentic auto-run loop with mid-stream interruption and
tool-call support. Streams each LLM response live to the caller, handles
two event types — XML command blocks and `spawn_agent` tool calls — then
injects results back into message history and loops with a fresh LLM call.

## Exports
```python
async def run_agentic_chat(
    openai_body: dict,
    model_name: str,
    llama_base: str,
    execute_tool_fn: Callable[[str, dict], Awaitable[str]],
    max_iterations: int = 10,
    max_consecutive_failures: int = 3,
) -> AsyncGenerator[str, None]
```

## Internal helpers

```python
_AUTORUN_TOOLS: list[dict]
# Filtered subset of TOOLS containing only "spawn_agent".
# run_shell/run_python are intentionally excluded — they arrive via XML
# tags; offering them as tool-call schemas alongside XML causes confusion.

async def _stream_until_event(
    openai_body: dict,
    llama_base: str,
    model_name: str,
) -> AsyncGenerator[tuple[str, str, str | None, any], None]
```

Yields `(chunk_str, accumulated, event_type, payload)`:
- `event_type=None`: normal content token or clean finish
- `event_type="shell"`: complete `<run-shell>…</run-shell>` detected;
  payload is the command string; HTTP stream broken immediately
- `event_type="python"`: complete `<run-python>…</run-python>` detected;
  payload is the code string; HTTP stream broken immediately
- `event_type="tool_call"`: `finish_reason=="tool_calls"` received;
  payload is `{"name": str, "args": dict}`; `chunk_str` is `""`

## Anti-hallucination design

**Mid-stream interruption (XML commands):** The moment a closing
`</run-shell>` or `</run-python>` tag appears in the accumulator, the
httpx stream is broken before the model generates any further tokens.
This prevents pattern-completion of fake output.

**User-role injection (XML results):** Results injected as `role: "user"`.
The model has no learned pattern for predicting the user turn.

**Tool-call protocol (spawn_agent):** Tool calls run to
`finish_reason=="tool_calls"` — the model emits no text content during
accumulation so there is nothing to hallucinate. Results injected using
the correct `role: "tool"` multi-turn format, which is equally opaque
to prediction.

## Behavior Rules

### Per iteration
- Builds body as `{**openai_body, messages: messages, stream: true,
  tools: _AUTORUN_TOOLS, tool_choice: "auto"}`; never mutates `openai_body`
- Forwards content tokens live to caller; skips empty `chunk_str` yields
- On `event_type=None` (clean finish) → yield `{done: true}` and return

### XML command events
- Yields `⚙️ Running…` progress chunk
- Executes via `execute_tool_fn("run_shell"|"run_python", {key: payload})`
- Tracks `consecutive_failures`: increments on non-zero exit
  (detected by absence of `"exit code: \`0\`"` in result), resets to 0 on success
- If `consecutive_failures >= max_consecutive_failures` → yield warning, yield done, return
- Injects: `{role: "assistant", content: accumulated}` +
  `{role: "user", content: "<command-output>\n…\n</command-output>\n\nContinue…"}`

### Tool call events (spawn_agent)
- Yields `🤖 Spawning sub-agent…` progress chunk
- Executes via `execute_tool_fn(name, args)`; does NOT affect failure counter
- Injects: `{role: "assistant", content: accumulated, tool_calls: [{id, type, function}]}` +
  `{role: "tool", tool_call_id: "call_0", content: result}`
- `tool_call_id` is hardcoded to `"call_0"` (single tool call per turn)

## Must NOT
- Import from `stream_handler` — owns its own streaming loop
- Import from `proxy.py`, `session_manager`, or `skill_engine`
- Offer `run_shell`/`run_python` as tool schemas — XML tags only for those
- Hold any state between requests (fully stateless per-call)
