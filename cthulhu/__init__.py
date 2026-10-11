"""Cthulhu harness release identity (read-only source revision discovery)."""
from pathlib import Path
import subprocess

try:
    _root = str(Path(__file__).resolve().parent.parent)
    _revision = subprocess.run(["git", "-c", f"safe.directory={_root}", "rev-parse", "--short=12", "HEAD"],
                               cwd=_root, capture_output=True, text=True, timeout=2, check=True).stdout.strip()
except (OSError, subprocess.SubprocessError):
    _revision = "unknown"
BUILD = "galaxy-api-v1@" + _revision
