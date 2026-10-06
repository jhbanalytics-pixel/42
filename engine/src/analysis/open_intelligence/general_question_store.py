"""Generation-bound storage and one shared allowance for question admission."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from time import monotonic

from google.api_core.exceptions import Forbidden, NotFound, PreconditionFailed, Unauthorized

from src.analysis.open_intelligence.general_question_control import (
    ALLOWANCE_ID,
    MAX_REQUESTS,
    REQUEST_MICROUSD,
    QuestionStoreError,
    build_initial_question_control,
    control_time,
    require_digest,
    require_request_id,
    utc_time,
    validate_question_control,
)
from src.analysis.open_intelligence.general_question_policy import (
    require_question_admission_policy,
    validate_intake_context,
    validate_question_policy,
)
from src.analysis.open_intelligence.general_question_request import validate_question_request

_BUCKET = "listening-post-staging-cache"
_PREFIX = "open-intelligence/v2/staging/general-questions/"
_LEDGER = f"allowances/{ALLOWANCE_ID}/ledger.json"
_MAX_BYTES = 4 * 1024 * 1024
_POLICY_CACHE_LIMIT = 128
_FIXED_REQUEST_FILES = {
    "request.json",
    "intake.json",
    "plan.json",
    "snapshot.json",
    "parent_context.json",
    "calls/planning.json",
    "calls/answering.json",
    "usage/planning.json",
    "usage/answering.json",
}


def _json_value(value, depth=0):
    if depth > 30:
        raise QuestionStoreError("object_invalid")
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise QuestionStoreError("object_invalid")
        return {key: _json_value(item, depth + 1) for key, item in value.items()}
    if type(value) is list:
        return [_json_value(item, depth + 1) for item in value]
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise QuestionStoreError("object_invalid")


def _pack(value: dict) -> bytes:
    if type(value) is not dict:
        raise QuestionStoreError("object_invalid")
    try:
        raw = json.dumps(
            _json_value(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise QuestionStoreError("object_invalid") from exc
    if len(raw) > _MAX_BYTES:
        raise QuestionStoreError("object_too_large")
    return raw


def _unpack(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise QuestionStoreError("object_invalid")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise QuestionStoreError("object_invalid") from exc
    if _pack(value) != raw:
        raise QuestionStoreError("object_invalid")
    return value


@dataclass(frozen=True, slots=True)
class _StoredObject:
    value: dict
    generation: int
    raw: bytes


class _QuestionObjects:
    def __init__(self, bucket):
        if getattr(bucket, "name", None) != _BUCKET:
            raise QuestionStoreError("storage_target_invalid")
        self.bucket = bucket
        self._generation_bytes = {}
        self._generation_bytes_size = 0
        self._generation_bytes_lock = Lock()
        self.policy_resolver = None

    @staticmethod
    def _name(key: str, *, write=False) -> str:
        if key == _LEDGER and not write:
            return _PREFIX + key
        parts = key.split("/") if isinstance(key, str) else []
        if len(parts) >= 3 and parts[0] == "requests":
            require_request_id(parts[1])
            suffix = "/".join(parts[2:])
            if suffix in _FIXED_REQUEST_FILES:
                return _PREFIX + key
            if len(parts) in (5, 6) and parts[2] == "queries" and re.fullmatch(r"[1-8]", parts[3]):
                if len(parts) == 5 and parts[4] == "execution.json":
                    return _PREFIX + key
                if (
                    len(parts) == 6
                    and parts[4] == "observations"
                    and re.fullmatch(r"[0-9a-f]{64}\.json", parts[5])
                ):
                    return _PREFIX + key
            if (
                len(parts) == 4
                and parts[2] == "results"
                and re.fullmatch(r"[0-9a-f]{64}\.json", parts[3])
            ):
                return _PREFIX + key
        if not write and len(parts) == 3 and parts[0] in ("policies", "deployments"):
            require_digest(parts[1], "storage_target_invalid")
            if parts[2] == ("policy.json" if parts[0] == "policies" else "binding.json"):
                return _PREFIX + key
        raise QuestionStoreError("storage_target_invalid")

    @staticmethod
    def _content_address(key: str, raw: bytes) -> None:
        parts = key.split("/")
        if (
            len(parts) == 6
            and parts[0] == "requests"
            and parts[2] == "queries"
            and parts[4] == "observations"
            and parts[5] != hashlib.sha256(raw).hexdigest() + ".json"
        ):
            raise QuestionStoreError("object_digest_mismatch")
        if (
            len(parts) == 4
            and parts[0] == "requests"
            and parts[2] == "results"
            and parts[3] != hashlib.sha256(raw).hexdigest() + ".json"
        ):
            raise QuestionStoreError("object_digest_mismatch")

    def read(self, key: str, *, generation: int | None = None) -> _StoredObject | None:
        from src.analysis.open_intelligence.general_question_parent_context import (
            active_parent_attempt,
        )

        name = self._name(key)
        if generation is not None and (type(generation) is not int or generation <= 0):
            raise QuestionStoreError("object_invalid")
        attempt = active_parent_attempt(self)
        cached = attempt.lookup(key, generation) if attempt is not None else None
        if cached is not None:
            return cached
        for _ in range(3):
            try:
                timeout = attempt.before_read() if attempt is not None else 5
                blob = self.bucket.get_blob(
                    name, generation=generation, timeout=timeout, retry=None
                )
                if blob is None:
                    return None
                actual = blob.generation
                if (
                    type(actual) is not int
                    or actual <= 0
                    or (generation is not None and actual != generation)
                ):
                    raise QuestionStoreError("object_invalid")
                if type(blob.size) is not int or not 0 <= blob.size <= _MAX_BYTES:
                    raise QuestionStoreError("object_too_large")
                cache_key = (
                    (self.bucket.name, name, generation)
                    if generation is not None
                    and key != _LEDGER
                    and not (
                        key.startswith("requests/") and key.split("/")[2] in ("usage", "calls")
                    )
                    else None
                )
                with self._generation_bytes_lock:
                    raw = self._generation_bytes.get(cache_key) if cache_key is not None else None
                if raw is None:
                    if attempt is not None:
                        attempt.before_body()
                    raw = blob.download_as_bytes(
                        if_generation_match=actual,
                        raw_download=True,
                        timeout=attempt.remaining() if attempt is not None else 5,
                        retry=None,
                    )
                if not isinstance(raw, bytes) or len(raw) > _MAX_BYTES:
                    raise QuestionStoreError("object_too_large")
                if len(raw) != blob.size:
                    raise QuestionStoreError("object_invalid")
                self._content_address(key, raw)
                value = _unpack(raw)
                if cache_key is not None:
                    with self._generation_bytes_lock:
                        if (
                            cache_key not in self._generation_bytes
                            and self._generation_bytes_size + len(raw) <= _MAX_BYTES
                        ):
                            self._generation_bytes[cache_key] = raw
                            self._generation_bytes_size += len(raw)
                result = _StoredObject(value, actual, raw)
                if attempt is not None:
                    attempt.record(key, result)
                return result
            except (NotFound, PreconditionFailed) as exc:
                if generation is not None:
                    raise QuestionStoreError("object_version_unavailable") from exc
            except QuestionStoreError:
                raise
            except Exception as exc:
                raise QuestionStoreError("storage_unavailable") from exc
        raise QuestionStoreError("storage_unavailable")

    def create(self, key: str, value: dict) -> _StoredObject:
        from src.analysis.open_intelligence.general_question_parent_context import (
            active_parent_attempt,
        )

        name = self._name(key, write=True)
        raw = _pack(value)
        self._content_address(key, raw)
        blob = self.bucket.blob(name)
        written_generation = None
        attempt = active_parent_attempt(self)
        try:
            blob.upload_from_string(
                raw,
                content_type="application/json",
                if_generation_match=0,
                timeout=attempt.remaining() if attempt is not None else 5,
                retry=None,
            )
            written_generation = blob.generation
            if attempt is not None and type(written_generation) is int and written_generation > 0:
                with attempt.lock:
                    attempt.created_keys.add(key)
        except Exception:
            # An uncertain response permits readback, never a blind second put.
            pass
        result = self.read(key, generation=written_generation)
        if result is None:
            raise QuestionStoreError("storage_unavailable")
        if result.raw != raw:
            raise QuestionStoreError("run_id_conflict")
        return result

    def compare_control(self, generation: int, value: dict) -> bool:
        from src.analysis.open_intelligence.general_question_parent_context import (
            PARENT_READ_LIMIT,
            active_parent_attempt,
        )

        if type(generation) is not int or generation <= 0:
            raise QuestionStoreError("control_unavailable")
        raw = _pack(validate_question_control(value, policy_resolver=self.policy_resolver))
        attempt = active_parent_attempt(self)
        if attempt is not None and attempt.read_attempts >= PARENT_READ_LIMIT:
            raise QuestionStoreError("parent_read_budget_exhausted")
        try:
            self.bucket.blob(_PREFIX + _LEDGER).upload_from_string(
                raw,
                content_type="application/json",
                if_generation_match=generation,
                timeout=attempt.remaining() if attempt is not None else 5,
                retry=None,
            )
            return True
        except (PermissionError, Forbidden, Unauthorized) as exc:
            raise QuestionStoreError("storage_access_denied") from exc
        except Exception:
            # The admission loop re-reads and checks the same immutable input.
            return False


class GeneralQuestionStore:
    """Reserve admission; this object alone never authorizes model execution."""

    def __init__(self, bucket, *, policy: dict, deployment_digest: str):
        self._objects = _QuestionObjects(bucket)
        self.policy = validate_question_policy(policy)
        self.deployment_digest = require_digest(deployment_digest, "approval_required")
        self._policy_cache = OrderedDict()
        self._policy_cache_lock = Lock()
        self._objects.policy_resolver = self._stored_policy

    def _stored_policy(self, policy_digest):
        require_digest(policy_digest)
        with self._policy_cache_lock:
            cached = self._policy_cache.get(policy_digest)
            if cached is not None:
                self._policy_cache.move_to_end(policy_digest)
                return copy.deepcopy(cached)
            stored = self._objects.read(f"policies/{policy_digest}/policy.json")
            if stored is None:
                raise QuestionStoreError("control_invalid")
            try:
                policy = validate_question_policy(stored.value)
            except ValueError as exc:
                raise QuestionStoreError("control_invalid") from exc
            if policy["policy_digest"] != policy_digest:
                raise QuestionStoreError("control_invalid")
            self._policy_cache[policy_digest] = copy.deepcopy(policy)
            if len(self._policy_cache) > _POLICY_CACHE_LIMIT:
                self._policy_cache.popitem(last=False)
            return copy.deepcopy(policy)

    def _control(self):
        stored = self._objects.read(_LEDGER)
        if stored is None:
            raise QuestionStoreError("control_unavailable")
        return stored, validate_question_control(stored.value, policy_resolver=self._stored_policy)

    def publish_result(
        self,
        request_id,
        *,
        scope,
        response,
        state,
        now,
        plan_digest=None,
        snapshot_digest=None,
        answer_validator=None,
    ):
        from src.analysis.open_intelligence.general_question_result import publish_result

        try:
            return publish_result(
                self,
                request_id,
                scope=scope,
                response=response,
                state=state,
                now=now,
                plan_digest=plan_digest,
                snapshot_digest=snapshot_digest,
                answer_validator=answer_validator,
            )
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RecursionError,
            OverflowError,
        ) as error:
            raise QuestionStoreError("result_invalid") from error

    def status(self, request_id, *, scope, now, answer_validator=None):
        from src.analysis.open_intelligence.general_question_result import question_status

        try:
            return question_status(
                self, request_id, scope=scope, now=now, answer_validator=answer_validator
            )
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RecursionError,
            OverflowError,
        ) as error:
            raise QuestionStoreError("result_invalid") from error

    def cancel(self, request_id, *, scope, now, answer_validator=None):
        """Publish the durable cancellation marker; the reservation and any started call remain."""
        from src.analysis.open_intelligence.general_question_planning import (
            validate_stored_question_plan,
        )
        from src.analysis.open_intelligence.general_question_result import (
            observed_result_usage,
            unavailable_response,
        )

        now = utc_time(now)
        context = self.read_request(request_id, scope=scope)
        if context is None:
            raise QuestionStoreError("request_unknown")
        if context["admission"].get("result") is None:
            usage = observed_result_usage(self, request_id=request_id, scope=scope)
            plan_digest = window = markets = None
            stored_plan = self._objects.read(f"requests/{request_id}/plan.json")
            if stored_plan is not None:
                try:
                    plan = validate_stored_question_plan(
                        stored_plan.value, request=context["request"], intake=context["intake"]
                    )
                except ValueError as error:
                    raise QuestionStoreError("result_invalid") from error
                plan_digest, window, markets = plan["plan_digest"], plan["window"], plan["markets"]
            response = unavailable_response(
                context["request"],
                usage,
                reason="request_cancelled",
                window=window,
                markets=markets,
            )
            try:
                self.publish_result(
                    request_id,
                    scope=scope,
                    response=response,
                    state="held" if usage["status"] == "unresolved" else "unavailable",
                    now=now,
                    plan_digest=plan_digest,
                )
            except QuestionStoreError as error:
                # A record published first by a worker stays; the reader below reports it.
                if error.code != "result_conflict":
                    raise
        return self.status(request_id, scope=scope, now=now, answer_validator=answer_validator)

    def read_request(self, request_id: str, *, scope: dict) -> dict | None:
        require_request_id(request_id)
        _, control = self._control()
        admission = control["requests"].get(request_id)
        if admission is None:
            return None
        if not isinstance(scope, dict) or admission["client_scope_id"] != scope.get(
            "client_scope_id"
        ):
            raise QuestionStoreError("scope_invalid")
        from src.analysis.open_intelligence.general_question_parent_context import (
            active_parent_attempt,
        )

        attempt = active_parent_attempt(self._objects)
        if attempt is not None:
            attempt.prefetch(
                [
                    (f"requests/{request_id}/request.json", int(admission["request_generation"])),
                    (f"requests/{request_id}/intake.json", int(admission["intake_generation"])),
                ]
            )
        request = self._objects.read(
            f"requests/{request_id}/request.json", generation=int(admission["request_generation"])
        )
        intake = self._objects.read(
            f"requests/{request_id}/intake.json", generation=int(admission["intake_generation"])
        )
        if request is None or intake is None:
            raise QuestionStoreError("storage_unavailable")
        try:
            validated = validate_question_request(
                request.value, scope=scope, policy_digest=admission["policy_digest"]
            )
            context = validate_intake_context(intake.value, request=validated)
        except ValueError as exc:
            raise QuestionStoreError("scope_invalid") from exc
        if (
            validated["request_digest"] != admission["request_digest"]
            or context["intake_digest"] != admission["intake_digest"]
        ):
            raise QuestionStoreError("control_invalid")
        return {"request": validated, "intake": context, "admission": copy.deepcopy(admission)}

    def admit(self, request: dict, intake: dict, *, scope: dict, now: datetime) -> dict:
        started = monotonic()
        try:
            request = validate_question_request(
                request, scope=scope, policy_digest=self.policy["policy_digest"]
            )
            intake = validate_intake_context(intake, request=request)
        except ValueError as exc:
            raise QuestionStoreError("request_invalid") from exc
        now = utc_time(now)
        request_id = request["request_id"]

        def require_live_admission():
            try:
                current = now + timedelta(seconds=monotonic() - started)
                require_question_admission_policy(self.policy, now=current)
                admitted = control_time(request["as_of"])
                deadline = admitted + timedelta(seconds=self.policy["limits"]["deadline_seconds"])
                if admitted > current or current >= deadline:
                    raise ValueError()
                return deadline
            except (ValueError, OverflowError) as exc:
                raise QuestionStoreError("request_expired") from exc

        proposed = None
        for _ in range(5):
            stored, control = self._control()
            existing = control["requests"].get(request_id)
            if existing is not None:
                if (
                    existing["request_digest"] != request["request_digest"]
                    or existing["intake_digest"] != intake["intake_digest"]
                ):
                    raise QuestionStoreError("run_id_conflict")
                from src.analysis.open_intelligence.general_question_parent_context import (
                    active_parent_attempt,
                )

                attempt = active_parent_attempt(self._objects)
                if attempt is not None:
                    with attempt.freeze_ledger(stored):
                        self.read_request(request_id, scope=scope)
                else:
                    self.read_request(request_id, scope=scope)
                if proposed is not None:
                    require_live_admission()
                return copy.deepcopy(existing)
            if (
                control["active_deployment_digest"] != self.deployment_digest
                or control["bindings"].get(self.deployment_digest) != self.policy["policy_digest"]
            ):
                raise QuestionStoreError("approval_required")
            from src.analysis.open_intelligence.general_question_parent_context import (
                require_parent_pointers,
            )

            require_parent_pointers(intake, control)
            if len(control["requests"]) >= MAX_REQUESTS:
                raise QuestionStoreError("budget_exhausted")
            deadline = require_live_admission()
            if proposed is None:
                request_object = self._objects.create(
                    f"requests/{request_id}/request.json", request
                )
                require_live_admission()
                intake_object = self._objects.create(f"requests/{request_id}/intake.json", intake)
                proposed = {
                    "request_digest": request["request_digest"],
                    "intake_digest": intake["intake_digest"],
                    "request_generation": str(request_object.generation),
                    "intake_generation": str(intake_object.generation),
                    "client_scope_id": request["client_scope_id"],
                    "policy_digest": request["policy_digest"],
                    "deployment_digest": self.deployment_digest,
                    "admitted_at": request["as_of"],
                    "deadline_at": deadline.isoformat(timespec="microseconds").replace(
                        "+00:00", "Z"
                    ),
                    "reserved_microusd": REQUEST_MICROUSD,
                    "execution": {"calls": {}, "queries": {}},
                }
            control["requests"][request_id] = proposed
            control["reserved_microusd"] += REQUEST_MICROUSD
            require_live_admission()
            self._objects.compare_control(stored.generation, control)
        # A final read resolves an acknowledged or ambiguous last conditional put.
        result = self.read_request(request_id, scope=scope)
        if result is not None and result["request"] == request and result["intake"] == intake:
            require_live_admission()
            return result["admission"]
        raise QuestionStoreError("control_conflict")


__all__ = ["GeneralQuestionStore", "QuestionStoreError", "build_initial_question_control"]
