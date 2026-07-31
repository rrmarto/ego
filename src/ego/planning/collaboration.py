from __future__ import annotations

from ego.agents.base import AgentInput
from ego.agents.runtime import AgentRuntime, NoParticipantsError
from ego.models import (
    FinalPlanAssembly,
    JointPlanDraft,
    PlanAudit,
    PlanDraft,
    PlanPhase,
    PlanSource,
    ToolPolicy,
    TurnRequest,
    WorkspaceContext,
)
from ego.participants import Participant
from ego.planning.assembly import qualify_audit


class PlanCollaboration:
    """Runs Plan's participant stages while PlanWorkflow owns the result."""

    def __init__(self, runtime: AgentRuntime) -> None:
        self.runtime = runtime

    async def independent_plans(
        self,
        run_id: str,
        request: AgentInput,
        sources: list[PlanSource],
        workspace_context: WorkspaceContext,
        active: dict[str, Participant],
    ) -> dict[str, PlanDraft]:
        tools = (
            ToolPolicy()
            if workspace_context.manifest.sufficient
            else ToolPolicy.local_read_only()
        )
        results = await self.runtime.parallel(
            run_id,
            PlanPhase.INDEPENDENT,
            {
                participant_id: (
                    participant,
                    self._turn_request(
                        run_id,
                        request,
                        sources,
                        PlanPhase.INDEPENDENT,
                        tools=tools,
                        workspace_context=workspace_context,
                    ),
                )
                for participant_id, participant in active.items()
            },
        )
        return {
            participant_id: result.payload
            for participant_id, result in results.items()
            if isinstance(result.payload, PlanDraft)
        }

    async def joint_draft(
        self,
        run_id: str,
        request: AgentInput,
        sources: list[PlanSource],
        workspace_context: WorkspaceContext,
        candidates: dict[str, PlanDraft],
        participant_id: str,
        participant: Participant,
    ) -> JointPlanDraft:
        results = await self.runtime.parallel(
            run_id,
            PlanPhase.JOINT_DRAFT,
            {
                participant_id: (
                    participant,
                    self._turn_request(
                        run_id,
                        request,
                        sources,
                        PlanPhase.JOINT_DRAFT,
                        workspace_context=workspace_context,
                        plan_candidates=candidates,
                    ),
                )
            },
        )
        result = results.get(participant_id)
        if result is None or not isinstance(result.payload, JointPlanDraft):
            raise NoParticipantsError("the rotating author did not produce a joint plan")
        return result.payload

    async def author_audits(
        self,
        run_id: str,
        request: AgentInput,
        sources: list[PlanSource],
        workspace_context: WorkspaceContext,
        candidates: dict[str, PlanDraft],
        joint: JointPlanDraft,
        active: dict[str, Participant],
    ) -> tuple[dict[str, PlanAudit], list[str]]:
        results = await self.runtime.parallel(
            run_id,
            PlanPhase.AUTHOR_AUDIT,
            {
                participant_id: (
                    active[participant_id],
                    self._turn_request(
                        run_id,
                        request,
                        sources,
                        PlanPhase.AUTHOR_AUDIT,
                        workspace_context=workspace_context,
                        plan_author_id=participant_id,
                        own_plan=draft,
                        joint_plan=joint,
                    ),
                )
                for participant_id, draft in candidates.items()
            },
        )
        audits = {
            participant_id: qualify_audit(participant_id, result.payload)
            for participant_id, result in results.items()
            if isinstance(result.payload, PlanAudit)
        }
        return audits, sorted(set(candidates) - set(audits))

    async def final_assembly(
        self,
        run_id: str,
        request: AgentInput,
        sources: list[PlanSource],
        workspace_context: WorkspaceContext,
        joint: JointPlanDraft,
        audits: dict[str, PlanAudit],
        participant_id: str,
        participant: Participant,
    ) -> FinalPlanAssembly | None:
        results = await self.runtime.parallel(
            run_id,
            PlanPhase.FINAL_ASSEMBLY,
            {
                participant_id: (
                    participant,
                    self._turn_request(
                        run_id,
                        request,
                        sources,
                        PlanPhase.FINAL_ASSEMBLY,
                        workspace_context=workspace_context,
                        joint_plan=joint,
                        plan_audits=audits,
                    ),
                )
            },
        )
        result = results.get(participant_id)
        return (
            result.payload
            if result is not None and isinstance(result.payload, FinalPlanAssembly)
            else None
        )

    @staticmethod
    def _turn_request(
        run_id: str,
        request: AgentInput,
        sources: list[PlanSource],
        phase: PlanPhase,
        *,
        tools: ToolPolicy | None = None,
        workspace_context: WorkspaceContext | None = None,
        plan_candidates: dict[str, PlanDraft] | None = None,
        plan_author_id: str | None = None,
        own_plan: PlanDraft | None = None,
        joint_plan: JointPlanDraft | None = None,
        plan_audits: dict[str, PlanAudit] | None = None,
    ) -> TurnRequest:
        return TurnRequest(
            run_id=run_id,
            phase=phase,
            question=request.question,
            workspace=request.workspace,
            agent_id="plan",
            workflow_id="plan",
            tool_policy=tools or ToolPolicy(),
            plan_sources=sources,
            workspace_context=workspace_context,
            plan_candidates=plan_candidates or {},
            plan_author_id=plan_author_id,
            own_plan=own_plan,
            joint_plan=joint_plan,
            plan_audits=plan_audits or {},
        )
