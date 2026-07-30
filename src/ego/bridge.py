from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ego.deliberation import NoParticipantsError
from ego.events import WorkEvent, WorkEventStream
from ego.participants import Participant
from ego.redaction import redact_sensitive_text
from ego.storage import Database
from ego.workflow_execution import (
    WorkflowExecutionOutcome,
    WorkflowExecutionRequest,
    WorkflowExecutionRuntime,
    WorkflowRequestError,
)

BRIDGE_PROTOCOL_VERSION: Literal[1] = 1


class BridgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = BRIDGE_PROTOCOL_VERSION
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1, max_length=128)
    agent_id: Literal["decision", "investigate"]
    question: str = Field(min_length=1)
    workspace: Path
    participant_ids: list[str] = Field(default_factory=list)


class AcceptedFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = BRIDGE_PROTOCOL_VERSION
    kind: Literal["accepted"] = "accepted"
    request_id: str
    agent_id: Literal["decision", "investigate"]
    workflow_id: Literal["decision", "investigation"]
    workspace: str
    participant_ids: list[str]


class EventFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = BRIDGE_PROTOCOL_VERSION
    kind: Literal["event"] = "event"
    request_id: str
    event: WorkEvent


class ResultFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = BRIDGE_PROTOCOL_VERSION
    kind: Literal["result"] = "result"
    request_id: str
    run_id: str
    agent_id: Literal["decision", "investigate"]
    workflow_id: Literal["decision", "investigation"]
    result_kind: Literal["decision", "investigation_report"]
    result: dict[str, Any]
    decision_id: str | None = None


class ErrorFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = BRIDGE_PROTOCOL_VERSION
    kind: Literal["error"] = "error"
    request_id: str | None = None
    run_id: str | None = None
    code: str
    message: str
    retryable: bool = False


class CancelledFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal[1] = BRIDGE_PROTOCOL_VERSION
    kind: Literal["cancelled"] = "cancelled"
    request_id: str
    run_id: str | None = None


BridgeFrame = Annotated[
    AcceptedFrame | EventFrame | ResultFrame | ErrorFrame | CancelledFrame,
    Field(discriminator="kind"),
]
_BRIDGE_FRAME_ADAPTER: TypeAdapter[BridgeFrame] = TypeAdapter(BridgeFrame)


def bridge_contract_schema() -> dict[str, object]:
    return {
        "protocol_version": BRIDGE_PROTOCOL_VERSION,
        "request": BridgeRequest.model_json_schema(),
        "frames": _BRIDGE_FRAME_ADAPTER.json_schema(),
    }


class JsonlWriter:
    def __init__(self, write: Callable[[str], None]) -> None:
        self.write = write

    def emit(self, frame: BaseModel) -> None:
        self.write(frame.model_dump_json())


class BridgeRuntime:
    def __init__(
        self,
        database: Database,
        participants: dict[str, Participant],
        event_stream: WorkEventStream,
        writer: JsonlWriter,
    ) -> None:
        self.database = database
        self.participants = participants
        self.event_stream = event_stream
        self.writer = writer
        self.run_id: str | None = None
        self.execution = WorkflowExecutionRuntime(database, participants, event_stream)

    async def execute(self, request: BridgeRequest) -> int:
        self.run_id = None
        try:
            prepared = self.execution.prepare(
                WorkflowExecutionRequest(
                    agent_id=request.agent_id,
                    question=request.question,
                    workspace=request.workspace,
                    participant_ids=request.participant_ids,
                    command="ask" if request.agent_id == "decision" else "investigate",
                )
            )
        except WorkflowRequestError as error:
            self._error(request.request_id, error.code, str(error))
            return 2

        self.writer.emit(
            AcceptedFrame(
                request_id=request.request_id,
                agent_id=prepared.agent_id,
                workflow_id=prepared.workflow_id,
                workspace=str(prepared.workspace),
                participant_ids=prepared.participant_ids,
            )
        )
        try:
            outcome = await self.execution.execute(
                prepared,
                lambda event: self._emit_event(request.request_id, event),
            )
        except asyncio.CancelledError:
            self.writer.emit(CancelledFrame(request_id=request.request_id, run_id=self.run_id))
            return 130
        except NoParticipantsError as error:
            self._error(request.request_id, "no_participants", str(error), retryable=True)
            return 2
        except Exception as error:
            self._error(request.request_id, "execution_failed", str(error), retryable=True)
            return 1

        self.writer.emit(self._result_frame(request.request_id, outcome))
        return 0

    async def _emit_event(self, request_id: str, event: WorkEvent) -> None:
        self.run_id = event.run_id
        self.writer.emit(EventFrame(request_id=request_id, event=event))

    @staticmethod
    def _result_frame(
        request_id: str,
        outcome: WorkflowExecutionOutcome,
    ) -> ResultFrame:
        return ResultFrame(
            request_id=request_id,
            run_id=outcome.run_id,
            agent_id=outcome.agent_id,
            workflow_id=outcome.workflow_id,
            result_kind=outcome.result_kind,
            result=outcome.result.model_dump(mode="json"),
            decision_id=outcome.decision_id,
        )

    def _error(
        self,
        request_id: str | None,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.writer.emit(
            ErrorFrame(
                request_id=request_id,
                run_id=self.run_id,
                code=code,
                message=redact_sensitive_text(message),
                retryable=retryable,
            )
        )


def invalid_request_frame(message: str) -> ErrorFrame:
    return ErrorFrame(
        code="invalid_request",
        message=redact_sensitive_text(message),
    )
