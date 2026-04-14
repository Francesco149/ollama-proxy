import asyncio
import logging
import os
import socket
import httpx
from fastapi import FastAPI
from pydantic import BaseModel

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("shell_server")

app = FastAPI()

class CommandRequest(BaseModel):
    command: str

@app.post("/exec")
async def exec_command(request: CommandRequest):
    log.info(f"[shell] executing command: {request.command}")
    
    process = await asyncio.create_subprocess_shell(
        request.command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    stdout, stderr = await process.communicate()
    exit_code = process.returncode

    return {
        "stdout": stdout.decode().strip(),
        "stderr": stderr.decode().strip(),
        "exit_code": exit_code
    }

@app.on_event("startup")
async def startup():
    proxy_url = os.environ.get("PROXY_URL")
    if not proxy_url:
        log.error("[shell] PROXY_URL environment variable not set. Cannot register.")
        return

    # Discover local IP address
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception as e:
        log.error(f"[shell] Failed to discover local IP: {e}")
        return

    registration_url = f"http://{local_ip}:8000"
    log.info(f"[shell] Attempting to register with proxy: {proxy_url}/register_shell")
    log.info(f"[shell] Local server URL: {registration_url}")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                f"{proxy_url.rstrip('/')}/register_shell",
                json={"url": registration_url}
            )
            if response.status_code == 200:
                log.info(f"[shell] Successfully registered with proxy: {response.json()}")
            else:
                log.warning(f"[shell] Registration failed with status {response.status_code}: {response.text}")
        except Exception as e:
            log.error(f"[shell] Error during registration: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
