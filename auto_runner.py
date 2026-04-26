"""
auto_runner.py

Agentic loop using the model's native tool-call protocol.

Design:
- Content tokens stream live to Open WebUI as they arrive
- Tool calls accumulate via finish_reason="tool_calls", then execute
- Results injected as role="tool" — the format the model was trained on
- Shadow context (on_clean_turn) stores only clean prose + tool turns,
  never any display markup — so the model never sees <details> in history

Display:
- Params block yielded immediately when tool call is decided (before execution)
- Result block yielded after execution with stripped single-line preview
- Both are fully closed <details> blocks — no open HTML, no rendering glitches
"""

import httpx
import json
import logging
import re
from typing import Callable, Awaitable, AsyncGenerator

from tool_manager import TOOLS

log = logging.getLogger("auto-runner")

_RE_EXIT_CODE = re.compile(r'exit code: `(\d+)`')


# ── display helpers ───────────────────────────────────────────────────────────

def _strip_preview(text: str, max_len: int = 160) -> str:
    """Single-line preview: strip markdown, collapse whitespace."""
    t = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    t = re.sub(r'`{2,}[^\n]*\n', '', t)          # opening fence lines
    t = re.sub(r'`{2,}', '', t)                   # closing fences
    t = re.sub(r'\*{1,3}([^*\n]+)\*{1,3}', r'\1', t)
    t = re.sub(r'`([^`]+)`', r'\1', t)
    t = re.sub(r'^[\-\*\+]\s+', '', t, flags=re.MULTILINE)
    t = re.sub(r'\s+', ' ', t).strip()
    return (t[:max_len] + "…") if len(t) > max_len else t


def _fenced(text: str) -> str:
    return f"``````\n{text.strip()}\n``````"


def _exit_code(result: str) -> int | None:
    m = _RE_EXIT_CODE.search(result)
    return int(m.group(1)) if m else None


def _details(summary: str, body: str) -> str:
    """Fully closed collapsed block — double newline ensures block-level rendering."""
    return f"\n\n<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>\n"


def _params_block(tool_name: str, args: dict) -> str:
    """Yield immediately when the model decides to call a tool."""
    icon = {"run_shell": "▶ $", "run_python": "▶ 🐍",
            "spawn_agent": "🤖", "write_file": "📝"}.get(tool_name, "▶")

    if tool_name == "run_shell":
        cmd = args.get("command", "")
        summary = f"{icon}  {cmd[:80]}{'…' if len(cmd)>80 else ''}"
        body = _fenced(cmd)

    elif tool_name == "run_python":
        code = args.get("code", "")
        first = code.strip().split("\n")[0][:80]
        summary = f"{icon}  {first}"
        body = _fenced(code)

    elif tool_name == "spawn_agent":
        prompt    = args.get("prompt", "")
        file_path = args.get("file_path")
        files     = args.get("files", [])
        all_files = list(files) + ([file_path] if file_path and file_path not in files else [])
        preview   = prompt[:80] + ("…" if len(prompt) > 80 else "")
        names     = ", ".join(f.split("/")[-1] for f in all_files[:3])
        if len(all_files) > 3:
            names += f" +{len(all_files)-3}"
        summary = f"{icon}  {preview}" + (f"  —  {names}" if names else "")

        parts = []
        if all_files:
            parts.append("**Files:**\n" + "\n".join(f"- `{f}`" for f in all_files))
        if args.get("context"):
            parts.append(f"**Context:** {args['context']}")
        parts.append(f"**Prompt:** {prompt}")
        body = "\n\n".join(parts)

    elif tool_name == "write_file":
        path   = args.get("path", "")
        prompt = args.get("prompt", "")
        summary = f"{icon}  {path}"
        body = f"**Path:** `{path}`\n\n**Prompt:** {prompt}"

    elif tool_name == "run_test":
        hyp  = args.get("hypothesis", "")
        code = args.get("code", "")
        first = code.strip().split("\n")[0][:80]
        summary = f"🧪  {hyp[:80]}" if hyp else f"🧪  {first}"
        body_parts = []
        if hyp:
            body_parts.append(f"**Hypothesis:** {hyp}")
        body_parts.append(f"**Code:**\n\n{_fenced(code)}")
        body = "\n\n".join(body_parts)

    elif tool_name == "patch_file":
        path        = args.get("path", "")
        instruction = args.get("instruction", "")
        summary = f"🩹  {path.split('/')[-1]}"
        body = f"**Path:** `{path}`\n\n**Instruction:** {instruction}"

    else:
        summary = f"▶  {tool_name}"
        body = _fenced(json.dumps(args, indent=2))

    return _details(summary, body)


def _result_block(tool_name: str, args: dict, result: str) -> str:
    """Yield after execution with result preview as summary."""
    icon = {"run_shell": "✓ $" if (_exit_code(result) or 0) == 0 else "✗ $",
            "run_python": "✓ 🐍" if (_exit_code(result) or 0) == 0 else "✗ 🐍",
            "spawn_agent": "🤖", "write_file": "📝"}.get(tool_name, "✓")

    preview = _strip_preview(result)

    if tool_name == "run_shell":
        cmd = args.get("command", "")
        body = f"**Command:**\n\n{_fenced(cmd)}\n\n**Output:**\n\n{_fenced(result)}"

    elif tool_name == "run_python":
        code = args.get("code", "")
        body = f"**Code:**\n\n{_fenced(code)}\n\n**Output:**\n\n{_fenced(result)}"

    elif tool_name == "spawn_agent":
        # Result is already markdown from the sub-agent — no extra fencing
        body = result

    elif tool_name == "write_file":
        path = args.get("path", "")
        lines_m = re.search(r'\((\d+) lines\)', result)
        n_lines = int(lines_m.group(1)) if lines_m else 0
        preview_m = re.search(r'```\n(.*?)```', result, re.DOTALL)
        file_preview = preview_m.group(1) if preview_m else ""
        preview = f"📝 {path} — {n_lines} lines"
        body = f"**Path:** `{path}`\n\n**Preview:**\n\n{_fenced(file_preview)}" if file_preview else result

    elif tool_name == "run_test":
        preview = _strip_preview(result)
        code_val = _exit_code(result)
        icon = "✓ 🧪" if code_val == 0 else "✗ 🧪"
        summary = f"{icon}  {preview}"
        return _details(summary, result)

    elif tool_name == "patch_file":
        if result.startswith("Patch applied"):
            summary = f"🩹  ✓  {args.get('path','').split('/')[-1]}"
        else:
            summary = f"🩹  ✗  {args.get('path','').split('/')[-1]}"
        return _details(summary, result)

    else:
        body = _fenced(result)

    # For shell/python prepend the exit-code icon to the preview
    if tool_name in ("run_shell", "run_python"):
        code_val = _exit_code(result)
        status = "✓" if code_val == 0 else (f"✗ exit {code_val}" if code_val is not None else "✗")
        summary = f"{status}  {preview}"
    else:
        summary = f"{icon}  {preview}"

    return _details(summary, body)


def _make_chunk(model: str, content: str, done: bool = False) -> str:
    return json.dumps({
        "model": model,
        "message": {"role": "assistant", "content": content},
        "done": done,
    }) + "\n"


# ── streaming ─────────────────────────────────────────────────────────────────

async def _stream_one_turn(
    openai_body: dict,
    llama_base: str,
    model_name: str,
) -> AsyncGenerator[tuple, None]:
    """
    Stream one LLM turn.

    Yields:
      ("text",  chunk_str, content)      — forward content token
      ("tool",  chunk_str, tool_calls)   — model decided to call tools
      ("done",  "",        full_content) — clean finish, no tool call
    """
    full_content = ""
    tool_calls_buf: dict[int, dict] = {}   # index → {name, args_str, id}

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream(
            "POST", f"{llama_base}/v1/chat/completions", json=openai_body
        ) as resp:
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[6:]
                if raw == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw)
                    choice = chunk["choices"][0]
                    delta  = choice["delta"]
                    finish = choice.get("finish_reason")

                    # ── tool call accumulation ────────────────────────────────
                    if delta.get("tool_calls"):
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            if idx not in tool_calls_buf:
                                tool_calls_buf[idx] = {"name": "", "args_str": "", "id": ""}
                            if tc.get("id"):
                                tool_calls_buf[idx]["id"] = tc["id"]
                            fn = tc.get("function", {})
                            if fn.get("name"):
                                tool_calls_buf[idx]["name"] = fn["name"]
                            tool_calls_buf[idx]["args_str"] += fn.get("arguments", "")
                        continue

                    if finish == "tool_calls":
                        tool_calls = []
                        for idx in sorted(tool_calls_buf):
                            buf = tool_calls_buf[idx]
                            try:
                                args = json.loads(buf["args_str"] or "{}")
                            except Exception:
                                args = {}
                            tool_calls.append({
                                "id":   buf["id"] or f"call_{idx}",
                                "name": buf["name"],
                                "args": args,
                            })
                        yield "tool", "", tool_calls
                        return

                    # ── content token ─────────────────────────────────────────
                    content = delta.get("content", "")
                    if content:
                        full_content += content
                        yield "text", _make_chunk(model_name, content), content

                    if finish in ("stop", "length"):
                        break

                except Exception as e:
                    log.warning(f"chunk parse error: {e}")

    yield "done", "", full_content


# ── main loop ─────────────────────────────────────────────────────────────────

async def run_agentic_chat(
    openai_body: dict,
    model_name: str,
    llama_base: str,
    execute_tool_fn: Callable[[str, dict], Awaitable[str]],
    max_iterations: int = 10,
    max_consecutive_failures: int = 3,
    on_clean_turn: Callable[[str, list | None], None] | None = None,
) -> AsyncGenerator[str, None]:
    """
    Agentic loop using native tool-call protocol.

    on_clean_turn(assistant_content, tool_results_or_None):
      assistant_content: prose the model generated this turn
      tool_results_or_None: list of {"tool_call_id", "name", "result"} or None
    """
    messages = list(openai_body["messages"])
    consecutive_failures = 0

    for iteration in range(max_iterations):
        log.info(f"iteration {iteration}, messages={len(messages)}")

        body = {
            **openai_body,
            "messages": messages,
            "stream": True,
            "tools": TOOLS,
            "tool_choice": "auto",
        }

        prose = ""
        tool_calls = None

        async for kind, chunk_str, payload in _stream_one_turn(body, llama_base, model_name):
            if kind == "text":
                prose += payload
                yield chunk_str

            elif kind == "tool":
                tool_calls = payload
                break

            elif kind == "done":
                prose = payload
                break

        # ── clean finish ──────────────────────────────────────────────────────
        if tool_calls is None:
            log.info(f"iteration {iteration}: clean finish")
            if on_clean_turn:
                on_clean_turn(prose, None)
            yield _make_chunk(model_name, "", done=True)
            return

        # ── execute tool calls ────────────────────────────────────────────────
        tool_results = []
        had_failure  = False

        for tc in tool_calls:
            name = tc["name"]
            args = tc["args"]
            call_id = tc["id"]

            log.info(f"iteration {iteration}: tool={name} args_keys={list(args)}")

            # Params block — yield immediately before execution
            yield _make_chunk(model_name, _params_block(name, args))

            result = await execute_tool_fn(name, args)

            # Track failures for shell/python only
            if name in ("run_shell", "run_python"):
                code = _exit_code(result)
                if (code or 0) != 0:
                    had_failure = True
                    log.warning(f"non-zero exit for {name}")

            # Result block
            yield _make_chunk(model_name, _result_block(name, args, result))

            tool_results.append({
                "tool_call_id": call_id,
                "name": name,
                "result": result,
            })

        consecutive_failures = (consecutive_failures + 1) if had_failure else 0

        if consecutive_failures >= max_consecutive_failures:
            log.warning(f"circuit-breaker: {consecutive_failures} consecutive failure(s)")
            yield _make_chunk(
                model_name,
                f"\n⚠️ Auto-run stopped after {consecutive_failures} consecutive "
                "failure(s). Type `.run` to retry manually.\n",
            )
            yield _make_chunk(model_name, "", done=True)
            return

        # ── persist clean turn ────────────────────────────────────────────────
        if on_clean_turn:
            on_clean_turn(prose, tool_results)

        # ── inject into message history ───────────────────────────────────────
        # Assistant turn: prose + tool_calls array (OpenAI multi-turn format)
        messages.append({
            "role": "assistant",
            "content": prose,
            "tool_calls": [
                {
                    "id": tr["tool_call_id"],
                    "type": "function",
                    "function": {
                        "name": tr["name"],
                        "arguments": json.dumps(
                            next(tc["args"] for tc in tool_calls if tc["id"] == tr["tool_call_id"])
                        ),
                    },
                }
                for tr in tool_results
            ],
        })
        # One role=tool message per result
        for tr in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": tr["tool_call_id"],
                "content": tr["result"],
            })

        log.info(f"iteration {iteration}: {len(tool_results)} tool result(s) injected, looping")

    yield _make_chunk(model_name, "", done=True)
