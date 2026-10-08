"""What the release tests need from the machine, found by an explicit order rather than by whatever happens to be first on PATH.

The deploy-script tests derived bash from the directory of whichever git came first on PATH, and failed all eight tests when
that git was a mingw64 shim (W8-REL F7). resolve_bash states the order: F42_BASH, then bash on PATH on Linux, then Git for Windows'
own bash under %LOCALAPPDATA%. A prerequisite that is missing fails the tests, with a message; it never skips them.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

A80 = "a80be1dee7f4ee2aa9775d0f353bab930809f057"


def resolve_bash(env=None, platform=None, which=shutil.which):
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    explicit = env.get("F42_BASH")
    if explicit:
        if not Path(explicit).is_file():
            raise FileNotFoundError(f"F42_BASH names a file that does not exist: {explicit}")
        return Path(explicit)
    if not platform.startswith("win"):
        found = which("bash")
        if found:
            return Path(found)
        raise FileNotFoundError("bash is not on PATH; set F42_BASH")
    local = env.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Programs" / "Git" / "bin" / "bash.exe"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(r"Git for Windows' bash.exe was not found under %LOCALAPPDATA%\Programs\Git\bin; set F42_BASH")


def prerequisites(repo, env=None, which=shutil.which):
    """The missing prerequisites of the deploy partition, by name: pwsh, bash, and the a80 commit in full history."""
    import subprocess

    missing = []
    if which("pwsh") is None:
        missing.append("pwsh")
    try:
        resolve_bash(env)
    except FileNotFoundError:
        missing.append("bash")
    if subprocess.run(["git", "cat-file", "-e", A80], cwd=repo, capture_output=True).returncode != 0:
        missing.append(f"commit {A80}")
    return missing
