from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ego.models import HumanPlanBrief, PlanDraft, PlanTask
from ego.planning.context import WorkspaceContextBuilder
from ego.planning.context_enrichment import (
    MAX_ENRICHMENT_BYTES,
    stale_workspace_evidence_ids,
)


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


@pytest.mark.asyncio
async def test_workspace_context_adaptively_recovers_distant_symbols_and_consumers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "ego"
    source.mkdir(parents=True)
    filler_before = "\n".join(f"before_{index} = {index}" for index in range(210))
    filler_between = "\n".join(f"between_{index} = {index}" for index in range(170))
    (source / "models.py").write_text(
        f"{filler_before}\n"
        "class WorkspaceContextManifest:\n"
        "    context_id: str\n"
        f"{filler_between}\n"
        "class ImplementationPlan:\n"
        "    workspace_context_manifest: WorkspaceContextManifest | None = None\n",
        encoding="utf-8",
    )
    (source / "cli.py").write_text(
        "def inspect_run(run_id: str):\n"
        "    return run_id\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_plan_compat.py").write_text(
        "def test_legacy_implementation_plan():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    await _track_workspace(tmp_path)
    builder = WorkspaceContextBuilder()
    initial = await builder.build(
        workspace=tmp_path,
        question="Expose WorkspaceContextManifest in ego inspect.",
        sources=[_source("Show the safe WorkspaceContext summary.")],
        git_head=None,
        git_status=None,
    )

    assert "WorkspaceContextManifest" in next(
        item.content for item in initial.evidence if item.path == "src/ego/models.py"
    )
    assert all("ImplementationPlan" not in item.content for item in initial.evidence)

    enriched = await builder.enrich(
        workspace=tmp_path,
        context=initial,
        candidates={
            "codex": PlanDraft(
                title="Expose WorkspaceContextManifest",
                objective="Read it from ImplementationPlan.",
                affected_areas=["src/ego/models.py"],
                open_questions=[
                    "Does ImplementationPlan persist workspace_context_manifest?"
                ],
                tasks=[
                    PlanTask(
                        id="compat",
                        title="Preserve ImplementationPlan compatibility",
                        description="Use workspace_context_manifest when present.",
                        affected_paths=["src/ego/models.py"],
                        acceptance_criteria=[
                            "test_legacy_implementation_plan covers old records."
                        ],
                    )
                ],
            )
        },
    )

    adaptive = [
        item
        for item in enriched.evidence
        if item.id in enriched.manifest.enrichment_evidence_ids
    ]
    assert enriched.manifest.initial_context_id == initial.manifest.context_id
    assert enriched.manifest.context_id != initial.manifest.context_id
    assert any(
        item.path == "src/ego/models.py" and "ImplementationPlan" in item.content
        for item in adaptive
    )
    assert any(
        item.path == "tests/test_plan_compat.py"
        and "test_legacy_implementation_plan" in item.content
        for item in adaptive
    )
    assert (
        enriched.manifest.bytes_used - initial.manifest.bytes_used
        <= MAX_ENRICHMENT_BYTES
    )
    assert (
        enriched.manifest.enrichment_bytes_used
        <= enriched.manifest.enrichment_byte_budget
    )
    assert enriched.manifest.bytes_used <= enriched.manifest.byte_budget

    models_reference_ids = [
        item.id
        for item in enriched.manifest.evidence
        if item.path == "src/ego/models.py"
    ]
    (source / "models.py").write_text(
        "# changed after collaborative context was frozen\n",
        encoding="utf-8",
    )

    stale_ids = await stale_workspace_evidence_ids(tmp_path, enriched.manifest)

    assert set(models_reference_ids).issubset(stale_ids)


@pytest.mark.asyncio
async def test_adaptive_context_reserves_author_gap_coverage(tmp_path: Path) -> None:
    source = tmp_path / "src" / "ego"
    source.mkdir(parents=True)
    filler = "\n".join(f"filler_{index} = {index}" for index in range(220))
    (source / "cli.py").write_text(
        "def render_plan(plan):\n"
        "    return plan\n"
        f"{filler}\n"
        "def inspect_run(run_id):\n"
        "    return run_id\n",
        encoding="utf-8",
    )
    await _track_workspace(tmp_path)
    builder = WorkspaceContextBuilder()
    initial = await builder.build(
        workspace=tmp_path,
        question="Expose a summary in ego inspect.",
        sources=[_source("Update ego inspect safely.")],
        git_head=None,
        git_status=None,
    )
    assert all("def render_plan" not in item.content for item in initial.evidence)

    def candidate(title: str, questions: list[str]) -> PlanDraft:
        return PlanDraft(
            title=title,
            objective="Update inspect without guessing missing workspace contracts.",
            affected_areas=["src/ego/cli.py"],
            open_questions=questions,
            tasks=[
                PlanTask(
                    id="inspect",
                    title="Update inspect",
                    description="Preserve the existing CLI contract.",
                    affected_paths=["src/ego/cli.py"],
                )
            ],
        )

    enriched = await builder.enrich(
        workspace=tmp_path,
        context=initial,
        candidates={
            "claude": candidate(
                "Locate rendering",
                [
                    "Where is `render_plan` defined?",
                    "Does MissingPresentationContract exist?",
                    "Is HistoricalInspectSchema persisted?",
                ],
            ),
            "codex": candidate(
                "Preserve expert mode",
                [
                    "How does TransparencyMode.EXPERT behave?",
                    "Does ExpertInspectPayload change?",
                    "Is LegacyInspectOutput stable?",
                ],
            ),
            "opencode": candidate(
                "Preserve storage",
                [
                    "Does ImplementationPlan own the manifest?",
                    "Is WorkspaceContextManifest optional?",
                    "Does workspace_context_manifest default safely?",
                ],
            ),
        },
    )

    adaptive = [
        item
        for item in enriched.evidence
        if item.id in enriched.manifest.enrichment_evidence_ids
    ]
    assert "render_plan" in enriched.manifest.enrichment_required_anchors
    assert "render_plan" not in enriched.manifest.enrichment_unresolved_anchors
    assert any("def render_plan" in item.content for item in adaptive)
