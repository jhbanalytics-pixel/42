import copy
import importlib

import pytest


def claim(cid="observation", **overrides):
    value = {
        "claim_id": cid,
        "text": "A synthetic source describes shared transport.",
        "kind": "observation",
        "receipt_ids": ["receipt_a"],
        "reading_ids": [],
        "parent_claim_ids": [],
        "support_state": "source_record",
        "limitations": [],
        "falsifier": None,
    }
    value.update(overrides)
    return value


def draft():
    return {"claims": [claim()], "sections": [{"kind": "answer", "claim_ids": ["observation"]}]}


def validate(value, **overrides):
    module = importlib.import_module("src.analysis.open_intelligence.general_question_claims")
    arguments = {"receipt_ids": ["receipt_a"], "reading_receipts": {"reading_a": ["receipt_a"]}}
    arguments.update(overrides)
    return module.validate_question_claim_structure(value, **arguments)


def test_unseen_claim_structure_retains_order_but_never_issues_support_authority():
    value = draft()
    result = validate(value)
    assert result["claims"] == value["claims"]
    assert result["sections"] == value["sections"]
    assert result["answer_has_reference_basis"] is True
    assert result["support_validation_required"] is True
    assert result["ready_for_downstream"] is False
    value["claims"][0]["text"] = "changed later"
    assert result["claims"][0]["text"] != "changed later"


@pytest.mark.parametrize("field", ["receipt_ids", "reading_ids", "parent_claim_ids"])
def test_unknown_or_cross_snapshot_reference_refuses(field):
    value = draft()
    value["claims"][0][field] = ["foreign"]
    with pytest.raises(ValueError):
        validate(value)


@pytest.mark.parametrize("field", ["receipt_ids", "reading_ids", "parent_claim_ids"])
def test_duplicate_references_refuse(field):
    value = draft()
    identity = {
        "receipt_ids": "receipt_a",
        "reading_ids": "reading_a",
        "parent_claim_ids": "observation",
    }[field]
    value["claims"][0][field] = [identity, identity]
    with pytest.raises(ValueError):
        validate(value)


def test_reading_without_receipt_cannot_create_a_basis():
    value = draft()
    value["claims"][0].update(receipt_ids=[], reading_ids=["reading_a"])
    with pytest.raises(ValueError):
        validate(value, reading_receipts={"reading_a": []})


@pytest.mark.parametrize("cycle", ["self", "two"])
def test_cycles_refuse_even_with_direct_receipts(cycle):
    value = draft()
    value["claims"][0]["parent_claim_ids"] = ["observation" if cycle == "self" else "other"]
    if cycle == "two":
        value["claims"].append(claim("other", parent_claim_ids=["observation"]))
    with pytest.raises(ValueError):
        validate(value)


def test_deep_parent_chain_uses_iterative_validation():
    claims = [claim("c0")]
    for index in range(1, 1200):
        claims.append(
            claim(
                f"c{index}",
                receipt_ids=[],
                parent_claim_ids=[f"c{index - 1}"],
                kind="inference",
                support_state="derived",
            )
        )
    value = {
        "claims": claims,
        "sections": [
            {"kind": "answer", "claim_ids": ["c1199"]},
            {"kind": "evidence", "claim_ids": [f"c{index}" for index in range(1199)]},
        ],
    }
    assert validate(value)["answer_has_reference_basis"] is True


def test_parent_only_interpretation_has_reference_basis():
    value = draft()
    value["claims"].append(
        claim(
            "interpretation",
            kind="interpretation",
            receipt_ids=[],
            parent_claim_ids=["observation"],
            support_state="derived",
            limitations=["Synthetic interpretation needs support review."],
        )
    )
    value["sections"].append({"kind": "interpretation", "claim_ids": ["interpretation"]})
    assert validate(value)["sections"] == value["sections"]


def proposal():
    return claim(
        "action",
        kind="proposal",
        receipt_ids=[],
        parent_claim_ids=["observation"],
        support_state="proposed",
        falsifier="A subsequent source check contradicts the observation.",
    )


def test_proposal_requires_review_and_stays_in_actions():
    value = draft()
    value["claims"].append(proposal())
    value["sections"].append({"kind": "actions", "claim_ids": ["action"]})
    result = validate(value)
    assert result["review_required"] is True
    assert result["ready_for_downstream"] is False


@pytest.mark.parametrize(
    "mutation", ["no_falsifier", "answer", "nonproposal", "no_basis", "promoted_parent"]
)
def test_proposal_cannot_mint_factual_support(mutation):
    value = draft()
    value["claims"].append(proposal())
    value["sections"].append({"kind": "actions", "claim_ids": ["action"]})
    if mutation == "no_falsifier":
        value["claims"][1]["falsifier"] = None
    elif mutation == "answer":
        value["sections"] = [{"kind": "answer", "claim_ids": ["observation", "action"]}]
    elif mutation == "nonproposal":
        value["claims"][1]["kind"] = "observation"
    elif mutation == "no_basis":
        value["claims"][1]["parent_claim_ids"] = []
    else:
        value["claims"].append(claim("promoted", receipt_ids=[], parent_claim_ids=["action"]))
        value["sections"][0]["claim_ids"].append("promoted")
    with pytest.raises(ValueError):
        validate(value)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_field",
        "duplicate_id",
        "empty_text",
        "unplaced_no_basis",
        "repeated_section",
        "out_of_order",
        "repeated_placement",
        "unknown_section_claim",
        "empty_section",
    ],
)
def test_malformed_claim_or_section_refuses(mutation):
    value = draft()
    if mutation == "unknown_field":
        value["claims"][0]["approved"] = True
    elif mutation == "duplicate_id":
        value["claims"].append(copy.deepcopy(value["claims"][0]))
    elif mutation == "empty_text":
        value["claims"][0]["text"] = " "
    elif mutation == "unplaced_no_basis":
        value["claims"].append(claim("orphan", receipt_ids=[]))
    elif mutation == "repeated_section":
        value["sections"].append(copy.deepcopy(value["sections"][0]))
    elif mutation == "out_of_order":
        value["sections"].insert(0, {"kind": "actions", "claim_ids": ["observation"]})
    elif mutation == "repeated_placement":
        value["sections"].append({"kind": "evidence", "claim_ids": ["observation"]})
    elif mutation == "unknown_section_claim":
        value["sections"][0]["claim_ids"] = ["unknown"]
    else:
        value["sections"][0]["claim_ids"] = []
    with pytest.raises(ValueError):
        validate(value)


def test_matching_ids_do_not_certify_changed_prose_or_quantities():
    value = draft()
    value["claims"][0]["text"] = "The same receipt supposedly proves 999 people changed behavior."
    result = validate(value)
    assert result["support_validation_required"] is True
    assert result["ready_for_downstream"] is False


def test_empty_structure_has_no_answer_basis():
    result = validate({"claims": [], "sections": []})
    assert result["answer_has_reference_basis"] is False


def test_supported_claim_cannot_hide_outside_the_rendered_sections():
    value = draft()
    value["claims"].append(claim("hidden_but_supported"))
    with pytest.raises(ValueError):
        validate(value)
