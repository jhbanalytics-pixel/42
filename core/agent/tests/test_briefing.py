"""The ranked briefing is read from a stored answer and never stored.

A story is the claims that start with the same name and a colon. Nothing here writes text: the briefing holds ids and
indexes into the answer, so every word and every figure a reader sees is the stored claim's own, with the checks it
passed. A figure counts as a story's size only when one claim pins its posts and its creators to the same query.
A part the answer does not carry is absent from the briefing, never filled."""
import copy

import pytest

from core.agent import answer_view, briefing

RUN = "r_20261011_080000_aaaaaaaa"
HASH = "sha256:" + "a" * 64


def number(value, unit, query="q_1"):
    return {"value": value, "unit": unit, "query_id": query, "run_id": RUN, "result_hash": HASH}


def claim(cid, text, *, numbers=(), kind="observation", label="corroborated", evidence=("p1",)):
    return {"id": cid, "text": text, "label": label, "kind": kind, "evidence_ids": list(evidence), "quotes": [],
            "numbers": list(numbers)}


def sized(cid, name, posts, creators, query, platforms=None, rest="is where the week's posts went."):
    nums = [number(posts, "posts", query), number(creators, "creators", query)]
    if platforms:
        nums.append(number(platforms, "platforms", query))
    return claim(cid, f"{name}: {rest}", numbers=nums)


def answer(claims, so_what=(), short="Three stories led the week."):
    return {"status": "complete", "as_of": "2026-10-11T08:00:00+02:00", "short_answer": short, "claims": list(claims),
            "evidence": [], "so_what": list(so_what), "watch_next": [], "gaps": []}


THREE = [
    sized("c1", "Soweto unrest", 412, 188, "q_a", 3),
    claim("c2", "Soweto unrest: posts say the clashes began after a taxi route dispute.", evidence=("p2",)),
    sized("c3", "Cup final", 301, 150, "q_b", 2),
    sized("c4", "Murder trial", 120, 77, "q_c", 2),
]


def test_three_named_stories_become_a_ranked_briefing():
    got = briefing.stories(answer(THREE))
    assert got["kind"] == "ranked_stories" and got["ordered"] is True
    assert [(s["rank"], s["name"], s["claim_ids"]) for s in got["stories"]] == [
        (1, "Soweto unrest", ["c1", "c2"]), (2, "Cup final", ["c3"]), (3, "Murder trial", ["c4"])]
    assert got["other_claim_ids"] == []


def test_a_stories_size_is_the_one_claim_that_pins_posts_and_creators_to_one_query():
    story = briefing.stories(answer(THREE))["stories"][0]
    assert story["size"] == {"claim_id": "c1", "figures": [{"unit": "posts", "index": 0},
                                                           {"unit": "creators", "index": 1},
                                                           {"unit": "platforms", "index": 2}]}


def test_platforms_are_a_size_figure_only_when_the_same_query_pins_them():
    nums = [number(412, "posts", "q_a"), number(188, "creators", "q_a"), number(3, "platforms", "q_other")]
    one = claim("c1", "Soweto unrest: the biggest.", numbers=nums)
    two = sized("c2", "Cup final", 301, 150, "q_b")
    story = briefing.stories(answer([one, two]))["stories"][0]
    assert [f["unit"] for f in story["size"]["figures"]] == ["posts", "creators"]


def test_a_claim_with_posts_from_one_query_and_creators_from_another_is_not_a_size():
    split = claim("c1", "Soweto unrest: the biggest.", numbers=[number(412, "posts", "q_a"), number(188, "creators", "q_b")])
    two = sized("c2", "Cup final", 301, 150, "q_b")
    got = briefing.stories(answer([split, two]))
    assert got["stories"][0]["size"] is None and got["ordered"] is False


def test_a_peak_claim_with_posts_alone_is_not_a_size():
    peak = claim("c2", "Soweto unrest: it peaked on 9 Oct.", numbers=[number(88, "posts", "q_day")])
    got = briefing.stories(answer([THREE[0], peak, THREE[2]]))
    assert got["stories"][0]["size"]["claim_id"] == "c1"


def test_two_claims_that_both_look_like_the_size_leave_the_size_open():
    twin = sized("c2", "Soweto unrest", 400, 180, "q_z")
    got = briefing.stories(answer([THREE[0], twin, THREE[2]]))
    assert got["stories"][0]["size"] is None


def test_a_figure_that_carries_no_pin_is_not_a_size():
    for bad in ({"value": 412, "unit": "posts"}, number(True, "posts", "q_a"), number(-4, "posts", "q_a"),
                {**number(412, "posts", "q_a"), "query_id": ""}, {**number(412, "posts", "q_a"), "run_id": ""},
                {**number(412, "posts", "q_a"), "result_hash": "sha256:abc"}):
        broken = claim("c1", "Soweto unrest: the biggest.", numbers=[bad, number(188, "creators", "q_a")])
        got = briefing.stories(answer([broken, THREE[2]]))
        assert got["stories"][0]["size"] is None, bad


def test_the_order_is_confirmed_only_when_creators_then_posts_do_not_rise():
    assert briefing.stories(answer(THREE))["ordered"] is True
    tied = [sized("c1", "A story", 300, 150, "q_a"), sized("c2", "B story", 200, 150, "q_b"),
            sized("c3", "C story", 200, 150, "q_c")]
    assert briefing.stories(answer(tied))["ordered"] is True
    backwards = [THREE[2], THREE[0], THREE[3]]
    assert briefing.stories(answer(backwards))["ordered"] is False


def test_a_story_with_no_size_leaves_the_order_unconfirmed():
    got = briefing.stories(answer([THREE[0], claim("c9", "Cup final: a match people watched."), THREE[3]]))
    assert got["ordered"] is False and [s["rank"] for s in got["stories"]] == [1, 2, 3]


def test_one_named_story_is_not_a_ranking():
    assert briefing.stories(answer([THREE[0], THREE[1]])) is None


def test_an_answer_with_no_named_story_has_no_briefing():
    plain = [claim("c1", "Amapiano Sundays are back, with 17 posts."), claim("c2", "Taxi playlists are next.")]
    assert briefing.stories(answer(plain)) is None
    assert briefing.stories(answer([])) is None
    assert briefing.stories({}) is None and briefing.stories(None) is None


@pytest.mark.parametrize("text", [
    "Top 3 stories: they led.",                    # a digit
    "Posted at 10:30 the clashes began.",          # a time
    "#soweto: the tag led.",                       # a hashtag
    "@someone: said it.",                          # a handle
    "https://example.test/x: link.",               # a link
    "One two three four five six seven eight nine: too long.",
    "Soweto unrest:no space after the colon.",
    ": nothing before the colon.",
    "Soweto unrest",                               # no colon
    "Soweto unrest:",                              # nothing after it
])
def test_a_prefix_that_is_not_a_plain_story_name_names_no_story(text):
    other = sized("c2", "Cup final", 301, 150, "q_b")
    third = sized("c3", "Murder trial", 120, 77, "q_c")
    got = briefing.stories(answer([claim("c1", text), other, third]))
    assert [s["name"] for s in got["stories"]] == ["Cup final", "Murder trial"]
    assert got["other_claim_ids"] == ["c1"]


def test_the_same_name_in_another_case_is_the_same_story_and_the_first_spelling_is_kept():
    again = claim("c5", "SOWETO UNREST: a second look.", evidence=("p3",))
    got = briefing.stories(answer([THREE[0], THREE[2], again, THREE[3]]))
    assert got["stories"][0]["name"] == "Soweto unrest" and got["stories"][0]["claim_ids"] == ["c1", "c5"]


def test_a_recommendation_or_proposal_is_never_grouped_into_a_story():
    rec = claim("c5", "Soweto unrest: join in.", kind="recommendation")
    got = briefing.stories(answer([*THREE, rec]))
    assert got["stories"][0]["claim_ids"] == ["c1", "c2"] and got["other_claim_ids"] == ["c5"]


def test_a_claim_with_no_story_stays_where_it_was_as_an_other_finding():
    across = claim("c5", "Each of the three lives on TikTok.", evidence=("p4",))
    got = briefing.stories(answer([*THREE, across]))
    assert got["other_claim_ids"] == ["c5"]


def test_a_so_what_line_belongs_to_a_story_when_all_its_claims_are_that_storys():
    lines = [{"text": "Join the conversation as a fan.", "claim_ids": ["c1", "c2"]},
             {"text": "Sponsor the screening.", "claim_ids": ["c3"]},
             {"text": "Across all of them.", "claim_ids": ["c1", "c3"]},
             {"text": "Nothing cited.", "claim_ids": []},
             {"text": "A stray reference.", "claim_ids": ["c99"]}]
    got = briefing.stories(answer(THREE, so_what=lines))
    assert [s["so_what"] for s in got["stories"]] == [[0], [1], []]


def test_the_briefing_carries_indexes_and_ids_and_no_text():
    got = briefing.stories(answer(THREE))
    names = {s["name"] for s in got["stories"]}

    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for v in value.values():
                yield from strings(v)
        elif isinstance(value, list):
            for v in value:
                yield from strings(v)

    ids = {c["id"] for c in THREE}
    assert set(strings(got)) <= names | ids | {"ranked_stories", "posts", "creators", "platforms"}, sorted(set(strings(got)))


def test_the_input_is_not_changed():
    before = copy.deepcopy(answer(THREE))
    briefing.stories(before)
    assert before == answer(THREE)


def test_a_story_whose_claims_were_withheld_drops_out_and_the_ranks_close_up():
    shown = answer([c for c in THREE if c["id"] not in ("c3",)])
    got = briefing.stories(shown)
    assert [(s["rank"], s["name"]) for s in got["stories"]] == [(1, "Soweto unrest"), (2, "Murder trial")]
    assert briefing.stories(answer([THREE[0], THREE[1]])) is None


# Where a reader gets it: answer_view.present adds it to the run and recomputes it on every read.

RANKED_Q = "What are people in South Africa posting about most this week?"


def record(claims=THREE, run=None, so_what=(), question=RANKED_Q, **more):
    return {"ask_id": "a_1", "status": "complete", "question": question, "answer": answer(claims, so_what),
            "run": run if run is not None else {}, **more}


def test_present_adds_the_briefing_beside_the_answer_in_the_run():
    shown = answer_view.present(record())
    assert shown["run"]["briefing"] == briefing.stories(shown["answer"])
    assert shown["run"]["briefing"]["stories"][0]["name"] == "Soweto unrest"


def test_present_leaves_every_claim_byte_for_byte_as_stored():
    stored = record()
    assert answer_view.present(stored)["answer"]["claims"] == stored["answer"]["claims"]


def test_present_is_stable_with_the_briefing_in_place():
    once = answer_view.present(record())
    assert answer_view.present(once) == once


def test_a_briefing_in_a_stored_run_is_never_trusted():
    forged = {"briefing": {"kind": "ranked_stories", "ordered": True, "other_claim_ids": [],
                           "stories": [{"rank": 1, "name": "Forged", "claim_ids": ["c9"], "size": None, "so_what": []}]}}
    shown = answer_view.present(record(run=forged))
    assert shown["run"]["briefing"]["stories"][0]["name"] == "Soweto unrest"
    plain = answer_view.present(record([claim("c1", "One plain claim.")], run=forged))
    assert "briefing" not in plain["run"]


def test_an_answer_with_no_story_gets_no_briefing_key_and_no_run_is_made_up():
    plain = answer_view.present(record([claim("c1", "One plain claim.")]))
    assert "briefing" not in plain["run"]
    bare = {"ask_id": "a_1", "status": "complete", "question": RANKED_Q, "answer": answer([claim("c1", "One plain claim.")])}
    assert "run" not in answer_view.present(bare)
    assert "briefing" in answer_view.present({**bare, "answer": answer(THREE)})["run"]


# I1: a briefing is read only for a stored question that asked for a ranking, and never for a skin, an investigation
# or a spike. A claim that starts with a label and a colon is not a story until the question asked for stories.

def labelled(*names):
    return [claim(f"c{i}", f"{name}: a line under its label.", evidence=(f"p{i}",)) for i, name in enumerate(names, 1)]


NOT_RANKED_ASKS = [
    ("A brief for a data bundle aimed at students in Nigeria.", labelled("Tensions", "Sounds", "Language to avoid")),
    ("Compare football banter in South Africa, Nigeria and Kenya.", labelled("South Africa", "Nigeria", "Kenya")),
    ("How is the Springbok tour playing on social?", labelled("TikTok", "X")),
    ("What is happening with amapiano Sundays?", THREE),
    ("A launch brief for a student data bundle in Nigeria: lean into the top trending conversations on campus.", THREE),
    ("Why are the top stories in South Africa trending?", THREE),
    ("Which of the top stories will keep growing?", THREE),
    ("Which topics are most likely to blow up in South Africa this week?", THREE),
    ("", THREE),
    (None, THREE),
]


@pytest.mark.parametrize("question,claims", NOT_RANKED_ASKS)
def test_an_answer_to_a_question_that_asked_for_no_ranking_gets_no_briefing(question, claims):
    shown = answer_view.present(record(claims, question=question))
    assert "briefing" not in shown["run"]


def test_the_three_misfire_answers_would_read_as_stories_if_the_question_were_not_asked():
    for _, claims in NOT_RANKED_ASKS[:3]:
        assert briefing.stories(answer(claims)) is not None


def test_a_record_with_no_question_gets_no_briefing():
    bare = record()
    del bare["question"]
    assert "briefing" not in answer_view.present(bare)["run"]


@pytest.mark.parametrize("more", [{"skin_id": "skin_1"}, {"investigation_id": "inv_1"}, {"spike": {"item_id": "i"}}])
def test_a_skin_an_investigation_or_a_spike_gets_no_briefing_even_for_a_ranked_question(more):
    assert "briefing" not in answer_view.present(record(**more))["run"]


def test_a_record_that_has_no_skin_and_no_spike_is_read_with_those_keys_null_or_absent():
    assert "briefing" in answer_view.present(record(skin_id=None, investigation_id=None, spike=None))["run"]


def test_for_record_is_the_one_gate_present_uses():
    shown = record()
    assert briefing.for_record(shown, shown["answer"]) == briefing.stories(shown["answer"])
    assert briefing.for_record(None, shown["answer"]) is None
    assert briefing.for_record({"answer": shown["answer"]}, shown["answer"]) is None


# M2: claims of two stories are never one fact, so a merge cannot move a so-what line to another story.

def test_two_stories_that_quote_the_same_words_with_the_same_figures_are_not_merged():
    quote = [{"evidence_id": "p1", "text": "burning tyres at the intersection"}]
    one = {**sized("c1", "Soweto unrest", 640, 301, "q_a", 4), "quotes": quote, "evidence_ids": ["p1"]}
    two = {**sized("c2", "Cup final", 640, 301, "q_a", 4), "quotes": quote, "evidence_ids": ["p1"]}
    lines = [{"text": "Pause posts near the routes.", "claim_ids": ["c1"]}, {"text": "Plan for the whistle.", "claim_ids": ["c2"]}]
    shown = answer_view.present(record([one, two], so_what=lines))
    assert [c["id"] for c in shown["answer"]["claims"]] == ["c1", "c2"]
    assert [(s["name"], s["so_what"]) for s in shown["run"]["briefing"]["stories"]] == [("Soweto unrest", [0]), ("Cup final", [1])]


def test_a_claim_said_twice_inside_one_story_still_merges_and_the_story_is_read_after_the_merge():
    quote = [{"evidence_id": "p1", "text": "burning tyres at the intersection"}]
    one = {**sized("c1", "Soweto unrest", 640, 301, "q_a", 4), "quotes": quote}
    twin = {**one, "id": "c2"}
    other = sized("c3", "Cup final", 512, 233, "q_b", 4)
    shown = answer_view.present(record([one, twin, other]))
    assert [c["id"] for c in shown["answer"]["claims"]] == ["c1", "c3"]
    assert [s["claim_ids"] for s in shown["run"]["briefing"]["stories"]] == [["c1"], ["c3"]]


def test_a_claim_said_twice_in_two_cases_of_one_name_is_one_fact_in_one_story():
    quote = [{"evidence_id": "p1", "text": "burning tyres at the intersection"}]
    one = {**sized("c1", "Soweto unrest", 640, 301, "q_a", 4), "quotes": quote}
    twin = {**one, "id": "c2", "text": one["text"].replace("Soweto unrest", "SOWETO UNREST")}
    shown = answer_view.present(record([one, twin, sized("c3", "Cup final", 512, 233, "q_b", 4)]))
    assert [c["id"] for c in shown["answer"]["claims"]] == ["c1", "c3"]


# M3: a masked name is not a story name.

def test_a_name_a_skin_has_masked_is_no_story_name_so_two_masked_names_never_fold_into_one_story():
    claims = [claim("c1", "*** skit: a line.", evidence=("p1",)), claim("c2", "*** skit: another line.", evidence=("p2",)),
              sized("c3", "Cup final", 512, 233, "q_b"), sized("c4", "Murder trial", 120, 77, "q_c")]
    got = briefing.stories(answer(claims))
    assert [s["name"] for s in got["stories"]] == ["Cup final", "Murder trial"] and got["other_claim_ids"] == ["c1", "c2"]


# M5: the cases a mutation pass found nothing pinning.

def test_a_zero_figure_is_not_a_size():
    zero = claim("c1", "Soweto unrest: nothing.", numbers=[number(0, "posts", "q_a"), number(5, "creators", "q_a")])
    assert briefing.stories(answer([zero, THREE[2]]))["stories"][0]["size"] is None


def test_every_figure_the_one_query_gives_is_named_in_order():
    got = briefing.stories(answer(THREE))["stories"][0]["size"]["figures"]
    assert [f["unit"] for f in got] == ["posts", "creators", "platforms"] and [f["index"] for f in got] == [0, 1, 2]


def test_a_name_with_no_letter_or_a_padded_name_is_not_a_story():
    other = [sized("c8", "Cup final", 301, 150, "q_b"), sized("c9", "Murder trial", 120, 77, "q_c")]
    for text in ("***: a line.", "..: a line.", " Soweto unrest: a line.", "Soweto unrest : a line."):
        got = briefing.stories(answer([claim("c1", text), *other]))
        assert [s["name"] for s in got["stories"]] == ["Cup final", "Murder trial"], text


def test_a_so_what_line_that_names_a_claim_that_is_not_there_is_not_attached():
    lines = [{"text": "Mixed.", "claim_ids": ["c1", "c99"]}, {"text": "Fine.", "claim_ids": ["c3"]}]
    got = briefing.stories(answer(THREE, so_what=lines))
    assert [s["so_what"] for s in got["stories"]] == [[], [1], []]
