# CONVENTIONS.md

## Core Philosophy
- **No Hackjobs:** Prioritize long-term maintainability and scalability over quick fixes. If a solution feels "dirty," it needs to be refactored immediately.
- **Single Source of Truth (SSOT):** Never duplicate data or logic. Every piece of state, configuration, or business logic must have exactly one authoritative source.
- **SPEC-Driven Development:** For any complex feature or architectural change, a `SPEC.md` file must be created or updated first. The code must strictly adhere to the SPEC.
- **Architecture over Speed:** Always implement clean, decoupled architecture (e.g., Hexagonal, Clean, or Layered architecture) from the start.

## Architectural Standards
- **Separation of Concerns:** Keep domain logic, data access, and presentation layers strictly separated.
- **Modular Design:** Build features as independent modules. High cohesion, low coupling is the goal.
- **Dependency Injection:** Use dependency injection to manage object lifetimes and improve testability.
- **Interface-First:** Define interfaces/contracts before implementing logic.

## Coding Standards
- **Strict Typing:** Use the strongest typing available in the language (e.g., TypeScript `strict` mode, Python type hints, Rust's type system). Avoid `any` or untyped blobs.
- **DRY (Don't Repeat Yourself):** If you find yourself writing the same logic twice, abstract it into a reusable utility or service.
- **Error Handling:** Do not swallow errors. Use structured error handling and ensure errors are caught at the appropriate architectural boundary.
- **Self-Documenting Code:** Variable and function names must be descriptive and unambiguous. Use comments only for "why," not "what."

## Tooling & Environment
- **Standard Libraries:** Prefer well-maintained, industry-standard libraries over custom-built solutions unless there is a clear performance or architectural reason.
- **Automated Testing:** Every core logic component must have corresponding unit tests. Aim for high coverage on business logic, not just boilerplate.
- **Linting & Formatting:** Strictly adhere to the project's established linter and formatter settings.

## Agentic Workflow for Aider
1. **Read the SPEC:** Before writing code, read the relevant `SPEC.md` to understand the intent.
2. **Verify SSOT:** Before creating a new variable or function, check if a similar one already exists.
3. **Refactor as you go:** If a task requires a "quick fix" that violates these conventions, propose a refactor instead.
4. **Check Dependencies:** Ensure new additions do not create circular dependencies.

## Project Context & Hierarchy
To ensure architectural integrity, Aider must prioritize the following files as the absolute source of truth:
1.  **`GDD.md`** or **`DESIGN.md`** if not a game: The "What." Defines gameplay, feel, and aesthetic. If a feature request contradicts the GDD, flag it.
2.  **`TECH_SPEC.md`**: The "How." Defines the technical architecture, patterns (ECS-lite, Pooling, SPOT), and engine usage. **Never violate a pattern defined here for the sake of a "quick fix."**
3.  **`CONVENTIONS.md`**: The "Style." Defines coding standards, naming, and my specific preferences for certain frameworks.

**Workflow Rule:** Before implementing any new feature, Aider must cross-reference `TECH_SPEC.md` to ensure the proposed solution follows the established architectural patterns. If a task requires a new pattern, it must be proposed and added to `TECH_SPEC.md` *before* implementation.
