import httpx
import asyncio
import logging
import re
from config_loader import get_config

log = logging.getLogger("tool-manager")

SHELL_SERVER_URL = None
_LLM_BASE: str | None = None
_LLM_MODEL: str | None = None

_ingest_sem = asyncio.Semaphore(2)

# Document updater — set per-request by proxy.py
# Callable[(section, content) -> bool] that writes to session_manager.
_doc_updater: "Callable[[str, str], bool] | None" = None

# Context provider — set per-request by proxy.py
# Returns the clean shadow-context message list for the current session.
# None means no context injection is configured.
_context_provider: "Callable[[], list[dict]] | None" = None

# Per-tool context mode: "none" | "full" | "summarize"
_tool_context_modes: dict[str, str] = {
    "spawn_agent": "none",
    "write_file":  "summarize",
    "patch_file":  "full",
    "run_test":    "none",
}

# Max messages to include in "full" mode (last N, excluding role=tool)
_context_max_messages: int = 20

RE_SHELL = re.compile(r'<run-shell>(.*?)</run-shell>', re.DOTALL)
RE_PYTHON = re.compile(r'<run-python>(.*?)</run-python>', re.DOTALL)

# ── shell URL state ───────────────────────────────────────────────────────────

def set_shell_url(url: str):
    global SHELL_SERVER_URL
    SHELL_SERVER_URL = url
    log.info(f"SHELL_SERVER_URL set to {url}")


def set_llm_config(llm_base: str, llm_model: str) -> None:
    global _LLM_BASE, _LLM_MODEL
    _LLM_BASE = llm_base
    _LLM_MODEL = llm_model
    log.info(f"LLM config set: base={llm_base} model={llm_model}")


def set_doc_updater(updater: "Callable[[str, str], bool] | None") -> None:
    """Register a callable that writes Working Document sections for this request."""
    global _doc_updater
    _doc_updater = updater
    log.debug("doc_updater registered")


def set_context_provider(
    provider: "Callable[[], list[dict]] | None",
    modes: dict[str, str] | None = None,
    max_messages: int = 20,
) -> None:
    """
    Register a lazy context provider for the current request.

    provider: callable that returns the clean shadow-context message list.
              Called lazily at tool-execution time so it includes all turns
              up to and including the current one.
    modes:    per-tool override map {"tool_name": "none"|"full"|"summarize"}.
              Merges with defaults — only keys present are overridden.
    """
    global _context_provider, _tool_context_modes, _context_max_messages
    _context_provider = provider
    _context_max_messages = max_messages
    if modes:
        _tool_context_modes.update(modes)
    log.debug(f"context provider set, modes={_tool_context_modes}")

# ── tool schemas ──────────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": (
                "Execute a shell command on the project host. "
                "Use for: git commands, reading file trees (git ls-files), "
                "running scripts, checking diffs. "
                "Never use ls or find — always git ls-files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute. May be multi-line for scripts.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute Python code on the project host. "
                "Use for: simple refactors (fixing imports, bulk renames), "
                "scaffolding new files/dirs, data transformations. "
                "Never implement business logic here — use write_file or aider."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute.",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": (
                "Spawn a focused sub-agent with a fresh context window to analyze one or "
                "more files. Use to inspect interfaces, plan refactors, validate SPECs, "
                "or reason across module boundaries — without loading files into the main "
                "context. The sub-agent sees the full file(s); you receive only its answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Precise question for the sub-agent. Ask for function signatures, "
                            "line numbers, interface compatibility, or SPEC gaps. "
                            "End with: Do not include any preamble, be as brief as possible."
                        ),
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Absolute path to a single file to load (convenience alias for files[0]).",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Absolute paths of multiple files to load for cross-module reasoning. "
                            "Use when checking interface compatibility or tracing calls across modules."
                        ),
                    },
                    "context": {
                        "type": "string",
                        "description": "Current task context so the sub-agent gives relevant answers.",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Generate a complete file via a sub-agent and write it to disk. "
                "File content never enters the orchestrator context — you receive only "
                "a brief status line. Use for new modules or full rewrites. "
                "Always verify with spawn_agent immediately after."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path where the file should be written.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Fully self-contained generation prompt. Include: all function "
                            "signatures, behavior rules, imports, edge cases, logging setup. "
                            "The agent has no other context."
                        ),
                    },
                },
                "required": ["path", "prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_test",
            "description": (
                "Write a Python test script to /tmp, execute it, and return the output. "
                "Use to confirm or deny a hypothesis about runtime behaviour before "
                "touching production code. The script runs in the sandbox and is deleted "
                "after execution — it never touches the project tree."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hypothesis": {
                        "type": "string",
                        "description": (
                            "One-sentence statement of what you expect to be true or false. "
                            "Prepended as a comment so the output is self-documenting."
                        ),
                    },
                    "code": {
                        "type": "string",
                        "description": (
                            "Complete, self-contained Python script that proves or disproves "
                            "the hypothesis. May import from the project — the project dir "
                            "is on sys.path inside the sandbox."
                        ),
                    },
                },
                "required": ["hypothesis", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_document",
            "description": (
                "Update a section of the Working Document. "
                "Call this after every spawn_agent to record findings, after completing "
                "each plan step to mark it [done], and whenever you discover a constraint "
                "or decision. Keeping the document current is what allows old tool results "
                "to be safely evicted from context. This is a lightweight local operation "
                "— no file I/O, no network call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": ["Project", "Task", "Scope", "Findings", "Plan",
                                 "Decisions", "Open Questions"],
                        "description": "Which section to update (replaces existing content).",
                    },
                    "content": {
                        "type": "string",
                        "description": "New content for this section. Be concise — bullet points preferred.",
                    },
                },
                "required": ["section", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_file",
            "description": (
                "Apply a surgical edit to an existing file without loading it into context. "
                "A sub-agent receives the full file + instruction and produces a "
                "SEARCH/REPLACE block; the proxy applies it as a literal string replacement. "
                "Returns a diff of the change on success. "
                "Use for any targeted edit to an existing file — do NOT use write_file "
                "to overwrite existing files, as it will silently drop all other functions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the file to edit.",
                    },
                    "instruction": {
                        "type": "string",
                        "description": (
                            "Precise edit instruction. Include: exact function name, "
                            "what currently happens, what should happen instead, "
                            "any variable names, imports, or API calls involved."
                        ),
                    },
                },
                "required": ["path", "instruction"],
            },
        },
    },
]

# ── tool execution ────────────────────────────────────────────────────────────

async def execute_tool(name: str, args: dict) -> str:
    log.info(f"execute name={name} args={args}")

    if name == "ingest_url":
        return await _execute_ingest(args)

    if name == "run_shell":
        return await _execute_shell(args)

    if name == "run_python":
        return await _execute_python(args)

    if name == "spawn_agent":
        return await _execute_spawn_agent(args)

    if name == "write_file":
        return await _execute_write_file(args)

    if name == "update_document":
        return _execute_update_document(args)

    if name == "run_test":
        return await _execute_run_test(args)

    if name == "patch_file":
        return await _execute_patch_file(args)

    return f"Unknown tool: {name}"


async def _execute_ingest(args: dict) -> str:
    ingest_base = get_config().get("server", {}).get("ingest_base", "http://localhost:8083")

    urls = args.get("urls", [])
    if isinstance(urls, str):
        urls = [urls]
    # Handle model schema drift: sometimes sends singular "url" key
    if not urls and args.get("url"):
        urls = [args["url"]]

    if not urls:
        return "No URLs found in tool call args"

    note = args.get("note", "")
    log.info(f"urls to ingest: {urls}")

    async def ingest_one(url: str) -> str:
        try:
            async with _ingest_sem:
                async with httpx.AsyncClient(timeout=86400) as client:
                    log.info(f"ingesting {url}")
                    resp = await client.post(f"{ingest_base}/ingest", json={"url": url, "note": note})
                    r = resp.json()
            if r.get("status") == "ok":
                return f"✓ {r.get('title') or r.get('domain') or url}"
            elif r.get("status") == "todo":
                return f"⚠ unsupported URL saved as todo: {url}"
            else:
                return f"✗ failed: {url} — {r.get('error')}"
        except Exception as e:
            log.error(f"ingest_one exception for {url}: {e}", exc_info=True)
            return f"✗ exception: {url} — {e}"

    try:
        results = await asyncio.gather(*[ingest_one(url) for url in urls])
        return "Saved to knowledge base:\n" + "\n".join(results)
    except Exception as e:
        log.error(f"gather exception: {e}", exc_info=True)
        return f"Error during ingestion: {e}"


async def _execute_shell(args: dict) -> str:
    command = args.get("command")
    if not command:
        return "No command provided for run_shell"
    if not SHELL_SERVER_URL:
        return "Shell server URL is not configured"
    try:
        log.info(f"running shell command: {command}")
        async with httpx.AsyncClient(timeout=600) as client:
            if "\n" in command:
                resp = await client.post(f"{SHELL_SERVER_URL}/exec_shell", json={"script": command})
            else:
                resp = await client.post(f"{SHELL_SERVER_URL}/exec", json={"command": command})
            r = resp.json()
        stdout = r.get("stdout", "")
        stderr = r.get("stderr", "")
        exit_code = r.get("exit_code", 0)
        result = f"Command executed (exit code: `{exit_code}`)\n"
        if stdout:
            result += f"### STDOUT:\n`````\n{stdout}\n`````\n"
        if stderr:
            result += f"### STDERR:\n`````\n{stderr}\n`````\n"
        return result
    except Exception as e:
        log.error(f"run_shell exception: {e}", exc_info=True)
        return f"Error executing shell command: {e}"


async def _execute_python(args: dict) -> str:
    code = args.get("code")
    if not code:
        return "No code provided for run_python"
    if not SHELL_SERVER_URL:
        return "Shell server URL is not configured"
    try:
        log.info(f"running python code: {code[:50]}...")
        async with httpx.AsyncClient(timeout=600) as client:
            resp = await client.post(f"{SHELL_SERVER_URL}/exec_python", json={"code": code})
            r = resp.json()
        stdout = r.get("stdout", "")
        stderr = r.get("stderr", "")
        exit_code = r.get("exit_code", 0)
        result = f"Python executed (exit code: `{exit_code}`)\n"
        if stdout:
            result += f"### STDOUT:\n`````\n{stdout}\n`````\n"
        if stderr:
            result += f"### STDERR:\n`````\n{stderr}\n`````\n"
        return result
    except Exception as e:
        log.error(f"run_python exception: {e}", exc_info=True)
        return f"Error executing python code: {e}"

def _execute_update_document(args: dict) -> str:
    """Synchronous — just updates in-memory state via the registered callback."""
    section = args.get("section", "")
    doc_content = args.get("content", "")
    if not section:
        return "❌ update_document: 'section' is required"
    if _doc_updater is None:
        return "❌ update_document: no doc_updater registered"
    ok = _doc_updater(section, doc_content)
    if ok:
        return f"✓ Working Document [{section}] updated."
    return f"❌ update_document: unknown section {section!r}. Valid: Project, Task, Scope, Findings, Plan, Decisions, Open Questions"


def validate_tool_args(name: str, args: dict) -> str | None:
    """
    Return an error string if required args are missing or empty,
    None if args look valid.

    Catches the common failure mode where the model emits a tool call
    with empty or null parameters — returns a structured error message
    the model can self-correct from rather than executing a no-op.
    """
    required: dict[str, list[str]] = {
        "run_shell":       ["command"],
        "run_python":      ["code"],
        "spawn_agent":     ["prompt"],
        "write_file":      ["path", "prompt"],
        "run_test":        ["code"],
        "patch_file":      ["path", "instruction"],
        "update_document": ["section", "content"],
    }
    fields = required.get(name, [])
    missing = [f for f in fields if not args.get(f)]
    if missing:
        return (
            f"❌ {name} called with missing/empty required field(s): "
            f"{', '.join(missing)}. "
            f"This is likely a generation error — retry with the intended values."
        )
    return None


async def _build_context_messages(tool_name: str) -> list[dict]:
    """
    Build the message history to prepend to a sub-agent call.

    Mode "none"      — empty list (no context injected)
    Mode "full"      — last _context_max_messages assistant+user turns
                       from the shadow context. role=tool messages are
                       excluded: they are raw stdout/file content that
                       bloats the prompt without helping the sub-agent.
    Mode "summarize" — single separate LLM call that condenses the session
                       into a concise handoff paragraph, then wraps it as
                       a user message. Costs one extra round-trip but
                       produces focused context even from long sessions.
    """
    mode = _tool_context_modes.get(tool_name, "none")
    if mode == "none" or _context_provider is None:
        return []

    raw = _context_provider()  # full clean shadow context
    # Filter to prose turns only — exclude role=tool (raw results)
    prose = [m for m in raw if m.get("role") in ("user", "assistant")]

    if mode == "full":
        kept = prose[-_context_max_messages:]
        log.debug(f"context full: {len(kept)} messages for {tool_name}")
        return kept

    if mode == "summarize":
        if not prose:
            return []
        # Build a flat transcript for the summariser
        lines = []
        for m in prose[-_context_max_messages:]:
            role = m.get("role", "")
            txt  = m.get("content") or ""
            if isinstance(txt, list):          # multi-part content
                txt = " ".join(p.get("text", "") for p in txt if p.get("type") == "text")
            lines.append(f"[{role}]: {txt[:400]}")
        transcript = "\n".join(lines)

        summarise_prompt = (
            "You are preparing a handoff for a coding sub-agent. "
            "Given the conversation below, write a concise handoff (≤150 words) covering:\n"
            "- Project and module being worked on\n"
            "- Relevant structure discovered (function names, file paths, patterns)\n"
            "- The specific task that needs to happen next\n"
            "- Any constraints or conventions that apply\n\n"
            "Be specific. Include file paths, function names, variable names where known.\n\n"
            f"Conversation:\n{transcript}"
        )
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{_LLM_BASE}/v1/chat/completions",
                    json={
                        "model":      _LLM_MODEL,
                        "messages":   [{"role": "user", "content": summarise_prompt}],
                        "stream":     False,
                        "max_tokens": 300,
                    },
                )
                summary = resp.json()["choices"][0]["message"]["content"].strip()
            log.debug(f"context summarize: {len(summary)} chars for {tool_name}")
            return [{"role": "user", "content": f"Session context:\n{summary}"}]
        except Exception as e:
            log.warning(f"context summarize failed: {e} — falling back to none")
            return []

    return []


async def _read_file_via_shell(path: str) -> tuple[str, str | None]:
    """Read a file via shell_server. Returns (content, error_or_None)."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SHELL_SERVER_URL}/exec",
                json={"command": f"cat '{path}'"},
            )
            r = resp.json()
        if r.get("exit_code") != 0:
            return "", f"❌ Failed to read {path}: {r.get('stderr', 'unknown error')}"
        return r.get("stdout", ""), None
    except Exception as e:
        return "", f"❌ Error reading {path}: {e}"


async def _execute_spawn_agent(args: dict) -> str:
    """
    Spawn a sub-agent with one or more files loaded into its context.

    Accepts either:
      file_path: str          — single file (backward compatible)
      files: list[str]        — multiple files, each wrapped in <file path="..."> tags

    The sub-agent sees all requested files and responds to `prompt`.
    `context` is prepended as task context.
    """
    prompt    = args.get("prompt", "")
    file_path = args.get("file_path")
    files     = args.get("files", [])
    context   = args.get("context", "")

    if not _LLM_BASE or not _LLM_MODEL:
        return "❌ Sub-agent not configured — call set_llm_config first"
    if not SHELL_SERVER_URL:
        return "❌ Shell server not registered — cannot read files"

    # Normalise to a single list
    all_paths = list(files)
    if file_path and file_path not in all_paths:
        all_paths.insert(0, file_path)

    parts: list[str] = []
    if context:
        parts.append(f"Task context: {context}")

    for path in all_paths:
        content, err = await _read_file_via_shell(path)
        if err:
            parts.append(err)
        else:
            parts.append(f'<file path="{path}">\n{content}\n</file>')
            log.info(f"spawn_agent: loaded {len(content)} chars from {path}")

    parts.append(prompt)
    user_content = "\n\n".join(parts)

    ctx_messages = await _build_context_messages("spawn_agent")
    messages = ctx_messages + [{"role": "user", "content": user_content}]
    log.info(f"spawn_agent: {len(all_paths)} file(s), prompt_len={len(user_content)}, ctx={len(ctx_messages)}")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{_LLM_BASE}/v1/chat/completions",
                json={
                    "model": _LLM_MODEL,
                    "messages": messages,
                    "stream": False,
                    "max_tokens": 2048,
                },
            )
            data = resp.json()
        result = data["choices"][0]["message"]["content"]
        log.info("spawn_agent: response received")
        return result
    except Exception as e:
        log.error(f"spawn_agent LLM call error: {e}", exc_info=True)
        return f"❌ Sub-agent call failed: {e}"


async def _execute_write_file(args: dict) -> str:
    """
    Generate a file via a fresh sub-agent call, write it to disk, and
    return a brief status string. The LLM context only sees the status —
    never the file content — keeping the orchestrator context lean.
    """
    path   = args.get("path", "")
    prompt = args.get("prompt", "")

    if not path or not prompt:
        return "❌ write-file: 'path' and 'prompt' are required"

    if not _LLM_BASE or not _LLM_MODEL:
        return "❌ write-file: LLM not configured (call set_llm_config first)"
    if not SHELL_SERVER_URL:
        return "❌ write-file: shell server not registered"

    # ── Generate file content via sub-agent ───────────────────────────────────
    system_instruction = (
        "You are a code generation assistant. "
        "Respond with ONLY the complete file contents — no preamble, "
        "no explanation, no markdown fences. Raw code only."
    )
    ctx_messages = await _build_context_messages("write_file")
    # Context goes between system instruction and the generation prompt
    messages = [{"role": "system", "content": system_instruction}]
    messages += ctx_messages
    messages += [{"role": "user", "content": prompt}]
    log.info(f"write_file: generating {path!r}, ctx={len(ctx_messages)}")
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{_LLM_BASE}/v1/chat/completions",
                json={
                    "model": _LLM_MODEL,
                    "messages": messages,
                    "stream": False,
                    "max_tokens": 8192,
                },
            )
            data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        log.info(f"write_file: sub-agent returned {len(content)} chars")
    except Exception as e:
        log.error(f"write_file sub-agent error: {e}", exc_info=True)
        return f"❌ write-file: sub-agent call failed: {e}"

    # Strip any accidental markdown fences the model may have added
    fence_m = re.search(r'^```[^\n]*\n(.*?)^```\s*$', content, re.DOTALL | re.MULTILINE)
    if fence_m:
        content = fence_m.group(1)
        log.debug("write_file: stripped markdown fences from output")

    # ── Write to disk via shell_server ────────────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SHELL_SERVER_URL}/write_file",
                json={"path": path, "content": content},
            )
            r = resp.json()
        if not r.get("ok"):
            return f"❌ write-file: could not write {path}: {r.get('error', 'unknown')}"
        n_lines = r.get("lines", content.count("\n") + 1)
        log.info(f"write_file: wrote {path!r} ({n_lines} lines)")
    except Exception as e:
        log.error(f"write_file disk write error: {e}", exc_info=True)
        return f"❌ write-file: disk write failed: {e}"

    # Return a terse status — content never enters the orchestrator context
    preview = "\n".join(content.splitlines()[:8])
    return (
        f"File written: `{path}` ({n_lines} lines). "
        f"Verify implementation with spawn-agent.\n\n"
        f"```\n{preview}\n{'...' if n_lines > 8 else ''}\n```"
    )


async def _execute_run_test(args: dict) -> str:
    """Write a hypothesis + test script to /tmp in the sandbox, run it, clean up."""
    code       = args.get("code", "")
    hypothesis = args.get("hypothesis", "")

    if not code:
        return "❌ run_test: 'code' is required"
    if not SHELL_SERVER_URL:
        return "❌ run_test: shell server not registered"

    log.info(f"run_test: hypothesis={hypothesis[:60]!r}")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{SHELL_SERVER_URL}/run_test",
                json={"code": code, "hypothesis": hypothesis},
            )
            r = resp.json()
        stdout = r.get("stdout", "")
        stderr = r.get("stderr", "")
        code_val = r.get("exit_code", -1)
        parts = []
        if hypothesis:
            parts.append("**Hypothesis:** " + hypothesis)
        if stdout:
            parts.append("**stdout:**\n``````\n" + stdout + "\n``````")
        if stderr:
            parts.append("**stderr:**\n``````\n" + stderr + "\n``````")
        parts.append(f"**exit code:** `{code_val}`")
        return "\n\n".join(parts)
    except Exception as e:
        log.error(f"run_test error: {e}", exc_info=True)
        return f"❌ run_test failed: {e}"


async def _execute_patch_file(args: dict, _retry: bool = False) -> str:
    """
    Sub-agent produces a SEARCH/REPLACE block for the given instruction.
    Applied as a literal string replacement — no diff format, no line-number
    sensitivity. Retries once with explicit error feedback on failure.

    Format the sub-agent must produce:
        <<<SEARCH
        <exact lines to find>
        >>>REPLACE
        <replacement lines>
        >>>END
    """
    path        = args.get("path", "")
    instruction = args.get("instruction", "")

    if not path or not instruction:
        return "❌ patch_file: 'path' and 'instruction' are required"
    if not _LLM_BASE or not _LLM_MODEL:
        return "❌ patch_file: LLM not configured"
    if not SHELL_SERVER_URL:
        return "❌ patch_file: shell server not registered"

    file_content, err = await _read_file_via_shell(path)
    if err:
        return err

    system = (
        "You are a precise code editing assistant. "
        "Given a file and an instruction, produce a SEARCH/REPLACE block. "
        "Rules:\n"
        "1. SEARCH must be an EXACT verbatim copy of the lines to replace — "
        "   whitespace, indentation, and punctuation must match perfectly.\n"
        "2. REPLACE is the new version of those lines.\n"
        "3. Keep the block as small as possible — only include lines that change "
        "   plus 1-2 lines of context to make it unique in the file.\n"
        "4. Output ONLY the block. No explanation, no markdown, no preamble.\n\n"
        "Format:\n"
        "<<<SEARCH\n"
        "<exact lines>\n"
        ">>>REPLACE\n"
        "<new lines>\n"
        ">>>END"
    )
    user = (
        f"File: {path}\n\n"
        f"<file>\n{file_content}\n</file>\n\n"
        f"Instruction: {instruction}"
    )

    ctx_messages = await _build_context_messages("patch_file")
    messages = [{"role": "system", "content": system}]
    messages += ctx_messages
    messages += [{"role": "user", "content": user}]
    log.info(f"patch_file: generating search/replace for {path!r}, ctx={len(ctx_messages)}")

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{_LLM_BASE}/v1/chat/completions",
                json={
                    "model":      _LLM_MODEL,
                    "messages":   messages,
                    "stream":     False,
                    "max_tokens": 4096,
                },
            )
            raw = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error(f"patch_file sub-agent error: {e}", exc_info=True)
        return f"❌ patch_file: generation failed: {e}"

    # Parse SEARCH/REPLACE block — strip accidental fences first
    raw = re.sub(r'(?m)^```[^\n]*\n', "", raw)
    raw = re.sub(r'(?m)^```\s*$', "", raw)
    raw = raw.strip()

    m = re.search(
        r'<<<SEARCH\n(.*?)\n>>>REPLACE\n(.*?)\n>>>END',
        raw, re.DOTALL
    )
    if not m:
        err_msg = f"❌ patch_file: could not parse SEARCH/REPLACE block. Got:\n{raw[:300]}"
        if not _retry:
            log.warning("patch_file: bad format, retrying with explicit feedback")
            # Feed the failure back so the model can self-correct
            args = dict(args)
            args["_format_error"] = raw[:300]
            return await _execute_patch_file(args, _retry=True)
        return err_msg

    search_text  = m.group(1)
    replace_text = m.group(2)

    if search_text not in file_content:
        if not _retry:
            log.warning("patch_file: SEARCH text not found in file, retrying")
            args = dict(args)
            args["_match_error"] = (
                f"The SEARCH block you produced was not found verbatim in the file. "
                f"Check indentation and whitespace. SEARCH was:\n{search_text[:200]}"
            )
            return await _execute_patch_file(args, _retry=True)
        return (
            f"❌ patch_file: SEARCH block not found in file after retry.\n"
            f"SEARCH was:\n{search_text[:300]}"
        )

    new_content = file_content.replace(search_text, replace_text, 1)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{SHELL_SERVER_URL}/apply_patch",
                json={"path": path, "new_content": new_content},
            )
            r = resp.json()
    except Exception as e:
        return f"❌ patch_file: write failed: {e}"

    if not r.get("ok"):
        return f"❌ patch_file: write failed: {r.get('error', 'unknown')}"

    diff_text = r.get("diff", "")
    log.info(f"patch_file: applied search/replace to {path!r}")
    return (
        f"Patch applied to `{path}`.\n\n"
        f"**Diff:**\n``````diff\n{diff_text}\n``````"
    )


# ── dot-command dispatcher ────────────────────────────────────────────────────

async def _handle_run(messages: list) -> str:
    assistant_msg = None
    for msg in reversed(messages[:-1]):
        if msg.get("role") == "assistant":
            assistant_msg = msg
            break
    if not assistant_msg:
        return "No assistant message found to extract commands from."
    
    content = assistant_msg.get("content", "")
    shell_matches = RE_SHELL.findall(content)
    python_matches = RE_PYTHON.findall(content)
    
    if not shell_matches and not python_matches:
        return "No shell or python commands found in the last assistant message."
    
    results = []
    for cmd in shell_matches:
        res = await execute_tool("run_shell", {"command": cmd.strip()})
        results.append(res)
    for code in python_matches:
        res = await execute_tool("run_python", {"code": code.strip()})
        results.append(res)
    return "\n\n---\n\n".join(results)


async def _handle_py(messages: list) -> str:
    assistant_msg = None
    for msg in reversed(messages[:-1]):
        if msg.get("role") == "assistant":
            assistant_msg = msg
            break
    if not assistant_msg:
        return "No assistant message found to extract python code from."
    matches = RE_PYTHON.findall(assistant_msg.get("content", ""))
    if not matches:
        return "No python code found in the last assistant message."
    results = []
    for code in matches:
        res = await execute_tool("run_python", {"code": code.strip()})
        results.append(res)
    return "\n\n---\n\n".join(results)


async def _handle_diff(_messages: list) -> str:
    return await execute_tool("run_shell", {"command": "git diff HEAD~1 HEAD"})


async def process_manual_command(messages: list) -> str | None:
    if not messages:
        return None
    last_msg = messages[-1]
    if last_msg.get("role") != "user":
        return None
    content = last_msg.get("content", "").strip()

    handlers = {
        ".run": _handle_run,
        ".diff": _handle_diff,
        ".fetch": None,
    }

    if content in handlers:
        if content == ".fetch":
            return "No URL provided for .fetch"
        return await handlers[content](messages)

    for prefix, handler in handlers.items():
        if content.startswith(prefix + " "):
            if prefix == ".run":
                cmd = content[len(prefix) + 1:].strip()
                if not cmd:
                    return f"No command provided after {prefix}"
                return await execute_tool("run_shell", {"command": cmd})
            elif prefix == ".fetch":
                url = content[len(prefix) + 1:].strip()
                if not url:
                    return f"No URL provided after {prefix}"
                return await execute_tool("ingest_url", {"urls": [url]})
            else:
                return await handler(messages)

    if content.startswith("."):
        return f"❌ Error: Unknown command '{content.split()[0]}'"

    return None
