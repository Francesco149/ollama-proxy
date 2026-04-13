import hashlib
import logging

log = logging.getLogger("session-manager")

class SessionManager:
    def __init__(self):
        # Maps session_id -> set of active skill names
        self.active_skills_sessions: dict[str, set[str]] = {}

    def get_session_id(self, messages: list) -> str:
        for m in messages:
            if m["role"] == "user":
                content = m["content"]
                if isinstance(content, list):
                    content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
                return hashlib.sha256(content.encode()).hexdigest()[:16]
        return "default"

    def get_active_skills(self, session_id: str) -> set[str]:
        return self.active_skills_sessions.get(session_id, set())

    def update_skills(self, session_id: str, new_skills: set[str]):
        if session_id not in self.active_skills_sessions:
            self.active_skills_sessions[session_id] = set()
        
        self.active_skills_sessions[session_id] = new_skills

    def clear_session(self, session_id: str):
        if session_id in self.active_skills_sessions:
            del self.active_skills_sessions[session_id]
