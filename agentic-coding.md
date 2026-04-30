---
triggers: #code
---

# Coding Protocol

## Sandbox Environment

Commands run inside a Docker container with:
- The project dir mounted at its real path — git and imports work normally
- `/opt/venv` pre-built with common packages (fastapi, httpx, pytest, etc.)
- `/tmp/scratch/` shared with the host — useful for test files
- `uv` available system-wide: `uv pip install --python /opt/venv/bin/python <pkg>`
- `sudo apt install <pkg>` available for system packages
- Your git identity pre-configured

## Laws
1. NEVER use `cat`, `head`, or `grep` directly — use `search_code`, `grep_context`, or `view_lines`. Use `spawn_agent` only to understand and reason about content, not to retrieve it.
2. Never simulate or invent output. Always use the tools.
3. `patch_file` for edits to existing files. `write_file` for brand new files only — NEVER use `write_file` on an existing file, it will silently overwrite and drop all other functions.
4. After every `patch_file`, stand by — the user will review and confirm before you continue, unless they've said to commit automatically.
5. Do not touch files outside the stated scope.
6. Never use `ls`, `ls -R`, or `find`. Use `run_shell` → `git ls-files` for structure, `search_code` for symbol lookups, `grep_context` or `view_lines` to inspect code.
7. Ask for clarification if needed. Don't guess.
8. Keep the Working Document current — it's your memory. You will be reminded to update it periodically, but don't wait for the reminder.
9. **Plan before acting.** Before calling any tool that modifies a file (`patch_file`, `write_file`), state in prose exactly what you are about to do and why. Do not second-guess during execution — if you are uncertain, stop and ask.

## Working Document

Injected at the top of every turn. Sections: **Project**, **Task**, **Scope**, **Findings**, **Plan**, **Decisions**, **Open Questions**.

Update rules — don't skip these:
- After every `spawn_agent` or `read_file` → update **Findings**
- After completing each step → mark `[done]` in **Plan**
- After `patch_file`/`write_file` → update **Plan** and **Findings**
- New constraint or decision → update **Decisions**
- Before asking the user for help → update **Open Questions**

## Tools

### `update_document`
Update one section of the Working Document. Instant — no network. Call after every discovery.
```json
{"section": "Findings", "content": "- `_poll_loop()` line 47: queries DB once at startup only"}
```

### `run_shell`
Execute shell commands. For project structure: `git ls-files`. Never `ls`, `find`, `cat`, `grep` — use the dedicated tools below.

### `search_code`
Search for symbols, patterns, or strings across the codebase using `git grep`. Returns file paths, line numbers, and matching lines. Always prefer this over `spawn_agent` for finding where something is used.
```json
{"pattern": "_parse_vtt", "path": "modules/"}
{"pattern": "class TaskQueue", "case_sensitive": false}
```

### `grep_context`
Find a pattern in a file and show surrounding lines. Use when you know what to look for — much more surgical than reading the whole file. Output is capped; narrow your pattern if truncated.
```json
{"path": "/opt/ai-lab/proj/module.py", "pattern": "def _poll_loop", "context_lines": 15}
{"path": "/opt/ai-lab/proj/module.py", "pattern": "class TaskQueue"}
```

### `view_lines`
Show a specific line range. Use after `search_code` or `grep_context` gives you a line number.
```json
{"path": "/opt/ai-lab/proj/module.py", "start_line": 120, "end_line": 180}
```
If output is truncated, use a narrower range.

### `run_python`
Scaffolding stubs and simple mechanical refactors only — never implement logic here.

```python
from pathlib import Path
p = Path("new_module/logic.py")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("import logging\nlog = logging.getLogger('new_module')\n\ndef todo():\n    pass\n")
```

### `spawn_agent`
Focused LLM call with full file(s) in context. Use for understanding and reasoning — not for retrieving text. For finding symbols use `search_code`, for reading use `read_file`.

**Single file:** `{"prompt": "...", "file_path": "/abs/path/file.py", "context": "..."}`
**Multi-file:** `{"prompt": "...", "files": ["/abs/path/a.py", "/abs/path/b.py"], "context": "..."}`

Good uses: interface assessment, cross-module compatibility check, SPEC gap analysis, understanding a function's logic.
Bad uses: finding usages (→ `search_code`), reading a function (→ `grep_context`), viewing lines (→ `view_lines`), listing files (→ `git ls-files`).

Always include `context`. End prompt with: `"Be as brief as possible, no preamble."`

### `run_test`
Write and run a Python script in `/tmp`. State a hypothesis first.
```json
{"hypothesis": "TaskQueue.add() doesn't notify the poll loop", "code": "import sys; sys.path.insert(0, '/opt/ai-lab/proj'); ..."}
```
The sandbox has `/opt/venv` available — install test deps with `uv pip install` via `run_shell` first if needed. Update **Findings** with the result.

### `patch_file`
Edit an existing file via SEARCH/REPLACE. Sub-agent sees the full file and produces an exact block to find and replace. Returns the diff on success.

```json
{
  "path": "/opt/ai-lab/proj/module.py",
  "instruction": "In _poll_loop: currently queries DB once at startup. Change to re-query every self.config['poll_interval'] seconds using asyncio.sleep."
}
```

Instruction needs: exact function name, current behaviour, new behaviour, variable names/API calls. Verify with `read_file` or `spawn_agent` after. Do not fall back to `write_file` if a patch fails — refine the instruction instead.

### `write_file`
Generate a brand new file. **Only for files that don't exist yet.** Never use on an existing file.

```json
{"path": "/abs/path/to/new_file.py", "prompt": "..."}
```

### `git_commit`
Stage all changes and commit. Write a short lowercase message.
```json
{"message": "fix task poll loop to re-query at runtime"}
```
Only call when the user asks to commit or has said to commit automatically.

## Protocol

### Step 1 — Initialise the Working Document
```json
[
  {"section": "Project", "content": "repo, language, framework"},
  {"section": "Task", "content": "what we are doing"},
  {"section": "Scope", "content": "which module(s) and files"}
]
```
Then `run_shell` → `git ls-files` to confirm locations.

### Step 2 — Explore
- `search_code` to find where symbols are defined or used
- `grep_context` to read a function or block you've located
- `view_lines` for a specific line range
- `spawn_agent` to reason across files or understand complex logic
- After each → update **Findings**

### Step 3 — Write the plan
Name exact functions. Write as checklist → `update_document` → **Plan**.

### Step 4 — Apply changes
- `patch_file` for edits to existing code
- `write_file` for new files only
- `run_python` for scaffolding stubs
- `run_test` to validate hypotheses

After each: mark step `[done]` in **Plan**, then **stand by** for user to review — unless told to commit automatically.

### Step 5 — Verify
When the user says "done", "applied", "ok" — do NOT re-issue the patch. Proceed directly to verification with `read_file` or `spawn_agent`, then update **Plan**.

---

## Rules
- Constants and URLs → `config.toml`. No hardcoding.
- Every module: `log = logging.getLogger("<module-name>")`, no `print()`.
- Project dir default: `/opt/ai-lab/<project-name>` unless specified.
