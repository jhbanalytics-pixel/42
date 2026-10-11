"""A skin dossier is projected for privacy before the skin masks it, as an Ask record is. The skin strips the handle of
every author it may not name, and a suppression row that names only a handle then matches nothing. Same shape as the
Ask check: a person hidden by a handle-only row, a skin that does not approve them, a draft or frozen version made
before the hide."""
import json

from core.api import agent_app
from core.api.tests import test_privacy_dossiers as dossier_tests
from core.api.tests.test_privacy_ask_routes import ASK, hold
from core.api.tests.test_privacy_projection import P_HID1, ask_record, handle_row, leaks

world = dossier_tests.world
WORDS = "fixture hidden words one"


def skin_dossier(world, freeze=False):
    """A dossier of a skin run made while nobody was hidden; the hide comes after."""
    record = dossier_tests.source()
    record["skin_id"] = "s_fixture"
    hold(record)
    did = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}}).json()["dossier_id"]
    assert world.client.put(f"/api/dossiers/{did}", json={"keep": ["c1", "c2", "c3"], "from_version": 1}).status_code == 200
    if freeze:
        for cid in ("c2",):
            world.client.post(f"/api/dossiers/{did}/ticks", json={"claim_id": cid, "ticked": True})
        assert world.client.post(f"/api/dossiers/{did}/freeze", json={}).status_code == 201
    world.store.hide = set()
    world.store.suppression_rows = [handle_row()]  # the row names the platform and the handle only
    return did


def test_the_skin_dossier_is_a_skin_dossier_so_the_check_means_something(world):
    did = skin_dossier(world)
    assert dossier_tests.versions(did)[-1]["people"]["approved"] == []


def test_a_skin_dossier_view_withholds_a_handle_only_suppressed_post(world):
    did = skin_dossier(world)
    view = world.client.get(f"/api/dossiers/{did}").json()
    assert P_HID1 not in [e["id"] for e in view["evidence"]]
    assert WORDS not in json.dumps(view) and leaks(view, [P_HID1]) == []


def test_a_frozen_skin_dossier_version_and_its_export_withhold_it_too(world):
    did = skin_dossier(world, freeze=True)
    for url in (f"/api/dossiers/{did}", f"/api/dossiers/{did}/versions/{len(dossier_tests.versions(did))}"):
        assert WORDS not in world.client.get(url).text
    exported = world.client.get(f"/api/dossiers/{did}/versions/{len(dossier_tests.versions(did))}/export")
    assert exported.status_code == 200 and WORDS not in exported.text


def test_a_skin_dossier_still_masks_an_author_the_skin_may_not_name(world):
    did = skin_dossier(world)
    assert "vis_handle" not in json.dumps(world.client.get(f"/api/dossiers/{did}").json()["evidence"])
    assert ask_record() and agent_app
