from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ego.agents import build_agent_registry
from ego.agents.investigate import InvestigationInput
from ego.decision import DecisionInput
from ego.deliberation import DeliberationOutcome, NoParticipantsError
from ego.events import WorkEvent, WorkEventStream
from ego.investigation import InvestigationOutcome
from ego.participants import Participant
from ego.redaction import redact_sensitive_text
from ego.storage import Database
from ego.workspace import resolve_workspace

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

    async def execute(self, request: BridgeRequest) -> int:
        self.run_id = None
        try:
            workspace = resolve_workspace(request.workspace)
        except (OSError, ValueError) as error:
            self._error(request.request_id, "invalid_workspace", str(error))
            return 2

        selected = list(dict.fromkeys(request.participant_ids or self.participants))
        unknown = sorted(set(selected) - set(self.participants))
        if unknown:
            self._error(
                request.request_id,
                "unknown_participants",
                f"Unknown participant(s): {', '.join(unknown)}",
            )
            return 2

        workflow_id: Literal["decision", "investigation"] = (
            "decision" if request.agent_id == "decision" else "investigation"
        )
        self.writer.emit(
            AcceptedFrame(
                request_id=request.request_id,
                agent_id=request.agent_id,
                workflow_id=workflow_id,
                workspace=str(workspace),
                participant_ids=selected,
            )
        )
        registry = build_agent_registry(self.database, self.participants)
        dispatch = asyncio.create_task(
            registry.dispatch(
                request.agent_id,
                self._agent_input(request, workspace, selected),
            )
        )
        try:
            outcome = await self._await_with_events(dispatch, request.request_id)
        except asyncio.CancelledError:
            dispatch.cancel()
            with suppress(asyncio.CancelledError):
                await dispatch
            self._drain_events(request.request_id)
            self.writer.emit(CancelledFrame(request_id=request.request_id, run_id=self.run_id))
            return 130
        except NoParticipantsError as error:
            self._drain_events(request.request_id)
            self._error(request.request_id, "no_participants", str(error), retryable=True)
            return 2
        except Exception as error:
            self._drain_events(request.request_id)
            self._error(request.request_id, "execution_failed", str(error), retryable=True)
            return 1

        self.writer.emit(self._result_frame(request, outcome))
        return 0

    async def _await_with_events(
        self,
        dispatch: asyncio.Task[Any],
        request_id: str,
    ) -> Any:
        pending_event: asyncio.Task[WorkEvent] | None = None
        try:
            while True:
                self._drain_events(request_id)
                if dispatch.done():
                    return await dispatch
                pending_event = asyncio.create_task(self.event_stream.get())
                done, _ = await asyncio.wait(
                    {dispatch, pending_event},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if pending_event in done:
                    event = pending_event.result()
                    self._emit_event(request_id, event)
                    pending_event = None
                else:
                    pending_event.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending_event
                    pending_event = None
        finally:
            if pending_event is not None and not pending_event.done():
                pending_event.cancel()
                with suppress(asyncio.CancelledError):
                    await pending_event

    def _drain_events(self, request_id: str) -> None:
        while not self.event_stream.empty():
            event = self.event_stream.get_nowait()
            self._emit_event(request_id, event)

    def _emit_event(self, request_id: str, event: WorkEvent) -> None:
        self.run_id = event.run_id
        self.writer.emit(EventFrame(request_id=request_id, event=event))

    @staticmethod
    def _agent_input(
        request: BridgeRequest,
        workspace: Path,
        selected: list[str],
    ) -> DecisionInput | InvestigationInput:
        if request.agent_id == "decision":
            return DecisionInput(
                question=request.question,
                workspace=workspace,
                participant_ids=selected,
                command="ask",
            )
        return InvestigationInput(
            question=request.question,
            workspace=workspace,
            participant_ids=selected,
        )

    @staticmethod
    def _result_frame(
        request: BridgeRequest,
        outcome: DeliberationOutcome | InvestigationOutcome,
    ) -> ResultFrame:
        if isinstance(outcome, DeliberationOutcome):
            return ResultFrame(
                request_id=request.request_id,
                run_id=outcome.final.run_id,
                agent_id="decision",
                workflow_id="decision",
                result_kind="decision",
                result=outcome.final.model_dump(mode="json"),
                decision_id=outcome.decision_id,
            )
        return ResultFrame(
            request_id=request.request_id,
            run_id=outcome.report.run_id,
            agent_id="investigate",
            workflow_id="investigation",
            result_kind="investigation_report",
            result=outcome.report.model_dump(mode="json"),
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
