# auto_runner

## Purpose
Orchestrates the agentic auto-run loop: streams each LLM response live
to the caller, detects `<run-shell>` and `<run-python>` blocks in the
accumulated output, executes them via an injected callable, yields
visual progress indicators inline, injects results back into message
history as internal turns, and loops — up to `max_iterations` times or
until a circuit-breaker fires on consecutive failures.

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

## Imports From
- `tool_manager`: `RE_SHELL`, `RE_PYTHON`
- `stream_handler`: `generate_streaming_chat`

## Behavior Rules
- On each iteration: calls `generate_streaming_chat` and re-yields its
  chunks in real time; holds back the final `{"done": true}` chunk
- After each LLM call: scans accumulated `message.content` for
  `<run-shell>` and `<run-python>` blocks via `RE_SHELL`/`RE_PYTHON`
- If no blocks found → yield the held done chunk and return (clean exit)
- If blocks found:
  - Yield a progress header chunk: `\n\n---\n⚙️ Auto-running N command(s)…\n`
  - For each shell block: yield a preview chunk, await `execute_tool_fn("run_shell", …)`, yield result
  - For each python block: yield a preview chunk, await `execute_tool_fn("run_python", …)`, yield result
  - Success detection: result string contains `"exit code: \`0\`"` (exact substring match)
  - Track `consecutive_failures`: increment on any failed iteration, reset to 0 on clean one
  - If `consecutive_failures >= max_consecutive_failures`: yield warning chunk and break
- After executing commands, inject two messages for the next iteration:
  - `{role: "assistant", content: <full accumulated LLM text for this iteration>}`
  - `{role: "user", content: "<auto-run-results>\n…joined results…\n</auto-run-results>\n\nContinue your analysis…"}`
  These are internal to the proxy loop; Open WebUI never sees them as
  separate conversation bubbles.
- Always yields a final `{"done": true}` chunk before returning
- Never modifies `openai_body` in-place; uses `{**openai_body, "messages": messages}`
- Owns its own logger: `log = logging.getLogger("auto-runner")`

## Must NOT
- Import from `proxy.py`, `session_manager`, or `skill_engine`
- Hold any state between requests (fully stateless per-call)
- Duplicate regex patterns — always import from `tool_manager`
