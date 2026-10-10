"""Helpers the smoke tests share: the wire value f42-api returns under answer_meta (C1 5.1), and a client that adds a
verified one to the final ask record, the way lane 1's fixture agent will (C1 P-19)."""
import httpx


def wire(summary="shown", *, removals=(), rewrite="not_attempted", execution="completed", stop_reason=None):
    """A verified wire value."""
    return {"check": "verified", "v": 1, "execution": {"state": execution, "stop_reason": stop_reason},
            "summary": {"state": summary, "removals": [{"stage": stage, "cause": cause} for stage, cause in removals],
                        "rewrite": rewrite}}


def with_wire(record, state=None, **kwargs):
    return {**record, "answer_meta": state if state is not None else wire(**kwargs)}


def producer_state_for(record):
    """What the producer attaches to a fixture-agent answer whose summary is shown. The smoke judges a state it did not
    make, so the in-process runs add one here, outside the smoke."""
    answer = record.get("answer")
    if record.get("status") in ("complete", "stopped") and isinstance(answer, dict) and str(answer.get("short_answer", "")).strip():
        return with_wire(record)
    return record


class ProducerState:
    """A client that adds the verified state to the final ask record."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def get(self, url, *args, **kwargs):
        resp = self.inner.get(url, *args, **kwargs)
        parts = url.split("?", 1)[0].rstrip("/").split("/")
        if resp.status_code == 200 and len(parts) >= 2 and parts[-2] == "ask" and "application/json" in resp.headers.get("content-type", ""):
            return httpx.Response(200, json=producer_state_for(resp.json()))
        return resp


import sys  # noqa: E402
import types  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def answer_state_double(monkeypatch):
    """core/agent/answer_state.py (lane 1) is not in this branch, so check_wire is a double: it answers None, or the
    problem code a test sets. What the smoke decides on top of it is what its tests pin."""
    seen = types.SimpleNamespace(problem=None, records=[])

    def check_wire(record):
        seen.records.append(record)
        return seen.problem

    monkeypatch.setitem(sys.modules, "core.agent.answer_state", types.SimpleNamespace(check_wire=check_wire))
    return seen
