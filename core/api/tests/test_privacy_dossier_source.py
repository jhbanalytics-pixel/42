"""A dossier claim that was removed is judged against the Ask it came from, when a reader looks (release C review 5,
finding 1). A dossier holds the evidence of the claims it keeps only, so a removed claim cites posts the dossier does not
hold. Withholding every one of them while anybody is hidden told the reader, falsely, that the claim rested on a post 42
no longer shows, raised the privacy notice on the view and on the frozen export, and took the Keep button away.

The read now takes the source Ask's evidence (with its comment links) and withholds a removed claim only when it cites a
hidden person's post there. It also learns the names that evidence reveals, so a handle a hidden person once had is
masked in the question, the title and the held text as the Ask read masks it. When the source cannot be read, a removed
claim that cites a post the dossier does not hold is withheld, as before."""
import copy
import json

import pytest

from core.api import agent_app, dossiers, privacy
from core.api.export import EXPORT_NOTICE
from core.api.tests import test_privacy_ask_routes as routes
from core.api.tests.test_privacy_ask_routes import ASK, GOOD, api, hold, older_agent  # noqa: F401  (fixtures)
from core.api.tests.test_privacy_comments import comment
from core.api.tests.test_privacy_projection import P_HID1, P_REN1, P_VIS1, PrivStore, ask_record

world = routes.world
OLD = "hid_old_handle"
HID_WORDS = "fixture hidden words one"
SHOWN_C4 = "A renamed creator posted"


def reader_of(record, calls=None):
    """What a caller hands the projection: the Ask record for an ask id, as the stores hold it."""
    def read(ask_id):
        if calls is not None:
            calls.append(ask_id)
        return copy.deepcopy(record) if ask_id == record["ask_id"] else None
    return read


def view_of(record, keep=("c1",)):
    body = dossiers.build(record, {"keep": list(keep), "title": "Amapiano", "notes": {}}, dossier_id="d_fixture3", version=1,
                          created_at="2026-10-07T10:00:00+02:00", source={"ask_id": record["ask_id"]})
    return {**body, "content_hash": dossiers.content_hash(body), "ticks": {}, "needs_tick": []}


def by(view):
    return {c["claim_id"]: c for c in view["claims"]}


def visible_only():
    """An Ask whose evidence and claims are the visible creators' only: nobody hidden below is in it."""
    record = ask_record()
    record["question"] = "What is new in amapiano?"
    record["answer"]["short_answer"] = "Amapiano posts rose."
    record["answer"]["evidence"] = [e for e in record["answer"]["evidence"] if e["id"] in (P_VIS1, P_REN1)]
    record["answer"]["claims"] = [c for c in record["answer"]["claims"] if c["id"] in ("c1", "c4")]
    record["answer"]["so_what"] = [s for s in record["answer"]["so_what"] if s["claim_ids"] == ["c1"]]
    record["answer"]["watch_next"] = [w for w in record["answer"]["watch_next"] if w["claim_ids"] == ["c4"]]
    record["run"].pop("ranked_list", None)
    record["steps"] = [{"seq": 1, "text": "Reading 3 posts"}]
    return record


def assert_withheld(claim):
    assert claim["withheld"] is True and claim["text"] == privacy.CLAIM_WITHHELD
    assert claim["evidence_ids"] == [] and claim["quotes"] == []


# The projection.
def test_a_removed_claim_resting_on_a_visible_post_is_shown_and_one_resting_on_a_hidden_post_is_withheld():
    record = ask_record()
    out = privacy.project_dossier(view_of(record), PrivStore(hide={"c_hid"}), read_source=reader_of(record))
    claims = by(out)
    assert claims["c4"]["kept"] is False and claims["c4"]["text"] == SHOWN_C4 and "withheld" not in claims["c4"]
    assert claims["c4"]["evidence_ids"] == [P_REN1]
    assert_withheld(claims["c2"])
    assert_withheld(claims["c3"])
    assert out["privacy"]["withheld"] == {"posts": 0, "claims": 2, "summary": False}
    assert HID_WORDS not in json.dumps(out) and P_HID1 not in json.dumps(out)


def test_a_dossier_that_holds_nothing_of_a_hidden_person_is_shown_as_stored_with_no_notice():
    record = visible_only()
    view = view_of(record)
    out = privacy.project_dossier(view, PrivStore(hide={"c_hid"}), read_source=reader_of(record))
    assert by(out)["c4"]["text"] == SHOWN_C4 and "withheld" not in by(out)["c4"]
    assert "privacy" not in out
    assert out == view


def test_the_source_is_read_once_and_only_when_somebody_is_hidden():
    record = ask_record()
    calls = []
    privacy.project_dossier(view_of(record), PrivStore(hide={"c_hid"}), read_source=reader_of(record, calls))
    assert calls == [record["ask_id"]]
    calls.clear()
    privacy.project_dossier(view_of(record), PrivStore(hide=set()), read_source=reader_of(record, calls))
    assert calls == []


def raising(ask_id):
    raise RuntimeError("runs unreadable")


def no_answer(ask_id):
    return {"ask_id": ask_id, "answer": None}


def another_ask(ask_id):
    other = visible_only()
    other["answer"]["evidence"] = [e for e in other["answer"]["evidence"] if e["id"] == P_VIS1]
    return other


@pytest.mark.parametrize("read", [None, lambda ask_id: None, raising, no_answer, another_ask],
                         ids=["no reader", "no record", "raises", "no answer", "an Ask that does not hold the post"])
def test_when_the_source_cannot_judge_a_removed_claim_it_is_withheld(read):
    out = privacy.project_dossier(view_of(ask_record()), PrivStore(hide={"c_hid"}), read_source=read)
    claims = by(out)
    for cid in ("c2", "c3", "c4"):
        assert_withheld(claims[cid])
    assert claims["c1"]["kept"] is True and "withheld" not in claims["c1"]
    assert HID_WORDS not in json.dumps(out) and P_HID1 not in json.dumps(out)


@pytest.mark.parametrize("source_ask_id", [None, 7, "a_20261007_other"])
def test_a_dossier_whose_source_ask_id_names_no_ask_withholds_its_removed_claims(source_ask_id):
    record = ask_record()
    view = {**view_of(record), "source_ask_id": source_ask_id}
    out = privacy.project_dossier(view, PrivStore(hide={"c_hid"}), read_source=reader_of(record))
    assert_withheld(by(out)["c4"])


@pytest.mark.parametrize("source_ask_id", [None, 7])
def test_a_dossier_with_no_source_ask_id_is_not_judged_by_whatever_record_a_reader_returns(source_ask_id):
    record = ask_record()
    view = {**view_of(record), "source_ask_id": source_ask_id}
    out = privacy.project_dossier(view, PrivStore(hide={"c_hid"}), read_source=lambda ask_id: copy.deepcopy(record))
    assert_withheld(by(out)["c4"])


def test_an_unreadable_list_withholds_every_claim_whatever_the_source_says():
    record = ask_record()
    out = privacy.project_dossier(view_of(record), PrivStore(fail="list"), read_source=reader_of(record))
    assert all(c["withheld"] for c in out["claims"]) and out["privacy"]["state"] == "unavailable"


def with_a_linked_comment(parent):
    """A source Ask with a comment that has an address of its own, as the stored evidence holds it (no parent_id), the
    record's links naming the post it sits under, and a claim that rests on the comment alone."""
    record = ask_record()
    remark = comment("tiktok_comment_903", None, url="https://www.tiktok.com/@vis_handle/video/1?comment=77",
                     handle="fan_one", text="words said under a post")
    remark.pop("parent_id")
    record["answer"]["evidence"].append(remark)
    record["answer"]["claims"].append({"id": "c9", "text": "A commenter reacted", "label": "single_source", "kind": "observation",
                                       "evidence_ids": [remark["id"]], "quotes": [{"evidence_id": remark["id"], "text": remark["text"]}]})
    record["evidence_links"] = {"v": 1, "parents": {remark["id"]: parent}}
    return record


def test_a_removed_claim_citing_a_comment_the_links_put_under_a_hidden_post_is_withheld():
    record = with_a_linked_comment(P_HID1)
    out = privacy.project_dossier(view_of(record), PrivStore(hide={"c_hid"}), read_source=reader_of(record))
    assert_withheld(by(out)["c9"])
    assert "words said under a post" not in json.dumps(out)


def test_a_removed_claim_citing_a_comment_the_links_put_under_a_visible_post_is_shown():
    record = with_a_linked_comment(P_VIS1)
    out = privacy.project_dossier(view_of(record), PrivStore(hide={"c_hid"}), read_source=reader_of(record))
    assert by(out)["c9"]["text"] == "A commenter reacted" and "withheld" not in by(out)["c9"]


def test_the_names_the_source_reveals_are_masked_in_the_question_the_title_and_the_held_text():
    record = ask_record()
    record["question"] = f"What is @{OLD} doing with amapiano?"
    record["answer"]["evidence"][0]["text"] = f"visible fixture words, a shout to @{OLD}"
    record["answer"]["claims"][0]["quotes"] = []
    view = view_of(record, keep=("c1",))
    view["title"] = f"About @{OLD}"
    out = privacy.project_dossier(view, PrivStore(hide={"c_ren"}), read_source=reader_of(record))
    assert OLD not in json.dumps(out)
    assert out["question"] == "What is @*** doing with amapiano?"


def test_a_kept_claim_naming_a_handle_the_source_reveals_is_masked_though_the_dossier_holds_no_post_of_that_creator():
    record = ask_record()
    record["answer"]["claims"][0]["text"] = f"Fans of @{OLD}"
    out = privacy.project_dossier(view_of(record), PrivStore(hide={"c_ren"}), read_source=reader_of(record))
    assert OLD not in json.dumps(out)
    assert by(out)["c1"]["text"] == "Fans of @***"


def test_the_same_old_handle_is_left_alone_while_its_creator_is_not_hidden():
    record = ask_record()
    record["answer"]["claims"][0]["text"] = f"Fans of @{OLD}"
    view = view_of(record)
    out = privacy.project_dossier(view, PrivStore(hide={"c_hid"}), read_source=reader_of(record))
    assert by(out)["c1"]["text"] == f"Fans of @{OLD}"


# The routes.
def draft_before_the_hide(world, record, keep):
    world.store.hide = set()
    hold(record)
    did = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}}).json()["dossier_id"]
    assert world.client.put(f"/api/dossiers/{did}", json={"keep": keep, "from_version": 1}).status_code == 200
    return did


def test_a_visible_only_dossier_shows_its_removed_claim_with_no_notice_and_the_claim_can_be_kept_again(world):
    did = draft_before_the_hide(world, visible_only(), ["c1"])
    world.store.hide = {"c_hid"}
    view = world.client.get(f"/api/dossiers/{did}")
    claim = by(view.json())["c4"]
    assert claim["kept"] is False and claim["text"] == SHOWN_C4 and "withheld" not in claim
    assert view.json().get("privacy") is None
    assert world.client.get(f"/api/ask/{ASK}").json().get("privacy") is None
    frozen = world.client.post(f"/api/dossiers/{did}/freeze", json={"from_version": 2})
    assert frozen.status_code == 201, frozen.text
    export = world.client.get(f"/api/dossiers/{did}/versions/3/export?format=html")
    assert export.status_code == 200 and EXPORT_NOTICE not in export.text
    again = world.client.put(f"/api/dossiers/{did}", json={"keep": ["c1", "c4"], "from_version": 3})
    assert again.status_code == 200, again.text
    assert by(again.json())["c4"]["kept"] is True


def test_a_version_made_before_the_hide_withholds_only_the_removed_claims_that_rest_on_the_hidden_post(world):
    did = draft_before_the_hide(world, ask_record(), ["c1"])
    world.store.hide = {"c_hid"}
    for path in (f"/api/dossiers/{did}", f"/api/dossiers/{did}/versions/2"):
        view = world.client.get(path)
        claims = by(view.json())
        assert_withheld(claims["c2"])
        assert_withheld(claims["c3"])
        assert claims["c4"]["text"] == SHOWN_C4 and "withheld" not in claims["c4"], path
        assert view.json()["privacy"]["withheld"] == {"posts": 0, "claims": 2, "summary": False}
        for needle in (HID_WORDS, P_HID1, "The hidden creator led"):
            assert view.text.count(needle) == 0, (path, needle)


def test_a_removed_claim_is_withheld_when_the_agent_can_no_longer_read_its_source(world):
    did = draft_before_the_hide(world, visible_only(), ["c1"])
    world.store.hide = {"c_hid"}
    agent_app.ASKS.clear()  # no longer held here, and the runs table does not hold it either
    view = world.client.get(f"/api/dossiers/{did}")
    assert_withheld(by(view.json())["c4"])
    assert view.json()["privacy"]["withheld"]["claims"] == 1


def test_a_frozen_version_naming_a_handle_the_source_reveals_shows_it_in_neither_the_view_nor_the_export(world):
    record = ask_record()
    record["answer"]["claims"][0]["text"] = f"Fans of @{OLD}"
    did = draft_before_the_hide(world, record, ["c1"])
    assert world.client.post(f"/api/dossiers/{did}/freeze", json={"from_version": 2}).status_code == 201
    world.store.hide = {"c_ren"}
    view = world.client.get(f"/api/dossiers/{did}/versions/3")
    export = world.client.get(f"/api/dossiers/{did}/versions/3/export?format=html")
    assert export.status_code == 200
    assert view.text.count(OLD) == 0 and export.text.count(OLD) == 0
    assert world.client.get(f"/api/ask/{ASK}").text.count(OLD) == 0


@pytest.mark.parametrize("words", [lambda r: r["answer"]["evidence"][0].update(text=f"visible fixture words, a shout to @{OLD}"),
                                   lambda r: r.update(question=f"What is @{OLD} doing with amapiano?")],
                         ids=["held visible text", "question"])
def test_a_dossier_made_while_the_renamed_creator_is_hidden_shows_the_old_handle_nowhere(world, words):
    world.store.hide = {"c_ren"}
    record = ask_record()
    words(record)
    hold(record)
    made = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}})
    assert made.status_code == 201, made.text
    did = made.json()["dossier_id"]
    view = world.client.get(f"/api/dossiers/{did}")
    frozen = world.client.post(f"/api/dossiers/{did}/freeze", json={"from_version": 1})
    if frozen.status_code == 409:
        for claim in frozen.json().get("claims") or []:
            world.client.post(f"/api/dossiers/{did}/ticks", json={"claim_id": claim["claim_id"], "ticked": True})
        frozen = world.client.post(f"/api/dossiers/{did}/freeze", json={"from_version": 1})
    assert frozen.status_code == 201, frozen.text
    export = world.client.get(f"/api/dossiers/{did}/versions/2/export?format=html")
    assert world.client.get(f"/api/ask/{ASK}").text.count(OLD) == 0
    assert view.text.count(OLD) == 0 and export.status_code == 200 and export.text.count(OLD) == 0


# A name only a quote carries.
def test_a_claim_whose_quote_alone_carries_the_old_handle_is_stored_and_shown_without_it(world):
    world.store.hide = {"c_ren"}
    record = ask_record()
    record["answer"]["evidence"][0]["text"] = f"visible fixture words, a shout to @{OLD}"
    record["answer"]["claims"][0]["quotes"] = [{"evidence_id": P_VIS1, "text": f"a shout to @{OLD}"}]
    hold(record)
    made = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}})
    assert made.status_code == 201, made.text
    did = made.json()["dossier_id"]
    stored = [json.loads(v["body"]) if isinstance(v["body"], str) else v["body"]
              for v in agent_app.DOSSIER_VERSIONS if v["dossier_id"] == did]
    view = world.client.get(f"/api/dossiers/{did}")
    assert world.client.get(f"/api/ask/{ASK}").text.count(OLD) == 0
    kept_c1 = by(stored[0])["c1"]
    assert kept_c1["kept"] is False and kept_c1["quotes"] == [] and kept_c1["text"] == privacy.CLAIM_WITHHELD
    assert view.text.count(OLD) == 0 and made.text.count(OLD) == 0
    c1 = by(view.json())["c1"]
    assert c1["kept"] is False and c1["quotes"] == [] and c1["text"] == privacy.CLAIM_WITHHELD


def test_selectable_claims_learns_a_claim_whose_quote_alone_names_a_hidden_creators_old_handle():
    store = PrivStore(hide={"c_ren"})
    record = ask_record()
    claim = record["answer"]["claims"][0]
    claim["quotes"] = [{"evidence_id": P_VIS1, "text": f"a shout to @{OLD}"}]
    record["answer"]["evidence"][0]["text"] = f"visible fixture words, a shout to @{OLD}"
    learned = set()
    ok = privacy.selectable_claims({"c1": claim}, record["answer"]["evidence"], privacy.read_hidden(store), store, learned)
    assert ok["c1"] is False and learned == {"c1"}


# The API's second pass reads the same source, or it would withhold again what the agent showed.
def agents_view(record, store):
    return json.loads(json.dumps(privacy.project_dossier(view_of(record), store, read_source=reader_of(record))))


def test_the_second_pass_shows_a_removed_claim_the_agent_showed_when_it_can_read_the_source(api):  # noqa: F811
    record = ask_record()
    api.store.records[ASK] = record
    first = agents_view(record, api.store)
    assert by(first)["c4"]["text"] == SHOWN_C4
    body = api.serve(older_agent({"/api/dossiers/d_fixture3": (200, first)})).get("/api/dossiers/d_fixture3", headers=GOOD).json()
    claims = by(body)
    assert claims["c4"]["text"] == SHOWN_C4 and "withheld" not in claims["c4"]
    assert_withheld(claims["c2"])
    assert body["privacy"]["withheld"]["claims"] == 2


def test_the_second_pass_withholds_a_removed_claim_when_it_cannot_read_the_source(api):  # noqa: F811
    record = ask_record()
    first = agents_view(record, api.store)
    assert api.store.records == {}
    body = api.serve(older_agent({"/api/dossiers/d_fixture3": (200, first)})).get("/api/dossiers/d_fixture3", headers=GOOD).json()
    assert_withheld(by(body)["c4"])


def test_the_second_pass_leaves_a_visible_only_dossier_with_no_notice(api):  # noqa: F811
    record = visible_only()
    api.store.records[ASK] = record
    first = agents_view(record, api.store)
    assert "privacy" not in first
    body = api.serve(older_agent({"/api/dossiers/d_fixture3": (200, first)})).get("/api/dossiers/d_fixture3", headers=GOOD).json()
    assert "privacy" not in body and by(body)["c4"]["text"] == SHOWN_C4
