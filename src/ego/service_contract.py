from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ego.bridge import BRIDGE_PROTOCOL_VERSION
from ego.models import AvailabilityStatus, ParticipantAvailability

SERVICE_PROTOCOL_VERSION: Literal[1] = 1
DEFAULT_SERVICE_PORT = 37645
DEFAULT_MAX_MESSAGE_BYTES = 64 * 1024


class ServiceAuthentication(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    nonce: str = Field(min_length=32, max_length=128)
    proof: str = Field(min_length=64, max_length=64)


class ServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    request_id: str = Field(min_length=1, max_length=128)
    method: str = Field(min_length=1, max_length=64)
    authentication: ServiceAuthentication | None = None


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


class ServiceErrorFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = SERVICE_PROTOCOL_VERSION
    kind: Literal["error"] = "error"
    request_id: str | None = None
    code: str
    message: str
    retryable: bool = False


ServiceFrame = (
    AuthenticationChallengeFrame | DiagnosticResultFrame | SchemaResultFrame | ServiceErrorFrame
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
        "methods": ["diagnostic", "schema"],
        "request": ServiceRequest.model_json_schema(),
        "frames": _SERVICE_FRAME_ADAPTER.json_schema(),
    }
