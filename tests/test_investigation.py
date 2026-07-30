from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.containers import Vertical
from textual.widgets import Markdown
from typer.testing import CliRunner

from ego.agents import build_agent_registry
from ego.agents.investigate import InvestigationInput
from ego.cli import app
from ego.config import AppPaths, EgoConfig
from ego.events import WorkEventType
from ego.investigation import InvestigationWorkflow
from ego.models import (
    AvailabilityStatus,
    Confidence,
    Evidence,
    InvestigationDraft,
    InvestigationFinding,
    InvestigationHypothesis,
    InvestigationHypothesisState,
    InvestigationPhase,
    InvestigationReport,
    InvestigationReview,
    InvestigationReviewBundle,
    InvestigationSynthesis,
    ParticipantAvailability,
    ParticipantTurnResult,
    RunStatus,
    TurnRequest,
)
from ego.storage import Database
from ego.tui.app import EgoApp, QuestionInput
from ego.tui.presentation import investigation_markdown
from ego.workspace import validate_evidence


class InvestigationParticipant:
    def __init__(
        self,
        name: str,
        *,
        evidence_path: str = "fact.txt",
        fail_stage: InvestigationPhase | None = None,
        block: asyncio.Event | None = None,
    ) -> None:
        self.participant_id = name
        self.evidence_path = evidence_path
        self.fail_stage = fail_stage
        self.block = block
        self.responding = asyncio.Event()
        self.requests: list[TurnRequest] = []

    async def probe(self) -> ParticipantAvailability:
        return ParticipantAvailability(
            participant_id=self.participant_id,
            status=AvailabilityStatus.AVAILABLE,
            binary=f"/fake/{self.participant_id}",
            version="1.0",
        )

    async def respond(self, request: TurnRequest) -> ParticipantTurnResult:
        self.requests.append(request)
        self.responding.set()
        if self.block is not None:
            await self.block.wait()
        if request.phase is self.fail_stage:
            raise ValueError("synthetic partial failure")
        evidence = Evidence(
            path=self.evidence_path,
            line_start=1,
            line_end=1,
            explanation="The configured mode is declared here.",
            critical=True,
        )
        finding = InvestigationFinding(
            claim="The workspace enables strict mode.",
            explanation="The local configuration explicitly enables strict mode.",
            evidence=[evidence],
            confidence=Confidence.MODERATE,
        )
        hypothesis = InvestigationHypothesis(
            hypothesis="The failure is caused by strict mode.",
            state=InvestigationHypothesisState.SUPPORTED,
            supporting_evidence=[evidence],
            explanation="The failing behavior matches the enabled mode.",
        )
        refuted = InvestigationHypothesis(
            hypothesis="The configuration file is missing.",
            state=InvestigationHypothesisState.REFUTED,
            counter_evidence=[evidence],
            explanation="The configuration file is present and readable.",
        )
        if request.phase in {
            InvestigationPhase.INDEPENDENT,
            InvestigationPhase.REVISION,
        }:
            payload = InvestigationDraft(
                findings=[finding],
                hypotheses=[hypothesis, refuted],
                unknowns=["Whether an override exists."],
            )
        elif request.phase is InvestigationPhase.PEER_CHALLENGE:
            payload = InvestigationReviewBundle(
                reviews=[
                    InvestigationReview(
                        target_participant=name,
                        valid_points=["The configuration is cited."],
                        challenges=["Check whether the mode is overridden."],
                        missing_evidence=["No runtime override was found."],
                        omitted_hypotheses=["A stale cache could also explain the failure."],
                    )
                    for name in request.peer_investigations
                ]
            )
        else:
            payload = InvestigationSynthesis(
                facts=[finding],
                probable_causes=[hypothesis, refuted],
                disputed_findings=[
                    finding.model_copy(
                        update={"claim": "A runtime override may disable strict mode."}
                    )
                ],
                unknowns=["Whether an override exists."],
                next_checks=["Read the local override configuration."],
            )
        return ParticipantTurnResult(
            participant_id=self.participant_id,
            phase=request.phase,
            payload=payload,
            raw_output=payload.model_dump_json(),
            duration_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_registry_discovers_and_dispatches_specialized_agents(
    database: Database, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    participants = {
        name: InvestigationParticipant(name) for name in ("codex", "opencode")
    }
    registry = build_agent_registry(database, participants)

    assert [agent.agent_id for agent in registry.list()] == [
        "decision",
        "investigate",
        "plan",
    ]
    assert registry.get("investigate").workflow_id == "investigation"

    outcome = await registry.dispatch(
        "investigate",
        InvestigationInput(
            question="Why does it fail?",
            workspace=tmp_path,
            participant_ids=list(participants),
        ),
    )

    assert outcome.report.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_investigation_runs_five_stages_with_local_read_only_policy(
    database: Database, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    participants = {
        name: InvestigationParticipant(name) for name in ("codex", "opencode")
    }

    outcome = await InvestigationWorkflow(database, participants).investigate(
        question="Why does it fail?",
        workspace=tmp_path,
        participant_ids=list(participants),
    )

    report = outcome.report
    run = database.get_run(report.run_id)
    assert report.status is RunStatus.COMPLETED
    assert report.findings
    assert report.hypotheses[0].state is InvestigationHypothesisState.SUPPORTED
    assert any(
        item.state is InvestigationHypothesisState.REFUTED for item in report.hypotheses
    )
    assert report.disputed_findings
    assert run["agent_id"] == "investigate"
    assert run["workflow_id"] == "investigation"
    assert run["result_kind"] == "investigation_report"
    assert run["result"] == report.model_dump(mode="json")
    assert database.list_decisions() == []
    assert {call["phase"] for call in run["calls"]} == {
        stage.value for stage in InvestigationPhase
    }
    assert database.get_run_events(report.run_id)[-1].event_type is WorkEventType.RESULT_CREATED

    for participant in participants.values():
        assert {request.phase for request in participant.requests} == set(InvestigationPhase)
        for request in participant.requests:
            policy = request.tool_policy
            if request.phase in {
                InvestigationPhase.INDEPENDENT,
                InvestigationPhase.PEER_CHALLENGE,
                InvestigationPhase.REVISION,
            }:
                assert policy.read and policy.glob and policy.grep and policy.local_search
            else:
                assert not any(
                    (policy.read, policy.glob, policy.grep, policy.local_search)
                )
            assert not any(
                (
                    policy.web,
                    policy.shell,
                    policy.write,
                    policy.plugins,
                    policy.mcp,
                    policy.delegation,
                )
            )


@pytest.mark.asyncio
async def test_invalid_evidence_and_partial_synthesis_are_inconclusive(
    database: Database, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    participants = {
        "codex": InvestigationParticipant("codex", evidence_path="missing.txt"),
        "opencode": InvestigationParticipant(
            "opencode",
            evidence_path="missing.txt",
            fail_stage=InvestigationPhase.SYNTHESIS,
        ),
    }

    outcome = await InvestigationWorkflow(database, participants).investigate(
        question="Why does it fail?",
        workspace=tmp_path,
        participant_ids=list(participants),
    )

    assert outcome.report.status is RunStatus.INCONCLUSIVE
    assert any("Two independent" in warning for warning in outcome.report.warnings)
    assert all(
        evidence.status.value == "invalid"
        for finding in outcome.report.findings
        for evidence in finding.evidence
    )


@pytest.mark.asyncio
async def test_investigation_cancellation_is_persisted_as_interrupted(
    database: Database, tmp_path: Path
) -> None:
    gate = asyncio.Event()
    participant = InvestigationParticipant("codex", block=gate)
    task = asyncio.create_task(
        InvestigationWorkflow(database, {"codex": participant}).investigate(
            question="Why does it fail?",
            workspace=tmp_path,
            participant_ids=["codex"],
        )
    )
    await participant.responding.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert database.list_runs()[0]["status"] == RunStatus.INTERRUPTED.value


def test_investigate_cli_human_and_json_output(
    monkeypatch: pytest.MonkeyPatch,
    app_paths: AppPaths,
    tmp_path: Path,
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    participants = {
        name: InvestigationParticipant(name) for name in ("codex", "opencode")
    }
    database = Database(app_paths)
    monkeypatch.setattr(
        "ego.cli.services",
        lambda: (EgoConfig(), database, participants),
    )
    runner = CliRunner()

    human = runner.invoke(
        app,
        ["investigate", "Why does it fail?", "--dir", str(tmp_path)],
    )
    structured = runner.invoke(
        app,
        ["investigate", "Why does it fail?", "--dir", str(tmp_path), "--json"],
    )

    assert human.exit_code == 0
    assert "Findings" in human.stdout
    assert "Disputed findings" in human.stdout
    assert structured.exit_code == 0
    assert '"agent_id": "investigate"' in structured.stdout
    assert '"workflow_id": "investigation"' in structured.stdout


def test_investigation_tui_report_has_no_human_resolution_actions() -> None:
    report = InvestigationReport(
        run_id="run",
        status=RunStatus.INCONCLUSIVE,
        question="Why?",
        disputed_findings=[
            InvestigationFinding(
                claim="The override is active.",
                explanation="Participants disagree about the override.",
                confidence=Confidence.LOW,
            )
        ],
        unknowns=["Whether the override loads at runtime."],
        next_checks=["Read the local override file."],
    )

    rendered = investigation_markdown(report)

    assert "Disputed findings" in rendered
    assert "Unknowns" in rendered
    assert "Next checks" in rendered
    assert "/accept" not in rendered
    assert "/choose" not in rendered


@pytest.mark.asyncio
async def test_tui_investigate_uses_active_workspace_without_resolution_panel(
    app_paths: AppPaths, tmp_path: Path
) -> None:
    (tmp_path / "fact.txt").write_text("strict=true\n", encoding="utf-8")
    participants = {
        name: InvestigationParticipant(name) for name in ("codex", "opencode")
    }
    app_instance = EgoApp(
        workspace=tmp_path,
        paths=app_paths,
        participants=participants,
    )

    async with app_instance.run_test(size=(140, 50)) as pilot:
        question = app_instance.query_one("#question-input", QuestionInput)
        question.text = "/investigate Why does strict mode fail?"
        await pilot.press("enter")
        async with asyncio.timeout(3):
            while app_instance.current_investigation is None:
                await pilot.pause()

        assert app_instance.session.agent_id == "investigate"
        assert app_instance.current_decision_id is None
        assert not app_instance.query_one("#resolution-panel", Vertical).display
        rendered = app_instance.query_one("#result", Markdown).source
        assert "Findings" in rendered
        assert "Disputed findings" in rendered
        assert "Next checks" in rendered


def test_stale_critical_investigation_evidence_is_inconclusive(tmp_path: Path) -> None:
    source = tmp_path / "fact.txt"
    source.write_text("strict=true\n", encoding="utf-8")
    evidence = validate_evidence(
        tmp_path,
        Evidence(
            path="fact.txt",
            line_start=1,
            line_end=1,
            explanation="Strict mode is enabled.",
            critical=True,
        ),
    )
    finding = InvestigationFinding(
        claim="Strict mode is enabled.",
        explanation="The configuration enabled it.",
        evidence=[evidence],
        confidence=Confidence.MODERATE,
    )
    source.write_text("strict=false\n", encoding="utf-8")

    report = InvestigationWorkflow._build_report(
        run_id="run",
        question="Why?",
        workspace=tmp_path,
        investigations={},
        reviews={},
        revisions={},
        syntheses={},
        material={
            "codex": InvestigationSynthesis(facts=[finding]),
            "opencode": InvestigationSynthesis(facts=[finding]),
        },
        warnings=[],
    )

    assert report.status is RunStatus.INCONCLUSIVE
    assert report.findings[0].evidence[0].status.value == "stale"
