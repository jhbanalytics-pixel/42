"""core/eval/reference_eval.py: matching, best-place outcomes per label, the date window, not_checked when a market has
no brief, and the bundled reference cases' shape."""
import json

from core.eval import reference_eval as re_


def _case(label="positive", tags=(), terms=(), market="ZA", week="2026-10-12"):
    return {"id": "c", "market": market, "week": week, "label": label, "kind": "k",
            "match": {"tags": list(tags), "terms": [list(t) for t in terms]}}


def _brief(market="ZA", day="2026-10-19", cards=(), held=(), moments=()):
    return {"market": market, "brief_date": day,
            "payload": json.dumps({"cards": list(cards), "more": [], "held_back": {"count": len(held), "items": list(held)},
                                   "moments": list(moments)})}


def test_tag_matches_hashtag_title_key_and_merged_ids():
    c = _case(tags=["amapianochallenge"])
    assert re_.matches(c, ["#AmapianoChallenge"])
    assert re_.matches(c, ["hashtag:tiktok:amapianochallenge2026"])
    assert re_.matches(c, ["something else", "amapiano_challenge"])
    assert not re_.matches(c, ["amapiano"])


def test_short_tag_needs_a_whole_word():
    c = _case(tags=["cng"])
    assert re_.matches(c, ["CNG vehicles push"])
    assert not re_.matches(c, ["dancing challenge"])


def test_term_group_needs_every_word():
    c = _case(terms=[["nollywood", "part 2"]])
    assert re_.matches(c, ["Nollywood clips, Part 2 cliffhangers"])
    assert not re_.matches(c, ["Nollywood premiere"])
    assert not re_.matches(c, ["part 23 nollywood"])


def test_positive_outcomes_take_the_best_place_across_briefs():
    c = _case(tags=["thecluster"], market="KE")
    held = {"item_id": "hashtag:tiktok:thecluster", "title": "#thecluster", "rule": "G10", "reason": "not_confirmed"}
    card = {"item_id": "hashtag:tiktok:theclusterke", "title": "#theclusterke"}
    briefs = [_brief("KE", "2026-10-16", held=[held]), _brief("KE", "2026-10-20", cards=[card])]
    [r] = re_.score([c], briefs)
    assert r["outcome"] == "caught" and r["match"]["brief_date"] == "2026-10-20"
    [r] = re_.score([c], briefs[:1])
    assert r["outcome"] == "held" and r["match"]["rule"] == "G10" and r["match"]["reason"] == "not_confirmed"
    [r] = re_.score([c], [_brief("KE", "2026-10-16")])
    assert r["outcome"] == "missed"


def test_negative_outcomes_and_moments():
    c = _case(label="negative", tags=["ebola"], market="KE")
    assert re_.score([c], [_brief("KE", cards=[{"item_id": "x", "title": "Ebola in Kenya"}])])[0]["outcome"] == \
        "false_publication"
    assert re_.score([c], [_brief("KE", held=[{"item_id": "x", "title": "#ebola"}])])[0]["outcome"] == "held"
    moment = {"name": "Ebola briefing", "item_ids": ["topic:1"]}
    assert re_.score([c], [_brief("KE", moments=[moment])])[0]["outcome"] == "moment"
    assert re_.score([c], [_brief("KE")])[0]["outcome"] == "absent"


def test_other_markets_and_dates_outside_the_window_do_not_count():
    c = _case(tags=["amapianochallenge"])
    card = [{"item_id": "hashtag:tiktok:amapianochallenge", "title": "#amapianochallenge"}]
    assert re_.score([c], [_brief("NG", cards=card)])[0]["outcome"] == "not_checked"
    assert re_.score([c], [_brief("ZA", "2026-10-26", cards=card)])[0]["outcome"] == "not_checked"
    assert re_.score([c], [_brief("ZA", "2026-10-25", cards=card)])[0]["outcome"] == "caught"
    assert re_.score([c], [_brief("ZA", "2026-10-11", cards=card)])[0]["outcome"] == "not_checked"


def test_summary_keeps_errors_separate_with_denominators():
    rows = [{"label": "positive", "outcome": "missed"}, {"label": "positive", "outcome": "not_checked"},
            {"label": "negative", "outcome": "false_publication"}, {"label": "negative", "outcome": "held"}]
    s = re_.summary(rows)
    assert s["positive"] == {"cases": 2, "checked": 1, "outcomes": {"missed": 1, "not_checked": 1}}
    assert s["negative"]["checked"] == 2 and s["negative"]["outcomes"]["false_publication"] == 1


def test_report_hides_titles_unless_asked(tmp_path, capsys):
    c = _case(tags=["amapianochallenge"])
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [c]}))
    briefs = tmp_path / "briefs.json"
    briefs.write_text(json.dumps([_brief(cards=[{"item_id": "k", "title": "Secret title #amapianochallenge"}])]))
    re_.main(["--cases", str(path), "--briefs", str(briefs)])
    assert "Secret title" not in capsys.readouterr().out
    re_.main(["--cases", str(path), "--briefs", str(briefs), "--json"])
    assert "Secret title" not in capsys.readouterr().out
    re_.main(["--cases", str(path), "--briefs", str(briefs), "--show-titles"])
    assert "Secret title" in capsys.readouterr().out


def test_bundled_cases_are_allowlisted_and_well_formed():
    cases = re_.load_cases()
    assert len(cases) == 14 and len({c["id"] for c in cases}) == 14
    assert sum(c["label"] == "positive" for c in cases) == 3
    allowed = {"id", "week", "market", "first_seen", "label", "kind", "what", "evidence", "match"}
    for c in cases:
        assert set(c) == allowed and c["market"] in ("ZA", "NG", "KE") and c["label"] in ("positive", "negative")
        assert c["match"]["tags"] or c["match"]["terms"]
        assert "http" not in json.dumps(c) and "@" not in json.dumps(c)
