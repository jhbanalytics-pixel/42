"""The consolidated dossier read.

One request, one server-resolved scope, one honest answer. The tests here guard
the same boundary the frontend projection guards, on the side where it actually
matters: material that never leaves the server cannot be leaked by a client
mistake later.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import main

client = TestClient(main.app)

ROUTE = "/api/internal/v2/investigations/inv_case/dossier/read"
VERSION = "intelligence_dossier_v1"


@pytest.fixture(autouse=True)
def open_gate(monkeypatch):
    monkeypatch.setenv("LP_ALLOW_OPEN_GATE", "true")
    monkeypatch.delenv("UI_PASSCODE", raising=False)


@pytest.mark.parametrize(
    "query",
    [
        "",
        "?contract_version=",
        f"?contract_version={VERSION}&contract_version={VERSION}",
        f"?contract_version={VERSION}&actor=someone",
        f"?contract_version={VERSION}&client_scope_id=other",
        f"?contract_version={VERSION}&include_rejected=true",
    ],
    ids=[
        "missing",
        "blank",
        "duplicated",
        "caller_actor",
        "caller_scope",
        "caller_asks_for_rejected",
    ],
)
def test_only_the_exact_request_grammar_is_accepted(query: str):
    """Break caught: a caller widening its own scope through the query.

    include_rejected is the sharp one. A caller must not be able to ask for
    material the projection exists to withhold.
    """
    response = client.get(f"{ROUTE}{query}")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "workspace_request_invalid"


def test_an_unsupported_version_is_named_as_such():
    response = client.get(f"{ROUTE}?contract_version=intelligence_dossier_v2")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "contract_version_unsupported"


def test_authentication_fails_before_any_read(monkeypatch):
    monkeypatch.delenv("LP_ALLOW_OPEN_GATE", raising=False)
    monkeypatch.setenv("UI_PASSCODE", "fixture-passcode")

    reads: list[str] = []
    from src.api import intelligence_dossier

    def forbidden(*args: object, **kwargs: object) -> object:
        reads.append("read")
        raise AssertionError("a read ran before authentication")

    monkeypatch.setattr(intelligence_dossier, "read_dossier", forbidden)
    response = client.get(f"{ROUTE}?contract_version={VERSION}")

    assert response.status_code == 401
    assert reads == []


def test_the_private_route_never_enters_the_public_api_surface():
    generated = main.app.openapi()
    assert not any("dossier" in path for path in generated.get("paths", {}))


def test_the_read_never_writes():
    """Break caught: a read that starts work.

    Opening a dossier must not create, approve, generate or spend anything.
    """
    for method in (client.post, client.put, client.patch, client.delete):
        response = method(f"{ROUTE}?contract_version={VERSION}")
        assert response.status_code in (404, 405)


# --- The projection itself ----------------------------------------------------
#
# The request grammar above stops a caller widening its scope. These exercise
# what the server actually hands back, which is where material either leaks or
# is withheld.

from src.api import intelligence_dossier  # noqa: E402


def _claim(**overrides):
    claim = {
        "claim_id": "clm_1",
        "kind": "observation",
        "text": "Repair tutorials recur across two independent source families.",
        "status": "approved",
        "evidence_state": "ready",
        "selected": True,
        "citations": ["ev_1"],
    }
    claim.update(overrides)
    return claim


def _record(**overrides):
    record = {
        "investigation_id": "inv_case",
        "cutoff": "2026-08-28",
        "concise_answer": "Repair is becoming a weekend social activity.",
        "decision": "approved",
        "claims": [_claim()],
        "evidence": [
            {
                "evidence_id": "ev_1",
                "client_citable": True,
                "in_scope": True,
                "published_at": "2026-08-20T00:00:00Z",
            }
        ],
        "relationships": [{"relationship_id": "rel_1", "status": "approved"}],
        "unanswered_questions": [],
    }
    record.update(overrides)
    return record


def _read(**overrides):
    return intelligence_dossier.read_dossier("inv_case", _record(**overrides))


def test_an_approved_ready_selected_cited_claim_is_projected():
    payload = _read()
    assert [item["claim_id"] for item in payload["claims"]] == ["clm_1"]
    assert payload["artifact_readiness"]["state"] == "client_ready"


@pytest.mark.parametrize(
    "change",
    ["missing", "foreign", "noncitable", "late", "undated", "duplicate", "truthy"],
)
def test_unusable_citation_blocks_and_withholds_its_claim(change):
    record = _record()
    item = record["evidence"][0]
    if change == "missing":
        record["evidence"] = []
    elif change == "foreign":
        item["in_scope"] = False
    elif change == "noncitable":
        item["client_citable"] = False
    elif change == "late":
        item["published_at"] = "2026-09-01T00:00:00Z"
    elif change == "undated":
        item["published_at"] = None
    elif change == "duplicate":
        record["evidence"].append(dict(item))
    else:
        item["client_citable"] = "true"
    result = intelligence_dossier.read_dossier("inv_case", record)
    assert result["artifact_readiness"]["state"] == "blocked"
    assert result["claims"] == []
    assert result["evidence"] == []
    assert result["excluded_count"] > 0


def test_one_missing_citation_withholds_the_whole_claim():
    result = _read(claims=[_claim(citations=["ev_1", "ev_missing"])])
    assert result["claims"] == []
    assert result["artifact_readiness"]["state"] == "blocked"


def test_pending_selection_requires_approval_and_empty_selection_is_blocked():
    assert (
        _read(claims=[_claim(status="pending")])["artifact_readiness"]["state"]
        == "approval_required"
    )
    assert _read(claims=[])["artifact_readiness"]["state"] == "blocked"
    assert (
        _read(claims=[_claim(selected=False)])["artifact_readiness"]["state"]
        == "blocked"
    )


def test_duplicate_claim_identity_is_withheld():
    result = _read(claims=[_claim(), _claim(text="A different assertion.")])
    assert result["claims"] == []
    assert result["artifact_readiness"]["state"] == "blocked"


def test_public_validator_independently_rejects_dangling_or_empty_ready_payload():
    result = _read()
    result["evidence"] = []
    with pytest.raises(intelligence_dossier.DossierContractError):
        intelligence_dossier.validate_dossier_payload(result)
    result["claims"] = []
    with pytest.raises(intelligence_dossier.DossierContractError):
        intelligence_dossier.validate_dossier_payload(result)


@pytest.mark.parametrize(
    "mutation", ["internal_field", "internal_kind", "bad_date", "duplicate_id"]
)
def test_public_validator_rejects_projection_bypasses(mutation):
    result = _read()
    if mutation == "internal_field":
        result["claims"][0]["model_memory"] = "withheld"
    elif mutation == "internal_kind":
        result["claims"][0]["kind"] = "model_memory"
    elif mutation == "bad_date":
        result["evidence"][0]["published_at"] = "not a timestamp"
    else:
        result["evidence"].append(dict(result["evidence"][0]))
    with pytest.raises(intelligence_dossier.DossierContractError):
        intelligence_dossier.validate_dossier_payload(result)


def test_the_final_fractional_second_of_the_cutoff_day_is_eligible():
    record = _record()
    record["evidence"][0]["published_at"] = "2026-08-28T23:59:59.999999Z"
    assert (
        intelligence_dossier.read_dossier("inv_case", record)["artifact_readiness"][
            "state"
        ]
        == "client_ready"
    )


def test_server_honors_the_existing_precedent_gates():
    result = _read(
        precedents=[
            {
                "occurred_on": "2026-08-29",
                "material_differences": [],
                "nontransferable": [],
            }
        ]
    )
    assert result["artifact_readiness"]["state"] == "blocked"
    assert set(result["artifact_readiness"]["failed"]) == {
        "precedent_not_earlier_than_cutoff",
        "precedent_missing_transfer_limits",
    }


def test_whitespace_answer_or_claim_is_not_ready():
    assert _read(concise_answer=" \n ")["artifact_readiness"]["state"] == "blocked"
    assert (
        _read(claims=[_claim(text=" \n ")])["artifact_readiness"]["state"] == "blocked"
    )


def test_malformed_summary_or_status_cannot_leak_internal_objects():
    result = _read(concise_answer={"model_memory": "INTERNAL_MARKER"})
    assert result["concise_answer"] is None
    assert "INTERNAL_MARKER" not in repr(result)
    result = _read(claims=[_claim(status={"model_memory": "INTERNAL_MARKER"})])
    assert "INTERNAL_MARKER" not in repr(result)
    for field in ("evidence_state", "kind", "claim_id"):
        result = _read(claims=[_claim(**{field: {"model_memory": "INTERNAL_MARKER"}})])
        assert "INTERNAL_MARKER" not in repr(result)
    result = _read()
    result["concise_answer"] = {"model_memory": "INTERNAL_MARKER"}
    with pytest.raises(intelligence_dossier.DossierContractError):
        intelligence_dossier.validate_dossier_payload(result)


@pytest.mark.parametrize(
    "precedent",
    [
        {
            "occurred_on": None,
            "material_differences": ["different"],
            "nontransferable": ["limited"],
        },
        {"occurred_on": "0", "material_differences": [""], "nontransferable": [""]},
        {
            "occurred_on": "2026-08-20",
            "material_differences": {"text": "different"},
            "nontransferable": ["limited"],
        },
    ],
)
def test_unproven_precedent_is_blocked(precedent):
    assert _read(precedents=[precedent])["artifact_readiness"]["state"] == "blocked"


def test_invalid_timezone_offset_is_not_normalized_into_valid_evidence():
    record = _record()
    record["evidence"][0]["published_at"] = "2026-08-20T10:00:00+00:99"
    assert (
        intelligence_dossier.read_dossier("inv_case", record)["artifact_readiness"][
            "state"
        ]
        == "blocked"
    )


@pytest.mark.parametrize("status", ["pending", "rejected", "superseded", "disputed"])
def test_an_unapproved_claim_never_leaves_the_server(status: str):
    """Break caught: withheld material reaching the wire at all."""
    payload = _read(claims=[_claim(status=status)])
    assert payload["claims"] == []
    # The text itself is nowhere in the response, not merely flagged.
    assert "Repair tutorials recur" not in repr(payload)


def test_thin_evidence_is_admissible_only_as_a_limitation():
    assert _read(claims=[_claim(evidence_state="thin")])["claims"] == []
    limitation = _read(claims=[_claim(kind="limitation", evidence_state="thin")])
    assert [item["kind"] for item in limitation["claims"]] == ["limitation"]


def test_a_claim_without_a_citation_is_never_projected():
    assert _read(claims=[_claim(citations=[])])["claims"] == []


def test_exclusion_is_counted_and_explained_without_carrying_the_text():
    """A reader is entitled to know something was withheld, not to read it."""
    payload = _read(claims=[_claim(status="rejected")])
    assert payload["excluded_count"] == 1
    assert payload["excluded_reasons"]
    assert "Repair tutorials recur" not in repr(payload)


@pytest.mark.parametrize(
    "kind",
    ["model_proposal", "model_memory", "internal_assumption", "diffusion_shadow"],
)
def test_model_proposals_and_internal_material_are_never_projected(kind: str):
    payload = _read(
        claims=[_claim(), _claim(claim_id="clm_2", kind=kind, text="an internal idea")]
    )
    assert [item["claim_id"] for item in payload["claims"]] == ["clm_1"]
    assert "an internal idea" not in repr(payload)
    # The reason matters as much as the exclusion. "never projected to a
    # client" is a policy; "not a client claim kind" reads like an oversight,
    # and an operator reviewing exclusions needs to tell those apart.
    assert "never projected" in " ".join(payload["excluded_reasons"])


def test_evidence_that_is_not_client_citable_is_withheld():
    payload = _read(
        evidence=[
            {"evidence_id": "ev_1", "client_citable": False, "published_at": None}
        ]
    )
    assert payload["evidence"] == []
    assert payload["excluded_count"] >= 1


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"decision": "pending"}, "approval_required"),
        ({"concise_answer": None}, "blocked"),
        (
            {"unanswered_questions": [{"question_id": "q1", "blocking": True}]},
            "blocked",
        ),
    ],
)
def test_a_failed_gate_is_named_and_never_softened(overrides, expected):
    readiness = _read(**overrides)["artifact_readiness"]
    assert readiness["state"] == expected
    assert readiness["failed"]


# --- The server must not be the weaker gate -----------------------------------
#
# Found by reviewing this projection against the client one. The client refused
# evidence on three grounds and the server refused it on one, so out-of-scope
# and future-dated evidence reached the wire and only a client filter kept it
# from a reader. That is backwards: the whole point of projecting here is that
# no later client mistake can leak what a gate withheld.


def test_evidence_outside_the_investigation_scope_is_withheld():
    payload = _read(
        evidence=[
            {
                "evidence_id": "ev_1",
                "client_citable": True,
                "in_scope": False,
                "published_at": "2026-08-20T00:00:00Z",
            }
        ]
    )
    assert payload["evidence"] == []
    assert any("scope" in reason for reason in payload["excluded_reasons"])


def test_evidence_dated_after_the_cutoff_is_withheld():
    payload = _read(
        cutoff="2026-08-28",
        evidence=[
            {
                "evidence_id": "ev_1",
                "client_citable": True,
                "in_scope": True,
                "published_at": "2026-09-30T00:00:00Z",
            }
        ],
    )
    assert payload["evidence"] == []
    assert any("cutoff" in reason for reason in payload["excluded_reasons"])


def test_evidence_on_the_cutoff_day_is_still_carried():
    """The boundary is the end of the cutoff day, not its start. Dropping same
    day evidence would withhold exactly the most recent proof a claim rests on."""
    payload = _read(
        cutoff="2026-08-28",
        evidence=[
            {
                "evidence_id": "ev_1",
                "client_citable": True,
                "in_scope": True,
                "published_at": "2026-08-28T23:00:00Z",
            }
        ],
    )
    assert [item["evidence_id"] for item in payload["evidence"]] == ["ev_1"]


def test_evidence_with_no_published_date_is_withheld_with_a_dating_reason():
    """Undated is unknown, and unknown must refuse.

    This test once asserted the opposite, on a docstring's claim that an
    absent date was refused elsewhere. Independent review found nothing
    behind that claim, so the gate now refuses here and says why.
    """
    payload = _read(
        cutoff="2026-08-28",
        evidence=[
            {
                "evidence_id": "ev_1",
                "client_citable": True,
                "in_scope": True,
                "published_at": None,
            }
        ],
    )
    assert payload["evidence"] == []
    assert any(
        "no publication date" in reason for reason in payload["excluded_reasons"]
    )


def test_the_server_refuses_everything_the_client_refuses():
    """The two projections must not disagree about what a client may see."""
    payload = _read(
        evidence=[
            {
                "evidence_id": "ev_1",
                "client_citable": True,
                "in_scope": True,
                "published_at": "2026-08-20T00:00:00Z",
            },
            {
                "evidence_id": "ev_2",
                "client_citable": False,
                "in_scope": True,
                "published_at": "2026-08-20T00:00:00Z",
            },
            {
                "evidence_id": "ev_3",
                "client_citable": True,
                "in_scope": False,
                "published_at": "2026-08-20T00:00:00Z",
            },
            {
                "evidence_id": "ev_4",
                "client_citable": True,
                "in_scope": True,
                "published_at": "2026-09-30T00:00:00Z",
            },
        ],
        claims=[
            _claim(),
            _claim(claim_id="clm_2", citations=["ev_1", "ev_2", "ev_3", "ev_4"]),
        ],
    )
    assert [item["evidence_id"] for item in payload["evidence"]] == ["ev_1"]
    assert [item["claim_id"] for item in payload["claims"]] == ["clm_1"]
    assert payload["artifact_readiness"]["state"] == "blocked"
    assert payload["excluded_count"] == 4


def test_dated_evidence_is_refused_when_the_cutoff_is_unavailable():
    """A missing boundary is not a passing one.

    Without a cutoff the dating of evidence cannot be checked at all, so it is
    withheld and said to be unchecked rather than admitted on the assumption
    that it is probably fine.
    """
    payload = _read(
        cutoff=None,
        evidence=[
            {
                "evidence_id": "ev_1",
                "client_citable": True,
                "in_scope": True,
                "published_at": "2026-08-20T00:00:00Z",
            }
        ],
    )
    assert payload["evidence"] == []
    assert any(
        "cutoff is unavailable" in reason for reason in payload["excluded_reasons"]
    )


# --- Two mutations the contract names by hand ---------------------------------
#
# Section 20 requires a named mutation for a cross-scope artifact and for an
# extra Client Read field. Neither existed. The first turned out not to be a
# missing test but a missing check: the read echoed whichever investigation id
# it was handed, without ever asking whether the record belonged to it.


def test_a_record_from_another_investigation_is_refused():
    """A dossier labelled one investigation may not carry another's content.

    The id is not a caption. It names the scope the content was approved
    within, and echoing a caller's id over a foreign record would produce a
    document that is wrong in the one way nobody would check.
    """
    record = _record()
    record["investigation_id"] = "inv_other"
    with pytest.raises(intelligence_dossier.DossierContractError):
        intelligence_dossier.read_dossier("inv_case", record)


def test_a_record_that_names_its_own_investigation_is_accepted():
    record = _record()
    record["investigation_id"] = "inv_case"
    assert (
        intelligence_dossier.read_dossier("inv_case", record)["investigation_id"]
        == "inv_case"
    )


def test_a_record_naming_no_investigation_is_refused():
    """Unnamed is not the same as matching. A record that never said which
    investigation it belongs to cannot be assumed to belong to this one."""
    record = _record()
    record.pop("investigation_id", None)
    with pytest.raises(intelligence_dossier.DossierContractError):
        intelligence_dossier.read_dossier("inv_case", record)


def test_an_extra_field_never_reaches_a_client_read():
    """The response is an exact shape, not a minimum one."""
    payload = _read()
    payload["graph_scores"] = {"clm_1": 0.91}
    with pytest.raises(intelligence_dossier.DossierContractError):
        intelligence_dossier.validate_dossier_payload(payload)


def test_a_missing_field_is_refused_the_same_way():
    payload = _read()
    del payload["excluded_reasons"]
    with pytest.raises(intelligence_dossier.DossierContractError):
        intelligence_dossier.validate_dossier_payload(payload)


def test_the_same_fields_in_a_different_order_are_refused():
    """Order is part of the contract, so a reordered payload is a different
    payload even though it carries the same keys."""
    payload = _read()
    reordered = {key: payload[key] for key in reversed(list(payload))}
    with pytest.raises(intelligence_dossier.DossierContractError):
        intelligence_dossier.validate_dossier_payload(reordered)


# --- A malformed boundary must not become a passing gate ----------------------
#
# Found by independent review. Three fail-open shapes, all the same law: a
# value the gate cannot read must refuse, not pass.


def test_a_malformed_cutoff_withholds_dated_evidence():
    payload = _read(
        cutoff="pending",
        evidence=[
            {
                "evidence_id": "ev_1",
                "client_citable": True,
                "in_scope": True,
                "published_at": "2026-09-20T00:00:00Z",
            }
        ],
    )
    assert payload["evidence"] == []
    assert payload["excluded_count"] >= 1


def test_undated_evidence_cannot_be_checked_and_is_withheld():
    payload = _read(
        evidence=[
            {
                "evidence_id": "ev_1",
                "client_citable": True,
                "in_scope": True,
                "published_at": None,
            }
        ]
    )
    assert payload["evidence"] == []


def test_unreadably_dated_evidence_is_withheld():
    payload = _read(
        evidence=[
            {
                "evidence_id": "ev_1",
                "client_citable": True,
                "in_scope": True,
                "published_at": "soonish",
            }
        ]
    )
    assert payload["evidence"] == []


def test_a_question_missing_its_blocking_flag_blocks():
    """A question that does not say whether it blocks is not known safe."""
    readiness = _read(unanswered_questions=[{"question_id": "q1"}])[
        "artifact_readiness"
    ]
    assert readiness["state"] == "blocked"


def test_a_question_explicitly_not_blocking_does_not_block():
    readiness = _read(unanswered_questions=[{"question_id": "q1", "blocking": False}])[
        "artifact_readiness"
    ]
    assert readiness["state"] == "client_ready"
