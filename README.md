# Ego CLI

[![Architecture site](https://github.com/rrmarto/ego/actions/workflows/pages.yml/badge.svg)](https://github.com/rrmarto/ego/actions/workflows/pages.yml)

Ego is a local decision-support harness that asks multiple AI CLIs to inspect the
same workspace, challenge each other's conclusions, and produce an auditable
recommendation. It provides a full-screen terminal interface for interactive
work and stable commands for scripts and one-off checks.

Ego does **not** modify the inspected workspace or implement the recommendation.
When participants disagree materially, it preserves their alternatives and asks
the user to choose, write a different conclusion, defer, or reject the result.

**[Open the interactive architecture map](https://rrmarto.github.io/ego/)**

## Current scope

Ego v0.1.0:

- coordinates locally installed Codex, Claude, Gemini, Copilot, and OpenCode CLIs;
- reads the real workspace under a mandatory macOS Seatbelt boundary;
- runs a five-phase deliberation protocol instead of selecting a result by vote;
- validates cited paths, line ranges, and file fragments against the workspace;
- persists runs, normalized model responses, decisions, plans, and human resolutions;
- creates collaboratively audited Markdown implementation plans from explicit sources under
  the bounded `.ego/plans/` artifact path;
- reports provider usage when a CLI exposes token or cost information;
- exposes authenticated diagnostics, workflow streams, recovery, and human Decision
  actions on IPv4 loopback for sandboxed clients;
- supports interactive inspection through a Textual TUI and JSON output through
  the command line.

This is an early release. The stored decision format is append-only, but the UI
and installation process may still change.

## Requirements

- macOS. Ego v1 relies on `/usr/bin/sandbox-exec` and refuses to run a
  participant if the read-only boundary cannot be verified.
- Python 3.14 or newer.
- [`uv`](https://docs.astral.sh/uv/) for the installation commands below.
- At least one supported AI CLI installed and authenticated. Participants that
  are missing, incompatible, or unsafe are excluded and reported by `doctor`.

Ego invokes existing CLI sessions. It does not configure provider accounts or
call provider HTTP APIs directly.

## Install

Install the current version directly from GitHub:

```bash
uv tool install "git+https://github.com/rrmarto/ego.git"
ego --version
ego doctor
```

For development, use a local clone:

```bash
git clone https://github.com/rrmarto/ego.git
cd ego
uv sync --dev
uv run ego doctor
```

An editable tool installation keeps the global `ego` command linked to the
local source tree:

```bash
uv tool install --force --editable .
```

For a stable background service, prefer the normal global installation. Its
public executable is normally the stable symlink `~/.local/bin/ego`. A local
`.venv/bin/ego` is also supported for development, but it is tied to that
checkout and virtual environment.

## Quick start

Open the interactive interface from the workspace you want Ego to inspect:

```bash
cd /path/to/project
ego
```

Enter a question directly, for example:

```text
Should this project keep its current authentication module boundaries?
```

The TUI shows participant readiness, phase progress, elapsed time, usage data,
expandable normalized responses, the final recommendation, and any action that
still requires a human decision.

Common interactive commands:

| Command | Purpose |
| --- | --- |
| `/help` | Show every interactive command. |
| `/doctor` | Re-run participant and sandbox checks. |
| `/investigate <question>` | Investigate only the active local workspace. |
| `/summon codex opencode -- <question>` | Use selected participants. |
| `/mode standard\|discussion\|expert` | Change the amount of visible detail. |
| `/runs` and `/inspect <run-id>` | Review previous deliberations. |
| `/decisions` and `/show <decision-id>` | Review persisted decisions. |
| `/choose`, `/decide`, `/defer`, `/reject` | Resolve the current decision. |
| `/exit` | Leave Ego. |

## Command-line usage

The Typer commands remain available for scripts and non-interactive use:

```bash
# Ask every enabled and available participant
ego ask "Should we split this service?" --dir .

# Ask explicitly selected participants
ego summon "Review the caching strategy" --dir . \
  --participant codex --participant opencode

# Emit structured output
ego ask "Review this architecture" --dir . --json

# Investigate local evidence without web, commands, or modifications
ego investigate "Why does OpenCode fail?" --dir .
ego investigate "Why does OpenCode fail?" --dir . \
  --participant codex --participant opencode
ego investigate "Why does OpenCode fail?" --dir . --json

# Create a portable Markdown plan from direct text, decisions, or a workspace file
ego plan "Add CSV export" --dir .
ego plan --decision <decision-id> --dir .
ego plan --file docs/export-plan.md --dir .
# Optional restriction; Plan still requires at least two available participants
ego plan "Add CSV export" -p codex -p claude --dir .
ego plans
ego plans approve <plan-id>

# Diagnose adapters, CLI versions, authentication, and sandbox support
ego doctor
ego participants --json

# Inspect recorded work
ego runs
ego inspect <run-id> --mode expert
ego decisions
ego show <decision-id>

# Re-run a decision with new information while preserving its relationship
ego reconsider <decision-id> "The deployment target changed to macOS only"
```

Run `ego --help` or `ego <command> --help` for the complete option reference.

### macOS subprocess bridge

A native macOS client can execute the same agents and workflows through a
versioned subprocess contract. It sends one JSON request on standard input and
reads newline-delimited JSON frames from standard output:

```bash
echo '{"protocol_version":1,"request_id":"mac-1","agent_id":"investigate","question":"Why does OpenCode fail?","workspace":"/absolute/path/to/workspace","participant_ids":[]}' | ego bridge
```

An empty `participant_ids` array selects every configured participant. Frames
are emitted as `accepted`, committed `event` records, and one terminal
`result`, `error`, or `cancelled` record. The current JSON schemas are available
without starting a run:

```bash
ego bridge --schema
```

The client should treat Ego as the authority for execution, persistence,
sandboxing, and results. It must not read Ego's SQLite database or invoke
provider CLIs directly. Sending `SIGINT` to the bridge cancels the active
workflow and persists the run as interrupted.

### Authenticated local service

Sandboxed native clients must not spawn Ego because child processes inherit the
client’s App Sandbox and cannot apply Ego's mandatory Seatbelt profile. Ego can
instead install its own per-user LaunchAgent once:

```bash
ego service install
ego service status
ego service uninstall
```

`service install` is the single user activation. It records the absolute Ego
executable and effective data directory, installs
`~/Library/LaunchAgents/com.rrmarto.ego.service.plist`, and asks launchd to
start and supervise the service during the user's session. Repeating it updates
the recorded path/configuration and restarts the service safely. Roma Desk
never starts Ego or opens a Terminal; it is only an authenticated TCP client.

The service listens only on `127.0.0.1:37645` by default. Configure the port
with `[service].port` in `config.toml`; no host option exists. The foreground
`ego service run` command remains available for development and launchd uses
that exact runtime internally.

Prefer running install from the stable global executable:

```bash
~/.local/bin/ego service install
```

For development, this is also valid:

```bash
/Users/marto/FlutterDev/proyectos/MyApps/ego/.venv/bin/ego service install
```

The global symlink is retained without resolving uv's replaceable internal
environment. If the repository moves or `.venv` is rebuilt, run
`ego service install` again from the new development executable. Status reports
a stale recorded path. Uninstall removes only the known plist; credentials,
configuration, SQLite data, logs, and the Ego installation remain.

Standard output and error are separate private files in Ego's data directory:

```text
service.stdout.log
service.stderr.log
```

Use `ego service status` to print their full paths. The plist uses a closed GUI
`PATH`, preserves the effective `EGO_DATA_DIR`, and never contains the service
credential.

Ego creates a 256-bit credential in its application-data directory with
user-only permissions. Print it for an explicit client setup, or rotate it:

```bash
ego service token
ego service token --regenerate
```

Treat this output as a secret. The service never receives the token itself over
TCP. Each connection starts with an `authentication_challenge` frame. The
client validates:

```text
HMAC-SHA256(token, "server:" + nonce)
```

and sends an authentication proof bound to its request:

```text
HMAC-SHA256(token, "client:" + nonce + ":1:" + request_id + ":" + method)
```

Version 1 preserves `diagnostic` and `schema` and adds a closed set of typed
methods:

- `run.start` and `run.cancel` for explicit Decision or Investigation workflows;
- `runs.list`, `runs.get`, and `runs.events` for global recovery;
- `decision.transition` and `decision.resolve` for append-only human actions.

Only one workflow runs at a time. A disconnected stream does not cancel its
Ego-owned task; the client recovers committed state through history and events.
There is no generic command, argv, shell, provider, or SQLite method, and no
service action implements a recommendation.

The complete request and frame schemas are available without starting the
server:

```bash
ego service schema
```

External application authors should use the complete
[Ego Service integration guide](docs/external-service.md). It documents
LaunchAgent ownership, lifecycle states, the HMAC handshake, streaming,
cancellation, history, Decision actions, client responsibilities, logs, and
stable versus editable installations.

## Deliberation protocol

Every available participant receives the same question and follows the same
protocol:

1. **Independent reasoning** — each participant produces its own structured
   position and cites relevant workspace evidence.
2. **Peer review** — participants challenge claims, missing evidence, and risks
   in the other positions.
3. **Position revision** — each participant updates or explicitly preserves its
   position after the review.
4. **Cross synthesis** — two rotating peers create independent syntheses from
   the revised material.
5. **Reconciliation** — the synthesizers determine whether the results are
   materially equivalent or still contested.

There is no majority vote and no permanently privileged model. Invalid or
insubstantial structured responses receive one corrective attempt; repeated
failures are recorded and the run degrades explicitly.

`InvestigateAgent` uses a separate five-stage workflow: independent
investigation, peer challenge, investigation revision, two rotating
cross-syntheses, and reconciliation. Its first three stages may only read and
search the local workspace; the final two use no tools. The immutable report
keeps findings, hypotheses, disputes, unknowns, and next checks. It never creates
a decision or human-resolution action.

Citation verification confirms that a referenced path, line range, and content
hash match the inspected workspace. It does not prove that the model interpreted
that source correctly. For this reason, model agreement alone cannot produce
high confidence.

## Human decision loop

A recommendation is stored separately from the user's final action. For a
contested result, accept one of the recorded alternatives or provide a custom
conclusion:

```bash
ego decisions choose <decision-id> 1 --note "Preferred compatibility tradeoff"
ego decisions decide <decision-id> \
  "Keep the current boundary until the migration test is complete"
```

For any decision, the user can also record its operational state:

```bash
ego decisions accept <decision-id> --note "Approved for planning"
ego decisions defer <decision-id> --note "Need runtime evidence"
ego decisions reject <decision-id> --note "Risk is not acceptable"
```

These actions append a new event. They do not rewrite the original model result,
disagreements, or evidence.

Plan can translate direct instructions, a workspace file, or accepted Decision
Records without repeating the five-stage deliberation:

```bash
ego plan "Add CSV export" --dir .
ego plan --decision <decision-id> --dir .
ego plan --file docs/export-plan.md --dir .
ego plans approve <plan-id>
```

Plan resolves and snapshots the explicit source, then builds one bounded
workspace context in memory for all participants. Independent plans receive
the evidence contents once; later stages receive only its manifest and project
map. If that context is insufficient, independent participants retain protected
read/search as a fallback. Ego creates no context cache: only evidence hashes,
paths, bounds, and fallback metadata survive in the plan.

Plan then builds a rotating joint candidate and lets every original author
audit it. Final assembly runs only when criticism exists. Ego writes `plan.md`,
`sources.json`, and `manifest.json` below `.ego/plans/`. The participant remains
read-only; a deterministic writer owns that narrow artifact path. Approval
records readiness for an external Builder but never implements the plan.
Unmapped contributions, missing audits, unapplied material criticism, and
variants block approval.

## Architecture

The TUI and command-line interface depend on the same application services.
`DecisionAgent`, `InvestigateAgent`, and `PlanAgent` select reproducible
workflows through an explicit registry and share an `AgentRuntime`. Plan uses
bounded parallel stages and at least two participants. The runtime works
through the `Participant` contract, so provider flags,
authentication checks, and output parsing remain inside their adapters.

```mermaid
flowchart LR
    User[User] --> Interfaces[Textual TUI / Typer CLI / macOS bridge]

    subgraph Harness[Ego deliberation harness]
        Engine[Deliberation engine]
        Contract[Participant contract]
        Adapters[Provider adapters]
        Boundary[Runner + Seatbelt]
        Evidence[Workspace evidence validation]
        Finalization[Finalization + human resolution]
        Events[Committed typed events]
        Store[(SQLite + raw output files)]

        Engine --> Contract --> Adapters --> Boundary
        Engine --> Evidence
        Engine --> Finalization
        Engine --> Events --> Store
        Finalization --> Store
    end

    Interfaces --> Engine
    Events -. live updates .-> Interfaces
    Boundary --> CLIs[Local AI CLIs]
    CLIs --> Workspace[Real workspace · read-only]
    Evidence --> Workspace
    Finalization --> Decision[Recommendation / contested alternatives]
    Decision --> User
```

| Area | Responsibility | Source |
| --- | --- | --- |
| Interfaces | TUI state, timeline, commands, non-interactive rendering, and the versioned JSONL bridge | [`src/ego/tui`](src/ego/tui), [`src/ego/cli.py`](src/ego/cli.py), [`src/ego/bridge.py`](src/ego/bridge.py) |
| Deliberation | Phase barriers, failure handling, synthesis, and reconciliation | [`src/ego/deliberation`](src/ego/deliberation) |
| Participants | Provider probing, command construction, structured output, and usage extraction | [`src/ego/participants`](src/ego/participants) |
| Safety boundary | Subprocess limits, reduced environments, and macOS Seatbelt enforcement | [`src/ego/runner.py`](src/ego/runner.py), [`src/ego/sandbox.py`](src/ego/sandbox.py) |
| Workspace | Canonical path resolution and evidence verification | [`src/ego/workspace.py`](src/ego/workspace.py) |
| Observability | Typed lifecycle events published only after persistence | [`src/ego/events.py`](src/ego/events.py) |
| Persistence | SQLite migrations, append-only records, and raw-output retention | [`src/ego/storage`](src/ego/storage) |

Architecture resources:

- **[Interactive system map](https://rrmarto.github.io/ego/)** — navigable view
  from the TUI through the harness, safety boundary, and human decision loop.
- [Architecture contract](docs/architecture.md) — invariants and component
  responsibilities.
- [Architecture Decision Records](docs/decisions) — accepted design decisions
  and their consequences.

## Safety and data handling

Every participant must pass binary, capability, and external Seatbelt checks
before execution. Ego also checks authentication when the provider CLI exposes
a non-invasive status command. The wrapper denies writes to the canonical
workspace path. Native read-only controls remain enabled where they can coexist
with that wrapper.

Codex cannot nest its internal macOS Seatbelt inside Ego's boundary. Its adapter
therefore uses Codex's externally sandboxed mode only for the child process
launched by Ego. That process receives a temporary `CODEX_HOME` with a private
authentication copy, while the external profile protects the workspace and
durable user and system locations. Ego does not modify global Codex settings,
workspace permissions, or macOS policy.

OpenCode does not provide a native security sandbox, so its adapter also requires
Ego's external Seatbelt boundary. Each call receives temporary HOME and XDG
directories containing only a private authentication copy, model-selection
state, and a sanitized subset of the user's OpenCode provider configuration.
Plugins, MCP servers, custom agents, commands, and tools are not inherited.
OpenCode runs from a neutral temporary project directory and may only read or
search the target workspace during the evidence phases.

SQLite stores runs, calls, events, decisions, and human resolutions in the
platform application-data directory. Raw provider output may contain workspace
fragments; it is stored separately and removed after 30 days by default. Set
`EGO_DATA_DIR` to use a different data location.

## Configuration

Ego reads `config.toml` from its platform application-data directory. A minimal
example:

```toml
raw_retention_days = 30
output_limit_bytes = 5242880

[service]
port = 37645
max_message_bytes = 65536
request_timeout_seconds = 10
diagnostic_timeout_seconds = 30

[participants.codex]
enabled = true
timeout_seconds = 600

[participants.claude]
enabled = true
timeout_seconds = 600

[participants.gemini]
enabled = false

[participants.copilot]
enabled = false

[participants.opencode]
enabled = true
timeout_seconds = 600
```

Participant-specific binary paths can be set with `binary = "/path/to/cli"`.
OpenCode uses its normal default-model hierarchy: configured model, most recently
used model, then OpenCode's internal priority. Set `model = "provider/model"`
under `[participants.opencode]` only when Ego should override that default.
Run `ego doctor` after any configuration change.

## Development

Normal tests use synthetic participants and do not require provider credentials
or invoke external AI CLIs:

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run mypy
```
