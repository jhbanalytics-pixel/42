"""Ranked briefing answers replayed offline through the real run_ask, then read the way a reader gets them.

Four recorded writer replies in the ranked shape (fixtures/ranked_briefing): NOW-01, NOW-03 and RISE-02 shaped, and
TOP-ZA, the question asked on the live site. Each runs through the real run_ask with the real trust checks, support
check, field check and model budget; only the outside world is the fixture's (core/eval/ask_replay.py). What this
proves is that the shape survives every check as a stored answer and reads back as a briefing. What it cannot prove is
that a live model writes this shape: the replies are recorded, so that is for a live run.

The expectations below were written from the design of each fixture, and none is read from the replay's output."""
import copy
import json
from pathlib import Path

import pytest

from core.agent import answer_view, briefing, writer
from core.agent.context import result_hash
from core.agent.tools.socialcrawl import creator_key
from core.api import privacy
from core.eval import ask_replay

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ranked_briefing"


@pytest.fixture(autouse=True)
def ranked_fixtures(monkeypatch):
    monkeypatch.setattr(ask_replay, "FIXTURES", FIXTURES)


@pytest.fixture(scope="module")
def replays():
    """Each fixture replayed once. The writer's user message is kept so a test can see which rule it carried."""
    saved, users, outs = ask_replay.FIXTURES, {}, {}
    ask_replay.FIXTURES = FIXTURES
    original = ask_replay.ReplayModel.complete_json
    real_run_ask = ask_replay.ask.run_ask

    def spy(self, *, system, user, schema, model, max_tokens):
        if schema is writer.WRITER_SCHEMA:
            users[self.fx["id"]] = user
        return original(self, system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)

    def run_ask(body, *args, **kwargs):
        out = real_run_ask(body, *args, **kwargs)
        outs[body["ask_id"]] = out
        return out

    ask_replay.ReplayModel.complete_json = spy
    ask_replay.ask.run_ask = run_ask
    try:
        found = {qid: ask_replay.run_question(qid) for qid in
                 ("NOW-01-RANKED", "NOW-03-RANKED", "RISE-02-RANKED", "TOP-ZA")}
    finally:
        ask_replay.ReplayModel.complete_json = original
        ask_replay.ask.run_ask = real_run_ask
        ask_replay.FIXTURES = saved
    for qid, result in found.items():
        result["writer_user"] = users.get(qid)
        result["query_receipts"] = outs[f"a_replay_{qid}"]["query_receipts"]
    return found


def read(result):
    """The record as the API gives it to a reader: stored answer and run, then answer_view.present."""
    return answer_view.present({"ask_id": "a_replay", "status": "complete", "answer": copy.deepcopy(result["answer"]),
                                "run": copy.deepcopy(result["run"])})


def recomputed(result):
    """Every number of every kept claim, its hash recomputed from the rows of its query receipt in their original
    order. A receipt keeps the rows that support a number first and records where each came from (row_indexes)."""
    receipts, problems, count = result["query_receipts"], [], 0
    for claim in result["answer"]["claims"]:
        for number in claim.get("numbers") or []:
            count += 1
            receipt = receipts.get(number["query_id"])
            if receipt is None or receipt["row_count"] > len(receipt["rows"]):
                problems.append((claim["id"], number["query_id"], "no full receipt"))
                continue
            rows = receipt["rows"]
            if "row_indexes" in receipt:
                rows = [row for _, row in sorted(zip(receipt["row_indexes"], rows))]
            if result_hash(rows) != number["result_hash"]:
                problems.append((claim["id"], number["query_id"], "hash"))
    return count, problems


def cuts(result):
    return {row["claim_id"]: sorted({r["rule"] for r in result["claim_checks"] if r["claim_id"] == row["claim_id"]
                                     and r["verdict"] == "cut"})
            for row in result["claim_checks"] if row["verdict"] == "cut"}


def shape(view):
    return [(s["rank"], s["name"], s["claim_ids"]) for s in view["run"]["briefing"]["stories"]]


@pytest.mark.parametrize("qid", ["NOW-01-RANKED", "NOW-03-RANKED", "RISE-02-RANKED", "TOP-ZA"])
def test_a_ranked_answer_keeps_its_contract_its_pins_and_its_summary_state(qid, replays):
    result = replays[qid]
    assert result["error"] is None
    assert result["contract"] == [] and result["meta_problem"] is None
    count, problems = recomputed(result)
    assert problems == [] and count == result["numbers"]["numbers"]
    assert float(result["run"]["model_usd"]) <= result["hold_usd"]


@pytest.mark.parametrize("qid", ["NOW-01-RANKED", "NOW-03-RANKED", "RISE-02-RANKED", "TOP-ZA"])
def test_a_ranked_answer_adds_nothing_to_the_stored_answer_or_the_stored_run(qid, replays):
    result = replays[qid]
    assert set(result["answer"]) == {"status", "as_of", "short_answer", "claims", "evidence", "so_what", "watch_next",
                                     "gaps", "context"}
    assert "briefing" not in result["run"]
    assert set(result["answer"]["claims"][0]) <= {"id", "text", "label", "kind", "evidence_ids", "quotes", "numbers"}


def test_the_rule_reaches_the_writer_for_the_ranked_questions_and_for_no_other_replay(replays):
    assert writer.asks_ranked(replays["TOP-ZA"]["vars"]["question"])
    assert writer.RANKED_BRIEFING_RULE in replays["TOP-ZA"]["writer_user"]
    assert writer.RANKED_BRIEFING_RULE in replays["NOW-01-RANKED"]["writer_user"]
    for qid in ("NOW-03-RANKED", "RISE-02-RANKED"):
        assert writer.RANKED_BRIEFING_RULE not in replays[qid]["writer_user"]  # their recorded reply is the shape's, not the rule's


# TOP-ZA, the question asked on the live site.

def test_the_live_question_reads_as_a_ranked_briefing_of_three_stories(replays):
    result = replays["TOP-ZA"]
    assert result["answer"]["status"] == "complete" and result["answer"]["short_answer"].startswith("Three stories led")
    view = read(result)
    assert shape(view) == [(1, "Soweto unrest", ["c1", "c2", "c3"]),
                           (2, "Pirates vs Sundowns cup final", ["c4", "c5", "c6"]),
                           (3, "Forbes and Motsoane murder trial", ["c7"])]
    assert view["run"]["briefing"]["ordered"] is True and view["run"]["briefing"]["other_claim_ids"] == ["c8"]


def test_each_story_size_reads_its_own_pinned_figures_from_one_query(replays):
    result = replays["TOP-ZA"]
    claims = {c["id"]: c for c in result["answer"]["claims"]}
    got = {}
    for story in read(result)["run"]["briefing"]["stories"]:
        size = story["size"]
        numbers = [claims[size["claim_id"]]["numbers"][f["index"]] for f in size["figures"]]
        assert len({n["query_id"] for n in numbers}) == 1
        got[story["name"]] = {n["unit"]: n["value"] for n in numbers}
    assert got == {"Soweto unrest": {"posts": 640, "creators": 301, "platforms": 4},
                   "Pirates vs Sundowns cup final": {"posts": 512, "creators": 233, "platforms": 4},
                   "Forbes and Motsoane murder trial": {"posts": 288, "creators": 146, "platforms": 3}}


def test_a_part_the_pack_does_not_support_is_left_out_of_the_story(replays):
    stories = {s["name"]: s["claim_ids"] for s in read(replays["TOP-ZA"])["run"]["briefing"]["stories"]}
    assert stories["Forbes and Motsoane murder trial"] == ["c7"]  # no peak claim and no why claim
    claims = {c["id"]: c for c in replays["TOP-ZA"]["answer"]["claims"]}
    assert all(not claims[cid]["numbers"] for cid in ("c3", "c6"))  # a why claim carries no figure of its own
    assert [n["value"] for n in claims["c2"]["numbers"]] == [152, 60]  # the peak and the last day, from the daily row


def test_every_claim_the_briefing_points_at_is_a_stored_claim_byte_for_byte(replays):
    result = replays["TOP-ZA"]
    view = read(result)
    assert view["answer"]["claims"] == result["answer"]["claims"]
    stored = {c["id"] for c in result["answer"]["claims"]}
    for story in view["run"]["briefing"]["stories"]:
        assert set(story["claim_ids"]) <= stored
        assert all(c["text"].startswith(story["name"] + ": ") for c in view["answer"]["claims"] if c["id"] in story["claim_ids"])
        assert all(0 <= i < len(view["answer"]["so_what"]) for i in story["so_what"])


def test_each_story_has_its_own_so_what_line_and_none_is_left_over(replays):
    view = read(replays["TOP-ZA"])
    assert [s["so_what"] for s in view["run"]["briefing"]["stories"]] == [[0], [1], [2]]


def test_hiding_the_creator_behind_the_third_story_removes_it_and_closes_the_ranks_up(replays):
    result = replays["TOP-ZA"]
    record = {"ask_id": "a_replay", "status": "complete", "answer": result["answer"], "run": result["run"]}
    hidden = ({creator_key("x", "@court_roll_za")}, set(), set())
    view = answer_view.present(privacy.project_record(record, None, hidden=hidden))
    assert shape(view) == [(1, "Soweto unrest", ["c1", "c2", "c3"]), (2, "Pirates vs Sundowns cup final", ["c4", "c5", "c6"])]
    assert view["run"]["briefing"]["ordered"] is True
    assert "Forbes" not in json.dumps(view["run"]["briefing"]) and "Motsoane" not in json.dumps(view["answer"]["claims"])
    assert view["answer"]["short_answer"] == privacy.SUMMARY_WITHHELD  # the lead named the story that is gone


def test_hiding_all_but_one_story_leaves_no_briefing(replays):
    result = replays["TOP-ZA"]
    record = {"ask_id": "a_replay", "status": "complete", "answer": result["answer"], "run": result["run"]}
    hidden = ({creator_key("tiktok", "@kasi_football_hub"), creator_key("x", "@court_roll_za")}, set(), set())
    view = answer_view.present(privacy.project_record(record, None, hidden=hidden))
    assert "briefing" not in view["run"]


# The three benchmark shapes.

def test_now_01_in_the_ranked_shape_has_three_sized_stories_in_order(replays):
    result = replays["NOW-01-RANKED"]
    assert result["answer"]["status"] == "complete" and cuts(result) == {}
    view = read(result)
    assert shape(view) == [(1, "Pap and gravy duets", ["c1", "c2", "c3"]), (2, "Taxi rank playlists", ["c4", "c5"]),
                           (3, "Run club mornings", ["c6"])]
    assert view["run"]["briefing"]["ordered"] is True and view["run"]["briefing"]["other_claim_ids"] == ["c7"]
    assert all(s["size"] is not None for s in view["run"]["briefing"]["stories"])


def test_now_03_has_no_size_for_any_story_because_the_stored_side_holds_no_count(replays):
    result = replays["NOW-03-RANKED"]
    assert cuts(result) == {"c2": ["K1", "K10"]}  # the seeded quote that is not verbatim
    view = read(result)
    assert shape(view) == [(1, "Mambo ni matatu", ["c1"]), (2, "Kasarani Kick", ["c3", "c4"]), (3, "Si ulisema", ["c5"])]
    briefing_ = view["run"]["briefing"]
    assert briefing_["ordered"] is False and all(s["size"] is None for s in briefing_["stories"])


def test_rise_02_keeps_two_sized_stories_after_the_seeded_defects_are_cut(replays):
    result = replays["RISE-02-RANKED"]
    assert cuts(result) == {"c4": ["K10", "K3"], "c6": ["K4"]}
    view = read(result)
    assert shape(view) == [(1, "Sapa reset", ["c1", "c2", "c3"]), (2, "Wahala package", ["c5"])]
    assert view["run"]["briefing"]["ordered"] is True


def test_the_repos_own_now_01_replay_reads_as_before(monkeypatch):
    monkeypatch.setattr(ask_replay, "FIXTURES", Path(ask_replay.__file__).parent / "ask_replay_fixtures")
    result = ask_replay.run_question("NOW-01")
    assert result["error"] is None
    view = read(result)
    assert "briefing" not in view["run"]
    assert briefing.stories(result["answer"]) is None
