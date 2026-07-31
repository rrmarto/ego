from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ego.models import HumanPlanBrief
from ego.planning.context import WorkspaceContextBuilder


def _source(instruction: str) -> HumanPlanBrief:
    return HumanPlanBrief(
        source_kind="text",
        brief_id="brief-1",
        instruction=instruction,
        created_at="2026-07-31T00:00:00+00:00",
    )


def _workspace_paths(workspace: Path) -> set[Path]:
    return {
        path.relative_to(workspace)
        for path in workspace.rglob("*")
    }


async def _track_workspace(workspace: Path) -> None:
    for arguments in (("init", "-q"), ("add", ".")):
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/git",
            "-C",
            str(workspace),
            *arguments,
        )
        assert await process.wait() == 0


@pytest.mark.asyncio
async def test_workspace_context_is_bounded_in_memory_and_includes_required_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "Read `docs/architecture.md`; write artifacts only in `.ego/plans/`.\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "architecture.md").write_text(
        "# Architecture\n\nPlan uses a deterministic context builder.\n",
        encoding="utf-8",
    )
    source_dir = tmp_path / "src"
    source_dir.mkdir()
    (source_dir / "workspace_context.py").write_text(
        "class WorkspaceContextBuilder:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("SECRET=do-not-read\n", encoding="utf-8")
    generated = tmp_path / ".ego" / "plans" / "old"
    generated.mkdir(parents=True)
    (generated / "plan.md").write_text("old plan\n", encoding="utf-8")
    paths_before = _workspace_paths(tmp_path)

    context = await WorkspaceContextBuilder().build(
        workspace=tmp_path,
        question="Add the WorkspaceContext builder.",
        sources=[_source("Create bounded workspace context.")],
        git_head=None,
        git_status=None,
    )

    evidence_paths = {item.path for item in context.evidence}
    assert context.manifest.sufficient
    assert context.manifest.bytes_used <= context.manifest.byte_budget
    assert "AGENTS.md" in evidence_paths
    assert "docs/architecture.md" in evidence_paths
    assert "src/workspace_context.py" in evidence_paths
    assert ".env" not in evidence_paths
    assert not any(path.startswith(".ego/") for path in evidence_paths)
    assert _workspace_paths(tmp_path) == paths_before


@pytest.mark.asyncio
async def test_workspace_context_falls_back_when_mandatory_context_does_not_fit(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "Read `docs/architecture.md` before changing the harness.\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "architecture.md").write_text("required architecture\n", encoding="utf-8")
    (tmp_path / "workspace_context.py").write_text(
        "workspace context implementation\n",
        encoding="utf-8",
    )

    context = await WorkspaceContextBuilder(byte_budget=20).build(
        workspace=tmp_path,
        question="Change workspace context.",
        sources=[_source("Change workspace context.")],
        git_head=None,
        git_status=None,
    )

    assert not context.manifest.sufficient
    assert context.manifest.truncated
    assert context.manifest.fallback_reason == "mandatory workspace instructions did not fit"
    assert context.manifest.omitted_paths


@pytest.mark.asyncio
async def test_workspace_context_selects_symbols_and_best_matching_windows(
    tmp_path: Path,
) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "Read `docs/architecture.md` and ADRs in `docs/decisions/`.\n",
        encoding="utf-8",
    )
    decisions = tmp_path / "docs" / "decisions"
    decisions.mkdir(parents=True)
    (tmp_path / "docs" / "architecture.md").write_text(
        "WorkspaceContext is ephemeral.\n",
        encoding="utf-8",
    )
    for index in range(16):
        (decisions / f"{index:04d}.md").write_text(
            f"ADR {index}: inspect planning behavior.\n",
            encoding="utf-8",
        )
    source = tmp_path / "src" / "ego"
    planning = source / "planning"
    planning.mkdir(parents=True)
    filler = "\n".join(f"filler_{index} = {index}" for index in range(220))
    (source / "models.py").write_text(
        f'CREATED = "created"\n{filler}\n'
        "class WorkspaceContextManifest:\n"
        "    context_id: str\n"
        "    bytes_used: int\n"
        "    byte_budget: int\n"
        "    fallback_reason: str | None\n",
        encoding="utf-8",
    )
    (source / "cli.py").write_text(
        f'CREATED = "created"\n{filler}\n'
        "def inspect_run(run_id: str):\n"
        "    return run_id\n",
        encoding="utf-8",
    )
    (planning / "context.py").write_text(
        "class WorkspaceContextBuilder:\n"
        "    def build(self): ...\n",
        encoding="utf-8",
    )
    await _track_workspace(tmp_path)

    context = await WorkspaceContextBuilder().build(
        workspace=tmp_path,
        question="Create an implementation plan from the direct instruction.",
        sources=[
            _source(
                "Expón en ego inspect RUN_ID un resumen seguro de WorkspaceContext: "
                "context_id, bytes_used/byte_budget y fallback_reason."
            )
        ],
        git_head=None,
        git_status=None,
    )

    by_path = {item.path: item for item in context.evidence}
    assert context.manifest.sufficient
    assert "WorkspaceContextManifest" in by_path["src/ego/models.py"].content
    assert "inspect_run" in by_path["src/ego/cli.py"].content
    assert "WorkspaceContextBuilder" in by_path["src/ego/planning/context.py"].content
    assert len(
        [
            item
            for item in context.evidence
            if item.path.startswith("docs/decisions/")
        ]
    ) <= 3


@pytest.mark.asyncio
async def test_workspace_context_requires_query_anchor_coverage(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "feature.py").write_text("unrelated = True\n", encoding="utf-8")
    await _track_workspace(tmp_path)

    context = await WorkspaceContextBuilder().build(
        workspace=tmp_path,
        question="Create an implementation plan.",
        sources=[_source("Add NewLedgerAPI with ledger_id support.")],
        git_head=None,
        git_status=None,
    )

    assert not context.manifest.sufficient
    assert context.manifest.fallback_reason is not None
    assert "query anchors" in context.manifest.fallback_reason
