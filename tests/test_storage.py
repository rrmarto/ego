from pathlib import Path

from ego.events import DeliberationEventType
from ego.models import Confidence, FinalDecision, RunStatus
from ego.storage import Database


def final(run_id: str) -> FinalDecision:
    return FinalDecision(
        run_id=run_id,
        status=RunStatus.COMPLETED,
        recommendation="Keep the boundary.",
        confidence=Confidence.MODERATE,
        confidence_reason="Supported by evidence.",
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
