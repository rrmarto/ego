# ADR-0018: Plan freezes author-discovered workspace evidence

Status: accepted

## Context

ADR-0016 builds a useful shared seed context, but its bounded fragments cannot
prove that every file or symbol needed by a plan is present. ADR-0017 tried to
recover missing context after independent planning by extracting paths and
identifier-like anchors from model prose. That made incomplete evidence and
model assumptions inputs to another heuristic selection pass. Multiple authors
could therefore share the same blind spot, guess the same incorrect location,
and remain unable to inspect the workspace once the seed context was considered
sufficient.

## Decision

The initial `WorkspaceContext` is orientation rather than a completeness gate.
Every independent Plan author always retains the existing Seatbelt-protected
local read, glob, grep, and search capabilities. Web, shell, writes, plugins,
MCP, delegation, project commands, tests, and builds remain prohibited.

Every independent task that affects an existing file must cite that exact file
through a bounded `workspace_evidence` record containing a relative path, line
range, explanation, and at least one relevant symbol visible in the fragment.
Ego rejects excluded paths, invalid ranges, oversized fragments, missing
symbols, and existing affected files without direct evidence. Normal structured
response correction gives the independent author one focused opportunity to
reinspect and repair rejected evidence without repeating the full seed context.

After independent planning, Ego validates every citation against the canonical
workspace, hashes it, assigns a stable `CTX` identifier, deduplicates identical
ranges, and freezes the union in memory. Existing seed evidence explicitly used
by a task joins the same frozen set. Joint drafting, author audits, and final
assembly receive only that shared frozen evidence plus the compact project map,
sources, and structured planning records. They have no workspace tools and may
not introduce new evidence.

The joint and final drafts must preserve direct evidence for every existing
affected file. Unsupported independent evidence becomes a deterministic
blocking issue if a custom participant bypasses normal response validation.
Every frozen file hash is revalidated before artifact creation. The manifest
records discovered evidence identifiers and bytes; artifact manifest version 6
adds those fields while historical versions remain readable.

ADR-0017's prose-derived adaptive anchor selection and unresolved-anchor
blocking are removed from the active workflow. Its historical manifest fields
remain defaulted in the model so version 5 artifacts and stored plans remain
readable.

## Consequences

- Authors can inspect the exact definitions, callers, tests, and contracts they
  need instead of guessing from a shared excerpt.
- Multiple models no longer multiply one centrally selected blind spot.
- Later stages share one deduplicated, validated evidence packet and cannot
  diverge through independent rediscovery.
- Citation validation proves source integrity and symbol presence, not the full
  semantic interpretation; author audits still challenge meaning and scope.
- Independent calls may consume more tokens for focused reads, but failed runs
  caused by missing context should decrease and later-stage evidence remains
  shared and bounded per citation.
- Context remains ephemeral. Ego writes no cache and retains only evidence
  metadata in the exported manifest.
