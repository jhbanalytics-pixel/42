"""C04 renewal controller: activate a freshly observed pricing policy on the same image.

The controller reads the actual active binding and its ledger generation afresh
on every call, derives the new immutable policy with the engine's canonical
policy code so every limit and every old policy survives, and serialises
against ordinary deployment through the ledger's generation-conditional write.
It performs no Git, gcloud or network call; every native client is injected.
The pricing profile table and the activation reader checks were ported from
refresh_question_diagnostic_activation_20260909.py.
"""

import argparse
import copy
import json
import re
import sys
import traceback
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
for _entry in (str(_ROOT / "engine"), str(_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from src.analysis.open_intelligence import (  # noqa: E402
    general_question_policy as _policy_module,
)
from src.analysis.open_intelligence.brain_contract import (  # noqa: E402
    canonical_bytes,
    canonical_digest,
)
from src.analysis.open_intelligence.general_question_control import (  # noqa: E402
    QuestionStoreError,
)
from src.analysis.open_intelligence.general_question_deployment import (  # noqa: E402
    validate_question_deployment,
)
from src.analysis.open_intelligence.general_question_policy import (  # noqa: E402
    build_question_policy,
    require_question_admission_policy,
    validate_question_policy,
)

from ops.deploy import readback, release, resource_guard  # noqa: E402

OBSERVATION_CONTRACT_V1 = "42_pricing_observation_v1"
OBSERVATION_CONTRACT = "42_pricing_observation_v2"
_LOCATOR_PREFIX = "object:"
GRANT_CONTRACT = "42_renewal_grant_v1"
GRANT_PURPOSE = "pricing_renewal"
OBSERVATION_VALIDITY = timedelta(hours=24)
RENEWAL_INTERVAL = timedelta(hours=12)
PRICING_SOURCE = _policy_module._PRICING_SOURCE
LOCATION = "global"
SERVICE_TIER = "standard"
CURRENCY = "USD"
MODEL_38_DIGEST = "2885012a99bfc352c9ad92ced7b5bddd0b022f88e8ddc9d7688f237179062a8b"
MODEL_35_DIGEST = "519a658ad91b3b4d447298611d259421bbac5b7765d7c0f0049dc8bdd08c5857"


def _model_for(contract_digest):
    """Resolve the approved model identifier from the engine's own contract table."""
    return build_question_policy(
        pricing_verified_at=datetime(2026, 1, 1, tzinfo=UTC),
        approval_contract_digest=contract_digest,
    )["model"]


PRICING_PROFILES = {
    _model_for(MODEL_35_DIGEST): ("1.500000", "9.000000", "0.084", MODEL_35_DIGEST),
    _model_for(MODEL_38_DIGEST): ("0.750000", "3.750000", "0.039", MODEL_38_DIGEST),
}
LEDGER_MOVED_STATE = "ledger_moved_requires_native_reconciliation"
SPLIT_STATE = "ledger_ahead_of_served_revision_requires_native_reconciliation"
SERVED_AHEAD_STATE = "served_revision_ahead_of_ledger_requires_native_reconciliation"
INERT_STATE = "renewal_bound_but_neither_active_nor_served"
RECEIPT_CONTRACT = "42_renewal_receipt_v1"
EVIDENCE_PREFIX = release.OBJECT_PREFIX + "renewal/sources/"
# A renewal has to finish, and then a question has to run, inside what is left
# of the observation's life. Both numbers are taken from the code that spends
# them: two readbacks bounded by DEADLINE_SECONDS, then the question deadline
# the engine's own policy limits carry.
RENEWAL_WORST_CASE_SECONDS = 2 * 600.0
OBSERVATION_MARGIN = timedelta(
    seconds=RENEWAL_WORST_CASE_SECONDS + _policy_module.QUESTION_DEADLINE_SECONDS
)
DEADLINE_SECONDS = 600.0
POLL_SECONDS = 5.0
_ADMISSION_ERRORS = {
    "pricing review is stale": "observation_stale",
    "pricing review is in the future": "observation_future_dated",
    "question policy is expired": "policy_expired",
    "question policy cost exceeds request reservation": "rate_above_reservation_bound",
}
_OBSERVATION_FIELDS_V1 = {
    "contract_version",
    "observed_at",
    "source_url",
    "source_sha256",
    "model",
    "location",
    "service_tier",
    "currency",
    "input_usd_per_million",
    "output_usd_per_million",
}
# v2 adds the locator of the immutable object holding the bytes the rates
# were read from. The live page answers a different body to every request,
# so without it source_sha256 names bytes nobody can ever fetch again.
_OBSERVATION_FIELDS = _OBSERVATION_FIELDS_V1 | {"source_object"}
_GRANT_FIELDS = {
    "contract_version",
    "purpose",
    "resource_manifest_sha256",
    "granted_at",
    "expires_at",
    "grantor",
}
# The grant for a run nobody watches. It is written by hand, once, and it names
# what a v1 grant cannot: the release it covers, the rates Albert last proved
# and the page bytes he proved them against. An unattended run reuses exactly
# those rates and nothing else, so the grant is the authority for the price.
UNATTENDED_GRANT_CONTRACT = "42_unattended_renewal_grant_v1"
UNATTENDED_GRANT_PURPOSE = "unattended_pricing_renewal"
UNATTENDED_GRANT_MAX_WINDOW = timedelta(days=92)
_UNATTENDED_GRANT_FIELDS = _GRANT_FIELDS | {
    "release_commit",
    "model",
    "input_usd_per_million",
    "output_usd_per_million",
    "baseline_source_object",
    "baseline_source_sha256",
}
# The grant for moving the active policy to the extended expiry. It names the
# policy it replaces, so it authorises one replacement of one policy and cannot
# be spent on whatever a later renewal or release makes active.
EXPIRY_GRANT_CONTRACT = "42_policy_expiry_grant_v1"
EXPIRY_GRANT_PURPOSE = "policy_expiry_replacement"
EXPIRY_GRANT_MAX_WINDOW = timedelta(hours=8)
_EXPIRY_GRANT_FIELDS = _GRANT_FIELDS | {
    "from_policy_digest",
    "to_expires_at",
    "release_commit",
}
_EXPIRY_FIELDS = frozenset({"expires_at", "policy_digest"})
# A locator whose generation reads pending refuses before any native call. The
# managed job reads current instead: the generation at the one fixed name for
# each document when the run starts. That is a relaxation of the rule that the
# vector refuses before any native call until real generations are written in,
# because a sealed vector with pinned generations would need a job redeploy on
# every release and every grant. What keeps it safe is that the grant is
# written by hand after the index is staged and names the release commit: the
# run refuses unless the grant is live and names the same commit as the index
# and the active binding, refuses when the index object was created after the
# grant's granted_at, and records both generations and digests in its receipt,
# so a swapped or stale document at either name can only make the run refuse.
PENDING_GENERATION = "pending"
CURRENT_GENERATION = "current"
# How far ahead of its granted_at an unattended grant may be staged.
STAGE_AHEAD_LIMIT = timedelta(hours=24)
RENEWAL_INDEX_OBJECT = release.OBJECT_PREFIX + "renewal/release-index.json"
RENEWAL_GRANT_OBJECT = release.OBJECT_PREFIX + "renewal/unattended-grant.json"
_CURRENT_OBJECTS = {
    "release_index": RENEWAL_INDEX_OBJECT,
    "grant": RENEWAL_GRANT_OBJECT,
}
_RENEWAL_LOCATOR = (
    f"{_LOCATOR_PREFIX}gs://{release.BUCKET}/{release.OBJECT_PREFIX}renewal/"
)
MANAGED_JOB_ARGS = [
    "-m",
    "ops.deploy.refresh_question_policy",
    "renew-unattended",
    "--release-index",
    _RENEWAL_LOCATOR + "release-index.json#" + CURRENT_GENERATION,
    "--grant",
    _RENEWAL_LOCATOR + "unattended-grant.json#" + CURRENT_GENERATION,
    "--adapter",
    "/app/ops/deploy/release_native_adapter.py:factory",
    "--adapter-state",
    "adc",
    "--output",
    "/tmp/renewal-receipt.json",
]
# What is compared between the proven page and the live one. The body differs
# on every request, so its digest cannot be; the dollar amounts on it should not.
_PRICE_TOKEN = re.compile(r"\$\s*[0-9][0-9,]*(?:\.[0-9]+)?")
# The dollar list alone lets a changed row through whenever the old dollar
# amounts survive elsewhere on the page, or the row loses its dollar sign, or
# the unit moves under the same numbers. So every decimal numeral, with or
# without a dollar sign, and every unit token is compared in order as well. A
# numeral that is part of a longer dotted run, such as a version 1.2.3, is not
# one number and is skipped rather than half read.
_DECIMAL_TOKEN = re.compile(r"(?<![0-9.])[0-9][0-9,]*\.[0-9]+(?![0-9.])")
_UNIT_TOKEN = re.compile(
    r"\b[0-9][0-9,]*(?:\.[0-9]+)?\s*[KMB]\b"
    r"|(?i:\b(?:thousand|million|billion)s?\b)"
    r"|(?i:\bper\s+(?:token|character|image|second|minute|hour|request)s?\b)"
)


class Refused(ValueError):
    pass


def _refuse(code):
    raise Refused(code)


def _require(condition, code):
    if not condition:
        _refuse(code)


def _time(value, code):
    if not isinstance(value, str):
        _refuse(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _refuse(code)
    _require(parsed.tzinfo is not None, code)
    return parsed.astimezone(UTC)


def _rate(value):
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        _refuse("observation_invalid")
    _require(parsed > 0, "observation_invalid")
    return parsed


def _check_grant(grant, now, manifest_sha256):
    _require(type(grant) is dict and set(grant) == _GRANT_FIELDS, "grant_invalid")
    _require(grant["contract_version"] == GRANT_CONTRACT, "grant_invalid")
    _require(grant["purpose"] == GRANT_PURPOSE, "grant_invalid")
    _require(
        grant["resource_manifest_sha256"] == manifest_sha256, "grant_manifest_mismatch"
    )
    _require(_time(grant["granted_at"], "grant_invalid") <= now, "grant_not_yet_valid")
    _require(now < _time(grant["expires_at"], "grant_invalid"), "grant_expired")


def _check_unattended_grant(grant, now, manifest_sha256):
    """The hand written grant an unattended run answers to. Never a v1 grant."""
    _require(
        type(grant) is dict and set(grant) == _UNATTENDED_GRANT_FIELDS,
        "grant_invalid",
    )
    _require(grant["contract_version"] == UNATTENDED_GRANT_CONTRACT, "grant_invalid")
    _require(grant["purpose"] == UNATTENDED_GRANT_PURPOSE, "grant_invalid")
    _require(
        grant["resource_manifest_sha256"] == manifest_sha256, "grant_manifest_mismatch"
    )
    granted_at = _time(grant["granted_at"], "grant_invalid")
    expires_at = _time(grant["expires_at"], "grant_invalid")
    _require(
        expires_at - granted_at <= UNATTENDED_GRANT_MAX_WINDOW, "grant_window_too_long"
    )
    _require(granted_at <= now, "grant_not_yet_valid")
    _require(now < expires_at, "grant_expired")
    _require(
        isinstance(grant["release_commit"], str)
        and release._HEX40.fullmatch(grant["release_commit"]) is not None,
        "grant_invalid",
    )
    _require(
        isinstance(grant["model"], str) and grant["model"] in PRICING_PROFILES,
        "observation_model_unknown",
    )
    for field in ("input_usd_per_million", "output_usd_per_million"):
        _require(isinstance(grant[field], str), "grant_invalid")
        _rate(grant[field])
    try:
        _check_evidence_locator(
            grant["baseline_source_object"],
            grant["baseline_source_sha256"],
            code="grant_invalid",
        )
    except Refused as error:
        if str(error) == "observation_object_name_mismatch":
            _refuse("unattended_baseline_mismatch")
        raise


def _evidence_object_name(source_sha256):
    """The only name an evidence object is ever allowed to have."""
    return EVIDENCE_PREFIX + source_sha256 + ".html"


def _check_evidence_locator(locator, source_sha256, code="observation_object_invalid"):
    """The approved bucket, the evidence prefix, a generation, and the right name.

    ``observe`` always names the object after the digest of its own bytes, so
    requiring that here refuses a locator pointing at content it does not
    describe before a byte is read. It is not the whole proof and must not be
    read as one: the digest is still recomputed from what comes back.
    """
    _require(isinstance(locator, str) and locator.startswith(_LOCATOR_PREFIX), code)
    uri, marker, generation = locator[len(_LOCATOR_PREFIX) :].rpartition("#")
    name = release.approved_object_name(uri)
    _require(
        bool(marker)
        and name is not None
        and release._GENERATION.fullmatch(generation) is not None,
        code,
    )
    _require(
        isinstance(source_sha256, str)
        and release._HEX64.fullmatch(source_sha256) is not None
        and name == _evidence_object_name(source_sha256),
        "observation_object_name_mismatch",
    )
    return name, generation


def _check_observation(observation, now):
    _require(type(observation) is dict, "observation_invalid")
    version = observation.get("contract_version")
    _require(
        version in (OBSERVATION_CONTRACT, OBSERVATION_CONTRACT_V1),
        "observation_invalid",
    )
    fields = (
        _OBSERVATION_FIELDS
        if version == OBSERVATION_CONTRACT
        else _OBSERVATION_FIELDS_V1
    )
    _require(set(observation) == fields, "observation_invalid")
    if version == OBSERVATION_CONTRACT:
        _check_evidence_locator(
            observation["source_object"], observation.get("source_sha256")
        )
    return _check_observation_core(observation, now)


def _check_observation_core(observation, now):
    """Scope, model, age and rates, the rules both contract versions share."""
    scope = (
        observation["source_url"],
        observation["location"],
        observation["service_tier"],
        observation["currency"],
    )
    _require(
        scope == (PRICING_SOURCE, LOCATION, SERVICE_TIER, CURRENCY),
        "observation_scope_mismatch",
    )
    _require(
        isinstance(observation["model"], str)
        and observation["model"] in PRICING_PROFILES,
        "observation_model_unknown",
    )
    observed_at = _time(observation["observed_at"], "observation_invalid")
    _require(observed_at <= now, "observation_future_dated")
    _require(now - observed_at <= OBSERVATION_VALIDITY, "observation_stale")
    # Activating on an observation that expires mid flight buys nothing: the
    # renewal reports success and the next question is refused request_expired.
    _require(
        now - observed_at <= OBSERVATION_VALIDITY - OBSERVATION_MARGIN,
        "observation_expires_before_a_question_can_run",
    )
    rates = (
        _rate(observation["input_usd_per_million"]),
        _rate(observation["output_usd_per_million"]),
    )
    return observed_at, rates


def _read_source_bytes(clients, source_url):
    """The one fetch of the approved pricing source a run is allowed to make.

    The approved host is checked here, on the acting path, rather than left to
    the reader: the reader answers an unapproved reference with None exactly as
    it answers a transport failure, so without this check an operator could not
    tell a refusal from an outage. The three outcomes are therefore named
    apart: not approved, the reader declined, the fetch failed.
    """
    _require(source_url == PRICING_SOURCE, "observation_source_not_approved")
    reader = clients.get("bytes")
    _require(reader is not None, "observation_source_unreadable")
    try:
        raw = reader.read("source:" + source_url)
    except Refused:
        raise
    except Exception:
        _refuse("observation_source_unavailable")
    _require(isinstance(raw, bytes | bytearray), "observation_source_unreadable")
    return bytes(raw)


def _read_evidence_object(clients, locator, source_sha256):
    """Read the pinned page bytes by locator. Never touches the live page."""
    _check_evidence_locator(locator, source_sha256)
    reader = clients.get("bytes")
    _require(reader is not None, "observation_object_unreadable")
    try:
        raw = reader.read(locator)
    except Refused:
        raise
    except Exception:
        _refuse("observation_object_unavailable")
    _require(isinstance(raw, bytes | bytearray), "observation_object_unreadable")
    return bytes(raw)


def _observation_source_bytes(clients, observation):
    """The bytes an observation is proved against.

    Under v2 they come from the immutable object the observing run pinned, so a
    renewal makes no request to the vendor at all and the digest it recomputes
    is over the very bytes the rates were read from. A v1 document has no such
    object and can only be checked against the live page, which now answers a
    different body to every request.
    """
    if observation["contract_version"] == OBSERVATION_CONTRACT:
        return _read_evidence_object(
            clients, observation["source_object"], observation["source_sha256"]
        )
    return _read_source_bytes(clients, observation["source_url"])


def _require_rates_evidenced(raw, rates):
    """Each rate must appear in the bytes the caller read.

    This is a substring test over the whole body and nothing more. It catches a
    rate whose digits are absent from the page; it cannot tell the model's row
    from any other number on it. Parsing the vendor's table to do better would
    put a silently rotting parser behind a value the operator is accountable for.
    """
    text = bytes(raw).decode("utf-8", "replace")
    for rate in rates:
        _require(
            format(rate.normalize(), "f") in text, "observation_rate_not_evidenced"
        )


def _verify_source_bytes(raw, observation, rates):
    """Prove an observation against bytes the caller already read. Never fetches.

    The digest comparison is only worth making when ``raw`` was read
    independently of ``observation``, as it is on the renewal path where the
    observation arrives from an earlier run. Handing this function the same
    bytes the observation was built from would compare a value with itself.
    """
    raw = bytes(raw)
    _require(
        release._sha256(raw) == observation["source_sha256"],
        "observation_source_mismatch",
    )
    _require_rates_evidenced(raw, rates)
    return release._sha256(raw)


def _read_json_object(clients, name, code):
    stored = clients["objects"].read(name)
    _require(stored is not None, code)
    try:
        return json.loads(stored["raw"].decode("utf-8")), bytes(stored["raw"])
    except (UnicodeError, ValueError):
        _refuse(code)


class _Writes:
    """The one place a native write is either sent or recorded.

    The dry run is this class with ``execute`` false. Nothing renders a request
    anywhere else, so the request a dry run prints is built by the same lines
    that send it and the two cannot drift apart.
    """

    def __init__(self, *, execute):
        self.execute = execute
        self.requests = []

    def run(self, request, send):
        self.requests.append(request)
        return send() if self.execute else None


def _create_object_request(clients, name, raw, generation_match):
    return {
        "call": "create_object",
        "resource": release.BUCKET_RESOURCE,
        "action": "write",
        "object": name,
        "if_generation_match": int(generation_match),
        "sha256": release._sha256(raw),
        "bytes": len(raw),
        "native": _render(
            clients,
            "create_object",
            name=name,
            raw=raw,
            if_generation_match=int(generation_match),
        ),
    }


def _ensure_immutable_object(clients, manifest, name, raw, mutations, writes):
    stored = clients["objects"].read(name)
    if stored is not None:
        _require(bytes(stored["raw"]) == raw, "immutable_object_conflict")
        mutations.append(
            {
                "action": "reuse_immutable_artifact",
                "object": name,
                "generation": str(stored["generation"]),
            }
        )
        return str(stored["generation"])
    release._guard(manifest, release.BUCKET_RESOURCE, "write")
    written = writes.run(
        _create_object_request(clients, name, raw, 0),
        lambda: clients["objects"].create(name, raw, if_generation_match=0),
    )
    if written is None:
        return None
    generation = str(written["generation"])
    readback_object = clients["objects"].read(name, generation=generation)
    _require(
        readback_object is not None and bytes(readback_object["raw"]) == raw,
        "immutable_object_readback_mismatch",
    )
    mutations.append(
        {
            "action": "create_immutable_artifact",
            "object": name,
            "generation": generation,
            "sha256": release._sha256(raw),
        }
    )
    return generation


def _wait(clients, operation):
    clock = clients["clock"]
    return readback.wait_operation(
        clients["run"].read_operation,
        operation["operation"],
        deadline_seconds=DEADLINE_SECONDS,
        poll_seconds=POLL_SECONDS,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )


_REVISION_TEMPLATE_FIELDS = frozenset(
    {
        "labels",
        "annotations",
        "scaling",
        "vpcAccess",
        "timeout",
        "serviceAccount",
        "containers",
        "volumes",
        "executionEnvironment",
        "encryptionKey",
        "maxInstanceRequestConcurrency",
        "serviceMesh",
        "encryptionKeyRevocationAction",
        "encryptionKeyShutdownDuration",
        "sessionAffinity",
        "nodeSelector",
        "client",
        "clientVersion",
        "gpuZonalRedundancyDisabled",
    }
)
_REVISION_OUTPUT_FIELDS = frozenset(
    {
        "name",
        "uid",
        "generation",
        "createTime",
        "updateTime",
        "deleteTime",
        "expireTime",
        "launchStage",
        "service",
        "reconciling",
        "conditions",
        "observedGeneration",
        "logUri",
        "satisfiesPzs",
        "scalingStatus",
        "creator",
        "etag",
    }
)


def _serving_revision_template(clients, service, active, tag):
    release.verify_service(service, active, tag)
    revision_name = (
        release.SERVICE_API_NAME + "/revisions/" + active["revision_name"]
    )
    generation = service.get("generation")
    _require(
        service.get("reconciling", False) is False
        and isinstance(generation, str)
        and generation.isdecimal()
        and int(generation) > 0
        and service.get("observedGeneration") == generation
        and service.get("latestCreatedRevision") == revision_name
        and service.get("latestReadyRevision") == revision_name,
        "service_not_ready",
    )
    targets = release.traffic_targets(active, tag)
    statuses = [
        {key: item.get(key) for key in targets[0]}
        for item in (service.get("trafficStatuses") or [])
    ]
    _require(
        service.get("traffic") == targets and statuses == targets,
        "service_not_serving_active_revision",
    )
    revision = clients["run"].get_revision(revision_name)
    _require(revision is not None, "serving_revision_unavailable")
    release.verify_revision(revision, active, tag)
    _require(
        set(revision) <= _REVISION_TEMPLATE_FIELDS | _REVISION_OUTPUT_FIELDS,
        "serving_template_mismatch",
    )
    expected = {
        field: copy.deepcopy(revision[field])
        for field in _REVISION_TEMPLATE_FIELDS
        if field in revision
    }
    try:
        for container in expected["containers"]:
            container.pop("name", None)
    except (KeyError, TypeError, AttributeError):
        _refuse("serving_template_mismatch")
    expected["revision"] = active["revision_name"]
    _require(service["template"] == expected, "serving_template_mismatch")
    return expected


def _serving_state(clients, binding):
    """Whether the service is serving this binding, judged as the acting path judges it.

    Membership in the traffic list is not the question. A revision holding one
    percent is in that list while almost every question still goes elsewhere,
    so this asks the same thing the run asserts after it routes: that the
    service's traffic and its readback both equal the full target for this
    binding. The answer comes from Cloud Run, which is the half of the
    comparison a failed attempt cannot have written.
    """
    service = clients["run"].get_service(release.SERVICE_API_NAME)
    _require(service is not None, "service_unavailable")
    served = sorted(
        {
            item.get("revision")
            for item in (service.get("trafficStatuses") or [])
            if item.get("revision") and item.get("percent")
        }
    )
    try:
        tag, _ = release._tag_from_environment(
            service["template"]["containers"][0]["env"]
        )
    except (KeyError, IndexError, TypeError, ValueError):
        return False, served, None
    targets = release.traffic_targets(binding, tag)
    observed = [
        {key: item.get(key) for key in targets[0]}
        for item in (service.get("trafficStatuses") or [])
    ]
    serving = service.get("traffic") == targets and observed == targets
    return serving, served, targets


def _reconciliation(state, error, binding, policy, detail, remedy):
    return {
        "state": state,
        "error": error,
        "binding": binding,
        "policy": policy,
        "active": False,
        "ledger_generation": None,
        "split": {**detail, "remedy": remedy},
    }


_REMEDIES = {
    SPLIT_STATE: (
        "ledger_active_revision_not_served",
        "the ledger names this deployment active and the service is "
        "not serving {revision} in full, so every "
        "question is refused with approval_required; either route "
        "that revision natively and read the traffic back, or roll "
        "the ledger back to the deployment whose revision is "
        "serving. Do not rerun this tool to repair it.",
    ),
    SERVED_AHEAD_STATE: (
        "served_revision_not_named_by_ledger",
        "the service is serving {revision} while the "
        "ledger still names another deployment active, so every "
        "question is refused with approval_required; reconcile the "
        "ledger to the served deployment natively, or route traffic "
        "back to the deployment the ledger names.",
    ),
    INERT_STATE: (
        "renewal_deployment_neither_active_nor_served",
        "this policy is already bound to a deployment that the ledger "
        "does not activate and the service does not serve, so the "
        "renewal has not taken effect and rerunning will not make it "
        "take effect; activate and route that deployment natively, or "
        "remove the stale binding, then run the renewal again.",
    ),
}
# Worst first: a ledger naming an unserved revision, then traffic the ledger
# does not name, then a binding nothing uses.
_SEVERITY = (SPLIT_STATE, SERVED_AHEAD_STATE, INERT_STATE)


def _bound_binding(clients, deployment_digest, policy_digest):
    """Read a bound binding and check it as the acting path checks the active one.

    The stored document must validate, which recomputes its digest from its
    own content, and that digest must be the ledger key it is stored under and
    the binding must name the policy the ledger binds it to. Anything else is
    not this deployment, whatever revision it names.
    """
    name = release.OBJECT_PREFIX + f"deployments/{deployment_digest}/binding.json"
    binding, _ = _read_json_object(clients, name, "active_binding_unavailable")
    try:
        valid = validate_question_deployment(binding)
    except QuestionStoreError:
        return binding, False
    return valid, (
        valid["deployment_digest"] == deployment_digest
        and valid["policy_digest"] == policy_digest
    )


def _binding_state(detail):
    claimed, serving = detail["ledger_claims_active"], detail["serving"]
    if claimed and not serving:
        return SPLIT_STATE
    if serving and not claimed:
        return SERVED_AHEAD_STATE
    return INERT_STATE


def _existing_activation(clients, ledger, policy):
    """Decide what an existing binding means, from both sides, with no fall through.

    All four combinations of what the ledger claims and what Cloud Run serves
    are named here. Only one of them is a success, and it is the one where the
    service is actually serving the revision; a question is refused with
    approval_required in each of the other three, so none of them may exit
    zero by dropping out of the bottom of a conditional. Each bound deployment
    is decided on its own and the worst of them is reported with its remedy.
    """
    matches = []
    for deployment_digest, policy_digest in ledger["bindings"].items():
        if policy_digest != policy["policy_digest"]:
            continue
        binding, valid = _bound_binding(clients, deployment_digest, policy_digest)
        claimed = ledger["active_deployment_digest"] == deployment_digest
        # A binding that is not the deployment it is filed under proves nothing
        # about what is served, and one the ledger claims active is refused.
        _require(valid or not claimed, "renewal_binding_invalid")
        if valid:
            serving, served, targets = _serving_state(clients, binding)
        else:
            serving, served, targets = False, None, None
        detail = {
            "ledger_active_deployment_digest": ledger["active_deployment_digest"],
            "renewal_deployment_digest": deployment_digest,
            "ledger_claims_active": claimed,
            "binding_valid": valid,
            "expected_revision_name": binding.get("revision_name")
            if isinstance(binding, dict)
            else None,
            "expected_traffic": targets,
            "served_revisions": served,
            "serving": serving,
        }
        matches.append((binding, detail))

    for binding, detail in matches:
        if detail["ledger_claims_active"] and detail["serving"]:
            return {
                "state": "already_activated",
                "binding": binding,
                "policy": policy,
                "active": True,
                "ledger_claims_active": True,
                "served_revisions": detail["served_revisions"],
                "ledger_generation": None,
            }
    if not matches:
        return None
    decided = [(_binding_state(detail), binding, detail) for binding, detail in matches]
    state, binding, detail = min(decided, key=lambda item: _SEVERITY.index(item[0]))
    error, remedy = _REMEDIES[state]
    remedy = remedy.format(revision=detail["expected_revision_name"])
    if len(matches) > 1:
        detail = {
            "ledger_active_deployment_digest": ledger["active_deployment_digest"],
            "bound_deployments": [item for _, item in matches],
        }
        if state == INERT_STATE:
            error = "renewal_policy_bound_to_retired_deployments"
            remedy = (
                "this policy has no bound deployment that is both active and "
                "served; reconcile the ledger and traffic natively before "
                "rerunning renewal."
            )
    return _reconciliation(state, error, binding, policy, detail, remedy)


def _policy_rates(policy):
    return (
        policy["pricing"]["input_usd_per_million"],
        policy["pricing"]["output_usd_per_million"],
    )


def _read_active_policy(clients, control, active):
    """The stored policy the active binding names, validated against its digest."""
    policy_name = (
        release.OBJECT_PREFIX
        + f"policies/{control['bindings'][active['deployment_digest']]}/policy.json"
    )
    stored_policy, _ = _read_json_object(
        clients, policy_name, "active_policy_unavailable"
    )
    try:
        active_policy = validate_question_policy(stored_policy)
    except ValueError:
        _refuse("active_policy_unavailable")
    _require(
        active_policy["policy_digest"] == active["policy_digest"],
        "active_policy_unavailable",
    )
    return active_policy


def _settle(receipt, state, error):
    """Label the receipt; once the ledger has moved only reconciliation may follow."""
    if receipt.get("ledger_moved"):
        state = LEDGER_MOVED_STATE
    receipt.update(state=state, error=error)
    return receipt


def refresh_observed_policy(
    *,
    release_index: dict,
    observation: dict,
    grant: dict,
    clients: dict,
    execute: bool = True,
    unattended: bool = False,
    expected_ledger_generation: int | None = None,
) -> dict:
    """Renew the observed policy, or with ``execute`` false plan the same run.

    The planning form walks the identical code and suppresses the writes at the
    one place they are issued, so it cannot report a request the run would not
    make.

    With ``unattended`` the grant must be the unattended contract instead of v1,
    and the observed rates must equal both the grant's and the active policy's,
    checked here at the acting point after the ledger is read afresh.
    """
    receipt = {
        "state": "started",
        "mutations": [],
        "operations": {},
        "ledger_moved": False,
    }
    writes = _Writes(execute=execute)
    try:
        _require(
            expected_ledger_generation is None
            or (
                type(expected_ledger_generation) is int
                and expected_ledger_generation > 0
            ),
            "expected_ledger_generation_invalid",
        )
        return _refresh(
            release_index,
            observation,
            grant,
            clients,
            receipt,
            writes,
            unattended,
            expected_ledger_generation,
        )
    except ValueError as error:
        return _settle(receipt, "refused", str(error))
    except Exception as error:
        return _settle(receipt, "failed", f"{type(error).__name__}: {error}")


def _refresh(
    release_index,
    observation,
    grant,
    clients,
    receipt,
    writes,
    unattended=False,
    expected_ledger_generation=None,
):
    manifest = clients["manifest"]["value"]
    manifest_sha256 = clients["manifest"]["sha256"]
    now = clients["clock"].now()
    _require(isinstance(now, datetime) and now.tzinfo is not None, "clock_invalid")
    if unattended:
        _check_unattended_grant(grant, now, manifest_sha256)
    else:
        _check_grant(grant, now, manifest_sha256)
    observed_at, rates = _check_observation(observation, now)
    receipt["observation_source_sha256"] = _verify_source_bytes(
        _observation_source_bytes(clients, observation), observation, rates
    )
    release.validate_release_index_shape(release_index)
    _require(
        release_index["resource_manifest_sha256"] == manifest_sha256,
        "release_index_manifest_mismatch",
    )

    ledger = release._read_ledger(clients)
    if expected_ledger_generation is not None:
        _require(
            str(ledger["generation"]) == str(expected_ledger_generation),
            "stale_ledger_generation",
        )
    control = ledger["value"]
    active = release._active_binding(clients, control)
    _require(
        active["deployment_digest"] == control["active_deployment_digest"],
        "active_binding_unavailable",
    )
    _require(
        release_index["app_image"] == release.image_reference(active),
        "release_index_image_mismatch",
    )
    _require(
        release_index["commit"] == active["lp_commit"], "release_index_source_mismatch"
    )
    active_policy = _read_active_policy(clients, control, active)
    _require(
        observation["model"] == active_policy["model"], "observation_model_mismatch"
    )
    if expected_ledger_generation is not None:
        profile = PRICING_PROFILES[observation["model"]]
        observed_rates = (
            observation["input_usd_per_million"],
            observation["output_usd_per_million"],
        )
        receipt["rate_changed"] = observed_rates != profile[:2]
        _require(
            observed_rates == _policy_rates(active_policy),
            "active_policy_rate_mismatch",
        )
    if unattended:
        observed_rates = (
            observation["input_usd_per_million"],
            observation["output_usd_per_million"],
        )
        _require(
            observed_rates
            == (grant["input_usd_per_million"], grant["output_usd_per_million"])
            == _policy_rates(active_policy)
            and observation["model"] == grant["model"],
            "unattended_rate_changed",
        )

    try:
        policy = build_question_policy(
            pricing_verified_at=observed_at,
            input_usd_per_million=observation["input_usd_per_million"],
            output_usd_per_million=observation["output_usd_per_million"],
            approval_contract_digest=active_policy["approval_contract_digest"],
            expires_at=active_policy["expires_at"],
        )
        policy = require_question_admission_policy(policy, now=now)
    except ValueError as error:
        _refuse(_ADMISSION_ERRORS.get(str(error), "policy_invalid"))
    for field in (
        "limits",
        "stages",
        "model",
        "expires_at",
        "approval_contract_digest",
        "policy_id",
    ):
        _require(policy[field] == active_policy[field], "policy_scope_changed")
    profile = PRICING_PROFILES[observation["model"]]
    receipt["rate_changed"] = (
        observation["input_usd_per_million"],
        observation["output_usd_per_million"],
    ) != profile[:2]
    return _activate(
        clients,
        manifest,
        ledger,
        active,
        policy,
        receipt,
        writes,
        {"observation": observation},
    )


def _activate(
    clients, manifest, ledger, active, policy, receipt, writes, idempotency_seed
):
    """Bind, deploy, move the ledger and route: the one activation both paths share.

    A renewal and an expiry replacement differ only in the policy they hand in
    and the seed their idempotency key is derived from; every request after
    that is built here, so the two cannot drift apart.
    """
    control = ledger["value"]
    existing = _existing_activation(clients, control, policy)
    if existing is not None:
        existing["ledger_generation"] = ledger["generation"]
        receipt.update(existing)
        return receipt

    binding = copy.deepcopy(active)
    binding["policy_digest"] = policy["policy_digest"]
    binding["revision_name"] = (
        f"{release.SERVICE_NAME}-p-{policy['policy_digest'][:10]}"
    )
    del binding["deployment_digest"]
    binding["deployment_digest"] = canonical_digest(binding)
    try:
        binding = validate_question_deployment(binding)
    except QuestionStoreError:
        _refuse("binding_invalid")
    service = clients["run"].get_service(release.SERVICE_API_NAME)
    _require(service is not None, "service_unavailable")
    try:
        tag, _ = release._tag_from_environment(
            service["template"]["containers"][0]["env"]
        )
    except (KeyError, IndexError, TypeError, ValueError):
        _refuse("service_tag_unavailable")
    current_template = _serving_revision_template(clients, service, active, tag)
    receipt.update(
        {"policy": policy, "binding": binding, "tag": tag, "deployed_revision": None}
    )

    mutations = receipt["mutations"]
    _ensure_immutable_object(
        clients,
        manifest,
        release.OBJECT_PREFIX + f"policies/{policy['policy_digest']}/policy.json",
        canonical_bytes(policy),
        mutations,
        writes,
    )
    _ensure_immutable_object(
        clients,
        manifest,
        release.OBJECT_PREFIX
        + f"deployments/{binding['deployment_digest']}/binding.json",
        canonical_bytes(binding),
        mutations,
        writes,
    )
    idempotency_key = canonical_digest(
        {**idempotency_seed, "deployment_digest": binding["deployment_digest"]}
    )
    template = copy.deepcopy(current_template)
    template["revision"] = binding["revision_name"]
    deployment_entries = [
        entry
        for entry in template["containers"][0]["env"]
        if entry["name"] == "GENERAL_QUESTION_DEPLOYMENT_DIGEST"
    ]
    _require(len(deployment_entries) == 1, "serving_template_mismatch")
    deployment_entries[0]["value"] = binding["deployment_digest"]
    new_service = {**service, "template": template}
    release.verify_service(new_service, binding, tag)
    release._guard(manifest, release.SERVICE_RESOURCE, "deploy")
    operation = writes.run(
        {
            "call": "deploy_revision",
            "resource": release.SERVICE_RESOURCE,
            "action": "deploy",
            "service": release.SERVICE_API_NAME,
            "revision": binding["revision_name"],
            "idempotency_key": idempotency_key,
            "template": template,
            "native": _render(
                clients,
                "deploy_revision",
                service_name=release.SERVICE_API_NAME,
                template=template,
            ),
        },
        lambda: clients["run"].deploy_revision(
            release.SERVICE_API_NAME,
            revision=binding["revision_name"],
            template=template,
            idempotency_key=idempotency_key,
            expected_current_template=current_template,
            expected_current_service=copy.deepcopy(service),
        ),
    )
    if writes.execute:
        result = _wait(clients, operation)
        receipt["operations"]["deploy_revision"] = result
        if result["state"] != "succeeded":
            receipt.update(
                state="failed",
                error="deployment_unproven"
                if result["state"] == "unproven"
                else "deployment_failed",
            )
            return receipt
        receipt["deployed_revision"] = binding["revision_name"]
        revision = clients["run"].get_revision(
            release.SERVICE_API_NAME + "/revisions/" + binding["revision_name"]
        )
        _require(revision is not None, "revision_unavailable")
        release.verify_revision(revision, binding, tag)

    activated = copy.deepcopy(control)
    activated["bindings"][binding["deployment_digest"]] = policy["policy_digest"]
    activated["active_deployment_digest"] = binding["deployment_digest"]
    release._validate_ledger(activated)
    ledger_raw = canonical_bytes(activated)
    release._guard(manifest, release.BUCKET_RESOURCE, "write")
    ledger_request = _create_object_request(
        clients, release.LEDGER_OBJECT, ledger_raw, int(ledger["generation"])
    )
    ledger_request["active_deployment_digest"] = binding["deployment_digest"]
    try:
        written = writes.run(
            ledger_request,
            lambda: clients["objects"].create(
                release.LEDGER_OBJECT,
                ledger_raw,
                if_generation_match=int(ledger["generation"]),
            ),
        )
    except Exception as error:
        # A refused precondition and a lost response look identical from here,
        # and they are opposite events: one means nothing happened, the other
        # means the ledger moved and staging is mid activation. The call
        # returning is not evidence either way, so read the ledger back and
        # let what is actually stored decide the label.
        receipt["detail"] = f"{type(error).__name__}: {error}"
        try:
            after = release._read_ledger(clients)
        except Exception as read_error:
            receipt["ledger_readback_error"] = (
                f"{type(read_error).__name__}: {read_error}"
            )
            return _settle(receipt, "failed", "ledger_write_outcome_unknown")
        if canonical_bytes(after["value"]) == ledger_raw:
            receipt["activation"] = {
                "if_generation_match": int(ledger["generation"]),
                "generation": str(after["generation"]),
                "sha256": release._sha256(ledger_raw),
                "active_deployment_digest": binding["deployment_digest"],
            }
            receipt["ledger_moved"] = True
            return _settle(receipt, "failed", "ledger_write_response_lost")
        if after["generation"] != ledger["generation"]:
            return _settle(receipt, "failed", "stale_ledger_generation")
        return _settle(receipt, "failed", "ledger_write_failed")
    if writes.execute:
        receipt["activation"] = {
            "if_generation_match": int(ledger["generation"]),
            "generation": str(written["generation"]),
            "sha256": release._sha256(ledger_raw),
            "active_deployment_digest": binding["deployment_digest"],
        }
        receipt["ledger_moved"] = True

        service = clients["run"].get_service(release.SERVICE_API_NAME)
        _require(service is not None, "service_unavailable")
        release.verify_service(service, binding, tag)
        etag = service["etag"]
    else:
        # The route carries the etag the service reports after the new revision
        # exists. No deploy has happened on this path, so that value cannot be
        # known here, and printing the etag read before the deploy would show a
        # request the real run never sends.
        etag = None
    targets = release.traffic_targets(binding, tag)
    release._guard(manifest, release.SERVICE_RESOURCE, "deploy")
    traffic_request = {
        "call": "update_traffic",
        "resource": release.SERVICE_RESOURCE,
        "action": "deploy",
        "service": release.SERVICE_API_NAME,
        "traffic": targets,
        "idempotency_key": idempotency_key,
        "etag": etag,
        "native": _render(
            clients,
            "update_traffic",
            service_name=release.SERVICE_API_NAME,
            traffic=targets,
            etag=etag,
        ),
    }
    if not writes.execute:
        traffic_request["etag_unknown_until_deployed"] = True
    operation = writes.run(
        traffic_request,
        lambda: clients["run"].update_traffic(
            release.SERVICE_API_NAME,
            etag=etag,
            traffic=targets,
            idempotency_key=idempotency_key,
        ),
    )
    if not writes.execute:
        receipt.update(
            state="dry_run",
            requests=writes.requests,
            ledger_generation=ledger["generation"],
        )
        return receipt
    result = _wait(clients, operation)
    receipt["operations"]["route_traffic"] = result
    if result["state"] != "succeeded":
        return _settle(
            receipt,
            "failed",
            "route_unproven" if result["state"] == "unproven" else "route_failed",
        )
    after = clients["run"].get_service(release.SERVICE_API_NAME)
    observed = [
        {key: item.get(key) for key in targets[0]}
        for item in (after or {}).get("trafficStatuses", [])
    ]
    _require(
        (after or {}).get("traffic") == targets and observed == targets,
        "traffic_unverified",
    )
    final = release._read_ledger(clients)
    _require(
        final["generation"] == receipt["activation"]["generation"], "ledger_changed"
    )
    _require(
        final["value"]["active_deployment_digest"] == binding["deployment_digest"],
        "ledger_incompatible",
    )
    _require(
        canonical_bytes(final["value"]["requests"])
        == canonical_bytes(control["requests"]),
        "requests_changed",
    )
    receipt.update(state="activated", ledger_generation=final["generation"])
    return receipt


def _check_expiry_grant(grant, now, manifest_sha256):
    """The grant for the one expiry replacement, bound to the policy it replaces."""
    _require(
        type(grant) is dict and set(grant) == _EXPIRY_GRANT_FIELDS, "grant_invalid"
    )
    _require(grant["contract_version"] == EXPIRY_GRANT_CONTRACT, "grant_invalid")
    _require(grant["purpose"] == EXPIRY_GRANT_PURPOSE, "grant_invalid")
    _require(
        grant["resource_manifest_sha256"] == manifest_sha256, "grant_manifest_mismatch"
    )
    granted_at = _time(grant["granted_at"], "grant_invalid")
    expires_at = _time(grant["expires_at"], "grant_invalid")
    _require(expires_at - granted_at <= EXPIRY_GRANT_MAX_WINDOW, "grant_window_too_long")
    _require(granted_at <= now, "grant_not_yet_valid")
    _require(now < expires_at, "grant_expired")
    _require(
        isinstance(grant["from_policy_digest"], str)
        and release._HEX64.fullmatch(grant["from_policy_digest"]) is not None,
        "grant_invalid",
    )
    _require(
        isinstance(grant["release_commit"], str)
        and release._HEX40.fullmatch(grant["release_commit"]) is not None,
        "grant_invalid",
    )
    _require(
        grant["to_expires_at"] == _policy_module.EXTENDED_EXPIRES_AT,
        "grant_expiry_invalid",
    )


def replace_policy_expiry(
    *, release_index: dict, grant: dict, clients: dict, execute: bool = True
) -> dict:
    """Activate the granted policy with the extended expiry and nothing else changed.

    The replacement is the policy the grant names with expires_at moved and its
    digest recomputed. It is admitted as a renewal is, so it must run inside the
    24 hours the last renewal's pricing review covers, and it is activated by
    the same code a renewal uses. The grant names the policy it replaces, and
    the active binding must still bind that policy when the ledger is read, so
    a renewal or release in between refuses the run instead of being undone.
    """
    receipt = {
        "state": "started",
        "mutations": [],
        "operations": {},
        "ledger_moved": False,
    }
    writes = _Writes(execute=execute)
    try:
        return _replace_expiry(release_index, grant, clients, receipt, writes)
    except ValueError as error:
        return _settle(receipt, "refused", str(error))
    except Exception as error:
        return _settle(receipt, "failed", f"{type(error).__name__}: {error}")


def _replace_expiry(release_index, grant, clients, receipt, writes):
    manifest = clients["manifest"]["value"]
    manifest_sha256 = clients["manifest"]["sha256"]
    now = clients["clock"].now()
    _require(isinstance(now, datetime) and now.tzinfo is not None, "clock_invalid")
    _check_expiry_grant(grant, now, manifest_sha256)
    release.validate_release_index_shape(release_index)
    _require(
        release_index["resource_manifest_sha256"] == manifest_sha256,
        "release_index_manifest_mismatch",
    )
    # An image older than this code refuses a policy with the extended expiry, so
    # the grant names the release that carries it and the active binding must be
    # serving that release. Nothing here can read the image's code; the grant is
    # the attestation, as the unattended grant's release_commit is.
    _require(
        release_index["commit"] == grant["release_commit"],
        "expiry_release_commit_mismatch",
    )

    ledger = release._read_ledger(clients)
    control = ledger["value"]
    active = release._active_binding(clients, control)
    _require(
        active["deployment_digest"] == control["active_deployment_digest"],
        "active_binding_unavailable",
    )
    _require(
        release_index["app_image"] == release.image_reference(active),
        "release_index_image_mismatch",
    )
    _require(
        release_index["commit"] == active["lp_commit"], "release_index_source_mismatch"
    )

    replaced_digest = grant["from_policy_digest"]
    stored, _ = _read_json_object(
        clients,
        release.OBJECT_PREFIX + f"policies/{replaced_digest}/policy.json",
        "replaced_policy_unavailable",
    )
    try:
        replaced = validate_question_policy(stored)
    except ValueError:
        _refuse("replaced_policy_unavailable")
    _require(
        replaced["policy_digest"] == replaced_digest, "replaced_policy_unavailable"
    )
    _require(
        replaced["expires_at"] != grant["to_expires_at"], "policy_already_extended"
    )
    receipt["replaced_policy_digest"] = replaced_digest

    policy = copy.deepcopy(replaced)
    policy["expires_at"] = grant["to_expires_at"]
    del policy["policy_digest"]
    policy["policy_digest"] = canonical_digest(policy)
    try:
        policy = validate_question_policy(policy)
        policy = require_question_admission_policy(policy, now=now)
    except ValueError as error:
        _refuse(_ADMISSION_ERRORS.get(str(error), "policy_invalid"))
    _require(
        {key: value for key, value in policy.items() if key not in _EXPIRY_FIELDS}
        == {key: value for key, value in replaced.items() if key not in _EXPIRY_FIELDS},
        "policy_scope_changed",
    )

    # A rerun after the replacement finds it bound and reports what is served;
    # _existing_activation never routes traffic, so skipping the check below
    # cannot move anything. Otherwise the active binding must still bind the
    # policy the grant names.
    if policy["policy_digest"] not in control["bindings"].values():
        _require(
            control["bindings"][active["deployment_digest"]] == replaced_digest
            and active["policy_digest"] == replaced_digest,
            "active_policy_moved",
        )
    return _activate(
        clients,
        manifest,
        ledger,
        active,
        policy,
        receipt,
        writes,
        {
            "expiry_replacement": replaced_digest,
            "to_expires_at": grant["to_expires_at"],
        },
    )


_SUCCESS_STATES = ("activated", "already_activated")


def _observe_bytes_name(raw):
    """The evidence object is named by the digest of its own bytes.

    Name and content therefore cannot disagree, and two runs that read the same
    body converge on one object instead of racing to write two.
    """
    return _evidence_object_name(release._sha256(raw))


def _observation_document(args, clients, manifest, now, writes, receipt):
    """Fetch the approved source once, pin those bytes, and describe them.

    The vendor serves a different body on every request, so the bytes the rates
    were read from cannot be fetched again. They are written to an immutable
    object instead, and the observation carries the locator beside the digest,
    which is what lets a later renewal recompute that digest against the very
    bytes this run saw rather than against a page that has moved on.
    """
    raw = _read_source_bytes(clients, PRICING_SOURCE)
    pending = {
        "contract_version": OBSERVATION_CONTRACT_V1,
        "observed_at": release._stamp(now),
        "source_url": PRICING_SOURCE,
        "source_sha256": release._sha256(raw),
        "model": args.model,
        "location": args.location,
        "service_tier": args.service_tier,
        "currency": args.currency,
        "input_usd_per_million": args.input_usd_per_million,
        "output_usd_per_million": args.output_usd_per_million,
    }
    # Everything that can be judged before a byte is written, is.
    observed_at, rates = _check_observation_core(pending, now)
    try:
        build_question_policy(
            pricing_verified_at=observed_at,
            input_usd_per_million=pending["input_usd_per_million"],
            output_usd_per_million=pending["output_usd_per_million"],
            approval_contract_digest=PRICING_PROFILES[pending["model"]][3],
        )
    except ValueError:
        _refuse("observation_rate_invalid")
    # Only the rate proof runs against these bytes. The digest above is the
    # digest of the same bytes, so comparing the two would prove nothing.
    _require_rates_evidenced(raw, rates)

    name = _observe_bytes_name(raw)
    generation = _ensure_immutable_object(
        clients, manifest, name, raw, receipt["mutations"], writes
    )
    receipt["source_object_name"] = name
    receipt["source_object_generation"] = generation
    receipt["source_sha256"] = pending["source_sha256"]
    receipt["observed_at"] = pending["observed_at"]
    if generation is None:
        return None
    document = dict(
        pending,
        contract_version=OBSERVATION_CONTRACT,
        source_object=f"{_LOCATOR_PREFIX}gs://{release.BUCKET}/{name}#{generation}",
    )
    _check_observation(document, now)
    return document


def _observe(args, clients):
    manifest = clients["manifest"]["value"]
    manifest_sha256 = clients["manifest"]["sha256"]
    grant, grant_input = _load_document(clients, args.grant, "grant")
    now = clients["clock"].now()
    _require(isinstance(now, datetime) and now.tzinfo is not None, "clock_invalid")
    # observe writes to the bucket, so it answers to the same grant the renewal
    # does, and it answers before anything is written.
    _check_grant(grant, now, manifest_sha256)
    inputs = {"grant": grant_input, "source_url": PRICING_SOURCE}
    command = "observe --dry-run" if args.dry_run else "observe"
    writes = _Writes(execute=not args.dry_run)
    receipt = {"state": "started", "mutations": []}
    release._write_once(args.receipt, _envelope(command, inputs, dict(receipt)))

    def settle(state, extra=None):
        receipt.update(state=state, requests=writes.requests, **(extra or {}))
        release._rewrite(args.receipt, _envelope(command, inputs, receipt))
        summary = {
            "state": state,
            "output": str(args.output),
            "receipt": str(args.receipt),
            "contract_version": RECEIPT_CONTRACT,
            "command": command,
            "inputs": inputs,
        }
        for key in (
            "error",
            "source_sha256",
            "source_object_name",
            "source_object_generation",
            "observed_at",
            "requests",
        ):
            if key in receipt:
                summary[key] = receipt[key]
        return summary

    try:
        document = _observation_document(args, clients, manifest, now, writes, receipt)
    except ValueError as error:
        return 1, settle("refused", {"error": str(error)})
    if document is None:
        return 0, settle("dry_run")
    receipt["observation"] = document
    release._write_once(args.output, document)
    return 0, settle("observed")


def _load_document(clients, spec, name, *, current=False):
    """A local JSON file, or object:gs://<approved bucket>/<name>#<generation>.

    With ``current`` the generation may read current, for the one fixed name of
    that document only: the object is read at whatever generation it has now,
    and the generation, digest and creation time read are what is returned.
    """
    if not isinstance(spec, str) or not spec.startswith(_LOCATOR_PREFIX):
        value, raw = release._read_json(spec, f"{name}_unreadable")
        return value, {"path": str(spec), "sha256": release._sha256(raw)}
    uri, marker, generation = spec[len(_LOCATOR_PREFIX) :].rpartition("#")
    _require(generation != PENDING_GENERATION, f"{name}_generation_pending")
    if generation == CURRENT_GENERATION:
        object_name = _CURRENT_OBJECTS.get(name) if current else None
        _require(
            object_name is not None
            and bool(marker)
            and release.approved_object_name(uri) == object_name,
            f"{name}_locator_invalid",
        )
        return _load_current(clients, object_name, name)
    _require(
        bool(marker)
        and release.approved_object_name(uri) is not None
        and release._GENERATION.fullmatch(generation) is not None,
        f"{name}_locator_invalid",
    )
    raw = clients["bytes"].read(spec)
    _require(isinstance(raw, bytes | bytearray), f"{name}_unreadable")
    raw = bytes(raw)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        _refuse(f"{name}_unreadable")
    return value, {"locator": spec, "sha256": release._sha256(raw)}


def _load_current(clients, object_name, name):
    stored = clients["objects"].read(object_name)
    _require(stored is not None, f"{name}_unreadable")
    raw = bytes(stored["raw"])
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        _refuse(f"{name}_unreadable")
    generation = str(stored["generation"])
    return value, {
        "locator": f"{_LOCATOR_PREFIX}gs://{release.BUCKET}/{object_name}#{generation}",
        "generation": generation,
        "sha256": release._sha256(raw),
        "time_created": stored.get("time_created"),
    }


def _check_index_older_than_grant(index_input, grant):
    """The index a grant answers for must already have been there when granted.

    A grant is written after its index is staged, so an index object created
    after the grant's granted_at was put there after the person looked, and is
    refused even when it names the same commit.
    """
    created = index_input.get("time_created")
    _require(isinstance(created, str), "release_index_time_unavailable")
    staged_at = _time(created, "release_index_time_unavailable")
    _require(
        staged_at <= _time(grant["granted_at"], "grant_invalid"),
        "release_index_newer_than_grant",
    )


def _check_index_older_than_grant_object(index_input, grant_input):
    """The index must also predate the grant object read beside it.

    A grant may be staged before its granted_at, so an index restaged in that
    gap passes the granted_at rule; it cannot pass this one, because the grant
    object was written after the index the person staged it against.
    """
    created = grant_input.get("time_created")
    _require(isinstance(created, str), "grant_time_unavailable")
    staged_at = _time(index_input.get("time_created"), "release_index_time_unavailable")
    _require(
        staged_at <= _time(created, "grant_time_unavailable"),
        "release_index_newer_than_grant",
    )


def _render(clients, call, **kwargs):
    native = clients.get("_native")
    return native.render(call, **kwargs) if native is not None else None


def _envelope(command, inputs, receipt):
    """Every receipt this tool writes names its contract and the inputs it ran on."""
    return {
        "contract_version": RECEIPT_CONTRACT,
        "command": command,
        "inputs": inputs,
        **receipt,
    }


def _renew(args, clients):
    inputs = {}
    documents = {}
    for name, spec in (
        ("release_index", args.release_index),
        ("observation", args.observation),
        ("grant", args.grant),
    ):
        documents[name], inputs[name] = _load_document(clients, spec, name)
    command = "renew --dry-run" if args.dry_run else "renew"
    release._write_once(
        args.output,
        _envelope(command, inputs, {"state": "started"}),
    )
    receipt = refresh_observed_policy(
        release_index=documents["release_index"],
        observation=documents["observation"],
        grant=documents["grant"],
        clients=clients,
        execute=not args.dry_run,
        expected_ledger_generation=args.expected_ledger_generation,
    )
    written = _envelope(command, inputs, receipt)
    release._rewrite(args.output, written)
    summary = {
        "state": receipt["state"],
        "output": str(args.output),
        "contract_version": RECEIPT_CONTRACT,
        "command": command,
        "inputs": inputs,
    }
    for key in (
        "error",
        "deployed_revision",
        "ledger_generation",
        "active",
        "rate_changed",
        "split",
        "requests",
        "observation_source_sha256",
        "policy",
        "binding",
        "tag",
    ):
        if key in receipt:
            summary[key] = receipt[key]
    if "binding" in receipt:
        summary["deployment_digest"] = receipt["binding"]["deployment_digest"]
    success = receipt["state"] in (
        _SUCCESS_STATES if args.dry_run is False else _SUCCESS_STATES + ("dry_run",)
    )
    return (0 if success else 1), summary


def _extend_expiry(args, clients):
    inputs = {}
    documents = {}
    for name, spec in (
        ("release_index", args.release_index),
        ("grant", args.grant),
    ):
        documents[name], inputs[name] = _load_document(clients, spec, name)
    command = "extend-expiry --dry-run" if args.dry_run else "extend-expiry"
    release._write_once(
        args.output,
        _envelope(command, inputs, {"state": "started"}),
    )
    receipt = replace_policy_expiry(
        release_index=documents["release_index"],
        grant=documents["grant"],
        clients=clients,
        execute=not args.dry_run,
    )
    written = _envelope(command, inputs, receipt)
    release._rewrite(args.output, written)
    summary = {
        "state": receipt["state"],
        "output": str(args.output),
        "contract_version": RECEIPT_CONTRACT,
        "command": command,
        "inputs": inputs,
    }
    for key in (
        "error",
        "detail",
        "deployed_revision",
        "ledger_generation",
        "ledger_moved",
        "ledger_readback_error",
        "activation",
        "active",
        "split",
        "requests",
        "replaced_policy_digest",
        "policy",
        "binding",
        "tag",
    ):
        if key in receipt:
            summary[key] = receipt[key]
    if "binding" in receipt:
        summary["deployment_digest"] = receipt["binding"]["deployment_digest"]
    success = receipt["state"] in (
        _SUCCESS_STATES if args.dry_run is False else _SUCCESS_STATES + ("dry_run",)
    )
    return (0 if success else 1), summary


def _price_list(raw):
    text = bytes(raw).decode("utf-8", "replace")
    return [re.sub(r"\s", "", token) for token in _PRICE_TOKEN.findall(text)]


def _token_list(pattern, raw):
    text = bytes(raw).decode("utf-8", "replace")
    return [re.sub(r"\s", "", token).lower() for token in pattern.findall(text)]


def _require_priced(prices, rates, code):
    """Each rate must be one of the page's dollar amounts, whole, not a substring.

    The bare substring test accepts 0.75 inside 10.75. A dollar amount is a
    whole token, so "$0.75" is not found in "$10.75" or "$0.755".
    """
    for rate in rates:
        _require("$" + format(rate.normalize(), "f") in prices, code)


class _WholePageParser(HTMLParser):
    _TRAILER_ELEMENTS = frozenset(
        {
            "a",
            "div",
            "footer",
            "h3",
            "i",
            "li",
            "nav",
            "polygon",
            "section",
            "span",
            "svg",
            "ul",
        }
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.valid = True
        self.html_starts = 0
        self.html_ends = 0
        self.body_starts = 0
        self.body_ends = 0
        self.root_closed = False
        self.trailer_phase = "document"
        self.script_data_chars = 0
        self.footer_stack = []
        self.footer_has_content = False

    def handle_starttag(self, tag, attrs):
        if not self.root_closed:
            if tag == "html":
                self.html_starts += 1
                if self.html_starts != 1:
                    self.valid = False
            elif tag == "body":
                self.body_starts += 1
                if self.body_starts != 1 or self.html_starts != 1:
                    self.valid = False
            return
        if self.trailer_phase == "after_html" and tag == "script":
            self.trailer_phase = "script"
        elif self.trailer_phase == "after_script" and tag == "div":
            self.trailer_phase = "empty_div"
        elif self.trailer_phase == "after_div" and tag == "footer":
            self.trailer_phase = "footer"
            self.footer_stack = ["footer"]
            self.footer_has_content = False
        elif self.trailer_phase == "footer" and tag in self._TRAILER_ELEMENTS - {
            "footer"
        }:
            self.footer_stack.append(tag)
        else:
            self.valid = False

    def handle_endtag(self, tag):
        if not self.root_closed:
            if tag == "body":
                self.body_ends += 1
                if self.body_ends != 1 or self.body_starts != 1:
                    self.valid = False
            elif tag == "html":
                self.html_ends += 1
                if self.html_starts != 1 or self.html_ends != 1:
                    self.valid = False
                if self.body_starts and self.body_ends != 1:
                    self.valid = False
                self.root_closed = True
                self.trailer_phase = "after_html"
            return
        if self.trailer_phase == "script" and tag == "script":
            if self.script_data_chars == 0:
                self.valid = False
            self.trailer_phase = "after_script"
        elif self.trailer_phase == "empty_div" and tag == "div":
            self.trailer_phase = "after_div"
        elif (
            self.trailer_phase == "footer"
            and self.footer_stack
            and tag == self.footer_stack[-1]
        ):
            self.footer_stack.pop()
            if not self.footer_stack:
                if not self.footer_has_content:
                    self.valid = False
                self.trailer_phase = "complete"
        else:
            self.valid = False

    def handle_data(self, data):
        if not self.root_closed:
            return
        if self.trailer_phase == "script":
            self.script_data_chars += len(data.strip())
        elif self.trailer_phase == "footer":
            self.footer_has_content |= bool(data.strip())
        elif data.strip():
            self.valid = False

    def handle_comment(self, data):
        if self.root_closed:
            self.valid = False

    def handle_decl(self, decl):
        if self.root_closed:
            self.valid = False

    def handle_pi(self, data):
        if self.root_closed:
            self.valid = False

    def unknown_decl(self, data):
        if self.root_closed:
            self.valid = False

    def handle_startendtag(self, tag, attrs):
        if self.root_closed:
            self.valid = False
            return
        if tag in {"html", "body"}:
            self.valid = False
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def complete(self):
        if (
            not self.valid
            or bool(self.rawdata)
            or not self.root_closed
            or self.html_starts != 1
            or self.html_ends != 1
            or (self.body_starts and self.body_ends != 1)
        ):
            return False
        if self.trailer_phase == "after_html":
            return True
        return (
            self.body_starts == 1
            and self.body_ends == 1
            and self.trailer_phase == "complete"
            and not self.footer_stack
        )


def _require_whole_page(raw):
    """Require a closed document or the observed complete post-document trailer."""
    parser = _WholePageParser()
    parser.feed(bytes(raw).decode("latin-1"))
    _require(not parser.rawdata, "unattended_source_truncated")
    parser.close()
    _require(parser.complete(), "unattended_source_truncated")


def _unattended_observation(grant, clients, manifest, now, receipt):
    """Everything an unattended run proves before it writes, then the one write.

    The rates are the grant's, never read off the page. The page is fetched
    once and has to carry the same dollar amounts, in the same order, as the
    baseline bytes Albert proved those rates against; any change hands the
    renewal back to a person. Only then are the live bytes pinned and the
    observation built, so every refusal above leaves nothing written.
    """
    baseline = _read_evidence_object(
        clients, grant["baseline_source_object"], grant["baseline_source_sha256"]
    )
    _require(
        release._sha256(baseline) == grant["baseline_source_sha256"],
        "unattended_baseline_mismatch",
    )
    pending = {
        "contract_version": OBSERVATION_CONTRACT_V1,
        "observed_at": release._stamp(now),
        "source_url": PRICING_SOURCE,
        "source_sha256": None,
        "model": grant["model"],
        "location": LOCATION,
        "service_tier": SERVICE_TIER,
        "currency": CURRENCY,
        "input_usd_per_million": grant["input_usd_per_million"],
        "output_usd_per_million": grant["output_usd_per_million"],
    }
    _observed_at, rates = _check_observation_core(pending, now)
    _require_whole_page(baseline)
    baseline_prices = _price_list(baseline)
    _require(bool(baseline_prices), "unattended_source_changed")
    _require_priced(baseline_prices, rates, "unattended_baseline_rate_not_priced")
    _require_rates_evidenced(baseline, rates)
    live = _read_source_bytes(clients, PRICING_SOURCE)
    _require_whole_page(live)
    live_prices = _price_list(live)
    _require_priced(live_prices, rates, "observation_rate_not_evidenced")
    _require(live_prices == baseline_prices, "unattended_source_changed")
    for pattern in (_DECIMAL_TOKEN, _UNIT_TOKEN):
        _require(
            _token_list(pattern, live) == _token_list(pattern, baseline),
            "unattended_source_changed",
        )
    _require_rates_evidenced(live, rates)
    pending["source_sha256"] = release._sha256(live)
    receipt["baseline_source_sha256"] = grant["baseline_source_sha256"]
    receipt["live_source_sha256"] = pending["source_sha256"]
    name = _observe_bytes_name(live)
    generation = _ensure_immutable_object(
        clients, manifest, name, live, receipt["mutations"], _Writes(execute=True)
    )
    document = dict(
        pending,
        contract_version=OBSERVATION_CONTRACT,
        source_object=f"{_LOCATOR_PREFIX}gs://{release.BUCKET}/{name}#{generation}",
    )
    _check_observation(document, now)
    return document


def _check_index_shape(index, manifest_sha256):
    release.validate_release_index_shape(index)
    _require(
        index["resource_manifest_sha256"] == manifest_sha256,
        "release_index_manifest_mismatch",
    )


def _check_index_against_active(index, clients):
    control = release._read_ledger(clients)["value"]
    active = release._active_binding(clients, control)
    _require(
        active["deployment_digest"] == control["active_deployment_digest"],
        "active_binding_unavailable",
    )
    _require(
        index["app_image"] == release.image_reference(active),
        "release_index_image_mismatch",
    )
    _require(index["commit"] == active["lp_commit"], "release_index_source_mismatch")
    return control, active


def _unattended_preflight(
    documents, clients, manifest_sha256, now, index_input=None, grant_input=None
):
    """Read only checks, in the order that writes nothing and fetches nothing.

    Returns ``not_due`` when the active review is younger than the renewal
    interval less the observation margin, so a twelve hour trigger renews every
    time and an extra trigger does nothing.
    """
    grant, index = documents["grant"], documents["release_index"]
    _check_unattended_grant(grant, now, manifest_sha256)
    if index_input is not None and "time_created" in index_input:
        _check_index_older_than_grant(index_input, grant)
        if grant_input is not None and "time_created" in grant_input:
            _check_index_older_than_grant_object(index_input, grant_input)
    _check_index_shape(index, manifest_sha256)
    _require(
        index["commit"] == grant["release_commit"],
        "unattended_release_commit_mismatch",
    )
    control, active = _check_index_against_active(index, clients)
    active_policy = _read_active_policy(clients, control, active)
    _require(
        active_policy["model"] == grant["model"]
        and _policy_rates(active_policy)
        == (grant["input_usd_per_million"], grant["output_usd_per_million"]),
        "unattended_rates_not_proven",
    )
    reviewed_at = _time(
        active_policy["pricing"]["verified_at"], "active_policy_unavailable"
    )
    return now - reviewed_at >= RENEWAL_INTERVAL - OBSERVATION_MARGIN


def _renew_unattended(args, clients):
    command = "renew-unattended"
    inputs, documents = {}, {}
    for name, spec in (
        ("release_index", args.release_index),
        ("grant", args.grant),
    ):
        documents[name], inputs[name] = _load_document(
            clients, spec, name, current=True
        )
    release._write_once(args.output, _envelope(command, inputs, {"state": "started"}))
    receipt = {"state": "started", "mutations": []}
    manifest = clients["manifest"]["value"]
    manifest_sha256 = clients["manifest"]["sha256"]
    try:
        now = clients["clock"].now()
        _require(isinstance(now, datetime) and now.tzinfo is not None, "clock_invalid")
        # Either document read at current brings the time rule in, so an index
        # read another way beside a current grant refuses rather than skip it.
        index_input = inputs["release_index"]
        if any("time_created" in item for item in inputs.values()):
            index_input = {"time_created": None, **index_input}
        if not _unattended_preflight(
            documents, clients, manifest_sha256, now, index_input, inputs["grant"]
        ):
            receipt["state"] = "not_due"
        else:
            document = _unattended_observation(
                documents["grant"], clients, manifest, now, receipt
            )
            renewal = refresh_observed_policy(
                release_index=documents["release_index"],
                observation=document,
                grant=documents["grant"],
                clients=clients,
                unattended=True,
            )
            renewal["mutations"] = receipt["mutations"] + renewal["mutations"]
            receipt.update(renewal, observation=document)
    except ValueError as error:
        receipt.update(state="refused", error=str(error))
    except Exception as error:
        receipt.update(state="failed", error=f"{type(error).__name__}: {error}")
    written = _envelope(command, inputs, receipt)
    release._rewrite(args.output, written)
    # The job's container and its /tmp are thrown away when it exits, so the
    # durable copy of the receipt is the job's own output: the whole receipt is
    # printed, with its digest, and the fields a reconciliation needs first are
    # lifted to the top of the summary.
    summary = {
        "state": receipt["state"],
        "output": str(args.output),
        "contract_version": RECEIPT_CONTRACT,
        "command": command,
        "inputs": inputs,
        "receipt": written,
        "receipt_sha256": release._sha256(canonical_bytes(written)),
    }
    for key in (
        "error",
        "detail",
        "deployed_revision",
        "ledger_generation",
        "ledger_moved",
        "ledger_readback_error",
        "activation",
        "mutations",
        "operations",
        "active",
        "rate_changed",
        "split",
        "baseline_source_sha256",
        "live_source_sha256",
        "observation_source_sha256",
    ):
        if key in receipt:
            summary[key] = receipt[key]
    if "binding" in receipt:
        summary["deployment_digest"] = receipt["binding"]["deployment_digest"]
    success = receipt["state"] in _SUCCESS_STATES + ("not_due",)
    return (0 if success else 1), summary


def _stage_document(clients, manifest, name, raw, writes):
    """Write one document at its fixed name, conditional on what was read there."""
    current = clients["objects"].read(name)
    if current is not None and bytes(current["raw"]) == raw:
        generation = str(current["generation"])
        action = "unchanged"
    else:
        expected = 0 if current is None else int(current["generation"])
        release._guard(manifest, release.BUCKET_RESOURCE, "write")
        written = writes.run(
            _create_object_request(clients, name, raw, expected),
            lambda: clients["objects"].create(name, raw, if_generation_match=expected),
        )
        if written is None:
            return {"object": name, "action": "planned", "sha256": release._sha256(raw)}
        generation = str(written["generation"])
        stored = clients["objects"].read(name, generation=generation)
        _require(
            stored is not None and bytes(stored["raw"]) == raw,
            "staged_object_readback_mismatch",
        )
        action = "written"
    return {
        "object": name,
        "action": action,
        "generation": generation,
        "sha256": release._sha256(raw),
        "locator": f"{_LOCATOR_PREFIX}gs://{release.BUCKET}/{name}#{generation}",
    }


def _stage_unattended(args, clients):
    """Check one document as the job will, then put it where the job reads it.

    Exactly one of the two is staged per run. The index is staged first and is
    checked against the active binding. The grant is staged after it and is
    checked with every check the job's preflight makes before it fetches the
    page, against the index staged at that moment, including that the index
    object was created no later than the grant's granted_at; the baseline bytes
    are read and hashed too. The grant may be staged up to a day before its
    granted_at and is then checked as it will stand at granted_at. A document the job would refuse is never staged.
    The write is conditional on the generation read at that name, and an
    identical document already there is left as it is.
    """
    command = "stage-unattended --dry-run" if args.dry_run else "stage-unattended"
    name = "release_index" if args.release_index is not None else "grant"
    spec = args.release_index if name == "release_index" else args.grant
    value, raw = release._read_json(spec, f"{name}_unreadable")
    inputs = {name: {"path": str(spec), "sha256": release._sha256(raw)}}
    release._write_once(args.output, _envelope(command, inputs, {"state": "started"}))
    receipt = {"state": "started"}
    writes = _Writes(execute=not args.dry_run)
    manifest = clients["manifest"]["value"]
    manifest_sha256 = clients["manifest"]["sha256"]
    try:
        now = clients["clock"].now()
        _require(isinstance(now, datetime) and now.tzinfo is not None, "clock_invalid")
        if name == "release_index":
            _check_index_shape(value, manifest_sha256)
            _check_index_against_active(value, clients)
            target = RENEWAL_INDEX_OBJECT
        else:
            index, index_input = _load_current(
                clients, RENEWAL_INDEX_OBJECT, "release_index"
            )
            receipt["staged_release_index"] = index_input
            documents = {"release_index": index, "grant": value}
            # A grant shown in full before the go names a granted_at after the
            # brief's writes, so it may be staged ahead of it, by at most a day,
            # and is checked as it will stand when it starts. The job itself
            # refuses grant_not_yet_valid until then.
            granted_at = _time(
                value.get("granted_at") if type(value) is dict else None,
                "grant_invalid",
            )
            _require(granted_at - now <= STAGE_AHEAD_LIMIT, "grant_start_too_far")
            checked_as_of = max(now, granted_at)
            receipt["grant_checked_as_of"] = checked_as_of.isoformat()
            _unattended_preflight(
                documents, clients, manifest_sha256, checked_as_of, index_input
            )
            baseline = _read_evidence_object(
                clients,
                value["baseline_source_object"],
                value["baseline_source_sha256"],
            )
            _require(
                release._sha256(baseline) == value["baseline_source_sha256"],
                "unattended_baseline_mismatch",
            )
            target = RENEWAL_GRANT_OBJECT
        receipt[name] = _stage_document(clients, manifest, target, raw, writes)
        if args.dry_run:
            receipt.update(state="dry_run", requests=writes.requests)
        else:
            receipt["state"] = "staged"
    except ValueError as error:
        receipt.update(state="refused", error=str(error))
    except Exception as error:
        receipt.update(state="failed", error=f"{type(error).__name__}: {error}")
    written = _envelope(command, inputs, receipt)
    release._rewrite(args.output, written)
    summary = {"output": str(args.output), **written}
    success = receipt["state"] in ("staged", "dry_run")
    return (0 if success else 1), summary


def _add_common(parser):
    parser.add_argument(
        "--resources", default=None, help="approved resource manifest path"
    )
    parser.add_argument(
        "--resources-sha256",
        default=None,
        help="independently approved manifest digest",
    )


def _add_adapter(parser):
    parser.add_argument(
        "--adapter", required=True, help="module path and factory, PATH.py:factory"
    )
    parser.add_argument(
        "--adapter-state", default=None, help="opaque value handed to the factory"
    )
    parser.add_argument("--output", required=True, help="write-once output path")


def _positive_generation(value):
    try:
        generation = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            "expected a positive ledger generation"
        ) from None
    if generation <= 0:
        raise argparse.ArgumentTypeError("expected a positive ledger generation")
    return generation


def build_parser():
    parser = argparse.ArgumentParser(
        prog="refresh_question_policy.py", description=__doc__.splitlines()[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    observe = commands.add_parser(
        "observe",
        help="write a pricing observation of the approved source, rates as typed",
    )
    observe.add_argument("--model", required=True, help="the active model identifier")
    observe.add_argument(
        "--input-usd-per-million",
        required=True,
        help="input rate as printed on the page, six decimals, e.g. 0.750000",
    )
    observe.add_argument(
        "--output-usd-per-million",
        required=True,
        help="output rate as printed on the page, six decimals, e.g. 3.750000",
    )
    observe.add_argument("--location", default=LOCATION)
    observe.add_argument("--service-tier", default=SERVICE_TIER)
    observe.add_argument("--currency", default=CURRENCY)
    observe.add_argument(
        "--grant",
        required=True,
        help="renewal grant; observe writes to the bucket and answers to it",
    )
    observe.add_argument(
        "--receipt", required=True, help="write-once receipt path for this run"
    )
    observe.add_argument(
        "--dry-run",
        action="store_true",
        help="render the evidence object write, send nothing",
    )
    _add_common(observe)
    _add_adapter(observe)
    renew = commands.add_parser(
        "renew", help="activate an observed policy on the same image"
    )
    for flag in ("--release-index", "--observation", "--grant"):
        renew.add_argument(
            flag,
            required=True,
            help="local JSON path or object:gs://<bucket>/<name>#<generation>",
        )
    _add_common(renew)
    renew.add_argument(
        "--dry-run",
        action="store_true",
        help="verify every input and render every write, send nothing",
    )
    renew.add_argument(
        "--expected-ledger-generation",
        type=_positive_generation,
        help="require the dry plan ledger generation and unchanged active pricing",
    )
    _add_adapter(renew)
    unattended = commands.add_parser(
        "renew-unattended",
        help="renew on the proven rates a hand written unattended grant names",
    )
    for flag in ("--release-index", "--grant"):
        unattended.add_argument(
            flag,
            required=True,
            help="local JSON path or object:gs://<bucket>/<name>#<generation>",
        )
    _add_common(unattended)
    _add_adapter(unattended)
    extend = commands.add_parser(
        "extend-expiry",
        help="activate the granted policy with the extended expiry, nothing else changed",
    )
    for flag in ("--release-index", "--grant"):
        extend.add_argument(
            flag,
            required=True,
            help="local JSON path or object:gs://<bucket>/<name>#<generation>",
        )
    _add_common(extend)
    extend.add_argument(
        "--dry-run",
        action="store_true",
        help="verify every input and render every write, send nothing",
    )
    _add_adapter(extend)
    staged = commands.add_parser(
        "stage-unattended",
        help="check a release index or an unattended grant as the job will, "
        "then write it where the job reads it; the index first, then the grant",
    )
    which = staged.add_mutually_exclusive_group(required=True)
    for flag in ("--release-index", "--grant"):
        which.add_argument(flag, default=None, help="local JSON path")
    _add_common(staged)
    staged.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check and render the write, send nothing",
    )
    _add_adapter(staged)
    return parser


def _clients_for(args, clients):
    if clients is None:
        clients = release._load_adapter(args.adapter, args.adapter_state)
    _require("bytes" in clients, "adapter_invalid")
    manifest_path = (
        Path(args.resources)
        if args.resources
        else _HERE.parent / "resource_manifest.json"
    )
    manifest_sha256 = args.resources_sha256 or release.APPROVED_RESOURCE_MANIFEST_SHA256
    clients["manifest"] = {
        "value": resource_guard.load_resource_manifest(
            manifest_path, expected_sha256=manifest_sha256
        ),
        "sha256": manifest_sha256,
    }
    return clients


def main(argv=None, *, clients=None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        release._require_absent(args.output)
        if getattr(args, "receipt", None) is not None:
            release._require_absent(args.receipt)
        clients = _clients_for(args, clients)
        if args.command == "observe":
            code, summary = _observe(args, clients)
        elif args.command == "renew-unattended":
            code, summary = _renew_unattended(args, clients)
        elif args.command == "extend-expiry":
            code, summary = _extend_expiry(args, clients)
        elif args.command == "stage-unattended":
            code, summary = _stage_unattended(args, clients)
        else:
            code, summary = _renew(args, clients)
    except ValueError as error:
        summary = {"state": "refused", "error": str(error), "output": str(args.output)}
        code = 1
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        summary = {
            "state": "refused",
            "error": f"{type(error).__name__}: {error}",
            "output": str(args.output),
        }
        code = 1
    print(json.dumps(summary, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
