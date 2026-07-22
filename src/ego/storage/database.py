from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ego.config import AppPaths
from ego.events import DeliberationEvent, DeliberationEventStream, DeliberationEventType
from ego.models import (
    DecisionState,
    FinalDecision,
    JsonObject,
    ParticipantAvailability,
    ParticipantTurnResult,
    Phase,
    RunStatus,
)
from ego.redaction import redact_sensitive_text

SCHEMA_VERSION = 3


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(
        self,
        paths: AppPaths,
        *,
        event_stream: DeliberationEventStream | None = None,
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
                    PRAGMA user_version = 3;
                    """
                )
                version = 3
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

    def create_run(
        self,
        *,
        command: str,
        question: str,
        workspace: Path,
        parent_decision_id: str | None = None,
        git_head: str | None = None,
        git_status: str | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO runs
                (id, command, question, workspace, status, parent_decision_id,
                 git_head_start, git_status_start, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    command,
                    question,
                    str(workspace),
                    RunStatus.CREATED.value,
                    parent_decision_id,
                    git_head,
                    git_status,
                    now,
                    now,
                ),
            )
            event = self._event(
                connection,
                run_id,
                DeliberationEventType.RUN_CREATED,
                {"command": command, "workspace": str(workspace)},
            )
        self._publish(event)
        return run_id

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        run_id: str,
        event_type: DeliberationEventType,
        payload: JsonObject,
        participant_id: str | None = None,
    ) -> DeliberationEvent:
        created_at = datetime.now(UTC)
        cursor = connection.execute(
            """INSERT INTO events
            (run_id, event_type, participant_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                run_id,
                event_type.value,
                participant_id,
                json.dumps(payload),
                created_at.isoformat(),
            ),
        )
        event_id = cursor.lastrowid
        if event_id is None:
            raise RuntimeError("SQLite did not assign an event id")
        phase_value = payload.get("phase")
        phase = Phase(str(phase_value)) if phase_value is not None else None
        return DeliberationEvent(
            event_id=event_id,
            run_id=run_id,
            event_type=event_type,
            participant_id=participant_id,
            phase=phase,
            payload=payload,
            created_at=created_at,
        )

    def _publish(self, event: DeliberationEvent) -> None:
        if self.event_stream is not None:
            self.event_stream.publish(event)

    def add_event(
        self,
        run_id: str,
        event_type: DeliberationEventType,
        payload: JsonObject,
        participant_id: str | None = None,
    ) -> DeliberationEvent:
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
        git_head: str | None = None,
        git_status: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """UPDATE runs SET status = ?, final_json = COALESCE(?, final_json),
                git_head_end = COALESCE(?, git_head_end),
                git_status_end = COALESCE(?, git_status_end), updated_at = ? WHERE id = ?""",
                (
                    status.value,
                    final.model_dump_json() if final else None,
                    git_head,
                    git_status,
                    now,
                    run_id,
                ),
            )
            event = self._event(
                connection,
                run_id,
                DeliberationEventType.RUN_STATUS_CHANGED,
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
    ) -> None:
        call_id = str(uuid.uuid4())
        raw_path: Path | None = None
        if result:
            directory = self.paths.raw_dir / run_id
            directory.mkdir(parents=True, exist_ok=True)
            raw_path = directory / f"{call_id}.txt"
            raw_path.write_text(redact_sensitive_text(result.raw_output), encoding="utf-8")
        usage = result.usage if result else None
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
                    result.duration_seconds if result else None,
                    result.model if result else None,
                    usage.input_tokens if usage else None,
                    usage.output_tokens if usage else None,
                    usage.cached_input_tokens if usage else None,
                    usage.total_tokens if usage else None,
                    usage.cost_usd if usage else None,
                    str(raw_path) if raw_path else None,
                    result.payload.model_dump_json() if result else None,
                    redact_sensitive_text(error) if error else None,
                    utc_now(),
                ),
            )
            event = self._event(
                connection,
                run_id,
                DeliberationEventType.PARTICIPANT_TURN_COMPLETED
                if result
                else DeliberationEventType.PARTICIPANT_TURN_FAILED,
                {
                    "call_id": call_id,
                    "phase": phase,
                    "duration_seconds": result.duration_seconds if result else None,
                    "model": result.model if result else None,
                    "usage": usage.model_dump(mode="json") if usage else None,
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
                DeliberationEventType.DECISION_CREATED,
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
                """SELECT id, command, question, workspace, status, created_at
                FROM runs ORDER BY created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

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
        result["events"] = [dict(row) for row in events]
        result["calls"] = [dict(row) for row in calls]
        return result

    def get_run_events(self, run_id: str, *, after_event_id: int = 0) -> list[DeliberationEvent]:
        with self.connect() as connection:
            run = connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(run_id)
            rows = connection.execute(
                """SELECT id, run_id, event_type, participant_id, payload_json, created_at
                FROM events WHERE run_id = ? AND id > ? ORDER BY id""",
                (run_id, after_event_id),
            ).fetchall()
        events: list[DeliberationEvent] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            phase_value = payload.get("phase")
            events.append(
                DeliberationEvent(
                    event_id=row["id"],
                    run_id=row["run_id"],
                    event_type=row["event_type"],
                    participant_id=row["participant_id"],
                    phase=Phase(str(phase_value)) if phase_value is not None else None,
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
