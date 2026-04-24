# auto_runner

## Purpose
Orchestrates the agentic auto-run loop with mid-stream interruption,
clean markdown output, and shadow context support via an injected callback.
Handles three XML command types — `<run-shell>`, `<run-python>`,
`<spawn-agent>` — on the same tag-detection path.

## Exports
```python
async def run_agentic_chat(
    openai_body: dict,
    model_name: str,
    llama_base: str,
    execute_tool_fn: Callable[[str, dict], Awaitable[str]],
    max_iterations: int = 10,
    max_consecutive_failures: int = 3,
    on_clean_turn: Callable[[str, str | None], None] | None = None,
) -> AsyncGenerator[str, None]
```

## on_clean_turn callback
```python
on_clean_turn(assistant_content: str, command_output: str | None)
```
Called once per iteration:
- `assistant_content`: raw LLM prose up to (not including) the command tag
- `command_output`: plain-text execution result, or `None` on clean finish

The callback is responsible for storage. `auto_runner` is fully stateless
between requests. proxy.py provides a closure over `session_manager` and
`session_id` as the callback.

## Internal helpers
```python
_RE_FIRST_CMD        # matches first complete command block
_RE_TAG_START        # r'<(run-|spawn-)' — suppresses forwarding early
_RE_EXIT_CODE        # parses exit code from tool_manager result string

async def _stream_until_event(...)
# Yields (chunk_str, accumulated, forward, event_type, payload)
# forward=False once _RE_TAG_START matches — tag content never reaches UI
# HTTP stream broken immediately on closing tag

def _format_result(header, body) -> str   # plain markdown block
def _shell_header(cmd, result) -> str     # ✓/✗  $ preview
def _python_header(code, result) -> str   # ✓/✗  🐍 first_line
def _agent_header(prompt, file_path) -> str  # 🤖 preview — path
def _exit_code(result) -> int | None
```

## Behavior per iteration
1. Stream LLM tokens live; suppress forwarding once `_RE_TAG_START` fires
2. Break HTTP stream on first complete command closing tag
3. Execute via `execute_tool_fn`
4. Yield `_format_result(header, result)` as a single chunk (plain markdown)
5. Call `on_clean_turn(assistant_content, result)` — shadow context update
6. Append clean turns to internal `messages` list for next iteration
7. On clean finish: call `on_clean_turn(content, None)`, yield done

## Circuit-breaker
- Tracks `consecutive_failures` (non-zero exit code per `_exit_code()`)
- Resets to 0 on any clean iteration
- spawn-agent results never count toward failures
- At `>= max_consecutive_failures`: yield warning, yield done, return

## Must NOT
- Import from `proxy.py`, `session_manager`, `skill_engine`, or `stream_handler`
- Yield HTML (display is plain markdown only)
- Store any state between requests — fully stateless, callback-driven
