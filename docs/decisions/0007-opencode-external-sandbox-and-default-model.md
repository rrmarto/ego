# ADR-0007: OpenCode uses an isolated external boundary and its default model

Status: accepted

## Context

OpenCode exposes a non-interactive `run` command, JSON event output, provider
authentication, model selection, and usage metrics that fit Ego's participant
contract. It can route requests to many provider and local models.

OpenCode's permission system is not a security sandbox. It also merges global,
project, plugin, MCP, agent, command, tool, and instruction configuration. Using
the inspected workspace as OpenCode's project root could therefore execute
configuration that belongs to the target rather than to Ego.

Ego previously excluded Ollama and other local-model paths in ADR-0001. OpenCode
cannot preserve its normal default-model semantics if Ego rejects a default
solely because it resolves to a local provider.

## Decision

Ego adds OpenCode as an external-only participant. The adapter declares
`requires_external_sandbox`, and the shared runner refuses to launch it without
Ego's verified Seatbelt wrapper.

Each call creates temporary HOME and XDG directories. The adapter copies only:

- the private OpenCode authentication file;
- the model-selection state;
- `model`, `provider`, and provider allow/deny settings from global OpenCode
  configuration.

The temporary configuration disables updates, sharing, plugins, MCP servers,
inherited agents, commands, skills, shell, edits, delegation, and web tools.
OpenCode runs with `--pure` from a neutral temporary project directory. During
independent reasoning and peer review it may read, list, glob, and grep only the
target workspace. Later phases receive no tools.

Ego does not pass `--model` by default. OpenCode resolves its model using its
normal hierarchy: global configuration, recent selection, then internal
priority. An explicit `[participants.opencode].model` remains an optional Ego
override. The selected OpenCode model may be remote or local; Ego still does not
call a provider API or Ollama directly.

OpenCode JSON text events remain subject to Ego's phase-specific Pydantic
validation and one corrective retry. Ego records OpenCode-reported token counts
but not OpenCode's calculated price as confirmed provider billing.

## Consequences

- Clients can use the model already selected in OpenCode without duplicating its
  provider configuration in Ego.
- OpenCode may resolve to the same underlying model as another participant.
  Its presence represents another CLI execution path, not guaranteed model
  diversity.
- OpenCode cannot load target-workspace plugins, MCP servers, or provider
  configuration because its project root is the temporary neutral directory.
- Global executable OpenCode customization is intentionally discarded.
- A malformed global OpenCode configuration is reported as misconfigured unless
  Ego provides an explicit model override.
- Normal tests use synthetic output. A credentialed real OpenCode boundary test
  is opt-in and never part of the default suite.
- This decision narrows ADR-0001's Ollama exclusion: Ego still has no Ollama
  adapter, but OpenCode may choose a local model as its own default.
