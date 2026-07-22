# Ego engineering rules

Read `docs/architecture.md` and the ADRs in `docs/decisions/` before changing
or extending the harness.

- Ego v1 is a decision-support tool. It must never implement a recommendation.
- A target workspace is real user data. Never modify its permissions or contents.
- Every participant must pass the external macOS Seatbelt check before it can
  run. Native read-only controls remain mandatory unless an accepted ADR
  documents that they cannot be nested; such adapters must declare the
  external-only requirement and the runner must refuse an unwrapped launch.
- Provider details stay inside participant adapters. The deliberation engine
  depends only on the `Participant` protocol and Pydantic contracts.
- Preserve disagreement. Do not introduce voting, majority rules, hidden roles,
  or a permanently privileged synthesizer.
- Persist material state changes as append-only events.
- Keep modules focused. Split by responsibility before growing large classes.
- Normal tests must not call providers, install CLIs, or require credentials.
