from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PBCOPY = Path("/usr/bin/pbcopy")


def copy_to_macos_clipboard(text: str) -> bool | None:
    if sys.platform != "darwin" or not PBCOPY.is_file():
        return None
    try:
        result = subprocess.run(
            [str(PBCOPY)],
            input=text,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
