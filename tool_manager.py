import httpx
import asyncio
import logging
import re
from config_loader import get_config

log = logging.getLogger("tool-manager")

SHELL_SERVER_URL = None

_ingest_sem = asyncio.Semaphore(2)

RE_SHELL = re.compile(r'<run-shell>(.*?)</run-shell>', re.DOTALL)
RE_PYTHON = re.compile(r'<run-python>(.*?)</run-python>', re.DOTALL)

# ── shell URL state ───────────────────────────────────────────────────────────

def set_shell_url(url: str):
    global SHELL_SERVER_URL
    SHELL_SERVER_URL = url
    log.info(f"SHELL_SERVER_URL set to {url}")

# ── tool schemas ──────────────────────────────────────────────────────────────

TOOLS = []

# ── tool execution ────────────────────────────────────────────────────────────

async def execute_tool(name: str, args: dict) -> str:
    log.info(f"execute name={name} args={args}")

    if name == "ingest_url":
        return await _execute_ingest(args)

    if name == "run_shell":
        return await _execute_shell(args)

    if name == "run_python":
        return await _execute_python(args)

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
