# ADR-0006: Citation scope and human resolution

Status: accepted

## Context

Ego validates cited workspace fragments and asks peers to challenge one
another. A valid fragment does not establish that a model interpreted the code
correctly. Correlated models can repeat the same false claim, so agreement alone
must not be represented as semantic verification.

The protocol also preserves material disagreement as `contested`, but a model
status is not a human decision. Returning alternatives without a resolution
action leaves the user unable to close the decision loop.

## Decision

Newly validated fragments use the explicit `citation_verified` status. The
historical `valid` value remains readable for compatibility. Both mean only
that the path, line range, and hashes match the workspace.

Prompts require peers to try to falsify critical claims and inspect repository
runtime or manifest constraints for version-sensitive conclusions. Agreement
between models is corroboration rather than proof, so a model-only consensus is
capped at moderate confidence until Ego gains a deterministic claim verifier.

A contested final record declares that human resolution is required. The user
may select a numbered alternative, enter a custom conclusion, defer, or reject.
Selecting or authoring a conclusion creates an append-only resolution record
and moves the derived decision state to accepted without modifying the original
contested recommendation or disagreements.

## Consequences

- Ego no longer labels a verified source fragment as a verified semantic claim.
- Multiple models repeating the same error cannot elevate confidence to high by
  agreement alone.
- The protocol can still contain false claims; the interface states the
  verification boundary instead of promising certainty it cannot provide.
- Contested deliberations end in an explicit, auditable human action rather than
  a dead end.
- Existing v1 databases migrate in place and historical decision records remain
  readable.
- Future deterministic verifiers may justify high confidence for the specific
  claims they prove, without changing the human-resolution contract.
