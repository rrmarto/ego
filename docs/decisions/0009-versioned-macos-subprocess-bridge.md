# ADR-0009: Versioned macOS subprocess bridge

Status: accepted

## Context

A native macOS client needs live progress, structured results, cancellation,
and historical run identifiers. Coupling it to SQLite, TUI text, provider CLIs,
or workflow internals would duplicate authority and make migrations unsafe.

## Decision

Ego exposes `ego bridge` as a versioned subprocess protocol. A client writes one
JSON request to standard input with `protocol_version`, `request_id`,
`agent_id`, `question`, an absolute workspace path, and optional participant
identifiers. An empty participant list selects every configured participant.
The client reads newline-delimited JSON frames from standard output.

The frame sequence starts with `accepted`, continues with zero or more `event`
frames, and ends with exactly one `result`, `error`, or `cancelled` frame.
Events embed the public `WorkEvent` and are published only after persistence.
Results embed the immutable Decision or Investigation result already stored on
the run. The executable contract is discoverable through
`ego bridge --schema`.

The bridge dispatches through `AgentRegistry`; it does not contain workflow or
provider logic. Agent selection is explicit, so this does not implement the
future orchestrator. Standard output is reserved for protocol frames. Native
clients may collect standard error as diagnostics but must not parse it as
state.

Cancelling the subprocess propagates cancellation to the workflow. The workflow
persists an interrupted status before the terminal `cancelled` frame is
emitted.

## Consequences

- A macOS app can render live stages and typed results without reading SQLite.
- Existing CLI, TUI, agents, workflows, participants, Seatbelt enforcement, and
  persistence remain the execution authority.
- `protocol_version` permits incompatible contract changes to be rejected
  explicitly.
- The first integration assumes an installed `ego` executable. Bundling,
  XPC, a daemon, automatic routing, and direct decision-resolution commands are
  separate future decisions.
