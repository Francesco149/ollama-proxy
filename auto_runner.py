"""
auto_runner.py

Agentic loop using native tool-call protocol.

Key design properties:
  Working document: injected as the first system message each turn.
    The model updates it via update_document — findings go there before
    tool results are evicted from context.

  Eviction: after each on_clean_turn callback, old turns are compressed.
    role=tool results and command-output user turns become summary lines.
    The model's prose is preserved. Budget is configurable.

  Stuck detection: tracks consecutive identical tool calls and no-op turns.
    Injects a single redirect nudge when stuck; avoids compounding by
    limiting nudges to once per session of spinning.

  Empty arg guard: validates tool args before execution. Returns a
    structured error the model can self-correct from rather than executing
    a no-op or crashing.

  Context is never polluted with <details> markup — display blocks are
  fire-and-forget yields to Open WebUI only.
"""

import httpx
import json
import logging
import re
from typing import Callable, Awaitable, AsyncGenerator

from tool_manager import TOOLS, validate_tool_args

log = logging.getLogger("auto-runner")

_RE_EXIT_CODE = re.compile(r'exit code: `(\d+)`')


# ── display helpers ───────────────────────────────────────────────────────────

def _strip_preview(text: str, max_len: int = 160) -> str:
    t = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    t = re.sub(r'`{2,}[^\n]*\n', '', t)
    t = re.sub(r'`{2,}', '', t)
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
    return f"\n\n<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>\n"


def _params_block(tool_name: str, args: dict) -> str:
    if tool_name == "update_document":
        section = args.get("section", "")
        content = args.get("content", "")[:120]
        return _details(f"📋  [{section}]", f"**Section:** {section}\n\n{content}…")

    if tool_name == "run_shell":
        cmd = args.get("command", "")
        summary = f"▶  $ {cmd[:80]}{'…' if len(cmd)>80 else ''}"
        return _details(summary, _fenced(cmd))

    if tool_name == "run_python":
        code = args.get("code", "")
        first = code.strip().split("\n")[0][:80]
        return _details(f"▶  🐍 {first}", _fenced(code))

    if tool_name == "spawn_agent":
        prompt    = args.get("prompt", "")
        file_path = args.get("file_path")
        files     = args.get("files", [])
        all_files = list(files) + ([file_path] if file_path and file_path not in files else [])
        preview   = prompt[:80] + ("…" if len(prompt) > 80 else "")
        names     = ", ".join(f.split("/")[-1] for f in all_files[:3])
        if len(all_files) > 3:
            names += f" +{len(all_files)-3}"
        summary = f"🤖  {preview}" + (f"  —  {names}" if names else "")
        parts = []
        if all_files:
            parts.append("**Files:**\n" + "\n".join(f"- `{f}`" for f in all_files))
        if args.get("context"):
            parts.append(f"**Context:** {args['context']}")
        parts.append(f"**Prompt:** {prompt}")
        return _details(summary, "\n\n".join(parts))

    if tool_name == "write_file":
        path = args.get("path", "")
        prompt = args.get("prompt", "")
        return _details(f"📝  {path}", f"**Path:** `{path}`\n\n**Prompt:** {prompt}")

    if tool_name == "run_test":
        hyp  = args.get("hypothesis", "")
        code = args.get("code", "")
        first = code.strip().split("\n")[0][:80]
        summary = f"🧪  {hyp[:80]}" if hyp else f"🧪  {first}"
        parts = []
        if hyp:
            parts.append(f"**Hypothesis:** {hyp}")
        parts.append(f"**Code:**\n\n{_fenced(code)}")
        return _details(summary, "\n\n".join(parts))

    if tool_name == "patch_file":
        path        = args.get("path", "")
        instruction = args.get("instruction", "")
        return _details(f"🩹  {path.split('/')[-1]}", f"**Path:** `{path}`\n\n**Instruction:** {instruction}")

    if tool_name == "git_commit":
        msg = args.get("message", "")
        return _details(f"📦  git commit: {msg}", f"**Message:** {msg}")

    if tool_name == "search_code":
        n = len([l for l in result.strip().split("\n") if l])
        label = "search: " + str(n) + (" matches" if n != 1 else " match")
        return _details(label, _fenced(result))

    if tool_name == "read_file":
        truncated = "[truncated] " if result.startswith("[File truncated") else ""
        return _details("read_file: " + truncated + _strip_preview(result), _fenced(result))
        return _details(f"📄  {path.split('/')[-1]}", f"**Path:** `{path}`")

    return _details(f"▶  {tool_name}", _fenced(json.dumps(args, indent=2)))


def _result_block(tool_name: str, args: dict, result: str) -> str:
    preview = _strip_preview(result)

    if tool_name == "update_document":
        return _details("[doc] " + preview, result)

    if tool_name == "run_shell":
        cmd = args.get("command", "")
        code_val = _exit_code(result)
        status = "ok" if code_val == 0 else ("exit " + str(code_val) if code_val is not None else "err")
        body = "**Command:**\n\n" + _fenced(cmd) + "\n\n**Output:**\n\n" + _fenced(result)
        return _details(status + "  $ " + preview, body)

    if tool_name == "run_python":
        code = args.get("code", "")
        code_val = _exit_code(result)
        status = "ok" if code_val == 0 else ("exit " + str(code_val) if code_val is not None else "err")
        body = "**Code:**\n\n" + _fenced(code) + "\n\n**Output:**\n\n" + _fenced(result)
        return _details(status + "  py  " + preview, body)

    if tool_name == "spawn_agent":
        return _details("agent: " + preview, result)

    if tool_name == "write_file":
        path = args.get("path", "")
        lines_m = re.search(r"\((\d+) lines\)", result)
        n_lines = int(lines_m.group(1)) if lines_m else 0
        preview_m = re.search(r"```\n(.*?)```", result, re.DOTALL)
        file_preview = preview_m.group(1) if preview_m else ""
        body = "**Path:** `" + path + "`\n\n**Preview:**\n\n" + _fenced(file_preview) if file_preview else result
        return _details("write: " + path.split("/")[-1] + " (" + str(n_lines) + " lines)", body)

    if tool_name == "run_test":
        code_val = _exit_code(result)
        status = "pass" if code_val == 0 else "fail"
        return _details("test: " + status + "  " + preview, result)

    if tool_name == "patch_file":
        ok = result.startswith("Patch applied")
        return _details(("patch ok: " if ok else "patch fail: ") + args.get("path", "").split("/")[-1], result)

    if tool_name == "git_commit":
        ok = result.startswith(chr(10038) + " Committed") or result.startswith("ok") or "Committed" in result
        return _details(("commit ok: " if ok else "commit fail: ") + preview, result)

    if tool_name == "search_code":
        n = len([l for l in result.strip().split("\n") if l.strip()])
        return _details("search: " + str(n) + (" matches" if n != 1 else " match") + "  " + preview, _fenced(result))

    if tool_name == "read_file":
        truncated = "[truncated] " if result.startswith("[File truncated") else ""
        return _details("read: " + truncated + preview, _fenced(result))

    return _details("ok  " + preview, _fenced(result))

def _make_chunk(model: str, content: str, done: bool = False) -> str:
    return json.dumps({
        "model": model,
        "message": {"role": "assistant", "content": content},
        "done": done,
    }) + "\n"


# ── stuck detection ───────────────────────────────────────────────────────────

_STUCK_NUDGE = (
    "You appear to be repeating the same action or producing empty responses. "
    "Stop and take stock: call update_document to record what you know so far "
    "(Findings, current Plan state, any Open Questions), then reconsider the "
    "approach from first principles. If you're unsure what to do next, say so "
    "clearly rather than retrying the same tool call."
)


class StuckDetector:
    """
    Tracks repeated identical tool calls and empty turns across requests.
    State is serialisable so it can be persisted in session_manager and
    restored on the next HTTP request — preventing the reset-each-turn bug
    where the detector never accumulates enough repeat count to fire.
    """
    def __init__(
        self,
        repeat_threshold: int = 1,
        empty_threshold: int = 1,
        state: dict | None = None,
    ):
        self.repeat_threshold = repeat_threshold
        self.empty_threshold  = empty_threshold
        if state:
            self._last_call        = state.get("last_call")
            self._repeat_count     = state.get("repeat_count", 0)
            self._empty_count      = state.get("empty_count", 0)
            self._nudge_sent       = state.get("nudge_sent", False)
            self._suppressed_tools = set(state.get("suppressed_tools", []))
        else:
            self._last_call: str | None = None
            self._repeat_count   = 0
            self._empty_count    = 0
            self._nudge_sent     = False
            self._suppressed_tools: set[str] = set()

    def to_dict(self) -> dict:
        return {
            "last_call":        self._last_call,
            "repeat_count":     self._repeat_count,
            "empty_count":      self._empty_count,
            "nudge_sent":       self._nudge_sent,
            "suppressed_tools": list(self._suppressed_tools),
        }

    def record_tool_call(self, name: str, args: dict) -> bool:
        """Return True if this looks like a stuck repeat."""
        key = f"{name}:{json.dumps(args, sort_keys=True)}"
        if key == self._last_call:
            self._repeat_count += 1
        else:
            self._repeat_count = 0
            self._last_call = key
        return self._repeat_count >= self.repeat_threshold

    def record_empty_turn(self) -> bool:
        """Return True if we've had too many empty/no-tool turns."""
        self._empty_count += 1
        return self._empty_count >= self.empty_threshold

    def reset_empty(self) -> None:
        self._empty_count = 0

    def should_nudge(self) -> bool:
        return not self._nudge_sent

    def mark_nudged(self, tool_name: str | None = None) -> None:
        self._nudge_sent = True
        self._repeat_count = 0
        self._empty_count  = 0
        if tool_name:
            self._suppressed_tools.add(tool_name)

    def get_suppressed_tools(self) -> set[str]:
        return set(self._suppressed_tools)

    def clear_suppressed_tools(self) -> None:
        self._suppressed_tools.clear()
        self._nudge_sent = False  # allow another nudge after suppression resolves


# ── streaming ─────────────────────────────────────────────────────────────────

async def _stream_one_turn(
    openai_body: dict,
    llama_base: str,
    model_name: str,
) -> AsyncGenerator[tuple, None]:
    """
    Stream one LLM turn. Yields:
      ("text",  chunk_str, content)      — forward content token
      ("tool",  "",        tool_calls)   — model called tools
      ("done",  "",        full_content) — clean finish
    """
    full_content = ""
    tool_calls_buf: dict[int, dict] = {}

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
                    chunk  = json.loads(raw)
                    choice = chunk["choices"][0]
                    delta  = choice["delta"]
                    finish = choice.get("finish_reason")

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
    working_doc_system_msg: dict | None = None,
    stuck_state: dict | None = None,
    on_stuck_state: Callable[[dict], None] | None = None,
    tool_suppression_enabled: bool = True,
    working_doc_reminder_interval: int = 4,
) -> AsyncGenerator[str, None]:
    """
    Agentic loop with working document, stuck detection, and arg validation.

    stuck_state: serialised StuckDetector state from previous request.
      Persisting across requests prevents the reset-each-turn bug.
    on_stuck_state: called after each iteration with the current stuck state
      dict so the caller can persist it.
    tool_suppression_enabled: when True, tools that trigger stuck detection
      are temporarily removed from the tool list for subsequent iterations.
      Re-enabled once a different tool is called successfully.
    working_doc_system_msg: prepended to messages each iteration.
    on_clean_turn: called after each iteration for shadow context persistence.
    """
    messages = list(openai_body["messages"])
    consecutive_failures = 0
    stuck = StuckDetector(repeat_threshold=1, empty_threshold=1, state=stuck_state)

    for iteration in range(max_iterations):
        log.info(f"iteration {iteration}, messages={len(messages)}")

        # Inject current working document as first system message
        iter_messages = messages
        if working_doc_system_msg:
            # Replace or prepend the working doc system message
            non_doc = [m for m in messages if not m.get("_working_doc")]
            iter_messages = [working_doc_system_msg] + non_doc

        # Filter out suppressed tools for this iteration
        suppressed = stuck.get_suppressed_tools() if tool_suppression_enabled else set()
        active_tools = [t for t in TOOLS if t["function"]["name"] not in suppressed]
        if suppressed:
            log.warning(f"iteration {iteration}: suppressing tools: {suppressed}")

        body = {
            **openai_body,
            "messages": iter_messages,
            "stream": True,
            "tools": active_tools,
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
            if len(prose.strip()) < 50:
                # Suspiciously empty response
                if stuck.record_empty_turn() and stuck.should_nudge():
                    log.warning(f"iteration {iteration}: empty turn, injecting nudge")
                    stuck.mark_nudged()
                    if on_stuck_state:
                        on_stuck_state(stuck.to_dict())
                    nudge_msg = {"role": "user", "content": _STUCK_NUDGE}
                    messages.append({"role": "assistant", "content": prose})
                    messages.append(nudge_msg)
                    continue
            log.info(f"iteration {iteration}: clean finish")
            if on_clean_turn:
                on_clean_turn(prose, None)
            if on_stuck_state:
                on_stuck_state(stuck.to_dict())
            yield _make_chunk(model_name, "", done=True)
            return

        stuck.reset_empty()

        # ── execute tool calls ────────────────────────────────────────────────
        tool_results = []
        had_failure  = False
        is_update_doc = False

        for tc in tool_calls:
            name    = tc["name"]
            args    = tc["args"]
            call_id = tc["id"]

            # ── 1. stuck detection — runs first, before anything else ─────────
            # Placed first so it fires regardless of whether args are valid.
            # An empty repeated call is just as stuck as a valid repeated one.
            if stuck.record_tool_call(name, args) and stuck.should_nudge():
                log.warning(f"iteration {iteration}: stuck on {name}, injecting nudge")
                if tool_suppression_enabled:
                    stuck.mark_nudged(tool_name=name)
                    log.warning(f"suppressing tool '{name}' for subsequent iterations")
                else:
                    stuck.mark_nudged()
                nudge = f"[Stuck: {_STUCK_NUDGE}]"
                yield _make_chunk(model_name, f"\n\n⚠️ {_STUCK_NUDGE}\n\n")
                tool_results.append({"tool_call_id": call_id, "name": name, "result": nudge, "arguments": json.dumps(args)})
                continue

            # ── 2. arg validation ────────────────────────────────────────────
            err = validate_tool_args(name, args)
            if err:
                log.warning(f"iteration {iteration}: {name} bad args: {err}")
                yield _make_chunk(model_name, f"\n{err}\n")
                tool_results.append({"tool_call_id": call_id, "name": name, "result": err,
                                      "arguments": json.dumps(args)})
                had_failure = True
                continue

            # ── 3. update_document — inline, no network ──────────────────────
            if name == "update_document":
                is_update_doc = True
                yield _make_chunk(model_name, _params_block(name, args))
                result = await execute_tool_fn(name, args)
                yield _make_chunk(model_name, _result_block(name, args, result))
                tool_results.append({"tool_call_id": call_id, "name": name, "result": result, "arguments": json.dumps(args)})
                continue

            # ── 4. all other tools ───────────────────────────────────────────
            log.info(f"iteration {iteration}: {name} args_keys={list(args)}")

            yield _make_chunk(model_name, _params_block(name, args))

            result = await execute_tool_fn(name, args)

            if name in ("run_shell", "run_python"):
                code = _exit_code(result)
                if (code or 0) != 0:
                    had_failure = True
                    log.warning(f"non-zero exit for {name}")

            yield _make_chunk(model_name, _result_block(name, args, result))

            # Successful call — if this tool was suppressed before, clear all suppression
            # (the model has moved on, so the stuck state is resolved)
            if tool_suppression_enabled and name in stuck.get_suppressed_tools():
                log.info(f"iteration {iteration}: {name} succeeded after suppression — clearing")
                stuck.clear_suppressed_tools()

            tool_results.append({
                "tool_call_id": call_id,
                "name":         name,
                "result":       result,
                "arguments":    json.dumps(args),
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

        # ── persist clean turn + stuck state ─────────────────────────────────
        if on_clean_turn:
            on_clean_turn(prose, tool_results)
        if on_stuck_state:
            on_stuck_state(stuck.to_dict())

        # ── inject into message history ───────────────────────────────────────
        messages.append({
            "role": "assistant",
            "content": prose,
            "tool_calls": [
                {
                    "id": tr["tool_call_id"],
                    "type": "function",
                    "function": {
                        "name":      tr["name"],
                        "arguments": tr.get("arguments", "{}"),
                    },
                }
                for tr in tool_results
            ],
        })
        for tr in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": tr["tool_call_id"],
                "content": tr["result"],
            })

        log.info(f"iteration {iteration}: {len(tool_results)} tool result(s), looping")

        # Inject a Working Document reminder every (keep_turns // 2) iterations
        # so the model doesn't drift without updating findings/plan.
        if working_doc_reminder_interval and iteration > 0 and                 iteration % working_doc_reminder_interval == 0:
            reminder = (
                "**Reminder:** Before continuing, call `update_document` to record "
                "any new Findings and mark completed steps in Plan. "
                "Tool results that haven't been captured there will be evicted soon."
            )
            messages.append({"role": "user", "content": reminder})
            log.info(f"iteration {iteration}: injected working-doc reminder")

    yield _make_chunk(model_name, "", done=True)
