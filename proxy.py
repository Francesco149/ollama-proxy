import httpx
import json
import logging
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

from vision_module import to_openai_messages
from tool_manager import TOOLS, execute_tool, set_shell_url, set_llm_config, process_manual_command
from auto_runner import run_agentic_chat
from session_manager import SessionManager
from skill_engine import SkillEngine
from stream_handler import handle_non_streaming_chat, generate_streaming_chat
from config_loader import get_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("proxy")

# ── config ────────────────────────────────────────────────────────────────────

config = get_config()
server_cfg = config.get("server", {})
LLAMA_BASE = server_cfg.get("llama_base", "http://localhost:8080")
INGEST_BASE = server_cfg.get("ingest_base", "http://localhost:8083")
EMBEDDING_BASE = server_cfg.get("embedding_base", "http://localhost:6080")
MODEL_NAME = server_cfg.get("model_name", "gemma4")

autorun_cfg = config.get("autorun", {})
AUTORUN_ENABLED = autorun_cfg.get("enabled", False)
AUTORUN_MAX_ITER = autorun_cfg.get("max_iterations", 10)
AUTORUN_MAX_FAILURES = autorun_cfg.get("max_consecutive_failures", 3)

REAL_MODEL = None

# ── app + singletons ──────────────────────────────────────────────────────────

app = FastAPI()
session_manager = SessionManager()
skill_engine = SkillEngine(session_manager)

# ── startup ───────────────────────────────────────────────────────────────────

async def _resolve_real_model() -> str:
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{LLAMA_BASE}/v1/models")
        return r.json()["data"][0]["id"]

@app.on_event("startup")
async def startup():
    global REAL_MODEL
    REAL_MODEL = await _resolve_real_model()
    log.info(f"real model: {REAL_MODEL}")
    set_llm_config(LLAMA_BASE, REAL_MODEL)
    log.info(f"autorun enabled={AUTORUN_ENABLED} max_iter={AUTORUN_MAX_ITER} max_failures={AUTORUN_MAX_FAILURES}")

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

# ── shell registration ────────────────────────────────────────────────────────

@app.post("/register_shell")
async def register_shell(request: Request):
    body = await request.json()
    url = body.get("url")
    if not url:
        return JSONResponse(content={"error": "url is required"}, status_code=400)
    set_shell_url(url)
    log.info(f"shell registered: {url}")
    return {"status": "ok"}

# ── embeddings ────────────────────────────────────────────────────────────────

@app.post("/api/embed")
async def embeddings(request: Request):
    log.info("embedding request received")
    body_json = await request.json()
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers.pop("Content-Length", None)

    if "prompt" in body_json:
        body_json["input"] = body_json.pop("prompt")

    content = json.dumps(body_json)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{EMBEDDING_BASE}/embedding",
            content=content,
            headers=headers,
        )
        data = resp.json()

    if isinstance(data, list):
        embeddings_out = [
            item["embedding"] if isinstance(item["embedding"][0], float) else item["embedding"][0]
            for item in data
            if "embedding" in item
        ]
        return JSONResponse(content={"embeddings": embeddings_out}, status_code=resp.status_code)

    return JSONResponse(content=data, status_code=resp.status_code)

# ── chat ──────────────────────────────────────────────────────────────────────

@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    images = body.get("images", [])
    stream = body.get("stream", True)
    options = body.get("options", {})

    # Dot-command interceptor — bypasses LLM entirely
    if messages and messages[-1].get("role") == "user":
        user_input = messages[-1].get("content", "").strip()
        if user_input.startswith("."):
            result = await process_manual_command(messages)
            if result:
                return JSONResponse(content={"message": {"role": "assistant", "content": result}})
            return JSONResponse(content={"message": {"role": "assistant", "content": "❌ Error: No commands found."}})

    has_images = images or any(m.get("images") for m in messages)
    log.info(f"chat stream={stream} images={has_images} msgs={len(messages)}")

    messages = skill_engine.process_message(messages)

    openai_body = {
        "model": REAL_MODEL,
        "messages": to_openai_messages(messages, images),
        "stream": stream,
        "temperature": options.get("temperature", 1.0),
        "tools": TOOLS,
        "tool_choice": "auto",
    }

    if not stream:
        return await handle_non_streaming_chat(openai_body, MODEL_NAME, LLAMA_BASE, execute_tool)

    if AUTORUN_ENABLED:
        return StreamingResponse(
            run_agentic_chat(
                openai_body,
                MODEL_NAME,
                LLAMA_BASE,
                execute_tool,
                max_iterations=AUTORUN_MAX_ITER,
                max_consecutive_failures=AUTORUN_MAX_FAILURES,
            ),
            media_type="application/x-ndjson",
        )

    return StreamingResponse(
        generate_streaming_chat(openai_body, MODEL_NAME, LLAMA_BASE, execute_tool),
        media_type="application/x-ndjson",
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=11434, log_level="info")
