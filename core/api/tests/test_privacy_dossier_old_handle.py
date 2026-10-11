"""A durable copy keeps only what the Ask read shows (release C review 3, finding 3). A visible claim cites a visible post
and its words name the old handle of a creator who was renamed and is hidden. The Ask read masks that handle because the
record also holds the hidden creator's post, which says what they used to be called. What a dossier keeps, and what a
finding is built from, is judged with the same knowledge, so a dossier never stores or shows a handle the Ask read masks.
A dossier holds no hidden post to learn the name from, so a claim it does not keep is stored without its words."""
import json

from core.api import agent_app, privacy
from core.api.tests import test_privacy_ask_routes as routes
from core.api.tests.test_privacy_ask_routes import ASK, hold
from core.api.tests.test_privacy_projection import ask_record

world = routes.world
OLD = "hid_old_handle"
WORDS = f"Amapiano posts rose, led by fans of @{OLD}"


def named_record():
    record = ask_record()
    record["answer"]["claims"][0]["text"] = WORDS
    return record


def claim_rows(view):
    return {c["claim_id"]: c for c in view["claims"]}


def test_the_ask_read_masks_the_old_handle_the_dossier_would_copy(world):
    world.store.hide = {"c_ren"}
    hold(named_record())
    read = world.client.get(f"/api/ask/{ASK}").json()
    assert [c["text"] for c in read["answer"]["claims"] if c["id"] == "c1"] == ["Amapiano posts rose, led by fans of @***"]


def test_a_dossier_made_from_such_an_ask_neither_keeps_nor_shows_the_old_handle(world):
    world.store.hide = {"c_ren"}
    hold(named_record())
    made = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}})
    assert made.status_code == 201
    did = made.json()["dossier_id"]
    view = world.client.get(f"/api/dossiers/{did}")
    stored = [v for v in agent_app.DOSSIER_VERSIONS if v["dossier_id"] == did]
    assert made.text.count(OLD) == 0 and view.text.count(OLD) == 0 and json.dumps(stored, default=str).count(OLD) == 0
    c1 = claim_rows(view.json())["c1"]
    assert c1["kept"] is False and c1["text"] == privacy.CLAIM_WITHHELD and c1["quotes"] == [] and c1["evidence_ids"] == []
    assert claim_rows(view.json())["c3"]["kept"] is True


def test_asking_to_keep_the_claim_is_refused(world):
    world.store.hide = {"c_ren"}
    hold(named_record())
    did = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}}).json()["dossier_id"]
    refused = world.client.put(f"/api/dossiers/{did}", json={"keep": ["c1"], "from_version": 1})
    assert refused.status_code == 409 and refused.text.count(OLD) == 0


def test_an_edit_made_after_the_rename_is_hidden_does_not_carry_the_old_handle_forward(world):
    world.store.hide = set()
    hold(named_record())
    did = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}}).json()["dossier_id"]
    assert OLD in world.client.get(f"/api/dossiers/{did}").text  # nobody is hidden yet: the words are fine to keep
    world.store.hide = {"c_ren"}
    stuck = world.client.put(f"/api/dossiers/{did}", json={"title": "Renamed", "from_version": 1})
    assert stuck.status_code == 409 and stuck.text.count(OLD) == 0
    ok = world.client.put(f"/api/dossiers/{did}", json={"keep": [], "title": "Renamed", "from_version": 1})
    assert ok.status_code == 200
    latest = [v for v in agent_app.DOSSIER_VERSIONS if v["dossier_id"] == did][-1]
    assert json.dumps(latest, default=str).count(OLD) == 0 and ok.text.count(OLD) == 0


def test_the_claim_is_not_keepable_and_blocks_a_finding():
    store = routes.RouteStore(hide={"c_ren"})
    record = named_record()
    claims = {c["id"]: c for c in record["answer"]["claims"]}
    learned = set()
    kept = privacy.selectable_claims({"c1": claims["c1"]}, record["answer"]["evidence"], privacy.read_hidden(store), store, learned)
    assert kept == {"c1": False} and learned == {"c1"}
    blocked, named = privacy.finding_blockers(record, store)
    assert "c1" in blocked and named is False


def test_a_claim_with_no_old_handle_is_still_keepable_and_not_withheld():
    store = routes.RouteStore(hide={"c_ren"})
    record = ask_record()
    claims = {c["id"]: c for c in record["answer"]["claims"]}
    learned = set()
    kept = privacy.selectable_claims({"c1": claims["c1"]}, record["answer"]["evidence"], privacy.read_hidden(store), store, learned)
    assert kept == {"c1": True} and learned == set()
    assert "c1" not in privacy.finding_blockers(record, store)[0]


def test_a_claim_that_rests_on_a_hidden_post_is_learned_so_a_dossier_that_does_not_keep_it_stores_no_words():
    store = routes.RouteStore(hide={"c_hid"})
    record = ask_record()
    claims = {c["id"]: c for c in record["answer"]["claims"]}
    learned = set()
    kept = privacy.selectable_claims({"c2": claims["c2"]}, record["answer"]["evidence"], privacy.read_hidden(store), store, learned)
    assert kept == {"c2": False} and learned == {"c2"}


def test_a_question_that_names_the_old_handle_blocks_a_finding():
    store = routes.RouteStore(hide={"c_ren"})
    record = ask_record()
    record["question"] = f"What is @{OLD} doing with amapiano?"
    assert privacy.finding_blockers(record, store)[1] is True
