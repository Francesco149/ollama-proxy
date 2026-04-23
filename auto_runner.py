import json
import logging
from typing import Callable, Awaitable, AsyncGenerator

from tool_manager import RE_SHELL, RE_PYTHON
from stream_handler import generate_streaming_chat

log = logging.getLogger("auto-runner")


def _make_chunk(model: str, content: str, done: bool = False) -> str:
    return json.dumps({
        "model": model,
        "message": {"role": "assistant", "content": content},
        "done": done,
    }) + "\n"


async def run_agentic_chat(
    openai_body: dict,
    model_name: str,
    llama_base: str,
    execute_tool_fn: Callable[[str, dict], Awaitable[str]],
    max_iterations: int = 10,
    max_consecutive_failures: int = 3,
) -> AsyncGenerator[str, None]:
    """
    Wraps generate_streaming_chat in an agentic loop:
    - Streams each LLM response live to the caller
    - Detects <run-shell> / <run-python> blocks in accumulated output
    - Executes them automatically, yields visual progress to the caller
    - Injects results into message history and loops
    - Stops when no commands are found, max_iterations is reached,
      or max_consecutive_failures non-zero-exit commands fire in a row
    """
    messages = list(openai_body["messages"])
    consecutive_failures = 0

    for iteration in range(max_iterations):
        log.info(f"auto-run iteration {iteration} start")
        body = {**openai_body, "messages": messages}
        full_content = ""
        done_chunk: str | None = None

        # Stream this LLM call live; hold the final done chunk
        async for chunk_str in generate_streaming_chat(
            body, model_name, llama_base, execute_tool_fn
        ):
            try:
                chunk = json.loads(chunk_str)
            except Exception:
                yield chunk_str
                continue

            if chunk.get("done"):
                done_chunk = chunk_str
                continue

            full_content += chunk.get("message", {}).get("content", "")
            yield chunk_str

        shell_cmds = [c.strip() for c in RE_SHELL.findall(full_content)]
        py_cmds = [c.strip() for c in RE_PYTHON.findall(full_content)]

        if not shell_cmds and not py_cmds:
            log.info(f"iteration {iteration}: no commands found, stopping loop")
            yield done_chunk or _make_chunk(model_name, "", done=True)
            return

        total = len(shell_cmds) + len(py_cmds)
        log.info(f"iteration {iteration}: found {total} command block(s)")
        yield _make_chunk(model_name, f"\n\n---\n⚙️ Auto-running {total} command(s)…\n")

        results: list[str] = []
        had_failure = False

        for cmd in shell_cmds:
            preview = cmd[:80] + ("…" if len(cmd) > 80 else "")
            yield _make_chunk(model_name, f"\n```\n$ {preview}\n```\n")
            result = await execute_tool_fn("run_shell", {"command": cmd})
            results.append(result)
            if "exit code: `0`" not in result:
                had_failure = True
                log.warning(f"shell command returned non-zero: {preview}")
            yield _make_chunk(model_name, result + "\n")

        for code in py_cmds:
            first_line = code.split("\n")[0][:60]
            yield _make_chunk(model_name, f"\n```python\n# {first_line}\n```\n")
            result = await execute_tool_fn("run_python", {"code": code})
            results.append(result)
            if "exit code: `0`" not in result:
                had_failure = True
                log.warning(f"python block returned non-zero: {first_line}")
            yield _make_chunk(model_name, result + "\n")

        consecutive_failures = (consecutive_failures + 1) if had_failure else 0
        log.info(
            f"iteration {iteration}: had_failure={had_failure} "
            f"consecutive_failures={consecutive_failures}"
        )

        if consecutive_failures >= max_consecutive_failures:
            log.warning(
                f"circuit-breaker triggered after {consecutive_failures} "
                "consecutive failing iterations"
            )
            yield _make_chunk(
                model_name,
                f"\n⚠️ Auto-run stopped: {consecutive_failures} consecutive "
                "failure(s). Type `.run` to retry manually.\n",
            )
            break

        # Build injected context for next iteration.
        # These messages exist only in the proxy loop — Open WebUI never
        # sees them as separate bubbles; it only records the streamed
        # assistant content above.
        result_block = "\n\n---\n\n".join(results)
        injected_user = (
            f"<auto-run-results>\n{result_block}\n</auto-run-results>\n\n"
            "Continue your analysis based on the above command output."
        )
        messages.append({"role": "assistant", "content": full_content})
        messages.append({"role": "user", "content": injected_user})
        log.info(f"injected results into context, proceeding to iteration {iteration + 1}")

    yield _make_chunk(model_name, "", done=True)
