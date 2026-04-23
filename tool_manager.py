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
            "name": "spawn_agent",
            "description": (
                "Spawn a focused sub-agent with a fresh context window to analyze a file "
                "or answer a specific question. Use this to inspect a module's interface, "
                "summarize findings, or assess how to approach a change — without loading "
                "the full file into the main context. Returns a concise analysis with "
                "function names, signatures, and line numbers as relevant."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Specific question or task for the sub-agent. "
                            "Ask for function signatures, line numbers, interfaces, "
                            "or targeted patterns relevant to your current task. "
                            "The more precise, the more useful the response."
                        ),
                    },
                    "file_path": {
                        "type": "string",
                        "description": (
                            "Absolute path to a file to load into the sub-agent's context. "
                            "The full file contents will be visible to the sub-agent only."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": (
                            "Current task context so the sub-agent gives relevant answers. "
                            "Example: 'We are adding an auto-run feature to tool_manager.'"
                        ),
                    },
                },
                "required": ["prompt"],
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

async def _execute_spawn_agent(args: dict) -> str:
    prompt = args.get("prompt", "")
    file_path = args.get("file_path")
    context = args.get("context", "")

    if not _LLM_BASE or not _LLM_MODEL:
        return "❌ Sub-agent not configured — call set_llm_config first"

    file_content = ""
    if file_path:
        if not SHELL_SERVER_URL:
            return "❌ Shell server not registered — cannot read file"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{SHELL_SERVER_URL}/exec",
                    json={"command": f"cat '{file_path}'"},
                )
                r = resp.json()
            if r.get("exit_code") != 0:
                return f"❌ Failed to read {file_path}: {r.get('stderr', 'unknown error')}"
            file_content = r.get("stdout", "")
            log.info(f"spawn_agent: read {len(file_content)} chars from {file_path}")
        except Exception as e:
            log.error(f"spawn_agent file read error: {e}", exc_info=True)
            return f"❌ Error reading file: {e}"

    parts: list[str] = []
    if context:
        parts.append(f"Task context: {context}")
    if file_content:
        parts.append(f'<file path="{file_path}">\n{file_content}\n</file>')
    parts.append(prompt)
    user_content = "\n\n".join(parts)

    log.info(f"spawn_agent: sub-agent call, prompt_len={len(user_content)}")
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
        log.info("spawn_agent: sub-agent response received")
        return f"### Sub-agent analysis\n\n{result}"
    except Exception as e:
        log.error(f"spawn_agent LLM call error: {e}", exc_info=True)
        return f"❌ Sub-agent call failed: {e}"


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
