"""Bounded parent reads and generation-pinned continuity context."""

import copy
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, suppress
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic

from src.analysis.open_intelligence.general_question_control import QuestionStoreError

_ATTEMPT = ContextVar("question_parent_read_attempt", default=None)
_READ_TICKET = ContextVar("question_parent_prefetch_ticket", default=None)
_DIAGNOSTIC = ContextVar("question_admission_diagnostic", default=None)
_ADMISSION_STAGES = frozenset(
    {
        "host_load",
        "admission",
        "authority",
        "child_lookup",
        "policy_freshness",
        "intake_context",
        "parent_intake",
        "context_prefetch",
        "scope_validation",
        "result_prefetch",
        "parent_validation",
        "anchor_validation",
        "capsule_write",
        "reservation",
    }
)
_ADMISSION_ERRORS = frozenset(
    {
        "admission_failed",
        "parent_unavailable",
        "parent_context_invalid",
        "parent_context_conflict",
        "parent_read_budget_exhausted",
        "parent_context_deadline",
        "parent_source_unavailable",
        "parent_alias_invalid",
        "parent_reference_invalid",
        "scope_invalid",
        "control_invalid",
        "protected_context_registry_invalid",
        "snapshot_cap_version_unknown",
        "request_invalid",
        "run_id_conflict",
        "approval_required",
        "storage_unavailable",
        "object_version_unavailable",
        "budget_exhausted",
        "request_expired",
        "control_conflict",
    }
)
PARENT_READ_LIMIT = 40
RESPONSE_VERSIONS_VERSION = "general_question_response_versions_v1"
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def resolve_response_versions(*, policy_digest, planning, answering, parent_aliases=None):
    """Resolve recorded call bindings to exact prompt, schema and adapter identities.

    A binding is a recorded call. Its system_instruction_digest is the SHA256
    of the exact prompt bytes and its response_schema_digest the canonical
    digest of the exact response schema. Both stages bind both digests
    exactly against the adapter catalogue: the planning schema is the
    parent-less schema, or the schema bound to the visible parent aliases the
    caller supplies, and the answering adapter resolves from its schema and
    prompt digest pair. Each binding must carry the request's policy digest.
    A model alias or a file path resolves nothing, so a binding that offers
    only those refuses, a crossed pair refuses, and an old response resolves
    to its old adapter while a current response resolves to the current one.
    """
    from src.analysis.open_intelligence.general_question_answer import answer_adapter_id
    from src.analysis.open_intelligence.general_question_planning import planning_adapter_id

    def digest(value):
        if type(value) is not str or _HEX64.fullmatch(value) is None:
            raise ValueError("response_version_unresolved")
        return value

    def stage(binding, resolve):
        if type(binding) is not dict or binding.get("policy_digest") != policy_digest:
            raise ValueError("response_version_unresolved")
        instruction = digest(binding.get("system_instruction_digest"))
        schema = digest(binding.get("response_schema_digest"))
        try:
            adapter = resolve(instruction, schema)
        except ValueError as error:
            raise ValueError("response_version_unresolved") from error
        return {
            "adapter": adapter,
            "system_instruction_digest": instruction,
            "response_schema_digest": schema,
            "model": binding.get("model"),
        }

    return {
        "contract_version": RESPONSE_VERSIONS_VERSION,
        "policy_digest": digest(policy_digest),
        "planning": stage(
            planning,
            lambda instruction, schema: planning_adapter_id(
                instruction, schema, parent_aliases=parent_aliases
            ),
        ),
        "answering": None
        if answering is None
        else stage(answering, lambda instruction, schema: answer_adapter_id(schema, instruction)),
    }


def parent_planning_aliases(store, request, intake):
    """The visible parent aliases the planning schema was bound to; None without a parent."""
    from src.analysis.open_intelligence.general_question_parent_capsule import planner_parent_view

    capsule = read_parent_context(store, request, intake)
    if capsule is None:
        return None
    return [row["alias"] for row in planner_parent_view(capsule)["source_previews"]]


def recorded_response_versions(context, *, parent_aliases=None):
    """Resolve the versions of every answered call on a request context.

    Returns None when no call has a recorded response. A recorded response
    whose binding does not resolve raises, so a continuity read or an
    observation never accepts a response it cannot reconstruct exactly. A
    parent-bound request passes the visible aliases its planning schema
    carried; without them the planning schema binds as parent-less.
    """
    calls = context["admission"]["execution"]["calls"]
    recorded = {
        stage: call
        for stage, call in calls.items()
        if type(call) is dict and call.get("response") is not None
    }
    if not recorded:
        return None
    if "planning" not in recorded:
        raise ValueError("response_version_unresolved")
    return resolve_response_versions(
        policy_digest=context["request"]["policy_digest"],
        planning=recorded["planning"],
        answering=recorded.get("answering"),
        parent_aliases=parent_aliases,
    )


@contextmanager
def admission_stage(stage, *, request_id=None, diagnostics=None):
    token = _DIAGNOSTIC.set((request_id, diagnostics)) if diagnostics is not None else None
    current = _DIAGNOSTIC.get()
    start = monotonic()

    def emit(state, error=None):
        if (
            current is None
            or not callable(current[1])
            or type(stage) is not str
            or stage not in _ADMISSION_STAGES
        ):
            return
        from src.analysis.open_intelligence.general_question_control import require_request_id

        try:
            require_request_id(current[0])
        except (ValueError, QuestionStoreError):
            return
        attempt = _ATTEMPT.get()
        counts = dict.fromkeys(("read_attempts", "metadata_attempts", "body_attempts"))
        if attempt is not None:
            with attempt.lock:
                counts = {key: getattr(attempt, key) for key in counts}
            if (
                not all(type(value) is int and 0 <= value <= 40 for value in counts.values())
                or counts["read_attempts"] != counts["metadata_attempts"]
                or counts["body_attempts"] > counts["metadata_attempts"]
            ):
                return
        code = getattr(error, "code", None) if error is not None else None
        if error is not None and (type(code) is not str or code not in _ADMISSION_ERRORS):
            code = "admission_failed"
        event = {
            "contract_version": "general_question_admission_stage_v1",
            "request_id": current[0],
            "stage": stage,
            "state": state,
            "error_code": code,
            "elapsed_ms": min(60000, max(0, int((monotonic() - start) * 1000))),
            **counts,
        }
        with suppress(Exception):
            current[1](event)

    emit("started")
    try:
        yield
    except BaseException as error:
        emit("failed", error)
        raise
    else:
        emit("succeeded")
    finally:
        if token is not None:
            _DIAGNOSTIC.reset(token)


@dataclass
class ParentReadAttempt:
    objects: object
    deadline: float
    read_attempts: int = 0
    metadata_attempts: int = 0
    body_attempts: int = 0
    cache: dict = field(default_factory=dict)
    generations: dict = field(default_factory=dict)
    created_keys: set = field(default_factory=set)
    frozen_ledger: object = None
    last_ledger: object = None
    lock: object = field(default_factory=RLock)
    failed: bool = False
    failure_code: str = "parent_context_invalid"

    def remaining(self, maximum=5):
        with self.lock:
            if self.failed:
                raise QuestionStoreError(self.failure_code)
            remaining = self.deadline - monotonic()
            if remaining <= 0:
                raise QuestionStoreError("parent_context_deadline")
            return min(maximum, remaining)

    def before_read(self):
        with self.lock:
            timeout = self.remaining()
            if _READ_TICKET.get() is self:
                _READ_TICKET.set(None)
                return timeout
            if self.read_attempts >= PARENT_READ_LIMIT:
                raise QuestionStoreError("parent_read_budget_exhausted")
            self.read_attempts += 1
            self.metadata_attempts += 1
            return timeout

    def before_body(self):
        with self.lock:
            self.remaining()
            self.body_attempts += 1

    def lookup(self, key, generation):
        with self.lock:
            self.remaining()
            if key.startswith("allowances/"):
                item = self.frozen_ledger
                return (
                    copy.deepcopy(item)
                    if item is not None and generation in (None, item.generation)
                    else None
                )
            selected = generation if generation is not None else self.generations.get(key)
            return copy.deepcopy(self.cache.get((key, selected)))

    def record(self, key, value):
        with self.lock:
            if key.startswith("allowances/"):
                self.last_ledger = copy.deepcopy(value)
            else:
                self.generations.setdefault(key, value.generation)
                self.cache[(key, value.generation)] = copy.deepcopy(value)

    def prefetch(self, manifest):
        pending = []
        for key, generation in dict.fromkeys(manifest):
            if type(generation) is not int or generation <= 0 or not key.startswith("requests/"):
                raise QuestionStoreError("parent_context_invalid")
            if self.lookup(key, generation) is None:
                pending.append((key, generation))
        if not pending:
            return

        def read(key, generation):
            self.remaining()
            token = _READ_TICKET.set(self)
            try:
                result = self.objects.read(key, generation=generation)
                if result is None:
                    raise QuestionStoreError("parent_unavailable")
                return result
            except BaseException as error:
                with self.lock:
                    if not self.failed:
                        self.failure_code = (
                            error.code
                            if isinstance(error, QuestionStoreError)
                            else "parent_context_invalid"
                        )
                    self.failed = True
                raise
            finally:
                _READ_TICKET.reset(token)

        futures = []
        pool = ThreadPoolExecutor(max_workers=4)
        try:
            for key, generation in pending:
                self.before_read()
                futures.append(pool.submit(copy_context().run, read, key, generation))
            for future in as_completed(futures):
                future.result()
            self.remaining()
        except BaseException as error:
            with self.lock:
                if not self.failed:
                    self.failure_code = (
                        error.code
                        if isinstance(error, QuestionStoreError)
                        else "parent_context_invalid"
                    )
                self.failed = True
            for future in futures:
                future.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    @contextmanager
    def freeze_ledger(self, value):
        with self.lock:
            prior = self.frozen_ledger
            self.frozen_ledger = copy.deepcopy(value)
        try:
            yield
        finally:
            with self.lock:
                self.frozen_ledger = prior


def active_parent_attempt(objects):
    attempt = _ATTEMPT.get()
    return attempt if attempt is not None and attempt.objects is objects else None


@contextmanager
def parent_read_attempt(store, *, deadline):
    current = active_parent_attempt(store._objects)
    if current is not None:
        yield current
        return
    attempt = ParentReadAttempt(store._objects, deadline)
    token = _ATTEMPT.set(attempt)
    try:
        yield attempt
    finally:
        _ATTEMPT.reset(token)


def require_parent_pointers(intake, control):
    reference = intake.get("parent_context_ref")
    if reference is None:
        return
    for ref in (reference["parent"], reference["anchor"]):
        if ref is None:
            continue
        current = control["requests"].get(ref["request_id"])
        if (
            current is None
            or any(
                current.get(key) != ref[key]
                for key in (
                    "request_digest",
                    "intake_digest",
                    "request_generation",
                    "intake_generation",
                )
            )
            or current.get("result")
            != {"digest": ref["result_digest"], "generation": ref["result_generation"]}
        ):
            raise QuestionStoreError("parent_context_conflict")


def read_parent_context(store, request, intake):
    from src.analysis.open_intelligence.general_question_parent_capsule import validate_capsule

    reference = intake.get("parent_context_ref")
    if reference is None:
        return None
    stored = store._objects.read(
        f"requests/{request['request_id']}/parent_context.json",
        generation=int(reference["context_generation"]),
    )
    if stored is None:
        raise QuestionStoreError("parent_context_invalid")
    return validate_capsule(stored.value, request=request, reference=reference)


def _parent_bundle(store, identifier, scope):
    from src.analysis.open_intelligence.general_question_execution import question_answer_validators
    from src.analysis.open_intelligence.general_question_result import _context, _selected

    try:
        context, _, _ = _context(store, identifier, scope)
        aliases = parent_planning_aliases(store, context["request"], context["intake"])
        _, validator = question_answer_validators(store, scope)
        result = _selected(store, context, validator)
        if (
            result is None
            or result["state"] not in {"complete", "partial"}
            or result["response"]["intelligence"]["status"] not in {"complete", "partial"}
        ):
            raise ValueError()
        reference = {
            "request_id": identifier,
            **{
                key: context["admission"][key]
                for key in (
                    "request_digest",
                    "intake_digest",
                    "request_generation",
                    "intake_generation",
                )
            },
            "result_digest": context["admission"]["result"]["digest"],
            "result_generation": context["admission"]["result"]["generation"],
        }
        artifacts = {}
        for name in ("plan", "snapshot"):
            item = store._objects.read(f"requests/{identifier}/{name}.json")
            if item is None or item.value[name + "_digest"] != result[name + "_digest"]:
                raise ValueError()
            reference[name + "_digest"] = result[name + "_digest"]
            reference[name + "_generation"] = str(item.generation)
            artifacts[name] = item.value
        return {
            "reference": reference,
            "request": context["request"],
            "intake": context["intake"],
            "result": result,
            "response_versions": recorded_response_versions(context, parent_aliases=aliases),
            **artifacts,
        }
    except QuestionStoreError as error:
        if error.code in {"parent_read_budget_exhausted", "parent_context_deadline"}:
            raise
        raise QuestionStoreError("parent_unavailable") from error
    except (ValueError, TypeError, KeyError) as error:
        raise QuestionStoreError("parent_unavailable") from error


def _prefetch_parent_contexts(store, control, identifiers, scope):
    attempt = active_parent_attempt(store._objects)
    manifest = []
    try:
        for identifier in identifiers:
            row = control["requests"].get(identifier)
            if row is None or row["client_scope_id"] != scope.get("client_scope_id"):
                raise QuestionStoreError("parent_unavailable")
            for name in ("request", "intake"):
                manifest.append(
                    (f"requests/{identifier}/{name}.json", int(row[name + "_generation"]))
                )
        with admission_stage("context_prefetch"):
            attempt.prefetch(manifest)
        with admission_stage("scope_validation"):
            contexts = [store.read_request(identifier, scope=scope) for identifier in identifiers]
        manifest = []
        for identifier, context in zip(identifiers, contexts, strict=True):
            result = context["admission"].get("result")
            if result is None:
                raise QuestionStoreError("parent_unavailable")
            manifest.append(
                (
                    f"requests/{identifier}/results/{result['digest']}.json",
                    int(result["generation"]),
                )
            )
            reference = context["intake"].get("parent_context_ref")
            if reference is not None:
                manifest.append(
                    (
                        f"requests/{identifier}/parent_context.json",
                        int(reference["context_generation"]),
                    )
                )
        with admission_stage("result_prefetch"):
            attempt.prefetch(manifest)
    except QuestionStoreError as error:
        if error.code in {"parent_read_budget_exhausted", "parent_context_deadline"}:
            raise
        raise QuestionStoreError("parent_unavailable") from error


def prepare_parent_intake(
    store, request, *, scope, parent_id, anchor_id, selected_market, retained_intake=None
):
    from src.analysis.open_intelligence.brain_contract import canonical_digest
    from src.analysis.open_intelligence.general_question_parent_capsule import (
        REF_VERSION,
        build_capsule,
        validate_capsule,
    )
    from src.analysis.open_intelligence.general_question_policy import build_intake_context

    attempt = active_parent_attempt(store._objects)
    if attempt is None or attempt.last_ledger is None:
        raise QuestionStoreError("parent_context_invalid")
    frozen = attempt.last_ledger
    existing = frozen.value["requests"].get(request["request_id"])
    if existing is not None:
        with attempt.freeze_ledger(frozen):
            current = store.read_request(request["request_id"], scope=scope)
        if current["request"] != request:
            raise QuestionStoreError("run_id_conflict")
        intake = current["intake"]
        capsule = read_parent_context(store, request, intake)
        if (
            capsule is None
            or capsule["parent"]["request_id"] != parent_id
            or (capsule["anchor"]["request_id"] if capsule["anchor"] else None) != anchor_id
            or intake["selected_market"] != selected_market
        ):
            raise QuestionStoreError("parent_context_conflict")
        return intake
    request_key = f"requests/{request['request_id']}/request.json"
    store._objects.create(request_key, request)
    key = f"requests/{request['request_id']}/parent_context.json"
    capsule_object = None if request_key in attempt.created_keys else store._objects.read(key)
    if capsule_object is not None:
        capsule = validate_capsule(capsule_object.value, request=request)
        if (
            capsule["parent"]["request_id"] != parent_id
            or (capsule["anchor"]["request_id"] if capsule["anchor"] else None) != anchor_id
        ):
            raise QuestionStoreError("parent_context_conflict")
    with attempt.freeze_ledger(frozen):
        identifiers = list(
            dict.fromkeys(identifier for identifier in (parent_id, anchor_id) if identifier)
        )
        _prefetch_parent_contexts(store, frozen.value, identifiers, scope)
        with admission_stage("parent_validation"):
            parent = _parent_bundle(store, parent_id, scope)
        with admission_stage("anchor_validation"):
            anchor = (
                parent
                if anchor_id == parent_id
                else _parent_bundle(store, anchor_id, scope)
                if anchor_id
                else None
            )
    expected_capsule = build_capsule(request, parent, anchor)
    if capsule_object is not None:
        if capsule != expected_capsule:
            raise QuestionStoreError("parent_context_conflict")
    else:
        capsule = expected_capsule
        with admission_stage("capsule_write"):
            capsule_object = store._objects.create(key, capsule)
    reference = {
        "contract_version": REF_VERSION,
        "parent": capsule["parent"],
        "anchor": capsule["anchor"],
        "context_digest": canonical_digest(capsule),
        "context_generation": str(capsule_object.generation),
    }
    if retained_intake is not None:
        from src.analysis.open_intelligence.general_question_policy import validate_intake_context

        intake = validate_intake_context(retained_intake, request=request)
        if (
            intake.get("parent_context_ref") != reference
            or intake["selected_market"] != selected_market
        ):
            raise QuestionStoreError("parent_context_conflict")
        return intake
    return build_intake_context(
        request, selected_market=selected_market, parent_context_ref=reference, source_window=True
    )
