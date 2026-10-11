"""Release C review 2 finding m3: the reader view keeps the post a kept comment sits under, after claims merge.

A comment's post can be in the answer only because the comment cites it (the record's evidence_links names the pair).
The merge rebuilds the evidence from the claims that stay; it keeps the posts those comments' links name, as the
writer's and the critic's prunes do, so the page and the export still show the post beside the comment."""
import copy

from core.agent import answer_view


def post(pid, handle="dj_one", text="amapiano and gqom this week"):
    return {"id": pid, "platform": "tiktok", "url": f"https://www.tiktok.com/@{handle}/video/{pid}", "handle": handle,
            "text": text}


COMMENT = "tiktok_comment_1"


def claim(cid, text, evidence_ids, quote_id, quote_text, kind="observation", label="single_source"):
    return {"id": cid, "text": text, "label": label, "kind": kind, "evidence_ids": evidence_ids,
            "quotes": [{"evidence_id": quote_id, "text": quote_text}], "numbers": []}


def merging_record(links):
    """c1 and c2 say one thing twice and merge. c3 cites a comment whose post, p9, no claim cites."""
    record = {"ask_id": "a_1", "answer": {
        "evidence": [post("p1"), post("p9", "dj_two", "the post under the comment"),
                     {"id": COMMENT, "platform": "tiktok", "url": "https://www.tiktok.com/@dj_two/video/p9",
                      "handle": "fan_one", "text": "a remark"}],
        "claims": [claim("c1", "Amapiano is everywhere", ["p1"], "p1", "amapiano and gqom this week"),
                   claim("c2", "Amapiano is everywhere in South Africa", ["p1"], "p1", "amapiano and gqom this week",
                         label="observed"),
                   claim("c3", "A fan remarked on it", [COMMENT], COMMENT, "a remark")],
        "so_what": [], "watch_next": [], "gaps": []}, "run": {"followups": []}}
    if links is not None:
        record["evidence_links"] = links
    return record


def ids(record):
    return [e["id"] for e in record["answer"]["evidence"]]


def test_after_a_merge_the_post_a_kept_comment_sits_under_stays_in_the_view_m3():
    shown = answer_view.present(merging_record({"v": 1, "parents": {COMMENT: "p9"}}))
    assert [c["id"] for c in shown["answer"]["claims"]] == ["c2", "c3"]  # c1 merged into c2
    assert ids(shown) == ["p1", "p9", COMMENT]
    assert shown["evidence_links"] == {"v": 1, "parents": {COMMENT: "p9"}}  # the link still names a post that shows


def test_a_post_only_a_merged_claim_cited_still_leaves_the_view_m3():
    record = merging_record({"v": 1, "parents": {COMMENT: "p9"}})
    record["answer"]["evidence"].append(post("p5", "dj_three", "only c1 cites me"))
    record["answer"]["claims"][0]["evidence_ids"].append("p5")
    assert "p5" not in ids(answer_view.present(record))


def test_without_a_link_the_merge_prunes_as_before_m3():
    assert ids(answer_view.present(merging_record(None))) == ["p1", COMMENT]


def test_a_link_in_another_shape_is_not_followed_m3():
    for links in ({"v": 2, "parents": {COMMENT: "p9"}}, {"v": 1, "parents": {COMMENT: 9}}, {"parents": {COMMENT: "p9"}},
                  {"v": 1, "parents": {COMMENT: "p9"}, "extra": 1}, "p9", {"v": 1, "parents": []}):
        assert "p9" not in ids(answer_view.present(merging_record(links))), links


def test_a_link_names_a_post_only_for_a_comment_that_stays_m3():
    record = merging_record({"v": 1, "parents": {COMMENT: "p9", "tiktok_comment_2": "p5"}})
    record["answer"]["evidence"].append(post("p5", "dj_three", "under a comment no claim cites"))
    assert ids(answer_view.present(record)) == ["p1", "p9", COMMENT]


def test_the_stored_record_is_not_changed_and_the_view_is_stable_m3():
    record = merging_record({"v": 1, "parents": {COMMENT: "p9"}})
    before = copy.deepcopy(record)
    shown = answer_view.present(record)
    assert record == before
    assert answer_view.present(shown) == shown
