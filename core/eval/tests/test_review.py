"""The weekly human review routine (TRUST.md section 6, BUILD.md task 1.20)."""

import csv
import io
import json
import math
import random
from collections import Counter

import pytest

from core.eval import review

WEEK = "2026-W40"
MARKETS = ["ZA", "NG", "KE"]


def claims(n=200):
    rows = []
    for i in range(n):
        rows.append({
            "id": f"c{i:03d}",
            "market": MARKETS[i % 3],
            "text": f"Posts about item {i} rose this week.",
            "url": f"https://example.org/p/{i}",
            "quote": f"plain english quote {i}",
            "numbers": [],
            "support": "supported",
        })
    return rows


def cards(per_market=12):
    rows = []
    for m in MARKETS:
        for r in range(1, per_market + 1):
            rows.append({"id": f"{m}-card{r}", "market": m, "rank": r, "url": f"https://example.org/{m}/{r}",
                         "text": f"{m} card {r}"})
    return rows


def label(kind, id_, verdict, *, reviewer="Jo", stratum="random", market="ZA", useful="", week=WEEK,
          non_english=False, double=False, language="", reason=""):
    return {"week": week, "kind": kind, "id": id_, "market": market, "stratum": stratum,
            "review_scope": "full" if market == "ZA" else "evidence_only", "double": double,
            "non_english": non_english, "language": language, "verdict": verdict, "useful": useful,
            "reviewer": reviewer, "reason": reason}


# Sampling

def test_claim_sample_is_30_random_plus_10_risk_with_no_duplicates():
    rows = review.claim_sample(claims(), week=WEEK, seed=7)
    assert len(rows) == 40
    assert len({r["id"] for r in rows}) == 40
    assert sum(r["stratum"] == "random" for r in rows) == 30
    assert sum(r["stratum"] == "risk" for r in rows) == 10
    assert sum(r["double"] for r in rows) == 4


def test_claim_sample_is_deterministic_for_week_and_seed_and_ignores_input_order():
    a = review.claim_sample(claims(), week=WEEK, seed=7)
    shuffled = claims()
    random.Random(1).shuffle(shuffled)
    b = review.claim_sample(shuffled, week=WEEK, seed=7)
    assert a == b
    c = review.claim_sample(claims(), week="2026-W41", seed=7)
    d = review.claim_sample(claims(), week=WEEK, seed=8)
    assert [r["id"] for r in c] != [r["id"] for r in a]
    assert [r["id"] for r in d] != [r["id"] for r in a]


def test_claim_sample_with_few_claims_takes_them_all_once():
    rows = review.claim_sample(claims(25), week=WEEK, seed=1)
    assert len(rows) == 25
    assert len({r["id"] for r in rows}) == 25


def test_risk_score_adds_numbers_causal_partial_and_non_english():
    base = {"id": "x", "market": "ZA", "text": "People posted about it.", "quote": "nice one", "numbers": [],
            "support": "supported"}
    assert review.risk_score(base) == 0
    assert review.risk_score({**base, "numbers": [{"value": 12, "query": "q1"}]}) == 1
    assert review.risk_score({**base, "numbers": [{"value": 12}]}) == 2
    assert review.risk_score({**base, "text": "It spread because of the final."}) == 1
    assert review.risk_score({**base, "text": "Driven by one creator."}) == 1
    assert review.risk_score({**base, "text": "Views fell due to the outage."}) == 1
    assert review.risk_score({**base, "text": "The clip led to a wave of remixes."}) == 1
    assert review.risk_score({**base, "support": "partial"}) == 1
    assert review.risk_score({**base, "quote": "Abeg wetin dey happen for here"}) == 1
    assert review.risk_score({**base, "quote": "Niaje msee, hii ni fiti sana"}) == 1
    assert review.risk_score({**base, "quote": "Ngiyabonga kakhulu, yebo"}) == 1
    assert review.risk_score({**base, "quote": "Ẹ kú àárọ̀ o, ṣé dáadáa ni"}) == 1
    everything = {**base, "numbers": [{"value": 3, "query": "q"}], "text": "It grew because of X.",
                  "support": "partial", "quote": "Abeg na wetin"}
    assert review.risk_score(everything) == 4


def test_non_english_detection_does_not_fire_on_plain_english():
    assert not review.non_english("This is a completely ordinary English sentence, no dialect at all.")
    assert not review.non_english("")
    assert review.non_english("Hapana, asante sana rafiki")


def test_a_number_with_no_query_is_riskier_than_one_with_a_query():
    base = {"id": "x", "market": "ZA", "text": "Posts rose.", "quote": "nice", "support": "supported"}
    with_query = review.risk_score({**base, "numbers": [{"value": 12, "query": "q1"}]})
    without = review.risk_score({**base, "numbers": [{"value": 12}]})
    mixed = review.risk_score({**base, "numbers": [{"value": 12, "query": "q1"}, {"value": 4}]})
    assert without > with_query > 0
    assert mixed == without


def test_markers_hold_no_english_words_and_cover_afrikaans_sesotho_and_setswana():
    assert "pole" not in review.MARKERS and "una" not in review.MARKERS
    assert not review.non_english("Take it pole to pole, said una of the fans")
    assert review.non_english("Dankie, dit is baie lekker")
    assert review.language_of("Dankie, dit is baie lekker") == "af"
    assert review.language_of("Dumela ntate, kea leboha haholo") == "st"
    assert review.language_of("Dumelang rra, ke a leboga thata") == "tn"
    assert review.language_of("Niaje msee, hii ni fiti") == "sheng"
    assert review.language_of("This is plain English") == ""


def test_claim_rows_carry_the_quote_language():
    rows = claims(40)
    rows[0]["quote"] = "Dankie, dit is baie lekker"
    rows[1]["quote_lang"] = "zu"
    rows[2]["quote"] = "Ẹ kú àárọ̀ o, ṣé dáadáa ni"
    sample = {r["id"]: r for r in review.claim_sample(rows, week=WEEK, seed=1)}
    assert sample["c000"]["language"] == "af"
    assert sample["c001"]["language"] == "zu"
    assert sample["c002"]["language"] == "unknown"
    assert sample["c003"]["language"] == ""
    assert "language" in review.SHEET_COLUMNS


def test_risk_stratum_takes_the_highest_risk_claims_outside_the_random_draw():
    rows = claims(200)
    risky = {}
    for i, r in enumerate(rows):
        if i % 20 == 0:
            r["numbers"] = [{"value": 5, "query": "q"}]
            r["text"] = "Shares rose because of the derby."
            r["support"] = "partial"
            risky[r["id"]] = 3
    sample = review.claim_sample(rows, week=WEEK, seed=3)
    risk_ids = [r["id"] for r in sample if r["stratum"] == "risk"]
    random_ids = {r["id"] for r in sample if r["stratum"] == "random"}
    expected = [i for i in risky if i not in random_ids]
    assert set(expected) <= set(risk_ids)
    scores = [review.risk_score(next(c for c in rows if c["id"] == i)) for i in risk_ids]
    assert scores == sorted(scores, reverse=True)


def test_card_sample_is_every_top3_per_market_plus_11_random():
    rows = review.card_sample(cards(), week=WEEK, seed=5)
    top = [r for r in rows if r["stratum"] == "top3"]
    rand = [r for r in rows if r["stratum"] == "random"]
    assert len(top) == 9
    assert {(r["market"], r["rank"]) for r in top} == {(m, k) for m in MARKETS for k in (1, 2, 3)}
    assert len(rand) == 11
    assert all(r["rank"] > 3 for r in rand)
    assert len({r["id"] for r in rows}) == 20
    assert sum(r["double"] for r in rows) == 2
    assert rows == review.card_sample(list(reversed(cards())), week=WEEK, seed=5)


def test_review_scope_is_full_for_za_and_evidence_only_for_ng_and_ke():
    for row in review.claim_sample(claims(), week=WEEK, seed=7) + review.card_sample(cards(), week=WEEK, seed=5):
        assert row["review_scope"] == ("full" if row["market"] == "ZA" else "evidence_only")


def test_unknown_market_is_refused():
    bad = claims(40)
    bad[0]["market"] = "GH"
    with pytest.raises(ValueError, match="market"):
        review.claim_sample(bad, week=WEEK, seed=1)


# Sheet

def test_sheet_round_trip_and_double_rows_appear_twice():
    rows = review.claim_sample(claims(), week=WEEK, seed=7) + review.card_sample(cards(), week=WEEK, seed=5)
    text = review.review_sheet(rows)
    lines = list(csv.DictReader(io.StringIO(text)))
    for col in ("id", "url", "text", "quote", "verdict", "reviewer", "reason", "double", "review_scope"):
        assert col in lines[0]
    assert len(lines) == len(rows) + sum(r["double"] for r in rows)
    assert all(line["verdict"] == "" and line["reviewer"] == "" for line in lines)
    seen = set()
    for line in lines:
        dup = line["id"] in seen
        seen.add(line["id"])
        line["reviewer"] = "Thapelo" if dup else "Jo"
        if line["kind"] == "claim":
            line["verdict"] = "supported"
        else:
            line["verdict"], line["useful"] = "real", "yes"
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(lines[0]))
    writer.writeheader()
    writer.writerows(lines)
    labels = review.read_sheet(out.getvalue())
    assert len(labels) == len(lines)
    first = next(r for r in rows if r["kind"] == "claim")
    got = next(lab for lab in labels if lab["id"] == first["id"])
    assert got["url"] == first["url"] and got["quote"] == first["quote"] and got["week"] == WEEK
    assert got["double"] == first["double"] and isinstance(got["double"], bool)
    assert got["review_scope"] == first["review_scope"]


def sheet(*lines):
    head = review.SHEET_COLUMNS
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=head)
    writer.writeheader()
    for line in lines:
        writer.writerow({c: line.get(c, "") for c in head})
    return out.getvalue()


def test_read_sheet_refuses_unknown_verdicts():
    good = {"week": WEEK, "kind": "claim", "id": "c1", "market": "ZA", "stratum": "random", "review_scope": "full",
            "double": "false", "non_english": "false", "verdict": "supported", "reviewer": "Jo"}
    assert review.read_sheet(sheet(good))[0]["verdict"] == "supported"
    with pytest.raises(ValueError, match="verdict"):
        review.read_sheet(sheet({**good, "verdict": "mostly true"}))
    with pytest.raises(ValueError, match="verdict"):
        review.read_sheet(sheet({**good, "verdict": "real"}))
    with pytest.raises(ValueError, match="verdict"):
        review.read_sheet(sheet({**good, "kind": "card", "verdict": "supported"}))
    with pytest.raises(ValueError, match="useful"):
        review.read_sheet(sheet({**good, "kind": "card", "verdict": "real", "useful": "maybe"}))


def test_read_sheet_requires_yes_or_no_for_useful_on_card_lines():
    card = {"week": WEEK, "kind": "card", "id": "k1", "market": "ZA", "stratum": "top3", "review_scope": "full",
            "double": "false", "non_english": "false", "verdict": "real", "useful": "", "reviewer": "Jo"}
    with pytest.raises(ValueError, match="k1: useful"):
        review.read_sheet(sheet(card))
    assert review.read_sheet(sheet({**card, "useful": " No"}))[0]["useful"] == "no"
    claim = {**card, "kind": "claim", "verdict": "supported"}
    assert review.read_sheet(sheet(claim))[0]["useful"] == ""


def test_read_sheet_accepts_trust_md_unverifiable_as_the_rubric_unverified():
    line = {"week": WEEK, "kind": "claim", "id": "c1", "market": "ZA", "stratum": "random", "review_scope": "full",
            "double": "false", "non_english": "false", "verdict": "Unverifiable ", "reviewer": "Jo"}
    assert review.read_sheet(sheet(line))[0]["verdict"] == "unverified"


def test_read_sheet_refuses_blank_verdicts_and_missing_reviewers():
    line = {"week": WEEK, "kind": "claim", "id": "c1", "market": "ZA", "stratum": "random", "review_scope": "full",
            "double": "false", "non_english": "false", "verdict": "", "reviewer": "Jo"}
    with pytest.raises(ValueError, match="c1"):
        review.read_sheet(sheet(line))
    with pytest.raises(ValueError, match="reviewer"):
        review.read_sheet(sheet({**line, "verdict": "supported", "reviewer": ""}))


# Statistics

def test_cohens_kappa_matches_the_hand_computed_example():
    # 50 items: both yes 20, A yes B no 5, A no B yes 10, both no 15.
    # p_o = 35/50 = 0.7; p_e = 0.5*0.6 + 0.5*0.4 = 0.5; kappa = (0.7 - 0.5) / 0.5 = 0.4.
    a = ["yes"] * 25 + ["no"] * 25
    b = ["yes"] * 20 + ["no"] * 5 + ["yes"] * 10 + ["no"] * 15
    assert review.cohens_kappa(a, b) == pytest.approx(0.4)
    assert review.cohens_kappa(["x", "y"], ["x", "y"]) == pytest.approx(1.0)
    assert review.cohens_kappa(["x", "x"], ["x", "x"]) is None


def test_rule_of_three_and_clopper_pearson_known_values():
    assert review.error_upper_bound(0, 120) == (pytest.approx(0.025), "rule_of_three")
    assert review.error_upper_bound(0, 0) == (None, None)
    # Exact one-sided 95% Clopper-Pearson with no errors is 1 - 0.05**(1/n), close to 3/n.
    assert review.clopper_pearson_upper(0, 120) == pytest.approx(1 - 0.05 ** (1 / 120), abs=1e-9)
    # Textbook two-sided 95% interval for 1 in 10 is (0.0025, 0.4450).
    assert review.clopper_pearson_upper(1, 10, level=0.975) == pytest.approx(0.4450, abs=1e-4)
    # scipy.stats.beta.ppf(0.95, 2, 119) = 0.0389208...
    bound, method = review.error_upper_bound(1, 120)
    assert method == "clopper_pearson"
    assert bound == pytest.approx(0.0389208, abs=1e-6)
    assert review.clopper_pearson_upper(120, 120) == 1.0


def test_zero_errors_at_small_n_use_the_exact_bound_not_three_over_n():
    assert review.error_upper_bound(0, 1) == (pytest.approx(0.95), "exact")
    assert review.error_upper_bound(0, 2) == (pytest.approx(1 - 0.05 ** 0.5), "exact")
    for n in range(1, 30):
        bound, method = review.error_upper_bound(0, n)
        assert method == "exact"
        assert bound == pytest.approx(min(1.0, 1 - 0.05 ** (1 / n)))
        assert bound <= 1.0


def test_clopper_pearson_agrees_with_scipy_where_installed():
    stats = pytest.importorskip("scipy.stats")
    for x, n in [(1, 30), (2, 40), (5, 120), (17, 300), (1, 1000)]:
        assert review.clopper_pearson_upper(x, n) == pytest.approx(stats.beta.ppf(0.95, x + 1, n - x), abs=1e-7)


# Weekly score

def test_score_week_error_rate_uses_the_random_stratum_only():
    labels = [label("claim", f"r{i}", "supported") for i in range(30)]
    labels += [label("claim", f"k{i}", "contradicted", stratum="risk") for i in range(10)]
    s = review.score_week(labels)
    assert s["claims"]["n"] == 30
    assert s["claims"]["errors"] == 0
    assert s["claims"]["upper_95"] == pytest.approx(0.1)
    assert s["claims"]["bound"] == "rule_of_three"
    assert s["risk"] == {"n": 10, "errors": 10}


def test_score_week_counts_contradicted_and_unverified_as_errors_and_reports_both():
    labels = [label("claim", f"r{i}", "supported") for i in range(28)]
    labels += [label("claim", "r28", "contradicted"), label("claim", "r29", "unverified")]
    s = review.score_week(labels)["claims"]
    assert (s["errors"], s["contradicted"], s["unverified"]) == (2, 1, 1)
    assert s["error_rate"] == pytest.approx(2 / 30)
    assert s["bound"] == "clopper_pearson"
    assert s["upper_95"] == pytest.approx(review.clopper_pearson_upper(2, 30))


def test_a_double_reviewed_claim_counts_once_and_is_an_error_if_either_reviewer_says_so():
    labels = [label("claim", f"r{i}", "supported") for i in range(29)]
    labels += [label("claim", "r29", "supported", double=True),
               label("claim", "r29", "contradicted", reviewer="Thapelo", double=True)]
    s = review.score_week(labels)
    assert s["claims"]["n"] == 30
    assert s["claims"]["errors"] == 1


def test_score_week_kappa_on_double_reviewed_items():
    labels = []
    a = ["supported"] * 25 + ["contradicted"] * 25
    b = ["supported"] * 20 + ["contradicted"] * 5 + ["supported"] * 10 + ["contradicted"] * 15
    for i, (x, y) in enumerate(zip(a, b)):
        labels += [label("claim", f"d{i}", x, double=True), label("claim", f"d{i}", y, reviewer="Thabang", double=True)]
    s = review.score_week(labels)
    assert s["kappa"]["claims"]["n"] == 50
    assert s["kappa"]["claims"]["value"] == pytest.approx(0.4)
    assert s["kappa"]["cards"] == {"n": 0, "value": None}
    assert s["passes"]["kappa"] is False

    agree = [label("claim", "e1", "supported", double=True), label("claim", "e1", "supported", reviewer="Thabang", double=True),
             label("claim", "e2", "contradicted", double=True), label("claim", "e2", "contradicted", reviewer="Thabang", double=True)]
    s = review.score_week(agree)
    assert s["kappa"]["claims"]["value"] == pytest.approx(1.0)
    assert s["passes"]["kappa"] is True


def test_kappa_is_per_kind_and_one_low_kind_fails_the_pass():
    labels = [label("claim", "e1", "supported", double=True), label("claim", "e1", "supported", reviewer="Thabang", double=True),
              label("claim", "e2", "contradicted", double=True), label("claim", "e2", "contradicted", reviewer="Thabang", double=True),
              label("card", "f1", "real", double=True), label("card", "f1", "not_real", reviewer="Thabang", double=True),
              label("card", "f2", "not_real", double=True), label("card", "f2", "real", reviewer="Thabang", double=True)]
    s = review.score_week(labels)
    assert s["kappa"]["claims"]["value"] == pytest.approx(1.0)
    assert s["kappa"]["cards"]["value"] == pytest.approx(-1.0)
    assert s["passes"]["kappa"] is False


def test_kappa_pass_is_none_when_agreement_is_not_measurable():
    labels = [label("claim", "e1", "supported", double=True), label("claim", "e1", "supported", reviewer="Thabang", double=True)]
    s = review.score_week(labels)
    assert s["kappa"]["claims"] == {"n": 1, "value": None}
    assert s["passes"]["kappa"] is None


def test_kappa_ignores_a_repeated_line_from_the_same_reviewer():
    labels = [label("claim", "e1", "supported", double=True), label("claim", "e1", "supported", double=True)]
    assert review.score_week(labels)["kappa"]["claims"]["n"] == 0
    assert review.score_week(labels)["passes"]["kappa"] is None


def test_card_precision_is_the_real_share():
    labels = [label("card", f"t{i}", "real", stratum="top3", useful="yes") for i in range(7)]
    labels += [label("card", f"t{i}", "not_real", stratum="top3", useful="no") for i in range(7, 9)]
    labels += [label("card", f"x{i}", "real", useful="no") for i in range(5)]
    labels += [label("card", f"x{i}", "not_real", useful="no") for i in range(5, 11)]
    c = review.score_week(labels)["cards"]
    assert c["n"] == 20
    assert "precision" not in c  # no pooled headline: top-3 and random answer different questions
    assert c["precision_top3"] == pytest.approx(7 / 9)
    assert c["precision_random"] == pytest.approx(5 / 11)
    assert c["useful"] == pytest.approx(7 / 20)


def test_grader_agreement_against_promptfoo_verdicts():
    labels = [label("claim", f"r{i}", "supported") for i in range(20)]
    grader = {f"r{i}": ("supported" if i < 17 else "contradicted") for i in range(20)}
    grader["not_sampled"] = "supported"
    s = review.score_week(labels, grader=grader)
    assert s["grader"] == {"n": 20, "agreement": pytest.approx(0.85)}
    assert s["passes"]["grader"] is True
    grader["r16"] = "unverifiable"
    s = review.score_week(labels, grader=grader)
    assert s["grader"]["agreement"] == pytest.approx(0.80)
    assert s["passes"]["grader"] is False
    assert review.score_week(labels)["grader"] is None
    assert review.score_week(labels)["passes"]["grader"] is None


def test_cumulative_bound_over_four_weeks_of_120_random_claims():
    weeks = ["2026-W37", "2026-W38", "2026-W39", "2026-W40"]
    prior = [label("claim", f"{w}-{i}", "supported", week=w) for w in weeks[:3] for i in range(30)]
    now = [label("claim", f"{weeks[3]}-{i}", "supported", week=weeks[3]) for i in range(30)]
    s = review.score_week(now, prior=prior)
    assert s["week"] == "2026-W40"
    assert s["claims"]["n"] == 30
    cum = s["cumulative"]
    assert cum["weeks"] == weeks
    assert (cum["n"], cum["errors"]) == (120, 0)
    assert cum["upper_95"] == pytest.approx(0.025)
    assert cum["bound"] == "rule_of_three"
    assert cum["kappa"]["claims"]["n"] == 0


def test_cumulative_refuses_more_than_four_weeks():
    prior = [label("claim", f"{w}-1", "supported", week=w) for w in ["2026-W36", "2026-W37", "2026-W38", "2026-W39"]]
    with pytest.raises(ValueError, match="4 weeks"):
        review.score_week([label("claim", "n1", "supported")], prior=prior)


def test_score_week_reports_za_and_ng_ke_separately():
    labels = [label("claim", f"z{i}", "supported", market="ZA") for i in range(10)]
    labels += [label("claim", f"n{i}", "supported", market="NG") for i in range(10)]
    labels += [label("claim", "k1", "contradicted", market="KE")]
    labels += [label("card", "zc", "real", market="ZA", useful="yes"), label("card", "kc", "not_real", market="KE", useful="no")]
    s = review.score_week(labels)
    assert s["by_group"]["ZA"]["claims"]["n"] == 10
    assert s["by_group"]["ZA"]["claims"]["errors"] == 0
    assert s["by_group"]["NG_KE"]["claims"]["n"] == 11
    assert s["by_group"]["NG_KE"]["claims"]["errors"] == 1
    assert s["by_group"]["ZA"]["cards"]["precision_random"] == 1.0
    assert s["by_group"]["NG_KE"]["cards"]["precision_random"] == 0.0
    assert s["claims"]["n"] == 21


def test_evidence_only_rows_never_count_toward_native_language_accuracy():
    labels = [label("claim", f"z{i}", "supported", market="ZA", non_english=True) for i in range(4)]
    labels += [label("claim", "z4", "contradicted", market="ZA", non_english=True)]
    labels += [label("claim", "z5", "contradicted", market="ZA", non_english=False)]
    labels += [label("claim", f"n{i}", "contradicted", market="NG", non_english=True) for i in range(5)]
    labels += [label("claim", f"k{i}", "supported", market="KE", non_english=True) for i in range(5)]
    scored = review.score_week(labels)
    assert "native" not in scored
    support = scored["non_english_support"]
    assert (support["n"], support["support"]) == (5, pytest.approx(0.8))
    only_evidence = [lab for lab in labels if lab["market"] != "ZA"]
    empty = review.score_week(only_evidence)["non_english_support"]
    assert (empty["n"], empty["support"], empty["by_language"], empty["flagged"]) == (0, None, {}, [])


def test_a_failing_language_never_hides_behind_a_good_one_in_non_english_support():
    labels = [label("claim", f"z{i}", "supported", non_english=True, language="zu") for i in range(20)]
    labels += [label("claim", "a1", "supported", non_english=True, language="af"),
               label("claim", "a2", "contradicted", non_english=True, language="af")]
    labels += [label("claim", "u1", "supported", non_english=True)]
    support = review.score_week(labels)["non_english_support"]
    assert support["n"] == 23
    assert support["support"] == pytest.approx(22 / 23)  # pooled looks fine
    assert support["by_language"]["zu"] == {"n": 20, "support": 1.0, "flagged": False}
    assert support["by_language"]["af"] == {"n": 2, "support": pytest.approx(0.5), "flagged": True}
    assert support["by_language"]["unknown"]["n"] == 1
    assert support["flagged"] == ["af"]


# Native-speaker checks

def quotes(per_language=20):
    rows = []
    for lang in review.LANGUAGES:
        for i in range(per_language):
            rows.append({"id": f"{lang}-q{i:02d}", "language": lang, "market": "ZA", "text": f"{lang} quote {i}",
                         "url": f"https://example.org/{lang}/{i}", "gloss": f"gloss {i}", "tone": "positive"})
    return rows


def test_rotation_follows_trust_md_order_one_slot_a_week():
    names = [name for name, _ in review.ROTATION]
    assert names == ["isiZulu and isiXhosa", "Sesotho", "Setswana", "Sepedi and Xitsonga", "Afrikaans",
                     "Pidgin", "Yoruba", "Hausa and Igbo", "Swahili and Sheng"]
    weeks = [f"2026-W{w}" for w in range(40, 54)] + ["2027-W01"]  # 2026 has 53 ISO weeks
    slots = [review.rotation(w)[0] for w in weeks]
    start = names.index(slots[0])
    assert slots == [names[(start + i) % 9] for i in range(len(weeks))]


def test_native_sample_is_15_quotes_of_one_language_from_the_weeks_slot():
    _, langs = review.rotation(WEEK)
    lang = langs[0]
    rows = review.native_sample(quotes(), week=WEEK, seed=3, language=lang)
    assert len(rows) == 15
    assert len({r["id"] for r in rows}) == 15
    assert {r["language"] for r in rows} == {lang}
    assert all(r["week"] == WEEK and r["gloss"] and r["tone"] and r["quote"] for r in rows)
    shuffled = quotes()
    random.Random(2).shuffle(shuffled)
    assert rows == review.native_sample(shuffled, week=WEEK, seed=3, language=lang)
    assert rows != review.native_sample(quotes(), week=WEEK, seed=4, language=lang)


def test_native_sample_defaults_to_the_weeks_slot_and_refuses_a_language_outside_it():
    _, langs = review.rotation(WEEK)
    rows = review.native_sample(quotes(), week=WEEK, seed=3, language=None)
    assert len(rows) == 15 and {r["language"] for r in rows} <= set(langs)
    outside = next(lang for lang in review.LANGUAGES if lang not in langs)
    with pytest.raises(ValueError, match="rotation"):
        review.native_sample(quotes(), week=WEEK, seed=3, language=outside)
    few = review.native_sample(quotes(4), week=WEEK, seed=3, language=langs[0])
    assert len(few) == 4


def native_filled(results, week=WEEK):
    """results: (language, gloss_correct, tone_correct) per quote."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=review.NATIVE_COLUMNS)
    writer.writeheader()
    for i, (lang, gloss, tone) in enumerate(results):
        writer.writerow({c: "" for c in review.NATIVE_COLUMNS} | {
            "week": week, "id": f"{lang}-{i}", "language": lang, "market": "ZA", "quote": "q", "gloss": "g",
            "tone": "neutral", "gloss_correct": gloss, "tone_correct": tone, "reviewer": "Sipho"})
    return out.getvalue()


def test_native_sheet_round_trip_has_gloss_and_tone_columns():
    rows = review.native_sample(quotes(), week=WEEK, seed=3, language=review.rotation(WEEK)[1][0])
    lines = list(csv.DictReader(io.StringIO(review.native_sheet(rows))))
    assert len(lines) == 15
    for col in ("quote", "gloss", "tone", "gloss_correct", "tone_correct", "reviewer", "reason", "language"):
        assert col in lines[0]
    assert all(line["gloss_correct"] == "" and line["tone_correct"] == "" for line in lines)


def test_read_native_sheet_requires_yes_or_no_a_reviewer_and_a_known_language():
    good = review.read_native_sheet(native_filled([("zu", "yes", "No ")]))
    assert good[0]["gloss_correct"] is True and good[0]["tone_correct"] is False
    with pytest.raises(ValueError, match="gloss_correct"):
        review.read_native_sheet(native_filled([("zu", "", "yes")]))
    with pytest.raises(ValueError, match="tone_correct"):
        review.read_native_sheet(native_filled([("zu", "yes", "maybe")]))
    with pytest.raises(ValueError, match="language"):
        review.read_native_sheet(native_filled([("xx", "yes", "yes")]))
    with pytest.raises(ValueError, match="reviewer"):
        review.read_native_sheet(native_filled([("zu", "yes", "yes")]).replace("Sipho", ""))


def test_score_native_is_per_language_and_caps_tone_under_80_percent():
    results = [("zu", "yes", "yes")] * 14 + [("zu", "no", "yes")]  # 14/15
    results += [("xh", "yes", "yes")] * 3 + [("xh", "yes", "no")] * 2  # 3/5
    s = review.score_native(review.read_native_sheet(native_filled(results)))
    zu, xh = s["by_language"]["zu"], s["by_language"]["xh"]
    assert (zu["n"], zu["gloss_correct"], zu["tone_correct"]) == (15, pytest.approx(14 / 15), 1.0)
    assert zu["accuracy"] == pytest.approx(14 / 15)
    assert zu["tone_capped"] is None
    assert xh["accuracy"] == pytest.approx(0.6)
    assert xh["tone_capped"] == "tone capped at Single source"
    assert s["capped"] == ["xh", *review.NO_REVIEWER]
    group = s["by_group"]["isiZulu and isiXhosa"]
    assert group["n"] == 20 and group["accuracy"] == pytest.approx(17 / 20)
    assert group["meets_target"] is False  # 90% per group


def test_a_quote_checked_twice_is_correct_only_if_both_native_speakers_say_so():
    text = native_filled([("af", "yes", "yes")] * 5)  # five quotes, the fewest score_native caps on
    second = [line.replace("Sipho", "Anika").replace("yes,yes", "yes,no") for line in text.splitlines()[1:]]
    s = review.score_native(review.read_native_sheet(text + "\n".join(second) + "\n"))
    assert s["by_language"]["af"]["n"] == 5
    assert s["by_language"]["af"]["accuracy"] == 0.0
    assert s["capped"] == ["af", *review.NO_REVIEWER]


# FEATURES 27: every language in the slot gets tone labels checked, and a quote with no gloss is checked for tone

def test_a_two_language_slot_splits_the_15_quotes_between_its_languages():
    _, langs = review.rotation(WEEK)
    assert len(langs) == 2
    rows = review.native_sample(quotes(), week=WEEK, seed=3, language=None)
    assert Counter(r["language"] for r in rows) == {langs[0]: 8, langs[1]: 7}
    lopsided = [q for q in quotes() if q["language"] == langs[0]] + [q for q in quotes(3) if q["language"] == langs[1]]
    rows = review.native_sample(lopsided, week=WEEK, seed=3, language=None)
    assert Counter(r["language"] for r in rows) == {langs[0]: 12, langs[1]: 3}


def test_a_quote_with_no_tone_label_is_never_sampled():
    lang = review.rotation(WEEK)[1][0]
    pool = [q | {"tone": ""} if i % 2 else q for i, q in enumerate(quotes()) if q["language"] == lang]
    rows = review.native_sample(pool, week=WEEK, seed=3, language=lang)
    assert len(rows) == 10 and all(r["tone"] for r in rows)


def tone_only_sheet(results):
    """results: (gloss_correct, tone_correct) per zu quote whose gloss is empty, as native_quotes.sql gives."""
    text = native_filled([("zu", g, t) for g, t in results])
    return text.replace(",q,g,neutral,", ",q,,neutral,")


def test_a_quote_with_no_gloss_is_marked_for_tone_alone():
    labels = review.read_native_sheet(tone_only_sheet([("", "yes")] * 4 + [("", "no")]))
    assert [lab["gloss_correct"] for lab in labels] == [None] * 5
    s = review.score_native(labels)["by_language"]["zu"]
    assert (s["n"], s["tone_correct"], s["accuracy"], s["gloss_correct"]) == (5, pytest.approx(0.8),
                                                                             pytest.approx(0.8), None)
    assert s["tone_capped"] is None
    capped = review.score_native(review.read_native_sheet(tone_only_sheet([("", "yes")] * 3 + [("", "no")] * 2)))
    assert capped["capped"] == ["zu", *review.NO_REVIEWER]
    back = [review.from_feedback(review.to_feedback(lab)) for lab in labels]
    assert review.score_native(back) == review.score_native(labels)
    with pytest.raises(ValueError, match="gloss_correct"):
        review.read_native_sheet(native_filled([("zu", "", "yes")]))  # a gloss shown must still be marked
    with pytest.raises(ValueError, match="tone_correct"):
        review.read_native_sheet(tone_only_sheet([("", "")]))


def test_score_native_says_when_accuracy_rests_on_tone_alone():
    s = review.score_native(review.read_native_sheet(tone_only_sheet([("", "yes")] * 5)))
    zu = s["by_language"]["zu"]
    assert (zu["accuracy"], zu["tone_capped"], zu["status"]) == (1.0, None, "cleared")
    assert zu["accuracy_basis"] == "tone only"
    assert s["by_group"]["isiZulu and isiXhosa"]["accuracy_basis"] == "tone only"
    glossed = review.score_native(review.read_native_sheet(native_filled([("zu", "yes", "yes")] * 5)))
    assert glossed["by_language"]["zu"]["accuracy_basis"] == "gloss and tone"
    mixed_text = native_filled([("zu", "", "yes")] * 2 + [("zu", "yes", "yes")] * 3)
    mixed = review.score_native(review.read_native_sheet(mixed_text.replace(",q,g,neutral,", ",q,,neutral,", 2)))
    assert mixed["by_language"]["zu"]["accuracy_basis"] == "gloss and tone, tone only where no gloss"


# No Lagos or Nairobi reviewer is named yet (progress/L3.md, Decisions from Albert)

def slot_weeks():
    return {review.rotation(f"2026-W{w}")[0]: f"2026-W{w}" for w in range(40, 49)}


def test_slots_with_no_named_reviewer_are_marked_unreviewed_and_get_no_sheet():
    assert review.NO_REVIEWER == ("pcm", "yo", "ha", "ig", "sw", "sheng")
    weeks = slot_weeks()
    for name in ("Pidgin", "Yoruba", "Hausa and Igbo", "Swahili and Sheng"):
        slot = review.native_slot(weeks[name])
        assert (slot["slot"], slot["status"]) == (name, review.UNREVIEWED)
        assert review.native_sample(quotes(), week=weeks[name], seed=3, language=None) == []
        for code in slot["languages"]:
            assert review.native_sample(quotes(), week=weeks[name], seed=3, language=code) == []
    for name in ("isiZulu and isiXhosa", "Sesotho", "Setswana", "Sepedi and Xitsonga", "Afrikaans"):
        assert review.native_slot(weeks[name])["status"] == "due"
        assert len(review.native_sample(quotes(), week=weeks[name], seed=3, language=None)) == 15


def test_score_native_neither_caps_nor_clears_a_language_on_fewer_than_5_quotes():
    results = [("zu", "yes", "yes")] * 4 + [("xh", "no", "no")] * 3 + [("xh", "yes", "yes")]
    s = review.score_native(review.read_native_sheet(native_filled(results)))
    for lang in ("zu", "xh"):
        block = s["by_language"][lang]
        assert (block["n"], block["status"], block["tone_capped"]) == (4, "insufficient", None)
    assert s["capped"] == list(review.NO_REVIEWER) and s["insufficient"] == ["xh", "zu"]
    four = review.score_native(review.read_native_sheet(native_filled([("zu", "yes", "yes")] * 4)))
    assert four["by_group"]["isiZulu and isiXhosa"]["meets_target"] is None
    five = review.score_native(review.read_native_sheet(native_filled([("zu", "yes", "yes")] * 4
                                                                      + [("zu", "no", "yes")])))
    assert five["by_language"]["zu"]["status"] == "cleared" and five["insufficient"] == []


def test_a_language_is_capped_early_when_even_a_perfect_rest_of_the_week_cannot_reach_80_percent():
    """12 of 15 is the floor, so four wrong quotes cap a language before NATIVE_MIN; three do not."""
    s = review.score_native(review.read_native_sheet(native_filled([("xh", "no", "no")] * 4)))
    xh = s["by_language"]["xh"]
    assert (xh["n"], xh["status"], xh["tone_capped"]) == (4, "capped", review.TONE_CAPPED)
    assert s["capped"] == ["xh", *review.NO_REVIEWER] and s["insufficient"] == []
    three = review.score_native(review.read_native_sheet(native_filled([("xh", "no", "yes")] * 3)))
    assert three["by_language"]["xh"]["status"] == "insufficient"
    one_right = native_filled([("zu", "yes", "yes")] + [("zu", "yes", "no")] * 3)
    assert review.score_native(review.read_native_sheet(one_right))["by_language"]["zu"]["status"] == "insufficient"


def test_ng_and_ke_languages_stay_capped_until_a_reviewer_is_named():
    s = review.score_native(review.read_native_sheet(native_filled([("pcm", "yes", "yes")] * 15)))
    pcm = s["by_language"]["pcm"]
    assert pcm["accuracy"] == 1.0
    assert (pcm["status"], pcm["tone_capped"]) == ("unreviewed", review.TONE_CAPPED)
    assert s["capped"] == list(review.NO_REVIEWER)
    assert s["unreviewed"] == list(review.NO_REVIEWER)
    assert review.score_native([])["unreviewed"] == list(review.NO_REVIEWER)


def test_capped_lists_every_ng_and_ke_language_even_with_no_labels():
    """A consumer reading only capped must see pcm, yo, ha, ig, sw and sheng as capped."""
    assert review.score_native([])["capped"] == ["pcm", "yo", "ha", "ig", "sw", "sheng"]
    s = review.score_native(review.read_native_sheet(native_filled([("zu", "yes", "yes")] * 15)))
    assert s["capped"] == list(review.NO_REVIEWER)


# Conformal threshold

def synthetic(n, rng, power=4):
    scores, labels = [], []
    for _ in range(n):
        s = rng.random()
        scores.append(s)
        labels.append(rng.random() >= 0.6 * (1 - s) ** power)  # True means the claim is supported
    return scores, labels


def test_conformal_threshold_is_none_below_300_labels():
    scores, labels = synthetic(299, random.Random(0))
    assert review.conformal_threshold(scores, labels) is None
    clean = [i / 300 for i in range(300)]
    assert review.conformal_threshold(clean[:299], [s > 0.3 for s in clean[:299]]) is None
    assert review.conformal_threshold(clean, [s > 0.3 for s in clean]) is not None


def test_conformal_threshold_meets_alpha_on_held_out_data():
    # Promise: with 95% confidence over the calibration draw, accepted claims are wrong at most alpha.
    test_scores, test_labels = synthetic(100_000, random.Random(11))
    rates = []
    for trial in range(100):
        scores, labels = synthetic(600, random.Random(100 + trial))
        t = review.conformal_threshold(scores, labels, alpha=0.03)
        if t is None:  # nothing certified: the gate keeps supported-only, which is safe
            continue
        kept = [ok for s, ok in zip(test_scores, test_labels) if s >= t]
        assert len(kept) > 10_000
        rates.append(1 - sum(kept) / len(kept))
    assert len(rates) >= 95
    assert sum(rates) / len(rates) <= 0.03
    assert sum(r > 0.03 for r in rates) <= 5


def test_conformal_threshold_accepts_verdict_strings_and_rejects_mismatched_lengths():
    scores, flags = synthetic(400, random.Random(5))
    words = ["supported" if ok else "contradicted" for ok in flags]
    assert review.conformal_threshold(scores, words) == review.conformal_threshold(scores, flags)
    with pytest.raises(ValueError):
        review.conformal_threshold(scores, flags[:-1])


def clean(n=400):
    scores = [i / n for i in range(n)]
    return scores, [s > 0.3 for s in scores]


def test_conformal_threshold_reads_any_truthy_non_string_as_true():
    scores, flags = clean()
    expected = review.conformal_threshold(scores, flags)
    assert expected is not None
    assert review.conformal_threshold(scores, [int(f) for f in flags]) == expected
    try:
        import numpy as np
    except ImportError:
        return
    assert review.conformal_threshold(scores, list(np.array(flags, dtype=bool))) == expected


def test_conformal_threshold_refuses_labels_that_are_not_verdicts():
    scores, flags = clean()
    with pytest.raises(ValueError, match="label"):
        review.conformal_threshold(scores, [str(f) for f in flags])  # "True"/"False" would all read as wrong
    with pytest.raises(ValueError, match="label"):
        review.conformal_threshold(scores, ["yes" if f else "no" for f in flags])


def test_conformal_collapses_double_reviews_to_one_label_per_claim_before_the_300_minimum():
    scores, flags = clean()
    ids = [f"c{i}" for i in range(400)]
    base = review.conformal_threshold(scores, flags, ids=ids)
    assert base is not None and base == review.conformal_threshold(scores, flags)
    # 200 claims each reviewed twice make 400 lines but only 200 labels: below the minimum.
    spread = clean(200)
    assert review.conformal_threshold(spread[0] * 2, spread[1] * 2) is not None  # 400 lines alone would pass
    assert review.conformal_threshold(spread[0] * 2, spread[1] * 2, ids=[f"d{i}" for i in range(200)] * 2) is None
    # A claim is wrong when any reviewer marks it wrong.
    top = max(range(400), key=lambda i: scores[i])
    assert flags[top]
    extra = review.conformal_threshold(scores + [scores[top]], flags + [False], ids=ids + [ids[top]])
    wrong_top = review.conformal_threshold(scores, flags[:top] + [False] + flags[top + 1:], ids=ids)
    assert extra == wrong_top
    assert extra != base
    with pytest.raises(ValueError, match="ids"):
        review.conformal_threshold(scores, flags, ids=ids[:-1])
    with pytest.raises(ValueError, match="score"):
        review.conformal_threshold(scores + [scores[top] / 2], flags + [True], ids=ids + [ids[top]])


def test_conformal_threshold_is_none_when_no_threshold_reaches_alpha():
    rng = random.Random(9)
    scores = [rng.random() for _ in range(400)]
    labels = [rng.random() > 0.5 for _ in range(400)]  # half wrong at every score
    assert review.conformal_threshold(scores, labels, alpha=0.03) is None


# Wiring

def test_prepare_and_record_week_go_through_injected_callables():
    saved = {}
    text = review.prepare_week(week=WEEK, seed=7, load_claims=lambda w: claims(), load_cards=lambda w: cards(),
                               save_sheet=lambda w, t: saved.update(sheet=(w, t)))
    assert saved["sheet"] == (WEEK, text)
    lines = list(csv.DictReader(io.StringIO(text)))
    seen = set()
    for line in lines:
        line["reviewer"] = "Thabang" if line["id"] in seen else "Jo"
        seen.add(line["id"])
        line["verdict"] = "supported" if line["kind"] == "claim" else "real"
        line["useful"] = "" if line["kind"] == "claim" else "yes"
    filled = sheet(*lines)
    stored = []
    s = review.record_week(filled, load_prior=lambda w: [], save_labels=stored.extend)
    assert len(stored) == len(lines)
    assert s["week"] == WEEK
    assert s["claims"]["n"] == 30 and s["cards"]["n"] == 20
    assert s["kappa"]["claims"] == {"n": 4, "value": None}
    assert s["kappa"]["cards"] == {"n": 2, "value": None}
    assert math.isclose(s["claims"]["upper_95"], 0.1)
    assert all(set(row) == {"who", "what", "reason"} for row in stored)
    assert [review.from_feedback(row)["id"] for row in stored] == [line["id"] for line in lines]


def test_feedback_encoding_round_trips_a_label():
    lab = label("claim", "c7", "contradicted", reviewer="Thapelo", market="KE", stratum="risk", double=True,
                non_english=True, language="sw", reason="post says the opposite")
    row = review.to_feedback(lab)
    assert set(row) == {"who", "what", "reason"}
    assert row["who"] == "Thapelo" and row["reason"] == "post says the opposite"
    what = json.loads(row["what"])
    assert set(what) == {"source", "week", "kind", "id", "market", "stratum", "double", "non_english", "language",
                         "verdict", "useful"}
    assert what["source"] == "42_review_v1"
    assert review.from_feedback(row) == lab
    card = label("card", "ZA-card1", "real", stratum="top3", useful="yes")
    assert review.from_feedback(review.to_feedback(card)) == card


def native_label(id_="xh-3", *, language="xh", gloss=True, tone=False, reviewer="Sipho", week=WEEK, reason=""):
    return {"week": week, "kind": "native", "id": id_, "language": language, "gloss_correct": gloss,
            "tone_correct": tone, "reviewer": reviewer, "reason": reason}


def test_feedback_encoding_round_trips_a_native_label():
    lab = native_label(reason="tone reads as sarcasm, labelled positive")
    row = review.to_feedback(lab)
    assert set(row) == {"who", "what", "reason"}
    assert row["who"] == "Sipho" and row["reason"] == "tone reads as sarcasm, labelled positive"
    what = json.loads(row["what"])
    assert what == {"source": "42_review_v1", "kind": "native", "week": WEEK, "id": "xh-3", "language": "xh",
                    "gloss_correct": True, "tone_correct": False}
    assert review.from_feedback(row) == lab


def test_a_native_label_read_from_the_sheet_encodes_as_native():
    sheet_label = review.read_native_sheet(native_filled([("zu", "yes", "no")]))[0]
    what = json.loads(review.to_feedback(sheet_label)["what"])
    assert what["kind"] == "native" and what["source"] == "42_review_v1"
    assert (what["id"], what["language"], what["gloss_correct"], what["tone_correct"]) == ("zu-0", "zu", True, False)
    back = review.from_feedback(review.to_feedback(sheet_label))
    assert review.score_native([back]) == review.score_native([sheet_label])


def foreign_rows():
    """Rows the feedback table also holds that are not ours: app taps, free text, other JSON."""
    return [
        {"who": "app", "what": "thumbs_up", "reason": ""},
        {"who": "Jo", "what": "this card is great", "reason": None},
        {"who": "app", "what": json.dumps({"kind": "claim", "id": "c1", "market": "ZA"}), "reason": ""},
        {"who": "app", "what": json.dumps({"source": "42_app_v1", "kind": "claim"}), "reason": ""},
        {"who": "app", "what": json.dumps(["42_review_v1"]), "reason": ""},
        {"who": "app", "what": None, "reason": ""},
        {"who": "app", "what": "", "reason": ""},
        {"who": "app", "what": "{not json", "reason": ""},
    ]


def test_review_labels_keeps_only_our_rows_and_skips_the_rest_without_raising():
    ours = [review.to_feedback(label("claim", f"p{i}", "supported", week="2026-W39")) for i in range(3)]
    native = [review.to_feedback(native_label())]
    rows = foreign_rows()[:2] + ours + foreign_rows()[2:] + native
    claims_back = review.review_labels(rows, {"claim", "card"})
    assert [lab["id"] for lab in claims_back] == ["p0", "p1", "p2"]
    assert all(lab["review_scope"] == "full" for lab in claims_back)
    assert review.review_labels(rows, {"native"}) == [native_label()]
    assert review.review_labels(foreign_rows(), {"claim", "card", "native"}) == []


def test_record_week_skips_foreign_and_native_rows_in_prior():
    prior_rows = ([review.to_feedback(label("claim", f"p{i}", "supported", week="2026-W39")) for i in range(30)]
                  + foreign_rows() + [review.to_feedback(native_label(week="2026-W39"))])
    lines = [{"week": WEEK, "kind": "claim", "id": f"c{i}", "market": "ZA", "stratum": "random",
              "review_scope": "full", "double": "false", "non_english": "false", "verdict": "supported",
              "reviewer": "Jo"} for i in range(30)]
    s = review.record_week(sheet(*lines), load_prior=lambda w: prior_rows, save_labels=[].extend)
    assert s["cumulative"]["weeks"] == ["2026-W39", WEEK]
    assert s["cumulative"]["n"] == 60


def test_record_native_week_stores_native_rows_and_scores_with_prior_weeks():
    prior_rows = ([review.to_feedback(native_label(f"xh-p{i}", week="2026-W39", tone=True)) for i in range(4)]
                  + foreign_rows()
                  + [review.to_feedback(label("claim", "p0", "supported", week="2026-W39"))])
    asked = []
    stored = []
    results = [("xh", "yes", "yes")] * 3 + [("xh", "no", "yes")]
    s = review.record_native_week(native_filled(results), load_prior=lambda w: asked.append(w) or prior_rows,
                                  save_labels=stored.extend)
    assert asked == [WEEK]
    assert len(stored) == 4 and all(set(row) == {"who", "what", "reason"} for row in stored)
    assert all(json.loads(row["what"])["kind"] == "native" for row in stored)
    assert [review.from_feedback(row)["id"] for row in stored] == [f"xh-{i}" for i in range(4)]
    xh = s["by_language"]["xh"]
    assert xh["n"] == 8
    assert xh["accuracy"] == pytest.approx(7 / 8)
    assert s["capped"] == list(review.NO_REVIEWER)


def test_record_week_stores_feedback_rows_and_reads_prior_weeks_back_from_them():
    prior_labels = [label("claim", f"p{i}", "supported", week="2026-W39") for i in range(30)]
    prior_rows = [review.to_feedback(lab) for lab in prior_labels]
    lines = [{"week": WEEK, "kind": "claim", "id": f"c{i}", "market": "ZA", "stratum": "random",
              "review_scope": "full", "double": "false", "non_english": "false", "verdict": "supported",
              "reviewer": "Jo"} for i in range(30)]
    stored = []
    s = review.record_week(sheet(*lines), load_prior=lambda w: prior_rows, save_labels=stored.extend)
    assert len(stored) == 30 and all(set(row) == {"who", "what", "reason"} for row in stored)
    assert s["cumulative"]["weeks"] == ["2026-W39", WEEK]
    assert s["cumulative"]["n"] == 60
