from __future__ import annotations

import asyncio
import re
from pathlib import Path

from ego.models import (
    PlanDraft,
    WorkspaceContext,
    WorkspaceContextEvidence,
    WorkspaceContextEvidenceReference,
    WorkspaceContextManifest,
)
from ego.planning.context import (
    _BACKTICK_PATH,
    _CAMEL_BOUNDARY,
    _STOPWORDS,
    _TERM,
    MAX_QUERY_TERMS,
    _catalog,
    _digest,
    _digest_bytes,
    _git_content_scores,
    _is_allowed_path,
    _ranked_fragment_bounds,
    _read_text,
    _score_paths,
)

MAX_ENRICHMENT_BYTES = 16 * 1024
MAX_ENRICHMENT_TOTAL_PROMPT_BYTES = 64 * 1024
MAX_ENRICHMENT_FILES = 6
MAX_ENRICHMENT_FRAGMENTS_PER_FILE = 2
MAX_ENRICHMENT_ANCHORS = 16
MAX_ENRICHMENT_FRAGMENT_LINES = 60

_CALL_SYMBOL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


async def enrich_workspace_context(
    *,
    workspace: Path,
    context: WorkspaceContext,
    candidates: dict[str, PlanDraft],
    byte_budget: int,
) -> WorkspaceContext:
    """Add bounded evidence for technical signals discovered by plan authors."""
    workspace = await asyncio.to_thread(workspace.resolve)
    catalog, _ = await _catalog(workspace)
    paths, anchors = _plan_signals(candidates, set(catalog))
    if not paths and not anchors:
        return context
    terms = _expanded_terms(anchors)
    content_scores = await _git_content_scores(workspace, terms, anchors)
    scored = _score_paths(
        catalog,
        terms=terms,
        anchors=anchors,
        source_paths=set(paths),
        modified_paths=set(),
        content_scores=content_scores,
    )
    ranked_paths = list(
        dict.fromkeys(
            [
                *paths,
                *(
                    path
                    for _, path in scored
                    if path in content_scores
                    or any(
                        term in path.as_posix().casefold()
                        for term in terms
                    )
                ),
            ]
        )
    )
    remaining_total = max(0, byte_budget - context.manifest.bytes_used)
    later_call_count = max(1, len(candidates) + 2)
    enrichment_budget = min(
        MAX_ENRICHMENT_BYTES,
        MAX_ENRICHMENT_TOTAL_PROMPT_BYTES // later_call_count,
        remaining_total,
    )
    existing_ranges: dict[Path, list[tuple[int, int]]] = {}
    for item in context.evidence:
        existing_ranges.setdefault(Path(item.path), []).append(
            (item.line_start, item.line_end)
        )
    enrichment: list[WorkspaceContextEvidence] = []
    enrichment_bytes = 0
    truncated = False
    for path_index, path in enumerate(ranked_paths):
        if len(enrichment) >= MAX_ENRICHMENT_FILES:
            truncated = path_index < len(ranked_paths)
            break
        items = _adaptive_evidence(
            workspace,
            path,
            terms=terms,
            anchors=anchors,
            excluded_ranges=existing_ranges.get(path, []),
        )
        for item in items:
            item_bytes = len(item.content.encode("utf-8"))
            if (
                len(enrichment) >= MAX_ENRICHMENT_FILES
                or enrichment_bytes + item_bytes > enrichment_budget
            ):
                truncated = True
                continue
            enrichment.append(item)
            enrichment_bytes += item_bytes
            existing_ranges.setdefault(path, []).append(
                (item.line_start, item.line_end)
            )
    if not enrichment and not truncated:
        return context
    if not enrichment:
        return context.model_copy(
            update={
                "manifest": context.manifest.model_copy(
                    update={
                        "initial_context_id": (
                            context.manifest.initial_context_id
                            or context.manifest.context_id
                        ),
                        "enrichment_byte_budget": enrichment_budget,
                        "enrichment_truncated": True,
                        "truncated": True,
                    }
                )
            }
        )
    evidence = [*context.evidence, *enrichment]
    references = [
        WorkspaceContextEvidenceReference(
            id=item.id,
            path=item.path,
            line_start=item.line_start,
            line_end=item.line_end,
            file_sha256=item.file_sha256,
            fragment_sha256=item.fragment_sha256,
            reason=item.reason,
        )
        for item in evidence
    ]
    enrichment_ids = [item.id for item in enrichment]
    fingerprint = _digest(
        "\n".join(
            [
                context.manifest.workspace_fingerprint,
                *(f"{item.path}:{item.file_sha256}" for item in enrichment),
            ]
        )
    )
    context_id = _digest(
        "\n".join(
            [
                context.manifest.context_id,
                *(f"{item.id}:{item.fragment_sha256}" for item in enrichment),
            ]
        )
    )[:16]
    manifest = context.manifest.model_copy(
        update={
            "context_id": context_id,
            "initial_context_id": (
                context.manifest.initial_context_id
                or context.manifest.context_id
            ),
            "workspace_fingerprint": fingerprint,
            "evidence": references,
            "enrichment_evidence_ids": enrichment_ids,
            "enrichment_bytes_used": enrichment_bytes,
            "enrichment_byte_budget": enrichment_budget,
            "enrichment_truncated": truncated,
            "bytes_used": context.manifest.bytes_used + enrichment_bytes,
            "truncated": context.manifest.truncated or truncated,
        }
    )
    return WorkspaceContext(
        manifest=manifest,
        project_map=context.project_map,
        evidence=evidence,
    )


async def stale_workspace_evidence_ids(
    workspace: Path,
    manifest: WorkspaceContextManifest,
) -> list[str]:
    """Return evidence ids whose source file no longer matches the frozen hash."""
    workspace = await asyncio.to_thread(workspace.resolve)
    return await asyncio.to_thread(
        _stale_workspace_evidence_ids,
        workspace,
        manifest,
    )


def _plan_signals(
    candidates: dict[str, PlanDraft],
    catalog: set[Path],
) -> tuple[list[Path], set[str]]:
    path_scores: dict[Path, int] = {}
    anchor_scores: dict[str, int] = {}

    def add_value(value: str, weight: int) -> None:
        explicit = {
            token.casefold()
            for quoted in _BACKTICK_PATH.findall(value)
            for token in _TERM.findall(quoted)
        }
        explicit.update(token.casefold() for token in _CALL_SYMBOL.findall(value))
        for match in _TERM.finditer(value):
            raw = match.group(0).strip("._-")
            normalized = raw.casefold()
            if (
                len(normalized) < 3
                or normalized in _STOPWORDS
                or not (
                    "_" in raw
                    or "." in raw
                    or "-" in raw
                    or bool(_CAMEL_BOUNDARY.search(raw))
                    or (raw.isupper() and len(raw) >= 3)
                    or normalized in explicit
                )
            ):
                continue
            anchor_scores[normalized] = anchor_scores.get(normalized, 0) + weight

    for draft in candidates.values():
        for path_value in (
            *draft.affected_areas,
            *(path for task in draft.tasks for path in task.affected_paths),
        ):
            path = Path(path_value)
            if (
                not path.is_absolute()
                and ".." not in path.parts
                and path in catalog
                and _is_allowed_path(path)
            ):
                path_scores[path] = path_scores.get(path, 0) + 4
            add_value(path_value, 2)
        add_value(draft.title, 1)
        add_value(draft.objective, 1)
        for value in (
            *draft.scope,
            *draft.constraints,
            *draft.non_goals,
            *draft.validation,
        ):
            add_value(value, 1)
        for value in (*draft.risks, *draft.open_questions):
            add_value(value, 3)
        for task in draft.tasks:
            add_value(task.title, 2)
            add_value(task.description, 2)
            for value in task.acceptance_criteria:
                add_value(value, 2)

    ranked_paths = [
        path
        for path, _ in sorted(
            path_scores.items(),
            key=lambda item: (-item[1], item[0].as_posix()),
        )
    ]
    ranked_anchors = [
        anchor
        for anchor, _ in sorted(
            anchor_scores.items(),
            key=lambda item: (-item[1], -len(item[0]), item[0]),
        )
    ][:MAX_ENRICHMENT_ANCHORS]
    return ranked_paths, set(ranked_anchors)


def _expanded_terms(anchors: set[str]) -> list[str]:
    terms = set(anchors)
    for anchor in anchors:
        for part in re.split(r"[._-]+", anchor):
            if len(part) >= 3 and part not in _STOPWORDS:
                terms.add(part)
    return sorted(
        terms,
        key=lambda value: (value not in anchors, -len(value), value),
    )[:MAX_QUERY_TERMS]


def _adaptive_evidence(
    workspace: Path,
    path: Path,
    *,
    terms: list[str],
    anchors: set[str],
    excluded_ranges: list[tuple[int, int]],
) -> list[WorkspaceContextEvidence]:
    text = _read_text(workspace, path)
    if text is None:
        return []
    lines = text.splitlines()
    if not lines:
        return []
    evidence: list[WorkspaceContextEvidence] = []
    selected_ranges = list(excluded_ranges)
    covered_anchors = {
        anchor
        for start, end in excluded_ranges
        for anchor in anchors
        if anchor in "\n".join(lines[start - 1 : end]).casefold()
    }
    for start, end in _ranked_fragment_bounds(
        lines,
        terms,
        anchors,
        max_lines=MAX_ENRICHMENT_FRAGMENT_LINES,
    ):
        line_start = start + 1
        if any(
            line_start <= existing_end and end >= existing_start
            for existing_start, existing_end in selected_ranges
        ):
            continue
        content = "\n".join(lines[start:end]) + "\n"
        fragment_anchors = {
            anchor for anchor in anchors if anchor in content.casefold()
        }
        if selected_ranges and not fragment_anchors.difference(covered_anchors):
            continue
        data = text.encode("utf-8")
        fragment = content.encode("utf-8")
        path_value = path.as_posix()
        evidence.append(
            WorkspaceContextEvidence(
                id=f"CTX-{_digest(path_value + ':' + str(line_start) + ':' + str(end))[:10]}",
                path=path_value,
                line_start=line_start,
                line_end=end,
                file_sha256=_digest_bytes(data),
                fragment_sha256=_digest_bytes(fragment),
                reason="plan-derived workspace evidence",
                content=content,
            )
        )
        selected_ranges.append((line_start, end))
        covered_anchors.update(fragment_anchors)
        if len(evidence) >= MAX_ENRICHMENT_FRAGMENTS_PER_FILE:
            break
    return evidence


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
