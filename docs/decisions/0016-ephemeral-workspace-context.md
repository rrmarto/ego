# ADR-0016: Plan shares one ephemeral workspace context

Status: accepted

## Context

ADR-0015 lets every participant create an independent workspace-grounded plan.
Giving each provider a fresh session preserves isolation, but asking all of
them to rediscover the same project instructions and files repeats reads and
tokens. Reusing provider sessions would make behavior provider-specific and
allow context to grow across runs.

A durable generated context directory would avoid repeated discovery but
creates stale files, cleanup policy, and another source of truth inside user
workspaces.

## Decision

Before any Plan provider call, Ego builds one provider-neutral
`WorkspaceContext` in memory. Selection is deterministic and bounded. It
contains:

- applicable `AGENTS.md` instructions and explicitly referenced files;
- a small relevance-ranked sample from referenced documentation directories;
- a compact path map from tracked and non-ignored workspace files;
- query-relevant text fragments with stable evidence identifiers and hashes.

Generated directories, common dependency/build caches, symlinks, oversized
files, environment files, and common credential/key filenames are excluded.
The context has fixed byte, file, catalog, fragment, referenced-directory, and
omission limits. Source paths are preferred after mandatory instructions fit.
Fragment selection scores the complete bounded window instead of accepting the
first weak keyword match.

Every independent author receives the same evidence snapshot. Tasks may cite
only its known evidence identifiers. Later joint, audit, and assembly stages
receive the manifest and project map, not the initial evidence contents again.
ADR-0017 adds one separately bounded adaptive evidence pass after independent
planning; later stages receive only those newly recovered fragment contents.

When all mandatory instructions fit, relevant evidence was selected, and at
least half of the identifier-like query anchors are covered by non-instruction
evidence, participant tools are disabled for the entire Plan run. Commands,
snake_case identifiers, CamelCase symbols, and acronyms are anchors. If the
context is insufficient or construction fails, only independent authors retain
the existing Seatbelt-protected local read/search policy. The manifest records
the fallback reason.

Context evidence contents are never written as a cache. The immutable Plan
result and `manifest.json` retain only identifiers, paths, line ranges, hashes,
bounds, omissions, sufficiency, and fallback metadata. Normal Plan artifacts
remain the only workspace writes.

## Consequences

- Providers share the same initial project evidence without sharing sessions.
- Common runs avoid repeated workspace discovery and reduce input tokens.
- Referenced documentation directories cannot consume the whole evidence
  budget merely because an instruction mentions them.
- Later stages do not pay again for full source fragments.
- Newly discovered technical gaps may be enriched under ADR-0017 without
  changing the identical initial snapshot.
- A run leaves no temporary context files requiring retention or cleanup.
- Evidence hashes make the exported plan auditable without copying source code.
- Large or unusual workspaces fall back safely to protected reads rather than
  silently planning from incomplete instructions.
- Context selection is intentionally heuristic; improving relevance must
  preserve deterministic bounds and provider neutrality.
