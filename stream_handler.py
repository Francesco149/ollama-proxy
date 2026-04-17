import httpx
import json
import logging
from typing import Callable, Awaitable, AsyncGenerator
from fastapi.responses import JSONResponse

log = logging.getLogger("stream-handler")


async def handle_non_streaming_chat(
    openai_body: dict,
    model_name: str,
    llama_base: str,
    execute_tool_fn: Callable[[str, dict], Awaitable[str]],
) -> JSONResponse:
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(f"{llama_base}/v1/chat/completions", json=openai_body)
        data = resp.json()

    msg = data["choices"][0]["message"]

    if msg.get("tool_calls"):
        tool_results = []
        for tc in msg["tool_calls"]:
            args = json.loads(tc["function"]["arguments"])
            result = await execute_tool_fn(tc["function"]["name"], args)
            tool_results.append(result)
        return JSONResponse({
            "model": model_name,
            "message": {"role": "assistant", "content": "\n".join(tool_results)},
            "done": True,
        })

    return JSONResponse({
        "model": model_name,
        "message": {"role": "assistant", "content": msg.get("content", "")},
        "done": True,
    })


async def generate_streaming_chat(
    openai_body: dict,
    model_name: str,
    llama_base: str,
    execute_tool_fn: Callable[[str, dict], Awaitable[str]],
) -> AsyncGenerator[str, None]:
    tool_call_buffer = ""
    tool_name = None
    in_tool_call = False

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream(
            "POST", f"{llama_base}/v1/chat/completions", json=openai_body
        ) as resp:
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"]
                    finish = chunk["choices"][0].get("finish_reason")

                    if delta.get("tool_calls"):
                        in_tool_call = True
                        tc = delta["tool_calls"][0]
                        if tc.get("function", {}).get("name"):
                            tool_name = tc["function"]["name"]
                        tool_call_buffer += tc.get("function", {}).get("arguments", "")
                        continue

                    if finish == "tool_calls" or (in_tool_call and finish == "stop"):
                        log.info(f"tool_name={tool_name} buffer={tool_call_buffer!r}")
                        try:
                            args = json.loads(tool_call_buffer)
                        except Exception as e:
                            log.warning(f"tool args json parse failed: {e}")
                            args = {}

                        yield json.dumps({
                            "model": model_name,
                            "message": {"role": "assistant", "content": "⏳ Processing..."},
                            "done": False,
                        }) + "\n"

                        result = await execute_tool_fn(tool_name or "", args)

                        yield json.dumps({
                            "model": model_name,
                            "message": {"role": "assistant", "content": result},
                            "done": False,
                        }) + "\n"
                        break

                    content = delta.get("content", "")
                    if content:
                        yield json.dumps({
                            "model": model_name,
                            "message": {"role": "assistant", "content": content},
                            "done": False,
                        }) + "\n"

                except Exception as e:
                    log.warning(f"chunk parse error: {e}")
                    continue

    yield json.dumps({
        "model": model_name,
        "message": {"role": "assistant", "content": ""},
        "done": True,
    }) + "\n"
