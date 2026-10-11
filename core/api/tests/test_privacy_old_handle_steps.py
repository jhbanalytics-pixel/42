"""A creator hidden under the name they have now is also named by the name they had: in the address of their old posts
and of the comments under them, and in the step text a run stored while it searched ("Searching TikTok for posts by
@hid_old_handle"). The masking reads the current handle only, so the projection also learns the handles of the posts and
comments it withholds for that creator, and masks those wherever they appear. Reviewer shape: a comment on P_REN1, whose
creator c_ren is hidden and was called hid_old_handle when the run read it."""
import json

from core.api import agent_app, privacy
from core.api.tests import test_privacy_ask_routes as routes
from core.api.tests.test_privacy_ask_routes import ASK, GOOD, hold, remote_agent, sse
from core.api.tests.test_privacy_projection import P_REN1, P_VIS1, ask_record, ev

world = routes.world
api = routes.api
OLD = "hid_old_handle"
COMMENT = "tiktok_comment_88"
STEP = f"Searching TikTok for posts by @{OLD}"
COMMENT_RECORD = {"id": COMMENT, "platform": "tiktok", "handle": "fan_one", "url": f"https://www.tiktok.com/@{OLD}/video/1",
                  "posted_at": "2026-10-05T10:00:00+02:00", "market": "ZA", "text": "great clip", "engagement": {},
                  "flags": ["market_assumed"]}


def stored_shape():
    record = ask_record()
    record["steps"] = [{"seq": 1, "text": STEP}, {"seq": 2, "text": f"Reading {OLD}'s post on TikTok"},
                       {"seq": 3, "text": "Reading 3 posts"}]
    record["answer"]["evidence"].append(dict(COMMENT_RECORD))
    record["answer"]["claims"].append({"id": "c9", "text": "A fan liked the clip", "label": "single_source",
                                       "kind": "observation", "evidence_ids": [COMMENT], "quotes": []})
    record["evidence_links"] = {"v": 1, "parents": {COMMENT: P_REN1}}
    return record


def test_the_projection_masks_the_old_handle_in_step_text_and_everywhere_i5():
    store = routes.RouteStore(hide={"c_ren"})
    out = privacy.project_record(stored_shape(), store)
    assert json.dumps(out).count(OLD) == 0
    assert "Reading 3 posts" in [s["text"] for s in out["steps"]]
    assert {e["id"] for e in out["answer"]["evidence"]} >= {P_VIS1}


def test_the_agent_reader_masks_the_old_handle_i5(world):
    world.store.hide = {"c_ren"}
    hold(stored_shape())
    assert json.dumps(world.client.get(f"/api/ask/{ASK}").json()).count(OLD) == 0


def test_the_api_stored_path_and_the_export_mask_the_old_handle_i5(api):
    api.store.hide = {"c_ren"}
    api.store.records[ASK] = stored_shape()
    client = api.serve(remote_agent({}))
    assert client.get(f"/api/ask/{ASK}", headers=GOOD).text.count(OLD) == 0
    assert client.get(f"/api/ask/{ASK}/export", headers=GOOD).text.count(OLD) == 0
    assert client.get(f"/api/ask/{ASK}/events", headers=GOOD).text.count(OLD) == 0


def test_the_live_stream_masks_the_old_handle_in_a_step_that_comes_before_its_post_i5(world):
    world.store.hide = {"c_ren"}
    events = [("step", {"text": STEP}),
              ("evidence", {"evidence": ev(P_REN1, OLD, "renamed creator words", "2026-10-05T01:00:00+02:00")}),
              ("evidence", {"evidence": dict(COMMENT_RECORD, parent_id=P_REN1)}),
              ("step", {"text": f"Reading {OLD}'s post on TikTok"}),
              ("evidence", {"evidence": ev(P_VIS1, "vis_handle", "visible fixture words")})]
    hold(ask_record(), events)
    text = world.client.get(f"/api/ask/{ASK}/events").text
    got = sse(text)
    assert text.count(OLD) == 0
    assert [d["evidence"]["id"] for k, d in got if k == "evidence"] == [P_VIS1]
    assert len([k for k, _ in got if k == "step"]) == 2


def test_a_visible_creator_is_not_masked_by_it_i5():
    store = routes.RouteStore(hide={"c_ren"})
    record = stored_shape()
    record["steps"].append({"seq": 4, "text": "Searching TikTok for posts by @vis_handle"})
    out = privacy.project_record(record, store)
    assert "@vis_handle" in json.dumps(out["steps"])
    assert agent_app is not None
