"""One refill round when every judged candidate of a market was held (Albert, 23 Oct 2026).

When a market publishes no card after its normal judged round (CANDIDATES), the brief judges up to REFILL more of
its candidates, the next eligible ones in the same selection order, through the same explanation pipeline and the
same gates, once. It runs only inside the day's model cap and the job's deadline and time limit; when there is no
room it does not run and the reason is recorded. The selection receipt and the run counts record the refill, how many
it judged and why it stopped, and the not assessed block leaves out every candidate the refill prepared. Fakes only:
no model, no cloud.
"""

import re
from datetime import datetime, time
from types import SimpleNamespace

import pytest

from core.brief import explain, job
from core.brief.tests.test_brief_job import (  # noqa: F401
    D, SAST, Client, FakeModel, brief, busy_waits, market_scope_is_valid_by_default, payload, world,
)


class Passes(FakeModel):
    """The job's FakeModel whose critic leaves the simple explanation standing for every item but those in `ok`, so
    every other explanation is held (G10)."""

    def __init__(self, ok=(), **kw):
        super().__init__(**kw)
        self.ok = set(ok)
        self.judged = []

    def complete_json(self, *, system, user, schema, model, max_tokens):
        key = re.search(r"#((?:za|ng|ke)\d+)\b|\b((?:za|ng|ke)\d+)_p\d", user)
        key = key.group(1) or key.group(2)
        if "claims" in schema["properties"] and key not in self.judged:
            self.judged.append(key)
        out, usage = super().complete_json(system=system, user=user, schema=schema, model=model,
                                           max_tokens=max_tokens)
        if "ruled_out" in schema["properties"] and key not in self.ok:
            out = {**out, "ruled_out": False}
        return out, usage


@pytest.fixture(autouse=True)
def receipts(monkeypatch):
    """Selection reads carry a job id, so the receipt and the not assessed block are written."""
    real_query = Client.query

    def with_job_id(self, sql, job_config=None):
        result = real_query(self, sql, job_config)
        return SimpleNamespace(job_id="selection-fixture", result=result.result)

    monkeypatch.setattr(Client, "query", with_job_id)


def ng(*numbers):
    return [f"ng{n}" for n in numbers]


def shown(r, market="NG"):
    p = payload(r, market)
    return [c["item_id"] for c in p["cards"] + p["more"]]


def test_all_ten_held_refill_judges_five_more_and_publishes_the_one_that_passes():
    r = brief(world(n=17, markets=("NG",)), model=Passes(ok=ng(11)))
    assert r.model.judged == ng(*range(1, 16))
    assert shown(r) == ng(11)
    p = payload(r, "NG")
    refill = p["selection_receipt"]["refill"]
    assert refill == {"ran": True, "judged": 5, "taken": 5, "stop": "refill_limit_reached"}
    assert r.counts["refill"] == {"NG": refill}
    assert p["selection_receipt"]["preparation"]["judged"] == 15
    # The ten the normal round judged and the four other refills stay held; only 16 and 17 were never assessed.
    assert {i["item_id"] for i in p["held_back"]["items"]} == set(ng(*range(1, 11), *range(12, 16)))
    assert {i["item_id"]: i["reason"] for i in p["not_assessed"]["items"]} == {
        "ng16": "judged_limit_reached", "ng17": "judged_limit_reached"}
    assert p["not_assessed"]["count"] == 2 and p["not_assessed"]["judged_limit"] == job.CANDIDATES


def test_a_market_with_a_card_gets_no_refill():
    r = brief(world(n=17, markets=("NG",)), model=Passes(ok=ng(5)))
    assert r.model.judged == ng(*range(1, 11))
    assert shown(r) == ng(5)
    assert "refill" not in payload(r, "NG")["selection_receipt"] and "refill" not in r.counts
    assert payload(r, "NG")["not_assessed"]["count"] == 7


def test_only_the_market_with_no_card_is_refilled():
    model = Passes(ok=["za1", "ke1", "ng12"])
    r = brief(world(n=13), model=model)
    assert [k for k in model.judged if k.startswith("ng")] == ng(*range(1, 14))
    assert len([k for k in model.judged if k.startswith("za")]) == 10
    assert len([k for k in model.judged if k.startswith("ke")]) == 10
    assert shown(r) == ng(12) and shown(r, "ZA") == ["za1"] and shown(r, "KE") == ["ke1"]
    assert r.counts["refill"] == {"NG": {"ran": True, "judged": 3, "taken": 3, "stop": "pool_exhausted"}}


def test_no_cap_headroom_means_no_refill_and_the_reason_is_recorded(monkeypatch):
    monkeypatch.setattr(explain, "_estimate_usd", lambda *args: 0.0)
    # What the ten held explanations cost; the day's cap is set to exactly that.
    spent = brief(world(n=10, markets=("NG",)), model=Passes()).counts["model_usd"]
    monkeypatch.setattr(job, "model_daily_usd", lambda: spent)
    monkeypatch.setattr(explain, "model_daily_usd", lambda: spent)
    model = Passes(ok=ng(11))
    r = brief(world(n=17, markets=("NG",)), model=model)
    assert model.judged == ng(*range(1, 11))
    assert shown(r) == []
    refill = payload(r, "NG")["selection_receipt"]["refill"]
    assert refill == {"ran": False, "judged": 0, "taken": 0, "stop": "model_cap"}
    assert r.counts["refill"] == {"NG": refill} and r.counts["model_usd"] == pytest.approx(spent)
    p = payload(r, "NG")
    assert p["not_assessed"]["count"] == 7 and "ng11" in {i["item_id"] for i in p["not_assessed"]["items"]}


def test_no_refill_once_a_whole_call_no_longer_fits_before_the_deadline():
    near = datetime.combine(D, time(6, 14, 30), SAST)  # 30 s left, under GEMINI_TIMEOUT_S
    model = Passes(ok=ng(11))
    r = brief(world(n=17, markets=("NG",)), model=model, clock=lambda: near)
    assert "ng11" not in model.judged and shown(r) == []
    assert r.counts["refill"]["NG"] == {"ran": False, "judged": 0, "taken": 0, "stop": "deadline"}


def test_the_refill_never_judges_more_than_five_and_runs_once():
    model = Passes()
    r = brief(world(n=25, markets=("NG",)), model=model)
    assert model.judged == ng(*range(1, 16))
    p = payload(r, "NG")
    assert p["selection_receipt"]["refill"] == {"ran": True, "judged": 5, "taken": 5,
                                                "stop": "refill_limit_reached"}
    assert shown(r) == [] and p["held_back"]["count"] == 15
    assert p["not_assessed"]["count"] == 10
    assert {i["item_id"] for i in p["not_assessed"]["items"]} == set(ng(*range(16, 26)))


def test_a_g1_hold_in_the_refill_takes_no_refill_slot():
    from core.brief.tests.test_brief_job import FakeCtx

    model = Passes()
    r = brief(world(n=20, markets=("NG",)), model=model, ctx=FakeCtx({"ng12": {"valid_days": [True, False, True]}}))
    assert model.judged == ng(*range(1, 12), *range(13, 17))
    refill = payload(r, "NG")["selection_receipt"]["refill"]
    assert refill == {"ran": True, "judged": 5, "taken": 6, "stop": "refill_limit_reached"}
    assert payload(r, "NG")["not_assessed"]["count"] == 4
