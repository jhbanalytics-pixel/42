"""Commit hygiene over a range of commits (W8-REL-B JP-05), scoped to paths.

The core suite checks the commits that touch this lane's own files (LANE_PATHS) and nothing else, so a branch that has other lanes
merged into it is not failed by their commits. The whole range is scanned once, by hand, by a reviewer:

    py -3.13 -m core.setup.tests.range_hygiene <base> <head>
    py -3.13 -m core.setup.tests.range_hygiene <base> <head> --paths core/setup/release core/setup/tests

Exit 0 when nothing is found, 1 otherwise; every finding is one line. A double hyphen counts only when it is prose: the form of a
command line flag (a double hyphen directly followed by a letter, not preceded by a word character or another hyphen) is allowed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AUTHOR = "Albert Meintjes|albert.meintjes@ogilvy.co.za|Albert Meintjes|albert.meintjes@ogilvy.co.za"
TOOL_NAMES = ("cl" + "aude", "anth" + "ropic", "co" + "dex", "co" + "pilot", "open" + "ai")
EM, EN = chr(0x2014), chr(0x2013)
FLAG = re.compile(r"(?<![\w-])--[A-Za-z][\w-]*")

# The files this lane creates. Commits that touch any of them are the lane's commits; shared files other lanes also change
# (plan.py, lock.py, bound_readback.py, the test map) are left out on purpose.
LANE_PATHS = (
    "core/setup/release/chain_evidence.py", "core/setup/release/jobs_only.py", "core/setup/release/jobs_run.py",
    "core/setup/release/JOBS-PASTE.ps1", "core/setup/cloudbuild.jobs.yaml", "core/setup/jobs.Dockerfile", "core/setup/deploy_jobs.py",
    "core/setup/tests/test_chain_evidence.py", "core/setup/tests/test_jobs_readback.py", "core/setup/tests/test_jobs_plan.py",
    "core/setup/tests/test_jobs_update.py", "core/setup/tests/test_jobs_paste.py", "core/setup/tests/test_jobs_hygiene.py",
    "core/setup/tests/test_jobs_checks.py", "core/setup/tests/test_jobs_prefix.py", "core/setup/tests/update_support.py",
    "core/setup/tests/jobs_world.py", "core/setup/tests/cloud_world.py", "core/setup/tests/jobs_paste_world.py",
    "core/setup/tests/range_hygiene.py",
    # the assembled release: the wiring, the packet command line and the lock tests with their support
    "core/setup/tests/test_jobs_wiring.py", "core/setup/tests/test_jobs_packet.py", "core/setup/tests/jobs_packet_world.py",
    "core/setup/tests/test_jobs_lock.py", "core/setup/tests/lock_closure.py",
)
# The folders in which an agent instruction file, a tool directory or scratch output must never be added.
LANE_FOLDERS = ("core/setup/release", "core/setup/tests")


def git(root, *args):
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=300)
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return done.stdout.decode("utf-8", "replace")


def prose_double_hyphen(text):
    return "--" in FLAG.sub("", text)


def commits(root, base, head, paths):
    """The non-merge commits of base..head that touch any of the paths (all of them when no path is given)."""
    tail = ["--", *paths] if paths else []
    return git(root, "log", "--no-merges", "--format=%H", f"{base}..{head}", *tail).split()


def identity_problems(root, shas):
    problems = []
    for sha in shas:
        row = git(root, "log", "-1", "--format=%an|%ae|%cn|%ce", sha).strip()
        if row != AUTHOR:
            problems.append(f"{sha[:10]}: author or committer is not Albert Meintjes ({row})")
    return problems


def message_problems(root, shas):
    problems = []
    for sha in shas:
        message = git(root, "log", "-1", "--format=%B", sha)
        parsed = subprocess.run(["git", "interpret-trailers", "--parse"], cwd=root, input=message.encode("utf-8"), capture_output=True).stdout
        if parsed.strip():
            problems.append(f"{sha[:10]}: the message ends in a trailer")
        if EM in message or EN in message:
            problems.append(f"{sha[:10]}: the message holds an em or en dash")
        if prose_double_hyphen(message):
            problems.append(f"{sha[:10]}: the message holds a double hyphen that is not a command line flag")
    return problems


def name_problems(root, base, head, shas, paths):
    if not shas:
        return []
    tail = ["--", *paths] if paths else []
    patch = git(root, "log", "-p", "--no-merges", f"{base}..{head}", *tail).lower()
    names = git(root, "log", "--no-merges", "--format=%D", f"{base}..{head}", *tail).lower() + git(root, "branch", "--show-current").lower()
    return [f"a tool or vendor name appears in the range: {name}" for name in TOOL_NAMES if name in patch or name in names]


def dash_problems(root, base, head, paths):
    tail = ["--", *paths] if paths else []
    diff = git(root, "diff", "--unified=0", f"{base}..{head}", *tail)
    return [f"an added line holds an em or en dash: {line[:80]}" for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++") and (EM in line or EN in line)]


def file_problems(root, base, head, folders):
    tool_dirs = "|".join(("cl" + "aude", "co" + "dex", "agents"))
    banned = re.compile(rf"(^|/)(AGENTS|{'CL' + 'AUDE'})\.md$|(^|/)\.({tool_dirs})(/|$)|\.log$|\.tmp$")
    added = git(root, "diff", "--name-only", "--diff-filter=A", f"{base}..{head}", "--", *folders).splitlines()
    return [f"a file that must not be committed was added: {name}" for name in added if banned.search(name)]


def scan(root, base, head, paths=None, folders=None):
    """Every finding over base..head. `paths` scopes the commit checks (None is the whole range); `folders` scopes the added file check."""
    shas = commits(root, base, head, paths)
    if paths is not None and not shas:
        return ["no commit of the range touches the lane's files; the range or the base is wrong"]
    return (identity_problems(root, shas) + message_problems(root, shas) + name_problems(root, base, head, shas, paths or [])
            + dash_problems(root, base, head, paths or []) + file_problems(root, base, head, folders or ["."]))


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print(__doc__)
        return 2
    base, head, rest = args[0], args[1], args[2:]
    paths = rest[1:] if rest[:1] == ["--paths"] else None
    problems = scan(ROOT, base, head, paths, paths)
    for line in problems:
        print(line)
    print(f"{len(problems)} finding(s) over {base}..{head}" + (f" for {len(paths)} path(s)" if paths else ", whole range"))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
