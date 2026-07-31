from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
from pathlib import Path

from ego.models import (
    PlanSource,
    WorkspaceContext,
    WorkspaceContextEvidence,
    WorkspaceContextEvidenceReference,
    WorkspaceContextManifest,
)

MAX_CONTEXT_BYTES = 128 * 1024
MAX_CONTEXT_FILES = 12
MAX_CATALOG_FILES = 20_000
MAX_FRAGMENT_LINES = 100
MAX_FILE_BYTES = 256 * 1024
MAX_PROJECT_MAP_PATHS = 120
MAX_OMITTED_PATHS = 50
MAX_QUERY_TERMS = 32
MAX_INSTRUCTION_REFERENCES = 3
SOURCE_PATH_BONUS = 400
_SOURCE_ROOTS = frozenset({"app", "lib", "packages", "src"})

_EXCLUDED_PARTS = frozenset(
    {
        ".dart_tool",
        ".ego",
        ".git",
        ".gradle",
        ".idea",
        ".next",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        ".vscode",
        "__pycache__",
        "build",
        "coverage",
        "deriveddata",
        "dist",
        "node_modules",
        "pods",
        "target",
        "venv",
    }
)
_SENSITIVE_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".pypirc",
        "auth.json",
        "credentials",
        "credentials.json",
        "id_dsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "service-account.json",
    }
)
_SENSITIVE_SUFFIXES = (".key", ".p12", ".pem")
_TEXT_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".css",
        ".dart",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".kts",
        ".md",
        ".php",
        ".proto",
        ".py",
        ".rb",
        ".rs",
        ".scss",
        ".sh",
        ".sql",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_TEXT_NAMES = frozenset(
    {
        "Dockerfile",
        "Gemfile",
        "Makefile",
        "Package.swift",
        "Podfile",
        "Procfile",
        "pyproject.toml",
    }
)
_STOPWORDS = frozenset(
    {
        "actualizar",
        "add",
        "agente",
        "build",
        "con",
        "crear",
        "create",
        "del",
        "desde",
        "direct",
        "esta",
        "este",
        "from",
        "hacer",
        "implementation",
        "instruction",
        "las",
        "los",
        "para",
        "plan",
        "planning",
        "por",
        "process",
        "proceso",
        "project",
        "que",
        "sin",
        "sobre",
        "that",
        "the",
        "this",
        "todo",
        "una",
        "uno",
        "with",
    }
)
_TERM = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ_][A-Za-zÀ-ÖØ-öø-ÿ0-9_.-]{2,}")
_BACKTICK_PATH = re.compile(r"`([^`\n]+)`")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-zà-öø-ÿ0-9])(?=[A-ZÀ-ÖØ-Þ])")
_EGO_COMMAND = re.compile(r"\bego\s+([A-Za-z][A-Za-z0-9_-]{2,})", re.IGNORECASE)


class WorkspaceContextBuilder:
    """Builds one bounded, provider-neutral context without writing to disk."""

    def __init__(self, byte_budget: int = MAX_CONTEXT_BYTES) -> None:
        self.byte_budget = byte_budget

    async def build(
        self,
        *,
        workspace: Path,
        question: str,
        sources: list[PlanSource],
        git_head: str | None,
        git_status: str | None,
    ) -> WorkspaceContext:
        workspace = await asyncio.to_thread(workspace.resolve)
        catalog, catalog_complete = await _catalog(workspace)
        terms, anchors = _query_terms(question, sources)
        source_paths = _source_paths(sources)
        modified_paths = _modified_paths(git_status)
        content_scores = await _git_content_scores(workspace, terms, anchors)
        scored = _score_paths(
            catalog,
            terms=terms,
            anchors=anchors,
            source_paths=source_paths,
            modified_paths=modified_paths,
            content_scores=content_scores,
        )
        relevant_paths = [path for score, path in scored if score > 0]
        instruction_paths = _instruction_paths(workspace, catalog, relevant_paths)
        mandatory_paths, reference_paths = _instruction_references(
            workspace,
            instruction_paths,
        )
        reference_set = set(reference_paths)
        ranked_references = [
            path
            for _, path in scored
            if path in reference_set
        ][:MAX_INSTRUCTION_REFERENCES]
        selected_paths = list(
            dict.fromkeys(
                [*instruction_paths, *mandatory_paths, *ranked_references]
            )
        )
        selected_paths.extend(
            path
            for path in relevant_paths
            if path not in selected_paths and path not in reference_set
        )
        project_map = [path.as_posix() for _, path in scored[:MAX_PROJECT_MAP_PATHS]]
        bytes_used = sum(len(path.encode("utf-8")) + 1 for path in project_map)
        evidence: list[WorkspaceContextEvidence] = []
        omitted: list[str] = []
        mandatory_set = set(instruction_paths) | set(mandatory_paths)
        mandatory_missing = False
        relevant_evidence = 0

        for path in selected_paths:
            if len(evidence) >= MAX_CONTEXT_FILES:
                omitted.append(path.as_posix())
                if path in mandatory_set:
                    mandatory_missing = True
                continue
            item = _evidence(
                workspace,
                path,
                terms=terms,
                anchors=anchors,
                full_file=path in mandatory_set,
            )
            if item is None:
                omitted.append(path.as_posix())
                if path in mandatory_set:
                    mandatory_missing = True
                continue
            item_bytes = len(item.content.encode("utf-8"))
            if bytes_used + item_bytes > self.byte_budget:
                omitted.append(path.as_posix())
                if path in mandatory_set:
                    mandatory_missing = True
                continue
            evidence.append(item)
            bytes_used += item_bytes
            if path not in mandatory_set:
                relevant_evidence += 1

        if not catalog_complete:
            omitted.append("<workspace catalog exceeded limit>")
        truncated = bool(omitted)
        relevant_items = [
            item for item in evidence if Path(item.path) not in mandatory_set
        ]
        covered_anchors = {
            anchor
            for anchor in anchors
            if any(
                anchor in f"{item.path}\n{item.content}".casefold()
                for item in relevant_items
            )
        }
        required_anchor_count = math.ceil(len(anchors) / 2)
        anchors_sufficient = (
            not anchors or len(covered_anchors) >= required_anchor_count
        )
        sufficient = (
            catalog_complete
            and not mandatory_missing
            and relevant_evidence > 0
            and anchors_sufficient
        )
        fallback_reason = None
        if not sufficient:
            if mandatory_missing:
                fallback_reason = "mandatory workspace instructions did not fit"
            elif not catalog_complete:
                fallback_reason = "workspace catalog exceeded the deterministic limit"
            elif not anchors_sufficient:
                fallback_reason = (
                    "workspace evidence covered "
                    f"{len(covered_anchors)}/{len(anchors)} query anchors"
                )
            else:
                fallback_reason = "no relevant workspace evidence was selected"
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
        fingerprint = _digest(
            "\n".join(
                [
                    git_head or "",
                    git_status or "",
                    *(f"{item.path}:{item.file_sha256}" for item in references),
                ]
            )
        )
        context_id = _digest(
            "\n".join(
                [
                    fingerprint,
                    question,
                    *(f"{item.id}:{item.fragment_sha256}" for item in references),
                ]
            )
        )[:16]
        manifest = WorkspaceContextManifest(
            context_id=context_id,
            workspace_fingerprint=fingerprint,
            evidence=references,
            omitted_paths=omitted[:MAX_OMITTED_PATHS],
            truncated=truncated,
            sufficient=sufficient,
            fallback_reason=fallback_reason,
            bytes_used=bytes_used,
            byte_budget=self.byte_budget,
        )
        return WorkspaceContext(
            manifest=manifest,
            project_map=project_map,
            evidence=evidence,
        )


def fallback_workspace_context(
    *,
    question: str,
    git_head: str | None,
    git_status: str | None,
    reason: str,
    byte_budget: int = MAX_CONTEXT_BYTES,
) -> WorkspaceContext:
    """Return a stable empty context that keeps protected participant reads enabled."""
    fingerprint = _digest("\n".join([git_head or "", git_status or ""]))
    context_id = _digest("\n".join([fingerprint, question, reason]))[:16]
    return WorkspaceContext(
        manifest=WorkspaceContextManifest(
            context_id=context_id,
            workspace_fingerprint=fingerprint,
            sufficient=False,
            fallback_reason=reason,
            byte_budget=byte_budget,
        ),
    )


async def _catalog(workspace: Path) -> tuple[list[Path], bool]:
    git_paths = await _git_paths(workspace)
    raw_paths = (
        git_paths
        if git_paths is not None
        else await asyncio.to_thread(_walk_paths, workspace)
    )
    paths = sorted(
        {
            path
            for path in raw_paths
            if _is_allowed_path(path) and _is_text_candidate(path)
        },
        key=lambda item: item.as_posix(),
    )
    complete = len(paths) <= MAX_CATALOG_FILES
    return paths[:MAX_CATALOG_FILES], complete


async def _git_paths(workspace: Path) -> list[Path] | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/git",
            "-C",
            str(workspace),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
    except OSError:
        return None
    if process.returncode != 0:
        return None
    return [
        Path(value.decode("utf-8", errors="surrogateescape"))
        for value in stdout.split(b"\0")
        if value
    ]


def _walk_paths(workspace: Path) -> list[Path]:
    paths: list[Path] = []
    for root, directories, files in os.walk(workspace):
        root_path = Path(root)
        directories[:] = [
            name
            for name in directories
            if name.casefold() not in _EXCLUDED_PARTS
            and not (root_path / name).is_symlink()
        ]
        for name in files:
            paths.append((root_path / name).relative_to(workspace))
            if len(paths) > MAX_CATALOG_FILES:
                return paths
    return paths


async def _git_content_scores(
    workspace: Path,
    terms: list[str],
    anchors: set[str],
) -> dict[Path, int]:
    searches = [
        _git_grep(workspace, terms[:12]),
        *(
            _git_grep(workspace, [anchor])
            for anchor in sorted(anchors)[:8]
        ),
    ]
    results = await asyncio.gather(*searches)
    scores = {path: 30 for path in results[0]}
    for paths in results[1:]:
        for path in paths:
            scores[path] = scores.get(path, 0) + 80
    return scores


async def _git_grep(workspace: Path, terms: list[str]) -> set[Path]:
    if not terms:
        return set()
    command = [
        "/usr/bin/git",
        "-C",
        str(workspace),
        "grep",
        "-I",
        "-i",
        "-l",
        "-z",
    ]
    for term in terms[:12]:
        command.extend(["-e", term])
    command.append("--")
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
    except OSError:
        return set()
    if process.returncode not in {0, 1}:
        return set()
    return {
        Path(value.decode("utf-8", errors="surrogateescape"))
        for value in stdout.split(b"\0")
        if value
    }


def _query_terms(
    question: str,
    sources: list[PlanSource],
) -> tuple[list[str], set[str]]:
    values = [question]
    for source in sources:
        if source.source_kind == "decision":
            values.extend((source.question, source.conclusion))
        else:
            values.append(source.instruction)
    terms: set[str] = set()
    anchors: set[str] = set()
    for value in values:
        for command in _EGO_COMMAND.findall(value):
            normalized_command = command.casefold()
            terms.add(normalized_command)
            anchors.add(normalized_command)
        for match in _TERM.finditer(value):
            raw = match.group(0).strip("._-")
            normalized = raw.casefold()
            if len(normalized) < 3 or normalized in _STOPWORDS:
                continue
            terms.add(normalized)
            identifier_like = (
                "_" in raw
                or bool(_CAMEL_BOUNDARY.search(raw))
                or (raw.isupper() and len(raw) >= 3)
            )
            if identifier_like:
                anchors.add(normalized)
            for part in re.split(r"[._-]+", _CAMEL_BOUNDARY.sub("_", raw)):
                normalized_part = part.casefold()
                if len(normalized_part) >= 3 and normalized_part not in _STOPWORDS:
                    terms.add(normalized_part)
    ordered = sorted(
        terms,
        key=lambda value: (value not in anchors, -len(value), value),
    )
    selected = ordered[:MAX_QUERY_TERMS]
    return selected, anchors.intersection(selected)


def _source_paths(sources: list[PlanSource]) -> set[Path]:
    paths: set[Path] = set()
    for source in sources:
        if source.source_kind == "decision":
            paths.update(Path(item.path) for item in source.evidence)
    return paths


def _modified_paths(status: str | None) -> set[Path]:
    if not status:
        return set()
    paths: set[Path] = set()
    for line in status.splitlines():
        value = line[3:].strip()
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[1]
        if value:
            paths.add(Path(value.strip('"')))
    return paths


def _score_paths(
    catalog: list[Path],
    *,
    terms: list[str],
    anchors: set[str],
    source_paths: set[Path],
    modified_paths: set[Path],
    content_scores: dict[Path, int],
) -> list[tuple[int, Path]]:
    scored: list[tuple[int, Path]] = []
    for path in catalog:
        value = path.as_posix().casefold()
        score = sum(
            (80 if term in anchors else 50)
            for term in terms
            if term in value
        )
        score += content_scores.get(path, 0)
        if path in source_paths:
            score += 80
        if path in modified_paths:
            score += 30
        if path.parts and path.parts[0].casefold() in _SOURCE_ROOTS:
            score += SOURCE_PATH_BONUS
        if path.name in _TEXT_NAMES or path.name.casefold() in {
            "cargo.toml",
            "package.json",
            "pubspec.yaml",
            "readme.md",
        }:
            score += 5
        scored.append((score, path))
    return sorted(scored, key=lambda item: (-item[0], item[1].as_posix()))


def _instruction_paths(
    workspace: Path,
    catalog: list[Path],
    relevant_paths: list[Path],
) -> list[Path]:
    available = set(catalog)
    directories = {Path(".")}
    for path in relevant_paths[:MAX_CONTEXT_FILES]:
        current = path.parent
        while current != Path("."):
            directories.add(current)
            current = current.parent
    result: list[Path] = []
    for directory in sorted(directories, key=lambda item: (len(item.parts), item.as_posix())):
        override = directory / "AGENTS.override.md"
        regular = directory / "AGENTS.md"
        if override in available:
            result.append(override)
        elif regular in available:
            result.append(regular)
    return result


def _instruction_references(
    workspace: Path,
    instruction_paths: list[Path],
) -> tuple[list[Path], list[Path]]:
    mandatory: list[Path] = []
    ranked: list[Path] = []
    for path in instruction_paths:
        text = _read_text(workspace, path)
        if text is None:
            continue
        for match in _BACKTICK_PATH.finditer(text):
            value = match.group(1).strip()
            if "/" not in value and not value.endswith(".md"):
                continue
            candidate = Path(value)
            if (
                candidate.is_absolute()
                or ".." in candidate.parts
                or not _is_allowed_path(candidate)
            ):
                continue
            absolute = workspace / candidate
            if absolute.is_dir() and not absolute.is_symlink():
                ranked.extend(
                    item.relative_to(workspace)
                    for item in sorted(absolute.rglob("*.md"))
                    if item.is_file()
                    and not item.is_symlink()
                    and _is_allowed_path(item.relative_to(workspace))
                )
            elif absolute.is_file() and not absolute.is_symlink():
                mandatory.append(candidate)
    return list(dict.fromkeys(mandatory)), list(dict.fromkeys(ranked))


def _evidence(
    workspace: Path,
    path: Path,
    *,
    terms: list[str],
    anchors: set[str],
    full_file: bool,
) -> WorkspaceContextEvidence | None:
    text = _read_text(workspace, path)
    if text is None:
        return None
    lines = text.splitlines()
    if not lines:
        return None
    if full_file:
        start, end = 1, len(lines)
        reason = "mandatory project instruction"
    else:
        start, end = _best_fragment(lines, terms, anchors)
        reason = "query-relevant workspace evidence"
        start += 1
    content = "\n".join(lines[start - 1 : end]) + "\n"
    data = text.encode("utf-8")
    fragment = content.encode("utf-8")
    path_value = path.as_posix()
    return WorkspaceContextEvidence(
        id=f"CTX-{_digest(path_value + ':' + str(start) + ':' + str(end))[:10]}",
        path=path_value,
        line_start=start,
        line_end=end,
        file_sha256=_digest_bytes(data),
        fragment_sha256=_digest_bytes(fragment),
        reason=reason,
        content=content,
    )


def _best_fragment(
    lines: list[str],
    terms: list[str],
    anchors: set[str],
) -> tuple[int, int]:
    folded = [line.casefold() for line in lines]
    matching = [
        index
        for index, line in enumerate(folded)
        if any(term in line for term in terms)
    ]
    if not matching:
        return 0, min(len(lines), MAX_FRAGMENT_LINES)
    best: tuple[int, int, int, int] | None = None
    best_bounds = (0, min(len(lines), MAX_FRAGMENT_LINES))
    for center in matching:
        start = max(0, center - MAX_FRAGMENT_LINES // 3)
        end = min(len(lines), start + MAX_FRAGMENT_LINES)
        start = max(0, end - MAX_FRAGMENT_LINES)
        window = "\n".join(folded[start:end])
        covered = {term for term in terms if term in window}
        definition_hits = sum(
            1
            for line in folded[start:end]
            for anchor in anchors
            if (
                f"def {anchor}" in line
                or f"class {anchor}" in line
                or f'command("{anchor}")' in line
                or f"command('{anchor}')" in line
            )
        )
        occurrences = sum(window.count(anchor) for anchor in anchors)
        score = (
            sum(10 if term in anchors else 1 for term in covered)
            + definition_hits * 25
            + min(occurrences, 20)
        )
        candidate = (score, definition_hits, len(covered & anchors), -start)
        if best is None or candidate > best:
            best = candidate
            best_bounds = (start, end)
    return best_bounds


def _read_text(workspace: Path, path: Path) -> str | None:
    candidate = workspace / path
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(workspace)
        if candidate.is_symlink() or not resolved.is_file():
            return None
        if resolved.stat().st_size > MAX_FILE_BYTES:
            return None
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return None


def _is_allowed_path(path: Path) -> bool:
    folded_parts = {part.casefold() for part in path.parts}
    if folded_parts.intersection(_EXCLUDED_PARTS):
        return False
    name = path.name.casefold()
    return not (
        name == ".env"
        or name.startswith(".env.")
        or name in _SENSITIVE_NAMES
        or name.endswith(_SENSITIVE_SUFFIXES)
    )


def _is_text_candidate(path: Path) -> bool:
    return path.suffix.casefold() in _TEXT_SUFFIXES or path.name in _TEXT_NAMES


def _digest(value: str) -> str:
    return _digest_bytes(value.encode("utf-8"))


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
