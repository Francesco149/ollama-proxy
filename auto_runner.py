import httpx
import json
import logging
import re
from typing import Callable, Awaitable, AsyncGenerator

log = logging.getLogger("auto-runner")

_RE_FIRST_CMD = re.compile(
    r'(<run-shell>(.*?)</run-shell>'
    r'|<run-python>(.*?)</run-python>'
    r'|<spawn-agent>(.*?)</spawn-agent>)',
    re.DOTALL,
)

# Suppress forwarding to Open WebUI as soon as this prefix lands in the
# accumulator — before any of the tag name reaches the client.
_RE_TAG_START = re.compile(r'<(run-|spawn-)')

_RE_EXIT_CODE = re.compile(r'exit code: `(\d+)`')


def _make_chunk(model: str, content: str, done: bool = False) -> str:
    return json.dumps({
        "model": model,
        "message": {"role": "assistant", "content": content},
        "done": done,
    }) + "\n"


def _parse_spawn_agent(body: str) -> dict:
    try:
        return json.loads(body.strip())
    except Exception as e:
        log.warning(f"spawn-agent body parse failed: {e!r} body={body!r}")
        return {}


def _exit_code(result: str) -> int | None:
    m = _RE_EXIT_CODE.search(result)
    return int(m.group(1)) if m else None


def _format_result(header: str, body: str) -> str:
    """Plain markdown — safe to appear in model history without triggering
    pattern completion. No HTML."""
    return f"\n\n---\n**{header}**\n```\n{body.strip()}\n```\n"


def _shell_header(cmd: str, result: str) -> str:
    code = _exit_code(result)
    icon = "✓" if code == 0 else f"✗ exit {code}" if code is not None else "✗"
    preview = cmd.strip()[:80] + ("…" if len(cmd.strip()) > 80 else "")
    return f"{icon}  $ {preview}"


def _python_header(code: str, result: str) -> str:
    code_val = _exit_code(result)
    icon = "✓" if code_val == 0 else f"✗ exit {code_val}" if code_val is not None else "✗"
    first_line = code.strip().split("\n")[0][:80]
    return f"{icon}  🐍 {first_line}"


def _agent_header(prompt: str, file_path: str | None) -> str:
    preview = prompt[:80] + ("…" if len(prompt) > 80 else "")
    suffix = f" — {file_path}" if file_path else ""
    return f"🤖  {preview}{suffix}"


async def _stream_until_event(openai_body: dict, llama_base: str, model_name: str):
    """
    Streams from llama.cpp token by token.

    Yields (chunk_str, accumulated, forward, event_type, payload):
      forward=True   — token should be forwarded to Open WebUI
      forward=False  — suppress (tag content in progress)
      event_type: None | "shell" | "python" | "agent"

    HTTP stream is broken the instant any closing tag lands.
    """
    accumulated = ""
    suppress = False

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
                    if content:
                        accumulated += content
                        chunk_out = _make_chunk(model_name, content)

                        if not suppress and _RE_TAG_START.search(accumulated):
                            suppress = True

                        m = _RE_FIRST_CMD.search(accumulated)
                        if m:
                            if m.group(2) is not None:
                                yield chunk_out, accumulated, False, "shell", m.group(2).strip()
                            elif m.group(3) is not None:
                                yield chunk_out, accumulated, False, "python", m.group(3).strip()
                            else:
                                yield chunk_out, accumulated, False, "agent", _parse_spawn_agent(m.group(4))
                            return

                        yield chunk_out, accumulated, not suppress, None, None

                        if finish in ("stop", "length"):
                            break

                except Exception as e:
                    log.warning(f"chunk parse error: {e}")
                    continue

    yield "", accumulated, False, None, None


async def run_agentic_chat(
    openai_body: dict,
    model_name: str,
    llama_base: str,
    execute_tool_fn: Callable[[str, dict], Awaitable[str]],
    max_iterations: int = 10,
    max_consecutive_failures: int = 3,
    on_clean_turn: Callable[[str, str | None], None] | None = None,
):
    """
    Agentic loop with mid-stream interruption and shadow context support.

    on_clean_turn(assistant_content, command_output_or_None):
      Called after each iteration with clean (no HTML) text so the caller
      can persist a context that is safe to replay to llama.cpp later.
      - command_output is the plain result string, or None on clean finish.
      The caller is responsible for storage; auto_runner is stateless.
    """
    messages = list(openai_body["messages"])
    consecutive_failures = 0

    for iteration in range(max_iterations):
        log.info(f"auto-run iteration {iteration}, messages={len(messages)}")

        body = {**openai_body, "messages": messages, "stream": True}
        body.pop("tools", None)
        body.pop("tool_choice", None)

        assistant_content = ""
        event_type: str | None = None
        payload = None

        async for chunk_out, accumulated, forward, ev, pload in _stream_until_event(
            body, llama_base, model_name
        ):
            assistant_content = accumulated

            if ev is not None:
                event_type = ev
                payload = pload
                break

            if forward and chunk_out:
                parsed = json.loads(chunk_out)
                if parsed.get("message", {}).get("content"):
                    yield chunk_out

        # ── Clean finish ──────────────────────────────────────────────────────
        if event_type is None:
            log.info(f"iteration {iteration}: clean finish")
            if on_clean_turn:
                on_clean_turn(assistant_content, None)
            yield _make_chunk(model_name, "", done=True)
            return

        # ── Shell / Python ────────────────────────────────────────────────────
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

            header = (_shell_header if event_type == "shell" else _python_header)(payload or "", result)
            yield _make_chunk(model_name, _format_result(header, result))

            if consecutive_failures >= max_consecutive_failures:
                log.warning(f"circuit-breaker fired at {consecutive_failures} failures")
                yield _make_chunk(
                    model_name,
                    f"\n⚠️ Auto-run stopped after {consecutive_failures} consecutive "
                    "failure(s). Type `.run` to retry manually.\n",
                )
                yield _make_chunk(model_name, "", done=True)
                return

        # ── Spawn-agent ───────────────────────────────────────────────────────
        elif event_type == "agent":
            prompt    = payload.get("prompt", "")
            file_path = payload.get("file_path")
            context   = payload.get("context", "")

            if not prompt:
                log.warning(f"iteration {iteration}: spawn-agent missing prompt")
                result = "❌ spawn-agent: missing prompt field in JSON body"
                yield _make_chunk(model_name, f"\n{result}\n")
            else:
                log.info(f"iteration {iteration}: spawn-agent: {prompt[:60]!r}")
                result = await execute_tool_fn("spawn_agent", {
                    "prompt": prompt,
                    **( {"file_path": file_path} if file_path else {}),
                    **( {"context": context}     if context   else {}),
                })
                yield _make_chunk(model_name, _format_result(_agent_header(prompt, file_path), result))

        # ── Persist clean turn (no HTML, no display artifacts) ────────────────
        if on_clean_turn:
            on_clean_turn(assistant_content, result)

        # ── Inject plain text into internal loop context ──────────────────────
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({
            "role": "user",
            "content": (
                f"<command-output>\n{result}\n</command-output>\n\n"
                "Continue based on the above output."
            ),
        })
        log.info(f"iteration {iteration}: result injected, looping")

    yield _make_chunk(model_name, "", done=True)
