"""Admit stored question material for a dossier by rereading it through the runtime.

The producer in investigation_dossier checks the cross bindings between the
parts it is handed. This module is the only way those parts are obtained for
an export. It takes a store handle and identifiers, never a result, snapshot
or policy object, and rereads the request, intake, admission, original
policy, plan, snapshot and selected result through the same readers the
question runtime uses to serve its own status: the scoped request read that
validates the stored request against the policy digest the control admitted
it under, the selected result read pinned to the digest and generation the
control names, the stored plan and snapshot validation with the query
sidecars, and the persisted answer validation that re-projects the stored
model output and requires it to equal the stored response byte for byte.

What comes back is material storage proved on this read. Nothing a caller
holds in memory can stand in for it, and a body is exported only from it.
"""

from __future__ import annotations

import copy
import re

from src.analysis.open_intelligence import general_question_result as result_runtime
from src.analysis.open_intelligence.general_question_control import QuestionStoreError
from src.analysis.open_intelligence.general_question_execution import (
    question_answer_validators,
)
from src.analysis.open_intelligence.general_question_runtime import _answer_material
from src.analysis.open_intelligence.investigation_dossier import (
    ADMITTED_ANSWER_STATES,
    _source_binding_digest,
    build_dossier_body,
)

# Refusals the runtime readers raise and the adapter passes through by name.
RUNTIME_REFUSALS = frozenset(
    {
        "request_unknown",
        "request_invalid",
        "scope_invalid",
        "control_invalid",
        "protected_context_registry_invalid",
        "snapshot_cap_version_unknown",
        "control_unavailable",
        "control_conflict",
        "storage_unavailable",
        "storage_target_invalid",
        "object_invalid",
        "object_digest_mismatch",
        "object_version_unavailable",
        "object_too_large",
        "result_unavailable",
        "result_invalid",
        "result_binding_invalid",
        "result_support_unverified",
        "answer_binding_invalid",
        "snapshot_unavailable",
        "response_unknown",
        "response_unavailable",
    }
)
# Refusals the adapter itself raises.
ADAPTER_REFUSALS = frozenset(
    {
        "request_id_invalid",
        "result_pointer_invalid",
        "result_unselected",
        "result_pointer_mismatch",
        "result_state_inadmissible",
        "source_binding_missing",
        "material_unadmitted",
        "runtime_refused",
    }
)
REFUSAL_CODES = RUNTIME_REFUSALS | ADAPTER_REFUSALS

RESOLVED_SCOPE_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
)
MATERIAL_BINDING_FIELDS = (
    "policy_digest",
    "deployment_digest",
    "source_binding_digest",
    "snapshot_digest",
    "result_digest",
)

MATERIAL_FIELDS = (
    "request_id",
    "request_reference",
    "resolved_scope",
    "policy",
    "plan",
    "evidence_snapshot",
    "admitted_answer",
    "result_generation",
    "bindings",
    "temporal_binding",
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = object()


class DossierAdmissionRefused(ValueError):
    def __init__(self, code: str, field: str | None = None) -> None:
        self.code = code
        self.field = field
        super().__init__(code if field is None else f"{code}:{field}")


def _refuse(code: str, field: str | None = None) -> None:
    raise DossierAdmissionRefused(code, field)


def _from_runtime(error: QuestionStoreError) -> DossierAdmissionRefused:
    code = getattr(error, "code", None)
    if code in RUNTIME_REFUSALS:
        return DossierAdmissionRefused(code)
    return DossierAdmissionRefused("runtime_refused", code if type(code) is str else None)


class AdmittedDossierMaterial:
    """Material the adapter reread and proved. Only admit_dossier_material makes one."""

    __slots__ = (*MATERIAL_FIELDS, "_token")

    def __init__(self, token: object, **values: object) -> None:
        if token is not _TOKEN:
            _refuse("material_unadmitted")
        if set(values) != set(MATERIAL_FIELDS):
            _refuse("material_unadmitted")
        for name in MATERIAL_FIELDS:
            object.__setattr__(self, name, values[name])
        object.__setattr__(self, "_token", token)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("admitted material is read only")


def admit_dossier_material(
    store: object,
    *,
    request_id: str,
    scope: dict,
    result_digest: str,
    result_generation: int,
) -> AdmittedDossierMaterial:
    """Reread one selected result and everything it binds, or refuse by name.

    The caller names what it expects, the result digest and object generation
    the publication will pin. The control ledger must name the same selected
    result, or the caller is describing a result the runtime never selected.
    """
    if type(request_id) is not str or not request_id:
        _refuse("request_id_invalid")
    if (
        type(result_digest) is not str
        or _DIGEST.fullmatch(result_digest) is None
        or type(result_generation) is not int
        or result_generation <= 0
    ):
        _refuse("result_pointer_invalid")
    try:
        context, _, _ = result_runtime._context(store, request_id, scope)
        pointer = context["admission"].get("result")
        if pointer is None:
            _refuse("result_unselected")
        if pointer["digest"] != result_digest or int(pointer["generation"]) != result_generation:
            _refuse("result_pointer_mismatch")
        snapshot_validator, answer_validator = question_answer_validators(store, scope)
        record = result_runtime._selected(store, context, answer_validator)
        if record is None:
            _refuse("result_unselected")
        if record["state"] not in ADMITTED_ANSWER_STATES:
            _refuse("result_state_inadmissible", record["state"])
        plan, snapshot = _answer_material(store, context, snapshot_validator)
        policy = store._stored_policy(context["admission"]["policy_digest"])
    except QuestionStoreError as error:
        raise _from_runtime(error) from error
    if snapshot["snapshot_digest"] != record["snapshot_digest"]:
        _refuse("result_binding_invalid")
    source_binding_digest = _source_binding_digest(snapshot.get("provenance"))
    if source_binding_digest is None:
        _refuse("source_binding_missing")
    admission = context["admission"]
    reference = context["intake"].get("parent_context_ref")
    parent = reference["parent"] if reference is not None else None
    anchor = reference["anchor"] if reference is not None else None
    window = snapshot["window"]
    return AdmittedDossierMaterial(
        _TOKEN,
        request_id=request_id,
        request_reference={
            "request_id": request_id,
            "request_digest": admission["request_digest"],
            "thread_anchor_request_id": anchor["request_id"] if anchor is not None else None,
            "parent_request_id": parent["request_id"] if parent is not None else None,
        },
        resolved_scope={
            field: copy.deepcopy(context["request"][field]) for field in RESOLVED_SCOPE_FIELDS
        },
        policy=policy,
        plan=copy.deepcopy(plan),
        evidence_snapshot=copy.deepcopy(snapshot),
        admitted_answer=copy.deepcopy(record),
        result_generation=result_generation,
        bindings={
            "policy_digest": admission["policy_digest"],
            "deployment_digest": admission["deployment_digest"],
            "source_binding_digest": source_binding_digest,
            "snapshot_digest": record["snapshot_digest"],
            "result_digest": result_digest,
        },
        # C02: inclusive UTC window dates, an explicit instant for as_of, and
        # the source cutoff is the last window day the plan admitted evidence
        # for. The snapshot's closed flag travels verbatim inside the record.
        temporal_binding={
            "window_start": window["start"],
            "window_end": window["end"],
            "as_of": snapshot["as_of"],
            "source_cutoff": window["end"],
        },
    )


def export_dossier_body(
    material: AdmittedDossierMaterial,
    *,
    investigation_id: str,
    frame: dict,
    scope: dict,
    review_state: dict,
    predecessor_version: str | None,
    created_at: str,
    prompt_manifest_digest: str,
    publisher_build_digest: str,
    frame_validator,
) -> bytes:
    """The exact dossier bytes for admitted material, and for nothing else."""
    if (
        type(material) is not AdmittedDossierMaterial
        or getattr(material, "_token", None) is not _TOKEN
    ):
        _refuse("material_unadmitted")
    bindings = dict(material.bindings)
    bindings["prompt_manifest_digest"] = prompt_manifest_digest
    bindings["publisher_build_digest"] = publisher_build_digest
    return build_dossier_body(
        investigation_id=investigation_id,
        frame=frame,
        scope=scope,
        temporal_binding=copy.deepcopy(material.temporal_binding),
        request_reference=copy.deepcopy(material.request_reference),
        admitted_answer=copy.deepcopy(material.admitted_answer),
        evidence_snapshot=copy.deepcopy(material.evidence_snapshot),
        bindings=bindings,
        review_state=review_state,
        predecessor_version=predecessor_version,
        created_at=created_at,
        frame_validator=frame_validator,
    )
