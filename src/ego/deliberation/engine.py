from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ego.agents.runtime import AgentRuntime, NoParticipantsError
from ego.deliberation.finalization import (
    SEMANTIC_VERIFICATION_WARNING,
    apply_workspace_changes,
    contested_final,
    final_from_synthesis,
    revalidate_final,
    single_participant_final,
    unique_evidence,
    valid_evidence_count,
    validate_position,
    validate_synthesis,
)
from ego.models import (
    Confidence,
    FinalDecision,
    PeerReviewBundle,
    Phase,
    Position,
    RunStatus,
    Synthesis,
    TurnRequest,
)
from ego.participants import Participant
from ego.storage import Database
from ego.workspace import observe_git


@dataclass(frozen=True)
class DeliberationOutcome:
    decision_id: str
    final: FinalDecision


class DeliberationEngine:
    def __init__(self, database: Database, participants: dict[str, Participant]) -> None:
        self.database = database
        self.participants = participants
        self.runtime = AgentRuntime(database, participants)

    async def deliberate(
        self,
        *,
        question: str,
        workspace: Path,
        participant_ids: list[str],
        command: str,
        parent_decision_id: str | None = None,
    ) -> DeliberationOutcome:
        workspace_path = workspace
        git_start = await observe_git(workspace_path)
        run_id = self.database.create_run(
            command=command,
            question=question,
            workspace=workspace_path,
            parent_decision_id=parent_decision_id,
            git_head=git_start.head,
            git_status=git_start.status,
        )
        self.database.set_run_status(run_id, RunStatus.RUNNING)
        try:
            active = await self.runtime.active_participants(run_id, participant_ids)
            if not active:
                raise NoParticipantsError("no selected participant passed the availability checks")

            independent = await self._position_phase(
                run_id,
                Phase.INDEPENDENT,
                question,
                workspace_path,
                active,
                {},
                {},
            )
            if not independent:
                raise NoParticipantsError("all participants failed independent reasoning")

            if len(independent) == 1:
                final = single_participant_final(run_id, next(iter(independent.values())))
            else:
                reviewers = {name: active[name] for name in independent}
                reviews = await self._review_phase(
                    run_id, question, workspace_path, reviewers, independent
                )
                revision_participants = {name: reviewers[name] for name in reviews}
                revised = await self._position_phase(
                    run_id,
                    Phase.REVISION,
                    question,
                    workspace_path,
                    revision_participants,
                    independent,
                    reviews,
                )
                if not revised:
                    raise NoParticipantsError("all participants failed position revision")
                if len(revised) == 1:
                    final = single_participant_final(run_id, next(iter(revised.values())))
                else:
                    final = await self._synthesize(
                        run_id, question, workspace_path, active, revised
                    )

            final = revalidate_final(workspace_path, final)
            git_end = await observe_git(workspace_path)
            final = apply_workspace_changes(final, git_start, git_end)
            self.database.set_run_status(
                run_id,
                final.status,
                final=final,
                git_head=git_end.head,
                git_status=git_end.status,
            )
            decision_id = self.database.create_decision(final, supersedes_id=parent_decision_id)
            return DeliberationOutcome(decision_id=decision_id, final=final)
        except KeyboardInterrupt, asyncio.CancelledError:
            self.database.set_run_status(run_id, RunStatus.INTERRUPTED)
            raise
        except BaseException:
            self.database.set_run_status(run_id, RunStatus.FAILED)
            raise

    async def _position_phase(
        self,
        run_id: str,
        phase: Phase,
        question: str,
        workspace: Path,
        participants: dict[str, Participant],
        positions: dict[str, Position],
        reviews: dict[str, PeerReviewBundle],
    ) -> dict[str, Position]:
        requests: dict[str, tuple[Participant, TurnRequest]] = {}
        for name, participant in participants.items():
            targeted_reviews = {
                reviewer: [review for review in bundle.reviews if review.target_participant == name]
                for reviewer, bundle in reviews.items()
            }
            requests[name] = (
                participant,
                TurnRequest(
                    run_id=run_id,
                    phase=phase,
                    question=question,
                    workspace=workspace,
                    own_position=positions.get(name),
                    peer_positions={key: value for key, value in positions.items() if key != name},
                    peer_reviews={
                        reviewer: values for reviewer, values in targeted_reviews.items() if values
                    },
                ),
            )
        results = await self.runtime.parallel(run_id, phase, requests)
        return {
            name: validate_position(workspace, result.payload)
            for name, result in results.items()
            if isinstance(result.payload, Position)
        }

    async def _review_phase(
        self,
        run_id: str,
        question: str,
        workspace: Path,
        participants: dict[str, Participant],
        positions: dict[str, Position],
    ) -> dict[str, PeerReviewBundle]:
        requests = {
            name: (
                participant,
                TurnRequest(
                    run_id=run_id,
                    phase=Phase.PEER_REVIEW,
                    question=question,
                    workspace=workspace,
                    own_position=positions[name],
                    peer_positions={key: value for key, value in positions.items() if key != name},
                ),
            )
            for name, participant in participants.items()
        }
        results = await self.runtime.parallel(run_id, Phase.PEER_REVIEW, requests)
        return {
            name: result.payload
            for name, result in results.items()
            if isinstance(result.payload, PeerReviewBundle)
        }

    async def _synthesize(
        self,
        run_id: str,
        question: str,
        workspace: Path,
        participants: dict[str, Participant],
        positions: dict[str, Position],
    ) -> FinalDecision:
        first, second = self.runtime.rotating_pair(run_id, positions)
        claim_map = {
            argument.id: argument.claim
            for position in positions.values()
            for argument in position.arguments
        }
        selected = {first: participants[first], second: participants[second]}
        requests = {
            name: (
                participant,
                TurnRequest(
                    run_id=run_id,
                    phase=Phase.SYNTHESIS,
                    question=question,
                    workspace=workspace,
                    peer_positions=positions,
                ),
            )
            for name, participant in selected.items()
        }
        results = await self.runtime.parallel(run_id, Phase.SYNTHESIS, requests)
        syntheses = {
            name: validate_synthesis(workspace, result.payload)
            for name, result in results.items()
            if isinstance(result.payload, Synthesis)
        }
        if len(syntheses) < 2:
            fallback = next(iter(syntheses.values()), None)
            if fallback:
                final = final_from_synthesis(
                    run_id,
                    fallback,
                    RunStatus.INCONCLUSIVE,
                    Confidence.LOW,
                    ["Cross synthesis could not be completed by two participants."],
                )
                return final.model_copy(
                    update={
                        "supporting_arguments": [
                            claim_map.get(item, item) for item in final.supporting_arguments
                        ]
                    }
                )
            return single_participant_final(run_id, positions[first]).model_copy(
                update={
                    "status": RunStatus.INCONCLUSIVE,
                    "warnings": ["Both synthesis calls failed."],
                }
            )

        reconciliation_requests = {
            name: (
                selected[name],
                TurnRequest(
                    run_id=run_id,
                    phase=Phase.RECONCILIATION,
                    question=question,
                    workspace=workspace,
                    syntheses=syntheses,
                ),
            )
            for name in (first, second)
        }
        reconciled_results = await self.runtime.parallel(
            run_id, Phase.RECONCILIATION, reconciliation_requests
        )
        reconciled = [
            validate_synthesis(workspace, item.payload)
            for item in reconciled_results.values()
            if isinstance(item.payload, Synthesis)
        ]
        equivalent = len(reconciled) == 2 and all(
            item.equivalent_to_peer is True for item in reconciled
        )
        if not equivalent:
            values = reconciled if reconciled else list(syntheses.values())
            return contested_final(run_id, values, claim_map)
        canonical = max(
            reconciled,
            key=lambda item: (valid_evidence_count(item.evidence), item.recommendation),
        )
        merged_evidence = unique_evidence(
            item for synthesis in reconciled for item in synthesis.evidence
        )
        # Agreement between language models is corroboration, not deterministic proof.
        # Until Ego has a claim verifier, model consensus must not produce HIGH confidence.
        confidence = Confidence.MODERATE
        confidence_reason = (
            f"{canonical.confidence_reason} Model agreement alone is corroboration, so Ego caps "
            "model-only confidence at moderate."
        )
        final = final_from_synthesis(
            run_id,
            canonical.model_copy(
                update={"evidence": merged_evidence, "confidence_reason": confidence_reason}
            ),
            RunStatus.COMPLETED,
            confidence,
            [SEMANTIC_VERIFICATION_WARNING],
        )
        return final.model_copy(
            update={
                "supporting_arguments": [
                    claim_map.get(item, item) for item in final.supporting_arguments
                ]
            }
        )
