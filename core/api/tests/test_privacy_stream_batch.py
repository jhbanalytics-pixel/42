"""Release C review I4: a stream asks who wrote its stored posts once per chunk of events, not once per event."""
from core.api import agent_app
from core.api.tests import test_privacy_ask_routes as routes
from core.api.tests.test_privacy_ask_routes import ASK, hold, sse
from core.api.tests.test_privacy_projection import P_HID1, ask_record, ev, leaks

world = routes.world  # the fixture of the privacy route tests

HIDDEN_AT = {7, 42}  # positions of the events written by the hidden creator c_hid


def post_id(n):
    return "obs1_" + f"{n:032x}"


def events_for(first, count):
    out = []
    for n in range(first, first + count):
        out.append(("evidence", {"evidence": ev(post_id(n), f"handle_{n}", f"words of post {n}")}))
    return out


def store_with(count, world):
    for n in range(count):
        world.store.posts[post_id(n)] = ("c_hid" if n in HIDDEN_AT else "c_vis", "2026-10-05")


def test_a_chunk_of_evidence_events_costs_one_lookup_not_one_per_event_i4(world):
    store_with(100, world)
    hold(ask_record(), events_for(0, 100))
    r = world.client.get(f"/api/ask/{ASK}/events")
    shown = [d["evidence"]["id"] for k, d in sse(r.text) if k == "evidence"]
    assert shown == [post_id(n) for n in range(100) if n not in HIDDEN_AT]  # the hidden creator's posts are gone
    assert world.store.calls.count("post_creators") == 1


def test_three_chunks_cost_three_lookups_i4(world):
    store_with(300, world)
    ask = hold(ask_record(), events_for(0, 100))
    ask.done = None  # still running, so the next chunk can arrive
    stream = agent_app.follow(ask, 0)
    frames = []
    while len([f for f in frames if "event: evidence" in f]) < 98:
        frames.append(next(stream))
    assert world.store.calls.count("post_creators") == 1
    for first in (100, 200):
        with ask.cond:
            ask.events.extend((kind, {"seq": len(ask.events) + 1, **data}) for kind, data in events_for(first, 100))
        want = len([f for f in frames if "event: evidence" in f]) + 100 - len(HIDDEN_AT & set(range(first, first + 100)))
        while len([f for f in frames if "event: evidence" in f]) < want:
            frames.append(next(stream))
    assert world.store.calls.count("post_creators") == 3
    assert leaks("".join(frames), [P_HID1]) == []
