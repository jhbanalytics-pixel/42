import copy
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from core.agent import checks, writer
from core.agent.answer import validate_answer
from core.agent.checks import RULES, check_answer
from core.agent.context import RunContext
from core.agent.forecast_promotion import PROMOTED_SQL

RUN = "r_20260928_ask"
AS_OF = "2026-09-28T06:10:00+02:00"
WINDOW = (date(2026, 9, 21), date(2026, 9, 28))
SQL = "SELECT d.day, d.posts, d.ratio FROM intelligence_42_core.v_item_daily_current d WHERE d.tag = @tag"
PARAMS = {"tag": "7colours"}
ROWS = [{"day": "2026-09-27", "posts": 412, "ratio": 2.8}, {"day": "2026-09-26", "posts": 131, "ratio": 0.9}]


def record(rid, platform, handle, text, posted_at="2026-09-27T14:05:00+02:00", market="ZA", flags=None, url=None):
    return {
        "id": rid,
        "platform": platform,
        "handle": handle,
        "url": url or f"https://example.test/{platform}/{handle.strip('@')}/{rid}",
        "posted_at": posted_at,
        "market": market,
        "text": text,
        "engagement": {"views": 1000},
        "flags": flags or [],
    }


STORED = [
    record("tt_1", "tiktok", "@mpho.cooks.za",
           "Sunday 7 colours check. I can’t be the only one who puts beetroot right next to the chakalaka #7colours"),
    record("tt_2", "tiktok", "@thandi_eats_jozi", "Rating your 7 colours plates. Pap AND rice on one plate is a crime #7colours"),
    record("tt_3", "tiktok", "@kay.plates", "My 7 colours plate for today, rate it in the comments #7colours"),
    record("x_1", "x", "@lerato_mk", "Rate my 7 colours honestly, the butternut is doing the most #7colours"),
    record("x_2", "x", "@sipho_k", "Ngiyakuthanda ukudla kwami kwangeSonto #7colours"),
    record("ig_paid", "instagram", "@brand.kitchen", "Our 7 colours platter is back this Sunday #ad", flags=["paid"]),
    record("gem_1", "web", "@gemini", "Seven colours is a South African Sunday lunch tradition",
           url="https://gemini.google.com/app/3f2a9c"),
]


class FakeWarehouse:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def dry_run(self, sql, params):
        raise AssertionError("checks never dry-run")

    def run(self, sql, params, max_bytes_billed):
        self.calls.append((sql, params, max_bytes_billed))
        return copy.deepcopy(self.rows)


def make_ctx(rows=ROWS, stored=STORED, live=True):
    """live adds a SocialCrawl call on reddit/search for 7colours, which pins "reddit/search 7colours" in a gap."""
    ctx = RunContext(run_id=RUN, tier="T1", as_of=datetime.fromisoformat(AS_OF), market="ZA")
    for r in stored:
        ctx.evidence[r["id"]] = copy.deepcopy(r)
    ctx.record_query(SQL, PARAMS, copy.deepcopy(rows), "daily #7colours posts and ratio")
    if live:
        ctx.sc_calls.append({"route": "reddit/search", "params": {"query": "7colours"}, "status": "ok"})
    return ctx


def number(ctx, value, unit):
    q = ctx.queries["q_1"]
    return {"value": value, "unit": unit, "query_id": "q_1", "run_id": RUN, "result_hash": q["result_hash"]}


def make_draft(ctx):
    return {
        "status": "complete",
        "as_of": AS_OF,
        "short_answer": "Sunday lunch plate ratings rose on TikTok and X this week.",
        "claims": [
            {
                "id": "c1",
                "text": "People on TikTok and X rated their Sunday lunch plates on 27 September: 412 posts, 2.8x the usual daily count.",
                "label": "corroborated",
                "kind": "observation",
                "evidence_ids": ["tt_1", "tt_3", "x_1"],
                "quotes": [
                    {"evidence_id": "tt_1", "text": "I can't be the only one who puts beetroot"},
                    {"evidence_id": "x_1", "text": "Rate my 7 colours honestly"},
                ],
                "numbers": [number(ctx, 412, "posts on 27 September"), number(ctx, 2.8, "times the usual daily count")],
            },
            {
                "id": "c2",
                "text": "Creators on TikTok called “Pap AND rice on one plate” a crime.",
                "label": "observed",
                "kind": "observation",
                "evidence_ids": ["tt_1", "tt_2"],
                "quotes": [{"evidence_id": "tt_2", "text": "Pap AND rice on one plate is a crime"}],
            },
            {
                "id": "c3",
                "text": "Sunday plate rating looks like a weekly ritual rather than a one-off.",
                "label": "inferred",
                "kind": "interpretation",
                "evidence_ids": ["tt_1", "x_1"],
            },
        ],
        "evidence": [copy.deepcopy(ctx.evidence[i]) for i in ("tt_1", "tt_2", "tt_3", "x_1")],
        "so_what": [
            {"text": "A food brand can join the Sunday rating ritual with its own plate.", "claim_ids": ["c1", "c2"]},
            {"text": "Treat the rating habit as a weekly slot.", "claim_ids": ["c3"]},
        ],
        "watch_next": [{"text": "Watch whether plate ratings return next Sunday.", "claim_ids": ["c1"], "forecast": False}],
        "gaps": [],
    }


def run(draft, ctx, wh, market="ZA", markets=None):
    where = {"markets": markets} if markets is not None else {"market": market}
    answer, verdicts = check_answer(draft, ctx, wh, window=WINDOW, **where)
    assert validate_answer(answer) == []
    return answer, verdicts


def verdict(verdicts, cid, rule):
    rows = [v for v in verdicts if v["claim_id"] == cid and v["rule"] == rule]
    assert len(rows) == 1, rows
    return rows[0]


def claim(answer, cid):
    return next((c for c in answer["claims"] if c["id"] == cid), None)


def claim_ids(answer):
    return [c["id"] for c in answer["claims"]]


@pytest.fixture
def ctx():
    return make_ctx()


@pytest.fixture
def wh():
    return FakeWarehouse(ROWS)


@pytest.fixture
def draft(ctx):
    return make_draft(ctx)


# clean draft and verdict shape

def test_clean_draft_passes_every_rule(draft, ctx, wh):
    before = copy.deepcopy(draft)
    answer, verdicts = run(draft, ctx, wh)
    assert draft == before, "check_answer must not mutate the draft"
    assert answer["status"] == "complete"
    assert claim_ids(answer) == ["c1", "c2", "c3"]
    assert [v for v in verdicts if v["verdict"] != "pass"] == []
    assert answer["gaps"] == []
    assert len(answer["so_what"]) == 2


def test_one_verdict_row_per_rule_per_claim(draft, ctx, wh):
    draft["claims"][1]["text"] += " Gen Z loves it."
    _, verdicts = run(draft, ctx, wh)
    per_claim = [v for v in verdicts if v["claim_id"] in ("c1", "c2", "c3")]
    assert sorted((v["claim_id"], v["rule"]) for v in per_claim) == sorted(
        (c, r) for c in ("c1", "c2", "c3") for r in RULES)
    assert set(RULES) == {"K1", "K2", "K3", "K5", "K6", "K8", "K9", "K10"}
    for v in verdicts:
        assert v["checker"] == "code"
        assert v["run_id"] == RUN
        assert v["verdict"] in ("pass", "cut", "downgrade")
        assert isinstance(v["reason"], str) and v["reason"]


def test_each_query_is_rerun_once_per_answer_with_the_cap(draft, ctx, wh):
    run(draft, ctx, wh)
    assert wh.calls == [(SQL, PARAMS, 2_000_000_000)]


def test_output_evidence_is_the_stored_records_that_surviving_claims_cite(draft, ctx, wh):
    draft["claims"][0]["text"] += " Another 37 posts followed."
    answer, _ = run(draft, ctx, wh)
    assert "c1" not in claim_ids(answer)
    assert sorted(r["id"] for r in answer["evidence"]) == ["tt_1", "tt_2", "x_1"]
    assert {r["id"]: r for r in answer["evidence"]}["tt_1"] == ctx.evidence["tt_1"]


# K1 citations

def test_k1_unresolved_id_cuts_even_when_the_draft_carries_the_record(draft, ctx, wh):
    fake = record("tt_999", "tiktok", "@ghost", "I can't be the only one")
    draft["evidence"].append(fake)
    draft["claims"][1]["evidence_ids"].append("tt_999")
    answer, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c2", "K1")
    assert v["verdict"] == "cut" and "tt_999" in v["reason"]
    assert "c2" not in claim_ids(answer)
    assert all(r["id"] != "tt_999" for r in answer["evidence"])


@pytest.mark.parametrize("cited", [[3], ["tt_1", 3], ["tt_1", "tt_2", {"id": "teen_voters"}], ["tt_1", None]])
def test_k1_cuts_a_claim_citing_an_id_that_is_not_a_string(draft, ctx, wh, cited):
    draft["claims"][1]["evidence_ids"] = cited
    draft["claims"][1]["quotes"] = []
    answer, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c2", "K1")
    assert v["verdict"] == "cut" and "1 evidence id that is not a string" in v["reason"]
    assert "c2" not in claim_ids(answer)
    assert "teen" not in json.dumps([answer, verdicts])


def test_k1_counts_two_ids_that_are_not_strings_in_the_plural(draft, ctx, wh):
    draft["claims"][1]["evidence_ids"] = [3, None]
    draft["claims"][1]["quotes"] = []
    answer, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c2", "K1")
    assert v["verdict"] == "cut" and "2 evidence ids that are not strings" in v["reason"]
    assert "c2" not in claim_ids(answer)


def test_k1_tampered_draft_url_cuts(draft, ctx, wh):
    x1 = next(r for r in draft["evidence"] if r["id"] == "x_1")
    x1["url"] = "https://x.com/someone_else/status/1"
    answer, verdicts = run(draft, ctx, wh)
    for cid in ("c1", "c3"):
        v = verdict(verdicts, cid, "K1")
        assert v["verdict"] == "cut" and "url" in v["reason"]
    assert verdict(verdicts, "c2", "K1")["verdict"] == "pass"
    assert claim_ids(answer) == ["c2"]


@pytest.mark.parametrize("field,value", [("handle", "@other"), ("platform", "instagram"),
                                         ("posted_at", "2026-09-26T10:00:00+02:00"), ("market", "NG")])
def test_k1_other_tampered_fields_cut(draft, ctx, wh, field, value):
    tt2 = next(r for r in draft["evidence"] if r["id"] == "tt_2")
    tt2[field] = value
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c2", "K1")
    assert v["verdict"] == "cut" and field in v["reason"]


def test_k1_quote_not_verbatim_cuts(draft, ctx, wh):
    draft["claims"][0]["quotes"][0]["text"] = "I can't be the only one who puts beetroot beside the chakalaka"
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c1", "K1")
    assert v["verdict"] == "cut" and "verbatim" in v["reason"]


def test_k1_quote_checked_against_stored_text_not_draft_text(draft, ctx, wh):
    tt2 = next(r for r in draft["evidence"] if r["id"] == "tt_2")
    tt2["text"] = "Pap AND rice on one plate is a crime and so is samp"
    draft["claims"][1]["quotes"][0]["text"] = "Pap AND rice on one plate is a crime and so is samp"
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c2", "K1")["verdict"] == "cut"


def test_k1_curly_quote_in_stored_text_matches_straight_quote(draft, ctx, wh):
    assert "’" in ctx.evidence["tt_1"]["text"]
    assert "'" in draft["claims"][0]["quotes"][0]["text"]
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c1", "K1")["verdict"] == "pass"


# K2 numbers

def test_k2_numeral_without_numbers_entry_cuts(draft, ctx, wh):
    draft["claims"][1]["text"] += " It ran across 37 posts."
    answer, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c2", "K2")
    assert v["verdict"] == "cut" and "37" in v["reason"]
    assert "c2" not in claim_ids(answer)


def test_k2_percentage_without_entry_cuts(draft, ctx, wh):
    draft["claims"][2]["text"] = "About 40% of plate posts were ratings, which looks like a weekly ritual."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "cut"


def test_k2_ignores_digits_in_quotes_ids_handles_and_dates(draft, ctx, wh):
    draft["claims"][2]["text"] = (
        "On 27 September and on 2026-09-26, tt_1 and @kay.plates posted “Rate my 7 colours honestly” "
        "style captions under #7colours, which looks like a weekly ritual.")
    draft["claims"][2]["evidence_ids"] = ["tt_1", "tt_3", "x_1"]
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "pass"


def test_k2_rerun_rows_changed_cuts(draft, ctx):
    wh = FakeWarehouse([{"day": "2026-09-27", "posts": 380, "ratio": 2.8}])
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c1", "K2")
    assert v["verdict"] == "cut" and "re-run" in v["reason"]


def test_k2_rerun_that_moves_the_value_to_another_row_cuts(draft, ctx):
    # 412 still comes back, but now for 26 September; the claim was about 27 September.
    wh = FakeWarehouse([{"day": "2026-09-27", "posts": 131, "ratio": 2.8},
                        {"day": "2026-09-26", "posts": 412, "ratio": 0.9}])
    answer, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c1", "K2")
    assert v["verdict"] == "cut" and "412" in v["reason"] and "re-run" in v["reason"]
    assert "c1" not in claim_ids(answer)


def test_k2_rerun_with_the_same_rows_in_another_order_passes(draft, ctx):
    _, verdicts = run(draft, ctx, FakeWarehouse(list(reversed(ROWS))))
    assert verdict(verdicts, "c1", "K2")["verdict"] == "pass"


def test_k2_value_not_in_recorded_rows_cuts(draft, ctx, wh):
    draft["claims"][0]["text"] = draft["claims"][0]["text"].replace("412", "413")
    draft["claims"][0]["numbers"][0]["value"] = 413
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c1", "K2")["verdict"] == "cut"


@pytest.mark.parametrize("ratio,expected", [(2.75, "pass"), (2.72, "cut")])
def test_k2_ratio_tolerance_is_two_percent(ratio, expected):
    rows = [{"day": "2026-09-27", "posts": 412, "ratio": ratio}]
    ctx = make_ctx(rows=rows)
    _, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(rows))
    assert verdict(verdicts, "c1", "K2")["verdict"] == expected


@pytest.mark.parametrize("field,value", [("run_id", "r_other"), ("query_id", "q_9"),
                                         ("result_hash", "sha256:" + "0" * 64)])
def test_k2_unpinned_number_cuts(draft, ctx, wh, field, value):
    draft["claims"][0]["numbers"][1][field] = value
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c1", "K2")["verdict"] == "cut"


# K3 window and market

def test_k3_out_of_window_post_cuts():
    stored = [dict(r, posted_at="2026-09-10T09:00:00+02:00") if r["id"] == "x_1" else r for r in STORED]
    ctx = make_ctx(stored=stored)
    answer, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c1", "K3")["verdict"] == "cut"
    assert verdict(verdicts, "c3", "K3")["verdict"] == "cut"
    assert verdict(verdicts, "c2", "K3")["verdict"] == "pass"
    assert answer["status"] == "insufficient_evidence"


def test_k3_window_edge_uses_johannesburg_date():
    # 22:30 UTC on 20 September is 00:30 on 21 September in Johannesburg: inside the window.
    stored = [dict(r, posted_at="2026-09-20T22:30:00+00:00") if r["id"] == "tt_2" else r for r in STORED]
    ctx = make_ctx(stored=stored)
    _, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c2", "K3")["verdict"] == "pass"


def test_k3_wrong_market_post_cuts():
    stored = [dict(r, market="NG") if r["id"] == "x_1" else r for r in STORED]
    ctx = make_ctx(stored=stored)
    _, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(ROWS))
    v = verdict(verdicts, "c1", "K3")
    assert v["verdict"] == "cut" and "NG" in v["reason"]


def test_k3_without_market_uses_first_cited_record():
    stored = [dict(r, market="NG") if r["id"] == "x_1" else r for r in STORED]
    ctx = make_ctx(stored=stored)
    _, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(ROWS), market=None)
    assert verdict(verdicts, "c1", "K3")["verdict"] == "cut"
    assert verdict(verdicts, "c2", "K3")["verdict"] == "pass"


OTHER_MARKETS = [
    record("ng_1", "tiktok", "@ada.eats.lagos", "Rate my Sunday jollof plate, be honest #sundayplate", market="NG"),
    record("ke_1", "tiktok", "@wanjiku.cooks", "Sunday plate check from Nairobi, rate it #sundayplate", market="KE"),
    record("ke_2", "x", "@otieno_eats", "My Sunday pilau plate deserves a 10 #sundayplate", market="KE"),
]


def cross_market(evidence_ids, text):
    ctx = make_ctx(stored=STORED + OTHER_MARKETS)
    draft = make_draft(ctx)
    c2 = draft["claims"][1]
    c2["text"], c2["evidence_ids"], c2["quotes"] = text, evidence_ids, []
    return draft, ctx


def market_feed_posts(source):
    flags = ["market_assumed"]
    return [dict(record("feed_1", "tiktok", "@afcon.watch", "AFCON fever is real this week", market=source,
                        flags=flags), source_market=source),
            dict(record("feed_2", "x", "@goal_diaries", "Cannot wait for the AFCON draw", market=source,
                        flags=flags), source_market=source)]


def market_feed_claim(text, posts, markets):
    ctx = make_ctx(stored=STORED + posts)
    draft = make_draft(ctx)
    c2 = draft["claims"][1]
    c2.update(text=text, evidence_ids=[p["id"] for p in posts], quotes=[], label="corroborated")
    draft["evidence"].extend(copy.deepcopy(posts))
    return run(draft, ctx, FakeWarehouse(ROWS), markets=markets)


def test_k3_market_by_source_claim_needs_explicit_feed_scope_and_downgrades_label():
    _, verdicts = market_feed_claim(
        "Nigeria embraced amapiano, as seen in Nigeria's feeds.", market_feed_posts("NG"), ["NG"])
    assert verdict(verdicts, "c2", "K3")["verdict"] == "cut"

    answer, verdicts = market_feed_claim(
        "Posts seen in Kenya's feeds discuss amapiano.", market_feed_posts("KE"), ["KE"])
    assert verdict(verdicts, "c2", "K3")["verdict"] == "pass"
    assert claim(answer, "c2")["label"] == "observed"
    assert verdict(verdicts, "c2", "K5")["verdict"] == "downgrade"
    assert next(r for r in answer["evidence"] if r["id"] == "feed_1")["source_market"] == "KE"


def test_k3_source_only_people_claim_and_other_market_feed_are_cut():
    _, verdicts = market_feed_claim(
        "Kenyan fans were excited, as seen in Kenya's feeds.", market_feed_posts("KE"), ["KE"])
    assert verdict(verdicts, "c2", "K3")["verdict"] == "cut"

    _, verdicts = market_feed_claim(
        "Posts were seen in Kenya's feeds.", market_feed_posts("NG"), ["KE"])
    assert verdict(verdicts, "c2", "K3")["verdict"] == "cut"


def test_k3_draft_evidence_cannot_spoof_source_market():
    post = record("feed_1", "tiktok", "@afcon.watch", "AFCON fever is real this week", market="KE",
                  flags=["market_assumed"])
    ctx = make_ctx(stored=STORED + [post])
    draft = make_draft(ctx)
    draft["claims"][1].update(text="Amapiano posts were seen in Kenya's feeds.", evidence_ids=["feed_1"], quotes=[])
    draft["evidence"].append(dict(post, source_market="KE"))
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["KE"])
    assert verdict(verdicts, "c2", "K3")["verdict"] == "cut"


def test_source_only_answer_field_needs_feed_scope_too():
    posts = market_feed_posts("NG")
    ctx = make_ctx(stored=STORED + posts)
    draft = make_draft(ctx)
    draft["claims"][1].update(text="Amapiano posts were seen in Nigeria's feeds.",
                              evidence_ids=[p["id"] for p in posts], quotes=[])
    draft["evidence"].extend(copy.deepcopy(posts))
    draft["short_answer"] = "Nigeria embraced amapiano, as seen in Nigeria's feeds."
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG"])
    assert claim(answer, "c2") is not None
    assert answer["short_answer"] == ""
    assert verdict(verdicts, "short_answer", "K3")["verdict"] == "cut"


def test_k3_comparison_across_two_markets_passes():
    draft, ctx = cross_market(["tt_2", "ng_1"], "South African and Nigerian creators both rated their Sunday plates.")
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG"])
    for cid in ("c1", "c2", "c3"):
        assert verdict(verdicts, cid, "K3")["verdict"] == "pass"
    assert claim_ids(answer) == ["c1", "c2", "c3"]
    assert answer["status"] == "complete"


def test_k3_claim_naming_one_market_cuts_a_record_from_the_other():
    draft, ctx = cross_market(["ke_1", "ng_1"], "Kenyans rated their Sunday plates too.")
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["NG", "KE"])
    v = verdict(verdicts, "c2", "K3")
    assert v["verdict"] == "cut" and "ng_1" in v["reason"] and "ke_1" not in v["reason"]

    draft, ctx = cross_market(["ke_1", "ke_2"], "Kenyans rated their Sunday plates too.")
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["NG", "KE"])
    assert verdict(verdicts, "c2", "K3")["verdict"] == "pass"


@pytest.mark.parametrize("phrase,own,other", [
    ("In South Africa, creators", "tt_2", "ng_1"),
    ("SA creators", "tt_2", "ng_1"),
    ("South Africans", "tt_2", "ng_1"),
    ("In Nigeria, creators", "ng_1", "tt_2"),
    ("Nigerian creators", "ng_1", "tt_2"),
])
def test_k3_every_market_name_form_is_recognised(phrase, own, other):
    text = f"{phrase} rated their Sunday plates."
    draft, ctx = cross_market([own, other], text)
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG"])
    v = verdict(verdicts, "c2", "K3")
    assert v["verdict"] == "cut" and other in v["reason"]

    draft, ctx = cross_market([own], text)
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG"])
    assert verdict(verdicts, "c2", "K3")["verdict"] == "pass"


def test_k3_record_outside_the_asked_markets_cuts():
    draft, ctx = cross_market(["tt_2", "ke_1"], "Creators rated their Sunday plates.")
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG"])
    v = verdict(verdicts, "c2", "K3")
    assert v["verdict"] == "cut" and "ke_1" in v["reason"] and "KE" in v["reason"]


def test_k3_no_markets_checks_only_the_window():
    stored = [dict(r, market="NG") if r["id"] == "x_1" else r for r in STORED]
    ctx = make_ctx(stored=stored)
    _, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(ROWS), markets=[])
    assert [v for v in verdicts if v["rule"] == "K3" and v["verdict"] != "pass"] == []

    stored = [dict(r, market="NG", posted_at="2026-09-10T09:00:00+02:00") if r["id"] == "x_1" else r for r in STORED]
    ctx = make_ctx(stored=stored)
    _, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(ROWS), markets=[])
    v = verdict(verdicts, "c1", "K3")
    assert v["verdict"] == "cut" and "outside" in v["reason"] and "NG" not in v["reason"]


def test_k3_single_market_as_market_or_markets_gives_the_same_verdicts():
    draft, ctx = cross_market(["tt_2", "ke_1"], "Creators rated their Sunday plates.")
    _, legacy = check_answer(draft, ctx, FakeWarehouse(ROWS), window=WINDOW, market="ZA")
    _, current = check_answer(draft, ctx, FakeWarehouse(ROWS), window=WINDOW, markets=["ZA"])
    assert legacy == current
    v = verdict(legacy, "c2", "K3")
    assert v["verdict"] == "cut" and "KE" in v["reason"]


# K5 labels

def test_k5_corroborated_on_one_platform_with_two_authors_is_downgraded(draft, ctx, wh):
    draft["claims"][1]["label"] = "corroborated"
    answer, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c2", "K5")
    assert v["verdict"] == "downgrade" and "observed" in v["reason"]
    assert claim(answer, "c2")["label"] == "observed"
    assert answer["status"] == "complete"


def test_k5_paid_record_does_not_count_as_an_author(draft, ctx, wh):
    draft["claims"][1]["evidence_ids"] = ["tt_2", "ig_paid"]
    answer, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c2", "K5")["verdict"] == "downgrade"
    assert claim(answer, "c2")["label"] == "single_source"


def test_k5_three_authors_on_one_platform_need_a_number(draft, ctx, wh):
    c = draft["claims"][0]
    c["evidence_ids"] = ["tt_1", "tt_2", "tt_3"]
    c["quotes"] = [c["quotes"][0]]
    answer, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c1", "K5")["verdict"] == "pass"
    assert claim(answer, "c1")["label"] == "corroborated"

    c["text"] = "People on TikTok rated their Sunday lunch plates."
    del c["numbers"]
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c1", "K5")["verdict"] == "downgrade"
    assert claim(answer, "c1")["label"] == "observed"


def test_k5_never_raises_a_label(draft, ctx, wh):
    draft["claims"][0]["label"] = "single_source"
    answer, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c1", "K5")["verdict"] == "pass"
    assert claim(answer, "c1")["label"] == "single_source"


def test_k5_interpretation_is_capped_at_inferred(draft, ctx, wh):
    draft["claims"][2]["label"] = "observed"
    answer, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K5")["verdict"] == "downgrade"
    assert claim(answer, "c3")["label"] == "inferred"


# K5 tone cap: until a Lagos or Nairobi reviewer is named, an NG or KE tone claim resting on a non-English post is
# capped at single_source (progress/L3.md, Decisions from Albert; TRUST.md section 6).
TONE_STORED = STORED + [
    record("ng_pidgin", "tiktok", "@lagos.gist", "Abeg this fuel price don tire us, wetin we go chop #fuelprice",
           market="NG"),
    record("ng_en_1", "x", "@ada_speaks", "Fuel queues again in Lagos, this is so frustrating #fuelprice", market="NG"),
    record("ng_en_2", "tiktok", "@tunde.daily", "Another week of fuel queues and nobody is fixing it #fuelprice",
           market="NG"),
    record("ke_sheng", "x", "@nairobi.maze", "Maze hii Sunday plate ni noma sana #sundayplate", market="KE"),
    record("ke_en", "tiktok", "@wanjiku.cooks", "Sunday plate check from Nairobi, rate it #sundayplate", market="KE"),
]


def tone_setup(text, evidence_ids, label="corroborated", numbers=None):
    ctx = make_ctx(stored=TONE_STORED)
    draft = make_draft(ctx)
    c2 = draft["claims"][1]
    c2.update(text=text, evidence_ids=list(evidence_ids), quotes=[], label=label)
    if numbers:
        c2["numbers"] = [number(ctx, *n) for n in numbers]
    return draft, ctx


def test_k5_ng_tone_claim_citing_a_pidgin_post_is_capped_at_single_source():
    draft, ctx = tone_setup("Nigerian posters were angry about fuel prices.", ["ng_pidgin", "ng_en_1"])
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG"])
    v = verdict(verdicts, "c2", "K5")
    assert v["verdict"] == "downgrade" and "single_source" in v["reason"]
    assert claim(answer, "c2")["label"] == "single_source"


def test_k5_the_same_ng_tone_claim_citing_only_english_posts_keeps_its_label():
    draft, ctx = tone_setup("Nigerian posters were angry about fuel prices.", ["ng_en_2", "ng_en_1"])
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG"])
    assert verdict(verdicts, "c2", "K5")["verdict"] == "pass"
    assert claim(answer, "c2")["label"] == "corroborated"


def test_k5_tone_cap_never_raises_a_label():
    draft, ctx = tone_setup("Nigerian posters were angry about fuel prices.", ["ng_pidgin"], label="single_source")
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG"])
    assert verdict(verdicts, "c2", "K5")["verdict"] == "pass"
    assert claim(answer, "c2")["label"] == "single_source"


def test_k5_ke_count_claim_that_is_not_about_tone_keeps_its_label():
    draft, ctx = tone_setup("Kenyan Sunday plate posts reached 412 on 27 September.", ["ke_sheng", "ke_en"],
                            numbers=[(412, "posts on 27 September")])
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "KE"])
    assert verdict(verdicts, "c2", "K5")["verdict"] == "pass"
    assert claim(answer, "c2")["label"] == "corroborated"


def test_k5_ke_tone_claim_citing_a_sheng_post_is_capped():
    draft, ctx = tone_setup("Kenyan posters were proud of their Sunday plates.", ["ke_sheng", "ke_en"])
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "KE"])
    assert verdict(verdicts, "c2", "K5")["verdict"] == "downgrade"
    assert claim(answer, "c2")["label"] == "single_source"


def test_k5_za_tone_claim_citing_an_isizulu_post_is_unaffected():
    draft, ctx = tone_setup("South Africans were proud of their Sunday plates.", ["x_2", "tt_2"])
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c2", "K5")["verdict"] == "pass"
    assert claim(answer, "c2")["label"] == "corroborated"


def test_k5_native_policy_caps_za_tone_for_a_capped_language():
    draft, ctx = tone_setup("South Africans were angry about prices.", ["x_2", "tt_2"])
    ctx.native_languages = {"x_2": ["zu"], "tt_2": ["zu"]}
    ctx.native_statuses = {"zu": "capped"}
    ctx.native_review_loaded = True
    ctx.native_review_available = True

    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA"])

    assert verdict(verdicts, "c2", "K5")["verdict"] == "downgrade"
    assert claim(answer, "c2")["label"] == "single_source"


def test_k5_cleared_language_keeps_the_existing_evidence_ceiling():
    _, ctx = tone_setup("Nigerian posters were angry about fuel prices.", ["x_2", "tt_2"])
    records = [
        {"id": "x_2", "market": "NG", "platform": "tiktok", "handle": "@one", "text": "Abeg this is wahala"},
        {"id": "tt_2", "market": "NG", "platform": "tiktok", "handle": "@two", "text": "Abeg this is wahala"},
    ]
    ctx.native_languages = {"x_2": ["zu"], "tt_2": ["zu"]}
    ctx.native_statuses = {"zu": "cleared"}
    ctx.native_review_loaded = True
    ctx.native_review_available = True

    top, _ = checks._tone_cap({"text": "Nigerian posters were angry about fuel prices."}, records, "observed",
                              "two authors on one platform", ctx)

    assert top == "observed"


@pytest.mark.parametrize("text, expected", [
    ("Abeg this fuel price don tire us, wetin we go chop", True),
    ("Maze hii Sunday plate ni noma sana", True),
    ("Serikali haitusikii kabisa, watu wanateseka", True),
    ("Biko nna, ihe a adighi mma", True),
    ("Wallahi gaskiya mutane suna shan wahala", True),
    ("Ẹ ku ọjọ́ Sunday, ayé ti le", True),
    ("Ngiyakuthanda ukudla kwami kwangeSonto", True),
    ("Fuel queues again in Lagos, this is so frustrating", False),
    ("Pap AND rice on one plate is a crime", False),
    ("Rate my 7 colours honestly, the butternut is doing the most", False),
    ("#fuelprice @ada_speaks https://example.test/x/1", False),
])
def test_mostly_non_english_rule(text, expected):
    assert checks.mostly_non_english(text) is expected


@pytest.mark.parametrize("text, expected", [
    ("Nigerian posters were angry about fuel prices.", True),
    ("The mood on X was celebratory.", True),
    ("Kenyan posts turned sarcastic and mocking.", True),
    ("Sentiment around the fuel price hike soured.", True),
    ("Kenyan Sunday plate posts reached 412 on 27 September.", False),
    ("Creators on TikTok rated their Sunday lunch plates.", False),
])
def test_tone_claim_rule(text, expected):
    assert checks.is_tone_claim(text) is expected


# K6 breaches

def test_k6_gen_z_in_claim_is_a_breach(draft, ctx, wh):
    draft["claims"][1]["text"] += " Gen Z leads it."
    answer, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c2", "K6")
    assert v["verdict"] == "cut" and v["reason"].startswith("breach:")
    assert "c2" not in claim_ids(answer)


@pytest.mark.parametrize("phrase", ["mostly 18-24 year olds", "a middle class habit", "the demographic is urban"])
def test_k6_age_and_demographic_inference_is_a_breach(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"Plate rating looks like {phrase}."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K6")["reason"].startswith("breach:")


def test_k6_google_trends_is_a_breach(draft, ctx, wh):
    draft["claims"][2]["text"] = "Google Trends interest in seven colours also rose, so it looks like a ritual."
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K6")
    assert v["verdict"] == "cut" and v["reason"].startswith("breach:")


def test_k6_gemini_output_cited_as_evidence_is_a_breach(draft, ctx, wh):
    draft["claims"][2]["evidence_ids"] = ["tt_1", "gem_1"]
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K6")
    assert v["verdict"] == "cut" and v["reason"].startswith("breach:")


def test_k6_generated_flag_is_a_breach(draft, ctx, wh):
    ctx.evidence["x_1"]["flags"] = ["ai_generated"]
    next(r for r in draft["evidence"] if r["id"] == "x_1")["flags"] = ["ai_generated"]
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K6")["reason"].startswith("breach:")


def test_k6_breach_in_so_what_and_short_answer(draft, ctx, wh):
    draft["so_what"][1]["text"] = "Aim the weekly slot at millennials."
    draft["watch_next"][0]["text"] = "Watch whether teenagers pick it up."
    draft["short_answer"] = "Sunday plate ratings rose, led by Gen Z."
    answer, verdicts = run(draft, ctx, wh)
    assert [s["text"] for s in answer["so_what"]] == [draft["so_what"][0]["text"]]
    assert answer["watch_next"] == []
    assert answer["short_answer"] == ""
    assert answer["status"] == "partial"
    breaches = [v for v in verdicts if v["rule"] == "K6" and v["verdict"] == "cut"]
    assert {v["claim_id"] for v in breaches} == {"short_answer", "so_what/1", "watch_next/0"}
    assert all(v["reason"].startswith("breach:") for v in breaches)
    assert claim_ids(answer) == ["c1", "c2", "c3"]


# K8 quoted spans

def test_k8_untranslated_quoted_span_cuts(draft, ctx, wh):
    draft["claims"][1]["text"] = "Creators on TikTok called it “the plate of the year”."
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c2", "K8")
    assert v["verdict"] == "cut" and "the plate of the year" in v["reason"]


def test_k8_single_quoted_span_is_checked_and_apostrophes_are_not(draft, ctx, wh):
    draft["claims"][1]["text"] = "Creators' plates were called 'Pap AND rice on one plate' and it's a running joke."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c2", "K8")["verdict"] == "pass"
    draft["claims"][1]["text"] = "Creators' plates were called 'a crime against samp' and it's a running joke."
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c2", "K8")["verdict"] == "cut"


def test_k8_original_language_span_passes_and_a_translated_span_cuts(draft, ctx, wh):
    draft["claims"][1]["text"] = "@sipho_k wrote “Ngiyakuthanda ukudla kwami” about Sunday food."
    draft["claims"][1]["evidence_ids"] = ["x_2", "tt_2"]
    draft["claims"][1]["quotes"] = []
    answer, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c2", "K8")["verdict"] == "pass"
    assert "c2" in claim_ids(answer)

    draft["claims"][1]["text"] = (
        "@sipho_k wrote “Ngiyakuthanda ukudla kwami” and another post said "
        "“I love my Sunday food” (translated from isiZulu).")
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c2", "K8")["verdict"] == "cut"
    assert "c2" not in claim_ids(answer)


def test_k8_curly_quoted_span_matches_straight_apostrophe_record(draft, ctx, wh):
    draft["claims"][1]["text"] = "One creator wrote “I can’t be the only one” about beetroot."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c2", "K8")["verdict"] == "pass"


# K10, pruning and gaps

def test_k9_cuts_an_unlogged_future_claim_and_short_answer(draft, ctx, wh):
    future = "Sunday plate ratings will keep rising."
    draft["claims"][2]["text"] = future
    draft["short_answer"] = future

    answer, verdicts = run(draft, ctx, wh)

    assert claim(answer, "c3") is None
    assert answer["short_answer"] == ""
    assert verdict(verdicts, "c3", "K9")["reason"] == (
        "forecast has no matching current-run log; publication held pending persistence proof")
    assert verdict(verdicts, "short_answer", "K9")["verdict"] == "cut"
    assert future not in json.dumps(answer)


def test_k9_cuts_an_explicit_watch_forecast_and_keeps_a_neutral_watch_condition(draft, ctx, wh):
    answer_draft = copy.deepcopy(draft)
    answer_draft["watch_next"][0]["forecast"] = True

    answer, verdicts = run(answer_draft, ctx, wh)

    assert answer["watch_next"] == []
    assert verdict(verdicts, "watch_next/0", "K9")["verdict"] == "cut"

    draft["watch_next"][0]["text"] = "Watch whether plate ratings will keep rising."
    neutral, neutral_verdicts = run(draft, ctx, wh)
    assert neutral["watch_next"] == draft["watch_next"]
    assert not any(v["claim_id"] == "watch_next/0" and v["rule"] == "K9" for v in neutral_verdicts)


@pytest.mark.parametrize("text", [
    "Watch whether posts change. Amapiano will keep rising.",
    "Watch whether posts change; Amapiano will keep rising.",
    "Watch whether posts change, Amapiano will keep rising.",
    "Watch whether posts change\nAmapiano will keep rising.",
    "Watch whether posts change - Amapiano will keep rising.",
    "Watch whether posts change and Amapiano will keep rising.",
])
def test_k9_scans_assertions_after_a_conditional_watch_clause(draft, ctx, wh, text):
    draft["watch_next"][0]["text"] = text

    answer, verdicts = run(draft, ctx, wh)

    assert answer["watch_next"] == []
    assert verdict(verdicts, "watch_next/0", "K9")["verdict"] == "cut"


def test_k9_a_matching_current_run_log_changes_reason_but_never_opens_publication(draft, ctx, wh):
    future = "Sunday plate ratings will keep rising."
    draft["claims"][2]["text"] = future
    ctx.forecasts["f_current"] = {"forecast_id": "f_current", "statement": future, "evidence_ids": ["tt_1"],
                                  "target": "reach_rising", "horizon": 7, "probability": 0.7}

    answer, verdicts = run(draft, ctx, wh)

    assert claim(answer, "c3") is None
    assert verdict(verdicts, "c3", "K9")["reason"] == (
        "forecast recorded in this run; publication held pending persistence proof")


def test_k9_a_shorter_logged_statement_does_not_match_a_larger_forecast(draft, ctx, wh):
    draft["claims"][2]["text"] = "Sunday plate ratings will keep rising, while a different trend is likely to spread."
    ctx.forecasts["f_short"] = {"forecast_id": "f_short", "statement": "Sunday plate ratings will keep rising",
                                 "evidence_ids": ["tt_1"], "target": "reach_rising", "horizon": 7,
                                 "probability": 0.7}

    answer, verdicts = run(draft, ctx, wh)

    assert claim(answer, "c3") is None
    assert verdict(verdicts, "c3", "K9")["reason"] == (
        "forecast has no matching current-run log; publication held pending persistence proof")


def test_k9_allows_a_verified_forecast_phrase_attributed_to_a_post(draft, ctx, wh):
    quoted = record("tt_future", "tiktok", "@creator.za", "A creator wrote will keep rising about Sunday plates.")
    ctx.evidence[quoted["id"]] = quoted
    draft["evidence"].append(copy.deepcopy(quoted))
    draft["claims"][2].update(text='A creator wrote "will keep rising" about Sunday plates.',
                              evidence_ids=[quoted["id"]],
                              quotes=[{"evidence_id": quoted["id"], "text": "will keep rising"}])

    answer, verdicts = run(draft, ctx, wh)

    assert claim(answer, "c3") is not None
    assert verdict(verdicts, "c3", "K9")["verdict"] == "pass"


@pytest.mark.parametrize("forecast", [
    {"forecast_id": "f_prior", "already_logged": True},
    {"forecast_id": "f_other", "statement": "A different trend will keep rising.", "evidence_ids": ["x_1"]},
])
def test_k9_prior_id_only_and_unrelated_logs_do_not_match(draft, ctx, wh, forecast):
    draft["claims"][2]["text"] = "Sunday plate ratings will keep rising."
    ctx.forecasts[forecast["forecast_id"]] = forecast

    answer, verdicts = run(draft, ctx, wh)

    assert claim(answer, "c3") is None
    assert verdict(verdicts, "c3", "K9")["reason"] == (
        "forecast has no matching current-run log; publication held pending persistence proof")


LINE = "The Sunday plate rating is expected to keep rising in South Africa over the next week."


def promoted_row(fid, **change):
    return {"forecast_id": fid, "item_id": "item_plates", "market": "ZA", "label": "The Sunday plate rating",
            "rule": "ask_v1", "target": "reach_rising", "horizon": 7, "score_run_id": "learn_1", **change}


class PromotingWarehouse(FakeWarehouse):
    """Answers the promoted-forecast read with the stored rows in promoted, every other query with rows."""

    def __init__(self, rows, promoted=()):
        super().__init__(rows)
        self.promoted = [promoted_row(p) if isinstance(p, str) else p for p in promoted]

    def run(self, sql, params, max_bytes_billed):
        if sql == PROMOTED_SQL:
            self.calls.append((sql, params, max_bytes_billed))
            return copy.deepcopy(self.promoted)
        return super().run(sql, params, max_bytes_billed)


def promoted_reads(wh):
    return [c for c in wh.calls if c[0] == PROMOTED_SQL]


def log_current(ctx, statement, fid="f_current", **extra):
    ctx.forecasts[fid] = {"forecast_id": fid, "statement": statement, "evidence_ids": ["tt_1"],
                          "target": "reach_rising", "horizon": 7, "probability": 0.7, **extra}


def test_k9_a_logged_forecast_from_a_promoted_cohort_is_published(draft, ctx):
    draft["claims"][2]["text"] = LINE
    draft["watch_next"] = [{"text": LINE, "claim_ids": ["c3"], "forecast": True}]
    log_current(ctx, LINE)
    logged = copy.deepcopy(ctx.forecasts)
    wh = PromotingWarehouse(ROWS, promoted=["f_current"])

    answer, verdicts = run(draft, ctx, wh)

    assert claim(answer, "c3")["text"] == LINE
    assert verdict(verdicts, "c3", "K9")["verdict"] == "pass"
    assert "f_current" in verdict(verdicts, "c3", "K9")["reason"]
    assert "learn_1" in verdict(verdicts, "c3", "K9")["reason"]
    assert answer["watch_next"] == [{"text": LINE, "claim_ids": ["c3"], "forecast": True}]
    assert len(promoted_reads(wh)) == 1
    assert promoted_reads(wh)[0][1]["today"] == date(2026, 9, 28)
    assert ctx.forecasts == logged, "the logged forecasts, which replay records export, are not changed"
    assert any(e["step"] == "forecast_promotion" and e["promoted"] == ["f_current"] for e in ctx.events)
    rechecked, _ = checks.recheck_fields(answer, ctx, window=WINDOW)
    assert rechecked["watch_next"] == answer["watch_next"]


@pytest.mark.parametrize(("statement", "stored"), [
    # the logged item is promoted as persist_50 while the statement names another item and asserts rising
    ("Sunday plate ratings will keep rising.", {"target": "persist_50"}),
    ("Sunday plate ratings will keep rising.", {}),
    # the right wording for a different item
    ("Pap and rice is expected to keep rising in South Africa over the next week.", {}),
    # the right item with the wrong target, horizon or market
    (LINE, {"target": "persist_50"}),
    (LINE, {"horizon": 14}),
    (LINE, {"market": "KE"}),
    # no named item: the label is the bare id
    ("item_plates is expected to keep rising in South Africa over the next week.", {"label": "item_plates"}),
])
def test_k9_a_promoted_cohort_never_publishes_a_statement_that_is_not_its_row(draft, ctx, statement, stored):
    draft["claims"][2]["text"] = statement
    log_current(ctx, statement)
    wh = PromotingWarehouse(ROWS, promoted=[promoted_row("f_current", **stored)])

    answer, verdicts = run(draft, ctx, wh)

    assert claim(answer, "c3") is None
    assert verdict(verdicts, "c3", "K9")["reason"] == (
        "forecast recorded in this run; publication held pending persistence proof")


def test_k9_two_logs_with_one_statement_promote_nothing(draft, ctx):
    draft["claims"][2]["text"] = LINE
    log_current(ctx, LINE, "f_one")
    log_current(ctx, LINE, "f_two")
    wh = PromotingWarehouse(ROWS, promoted=["f_one", promoted_row("f_two", item_id="item_other")])

    answer, verdicts = run(draft, ctx, wh)

    assert claim(answer, "c3") is None
    assert verdict(verdicts, "c3", "K9")["verdict"] == "cut"


def test_k9_a_stored_promoted_flag_is_not_trusted_without_the_read(draft, ctx):
    draft["claims"][2]["text"] = LINE
    log_current(ctx, LINE, promoted=True)

    answer, verdicts = run(draft, ctx, PromotingWarehouse(ROWS))

    assert claim(answer, "c3") is None
    assert verdict(verdicts, "c3", "K9")["reason"] == (
        "forecast recorded in this run; publication held pending persistence proof")


def test_k9_a_logged_forecast_outside_a_promoted_cohort_stays_cut_as_before(draft, ctx):
    draft["claims"][2]["text"] = LINE
    log_current(ctx, LINE)
    logged = copy.deepcopy(ctx.forecasts)
    wh = PromotingWarehouse(ROWS, promoted=["f_someone_else"])

    answer, verdicts = run(draft, ctx, wh)

    assert claim(answer, "c3") is None
    assert verdict(verdicts, "c3", "K9")["reason"] == (
        "forecast recorded in this run; publication held pending persistence proof")
    assert ctx.forecasts == logged
    assert not any(e["step"] == "forecast_promotion" for e in ctx.events)


def test_k9_a_promoted_log_does_not_open_a_different_forecast(draft, ctx):
    draft["claims"][2]["text"] = LINE[:-1] + ", while a different trend is likely to spread."
    log_current(ctx, LINE)
    wh = PromotingWarehouse(ROWS, promoted=["f_current"])

    answer, verdicts = run(draft, ctx, wh)

    assert claim(answer, "c3") is None
    assert verdict(verdicts, "c3", "K9")["reason"] == (
        "forecast has no matching current-run log; publication held pending persistence proof")


def test_k9_without_a_logged_forecast_there_is_no_promotion_read(draft, ctx):
    wh = PromotingWarehouse(ROWS, promoted=["f_current"])
    plain, plain_verdicts = run(copy.deepcopy(draft), make_ctx(), FakeWarehouse(ROWS))

    answer, verdicts = run(draft, ctx, wh)

    assert promoted_reads(wh) == []
    assert (answer, verdicts) == (plain, plain_verdicts)


def test_k9_a_failed_promotion_read_keeps_the_forecast_cut(draft, ctx):
    draft["claims"][2]["text"] = LINE
    log_current(ctx, LINE)

    class Down(FakeWarehouse):
        def run(self, sql, params, max_bytes_billed):
            if sql == PROMOTED_SQL:
                raise RuntimeError("backend down")
            return super().run(sql, params, max_bytes_billed)

    answer, verdicts = run(draft, ctx, Down(ROWS))

    assert claim(answer, "c3") is None
    assert verdict(verdicts, "c3", "K9")["verdict"] == "cut"


class ForecastSupportModel:
    def complete_json(self, *, system, user, schema, model, max_tokens):
        return {"verdict": "supported", "reason": "checked", "demographic_inference": False, "tone_claim": False,
                "forecast_assertion": True, "country_people": []}, {"input_tokens": 1, "output_tokens": 1, "usd": 0.0}


@pytest.mark.parametrize(("promoted", "kept"), [(["f_current"], True), ([], False)])
def test_k9_support_check_keeps_a_flagged_forecast_only_from_a_promoted_cohort(draft, ctx, promoted, kept):
    draft["claims"] = [dict(draft["claims"][2], text=LINE)]
    draft["so_what"] = [{"text": "Treat the rating habit as a weekly slot.", "claim_ids": ["c3"]}]
    draft["watch_next"] = []
    draft["short_answer"] = "Sunday plate rating looks like a weekly ritual."
    log_current(ctx, LINE)
    checked, _ = run(draft, ctx, PromotingWarehouse(ROWS, promoted=promoted))
    if not kept:
        assert checked["claims"] == []
        return
    assert [c["id"] for c in checked["claims"]] == ["c3"]

    answer, rows, _ = writer.apply_support(ForecastSupportModel(), checked, ctx)

    assert [c["id"] for c in answer["claims"]] == ["c3"]
    assert not any(r["rule"] == "K9" and r["verdict"] == "cut" for r in rows)


def test_k10_one_cut_makes_the_answer_partial(draft, ctx, wh):
    draft["claims"][2]["text"] = "Google Trends shows it is a weekly ritual."
    answer, verdicts = run(draft, ctx, wh)
    assert answer["status"] == "partial"
    assert answer["short_answer"] == draft["short_answer"]
    assert verdict(verdicts, "c3", "K10")["verdict"] == "cut"
    assert verdict(verdicts, "c1", "K10")["verdict"] == "pass"
    assert any("K6" in g["what"] + g["why"] and "c3" in g["what"] for g in answer["gaps"])


def test_k10_short_answer_resting_on_a_cut_claim_is_named(draft, ctx, wh):
    draft["claims"][2]["text"] = "Sunday plate rating looks like a weekly ritual, and Gen Z carries it."
    draft["short_answer"] = "Sunday plate rating looks like a weekly ritual rather than a passing joke."
    answer, verdicts = run(draft, ctx, wh)
    assert answer["status"] == "partial"
    assert "short answer" in verdict(verdicts, "c3", "K10")["reason"]


def test_k10_short_answer_citing_a_cut_claim_id(draft, ctx, wh):
    draft["claims"][2]["text"] += " Gen Z carries it."
    draft["short_answer"] = "Plate ratings look set to become a weekly slot (c3)."
    _, verdicts = run(draft, ctx, wh)
    assert "short answer" in verdict(verdicts, "c3", "K10")["reason"]


def test_k10_fewer_than_two_surviving_claims_is_insufficient_evidence(draft, ctx, wh):
    draft["claims"][0]["text"] += " Another 37 posts followed."
    draft["claims"][2]["text"] = "Google Trends shows it is a weekly ritual."
    answer, verdicts = run(draft, ctx, wh)
    assert claim_ids(answer) == ["c2"]
    assert answer["status"] == "insufficient_evidence"
    assert answer["short_answer"] != draft["short_answer"]
    assert "fewer than two" in answer["short_answer"]
    rules = {g["what"] for g in answer["gaps"]}
    assert any("c1" in w and "K2" in w for w in rules)
    assert any("c3" in w and "K6" in w for w in rules)
    assert all(set(g) == {"what", "searched", "why"} for g in answer["gaps"])


def test_so_what_and_watch_next_pointing_only_at_cut_claims_are_dropped(draft, ctx, wh):
    draft["claims"][0]["text"] += " Another 37 posts followed."
    draft["claims"][2]["text"] += " Gen Z carries it."
    draft["claims"].append(dict(copy.deepcopy(draft["claims"][1]), id="c4"))
    answer, _ = run(draft, ctx, wh)
    assert claim_ids(answer) == ["c2", "c4"]
    assert answer["so_what"] == [{"text": draft["so_what"][0]["text"], "claim_ids": ["c2"]}]
    assert answer["watch_next"] == []


def test_failed_sources_add_gaps(draft, ctx, wh):
    ctx.emit("socialcrawl", route="tiktok/search/hashtag", status="rate_limited", credits=0.0)
    ctx.emit("socialcrawl", route="x/search", status="ok", credits=2.0)
    answer, _ = run(draft, ctx, wh)
    assert len(answer["gaps"]) == 1
    gap = answer["gaps"][0]
    assert "tiktok/search/hashtag" in gap["searched"] and "rate_limited" in gap["why"]
    assert answer["status"] == "complete"


def test_a_failed_route_gets_exactly_one_gap_in_plain_words(draft, ctx, wh):
    ctx.emit("socialcrawl", route="threads/search", status="rate_limited", credits=0.0)
    ctx.emit("socialcrawl", route="threads/search", status="rate_limited", credits=0.0)
    ctx.emit("socialcrawl", route="threads/search", status="error", credits=0.0)
    ctx.emit("socialcrawl", route="youtube/search", status="partial", credits=1.0)
    answer, _ = run(draft, ctx, wh)
    assert answer["gaps"] == [
        {"what": "No live Threads data", "searched": "threads/search, 21 to 28 September", "why": "rate_limited, error"},
        {"what": "Live YouTube data is partial", "searched": "youtube/search, 21 to 28 September", "why": "partial"},
    ]


def test_draft_gaps_are_kept(draft, ctx, wh):
    draft["gaps"] = [{"what": "No Reddit posts", "searched": "reddit/search 7colours", "why": "zero results"}]
    draft["claims"][2]["text"] += " Gen Z carries it."
    answer, _ = run(draft, ctx, wh)
    assert answer["gaps"][0] == draft["gaps"][0]
    assert len(answer["gaps"]) == 2


# Reviewer probes, task 1.11

BANNED_ECHOES = ("gen z", "18-24", "year olds", "teens", "13-17", "search interest")


def all_text(answer, verdicts):
    parts = [v["reason"] for v in verdicts] + [answer.get("context", ""), answer["short_answer"]]
    parts += [g[k] for g in answer["gaps"] for k in ("what", "searched", "why")]
    return " | ".join(parts).lower()


def test_k6_context_breach_blanks_it_and_names_both_kinds(draft, ctx, wh):
    draft["context"] = "a Gen Z ritual among 18-24 year olds; Google Trends shows search interest up 5x"
    answer, verdicts = run(draft, ctx, wh)
    assert answer["context"] == ""
    assert answer["status"] == "complete"
    v = verdict(verdicts, "context", "K6")
    assert v["verdict"] == "cut"
    assert v["reason"] == "breach: age or generation term; Google Trends or search volume"
    assert not [t for t in BANNED_ECHOES if t in all_text(answer, verdicts)]


@pytest.mark.parametrize("field", ["what", "searched", "why"])
def test_k6_gap_breach_drops_that_gap(draft, ctx, wh, field):
    clean = {"what": "No Reddit posts", "searched": "reddit/search 7colours", "why": "zero results"}
    bad = dict(clean, **{field: "Teens 13-17 not measured"})
    draft["gaps"] = [bad, clean]
    answer, verdicts = run(draft, ctx, wh)
    assert answer["gaps"] == [clean]
    assert answer["status"] == "complete"
    v = verdict(verdicts, "gaps/0", "K6")
    assert v["verdict"] == "cut" and v["reason"] == "breach: age or generation term"
    assert [r for r in verdicts if r["claim_id"] == "gaps/1"] == []
    assert not [t for t in BANNED_ECHOES if t in all_text(answer, verdicts)]


def test_k6_search_volume_gap_reason(draft, ctx, wh):
    draft["gaps"] = [{"what": "Search volume not checked", "searched": "nothing", "why": "out of scope"}]
    answer, verdicts = run(draft, ctx, wh)
    assert answer["gaps"] == []
    assert verdict(verdicts, "gaps/0", "K6")["reason"] == "breach: Google Trends or search volume"


def test_k6_reasons_and_gaps_never_echo_the_matched_term(draft, ctx, wh):
    draft["claims"][2]["text"] = "Plate rating is a Gen Z ritual among 18-24 year olds, and search interest rose."
    draft["claims"][1]["text"] = "Creators on TikTok called it “Teens 13-17 love this”."
    draft["short_answer"] = "Sunday plate ratings rose, led by Gen Z."
    draft["so_what"][1]["text"] = "Aim the weekly slot at teens."
    draft["watch_next"][0]["text"] = "Watch search interest next Sunday."
    answer, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K6")["verdict"] == "cut"
    assert verdict(verdicts, "c2", "K6")["verdict"] == "cut"
    assert verdict(verdicts, "c2", "K8")["verdict"] == "cut"
    assert not [t for t in BANNED_ECHOES if t in all_text(answer, verdicts)]


def test_k2_short_answer_numerals_must_be_pinned(draft, ctx, wh):
    draft["short_answer"] = "Sunday plate ratings rose 340% this week across 9,000 posts."
    answer, verdicts = run(draft, ctx, wh)
    assert answer["short_answer"] == ""
    assert answer["status"] == "partial"
    v = verdict(verdicts, "short_answer", "K2")
    assert v["verdict"] == "cut" and "340%" in v["reason"] and "9,000" in v["reason"]
    assert any("Short answer" in g["what"] and "K2" in g["what"] for g in answer["gaps"])
    assert claim_ids(answer) == ["c1", "c2", "c3"]


def test_code_gaps_never_repeat_the_numeral_they_cut(draft, ctx, wh):
    draft["claims"][2]["text"] += " Plate ratings rose 340% this week."
    draft["short_answer"] = "Sunday plate ratings rose 340% this week."
    answer, verdicts = run(draft, ctx, wh)
    assert "340%" in verdict(verdicts, "c3", "K2")["reason"]
    assert "340%" in verdict(verdicts, "short_answer", "K2")["reason"]
    assert "340" not in json.dumps(answer["gaps"])
    whats = [g["what"] for g in answer["gaps"]]
    assert "Claim c3 cut: a number with no query behind it (K2)" in whats
    assert "Short answer removed: a number with no query behind it (K2)" in whats


SPAN = "the plate of the century"


def test_code_gaps_never_repeat_the_quoted_span_they_cut(draft, ctx, wh):
    draft["claims"][1]["text"] = f"Creators on TikTok called it “{SPAN}”."
    draft["so_what"][0]["text"] = f'One creator said "{SPAN}", so join in.'
    answer, verdicts = run(draft, ctx, wh)
    assert SPAN in verdict(verdicts, "c2", "K8")["reason"]
    assert SPAN in verdict(verdicts, "so_what/0", "K8")["reason"]
    assert SPAN not in json.dumps(answer["gaps"])
    whats = [g["what"] for g in answer["gaps"]]
    assert "Claim c2 cut: a quote not found in its posts (K8)" in whats
    assert "so_what item 0 removed: a quote not found in its posts (K8)" in whats


def test_recheck_fields_keeps_every_gap_the_checks_wrote_and_none_repeats_the_detail(draft, ctx, wh):
    ctx.evidence["old_1"] = record("old_1", "tiktok", "@old.plates", "An old plate post", posted_at="2026-08-01T10:00:00+02:00")
    draft["claims"][1]["text"] = f"Creators on TikTok called it “{SPAN}”."
    draft["claims"][2]["text"] += " Gen Z carries it. Another 37 posts followed."
    draft["claims"] += [
        {"id": "c4", "text": "An old plate post.", "label": "single_source", "kind": "observation",
         "evidence_ids": ["old_1"]},
        {"id": "c5", "text": "A missing plate post.", "label": "single_source", "kind": "observation",
         "evidence_ids": ["tt_404"]},
    ]
    draft["short_answer"] = "Sunday plate ratings rose 340% this week."
    draft["watch_next"][0]["text"] = f'Watch for "{SPAN}" posts.'
    answer, _ = run(draft, ctx, wh)
    whats = " ".join(g["what"] for g in answer["gaps"])
    assert all(f"({rule})" in whats for rule in ("K1", "K2", "K3", "K6", "K8"))
    text = json.dumps(answer["gaps"]).lower()
    assert not [t for t in ("340", "37", SPAN, *BANNED_ECHOES) if t in text]

    rechecked, verdicts = checks.recheck_fields(answer, ctx, window=WINDOW,
                                                code_gaps=checks.code_gap_indexes(draft["gaps"], answer["gaps"]))
    assert rechecked["gaps"] == answer["gaps"]
    assert [v for v in verdicts if v["claim_id"].startswith("gaps/")] == []


def test_k2_short_answer_may_repeat_numbers_of_a_surviving_claim(draft, ctx, wh):
    draft["short_answer"] = "Sunday plate ratings hit 412 posts on 27 September, 2.8x the usual day."
    answer, verdicts = run(draft, ctx, wh)
    assert answer["short_answer"] == draft["short_answer"]
    assert answer["status"] == "complete"
    assert [v for v in verdicts if v["claim_id"] == "short_answer"] == []


def test_k2_short_answer_numbers_of_a_cut_claim_do_not_count(draft, ctx, wh):
    draft["claims"][0]["text"] += " Another 37 posts followed."
    draft["short_answer"] = "Sunday plate ratings hit 412 posts."
    answer, verdicts = run(draft, ctx, wh)
    assert answer["short_answer"] == ""
    assert verdict(verdicts, "short_answer", "K2")["verdict"] == "cut"


def test_k2_and_k8_so_what_probe_is_dropped(draft, ctx, wh):
    draft["so_what"][0]["text"] = 'see 45% more reach; one creator said "this is the best plate in Mzansi"'
    answer, verdicts = run(draft, ctx, wh)
    assert [s["text"] for s in answer["so_what"]] == [draft["so_what"][1]["text"]]
    assert answer["status"] == "complete"
    k2, k8 = verdict(verdicts, "so_what/0", "K2"), verdict(verdicts, "so_what/0", "K8")
    assert k2["verdict"] == "cut" and "45%" in k2["reason"]
    assert k8["verdict"] == "cut" and "verbatim" in k8["reason"]
    assert any("so_what item 0" in g["what"] for g in answer["gaps"])


def test_k2_watch_next_probe_is_dropped(draft, ctx, wh):
    draft["watch_next"][0]["text"] = "Expect 3,000 posts"
    answer, verdicts = run(draft, ctx, wh)
    assert answer["watch_next"] == []
    v = verdict(verdicts, "watch_next/0", "K2")
    assert v["verdict"] == "cut" and "3,000" in v["reason"]


def test_k2_so_what_numeral_must_come_from_the_claims_it_rests_on(draft, ctx, wh):
    draft["so_what"][0] = {"text": "A slot built around 412 posts a day is worth a test.", "claim_ids": ["c2"]}
    answer, verdicts = run(draft, ctx, wh)
    assert len(answer["so_what"]) == 1
    assert verdict(verdicts, "so_what/0", "K2")["verdict"] == "cut"

    draft["so_what"][0]["claim_ids"] = ["c1"]
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert len(answer["so_what"]) == 2
    assert [v for v in verdicts if v["claim_id"] == "so_what/0"] == []


def test_k8_so_what_quote_must_be_in_a_record_its_claims_cite(draft, ctx, wh):
    draft["so_what"][0] = {"text": 'One creator said "Pap AND rice on one plate is a crime".', "claim_ids": ["c2"]}
    answer, _ = run(draft, ctx, wh)
    assert len(answer["so_what"]) == 2

    draft["so_what"][0]["text"] = 'One creator said "Rate my 7 colours honestly".'
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert len(answer["so_what"]) == 1
    assert verdict(verdicts, "so_what/0", "K8")["verdict"] == "cut"


def test_k8_short_answer_quote_must_be_verbatim(draft, ctx, wh):
    draft["short_answer"] = 'Creators called mixed starches "a crime against Sunday".'
    answer, verdicts = run(draft, ctx, wh)
    assert answer["short_answer"] == ""
    assert answer["status"] == "partial"
    assert verdict(verdicts, "short_answer", "K8")["verdict"] == "cut"

    draft["short_answer"] = 'Creators called mixed starches "a crime".'
    answer, _ = run(draft, ctx, FakeWarehouse(ROWS))
    assert answer["short_answer"] == draft["short_answer"]
    assert answer["status"] == "complete"


@pytest.mark.parametrize("ids", [[], None])
def test_k1_claim_without_evidence_ids_is_cut(draft, ctx, wh, ids):
    c3 = draft["claims"][2]
    if ids is None:
        del c3["evidence_ids"]
    else:
        c3["evidence_ids"] = ids
    answer, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K1")
    assert v["verdict"] == "cut" and "no evidence" in v["reason"]
    assert "c3" not in claim_ids(answer)
    assert answer["status"] == "partial"


@pytest.mark.parametrize("quote", ["a", "ating your 7 colours", "Pap AND ric", "crime and"])
def test_k1_quote_must_match_whole_words(draft, ctx, wh, quote):
    draft["claims"][1]["quotes"] = [{"evidence_id": "tt_2", "text": quote}]
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c2", "K1")
    assert v["verdict"] == "cut" and "verbatim" in v["reason"]


@pytest.mark.parametrize("quote", ["a crime", "Pap AND rice", "Rating your 7 colours plates"])
def test_k1_whole_word_quotes_pass(draft, ctx, wh, quote):
    draft["claims"][1]["quotes"] = [{"evidence_id": "tt_2", "text": quote}]
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c2", "K1")["verdict"] == "pass"


def test_k1_single_word_quote_needs_eight_characters(draft, ctx, wh):
    draft["claims"][0]["quotes"] = [{"evidence_id": "tt_1", "text": "beetroot"}]
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c1", "K1")["verdict"] == "pass"
    draft["claims"][0]["quotes"] = [{"evidence_id": "tt_1", "text": "Sunday"}]
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c1", "K1")["verdict"] == "cut"


@pytest.mark.parametrize("span", ["“a”", "“ating your 7 colours”", "'Pap AND ric'"])
def test_k8_quoted_span_must_match_whole_words(draft, ctx, wh, span):
    draft["claims"][1]["text"] = f"Creators on TikTok called it {span} on Sunday."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c2", "K8")["verdict"] == "cut"


@pytest.mark.parametrize("section", ["so_what", "watch_next"])
@pytest.mark.parametrize("ids", [[], None])
def test_items_without_claim_ids_are_dropped_with_a_verdict(draft, ctx, wh, section, ids):
    item = draft[section][0]
    if ids is None:
        del item["claim_ids"]
    else:
        item["claim_ids"] = ids
    answer, verdicts = run(draft, ctx, wh)
    assert item["text"] not in [s["text"] for s in answer[section]]
    v = verdict(verdicts, f"{section}/0", "K10")
    assert v["verdict"] == "cut" and "claim" in v["reason"]


def test_items_resting_only_on_cut_claims_get_a_verdict(draft, ctx, wh):
    draft["claims"][2]["text"] += " Another 37 posts followed."
    answer, verdicts = run(draft, ctx, wh)
    assert len(answer["so_what"]) == 1
    v = verdict(verdicts, "so_what/1", "K10")
    assert v["verdict"] == "cut" and "c3" in v["reason"]


def assumed(ids, stored=STORED):
    return [dict(r, flags=r["flags"] + ["market_assumed"]) if r["id"] in ids else r for r in stored]


def test_k3_market_assumed_records_alone_support_a_claim_that_names_no_place():
    # Albert, 29 September 2026: a post with no location may support a claim that names no place. This test cut c2
    # before; its text names no place, so it now passes.
    ctx = make_ctx(stored=assumed({"tt_1", "tt_2"}))
    answer, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c2", "K3")["verdict"] == "pass"
    assert "c2" in claim_ids(answer)


@pytest.mark.parametrize("text,markets", [
    ("Creators in Soweto called “Pap AND rice on one plate” a crime.", ["ZA"]),
    ("Kenyan creators called “Pap AND rice on one plate” a crime.", ["KE"]),
])
def test_k3_unlocated_posts_cannot_support_a_place_claim(text, markets):
    stored = assumed({"tt_1", "tt_2"}, [dict(r, market=markets[0]) for r in STORED])
    ctx = make_ctx(stored=stored)
    draft = make_draft(ctx)
    draft["claims"][1]["text"] = text
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=markets)
    v = verdict(verdicts, "c2", "K3")
    assert v["verdict"] == "cut" and f"located in {markets[0]}" in v["reason"]
    assert "tt_1" in v["reason"] and "tt_2" in v["reason"]
    assert "c2" not in claim_ids(answer)


def test_k3_one_located_post_supports_a_place_claim():
    ctx = make_ctx(stored=assumed({"tt_1"}))
    draft = make_draft(ctx)
    draft["claims"][1]["text"] = "Creators in Soweto called “Pap AND rice on one plate” a crime."
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c2", "K3")["verdict"] == "pass"


def test_k3_a_located_post_from_another_market_still_cuts_a_place_free_claim():
    stored = assumed({"tt_1"}, [dict(r, market="NG") if r["id"] == "tt_2" else r for r in STORED])
    ctx = make_ctx(stored=stored)
    _, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(ROWS))
    v = verdict(verdicts, "c2", "K3")
    assert v["verdict"] == "cut" and "tt_2 is from NG" in v["reason"] and "tt_1" not in v["reason"]


def test_k3_the_window_still_cuts_an_unlocated_post():
    stored = assumed({"tt_2"}, [dict(r, posted_at="2026-09-10T09:00:00+02:00") if r["id"] == "tt_2" else r
                                for r in STORED])
    ctx = make_ctx(stored=stored)
    _, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(ROWS))
    v = verdict(verdicts, "c2", "K3")
    assert v["verdict"] == "cut" and "tt_2 was posted 2026-09-10, outside" in v["reason"]


def test_k3_place_rule_holds_when_no_market_is_asked():
    ctx = make_ctx(stored=assumed({"tt_1", "tt_2"}))
    draft = make_draft(ctx)
    draft["claims"][1]["text"] = "Creators in Soweto called “Pap AND rice on one plate” a crime."
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=[])
    assert verdict(verdicts, "c2", "K3")["verdict"] == "cut"


# The place rule, row by row: (claim text, verdict when both cited posts are unlocated, verdict when both are located
# in ZA). The ask is about ZA. A ZA place passes only on a located ZA post. A place outside the asked markets (NG, KE
# or any other country) passes on a post located in an asked market, since the claim is about it, not from it, as in
# "South Africans rallied for Palestine" or "Bafana beat Nigeria"; with no located post it is cut (review round 1,
# which turned the located column of those rows from cut to pass). A place word inside a name ("Durban Poison", a
# strain; "the Durban July", a race; "the Soweto derby", a fixture) is not a place, and Chad, Jordan and Georgia are
# left off the country list as people's names, a trainer and a US state far more often than countries in social posts.
# Review round 3: people named by where they are from ("Kenyan creators", "Nigerians", "Zimbabwean fans") are a claim
# about people there, so they need a cited post located in their own market, with no home-market fallback; people
# from outside ZA, NG and KE are always cut. Those rows passed on located ZA posts before and are cut now. Review round
# 4 widened people to an NG, KE or foreign demonym and any noun, and to a place outside the asked market next to a
# people word or after in, from or based in ("Creators in Nairobi", "Lagos creators"), so those rows are cut too.
PLACE_ROWS = [
    ("Creators rated their Sunday plates.", "pass", "pass"),
    ("Creators in Soweto rated their Sunday plates.", "cut", "pass"),
    ("South African creators rated their Sunday plates.", "cut", "pass"),
    ("SA creators rated their Sunday plates.", "cut", "pass"),
    ("Mzansi creators rated their Sunday plates.", "cut", "pass"),
    ("Creators in Gauteng rated their Sunday plates.", "cut", "pass"),
    ("Joburg creators rated their Sunday plates.", "cut", "pass"),
    ("Jo'burg creators rated their Sunday plates.", "cut", "pass"),
    ("Creators in Johannesburg rated their Sunday plates.", "cut", "pass"),
    ("Creators in Cape Town rated their Sunday plates.", "cut", "pass"),
    ("Durban creators rated their Sunday plates.", "cut", "pass"),
    ("Creators in KwaZulu-Natal rated their Sunday plates.", "cut", "pass"),
    ("Creators in the Free State rated their Sunday plates.", "cut", "pass"),
    # review round 1: city demonyms and short forms
    ("Creators in Kwa-Zulu Natal rated their Sunday plates.", "cut", "pass"),
    ("Sowetan creators rated their Sunday plates.", "cut", "pass"),
    ("Capetonians rated their Sunday plates.", "cut", "pass"),
    ("Durbanites rated their Sunday plates.", "cut", "pass"),
    ("Joburgers rated their Sunday plates.", "cut", "pass"),
    ("Pretorians rated their Sunday plates.", "cut", "pass"),
    ("ZA creators rated their Sunday plates.", "cut", "pass"),
    ("RSA creators rated their Sunday plates.", "cut", "pass"),
    ("JHB creators rated their Sunday plates.", "cut", "pass"),
    ("CPT creators rated their Sunday plates.", "cut", "pass"),
    ("Durban Poison came up in the plate posts.", "pass", "pass"),
    ("Durban July hats came up in the plate posts.", "pass", "pass"),
    ("Soweto derby banter came up in the plate posts.", "pass", "pass"),
    ("Creators asked us to rate their Sunday plates.", "pass", "pass"),
    ("Creators said the free state of their plates was a joke.", "pass", "pass"),
    ("Jordan 4s showed up next to the plates.", "pass", "pass"),
    # places outside the asked market: cut on unlocated posts, backed by located ZA posts
    ("Kenyan creators rated their Sunday plates.", "cut", "cut"),
    ("Creators in Nairobi rated their Sunday plates.", "cut", "cut"),
    ("Creators in Mombasa rated their Sunday plates.", "cut", "cut"),
    ("Creators in Kisumu rated their Sunday plates.", "cut", "cut"),
    ("Nigerian creators rated their Sunday plates.", "cut", "cut"),
    ("Naija creators rated their Sunday plates.", "cut", "cut"),
    ("Creators in Lagos rated their Sunday plates.", "cut", "cut"),
    ("Creators in Abuja rated their Sunday plates.", "cut", "cut"),
    ("Creators in Kano rated their Sunday plates.", "cut", "cut"),
    ("Creators in Benin City rated their Sunday plates.", "cut", "cut"),
    ("Creators in Ghana rated their Sunday plates.", "cut", "cut"),
    ("Creators in the United States rated their Sunday plates.", "cut", "cut"),
    ("Creators in the US rated their Sunday plates.", "cut", "cut"),
    ("Creators in Zimbabwe rated their Sunday plates.", "cut", "cut"),
    ("Lagosians rated their Sunday plates.", "cut", "cut"),
    ("Nairobians rated their Sunday plates.", "cut", "cut"),
    ("Bafana beat Nigeria and plates came out.", "cut", "pass"),
    ("South Africans rallied for Palestine over Sunday plates.", "cut", "pass"),
    # review round 1: foreign demonyms, the neighbours as country and demonym, regions and foreign cities
    ("Ghanaian creators rated their Sunday plates.", "cut", "cut"),
    ("Zimbabwean creators rated their Sunday plates.", "cut", "cut"),
    ("British creators rated their Sunday plates.", "cut", "cut"),
    ("American creators rated their Sunday plates.", "cut", "cut"),
    ("Malawian creators rated their Sunday plates.", "cut", "cut"),
    ("Creators in Malawi rated their Sunday plates.", "cut", "cut"),
    ("Mozambican creators rated their Sunday plates.", "cut", "cut"),
    ("Creators in Mozambique rated their Sunday plates.", "cut", "cut"),
    ("Creators in Lesotho rated their Sunday plates.", "cut", "cut"),
    ("Creators in Eswatini rated their Sunday plates.", "cut", "cut"),
    ("Creators in Botswana rated their Sunday plates.", "cut", "cut"),
    ("Namibian creators rated their Sunday plates.", "cut", "cut"),
    ("Creators in Namibia rated their Sunday plates.", "cut", "cut"),
    ("Ugandan creators rated their Sunday plates.", "cut", "cut"),
    ("Creators in Uganda rated their Sunday plates.", "cut", "cut"),
    ("Tanzanian creators rated their Sunday plates.", "cut", "cut"),
    ("Creators in Tanzania rated their Sunday plates.", "cut", "cut"),
    ("Ethiopian creators rated their Sunday plates.", "cut", "cut"),
    ("Creators in Ethiopia rated their Sunday plates.", "cut", "cut"),
    ("Somalian creators rated their Sunday plates.", "cut", "cut"),
    ("Creators in Somalia rated their Sunday plates.", "cut", "cut"),
    ("Cameroonian creators rated their Sunday plates.", "cut", "cut"),
    ("Creators in Cameroon rated their Sunday plates.", "cut", "cut"),
    ("Congolese creators rated their Sunday plates.", "cut", "cut"),
    ("Creators in Congo rated their Sunday plates.", "cut", "cut"),
    ("West African creators rated their Sunday plates.", "cut", "cut"),
    ("East African creators rated their Sunday plates.", "cut", "cut"),
    ("Creators in Accra rated their Sunday plates.", "cut", "cut"),
    ("Creators in London rated their Sunday plates.", "cut", "cut"),
    # review round 3: the reviewer's probes, people named by a demonym, on posts located only in ZA
    ("Nigerian fans mocked Bafana over their Sunday plates.", "cut", "cut"),
    ("Kenyans online joked about their Sunday plates.", "cut", "cut"),
    ("Zimbabwean fans cheered their Sunday plates.", "cut", "cut"),
    ("Nigerians rated their Sunday plates.", "cut", "cut"),
    ("Ghanaians rated their Sunday plates.", "cut", "cut"),
    ("Kenyan TikTok creators rated their Sunday plates.", "cut", "cut"),
    ("Kenyan and South African fans rated their Sunday plates.", "cut", "cut"),
    ("Somalis rated their Sunday plates.", "cut", "cut"),
    ("Somali traders rated their Sunday plates.", "cut", "cut"),
    ("South African fans rated their Sunday plates.", "cut", "pass"),
    ("Sowetans rated their Sunday plates.", "cut", "pass"),
    # lower case and short forms of places outside the asked markets
    ("kenyan creators rated their Sunday plates.", "cut", "cut"),
    ("Creators in ghana rated their Sunday plates.", "cut", "cut"),
    ("Creators in Zim rated their Sunday plates.", "cut", "cut"),
    ("Creators in zimbabwe rated their Sunday plates.", "cut", "cut"),
    ("Creators posted in Somali about their Sunday plates.", "pass", "pass"),
    ("Creators said turkey beat chicken on their Sunday plates.", "pass", "pass"),
    # review round 4: an NG, KE or foreign demonym and any noun, and a non-home place next to a people word or after
    # in, from or based in, name people there
    ("Nigerian audiences embraced amapiano over Sunday plates.", "cut", "cut"),
    ("Kenyan TikTokers rated their Sunday plates.", "cut", "cut"),
    ("Nigerian Twitter rated their Sunday plates.", "cut", "cut"),
    ("Naija Twitter rated their Sunday plates.", "cut", "cut"),
    ("Kenyan women rated their Sunday plates.", "cut", "cut"),
    ("Nigerian listeners rated their Sunday plates.", "cut", "cut"),
    ("Kenya's creators rated their Sunday plates.", "cut", "cut"),
    ("Lagos creators rated their Sunday plates.", "cut", "cut"),
    ("Creators from Kenya rated their Sunday plates.", "cut", "cut"),
    ("Creators based in Lagos rated their Sunday plates.", "cut", "cut"),
    ("Nairobi TikTokers rated their Sunday plates.", "cut", "cut"),
    ("Lagos-based creators rated their Sunday plates.", "cut", "cut"),
    ("London creators rated their Sunday plates.", "cut", "cut"),
    ("Bafana beat Nigeria as fans cheered their Sunday plates.", "cut", "pass"),
    # brand and show names are no foreign people
    ("British Airways workers joked about their Sunday plates.", "pass", "pass"),
    ("American Idol fans rated their Sunday plates.", "pass", "pass"),
    ("African American creators rated their Sunday plates.", "pass", "pass"),
    # lower-case words that are also countries
    ("Creators topped their Sunday plates with brazil nuts.", "pass", "pass"),
    ("Creators packed their Sunday plates in togo boxes.", "pass", "pass"),
    ("Creators drank fiji water with their Sunday plates.", "pass", "pass"),
    # review round 5: people located in NG, KE or abroad, named by a place and a people noun in any order
    ("Fans across Nigeria embraced amapiano.", "cut", "cut"),
    ("Audiences throughout Kenya embraced amapiano.", "cut", "cut"),
    ("Listeners all over Lagos embraced amapiano.", "cut", "cut"),
    ("Fans around Nigeria embraced amapiano.", "cut", "cut"),
    ("The people of Kenya embraced amapiano.", "cut", "cut"),
    ("Across Nigeria, fans embraced amapiano.", "cut", "cut"),
    ("In Lagos, creators embraced amapiano.", "cut", "cut"),
    ("Amapiano has spread to Nigeria and Kenya, where fans are adopting it.", "cut", "cut"),
    ("Lagos partygoers embraced amapiano.", "cut", "cut"),
    ("Lagos streamers embraced amapiano.", "cut", "cut"),
    ("Nigeria's music lovers embraced amapiano.", "cut", "cut"),
    ("Nairobi's nightlife crowd embraced amapiano.", "cut", "cut"),
    ("Creators across the UK embraced amapiano.", "cut", "cut"),
    ("Kenyan netizens embraced amapiano.", "cut", "cut"),
    ("Lagos food creators embraced amapiano.", "cut", "cut"),
    # review round 5: a place or demonym as the topic or object, not where people are
    ("Nigerian Afrobeats topped playlists.", "cut", "pass"),
    ("Nigerian music dominated SA timelines.", "cut", "pass"),
    ("Creators in SA reacted to the Nigerian election.", "cut", "pass"),
    ("Nigerian Pidgin slang spread on SA TikTok.", "cut", "pass"),
    ("Creators remixed an American rapper's verse.", "cut", "pass"),
    ("Creators mocked the British accent.", "cut", "pass"),
    ("Bafana's win over Nigeria sent fans wild.", "cut", "pass"),
    ("Bafana beat Nigeria tonight fans say.", "cut", "pass"),
    ("Palestine got users talking.", "cut", "pass"),
    ("Support for Palestine drew creators out.", "cut", "pass"),
    ("Protests for Palestine brought people to Cape Town.", "cut", "pass"),
    ("Bafana beat Nigeria as fans cheered.", "cut", "pass"),
    ("South Africans rallied for Palestine.", "cut", "pass"),
    ("Kenya Airways crew went viral.", "pass", "pass"),
    ("Kenya Power outages got users talking.", "pass", "pass"),
    ("Nigerian Breweries ads got users talking.", "pass", "pass"),
]


@pytest.mark.parametrize("text,unlocated,located", PLACE_ROWS)
def test_k3_place_rows(text, unlocated, located):
    for flagged, expected in (({"tt_1", "tt_2"}, unlocated), (set(), located)):
        ctx = make_ctx(stored=assumed(flagged))
        draft = make_draft(ctx)
        c2 = draft["claims"][1]
        c2["text"], c2["quotes"] = text, []
        _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
        assert verdict(verdicts, "c2", "K3")["verdict"] == expected, (text, sorted(flagged))


def test_place_rows_hold_in_the_answer_fields():
    # The short answer, context, so_what and watch_next follow the same rule, against the claims they rest on.
    for flagged, expected in (({"tt_1", "tt_2", "tt_3", "x_1"}, "cut"), (set(), "pass")):
        ctx = make_ctx(stored=assumed(flagged))
        draft = make_draft(ctx)
        draft["short_answer"] = "Sunday lunch plate ratings rose in Soweto this week."
        draft["context"] = "Soweto has a long Sunday lunch habit."
        draft["so_what"][0]["text"] = "A food brand in Gauteng can join the Sunday rating ritual."
        draft["watch_next"][0]["text"] = "Watch whether plate ratings return in Soweto next Sunday."
        answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
        for where in ("short_answer", "context", "so_what/0", "watch_next/0"):
            rows = [v for v in verdicts if v["claim_id"] == where and v["rule"] == "K3"]
            assert (rows[0]["verdict"] if rows else "pass") == expected, (where, expected)
        if expected == "cut":
            assert answer["short_answer"] == "" and answer["context"] == ""
            assert len(answer["so_what"]) == 1 and answer["watch_next"] == []
            assert "located in ZA" in verdict(verdicts, "short_answer", "K3")["reason"]
        else:
            assert answer["short_answer"] == draft["short_answer"] and answer["context"] == draft["context"]


# DEMO-03 (progress/L3.md): a nationality attached to a named creator or person ("Nigerian creator @x", "@x, a
# Kenyan rapper") needs a cited post whose own text states that nationality for that person: the person's own post
# naming it, or a post that itself calls them by it. A post located in the market, or seen in its feeds, does not
# establish nationality, so place-located wording ("a creator posting from Lagos", "seen in Nigeria's feeds") and
# people wording without a named person keep the place rules above.
NATIONALITY_POSTS = [
    record("ng_skit", "tiktok", "@mr_funny_tv1", "Mama said no jollof till I finish this skit #skit", market="NG"),
    record("ng_self", "tiktok", "@ada.cooks", "Proud Nigerian creator here, jollof every Sunday #jollof", market="NG"),
    record("ng_about", "x", "@lagos.daily", "Nigerian comedian @mr_funny_tv1 just dropped another jollof skit",
           market="NG"),
    record("ng_named", "x", "@skit.watch", "Mr Funny, a Nigerian comedian, is back with a jollof skit", market="NG"),
    dict(record("ng_feed", "tiktok", "@mr_funny_tv1", "Another jollof skit for the weekend #skit", market="NG",
                flags=["market_assumed"]), source_market="NG"),
    record("ke_rap", "x", "@nairobi.beats", "Khaligraph Jones dropped a new verse tonight", market="KE"),
    record("ng_neg", "tiktok", "@mr_funny_tv1", "I'm not Nigerian lol, I just love jollof #skit", market="NG"),
    record("ng_born", "x", "@lagos.daily", "Born and raised in Lagos, still eating jollof every day", market="NG"),
    record("ng_burna", "x", "@afro.watch", "Burna Boy is Nigerian and he never lets you forget it", market="NG"),
]

NATIONALITY_ROWS = [
    # a nationality on a named person that no cited post states: cut, though the posts are located in NG or KE
    ("Nigerian creator @mr_funny_tv1 posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("Nigerian comedy creator @mr_funny_tv1 posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("@mr_funny_tv1, a Nigerian comedian, posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("@mr_funny_tv1 is a Nigerian creator who posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("@mr_funny_tv1 is Nigerian and posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("The Nigerian @mr_funny_tv1 posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("nigerian creator @mr_funny_tv1 posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("Nigerian comedian Mr Funny posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("Kenyan rapper Khaligraph Jones dropped a new verse.", ["ke_rap"], ["KE"], "cut"),
    ("Khaligraph Jones, a Kenyan rapper, dropped a new verse.", ["ke_rap"], ["KE"], "cut"),
    # the post states the nationality, but for someone else, or a different nationality
    ("Nigerian creator @ada.cooks posted a jollof skit.", ["ng_about"], ["NG"], "cut"),
    ("Ghanaian comedian @mr_funny_tv1 dropped another jollof skit.", ["ng_about"], ["NG"], "cut"),
    # a cited post's own text states it: the person's own post, or a post that calls them by it
    ("Nigerian comedian @mr_funny_tv1 dropped another jollof skit.", ["ng_about"], ["NG"], "pass"),
    ("@mr_funny_tv1, a Nigerian comedian, dropped another jollof skit.", ["ng_about", "ng_skit"], ["NG"], "pass"),
    ("Nigerian creator @ada.cooks shares jollof every Sunday.", ["ng_self"], ["NG"], "pass"),
    ("Nigerian comedian Mr Funny is back with a jollof skit.", ["ng_named"], ["NG"], "pass"),
    # place-located and market wording, and people with no named person, keep passing
    ("A creator posting from Lagos, @mr_funny_tv1, posted a jollof skit.", ["ng_skit"], ["NG"], "pass"),
    ("@mr_funny_tv1 posted a jollof skit, seen in Nigeria's feeds.", ["ng_feed"], ["NG"], "pass"),
    ("Jollof skits were seen in Nigeria's feeds.", ["ng_feed"], ["NG"], "pass"),
    ("Creators in Lagos posted jollof skits.", ["ng_skit"], ["NG"], "pass"),
    ("Nigerian creators posted jollof skits.", ["ng_skit"], ["NG"], "pass"),
    ("Posts from Nairobi carried a new verse.", ["ke_rap"], ["KE"], "pass"),
    ("Nigerian Breweries ads showed up next to the skits.", ["ng_skit"], ["NG"], "pass"),
    ("Creators remixed an American rapper's verse.", ["ke_rap"], ["KE"], "pass"),
    # review round 1 of the nationality rule: more forms of a nationality or birthplace on a named person
    ("@mr_funny_tv1 (Nigerian) posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    # review round 2: a plain name with no person noun is a person only with person context: a person noun before
    # it, or he, she, his or her later in the sentence ("Jollof Rice is Nigerian" names a dish)
    ("Burna Boy is Nigerian, and he posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("Rapper Burna Boy is Nigerian and posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("@mr_funny_tv1 is a Nigerian.", ["ng_skit"], ["NG"], "cut"),
    ("@mr_funny_tv1, who is Nigerian, posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("The Nigerian Burna Boy posted his jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("Nigerian-born @mr_funny_tv1 posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("Lagos-born @mr_funny_tv1 posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("Nigeria's @mr_funny_tv1 posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("Kenya's own Sauti Sol dropped a new verse.", ["ke_rap"], ["KE"], "cut"),
    ("Mzansi rapper @kay.plates rated a Sunday plate.", ["tt_3"], ["ZA"], "cut"),
    ("Naija comedian @mr_funny_tv1 posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    # the person's own post names the demonym only to deny it
    ("Nigerian comedian @mr_funny_tv1 loves jollof.", ["ng_neg"], ["NG"], "cut"),
    ("@mr_funny_tv1 is Nigerian and loves jollof.", ["ng_neg"], ["NG"], "cut"),
    # stated affirmatively by the person, or by a post about them, in the same or an equivalent word
    ("Naija creator @ada.cooks shares jollof every Sunday.", ["ng_self"], ["NG"], "pass"),
    ("Lagos-born @lagos.daily eats jollof every day.", ["ng_born"], ["NG"], "pass"),
    ("Burna Boy is Nigerian and never lets anyone forget it.", ["ng_burna"], ["NG"], "pass"),
    # no named person, an organisation, or a demonym on a thing
    ("Nigerian fans flooded @mr_funny_tv1's comments.", ["ng_skit"], ["NG"], "pass"),
    ("Nigerian creators like @mr_funny_tv1 posted jollof skits.", ["ng_skit"], ["NG"], "pass"),
    ("The Kenyan Premier League restarted as a new verse dropped.", ["ke_rap"], ["KE"], "pass"),
    ("Kenyan news account Tuko shared the new verse.", ["ke_rap"], ["KE"], "pass"),
    ("Showmax, a South African streamer, showed up next to the plates.", ["tt_1"], ["ZA"], "pass"),
    ("Pap is a South African staple on Sunday plates.", ["tt_1"], ["ZA"], "pass"),
    ("Nigeria's Super Eagles came up in the skits.", ["ng_skit"], ["NG"], "pass"),
    # review round 2: SA before a person noun, hyphenated demonyms, a possessive before a person noun
    ("SA rapper Nasty C rated a Sunday plate.", ["tt_1"], ["ZA"], "cut"),
    ("The Ghanaian-British star @mr_funny_tv1 posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("Nigeria's top comedian @mr_funny_tv1 posted a jollof skit.", ["ng_skit"], ["NG"], "cut"),
    ("SA artists rated their Sunday plates.", ["tt_1"], ["ZA"], "pass"),
    # review round 2: a possessive or a demonym before an event, a place, a scene, a brand or a dish names no person
    ("Nigeria's Independence Day trended next to the skits.", ["ng_skit"], ["NG"], "pass"),
    ("South Africa's Heritage Day braai posts rated Sunday plates.", ["tt_1"], ["ZA"], "pass"),
    ("Nigeria's Detty December trended next to the skits.", ["ng_skit"], ["NG"], "pass"),
    ("South Africa's Table Mountain showed up behind the plates.", ["tt_1"], ["ZA"], "pass"),
    ("Kenya's Mount Kenya showed up behind the verse.", ["ke_rap"], ["KE"], "pass"),
    ("Nigeria's Eid Celebrations came up in the skits.", ["ng_skit"], ["NG"], "pass"),
    ("Nigeria's Afrobeats Scene came up in the skits.", ["ng_skit"], ["NG"], "pass"),
    ("Nigeria's Dangote Cement came up in the skits.", ["ng_skit"], ["NG"], "pass"),
    ("Lagos's Third Mainland Bridge came up in the skits.", ["ng_skit"], ["NG"], "pass"),
    ("Jollof Rice is Nigerian, the skits said.", ["ng_skit"], ["NG"], "pass"),
    ("Ankara Print is Nigerian, the skits said.", ["ng_skit"], ["NG"], "pass"),
    ("The Nigerian Independence Day trended next to the skits.", ["ng_skit"], ["NG"], "pass"),
]


def nationality_draft(text, evidence_ids):
    ctx = make_ctx(stored=STORED + NATIONALITY_POSTS)
    draft = make_draft(ctx)
    draft["claims"][1].update(text=text, evidence_ids=evidence_ids, quotes=[])
    draft["evidence"].extend(copy.deepcopy(ctx.evidence[e]) for e in evidence_ids)
    return draft, ctx


@pytest.mark.parametrize("text,evidence_ids,markets,expected", NATIONALITY_ROWS)
def test_k3_nationality_of_a_named_person_needs_a_post_that_states_it(text, evidence_ids, markets, expected):
    draft, ctx = nationality_draft(text, evidence_ids)
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=markets)
    v = verdict(verdicts, "c2", "K3")
    assert v["verdict"] == expected, (text, v["reason"])
    assert (claim(answer, "c2") is None) is (expected == "cut")
    if expected == "cut":
        assert "nationality" in v["reason"] and "feed" in v["reason"]


def test_k3_nationality_of_a_named_person_holds_in_the_answer_fields():
    draft, ctx = nationality_draft("@mr_funny_tv1 posted a jollof skit, seen in Nigeria's feeds.", ["ng_skit"])
    draft["short_answer"] = "Nigerian creator @mr_funny_tv1 led the jollof skits this week."
    draft["so_what"][0]["text"] = "A food brand can join @mr_funny_tv1's jollof skits."
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG"])
    assert answer["short_answer"] == ""
    assert "nationality" in verdict(verdicts, "short_answer", "K3")["reason"]
    assert not [v for v in verdicts if v["claim_id"] == "so_what/0" and v["rule"] == "K3"]


def test_k3_nationality_of_a_named_person_holds_in_the_so_what():
    draft, ctx = nationality_draft("@mr_funny_tv1 posted a jollof skit, seen in Nigeria's feeds.", ["ng_skit"])
    draft["so_what"][0]["text"] = "A food brand can partner with Lagos-born @mr_funny_tv1 on jollof skits."
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG"])
    v = verdict(verdicts, "so_what/0", "K3")
    assert v["verdict"] == "cut" and "nationality" in v["reason"]
    assert len(answer["so_what"]) == 1


def test_a_field_naming_a_place_outside_the_market_needs_a_located_post():
    # review round 1: a place outside the asked market is backed by a post located in it; this was cut before on
    # located ZA posts.
    for flagged, expected in (({"tt_1", "tt_2", "tt_3", "x_1"}, "cut"), (set(), "pass")):
        ctx = make_ctx(stored=assumed(flagged))
        draft = make_draft(ctx)
        draft["so_what"][0]["text"] = "A food brand in Nairobi can join the Sunday rating ritual."
        _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
        rows = [v for v in verdicts if v["claim_id"] == "so_what/0" and v["rule"] == "K3"]
        assert (rows[0]["verdict"] if rows else "pass") == expected
        assert not rows or "Nairobi" in rows[0]["reason"]


def test_k3_a_place_in_an_asked_market_needs_a_post_located_in_that_market():
    draft, ctx = cross_market(["tt_2"], "Creators in Lagos rated their Sunday plates.")
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG"])
    v = verdict(verdicts, "c2", "K3")
    assert v["verdict"] == "cut" and "located in NG" in v["reason"]


def test_niger_delta_is_nigeria_and_niger_is_another_country():
    assert checks._places("Creators in the Niger Delta") == {"NG": ("Creators in the Niger Delta", True)}
    assert checks._places("Creators in Niger") == {"": ("Creators in Niger", True)}
    assert checks._places("Plates from the Niger Delta") == {"NG": ("Niger Delta", False)}
    assert checks._places("Plates from Niger") == {"": ("Niger", False)}


# Review round 3: a demonym naming people needs a cited post located in that people's own market.

@pytest.mark.parametrize("text,market", [
    ("Kenyan creators rated their Sunday plates.", "KE"),
    ("Kenyans online joked about their Sunday plates.", "KE"),
    ("Nigerian fans mocked Bafana over their Sunday plates.", "NG"),
])
def test_k3_people_named_by_a_demonym_need_a_post_located_in_their_market(text, market):
    ctx = make_ctx()
    draft = make_draft(ctx)
    c2 = draft["claims"][1]
    c2["text"], c2["quotes"] = text, []
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    v = verdict(verdicts, "c2", "K3")
    assert v["verdict"] == "cut" and f"located in {market}" in v["reason"]

    own = {"KE": "ke_1", "NG": "ng_1"}[market]
    draft, ctx = cross_market([own], text)
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", market])
    assert verdict(verdicts, "c2", "K3")["verdict"] == "pass"


def test_k3_people_from_outside_the_three_markets_are_always_cut():
    draft, ctx = cross_market(["tt_2", "ng_1", "ke_1"], "Zimbabwean fans cheered their Sunday plates.")
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG", "KE"])
    v = verdict(verdicts, "c2", "K3")
    assert v["verdict"] == "cut" and "Zimbabwean fans" in v["reason"] and "outside ZA, NG and KE" in v["reason"]


def test_k3_south_africans_rallied_for_palestine_passes_on_a_located_za_post():
    ctx = make_ctx()
    draft = make_draft(ctx)
    c2 = draft["claims"][1]
    c2["text"], c2["quotes"] = "South Africans rallied for Palestine over Sunday plates.", []
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c2", "K3")["verdict"] == "pass"
    assert checks._places(c2["text"]) == {"ZA": ("South Africans", True), "": ("Palestine", False)}


def test_a_field_naming_people_from_another_market_needs_a_post_located_there():
    ctx = make_ctx()
    draft = make_draft(ctx)
    draft["short_answer"] = "Kenyan creators rated Sunday plates this week."
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert answer["short_answer"] == ""
    assert "located in KE" in verdict(verdicts, "short_answer", "K3")["reason"]


@pytest.mark.parametrize("text,places", [
    ("Plates in ghana", {"": ("ghana", False)}),
    ("Plates in Zim", {"": ("Zim", False)}),
    ("Creators in ghana", {"": ("Creators in ghana", True)}),
    ("zim", {}),
    ("japan", {}),
    ("iceland", {}),
    ("brazil nuts", {}),
    ("Plates in Japan", {"": ("Japan", False)}),
    ("Kenya's creators", {"KE": ("Kenya's creators", True)}),
    ("Nigerian audiences", {"NG": ("Nigerian audiences", True)}),
    ("African American creators", {}),
    ("Somalis joked", {"": ("Somalis", True)}),
    ("Posts in Somali", {}),
    ("Creators asked us to rate", {}),
    ("Plates in the US", {"": ("US", False)}),
    ("Creators in the US", {"": ("Creators in the US", True)}),
    ("kenyans joked", {"KE": ("kenyans", True)}),
    ("Kenyan food creators", {"KE": ("Kenyan food creators", True)}),
    ("Kenya", {"KE": ("Kenya", False)}),
])
def test_places_reads_case_short_forms_and_people(text, places):
    assert checks._places(text) == places


def test_k5_x_and_twitter_are_one_platform():
    both = [record("tw_1", "twitter", "@naledi_eats", "Rate my Sunday plate honestly"),
            record("x_9", "x", "@bongi_eats", "Rate my Sunday plate honestly")]
    ctx = make_ctx(stored=STORED + both)
    draft = make_draft(ctx)
    draft["claims"][1].update(text="Creators asked for Sunday plate ratings.", evidence_ids=["tw_1", "x_9"],
                              quotes=[], label="corroborated")
    answer, verdicts = check_answer(draft, ctx, FakeWarehouse(ROWS), window=WINDOW, markets=["ZA"])
    assert verdict(verdicts, "c2", "K5")["verdict"] == "downgrade"
    assert claim(answer, "c2")["label"] == "observed"


def test_k3_one_observed_market_record_is_enough():
    ctx = make_ctx(stored=assumed({"tt_2"}))
    _, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c2", "K3")["verdict"] == "pass"


def test_k3_market_assumed_in_one_market_of_a_comparison_cuts():
    draft, ctx = cross_market(["tt_2", "ng_1"], "South African and Nigerian creators both rated their Sunday plates.")
    ctx.evidence["ng_1"]["flags"] = ["market_assumed"]
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG"])
    v = verdict(verdicts, "c2", "K3")
    assert v["verdict"] == "cut" and "ng_1" in v["reason"] and "tt_2" not in v["reason"]


def test_k3_market_assumed_is_ignored_when_no_market_is_asked():
    ctx = make_ctx(stored=assumed({"tt_1", "tt_2"}))
    _, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(ROWS), markets=[])
    assert verdict(verdicts, "c2", "K3")["verdict"] == "pass"


def test_k5_market_assumed_record_is_not_an_independent_author():
    ctx = make_ctx(stored=assumed({"tt_2"}))
    answer, verdicts = run(make_draft(ctx), ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c2", "K5")["verdict"] == "downgrade"
    assert claim(answer, "c2")["label"] == "single_source"


# Reviewer probes, task 1.11 round 2

def test_k2_and_k8_context_probe_is_blanked(draft, ctx, wh):
    draft["context"] = 'Fuel rose 12% in September; officials said "prices will fall soon".'
    answer, verdicts = run(draft, ctx, wh)
    assert answer["context"] == ""
    assert answer["status"] == "complete"
    k2, k8 = verdict(verdicts, "context", "K2"), verdict(verdicts, "context", "K8")
    assert k2["verdict"] == "cut" and "12%" in k2["reason"]
    assert k8["verdict"] == "cut" and "verbatim" in k8["reason"]
    assert claim_ids(answer) == ["c1", "c2", "c3"]


def test_context_may_repeat_numbers_and_quotes_of_surviving_claims(draft, ctx, wh):
    draft["context"] = 'Plate posts hit 412 on 27 September, and one creator said "Pap AND rice on one plate is a crime".'
    answer, verdicts = run(draft, ctx, wh)
    assert answer["context"] == draft["context"]
    assert [v for v in verdicts if v["claim_id"] == "context"] == []


def test_context_numbers_of_a_cut_claim_do_not_count(draft, ctx, wh):
    draft["claims"][0]["text"] += " Another 37 posts followed."
    draft["context"] = "Plate posts hit 412 on 27 September."
    answer, verdicts = run(draft, ctx, wh)
    assert answer["context"] == ""
    assert verdict(verdicts, "context", "K2")["verdict"] == "cut"


@pytest.mark.parametrize("field", ["what", "searched", "why"])
def test_k2_gap_numeral_probe_is_dropped(draft, ctx, wh, field):
    clean = {"what": "No Reddit posts", "searched": "reddit/search 7colours", "why": "zero results"}
    bad = dict(clean, **{field: "3,400 posts were not placed"})
    draft["gaps"] = [bad, clean]
    answer, verdicts = run(draft, ctx, wh)
    assert answer["gaps"] == [clean]
    assert answer["status"] == "complete"
    v = verdict(verdicts, "gaps/0", "K2")
    assert v["verdict"] == "cut" and "3,400" in v["reason"]
    assert [r for r in verdicts if r["claim_id"] == "gaps/1"] == []
    assert "3,400" not in json.dumps(answer)


def test_k2_gap_numeral_inside_quotes_is_still_scanned(draft, ctx, wh):
    draft["gaps"] = [{"what": "Unplaced posts", "searched": "stored posts", "why": 'a tally of "3,400 posts" was not placed'}]
    answer, verdicts = run(draft, ctx, wh)
    assert answer["gaps"] == []
    assert verdict(verdicts, "gaps/0", "K2")["verdict"] == "cut"


def test_gap_dates_window_length_and_route_names_are_allowed(draft, ctx, wh):
    gap = {"what": "No Reddit posts in the last 8 days",
           "searched": "reddit/search/v2 and x/search, 21 to 28 September, 2026-09-21 to 2026-09-28",
           "why": "zero results since 27 September, the quietest week in 2026"}
    draft["gaps"] = [gap]
    answer, verdicts = run(draft, ctx, wh)
    assert answer["gaps"] == [gap]
    assert [v for v in verdicts if v["claim_id"] == "gaps/0"] == []


def test_gap_numeral_other_than_the_window_length_is_dropped(draft, ctx, wh):
    draft["gaps"] = [{"what": "No Reddit posts in the last 30 days", "searched": "reddit/search", "why": "zero results"}]
    answer, verdicts = run(draft, ctx, wh)
    assert answer["gaps"] == []
    assert verdict(verdicts, "gaps/0", "K2")["verdict"] == "cut"


def test_gap_may_repeat_a_number_of_a_surviving_claim_only(draft, ctx, wh):
    gap = {"what": "The 412 posts on 27 September were not split by platform", "searched": "q_1", "why": "no platform column"}
    draft["gaps"] = [gap]
    answer, _ = run(draft, ctx, wh)
    assert answer["gaps"] == [gap]

    draft["claims"][0]["text"] += " Another 37 posts followed."
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert gap not in answer["gaps"]
    assert verdict(verdicts, "gaps/0", "K2")["verdict"] == "cut"


def test_k1_repeated_claim_id_is_cut_and_the_first_is_kept(draft, ctx, wh):
    first = draft["claims"][0]
    repeat = dict(copy.deepcopy(draft["claims"][1]), id="c1")
    draft["claims"].append(repeat)
    answer, verdicts = run(draft, ctx, wh)
    assert claim_ids(answer) == ["c1", "c2", "c3"]
    assert claim(answer, "c1")["text"] == first["text"]
    k1 = [v for v in verdicts if v["claim_id"] == "c1" and v["rule"] == "K1"]
    assert [v["verdict"] for v in k1] == ["pass", "cut"]
    assert "repeats" in k1[1]["reason"]
    assert answer["status"] == "partial"
    assert any(g["what"] == "Claim c1 cut: a citation that does not match its stored post (K1)" for g in answer["gaps"])


# recheck_fields: the answer-level checks again after the support check has cut claims

FUEL_ROWS = [{"day": "2026-09-27", "posts": 1250, "ratio": 2.8}]


class SupportModel:
    def __init__(self, *verdicts):
        self.verdicts = list(verdicts)

    def complete_json(self, *, system, user, schema, model, max_tokens):
        return {"verdict": self.verdicts.pop(0), "reason": "checked", "demographic_inference": False,
                "tone_claim": False, "forecast_assertion": False, "country_people": []}, \
            {"input_tokens": 1, "output_tokens": 1, "usd": 0.0}


def fuel_answer():
    ctx = make_ctx(rows=FUEL_ROWS)
    draft = make_draft(ctx)
    draft["claims"][0] = {"id": "c1", "text": "1,250 posts talk about fuel prices", "label": "observed",
                          "kind": "observation", "evidence_ids": ["tt_1", "tt_2"], "quotes": [],
                          "numbers": [number(ctx, 1250, "posts")]}
    draft["short_answer"] = "1,250 posts in South Africa talk about fuel."
    draft["so_what"] = [{"text": "Brands can speak to 1,250 fuel posters.", "claim_ids": ["c1", "c2"]},
                        {"text": "Treat the rating habit as a weekly slot.", "claim_ids": ["c3"]}]
    draft["watch_next"] = [{"text": "Watch whether plate ratings return next Sunday.", "claim_ids": ["c2"], "forecast": False}]
    checked, verdicts = run(draft, ctx, FakeWarehouse(FUEL_ROWS))
    assert checked["status"] == "complete" and verdicts and all(v["verdict"] == "pass" for v in verdicts)
    answer, _, _ = writer.apply_support(SupportModel("unsupported", "supported", "supported"), checked, ctx)
    return answer, ctx


def test_recheck_fields_probe_removes_numbers_of_a_support_cut_claim():
    answer, ctx = fuel_answer()
    assert answer["short_answer"] == "1,250 posts in South Africa talk about fuel."
    assert answer["so_what"][0] == {"text": "Brands can speak to 1,250 fuel posters.", "claim_ids": ["c2"]}
    before = copy.deepcopy(answer)

    rechecked, verdicts = checks.recheck_fields(answer, ctx)
    assert answer == before, "recheck_fields must not mutate its input"
    assert validate_answer(rechecked) == []
    assert rechecked["short_answer"] == ""
    assert [s["text"] for s in rechecked["so_what"]] == ["Treat the rating habit as a weekly slot."]
    assert LABEL_ORDER.index(rechecked["status"]) >= LABEL_ORDER.index("partial")
    assert rechecked["claims"] == before["claims"] and rechecked["evidence"] == before["evidence"]
    assert verdict(verdicts, "short_answer", "K2")["verdict"] == "cut"
    assert verdict(verdicts, "so_what/0", "K2")["verdict"] == "cut"
    assert all(v["checker"] == "code" and v["run_id"] == RUN for v in verdicts)
    assert "1,250" not in rechecked["short_answer"] + json.dumps(rechecked["so_what"])


LABEL_ORDER = ["complete", "partial", "insufficient_evidence"]


def test_recheck_fields_leaves_a_clean_answer_alone(draft, ctx, wh):
    draft["gaps"] = [{"what": "No Reddit posts", "searched": "reddit/search 7colours", "why": "zero results"}]
    draft["claims"][2]["text"] += " Another 37 posts followed."
    ctx.emit("socialcrawl", route="threads/search", status="rate_limited", credits=0.0)
    answer, _ = run(draft, ctx, wh)
    assert any(g["what"] == "Claim c3 cut: a number with no query behind it (K2)" for g in answer["gaps"])
    assert "37" not in json.dumps(answer["gaps"]), "the code's own K2 gap never names the numeral it cut"
    rechecked, verdicts = checks.recheck_fields(answer, ctx, window=WINDOW,
                                                code_gaps=checks.code_gap_indexes(draft["gaps"], answer["gaps"]))
    assert rechecked == answer
    assert verdicts == []


def test_recheck_fields_drops_a_drafted_gap_pinned_only_by_a_support_cut_claim():
    answer, ctx = fuel_answer()
    gap = {"what": "The 1,250 fuel posts were not split by city", "searched": "q_1", "why": "no city column"}
    answer["gaps"].insert(0, gap)
    rechecked, verdicts = checks.recheck_fields(answer, ctx)
    assert gap not in rechecked["gaps"]
    assert verdict(verdicts, "gaps/0", "K2")["verdict"] == "cut"


def test_recheck_fields_allows_the_window_length_when_given_the_window():
    answer, ctx = fuel_answer()
    gap = {"what": "No Reddit posts in the last 8 days", "searched": "reddit/search", "why": "zero results"}
    answer["gaps"].insert(0, gap)
    rechecked, _ = checks.recheck_fields(answer, ctx, window=WINDOW)
    assert gap in rechecked["gaps"]


def test_recheck_fields_k6_and_k8_on_every_field():
    answer, ctx = fuel_answer()
    answer["short_answer"] = "Plate ratings are a Gen Z habit."
    answer["context"] = 'Officials said "prices will fall soon".'
    answer["watch_next"][0]["text"] = 'Watch for "the plate of the year" posts.'
    answer["gaps"].insert(0, {"what": "Teens not measured", "searched": "nothing", "why": "out of scope"})
    rechecked, verdicts = checks.recheck_fields(answer, ctx)
    assert rechecked["short_answer"] == "" and rechecked["context"] == "" and rechecked["watch_next"] == []
    assert verdict(verdicts, "short_answer", "K6")["reason"] == "breach: age or generation term"
    assert verdict(verdicts, "context", "K8")["verdict"] == "cut"
    assert verdict(verdicts, "watch_next/0", "K8")["verdict"] == "cut"
    assert verdict(verdicts, "gaps/0", "K6")["verdict"] == "cut"
    assert not [t for t in BANNED_ECHOES if t in all_text(rechecked, verdicts)]


def test_recheck_fields_status_rule_only_lowers():
    answer, ctx = fuel_answer()
    answer["status"] = "complete"
    rechecked, _ = checks.recheck_fields(answer, ctx)
    assert rechecked["status"] == "partial"

    answer["status"] = "refused"
    rechecked, _ = checks.recheck_fields(answer, ctx)
    assert rechecked["status"] == "refused"

    answer["status"], answer["claims"] = "complete", answer["claims"][:1]
    answer["short_answer"] = "Sunday plate ratings rose."
    rechecked, _ = checks.recheck_fields(answer, ctx)
    assert rechecked["status"] == "insufficient_evidence" and rechecked["short_answer"] == checks.INSUFFICIENT
    assert rechecked["claims"] == answer["claims"]


# Reviewer probes, task 1.11 round 3

WIDE_ROWS = [dict(ROWS[0], views=900000, views_k=900, price=450, rank=3, reach=5000000, reach_m=5,
                  spend=2412345678, spend_bn=2.4, place=12, naira=5000), ROWS[1]]


def test_k2_claim_scaled_and_currency_probe_cuts(draft, ctx, wh):
    draft["claims"][2]["text"] = "900k views and plates cost R450, so plate rating looks like a weekly ritual."
    answer, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K2")
    assert v["verdict"] == "cut" and "900k" in v["reason"] and "R450" in v["reason"]
    assert "c3" not in claim_ids(answer)


def test_k2_short_answer_scaled_and_ordinal_probe_is_blanked(draft, ctx, wh):
    draft["short_answer"] = "Sunday plate ratings hit 900k views, ranked 3rd on TikTok."
    answer, verdicts = run(draft, ctx, wh)
    assert answer["short_answer"] == ""
    assert answer["status"] == "partial"
    v = verdict(verdicts, "short_answer", "K2")
    assert v["verdict"] == "cut" and "900k" in v["reason"]

    # An ordinal no entry shows is cut.
    draft["short_answer"] = "Sunday plate ratings ranked 4th on TikTok."
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert answer["short_answer"] == ""
    v = verdict(verdicts, "short_answer", "K2")
    assert v["verdict"] == "cut" and "4th" in v["reason"]


@pytest.mark.parametrize("figure,value,expected", [
    ("900k", 900000, "pass"), ("900k", 900, "cut"), ("900k", None, "cut"),
    ("5M", 5000000, "pass"), ("5M", 5, "cut"), ("5M", None, "cut"),
    ("2.4bn", 2412345678, "pass"), ("2.4bn", 2.4, "cut"), ("2.4bn", None, "cut"),
    ("R450", 450, "pass"), ("R450", None, "cut"),
    ("3rd", 3, "pass"), ("4th", None, "cut"),
    ("12th", 12, "pass"), ("12th", None, "cut"),
    ("N5,000", 5000, "pass"), ("N5,000", None, "cut"),
    ("\u20a65,000", 5000, "pass"), ("\u20a65,000", None, "cut"),
    ("Ksh450", 450, "pass"), ("KSH450", 450, "pass"), ("ksh450", None, "cut"), ("KSh450", None, "cut"),
])
def test_k2_scale_currency_and_ordinal_figures_must_match_a_numbers_entry(figure, value, expected):
    ctx = make_ctx(rows=WIDE_ROWS)
    draft = make_draft(ctx)
    draft["claims"][0]["text"] += f" One plate figure was {figure}."
    if value is not None:
        draft["claims"][0]["numbers"].append(number(ctx, value, "plate figure"))
    _, verdicts = run(draft, ctx, FakeWarehouse(WIDE_ROWS))
    assert verdict(verdicts, "c1", "K2")["verdict"] == expected


@pytest.mark.parametrize("figure", ["3rd", "3", "3 posts"])
@pytest.mark.parametrize("value,expected", [(None, "cut"), (3, "pass"), (3.0, "pass")])
def test_k2_a_whole_numeral_needs_an_entry_of_that_exact_integer(figure, value, expected):
    # c1's only non-integer entry is the 2.8 ratio, which rounds to 3 but must not pin a whole 3.
    ctx = make_ctx(rows=WIDE_ROWS)
    draft = make_draft(ctx)
    draft["claims"][0]["text"] += f" It ranked {figure}."
    if value is not None:
        draft["claims"][0]["numbers"].append(number(ctx, value, "rank"))
    _, verdicts = run(draft, ctx, FakeWarehouse(WIDE_ROWS))
    assert verdict(verdicts, "c1", "K2")["verdict"] == expected


def test_k2_short_answer_whole_numeral_is_not_pinned_by_a_rounded_ratio(draft, ctx, wh):
    draft["short_answer"] = "Sunday plate ratings ranked 3rd on TikTok."
    answer, verdicts = run(draft, ctx, wh)
    assert answer["short_answer"] == ""
    assert "3rd" in verdict(verdicts, "short_answer", "K2")["reason"]


@pytest.mark.parametrize("figure,expected", [("2.8", "pass"), ("2.80", "cut"), ("2.9", "cut")])
def test_k2_a_decimal_numeral_keeps_rounding_to_its_shown_decimals(figure, expected):
    ctx = make_ctx(rows=[dict(ROWS[0], ratio=2.8123), ROWS[1]])
    draft = make_draft(ctx)
    draft["claims"][0]["numbers"][1] = number(ctx, 2.8123, "times the usual daily count")
    draft["claims"][0]["text"] = draft["claims"][0]["text"].replace("2.8x", f"{figure}x")
    _, verdicts = run(draft, ctx, FakeWarehouse([dict(ROWS[0], ratio=2.8123), ROWS[1]]))
    assert verdict(verdicts, "c1", "K2")["verdict"] == expected


def test_k2_short_answer_may_repeat_a_scaled_figure_of_a_surviving_claim():
    ctx = make_ctx(rows=WIDE_ROWS)
    draft = make_draft(ctx)
    draft["claims"][0]["text"] += " The top plate reached 900,000 views."
    draft["claims"][0]["numbers"].append(number(ctx, 900000, "views"))
    draft["short_answer"] = "Sunday plate ratings reached 900k views."
    answer, verdicts = run(draft, ctx, FakeWarehouse(WIDE_ROWS))
    assert answer["short_answer"] == draft["short_answer"]
    assert [v for v in verdicts if v["claim_id"] == "short_answer"] == []


def test_k2_still_ignores_ordinal_dates_clock_times_and_ids(draft, ctx, wh):
    draft["claims"][2]["text"] = (
        "On 27th September at 12:30, and again on September 26th, tt_1 and @kay.plates posted plates, "
        "which looks like a weekly ritual.")
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "pass"


def test_k8_translated_claim_span_probe_cuts_and_its_numeral_is_scanned(draft, ctx, wh):
    draft["claims"][1]["text"] = 'One post says "plate ratings are up 900% this week" (translated from isiZulu).'
    draft["claims"][1]["evidence_ids"] = ["x_2", "tt_2"]
    draft["claims"][1]["quotes"] = []
    answer, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c2", "K8")["verdict"] == "cut"
    k2 = verdict(verdicts, "c2", "K2")
    assert k2["verdict"] == "cut" and "900%" in k2["reason"]
    assert "c2" not in claim_ids(answer)


def test_k8_translated_short_answer_span_probe_is_blanked(draft, ctx, wh):
    draft["short_answer"] = 'fans say "ratings up 900%" (translated).'
    answer, verdicts = run(draft, ctx, wh)
    assert answer["short_answer"] == ""
    assert verdict(verdicts, "short_answer", "K8")["verdict"] == "cut"
    k2 = verdict(verdicts, "short_answer", "K2")
    assert k2["verdict"] == "cut" and "900%" in k2["reason"]


@pytest.mark.parametrize("section", ["context", "so_what", "watch_next"])
def test_k8_translated_span_in_other_fields_is_removed(draft, ctx, wh, section):
    text = 'fans say "ratings up 900%" (translated).'
    if section == "context":
        draft["context"] = text
    else:
        draft[section][0]["text"] = text
    answer, verdicts = run(draft, ctx, wh)
    where = "context" if section == "context" else f"{section}/0"
    assert verdict(verdicts, where, "K8")["verdict"] == "cut"
    assert verdict(verdicts, where, "K2")["verdict"] == "cut"
    assert text not in json.dumps(answer)


@pytest.mark.parametrize("phrase", ["Gen Zers", "Gen-Zers", "GenZers", "Gen Xers", "Gen Z's", "Gen Zs",
                                    "Gen‐Z", "Gen‑Z", "Gen‑Zers", "genz",
                                    "Gen\u2013Z", "Gen\u2014Z", "Gen\u2013Zers"])
def test_k6_generation_forms_are_a_breach(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"Plate rating looks like a {phrase} ritual."
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K6")
    assert v["verdict"] == "cut" and v["reason"] == "breach: age or generation term"


@pytest.mark.parametrize("phrase", ["a generation of content", "a general habit", "a genuine habit"])
def test_k6_words_that_start_with_gen_are_not_a_breach(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"Plate rating looks like {phrase}."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K6")["verdict"] == "pass"


@pytest.mark.parametrize("phrase", ["google_trends", "google-trends", "Google Trends", "googletrends"])
def test_k6_google_trends_spellings_are_a_breach(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"The {phrase} route also rose, so it looks like a ritual."
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K6")
    assert v["verdict"] == "cut" and v["reason"] == "breach: Google Trends or search volume"


@pytest.mark.parametrize("phrase", ["in 2026", "In 2026", "the 2026", "since 2025", "during 2024"])
def test_k2_claim_year_phrases_need_no_numbers_entry(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"Plate rating {phrase} looks like a weekly ritual."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "pass"


def test_k2_bare_year_in_a_claim_still_needs_a_numbers_entry(draft, ctx, wh):
    draft["claims"][2]["text"] = "Plate rating reached 2026 and looks like a weekly ritual."
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K2")
    assert v["verdict"] == "cut" and "2026" in v["reason"]


# Reviewer probes, task 1.11 round 4: every run of digits is a figure unless it is an identifier or an allowed form

FIGURE_PROBES = ["2.4B", "2B", "5mn", "3million", "USD5", "ZAR450", "KES500", "NGN5000", "top10", "Q3", "x2",
                 "1in5", "No.1", "#1"]


@pytest.mark.parametrize("figure", FIGURE_PROBES)
def test_k2_unpinned_figure_in_any_form_cuts_the_claim(draft, ctx, wh, figure):
    draft["claims"][2]["text"] = f"Plate rating hit {figure}, which looks like a weekly ritual."
    answer, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "cut"
    assert "c3" not in claim_ids(answer)


@pytest.mark.parametrize("figure", FIGURE_PROBES)
def test_k2_unpinned_figure_in_any_form_blanks_the_short_answer(draft, ctx, wh, figure):
    draft["short_answer"] = f"Sunday plate ratings hit {figure} on TikTok."
    answer, verdicts = run(draft, ctx, wh)
    assert answer["short_answer"] == ""
    assert answer["status"] == "partial"
    assert verdict(verdicts, "short_answer", "K2")["verdict"] == "cut"


def test_k2_views_rank_and_price_probe_claim_is_cut(draft, ctx, wh):
    draft["claims"][2]["text"] = "views past 2.4B and the #1 sound at USD5"
    answer, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K2")
    assert v["verdict"] == "cut" and "2.4B" in v["reason"] and "USD5" in v["reason"]
    assert v["reason"].count("numeral ") == 3
    assert "c3" not in claim_ids(answer)


PROBE_ROWS = [dict(ROWS[0], spend=2412345678, budget=2000000000, reach=5000000, fans=3000000, usd=5, zar=450,
                   kes=500, ngn=5000, top=10, quarter=3, times=2, one=1), ROWS[1]]


@pytest.mark.parametrize("figure,values,expected", [
    ("2.4B", [2412345678], "pass"), ("2.4B", [2.4], "cut"),
    ("2B", [2000000000], "pass"), ("2B", [2], "cut"),
    ("5mn", [5000000], "pass"), ("5mn", [5], "cut"),
    ("3million", [3000000], "pass"), ("3million", [3], "cut"),
    ("5 million", [5000000], "pass"), ("5 million", [5], "cut"),
    ("USD5", [5], "pass"), ("ZAR450", [450], "pass"), ("KES500", [500], "pass"), ("NGN5000", [5000], "pass"),
    ("top10", [10], "pass"), ("Q3", [3], "pass"), ("x2", [2], "pass"),
    ("1in5", [1, 5], "pass"), ("1in5", [1], "cut"),
    ("No.1", [1], "pass"), ("#1", [1], "pass"),
])
def test_k2_every_figure_form_is_pinned_by_its_value(figure, values, expected):
    ctx = make_ctx(rows=PROBE_ROWS)
    draft = make_draft(ctx)
    draft["claims"][0]["text"] += f" One plate figure was {figure}."
    draft["claims"][0]["numbers"] += [number(ctx, v, "plate figure") for v in values]
    _, verdicts = run(draft, ctx, FakeWarehouse(PROBE_ROWS))
    assert verdict(verdicts, "c1", "K2")["verdict"] == expected


@pytest.mark.parametrize("text", [
    "Plate posts under #amapiano2026 and #7colours look like a weekly ritual.",
    "Plate posts like those in c1 and c2 look like a weekly ritual.",
    "Posts p12 and tt_3 look like a weekly ritual.",
    "Posts from reddit/search/v2 look like a weekly ritual.",
    "Posts such as https://example.test/p/2026/991 look like a weekly ritual.",
    "Posts on 27 September at 12:30 and on 2026-09-26 look like a weekly ritual.",
])
def test_k2_identifiers_and_allowed_forms_need_no_numbers_entry(text):
    # the hashtags and the link resolve to stored posts; test_claims_cut_an_id_handle_and_hashtag_that_do_not_resolve
    # and BROAD_PROBES cover the ones that do not
    ctx = make_ctx(stored=STORED + [record("p12", "tiktok", "@plate.p12", "Rate my Sunday plate #7colours #amapiano2026",
                                           url="https://example.test/p/2026/991")])
    draft = make_draft(ctx)
    draft["claims"][2]["text"] = text
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c3", "K2")["verdict"] == "pass"


@pytest.mark.parametrize("text", [
    "Posts p13 look like a weekly ritual.",
    "Posts per views/1000 look like a weekly ritual.",
])
def test_k2_letter_digit_tokens_that_are_not_ids_of_this_answer_are_figures(text):
    ctx = make_ctx(stored=STORED + [record("p12", "tiktok", "@plate.p12", "Rate my Sunday plate #7colours")])
    draft = make_draft(ctx)
    draft["claims"][2]["text"] = text
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c3", "K2")["verdict"] == "cut"


def test_recheck_fields_keeps_the_writer_gap_for_a_support_cut_claim():
    answer, ctx = fuel_answer()
    gap = next(g for g in answer["gaps"] if g["what"].startswith("Claim c1 removed"))
    rechecked, verdicts = checks.recheck_fields(answer, ctx, window=WINDOW, code_gaps=range(len(answer["gaps"])))
    assert gap in rechecked["gaps"]
    assert [v for v in verdicts if v["claim_id"].startswith("gaps/")] == []


def test_k8_guillemet_quoted_span_probe_cuts(draft, ctx, wh):
    draft["claims"][1]["text"] = "One creator said «nobody does it like my gran, honestly»"
    answer, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c2", "K8")
    assert v["verdict"] == "cut" and "nobody does it like my gran, honestly" in v["reason"]
    assert "c2" not in claim_ids(answer)


@pytest.mark.parametrize("marks", ["«»", "„‟", "‹›", "‚‛"])
def test_k8_every_quote_mark_opens_a_checked_span(draft, ctx, wh, marks):
    draft["claims"][1]["text"] = f"Creators on TikTok called it {marks[0]}a crime against samp{marks[1]} on Sunday."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c2", "K8")["verdict"] == "cut"
    draft["claims"][1]["text"] = f"Creators on TikTok called it {marks[0]}Pap AND rice on one plate{marks[1]} on Sunday."
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c2", "K8")["verdict"] == "pass"


@pytest.mark.parametrize("phrase", ["Young South Africans", "youth culture", "young adults", "kids", "adolescents",
                                    "older audiences", "18-24s", "younger people", "the youngest fans", "youths",
                                    "youngsters", "children", "the elderly", "seniors", "pensioners", "under 25",
                                    "over 50", "under-18s", "over 60s", "an audience that skews young"])
def test_k6_age_words_are_a_breach(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"Plate rating looks like a ritual for {phrase}."
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K6")
    assert v["verdict"] == "cut" and v["reason"] == "breach: age or generation term"


@pytest.mark.parametrize("phrase", ["the Youth Day march", "a Youth Month lunch", "a June 16 to 24 programme",
                                    "a young brand", "older posts", "posts older than a week", "over 50 posts",
                                    "under 10 minutes", "over 40% of plates"])
def test_k6_proper_names_and_non_people_uses_are_not_a_breach(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"Plate rating looks like {phrase}."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K6")["verdict"] == "pass"


@pytest.mark.parametrize("phrase", ["google.trends", "trends.google.com", "Google‐Trends", "google trend"])
def test_k6_more_google_trends_spellings_are_a_breach(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"The {phrase} data also rose, so it looks like a ritual."
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K6")
    assert v["verdict"] == "cut" and v["reason"] == "breach: Google Trends or search volume"


# Task 1.11 round 5: the checks accept exactly what WRITER_SYSTEM tells the writer to write

WRITER_ROWS = [{"day": "2026-09-27", "posts": 412, "ratio": 2.8, "total": 1250},
               {"day": "2026-09-26", "posts": 131, "ratio": 0.9, "total": 1250}]


def writer_rules_draft():
    """A draft that follows WRITER_SYSTEM literally for an 8-day window: "last 8 days", day ranges in window_text's
    format, "in 2026" and "the 2026 <event>", 4-digit counts with a comma, every number pinned, and quotation marks
    only around verbatim quotes."""
    ctx = make_ctx(rows=WRITER_ROWS)
    days = (WINDOW[1] - WINDOW[0]).days + 1
    assert days == 8 and f"last {days} days" in writer.WRITER_SYSTEM.format(days=days)
    draft = make_draft(ctx)
    draft["short_answer"] = ("Over the last 8 days, 1,250 posts rated Sunday lunch plates on TikTok and X, "
                             "peaking on 26 and 27 September.")
    draft["context"] = "Seven colours is a Sunday lunch tradition in South Africa, and in 2026 it is also a rating game."
    draft["claims"] = [
        {"id": "c1", "text": "Over the last 8 days, 1,250 posts on TikTok and X rated Sunday lunch plates.",
         "label": "corroborated", "kind": "observation", "evidence_ids": ["tt_1", "tt_3", "x_1"],
         "quotes": [{"evidence_id": "x_1", "text": "Rate my 7 colours honestly"}],
         "numbers": [number(ctx, 1250, "posts in the window")]},
        {"id": "c2", "text": "Between 26 and 27 September, creators on TikTok called “Pap AND rice on one plate” a crime.",
         "label": "observed", "kind": "observation", "evidence_ids": ["tt_1", "tt_2"],
         "quotes": [{"evidence_id": "tt_2", "text": "Pap AND rice on one plate is a crime"}]},
        {"id": "c3", "text": "Plate rating in 2026 looks like a weekly ritual rather than a one-off.",
         "label": "inferred", "kind": "interpretation", "evidence_ids": ["tt_1", "x_1"]},
        {"id": "c4", "text": "On 27 September, plate posts ran at 2.8x the usual daily count, the peak of the 8-day window.",
         "label": "corroborated", "kind": "observation", "evidence_ids": ["tt_1", "x_1"],
         "numbers": [number(ctx, 2.8, "times the usual daily count")]},
        {"id": "c5", "text": "From 21 to 27 September, the 2026 rating wave peaked with 412 posts on 26-27 September 2026.",
         "label": "corroborated", "kind": "observation", "evidence_ids": ["tt_3", "x_1"],
         "numbers": [number(ctx, 412, "posts on the peak day")]},
    ]
    draft["so_what"] = [{"text": "Brands can join the rating ritual on the Sunday after 26-27 September.",
                         "claim_ids": ["c2", "c5"]},
                        {"text": "Treat the rating habit in 2026 as a weekly slot over the past 8 days and beyond.",
                         "claim_ids": ["c3"]}]
    draft["watch_next"] = [{"text": "Watch whether the 1,250 posts of the last 8 days hold up after 21 to 27 September.",
                            "claim_ids": ["c1", "c5"], "forecast": False}]
    draft["gaps"] = [{"what": "No Reddit posts in the last 8 days", "searched": "reddit/search, 21 to 28 September",
                      "why": "zero results during 2026 on 26 and 27 September"}]
    return draft, ctx


def test_writer_rules_draft_comes_back_complete_through_check_answer_and_recheck_fields():
    draft, ctx = writer_rules_draft()
    answer, verdicts = run(draft, ctx, FakeWarehouse(WRITER_ROWS))
    assert [v for v in verdicts if v["verdict"] != "pass"] == []
    assert answer["status"] == "complete"
    assert claim_ids(answer) == ["c1", "c2", "c3", "c4", "c5"]
    rechecked, reverdicts = checks.recheck_fields(answer, ctx, window=WINDOW)
    assert reverdicts == []
    assert rechecked == answer
    assert rechecked["status"] == "complete" and len(rechecked["claims"]) == 5
    for field in ("short_answer", "context"):
        assert rechecked[field] == draft[field]
    assert rechecked["so_what"] == draft["so_what"] and rechecked["watch_next"] == draft["watch_next"]
    assert rechecked["gaps"] == draft["gaps"]


def test_writer_rules_draft_still_cuts_an_unpinned_7_outside_the_window_phrase():
    draft, ctx = writer_rules_draft()
    draft["claims"][2]["text"] = "Plate rating in 2026 looks like a weekly ritual that ran for 7 Sundays."
    answer, verdicts = run(draft, ctx, FakeWarehouse(WRITER_ROWS))
    v = verdict(verdicts, "c3", "K2")
    assert v["verdict"] == "cut" and "numeral 7 " in v["reason"]
    assert claim_ids(answer) == ["c1", "c2", "c4", "c5"]
    assert answer["status"] == "partial"


ALLOWED_PHRASES = ["in the last 8 days", "over the past 8 days", "an 8-day run", "from 21 to 27 September",
                   "on 26 and 27 September", "on 26-27 September", "on 26-27 September 2026",
                   "from 21 to 27 September, 2026", "in 2026", "during 2025", "since 2019", "the 2026 Durban July",
                   # Round 6: a year up to two years ahead names an event (WHY-03, CMP-02 and NOW-02 phrasings).
                   "around the AFCON 2027 qualifiers", "during the 2027 election campaign", "since the 2027 campaign",
                   "at AFCON 2027", "at the BBNaija 2026 finale", "around the 2028 elections",
                   "as The 2027 election nears"]
NOT_ALLOWED = ["in the last 7 days", "over the past 30 days", "a 7-day run", "from 14 to 20 September",
               "on 26 and 27 October", "on 29-30 September", "on 26-27 September 2025", "in 2027",
               "the 2000 posts", "in 2026 posts", "8 days",
               "since 2027", "the 2029 election", "at AFCON 2029", "AFCON 2027 posts", "the 2027 posts",
               "In 2027", "about 2027", "the 2027 and beyond"]
FIELDS = ["claims", "short_answer", "context", "so_what", "watch_next", "gaps"]


def _field_draft(field, phrase, ctx):
    draft = make_draft(ctx)
    text = f"Plate rating {phrase} looks like a weekly ritual."
    if field == "claims":
        draft["claims"][2]["text"] = text
    elif field in ("short_answer", "context"):
        draft[field] = text
    elif field == "gaps":
        draft["gaps"] = [{"what": text, "searched": "stored posts", "why": "no split"}]
    else:
        draft[field][0]["text"] = text
    return draft


def _field_verdicts(verdicts, field):
    where = {"claims": "c3", "short_answer": "short_answer", "context": "context", "gaps": "gaps/0",
             "so_what": "so_what/0", "watch_next": "watch_next/0"}[field]
    return [v for v in verdicts if v["claim_id"] == where and v["verdict"] != "pass"]


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("phrase", ALLOWED_PHRASES)
def test_window_phrase_day_ranges_and_years_are_allowed_in_every_field(field, phrase):
    ctx = make_ctx()
    answer, verdicts = run(_field_draft(field, phrase, ctx), ctx, FakeWarehouse(ROWS))
    assert _field_verdicts(verdicts, field) == []
    rechecked, reverdicts = checks.recheck_fields(answer, ctx, window=WINDOW)
    assert reverdicts == [] and rechecked == answer


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("phrase", NOT_ALLOWED)
def test_other_lengths_ranges_outside_the_window_and_later_years_are_figures_in_every_field(field, phrase):
    ctx = make_ctx()
    _, verdicts = run(_field_draft(field, phrase, ctx), ctx, FakeWarehouse(ROWS))
    assert [v["rule"] for v in _field_verdicts(verdicts, field) if v["rule"] != "K10"] == ["K2"]


@pytest.mark.parametrize("phrase", ["over 2 million views", "under 5 million", "over 20 plates", "over 50 reposts",
                                    "over 10 tweets", "over 3 weekends", "over 2m views", "under 3 thousand",
                                    "over 40% of plates", "over 1,000 posts", "over 10 years of Sundays",
                                    "10-15s clips", "15-30s videos", "fans over 2 million",
                                    "viewers over 3 weeks"])
def test_k6_under_and_over_before_a_scale_or_count_noun_are_not_a_breach(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"Plate rating reached {phrase} and looks like a ritual."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K6")["verdict"] == "pass"


NEW_AGE_FORMS = ["over 50s", "under 25", "over 18 year olds", "under 18-year-olds", "over 18 years of age",
                 "users under 30", "users over 30 in Soweto", "fans under 25 in Lagos", "over 18+", "under 30 fans",
                 "teen", "teens", "teenage", "teenagers", "tweens", "preteens", "pre-teens", "the 25-34s",
                 "the 35\u201344s", "18+ audiences", "people aged 18+", "early teens", "mid twenties", "late thirties",
                 "early forties", "late 20s", "mid-30s", "early 40s", "30-somethings", "20 somethings",
                 "fifty-somethings", "under-twenties", "under-thirties", "under-forties", "youthful"]


@pytest.mark.parametrize("phrase", NEW_AGE_FORMS)
def test_k6_round_5_age_forms_are_a_breach(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"Plate rating looks like a ritual for {phrase}"
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K6")
    assert v["verdict"] == "cut" and v["reason"] == "breach: age or generation term"


@pytest.mark.parametrize("text", ["Plate posts were three times the usual count.", "Twice as many people rated plates.",
                                  "Plate posts were double the usual count.", "Plate posts tripled on Sunday.",
                                  "Half the plates had rice.", "A dozen creators rated plates.",
                                  "Dozens of creators rated plates.", "Plate posts rose threefold.",
                                  "Half a dozen creators rated plates.", "Plate posts were ten times the norm."])
def test_k2_spelled_multipliers_and_counts_are_figures(draft, ctx, wh, text):
    draft["claims"][2]["text"] = text
    answer, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "cut"
    assert "c3" not in claim_ids(answer)


@pytest.mark.parametrize("text,value", [("Plate posts were three times the usual count.", 3),
                                        ("Twice as many people rated plates.", 2),
                                        ("Plate posts were double the usual count.", 2)])
def test_k2_spelled_multiplier_passes_with_its_numbers_entry(text, value):
    ctx = make_ctx(rows=PROBE_ROWS)
    draft = make_draft(ctx)
    draft["claims"][0]["text"] += " " + text
    draft["claims"][0]["numbers"].append(number(ctx, value, "multiple of the usual count"))
    _, verdicts = run(draft, ctx, FakeWarehouse(PROBE_ROWS))
    assert verdict(verdicts, "c1", "K2")["verdict"] == "pass"
    draft["claims"][0]["numbers"].pop()
    _, verdicts = run(draft, ctx, FakeWarehouse(PROBE_ROWS))
    assert verdict(verdicts, "c1", "K2")["verdict"] == "cut"


def test_k2_spelled_multiplier_in_the_short_answer_is_blanked_unpinned(draft, ctx, wh):
    draft["short_answer"] = "Plate posts ran at three times the usual count."
    answer, verdicts = run(draft, ctx, wh)
    assert answer["short_answer"] == "" and verdict(verdicts, "short_answer", "K2")["verdict"] == "cut"


@pytest.mark.parametrize("text", ["One creator said the plate was a crime.", "Creators rated one plate after another.",
                                  "The second half of the day was quiet.", "A double-tap habit looks like a ritual.",
                                  "Plate rating is a double act on Sundays."])
def test_k2_pronoun_one_and_non_quantity_words_are_not_figures(draft, ctx, wh, text):
    draft["claims"][2]["text"] = text
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "pass"


@pytest.mark.parametrize("text", ["Posts per views/2m look like a weekly ritual.",
                                  "Posts per reddit/2k look like a weekly ritual.",
                                  "Posts per x/v2/3bn look like a weekly ritual.",
                                  "Posts per views/top2m look like a weekly ritual."])
def test_k2_route_segment_with_a_scaled_figure_is_a_figure(text):
    ctx = make_ctx()
    draft = make_draft(ctx)
    draft["claims"][2]["text"] = text
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c3", "K2")["verdict"] == "cut"


def test_gap_route_segment_with_a_scaled_figure_is_a_figure(draft, ctx, wh):
    draft["gaps"] = [{"what": "No posts past views/2m", "searched": "reddit/search", "why": "zero results"}]
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "gaps/0", "K2")["verdict"] == "cut"


# Task 1.11 round 6: event years, more age terms, a post's own words as quotation, tier names and the spent budget

@pytest.mark.parametrize("text", [
    "Kenyan posts about AFCON 2027 lean cynical.",
    "Posts tie the 2027 election campaign to Independence Day.",
    "Posts about the BBNaija 2026 finale look like a weekly ritual.",
    "Posts about the AFCON 2027 qualifiers lean cynical.",
    "Election talk since the 2027 campaign opened looks tense.",
    "The 2027 election campaign is colouring Independence posts.",
])
def test_k2_a_year_up_to_two_years_ahead_that_names_an_event_needs_no_numbers_entry(draft, ctx, wh, text):
    draft["claims"][2]["text"] = text
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "pass"


@pytest.mark.parametrize("text", [
    "in 2027 posts will double",
    "In 2027 posts will double.",
    "Posts about AFCON 2027 posts rose.",
    "Posts about the 2029 election lean cynical.",
    "Posts expect a bigger turnout in 2027.",
])
def test_k2_a_bare_later_year_or_a_year_before_a_count_noun_stays_a_figure(draft, ctx, wh, text):
    draft["claims"][2]["text"] = text
    answer, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K2")
    assert v["verdict"] == "cut" and "numeral 20" in v["reason"]
    assert "c3" not in claim_ids(answer)


ROUND_6_AGE_FORMS = ["teenaged", "old people", "old folks", "old heads", "old-timers", "old timers", "born-free",
                     "born-frees", "born frees", "the born free generation", "minors", "retirees", "a retiree",
                     "millenials", "milennials", "millennial", "nineties babies", "noughties kids", "90s babies",
                     "'90s kids", "2000s babies", "1990s kids", "80s baby", "digital natives", "a digital native",
                     "a generation that grew up online", "a generation who grew up online"]


@pytest.mark.parametrize("phrase", ROUND_6_AGE_FORMS)
def test_k6_round_6_age_forms_are_a_breach(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"Plate rating looks like a ritual for {phrase}"
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K6")
    assert v["verdict"] == "cut" and v["reason"] == "breach: age or generation term"


@pytest.mark.parametrize("phrase", ["an old plate", "old-school plates", "a minor change", "the 1990s", "90s music",
                                    "nineties house", "born in Soweto", "digital content", "a generation of creators"])
def test_k6_round_6_neighbours_of_age_terms_are_not_a_breach(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"Plate rating looks like {phrase}"
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K6")["verdict"] == "pass"


YOUTHS = "Our youths deserve better than this"
QUOTE_STORED = STORED + [
    record("x_3", "x", "@roads.watch", f"{YOUTHS}. Fix the roads before you campaign #7colours"),
    record("x_4", "x", "@samp.news", "google trends says samp is back this Sunday #7colours"),
]


def quote_setup(text, evidence_ids=("x_3", "x_1")):
    ctx = make_ctx(stored=QUOTE_STORED)
    draft = make_draft(ctx)
    draft["claims"][2]["text"] = text
    draft["claims"][2]["evidence_ids"] = list(evidence_ids)
    return draft, ctx


def test_k6_passes_age_words_inside_a_quote_verbatim_in_a_cited_record():
    draft, ctx = quote_setup(f'One post reads "{YOUTHS}"')
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c3", "K6")["verdict"] == "pass"
    assert verdict(verdicts, "c3", "K8")["verdict"] == "pass"
    assert "c3" in claim_ids(answer)


@pytest.mark.parametrize("text", [
    "Nigerian youths are angry",
    f'Young posters echo "{YOUTHS}"',
    'One post reads "Our youths are tired of this"',
    'One post reads "ur youths deserve better than thi"',
    f'One post reads "{YOUTHS}" and speaks for teens',
])
def test_k6_scans_the_claims_own_words_and_every_unverified_span(text):
    draft, ctx = quote_setup(text)
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    v = verdict(verdicts, "c3", "K6")
    assert v["verdict"] == "cut" and v["reason"] == "breach: age or generation term"
    assert "c3" not in claim_ids(answer)


def test_k6_a_verbatim_quote_does_not_carry_age_words_from_a_record_the_claim_does_not_cite():
    draft, ctx = quote_setup(f'One post reads "{YOUTHS}"', evidence_ids=("tt_1", "x_1"))
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c3", "K6")["verdict"] == "cut"


def test_k6_a_verbatim_quote_of_google_trends_is_still_a_breach():
    draft, ctx = quote_setup('One post reads "google trends says samp is back"', evidence_ids=("x_4", "x_1"))
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    v = verdict(verdicts, "c3", "K6")
    assert v["verdict"] == "cut" and v["reason"] == "breach: Google Trends or search volume"


@pytest.mark.parametrize("field", ["short_answer", "context", "so_what", "watch_next"])
def test_answer_fields_pass_age_words_quoted_verbatim_from_a_surviving_claims_record(field):
    draft, ctx = quote_setup("Plate posts look like a weekly ritual.")
    text = f'One post reads "{YOUTHS}".'
    if field in ("short_answer", "context"):
        draft[field] = text
    else:
        draft[field] = [{"text": text, "claim_ids": ["c3"], **({"forecast": False} if field == "watch_next" else {})}]
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    where = field if field in ("short_answer", "context") else f"{field}/0"
    assert [v for v in verdicts if v["claim_id"] == where] == []
    shown = answer[field] if field in ("short_answer", "context") else answer[field][0]["text"]
    assert shown == text
    rechecked, reverdicts = checks.recheck_fields(answer, ctx, window=WINDOW)
    assert reverdicts == [] and rechecked == answer


@pytest.mark.parametrize("field", ["short_answer", "context", "so_what", "watch_next"])
def test_answer_fields_scan_a_quote_no_surviving_claim_cites(field):
    draft, ctx = quote_setup("Plate posts look like a weekly ritual.", evidence_ids=("tt_1", "x_1"))
    text = f'One post reads "{YOUTHS}".'
    if field in ("short_answer", "context"):
        draft[field] = text
    else:
        draft[field] = [{"text": text, "claim_ids": ["c3"], **({"forecast": False} if field == "watch_next" else {})}]
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    where = field if field in ("short_answer", "context") else f"{field}/0"
    assert verdict(verdicts, where, "K6")["verdict"] == "cut"


def skill_example_gaps():
    skill = (Path(checks.__file__).parents[1] / "skills" / "culture-read" / "SKILL.md").read_text(encoding="utf-8")
    line = next(line for line in skill.splitlines() if line.startswith("Gaps: "))
    return [part.strip().rstrip(".") for part in line[len("Gaps: "):].split(";")]


def test_skill_method_keeps_search_queries_to_the_words_people_post():
    skill = (Path(checks.__file__).parents[1] / "skills" / "culture-read" / "SKILL.md").read_text(encoding="utf-8")
    method = skill.split("\nMethod\n", 1)[1].split("\nExample of a good finishing note", 1)[0]
    assert ("Search queries describe topics in the words people post, never by age, generation, life stage, gender, "
            "income or class.") in method


def test_skill_example_gap_wording_survives_the_gap_checks(draft, ctx, wh):
    gaps = skill_example_gaps()
    assert "Instagram was not searched at T1" in gaps
    draft["gaps"] = [{"what": g, "searched": "instagram/search, 21 to 28 September", "why": "tier T1"} for g in gaps]
    answer, verdicts = run(draft, ctx, wh)
    assert [v for v in verdicts if v["claim_id"].startswith("gaps/")] == []
    assert answer["gaps"] == draft["gaps"]
    _, reverdicts = checks.recheck_fields(answer, ctx, window=WINDOW)
    assert reverdicts == []


@pytest.mark.parametrize("tier", ["T0", "T1", "T2", "T3"])
def test_gap_tier_names_are_not_figures(draft, ctx, wh, tier):
    draft["gaps"] = [{"what": f"Instagram was not searched at {tier}", "searched": "stored posts", "why": "tier"}]
    _, verdicts = run(draft, ctx, wh)
    assert [v for v in verdicts if v["claim_id"] == "gaps/0"] == []


@pytest.mark.parametrize("what", ["Instagram was not searched at T4", "Instagram was not searched at T12"])
def test_gap_tier_like_tokens_past_t3_are_figures(draft, ctx, wh, what):
    draft["gaps"] = [{"what": what, "searched": "stored posts", "why": "tier"}]
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "gaps/0", "K2")["verdict"] == "cut"


def test_a_cap_reached_route_gap_says_the_live_search_budget_is_spent(draft, ctx, wh):
    ctx.emit("socialcrawl", route="instagram/search", status="cap_reached", credits=0.0)
    ctx.emit("socialcrawl", route="instagram/search", status="cap_reached", credits=0.0)
    answer, _ = run(draft, ctx, wh)
    assert answer["gaps"] == [{"what": "Live search budget for today is spent",
                               "searched": "instagram/search, 21 to 28 September", "why": "cap_reached"}]
    assert not any("No live" in g["what"] for g in answer["gaps"])
    _, reverdicts = checks.recheck_fields(answer, ctx, window=WINDOW)
    assert reverdicts == []


# Task 1.11 round 7: the last age terms, the tone stems, and years before plural nouns

ROUND_7_AGE_FORMS = ["middle-aged", "middle aged", "middleaged", "#GenZProtests", "#GenZRevolution", "#genz254",
                     "GenZers", "#GenAlphaTok", "GenZProtests", "schoolchildren", "schoolkids", "toddlers", "infants",
                     "babies", "vijana", "wazee", "pikin", "the next generation", "a generation", "this generation",
                     "the new generation", "the next-generation crowd"]


@pytest.mark.parametrize("phrase", ROUND_7_AGE_FORMS)
def test_k6_round_7_age_forms_are_a_breach(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"Plate rating looks like a ritual for {phrase}"
    _, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K6")
    assert v["verdict"] == "cut" and v["reason"] == "breach: age or generation term"


@pytest.mark.parametrize("phrase", ["a generation of content", "the generation of power", "a new generation of plates",
                                    "Gen Za", "Genzebe", "fur babies", "sugar babies", "a middle ground",
                                    "the Gen Alphabet book", "a pikinini", "a ritual from a generation ago"])
def test_k6_round_7_neighbours_of_age_terms_are_not_a_breach(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"Plate rating looks like {phrase}"
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K6")["verdict"] == "pass"


ROUND_7_TONE_WORDS = ["pride", "prides", "prided", "priding", "proud", "joke", "jokes", "joked", "joking", "humour",
                      "humoured", "humor", "humors", "banter", "bantering", "negative", "positive", "optimistic",
                      "pessimistic", "fed up", "disappointed", "happy", "hostile", "mock", "mocks", "mocked", "mocking",
                      "mockery", "praise", "praises", "praised", "praising", "celebrate", "celebrates", "celebrated",
                      "celebrating", "complain", "complains", "complained", "complaining", "grief", "grieve", "grieves",
                      "grieved", "grieving", "laugh", "laughs", "laughed", "laughing", "outrage", "outraged",
                      "anger", "angered", "angering"]


@pytest.mark.parametrize("word", ROUND_7_TONE_WORDS)
def test_round_7_tone_stems_and_their_forms_make_a_tone_claim(word):
    assert checks.is_tone_claim(f"Nigerian posters {word} about the country this week.") is True


@pytest.mark.parametrize("text", ["Nigerian posters shared plates.", "The report was prepared on Sunday.",
                                  "Posters mocha-coloured plates."])
def test_round_7_tone_rule_leaves_other_words_alone(text):
    assert checks.is_tone_claim(text) is False


def test_k5_now_02_tone_claim_on_a_pidgin_post_is_capped():
    draft, ctx = tone_setup("Nigerians online mixed pride, jokes and frustration about the country.",
                            ["ng_pidgin", "ng_en_1"])
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "NG"])
    v = verdict(verdicts, "c2", "K5")
    assert v["verdict"] == "downgrade" and "single_source" in v["reason"]
    assert claim(answer, "c2")["label"] == "single_source"


@pytest.mark.parametrize("text", [
    "Kenya's 2027 election is colouring Independence posts.",
    "Nigeria's 2027 election is colouring Independence posts.",
    "Ruto's 2027 bid is colouring Independence posts.",
    "Posts ahead of 2027 lean cynical.",
    "Posts expect a bigger turnout by 2027.",
    "Posts in the run-up to 2027 lean cynical.",
    "Posts about the 2027 and 2028 elections lean cynical.",
    "Posts about the 2027 general elections lean cynical.",
    "Posts about the AFCON 2027 qualifiers lean cynical.",
    "Posts say the 2027 campaign looks tense.",
    "In 2026 new creators joined the plate ritual.",
])
def test_k2_round_7_event_year_forms_need_no_numbers_entry(draft, ctx, wh, text):
    draft["claims"][2]["text"] = text
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "pass"


@pytest.mark.parametrize("text", [
    "Posts reached the 2027 unique voices.",
    "Hits 2027 retweets on the plate thread.",
    "Another 1999 signatures joined the petition.",
    "Total 2014 retweets on the plate thread.",
    "Posts reached the 2014 unique voices.",
    "Kenya's 2027 retweets rose.",
    "Posts grew by 2027 retweets.",
    "Posts about the 2027 and 2029 elections lean cynical.",
    "Posts ahead of 2029 lean cynical.",
])
def test_k2_round_7_a_year_before_a_plural_noun_stays_a_figure(draft, ctx, wh, text):
    draft["claims"][2]["text"] = text
    answer, verdicts = run(draft, ctx, wh)
    v = verdict(verdicts, "c3", "K2")
    assert v["verdict"] == "cut" and ("numeral 20" in v["reason"] or "numeral 19" in v["reason"])
    assert "c3" not in claim_ids(answer)


@pytest.mark.parametrize("noun", ["retweets", "signatures", "voices", "likes", "shares", "comments", "followers",
                                  "views"])
def test_k2_round_7_count_nouns_make_a_year_a_figure(draft, ctx, wh, noun):
    draft["claims"][2]["text"] = f"Posts reached the 2026 {noun} on Sunday."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "cut"


# Round 8 (task 1.11): the tone cap reads adverbs and more tone words

ROUND_8_TONE_WORDS = ["angrily", "playfully", "mockingly", "sarcastically", "bitterly", "furiously", "jokingly",
                      "rage", "raged", "raging", "fury", "funny", "funnily", "humorous", "humorously", "cheerful",
                      "cheerfully", "upbeat", "livid", "irritated", "resentment", "disgust", "disgusted", "disgusting"]


@pytest.mark.parametrize("word", ROUND_8_TONE_WORDS)
def test_round_8_tone_adverbs_and_words_make_a_tone_claim(word):
    assert checks.is_tone_claim(f"Kenyan posters reacted {word} to the fuel price this week.") is True


@pytest.mark.parametrize("text", ["Kenyans reacted to the new fuel price.", "Posts rallied on Sunday.",
                                  "Posters shared early plates."])
def test_round_8_tone_rule_still_leaves_other_words_alone(text):
    assert checks.is_tone_claim(text) is False


def test_k5_ke_claim_that_kenyans_reacted_angrily_citing_swahili_and_sheng_posts_is_capped():
    swahili = record("ke_swahili", "tiktok", "@mama.mboga", "Serikali haitusikii kabisa, bei ya mafuta ni juu sana "
                     "#fuelprice", market="KE")
    ctx = make_ctx(stored=TONE_STORED + [swahili])
    draft = make_draft(ctx)
    draft["claims"][1].update(text="Kenyans reacted angrily to the new fuel price", evidence_ids=["ke_swahili", "ke_sheng"],
                              quotes=[], label="corroborated")
    answer, verdicts = run(draft, ctx, FakeWarehouse(ROWS), markets=["ZA", "KE"])
    v = verdict(verdicts, "c2", "K5")
    assert v["verdict"] == "downgrade" and "single_source" in v["reason"]
    assert claim(answer, "c2")["label"] == "single_source"


# Round 9 (task 1.11): "ahead to" and "look to" lead an event year

@pytest.mark.parametrize("text", ["Posts look ahead to 2027.", "Posts look to 2027 with some worry.",
                                  "Posts look ahead to the 2027 election."])
def test_k2_round_9_ahead_to_and_look_to_lead_an_event_year(draft, ctx, wh, text):
    draft["claims"][2]["text"] = text
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "pass"


@pytest.mark.parametrize("text", ["Posts look ahead to 2029.", "Posts look to 2029 with some worry.",
                                  "Posts look ahead to 2027 retweets."])
def test_k2_round_9_those_leads_keep_the_two_year_limit_and_the_count_nouns(draft, ctx, wh, text):
    draft["claims"][2]["text"] = text
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "cut"


# Round 10 (task 1.11): an id the model invented never reaches a gap. Only ids that resolve are named; the rest are
# counted.

PROBE_EVIDENCE_IDS = ["x_z2", "teen_voters_1", "Gen Z voters", "kids_2", "students_1"]
PROBE_QUERY_IDS = ["q_Gen_Z_voters", "youth_posts"]


def probe_number(ctx, qid):
    return {"value": 412, "unit": "posts", "query_id": qid, "run_id": RUN, "result_hash": ctx.queries["q_1"]["result_hash"]}


def test_round_10_a_cut_claims_gaps_name_only_ids_that_resolve_and_count_the_rest(draft, ctx, wh):
    draft["claims"][1]["evidence_ids"] += PROBE_EVIDENCE_IDS
    draft["claims"][0]["numbers"] += [probe_number(ctx, q) for q in PROBE_QUERY_IDS]
    answer, verdicts = run(draft, ctx, wh)
    assert "c1" not in claim_ids(answer) and "c2" not in claim_ids(answer)
    gaps = {g["what"]: g for g in answer["gaps"]}
    k1 = gaps[f"Claim c2 cut: {checks.RULE_GAPS['K1'][0]} (K1)"]
    assert k1["searched"] == "stored evidence tt_1, tt_2 and 5 ids that do not resolve"
    k2 = gaps[f"Claim c1 cut: {checks.RULE_GAPS['K2'][0]} (K2)"]
    assert k2["searched"] == "recorded queries q_1 and 2 ids that do not resolve"
    dumped = json.dumps(answer["gaps"])
    for invented in PROBE_EVIDENCE_IDS + PROBE_QUERY_IDS:
        assert invented not in dumped
    for gap in answer["gaps"]:
        assert checks._gap_problems(gap, answer["claims"], None, code=True) == {}


@pytest.mark.parametrize("rule, claim, searched", [
    ("K1", {"evidence_ids": ["x_z2", "Gen Z voters"]}, "stored evidence: 2 ids that do not resolve"),
    ("K1", {"evidence_ids": ["kids_2"]}, "stored evidence: 1 id that does not resolve"),
    ("K1", {"evidence_ids": ["tt_1", "tt_1", "kids_2", "kids_2"]}, "stored evidence tt_1 and 1 id that does not resolve"),
    ("K1", {"evidence_ids": [{"id": "teen"}, ["x"], None]}, "stored evidence: 3 ids that do not resolve"),
    ("K1", {"evidence_ids": []}, "stored evidence none cited"),
    ("K2", {"numbers": [{"query_id": "q_Gen_Z_voters"}, {"query_id": "q_1"}]},
     "recorded queries q_1 and 1 id that does not resolve"),
    ("K2", {"numbers": [{"query_id": "youth_posts"}]}, "recorded queries: 1 id that does not resolve"),
    ("K2", {"numbers": [{"value": 3}]}, "recorded queries none cited"),
])
def test_round_10_searched_names_resolved_ids_and_counts_the_rest(ctx, rule, claim, searched):
    assert checks._searched(rule, claim, ctx.evidence, ctx.queries) == searched
    gap = checks._code_gap("Claim c1 cut", rule, searched)
    assert checks._gap_problems(gap, [], None, code=True) == {}
    assert checks._text_breaches(searched) == []


@pytest.mark.parametrize("phrase", ["matric learners", "Matric pupils", "matriculants", "a matriculant",
                                    "first-time voters", "first time voters", "school leavers", "school-leavers"])
def test_round_10_life_stage_terms_are_age_terms(draft, ctx, wh, phrase):
    draft["claims"][2]["text"] = f"Sunday plate rating is big with {phrase}."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K6")["verdict"] == "cut"


@pytest.mark.parametrize("text", ["Matric results day filled timelines.", "First-time buyers rated their plates.",
                                  "The school holidays start next week."])
def test_round_10_matric_first_time_and_school_words_alone_stay_clean(text):
    assert checks._text_breaches(text) == []


# Review block (task 1.11): a writer gap never borrows the code's own gap wording to carry a figure

CODE_WORDED_GAPS = ["Amapiano claim 5000 cut in Soweto", "Township fares so_what item 300 removed",
                    "Soweto saw 2000 ids that do not resolve", "Spend on braai claim 40 removed at T1",
                    "What the 12 ids that do not resolve say about Durban"]


@pytest.mark.parametrize("what", CODE_WORDED_GAPS)
def test_a_writer_gap_in_the_codes_gap_wording_is_cut_by_the_gate(draft, ctx, wh, what):
    gap = {"what": what, "searched": "stored posts", "why": "no posts"}
    draft["gaps"] = [gap]
    answer, verdicts = run(draft, ctx, wh)
    assert gap not in answer["gaps"]
    assert verdict(verdicts, "gaps/0", "K2")["verdict"] == "cut"
    rechecked, again = checks.recheck_fields({**answer, "gaps": [gap] + answer["gaps"]}, ctx, window=WINDOW)
    assert gap not in rechecked["gaps"]
    assert verdict(again, "gaps/0", "K2")["verdict"] == "cut"


@pytest.mark.parametrize("gap", [
    checks._code_gap("Claim c3 cut", "K1", "stored evidence tt_1 and 3 ids that do not resolve"),
    checks._code_gap("Claim c3 cut", "K2", "recorded queries: 1 id that does not resolve"),
    checks._code_gap("so_what item 2 removed", "K2", "the so_what text"),
    checks._code_gap("watch_next item 0 removed", "K8", "the watch_next text"),
    checks._code_gap("Short answer removed", "K6", "the short answer text"),
    {"what": "Claim c3 removed: not supported by its cited posts", "searched": "cited posts tt_1, tt_2",
     "why": "support check unsupported: not supported by its cited posts"},
])
def test_the_codes_own_cut_gaps_still_pass_the_gate(gap):
    assert checks._gap_problems(gap, [], None, code=True) == {}
    answer, ctx = fuel_answer()
    answer["gaps"].insert(0, gap)
    rechecked, verdicts = checks.recheck_fields(answer, ctx, window=WINDOW, code_gaps=range(len(answer["gaps"])))
    assert gap in rechecked["gaps"]
    assert [v for v in verdicts if v["claim_id"] == "gaps/0"] == []


def test_a_claim_id_made_only_of_digits_is_not_read_as_a_code_gap():
    gap = checks._code_gap("Claim 5000 cut", "K2", "recorded queries none cited")
    assert "K2" in checks._gap_problems(gap, [], None)
    assert "K2" in checks._gap_problems(dict(gap, what=gap["what"].replace("cut", "removed")), [], None)


# Review block (task 1.11): the route and ISO date allowances carry no unpinned figure, and a gap is the code's only
# by the mark the code gave it

LEAK_TEXTS = ["Amapiano/Soweto 5000-streams", "Amapiano grew on plays/week 5000-plus", "Amapiano tiktok/a-5000",
              "tiktok/s5000", "Amapiano 1200-00-00 posts", "Amapiano 2026-99-99", "Amapiano on 5000-09-21",
              "Amapiano on 2029-01-01", "Amapiano on 1899-12-31", "No Reddit posts from reddit/search 5000-plus",
              "Amapiano on 2026-09-24 5000 posts"]


@pytest.mark.parametrize("text", LEAK_TEXTS)
@pytest.mark.parametrize("field", ["what", "searched"])
def test_a_gap_hiding_a_figure_in_a_route_or_date_shape_is_cut(draft, ctx, wh, text, field):
    gap = dict({"what": "Whether amapiano Sundays reach Durban", "searched": "stored posts", "why": "no posts"},
               **{field: text})
    draft["gaps"] = [gap]
    answer, verdicts = run(draft, ctx, wh)
    assert gap not in answer["gaps"]
    assert verdict(verdicts, "gaps/0", "K2")["verdict"] == "cut"
    rechecked, again = checks.recheck_fields({**answer, "gaps": [gap] + answer["gaps"]}, ctx, window=WINDOW)
    assert gap not in rechecked["gaps"]
    assert verdict(again, "gaps/0", "K2")["verdict"] == "cut"


@pytest.mark.parametrize("gap", [
    {"what": "No TikTok posts", "searched": "tiktok/search for amapiano returned rate_limited", "why": "rate limit"},
    {"what": "No Reddit posts on 2026-09-24", "searched": "reddit/search, 2026-09-21 to 2026-09-28", "why": "none"},
    {"what": "Posts about the 2028-06-01 draw", "searched": "reddit/search 7colours", "why": "zero results"},
    {"what": "No Reddit posts", "searched": "reddit/search/v2 at 2026-09-24T12:30:00Z", "why": "zero results"},
    {"what": "Instagram was not searched at T1", "searched": "stored posts", "why": "not needed at T1"},
])
def test_a_real_route_or_date_in_a_gap_still_passes(draft, ctx, wh, gap):
    draft["gaps"] = [gap]
    answer, verdicts = run(draft, ctx, wh)
    assert gap in answer["gaps"]
    assert [v for v in verdicts if v["claim_id"] == "gaps/0"] == []


def test_a_query_word_with_a_digit_passes_only_when_a_recorded_query_searched_it(draft, wh):
    ctx = make_ctx(live=False)
    gap = {"what": "No Reddit posts", "searched": "reddit/search 7colours", "why": "zero results"}
    ctx.queries["q_1"]["params"] = {"tag": "sundaylunch"}
    draft["gaps"] = [gap]
    answer, verdicts = run(draft, ctx, wh)
    assert gap not in answer["gaps"]
    assert verdict(verdicts, "gaps/0", "K2")["verdict"] == "cut"


def test_an_iso_date_outside_the_event_year_bound_is_a_figure_in_every_field():
    allow = checks._allowance(WINDOW, datetime.fromisoformat(AS_OF))
    assert list(checks._numerals("on 2028-12-31", allow=allow)) == []
    assert [t for t, *_ in checks._numerals("on 2029-01-01", allow=allow)] == ["2029", "01", "01"]
    assert list(checks._numerals("on 2099-12-31")) == []
    assert [t for t, *_ in checks._numerals("on 2026-02-30")] == ["2026", "02", "30"]
    assert [t for t, *_ in checks._numerals("on 2026-09-24 5000 posts")] == ["5000"]


def test_a_draft_gap_in_the_codes_exact_shape_is_read_as_the_writers(draft, ctx, wh):
    gap = {"what": "Claim c3 cut: numbers not reproduced (K2)",
           "searched": "recorded queries: 5000 ids that do not resolve",
           "why": "every number must come from a query recorded in this run"}
    draft["gaps"] = [gap]
    answer, verdicts = run(draft, ctx, wh)
    assert gap not in answer["gaps"]
    assert verdict(verdicts, "gaps/0", "K2")["verdict"] == "cut"
    assert "5000" in verdict(verdicts, "gaps/0", "K2")["reason"]


FORGED_CODE_GAPS = [
    {"what": "Claim c5000 cut: a number with no query behind it (K2)", "searched": "recorded queries q_1",
     "why": checks.RULE_GAPS["K2"][1]},
    {"what": "so_what item 5000 removed: a number with no query behind it (K2)", "searched": "the so_what text",
     "why": checks.RULE_GAPS["K2"][1]},
    {"what": "Claim c1 removed: not supported by its cited posts", "searched": "cited posts a5000, soweto-5000",
     "why": "support check unsupported: not supported by its cited posts"},
]


@pytest.mark.parametrize("gap", FORGED_CODE_GAPS)
def test_a_code_shaped_gap_the_code_did_not_mark_is_the_writers_in_the_recheck(gap):
    answer, ctx = fuel_answer()
    marked = set(range(len(answer["gaps"])))
    answer["gaps"].append(gap)
    rechecked, verdicts = checks.recheck_fields(answer, ctx, window=WINDOW, code_gaps=marked)
    assert gap not in rechecked["gaps"]
    assert verdict(verdicts, f"gaps/{len(marked)}", "K2")["verdict"] == "cut"
    assert rechecked["gaps"][:len(marked)] == answer["gaps"][:len(marked)]


def test_the_code_gaps_are_every_gap_past_the_drafts_surviving_gaps(draft, ctx, wh):
    kept = {"what": "No Reddit posts", "searched": "reddit/search 7colours", "why": "zero results"}
    draft["gaps"] = [FORGED_CODE_GAPS[0], kept]
    draft["claims"][2]["text"] += " Another 37 posts followed."
    ctx.emit("socialcrawl", route="threads/search", status="rate_limited", credits=0.0)
    answer, _ = run(draft, ctx, wh)
    assert answer["gaps"][0] == kept and len(answer["gaps"]) == 3
    assert checks.code_gap_indexes(draft["gaps"], answer["gaps"]) == {1, 2}
    rechecked, verdicts = checks.recheck_fields(answer, ctx, window=WINDOW,
                                                code_gaps=checks.code_gap_indexes(draft["gaps"], answer["gaps"]))
    assert rechecked == answer and verdicts == []


# Review block (tasks 1.11 and 1.17): in a writer gap every digit-bearing allowance is pinned to something this run
# holds, or parses as a real date or time. make_ctx holds evidence tt_1 to gem_1, query q_1 with tag 7colours, and a
# SocialCrawl call on reddit/search for 7colours.

PINNED_LEAKS = [
    "Durban views x_5000", "Soweto streams_5k", "Amapiano s_5000", "Amapiano q_5000", "Amapiano tt_5000",
    "Durban posts @5000", "Durban posts from @dj5000", "Amapiano #5000plus", "Amapiano under #amapiano5000",
    "Amapiano on 99/99/2026", "Amapiano on 31/12/5000", "Amapiano on 30/02/2026", "Amapiano on 1/13/2026",
    "Amapiano 99:99 views", "Amapiano 24:00 views", "Amapiano 12:60 views", "Amapiano at 12:30:99",
    "Amapiano at 2026-09-24T99:99:99Z", "Amapiano at 2026-09-24T12:30:00+99:99", "Amapiano at 2026-09-24 25:00",
    "Amapiano on 99 September", "Amapiano on 31 February", "Amapiano in September 5000",
    "Amapiano at https://example.test/views/5000", "sql_query 7colours", "reddit/search 7colours5000",
]


@pytest.mark.parametrize("text", PINNED_LEAKS)
@pytest.mark.parametrize("field", ["what", "searched"])
def test_a_writer_gap_figure_behind_an_unpinned_allowance_is_cut(draft, ctx, wh, text, field):
    gap = dict({"what": "Whether amapiano Sundays reach Durban", "searched": "stored posts", "why": "no posts"},
               **{field: text})
    draft["gaps"] = [gap]
    answer, verdicts = run(draft, ctx, wh)
    assert gap not in answer["gaps"]
    assert verdict(verdicts, "gaps/0", "K2")["verdict"] == "cut"
    rechecked, again = checks.recheck_fields({**answer, "gaps": [gap] + answer["gaps"]}, ctx, window=WINDOW)
    assert gap not in rechecked["gaps"]
    assert verdict(again, "gaps/0", "K2")["verdict"] == "cut"


PINNED_PASSES = [
    "reddit/search 7colours", "reddit/search/v2 7colours", "Amapiano on 21/09/2026", "Amapiano at 12:30 and 23:59:59",
    "Amapiano at 2026-09-24T12:30:00Z", "Amapiano at 2026-09-24T12:30:00+02:00", "Amapiano on 29 February",
    "Amapiano on 27 September and September 26th, 2026", "Amapiano in tt_1 and q_1", "Amapiano from @kay.plates",
    "Amapiano under #7colours", "Amapiano at https://example.test/tiktok/kay.plates/tt_3",
    "Instagram was not searched at T1",
]


@pytest.mark.parametrize("text", PINNED_PASSES)
def test_a_writer_gap_with_a_pinned_route_word_real_date_resolving_id_known_handle_or_tag_passes(draft, ctx, wh, text):
    gap = {"what": text, "searched": "stored posts", "why": "no posts"}
    draft["gaps"] = [gap]
    answer, verdicts = run(draft, ctx, wh)
    assert gap in answer["gaps"]
    assert [v for v in verdicts if v["claim_id"] == "gaps/0"] == []
    assert checks._gap_problems(gap, [], checks._allowance(WINDOW, ctx.as_of), ctx=ctx) == {}


@pytest.mark.parametrize("calls, kept", [
    ([], False),
    ([("x/search", {"query": "7colours"})], False),
    ([("reddit/search", {"query": "7colour"})], False),
    ([("reddit/search", {"query": "7colours"})], True),
    ([("x/search", {"query": "braai"}), ("reddit/search", {"subreddit": "7colours"})], True),
])
def test_a_route_query_word_is_pinned_only_by_a_socialcrawl_call_on_that_route(draft, wh, calls, kept):
    ctx = make_ctx(live=False)  # q_1, an sql_query, still has tag 7colours: that pins nothing
    for route, params in calls:
        ctx.sc_calls.append({"route": route, "params": params, "status": "ok"})
    gap = {"what": "No Reddit posts", "searched": "reddit/search 7colours", "why": "zero results"}
    draft["gaps"] = [gap]
    answer, verdicts = run(draft, ctx, wh)
    assert (gap in answer["gaps"]) is kept
    assert [v["verdict"] for v in verdicts if v["claim_id"] == "gaps/0"] == ([] if kept else ["cut"])


def test_an_sql_param_never_pins_a_route_word(draft, wh):
    ctx = make_ctx(live=False)
    ctx.record_query(SQL, {"tag": "5k"}, copy.deepcopy(ROWS), "probe")
    gap = {"what": "No Reddit posts", "searched": "reddit/search 5k", "why": "zero results"}
    draft["gaps"] = [gap]
    answer, verdicts = run(draft, ctx, wh)
    assert gap not in answer["gaps"]
    assert verdict(verdicts, "gaps/0", "K2")["verdict"] == "cut"


def test_a_digit_hashtag_in_socialcrawl_params_passes_and_one_only_in_sql_params_or_nowhere_is_cut(draft, wh):
    ctx = make_ctx()
    ctx.record_query(SQL, {"tag": "amapiano2027"}, copy.deepcopy(ROWS), "probe")  # an sql_query param pins nothing
    ctx.sc_calls.append({"route": "tiktok/search/hashtag", "params": {"hashtag": "amapiano2026"}, "status": "ok"})
    kept = {"what": "Posts under #amapiano2026 not split by city", "searched": "stored posts", "why": "no city"}
    sql_only = {"what": "Posts under #amapiano2027 not split by city", "searched": "stored posts", "why": "no city"}
    nowhere = {"what": "Posts under #amapiano2028 not split by city", "searched": "stored posts", "why": "no city"}
    draft["gaps"] = [kept, sql_only, nowhere]
    answer, verdicts = run(draft, ctx, wh)
    assert kept in answer["gaps"] and sql_only not in answer["gaps"] and nowhere not in answer["gaps"]
    assert verdict(verdicts, "gaps/1", "K2")["verdict"] == "cut"
    assert verdict(verdicts, "gaps/2", "K2")["verdict"] == "cut"


def test_claims_cut_an_id_handle_and_hashtag_that_do_not_resolve(draft, ctx, wh):
    draft["claims"][2]["text"] = "Posts by @dj5000 in tt_9 look like a weekly ritual."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "cut"
    draft["claims"][2]["text"] = "Posts under #amapiano2026 look like a weekly ritual."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "cut"


def test_claims_pass_an_id_handle_and_hashtag_that_resolve(draft, wh):
    ctx = make_ctx(stored=STORED + [record("tt_9", "tiktok", "@dj5000", "Sunday set #amapiano2026")])
    draft["claims"][2]["text"] = "Posts under #amapiano2026 by @dj5000 in tt_9 look like a weekly ritual."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "pass"


# Review block (1.11): the broad id, handle, hashtag and link forms hid figures in every field. Each field now resolves
# them against this run as a writer gap does, and spelled cardinals are figures.

BROAD_PROBES = ["reached @5000 views", "x_5000 views", "views_5000 this week", "R_5000 in sales", "#300percent",
                "#top10 in ZA", "https://stats.example/5000-posts", "@5k"]
SPELLED_CARDINALS = ["three hundred thousand", "a million views", "twelve thousand posts", "half a million views",
                     "twenty-five thousand plays", "a thousand thanks"]
ANSWER_FIELDS = ["short_answer", "claim", "context", "so_what", "watch_next"]


def put(draft, field, text):
    """Write text into one field of the draft and return where its verdict rows are keyed."""
    if field == "claim":
        draft["claims"][2]["text"] = text
        return "c3"
    if field in ("so_what", "watch_next"):
        draft[field][0]["text"] = text
        return f"{field}/0"
    draft[field] = text
    return field


@pytest.mark.parametrize("field", ANSWER_FIELDS)
@pytest.mark.parametrize("probe", BROAD_PROBES + SPELLED_CARDINALS)
def test_a_figure_behind_a_broad_form_or_a_spelled_cardinal_is_cut_in_every_field(draft, ctx, wh, field, probe):
    where = put(draft, field, f"Sunday plate ratings: {probe}.")
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, where, "K2")["verdict"] == "cut"


LEGIT = ["Sunday plate ratings in tt_1 and q_1.", "Sunday plate ratings from @dj5000.",
         "Sunday plate ratings at https://example.test/tiktok/kay.plates/tt_3.",
         "Sunday plate ratings under #amapiano2026.", "One creator rated Sunday plates.",
         "One of the creators rated Sunday plates.", "Sunday plate ratings under #7colours.",
         "Sunday plate ratings reached twelve thousand posts."]


@pytest.mark.parametrize("field", ANSWER_FIELDS)
@pytest.mark.parametrize("text", LEGIT)
def test_resolved_ids_handles_links_and_hashtags_small_number_words_and_a_pinned_cardinal_pass(wh, field, text):
    rows = ROWS + [{"day": "2026-09-25", "posts": 12000, "ratio": 1.1}]
    ctx = make_ctx(rows=rows, stored=STORED + [record("tt_9", "tiktok", "@dj5000", "Sunday set #amapiano2026")])
    draft = make_draft(ctx)
    draft["claims"][0]["numbers"].append(number(ctx, 12000, "posts on 25 September"))
    draft["claims"][2]["numbers"] = [number(ctx, 12000, "posts on 25 September")]
    where = put(draft, field, text)
    _, verdicts = run(draft, ctx, FakeWarehouse(rows))
    assert [v for v in verdicts if v["claim_id"] == where and v["verdict"] == "cut"] == []


def test_a_digit_hashtag_pinned_only_by_an_sql_param_is_cut_in_a_claim(draft, wh):
    ctx = make_ctx()
    ctx.record_query(SQL, {"tag": "amapiano2026"}, copy.deepcopy(ROWS), "probe")
    draft["claims"][2]["text"] = "Posts under #amapiano2026 look like a weekly ritual."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "cut"
    ctx.sc_calls.append({"route": "tiktok/search/hashtag", "params": {"hashtag": "#amapiano2026"}, "status": "ok"})
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "pass"


@pytest.mark.parametrize("text, value, decimals", [
    ("three hundred thousand", 300000, -3), ("a million views", 1000000, -6), ("twelve thousand posts", 12000, -3),
    ("half a million", 500000, -5), ("twenty-five thousand", 25000, -3), ("two billion", 2000000000, -9),
    ("three hundred", 300, None), ("a hundred times", 100, None), ("a millionaire", None, None),
])
def test_spelled_cardinals_read_as_figures(text, value, decimals):
    found = [(v, d) for _, v, d, _ in checks._numerals(text)]
    assert found == ([] if value is None else [(value, decimals)])


@pytest.mark.parametrize("text", ["one creator", "one of the creators", "two platforms", "a creator", "an account"])
def test_a_lone_number_word_is_not_a_figure(text):
    assert list(checks._numerals(text)) == []


@pytest.mark.parametrize("text", ["On 99/99/2026 plates were rated.", "At 99:99 plates were rated.",
                                  "At 2026-09-24T99:99:99Z plates were rated.", "On 31 February plates were rated."])
def test_an_impossible_date_or_time_is_a_figure_in_a_claim_too(draft, ctx, wh, text):
    draft["claims"][2]["text"] = text
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K2")["verdict"] == "cut"


ENRICH_ROUTES = [
    ("get_comments", "tiktok/post/comments", "Comments on a TikTok post", "No comments returned for a TikTok post"),
    ("get_comments", "twitter/tweet/replies", "Replies to an X post", "No replies returned for an X post"),
    ("get_transcript", "youtube/video/transcript", "Transcript of a YouTube video",
     "No transcript returned for a YouTube video"),
    ("get_transcript", "instagram/media/transcript", "Transcript of an Instagram post",
     "No transcript returned for an Instagram post"),
]


@pytest.mark.parametrize("step, route, subject, empty", ENRICH_ROUTES)
@pytest.mark.parametrize("status", ["rate_limited", "auth_failed", "error", "schema_drift", "partial", "empty"])
def test_a_non_ok_enrichment_call_gives_its_own_source_gap(ctx, step, route, subject, empty, status):
    ctx.emit(step, route=route, evidence_id="tt_1", status="ok", credits=1.0)
    ctx.emit(step, route=route, evidence_id="tt_2", status=status, credits=0.0)
    what = (empty if status == "empty" else f"{subject} came back partial" if status == "partial"
            else f"{subject} not fetched")
    assert checks.source_gaps(ctx, WINDOW) == [{"what": what, "searched": route, "why": status}]
    assert "No live" not in what


def test_enrichment_and_search_failures_each_get_a_gap_that_passes_the_gate(draft, ctx, wh):
    ctx.emit("socialcrawl", route="threads/search", status="rate_limited", credits=0.0)
    ctx.emit("get_comments", route="tiktok/post/comments", evidence_id="tt_1", status="auth_failed", credits=0.0)
    ctx.emit("get_comments", route="tiktok/post/comments", evidence_id="tt_2", status="error", credits=0.0)
    ctx.emit("get_transcript", route="tiktok/post/transcript", evidence_id="tt_1", status="cap_reached", credits=0.0)
    answer, _ = run(draft, ctx, wh)
    assert answer["gaps"] == [
        {"what": "No live Threads data", "searched": "threads/search, 21 to 28 September", "why": "rate_limited"},
        {"what": "Comments on a TikTok post not fetched", "searched": "tiktok/post/comments", "why": "auth_failed, error"},
        {"what": "Live search budget for today is spent", "searched": "tiktok/post/transcript", "why": "cap_reached"},
    ]
    rechecked, verdicts = checks.recheck_fields(answer, ctx, window=WINDOW, code_gaps=range(len(answer["gaps"])))
    assert rechecked == answer and verdicts == []


class RedditClient:
    """A SocialCrawl client whose every call answers ok with no items."""

    def quote(self, route, params):
        return 1.0

    def call(self, route, params, *, lane, run_id, max_credits):
        return {"items": [], "next_cursor": None, "credits_charged": 1.0, "status": "ok", "cache_hit": False}


@pytest.mark.parametrize("query, kept", [("7colours", True), ("7colour", False)])
def test_a_live_route_word_is_pinned_by_a_real_socialcrawl_call_and_no_param_reaches_the_events(draft, wh, query, kept):
    from core.agent.tools.socialcrawl import socialcrawl_call

    ctx = make_ctx(live=False)
    socialcrawl_call(ctx, RedditClient(), "reddit", "search", {"query": query}, max_credits=5)
    gap = {"what": "No Reddit posts", "searched": "reddit/search 7colours", "why": "zero results"}
    draft["gaps"] = [gap]
    answer, verdicts = run(draft, ctx, wh)
    assert (gap in answer["gaps"]) is kept
    assert [v["verdict"] for v in verdicts if v["claim_id"] == "gaps/0"] == ([] if kept else ["cut"])
    assert all("params" not in e for e in ctx.events) and query not in json.dumps(ctx.events)


def test_route_words_in_events_pin_nothing(draft, wh):
    ctx = make_ctx(live=False)
    ctx.emit("socialcrawl", route="reddit/search", status="ok", credits=1.0, params={"query": "7colours"})
    gap = {"what": "No Reddit posts", "searched": "reddit/search 7colours", "why": "zero results"}
    draft["gaps"] = [gap]
    answer, verdicts = run(draft, ctx, wh)
    assert gap not in answer["gaps"]
    assert verdict(verdicts, "gaps/0", "K2")["verdict"] == "cut"


# Understand review (rule 1): Ask-side age terms the understand scan had and AGE_PATTERNS lacked. Each is cut in a claim
# and in a gap, a post's own words quoted verbatim stay allowed, and no_age_lens flags exactly the same phrases.

NEW_AGE_TERMS = ["ama2000", "ama2000s", "ama-2000", "Ama2000s", "#ama2000s", "ama2k", "ama1990s", "zillennial",
                 "zillennials", "juvenile", "juveniles", "iGen", "igen", "amakhehla", "kid", "kids", "kiddies",
                 "kiddos", "kidz", "Kid Cudi", "gogo", "gogos", "granny", "grannies", "grandma", "grandmothers",
                 "grandfather", "grandparents", "mkhulu", "mkhulus", "ugogo", "ogogo", "kogogo",
                 "makhulu", "umakhulu", "koko", "bibi", "babu", "watoto", "abantwana"]
NEW_AGE_CLEAN = ["kidney beans", "a kidnap story", "just kidding", "iGenius", "amapiano", "amakhosi", "the 2000s",
                 "Durban 2000", "juvenal"]


@pytest.mark.parametrize("term", NEW_AGE_TERMS)
def test_new_age_terms_are_cut_in_a_claim(draft, ctx, wh, term):
    draft["claims"][2]["text"] = f"Sunday plate rating is big with {term} in Durban."
    answer, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "c3", "K6")["verdict"] == "cut"
    assert term.lower() not in json.dumps(answer).lower()


@pytest.mark.parametrize("term", NEW_AGE_TERMS)
def test_new_age_terms_are_cut_in_a_gap(draft, ctx, wh, term):
    gap = {"what": f"How {term} in Durban rate Sunday plates", "searched": "stored posts", "why": "no posts"}
    draft["gaps"] = [gap]
    answer, verdicts = run(draft, ctx, wh)
    assert gap not in answer["gaps"]
    assert verdict(verdicts, "gaps/0", "K6")["reason"] == "breach: age or generation term"


@pytest.mark.parametrize("text", NEW_AGE_CLEAN)
def test_words_near_the_new_age_terms_stay_clean(text):
    assert checks._text_breaches(f"Posts about {text} filled timelines.") == []


def test_a_new_age_term_quoted_verbatim_from_a_cited_post_is_allowed():
    words = "the kiddos and ama2000s love this plate"
    ctx = make_ctx(stored=STORED + [record("tt_9", "tiktok", "@plate.fan", f"Honestly {words}, rate it #7colours")])
    draft = make_draft(ctx)
    draft["claims"][1]["evidence_ids"] = ["tt_2", "tt_9"]
    draft["claims"][1]["quotes"] = [{"evidence_id": "tt_9", "text": words}]
    draft["claims"][1]["text"] = f'One creator on TikTok wrote "{words}".'
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c2", "K6")["verdict"] == "pass"
    draft["claims"][1]["text"] = f"One creator on TikTok says {words}."
    _, verdicts = run(draft, ctx, FakeWarehouse(ROWS))
    assert verdict(verdicts, "c2", "K6")["verdict"] == "cut"


AGE_RUNNER = """
import { readFileSync } from 'node:fs';
const input = JSON.parse(readFileSync(0, 'utf8'));
const check = new Function('output', 'context', input.body);
const flagged = input.phrases.map((p) => check(JSON.stringify({ short_answer: p }), {}).pass === false);
process.stdout.write(JSON.stringify(flagged));
"""


def test_no_age_lens_flags_the_new_age_terms_exactly_as_the_checks_do():
    import subprocess

    import yaml

    config = yaml.safe_load((Path(__file__).resolve().parents[2] / "eval" / "promptfooconfig.yaml")
                            .read_text(encoding="utf-8"))
    body = next(a["value"] for a in config["defaultTest"]["assert"] if a.get("metric") == "no_age_lens")
    phrases = [f"Posts from {t} in Durban." for t in NEW_AGE_TERMS + NEW_AGE_CLEAN]
    done = subprocess.run(["node", "--input-type=module", "-e", AGE_RUNNER], capture_output=True, encoding="utf-8",
                          input=json.dumps({"body": body, "phrases": phrases}))
    assert done.returncode == 0, done.stderr
    expected = [True] * len(NEW_AGE_TERMS) + [False] * len(NEW_AGE_CLEAN)
    assert json.loads(done.stdout) == expected
    assert [any(p.search(phrase) for p in checks.AGE_PATTERNS) for phrase in phrases] == expected


def test_a_claim_id_off_the_writer_form_is_no_id_so_its_figure_is_cut(draft, ctx, wh):
    draft["claims"][2]["id"] = "x_5000"
    draft["short_answer"] = "Sunday plate ratings reached x_5000 views."
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, "short_answer", "K2")["verdict"] == "cut"
    assert checks._id_pattern(draft["claims"], ctx).search("x_5000") is None


# Second review block (1.11): a digit with a spaced scale word, spelled quantities with no scale, and bare clocks.

def k2_with(figure, values):
    """The K2 verdict on claim c3, which has no other numbers, with text naming figure and one entry per value."""
    rows = [dict(ROWS[0], **{f"v{i}": v for i, v in enumerate(values)}), ROWS[1]]
    ctx = make_ctx(rows=rows)
    draft = make_draft(ctx)
    draft["claims"][2]["text"] = f"One Sunday plate figure was {figure}."
    draft["claims"][2]["numbers"] = [number(ctx, v, "plate figure") for v in values]
    _, verdicts = run(draft, ctx, FakeWarehouse(rows))
    return verdict(verdicts, "c3", "K2")["verdict"]


@pytest.mark.parametrize("figure, values, expected", [
    ("412 thousand", [412], "cut"), ("412 thousand", [412000], "pass"),
    ("412 hundred", [412], "cut"), ("412 hundred", [41200], "pass"),
    ("412 dozen", [412], "cut"), ("412 dozen", [4944], "pass"), ("412dozen", [4944], "pass"),
    ("412 k", [412], "cut"), ("412 k", [412000], "pass"),
    ("412 m", [412], "cut"), ("412 m", [412000000], "pass"),
    ("4 b", [4], "cut"), ("4 b", [4000000000], "pass"),
    ("412 lakh", [412], "cut"), ("412 lakh", [41200000], "cut"), ("412lakh", [412], "cut"),
    ("412 crore", [412], "cut"), ("412 grand", [412], "cut"), ("412 grand", [412000], "cut"),
    ("412 thou", [412], "cut"), ("412 thou", [412000], "cut"),
    ("412 thousand million", [412], "cut"), ("412 thousand million", [412000], "cut"),
    ("412 thousand million", [412000000000], "cut"), ("412k million", [412000], "cut"),
])
def test_a_digit_with_a_scale_word_is_read_at_its_scale_or_cut(figure, values, expected):
    assert k2_with(figure, values) == expected


@pytest.mark.parametrize("figure, values, expected", [
    ("thousands of views", [5000], "pass"), ("thousands of views", [500], "cut"),
    ("hundreds of posts", [300], "pass"), ("hundreds of posts", [30], "cut"), ("hundreds of posts", [3000], "cut"),
    ("millions of views", [3000000], "pass"), ("millions of views", [3000], "cut"),
    ("billions of views", [2000000000], "pass"), ("billions of views", [2000000], "cut"),
    ("forty creators", [40], "pass"), ("forty creators", [4], "cut"),
    ("twenty-five creators", [25], "pass"), ("twenty five creators", [25], "pass"), ("ninety posts", [9], "cut"),
    ("fifty percent of creators", [0.5], "pass"), ("fifty percent of creators", [50], "pass"),
    ("fifty per cent of creators", [0.5], "pass"), ("fifty percent of creators", [5], "cut"),
    ("a hundred percent", [1], "pass"),
    ("one in five creators", [0.2], "pass"), ("one in five creators", [5], "cut"),
    ("one in three creators", [0.333], "pass"),
    ("a quarter of posts", [0.25], "pass"), ("a quarter of posts", [4], "cut"),
    ("a third of posts", [0.333], "pass"), ("two thirds of posts", [0.667], "pass"), ("a fifth of posts", [0.2], "pass"),
    ("three-quarters of posts", [0.75], "pass"), ("three-quarters of posts", [3], "cut"),
    ("half of the posts", [0.5], "pass"), ("half of the posts", [2], "cut"),
])
def test_spelled_quantities_with_no_scale_are_figures(figure, values, expected):
    assert k2_with(figure, values) == expected


SPELLED_QUANTITIES = ["412 thousand posts", "thousands of views", "forty creators posted", "fifty percent of creators",
                      "one in five creators", "a quarter of posts", "a 3:10 ratio"]


@pytest.mark.parametrize("field", ANSWER_FIELDS)
@pytest.mark.parametrize("probe", SPELLED_QUANTITIES)
def test_a_spelled_quantity_scaled_digit_or_bare_clock_is_cut_in_every_field(draft, ctx, wh, field, probe):
    where = put(draft, field, f"Sunday plate ratings: {probe}.")
    _, verdicts = run(draft, ctx, wh)
    assert verdict(verdicts, where, "K2")["verdict"] == "cut"


@pytest.mark.parametrize("field", ANSWER_FIELDS)
@pytest.mark.parametrize("text", ["Two platforms carried Sunday plate ratings.", "One creator rated Sunday plates.",
                                  "One of the creators rated Sunday plates.", "The third of the plates was best.",
                                  "The first half of the day was quiet.", "Sunday plate ratings peaked at 12:30.",
                                  "Sunday plate ratings peaked by 12:30 pm.", "Sunday plate ratings ran 10:00 SAST.",
                                  "Sunday plate ratings ran from 10:00 to 12:30.",
                                  "Sunday plate ratings ran until 9:45am."])
def test_small_number_words_and_a_clock_in_its_place_still_pass(draft, ctx, wh, field, text):
    where = put(draft, field, text)
    _, verdicts = run(draft, ctx, wh)
    assert [v for v in verdicts if v["claim_id"] == where and v["verdict"] == "cut"] == []


@pytest.mark.parametrize("text", ["a 3:10 ratio", "3:10 of creators", "the 2:30 mark and 4:15"])
def test_a_clock_with_no_time_word_is_a_figure(text):
    assert list(checks._numerals(text)) != []


@pytest.mark.parametrize("text", ["at 12:30", "from 10:00 to 12:30", "12:30 pm", "12:30pm", "10:00 SAST", "by 9:45",
                                  "before 23:59:59", "until 7:00", "at 12:30 and 23:59:59", "at 9:00, 12:00 or 15:00"])
def test_a_clock_after_a_time_word_or_before_am_pm_or_sast_is_allowed(text):
    assert list(checks._numerals(text)) == []


# Third review block (1.11): clock phrasing that passed at HEAD, hyphenated scale words, vague cardinals, "out of",
# spelled percents after digits, quarters and decades.

HEAD_CLOCKS = ["between 19:00 and 21:00", "after 18:00", "around 21:00", "since 18:00", "Sunday 18:00",
               "peaked 20:00 to 22:00", "posts peak 19:00-21:00", "on Sunday, 18:00 to 20:00", "at around 21:00",
               "at roughly 18:30", "about 18:00", "approximately 18:00", "past 23:00", "on 18:00",
               "at 6pm", "at 6 p.m.", "6:30 pm", "18h00", "from 18h00 to 20h30"]


@pytest.mark.parametrize("text", HEAD_CLOCKS)
def test_clock_phrasing_that_names_a_time_is_allowed(text):
    assert list(checks._numerals(f"Plate posts peaked {text} on the day.")) == []


@pytest.mark.parametrize("field", ANSWER_FIELDS)
@pytest.mark.parametrize("text", ["between 19:00 and 21:00", "Sunday 18:00", "at 6pm", "18h00", "posts peak 19:00-21:00"])
def test_clock_phrasing_passes_in_every_field(draft, ctx, wh, field, text):
    where = put(draft, field, f"Sunday plate ratings peaked {text}.")
    _, verdicts = run(draft, ctx, wh)
    assert [v for v in verdicts if v["claim_id"] == where and v["verdict"] == "cut"] == []


@pytest.mark.parametrize("text", ["a 3:10 ratio", "13pm", "25h00", "a 3:10 and 4:99 split", "18h75"])
def test_a_clock_shape_that_is_no_time_is_still_a_figure(text):
    assert list(checks._numerals(text)) != []


@pytest.mark.parametrize("figure, values, expected", [
    ("a 1.2 million-view clip", [1200000], "pass"), ("a 1.2 million-view clip", [1.2], "cut"),
    ("2 million-strong", [2000000], "pass"), ("2 million-strong", [2], "cut"),
    ("20 thousand-plus", [20000], "pass"), ("20 thousand-plus", [20], "cut"),
    ("5 dozen-odd", [60], "pass"), ("5 k-pop acts", [5], "pass"),
    ("a few thousand posts", [5000], "pass"), ("a few thousand posts", [50], "cut"),
    ("several thousand posts", [3000], "pass"), ("several thousand posts", [30000], "cut"),
    ("a few million views", [4000000], "pass"), ("a few million views", [4], "cut"),
    ("a couple of hundred comments", [250], "pass"), ("a couple of hundred comments", [2], "cut"),
    ("few hundred posts", [300], "pass"), ("some thousand posts", [1500], "pass"), ("some thousand posts", [3000], "cut"),
    ("nine out of ten creators", [0.9], "pass"), ("nine out of ten creators", [9], "cut"),
    ("one out of every five creators", [0.2], "pass"), ("one in every four creators", [0.25], "pass"),
    ("40 percent of posts", [0.4], "pass"), ("40 percent of posts", [40], "pass"), ("40 percent of posts", [4], "cut"),
    ("40 per cent of posts", [0.4], "pass"),
])
def test_scale_words_vague_cardinals_out_of_and_digit_percents_are_read(figure, values, expected):
    assert k2_with(figure, values) == expected


@pytest.mark.parametrize("field", ANSWER_FIELDS)
@pytest.mark.parametrize("text, cut", [
    ("Plate posts rose in Q3 2026.", False), ("Plate posts will rise in Q1 2028.", False),
    ("Plate posts will rise in Q1 2031.", True), ("The 1990s sound is back on Sunday plates.", False),
    ("The '90s sound is back on Sunday plates.", False), ("2000s kwaito is back on Sunday plates.", False),
    ("The 1995s sound is back on Sunday plates.", True),
])
def test_a_quarter_with_its_year_and_a_decade_are_periods(draft, ctx, wh, field, text, cut):
    where = put(draft, field, text)
    _, verdicts = run(draft, ctx, wh)
    assert bool([v for v in verdicts if v["claim_id"] == where and v["rule"] == "K2" and v["verdict"] == "cut"]) is cut


# Fourth review block (rule 1): an apostrophe decade after "in their" is an age, in the checks and in no_age_lens alike.

APOSTROPHE_AGES = ["Plate fans in their '20s drove it.", "Plate fans in their ’30s drove it.",
                   "Plate fans in their '40s drove it.", "Plate fans in their late '20s drove it.",
                   "Plate fans in their mid-'30s drove it.", "Late-’20s creators drove the plate posts.",
                   "The '20s crowd drove the plate posts.", "Mid-'30s professionals drove the plate posts.",
                   "Plate fans in their fifties drove it.", "Plate fans in their 50s drove it.",
                   "Plate fans aged between 18 and 24 drove it.", "Creators born in the 2000s rated plates.",
                   "The 2000s generation rated plates.", "The '90s generation rated plates.",
                   "Creators who grew up in the 1990s rated plates.", "Creators born in 1998 rated plates."]
APOSTROPHE_CLEAN = ["The '90s sound is back on Sunday plates.", "The 1990s sound is back on Sunday plates.",
                    "Plate clips leaned on 90s kwaito.", "Plate posts peaked in the late 2000s."]


def test_no_age_lens_and_the_checks_flag_an_apostrophe_decade_in_their_alike():
    import subprocess

    import yaml

    config = yaml.safe_load((Path(__file__).resolve().parents[2] / "eval" / "promptfooconfig.yaml")
                            .read_text(encoding="utf-8"))
    body = next(a["value"] for a in config["defaultTest"]["assert"] if a.get("metric") == "no_age_lens")
    phrases = APOSTROPHE_AGES + APOSTROPHE_CLEAN
    done = subprocess.run(["node", "--input-type=module", "-e", AGE_RUNNER], capture_output=True, encoding="utf-8",
                          input=json.dumps({"body": body, "phrases": phrases}))
    assert done.returncode == 0, done.stderr
    expected = [True] * len(APOSTROPHE_AGES) + [False] * len(APOSTROPHE_CLEAN)
    assert json.loads(done.stdout) == expected
    assert [bool(checks._text_breaches(p)) for p in phrases] == expected
