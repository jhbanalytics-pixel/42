"""The critic judges on the evidence the writer saw (22 Oct 2026 zero-card review, EVIDENCE C1, CRITIC D1 to D3).

Fakes only: no model, no cloud.
"""

import json
from datetime import datetime, timezone

from core.brief import evidence, explain
from core.brief.payload import MODEL_ONLY_FIELDS, reader_evidence
from core.brief.tests.test_brief_explain import CANDIDATE, FakeModel, W_END, W_START, good, make_pack, run


def dance_post(i, market, day):
    return {"id": f"p{i}", "platform": "tiktok", "creator_id": f"c{i}", "handle": f"h{i}", "market": market,
            "posted_at": f"2026-10-{day}T10:00:00+02:00", "formats": ["dance"], "sound_id": None, "flags": [],
            "text": f"dance post {i}"}


def critic_line(user):
    """The pattern line block of a critic prompt: its heading line and the JSON lines after it."""
    block = next(p for p in user.split("\n\n") if p.startswith("Shared sounds and formats"))
    return block.split("\n", 1)[1]


# EVIDENCE C1, converted from EVIDENCE-repro_test.py: the critic once counted posts the writer may not cite.
def test_the_critics_pattern_line_counts_only_the_posts_the_writer_may_cite():
    pack = {"evidence": [dance_post(1, "ZA", 20), dance_post(2, "NG", 21), dance_post(3, "NG", 21)],
            "numbers": [], "facts": []}
    letters = explain._sound_letters(pack)
    citable = [r for r in pack["evidence"] if explain._citable(r, "ZA")]
    assert explain._shared_patterns({"evidence": citable}, letters) == []  # the writer is told: none
    user = explain._critic_user(CANDIDATE, "ZA", "s", [], pack, ())
    assert critic_line(user) == "none"
    assert "over the 1 posts shown here that the writer may cite, posts located in another market left out" in user
    # The posts located in another market are still listed for the critic to read, below the line.
    assert "post p2 " in user and "post p3 " in user


def test_the_critic_and_the_writer_read_the_same_count_over_the_citable_posts():
    pack = {"evidence": [dance_post(1, "ZA", 20), dance_post(2, "ZA", 21), dance_post(3, "NG", 21),
                         dance_post(4, None, 22)], "numbers": [], "facts": []}
    writer = explain._writer_user(CANDIDATE, pack, "ZA", W_START, W_END)
    critic = explain._critic_user(CANDIDATE, "ZA", "s", [], pack, ())
    shared = explain._critic_patterns(pack, "ZA")
    assert shared == [{"pattern": "format dance", "post_ids": ["p1", "p2", "p4"], "creators": 3, "days": 3}]
    line = json.dumps(shared[0], ensure_ascii=False)
    assert critic_line(critic) == line
    assert line in writer


def test_the_critic_system_prompt_scopes_its_counted_line_to_the_citable_posts():
    assert "among the posts shown here that the writer may cite" in explain.CRITIC_SYSTEM
    assert "It counts only those posts, never the whole trend" in explain.CRITIC_SYSTEM


# CRITIC D1: the pattern line the critic saw is kept beside code_rivals.
def test_the_pattern_line_the_critic_read_is_recorded_beside_its_answer():
    pack = make_pack()
    for r, day in zip(pack["evidence"], (25, 26, 27)):
        r.update(formats=["dance"], creator_id=r["id"], posted_at=f"2026-09-{day}T08:10:00+02:00")
    pack["evidence"].append({**pack["evidence"][0], "id": "ng_9", "market": "NG", "creator_id": "ng"})
    model = FakeModel([good()])
    result = run(model, pack=pack)
    [critic_call] = model.critic_calls()
    seen = result["critic"]["patterns_seen"]
    assert seen == [{"pattern": "format dance", "post_ids": ["tt_1", "ig_2", "tt_3"], "creators": 3, "days": 3}]
    assert critic_line(critic_call["user"]) == json.dumps(seen[0], ensure_ascii=False)
    [row] = [c for c in result["checks"] if c["rule"] == "critic"]
    assert row["patterns_seen"] == seen
    # A shadow record: the decision is the one the critic made.
    assert result["reason"] is None and row["verdict"] == "pass"


def test_a_fault_in_the_pattern_record_is_kept_as_its_exception_type(monkeypatch):
    def boom(pack, market):
        raise ValueError("x")
    monkeypatch.setattr(explain, "_critic_patterns", boom)
    result = run(FakeModel([good()]))
    assert result["critic"]["patterns_seen"] == {"error": "ValueError"}
    assert result["reason"] is None


# CRITIC D2: lane and cluster membership reach the writer and the critic as model-only fields.
def record_row(**kw):
    row = {"post_id": "p1", "platform": "tiktok", "handle": "@a", "url": "https://x/p1", "quote_text": "caption",
           "published_at": datetime(2026, 10, 20, 8, tzinfo=timezone.utc), "geo_market": "ZA", "geo_confidence": 0.9,
           "geo_source": "ext_region", "source_market": "ZA", "views": 1, "likes": 0, "comments": 0, "shares": 0,
           "flagged": False, "sponsored": False, "near_dup": False, "thumbnail_url": None, "duration_s": None,
           "creator_tier": "micro", "sponsor_checked": False, "hashtags": [], "creator_id": "c1"}
    row.update(kw)
    return row


def test_the_record_carries_measured_and_market_member_from_the_pack_row():
    tz = timezone.utc
    on = evidence._record(record_row(measured=True, market_member=True), tz)
    off = evidence._record(record_row(measured=False, market_member=None), tz)
    old = evidence._record(record_row(), tz)  # a statement without the columns
    assert (on["measured"], on["market_member"]) == (True, True)
    assert (off["measured"], off["market_member"]) == (False, False)
    assert (old["measured"], old["market_member"]) == (False, False)
    assert {"measured", "market_member"} <= set(MODEL_ONLY_FIELDS)
    assert not {"measured", "market_member"} & set(reader_evidence([on])[0])


def test_the_critic_and_the_writer_see_measured_and_market_member_per_post():
    pack = make_pack()
    pack["evidence"][0].update(measured=True, market_member=True)
    pack["evidence"][1].update(measured=False, market_member=False)
    critic = explain._critic_user(CANDIDATE, "ZA", "s", [], pack, ())
    writer = explain._writer_user(CANDIDATE, pack, "ZA", W_START, W_END)
    for user in (critic, writer):
        heads = {}
        for line in user.split("\n"):
            if line.startswith("post "):
                head = json.loads(line[line.index("{"):])
                heads[head["id"] if "id" in head else line.split()[1]] = head
        assert (heads["tt_1"]["measured"], heads["tt_1"]["market_member"]) == (True, True)
        assert (heads["ig_2"]["measured"], heads["ig_2"]["market_member"]) == (False, False)
        assert (heads["tt_3"]["measured"], heads["tt_3"]["market_member"]) == (False, False)  # unmarked reads false
    assert "measured is true when 42 sighted the post in a measured lane" in explain.CRITIC_SYSTEM
    assert "scraping, collection or grouping artefact" in explain.CRITIC_SYSTEM
    assert "neither rules a simpler explanation out on its own" in explain.CRITIC_SYSTEM


def test_the_marks_change_neither_the_pack_order_nor_the_critics_decision():
    plain, marked = make_pack(), make_pack()
    for r in marked["evidence"]:
        r.update(measured=True, market_member=True)
    a, b = FakeModel([good()]), FakeModel([good()])
    first, second = run(a, pack=plain), run(b, pack=marked)
    assert [r["id"] for r in plain["evidence"]] == [r["id"] for r in marked["evidence"]]
    assert first["reason"] is second["reason"] is None
    assert first["explanation"] == second["explanation"]


# CRITIC D3: the critic gets the detection facts the writer gets.
def test_the_critic_gets_the_detection_facts_with_the_calendar_moment_of_a_post_day():
    pack = make_pack()
    pack["facts"].append("Post tt_1: posted on Thursday 2026-09-24, the calendar's Heritage Day")
    critic = explain._critic_user(CANDIDATE, "ZA", "s", [], pack, ())
    writer = explain._writer_user(CANDIDATE, pack, "ZA", W_START, W_END)
    for fact in pack["facts"]:
        assert f"- {fact}" in critic and f"- {fact}" in writer
    assert "Facts from 42's detection, context you may weigh, never a cited post:" in critic
    empty = explain._critic_user(CANDIDATE, "ZA", "s", [], {**pack, "facts": []}, ())
    assert "Facts from 42's detection, context you may weigh, never a cited post:\nnone" in empty
