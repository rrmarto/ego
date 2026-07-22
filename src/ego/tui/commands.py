from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ego.models import FinalDecision, ParticipantAvailability


@dataclass(frozen=True)
class CommandSpec:
    name: str
    usage: str
    description: str


COMMANDS = (
    CommandSpec("/help", "/help", "Show interactive commands"),
    CommandSpec("/ask", "/ask <question>", "Ask every participant"),
    CommandSpec(
        "/summon",
        "/summon codex claude -- <question>",
        "Ask selected participants",
    ),
    CommandSpec("/cd", "/cd <path>", "Change the session workspace"),
    CommandSpec("/pwd", "/pwd", "Show the current workspace"),
    CommandSpec(
        "/mode",
        "/mode standard|discussion|expert",
        "Set the transparency level",
    ),
    CommandSpec("/doctor", "/doctor", "Diagnose local participants"),
    CommandSpec("/participants", "/participants", "Alias for /doctor"),
    CommandSpec("/runs", "/runs", "List previous runs"),
    CommandSpec("/inspect", "/inspect <run-id>", "Inspect a run"),
    CommandSpec("/decisions", "/decisions", "List decision records"),
    CommandSpec("/show", "/show <decision-id>", "Show a decision record"),
    CommandSpec(
        "/reconsider",
        "/reconsider <decision-id> -- <new context>",
        "Revisit a decision with new context",
    ),
    CommandSpec("/choose", "/choose <number> [note]", "Accept a contested alternative"),
    CommandSpec("/decide", "/decide <text>", "Record your own conclusion"),
    CommandSpec("/accept", "/accept [note]", "Accept the current recommendation"),
    CommandSpec("/defer", "/defer [note]", "Defer the current decision"),
    CommandSpec("/reject", "/reject [note]", "Reject the current recommendation"),
    CommandSpec("/quit", "/quit", "Exit Ego"),
    CommandSpec("/exit", "/exit", "Exit Ego"),
)
COMMAND_BY_NAME = {command.name: command for command in COMMANDS}


def help_text() -> str:
    lines = ["Interactive commands:", f"  {'<question>':<43} Ask every available participant"]
    for command in COMMANDS:
        lines.append(f"  {command.usage:<43} {command.description}")
    return "\n".join(lines)


def participant_checks_text(
    participant_ids: Sequence[str],
    results: Sequence[ParticipantAvailability | BaseException],
) -> str:
    lines = ["Participant checks:"]
    for name, result in zip(participant_ids, results, strict=True):
        if isinstance(result, BaseException):
            lines.extend((f"  {name.upper()}  unknown", f"      {result}"))
            continue
        version = f" ({result.version})" if result.version else ""
        lines.append(f"  {result.participant_id.upper()}  {result.status.value}{version}")
        if result.reason:
            lines.append(f"      {result.reason}")
    return "\n".join(lines)


def runs_text(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "Runs:\n  No persisted runs."
    lines = ["Runs:"]
    for row in rows:
        lines.append(f"  {row['id']}  {row['status']}")
        lines.append(f"      {row['question']}")
    return "\n".join(lines)


def decisions_text(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "Decisions:\n  No decision records."
    lines = ["Decisions:"]
    for row in rows:
        lines.append(f"  {row['id']}  {row['state']}")
        lines.append(f"      {row['question']}")
    return "\n".join(lines)


def run_text(row: Mapping[str, Any]) -> str:
    return "Run details:\n" + json.dumps(row, ensure_ascii=False, indent=2, default=str)


def decision_text(row: Mapping[str, Any]) -> str:
    final = FinalDecision.model_validate(row["record"])
    lines = [
        f"Decision: {row['id']}",
        f"State: {row['state']}",
        f"Run: {final.run_id}",
        f"Status: {final.status.value}",
        f"Confidence: {final.confidence.value} — {final.confidence_reason}",
        "",
        "Recommendation:",
        final.recommendation,
    ]
    for heading, values in (
        ("Supporting reasoning", final.supporting_arguments),
        ("Alternatives", final.alternatives),
        ("Disagreements", final.disagreements),
        ("Assumptions", final.assumptions),
        ("Risks", final.risks),
    ):
        if values:
            lines.extend(("", f"{heading}:", *(f"  - {value}" for value in values)))
    if row["resolutions"]:
        lines.extend(("", "Human resolution:"))
        for resolution in row["resolutions"]:
            source = (
                f"alternative {resolution['alternative_index']}"
                if resolution["resolution_type"] == "alternative"
                else "custom decision"
            )
            lines.append(f"  - {source}: {resolution['recommendation']}")
    lines.extend(("", "State history:"))
    for event in row["events"]:
        suffix = f" — {event['note']}" if event["note"] else ""
        lines.append(f"  - {event['created_at']}: {event['state']}{suffix}")
    return "\n".join(lines)
