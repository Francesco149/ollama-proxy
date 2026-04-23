import httpx
import json
import logging
import re
from typing import Callable, Awaitable, AsyncGenerator

from tool_manager import TOOLS

log = logging.getLogger("auto-runner")

# Only expose spawn_agent in the autorun loop.
# run_shell / run_python come in via XML tags; mixing both mechanisms
# for the same tools causes model confusion.
_AUTORUN_TOOLS = [t for t in TOOLS if t["function"]["name"] == "spawn_agent"]

# Matches the FIRST complete command block in the accumulator.
# Checked incrementally so we can break the stream the moment the
# closing tag arrives — before the model predicts anything after it.
_RE_FIRST_CMD = re.compile(
    r'(<run-shell>(.*?)</run-shell>|<run-python>(.*?)</run-python>)',
    re.DOTALL,
)


def _make_chunk(model: str, content: str, done: bool = False) -> str:
    return json.dumps({
        "model": model,
        "message": {"role": "assistant", "content": content},
        "done": done,
    }) + "\n"


async def _stream_until_event(
    openai_body: dict,
    llama_base: str,
    model_name: str,
):
    """
    Streams from llama.cpp token by token.

    Yields tuples: (chunk_str, accumulated, event_type, payload)

    event_type is None for normal tokens and on clean finish.
    On detection it is one of:
      "shell"     — payload is the command string
      "python"    — payload is the code string
      "tool_call" — payload is {"name": str, "args": dict}

    For "shell" / "python": the HTTP stream is broken immediately on
    the closing tag, before the model generates any further tokens.

    For "tool_call": the stream runs to finish_reason=="tool_calls";
    no content tokens are forwarded during accumulation.
    """
    accumulated = ""
    tool_name: str | None = None
    tool_arg_buf = ""
    in_tool_call = False

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

                    # ── tool-call accumulation ────────────────────────────────
                    if delta.get("tool_calls"):
                        in_tool_call = True
                        tc = delta["tool_calls"][0]
                        if tc.get("function", {}).get("name"):
                            tool_name = tc["function"]["name"]
                        tool_arg_buf += tc.get("function", {}).get("arguments", "")
                        continue

                    if finish == "tool_calls" or (in_tool_call and finish == "stop"):
                        try:
                            args = json.loads(tool_arg_buf)
                        except Exception as e:
                            log.warning(f"tool arg parse failed: {e} buf={tool_arg_buf!r}")
                            args = {}
                        log.info(f"tool_call detected: {tool_name} args={args}")
                        yield "", accumulated, "tool_call", {"name": tool_name or "", "args": args}
                        return

                    # ── normal content token ──────────────────────────────────
                    content = delta.get("content", "")
                    if content:
                        accumulated += content
                        chunk_out = _make_chunk(model_name, content)

                        m = _RE_FIRST_CMD.search(accumulated)
                        if m:
                            # Break stream immediately on closing tag
                            if m.group(2) is not None:
                                yield chunk_out, accumulated, "shell", m.group(2).strip()
                            else:
                                yield chunk_out, accumulated, "python", m.group(3).strip()
                            return

                        yield chunk_out, accumulated, None, None

                        if finish in ("stop", "length"):
                            break

                except Exception as e:
                    log.warning(f"chunk parse error: {e}")
                    continue

    # Clean finish — no command or tool call
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

    Anti-hallucination design:
    - For XML command blocks: stream is broken the instant the closing tag
      lands. The model never gets to predict tokens after it.
    - For tool calls (spawn_agent): standard finish_reason protocol; no
      content to hallucinate since the model emits no text during tool use.
    - All results injected as role=user (XML cmds) or role=tool (tool calls).
      The model has no learned pattern for predicting these turns.
    """
    messages = list(openai_body["messages"])
    consecutive_failures = 0

    for iteration in range(max_iterations):
        log.info(f"auto-run iteration {iteration}, messages={len(messages)}")

        body = {
            **openai_body,
            "messages": messages,
            "stream": True,
            "tools": _AUTORUN_TOOLS,
            "tool_choice": "auto",
        }

        assistant_content = ""
        event_type: str | None = None
        payload = None

        async for chunk_out, accumulated, ev, pload in _stream_until_event(
            body, llama_base, model_name
        ):
            assistant_content = accumulated

            if ev is not None:
                # Forward the completing chunk for xml events (has content);
                # tool_call events emit an empty string chunk — skip it.
                if chunk_out:
                    yield chunk_out
                event_type = ev
                payload = pload
                break

            if chunk_out:
                parsed = json.loads(chunk_out)
                if parsed.get("message", {}).get("content"):
                    yield chunk_out

        # ── Clean finish — no command or tool call ────────────────────────────
        if event_type is None:
            log.info(f"iteration {iteration}: clean finish, done")
            yield _make_chunk(model_name, "", done=True)
            return

        # ── XML command block ─────────────────────────────────────────────────
        if event_type in ("shell", "python"):
            tool_name = "run_shell" if event_type == "shell" else "run_python"
            arg_key   = "command"   if event_type == "shell" else "code"
            preview   = (payload or "")[:80]

            log.info(f"iteration {iteration}: {tool_name}: {preview!r}")
            yield _make_chunk(model_name, "\n\n⚙️ Running…\n")

            result = await execute_tool_fn(tool_name, {arg_key: payload})

            had_failure = "exit code: `0`" not in result
            consecutive_failures = (consecutive_failures + 1) if had_failure else 0
            if had_failure:
                log.warning(f"non-zero exit, consecutive_failures={consecutive_failures}")
            else:
                consecutive_failures = 0

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

            # Inject as user — immune to hallucination (model can't predict user turn)
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({
                "role": "user",
                "content": (
                    f"<command-output>\n{result}\n</command-output>\n\n"
                    "Continue based on the above output."
                ),
            })
            log.info(f"iteration {iteration}: result injected as user message, looping")

        # ── Tool call (spawn_agent) ────────────────────────────────────────────
        elif event_type == "tool_call":
            name = payload["name"]
            args = payload["args"]

            log.info(f"iteration {iteration}: tool_call {name}")
            yield _make_chunk(model_name, f"\n\n🤖 Spawning sub-agent…\n")

            result = await execute_tool_fn(name, args)

            yield _make_chunk(model_name, result + "\n")

            # role=tool is the correct multi-turn format for tool results and
            # equally immune to hallucination — no training signal for it.
            messages.append({
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": [{
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": "call_0",
                "content": result,
            })
            log.info(f"iteration {iteration}: tool result injected as tool message, looping")

    yield _make_chunk(model_name, "", done=True)
