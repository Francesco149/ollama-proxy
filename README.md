# ollama-proxy

> **WARNING**: This project is an ad-hoc tool. Most of the code has been generated or refactored with AI assistance. Use with caution.

A personal, ad-hoc tool designed to bridge LLMs (via Ollama/llama.cpp) with a local shell environment.

The main feature is a custom agentic workflow in Open WebUI where the LLM can surgically explore and probe your code by running shell commands like grep in your project folder.

This keeps the context small and maintains very quick response times from the LLM as it methodically gathers information and navigates the codebase.

I currently use this with gemma4 a4b `q3_k_m` and it's a very responsive and viable local AI coding workflow which wrote most of this code and spec files.

## Quick Start

1. **Configure**:
   - `cp config.example.toml config.toml`
   - Edit `config.toml` with your service URLs (LLM, Ingestion, Embedding).
   - Make sure to put the skills repo in the dir you specify here.
2. **Start the Proxy**:
   - `python proxy.py` (Ensure your LLM engine is running).
3. **Start the Shell Server**:
   - `export PROXY_URL=http://localhost:11434 && python shell_server.py`
   - You would run this in the root folder of your project, ideally sandboxed so it doesn't have access to anything else. **This can run arbitrary commands on your machine!**

## Agentic Coding Workflow (Example)

This is how a typical session looks when using the agentic skills:

```text
user: #code let's refactor the port logic. project folder is /opt/ai-lab/ollama-proxy
agent: I'll need to find where the port is currently defined. Please run:
`git -C /opt/ai-lab/ollama-proxy grep -r "port" .`
user: .run
agent:
proxy.py:25:        proxy_port = server_cfg.get("proxy_port", 11434)
shell_server.py:12:    log.error("[shell] PROXY_URL environment variable not set.")

user: move the port into the config file
agent: I need to see how the config is loaded. Please run:
`cat config_loader.py`
user: .run
agent:
import os
...
def _load_config() -> Dict[str, Any]:
    config_path = os.environ.get("OLLAMA_PROXY_CONFIG", "config.toml")

user: okay, update config_loader.py to use the absolute path
agent: I'll update the default path in config_loader.py to /opt/ai-lab/ollama-proxy/config.toml.

edit config_loader.py
... update default path ...

(user manually runs prompt on aider interactively)

user: .diff
agent:
diff --git a/config_loader.py b/config_loader.py
index 8c7414b..9eefa5c 100644
--- a/config_loader.py
+++ b/config_loader.py
@@ -22,7 +22,7 @@ def _load_config() -> Dict[str, Any]:
-    config_path = os.environ.get("OLLAMA_PROXY_CONFIG", "config.toml")
+    config_path = os.environ.get("OLLAMA_PROXY_CONFIG", "/opt/ai-lab/ollama-proxy/config.toml")

user: looks good?
agent: Yes, that looks perfect. Refactor complete.
```

## Skills & Commands

- **Manual Commands**: Start a message with `.` to bypass the LLM and run a command directly (e.g., `.run ls -la`).
- **Skill Triggers**: Use `#` to explicitly trigger agentic skills (e.g., `#code`, `#diff`, `#run`). This ensures reliable activation without relying on semantic intent.
- **Tools**:
    - `ingest_url`: Feed a URL into the RAG knowledge base.
    - `run_shell`: Execute arbitrary commands in the local environment.

## Resources

- **Specifications**: [TECH_SPEC.md](./TECH_SPEC.md) | [DESIGN.md](./DESIGN.md)
- **Skills Repository**: [github.com/Francesco149/skills](https://github.com/Francesco149/skills)
- **Ingestion Server**: [github.com/Francesco149/ingest](https://github.com/Francesco149/ingest)
