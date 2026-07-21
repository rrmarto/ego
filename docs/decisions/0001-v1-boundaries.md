# ADR-0001: Ego v1 boundaries

Status: accepted

Ego v1 invokes the local Codex, Claude, Gemini, and Copilot CLIs through a shared
participant contract. It reads the real target directory under mandatory native
and external read-only controls. It does not copy that directory, call provider
APIs, use Ollama, execute recommendations, or assign specialized model roles.

Runtime discussions are captured as Ego Decision Records rather than OpenSpec
changes because they describe reasoning and human decisions, not implementation
proposals. Architecture decisions for Ego itself remain versioned ADRs.
