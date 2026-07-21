from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AvailabilityStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MISCONFIGURED = "misconfigured"
    UNSUPPORTED = "unsupported"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    CONTESTED = "contested"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class Phase(StrEnum):
    INDEPENDENT = "independent_reasoning"
    PEER_REVIEW = "peer_review"
    REVISION = "position_revision"
    SYNTHESIS = "cross_synthesis"
    RECONCILIATION = "reconciliation"


class Confidence(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class EvidenceStatus(StrEnum):
    UNVALIDATED = "unvalidated"
    VALID = "valid"
    INVALID = "invalid"
    STALE = "stale"


class ParticipantCapabilities(BaseModel):
    structured_output: bool = True
    model_selection: bool = True
    file_reading: bool = True
    native_read_only: bool = True


class ParticipantAvailability(BaseModel):
    participant_id: str
    status: AvailabilityStatus
    binary: str | None = None
    version: str | None = None
    model: str | None = None
    reason: str | None = None
    authentication: Literal["authenticated", "unauthenticated", "unknown"] = "unknown"
    capabilities: ParticipantCapabilities = Field(default_factory=ParticipantCapabilities)


class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    explanation: str
    critical: bool = False
    file_sha256: str | None = None
    fragment_sha256: str | None = None
    status: EvidenceStatus = EvidenceStatus.UNVALIDATED
    validation_error: str | None = None


class Argument(BaseModel):
    id: str
    claim: str
    evidence: list[Evidence] = Field(default_factory=list)


class Position(BaseModel):
    recommendation: str
    arguments: list[Argument] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: Confidence
    confidence_reason: str
    changed_position: bool = False
    change_reason: str = "Initial position"


class PeerReview(BaseModel):
    target_participant: str
    valid_points: list[str] = Field(default_factory=list)
    challenges: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    stronger_arguments: list[str] = Field(default_factory=list)


class PeerReviewBundle(BaseModel):
    reviews: list[PeerReview] = Field(default_factory=list)


class Synthesis(BaseModel):
    recommendation: str
    supporting_argument_ids: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: Confidence
    confidence_reason: str
    evidence: list[Evidence] = Field(default_factory=list)
    equivalent_to_peer: bool | None = None
    material_conflicts: list[str] = Field(default_factory=list)


class FinalDecision(BaseModel):
    run_id: str
    status: RunStatus
    recommendation: str
    supporting_arguments: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: Confidence
    confidence_reason: str
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TurnRequest(BaseModel):
    run_id: str
    phase: Phase
    question: str
    workspace: Path
    language: str = "same as the user's question"
    own_position: Position | None = None
    peer_positions: dict[str, Position] = Field(default_factory=dict)
    peer_reviews: dict[str, list[PeerReview]] = Field(default_factory=dict)
    syntheses: dict[str, Synthesis] = Field(default_factory=dict)


class ParticipantTurnResult(BaseModel):
    participant_id: str
    phase: Phase
    payload: Position | PeerReviewBundle | Synthesis
    raw_output: str
    duration_seconds: float
    model: str | None = None


class ProcessResult(BaseModel):
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


JsonObject = dict[str, Any]
DecisionState = Literal["recommended", "accepted", "rejected", "deferred", "superseded"]
