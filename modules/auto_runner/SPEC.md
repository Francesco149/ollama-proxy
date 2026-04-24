# auto_runner

## Purpose
Orchestrates the agentic auto-run loop with mid-stream interruption and
collapsible result display. Handles three XML command types —
`<run-shell>`, `<run-python>`, `<spawn-agent>` — all on the same
tag-detection path. The HTTP stream is broken the instant any closing
tag lands in the accumulator. Results are rendered as collapsed
`<details>` blocks for the user and injected as plain-text `role=user`
messages into the LLM context.

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
_RE_FIRST_CMD: re.Pattern
# Matches the first complete block across all three tag types.

_RE_EXIT_CODE: re.Pattern
# Extracts numeric exit code from tool_manager's formatted result string.

async def _stream_until_event(...) -> AsyncGenerator[tuple, None]
# Yields (chunk_str, accumulated, event_type, payload).
# event_type: None | "shell" | "python" | "agent"
# Breaks the HTTP stream immediately on any closing tag.

def _collapsible(summary: str, body: str) -> str
# Wraps body in a <details><summary>…</summary>…</details> block.
# Yielded as a single chunk so HTML is always well-formed.

def _shell_summary(cmd: str, result: str) -> str
def _python_summary(code: str, result: str) -> str
def _agent_summary(prompt: str, file_path: str | None) -> str
# Build the <summary> line for each event type.
# Shell/python include exit-code status icon (✓ / ✗ exit N).
# Agent shows prompt preview and file path.

def _exit_code(result: str) -> int | None
# Parses exit code from result string. Returns None if not found.
```

## Display vs context separation
- **Display (Open WebUI):** each result yielded as one `_collapsible()` chunk
  with a descriptive summary line and the raw result as body
- **LLM context:** plain-text result injected as `role=user` with
  `<command-output>…</command-output>` wrapper — no HTML ever enters
  the model's reasoning context

## Behavior Rules
- Builds `{**openai_body, messages: messages, stream: true}` each iteration;
  removes `tools` and `tool_choice`; never mutates `openai_body`
- **Clean finish** (no tag): yield `{done: true}`, return
- **shell / python events:**
  - Execute via `execute_tool_fn`
  - Parse exit code via `_exit_code()`; track `consecutive_failures`
    (increment on non-zero, reset to 0 on success)
  - Yield single `_collapsible()` chunk
  - Circuit-breaker: if `consecutive_failures >= max_consecutive_failures`
    → yield warning, yield done, return
- **agent events:**
  - Execute `spawn_agent` via `execute_tool_fn`
  - Yield single `_collapsible()` chunk
  - Does NOT affect `consecutive_failures`
- All results injected as:
  `{role: "assistant", content: accumulated}` +
  `{role: "user", content: "<command-output>\n…\n</command-output>\n\nContinue…"}`

## Why tool-call protocol is not used
After several `role=user` injections, models lose the tool-call framing
and invent ad-hoc text syntax. All three command types use XML tags for
consistency. `tools` and `tool_choice` are stripped from each request.

## Must NOT
- Import from `stream_handler` — owns its own httpx streaming loop
- Import from `proxy.py`, `session_manager`, or `skill_engine`
- Yield HTML inside the `role=user` injected context
- Hold any state between requests (fully stateless per-call)
