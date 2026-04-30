import os
import glob
import re
import logging
from session_manager import SessionManager
from config_loader import get_config

log = logging.getLogger("skill-engine")

GENERIC_DOC_PROTOCOL = """
## Working Document Protocol

You have a Working Document that persists across this entire conversation.
Use it as your live memory — update it continuously, not just at the start.

### Sections
- **Goal** — what the user is ultimately trying to achieve
- **Status** — current state: what's done, what's in progress, what's blocked
- **Key Facts** — important constraints, decisions, values, or context the user has given
- **Open Items** — things that need doing, clarifying, or following up

### When to update — be proactive, not reactive

Update the document when:
- The user states a new goal or task → set **Goal**, add to **Open Items**
- You complete something the user asked for → mark it done in **Status**, remove from **Open Items**
- The user says something was wrong or asks for a correction → revert **Status**, re-add to **Open Items**
- The user provides new information or a constraint → add to **Key Facts**
- The user changes direction → update **Goal** and **Status** to reflect the new direction
- The user confirms something is good → note it in **Status**

### How to update

Call `update_document` with the section name and its full new content.
Replace the entire section — don't append. Keep entries concise (bullet points).

```
update_document: {"section": "Status", "content": "- Drafted intro paragraph — approved by user\n- Working on section 2"}
```

Do not wait for a reminder. If something changed, update the document immediately.
"""


class SkillEngine:
    def __init__(self, session_manager: SessionManager):
        self.session_manager = session_manager
        self.skills = {}
        self.triggers = {}
        self._load_skills()

    def _load_skills(self):
        config = get_config()
        skills_cfg = config.get("skills", {})
        
        skills_dir_cfg = skills_cfg.get("dir", "/opt/ai-lab/skills")
        if isinstance(skills_dir_cfg, str):
            self.skills_dir = [skills_dir_cfg]
        else:
            self.skills_dir = skills_dir_cfg

        self.max_skills = skills_cfg.get("max_skills", 2)
        self.min_score = skills_cfg.get("min_score", 0.15)

        self.skills = {}
        self.triggers = {}
        for directory in self.skills_dir:
            for path in glob.glob(os.path.join(directory, "*.md")):
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
        # Ensure live reloading of skills and config
        self._load_skills()
        
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
        newly = {n for n, s in scores.items() if s >= self.min_score}
        
        current_active = self.session_manager.get_active_skills(session_id)
        
        if newly - current_active:
            log.info(f"[skill-engine] activating: {newly - current_active}")
        
        # Update state
        combined_active = current_active | newly
        if len(combined_active) > self.max_skills:
            top = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            combined_active = {n for n, _ in top[:self.max_skills]}
        
        self.session_manager.update_skills(session_id, combined_active)
        log.info(f"[skill-engine] active: {combined_active}")

        # Build skill injection text
        if combined_active:
            blocks = [f"## Active Skill: {n}\n\n{self.skills[n]}" for n in combined_active if n in self.skills]
            skill_text = (
                "# Active workflow skills for this conversation:\n\n"
                + "\n\n---\n\n".join(blocks)
                + "\n\n---\n\nThese skills remain active for the entire conversation.\n\n"
            )
            # Only inject generic doc protocol if no active skill already covers update_document
            skill_content_combined = " ".join(self.skills.get(n, "") for n in combined_active)
            needs_generic_doc = "update_document" not in skill_content_combined
        else:
            skill_text = ""
            needs_generic_doc = True

        if needs_generic_doc:
            doc_text = GENERIC_DOC_PROTOCOL
        else:
            doc_text = ""

        injection = doc_text + skill_text
        if not injection:
            return messages

        new_messages = list(messages)
        for i, msg in enumerate(new_messages):
            if msg["role"] == "system":
                new_messages[i] = dict(msg)
                new_messages[i]["content"] = injection + msg["content"]
                return new_messages

        new_messages.insert(0, {"role": "system", "content": injection})
        return new_messages
