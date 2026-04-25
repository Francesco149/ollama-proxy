import httpx
import asyncio
import logging
import re
from config_loader import get_config

log = logging.getLogger("tool-manager")

SHELL_SERVER_URL = None
_LLM_BASE: str | None = None
_LLM_MODEL: str | None = None

_ingest_sem = asyncio.Semaphore(2)

RE_SHELL = re.compile(r'<run-shell>(.*?)</run-shell>', re.DOTALL)
RE_PYTHON = re.compile(r'<run-python>(.*?)</run-python>', re.DOTALL)

# ── shell URL state ───────────────────────────────────────────────────────────

def set_shell_url(url: str):
    global SHELL_SERVER_URL
    SHELL_SERVER_URL = url
    log.info(f"SHELL_SERVER_URL set to {url}")


def set_llm_config(llm_base: str, llm_model: str) -> None:
    global _LLM_BASE, _LLM_MODEL
    _LLM_BASE = llm_base
    _LLM_MODEL = llm_model
    log.info(f"LLM config set: base={llm_base} model={llm_model}")

# ── tool schemas ──────────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Execute a shell command on the project host. "
                "Use for: git commands, reading file trees (git ls-files), "
                "running scripts, checking diffs. "
                "Never use ls or find — always git ls-files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute. May be multi-line for scripts.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute Python code on the project host. "
                "Use for: simple refactors (fixing imports, bulk renames), "
                "scaffolding new files/dirs, data transformations. "
                "Never implement business logic here — use write_file or aider."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute.",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": (
                "Spawn a focused sub-agent with a fresh context window to analyze one or "
                "more files. Use to inspect interfaces, plan refactors, validate SPECs, "
                "or reason across module boundaries — without loading files into the main "
                "context. The sub-agent sees the full file(s); you receive only its answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Precise question for the sub-agent. Ask for function signatures, "
                            "line numbers, interface compatibility, or SPEC gaps. "
                            "End with: Do not include any preamble, be as brief as possible."
                        ),
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to a single file to load (convenience alias for files[0]).",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Absolute paths of multiple files to load for cross-module reasoning. "
                            "Use when checking interface compatibility or tracing calls across modules."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": "Current task context so the sub-agent gives relevant answers.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Generate a complete file via a sub-agent and write it to disk. "
                "File content never enters the orchestrator context — you receive only "
                "a brief status line. Use for new modules or full rewrites. "
                "Always verify with spawn_agent immediately after."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path where the file should be written.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Fully self-contained generation prompt. Include: all function "
                            "signatures, behavior rules, imports, edge cases, logging setup. "
                            "The agent has no other context."
                        ),
                    },
                },
                "required": ["path", "prompt"],
            },
        },
    },
]

# ── tool execution ────────────────────────────────────────────────────────────

async def execute_tool(name: str, args: dict) -> str:
    log.info(f"execute name={name} args={args}")

    if name == "ingest_url":
        return await _execute_ingest(args)

    if name == "run_shell":
        return await _execute_shell(args)

    if name == "run_python":
        return await _execute_python(args)

    if name == "spawn_agent":
        return await _execute_spawn_agent(args)

    if name == "write_file":
        return await _execute_write_file(args)

    return f"Unknown tool: {name}"


async def _execute_ingest(args: dict) -> str:
    ingest_base = get_config().get("server", {}).get("ingest_base", "http://localhost:8083")

    urls = args.get("urls", [])
    if isinstance(urls, str):
        urls = [urls]
    # Handle model schema drift: sometimes sends singular "url" key
    if not urls and args.get("url"):
        urls = [args["url"]]

    if not urls:
        return "No URLs found in tool call args"

    note = args.get("note", "")
    log.info(f"urls to ingest: {urls}")

    async def ingest_one(url: str) -> str:
        try:
            async with _ingest_sem:
                async with httpx.AsyncClient(timeout=86400) as client:
                    log.info(f"ingesting {url}")
                    resp = await client.post(f"{ingest_base}/ingest", json={"url": url, "note": note})
                    r = resp.json()
            if r.get("status") == "ok":
                return f"✓ {r.get('title') or r.get('domain') or url}"
            elif r.get("status") == "todo":
                return f"⚠ unsupported URL saved as todo: {url}"
            else:
                return f"✗ failed: {url} — {r.get('error')}"
        except Exception as e:
            log.error(f"ingest_one exception for {url}: {e}", exc_info=True)
            return f"✗ exception: {url} — {e}"

    try:
        results = await asyncio.gather(*[ingest_one(url) for url in urls])
        return "Saved to knowledge base:\n" + "\n".join(results)
    except Exception as e:
        log.error(f"gather exception: {e}", exc_info=True)
        return f"Error during ingestion: {e}"


async def _execute_shell(args: dict) -> str:
    command = args.get("command")
    if not command:
        return "No command provided for run_shell"
    if not SHELL_SERVER_URL:
        return "Shell server URL is not configured"
    try:
        log.info(f"running shell command: {command}")
        async with httpx.AsyncClient(timeout=600) as client:
            if "\n" in command:
                resp = await client.post(f"{SHELL_SERVER_URL}/exec_shell", json={"script": command})
            else:
                resp = await client.post(f"{SHELL_SERVER_URL}/exec", json={"command": command})
            r = resp.json()
        stdout = r.get("stdout", "")
        stderr = r.get("stderr", "")
        exit_code = r.get("exit_code", 0)
        result = f"Command executed (exit code: `{exit_code}`)\n"
        if stdout:
            result += f"### STDOUT:\n`````\n{stdout}\n`````\n"
        if stderr:
            result += f"### STDERR:\n`````\n{stderr}\n`````\n"
        return result
    except Exception as e:
        log.error(f"run_shell exception: {e}", exc_info=True)
        return f"Error executing shell command: {e}"


async def _execute_python(args: dict) -> str:
    code = args.get("code")
    if not code:
        return "No code provided for run_python"
    if not SHELL_SERVER_URL:
        return "Shell server URL is not configured"
    try:
        log.info(f"running python code: {code[:50]}...")
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(f"{SHELL_SERVER_URL}/exec_python", json={"code": code})
            r = resp.json()
        stdout = r.get("stdout", "")
        stderr = r.get("stderr", "")
        exit_code = r.get("exit_code", 0)
        result = f"Python executed (exit code: `{exit_code}`)\n"
        if stdout:
            result += f"### STDOUT:\n`````\n{stdout}\n`````\n"
        if stderr:
            result += f"### STDERR:\n`````\n{stderr}\n`````\n"
        return result
    except Exception as e:
        log.error(f"run_python exception: {e}", exc_info=True)
        return f"Error executing python code: {e}"

async def _read_file_via_shell(path: str) -> tuple[str, str | None]:
    """Read a file via shell_server. Returns (content, error_or_None)."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SHELL_SERVER_URL}/exec",
                json={"command": f"cat '{path}'"},
            )
            r = resp.json()
        if r.get("exit_code") != 0:
            return "", f"❌ Failed to read {path}: {r.get('stderr', 'unknown error')}"
        return r.get("stdout", ""), None
    except Exception as e:
        return "", f"❌ Error reading {path}: {e}"


async def _execute_spawn_agent(args: dict) -> str:
    """
    Spawn a sub-agent with one or more files loaded into its context.

    Accepts either:
      file_path: str          — single file (backward compatible)
      files: list[str]        — multiple files, each wrapped in <file path="..."> tags

    The sub-agent sees all requested files and responds to `prompt`.
    `context` is prepended as task context.
    """
    prompt    = args.get("prompt", "")
    file_path = args.get("file_path")
    files     = args.get("files", [])
    context   = args.get("context", "")

    if not _LLM_BASE or not _LLM_MODEL:
        return "❌ Sub-agent not configured — call set_llm_config first"
    if not SHELL_SERVER_URL:
        return "❌ Shell server not registered — cannot read files"

    # Normalise to a single list
    all_paths = list(files)
    if file_path and file_path not in all_paths:
        all_paths.insert(0, file_path)

    parts: list[str] = []
    if context:
        parts.append(f"Task context: {context}")

    for path in all_paths:
        content, err = await _read_file_via_shell(path)
        if err:
            parts.append(err)
        else:
            parts.append(f'<file path="{path}">\n{content}\n</file>')
            log.info(f"spawn_agent: loaded {len(content)} chars from {path}")

    parts.append(prompt)
    user_content = "\n\n".join(parts)

    log.info(f"spawn_agent: calling sub-agent, {len(all_paths)} file(s), prompt_len={len(user_content)}")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{_LLM_BASE}/v1/chat/completions",
                json={
                    "model": _LLM_MODEL,
                    "messages": [{"role": "user", "content": user_content}],
                    "stream": False,
                    "max_tokens": 2048,
                },
            )
            data = resp.json()
        result = data["choices"][0]["message"]["content"]
        log.info("spawn_agent: response received")
        return result
    except Exception as e:
        log.error(f"spawn_agent LLM call error: {e}", exc_info=True)
        return f"❌ Sub-agent call failed: {e}"


async def _execute_write_file(args: dict) -> str:
    """
    Generate a file via a fresh sub-agent call, write it to disk, and
    return a brief status string. The LLM context only sees the status —
    never the file content — keeping the orchestrator context lean.
    """
    path   = args.get("path", "")
    prompt = args.get("prompt", "")

    if not path or not prompt:
        return "❌ write-file: 'path' and 'prompt' are required"

    if not _LLM_BASE or not _LLM_MODEL:
        return "❌ write-file: LLM not configured (call set_llm_config first)"
    if not SHELL_SERVER_URL:
        return "❌ write-file: shell server not registered"

    # ── Generate file content via sub-agent ───────────────────────────────────
    system_instruction = (
        "You are a code generation assistant. "
        "Respond with ONLY the complete file contents — no preamble, "
        "no explanation, no markdown fences. Raw code only."
    )
    log.info(f"write_file: generating {path!r} via sub-agent")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{_LLM_BASE}/v1/chat/completions",
                json={
                    "model": _LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system_instruction},
                        {"role": "user",   "content": prompt},
                    ],
                    "stream": False,
                    "max_tokens": 8192,
                },
            )
            data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        log.info(f"write_file: sub-agent returned {len(content)} chars")
    except Exception as e:
        log.error(f"write_file sub-agent error: {e}", exc_info=True)
        return f"❌ write-file: sub-agent call failed: {e}"

    # Strip any accidental markdown fences the model may have added
    fence_m = re.search(r'^```[^\n]*\n(.*?)^```\s*$', content, re.DOTALL | re.MULTILINE)
    if fence_m:
        content = fence_m.group(1)
        log.debug("write_file: stripped markdown fences from output")

    # ── Write to disk via shell_server ────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SHELL_SERVER_URL}/write_file",
                json={"path": path, "content": content},
            )
            r = resp.json()
        if not r.get("ok"):
            return f"❌ write-file: could not write {path}: {r.get('error', 'unknown')}"
        n_lines = r.get("lines", content.count("\n") + 1)
        log.info(f"write_file: wrote {path!r} ({n_lines} lines)")
    except Exception as e:
        log.error(f"write_file disk write error: {e}", exc_info=True)
        return f"❌ write-file: disk write failed: {e}"

    # Return a terse status — content never enters the orchestrator context
    preview = "\n".join(content.splitlines()[:8])
    return (
        f"File written: `{path}` ({n_lines} lines). "
        f"Verify implementation with spawn-agent.\n\n"
        f"```\n{preview}\n{'...' if n_lines > 8 else ''}\n```"
    )


# ── dot-command dispatcher ────────────────────────────────────────────────────

async def _handle_run(messages: list) -> str:
    assistant_msg = None
    for msg in reversed(messages[:-1]):
        if msg.get("role") == "assistant":
            assistant_msg = msg
            break
    if not assistant_msg:
        return "No assistant message found to extract commands from."
    
    content = assistant_msg.get("content", "")
    shell_matches = RE_SHELL.findall(content)
    python_matches = RE_PYTHON.findall(content)
    
    if not shell_matches and not python_matches:
        return "No shell or python commands found in the last assistant message."
    
    results = []
    for cmd in shell_matches:
        res = await execute_tool("run_shell", {"command": cmd.strip()})
        results.append(res)
    for code in python_matches:
        res = await execute_tool("run_python", {"code": code.strip()})
        results.append(res)
    return "\n\n---\n\n".join(results)


async def _handle_py(messages: list) -> str:
    assistant_msg = None
    for msg in reversed(messages[:-1]):
        if msg.get("role") == "assistant":
            assistant_msg = msg
            break
    if not assistant_msg:
        return "No assistant message found to extract python code from."
    matches = RE_PYTHON.findall(assistant_msg.get("content", ""))
    if not matches:
        return "No python code found in the last assistant message."
    results = []
    for code in matches:
        res = await execute_tool("run_python", {"code": code.strip()})
        results.append(res)
    return "\n\n---\n\n".join(results)


async def _handle_diff(_messages: list) -> str:
    return await execute_tool("run_shell", {"command": "git diff HEAD~1 HEAD"})


async def process_manual_command(messages: list) -> str | None:
    if not messages:
        return None
    last_msg = messages[-1]
    if last_msg.get("role") != "user":
        return None
    content = last_msg.get("content", "").strip()

    handlers = {
        ".run": _handle_run,
        ".diff": _handle_diff,
        ".fetch": None,
    }

    if content in handlers:
        if content == ".fetch":
            return "No URL provided for .fetch"
        return await handlers[content](messages)

    for prefix, handler in handlers.items():
        if content.startswith(prefix + " "):
            if prefix == ".run":
                cmd = content[len(prefix) + 1:].strip()
                if not cmd:
                    return f"No command provided after {prefix}"
                return await execute_tool("run_shell", {"command": cmd})
            elif prefix == ".fetch":
                url = content[len(prefix) + 1:].strip()
                if not url:
                    return f"No URL provided after {prefix}"
                return await execute_tool("ingest_url", {"urls": [url]})
            else:
                return await handler(messages)

    if content.startswith("."):
        return f"❌ Error: Unknown command '{content.split()[0]}'"

    return None
