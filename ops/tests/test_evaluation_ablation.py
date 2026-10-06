import copy

import pytest

from ops.evaluation.source_ablation import (
    ablate_source_family,
    canonical_packet_sha256,
    negative_control,
)


def receipt(family, passage="p1"):
    return {"source_family": family, "passage_id": passage}


def claim(claim_id, verdict, *receipts):
    return {"claim_id": claim_id, "verdict": verdict, "receipts": list(receipts)}


def result():
    return {
        "case_id": "discovery-01",
        "packet_sources": ["social_video", "news", "search"],
        "material_claims": [
            claim("c1", "supported", receipt("social_video")),
            claim("c2", "supported", receipt("social_video"), receipt("news", "p2")),
            claim("c3", "contradicted", receipt("social_video")),
            claim("c4", "unverified"),
        ],
    }


def test_ablation_removes_the_family_and_recomputes_support():
    outcome = ablate_source_family(result(), "social_video")
    assert outcome["source_family"] == "social_video"
    assert outcome["packet_changed"] is True
    assert outcome["supported_before"] == 2
    assert outcome["supported_after"] == 1
    assert outcome["unique_supported_contribution"] == 1
    by_id = {row["claim_id"]: row for row in outcome["claims"]}
    assert by_id["c1"] == {
        "claim_id": "c1",
        "verdict_before": "supported",
        "verdict_after": "unverified",
        "receipts_removed": 1,
        "receipts_remaining": 0,
    }
    assert by_id["c2"]["verdict_after"] == "supported"
    assert by_id["c2"]["receipts_remaining"] == 1
    assert by_id["c3"]["verdict_after"] == "contradicted"
    assert by_id["c4"]["verdict_after"] == "unverified"
    assert outcome["packet_sources_after"] == ["news", "search"]


def test_ablation_is_pure():
    before = result()
    frozen = copy.deepcopy(before)
    ablate_source_family(before, "news")
    assert before == frozen


def test_ablating_a_family_absent_from_the_packet_is_not_an_ablation():
    with pytest.raises(ValueError, match="ablation_source_absent"):
        ablate_source_family(result(), "radio")


def test_uncited_but_present_family_is_still_a_real_ablation():
    outcome = ablate_source_family(result(), "search")
    assert outcome["packet_changed"] is True
    assert outcome["unique_supported_contribution"] == 0
    assert outcome["supported_after"] == outcome["supported_before"]


def test_negative_control_requires_a_truly_absent_family():
    control = negative_control(result(), "radio")
    assert control["packet_changed"] is False
    assert control["claims_changed"] == 0
    assert control["canonical_packet_sha256"] == canonical_packet_sha256(result())
    with pytest.raises(ValueError, match="negative_control_source_present"):
        negative_control(result(), "search")
    with pytest.raises(ValueError, match="negative_control_source_present"):
        negative_control(result(), "news")


def test_canonical_digest_ignores_key_order_only():
    base = result()
    reordered = {key: base[key] for key in reversed(list(base))}
    assert canonical_packet_sha256(base) == canonical_packet_sha256(reordered)
    changed = result()
    changed["packet_sources"].append("radio")
    assert canonical_packet_sha256(base) != canonical_packet_sha256(changed)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda r: r.pop("packet_sources"), "result_field_missing"),
        (lambda r: r.update(packet_sources="news"), "packet_sources_invalid"),
        (lambda r: r["material_claims"][0].pop("receipts"), "claim_receipts_required"),
        (
            lambda r: r["material_claims"][0].update(verdict="ok"),
            "claim_verdict_invalid",
        ),
        (
            lambda r: r["material_claims"][0]["receipts"].append({"passage_id": "p9"}),
            "receipt_source_family_required",
        ),
        (
            lambda r: r["material_claims"][0]["receipts"].append(receipt("radio")),
            "receipt_outside_packet",
        ),
        (
            lambda r: r["material_claims"].append(claim("c1", "supported")),
            "duplicate_claim_id",
        ),
    ],
)
def test_results_are_validated_before_ablation(mutate, code):
    broken = result()
    mutate(broken)
    with pytest.raises(ValueError, match=code):
        ablate_source_family(broken, "news")


def test_family_name_must_be_a_non_empty_string():
    with pytest.raises(ValueError, match="source_family_invalid"):
        ablate_source_family(result(), "")
    with pytest.raises(ValueError, match="source_family_invalid"):
        negative_control(result(), None)
