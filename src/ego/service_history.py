from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from ego.models import FinalDecision, InvestigationReport
from ego.service_contract import (
    RunDetail,
    RunParticipantSummary,
    RunsEventsParameters,
    RunsEventsResult,
    RunsGetParameters,
    RunsListParameters,
    RunsListResult,
    RunSummary,
)
from ego.storage import Database


class ServiceHistoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ServiceHistory:
    """Builds bounded public read models from Ego-owned persistence."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def list_runs(self, params: RunsListParameters) -> RunsListResult:
        before_created_at, before_id = self._decode_cursor(params.cursor)
        rows, has_more = self.database.list_runs_page(
            limit=params.limit,
            before_created_at=before_created_at,
            before_id=before_id,
            agent_id=params.agent_id,
        )
        runs = [self._summary(row) for row in rows]
        next_cursor = None
        if has_more and rows:
            next_cursor = self._encode_cursor(
                created_at=str(rows[-1]["created_at"]),
                run_id=str(rows[-1]["id"]),
            )
        return RunsListResult(runs=runs, next_cursor=next_cursor)

    def get_run(self, params: RunsGetParameters) -> RunDetail:
        try:
            row = self.database.get_public_run(params.run_id)
        except KeyError as error:
            raise ServiceHistoryError(
                "run_not_found",
                f"No run has id {params.run_id}.",
            ) from error
        result = row["result"]
        typed_result: FinalDecision | InvestigationReport | None
        if result is None:
            typed_result = None
        elif row["result_kind"] == "decision":
            typed_result = FinalDecision.model_validate(result)
        else:
            typed_result = InvestigationReport.model_validate(result)
        summary = self._summary(row)
        return RunDetail(
            **summary.model_dump(),
            participants=[
                RunParticipantSummary.model_validate(item) for item in row["participants"]
            ],
            result=typed_result,
            decision_id=row["decision_id"],
        )

    def get_events(self, params: RunsEventsParameters) -> RunsEventsResult:
        try:
            events = self.database.get_run_events(
                params.run_id,
                after_event_id=params.after_event_id,
                limit=params.limit,
            )
        except KeyError as error:
            raise ServiceHistoryError(
                "run_not_found",
                f"No run has id {params.run_id}.",
            ) from error
        next_after = events[-1].event_id if events else params.after_event_id
        return RunsEventsResult(
            run_id=params.run_id,
            events=events,
            next_after_event_id=next_after,
        )

    @staticmethod
    def _summary(row: dict[str, Any]) -> RunSummary:
        return RunSummary(
            run_id=row["id"],
            question=row["question"],
            workspace=row["workspace"],
            status=row["status"],
            agent_id=row["agent_id"],
            workflow_id=row["workflow_id"],
            result_kind=row["result_kind"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _encode_cursor(*, created_at: str, run_id: str) -> str:
        encoded = json.dumps(
            {"created_at": created_at, "run_id": run_id},
            separators=(",", ":"),
        ).encode()
        return base64.urlsafe_b64encode(encoded).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[str | None, str | None]:
        if cursor is None:
            return None, None
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode())
            created_at = value["created_at"]
            run_id = value["run_id"]
            if not isinstance(created_at, str) or not isinstance(run_id, str):
                raise TypeError
            return created_at, run_id
        except (
            binascii.Error,
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise ServiceHistoryError("invalid_cursor", "Run cursor is invalid.") from error
