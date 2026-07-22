from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ego.config import EgoConfig, ParticipantConfig
from ego.deliberation.finalization import validate_position
from ego.models import EvidenceStatus, Phase, Position, TurnRequest
from ego.participants.codex import CodexParticipant
from ego.sandbox import probe_seatbelt

pytestmark = pytest.mark.skipif(
    os.environ.get("EGO_RUN_REAL_CODEX_TEST") != "1",
    reason="real Codex boundary check is opt-in",
)


@pytest.mark.asyncio
async def test_real_codex_reads_through_ego_external_sandbox(tmp_path: Path) -> None:
    sandbox = await probe_seatbelt()
    assert sandbox.safe, sandbox.reason

    binary = shutil.which("codex")
    assert binary is not None
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "marker.txt").write_text("external-boundary-readable\n", encoding="utf-8")

    participant = CodexParticipant(
        ParticipantConfig(binary=binary, timeout_seconds=180),
        EgoConfig(),
    )
    result = await participant.respond(
        TurnRequest(
            run_id="real-codex-boundary",
            phase=Phase.INDEPENDENT,
            question=(
                "Read marker.txt. Recommend its exact content and cite marker.txt line 1 as "
                "evidence. Do not inspect anything outside this workspace."
            ),
            workspace=workspace,
        )
    )

    assert isinstance(result.payload, Position)
    validated = validate_position(workspace, result.payload)
    evidence = [item for argument in validated.arguments for item in argument.evidence]
    assert any(item.path == "marker.txt" for item in evidence)
    assert all(item.status is EvidenceStatus.CITATION_VERIFIED for item in evidence)
    assert "external-boundary-readable" in validated.recommendation
