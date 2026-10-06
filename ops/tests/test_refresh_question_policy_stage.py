"""stage-unattended, and the managed job reading what it staged.

The index is staged first, then the grant, one document per run. The job reads
both at the generation current when it runs, and a swapped or stale document at
either name can only make it refuse. Every test runs against the local double
in fixtures/release_fake_adapter.py through the unattended scenario. Nothing
touches a network.
"""

import json
from datetime import timedelta

import pytest

from ops.deploy import refresh_question_policy, release
from ops.tests.test_foundation_release import NOW, fake, write_json
from ops.tests.test_refresh_question_policy_cli import recording_clients, run_main
from ops.tests.test_refresh_question_policy_unattended import (
    DUE,
    SHIPPED_ARGS,
    Unattended,
    unattended_grant,
)

INDEX_NAME = release.OBJECT_PREFIX + "renewal/release-index.json"
GRANT_NAME = release.OBJECT_PREFIX + "renewal/unattended-grant.json"
LOCATOR = "object:gs://" + release.BUCKET + "/"
# The default grant is granted a day before NOW: the index goes up before
# that, the grant after it, and the job runs at DUE.
INDEX_STAGED_AT = NOW - timedelta(days=2)
GRANT_STAGED_AT = NOW - timedelta(hours=20)


def set_now(job, when):
    state = job.lane.state()
    state["clock"]["now"] = when.isoformat()
    write_json(job.lane.state_path, state)


def stage(job, capsys, *extra, index=None, grant=None, output=None):
    which = ["--release-index", index] if index is not None else ["--grant", grant]
    argv = [
        "stage-unattended",
        *which,
        "--output",
        output or (job.lane.dir / f"stage-{len(job.lane.state()['journal'])}.json"),
        *job.lane.adapter(),
        *extra,
    ]
    clients = recording_clients(job.lane.state_path)
    return run_main(argv, capsys, clients=clients)


def stage_index(job, capsys, *extra, when=INDEX_STAGED_AT, **kwargs):
    set_now(job, when)
    return stage(job, capsys, *extra, index=job.lane.index_path, **kwargs)


def stage_grant(job, capsys, *extra, when=GRANT_STAGED_AT, **kwargs):
    set_now(job, when)
    return stage(job, capsys, *extra, grant=job.grant_path, **kwargs)


def stage_both(job, capsys):
    code, summary = stage_index(job, capsys)
    assert (code, summary["state"]) == (0, "staged"), summary
    code, summary = stage_grant(job, capsys)
    assert (code, summary["state"]) == (0, "staged"), summary


def shipped(job, capsys, when=DUE):
    """Run the vector the job ships, with its paths swapped for the lane's."""
    set_now(job, when)
    argv = list(SHIPPED_ARGS[2:])
    argv[argv.index("--adapter") : argv.index("--adapter") + 4] = job.lane.adapter()
    argv[argv.index("--output") + 1] = str(job.output)
    clients = recording_clients(job.lane.state_path)
    return run_main(argv, capsys, clients=clients)


def stored(job, name):
    return job.lane.state()["objects"].get(name)


def overwrite(job, name, raw, when):
    """Replace the object at a fixed name as a later writer would."""
    state = job.lane.state()
    generation = str(state["next_generation"])
    state["next_generation"] += 1
    state["objects"][name] = {
        "generation": generation,
        "raw_b64": fake.encode_raw(raw),
        "time_created": when.isoformat(),
    }
    write_json(job.lane.state_path, state)


def test_each_document_is_staged_byte_for_byte_and_the_shipped_vector_renews_on_them(
    tmp_path, capsys
):
    job = Unattended(tmp_path)
    code, index = stage_index(job, capsys)
    assert (code, index["state"]) == (0, "staged"), index
    code, grant = stage_grant(job, capsys)
    assert (code, grant["state"]) == (0, "staged"), grant
    for name, path, summary, key in (
        (INDEX_NAME, job.lane.index_path, index, "release_index"),
        (GRANT_NAME, job.grant_path, grant, "grant"),
    ):
        entry = stored(job, name)
        raw = path.read_bytes()
        assert fake.decode_raw(entry["raw_b64"]) == raw
        assert summary[key]["sha256"] == release._sha256(raw)
        assert summary[key]["locator"] == LOCATOR + name + "#" + entry["generation"]
    assert (
        grant["staged_release_index"]["generation"]
        == stored(job, INDEX_NAME)["generation"]
    )
    code, renewed = shipped(job, capsys)
    assert (code, renewed["state"]) == (0, "activated"), renewed
    # The receipt names what was read at each name: generation, digest, time.
    for key, name, path, when in (
        ("release_index", INDEX_NAME, job.lane.index_path, INDEX_STAGED_AT),
        ("grant", GRANT_NAME, job.grant_path, GRANT_STAGED_AT),
    ):
        recorded = renewed["receipt"]["inputs"][key]
        generation = stored(job, name)["generation"]
        assert recorded == {
            "locator": LOCATOR + name + "#" + generation,
            "generation": generation,
            "sha256": release._sha256(path.read_bytes()),
            "time_created": when.isoformat(),
        }
        assert renewed["inputs"][key] == recorded


def test_staging_is_a_compare_and_swap_on_what_it_read(tmp_path, capsys):
    job = Unattended(tmp_path)
    stage_both(job, capsys)
    first = stored(job, GRANT_NAME)["generation"]
    job.write_grant(unattended_grant(expires_at=(DUE + timedelta(days=30)).isoformat()))
    code, summary = stage_grant(job, capsys)
    assert (code, summary["state"]) == (0, "staged"), summary
    assert stored(job, GRANT_NAME)["generation"] != first
    writes = [
        entry
        for entry in job.lane.state()["journal"]
        if entry["call"] == "create_object" and entry["name"] == GRANT_NAME
    ]
    assert [entry["if_generation_match"] for entry in writes] == [0, int(first)]


def test_an_identical_document_is_not_written_again(tmp_path, capsys):
    job = Unattended(tmp_path)
    stage_both(job, capsys)
    before = len(job.writes())
    code, summary = stage_grant(job, capsys)
    assert (code, summary["state"]) == (0, "staged"), summary
    assert len(job.writes()) == before
    assert summary["grant"]["action"] == "unchanged"


def test_exactly_one_document_is_staged_per_run(tmp_path, capsys):
    job = Unattended(tmp_path)
    base = ["stage-unattended", "--output", str(tmp_path / "o.json")]
    for which in (
        [],
        ["--release-index", str(job.lane.index_path), "--grant", str(job.grant_path)],
    ):
        with pytest.raises(SystemExit):
            refresh_question_policy.build_parser().parse_args(
                base + which + job.lane.adapter()
            )


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"release_commit": "f" * 40}, "unattended_release_commit_mismatch"),
        ({"input_usd_per_million": "1.500000"}, "unattended_rates_not_proven"),
        (
            {"expires_at": (GRANT_STAGED_AT - timedelta(seconds=1)).isoformat()},
            "grant_expired",
        ),
        ({"contract_version": "42_renewal_grant_v1"}, "grant_invalid"),
        ({"baseline_source_sha256": "0" * 64}, "unattended_baseline_mismatch"),
        (
            {"granted_at": (INDEX_STAGED_AT - timedelta(seconds=1)).isoformat()},
            "release_index_newer_than_grant",
        ),
    ],
)
def test_a_grant_the_job_would_refuse_is_refused_before_it_is_written(
    tmp_path, capsys, changes, error
):
    job = Unattended(tmp_path)
    assert stage_index(job, capsys)[0] == 0
    job.write_grant(unattended_grant(**changes))
    before = len(job.writes())
    code, summary = stage_grant(job, capsys)
    assert (code, summary["state"], summary["error"]) == (1, "refused", error), (
        changes,
        summary,
    )
    assert len(job.writes()) == before
    assert stored(job, GRANT_NAME) is None


def test_a_grant_is_refused_until_its_index_is_staged(tmp_path, capsys):
    job = Unattended(tmp_path)
    code, summary = stage_grant(job, capsys)
    assert (code, summary["error"]) == (1, "release_index_unreadable"), summary
    assert stored(job, GRANT_NAME) is None


def test_an_index_for_another_image_is_refused_before_it_is_written(tmp_path, capsys):
    job = Unattended(tmp_path)
    index = json.loads(job.lane.index_path.read_bytes())
    index["app_image"] = index["app_image"].rsplit("@", 1)[0] + "@sha256:" + "e" * 64
    write_json(job.lane.index_path, index)
    code, summary = stage_index(job, capsys)
    assert (code, summary["error"]) == (1, "release_index_image_mismatch"), summary
    assert stored(job, INDEX_NAME) is None


def test_a_missing_baseline_object_is_refused_before_the_grant_is_written(
    tmp_path, capsys
):
    job = Unattended(tmp_path)
    assert stage_index(job, capsys)[0] == 0
    state = job.lane.state()
    name = refresh_question_policy._evidence_object_name(
        unattended_grant()["baseline_source_sha256"]
    )
    del state["objects"][name]
    write_json(job.lane.state_path, state)
    code, summary = stage_grant(job, capsys)
    # The same reason the job itself would give for a missing baseline.
    assert (code, summary["error"]) == (1, "observation_object_unreadable"), summary
    assert stored(job, GRANT_NAME) is None


def test_dry_run_renders_the_one_write_and_sends_nothing(tmp_path, capsys):
    job = Unattended(tmp_path)
    before = len(job.writes())
    code, summary = stage_index(job, capsys, "--dry-run")
    assert (code, summary["state"]) == (0, "dry_run"), summary
    assert len(job.writes()) == before
    assert [
        (item["call"], item["object"], item["if_generation_match"])
        for item in summary["requests"]
    ] == [("create_object", INDEX_NAME, 0)]


def test_an_existing_output_is_refused_before_any_native_call(tmp_path, capsys):
    job = Unattended(tmp_path)
    output = tmp_path / "stage-receipt.json"
    output.write_bytes(b"existing")
    before = job.lane.state()["journal"][:]
    code, summary = stage_index(job, capsys, output=output)
    assert (code, summary["error"]) == (1, "output_exists"), summary
    assert job.lane.state()["journal"] == before


def test_an_index_swapped_in_after_the_grant_only_refuses(tmp_path, capsys):
    job = Unattended(tmp_path)
    stage_both(job, capsys)
    # The same bytes, even, put there after the grant was written.
    overwrite(
        job, INDEX_NAME, job.lane.index_path.read_bytes(), NOW - timedelta(hours=1)
    )
    before = len(job.writes())
    code, summary = shipped(job, capsys)
    assert (code, summary["error"]) == (1, "release_index_newer_than_grant"), summary
    assert len(job.writes()) == before


def test_a_grant_left_from_an_earlier_release_only_refuses(tmp_path, capsys):
    job = Unattended(tmp_path)
    stage_both(job, capsys)
    stale = unattended_grant(release_commit="f" * 40)
    overwrite(
        job,
        GRANT_NAME,
        json.dumps(stale).encode("utf-8"),
        GRANT_STAGED_AT,
    )
    before = len(job.writes())
    code, summary = shipped(job, capsys)
    assert (code, summary["error"]) == (
        1,
        "unattended_release_commit_mismatch",
    ), summary
    assert len(job.writes()) == before


def test_an_expired_staged_grant_only_refuses(tmp_path, capsys):
    job = Unattended(tmp_path)
    stage_both(job, capsys)
    expires = NOW + timedelta(days=60)
    code, summary = shipped(job, capsys, when=expires + timedelta(seconds=1))
    assert (code, summary["error"]) == (1, "grant_expired"), summary


def test_nothing_staged_refuses_with_nothing_written(tmp_path, capsys):
    job = Unattended(tmp_path)
    before = len(job.writes())
    code, summary = shipped(job, capsys)
    assert (code, summary["error"]) == (1, "release_index_unreadable"), summary
    assert len(job.writes()) == before


def test_an_index_without_a_creation_time_refuses(tmp_path, capsys):
    job = Unattended(tmp_path)
    stage_both(job, capsys)
    state = job.lane.state()
    del state["objects"][INDEX_NAME]["time_created"]
    write_json(job.lane.state_path, state)
    code, summary = shipped(job, capsys)
    assert (code, summary["error"]) == (1, "release_index_time_unavailable"), summary


@pytest.mark.parametrize(
    "command, index, grant, error",
    [
        # current names only the two fixed objects, each for its own document.
        ("renew-unattended", "unattended-grant.json", None, "release_index"),
        ("renew-unattended", None, "release-index.json", "grant"),
        ("renew-unattended", "other.json", None, "release_index"),
        # and only in the unattended run.
        ("extend-expiry", "release-index.json", None, "release_index"),
    ],
)
def test_current_is_accepted_only_for_its_own_fixed_name_in_the_unattended_run(
    tmp_path, capsys, command, index, grant, error
):
    job = Unattended(tmp_path)
    stage_both(job, capsys)
    renewal = LOCATOR + release.OBJECT_PREFIX + "renewal/"
    argv = [
        command,
        "--release-index",
        renewal + (index or "release-index.json") + "#current",
        "--grant",
        renewal + (grant or "unattended-grant.json") + "#current",
        "--output",
        str(job.output),
        *job.lane.adapter(),
    ]
    set_now(job, DUE)
    before = len(job.writes())
    code, summary = run_main(
        argv, capsys, clients=recording_clients(job.lane.state_path)
    )
    assert (code, summary["error"]) == (1, error + "_locator_invalid"), summary
    assert len(job.writes()) == before


def test_a_current_grant_beside_a_pinned_index_still_needs_the_index_time(
    tmp_path, capsys
):
    job = Unattended(tmp_path)
    stage_both(job, capsys)
    renewal = LOCATOR + release.OBJECT_PREFIX + "renewal/"
    pinned = renewal + "release-index.json#" + stored(job, INDEX_NAME)["generation"]
    set_now(job, DUE)
    before = len(job.writes())
    code, summary = job.run(
        capsys, index=pinned, grant=renewal + "unattended-grant.json#current"
    )
    assert (code, summary["error"]) == (1, "release_index_time_unavailable"), summary
    assert len(job.writes()) == before


def test_the_creation_time_is_read_in_the_form_storage_writes_it(tmp_path, capsys):
    job = Unattended(tmp_path)
    stage_both(job, capsys)
    state = job.lane.state()
    state["objects"][INDEX_NAME]["time_created"] = (
        INDEX_STAGED_AT.strftime("%Y-%m-%dT%H:%M:%S") + ".123Z"
    )
    write_json(job.lane.state_path, state)
    code, renewed = shipped(job, capsys)
    assert (code, renewed["state"]) == (0, "activated"), renewed
    state = job.lane.state()
    state["objects"][INDEX_NAME]["time_created"] = "2026-09-24T07:00:00.123Z"
    write_json(job.lane.state_path, state)
    job.output = tmp_path / "second.json"
    code, summary = shipped(job, capsys)
    assert (code, summary["error"]) == (1, "release_index_newer_than_grant"), summary


# A grant whose text is fixed before the go names a granted_at in the near
# future, after every write of the brief; it is staged ahead of that time.
FUTURE_GRANTED_AT = NOW + timedelta(hours=4)


def future_grant(**changes):
    return unattended_grant(
        granted_at=FUTURE_GRANTED_AT.isoformat(),
        expires_at=(FUTURE_GRANTED_AT + timedelta(days=60)).isoformat(),
        **changes,
    )


def test_a_future_grant_is_staged_ahead_and_the_job_waits_for_it(tmp_path, capsys):
    job = Unattended(tmp_path)
    assert stage_index(job, capsys, when=NOW - timedelta(hours=3))[0] == 0
    job.write_grant(future_grant())
    code, summary = stage_grant(job, capsys, when=NOW - timedelta(hours=2))
    assert (code, summary["state"]) == (0, "staged"), summary
    before = len(job.writes())
    code, early = shipped(job, capsys, when=FUTURE_GRANTED_AT - timedelta(seconds=1))
    assert (code, early["error"]) == (1, "grant_not_yet_valid"), early
    assert len(job.writes()) == before
    job.output = tmp_path / "after.json"
    code, renewed = shipped(job, capsys, when=DUE)
    assert DUE >= FUTURE_GRANTED_AT
    assert (code, renewed["state"]) == (0, "activated"), renewed


def test_a_future_grant_is_refused_when_the_index_came_after_it(tmp_path, capsys):
    job = Unattended(tmp_path)
    assert (
        stage_index(job, capsys, when=FUTURE_GRANTED_AT + timedelta(minutes=1))[0] == 0
    )
    job.write_grant(future_grant())
    code, summary = stage_grant(
        job, capsys, when=FUTURE_GRANTED_AT + timedelta(minutes=2)
    )
    assert (code, summary["error"]) == (1, "release_index_newer_than_grant"), summary
    assert stored(job, GRANT_NAME) is None


def test_a_grant_starting_more_than_a_day_ahead_is_refused(tmp_path, capsys):
    job = Unattended(tmp_path)
    assert stage_index(job, capsys, when=NOW - timedelta(hours=3))[0] == 0
    job.write_grant(future_grant())
    code, summary = stage_grant(
        job, capsys, when=FUTURE_GRANTED_AT - timedelta(hours=24, seconds=1)
    )
    assert (code, summary["error"]) == (1, "grant_start_too_far"), summary
    assert stored(job, GRANT_NAME) is None


def test_an_index_restaged_after_a_future_grant_was_staged_only_refuses(
    tmp_path, capsys
):
    job = Unattended(tmp_path)
    assert stage_index(job, capsys, when=NOW - timedelta(hours=3))[0] == 0
    job.write_grant(future_grant())
    assert stage_grant(job, capsys, when=NOW - timedelta(hours=2))[0] == 0
    # Before granted_at, so the granted_at rule alone would let it through.
    overwrite(
        job, INDEX_NAME, job.lane.index_path.read_bytes(), NOW - timedelta(hours=1)
    )
    before = len(job.writes())
    code, summary = shipped(job, capsys, when=DUE)
    assert (code, summary["error"]) == (1, "release_index_newer_than_grant"), summary
    assert len(job.writes()) == before


def test_a_current_grant_without_a_creation_time_refuses(tmp_path, capsys):
    job = Unattended(tmp_path)
    stage_both(job, capsys)
    state = job.lane.state()
    del state["objects"][GRANT_NAME]["time_created"]
    write_json(job.lane.state_path, state)
    code, summary = shipped(job, capsys)
    assert (code, summary["error"]) == (1, "grant_time_unavailable"), summary


def test_a_grant_exactly_a_day_ahead_is_staged(tmp_path, capsys):
    job = Unattended(tmp_path)
    assert stage_index(job, capsys, when=NOW - timedelta(days=2))[0] == 0
    job.write_grant(future_grant())
    code, summary = stage_grant(
        job, capsys, when=FUTURE_GRANTED_AT - timedelta(hours=24)
    )
    assert (code, summary["state"]) == (0, "staged"), summary
    assert summary["grant_checked_as_of"] == FUTURE_GRANTED_AT.isoformat()
