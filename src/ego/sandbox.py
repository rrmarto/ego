from __future__ import annotations

import asyncio
import json
import platform
import tempfile
from dataclasses import dataclass
from pathlib import Path

SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")


@dataclass(frozen=True)
class SandboxProbe:
    safe: bool
    reason: str


def seatbelt_profile(workspace: Path) -> str:
    quoted = json.dumps(str(workspace.resolve()))
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            f"(deny file-write* (literal {quoted}) (subpath {quoted}))",
        ]
    )


def wrap_read_only(command: list[str], workspace: Path) -> list[str]:
    return [str(SANDBOX_EXEC), "-p", seatbelt_profile(workspace), *command]


async def probe_seatbelt() -> SandboxProbe:
    if platform.system() != "Darwin":
        return SandboxProbe(False, "Ego v1 supports read-only enforcement on macOS only")
    if not SANDBOX_EXEC.exists():  # noqa: ASYNC240 - one local metadata check
        return SandboxProbe(False, "/usr/bin/sandbox-exec is missing")

    with tempfile.TemporaryDirectory(prefix="ego-seatbelt-") as directory:
        root = Path(directory).resolve()  # noqa: ASYNC240 - tempfile setup is local
        readable = root / "readable.txt"
        readable.write_text("ego", encoding="utf-8")
        read_process = await asyncio.create_subprocess_exec(
            *wrap_read_only(["/bin/cat", str(readable)], root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, read_stderr = await read_process.communicate()
        if read_process.returncode != 0 or stdout != b"ego":
            detail = read_stderr.decode(errors="replace").strip()
            return SandboxProbe(False, f"Seatbelt read probe failed: {detail}")

        target = root / "forbidden.txt"
        write_process = await asyncio.create_subprocess_exec(
            *wrap_read_only(["/usr/bin/touch", str(target)], root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, write_stderr = await write_process.communicate()
        if write_process.returncode == 0 or target.exists():
            return SandboxProbe(False, "Seatbelt allowed a workspace write")
        detail = write_stderr.decode(errors="replace").strip()
        return SandboxProbe(True, detail or "read allowed and write denied")
