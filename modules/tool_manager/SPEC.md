# tool_manager

## Purpose
Owns shell URL state, LLM config state, defines LLM tool schemas, executes
tool calls (`ingest_url`, `run_shell`, `run_python`, `spawn_agent`), and
dispatches manual dot-commands (`.run`, `.diff`).

## Exports
```python
RE_SHELL: re.Pattern                           # matches <run-shell>…</run-shell>
RE_PYTHON: re.Pattern                          # matches <run-python>…</run-python>

TOOLS: list[dict]                              # OpenAI-format tool schema definitions

def set_shell_url(url: str) -> None
def set_llm_config(llm_base: str, llm_model: str) -> None
async def execute_tool(name: str, args: dict) -> str
async def process_manual_command(messages: list) -> str | None
```

## Imports From
- `config_loader`: `get_config()` — for `server.ingest_base` (read inside functions, not at module level)

## State
- `SHELL_SERVER_URL`: module-level global, mutated exclusively by `set_shell_url()`
- `_LLM_BASE`, `_LLM_MODEL`: module-level globals, mutated exclusively by `set_llm_config()`

## Behavior Rules
- `set_llm_config(llm_base, llm_model)`: stores base URL and model ID for sub-agent calls
- `RE_SHELL` and `RE_PYTHON` are module-level compiled regexes, imported by `auto_runner`
- `execute_tool("ingest_url", ...)`: POSTs to `{ingest_base}/ingest`; handles both `urls` (list) and `url` (singular) key for model schema drift
- `execute_tool("run_shell", ...)`: POSTs `{"command": …}` to `{SHELL_SERVER_URL}/exec`; returns formatted stdout/stderr/exit_code
- `execute_tool("run_python", ...)`: POSTs `{"code": …}` to `{SHELL_SERVER_URL}/exec_python`; returns formatted stdout/stderr/exit_code
- `execute_tool("spawn_agent", ...)`: reads file via raw `{SHELL_SERVER_URL}/exec` (cat), makes a fresh non-streaming call to `{_LLM_BASE}/v1/chat/completions`, returns sub-agent response
- Concurrent ingest calls are limited to 2 via `_ingest_sem = asyncio.Semaphore(2)`
- `process_manual_command` dispatches `.run` (extracts `<run-shell>` and `<run-python>` XML-style blocks from last assistant message and runs them) and `.diff` (runs `git diff HEAD~1 HEAD`)
- `ingest_base` is resolved inside `execute_tool` via `get_config()` — not frozen at import time

## spawn_agent behavior
- If `_LLM_BASE` or `_LLM_MODEL` not set: returns error string
- If `file_path` provided and `SHELL_SERVER_URL` not set: returns error string
- Reads file by POSTing `{"command": "cat '<file_path>'"}` to `{SHELL_SERVER_URL}/exec` directly (bypasses `_execute_shell` to get raw stdout, not formatted markdown)
- Builds user prompt: `[context paragraph] + [<file> block] + [prompt]`, joined with double newlines
- Calls llama.cpp with `stream: false`, `max_tokens: 2048`
- Returns `"### Sub-agent analysis\n\n{response}"` on success

## Must NOT
- Import from `stream_handler`, `skill_engine`, `session_manager`, or `auto_runner`
- Execute shell commands directly — all execution goes through `shell_server` via HTTP
- Hold state beyond `SHELL_SERVER_URL`, `_LLM_BASE`, `_LLM_MODEL`, and `_ingest_sem`
