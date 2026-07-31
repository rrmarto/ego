from __future__ import annotations

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
