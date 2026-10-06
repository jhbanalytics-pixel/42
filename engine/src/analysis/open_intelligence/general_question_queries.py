"""Held query reservations and readback data, without query submission authority."""

from __future__ import annotations

import copy
from datetime import timedelta
from time import monotonic
from types import MappingProxyType

from src.analysis.open_intelligence.general_question_control import (
    QUERY_BINDING_FIELDS,
    QUERY_TEMPLATES,
    QuestionStoreError,
    control_time,
    query_binding_digest,
    query_job_id,
    require_digest,
    utc_time,
    validate_query_receipt,
)
from src.analysis.open_intelligence.general_question_policy import require_question_admission_policy


def _reservation(value):
    return MappingProxyType(
        copy.deepcopy({key: item for key, item in value.items() if key != "receipt"})
    )


def _ordinal(value):
    if type(value) is not int or not 1 <= value <= 8:
        raise QuestionStoreError("query_invalid")
    return str(value)


def _require_no_observed_overage(queries):
    for reservation in queries.values():
        receipt = reservation["receipt"]
        if (
            receipt is not None
            and receipt["query_state"] == "failed"
            and any(
                receipt[field] is not None and receipt[field] > reservation[bound]
                for field, bound in (
                    ("billed_bytes", "maximum_bytes_billed"),
                    ("observed_candidate_count", "candidate_limit"),
                )
            )
        ):
            raise QuestionStoreError("query_budget_exhausted")


class GeneralQuestionQueries:
    def __init__(self, store):
        self.store = store

    def _context(self, request_id, scope):
        context = self.store.read_request(request_id, scope=scope)
        if context is None:
            raise QuestionStoreError("request_unknown")
        stored, control = self.store._control()
        admission = control["requests"].get(request_id)
        if admission is None or any(
            admission[key] != context["admission"][key]
            for key in ("request_digest", "intake_digest", "policy_digest", "deployment_digest")
        ):
            raise QuestionStoreError("control_conflict")
        if "queries" not in admission.get("execution", {}):
            raise QuestionStoreError("query_unavailable")
        context["admission"] = admission
        return context, stored, control

    def _live(self, context, control, now):
        admission = context["admission"]
        if admission.get("result") is not None:
            raise QuestionStoreError("request_terminal")
        if (
            admission["deployment_digest"] != self.store.deployment_digest
            or control["active_deployment_digest"] != self.store.deployment_digest
            or admission["policy_digest"] != self.store.policy["policy_digest"]
        ):
            raise QuestionStoreError("approval_required")
        try:
            require_question_admission_policy(self.store.policy, now=now)
            if (
                not control_time(admission["admitted_at"])
                <= now
                < control_time(admission["deadline_at"])
            ):
                raise ValueError()
        except (ValueError, OverflowError) as error:
            raise QuestionStoreError("request_expired") from error

    def reserve_query(
        self,
        request_id,
        *,
        scope,
        ordinal,
        template_id,
        sql_digest,
        parameters_digest,
        maximum_bytes_billed,
        candidate_limit,
        now,
        cap_version=None,
    ):
        started = monotonic()
        now = utc_time(now)
        key = _ordinal(ordinal)
        if cap_version is not None and (type(cap_version) is not int or cap_version < 1):
            raise QuestionStoreError("query_invalid")
        if type(template_id) is not str or template_id not in QUERY_TEMPLATES:
            raise QuestionStoreError("query_invalid")
        require_digest(sql_digest, "query_invalid")
        require_digest(parameters_digest, "query_invalid")
        for value, bound in ((maximum_bytes_billed, 1000000000), (candidate_limit, 1000)):
            if type(value) is not int or not 0 <= value <= bound:
                raise QuestionStoreError("query_invalid")
        proposal = None
        for attempt in range(6):
            context, stored, control = self._context(request_id, scope)
            admission = context["admission"]
            queries = admission["execution"]["queries"]
            binding = {
                "contract_version": "general_question_query_reservation_v1"
                if cap_version is None
                else "general_question_query_reservation_v2",
                "request_id": request_id,
                "run_id": context["request"]["run_id"],
                **{
                    field: admission[field]
                    for field in (
                        "request_digest",
                        "intake_digest",
                        "policy_digest",
                        "deployment_digest",
                    )
                },
                "ordinal": ordinal,
                "template_id": template_id,
                "sql_digest": sql_digest,
                "parameters_digest": parameters_digest,
                "maximum_bytes_billed": maximum_bytes_billed,
                **({"cap_version": cap_version} if cap_version is not None else {}),
                "candidate_limit": candidate_limit,
            }
            existing = queries.get(key)
            if existing is not None:
                fields = QUERY_BINDING_FIELDS | (
                    {"cap_version"} if "cap_version" in existing else set()
                )
                if {field: existing[field] for field in fields} != binding:
                    raise QuestionStoreError("query_conflict")
                if proposal is not None:
                    _require_no_observed_overage(queries)
                    self._live(context, control, now + timedelta(seconds=monotonic() - started))
                return _reservation(existing)
            if attempt == 5:
                break
            current = now + timedelta(seconds=monotonic() - started)
            self._live(context, control, current)
            _require_no_observed_overage(queries)
            if (
                len(queries) >= 8
                or sum(row["maximum_bytes_billed"] for row in queries.values())
                + maximum_bytes_billed
                > 1000000000
                or sum(row["candidate_limit"] for row in queries.values()) + candidate_limit > 1000
            ):
                raise QuestionStoreError("query_budget_exhausted")
            if proposal is None:
                digest = query_binding_digest(binding)
                proposal = {
                    **binding,
                    "query_digest": digest,
                    "job_id": query_job_id(request_id, ordinal, digest),
                    "reserved_at": current.isoformat(timespec="microseconds").replace(
                        "+00:00", "Z"
                    ),
                    "receipt": None,
                }
            queries[key] = copy.deepcopy(proposal)
            self._live(context, control, now + timedelta(seconds=monotonic() - started))
            self.store._objects.compare_control(stored.generation, control)
        raise QuestionStoreError("control_conflict")

    def read_query(self, request_id, *, scope, ordinal):
        key = _ordinal(ordinal)
        context, _, _ = self._context(request_id, scope)
        value = context["admission"]["execution"]["queries"].get(key)
        if value is None:
            return None
        return {"reservation": _reservation(value), "receipt": copy.deepcopy(value["receipt"])}

    def record_query_receipt(self, request_id, *, scope, ordinal, receipt):
        key = _ordinal(ordinal)
        for attempt in range(6):
            context, stored, control = self._context(request_id, scope)
            value = context["admission"]["execution"]["queries"].get(key)
            if value is None:
                raise QuestionStoreError("query_unknown")
            incoming = validate_query_receipt(receipt, value)
            prior = value["receipt"]
            if prior == incoming:
                return copy.deepcopy(prior)
            if (
                prior is not None
                and prior["query_state"] != "running"
                and (
                    prior["query_state"] != incoming["query_state"]
                    or any(
                        prior[field] is not None and prior[field] != incoming[field]
                        for field in ("billed_bytes", "observed_candidate_count", "result_digest")
                    )
                )
            ):
                raise QuestionStoreError("query_receipt_conflict")
            if attempt == 5:
                break
            value["receipt"] = incoming
            self.store._objects.compare_control(stored.generation, control)
        raise QuestionStoreError("control_conflict")
