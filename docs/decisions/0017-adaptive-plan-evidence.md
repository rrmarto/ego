# ADR-0017: Plan enriches shared evidence after independent discovery

Status: accepted

## Context

ADR-0016 gives every independent Plan author the same bounded workspace
snapshot. A fixed fragment can contain a referenced type while omitting a
distant consumer in the same file, or contain an implementation while omitting
its caller, compatibility test, configuration key, or persisted contract.
Multiple providers cannot correct a shared blind spot when later stages receive
no new source content.

Giving every later stage unrestricted workspace tools would repeat discovery,
make provider behavior diverge, and increase tokens unpredictably. Sending the
entire initial snapshot again would spend tokens without targeting the newly
discovered gap.

## Decision

After independent plans complete and before the joint draft, Ego performs one
provider-neutral adaptive evidence pass. It derives bounded technical signals
from normalized plan fields:

- existing relative affected paths;
- snake_case, CamelCase, dotted, acronym, backticked, CLI-flag, and function-call
  identifiers;
- technical signals in risks, open questions, validation, tasks, and acceptance
  criteria.

Ego uses those signals to recover additional non-overlapping fragments from
the existing safe workspace catalog. Exact affected paths are considered
first; repository search then finds definitions, uses, tests, configuration,
and other consumers. Multiple fragments from one file are allowed so a distant
definition and consumer can coexist. Sensitive, generated, symlinked,
oversized, binary, and out-of-workspace paths retain ADR-0016 exclusions.

The adaptive pass has independent fixed limits for anchors, files, fragments
per file, fragment lines, per-call bytes, and aggregate later-stage prompt
bytes. Its evidence budget shrinks as the participant count grows so repeating
the fragments across audits remains bounded. It makes no provider call, opens
no tool permission, writes no cache, and runs exactly once. Failure preserves
the initial context and emits a warning rather than failing the Plan.

Independent authors retain the original identical snapshot. Joint drafting,
author audits, and final assembly receive only the adaptive fragment contents
in addition to the existing manifest, project map, sources, and structured
planning records. Prompts require later stages to resolve factual gaps directly
answered by adaptive evidence and to preserve genuine product choices as
variants.

The final context manifest records the initial context identifier, adaptive
evidence identifiers, whether adaptive selection was truncated, hashes, ranges,
and total bytes. Before artifact creation, Ego revalidates every initial and
adaptive file hash. Changed or unreadable evidence is recorded by identifier
and deterministically blocks approval, because independent and collaborative
stages no longer share one coherent workspace snapshot. Evidence content
remains ephemeral. Artifact manifest version 5 adds this metadata while
historical manifests remain readable through defaulted model fields.

## Consequences

- Shared blind spots discovered in independent plans can be checked before
  synthesis without another model round.
- Distant symbols in one file and related definitions, callers, tests, and
  contracts in other files can be supplied together.
- Model-suggested paths cannot escape the existing safe catalog or expand
  workspace authority.
- Workspace drift cannot silently combine stale independent conclusions with
  newer adaptive fragments into an approvable plan.
- Later-stage token growth is bounded by a small adaptive budget rather than
  the complete initial context.
- Adaptive retrieval improves factual grounding but is still heuristic;
  unresolved product choices and unsupported semantic claims remain explicit.
