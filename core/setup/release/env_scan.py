"""The environment read scan (W8-REL-B v2.1 section 3.5, JB-06).

    py -3.13 -m core.setup.release.env_scan --base-rev a80be1d --head-rev <release commit> [--baseline-env-names FILE]
    py -3.13 -m core.setup.release.env_scan --base-dir DIR --head-dir DIR   (each holds a core/ folder)

The jobs image is updated with `jobs update --image` and nothing else, so B's job code must read no setting the live job
definitions lack. This scan lists every environment read under core/ (tests and core/setup/release excluded) that the release
commit has and a80 did not, and fails any whose name is not in one of four classes:

    platform injected   CLOUD_RUN_*
    image baked         IMAGE_BAKED, each set by an ENV line of core/setup/jobs.Dockerfile
    process local       a setdefault, which only fills the process's own environment
    baseline-J          a name in the env name list of the 14 job definitions captured in baseline-J

A read is os.environ.get / .pop / [...] / `in`, os.getenv and environ.get / .setdefault. A name that is not a literal is
<dynamic>. A whole mapping taken from the environment (`env = os.environ`) is judged by the literal names its function reads
with .get or [...]; with none it is <mapping>. A read of a name a80's file already made is not new. Every finding carries the
file and line. Exit 0 when there is none, 1 when there is any.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core.setup import durable_effects_check as de  # noqa: E402

PLATFORM = re.compile(r"CLOUD_RUN_[A-Z0-9_]+\Z")
IMAGE_BAKED = frozenset({"F42_GIT_SHA"})
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,}\Z")
RELEASE_TOOLING = "core/setup/release/"


@dataclass(frozen=True)
class Read:
    path: str
    line: int
    name: str
    kind: str  # get, getenv, pop, subscript, in, setdefault, mapping

    def __str__(self):
        return f"{self.path}:{self.line}: {self.name} ({self.kind})"


@dataclass(frozen=True)
class Finding(Read):
    reason: str = ""

    def __str__(self):
        return f"{self.path}:{self.line}: {self.name} is read and is not platform injected, image baked, a setdefault or in baseline-J"


def in_scope(path):
    return path.endswith(".py") and not de.is_test_path(path) and not path.startswith(RELEASE_TOOLING)


def _literal(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_environ(node):
    return ast.unparse(node).endswith("environ")


def _parents(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing(node, parents):
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            return node
    return node


def _named_reads(scope):
    """The literal upper case names read with .get("X") or ["X"] anywhere in scope: what a whole mapping may be asked."""
    found = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            name = _literal(node.args[0])
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            name = _literal(node.slice)
        else:
            continue
        if name and ENV_NAME.match(name):
            found.append((name, node.lineno))
    return found


def file_reads(path, text):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [Read(path, 1, "<unparsed>", "mapping")]
    parents = _parents(tree)
    reads, covered = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if func.attr in ("get", "pop", "setdefault") and _is_environ(func.value) or func.attr == "getenv" and ast.unparse(func.value) == "os":
                name = _literal(node.args[0]) if node.args else None
                reads.append(Read(path, node.lineno, name or "<dynamic>", func.attr if func.attr != "getenv" else "getenv"))
                covered.add(func.value)
        elif isinstance(node, ast.Subscript) and _is_environ(node.value):
            covered.add(node.value)
            if isinstance(node.ctx, ast.Load):
                reads.append(Read(path, node.lineno, _literal(node.slice) or "<dynamic>", "subscript"))
        elif isinstance(node, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
            for comparator in node.comparators:
                if _is_environ(comparator):
                    covered.add(comparator)
                    reads.append(Read(path, node.lineno, _literal(node.left) or "<dynamic>", "in"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "environ" and node not in covered and _is_environ(node):
            named = _named_reads(_enclosing(node, parents))
            if named:
                reads += [Read(path, line, name, "mapping") for name, line in named]
            else:
                reads.append(Read(path, node.lineno, "<mapping>", "mapping"))
    return sorted(set(reads), key=lambda r: (r.path, r.line, r.name, r.kind))


def tree_reads(files):
    out = []
    for path, text in files:
        if in_scope(path):
            out += file_reads(path, text)
    return out


def added_reads(a80_files, head_files):
    """Every read the release tree has and a80's tree did not: a named read whose name a80's same file never read, and for a
    name that is not a literal, the reads beyond the count a80's file had."""
    base = tree_reads(a80_files)
    seen = {(r.path, r.name) for r in base if not r.name.startswith("<")}
    counts = {}
    for r in base:
        if r.name.startswith("<"):
            counts[(r.path, r.name)] = counts.get((r.path, r.name), 0) + 1
    added, dynamic = [], {}
    for r in tree_reads(head_files):
        if not r.name.startswith("<"):
            if (r.path, r.name) not in seen:
                added.append(r)
        else:
            dynamic.setdefault((r.path, r.name), []).append(r)
    for key, sites in dynamic.items():
        extra = len(sites) - counts.get(key, 0)
        if extra > 0:
            added += sorted(sites, key=lambda r: r.line)[-extra:]
    return sorted(added, key=lambda r: (r.path, r.line, r.name))


def classified(read, baseline_names):
    return read.kind == "setdefault" or bool(PLATFORM.match(read.name)) or read.name in IMAGE_BAKED or read.name in baseline_names


def scan(a80_files, head_files, baseline_names):
    names = set(baseline_names)
    return [Finding(r.path, r.line, r.name, r.kind) for r in added_reads(a80_files, head_files) if not classified(r, names)]


def tree_from_disk(core_dir, root):
    files = []
    for path in sorted(Path(core_dir).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            files.append((path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scan the environment reads a release commit adds under core/.")
    parser.add_argument("--base-rev", help="the a80 commit")
    parser.add_argument("--head-rev", help="the release commit")
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--head-dir", type=Path)
    parser.add_argument("--baseline-env-names", type=Path, help="a text file, one env name per line, from baseline-J")
    args = parser.parse_args(argv)
    if args.base_dir and args.head_dir:
        base, head = (tree_from_disk(d / "core", d) for d in (args.base_dir, args.head_dir))
    elif args.base_rev and args.head_rev:
        base, head = (de.git_tree_files(rev, "core") for rev in (args.base_rev, args.head_rev))
    else:
        parser.error("give --base-rev and --head-rev, or --base-dir and --head-dir")
    names = set()
    if args.baseline_env_names:
        names = {ln.strip() for ln in args.baseline_env_names.read_text(encoding="utf-8").splitlines() if ln.strip()}
    findings = scan(base, head, names)
    for finding in findings:
        print(finding)
    print(f"{len(findings)} environment read(s) outside the four classes")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
