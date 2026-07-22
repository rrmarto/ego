from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from ego.models import Evidence, EvidenceStatus


@dataclass(frozen=True)
class GitObservation:
    head: str | None
    status: str | None


def resolve_workspace(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"Workspace is not a directory: {resolved}")
    return resolved


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_evidence(workspace: Path, evidence: Evidence) -> Evidence:
    try:
        candidate = (workspace / evidence.path).resolve(strict=True)
        candidate.relative_to(workspace)
        if not candidate.is_file():
            raise ValueError("evidence path is not a file")
        data = candidate.read_bytes()
        text = data.decode("utf-8")
        lines = text.splitlines(keepends=True)
        if evidence.line_end < evidence.line_start:
            raise ValueError("line_end precedes line_start")
        if evidence.line_end > len(lines):
            raise ValueError(f"file has only {len(lines)} lines")
        fragment = "".join(lines[evidence.line_start - 1 : evidence.line_end]).encode()
        return evidence.model_copy(
            update={
                "file_sha256": _sha256(data),
                "fragment_sha256": _sha256(fragment),
                "status": EvidenceStatus.CITATION_VERIFIED,
                "validation_error": None,
            }
        )
    except (OSError, UnicodeError, ValueError) as error:
        return evidence.model_copy(
            update={"status": EvidenceStatus.INVALID, "validation_error": str(error)}
        )


def revalidate_evidence(workspace: Path, evidence: Evidence) -> Evidence:
    current = validate_evidence(workspace, evidence)
    if current.status is not EvidenceStatus.CITATION_VERIFIED:
        return current
    if evidence.file_sha256 and evidence.file_sha256 != current.file_sha256:
        return current.model_copy(
            update={"status": EvidenceStatus.STALE, "validation_error": "source file changed"}
        )
    return current


async def observe_git(workspace: Path) -> GitObservation:
    async def run(*args: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/git",
                "-C",
                str(workspace),
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            if process.returncode != 0:
                return None
            return stdout.decode(errors="replace").strip()
        except OSError:
            return None

    return GitObservation(
        head=await run("rev-parse", "HEAD"), status=await run("status", "--porcelain")
    )
