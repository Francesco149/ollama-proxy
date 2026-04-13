import os
import glob
import re
import logging
from session_manager import SessionManager

log = logging.getLogger("skill-engine")

SKILLS_DIR = "/opt/ai-lab/skills"
MAX_SKILLS = 2
MIN_SCORE = 0.15

class SkillEngine:
    def __init__(self, session_manager: SessionManager):
        self.session_manager = session_manager
        self.skills = {}
        self.triggers = {}
        self._load_skills()

    def _load_skills(self):
        self.skills = {}
        self.triggers = {}
        for path in glob.glob(os.path.join(SKILLS_DIR, "*.md")):
            name = os.path.basename(path).replace(".md", "")
            with open(path) as f:
                content = f.read()
            self.skills[name] = content
            match = re.match(r'^---\s*\ntriggers:\s*(.+?)\n---', content, re.DOTALL)
            self.triggers[name] = (
                set(match.group(1).strip().lower().split()) if match
                else set(content[:300].lower().split())
            )

    def score(self, message: str, trigger_words: set) -> float:
        words = message.lower().split()
        msg_words = set(words)
        bigrams = {f"{words[i]} {words[i+1]}" for i in range(len(words)-1)}
        overlap = (msg_words | bigrams) & trigger_words
        return len(overlap) / max(len(trigger_words), 1)

    def process_message(self, messages: list) -> list:
        # Extract last user message for scoring
        last_user = None
        for m in reversed(messages):
            if m["role"] == "user":
                content = m["content"]
                if isinstance(content, str):
                    last_user = content
                elif isinstance(content, list):
                    last_user = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
                break
        
        if not last_user:
            return messages

        session_id = self.session_manager.get_session_id(messages)
        
        # Calculate scores
        scores = {name: self.score(last_user, self.triggers[name]) for name in self.skills}
        log.info(f"[skill-engine] session={session_id} scores={scores}")

        # Determine newly activated skills
        newly = {n for n, s in scores.items() if s >= MIN_SCORE}
        
        current_active = self.session_manager.get_active_skills(session_id)
        
        if newly - current_active:
            log.info(f"[skill-engine] activating: {newly - current_active}")
        
        # Update state
        combined_active = current_active | newly
        if len(combined_active) > MAX_SKILLS:
            top = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            combined_active = {n for n, _ in top[:MAX_SKILLS]}
        
        self.session_manager.update_skills(session_id, combined_active)
        log.info(f"[skill-engine] active: {combined_active}")

        if not combined_active:
            return messages

        # Prepare injection
        blocks = [f"## Active Skill: {n}\n\n{self.skills[n]}" for n in combined_active if n in self.skills]
        injection = (
            "# Active workflow skills for this conversation:\n\n"
            + "\n\n---\n\n".join(blocks)
            + "\n\n---\n\nThese skills remain active for the entire conversation.\n\n"
        )

        new_messages = list(messages)
        for i, msg in enumerate(new_messages):
            if msg["role"] == "system":
                new_messages[i] = dict(msg)
                new_messages[i]["content"] = injection + msg["content"]
                return new_messages
        
        new_messages.insert(0, {"role": "system", "content": injection})
        return new_messages
