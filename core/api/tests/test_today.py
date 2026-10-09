from copy import deepcopy
import hashlib
from itertools import permutations
import json
import re
from pathlib import Path
from urllib.parse import urlencode

import pytest

from core.api import today
from core.api.store import BigQueryStore, FixtureStore, get_store

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
D30 = "2026-09-30"
D29 = "2026-09-29"


def iid(kind, key):
    return hashlib.sha256(f"{kind}|{key}".encode("utf-8")).hexdigest()


ZA_A = iid("hashtag", "#fixture_za_step")
ZA_B = iid("sound", "fixture za sound one")
ZA_C = iid("hashtag", "#fixture_za_spring")
ZA_D = iid("topic", "fixture za taxi fares")
ZA_E = iid("hashtag", "#fixture_za_board")
ZA_F = iid("format", "fixture za duet format")
ZA_G = iid("meme", "fixture za meme")
ZA_H = iid("hashtag", "#fixture_za_small")
ZA_I = iid("hashtag", "#fixture_za_outage")
ZA_X = iid("hashtag", "#fixture_za_picnic")
ZA_Y = iid("topic", "fixture za late train")
NG_A = iid("hashtag", "#fixture_ng_owambe")
NG_B = iid("sound", "fixture ng sound one")
NG_C = iid("topic", "fixture ng fuel queue")
NG_D = iid("hashtag", "#fixture_ng_skit")
NG_E = iid("meme", "fixture ng meme")
NG_Y = iid("topic", "fixture ng rain")
NG_Z = iid("hashtag", "#fixture_ng_promo")
KE_Q = iid("hashtag", "#fixture_ke_match")


@pytest.fixture
def fx():
    return FixtureStore()


def market(obj, code):
    return next(m for m in obj["markets"] if m["market"] == code)


class Patched(FixtureStore):
    """FixtureStore with chosen methods replaced, for edge cases."""

    def __init__(self, **overrides):
        super().__init__()
        for name, fn in overrides.items():
            setattr(self, name, fn)


def _passing_specificity_card():
    rows = FixtureStore().briefs(D30)
    row = next(r for r in rows if r["market"] == "ZA")
    card = deepcopy(
        next(c for c in row["payload"]["cards"] + row["payload"]["more"] if c["item_id"] == ZA_A)
    )
    evidence = card["evidence"]
    evidence_by_id = {
        record["id"]: record for record in evidence
        if isinstance(record, dict) and isinstance(record.get("id"), str)
    }
    claims_by_id = {
        claim["id"]: claim for claim in card["claims"]
        if isinstance(claim, dict) and isinstance(claim.get("id"), str)
    }
    referenced = [claims_by_id[claim_id] for claim_id in card["explanation_claim_ids"]]
    local_ids = list(dict.fromkeys(
        evidence_id for claim in referenced for evidence_id in claim["evidence_ids"]
        if evidence_id in evidence_by_id
    ))
    quote = next(
        quote for claim in referenced for quote in claim.get("quotes") or []
        if quote.get("evidence_id") in local_ids
    )
    quote_id = quote["evidence_id"]
    local_ids = [evidence_id for evidence_id in local_ids if evidence_id != quote_id] + [quote_id]
    card["evidence"] = [record for record in evidence if record["id"] != quote_id] + [evidence_by_id[quote_id]]
    card["specificity"] = {
        "status": "pass", "local_evidence_ids": local_ids, "quote": deepcopy(quote),
        "why_now": card["explanation"], "reason": None,
    }
    return card


def _today_store(cards, more=None, headline=None, held_back=None):
    base = FixtureStore()

    def briefs(date):
        rows = deepcopy(base.briefs(date))
        if date == D30:
            for row in rows:
                row["payload"]["headline"] = None
                if row["market"] == "ZA":
                    row["payload"]["cards"] = deepcopy(cards)
                    row["payload"]["more"] = deepcopy(more or [])
                    row["payload"]["headline"] = deepcopy(headline)
                    if held_back is not None:
                        row["payload"]["held_back"] = deepcopy(held_back)
        return rows

    return Patched(briefs=briefs)


def _youtube_creator_card(item_id, title):
    card = _passing_specificity_card()
    card.update(item_id=item_id, kind="creator", title=title)
    return card


def _replace_specificity_quote(card, text, source_text=None):
    quote = card["specificity"]["quote"]
    quote_id = quote["evidence_id"]
    quote["text"] = text
    record = next(record for record in card["evidence"] if record["id"] == quote_id)
    if source_text is not None:
        record["text"] = source_text
    for claim_id in card["explanation_claim_ids"]:
        for cited_quote in card["claims"]:
            if cited_quote["id"] != claim_id:
                continue
            for claim_quote in cited_quote.get("quotes") or []:
                if claim_quote.get("evidence_id") == quote_id:
                    claim_quote["text"] = text


# ---------- store ----------

def test_get_store_picks_backend(monkeypatch):
    monkeypatch.delenv("F42_DATA", raising=False)
    assert isinstance(get_store(), FixtureStore)
    monkeypatch.setenv("F42_DATA", "fixtures")
    assert isinstance(get_store(), FixtureStore)
    monkeypatch.setenv("F42_DATA", "bigquery")
    monkeypatch.setattr("google.cloud.bigquery.Client", lambda *a, **k: FakeClient())
    assert isinstance(get_store(), BigQueryStore)


def test_fixture_store_reads(fx):
    assert fx.latest_brief_date() == D30
    rows = fx.briefs(D30)
    assert sorted(r["market"] for r in rows) == ["KE", "NG", "ZA"]
    for r in rows:
        assert set(r) >= {"brief_date", "market", "run_id", "published_at", "status", "payload", "rule_version"}
        assert isinstance(r["payload"], dict)
    assert sorted(r["market"] for r in fx.briefs(D29)) == ["NG", "ZA"]
    assert fx.briefs("2026-09-28") == []
    assert fx.previous_brief("ZA", D30)["brief_date"] == D29
    assert fx.previous_brief("ZA", D29) is None
    assert fx.previous_brief("KE", D30) is None
    assert fx.first_ok_collect_date() == D29
    assert fx.collection_health(D30) and all(r["day"] == D30 for r in fx.collection_health(D30))
    cal = fx.calendar(D30, "2026-10-14")
    assert cal and all(D30 <= r["moment_date"] <= "2026-10-14" for r in cal)
    assert [r["status"] for r in fx.runs("collect", D30)] == ["ok"]
    assert fx.runs("collect", "2026-09-28")[0]["status"] == "failed"
    rec = fx.ask_record("a_20260930_fixture_failed")
    assert rec["ask_id"] == "a_20260930_fixture_failed" and rec["status"] == "failed"
    assert fx.ask_record("a_missing") is None
    assert fx.health() == "ok"


class FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class FakeClient:
    def __init__(self, rows=None):
        self.calls = []
        self.rows = rows or []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return FakeJob(self.rows)


def test_bigquery_store_queries_views_with_bytes_cap():
    client = FakeClient()
    bq = BigQueryStore(client=client)
    bq.latest_brief_date()
    bq.briefs(D30)
    bq.previous_brief("ZA", D30)
    bq.collection_health(D30)
    bq.calendar(D30, "2026-10-14")
    bq.runs("collect", D30)
    bq.first_ok_collect_date()
    bq.ask_record("a_x")
    sqls = [c[0] for c in client.calls]
    assert len(sqls) == 8
    for sql in sqls[:3]:
        assert "intelligence_42_agent.v_briefs_current" in sql
    assert "intelligence_42_core.v_collection_health_current" in sqls[3]
    assert "intelligence_42_core.calendar" in sqls[4]
    for sql in sqls[5:]:
        assert "intelligence_42_agent.runs" in sql
    assert all("ogilvy-trends-v2" in s for s in sqls)
    for sql, cfg in client.calls:
        assert cfg.maximum_bytes_billed == 2_000_000_000
        assert "@" in sql or not cfg.query_parameters
    # parameterised: the values never appear inside the SQL text
    assert D30 not in sqls[1] and "'ZA'" not in sqls[2] and "a_x" not in sqls[7]
    names = {p.name for _, cfg in client.calls for p in cfg.query_parameters}
    assert {"d", "market", "before", "start", "end", "stage", "ask_id"} <= names


def test_bigquery_store_project_from_env(monkeypatch):
    monkeypatch.setenv("F42_PROJECT", "some-other-project")
    client = FakeClient()
    BigQueryStore(client=client).briefs(D30)
    assert "some-other-project.intelligence_42_agent.v_briefs_current" in client.calls[0][0]


def test_bigquery_store_normalises_rows():
    import datetime as dt

    row = {
        "brief_date": dt.date(2026, 9, 30), "market": "ZA", "run_id": "r_1",
        "published_at": dt.datetime(2026, 9, 30, 4, 14, 40, tzinfo=dt.timezone.utc),
        "status": "published", "payload": json.dumps({"cards": []}), "rule_version": "v1",
    }
    bq = BigQueryStore(client=FakeClient([row]))
    out = bq.briefs(D30)[0]
    assert out["brief_date"] == D30
    assert out["published_at"] == "2026-09-30T06:14:40+02:00"
    assert out["payload"] == {"cards": []}


def test_bigquery_store_health_reports_error():
    class Broken(FakeClient):
        def query(self, sql, job_config=None):
            raise RuntimeError("boom")

    assert BigQueryStore(client=FakeClient([{"ok": 1}])).health() == "ok"
    assert BigQueryStore(client=Broken()).health() != "ok"


# ---------- Today ----------

def test_today_top_level(fx):
    t = today.build_today(fx, D30)
    assert t["date"] == D30
    assert t["status"] == "partial"  # KE is data_issue
    assert t["heading"] == "Taking off, 30 September 2026"
    assert t["published_at"] == "2026-09-30T06:14:40+02:00"
    assert t["warmup"] == {"active": True, "day": 2, "of": 14, "text": "Warming up: day 2 of 14"}
    assert t["first_morning"] is False
    assert [m["market"] for m in t["markets"]] == ["ZA", "NG", "KE"]
    assert [m["label"] for m in t["markets"]] == ["South Africa", "Nigeria", "Kenya"]
    h = t["headline"]
    assert h["market"] == "ZA" and h["item_id"] == ZA_A and h["claim_ids"] == ["c1"]
    assert h["text"]


def test_today_defaults_to_latest_date(fx):
    assert today.build_today(fx)["date"] == D30


def test_today_not_ready(fx):
    with pytest.raises(today.NotReady):
        today.build_today(fx, "2026-09-28")


def test_cards_tags_words(fx):
    za = market(today.build_today(fx, D30), "ZA")
    assert za["status"] == "published"
    assert len(za["cards"]) <= 5
    assert [c["rank"] for c in za["cards"] + za["more"]] == sorted(
        c["rank"] for c in za["cards"] + za["more"])
    tags = {c["item_id"]: c["tag"] for c in za["cards"] + za["more"]}
    assert ZA_C not in tags
    assert all(c["explained"] is True and c["specificity"]["status"] == "pass"
               for c in za["cards"] + za["more"])
    c1 = next(c for c in za["cards"] + za["more"] if c["item_id"] == ZA_A)
    assert c1["market"] == "ZA" and c1["date"] == D30
    assert c1["state"] == "emerging" and c1["state_word"] == "Emerging"
    assert c1["flag"] is None and c1["flag_word"] is None
    ng = market(today.build_today(fx, D30), "NG")
    assert all(c["explained"] is True and c["specificity"]["status"] == "pass"
               for c in ng["cards"] + ng["more"])


def test_card_shape(fx):
    keys = {"item_id", "market", "date", "rank", "kind", "title", "tag", "state", "state_word", "flag",
            "flag_word", "explained", "explanation", "explanation_claim_ids", "claims", "count_line",
            "numbers", "sparkline", "thumbnails", "evidence_ids", "evidence", "ask", "specificity"}
    t = today.build_today(fx, D30)
    for m in t["markets"]:
        for c in m["cards"] + m["more"]:
            assert keys <= set(c), keys - set(c)
            assert len(c["thumbnails"]) <= 2
            ev_ids = {e["id"] for e in c["evidence"]}
            assert set(c["thumbnails"]) <= ev_ids and set(c["evidence_ids"]) <= ev_ids


def test_today_card_words_match_discover_for_new_items_and_unusual_patterns():
    card = _passing_specificity_card()
    card.update(state="new_to_42", flag="check_pattern")
    za = market(today.build_today(_today_store([card]), D30), "ZA")
    (out,) = [c for c in za["cards"] + za["more"] if c["item_id"] == card["item_id"]]
    assert (out["state_word"], out["flag_word"]) == ("First spotted", "Unusual posting pattern")


def test_dropped(fx):
    t = today.build_today(fx, D30)
    za = market(t, "ZA")["dropped"]
    assert za["first_morning"] is False
    reasons = {i["item_id"]: i["reason"] for i in za["items"]}
    assert reasons == {ZA_H: "held_back", ZA_X: "reclassified_seasonal", ZA_Y: "not_confirmed"}
    assert ZA_C not in reasons
    assert all(i["title"] and i["reason_text"] for i in za["items"])
    ng = market(t, "NG")["dropped"]
    assert {i["item_id"]: i["reason"] for i in ng["items"]} == {NG_Z: "held_back", NG_Y: "not_confirmed"}
    ke = market(t, "KE")["dropped"]
    assert ke == {"first_morning": True, "text": "First morning: nothing to compare yet", "items": []}


def test_dropped_held_item_names_why_it_was_held(fx):
    t = today.build_today(fx, D30)
    za = {i["item_id"]: i["reason_text"] for i in market(t, "ZA")["dropped"]["items"]}
    assert za[ZA_H] == "Held back today: Too few creators"
    ng = {i["item_id"]: i["reason_text"] for i in market(t, "NG")["dropped"]["items"]}
    assert ng[NG_Z] == "Held back today: Paid-led: most posts are sponsored"


def test_dropped_held_item_uses_plain_gate_words_or_points_below():
    prev = (None, {"payload": {"cards": [{"item_id": "a", "title": "#a"}, {"item_id": "b", "title": "#b"}]}})
    payload = {"held_back": {"items": [
        {"item_id": "a", "reason": "paid_led", "reason_text": "Paid-led: sponsored or brand-owned share 0.62"},
        {"item_id": "b", "reason": "held_by_gate"}]}}
    got = {i["item_id"]: i["reason_text"] for i in today._dropped(payload, prev, set(), [])["items"]}
    assert got == {"a": "Held back today: Mostly sponsored or brand posts (62%)",
                   "b": "Held back today; see why below"}
    assert payload["held_back"]["items"][0]["reason_text"] == "Paid-led: sponsored or brand-owned share 0.62"


def test_held_back(fx):
    t = today.build_today(fx, D30)
    za = market(t, "ZA")["held_back"]
    assert za["count"] == len(za["items"])
    assert {i["reason"] for i in za["items"]} >= {"too_few_creators", "data_issue"}
    ng = market(t, "NG")["held_back"]
    assert ng["items"][0]["reason"] == "paid_led" and ng["items"][0]["rule"] == "G5"
    for m in t["markets"]:
        for i in m["held_back"]["items"]:
            assert {"item_id", "title", "rule", "reason", "reason_text", "evidence_ids", "evidence"} <= set(i)


def test_today_reader_rejections_are_held_with_reasons_and_sources_in_order():
    first = _passing_specificity_card()
    first["item_id"] = "reader-held-first"
    first["failed_reason"] = "The saved explanation did not meet the source check"
    first["specificity"].update(
        status="fail", reason="insufficient_local_evidence", reason_text="Only one required local source could be verified"
    )
    first["legacy_detail"] = {"source": "older producer field"}
    first["evidence"][0]["platform"] = "twitter"
    first["platforms"] = ["twitter"]

    second = deepcopy(first)
    second["item_id"] = "reader-held-second"
    second["specificity"].update(reason="quote_not_verbatim", reason_text="Quoted wording could not be verified")
    for index, record in enumerate(first["evidence"]):
        record["url"] = f"https://example.invalid/held-first/{index}"
    for index, record in enumerate(second["evidence"]):
        record["url"] = f"https://example.invalid/held-second/{index}"
    second["evidence"].append(None)

    empty_held = {"count": 0, "text": "Nothing held back", "items": []}
    za = market(today.build_today(_today_store([first], [second], held_back=empty_held), D30), "ZA")

    assert za["cards"] + za["more"] == []
    assert [item["item_id"] for item in za["held_back"]["items"]] == [
        "reader-held-first", "reader-held-second"
    ]
    assert za["held_back"]["count"] == len(za["held_back"]["items"]) == 2
    assert za["held_back"]["text"] == "2 items held back"
    held_by_id = {item["item_id"]: item for item in za["held_back"]["items"]}
    assert (held_by_id["reader-held-first"]["reason"], held_by_id["reader-held-first"]["reason_text"]) == (
        "insufficient_local_evidence", "Only one required local source could be verified"
    )
    assert (held_by_id["reader-held-second"]["reason"], held_by_id["reader-held-second"]["reason_text"]) == (
        "quote_not_verbatim", "Quoted wording could not be verified"
    )
    assert held_by_id["reader-held-first"]["failed_reason"] == first["failed_reason"]
    assert held_by_id["reader-held-first"]["legacy_detail"] == {"source": "older producer field"}
    assert held_by_id["reader-held-first"]["specificity"]["status"] == "fail"
    assert held_by_id["reader-held-first"]["platforms"] == ["x"]
    assert held_by_id["reader-held-first"]["evidence"][0]["platform"] == "x"
    for item_id, source in (("reader-held-first", first), ("reader-held-second", second)):
        assert {record["url"] for record in held_by_id[item_id]["evidence"]} == {
            record["url"] for record in source["evidence"] if isinstance(record, dict)
        }


def test_today_explicit_held_item_wins_reader_rejection_duplicate():
    card = _passing_specificity_card()
    card["item_id"] = "shared-held-id"
    card["numbers"] = [{"value": 12, "unit": "posts", "query_id": "q_reader", "run_id": "r_reader",
                        "result_hash": "sha256:reader"}]
    card["count_line"] = "Reader count line"
    card["failed_reason"] = "Reader supplied failure detail"
    card["specificity"].update(status="fail", reason="quote_not_verbatim")
    producer_item = {
        "item_id": "shared-held-id", "title": "Producer title", "rule": "G5", "reason": "paid_led",
        "reason_text": "Producer supplied hold reason", "evidence_ids": [], "evidence": [],
        "numbers": [{"value": 99, "unit": "posts", "query_id": "q_producer", "run_id": "r_producer",
                     "result_hash": "sha256:producer"}],
    }
    held_back = {"count": 1, "text": "Producer summary", "items": [producer_item]}

    za = market(today.build_today(_today_store([card], held_back=held_back), D30), "ZA")

    assert za["cards"] + za["more"] == []
    assert za["held_back"]["count"] == len(za["held_back"]["items"]) == 1
    assert za["held_back"]["items"][0]["reason"] == "paid_led"
    assert za["held_back"]["items"][0]["reason_text"] == "Producer supplied hold reason"
    assert za["held_back"]["items"][0]["title"] == "Producer title"
    assert za["held_back"]["items"][0]["numbers"] == producer_item["numbers"]
    assert za["held_back"]["items"][0]["count_line"] == "Reader count line"
    assert za["held_back"]["items"][0]["failed_reason"] == "Reader supplied failure detail"
    assert za["held_back"]["items"][0]["evidence"] == []


def test_today_enriches_unmeasured_and_measured_numbers_from_rejected_card():
    card = _passing_specificity_card()
    card["item_id"] = "held-with-partial-numbers"
    card["numbers"] = [
        {"value": None, "unit": "posts", "query_id": "q_missing", "run_id": "r_missing",
         "result_hash": "sha256:missing"},
        {"value": 12, "unit": "creators", "query_id": "q_measured", "run_id": "r_measured",
         "result_hash": "sha256:measured"},
    ]
    card["specificity"].update(status="fail", reason="quote_not_verbatim")
    held_back = {"count": 1, "items": [{"item_id": card["item_id"], "reason": "paid_led", "evidence": []}]}

    za = market(today.build_today(_today_store([card], held_back=held_back), D30), "ZA")

    assert za["held_back"]["items"][0]["numbers"] == card["numbers"]


@pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
def test_today_skips_rejected_numbers_with_invalid_values(value):
    card = _passing_specificity_card()
    card["item_id"] = "held-with-invalid-number"
    card["numbers"] = [{"value": value, "unit": "posts", "query_id": "q_invalid", "run_id": "r_invalid",
                        "result_hash": "sha256:invalid"}]
    card["specificity"].update(status="fail", reason="quote_not_verbatim")
    held_back = {"count": 1, "items": [{"item_id": card["item_id"], "reason": "paid_led", "evidence": []}]}

    za = market(today.build_today(_today_store([card], held_back=held_back), D30), "ZA")

    assert "numbers" not in za["held_back"]["items"][0]


def test_today_explicit_held_item_does_not_take_details_from_another_market_or_item():
    base = FixtureStore()
    explicit_item = {
        "item_id": "shared-held-id", "title": "Producer title", "reason": "paid_led",
        "reason_text": "Producer supplied hold reason", "evidence_ids": [], "evidence": [],
    }
    other_item = _passing_specificity_card()
    other_item.update(item_id="different-held-id", numbers=[{"value": 7}], count_line="Other item",
                      failed_reason="Other item detail")
    other_item["specificity"].update(status="fail", reason="quote_not_verbatim")
    other_market_item = _passing_specificity_card()
    other_market_item.update(item_id="shared-held-id", numbers=[{"value": 8}], count_line="Other market",
                             failed_reason="Other market detail")
    other_market_item["specificity"].update(status="fail", reason="quote_not_verbatim")

    def briefs(date):
        rows = deepcopy(base.briefs(date))
        if date == D30:
            for row in rows:
                if row["market"] == "ZA":
                    row["payload"]["held_back"] = {"count": 1, "items": [deepcopy(explicit_item)]}
                    row["payload"]["cards"] = [deepcopy(other_item)]
                    row["payload"]["more"] = []
                elif row["market"] == "NG":
                    row["payload"]["cards"] = [deepcopy(other_market_item)]
                    row["payload"]["more"] = []
        return rows

    za = market(today.build_today(Patched(briefs=briefs), D30), "ZA")

    held_by_id = {item["item_id"]: item for item in za["held_back"]["items"]}
    assert za["held_back"]["count"] == len(za["held_back"]["items"])
    assert "numbers" not in held_by_id["shared-held-id"]
    assert "count_line" not in held_by_id["shared-held-id"]
    assert "failed_reason" not in held_by_id["shared-held-id"]


def test_today_explicit_held_item_skips_malformed_rejected_details():
    card = _passing_specificity_card()
    card.update(item_id="malformed-held-id", numbers="not numbers", count_line={"text": "invalid"},
                failed_reason=["invalid"])
    card["specificity"].update(status="fail", reason="quote_not_verbatim")
    explicit_item = {"item_id": "malformed-held-id", "reason": "paid_led", "evidence": []}
    held_back = {"count": 1, "items": [explicit_item]}

    za = market(today.build_today(_today_store([card], held_back=held_back), D30), "ZA")
    item = za["held_back"]["items"][0]

    assert za["held_back"]["count"] == 1
    assert "numbers" not in item
    assert "count_line" not in item
    assert "failed_reason" not in item


def test_today_reader_rejection_without_reason_uses_neutral_text_and_safe_evidence():
    card = _passing_specificity_card()
    card["item_id"] = "reader-held-unknown"
    card.pop("specificity")
    card["failed_reason"] = ["malformed"]
    card["evidence"] = [None, {"id": "source-1", "platform": [], "url": "https://example.invalid/source"}]
    empty_held = {"count": 0, "text": "Nothing held back", "items": []}

    za = market(today.build_today(_today_store([card], held_back=empty_held), D30), "ZA")
    item = za["held_back"]["items"][0]

    assert za["cards"] + za["more"] == []
    assert item["reason"] is None
    assert item["reason_text"] == "The posts behind it could not be read"
    assert "failed_reason" not in item
    assert item["evidence"] == [
        {"id": "source-1", "platform": None, "url": "https://example.invalid/source"}
    ]


@pytest.mark.parametrize(("mapping_case", "expected_title"), [
    ("matched", "Verified channel"),
    ("wrong_item_id", "YouTube channel"),
    ("wrong_kind", "YouTube channel"),
    ("id_label", "YouTube channel"),
    ("missing", "YouTube channel"),
    ("unavailable", "YouTube channel"),
    ("conflicting_labels", "YouTube channel"),
])
def test_today_youtube_channel_title_requires_same_creator_map_row(mapping_case, expected_title):
    item_id = "creator-map-target"
    channel_id = FAKE_CHANNEL
    card = _youtube_creator_card(item_id, channel_id)
    store = _today_store([card])
    calls = []

    def map_items(item_ids):
        calls.append(list(item_ids))
        rows = {
            "matched": [{"item_id": item_id, "kind": "creator", "label": "Verified channel"}],
            "wrong_item_id": [{"item_id": "other-item", "creator_id": item_id, "kind": "creator",
                               "label": "Unrelated channel"}],
            "wrong_kind": [{"item_id": item_id, "kind": "topic", "label": "Topic label"}],
            "id_label": [{"item_id": item_id, "kind": "creator", "label": channel_id}],
            "missing": [],
            "unavailable": None,
            "conflicting_labels": [
                {"item_id": item_id, "kind": "creator", "label": "First label"},
                {"item_id": item_id, "kind": "creator", "label": "Second label"},
            ],
        }
        return rows[mapping_case]

    store.map_items = map_items

    za = market(today.build_today(store, D30), "ZA")

    assert za["cards"][0]["title"] == expected_title
    assert calls == [[item_id]]


def test_today_batches_only_exact_youtube_creator_map_ids_across_cards_and_held_items():
    channel_id = FAKE_CHANNEL
    first = _youtube_creator_card("creator-one", channel_id)
    second = _youtube_creator_card("creator-two", channel_id)
    normal = _passing_specificity_card()
    normal.update(item_id="normal-item", title="A readable trend")
    explicit_held = {
        "item_id": "creator-held", "kind": "creator", "title": channel_id, "rule": "G1",
        "reason": "data_issue", "reason_text": "Producer supplied hold reason", "evidence_ids": [], "evidence": [],
    }
    held_back = {"count": 1, "text": "Producer summary", "items": [explicit_held]}
    store = _today_store([first], [second, normal], held_back=held_back)
    calls = []

    def map_items(item_ids):
        calls.append(list(item_ids))
        return [{"item_id": item_id, "kind": "creator", "label": f"Verified {item_id}"}
                for item_id in item_ids]

    store.map_items = map_items

    za = market(today.build_today(store, D30), "ZA")

    assert calls == [["creator-one", "creator-two", "creator-held"]]
    assert {item["item_id"]: item["title"] for item in za["cards"] + za["more"]} == {
        "creator-one": "Verified creator-one", "creator-two": "Verified creator-two", "normal-item": "A readable trend"
    }
    assert za["held_back"]["items"][0]["title"] == "Verified creator-held"


def test_today_does_not_query_creator_map_for_normal_cards():
    card = _passing_specificity_card()
    card.update(item_id="normal-item", title="A readable trend")
    store = _today_store([card])
    calls = []
    store.map_items = lambda item_ids: calls.append(list(item_ids))

    za = market(today.build_today(store, D30), "ZA")

    assert za["cards"][0]["title"] == "A readable trend"
    assert calls == []


def test_today_rejects_bad_rank_with_neutral_unknown_reason_and_title():
    card = _passing_specificity_card()
    card.update(item_id="bad-rank-creator", kind="creator", title=None, rank="1", platforms=[{}], evidence=[])
    empty_held = {"count": 0, "text": "Nothing held back", "items": []}

    za = market(today.build_today(_today_store([card], held_back=empty_held), D30), "ZA")
    item = next(item for item in za["held_back"]["items"] if item["item_id"] == "bad-rank-creator")

    assert za["cards"] + za["more"] == []
    assert item["specificity"]["status"] == "pass"
    assert item["reason"] is None
    assert item["reason_text"] == "The trend record is incomplete"
    assert item["title"] == "Creator item"
    assert item["platforms"] == []


def test_held_back_numeric_title_uses_only_the_matching_evidence_handle(fx):
    post_id = "71001"

    def briefs(date):
        rows = fx.briefs(date)
        item = next(i for r in rows if r["market"] == "ZA"
                    for i in r["payload"]["held_back"]["items"] if i["item_id"] == ZA_H)
        item["title"] = post_id
        item["evidence"] = [
            {"id": "71002", "platform": "tiktok", "handle": "unrelated"},
            {"id": post_id, "platform": "tiktok", "handle": "matching"},
        ]
        item["evidence_ids"] = [e["id"] for e in item["evidence"]]
        return rows

    store = Patched(briefs=briefs)
    held = next(i for i in market(today.build_today(store, D30), "ZA")["held_back"]["items"]
                if i["item_id"] == ZA_H)
    assert held["title"] == "TikTok post by @matching"
    assert held["item_id"] == ZA_H and held["evidence_ids"] == ["71002", post_id]
    detail = today.build_trend(store, ZA_H, "ZA", D30)
    assert detail["title"] == held["title"]
    assert detail["ask"] == f"What is behind {held['title']} in South Africa this week?"


def test_held_back_numeric_title_without_matching_evidence_uses_platform_only(fx):
    post_id = "71003"

    def briefs(date):
        rows = fx.briefs(date)
        item = next(i for r in rows if r["market"] == "ZA"
                    for i in r["payload"]["held_back"]["items"] if i["item_id"] == ZA_H)
        item["title"] = post_id
        item["evidence"] = [{"id": "71004", "platform": "tiktok", "handle": "unrelated"}]
        item["evidence_ids"] = [e["id"] for e in item["evidence"]]
        return rows

    held = next(i for i in market(today.build_today(Patched(briefs=briefs), D30), "ZA")["held_back"]["items"]
                if i["item_id"] == ZA_H)
    assert held["title"] == "TikTok post"
    assert held["title"] != post_id and "unrelated" not in held["title"]


def test_held_back_numeric_youtube_channel_id_handle_uses_neutral_title_and_keeps_links(fx):
    post_id = "71005"
    channel_id = "UC" + "a1B2c3D4e5F6g7H8i9J0k_"
    cases = [
        ("held_youtube_id_handle", "youtube", channel_id, "YouTube post"),
        ("held_youtube_named_handle", "youtube", "fixture_creator", "YouTube post by @fixture_creator"),
        ("held_tiktok_id_shaped_handle", "tiktok", channel_id, f"TikTok post by @{channel_id}"),
    ]

    def briefs(date):
        rows = deepcopy(fx.briefs(date))
        if date == D30:
            row = next(r for r in rows if r["market"] == "ZA")
            template = next(i for i in row["payload"]["held_back"]["items"] if i["item_id"] == ZA_H)
            items = []
            for item_id, platform, handle, _ in cases:
                evidence = [{
                    "id": post_id, "platform": platform, "handle": handle,
                    "url": f"https://example.invalid/{platform}/post/{item_id}",
                }]
                item = deepcopy(template)
                item.update(item_id=item_id, title=post_id, evidence=evidence, evidence_ids=[post_id])
                items.append(item)
            row["payload"]["held_back"]["items"] = items
        return rows

    za = market(today.build_today(Patched(briefs=briefs), D30), "ZA")
    held = {item["item_id"]: item for item in za["held_back"]["items"]}

    assert {item_id: held[item_id]["title"] for item_id, _, _, _ in cases} == {
        item_id: expected for item_id, _, _, expected in cases
    }
    expected_evidence = [{
        "id": post_id, "platform": "youtube", "handle": channel_id,
        "url": "https://example.invalid/youtube/post/held_youtube_id_handle",
    }]
    assert held["held_youtube_id_handle"]["evidence"] == expected_evidence
    assert held["held_youtube_id_handle"]["evidence_ids"] == [post_id]


def test_coverage(fx):
    rows = [r for r in fx.collection_health(D30) if r["market"] == "ZA"]
    valid = [r for r in rows if r["valid"]]
    invalid = [r for r in rows if not r["valid"]]
    want_share = sum(r["items"] * r["located_share"] for r in valid) / sum(r["items"] for r in valid)
    cov = market(today.build_today(fx, D30), "ZA")["coverage"]
    assert cov["posts"] == sum(r["items"] for r in valid)
    assert cov["platforms"] == sorted({r["platform"] for r in valid})
    assert cov["located_share"] == pytest.approx(want_share, abs=1e-4)
    assert cov["invalid_series"] == len(invalid) == 1
    assert cov["issues"] == ["TikTok hashtag board: could not be read"]


def health_row(platform, series, valid, items=10, reason=None):
    return {"day": D30, "market": "ZA", "platform": platform, "series": series, "valid": valid,
            "invalid_reason": reason, "items": items, "located_share": 0.5}


def test_coverage_names_series_without_platform():
    rows = [health_row("instagram", "ig_location", False, reason="calls"),
            health_row(None, "counter_post_views", False, reason="calls"),
            health_row(None, "panel_culture_desk", False, reason="calls"),
            health_row(None, "search", False, reason="calls"),
            health_row(None, "new_cross_route", False, reason="items"),
            health_row("tiktok", "search", False, reason="calls"),
            health_row("tiktok", "feed_tiktok", True),
            health_row(None, "counter_post_views", True)]
    store = Patched(collection_health=lambda date: rows)
    cov = market(today.build_today(store, D30), "ZA")["coverage"]
    assert cov["issues"] == ["Instagram location posts: could not be read", "Post view re-reads: could not be read",
                             "Culture accounts we follow: could not be read", "Searches: could not be read",
                             "New cross route: post count far from usual", "TikTok search: could not be read"]
    assert not any("None" in i for i in cov["issues"])
    assert cov["platforms"] == ["tiktok"]
    assert cov["posts"] == 20


STAGING_SERIES = [  # the 17 distinct (platform, series) pairs in staging collection_health, 28 September
    (None, "counter_post_views", "Post view re-reads"),
    (None, "panel_culture_desk", "Culture accounts we follow"),
    (None, "search", "Searches"),
    ("apple_music", "board_apple_music", "Apple Music chart"),
    ("facebook", "panel_fb_hub", "Facebook"),
    ("instagram", "board_global_music", "Instagram global music board"),
    ("instagram", "ig_location", "Instagram location posts"),
    ("reddit", "list_reddit", "Reddit"),
    ("tiktok", "board_tiktok_hashtag", "TikTok hashtag board"),
    ("tiktok", "counter_tiktok_hashtag", "TikTok hashtag totals"),
    ("tiktok", "counter_tiktok_sound", "TikTok sound totals"),
    ("tiktok", "curve_tiktok_sound", "TikTok sound popularity (no longer collected)"),
    ("tiktok", "feed_tiktok", "TikTok"),
    ("tiktok", "search", "TikTok search"),
    ("twitter", "panel_x_hub", "X"),
    ("youtube", "board_global_music", "YouTube global music board"),
    ("youtube", "board_youtube", "YouTube trending board"),
]


def test_coverage_names_every_staging_series():
    rows = [health_row(p, s, False, reason="calls") for p, s, _ in STAGING_SERIES]
    cov = market(today.build_today(Patched(collection_health=lambda date: rows), D30), "ZA")["coverage"]
    assert cov["issues"] == [f"{words}: could not be read" for _, _, words in STAGING_SERIES]
    assert not any("_" in i or "None" in i for i in cov["issues"])


def test_series_words_are_plain_and_never_repeat_within_a_platform():
    words = [w for _, _, w in STAGING_SERIES]
    assert len(words) == len(set(words))
    assert len(set(today.SERIES_WORDS.values())) == len(today.SERIES_WORDS)
    for jargon in ("local feed", "hub", "desk", "panel", "curve", "rising and hot"):
        assert not any(jargon in w for w in today.SERIES_WORDS.values()), jargon


def news_row(protocol, valid, reason=None, calls=1, calls_ok=1, items=20):
    return {**health_row("news", "news_rss", valid, items=items, reason=reason), "route": "public_feed",
            "protocol": protocol, "calls": calls, "calls_ok": calls_ok}


def feed(feed_id, url):
    return "public_feed?" + urlencode({"feed_id": feed_id, "url": url})


def test_news_feeds_are_named_once_with_how_many_were_read():
    # 4 Oct 2026, ZA: one news feed failed and Today said only "News: News rss: could not be read", once per
    # failed feed, naming none of them. Each feed is its own row (one protocol a feed).
    rows = [news_row(feed("za_business_day", "https://www.businessday.co.za/"), False, "calls: http_4xx", calls_ok=0),
            news_row(feed("za_current_affairs_za", "https://currentaffairsza.com/"), False, "calls: robots",
                     calls=0, calls_ok=0),
            news_row(feed("za_enca", "https://www.enca.com/"), True),
            {**news_row("rss?url=https://briefly.co.za/rss/all.rss", True), "route": "rss"},
            health_row("tiktok", "feed_tiktok", True)]
    za = market(today.build_today(Patched(collection_health=lambda date: rows), D30), "ZA")
    assert za["coverage"]["issues"] == [
        "News feeds: 2 of 4 read; could not read Business Day (the site refused it), "
        "Current Affairs ZA (robots.txt did not allow it)"]
    # Only the feed whose call was made and failed is a failed source; the robots refusal made no call.
    assert "1 source failed today: Business Day" in [b["text"] for b in za["banners"]]


def test_news_feeds_judged_on_items_are_named_apart_from_unread_ones():
    rows = [news_row(feed("za_timeslive", "https://www.timeslive.co.za/"), False, "items", items=2),
            news_row("rss?url=https://www.zalebs.com/feed/", False, "calls: timeout", calls_ok=0),
            news_row("rss?url=https://briefly.co.za/rss/all.rss", False, "calls: http_5xx,timeout", calls_ok=0),
            news_row(feed("za_enca", "https://www.enca.com/"), True)]
    cov = market(today.build_today(Patched(collection_health=lambda date: rows), D30), "ZA")["coverage"]
    assert cov["issues"] == [
        "News feeds: 2 of 4 read; could not read briefly.co.za, zalebs.com (no answer in time)",
        "News feeds: post count far from usual: TimesLIVE"]


def test_coverage_unknown_series_with_platform():
    rows = [health_row("tiktok", "new_feed_thing", False, reason="drift"),
            health_row("twitter", "new_list", False, reason="calls")]
    cov = market(today.build_today(Patched(collection_health=lambda date: rows), D30), "ZA")["coverage"]
    assert cov["issues"] == ["TikTok: New feed thing: mix of posts far from usual", "X: New list: could not be read"]


def test_coverage_platforms_report_x_for_twitter():
    rows = [health_row("twitter", "panel_x_hub", True), health_row("x", "panel_x_hub", True),
            health_row("tiktok", "feed_tiktok", True), health_row(None, "search", True)]
    cov = market(today.build_today(Patched(collection_health=lambda date: rows), D30), "ZA")["coverage"]
    assert cov["platforms"] == ["tiktok", "x"]


def test_banners(fx):
    t = today.build_today(fx, D30)
    kinds = {m["market"]: [b["kind"] for b in m["banners"]] for m in t["markets"]}
    assert kinds["ZA"] == ["warming_up", "data_issue"]
    assert kinds["NG"] == ["warming_up", "thin_coverage"]
    assert kinds["KE"] == ["warming_up", "thin_coverage", "data_issue"]
    za = market(t, "ZA")
    assert za["banners"][0]["text"] == "Warming up: day 2 of 14"
    assert za["banners"][-1]["text"] == "1 source failed today: TikTok hashtag board"
    ke = market(t, "KE")
    assert ke["banners"][-1]["text"] == "1 source failed today: TikTok"
    assert not any("collection or detection failed" in b["text"] for m in t["markets"] for b in m["banners"])
    assert all(b["text"] for m in t["markets"] for b in m["banners"])


def test_source_failures_are_named_from_calls_health_without_claiming_stage_failure(fx):
    ig = {**health_row("instagram", "ig_location", False, reason="calls"), "calls": 1, "calls_ok": 0,
          "route": "instagram/location_posts", "protocol": "location_posts"}
    ig_retry = {**ig, "protocol": "location_search"}
    ewn = {**health_row(None, "news_rss", False, reason="calls"), "calls": 1, "calls_ok": 0,
            "route": "rss", "protocol": "rss?url=https://ewn.co.za/RSS%20Feeds/Latest%20News"}
    not_call_failure = {**health_row("reddit", "list_reddit", False, reason="items"), "calls": 4,
                        "calls_ok": 4, "route": "reddit/subreddit", "protocol": "list_reddit_p1"}
    real_briefs = fx.briefs
    candidate_issue = "Data issue: 2 of 4 candidates held for invalid collection days or unreadable evidence"

    def briefs(date):
        rows = real_briefs(date)
        for row in rows:
            if row["market"] == "ZA":
                row["status"] = "data_issue"
                row["payload"]["banners"] = [{"kind": "data_issue", "text": candidate_issue}]
        return rows

    store = Patched(collection_health=lambda date: [ig, ig_retry, ewn, not_call_failure], briefs=briefs)

    za = market(today.build_today(store, D30), "ZA")
    issues = [b["text"] for b in za["banners"] if b["kind"] == "data_issue"]
    assert "2 sources failed today: EWN, Instagram locations" in issues
    assert candidate_issue in issues
    assert not any("collection or detection failed" in text for text in issues)
    assert "Warming up: day 2 of 14" in [b["text"] for b in za["banners"]]


def test_latest_detect_failure_keeps_stage_failure_wording(fx):
    real_briefs = fx.briefs

    def briefs(date):
        rows = real_briefs(date)
        for row in rows:
            if row["brief_date"] == D30:
                row["status"] = "data_issue"
                row["payload"]["banners"] = [{"kind": "data_issue",
                                               "text": "Data issue: the steps before the brief did not finish by 06:15, so no trends were checked today"}]
        return rows

    def runs(stage, run_date):
        if stage == "collect":
            return [{"run_id": "collect-ok", "stage": stage, "run_date": run_date, "status": "ok",
                     "started_at": "2026-09-30 02:00:00", "finished_at": "2026-09-30 02:10:00"}]
        if stage == "detect":
            return [{"run_id": "detect-failed", "stage": stage, "run_date": run_date, "status": "failed",
                     "started_at": "2026-09-30 02:10:00", "finished_at": "2026-09-30 02:20:00"}]
        return []

    za = market(today.build_today(Patched(briefs=briefs, runs=runs), D30), "ZA")
    assert "Data issue: collection or detection failed for South Africa" in [
        b["text"] for b in za["banners"] if b["kind"] == "data_issue"]


def test_latest_successful_stage_attempt_suppresses_earlier_failure_wording(fx):
    real_briefs = fx.briefs

    def briefs(date):
        rows = real_briefs(date)
        for row in rows:
            if row["brief_date"] == D30 and row["market"] == "ZA":
                row["status"] = "data_issue"
                row["payload"]["banners"] = [{"kind": "data_issue",
                                               "text": "Data issue: the steps before the brief did not finish by 06:15, so no trends were checked today"}]
        return rows

    def runs(stage, run_date):
        if stage not in {"collect", "detect"}:
            return []
        return [
            {"run_id": f"{stage}-failed", "stage": stage, "run_date": run_date, "status": "failed",
             "started_at": "2026-09-30 01:00:00", "finished_at": "2026-09-30 01:10:00"},
            {"run_id": f"{stage}-ok", "stage": stage, "run_date": run_date, "status": "ok",
             "started_at": "2026-09-30 02:00:00", "finished_at": "2026-09-30 02:10:00"},
        ]

    za = market(today.build_today(Patched(briefs=briefs, runs=runs), D30), "ZA")
    issues = [b["text"] for b in za["banners"] if b["kind"] == "data_issue"]
    assert not any("collection or detection failed" in text for text in issues)
    assert not any("steps before the brief did not finish" in text for text in issues)


def test_missing_stage_status_stays_neutral_when_no_source_calls_failed(fx):
    def collection_health(date):
        rows = fx.collection_health(date)
        return [dict(r, invalid_reason="items") if r.get("invalid_reason") == "calls" else r for r in rows]

    t = today.build_today(Patched(collection_health=collection_health), D30)
    ke = market(t, "KE")
    assert "Data issue: today's brief is incomplete for Kenya" in [b["text"] for b in ke["banners"]]
    assert not any("collection or detection failed" in b["text"] for m in t["markets"] for b in m["banners"])
    assert market(t, "ZA")["banners"][0]["text"] == "Warming up: day 2 of 14"


def test_moments(fx):
    t = today.build_today(fx, D30)
    za = market(t, "ZA")["moments"]
    assert [(m["date"], m["kind"]) for m in za] == [("2026-10-03", "festival")]
    assert set(za[0]["item_ids"]) == {ZA_C, ZA_X}
    assert za[0]["source"] == "calendar" and za[0]["name"]
    assert [m["date"] for m in market(t, "NG")["moments"]] == ["2026-10-01"]
    assert [m["date"] for m in market(t, "KE")["moments"]] == ["2026-10-09"]


def test_boards_pass_through(fx):
    za = market(today.build_today(fx, D30), "ZA")
    assert za["boards"] and za["boards"][0]["platform"] == "tiktok"


# Shaped like the staging briefs of 29 September: one list per board, a YouTube board whose best ranks tie across
# several reads (1, 1, 1, 2, 2), and titles that are a channel id in either case or a 64-hex item hash. Made up ids.
FAKE_CHANNEL = "UC" + "a1B2c3D4e5F6g7H8i9J0k_"
YOUTUBE_BOARD = {"platform": "youtube", "list": "YouTube trending board", "entries": [
    {"rank": 1, "title": "#fixture_yt_one", "item_id": iid("hashtag", "#fixture_yt_one")},
    {"rank": 1, "title": FAKE_CHANNEL.lower(), "item_id": iid("creator", "c1")},
    {"rank": 1, "title": "#fixture_yt_two", "item_id": iid("hashtag", "#fixture_yt_two")},
    {"rank": 2, "title": FAKE_CHANNEL, "item_id": iid("creator", "c2")},
    {"rank": 2, "title": "  ", "item_id": iid("creator", "c3")},
    {"rank": 2, "title": iid("creator", "c4"), "item_id": iid("creator", "c4")},
    {"rank": 2, "title": "fixture yt name", "item_id": iid("creator", "c5")}]}
ID_BOARD = {"platform": "youtube", "list": "YouTube trending board", "entries": [
    {"rank": 1, "title": FAKE_CHANNEL.lower(), "item_id": iid("creator", "c6")}]}


def boards_store(boards):
    base = FixtureStore()

    def briefs(date):
        rows = base.briefs(date)
        for r in rows:
            if r["market"] == "ZA":
                r["payload"]["boards"] = json.loads(json.dumps(boards))
        return rows

    return Patched(briefs=briefs)


def test_board_entries_without_a_readable_name_are_left_out_and_counted():
    tiktok = {"platform": "tiktok", "list": "TikTok hashtag board", "entries": [
        {"rank": 1, "title": "#fixture_tt_one", "item_id": iid("hashtag", "#fixture_tt_one")}]}
    za = market(today.build_today(boards_store([tiktok, YOUTUBE_BOARD]), D30), "ZA")
    tt, yt = za["boards"]
    assert [e["title"] for e in yt["entries"]] == ["#fixture_yt_one", "#fixture_yt_two", "fixture yt name"]
    assert (yt["left_out"], yt["left_out_reason"]) == (4, "No readable name")
    assert (tt["left_out"], tt["left_out_reason"]) == (0, None)
    assert [e["title"] for e in tt["entries"]] == ["#fixture_tt_one"]


def test_numeric_board_title_is_kept_without_changing_left_out_count():
    before = market(today.build_today(boards_store([YOUTUBE_BOARD]), D30), "ZA")["boards"][0]
    board = dict(YOUTUBE_BOARD, entries=[
        *(YOUTUBE_BOARD["entries"]),
        {"rank": 3, "title": "42", "item_id": iid("sound", "42")},
    ])
    after = market(today.build_today(boards_store([board]), D30), "ZA")["boards"][0]

    assert any(entry["title"] == "42" for entry in after["entries"])
    assert (after["left_out"], after["left_out_reason"]) == (before["left_out"], before["left_out_reason"])
    assert (after["left_out"], after["left_out_reason"]) == (4, "No readable name")


def test_board_entries_keep_their_own_best_rank_with_ties_and_gaps():
    """rank is L2's best rank today (MIN over the day's reads); leaving entries out never renumbers the rest."""
    tiktok = {"platform": "tiktok", "list": "TikTok hashtag board", "entries": [
        {"rank": 1, "title": "#fixture_tt_one", "item_id": iid("hashtag", "#fixture_tt_one")},
        {"rank": 3, "title": "#fixture_tt_two", "item_id": iid("hashtag", "#fixture_tt_two")}]}
    za = market(today.build_today(boards_store([tiktok, YOUTUBE_BOARD]), D30), "ZA")
    tt, yt = za["boards"]
    assert [e["rank"] for e in tt["entries"]] == [1, 3]
    assert [(e["rank"], e["title"]) for e in yt["entries"]] == [
        (1, "#fixture_yt_one"), (1, "#fixture_yt_two"), (2, "fixture yt name")]
    source = {e["item_id"]: e["rank"] for b in (tiktok, YOUTUBE_BOARD) for e in b["entries"]}
    assert all(e["rank"] == source[e["item_id"]] for b in za["boards"] for e in b["entries"])


def test_a_board_of_only_ids_stays_with_its_count():
    import datetime as dt
    on_the_day = dt.datetime(2026, 9, 30, 9, 0, tzinfo=today.SAST)  # "today" words the reason on the brief's own day
    za = market(today.build_today(boards_store([ID_BOARD]), D30, now=on_the_day), "ZA")
    # Every entry left out: the reason says none had a name, not that a few were missing one.
    assert za["boards"] == [{"platform": "youtube", "list": "YouTube trending board", "entries": [],
                             "left_out": 1, "left_out_reason": today.NO_NAMES_READ}]
    assert today.NO_NAMES_READ == "None of this list's entries had a readable name today"


def test_one_song_under_two_item_ids_shows_once_at_its_better_rank():
    """Visual QA, 5 October 2026: Apple Music listed "Solar Eclipse by Drake & Don Toliver" twice at =3."""
    song = "Solar Eclipse by Drake & Don Toliver"
    board = {"platform": "apple_music", "list": "Apple Music chart", "entries": [
        {"rank": 2, "title": "Slap The City by Drake & Qendresa", "item_id": iid("sound", "a1")},
        {"rank": 3, "title": song, "item_id": iid("sound", "a2")},
        {"rank": 3, "title": "  solar eclipse by Drake &  Don Toliver", "item_id": iid("sound", "a3")}]}
    b = market(today.build_today(boards_store([board]), D30), "ZA")["boards"][0]
    assert [(e["rank"], e["title"]) for e in b["entries"]] == [(2, "Slap The City by Drake & Qendresa"), (3, song)]
    assert (b["left_out"], b["left_out_reason"]) == (0, None)  # shown once, not left out


def test_duplicate_music_entries_keep_the_best_rank_in_every_input_order():
    entries = [
        {"rank": 8, "title": "Back 2 U by Seyi Vibez", "item_id": "song-a"},
        {"rank": 1, "title": "back  2 u by SEYI VIBEZ", "item_id": "song-b"},
        {"rank": 4, "title": "Back 2 U by Seyi Vibez", "item_id": "song-c"},
    ]
    for order in permutations(entries):
        board = {"platform": "apple_music", "list": "Apple Music chart", "entries": list(order)}
        result = today._boards([board])[0]
        assert result["entries"] == [entries[1]]
        assert (result["left_out"], result["left_out_reason"]) == (0, None)
        assert board["entries"] == list(order)


@pytest.mark.parametrize("bad_rank", [None, 0, -1, True, False, 1.0, "1"])
def test_duplicate_board_entry_prefers_a_valid_rank_to_an_invalid_one(bad_rank):
    invalid = {"rank": bad_rank, "title": "Back 2 U by Seyi Vibez", "item_id": "song-a"}
    valid = {"rank": 9, "title": "Back 2 U by Seyi Vibez", "item_id": "song-b"}
    for entries in ([invalid, valid], [valid, invalid]):
        result = today._boards([{"platform": "shazam", "list": "board shazam", "entries": entries}])[0]
        assert result["entries"] == [valid]


def test_duplicate_board_entry_rank_ties_keep_the_first_input_row():
    entries = [
        {"rank": 3, "title": "Back 2 U by Seyi Vibez", "item_id": "song-a"},
        {"rank": 3, "title": "back 2 u by seyi vibez", "item_id": "song-b"},
    ]
    for order in (entries, entries[::-1]):
        result = today._boards([{"platform": "apple_music", "list": "Apple Music chart", "entries": order}])[0]
        assert result["entries"] == [order[0]]


def test_duplicate_board_entry_without_valid_ranks_keeps_the_first_input_row():
    entries = [
        {"rank": None, "title": "Back 2 U by Seyi Vibez", "item_id": "song-a"},
        {"rank": 0, "title": "Back 2 U by Seyi Vibez", "item_id": "song-b"},
    ]
    result = today._boards([{"platform": "shazam", "list": "board shazam", "entries": entries}])[0]
    assert result["entries"] == [entries[0]]


def test_board_rank_dedupe_keeps_unrelated_source_order_and_different_artists():
    entries = [
        {"rank": 8, "title": "Hello by Artist A", "item_id": "song-a"},
        {"rank": 1, "title": "Hello by Artist B", "item_id": "song-b"},
        {"rank": 4, "title": "Hello", "item_id": "song-c"},
        {"rank": 2, "title": "Hello", "item_id": "song-d"},
    ]
    result = today._boards([{"platform": "apple_music", "list": "Apple Music chart", "entries": entries}])[0]
    assert result["entries"] == entries


def test_board_rank_dedupe_stays_within_each_platform_and_city_list():
    boards = [
        {"platform": "shazam", "list": "board shazam", "entries": [
            {"rank": 8, "title": "Back 2 U by Seyi Vibez", "item_id": "song-a"},
            {"rank": 1, "title": "Back 2 U by Seyi Vibez", "item_id": "song-b"}]},
        {"platform": "shazam", "list": "board shazam city", "city": "Lagos", "entries": [
            {"rank": 5, "title": "Back 2 U by Seyi Vibez", "item_id": "song-a"},
            {"rank": 2, "title": "Back 2 U by Seyi Vibez", "item_id": "song-b"}]},
        {"platform": "shazam", "list": "board shazam city", "city": "Abuja", "entries": [
            {"rank": 7, "title": "Back 2 U by Seyi Vibez", "item_id": "song-a"},
            {"rank": 4, "title": "Back 2 U by Seyi Vibez", "item_id": "song-b"}]},
        {"platform": "apple_music", "list": "Apple Music chart", "entries": [
            {"rank": 9, "title": "Back 2 U by Seyi Vibez", "item_id": "song-a"},
            {"rank": 3, "title": "Back 2 U by Seyi Vibez", "item_id": "song-b"}]},
    ]
    result = today._boards(boards)
    assert [board["platform"] for board in result] == [board["platform"] for board in boards]
    assert [board.get("city") for board in result] == [board.get("city") for board in boards]
    assert [board["entries"] for board in result] == [[board["entries"][1]] for board in boards]


def test_only_the_same_entry_merges_on_a_board():
    """Review, 6 October 2026: the merge went by title alone, so two YouTube videos both called "Highlights" read as
    one. Off the music charts only the same item id merges; on them a title that names its artist does too, and a
    bare song title does not, since two artists can share it."""
    videos = {"platform": "youtube", "list": "YouTube trending board", "entries": [
        {"rank": 1, "title": "Highlights", "item_id": iid("topic", "v1")},
        {"rank": 2, "title": "highlights", "item_id": iid("topic", "v2")},
        {"rank": 3, "title": "Highlights", "item_id": iid("topic", "v1")}]}
    chart = {"platform": "boomplay", "list": "board boomplay", "entries": [
        {"rank": 1, "title": "Hello", "item_id": iid("sound", "h1")},
        {"rank": 2, "title": "Hello", "item_id": iid("sound", "h2")},
        {"rank": 3, "title": "Rapudo by Prince Indah", "item_id": iid("sound", "r1")},
        {"rank": 4, "title": "rapudo by prince  indah", "item_id": iid("sound", "r2")},
        {"rank": 5, "title": "Rapudo by Someone Else", "item_id": iid("sound", "r3")}]}
    yt, music = market(today.build_today(boards_store([videos, chart]), D30), "ZA")["boards"]
    assert [(e["rank"], e["title"]) for e in yt["entries"]] == [(1, "Highlights"), (2, "highlights")]
    assert [(e["rank"], e["title"]) for e in music["entries"]] == [
        (1, "Hello"), (2, "Hello"), (3, "Rapudo by Prince Indah"), (5, "Rapudo by Someone Else")]
    assert (yt["left_out"], music["left_out"]) == (0, 0)


def test_a_public_chart_feed_board_is_named_for_its_own_chart():
    """board_music_country holds each public chart feed; Mdundo's board read "Country music charts"."""
    entry = [{"rank": 1, "title": "Prince Indah - Rapudo", "item_id": iid("sound", "m1")}]
    boards = [{"platform": p, "list": "board music country", "entries": entry} for p in ("mdundo", "turntable", "x9")]
    got = [b["list"] for b in market(today.build_today(boards_store(boards), D30), "ZA")["boards"]]
    assert got == ["Mdundo top songs", "TurnTable Top 100", "National music chart"]


def test_only_an_exact_channel_id_is_left_out():
    """uc plus exactly 22 id characters is a channel id; a longer id-like key is a name and stays."""
    longer = "uc" + "fixture_longer_key_abcdef"
    board = {"platform": "tiktok", "list": "TikTok hashtag board", "entries": [
        {"rank": 1, "title": longer, "item_id": iid("hashtag", longer)},
        {"rank": 2, "title": "#" + longer, "item_id": iid("hashtag", "#" + longer)},
        {"rank": 3, "title": FAKE_CHANNEL, "item_id": iid("creator", "c7")}]}
    b = market(today.build_today(boards_store([board]), D30), "ZA")["boards"][0]
    assert [e["title"] for e in b["entries"]] == [longer, "#" + longer]
    assert b["left_out"] == 1


def test_a_left_out_count_already_on_the_board_is_added_to():
    board = dict(YOUTUBE_BOARD, left_out=5, left_out_reason="No readable name")
    b = market(today.build_today(boards_store([board]), D30), "ZA")["boards"][0]
    assert (b["left_out"], b["left_out_reason"]) == (9, "No readable name")


def twitter_briefs():
    """Every evidence record and board in the fixture briefs as collection writes X: platform "twitter"."""
    base = FixtureStore()

    def briefs(date):
        rows = base.briefs(date)
        for r in rows:
            p = r["payload"]
            for c in (p.get("cards") or []) + (p.get("more") or []) + ((p.get("held_back") or {}).get("items") or []):
                for e in c.get("evidence") or []:
                    e["platform"] = "twitter"
            for b in p.get("boards") or []:
                b["platform"] = "twitter"
        return rows

    return Patched(briefs=briefs)


def platform_values(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if k == "platform":
                yield v
            elif k == "platforms" and isinstance(v, list):
                yield from v
            else:
                yield from platform_values(v)
    elif isinstance(value, list):
        for v in value:
            yield from platform_values(v)


def test_today_card_evidence_and_boards_say_x_for_twitter():
    store = twitter_briefs()
    t = today.build_today(store, D30)
    za = market(t, "ZA")
    assert za["boards"] and {b["platform"] for b in za["boards"]} == {"x"}
    assert {e["platform"] for c in za["cards"] + za["more"] for e in c["evidence"]} == {"x"}
    assert "twitter" not in list(platform_values(t))
    assert "twitter" not in list(platform_values(today.build_trends(store, "ZA", D30)))
    card = today.build_trend(store, ZA_A, "ZA", D30)
    held = today.build_trend(store, ZA_I, "ZA", D30)
    assert card["evidence"] and {e["platform"] for e in card["evidence"]} == {"x"}
    assert held["evidence"] and {e["platform"] for e in held["evidence"]} == {"x"}


def test_data_issue_market(fx):
    ke = market(today.build_today(fx, D30), "KE")
    assert ke["status"] == "data_issue"
    assert ke["cards"] == [] and ke["more"] == []
    assert ke["held_back"]["count"] == len(ke["held_back"]["items"]) >= 1


def test_first_morning(fx):
    t = today.build_today(fx, D29)
    assert t["first_morning"] is True
    assert t["warmup"]["day"] == 1
    assert t["status"] == "partial"  # KE missing on 29 September
    for m in t["markets"]:
        assert m["dropped"]["first_morning"] is True
        assert m["dropped"]["text"] == "First morning: nothing to compare yet"
        assert all(c["tag"] is None for c in m["cards"] + m["more"])
    ke = market(t, "KE")
    assert ke["cards"] == [] and "data_issue" in [b["kind"] for b in ke["banners"]]


def test_all_published_status(fx):
    rows = [r for r in fx.briefs(D30) if r["market"] != "KE"]
    ke = dict(next(r for r in fx.briefs(D30) if r["market"] == "KE"), status="published")
    s = Patched(briefs=lambda d: rows + [ke] if d == D30 else FixtureStore().briefs(d))
    assert today.build_today(s, D30)["status"] == "published"


def test_headline_skips_unexplained_card(fx):
    def briefs(d):
        rows = FixtureStore().briefs(d)
        for r in rows:
            if r["market"] == "ZA":
                r["payload"]["headline"] = {"text": "x", "item_id": ZA_C, "claim_ids": ["c1"]}
        return rows

    h = today.build_today(Patched(briefs=briefs), D30)["headline"]
    assert h["market"] == "NG" and h["item_id"] == NG_A

    def none(d):
        rows = FixtureStore().briefs(d)
        for r in rows:
            r["payload"]["headline"] = None
        return rows

    assert today.build_today(Patched(briefs=none), D30)["headline"] is None


def test_today_admits_specific_cards_and_repartitions_without_renumbering():
    base = _passing_specificity_card()
    top = []
    for index in range(4):
        card = deepcopy(base)
        card["item_id"] = f"valid-top-{index}"
        card["rank"] = index + 1
        top.append(card)
    hidden = deepcopy(base)
    hidden["item_id"] = "hidden-top"
    hidden["rank"] = 5
    hidden["explained"] = "true"
    more = []
    for index in range(2):
        card = deepcopy(base)
        card["item_id"] = f"valid-more-{index}"
        card["rank"] = 6 + index
        more.append(card)

    za = market(today.build_today(_today_store(top + [hidden], more), D30), "ZA")
    assert [card["item_id"] for card in za["cards"]] == [*(f"valid-top-{i}" for i in range(4)), "valid-more-0"]
    assert [card["item_id"] for card in za["more"]] == ["valid-more-1"]
    assert [card["rank"] for card in za["cards"] + za["more"]] == [1, 2, 3, 4, 6, 7]
    assert all(card["explained"] is True for card in za["cards"] + za["more"])


@pytest.mark.parametrize("mutate", [
    lambda card: card.pop("specificity"),
    lambda card: card["specificity"].update(status="fail"),
    lambda card: card["specificity"].pop("reason"),
    lambda card: card["specificity"].update(reason="missing_explanation"),
    lambda card: card["specificity"].update(why_now="other explanation"),
    lambda card: card["specificity"].update(local_evidence_ids=[card["specificity"]["local_evidence_ids"][0]] * 2),
    lambda card: card["specificity"].update(local_evidence_ids=[1, "tt_za_006"]),
    lambda card: card["specificity"]["quote"].update(evidence_id="missing-source"),
    lambda card: card["specificity"]["quote"].update(text="routine at the taxi"),
    lambda card: _replace_specificity_quote(card, "6"),
    lambda card: _replace_specificity_quote(card, "st 6"),
    lambda card: _replace_specificity_quote(card, "not present in source"),
    lambda card: _replace_specificity_quote(card, " ".join(["word"] * 26), " ".join(["word"] * 26)),
    lambda card: _replace_specificity_quote(card, "a" * 159 + " b", "a" * 159 + " b"),
    lambda card: card.update(explained="true"),
    lambda card: card.update(explained=False),
    lambda card: card.update(explanation_status="failed_checks"),
    lambda card: card.update(claims=[]),
    lambda card: card.update(claims=[None]),
    lambda card: card.update(explanation_claim_ids=[]),
    lambda card: card.update(explanation_claim_ids=["missing-claim"]),
    lambda card: card.update(explanation_claim_ids="c1"),
    lambda card: card.update(explanation="  "),
    lambda card: card.update(evidence=[]),
    lambda card: card.update(evidence="malformed"),
    lambda card: card.update(evidence=[None]),
    lambda card: card["specificity"].update(local_evidence_ids="tt_za_006"),
    lambda card: card["specificity"].update(quote=None),
])
def test_today_hides_malformed_or_failed_specificity_cards(mutate):
    card = _passing_specificity_card()
    item_id = card["item_id"]
    mutate(card)

    za = market(today.build_today(_today_store([card]), D30), "ZA")
    assert item_id not in {candidate["item_id"] for candidate in za["cards"] + za["more"]}


def test_today_requires_two_available_local_examples_and_allows_unavailable_extra_ids():
    card = _passing_specificity_card()
    quote_id = card["specificity"]["quote"]["evidence_id"]
    card["evidence"] = [record for record in card["evidence"] if record["id"] == quote_id]
    za = market(today.build_today(_today_store([card]), D30), "ZA")
    assert za["cards"] + za["more"] == []

    card = _passing_specificity_card()
    unavailable_id = next(i for i in card["specificity"]["local_evidence_ids"] if i != quote_id)
    card["evidence"] = [record for record in card["evidence"] if record["id"] != unavailable_id]
    za = market(today.build_today(_today_store([card]), D30), "ZA")
    assert [candidate["item_id"] for candidate in za["cards"] + za["more"]] == [card["item_id"]]


def test_today_holds_a_card_whose_claim_quotes_or_rests_only_on_a_post_it_does_not_return():
    card = _passing_specificity_card()
    secondary = next(claim for claim in card["claims"] if claim["id"] not in card["explanation_claim_ids"])
    secondary["quotes"] = [{"evidence_id": "missing-post", "text": "words from a post not returned"}]
    za = market(today.build_today(_today_store([card]), D30), "ZA")
    assert za["cards"] + za["more"] == []
    assert today._today_card_fault(card) == today.REJECT_QUOTE

    card = _passing_specificity_card()
    card["claims"].append({"id": "c9", "text": "Rests on a post not returned", "evidence_ids": ["missing-post"]})
    card["explanation_claim_ids"].append("c9")
    za = market(today.build_today(_today_store([card]), D30), "ZA")
    assert za["cards"] + za["more"] == []
    assert today._today_card_fault(card) == today.REJECT_POSTS


def test_today_counts_distinct_available_local_ids():
    card = _passing_specificity_card()
    quote_id = card["specificity"]["quote"]["evidence_id"]
    other_id = next(i for i in card["specificity"]["local_evidence_ids"] if i != quote_id)
    card["specificity"]["local_evidence_ids"] = [quote_id, quote_id, other_id]
    card["evidence"] = [record for record in card["evidence"] if record["id"] == quote_id]
    za = market(today.build_today(_today_store([card]), D30), "ZA")
    assert za["cards"] + za["more"] == []


def test_today_does_not_count_an_id_only_record_as_an_available_example():
    card = _passing_specificity_card()
    quote_id = card["specificity"]["quote"]["evidence_id"]
    other_id = next(i for i in card["specificity"]["local_evidence_ids"] if i != quote_id)
    quote_record = next(record for record in card["evidence"] if record["id"] == quote_id)
    card["evidence"] = [quote_record, {"id": other_id}]
    za = market(today.build_today(_today_store([card]), D30), "ZA")
    assert za["cards"] + za["more"] == []


def test_today_counts_a_safe_source_url_as_an_available_example():
    card = _passing_specificity_card()
    quote_id = card["specificity"]["quote"]["evidence_id"]
    other_id = next(i for i in card["specificity"]["local_evidence_ids"] if i != quote_id)
    quote_record = next(record for record in card["evidence"] if record["id"] == quote_id)
    card["evidence"] = [quote_record, {"id": other_id, "url": "https://example.invalid/post"}]
    za = market(today.build_today(_today_store([card]), D30), "ZA")
    assert [candidate["item_id"] for candidate in za["cards"] + za["more"]] == [card["item_id"]]


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "data:text/plain,post", "/relative/path", "//example.invalid/post",
    "https://user:secret@example.invalid/post",
])
def test_today_does_not_count_unsafe_urls_as_available_examples(url):
    card = _passing_specificity_card()
    quote_id = card["specificity"]["quote"]["evidence_id"]
    other_id = next(i for i in card["specificity"]["local_evidence_ids"] if i != quote_id)
    quote_record = next(record for record in card["evidence"] if record["id"] == quote_id)
    card["evidence"] = [quote_record, {"id": other_id, "url": url}]
    za = market(today.build_today(_today_store([card]), D30), "ZA")
    assert za["cards"] + za["more"] == []


def test_today_matches_stripped_explanation_for_why_now():
    card = _passing_specificity_card()
    card.pop("explanation_status", None)
    card["explanation"] = f" {card['explanation']} "
    za = market(today.build_today(_today_store([card]), D30), "ZA")
    assert [candidate["item_id"] for candidate in za["cards"] + za["more"]] == [card["item_id"]]


def test_today_accepts_a_160_codepoint_quote_with_astral_characters():
    card = _passing_specificity_card()
    text = "\U0001f4a1" * 158 + " x"
    _replace_specificity_quote(card, text, text)
    za = market(today.build_today(_today_store([card]), D30), "ZA")
    assert [candidate["item_id"] for candidate in za["cards"] + za["more"]] == [card["item_id"]]


def test_today_hides_card_when_its_quote_source_is_unavailable():
    card = _passing_specificity_card()
    quote_id = card["specificity"]["quote"]["evidence_id"]
    card["evidence"] = [record for record in card["evidence"] if record["id"] != quote_id]
    za = market(today.build_today(_today_store([card]), D30), "ZA")
    assert za["cards"] + za["more"] == []


def test_today_requires_quote_and_source_id_on_the_same_claim():
    card = _passing_specificity_card()
    quote_id = card["specificity"]["quote"]["evidence_id"]
    quoted_claim = next(claim for claim in card["claims"] if claim["id"] == "c1")
    other_claim = next(claim for claim in card["claims"] if claim["id"] == "c2")
    quoted_claim["evidence_ids"] = [i for i in quoted_claim["evidence_ids"] if i != quote_id]
    other_claim["evidence_ids"].append(quote_id)
    card["explanation_claim_ids"] = ["c1", "c2"]

    za = market(today.build_today(_today_store([card]), D30), "ZA")
    assert za["cards"] + za["more"] == []


def test_today_headline_cannot_refer_to_a_hidden_card():
    card = _passing_specificity_card()
    card["specificity"]["status"] = "fail"
    headline = {"text": "Hidden card headline", "item_id": card["item_id"], "claim_ids": ["c1"]}
    result = today.build_today(_today_store([card], headline=headline), D30)
    assert result["headline"] is None


def test_today_ignores_malformed_headline_types():
    card = _passing_specificity_card()
    store = _today_store([card], headline=["malformed"])
    assert today.build_today(store, D30)["headline"] is None


def test_no_ok_collect_is_data_issue(fx):
    s = Patched(runs=lambda stage, run_date: [{"run_id": "r_x", "stage": stage, "run_date": run_date,
                                                "status": "failed"}])
    t = today.build_today(s, D30)
    assert t["status"] == "data_issue"
    for m in t["markets"]:
        assert "data_issue" in [b["kind"] for b in m["banners"]]
    assert today.today_status(s, D30) == "data_issue"


def test_warmup_ends():
    s = Patched(first_ok_collect_date=lambda: "2026-09-10")
    w = today.build_today(s, D30)["warmup"]
    assert w["day"] == 21 and w["active"] is False
    kinds = [b["kind"] for m in today.build_today(s, D30)["markets"] for b in m["banners"]]
    assert "warming_up" not in kinds


def test_today_status(fx):
    assert today.today_status(fx, D30) == "published"
    assert today.today_status(fx, "2026-09-28") == "not_ready"


# ---------- trends ----------

def test_build_trends(fx):
    tr = today.build_trends(fx, "ZA", D30)
    assert tr["date"] == D30 and tr["market"] == "ZA"
    za = market(today.build_today(fx, D30), "ZA")
    assert ZA_C not in {card["item_id"] for card in za["cards"] + za["more"]}
    legacy = next(card for card in tr["cards"] if card["item_id"] == ZA_C)
    assert legacy["explained"] is False and legacy["claims"] == [] and legacy["evidence"]
    assert today.build_trends(fx, "ZA")["date"] == D30
    with pytest.raises(today.NotReady):
        today.build_trends(fx, "ZA", "2026-09-28")


def test_build_trend(fx):
    card = today.build_trend(fx, ZA_A, "ZA", D30)
    assert card["item_id"] == ZA_A and card["tag"] == "moved_up" and card["evidence"]
    assert "held_back" not in card
    held = today.build_trend(fx, ZA_I, "ZA", D30)
    assert held["item_id"] == ZA_I
    assert held["held_back"] == {"rule": "G1", "reason": "data_issue",
                                 "reason_text": "Data issue on TikTok in the last 3 days"}
    assert held["evidence"] and held["explained"] is False and held["claims"] == []
    legacy = today.build_trend(fx, ZA_C, "ZA", D30)
    assert legacy["explained"] is False and legacy["evidence"]
    with pytest.raises(today.NotFound):
        today.build_trend(fx, NG_A, "ZA", D30)
    with pytest.raises(today.NotFound):
        today.build_trend(fx, "nope", "ZA", D30)


def test_a_busy_model_hold_reads_the_same_on_the_trend_page_as_on_today():
    from core.brief.payload import NOT_RUN_REASONS
    busy = sorted(NOT_RUN_REASONS)[0]
    rows = deepcopy(FixtureStore().briefs(D30))
    za = next(r for r in rows if r["market"] == "ZA")
    item = next(i for i in za["payload"]["held_back"]["items"] if i["item_id"] == ZA_I)
    item.update(rule="G10", reason="explanation_failed", reason_text="Explanation failed its checks",
                failed_reason=busy)
    store = Patched(briefs=lambda date: rows if date == D30 else [])
    held = today.build_trend(store, ZA_I, "ZA", D30)["held_back"]
    assert held["reason_text"] == "Not explained in time: the model was busy" and held["failed_reason"] == busy
    shown = next(i for i in market(today.build_today(store, D30), "ZA")["held_back"]["items"]
                 if i["item_id"] == ZA_I)
    assert shown["reason_text"] == held["reason_text"]


# ---------- fixture hygiene ----------

BANNED = [r"\bgen ?z\b", r"\bmillennial", r"\bgeneration", r"\byouth", r"\bteen", r"\bage\b", r"\baged\b",
          r"\byears old\b", r"\byoung\b", r"google.?trends"]
LABELS = {"observed", "corroborated", "single_source", "inferred"}


def all_numbers(obj):
    if isinstance(obj, dict):
        if "numbers" in obj:
            yield from obj["numbers"]
        for v in obj.values():
            yield from all_numbers(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from all_numbers(v)


def test_fixture_hygiene():
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(FIXTURES.glob("*.json"))
                     if p.name in {"briefs.json", "collection_health.json", "calendar.json", "runs.json"})
    low = text.lower()
    for pat in BANNED:
        assert not re.search(pat, low), pat
    briefs = json.loads((FIXTURES / "briefs.json").read_text(encoding="utf-8"))
    labels_seen = set()
    for row in briefs:
        p = row["payload"]
        for c in p.get("cards", []) + p.get("more", []):
            ev = {e["id"]: e for e in c["evidence"]}
            for e in c["evidence"]:
                assert e["url"].startswith("https://example.invalid/")
                assert e["handle"].startswith("@fixture_")
                assert e["market"] == row["market"]
            for cl in c["claims"]:
                assert cl["label"] in LABELS and cl["evidence_ids"]
                assert set(cl["evidence_ids"]) <= set(ev)
                labels_seen.add(cl["label"])
            assert set(c["explanation_claim_ids"]) <= {cl["id"] for cl in c["claims"]}
        for h in p["held_back"]["items"]:
            for e in h["evidence"]:
                assert e["url"].startswith("https://example.invalid/")
    assert labels_seen == LABELS
    nums = list(all_numbers(briefs))
    assert nums
    for n in nums:
        assert n["query_id"] and n["run_id"] and n["result_hash"].startswith("sha256:")
        assert "value" in n and "unit" in n


def test_brief_coverage_issues_are_appended_not_replaced(fx, monkeypatch):
    real = fx.briefs

    def briefs(date):
        rows = real(date)
        for r in rows:
            if r["market"] == "ZA":
                r = r.update({"payload": {**r["payload"], "coverage": {"issues": [
                    "3 board entries without a name", "TikTok hashtag board: could not be read"]}}}) or r
        return rows

    monkeypatch.setattr(fx, "briefs", briefs)
    cov = market(today.build_today(fx, D30), "ZA")["coverage"]
    assert cov["issues"] == ["TikTok hashtag board: could not be read", "3 board entries without a name"]


CRITIC_HELD = "held-critic-item"


def _critic_store(check_rows, held_ids=(CRITIC_HELD,), fail=None):
    """The fixture brief with explicit held items on ZA, and claim_checks answered by a recording _query."""
    calls = []
    held = {"count": len(held_ids), "text": "held", "items": [
        {"item_id": item_id, "title": "Held trend", "rule": "G10", "reason": "explanation_failed",
         "reason_text": "Explanation failed its checks", "evidence_ids": [], "evidence": []}
        for item_id in held_ids]}
    base = _today_store([], held_back=held)

    def _t(name):
        return f"`ogilvy-trends-v2.{name}`"

    def _query(sql, **params):
        calls.append((sql, params))
        if fail is not None:
            raise fail
        wanted = set(params["ids"][1])
        return [dict(r) for r in check_rows if r["answer_or_brief_id"] in wanted]

    base._t = _t
    base._query = _query
    return base, calls


def _check(item_id, verdict, reason, rule="critic", run_id="r_brief_20260930_01", market_code="ZA", claim_id=None):
    return {"answer_or_brief_id": f"{run_id}:{market_code}:{item_id}", "claim_id": claim_id, "rule": rule,
            "verdict": verdict, "reason": reason}


def _held_by_id(store, code="ZA"):
    return {i["item_id"]: i for i in market(today.build_today(store, D30), code)["held_back"]["items"]}


@pytest.mark.parametrize("menu, sentence", [
    ("a news or scheduled event", "A news or scheduled event could explain this, and the posts do not rule it out."),
    ("a paid campaign", "A paid campaign could explain this, and the posts do not rule it out."),
    ("one viral post or creator", "One viral post or creator could explain this, and the posts do not rule it out."),
])
def test_today_held_item_names_the_critics_menu_explanation(menu, sentence):
    store, _ = _critic_store([_check(CRITIC_HELD, "cut", f"Critic: a simpler explanation was not ruled out: {menu}")])
    assert _held_by_id(store)[CRITIC_HELD]["held_detail"] == sentence


def test_today_held_item_without_a_named_explanation_says_why_now_was_not_shown():
    store, _ = _critic_store([_check(CRITIC_HELD, "cut", "Critic: a simpler explanation was not ruled out")])
    assert _held_by_id(store)[CRITIC_HELD]["held_detail"] == (
        "A simpler explanation could not be ruled out from these posts.")


def test_today_held_detail_never_names_words_outside_the_critic_menu():
    store, _ = _critic_store([_check(CRITIC_HELD, "cut",
                                     "Critic: a simpler explanation was not ruled out: a rival brand's stunt")])
    assert _held_by_id(store)[CRITIC_HELD]["held_detail"] == (
        "A simpler explanation could not be ruled out from these posts.")


def test_today_held_detail_menu_matches_the_brief_jobs_critic_menu():
    from core.brief.job import CRITIC_MENU

    assert today.CRITIC_MENU_WORDS == tuple(words for words, _ in CRITIC_MENU)


@pytest.mark.parametrize("rows", [
    [],
    [_check(CRITIC_HELD, "pass", "Critic: the simpler explanation was ruled out: a paid campaign")],
    [_check(CRITIC_HELD, "cut", "before repair: Critic: a simpler explanation was not ruled out: a paid campaign")],
    [_check(CRITIC_HELD, "pass", "Banned term check: passed", rule="K6")],
    [_check(CRITIC_HELD, "cut", "Label check: the label was lowered to what the evidence allows", rule="K5")],
    [_check(CRITIC_HELD, "cut", "Status check: the status was lowered as claims were cut", rule="K10")],
    [_check(CRITIC_HELD, "cut", "before repair: Support check: the explanation sentence was not supported by its posts",
            rule="K4")],
    [_check(CRITIC_HELD, "cut", "Critic: a simpler explanation was not ruled out: a paid campaign",
            run_id="r_brief_20260929_01")],
    [_check(CRITIC_HELD, "cut", "Critic: a simpler explanation was not ruled out: a paid campaign", market_code="NG")],
])
def test_today_held_detail_is_omitted_without_a_holding_row_for_this_run_and_market(rows):
    store, _ = _critic_store(rows)
    assert "held_detail" not in _held_by_id(store)[CRITIC_HELD]


def test_today_held_detail_ignores_the_before_repair_row_when_the_final_row_holds():
    store, _ = _critic_store([
        _check(CRITIC_HELD, "cut", "before repair: Critic: a simpler explanation was not ruled out: a paid campaign"),
        _check(CRITIC_HELD, "cut", "Critic: a simpler explanation was not ruled out: a coordinated push"),
    ])
    assert _held_by_id(store)[CRITIC_HELD]["held_detail"] == (
        "A coordinated push could explain this, and the posts do not rule it out.")


def test_today_held_detail_with_conflicting_named_explanations_names_none():
    store, _ = _critic_store([
        _check(CRITIC_HELD, "cut", "Critic: a simpler explanation was not ruled out: a paid campaign"),
        _check(CRITIC_HELD, "cut", "Critic: a simpler explanation was not ruled out: a coordinated push"),
    ])
    assert _held_by_id(store)[CRITIC_HELD]["held_detail"] == (
        "A simpler explanation could not be ruled out from these posts.")


def test_today_reads_critic_rows_once_with_bounded_parameterised_ids():
    store, calls = _critic_store([], held_ids=(CRITIC_HELD, "held-other"))
    today.build_today(store, D30)
    assert len(calls) == 1
    sql, params = calls[0]
    assert "`ogilvy-trends-v2.intelligence_42_agent.claim_checks`" in sql
    assert "verdict != 'pass'" in sql and "IN UNNEST(@ids)" in sql and "LIMIT" in sql
    assert re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP)\b", sql, re.I) is None
    assert CRITIC_HELD not in sql
    typ, ids = params["ids"]
    assert typ == "ARRAY<STRING>"
    assert set(ids) >= {f"r_brief_20260930_01:ZA:{CRITIC_HELD}", "r_brief_20260930_01:ZA:held-other"}
    assert all(i.startswith("r_brief_20260930_01:") for i in ids)


def test_today_held_detail_read_failure_keeps_today_working_without_the_field():
    store, _ = _critic_store([], fail=RuntimeError("Access Denied: claim_checks"))
    held = _held_by_id(store)
    assert CRITIC_HELD in held and "held_detail" not in held[CRITIC_HELD]


def test_today_held_detail_read_failure_marks_the_detail_unavailable_not_absent():
    store, _ = _critic_store([], held_ids=(CRITIC_HELD, "held-other"), fail=RuntimeError("Access Denied: claim_checks"))
    held = _held_by_id(store)
    assert held[CRITIC_HELD]["check_detail"] == "unavailable"
    assert held["held-other"]["check_detail"] == "unavailable"
    assert all("held_detail" not in i for i in held.values())


def test_today_held_detail_read_with_no_rows_leaves_the_detail_absent():
    store, calls = _critic_store([])
    held = _held_by_id(store)
    assert len(calls) == 1
    assert "check_detail" not in held[CRITIC_HELD] and "held_detail" not in held[CRITIC_HELD]


def test_today_held_detail_read_that_finds_a_row_is_not_marked_unavailable():
    store, _ = _critic_store([SUPPORT])
    held = _held_by_id(store)[CRITIC_HELD]
    assert "held_detail" in held and "check_detail" not in held


def test_today_fixture_store_skips_the_critic_read(fx):
    t = today.build_today(fx, D30)
    assert all("held_detail" not in i for m in t["markets"] for i in m["held_back"]["items"])
    assert all("check_detail" not in i for m in t["markets"] for i in m["held_back"]["items"])


SUPPORT = _check(CRITIC_HELD, "cut", "Support check: the explanation sentence was not supported by its posts", rule="K4")
PLACE = _check(CRITIC_HELD, "cut", "Place check: the explanation named a place no cited post is located in", rule="K3")
NUMBER = _check(CRITIC_HELD, "cut", "Number check: the explanation had a number not pinned to its query", rule="K2")
CRITIC_CUT = _check(CRITIC_HELD, "cut", "Critic: a simpler explanation was not ruled out: a paid campaign")
CLAIM_CUT = _check(CRITIC_HELD, "cut", "Support check: a claim was not supported by its posts", rule="K4",
                   claim_id="c2")
QUOTE_CUT = _check(CRITIC_HELD, "cut", "Quote check: a quote or evidence id in a claim did not check out", rule="K1",
                   claim_id="c1")


@pytest.mark.parametrize("rows, sentence", [
    ([CRITIC_CUT, NUMBER, CLAIM_CUT, PLACE, SUPPORT], "The explanation sentence was not supported by its posts."),
    ([CRITIC_CUT, NUMBER, CLAIM_CUT, PLACE], "The explanation named a place no cited post is located in."),
    ([NUMBER, CLAIM_CUT, CRITIC_CUT], "A paid campaign could explain this, and the posts do not rule it out."),
    ([CLAIM_CUT, NUMBER], "The explanation had a number not pinned to its query."),
    ([CLAIM_CUT, QUOTE_CUT], "A quote or evidence id in a claim did not check out."),
    ([CLAIM_CUT], "A claim was not supported by its posts."),
    ([SUPPORT, dict(QUOTE_CUT, verdict="breach")], "A quote or evidence id in a claim did not check out."),
])
def test_today_held_detail_follows_the_brief_jobs_failed_reason_order(rows, sentence):
    store, _ = _critic_store(rows)
    assert _held_by_id(store)[CRITIC_HELD]["held_detail"] == sentence


WHY_NOW = "The posts do not show why this is happening in this market now."


@pytest.mark.parametrize("reasons, sentence", [
    (["Critic: local why-now not shown"], WHY_NOW),
    (["Critic: a simpler explanation was not ruled out: a paid campaign; local why-now not shown"],
     "A paid campaign could explain this, the posts do not rule it out, and they do not show why this is happening "
     "in this market now."),
    (["Critic: a simpler explanation was not ruled out; local why-now not shown"],
     "A simpler explanation could not be ruled out from these posts, and they do not show why this is happening "
     "in this market now."),
    (["Critic: a simpler explanation was not ruled out: a coordinated push"],
     "A coordinated push could explain this, and the posts do not rule it out."),
    (["Critic: local why-now not shown", "Critic: a simpler explanation was not ruled out: a paid campaign"],
     "A simpler explanation could not be ruled out from these posts."),
])
def test_today_held_detail_names_the_critic_part_that_failed(reasons, sentence):
    store, _ = _critic_store([_check(CRITIC_HELD, "cut", reason) for reason in reasons])
    assert _held_by_id(store)[CRITIC_HELD]["held_detail"] == sentence


def test_today_held_detail_why_now_ranks_where_the_critic_does():
    store, _ = _critic_store([NUMBER, _check(CRITIC_HELD, "cut", "Critic: local why-now not shown")])
    assert _held_by_id(store)[CRITIC_HELD]["held_detail"] == WHY_NOW
    store, _ = _critic_store([SUPPORT, _check(CRITIC_HELD, "cut", "Critic: local why-now not shown")])
    assert _held_by_id(store)[CRITIC_HELD]["held_detail"] == (
        "The explanation sentence was not supported by its posts.")


def test_today_held_detail_ignores_a_before_repair_why_now_row():
    store, _ = _critic_store([_check(CRITIC_HELD, "cut", "before repair: Critic: local why-now not shown")])
    assert "held_detail" not in _held_by_id(store)[CRITIC_HELD]


def _held_with_reason(rule, text):
    return {"item_id": f"plain-{rule}", "title": "Held trend", "rule": rule, "reason": "held",
            "reason_text": text, "evidence_ids": [], "evidence": []}


@pytest.mark.parametrize("rule, raw, plain", [
    ("G5", "Paid-led: sponsored or brand-owned share 0.62", "Mostly sponsored or brand posts (62%)"),
    ("G5b", "Paid-led: #fixture_promo is on the campaign hashtag list", "#fixture_promo is a known campaign hashtag"),
    ("G4b", "Not assessed: political item not Corroborated in an unbiased lane",
     "Political: waiting for independent confirmation"),
    ("G3", "Found by search: seen only in search", "Only found through our own searches so far"),
    ("G6", "Global: 14 of 52 card source posts in the last 7 days were located in this market or came from its feeds",
     "Mostly posted outside this market (14 of 52 posts local)"),
    ("G6", "Market unconfirmed: source market evidence is missing or invalid",
     "We could not confirm which market this comes from"),
    ("G6", "Market unconfirmed: source market evidence is inconsistent",
     "We could not confirm which market this comes from"),
    ("G1", "Data issue: 2 of the last 3 market-days invalid on the main platform",
     "Not enough clean data on the main platform"),
])
def test_today_held_reasons_read_in_plain_words_with_the_gate_text_kept(rule, raw, plain):
    held = {"count": 1, "text": "held", "items": [_held_with_reason(rule, raw)]}
    item = market(today.build_today(_today_store([], held_back=held), D30), "ZA")["held_back"]["items"][0]
    assert (item["reason_text"], item["reason_raw"]) == (plain, raw)


# "Explanation failed its checks" left this list on 6 October 2026: it now reads "The explanation did not pass our
# checks" (core/api/held_words.py, tester report of 5 October 2026).
@pytest.mark.parametrize("raw", ["Producer supplied hold reason", "Paid-led: sponsored or brand-owned share lots"])
def test_today_held_reasons_outside_the_gate_wording_stay_as_written(raw):
    held = {"count": 1, "text": "held", "items": [_held_with_reason("G5", raw)]}
    item = market(today.build_today(_today_store([], held_back=held), D30), "ZA")["held_back"]["items"][0]
    assert item["reason_text"] == raw and "reason_raw" not in item


def test_trend_held_back_reason_reads_in_plain_words():
    held = {"count": 1, "text": "held", "items": [
        _held_with_reason("G5", "Paid-led: sponsored or brand-owned share 0.62")]}
    out = today.build_trend(_today_store([], held_back=held), "plain-G5", "ZA", D30)
    assert out["held_back"]["reason_text"] == "Mostly sponsored or brand posts (62%)"
    assert out["held_back"]["reason_raw"] == "Paid-led: sponsored or brand-owned share 0.62"


def test_today_heading_says_today_when_no_market_has_a_card():
    base = FixtureStore()

    def briefs(date):
        rows = deepcopy(base.briefs(date))
        for row in rows:
            row["payload"].update(cards=[], more=[], headline=None)
        return rows

    t = today.build_today(Patched(briefs=briefs), D30)
    assert t["heading"] == "Today, 30 September 2026"



def _no_explanation(card):
    card.update(explained=False)


def _no_claims(card):
    card["claims"] = []


def _no_specificity(card):
    card.pop("specificity")


def _one_local_post(card):
    card["specificity"]["local_evidence_ids"] = card["specificity"]["local_evidence_ids"][:1]


def _unverified_quote(card):
    card["specificity"]["quote"] = {"evidence_id": card["specificity"]["quote"]["evidence_id"],
                                    "text": "words that are not in the post"}


def _unreadable_posts(card):
    card["evidence"] = "not a list"


@pytest.mark.parametrize("mutate, words", [
    # Was "The explanation did not pass its checks"; it reads as the brief's own explanation holds do (held_words.py).
    (_no_explanation, "The explanation did not pass our checks"),
    (_unreadable_posts, "The posts behind it could not be read"),
    (_no_claims, "The explanation is not tied to checked claims"),
    (_no_specificity, "The local why-now was not checked"),
    (_one_local_post, "Fewer than 2 local posts can be shown"),
    (_unverified_quote, "The quoted post wording could not be verified"),
])
def test_today_reader_rejection_names_the_check_that_failed(mutate, words):
    card = _passing_specificity_card()
    card["item_id"] = "reader-held-check"
    mutate(card)
    empty_held = {"count": 0, "text": "Nothing held back", "items": []}
    item = market(today.build_today(_today_store([card], held_back=empty_held), D30), "ZA")["held_back"]["items"][0]
    assert (item["reason"], item["reason_text"]) == (None, words)


def test_today_reader_rejection_keeps_the_specificity_reason_when_given():
    card = _passing_specificity_card()
    card["item_id"] = "reader-held-specific"
    card["specificity"].update(status="fail", reason="quote_not_verbatim", reason_text="Quoted wording could not be verified")
    empty_held = {"count": 0, "text": "Nothing held back", "items": []}
    item = market(today.build_today(_today_store([card], held_back=empty_held), D30), "ZA")["held_back"]["items"][0]
    assert (item["reason"], item["reason_text"]) == ("quote_not_verbatim", "Quoted wording could not be verified")


def test_a_calls_failure_that_names_its_class_is_still_a_failed_source():
    yt = {**health_row("youtube", "board_youtube", False, reason="calls: http_5xx,timeout"), "calls": 5,
          "calls_ok": 0, "route": "youtube/videos/trending", "protocol": "youtube/videos/trending"}
    store = Patched(collection_health=lambda date: [yt])
    za = market(today.build_today(store, D30), "ZA")
    issues = [b["text"] for b in za["banners"] if b["kind"] == "data_issue"]
    assert "1 source failed today: YouTube trending board" in issues
    assert today._coverage([yt])["issues"] == ["YouTube trending board: could not be read"]
    assert today.invalid_code(yt) == "calls" and today.invalid_code({"invalid_reason": None}) is None


def test_board_titles_from_broken_parses_are_repaired_or_left_out():
    """Live audit F02, 4 October 2026: Boomplay rows titled with their own rank ('1 1'), Shazam rows carrying a
    Markdown link with the song and artist swapped, escaped underscores, and a platform-prefixed channel id."""
    board = {"platform": "shazam", "list": "board shazam", "entries": [
        {"rank": 1, "title": "Nayvee's Prayer by DJ Zinhle", "item_id": iid("sound", "a")},
        {"rank": 2, "title": "2", "item_id": iid("sound", "b")},
        {"rank": 3, "title": "Darque, Atmos Blaq & Tycoon by [uMoya 2.0 (feat. Soulful Disciple) \\[Tycoon Remix\\]]"
                            "(https://www.shazam.com/song/6807779984/umoya-20)", "item_id": iid("sound", "c")},
        {"rank": 4, "title": "Ibele by Dj Jaivane & Zem\\_sa", "item_id": iid("sound", "d")},
        {"rank": 5, "title": "youtube:ucnem4dhhuwmvwmyktz8x46g", "item_id": iid("sound", "e")},
        {"rank": 6, "title": "[Plain link](https://example.invalid/x)", "item_id": iid("sound", "f")},
    ]}
    (out,) = market(today.build_today(boards_store([board]), D30), "ZA")["boards"]
    assert [(e["rank"], e["title"]) for e in out["entries"]] == [
        (1, "Nayvee's Prayer by DJ Zinhle"),
        (3, "uMoya 2.0 (feat. Soulful Disciple) [Tycoon Remix] by Darque, Atmos Blaq & Tycoon"),
        (4, "Ibele by Dj Jaivane & Zem_sa"),
        (6, "Plain link"),
    ]
    assert (out["left_out"], out["left_out_reason"]) == (2, "No readable name")


def test_a_platform_prefixed_channel_id_is_not_a_readable_title():
    assert not today._readable("youtube:ucnem4dhhuwmvwmyktz8x46g")
    assert today._readable("42")


def test_board_list_and_coverage_names_read_as_the_series_name():
    """Live audit F23: 'board boomplay' on Today and 'Boomplay: Board boomplay' in coverage."""
    board = {"platform": "boomplay", "list": "board boomplay",
             "entries": [{"rank": 1, "title": "Song by Artist", "item_id": iid("sound", "a")}]}
    (out,) = market(today.build_today(boards_store([board]), D30), "ZA")["boards"]
    assert out["list"] == "Boomplay trending songs"
    assert today._series_name({"series": "board_boomplay", "platform": "boomplay"}) == "Boomplay trending songs"
    assert today._series_name({"series": "panel_ig_gossip", "platform": "instagram"}).startswith("Gossip")


def test_a_market_with_no_usable_source_has_no_item_count():
    """Product review TOD-03: no usable source read nothing countable, so the count is unmeasured, not zero."""
    assert today._coverage([])["posts"] is None


def test_only_a_shazam_row_is_unswapped():
    """Other boards write "song by artist" the right way round, even when the artist is a Markdown link."""
    entry = {"rank": 1, "title": "Hello by [World](https://example.invalid/a)"}
    assert today._board_title(entry, "boomplay") == "Hello by World"
    assert today._board_title(entry, "shazam") == "World by Hello"


# Tester report, 5 October 2026: briefs stored before the payload fix say "1 creators and 1 posts in 3 days".
def test_a_stored_count_of_one_reads_in_the_singular_on_cards_and_held_items():
    card = _passing_specificity_card()
    card["count_line"] = "1 creators and 1 posts in 3 days"
    held = {"count": 1, "text": "held", "items": [dict(_held_with_reason("G5", "Producer supplied hold reason"),
                                                       count_line="1 creators and 21 posts in 3 days")]}
    za = market(today.build_today(_today_store([card], held_back=held), D30), "ZA")
    assert za["cards"][0]["count_line"] == "1 creator and 1 post in 3 days"
    assert za["held_back"]["items"][0]["count_line"] == "1 creator and 21 posts in 3 days"


def test_a_reddit_account_id_is_not_a_readable_title():
    assert not today._readable("t2_x9o72po41") and not today._readable("reddit:t2_x9o72po41")
    assert today._readable("t2 fans") and today._readable("#t2_tour")
    assert today._display_title(["t2_x9o72po41"], platforms=["reddit"], kind="creator") == "Reddit account"


# The writer's checked title (contract.md section 4, title_written)

WRITTEN = "Independence Day reflections"


def _titled_store(**card_fields):
    rows = deepcopy(FixtureStore().briefs(D30))
    za = next(r for r in rows if r["market"] == "ZA")
    card = next(c for c in za["payload"]["cards"] + za["payload"]["more"] if c["item_id"] == ZA_A)
    card.update(card_fields)
    return Patched(briefs=lambda date: rows if date == D30 else deepcopy(FixtureStore().briefs(date)))


def _za_card(store, item_id=ZA_A):
    za = market(today.build_today(store, D30), "ZA")
    return next(c for c in za["cards"] + za["more"] if c["item_id"] == item_id)


def test_a_written_title_reaches_the_card_beside_its_unchanged_label(fx):
    before = _za_card(fx)
    after = _za_card(_titled_store(title_written=f"  {WRITTEN} "))
    assert after["title_written"] == WRITTEN and after["title"] == before["title"]
    assert {k: v for k, v in after.items() if k != "title_written"} == before
    assert today.build_trend(_titled_store(title_written=WRITTEN), ZA_A, "ZA", D30)["title_written"] == WRITTEN


def test_a_brief_without_written_titles_reads_exactly_as_before(fx):
    out = today.build_today(fx, D30)
    assert all("title_written" not in c for m in out["markets"] for c in m["cards"] + m["more"])


def test_a_written_title_shows_only_on_an_explained_card():
    store = _titled_store(title_written=WRITTEN, explained=False)
    card = next(c for c in today.build_trends(store, "ZA", D30)["cards"] if c["item_id"] == ZA_A)
    assert card["title_written"] is None


def test_a_card_that_loses_a_suppressed_persons_post_keeps_its_label():
    card = {"item_id": "i", "explained": True, "title": "#x", "title_written": WRITTEN, "claims": [],
            "explanation_claim_ids": [], "evidence": [{"id": "p1", "platform": "tiktok", "handle": "@gone"},
                                                       {"id": "p2", "platform": "tiktok", "handle": "@stays"}]}
    out = today.without_hidden(card, ({"tiktok:gone"}, set(), set()))
    assert out["title_written"] is None and out["title"] == "#x"
    assert today.without_hidden(card, ({"tiktok:other"}, set(), set()))["title_written"] == WRITTEN


def test_a_title_check_row_never_names_a_held_items_reason():
    title_row = _check(CRITIC_HELD, "cut", "Title check: the written title did not pass, so the card keeps its label",
                       rule="title")
    store, _ = _critic_store([title_row])
    assert "held_detail" not in _held_by_id(store)[CRITIC_HELD]
    store, _ = _critic_store([title_row, _check(CRITIC_HELD, "cut", "Critic: a simpler explanation was not ruled out")])
    assert _held_by_id(store)[CRITIC_HELD]["held_detail"] == (
        "A simpler explanation could not be ruled out from these posts.")


def test_running_row_does_not_outrank_the_failed_row_of_the_same_run(fx):
    real_briefs = fx.briefs

    def briefs(date):
        rows = real_briefs(date)
        for row in rows:
            if row["brief_date"] == D30 and row["market"] == "ZA":
                row["status"] = "data_issue"
                row["payload"]["banners"] = [{"kind": "data_issue",
                                               "text": "Data issue: the steps before the brief did not finish by 06:15, so no trends were checked today"}]
        return rows

    def runs(stage, run_date):
        if stage == "collect":
            return [{"run_id": "collect-ok", "stage": stage, "run_date": run_date, "status": "ok",
                     "started_at": "2026-09-30 02:00:00", "finished_at": "2026-09-30 02:10:00"}]
        if stage == "detect":
            return [{"run_id": "detect-x", "stage": stage, "run_date": run_date, "status": "running",
                     "started_at": "2026-09-30 02:10:00", "finished_at": None},
                    {"run_id": "detect-x", "stage": stage, "run_date": run_date, "status": "failed",
                     "started_at": "2026-09-30 02:10:00", "finished_at": "2026-09-30 02:20:00"}]
        return []

    za = market(today.build_today(Patched(briefs=briefs, runs=runs), D30), "ZA")
    texts = [b["text"] for b in za["banners"] if b["kind"] == "data_issue"]
    assert "Data issue: collection or detection failed for South Africa" in texts
    assert not any("no failed stage is recorded" in text for text in texts)


def test_latest_run_prefers_the_finished_row_of_a_run_over_its_running_row():
    running = {"run_id": "detect-x", "status": "running", "started_at": "2026-09-30 02:10:00", "finished_at": None}
    failed = {"run_id": "detect-x", "status": "failed", "started_at": "2026-09-30 02:10:00",
              "finished_at": "2026-09-30 02:20:00"}
    for pair in ([running, failed], [failed, running]):
        assert today._latest_run(pair) is failed
        assert today._stage_failed(pair) is True


def test_a_later_attempt_still_running_outranks_an_earlier_failed_attempt():
    failed = {"run_id": "detect-a", "status": "failed", "started_at": "2026-09-30 02:10:00",
              "finished_at": "2026-09-30 02:20:00"}
    retry = {"run_id": "detect-b", "status": "running", "started_at": "2026-09-30 02:30:00", "finished_at": None}
    assert today._latest_run([failed, retry]) is retry
    assert today._stage_failed([failed, retry]) is False


# Review of the boards redesign, finding 3: the app counts "on N charts" from the entries it receives, and _boards
# had already dropped the id-titled ones and the in-chart repeats. The server counts first, over every stored entry.
def _chart_counts(boards, code="ZA"):
    return {b["list"]: b.get("chart_counts") for b in
            market(today.build_today(boards_store(boards), D30), code)["boards"]}


def test_an_item_on_three_stored_charts_counts_three_though_one_of_them_names_it_by_an_id():
    shared = iid("hashtag", "#fixture_shared")
    boards = [
        {"platform": "tiktok", "list": "TikTok hashtag board", "entries": [
            {"rank": 2, "title": "#fixture_shared", "item_id": shared}]},
        {"platform": "youtube", "list": "YouTube trending board", "entries": [
            {"rank": 17, "title": "#fixture_shared", "item_id": shared}]},
        {"platform": "twitter", "list": "X trending board", "entries": [
            {"rank": 4, "title": FAKE_CHANNEL, "item_id": shared}]},
    ]
    counts = _chart_counts(boards)
    assert counts["TikTok hashtag board"] == {shared: 3}
    assert counts["YouTube trending board"] == {shared: 3}
    assert counts["X trending board"] is None  # nothing on it is shown, so it carries no counts


def test_a_chart_counts_once_however_often_it_repeats_the_item_and_a_null_rank_still_counts():
    shared = iid("hashtag", "#fixture_repeat")
    boards = [
        {"platform": "tiktok", "list": "TikTok hashtag board", "entries": [
            {"rank": 1, "title": "#fixture_repeat", "item_id": shared},
            {"rank": 5, "title": "#fixture_repeat", "item_id": shared}]},
        {"platform": "youtube", "list": "YouTube trending board", "entries": [
            {"rank": None, "title": "#fixture_repeat", "item_id": shared}]},
    ]
    assert _chart_counts(boards) == {"TikTok hashtag board": {shared: 2}, "YouTube trending board": {shared: 2}}


def test_a_chart_is_its_platform_and_its_list_and_a_city_list_is_one_chart_per_city():
    shared = iid("sound", "fixture shared sound")
    entry = [{"rank": 3, "title": "Back 2 U", "item_id": shared}]
    boards = [
        {"platform": "apple_music", "list": "Apple Music chart", "entries": entry},
        {"platform": "apple_music", "list": "Apple Music new", "entries": entry},
        {"platform": "shazam", "list": "board shazam city", "city": "Lagos", "entries": entry},
        {"platform": "shazam", "list": "board shazam city", "city": "Abuja", "entries": entry},
    ]
    for chart in market(today.build_today(boards_store(boards), D30), "ZA")["boards"]:
        assert chart["chart_counts"] == {shared: 4}


def test_an_item_with_no_item_id_has_no_count_and_the_same_item_in_another_market_is_separate():
    other = iid("hashtag", "#fixture_both_markets")
    board = {"platform": "tiktok", "list": "TikTok hashtag board", "entries": [
        {"rank": 1, "title": "#no_id_here"}, {"rank": 2, "title": "#fixture_both_markets", "item_id": other}]}
    za_only = {"platform": "youtube", "list": "YouTube trending board", "entries": [
        {"rank": 3, "title": "#fixture_both_markets", "item_id": other}]}
    base = FixtureStore()

    def briefs(date):
        rows = base.briefs(date)
        for r in rows:
            if r["market"] == "ZA":
                r["payload"]["boards"] = json.loads(json.dumps([board, za_only]))
            elif r["market"] == "NG":
                r["payload"]["boards"] = json.loads(json.dumps([board]))
        return rows

    out = today.build_today(Patched(briefs=briefs), D30)
    za, ng = market(out, "ZA")["boards"], market(out, "NG")["boards"]
    assert [b["chart_counts"] for b in za] == [{other: 2}, {other: 2}]  # two charts in ZA
    assert [b["chart_counts"] for b in ng] == [{other: 1}]  # one chart in NG: the ZA charts are not counted there
    assert all(not any(k is None or k == "" for k in b["chart_counts"]) for b in za + ng)  # no marker without an id


def test_a_board_of_only_ids_on_a_past_brief_says_the_brief_day_not_today():
    """The server worded this reason "today" whatever the day. The app shows it beside the brief's own date."""
    import datetime as dt
    ahead = dt.datetime(2026, 10, 8, 9, 0, tzinfo=today.SAST)
    za = market(today.build_today(boards_store([ID_BOARD]), D30, now=ahead), "ZA")["boards"][0]
    assert za["left_out_reason"] == "None of this list's entries had a readable name on 30 September 2026"
    assert za["left_out"] == 1


def test_a_board_of_only_ids_on_todays_brief_still_says_today():
    import datetime as dt
    same_day = dt.datetime(2026, 9, 30, 9, 0, tzinfo=today.SAST)
    za = market(today.build_today(boards_store([ID_BOARD]), D30, now=same_day), "ZA")["boards"][0]
    assert za["left_out_reason"] == today.NO_NAMES_READ


# W8-DEC-11: a paid route whose calls succeed on two consecutive days and land nothing is recorded invalid with
# reason zero_yield (core/collect/writers.py). The source answered, so Today says that, not "not usable" or "failed".
def test_a_zero_yield_series_is_worded_as_a_source_that_answered_but_returned_nothing():
    answered = dict(health_row("tiktok", "feed_tiktok", False, items=0, reason="zero_yield"), calls=4, calls_ok=4)
    broken = dict(health_row("instagram", "ig_location", False, reason="calls"), calls=4, calls_ok=0)
    store = Patched(collection_health=lambda date: [answered, broken])
    za = market(today.build_today(store, D30), "ZA")
    assert za["coverage"]["issues"] == ["TikTok: answered but returned no posts or counts",
                                        "Instagram location posts: could not be read"]
    assert not any("not usable" in i for i in za["coverage"]["issues"])
    failed = [b["text"] for b in za["banners"] if "failed today" in b["text"]]
    assert failed == ["1 source failed today: Instagram locations"]


# The brief payload carries explanation_status on held items (failed_checks or not_run). A G10 hold is worded from it.
G10_CASES = [
    ({"explanation_status": "not_run", "failed_reason": None}, "Not explained: the model did not get to this topic"),
    ({"explanation_status": "not_run", "failed_reason": "BUSY"}, "Not explained in time: the model was busy"),
    ({"explanation_status": "failed_checks", "failed_reason": "A claim was not supported by its posts"},
     "The explanation did not pass our checks"),
    ({"explanation_status": "explained", "failed_reason": None}, "The explanation did not pass our checks"),
    ({}, "The explanation did not pass our checks"),  # a brief stored before the field existed
]


def _g10_store(fields):
    from core.brief.payload import NOT_RUN_REASONS
    fields = {k: (sorted(NOT_RUN_REASONS)[0] if v == "BUSY" else v) for k, v in fields.items()}
    rows = deepcopy(FixtureStore().briefs(D30))
    za = next(r for r in rows if r["market"] == "ZA")
    item = next(i for i in za["payload"]["held_back"]["items"] if i["item_id"] == ZA_I)
    for key in ("explanation_status", "failed_reason"):
        item.pop(key, None)
    item.update(rule="G10", reason="explanation_failed", reason_text="Explanation failed its checks", **fields)
    return Patched(briefs=lambda date: rows if date == D30 else [])


@pytest.mark.parametrize("fields, words", G10_CASES)
def test_a_g10_hold_on_today_is_worded_from_the_status_the_brief_carries(fields, words):
    shown = next(i for i in market(today.build_today(_g10_store(fields), D30), "ZA")["held_back"]["items"]
                 if i["item_id"] == ZA_I)
    assert shown["reason_text"] == words


@pytest.mark.parametrize("fields, words", G10_CASES)
def test_a_g10_hold_on_the_trend_page_is_worded_from_the_status_the_brief_carries(fields, words):
    assert today.build_trend(_g10_store(fields), ZA_I, "ZA", D30)["held_back"]["reason_text"] == words


def test_the_contract_lists_explanation_status_among_the_held_item_fields():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "contract.md").read_text(encoding="utf-8")
    assert "`explanation_status`" in text and "`failed_checks` or `not_run`" in text


# wave8/detect: an understand run whose clusterer failed is recorded ok with counts.partial, partial_reason
# cluster_stack_failed and data_issue. Today reads that row and says so.
TOPICS_FAILED = "Data issue: topic grouping failed today"


def _understand(counts, run_id="r_understand_20260930_09", started="2026-09-30T05:00:00+02:00", as_text=False):
    import json as _json
    return {"run_id": run_id, "stage": "understand", "run_date": D30, "status": "ok", "started_at": started,
            "finished_at": started.replace("05:00", "05:30"), "counts": _json.dumps(counts) if as_text else counts,
            "error": None, "model_usd": 0.0}


def _topics_store(*rows, detect=()):
    base = FixtureStore()
    other = {"understand": list(rows), "detect": list(detect)}
    return Patched(runs=lambda stage, d: other[stage] if stage in other else base.runs(stage, d))


PARTIAL = {"partial": True, "partial_reason": "cluster_stack_failed", "partial_error": "ImportError: no module",
           "data_issue": TOPICS_FAILED}


@pytest.mark.parametrize("as_text", [False, True])
def test_a_topic_grouping_failure_shows_as_a_data_issue_in_every_market(as_text):
    out = today.build_today(_topics_store(_understand(PARTIAL, as_text=as_text)), D30)
    for m in out["markets"]:
        issues = [b for b in m["banners"] if b["text"] == TOPICS_FAILED]
        assert issues == [{"kind": "data_issue", "text": TOPICS_FAILED}], m["market"]


def test_the_banner_is_the_fixed_words_not_the_text_the_row_carries():
    row = _understand({**PARTIAL, "data_issue": "Data issue: something else the row said"})
    out = today.build_today(_topics_store(row), D30)
    za = market(out, "ZA")
    assert TOPICS_FAILED in [b["text"] for b in za["banners"]]
    assert not any("something else" in b["text"] for b in za["banners"])


@pytest.mark.parametrize("counts", [
    {}, {"embedded": 40}, {"partial": True, "partial_reason": "something_else", "data_issue": "Data issue: other"},
    {"partial": False, "partial_reason": "cluster_stack_failed"}])
def test_an_understand_run_that_did_not_fail_on_the_clusterer_adds_no_banner(counts):
    out = today.build_today(_topics_store(_understand(counts)), D30)
    assert not any(b["text"] == TOPICS_FAILED for m in out["markets"] for b in m["banners"])


def test_a_later_clean_understand_run_clears_the_banner():
    failed = _understand(PARTIAL, run_id="r_understand_20260930_08", started="2026-09-30T04:00:00+02:00")
    clean = _understand({"embedded": 40}, run_id="r_understand_20260930_09", started="2026-09-30T06:00:00+02:00")
    out = today.build_today(_topics_store(failed, clean), D30)
    assert not any(b["text"] == TOPICS_FAILED for m in out["markets"] for b in m["banners"])
    again = today.build_today(_topics_store(clean, failed), D30)  # the order the rows come in does not matter
    assert not any(b["text"] == TOPICS_FAILED for m in again["markets"] for b in m["banners"])


def test_a_day_with_no_understand_row_adds_no_banner():
    out = today.build_today(_topics_store(), D30)
    assert not any(b["text"] == TOPICS_FAILED for m in out["markets"] for b in m["banners"])


def test_the_banner_is_shown_once_and_the_fixture_day_is_otherwise_unchanged():
    plain = today.build_today(FixtureStore(), D30)
    marked = today.build_today(_topics_store(_understand(PARTIAL)), D30)
    for a, b in zip(plain["markets"], marked["markets"]):
        assert [x for x in b["banners"] if x["text"] != TOPICS_FAILED] == a["banners"]
        assert sum(x["text"] == TOPICS_FAILED for x in b["banners"]) == 1


# The banner reads the run detect reads: the newest ok understand run by finished_at, whatever came after it.
def _failed_rerun(run_id="r_understand_20260930_10", started="2026-09-30T07:00:00+02:00"):
    row = _understand({}, run_id=run_id, started=started)
    return {**row, "status": "failed", "finished_at": started.replace("07:00", "07:05"), "counts": None,
            "error": "the run raised"}


def _banner(*rows):
    out = today.build_today(_topics_store(*rows), D30)
    return [any(b["text"] == TOPICS_FAILED for b in m["banners"]) for m in out["markets"]]


def _topics_banner(understand, detect):
    out = today.build_today(_topics_store(*understand, detect=detect), D30)
    return [any(b["text"] == TOPICS_FAILED for b in m["banners"]) for m in out["markets"]]


def test_a_failed_rerun_after_a_partial_ok_run_keeps_the_banner():
    partial = _understand(PARTIAL, run_id="r_understand_20260930_08", started="2026-09-30T04:00:00+02:00")
    for rows in ((partial, _failed_rerun()), (_failed_rerun(), partial)):
        assert all(_banner(*rows))


def test_a_clean_ok_run_after_a_partial_ok_run_shows_no_banner_even_with_a_failed_rerun_after_it():
    partial = _understand(PARTIAL, run_id="r_understand_20260930_08", started="2026-09-30T04:00:00+02:00")
    clean = _understand({"embedded": 40}, run_id="r_understand_20260930_09", started="2026-09-30T06:00:00+02:00")
    assert not any(_banner(partial, clean))
    assert not any(_banner(partial, clean, _failed_rerun()))


def test_the_newest_ok_run_is_taken_by_finished_at_not_started_at():
    slow_partial = _understand(PARTIAL, run_id="r_understand_20260930_08", started="2026-09-30T04:00:00+02:00")
    slow_partial["finished_at"] = "2026-09-30T08:00:00+02:00"  # started first, finished last
    quick_clean = _understand({"embedded": 40}, run_id="r_understand_20260930_09", started="2026-09-30T06:00:00+02:00")
    assert all(_banner(slow_partial, quick_clean))
    assert all(_banner(quick_clean, slow_partial))


def test_a_day_with_only_a_failed_understand_run_adds_no_topics_banner():
    assert not any(_banner(_failed_rerun()))


# The banner reads detect's own record (counts.topics_failed of the day's good detect run), and the understand rule
# only while no ok detect run exists. Scenarios A to F of the detect re-review: the same rows go to both readers.
TOPICS_FAILED_COUNTS = {"topics_failed": {"reason": "cluster_stack_failed", "data_issue": TOPICS_FAILED,
                                          "topic_items_judged": 0}}


def _detect(counts, run_id="r_detect_20260930_10", status="ok", started="2026-09-30T06:30:00+02:00", finished=True):
    return {"run_id": run_id, "stage": "detect", "run_date": D30, "status": status, "started_at": started,
            "finished_at": started.replace(":00+", ":40+", 1) if finished else None, "counts": counts, "error": None,
            "model_usd": 0.0}


def detect_reads(understand_rows):
    """What core/detect/job.py topics_failed_today decides from these understand rows: the newest ok row by
    finished_at, and a partial_reason of cluster_stack_failed on it."""
    ok = [r for r in understand_rows if r["status"] == "ok" and r["finished_at"] is not None]
    newest = max(ok, key=lambda r: r["finished_at"], default=None)
    return bool(newest and (newest["counts"] or {}).get("partial_reason") == "cluster_stack_failed")


def _scenario(name):
    partial = _understand(PARTIAL, run_id="r_understand_20260930_08", started="2026-09-30T04:00:00+02:00")
    later = "2026-09-30T07:00:00+02:00"
    if name == "A":
        return [partial]
    if name == "B":  # an operator re-triggers f42-understand without FORCE_RERUN
        return [partial, {**_understand({}, run_id="r_understand_20260930_09", started=later), "status": "skipped_duplicate"}]
    if name == "C":
        return [partial, _failed_rerun()]
    if name == "D":
        return [partial, {**_understand({}, run_id="r_understand_20260930_09", started=later), "status": "running",
                          "finished_at": None, "counts": None}]
    if name == "E":  # overlapping runs: the partial one starts first and finishes last
        clean = _understand({"embedded": 40}, run_id="r_understand_20260930_09", started="2026-09-30T04:10:00+02:00")
        return [{**partial, "finished_at": "2026-09-30T05:00:00+02:00"}, {**clean, "finished_at": "2026-09-30T04:20:00+02:00"}]
    repaired = _understand({"embedded": 40}, run_id="r_understand_20260930_09", started=later)  # F
    return [partial, repaired]


@pytest.mark.parametrize("name", ["A", "B", "C", "D", "E"])
def test_scenarios_a_to_e_with_no_detect_run_yet_read_the_understand_rows_as_detect_would(name):
    rows = _scenario(name)
    assert detect_reads(rows) is True
    assert all(_banner(*rows))


@pytest.mark.parametrize("name", ["A", "B", "C", "D", "E", "F"])
def test_scenarios_a_to_f_with_detect_having_left_topics_out_show_the_banner(name):
    rows = _scenario(name)
    assert all(_topics_banner(rows, [_detect(TOPICS_FAILED_COUNTS)]))


def test_scenario_f_detect_ran_on_the_partial_day_and_a_later_repair_does_not_lift_the_banner():
    rows = _scenario("F")
    assert detect_reads(rows) is False  # detect asked now would judge topics, but that day's item_state has none
    assert all(_topics_banner(rows, [_detect(TOPICS_FAILED_COUNTS)]))
    assert not any(_topics_banner(rows, []))  # no detect run yet: the repaired understand run decides


def test_a_good_detect_run_without_topics_failed_shows_no_banner_whatever_understand_says_later():
    clean_detect = _detect({"item_state": 120})
    assert not any(_topics_banner(_scenario("A"), [clean_detect]))
    assert not any(_topics_banner(_scenario("C"), [clean_detect]))


def test_the_newest_ok_detect_run_by_finished_at_decides_and_a_failed_or_running_one_does_not():
    bad = _detect(TOPICS_FAILED_COUNTS, run_id="r_detect_20260930_10", started="2026-09-30T06:30:00+02:00")
    clean = _detect({"item_state": 9}, run_id="r_detect_20260930_11", started="2026-09-30T07:30:00+02:00")
    failed = _detect({}, run_id="r_detect_20260930_12", status="failed", started="2026-09-30T08:30:00+02:00")
    running = _detect({}, run_id="r_detect_20260930_13", status="running", started="2026-09-30T09:30:00+02:00", finished=False)
    partial = _scenario("A")
    assert all(_topics_banner(partial, [bad, failed, running]))
    assert not any(_topics_banner(partial, [bad, clean, failed, running]))
    assert not any(_topics_banner(partial, [clean, bad, failed]))


def test_with_only_a_failed_detect_run_the_understand_rule_still_applies():
    failed = _detect({}, status="failed")
    assert all(_topics_banner(_scenario("A"), [failed]))
    assert not any(_topics_banner([_understand({"embedded": 40})], [failed]))


@pytest.mark.parametrize("counts", [{"topics_failed": None}, {"topics_failed": {}}, {"topics_failed": "yes"},
                                    {"topics_failed": {"reason": "something_else"}}])
def test_a_detect_record_that_does_not_name_the_clusterer_failure_adds_no_banner(counts):
    assert not any(_topics_banner(_scenario("A"), [_detect(counts)]))


def test_detect_counts_stored_as_text_are_read():
    import json as _json
    assert all(_topics_banner([], [_detect(_json.dumps(TOPICS_FAILED_COUNTS))]))
    assert all(_topics_banner(_scenario("A"), [_detect("not json")]))  # an unreadable record falls back
    assert not any(_topics_banner([_understand({"embedded": 40})], [_detect("not json")]))
