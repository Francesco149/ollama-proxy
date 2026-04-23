import httpx
import json
import logging
import re
from typing import Callable, Awaitable, AsyncGenerator

log = logging.getLogger("auto-runner")

# All three command types use XML tags in the autorun loop.
# Tool-call protocol is dropped here: after a few injected <command-output>
# turns the model loses the tool-call framing and falls back to inventing
# its own ad-hoc text syntax. Keeping everything on one consistent XML path
# avoids that entirely.
#
# Matches the FIRST complete block in the accumulator. Checked after every
# token so the HTTP stream can be broken the instant the closing tag lands.
_RE_FIRST_CMD = re.compile(
    r'(<run-shell>(.*?)</run-shell>'
    r'|<run-python>(.*?)</run-python>'
    r'|<spawn-agent>(.*?)</spawn-agent>)',
    re.DOTALL,
)


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


async def _stream_until_event(
    openai_body: dict,
    llama_base: str,
    model_name: str,
):
    """
    Streams from llama.cpp token by token.

    Yields (chunk_str, accumulated, event_type, payload):
      event_type=None      — normal token or clean finish
      event_type="shell"   — payload: command str
      event_type="python"  — payload: code str
      event_type="agent"   — payload: {"prompt", "file_path"?, "context"?}

    For all three events the HTTP stream is broken immediately on the
    closing tag — before the model predicts any tokens after it.
    chunk_str on an event yield is the token that completed the tag.
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

                    # Ignore tool-call frames — not used in autorun mode
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

    # Clean finish — no event detected
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
    Agentic loop with mid-stream interruption.

    All three command types (<run-shell>, <run-python>, <spawn-agent>) share
    the same XML-tag path. The HTTP stream is broken the instant any closing
    tag appears in the accumulator, before the model can hallucinate output.
    Results are injected as role=user — a turn the model cannot predict.
    """
    messages = list(openai_body["messages"])
    consecutive_failures = 0

    for iteration in range(max_iterations):
        log.info(f"auto-run iteration {iteration}, messages={len(messages)}")

        # No tools/tool_choice — everything goes through XML tags
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
            preview   = (payload or "")[:80]

            log.info(f"iteration {iteration}: {tool_name}: {preview!r}")
            yield _make_chunk(model_name, "\n\n⚙️ Running…\n")

            result = await execute_tool_fn(tool_name, {arg_key: payload})

            had_failure = "exit code: `0`" not in result
            consecutive_failures = (consecutive_failures + 1) if had_failure else 0
            if not had_failure:
                consecutive_failures = 0

            if had_failure:
                log.warning(f"non-zero exit, consecutive_failures={consecutive_failures}")

            yield _make_chunk(model_name, result + "\n")

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
            preview   = prompt[:60]

            if not prompt:
                log.warning(f"iteration {iteration}: spawn-agent missing prompt, skipping")
                result = "❌ spawn-agent: missing prompt field in JSON body"
            else:
                log.info(f"iteration {iteration}: spawn-agent: {preview!r}")
                yield _make_chunk(model_name, "\n\n🤖 Spawning sub-agent…\n")
                result = await execute_tool_fn("spawn_agent", {
                    "prompt": prompt,
                    **({"file_path": file_path} if file_path else {}),
                    **({"context": context} if context else {}),
                })

            yield _make_chunk(model_name, result + "\n")
            # spawn-agent results don't count toward the failure circuit-breaker

        # ── Inject result as user message (immune to hallucination) ───────────
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
