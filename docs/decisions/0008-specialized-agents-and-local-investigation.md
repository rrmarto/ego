# ADR-0008: Specialized agents over reproducible workflows

Status: accepted

## Context

Ego's original engine combined one decision workflow with shared participant
execution concerns. Local investigation needs different contracts and result
semantics without turning provider adapters into roles or changing Decision.

## Decision

Ego adds controlled `SpecializedAgent` contracts and an explicit registry.
`DecisionAgent` delegates to the current behavior through `DecisionWorkflow`.
`InvestigateAgent` delegates to `InvestigationWorkflow`. Both use
`AgentRuntime` for participant probing, mandatory Seatbelt enforcement,
parallel turns, persisted-before-published events, calls, metrics,
cancellation, and corrective attempts.

Investigation uses five stages: independent investigation, peer challenge,
investigation revision, rotating cross-synthesis, and reconciliation. The first
three allow local read and search only. The final two use no tools. Web tools,
URLs, writes, plugins, MCP, delegation, project commands, tests, builds, and
implementation are outside the workflow contract. Provider transport may
remain available to a remote-model CLI but is not exposed as an investigation
or research tool.

Investigation disagreement is stored as disputed findings. It never creates a
decision alternative or human-resolution action. The final
`InvestigationReport` is an immutable run result; the decisions table remains
exclusive to `DecisionAgent`.

The registry exposes explicit discovery and dispatch only. Automatic routing
and an orchestrator are deferred.

## Consequences

- Existing Decision CLI, TUI, phases, records, and resolution behavior remain
  compatible.
- Provider CLIs remain implementations of `Participant`, not specialized agents.
- Runs and events identify their agent, workflow, result kind, and stage.
- Historical runs migrate to the decision agent and workflow.
- A future orchestrator can select an agent and explain why without becoming a
  synthesizer or authority over the result.
