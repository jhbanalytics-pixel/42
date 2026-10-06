"""Produce the shared release index for one staging release.

``build`` runs offline against a clean checkout at the release commit. It reads
every tracked blob under the engine, app and ops trees from the git object store
at that commit, so the inventories describe the commit and not the working copy
or its line endings, and writes the tree inventories, the resource manifest and
each reviewed contract as evidence payloads next to a draft index. Everything
else in the index comes from receipts the release left behind: the Cloud Build
receipts name the images and the build that pushed the app image, the activation
receipt names the binding, the policy and the objects it wrote, the release
result proves the binding was routed, and the ledger copy is the source the
release was planned from.

``publish`` uploads each payload once through the adapter, with the resource
guard asserted before every write and ``ifGenerationMatch=0`` on the wire, reads
each object back at the generation it received, and writes the final index with
those bindings under ``evidence_objects``. Both commands refuse an existing
output. Neither runs gcloud, and ``build`` makes no network call at all.

The index proves nothing by itself: ``release.py verify --release-index`` reads
every value back from its native source, and the values this producer computed
and uploaded prove only that the checkout and the uploaded evidence agree.
"""

import argparse
import json
import subprocess
import sys
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
for _entry in (str(_ROOT / "engine"), str(_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from ops.deploy import release, resource_guard  # noqa: E402
from ops.deploy.release import _refuse, _require, _sha256  # noqa: E402

DRAFT_CONTRACT = "42_staging_release_draft_v1"
TREES = {
    "engine_tree": ("engine",),
    "app_tree": ("app",),
    "ops_tree": ("ops", "infra", "cloudbuild.app.publish.yaml"),
}
MANIFEST_TREE_PATH = "ops/deploy/resource_manifest.json"
ENGINE_IMAGE_NAME = release.IMAGE_REGISTRY + "engine"
EVIDENCE_DIRECTORY = "evidence"
_BLOB_MODES = {"100644", "100755"}


# Git object reads


def _git(repo, *args, data=None):
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=data,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        _refuse("git_" + args[0].replace("-", "_") + "_failed")
    return completed.stdout


def _require_checkout(repo, commit):
    head = _git(repo, "rev-parse", "HEAD").decode("ascii", "replace").strip()
    _require(head == commit, "checkout_not_at_commit")
    status = _git(repo, "status", "--porcelain", "--untracked-files=no")
    _require(not status.strip(), "checkout_dirty")


def _tree_entries(repo, commit):
    """Every tracked path at the commit with its blob id; anything that is not a
    plain blob (a symlink, a submodule) refuses, since it has no file bytes."""
    listing = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    entries = {}
    for record in listing.split(b"\0"):
        if not record:
            continue
        header, _, path = record.partition(b"\t")
        mode, kind, blob = header.decode("ascii").split(" ")
        _require(kind == "blob" and mode in _BLOB_MODES, "tree_entry_unsupported")
        entries[path.decode("utf-8")] = blob
    return entries


def _blobs(repo, entries, paths):
    """Read the named paths' blobs in one batched cat-file read."""
    wanted = sorted(set(paths))
    for path in wanted:
        _require(path in entries, "tree_path_missing:" + path)
    if not wanted:
        return {}
    request = "".join(entries[path] + "\n" for path in wanted).encode("ascii")
    output = _git(repo, "cat-file", "--batch", data=request)
    blobs = {}
    offset = 0
    for path in wanted:
        newline = output.index(b"\n", offset)
        blob, kind, size = output[offset:newline].decode("ascii").split(" ")
        _require(blob == entries[path] and kind == "blob", "git_cat_file_failed")
        start = newline + 1
        end = start + int(size)
        blobs[path] = output[start:end]
        _require(output[end : end + 1] == b"\n", "git_cat_file_failed")
        offset = end + 1
    return blobs


def _tree_paths(entries, roots):
    return [
        path
        for path in entries
        if path in roots or any(path.startswith(root + "/") for root in roots)
    ]


def inventory_bytes(blobs, paths):
    """``sha256  path`` per file, sorted by path, one line each, the format the
    engine build's ops-tree-inventory step prints and sums."""
    lines = [f"{_sha256(blobs[path])}  {path}\n" for path in sorted(paths)]
    return "".join(lines).encode("utf-8")


# Receipts


def _read_dict(path, code):
    value, raw = release._read_json(path, code)
    _require(type(value) is dict, code)
    return value, raw


def _build_receipt(path, *, image_name, prefix):
    build, _ = _read_dict(path, prefix + "_invalid")
    name = build.get("name")
    _require(
        isinstance(name, str) and release._BUILD_NAME.fullmatch(name),
        prefix + "_invalid",
    )
    _require(build.get("projectId") == release.PROJECT, prefix + "_project_mismatch")
    _require(build.get("status") == "SUCCESS", prefix + "_not_successful")
    images = (build.get("results") or {}).get("images")
    _require(type(images) is list and len(images) == 1, prefix + "_invalid")
    image = images[0]
    _require(
        type(image) is dict
        and isinstance(image.get("name"), str)
        and image["name"].startswith(image_name + ":"),
        prefix + "_image_name_mismatch",
    )
    digest = image.get("digest")
    _require(
        isinstance(digest, str)
        and digest.startswith("sha256:")
        and release._hex64(digest[len("sha256:") :]),
        prefix + "_invalid",
    )
    revision = ((build.get("source") or {}).get("connectedRepository") or {}).get(
        "revision"
    )
    _require(
        isinstance(revision, str) and release._HEX40.fullmatch(revision),
        prefix + "_invalid",
    )
    return name, digest[len("sha256:") :], revision


def _object_binding_from_chain(name, item, expected_object):
    _require(
        type(item) is dict
        and item.get("object") == expected_object
        and isinstance(item.get("generation"), str)
        and release._GENERATION.fullmatch(item["generation"])
        and release._hex64(item.get("sha256")),
        "activation_chain_invalid",
    )
    return {
        "name": name,
        "uri": "gs://" + release.BUCKET + "/" + item["object"],
        "generation": item["generation"],
        "sha256": item["sha256"],
    }


def _activation_receipt(path, commit, app_digest):
    activation, _ = _read_dict(path, "activation_invalid")
    release.verify_activation(activation)
    binding = activation["binding"]
    _require(binding["lp_commit"] == commit, "activation_commit_mismatch")
    _require(binding["image_digest"] == app_digest, "app_build_image_mismatch")
    chain = activation.get("chain")
    _require(type(chain) is dict, "activation_chain_invalid")
    binding_object = _object_binding_from_chain(
        "deployment_binding",
        chain.get("binding_object"),
        release.BINDING_OBJECT_NAME.format(
            deployment_digest=binding["deployment_digest"]
        ),
    )
    ledger_object = _object_binding_from_chain(
        "activation_ledger", chain.get("ledger"), release.LEDGER_OBJECT
    )
    before = chain.get("ledger_generation_before")
    _require(
        isinstance(before, str) and release._GENERATION.fullmatch(before),
        "activation_chain_invalid",
    )
    # The bytes behind each authority record, reconstructed from the receipt and
    # required to hash to the digest the activation wrote down. The objects
    # themselves are not read here: build makes no request.
    control = activation["control_after"]
    payloads = {
        "authority:deployment_binding": release.canonical_bytes(binding),
        "authority:activation_ledger": release.canonical_bytes(control),
    }
    for reference, record in (
        ("authority:deployment_binding", binding_object),
        ("authority:activation_ledger", ledger_object),
    ):
        _require(
            _sha256(payloads[reference]) == record["sha256"],
            "activation_chain_invalid",
        )
    return activation, [binding_object, ledger_object], before, payloads


def _release_result(path, activation):
    result, _ = _read_dict(path, "release_result_invalid")
    _require(
        result.get("contract_version") == release.RECEIPT_CONTRACT
        and result.get("kind") == "release",
        "release_result_invalid",
    )
    _require(
        result.get("state") == release.ROUTE_VERIFIED_STATE,
        "release_result_unverified",
    )
    _require(
        result.get("binding") == activation["binding"],
        "release_result_binding_mismatch",
    )
    return result


def _ledger_copy(path, activation, generation_before):
    """The ledger as it stood before the activation wrote it.

    That generation is gone from the bucket, so these bytes are pinned as an
    evidence object rather than fetched later. They are not taken on trust: the
    activation's own ledger write must be exactly this ledger with the new
    binding added and the active digest swapped, and that written ledger's
    digest is recorded in the activation chain. A copy of some other ledger, or
    the same ledger reformatted, fails that derivation.
    """
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _refuse("ledger_invalid")
    release._validate_ledger(value)
    _require(value["allowance_id"] in release.LEDGER_OBJECT, "ledger_invalid")
    _require(release.canonical_bytes(value) == raw, "ledger_copy_not_canonical")
    binding = activation["binding"]
    derived = json.loads(raw.decode("utf-8"))
    derived["bindings"][binding["deployment_digest"]] = binding["policy_digest"]
    derived["active_deployment_digest"] = binding["deployment_digest"]
    _require(
        derived == activation["control_after"],
        "ledger_copy_not_the_activation_source",
    )
    return {
        "uri": "gs://" + release.BUCKET + "/" + release.LEDGER_OBJECT,
        "generation": generation_before,
        "sha256": _sha256(raw),
    }, raw


def _served_assets(path):
    if path is None:
        return {}
    assets, _ = _read_dict(path, "served_assets_invalid")
    for name, digest in assets.items():
        _require(
            isinstance(name, str) and name.strip() and release._hex64(digest),
            "served_assets_invalid",
        )
    return assets


def _contracts(specs):
    contracts = {}
    for spec in specs or ():
        name, marker, path = spec.partition("=")
        _require(marker and release._CONTRACT_NAME.fullmatch(name), "contract_invalid")
        _require(path and name not in contracts, "contract_invalid")
        contracts[name] = path
    return contracts


def _payload_file(reference):
    return EVIDENCE_DIRECTORY + "/" + reference.replace(":", "/", 1)


# Build


def _build(args, manifest_sha256):
    output = Path(args.output)
    _require(not output.exists(), "output_exists")
    commit = args.commit
    _require(
        isinstance(commit, str) and release._HEX40.fullmatch(commit), "commit_invalid"
    )
    repo = Path(args.repo).resolve()
    _require_checkout(repo, commit)
    contracts = _contracts(args.contract)
    entries = _tree_entries(repo, commit)
    tree_paths = {name: _tree_paths(entries, roots) for name, roots in TREES.items()}
    wanted = [MANIFEST_TREE_PATH, *contracts.values()]
    for paths in tree_paths.values():
        wanted.extend(paths)
    blobs = _blobs(repo, entries, wanted)
    payloads = {
        name: inventory_bytes(blobs, paths) for name, paths in tree_paths.items()
    }
    payloads["resource_manifest"] = blobs[MANIFEST_TREE_PATH]
    _require(
        _sha256(payloads["resource_manifest"]) == manifest_sha256,
        "resource_manifest_tree_mismatch",
    )
    for name, path in contracts.items():
        payloads["contract:" + name] = blobs[path]

    app_build, app_digest, app_revision = _build_receipt(
        args.app_build, image_name=release.APP_IMAGE_NAME, prefix="app_build"
    )
    _require(app_revision == commit, "app_build_commit_mismatch")
    engine_build, engine_digest, engine_revision = _build_receipt(
        args.engine_build, image_name=ENGINE_IMAGE_NAME, prefix="engine_build"
    )
    _require(engine_revision == commit, "engine_build_commit_mismatch")
    (
        activation,
        raw_authority_objects,
        generation_before,
        authority_payloads,
    ) = _activation_receipt(args.activation, commit, app_digest)
    _release_result(args.release_result, activation)
    source_binding, ledger_raw = _ledger_copy(
        args.ledger_copy, activation, generation_before
    )
    payloads.update(authority_payloads)
    payloads["source_binding"] = ledger_raw
    binding = activation["binding"]
    index = {
        "schema_version": release.RELEASE_INDEX_SCHEMA,
        "repository": release.REPOSITORY,
        "commit": commit,
        "app_build": app_build,
        "engine_build": engine_build,
        "engine_tree_sha256": _sha256(payloads["engine_tree"]),
        "app_tree_sha256": _sha256(payloads["app_tree"]),
        "ops_tree_sha256": _sha256(payloads["ops_tree"]),
        "engine_image": ENGINE_IMAGE_NAME + "@sha256:" + engine_digest,
        "app_image": release.APP_IMAGE_NAME + "@sha256:" + app_digest,
        "resource_manifest_sha256": manifest_sha256,
        "policy_digest": binding["policy_digest"],
        "deployment_digest": binding["deployment_digest"],
        "contract_digests": {
            name: _sha256(payloads["contract:" + name]) for name in contracts
        },
        "raw_authority_objects": raw_authority_objects,
        "evidence_mode": args.evidence_mode,
        "source_binding": source_binding,
        "served_assets": _served_assets(args.served_assets),
    }
    records = {
        reference: {
            "file": _payload_file(reference),
            "sha256": _sha256(raw),
            "object": release.evidence_object_name(commit, reference),
        }
        for reference, raw in payloads.items()
    }
    # The grammar is run over the draft with placeholder generations so a draft
    # that publish could never turn into a valid index refuses here, offline.
    release.validate_release_index_shape(
        {
            **index,
            "evidence_objects": {
                reference: {
                    "uri": "gs://" + release.BUCKET + "/" + item["object"],
                    "generation": "1",
                    "sha256": item["sha256"],
                }
                for reference, item in records.items()
            },
        }
    )
    draft = {"contract_version": DRAFT_CONTRACT, "index": index, "payloads": records}
    output.mkdir(parents=True, exist_ok=False)
    for reference, raw in payloads.items():
        target = output / records[reference]["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as stream:
            stream.write(raw)
    release._write_once(output / "draft.json", draft)
    return 0, {
        "state": "built",
        "commit": commit,
        "payloads": len(payloads),
        "output": str(output),
    }


# Publish


def _load_draft(directory):
    draft, _ = _read_dict(Path(directory) / "draft.json", "draft_invalid")
    _require(draft.get("contract_version") == DRAFT_CONTRACT, "draft_invalid")
    index = draft.get("index")
    records = draft.get("payloads")
    _require(
        type(index) is dict
        and "evidence_objects" not in index
        and type(records) is dict,
        "draft_invalid",
    )
    payloads = {}
    for reference, item in records.items():
        _require(
            type(item) is dict and set(item) == {"file", "sha256", "object"},
            "draft_invalid",
        )
        _require(
            item["object"] == release.evidence_object_name(index["commit"], reference)
            and item["file"] == _payload_file(reference),
            "draft_invalid",
        )
        try:
            raw = (Path(directory) / item["file"]).read_bytes()
        except OSError:
            _refuse("draft_payload_missing")
        _require(_sha256(raw) == item["sha256"], "draft_payload_mismatch")
        payloads[reference] = raw
    return index, payloads


def _upload(clients, manifest, name, raw):
    """One guarded create at generation zero; an identical object that already
    exists is reused at its generation, a different one refuses."""
    release._guard(manifest, release.BUCKET_RESOURCE, "write")
    try:
        created = clients["objects"].create(name, raw, if_generation_match=0)
    except Exception:
        existing = clients["objects"].read(name)
        _require(existing is not None, "evidence_upload_failed")
        _require(bytes(existing["raw"]) == raw, "evidence_object_conflict")
        return str(existing["generation"])
    generation = str(created["generation"])
    stored = clients["objects"].read(name, generation=generation)
    _require(
        stored is not None and bytes(stored["raw"]) == raw, "evidence_readback_mismatch"
    )
    return generation


def _publish(args, clients, manifest):
    release._require_absent(args.output)
    index, payloads = _load_draft(args.draft)
    evidence = {}
    for reference in sorted(payloads):
        raw = payloads[reference]
        name = release.evidence_object_name(index["commit"], reference)
        generation = _upload(clients, manifest, name, raw)
        evidence[reference] = {
            "uri": "gs://" + release.BUCKET + "/" + name,
            "generation": generation,
            "sha256": _sha256(raw),
        }
    final = {**index, "evidence_objects": evidence}
    release.validate_release_index_shape(final)
    release._write_once(args.output, final)
    return 0, {
        "state": "published",
        "commit": index["commit"],
        "evidence_objects": len(evidence),
        "output": str(args.output),
    }


# CLI


def build_parser():
    parser = argparse.ArgumentParser(
        prog="release_index.py", description=__doc__.splitlines()[0]
    )
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser(
        "build", help="compute the draft index and evidence payloads offline"
    )
    build.add_argument("--repo", default=str(_ROOT), help="checkout to read")
    build.add_argument("--commit", required=True, help="release commit, forty hex")
    build.add_argument("--app-build", required=True, help="app Cloud Build receipt")
    build.add_argument(
        "--engine-build", required=True, help="engine Cloud Build receipt"
    )
    build.add_argument("--activation", required=True, help="activation receipt")
    build.add_argument(
        "--release-result", required=True, help="verified release apply receipt"
    )
    build.add_argument(
        "--ledger-copy",
        required=True,
        help="ledger bytes as read before the activation wrote it",
    )
    build.add_argument(
        "--contract",
        action="append",
        default=None,
        metavar="NAME=PATH",
        help="reviewed contract file, repo relative; repeatable",
    )
    build.add_argument(
        "--served-assets",
        default=None,
        help="JSON map of served asset path to sha256; omitted means none",
    )
    build.add_argument(
        "--evidence-mode", required=True, choices=("retained_baseline", "daily_capture")
    )
    build.add_argument("--output", required=True, help="new directory to write")
    publish = commands.add_parser(
        "publish", help="upload the evidence payloads and write the final index"
    )
    publish.add_argument("--draft", required=True, help="directory build wrote")
    publish.add_argument("--adapter", required=True, help="PATH.py:factory")
    publish.add_argument("--adapter-state", default=None)
    publish.add_argument("--output", required=True, help="write-once index path")
    for command in (build, publish):
        command.add_argument("--resources", default=None)
        command.add_argument("--resources-sha256", default=None)
    return parser


def execute(argv, *, clients=None) -> int:
    args = build_parser().parse_args(argv)
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
        if args.command == "build":
            code, summary = _build(args, manifest_sha256)
        else:
            if clients is None:
                clients = release._load_adapter(args.adapter, args.adapter_state)
            code, summary = _publish(args, clients, manifest)
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
    sys.exit(execute(sys.argv[1:]))
