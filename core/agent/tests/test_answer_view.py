"""The reader's view of a stored Ask answer: comments told apart from posts, one claim per fact, gaps in plain words.

The fixture is shaped like a live answer (three posts that share one hashtag line, four comments whose URL is the
parent video's, seven claims that restate one fact, nine gaps with a repeat) with invented handles."""

import copy
import json
import re
from pathlib import Path

import pytest

from core.agent import answer_view

FIXTURE = Path(__file__).resolve().parents[2] / "api" / "fixtures" / "ask_comments_repeat.json"
INTERNAL = re.compile(r"[a-z]+/[a-z]+/[a-z]+|_comment_|stored evidence|Why: error|Searched the |the what to watch next text",
                      re.I)


@pytest.fixture
def record():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def restating(record):
    """The stored fixture with c1 and c7 saying c2 again: the same quoted posts and words, the same figures and queries.
    c3, c4 and c6 stay three different creators' posts with their own view counts, and c5 stays the comments."""
    claims = {c["id"]: c for c in record["answer"]["claims"]}
    claims["c1"]["numbers"] = copy.deepcopy(claims["c2"]["numbers"])
    claims["c7"]["quotes"] = copy.deepcopy(claims["c2"]["quotes"])
    claims["c7"]["numbers"] = copy.deepcopy(claims["c2"]["numbers"])
    return record


def test_comment_is_told_from_post_by_its_id(record):
    posts = [e for e in record["answer"]["evidence"] if not answer_view.is_comment(e)]
    comments = [e for e in record["answer"]["evidence"] if answer_view.is_comment(e)]
    assert [e["id"] for e in posts] == ["tt_post_1", "tt_post_2", "tt_post_3"]
    assert len(comments) == 4


def test_comment_names_the_author_of_the_post_it_sits_under(record):
    by_id = {e["id"]: e for e in record["answer"]["evidence"]}
    assert answer_view.comment_parent_handle(by_id["tiktok_comment_770200000000000004"]) == "ngwenya.tales"
    assert answer_view.comment_parent_handle(by_id["tiktok_comment_770200000000000006"]) == "lerato_clips_22"
    other = {"id": "instagram_comment_9", "platform": "instagram", "url": "https://www.instagram.com/p/Cx1/"}
    assert answer_view.is_comment(other)
    assert answer_view.comment_parent_handle(other) is None


def test_repeated_claims_become_one_and_the_distinct_claims_stay(record):
    restating(record)
    shown = answer_view.present(record)["answer"]
    assert [c["id"] for c in shown["claims"]] == ["c2", "c3", "c4", "c5", "c6"]
    original = {c["id"]: c for c in record["answer"]["claims"]}
    for claim in shown["claims"]:
        assert claim == original[claim["id"]]  # a kept claim is never reworded, widened or re-cited


def test_three_creators_with_the_same_hashtags_are_three_findings_i2(record):
    shown = answer_view.present(record)["answer"]
    by_id = {c["id"]: c for c in shown["claims"]}
    assert {"c3", "c4", "c6"} <= set(by_id)
    views = {cid: [n["value"] for n in by_id[cid]["numbers"] if n["unit"] == "views"] for cid in ("c3", "c4", "c6")}
    assert views == {"c3": [460457], "c4": [131988], "c6": [232717]}


def test_merge_keeps_the_stronger_label_first(record):
    restating(record)
    claims = record["answer"]["claims"]
    claims[1]["label"] = "inferred"
    claims[6]["label"] = "corroborated"
    kept = [c["id"] for c in answer_view.present(record)["answer"]["claims"]]
    assert kept == ["c3", "c4", "c5", "c6", "c7"]


def test_claims_with_other_words_or_a_proposal_are_not_merged(record):
    restating(record)
    claims = record["answer"]["claims"]
    claims[0]["quotes"] = [{"evidence_id": q["evidence_id"], "text": q["text"] + " tonight"} for q in claims[0]["quotes"]]
    claims[6].update(kind="proposal", basis="three posts", falsifier="a fourth post without the tags")
    kept = [c["id"] for c in answer_view.present(record)["answer"]["claims"]]
    assert kept == ["c1", "c2", "c3", "c4", "c5", "c6", "c7"]


def test_dropped_claim_ids_are_mapped_in_the_lines_that_refer_to_them(record):
    restating(record)
    record["answer"]["so_what"] = [{"text": "Use the shared tags.", "claim_ids": ["c1", "c4"]},
                                   {"text": "Watch the tags.", "claim_ids": ["c5", "c7"]}]
    record["answer"]["watch_next"] = [{"text": "Check again Friday.", "claim_ids": ["c7"], "forecast": False}]
    shown = answer_view.present(record)["answer"]
    assert [s["claim_ids"] for s in shown["so_what"]] == [["c2", "c4"], ["c5", "c2"]]
    assert shown["watch_next"][0]["claim_ids"] == ["c2"]


def test_present_does_not_change_the_stored_record_and_is_stable(record):
    before = copy.deepcopy(record)
    once = answer_view.present(record)
    assert record == before
    assert answer_view.present(once) == once


def test_gaps_read_in_plain_words_once_each(record):
    shown = answer_view.present(record)
    gaps = shown["answer"]["gaps"]
    text = " ".join(" ".join(str(v) for v in g.values()) for g in gaps)
    assert not INTERNAL.search(text), text
    whats = [g["what"] for g in gaps]
    assert len(whats) == len(set(whats))
    assert "We could not read the video's spoken words this time" in whats
    assert sum("forecast" in w.lower() for w in whats) == 1
    assert len(gaps) == 7  # nine stored lines, one repeat dropped, the two removal lines said as one


def test_the_full_technical_text_is_kept_for_the_technical_details(record):
    shown = answer_view.present(record)
    technical = shown["run"]["technical_gaps"]
    assert any(g["searched"] == "tiktok/post/transcript" and g["why"] == "error" for g in technical)
    assert any("tiktok_comment_77020000000000001" in g["searched"] for g in technical)
    assert len(technical) == 8  # the repeat is dropped, nothing else


def test_followups_name_no_route(record):
    shown = answer_view.present(record)
    assert not any(re.search(r"[a-z]+/[a-z]+", f) for f in shown["run"]["followups"])
    assert "Which creators posted #funnyclip in South Africa this week?" in shown["run"]["followups"]


def test_short_answer_is_not_touched_by_the_view(record):
    assert answer_view.present(record)["answer"]["short_answer"] == record["answer"]["short_answer"]


def test_evidence_only_a_merged_claim_cited_leaves_the_view(record):
    restating(record)
    record["answer"]["evidence"].append(dict(record["answer"]["evidence"][0], id="tt_post_9", text="only c7 cites me"))
    record["answer"]["claims"][6]["evidence_ids"].append("tt_post_9")  # c7 is the inferred repeat, so it goes
    shown = answer_view.present(record)["answer"]
    cited = {eid for claim in shown["claims"] for eid in claim["evidence_ids"]}
    assert {e["id"] for e in shown["evidence"]} == cited
    assert "tt_post_9" not in cited
    assert len(shown["evidence"]) == 7  # three posts and four comments, each cited by a claim that stays


# Release C review I2: one fact is the same posts, the same quoted words and the same figures from the same queries.
def number(value, unit, query_id):
    return {"value": value, "unit": unit, "query_id": query_id, "run_id": "r_1", "result_hash": "sha256:" + "ab" * 32}


def post(pid, handle, text):
    return {"id": pid, "platform": "tiktok", "url": f"https://www.tiktok.com/@{handle}/video/{pid}", "handle": handle,
            "text": text}


def claim(cid, text, evidence_ids, quote_text, numbers, kind="observation", label="single_source"):
    return {"id": cid, "text": text, "label": label, "kind": kind, "evidence_ids": evidence_ids,
            "quotes": [{"evidence_id": eid, "text": quote_text} for eid in evidence_ids], "numbers": numbers}


def amapiano_gqom():
    """Two distinct findings that quote one post: one about amapiano (41 posts), one about gqom (12 posts)."""
    return {"ask_id": "a_1", "answer": {
        "evidence": [post("p1", "dj_one", "amapiano and gqom this week")],
        "claims": [claim("c1", "Amapiano posts are everywhere this week", ["p1"], "amapiano and gqom this week",
                         [number(41, "posts", "q_1")]),
                   claim("c2", "Gqom is rising beside it in Durban", ["p1"], "amapiano and gqom this week",
                         [number(12, "posts", "q_2")])],
        "so_what": [{"text": "Gqom promoters in Durban have an opening", "claim_ids": ["c2"]}],
        "watch_next": [], "gaps": []}, "run": {"followups": []}}


def restated():
    """c2 says c1 again with more words: the same post, quote, figures and query."""
    record = amapiano_gqom()
    record["answer"]["claims"][1].update(text="Amapiano posts are everywhere in South Africa this week",
                                         numbers=json.loads(json.dumps(record["answer"]["claims"][0]["numbers"])))
    return record


def test_two_findings_that_quote_one_post_but_carry_other_figures_stay_two_i2():
    shown = answer_view.present(amapiano_gqom())["answer"]
    assert [c["id"] for c in shown["claims"]] == ["c1", "c2"]
    assert shown["claims"][1]["numbers"][0]["value"] == 12
    assert shown["so_what"][0]["claim_ids"] == ["c2"]  # never re-pointed at a claim about something else


def test_the_same_words_on_different_posts_are_different_facts_i2():
    record = {"ask_id": "a_1", "answer": {
        "evidence": [post("p1", "a_one", "#tag #tag2"), post("p2", "b_two", "#tag #tag2")],
        "claims": [claim("c1", "One creator used the tags", ["p1"], "#tag #tag2", []),
                   claim("c2", "Another creator used the tags", ["p2"], "#tag #tag2", [])],
        "so_what": [], "watch_next": [], "gaps": []}, "run": {"followups": []}}
    assert [c["id"] for c in answer_view.present(record)["answer"]["claims"]] == ["c1", "c2"]


def test_the_same_post_and_words_with_other_figures_or_another_query_are_not_one_fact_i2():
    base = amapiano_gqom()
    base["answer"]["claims"][1]["numbers"] = [number(41, "posts", "q_9")]  # same value and unit, another query
    assert len(answer_view.present(base)["answer"]["claims"]) == 2
    base["answer"]["claims"][1]["numbers"] = [number(41, "views", "q_1")]  # same value, another unit
    assert len(answer_view.present(base)["answer"]["claims"]) == 2


def test_a_claim_the_ranked_list_points_at_is_never_merged_i2():
    record = restated()
    assert [c["id"] for c in answer_view.present(copy.deepcopy(record))["answer"]["claims"]] == ["c2"]  # a true repeat
    record["run"]["ranked_list"] = {"platform": "tiktok", "items": [
        {"claim_id": "c1", "usage_handle": "dj_one", "tied_with_previous": False},
        {"claim_id": "c2", "usage_handle": "dj_one", "tied_with_previous": False}]}
    assert [c["id"] for c in answer_view.present(record)["answer"]["claims"]] == ["c1", "c2"]


def test_a_repeat_with_the_same_figures_still_folds_and_the_line_follows_it_i2():
    record = restated()
    record["answer"]["so_what"] = [{"text": "Use it.", "claim_ids": ["c1"]}]
    shown = answer_view.present(record)["answer"]
    assert [c["id"] for c in shown["claims"]] == ["c2"]
    assert shown["so_what"][0]["claim_ids"] == ["c2"]
    assert shown["claims"][0]["numbers"] == record["answer"]["claims"][0]["numbers"]  # the kept claim has the same figures


def test_a_claim_that_quotes_a_comment_is_keyed_by_the_comment_too_i2():
    record = {"ask_id": "a_1", "answer": {
        "evidence": [post("p1", "a_one", "words"),
                     {"id": "tiktok_comment_1", "platform": "tiktok", "url": "https://www.tiktok.com/@a_one/video/p1",
                      "text": "first remark"},
                     {"id": "tiktok_comment_2", "platform": "tiktok", "url": "https://www.tiktok.com/@a_one/video/p1",
                      "text": "other remark"}],
        "claims": [dict(claim("c1", "People said one thing", ["p1"], "words", []),
                        quotes=[{"evidence_id": "p1", "text": "words"}, {"evidence_id": "tiktok_comment_1", "text": "first remark"}]),
                   dict(claim("c2", "People said another", ["p1"], "words", []),
                        quotes=[{"evidence_id": "p1", "text": "words"}, {"evidence_id": "tiktok_comment_2", "text": "other remark"}])],
        "so_what": [], "watch_next": [], "gaps": []}, "run": {"followups": []}}
    assert [c["id"] for c in answer_view.present(record)["answer"]["claims"]] == ["c1", "c2"]


# Release C review m7: two mutants of the gap view survived the first tests.
def route_record(gaps):
    return {"ask_id": "a_1", "answer": {"evidence": [], "claims": [], "so_what": [], "watch_next": [], "gaps": gaps},
            "run": {"followups": []}}


def test_a_reworded_route_gap_shows_a_status_word_or_nothing_never_the_stored_reason_m4():
    gaps = [{"what": "Transcript of a TikTok post not fetched", "searched": "tiktok/post/transcript",
             "why": "HTTP 500 from host 10.0.0.7 after 3 tries"},
            {"what": "Comments on a post not fetched", "searched": "tiktok/post/comments", "why": "rate_limited"}]
    shown = answer_view.present(route_record(gaps))["answer"]["gaps"]
    assert [g["why"] for g in shown] == ["", "rate_limited"]
    assert [g["searched"] for g in shown] == ["", ""]


def test_gaps_that_already_read_in_words_add_no_technical_details_m3():
    gaps = [{"what": "Original sound titles for the three TikTok posts", "searched": "Inspected audio metadata",
             "why": "The platform records only numeric sound identifiers"}]
    shown = answer_view.present(route_record(gaps))
    assert shown["answer"]["gaps"] == gaps
    assert "technical_gaps" not in shown["run"]
    assert answer_view.present(route_record([]))["run"] == {"followups": []}


def test_reworded_gaps_keep_their_stored_text_in_the_technical_details_once_m3():
    gaps = [{"what": "Transcript of a TikTok post not fetched", "searched": "tiktok/post/transcript", "why": "error"}] * 2
    shown = answer_view.present(route_record(gaps))
    assert shown["run"]["technical_gaps"] == gaps[:1]
    assert answer_view.present(shown)["run"]["technical_gaps"] == gaps[:1]  # a second view keeps the first one's text
