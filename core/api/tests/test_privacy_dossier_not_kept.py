"""A claim a dossier does not keep is stored and shown without its words when they would name, or rest on, a hidden
person (release C review 4, findings 1 and 2, and the privacy notice). Three gaps are pinned here.

A claim whose words name the hidden creator's current handle as well as the old one was never learned, so a dossier
stored it as written and showed the old handle the Ask read masks. A claim not kept that rests on a hidden post kept its
words, its verbatim quote and the post id, because a dossier holds only the evidence of the claims it keeps, so the
hidden post was never among what the dossier could judge. And a dossier made before the hide shows those words again
unless the read itself withholds a claim not kept whose evidence the dossier does not hold. The notice that some content was
left out is raised only when the projection withheld something or masked a hidden person's handle, not when the step rule
masked a handle it could not clear."""
import json

from core.api import agent_app, dossiers, privacy
from core.api.tests import test_privacy_ask_routes as routes
from core.api.tests.test_privacy_ask_routes import ASK, GOOD, hold, remote_agent
from core.api.tests.test_privacy_projection import P_HID1, P_REN1, P_VIS1, PrivStore, ask_record, ev
from core.api.export import EXPORT_NOTICE

world = routes.world
api = routes.api
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


# Finding 2: a claim not kept that rests on a hidden post.
def test_a_claim_not_kept_that_rests_on_a_hidden_post_is_stored_without_its_words_quote_and_post_id(world):
    response, did = made(world, ask_record(), {"c_hid"})
    ask = world.client.get(f"/api/ask/{ASK}").text
    view = world.client.get(f"/api/dossiers/{did}")
    stored = json.dumps(stored_of(did), default=str)
    for needle in (HID_WORDS, P_HID1, "The hidden creator led"):
        assert ask.count(needle) == 0
        assert response.text.count(needle) == 0 and view.text.count(needle) == 0 and stored.count(needle) == 0
    by = rows(view.json())
    assert_withheld_in_full(by["c2"])
    assert_withheld_in_full(by["c3"])
    assert by["c1"]["kept"] is True and by["c1"]["quotes"][0]["text"] == "visible fixture words"


def test_a_claim_not_kept_that_cites_the_renamed_creators_post_and_names_the_old_handle_is_stored_without_both(world):
    record = ask_record()
    record["answer"]["claims"][3].update(text=f"A renamed creator posted, @{OLD}",
                                         quotes=[{"evidence_id": P_REN1, "text": REN_WORDS}])
    response, did = made(world, record, {"c_ren"})
    view = world.client.get(f"/api/dossiers/{did}")
    stored = json.dumps(stored_of(did), default=str)
    for needle in (OLD, REN_WORDS, P_REN1):
        assert response.text.count(needle) == 0 and view.text.count(needle) == 0 and stored.count(needle) == 0
    assert_withheld_in_full(rows(view.json())["c4"])


def test_an_edit_made_while_somebody_is_hidden_stores_the_new_version_without_those_words(world):
    world.store.hide = set()
    hold(ask_record())
    did = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}}).json()["dossier_id"]
    world.store.hide = {"c_hid"}
    edited = world.client.put(f"/api/dossiers/{did}", json={"keep": ["c1"], "title": "Renamed", "from_version": 1})
    assert edited.status_code == 200, edited.text
    latest = stored_of(did)[-1]
    text = json.dumps(latest, default=str)
    for needle in (HID_WORDS, P_HID1, "The hidden creator led"):
        assert text.count(needle) == 0 and edited.text.count(needle) == 0
    assert_withheld_in_full({c["claim_id"]: c for c in latest["claims"]}["c2"])


def test_a_claim_not_kept_whose_words_are_clean_is_stored_as_written(world):
    _, did = made(world, ask_record(), {"c_hid"})
    stored = {c["claim_id"]: c for c in stored_of(did)[0]["claims"]}
    assert stored["c4"]["kept"] is True  # the renamed creator is not hidden here, so the default route keeps it
    removed = world.client.put(f"/api/dossiers/{did}", json={"keep": ["c1"], "from_version": 1})
    assert removed.status_code == 200
    stored = {c["claim_id"]: c for c in stored_of(did)[-1]["claims"]}
    assert stored["c4"]["kept"] is False and stored["c4"]["text"] == "A renamed creator posted"
    assert stored["c4"]["evidence_ids"] == [P_REN1]


def test_only_the_claims_that_rest_on_a_hidden_post_are_learned():
    store = PrivStore(hide={"c_hid"})
    record = ask_record()
    claims = {c["id"]: c for c in record["answer"]["claims"]}
    learned = set()
    privacy.selectable_claims(claims, record["answer"]["evidence"], privacy.read_hidden(store), store, learned)
    assert learned == {"c2", "c3"}


def test_a_withheld_claim_is_stored_with_the_keys_of_any_other_claim(world):
    _, did = made(world, ask_record(), {"c_hid"})
    by = {c["claim_id"]: c for c in stored_of(did)[0]["claims"]}
    assert set(by["c2"]) == set(by["c4"]) and "withheld" not in by["c2"]


# A dossier made before the hide.
def draft_before_the_hide(world, keep):
    world.store.hide = set()
    hold(ask_record())
    did = world.client.post("/api/dossiers", json={"from": {"ask_id": ASK}}).json()["dossier_id"]
    assert world.client.put(f"/api/dossiers/{did}", json={"keep": keep, "from_version": 1}).status_code == 200
    return did


def test_a_version_made_before_the_hide_withholds_a_claim_not_kept_that_rests_on_the_hidden_post(world):
    did = draft_before_the_hide(world, ["c1"])
    world.store.hide = {"c_hid"}
    for path in (f"/api/dossiers/{did}", f"/api/dossiers/{did}/versions/2"):
        view = world.client.get(path)
        for needle in (HID_WORDS, P_HID1, "The hidden creator led"):
            assert view.text.count(needle) == 0, (path, needle)
        by = rows(view.json())
        assert by["c2"]["withheld"] is True and by["c2"]["text"] == privacy.CLAIM_WITHHELD
        assert by["c1"]["kept"] is True
        assert view.json()["privacy"]["state"] == "applied"
    stored = json.dumps(stored_of(did)[1], default=str)
    assert stored.count(HID_WORDS) == 1  # the stored body is never rewritten by a read


def test_a_claim_not_kept_that_cites_a_post_the_dossier_does_not_hold_is_withheld_while_somebody_is_hidden():
    view = dossiers_view_with_removed_claims()
    out = privacy.project_dossier(view, PrivStore(hide={"c_hid"}))
    by = {c["claim_id"]: c for c in out["claims"]}
    assert by["c2"]["withheld"] is True and by["c4"]["withheld"] is True
    assert by["c1"]["kept"] is True and "withheld" not in by["c1"]
    assert HID_WORDS not in json.dumps(out) and P_HID1 not in json.dumps(out)


def test_a_claim_not_kept_that_cites_a_post_the_dossier_holds_or_none_is_shown_as_stored():
    view = dossiers_view_with_removed_claims()
    held = ev(P_VIS1, "vis_handle", "visible fixture words")
    assert [e["id"] for e in view["evidence"]] == [P_VIS1]
    view["claims"][1].update(evidence_ids=[P_VIS1], quotes=[], text="Not kept, cites a held visible post")
    view["claims"][2].update(evidence_ids=[], quotes=[], text="Not kept, cites nothing")
    out = privacy.project_dossier(view, PrivStore(hide={"c_hid"}))
    by = {c["claim_id"]: c for c in out["claims"]}
    assert by["c2"]["text"] == "Not kept, cites a held visible post" and "withheld" not in by["c2"]
    assert by["c3"]["text"] == "Not kept, cites nothing" and "withheld" not in by["c3"]
    assert held["id"] in json.dumps(out)


def test_nobody_hidden_shows_a_claim_not_kept_as_stored():
    view = dossiers_view_with_removed_claims()
    assert privacy.project_dossier(view, PrivStore(hide=set())) == view


def dossiers_view_with_removed_claims():
    record = ask_record()
    body = dossiers.build(record, {"keep": ["c1"], "title": "Amapiano", "notes": {}}, dossier_id="d_fixture2", version=1,
                          created_at="2026-10-07T10:00:00+02:00", source={"ask_id": record["ask_id"]})
    return {**body, "content_hash": dossiers.content_hash(body), "ticks": {}, "needs_tick": []}


# The privacy notice.
def plain_record():
    record = ask_record()
    record["question"] = "What is new in amapiano?"
    record["answer"]["short_answer"] = "Amapiano posts rose."
    record["answer"]["evidence"] = [e for e in record["answer"]["evidence"] if e["id"] == P_VIS1]
    record["answer"]["claims"] = [c for c in record["answer"]["claims"] if c["id"] == "c1"]
    record["answer"]["so_what"] = [s for s in record["answer"]["so_what"] if s["claim_ids"] == ["c1"]]
    record["answer"]["watch_next"] = []
    record["run"].pop("ranked_list", None)
    record["steps"] = [{"seq": 1, "text": "Searching TikTok for posts by @dj_maphorisa"}, {"seq": 2, "text": "Reading 3 posts"}]
    return record


def test_a_step_naming_an_uncited_visible_creator_is_masked_without_raising_the_notice():
    out = privacy.project_record(plain_record(), PrivStore(hide={"c_hid"}))
    assert out["steps"][0]["text"] == "Searching TikTok for posts by @***"
    assert "privacy" not in out


def test_the_agent_read_of_such_an_answer_carries_no_notice(world):
    world.store.hide = {"c_hid"}
    hold(plain_record())
    body = world.client.get(f"/api/ask/{ASK}").json()
    assert body.get("privacy") is None and body["steps"][0]["text"] == "Searching TikTok for posts by @***"


def test_the_export_of_such_an_answer_carries_no_notice(api):
    api.store.hide = {"c_hid"}
    api.store.records[ASK] = plain_record()
    client = api.serve(remote_agent({}))
    read = client.get(f"/api/ask/{ASK}", headers=GOOD)
    export = client.get(f"/api/ask/{ASK}/export?format=html", headers=GOOD)
    assert export.status_code == 200 and read.json().get("privacy") is None
    assert EXPORT_NOTICE not in export.text


def test_a_step_naming_a_hidden_handle_still_raises_the_notice():
    record = plain_record()
    record["steps"] = [{"seq": 1, "text": "Searching TikTok for posts by @hid_handle"}]
    out = privacy.project_record(record, PrivStore(hide={"c_hid"}))
    assert out["steps"][0]["text"] == "Searching TikTok for posts by @***"
    assert out["privacy"] == {"v": 1, "state": "applied", "withheld": {"posts": 0, "claims": 0, "summary": False}}


def test_a_question_naming_a_hidden_handle_still_raises_the_notice():
    record = plain_record()
    record["question"] = "What is @hid_handle doing?"
    out = privacy.project_record(record, PrivStore(hide={"c_hid"}))
    assert "hid_handle" not in json.dumps(out) and out["privacy"]["state"] == "applied"


# The step rule, pinned where the review's mutants survived.
MISS = "obs1_" + "9" * 32


def test_a_step_naming_the_author_of_a_post_the_lookup_could_not_place_stays_masked():
    record = plain_record()
    record["answer"]["evidence"].append(ev(MISS, "ghost_handle", "ghost words"))
    record["steps"] = [{"seq": 1, "text": "Searching TikTok for posts by @ghost_handle"}]
    out = privacy.project_record(record, PrivStore(hide={"c_hid"}))
    assert MISS not in {e["id"] for e in out["answer"]["evidence"]}
    assert out["steps"][0]["text"] == "Searching TikTok for posts by @***"


def test_a_step_names_a_creator_whose_kept_post_carries_the_handle_only_in_its_address():
    record = plain_record()
    kept = record["answer"]["evidence"][0]
    kept.pop("handle")
    kept["url"] = "https://www.tiktok.com/@url_only_handle/video/1"
    record["steps"] = [{"seq": 1, "text": "Searching TikTok for posts by @url_only_handle"},
                       {"seq": 2, "text": "Searching TikTok for posts by @other_handle"}]
    out = privacy.project_record(record, PrivStore(hide={"c_hid"}))
    assert [s["text"] for s in out["steps"]] == ["Searching TikTok for posts by @url_only_handle",
                                                 "Searching TikTok for posts by @***"]


def test_a_later_step_naming_the_old_handle_is_masked_like_the_first():
    record = plain_record()
    record["answer"]["evidence"] = [e for e in ask_record()["answer"]["evidence"] if e["id"] == P_VIS1]
    record["steps"] = [{"seq": 1, "text": "Planning the search"},
                       {"seq": 2, "text": "Searching TikTok for posts by @vis_handle"},
                       {"seq": 3, "text": f"Searching TikTok for posts by @{OLD}"}]
    out = privacy.project_record(record, PrivStore(hide={"c_ren"}))
    assert [s["text"] for s in out["steps"]] == ["Planning the search", "Searching TikTok for posts by @vis_handle",
                                                 "Searching TikTok for posts by @***"]
