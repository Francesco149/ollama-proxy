"""
session_manager.py

Branch-aware, disk-persisted shadow context and skill state for the proxy.

Context is keyed by the hash of all PRIOR user messages (excluding the
current incoming one). This makes branching automatic: editing any prior
message changes the key, creating a fresh independent branch.

Turn-to-turn continuity works by registering the current context under
the key the NEXT turn will look up (next_key = hash of ALL current user
messages, including the one we're responding to).
"""

import hashlib
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("session-manager")


def _user_text(m: dict) -> str:
    content = m.get("content", "")
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if p.get("type") == "text")
    return str(content)


def _user_fingerprint(user_messages: list[dict]) -> str:
    return "\n---\n".join(_user_text(m) for m in user_messages)


class SessionManager:
    def __init__(self, persist_path: str | None = None):
        self.persist_path = Path(persist_path) if persist_path else None
        # {key: {"messages": [...], "skills": [str, ...]}}
        self._store: dict[str, dict] = {}
        if self.persist_path and self.persist_path.exists():
            self._load()

    # ── context keys ─────────────────────────────────────────────────────────

    def context_key(self, messages: list[dict]) -> str:
        """
        Key for looking up the stored context for the current request.
        = hash of all user messages EXCEPT the last (incoming) one.
        First turn always produces the same base key (hash of empty string)
        regardless of conversation content.
        """
        users = [m for m in messages if m["role"] == "user"]
        prior = users[:-1]
        return hashlib.sha256(_user_fingerprint(prior).encode()).hexdigest()[:24]

    def next_context_key(self, messages: list[dict]) -> str:
        """
        Key that the NEXT turn will use to look up the context built
        during THIS turn. = hash of ALL current user messages.
        After responding to U_n, we register the updated context here
        so the request for U_{n+1} can find it.
        """
        users = [m for m in messages if m["role"] == "user"]
        return hashlib.sha256(_user_fingerprint(users).encode()).hexdigest()[:24]

    # ── legacy skill-tracking key (hash of first user message) ───────────────

    def get_session_id(self, messages: list[dict]) -> str:
        users = [m for m in messages if m["role"] == "user"]
        if not users:
            return "default"
        return hashlib.sha256(_user_text(users[0]).encode()).hexdigest()[:16]

    # ── skill state ───────────────────────────────────────────────────────────

    def get_active_skills(self, key: str) -> set[str]:
        return set(self._store.get(key, {}).get("skills", []))

    def update_skills(self, key: str, new_skills: set[str]) -> None:
        entry = self._store.setdefault(key, {"messages": [], "skills": []})
        merged = set(entry.get("skills", [])) | new_skills
        entry["skills"] = sorted(merged)
        self._save()

    def clear_session(self, key: str) -> None:
        self._store.pop(key, None)
        log.info(f"context {key}: cleared")
        self._save()

    # ── shadow context ────────────────────────────────────────────────────────

    def has_clean_context(self, key: str) -> bool:
        return bool(self._store.get(key, {}).get("messages"))

    def get_clean_messages(self, key: str) -> list[dict]:
        return list(self._store.get(key, {}).get("messages", []))

    def init_clean_context(self, key: str, messages: list[dict]) -> None:
        """Seed a fresh context (first turn or new branch)."""
        entry = self._store.setdefault(key, {"skills": []})
        entry["messages"] = list(messages)
        log.info(f"context {key}: initialised ({len(messages)} messages)")
        self._save()

    def append_clean(self, key: str, *messages: dict) -> None:
        """Append clean messages (no display artefacts) to stored context."""
        if not self.has_clean_context(key):
            log.warning(f"context {key}: append_clean before init — ignoring")
            return
        self._store[key]["messages"].extend(messages)
        log.debug(f"context {key}: +{len(messages)}, "
                  f"total={len(self._store[key]['messages'])}")
        self._save()

    def register_next(self, from_key: str, to_key: str) -> None:
        """
        Copy the current context to the key the next turn will look up.
        Called after on_clean_turn so the continuation is findable even
        if the process restarts between turns.
        """
        if from_key not in self._store:
            return
        if to_key == from_key:
            return
        self._store[to_key] = {
            "messages": list(self._store[from_key]["messages"]),
            "skills": list(self._store[from_key].get("skills", [])),
        }
        log.debug(f"context {from_key} → {to_key} registered for next turn")
        self._save()

    # ── persistence ───────────────────────────────────────────────────────────

    def _save(self) -> None:
        if not self.persist_path:
            return
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.persist_path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._store, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.persist_path)
        except Exception as e:
            log.error(f"session persist failed: {e}", exc_info=True)

    def _load(self) -> None:
        try:
            with open(self.persist_path, encoding="utf-8") as f:
                self._store = json.load(f)
            log.info(f"loaded {len(self._store)} session(s) from {self.persist_path}")
        except Exception as e:
            log.warning(f"session load failed ({e}) — starting empty")
            self._store = {}
