"""The synthetic bridge worlds of the loader tests, moved whole to a later cutoff day.

The loader fixtures build one unissued capture at cutoff 2026-09-20. This helper reads
those fixture modules' own source, moves every September 2026 date they spell by a fixed
number of days, and executes the result as separate modules, so a capture at another
cutoff is built by the same code with every digest recomputed over the moved values. The
original modules are never changed or reloaded. Nothing here grants any authority.
"""

import importlib.util
import re
import sys
import types
from datetime import date, timedelta

# Fixture modules, in import order; each later one may import the earlier ones.
GROUP = (
    "test_source_estate_bridge_contract",
    "test_daily_product_completion",
    "test_source_estate_bridge_plan",
    "test_bridge_history_loader",
    "bridge_ledger_fixture",
    "test_bridge_ledger_route",
)
_ISO = re.compile(r"2026-09-(\d{2})")
_COMPACT = re.compile(r"202609(\d{2})")
_CALL = re.compile(r"(datetime|date)\(2026, 9, (\d{1,2})")


def _day(value, days):
    return date(2026, 9, int(value)) + timedelta(days=days)


def shifted_source(source, days):
    """The source with every September 2026 date it spells moved by ``days``."""
    source = _ISO.sub(lambda match: _day(match.group(1), days).isoformat(), source)
    source = _COMPACT.sub(lambda match: f"{_day(match.group(1), days):%Y%m%d}", source)
    return _CALL.sub(
        lambda match: (
            f"{match.group(1)}({_day(match.group(2), days).year}, "
            f"{_day(match.group(2), days).month}, {_day(match.group(2), days).day}"
        ),
        source,
    )


def shifted_world(days):
    """A package of the fixture modules with every date moved by ``days``."""
    package = f"tests.unit._bridge_shift_{days}"
    if package in sys.modules:
        return sys.modules[package]
    root = types.ModuleType(package)
    root.__path__ = []
    sys.modules[package] = root
    for name in GROUP:
        spec = importlib.util.find_spec(f"tests.unit.{name}")
        with open(spec.origin, encoding="utf-8") as handle:
            source = shifted_source(handle.read(), days)
        for other in GROUP:
            source = source.replace(f"tests.unit.{other} ", f"{package}.{other} ")
            source = source.replace(f"tests.unit.{other}\n", f"{package}.{other}\n")
            source = source.replace(
                f"from tests.unit import {other}", f"from {package} import {other}"
            )
        module = types.ModuleType(f"{package}.{name}")
        module.__file__ = spec.origin
        sys.modules[module.__name__] = module
        exec(compile(source, spec.origin, "exec"), module.__dict__)
        setattr(root, name, module)
    return root
