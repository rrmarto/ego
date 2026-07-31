# ADR-0015: Collaborative Plan assembly preserves every author's contribution

Status: accepted

## Context

ADR-0013 and ADR-0014 optimized Plan to one participant and one normal provider
call. That produces a bounded artifact, but it removes Ego's defining value:
independent multi-model work with preserved disagreement. Asking one provider
to plan through Ego offers little advantage over invoking that provider
directly.

Letting every model reconcile every plan avoids unilateral synthesis but makes
context consumption quadratic. A single reconciler is cheaper, but it can
silently omit a contribution before peers examine the result.

## Decision

Plan requires at least two available participants and uses all configured
participants unless the user explicitly restricts the set.

The workflow has four typed stages:

1. `plan_draft`: every participant independently creates a workspace-grounded
   plan in parallel.
2. `plan_joint_draft`: one rotating author receives every normalized plan and
   creates a joint candidate plus coverage for every qualified source task.
3. `plan_author_audit`: every original author compares the joint candidate with
   its own plan in parallel and returns self-contained structured criticisms.
4. `plan_final_assembly`: only when criticism exists, a rotating participant
   different from the joint author applies compatible changes and records one
   disposition for every critique.

An applied disposition explicitly names every task and plan-level section it
changes. Removing a joint variant also requires that disposition to name the
variant as resolved. Untargeted tasks and sections must remain equivalent in
the typed record, and an omitted variant remains unresolved.

`applied` means that the criticism is fully incorporated. A material criticism
cannot be marked applied by adding a new open question. If the correctness-
affecting issue remains undecided, final assembly must return it as an explicit
variant; response validation rejects a silent deferral and deterministic
blocking validation protects historical or normalized records. Every new
non-material open question is attributed exactly once to its disposition so
the invariant is checked per criticism rather than inferred globally.

Only the first stage may read or search the workspace. Later stages use compact
structured context and no tools. Corrections do not repeat that context.

Ego completes missing task coverage and critique dispositions
deterministically. It does not semantically reconcile, vote, or infer that
silence means agreement. An unmapped or variant contribution, missing author
audit, unapplied material criticism, or explicit variant becomes a blocking
issue. Final assembly also blocks if it changes an untouched joint task or plan
section, removes a task without an applied criticism, or adds a task not mapped
by an applied disposition. A plan with blocking issues may be inspected or
rejected but cannot be approved. Material choices must return to human
resolution or a new accepted Decision source.

The allowed plan-level targets are title, objective, scope, constraints,
non-goals, affected areas, validation, risks, and open questions. This keeps
global criticisms representable without inventing a task target and prevents a
final assembler from rewriting unrelated sections.

Independent plans, the joint candidate, audits, final assembly, variants, and
blockers are stored in the immutable plan result. The portable artifact remains
focused on construction: final `plan.md`, frozen `sources.json`, and
`manifest.json` with participant and blocker metadata.

## Token and authority bounds

For `N` participants the normal path makes `2N + 1` calls: `N` independent
plans, one joint candidate, and `N` audits. Final assembly adds one call only
when criticism exists. Parallel stages share latency barriers, and no stage
causes an unbounded model loop.

The joint author proposes but cannot finalize silently. Original authors audit
only their own contribution against the candidate, so context grows linearly
rather than every model reading every plan again. The final assembler is
different from the joint author and cannot make a criticism disappear because
Ego verifies the disposition identifiers.

## Consequences

- Plan again provides multi-model value distinct from invoking one provider.
- Independent framing is preserved before any shared candidate anchors review.
- Every author receives an explicit opportunity to recover omitted work.
- Unresolved disagreement blocks approval instead of being hidden by synthesis.
- The common path costs more than ADR-0013 but remains bounded and linear.
- TUI execution remains a separate interface increment over this stable
  workflow contract.
