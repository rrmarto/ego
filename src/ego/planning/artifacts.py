from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ego.models import PlanDraft, PlanSource, PlanVariant, WorkspaceContextManifest

ARTIFACT_FILES = frozenset({"plan.md", "sources.json", "manifest.json"})


class PlanArtifactError(ValueError):
    pass


@dataclass(frozen=True)
class WrittenPlanArtifact:
    path: Path
    manifest_sha256: str
    plan_sha256: str


class PlanArtifactWriter:
    """The only workspace write boundary owned by Plan."""

    def write(
        self,
        *,
        workspace: Path,
        plan_id: str,
        run_id: str,
        draft: PlanDraft,
        sources: list[PlanSource],
        destination: Path | None,
        workspace_git_head: str | None,
        participant_ids: list[str] | None = None,
        variants: list[PlanVariant] | None = None,
        blocking_issues: list[str] | None = None,
        workspace_context_manifest: WorkspaceContextManifest | None = None,
    ) -> WrittenPlanArtifact:
        root = workspace / ".ego" / "plans"
        target = self._target(root, workspace, draft.title, plan_id, destination)
        root = self._prepare_root(workspace)
        temporary = root / f".tmp-{plan_id}-{uuid.uuid4().hex[:8]}"
        if target.exists() or target.is_symlink():
            raise PlanArtifactError(f"plan destination already exists: {target}")
        try:
            temporary.mkdir(mode=0o700)
            sources_path = temporary / "sources.json"
            plan_path = temporary / "plan.md"
            self._write_text(
                sources_path,
                json.dumps(
                    [item.model_dump(mode="json") for item in sources],
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
            self._write_text(
                plan_path,
                render_markdown(
                    draft,
                    sources,
                    variants=variants or [],
                    blocking_issues=blocking_issues or [],
                ),
            )
            file_hashes = {
                "plan.md": _sha256(plan_path.read_bytes()),
                "sources.json": _sha256(sources_path.read_bytes()),
            }
            manifest = {
                "artifact_version": 5,
                "plan_id": plan_id,
                "run_id": run_id,
                "state": "draft",
                "format": "markdown",
                "sources": [_source_reference(item) for item in sources],
                "participants": participant_ids or [],
                "blocking_issues": blocking_issues or [],
                "workspace_context": (
                    workspace_context_manifest.model_dump(mode="json")
                    if workspace_context_manifest is not None
                    else None
                ),
                "workspace": str(workspace),
                "workspace_git_head": workspace_git_head,
                "created_at": datetime.now(UTC).isoformat(),
                "files": file_hashes,
            }
            manifest_path = temporary / "manifest.json"
            self._write_text(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
            if {item.name for item in temporary.iterdir()} != ARTIFACT_FILES:
                raise PlanArtifactError("plan writer produced an unexpected artifact set")
            os.replace(temporary, target)
            return WrittenPlanArtifact(
                path=target,
                manifest_sha256=_sha256((target / "manifest.json").read_bytes()),
                plan_sha256=file_hashes["plan.md"],
            )
        except BaseException:
            if temporary.exists() and temporary.parent == root:
                shutil.rmtree(temporary)
            raise

    def update_state(
        self,
        *,
        workspace: Path,
        artifact_path: Path,
        plan_id: str,
        state: str,
    ) -> str:
        root = self._prepare_root(workspace)
        target = workspace / artifact_path
        if target.parent != root or target.is_symlink() or not target.is_dir():
            raise PlanArtifactError("stored plan artifact is outside .ego/plans")
        manifest_path = target / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise PlanArtifactError("stored plan manifest is missing or unsafe")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("plan_id") != plan_id:
            raise PlanArtifactError("stored plan manifest does not match the plan")
        manifest["state"] = state
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        temporary = target / f".manifest-{uuid.uuid4().hex}.tmp"
        self._write_text(
            temporary,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        os.replace(temporary, manifest_path)
        return _sha256(manifest_path.read_bytes())

    @staticmethod
    def _prepare_root(workspace: Path) -> Path:
        ego_dir = workspace / ".ego"
        root = ego_dir / "plans"
        for directory in (ego_dir, root):
            if directory.is_symlink():
                raise PlanArtifactError(f"plan directory cannot be a symlink: {directory}")
            if directory.exists() and not directory.is_dir():
                raise PlanArtifactError(f"plan directory is not a directory: {directory}")
            directory.mkdir(exist_ok=True)
        return root

    @staticmethod
    def _target(
        root: Path,
        workspace: Path,
        title: str,
        plan_id: str,
        destination: Path | None,
    ) -> Path:
        if destination is None:
            return root / f"{_slug(title)}-{plan_id[:8]}"
        if destination.is_absolute():
            raise PlanArtifactError("plan destination must be relative to the workspace")
        target = workspace / destination
        if target.parent != root:
            raise PlanArtifactError("plan destination must be a direct child of .ego/plans")
        if target.name in {"", ".", ".."}:
            raise PlanArtifactError("plan destination requires a directory name")
        return target

    @staticmethod
    def _write_text(path: Path, value: str) -> None:
        path.write_text(value, encoding="utf-8")


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return (normalized or "implementation-plan")[:60]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def render_markdown(
    draft: PlanDraft,
    sources: list[PlanSource],
    *,
    variants: list[PlanVariant] | None = None,
    blocking_issues: list[str] | None = None,
) -> str:
    lines = [
        f"# {draft.title}",
        "",
        "## Objective",
        "",
        draft.objective,
        "",
        "## Sources",
        "",
    ]
    for item in sources:
        if item.source_kind == "decision":
            lines.append(f"- Decision `{item.decision_id}` — {item.conclusion}")
        elif item.source_kind == "file":
            lines.append(f"- File `{item.source_path}` — brief `{item.brief_id}`")
        else:
            lines.append(f"- Direct instruction — brief `{item.brief_id}`")
    _list_section(lines, "Scope", draft.scope)
    _list_section(lines, "Constraints", draft.constraints)
    _list_section(lines, "Non-goals", draft.non_goals)
    _list_section(lines, "Affected areas", draft.affected_areas)
    lines.extend(("", "## Implementation tasks", ""))
    for task in draft.tasks:
        lines.extend((f"### {task.id}: {task.title}", "", task.description))
        _list_section(lines, "Affected paths", [f"`{value}`" for value in task.affected_paths], 4)
        _list_section(lines, "Depends on", [f"`{value}`" for value in task.depends_on], 4)
        _list_section(
            lines,
            "Workspace evidence",
            [f"`{value}`" for value in task.evidence_ids],
            4,
        )
        _list_section(lines, "Acceptance criteria", task.acceptance_criteria, 4)
    _list_section(lines, "Validation", draft.validation)
    _list_section(lines, "Risks", draft.risks)
    _list_section(lines, "Open questions", draft.open_questions)
    if variants:
        lines.extend(("", "## Variants requiring resolution", ""))
        for variant in variants:
            lines.extend((f"### {variant.id}", "", variant.question, ""))
            lines.extend(f"- {option}" for option in variant.options)
    _list_section(lines, "Blocking issues", blocking_issues or [])
    return "\n".join(lines).rstrip() + "\n"


def _list_section(
    lines: list[str],
    heading: str,
    values: list[str],
    level: int = 2,
) -> None:
    if not values:
        return
    lines.extend(("", f"{'#' * level} {heading}", ""))
    lines.extend(f"- {value}" for value in values)


def _source_reference(source: PlanSource) -> dict[str, str | None]:
    if source.source_kind == "decision":
        return {
            "source_kind": source.source_kind,
            "source_id": source.decision_id,
            "source_path": None,
        }
    return {
        "source_kind": source.source_kind,
        "source_id": source.brief_id,
        "source_path": source.source_path,
    }
