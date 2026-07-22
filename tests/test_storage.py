from pathlib import Path

from ego.config import AppPaths
from ego.events import DeliberationEventType
from ego.models import (
    Confidence,
    FinalDecision,
    ParticipantTurnResult,
    Phase,
    Position,
    RunStatus,
    UsageMetrics,
)
from ego.storage import Database


def final(run_id: str) -> FinalDecision:
    return FinalDecision(
        run_id=run_id,
        status=RunStatus.COMPLETED,
        recommendation="Keep the boundary.",
        confidence=Confidence.MODERATE,
        confidence_reason="Supported by evidence.",
    )


def contested_final(run_id: str) -> FinalDecision:
    return FinalDecision(
        run_id=run_id,
        status=RunStatus.CONTESTED,
        recommendation="The models disagree.",
        alternatives=["Choose the strict boundary.", "Keep the current boundary."],
        confidence=Confidence.LOW,
        confidence_reason="Material disagreement remains.",
    )


def make_run(database: Database, workspace: Path) -> str:
    return database.create_run(command="ask", question="Question?", workspace=workspace)


def test_database_persists_run_decision_and_state_events(
    database: Database, tmp_path: Path
) -> None:
    run_id = make_run(database, tmp_path)
    result = final(run_id)
    database.set_run_status(run_id, RunStatus.COMPLETED, final=result)
    decision_id = database.create_decision(result)
    database.transition_decision(decision_id, "deferred", "Need more context")
    database.transition_decision(decision_id, "accepted", "Context arrived")

    decision = database.get_decision(decision_id)
    assert decision["state"] == "accepted"
    assert [item["state"] for item in decision["events"]] == [
        "recommended",
        "deferred",
        "accepted",
    ]
    assert database.get_run(run_id)["status"] == "completed"


def test_accepting_reconsideration_supersedes_previous(database: Database, tmp_path: Path) -> None:
    first_run = make_run(database, tmp_path)
    first_id = database.create_decision(final(first_run))
    second_run = make_run(database, tmp_path)
    second_id = database.create_decision(final(second_run), supersedes_id=first_id)

    assert database.get_decision(first_id)["state"] == "recommended"
    database.transition_decision(second_id, "accepted", None)
    assert database.get_decision(first_id)["state"] == "superseded"


def test_run_events_can_be_read_incrementally(database: Database, tmp_path: Path) -> None:
    run_id = make_run(database, tmp_path)
    initial = database.get_run_events(run_id)
    database.set_run_status(run_id, RunStatus.RUNNING)

    recent = database.get_run_events(run_id, after_event_id=initial[-1].event_id)

    assert [item.event_type for item in initial] == [DeliberationEventType.RUN_CREATED]
    assert [item.event_type for item in recent] == [
        DeliberationEventType.RUN_STATUS_CHANGED
    ]
    assert recent[0].payload["status"] == RunStatus.RUNNING.value


def test_call_usage_is_persisted_and_published(database: Database, tmp_path: Path) -> None:
    run_id = make_run(database, tmp_path)
    position = Position(
        recommendation="Keep the boundary.",
        confidence=Confidence.MODERATE,
        confidence_reason="The current evidence supports the boundary.",
    )
    database.record_call(
        run_id,
        ParticipantTurnResult(
            participant_id="claude",
            phase=Phase.INDEPENDENT,
            payload=position,
            raw_output=position.model_dump_json(),
            duration_seconds=1.0,
            usage=UsageMetrics(
                input_tokens=1_000,
                output_tokens=200,
                cached_input_tokens=800,
                total_tokens=1_200,
                cost_usd=0.12,
            ),
        ),
        participant_id="claude",
        phase=Phase.INDEPENDENT.value,
    )

    call = database.get_run(run_id)["calls"][0]
    completed = database.get_run_events(run_id)[-1]
    assert call["total_tokens"] == 1_200
    assert call["cost_usd"] == 0.12
    assert completed.payload["usage"]["cached_input_tokens"] == 800


def test_contested_decision_requires_a_structured_human_resolution(
    database: Database, tmp_path: Path
) -> None:
    run_id = make_run(database, tmp_path)
    result = contested_final(run_id)
    database.set_run_status(run_id, RunStatus.CONTESTED, final=result)
    decision_id = database.create_decision(result)

    try:
        database.transition_decision(decision_id, "accepted", None)
    except ValueError as error:
        assert "require selecting an alternative" in str(error)
    else:
        raise AssertionError("contested result should not be accepted without a resolution")

    resolution = database.resolve_decision(decision_id, alternative_index=2, note="Safer fit")
    decision = database.get_decision(decision_id)

    assert resolution["recommendation"] == "Keep the current boundary."
    assert decision["state"] == "accepted"
    assert decision["resolutions"] == [
        {
            "resolution_type": "alternative",
            "alternative_index": 2,
            "recommendation": "Keep the current boundary.",
            "note": "Safer fit",
            "created_at": decision["resolutions"][0]["created_at"],
        }
    ]
    assert decision["events"][-1]["state"] == "accepted"
    assert "Selected alternative 2" in decision["events"][-1]["note"]


def test_contested_decision_can_record_a_custom_human_conclusion(
    database: Database, tmp_path: Path
) -> None:
    run_id = make_run(database, tmp_path)
    result = contested_final(run_id)
    decision_id = database.create_decision(result)

    resolution = database.resolve_decision(
        decision_id, custom_text="Adopt the strict boundary after a compatibility test."
    )

    assert resolution["resolution_type"] == "custom"
    assert database.get_decision(decision_id)["state"] == "accepted"


def test_schema_one_database_is_migrated_for_human_resolutions(
    database: Database, app_paths: AppPaths
) -> None:
    with database.connect() as connection:
        connection.execute("DROP TABLE decision_resolutions")
        connection.execute("DROP TABLE calls")
        connection.executescript(
            """
            CREATE TABLE calls (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                participant_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_seconds REAL,
                model TEXT,
                raw_path TEXT,
                parsed_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX calls_run_id_idx ON calls(run_id, created_at);
            """
        )
        connection.execute("PRAGMA user_version = 1")

    migrated = Database(app_paths)
    with migrated.connect() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("decision_resolutions",),
        ).fetchone()
        call_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(calls)").fetchall()
        }

    assert version == 3
    assert table is not None
    assert {"input_tokens", "output_tokens", "total_tokens", "cost_usd"} <= call_columns
