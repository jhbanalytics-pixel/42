"""The step rule on every stored-record path (release C review 3, finding 2). A creator renamed from hid_old_handle is hidden,
and a step the run stored while it searched names the old handle. The stream masks an @handle in a step unless it belongs
to a shown creator; the page reads the same steps from the poll of a running Ask, the investigation read, the finished
record and both replays, so the rule lives in project_record, which every one of them goes through. A running record holds
no answer and a finished one only the evidence it cited, so a step can name a hidden creator whose post the reader never
holds: the name cannot be learned there, and the step masks every @handle it cannot clear (fail closed)."""
import json

from core.api import agent_app, privacy
from core.api.tests import test_privacy_ask_routes as routes
from core.api.tests.test_privacy_ask_routes import ASK, GOOD, hold, remote_agent
from core.api.tests.test_privacy_projection import P_VIS1, ask_record

world = routes.world
api = routes.api
OLD = "hid_old_handle"
STEP = f"Searching TikTok for posts by @{OLD}"
CLEAR = "Searching TikTok for posts by @vis_handle"


def step_texts(view):
    return [s["text"] for s in view["steps"]]


def running_ask(texts):
    ask = agent_app.Ask({"ask_id": ASK, "question": "What is new in amapiano?", "market": "ZA"})
    for text in texts:
        ask.emit({"event": "step", "kind": "search", "text": text})
    agent_app.ASKS[ASK] = ask
    return ask


def finished_without_the_hidden_post():
    """A finished record whose cited evidence holds no post of the renamed creator, so the name cannot be learned from it."""
    record = ask_record()
    record["steps"] = [{"seq": 1, "text": STEP}, {"seq": 2, "text": CLEAR}, {"seq": 3, "text": "Reading 3 posts"}]
    record["answer"]["evidence"] = [e for e in record["answer"]["evidence"] if e["id"] == P_VIS1]
    record["answer"]["claims"] = [c for c in record["answer"]["claims"] if c["id"] == "c1"]
    record["answer"]["so_what"] = [s for s in record["answer"]["so_what"] if s["claim_ids"] == ["c1"]]
    record["answer"]["watch_next"] = []
    return record


def test_a_running_record_whose_only_event_is_a_step_masks_the_old_handle():
    store = routes.RouteStore(hide={"c_ren"})
    snapshot = running_ask([STEP]).snapshot()
    assert snapshot["answer"] is None
    assert step_texts(privacy.project_record(snapshot, store)) == ["Searching TikTok for posts by @***"]


def test_the_agent_poll_of_a_running_ask_masks_the_old_handle(world):
    world.store.hide = {"c_ren"}
    running_ask([STEP])
    body = world.client.get(f"/api/ask/{ASK}").json()
    assert json.dumps(body).count(OLD) == 0 and step_texts(body) == ["Searching TikTok for posts by @***"]


def test_the_investigation_read_of_a_running_ask_masks_the_old_handle(world):
    world.store.hide = {"c_ren"}
    ask = running_ask([STEP])
    view = agent_app.readable(ask.snapshot(), privacy.read_hidden(world.store))
    assert json.dumps(view).count(OLD) == 0 and step_texts(view) == ["Searching TikTok for posts by @***"]


def test_the_finished_record_masks_the_old_handle_when_no_cited_post_names_it(world):
    world.store.hide = {"c_ren"}
    hold(finished_without_the_hidden_post())
    body = world.client.get(f"/api/ask/{ASK}").json()
    assert json.dumps(body).count(OLD) == 0
    assert step_texts(body) == ["Searching TikTok for posts by @***", CLEAR, "Reading 3 posts"]


def test_the_investigation_replay_of_a_finished_record_masks_the_old_handle(world):
    world.store.hide = {"c_ren"}
    view = agent_app.readable(finished_without_the_hidden_post(), privacy.read_hidden(world.store))
    replayed = "".join(agent_app.replay(view, 0, "/api/investigations/i_1"))
    assert replayed.count(OLD) == 0 and "@***" in replayed and "@vis_handle" in replayed


def test_the_api_poll_of_a_running_ask_masks_the_old_handle(api):
    api.store.hide = {"c_ren"}
    held = json.loads(json.dumps(running_ask([STEP]).snapshot(), default=str))
    client = api.serve(remote_agent({}, held=held))
    body = client.get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert json.dumps(body).count(OLD) == 0 and step_texts(body) == ["Searching TikTok for posts by @***"]


def test_the_api_stored_read_and_replay_mask_the_old_handle(api):
    api.store.hide = {"c_ren"}
    api.store.records[ASK] = finished_without_the_hidden_post()
    client = api.serve(remote_agent({}))
    read = client.get(f"/api/ask/{ASK}", headers=GOOD)
    replay = client.get(f"/api/ask/{ASK}/events", headers=GOOD)
    assert read.text.count(OLD) == 0 and replay.text.count(OLD) == 0
    assert step_texts(read.json())[0] == "Searching TikTok for posts by @***"
    assert "@***" in replay.text and "@vis_handle" in replay.text


def test_a_step_names_only_a_creator_the_record_shows():
    store = routes.RouteStore(hide={"c_ren"})
    out = privacy.project_record(finished_without_the_hidden_post(), store)
    assert step_texts(out) == ["Searching TikTok for posts by @***", CLEAR, "Reading 3 posts"]
    kept = ask_record()
    kept["steps"] = [{"seq": 1, "text": CLEAR}]
    assert step_texts(privacy.project_record(kept, store)) == [CLEAR]


def test_a_list_that_cannot_be_read_masks_every_handle_in_a_step():
    store = routes.RouteStore(hide={"c_ren"}, fail="list")
    out = privacy.project_record(finished_without_the_hidden_post(), store)
    assert step_texts(out) == ["Searching TikTok for posts by @***", "Searching TikTok for posts by @***", "Reading 3 posts"]


def test_nobody_hidden_leaves_the_steps_as_they_are():
    store = routes.RouteStore(hide=set())
    record = finished_without_the_hidden_post()
    assert privacy.project_record(record, store)["steps"] == record["steps"]
