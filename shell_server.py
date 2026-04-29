"""
shell_server.py

Lightweight execution agent. Runs inside the sandbox container (or directly
on the host for development). Registers itself with the proxy on startup.

Endpoints:
  POST /exec          — run a shell command
  POST /exec_python   — run a Python snippet via tempfile
  POST /write_file    — write arbitrary content to a path
  POST /run_test      — write and run a test script in /tmp, clean up after
  POST /patch_file    — apply a unified diff to a file; returns success/failure
"""

import asyncio
import logging
import os
import hashlib
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("shell-server")

app = FastAPI()


# ── request models ────────────────────────────────────────────────────────────

class CommandRequest(BaseModel):
    command: str

class PythonCodeRequest(BaseModel):
    code: str

class ShellScriptRequest(BaseModel):
    script: str

class WriteFileRequest(BaseModel):
    path: str
    content: str

class RunTestRequest(BaseModel):
    code: str
    hypothesis: str = ""

class PatchFileRequest(BaseModel):
    path: str
    diff: str   # unified diff (legacy endpoint, kept for compatibility)

class ApplyPatchRequest(BaseModel):
    path: str
    new_content: str   # complete new file content after search/replace


# ── helpers ───────────────────────────────────────────────────────────────────

async def _run(cmd: str, cwd: str | None = None) -> dict:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return {
        "stdout":    stdout.decode().strip(),
        "stderr":    stderr.decode().strip(),
        "exit_code": proc.returncode,
    }


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.post("/exec")
async def exec_command(request: CommandRequest):
    log.info(f"exec: {request.command[:120]}")
    return await _run(request.command)


@app.post("/exec_shell")
async def exec_shell(request: ShellScriptRequest):
    log.info("exec_shell: running script")
    with tempfile.NamedTemporaryFile(suffix=".sh", delete=False) as f:
        f.write(request.script.encode())
        tmp = f.name
    try:
        return await _run(f"bash {tmp}")
    finally:
        Path(tmp).unlink(missing_ok=True)


@app.post("/exec_python")
async def exec_python(request: PythonCodeRequest):
    log.info("exec_python: running code")
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
        f.write(request.code.encode())
        tmp = f.name
    try:
        return await _run(f"{sys.executable} {tmp}")
    finally:
        Path(tmp).unlink(missing_ok=True)


@app.post("/write_file")
async def write_file(request: WriteFileRequest):
    log.info(f"write_file: {request.path}")
    try:
        p = Path(request.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(request.content, encoding="utf-8")
        lines = request.content.count("\n") + (1 if request.content else 0)
        return {"ok": True, "path": request.path, "lines": lines}
    except Exception as e:
        log.error(f"write_file error: {e}")
        return {"ok": False, "error": str(e)}


@app.post("/run_test")
async def run_test(request: RunTestRequest):
    """
    Write a test script to /tmp, execute it, return output, clean up.
    Never touches the project tree. Hypothesis is prepended as a comment
    so it appears in the output for the model to reason against.
    """
    sig  = hashlib.sha1(request.code.encode()).hexdigest()[:8]
    tmp  = Path(f"/tmp/test_{sig}.py")
    header = f"# Hypothesis: {request.hypothesis}\n\n" if request.hypothesis else ""
    tmp.write_text(header + request.code, encoding="utf-8")
    log.info(f"run_test: {tmp} hypothesis={request.hypothesis[:60]!r}")
    try:
        result = await _run(f"{sys.executable} {tmp}")
        return result
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/patch_file")
async def patch_file(request: PatchFileRequest):
    """
    Apply a unified diff to a file using the `patch` utility.
    Returns success + the resulting git diff, or failure + error output.
    The original file is backed up to /tmp before patching so errors
    are recoverable without touching git.
    """
    target = Path(request.path)
    if not target.exists():
        return {"ok": False, "error": f"File not found: {request.path}"}

    # Backup original
    backup = Path(f"/tmp/patch_backup_{target.name}")
    backup.write_bytes(target.read_bytes())
    log.info(f"patch_file: applying diff to {request.path}")

    # Write diff to tempfile
    with tempfile.NamedTemporaryFile(suffix=".diff", delete=False, mode="w") as f:
        f.write(request.diff)
        diff_path = f.name

    try:
        result = await _run(
            f"patch --unified --backup {target} {diff_path}",
            cwd=str(target.parent),
        )

        if result["exit_code"] != 0:
            # Restore from backup on failure
            target.write_bytes(backup.read_bytes())
            log.warning(f"patch_file: apply failed, restored backup: {result['stderr']}")
            return {
                "ok":    False,
                "error": result["stderr"] or result["stdout"],
            }

        # Diff backup vs patched file — works whether or not the file is
        # tracked by git. Keep backup alive until after the diff.
        diff_result = await _run(f"diff -u {backup} {target}")
        # diff exits 1 when files differ (expected) — not an error
        log.info(f"patch_file: applied successfully to {request.path}")
        return {
            "ok":   True,
            "path": request.path,
            "diff": diff_result["stdout"],
        }

    finally:
        Path(diff_path).unlink(missing_ok=True)
        backup.unlink(missing_ok=True)


@app.post("/apply_patch")
async def apply_patch(request: ApplyPatchRequest):
    """
    Write new_content to path and return a diff of the change.
    Used by the search/replace patch_file implementation — the proxy does
    the string replacement, we just write the result and produce the diff.
    """
    target = Path(request.path)
    if not target.exists():
        return {"ok": False, "error": f"File not found: {request.path}"}

    backup = Path(f"/tmp/apply_patch_backup_{target.name}")
    backup.write_bytes(target.read_bytes())
    log.info(f"apply_patch: writing {request.path} ({len(request.new_content)} chars)")

    try:
        target.write_text(request.new_content, encoding="utf-8")
        diff_result = await _run(f"diff -u {backup} {target}")
        return {
            "ok":   True,
            "path": request.path,
            "diff": diff_result["stdout"],
        }
    except Exception as e:
        # Restore backup on failure
        target.write_bytes(backup.read_bytes())
        log.error(f"apply_patch failed, restored: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        backup.unlink(missing_ok=True)


# ── startup registration ──────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    proxy_url = os.environ.get("PROXY_URL")
    if not proxy_url:
        log.warning("PROXY_URL not set — skipping proxy registration")
        return

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception as e:
        log.error(f"Could not determine local IP: {e}")
        return

    shell_url = f"http://{local_ip}:8000"
    log.info(f"Registering with proxy at {proxy_url} as {shell_url}")

    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                f"{proxy_url.rstrip('/')}/register_shell",
                json={"url": shell_url},
            )
            if r.status_code == 200:
                log.info(f"Registered: {r.json()}")
            else:
                log.warning(f"Registration failed {r.status_code}: {r.text}")
        except Exception as e:
            log.error(f"Registration error: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
