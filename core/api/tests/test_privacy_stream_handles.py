"""Release C review 2 finding I2: a live stream names no @handle in a step unless the stream knows it belongs to a shown
creator, whenever any creator is hidden. The old handle of a renamed hidden creator reaches a step before its post
arrives, or on a resumed stream that never saw the post, so the stream cannot clear it and must mask it (fail closed).
A resumed stream judges the events it skips first, so it applies the same rule from its first event."""
from core.api import agent_app
from core.api.tests import test_privacy_ask_routes as routes
from core.api.tests.test_privacy_ask_routes import ASK, hold
from core.api.tests.test_privacy_projection import P_REN1, P_VIS1, ask_record, ev

world = routes.world
OLD = "hid_old_handle"
SEARCH_OLD = f"Searching TikTok for posts by @{OLD}"
READ_OLD = f"Reading {OLD}'s post on TikTok"


def renamed():
    return ("evidence", {"evidence": ev(P_REN1, OLD, "renamed creator words", "2026-10-05T01:00:00+02:00")})


def visible():
    return ("evidence", {"evidence": ev(P_VIS1, "vis_handle", "visible fixture words")})


def running(events):
    ask = hold(ask_record(), events)
    ask.done = None  # still running, so more events can arrive
    ask.finished.clear()
    return ask


def push(ask, kind, data):
    with ask.cond:
        ask.events.append((kind, {"seq": len(ask.events) + 1, **data}))
        ask.cond.notify_all()


def test_a_step_that_names_the_old_handle_before_its_post_arrives_is_masked_i2(world):
    world.store.hide = {"c_ren"}
    ask = running([("step", {"text": SEARCH_OLD})])
    stream = agent_app.follow(ask, 0)
    first = next(stream)  # the search step goes out alone: its post has not been read yet
    assert OLD not in first and "step" in first
    push(ask, "evidence", renamed()[1])
    push(ask, "step", {"text": READ_OLD})
    rest = "".join(next(stream) for _ in range(1))  # the step after its post, now learned
    assert OLD not in rest


def test_a_resumed_stream_masks_the_old_handle_it_never_saw_arrive_i2(world):
    world.store.hide = {"c_ren"}
    events = [renamed(), visible(), ("step", {"text": READ_OLD}), ("step", {"text": SEARCH_OLD})]
    hold(ask_record(), events)
    whole = "".join(agent_app.follow(agent_app.ASKS[ASK], 0))
    resumed = "".join(agent_app.follow(agent_app.ASKS[ASK], 2))  # the browser reconnects with Last-Event-ID 2
    assert OLD not in whole
    assert OLD not in resumed
    assert "event: step" in resumed


def test_a_resumed_stream_masks_an_at_handle_it_cannot_clear_even_with_no_post_skipped_i2(world):
    world.store.hide = {"c_ren"}
    hold(ask_record(), [("step", {"text": "Looking"}), ("step", {"text": SEARCH_OLD}),
                        ("step", {"text": "Searching TikTok for posts by @someone_else"})])
    resumed = "".join(agent_app.follow(agent_app.ASKS[ASK], 1))
    assert OLD not in resumed and "someone_else" not in resumed
    assert "@***" in resumed


def test_an_at_handle_of_a_shown_creator_stays_once_its_post_was_shown_i2(world):
    world.store.hide = {"c_ren"}
    events = [visible(), ("step", {"text": "Reading @vis_handle's post on TikTok"})]
    hold(ask_record(), events)
    whole = "".join(agent_app.follow(agent_app.ASKS[ASK], 0))
    assert "@vis_handle" in whole


def test_a_resumed_stream_keeps_the_at_handle_of_a_creator_shown_before_the_reconnect_i2(world):
    world.store.hide = {"c_ren"}
    events = [visible(), ("step", {"text": "Reading @vis_handle's post on TikTok"})]
    hold(ask_record(), events)
    resumed = "".join(agent_app.follow(agent_app.ASKS[ASK], 1))
    assert "@vis_handle" in resumed


def test_an_at_handle_before_its_post_is_shown_is_masked_even_for_a_visible_creator_i2(world):
    world.store.hide = {"c_ren"}
    ask = running([("step", {"text": "Searching TikTok for posts by @vis_handle"})])
    stream = agent_app.follow(ask, 0)
    assert "vis_handle" not in next(stream)  # the stream cannot clear it yet


def test_a_handle_that_a_shown_post_shares_with_a_hidden_creator_is_still_masked_i2(world):
    world.store.hide = {"c_ren"}
    other_platform = ("evidence", {"evidence": ev(P_VIS1, OLD, "a shown post on another platform", platform="x")})
    hold(ask_record(), [renamed(), other_platform, ("step", {"text": SEARCH_OLD})])
    assert OLD not in "".join(agent_app.follow(agent_app.ASKS[ASK], 0))


def test_nobody_hidden_leaves_every_step_as_it_came_i2(world):
    world.store.hide = set()
    hold(ask_record(), [("step", {"text": SEARCH_OLD})])
    assert SEARCH_OLD in "".join(agent_app.follow(agent_app.ASKS[ASK], 0))


def test_an_unreadable_list_masks_every_at_handle_in_a_step_i2(world):
    world.store.fail = "list"
    hold(ask_record(), [("step", {"text": "Searching TikTok for posts by @anyone_at_all"})])
    text = "".join(agent_app.follow(agent_app.ASKS[ASK], 0))
    assert "anyone_at_all" not in text


def test_the_resume_costs_one_more_lookup_not_one_per_skipped_event_i2(world):
    world.store.hide = {"c_ren"}
    hold(ask_record(), [renamed(), visible(), ("step", {"text": "after"})])
    "".join(agent_app.follow(agent_app.ASKS[ASK], 2))
    assert world.store.calls.count("post_creators") == 1
