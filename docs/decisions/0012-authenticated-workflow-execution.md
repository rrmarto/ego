# ADR-0012: Authenticated workflow execution over Ego Service

Status: accepted

## Context

ADR-0010 introduced the authenticated IPv4 loopback service with only
`diagnostic` and `schema`. ADR-0011 made its lifecycle exclusively Ego-owned so
launchd, rather than a sandboxed client, remains the service parent. A native
application now needs to start and observe real Ego workflows without spawning
Ego, invoking provider CLIs, reading SQLite, parsing TUI output, or duplicating
workflow and Seatbelt policy.

The existing versioned bridge already streams committed `WorkEvent` values and
typed immutable results. Reimplementing that behavior in the TCP transport
would create a second execution authority. Coupling workflow lifetime to one
socket would also make a client disconnect an implicit cancellation and could
leave completed work unavailable to other Ego interfaces.

## Decision

Ego Service adds authenticated, explicitly typed workflow methods to service
protocol version 1. This is an additive extension: existing `diagnostic` and
`schema` requests and responses remain unchanged. An incompatible envelope or
authentication change would require a later protocol version.

The execution method is `run.start`. Its closed request contract contains only:

- an explicit `decision` or `investigate` `agent_id`;
- a non-empty question;
- an absolute workspace path;
- selected participant identifiers.

There is no automatic routing, command, argv, shell, arbitrary context,
attachment, transparency setting, provider method, or generic dispatch field.
Ego resolves the real workspace, validates participant identifiers, dispatches
through `AgentRegistry`, applies the existing participant and Seatbelt checks,
and persists the existing immutable Decision or Investigation result.

An authenticated `run.start` connection receives:

```text
authentication_challenge
accepted
event*
result | error | cancelled
```

Every post-authentication frame carries the client's `request_id`. Events embed
the existing public `WorkEvent` and are eligible for live delivery only after
their SQLite transaction commits. The result embeds the typed `FinalDecision`
and its `decision_id`, or the typed `InvestigationReport`; raw provider output,
private rationale, raw file paths, credentials, and internal SQLite columns are
excluded.

Exactly one workflow may be active in one Ego Service process. A second
`run.start` receives the typed, retryable `service_busy` error. The active run
is keyed by `request_id` until its persisted `run_id` becomes available from
the first committed event.

Cancellation is explicit through `run.cancel` on a separate authenticated
connection. It names only the active `request_id`. A valid cancellation
propagates to the workflow task, which persists `RunStatus.INTERRUPTED`; the
original stream ends with `cancelled` if it remains connected. Unknown or
already terminal identifiers are rejected predictably.

The workflow task belongs to Ego Service rather than to the client socket.
Losing the streaming connection detaches its bounded live-event sink but does
not cancel the task. Ego continues the run, persists its events and terminal
state, and later service history methods recover that durable state. A service
process restart is not resumable execution: persisted state remains
authoritative, but in-memory work is lost and must not be described as resumed.

Execution behavior is provided by one focused in-process runtime shared with
`ego bridge`. The service does not spawn the bridge and does not copy Decision
workflow logic into the TCP server. SQLite remains behind `Database`; service
transport models are dedicated public read and stream contracts.

Decision execution is delivered first. The explicit `investigate` agent then
uses the same coordinator and frames. Global run history, event replay, and
human Decision transitions are separate additive methods over `Database`, not
client-owned state or parallel workflow implementations.

## Threat model and limits

ADR-0010 authentication, nonce binding, constant-time comparison, message
limits, timeouts, and same-user limitations remain in force. ADR-0011 lifecycle
ownership remains unchanged. Workflow methods increase what an authenticated
same-user client may ask Ego to do, so their allowlist and Pydantic contracts
are the authorization boundary.

The service may observe, deliberate, recommend, and record. It cannot implement
a recommendation, modify the workspace, execute an accepted Decision, expose a
generic process surface, weaken Seatbelt, or reinterpret an `unsafe`
participant as available.

## Consequences

- Sandboxed applications can request and observe real Decision and Investigation
  workflows while Ego and launchd retain process, workflow, participant,
  safety, and persistence ownership.
- Existing diagnostic clients remain compatible with service protocol v1.
- Only one provider-using workflow consumes resources at a time.
- Client disconnect and explicit cancellation have distinct semantics.
- Live delivery may be incomplete after disconnect or backpressure; committed
  SQLite events remain the source of truth for recovery.
- Investigation, history, event replay, and human transitions must use this
  same infrastructure rather than parallel implementations.
