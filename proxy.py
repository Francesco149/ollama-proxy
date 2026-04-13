import httpx
import json
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

from vision_module import to_openai_messages
from tool_manager import TOOLS, execute_tool
from session_manager import SessionManager
from skill_engine import SkillEngine

LLAMA_BASE = "http://localhost:8080"
INGEST_BASE = "http://localhost:8083"
EMBEDDING_BASE = "http://localhost:6080"
MODEL_NAME = "gemma4"
REAL_MODEL = None

app = FastAPI()
session_manager = SessionManager()
skill_engine = SkillEngine(session_manager)

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("proxy")

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

# ── embeddings ────────────────────────────────────────────────────────────────

@app.post("/api/embed")
async def embeddings(request: Request):
    log.info(f"[proxy] embedding request received")
    body_json = await request.json()
    headers = dict(request.headers)
    
    # Remove host header to avoid conflicts with the downstream service
    headers.pop("host", None)

    # Protocol translation: Ollama "prompt" -> OpenAI "input"
    if "prompt" in body_json:
        body_json["input"] = body_json.pop("prompt")
    
    content = json.dumps(body_json)

    # Remove Content-Length to allow httpx to recalculate it for the new body size
    headers.pop("content-length", None)
    headers.pop("Content-Length", None)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{EMBEDDING_BASE}/embedding",
            content=content,
            headers=headers
        )
        data = resp.json()
        
        # Reformat response for Open-WebUI compatibility
        # Downstream returns: [{"embedding": [...]}, ...]
        # Open-WebUI expects: {"embeddings": [[...], ...]}
        # Note: llama.cpp may return a nested list, so we flatten it if necessary.
        if isinstance(data, list):
            new_data = {"embeddings": [item["embedding"] if isinstance(item["embedding"][0], float) else item["embedding"][0] for item in data if "embedding" in item]}
            return JSONResponse(
                content=new_data,
                status_code=resp.status_code
            )
            
        return JSONResponse(
            content=data,
            status_code=resp.status_code
        )

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

    # Use skill engine for injection
    messages = skill_engine.process_message(messages)

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
