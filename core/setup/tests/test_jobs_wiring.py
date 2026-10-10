"""Release B, the seams between the two halves once they are merged: FreezeJobs bound to the build (JB-02, JB-03), the apply dry run
receipt refused inside BeforeAnyWrite (JS-09) and the durable apply, readback and schema receipt order (JS-04, RB-T3). Offline: a
fake world answers every read, nothing here starts pwsh (the paste is driven in test_jobs_paste.py and test_jobs_final.py)."""
import json
import re

import pytest

from core.setup import deploy_jobs as dj
from core.setup.release import jobs_only as jo
from core.setup.release import jobs_source as js
from core.setup.release import services_only as so
from core.setup.tests import jobs_world as jw
from core.setup.tests import release_world as rw

BUILD_ID = "0b5f3c1e-6a2d-4c7b-9e81-3d5a7f9b1c24"
OTHER_BUILD_ID = "7c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"


def freezing(tmp_path, *, record=None):
    """A world at the moment the build has finished: the build record and the registry tag exist, the run folder holds the build id,
    and no release manifest exists yet."""
    w = jw.ReleaseWorld(tmp_path)
    bound = w.prepare(frozen=False)
    tag = jo.image_tag(bound)
    build = {"id": BUILD_ID, "status": "SUCCESS", "serviceAccount": rw.BUILD_SA,
             "source": {"storageSource": {"bucket": dj.source_location()[0], "object": dj.source_object(tag.rsplit(":", 1)[1])}},
             "steps": [{"name": "gcr.io/cloud-builders/docker",
                        "args": ["build", "-f", "core/setup/jobs.Dockerfile", "--build-arg", f"GIT_SHA={jw.B_COMMIT}", "-t", tag, "."]}],
             "results": {"images": [{"name": tag, "digest": jw.NEW_DIGEST}]}}
    build.update(record or {})
    w.world.builds[BUILD_ID] = build
    w.world.registry[tag] = jw.NEW_DIGEST
    (w.evidence / "build-id.txt").write_text(BUILD_ID + "\n", encoding="utf-8")
    return w


def manifest_files(w):
    return sorted(p.name for p in w.release_dir.glob("release-manifest.*"))


# JB-02: the FreezeJobs phase freezes the build the run folder names

def test_jb02_the_freezejobs_phase_freezes_the_digest_of_the_build_in_the_run_folder_and_writes_the_manifest_once(tmp_path):
    w = freezing(tmp_path)
    result = w.run("FreezeJobs")
    assert manifest_files(w) == ["release-manifest.json", "release-manifest.sha256"]
    assert jo.frozen_digest(w.release_dir) == jw.NEW_DIGEST
    assert result["phase"] == "FreezeJobs" and result["observations"]["image_digest"] == jw.NEW_DIGEST
    assert json.loads((w.release_dir / "readbacks" / "FreezeJobs-01.json").read_text(encoding="utf-8"))["release_id"] == jw.B_RID
    assert ("build", BUILD_ID) in w.fake_reader.calls
    assert w.fake_reader.calls.index(("build", BUILD_ID)) < w.fake_reader.calls.index(("registry_digest", jo.image_tag(w.bound)))


def test_jb02_the_freezejobs_phase_will_not_write_a_second_manifest(tmp_path):
    w = freezing(tmp_path)
    w.run("FreezeJobs")
    stopped = w.stop("FreezeJobs")
    assert stopped.code == "MANIFEST_HASH"


def test_jb02_without_a_build_id_in_the_run_folder_the_phase_stops_before_any_cloud_read_and_writes_nothing(tmp_path):
    w = freezing(tmp_path)
    (w.evidence / "build-id.txt").unlink()
    stopped = w.stop("FreezeJobs")
    assert stopped.code == "BUILD"
    assert manifest_files(w) == [] and ("build", BUILD_ID) not in w.fake_reader.calls


@pytest.mark.parametrize("text", ["", "\n", "not a build id\n", f"{BUILD_ID} extra\n", f"{BUILD_ID}\n{OTHER_BUILD_ID}\n", "--project=other\n",
                                  BUILD_ID.upper() + "\n", BUILD_ID[:-1] + "\n", "../../x\n"])
def test_jb02_a_build_id_that_is_not_one_uuid_is_never_passed_to_gcloud(tmp_path, text):
    w = freezing(tmp_path)
    (w.evidence / "build-id.txt").write_text(text, encoding="utf-8")
    stopped = w.stop("FreezeJobs")
    assert stopped.code == "BUILD"
    assert manifest_files(w) == [] and not [c for c in w.fake_reader.calls if c[0] == "build"]


def test_jb02_the_phase_takes_the_commit_the_tag_and_the_account_from_the_bindings_and_not_from_the_build_record(tmp_path):
    other = jw.B_COMMIT[:-1] + "0"
    w = freezing(tmp_path)
    tag = jo.image_tag(w.bound)
    w.world.builds[BUILD_ID]["steps"][0]["args"][4] = f"GIT_SHA={other}"
    assert w.stop("FreezeJobs").code == "BUILD"
    w2 = freezing(tmp_path / "second")
    w2.world.builds[BUILD_ID]["serviceAccount"] = "projects/ogilvy-trends-v2/serviceAccounts/someone@ogilvy-trends-v2.iam.gserviceaccount.com"
    assert w2.stop("FreezeJobs").code == "BUILD"
    w3 = freezing(tmp_path / "third")
    w3.world.builds[BUILD_ID]["source"]["storageSource"]["object"] = dj.source_object("0" * 12 + "-01")
    assert w3.stop("FreezeJobs").code == "BUILD"
    assert tag.endswith(":b5e1a2c4d6f8-01")
    for world in (w, w2, w3):
        assert manifest_files(world) == []


def test_jb02_a_registry_digest_that_differs_from_the_build_result_freezes_nothing(tmp_path):
    w = freezing(tmp_path)
    w.world.registry[jo.image_tag(w.bound)] = "sha256:" + "a1" * 32
    assert w.stop("FreezeJobs").code == "DIGEST"
    assert manifest_files(w) == []


def test_jb02_the_phase_is_one_of_the_six_phases_and_no_longer_says_it_is_not_built(tmp_path):
    w = freezing(tmp_path)
    text = open(jo.__file__, encoding="utf-8").read()
    assert "NOT_BUILT" not in text and "FreezeJobs" in jo.PHASES
    assert w.run("FreezeJobs")["mode"] == "jobs"


# JS-09: JobsCandidate refuses without a clean apply dry run receipt, on the acting path (BeforeAnyWrite runs before the build)

def candidate_world(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    w.prepare()
    return w


def one_view():
    from core.schema import apply

    return next(apply.statement_name(s) for s in apply.load_statements() if apply.kind(s) == "view")


def test_js09_a_clean_receipt_with_the_bound_hash_lets_before_any_write_pass(tmp_path):
    w = candidate_world(tmp_path)
    assert w.run("BeforeAnyWrite")["phase"] == "BeforeAnyWrite"


RECEIPTS = {
    "drift": lambda: jw.dry_run_receipt_text(drift=[one_view()]),
    "failed statement": lambda: jw.dry_run_receipt_text(failed=one_view()),
    "view not dry run": lambda: jw.dry_run_receipt_text(skipped=[one_view()]),
    "not a dry run log": lambda: jw.dry_run_receipt_text(tail="created everything" + chr(10)),
    "empty": lambda: "",
}


@pytest.mark.parametrize("name", sorted(RECEIPTS))
def test_js09_before_any_write_refuses_a_receipt_that_does_not_show_a_clean_dry_run_even_when_its_hash_is_bound(tmp_path, name):
    w = candidate_world(tmp_path)
    w.rewrite_dry_run(RECEIPTS[name]())
    stopped = w.stop("BeforeAnyWrite")
    assert stopped.code == "DRY_RUN_RECEIPT"
    assert not [c for c in w.fake_reader.calls if c[0] in ("job", "service", "registry_digest")]
    assert not (w.release_dir / "readbacks").exists()


def test_js09_before_any_write_refuses_a_receipt_changed_after_its_hash_was_bound(tmp_path):
    w = candidate_world(tmp_path)
    w.rewrite_dry_run(jw.dry_run_receipt_text(), rebind=False)
    path = w.release_dir / "apply-dry-run-receipt.json"
    path.write_bytes(path.read_bytes() + b" ")
    assert w.stop("BeforeAnyWrite").code == "DRY_RUN_RECEIPT"


def test_js09_before_any_write_refuses_when_there_is_no_receipt_or_no_hash_is_bound(tmp_path):
    w = candidate_world(tmp_path)
    (w.release_dir / "apply-dry-run-receipt.json").unlink()
    assert w.stop("BeforeAnyWrite").code == "DRY_RUN_RECEIPT"
    w2 = candidate_world(tmp_path / "unbound")
    del w2.bound["dryRunReceiptSha256"]
    assert w2.stop("BeforeAnyWrite").code == "BINDINGS"


def test_js09_the_views_the_receipt_must_cover_come_from_the_tree_of_the_bound_commit_not_from_the_receipt(tmp_path):
    w = candidate_world(tmp_path)
    short = one_view().rsplit(".", 1)[-1]
    tree = [(path, text.replace(short, short + "_other")) if path.startswith("core/schema/") and path.endswith(".sql") else (path, text)
            for path, text in jw.head_tree()]
    assert tree != jw.head_tree()
    from core.setup.release import jobs_only as jo

    with pytest.raises(so.Stop) as stopped:
        jo.run_phase(w.bound, "BeforeAnyWrite", w.fake_reader, w.evidence, now=lambda: w.now, bq=w.bq_client(), head_files=lambda: tree)
    assert stopped.value.code == "DRY_RUN_RECEIPT" and short + "_other" in stopped.value.message


def test_js09_a_source_commit_that_cannot_be_read_stops_with_a_code_and_not_a_traceback(tmp_path):
    w = candidate_world(tmp_path)
    from core.setup.release import jobs_only as jo

    def unreadable():
        from core.setup import durable_effects_check as de

        raise de.Refusal("git archive of the commit failed")

    with pytest.raises(so.Stop) as stopped:
        jo.run_phase(w.bound, "BeforeAnyWrite", w.fake_reader, w.evidence, now=lambda: w.now, bq=w.bq_client(), head_files=unreadable)
    assert stopped.value.code == "SOURCE"
