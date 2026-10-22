"""Audit F7D-TQ-001: a test module that defines the same test name twice keeps only the last one, so the first test's
assertions never run. No module in this folder may do that."""

import ast
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def test_no_test_module_here_defines_a_test_name_twice():
    shadowed = {}
    for path in sorted(HERE.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = Counter(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and n.name.startswith("test_"))
        twice = sorted(n for n, c in names.items() if c > 1)
        if twice:
            shadowed[path.name] = twice
    assert shadowed == {}
