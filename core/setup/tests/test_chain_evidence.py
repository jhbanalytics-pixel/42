"""The chain evidence producer (W8-REL-B v2.1 section 2, JM-01 to JM-11). Fake gcloud reads and a fake BigQuery client only:
no cloud, no network. A missing prerequisite fails; nothing here skips."""
import copy
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

import pytest

from core.setup.release import bound_readback as helper
from core.setup.release import chain_evidence as ce
from core.setup.release import jobs_only as jo
from core.setup.release import services_only as so
from core.setup.tests import jobs_world as jw
from core.setup.tests import release_world as rw

NOW = dt.datetime(2026, 10, 11, 9, 0, tzinfo=dt.timezone.utc)


def fixture(tmp_path, **kw):
    fix = jw.ChainFixture(tmp_path, **kw)
    return fix


def build(fix, *, role=None, bound_over=None, baseline=None, run_date=None, reader=None, bq=None):
    bound = fix.write_bindings(**(bound_over or {}), **({"baseline": baseline} if baseline else {}))
    return ce.build_manifest(bound, reader or fix.reader(), bq or fix.bq(), run_date or fix.day, role or fix.role, now=lambda: NOW)


def cli(fix, *, role=None, date=None, reader=None, bq=None, extra=()):
    fix.write_bindings() if fix.bound is None else None
    bindings = fix.tmp / "bindings.json"
    bindings.write_text(json.dumps(fix.bound), encoding="utf-8")
    argv = ["--date", (date or fix.day).isoformat(), "--bindings", str(bindings), "--evidence", str(fix.evidence), "--expect", role or fix.role, *extra]
    return ce.main(argv, reader_factory=lambda timeouts: reader or fix.reader(), bq_factory=lambda bound: bq or fix.bq(), now=lambda: NOW)


def reasons(manifest):
    return manifest["verdict"]["reasons"]


TOP_FIELDS = {"schema_version", "kind", "run_date", "role", "read_at_utc", "reader", "jobs_image", "services", "a_terminal", "stages",
              "detect_substages", "side_executions", "verdict", "generated_from", "bytes_billed"}
STAGE_FIELDS = {"run_id", "run_ids", "rows", "execution", "attempt", "timestamps", "configuration", "lineage", "degradation",
                "skipped_duplicates"}


# JM-01

def test_jm01_the_producer_writes_a_manifest_with_every_field_of_section_2_2_from_a_fake_reader_and_a_fake_bigquery(tmp_path, capsys):
    fix = fixture(tmp_path, role="first-b", digest=jw.NEW_DIGEST)
    fix.write_bindings()
    fix.freeze(jw.NEW_DIGEST)
    jw.set_all_jobs_image(fix.world, jw.NEW_DIGEST)
    code = cli(fix)
    assert code == 0, capsys.readouterr().err
    written = fix.manifest_path()
    manifest = json.loads(written.read_text(encoding="utf-8"))
    assert set(manifest) == TOP_FIELDS
    assert manifest["schema_version"] == 1 and manifest["kind"] == "chain-evidence" and manifest["run_date"] == "2026-10-10"
    assert set(manifest["stages"]) == set(jo.CHAIN_STAGES)
    for stage in jo.CHAIN_STAGES:
        assert set(manifest["stages"][stage]) == STAGE_FIELDS, stage
    assert set(manifest["jobs_image"]) == {"expected_digest", "definitions"} and set(manifest["jobs_image"]["definitions"]) == set(so.JOB_NAMES)
    assert set(manifest["verdict"]) == {"qualifies", "baseline_by_line", "failed_stage", "reasons", "decision"}
    assert manifest["a_terminal"] == {"kind": "AfterPromotion", "at_utc": jw.A_TERMINAL_AT}
    assert manifest["generated_from"] == {"head": jw.B_COMMIT}
    assert {q["template"] for q in manifest["bytes_billed"]} >= {"stage_rows", "item_state_count", "series_test_count", "briefs_by_market"}
    assert manifest["reader"]["account"] == jw.CALLER and manifest["reader"]["impersonation"] is None
    assert manifest["reader"]["commands"] and all(re.fullmatch(r"[0-9a-f]{64}", c) for c in manifest["reader"]["commands"])
    assert set(manifest["detect_substages"]) == set(jw.SUBSTAGES)
    assert manifest["verdict"]["qualifies"] is True, manifest["verdict"]


def test_jm01_a_second_run_on_the_same_date_refuses_and_leaves_the_first_file_unchanged(tmp_path, capsys):
    fix = fixture(tmp_path)
    assert cli(fix) == 0
    before = fix.manifest_path().read_bytes()
    assert cli(fix) == 1
    assert "STOP: EXISTS" in capsys.readouterr().err
    assert fix.manifest_path().read_bytes() == before


def test_jm01_the_writer_itself_refuses_to_overwrite_a_file_that_appeared_after_the_check(tmp_path):
    fix = fixture(tmp_path)
    manifest = build(fix)
    path = ce.write_manifest(fix.release_dir, manifest)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        ce.write_manifest(fix.release_dir, {**manifest, "role": "tampered"})
    assert path.read_bytes() == before


def test_jm01_the_file_is_created_exclusively_and_only_in_the_release_directory(tmp_path):
    fix = fixture(tmp_path)
    assert cli(fix) == 0
    assert [p.name for p in fix.release_dir.iterdir()] == ["chain-evidence-2026-10-10.json"]
    assert [p.name for p in fix.evidence.iterdir()] == ["chain-evidence-2026-10-10.run.json"]
    assert "'x'" in Path(ce.__file__).read_text(encoding="utf-8") or '"x"' in Path(ce.__file__).read_text(encoding="utf-8")


# JM-02: one fixture per code of 2.4

def test_jm02_a_clean_chain_qualifies(tmp_path):
    manifest = build(fixture(tmp_path))
    assert reasons(manifest) == [] and manifest["verdict"]["qualifies"] is True
    assert manifest["verdict"]["failed_stage"] is None and manifest["verdict"]["baseline_by_line"] is False


def test_jm02_image_when_a_stage_execution_ran_on_another_digest(tmp_path):
    fix = fixture(tmp_path)
    name = "f42-understand-aaaaa"
    fix.world.executions[name] = jw.execution_raw(name, "f42-understand", fix.world.executions[name]["status"]["startTime"],
                                                  fix.world.executions[name]["status"]["completionTime"], jw.OLD_DIGEST)
    manifest = build(fix)
    assert reasons(manifest) == ["IMAGE"] and manifest["verdict"]["failed_stage"] == "understand"


def test_jm02_window_when_collect_started_before_the_terminal_readback_of_release_a(tmp_path):
    fix = fixture(tmp_path)
    manifest = build(fix, baseline=fix.baseline_j(at_utc="2026-10-10T00:10:00+00:00"))
    assert reasons(manifest) == ["WINDOW"] and manifest["verdict"]["failed_stage"] == "collect"


def test_jm02_window_holds_for_a_release_a_that_was_rolled_back_too(tmp_path):
    fix = fixture(tmp_path)
    manifest = build(fix, baseline=fix.baseline_j(a_kind="AfterRollback", at_utc="2026-10-10T00:10:00+00:00"))
    assert reasons(manifest) == ["WINDOW"] and manifest["a_terminal"]["kind"] == "AfterRollback"


def test_jm02_not_one_run_when_a_stage_has_two_non_skipped_run_ids(tmp_path):
    fix = fixture(tmp_path)
    fix.add_stage("understand", run_id="understand-20261010-early", execution="f42-understand-early", terminal=False,
                  with_execution=False)
    # the early run started before the one that finished
    fix.rows[-1]["started_at"] -= dt.timedelta(minutes=30)
    manifest = build(fix)
    assert reasons(manifest) == ["NOT_ONE_RUN"]


def test_jm02_not_one_run_when_a_stage_has_no_run_at_all(tmp_path):
    fix = fixture(tmp_path)
    fix.rows = [r for r in fix.rows if r["stage"] != "detect"]
    manifest = build(fix)
    assert "NOT_ONE_RUN" in reasons(manifest) and manifest["stages"]["detect"]["run_id"] is None


def test_jm02_terminal_when_the_newest_row_of_a_stage_is_not_a_finished_ok_row(tmp_path):
    fix = fixture(tmp_path)
    fix.set_terminal("brief", status="failed")
    assert reasons(build(fix)) == ["TERMINAL"]
    fix2 = fixture(tmp_path / "two")
    fix2.rows = [r for r in fix2.rows if not (r["stage"] == "brief" and r["status"] == "ok")]
    assert reasons(build(fix2)) == ["TERMINAL"]


@pytest.mark.parametrize("how", ["missing", "other_job", "started_after_the_row", "completed_before_the_row"])
def test_jm02_bound_three_ways(tmp_path, how):
    fix = fixture(tmp_path)
    started, finished = fix.times("detect")
    if how == "missing":
        del fix.world.executions["f42-detect-aaaaa"]
    elif how == "other_job":
        fix.world.executions["f42-detect-aaaaa"] = jw.execution_raw("f42-detect-aaaaa", "f42-brief", started.isoformat(),
                                                                     (finished + dt.timedelta(seconds=10)).isoformat(), fix.digest)
    elif how == "started_after_the_row":
        fix.world.executions["f42-detect-aaaaa"]["status"]["startTime"] = (started + dt.timedelta(seconds=60)).isoformat()
    else:
        fix.world.executions["f42-detect-aaaaa"]["status"]["completionTime"] = (finished - dt.timedelta(seconds=60)).isoformat()
    manifest = build(fix)
    assert reasons(manifest) == ["BOUND"] and manifest["verdict"]["failed_stage"] == "detect"


@pytest.mark.parametrize("change", [{"succeededCount": 0}, {"failedCount": 1}, {"cancelledCount": 1}])
def test_jm02_bound_when_the_execution_counts_are_not_one_succeeded_none_failed_none_cancelled(tmp_path, change):
    fix = fixture(tmp_path)
    fix.world.executions["f42-collect-aaaaa"]["status"].update(change)
    assert reasons(build(fix)) == ["BOUND"]


def test_jm02_bound_when_the_running_row_names_no_execution(tmp_path):
    fix = fixture(tmp_path)
    running = next(r for r in fix.rows if r["stage"] == "understand" and r["status"] == "running")
    running["counts"] = None
    # the understand execution is now referenced by no row, so it also counts as a start outside the chain
    assert sorted(reasons(build(fix))) == ["BOUND", "MANUAL"]


def test_jm02_retried_reports_not_one_run_with_it(tmp_path):
    fix = fixture(tmp_path)
    run_id, _ = fix.add_stage("detect", run_id="detect-20261010-retry", execution="f42-detect-aaaaa", terminal=False, with_execution=False)
    fix.rows[-1]["started_at"] -= dt.timedelta(minutes=10)
    manifest = build(fix)
    assert sorted(reasons(manifest)) == ["NOT_ONE_RUN", "RETRIED"]


def test_jm02_retried_when_the_execution_reports_a_retried_count(tmp_path):
    fix = fixture(tmp_path)
    fix.world.executions["f42-detect-aaaaa"]["status"]["retriedCount"] = 1
    assert reasons(build(fix)) == ["RETRIED"]


def test_jm02_order_when_the_brief_finished_after_0615_sast(tmp_path):
    fix = fixture(tmp_path)
    late = fix.midnight + dt.timedelta(minutes=380)
    fix.set_terminal("brief", finished_at=late)
    fix.world.executions["f42-brief-aaaaa"]["status"]["completionTime"] = (late + dt.timedelta(seconds=10)).isoformat()
    assert reasons(build(fix)) == ["ORDER"]


def test_jm02_order_when_a_stage_started_before_the_stage_before_it_finished(tmp_path):
    fix = fixture(tmp_path)
    early = fix.midnight + dt.timedelta(minutes=170)
    for row in fix.rows:
        if row["stage"] == "detect" and row["status"] == "running":
            row["started_at"] = early
        if row["stage"] == "detect" and row["status"] == "ok":
            row["started_at"] = early
    fix.world.executions["f42-detect-aaaaa"]["status"]["startTime"] = (early - dt.timedelta(seconds=5)).isoformat()
    assert reasons(build(fix)) == ["ORDER"]


def test_jm02_order_when_collect_did_not_start_near_0200_sast_and_records_the_gap(tmp_path):
    fix = fixture(tmp_path, schedule={"collect": 140}, length={"collect": 15})
    manifest = build(fix)
    assert reasons(manifest) == ["ORDER"]
    assert manifest["stages"]["collect"]["timestamps"]["scheduled_gap_seconds"] == 20 * 60 + 5


def test_jm02_upstream_when_the_stage_before_did_not_end_ok(tmp_path):
    fix = fixture(tmp_path)
    fix.set_terminal("understand", status="blocked")
    assert sorted(reasons(build(fix))) == ["TERMINAL", "UPSTREAM"]


def test_jm02_substages_when_a_detect_substage_row_is_not_ok(tmp_path):
    fix = fixture(tmp_path)
    for row in fix.rows:
        if row["stage"] == "seeds":
            row["status"] = "failed"
    manifest = build(fix)
    assert reasons(manifest) == ["SUBSTAGES"] and manifest["detect_substages"]["seeds"]["status"] == "failed"


def test_jm02_lineage_when_detect_wrote_no_item_state_or_a_market_has_no_brief(tmp_path):
    fix = fixture(tmp_path)
    fix.outputs["item_state"] = 0
    assert reasons(build(fix)) == ["LINEAGE"]
    fix2 = fixture(tmp_path / "two")
    fix2.outputs["briefs"].pop("KE")
    assert reasons(build(fix2)) == ["LINEAGE"]
    fix3 = fixture(tmp_path / "three")
    fix3.outputs["series_test"] = 0
    assert reasons(build(fix3)) == ["LINEAGE"]


@pytest.mark.parametrize("change", [{"enrich_error": "RuntimeError: x"}, {"embed_error": "RuntimeError: y"}, {"partial": True},
                                    {"partial": True, "partial_reason": "index_failed"}])
def test_jm02_degraded_when_understand_shows_an_error_or_any_partial(tmp_path, change):
    fix = fixture(tmp_path)
    fix.set_counts("understand", **change)
    manifest = build(fix)
    assert reasons(manifest) == ["DEGRADED"] and manifest["verdict"]["failed_stage"] == "understand"


def test_jm02_degraded_when_a_market_carries_a_cluster_error(tmp_path):
    fix = fixture(tmp_path)
    fix.set_cluster_error("NG", "RuntimeError: umap failed")
    assert reasons(build(fix)) == ["DEGRADED"]


def test_jm02_services_when_a_service_differs_from_the_bound_post_a_state(tmp_path):
    fix = fixture(tmp_path)
    bound_state = fix.baseline_j()
    fix.world.svc["f42-api"]["env"]["F42_EXTRA"] = "1"
    manifest = build(fix, baseline=bound_state)
    assert reasons(manifest) == ["SERVICES"]
    assert manifest["services"]["f42-api"]["matches_bound"] is False and manifest["services"]["f42-agent"]["matches_bound"] is True


def test_jm02_manual_when_a_chain_job_ran_outside_the_chain_on_the_run_date(tmp_path):
    fix = fixture(tmp_path)
    fix.side_execution("f42-collect", "f42-collect-manual", fix.midnight + dt.timedelta(minutes=700))
    manifest = build(fix)
    assert reasons(manifest) == ["MANUAL"] and manifest["verdict"]["failed_stage"] == "collect"


def test_jm02_a_skipped_duplicate_start_of_the_brief_is_not_manual(tmp_path):
    fix = fixture(tmp_path)
    assert fix.stage_rows("brief")[-1]["status"] == "skipped_duplicate"
    assert reasons(build(fix)) == []


def test_jm02_the_expected_digest_is_the_rollback_digest_for_a_baseline_and_the_frozen_digest_for_the_first_b_chain(tmp_path):
    fix = fixture(tmp_path)
    assert build(fix)["jobs_image"]["expected_digest"] == jw.ROLLBACK_DIGEST
    fix2 = fixture(tmp_path / "b", role="first-b")
    fix2.write_bindings()
    fix2.freeze(jw.NEW_DIGEST)
    manifest = ce.build_manifest(fix2.bound, fix2.reader(), fix2.bq(), fix2.day, "first-b", now=lambda: NOW)
    assert manifest["jobs_image"]["expected_digest"] == jw.NEW_DIGEST
    assert "IMAGE" in reasons(manifest)  # the chain ran on the rollback digest, not on the frozen one


def test_jm02_the_first_b_chain_reads_the_frozen_digest_only_from_a_manifest_whose_hash_is_recomputed(tmp_path, capsys):
    fix = fixture(tmp_path, role="first-b", digest=jw.NEW_DIGEST)
    fix.write_bindings()
    manifest = fix.freeze(jw.NEW_DIGEST)
    edited = dict(manifest)
    edited["image"] = {**manifest["image"], "digest": jw.ROLLBACK_DIGEST, "registry_digest": jw.ROLLBACK_DIGEST}
    jw.write_json(fix.release_dir / "release-manifest.json", edited)
    assert cli(fix) == 1
    assert "STOP: MANIFEST_HASH" in capsys.readouterr().err
    fix2 = fixture(tmp_path / "none", role="first-b", digest=jw.NEW_DIGEST)
    fix2.write_bindings()
    assert cli(fix2) == 1 and "STOP: MANIFEST_HASH" in capsys.readouterr().err


# JM-03

def test_jm03_the_9_oct_chain_fails_degraded_and_window_and_is_not_a_baseline_by_line(tmp_path):
    fix = fixture(tmp_path, day=dt.date(2026, 10, 9))
    fix.set_counts("understand", index="failed", index_error="BadRequestError: vector index")
    for market in ("KE", "NG", "pan"):
        fix.set_cluster_error(market)
    manifest = build(fix, baseline=fix.baseline_j(at_utc="2026-10-10T08:00:00+00:00"))
    assert sorted(reasons(manifest)) == ["DEGRADED", "WINDOW"]
    assert manifest["verdict"]["qualifies"] is False and manifest["verdict"]["baseline_by_line"] is False
    assert manifest["stages"]["understand"]["degradation"]["index"] == "failed"
    assert sorted(manifest["stages"]["understand"]["degradation"]["cluster_error_markets"]) == ["KE", "NG", "pan"]


def test_jm03_the_same_chain_after_release_a_is_a_baseline_by_line_because_its_errors_match_the_parse_json_signature(tmp_path):
    fix = fixture(tmp_path, day=dt.date(2026, 10, 9))
    fix.set_counts("understand", index="failed", index_error="BadRequestError: vector index")
    for market in ("KE", "NG", "pan"):
        fix.set_cluster_error(market)
    manifest = build(fix, baseline=fix.baseline_j(at_utc="2026-10-08T08:00:00+00:00"))
    assert reasons(manifest) == ["DEGRADED"]
    verdict = manifest["verdict"]
    assert verdict["qualifies"] is False and verdict["baseline_by_line"] is True and verdict["failed_stage"] == "understand"
    assert verdict["decision"] == "W8-REL-B Q3"


def test_jm03_the_8_oct_chain_on_the_older_image_yields_image_and_window(tmp_path):
    fix = fixture(tmp_path, day=dt.date(2026, 10, 8), digest=jw.OLD_DIGEST)
    fix.set_counts("understand", index="failed", index_error="BadRequestError: vector index")
    terminal = [r for r in fix.stage_rows("understand") if r["status"] == "ok"][-1]
    counts = json.loads(terminal["counts"])
    counts["cluster"]["NG"]["net_failed"] = True
    terminal["counts"] = json.dumps(counts)
    manifest = build(fix, baseline=fix.baseline_j(at_utc="2026-10-10T08:00:00+00:00"))
    assert sorted(reasons(manifest)) == ["IMAGE", "WINDOW"]
    assert manifest["stages"]["understand"]["degradation"]["markets_net_failed"] == ["NG"]


def test_jm03_an_index_failure_alone_on_the_bound_image_after_release_a_qualifies(tmp_path):
    fix = fixture(tmp_path)
    fix.set_counts("understand", index="failed", index_error="BadRequestError: vector index could not be created")
    manifest = build(fix)
    assert reasons(manifest) == [] and manifest["verdict"]["qualifies"] is True and manifest["verdict"]["baseline_by_line"] is False
    assert manifest["stages"]["understand"]["degradation"]["index"] == "failed"
    assert manifest["stages"]["understand"]["degradation"]["index_error"] == "BadRequestError: vector index could not be created"


# JM-04

def test_jm04_two_run_ids_on_one_execution_are_retried_and_not_one_run_and_the_attempt_names_both(tmp_path):
    fix = fixture(tmp_path)
    fix.add_stage("understand", run_id="understand-20261010-died", execution="f42-understand-aaaaa", terminal=False, with_execution=False)
    fix.rows[-1]["started_at"] -= dt.timedelta(minutes=5)
    manifest = build(fix)
    assert sorted(reasons(manifest)) == ["NOT_ONE_RUN", "RETRIED"]
    block = manifest["stages"]["understand"]
    chronological = ["understand-20261010-died", "understand-20261010-aaaaaaaaaaaa"]
    assert block["attempt"]["inferred"] == {"run_ids": chronological, "position": 2, "of": 2}
    assert block["run_id"] is None and block["run_ids"] == chronological


def test_jm04_a_skipped_duplicate_row_is_listed_without_an_execution_name_and_does_not_disqualify(tmp_path):
    fix = fixture(tmp_path)
    manifest = build(fix)
    listed = manifest["stages"]["brief"]["skipped_duplicates"]
    assert len(listed) == 1 and set(listed[0]) == {"run_id", "started_at", "error_sha256"}
    assert listed[0]["error_sha256"] == jw.sha("already ran ok") and reasons(manifest) == []


# JM-05

def test_jm05_the_terminal_row_is_chosen_by_the_order_of_fb_1_and_its_version_equals_the_row_count(tmp_path):
    fix = fixture(tmp_path)
    fix.rows.reverse()
    block = build(fix)["stages"]["collect"]
    assert block["rows"]["count"] == 2 and block["rows"]["terminal"]["version"] == 2 and block["rows"]["terminal"]["status"] == "ok"
    assert block["rows"]["running"]["execution"] == "f42-collect-aaaaa"


def test_jm05_a_terminal_row_with_the_same_time_as_the_running_row_is_still_the_newest(tmp_path):
    fix = fixture(tmp_path)
    running = next(r for r in fix.rows if r["stage"] == "understand" and r["status"] == "running")
    terminal = next(r for r in fix.rows if r["stage"] == "understand" and r["status"] == "ok")
    terminal["finished_at"] = running["started_at"]
    fix.rows[:] = [r for r in fix.rows if r is not terminal] + [terminal]
    fix.rows.reverse()
    terminal_block = build(fix)["stages"]["understand"]["rows"]["terminal"]
    assert terminal_block["version"] == 2 and terminal_block["status"] == "ok"


# JM-06

def test_jm06_the_digest_is_read_from_describe_never_from_list(tmp_path):
    fix = fixture(tmp_path)
    base = fix.reader()
    listed = base.executions("f42-collect")
    assert jw.ROLLBACK_DIGEST[7:] in json.dumps(listed)
    fix.world.described["f42-collect-aaaaa"] = jw.execution_raw(
        "f42-collect-aaaaa", "f42-collect", fix.world.executions["f42-collect-aaaaa"]["status"]["startTime"],
        fix.world.executions["f42-collect-aaaaa"]["status"]["completionTime"], jw.OLD_DIGEST)
    manifest = build(fix)
    assert manifest["stages"]["collect"]["execution"]["image_digest"] == jw.OLD_DIGEST and len(jw.OLD_DIGEST) == 71
    assert reasons(manifest) == ["IMAGE"]


def test_jm06_a_describe_with_no_digest_form_is_a_probe_and_exit_3_even_when_list_has_one(tmp_path, capsys):
    fix = fixture(tmp_path)
    fix.world.described["f42-collect-aaaaa"] = jw.execution_raw(
        "f42-collect-aaaaa", "f42-collect", fix.world.executions["f42-collect-aaaaa"]["status"]["startTime"],
        fix.world.executions["f42-collect-aaaaa"]["status"]["completionTime"], jw.ROLLBACK_DIGEST, with_digest=False)
    assert jw.ROLLBACK_DIGEST[7:] in json.dumps(fix.reader().executions("f42-collect"))
    assert cli(fix) == 3
    assert "PROBE" in capsys.readouterr().err and not fix.manifest_path().exists()


# JM-07

def test_jm07_net_failed_and_an_index_failure_are_recorded_and_do_not_disqualify(tmp_path):
    fix = fixture(tmp_path)
    fix.set_counts("understand", index="failed", index_error="BadRequestError: x")
    terminal = [r for r in fix.stage_rows("understand") if r["status"] == "ok"][-1]
    counts = json.loads(terminal["counts"])
    counts["cluster"]["NG"]["net_failed"] = True
    terminal["counts"] = json.dumps(counts)
    manifest = build(fix)
    assert reasons(manifest) == []
    degradation = manifest["stages"]["understand"]["degradation"]
    assert degradation["index"] == "failed" and degradation["markets_net_failed"] == ["NG"]


@pytest.mark.parametrize("extra", [{"partial": True}, {"partial": True, "partial_reason": "index_failed"},
                                   {"partial": True, "partial_reason": "cluster_stack_failed"}])
def test_jm07_partial_true_for_any_reason_disqualifies_and_is_never_a_baseline_by_line(tmp_path, extra):
    fix = fixture(tmp_path)
    for market in ("KE", "NG"):
        fix.set_cluster_error(market)
    fix.set_counts("understand", **extra)
    manifest = build(fix)
    assert reasons(manifest) == ["DEGRADED"]
    assert manifest["verdict"]["baseline_by_line"] is False and manifest["verdict"]["qualifies"] is False


@pytest.mark.parametrize("extra", [{"enrich_error": "RuntimeError: z"}, {"embed_error": "RuntimeError: z"}])
def test_jm07_an_enrich_or_embed_error_beside_the_parse_json_errors_is_not_a_baseline_by_line(tmp_path, extra):
    fix = fixture(tmp_path)
    fix.set_cluster_error("KE")
    fix.set_counts("understand", **extra)
    manifest = build(fix)
    assert reasons(manifest) == ["DEGRADED"] and manifest["verdict"]["baseline_by_line"] is False


def test_jm07_a_cluster_error_that_is_not_the_parse_json_signature_is_not_a_baseline_by_line(tmp_path):
    fix = fixture(tmp_path)
    fix.set_cluster_error("KE", "RuntimeError: the clusterer ran out of memory")
    manifest = build(fix)
    assert reasons(manifest) == ["DEGRADED"] and manifest["verdict"]["baseline_by_line"] is False


def test_jm07_the_signature_needs_both_halves_and_only_the_first_line_counts(tmp_path):
    fix = fixture(tmp_path)
    fix.set_cluster_error("KE", "ValueError: cannot round-trip through string representation")
    assert build(fix)["verdict"]["baseline_by_line"] is False
    fix2 = fixture(tmp_path / "two")
    fix2.set_cluster_error("KE", "ValueError: boom\n" + jw.PARSE_JSON_ERROR)
    assert build(fix2)["verdict"]["baseline_by_line"] is False


def test_jm07_a_baseline_by_line_is_only_ever_claimed_for_the_baseline_role(tmp_path):
    fix = fixture(tmp_path, role="first-b")
    fix.write_bindings()
    fix.freeze(jw.ROLLBACK_DIGEST)
    fix.set_cluster_error("KE")
    manifest = ce.build_manifest(fix.bound, fix.reader(), fix.bq(), fix.day, "first-b", now=lambda: NOW)
    assert reasons(manifest) == ["DEGRADED"] and manifest["verdict"]["baseline_by_line"] is False


# JM-08

def test_jm08_the_command_set_is_the_read_prefixes_plus_exactly_the_two_executions_prefixes():
    added = set(helper.READ_PREFIXES) - {("config", "list"), ("run", "services", "describe"), ("run", "services", "get-iam-policy"),
                                         ("run", "revisions", "describe"), ("run", "revisions", "list"), ("run", "jobs", "describe"),
                                         ("artifacts", "docker", "images", "describe"), ("builds", "describe"), ("auth", "print-access-token")}
    assert added == {("run", "jobs", "executions", "list"), ("run", "jobs", "executions", "describe")}


@pytest.mark.parametrize("argv", [["run", "jobs", "execute", "f42-collect"], ["run", "jobs", "executions", "cancel", "x"],
                                  ["run", "jobs", "update", "f42-collect"], ["scheduler", "jobs", "list"], ["bq", "query"],
                                  ["run", "jobs", "executions", "delete", "x"]])
def test_jm08_any_other_argv_is_write_refused_before_it_runs(monkeypatch, argv):
    reader = helper.GcloudReader({"gcloud": 5, "http": 5})
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **k: pytest.fail("a refused command was run"))
    with pytest.raises(so.Stop) as stop:
        reader._gcloud(argv)
    assert stop.value.code == "WRITE_REFUSED"


def test_jm08_the_real_reader_issues_the_two_executions_reads_with_the_project_and_region_pinned(monkeypatch):
    seen = []

    class Done:
        returncode, stdout, stderr = 0, "[]", ""

    monkeypatch.setattr(helper.subprocess, "run", lambda argv, **k: seen.append(argv) or Done())
    reader = helper.GcloudReader({"gcloud": 5, "http": 5})
    reader.executions("f42-collect")
    reader.execution("f42-collect-abc12")
    tails = [a[1:] for a in seen]
    assert tails == [["run", "jobs", "executions", "list", "--job", "f42-collect", "--project", "ogilvy-trends-v2", "--region", "us-central1",
                      "--format=json"],
                     ["run", "jobs", "executions", "describe", "f42-collect-abc12", "--project", "ogilvy-trends-v2", "--region", "us-central1",
                      "--format=json"]]
    assert reader.argv_log == tails


def test_jm08_every_template_is_one_select_with_its_partition_filter_and_no_write_verb():
    assert set(ce.TEMPLATES) == {"stage_rows", "item_state_count", "series_test_count", "briefs_by_market", "item_locality_count", "table_exists"}
    for name, text in ce.TEMPLATES.items():
        assert text.lstrip().upper().startswith("SELECT"), name
        assert ";" not in text, name
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|MERGE|TRUNCATE|ALTER|REPLACE|EXPORT|CALL)\b", text, re.I), name
    assert "run_date = @d" in ce.TEMPLATES["stage_rows"] and "stage IN UNNEST(@stages)" in ce.TEMPLATES["stage_rows"]
    assert "metric_date = @d" in ce.TEMPLATES["item_state_count"] and "run_id = @run_id" in ce.TEMPLATES["item_state_count"]
    assert "metric_date = @d" in ce.TEMPLATES["series_test_count"]
    assert "brief_date = @d" in ce.TEMPLATES["briefs_by_market"] and "run_id = @run_id" in ce.TEMPLATES["briefs_by_market"]
    assert "run_date = @d" in ce.TEMPLATES["item_locality_count"] and "detect_run_id = @run_id" in ce.TEMPLATES["item_locality_count"]


def test_jm08_each_query_is_a_dry_run_first_then_a_capped_run_and_the_manifest_records_both(tmp_path):
    fix = fixture(tmp_path)
    bq = fix.bq(estimate=4096, billed=2048)
    manifest = build(fix, bq=bq)
    kinds = [entry[0] for entry in bq.log]
    assert kinds[0] == "dry_run" and kinds.count("dry_run") == kinds.count("run")
    for index in range(0, len(kinds), 2):
        assert kinds[index:index + 2] == ["dry_run", "run"] and bq.log[index][1] == bq.log[index + 1][1]
        assert bq.log[index + 1][3] == 64 * 1024 * 1024
    entry = manifest["bytes_billed"][0]
    assert entry["dry_run_bytes"] == 4096 and entry["billed_bytes"] == 2048 and entry["cap"] == 64 * 1024 * 1024
    assert entry["sha256"] == hashlib.sha256(ce.TEMPLATES[entry["template"]].encode("utf-8")).hexdigest()


def test_jm08_a_dry_run_over_the_cap_is_refused_before_the_query_runs(tmp_path, capsys):
    fix = fixture(tmp_path)
    bq = fix.bq(estimate=64 * 1024 * 1024 + 1)
    assert cli(fix, bq=bq) == 1
    assert "STOP: BYTES_CAP" in capsys.readouterr().err
    assert all(entry[0] == "dry_run" for entry in bq.log) and not fix.manifest_path().exists()


def test_jm08_the_hashes_the_bindings_bind_are_recomputed_against_the_templates_and_a_changed_template_stops(tmp_path, capsys, monkeypatch):
    fix = fixture(tmp_path)
    fix.write_bindings()
    changed = dict(ce.TEMPLATES)
    changed["stage_rows"] = changed["stage_rows"] + " LIMIT 5"
    monkeypatch.setattr(ce, "TEMPLATES", changed)
    assert cli(fix) == 1
    assert "STOP: BINDINGS" in capsys.readouterr().err and not fix.manifest_path().exists()


def test_jm08_the_producer_never_runs_the_bq_command_line_tool():
    text = Path(ce.__file__).read_text(encoding="utf-8")
    assert "subprocess" not in text and not re.search(r"""["']bq["']|bq query|bq\.exe""", text)


# JM-09

SENTINELS = ("https://example.com/post/9981", "@somecreator_handle", "ya29.A0ARrdaM-sentinel-token-value-123456789", "ENVVALUE-SENTINEL-77")


def test_jm09_no_post_text_url_handle_token_or_environment_value_reaches_the_manifest(tmp_path):
    fix = fixture(tmp_path)
    text = f"RuntimeError: bad post by {SENTINELS[1]} see {SENTINELS[0]} token {SENTINELS[2]}\nsecond line {SENTINELS[0]}"
    fix.set_counts("understand", enrich_error=text, index="failed", index_error=text)
    fix.set_cluster_error("KE", text)
    running = next(r for r in fix.rows if r["stage"] == "collect" and r["status"] == "running")
    fix.world.jobs["f42-collect"]["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "F42_SECRETISH", "value": SENTINELS[3]})
    fix.set_terminal("detect", counts=json.dumps({"items": 300, "note": f"{SENTINELS[1]} {SENTINELS[0]}"}))
    manifest = build(fix)
    dumped = json.dumps(manifest)
    for sentinel in SENTINELS:
        assert sentinel not in dumped, sentinel
    assert "second line" not in dumped
    error = manifest["stages"]["understand"]["rows"]["terminal"]["counts"]["enrich_error"]
    assert "\n" not in error and "http" not in error and error.startswith("RuntimeError: bad post by")


def test_jm09_environment_values_appear_only_as_hashes_and_the_error_column_only_as_a_hash(tmp_path):
    fix = fixture(tmp_path)
    manifest = build(fix)
    dumped = json.dumps(manifest)
    assert jw.rw.PROJECT not in dumped.replace("ogilvy-trends-v2/intelligence-42", "")
    terminal = manifest["stages"]["collect"]["rows"]["terminal"]
    assert "error" not in terminal and terminal["has_error"] is False and re.fullmatch(r"[0-9a-f]{64}", terminal["error_sha256"])
    config = manifest["stages"]["collect"]["configuration"]
    assert re.fullmatch(r"[0-9a-f]{64}", config["job_view_sha256"]) and re.fullmatch(r"[0-9a-f]{64}", config["env_names_sha256"])


# JM-10

def test_jm10_a_stalled_read_is_exit_3_and_writes_nothing(tmp_path, capsys):
    fix = fixture(tmp_path)
    fix.world.executions_unreadable = True
    assert cli(fix) == 3
    assert "PROBE" in capsys.readouterr().err
    assert not fix.manifest_path().exists() and list(fix.release_dir.iterdir()) == []


def test_jm10_a_bigquery_read_that_cannot_complete_is_exit_3_and_writes_nothing(tmp_path, capsys):
    fix = fixture(tmp_path)

    class Stalled(jw.FakeBq):
        def run(self, sql, params, max_bytes):
            raise so.Probe("A read exceeded its bound timeout")

    assert cli(fix, bq=Stalled(fix.tables())) == 3
    assert not fix.manifest_path().exists()


def test_jm10_a_contradiction_is_exit_1_and_writes_nothing(tmp_path, capsys):
    fix = fixture(tmp_path)
    other = copy.deepcopy(fix.world.executions["f42-collect-aaaaa"])
    other["metadata"]["name"] = "f42-collect-someone-else"
    fix.world.described["f42-collect-aaaaa"] = other
    assert cli(fix) == 1
    assert "STOP: EXECUTION_IDENTITY" in capsys.readouterr().err and not fix.manifest_path().exists()


def test_jm10_bindings_that_are_malformed_or_a_baseline_whose_hash_changed_stop_with_bindings(tmp_path, capsys):
    fix = fixture(tmp_path)
    fix.write_bindings()
    fix.bound.pop("collectStartToleranceMinutes")
    assert cli(fix) == 1 and "STOP: BINDINGS" in capsys.readouterr().err
    fix2 = fixture(tmp_path / "two")
    fix2.write_bindings()
    Path(fix2.bound["baselinePath"]).write_text(json.dumps({"schema_version": 1, "kind": "baseline-J"}), encoding="utf-8")
    assert cli(fix2) == 1 and "STOP: BINDINGS" in capsys.readouterr().err
    assert not fix2.manifest_path().exists()


def test_jm10_the_main_catches_a_probe_and_a_stop_by_class_and_never_prints_a_raw_exception_text(tmp_path, capsys):
    fix = fixture(tmp_path)
    fix.write_bindings(rollbackJobsDigest="not-a-digest")
    assert cli(fix) == 1
    err = capsys.readouterr().err
    assert err.startswith("STOP: BINDINGS") and "Traceback" not in err


# JM-11

def test_jm11_lineage_counts_are_read_only_for_tables_that_exist_and_a_missing_locality_table_is_null_not_lineage(tmp_path):
    fix = fixture(tmp_path)
    bq = fix.bq()
    manifest = build(fix, bq=bq)
    assert manifest["stages"]["detect"]["lineage"]["outputs"]["item_locality"] is None and reasons(manifest) == []
    asked = [e[1] for e in bq.log if e[0] == "run"]
    assert ce.TEMPLATES["table_exists"] in asked and ce.TEMPLATES["item_locality_count"] not in asked


def test_jm11_when_the_locality_table_exists_its_count_is_read_and_recorded(tmp_path):
    fix = fixture(tmp_path)
    fix.locality_table = True
    fix.outputs["item_locality"] = 77
    bq = fix.bq()
    manifest = build(fix, bq=bq)
    assert manifest["stages"]["detect"]["lineage"]["outputs"]["item_locality"] == 77
    assert ce.TEMPLATES["item_locality_count"] in [e[1] for e in bq.log if e[0] == "run"]


def test_jm11_generated_from_cites_the_commit_the_bindings_bind(tmp_path):
    fix = fixture(tmp_path)
    other = "c" * 40
    manifest = build(fix, bound_over={"target": other, "release_id": "rel-ccccccc-01"})
    assert manifest["generated_from"] == {"head": other}


# RB-T8 (F9, F12, F17, C30)

# F9: a typed manual run is MANUAL (the accepted reading), and the skipped duplicate allowance covers only the brief's 06:15 start

def manual_names(manifest):
    return [e["name"] for e in manifest["side_executions"]]


def test_rb_t8_a_typed_manual_run_of_a_chain_job_disqualifies_and_is_listed_in_side_executions(tmp_path):
    fix = fixture(tmp_path)
    fix.side_execution("f42-detect", "f42-detect-typed", fix.midnight + dt.timedelta(minutes=600))
    manifest = build(fix)
    assert reasons(manifest) == ["MANUAL"] and "f42-detect-typed" in manual_names(manifest)


def test_rb_t8_an_extra_collect_start_at_0520_sast_is_manual_even_when_it_ended_as_a_skipped_duplicate(tmp_path):
    fix = fixture(tmp_path)
    fix.skipped("collect", "x", minutes=320)
    manifest = build(fix)
    assert reasons(manifest) == ["MANUAL"] and manifest["verdict"]["failed_stage"] == "collect"
    assert manifest["stages"]["collect"]["skipped_duplicates"]  # still recorded, never a reason on its own


def test_rb_t8_an_extra_brief_start_at_0520_sast_is_manual_although_a_skipped_row_and_the_0615_start_exist(tmp_path):
    fix = fixture(tmp_path)
    fix.skipped("brief", "early", minutes=320)
    assert reasons(build(fix)) == ["MANUAL"]


def test_rb_t8_a_second_start_near_0615_without_a_second_skipped_row_is_manual(tmp_path):
    fix = fixture(tmp_path)
    fix.side_execution("f42-brief", "f42-brief-again", fix.midnight + dt.timedelta(minutes=377))
    assert reasons(build(fix)) == ["MANUAL"]


@pytest.mark.parametrize("minutes,manual", [(385, False), (386, True), (365, False), (364, True)])
def test_rb_t8_the_skipped_duplicate_allowance_follows_the_bound_tolerance_around_0615(tmp_path, minutes, manual):
    fix = fixture(tmp_path)
    fix.rows = [r for r in fix.rows if r["status"] != "skipped_duplicate"]
    fix.world.executions = {k: v for k, v in fix.world.executions.items() if "-skip-" not in k}
    fix.skipped("brief", "edge", minutes=minutes)
    assert (reasons(build(fix)) == ["MANUAL"]) is manual


def test_rb_t8_the_0615_start_with_its_skipped_row_still_does_not_disqualify(tmp_path):
    fix = fixture(tmp_path)
    assert reasons(build(fix)) == []


# F12: an execution name a runs row declares about itself is checked for shape before it reaches gcloud

@pytest.mark.parametrize("name", ["--project=evil", "-x", "F42-COLLECT-AAAAA", "f42-collect aaaaa", "../f42-collect-aaaaa", "f42-", "f42-collect-aaaaa;x", ""])
def test_rb_t8_a_declared_execution_name_with_the_wrong_shape_is_a_bound_failure_and_is_never_described(tmp_path, name):
    fix = fixture(tmp_path)
    running = next(r for r in fix.rows if r["stage"] == "collect" and r["status"] == "running")
    running["counts"] = json.dumps({"execution": name})
    reader = fix.reader()
    manifest = build(fix, reader=reader)
    assert "BOUND" in reasons(manifest)
    assert all(call[:5] != ["run", "jobs", "executions", "describe", name] for call in reader.argv_log)
    assert not [call for call in reader.argv_log if name and name in call]


def test_rb_t8_describe_execution_refuses_a_malformed_name_before_asking_the_reader():
    class Reader:
        def execution(self, name):
            raise AssertionError("the reader must not be asked")

    for name in ("--flag", "f42-UPPER", "other-collect-1", None, 5):
        with pytest.raises(so.NotFound):
            jo.describe_execution(Reader(), name)


# C30: the producer checks who is calling

@pytest.mark.parametrize("config", [{"core": {"account": "someone.else@example.com", "project": "ogilvy-trends-v2"}},
                                    {"core": {"account": jw.CALLER, "project": "another-project"}},
                                    {"core": {"account": jw.CALLER, "project": "ogilvy-trends-v2"}, "auth": {"impersonate_service_account": "x@y.iam"}},
                                    {}], ids=["other_account", "other_project", "impersonation", "empty"])
def test_rb_t8_the_producer_stops_when_the_cli_caller_project_or_impersonation_differs_from_the_bound_one(tmp_path, config):
    fix = fixture(tmp_path)
    fix.world.config = config
    with pytest.raises(so.Stop) as stop:
        build(fix)
    assert stop.value.code == "IDENTITY"


# F17: the thirteen codes of JM-02 are each asserted by a node of this file, so a deleted fixture cannot leave JM-02 green

def test_rb_t8_each_of_the_thirteen_codes_is_asserted_by_a_jm02_node_of_this_file():
    import ast

    assert len(ce.CODES) == 13 and set(ce.CODES) == set(jo.CHAIN_CODES)
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    asserted = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_jm02_"):
            for statement in ast.walk(node):
                if isinstance(statement, ast.Assert):
                    for const in ast.walk(statement.test):
                        if isinstance(const, ast.Constant) and const.value in ce.CODES:
                            asserted.setdefault(const.value, set()).add(node.name)
    assert sorted(set(ce.CODES) - set(asserted)) == []


@pytest.mark.parametrize("stage", ["collect", "understand", "detect"])
def test_rb_t8_only_the_brief_has_a_skipped_duplicate_allowance_even_at_0615(tmp_path, stage):
    fix = fixture(tmp_path)
    fix.skipped(stage, "x", minutes=375)
    assert reasons(build(fix)) == ["MANUAL"]


# F20 (final assembly): the producer writes a date's manifest once and never overwrites it, so a read before the brief's 06:15 SAST
# deadline of that date would leave a half chain on record for good. It refuses to read before that moment, and writes nothing.

def sast(day, hour, minute, second=0):
    return dt.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=jo.SAST)


def run_cli_at(fix, moment, **kw):
    fix.write_bindings() if fix.bound is None else None
    bindings = fix.tmp / "bindings.json"
    bindings.write_text(json.dumps(fix.bound), encoding="utf-8")
    argv = ["--date", fix.day.isoformat(), "--bindings", str(bindings), "--evidence", str(fix.evidence), "--expect", fix.role]
    return ce.main(argv, reader_factory=lambda timeouts: fix.reader(), bq_factory=lambda bound: fix.bq(), now=lambda: moment, **kw)


def test_f20_the_producer_stops_too_early_one_second_before_0615_sast_of_the_run_date_and_writes_nothing(tmp_path, capsys):
    fix = fixture(tmp_path)
    fix.write_bindings()
    assert run_cli_at(fix, sast(fix.day, 6, 14, 59)) == 1
    assert "TOO_EARLY" in capsys.readouterr().err
    assert not fix.manifest_path().exists() and not list(fix.evidence.glob("chain-evidence-*"))


def test_f20_a_refused_early_read_does_not_use_up_the_date_so_the_read_at_0615_sast_writes_the_manifest(tmp_path, capsys):
    fix = fixture(tmp_path)
    fix.write_bindings()
    assert run_cli_at(fix, sast(fix.day, 5, 0)) == 1
    assert run_cli_at(fix, sast(fix.day, 6, 15)) == 0, capsys.readouterr().err
    assert fix.manifest_path().is_file()


def test_f20_build_manifest_stops_too_early_on_the_same_rule_for_the_day_before_it_ran(tmp_path):
    fix = fixture(tmp_path)
    bound = fix.write_bindings()
    with pytest.raises(so.Stop) as stop:
        ce.build_manifest(bound, fix.reader(), fix.bq(), fix.day, fix.role, now=lambda: sast(fix.day - dt.timedelta(days=1), 23, 59))
    assert stop.value.code == "TOO_EARLY"


# F8 (final assembly), RB-C2 as ruled by RJ-6: a chain whose detect stops before its views step leaves the brief failing on the missing view
# column. That chain fails TERMINAL (detect's own terminal row is not ok), never DEGRADED, which is understand's rule alone.

def test_f8_a_chain_whose_detect_stops_before_its_views_step_and_whose_brief_then_fails_is_terminal_never_degraded(tmp_path):
    fix = fixture(tmp_path)
    fix.set_terminal("detect", status="failed")
    fix.set_terminal("brief", status="failed")
    manifest = build(fix)
    assert "TERMINAL" in reasons(manifest) and "DEGRADED" not in reasons(manifest)
    assert manifest["verdict"]["failed_stage"] == "detect" and manifest["verdict"]["qualifies"] is False
    assert manifest["verdict"]["baseline_by_line"] is False


def test_f8_the_same_chain_is_never_a_baseline_by_line_even_with_the_parse_json_degradation_in_understand(tmp_path):
    fix = fixture(tmp_path)
    fix.set_cluster_error("ZA")
    fix.set_terminal("detect", status="failed")
    fix.set_terminal("brief", status="failed")
    manifest = build(fix)
    assert "TERMINAL" in reasons(manifest) and manifest["verdict"]["baseline_by_line"] is False
