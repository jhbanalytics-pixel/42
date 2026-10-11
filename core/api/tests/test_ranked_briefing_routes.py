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


# Every route that returns the agent's record or something built from it: POST /api/ask with wait true, polling (above),
# the event stream, the history list and the export. A projection that withholds a claim leaves no story or claim id of it.

older_agent = routes.older_agent
WORDS = ("Cup final", '"c2"', "Soweto", "Murder trial", '"c1"', '"c3"')


def post_wait(api, held):
    client = api.serve(older_agent({"/api/ask": (200, held)}))
    return client.post("/api/ask", headers=GOOD, json={"question": QUESTION, "wait": True, "mode": "replay"})


def test_ask_with_wait_true_sends_no_story_or_claim_id_the_api_withheld(api):
    api.store.posts[P_VIS2] = ("c_vis", "2026-10-05")
    r = post_wait(api, held_by_an_agent_that_saw_nobody_hidden())
    body = r.json()
    assert r.status_code == 200 and [c["id"] for c in body["answer"]["claims"]] == ["c1", "c3"]
    assert names(body) == [(1, "Soweto unrest", ["c1"]), (2, "Murder trial", ["c3"])]
    sent = json.dumps(body)
    assert "Cup final" not in sent and '"c2"' not in sent


def test_ask_with_wait_true_sends_no_story_when_the_api_cannot_read_who_is_hidden(api):
    api.store.posts[P_VIS2] = ("c_vis", "2026-10-05")
    held = held_by_an_agent_that_saw_nobody_hidden()
    api.store.fail = "list"
    body = post_wait(api, held).json()
    assert body["answer"]["claims"] == [] and "briefing" not in body["run"]
    sent = json.dumps(body)
    assert not any(word in sent for word in WORDS)


def test_ask_with_wait_true_sends_the_briefing_the_agent_built_when_nobody_is_hidden(api):
    api.store.hide.clear()
    api.store.posts[P_VIS2] = ("c_vis", "2026-10-05")
    held = held_by_an_agent_that_saw_nobody_hidden()
    assert post_wait(api, held).json()["run"]["briefing"] == held["run"]["briefing"]


def test_the_event_stream_carries_no_briefing_and_no_story_name(api):
    wire = ('id: 1\nevent: step\ndata: {"seq": 1, "text": "Reading 3 posts"}\n\n'
            'id: 2\nevent: done\ndata: {"seq": 2, "status": "complete", "url": "/api/ask/a"}\n\n')
    r = api.serve(remote_agent({}, events=wire)).get(f"/api/ask/{ASK}/events", headers=GOOD)
    assert "briefing" not in r.text and not any(word in r.text for word in WORDS)
    api.store.records[ASK] = ranked_record()
    stored = api.serve(remote_agent({})).get(f"/api/ask/{ASK}/events", headers={**GOOD, "Last-Event-ID": "0"})
    assert stored.status_code == 200 and "briefing" not in stored.text and "Cup final" not in stored.text


def test_the_history_list_carries_no_briefing_and_no_story_name(api):
    api.store.records[ASK] = ranked_record()
    r = api.serve(remote_agent({})).get("/api/history/asks", headers=GOOD)
    assert r.status_code == 200 and "briefing" not in r.text and "Cup final" not in r.text


def test_the_export_of_a_ranked_answer_names_no_story_the_api_withheld(api):
    api.store.posts[P_VIS2] = ("c_vis", "2026-10-05")
    api.store.records[ASK] = ranked_record()
    r = api.serve(remote_agent({})).get(f"/api/ask/{ASK}/export", headers=GOOD)
    assert r.status_code == 200 and "briefing" not in r.text.lower() and "Cup final" not in r.text
    assert "Soweto unrest" in r.text and "Murder trial" in r.text
    held = held_by_an_agent_that_saw_nobody_hidden()
    again = api.serve(remote_agent({}, held=held)).get(f"/api/ask/{ASK}/export", headers=GOOD)
    assert again.status_code == 200 and "Cup final" not in again.text


def test_a_projection_that_withholds_drops_the_briefing_and_one_that_does_not_keeps_it(api):
    from core.api import privacy
    api.store.posts[P_VIS2] = ("c_vis", "2026-10-05")
    held = held_by_an_agent_that_saw_nobody_hidden()
    withheld = privacy.project_record(held, api.store)
    assert [c["id"] for c in withheld["answer"]["claims"]] == ["c1", "c3"] and "briefing" not in withheld["run"]
    assert "briefing" in held["run"]  # the input is not changed
    api.store.hide.clear()
    assert privacy.project_record(held_by_an_agent_that_saw_nobody_hidden(), api.store)["run"]["briefing"] == held["run"]["briefing"]
