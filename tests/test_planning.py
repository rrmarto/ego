from __future__ import annotations

import json
from pathlib import Path

import pytest

from ego.models import (
    AvailabilityStatus,
    Confidence,
    FinalDecision,
    ParticipantAvailability,
    ParticipantTurnResult,
    PlanDraft,
    PlanPhase,
    PlanState,
    PlanTask,
    RunStatus,
    ToolPolicy,
    TurnRequest,
)
from ego.planning import PlanArtifactError, PlanArtifactWriter, PlanInput, PlanWorkflow
from ego.prompts import build_prompt
from ego.storage import Database


class PlanParticipant:
    participant_id = "codex"

    def __init__(self) -> None:
        self.requests: list[TurnRequest] = []

    async def probe(self) -> ParticipantAvailability:
        return ParticipantAvailability(
            participant_id=self.participant_id,
            status=AvailabilityStatus.AVAILABLE,
            binary="/fake/codex",
            version="1.0",
        )

    async def respond(self, request: TurnRequest) -> ParticipantTurnResult:
        self.requests.append(request)
        assert request.phase is PlanPhase.DRAFT
        assert request.tool_policy.read
        assert not request.tool_policy.write
        assert request.plan_sources
        draft = PlanDraft(
            title="Add bounded plan artifacts",
            objective="Create one portable Markdown implementation plan.",
            scope=["Add the Plan specialized agent."],
            constraints=["Write only below .ego/plans."],
            non_goals=["Do not implement the accepted decision."],
            affected_areas=["src/ego/planning"],
            tasks=[
                PlanTask(
                    id="T1",
                    title="Create the writer",
                    description="Render and validate the portable artifact.",
                    affected_paths=["src/ego/planning/artifacts.py"],
                    acceptance_criteria=["Traversal outside .ego/plans is rejected."],
                )
            ],
            validation=["Run the focused planning tests."],
        )
        return ParticipantTurnResult(
            participant_id=self.participant_id,
            phase=request.phase,
            payload=draft,
            raw_output=draft.model_dump_json(),
            duration_seconds=0.01,
        )


def accepted_decision(database: Database, workspace: Path) -> str:
    run_id = database.create_run(
        command="ask",
        question="Should Ego create portable plans?",
        workspace=workspace,
    )
    final = FinalDecision(
        run_id=run_id,
        status=RunStatus.COMPLETED,
        recommendation="Create bounded Markdown plan artifacts.",
        supporting_arguments=["Builders need durable accepted context."],
        constraints=["Write only below .ego/plans."],
        non_goals=["Do not implement the plan."],
        confidence=Confidence.MODERATE,
        confidence_reason="The accepted workflow boundary supports this conclusion.",
    )
    database.set_run_status(run_id, RunStatus.COMPLETED, final=final)
    decision_id = database.create_decision(final)
    database.transition_decision(decision_id, "accepted", "Proceed with Markdown first.")
    return decision_id


@pytest.mark.asyncio
async def test_plan_uses_one_call_and_exports_a_portable_markdown_artifact(
    database: Database,
    tmp_path: Path,
) -> None:
    decision_id = accepted_decision(database, tmp_path)
    participant = PlanParticipant()
    workflow = PlanWorkflow(database, {participant.participant_id: participant})

    outcome = await workflow.plan(
        PlanInput(
            question="Create an implementation plan from the accepted decision.",
            workspace=tmp_path,
            participant_ids=["codex"],
            command="plan",
            decision_ids=[decision_id],
            destination=".ego/plans/portable-plan",
        )
    )

    assert len(participant.requests) == 1
    assert outcome.plan.state is PlanState.DRAFT
    artifact = tmp_path / outcome.plan.artifact_path
    assert {item.name for item in artifact.iterdir()} == {
        "plan.md",
        "sources.json",
        "manifest.json",
    }
    assert decision_id in (artifact / "plan.md").read_text(encoding="utf-8")
    sources = json.loads((artifact / "sources.json").read_text(encoding="utf-8"))
    assert sources[0]["conclusion"] == "Create bounded Markdown plan artifacts."
    assert database.get_plan(outcome.plan.plan_id)["state"] == "draft"
    calls = database.get_run(outcome.plan.run_id)["calls"]
    assert len(calls) == 1
    assert calls[0]["phase"] == PlanPhase.DRAFT.value


@pytest.mark.asyncio
async def test_plan_accepts_direct_text_without_a_decision(
    database: Database,
    tmp_path: Path,
) -> None:
    participant = PlanParticipant()
    instruction = "Add a CSV export while preserving the current JSON export."

    outcome = await PlanWorkflow(database, {"codex": participant}).plan(
        PlanInput(
            question="Create a plan from the direct instruction.",
            workspace=tmp_path,
            participant_ids=["codex"],
            command="plan",
            brief=instruction,
        )
    )

    assert len(participant.requests) == 1
    assert outcome.plan.decision_ids == []
    assert outcome.plan.sources[0].source_kind == "text"
    artifact = tmp_path / outcome.plan.artifact_path
    sources = json.loads((artifact / "sources.json").read_text(encoding="utf-8"))
    assert sources[0]["instruction"] == instruction
    assert database.get_plan(outcome.plan.plan_id)["decision_ids"] == []


@pytest.mark.asyncio
async def test_plan_accepts_a_bounded_workspace_file(
    database: Database,
    tmp_path: Path,
) -> None:
    source_file = tmp_path / "docs" / "export.md"
    source_file.parent.mkdir()
    source_file.write_text("Add a CSV export.\n", encoding="utf-8")
    participant = PlanParticipant()

    outcome = await PlanWorkflow(database, {"codex": participant}).plan(
        PlanInput(
            question="Create a plan from docs/export.md.",
            workspace=tmp_path,
            participant_ids=["codex"],
            command="plan",
            brief_file=Path("docs/export.md"),
        )
    )

    source = outcome.plan.sources[0]
    assert source.source_kind == "file"
    assert source.source_path == "docs/export.md"
    assert source.instruction == "Add a CSV export."


@pytest.mark.asyncio
async def test_plan_rejects_a_file_outside_the_workspace_before_provider_use(
    database: Database,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("Do something.", encoding="utf-8")
    participant = PlanParticipant()

    with pytest.raises(ValueError, match="inside the workspace"):
        await PlanWorkflow(database, {"codex": participant}).plan(
            PlanInput(
                question="Create a plan from a file.",
                workspace=workspace,
                participant_ids=["codex"],
                command="plan",
                brief_file=outside,
            )
        )

    assert participant.requests == []


def test_plan_requires_exactly_one_source(tmp_path: Path) -> None:
    common = {
        "question": "Plan it.",
        "workspace": tmp_path,
        "participant_ids": ["codex"],
        "command": "plan",
    }

    with pytest.raises(ValueError, match="exactly one source"):
        PlanInput(**common)
    with pytest.raises(ValueError, match="exactly one source"):
        PlanInput(**common, brief="Plan it.", decision_ids=["decision-1"])


def test_direct_source_is_not_repeated_in_the_prompt(tmp_path: Path) -> None:
    from ego.models import HumanPlanBrief

    instruction = "Add one bounded export command."
    source = HumanPlanBrief(
        source_kind="text",
        brief_id="brief-1",
        instruction=instruction,
        created_at="2026-07-30T00:00:00+00:00",
    )
    request = TurnRequest(
        run_id="run-1",
        phase=PlanPhase.DRAFT,
        question="Create a plan from the direct instruction.",
        workspace=tmp_path,
        agent_id="plan",
        workflow_id="plan",
        tool_policy=ToolPolicy(),
        plan_sources=[source],
    )

    prompt = build_prompt(request)

    assert prompt.count(instruction) == 1


def test_accepted_decision_package_combines_human_state_and_model_context(
    database: Database,
    tmp_path: Path,
) -> None:
    decision_id = accepted_decision(database, tmp_path)

    package = database.get_accepted_decision_package(
        decision_id,
        workspace=tmp_path,
    )

    assert package.conclusion_source == "recommendation"
    assert package.constraints == ["Write only below .ego/plans."]
    assert package.non_goals == ["Do not implement the plan."]
    assert package.human_note == "Proceed with Markdown first."


def test_plan_correction_prompt_does_not_repeat_decision_context(
    database: Database,
    tmp_path: Path,
) -> None:
    decision_id = accepted_decision(database, tmp_path)
    package = database.get_accepted_decision_package(decision_id, workspace=tmp_path)
    request = TurnRequest(
        run_id="run-1",
        phase=PlanPhase.DRAFT,
        question="Create the plan.",
        workspace=tmp_path,
        agent_id="plan",
        workflow_id="plan",
        tool_policy=ToolPolicy(),
        plan_sources=[package],
    )

    prompt = build_prompt(
        request,
        correction="tasks are required",
        previous_response={"title": "Broken"},
    )

    assert "Omitted on correction." in prompt
    assert package.conclusion not in prompt


@pytest.mark.asyncio
async def test_plan_rejects_unaccepted_decisions_before_calling_a_participant(
    database: Database,
    tmp_path: Path,
) -> None:
    run_id = database.create_run(command="ask", question="Question?", workspace=tmp_path)
    decision_id = database.create_decision(
        FinalDecision(
            run_id=run_id,
            status=RunStatus.COMPLETED,
            recommendation="Wait.",
            confidence=Confidence.LOW,
            confidence_reason="The decision has not been accepted.",
        )
    )
    participant = PlanParticipant()

    with pytest.raises(ValueError, match="is not accepted"):
        await PlanWorkflow(database, {"codex": participant}).plan(
            PlanInput(
                question="Plan it.",
                workspace=tmp_path,
                participant_ids=["codex"],
                command="plan",
                decision_ids=[decision_id],
            )
        )

    assert participant.requests == []


@pytest.mark.asyncio
async def test_plan_requires_exactly_one_explicit_participant(
    database: Database,
    tmp_path: Path,
) -> None:
    decision_id = accepted_decision(database, tmp_path)

    with pytest.raises(ValueError, match="exactly one"):
        await PlanWorkflow(database, {}).plan(
            PlanInput(
                question="Plan it.",
                workspace=tmp_path,
                participant_ids=[],
                command="plan",
                decision_ids=[decision_id],
            )
        )


def test_writer_rejects_destinations_and_symlinks_outside_owned_plan_root(
    tmp_path: Path,
) -> None:
    writer = PlanArtifactWriter()
    draft = PlanDraft(
        title="Bounded plan",
        objective="Keep every artifact inside the owned plan root.",
        tasks=[
            PlanTask(
                id="T1",
                title="Validate",
                description="Reject an unsafe destination.",
            )
        ],
    )

    with pytest.raises(PlanArtifactError, match="direct child"):
        writer.write(
            workspace=tmp_path,
            plan_id="plan-1",
            run_id="run-1",
            draft=draft,
            sources=[],
            destination=Path("outside/plan"),
            workspace_git_head=None,
        )

    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    ego_dir = tmp_path / ".ego"
    if ego_dir.exists():
        ego_dir.rmdir()
    ego_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(PlanArtifactError, match="symlink"):
        writer.write(
            workspace=tmp_path,
            plan_id="plan-2",
            run_id="run-2",
            draft=draft,
            sources=[],
            destination=None,
            workspace_git_head=None,
        )


def test_plan_approval_updates_the_portable_manifest_and_append_only_state(
    database: Database,
    tmp_path: Path,
) -> None:
    decision_id = accepted_decision(database, tmp_path)
    run_id = database.create_run(command="plan", question="Plan it.", workspace=tmp_path)
    draft = PlanDraft(
        title="Approval",
        objective="Verify portable and internal approval state.",
        tasks=[PlanTask(id="T1", title="Approve", description="Approve the plan.")],
    )
    plan_id = "plan-approval"
    package = database.get_accepted_decision_package(decision_id, workspace=tmp_path)
    artifact = PlanArtifactWriter().write(
        workspace=tmp_path,
        plan_id=plan_id,
        run_id=run_id,
        draft=draft,
        sources=[package],
        destination=Path(".ego/plans/approval"),
        workspace_git_head=None,
    )
    from ego.models import ImplementationPlan, PlanFormat

    plan = ImplementationPlan(
        plan_id=plan_id,
        run_id=run_id,
        state=PlanState.DRAFT,
        format=PlanFormat.MARKDOWN,
        workspace=tmp_path,
        decision_ids=[decision_id],
        sources=[package],
        artifact_path=artifact.path.relative_to(tmp_path),
        manifest_sha256=artifact.manifest_sha256,
        plan_sha256=artifact.plan_sha256,
        draft=draft,
    )
    database.create_plan(plan)

    manifest_sha256 = PlanArtifactWriter().update_state(
        workspace=tmp_path,
        artifact_path=plan.artifact_path,
        plan_id=plan_id,
        state=PlanState.APPROVED.value,
    )
    database.transition_plan(
        plan_id,
        PlanState.APPROVED,
        "Ready to build.",
        manifest_sha256=manifest_sha256,
    )

    stored = database.get_plan(plan_id)
    manifest = json.loads((artifact.path / "manifest.json").read_text(encoding="utf-8"))
    assert stored["state"] == "approved"
    assert [item["state"] for item in stored["events"]] == ["draft", "approved"]
    assert manifest["state"] == "approved"
