import httpx
import json
import logging
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

from vision_module import to_openai_messages
from tool_manager import TOOLS, execute_tool, set_shell_url, set_llm_config, set_context_provider, set_doc_updater, process_manual_command
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

session_cfg = config.get("session", {})
SESSION_PERSIST_PATH = session_cfg.get("persist_path", None)

tools_ctx_cfg = config.get("tools", {}).get("context", {})
TOOL_CONTEXT_MODES = {
    k: v for k, v in tools_ctx_cfg.items()
    if k in ("spawn_agent", "write_file", "patch_file", "run_test")
}
TOOL_CONTEXT_MAX_MESSAGES = tools_ctx_cfg.get("max_messages", 20)

ctx_cfg = config.get("context", {})
EVICTION_KEEP_TURNS = ctx_cfg.get("eviction_keep_turns", 8)
EVICTION_TOKEN_BUDGET = ctx_cfg.get("token_budget", 12000)

autorun_cfg = config.get("autorun", {})
TOOL_SUPPRESSION_ENABLED = autorun_cfg.get("tool_suppression", True)

REAL_MODEL = None

# ── app + singletons ──────────────────────────────────────────────────────────

app = FastAPI()
session_manager = SessionManager(persist_path=SESSION_PERSIST_PATH)
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


@app.post("/clear_session")
async def clear_session(request: Request):
    """
    Clear the shadow context for a session so the next request starts fresh.
    Body: {"session_id": "..."} or {} to clear all sessions.
    """
    body = await request.json()
    sid = body.get("session_id")
    if sid:
        session_manager.clear_session(sid)
        log.info(f"cleared session: {sid}")
        return {"status": "ok", "session_id": sid}
    else:
        count = len(session_manager.clean_messages)
        for s in list(session_manager.clean_messages.keys()):
            session_manager.clear_session(s)
        log.info(f"cleared all {count} session(s)")
        return {"status": "ok", "cleared": count}

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
        oai_messages = openai_body["messages"]  # already OpenAI-format

        # context_key: hash of PRIOR user messages (parent state).
        # next_key:    hash of ALL user messages including this one.
        # Branching is automatic: editing any prior message changes ctx_key,
        # landing on a different (or absent) parent → new branch seeded.
        ctx_key  = session_manager.context_key(oai_messages)
        next_key = session_manager.next_context_key(oai_messages)

        # ── context provider + doc updater (set before any tool executes) ───────
        set_context_provider(
            provider=lambda: session_manager.get_clean_messages(next_key),
            modes=TOOL_CONTEXT_MODES if TOOL_CONTEXT_MODES else None,
            max_messages=TOOL_CONTEXT_MAX_MESSAGES,
        )
        set_doc_updater(
            lambda section, doc_content: session_manager.update_doc_section(next_key, section, doc_content)
        )

        if session_manager.has_clean_context(ctx_key):
            new_user = next((m for m in reversed(oai_messages) if m["role"] == "user"), None)
            if new_user:
                session_manager.append_clean(ctx_key, new_user)
            context_messages = session_manager.get_clean_messages(ctx_key)
            session_manager.register_next(ctx_key, next_key)
            log.info(f"resuming context {ctx_key} ({len(context_messages)} messages)")
        elif session_manager.has_clean_context(next_key):
            context_messages = session_manager.get_clean_messages(next_key)
            log.info(f"exact context {next_key} found ({len(context_messages)} messages)")
        else:
            session_manager.init_clean_context(next_key, oai_messages)
            context_messages = oai_messages
            log.info(f"new context {next_key} seeded ({len(oai_messages)} messages)")

        # ── working document: always prepend current state ────────────────────
        doc_content = session_manager.get_doc_rendered(next_key)
        working_doc_msg = {
            "role": "system",
            "content": doc_content,
            "_working_doc": True,  # marker so auto_runner can identify it
        }
        # Strip any prior working doc from context_messages, re-inject fresh
        context_messages = [m for m in context_messages if not m.get("_working_doc")]
        openai_body = {**openai_body, "messages": context_messages}

        def on_clean_turn(
            assistant_content: str,
            tool_results: list | None,
        ) -> None:
            turns: list[dict] = []
            if tool_results:
                tool_calls_arr = [
                    {
                        "id": tr["tool_call_id"],
                        "type": "function",
                        "function": {
                            "name": tr["name"],
                            # Store actual arguments — "{}" here would teach the model
                            # that empty args are valid, corrupting future generations.
                            "arguments": tr.get("arguments", "{}"),
                        },
                    }
                    for tr in tool_results
                ]
                turns.append({
                    "role": "assistant",
                    "content": assistant_content,
                    "tool_calls": tool_calls_arr,
                })
                for tr in tool_results:
                    turns.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_call_id"],
                        "content": tr["result"],
                    })
            else:
                turns.append({"role": "assistant", "content": assistant_content})

            session_manager.append_clean(next_key, *turns)
            # Evict stale turns to stay within budget
            session_manager.evict(
                next_key,
                keep_turns=EVICTION_KEEP_TURNS,
                token_budget=EVICTION_TOKEN_BUDGET,
            )
            log.debug(
                f"context {next_key}: persisted + evicted "
                f"({'tools' if tool_results else 'final'})"
            )

        stuck_state = session_manager.get_stuck_state(next_key)

        def on_stuck_state(state: dict) -> None:
            session_manager.save_stuck_state(next_key, state)

        return StreamingResponse(
            run_agentic_chat(
                openai_body,
                MODEL_NAME,
                LLAMA_BASE,
                execute_tool,
                max_iterations=AUTORUN_MAX_ITER,
                max_consecutive_failures=AUTORUN_MAX_FAILURES,
                on_clean_turn=on_clean_turn,
                working_doc_system_msg=working_doc_msg,
                stuck_state=stuck_state,
                on_stuck_state=on_stuck_state,
                tool_suppression_enabled=TOOL_SUPPRESSION_ENABLED,
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
