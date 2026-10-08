import copy
import json
import re

from ops import pair_score_compare as compare


def test_the_fixture_has_at_least_40_labelled_pairs_each_with_a_reason():
    pairs = compare.build_fixture()["pairs"]
    assert len(pairs) >= 40
    assert len({p["id"] for p in pairs}) == len(pairs)
    assert {p["label"] for p in pairs} == {"true", "false"}
    assert all(p["why"] for p in pairs)
    assert min(sum(p["label"] == k for p in pairs) for k in ("true", "false")) >= 15


def test_the_review_case_is_accepted_by_the_vote_rule_and_refused_by_the_shadow():
    rows = compare.evaluate(compare.build_fixture())
    summary = compare.summarise(rows)
    refused = summary["today_accepts_shadow_rejects"]
    assert refused and all(r["today_accept"] and not r["shadow_accept"] for r in refused)
    generic_recent = [r for r in refused if r["today_votes"] == ["hashtag_or_sound", "recent"] and r["cosine"] < 0.65]
    assert len(generic_recent) >= 4
    assert all(r["label"] == "false" and r["shadow_reason"] == "cosine_floor" for r in generic_recent)
    assert summary["today_accepts_shadow_rejects_vote_sets"]["hashtag_or_sound+recent"] >= 4


def test_the_reverse_set_holds_a_cosine_only_recurrence():
    summary = compare.summarise(compare.evaluate(compare.build_fixture()))
    reverse = summary["shadow_accepts_today_rejects"]
    assert any(r["label"] == "true" and r["today_votes"] == ["cosine"] and r["days_since_seen"] >= 28 for r in reverse)


def test_the_counts_add_up():
    s = compare.summarise(compare.evaluate(compare.build_fixture()))
    assert s["agree"] == s["pairs"] - len(s["today_accepts_shadow_rejects"]) - len(s["shadow_accepts_today_rejects"])
    for rule in ("today", "shadow"):
        c = s[rule]
        assert c["tp"] + c["fp"] + c["fn"] + c["tn"] == s["pairs"]
        assert c["tp"] + c["fn"] == s["true"] and c["fp"] + c["tn"] == s["false"]


def test_the_shadow_score_of_every_pair_ignores_when_the_item_was_last_seen():
    data = compare.build_fixture()
    scores = {}
    for days in (1, 90):
        shifted = copy.deepcopy(data)
        for p in shifted["pairs"]:
            p["item"]["last_seen"] = (compare.RUN_DATE - compare.timedelta(days=days)).isoformat()
        scores[days] = [(r["id"], r["shadow_score"], r["shadow_accept"]) for r in compare.evaluate(shifted)]
    assert scores[1] == scores[90]


def test_the_report_is_written_deterministically_and_holds_no_dash_runs(tmp_path):
    for name in ("a", "b"):
        assert compare.main(["pair_score_compare", str(tmp_path / name)]) == 0
    for f in ("pair_score_comparison.json", "pair_score_comparison.md"):
        assert (tmp_path / "a" / f).read_bytes() == (tmp_path / "b" / f).read_bytes()
    text = (tmp_path / "a" / "pair_score_comparison.md").read_text(encoding="utf-8")
    bars = "|" + "-" * 3  # a markdown table separator is the one place a run of hyphens is allowed
    assert not re.search("[" + chr(0x2013) + chr(0x2014) + "]|" + "-" * 2, text.replace(bars, ""))
    assert json.loads((tmp_path / "a" / "pair_score_comparison.json").read_text(encoding="utf-8"))["summary"]["pairs"] >= 40
