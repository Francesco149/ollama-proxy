"""
session_manager.py

Branch-aware, disk-persisted shadow context, working document, and skill
state for the proxy.

Context is keyed by the hash of all PRIOR user messages (excluding the
current incoming one). Branching is automatic: editing any prior message
changes the key, creating a fresh independent branch.

Working Document
────────────────
A structured markdown document (sections: Project, Task, Scope, Findings,
Plan, Decisions, Open Questions) maintained per session. Injected as the
first system message on every turn. The model updates it via the
update_document tool. Old tool results can be evicted from the message
history because findings are captured in the document first.

Eviction
────────
After each completed iteration, old turns beyond the keep window are
compressed: role=tool results and command-output user turns become single
summary lines. The model's prose turns are kept verbatim. This keeps the
context lean without losing any information that hasn't already been
extracted into the working document.
"""

import hashlib
import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("session-manager")

# ── working document defaults ─────────────────────────────────────────────────

_DEFAULT_DOC_SECTIONS = [
    "Project",
    "Task",
    "Scope",
    "Findings",
    "Plan",
    "Decisions",
    "Open Questions",
]

_EMPTY_DOC: dict[str, str] = {s: "" for s in _DEFAULT_DOC_SECTIONS}

_EVICTABLE_ROLES = {"tool"}  # roles whose content is replaced on eviction


def _user_text(m: dict) -> str:
    content = m.get("content", "")
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if p.get("type") == "text")
    return str(content)


def _user_fingerprint(user_messages: list[dict]) -> str:
    return "\n---\n".join(_user_text(m) for m in user_messages)


def _estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate: 1 token ≈ 4 chars of text content."""
    total = 0
    for m in messages:
        c = m.get("content") or ""
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if p.get("type") == "text")
        total += len(str(c)) // 4
    return total


def _first_line(text: str, max_len: int = 120) -> str:
    line = (text or "").strip().split("\n")[0][:max_len]
    return line + ("…" if len((text or "").strip().split("\n")[0]) > max_len else "")


def evict_old_turns(
    messages: list[dict],
    keep_turns: int = 8,
    token_budget: int = 12000,
) -> list[dict]:
    """
    Compress message history to stay within budget.

    Always keeps:
      - All system messages (working doc lives here)
      - The last `keep_turns` assistant+tool pairs verbatim

    For older turns beyond the keep window:
      - role=tool → "[Result evicted: <first line>]"
      - role=user with <command-output> → "[Output evicted]"
      - role=assistant prose → kept (the model's reasoning is still useful)

    If still over token_budget after one pass, evicts one more window.
    """
    if not messages:
        return messages

    # Separate system messages (always kept at front)
    system_msgs = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]

    if _estimate_tokens(rest) <= token_budget and len(rest) <= keep_turns * 2:
        return messages

    # Find the keep boundary — last keep_turns assistant messages
    assistant_indices = [i for i, m in enumerate(rest) if m.get("role") == "assistant"]
    if len(assistant_indices) <= keep_turns:
        return messages  # not enough history to evict anything

    evict_before = assistant_indices[-keep_turns]

    evicted = []
    for i, m in enumerate(rest):
        if i >= evict_before:
            evicted.append(m)
            continue

        role = m.get("role", "")
        content = m.get("content") or ""

        if role == "tool":
            preview = _first_line(str(content))
            evicted.append({**m, "content": f"[Result evicted: {preview}]"})

        elif role == "user" and "<command-output>" in str(content):
            evicted.append({**m, "content": "[Output evicted]"})

        else:
            evicted.append(m)  # keep assistant prose and regular user turns

    result = system_msgs + evicted

    # Second pass if still over budget
    if _estimate_tokens(result) > token_budget:
        log.warning(
            f"still over budget after eviction "
            f"(~{_estimate_tokens(result)} tokens), evicting one more window"
        )
        result = evict_old_turns(result, keep_turns=max(2, keep_turns // 2), token_budget=token_budget)

    log.debug(
        f"eviction: {len(messages)} → {len(result)} messages, "
        f"~{_estimate_tokens(result)} tokens"
    )
    return result


def render_working_doc(doc: dict[str, str]) -> str:
    """Render the working document as a markdown string for injection."""
    lines = ["# Working Document\n"]
    for section in _DEFAULT_DOC_SECTIONS:
        content = doc.get(section, "").strip()
        lines.append(f"## {section}")
        lines.append(content if content else "_not yet filled_")
        lines.append("")
    return "\n".join(lines)


class SessionManager:
    def __init__(self, persist_path: str | None = None):
        self.persist_path = Path(persist_path) if persist_path else None
        # {key: {"messages": [...], "skills": [...], "doc": {...}}}
        self._store: dict[str, dict] = {}
        if self.persist_path and self.persist_path.exists():
            self._load()

    # ── context keys ─────────────────────────────────────────────────────────

    def context_key(self, messages: list[dict]) -> str:
        """Hash of all user messages EXCEPT the last (incoming) one."""
        users = [m for m in messages if m["role"] == "user"]
        prior = users[:-1]
        return hashlib.sha256(_user_fingerprint(prior).encode()).hexdigest()[:24]

    def next_context_key(self, messages: list[dict]) -> str:
        """Hash of ALL current user messages — key for the next turn's lookup."""
        users = [m for m in messages if m["role"] == "user"]
        return hashlib.sha256(_user_fingerprint(users).encode()).hexdigest()[:24]

    def get_session_id(self, messages: list[dict]) -> str:
        users = [m for m in messages if m["role"] == "user"]
        if not users:
            return "default"
        return hashlib.sha256(_user_text(users[0]).encode()).hexdigest()[:16]

    # ── skill state ───────────────────────────────────────────────────────────

    def get_active_skills(self, key: str) -> set[str]:
        return set(self._store.get(key, {}).get("skills", []))

    def update_skills(self, key: str, new_skills: set[str]) -> None:
        entry = self._store.setdefault(key, {"messages": [], "skills": [], "doc": dict(_EMPTY_DOC)})
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
        entry = self._store.setdefault(key, {"skills": [], "doc": dict(_EMPTY_DOC)})
        entry["messages"] = list(messages)
        if "doc" not in entry:
            entry["doc"] = dict(_EMPTY_DOC)
        log.info(f"context {key}: initialised ({len(messages)} messages)")
        self._save()

    def append_clean(self, key: str, *messages: dict) -> None:
        if not self.has_clean_context(key):
            log.warning(f"context {key}: append_clean before init — ignoring")
            return
        self._store[key]["messages"].extend(messages)
        log.debug(
            f"context {key}: +{len(messages)}, "
            f"total={len(self._store[key]['messages'])}"
        )
        self._save()

    def evict(
        self,
        key: str,
        keep_turns: int = 8,
        token_budget: int = 12000,
    ) -> None:
        """Run eviction pass on the stored context for this key."""
        if not self.has_clean_context(key):
            return
        before = self._store[key]["messages"]
        after = evict_old_turns(before, keep_turns=keep_turns, token_budget=token_budget)
        if len(after) < len(before):
            self._store[key]["messages"] = after
            self._save()

    def register_next(self, from_key: str, to_key: str) -> None:
        if from_key not in self._store:
            return
        if to_key == from_key:
            return
        self._store[to_key] = {
            "messages": list(self._store[from_key]["messages"]),
            "skills":   list(self._store[from_key].get("skills", [])),
            "doc":      dict(self._store[from_key].get("doc", _EMPTY_DOC)),
        }
        log.debug(f"context {from_key} → {to_key} registered for next turn")
        self._save()

    # ── working document ──────────────────────────────────────────────────────

    def get_doc(self, key: str) -> dict[str, str]:
        return dict(self._store.get(key, {}).get("doc", _EMPTY_DOC))

    def update_doc_section(self, key: str, section: str, content: str) -> bool:
        """
        Update one section of the working document.
        Returns True if the section is valid, False otherwise.
        """
        valid = [s.lower() for s in _DEFAULT_DOC_SECTIONS]
        # Accept both exact case and lowercase
        matched = next(
            (s for s in _DEFAULT_DOC_SECTIONS if s.lower() == section.lower()),
            None,
        )
        if not matched:
            log.warning(f"update_doc: unknown section {section!r}")
            return False
        if key not in self._store:
            self._store[key] = {"messages": [], "skills": [], "doc": dict(_EMPTY_DOC)}
        if "doc" not in self._store[key]:
            self._store[key]["doc"] = dict(_EMPTY_DOC)
        self._store[key]["doc"][matched] = content.strip()
        log.info(f"context {key}: doc[{matched}] updated ({len(content)} chars)")
        self._save()
        return True

    def get_doc_rendered(self, key: str) -> str:
        return render_working_doc(self.get_doc(key))

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
            # Back-fill doc field for sessions created before this version
            for key, entry in self._store.items():
                if "doc" not in entry:
                    entry["doc"] = dict(_EMPTY_DOC)
            log.info(f"loaded {len(self._store)} session(s) from {self.persist_path}")
        except Exception as e:
            log.warning(f"session load failed ({e}) — starting empty")
            self._store = {}
