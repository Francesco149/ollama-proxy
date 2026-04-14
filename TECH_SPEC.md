# TECH_SPEC.md: ollama-proxy Refactor

## Current State
- **Monolith:** `proxy.py` handles routing, vision translation, skill injection, and tool execution.
- **Skill Logic:** Hardcoded or "vibe-coded" injection logic that lacks clear separation from the main request flow.
- **Scalability:** Low. Adding new skills or tools requires modifying the core proxy logic.

## Target Architecture (Modular)
The goal is to move from a single file to a modular service where `proxy.py` acts only as a thin routing layer.

### Module Breakdown
- `proxy.py`: Main entry point. Handles basic Ollama API routing, version checks, protocol translation for embeddings, and provides the `/register_shell` endpoint for tool configuration.
- `skill_engine.py`: 
    - **Intent Detection:** Middleware that scans prompts for skill triggers.
    - **Lifecycle Management:** Manages skill activation and session persistence.
- `vision_module.py`: Translates Ollama image formats to OpenAI multipart requests.
- `tool_manager.py`: Intercepts tool calls and manages local execution. Includes `ingest_url` for knowledge base management and `run_shell` for remote command execution.
- `session_manager.py`: Handles session persistence and active skill state across the conversation.

## Implementation Phases
1. **Phase 1: Extraction.** Move vision and tool logic into standalone modules. (COMPLETED)
2. **Phase 2: Skill Engine.** Implement the `SkillEngine` with a dedicated trigger-detection middleware. (COMPLETED)
3. **Phase 3: Session Refactor.** Transition skill state management to the `SessionManager`. (COMPLETED)
4. **Phase 4: Cleanup.** Strip `proxy.py` down to a clean router.

## Constraints
- Must maintain 100% compatibility with the Ollama API for Open-WebUI.
- Must follow the "Interface-First" principle to ensure modules are easily testable.
