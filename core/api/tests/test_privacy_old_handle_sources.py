"""Release C review 2 finding m5: two mutants of the old-handle masking survived because every test used a TikTok
address that repeats the handle and never streamed a visible post that quotes the old handle.

P10: a hidden creator's post carries its own handle field. A renamed YouTube creator's address names no handle, so the
field is the only place the old name comes from. P14: a stream masks the evidence, claim and step events that follow a
hidden creator's post with the handles it learned from that post, not with the current ones alone."""
import json

from core.api import agent_app, privacy
from core.api.tests import test_privacy_ask_routes as routes
from core.api.tests.test_privacy_ask_routes import ASK, hold
from core.api.tests.test_privacy_projection import P_VIS1, ask_record, ev

world = routes.world
OLD = "hid_old_handle"
P_YT = "obs1_" + "d" * 32
QUOTING = f"Great video by {OLD} and friends"


def youtube_post():
    return {"id": P_YT, "platform": "youtube", "handle": OLD, "url": "https://www.youtube.com/watch?v=abc123",
            "posted_at": "2026-10-05T09:00:00+02:00", "market": "ZA", "text": "renamed channel words"}


def store_with_youtube_post(**kw):
    store = routes.RouteStore(hide={"c_ren"}, **kw)
    store.posts[P_YT] = ("c_ren", "2026-10-05")
    return store


def stored_shape():
    record = ask_record()
    record["steps"] = [{"seq": 1, "text": f"Reading {OLD}'s channel"}, {"seq": 2, "text": "Reading 3 posts"}]
    record["answer"]["evidence"] = [youtube_post(), dict(ev(P_VIS1, "vis_handle", QUOTING))]
    record["answer"]["claims"] = [{"id": "c1", "text": f"{OLD} posted", "label": "single_source", "kind": "observation",
                                   "evidence_ids": [P_VIS1], "quotes": [{"evidence_id": P_VIS1, "text": QUOTING}]}]
    record["answer"]["so_what"], record["answer"]["watch_next"] = [], []
    record["run"] = {"posts": 2}
    return record


def test_a_renamed_youtube_creators_old_name_comes_from_the_posts_handle_field_p10():
    out = privacy.project_record(stored_shape(), store_with_youtube_post())
    assert P_YT not in json.dumps(out)
    assert OLD not in json.dumps(out)
    assert P_VIS1 in json.dumps(out)  # the visible post stays, with the old name masked in its words


def test_a_dossier_masks_the_old_name_of_a_renamed_youtube_creator_p10():
    view = {"dossier_id": "d_1", "version": 1, "question": f"What did {OLD} say", "evidence": [youtube_post()],
            "claims": []}
    assert OLD not in json.dumps(privacy.project_dossier(view, store_with_youtube_post()))


def stream_text(world, events):
    world.store.hide = {"c_ren"}
    world.store.posts[P_YT] = ("c_ren", "2026-10-05")
    hold(ask_record(), events)
    return "".join(agent_app.follow(agent_app.ASKS[ASK], 0))


def test_a_visible_post_that_quotes_the_old_name_is_masked_in_the_stream_p14(world):
    text = stream_text(world, [("evidence", {"evidence": youtube_post()}),
                               ("evidence", {"evidence": ev(P_VIS1, "vis_handle", QUOTING)})])
    assert OLD not in text
    assert "event: evidence" in text and P_VIS1 in text


def test_a_claim_that_quotes_the_old_name_is_masked_in_the_stream_p14(world):
    text = stream_text(world, [("evidence", {"evidence": youtube_post()}),
                               ("evidence", {"evidence": ev(P_VIS1, "vis_handle", "visible words")}),
                               ("claim", {"claim": {"id": "c1", "text": f"{OLD} is not the only one",
                                                    "evidence_ids": [P_VIS1]}})])
    assert OLD not in text
    assert "event: claim" in text


def test_a_step_that_names_the_old_name_without_an_at_sign_is_masked_after_its_post_p14(world):
    text = stream_text(world, [("evidence", {"evidence": youtube_post()}), ("step", {"text": f"Reading {OLD}'s channel"})])
    assert OLD not in text
    assert "event: step" in text
