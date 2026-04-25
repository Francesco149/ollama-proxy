"""
auto_runner.py

Agentic loop with mid-stream tag suppression, collapsible result blocks,
and shadow context support via on_clean_turn callback.

Tag suppression: the tail of the accumulator is scanned after every token.
Characters are held back whenever the tail is a prefix of a known command
tag — so no part of any tag name ever reaches Open WebUI.  The hold
distance is computed by _tag_prefix_hold().

Collapsibles: safe now that shadow context ensures the model's history
never contains <details> markup.

All five tag types share one detection path: <run-shell>, <run-python>,
<spawn-agent>, <write-file>.
"""

import httpx
import json
import logging
import re
from typing import Callable, Awaitable, AsyncGenerator

log = logging.getLogger("auto-runner")

# ── tag detection ─────────────────────────────────────────────────────────────

_TAG_PATTERNS = [
    "<run-shell>",
    "<run-python>",
    "<spawn-agent>",
    "<write-file>",
]
_MAX_TAG_LEN = max(len(p) for p in _TAG_PATTERNS)

_RE_FIRST_CMD = re.compile(
    r'(<run-shell>(.*?)</run-shell>'
    r'|<run-python>(.*?)</run-python>'
    r'|<spawn-agent>(.*?)</spawn-agent>'
    r'|<write-file>(.*?)</write-file>)',
    re.DOTALL,
)

_RE_EXIT_CODE = re.compile(r'exit code: `(\d+)`')


def _tag_prefix_hold(text: str) -> int:
    """
    Return how many characters at the END of text must be withheld because
    they could be the opening of a command tag.

    Works by finding the rightmost '<' in the last _MAX_TAG_LEN characters
    and checking whether everything from that '<' to the end of text is a
    prefix of any known tag pattern.

    Examples:
      "hello <"        → 1   (could start <run-shell>)
      "hello <run"     → 4   (<run is prefix of <run-shell>/<run-python>)
      "hello <run-shell>cmd" → 0  (_RE_FIRST_CMD catches this before us)
      "hello <z"       → 0   (not a prefix of any tag)
    """
    search_from = max(0, len(text) - _MAX_TAG_LEN)
    tail = text[search_from:]
    lt = tail.rfind("<")
    if lt == -1:
        return 0
    potential = tail[lt:]  # from '<' to end of tail
    for pat in _TAG_PATTERNS:
        if pat.startswith(potential):
            # hold from the '<' to end of text
            return len(text) - (search_from + lt)
    return 0


# ── chunk helpers ─────────────────────────────────────────────────────────────

def _make_chunk(model: str, content: str, done: bool = False) -> str:
    return json.dumps({
        "model": model,
        "message": {"role": "assistant", "content": content},
        "done": done,
    }) + "\n"


def _parse_json_tag(body: str, tag: str) -> dict:
    try:
        return json.loads(body.strip())
    except Exception as e:
        log.warning(f"<{tag}> body parse failed: {e!r}")
        return {}


def _exit_code(result: str) -> int | None:
    m = _RE_EXIT_CODE.search(result)
    return int(m.group(1)) if m else None


# ── display helpers ───────────────────────────────────────────────────────────

def _collapsible(summary: str, body: str) -> str:
    """
    Collapsed <details> block. Safe now that shadow context guarantees the
    model never sees this markup in its own history.
    Body is passed pre-formatted (fenced or plain markdown).
    """
    return f"\n<details>\n<summary>{summary}</summary>\n\n{body}\n</details>\n"


def _fenced(text: str) -> str:
    """6-backtick fence — survives any nested triple/quintuple fences."""
    return f"``````\n{text.strip()}\n``````"


def _shell_summary(cmd: str, result: str) -> str:
    code = _exit_code(result)
    icon = "✓" if code == 0 else f"✗ exit {code}" if code is not None else "✗"
    preview = cmd.strip()[:80] + ("…" if len(cmd.strip()) > 80 else "")
    return f"{icon}  $ {preview}"


def _python_summary(code: str, result: str) -> str:
    val = _exit_code(result)
    icon = "✓" if val == 0 else f"✗ exit {val}" if val is not None else "✗"
    first = code.strip().split("\n")[0][:80]
    return f"{icon}  🐍 {first}"


def _agent_summary(prompt: str, file_path: str | None, files: list | None = None) -> str:
    preview = prompt[:80] + ("…" if len(prompt) > 80 else "")
    all_files = list(files or [])
    if file_path and file_path not in all_files:
        all_files.insert(0, file_path)
    if all_files:
        names = ", ".join(f.split("/")[-1] for f in all_files[:3])
        if len(all_files) > 3:
            names += f" +{len(all_files)-3}"
        suffix = f"  —  {names}"
    else:
        suffix = ""
    return f"🤖  {preview}{suffix}"


def _write_summary(path: str, lines: int) -> str:
    return f"📝  {path}  ({lines} lines written)"


def _agent_body(prompt: str, file_path: str | None, files: list | None,
                context: str, result: str) -> str:
    parts = []
    all_files = list(files or [])
    if file_path and file_path not in all_files:
        all_files.insert(0, file_path)
    if all_files:
        file_list = "\n".join(f"- `{f}`" for f in all_files)
        parts.append(f"**Files:**\n{file_list}")
    if context:
        parts.append(f"**Context:** {context}")
    parts.append(f"**Prompt:** {prompt}")
    parts.append("---")
    parts.append(result)
    return "\n\n".join(parts)


def _write_body(path: str, prompt: str, lines: int, preview: str) -> str:
    preview_block = _fenced(preview)
    return (
        f"**Path:** `{path}`\n\n"
        f"**Prompt:** {prompt}\n\n"
        f"**Preview ({lines} lines):**\n\n{preview_block}"
    )


def _shell_body(cmd: str, result: str) -> str:
    return f"**Command:**\n\n{_fenced(cmd)}\n\n**Output:**\n\n{_fenced(result)}"


def _python_body(code: str, result: str) -> str:
    return f"**Code:**\n\n{_fenced(code)}\n\n**Output:**\n\n{_fenced(result)}"


# ── streaming ─────────────────────────────────────────────────────────────────

async def _stream_until_event(openai_body: dict, llama_base: str, model_name: str):
    """
    Streams from llama.cpp, withholding any characters that could be the
    start of a command tag. Yields text pieces incrementally; on detecting
    a complete command block, yields one event tuple and stops.

    Yields: (event_type, payload, text)
      - Normal:  (None, None, text_to_forward)   — incremental safe text
      - Event:   (type_str, payload, prose_so_far) — command detected
      - Sentinel: (None, None, "")                — clean finish
    """
    accumulated = ""
    forwarded_up_to = 0
    # Position in accumulated where suppression began (start of the `<`
    # that opened a potential tag). Once set, forwarded_up_to never
    # advances past this point — so no part of the tag or its content
    # ever reaches Open WebUI, even when _tag_prefix_hold() returns 0
    # for the tag body (e.g. JSON that contains no `<`).
    suppress_from: int | None = None

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
                    delta = choice["delta"]
                    finish = choice.get("finish_reason")

                    if delta.get("tool_calls"):
                        continue

                    content = delta.get("content", "")
                    if not content:
                        if finish in ("stop", "length"):
                            break
                        continue

                    accumulated += content

                    # Check for a complete command block first
                    m = _RE_FIRST_CMD.search(accumulated)
                    if m:
                        # Flush prose up to where suppression began (or tag
                        # start if we never had a partial hold)
                        prose_end = suppress_from if suppress_from is not None else m.start()
                        prose_end = min(prose_end, m.start())
                        if prose_end > forwarded_up_to:
                            yield None, None, accumulated[forwarded_up_to:prose_end]
                        prose = accumulated[:m.start()]
                        g = m.groups()
                        if g[1] is not None:
                            yield "shell", g[1].strip(), prose
                        elif g[2] is not None:
                            yield "python", g[2].strip(), prose
                        elif g[3] is not None:
                            yield "agent", _parse_json_tag(g[3], "spawn-agent"), prose
                        else:
                            yield "write", _parse_json_tag(g[4], "write-file"), prose
                        return  # break HTTP stream immediately

                    # No complete command yet.
                    # Compute how many tail chars to hold back for a potential tag.
                    hold = _tag_prefix_hold(accumulated)
                    if hold > 0 and suppress_from is None:
                        # Record where clean prose ends — nothing after this
                        # point will ever be forwarded while suppressing.
                        suppress_from = len(accumulated) - hold

                    # Only advance forwarded_up_to up to the suppression point
                    # (or the hold boundary if we haven't started suppressing).
                    if suppress_from is not None:
                        safe_end = suppress_from
                    else:
                        safe_end = len(accumulated) - hold

                    if safe_end > forwarded_up_to:
                        chunk_text = accumulated[forwarded_up_to:safe_end]
                        if chunk_text:
                            yield None, None, chunk_text
                        forwarded_up_to = safe_end

                    if finish in ("stop", "length"):
                        break

                except Exception as e:
                    log.warning(f"chunk parse error: {e}")
                    continue

    # Clean finish — flush any held-back tail (safe because no complete tag found)
    remainder = accumulated[forwarded_up_to:]
    if remainder:
        yield None, None, remainder
    yield None, None, ""  # sentinel


# ── main loop ─────────────────────────────────────────────────────────────────

async def run_agentic_chat(
    openai_body: dict,
    model_name: str,
    llama_base: str,
    execute_tool_fn: Callable[[str, dict], Awaitable[str]],
    max_iterations: int = 10,
    max_consecutive_failures: int = 3,
    on_clean_turn: Callable[[str, str | None], None] | None = None,
) -> AsyncGenerator[str, None]:
    """
    Agentic loop with mid-stream interruption and collapsible result blocks.

    on_clean_turn(assistant_prose, command_result_or_None):
      Called after each iteration so the caller can persist a clean
      shadow context (no display markup). auto_runner is stateless.
    """
    messages = list(openai_body["messages"])
    consecutive_failures = 0

    for iteration in range(max_iterations):
        log.info(f"auto-run iteration {iteration}, messages={len(messages)}")

        body = {**openai_body, "messages": messages, "stream": True}
        body.pop("tools", None)
        body.pop("tool_choice", None)

        prose_content = ""
        event_type: str | None = None
        payload = None
        result: str = ""

        async for ev_type, ev_payload, text in _stream_until_event(
            body, llama_base, model_name
        ):
            if ev_type is not None:
                event_type = ev_type
                payload = ev_payload
                prose_content = text
                break
            if text:
                prose_content = (prose_content or "") + text
                yield _make_chunk(model_name, text)

        # ── clean finish ──────────────────────────────────────────────────────
        if event_type is None:
            log.info(f"iteration {iteration}: clean finish")
            if on_clean_turn:
                on_clean_turn(prose_content, None)
            yield _make_chunk(model_name, "", done=True)
            return

        # ── shell / python ────────────────────────────────────────────────────
        if event_type in ("shell", "python"):
            tool_name = "run_shell" if event_type == "shell" else "run_python"
            arg_key   = "command"   if event_type == "shell" else "code"
            log.info(f"iteration {iteration}: {tool_name}: {(payload or '')[:72]!r}")

            result = await execute_tool_fn(tool_name, {arg_key: payload})

            had_failure = (_exit_code(result) or 0) != 0
            consecutive_failures = (consecutive_failures + 1) if had_failure else 0
            if not had_failure:
                consecutive_failures = 0
            if had_failure:
                log.warning(f"non-zero exit, consecutive_failures={consecutive_failures}")

            fn_sum  = _shell_summary  if event_type == "shell" else _python_summary
            fn_body = _shell_body     if event_type == "shell" else _python_body
            block = _collapsible(fn_sum(payload or "", result),
                                 fn_body(payload or "", result))
            yield _make_chunk(model_name, block)

            if consecutive_failures >= max_consecutive_failures:
                log.warning(f"circuit-breaker fired at {consecutive_failures} failures")
                yield _make_chunk(
                    model_name,
                    f"\n⚠️ Auto-run stopped after {consecutive_failures} consecutive "
                    "failure(s). Type `.run` to retry manually.\n",
                )
                yield _make_chunk(model_name, "", done=True)
                return

        # ── spawn-agent ───────────────────────────────────────────────────────
        elif event_type == "agent":
            prompt    = payload.get("prompt", "")
            file_path = payload.get("file_path")
            context   = payload.get("context", "")

            if not prompt:
                result = "❌ spawn-agent: missing prompt"
                log.warning(f"iteration {iteration}: spawn-agent missing prompt")
                yield _make_chunk(model_name, f"\n{result}\n")
            else:
                files = payload.get("files", [])
                n_files = len(files) + bool(file_path)
                log.info(f"iteration {iteration}: spawn-agent: {prompt[:60]!r} files={n_files}")
                result = await execute_tool_fn("spawn_agent", {
                    "prompt": prompt,
                    **( {"file_path": file_path} if file_path else {}),
                    **( {"files": files}         if files      else {}),
                    **( {"context": context}     if context    else {}),
                })
                block = _collapsible(
                    _agent_summary(prompt, file_path, files),
                    _agent_body(prompt, file_path, files, context, result),
                )
                yield _make_chunk(model_name, block)

        # ── write-file ────────────────────────────────────────────────────────
        elif event_type == "write":
            path   = payload.get("path", "")
            prompt = payload.get("prompt", "")

            if not path or not prompt:
                result = "❌ write-file: requires 'path' and 'prompt' fields"
                log.warning(f"iteration {iteration}: write-file missing fields")
                yield _make_chunk(model_name, f"\n{result}\n")
            else:
                log.info(f"iteration {iteration}: write-file: {path!r}")
                result = await execute_tool_fn("write_file", {
                    "path": path,
                    "prompt": prompt,
                })
                # Extract line count and preview from result for display
                # result is a status string; full details in collapsible
                lines_m = re.search(r'\((\d+) lines\)', result)
                n_lines = int(lines_m.group(1)) if lines_m else 0
                preview_m = re.search(r'```\n(.*?)```', result, re.DOTALL)
                preview = preview_m.group(1) if preview_m else ""
                block = _collapsible(
                    _write_summary(path, n_lines),
                    _write_body(path, prompt, n_lines, preview),
                )
                yield _make_chunk(model_name, block)

        # ── persist clean turn (no display markup) ────────────────────────────
        if on_clean_turn:
            on_clean_turn(prose_content, result)

        # ── inject plain-text result into internal loop context ───────────────
        messages.append({"role": "assistant", "content": prose_content})
        messages.append({
            "role": "user",
            "content": (
                f"<command-output>\n{result}\n</command-output>\n\n"
                "Continue based on the above output."
            ),
        })
        log.info(f"iteration {iteration}: injected, looping")

    yield _make_chunk(model_name, "", done=True)
