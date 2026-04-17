# skill_engine

## Purpose
Scores each incoming message against the loaded skill library and injects matching skill content into the system prompt.

## Exports
```python
class SkillEngine:
    def __init__(self, session_manager: SessionManager)
    def process_message(self, messages: list) -> list
```

## Imports From
- `session_manager`: `SessionManager` — for session ID resolution and active skill state
- `config_loader`: `get_config()` — for `skills.dir`, `skills.max_skills`, `skills.min_score`

## Behavior Rules
- `_load_skills()` is called at the start of every `process_message()` to support live skill reloading without a restart
- Skill files are `.md` files found in `config["skills"]["dir"]` (supports a string or list of dirs)
- Trigger words are parsed from `---\ntriggers: ...\n---` frontmatter; falls back to first 300 chars of content split into words
- Score = `|overlap(message_words ∪ message_bigrams, trigger_words)| / |trigger_words|`
- Skills with score ≥ `min_score` are activated; if the active count exceeds `max_skills`, only the top-N by score are kept
- Skill content is injected by prepending to the first `role == "system"` message, or inserting a new system message at index 0 if none exists
- Active skills are accumulated via set union across the session

## Must NOT
- Import from `tool_manager` or `stream_handler`
- Execute or interpret skill content — injection only
- Modify any message other than the system prompt
