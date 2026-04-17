# config_loader

## Purpose
Loads `config.toml` once at process startup and exposes it as an immutable dict for the process lifetime.

## Exports
```python
def get_config() -> dict[str, Any]
```

## Imports From
None — no internal dependencies.

## Behavior Rules
- Reads from path in `OLLAMA_PROXY_CONFIG` env var; falls back to `/opt/ai-lab/ollama-proxy/config.toml`
- Config is loaded once into module-level `CONFIG` at import time
- Returns an empty dict (not an exception) if the file is missing

## Must NOT
- Re-read the file on every `get_config()` call
- Hold any state beyond the loaded config dict
- Import from any other internal module
