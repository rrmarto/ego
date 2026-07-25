from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from ego.agents.runtime import AgentRuntime, NoParticipantsError
from ego.deliberation.finalization import citation_is_verified
from ego.events import WorkEventType
from ego.models import (
    EvidenceStatus,
    InvestigationDraft,
    InvestigationFinding,
    InvestigationHypothesis,
    InvestigationHypothesisState,
    InvestigationPhase,
    InvestigationReport,
    InvestigationReview,
    InvestigationReviewBundle,
    InvestigationSynthesis,
    RunStatus,
    ToolPolicy,
    TurnRequest,
)
from ego.participants import Participant
from ego.storage import Database
from ego.workspace import observe_git, revalidate_evidence, validate_evidence


@dataclass(frozen=True)
class InvestigationOutcome:
    report: InvestigationReport


class InvestigationWorkflow:
    workflow_id = "investigation"

    def __init__(self, database: Database, participants: dict[str, Participant]) -> None:
        self.database = database
        self.runtime = AgentRuntime(database, participants)

    async def investigate(
        self,
        *,
        question: str,
        workspace: Path,
        participant_ids: list[str],
        command: str = "investigate",
    ) -> InvestigationOutcome:
        git_start = await observe_git(workspace)
        run_id = self.database.create_run(
            command=command,
            question=question,
            workspace=workspace,
            git_head=git_start.head,
            git_status=git_start.status,
            agent_id="investigate",
            workflow_id=self.workflow_id,
            result_kind="investigation_report",
        )
        self.database.set_run_status(run_id, RunStatus.RUNNING)
        try:
            active = await self.runtime.active_participants(run_id, participant_ids)
            if not active:
                raise NoParticipantsError("no selected participant passed the availability checks")

            investigations = await self._draft_stage(
                run_id, InvestigationPhase.INDEPENDENT, question, workspace, active
            )
            if not investigations:
                raise NoParticipantsError("all participants failed independent investigation")

            reviews = await self._challenge_stage(
                run_id, question, workspace, active, investigations
            )
            revisions = await self._revision_stage(
                run_id, question, workspace, active, investigations, reviews
            )
            if not revisions:
                revisions = investigations

            syntheses, synthesis_expected = await self._synthesis_stage(
                run_id, question, workspace, active, revisions
            )
            reconciled, reconciliation_expected = await self._reconciliation_stage(
                run_id, question, workspace, active, syntheses
            )
            material = reconciled or syntheses
            report = self._build_report(
                run_id=run_id,
                question=question,
                workspace=workspace,
                investigations=investigations,
                reviews=reviews,
                revisions=revisions,
                syntheses=syntheses,
                material=material,
                warnings=self._degradation_warnings(
                    active_count=len(active),
                    reviews=reviews,
                    revision_count=len(revisions),
                    synthesis_count=len(syntheses),
                    synthesis_expected=synthesis_expected,
                    reconciliation_count=len(reconciled),
                    reconciliation_expected=reconciliation_expected,
                ),
            )
            git_end = await observe_git(workspace)
            if git_start != git_end:
                report = report.model_copy(
                    update={
                        "status": RunStatus.INCONCLUSIVE,
                        "warnings": [
                            *report.warnings,
                            "The Git workspace state changed during investigation.",
                        ],
                    }
                )
            self.database.set_run_status(
                run_id,
                report.status,
                result=report,
                git_head=git_end.head,
                git_status=git_end.status,
            )
            self.database.add_event(
                run_id,
                WorkEventType.RESULT_CREATED,
                {"result_kind": "investigation_report", "status": report.status.value},
            )
            return InvestigationOutcome(report=report)
        except KeyboardInterrupt, asyncio.CancelledError:
            self.database.set_run_status(run_id, RunStatus.INTERRUPTED)
            raise
        except BaseException:
            self.database.set_run_status(run_id, RunStatus.FAILED)
            raise

    async def _draft_stage(
        self,
        run_id: str,
        stage: InvestigationPhase,
        question: str,
        workspace: Path,
        participants: dict[str, Participant],
    ) -> dict[str, InvestigationDraft]:
        requests = {
            name: (
                participant,
                self._request(
                    run_id=run_id,
                    stage=stage,
                    question=question,
                    workspace=workspace,
                    tools=ToolPolicy.local_read_only(),
                ),
            )
            for name, participant in participants.items()
        }
        results = await self.runtime.parallel(run_id, stage, requests)
        return {
            name: _validate_draft(workspace, result.payload)
            for name, result in results.items()
            if isinstance(result.payload, InvestigationDraft)
        }

    async def _challenge_stage(
        self,
        run_id: str,
        question: str,
        workspace: Path,
        participants: dict[str, Participant],
        investigations: dict[str, InvestigationDraft],
    ) -> dict[str, InvestigationReviewBundle]:
        requests: dict[str, tuple[Participant, TurnRequest]] = {}
        for name in investigations:
            peers = {key: value for key, value in investigations.items() if key != name}
            if not peers:
                peers = {name: investigations[name]}
            requests[name] = (
                participants[name],
                self._request(
                    run_id=run_id,
                    stage=InvestigationPhase.PEER_CHALLENGE,
                    question=question,
                    workspace=workspace,
                    tools=ToolPolicy.local_read_only(),
                    own_investigation=investigations[name],
                    peer_investigations=peers,
                ),
            )
        results = await self.runtime.parallel(
            run_id, InvestigationPhase.PEER_CHALLENGE, requests
        )
        return {
            name: result.payload
            for name, result in results.items()
            if isinstance(result.payload, InvestigationReviewBundle)
        }

    async def _revision_stage(
        self,
        run_id: str,
        question: str,
        workspace: Path,
        participants: dict[str, Participant],
        investigations: dict[str, InvestigationDraft],
        reviews: dict[str, InvestigationReviewBundle],
    ) -> dict[str, InvestigationDraft]:
        requests: dict[str, tuple[Participant, TurnRequest]] = {}
        for name in investigations:
            targeted = {
                reviewer: [
                    review
                    for review in bundle.reviews
                    if review.target_participant == name
                ]
                for reviewer, bundle in reviews.items()
            }
            requests[name] = (
                participants[name],
                self._request(
                    run_id=run_id,
                    stage=InvestigationPhase.REVISION,
                    question=question,
                    workspace=workspace,
                    tools=ToolPolicy.local_read_only(),
                    own_investigation=investigations[name],
                    peer_investigations={
                        key: value for key, value in investigations.items() if key != name
                    },
                    investigation_reviews={
                        key: value for key, value in targeted.items() if value
                    },
                ),
            )
        results = await self.runtime.parallel(run_id, InvestigationPhase.REVISION, requests)
        return {
            name: _validate_draft(workspace, result.payload)
            for name, result in results.items()
            if isinstance(result.payload, InvestigationDraft)
        }

    async def _synthesis_stage(
        self,
        run_id: str,
        question: str,
        workspace: Path,
        participants: dict[str, Participant],
        investigations: dict[str, InvestigationDraft],
    ) -> tuple[dict[str, InvestigationSynthesis], int]:
        selected_names = self._synthesizers(run_id, investigations)
        requests = {
            name: (
                participants[name],
                self._request(
                    run_id=run_id,
                    stage=InvestigationPhase.SYNTHESIS,
                    question=question,
                    workspace=workspace,
                    peer_investigations=investigations,
                ),
            )
            for name in selected_names
        }
        results = await self.runtime.parallel(run_id, InvestigationPhase.SYNTHESIS, requests)
        return (
            {
                name: _validate_synthesis(workspace, result.payload)
                for name, result in results.items()
                if isinstance(result.payload, InvestigationSynthesis)
            },
            2,
        )

    async def _reconciliation_stage(
        self,
        run_id: str,
        question: str,
        workspace: Path,
        participants: dict[str, Participant],
        syntheses: dict[str, InvestigationSynthesis],
    ) -> tuple[dict[str, InvestigationSynthesis], int]:
        selected_names = list(syntheses)
        requests = {
            name: (
                participants[name],
                self._request(
                    run_id=run_id,
                    stage=InvestigationPhase.RECONCILIATION,
                    question=question,
                    workspace=workspace,
                    investigation_syntheses=syntheses,
                ),
            )
            for name in selected_names
        }
        results = await self.runtime.parallel(
            run_id, InvestigationPhase.RECONCILIATION, requests
        )
        return (
            {
                name: _validate_synthesis(workspace, result.payload)
                for name, result in results.items()
                if isinstance(result.payload, InvestigationSynthesis)
            },
            2,
        )

    @staticmethod
    def _request(
        *,
        run_id: str,
        stage: InvestigationPhase,
        question: str,
        workspace: Path,
        tools: ToolPolicy | None = None,
        own_investigation: InvestigationDraft | None = None,
        peer_investigations: dict[str, InvestigationDraft] | None = None,
        investigation_reviews: dict[str, list[InvestigationReview]] | None = None,
        investigation_syntheses: dict[str, InvestigationSynthesis] | None = None,
    ) -> TurnRequest:
        return TurnRequest(
            run_id=run_id,
            phase=stage,
            question=question,
            workspace=workspace,
            agent_id="investigate",
            workflow_id="investigation",
            tool_policy=tools or ToolPolicy(),
            own_investigation=own_investigation,
            peer_investigations=peer_investigations or {},
            investigation_reviews=investigation_reviews or {},
            investigation_syntheses=investigation_syntheses or {},
        )

    def _synthesizers(
        self, run_id: str, investigations: dict[str, InvestigationDraft]
    ) -> tuple[str, ...]:
        if len(investigations) == 1:
            return (next(iter(investigations)),)
        return self.runtime.rotating_pair(run_id, investigations)

    @staticmethod
    def _degradation_warnings(
        *,
        active_count: int,
        reviews: dict[str, InvestigationReviewBundle],
        revision_count: int,
        synthesis_count: int,
        synthesis_expected: int,
        reconciliation_count: int,
        reconciliation_expected: int,
    ) -> list[str]:
        warnings: list[str] = []
        if active_count < 2:
            warnings.append("Only one participant was available; cross-checking was degraded.")
        if len(reviews) < active_count:
            warnings.append("One or more peer challenges failed.")
        if revision_count < active_count:
            warnings.append("One or more investigation revisions failed.")
        if synthesis_count < synthesis_expected:
            warnings.append("Two independent consolidated reports were not completed.")
        if reconciliation_count < reconciliation_expected:
            warnings.append("Reconciliation was materially degraded.")
        return warnings

    @staticmethod
    def _build_report(
        *,
        run_id: str,
        question: str,
        workspace: Path,
        investigations: dict[str, InvestigationDraft],
        reviews: dict[str, InvestigationReviewBundle],
        revisions: dict[str, InvestigationDraft],
        syntheses: dict[str, InvestigationSynthesis],
        material: dict[str, InvestigationSynthesis],
        warnings: list[str],
    ) -> InvestigationReport:
        findings = _unique_findings(item for value in material.values() for item in value.facts)
        hypotheses = _unique_hypotheses(
            item for value in material.values() for item in value.probable_causes
        )
        disputed = _unique_findings(
            item for value in material.values() for item in value.disputed_findings
        )
        unknowns = list(
            dict.fromkeys(item for value in material.values() for item in value.unknowns)
        )
        next_checks = list(
            dict.fromkeys(item for value in material.values() for item in value.next_checks)
        )

        findings = [_revalidate_finding(workspace, item) for item in findings]
        hypotheses = [_revalidate_hypothesis(workspace, item) for item in hypotheses]
        disputed = [_revalidate_finding(workspace, item) for item in disputed]
        evidence = [
            item
            for finding in findings + disputed
            for item in finding.evidence
        ] + [
            item
            for hypothesis in hypotheses
            for item in hypothesis.supporting_evidence + hypothesis.counter_evidence
        ]
        stale_or_invalid = [
            item
            for item in evidence
            if item.status in {EvidenceStatus.STALE, EvidenceStatus.INVALID}
        ]
        useful = any(
            any(citation_is_verified(item) for item in finding.evidence) for finding in findings
        ) or any(
            hypothesis.state
            in {
                InvestigationHypothesisState.SUPPORTED,
                InvestigationHypothesisState.PLAUSIBLE,
            }
            and any(citation_is_verified(item) for item in hypothesis.supporting_evidence)
            for hypothesis in hypotheses
        )
        material_degradation = any(
            message.startswith(("Only one", "Two independent", "Reconciliation"))
            for message in warnings
        )
        if stale_or_invalid:
            warnings.append(
                f"{len(stale_or_invalid)} cited source(s) became stale or invalid."
            )
        status = (
            RunStatus.COMPLETED
            if useful
            and not material_degradation
            and not any(item.critical for item in stale_or_invalid)
            else RunStatus.INCONCLUSIVE
        )
        if not useful:
            warnings.append("No backed finding or supported hypothesis remained.")
        return InvestigationReport(
            run_id=run_id,
            status=status,
            question=question,
            findings=findings,
            hypotheses=hypotheses,
            disputed_findings=disputed,
            unknowns=unknowns,
            next_checks=next_checks,
            warnings=warnings,
            participant_investigations=revisions or investigations,
            participant_reviews=reviews,
            syntheses=syntheses,
        )


def _validate_draft(workspace: Path, draft: object) -> InvestigationDraft:
    assert isinstance(draft, InvestigationDraft)
    return draft.model_copy(
        update={
            "findings": _distinct_findings(
                _validate_finding(workspace, item) for item in draft.findings
            ),
            "hypotheses": _distinct_hypotheses(
                _validate_hypothesis(workspace, item) for item in draft.hypotheses
            ),
            "unknowns": list(dict.fromkeys(draft.unknowns)),
        }
    )


def _validate_synthesis(workspace: Path, synthesis: object) -> InvestigationSynthesis:
    assert isinstance(synthesis, InvestigationSynthesis)
    return synthesis.model_copy(
        update={
            "facts": _distinct_findings(
                _validate_finding(workspace, item) for item in synthesis.facts
            ),
            "probable_causes": _distinct_hypotheses(
                _validate_hypothesis(workspace, item) for item in synthesis.probable_causes
            ),
            "disputed_findings": _distinct_findings(
                _validate_finding(workspace, item) for item in synthesis.disputed_findings
            ),
            "unknowns": list(dict.fromkeys(synthesis.unknowns)),
            "next_checks": list(dict.fromkeys(synthesis.next_checks)),
        }
    )


def _validate_finding(workspace: Path, finding: InvestigationFinding) -> InvestigationFinding:
    return finding.model_copy(
        update={"evidence": [validate_evidence(workspace, item) for item in finding.evidence]}
    )


def _validate_hypothesis(
    workspace: Path, hypothesis: InvestigationHypothesis
) -> InvestigationHypothesis:
    return hypothesis.model_copy(
        update={
            "supporting_evidence": [
                validate_evidence(workspace, item) for item in hypothesis.supporting_evidence
            ],
            "counter_evidence": [
                validate_evidence(workspace, item) for item in hypothesis.counter_evidence
            ],
        }
    )


def _revalidate_finding(
    workspace: Path, finding: InvestigationFinding
) -> InvestigationFinding:
    return finding.model_copy(
        update={"evidence": [revalidate_evidence(workspace, item) for item in finding.evidence]}
    )


def _revalidate_hypothesis(
    workspace: Path, hypothesis: InvestigationHypothesis
) -> InvestigationHypothesis:
    return hypothesis.model_copy(
        update={
            "supporting_evidence": [
                revalidate_evidence(workspace, item) for item in hypothesis.supporting_evidence
            ],
            "counter_evidence": [
                revalidate_evidence(workspace, item) for item in hypothesis.counter_evidence
            ],
        }
    )


def _unique_findings(values: Iterable[InvestigationFinding]) -> list[InvestigationFinding]:
    unique: dict[str, InvestigationFinding] = {}
    for value in values:
        unique.setdefault(value.claim, value)
    return list(unique.values())


def _unique_hypotheses(
    values: Iterable[InvestigationHypothesis],
) -> list[InvestigationHypothesis]:
    unique: dict[str, InvestigationHypothesis] = {}
    for value in values:
        unique.setdefault(value.hypothesis, value)
    return list(unique.values())


def _distinct_findings(
    values: Iterable[InvestigationFinding],
) -> list[InvestigationFinding]:
    unique: dict[str, InvestigationFinding] = {}
    for value in values:
        unique.setdefault(value.model_dump_json(), value)
    return list(unique.values())


def _distinct_hypotheses(
    values: Iterable[InvestigationHypothesis],
) -> list[InvestigationHypothesis]:
    unique: dict[str, InvestigationHypothesis] = {}
    for value in values:
        unique.setdefault(value.model_dump_json(), value)
    return list(unique.values())
