# session_manager

## Purpose
Sole authority for session identity (deterministic ID), per-session active
skill state, and per-session shadow context (clean message history for
llama.cpp, separate from Open WebUI's display-polluted history).

## Exports
```python
class SessionManager:
    # identity
    def get_session_id(self, messages: list) -> str

    # skill state
    def get_active_skills(self, session_id: str) -> set[str]
    def update_skills(self, session_id: str, new_skills: set[str])
    def clear_session(self, session_id: str)

    # shadow context
    def has_clean_context(self, session_id: str) -> bool
    def get_clean_messages(self, session_id: str) -> list[dict]
    def init_clean_context(self, session_id: str, messages: list[dict]) -> None
    def append_clean(self, session_id: str, *messages: dict) -> None
```

## Imports From
None — no internal dependencies.

## State
```python
active_skills_sessions: dict[str, set[str]]
clean_messages: dict[str, list[dict]]
```

## Behavior Rules

### Session identity
- Session ID: SHA-256 of the first `role == "user"` message content,
  truncated to 16 hex chars
- Multi-part content (list format) is joined as space-separated text before hashing
- If no user message exists, returns `"default"` as session ID

### Skill state
- `update_skills` performs set union — skills are accumulated, never removed
  unless `clear_session` is called
- `clear_session` removes both skill state AND clean context for the session

### Shadow context
- `init_clean_context`: seeds a new session with first-turn messages already
  in OpenAI format (called by proxy on first request for a session)
- `append_clean`: extends the stored list; logs a warning and no-ops if called
  before `init_clean_context`
- `get_clean_messages`: returns a shallow copy (list()) to prevent callers
  from mutating stored state
- `has_clean_context`: returns True iff `init_clean_context` has been called
  for this session

## Must NOT
- Import from any other internal module
- Perform any I/O or HTTP calls
- Summarise or transform stored messages
