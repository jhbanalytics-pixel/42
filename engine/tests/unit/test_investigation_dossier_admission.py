"""The admission adapter: a dossier is exported only from material reread through the runtime.

The material is the real stored result the repository's own source
integration fixture produces through the whole question pipeline on a bucket
double. Every admission here happens through a fresh store over a copy of that
bucket, so nothing the producing run held in memory can leak into a read, and
every tampering case edits the stored bytes the way a broken store would.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest

from tests.unit import test_general_question_execution as execution
from tests.unit import test_general_question_store as store_fixture
from tests.unit import test_investigation_dossier as producer_fixture

MODULE = "src.analysis.open_intelligence.investigation_dossier_admission"
PROMPT_DIGEST = hashlib.sha256(b"prompt manifest").hexdigest()
BUILD_DIGEST = hashlib.sha256(b"publisher build").hexdigest()
CREATED_AT = "2026-09-13T09:00:00Z"


def module():
    return __import__(MODULE, fromlist=["admit_dossier_material"])


def _answering_handler(source):
    def handler(request):
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 50})
        body = json.loads(request.content)
        content = json.loads(body["contents"][0]["parts"][0]["text"])
        if "snapshot" not in content:
            draft = json.loads(
                source.planning_response["candidates"][0]["content"]["parts"][0]["text"]
            )
        else:
            reading = content["snapshot"]["readings"][0]
            draft = {
                "quote_observations": [],
                "reading_observations": [
                    {"claim_id": "observed_count", "reading_id": reading["reading_id"]}
                ],
                "interpretations": [],
                "inferences": [],
                "proposals": [],
                "answer": ["observed_count"],
            }
        return httpx.Response(
            200,
            json={
                # The version the admitted policy names, read from the policy.
                "modelVersion": source.store.policy["model"],
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"role": "model", "parts": [{"text": json.dumps(draft)}]},
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 50,
                    "candidatesTokenCount": 10,
                    "thoughtsTokenCount": 5,
                    "totalTokenCount": 65,
                },
            },
        )

    return handler


@pytest.fixture(scope="module")
def produced():
    """One real stored result, produced once; every test reads a copy of its bucket."""
    from tests.unit import test_general_question_source_integration as source_fixture

    # The fixture blocks socket connects once it is set up, and a new event
    # loop on this platform opens a socket pair, so the loop is made first.
    with asyncio.Runner() as runner, pytest.MonkeyPatch.context() as patch:
        source = source_fixture.setup_fixture(patch, seed_planning=False)
        patch.setattr(
            httpx,
            "AsyncHTTPTransport",
            lambda **_: httpx.MockTransport(_answering_handler(source)),
        )
        values = (
            source.store,
            None,
            source.invocation,
            source.runtime_identity,
            source.credentials,
        )
        result = runner.run(execution.execute(values, now=store_fixture.NOW + timedelta(seconds=2)))
    assert result["state"] == "partial"
    bucket = source.store._objects.bucket
    return SimpleNamespace(
        objects=copy.deepcopy(bucket.objects),
        versions=copy.deepcopy(bucket.versions),
        counter=bucket.counter,
        request_id=result["request_id"],
        result_digest=result["result_digest"],
        result_generation=int(result["result_generation"]),
        scope=copy.deepcopy(source.scope),
        policy=copy.deepcopy(source.store.policy),
        deployment_digest=source.store.deployment_digest,
    )


@pytest.fixture(autouse=True)
def reading_environment(monkeypatch):
    # The producing run admitted its context without the protected profiles;
    # a reader in the same deployment reads under the same rule.
    from src.analysis.open_intelligence import general_question_context_admission

    monkeypatch.setattr(general_question_context_admission, "_PROTECTED_CONTEXT_PROFILES", ())
    from src.analysis.open_intelligence import protected_context_registry

    monkeypatch.setattr(protected_context_registry, "_PROTECTED_CONTEXT_PROFILES", ())
    monkeypatch.setattr(protected_context_registry, "bridge_entries", lambda: ())


def fresh_store(produced, mutate=None):
    """A cold store over a copy of the produced bucket, optionally tampered."""
    bucket = store_fixture.Bucket()
    bucket.objects = copy.deepcopy(produced.objects)
    bucket.versions = copy.deepcopy(produced.versions)
    bucket.counter = produced.counter
    if mutate is not None:
        mutate(bucket)
    return store_fixture.module().GeneralQuestionStore(
        bucket, policy=produced.policy, deployment_digest=produced.deployment_digest
    )


def key(produced, tail):
    return store_fixture.PREFIX + f"requests/{produced.request_id}/{tail}"


def rewrite(bucket, name, change):
    """Replace one stored object's bytes in place, keeping its generation."""
    generation, raw = bucket.objects[name]
    value = json.loads(raw.decode("utf-8"))
    changed = change(value)
    raw = canonical_bytes(value if changed is None else changed)
    bucket.objects[name] = (generation, raw)
    bucket.versions[(name, generation)] = raw


def delete(bucket, name):
    generation, _ = bucket.objects.pop(name)
    bucket.versions.pop((name, generation), None)


def admit(produced, mutate=None, **overrides):
    values = {
        "request_id": produced.request_id,
        "scope": copy.deepcopy(produced.scope),
        "result_digest": produced.result_digest,
        "result_generation": produced.result_generation,
    }
    values.update(overrides)
    return module().admit_dossier_material(fresh_store(produced, mutate), **values)


def refused(produced, mutate=None, **overrides):
    with pytest.raises(module().DossierAdmissionRefused) as caught:
        admit(produced, mutate, **overrides)
    return caught.value


def frame(material):
    value = producer_fixture.frame()
    value["market_scope"] = ["ke", "ng", "za"]
    return value


def scope_of(frame_value):
    return {field: frame_value[field] for field in producer_fixture.scope()}


def export(material, **changes):
    frame_value = changes.pop("frame", None) or frame(material)
    values = {
        "investigation_id": producer_fixture.frame_validator(frame_value),
        "frame": frame_value,
        "scope": scope_of(frame_value),
        "review_state": producer_fixture.review_state()
        | {"selected_claim_ids": ["observed_count"]},
        "predecessor_version": None,
        "created_at": CREATED_AT,
        "prompt_manifest_digest": PROMPT_DIGEST,
        "publisher_build_digest": BUILD_DIGEST,
        "frame_validator": producer_fixture.frame_validator,
    }
    values.update(changes)
    return module().export_dossier_body(material, **values)


# Admission


def test_a_stored_result_is_readmitted_from_a_cold_store(produced):
    material = admit(produced)
    assert type(material) is module().AdmittedDossierMaterial
    assert material.request_reference == {
        "request_id": produced.request_id,
        "request_digest": material.admitted_answer["request_digest"],
        "thread_anchor_request_id": None,
        "parent_request_id": None,
    }
    assert set(material.bindings) == set(module().MATERIAL_BINDING_FIELDS)
    assert material.bindings["result_digest"] == produced.result_digest
    assert material.bindings["result_digest"] == canonical_digest(material.admitted_answer)
    assert material.bindings["snapshot_digest"] == material.evidence_snapshot["snapshot_digest"]
    assert material.bindings["policy_digest"] == material.policy["policy_digest"]
    assert (
        material.bindings["source_binding_digest"]
        == (material.evidence_snapshot["provenance"]["source"]["receipt"]["result_digest"])
    )
    window = material.evidence_snapshot["window"]
    assert material.temporal_binding == {
        "window_start": window["start"],
        "window_end": window["end"],
        "as_of": material.evidence_snapshot["as_of"],
        "source_cutoff": window["end"],
    }
    assert set(material.resolved_scope) == set(module().RESOLVED_SCOPE_FIELDS)
    assert material.admitted_answer["state"] == "partial"
    assert material.result_generation == produced.result_generation


def test_admitted_material_is_read_only_and_cannot_be_made_by_hand(produced):
    material = admit(produced)
    with pytest.raises(AttributeError):
        material.bindings = {}
    with pytest.raises(module().DossierAdmissionRefused) as caught:
        module().AdmittedDossierMaterial(object(), request_id="x")
    assert caught.value.code == "material_unadmitted"


def test_two_cold_admissions_export_identical_bytes(produced):
    first = export(admit(produced))
    second = export(admit(produced))
    assert first == second
    record = json.loads(first.decode("utf-8"))
    assert record["contract_version"] == "investigation_dossier_record_v1"
    assert record["request_reference"]["request_id"] == produced.request_id
    assert record["bindings"]["prompt_manifest_digest"] == PROMPT_DIGEST
    assert record["bindings"]["publisher_build_digest"] == BUILD_DIGEST
    assert record["admitted_answer"] == admit(produced).admitted_answer
    assert record["evidence_snapshot"]["window"]["closed"] is True
    assert hashlib.sha256(first).hexdigest() == canonical_digest(record)


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"request_id": ""}, "request_id_invalid"),
        ({"request_id": 7}, "request_id_invalid"),
        ({"result_digest": "abc"}, "result_pointer_invalid"),
        ({"result_generation": 0}, "result_pointer_invalid"),
        ({"result_generation": "38"}, "result_pointer_invalid"),
        ({"request_id": "00000000-0000-0000-0000-00000000ffff"}, "request_unknown"),
        ({"result_digest": "b" * 64}, "result_pointer_mismatch"),
    ],
)
def test_the_caller_must_name_the_selected_result_exactly(produced, overrides, code):
    assert refused(produced, **overrides).code == code


def test_a_moved_result_generation_is_refused(produced):
    error = refused(produced, result_generation=produced.result_generation + 1)
    assert error.code == "result_pointer_mismatch"


def test_a_request_from_another_client_scope_is_refused(produced):
    scope = dict(produced.scope, client_scope_id="other_client")
    assert refused(produced, scope=scope).code == "scope_invalid"


def test_a_substituted_result_body_is_refused(produced):
    name = key(produced, f"results/{produced.result_digest}.json")

    def move_text(bucket):
        rewrite(
            bucket,
            name,
            lambda value: value["response"]["intelligence"]["claims"][0].update(
                text="A different finding under the same digest."
            ),
        )

    # The content address of a result object is its digest; moved bytes under
    # the same name fail that check before any record validation runs.
    assert refused(produced, move_text).code == "object_digest_mismatch"


def test_a_substituted_snapshot_is_refused(produced):
    name = key(produced, "snapshot.json")

    def move_receipt(bucket):
        rewrite(bucket, name, lambda value: value["receipts"][0].update(excerpt="moved"))

    assert refused(produced, move_receipt).code == "result_binding_invalid"


def test_a_resigned_snapshot_is_still_refused_against_the_result(produced):
    name = key(produced, "snapshot.json")

    def resign(bucket):
        def change(value):
            value["receipts"][0]["excerpt"] = "moved"
            unsigned = {k: v for k, v in value.items() if k != "snapshot_digest"}
            value["snapshot_digest"] = canonical_digest(unsigned)

        rewrite(bucket, name, change)

    assert refused(produced, resign).code == "result_binding_invalid"


def test_a_substituted_request_is_refused(produced):
    name = key(produced, "request.json")

    def move_question(bucket):
        rewrite(bucket, name, lambda value: value.update(question="A different question?"))

    # The stored request is revalidated under its admitted policy before its
    # digest is compared, and a rewritten request no longer validates in scope.
    assert refused(produced, move_question).code == "scope_invalid"


def test_a_substituted_model_output_is_refused(produced):
    name = key(produced, "calls/answering.json")

    def move_output(bucket):
        def change(value):
            text = json.dumps(value)
            return json.loads(text.replace("observed_count", "invented_count"))

        rewrite(bucket, name, change)

    # The stored model output is content addressed too, so a rewrite is a
    # response that cannot be read rather than one that re-projects differently.
    assert refused(produced, move_output).code == "response_unavailable"


def test_a_missing_original_policy_is_refused(produced):
    policy_digest = produced.policy["policy_digest"]
    name = store_fixture.PREFIX + f"policies/{policy_digest}/policy.json"
    assert refused(produced, lambda bucket: delete(bucket, name)).code == "control_invalid"


def test_a_deleted_result_object_is_refused(produced):
    name = key(produced, f"results/{produced.result_digest}.json")
    assert refused(produced, lambda bucket: delete(bucket, name)).code == "result_unavailable"


def test_a_deleted_snapshot_is_refused(produced):
    error = refused(produced, lambda bucket: delete(bucket, key(produced, "snapshot.json")))
    assert error.code == "result_binding_invalid"


def test_an_unselected_or_inadmissible_result_state_is_refused(produced, monkeypatch):
    runtime = __import__(
        "src.analysis.open_intelligence.general_question_result", fromlist=["_selected"]
    )
    monkeypatch.setattr(runtime, "_selected", lambda store, context, validator: None)
    assert refused(produced).code == "result_unselected"

    def unavailable(store, context, validator):
        name = key(produced, f"results/{produced.result_digest}.json")
        _, raw = store._objects.bucket.objects[name]
        return dict(json.loads(raw.decode("utf-8")), state="unavailable")

    monkeypatch.setattr(runtime, "_selected", unavailable)
    error = refused(produced)
    assert (error.code, error.field) == ("result_state_inadmissible", "unavailable")


def test_a_runtime_code_outside_the_register_is_named_as_such(produced, monkeypatch):
    from src.analysis.open_intelligence.general_question_control import QuestionStoreError

    runtime = __import__(
        "src.analysis.open_intelligence.general_question_result", fromlist=["_context"]
    )

    def novel(store, request_id, scope):
        raise QuestionStoreError("something_new")

    monkeypatch.setattr(runtime, "_context", novel)
    error = refused(produced)
    assert (error.code, error.field) == ("runtime_refused", "something_new")


def test_every_refusal_code_is_registered(produced):
    assert module().REFUSAL_CODES == module().RUNTIME_REFUSALS | module().ADAPTER_REFUSALS
    assert "material_unadmitted" in module().ADAPTER_REFUSALS


# Export


def test_only_admitted_material_can_be_exported(produced):
    material = admit(produced)
    look_alike = SimpleNamespace(
        **{name: getattr(material, name) for name in module().MATERIAL_FIELDS},
        _token=object(),
    )
    for fake in (look_alike, {"bindings": material.bindings}, None):
        with pytest.raises(module().DossierAdmissionRefused) as caught:
            export(fake)
        assert caught.value.code == "material_unadmitted"
    unset = object.__new__(module().AdmittedDossierMaterial)
    with pytest.raises(module().DossierAdmissionRefused) as caught:
        export(unset)
    assert caught.value.code == "material_unadmitted"


def test_export_binds_the_prompt_and_publisher_digests_exactly(produced):
    material = admit(produced)
    body = export(material)
    version = hashlib.sha256(body).hexdigest()
    other = hashlib.sha256(export(material, prompt_manifest_digest="c" * 64)).hexdigest()
    assert other != version
    for field, value in (
        ("prompt_manifest_digest", "not-a-digest"),
        ("publisher_build_digest", ""),
        ("publisher_build_digest", None),
    ):
        with pytest.raises(ValueError) as caught:
            export(material, **{field: value})
        assert (caught.value.code, caught.value.field) == ("bindings_invalid", field)


@pytest.mark.parametrize(
    "field, value",
    [
        ("client_scope_id", "other_client"),
        ("market_scope", ["za"]),
        ("brand_config_id", "brand_x"),
        ("audience_lens_ids", ["lens_1"]),
        ("theme_id", "theme_x"),
        ("run_id", "investigation_run_002"),
        ("contract_version", "2.2.0"),
    ],
)
def test_each_scope_field_must_agree_with_the_frame_on_export(produced, field, value):
    material = admit(produced)
    frame_value = frame(material)
    scope = scope_of(frame_value) | {field: value}
    with pytest.raises(ValueError) as caught:
        export(material, frame=frame_value, scope=scope)
    assert (caught.value.code, caught.value.field) == ("scope_frame_mismatch", field)


def test_the_answer_must_sit_inside_the_investigation_scope(produced):
    material = admit(produced)
    frame_value = frame(material)
    frame_value["market_scope"] = ["ng"]
    with pytest.raises(ValueError) as caught:
        export(material, frame=frame_value)
    assert caught.value.code == "answer_scope_mismatch"


def test_the_cutoff_and_window_come_from_the_admitted_snapshot_only(produced):
    material = admit(produced)
    body = json.loads(export(material).decode("utf-8"))
    snapshot = body["evidence_snapshot"]
    assert body["temporal_binding"]["source_cutoff"] == snapshot["window"]["end"]
    assert body["temporal_binding"]["as_of"] == snapshot["as_of"]
    assert body["temporal_binding"]["window_start"] == snapshot["window"]["start"]
    assert body["admitted_answer"]["response"]["intelligence"]["window"] == snapshot["window"]


def test_selected_claims_and_predecessor_change_the_version(produced):
    material = admit(produced)
    base = hashlib.sha256(export(material)).hexdigest()
    review = producer_fixture.review_state() | {"selected_claim_ids": []}
    assert hashlib.sha256(export(material, review_state=review)).hexdigest() != base
    assert hashlib.sha256(export(material, predecessor_version=base)).hexdigest() != base
    unknown = producer_fixture.review_state() | {"selected_claim_ids": ["not_a_claim"]}
    with pytest.raises(ValueError) as caught:
        export(material, review_state=unknown)
    assert caught.value.code == "selected_claim_unknown"


@pytest.mark.parametrize(
    "error, expected",
    [
        (ValueError("snapshot_cap_version_unknown"), "snapshot_cap_version_unknown"),
        (ValueError("protected_context_registry_invalid"), "protected_context_registry_invalid"),
        (ValueError("PRIVATE_DETAIL"), "snapshot_invalid"),
        (RuntimeError("snapshot_cap_version_unknown"), "snapshot_invalid"),
    ],
)
def test_status_preserves_only_named_snapshot_cap_refusal(produced, monkeypatch, error, expected):
    from src.analysis.open_intelligence import general_question_execution
    from src.analysis.open_intelligence.general_question_control import QuestionStoreError

    def invalid(*args, **kwargs):
        raise error

    monkeypatch.setattr(
        general_question_execution, "validate_stored_general_question_snapshot", invalid
    )
    with pytest.raises(QuestionStoreError) as caught:
        general_question_execution.read_general_question_status(
            produced.request_id,
            store=fresh_store(produced),
            scope=produced.scope,
            now=store_fixture.NOW + timedelta(seconds=30),
        )
    assert caught.value.code == expected
    assert "PRIVATE_DETAIL" not in str(caught.value)


@pytest.mark.parametrize(
    "code", ["snapshot_cap_version_unknown", "protected_context_registry_invalid"]
)
def test_dossier_preserves_named_snapshot_cap_refusal(produced, monkeypatch, code):
    from src.analysis.open_intelligence import general_question_execution

    def invalid(*args, **kwargs):
        raise ValueError(code)

    monkeypatch.setattr(
        general_question_execution, "validate_stored_general_question_snapshot", invalid
    )
    error = refused(produced)
    assert (error.code, error.field) == (code, None)
