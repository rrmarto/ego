# Ego CLI

Ego asks multiple locally installed AI CLIs to examine a real directory in
read-only mode, challenge one another, and produce an auditable decision record.
It does not implement the recommendation.

## Install for development

```bash
uv sync --dev
uv run ego doctor
```

Install as a tool:

```bash
uv tool install --force --editable .
```

The editable install keeps the global `ego` command linked to the current
source tree while the project is under development.

## First deliberation

Start Ego's interactive environment from the directory you want it to inspect:

```bash
cd /path/to/project
ego
```

Write a question directly to deliberate with every available participant. The
TUI shows participant checks, active phases, per-turn elapsed time, failures,
and the final recommendation. Use `/help` to see interactive commands, `/mode`
to change the transparency level, and `/exit` to leave.

The existing subcommands remain available for scripts and one-off use:

```bash
ego doctor
ego ask "Should this project keep its current module boundaries?" --dir .
ego summon "Review the authentication architecture" --participant codex --participant claude
```

`ask` uses every enabled and available participant. `summon` accepts repeated
`--participant` flags or presents a selector in an interactive terminal.

## Human decision

Model agreement is corroboration, not proof. Ego verifies that cited source
fragments match the workspace, labels that scope explicitly, and caps
model-only consensus at moderate confidence.

When recommendations remain materially different, the TUI displays numbered
alternatives and waits for a human action. Choose an option with the displayed
button or type:

```text
/choose 1
/decide Adopt option 1 after the compatibility check
/defer Need more evidence
/reject The available alternatives are unsafe
```

The same workflow is available non-interactively:

```bash
ego decisions choose <decision-id> 1 --note "Preferred tradeoff"
ego decisions decide <decision-id> "Human-authored conclusion"
```

The human resolution is appended to the decision record; Ego preserves the
original contested result and its disagreements.

Inspect persisted results:

```bash
ego runs
ego inspect RUN_ID --mode discussion
ego decisions
ego show DECISION_ID
ego decisions accept DECISION_ID --note "Proceed with this direction"
```

## Safety model

Ego wraps every CLI with macOS Seatbelt and denies writes to the target
directory. Participants normally retain their native read-only controls. Codex
uses its documented externally-sandboxed mode because macOS cannot nest its
internal Seatbelt inside Ego's boundary; Ego additionally denies that process
writes to durable user and system locations. The invocation uses a temporary
`CODEX_HOME` with a private temporary authentication copy and receives no
credentials belonging to other providers. If the external protection cannot be
verified, the participant is reported as `unsafe` and is not executed. Ego
never changes workspace permissions, global Codex settings, or workspace
contents, and never creates a workspace copy.

Raw provider output may contain project excerpts. It is stored in the user's
application data directory and removed after 30 days. Structured records remain
until explicitly deleted.

See [docs/architecture.md](docs/architecture.md) for the complete contract.
