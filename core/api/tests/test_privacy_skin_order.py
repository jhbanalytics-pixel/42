"""Privacy comments review 2, finding 5: the privacy projection runs on the record as stored and the skin masks what is
left. A skin strips the handle of every author it may not name, and a suppression row that names only a handle then
matches nothing, so the projection has to see the handles first. Checked on the agent reader, the API stored path and the
live stream, with and without a skin."""
import json

import pytest

from core.api import agent_app
from core.api.tests import test_privacy_ask_routes as routes
from core.api.tests.test_privacy_ask_routes import ASK, EVENTS, GOOD, hold, remote_agent, sse
from core.api.tests.test_privacy_projection import P_HID1, ask_record, handle_row, leaks

world = routes.world
api = routes.api
WORDS = "fixture hidden words one"


def skinned():
    record = ask_record()
    record["skin_id"] = "s_fixture"
    return record


def only_the_handle_is_hidden(store):
    store.hide = set()
    store.suppression_rows = [handle_row()]  # no creator id: the row names the platform and the handle only


@pytest.mark.parametrize("skin", [False, True])
def test_the_agent_reader_withholds_a_handle_only_suppressed_post_with_or_without_a_skin(world, skin):
    only_the_handle_is_hidden(world.store)
    shown = agent_app.readable(skinned() if skin else ask_record())
    assert P_HID1 not in [e["id"] for e in shown["answer"]["evidence"]]
    assert WORDS not in json.dumps(shown) and leaks(shown, [P_HID1]) == []


@pytest.mark.parametrize("skin", [False, True])
def test_the_api_stored_path_withholds_a_handle_only_suppressed_post_with_or_without_a_skin(api, skin):
    only_the_handle_is_hidden(api.store)
    api.store.records[ASK] = skinned() if skin else ask_record()
    shown = api.serve(remote_agent({})).get(f"/api/ask/{ASK}", headers=GOOD).json()
    assert P_HID1 not in [e["id"] for e in shown["answer"]["evidence"]]
    assert WORDS not in json.dumps(shown) and leaks(shown, [P_HID1]) == []


@pytest.mark.parametrize("skin", [False, True])
def test_the_api_export_and_replay_withhold_it_too_with_or_without_a_skin(api, skin):
    only_the_handle_is_hidden(api.store)
    api.store.records[ASK] = skinned() if skin else ask_record()
    client = api.serve(remote_agent({}))
    assert WORDS not in client.get(f"/api/ask/{ASK}/export", headers=GOOD).text
    assert leaks(client.get(f"/api/ask/{ASK}/events", headers=GOOD).text) == []


@pytest.mark.parametrize("skin", [False, True])
def test_the_live_stream_withholds_a_handle_only_suppressed_post_with_or_without_a_skin(world, skin):
    only_the_handle_is_hidden(world.store)
    hold(skinned() if skin else ask_record(), EVENTS)
    text = world.client.get(f"/api/ask/{ASK}/events").text
    events = sse(text)
    assert P_HID1 not in [d["evidence"]["id"] for k, d in events if k == "evidence"]
    assert WORDS not in text and leaks(text, [P_HID1]) == []


def test_a_skin_still_masks_an_author_it_may_not_name_after_the_projection(world):
    only_the_handle_is_hidden(world.store)
    shown = agent_app.readable(skinned())
    assert "vis_handle" not in json.dumps(shown["answer"]["evidence"])  # the visible author is stripped by the skin
