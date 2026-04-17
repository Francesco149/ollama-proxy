# tool_manager

## Purpose
Owns shell URL state, defines LLM tool schemas, executes tool calls (`ingest_url`, `run_shell`), and dispatches manual dot-commands (`.run`, `.diff`).

## Exports
```python
TOOLS: list[dict]                              # OpenAI-format tool schema definitions

def set_shell_url(url: str) -> None
async def execute_tool(name: str, args: dict) -> str
async def process_manual_command(messages: list) -> str | None
```

## Imports From
- `config_loader`: `get_config()` — for `server.ingest_base` (read inside functions, not at module level)

## Behavior Rules
- `SHELL_SERVER_URL` is a module-level global, mutated exclusively by `set_shell_url()`
- `execute_tool("ingest_url", ...)`: POSTs to `{ingest_base}/ingest`; handles both `urls` (list) and `url` (singular) key for model schema drift
- `execute_tool("run_shell", ...)`: POSTs `{"command": ...}` to `{SHELL_SERVER_URL}/exec`; returns formatted stdout/stderr/exit_code
- Concurrent ingest calls are limited to 2 via `_ingest_sem = asyncio.Semaphore(2)`
- `process_manual_command` dispatches `.run` (extracts quintuple-backtick blocks from last assistant message and runs them) and `.diff` (runs `git diff HEAD~1 HEAD`)
- `ingest_base` is resolved inside `execute_tool` via `get_config()` — not frozen at import time

## Must NOT
- Import from `stream_handler`, `skill_engine`, or `session_manager`
- Execute shell commands directly — all execution goes through `shell_server` via HTTP
- Hold state beyond `SHELL_SERVER_URL` and `_ingest_sem`
