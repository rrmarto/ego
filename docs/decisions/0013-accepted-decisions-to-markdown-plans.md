# ADR-0013: Accepted decisions produce bounded Markdown plan artifacts

Status: accepted

## Context

Decision deliberation records recommendations, disagreement, evidence, and a
separate human resolution. An implementation agent should not reconstruct or
reconsider that context. A durable implementation plan is useful when work will
start later or outside Ego, but a second five-stage deliberation would repeat
context and spend unnecessary tokens.

ADR-0001 made the inspected workspace fully read-only. Planning now needs one
narrow exception so its final result can be a portable artifact rather than an
internal object awaiting a later export.

## Decision

Ego adds an explicit `PlanAgent` and `PlanWorkflow`. Plan accepts one or more
Decision Records that Ego has resolved and verified as `accepted` for the same
workspace. Ego injects complete normalized decision packages; participants
never read SQLite or resolve identifiers.

Plan uses exactly one explicitly selected participant and one planning stage.
The participant receives only accepted conclusions, material rationale,
constraints, exclusions, risks, evidence without storage hashes, and the
workspace path. It may read and search the workspace but cannot use web, shell,
writes, plugins, MCP, or delegation. Normal structured-output correction may
make one additional call only after an invalid response. Plan does not reopen
alternatives or make missing product decisions.

The first output format is Markdown. A successful run produces a self-contained
directory under:

```text
<workspace>/.ego/plans/<slug>-<plan-id-prefix>/
```

It contains `plan.md`, `decisions.json`, and `manifest.json`. The model returns
only a typed plan draft. A deterministic `PlanArtifactWriter` validates the
destination, rejects traversal and symlinks, writes only the three allowlisted
files through an atomic directory replacement, and records their hashes. The
participant sandbox remains read-only.

The plan and its append-only lifecycle are also recorded in Ego's SQLite
database. Plans begin as `draft` and may become `approved`, `rejected`, or
`superseded`. Approval does not implement the plan. An external Builder may
consume an approved portable artifact without SQLite access.

The first delivery exposes Plan through the CLI and the specialized-agent
registry. Bridge, service, native-client, and automatic Decision-to-Plan routing
require dedicated typed transport changes and are not implied by generic
workflow execution.

## Consequences

- Decision remains responsible for what to do; Plan translates an accepted
  conclusion into implementation work; Ego still does not implement it.
- The only Ego-owned workspace writes are new Plan artifacts below
  `.ego/plans/`; no source, configuration, permission, or ignore file is changed.
- One normal provider call bounds token use and avoids repeating Decision's
  five-stage deliberation.
- Selecting multiple participants is rejected instead of silently privileging
  one or paying for redundant plans.
- Markdown artifacts retain decision snapshots and hashes so another agent can
  build later without Ego's database.
- OpenSpec and other formats are future deterministic renderers over the same
  canonical plan contract.
