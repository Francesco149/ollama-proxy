import hashlib
import logging

log = logging.getLogger("session-manager")


class SessionManager:
    def __init__(self):
        # Maps session_id -> set of active skill names
        self.active_skills_sessions: dict[str, set[str]] = {}

        # Maps session_id -> clean message list for shadow context.
        # These messages are what gets sent to llama.cpp — never the
        # display-artifact-polluted history Open WebUI sends back.
        self.clean_messages: dict[str, list[dict]] = {}

    # ── session identity ──────────────────────────────────────────────────────

    def get_session_id(self, messages: list) -> str:
        for m in messages:
            if m["role"] == "user":
                content = m["content"]
                if isinstance(content, list):
                    content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
                return hashlib.sha256(content.encode()).hexdigest()[:16]
        return "default"

    # ── skill state ───────────────────────────────────────────────────────────

    def get_active_skills(self, session_id: str) -> set[str]:
        return self.active_skills_sessions.get(session_id, set())

    def update_skills(self, session_id: str, new_skills: set[str]):
        if session_id not in self.active_skills_sessions:
            self.active_skills_sessions[session_id] = set()
        self.active_skills_sessions[session_id].update(new_skills)

    def clear_session(self, session_id: str):
        self.active_skills_sessions.pop(session_id, None)
        self.clean_messages.pop(session_id, None)
        log.info(f"session {session_id}: cleared")

    # ── shadow context ────────────────────────────────────────────────────────

    def has_clean_context(self, session_id: str) -> bool:
        return session_id in self.clean_messages

    def get_clean_messages(self, session_id: str) -> list[dict]:
        return list(self.clean_messages.get(session_id, []))

    def init_clean_context(self, session_id: str, messages: list[dict]) -> None:
        """
        Seed the clean context for a new session from the first-turn messages
        (already transformed to OpenAI format by the time proxy calls this).
        """
        self.clean_messages[session_id] = list(messages)
        log.info(f"session {session_id}: clean context initialised ({len(messages)} messages)")

    def append_clean(self, session_id: str, *messages: dict) -> None:
        """Append one or more clean messages to the stored context."""
        if session_id not in self.clean_messages:
            log.warning(f"session {session_id}: append_clean called before init — ignoring")
            return
        self.clean_messages[session_id].extend(messages)
        log.debug(f"session {session_id}: appended {len(messages)} message(s), "
                  f"total={len(self.clean_messages[session_id])}")
