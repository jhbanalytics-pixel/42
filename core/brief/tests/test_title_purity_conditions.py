import pytest

from core.brief import title_purity
from core.brief.tests import test_brief_evidence as evidence_cases
from core.brief.tests import test_title_purity as cases
from core.brief.tests.test_brief_explain import run


@pytest.mark.parametrize("supported", [6, 7])
def test_half_is_not_enough_for_twelve_members(monkeypatch, supported):
    snapshot, pack = cases.reader(monkeypatch, n=12)
    assert snapshot["N"] == 12 and len(snapshot["records"]) == 12
    pack["title_snapshot"] = snapshot

    def bound(out):
        out["member_checks"] = out["member_checks"][:supported]
    result = run(cases.MemberModel(bound), pack=pack, candidate=cases.ROW)
    audit = result["title_majority"]
    assert audit["N"] == 12 and audit["S"] == supported and audit["U"] == 12 - supported
    assert (result["title_written"] is not None) == (supported == 7)
    assert audit["decision"] == ("pass" if supported == 7 else "unknown")


@pytest.mark.parametrize("fake", ["empty quote", "space quote", "spaces quote", "partial verdict"])
def test_fake_member_support_is_never_counted(monkeypatch, fake):
    snapshot, pack = cases.reader(monkeypatch, n=3)
    pack["title_snapshot"] = snapshot

    def change(out):
        for check in out["member_checks"]:
            if fake == "empty quote":
                check["quote"] = ""
            elif fake == "space quote":
                check["quote"] = " "
            elif fake == "spaces quote":
                check["quote"] = "   "
            else:
                check["verdict"] = "partial"
    result = run(cases.MemberModel(change), pack=pack, candidate=cases.ROW)
    audit = result["title_majority"]
    assert result["title_written"] is None and result["reason"] is None
    assert audit["N"] == 3 and audit["S"] == 0 and audit["U"] == 3
    assert audit["decision"] != "pass"
    assert len(audit["member_receipts"]) == 3
    assert all(not r["validated_support"] and r["quote_span"] is None for r in audit["member_receipts"])


def test_build_pack_attaches_the_producer_snapshot_to_a_topic_row_only():
    topic = evidence_cases.world(kind="topic")
    pack, *_ = evidence_cases.build(topic)
    assert "title_snapshot" in pack
    assert pack["title_snapshot"]["status"] == "unknown"
    plain = evidence_cases.world(kind="hashtag")
    pack, *_ = evidence_cases.build(plain)
    assert "title_snapshot" not in pack


def test_the_title_request_carries_the_final_pack_hash_and_the_receipt_matches(monkeypatch):
    snapshot, pack = cases.reader(monkeypatch, n=3)
    pack["title_snapshot"] = snapshot
    expected = title_purity.digest(sorted(
        ({"post_id": r["id"], "text_hash": title_purity.digest(r.get("quote_text") or r.get("text") or "")}
         for r in pack["evidence"]), key=lambda r: str(r["post_id"])))
    assert expected and "final_pack_hash" not in snapshot
    model = cases.MemberModel()
    result = run(model, pack=pack, candidate=cases.ROW)
    title_call = model.calls[-1]
    assert "member_checks" in title_call["schema"]["properties"]
    assert f"Final pack hash: {expected}." in title_call["user"]
    assert result["title_majority"]["decision"] == "pass"
    assert result["title_majority"]["final_pack_hash"] == expected
