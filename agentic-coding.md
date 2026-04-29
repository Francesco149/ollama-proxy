---
triggers: #code
---

# Coding Protocol

## Laws
1. NEVER read code directly. Use `spawn_agent` to understand structure and contents. Sub-agents are for reading and understanding — not for searching. For finding usages, use `run_shell` with `git grep`.
2. Never simulate or invent output. Always use the tools.
3. `patch_file` for edits to existing files. `write_file` for brand new files only — NEVER use `write_file` on an existing file, it will silently overwrite and drop all other functions.
4. After every `patch_file`, stand by — the user will review and confirm before you continue, unless they've said to commit automatically.
5. Do not touch files outside the stated scope.
6. Never use `ls`, `ls -R`, or `find`. Use `run_shell` → `git ls-files` or `git grep`.
7. Ask for clarification if needed. Don't guess.
8. Keep the Working Document current — it's your memory across turns and after context eviction.

## Working Document

Injected at the top of every turn. Sections: **Project**, **Task**, **Scope**, **Findings**, **Plan**, **Decisions**, **Open Questions**.

Update rules:
- After every `spawn_agent` → update **Findings**
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
Execute shell commands. For searching: `git grep -n 'symbol' -- '*.py'`. For structure: `git ls-files`. Never `ls` or `find`.

### `run_python`
Scaffolding stubs and simple mechanical refactors only — never implement logic here.

```python
from pathlib import Path
p = Path("new_module/logic.py")
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text("import logging\nlog = logging.getLogger('new_module')\n\ndef todo():\n    pass\n")
```

### `spawn_agent`
Focused LLM call with full file(s) in context. Sub-agent reads and understands; you get a concise answer. For searching across files use `run_shell` → `git grep` instead.

**Single file:** `{"prompt": "...", "file_path": "/abs/path/file.py", "context": "..."}`
**Multi-file:** `{"prompt": "...", "files": ["/abs/path/a.py", "/abs/path/b.py"], "context": "..."}`

Good uses: interface assessment, cross-module compatibility check, SPEC gap analysis, understanding a specific function's behaviour.
Bad uses: searching for usages (use git grep), listing files (use git ls-files), showing whole functions verbatim.

Always include `context`. End prompt with: `"Be as brief as possible, no preamble."`

### `run_test`
Run a Python script in `/tmp` — never touches the project. State a hypothesis first.
```json
{"hypothesis": "TaskQueue.add() doesn't notify the poll loop", "code": "import sys; sys.path.insert(0, '/opt/ai-lab/proj'); ..."}
```
Update **Findings** with the result.

### `patch_file`
Edit an existing file via SEARCH/REPLACE. Sub-agent sees the full file and produces an exact block to find and replace. Returns the diff on success.

```json
{
  "path": "/opt/ai-lab/proj/module.py",
  "instruction": "In _poll_loop: currently queries DB once at startup. Change to re-query every self.config['poll_interval'] seconds using asyncio.sleep."
}
```

Instruction needs: exact function name, current behaviour, new behaviour, variable names/API calls involved. Verify with `spawn_agent` after. If wrong, call again with a more precise instruction — do not fall back to `write_file`.

### `write_file`
Generate a brand new file via sub-agent. **Only for files that don't exist yet.** Never use this to modify an existing file — it overwrites completely and silently drops all other functions.

```json
{"path": "/abs/path/to/new_file.py", "prompt": "..."}
```

Prompt must be fully self-contained — all signatures, imports, behaviour rules, logging setup.

## Protocol

### Step 1 — Initialise the Working Document
```json
[
  {"section": "Project", "content": "repo, language, framework"},
  {"section": "Task", "content": "what we are doing"},
  {"section": "Scope", "content": "which module(s) and files"}
]
```
Then: `run_shell` → `git ls-files` to confirm file locations.

### Step 2 — Read the SPEC, explore surgically
```json
{"prompt": "Summarise purpose, exports, key behaviour rules. Be brief.", "file_path": "/opt/ai-lab/<proj>/modules/<mod>/SPEC.md"}
```
Follow with targeted `spawn_agent` calls. After each → update **Findings**. Use `git grep` for symbol searches, `run_test` to confirm hypotheses.

### Step 3 — Write the plan
Name exact functions. Write as a checklist → `update_document` → **Plan**.

### Step 4 — Apply changes
- `patch_file` for edits to existing code
- `write_file` for new files only
- `run_python` for scaffolding stubs

After each: mark step `[done]` in **Plan**, then **stand by** for user to review and confirm unless told to commit automatically.

### Step 5 — Verify
When the user says "done", "applied", "ok" — do NOT re-issue the patch. Proceed directly to verification: `spawn_agent` to confirm the behaviour changed as intended, then update **Plan**.

---

## Rules
- Constants and URLs → `config.toml`. No hardcoding.
- Python deps: ask user to run `uv pip install --python ./venv/bin/python <pkg>`.
- Every module: `log = logging.getLogger("<module-name>")`, no `print()`.
- Project dir default: `/opt/ai-lab/<project-name>` unless specified.
