from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from ego.models import (
    Argument,
    Confidence,
    Evidence,
    EvidenceStatus,
    FinalDecision,
    Position,
    RunStatus,
    Synthesis,
)
from ego.workspace import GitObservation, revalidate_evidence, validate_evidence


def validate_position(workspace: Path, position: Position) -> Position:
    arguments: list[Argument] = []
    for argument in position.arguments:
        arguments.append(
            argument.model_copy(
                update={
                    "evidence": [validate_evidence(workspace, item) for item in argument.evidence]
                }
            )
        )
    return position.model_copy(update={"arguments": arguments})


def validate_synthesis(workspace: Path, synthesis: Synthesis) -> Synthesis:
    return synthesis.model_copy(
        update={"evidence": [validate_evidence(workspace, item) for item in synthesis.evidence]}
    )


def valid_evidence_count(evidence: list[Evidence]) -> int:
    return sum(item.status is EvidenceStatus.VALID for item in evidence)


def unique_evidence(evidence: Iterable[Evidence]) -> list[Evidence]:
    unique: dict[tuple[str, int, int], Evidence] = {}
    for item in evidence:
        unique[(item.path, item.line_start, item.line_end)] = item
    return list(unique.values())


def single_participant_final(run_id: str, position: Position) -> FinalDecision:
    evidence = [item for argument in position.arguments for item in argument.evidence]
    return FinalDecision(
        run_id=run_id,
        status=RunStatus.COMPLETED,
        recommendation=position.recommendation,
        supporting_arguments=[item.claim for item in position.arguments],
        alternatives=position.alternatives,
        disagreements=position.disagreements,
        assumptions=position.assumptions,
        risks=position.risks,
        confidence=Confidence.LOW,
        confidence_reason="Only one participant completed the deliberation.",
        evidence=evidence,
        warnings=["This was an individual consultation, not a cross-model discussion."],
    )


def final_from_synthesis(
    run_id: str,
    synthesis: Synthesis,
    status: RunStatus,
    confidence: Confidence,
    warnings: list[str],
) -> FinalDecision:
    return FinalDecision(
        run_id=run_id,
        status=status,
        recommendation=synthesis.recommendation,
        supporting_arguments=synthesis.supporting_argument_ids,
        alternatives=synthesis.alternatives,
        disagreements=synthesis.disagreements + synthesis.material_conflicts,
        assumptions=synthesis.assumptions,
        risks=synthesis.risks,
        confidence=confidence,
        confidence_reason=synthesis.confidence_reason,
        evidence=synthesis.evidence,
        warnings=warnings,
    )


def contested_final(
    run_id: str, syntheses: list[Synthesis], claim_map: dict[str, str]
) -> FinalDecision:
    recommendations = [item.recommendation for item in syntheses]
    return FinalDecision(
        run_id=run_id,
        status=RunStatus.CONTESTED,
        recommendation="Material disagreement remains:\n- " + "\n- ".join(recommendations),
        supporting_arguments=list(
            dict.fromkeys(
                claim_map.get(argument_id, argument_id)
                for item in syntheses
                for argument_id in item.supporting_argument_ids
            )
        ),
        alternatives=list(dict.fromkeys(recommendations)),
        disagreements=list(
            dict.fromkeys(
                conflict
                for item in syntheses
                for conflict in item.disagreements + item.material_conflicts
            )
        ),
        assumptions=list(dict.fromkeys(value for item in syntheses for value in item.assumptions)),
        risks=list(dict.fromkeys(value for item in syntheses for value in item.risks)),
        confidence=Confidence.LOW,
        confidence_reason="The rotating synthesizers did not recognize equivalent conclusions.",
        evidence=unique_evidence(value for item in syntheses for value in item.evidence),
        warnings=["Ego preserved competing recommendations instead of voting."],
    )


def revalidate_final(workspace: Path, final: FinalDecision) -> FinalDecision:
    evidence = [revalidate_evidence(workspace, item) for item in final.evidence]
    stale = [item for item in evidence if item.status is EvidenceStatus.STALE]
    invalid = [item for item in evidence if item.status is EvidenceStatus.INVALID]
    warnings = list(final.warnings)
    status = final.status
    confidence = final.confidence
    if stale:
        warnings.append(f"{len(stale)} cited source(s) changed during deliberation.")
        confidence = Confidence.MODERATE if confidence is Confidence.HIGH else confidence
    if invalid:
        warnings.append(f"{len(invalid)} citation(s) could not be validated.")
    if any(item.critical for item in stale + invalid) or (
        evidence and not any(item.status is EvidenceStatus.VALID for item in evidence)
    ):
        status = RunStatus.INCONCLUSIVE
        confidence = Confidence.LOW
        warnings.append("Critical evidence is stale or no valid cited evidence remains.")
    return final.model_copy(
        update={
            "evidence": evidence,
            "warnings": warnings,
            "status": status,
            "confidence": confidence,
        }
    )


def apply_workspace_changes(
    final: FinalDecision, start: GitObservation, end: GitObservation
) -> FinalDecision:
    warnings = list(final.warnings)
    if start != end:
        warnings.append("The Git workspace state changed during deliberation.")
    return final.model_copy(update={"warnings": warnings})
