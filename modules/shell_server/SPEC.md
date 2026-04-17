# shell_server

## Purpose
Standalone execution agent — accepts shell commands over HTTP and returns their output; self-registers with the proxy on startup.

## Exports
FastAPI `app` instance (consumed by uvicorn on port 8000).

## Imports From
None — runs as a completely separate process with no internal module dependencies.

## Behavior Rules
- On startup: discovers local IP via UDP socket trick (`connect("8.8.8.8", 80)`), then POSTs `{"url": "http://<local_ip>:8000"}` to `{PROXY_URL}/register_shell`
- `PROXY_URL` must be set as an environment variable; logs an error and skips registration if absent
- `POST /exec`: runs `request.command` in a subprocess shell asynchronously, returns `{stdout, stderr, exit_code}`
- All subprocess execution uses `asyncio.create_subprocess_shell` — never blocking calls

## Must NOT
- Import from any other internal module
- Expose any endpoint other than `POST /exec`
- Run commands synchronously
