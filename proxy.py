import httpx
import json
import glob
import os
import re
import hashlib
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

LLAMA_BASE = "http://localhost:8080"
INGEST_BASE = "http://localhost:8083"
MODEL_NAME = "gemma4"
SKILLS_DIR = "/opt/ai-lab/skills"
MAX_SKILLS = 2
MIN_SCORE = 0.15
REAL_MODEL = None

app = FastAPI()
active_skills: dict[str, set[str]] = {}

_ingest_sem = asyncio.Semaphore(2)  # max 2 concurrent

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ingest")

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

# ── startup ───────────────────────────────────────────────────────────────────

async def get_real_model():
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{LLAMA_BASE}/v1/models")
        return r.json()["data"][0]["id"]

@app.on_event("startup")
async def startup():
    global REAL_MODEL
    REAL_MODEL = await get_real_model()
    log.info(f"[proxy] real model: {REAL_MODEL}")

# ── ollama stubs ──────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return {"version": "0.1.0"}

@app.get("/api/version")
async def version():
    return {"version": "0.1.0"}

@app.get("/api/tags")
async def tags():
    return {"models": [{
        "name": MODEL_NAME,
        "model": MODEL_NAME,
        "modified_at": "2025-01-01T00:00:00Z",
        "details": {"families": ["gemma", "clip"]},
    }]}

@app.post("/api/show")
async def show():
    return {
        "modelfile": "FROM gemma4\n",
        "details": {"families": ["gemma", "clip"]},
        "model_info": {"general.architecture": "gemma3", "vision_encoder": "clip"},
    }

@app.get("/api/ps")
async def ps():
    return {"models": [{
        "name": MODEL_NAME,
        "model": MODEL_NAME,
        "size": 0,
        "digest": "abc123",
        "expires_at": "2099-01-01T00:00:00Z",
        "details": {"families": ["gemma", "clip"]},
    }]}

# ── skill router ──────────────────────────────────────────────────────────────

def load_skills():
    skills, triggers = {}, {}
    for path in glob.glob(os.path.join(SKILLS_DIR, "*.md")):
        name = os.path.basename(path).replace(".md", "")
        with open(path) as f:
            content = f.read()
        skills[name] = content
        match = re.match(r'^---\s*\ntriggers:\s*(.+?)\n---', content, re.DOTALL)
        triggers[name] = (
            set(match.group(1).strip().lower().split()) if match
            else set(content[:300].lower().split())
        )
    return skills, triggers

def score(message: str, trigger_words: set) -> float:
    words = message.lower().split()
    msg_words = set(words)
    bigrams = {f"{words[i]} {words[i+1]}" for i in range(len(words)-1)}
    overlap = (msg_words | bigrams) & trigger_words
    return len(overlap) / max(len(trigger_words), 1)

def get_session_id(messages: list) -> str:
    for m in messages:
        if m["role"] == "user":
            content = m["content"]
            if isinstance(content, list):
                content = " ".join(p.get("text","") for p in content if p.get("type")=="text")
            return hashlib.sha256(content.encode()).hexdigest()[:16]
    return "default"

def update_active_skills(session_id, message, skills, triggers):
    if session_id not in active_skills:
        active_skills[session_id] = set()
    scores = {name: score(message, triggers[name]) for name in skills}
    log.info(f"[skill-router] session={session_id} scores={scores}")
    newly = {n for n, s in scores.items() if s >= MIN_SCORE}
    if newly - active_skills[session_id]:
        log.info(f"[skill-router] activating: {newly - active_skills[session_id]}")
    active_skills[session_id] |= newly
    if len(active_skills[session_id]) > MAX_SKILLS:
        top = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        active_skills[session_id] = {n for n, _ in top[:MAX_SKILLS]}
    log.info(f"[skill-router] active: {active_skills[session_id]}")
    return active_skills[session_id]

def inject_skills(messages: list) -> list:
    skills, triggers = load_skills()
    session_id = get_session_id(messages)
    last_user = None
    for m in reversed(messages):
        if m["role"] == "user":
            content = m["content"]
            if isinstance(content, str):
                last_user = content
            elif isinstance(content, list):
                last_user = " ".join(p.get("text","") for p in content if p.get("type")=="text")
            break
    if not last_user:
        return messages
    session_skills = update_active_skills(session_id, last_user, skills, triggers)
    if not session_skills:
        return messages
    blocks = [f"## Active Skill: {n}\n\n{skills[n]}" for n in session_skills if n in skills]
    injection = (
        "# Active workflow skills for this conversation:\n\n"
        + "\n\n---\n\n".join(blocks)
        + "\n\n---\n\nThese skills remain active for the entire conversation.\n\n"
    )
    messages = list(messages)
    for i, msg in enumerate(messages):
        if msg["role"] == "system":
            messages[i] = dict(msg)
            messages[i]["content"] = injection + msg["content"]
            return messages
    messages.insert(0, {"role": "system", "content": injection})
    return messages

# ── tool execution ────────────────────────────────────────────────────────────

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

# ── format conversion ─────────────────────────────────────────────────────────

def to_openai_messages(messages: list, images: list = None) -> list:
    result = []
    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        msg_images = list(msg.get("images", []))
        if images and i == len(messages)-1 and msg["role"] == "user":
            msg_images += images
        if msg_images:
            parts = [{"type": "text", "text": content}]
            for img in msg_images:
                if not img.startswith("data:"):
                    img = f"data:image/jpeg;base64,{img}"
                parts.append({"type": "image_url", "image_url": {"url": img}})
            result.append({"role": msg["role"], "content": parts})
        else:
            result.append({"role": msg["role"], "content": content})
    return result

# ── chat ──────────────────────────────────────────────────────────────────────

@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    images = body.get("images", [])
    stream = body.get("stream", True)
    options = body.get("options", {})

    has_images = images or any(m.get("images") for m in messages)
    log.info(f"[proxy] chat stream={stream} images={has_images} msgs={len(messages)}")

    messages = inject_skills(messages)

    openai_body = {
        "model": REAL_MODEL,
        "messages": to_openai_messages(messages, images),
        "stream": stream,
        "temperature": options.get("temperature", 1.0),
        "tools": TOOLS,
        "tool_choice": "auto",
    }

    # non-streaming: simple tool loop
    if not stream:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(f"{LLAMA_BASE}/v1/chat/completions", json=openai_body)
            data = resp.json()
        msg = data["choices"][0]["message"]
        if msg.get("tool_calls"):
            tool_results = []
            for tc in msg["tool_calls"]:
                args = json.loads(tc["function"]["arguments"])
                result = await execute_tool(tc["function"]["name"], args)
                tool_results.append(result)
            return JSONResponse({
                "model": MODEL_NAME,
                "message": {"role": "assistant", "content": "\n".join(tool_results)},
                "done": True,
            })
        return JSONResponse({
            "model": MODEL_NAME,
            "message": {"role": "assistant", "content": msg.get("content", "")},
            "done": True,
        })

    # streaming: buffer until tool_call complete, then execute and stream result
    async def generate():
        tool_call_buffer = ""
        tool_name = None
        in_tool_call = False
        content_buffer = ""

        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream(
                "POST", f"{LLAMA_BASE}/v1/chat/completions", json=openai_body
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0]["delta"]
                        finish = chunk["choices"][0].get("finish_reason")

                        # tool call detection
                        if delta.get("tool_calls"):
                            in_tool_call = True
                            tc = delta["tool_calls"][0]
                            if tc.get("function", {}).get("name"):
                                tool_name = tc["function"]["name"]
                            tool_call_buffer += tc.get("function", {}).get("arguments", "")
                            continue

                        if finish == "tool_calls" or (in_tool_call and finish == "stop"):
                            log.info(f"[tool] tool_name={tool_name} buffer={tool_call_buffer!r}")
                            # execute tool
                            try:
                                args = json.loads(tool_call_buffer)
                            except Exception as e:
                                log.info(f"[tool] json parse failed: {e}")
                                args = {}
                            yield json.dumps({
                                "model": MODEL_NAME,
                                "message": {"role": "assistant", "content": "⏳ Processing..."},
                                "done": False,
                            }) + "\n"
                            result = await execute_tool(tool_name or "", args)
                            yield json.dumps({
                                "model": MODEL_NAME,
                                "message": {"role": "assistant", "content": result},
                                "done": False,
                            }) + "\n"
                            break

                        content = delta.get("content", "")
                        if content:
                            yield json.dumps({
                                "model": MODEL_NAME,
                                "message": {"role": "assistant", "content": content},
                                "done": False,
                            }) + "\n"

                    except Exception:
                        continue

        yield json.dumps({
            "model": MODEL_NAME,
            "message": {"role": "assistant", "content": ""},
            "done": True,
        }) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=11434, log_level="info")
