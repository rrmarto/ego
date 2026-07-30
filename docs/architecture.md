# Ego architecture

Ego is a Python CLI and internal workflow harness. It invokes authenticated
local AI CLIs; it does not call model APIs. The target directory is used in
place and must remain read-only.

## Boundaries

- `cli`: one-off commands, TUI launch wiring, and non-interactive rendering.
- `bridge`: versioned JSON request and JSONL event/result framing for native clients.
- `service`: authenticated loopback transport and typed request/frame dispatch.
- `workflow_execution`: shared in-process bridge/service workflow execution.
- `service_runs`: one-active-run ownership, streaming, disconnect, and cancellation policy.
- `service_history`: bounded public run and event read models.
- `service_decisions`: typed human Decision transitions and resolutions.
- `service_launchd`: Ego-owned per-user LaunchAgent installation and health verification.
- `tui`: full-screen interactive workflow and live event presentation.
- `shell`: legacy line-oriented interaction kept isolated from the harness.
- `agents`: specialized-agent contracts, explicit registry, and common runtime.
- `decision`: compatibility adapter for the existing decision workflow.
- `investigation`: local-only investigation workflow and report finalization.
- `participants`: provider-specific probing and command construction.
- `runner`: subprocess limits and the external Seatbelt boundary.
- `workspace`: path and evidence validation plus lightweight Git observations.
- `deliberation`: phase barriers, failure handling, synthesis, reconciliation.
- `events`: typed, real-time observation of committed deliberation events.
- `storage`: SQLite migrations, append-only events, raw-output retention.

The runtime depends on the `Participant` protocol, not provider classes. Provider
CLIs remain participants rather than specialized agents. HTTP
participants may implement the same protocol in a later version; v1 includes no
HTTP client or provider API configuration.

## Native client bridge

`ego bridge` is the integration boundary for a native macOS client. The client
writes one protocol-versioned request to standard input. Ego resolves the real
workspace, selects the explicitly requested specialized agent, executes its
registered workflow, persists the run, and emits newline-delimited frames on
standard output.

The stream begins with `accepted`, may contain any number of `event` frames, and
ends with exactly one `result`, `error`, or `cancelled` frame. `event` embeds the
same `WorkEvent` contract used by the TUI and is emitted only after its SQLite
transaction commits. The schema is discoverable with `ego bridge --schema`.

The bridge owns neither a second workflow implementation nor a second storage
model. Native clients must not read SQLite, parse TUI text, invoke providers, or
reproduce sandbox policy. They identify the desired agent explicitly; automatic
routing remains deferred. Process cancellation propagates into the active
workflow, which persists the run as interrupted before the bridge emits
`cancelled`.

## Local service

`ego service run` owns the same Ego installation, participant adapters, and
Seatbelt checks as the CLI while running outside a client's App Sandbox. It
binds only to `127.0.0.1` and exposes versioned newline-delimited JSON.

The normal lifecycle is an Ego-owned per-user LaunchAgent installed with
`ego service install`. Its fixed `ProgramArguments` start that same foreground
runtime, and `KeepAlive` lets launchd start and supervise it for the GUI
session. The LaunchAgent captures the effective Ego executable and data
directory at installation time. Repeating install boots out the old job,
atomically refreshes the plist, enables the stable label, bootstraps it, and
validates the server proof. `ego service status` observes the job by
`launchctl print` exit code and verifies the authenticated endpoint;
`ego service uninstall` removes only the known plist.

Roma Desk never starts Ego. It remains a sandboxed authenticated TCP client, so
launchd—not Roma Desk—is the service parent and Ego can apply its mandatory
Seatbelt boundary. There is no `SMAppService`, XPC service, app-bundled helper,
or shell command path. The service remains persistent rather than using socket
activation.

Service protocol v1 keeps the original `diagnostic` and `schema` contracts and
adds closed workflow and recovery methods: `run.start`, `run.cancel`,
`runs.list`, `runs.get`, `runs.events`, `decision.transition`, and
`decision.resolve`. This is additive; existing diagnostic clients continue to
work unchanged. There is still no command, argv, shell, provider, SQLite,
arbitrary context, attachment, or generic dispatch method.

`run.start` requires an explicit `decision` or `investigate` agent, question,
absolute workspace, and participant identifiers. It dispatches through the
same `WorkflowExecutionRuntime` used by `ego bridge`, so the service does not
spawn the bridge or duplicate workflow logic. The connection receives
`accepted`, committed `event` frames, and exactly one typed `result`, `error`,
or `cancelled` terminal frame.

Only one workflow may run in an Ego Service process. A concurrent start returns
the retryable `service_busy` error. The workflow task belongs to Ego rather than
to the socket: disconnecting detaches the bounded live sink without cancelling
the run. `run.cancel` is an explicit request on another authenticated
connection and persists the run as interrupted before the original stream
terminates as cancelled.

History is global across CLI, TUI, bridge, and service because SQLite remains
the source of truth. The service exposes bounded summaries, typed run details,
stable pagination, and incremental committed `WorkEvent` replay. Public models
exclude raw output paths, raw provider text, credentials, and internal SQLite
columns.

Human Decision methods reuse the existing append-only transition and resolution
APIs. A contested result cannot be accepted without selecting one existing
alternative or recording a custom human conclusion. Accepting a recommendation
never executes it.

The diagnostic still invokes participant `probe()` methods and the shared
Seatbelt probe directly. It does not invoke `ego` as a subprocess or create a
second domain or persistence implementation.

Each connection begins with an HMAC-SHA256 challenge. The client verifies that
the server possesses Ego's local service credential before returning a proof
bound to the nonce, protocol version, request identifier, and method. The
credential is generated by Ego, stored under its user-only data directory, and
never crosses the socket. This prevents an unauthenticated local process from
using the service and avoids disclosing the credential to a process that merely
occupies the configured port.

The credential does not defend against compromise of the same macOS user
account, an active process with access to Ego's data directory, or inspection of
an already authenticated client. Roma Desk stores its client copy in Keychain
through an explicit user-controlled setup and validates the server challenge
before sending a request.

External applications consume this same public boundary. They connect only to
the configured IPv4 loopback endpoint, validate the server proof before sending
a request, and decode versioned typed frames. They do not start Ego, invoke
providers directly, open SQLite, or reproduce Ego's domain and sandbox rules.
The integration contract and operational lifecycle are documented in
`docs/external-service.md`.

The shared Seatbelt diagnostic is functional rather than declarative. It first
confirms that a protected file remains readable, then deliberately attempts a
write that must fail. A raw `Operation not permitted` detail is therefore
positive evidence when the structured `safe` field is true.

## Specialized agents and workflows

`SpecializedAgent` declares an identifier, description, input and output
contracts, required capabilities, and exactly one workflow. The explicit
registry exposes `decision` → `DecisionWorkflow` and `investigate` →
`InvestigationWorkflow`. Automatic routing is intentionally absent.

`AgentRuntime` owns participant discovery, mandatory Seatbelt checks, parallel
turns, committed events, call persistence, cancellation propagation,
provider-reported metrics, and corrective attempts. Workflows retain their
stages, schemas, prompts, and tool policies.

## Deliberation invariant

All peers receive the same question. They reason independently, peer-review,
then revise their positions. Two rotating peers synthesize. A material conflict
is returned as contested rather than resolved through voting.

Ego requests concise rationale and verifiable citations, never private chains
of thought. Evidence is validated against the real directory when received and
again before the final record is written.

Citation validation proves only that a path, line range, and content hash match
the inspected workspace. It does not prove the participant's semantic
interpretation. Peer agreement is corroboration, not deterministic proof, and
therefore cannot by itself produce high confidence. Prompts require active
falsification of critical claims and explicit runtime or manifest checks for
version-sensitive conclusions.

A contested model result remains contested. Ego presents its alternatives to
the user and requires a separate human resolution: select an alternative,
record a custom conclusion, defer, or reject. That resolution is appended to
the decision history and never rewrites the original disagreement.

## Investigation invariant

Investigation executes `independent_investigation`, `peer_challenge`,
`investigation_revision`, `cross_synthesis`, and `reconciliation`. The first
three allow only local read, glob, grep, and search. The final two receive
structured context and no tools. Web tools, URL fetching, writes, plugins, MCP,
delegation, project commands, tests, builds, and implementation are outside the
workflow contract. Provider transport remains available because participant
CLIs may use remote models; it is not exposed as a research tool.

Two rotating participants normally produce consolidated reports.
Reconciliation merges matching findings and keeps conflicts under
`disputed_findings`; investigation disagreement never creates alternatives or
a human decision. A backed useful report is `completed`. Insufficient evidence,
critical stale evidence, or material degradation is `inconclusive`.
To make deterministic exact deduplication effective without guessing semantic
similarity, both reconciliators copy canonical claim and hypothesis wording from
the first supplied synthesis only when material claim, scope, conditions, and
state agree. Materially different items remain separate.

Investigation prompts carry the complete material conclusions from prior
stages, while omitting citation hashes used only for local persistence and
integrity checks. Exact duplicate intermediate items are removed. If a
participant returns an invalid but recoverable structured investigation
response, its single corrective attempt receives the prior response and
validation error with all tools disabled; it repairs the record instead of
inspecting the workspace again. A response that cannot be decoded retains the
existing full corrective fallback.

## Safety invariant

An adapter is runnable only when its binary exists, its version exposes the
required controls, and the macOS Seatbelt probe succeeds. The wrapper denies
writes to both the canonical workspace path and its root entry. Native
read-only controls remain active except for documented external-only adapters.
Codex cannot nest its internal macOS Seatbelt inside Ego's external Seatbelt.
OpenCode declares that its permission system is not a security sandbox. Both
adapters therefore declare the external-only requirement and are refused if the
external wrapper is not present. The stricter external profile also denies
writes to durable user and system roots while leaving temporary runtime
locations available.

Each Codex call uses an isolated temporary `CODEX_HOME` with a private
authentication copy. Each OpenCode call uses isolated HOME and XDG directories
with a private authentication copy, model-selection state, and only the
`model`/provider subset of the user's configuration. OpenCode plugins, MCP
servers, agents, commands, and tools are not inherited; its project root is a
neutral temporary directory rather than the inspected workspace. Direct reads
are enabled only when the stage permits them, while `external_directory`
confines file tools to the target workspace and the external Seatbelt boundary
continues to deny writes. OpenCode owns its normal default-model resolution
unless Ego configuration explicitly provides a model override.
Subprocess environments are provider-specific; no participant receives another
provider's credential variables. This changes only processes launched by Ego;
it does not modify global CLI or macOS configuration. There is no
user-selectable unsafe override in v1.

Because the directory is not frozen, source files can change externally during
a run. Changed citations become stale. Critical stale evidence makes the result
inconclusive; other changes cap confidence and are surfaced to the user.

## Persistence

SQLite stores runs, participants, calls, events, decisions, and decision events.
Runs identify their agent, workflow, result kind, and immutable structured
result. Historical runs migrate to `decision` / `decision`. The decisions table
remains exclusive to `DecisionAgent`.
Raw responses are files referenced by calls and expire after 30 days. Records
are append-only except for derived run and decision status columns, which are
updated in the same transaction as their corresponding event.

Calls also persist usage metrics when the provider CLI reports them. Ego records
provider-reported tokens and cost without applying its own pricing estimates;
interfaces must label unavailable metrics rather than inventing comparable
values for providers that expose none. OpenCode token counts are retained, but
its calculated price is not stored as billed provider cost.

Human resolutions are stored as append-only structured records containing the
selected alternative or custom conclusion. A contested decision cannot move to
accepted without one of those records.

Each persisted `WorkEvent` identifies its agent, workflow, and stage and may
also be published to an optional in-process
async queue. Persistence always commits before publication. The queue is a live
delivery mechanism for interfaces; SQLite remains the auditable source of truth
and can replay events incrementally by event identifier.

The interactive timeline may use a completed turn's committed `call_id` to render
the normalized `Position` or `Synthesis` payload for independent proposals,
revisions, cross-syntheses, and reconciliations. These views are collapsed by
default and never expose raw provider output or private chain-of-thought. Raw
peer-review bundles remain persisted for audit but are omitted from the default
timeline because individual model-to-model critiques add substantial noise.

Structured output is not considered successful merely because it matches the
JSON types. Phase-aware validation rejects placeholder syntheses, unsupported
argument identifiers, insubstantial confidence explanations, and reconciliation
records that do not make an explicit equivalence decision. The participant gets
one corrective attempt; repeated failure is recorded as a failed turn and the
engine degrades explicitly instead of treating filler content as disagreement.
