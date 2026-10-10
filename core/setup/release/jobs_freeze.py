"""FreezeJobs (W8-REL-B v2.1 section 3.5, JB-02): bind the jobs image digest two ways before anything is updated.

    freeze_jobs(bound, reader, build_id, now=None) -> {"image_digest", "manifest_sha256"}, or so.Stop

The build result (the Cloud Build record of the build the paste started) and the registry digest of the attempt numbered tag
are separate native reads. They must both be well formed and must agree. The expected tag and commit come from the bindings
(the target commit and the release id), never from the build record, so a record of another attempt or another commit is
refused. The registry is read only after the build record has given a digest, so the registry is never the only source of one.
The manifest is created once, with its sidecar, in the form services_only.freeze writes: image.digest equals image.registry_digest.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from core.setup import deploy_jobs as dj
from core.setup.release import services_only as so
from core.setup.release.services_only import Stop, require

SCHEMA_VERSION = so.SCHEMA_VERSION
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def expected_tag(bound):
    """(tag name, attempt) from the bound target commit and release id; the release id must name the target."""
    target, rid = bound.get("target"), bound.get("release_id")
    require(isinstance(target, str) and COMMIT.match(target) and isinstance(rid, str) and so.RELEASE_ID.match(rid)
            and rid[4:11] == target[:7], "BINDINGS", "The release id does not name the target commit")
    attempt = int(rid[-2:])
    require(1 <= attempt <= dj.MAX_ATTEMPT, "BINDINGS", f"The release id names attempt {attempt:02d}, outside 01 to {dj.MAX_ATTEMPT:02d}")
    return dj.image_tag(target, attempt), attempt


def check_build_record(bound, build, build_id, name, tag):
    require(isinstance(build, dict) and build.get("id") == build_id, "BUILD", "The build record is not the build the paste started")
    require(build.get("status") == "SUCCESS", "BUILD", "The jobs build did not succeed")
    account = f"projects/{so.PROJECT}/serviceAccounts/" + str(bound.get("buildServiceAccount", "")).split("serviceAccounts/")[-1]
    require(build.get("serviceAccount") == account, "BUILD", "The build ran as another service account")
    steps = build.get("steps")
    require(isinstance(steps, list) and len(steps) == 1 and isinstance(steps[0], dict), "BUILD", "The build record holds no single docker step")
    args = steps[0].get("args", [])
    require(isinstance(args, list), "BUILD", "The build step has no argument list")
    require(any(a == f"GIT_SHA={bound['target']}" for a in args), "BUILD", "The build was not made from the bound commit")
    require(any(args[i] == "-t" and args[i + 1] == name for i in range(len(args) - 1)), "BUILD", "The build produced another image tag")
    source = build.get("source")
    stored = source.get("storageSource") if isinstance(source, dict) else None
    require(isinstance(stored, dict) and stored.get("object") == dj.source_object(tag), "BUILD",
            "The build did not read the source object of this attempt's tag")


def build_digest(build, name):
    found = [i for i in (build.get("results") or {}).get("images", []) if isinstance(i, dict) and i.get("name") == name]
    require(len(found) == 1 and DIGEST.match(str(found[0].get("digest", ""))), "DIGEST",
            "The build result holds no well formed digest for the attempt's image tag")
    return found[0]["digest"]


def freeze_jobs(bound, reader, build_id, now=None):
    tag, attempt = expected_tag(bound)
    name = f"{dj.IMAGE}:{tag}"
    root = Path(bound["releaseDir"])
    path, sidecar = root / "release-manifest.json", root / "release-manifest.sha256"
    require(not path.exists() and not sidecar.exists(), "MANIFEST_HASH", "The manifest already exists; FreezeJobs does not overwrite it")
    build = reader.build(build_id)
    check_build_record(bound, build, build_id, name, tag)
    digest = build_digest(build, name)
    registry = reader.registry_digest(name)
    require(isinstance(registry, str) and DIGEST.match(registry), "DIGEST", "The registry holds no well formed digest for the image tag")
    require(registry == digest, "DIGEST", "The build result and the registry disagree about the image digest")
    manifest = {"schema_version": SCHEMA_VERSION, "kind": "jobs-release-manifest",
                "source": {"commit": bound["target"], "attempt": attempt, "release_id": bound["release_id"]},
                "image": {"repository": dj.IMAGE, "tag": tag, "digest": digest, "registry_digest": registry,
                          "reference": f"{dj.IMAGE}@{digest}", "build_id": build_id},
                "frozen_utc": (now() if callable(now) else now or dt.datetime.now(dt.timezone.utc)).isoformat()}
    manifest["manifest_sha256"] = so.manifest_hash(manifest)
    root.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as out:
        out.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    with sidecar.open("x", encoding="utf-8") as out:
        out.write(manifest["manifest_sha256"] + "\n")
    return {"image_digest": digest, "manifest_sha256": manifest["manifest_sha256"]}
