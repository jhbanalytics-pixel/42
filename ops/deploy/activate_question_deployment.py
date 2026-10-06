"""Guarded producer of the activation receipt for a new app image.

The receipt release.py plan verifies claims three things no read can make
true: a paused empty queue, a binding object for the new deployment digest and
a ledger that activates it. This command makes them true in that order, each
step through resource_guard.assert_allowed on the exact resource, then writes
the receipt from readbacks. The binding and every digest come from the release
module's own functions, never re-derived here. ``--dry-run`` renders every
mutation request and writes nothing.
"""

import argparse
import copy
import json
import sys
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
for _entry in (str(_ROOT / "engine"), str(_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from src.analysis.open_intelligence.general_question_control import (  # noqa: E402
    QuestionStoreError,
)
from src.analysis.open_intelligence.general_question_deployment import (  # noqa: E402
    validate_question_deployment,
)

from ops.deploy import release, resource_guard  # noqa: E402

BINDING_CONTRACT = "general_question_deployment_v1"
_HEX40 = release._HEX40
_HEX64 = release._HEX64


def _hex(value, pattern, code):
    release._require(isinstance(value, str) and pattern.fullmatch(value), code)
    return value


def build_binding(
    *,
    revision_name,
    lp_commit,
    image_digest,
    engine_bundle_digest,
    sdk_version,
    policy_digest,
):
    """The binding exactly as the renewal controller and the reference tests build it."""
    binding = {
        "contract_version": BINDING_CONTRACT,
        "project": release.PROJECT,
        "region": release.REGION,
        "service_name": release.SERVICE_NAME,
        "revision_name": revision_name,
        "service_account_email": release.IDENTITY,
        "lp_commit": lp_commit,
        "engine_bundle_digest": engine_bundle_digest,
        "canonical_service_audience": release.AUDIENCE,
        "sdk_version": sdk_version,
        "image_digest": image_digest,
        "worker_path": release.WORKER_PATH,
        "policy_digest": policy_digest,
    }
    binding["deployment_digest"] = release.canonical_digest(binding)
    try:
        return validate_question_deployment(binding)
    except QuestionStoreError:
        release._refuse("binding_invalid")


def build_block(build, lp_commit):
    """Reduce the build record to the fields the activation verifier reads."""
    try:
        release._require(build["status"] == "SUCCESS", "build_not_successful")
        images = build["results"]["images"]
        release._require(len(images) == 1, "build_invalid")
        name = images[0]["name"]
        digest = images[0]["digest"]
        release._require(
            name == release.APP_IMAGE_NAME + ":" + lp_commit,
            "build_image_name_mismatch",
        )
        release._require(
            isinstance(digest, str)
            and digest.startswith("sha256:")
            and _HEX64.fullmatch(digest[len("sha256:") :]),
            "build_invalid",
        )
        steps = [
            {
                "id": step["id"],
                "status": step["status"],
                "args": [str(item) for item in step.get("args", [])],
            }
            for step in build["steps"]
        ]
    except (KeyError, TypeError, AttributeError):
        release._refuse("build_invalid")
    block = {
        "status": "SUCCESS",
        "results": {"images": [{"name": name, "digest": digest}]},
        "steps": steps,
    }
    return block, digest[len("sha256:") :]


def activated_ledger(control, binding):
    activated = copy.deepcopy(control)
    bound = activated["bindings"].setdefault(
        binding["deployment_digest"], binding["policy_digest"]
    )
    release._require(bound == binding["policy_digest"], "ledger_binding_conflict")
    activated["active_deployment_digest"] = binding["deployment_digest"]
    release._validate_ledger(activated)
    return activated


def _receipt(binding, policy, reviewed, build, queue_after, control_after):
    return {
        "state": release.ACTIVATION_STATE,
        "binding": binding,
        "policy": policy,
        "reviewed_inputs": reviewed,
        "queue_after": queue_after,
        "build": build,
        "control_after": control_after,
    }


def _render(clients, call, **kwargs):
    native = clients.get("_native")
    return native.render(call, **kwargs) if native is not None else None


def _run(args, clients, manifest, trace):
    reviewed = {
        "lp_commit": _hex(args.lp_commit, _HEX40, "reviewed_inputs_invalid"),
        "engine_commit": _hex(args.engine_commit, _HEX40, "reviewed_inputs_invalid"),
        "engine_bundle_digest": _hex(
            args.engine_bundle_digest, _HEX64, "reviewed_inputs_invalid"
        ),
        "gate_sha256": _hex(args.gate_sha256, _HEX64, "reviewed_inputs_invalid"),
    }
    build_record, _raw = release._read_json(args.build, "build_invalid")
    build, image_digest = build_block(build_record, reviewed["lp_commit"])
    binding = build_binding(
        revision_name=args.revision_name,
        lp_commit=reviewed["lp_commit"],
        image_digest=image_digest,
        engine_bundle_digest=reviewed["engine_bundle_digest"],
        sdk_version=args.sdk_version,
        policy_digest=_hex(args.policy_digest, _HEX64, "policy_digest_invalid"),
    )
    if not args.dry_run:
        release._require_absent(args.output)

    # Guarded reads: service, queue, every task, the validated ledger, the policy.
    _before, native = release._capture_before(clients, manifest)
    policy = release._resolve_policy(clients, binding)
    generation = native["ledger"]["generation"]
    control = native["ledger"]["value"]
    activated = activated_ledger(control, binding)
    projected = _receipt(
        binding, policy, reviewed, build, {"state": "PAUSED"}, activated
    )
    release.verify_activation(projected)
    tag = release.activation_tag(projected)
    binding_name = (
        release.OBJECT_PREFIX
        + f"deployments/{binding['deployment_digest']}/binding.json"
    )
    binding_raw = release.canonical_bytes(binding)
    ledger_raw = release.canonical_bytes(activated)
    existing = clients["objects"].read(binding_name)
    if existing is not None:
        release._require(bytes(existing["raw"]) == binding_raw, "binding_conflict")
    release._require(not native["tasks"], "queue_not_empty")
    refreshed = control == activated

    if args.dry_run:
        requests = [
            {
                "call": "pause_queue",
                "resource": release.QUEUE_RESOURCE,
                "action": "write",
                "queue": release.QUEUE_API_NAME,
            },
            {
                "call": "create_object",
                "resource": release.BUCKET_RESOURCE,
                "action": "write",
                "object": binding_name,
                "if_generation_match": 0,
                "body": binding,
                "sha256": release._sha256(binding_raw),
                "bytes": len(binding_raw),
                "skipped": existing is not None,
            },
            {
                "call": "create_object",
                "resource": release.BUCKET_RESOURCE,
                "action": "write",
                "object": release.LEDGER_OBJECT,
                "if_generation_match": int(generation),
                "sha256": release._sha256(ledger_raw),
                "bytes": len(ledger_raw),
                "skipped": refreshed,
            },
        ]
        renderings = (
            _render(clients, "pause_queue", name=release.QUEUE_API_NAME),
            _render(
                clients,
                "create_object",
                name=binding_name,
                raw=binding_raw,
                if_generation_match=0,
            ),
            _render(
                clients,
                "create_object",
                name=release.LEDGER_OBJECT,
                raw=ledger_raw,
                if_generation_match=int(generation),
            ),
        )
        for item, rendered in zip(requests, renderings, strict=True):
            if rendered is not None:
                item["native"] = rendered
        return 0, {
            "state": "dry_run",
            "binding": binding,
            "tag": tag,
            "ledger_generation": generation,
            "queue_state": native["queue"].get("state"),
            "requests": requests,
        }

    # 1. Pause the queue, read back PAUSED with zero tasks.
    release._guard(manifest, release.QUEUE_RESOURCE, "write")
    clients["tasks"].pause(release.QUEUE_API_NAME)
    trace["progress"].append("queue_paused")
    queue_after = clients["tasks"].get_queue(release.QUEUE_API_NAME)
    release._require(
        queue_after is not None and queue_after.get("state") == "PAUSED",
        "queue_not_paused",
    )
    release._require(not release._listed_tasks(clients), "queue_not_empty")

    # 2. Binding object, create-only on generation 0.
    if existing is None:
        release._guard(manifest, release.BUCKET_RESOURCE, "write")
        try:
            written = clients["objects"].create(
                binding_name, binding_raw, if_generation_match=0
            )
        except Exception as error:
            trace["detail"] = f"{type(error).__name__}: {error}"
            release._refuse("binding_conflict")
        readback = clients["objects"].read(
            binding_name, generation=str(written["generation"])
        )
        release._require(
            readback is not None and bytes(readback["raw"]) == binding_raw,
            "binding_readback_mismatch",
        )
        binding_object = {"action": "created", "generation": str(written["generation"])}
        trace["progress"].append("binding_object_created")
    else:
        binding_object = {"action": "reused", "generation": str(existing["generation"])}
        trace["progress"].append("binding_object_reused")
    binding_object.update(
        {"object": binding_name, "sha256": release._sha256(binding_raw)}
    )

    # 3. Ledger refresh by compare and swap on the generation read; no retry.
    if refreshed:
        ledger_step = {"action": "already_refreshed", "generation": generation}
    else:
        release._guard(manifest, release.BUCKET_RESOURCE, "write")
        try:
            written = clients["objects"].create(
                release.LEDGER_OBJECT, ledger_raw, if_generation_match=int(generation)
            )
        except Exception as error:
            trace["detail"] = f"{type(error).__name__}: {error}"
            release._refuse("ledger_moved")
        ledger_step = {
            "action": "written",
            "if_generation_match": int(generation),
            "generation": str(written["generation"]),
        }
        trace["progress"].append("ledger_written")
    ledger_step.update(
        {"object": release.LEDGER_OBJECT, "sha256": release._sha256(ledger_raw)}
    )

    # 4. Read the ledger back.
    final = release._read_ledger(clients)
    release._require(final["value"] == activated, "ledger_readback_mismatch")
    release._require(
        final["generation"] == ledger_step["generation"], "ledger_readback_mismatch"
    )

    # 5. The receipt, from readbacks, checked by the same verifier plan runs.
    receipt = _receipt(binding, policy, reviewed, build, queue_after, final["value"])
    receipt["chain"] = {
        "created_at": release._stamp(clients["clock"].now()),
        "tag": tag,
        "ledger_generation_before": generation,
        "queue": {"action": "paused", "state": queue_after.get("state")},
        "binding_object": binding_object,
        "ledger": ledger_step,
    }
    native_adapter = clients.get("_native")
    if native_adapter is not None:
        receipt["native_record"] = copy.deepcopy(native_adapter.record)
    release.verify_activation(receipt)
    release._write_once(args.output, receipt)
    return 0, {
        "state": release.ACTIVATION_STATE,
        "output": str(args.output),
        "deployment_digest": binding["deployment_digest"],
        "tag": tag,
        "ledger_generation": final["generation"],
    }


def build_parser():
    parser = argparse.ArgumentParser(
        prog="activate_question_deployment.py",
        description=__doc__.splitlines()[0],
    )
    parser.add_argument("--build", required=True, help="app image build record")
    parser.add_argument("--lp-commit", required=True)
    parser.add_argument("--engine-commit", required=True)
    parser.add_argument("--engine-bundle-digest", required=True)
    parser.add_argument("--gate-sha256", required=True)
    parser.add_argument("--policy-digest", required=True)
    parser.add_argument("--revision-name", required=True)
    parser.add_argument("--sdk-version", required=True)
    parser.add_argument(
        "--adapter", required=True, help="module path and factory, PATH.py:factory"
    )
    parser.add_argument(
        "--adapter-state", default=None, help="opaque value handed to the factory"
    )
    parser.add_argument(
        "--resources", default=None, help="approved resource manifest path"
    )
    parser.add_argument(
        "--resources-sha256",
        default=None,
        help="independently approved manifest digest",
    )
    parser.add_argument("--output", required=True, help="write-once receipt path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render every mutation request, write nothing",
    )
    return parser


def execute(argv, *, clients=None) -> int:
    args = build_parser().parse_args(argv)
    trace = {"progress": []}
    try:
        manifest_path = (
            Path(args.resources)
            if args.resources
            else _HERE.parent / "resource_manifest.json"
        )
        manifest_sha256 = (
            args.resources_sha256 or release.APPROVED_RESOURCE_MANIFEST_SHA256
        )
        manifest = resource_guard.load_resource_manifest(
            manifest_path, expected_sha256=manifest_sha256
        )
        if clients is None:
            clients = release._load_adapter(args.adapter, args.adapter_state)
        code, summary = _run(args, clients, manifest, trace)
    except ValueError as error:
        summary = {"state": "refused", "error": str(error)}
        code = 1
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        summary = {"state": "refused", "error": f"{type(error).__name__}: {error}"}
        code = 1
    if code != 0:
        summary["output"] = str(args.output)
        summary["progress"] = trace["progress"]
        if "detail" in trace:
            summary["detail"] = trace["detail"]
    print(json.dumps(summary, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(execute(sys.argv[1:]))
