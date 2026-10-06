import argparse
import copy
import json
import sys
import traceback
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from ops.deploy import refresh_question_policy as refresh
from ops.deploy import release


CONTRACT_VERSION = "42_pricing_activation_finish_v1"
_SERVICE_FIELDS = (
    "name",
    "etag",
    "generation",
    "observedGeneration",
    "reconciling",
    "terminalCondition",
    "latestCreatedRevision",
    "latestReadyRevision",
    "template",
    "traffic",
    "trafficStatuses",
)


def _require(condition, code):
    refresh._require(condition, code)


def _utc(value, code):
    _require(isinstance(value, str), code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        refresh._refuse(code)
    _require(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), code)
    return parsed.astimezone(UTC)


def _deadline(before_state, policy, now):
    before_at = _utc(before_state.get("at"), "before_state_invalid")
    verified_at = _utc(
        policy.get("pricing", {}).get("verified_at"), "candidate_policy_invalid"
    )
    _require(before_at <= now, "before_state_future")
    _require(verified_at <= now, "candidate_future")
    _require(before_at.date() == verified_at.date(), "candidate_day_mismatch")
    return datetime.combine(before_at.date(), time(20, 0), tzinfo=UTC), verified_at


def _read_candidate_object(
    clients, receipt, name, value, code, *, expected_sha256=None
):
    matches = [
        item
        for item in receipt.get("mutations", [])
        if isinstance(item, dict) and item.get("object") == name
    ]
    _require(len(matches) == 1, code)
    mutation = matches[0]
    generation = mutation.get("generation")
    _require(
        isinstance(generation, str)
        and generation.isdecimal()
        and int(generation) > 0,
        code,
    )
    stored = clients["objects"].read(name, generation=generation)
    _require(stored is not None and str(stored.get("generation")) == generation, code)
    raw = bytes(stored.get("raw", b""))
    digest = release._sha256(raw)
    _require(
        mutation.get("action") == "create_immutable_artifact"
        and mutation.get("sha256") == digest
        and (value is None or raw == release.canonical_bytes(value))
        and (expected_sha256 is None or digest == expected_sha256),
        code,
    )
    return generation, digest


def _same_service(left, right):
    return isinstance(left, dict) and isinstance(right, dict) and all(
        left.get(field) == right.get(field) for field in _SERVICE_FIELDS
    )


def _traffic_is(service, binding, tag, code):
    targets = release.traffic_targets(binding, tag)
    statuses = [
        {key: item.get(key) for key in targets[0]}
        for item in (service.get("trafficStatuses") or [])
    ]
    _require(service.get("traffic") == targets and statuses == targets, code)


def _read_operation(clients, name):
    run = clients["run"]
    adapter = getattr(run, "adapter", None)
    _require(adapter is not None, "operation_reader_unavailable")
    try:
        status, operation = adapter.perform(
            "read_operation",
            name=name,
            timeout_seconds=30.0,
        )
        operation = adapter.require_ok("read_operation", status, operation)
    except Exception as error:
        refresh._refuse(
            f"operation_unavailable:{type(error).__name__}:{error}"
        )
    return operation


def _validate_inputs(receipt, before_state, operation_name, clients):
    _require(type(receipt) is dict, "receipt_invalid")
    _require(
        receipt.get("contract_version") == refresh.RECEIPT_CONTRACT
        and receipt.get("command") == "renew-unattended"
        and receipt.get("state") == "failed"
        and receipt.get("deployed_revision") is None
        and receipt.get("ledger_moved") is False
        and receipt.get("operations") == {},
        "receipt_invalid",
    )
    _require(
        isinstance(receipt.get("error"), str)
        and "read_operation" in receipt["error"]
        and operation_name in receipt["error"],
        "receipt_operation_mismatch",
    )
    _require(type(before_state) is dict, "before_state_invalid")
    _require(
        type(before_state.get("ledger")) is dict
        and type(before_state.get("binding")) is dict
        and type(before_state.get("policy")) is dict
        and type(before_state.get("service")) is dict,
        "before_state_invalid",
    )

    policy = refresh.validate_question_policy(receipt.get("policy"))
    binding = refresh.validate_question_deployment(receipt.get("binding"))
    observation = receipt.get("observation")
    _require(type(observation) is dict, "receipt_invalid")
    _require(
        observation.get("contract_version") == refresh.OBSERVATION_CONTRACT
        and observation.get("observed_at") == policy["pricing"]["verified_at"]
        and observation.get("model") == policy["model"]
        and observation.get("input_usd_per_million")
        == policy["pricing"]["input_usd_per_million"]
        and observation.get("output_usd_per_million")
        == policy["pricing"]["output_usd_per_million"]
        and observation.get("source_sha256")
        == receipt.get("observation_source_sha256"),
        "receipt_observation_mismatch",
    )
    _require(receipt.get("rate_changed") is False, "receipt_policy_changed")
    source_name = refresh.EVIDENCE_PREFIX + receipt["observation_source_sha256"] + ".html"
    source_generation, source_sha256 = _read_candidate_object(
        clients,
        receipt,
        source_name,
        None,
        "candidate_source_object_mismatch",
        expected_sha256=receipt["observation_source_sha256"],
    )
    _require(
        observation.get("source_object")
        == f"object:gs://{release.BUCKET}/{source_name}#{source_generation}",
        "receipt_source_object_mismatch",
    )

    try:
        old_ledger = copy.deepcopy(before_state["ledger"])
        release._validate_ledger(old_ledger)
        old_binding = refresh.validate_question_deployment(before_state["binding"])
        old_policy = refresh.validate_question_policy(before_state["policy"])
    except (KeyError, TypeError, ValueError):
        refresh._refuse("before_state_invalid")

    _require(
        old_ledger["active_deployment_digest"] == old_binding["deployment_digest"]
        and old_ledger["bindings"].get(old_binding["deployment_digest"])
        == old_policy["policy_digest"],
        "before_state_invalid",
    )
    for field in (
        "contract_version",
        "limits",
        "stages",
        "model",
        "expires_at",
        "approval_contract_digest",
        "policy_id",
        "location",
        "project",
        "service_tier",
    ):
        _require(policy[field] == old_policy[field], "candidate_policy_scope_changed")
    expected_policy = copy.deepcopy(old_policy)
    expected_policy["policy_digest"] = policy["policy_digest"]
    expected_policy["pricing"]["verified_at"] = policy["pricing"]["verified_at"]
    _require(policy == expected_policy, "candidate_policy_scope_changed")

    expected_binding = copy.deepcopy(old_binding)
    expected_binding["policy_digest"] = policy["policy_digest"]
    expected_binding["revision_name"] = (
        f"{release.SERVICE_NAME}-p-{policy['policy_digest'][:10]}"
    )
    del expected_binding["deployment_digest"]
    expected_binding["deployment_digest"] = release.canonical_digest(expected_binding)
    _require(binding == expected_binding, "candidate_binding_mismatch")

    policy_name = (
        release.OBJECT_PREFIX
        + f"policies/{policy['policy_digest']}/policy.json"
    )
    binding_name = (
        release.OBJECT_PREFIX
        + f"deployments/{binding['deployment_digest']}/binding.json"
    )
    policy_generation, policy_sha256 = _read_candidate_object(
        clients, receipt, policy_name, policy, "candidate_policy_object_mismatch"
    )
    binding_generation, binding_sha256 = _read_candidate_object(
        clients, receipt, binding_name, binding, "candidate_binding_object_mismatch"
    )
    _require(
        before_state.get("ledger_generation") is not None,
        "before_state_invalid",
    )
    expected_generation = str(before_state["ledger_generation"])
    _require(expected_generation.isdecimal() and int(expected_generation) > 0, "before_state_invalid")
    return {
        "policy": policy,
        "binding": binding,
        "observation": observation,
        "old_ledger": old_ledger,
        "old_binding": old_binding,
        "old_policy": old_policy,
        "expected_generation": expected_generation,
        "source_generation": source_generation,
        "source_sha256": source_sha256,
        "policy_generation": policy_generation,
        "policy_sha256": policy_sha256,
        "binding_generation": binding_generation,
        "binding_sha256": binding_sha256,
    }


def _validate_native_state(receipt, before_state, operation_name, clients, verified):
    operation_prefix = (
        release.SERVICE_API_NAME.rsplit("/services/", 1)[0] + "/operations/"
    )
    _require(
        isinstance(operation_name, str)
        and operation_name.startswith(operation_prefix),
        "operation_identity_mismatch",
    )
    operation = _read_operation(clients, operation_name)
    _require(
        type(operation) is dict
        and operation.get("name") == operation_name
        and operation.get("done") is True
        and operation.get("error") is None
        and type(operation.get("response")) is dict,
        "deployment_operation_unproven",
    )
    operation_service = operation["response"]
    service = clients["run"].get_service(release.SERVICE_API_NAME)
    _require(service is not None, "service_unavailable")
    _require(_same_service(service, operation_service), "service_etag_drift")

    old_binding = verified["old_binding"]
    binding = verified["binding"]
    policy = verified["policy"]
    tag = receipt.get("tag")
    _require(isinstance(tag, str) and tag, "candidate_tag_invalid")
    release.verify_service(before_state["service"], old_binding, tag)
    _traffic_is(before_state["service"], old_binding, tag, "before_traffic_mismatch")
    release.verify_service(service, binding, tag)
    _require(
        service.get("reconciling", False) is False
        and service.get("generation") == service.get("observedGeneration")
        and service.get("latestCreatedRevision")
        == release.SERVICE_API_NAME + "/revisions/" + binding["revision_name"]
        and service.get("latestReadyRevision")
        == release.SERVICE_API_NAME + "/revisions/" + old_binding["revision_name"],
        "service_candidate_mismatch",
    )
    _traffic_is(service, old_binding, tag, "service_traffic_mismatch")

    revision_name = (
        release.SERVICE_API_NAME + "/revisions/" + binding["revision_name"]
    )
    revision = clients["run"].get_revision(revision_name)
    _require(revision is not None, "candidate_revision_unavailable")
    release.verify_revision(revision, binding, tag)
    before_env = {
        item["name"]: item
        for item in before_state["service"]["template"]["containers"][0]["env"]
    }
    expected_env = copy.deepcopy(before_env)
    pointer = expected_env["GENERAL_QUESTION_DEPLOYMENT_DIGEST"]
    _require(
        pointer.get("value") == old_binding["deployment_digest"],
        "before_runtime_environment_mismatch",
    )
    pointer["value"] = binding["deployment_digest"]
    for entries in (
        service["template"]["containers"][0]["env"],
        revision["containers"][0]["env"],
    ):
        _require(
            len(entries) == len(expected_env)
            and {item["name"]: item for item in entries} == expected_env,
            "runtime_environment_mismatch",
        )
    active_conditions = [
        item for item in revision.get("conditions", []) if item.get("type") == "Active"
    ]
    _require(
        len(active_conditions) == 1
        and active_conditions[0].get("state") == "CONDITION_FAILED"
        and active_conditions[0].get("revisionReason") == "RETIRED",
        "candidate_revision_state_mismatch",
    )
    _require(
        release._active_binding(clients, verified["old_ledger"]) == old_binding,
        "active_binding_changed",
    )
    active_policy = refresh._read_active_policy(
        clients, verified["old_ledger"], old_binding
    )
    _require(active_policy == verified["old_policy"], "active_policy_changed")
    ledger = release._read_ledger(clients)
    _require(
        str(ledger["generation"]) == verified["expected_generation"]
        and release.canonical_bytes(ledger["value"])
        == release.canonical_bytes(verified["old_ledger"]),
        "stale_ledger_generation",
    )
    return service, revision, operation_service


def _check_write_deadline(clients, deadline, code):
    now = clients["clock"].now()
    _require(
        isinstance(now, datetime)
        and now.tzinfo is not None
        and now.utcoffset() == timedelta(0),
        "clock_invalid",
    )
    _require(now.astimezone(UTC) < deadline, code)


def finish_pricing_activation(
    *, receipt, before_state, operation_name, clients, execute=False
):
    output = {
        "contract_version": CONTRACT_VERSION,
        "state": "started",
        "error": None,
        "operation": operation_name,
        "ledger_moved": False,
        "mutations": [],
        "operations": {},
        "requests": [],
    }
    writes = refresh._Writes(execute=execute)
    try:
        verified = _validate_inputs(receipt, before_state, operation_name, clients)
        now = clients["clock"].now()
        _require(
            isinstance(now, datetime)
            and now.tzinfo is not None
            and now.utcoffset() == timedelta(0),
            "clock_invalid",
        )
        deadline, verified_at = _deadline(before_state, verified["policy"], now)
        output.update(
            candidate_revision=verified["binding"]["revision_name"],
            candidate_policy_digest=verified["policy"]["policy_digest"],
            candidate_binding_digest=verified["binding"]["deployment_digest"],
            candidate_policy_generation=verified["policy_generation"],
            candidate_binding_generation=verified["binding_generation"],
            candidate_policy_sha256=verified["policy_sha256"],
            candidate_binding_sha256=verified["binding_sha256"],
            expected_ledger_generation=verified["expected_generation"],
            verified_at=verified["policy"]["pricing"]["verified_at"],
            deadline=deadline.isoformat().replace("+00:00", "Z"),
        )
        _require(
            now >= verified_at
            and now - verified_at
            <= refresh.OBSERVATION_VALIDITY - refresh.OBSERVATION_MARGIN,
            "candidate_stale",
        )
        service, _revision, operation_service = _validate_native_state(
            receipt, before_state, operation_name, clients, verified
        )
        manifest = clients["manifest"]["value"]
        ledger = release._read_ledger(clients)
        activated = copy.deepcopy(ledger["value"])
        binding = verified["binding"]
        policy = verified["policy"]
        activated["bindings"][binding["deployment_digest"]] = policy["policy_digest"]
        activated["active_deployment_digest"] = binding["deployment_digest"]
        release._validate_ledger(activated)
        ledger_raw = release.canonical_bytes(activated)
        release._guard(manifest, release.BUCKET_RESOURCE, "write")
        ledger_request = refresh._create_object_request(
            clients,
            release.LEDGER_OBJECT,
            ledger_raw,
            int(verified["expected_generation"]),
        )
        ledger_request["active_deployment_digest"] = binding["deployment_digest"]
        targets = release.traffic_targets(binding, receipt["tag"])
        idempotency_key = release.canonical_digest(
            {
                "operation": operation_name,
                "deployment_digest": binding["deployment_digest"],
                "action": "finish_pricing_activation_traffic",
            }
        )
        traffic_request = {
            "call": "update_traffic",
            "resource": release.SERVICE_RESOURCE,
            "action": "deploy",
            "service": release.SERVICE_API_NAME,
            "traffic": targets,
            "idempotency_key": idempotency_key,
            "etag": service["etag"],
            "native": refresh._render(
                clients,
                "update_traffic",
                service_name=release.SERVICE_API_NAME,
                traffic=targets,
                etag=service["etag"],
            ),
        }
        release._guard(manifest, release.SERVICE_RESOURCE, "deploy")

        ledger_attempted = False

        def write_ledger():
            nonlocal ledger_attempted
            _check_write_deadline(
                clients, deadline, "write_deadline_expired_before_ledger_cas"
            )
            ledger_attempted = True
            return clients["objects"].create(
                release.LEDGER_OBJECT,
                ledger_raw,
                if_generation_match=int(verified["expected_generation"]),
            )

        try:
            written = writes.run(ledger_request, write_ledger)
        except Exception:
            if not ledger_attempted:
                raise
            try:
                after = release._read_ledger(clients)
            except Exception as read_error:
                output.update(
                    state="partial",
                    error=f"ledger_write_outcome_unknown:{type(read_error).__name__}:{read_error}",
                    ledger_moved=None,
                    ledger_status="unproven",
                    requests=writes.requests,
                )
                return output
            if (
                after["generation"] != verified["expected_generation"]
                and after["value"].get("active_deployment_digest")
                == binding["deployment_digest"]
            ):
                output.update(
                    state="partial",
                    error="ledger_cas_response_lost",
                    ledger_moved=True,
                    ledger_generation=after["generation"],
                    ledger_status="activated",
                    requests=writes.requests,
                )
                return output
            raise

        if not execute:
            writes.run(traffic_request, lambda: None)
            output.update(
                state="dry_run",
                requests=writes.requests,
            )
            return output

        output.update(
            state="ledger_activated",
            ledger_moved=True,
            ledger_generation=str(written["generation"]),
            mutations=[
                {
                    "action": "compare_and_swap_ledger",
                    "generation": str(written["generation"]),
                    "if_generation_match": verified["expected_generation"],
                    "sha256": release._sha256(ledger_raw),
                }
            ],
        )
        after_ledger = release._read_ledger(clients)
        _require(
            after_ledger["generation"] == str(written["generation"])
            and after_ledger["value"]["active_deployment_digest"]
            == binding["deployment_digest"]
            and after_ledger["value"]["requests"] == ledger["value"]["requests"],
            "ledger_readback_mismatch",
        )
        service = clients["run"].get_service(release.SERVICE_API_NAME)
        _require(service is not None, "service_unavailable_after_ledger_cas")
        release.verify_service(service, binding, receipt["tag"])
        _require(
            _same_service(service, operation_service),
            "service_etag_drift_after_ledger_cas",
        )

        def write_traffic():
            _check_write_deadline(
                clients, deadline, "traffic_deadline_expired_after_ledger_cas"
            )
            return clients["run"].update_traffic(
                release.SERVICE_API_NAME,
                etag=service["etag"],
                traffic=targets,
                idempotency_key=idempotency_key,
            )

        operation = writes.run(traffic_request, write_traffic)
        output["requests"] = writes.requests
        output["mutations"].append(
            {
                "action": "route_candidate_traffic",
                "revision": binding["revision_name"],
                "etag": traffic_request["etag"],
            }
        )
        route_result = refresh._wait(clients, operation)
        output["operations"]["route_traffic"] = route_result
        _require(
            route_result["state"] == "succeeded",
            "route_unproven"
            if route_result["state"] == "unproven"
            else "route_failed",
        )
        routed_service = clients["run"].get_service(release.SERVICE_API_NAME)
        _require(routed_service is not None, "service_unavailable_after_route")
        release.verify_service(routed_service, binding, receipt["tag"])
        _traffic_is(routed_service, binding, receipt["tag"], "traffic_unverified")
        final_ledger = release._read_ledger(clients)
        _require(
            final_ledger["generation"] == str(written["generation"])
            and final_ledger["value"]["active_deployment_digest"]
            == binding["deployment_digest"]
            and final_ledger["value"]["requests"] == ledger["value"]["requests"],
            "ledger_changed_after_route",
        )
        output.update(state="activated", error=None, requests=writes.requests)
    except ValueError as error:
        if output["ledger_moved"]:
            output.update(state="partial", error=str(error))
        else:
            output.update(state="refused", error=str(error))
    except Exception as error:
        if output["ledger_moved"]:
            output.update(
                state="partial",
                error=f"{type(error).__name__}: {error}",
            )
        else:
            output.update(
                state="refused",
                error=f"{type(error).__name__}: {error}",
            )
    output["requests"] = writes.requests
    if output["state"] == "started":
        output.update(state="refused", error="finish_not_settled")
    return output


def build_parser():
    parser = argparse.ArgumentParser(prog="finish_pricing_activation")
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--before-state", required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--adapter-state", default="owner-gcloud")
    parser.add_argument("--output", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def _read_json(path, code):
    try:
        value = json.loads(Path(path).read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError):
        refresh._refuse(code)
    _require(type(value) is dict, code)
    return value


def main(argv=None, *, clients=None):
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    output_path = Path(args.output)
    try:
        release._require_absent(output_path)
        receipt = _read_json(args.receipt, "receipt_unavailable")
        before_state = _read_json(args.before_state, "before_state_unavailable")
        if clients is None:
            adapter_args = argparse.Namespace(
                adapter=args.adapter,
                adapter_state=args.adapter_state,
                resources=None,
                resources_sha256=release.APPROVED_RESOURCE_MANIFEST_SHA256,
            )
            clients = refresh._clients_for(adapter_args, None)
        result = finish_pricing_activation(
            receipt=receipt,
            before_state=before_state,
            operation_name=args.operation,
            clients=clients,
            execute=args.apply,
        )
    except ValueError as error:
        result = {
            "contract_version": CONTRACT_VERSION,
            "state": "refused",
            "error": str(error),
            "operation": args.operation,
            "ledger_moved": False,
            "mutations": [],
            "operations": {},
            "requests": [],
        }
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        result = {
            "contract_version": CONTRACT_VERSION,
            "state": "refused",
            "error": f"{type(error).__name__}: {error}",
            "operation": args.operation,
            "ledger_moved": False,
            "mutations": [],
            "operations": {},
            "requests": [],
        }
    release._rewrite(output_path, result)
    print(json.dumps(result, sort_keys=True))
    if result["state"] in ("dry_run", "activated"):
        return 0
    return 2 if result["state"] == "partial" else 1


if __name__ == "__main__":
    sys.exit(main())
