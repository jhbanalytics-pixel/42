"""Release C: the packet command line binds a release to a prior release other than a80 (lead ruling of 11 Oct 2026). Offline, like
test_release_packet.py: a stand-in gcloud, a throwaway repository, Release A's packet rebuilt as files, and no pwsh.

The prior release is a bound input. `prior-release` derives its record from A's own lock and the files that lock and the review tie
together; capture-baseline and bind judge live against that record; bind writes the record, the tags to retain and the release letter
into the bindings, so the lock hashes them and the helper reads them back."""
import hashlib
import json
import re

import pytest

from core.setup.release import lock as locklib
from core.setup.release import packet
from core.setup.release import services_only as so
from core.setup.tests import packet_world as pw
from core.setup.tests import release_world as rw

AGENT, API = so.SERVICES


def run(argv, capsys):
    code = packet.main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def bench(tmp_path, monkeypatch):
    return pw.Bench(tmp_path, monkeypatch, after="A")


@pytest.fixture
def prior(bench):
    return bench.prepare_prior()


def bound_bench(bench, capsys):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 0, err
    return bench


def reviewed_bench(bench, capsys):
    bound_bench(bench, capsys)
    for argv in (bench.lock_argv(), bench.review_argv()):
        code, out, err = run(argv, capsys)
        assert code == 0, err
    return bench


# prior-release: the record is derived from A's own files, each tie is recomputed, and nothing is taken from the payload it proves

def test_prior_release_writes_the_record_built_from_the_values_and_hashes_of_the_prior_packet(bench, capsys, prior):
    expected = prior.expected()
    code, out, err = run(bench.prior_argv(), capsys)
    assert code == 0, err
    assert json.loads(bench.prior_file.read_text(encoding="utf-8")) == expected
    assert pw.sha(bench.prior_file) in out and str(bench.prior_file) in out
    assert so.validate_prior_release(expected, need_jobs=False)


def test_prior_release_derives_whether_the_prior_release_stores_the_typed_state_from_its_writers_at_that_commit(bench, capsys, prior, monkeypatch):
    asked = []

    def derive(repo, commit):
        asked.append((str(repo), commit))
        return False

    monkeypatch.setattr(packet, "prior_writes_answer_meta", derive)
    code, out, err = run(bench.prior_argv(), capsys)
    assert code == 0, err
    assert asked == [(str(bench.repo), rw.A_COMMIT)]
    assert json.loads(bench.prior_file.read_text(encoding="utf-8"))["stores_answer_meta"] is False


def test_prior_release_does_not_write_a_record_when_the_writers_cannot_be_run(bench, capsys, prior, monkeypatch):
    def refuse(repo, commit):
        raise packet.Refused("The writers of the prior commit could not be run")

    monkeypatch.setattr(packet, "prior_writes_answer_meta", refuse)
    code, out, err = run(bench.prior_argv(), capsys)
    assert code == 1 and "writers" in err and not bench.prior_file.exists()


def test_the_real_derivation_reads_a80_as_not_storing_the_state_and_release_a_as_storing_it():
    assert packet.prior_writes_answer_meta(pw.ROOT, "a80be1dee7f4ee2aa9775d0f353bab930809f057") is False
    assert packet.prior_writes_answer_meta(pw.ROOT, rw.A_COMMIT) is True


def test_the_real_derivation_refuses_a_commit_the_repository_does_not_hold():
    with pytest.raises(packet.Refused):
        packet.prior_writes_answer_meta(pw.ROOT, "0" * 40)


def test_prior_release_is_what_the_helper_and_the_live_system_of_a_agree_on(bench, capsys, prior):
    assert run(bench.prior_argv(), capsys)[0] == 0
    record = json.loads(bench.prior_file.read_text(encoding="utf-8"))
    baseline = so.capture_baseline(rw.FakeReader(bench.world), "2026-10-09T10:00:00+00:00", prior=record)
    assert baseline["services"][API]["serving"]["name"] == record["revisions"][API]


def corrupt(path, change):
    pw.edit_json(path, change)


def spoil(prior, victim):
    """Break one tie of A's packet and leave every other tie intact."""
    if victim == "bindings edited after the lock":
        prior.bindings.write_bytes(prior.bindings.read_bytes() + b" ")
    elif victim == "manifest edited without its hash":
        corrupt(prior.manifest, lambda m: m["image"].update(digest="sha256:" + "9" * 64))
    elif victim == "manifest sidecar differs":
        prior.sidecar.write_text("0" * 64 + "\n", encoding="utf-8")
    elif victim == "manifest rewritten with its hash and sidecar":
        manifest = json.loads(prior.manifest.read_text(encoding="utf-8"))
        manifest["image"]["digest"] = "sha256:" + "9" * 64
        manifest["image"]["registry_digest"] = manifest["image"]["digest"]
        manifest["manifest_sha256"] = so.manifest_hash({k: v for k, v in manifest.items() if k != "manifest_sha256"})
        prior.write(prior.manifest, manifest)
        prior.sidecar.write_text(manifest["manifest_sha256"] + "\n", encoding="utf-8", newline="\n")
    elif victim == "manifest field stale while sidecar and readback agree on the recomputed hash":
        manifest = json.loads(prior.manifest.read_text(encoding="utf-8"))
        manifest["image"]["digest"] = manifest["image"]["registry_digest"] = "sha256:" + "9" * 64
        manifest["image"]["reference"] = f"{rw.REPO}@{manifest['image']['digest']}"
        recomputed = so.manifest_hash(manifest)
        prior.write(prior.manifest, manifest)
        prior.sidecar.write_text(recomputed + "\n", encoding="utf-8", newline="\n")
        corrupt(prior.readback, lambda r: r.update(manifest_sha256=recomputed))
        corrupt(prior.ledger, lambda led: [e.update(digest=manifest["image"]["reference"], manifest_sha256=recomputed) for e in led["events"]])
    elif victim == "no AfterPromotion readback":
        prior.readback.unlink()
    elif victim == "readback of another release":
        corrupt(prior.readback, lambda r: r.update(release_id="rel-aaaaaaa-01"))
    elif victim == "readback of another phase":
        corrupt(prior.readback, lambda r: r.update(phase="AfterSmoke"))
    elif victim == "release was rolled back":
        prior.write(prior.packet / "readbacks" / "AfterRollback-01.json", {"schema_version": 1, "phase": "AfterRollback"})
    elif victim == "release tags were retired":
        prior.write(prior.packet / "readbacks" / "AfterRetire-01.json", {"schema_version": 1, "phase": "AfterRetire"})
    elif victim == "ledger lacks the serving event of the API":
        corrupt(prior.ledger, lambda led: led.update(events=[e for e in led["events"] if not (e["state"] == "SERVING" and e["service"] == API)]))
    elif victim == "ledger digest differs from the manifest":
        corrupt(prior.ledger, lambda led: [e.update(digest=f"{rw.REPO}@{rw.OTHER_DIGEST}") for e in led["events"] if e["state"] == "SERVING"])
    elif victim == "review rejects":
        corrupt(prior.review, lambda r: r.update(verdict="REJECT"))
    elif victim == "review binds another lock":
        corrupt(prior.review, lambda r: r.update(lock_sha256="0" * 64))
    elif victim == "review is of another commit":
        corrupt(prior.review, lambda r: r.update(target="b" * 40))
    elif victim == "lock is of another release":
        corrupt(prior.lock, lambda lock: lock.update(release_id="rel-aaaaaaa-01"))
    elif victim == "bindings name another agent tag URL":
        corrupt(prior.bindings, lambda b: b["envDeltas"]["f42-api"].update(AGENT_URL=so.tag_url("rel-aaaaaaa-01", rw.CANON[AGENT])))
    elif victim == "bindings name another audience":
        corrupt(prior.bindings, lambda b: b["envDeltas"]["f42-api"].update(AGENT_AUDIENCE=rw.A_TAG_URL[AGENT]))
    elif victim == "manifest source is another commit":
        manifest = json.loads(prior.manifest.read_text(encoding="utf-8"))
        manifest["source"]["commit"] = "b" * 40
        prior.seal(manifest)
    else:
        raise AssertionError(victim)


VICTIMS = ["bindings edited after the lock", "manifest edited without its hash", "manifest sidecar differs",
           "manifest rewritten with its hash and sidecar", "manifest field stale while sidecar and readback agree on the recomputed hash",
           "no AfterPromotion readback", "readback of another release",
           "readback of another phase", "release was rolled back", "release tags were retired",
           "ledger lacks the serving event of the API", "ledger digest differs from the manifest", "review rejects",
           "review binds another lock", "review is of another commit", "lock is of another release",
           "bindings name another agent tag URL", "bindings name another audience", "manifest source is another commit"]


@pytest.mark.parametrize("victim", VICTIMS)
def test_prior_release_refuses_a_prior_packet_whose_ties_do_not_hold_and_writes_nothing(bench, capsys, prior, victim):
    spoil(prior, victim)
    if victim == "manifest source is another commit":
        # the sidecar and the readback were re-sealed with the edit, so the hashes agree and only the commit tie can refuse it
        assert json.loads(prior.manifest.read_text(encoding="utf-8"))["source"]["commit"] == "b" * 40
    code, out, err = run(bench.prior_argv(), capsys)
    assert code == 1 and "REFUSED" in err, (victim, err)
    assert not bench.prior_file.exists(), victim


def test_prior_release_never_replaces_a_record(bench, capsys, prior):
    assert run(bench.prior_argv(), capsys)[0] == 0
    before = bench.prior_file.read_bytes()
    code, out, err = run(bench.prior_argv(), capsys)
    assert code == 1 and "REFUSED" in err and bench.prior_file.read_bytes() == before


def test_prior_release_needs_a_lock_a_packet_directory_and_a_destination(bench, capsys, prior):
    for flag in ("--prior-lock", "--prior-packet-dir", "--out"):
        argv = bench.prior_argv()
        index = argv.index(flag)
        with pytest.raises(SystemExit) as stopped:
            packet.main(argv[:index] + argv[index + 2:])
        assert stopped.value.code == 2, flag


def test_prior_release_prints_paths_and_a_hash_only(bench, capsys, prior):
    code, out, err = run(bench.prior_argv(), capsys)
    assert code == 0 and rw.A_COMMIT not in out + err and "https://" not in out + err


# capture-baseline: live must be the recorded prior release, the record is the pin, and the jobs are whatever they are

def test_capture_baseline_accepts_the_state_a_leaves_with_the_jobs_on_another_image(bench, capsys, prior):
    assert run(bench.prior_argv(), capsys)[0] == 0
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 0, err
    baseline = bench.baseline_value()
    assert baseline["api_agent_url_literal"] == rw.A_TAG_URL[AGENT] != baseline["canonical"][AGENT]
    assert {v["image"].rsplit("@", 1)[1] for v in baseline["jobs"].values()} == {rw.B_JOBS_DIGEST}
    asked = pw.calls(bench.log)
    assert asked and all(tuple(a[:3]) in {("run", "services", "describe"), ("run", "revisions", "describe"), ("run", "revisions", "list"),
                                          ("run", "jobs", "describe"), ("run", "services", "get-iam-policy")} for a in asked)


def test_capture_baseline_without_the_record_still_refuses_the_state_a_leaves(tmp_path, monkeypatch, capsys):
    bench = pw.Bench(tmp_path, monkeypatch, world=pw.a_world())
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "REFUSED" in err and not bench.baseline.exists()


def test_capture_baseline_refuses_a80_when_the_record_says_the_prior_release_is_a(tmp_path, monkeypatch, capsys):
    bench = pw.Bench(tmp_path, monkeypatch, after="A", world=pw.a80_world(declared=()))
    bench.prepare_inputs()
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "REFUSED" in err and rw.A_RID in err and not bench.baseline.exists()


def audience_dropped(world):
    world.drop_env(rw.A_REV[API], "AGENT_AUDIENCE")


def api_canonical(world):
    world.set_env(rw.A_REV[API], "AGENT_URL", rw.CANON[AGENT])


def agent_untagged(world):
    world.svc[AGENT]["traffic"] = [{k: v for k, v in e.items() if k != "tag"} for e in world.svc[AGENT]["traffic"]]


def a_job_elsewhere(world):
    container = world.jobs["f42-learn"]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
    container["image"] = container["image"].replace(rw.B_JOBS_DIGEST, rw.OTHER_DIGEST)


@pytest.mark.parametrize("change", [audience_dropped, api_canonical, agent_untagged, a_job_elsewhere])
def test_capture_baseline_refuses_a_drifted_prior_release_and_writes_nothing(tmp_path, monkeypatch, capsys, change):
    world = pw.a_world()
    change(world)
    bench = pw.Bench(tmp_path, monkeypatch, after="A", world=world)
    bench.prepare_inputs()
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "REFUSED" in err and not bench.baseline.exists()


def test_capture_baseline_pins_the_jobs_digest_when_the_lead_names_one(bench, capsys, prior):
    assert run(bench.prior_argv(), capsys)[0] == 0
    code, out, err = run(bench.capture_argv("--expect-jobs-digest", rw.OTHER_DIGEST), capsys)
    assert code == 1 and "jobs" in err and not bench.baseline.exists()
    code, out, err = run(bench.capture_argv("--expect-jobs-digest", rw.B_JOBS_DIGEST), capsys)
    assert code == 0, err
    with pytest.raises(SystemExit) as stopped:
        packet.main(bench.capture_argv("--expect-jobs-digest", "e77c3819"))
    assert stopped.value.code == 2


def test_capture_baseline_refuses_a_prior_release_file_that_is_missing_or_not_a_record(bench, capsys, prior):
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "prior" in err and not bench.baseline.exists()
    assert run(bench.prior_argv(), capsys)[0] == 0
    pw.edit_json(bench.prior_file, lambda r: r.update(extra=1))
    code, out, err = run(bench.capture_argv(), capsys)
    assert code == 1 and "REFUSED" in err and not bench.baseline.exists()


# bind: the record, the tags to retain and the letter go into the bindings, and every receipt is pinned to the prior commit

def test_bind_writes_the_record_with_the_jobs_image_read_from_the_baseline_and_the_tags_derived_from_it(bench, capsys):
    bound_bench(bench, capsys)
    value = bench.bindings_value()
    record = json.loads(bench.prior_file.read_text(encoding="utf-8"))
    assert value["priorRelease"] == {**record, "jobs_image_digest": rw.B_JOBS_DIGEST}
    assert value["retainedTags"] == {name: {rw.A_RID: rw.A_REV[name]} for name in so.SERVICES}
    assert value["retainedAgentUrls"] == [rw.A_TAG_URL[AGENT]]
    assert value["releaseLetter"] == "C"
    assert so.validate_bindings(value)


def test_bind_adds_exactly_four_keys_to_the_key_set_of_an_a80_release(bench, capsys):
    bound_bench(bench, capsys)
    from core.setup import durable_effects_check as dec

    paste = (pw.ROOT / "core/setup/release/SERVICES-PASTE.ps1").read_text(encoding="utf-8")
    checker = open(dec.__file__, encoding="utf-8").read()
    a80_keys = (set(so.REQUIRED_BINDINGS) | {"schema_version"} | set(re.findall(r"\$(?:Script:)?[Bb]ound\.([A-Za-z_]+)", paste)) - {"PSObject"}
                | set(re.findall(r'bindings\.get\("([A-Za-z0-9_]+)"\)', checker)))
    assert set(bench.bindings_value()) == a80_keys | {"priorRelease", "retainedTags", "retainedAgentUrls", "releaseLetter"}


def test_bind_keeps_the_candidate_deltas_the_baseline_and_the_target_derive(bench, capsys):
    bound_bench(bench, capsys)
    value, baseline = bench.bindings_value(), bench.baseline_value()
    agent = baseline["canonical"][AGENT]
    assert value["envDeltas"][API] == {"F42_VERSION": value["target"][:12], "AGENT_URL": so.tag_url(value["release_id"], agent), "AGENT_AUDIENCE": agent}
    release = so.Release(value, rw.FakeReader(bench.world), bench.tmp / "evidence")
    assert release.stage1["prior_release"] == value["priorRelease"]


def test_bind_needs_a_release_letter_when_it_is_given_a_prior_release(bench, capsys):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    argv = bench.bind_argv()
    index = argv.index("--release-letter")
    code, out, err = run(argv[:index] + argv[index + 2:], capsys)
    assert code == 1 and "letter" in err and not bench.bindings.exists()


@pytest.mark.parametrize("letter", ["c", "CC", "1", "", " C"])
def test_bind_refuses_a_release_letter_that_is_not_one_capital(bench, capsys, letter):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    code, out, err = run(bench.bind_argv(**{"--release-letter": letter}), capsys)
    assert code == 1 and "letter" in err and not bench.bindings.exists()


def test_bind_refuses_a_baseline_that_is_not_the_recorded_prior_release(bench, capsys):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    pw.edit_json(bench.baseline, lambda b: b["services"][API]["serving"].update(name="f42-api-00041-lns"))
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and rw.A_RID in err and not bench.bindings.exists()


def test_bind_refuses_a_bind_without_the_prior_release_flag_for_a_baseline_of_a(bench, capsys):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    argv = bench.bind_argv()
    index = argv.index("--prior-release")
    code, out, err = run(argv[:index] + argv[index + 2:], capsys)
    assert code == 1 and "REFUSED" in err and not bench.bindings.exists()


def test_bind_pins_the_jobs_digest_when_the_lead_names_one(bench, capsys):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    code, out, err = run(bench.bind_argv(**{"--expect-jobs-digest": rw.OTHER_DIGEST}), capsys)
    assert code == 1 and "jobs" in err and not bench.bindings.exists()
    assert run(bench.bind_argv(**{"--expect-jobs-digest": rw.B_JOBS_DIGEST}), capsys)[0] == 0


RECEIPTS = ("compat-receipt.json", "old-reader-receipt.json")


@pytest.mark.parametrize("name", RECEIPTS)
@pytest.mark.parametrize("field,value", [("base_commit", "a80be1d"), ("base_commit", None), ("base_commit", "3710c5"),
                                         ("base_commit", "3710c5f"), ("base_commit", "3710c5e" + "0" * 33),
                                         ("candidate_commit", None), ("candidate_commit", "b" * 40), ("candidate_commit", "HEAD")])
def test_bind_refuses_a_receipt_whose_commits_are_not_the_prior_commit_and_the_release_commit(bench, capsys, name, field, value):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0

    def change(receipt):
        if value is None:
            receipt.pop(field)
        else:
            receipt[field] = value

    pw.edit_json(bench.packet / name, change)
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "receipt" in err and not bench.bindings.exists(), (name, field, value)


@pytest.mark.parametrize("change,fragment", [(lambda r: r.update(prior_stores_answer_meta=False), "rules"), (lambda r: r.pop("prior_stores_answer_meta"), "rules"),
                                              (lambda r: r.update(prior_stores_answer_meta="true"), "rules")])
def test_bind_refuses_an_old_reader_receipt_judged_by_other_rules_than_the_record_calls_for(bench, capsys, change, fragment):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    pw.edit_json(bench.packet / "old-reader-receipt.json", change)
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 1 and "old-reader" in err and fragment in err and not bench.bindings.exists()


def test_bind_leaves_the_compat_receipt_out_of_the_rules_it_asks_of_the_old_reader_receipt(bench, capsys):
    bound_bench(bench, capsys)
    assert "prior_stores_answer_meta" not in json.loads((bench.packet / "compat-receipt.json").read_text(encoding="utf-8"))
    assert bench.bindings_value()["priorRelease"]["stores_answer_meta"] is True


@pytest.mark.parametrize("base", ["3710c5e", rw.A_COMMIT, rw.A_COMMIT[:12]])
def test_bind_accepts_the_prior_commit_as_the_base_of_the_receipts_in_short_or_full_form(bench, capsys, base):
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    for name in RECEIPTS:
        pw.edit_json(bench.packet / name, lambda r: r.update(base_commit=base))
    code, out, err = run(bench.bind_argv(), capsys)
    assert code == 0, err


def test_bind_for_a80_asks_nothing_of_the_receipts_commits(tmp_path, monkeypatch, capsys):
    bench = pw.Bench(tmp_path, monkeypatch)
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    assert run(bench.bind_argv(), capsys)[0] == 0 and "priorRelease" not in bench.bindings_value()


# the label: RELEASE C <id>, derived from the letter in the bindings, and the check stays exact

C_LINE = ("RELEASE C {rid}: I authorise exactly one live T1 Ask, market ZA, against the candidate API tag URL only, at most 60 search "
          "credits and a USD 2.00 research budget, with a model hold ceiling of USD 4.92, no second Ask and no retry. The window is "
          "quiet and I will start no manual job until the execution ends. I will type DEPLOY, IDLE and the passcode myself.")


def test_the_template_for_a_letter_is_the_label_and_nothing_else_changed():
    assert packet.authorisation_template("C").format(release_id="rel-0000000-01", hold=packet.hold_ceiling()) == C_LINE.format(rid="rel-0000000-01")
    assert packet.authorisation_template("A") == packet.AUTHORISATION_TEMPLATE
    with pytest.raises(packet.Refused):
        packet.authorisation_template("c")


def test_the_receipt_for_release_c_takes_the_line_that_starts_release_c(bench, capsys):
    reviewed_bench(bench, capsys)
    bench.line.write_text(C_LINE.format(rid=bench.release_id) + "\n", encoding="utf-8")
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 0, err
    assert bench.receipt.is_file()


@pytest.mark.parametrize("label", ["RELEASE A", "RELEASE B", "RELEASE", "Release C", "RELEASE C C"])
def test_the_receipt_for_release_c_refuses_any_other_label(bench, capsys, label):
    reviewed_bench(bench, capsys)
    bench.line.write_text(C_LINE.format(rid=bench.release_id).replace("RELEASE C", label, 1) + "\n", encoding="utf-8")
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "authorisation" in err and not bench.receipt.exists(), label


@pytest.mark.parametrize("edit", [lambda line: line + " I do not authorise this.", lambda line: line.replace("market ZA", "market ZA and market NG"),
                                  lambda line: line.replace("USD 4.92", "USD 49.20"), lambda line: line.replace("exactly one", "exactly two"),
                                  lambda line: line.replace("rel-", "xrel-", 1)])
def test_the_line_for_release_c_is_as_exact_as_the_line_for_release_a(bench, capsys, edit):
    reviewed_bench(bench, capsys)
    bench.line.write_text(edit(C_LINE.format(rid=bench.release_id)) + "\n", encoding="utf-8")
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "authorisation" in err and not bench.receipt.exists()


def test_a_release_bound_the_way_a_was_still_takes_release_a(tmp_path, monkeypatch, capsys):
    bench = pw.Bench(tmp_path, monkeypatch)
    bench.prepare_inputs()
    assert run(bench.capture_argv(), capsys)[0] == 0
    assert run(bench.bind_argv(), capsys)[0] == 0
    assert "releaseLetter" not in bench.bindings_value()
    for argv in (bench.lock_argv(), bench.review_argv()):
        assert run(argv, capsys)[0] == 0
    bench.line.write_text(C_LINE.format(rid=bench.release_id) + "\n", encoding="utf-8")
    assert run(bench.receipt_argv(), capsys)[0] == 1
    bench.line.write_text(pw.authorisation_line(bench.release_id) + "\n", encoding="utf-8")
    assert run(bench.receipt_argv(), capsys)[0] == 0


def test_the_letter_is_read_from_the_bindings_the_lock_hashes_so_a_changed_letter_breaks_the_lock(bench, capsys):
    reviewed_bench(bench, capsys)
    value = bench.bindings_value()
    value["releaseLetter"] = "D"
    bench.bindings.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    bench.line.write_text(C_LINE.format(rid=bench.release_id).replace("RELEASE C", "RELEASE D") + "\n", encoding="utf-8")
    code, out, err = run(bench.receipt_argv(), capsys)
    assert code == 1 and "bindings" in err and not bench.receipt.exists()


def test_the_lock_hashes_the_prior_release_tests_and_the_chain_to_the_receipt_holds(bench, capsys):
    reviewed_bench(bench, capsys)
    lock = json.loads(bench.lock_file().read_text(encoding="utf-8"))
    assert lock["bound_files"]["bindings"] == hashlib.sha256(bench.bindings.read_bytes()).hexdigest()
    for rel in ("core/setup/tests/test_release_prior.py", "core/setup/tests/test_release_prior_packet.py", "core/api/tests/test_release_base_commit.py"):
        assert rel in locklib.TEST_FILES, rel
        assert (pw.ROOT / rel).is_file(), rel
