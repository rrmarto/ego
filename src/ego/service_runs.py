from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pydantic import BaseModel

from ego.deliberation import NoParticipantsError
from ego.events import WorkEvent
from ego.redaction import redact_sensitive_text
from ego.service_contract import (
    RunAcceptedFrame,
    RunCancelledFrame,
    RunEventFrame,
    RunResultFrame,
    RunStartParameters,
    ServiceErrorFrame,
)
from ego.workflow_execution import (
    PreparedWorkflowExecution,
    WorkflowExecutionRequest,
    WorkflowExecutionRuntime,
    WorkflowRequestError,
)

DEFAULT_STREAM_BUFFER_SIZE = 128


class ServiceRunError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _RunStream:
    def __init__(self, max_frames: int) -> None:
        self._queue: asyncio.Queue[BaseModel] = asyncio.Queue(maxsize=max_frames)
        self._attached = True

    async def next(self) -> BaseModel:
        return await self._queue.get()

    def emit(self, frame: BaseModel, *, terminal: bool = False) -> None:
        if not self._attached:
            return
        if terminal:
            while self._queue.full():
                self._queue.get_nowait()
            self._queue.put_nowait(frame)
            return
        if not self._queue.full():
            self._queue.put_nowait(frame)

    def detach(self) -> None:
        self._attached = False
        while not self._queue.empty():
            self._queue.get_nowait()


@dataclass
class _ActiveRun:
    request_id: str
    prepared: PreparedWorkflowExecution
    stream: _RunStream
    task: asyncio.Task[None] | None = None
    run_id: str | None = None


class RunSubscription:
    def __init__(self, active: _ActiveRun) -> None:
        self._active = active

    @property
    def accepted(self) -> RunAcceptedFrame:
        prepared = self._active.prepared
        return RunAcceptedFrame(
            request_id=self._active.request_id,
            agent_id=prepared.agent_id,
            workflow_id=prepared.workflow_id,
            workspace=str(prepared.workspace),
            participant_ids=prepared.participant_ids,
        )

    async def next_frame(self) -> BaseModel:
        return await self._active.stream.next()

    def detach(self) -> None:
        self._active.stream.detach()


class ActiveRunCoordinator:
    """Owns the single workflow task independently from any client connection."""

    def __init__(
        self,
        execution: WorkflowExecutionRuntime,
        *,
        stream_buffer_size: int = DEFAULT_STREAM_BUFFER_SIZE,
    ) -> None:
        if stream_buffer_size < 1:
            raise ValueError("stream_buffer_size must be positive")
        self.execution = execution
        self.stream_buffer_size = stream_buffer_size
        self._active: _ActiveRun | None = None
        self._lock = asyncio.Lock()

    async def start(self, request_id: str, params: RunStartParameters) -> RunSubscription:
        async with self._lock:
            if self._active is not None:
                raise ServiceRunError(
                    "service_busy",
                    f"Ego Service is already running request {self._active.request_id}.",
                    retryable=True,
                )
            try:
                prepared = self.execution.prepare(
                    WorkflowExecutionRequest(
                        agent_id=params.agent_id,
                        question=params.question,
                        workspace=params.workspace,
                        participant_ids=params.participant_ids,
                        command="service",
                    )
                )
            except WorkflowRequestError as error:
                raise ServiceRunError(error.code, str(error)) from error

            active = _ActiveRun(
                request_id=request_id,
                prepared=prepared,
                stream=_RunStream(self.stream_buffer_size),
            )
            self._active = active
            active.task = asyncio.create_task(self._execute(active))
            return RunSubscription(active)

    async def cancel(self, target_request_id: str) -> str | None:
        async with self._lock:
            active = self._active
            if (
                active is None
                or active.request_id != target_request_id
                or active.task is None
                or active.task.done()
            ):
                raise ServiceRunError(
                    "run_not_active",
                    f"No active run has request_id {target_request_id}.",
                )
            task = active.task
            task.cancel()
        await task
        return active.run_id

    async def _execute(self, active: _ActiveRun) -> None:
        try:
            outcome = await self.execution.execute(
                active.prepared,
                lambda event: self._emit_event(active, event),
            )
        except asyncio.CancelledError:
            active.stream.emit(
                RunCancelledFrame(
                    request_id=active.request_id,
                    run_id=active.run_id,
                ),
                terminal=True,
            )
        except NoParticipantsError as error:
            active.stream.emit(
                self._error(
                    active,
                    "no_participants",
                    str(error),
                    retryable=True,
                ),
                terminal=True,
            )
        except Exception:
            active.stream.emit(
                self._error(
                    active,
                    "execution_failed",
                    "Workflow execution failed. Inspect the persisted run events and service logs.",
                    retryable=True,
                ),
                terminal=True,
            )
        else:
            if outcome.result_kind == "decision" and outcome.decision_id is None:
                active.stream.emit(
                    self._error(
                        active,
                        "missing_decision",
                        "Decision execution completed without a decision identifier.",
                    ),
                    terminal=True,
                )
            else:
                active.run_id = outcome.run_id
                active.stream.emit(
                    RunResultFrame(
                        request_id=active.request_id,
                        run_id=outcome.run_id,
                        agent_id=outcome.agent_id,
                        workflow_id=outcome.workflow_id,
                        result_kind=outcome.result_kind,
                        result=outcome.result,
                        decision_id=outcome.decision_id,
                    ),
                    terminal=True,
                )
        finally:
            async with self._lock:
                if self._active is active:
                    self._active = None

    async def _emit_event(self, active: _ActiveRun, event: WorkEvent) -> None:
        active.run_id = event.run_id
        active.stream.emit(
            RunEventFrame(
                request_id=active.request_id,
                run_id=event.run_id,
                event=event,
            )
        )

    @staticmethod
    def _error(
        active: _ActiveRun,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> ServiceErrorFrame:
        return ServiceErrorFrame(
            request_id=active.request_id,
            run_id=active.run_id,
            code=code,
            message=redact_sensitive_text(message),
            retryable=retryable,
        )
