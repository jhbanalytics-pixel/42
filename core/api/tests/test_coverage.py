"""Coverage over the fixtures and a fake BigQuery (contract.md section 10.4)."""
import datetime as dt
import json
import re

import pytest

from core.api import coverage, store as store_mod
from core.api.store import BigQueryStore, FixtureStore
from core.config import caps as caps_config

# Arithmetic against the cap as it stood until 3 October 2026 (conftest.py).
pytestmark = pytest.mark.usefixtures("old_model_cap_schedule")

D30 = "2026-09-30"
HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


@pytest.fixture
def fx():
    return FixtureStore()


def fixed_cap_clock(monkeypatch, current):
    class Clock:
        value = current

        @classmethod
        def now(cls, tz=None):
            return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)

    monkeypatch.setattr(caps_config, "datetime", Clock)
    return Clock


class Patched(FixtureStore):
    def __init__(self, **overrides):
        super().__init__()
        for name, fn in overrides.items():
            setattr(self, name, fn)


def is_figure(f, value=None, unit=None):
    assert set(f) == {"value", "unit", "query_id", "run_id", "result_hash"}
    assert HASH.match(f["result_hash"]) and f["query_id"].startswith("q_") and f["run_id"]
    if value is not None:
        assert f["value"] == pytest.approx(value)
    if unit is not None:
        assert f["unit"] == unit
    return True


@pytest.fixture(autouse=True)
def no_cap_env(monkeypatch):
    monkeypatch.delenv("MODEL_DAILY_USD", raising=False)
    monkeypatch.setattr(store_mod, "_CATALOG", {})
    fixed_cap_clock(monkeypatch, dt.datetime(2026, 10, 3, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2))))


def test_fixture_store_coverage_reads(fx):
    credits = fx.credits(D30)
    za_feed = next(r for r in credits if (r["market"], r["job"], r["lane"]) == ("ZA", "collect", "feed"))
    assert za_feed["charged"] == 120.0 and za_feed["calls"] == 12 and za_feed["run_ids"] == ["r_collect_20260930_01"]
    assert any(r["market"] is None for r in credits)
    runs = fx.runs_of_day(D30)
    assert len({r["run_id"] for r in runs}) == len(runs)
    understand = next(r for r in runs if r["run_id"] == "r_understand_20260930_01")
    assert understand["status"] == "ok" and understand["model_usd"] == 1.25
    assert fx.has_model_usd() is True
    card = fx.scorecard()
    assert [r["market"] for r in card] == ["ZA", "NG", "KE"]
    assert {r["week_start"] for r in card} == {"2026-09-21"}


def test_coverage_series_per_market(fx):
    out = coverage.build_coverage(fx, D30)
    assert out["date"] == D30
    assert [m["market"] for m in out["markets"]] == ["ZA", "NG", "KE"]
    za = out["markets"][0]
    assert za["label"] == "South Africa" and len(za["series"]) == 6
    board = next(s for s in za["series"] if s["series"] == "board_tiktok_hashtag")
    # Restated 5 October 2026: each source row also carries its table part (group, group_words) and what the Located
    # column says when no share can be measured (located_words), for the tidied sources table. The fixture's
    # protocol holds no query, so the name stays the series name.
    assert board == {"platform": "tiktok", "series": "board_tiktok_hashtag", "series_words": "TikTok hashtag board",
                     "group": "boards", "group_words": "Trending boards and feeds",
                     "located_words": "Market board",
                     "calls": board["calls"], "calls_ok": board["calls_ok"], "items": 0, "valid": False,
                     "invalid_reason": "calls", "invalid_words": "could not be read", "located_share": None}
    feed = next(s for s in za["series"] if s["series"] == "feed_tiktok")
    assert feed["valid"] is True and feed["invalid_words"] is None and feed["series_words"] == "TikTok"
    ke = out["markets"][2]
    assert {s["invalid_reason"] for s in ke["series"] if not s["valid"]} == {"calls", "drift"}


@pytest.mark.parametrize("calls,calls_ok,items,reason,words", [
    (0, 0, 0, "calls", "no calls recorded"),
    (1, 0, 0, "calls", "could not be read"),
    (1, 1, 0, "items", "post count far from usual"),
    (1, 1, 0, "effort", "too few accounts checked"),
    (1, 1, 0, "zero_yield", "answered but returned nothing for a second day"),
])
def test_coverage_invalid_words_distinguish_no_attempt_from_failure(calls, calls_ok, items, reason, words):
    row = {"market": "ZA", "platform": "x", "series": "panel_x_hub", "calls": calls, "calls_ok": calls_ok,
           "items": items, "valid": False, "invalid_reason": reason, "located_share": None}
    out = coverage.build_coverage(Patched(collection_health=lambda _date: [row]), D30)
    series = next(s for market in out["markets"] for s in market["series"])
    assert series["invalid_words"] == words
    assert {key: series[key] for key in ("calls", "calls_ok", "items", "valid", "invalid_reason")} == {
        "calls": calls, "calls_ok": calls_ok, "items": items, "valid": False, "invalid_reason": reason}


def test_coverage_credits_by_job_and_lane_charged_not_quoted(fx):
    credits = coverage.build_coverage(fx, D30)["credits"]
    assert is_figure(credits["total"], 310.0, "credits charged")  # 120 + 40 + 15 + 30 + 100 + 5, not the quotes
    rows = {(r["market"], r["job"], r["lane"]): r for r in credits["rows"]}
    assert set(rows) == {("ZA", "collect", "feed"), ("ZA", "collect", "expansion"), ("ZA", "brief", "confirm"),
                         ("ZA", "ask", "agent_live"), ("NG", "collect", "feed"), (None, "collect", "seed")}
    r = rows[("ZA", "collect", "feed")]
    assert is_figure(r["credits"], 120.0, "credits charged") and r["calls"] == 12
    assert r["credits"]["run_id"] == "r_collect_20260930_01" and r["credits"]["query_id"] == "q_coverage_credits"
    assert rows[(None, "collect", "seed")]["market_label"] == "No market"
    assert rows[("ZA", "ask", "agent_live")]["market_label"] == "South Africa"


def test_coverage_model_spend_over_every_stage_against_the_cap(fx):
    spend = coverage.build_coverage(fx, D30)["model_spend"]
    # understand 1.25 + detect 0.10 (failed) + 0.40 + brief 2.10 + ask 0.85, each run once
    assert is_figure(spend["usd"], 4.70, "USD")
    assert spend["cap_usd"] == 20.0
    assert {s["stage"]: s["usd"]["value"] for s in spend["stages"]} == pytest.approx(
        {"understand": 1.25, "detect": 0.5, "brief": 2.1, "ask": 0.85})
    assert spend["text"] == "USD 4.70 of the USD 20.00 daily model cap"


def test_coverage_environment_cap_can_only_lower_the_shared_cap(fx, monkeypatch):
    fixed_cap_clock(monkeypatch, dt.datetime(2026, 10, 3, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2))))
    monkeypatch.setenv("MODEL_DAILY_USD", "15")
    assert coverage.build_coverage(fx, D30)["model_spend"]["cap_usd"] == 15.0
    monkeypatch.setenv("MODEL_DAILY_USD", "35")
    assert coverage.build_coverage(fx, D30)["model_spend"]["cap_usd"] == 20.0


def test_coverage_reads_the_shared_cap_again_after_sast_expiry(fx, monkeypatch):
    clock = fixed_cap_clock(monkeypatch, dt.datetime(2026, 10, 2, 21, 59, tzinfo=dt.timezone.utc))
    before = coverage.build_coverage(fx, D30)["model_spend"]["cap_usd"]
    clock.value = dt.datetime(2026, 10, 2, 22, 0, tzinfo=dt.timezone.utc)
    after = coverage.build_coverage(fx, D30)["model_spend"]["cap_usd"]
    assert (before, after) == (80.0, 20.0)


def test_fixture_store_and_coverage_include_counts_and_record_spend(tmp_path):
    rows = [
        {"run_id": "r_parent", "stage": "understand", "run_date": D30, "status": "ok",
         "started_at": "2026-09-30T09:00:00+02:00", "finished_at": "2026-09-30T09:10:00+02:00",
         "counts": {"model_usd": 0.75, "booked_model_usd": 9.0}},
        {"run_id": "r_booking", "stage": "understand_spend", "run_date": D30, "status": "ok",
         "started_at": "2026-09-30T09:11:00+02:00", "finished_at": "2026-09-30T09:12:00+02:00",
         "counts": {"model_usd": 1.25}},
        {"run_id": "r_booking", "stage": "understand_spend", "run_date": D30, "status": "ok",
         "started_at": "2026-09-30T09:11:00+02:00", "finished_at": "2026-09-30T09:13:00+02:00",
         "counts": {"model_usd": 0.5}},
        {"run_id": "r_correction", "stage": "understand_spend", "run_date": D30, "status": "ok",
         "started_at": "2026-09-30T09:14:00+02:00", "finished_at": "2026-09-30T09:15:00+02:00",
         "counts": {"model_usd": -0.5}},
        {"run_id": "r_record", "stage": "brief", "run_date": D30, "status": "ok",
         "started_at": "2026-09-30T10:00:00+02:00", "finished_at": "2026-09-30T10:01:00+02:00",
         "record": {"run": {"model_usd": 0.2}}, "counts": {"model_usd": 8.0}},
        {"run_id": "r_old", "stage": "brief", "run_date": "2026-09-29", "status": "ok",
         "started_at": "2026-09-29T10:00:00+02:00", "finished_at": "2026-09-29T10:01:00+02:00",
         "counts": {"model_usd": 99.0}},
    ]
    for name, fixture_rows in (("runs", rows), ("runs_v2", []), ("collection_health", []),
                               ("credit_ledger", [])):
        (tmp_path / f"{name}.json").write_text(json.dumps(fixture_rows), encoding="utf-8")
    store = FixtureStore(tmp_path)
    spend = coverage.build_coverage(store, D30)["model_spend"]
    assert store.has_model_usd() is True
    assert spend["usd"]["value"] == pytest.approx(1.7)
    assert {row["stage"]: row["usd"]["value"] for row in spend["stages"]} == pytest.approx(
        {"understand": 0.75, "understand_spend": 0.75, "brief": 0.2})


def test_fixture_store_detects_counts_or_record_without_top_level_model_usd(tmp_path):
    (tmp_path / "runs.json").write_text(json.dumps([
        {"run_id": "r_counts", "stage": "understand_spend", "run_date": D30, "status": "ok",
         "counts": {"model_usd": -0.25}},
    ]), encoding="utf-8")
    store = FixtureStore(tmp_path)
    assert store.has_model_usd() is True
    assert store.runs_of_day(D30)[0]["model_usd"] == -0.25


def test_coverage_model_spend_null_until_runs_has_the_column(fx):
    spend = coverage.build_coverage(Patched(has_model_usd=lambda: False), D30)["model_spend"]
    assert spend["usd"] is None and spend["stages"] == []
    assert spend["text"] == "Model spend is not recorded per run yet"
    assert spend["cap_usd"] == 20.0


def test_coverage_model_spend_is_unknown_while_a_model_run_has_no_recorded_cost(fx):
    real = fx.runs_of_day
    no_cost = {"run_id": "r_ask_uncosted", "stage": "ask", "status": "failed",
               "started_at": "2026-09-30T11:00:00+02:00", "finished_at": "2026-09-30T11:00:30+02:00", "error": "agent_unavailable", "model_usd": None}
    # Only the uncosted ask: no measured zero.
    spend = coverage.build_coverage(Patched(runs_of_day=lambda _d: [no_cost]), D30)["model_spend"]
    assert spend["usd"] is None and spend["known_usd"] is None
    assert spend["uncosted_run_ids"] == ["r_ask_uncosted"]
    assert spend["text"] == "Model spend is incomplete: 1 model run has no recorded cost"
    # Beside costed runs: the known subtotal is kept apart, never given as the day's spend.
    spend = coverage.build_coverage(Patched(runs_of_day=lambda d: real(d) + [no_cost]), D30)["model_spend"]
    assert spend["usd"] is None and is_figure(spend["known_usd"], 4.70, "USD")
    assert spend["uncosted_run_ids"] == ["r_ask_uncosted"]
    assert spend["text"] == ("Model spend is incomplete: 1 model run has no recorded cost; "
                             "USD 4.70 is recorded against the USD 20.00 daily model cap")
    # A run that calls no model (collect) or books its spend elsewhere (understand_batch) needs no cost.
    others = [dict(no_cost, run_id="r_c", stage="collect"), dict(no_cost, run_id="r_b", stage="understand_batch"),
              dict(no_cost, run_id="r_z", stage="ask", model_usd=0.0)]
    spend = coverage.build_coverage(Patched(runs_of_day=lambda _d: others), D30)["model_spend"]
    assert is_figure(spend["usd"], 0.0, "USD") and spend["uncosted_run_ids"] == []
    assert spend["text"] == "USD 0.00 of the USD 20.00 daily model cap"


def test_coverage_runs_of_the_day(fx):
    runs = coverage.build_coverage(fx, D30)["runs"]
    assert {(r["stage"], r["status"]) for r in runs} == {
        ("collect", "ok"), ("understand", "ok"), ("detect", "failed"), ("detect", "ok"), ("brief", "ok"),
        ("ask", "failed"), ("ask", "ok")}
    assert len(runs) == 7
    for r in runs:
        assert set(r) == {"run_id", "stage", "status", "started_at", "finished_at", "error"}
    stages = [r["stage"] for r in runs]
    assert stages.index("collect") < stages.index("understand") < stages.index("detect") < stages.index("brief")


def degraded_understand(fx):
    """The fixture day with its understand run finished ok after enrichment and two markets' cluster writes failed,
    its counts as the job writes them."""
    def runs_all():
        rows = FixtureStore._runs_all(fx)
        failed = {"enrich_error": "RuntimeError: write refused",
                  "cluster": {"za": {"clusters": 2}, "ng": {"error": "RuntimeError: cluster_write refused"},
                              "ke": {"skipped": "too_few_posts"}, "pan": {"error": "RuntimeError: refused"}}}
        return [dict(r, counts=json.dumps({**(r.get("counts") or {}), **failed}))
                if r["run_id"] == "r_understand_20260930_01" and r.get("finished_at") else r for r in rows]
    return Patched(_runs_all=runs_all)


def test_fixture_store_reads_failed_understand_writes_from_the_runs_counts(fx):
    runs = degraded_understand(fx).runs_of_day(D30)
    understand = next(r for r in runs if r["run_id"] == "r_understand_20260930_01")
    assert understand["status"] == "ok" and understand["degraded"] == ["enrich", "cluster:ng", "cluster:pan"]
    assert next(r for r in runs if r["stage"] == "collect")["degraded"] == []


def test_coverage_shows_an_ok_understand_run_as_degraded_with_what_failed(fx):
    runs = coverage.build_coverage(degraded_understand(fx), D30)["runs"]
    understand = next(r for r in runs if r["stage"] == "understand")
    assert understand["status"] == "ok"
    assert understand["degraded"] == ["Enrichment", "Clustering for Nigeria",
                                      "Clustering for the three markets pooled"]
    assert all("degraded" not in r for r in runs if r["stage"] != "understand")


def test_coverage_does_not_call_a_failed_run_degraded(fx):
    def runs(_d):
        return [{"run_id": "r_u", "stage": "understand", "status": "failed", "error": "day_changed",
                 "degraded": ["enrich"]}]
    assert "degraded" not in coverage.build_coverage(Patched(runs_of_day=runs), D30)["runs"][0]


def test_coverage_not_seen_is_fixed_copy(fx):
    assert coverage.build_coverage(fx, D30)["not_seen"] == [
        "WhatsApp and other private groups",
        "Official X location trends (the public archive is used only as a seed)",
        "Pinterest Trends (our data supplier does not cover South Africa, Nigeria or Kenya)",
        "Google Trends beyond each market's trending searches of the day, which only guide what 42 looks into",
    ]


def test_coverage_not_seen_says_google_trends_is_read_as_search_interest_only(fx):
    # Ships with or after the Google Trends collect (feat/collect-google-trends, b8aa7548), which reads
    # google_trends/trending for ZA, NG and KE; Coverage then names what stays unseen and that search interest only
    # guides the looking, never evidence.
    lines = coverage.build_coverage(fx, D30)["not_seen"]
    assert not any("coming" in line for line in lines)
    trends = [line for line in lines if "Google Trends" in line]
    assert trends == ["Google Trends beyond each market's trending searches of the day, which only guide what 42 looks into"]


def test_coverage_not_seen_names_no_supplier(fx):
    # The Coverage page is shown to clients, so it says what is missing without naming the vendor behind it.
    for line in coverage.build_coverage(fx, D30)["not_seen"]:
        assert "socialcrawl" not in line.lower()


# The columns of intelligence_42_agent.engine_scorecard as L1 created it on staging (read with get_table as
# f42-builder, 29 September): one row per market per week, each metric a JSON Figure written by L2's scorecard.
LIVE_SCORECARD_COLUMNS = ("week_start", "week_end", "market", "run_id", "rule_version", "time_to_detect", "lead_time",
                          "precision", "recall", "breadth_platforms", "expansion_cluster_share",
                          "expansion_platform_share", "expansion_language_share", "cost_per_confirmed")


def test_scorecard_fixture_has_exactly_the_live_columns(fx):
    for row in fx.scorecard():
        assert tuple(row) == LIVE_SCORECARD_COLUMNS


def test_coverage_scorecard_is_the_latest_week_per_market(fx):
    out = coverage.build_coverage(fx, D30)
    card = out["scorecard"]
    assert out["scorecard_text"] is None
    assert card["week"] == "2026-09-21" and card["week_end"] == "2026-09-27"
    assert [m["market"] for m in card["markets"]] == ["ZA", "NG", "KE"]
    za = card["markets"][0]
    for key in ("time_to_detect", "lead_time", "precision", "recall", "breadth", "cost_per_confirmed_trend"):
        assert is_figure(za[key]) and za[key]["run_id"] == "r_learn_20260921_01"
        assert za[key]["query_id"].startswith("q_scorecard_") and za[key]["result_hash"].startswith("sha256:")
    assert za["precision"]["value"] == 0.6 and za["breadth"]["unit"].startswith("share of trends")
    assert za["cost_per_confirmed_trend"]["value"] == 50.0


def test_coverage_scorecard_keeps_an_unmeasured_metric_with_its_reason(fx):
    ke = coverage.build_coverage(fx, D30)["scorecard"]["markets"][2]
    assert ke["lead_time"]["value"] is None and set(ke["lead_time"]) == {"value", "unit", "query_id", "run_id",
                                                                          "result_hash"}
    assert ke["reasons"] == {"lead_time": "nothing to measure in this market this week"}


def test_coverage_scorecard_reads_json_strings_and_objects_alike():
    import json as _json
    row = {"week_start": "2026-09-21", "week_end": "2026-09-27", "market": "ZA", "run_id": "r1", "rule_version": "v",
           "precision": _json.dumps({"value": 0.5, "unit": "share of x", "query_id": "q", "run_id": "r1",
                                      "result_hash": "h", "n": 4, "reason": None}),
           "recall": {"value": 0.25, "unit": "share of y", "query_id": "q", "run_id": "r1", "result_hash": "h",
                      "n": 4, "reason": None}}
    card = coverage._scorecard([row])
    assert card["markets"][0]["precision"]["value"] == 0.5 and card["markets"][0]["recall"]["value"] == 0.25
    assert card["markets"][0]["lead_time"] is None


def test_coverage_scorecard_passes_the_regime_marker_of_each_figure_through():
    """C4 v3 section 11.4: a reader comparing two weeks needs the rule each Figure was read under."""
    marker = {"locality_basis": "mixed", "previous_week_basis": "v1", "comparable_with_previous_week": False,
              "days_by_basis": {"v1": 2, "locality_v2.1": 2}, "window": {"since": "2026-09-14", "until": "2026-09-27"}}
    row = {"week_start": "2026-09-21", "week_end": "2026-09-27", "market": "ZA", "run_id": "r1", "rule_version": "v",
           "precision": {"value": 0.5, "unit": "u", "query_id": "q", "run_id": "r1", "result_hash": "h", "n": 4,
                         "reason": None, "regime": marker},
           "recall": {"value": 0.25, "unit": "u", "query_id": "q", "run_id": "r1", "result_hash": "h", "n": 4,
                      "reason": None}}
    entry = coverage._scorecard([row])["markets"][0]
    assert entry["precision"]["regime"] == marker and "regime" not in entry["recall"]


def test_coverage_scorecard_null_with_words_before_four_weeks(fx):
    out = coverage.build_coverage(Patched(scorecard=lambda: None), D30)
    assert out["scorecard"] is None
    assert out["scorecard_text"] == ("No weekly scorecard yet. The learn job writes one every Monday at 07:30 SAST, "
                                     "for the week before")


def test_coverage_empty_day(fx):
    out = coverage.build_coverage(fx, "2026-08-01")
    assert all(m["series"] == [] for m in out["markets"])
    assert out["credits"]["rows"] == [] and out["credits"]["total"]["value"] == 0
    assert out["runs"] == [] and out["model_spend"]["usd"]["value"] == 0


def test_coverage_other_markets_are_listed_not_dropped(fx):
    real = fx.collection_health

    def health(date):
        rows = real(date)
        return rows + [dict(rows[0], market="GLOBAL", platform="tiktok", series="counter_tiktok_sound")]

    out = coverage.build_coverage(Patched(collection_health=health), D30)
    assert [m["market"] for m in out["markets"]] == ["ZA", "NG", "KE", "GLOBAL"]
    assert out["markets"][3]["label"] == "Global"


# ---------- redact: error text shown in the app ----------

FAKE_HEX = "0123456789abcdef" * 3
FAKE_B64 = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0MjQyNDI0Mg=="


@pytest.mark.parametrize("raw, gone, kept", [
    ("GET https://api.example.com/v1/feed?market=ZA&key=abc123 failed with 500",
     ["?market", "key=abc123", "abc123"], ["https://api.example.com/v1/feed", "failed with 500"]),
    ("upstream said token=zz-top-99 and api_key=hello-there", ["zz-top-99", "hello-there"], ["token=", "api_key="]),
    ("Authorization: Bearer eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl rejected", ["eyJhbGciOi", "c2lnbmF0dXJl"],
     ["Bearer", "rejected"]),
    (f"hash {FAKE_HEX} not found", [FAKE_HEX], ["hash", "not found"]),
    (f"secret {FAKE_B64} leaked", [FAKE_B64, FAKE_B64[:20]], ["secret", "leaked"]),
    ("Not found: Table ogilvy-trends-v2:intelligence_42_core.v_series_test_current", [],
     ["ogilvy-trends-v2:intelligence_42_core.v_series_test_current"]),
    ("headers {'x-api-key': 'sk_live_ab12cd34'} refused", ["sk_live_ab12cd34", "ab12cd34"], ["x-api-key", "refused"]),
    ('body {"api_key": "abc123def456"} refused', ["abc123def456"], ["api_key", "refused"]),
    ("upstream token: ab12cd34ef expired", ["ab12cd34ef"], ["token", "expired"]),
    ("X-Passcode: Tr3nds42! was wrong", ["Tr3nds42"], ["X-Passcode", "was wrong"]),
    ("Authorization: Basic dXNlcjpwYXNz rejected", ["dXNlcjpwYXNz"], ["Authorization", "Basic", "rejected"]),
    ("GET https://user:hunter2@api.example.com/v1 failed", ["hunter2", "user:"],
     ["https://api.example.com/v1", "failed"]),
])
def test_redact_removes_query_strings_keys_and_tokens(raw, gone, kept):
    out = coverage.redact(raw)
    for text in gone:
        assert text not in out
    for text in kept:
        assert text in out


@pytest.mark.parametrize("plain", [
    "keyboard layout failed; 12 tokens were refused by the passcode check",
    "KeyError: 'model_usd' in the secret manager step",
    "Not found: Table ogilvy-trends-v2:intelligence_42_agent.watches",
])
def test_redact_leaves_plain_words_alone(plain):
    assert coverage.redact(plain) == plain


def test_redact_truncates_to_300_and_passes_none():
    assert len(coverage.redact("word " * 200)) == 300
    assert coverage.redact(None) is None
    assert coverage.redact("short and plain") == "short and plain"


def test_coverage_run_errors_are_redacted(fx):
    real = fx.runs_of_day
    leak = f"collect failed: https://x.example.com/v1?key={FAKE_HEX} Bearer {FAKE_B64} " + "x" * 400

    def runs(date):
        return [dict(r, error=leak if r["status"] == "failed" else r.get("error")) for r in real(date)]

    out = coverage.build_coverage(Patched(runs_of_day=runs), D30)
    failed = [r for r in out["runs"] if r["status"] == "failed"]
    assert failed
    for r in failed:
        assert r["error"].startswith("collect failed: https://x.example.com/v1")
        assert FAKE_HEX not in r["error"] and FAKE_B64 not in r["error"] and len(r["error"]) <= 300


# ---------- BigQuery store ----------

class FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class FakeClient:
    def __init__(self, catalog, rows=None):
        self.catalog, self.rows, self.calls = catalog, rows or [], []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return FakeJob(self.catalog if "INFORMATION_SCHEMA" in sql else self.rows)


def catalog(*, model_usd=True, record=False, counts=False, scorecard=True):
    rows = [{"ds": "intelligence_42_core", "n": "credit_ledger", "what": "table"},
            {"ds": "intelligence_42_agent", "n": "runs", "what": "table"}]
    if scorecard:
        rows.append({"ds": "intelligence_42_agent", "n": "engine_scorecard", "what": "table"})
    if model_usd:
        rows.append({"ds": "intelligence_42_agent", "n": "model_usd", "what": "runs_column"})
    if record:
        rows.append({"ds": "intelligence_42_agent", "n": "record", "what": "runs_column"})
    if counts:
        rows.append({"ds": "intelligence_42_agent", "n": "counts", "what": "runs_column"})
    return rows


def test_bigquery_coverage_reads():
    client = FakeClient(catalog(), rows=[{"week_start": D30, "market": "ZA"}])
    bq = BigQueryStore(client=client)
    bq.credits(D30)
    bq.runs_of_day(D30)
    assert bq.has_model_usd() is True
    assert bq.scorecard() == [{"week_start": D30, "market": "ZA"}]
    reads = [(s, c) for s, c in client.calls if "INFORMATION_SCHEMA" not in s]
    assert len(reads) == 3
    credits_sql, runs_sql, card_sql = (s for s, _ in reads)
    assert "ogilvy-trends-v2.intelligence_42_core.credit_ledger" in credits_sql
    assert "credits_charged" in credits_sql and "credits_quoted" not in credits_sql
    assert "ogilvy-trends-v2.intelligence_42_agent.runs" in runs_sql and "model_usd" in runs_sql
    assert "ROW_NUMBER()" in runs_sql
    assert "ogilvy-trends-v2.intelligence_42_agent.engine_scorecard" in card_sql
    # The live table has week_start, not week (L1's schema); every column the read names must exist there.
    assert "week_start" in card_sql and ".week " not in card_sql + " " and "s.week DESC" not in card_sql
    import re as _re
    named = set(_re.findall(r"\bs\.(\w+)", card_sql))
    assert named <= set(LIVE_SCORECARD_COLUMNS), named - set(LIVE_SCORECARD_COLUMNS)
    for sql, cfg in client.calls:
        assert cfg.maximum_bytes_billed == 2_000_000_000
    assert D30 not in credits_sql + runs_sql
    assert {p.name for _, cfg in reads for p in cfg.query_parameters} == {"d"}


def test_bigquery_coverage_degrades_without_model_usd_or_scorecard():
    client = FakeClient(catalog(model_usd=False, scorecard=False), rows=[])
    bq = BigQueryStore(client=client)
    bq.runs_of_day(D30)
    assert bq.has_model_usd() is False
    assert bq.scorecard() is None
    runs_sql = next(s for s, _ in client.calls if "intelligence_42_agent.runs" in s and "INFORMATION" not in s)
    assert "r.model_usd" not in runs_sql
    assert not any("engine_scorecard" in s for s, _ in client.calls if "INFORMATION_SCHEMA" not in s)


def test_bigquery_runs_of_day_reads_the_failed_writes_only_when_runs_has_counts():
    client = FakeClient(catalog(counts=True, scorecard=False), rows=[
        {"run_id": "r_u", "stage": "understand", "status": "ok", "started_at": None, "finished_at": None,
         "error": None, "model_usd": 0.1, "degraded": ["cluster:za"]}])
    assert BigQueryStore(client=client).runs_of_day(D30)[0]["degraded"] == ["cluster:za"]
    runs_sql = next(s for s, _ in client.calls if "intelligence_42_agent.runs" in s and "INFORMATION" not in s)
    assert f"{store_mod.DEGRADED_SQL} AS degraded" in runs_sql
    assert "JSON_VALUE(r.counts, '$.enrich_error')" in runs_sql
    assert "JSON_VALUE(r.counts, '$.cluster.pan.error')" in runs_sql
    store_mod._CATALOG.clear()  # the catalog is cached per process; this second store's runs has no counts
    client = FakeClient(catalog(scorecard=False), rows=[])
    BigQueryStore(client=client).runs_of_day(D30)
    runs_sql = next(s for s, _ in client.calls if "intelligence_42_agent.runs" in s and "INFORMATION" not in s)
    assert "degraded" not in runs_sql


def test_bigquery_coverage_projects_record_and_counts_spend():
    client = FakeClient(catalog(model_usd=False, record=True, counts=True, scorecard=False), rows=[
        {"run_id": "r_correction", "stage": "understand_spend", "status": "ok",
         "started_at": "2026-09-30T09:14:00+02:00", "finished_at": "2026-09-30T09:15:00+02:00",
         "error": None, "model_usd": -0.25},
    ])
    bq = BigQueryStore(client=client)
    assert bq.has_model_usd() is True
    assert bq.runs_of_day(D30)[0]["model_usd"] == -0.25
    runs_sql = next(s for s, _ in client.calls if "intelligence_42_agent.runs" in s and "INFORMATION" not in s)
    assert "SAFE_CAST(JSON_VALUE(r.record, '$.run.model_usd') AS FLOAT64)" in runs_sql
    assert "SAFE_CAST(JSON_VALUE(r.counts, '$.model_usd') AS FLOAT64)" in runs_sql
    assert "MAX(COALESCE(" in runs_sql


# ---------- moving between days: the latest day with data and what happened to collection ----------

def test_fixture_store_latest_collection_day_is_the_newest_health_day(fx):
    assert fx.latest_collection_day() == D30


def test_coverage_names_the_latest_day_with_data_on_any_day(fx):
    assert coverage.build_coverage(fx, "2026-10-02")["latest_day_with_data"] == D30
    assert coverage.build_coverage(fx, D30)["latest_day_with_data"] == D30


def test_coverage_latest_day_is_none_when_nothing_was_ever_collected():
    out = coverage.build_coverage(Patched(latest_collection_day=lambda: None), "2026-10-02")
    assert out["latest_day_with_data"] is None


def test_coverage_carries_the_sast_day_it_was_read_on(fx, monkeypatch):
    monkeypatch.setattr(coverage, "sast_date", lambda: "2026-10-03")
    assert coverage.build_coverage(fx, D30)["today"] == "2026-10-03"


@pytest.mark.parametrize("day,state", [
    (D30, "collected"),          # sources recorded
    ("2026-09-28", "failed"),    # the collect run failed and no source was recorded
    ("2026-08-01", "not_recorded"),  # no collect run at all
])
def test_coverage_collection_state_from_the_store(fx, day, state):
    assert coverage.build_coverage(fx, day)["collection"] == state


@pytest.mark.parametrize("statuses,state", [
    (["running"], "running"),
    (["ok"], "empty"),
    (["failed", "ok"], "empty"),
    (["failed"], "failed"),
    ([], "not_recorded"),
])
def test_collection_state_without_sources_reads_the_collect_runs(statuses, state):
    runs = [{"stage": "collect", "status": s} for s in statuses] + [{"stage": "detect", "status": "ok"}]
    assert coverage.collection_state([], runs) == state


def test_a_collect_run_left_running_after_a_later_one_finished_does_not_make_the_day_running():
    runs = [{"run_id": "collect-a", "stage": "collect", "status": "running", "started_at": "2026-09-30T04:00:00Z"},
            {"run_id": "collect-b", "stage": "collect", "status": "ok", "started_at": "2026-09-30T05:00:00Z"}]
    assert coverage.collection_state([], runs) == "empty"
    # A run started after the ok one is a rerun still going.
    rerun = runs + [{"run_id": "collect-c", "stage": "collect", "status": "running",
                     "started_at": "2026-09-30T06:00:00Z"}]
    assert coverage.collection_state([], rerun) == "running"


@pytest.mark.parametrize("error,words", [
    ("upstream detect for 2026-10-05 is not ok (latest status running)",
     "Did not start: the detect step had not recorded a finish for this day yet"),
    ("upstream detect for 2026-10-05 is not ok (no runs row)",
     "Did not start: the detect step had not run for this day yet"),
    ("upstream understand for 2026-10-05 is not ok (latest status failed)",
     "Did not start: the understand step did not finish for this day (failed)"),
    ("brief for 2026-10-05 already ran ok (run brief-20261005-0123456789ab)",
     "Not run again: this step had already finished for this day"),
    ("detect for 2026-10-05 is still running (run detect-20261005-0123456789ab, started 2026-10-05T04:00:00+00:00)",
     "Not run again: another run of this step was still going"),
    ("HTTP 500 from the vendor", "HTTP 500 from the vendor"),
    (None, None),
])
def test_chain_reasons_read_in_words_with_no_run_id(error, words):
    """Visual QA, 5 October 2026: the run log showed "upstream detect for 2026-10-05 is not ok (latest status
    running)" and "(run brief-...)" to the reader."""
    assert coverage.run_note(error) == words


def test_a_run_left_running_after_a_later_run_of_its_stage_finished_says_so(fx):
    rows = [{"run_id": "detect-a", "stage": "detect", "status": "running", "started_at": "2026-09-30T04:00:00Z",
             "finished_at": None, "error": None},
            {"run_id": "detect-b", "stage": "detect", "status": "ok", "started_at": "2026-09-30T05:00:00Z",
             "finished_at": "2026-09-30T05:20:00Z", "error": None},
            {"run_id": "brief-a", "stage": "brief", "status": "blocked", "started_at": "2026-09-30T04:30:00Z",
             "finished_at": "2026-09-30T04:30:01Z",
             "error": "upstream detect for 2026-09-30 is not ok (latest status running)"}]
    fx.runs_of_day = lambda date: rows
    runs = {r["run_id"]: r for r in coverage.build_coverage(fx, D30)["runs"]}
    assert runs["detect-a"]["status"] == "running" and runs["detect-a"]["error"] == coverage.STALE_RUNNING
    assert runs["detect-b"]["error"] is None
    assert "run" not in runs["brief-a"]["error"].split() and "2026" not in runs["brief-a"]["error"]


def test_market_summary_counts_usable_sources_as_today_does(fx):
    za = coverage.build_coverage(fx, D30)["markets"][0]
    usable = [s for s in za["series"] if s["valid"]]
    summary = za["summary"]
    assert summary["posts"] == sum(s["items"] for s in usable)
    assert summary["platforms"] == len({s["platform"] for s in usable})
    assert summary["sources"] == len(za["series"]) and summary["sources_usable"] == len(usable)
    weight = sum(s["items"] for s in usable if s["located_share"] is not None and s["items"])
    expect = sum(s["items"] * s["located_share"] for s in usable if s["located_share"] is not None) / weight
    assert summary["located_share"] == pytest.approx(expect, abs=1e-4)


def test_market_summary_is_none_on_an_empty_day_never_zero(fx):
    out = coverage.build_coverage(fx, "2026-08-01")
    assert [m["summary"] for m in out["markets"]] == [None, None, None]


def test_bigquery_latest_collection_day_reads_the_current_health_view():
    client = FakeClient(catalog(), rows=[{"d": D30}])
    assert BigQueryStore(client=client).latest_collection_day() == D30
    sql = client.calls[-1][0]
    assert "MAX(h.day)" in sql and "ogilvy-trends-v2.intelligence_42_core.v_collection_health_current" in sql


@pytest.mark.parametrize("calls,reason,words", [
    (0, "calls: robots", "no calls recorded"),
    (5, "calls: http_5xx,timeout", "could not be read"),
    (1, "calls: empty", "could not be read"),
])
def test_coverage_reads_a_calls_failure_that_names_its_class(calls, reason, words):
    row = {"market": "ZA", "platform": "youtube", "series": "board_youtube", "calls": calls, "calls_ok": 0,
           "items": 0, "valid": False, "invalid_reason": reason, "located_share": None}
    out = coverage.build_coverage(Patched(collection_health=lambda _date: [row]), D30)
    series = next(s for market in out["markets"] for s in market["series"])
    assert series["invalid_words"] == words and series["invalid_reason"] == reason


def health(series, protocol, *, platform=None, route=None, lane="unbiased_rank", calls=1, items=50, valid=True,
           located=None, market="ZA"):
    return {"market": market, "platform": platform, "series": series, "route": route or protocol.partition("?")[0],
            "protocol": protocol, "lane_class": lane, "calls": calls, "calls_ok": calls, "items": items,
            "valid": valid, "invalid_reason": None if valid else "calls", "located_share": located}


# One ZA day shaped like the live one the owner sent (5 October 2026): two Apple Music charts, two TikTok hashtag
# boards, five YouTube boards, a news feed, two unnamed Instagram places, two culture panels and a chart with no call.
ZA_DAY = [
    health("board_google_play", "web/scrape?app_collection=topselling_free&country=ZA", calls=0, items=0,
           valid=False),
    health("board_app_store_iphone", "apple_rss?device=iphone_only&url=https://rss.applemarketingtools.com/za"),
    health("board_apple_music", "apple_music/charts?country=za&type=songs", platform="apple_music"),
    health("board_apple_music", "apple_music/charts?country=za&type=music-videos", platform="apple_music"),
    health("board_shazam_city", "web/scrape?url=https://www.shazam.com/charts/top-50/south-africa/johannesburg"),
    health("board_boomplay", "web/scrape?location_country=ZA&url=https://www.boomplay.com/trending-songs"),
    health("board_tiktok_hashtag", "tiktok/hashtags/popular?countryCode=ZA&period=7", platform="tiktok", items=46),
    health("board_tiktok_hashtag", "tiktok/hashtags/popular?countryCode=ZA&industry=all&period=7", platform="tiktok",
           items=3),
    *[health("board_youtube", "youtube/videos/trending?" + (f"category={c}&" if c else "") + "region=ZA",
             platform="youtube", located=0.04) for c in ("", "10", "1", "20", "24")],
    health("list_reddit", "reddit/subreddit?sort=hot&subreddit=southafrica", platform="reddit", located=0.5),
    health("feed_tiktok", "tiktok/trending?feed=local&region=ZA", platform="tiktok", calls=3, located=0.6),
    health("news_rss", "public_feed?feed_id=za_enca&url=https%3A%2F%2Fwww.enca.com%2F", route="public_feed",
           lane="context", items=12),
    health("ig_location", "instagram/location/posts?location_id=320191774775153", platform="instagram",
           lane="search_presence", located=0.9),
    health("ig_location", "instagram/location/posts?location_id=137545546357317", platform="instagram",
           lane="search_presence", located=0.8),
    health("search", "tiktok/search/top?country=ZA&planned=60", platform="tiktok", lane="search_presence",
           calls=60, items=900, located=0.5),
    health("panel_culture_desk", "panel:aaaaaaaaaaaa", route="prism/profiles", lane="panel", items=9, located=0.2),
    health("panel_culture_desk", "panel:bbbbbbbbbbbb", route="prism/profiles", lane="panel", items=19, located=0.2),
    health("panel_fb_hub", "panel:cccccccccccc", platform="facebook", route="facebook/profile/posts", lane="panel",
           calls=4, items=31, located=0.7),
]


def test_coverage_names_every_source_once_with_what_tells_it_apart():
    za = coverage.build_coverage(Patched(collection_health=lambda _date: ZA_DAY), D30)["markets"][0]
    names = [s["series_words"] for s in za["series"]]
    assert len(names) == len(set(names)) == len(ZA_DAY)
    assert names == [
        "Google Play top free apps", "App Store top free apps (iPhone)", "Apple Music chart, songs",
        "Apple Music chart, music videos", "Shazam city chart", "Boomplay trending songs",
        "TikTok hashtag board, last 7 days", "TikTok hashtag board, all industries, last 7 days",
        "YouTube trending board, all categories", "YouTube trending board, Music",
        "YouTube trending board, Film and animation", "YouTube trending board, Gaming",
        "YouTube trending board, Entertainment", "Reddit, r/southafrica, hot", "TikTok, local feed",
        "News feed, eNCA",
        # Instagram places have only ids, and two panels share a series: numbered in protocol order.
        "Instagram location posts, place 2", "Instagram location posts, place 1", "TikTok search, top posts",
        "Culture accounts we follow, list 1", "Culture accounts we follow, list 2", "Facebook, pages we follow"]


def test_coverage_puts_each_source_in_one_part_of_the_table():
    za = coverage.build_coverage(Patched(collection_health=lambda _date: ZA_DAY), D30)["markets"][0]
    parts = {}
    for s in za["series"]:
        parts.setdefault(s["group"], []).append(s["series_words"])
        assert s["group_words"] == coverage.GROUP_WORDS[s["group"]]
    assert list(parts) == ["charts", "boards", "news", "posts"]
    assert len(parts["charts"]) == 6 and len(parts["boards"]) == 9 and parts["news"] == ["News feed, eNCA"]
    assert len(parts["posts"]) == 6
    assert [w for _, w in coverage.GROUPS] == ["Charts and app stores", "Trending boards and feeds",
                                                "Posts from platforms and followed accounts", "News feeds",
                                                "Other sources"]


@pytest.mark.parametrize("row,group", [
    ({"series": "radio_playlist", "lane_class": "context"}, "charts"),
    ({"series": "board_music_country", "lane_class": "unbiased_rank"}, "charts"),
    ({"series": "news_rss", "lane_class": "context"}, "news"),
    ({"series": "x_trends", "lane_class": "unbiased_rank"}, "boards"),
    ({"series": "board_something_new", "lane_class": "unbiased_rank"}, "boards"),
    ({"series": "panel_telegram", "lane_class": "panel"}, "posts"),
    ({"series": "counter_tiktok_sound", "lane_class": "watchlist"}, "posts"),
    ({"series": "placebo", "lane_class": "search_presence"}, "posts"),
    ({"series": "mystery", "lane_class": None}, "other"),
])
def test_source_group_reads_series_then_lane(row, group):
    assert coverage.source_group(row) == group


def test_coverage_located_words_say_what_a_chart_is_only_when_nothing_was_measured():
    za = coverage.build_coverage(Patched(collection_health=lambda _date: ZA_DAY), D30)["markets"][0]
    words = {s["series_words"]: s["located_words"] for s in za["series"]}
    assert words["Apple Music chart, songs"] == "National chart"
    assert words["App Store top free apps (iPhone)"] == "National chart"
    assert words["Shazam city chart"] == "City chart"
    assert words["Boomplay trending songs"] == "Seen from the market"
    assert words["TikTok hashtag board, last 7 days"] == "Market board"
    # A measured share keeps its percentage, and a source that is no chart or board stays "Not measured".
    assert words["YouTube trending board, Music"] is None
    assert words["News feed, eNCA"] is None
    assert words["Culture accounts we follow, list 1"] is None


def test_coverage_tidy_names_leave_the_summary_counts_as_they_were():
    # The row with no call still counts as a source that is not usable, as before the tidy.
    za = coverage.build_coverage(Patched(collection_health=lambda _date: ZA_DAY), D30)["markets"][0]
    assert za["summary"]["sources"] == len(ZA_DAY)
    assert za["summary"]["sources_usable"] == len(ZA_DAY) - 1
    assert za["summary"]["posts"] == sum(r["items"] for r in ZA_DAY if r["valid"])


@pytest.mark.parametrize("protocol,name", [
    ("rss?url=https://ewn.co.za/feed", "News feed, EWN"),
    ("public_feed?feed_id=ng_punch&url=https%3A%2F%2Fpunchng.com%2F", "News feed, Punch"),
    ("news_rss_p1", "News feeds"),
])
def test_coverage_names_a_news_feed_by_its_publication(protocol, name):
    row = health("news_rss", protocol, lane="context")
    out = coverage.build_coverage(Patched(collection_health=lambda _date: [row]), D30)
    assert out["markets"][0]["series"][0]["series_words"] == name


def test_coverage_names_a_catalog_chart_or_playlist_by_its_own_name():
    rows = [health("board_music_country", "public_feed?feed_id=ng_turntable_top_100&url=x", market="NG"),
            health("radio_playlist", "public_feed?feed_id=ke_kbc_english_playlist&url=x", lane="context",
                   market="KE")]
    out = coverage.build_coverage(Patched(collection_health=lambda _date: rows), D30)
    assert out["markets"][1]["series"][0]["series_words"] == "TurnTable Official Nigeria Top 100"
    assert out["markets"][1]["series"][0]["located_words"] == "National chart"
    assert out["markets"][2]["series"][0]["series_words"] == "KBC English Service playlist via Radio.co"
    assert out["markets"][2]["series"][0]["located_words"] == "Radio station playlist"
