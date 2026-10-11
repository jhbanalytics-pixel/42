"""A claim a dossier does not keep is stored without its words when the handles the source evidence reveals mask them
further than the list alone does (release C review 4, finding 1).

A claim whose words name the hidden creator's current handle as well as the old one was never learned, so a dossier
stored it as written and showed the old handle the Ask read masks."""
import json

from core.api import agent_app, privacy
from core.api.tests import test_privacy_ask_routes as routes
from core.api.tests.test_privacy_ask_routes import ASK, hold
from core.api.tests.test_privacy_projection import PrivStore, ask_record

world = routes.world
OLD = "hid_old_handle"
NEW = "hid_new_handle"
HID_WORDS = "fixture hidden words one"
REN_WORDS = "renamed creator words"


def stored_of(did):
    return [json.loads(v["body"]) if isinstance(v["body"], str) else v["body"]
            for v in agent_app.DOSSIER_VERSIONS if v["dossier_id"] == did]


def rows(view):
    return {c["claim_id"]: c for c in view["claims"]}


def with_c1_words(text):
    record = ask_record()
    record["answer"]["claims"][0]["text"] = text
    return record


def made(world, record, hide):
    world.store.hide = set(hide)
    hold(record)
    response = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}})
    assert response.status_code == 201, response.text
    return response, response.json()["dossier_id"]


def assert_withheld_in_full(claim):
    assert claim["kept"] is False and claim["text"] == privacy.CLAIM_WITHHELD
    assert claim["evidence_ids"] == [] and claim["quotes"] == []
    assert claim.get("basis", "") == "" and claim.get("falsifier", "") == ""


# Finding 1: words that name the current and the old handle.
def test_a_claim_naming_the_current_and_the_old_handle_is_stored_and_shown_without_either(world):
    response, did = made(world, with_c1_words(f"Fans of @{NEW}, once @{OLD}"), {"c_ren"})
    ask = world.client.get(f"/api/ask/{ASK}").text
    view = world.client.get(f"/api/dossiers/{did}")
    stored = json.dumps(stored_of(did), default=str)
    assert ask.count(OLD) == 0 and ask.count(NEW) == 0  # the Ask read shows neither
    for text in (response.text, view.text, stored):
        assert text.count(OLD) == 0 and text.count(NEW) == 0
    assert_withheld_in_full(rows(view.json())["c1"])


def test_a_claim_naming_a_hidden_handle_and_an_old_one_is_stored_and_shown_without_either(world):
    response, did = made(world, with_c1_words(f"Fans of @hid_handle and @{OLD}"), {"c_hid", "c_ren"})
    view = world.client.get(f"/api/dossiers/{did}")
    stored = json.dumps([b["claims"] for b in stored_of(did)], default=str)
    for text in (response.text, view.text, stored):
        assert text.count(OLD) == 0 and text.count("hid_handle") == 0
    assert_withheld_in_full(rows(view.json())["c1"])


def test_the_old_handle_alone_is_still_stored_without_its_words(world):
    _, did = made(world, with_c1_words(f"Fans of @{OLD}"), {"c_ren"})
    assert json.dumps(stored_of(did), default=str).count(OLD) == 0
    assert_withheld_in_full(rows(world.client.get(f"/api/dossiers/{did}").json())["c1"])


def test_a_claim_with_the_old_handle_only_in_its_basis_or_falsifier_is_stored_without_them(world):
    record = ask_record()
    record["answer"]["claims"][0].update(basis=f"Counted by @{OLD}", falsifier=f"If @{OLD} stops")
    _, did = made(world, record, {"c_ren"})
    assert json.dumps(stored_of(did), default=str).count(OLD) == 0
    stored = {c["claim_id"]: c for c in stored_of(did)[0]["claims"]}["c1"]
    assert stored["basis"] == "" and stored["falsifier"] == ""


def test_selectable_claims_learns_every_claim_the_list_does_not_let_a_dossier_keep_whose_words_the_source_masks():
    store = PrivStore(hide={"c_ren"})
    record = ask_record()
    record["answer"]["claims"][0]["text"] = f"Fans of @{NEW}, once @{OLD}"
    claims = {c["id"]: c for c in record["answer"]["claims"]}
    learned = set()
    ok = privacy.selectable_claims(claims, record["answer"]["evidence"], privacy.read_hidden(store), store, learned)
    assert ok["c1"] is False and "c1" in learned


def test_a_claim_whose_words_the_list_alone_masks_is_not_learned_and_the_read_shows_none_of_it(world):
    _, did = made(world, with_c1_words("Fans of @hid_handle"), {"c_hid"})
    stored = {c["claim_id"]: c for c in stored_of(did)[0]["claims"]}["c1"]
    assert stored["kept"] is False and stored["text"] == "Fans of @hid_handle"
    assert world.client.get(f"/api/dossiers/{did}").text.count("hid_handle") == 0
    store = PrivStore(hide={"c_hid"})
    record = with_c1_words("Fans of @hid_handle")
    claims = {c["id"]: c for c in record["answer"]["claims"]}
    learned = set()
    privacy.selectable_claims({"c1": claims["c1"]}, record["answer"]["evidence"], privacy.read_hidden(store), store, learned)
    assert learned == set()
