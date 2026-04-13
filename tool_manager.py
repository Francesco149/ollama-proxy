import httpx
import asyncio
import logging

log = logging.getLogger("tool")

INGEST_BASE = "http://localhost:8083"
_ingest_sem = asyncio.Semaphore(2)  # max 2 concurrent

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

    return f"Unknown tool: {name}"
