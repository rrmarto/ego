from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ego.models import HumanPlanBrief, PlanSource
from ego.storage import Database

MAX_PLAN_BRIEF_CHARS = 12_000
MAX_PLAN_BRIEF_BYTES = 48_000


def resolve_plan_sources(
    database: Database,
    *,
    workspace: Path,
    decision_ids: list[str],
    brief: str | None,
    brief_file: Path | None,
) -> list[PlanSource]:
    if decision_ids:
        unique_ids = list(dict.fromkeys(decision_ids))
        return [
            database.get_accepted_decision_package(
                decision_id,
                workspace=workspace,
            )
            for decision_id in unique_ids
        ]
    if brief is not None:
        return [_human_brief(brief, source_kind="text")]
    if brief_file is None:
        raise ValueError("Plan source is missing")
    canonical_workspace = workspace.resolve()
    candidate = brief_file
    if not candidate.is_absolute():
        candidate = canonical_workspace / candidate
    try:
        source_path = candidate.resolve(strict=True)
        relative_path = source_path.relative_to(canonical_workspace)
    except (FileNotFoundError, ValueError) as error:
        raise ValueError("plan source file must exist inside the workspace") from error
    if not source_path.is_file():
        raise ValueError("plan source file must be a regular file")
    if source_path.stat().st_size > MAX_PLAN_BRIEF_BYTES:
        raise ValueError(
            f"plan source file exceeds the {MAX_PLAN_BRIEF_BYTES}-byte limit"
        )
    try:
        instruction = source_path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as error:
        raise ValueError("plan source file must be UTF-8 text") from error
    if not instruction:
        raise ValueError("plan source file cannot be empty")
    if len(instruction) > MAX_PLAN_BRIEF_CHARS:
        raise ValueError(
            f"plan source file exceeds the {MAX_PLAN_BRIEF_CHARS}-character limit"
        )
    return [
        _human_brief(
            instruction,
            source_kind="file",
            source_path=str(relative_path),
        )
    ]


def _human_brief(
    instruction: str,
    *,
    source_kind: Literal["text", "file"],
    source_path: str | None = None,
) -> HumanPlanBrief:
    return HumanPlanBrief(
        source_kind=source_kind,
        brief_id=str(uuid.uuid4()),
        instruction=instruction,
        source_path=source_path,
        created_at=datetime.now(UTC).isoformat(),
    )
