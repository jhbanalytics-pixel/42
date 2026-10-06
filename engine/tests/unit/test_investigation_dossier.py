from __future__ import annotations

import copy
import hashlib
import json

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_digest

MODULE = "src.analysis.open_intelligence.investigation_dossier"

# The existing frame payload shape from the app boundary. The engine cannot
# import the app package, so identity is established through the injected
# validator, which mirrors the app rule exactly: sorted compact JSON of the
# fifteen fields, prefixed inv_.
FRAME_FIELDS = frozenset(
    {
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "contract_version",
        "decision_question",
        "time_horizon_days",
        "brand_context",
        "known_assumptions",
        "change_my_mind_if",
        "research_role_id",
        "research_role_version",
        "output_mode",
    }
)


def frame_validator(frame):
    if not isinstance(frame, dict) or set(frame) != FRAME_FIELDS:
        raise ValueError("stored investigation frame is invalid")
    canonical = json.dumps(frame, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "inv_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _hex(label):
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def frame():
    return {
        "client_scope_id": "ogilvy_default",
        "market_scope": ["za"],
        "brand_config_id": None,
        "audience_lens_ids": [],
        "theme_id": None,
        "run_id": "investigation_run_001",
        "contract_version": "2.1.0",
        "decision_question": "Which emerging behaviour should the brand act on in six weeks?",
        "time_horizon_days": 42,
        "brand_context": None,
        "known_assumptions": [],
        "change_my_mind_if": [],
        "research_role_id": None,
        "research_role_version": None,
        "output_mode": "internal_working_paper",
    }


def scope():
    value = frame()
    return {
        key: value[key]
        for key in (
            "client_scope_id",
            "market_scope",
            "brand_config_id",
            "audience_lens_ids",
            "theme_id",
            "run_id",
            "contract_version",
        )
    }


# The evidence snapshot is the real one the repository's snapshot test builds
# through the engine's own builder, so its window, as_of, bindings, receipts
# and provenance are exactly what the producer meets in staging. It is built
# once and deep copied on every use.
_REAL_SNAPSHOT = None


def real_snapshot():
    global _REAL_SNAPSHOT
    if _REAL_SNAPSHOT is None:
        from tests.unit import test_general_question_snapshot as snapshot_fixture

        patch = pytest.MonkeyPatch()
        try:
            m, store, plan = snapshot_fixture.fixture(patch)
            _REAL_SNAPSHOT = snapshot_fixture.build(m, store, plan)["snapshot"]
        finally:
            patch.undo()
    return copy.deepcopy(_REAL_SNAPSHOT)


def resign(snapshot):
    snapshot.pop("snapshot_digest", None)
    snapshot["snapshot_digest"] = canonical_digest(snapshot)
    return snapshot


_SNAPSHOT = real_snapshot()
REQUEST_ID = _SNAPSHOT["request_id"]
REQUEST_DIGEST = _SNAPSHOT["request_digest"]
INTAKE = _SNAPSHOT["intake_digest"]
PLAN = _SNAPSHOT["plan_digest"]
POLICY = _SNAPSHOT["policy_digest"]
DEPLOYMENT = _SNAPSHOT["deployment_digest"]
SOURCE_BINDING = _SNAPSHOT["provenance"]["source"]["receipt"]["result_digest"]
AS_OF = _SNAPSHOT["as_of"]
WINDOW = _SNAPSHOT["window"]
RECEIPTS = tuple(item["receipt_id"] for item in _SNAPSHOT["receipts"])
PROMPT = _hex("prompt_manifest")
BUILD = _hex("publisher_build")


def temporal_binding():
    return {
        "window_start": WINDOW["start"],
        "window_end": WINDOW["end"],
        "as_of": AS_OF,
        "source_cutoff": WINDOW["end"],
    }


def request_reference():
    return {
        "request_id": REQUEST_ID,
        "request_digest": REQUEST_DIGEST,
        "thread_anchor_request_id": None,
        "parent_request_id": None,
    }


def evidence_snapshot():
    return real_snapshot()


def contextual_snapshot():
    """The same real snapshot carrying a protected context provenance instead."""
    value = real_snapshot()
    value["provenance"] = {
        "profile": "protected_context_v1",
        "source_binding": {
            "profile_id": "context_profile_v1",
            "source_binding_digest": _hex("capture binding"),
        },
        "selected_row_digests": [item["content_digest"] for item in value["receipts"]],
    }
    return resign(value)


def claim(claim_id, kind, receipt_ids, support_state="source_record"):
    return {
        "claim_id": claim_id,
        "text": "Repair tutorials recur across two independent source families.",
        "kind": kind,
        "receipt_ids": list(receipt_ids),
        "reading_ids": [],
        "parent_claim_ids": [],
        "support_state": support_state,
        "limitations": [],
        "falsifier": None,
    }


def admitted_answer(snapshot=None):
    snapshot = snapshot or evidence_snapshot()
    return {
        "contract_version": "general_question_result_record_v1",
        "request_id": REQUEST_ID,
        "request_digest": REQUEST_DIGEST,
        "intake_digest": INTAKE,
        "policy_digest": POLICY,
        "deployment_digest": DEPLOYMENT,
        "plan_digest": PLAN,
        "snapshot_digest": snapshot["snapshot_digest"],
        "state": "complete",
        "recorded_at": "2026-09-06T20:01:00Z",
        "response": {
            "intelligence": {
                "contract_version": "general_question_answer_v1",
                "request_id": REQUEST_ID,
                "request_digest": REQUEST_DIGEST,
                "status": "complete",
                "resolved_scope": {
                    "client_scope_id": "ogilvy_default",
                    "market_scope": ["za"],
                    "brand_config_id": None,
                    "audience_lens_ids": [],
                    "theme_id": None,
                },
                "window": copy.deepcopy(snapshot["window"]),
                "as_of": snapshot["as_of"],
                "snapshot_id": snapshot["snapshot_id"],
                "sections": [{"kind": "answer", "claim_ids": ["clm_1", "clm_2"]}],
                "claims": [
                    claim("clm_1", "observation", RECEIPTS[:1]),
                    claim("clm_2", "interpretation", RECEIPTS, "derived"),
                ],
                "receipts": snapshot["receipts"],
                "readings": [],
                "limitations": [],
                "missing_work": [],
                "clarification": None,
                "review_required": False,
                "ready_for_downstream": True,
                "usage": {"status": "resolved"},
            }
        },
    }


def bindings(answer=None, snapshot=None):
    snapshot = snapshot or evidence_snapshot()
    answer = answer or admitted_answer(snapshot)
    module = __import__(MODULE, fromlist=["_source_binding_digest"])
    return {
        "policy_digest": POLICY,
        "deployment_digest": DEPLOYMENT,
        "source_binding_digest": module._source_binding_digest(snapshot["provenance"]),
        "snapshot_digest": snapshot["snapshot_digest"],
        "result_digest": canonical_digest(answer),
        "prompt_manifest_digest": PROMPT,
        "publisher_build_digest": BUILD,
    }


def review_state():
    return {
        "selected_claim_ids": [],
        "claim_decision_refs": [],
        "relationship_decision_refs": [],
        "unresolved_questions": [
            {
                "question_id": "q_1",
                "text": "Does the repair pattern hold outside Gauteng?",
                "blocking": True,
            }
        ],
    }


def inputs(**changes):
    snapshot = evidence_snapshot()
    answer = admitted_answer(snapshot)
    value = {
        "investigation_id": frame_validator(frame()),
        "frame": frame(),
        "scope": scope(),
        "temporal_binding": temporal_binding(),
        "request_reference": request_reference(),
        "admitted_answer": answer,
        "evidence_snapshot": snapshot,
        "bindings": bindings(answer, snapshot),
        "review_state": review_state(),
        "predecessor_version": None,
        "created_at": "2026-09-12T00:00:00Z",
        "frame_validator": frame_validator,
    }
    value.update(changes)
    return value


def build(**changes):
    module = __import__(MODULE, fromlist=["build_dossier_body"])
    return module.build_dossier_body(**inputs(**changes))


def refused(**changes):
    module = __import__(MODULE, fromlist=["DossierRecordInvalid"])
    with pytest.raises(module.DossierRecordInvalid) as caught:
        build(**changes)
    return caught.value


def test_version_binds_nested_review_and_result_bytes():
    def version(value):
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        return hashlib.sha256(raw).hexdigest()

    original = {"result_digest": "a" * 64, "selected_claim_ids": ["c1"]}
    assert version(original) != version(dict(original, selected_claim_ids=["c2"]))
    assert version(original) != version(dict(original, result_digest="b" * 64))


def test_body_is_exact_sorted_compact_utf8_json_and_version_is_its_sha256():
    module = __import__(MODULE, fromlist=["dossier_version"])
    body = build()
    assert isinstance(body, bytes)
    record = json.loads(body.decode("utf-8"))
    assert list(record) == sorted(record)
    assert set(record) == {
        "contract_version",
        "investigation_id",
        "frame",
        "scope",
        "temporal_binding",
        "request_reference",
        "admitted_answer",
        "evidence_snapshot",
        "bindings",
        "review_state",
        "predecessor_version",
        "created_at",
    }
    assert record["contract_version"] == "investigation_dossier_record_v1"
    assert record["investigation_id"] == frame_validator(frame())
    assert record["predecessor_version"] is None
    assert record["created_at"] == "2026-09-12T00:00:00Z"
    assert record["frame"] == frame()
    assert record["review_state"]["unresolved_questions"][0]["blocking"] is True
    assert body == json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    assert module.dossier_version(body) == hashlib.sha256(body).hexdigest()
    assert module.dossier_version(body) == module.dossier_version(build())


def test_version_changes_with_selected_claims_result_and_predecessor():
    module = __import__(MODULE, fromlist=["dossier_version"])
    base = module.dossier_version(build())
    selected = review_state()
    selected["selected_claim_ids"] = ["clm_1"]
    assert module.dossier_version(build(review_state=selected)) != base

    snapshot = evidence_snapshot()
    answer = admitted_answer(snapshot)
    answer["response"]["intelligence"]["claims"][0]["text"] = "A different reading."
    rebound = build(admitted_answer=answer, bindings=bindings(answer, snapshot))
    assert module.dossier_version(rebound) != base

    edited = build(predecessor_version=base)
    assert module.dossier_version(edited) != base
    assert json.loads(edited)["predecessor_version"] == base


def test_non_ascii_text_is_kept_as_utf8_rather_than_escaped():
    value = frame()
    apostrophe = chr(0x2019)
    value["decision_question"] = (
        "Which behaviour should the brand act on in Soweto" + apostrophe + "s?"
    )
    body = build(
        investigation_id=frame_validator(value),
        frame=value,
        scope=scope() | {"run_id": "investigation_run_001"},
    )
    assert ("Soweto" + apostrophe + "s").encode() in body
    assert b"\\u2019" not in body


def test_frame_identity_and_validator_are_required():
    assert refused(investigation_id="inv_other").code == "investigation_identity_mismatch"
    assert refused(investigation_id="not_an_investigation").code == "investigation_id_invalid"
    assert refused(investigation_id=REQUEST_ID).code == "investigation_id_invalid"
    broken = frame()
    del broken["output_mode"]
    assert refused(frame=broken).code == "frame_invalid"
    assert refused(frame={}).code == "frame_invalid"
    module = __import__(MODULE, fromlist=["build_dossier_body"])
    values = inputs()
    del values["frame_validator"]
    with pytest.raises(TypeError):
        module.build_dossier_body(**values)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("client_scope_id", "other_scope", "scope_frame_mismatch"),
        ("client_scope_id", "", "scope_invalid"),
        ("market_scope", ["ng"], "scope_frame_mismatch"),
        ("market_scope", [], "scope_invalid"),
        ("market_scope", ["ZA"], "scope_invalid"),
        ("market_scope", ["za", "za"], "scope_invalid"),
        ("market_scope", "za", "scope_invalid"),
        ("brand_config_id", "brand_x", "scope_frame_mismatch"),
        ("brand_config_id", 7, "scope_invalid"),
        ("audience_lens_ids", ["lens_a"], "scope_frame_mismatch"),
        ("audience_lens_ids", [""], "scope_invalid"),
        ("audience_lens_ids", "lens_a", "scope_invalid"),
        ("theme_id", "theme_x", "scope_frame_mismatch"),
        ("theme_id", "", "scope_invalid"),
        ("run_id", "investigation_run_002", "scope_frame_mismatch"),
        ("run_id", None, "scope_invalid"),
        ("contract_version", "2.0.0", "scope_frame_mismatch"),
        ("contract_version", None, "scope_invalid"),
    ],
)
def test_each_of_the_seven_scope_fields_is_bound_to_the_frame(field, value, code):
    changed = scope()
    changed[field] = value
    error = refused(scope=changed)
    assert error.code == code
    assert error.field == field


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.pop("run_id"),
        lambda value: value.update(extra="x"),
        lambda value: value.clear(),
    ],
    ids=["missing_field", "eighth_field", "empty"],
)
def test_scope_shape_is_exactly_seven_fields(mutate):
    changed = scope()
    mutate(changed)
    assert refused(scope=changed).code == "scope_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("window_start", "2026-09-06"),
        ("window_start", "20260801"),
        ("window_end", None),
        ("window_end", "2026-13-01"),
        ("as_of", "2026-08-29"),
        ("as_of", "2026-08-29T06:30:00+02:00"),
        ("source_cutoff", "2026-09-07"),
        ("source_cutoff", ""),
        ("source_cutoff", "2026-08-28T00:00:00Z"),
    ],
)
def test_each_time_field_is_validated_and_a_cutoff_cannot_pass_as_of(field, value):
    changed = temporal_binding()
    changed[field] = value
    error = refused(temporal_binding=changed)
    assert error.code == "temporal_binding_invalid"
    assert error.field == field


def test_time_binding_shape_is_exactly_four_fields():
    changed = temporal_binding()
    changed["cutoff"] = changed.pop("source_cutoff")
    assert refused(temporal_binding=changed).code == "temporal_binding_invalid"
    assert refused(temporal_binding={}).code == "temporal_binding_invalid"
    changed = temporal_binding()
    changed["window"] = {"start": "2026-08-01", "end": "2026-08-28"}
    assert refused(temporal_binding=changed).code == "temporal_binding_invalid"


def test_time_binding_must_agree_with_the_admitted_material():
    changed = temporal_binding()
    changed["as_of"] = "2026-09-06T21:00:00Z"
    assert refused(temporal_binding=changed).code == "temporal_binding_mismatch"
    changed = temporal_binding()
    changed["window_start"] = "2026-08-24"
    assert refused(temporal_binding=changed).code == "temporal_binding_mismatch"


def test_a_substituted_request_is_refused():
    changed = request_reference()
    changed["request_id"] = "0b6d1f6a-0000-4000-8000-000000000000"
    assert refused(request_reference=changed).code == "request_substituted"
    changed = request_reference()
    changed["request_digest"] = _hex("another request")
    assert refused(request_reference=changed).code == "request_substituted"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", ""),
        ("request_id", None),
        ("request_digest", "abc"),
        ("thread_anchor_request_id", ""),
        ("parent_request_id", 3),
    ],
)
def test_request_reference_fields_are_typed_with_nullable_parents(field, value):
    changed = request_reference()
    changed[field] = value
    error = refused(request_reference=changed)
    assert error.code == "request_reference_invalid"
    assert error.field == field


def test_request_reference_parents_may_be_null_or_named():
    changed = request_reference()
    changed["thread_anchor_request_id"] = REQUEST_ID
    changed["parent_request_id"] = "0b6d1f6a-0000-4000-8000-000000000000"
    record = json.loads(build(request_reference=changed))
    assert record["request_reference"] == changed
    del changed["parent_request_id"]
    assert refused(request_reference=changed).code == "request_reference_invalid"


def test_a_substituted_result_is_refused_whether_bytes_or_digest_move():
    snapshot = evidence_snapshot()
    answer = admitted_answer(snapshot)
    answer["response"]["intelligence"]["claims"][0]["text"] = "A quietly edited finding."
    assert refused(admitted_answer=answer).code == "result_substituted"
    changed = bindings()
    changed["result_digest"] = _hex("another result")
    assert refused(bindings=changed).code == "result_substituted"
    other = admitted_answer(snapshot)
    other["request_id"] = "0b6d1f6a-0000-4000-8000-000000000000"
    assert (
        refused(admitted_answer=other, bindings=bindings(other, snapshot)).code
        == "request_substituted"
    )


@pytest.mark.parametrize("state", ["unavailable", "refused", "held", "needs_clarification"])
def test_only_a_complete_or_partial_answer_can_be_admitted(state):
    snapshot = evidence_snapshot()
    answer = admitted_answer(snapshot)
    answer["state"] = state
    error = refused(admitted_answer=answer, bindings=bindings(answer, snapshot))
    assert error.code == "answer_state_invalid"


def test_a_substituted_snapshot_is_refused():
    snapshot = evidence_snapshot()
    snapshot["receipts"][0]["published_at"] = "2026-08-30T00:00:00Z"
    assert refused(evidence_snapshot=snapshot).code == "snapshot_substituted"
    resigned = evidence_snapshot()
    resigned["receipts"][0]["published_at"] = "2026-08-30T00:00:00Z"
    del resigned["snapshot_digest"]
    resigned["snapshot_digest"] = canonical_digest(resigned)
    assert refused(evidence_snapshot=resigned).code == "snapshot_substituted"
    changed = bindings()
    changed["snapshot_digest"] = _hex("another snapshot")
    assert refused(bindings=changed).code == "snapshot_substituted"


def test_source_binding_and_policy_bindings_must_match_the_admitted_material():
    changed = bindings()
    changed["source_binding_digest"] = _hex("another capture")
    assert refused(bindings=changed).code == "source_binding_mismatch"
    for field in ("policy_digest", "deployment_digest"):
        changed = bindings()
        changed[field] = _hex("another " + field)
        error = refused(bindings=changed)
        assert error.code == "bindings_mismatch"
        assert error.field == field


@pytest.mark.parametrize(
    "field",
    [
        "policy_digest",
        "deployment_digest",
        "source_binding_digest",
        "snapshot_digest",
        "result_digest",
        "prompt_manifest_digest",
        "publisher_build_digest",
    ],
)
def test_every_binding_digest_is_required_and_exact(field):
    for bad in ("", None, "A" * 64, "f" * 63, 12):
        changed = bindings()
        changed[field] = bad
        error = refused(bindings=changed)
        assert error.code == "bindings_invalid"
        assert error.field == field
    changed = bindings()
    del changed[field]
    assert refused(bindings=changed).code == "bindings_invalid"


def test_review_state_binds_known_claims_and_blocking_flags():
    changed = review_state()
    changed["selected_claim_ids"] = ["clm_9"]
    assert refused(review_state=changed).code == "selected_claim_unknown"
    changed = review_state()
    changed["selected_claim_ids"] = ["clm_1", "clm_1"]
    assert refused(review_state=changed).code == "review_state_invalid"
    changed = review_state()
    changed["unresolved_questions"] = [{"question_id": "q_1", "text": "Open?"}]
    error = refused(review_state=changed)
    assert error.code == "review_state_invalid"
    assert error.field == "unresolved_questions"
    changed = review_state()
    changed["unresolved_questions"][0]["blocking"] = "yes"
    assert refused(review_state=changed).code == "review_state_invalid"
    changed = review_state()
    del changed["claim_decision_refs"]
    assert refused(review_state=changed).code == "review_state_invalid"
    changed = review_state()
    changed["relationship_decision_refs"] = "dec_1"
    assert refused(review_state=changed).code == "review_state_invalid"


def test_selected_claims_are_retained_exactly_and_bound_to_the_answer():
    changed = review_state()
    changed["selected_claim_ids"] = ["clm_2", "clm_1"]
    changed["claim_decision_refs"] = ["dec_1"]
    changed["unresolved_questions"] = []
    record = json.loads(build(review_state=changed))
    assert record["review_state"] == changed


def test_prompt_and_publisher_digests_are_bound_into_the_body():
    record = json.loads(build())
    assert record["bindings"]["prompt_manifest_digest"] == PROMPT
    assert record["bindings"]["publisher_build_digest"] == BUILD
    changed = bindings()
    changed["prompt_manifest_digest"] = "not a digest"
    error = refused(bindings=changed)
    assert error.code == "bindings_invalid"
    assert error.field == "prompt_manifest_digest"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("frame", {}, "frame_invalid"),
        ("scope", {}, "scope_invalid"),
        ("temporal_binding", {}, "temporal_binding_invalid"),
        ("request_reference", {}, "request_reference_invalid"),
        ("admitted_answer", {}, "admitted_answer_invalid"),
        ("evidence_snapshot", {}, "evidence_snapshot_invalid"),
        ("bindings", {}, "bindings_invalid"),
        ("review_state", {}, "review_state_invalid"),
        ("admitted_answer", [], "admitted_answer_invalid"),
        ("evidence_snapshot", None, "evidence_snapshot_invalid"),
    ],
)
def test_the_braces_in_the_contract_are_typed_objects_not_empty_values(field, value, code):
    assert refused(**{field: value}).code == code


def test_a_snapshot_without_receipts_or_source_binding_is_refused():
    snapshot = evidence_snapshot()
    snapshot["receipts"] = []
    del snapshot["snapshot_digest"]
    snapshot["snapshot_digest"] = canonical_digest(snapshot)
    answer = admitted_answer(snapshot)
    error = refused(
        evidence_snapshot=snapshot, admitted_answer=answer, bindings=bindings(answer, snapshot)
    )
    assert error.code == "evidence_snapshot_invalid"
    assert error.field == "receipts"
    snapshot = evidence_snapshot()
    snapshot["provenance"] = {"profile": "protected_context_v1"}
    resign(snapshot)
    answer = admitted_answer(snapshot)
    unbound = bindings(answer, snapshot)
    unbound["source_binding_digest"] = SOURCE_BINDING
    error = refused(evidence_snapshot=snapshot, admitted_answer=answer, bindings=unbound)
    assert error.code == "source_binding_mismatch"


def test_a_claim_citing_an_unknown_receipt_is_refused():
    snapshot = evidence_snapshot()
    answer = admitted_answer(snapshot)
    answer["response"]["intelligence"]["claims"][0]["receipt_ids"] = ["gqctx_" + _hex("foreign")]
    error = refused(admitted_answer=answer, bindings=bindings(answer, snapshot))
    assert error.code == "admitted_answer_invalid"
    assert error.field == "claims"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("predecessor_version", "", "predecessor_version_invalid"),
        ("predecessor_version", "B" * 64, "predecessor_version_invalid"),
        ("predecessor_version", 1, "predecessor_version_invalid"),
        ("created_at", "2026-09-12", "created_at_invalid"),
        ("created_at", "2026-09-12T00:00:00+00:00", "created_at_invalid"),
        ("created_at", None, "created_at_invalid"),
    ],
)
def test_predecessor_and_created_at_are_exact(field, value, code):
    assert refused(**{field: value}).code == code


def test_non_json_native_values_never_reach_the_bytes():
    value = frame()
    value["market_scope"] = ("za",)
    error = refused(frame=value, investigation_id=frame_validator(frame()))
    assert error.code == "frame_invalid"
    snapshot = evidence_snapshot()
    answer = admitted_answer(snapshot)
    bound = bindings(answer, snapshot)
    answer["response"]["intelligence"]["usage"]["cost"] = float("nan")
    error = refused(admitted_answer=answer, bindings=bound)
    assert error.code == "admitted_answer_invalid"


def test_inputs_are_copied_so_a_later_mutation_cannot_move_the_bytes():
    values = inputs()
    module = __import__(MODULE, fromlist=["build_dossier_body"])
    body = module.build_dossier_body(**values)
    values["review_state"]["selected_claim_ids"].append("clm_1")
    values["frame"]["decision_question"] = "changed"
    assert json.loads(body)["review_state"]["selected_claim_ids"] == []
    assert body == module.build_dossier_body(**copy.deepcopy(inputs()))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_scope_id", "other_scope"),
        ("market_scope", ["ke"]),
        ("market_scope", []),
        ("brand_config_id", "brand_x"),
        ("audience_lens_ids", ["lens_a"]),
        ("theme_id", "theme_x"),
    ],
)
def test_the_answer_must_have_run_inside_the_investigation_scope(field, value):
    snapshot = evidence_snapshot()
    answer = admitted_answer(snapshot)
    answer["response"]["intelligence"]["resolved_scope"][field] = value
    error = refused(admitted_answer=answer, bindings=bindings(answer, snapshot))
    assert error.code == "answer_scope_mismatch"


def test_an_answer_over_a_subset_of_the_investigation_markets_is_admitted():
    value = frame()
    value["market_scope"] = ["za", "ng", "ke"]
    widened = scope()
    widened["market_scope"] = ["za", "ng", "ke"]
    body = build(investigation_id=frame_validator(value), frame=value, scope=widened)
    assert json.loads(body)["scope"]["market_scope"] == ["za", "ng", "ke"]


def test_the_real_three_key_window_is_accepted_and_retained_with_closed():
    record = json.loads(build())
    assert record["evidence_snapshot"]["window"] == {
        "start": "2026-08-23",
        "end": "2026-09-05",
        "closed": True,
    }
    assert record["admitted_answer"]["response"]["intelligence"]["window"] == WINDOW
    assert record["temporal_binding"] == {
        "window_start": "2026-08-23",
        "window_end": "2026-09-05",
        "as_of": "2026-09-06T20:00:00.000000Z",
        "source_cutoff": "2026-09-05",
    }
    assert record["evidence_snapshot"]["receipts"][0]["receipt_id"].startswith("gqr_")
    assert record["bindings"]["source_binding_digest"] == SOURCE_BINDING


@pytest.mark.parametrize(
    "mutate",
    [
        lambda window: window.pop("closed"),
        lambda window: window.update(closed="yes"),
        lambda window: window.update(closed=1),
        lambda window: window.update(closed=None),
        lambda window: window.pop("end"),
    ],
    ids=["missing_closed", "text_closed", "int_closed", "null_closed", "missing_end"],
)
def test_a_window_missing_closed_or_with_a_non_bool_closed_is_refused(mutate):
    snapshot = evidence_snapshot()
    mutate(snapshot["window"])
    # The shape is refused before any digest is compared, signed or not.
    error = refused(evidence_snapshot=snapshot)
    assert (error.code, error.field) == ("evidence_snapshot_invalid", "window")
    resign(snapshot)
    answer = admitted_answer(snapshot)
    error = refused(
        evidence_snapshot=snapshot, admitted_answer=answer, bindings=bindings(answer, snapshot)
    )
    assert error.code == "evidence_snapshot_invalid"
    assert error.field == "window"


def test_a_closed_window_must_end_before_the_as_of_day():
    snapshot = evidence_snapshot()
    snapshot["window"]["end"] = "2026-09-06"
    resign(snapshot)
    answer = admitted_answer(snapshot)
    binding = temporal_binding()
    binding["window_end"] = "2026-09-06"
    error = refused(
        evidence_snapshot=snapshot,
        admitted_answer=answer,
        bindings=bindings(answer, snapshot),
        temporal_binding=binding,
    )
    assert error.code == "temporal_binding_mismatch"
    assert error.field == "window_closed"
    snapshot["window"]["closed"] = False
    resign(snapshot)
    answer = admitted_answer(snapshot)
    body = build(
        evidence_snapshot=snapshot,
        admitted_answer=answer,
        bindings=bindings(answer, snapshot),
        temporal_binding=binding,
    )
    assert json.loads(body)["evidence_snapshot"]["window"]["closed"] is False


def test_the_answer_window_and_as_of_must_be_the_snapshot_window():
    snapshot = evidence_snapshot()
    answer = admitted_answer(snapshot)
    answer["response"]["intelligence"]["window"]["closed"] = False
    error = refused(admitted_answer=answer, bindings=bindings(answer, snapshot))
    assert error.code == "temporal_binding_mismatch"
    assert error.field == "admitted_answer"
    answer = admitted_answer(snapshot)
    answer["response"]["intelligence"]["as_of"] = "2026-09-06T21:00:00.000000Z"
    error = refused(admitted_answer=answer, bindings=bindings(answer, snapshot))
    assert error.code == "temporal_binding_mismatch"


def test_a_contextual_snapshot_binds_through_its_capture_binding():
    snapshot = contextual_snapshot()
    answer = admitted_answer(snapshot)
    bound = bindings(answer, snapshot)
    assert bound["source_binding_digest"] == _hex("capture binding")
    record = json.loads(build(evidence_snapshot=snapshot, admitted_answer=answer, bindings=bound))
    assert record["evidence_snapshot"]["provenance"]["profile"] == "protected_context_v1"
    bound["source_binding_digest"] = SOURCE_BINDING
    error = refused(evidence_snapshot=snapshot, admitted_answer=answer, bindings=bound)
    assert error.code == "source_binding_mismatch"
