from __future__ import annotations

import json
from pathlib import Path

import pytest

from ego.models import (
    AvailabilityStatus,
    Confidence,
    CritiqueDisposition,
    CritiqueDispositionAction,
    FinalDecision,
    FinalPlanAssembly,
    JointPlanDraft,
    ParticipantAvailability,
    ParticipantTurnResult,
    PlanAudit,
    PlanCoverage,
    PlanCoverageDisposition,
    PlanCritique,
    PlanCritiqueCategory,
    PlanCritiqueSeverity,
    PlanDraft,
    PlanPhase,
    PlanSection,
    PlanState,
    PlanTask,
    PlanVariant,
    PlanWorkspaceEvidence,
    RunStatus,
    ToolPolicy,
    TurnRequest,
    WorkspaceContext,
    WorkspaceContextEvidence,
    WorkspaceContextEvidenceReference,
    WorkspaceContextManifest,
)
from ego.planning import PlanArtifactError, PlanArtifactWriter, PlanInput, PlanWorkflow
from ego.planning.assembly import (
    blocking_issues,
    merge_variants,
    normalize_final_assembly,
    normalize_joint_draft,
)
from ego.planning.context import WorkspaceContextBuilder
from ego.prompts import build_prompt, validate_response
from ego.storage import Database


class PlanParticipant:
    def __init__(self, participant_id: str, *, raises_critique: bool = False) -> None:
        self.participant_id = participant_id
        self.raises_critique = raises_critique
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
        if request.phase is PlanPhase.INDEPENDENT:
            assert request.workspace_context is not None
            assert request.tool_policy == ToolPolicy.local_read_only()
            assert request.plan_sources
            draft = self._draft(f"Independent contribution from {self.participant_id}.")
            affected = request.workspace / draft.tasks[0].affected_paths[0]
            if affected.is_file():
                line_count = len(affected.read_text(encoding="utf-8").splitlines())
                draft.tasks[0].workspace_evidence = [
                    PlanWorkspaceEvidence(
                        path=draft.tasks[0].affected_paths[0],
                        line_start=1,
                        line_end=line_count,
                        explanation="Existing planning writer surface.",
                        symbols=["PlanArtifactWriter"],
                    )
                ]
            payload: PlanDraft | JointPlanDraft | PlanAudit | FinalPlanAssembly = draft
        elif request.phase is PlanPhase.JOINT_DRAFT:
            assert not request.tool_policy.read
            source_task_ids = [
                f"{participant_id}:{task.id}"
                for participant_id, draft in request.plan_candidates.items()
                for task in draft.tasks
            ]
            joint_draft = self._draft("Reconcile every independent contribution.")
            joint_draft.tasks[0].evidence_ids = list(
                dict.fromkeys(
                    evidence_id
                    for draft in request.plan_candidates.values()
                    for task in draft.tasks
                    for evidence_id in task.evidence_ids
                )
            )
            payload = JointPlanDraft(
                draft=joint_draft,
                coverage=[
                    PlanCoverage(
                        source_task_id=source_task_id,
                        disposition=PlanCoverageDisposition.MERGED,
                        target_task_ids=["T1"],
                        rationale="The joint task preserves this contribution.",
                    )
                    for source_task_id in source_task_ids
                ],
            )
        elif request.phase is PlanPhase.AUTHOR_AUDIT:
            assert request.own_plan is not None
            assert request.joint_plan is not None
            payload = PlanAudit(
                criticisms=(
                    [
                        PlanCritique(
                            id="C1",
                            severity=PlanCritiqueSeverity.MATERIAL,
                            category=PlanCritiqueCategory.OMISSION,
                            description="The candidate lost a required validation.",
                            required_change="Add the missing validation to T1.",
                            source_task_ids=["T1"],
                            candidate_task_ids=["T1"],
                        )
                    ]
                    if self.raises_critique
                    else []
                )
            )
        else:
            assert request.phase is PlanPhase.FINAL_ASSEMBLY
            assert request.joint_plan is not None
            critique_ids = [
                item.id for audit in request.plan_audits.values() for item in audit.criticisms
            ]
            payload = FinalPlanAssembly(
                draft=request.joint_plan.draft,
                critique_dispositions=[
                    CritiqueDisposition(
                        critique_id=critique_id,
                        action=CritiqueDispositionAction.APPLIED,
                        target_task_ids=["T1"],
                        rationale="The final task now preserves the criticism.",
                    )
                    for critique_id in critique_ids
                ],
            )
        return ParticipantTurnResult(
            participant_id=self.participant_id,
            phase=request.phase,
            payload=payload,
            raw_output=payload.model_dump_json(),
            duration_seconds=0.01,
        )

    @staticmethod
    def _draft(description: str) -> PlanDraft:
        return PlanDraft(
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
                    description=description,
                    affected_paths=["src/ego/planning/artifacts.py"],
                    acceptance_criteria=["Traversal outside .ego/plans is rejected."],
                )
            ],
            validation=["Run the focused planning tests."],
        )


def plan_participants(
    *participant_ids: str,
    critic: str | None = None,
) -> dict[str, PlanParticipant]:
    return {
        participant_id: PlanParticipant(
            participant_id,
            raises_critique=participant_id == critic,
        )
        for participant_id in participant_ids
    }


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


class FailingContextBuilder(WorkspaceContextBuilder):
    async def build(self, **_: object) -> WorkspaceContext:
        raise OSError("synthetic context failure")


@pytest.mark.asyncio
async def test_plan_collaborates_without_final_assembly_when_audits_are_clear(
    database: Database,
    tmp_path: Path,
) -> None:
    decision_id = accepted_decision(database, tmp_path)
    participants = plan_participants("codex", "claude", "opencode")
    workflow = PlanWorkflow(database, participants)

    outcome = await workflow.plan(
        PlanInput(
            question="Create an implementation plan from the accepted decision.",
            workspace=tmp_path,
            participant_ids=list(participants),
            command="plan",
            decision_ids=[decision_id],
            destination=".ego/plans/portable-plan",
        )
    )

    assert sum(len(item.requests) for item in participants.values()) == 7
    assert outcome.plan.state is PlanState.DRAFT
    assert set(outcome.plan.participant_plans) == set(participants)
    assert set(outcome.plan.audits) == set(participants)
    assert outcome.plan.final_assembly is None
    assert outcome.plan.blocking_issues == []
    artifact = tmp_path / outcome.plan.artifact_path
    assert {item.name for item in artifact.iterdir()} == {
        "plan.md",
        "sources.json",
        "manifest.json",
    }
    assert decision_id in (artifact / "plan.md").read_text(encoding="utf-8")
    sources = json.loads((artifact / "sources.json").read_text(encoding="utf-8"))
    assert sources[0]["conclusion"] == "Create bounded Markdown plan artifacts."
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["artifact_version"] == 6
    assert manifest["participants"] == sorted(participants)
    assert manifest["blocking_issues"] == []
    assert manifest["workspace_context"]["context_id"]
    assert database.get_plan(outcome.plan.plan_id)["state"] == "draft"
    calls = database.get_run(outcome.plan.run_id)["calls"]
    assert len(calls) == 7
    assert [item["phase"] for item in calls].count(PlanPhase.INDEPENDENT.value) == 3
    assert [item["phase"] for item in calls].count(PlanPhase.JOINT_DRAFT.value) == 1
    assert [item["phase"] for item in calls].count(PlanPhase.AUTHOR_AUDIT.value) == 3


@pytest.mark.asyncio
async def test_material_author_critique_triggers_a_different_final_assembler(
    database: Database,
    tmp_path: Path,
) -> None:
    participants = plan_participants("codex", "claude", "opencode", critic="claude")

    outcome = await PlanWorkflow(database, participants).plan(
        PlanInput(
            question="Create a collaborative implementation plan.",
            workspace=tmp_path,
            participant_ids=list(participants),
            command="plan",
            brief="Add a bounded collaborative planning workflow.",
        )
    )

    joint_authors = [
        participant_id
        for participant_id, participant in participants.items()
        if any(request.phase is PlanPhase.JOINT_DRAFT for request in participant.requests)
    ]
    final_authors = [
        participant_id
        for participant_id, participant in participants.items()
        if any(request.phase is PlanPhase.FINAL_ASSEMBLY for request in participant.requests)
    ]
    assert len(joint_authors) == 1
    assert len(final_authors) == 1
    assert joint_authors != final_authors
    assert sum(len(item.requests) for item in participants.values()) == 8
    assert outcome.plan.final_assembly is not None
    assert outcome.plan.final_assembly.critique_dispositions[0].critique_id == "claude:C1"
    assert outcome.plan.blocking_issues == []


@pytest.mark.asyncio
async def test_plan_accepts_direct_text_without_a_decision(
    database: Database,
    tmp_path: Path,
) -> None:
    participants = plan_participants("codex", "claude")
    instruction = "Add a CSV export while preserving the current JSON export."

    outcome = await PlanWorkflow(database, participants).plan(
        PlanInput(
            question="Create a plan from the direct instruction.",
            workspace=tmp_path,
            participant_ids=[],
            command="plan",
            brief=instruction,
        )
    )

    assert sum(len(item.requests) for item in participants.values()) == 5
    assert outcome.plan.decision_ids == []
    assert outcome.plan.sources[0].source_kind == "text"
    artifact = tmp_path / outcome.plan.artifact_path
    sources = json.loads((artifact / "sources.json").read_text(encoding="utf-8"))
    assert sources[0]["instruction"] == instruction
    assert database.get_plan(outcome.plan.plan_id)["decision_ids"] == []


@pytest.mark.asyncio
async def test_plan_shares_one_context_and_freezes_discovered_workspace_evidence(
    database: Database,
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    relevant = source_dir / "csv_export.py"
    relevant.write_text(
        "def export_csv(records):\n    return records\n",
        encoding="utf-8",
    )
    for index in range(15):
        (source_dir / f"a{index:02d}.py").write_text(
            f"unrelated_{index} = True\n",
            encoding="utf-8",
        )
    artifacts = source_dir / "ego" / "planning" / "artifacts.py"
    artifacts.parent.mkdir(parents=True)
    artifacts.write_text(
        "class PlanArtifactWriter:\n    pass\n",
        encoding="utf-8",
    )
    participants = plan_participants("codex", "claude")

    outcome = await PlanWorkflow(database, participants).plan(
        PlanInput(
            question="Create a plan for the CSV export.",
            workspace=tmp_path,
            participant_ids=list(participants),
            command="plan",
            brief="Add a CSV export.",
        )
    )

    independent = [
        request
        for participant in participants.values()
        for request in participant.requests
        if request.phase is PlanPhase.INDEPENDENT
    ]
    assert len(independent) == 2
    assert all(request.workspace_context is not None for request in independent)
    context_ids = {
        request.workspace_context.manifest.context_id
        for request in independent
        if request.workspace_context is not None
    }
    assert len(context_ids) == 1
    assert all(request.tool_policy == ToolPolicy.local_read_only() for request in independent)
    joint = next(
        request
        for participant in participants.values()
        for request in participant.requests
        if request.phase is PlanPhase.JOINT_DRAFT
    )
    assert joint.workspace_context is not None
    assert joint.workspace_context.manifest.initial_context_id in context_ids
    assert joint.workspace_context.manifest.context_id not in context_ids
    assert joint.workspace_context.manifest.discovered_evidence_ids
    assert "def export_csv" in build_prompt(independent[0])
    assert "def export_csv" not in build_prompt(joint)
    assert "class PlanArtifactWriter" in build_prompt(joint)
    assert outcome.plan.workspace_context_manifest is not None
    stored = database.get_plan(outcome.plan.plan_id)["plan"]
    assert (
        stored["workspace_context_manifest"]["context_id"]
        == outcome.plan.workspace_context_manifest.context_id
    )


@pytest.mark.asyncio
async def test_plan_keeps_protected_reads_when_context_construction_fails(
    database: Database,
    tmp_path: Path,
) -> None:
    participants = plan_participants("codex", "claude")

    outcome = await PlanWorkflow(
        database,
        participants,
        context_builder=FailingContextBuilder(),
    ).plan(
        PlanInput(
            question="Create a fallback plan.",
            workspace=tmp_path,
            participant_ids=list(participants),
            command="plan",
            brief="Add fallback behavior.",
        )
    )

    independent = [
        request
        for participant in participants.values()
        for request in participant.requests
        if request.phase is PlanPhase.INDEPENDENT
    ]
    assert all(request.tool_policy.read for request in independent)
    assert outcome.plan.workspace_context_manifest is not None
    assert not outcome.plan.workspace_context_manifest.sufficient
    assert (
        outcome.plan.workspace_context_manifest.fallback_reason
        == "context builder failed (OSError)"
    )
    assert any("protected workspace reads" in item for item in outcome.plan.warnings)


@pytest.mark.asyncio
async def test_plan_accepts_a_bounded_workspace_file(
    database: Database,
    tmp_path: Path,
) -> None:
    source_file = tmp_path / "docs" / "export.md"
    source_file.parent.mkdir()
    source_file.write_text("Add a CSV export.\n", encoding="utf-8")
    participants = plan_participants("codex", "claude")

    outcome = await PlanWorkflow(database, participants).plan(
        PlanInput(
            question="Create a plan from docs/export.md.",
            workspace=tmp_path,
            participant_ids=list(participants),
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
    participants = plan_participants("codex", "claude")

    with pytest.raises(ValueError, match="inside the workspace"):
        await PlanWorkflow(database, participants).plan(
            PlanInput(
                question="Create a plan from a file.",
                workspace=workspace,
                participant_ids=list(participants),
                command="plan",
                brief_file=outside,
            )
        )

    assert all(not item.requests for item in participants.values())


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


def test_missing_collaboration_mappings_become_blocking_variants() -> None:
    candidates = {
        "codex": PlanParticipant._draft("Codex contribution."),
        "claude": PlanParticipant._draft("Claude contribution."),
    }
    joint, _ = normalize_joint_draft(
        JointPlanDraft(draft=PlanParticipant._draft("Joint candidate.")),
        candidates,
    )
    audit = PlanAudit(
        criticisms=[
            PlanCritique(
                id="claude:C1",
                severity=PlanCritiqueSeverity.MATERIAL,
                category=PlanCritiqueCategory.OMISSION,
                description="A material task was omitted.",
                required_change="Restore the task.",
                source_task_ids=["claude:T1"],
            )
        ]
    )
    assembly, _ = normalize_final_assembly(
        FinalPlanAssembly(draft=joint.draft),
        {"claude": audit},
    )

    assert assembly is not None
    assert assembly.critique_dispositions[0].action is CritiqueDispositionAction.VARIANT
    issues = blocking_issues(joint, {"claude": audit}, assembly, [], [])
    assert any("codex:T1 remains unmapped" in issue for issue in issues)
    assert any("claude:C1 remains a variant" in issue for issue in issues)


def test_final_assembly_explicitly_resolves_variants_and_global_sections() -> None:
    joint = JointPlanDraft(
        draft=PlanParticipant._draft("Joint candidate.").model_copy(
            update={"open_questions": ["Should V1 remain?"]}
        ),
        variants=[
            PlanVariant(
                id="V1",
                question="Should V1 remain?",
                options=["Keep it", "Resolve it"],
            )
        ],
    )
    audit = PlanAudit(
        criticisms=[
            PlanCritique(
                id="codex:C1",
                severity=PlanCritiqueSeverity.MATERIAL,
                category=PlanCritiqueCategory.VARIANT,
                description="V1 is resolved by existing evidence.",
                required_change="Remove V1 and its open question.",
                candidate_sections=[PlanSection.OPEN_QUESTIONS],
                candidate_variant_ids=["V1"],
            )
        ]
    )
    assembly, warnings = normalize_final_assembly(
        FinalPlanAssembly(
            draft=joint.draft.model_copy(update={"open_questions": []}),
            critique_dispositions=[
                CritiqueDisposition(
                    critique_id="codex:C1",
                    action=CritiqueDispositionAction.APPLIED,
                    target_sections=[PlanSection.OPEN_QUESTIONS],
                    resolved_variant_ids=["V1"],
                    rationale="The audit resolves the global question.",
                )
            ],
        ),
        {"codex": audit},
        joint,
    )

    assert assembly is not None
    assert warnings == []
    variants = merge_variants(
        joint.variants,
        assembly.variants,
        assembly.critique_dispositions[0].resolved_variant_ids,
    )
    assert variants == []
    assert blocking_issues(joint, {"codex": audit}, assembly, [], variants) == []


def test_final_assembly_cannot_silently_remove_a_variant() -> None:
    variant = PlanVariant(
        id="V1",
        question="Choose an implementation.",
        options=["A", "B"],
    )

    assert merge_variants([variant], []) == [variant]


def test_final_assembly_requires_explicit_targets_for_global_changes(
    tmp_path: Path,
) -> None:
    joint = JointPlanDraft(draft=PlanParticipant._draft("Joint candidate."))
    request = TurnRequest(
        run_id="run-1",
        phase=PlanPhase.FINAL_ASSEMBLY,
        question="Assemble the final plan.",
        workspace=tmp_path,
        agent_id="plan",
        workflow_id="plan",
        joint_plan=joint,
        plan_audits={
            "codex": PlanAudit(
                criticisms=[
                    PlanCritique(
                        id="codex:C1",
                        severity=PlanCritiqueSeverity.ADVISORY,
                        category=PlanCritiqueCategory.CONSTRAINT,
                        description="A global field requires correction.",
                        required_change="Correct the affected section.",
                    )
                ]
            )
        },
    )
    assembly = FinalPlanAssembly(
        draft=joint.draft,
        critique_dispositions=[
            CritiqueDisposition(
                critique_id="codex:C1",
                action=CritiqueDispositionAction.APPLIED,
                rationale="Applied without declaring what changed.",
            )
        ],
    )

    with pytest.raises(ValueError, match="require task, section, or variant targets"):
        validate_response(request, assembly)


def test_material_critique_cannot_be_applied_as_a_new_open_question(
    tmp_path: Path,
) -> None:
    joint = JointPlanDraft(draft=PlanParticipant._draft("Joint candidate."))
    critique = PlanCritique(
        id="codex:C1",
        severity=PlanCritiqueSeverity.MATERIAL,
        category=PlanCritiqueCategory.VARIANT,
        description="The default expert behavior remains undecided.",
        required_change="Preserve the unresolved behavior as a variant.",
        candidate_sections=[PlanSection.OPEN_QUESTIONS],
    )
    audits = {"codex": PlanAudit(criticisms=[critique])}
    assembly = FinalPlanAssembly(
        draft=joint.draft.model_copy(update={"open_questions": ["How should expert mode behave?"]}),
        critique_dispositions=[
            CritiqueDisposition(
                critique_id="codex:C1",
                action=CritiqueDispositionAction.APPLIED,
                target_sections=[PlanSection.OPEN_QUESTIONS],
                introduced_open_questions=["How should expert mode behave?"],
                rationale="The issue was retained as an open question.",
            )
        ],
    )
    request = TurnRequest(
        run_id="run-1",
        phase=PlanPhase.FINAL_ASSEMBLY,
        question="Assemble the final plan.",
        workspace=tmp_path,
        agent_id="plan",
        workflow_id="plan",
        joint_plan=joint,
        plan_audits=audits,
    )

    with pytest.raises(ValueError, match="return an explicit variant"):
        validate_response(request, assembly)
    assert any(
        "deferred as an open question" in issue
        for issue in blocking_issues(joint, audits, assembly, [], [])
    )


def test_final_assembly_keeps_unresolved_question_only_as_variant(
    tmp_path: Path,
) -> None:
    joint = JointPlanDraft(draft=PlanParticipant._draft("Joint candidate."))
    question = "How should the default expert inspection expose the summary?"
    critique = PlanCritique(
        id="codex:C1",
        severity=PlanCritiqueSeverity.MATERIAL,
        category=PlanCritiqueCategory.VARIANT,
        description="The default inspection contract remains unresolved.",
        required_change="Preserve the unresolved behavior as a variant.",
        candidate_task_ids=["T1"],
        candidate_sections=[PlanSection.OPEN_QUESTIONS],
    )
    audits = {"codex": PlanAudit(criticisms=[critique])}
    disposition = CritiqueDisposition(
        critique_id="codex:C1",
        action=CritiqueDispositionAction.VARIANT,
        target_task_ids=["T1"],
        target_sections=[PlanSection.OPEN_QUESTIONS],
        rationale="The sources do not resolve the public contract.",
    )
    variant = PlanVariant(
        id="V1",
        question=question,
        options=["Keep expert output.", "Change the default presentation."],
        source_task_ids=["codex:T1"],
    )
    request = TurnRequest(
        run_id="run-1",
        phase=PlanPhase.FINAL_ASSEMBLY,
        question="Assemble the final plan.",
        workspace=tmp_path,
        agent_id="plan",
        workflow_id="plan",
        joint_plan=joint,
        plan_audits=audits,
    )
    duplicated = FinalPlanAssembly(
        draft=joint.draft.model_copy(update={"open_questions": [question]}),
        critique_dispositions=[disposition],
        variants=[variant],
    )

    validate_response(request, duplicated)

    prompt = build_prompt(request)
    assert "Put an unresolved question only in variants" in prompt
    assert "Ego projects returned variants into the final plan" in prompt

    normalized, warnings = normalize_final_assembly(duplicated, audits, joint)

    assert normalized is not None
    assert normalized.draft.open_questions == []
    assert normalized.variants == [variant]
    assert any("normalized the duplicate representation" in item for item in warnings)


def test_advisory_open_question_is_explicitly_attributed(tmp_path: Path) -> None:
    joint = JointPlanDraft(draft=PlanParticipant._draft("Joint candidate."))
    question = "Which fixture name best matches the existing test style?"
    critique = PlanCritique(
        id="codex:C1",
        severity=PlanCritiqueSeverity.ADVISORY,
        category=PlanCritiqueCategory.VALIDATION,
        description="A non-blocking fixture name can be confirmed during implementation.",
        required_change="Record the fixture naming question.",
        candidate_sections=[PlanSection.OPEN_QUESTIONS],
    )
    audits = {"codex": PlanAudit(criticisms=[critique])}
    assembly = FinalPlanAssembly(
        draft=joint.draft.model_copy(update={"open_questions": [question]}),
        critique_dispositions=[
            CritiqueDisposition(
                critique_id="codex:C1",
                action=CritiqueDispositionAction.APPLIED,
                target_sections=[PlanSection.OPEN_QUESTIONS],
                introduced_open_questions=[question],
                rationale="The advisory implementation detail is explicitly recorded.",
            )
        ],
    )
    request = TurnRequest(
        run_id="run-1",
        phase=PlanPhase.FINAL_ASSEMBLY,
        question="Assemble the final plan.",
        workspace=tmp_path,
        agent_id="plan",
        workflow_id="plan",
        joint_plan=joint,
        plan_audits=audits,
    )

    validate_response(request, assembly)
    assert blocking_issues(joint, audits, assembly, [], []) == []


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


def test_independent_plan_evidence_correction_retains_protected_reads(
    tmp_path: Path,
) -> None:
    request = TurnRequest(
        run_id="run-1",
        phase=PlanPhase.INDEPENDENT,
        question="Create the plan.",
        workspace=tmp_path,
        agent_id="plan",
        workflow_id="plan",
        tool_policy=ToolPolicy.local_read_only(),
    )

    prompt = build_prompt(
        request,
        correction="cited fragment does not contain render_plan",
        previous_response={"title": "Wrong location"},
    )

    assert "Reinspect only the workspace evidence rejected by validation" in prompt
    assert "Use protected read/search tools" in prompt
    assert f"exact authorized root {tmp_path.resolve()}" in prompt
    assert "do not infer the workspace from the provider's process directory" in prompt
    assert "Omitted on correction." in prompt


def test_plan_rejects_unknown_workspace_evidence_ids(tmp_path: Path) -> None:
    evidence = WorkspaceContextEvidence(
        id="CTX-known",
        path="src/export.py",
        line_start=1,
        line_end=1,
        file_sha256="a" * 64,
        fragment_sha256="b" * 64,
        reason="query-relevant workspace evidence",
        content="def export_csv(): ...\n",
    )
    context = WorkspaceContext(
        manifest=WorkspaceContextManifest(
            context_id="context-1",
            workspace_fingerprint="c" * 64,
            evidence=[
                WorkspaceContextEvidenceReference(
                    id=evidence.id,
                    path=evidence.path,
                    line_start=evidence.line_start,
                    line_end=evidence.line_end,
                    file_sha256=evidence.file_sha256,
                    fragment_sha256=evidence.fragment_sha256,
                    reason=evidence.reason,
                )
            ],
            sufficient=True,
            bytes_used=len(evidence.content),
            byte_budget=1024,
        ),
        project_map=[evidence.path],
        evidence=[evidence],
    )
    request = TurnRequest(
        run_id="run-1",
        phase=PlanPhase.INDEPENDENT,
        question="Plan the CSV export.",
        workspace=tmp_path,
        agent_id="plan",
        workflow_id="plan",
        workspace_context=context,
    )
    draft = PlanParticipant._draft("Add the CSV export.")
    draft.tasks[0].evidence_ids = ["CTX-unknown"]

    with pytest.raises(ValueError, match="unknown workspace evidence"):
        validate_response(request, draft)


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
    participants = plan_participants("codex", "claude")

    with pytest.raises(ValueError, match="is not accepted"):
        await PlanWorkflow(database, participants).plan(
            PlanInput(
                question="Plan it.",
                workspace=tmp_path,
                participant_ids=list(participants),
                command="plan",
                decision_ids=[decision_id],
            )
        )

    assert all(not item.requests for item in participants.values())


@pytest.mark.asyncio
async def test_plan_requires_at_least_two_selected_participants(
    database: Database,
    tmp_path: Path,
) -> None:
    decision_id = accepted_decision(database, tmp_path)

    with pytest.raises(ValueError, match="at least two"):
        await PlanWorkflow(database, {"codex": PlanParticipant("codex")}).plan(
            PlanInput(
                question="Plan it.",
                workspace=tmp_path,
                participant_ids=["codex"],
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


def test_plan_with_blocking_issues_cannot_be_approved(
    database: Database,
    tmp_path: Path,
) -> None:
    from ego.models import ImplementationPlan, PlanFormat

    run_id = database.create_run(command="plan", question="Plan it.", workspace=tmp_path)
    draft = PlanDraft(
        title="Blocked plan",
        objective="Preserve a material unresolved author criticism.",
        tasks=[PlanTask(id="T1", title="Wait", description="Resolve the criticism.")],
    )
    artifact = PlanArtifactWriter().write(
        workspace=tmp_path,
        plan_id="blocked-plan",
        run_id=run_id,
        draft=draft,
        sources=[],
        destination=Path(".ego/plans/blocked"),
        workspace_git_head=None,
        blocking_issues=["Material critique claude:C1 was not assembled."],
    )
    plan = ImplementationPlan(
        plan_id="blocked-plan",
        run_id=run_id,
        state=PlanState.DRAFT,
        format=PlanFormat.MARKDOWN,
        workspace=tmp_path,
        decision_ids=[],
        artifact_path=artifact.path.relative_to(tmp_path),
        manifest_sha256=artifact.manifest_sha256,
        plan_sha256=artifact.plan_sha256,
        draft=draft,
        blocking_issues=["Material critique claude:C1 was not assembled."],
    )
    database.create_plan(plan)

    with pytest.raises(ValueError, match="unresolved blocking issues"):
        database.transition_plan(
            plan.plan_id,
            PlanState.APPROVED,
            "Approve anyway.",
            manifest_sha256=artifact.manifest_sha256,
        )

    assert database.get_plan(plan.plan_id)["state"] == PlanState.DRAFT.value
