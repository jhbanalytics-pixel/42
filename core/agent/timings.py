"""The Ask timing record, stored at run.timings in the runs row's record JSON (no DDL change).

Version 1: total_s (the run's seconds), spans (at most MAX_SPANS, each n, kind, name, t0, t1 and attrs) and dropped (the
spans not stored once the cap was reached). t0 and t1 are monotonic seconds from the run's start. Every string in a
record is a member of the fixed enums below and attrs hold numbers and those strings only, so no question, query, post
or model text can enter it. A recorder never raises into the ask: any failure inside it marks the record failed and the
ask goes on, with every check still running.
"""

from __future__ import annotations

import math
import threading
import time

VERSION = 1
MAX_SPANS = 400
PHASE_RESERVE = 40  # slots only phase spans may use, so the cap never leaves the run uncovered

KINDS = ("phase", "model_call", "tool_call", "check", "io")
PHASES = ("setup", "plan", "research", "store_count", "prepare", "write", "numeric_repair", "checks", "finalise")
PURPOSES = ("research_turn", "write", "numeric_repair", "support", "field_check", "headline_rewrite", "critic", "other")
STATUSES = ("ok", "busy", "timeout", "error", "refused")
IO_NAMES = ("spend_read", "trending_snapshot", "creator_discovery", "opening_lookups", "store_count",
            "store_count_background", "fetch_posts", "native_review", "claim_checks_write")
CHECK_NAMES = ("code_checks", "support_checks", "recheck_fields", "field_checks", "critic_review")
RULES = ("all", "K4", "K10", "fields", "critic")
OTHER = "other"

# Attribute keys that hold a number. Anything not named here or in ENUM_ATTRS is dropped.
NUMBER_ATTRS = ("attempt", "input", "output", "thinking", "cached", "wait_s", "batch", "lane", "rows", "bytes",
                "claims", "pass", "downgrade", "cut", "reserved")
ENUM_ATTRS = ("purpose", "model", "status", "tool", "rule")


def _tools() -> tuple:
    from core.agent.toolset import TOOL_NAMES

    return tuple(TOOL_NAMES)


def _models() -> tuple:
    from core.llm.provider import GEMINI_LIST_PRICES

    return tuple(GEMINI_LIST_PRICES)


def _enums() -> dict:
    return {"purpose": PURPOSES, "model": (*_models(), OTHER), "status": STATUSES, "tool": (*_tools(), OTHER),
            "rule": RULES}


def _names() -> dict:
    return {"phase": PHASES, "model_call": PURPOSES, "tool_call": (*_tools(), OTHER), "check": CHECK_NAMES,
            "io": IO_NAMES}


def _all_strings() -> frozenset:
    found = set(KINDS) | {OTHER}
    for group in (*_enums().values(), *_names().values()):
        found.update(group)
    return frozenset(found)


ENUM_VALUES = _all_strings()
KEYS = frozenset({"version", "total_s", "spans", "dropped", "failed", "n", "kind", "name", "t0", "t1", "attrs",
                  *NUMBER_ATTRS, *ENUM_ATTRS})


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return round(value, 3) if isinstance(value, float) else value


def _clean(attrs: dict) -> dict:
    enums = _enums()
    out = {}
    for key, value in attrs.items():
        if key in ENUM_ATTRS:
            out[key] = value if isinstance(value, str) and value in enums[key] else OTHER
        elif key in NUMBER_ATTRS:
            number = _number(value)
            if number is not None:
                out[key] = number
    return out


def token_attrs(usage) -> dict:
    """The token attrs of a usage dict as the budget ledger sees it: output includes thinking, cached is part of input."""
    if not isinstance(usage, dict):
        return {}

    def count(key):
        value = usage.get(key)
        return value if type(value) is int and value >= 0 else 0

    tokens_in = count("input_tokens")
    return {"input": tokens_in, "output": count("output_tokens"), "thinking": count("thinking_tokens"),
            "cached": min(count("cached_tokens"), tokens_in)}


def failure_status(exc: BaseException) -> str:
    """How a failed model call ends in the record: busy (a 429 refused before running), timeout or error."""
    code = getattr(exc, "code", None)
    name = type(exc).__name__.lower()
    if type(code) is int and code == 429 and getattr(exc, "usage", None) is None:
        return "busy"
    if isinstance(exc, TimeoutError) or "timeout" in name or "deadline" in name or code in (408, 504):
        return "timeout"
    return "error"


class Handle:
    """One open span. end() closes it once; set() adds attrs, also after the end."""

    def __init__(self, recorder, span):
        self._recorder = recorder
        self._span = span

    def set(self, **attrs):
        self._recorder._guarded(self._recorder._annotate, self._span, attrs)
        return self

    def end(self, **attrs):
        self._recorder._guarded(self._recorder._close, self._span, attrs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._span is not None and self._span["t1"] is None:
            self.end(**({"status": "error"} if exc_type is not None and self._span["kind"] in ("model_call", "tool_call")
                        and "status" not in self._span["attrs"] else {}))
        return False


class Timings:
    def __init__(self, started: float | None = None):
        self.started = time.monotonic() if started is None else started
        self.broken = False
        self._lock = threading.Lock()
        self._spans: list[dict] = []
        self._count = 0
        self._dropped = 0
        self._batches = 0
        self._phase: dict | None = None
        self.phase("setup")

    def _now(self) -> float:
        return round(time.monotonic() - self.started, 3)

    def _guarded(self, function, *args):
        try:
            return function(*args)
        except Exception:
            self.broken = True
            return None

    def _open(self, kind: str, name: str, attrs: dict, t0: float | None = None) -> dict | None:
        names = _names()
        if kind not in names:
            kind = "io"
        name = name if name in names[kind] else OTHER
        t0 = self._now() if t0 is None else t0
        with self._lock:
            phases = sum(1 for s in self._spans if s["kind"] == "phase")
            others = len(self._spans) - phases
            if kind == "phase" and phases >= PHASE_RESERVE or kind != "phase" and others >= MAX_SPANS - PHASE_RESERVE:
                self._dropped += 1
                return None
            self._count += 1
            span = {"n": self._count, "kind": kind, "name": name, "t0": t0, "t1": None, "attrs": _clean(attrs)}
            self._spans.append(span)
            return span

    def _close(self, span: dict | None, attrs: dict, t1: float | None = None) -> None:
        if span is None:
            return
        t1 = self._now() if t1 is None else t1
        with self._lock:
            span["attrs"].update(_clean(attrs))
            if span["t1"] is None:
                span["t1"] = max(t1, span["t0"])

    def _annotate(self, span: dict | None, attrs: dict) -> None:
        if span is not None:
            with self._lock:
                span["attrs"].update(_clean(attrs))

    def begin(self, kind: str, name: str, **attrs) -> Handle:
        return Handle(self, self._guarded(self._open, kind, name, attrs))

    def span(self, kind: str, name: str, **attrs) -> Handle:
        return self.begin(kind, name, **attrs)

    def phase(self, name: str) -> None:
        """Close the open phase and open the next, so the phases tile the run."""
        try:
            if name not in PHASES:
                name = OTHER
            current = self._phase
            if current is not None and current["name"] == name:
                return
            at = self._now()
            if current is not None:
                self._close(current, {}, at)
            self._phase = self._open("phase", name, {}, at)
        except Exception:
            self.broken = True

    @property
    def phase_name(self) -> str | None:
        current = self._phase
        return current["name"] if current else None

    def next_batch(self) -> int:
        with self._lock:
            self._batches += 1
            return self._batches

    def _snapshot(self, elapsed: float) -> dict:
        end = round(elapsed, 3)
        with self._lock:
            spans = [{**s, "attrs": dict(s["attrs"]), "t1": end if s["t1"] is None else s["t1"]}
                     for s in sorted(self._spans, key=lambda s: s["n"])]
            dropped = self._dropped
        record = {"version": VERSION, "total_s": round(elapsed, 1), "spans": spans, "dropped": dropped}
        if self.broken:
            record["failed"] = True
        return record

    def snapshot(self, elapsed: float) -> dict:
        """The record so far, the open phase closed at elapsed seconds. Idempotent, and never raises."""
        try:
            return self._snapshot(elapsed)
        except Exception:
            self.broken = True
            return {"version": VERSION, "total_s": round(elapsed, 1), "spans": [], "dropped": 0, "failed": True}


class _NullHandle:
    def set(self, **attrs):
        return self

    def end(self, **attrs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Null:
    """The recorder a context without one gets: every call does nothing."""

    broken = False
    phase_name = None

    def begin(self, kind, name, **attrs):
        return _NullHandle()

    span = begin

    def phase(self, name):
        return None

    def next_batch(self):
        return 0


NULL = _Null()


def of(ctx):
    """The recorder on a run context, or the null recorder."""
    return getattr(ctx, "timings", None) or NULL


def read(run) -> dict | None:
    """run.timings when the run carries a version 1 record; None for a legacy row or anything else."""
    record = run.get("timings") if isinstance(run, dict) else None
    if isinstance(record, dict) and record.get("version") == VERSION and isinstance(record.get("spans"), list):
        return record
    return None
