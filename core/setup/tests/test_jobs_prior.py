"""The jobs release tooling bound to a prior jobs release (Release D on top of the live Release B). The rollback image, the job
definitions a rollback restores, the baseline chain and the release letter of the typed lines come from a prior jobs release record, which
the packet command derives from the prior release's own lock and packet, and not from a constant of the tooling.

The values the tests expect are literals here (the digest Release B went live on, the dates, the letter), never read from the code under test.
Offline: no cloud, no network, no pwsh. The end to end run through the real paste is in test_jobs_final.py."""
import copy
import hashlib
import datetime as dt
import json
from pathlib import Path

import pytest

from core.setup.release import chain_evidence as ce
from core.setup.release import jobs_baseline as jb
from core.setup.release import jobs_only as jo
from core.setup.release import packet
from core.setup.release import services_only as so
from core.setup.tests import jobs_packet_world as jpw
from core.setup.tests import jobs_prior_world as jpr
from core.setup.tests import jobs_world as jw
from core.setup.tests import packet_world as pw
from core.setup.tests import release_world as rw
from core.setup.tests import test_jobs_packet as tjp

B = "sha256:a81ea0b5444c992171845a205a22c10721d50714884a50fd34747ac95dcbf0d0"
E77 = "sha256:e77c3819cc688d7690d4370a5fd575026e4d3e62c0ab322d54b6eabb515c75de"
D_NOW = dt.datetime(2026, 10, 13, 8, 0, tzinfo=dt.timezone.utc)
D_DAY = dt.date(2026, 10, 12)


def run(argv, capsys):
    code = packet.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


# prior-jobs-release: the record is derived from the prior release's own lock and packet

def derived(tmp_path, capsys, prior=None, **over):
    prior = prior or jpr.PriorPacket(tmp_path)
    out = tmp_path / "record.json"
    code, stdout, stderr = run(prior.derive_argv(out, **over), capsys)
    return prior, out, code, stdout, stderr


def test_the_record_holds_the_digest_the_terminal_and_the_fourteen_definitions_the_prior_release_left(tmp_path, capsys):
    prior, out, code, stdout, stderr = derived(tmp_path, capsys)
    assert code == 0, stderr
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record == {
        "schema_version": 1, "kind": "prior-jobs-release", "release_id": "rel-89d4629-01", "commit": "89d4629295bd6243149352c55ba42937bbddff47",
        "image_digest": B, "terminal": {"kind": "AfterJobsUpdate", "at_utc": "2026-10-11T14:56:41.844177+00:00"},
        "job_definitions": prior.definitions,
        "sources": {"lock_sha256": jpr.sha_file(prior.lock_path), "bindings_sha256": jpr.sha_file(prior.bindings_path),
                    "manifest_sha256": jpr.sha_file(prior.manifest_path), "after_update_sha256": jpr.sha_file(prior.readback_path("AfterJobsUpdate")),
                    "update_log_sha256": jpr.sha_file(prior.update_log_path)}}
    assert sorted(record["job_definitions"]) == sorted(so.JOB_NAMES)
    assert str(out) in stdout and jpr.sha_file(out) in stdout


def test_the_record_is_never_overwritten(tmp_path, capsys):
    prior = jpr.PriorPacket(tmp_path)
    out = tmp_path / "record.json"
    out.write_text("kept" + chr(10), encoding="utf-8")
    code, stdout, stderr = run(prior.derive_argv(out), capsys)
    assert code == 1 and "REFUSED" in stderr and out.read_text(encoding="utf-8") == "kept" + chr(10)


def test_the_digest_the_lead_names_is_required(tmp_path, capsys):
    prior = jpr.PriorPacket(tmp_path)
    argv = ["prior-jobs-release", "--prior-lock", str(prior.lock_path), "--prior-packet-dir", str(prior.packet), "--out", str(tmp_path / "r.json")]
    with pytest.raises(SystemExit) as stopped:
        packet.main(argv)
    assert stopped.value.code == 2


def refused(tmp_path, capsys, prior, **over):
    out = tmp_path / "record.json"
    code, stdout, stderr = run(prior.derive_argv(out, **over), capsys)
    assert code == 1 and "REFUSED" in stderr, (code, stderr)
    assert not out.exists()
    return stderr


def test_a_pin_that_differs_from_the_digest_the_prior_release_froze_refuses(tmp_path, capsys):
    stderr = refused(tmp_path, capsys, jpr.PriorPacket(tmp_path), **{"--expect-digest": "sha256:" + "ab" * 32})
    assert "digest" in stderr


def test_a_pin_that_is_not_a_sha256_digest_is_a_bad_command_line(tmp_path, capsys):
    prior = jpr.PriorPacket(tmp_path)
    with pytest.raises(SystemExit) as stopped:
        packet.main(prior.derive_argv(tmp_path / "r.json", **{"--expect-digest": "sha256:a81ea0b5"}))
    assert stopped.value.code == 2


def test_a_manifest_that_names_another_digest_is_refused_though_its_hash_and_sidecar_agree_with_it(tmp_path, capsys):
    # The manifest proves nothing about itself: its hash and its sidecar are recomputed from what it says. The FreezeJobs readback, a file the
    # helper wrote at the time, still says the first digest, and the pin says it again.
    prior = jpr.PriorPacket(tmp_path)
    other = "sha256:" + "cd" * 32

    def swap(value):
        value["image"].update({"digest": other, "registry_digest": other, "reference": f"{jo.JOBS_REPO}@{other}"})

    prior.refreeze(swap)
    refused(tmp_path, capsys, prior)


def test_a_prior_packet_made_wholly_consistent_around_another_digest_is_refused_by_the_pin(tmp_path, capsys):
    other = "sha256:" + "cd" * 32
    prior = jpr.PriorPacket(tmp_path, digest=other)
    assert "digest" in refused(tmp_path, capsys, prior)


def mutate(path_of, change):
    return lambda prior: prior.edit(path_of(prior), change)


def set_key(key, value):
    def change(data):
        data[key] = value
    return change


def drop_key(key):
    def change(data):
        del data[key]
    return change


def obs(key, value):
    def change(data):
        data["observations"][key] = value
    return change


def log_job(index, **changes):
    def change(data):
        data["changed_definitions"][index].update(changes)
    return change


def drop_job(data):
    del data["changed_definitions"][0]
    data["updated"] = data["updated"][1:]


def relocked(change):
    """Edit a file of the packet, then make the lock and the review agree with the edit, so the check under test is the one that refuses."""
    def apply(prior):
        change(prior)
        prior.relock()
    return apply


def lock_names(release_id):
    def apply(prior):
        prior.edit(prior.lock_path, set_key("release_id", release_id))
        prior.write_review()
    return apply


LOCK = "does not accept this lock"
BINDINGS = "bindings differ from the prior lock"
OTHER_RELEASE = "not for the jobs release the lock names"
AFTER = "AfterJobsUpdate readback is not this release's"
FROZEN = "FreezeJobs readback does not hold this manifest and this digest"
NO_DEF = "do not give a definition hash"

CASES = {
    "review rejects": (lambda p: p.write_review("REJECT"), LOCK),
    "review binds another lock": (lambda p: p.write_review(lock_sha256="0" * 64), LOCK),
    "review is for another commit": (lambda p: p.write_review(target="0" * 40), OTHER_RELEASE),
    "bindings changed after the lock": (lambda p: p.bindings_path.write_bytes(p.bindings_path.read_bytes() + b" "), BINDINGS),
    "bindings of another release": (relocked(mutate(lambda p: p.bindings_path, lambda d: d.update({"release_id": "rel-0000000-01", "target": "0" * 40}))),
                                    OTHER_RELEASE),
    "bindings whose release id does not name their commit": (relocked(mutate(lambda p: p.bindings_path, set_key("release_id", "rel-0000000-01"))),
                                                             "bindings are not accepted"),
    "bindings not in jobs mode": (relocked(mutate(lambda p: p.bindings_path, set_key("mode", "services-only"))), "bindings are not accepted"),
    "bindings do not validate": (relocked(mutate(lambda p: p.bindings_path, drop_key("window"))), "bindings are not accepted"),
    "bindings not bound for review": (relocked(mutate(lambda p: p.bindings_path, set_key("status", "DRAFT"))), OTHER_RELEASE),
    "lock names another release": (lock_names("rel-0000000-01"), OTHER_RELEASE),
    "manifest changed without its hash": (mutate(lambda p: p.manifest_path, lambda d: d["image"].update({"tag": "other"})), "own hash"),
    "manifest sidecar differs": (lambda p: p.sidecar_path.write_text("0" * 64 + chr(10), encoding="utf-8"), "sidecar"),
    "manifest for another commit": (lambda p: p.refreeze(lambda d: d["source"].update({"commit": "0" * 40})), "release and commit the lock"),
    "manifest for another release": (lambda p: p.refreeze(lambda d: d["source"].update({"release_id": "rel-0000000-01"})), "release and commit the lock"),
    "manifest of another kind": (lambda p: p.refreeze(lambda d: d.update({"kind": "release-manifest"})), "release and commit the lock"),
    "manifest image is not one digest": (lambda p: p.refreeze(lambda d: d["image"].update({"registry_digest": "sha256:" + "cd" * 32})), "not one digest"),
    "manifest reference names another image": (lambda p: p.refreeze(lambda d: d["image"].update({"reference": f"{jo.JOBS_REPO}@sha256:" + "cd" * 32})),
                                               "not one digest"),
    "freeze readback missing": (lambda p: p.readback_path("FreezeJobs").unlink(), "no FreezeJobs readback"),
    "freeze readback of another release": (mutate(lambda p: p.readback_path("FreezeJobs"), set_key("release_id", "rel-0000000-01")), FROZEN),
    "freeze readback in services mode": (mutate(lambda p: p.readback_path("FreezeJobs"), set_key("mode", "services-only")), FROZEN),
    "freeze readback holds another manifest": (mutate(lambda p: p.readback_path("FreezeJobs"), obs("manifest_sha256", "0" * 64)), FROZEN),
    "freeze readback holds another digest": (mutate(lambda p: p.readback_path("FreezeJobs"), obs("image_digest", "sha256:" + "cd" * 32)), FROZEN),
    "after update readback missing": (lambda p: p.readback_path("AfterJobsUpdate").unlink(), "no AfterJobsUpdate readback"),
    "after update readback of another release": (mutate(lambda p: p.readback_path("AfterJobsUpdate"), set_key("release_id", "rel-0000000-01")), AFTER),
    "after update readback in services mode": (mutate(lambda p: p.readback_path("AfterJobsUpdate"), set_key("mode", "services-only")), AFTER),
    "after update readback blocking": (mutate(lambda p: p.readback_path("AfterJobsUpdate"), set_key("blocking", {"jobs": {"blocking": True}})), AFTER),
    "after update readback with 13 jobs at the digest": (mutate(lambda p: p.readback_path("AfterJobsUpdate"), obs("jobs_at_digest", 13)), AFTER),
    "after update readback with a naive time": (mutate(lambda p: p.readback_path("AfterJobsUpdate"), set_key("at_utc", "2026-10-11T14:56:41")), AFTER),
    "after update readback with no time": (mutate(lambda p: p.readback_path("AfterJobsUpdate"), drop_key("at_utc")), AFTER),
    "the release was rolled back": (lambda p: jpr.write_json(p.readback_path("AfterJobsRollback"), p.readback("AfterJobsRollback", {})), "AfterJobsRollback readback"),
    "a rollback was started": (lambda p: jpr.write_json(p.readback_path("BeforeJobsRollback"), p.readback("BeforeJobsRollback", {})), "BeforeJobsRollback readback"),
    "update log missing": (lambda p: p.update_log_path.unlink(), "no JobsUpdate log"),
    "update log incomplete": (mutate(lambda p: p.update_log_path, set_key("complete", False)), "not a complete update"),
    "update log stopped": (mutate(lambda p: p.update_log_path, set_key("stopped", {"code": "UPDATE_FAILED", "job": "f42-detect"})), "not a complete update"),
    "update log is not an update": (mutate(lambda p: p.update_log_path, set_key("action", "rollback")), "not an update log"),
    "update log without a definition hash": (mutate(lambda p: p.update_log_path, log_job(0, after_sha256=None)), NO_DEF),
    "update log with a malformed definition hash": (mutate(lambda p: p.update_log_path, log_job(0, after_sha256="abc")), NO_DEF),
    "update log lacking a job": (mutate(lambda p: p.update_log_path, drop_job), NO_DEF),
    "update log finished after the readback": (mutate(lambda p: p.update_log_path, set_key("finished_at", "2026-10-11T14:56:42+00:00")),
                                               "finished after its AfterJobsUpdate readback"),
    "update log with no finish time": (mutate(lambda p: p.update_log_path, set_key("finished_at", None)), "finished after its AfterJobsUpdate readback"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_a_prior_packet_that_is_not_one_release_finished_by_its_own_files_is_refused_by_the_check_for_it_and_no_record_is_written(tmp_path, capsys, name):
    prior = jpr.PriorPacket(tmp_path)
    change, fragment = CASES[name]
    change(prior)
    assert fragment in refused(tmp_path, capsys, prior)


def test_the_latest_update_log_that_names_a_job_decides_its_definition(tmp_path, capsys):
    # A JobsUpdate that stopped and was run again leaves two logs. The first holds the jobs it updated, the second the rest, and the first job
    # again: it was updated a second time, so the first log's fingerprint of it is stale and the second log's is the one that stands.
    prior = jpr.PriorPacket(tmp_path)
    first_job = jo.UPDATE_ORDER[0]
    first = prior.update_log()
    first.update({"complete": False, "stopped": {"code": "UPDATE_FAILED", "job": "f42-detect"}, "updated": list(jo.UPDATE_ORDER[:11])})
    first["changed_definitions"] = first["changed_definitions"][:11]
    first["changed_definitions"][0]["after_sha256"] = jpr.sha_text("a definition the second run replaced")
    second = prior.update_log()
    second["changed_definitions"] = [second["changed_definitions"][0], *second["changed_definitions"][11:]]
    assert second["changed_definitions"][0]["job"] == first_job and second["changed_definitions"][0]["after_sha256"] == prior.definitions[first_job]
    second["skipped_already_new"] = list(jo.UPDATE_ORDER[1:11])
    second["updated"] = [first_job, *jo.UPDATE_ORDER[11:]]
    jpr.write_json(prior.update_log_path, first)
    jpr.write_json(prior.update_log_path.with_name("jobs-update-02.json"), second)
    prior, out, code, stdout, stderr = derived(tmp_path, capsys, prior)
    assert code == 0, stderr
    assert json.loads(out.read_text(encoding="utf-8"))["job_definitions"] == prior.definitions


def test_a_prior_packet_that_is_not_a_packet_is_refused_and_not_a_traceback(tmp_path, capsys):
    prior = jpr.PriorPacket(tmp_path)
    prior.review_path.write_text("[]", encoding="utf-8")
    refused(tmp_path, capsys, prior)


# the record itself: its shape is checked wherever it is read

def good_record():
    return jpr.record_for_world(jpr.world_on_prior(rw.World()))


def test_a_good_record_is_accepted():
    jb.validate_prior_jobs_release(good_record())


def break_key(key, value):
    def change(record):
        record[key] = value
    return change


def change_terminal(**over):
    def change(record):
        record["terminal"].update(over)
    return change


def remove_job(record):
    del record["job_definitions"]["f42-brief"]


def add_job(record):
    record["job_definitions"]["f42-extra"] = "0" * 64


RECORD_CASES = {
    "another kind": break_key("kind", "baseline-J"),
    "another schema version": break_key("schema_version", 2),
    "an extra key": break_key("note", "x"),
    "a release id that is not one": break_key("release_id", "rel-nonsense"),
    "a release id that does not name the commit": break_key("release_id", "rel-0000000-01"),
    "a short commit": break_key("commit", "89d4629"),
    "a digest that is a prefix": break_key("image_digest", "sha256:a81ea0b5"),
    "a digest in capitals": break_key("image_digest", B.upper()),
    "a terminal that is not AfterJobsUpdate": change_terminal(kind="AfterPromotion"),
    "a naive terminal time": change_terminal(at_utc="2026-10-11T14:56:41"),
    "a terminal time that is not a time": change_terminal(at_utc="yesterday"),
    "a missing job definition": remove_job,
    "an extra job definition": add_job,
    "a definition that is not a sha256": break_key("job_definitions", {**good_record()["job_definitions"], "f42-brief": "abc"}),
    "sources that are not hashes": break_key("sources", {"lock_sha256": "x"}),
    "no sources": lambda record: record.pop("sources"),
}


@pytest.mark.parametrize("name", sorted(RECORD_CASES))
def test_a_record_that_is_not_shaped_as_the_derivation_writes_it_is_refused(name):
    record = good_record()
    RECORD_CASES[name](record)
    with pytest.raises(so.Stop) as stop:
        jb.validate_prior_jobs_release(record)
    assert stop.value.code == "PRIOR_RELEASE", stop.value.message


# baseline-J captured against the prior release

def prior_reader(tmp_path, *, retire="waits_for_b"):
    world = jw.a_promoted_world(tmp_path)
    jpr.world_on_prior(world)
    return world, rw.FakeReader(world), jpr.record_for_world(world)


A_TERMINAL = {"kind": "AfterPromotion", "at_utc": jw.A_TERMINAL_AT, "a_release_id": jw.A_RID}
CAPTURED = "2026-10-13T08:00:00+00:00"


def capture(reader, prior, **over):
    args = dict(a_terminal=A_TERMINAL, a80_serving=None, rollback_digest=B, a_retire="waits_for_b", captured_utc=CAPTURED, prior=prior)
    args.update(over)
    return jb.capture_baseline_j(reader, **args)


def test_a_baseline_captured_against_the_prior_release_pins_its_digest_its_definitions_and_the_record_itself(tmp_path):
    world, reader, record = prior_reader(tmp_path)
    baseline = capture(reader, record)
    assert baseline["rollbackJobsDigest"] == B and baseline["priorJobsRelease"] == record
    assert {view["image"] for view in baseline["jobs"].values()} == {f"{jo.JOBS_REPO}@{B}"}
    assert baseline["aTerminal"] == A_TERMINAL and baseline["kind"] == "baseline-J" and baseline["schema_version"] == 1


def test_a_baseline_captured_without_a_prior_has_no_prior_key(tmp_path):
    world = jw.a_promoted_world(tmp_path)
    baseline = jb.capture_baseline_j(rw.FakeReader(world), a_terminal=A_TERMINAL, a80_serving=None, rollback_digest=E77, a_retire="waits_for_b",
                                     captured_utc=CAPTURED)
    assert "priorJobsRelease" not in baseline


def test_the_jobs_must_run_the_prior_digest_and_not_the_digest_of_the_release_before_it(tmp_path):
    world, reader, record = prior_reader(tmp_path)
    jw.set_all_jobs_image(world, E77)
    with pytest.raises(so.Stop) as stop:
        capture(reader, record)
    assert stop.value.code == "BASELINE"


def test_one_job_on_another_image_is_refused(tmp_path):
    world, reader, record = prior_reader(tmp_path)
    jw.set_job_image(world, "f42-digest", E77)
    with pytest.raises(so.Stop) as stop:
        capture(reader, record)
    assert stop.value.code == "BASELINE"


def test_a_job_whose_definition_differs_from_the_one_the_prior_release_left_is_refused_though_its_image_is_right(tmp_path):
    world, reader, record = prior_reader(tmp_path)
    container = world.jobs["f42-digest"]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    container["args"] = [*container.get("args", []), "--changed-since-the-prior-release"]
    with pytest.raises(so.Stop) as stop:
        capture(reader, record)
    assert stop.value.code == "BASELINE" and "f42-digest" in stop.value.message


def test_a_rollback_digest_that_is_not_the_priors_is_refused_before_the_jobs_are_read(tmp_path):
    world, reader, record = prior_reader(tmp_path)
    with pytest.raises(so.Stop) as stop:
        capture(reader, record, rollback_digest=E77)
    assert stop.value.code == "BASELINE" and "not the digest the prior jobs release left" in stop.value.message
    assert not [call for call in reader.calls if call[0] == "job"]


def test_a_prior_terminal_after_the_capture_is_refused(tmp_path):
    world, reader, record = prior_reader(tmp_path)
    with pytest.raises(so.Stop) as stop:
        capture(reader, record, captured_utc="2026-10-11T14:00:00+00:00")
    assert stop.value.code == "BASELINE"


def test_a_malformed_record_is_refused_before_the_jobs_are_read(tmp_path):
    world, reader, record = prior_reader(tmp_path)
    record["job_definitions"].pop("f42-brief")
    with pytest.raises(so.Stop) as stop:
        capture(reader, record)
    assert stop.value.code == "PRIOR_RELEASE"
    assert not [call for call in reader.calls if call[0] == "job"]


# the bindings and the acting path

def d_world(tmp_path, *, terminal_at=jpr.TERMINAL_AT, run_day=D_DAY, now=D_NOW):
    w = jw.ReleaseWorld(tmp_path, digest=B, day=run_day)
    w.now = now
    jpr.world_on_prior(w.world)
    record = jpr.record_for_world(w.world, terminal_at=terminal_at)
    baseline = w.baseline_j()
    baseline["rollbackJobsDigest"] = B
    baseline["priorJobsRelease"] = record
    w.prepare(baseline=baseline, bindings_over={"rollbackJobsDigest": B, "priorJobsRelease": record, "releaseLetter": "D"})
    return w, record


def test_the_release_phases_take_the_rollback_digest_and_the_definitions_from_the_prior_release(tmp_path):
    w, record = d_world(tmp_path)
    assert w.bound["rollbackJobsDigest"] == B
    assert w.run("BeforeAnyWrite")["phase"] == "BeforeAnyWrite"


def test_the_baseline_chain_is_judged_from_the_prior_terminal_and_not_from_release_a(tmp_path):
    # The chain of 12 Oct is the first after Release B's AfterJobsUpdate (11 Oct 16:56 SAST). Counted from Release A's terminal (9 Oct) it
    # would be the fourth chain; the manifest must still name the prior terminal as its anchor.
    w, record = d_world(tmp_path)
    assert w.baseline_j()["aTerminal"]["at_utc"] == jw.A_TERMINAL_AT
    assert w.run("BeforeAnyWrite")["observations"]["baseline_chain"]["run_date"] == "2026-10-12"
    manifest = json.loads(open(w.bound["baselineChainPath"], encoding="utf-8").read())
    assert manifest["a_terminal"] == {"kind": "AfterJobsUpdate", "at_utc": jpr.TERMINAL_AT}


@pytest.mark.parametrize("terminal,run_day,now_day,accepted", [
    ("2026-10-11T14:56:41.844177+00:00", 12, 13, True),
    ("2026-10-11T14:56:41.844177+00:00", 13, 13, True),
    ("2026-10-11T14:56:41.844177+00:00", 14, 14, True),   # the third chain after the prior terminal serves as well (the Q3 ordinal rule is gone)
    ("2026-10-11T14:56:41.844177+00:00", 11, 12, False),  # the 11th's collect started before the prior terminal: WINDOW
    ("2026-10-11T23:30:00+00:00", 12, 13, True),    # 01:30 SAST on the 12th, before that day's 02:00 collect: the chain of the 12th follows it
    ("2026-10-11T23:30:00+00:00", 14, 14, True),
    ("2026-10-12T00:30:00+00:00", 12, 13, False),   # 02:30 SAST on the 12th: the 12th's chain ran before it
    ("2026-10-12T00:30:00+00:00", 14, 14, True),
])
def test_any_chain_after_the_prior_terminal_inside_the_bound_age_can_be_the_baseline(tmp_path, terminal, run_day, now_day, accepted):
    w, record = d_world(tmp_path, terminal_at=terminal, run_day=dt.date(2026, 10, run_day), now=dt.datetime(2026, 10, now_day, 8, 0, tzinfo=jw.UTC))
    if accepted:
        assert w.run("BeforeAnyWrite")["phase"] == "BeforeAnyWrite"
    else:
        assert w.stop("BeforeAnyWrite").code == "BASELINE"


def failed_chain_rebound(w):
    """The qualifying manifest of the world, rewritten as one whose detect failed, rebound by hash."""
    path = Path(w.bound["baselineChainPath"])
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["verdict"] = {"qualifies": False, "baseline_by_line": False, "failed_stage": "detect", "reasons": ["TERMINAL", "UPSTREAM"],
                           "decision": None}
    tampered = w.release_dir / "failed-chain.json"
    jw.write_json(tampered, manifest)
    w.bound["baselineChainPath"] = str(tampered)
    w.bound["baselineChainSha256"] = hashlib.sha256(tampered.read_bytes()).hexdigest()
    return manifest


def test_a_typed_acceptance_after_a_prior_jobs_release_must_name_that_releases_commit(tmp_path):
    w, record = d_world(tmp_path)
    manifest = failed_chain_rebound(w)
    short = record["commit"][:7]
    without = {"line": "BASELINE ACCEPT 2026-10-12: I accept the chain, whose detect failed, as the baseline.", "run_date": "2026-10-12",
               "reasons": ["TERMINAL", "UPSTREAM"]}
    w.bound["baselineChainAcceptance"] = without
    assert w.stop("BeforeAnyWrite").code == "BINDINGS"
    w.bound["baselineChainAcceptance"] = {**without, "line": f"BASELINE ACCEPT 2026-10-12: I accept the chain, whose detect failed, as the "
                                                            f"baseline of the jobs only release on the {short} image."}
    observed = w.run("BeforeAnyWrite")["observations"]["baseline_chain"]
    assert observed["accepted_by_line"] is True and observed["run_date"] == manifest["run_date"]


def test_a_job_that_drifted_from_the_prior_definitions_after_baseline_j_was_captured_stops_the_release(tmp_path):
    w, record = d_world(tmp_path)
    w.world.jobs["f42-digest"]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["args"] = ["-m", "elsewhere"]
    assert w.stop("BeforeAnyWrite").code == "JOB_DRIFT"


def test_a_baseline_whose_jobs_are_not_the_priors_definitions_stops_the_release_though_the_bindings_pin_that_baseline(tmp_path):
    # baseline-J is bound by its hash, so a baseline written with other definitions would be a bound input. The prior record is the expectation.
    w = jw.ReleaseWorld(tmp_path, digest=B, day=D_DAY)
    w.now = D_NOW
    jpr.world_on_prior(w.world)
    record = jpr.record_for_world(w.world)
    w.world.jobs["f42-digest"]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["args"] = ["-m", "elsewhere"]
    baseline = w.baseline_j()
    baseline["rollbackJobsDigest"] = B
    baseline["priorJobsRelease"] = record
    w.prepare(baseline=baseline, bindings_over={"rollbackJobsDigest": B, "priorJobsRelease": record, "releaseLetter": "D"})
    assert w.stop("BeforeAnyWrite").code == "BASELINE"


def test_a_baseline_and_bindings_that_name_different_prior_records_stop_the_release(tmp_path):
    w, record = d_world(tmp_path)
    other = copy.deepcopy(record)
    other["release_id"], other["commit"] = "rel-0000000-01", "0" * 40
    w.bound["priorJobsRelease"] = other
    with pytest.raises(so.Stop) as stop:
        jo.JobsRelease(w.bound, w.fake_reader, w.evidence, w.now)
    assert stop.value.code == "BASELINE"


def test_a_baseline_with_a_prior_record_under_bindings_that_name_none_stops_the_release(tmp_path):
    w, record = d_world(tmp_path)
    del w.bound["priorJobsRelease"]
    with pytest.raises(so.Stop) as stop:
        jo.JobsRelease(w.bound, w.fake_reader, w.evidence, w.now)
    assert stop.value.code in ("BASELINE", "BINDINGS")


BINDING_CASES = {
    "a rollback digest other than the prior's": ("rollbackJobsDigest", E77, "BINDINGS"),
    "no release letter with a prior": ("releaseLetter", None, "BINDINGS"),
    "a lower case letter": ("releaseLetter", "d", "BINDINGS"),
    "two letters": ("releaseLetter", "DD", "BINDINGS"),
    "a letter that is not text": ("releaseLetter", 4, "BINDINGS"),
    "a malformed record": ("priorJobsRelease", {"kind": "prior-jobs-release"}, "PRIOR_RELEASE"),
}


@pytest.mark.parametrize("name", sorted(BINDING_CASES))
def test_bindings_that_name_a_prior_release_are_refused_on_the_acting_path_when_they_do_not_hold_together(tmp_path, name):
    w, record = d_world(tmp_path)
    key, value, code = BINDING_CASES[name]
    if value is None:
        del w.bound[key]
    else:
        w.bound[key] = value
    with pytest.raises(so.Stop) as stop:
        jo.validate_jobs_bindings(w.bound)
    assert stop.value.code == code, stop.value.message


def test_bindings_with_no_prior_release_are_unchanged_and_take_a_letter_if_given(tmp_path):
    w = jw.ReleaseWorld(tmp_path)
    bound = w.prepare()
    jo.validate_jobs_bindings(bound)
    jo.validate_jobs_bindings({**bound, "releaseLetter": "B"})
    with pytest.raises(so.Stop):
        jo.validate_jobs_bindings({**bound, "releaseLetter": "b"})


def test_the_producer_asks_for_the_prior_record_in_the_bindings_and_writes_the_prior_terminal_into_the_manifest(tmp_path):
    w, record = d_world(tmp_path)
    producer = {key: w.bound[key] for key in (*jo.PRODUCER_BINDINGS, "schema_version", "mode", "priorJobsRelease", "releaseLetter")}
    manifest = ce.build_manifest(producer, w.reader(), w.bq(), D_DAY, "baseline", now=lambda: D_NOW)
    assert manifest["a_terminal"] == {"kind": "AfterJobsUpdate", "at_utc": jpr.TERMINAL_AT}
    assert manifest["jobs_image"]["expected_digest"] == B and manifest["verdict"]["qualifies"] is True
    without = {key: value for key, value in producer.items() if key != "priorJobsRelease"}
    with pytest.raises(so.Stop):
        ce.build_manifest(without, w.reader(), w.bq(), D_DAY, "baseline", now=lambda: D_NOW)


def test_a_collect_that_started_before_the_prior_terminal_is_a_window_failure_in_the_manifest(tmp_path):
    w, record = d_world(tmp_path, run_day=dt.date(2026, 10, 11), now=dt.datetime(2026, 10, 12, 8, 0, tzinfo=jw.UTC))
    producer = {key: w.bound[key] for key in (*jo.PRODUCER_BINDINGS, "schema_version", "mode", "priorJobsRelease", "releaseLetter")}
    manifest = ce.build_manifest(producer, w.reader(), w.bq(), dt.date(2026, 10, 11), "baseline", now=lambda: w.now)
    assert "WINDOW" in manifest["verdict"]["reasons"]


# the typed lines carry the letter of the release

def test_the_typed_lines_carry_the_letter_the_release_was_bound_with():
    rid = "rel-1234567-01"
    assert packet.jobs_authorisation(rid, "D") == jpw.authorisation_line(rid, "D")
    assert packet.jobs_quiet(rid, "D") == jpw.quiet_line(rid, "D")
    assert packet.jobs_authorisation(rid) == jpw.authorisation_line(rid, "B")
    assert packet.jobs_quiet(rid) == jpw.quiet_line(rid, "B")
    assert jpw.authorisation_line(rid, "D").startswith("RELEASE D rel-1234567-01: I authorise the jobs only release")
    assert jpw.quiet_line(rid, "D").startswith("RELEASE D rel-1234567-01 QUIET: ")


# the packet commands

@pytest.fixture
def d_bench(tmp_path, monkeypatch):
    return make_d_bench(tmp_path, monkeypatch)


def make_d_bench(tmp_path, monkeypatch, *, on_prior=True, terminal_at=jpr.TERMINAL_AT, letter="D", day=D_DAY):
    prior_file = tmp_path / "prior" / "prior-jobs-release.json"

    def prepare(fixture):
        if on_prior:
            jpr.world_on_prior(fixture.world)
        fixture.now = D_NOW
        jpr.write_record(prior_file, jpr.record_for_world(fixture.world, terminal_at=terminal_at))

    return jpw.JobsBench(tmp_path / "d", monkeypatch, fixture_class=jw.ReleaseWorld, fixture_kwargs={"digest": B, "day": day}, prepare=prepare,
                         now=D_NOW, run_date=day, salt="a second release from one tree", letter=letter, prior_file=prior_file)


def captured(bench, capsys):
    bench.write_a_readback()
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 0, err
    return bench


def prepared(bench, capsys):
    captured(bench, capsys)
    code, out, err = run(bench.producer_argv(), capsys)
    assert code == 0, err
    bench.write_chain()
    bench.write_durable()
    bench.write_dry_run()
    return bench


def bound(bench, capsys):
    prepared(bench, capsys)
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 0, err
    return bench


def reviewed(bench, capsys):
    bound(bench, capsys)
    for argv in (bench.lock_argv(), bench.review_argv()):
        code, out, err = run(argv, capsys)
        assert code == 0, err
    return bench


def test_capture_baseline_j_with_a_prior_record_writes_a_baseline_on_the_prior_digest(d_bench, capsys):
    captured(d_bench, capsys)
    value = d_bench.baseline_value()
    record = json.loads(d_bench.prior_file.read_text(encoding="utf-8"))
    assert value["rollbackJobsDigest"] == B and value["priorJobsRelease"] == record
    assert {view["image"] for view in value["jobs"].values()} == {f"{jo.JOBS_REPO}@{B}"}
    assert value["aTerminal"]["a_release_id"] == jw.A_RID


def test_capture_baseline_j_with_a_prior_record_refuses_jobs_that_are_not_on_the_prior_digest_and_writes_nothing(tmp_path, monkeypatch, capsys):
    bench = make_d_bench(tmp_path, monkeypatch, on_prior=False)
    bench.write_a_readback()
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "do not run the bound rollback digest" in err and not bench.baseline.exists()


def test_capture_baseline_j_refuses_a_malformed_prior_record_before_any_read(d_bench, capsys):
    d_bench.write_a_readback()
    record = json.loads(d_bench.prior_file.read_text(encoding="utf-8"))
    del record["job_definitions"]["f42-brief"]
    d_bench.prior_file.write_text(json.dumps(record), encoding="utf-8")
    code, out, err = run(d_bench.capture_argv(), capsys)
    assert code == 1 and "REFUSED" in err and pw.calls(d_bench.log) == [] and not d_bench.baseline.exists()


def test_capture_baseline_j_without_a_prior_record_still_holds_the_jobs_to_the_digest_of_the_release_before_b(tmp_path, monkeypatch, capsys):
    bench = jpw.JobsBench(tmp_path, monkeypatch)
    bench.write_a_readback()
    assert run(bench.capture_argv(), capsys)[0] == 0
    assert bench.baseline_value()["rollbackJobsDigest"] == E77 and "priorJobsRelease" not in bench.baseline_value()


def test_bind_with_a_prior_record_binds_its_digest_its_record_and_the_release_letter(d_bench, capsys):
    bound(d_bench, capsys)
    value = d_bench.bindings_value()
    record = json.loads(d_bench.prior_file.read_text(encoding="utf-8"))
    assert value["rollbackJobsDigest"] == B and value["priorJobsRelease"] == record and value["releaseLetter"] == "D"
    assert value["release_id"] == f"rel-{d_bench.target[:7]}-01" and value["release_id"] != record["release_id"]
    producer = json.loads(d_bench.producer.read_text(encoding="utf-8"))
    assert producer["rollbackJobsDigest"] == B and producer["priorJobsRelease"] == record


def test_the_bindings_of_a_release_on_a_prior_hold_the_required_keys_and_the_two_optional_ones_of_a_prior(d_bench, capsys):
    # The third optional key, baselineChainAcceptance, is bound only with --accept-failed-baseline (test_jobs_packet).
    bound(d_bench, capsys)
    required = set(jo.RELEASE_BINDINGS) | {"schema_version", "mode", "status"}
    assert set(d_bench.bindings_value()) == required | {"priorJobsRelease", "releaseLetter"}
    assert set(jo.OPTIONAL_BINDINGS) == {"priorJobsRelease", "releaseLetter", "baselineChainAcceptance"}
    assert not set(jo.OPTIONAL_BINDINGS) & set(jo.RELEASE_BINDINGS)


def test_the_optional_bindings_are_exactly_the_keys_the_library_reads_beyond_the_required_ones():
    # test_jobs_packet holds the library to the keys bind writes. A key read there and declared nowhere is the undeclared read it exists to find,
    # and a key declared optional that nothing reads is a stale declaration.
    required = set(jo.RELEASE_BINDINGS) | {"schema_version", "mode", "status"}
    assert tjp.library_bound_keys() - required == set(jo.OPTIONAL_BINDINGS)


def test_the_bound_release_is_accepted_by_the_acting_path_and_its_chain_check_counts_from_the_prior_terminal(d_bench, capsys):
    bound(d_bench, capsys)
    value = d_bench.bindings_value()
    release = jo.JobsRelease(value, rw.FakeReader(d_bench.world), d_bench.tmp / "evidence", now=lambda: D_NOW)
    jo.check_baseline_chain(release)
    assert release.observations["baseline_chain"]["run_date"] == "2026-10-12"


def test_bind_with_a_prior_record_needs_a_release_letter(tmp_path, monkeypatch, capsys):
    bench = make_d_bench(tmp_path, monkeypatch, letter=None)
    prepared_no_letter = captured(bench, capsys)
    code, out, err = run(prepared_no_letter.producer_argv(), capsys)
    assert code == 1 and "letter" in err and not bench.producer.exists()


@pytest.mark.parametrize("letter", ["d", "DD", "1", ""])
def test_bind_refuses_a_letter_that_is_not_one_capital(tmp_path, monkeypatch, capsys, letter):
    bench = make_d_bench(tmp_path, monkeypatch, letter=letter)
    captured(bench, capsys)
    code, out, err = run(bench.producer_argv(), capsys)
    assert code == 1 and "letter" in err and not bench.producer.exists()


def test_bind_refuses_a_baseline_captured_without_the_prior_record(tmp_path, monkeypatch, capsys):
    # A baseline-J of the A form (jobs on the release before B) cannot be bound to a prior release, and a baseline captured against another
    # prior record cannot either.
    bench = make_d_bench(tmp_path, monkeypatch)
    captured(bench, capsys)
    value = bench.baseline_value()
    del value["priorJobsRelease"]
    bench.baseline.write_text(json.dumps(value), encoding="utf-8")
    code, out, err = run(bench.producer_argv(), capsys)
    assert code == 1 and "do not name the same prior jobs release" in err and not bench.producer.exists()


def test_bind_refuses_a_baseline_captured_against_another_prior_record(d_bench, capsys):
    captured(d_bench, capsys)
    value = d_bench.baseline_value()
    value["priorJobsRelease"]["release_id"] = "rel-0000000-01"
    value["priorJobsRelease"]["commit"] = "0" * 40
    d_bench.baseline.write_text(json.dumps(value), encoding="utf-8")
    code, out, err = run(d_bench.producer_argv(), capsys)
    assert code == 1 and "do not name the same prior jobs release" in err and not d_bench.producer.exists()


def test_bind_refuses_a_baseline_whose_job_is_not_the_definition_the_prior_release_left_though_its_image_is(d_bench, capsys):
    # The image is right and so is the record; one job's arguments are not what the prior release left, which a rollback would then restore.
    captured(d_bench, capsys)
    value = d_bench.baseline_value()
    value["jobs"]["f42-digest"]["args"] = ["-m", "elsewhere"]
    d_bench.baseline.write_text(json.dumps(value), encoding="utf-8")
    code, out, err = run(d_bench.producer_argv(), capsys)
    assert code == 1 and "f42-digest is not the definition the prior jobs release left" in err and not d_bench.producer.exists()


def test_bind_refuses_a_baseline_that_says_it_was_captured_against_another_digest(d_bench, capsys):
    # Its jobs and its record are right; what it says about the digest it was captured against is not. That is a baseline someone edited.
    captured(d_bench, capsys)
    value = d_bench.baseline_value()
    value["rollbackJobsDigest"] = E77
    d_bench.baseline.write_text(json.dumps(value), encoding="utf-8")
    code, out, err = run(d_bench.producer_argv(), capsys)
    assert code == 1 and "not captured against the digest of the prior jobs release" in err and not d_bench.producer.exists()


def test_bind_without_a_prior_record_refuses_a_baseline_that_carries_one(d_bench, capsys):
    # The jobs and the pinned digest are the release before B's, so the only thing left to refuse is the prior record the baseline holds.
    captured(d_bench, capsys)
    value = d_bench.baseline_value()
    value["rollbackJobsDigest"] = E77
    for view in value["jobs"].values():
        view["image"] = f"{jo.JOBS_REPO}@{E77}"
    d_bench.baseline.write_text(json.dumps(value), encoding="utf-8")
    argv = d_bench.producer_argv()
    index = argv.index("--prior-jobs-release")
    del argv[index:index + 2]
    code, out, err = run(argv, capsys)
    assert code == 1 and "do not name the same prior jobs release" in err and not d_bench.producer.exists()


def test_bind_refuses_a_chain_that_started_before_the_prior_terminal(tmp_path, monkeypatch, capsys):
    # The chain of 11 Oct ran from 02:00 SAST, before Release B's AfterJobsUpdate (16:56 SAST). It is a complete chain on Release B's image,
    # and it is not a chain after the prior release.
    bench = make_d_bench(tmp_path, monkeypatch, day=dt.date(2026, 10, 11))
    prepared(bench, capsys)
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "ran before the prior jobs release's AfterJobsUpdate readback" in err and not bench.bindings.exists()


def test_bind_refuses_a_chain_produced_under_other_bindings_than_the_prior_release_it_binds(d_bench, capsys):
    prepared(d_bench, capsys)
    record = json.loads(d_bench.producer.read_text(encoding="utf-8"))
    del record["priorJobsRelease"]
    d_bench.producer.write_text(json.dumps(record), encoding="utf-8")
    code, out, err = run(d_bench.bind_argv(), capsys)
    assert code == 1 and "other bindings" in err and not d_bench.bindings.exists()


def test_the_receipt_accepts_the_two_lines_with_the_letter_of_the_release_and_no_other(d_bench, capsys):
    reviewed(d_bench, capsys)
    rid = d_bench.release_id
    d_bench.line.write_text(jpw.authorisation_line(rid, "B") + chr(10), encoding="utf-8")
    d_bench.quiet_line_file.write_text(jpw.quiet_line(rid, "D") + chr(10), encoding="utf-8")
    code, out, err = run(d_bench.receipt_argv(), capsys)
    assert code == 1 and "authorisation line" in err and not d_bench.receipt.exists()
    d_bench.line.write_text(jpw.authorisation_line(rid, "D") + chr(10), encoding="utf-8")
    d_bench.quiet_line_file.write_text(jpw.quiet_line(rid, "B") + chr(10), encoding="utf-8")
    code, out, err = run(d_bench.receipt_argv(), capsys)
    assert code == 1 and "quiet window line" in err and not d_bench.receipt.exists()
    d_bench.quiet_line_file.write_text(jpw.quiet_line(rid, "D") + chr(10), encoding="utf-8")
    code, out, err = run(d_bench.receipt_argv(), capsys)
    assert code == 0, err
    assert json.loads(d_bench.receipt.read_text(encoding="utf-8"))["release_id"] == rid
