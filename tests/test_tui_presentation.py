from ego.models import Confidence, FinalDecision, Phase, RunStatus
from ego.tui.presentation import (
    final_markdown,
    participant_texts,
    protocol_text,
    session_strip,
    session_summary,
    welcome_status,
)
from ego.tui.state import ParticipantState, SessionState


def test_session_presentation_uses_only_session_state() -> None:
    session = SessionState(
        run_id="12345678-abcdef",
        status="running",
        phase=Phase.PEER_REVIEW,
        completed_phases=1,
    )

    assert session_summary(session, mode="discussion", elapsed=65) == (
        "Run: 12345678\nStatus: running\nMode: discussion\nElapsed: 01:05"
    )
    wide_strip = session_strip(session, mode="discussion", width=120, version="0.1.0")
    narrow_strip = session_strip(session, mode="discussion", width=80, version="0.1.0")
    assert "RUN  12345678" in wide_strip.plain
    assert "MODE  discussion" in wide_strip.plain
    assert "RUN  12345678" not in narrow_strip.plain
    assert narrow_strip.plain.endswith("STATUS  RUNNING")


def test_participant_and_protocol_presentation_preserves_statuses() -> None:
    participants = {
        "codex": ParticipantState(status="completed", detail="Completed in 4.2s"),
        "gemini": ParticipantState(status="unavailable", detail="Binary not found"),
    }
    session = SessionState(
        status="running",
        phase=Phase.PEER_REVIEW,
        completed_phases=1,
        participants=participants,
    )

    active, welcome = participant_texts(participants)
    protocol = protocol_text(session, running=True)

    assert "CODEX\n  Completed in 4.2s" in active.plain
    assert "GEMINI  ·  unavailable" in welcome.plain
    assert "✓ Independent reasoning" in protocol.plain
    assert "◆ Peer review" in protocol.plain
    assert welcome_status(participants) == "Checks complete · 0/2 participants available"


def test_final_markdown_respects_transparency_mode() -> None:
    final = FinalDecision(
        run_id="run-1",
        status=RunStatus.COMPLETED,
        recommendation="Keep the event boundary.",
        supporting_arguments=["Events remain auditable."],
        disagreements=["One participant preferred polling."],
        confidence=Confidence.MODERATE,
        confidence_reason="Evidence is sufficient.",
    )

    standard = final_markdown(final, "decision-1", mode="standard")
    discussion = final_markdown(final, "decision-1", mode="discussion")

    assert "Keep the event boundary." in standard
    assert "Supporting reasoning" not in standard
    assert "### Supporting reasoning" in discussion
    assert "### Disagreements" in discussion
    assert discussion.endswith("_Decision record: decision-1_")
