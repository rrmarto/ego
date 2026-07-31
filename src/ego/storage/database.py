from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from ego.config import AppPaths
from ego.events import WorkEvent, WorkEventStream, WorkEventType
from ego.models import (
    AcceptedDecisionPackage,
    DecisionState,
    FinalDecision,
    ImplementationPlan,
    InvestigationPhase,
    JsonObject,
    ParticipantAvailability,
    ParticipantTurnResult,
    Phase,
    PlanPhase,
    PlanState,
    RunStatus,
    UsageMetrics,
    WorkStage,
)
from ego.redaction import redact_sensitive_text

SCHEMA_VERSION = 5


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _work_stage(value: str, agent_id: str = "decision") -> WorkStage:
    if agent_id == "plan":
        return PlanPhase(value)
    if agent_id == "investigate":
        return InvestigationPhase(value)
    try:
        return Phase(value)
    except ValueError:
        return InvestigationPhase(value)


class Database:
    def __init__(
        self,
        paths: AppPaths,
        *,
        event_stream: WorkEventStream | None = None,
    ) -> None:
        self.paths = paths
        self.event_stream = event_stream
        paths.ensure()
        self._migrate()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.paths.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"database schema {version} is newer than Ego supports")
            if version == 0:
                connection.executescript(
                    """
                    CREATE TABLE runs (
                        id TEXT PRIMARY KEY,
                        command TEXT NOT NULL,
                        question TEXT NOT NULL,
                        workspace TEXT NOT NULL,
                        status TEXT NOT NULL,
                        parent_decision_id TEXT,
                        git_head_start TEXT,
                        git_status_start TEXT,
                        git_head_end TEXT,
                        git_status_end TEXT,
                        final_json TEXT,
                        agent_id TEXT NOT NULL DEFAULT 'decision',
                        workflow_id TEXT NOT NULL DEFAULT 'decision',
                        result_kind TEXT NOT NULL DEFAULT 'decision',
                        result_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE run_participants (
                        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                        participant_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        version TEXT,
                        model TEXT,
                        reason TEXT,
                        PRIMARY KEY (run_id, participant_id)
                    );
                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                        event_type TEXT NOT NULL,
                        agent_id TEXT NOT NULL DEFAULT 'decision',
                        workflow_id TEXT NOT NULL DEFAULT 'decision',
                        stage TEXT,
                        participant_id TEXT,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE calls (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                        participant_id TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        status TEXT NOT NULL,
                        duration_seconds REAL,
                        model TEXT,
                        input_tokens INTEGER,
                        output_tokens INTEGER,
                        cached_input_tokens INTEGER,
                        total_tokens INTEGER,
                        cost_usd REAL,
                        raw_path TEXT,
                        parsed_json TEXT,
                        error TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE decisions (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
                        state TEXT NOT NULL,
                        record_json TEXT NOT NULL,
                        supersedes_id TEXT REFERENCES decisions(id),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE decision_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        decision_id TEXT NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
                        state TEXT NOT NULL,
                        note TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE decision_resolutions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        decision_id TEXT NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
                        resolution_type TEXT NOT NULL,
                        alternative_index INTEGER,
                        recommendation TEXT NOT NULL,
                        note TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX events_run_id_idx ON events(run_id, id);
                    CREATE INDEX calls_run_id_idx ON calls(run_id, created_at);
                    CREATE INDEX decision_resolutions_decision_id_idx
                    ON decision_resolutions(decision_id, id);
                    PRAGMA user_version = 4;
                    """
                )
                version = 4
            if version < 2:
                connection.executescript(
                    """
                    CREATE TABLE decision_resolutions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        decision_id TEXT NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
                        resolution_type TEXT NOT NULL,
                        alternative_index INTEGER,
                        recommendation TEXT NOT NULL,
                        note TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX decision_resolutions_decision_id_idx
                    ON decision_resolutions(decision_id, id);
                    PRAGMA user_version = 2;
                    """
                )
                version = 2
            if version < 3:
                connection.executescript(
                    """
                    ALTER TABLE calls ADD COLUMN input_tokens INTEGER;
                    ALTER TABLE calls ADD COLUMN output_tokens INTEGER;
                    ALTER TABLE calls ADD COLUMN cached_input_tokens INTEGER;
                    ALTER TABLE calls ADD COLUMN total_tokens INTEGER;
                    ALTER TABLE calls ADD COLUMN cost_usd REAL;
                    PRAGMA user_version = 3;
                    """
                )
                version = 3
            if version < 4:
                self._add_column(
                    connection, "runs", "agent_id", "TEXT NOT NULL DEFAULT 'decision'"
                )
                self._add_column(
                    connection, "runs", "workflow_id", "TEXT NOT NULL DEFAULT 'decision'"
                )
                self._add_column(
                    connection, "runs", "result_kind", "TEXT NOT NULL DEFAULT 'decision'"
                )
                self._add_column(connection, "runs", "result_json", "TEXT")
                self._add_column(
                    connection, "events", "agent_id", "TEXT NOT NULL DEFAULT 'decision'"
                )
                self._add_column(
                    connection, "events", "workflow_id", "TEXT NOT NULL DEFAULT 'decision'"
                )
                self._add_column(connection, "events", "stage", "TEXT")
                connection.execute(
                    """UPDATE runs SET agent_id = 'decision', workflow_id = 'decision',
                    result_kind = 'decision', result_json = COALESCE(result_json, final_json)"""
                )
                connection.execute(
                    """UPDATE events SET agent_id = 'decision', workflow_id = 'decision',
                    stage = COALESCE(stage, json_extract(payload_json, '$.phase'))"""
                )
                connection.execute("PRAGMA user_version = 4")
                version = 4
            if version < 5:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS plans (
                        id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL UNIQUE REFERENCES runs(id),
                        state TEXT NOT NULL,
                        format TEXT NOT NULL,
                        artifact_path TEXT NOT NULL,
                        manifest_sha256 TEXT NOT NULL,
                        plan_sha256 TEXT NOT NULL,
                        plan_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS plan_decisions (
                        plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                        decision_id TEXT NOT NULL REFERENCES decisions(id),
                        ordinal INTEGER NOT NULL,
                        PRIMARY KEY (plan_id, decision_id)
                    );
                    CREATE TABLE IF NOT EXISTS plan_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
                        state TEXT NOT NULL,
                        note TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS plan_decisions_decision_id_idx
                    ON plan_decisions(decision_id, plan_id);
                    PRAGMA user_version = 5;
                    """
                )
                version = 5

    @staticmethod
    def _add_column(
        connection: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        existing = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def create_run(
        self,
        *,
        command: str,
        question: str,
        workspace: Path,
        parent_decision_id: str | None = None,
        git_head: str | None = None,
        git_status: str | None = None,
        agent_id: str = "decision",
        workflow_id: str = "decision",
        result_kind: str = "decision",
    ) -> str:
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO runs
                (id, command, question, workspace, status, parent_decision_id,
                 git_head_start, git_status_start, agent_id, workflow_id, result_kind,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    command,
                    question,
                    str(workspace),
                    RunStatus.CREATED.value,
                    parent_decision_id,
                    git_head,
                    git_status,
                    agent_id,
                    workflow_id,
                    result_kind,
                    now,
                    now,
                ),
            )
            event = self._event(
                connection,
                run_id,
                WorkEventType.RUN_CREATED,
                {
                    "command": command,
                    "workspace": str(workspace),
                    "agent_id": agent_id,
                    "workflow_id": workflow_id,
                },
            )
        self._publish(event)
        return run_id

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: WorkEventType,
        payload: JsonObject,
        participant_id: str | None = None,
    ) -> WorkEvent:
        created_at = datetime.now(UTC)
        run = connection.execute(
            "SELECT agent_id, workflow_id FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise KeyError(run_id)
        stage_value = payload.get("stage", payload.get("phase"))
        cursor = connection.execute(
            """INSERT INTO events
            (run_id, event_type, agent_id, workflow_id, stage, participant_id,
             payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                event_type.value,
                run["agent_id"],
                run["workflow_id"],
                str(stage_value) if stage_value is not None else None,
                participant_id,
                json.dumps(payload),
                created_at.isoformat(),
            ),
        )
        event_id = cursor.lastrowid
        if event_id is None:
            raise RuntimeError("SQLite did not assign an event id")
        phase = (
            _work_stage(str(stage_value), str(run["agent_id"]))
            if stage_value is not None
            else None
        )
        return WorkEvent(
            event_id=event_id,
            run_id=run_id,
            event_type=event_type,
            agent_id=run["agent_id"],
            workflow_id=run["workflow_id"],
            participant_id=participant_id,
            phase=phase,
            stage=str(stage_value) if stage_value is not None else None,
            payload=payload,
            created_at=created_at,
        )

    def _publish(self, event: WorkEvent) -> None:
        if self.event_stream is not None:
            self.event_stream.publish(event)

    def add_event(
        self,
        run_id: str,
        event_type: WorkEventType,
        payload: JsonObject,
        participant_id: str | None = None,
    ) -> WorkEvent:
        with self.connect() as connection:
            event = self._event(connection, run_id, event_type, payload, participant_id)
        self._publish(event)
        return event

    def add_participant(self, run_id: str, availability: ParticipantAvailability) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO run_participants
                (run_id, participant_id, status, version, model, reason)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    availability.participant_id,
                    availability.status.value,
                    availability.version,
                    availability.model,
                    availability.reason,
                ),
            )

    def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        final: FinalDecision | None = None,
        result: BaseModel | None = None,
        git_head: str | None = None,
        git_status: str | None = None,
    ) -> None:
        stored_result = result or final
        serialized_result = stored_result.model_dump_json() if stored_result else None
        now = utc_now()
        with self.connect() as connection:
            current = connection.execute(
                "SELECT result_json FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if current is None:
                raise KeyError(run_id)
            if (
                serialized_result is not None
                and current["result_json"] is not None
                and current["result_json"] != serialized_result
            ):
                raise ValueError("run result is immutable once stored")
            connection.execute(
                """UPDATE runs SET status = ?, final_json = COALESCE(?, final_json),
                result_json = COALESCE(?, result_json),
                git_head_end = COALESCE(?, git_head_end),
                git_status_end = COALESCE(?, git_status_end), updated_at = ? WHERE id = ?""",
                (
                    status.value,
                    final.model_dump_json() if final else None,
                    serialized_result,
                    git_head,
                    git_status,
                    now,
                    run_id,
                ),
            )
            event = self._event(
                connection,
                run_id,
                WorkEventType.RUN_STATUS_CHANGED,
                {"status": status.value},
            )
        self._publish(event)

    def record_call(
        self,
        run_id: str,
        result: ParticipantTurnResult | None,
        *,
        participant_id: str,
        phase: str,
        error: str | None = None,
        raw_output: str | None = None,
        duration_seconds: float | None = None,
        model: str | None = None,
        usage: UsageMetrics | None = None,
    ) -> None:
        call_id = str(uuid.uuid4())
        raw_path: Path | None = None
        captured_output = result.raw_output if result else raw_output
        if captured_output is not None:
            directory = self.paths.raw_dir / run_id
            directory.mkdir(parents=True, exist_ok=True)
            raw_path = directory / f"{call_id}.txt"
            raw_path.write_text(redact_sensitive_text(captured_output), encoding="utf-8")
        call_usage = result.usage if result else usage
        call_duration = result.duration_seconds if result else duration_seconds
        call_model = result.model if result else model
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO calls
                (id, run_id, participant_id, phase, status, duration_seconds, model,
                 input_tokens, output_tokens, cached_input_tokens, total_tokens, cost_usd,
                 raw_path, parsed_json, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    call_id,
                    run_id,
                    participant_id,
                    phase,
                    "completed" if result else "failed",
                    call_duration,
                    call_model,
                    call_usage.input_tokens if call_usage else None,
                    call_usage.output_tokens if call_usage else None,
                    call_usage.cached_input_tokens if call_usage else None,
                    call_usage.total_tokens if call_usage else None,
                    call_usage.cost_usd if call_usage else None,
                    str(raw_path) if raw_path else None,
                    result.payload.model_dump_json() if result else None,
                    redact_sensitive_text(error) if error else None,
                    utc_now(),
                ),
            )
            event = self._event(
                connection,
                run_id,
                WorkEventType.PARTICIPANT_TURN_COMPLETED
                if result
                else WorkEventType.PARTICIPANT_TURN_FAILED,
                {
                    "call_id": call_id,
                    "phase": phase,
                    "duration_seconds": call_duration,
                    "model": call_model,
                    "usage": call_usage.model_dump(mode="json") if call_usage else None,
                    "error": redact_sensitive_text(error) if error else None,
                },
                participant_id,
            )
        self._publish(event)

    def get_call(self, call_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT id, run_id, participant_id, phase, status, duration_seconds,
                model, input_tokens, output_tokens, cached_input_tokens, total_tokens,
                cost_usd, parsed_json, error, created_at FROM calls WHERE id = ?""",
                (call_id,),
            ).fetchone()
        if row is None:
            raise KeyError(call_id)
        return dict(row)

    def create_decision(self, final: FinalDecision, *, supersedes_id: str | None = None) -> str:
        decision_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO decisions
                (id, run_id, state, record_json, supersedes_id, created_at, updated_at)
                VALUES (?, ?, 'recommended', ?, ?, ?, ?)""",
                (decision_id, final.run_id, final.model_dump_json(), supersedes_id, now, now),
            )
            connection.execute(
                """INSERT INTO decision_events (decision_id, state, note, created_at)
                VALUES (?, 'recommended', NULL, ?)""",
                (decision_id, now),
            )
            event = self._event(
                connection,
                final.run_id,
                WorkEventType.DECISION_CREATED,
                {"decision_id": decision_id, "state": "recommended"},
            )
        self._publish(event)
        return decision_id

    def transition_decision(self, decision_id: str, state: DecisionState, note: str | None) -> None:
        if state not in {"accepted", "rejected", "deferred"}:
            raise ValueError(f"unsupported user transition: {state}")
        with self.connect() as connection:
            current = connection.execute(
                "SELECT state, supersedes_id, record_json FROM decisions WHERE id = ?",
                (decision_id,),
            ).fetchone()
            if current is None:
                raise KeyError(decision_id)
            record = FinalDecision.model_validate_json(current["record_json"])
            if state == "accepted" and record.needs_human_resolution:
                raise ValueError(
                    "contested decisions require selecting an alternative or recording a custom "
                    "human decision"
                )
            connection.execute(
                "UPDATE decisions SET state = ?, updated_at = ? WHERE id = ?",
                (state, utc_now(), decision_id),
            )
            connection.execute(
                """INSERT INTO decision_events (decision_id, state, note, created_at)
                VALUES (?, ?, ?, ?)""",
                (decision_id, state, note, utc_now()),
            )
            if state == "accepted" and current["supersedes_id"]:
                previous_id = current["supersedes_id"]
                connection.execute(
                    "UPDATE decisions SET state = 'superseded', updated_at = ? WHERE id = ?",
                    (utc_now(), previous_id),
                )
                connection.execute(
                    """INSERT INTO decision_events (decision_id, state, note, created_at)
                    VALUES (?, 'superseded', ?, ?)""",
                    (previous_id, f"Superseded by accepted decision {decision_id}", utc_now()),
                )

    def resolve_decision(
        self,
        decision_id: str,
        *,
        alternative_index: int | None = None,
        custom_text: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        if (alternative_index is None) == (custom_text is None):
            raise ValueError("choose exactly one alternative or provide one custom decision")
        with self.connect() as connection:
            current = connection.execute(
                "SELECT state, supersedes_id, record_json FROM decisions WHERE id = ?",
                (decision_id,),
            ).fetchone()
            if current is None:
                raise KeyError(decision_id)
            record = FinalDecision.model_validate_json(current["record_json"])
            if not record.needs_human_resolution:
                raise ValueError("this decision does not require a contested-result resolution")
            if current["state"] in {"accepted", "superseded"}:
                raise ValueError(f"decision is already {current['state']}")

            if alternative_index is not None:
                if alternative_index < 1 or alternative_index > len(record.alternatives):
                    raise ValueError(
                        f"alternative must be between 1 and {len(record.alternatives)}"
                    )
                recommendation = record.alternatives[alternative_index - 1]
                resolution_type = "alternative"
            else:
                recommendation = (custom_text or "").strip()
                if not recommendation:
                    raise ValueError("custom decision cannot be empty")
                resolution_type = "custom"

            now = utc_now()
            connection.execute(
                """INSERT INTO decision_resolutions
                (decision_id, resolution_type, alternative_index, recommendation, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    decision_id,
                    resolution_type,
                    alternative_index,
                    recommendation,
                    note,
                    now,
                ),
            )
            connection.execute(
                "UPDATE decisions SET state = 'accepted', updated_at = ? WHERE id = ?",
                (now, decision_id),
            )
            event_note = (
                f"Selected alternative {alternative_index}"
                if alternative_index is not None
                else "Recorded a custom human decision"
            )
            if note:
                event_note = f"{event_note}: {note}"
            connection.execute(
                """INSERT INTO decision_events (decision_id, state, note, created_at)
                VALUES (?, 'accepted', ?, ?)""",
                (decision_id, event_note, now),
            )
            if current["supersedes_id"]:
                previous_id = current["supersedes_id"]
                connection.execute(
                    "UPDATE decisions SET state = 'superseded', updated_at = ? WHERE id = ?",
                    (now, previous_id),
                )
                connection.execute(
                    """INSERT INTO decision_events (decision_id, state, note, created_at)
                    VALUES (?, 'superseded', ?, ?)""",
                    (previous_id, f"Superseded by accepted decision {decision_id}", now),
                )
        return {
            "decision_id": decision_id,
            "resolution_type": resolution_type,
            "alternative_index": alternative_index,
            "recommendation": recommendation,
            "note": note,
        }

    def list_runs(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id, command, question, workspace, status, agent_id, workflow_id,
                result_kind, created_at
                FROM runs ORDER BY created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def list_runs_page(
        self,
        *,
        limit: int,
        before_created_at: str | None = None,
        before_id: str | None = None,
        agent_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        if limit < 1:
            raise ValueError("limit must be positive")
        clauses: list[str] = []
        parameters: list[Any] = []
        if agent_id is not None:
            clauses.append("agent_id = ?")
            parameters.append(agent_id)
        if before_created_at is not None or before_id is not None:
            if before_created_at is None or before_id is None:
                raise ValueError("run cursor requires both created_at and id")
            clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
            parameters.extend((before_created_at, before_created_at, before_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit + 1)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT id, question, workspace, status, agent_id, workflow_id,
                result_kind, created_at, updated_at
                FROM runs {where}
                ORDER BY created_at DESC, id DESC LIMIT ?""",
                parameters,
            ).fetchall()
        values = [dict(row) for row in rows]
        return values[:limit], len(values) > limit

    def get_public_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            run = connection.execute(
                """SELECT id, question, workspace, status, agent_id, workflow_id,
                result_kind, result_json, created_at, updated_at
                FROM runs WHERE id = ?""",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            participants = connection.execute(
                """SELECT participant_id, status, version, model, reason
                FROM run_participants WHERE run_id = ? ORDER BY participant_id""",
                (run_id,),
            ).fetchall()
            decision = connection.execute(
                "SELECT id FROM decisions WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        result = dict(run)
        serialized_result = result.pop("result_json")
        result["result"] = json.loads(serialized_result) if serialized_result is not None else None
        result["participants"] = [dict(row) for row in participants]
        result["decision_id"] = decision["id"] if decision is not None else None
        return result

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            run = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            events = connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
            calls = connection.execute(
                """SELECT participant_id, phase, status, duration_seconds, model,
                input_tokens, output_tokens, cached_input_tokens, total_tokens, cost_usd,
                parsed_json, error, created_at FROM calls WHERE run_id = ? ORDER BY created_at""",
                (run_id,),
            ).fetchall()
        result = dict(run)
        result["result"] = (
            json.loads(result["result_json"]) if result.get("result_json") is not None else None
        )
        result["events"] = [dict(row) for row in events]
        result["calls"] = [dict(row) for row in calls]
        return result

    def get_run_events(
        self,
        run_id: str,
        *,
        after_event_id: int = 0,
        limit: int | None = None,
    ) -> list[WorkEvent]:
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive")
        with self.connect() as connection:
            run = connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            query = """SELECT id, run_id, event_type, agent_id, workflow_id, stage,
            participant_id, payload_json, created_at
            FROM events WHERE run_id = ? AND id > ? ORDER BY id"""
            parameters: list[Any] = [run_id, after_event_id]
            if limit is not None:
                query = f"{query} LIMIT ?"
                parameters.append(limit)
            rows = connection.execute(query, parameters).fetchall()
        events: list[WorkEvent] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            stage_value = row["stage"] or payload.get("stage", payload.get("phase"))
            events.append(
                WorkEvent(
                    event_id=row["id"],
                    run_id=row["run_id"],
                    event_type=row["event_type"],
                    agent_id=row["agent_id"],
                    workflow_id=row["workflow_id"],
                    participant_id=row["participant_id"],
                    phase=(
                        _work_stage(str(stage_value), str(row["agent_id"]))
                        if stage_value is not None
                        else None
                    ),
                    stage=str(stage_value) if stage_value is not None else None,
                    payload=payload,
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
            )
        return events

    def list_decisions(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT d.id, d.run_id, d.state, d.created_at, r.question
                FROM decisions d JOIN runs r ON r.id = d.run_id ORDER BY d.created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            decision = connection.execute(
                "SELECT * FROM decisions WHERE id = ?", (decision_id,)
            ).fetchone()
            if decision is None:
                raise KeyError(decision_id)
            events = connection.execute(
                """SELECT state, note, created_at FROM decision_events
                WHERE decision_id = ? ORDER BY id""",
                (decision_id,),
            ).fetchall()
            resolutions = connection.execute(
                """SELECT resolution_type, alternative_index, recommendation, note, created_at
                FROM decision_resolutions WHERE decision_id = ? ORDER BY id""",
                (decision_id,),
            ).fetchall()
        result = dict(decision)
        result["record"] = json.loads(result.pop("record_json"))
        result["events"] = [dict(row) for row in events]
        result["resolutions"] = [dict(row) for row in resolutions]
        return result

    def get_accepted_decision_package(
        self,
        decision_id: str,
        *,
        workspace: Path,
    ) -> AcceptedDecisionPackage:
        with self.connect() as connection:
            decision = connection.execute(
                """SELECT d.state, d.record_json, r.question, r.workspace
                FROM decisions d JOIN runs r ON r.id = d.run_id
                WHERE d.id = ?""",
                (decision_id,),
            ).fetchone()
            if decision is None:
                raise KeyError(decision_id)
            if decision["state"] != "accepted":
                raise ValueError(f"decision {decision_id} is not accepted")
            if Path(decision["workspace"]) != workspace:
                raise ValueError(f"decision {decision_id} belongs to a different workspace")
            accepted_event = connection.execute(
                """SELECT note, created_at FROM decision_events
                WHERE decision_id = ? AND state = 'accepted'
                ORDER BY id DESC LIMIT 1""",
                (decision_id,),
            ).fetchone()
            resolution = connection.execute(
                """SELECT resolution_type, recommendation, note
                FROM decision_resolutions WHERE decision_id = ?
                ORDER BY id DESC LIMIT 1""",
                (decision_id,),
            ).fetchone()
        if accepted_event is None:
            raise ValueError(f"decision {decision_id} has no accepted event")
        record = FinalDecision.model_validate_json(decision["record_json"])
        conclusion_source: Literal["recommendation", "alternative", "custom"]
        if resolution is None:
            conclusion = record.recommendation
            conclusion_source = "recommendation"
            human_note = accepted_event["note"]
        else:
            conclusion = resolution["recommendation"]
            if resolution["resolution_type"] == "alternative":
                conclusion_source = "alternative"
            elif resolution["resolution_type"] == "custom":
                conclusion_source = "custom"
            else:
                raise ValueError(f"decision {decision_id} has an invalid resolution type")
            human_note = resolution["note"]
        return AcceptedDecisionPackage(
            decision_id=decision_id,
            question=decision["question"],
            workspace=workspace,
            conclusion=conclusion,
            conclusion_source=conclusion_source,
            rationale=record.supporting_arguments,
            constraints=record.constraints,
            non_goals=record.non_goals,
            alternatives=record.alternatives,
            disagreements=record.disagreements,
            assumptions=record.assumptions,
            risks=record.risks,
            evidence=record.evidence,
            human_note=human_note,
            accepted_at=accepted_event["created_at"],
        )

    def create_plan(self, plan: ImplementationPlan) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO plans
                (id, run_id, state, format, artifact_path, manifest_sha256,
                 plan_sha256, plan_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan.plan_id,
                    plan.run_id,
                    plan.state.value,
                    plan.format.value,
                    str(plan.artifact_path),
                    plan.manifest_sha256,
                    plan.plan_sha256,
                    plan.model_dump_json(),
                    now,
                    now,
                ),
            )
            connection.executemany(
                """INSERT INTO plan_decisions (plan_id, decision_id, ordinal)
                VALUES (?, ?, ?)""",
                [
                    (plan.plan_id, decision_id, ordinal)
                    for ordinal, decision_id in enumerate(plan.decision_ids)
                ],
            )
            connection.execute(
                """INSERT INTO plan_events (plan_id, state, note, created_at)
                VALUES (?, ?, NULL, ?)""",
                (plan.plan_id, plan.state.value, now),
            )
            event = self._event(
                connection,
                plan.run_id,
                WorkEventType.PLAN_CREATED,
                {
                    "plan_id": plan.plan_id,
                    "state": plan.state.value,
                    "format": plan.format.value,
                    "artifact_path": str(plan.artifact_path),
                },
            )
        self._publish(event)

    def transition_plan(
        self,
        plan_id: str,
        state: PlanState,
        note: str | None,
        *,
        manifest_sha256: str,
    ) -> None:
        if state not in {PlanState.APPROVED, PlanState.REJECTED, PlanState.SUPERSEDED}:
            raise ValueError(f"unsupported plan transition: {state.value}")
        with self.connect() as connection:
            current = connection.execute(
                "SELECT run_id, state, plan_json FROM plans WHERE id = ?",
                (plan_id,),
            ).fetchone()
            if current is None:
                raise KeyError(plan_id)
            if current["state"] != PlanState.DRAFT.value:
                raise ValueError(f"plan is already {current['state']}")
            plan = ImplementationPlan.model_validate_json(current["plan_json"])
            if state is PlanState.APPROVED and plan.blocking_issues:
                raise ValueError("plan has unresolved blocking issues")
            now = utc_now()
            connection.execute(
                """UPDATE plans SET state = ?, manifest_sha256 = ?, updated_at = ?
                WHERE id = ?""",
                (state.value, manifest_sha256, now, plan_id),
            )
            connection.execute(
                """INSERT INTO plan_events (plan_id, state, note, created_at)
                VALUES (?, ?, ?, ?)""",
                (plan_id, state.value, note, now),
            )
            event = self._event(
                connection,
                current["run_id"],
                WorkEventType.PLAN_STATE_CHANGED,
                {"plan_id": plan_id, "state": state.value, "note": note},
            )
        self._publish(event)

    def list_plans(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT p.id, p.run_id, p.state, p.format, p.artifact_path,
                p.created_at, r.question
                FROM plans p JOIN runs r ON r.id = p.run_id
                ORDER BY p.created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            plan = connection.execute(
                "SELECT * FROM plans WHERE id = ?",
                (plan_id,),
            ).fetchone()
            if plan is None:
                raise KeyError(plan_id)
            decisions = connection.execute(
                """SELECT decision_id FROM plan_decisions
                WHERE plan_id = ? ORDER BY ordinal""",
                (plan_id,),
            ).fetchall()
            events = connection.execute(
                """SELECT state, note, created_at FROM plan_events
                WHERE plan_id = ? ORDER BY id""",
                (plan_id,),
            ).fetchall()
        result = dict(plan)
        result["plan"] = json.loads(result.pop("plan_json"))
        result["decision_ids"] = [row["decision_id"] for row in decisions]
        result["events"] = [dict(row) for row in events]
        return result

    def cleanup_raw(self, retention_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        removed = 0
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, raw_path, created_at FROM calls WHERE raw_path IS NOT NULL"
            ).fetchall()
            for row in rows:
                if datetime.fromisoformat(row["created_at"]) >= cutoff:
                    continue
                path = Path(row["raw_path"])
                path.unlink(missing_ok=True)
                connection.execute("UPDATE calls SET raw_path = NULL WHERE id = ?", (row["id"],))
                removed += 1
        return removed
