import httpx
import asyncio
import logging
import re

log = logging.getLogger("tool")

SHELL_SERVER_URL = None

INGEST_BASE = "http://localhost:8083"
_ingest_sem = asyncio.Semaphore(2)  # max 2 concurrent

SHELL_EXTRACT_PATTERN = re.compile(r'`````\s*(.*?)\s*`````', re.DOTALL)

def set_shell_url(url: str):
    global SHELL_SERVER_URL
    SHELL_SERVER_URL = url
    log.info(f"[tool] SHELL_SERVER_URL set to {url}")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ingest_url",
            "description": (
                "Save one or more URLs to the knowledge base. Use when the user shares links and asks to "
                "remember, save, archive, or learn from them. Works with YouTube videos, articles, and web pages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of URLs to ingest"
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional note about why these are being saved"
                    }
                },
                "required": ["urls"]
            }
        }
    }
]

async def process_manual_command(messages: list) -> str | None:
    if not messages:
        return None
    
    last_msg = messages[-1]
    if last_msg.get("role") != "user":
        return None
    
    content = last_msg.get("content", "").strip()
    
    if content == ".run":
        # Iterate backwards to find the most recent assistant message
        assistant_msg = None
        for msg in reversed(messages[:-1]):
            if msg.get("role") == "assistant":
                assistant_msg = msg
                break
        
        if not assistant_msg:
            return "No assistant message found to extract commands from."
        
        matches = SHELL_EXTRACT_PATTERN.findall(assistant_msg.get("content", ""))
        if not matches:
            return "No shell commands found in the last assistant message."
        
        results = []
        for cmd in matches:
            res = await execute_tool("run_shell", {"command": cmd.strip()})
            results.append(res)
        
        return "\n\n---\n\n".join(results)

    if content.startswith(".run "):
        cmd = content[5:].strip()
        if not cmd:
            return "No command provided after .run"
        return await execute_tool("run_shell", {"command": cmd})

    return None

async def execute_tool(name: str, args: dict) -> str:
    log.info(f"[tool] execute name={name} args={args}")
    if name == "ingest_url":
        urls = args.get("urls", [])

        if isinstance(urls, str):
            urls = [urls]

        # also check for singular "url" key — model sometimes ignores schema
        if not urls and args.get("url"):
            urls = [args["url"]]

        log.info(f"[tool] urls to ingest: {urls}")
        if not urls:
            return "No URLs found in tool call args"

        note = args.get("note", "")

        async def ingest_one(url: str) -> str:
            try:
                log.info(f"[tool] ingesting {url}")
                async with _ingest_sem:
                    async with httpx.AsyncClient(timeout=600) as client:
                        log.info(f"[tool] ingest_url {url}")
                        resp = await client.post(f"{INGEST_BASE}/ingest", json={"url": url, "note": note})
                        r = resp.json()
                    if r.get("status") == "ok":
                        return f"✓ {r.get('title') or r.get('domain') or url}"
                    elif r.get("status") == "todo":
                        return f"⚠ unsupported URL saved as todo: {url}"
                    else:
                        return f"✗ failed: {url} — {r.get('error')}"
            except Exception as e:
                log.error(f"[tool] ingest_one exception for {url}: {e}", exc_info=True)
                return f"✗ exception: {url} — {e}"

        try:
            results = await asyncio.gather(*[ingest_one(url) for url in urls])
            return "Saved to knowledge base:\n" + "\n".join(results)
        except Exception as e:
            log.error(f"[tool] gather exception: {e}", exc_info=True)
            return f"Error during ingestion: {e}"

    if name == "run_shell":
        command = args.get("command")
        if not command:
            return "No command provided for run_shell"
        
        if not SHELL_SERVER_URL:
            return "Shell server URL is not configured"

        try:
            log.info(f"[tool] running shell command: {command}")
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
            log.error(f"[tool] run_shell exception: {e}", exc_info=True)
            return f"Error executing shell command: {e}"

    return f"Unknown tool: {name}"
