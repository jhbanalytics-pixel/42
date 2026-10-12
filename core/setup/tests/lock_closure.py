"""Which files a release command executes, found from the source and not typed: the first party import closure of an entry script,
and the test sources that carry the nodes of a release prefix. The lock tests hold REPO_FILES and TEST_FILES to these."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIRST_PARTY = "core"


def module_path(name, root=ROOT):
    base = Path(root).joinpath(*name.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def imported_modules(path, root=ROOT):
    """Every first party module a file imports anywhere in its text, including an import inside a function."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    package = Path(path).relative_to(root).with_suffix("").parts[:-1]
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = ".".join(list(package[:len(package) - (node.level - 1)]) + ([node.module] if node.module else [])) if node.level else (node.module or "")
            found.add(base)
            found |= {f"{base}.{alias.name}" for alias in node.names}
    return {name for name in found if name == FIRST_PARTY or name.startswith(FIRST_PARTY + ".")}


def closure(entries, root=ROOT):
    """The repository paths of the entries and of every first party module they reach, sorted."""
    seen, todo = set(), []
    for entry in entries:
        path = Path(root) / entry if entry.endswith(".py") else module_path(entry, root)
        todo.append(path)
    while todo:
        path = todo.pop()
        if path is None or path in seen:
            continue
        seen.add(path)
        # Python runs the __init__ of every package above a module it imports.
        for parent in Path(path).relative_to(root).parents:
            init = Path(root) / parent / "__init__.py"
            if str(parent) != "." and init.is_file():
                todo.append(init)
        for name in imported_modules(path, root):
            todo.append(module_path(name, root))
    return sorted(p.relative_to(root).as_posix() for p in seen)


def script_paths(ps1_text):
    """The core/....py scripts and the -m modules a paste starts, read from its text."""
    scripts = set(re.findall(r"core/[A-Za-z0-9_/]+\.py", ps1_text))
    modules = set(re.findall(r"'-m', '(core\.[A-Za-z0-9_.]+)'", ps1_text))
    return scripts, modules


def test_files_with_prefix_nodes(prefixes, root=ROOT):
    """The test sources under core/ that define a node named test_<prefix><two digits>_..., by AST."""
    pattern = re.compile(r"^test_(" + "|".join(prefixes) + r")\d\d(?:_|$)")
    found = set()
    for path in sorted((Path(root) / "core").rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and pattern.match(n.name) for n in ast.walk(tree)):
            found.add(path.relative_to(root).as_posix())
    return sorted(found)


def tests_support_closure(test_files, root=ROOT):
    """The test sources a set of test files import from core/**/tests, which a lock of the tests must hold as well."""
    return [p for p in closure(test_files, root) if "/tests/" in p and not Path(p).name.startswith("test_")]
