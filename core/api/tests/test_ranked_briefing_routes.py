"""The ranked briefing never names a story whose claims the API withheld (GET /api/ask/{id}, the agent's branch).

The agent reads its own hide list, applies it and builds the reader view, so run.briefing names the stories it saw. The
API then projects that response with the list it reads itself, which can hide more (a hide made between the two reads,
or a people read that fails on the second). A projection takes claims out; it never rebuilt the briefing, so the story
names and claim ids of the withheld claims went on to the browser."""
import json

from core.agent import answer_view
from core.api import agent_app
from core.api.tests import test_privacy_ask_routes as routes
from core.api.tests.test_privacy_ask_routes import ASK, GOOD, remote_agent
from core.api.tests.test_privacy_projection import P_HID1, P_VIS1, ev

api = routes.api  # the fixture of the privacy route tests
P_VIS2 = "obs1_" + "2" * 32
QUESTION = "What are people in South Africa posting about most this week?"
HASH = "sha256:" + "0" * 64


def sized(cid, name, posts, creators, post, query):
    return {"id": cid, "text": f"{name}: the story of the week, in {posts} posts from {creators} creators.",
            "label": "corroborated", "kind": "observation", "evidence_ids": [post], "quotes": [],
            "numbers": [{"value": posts, "unit": "posts", "query_id": query, "run_id": "r_1", "result_hash": HASH},
                        {"value": creators, "unit": "creators", "query_id": query, "run_id": "r_1", "result_hash": HASH}]}


def ranked_record():
    return {
        "ask_id": ASK, "question": QUESTION, "market": "ZA", "status": "complete",
        "created_at": "2026-10-07T09:00:00+02:00", "finished_at": "2026-10-07T09:02:00+02:00", "error": None,
        "parent_id": None, "steps": [], "run": {"posts": 3},
        "answer": {
            "status": "complete", "as_of": "2026-10-07T09:00:00+02:00",
            "short_answer": "Three stories led South Africa this week: the Soweto unrest, the Cup final and the Murder trial.",
            "evidence": [ev(P_VIS1, "vis_handle", "visible fixture words"), ev(P_HID1, "hid_handle", "fixture hidden words one"),
                         ev(P_VIS2, "vis_handle", "more visible fixture words")],
            "claims": [sized("c1", "Soweto unrest", 640, 301, P_VIS1, "q_a"), sized("c2", "Cup final", 512, 233, P_HID1, "q_b"),
                       sized("c3", "Murder trial", 288, 146, P_VIS2, "q_c")],
            "so_what": [{"text": "Pause posts near the routes.", "claim_ids": ["c1"]},
                        {"text": "Plan for the final whistle.", "claim_ids": ["c2"]},
                        {"text": "Stay clear of the trial.", "claim_ids": ["c3"]}],
            "watch_next": [], "gaps": []}}


def held_by_an_agent_that_saw_nobody_hidden():
    held = agent_app.readable(ranked_record(), (set(), set(), {}))
    assert [s["name"] for s in held["run"]["briefing"]["stories"]] == ["Soweto unrest", "Cup final", "Murder trial"]
    return held


def names(body):
    return [(s["rank"], s["name"], s["claim_ids"]) for s in body["run"]["briefing"]["stories"]]


def test_a_story_the_api_withholds_leaves_the_briefing_and_the_ranks_close_up(api):
    api.store.posts[P_VIS2] = ("c_vis", "2026-10-05")
    held = held_by_an_agent_that_saw_nobody_hidden()
    body = api.serve(remote_agent({}, held=held)).get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert [c["id"] for c in body["answer"]["claims"]] == ["c1", "c3"] and body["privacy"]["withheld"]["claims"] == 1
    assert names(body) == [(1, "Soweto unrest", ["c1"]), (2, "Murder trial", ["c3"])]
    sent = json.dumps(body)
    assert "Cup final" not in sent and '"c2"' not in sent


def test_when_the_api_cannot_read_who_is_hidden_no_story_name_or_claim_id_is_sent(api):
    api.store.posts[P_VIS2] = ("c_vis", "2026-10-05")
    held = held_by_an_agent_that_saw_nobody_hidden()
    api.store.fail = "list"
    body = api.serve(remote_agent({}, held=held)).get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert body["answer"]["claims"] == [] and body["privacy"]["state"] == "unavailable"
    assert "briefing" not in body["run"]
    sent = json.dumps(body)
    assert not any(word in sent for word in ("Soweto", "Cup final", "Murder trial", '"c1"', '"c2"', '"c3"'))


def test_with_nobody_hidden_the_api_sends_the_briefing_the_agent_built(api):
    api.store.hide.clear()
    api.store.posts[P_VIS2] = ("c_vis", "2026-10-05")
    held = held_by_an_agent_that_saw_nobody_hidden()
    body = api.serve(remote_agent({}, held=held)).get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert body["run"]["briefing"] == held["run"]["briefing"]


def test_the_api_stored_path_and_the_agent_path_send_the_same_briefing(api):
    api.store.posts[P_VIS2] = ("c_vis", "2026-10-05")
    api.store.records[ASK] = ranked_record()
    from_store = api.serve(remote_agent({})).get(f"/api/ask/{ASK}", headers=GOOD).json()
    held = held_by_an_agent_that_saw_nobody_hidden()
    from_agent = api.serve(remote_agent({}, held=held)).get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert names(from_store) == names(from_agent) == [(1, "Soweto unrest", ["c1"]), (2, "Murder trial", ["c3"])]


def test_the_reader_view_is_stable_so_presenting_the_agents_view_again_changes_nothing_else():
    held = held_by_an_agent_that_saw_nobody_hidden()
    assert answer_view.present(held) == held
