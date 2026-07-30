from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from ego.bridge import BRIDGE_PROTOCOL_VERSION
from ego.events import WorkEvent
from ego.models import (
    AvailabilityStatus,
    FinalDecision,
    InvestigationReport,
    ParticipantAvailability,
    RunStatus,
)

SERVICE_PROTOCOL_VERSION: Literal[1] = 1
DEFAULT_SERVICE_PORT = 37645
DEFAULT_MAX_MESSAGE_BYTES = 64 * 1024


class ServiceAuthentication(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    nonce: str = Field(min_length=32, max_length=128)
    proof: str = Field(min_length=64, max_length=64)


class RunStartParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: Literal["decision", "investigate"]
    question: str = Field(min_length=1, max_length=16_384)
    workspace: Path
    participant_ids: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("workspace")
    @classmethod
    def require_absolute_workspace(cls, workspace: Path) -> Path:
        if not workspace.is_absolute():
            raise ValueError("workspace must be an absolute path")
        return workspace


class RunCancelParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_request_id: str = Field(min_length=1, max_length=128)


class RunsListParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=512)
    agent_id: Literal["decision", "investigate"] | None = None


class RunsGetParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=128)


class RunsEventsParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1, max_length=128)
    after_event_id: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)


class DecisionTransitionParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str = Field(min_length=1, max_length=128)
    state: Literal["accepted", "rejected", "deferred"]
    note: str | None = Field(default=None, max_length=4096)


class DecisionResolveParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str = Field(min_length=1, max_length=128)
    alternative_index: int | None = Field(default=None, ge=1)
    custom_text: str | None = Field(default=None, min_length=1, max_length=16_384)
    note: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def choose_one_resolution(self) -> DecisionResolveParameters:
        if (self.alternative_index is None) == (self.custom_text is None):
            raise ValueError("choose exactly one alternative or custom_text")
        return self


ServiceParameters = (
    RunStartParameters
    | RunCancelParameters
    | RunsListParameters
    | RunsGetParameters
    | RunsEventsParameters
    | DecisionTransitionParameters
    | DecisionResolveParameters
)


class ServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    request_id: str = Field(min_length=1, max_length=128)
    method: str = Field(min_length=1, max_length=64)
    authentication: ServiceAuthentication | None = None
    params: ServiceParameters | None = None

    @model_validator(mode="before")
    @classmethod
    def parse_method_parameters(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        parameter_models: dict[str, type[BaseModel]] = {
            "run.start": RunStartParameters,
            "run.cancel": RunCancelParameters,
            "runs.list": RunsListParameters,
            "runs.get": RunsGetParameters,
            "runs.events": RunsEventsParameters,
            "decision.transition": DecisionTransitionParameters,
            "decision.resolve": DecisionResolveParameters,
        }
        method = value.get("method")
        if not isinstance(method, str):
            return value
        model = parameter_models.get(method)
        if model is None:
            return value
        parsed = dict(value)
        parsed["params"] = model.model_validate(value.get("params"))
        return parsed

    @model_validator(mode="after")
    def validate_method_parameters(self) -> ServiceRequest:
        if self.method in {"diagnostic", "schema"} and self.params is not None:
            raise ValueError(f"{self.method} does not accept params")
        if self.method == "run.start" and not isinstance(self.params, RunStartParameters):
            raise ValueError("run.start requires RunStartParameters")
        if self.method == "run.cancel" and not isinstance(self.params, RunCancelParameters):
            raise ValueError("run.cancel requires RunCancelParameters")
        if self.method == "runs.list" and not isinstance(self.params, RunsListParameters):
            raise ValueError("runs.list requires RunsListParameters")
        if self.method == "runs.get" and not isinstance(self.params, RunsGetParameters):
            raise ValueError("runs.get requires RunsGetParameters")
        if self.method == "runs.events" and not isinstance(self.params, RunsEventsParameters):
            raise ValueError("runs.events requires RunsEventsParameters")
        if self.method == "decision.transition" and not isinstance(
            self.params, DecisionTransitionParameters
        ):
            raise ValueError("decision.transition requires DecisionTransitionParameters")
        if self.method == "decision.resolve" and not isinstance(
            self.params, DecisionResolveParameters
        ):
            raise ValueError("decision.resolve requires DecisionResolveParameters")
        return self


class ServiceCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    structured_output: bool
    model_selection: bool
    file_reading: bool
    native_read_only: bool


class ServiceParticipantDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    participant_id: str
    status: AvailabilityStatus
    binary: str | None = None
    version: str | None = None
    model: str | None = None
    authentication: Literal["authenticated", "unauthenticated", "unknown"]
    capabilities: ServiceCapabilities
    reason: str | None = None

    @classmethod
    def from_availability(
        cls, availability: ParticipantAvailability
    ) -> ServiceParticipantDiagnostic:
        return cls(
            participant_id=availability.participant_id,
            status=availability.status,
            binary=availability.binary,
            version=availability.version,
            model=availability.model,
            authentication=availability.authentication,
            capabilities=ServiceCapabilities.model_validate(
                availability.capabilities.model_dump()
            ),
            reason=availability.reason,
        )


class ServiceSeatbeltDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    safe: bool
    reason: str


class ServiceDiagnosticError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    action: str
    participant_id: str | None = None


class ServiceDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service_protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    ego_version: str
    bridge_protocol_version: Literal[1] = BRIDGE_PROTOCOL_VERSION
    ego_executable: str
    seatbelt: ServiceSeatbeltDiagnostic
    participants: list[ServiceParticipantDiagnostic]
    errors: list[ServiceDiagnosticError]


class AuthenticationChallengeFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["authentication_challenge"] = "authentication_challenge"
    nonce: str
    proof: str
    algorithm: Literal["hmac-sha256"] = "hmac-sha256"


class DiagnosticResultFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["result"] = "result"
    request_id: str
    method: Literal["diagnostic"] = "diagnostic"
    result: ServiceDiagnostic


class SchemaResultFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["result"] = "result"
    request_id: str
    method: Literal["schema"] = "schema"
    result: dict[str, Any]


class RunAcceptedFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["accepted"] = "accepted"
    request_id: str
    method: Literal["run.start"] = "run.start"
    agent_id: Literal["decision", "investigate"]
    workflow_id: Literal["decision", "investigation"]
    workspace: str
    participant_ids: list[str]


class RunEventFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["event"] = "event"
    request_id: str
    method: Literal["run.start"] = "run.start"
    run_id: str
    event: WorkEvent


class RunResultFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["result"] = "result"
    request_id: str
    method: Literal["run.start"] = "run.start"
    run_id: str
    agent_id: Literal["decision", "investigate"]
    workflow_id: Literal["decision", "investigation"]
    result_kind: Literal["decision", "investigation_report"]
    result: FinalDecision | InvestigationReport
    decision_id: str | None = None


class RunCancelledFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["cancelled"] = "cancelled"
    request_id: str
    method: Literal["run.start"] = "run.start"
    run_id: str | None = None


class RunCancelResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_request_id: str
    run_id: str | None = None
    status: Literal["cancelled"] = "cancelled"


class RunCancelResultFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["result"] = "result"
    request_id: str
    method: Literal["run.cancel"] = "run.cancel"
    result: RunCancelResult


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    question: str
    workspace: str
    status: RunStatus
    agent_id: Literal["decision", "investigate"]
    workflow_id: Literal["decision", "investigation"]
    result_kind: Literal["decision", "investigation_report"]
    created_at: datetime
    updated_at: datetime


class RunsListResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runs: list[RunSummary]
    next_cursor: str | None = None


class RunsListResultFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["result"] = "result"
    request_id: str
    method: Literal["runs.list"] = "runs.list"
    result: RunsListResult


class RunParticipantSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    participant_id: str
    status: AvailabilityStatus
    version: str | None = None
    model: str | None = None
    reason: str | None = None


class RunDetail(RunSummary):
    participants: list[RunParticipantSummary]
    result: FinalDecision | InvestigationReport | None = None
    decision_id: str | None = None


class RunDetailResultFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["result"] = "result"
    request_id: str
    method: Literal["runs.get"] = "runs.get"
    result: RunDetail


class RunsEventsResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    events: list[WorkEvent]
    next_after_event_id: int


class RunsEventsResultFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["result"] = "result"
    request_id: str
    method: Literal["runs.events"] = "runs.events"
    result: RunsEventsResult


class DecisionTransitionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    state: Literal["accepted", "rejected", "deferred"]


class DecisionTransitionResultFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["result"] = "result"
    request_id: str
    method: Literal["decision.transition"] = "decision.transition"
    result: DecisionTransitionResult


class DecisionResolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    resolution_type: Literal["alternative", "custom"]
    alternative_index: int | None = None
    recommendation: str
    note: str | None = None
    state: Literal["accepted"] = "accepted"


class DecisionResolutionResultFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["result"] = "result"
    request_id: str
    method: Literal["decision.resolve"] = "decision.resolve"
    result: DecisionResolutionResult


class ServiceErrorFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["error"] = "error"
    request_id: str | None = None
    run_id: str | None = None
    code: str
    message: str
    retryable: bool = False


ServiceFrame = (
    AuthenticationChallengeFrame
    | DiagnosticResultFrame
    | SchemaResultFrame
    | RunAcceptedFrame
    | RunEventFrame
    | RunResultFrame
    | RunCancelledFrame
    | RunCancelResultFrame
    | RunsListResultFrame
    | RunDetailResultFrame
    | RunsEventsResultFrame
    | DecisionTransitionResultFrame
    | DecisionResolutionResultFrame
    | ServiceErrorFrame
)
_SERVICE_FRAME_ADAPTER: TypeAdapter[ServiceFrame] = TypeAdapter(ServiceFrame)


def service_contract_schema() -> dict[str, object]:
    return {
        "protocol_version": SERVICE_PROTOCOL_VERSION,
        "transport": {
            "format": "newline-delimited JSON",
            "bind_host": "127.0.0.1",
            "default_port": DEFAULT_SERVICE_PORT,
            "default_max_message_bytes": DEFAULT_MAX_MESSAGE_BYTES,
        },
        "methods": [
            "diagnostic",
            "schema",
            "run.start",
            "run.cancel",
            "runs.list",
            "runs.get",
            "runs.events",
            "decision.transition",
            "decision.resolve",
        ],
        "request": ServiceRequest.model_json_schema(),
        "frames": _SERVICE_FRAME_ADAPTER.json_schema(),
    }
