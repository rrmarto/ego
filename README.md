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
uv tool install .
```

## First deliberation

Start Ego's interactive environment from the directory you want it to inspect:

```bash
cd /path/to/project
ego
```

Write a question directly to deliberate with every available participant. The
session remains open for further questions. Use `/help` to see internal commands,
`/summon codex claude -- <question>` to select participants, `/cd <path>` to
change workspace, and `/exit` to leave.

The existing subcommands remain available for scripts and one-off use:

```bash
ego doctor
ego ask "Should this project keep its current module boundaries?" --dir .
ego summon "Review the authentication architecture" --participant codex --participant claude
```

`ask` uses every enabled and available participant. `summon` accepts repeated
`--participant` flags or presents a selector in an interactive terminal.

Inspect persisted results:

```bash
ego runs
ego inspect RUN_ID --mode discussion
ego decisions
ego show DECISION_ID
ego decisions accept DECISION_ID --note "Proceed with this direction"
```

## Safety model

Ego gives each CLI its native read-only flags and wraps it with macOS Seatbelt,
denying writes to the target directory. If that external protection cannot be
verified, the participant is reported as `unsafe` and is not executed. Ego
never changes workspace permissions and never creates a workspace copy.

Raw provider output may contain project excerpts. It is stored in the user's
application data directory and removed after 30 days. Structured records remain
until explicitly deleted.

See [docs/architecture.md](docs/architecture.md) for the complete contract.
