"""FreezeJobs (W8-REL-B v2.1 section 3.5 and JB-02): the build result and the registry digest are two separate native reads
and must agree before a release manifest is written. No cloud access: a fake reader answers both."""
import json

import pytest

from core.setup import deploy_jobs as dj
from core.setup.release import jobs_freeze as jf
from core.setup.release import services_only as so

COMMIT = "5e1c0de9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3"
RID = "rel-5e1c0de-02"
TAG = f"{dj.IMAGE}:{COMMIT[:12]}-02"
DIGEST = "sha256:" + "d" * 64
OTHER_DIGEST = "sha256:" + "e" * 64
DEPLOYER = "f42-deployer@ogilvy-trends-v2.iam.gserviceaccount.com"
BUILD_ID = "b1d-0002"


def record(**over):
    value = {"id": BUILD_ID, "status": "SUCCESS", "serviceAccount": f"projects/ogilvy-trends-v2/serviceAccounts/{DEPLOYER}",
             "steps": [{"name": "gcr.io/cloud-builders/docker",
                        "args": ["build", "-f", "core/setup/jobs.Dockerfile", "--build-arg", f"GIT_SHA={COMMIT}", "-t", TAG, "."]}],
             "results": {"images": [{"name": TAG, "digest": DIGEST}]}}
    value.update(over)
    return value


class FakeReader:
    def __init__(self, build=None, registry=DIGEST):
        self._build = record() if build is None else build
        self.registry = registry
        self.calls = []

    def build(self, build_id):
        self.calls.append(("build", build_id))
        return self._build

    def registry_digest(self, image_tag):
        self.calls.append(("registry", image_tag))
        return self.registry


def bound(tmp_path, **over):
    value = {"release_id": RID, "target": COMMIT, "releaseDir": str(tmp_path / "release"), "buildServiceAccount": DEPLOYER}
    value.update(over)
    return value


def freeze(tmp_path, reader=None, build_id=BUILD_ID, **over):
    reader = reader or FakeReader()
    return reader, jf.freeze_jobs(bound(tmp_path, **over), reader, build_id)


def written(tmp_path):
    folder = tmp_path / "release"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def test_jb02_the_build_result_and_the_registry_agree_and_one_digest_is_frozen_with_its_sidecar(tmp_path):
    reader, result = freeze(tmp_path)
    assert result["image_digest"] == DIGEST
    assert written(tmp_path) == ["release-manifest.json", "release-manifest.sha256"]
    manifest = json.loads((tmp_path / "release" / "release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["image"] == {"repository": dj.IMAGE, "tag": f"{COMMIT[:12]}-02", "digest": DIGEST, "registry_digest": DIGEST,
                                 "reference": f"{dj.IMAGE}@{DIGEST}", "build_id": BUILD_ID}
    assert manifest["source"] == {"commit": COMMIT, "attempt": 2, "release_id": RID}
    assert manifest["manifest_sha256"] == so.manifest_hash(manifest)
    assert (tmp_path / "release" / "release-manifest.sha256").read_text(encoding="utf-8").strip() == manifest["manifest_sha256"]
    assert result["manifest_sha256"] == manifest["manifest_sha256"]
    assert reader.calls == [("build", BUILD_ID), ("registry", TAG)]


@pytest.mark.parametrize("results", [{}, {"images": []}, {"images": [{"name": TAG}]}, {"images": [{"name": TAG, "digest": ""}]},
                                     {"images": [{"name": TAG, "digest": "sha256:abc"}]},
                                     {"images": [{"name": f"{dj.IMAGE}:{COMMIT[:12]}-01", "digest": DIGEST}]},
                                     {"images": [{"name": TAG, "digest": DIGEST}, {"name": TAG, "digest": DIGEST}]}])
def test_jb02_a_build_result_without_a_well_formed_digest_for_this_attempts_tag_is_refused_and_the_registry_is_never_asked(tmp_path, results):
    reader = FakeReader(build=record(results=results))
    with pytest.raises(so.Stop) as stop:
        freeze(tmp_path, reader)
    assert stop.value.code == "DIGEST"
    assert ("registry", TAG) not in reader.calls  # the registry is never the only source of the digest
    assert written(tmp_path) == []


def test_jb02_two_well_formed_digests_that_differ_are_refused(tmp_path):
    reader = FakeReader(registry=OTHER_DIGEST)
    with pytest.raises(so.Stop) as stop:
        freeze(tmp_path, reader)
    assert stop.value.code == "DIGEST" and "disagree" in stop.value.message
    assert written(tmp_path) == []


@pytest.mark.parametrize("registry", [None, "", "sha256:abc", "latest"])
def test_jb02_a_registry_without_a_well_formed_digest_is_refused(tmp_path, registry):
    with pytest.raises(so.Stop) as stop:
        freeze(tmp_path, FakeReader(registry=registry))
    assert stop.value.code == "DIGEST" and "no well formed digest" in stop.value.message
    assert written(tmp_path) == []


@pytest.mark.parametrize("over, why", [
    ({"status": "FAILURE"}, "status"),
    ({"id": "another-build"}, "id"),
    ({"serviceAccount": "projects/ogilvy-trends-v2/serviceAccounts/f42-builder@ogilvy-trends-v2.iam.gserviceaccount.com"}, "account"),
    ({"steps": []}, "steps"),
    ({"steps": [{"name": "docker", "args": ["build", "--build-arg", "GIT_SHA=" + "0" * 40, "-t", TAG, "."]}]}, "other commit"),
    ({"steps": [{"name": "docker", "args": ["build", "--build-arg", f"GIT_SHA={COMMIT}", "-t", f"{dj.IMAGE}:{COMMIT[:12]}-01", "."]}]}, "other tag"),
])
def test_jb02_a_build_that_did_not_succeed_ran_as_another_account_or_built_another_commit_or_tag_is_refused(tmp_path, over, why):
    with pytest.raises(so.Stop) as stop:
        freeze(tmp_path, FakeReader(build=record(**over)))
    assert stop.value.code == "BUILD", why
    assert written(tmp_path) == []


def test_jb02_the_expected_tag_and_commit_come_from_the_bindings_not_from_the_build_record(tmp_path):
    # A record that names another commit and tag consistently is still refused: the bound target and release id decide.
    other = "7" * 40
    other_tag = f"{dj.IMAGE}:{other[:12]}-02"
    build = record(steps=[{"name": "docker", "args": ["build", "--build-arg", f"GIT_SHA={other}", "-t", other_tag, "."]}],
                   results={"images": [{"name": other_tag, "digest": DIGEST}]})
    with pytest.raises(so.Stop):
        freeze(tmp_path, FakeReader(build=build))
    assert written(tmp_path) == []


def test_jb02_a_release_id_that_does_not_name_the_target_commit_is_refused(tmp_path):
    with pytest.raises(so.Stop) as stop:
        freeze(tmp_path, release_id="rel-0000000-02")
    assert stop.value.code == "BINDINGS"
    assert written(tmp_path) == []


def test_jb02_freeze_never_overwrites_a_manifest(tmp_path):
    freeze(tmp_path)
    before = (tmp_path / "release" / "release-manifest.json").read_bytes()
    with pytest.raises(so.Stop) as stop:
        freeze(tmp_path)
    assert stop.value.code == "MANIFEST_HASH"
    assert (tmp_path / "release" / "release-manifest.json").read_bytes() == before


def test_jb02_the_build_result_is_read_before_the_registry_and_each_once(tmp_path):
    reader, _ = freeze(tmp_path)
    assert [c[0] for c in reader.calls] == ["build", "registry"]
