# Ego v1 architecture

Ego is a Python CLI and internal deliberation harness. It invokes authenticated
local AI CLIs; it does not call model APIs. The target directory is used in
place and must remain read-only.

## Boundaries

- `cli`: one-off commands, interactive-shell wiring, and rendering.
- `shell`: state and parsing for the interactive decision environment.
- `participants`: provider-specific probing and command construction.
- `runner`: subprocess limits and the external Seatbelt boundary.
- `workspace`: path and evidence validation plus lightweight Git observations.
- `deliberation`: phase barriers, failure handling, synthesis, reconciliation.
- `events`: typed, real-time observation of committed deliberation events.
- `storage`: SQLite migrations, append-only events, raw-output retention.

The harness depends on the `Participant` protocol, not provider classes. HTTP
participants may implement the same protocol in a later version; v1 includes no
HTTP client or provider API configuration.

## Deliberation invariant

All peers receive the same question. They reason independently, peer-review,
then revise their positions. Two rotating peers synthesize. A material conflict
is returned as contested rather than resolved through voting.

Ego requests concise rationale and verifiable citations, never private chains
of thought. Evidence is validated against the real directory when received and
again before the final record is written.

## Safety invariant

An adapter is runnable only when its binary exists, its version exposes the
required native controls, and the macOS Seatbelt probe succeeds. The wrapper
denies writes to both the canonical workspace path and its root entry. There is
no unsafe override in v1.

Because the directory is not frozen, source files can change externally during
a run. Changed citations become stale. Critical stale evidence makes the result
inconclusive; other changes cap confidence and are surfaced to the user.

## Persistence

SQLite stores runs, participants, calls, events, decisions, and decision events.
Raw responses are files referenced by calls and expire after 30 days. Records
are append-only except for derived run and decision status columns, which are
updated in the same transaction as their corresponding event.

Each persisted deliberation event may also be published to an optional in-process
async queue. Persistence always commits before publication. The queue is a live
delivery mechanism for interfaces; SQLite remains the auditable source of truth
and can replay events incrementally by event identifier.
