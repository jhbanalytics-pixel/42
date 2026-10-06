"""The client purpose policy at export and review: scoped to the brand, never overridable."""

from __future__ import annotations

import pytest
from src.api import (
    client_purpose,
    dossier_approval,
    dossier_artifacts,
    dossier_review_store,
)
from src.api.dossier_store import DossierRecordInvalid
from tests.unit import dossier_pdf_fakes

# The engine pins the same digest over the same block in test_client_overlay; the two
# literals move together when the policy version changes.
PURPOSE_POLICY_DIGEST = (
    "f3c523232ac172d26c842e546bc7adb68657bc58df3e06151515b12ee945bb8a"
)
PROHIBITED = {
    "political_party_purpose": "Track the ruling party campaign messaging in Gauteng",
    "electoral_purpose": "How is the election being discussed by Global South Africans?",
    "voter_targeting_purpose": "Which narratives could persuade undecided voters in Durban?",
    "agency_position_live_regulatory_matter": (
        "Should Brand South Africa oppose the draft bill under regulatory review?"
    ),
}
PERMITTED = [
    "What are Global South Africans saying about load shedding this week?",
    "What do we know about support for the regulator decision on tariffs?",
    "How did the party at the Cape Town jazz festival trend?",
]


def client_read(*texts):
    return {
        "investigation_id": "inv_fixture",
        "concise_answer": texts[0],
        "claims": [
            {"kind": "interpretation", "text": text, "citations": []}
            for text in texts[1:]
        ],
        "evidence": [],
        "artifact_readiness": {"state": "review_required"},
        "excluded_count": 0,
    }


def build(read, policy):
    return dossier_artifacts.build_artifact(
        investigation_id="inv_fixture",
        scope_digest="a" * 64,
        dossier_version="b" * 64,
        selected_claim_ids=[],
        client_read=read,
        format="html",
        purpose_policy=policy,
    )


def test_the_bsa_policy_loads_from_the_engine_overlay_and_general_42_names_none():
    policy = client_purpose.load_client_purpose_policy("bsa")
    assert policy["policy_version"] == client_purpose.POLICY_VERSION
    assert policy["policy_digest"] == PURPOSE_POLICY_DIGEST
    assert (
        tuple(p["code"] for p in policy["prohibited_purposes"])
        == client_purpose.PURPOSE_CODES
    )
    assert client_purpose.load_client_purpose_policy(None) is None
    for brand in ("unknown_brand", "../bsa", "", 7):
        with pytest.raises(client_purpose.ClientPurposePolicyUnavailable):
            client_purpose.load_client_purpose_policy(brand)


def test_the_policy_shape_is_exact():
    policy = client_purpose.load_client_purpose_policy("bsa")
    raw = {k: v for k, v in policy.items() if k != "policy_digest"}
    assert (
        client_purpose.validate_client_purpose_policy(raw)["policy_digest"]
        == PURPOSE_POLICY_DIGEST
    )
    for mutate in (
        lambda p: p.update(policy_version="client_purpose_policy_v0"),
        lambda p: p["prohibited_purposes"].pop(),
        lambda p: p.update(extra=True),
        lambda p: p["prohibited_purposes"][0].update(patterns=["("]),
    ):
        changed = {k: v for k, v in policy.items() if k != "policy_digest"}
        changed["prohibited_purposes"] = [
            dict(p) for p in changed["prohibited_purposes"]
        ]
        mutate(changed)
        with pytest.raises(client_purpose.ClientPurposePolicyUnavailable):
            client_purpose.validate_client_purpose_policy(changed)


def test_each_prohibited_purpose_is_named_and_permitted_wording_passes():
    policy = client_purpose.load_client_purpose_policy("bsa")
    for code, text in PROHIBITED.items():
        evaluation = client_purpose.evaluate_client_purpose(policy, [text])
        assert evaluation["state"] == "prohibited", text
        assert code in evaluation["purpose_codes"], text
        assert evaluation["review_override"] == "refused"
    for text in PERMITTED:
        assert (
            client_purpose.evaluate_client_purpose(policy, [text])["state"]
            == "permitted"
        ), text


def test_export_refuses_a_prohibited_purpose_under_the_brand_and_builds_it_under_general_42(monkeypatch):
    dossier_pdf_fakes.install_font(monkeypatch)
    policy = client_purpose.load_client_purpose_policy("bsa")
    prohibited = client_read(
        "Election messaging reached undecided voters in Durban.",
        "This may suggest the ruling party campaign is landing.",
    )
    with pytest.raises(client_purpose.ClientPurposeProhibited) as caught:
        build(prohibited, policy)
    assert caught.value.code == client_purpose.EXPORT_REFUSED
    assert set(caught.value.purpose_codes) >= {
        "electoral_purpose",
        "political_party_purpose",
    }
    assert caught.value.policy_version == client_purpose.POLICY_VERSION
    general = build(prohibited, None)
    assert general["contract_version"] == dossier_artifacts.ARTIFACT_CONTRACT_VERSION
    assert "undecided voters" in general["html"]
    permitted = build(
        client_read(
            "Load shedding stage six drew international pickup.",
            "This may suggest the energy narrative travelled.",
        ),
        policy,
    )
    assert permitted["artifact_id"].startswith("art_")
    assert client_purpose.client_read_texts(prohibited) == [
        "Election messaging reached undecided voters in Durban.",
        "This may suggest the ruling party campaign is landing.",
    ]


def test_no_review_role_or_action_can_override_a_prohibited_purpose():
    policy = client_purpose.load_client_purpose_policy("bsa")
    evaluation = client_purpose.evaluate_client_purpose(
        policy, ["Which narratives could persuade undecided voters in Durban?"]
    )
    for role in sorted(dossier_approval.APPROVAL_ROLES):
        for action in ("submit", "approve", "reject", "withdraw", "reopen"):
            with pytest.raises(DossierRecordInvalid) as caught:
                dossier_review_store.refuse_client_purpose_override(
                    evaluation, role=role, action=action
                )
            assert caught.value.code == client_purpose.REVIEW_OVERRIDE_REFUSED
    permitted = client_purpose.evaluate_client_purpose(
        policy, ["Load shedding stage six"]
    )
    assert (
        dossier_review_store.refuse_client_purpose_override(
            permitted, role="dossier_editor", action="submit"
        )
        is None
    )
    assert (
        dossier_review_store.refuse_client_purpose_override(
            None, role="dossier_editor", action="submit"
        )
        is None
    )
