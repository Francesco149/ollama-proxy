# auto_runner

## Purpose
Orchestrates the agentic auto-run loop with mid-stream interruption.
Handles three XML command types — `<run-shell>`, `<run-python>`,
`<spawn-agent>` — all on the same tag-detection path. The HTTP stream
is broken the instant any closing tag lands in the accumulator, before
the model predicts any tokens after it. Results are injected as
`role: "user"` messages, which the model cannot hallucinate.

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

async def _stream_until_event(...) -> AsyncGenerator[tuple, None]
# Yields (chunk_str, accumulated, event_type, payload).
# event_type: None | "shell" | "python" | "agent"
# payload: str (command/code) or dict (parsed spawn-agent JSON body)
# Breaks the HTTP stream immediately on any closing tag.

def _parse_spawn_agent(body: str) -> dict
# Parses the JSON body of a <spawn-agent> block. Returns {} on failure.
```

## Why tool-call protocol is NOT used in this module
After a few injected `<command-output>` / `role: "user"` turns, models
lose the tool-call framing established at the start of the conversation
and fall back to inventing ad-hoc text syntax (e.g. `/spawn_agent …`).
Using a single consistent XML-tag mechanism for all three command types
avoids this entirely. `tools` and `tool_choice` are stripped from the
body before each llama.cpp call.

## spawn-agent tag format
```
<spawn-agent>{"prompt": "...", "file_path": "/abs/path", "context": "..."}</spawn-agent>
```
`file_path` and `context` are optional. The body is parsed as JSON;
a parse failure yields an error result without breaking the loop.

## Behavior Rules
- Builds `{**openai_body, messages: messages, stream: true}` each iteration;
  removes `tools` and `tool_choice`; never mutates `openai_body`
- Forwards content tokens live to caller; skips empty `chunk_str` yields
- **Clean finish** (no tag found): yield `{done: true}`, return
- **shell / python events**:
  - Yield `⚙️ Running…`, execute, yield result
  - Track `consecutive_failures` (increment on non-zero exit, reset on success)
  - If `consecutive_failures >= max_consecutive_failures`: yield warning, done, return
- **agent events**:
  - Yield `🤖 Spawning sub-agent…`, execute `spawn_agent`, yield result
  - Does NOT affect `consecutive_failures`
- All results injected as:
  `{role: "assistant", content: accumulated}` +
  `{role: "user", content: "<command-output>\n…\n</command-output>\n\nContinue…"}`

## Must NOT
- Import from `stream_handler` — owns its own httpx streaming loop
- Import from `proxy.py`, `session_manager`, or `skill_engine`
- Pass `tools` or `tool_choice` to llama.cpp
- Hold any state between requests (fully stateless per-call)
