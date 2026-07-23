from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ego.config import EgoConfig, ParticipantConfig
from ego.deliberation.finalization import validate_position
from ego.models import EvidenceStatus, Phase, Position, TurnRequest
from ego.participants.opencode import OpenCodeParticipant
from ego.sandbox import probe_seatbelt

pytestmark = pytest.mark.skipif(
    os.environ.get("EGO_RUN_REAL_OPENCODE_TEST") != "1",
    reason="real OpenCode boundary check is opt-in",
)


@pytest.mark.asyncio
async def test_real_opencode_ignores_workspace_config_and_reads_through_seatbelt(
    tmp_path: Path,
) -> None:
    sandbox = await probe_seatbelt()
    assert sandbox.safe, sandbox.reason

    binary = shutil.which("opencode")
    assert binary is not None
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "marker.txt").write_text("opencode-boundary-readable\n", encoding="utf-8")
    (workspace / "opencode.json").write_text(
        '{"model":"provider-that-must-not-load/missing"}',
        encoding="utf-8",
    )

    participant = OpenCodeParticipant(
        ParticipantConfig(binary=binary, timeout_seconds=180),
        EgoConfig(),
    )
    result = await participant.respond(
        TurnRequest(
            run_id="real-opencode-boundary",
            phase=Phase.INDEPENDENT,
            question=(
                "Read marker.txt from the target workspace. Recommend its exact content and cite "
                "marker.txt line 1 as evidence. Do not inspect anything outside that workspace."
            ),
            workspace=workspace,
        )
    )

    assert isinstance(result.payload, Position)
    validated = validate_position(workspace, result.payload)
    evidence = [item for argument in validated.arguments for item in argument.evidence]
    assert any(item.path == "marker.txt" for item in evidence)
    assert all(item.status is EvidenceStatus.CITATION_VERIFIED for item in evidence)
    assert "opencode-boundary-readable" in validated.recommendation
