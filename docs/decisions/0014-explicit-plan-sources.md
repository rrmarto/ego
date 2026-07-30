# ADR-0014: Plan accepts explicit decisions, text, or workspace files

Status: accepted

## Context

ADR-0013 required an accepted Decision Record before planning. That is correct
when Ego helped make the decision, but it forces unnecessary deliberation when
the user already knows what should be built. Guessing whether a positional
value is a Decision identifier would also let a mistyped identifier become an
implementation instruction.

## Decision

Plan accepts exactly one explicitly selected source mode:

- positional text: `ego plan "instruction"`;
- one or more accepted records: `ego plan --decision <id>`;
- one UTF-8 workspace file: `ego plan --file <path>`.

`--decision` may be repeated. Ego never infers a source kind from the text.
Direct text and file content are bounded before any provider call; files must
resolve inside the canonical workspace. Each human source receives a generated
brief identifier and is snapshotted with its origin and creation time.

Planning still uses one selected participant and one normal model call. Missing
material choices become open questions rather than new model-made decisions.
The correction call still omits the full source context.

Portable artifacts contain `plan.md`, `sources.json`, and `manifest.json`.
`sources.json` replaces ADR-0013's decision-specific `decisions.json` and holds
either accepted Decision snapshots or explicit human-source snapshots. Plans
without decisions keep an empty Decision relation in SQLite and use the
embedded source snapshot as their durable provenance.

## Consequences

- Already-decided work can go directly to Plan without paying for Decision.
- Source selection remains organized and mistakes fail closed.
- Builders receive the same portable source provenance regardless of origin.
- Ego still plans and records only; it does not implement the result.
