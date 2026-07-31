from __future__ import annotations

import asyncio
import re
from pathlib import Path

from ego.models import (
    Evidence,
    EvidenceStatus,
    PlanDraft,
    PlanTask,
    PlanWorkspaceEvidence,
    WorkspaceContext,
    WorkspaceContextEvidence,
    WorkspaceContextEvidenceReference,
    WorkspaceContextManifest,
)
from ego.planning.context import _digest, _digest_bytes, _is_allowed_path, _read_text
from ego.workspace import validate_evidence

MAX_DISCOVERED_CITATIONS_PER_DRAFT = 24
MAX_DISCOVERED_CITATION_LINES = 80


async def stale_workspace_evidence_ids(
    workspace: Path,
    manifest: WorkspaceContextManifest,
) -> list[str]:
    """Return evidence ids whose frozen source file no longer matches."""
    resolved = await asyncio.to_thread(workspace.resolve)
    return await asyncio.to_thread(
        _stale_workspace_evidence_ids,
        resolved,
        manifest,
    )


def validate_plan_draft_evidence(
    workspace: Path,
    draft: PlanDraft,
    context: WorkspaceContext,
    *,
    allow_discovered: bool,
) -> list[str]:
    """Validate that existing affected files are backed by direct evidence."""
    errors: list[str] = []
    references = {item.id: item for item in context.manifest.evidence}
    discovered_count = sum(len(task.workspace_evidence) for task in draft.tasks)
    if discovered_count > MAX_DISCOVERED_CITATIONS_PER_DRAFT:
        errors.append(
            "plan exceeds the discovered workspace evidence limit "
            f"({discovered_count}/{MAX_DISCOVERED_CITATIONS_PER_DRAFT})"
        )
    for task in draft.tasks:
        errors.extend(
            _task_evidence_errors(
                workspace,
                task,
                references,
                allow_discovered=allow_discovered,
            )
        )
    return errors


def freeze_discovered_evidence(
    workspace: Path,
    context: WorkspaceContext,
    candidates: dict[str, PlanDraft],
) -> tuple[WorkspaceContext, dict[str, PlanDraft], list[str]]:
    """Freeze the validated union of author-discovered citations in memory."""
    existing = list(context.evidence)
    by_range = {(item.path, item.line_start, item.line_end): item for item in existing}
    discovered_ids: list[str] = []
    issues: list[str] = []
    normalized: dict[str, PlanDraft] = {}

    for participant_id, draft in sorted(candidates.items()):
        author_errors = validate_plan_draft_evidence(
            workspace,
            draft,
            context,
            allow_discovered=True,
        )
        issues.extend(f"{participant_id}: {error}" for error in author_errors)
        tasks: list[PlanTask] = []
        for task in draft.tasks:
            evidence_ids = list(task.evidence_ids)
            for citation in task.workspace_evidence:
                item = _freeze_citation(workspace, citation)
                if item is None:
                    continue
                key = (item.path, item.line_start, item.line_end)
                frozen = by_range.get(key)
                if frozen is None:
                    by_range[key] = item
                    existing.append(item)
                    frozen = item
                if frozen.id not in evidence_ids:
                    evidence_ids.append(frozen.id)
                if frozen.id not in discovered_ids:
                    discovered_ids.append(frozen.id)
            for evidence_id in evidence_ids:
                if evidence_id not in discovered_ids:
                    discovered_ids.append(evidence_id)
            tasks.append(task.model_copy(update={"evidence_ids": evidence_ids}))
        normalized[participant_id] = draft.model_copy(update={"tasks": tasks})

    discovered_id_set = set(discovered_ids)
    discovery_bytes = sum(
        len(item.content.encode("utf-8"))
        for item in existing
        if item.id in discovered_id_set
    )
    references = [_reference(item) for item in existing]
    fingerprint = _digest(
        "\n".join(
            [
                context.manifest.workspace_fingerprint,
                *(
                    f"{item.path}:{item.file_sha256}"
                    for item in existing
                    if item.id in discovered_ids
                ),
            ]
        )
    )
    context_id = _digest(
        "\n".join(
            [
                context.manifest.context_id,
                *(
                    f"{item.id}:{item.fragment_sha256}"
                    for item in existing
                    if item.id in discovered_ids
                ),
            ]
        )
    )[:16]
    manifest = context.manifest.model_copy(
        update={
            "context_id": context_id,
            "initial_context_id": context.manifest.initial_context_id
            or context.manifest.context_id,
            "workspace_fingerprint": fingerprint,
            "evidence": references,
            "discovered_evidence_ids": discovered_ids,
            "discovery_bytes_used": discovery_bytes,
            "bytes_used": context.manifest.bytes_used + discovery_bytes,
        }
    )
    return (
        WorkspaceContext(
            manifest=manifest,
            project_map=context.project_map,
            evidence=existing,
        ),
        normalized,
        issues,
    )


def _task_evidence_errors(
    workspace: Path,
    task: PlanTask,
    references: dict[str, WorkspaceContextEvidenceReference],
    *,
    allow_discovered: bool,
) -> list[str]:
    errors: list[str] = []
    citation_paths: set[str] = set()
    for evidence_id in task.evidence_ids:
        reference = references.get(evidence_id)
        if reference is None:
            errors.append(f"task {task.id} references unknown evidence {evidence_id}")
        else:
            citation_paths.add(reference.path)
    if task.workspace_evidence and not allow_discovered:
        errors.append(f"task {task.id} may not add workspace evidence after independent planning")
    for citation in task.workspace_evidence if allow_discovered else []:
        error = _citation_error(workspace, citation)
        if error is None:
            citation_paths.add(citation.path)
        else:
            errors.append(f"task {task.id} has invalid workspace evidence: {error}")
    for value in task.affected_paths:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or not _is_allowed_path(path):
            continue
        candidate = workspace / path
        if candidate.is_dir():
            errors.append(
                f"task {task.id} must name concrete files instead of existing directory {value}"
            )
            continue
        if candidate.is_file() and value not in citation_paths:
            available = sorted(
                evidence_id
                for evidence_id, reference in references.items()
                if reference.path == value
            )
            hint = (
                " (available evidence: " + ", ".join(available) + ")"
                if available
                else ""
            )
            errors.append(
                f"task {task.id} affects existing file {value} without direct workspace evidence"
                + hint
            )
    return errors


def _citation_error(workspace: Path, citation: PlanWorkspaceEvidence) -> str | None:
    path = Path(citation.path)
    if path.is_absolute() or ".." in path.parts or not _is_allowed_path(path):
        return f"unsafe or excluded path {citation.path}"
    if citation.line_end - citation.line_start + 1 > MAX_DISCOVERED_CITATION_LINES:
        return (
            f"{citation.path}:{citation.line_start}-{citation.line_end} exceeds the "
            f"{MAX_DISCOVERED_CITATION_LINES}-line limit"
        )
    checked = validate_evidence(
        workspace,
        Evidence(
            path=citation.path,
            line_start=citation.line_start,
            line_end=citation.line_end,
            explanation=citation.explanation,
            critical=True,
        ),
    )
    if checked.status is not EvidenceStatus.CITATION_VERIFIED:
        return checked.validation_error or "citation could not be verified"
    fragment = _fragment(workspace, citation)
    missing = [
        symbol
        for symbol in citation.symbols
        if not re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])",
            fragment,
        )
    ]
    if missing:
        return "cited fragment does not contain declared symbols: " + ", ".join(missing)
    return None


def _freeze_citation(
    workspace: Path,
    citation: PlanWorkspaceEvidence,
) -> WorkspaceContextEvidence | None:
    if _citation_error(workspace, citation) is not None:
        return None
    path = workspace / citation.path
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    content = "".join(lines[citation.line_start - 1 : citation.line_end])
    data = text.encode("utf-8")
    path_value = Path(citation.path).as_posix()
    evidence_key = f"{path_value}:{citation.line_start}:{citation.line_end}"
    return WorkspaceContextEvidence(
        id=f"CTX-{_digest(evidence_key)[:10]}",
        path=path_value,
        line_start=citation.line_start,
        line_end=citation.line_end,
        file_sha256=_digest_bytes(data),
        fragment_sha256=_digest_bytes(content.encode("utf-8")),
        reason="author-discovered workspace evidence: " + citation.explanation,
        content=content,
    )


def _fragment(workspace: Path, citation: PlanWorkspaceEvidence) -> str:
    lines = (workspace / citation.path).read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[citation.line_start - 1 : citation.line_end])


def _reference(item: WorkspaceContextEvidence) -> WorkspaceContextEvidenceReference:
    return WorkspaceContextEvidenceReference(
        id=item.id,
        path=item.path,
        line_start=item.line_start,
        line_end=item.line_end,
        file_sha256=item.file_sha256,
        fragment_sha256=item.fragment_sha256,
        reason=item.reason,
    )


def _stale_workspace_evidence_ids(
    workspace: Path,
    manifest: WorkspaceContextManifest,
) -> list[str]:
    current_hashes: dict[Path, str | None] = {}
    stale: list[str] = []
    for item in manifest.evidence:
        path = Path(item.path)
        if path not in current_hashes:
            text = _read_text(workspace, path)
            current_hashes[path] = (
                None if text is None else _digest_bytes(text.encode("utf-8"))
            )
        if current_hashes[path] != item.file_sha256:
            stale.append(item.id)
    return stale
