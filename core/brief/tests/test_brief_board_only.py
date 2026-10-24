"""A post sighted only on the YouTube trending board is marked to the writer and the critic (18 Oct 2026).

evidence.sql marks a post whose every sighting in the window was the board (post_observations.series board_youtube)
board_only; the record carries it as a model-only field, and the writer and the critic see the mark on the post and a
line naming such posts, as feed_evidence posts are named. Nothing about what counts as local changes: the board stays
a source market, the post stays local, and the stored card keeps its shape.
"""

from core.brief import explain
from core.brief.payload import MODEL_ONLY_FIELDS, reader_evidence
from core.brief.tests.test_brief_explain import CANDIDATE, W_END, W_START, heads, make_pack, outside_fences
from core.brief.tests.test_brief_explain_local import writer_heads

LEDGER = [{"id": "c1", "text": "Two creators post.", "label": "observed", "kind": "observation",
           "evidence_ids": ["tt_1", "ig_2"]}]


def board_pack():
    pack = make_pack()
    pack["evidence"][0]["board_only"] = True
    pack["evidence"][1]["board_only"] = False
    return pack


def test_the_writer_sees_the_mark_on_the_post_and_a_line_naming_board_only_posts():
    user = explain._writer_user(CANDIDATE, board_pack(), "ZA", W_START, W_END)
    marks = {h["id"]: h.get("board_only") for h in writer_heads(user)}
    assert marks == {"tt_1": True, "ig_2": None, "tt_3": None}  # marked only when true, like feed_evidence
    [scope] = [line for line in outside_fences(user).splitlines() if line.startswith("Posts you may cite")]
    assert ('Posts whose every sighting in the window was the YouTube trending board, marked board_only true: '
            '["tt_1"]; a board place shows the video ranked on South Africa\'s YouTube trending board, not a creator '
            "posting in South Africa's feeds. They stay local by the rules above and are cited for what their own text and "
            "fields show.") in scope
    # The mark changes nothing about what is local or citable.
    assert 'marked local true: ["tt_1", "ig_2", "tt_3"]' in scope and 'marked citable true: ["tt_1", "ig_2", "tt_3"]' in scope


def test_the_writer_gets_no_board_line_when_no_post_is_board_only():
    user = explain._writer_user(CANDIDATE, make_pack(), "ZA", W_START, W_END)
    assert "board_only" not in user


def test_the_critic_sees_the_mark_on_every_post_and_the_same_line():
    prompt = explain._critic_user(CANDIDATE, "ZA", "A sentence.", LEDGER, board_pack(), ("c1",))
    assert {h["id"]: h["board_only"] for h in heads(prompt)} == {"tt_1": True, "ig_2": False, "tt_3": False}
    blocks = outside_fences(prompt).split("\n\n")
    [line] = [b for b in blocks if b.startswith("Posts whose every sighting in the window was the YouTube trending board")]
    assert 'marked board_only true: ["tt_1"]' in line and "not a creator posting in South Africa's feeds" in line
    plain = outside_fences(explain._critic_user(CANDIDATE, "ZA", "A sentence.", LEDGER, make_pack(), ("c1",)))
    assert "marked board_only true: []" in plain  # the line is always there for the critic, as the own-feed line is


def test_the_support_check_carries_the_mark_and_the_stored_card_keeps_its_shape():
    # 22 Oct 2026, board-line-feed-wording: the support check sees the mark the writer and the critic see, on the
    # marked post only, so it can judge the board wording the line hands the writer.
    pack = board_pack()
    prompt = explain._support_user(LEDGER[0], pack["evidence"][:2], pack, "ZA")
    assert prompt.count('"board_only": true') == 1 and '"board_only": false' not in prompt
    assert "board_only" in MODEL_ONLY_FIELDS
    assert not any("board_only" in e for e in reader_evidence(pack["evidence"]))
