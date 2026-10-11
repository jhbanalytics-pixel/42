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


def test_repeated_claims_become_one_and_the_distinct_claim_stays(record):
    shown = answer_view.present(record)["answer"]
    assert [c["id"] for c in shown["claims"]] == ["c2", "c5"]
    original = {c["id"]: c for c in record["answer"]["claims"]}
    for claim in shown["claims"]:
        assert claim == original[claim["id"]]  # a kept claim is never reworded, widened or re-cited


def test_merge_keeps_the_stronger_label_first(record):
    claims = record["answer"]["claims"]
    claims[1]["label"] = "inferred"
    claims[6]["label"] = "corroborated"
    kept = [c["id"] for c in answer_view.present(record)["answer"]["claims"]]
    assert kept == ["c5", "c7"]


def test_claims_with_different_posts_or_a_proposal_are_not_merged(record):
    claims = record["answer"]["claims"]
    claims[3]["quotes"] = [{"evidence_id": "tt_post_2", "text": "#funnyclip #skits #laughs"}]
    claims[3]["evidence_ids"] = ["tt_post_2"]
    record["answer"]["evidence"][1]["text"] = "#funnyclip #skits #laughs tonight"
    claims[3]["quotes"][0]["text"] = "#funnyclip #skits #laughs tonight"
    claims[6].update(kind="proposal", basis="three posts", falsifier="a fourth post without the tags")
    kept = [c["id"] for c in answer_view.present(record)["answer"]["claims"]]
    assert kept == ["c2", "c4", "c5", "c7"]


def test_dropped_claim_ids_are_mapped_in_the_lines_that_refer_to_them(record):
    record["answer"]["so_what"] = [{"text": "Use the shared tags.", "claim_ids": ["c1", "c4"]},
                                   {"text": "Watch the tags.", "claim_ids": ["c5", "c6"]}]
    record["answer"]["watch_next"] = [{"text": "Check again Friday.", "claim_ids": ["c7"], "forecast": False}]
    shown = answer_view.present(record)["answer"]
    assert [s["claim_ids"] for s in shown["so_what"]] == [["c2"], ["c5", "c2"]]
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
    shown = answer_view.present(record)["answer"]
    cited = {eid for claim in shown["claims"] for eid in claim["evidence_ids"]}
    assert {e["id"] for e in shown["evidence"]} == cited
    assert len(shown["evidence"]) == 5  # three posts and the two comments the surviving claim quotes
