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
    _SOURCE_ROOTS,
    _STOPWORDS,
    _TERM,
    MAX_QUERY_TERMS,
    _catalog,
    _digest,
    _digest_bytes,
    _git_content_scores,
    _git_grep,
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
MAX_ENRICHMENT_REQUIRED_ANCHORS = 9
MAX_ENRICHMENT_FRAGMENT_LINES = 60
MAX_REQUIRED_LOCATION_FILES = 64

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
    paths, anchors, required_anchors = _plan_signals(candidates, set(catalog))
    if not paths and not anchors:
        return context
    terms = _expanded_terms(anchors)
    required_order = sorted(required_anchors)
    content_scores, required_results = await asyncio.gather(
        _git_content_scores(workspace, terms, anchors),
        asyncio.gather(
            *(_git_grep(workspace, [anchor]) for anchor in required_order)
        ),
    )
    required_candidates = {
        anchor: sorted(
            locations,
            key=lambda path: (
                not (
                    path.parts and path.parts[0].casefold() in _SOURCE_ROOTS
                ),
                path.as_posix(),
            ),
        )[:MAX_REQUIRED_LOCATION_FILES]
        for anchor, locations in zip(required_order, required_results, strict=True)
    }
    text_cache: dict[Path, str | None] = {}

    def text_for(path: Path) -> str | None:
        if path not in text_cache:
            text_cache[path] = _read_text(workspace, path)
        return text_cache[path]

    occurrence_locations = {
        anchor: {
            path
            for path in locations
            if not path.parts or path.parts[0].casefold() != "docs"
            if path.suffix.casefold() != ".md"
            if (text := text_for(path)) is not None
            and _contains_anchor(text, anchor)
        }
        for anchor, locations in required_candidates.items()
    }
    definition_locations = {
        anchor: {
            path
            for path in locations
            if (text := text_for(path)) is not None
            and _contains_definition(text, anchor)
        }
        for anchor, locations in occurrence_locations.items()
    }
    required_definition_anchors = {
        anchor for anchor, locations in definition_locations.items() if locations
    }
    required_locations = {
        anchor: definition_locations[anchor] or occurrence_locations[anchor]
        for anchor in required_order
    }
    required_path_scores: dict[Path, int] = {}
    for locations in required_locations.values():
        for path in locations:
            required_path_scores[path] = required_path_scores.get(path, 0) + 1
    required_paths = [
        path
        for path, _ in sorted(
            required_path_scores.items(),
            key=lambda item: (
                not (
                    item[0].parts
                    and item[0].parts[0].casefold() in _SOURCE_ROOTS
                ),
                -item[1],
                item[0].as_posix(),
            ),
        )
    ]
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
                *required_paths,
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
            priority_anchors=required_anchors,
            definition_anchors=required_definition_anchors,
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
    all_evidence = [*context.evidence, *enrichment]
    covered_required = {
        anchor
        for anchor in required_anchors
        if any(
            (
                _contains_definition(item.content, anchor)
                if anchor in required_definition_anchors
                else _contains_anchor(item.content, anchor)
            )
            for item in all_evidence
        )
    }
    unresolved_required = sorted(
        anchor
        for anchor, locations in required_locations.items()
        if locations and anchor not in covered_required
    )
    truncated = truncated or bool(unresolved_required)
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
                        "enrichment_required_anchors": required_order,
                        "enrichment_required_definition_anchors": sorted(
                            required_definition_anchors
                        ),
                        "enrichment_unresolved_anchors": unresolved_required,
                        "enrichment_truncated": truncated,
                        "truncated": context.manifest.truncated or truncated,
                    }
                )
            }
        )
    evidence = all_evidence
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
            "enrichment_required_anchors": required_order,
            "enrichment_required_definition_anchors": sorted(
                required_definition_anchors
            ),
            "enrichment_unresolved_anchors": unresolved_required,
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
) -> tuple[list[Path], set[str], set[str]]:
    path_scores: dict[Path, int] = {}
    global_scores: dict[str, int] = {}
    scores_by_author: dict[str, dict[str, int]] = {}
    required_by_author: dict[str, set[str]] = {}

    def add_value(scores: dict[str, int], value: str, weight: int) -> set[str]:
        explicit = {
            token.casefold()
            for quoted in _BACKTICK_PATH.findall(value)
            for token in _TERM.findall(quoted)
        }
        explicit.update(token.casefold() for token in _CALL_SYMBOL.findall(value))
        found: set[str] = set()
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
            scores[normalized] = scores.get(normalized, 0) + weight
            global_scores[normalized] = global_scores.get(normalized, 0) + weight
            found.add(normalized)
        return found

    for participant_id, draft in sorted(candidates.items()):
        author_scores: dict[str, int] = {}
        required: set[str] = set()
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
            add_value(author_scores, path_value, 2)
        add_value(author_scores, draft.title, 1)
        add_value(author_scores, draft.objective, 1)
        for value in (
            *draft.scope,
            *draft.constraints,
            *draft.non_goals,
            *draft.validation,
        ):
            add_value(author_scores, value, 1)
        for value in draft.risks:
            add_value(author_scores, value, 3)
        for value in draft.open_questions:
            required.update(add_value(author_scores, value, 6))
        for task in draft.tasks:
            add_value(author_scores, task.title, 2)
            add_value(author_scores, task.description, 2)
            for value in task.acceptance_criteria:
                add_value(author_scores, value, 2)
        scores_by_author[participant_id] = author_scores
        required_by_author[participant_id] = required

    ranked_paths = [
        path
        for path, _ in sorted(
            path_scores.items(),
            key=lambda item: (-item[1], item[0].as_posix()),
        )
    ]
    ranked_by_author = {
        participant_id: [
            anchor
            for anchor, _ in sorted(
                scores.items(),
                key=lambda item: (-item[1], -len(item[0]), item[0]),
            )
        ]
        for participant_id, scores in scores_by_author.items()
    }
    ranked_required = {
        participant_id: sorted(
            required,
            key=lambda anchor: (
                -scores_by_author[participant_id].get(anchor, 0),
                -len(anchor),
                anchor,
            ),
        )
        for participant_id, required in required_by_author.items()
    }
    participant_ids = sorted(candidates)
    required_quota = max(
        1,
        MAX_ENRICHMENT_REQUIRED_ANCHORS // max(1, len(participant_ids)),
    )
    selected_required: list[str] = []
    for rank in range(required_quota):
        for participant_id in participant_ids:
            values = ranked_required[participant_id]
            if rank < len(values) and values[rank] not in selected_required:
                selected_required.append(values[rank])
                if len(selected_required) >= MAX_ENRICHMENT_REQUIRED_ANCHORS:
                    break
        if len(selected_required) >= MAX_ENRICHMENT_REQUIRED_ANCHORS:
            break

    selected = list(selected_required)
    max_author_signals = max(
        (len(values) for values in ranked_by_author.values()),
        default=0,
    )
    for rank in range(max_author_signals):
        for participant_id in participant_ids:
            values = ranked_by_author[participant_id]
            if rank < len(values) and values[rank] not in selected:
                selected.append(values[rank])
                if len(selected) >= MAX_ENRICHMENT_ANCHORS:
                    break
        if len(selected) >= MAX_ENRICHMENT_ANCHORS:
            break
    if len(selected) < MAX_ENRICHMENT_ANCHORS:
        for anchor, _ in sorted(
            global_scores.items(),
            key=lambda item: (-item[1], -len(item[0]), item[0]),
        ):
            if anchor not in selected:
                selected.append(anchor)
            if len(selected) >= MAX_ENRICHMENT_ANCHORS:
                break
    return ranked_paths, set(selected), set(selected_required)


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
    priority_anchors: set[str],
    definition_anchors: set[str],
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
    covered_anchors = set().union(
        *(
            _covered_anchors(
                "\n".join(lines[start - 1 : end]),
                anchors,
                definition_anchors,
            )
            for start, end in excluded_ranges
        )
    ) if excluded_ranges else set()
    ranked_bounds = _ranked_fragment_bounds(
        lines,
        terms,
        anchors,
        max_lines=MAX_ENRICHMENT_FRAGMENT_LINES,
    )
    def priority(bounds: tuple[int, int]) -> tuple[int, int, int]:
        window_lines = lines[bounds[0] : bounds[1]]
        content = "\n".join(window_lines).casefold()
        definition_hits = sum(
            _contains_definition(content, anchor) for anchor in priority_anchors
        )
        definition_tail = sum(
            min(len(window_lines) - index, MAX_ENRICHMENT_FRAGMENT_LINES // 2)
            for index, line in enumerate(window_lines)
            for anchor in priority_anchors
            if _contains_definition(line, anchor)
        )
        covered = sum(_contains_anchor(content, anchor) for anchor in priority_anchors)
        return definition_hits, definition_tail, covered

    ranked_bounds.sort(key=priority, reverse=True)
    for start, end in ranked_bounds:
        line_start = start + 1
        if any(
            line_start <= existing_end and end >= existing_start
            for existing_start, existing_end in selected_ranges
        ):
            continue
        content = "\n".join(lines[start:end]) + "\n"
        fragment_anchors = _covered_anchors(content, anchors, definition_anchors)
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


def _contains_anchor(text: str, anchor: str) -> bool:
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(anchor)}(?![A-Za-z0-9_])",
            text,
            re.IGNORECASE,
        )
    )


def _contains_definition(text: str, anchor: str) -> bool:
    return bool(
        re.search(
            rf"\b(?:class|struct|enum|interface|protocol|type|def|func|function|const|let|var)\s+{re.escape(anchor)}(?![A-Za-z0-9_])",
            text,
            re.IGNORECASE,
        )
    )


def _covered_anchors(
    text: str,
    anchors: set[str],
    definition_anchors: set[str],
) -> set[str]:
    folded = text.casefold()
    return {
        anchor
        for anchor in anchors
        if (
            _contains_definition(folded, anchor)
            if anchor in definition_anchors
            else anchor in folded
        )
    }
