from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ego.agents import build_agent_registry
from ego.agents.investigate import InvestigationInput
from ego.decision import DecisionInput
from ego.deliberation import DeliberationOutcome
from ego.events import WorkEvent, WorkEventStream
from ego.investigation import InvestigationOutcome
from ego.models import FinalDecision, InvestigationReport
from ego.participants import Participant
from ego.storage import Database
from ego.workspace import resolve_workspace

WorkflowAgentId = Literal["decision", "investigate"]
WorkflowId = Literal["decision", "investigation"]
WorkflowResultKind = Literal["decision", "investigation_report"]
EventSink = Callable[[WorkEvent], Awaitable[None]]


class WorkflowRequestError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkflowExecutionRequest:
    agent_id: WorkflowAgentId
    question: str
    workspace: Path
    participant_ids: list[str]
    command: str


@dataclass(frozen=True)
class PreparedWorkflowExecution:
    agent_id: WorkflowAgentId
    workflow_id: WorkflowId
    question: str
    workspace: Path
    participant_ids: list[str]
    command: str


@dataclass(frozen=True)
class WorkflowExecutionOutcome:
    run_id: str
    agent_id: WorkflowAgentId
    workflow_id: WorkflowId
    result_kind: WorkflowResultKind
    result: FinalDecision | InvestigationReport
    decision_id: str | None = None


class WorkflowExecutionRuntime:
    """One in-process execution path shared by bridge and service transports."""

    def __init__(
        self,
        database: Database,
        participants: dict[str, Participant],
        event_stream: WorkEventStream,
    ) -> None:
        self.database = database
        self.participants = participants
        self.event_stream = event_stream

    def prepare(self, request: WorkflowExecutionRequest) -> PreparedWorkflowExecution:
        try:
            workspace = resolve_workspace(request.workspace)
        except (OSError, ValueError) as error:
            raise WorkflowRequestError("invalid_workspace", str(error)) from error

        selected = list(dict.fromkeys(request.participant_ids or self.participants))
        unknown = sorted(set(selected) - set(self.participants))
        if unknown:
            raise WorkflowRequestError(
                "unknown_participants",
                f"Unknown participant(s): {', '.join(unknown)}",
            )

        workflow_id: WorkflowId = (
            "decision" if request.agent_id == "decision" else "investigation"
        )
        return PreparedWorkflowExecution(
            agent_id=request.agent_id,
            workflow_id=workflow_id,
            question=request.question,
            workspace=workspace,
            participant_ids=selected,
            command=request.command,
        )

    async def execute(
        self,
        request: PreparedWorkflowExecution,
        emit_event: EventSink,
    ) -> WorkflowExecutionOutcome:
        registry = build_agent_registry(self.database, self.participants)
        dispatch = asyncio.create_task(
            registry.dispatch(request.agent_id, self._agent_input(request))
        )
        try:
            outcome = await self._await_with_events(dispatch, emit_event)
        except asyncio.CancelledError:
            dispatch.cancel()
            with suppress(asyncio.CancelledError):
                await dispatch
            await self._drain_events(emit_event)
            raise
        except BaseException:
            await self._drain_events(emit_event)
            raise
        await self._drain_events(emit_event)
        return self._outcome(outcome)

    async def _await_with_events(
        self,
        dispatch: asyncio.Task[DeliberationOutcome | InvestigationOutcome],
        emit_event: EventSink,
    ) -> DeliberationOutcome | InvestigationOutcome:
        pending_event: asyncio.Task[WorkEvent] | None = None
        try:
            while True:
                await self._drain_events(emit_event)
                if dispatch.done():
                    return await dispatch
                pending_event = asyncio.create_task(self.event_stream.get())
                done, _ = await asyncio.wait(
                    {dispatch, pending_event},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if pending_event in done:
                    await emit_event(pending_event.result())
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

    async def _drain_events(self, emit_event: EventSink) -> None:
        while not self.event_stream.empty():
            await emit_event(self.event_stream.get_nowait())

    @staticmethod
    def _agent_input(
        request: PreparedWorkflowExecution,
    ) -> DecisionInput | InvestigationInput:
        if request.agent_id == "decision":
            return DecisionInput(
                question=request.question,
                workspace=request.workspace,
                participant_ids=request.participant_ids,
                command=request.command,
            )
        return InvestigationInput(
            question=request.question,
            workspace=request.workspace,
            participant_ids=request.participant_ids,
            command=request.command,
        )

    @staticmethod
    def _outcome(
        outcome: DeliberationOutcome | InvestigationOutcome,
    ) -> WorkflowExecutionOutcome:
        if isinstance(outcome, DeliberationOutcome):
            return WorkflowExecutionOutcome(
                run_id=outcome.final.run_id,
                agent_id="decision",
                workflow_id="decision",
                result_kind="decision",
                result=outcome.final,
                decision_id=outcome.decision_id,
            )
        return WorkflowExecutionOutcome(
            run_id=outcome.report.run_id,
            agent_id="investigate",
            workflow_id="investigation",
            result_kind="investigation_report",
            result=outcome.report,
        )
