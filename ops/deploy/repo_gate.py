import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

_HEAD_RE = re.compile(r"^[0-9a-f]{40}$")


def _git(root: Path, operation: str, *args: str, allow_failure: bool = False) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"git_command_failed:{operation}") from error
    try:
        stdout = completed.stdout.decode("utf-8", errors="strict").strip()
        stderr = completed.stderr.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as error:
        raise ValueError(f"git_output_not_utf8:{operation}") from error
    if completed.returncode and not allow_failure:
        detail = stderr.splitlines()[-1] if stderr else f"exit_{completed.returncode}"
        raise ValueError(f"git_command_failed:{operation}:{detail}")
    return stdout if completed.returncode == 0 else ""


def _normalized_remote(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/")
        if path.lower().endswith(".git"):
            path = path[:-4]
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))
    path = Path(value)
    if path.suffix.lower() == ".git":
        path = path.with_suffix("")
    return path.resolve().as_posix().casefold()


def validate_repo(
    root: Path,
    expected_remote: str,
    expected_branch: str,
    require_clean: bool,
) -> dict:
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("repository_missing")

    top_level = _git(root, "top_level", "rev-parse", "--show-toplevel")
    if not top_level or Path(top_level).resolve() != root:
        raise ValueError("repository_not_top_level")

    branch = _git(
        root,
        "branch",
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        allow_failure=True,
    )
    if not branch:
        raise ValueError("detached_head")
    if branch != expected_branch:
        raise ValueError(f"branch_mismatch:{branch}")

    head = _git(
        root,
        "head",
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        allow_failure=True,
    )
    if not _HEAD_RE.fullmatch(head):
        raise ValueError("invalid_head")

    if _git(root, "unmerged", "ls-files", "-u"):
        raise ValueError("repository_unmerged")

    remote = _git(root, "remote", "config", "--get", "remote.origin.url")
    if not remote or _normalized_remote(remote) != _normalized_remote(expected_remote):
        raise ValueError("remote_mismatch")

    upstream = _git(
        root,
        "upstream",
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
        allow_failure=True,
    )
    if not upstream:
        raise ValueError("upstream_missing")
    expected_upstream = f"origin/{expected_branch}"
    if upstream != expected_upstream:
        raise ValueError(f"upstream_mismatch:{upstream}")

    remote_head = _git(
        root,
        "remote_head",
        "rev-parse",
        "--verify",
        f"refs/remotes/{expected_upstream}^{{commit}}",
        allow_failure=True,
    )
    if not _HEAD_RE.fullmatch(remote_head):
        raise ValueError("remote_branch_missing")
    if head != remote_head:
        raise ValueError("head_mismatch")

    status = _git(root, "status", "status", "--porcelain", "--untracked-files=all")
    if require_clean and status:
        raise ValueError("repository_dirty")
    return {
        "head": head,
        "remote_head": remote_head,
        "branch": branch,
        "status": "clean" if not status else "dirty",
    }
