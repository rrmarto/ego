from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ego.config import EgoConfig, ParticipantConfig
from ego.models import EvidenceStatus, Phase, Position, TurnRequest
from ego.participants.base import CliParticipant
from ego.sandbox import probe_seatbelt


class SyntheticParticipant(CliParticipant):
    participant_id = "synthetic"
    default_binary = "synthetic-ego-cli"
    required_help_tokens = ("--structured",)

    def command(self, binary: str, schema: dict[str, object], request: TurnRequest) -> list[str]:
        del schema, request
        return [binary]


@pytest.mark.asyncio
async def test_real_participant_boundary_reads_but_cannot_write(tmp_path: Path) -> None:
    sandbox = await probe_seatbelt()
    if not sandbox.safe and "Operation not permitted" in sandbox.reason:
        pytest.skip("the parent Codex sandbox forbids nested Seatbelt profiles")
    assert sandbox.safe, sandbox.reason

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("readable evidence\n", encoding="utf-8")
    script = tmp_path / "synthetic_cli.py"
    script.write_text(
        f"""#!{sys.executable}
import json
import pathlib
import sys

if '--version' in sys.argv:
    print('synthetic 1.0')
    raise SystemExit(0)
if '--help' in sys.argv:
    print('--structured')
    raise SystemExit(0)

prompt = sys.stdin.read()
assert 'decision-only deliberation engine' in prompt
assert pathlib.Path('source.txt').read_text() == 'readable evidence\\n'
try:
    pathlib.Path('forbidden.txt').write_text('must fail')
except OSError:
    pass
else:
    raise SystemExit(9)

print(json.dumps({{
    'recommendation': 'Use the readable evidence.',
    'arguments': [{{
        'id': 'source',
        'claim': 'The file is readable.',
        'evidence': [{{
            'path': 'source.txt',
            'line_start': 1,
            'line_end': 1,
            'explanation': 'Direct synthetic evidence.'
        }}]
    }}],
    'confidence': 'low',
    'confidence_reason': 'Synthetic boundary verification.'
}}))
""",
        encoding="utf-8",
    )
    script.chmod(0o755)

    participant = SyntheticParticipant(
        ParticipantConfig(binary=str(script)),
        EgoConfig(),
    )
    availability = await participant.probe()
    assert availability.status.value == "available", availability.reason
    result = await participant.respond(
        TurnRequest(
            run_id="synthetic-run",
            phase=Phase.INDEPENDENT,
            question="Can the source be read?",
            workspace=workspace,
        )
    )
    assert isinstance(result.payload, Position)
    assert not (workspace / "forbidden.txt").exists()
    # The harness performs citation validation after the adapter returns.
    assert result.payload.arguments[0].evidence[0].status is EvidenceStatus.UNVALIDATED
