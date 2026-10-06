"""The dossier resolver: pointer, exact version, honest absence, and the reader projection."""

from __future__ import annotations

import json
from functools import partial

import pytest

from src.api import (
    dossier_resolver,
    dossier_store,
    intelligence_dossier,
    investigations,
    workspace_scope,
)
from tests.unit.test_dossier_store import (
    PREFIX,
    Bucket,
    _hex,
    body_of,
    frame,
    publication,
    record,
    scope_digest,
)
from tests.unit.test_dossier_store import names as names_of

RECEIPTS = (
    "gqctx_" + _hex("row_one"),
    "gqctx_" + _hex("row_two"),
    "gqctx_" + _hex("row_three"),
)


def receipt(receipt_id, label, published_at, market="za", url="https://example.test/"):
    return {
        "receipt_id": receipt_id,
        "citation_label": label,
        "kind": "content",
        "snapshot_id": "gqs_" + _hex("snapshot"),
        "market": market,
        "source_label": "news",
        "source_family": "news",
        "platform": "web",
        "author": None,
        "url": None if url is None else url + label,
        "source_row_id": label,
        "published_at": published_at,
        "collected_at": "2026-08-28T20:00:00Z",
        "excerpt": "Repair tutorials recur across two independent source families.",
        "reading_ids": [],
        "limitations": ["Contextual records do not carry promoted signal authority."],
        "content_digest": receipt_id.removeprefix("gqctx_"),
    }


def claim(claim_id, kind, receipt_ids, support_state="source_record", parents=()):
    return {
        "claim_id": claim_id,
        "text": f"Claim {claim_id}: repair tutorials recur across independent sources.",
        "kind": kind,
        "receipt_ids": list(receipt_ids),
        "reading_ids": [],
        "parent_claim_ids": list(parents),
        "support_state": support_state,
        "limitations": [],
        "falsifier": None,
    }


def dossier(**overrides):
    receipts = [
        receipt(RECEIPTS[0], "R1", "2026-08-20T00:00:00Z"),
        receipt(RECEIPTS[1], "R2", "2026-08-27T00:00:00Z"),
        receipt(RECEIPTS[2], "R3", None, market="gh", url=None),
    ]
    claims = [
        claim("clm_1", "observation", RECEIPTS[:1]),
        claim("clm_2", "interpretation", RECEIPTS[:2], "derived", parents=["clm_1"]),
        claim("clm_3", "recommendation", RECEIPTS[1:2], "derived", parents=["clm_2"]),
        claim("clm_4", "limitation", RECEIPTS[2:]),
        claim("clm_5", "abstention", RECEIPTS[:1]),
        claim("clm_6", "inference", RECEIPTS[:1], "derived"),
        claim("clm_7", "proposal", RECEIPTS[:1], "proposed"),
        claim("clm_8", "model_proposal", RECEIPTS[:1]),
    ]
    value = record()
    value["evidence_snapshot"] = {
        "contract_version": "general_question_snapshot_v1",
        "snapshot_id": "gqs_" + _hex("snapshot"),
        "receipts": receipts,
        "provenance": {
            "profile": "protected_context_v1",
            "source_binding": {"source_binding_digest": _hex("source_binding")},
        },
    }
    value["admitted_answer"] = {
        "contract_version": "general_question_result_record_v1",
        "state": "complete",
        "response": {
            "answer": "Repair is becoming a weekend social activity.\n\nLimitation: internal.",
            "intelligence": {
                "sections": [
                    {"kind": "answer", "claim_ids": ["clm_1"]},
                    {"kind": "interpretation", "claim_ids": ["clm_2", "clm_3"]},
                ],
                "claims": claims,
                "receipts": receipts,
            },
        },
    }
    value["review_state"] = {
        "selected_claim_ids": ["clm_1", "clm_2"],
        "claim_decision_refs": [],
        "relationship_decision_refs": [],
        "unresolved_questions": [
            {
                "question_id": "q_1",
                "text": "Does it hold outside Gauteng?",
                "blocking": True,
            },
            {
                "question_id": "q_2",
                "text": "Is the sample seasonal?",
                "blocking": False,
            },
        ],
    }
    value.update(overrides)
    return value


def claim_record(value, claim_id):
    claims = value["admitted_answer"]["response"]["intelligence"]["claims"]
    return next(item for item in claims if item["claim_id"] == claim_id)


def decision(
    value, resource, resource_id, state="approved", verdict="supported", **overrides
):
    if resource == "claim":
        resource_version = dossier_store.canonical_digest(
            claim_record(value, resource_id)
        )
    elif resource == "relationship":
        parent, child = resource_id.split(">")
        resource_version = dossier_store.canonical_digest(
            {"claim_id": child, "parent_claim_id": parent}
        )
        resource_id = dossier_resolver.relationship_id(parent, child)
    else:
        resource_version = _hex("artifact")
    event = {
        "contract_version": "dossier_review_v1",
        "decision_id": "dec_" + _hex(resource + resource_id + state)[:16],
        "investigation_id": value["investigation_id"],
        "dossier_version": dossier_store.dossier_version_for_record(value),
        "resource": resource,
        "resource_id": resource_id,
        "resource_version": resource_version,
        "state": state,
        "principal_ref": "human:0a1b2c3d",
        "support_review": (
            {"verdict": verdict, "receipt_ids": [], "note": None}
            if resource == "claim"
            else None
        ),
    }
    event.update(overrides)
    return event


def resolved_scope(value=None):
    value = value or record()
    return workspace_scope.ResolvedWorkspaceScope(
        investigation_id=value["investigation_id"],
        frame=investigations.investigation_frame_from_payload(value["frame"]),
        response={},
        scope_digest=scope_digest(),
    )


def published(bucket, value=None):
    value = value or dossier()
    return dossier_store.create_dossier(
        bucket=bucket,
        prefix=PREFIX,
        body=body_of(value),
        publication=publication(),
        pointer_writer=partial(
            dossier_store.publish_pointer, bucket=bucket, prefix=PREFIX
        ),
    )


# resolve_dossier


def test_resolve_reads_the_pointer_then_the_exact_version_through_the_store():
    bucket = Bucket()
    value = dossier()
    created = published(bucket, value)

    resolution = dossier_resolver.resolve_dossier(resolved_scope(), bucket=bucket)

    assert resolution == {
        "contract_version": "dossier_resolution_v1",
        "state": "ready",
        "investigation_id": value["investigation_id"],
        "dossier_version": created["publication"]["dossier_version"],
        "generation": created["publication"]["generation"],
        "dossier": value,
        "publication": created["publication"],
        "reason": None,
    }
    assert resolution["dossier"] is not value


def test_an_exact_version_is_read_regardless_of_where_the_pointer_points():
    bucket = Bucket()
    first = published(bucket, dossier())
    version = first["publication"]["dossier_version"]
    edited = dossier(predecessor_version=version, created_at="2026-09-13T00:00:00Z")
    edited["review_state"]["selected_claim_ids"] = ["clm_1"]
    second = published(bucket, edited)

    latest = dossier_resolver.resolve_dossier(resolved_scope(), bucket=bucket)
    old = dossier_resolver.resolve_dossier(resolved_scope(), version, bucket=bucket)

    assert latest["dossier_version"] == second["publication"]["dossier_version"]
    assert old["dossier_version"] == version
    assert old["dossier"]["review_state"]["selected_claim_ids"] == ["clm_1", "clm_2"]
    assert old["dossier"]["predecessor_version"] is None


def test_a_retained_version_keeps_its_evidence_when_capture_access_has_lapsed():
    """A retained capture is valid by its original authority.

    The reader never returns to the protected capture, so a capture whose
    access has since expired changes nothing, and a newer pointer rewrites
    nothing. The bucket holds no capture objects at all and refuses any read
    outside the dossier namespace to prove it.
    """
    bucket = Bucket()
    first = published(bucket, dossier())
    version = first["publication"]["dossier_version"]
    published(
        bucket, dossier(predecessor_version=version, created_at="2026-09-13T00:00:00Z")
    )
    original_get = bucket.get_blob

    def guarded(name):
        assert f"{PREFIX}dossiers/" in name, name
        return original_get(name)

    bucket.get_blob = guarded
    old = dossier_resolver.resolve_dossier(resolved_scope(), version, bucket=bucket)
    assert old["state"] == "ready"
    assert [
        item["receipt_id"] for item in old["dossier"]["evidence_snapshot"]["receipts"]
    ] == list(RECEIPTS)
    assert old["dossier"]["bindings"]["source_binding_digest"] == _hex("source_binding")


@pytest.mark.parametrize(
    ("arrange", "version", "reason"),
    [
        (lambda bucket: None, None, "dossier_unpublished"),
        (lambda bucket: None, "0" * 64, "dossier_version_absent"),
        (
            lambda bucket: bucket.drop(published(bucket)["publication"]["object_name"]),
            None,
            "dossier_version_absent",
        ),
        (
            lambda bucket: bucket.drop(
                published(bucket)["publication"]["object_name"].replace(
                    ".json", ".publication.json"
                )
            ),
            None,
            "dossier_publication_missing",
        ),
        (
            lambda bucket: bucket.move_generation(
                published(bucket)["publication"]["object_name"]
            ),
            None,
            "dossier_generation_mismatch",
        ),
    ],
    ids=[
        "no_pointer",
        "explicit_absent",
        "body_gone",
        "publication_gone",
        "generation_moved",
    ],
)
def test_absence_and_broken_authority_are_named_never_an_empty_ready_dossier(
    arrange, version, reason
):
    bucket = Bucket()
    arrange(bucket)
    resolution = dossier_resolver.resolve_dossier(
        resolved_scope(), version, bucket=bucket
    )
    assert resolution["state"] == "unavailable"
    assert resolution["reason"] == reason
    assert resolution["dossier"] is None
    assert resolution["publication"] is None
    assert resolution["generation"] is None
    assert resolution["investigation_id"] == record()["investigation_id"]


def test_a_pointer_or_version_from_another_scope_reveals_nothing():
    bucket = Bucket()
    created = published(bucket)
    other = workspace_scope.ResolvedWorkspaceScope(
        investigation_id=record()["investigation_id"],
        frame=investigations.investigation_frame_from_payload(record()["frame"]),
        response={},
        scope_digest="1" * 64,
    )
    for version in (None, created["publication"]["dossier_version"]):
        resolution = dossier_resolver.resolve_dossier(other, version, bucket=bucket)
        assert resolution["state"] == "unavailable"
        assert resolution["reason"] == "dossier_scope_mismatch"
        assert resolution["dossier"] is None
        assert created["publication"]["generation"] not in json.dumps(resolution)


def test_a_body_belonging_to_another_investigation_is_refused_and_not_named():
    bucket = Bucket()
    foreign = frame()
    from dataclasses import replace

    foreign = replace(foreign, decision_question="A different question entirely.")
    foreign_record = dossier(
        investigation_id=investigations.investigation_id_for_frame(foreign),
        frame=investigations.investigation_frame_payload(foreign),
    )
    created = published(bucket, foreign_record)
    version = created["publication"]["dossier_version"]
    identity = record()["investigation_id"]
    for suffix in (".json", ".publication.json"):
        source = (
            f"{PREFIX}dossiers/{foreign_record['investigation_id']}/{version}{suffix}"
        )
        bucket.objects[f"{PREFIX}dossiers/{identity}/{version}{suffix}"] = (
            bucket.objects[source]
        )

    resolution = dossier_resolver.resolve_dossier(
        resolved_scope(), version, bucket=bucket
    )

    assert resolution["state"] == "unavailable"
    assert resolution["reason"] == "dossier_investigation_mismatch"
    assert foreign_record["investigation_id"] not in json.dumps(resolution)


def test_storage_failure_is_unavailable_not_empty():
    bucket = Bucket()
    published(bucket)

    def broken(name):
        raise RuntimeError("storage down")

    bucket.get_blob = broken
    resolution = dossier_resolver.resolve_dossier(resolved_scope(), bucket=bucket)
    assert resolution["state"] == "unavailable"
    assert resolution["reason"] == "dossier_storage_unavailable"


def test_storage_failure_and_refusals_keep_distinct_reasons(monkeypatch):
    """A refused request or authority is never relabelled as an outage, and back."""
    bucket = Bucket()
    created = published(bucket)
    version = created["publication"]["dossier_version"]

    malformed = dossier_resolver.resolve_dossier(
        resolved_scope(), "not-a-digest", bucket=bucket
    )
    assert (malformed["state"], malformed["reason"]) == (
        "unavailable",
        "dossier_request_invalid",
    )

    bucket.drop(names_of(version)[1])
    refused = dossier_resolver.resolve_dossier(resolved_scope(), version, bucket=bucket)
    assert (refused["state"], refused["reason"]) == (
        "unavailable",
        "dossier_publication_missing",
    )

    def factory():
        raise RuntimeError("no client")

    monkeypatch.setattr(workspace_scope, "_workspace_bucket", factory)
    outage = dossier_resolver.resolve_dossier(resolved_scope(), version)
    assert (outage["state"], outage["reason"]) == (
        "unavailable",
        "dossier_storage_unavailable",
    )

    class Defective:
        name = "listening-post-staging-cache"

        def get_blob(self, name):
            return object()

    with pytest.raises(dossier_store.DossierStoreError):
        dossier_store.read_dossier_version(
            bucket=Defective(),
            prefix=PREFIX,
            investigation_id=record()["investigation_id"],
            dossier_version=version,
            scope_digest=scope_digest(),
        )
    defective = dossier_resolver.resolve_dossier(
        resolved_scope(), version, bucket=Defective()
    )
    assert (defective["state"], defective["reason"]) == (
        "unavailable",
        "dossier_storage_unavailable",
    )


def test_the_bucket_defaults_to_the_workspace_bucket_and_scope_must_be_resolved(
    monkeypatch,
):
    bucket = Bucket()
    published(bucket)
    monkeypatch.setattr(workspace_scope, "_workspace_bucket", lambda: bucket)
    assert dossier_resolver.resolve_dossier(resolved_scope())["state"] == "ready"
    with pytest.raises(TypeError):
        dossier_resolver.resolve_dossier(
            {"investigation_id": record()["investigation_id"]}
        )
    with pytest.raises(TypeError):
        dossier_resolver.resolve_dossier(resolved_scope(), 7, bucket=bucket)


# projection_record


def test_projection_has_exactly_the_reader_fields_with_cutoff_from_the_temporal_binding():
    value = dossier()
    projection = dossier_resolver.projection_record(value, [])
    assert tuple(projection) == (
        "investigation_id",
        "cutoff",
        "concise_answer",
        "decision",
        "claims",
        "evidence",
        "relationships",
        "unanswered_questions",
    )
    assert projection["investigation_id"] == value["investigation_id"]
    assert projection["cutoff"] == "2026-08-28"
    assert projection["concise_answer"] == claim_record(value, "clm_1")["text"]
    assert projection["decision"] == "pending"
    assert (
        projection["unanswered_questions"]
        == value["review_state"]["unresolved_questions"]
    )


def test_evidence_keeps_receipt_ids_and_citations_resolve_to_them():
    projection = dossier_resolver.projection_record(dossier(), [])
    evidence_ids = [item["evidence_id"] for item in projection["evidence"]]
    assert evidence_ids == list(RECEIPTS)
    for item in projection["claims"]:
        assert item["citations"]
        assert set(item["citations"]) <= set(evidence_ids)
    assert [
        item["citations"]
        for item in projection["claims"]
        if item["claim_id"] == "clm_2"
    ] == [list(RECEIPTS[:2])]


def test_finding_kinds_are_kept_and_unknown_or_internal_kinds_are_excluded():
    projection = dossier_resolver.projection_record(dossier(), [])
    assert [(item["claim_id"], item["kind"]) for item in projection["claims"]] == [
        ("clm_1", "observation"),
        ("clm_2", "interpretation"),
        ("clm_3", "recommendation"),
        ("clm_4", "limitation"),
        ("clm_5", "abstention"),
    ]
    assert all(item["text"] for item in projection["claims"])


def test_selected_and_status_come_from_exact_selections_and_authenticated_decisions():
    value = dossier()
    decisions = [
        decision(value, "claim", "clm_1"),
        decision(value, "claim", "clm_2", state="rejected", verdict="unsupported"),
        decision(value, "claim", "clm_3", dossier_version="9" * 64),
        decision(value, "claim", "clm_4", principal_ref="service:worker"),
        decision(value, "claim", "clm_5", resource_version="8" * 64),
    ]
    projection = dossier_resolver.projection_record(value, decisions)
    by_id = {item["claim_id"]: item for item in projection["claims"]}
    assert [item["selected"] for item in projection["claims"]] == [
        True,
        True,
        False,
        False,
        False,
    ]
    assert by_id["clm_1"]["status"] == "approved"
    assert by_id["clm_2"]["status"] == "rejected"
    assert by_id["clm_3"]["status"] == "pending"
    assert by_id["clm_4"]["status"] == "pending"
    assert by_id["clm_5"]["status"] == "pending"


def test_a_claim_with_conflicting_decisions_is_not_approved():
    value = dossier()
    decisions = [
        decision(value, "claim", "clm_1"),
        decision(value, "claim", "clm_1", state="rejected", verdict="unsupported"),
    ]
    projection = dossier_resolver.projection_record(value, decisions)
    assert projection["claims"][0]["status"] == "rejected"


def test_evidence_state_requires_source_gates_and_a_supported_exact_version_verdict():
    value = dossier()
    unchecked = dossier_resolver.projection_record(value, [])
    assert [item["evidence_state"] for item in unchecked["claims"]] == ["unchecked"] * 5
    decisions = [
        decision(value, "claim", "clm_1", verdict="supported"),
        decision(value, "claim", "clm_2", verdict="partial"),
        decision(value, "claim", "clm_3", verdict="contradictory"),
        decision(value, "claim", "clm_4", verdict="supported"),
        decision(value, "claim", "clm_5", verdict="unsupported"),
    ]
    reviewed = dossier_resolver.projection_record(value, decisions)
    states = {item["claim_id"]: item["evidence_state"] for item in reviewed["claims"]}
    assert states == {
        "clm_1": "ready",
        "clm_2": "thin",
        "clm_3": "contradictory",
        "clm_4": "withheld",
        "clm_5": "withheld",
    }


def test_a_model_source_record_label_alone_never_confers_readiness():
    value = dossier()
    for item in value["admitted_answer"]["response"]["intelligence"]["claims"]:
        item["support_state"] = "source_record"
    projection = dossier_resolver.projection_record(value, [])
    assert all(item["evidence_state"] == "unchecked" for item in projection["claims"])
    stale = decision(
        value, "claim", "clm_1", verdict="supported", dossier_version="7" * 64
    )
    projection = dossier_resolver.projection_record(value, [stale])
    assert projection["claims"][0]["evidence_state"] == "unchecked"


def test_client_citable_in_scope_and_dating_come_from_admitted_sources():
    projection = dossier_resolver.projection_record(dossier(), [])
    by_id = {item["evidence_id"]: item for item in projection["evidence"]}
    assert by_id[RECEIPTS[0]] == {
        "evidence_id": RECEIPTS[0],
        "client_citable": True,
        "in_scope": True,
        "published_at": "2026-08-20T00:00:00Z",
        "authority": "contextual",
    }
    assert by_id[RECEIPTS[2]]["client_citable"] is False
    assert by_id[RECEIPTS[2]]["in_scope"] is False
    assert by_id[RECEIPTS[2]]["published_at"] is None
    assert {item["authority"] for item in projection["evidence"]} == {"contextual"}
    assert "promoted" not in json.dumps(projection)


def test_a_supported_verdict_over_ungated_sources_is_withheld_not_ready():
    value = dossier()
    claim_record(value, "clm_1")["receipt_ids"] = [RECEIPTS[0], RECEIPTS[2]]
    projection = dossier_resolver.projection_record(
        value, [decision(value, "claim", "clm_1")]
    )
    assert projection["claims"][0]["evidence_state"] == "withheld"
    value = dossier()
    value["temporal_binding"]["source_cutoff"] = "2026-08-19"
    projection = dossier_resolver.projection_record(
        value, [decision(value, "claim", "clm_1")]
    )
    assert projection["claims"][0]["evidence_state"] == "withheld"


def test_relationships_derive_from_parent_links_with_exact_version_decisions():
    value = dossier()
    decisions = [
        decision(value, "relationship", "clm_1>clm_2"),
        decision(value, "relationship", "clm_2>clm_3", state="rejected"),
    ]
    projection = dossier_resolver.projection_record(value, decisions)
    assert projection["relationships"] == [
        {
            "relationship_id": dossier_resolver.relationship_id("clm_1", "clm_2"),
            "parent_claim_id": "clm_1",
            "claim_id": "clm_2",
            "status": "approved",
        },
        {
            "relationship_id": dossier_resolver.relationship_id("clm_2", "clm_3"),
            "parent_claim_id": "clm_2",
            "claim_id": "clm_3",
            "status": "rejected",
        },
    ]
    stale = [decision(value, "relationship", "clm_1>clm_2", dossier_version="6" * 64)]
    projection = dossier_resolver.projection_record(value, stale)
    assert projection["relationships"][0]["status"] == "pending"


def test_decision_comes_only_from_an_exact_version_artifact_decision():
    value = dossier()
    assert dossier_resolver.projection_record(value, [])["decision"] == "pending"
    approved = decision(value, "artifact", "art_1")
    assert (
        dossier_resolver.projection_record(value, [approved])["decision"] == "approved"
    )
    rejected = decision(value, "artifact", "art_1", state="rejected")
    assert (
        dossier_resolver.projection_record(value, [approved, rejected])["decision"]
        == "rejected"
    )
    stale = decision(value, "artifact", "art_1", dossier_version="5" * 64)
    assert dossier_resolver.projection_record(value, [stale])["decision"] == "pending"
    forged = decision(value, "artifact", "art_1", principal_ref="human:notahex")
    assert dossier_resolver.projection_record(value, [forged])["decision"] == "pending"


def test_altered_selected_claims_make_a_new_version_that_inherits_no_decision():
    value = dossier()
    decisions = [
        decision(value, "claim", "clm_1"),
        decision(value, "artifact", "art_1"),
    ]
    before = dossier_resolver.projection_record(value, decisions)
    assert before["claims"][0]["status"] == "approved"
    assert before["decision"] == "approved"
    altered = dossier()
    altered["review_state"]["selected_claim_ids"] = ["clm_1"]
    assert dossier_store.dossier_version_for_record(altered) != (
        dossier_store.dossier_version_for_record(value)
    )
    after = dossier_resolver.projection_record(altered, decisions)
    assert after["claims"][0]["status"] == "pending"
    assert after["claims"][0]["evidence_state"] == "unchecked"
    assert after["decision"] == "pending"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.update(contract_version="dossier_review_v2"),
        lambda event: event.update(resource="dossier"),
        lambda event: event.update(state="approved_later"),
        lambda event: event.update(support_review=None),
        lambda event: event.update(
            support_review={"verdict": "strong", "receipt_ids": [], "note": None}
        ),
        lambda event: event.update(investigation_id="inv_other"),
        lambda event: event.pop("principal_ref"),
    ],
    ids=[
        "contract",
        "resource",
        "state",
        "no_review",
        "verdict",
        "investigation",
        "principal",
    ],
)
def test_decisions_outside_the_grammar_are_not_authenticated(mutate):
    value = dossier()
    event = decision(value, "claim", "clm_1")
    mutate(event)
    projection = dossier_resolver.projection_record(value, [event])
    assert projection["claims"][0]["status"] == "pending"
    assert projection["claims"][0]["evidence_state"] == "unchecked"


def test_client_read_keeps_the_eight_field_allowlist_and_the_working_response_carries_everything():
    bucket = Bucket()
    value = dossier()
    created = published(bucket, value)
    decisions = [
        decision(value, "claim", "clm_1"),
        decision(value, "claim", "clm_2"),
        decision(value, "relationship", "clm_1>clm_2"),
        decision(value, "artifact", "art_1"),
    ]
    resolution = dossier_resolver.resolve_dossier(resolved_scope(), bucket=bucket)

    working = dossier_resolver.working_response(resolution, decisions)
    assert tuple(working) == (
        "contract_version",
        "investigation_id",
        "dossier_version",
        "generation",
        "state",
        "dossier",
        "projection",
        "publication",
    )
    assert working["contract_version"] == "dossier_working_v1"
    assert working["dossier"] == value
    assert working["dossier_version"] == created["publication"]["dossier_version"]
    assert working["projection"] == dossier_resolver.projection_record(value, decisions)
    assert {item["claim_id"] for item in working["projection"]["claims"]} == {
        "clm_1",
        "clm_2",
        "clm_3",
        "clm_4",
        "clm_5",
    }

    client_read = intelligence_dossier.read_dossier(
        value["investigation_id"], working["projection"]
    )
    intelligence_dossier.validate_dossier_payload(client_read)
    assert tuple(client_read) == (
        "contract_version",
        "investigation_id",
        "concise_answer",
        "claims",
        "evidence",
        "excluded_count",
        "excluded_reasons",
        "artifact_readiness",
    )
    assert [item["claim_id"] for item in client_read["claims"]] == ["clm_1", "clm_2"]
    assert all(
        set(item) == {"claim_id", "kind", "text", "citations"}
        for item in client_read["claims"]
    )
    assert all(
        set(item) == {"evidence_id", "published_at"} for item in client_read["evidence"]
    )
    assert client_read["artifact_readiness"]["state"] == "blocked"
    assert "blocking_question" in client_read["artifact_readiness"]["failed"]
    assert "clm_8" not in json.dumps(client_read)
    assert "Limitation: internal" not in json.dumps(client_read)


def test_working_response_carries_an_unavailable_resolution_without_a_projection():
    resolution = dossier_resolver.resolve_dossier(resolved_scope(), bucket=Bucket())
    working = dossier_resolver.working_response(resolution, [])
    assert working["state"] == "unavailable"
    assert working["dossier"] is None
    assert working["projection"] is None
    assert working["dossier_version"] is None
