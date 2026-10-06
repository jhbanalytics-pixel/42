"""The dossiers the browser journey reviews, published through the real dossier store.

An investigation is created through the real API first. Its dossier is then
published here the way the engine's producer publishes one: the exact record
bytes through dossier_store.create_dossier and the pointer after read back, so
every later read and write the journey makes goes through the app unchanged.

The main dossier carries what the journey needs to meet: observations and an
interpretation with citable receipts, links between claims, a challenge finding
that contradicts the interpretation, a limitation, a recommendation that stays
internal, and one observation whose only receipt no client could follow.
Texts are distinct so the exported bytes can be searched for them.
"""

from __future__ import annotations

from src.api import dossier_resolver, dossier_store, investigation_store, investigations
from tests.unit.test_dossier_resolver import receipt
from tests.unit.test_dossier_store import _hex, publication, record

PREFIX = dossier_store.STAGING_PREFIX

RECEIPTS = {
    "meetups": "gqctx_" + _hex("journey_meetups"),
    "groups": "gqctx_" + _hex("journey_groups"),
    "sales": "gqctx_" + _hex("journey_sales"),
    "accra": "gqctx_" + _hex("journey_accra"),
}

TEXTS = {
    "clm_obs": "Weekend repair meetups are advertised in three Johannesburg community groups.",
    "clm_int": "Repair is turning into a social weekend activity rather than a chore.",
    "clm_chal": "One hardware chain reports that repair kit sales fell in August.",
    "clm_lim": "The groups sampled are all in Gauteng, so the pattern is not shown elsewhere.",
    "clm_rec": "Internal draft: pitch a repair cafe sponsorship before the holidays.",
    "clm_uncited": "Repair interest is also rising in Accra.",
}
ANSWER = "Weekend repair is becoming social in Johannesburg."
INTERNAL_QUESTION = "Is the August dip in kit sales seasonal?"


def _claim(claim_id, kind, receipt_ids, support_state="source_record", parents=(), limitations=()):
    return {
        "claim_id": claim_id,
        "text": TEXTS[claim_id],
        "kind": kind,
        "receipt_ids": list(receipt_ids),
        "reading_ids": [],
        "parent_claim_ids": list(parents),
        "support_state": support_state,
        "limitations": list(limitations),
        "falsifier": None,
    }


def main_dossier(frame: investigations.InvestigationFrame) -> dict:
    receipts = [
        receipt(RECEIPTS["meetups"], "R1", "2026-08-20T00:00:00Z"),
        receipt(RECEIPTS["groups"], "R2", "2026-08-24T00:00:00Z"),
        receipt(RECEIPTS["sales"], "R3", "2026-08-25T00:00:00Z"),
        # Outside the market scope and with no address: no client could follow it.
        receipt(RECEIPTS["accra"], "R4", "2026-08-26T00:00:00Z", market="gh", url=None),
    ]
    claims = [
        _claim("clm_obs", "observation", [RECEIPTS["meetups"]]),
        _claim(
            "clm_int",
            "interpretation",
            [RECEIPTS["meetups"], RECEIPTS["groups"]],
            "derived",
            parents=["clm_obs", "clm_chal"],
        ),
        _claim(
            "clm_chal",
            "observation",
            [RECEIPTS["sales"]],
            limitations=[dossier_resolver.CHALLENGE_FINDING],
        ),
        _claim("clm_lim", "limitation", [RECEIPTS["groups"]]),
        _claim("clm_rec", "recommendation", [RECEIPTS["groups"]], "derived", parents=["clm_int"]),
        _claim("clm_uncited", "observation", [RECEIPTS["accra"]]),
    ]
    return _dossier(frame, receipts, claims, ANSWER, [
        {"question_id": "q_1", "text": INTERNAL_QUESTION, "blocking": False},
    ])


def other_dossier(frame: investigations.InvestigationFrame, label: str) -> dict:
    """A small dossier whose every text names its label, for the late read and scope cases."""
    receipts = [receipt(RECEIPTS["meetups"], "R1", "2026-08-20T00:00:00Z")]
    claim = _claim("clm_obs", "observation", [RECEIPTS["meetups"]])
    claim["text"] = f"{label}: taxi rank traders are sharing repair tips."
    return _dossier(frame, receipts, [claim], f"{label} answer.", [])


def _dossier(frame, receipts, claims, answer, questions):
    payload = investigations.investigation_frame_payload(frame)
    value = record(
        investigation_id=investigations.investigation_id_for_frame(frame),
        frame=payload,
        scope={field: payload[field] for field in dossier_store.SCOPE_FIELDS},
    )
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
            "answer": answer,
            "intelligence": {
                "sections": [{"kind": "answer", "claim_ids": [claims[0]["claim_id"]]}],
                "claims": claims,
                "receipts": receipts,
            },
        },
    }
    value["review_state"] = {
        "selected_claim_ids": [],
        "claim_decision_refs": [],
        "relationship_decision_refs": [],
        "unresolved_questions": questions,
    }
    return value


def publish(*, bucket, investigation_id: str, kind: str, label: str = "") -> dict:
    """Publish the named dossier for an investigation the API already created."""
    parts = investigation_store.read_investigation_parts(
        bucket=bucket, prefix=investigation_store.STAGING_PREFIX, investigation_id=investigation_id
    )
    if parts is None:
        raise LookupError("create the investigation through the API first")
    frame = parts[0]
    value = main_dossier(frame) if kind == "main" else other_dossier(frame, label)
    created = dossier_store.create_dossier(
        bucket=bucket,
        prefix=PREFIX,
        body=dossier_store._canonical(value),
        publication=publication(),
        pointer_writer=lambda pointer: dossier_store.publish_pointer(
            pointer, bucket=bucket, prefix=PREFIX
        ),
    )
    return {
        "investigation_id": investigation_id,
        "dossier_version": created["publication"]["dossier_version"],
    }
