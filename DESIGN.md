# ollama-proxy — Behavioral Specification

This document defines the formal behavioral logic of the `ollama-proxy` system. It serves as the authoritative specification for state management, tool orchestration, and service discovery.

---

## 1. Session Identity & Persistence

The system maintains conversation state through a deterministic session identification mechanism.

### 1.1 Session ID Generation
- **Trigger:** The first message in a conversation where `role == "user"`.
- **Algorithm:** `SHA-256` hash of the raw string content of that message.
- **Truncation:** The first 16 characters of the hex digest are used as the `session_id`.
- **Scope:** This ID is used as the primary key for all stateful lookups in the `SessionManager`.

### 1.2 Skill Accumulation (Set Union)
- **Mechanism:** The `SkillEngine` performs continuous scoring of incoming messages against the local skill library.
- **State Update:** When a skill is triggered, the `SessionManager` performs a **Set Union** operation:
  `active_skills[session_id] = active_skills[session_id].union(newly_detected_skills)`
- **Persistence:** Skills are accumulated over the lifetime of the session. Once a skill is added to the session set, it remains active for all subsequent messages in that session until the proxy process restarts or the session is explicitly cleared.

---

## 2. Tool Interception & Streaming Logic

The proxy acts as an interceptor between the LLM (llama.cpp) and the local execution environment.

### 2.1 Buffer-and-Trigger Mechanism
- **Detection:** The proxy monitors the incoming SSE (Server-Sent Events) stream from the LLM.
- **Buffering:** If a chunk contains `delta.tool_calls`, the proxy enters a "Buffering State." It accumulates the `function.name` and `function.arguments` (JSON string) across subsequent chunks.
- **Trigger Condition:** The trigger occurs when the LLM emits a `finish_reason` of `"tool_calls"` or when the stream terminates while in the "Buffering State."

### 2.2 Execution Flow
1. **Parse:** The accumulated JSON string is parsed into a structured dictionary.
2. **Execute:** The `tool_manager` is invoked with the parsed arguments.
3. **Stream Result:** The output of the tool execution is captured and streamed back to the client (Open-WebUI) as a standard assistant text message, ensuring the user sees the tool's result immediately.

---

## 3. Registration Handshake (Service Discovery)

The system utilizes a dynamic registration pattern to connect external shell environments to the proxy.

### 3.1 UDP Discovery Protocol
- **Mechanism:** The `shell_server` (or external agent) broadcasts its presence via a UDP discovery packet on a predefined port.
- **Handshake:**
    1. **Discovery:** The `tool_manager` listens for incoming UDP broadcast packets.
    2. **Registration:** Upon receiving a valid packet, the `tool_manager` extracts the service URL.
    3. **Binding:** The URL is stored in the global `shell_url` state via `set_shell_url()`.
- **Purpose:** This allows the proxy to dynamically know where to route `register_shell` requests and manual command executions without hardcoded configuration changes.

---

## 4. Summary of Architectural Constraints

| Component | Responsibility | Logic Pattern |
| :--- | :--- | :--- |
| **SessionManager** | State Authority | Deterministic Hashing & Set Union |
| **SkillEngine** | Context Injection | Trigger-Coverage Scoring |
| **Proxy Streamer** | Protocol Translation | Buffer-and-Trigger Interception |
| **ToolManager** | Command Execution | UDP-based Registration Handshake |
