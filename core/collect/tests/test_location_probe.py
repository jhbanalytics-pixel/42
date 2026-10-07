import json
from datetime import datetime, timezone

import pytest

from core.collect.tests.test_socialcrawl_client import FakeHTTP, make


NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
ENV = {"CLOUD_RUN_JOB": "f42-probe", "CLOUD_RUN_EXECUTION": "f42-probe-offline",
       "CLOUD_RUN_TASK_INDEX": "0", "CLOUD_RUN_TASK_ATTEMPT": "0"}
POISON = "GENERATED DISCOVERY TEXT MUST NOT SURVIVE"


def bodies(count=3):
    posts = [{"id": str(n), "url": f"https://x.com/ARISEtv/status/{n}",
              "published_at": "2026-10-05T12:00:00Z", "author": {"username": "ARISEtv"},
              "content": {"text": "native post"}, "engagement": {"views": 2}} for n in range(1, count + 1)]
    return {"tiktok/profile": (200, {"success": True, "data": {"author": {
                "username": "jessykiss_madridista", "location": None}}}),
            "instagram/profile/about": (200, {"success": True, "data": {"author": {"username": "primo9teen", "ext": {"country": "South Africa"}}}}),
            "youtube/search/advanced": (200, {"success": True, "data": {"items": []}}),
            "twitter/ai-search": (200, {"success": True, "credits_used": 5, "data": {
                "answer": POISON, "sources": [{"url": p["url"], "title": POISON} for p in posts],
                "tool_calls_count": 1}}),
            "twitter/tweet": [(200, {"success": True, "data": {"post": p}}) for p in posts]}


def client(monkeypatch, answers=None):
    monkeypatch.setenv("SOCIALCRAWL_OGILVY_API_KEY", "fake")
    result = make(share="build", http=FakeHTTP(answers or bodies()), retry_wait=0, schedule_started=True)
    result.clock = lambda: NOW
    return result


def test_plan_is_exact_finite_and_uses_existing_geo_scope():
    from core.collect import location_probe as lp

    p = lp.plan()
    assert p["max_credits"] == 15 and p["max_lookups"] == 2 and p["max_http_attempts"] == 13
    assert [c["route"] for c in p["calls"]] == ["tiktok/profile", "instagram/profile/about", "youtube/search/advanced", "twitter/ai-search"]
    assert p["calls"][2]["params"]["location"] == "6.52,3.37"
    assert p["calls"][2]["params"]["location_radius"] == "50km"
    assert p["calls"][2]["params"]["max_pages"] == 1
    assert "include" not in p["calls"][2]["params"]


@pytest.mark.parametrize("env,digest", [({}, "correct"), (ENV, "wrong"),
    ({**ENV, "CLOUD_RUN_TASK_ATTEMPT": "1"}, "correct"), ({**ENV, "CLOUD_RUN_JOB": "f42-collect"}, "correct")])
def test_execute_refuses_local_wrong_job_retry_or_wrong_approval_before_factory(env, digest):
    from core.collect import location_probe as lp

    def no_factory(*args, **kwargs):
        raise AssertionError("factory must not run")

    approved = lp.plan_hash() if digest == "correct" else digest
    assert lp.main(["--execute", "--approved-plan-sha256", approved], env=env, factory=no_factory,
                   clock=lambda: NOW) == 2


def test_default_is_plan_only_without_credentials_or_factory(capsys):
    from core.collect import location_probe as lp

    assert lp.main([], factory=lambda *a, **kw: pytest.fail("factory must not run")) == 0
    assert json.loads(capsys.readouterr().out)["max_credits"] == 15


def test_finite_run_keeps_raw_provenance_and_discards_generated_text(monkeypatch):
    from core.collect import location_probe as lp

    c = client(monkeypatch, bodies(5))
    summary = lp.run(c, clock=lambda: NOW)
    assert summary["credits_charged"] == 15 and summary["http_attempts"] == 7
    assert summary["native_ids"] == ["1", "2"]
    assert len(c.raw.rows) == 6 and all(r["job"] == "build" for r in c.raw.rows)
    assert next(r for r in c.raw.rows if r["route"] == "instagram/profile/about")["market"] == "ZA"
    assert POISON not in json.dumps([summary, c.raw.rows, c.ledger.rows])
    assert all(p["geo_market"] is None for p in summary["parsed"]["posts"])
    assert all(o["source_market"] is None for o in summary["parsed"]["observations"])


def test_zero_charge_refund_retry_has_exact_attempt_bound(monkeypatch):
    from core.collect import location_probe as lp

    answers = bodies()
    for route in list(answers):
        values = answers[route] if isinstance(answers[route], list) else [answers[route]]
        answers[route] = [item for value in values for item in [(502, {"credits_used": 0}), value]]
    c = client(monkeypatch, answers)
    summary = lp.run(c, clock=lambda: NOW)
    assert summary["http_attempts"] == 13 and summary["credits_charged"] == 15
    assert len(c.raw.rows) == 12 and len(summary["native_ids"]) == 2


def test_lookup_date_refusal_does_not_replace_slot_and_later_valid_lookup_runs(monkeypatch):
    from core.collect import location_probe as lp

    answers = bodies(4)
    answers["twitter/tweet"][0][1]["data"]["post"]["published_at"] = None
    c = client(monkeypatch, answers)
    summary = lp.run(c, clock=lambda: NOW)
    assert summary["native_ids"] == ["2"]
    assert len([r for r in c.http.transport.requests if r["route"] == "twitter/tweet"]) == 2
    assert summary["records"][4]["status"] == "lookup_refused"


def test_cap_refusal_stops_without_provider_requests(monkeypatch):
    from core.collect import location_probe as lp
    from core.collect.tests.test_socialcrawl_client import spent

    c = client(monkeypatch)
    spent(c.ledger, "build", 150, day=NOW.date())
    summary = lp.run(c, clock=lambda: NOW)
    assert summary["stopped"] == "cap_reached" and summary["http_attempts"] == 0


def test_guard_refuses_unapproved_route_and_changed_page_params_without_http():
    from core.collect import location_probe as lp

    transport = FakeHTTP({})
    guard = lp.RequestGuard(transport)
    with pytest.raises(lp.ProbeRefused):
        guard("GET", "https://www.socialcrawl.dev/v1/tiktok/location/posts", params={"location_id": "unapproved"})
    c = lp.plan()["calls"][2]
    with pytest.raises(lp.ProbeRefused):
        guard("GET", "https://www.socialcrawl.dev/v1/" + c["route"], params={**c["params"], "max_pages": 2})
    assert transport.requests == []


def test_wrong_profile_owner_and_bad_youtube_shape_are_per_call_failures(monkeypatch):
    from core.collect import location_probe as lp

    answers = bodies()
    answers["tiktok/profile"][1]["data"]["author"]["username"] = "other"
    answers["youtube/search/advanced"][1]["data"] = {"wrong": []}
    summary = lp.run(client(monkeypatch, answers), clock=lambda: NOW)
    assert [summary["records"][i]["status"] for i in (0, 2)] == ["unparsable", "unparsable"]
    assert summary["native_ids"] == ["1", "2"] and summary["stopped"] is None


def test_nonjson_refunded_response_keeps_the_existing_retry_policy(monkeypatch):
    from core.collect import location_probe as lp

    answers = bodies()
    answers["tiktok/profile"] = [(502, "bad gateway"), answers["tiktok/profile"]]
    summary = lp.run(client(monkeypatch, answers), clock=lambda: NOW)
    assert summary["records"][0]["status"] == "ok" and summary["records"][0]["attempts"] == 2
    assert summary["credits_charged"] == 15 and summary["http_attempts"] == 8


def test_discovery_generated_text_cannot_hide_in_malformed_metadata(monkeypatch):
    from core.collect import location_probe as lp

    answers = bodies()
    discovery = answers["twitter/ai-search"][1]
    discovery["request_id"] = {"answer": POISON}
    discovery["credits_used"] = {"answer": POISON}
    discovery["cached"] = {"answer": POISON}
    c = client(monkeypatch, answers)
    summary = lp.run(c, clock=lambda: NOW)
    assert POISON not in json.dumps([c.raw.rows, c.ledger.rows, summary])


def test_raw_store_failure_stops_before_another_paid_request(monkeypatch):
    from core.collect import location_probe as lp

    c = client(monkeypatch)
    c.raw.append = lambda row: (_ for _ in ()).throw(RuntimeError("fake raw failure"))
    summary = lp.run(c, clock=lambda: NOW)
    assert summary["stopped"] == "probe_boundary"
    assert summary["http_attempts"] == 2 and summary["credits_charged"] == 1


@pytest.mark.parametrize("reported", [2, 16])
def test_unexpected_reported_charge_stops_before_next_call(monkeypatch, reported):
    from core.collect import location_probe as lp

    answers = bodies()
    answers["tiktok/profile"][1]["credits_used"] = reported
    summary = lp.run(client(monkeypatch, answers), clock=lambda: NOW)
    assert summary["stopped"] == "probe_boundary" and summary["http_attempts"] == 2
    assert summary["credits_charged"] == reported


def test_guard_refuses_repeating_a_successful_request():
    from core.collect import location_probe as lp

    transport = FakeHTTP(bodies())
    guard = lp.RequestGuard(transport)
    call = lp.plan()["calls"][0]
    url = "https://www.socialcrawl.dev/v1/" + call["route"]
    guard("GET", url, params=call["params"])
    with pytest.raises(lp.ProbeRefused, match="retry"):
        guard("GET", url, params=call["params"])
    assert len(transport.requests) == 1


def test_changed_dependency_invalidates_prior_approval_before_factory(monkeypatch, tmp_path):
    from core.collect import location_probe as lp

    prior = lp.plan_hash()
    for name in (*lp.DEPENDENCIES, "core/collect/location_probe.py"):
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((lp.ROOT / name).read_bytes())
    target = tmp_path / "core/config/caps.yaml"
    target.write_bytes(target.read_bytes() + b"\n")
    monkeypatch.setattr(lp, "ROOT", tmp_path)
    assert lp.plan_hash() != prior
    assert lp.main(["--execute", "--approved-plan-sha256", prior], env=ENV, clock=lambda: NOW,
                   factory=lambda *a, **kw: pytest.fail("factory must not run")) == 2
