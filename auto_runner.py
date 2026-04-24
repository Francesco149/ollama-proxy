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

# Extracts exit code from tool_manager's formatted result string
_RE_EXIT_CODE = re.compile(r'exit code: `(\d+)`')


def _make_chunk(model: str, content: str, done: bool = False) -> str:
    return json.dumps({
        "model": model,
        "message": {"role": "assistant", "content": content},
        "done": done,
    }) + "\n"


def _parse_spawn_agent(body: str) -> dict:
    """Parse the JSON body of a <spawn-agent> block. Returns {} on failure."""
    try:
        return json.loads(body.strip())
    except Exception as e:
        log.warning(f"spawn-agent body parse failed: {e!r} body={body!r}")
        return {}


def _exit_code(result: str) -> int | None:
    """Extract numeric exit code from a tool_manager result string, or None."""
    m = _RE_EXIT_CODE.search(result)
    return int(m.group(1)) if m else None


def _collapsible(summary: str, body: str) -> str:
    """
    Wrap body in a collapsed <details> block.

    Yielded to Open WebUI as a single chunk after execution — never
    split across multiple yields so the HTML is always well-formed.

    The raw result (without this wrapper) is what gets injected into
    the LLM context so no HTML leaks into the model's reasoning.
    """
    return f"\n<details>\n<summary>{summary}</summary>\n\n{body}\n</details>\n"


def _shell_summary(cmd: str, result: str) -> str:
    code = _exit_code(result)
    status = "✓" if code == 0 else f"✗ exit {code}" if code is not None else "✗"
    preview = cmd[:72] + ("…" if len(cmd) > 72 else "")
    return f"<code>$ {preview}</code> &nbsp; {status}"


def _python_summary(code: str, result: str) -> str:
    code_val = _exit_code(result)
    status = "✓" if code_val == 0 else f"✗ exit {code_val}" if code_val is not None else "✗"
    first_line = code.strip().split("\n")[0][:72]
    return f"🐍 <code>{first_line}</code> &nbsp; {status}"


def _agent_summary(prompt: str, file_path: str | None) -> str:
    preview = prompt[:80] + ("…" if len(prompt) > 80 else "")
    suffix = f" — <code>{file_path}</code>" if file_path else ""
    return f"🤖 {preview}{suffix}"


async def _stream_until_event(openai_body: dict, llama_base: str, model_name: str):
    """
    Streams from llama.cpp token by token.

    Yields (chunk_str, accumulated, event_type, payload):
      event_type=None    — normal token or clean finish
      event_type="shell" — payload: command str
      event_type="python"— payload: code str
      event_type="agent" — payload: {"prompt", "file_path"?, "context"?}

    HTTP stream is broken the instant any closing tag lands in the
    accumulator — before the model predicts any tokens after it.
    """
    accumulated = ""

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

                        m = _RE_FIRST_CMD.search(accumulated)
                        if m:
                            if m.group(2) is not None:
                                yield chunk_out, accumulated, "shell", m.group(2).strip()
                            elif m.group(3) is not None:
                                yield chunk_out, accumulated, "python", m.group(3).strip()
                            else:
                                yield chunk_out, accumulated, "agent", _parse_spawn_agent(m.group(4))
                            return  # break HTTP stream immediately

                        yield chunk_out, accumulated, None, None

                        if finish in ("stop", "length"):
                            break

                except Exception as e:
                    log.warning(f"chunk parse error: {e}")
                    continue

    yield "", accumulated, None, None


async def run_agentic_chat(
    openai_body: dict,
    model_name: str,
    llama_base: str,
    execute_tool_fn: Callable[[str, dict], Awaitable[str]],
    max_iterations: int = 10,
    max_consecutive_failures: int = 3,
):
    """
    Agentic loop with mid-stream interruption and collapsible result blocks.

    Anti-hallucination:
    - HTTP stream broken on closing tag before model predicts after it
    - Results injected as role=user (plain text, no HTML)

    Display:
    - Each result rendered as a <details> block collapsed by default
    - Summary line shows command preview + pass/fail at a glance
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

        async for chunk_out, accumulated, ev, pload in _stream_until_event(
            body, llama_base, model_name
        ):
            assistant_content = accumulated

            if ev is not None:
                if chunk_out:
                    yield chunk_out
                event_type = ev
                payload = pload
                break

            if chunk_out:
                parsed = json.loads(chunk_out)
                if parsed.get("message", {}).get("content"):
                    yield chunk_out

        # ── Clean finish ──────────────────────────────────────────────────────
        if event_type is None:
            log.info(f"iteration {iteration}: clean finish")
            yield _make_chunk(model_name, "", done=True)
            return

        # ── Shell / Python ────────────────────────────────────────────────────
        if event_type in ("shell", "python"):
            tool_name = "run_shell" if event_type == "shell" else "run_python"
            arg_key   = "command"   if event_type == "shell" else "code"
            preview   = (payload or "")[:72]

            log.info(f"iteration {iteration}: {tool_name}: {preview!r}")

            result = await execute_tool_fn(tool_name, {arg_key: payload})

            had_failure = (_exit_code(result) or 0) != 0
            consecutive_failures = (consecutive_failures + 1) if had_failure else 0
            if not had_failure:
                consecutive_failures = 0

            if had_failure:
                log.warning(f"non-zero exit, consecutive_failures={consecutive_failures}")

            if event_type == "shell":
                summary = _shell_summary(payload or "", result)
            else:
                summary = _python_summary(payload or "", result)

            yield _make_chunk(model_name, _collapsible(summary, result))

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
                summary = _agent_summary(prompt, file_path)
                yield _make_chunk(model_name, _collapsible(summary, result))

        # ── Inject plain-text result into context (no HTML) ───────────────────
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({
            "role": "user",
            "content": (
                f"<command-output>\n{result}\n</command-output>\n\n"
                "Continue based on the above output."
            ),
        })
        log.info(f"iteration {iteration}: result injected as user message, looping")

    yield _make_chunk(model_name, "", done=True)
