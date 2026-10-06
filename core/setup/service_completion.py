import hashlib
import json
import copy
from pathlib import Path
import re


PINNED_OPERATOR_SHA256 = "DBE0A90ACC54B9E093BB9B2430649FB47E4C484F0F2BF51E728CDF6C9915FD3E"
PINNED_HEAD = "abcae2c8b99bfcb257ff33cdfb2551596b47ec53"
BASE_HEAD = "453ec1a3528af054d476700bab7617e7ef5c851d"
ATTEMPT_ID = "453-daytime-abcae2c8-20261001-1034"
EXPECTED_BUILDS = {
    "jobs": {
        "object": "build-source/jobs-abcae2c8b99b.tar.gz",
        "image_tag": "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/jobs:abcae2c8b99b",
    },
    "web": {
        "object": "build-source/f42-web-abcae2c8b99b-459370aa1e9511c4.tar.gz",
        "image_tag": "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/f42-web:abcae2c8b99b",
    },
}
PREPARED_REVISION_PROOF_SHA256 = "0FB55C56325C577FE719BA72CAEF438B02C15A0B6C47F65EB5F8355A7728DED4"
PREPARED_REVISION = "f42-agent-00017-2tx"
PREVIOUS_REVISION = "f42-agent-00016-9gs"
PREPARED_IMAGE = (
    "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/f42-web@"
    "sha256:acaa76b45231201960135326d9a4dff9411ef5f14ab8cfeb9dc360e4d6b45464"
)
PREPARED_REVISION_PROOF = Path(
    r"C:\Users\AlbertMeintjes\dev\42-inputs"
    r"\L1-agent-prepared-specific-readback-abca-20261001.json"
)


class Refusal(RuntimeError):
    pass


def _sha256(data):
    return hashlib.sha256(data).hexdigest().upper()


def _read_prepared_proof(path):
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise Refusal("prepared agent revision proof is unavailable") from exc
    if _sha256(data) != PREPARED_REVISION_PROOF_SHA256:
        raise Refusal("prepared agent revision proof hash changed")
    try:
        proof = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal("prepared agent revision proof is invalid") from exc
    conditions = {row.get("type"): row for row in proof.get("specific_revision_conditions", [])}
    traffic = proof.get("service_traffic") or []
    if (proof.get("service_latest_created", "").rsplit("/", 1)[-1] != PREPARED_REVISION or
            proof.get("specific_revision", "").rsplit("/", 1)[-1] != PREPARED_REVISION or
            proof.get("service_latest_ready", "").rsplit("/", 1)[-1] != PREVIOUS_REVISION or
            len(traffic) != 1 or traffic[0].get("revision", "").rsplit("/", 1)[-1] != PREVIOUS_REVISION or
            traffic[0].get("percent") != 100 or
            proof.get("specific_image") != PREPARED_IMAGE or
            proof.get("specific_service_account") !=
            "f42-agent@ogilvy-trends-v2.iam.gserviceaccount.com" or
            proof.get("specific_provider_version") != {
                "F42_VERSION": PINNED_HEAD[:12], "MODEL_PROVIDER": "gemini",
            } or proof.get("no_new_write") is not True or
            conditions.get("Ready", {}).get("state") != "CONDITION_SUCCEEDED" or
            conditions.get("ContainerReady", {}).get("state") != "CONDITION_SUCCEEDED" or
            conditions.get("Active", {}).get("state") != "CONDITION_FAILED" or
            conditions.get("Active", {}).get("severity") != "INFO"):
        raise Refusal("prepared agent readback does not match the specific Ready revision")
    return proof


def _validate_ledger(path, expected_sha256, proof):
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise Refusal("pinned 1034 ledger is unavailable") from exc
    if _sha256(data) != str(expected_sha256).upper():
        raise Refusal("pinned 1034 ledger hash changed")
    try:
        ledger = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal("pinned 1034 ledger is invalid") from exc
    if (ledger.get("attempt_id") != ATTEMPT_ID or
            ledger.get("release_head") != PINNED_HEAD or ledger.get("base_head") != BASE_HEAD):
        raise Refusal("ledger does not match the pinned 1034 release")
    actions = ledger.get("actions")
    if not isinstance(actions, list):
        raise Refusal("ledger actions are missing")
    jobs = [row for row in actions if row.get("kind") == "job_update"]
    if (len(jobs) != 10 or len({row.get("id") for row in jobs}) != 10 or
            any(row.get("state") != "complete" or not row.get("name") for row in jobs)):
        raise Refusal("all ten existing job updates must be complete")
    by_id = {row.get("id"): row for row in actions}
    allowed_prefixes = ("job:", "source-upload:", "cloud-build:", "service-revision:")
    if any(not str(row.get("id", "")).startswith(allowed_prefixes) for row in actions):
        raise Refusal("ledger contains an unknown action prefix")
    for kind, expected in EXPECTED_BUILDS.items():
        action = by_id.get(f"cloud-build:{kind}")
        marker = f"{ATTEMPT_ID}-{kind}"
        target = {**expected, "attempt_marker": marker}
        observed = action.get("observed", {}) if action else {}
        if (not action or action.get("state") != "complete" or action.get("target") != target or
                any(observed.get(key) != value for key, value in target.items()) or
                not observed.get("build_id") or
                not re.fullmatch(r"sha256:[0-9a-f]{64}", str(observed.get("digest", "")))):
            raise Refusal("both expected source images must have completed build receipts")
    service_actions = [row for row in actions if str(row.get("id", "")).startswith("service-")]
    target = {
        "name": "f42-agent", "image": PREPARED_IMAGE, "f42_version": PINNED_HEAD[:12],
        "model_provider": "gemini",
        "service_account": "f42-agent@ogilvy-trends-v2.iam.gserviceaccount.com",
        "old_revision": PREVIOUS_REVISION,
        "old_traffic": [{"revision": PREVIOUS_REVISION, "percent": 100}],
    }
    pending = by_id.get("service-revision:f42-agent")
    if (len(service_actions) != 1 or not pending or pending.get("kind") != "service_revision" or
            pending.get("name") != "f42-agent" or pending.get("state") != "in_flight" or
            pending.get("readback") is not False or pending.get("target") != target):
        raise Refusal("ledger must contain only the exact in-flight prepared agent revision")
    if (proof.get("service_latest_created", "").rsplit("/", 1)[-1] != PREPARED_REVISION or
            target["old_revision"] != proof.get("service_latest_ready", "").rsplit("/", 1)[-1]):
        raise Refusal("prepared agent readback does not match the in-flight ledger target")


def install(operator, ledger_path, expected_ledger_sha256,
            prepared_revision_proof_path=PREPARED_REVISION_PROOF):
    operator_path = Path(operator.__file__)
    if _sha256(operator_path.read_bytes()) != PINNED_OPERATOR_SHA256:
        raise Refusal("frozen W9 operator hash changed")
    if not callable(getattr(operator, "_apply_service", None)):
        raise Refusal("frozen W9 service update function is missing")
    proof = _read_prepared_proof(prepared_revision_proof_path)
    _validate_ledger(ledger_path, expected_ledger_sha256, proof)
    original_resume = operator._resume_pending

    def apply_service(name, image, ledger, original_raw, version):
        return _apply_service(operator, name, image, ledger, original_raw, version, proof)

    def resume_pending(ledger, jobs, session):
        pending = [row for row in ledger.data["actions"]
                   if row["state"] != "complete" or not row.get("readback")]
        if (len(pending) == 1 and pending[0].get("id") == "service-revision:f42-agent" and
                pending[0].get("state") == "in_flight"):
            action = pending[0]
            current = operator._read_service("f42-agent")
            revision = _verify_specific_revision(
                operator, "f42-agent", current, current, action["target"],
                expected_revision=PREPARED_REVISION, active=False,
            )
            observed = {
                **action["target"], "new_revision": revision["revision"],
                "specific_ready_revision": revision["revision"],
                "latest_ready_alias": revision["latest_ready_alias"],
            }
            ledger.complete(action["id"], action["target"], observed)
        return original_resume(ledger, jobs, session)

    operator._apply_service = apply_service
    operator._resume_pending = resume_pending


def _service_provider(operator, service_spec):
    containers = service_spec.get("containers") or []
    if len(containers) != 1:
        raise operator.Refusal("service intent has an unexpected container count")
    values = {}
    for row in containers[0].get("env", []):
        key = row.get("name")
        if not key or key in values:
            raise operator.Refusal("service environment is duplicated or unnamed")
        values[key] = row.get("value") if "value" in row else row.get("valueSource")
    model = values.get("MODEL_PROVIDER")
    provider = model if model in {"gemini", "vertex"} else ("unset" if model is None else "other")
    return values, provider


def _verify_specific_revision(operator, name, original_raw, service_doc, target,
                              *, expected_revision=None, active):
    safe = operator.safe_service_state(service_doc)
    revision = safe["revision"]
    if (not revision or revision == target["old_revision"] or
            (expected_revision and revision != expected_revision)):
        raise operator.Refusal(f"latest created service revision does not match the prepared revision: {name}")
    expected_traffic = (
        [{"revision": revision, "percent": 100}] if active else target["old_traffic"]
    )
    if (safe["traffic"] != expected_traffic or safe["image"] != target["image"] or
            safe["f42_version"] != target["f42_version"] or
            safe["model_provider"] != target["model_provider"] or
            safe["service_account"] != target["service_account"]):
        raise operator.Refusal(f"service template does not match the revision target: {name}")
    if (operator._project_except(original_raw, "service", name, reference=original_raw) !=
            operator._project_except(service_doc, "service", name, reference=original_raw)):
        raise operator.Refusal(f"service template changed an unapproved setting: {name}")
    revision_doc = operator._gcloud_json([
        "run", "revisions", "describe", revision,
        f"--project={operator.PROJECT}", f"--region={operator.REGION}",
    ])
    revision_name = revision_doc.get("metadata", {}).get("name", "").rsplit("/", 1)[-1]
    revision_status = revision_doc.get("status") or {}
    revision_spec = revision_doc.get("spec") or {}
    _, service_spec = operator._service_config(service_doc)
    service_containers = service_spec.get("containers") or []
    revision_containers = revision_spec.get("containers") or []
    if (revision_name != revision or revision_status.get("imageDigest") != target["image"] or
            len(service_containers) != 1 or len(revision_containers) != 1):
        raise operator.Refusal(f"specific revision name, image, or container shape differs: {name}")
    normalized = copy.deepcopy(revision_spec)
    if "name" not in service_containers[0] and "name" in normalized["containers"][0]:
        normalized["containers"][0].pop("name")
    if normalized != service_spec:
        raise operator.Refusal(f"specific revision environment or TaskSpec differs from service intent: {name}")
    env, provider = _service_provider(operator, revision_spec)
    identity = revision_spec.get("serviceAccountName", revision_spec.get("serviceAccount"))
    if (revision_containers[0].get("image") != target["image"] or
            identity != target["service_account"] or
            env.get("F42_VERSION") != target["f42_version"] or
            provider != target["model_provider"]):
        raise operator.Refusal(f"specific revision image, version, provider, or identity differs: {name}")
    conditions = {}
    for row in revision_status.get("conditions", []):
        kind = row.get("type")
        if not kind or kind in conditions:
            raise operator.Refusal(f"specific revision condition readback is duplicated or unnamed: {name}")
        conditions[kind] = row
    ready = conditions.get("Ready", {})
    container_ready = conditions.get("ContainerReady", {})
    active_condition = conditions.get("Active", {})
    ready_state = ready.get("state", ready.get("status"))
    container_state = container_ready.get("state", container_ready.get("status"))
    active_state = active_condition.get("state", active_condition.get("status"))
    if ready_state not in {"True", "CONDITION_SUCCEEDED"} or container_state not in {
        "True", "CONDITION_SUCCEEDED",
    }:
        raise operator.Refusal(f"specific revision is not Ready and ContainerReady: {name}")
    if active:
        if active_state not in {"True", "CONDITION_SUCCEEDED"}:
            raise operator.Refusal(f"promoted specific revision is not Active: {name}")
    elif active_state not in {"False", "CONDITION_FAILED"} or active_condition.get("severity") != "INFO":
        raise operator.Refusal(f"prepared specific revision is not inactive at zero traffic: {name}")
    return {
        "revision": revision,
        "latest_ready_alias": safe["ready_revision"],
        "specific_ready_revision": revision,
    }


def _apply_service(operator, name, image, ledger, original_raw, version, proof):
    observed_prior = operator.safe_service_state(original_raw)
    expected_provider = {"f42-agent": "gemini", "f42-api": "unset"}.get(name)
    if expected_provider is None:
        raise operator.Refusal(f"service model provider does not match its approved prior value: {name}")
    revision_action = f"service-revision:{name}"
    promotion_action = f"service-promotion:{name}"
    action = ledger.action(revision_action)
    action_state = action.get("state") if action is not None else None
    if action is not None and action_state not in ("in_flight", "complete"):
        raise operator.Refusal(f"service revision ledger state is invalid: {name}")
    if action is not None:
        prior = {
            **observed_prior,
            "model_provider": action["target"]["model_provider"],
            "service_account": action["target"]["service_account"],
            "revision": action["target"]["old_revision"],
            "traffic": action["target"]["old_traffic"],
        }
    else:
        prior = observed_prior
    if prior.get("model_provider") != expected_provider:
        raise operator.Refusal(f"service model provider does not match its approved prior value: {name}")
    target = {
        "name": name, "image": image, "f42_version": version,
        "model_provider": prior["model_provider"], "service_account": prior["service_account"],
        "old_revision": prior["revision"], "old_traffic": prior["traffic"],
    }
    if action is not None and action.get("target") != target:
        raise operator.Refusal(f"service revision ledger target changed: {name}")
    if action is not None:
        if action_state == "in_flight":
            new_revision = None
        elif action_state == "complete":
            observed = action.get("observed")
            new_revision = observed.get("new_revision") if isinstance(observed, dict) else None
            if not isinstance(new_revision, str) or not new_revision.strip():
                raise operator.Refusal(f"completed service revision receipt is invalid: {name}")
        else:
            raise operator.Refusal(f"service revision ledger state is invalid: {name}")
    promotion = ledger.action(promotion_action)
    current = operator._read_service(name)
    safe = operator.safe_service_state(current)
    if action is not None and action_state == "complete" and promotion and promotion["state"] == "complete":
        verified = _verify_specific_revision(
            operator, name, original_raw, current, target,
            expected_revision=new_revision, active=True,
        )
        if safe["traffic"] != promotion["target"]["traffic"]:
            raise operator.Refusal(f"completed service promotion no longer reads back: {name}")
        return
    if action is not None:
        verified = _verify_specific_revision(
            operator, name, original_raw, current, target,
            expected_revision=new_revision or (
                PREPARED_REVISION if name == "f42-agent" and proof else None
            ), active=False,
        )
        new_revision = verified["revision"]
        if action_state == "in_flight":
            observed = {**target, **verified, "new_revision": new_revision}
            ledger.complete(revision_action, target, observed)
        elif action["observed"].get("new_revision") != new_revision:
            raise operator.Refusal(f"created service revision no longer reads back: {name}")
    else:
        ledger.begin(revision_action, target, kind="service_revision", name=name)
        operator._gcloud(operator.update_argv("service", name, image, version=version))
        current = operator._read_service(name)
        verified = _verify_specific_revision(operator, name, original_raw, current, target, active=False)
        new_revision = verified["revision"]
        observed = {**target, **verified, "new_revision": new_revision}
        ledger.complete(revision_action, target, observed)

    promoted_traffic = [{"revision": new_revision, "percent": 100}]
    promote_target = {"name": name, "revision": new_revision, "traffic": promoted_traffic}
    action = ledger.action(promotion_action)
    current = operator._read_service(name)
    safe = operator.safe_service_state(current)
    if action and action["state"] == "complete":
        _verify_specific_revision(
            operator, name, original_raw, current, target,
            expected_revision=new_revision, active=True,
        )
        if safe["traffic"] != promoted_traffic:
            raise operator.Refusal(f"service traffic promotion no longer reads back: {name}")
        return
    if action:
        _verify_specific_revision(
            operator, name, original_raw, current, target,
            expected_revision=new_revision, active=True,
        )
        if safe["traffic"] != promoted_traffic:
            raise operator.Refusal(f"pending service promotion is not complete by readback: {name}")
        ledger.complete(promotion_action, promote_target, promote_target)
        return
    if safe["traffic"] != target["old_traffic"]:
        raise operator.Refusal(f"old service traffic was not preserved before promotion: {name}")
    ledger.begin(promotion_action, promote_target, kind="service_promotion", name=name)
    operator._gcloud(operator.promote_argv(name, new_revision))
    current = operator._read_service(name)
    safe = operator.safe_service_state(current)
    _verify_specific_revision(
        operator, name, original_raw, current, target,
        expected_revision=new_revision, active=True,
    )
    if (safe["revision"] != new_revision or safe["traffic"] != promoted_traffic or
            safe["image"] != image or
            safe["f42_version"] != version or
            safe["model_provider"] != prior["model_provider"] or
            safe["service_account"] != prior["service_account"]):
        raise operator.Refusal(f"new service revision promotion did not read back: {name}")
    ledger.complete(promotion_action, promote_target, promote_target)
