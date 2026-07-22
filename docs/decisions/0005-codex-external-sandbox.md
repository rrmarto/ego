# ADR-0005: Codex runs in Ego's external Seatbelt boundary

Status: accepted

## Context

Ego previously wrapped `codex exec --sandbox read-only` with its own macOS
Seatbelt profile. Codex implements its native read-only shell boundary with
Seatbelt too. Commands requested by Codex therefore attempted to apply a second
Seatbelt profile from inside Ego's first profile and failed with
`sandbox_apply: Operation not permitted`. Codex could return valid structured
JSON, but it could not independently inspect the workspace.

The Codex CLI provides `--dangerously-bypass-approvals-and-sandbox` specifically
for environments where the caller already supplies the sandbox. Changing the
user's global Codex configuration or weakening every participant would be
broader than necessary.

## Decision

Only the Codex adapter uses the CLI's externally-sandboxed mode. It declares
`requires_external_sandbox`, and the shared runner refuses to spawn it unless
the command is wrapped by Ego's `/usr/bin/sandbox-exec` boundary.

The Codex-specific external profile denies writes to the canonical workspace,
durable user roots, mounted volumes, applications, and system/tool locations.
Temporary runtime locations remain available because the CLI needs scratch
space. Every call receives an isolated temporary `CODEX_HOME`; it contains a
private `0600` copy of the existing authentication file and is removed in a
`finally` block when the call ends or is cancelled. Reads and provider network
access remain available. The environment
allowlist is provider-specific, so Codex cannot inherit Claude, Gemini, or
GitHub credentials. Other participants keep their existing native controls and
external Seatbelt wrapper unchanged.

Ego does not edit Codex configuration, workspace permissions, or macOS policy.
The exception exists only for the lifetime of a Codex child process started by
Ego.

## Consequences

- Codex can inspect the real workspace without attempting nested Seatbelt.
- Workspace writes remain denied by the external boundary.
- Durable user and system locations receive additional write protection for
  this external-only process; temporary locations remain writable.
- Codex global configuration, sessions, caches, and state are not mutated.
- Model-generated commands share the provider connectivity required by the
  external-only Codex process. Provider-specific credential filtering limits
  the impact, but this mode still trusts Codex with workspace reads and its own
  provider connection.
- The runner fails closed if the Codex command is ever routed around Seatbelt.
- Normal tests continue to use synthetic participants. A credentialed real
  Codex boundary check is opt-in and never part of the default suite.
- Future adapters may not reuse this mode without a separate compatibility and
  threat-model review.
