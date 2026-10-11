"""Privacy comments review 2, finding 2: with nobody hidden, nothing is gone, so an unlinked comment is no reason to refuse
a dossier, a freeze or a finding."""
from core.api import privacy
from core.api.tests import test_privacy_dossiers as dossier_tests
from core.api.tests.test_privacy_ask_routes import ASK
from core.api.tests.test_privacy_projection import P_VIS1

world = dossier_tests.world

COMMENT = "tiktok_comment_77"
COMMENT_EVIDENCE = {"id": COMMENT, "platform": "tiktok", "handle": "a_commenter", "text": "agreed, the log drum rules",
                    "url": "https://www.tiktok.com/@some_other_account/video/99?comment=1",  # matches no post of the answer
                    "posted_at": "2026-10-05T09:00:00+02:00", "market": "ZA"}
K_COMMENT = {"id": "c9", "text": "A commenter agreed the log drum rules", "label": "single_source", "kind": "observation",
             "evidence_ids": [COMMENT], "quotes": [{"evidence_id": COMMENT, "text": "agreed, the log drum rules"}]}


def record():
    return dossier_tests.source([K_COMMENT], [COMMENT_EVIDENCE])


def test_evidence_gone_is_empty_when_nobody_is_hidden():
    assert privacy.evidence_gone([COMMENT_EVIDENCE], (set(), set(), {}), None) == set()


def test_an_unlinked_comment_claim_is_selectable_when_nobody_is_hidden(world):
    rec = record()
    claims = {c["id"]: c for c in rec["answer"]["claims"]}
    hidden = privacy.read_hidden(world.store)
    assert privacy.nothing_hidden(hidden)
    assert privacy.selectable_claims(claims, rec["answer"]["evidence"], hidden, world.store)["c9"] is True


def test_a_finding_is_not_blocked_by_an_unlinked_comment_when_nobody_is_hidden(world):
    assert privacy.finding_blockers(record(), world.store) == ([], False)


def test_a_dossier_keeps_an_unlinked_comment_claim_and_freezes_when_nobody_is_hidden(world):
    r = dossier_tests.make_dossier(world, record=record())
    assert r.status_code == 201
    did = r.json()["dossier_id"]
    assert "c9" in [c["claim_id"] for c in dossier_tests.versions(did)[-1]["claims"] if c["kept"]]
    assert world.client.put(f"/api/dossiers/{did}", json={"keep": ["c9"], "from_version": 1}).status_code == 200
    assert world.client.post(f"/api/dossiers/{did}/ticks", json={"claim_id": "c9", "ticked": True}).status_code == 201
    assert world.client.post(f"/api/dossiers/{did}/freeze", json={}).status_code == 201


def test_a_hide_still_withholds_the_unlinked_comment(world):
    world.store.hide = {"c_hid"}
    rec = record()
    claims = {c["id"]: c for c in rec["answer"]["claims"]}
    hidden = privacy.read_hidden(world.store)
    assert privacy.selectable_claims(claims, rec["answer"]["evidence"], hidden, world.store)["c9"] is False
    assert ASK and P_VIS1  # the shared fixture ids this record is built on
