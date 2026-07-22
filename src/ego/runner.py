from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from ego.models import ProcessResult
from ego.sandbox import SANDBOX_EXEC, wrap_read_only


class ProcessFailure(RuntimeError):
    pass


class ProcessTimeout(ProcessFailure):
    pass


class OutputLimitExceeded(ProcessFailure):
    pass


def reduced_environment(extra_keys: frozenset[str] = frozenset()) -> dict[str, str]:
    allowed = {
        "HOME",
        "PATH",
        "TMPDIR",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LC_ALL",
        "TERM",
    } | extra_keys
    return {key: value for key, value in os.environ.items() if key in allowed}


async def run_read_only(
    command: list[str],
    *,
    workspace: Path,
    stdin: str,
    timeout_seconds: float,
    output_limit_bytes: int,
    require_external_sandbox: bool = False,
    environment_keys: frozenset[str] = frozenset(),
) -> ProcessResult:
    wrapped = wrap_read_only(
        command,
        workspace,
        protect_user_data=require_external_sandbox,
    )
    if require_external_sandbox and wrapped[:2] != [str(SANDBOX_EXEC), "-p"]:
        raise ProcessFailure("participant requires Ego's external Seatbelt boundary")
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *wrapped,
        cwd=workspace,
        env=reduced_environment(environment_keys),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    output_size = [0]
    limit_exceeded = asyncio.Event()

    async def read_capped(stream: asyncio.StreamReader) -> bytes:
        captured = bytearray()
        while chunk := await stream.read(64 * 1024):
            remaining = output_limit_bytes - output_size[0]
            output_size[0] += len(chunk)
            if remaining > 0:
                captured.extend(chunk[:remaining])
            if output_size[0] > output_limit_bytes:
                limit_exceeded.set()
                break
        return bytes(captured)

    stdout_task = asyncio.create_task(read_capped(process.stdout))
    stderr_task = asyncio.create_task(read_capped(process.stderr))
    wait_task = asyncio.create_task(process.wait())
    limit_task = asyncio.create_task(limit_exceeded.wait())
    try:
        process.stdin.write(stdin.encode())
        await process.stdin.drain()
        process.stdin.close()
        async with asyncio.timeout(timeout_seconds):
            done, _ = await asyncio.wait(
                {wait_task, limit_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if limit_task in done and limit_exceeded.is_set():
                process.kill()
                await process.wait()
                raise OutputLimitExceeded(
                    f"process exceeded the {output_limit_bytes} byte output limit"
                )
            await wait_task
            stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    except TimeoutError as error:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.kill()
            await process.wait()
        raise ProcessTimeout(f"process exceeded {timeout_seconds:g}s") from error
    finally:
        for task in (stdout_task, stderr_task, wait_task, limit_task):
            if not task.done():
                task.cancel()
    return ProcessResult(
        command=command,
        returncode=process.returncode or 0,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
        duration_seconds=time.monotonic() - started,
    )
