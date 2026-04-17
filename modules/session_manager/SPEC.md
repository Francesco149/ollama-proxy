# session_manager

## Purpose
Sole authority for session identity (deterministic ID) and per-session active skill state.

## Exports
```python
class SessionManager:
    def get_session_id(self, messages: list) -> str
    def get_active_skills(self, session_id: str) -> set[str]
    def update_skills(self, session_id: str, new_skills: set[str])
    def clear_session(self, session_id: str)
```

## Imports From
None — no internal dependencies.

## Behavior Rules
- Session ID: SHA-256 of the first `role == "user"` message content, truncated to 16 hex chars
- Multi-part content (list format) is joined as space-separated text before hashing
- If no user message exists, returns `"default"` as session ID
- `update_skills` performs set union — skills are accumulated, never removed unless `clear_session` is called

## Must NOT
- Import from any other internal module
- Perform any I/O or HTTP calls
- Hold any state beyond `active_skills_sessions: dict[str, set[str]]`
