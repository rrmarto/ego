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


class InvestigationPhase(StrEnum):
    INDEPENDENT = "independent_investigation"
    PEER_CHALLENGE = "peer_challenge"
    REVISION = "investigation_revision"
    SYNTHESIS = "cross_synthesis"
    RECONCILIATION = "reconciliation"


class PlanPhase(StrEnum):
    INDEPENDENT = "plan_draft"
    DRAFT = "plan_draft"
    JOINT_DRAFT = "plan_joint_draft"
    AUTHOR_AUDIT = "plan_author_audit"
    FINAL_ASSEMBLY = "plan_final_assembly"


WorkStage = Phase | InvestigationPhase | PlanPhase


class Confidence(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class EvidenceStatus(StrEnum):
    UNVALIDATED = "unvalidated"
    CITATION_VERIFIED = "citation_verified"
    # Kept so historical decision records written by v0.1.0 remain readable.
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


class ToolPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    read: bool = False
    glob: bool = False
    grep: bool = False
    local_search: bool = False
    web: bool = False
    shell: bool = False
    write: bool = False
    plugins: bool = False
    mcp: bool = False
    delegation: bool = False

    @classmethod
    def local_read_only(cls) -> ToolPolicy:
        return cls(read=True, glob=True, grep=True, local_search=True)


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
    constraints: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
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
    constraints: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
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
    constraints: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    confidence: Confidence
    confidence_reason: str
    evidence: list[Evidence] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    verification_scope: str = (
        "Ego verified citation paths, line ranges, and hashes; it did not mechanically prove "
        "the semantic claims."
    )
    requires_human_resolution: bool = False

    @property
    def needs_human_resolution(self) -> bool:
        return self.requires_human_resolution or self.status is RunStatus.CONTESTED


class InvestigationFinding(BaseModel):
    claim: str
    explanation: str
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: Confidence


class InvestigationHypothesisState(StrEnum):
    SUPPORTED = "supported"
    PLAUSIBLE = "plausible"
    UNRESOLVED = "unresolved"
    REFUTED = "refuted"


class InvestigationHypothesis(BaseModel):
    hypothesis: str
    state: InvestigationHypothesisState
    supporting_evidence: list[Evidence] = Field(default_factory=list)
    counter_evidence: list[Evidence] = Field(default_factory=list)
    explanation: str


class InvestigationDraft(BaseModel):
    findings: list[InvestigationFinding] = Field(default_factory=list)
    hypotheses: list[InvestigationHypothesis] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)


class InvestigationReview(BaseModel):
    target_participant: str
    valid_points: list[str] = Field(default_factory=list)
    challenges: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    omitted_hypotheses: list[str] = Field(default_factory=list)


class InvestigationReviewBundle(BaseModel):
    reviews: list[InvestigationReview] = Field(default_factory=list)


class InvestigationSynthesis(BaseModel):
    facts: list[InvestigationFinding] = Field(default_factory=list)
    probable_causes: list[InvestigationHypothesis] = Field(default_factory=list)
    disputed_findings: list[InvestigationFinding] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    next_checks: list[str] = Field(default_factory=list)


class InvestigationReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    status: RunStatus
    question: str
    findings: list[InvestigationFinding] = Field(default_factory=list)
    hypotheses: list[InvestigationHypothesis] = Field(default_factory=list)
    disputed_findings: list[InvestigationFinding] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    next_checks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    participant_investigations: dict[str, InvestigationDraft] = Field(default_factory=dict)
    participant_reviews: dict[str, InvestigationReviewBundle] = Field(default_factory=dict)
    syntheses: dict[str, InvestigationSynthesis] = Field(default_factory=dict)


class AcceptedDecisionPackage(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_kind: Literal["decision"] = "decision"
    decision_id: str
    question: str
    workspace: Path
    conclusion: str
    conclusion_source: Literal["recommendation", "alternative", "custom"]
    rationale: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    human_note: str | None = None
    accepted_at: str


class HumanPlanBrief(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_kind: Literal["text", "file"]
    brief_id: str
    instruction: str
    source_path: str | None = None
    created_at: str


PlanSource = AcceptedDecisionPackage | HumanPlanBrief


class WorkspaceContextEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    file_sha256: str
    fragment_sha256: str
    reason: str
    content: str


class WorkspaceContextEvidenceReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    file_sha256: str
    fragment_sha256: str
    reason: str


class WorkspaceContextManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    context_id: str
    initial_context_id: str | None = None
    workspace_fingerprint: str
    evidence: list[WorkspaceContextEvidenceReference] = Field(default_factory=list)
    enrichment_evidence_ids: list[str] = Field(default_factory=list)
    discovered_evidence_ids: list[str] = Field(default_factory=list)
    discovery_bytes_used: int = Field(default=0, ge=0)
    enrichment_required_anchors: list[str] = Field(default_factory=list)
    enrichment_required_definition_anchors: list[str] = Field(default_factory=list)
    enrichment_unresolved_anchors: list[str] = Field(default_factory=list)
    enrichment_bytes_used: int = Field(default=0, ge=0)
    enrichment_byte_budget: int = Field(default=0, ge=0)
    enrichment_truncated: bool = False
    stale_evidence_ids: list[str] = Field(default_factory=list)
    omitted_paths: list[str] = Field(default_factory=list)
    truncated: bool = False
    sufficient: bool = False
    fallback_reason: str | None = None
    bytes_used: int = Field(default=0, ge=0)
    byte_budget: int = Field(default=0, ge=0)


class WorkspaceContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    manifest: WorkspaceContextManifest
    project_map: list[str] = Field(default_factory=list)
    evidence: list[WorkspaceContextEvidence] = Field(default_factory=list)


class PlanWorkspaceEvidence(BaseModel):
    """A workspace fragment discovered by an independent Plan author."""

    path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    explanation: str
    symbols: list[str] = Field(min_length=1)


class PlanTask(BaseModel):
    id: str
    title: str
    description: str
    affected_paths: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    workspace_evidence: list[PlanWorkspaceEvidence] = Field(default_factory=list)


class PlanDraft(BaseModel):
    title: str
    objective: str
    scope: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    affected_areas: list[str] = Field(default_factory=list)
    tasks: list[PlanTask] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class PlanCoverageDisposition(StrEnum):
    INCORPORATED = "incorporated"
    MERGED = "merged"
    OMITTED = "omitted"
    VARIANT = "variant"
    UNMAPPED = "unmapped"


class PlanCoverage(BaseModel):
    source_task_id: str
    disposition: PlanCoverageDisposition
    target_task_ids: list[str] = Field(default_factory=list)
    rationale: str


class PlanVariant(BaseModel):
    id: str
    question: str
    options: list[str] = Field(min_length=2)
    source_task_ids: list[str] = Field(default_factory=list)


class JointPlanDraft(BaseModel):
    draft: PlanDraft
    coverage: list[PlanCoverage] = Field(default_factory=list)
    variants: list[PlanVariant] = Field(default_factory=list)


class PlanCritiqueSeverity(StrEnum):
    ADVISORY = "advisory"
    MATERIAL = "material"


class PlanCritiqueCategory(StrEnum):
    OMISSION = "omission"
    INCORRECT_ADDITION = "incorrect_addition"
    INCORRECT_MERGE = "incorrect_merge"
    DEPENDENCY = "dependency"
    VALIDATION = "validation"
    RISK = "risk"
    CONSTRAINT = "constraint"
    VARIANT = "variant"


class PlanSection(StrEnum):
    TITLE = "title"
    OBJECTIVE = "objective"
    SCOPE = "scope"
    CONSTRAINTS = "constraints"
    NON_GOALS = "non_goals"
    AFFECTED_AREAS = "affected_areas"
    VALIDATION = "validation"
    RISKS = "risks"
    OPEN_QUESTIONS = "open_questions"


class PlanCritique(BaseModel):
    id: str
    severity: PlanCritiqueSeverity
    category: PlanCritiqueCategory
    description: str
    required_change: str
    source_task_ids: list[str] = Field(default_factory=list)
    candidate_task_ids: list[str] = Field(default_factory=list)
    candidate_sections: list[PlanSection] = Field(default_factory=list)
    candidate_variant_ids: list[str] = Field(default_factory=list)


class PlanAudit(BaseModel):
    criticisms: list[PlanCritique] = Field(default_factory=list)


class CritiqueDispositionAction(StrEnum):
    APPLIED = "applied"
    NOT_APPLICABLE = "not_applicable"
    VARIANT = "variant"


class CritiqueDisposition(BaseModel):
    critique_id: str
    action: CritiqueDispositionAction
    target_task_ids: list[str] = Field(default_factory=list)
    target_sections: list[PlanSection] = Field(default_factory=list)
    resolved_variant_ids: list[str] = Field(default_factory=list)
    introduced_open_questions: list[str] = Field(default_factory=list)
    rationale: str


class FinalPlanAssembly(BaseModel):
    draft: PlanDraft
    critique_dispositions: list[CritiqueDisposition] = Field(default_factory=list)
    variants: list[PlanVariant] = Field(default_factory=list)


class PlanState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class PlanFormat(StrEnum):
    MARKDOWN = "markdown"


class ImplementationPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    plan_id: str
    run_id: str
    state: PlanState
    format: PlanFormat
    workspace: Path
    decision_ids: list[str]
    sources: list[PlanSource] = Field(default_factory=list)
    workspace_context_manifest: WorkspaceContextManifest | None = None
    participant_plans: dict[str, PlanDraft] = Field(default_factory=dict)
    joint_draft: JointPlanDraft | None = None
    audits: dict[str, PlanAudit] = Field(default_factory=dict)
    final_assembly: FinalPlanAssembly | None = None
    variants: list[PlanVariant] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)
    artifact_path: Path
    workspace_git_head: str | None = None
    manifest_sha256: str
    plan_sha256: str
    draft: PlanDraft
    warnings: list[str] = Field(default_factory=list)


class TurnRequest(BaseModel):
    run_id: str
    phase: WorkStage
    question: str
    workspace: Path
    agent_id: str = "decision"
    workflow_id: str = "decision"
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    language: str = "same as the user's question"
    own_position: Position | None = None
    peer_positions: dict[str, Position] = Field(default_factory=dict)
    peer_reviews: dict[str, list[PeerReview]] = Field(default_factory=dict)
    syntheses: dict[str, Synthesis] = Field(default_factory=dict)
    own_investigation: InvestigationDraft | None = None
    peer_investigations: dict[str, InvestigationDraft] = Field(default_factory=dict)
    investigation_reviews: dict[str, list[InvestigationReview]] = Field(default_factory=dict)
    investigation_syntheses: dict[str, InvestigationSynthesis] = Field(default_factory=dict)
    plan_sources: list[PlanSource] = Field(default_factory=list)
    workspace_context: WorkspaceContext | None = None
    plan_candidates: dict[str, PlanDraft] = Field(default_factory=dict)
    plan_author_id: str | None = None
    own_plan: PlanDraft | None = None
    joint_plan: JointPlanDraft | None = None
    plan_audits: dict[str, PlanAudit] = Field(default_factory=dict)


class UsageMetrics(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)


class ParticipantTurnResult(BaseModel):
    participant_id: str
    phase: WorkStage
    payload: (
        Position
        | PeerReviewBundle
        | Synthesis
        | InvestigationDraft
        | InvestigationReviewBundle
        | InvestigationSynthesis
        | PlanDraft
        | JointPlanDraft
        | PlanAudit
        | FinalPlanAssembly
    )
    raw_output: str
    duration_seconds: float
    model: str | None = None
    usage: UsageMetrics | None = None


class ProcessResult(BaseModel):
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


JsonObject = dict[str, Any]
DecisionState = Literal["recommended", "accepted", "rejected", "deferred", "superseded"]
