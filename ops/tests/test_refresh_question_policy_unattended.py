"""renew-unattended: the managed job's renewal, run with nobody watching.

It consumes a long window grant Albert writes by hand once, naming the rates he
last proved and the page bytes he proved them against, plus the release index.
It makes its own observation from one live fetch, reuses only those rates, and
refuses on any change to the page's prices before anything is written.
"""

import hashlib
import json
from datetime import timedelta

import pytest

from ops.deploy import refresh_question_policy, release, runtime_jobs
from ops.runners.managed_runtime import PRICE_POLICY_JOB_ID
from ops.tests.test_foundation_release import (
    LP_COMMIT,
    MANIFEST_PATH,
    MANIFEST_SHA256,
    NOW,
    PRICING_SOURCE,
    ROOT,
    fake,
    make_policy,
    object_entry,
    observation,
    renewal_grant,
    write_json,
)
from ops.tests.test_refresh_question_policy_cli import (
    Lane,
    recording_clients,
    run_main,
)

RUNTIME_FILE = ROOT / "infra" / "runtime" / "daily-staging.json"
MODEL = make_policy()["model"]
# Fourteen hours after the active review at 00:00, so the renewal is due.
DUE = NOW + timedelta(hours=6)
BASELINE_PAGE = (
    b"<!doctype html><html><head><script nonce='a1b2c3'>window.p=1</script>"
    b"</head><body><table><tr><td>flash</td><td>$0.75</td><td>$3.75</td></tr>"
    b"<tr><td>pro</td><td>$1.50</td><td>$9.00</td></tr></table></body></html>"
)
# The vendor serves a different body every time; the prices are what matter.
LIVE_PAGE = BASELINE_PAGE.replace(b"a1b2c3", b"f9e8d7c6").replace(
    b"window.p=1", b"window.p=2;window.q=3"
)
RENEWAL = (
    "object:gs://listening-post-staging-cache/open-intelligence/v2/staging/"
    "general-questions/renewal/"
)
SEALED_ARGS = [
    "-m",
    "ops.deploy.refresh_question_policy",
    "renew-unattended",
    "--release-index",
    RENEWAL + "release-index.json#pending",
    "--grant",
    RENEWAL + "unattended-grant.json#pending",
    "--adapter",
    "/app/ops/deploy/release_native_adapter.py:factory",
    "--adapter-state",
    "adc",
    "--output",
    "/tmp/renewal-receipt.json",
]


# The vector the job ships: both documents at the generation current when it
# runs, recorded in its receipt (the ruling of 25 September; see the release
# lifecycle document). A vector still pinned to pending refuses as before.
SHIPPED_ARGS = [item.replace("#pending", "#current") for item in SEALED_ARGS]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def baseline_name(raw=BASELINE_PAGE):
    return refresh_question_policy._evidence_object_name(sha(raw))


def baseline_locator(raw=BASELINE_PAGE, generation=7):
    return f"object:gs://{release.BUCKET}/{baseline_name(raw)}#{generation}"


def unattended_grant(**changes):
    grant = {
        "contract_version": "42_unattended_renewal_grant_v1",
        "purpose": "unattended_pricing_renewal",
        "resource_manifest_sha256": MANIFEST_SHA256,
        "granted_at": (NOW - timedelta(days=1)).isoformat(),
        "expires_at": (NOW + timedelta(days=60)).isoformat(),
        "grantor": "Albert Meintjes",
        "release_commit": LP_COMMIT,
        "model": MODEL,
        "input_usd_per_million": "0.750000",
        "output_usd_per_million": "3.750000",
        "baseline_source_object": baseline_locator(),
        "baseline_source_sha256": sha(BASELINE_PAGE),
    }
    grant.update(changes)
    return grant


class Unattended:
    def __init__(self, tmp_path, *, live=LIVE_PAGE, baseline=BASELINE_PAGE, now=DUE):
        self.lane = Lane(tmp_path)
        state = self.lane.state()
        state["clock"]["now"] = now.isoformat()
        state["bytes"]["source:" + PRICING_SOURCE] = fake.encode_raw(live)
        state["objects"][baseline_name(baseline)] = object_entry(baseline, 7)
        write_json(self.lane.state_path, state)
        # The release that set the scenario up wrote its own objects and
        # revisions; only what this run adds counts.
        self.journal_start = len(state["journal"])
        self.grant_path = write_json(
            tmp_path / "unattended-grant.json", unattended_grant()
        )
        self.output = tmp_path / "unattended-receipt.json"

    def write_grant(self, grant):
        write_json(self.grant_path, grant)

    def argv(self, *, grant=None, index=None, output=None):
        return [
            "renew-unattended",
            "--release-index",
            index or self.lane.index_path,
            "--grant",
            grant or self.grant_path,
            "--output",
            output or self.output,
            *self.lane.adapter(),
        ]

    def run(self, capsys, **kwargs):
        clients = recording_clients(self.lane.state_path)
        code, summary = run_main(self.argv(**kwargs), capsys, clients=clients)
        return code, summary

    def writes(self):
        return [
            entry
            for entry in self.lane.state()["journal"][self.journal_start :]
            if entry["call"] in ("create_object", "deploy_revision", "update_traffic")
        ]

    def source_reads(self):
        return [
            reference
            for reference in self.lane.state().get("byte_reads", [])
            if reference.startswith("source:")
        ]


# The sealed argument vector


def test_the_shipped_managed_job_vector_is_the_sealed_unattended_form():
    config = json.loads(RUNTIME_FILE.read_bytes())
    shipped = config["jobs"][PRICE_POLICY_JOB_ID]["args"]
    assert shipped == SHIPPED_ARGS
    assert refresh_question_policy.MANAGED_JOB_ARGS == SHIPPED_ARGS
    parsed = refresh_question_policy.build_parser().parse_args(shipped[2:])
    assert parsed.command == "renew-unattended"
    assert parsed.adapter_state == "adc"
    # The bare module the file used to carry cannot renew anything.
    with pytest.raises(SystemExit) as raised:
        refresh_question_policy.build_parser().parse_args([])
    assert raised.value.code == 2


def test_the_sealed_vector_refuses_before_any_native_call_while_pending(
    tmp_path, capsys
):
    job = Unattended(tmp_path)
    before = job.lane.state()["journal"]
    argv = [*SEALED_ARGS[2:]]
    argv[argv.index("--output") + 1] = str(tmp_path / "sealed-receipt.json")
    argv[argv.index("--adapter") + 1] = job.lane.adapter()[1]
    argv[argv.index("--adapter-state") + 1] = str(job.lane.state_path)
    argv += ["--resources", str(MANIFEST_PATH)]
    code, summary = run_main(
        argv, capsys, clients=recording_clients(job.lane.state_path)
    )
    assert (code, summary["state"]) == (1, "refused")
    assert summary["error"] == "release_index_generation_pending"
    assert job.lane.state()["journal"] == before
    assert job.lane.state().get("byte_reads", []) == []


def test_the_jobs_plan_rendered_by_the_repo_tool_carries_the_sealed_vector(tmp_path):
    digest = "sha256:" + "4" * 64
    build = {
        "id": "b0a1",
        "status": "SUCCESS",
        "results": {
            "images": [
                {
                    "name": "us-central1-docker.pkg.dev/ogilvy-trends-v2/"
                    "intelligence-42/engine:" + "a" * 40,
                    "digest": digest,
                }
            ]
        },
    }
    (tmp_path / "build.json").write_text(json.dumps(build), encoding="utf-8")
    plan = runtime_jobs.render_plan(
        runtime_path=RUNTIME_FILE,
        image_digest=digest,
        build_record_path=tmp_path / "build.json",
        output_path=tmp_path / "plan.json",
    )
    job = next(item for item in plan["jobs"] if item["job_id"] == PRICE_POLICY_JOB_ID)
    assert job["expected"]["args"] == SHIPPED_ARGS
    assert runtime_jobs.verify_plan(plan) == plan["plan_sha256"]


# The run


def test_unattended_renewal_reuses_the_proven_rates_and_activates(tmp_path, capsys):
    job = Unattended(tmp_path)
    code, summary = job.run(capsys)
    assert (code, summary["state"]) == (0, "activated"), summary
    assert job.source_reads() == ["source:" + PRICING_SOURCE]
    receipt = json.loads(job.output.read_text(encoding="utf-8"))
    assert receipt["command"] == "renew-unattended"
    observed = receipt["observation"]
    assert observed["contract_version"] == "42_pricing_observation_v2"
    assert observed["observed_at"] == release._stamp(DUE)
    assert observed["source_sha256"] == sha(LIVE_PAGE)
    assert (observed["input_usd_per_million"], observed["output_usd_per_million"]) == (
        "0.750000",
        "3.750000",
    )
    assert receipt["policy"]["pricing"]["verified_at"] == observed["observed_at"]
    assert receipt["rate_changed"] is False
    assert receipt["baseline_source_sha256"] == sha(BASELINE_PAGE)
    stored = job.lane.state()["objects"][
        refresh_question_policy._evidence_object_name(sha(LIVE_PAGE))
    ]
    assert fake.decode_raw(stored["raw_b64"]) == LIVE_PAGE


def test_a_price_change_anywhere_on_the_page_refuses_with_nothing_written(
    tmp_path, capsys
):
    job = Unattended(tmp_path, live=LIVE_PAGE.replace(b"$9.00", b"$9.50"))
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "unattended_source_changed")
    assert job.writes() == []


def test_a_page_with_no_prices_is_not_proof_that_nothing_changed(tmp_path, capsys):
    bare = b"<html>flash input 0.75 output 3.75</html>"
    job = Unattended(tmp_path, live=bare, baseline=bare)
    job.write_grant(
        unattended_grant(
            baseline_source_object=baseline_locator(bare),
            baseline_source_sha256=sha(bare),
        )
    )
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "unattended_source_changed")
    assert job.writes() == []


def test_a_rate_the_live_page_does_not_evidence_refuses(tmp_path, capsys):
    # The baseline has to price both rates as dollar amounts before a live page
    # is fetched at all, so the rates carry their dollar sign here.
    baseline = b"<html>$1.00 flash input $0.75 output $3.75</html>"
    live = b"<html>$1.00 flash input output</html>"
    job = Unattended(tmp_path, live=live, baseline=baseline)
    job.write_grant(
        unattended_grant(
            baseline_source_object=baseline_locator(baseline),
            baseline_source_sha256=sha(baseline),
        )
    )
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "observation_rate_not_evidenced")
    assert job.writes() == []


def test_rates_other_than_the_active_proven_ones_refuse(tmp_path, capsys):
    job = Unattended(tmp_path)
    job.write_grant(unattended_grant(input_usd_per_million="1.500000"))
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "unattended_rates_not_proven")
    assert job.writes() == []
    assert job.source_reads() == []


def test_a_renewal_that_is_not_due_does_nothing(tmp_path, capsys):
    job = Unattended(tmp_path, now=NOW)
    code, summary = job.run(capsys)
    assert (code, summary["state"]) == (0, "not_due"), summary
    assert job.writes() == []
    assert job.source_reads() == []


def test_a_baseline_whose_bytes_do_not_match_the_grant_refuses(tmp_path, capsys):
    job = Unattended(tmp_path)
    job.write_grant(unattended_grant(baseline_source_sha256="0" * 64))
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "unattended_baseline_mismatch")
    assert job.writes() == []


def test_the_release_commit_must_be_the_one_the_grant_names(tmp_path, capsys):
    job = Unattended(tmp_path)
    job.write_grant(unattended_grant(release_commit="2" * 40))
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "unattended_release_commit_mismatch")
    assert job.writes() == []


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        (
            {"expires_at": (NOW + timedelta(days=100)).isoformat()},
            "grant_window_too_long",
        ),
        ({"expires_at": (NOW + timedelta(hours=1)).isoformat()}, "grant_expired"),
        ({"resource_manifest_sha256": "1" * 64}, "grant_manifest_mismatch"),
        ({"purpose": "pricing_renewal"}, "grant_invalid"),
        ({"model": "unknown-model"}, "observation_model_unknown"),
    ],
)
def test_the_unattended_grant_is_checked_before_anything_is_read(
    tmp_path, capsys, changes, error
):
    job = Unattended(tmp_path)
    job.write_grant(unattended_grant(**changes))
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, error)
    assert job.writes() == []
    assert job.source_reads() == []


def test_neither_grant_stands_in_for_the_other(tmp_path, capsys):
    job = Unattended(tmp_path)
    job.write_grant(renewal_grant())
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "grant_invalid")
    lane = job.lane
    write_json(lane.grant_path, unattended_grant())
    code, summary = run_main(
        lane.renew_argv(output=tmp_path / "attended.json"),
        capsys,
        clients=recording_clients(lane.state_path),
    )
    assert (code, summary["error"]) == (1, "grant_invalid")
    assert job.writes() == []


def test_the_controller_rechecks_the_rates_at_the_acting_point(tmp_path):
    """A ledger that moves between the preflight and the write cannot change price."""
    job = Unattended(tmp_path)
    lane = job.lane
    clients = recording_clients(lane.state_path)
    clients["manifest"] = {"value": lane.renewal.manifest, "sha256": MANIFEST_SHA256}
    document = observation(
        observed_at=DUE - timedelta(minutes=1),
        input_rate="1.500000",
        output_rate="9.000000",
        page=LIVE_PAGE,
    )
    result = refresh_question_policy.refresh_observed_policy(
        release_index=lane.renewal.index,
        observation=document,
        grant=unattended_grant(
            input_usd_per_million="1.500000", output_usd_per_million="9.000000"
        ),
        clients=clients,
        unattended=True,
    )
    assert (result["state"], result["error"]) == ("refused", "unattended_rate_changed")
    assert job.writes() == []


def _with_baseline(tmp_path, baseline, live):
    job = Unattended(tmp_path, live=live, baseline=baseline)
    job.write_grant(
        unattended_grant(
            baseline_source_object=baseline_locator(baseline),
            baseline_source_sha256=sha(baseline),
        )
    )
    return job


def test_rates_printed_without_a_dollar_sign_are_not_proof_of_a_price(tmp_path, capsys):
    """The review probe: a bare 0.75 is a substring of 10.75 on the live page."""
    baseline = b"<html>$300 credit flash input 0.75 output 3.75</html>"
    live = b"<html>$300 credit flash input 10.75 output 13.75</html>"
    job = _with_baseline(tmp_path, baseline, live)
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "unattended_baseline_rate_not_priced")
    assert job.writes() == []
    assert job.source_reads() == []


def test_a_dollar_amount_that_merely_ends_in_the_rate_is_not_the_rate(tmp_path, capsys):
    baseline = b"<html>flash input $10.75 output $13.75</html>"
    job = _with_baseline(tmp_path, baseline, baseline)
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "unattended_baseline_rate_not_priced")
    assert job.writes() == []
    assert job.source_reads() == []


def test_a_live_body_cut_off_after_its_last_dollar_amount_refuses(tmp_path, capsys):
    cut = LIVE_PAGE[: LIVE_PAGE.rindex(b"$9.00") + len(b"$9.00")]
    job = Unattended(tmp_path, live=cut)
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "unattended_source_truncated")
    assert job.writes() == []


def test_a_baseline_that_is_not_a_whole_page_is_not_a_baseline(tmp_path, capsys):
    baseline = BASELINE_PAGE[: BASELINE_PAGE.rindex(b"</table>")]
    job = _with_baseline(tmp_path, baseline, LIVE_PAGE)
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "unattended_source_truncated")
    assert job.writes() == []
    assert job.source_reads() == []


def test_the_job_output_carries_the_full_receipt_and_what_it_wrote(tmp_path, capsys):
    """The container is thrown away, so its stdout is where the receipt lives."""
    job = Unattended(tmp_path)
    code, summary = job.run(capsys)
    assert (code, summary["state"]) == (0, "activated"), summary
    written = json.loads(job.output.read_text(encoding="utf-8"))
    assert summary["receipt"] == written
    assert (
        summary["receipt_sha256"]
        == hashlib.sha256(refresh_question_policy.canonical_bytes(written)).hexdigest()
    )
    assert summary["mutations"] == written["mutations"]
    assert [item["action"] for item in summary["mutations"]] == [
        "create_immutable_artifact",
        "create_immutable_artifact",
        "create_immutable_artifact",
    ]
    assert summary["activation"] == written["activation"]
    assert summary["ledger_moved"] is True
    assert set(summary["operations"]) == {"deploy_revision", "route_traffic"}


def test_a_lost_ledger_write_is_printed_with_its_detail(tmp_path, capsys):
    job = Unattended(tmp_path)
    clients = recording_clients(job.lane.state_path)
    original = clients["objects"].create

    def failing(name, raw, *, if_generation_match):
        if name == release.LEDGER_OBJECT:
            raise ConnectionError("response lost")
        return original(name, raw, if_generation_match=if_generation_match)

    clients["objects"].create = failing
    code, summary = run_main(job.argv(), capsys, clients=clients)
    assert code == 1
    assert summary["error"] == "ledger_write_failed", summary
    assert summary["detail"] == "ConnectionError: response lost"
    assert summary["receipt"]["detail"] == summary["detail"]
    assert "mutations" in summary


def test_an_unreadable_ledger_after_a_failed_write_is_printed(tmp_path, capsys):
    job = Unattended(tmp_path)
    clients = recording_clients(job.lane.state_path)
    original_create = clients["objects"].create
    original_read = clients["objects"].read
    broken = {"on": False}

    def failing(name, raw, *, if_generation_match):
        if name == release.LEDGER_OBJECT:
            broken["on"] = True
            raise ConnectionError("response lost")
        return original_create(name, raw, if_generation_match=if_generation_match)

    def reading(name, **kwargs):
        if broken["on"] and name == release.LEDGER_OBJECT:
            raise ConnectionError("read failed")
        return original_read(name, **kwargs)

    clients["objects"].create = failing
    clients["objects"].read = reading
    code, summary = run_main(job.argv(), capsys, clients=clients)
    assert (code, summary["error"]) == (1, "ledger_write_outcome_unknown"), summary
    assert summary["ledger_readback_error"] == "ConnectionError: read failed"


# The dollar list alone lets a changed row through wherever the old dollar
# amounts survive elsewhere on the page. These are the four review probes that
# activated at the old rate; each must now refuse with nothing written.
_STALE_RATE_PAGES = {
    "dollar_amounts_only_in_unrelated_text": (
        b"<html>Save $0.75 off, $3.75 fee. flash input 0.75 output 3.75</html>",
        b"<html>Save $0.75 off, $3.75 fee. flash input 10.75 output 13.75</html>",
    ),
    "row_written_as_an_entity_old_amounts_elsewhere": (
        b"<html>flash input $0.75 output $3.75</html>",
        b"<html>flash input &#36;10.75 output &#36;13.75 $0.75 $3.75</html>",
    ),
    "row_gone_bare_old_amounts_in_a_comment": (
        b"<html>flash $0.75 $3.75</html>",
        b"<html>flash 10.75 13.75 <!-- $0.75 $3.75 --></html>",
    ),
    "unit_changed_with_the_same_numbers": (
        b"<html>flash input $0.75 / 1M tokens output $3.75 / 1M tokens</html>",
        b"<html>flash input $0.75 / 1K tokens output $3.75 / 1K tokens</html>",
    ),
}


@pytest.mark.parametrize("case", sorted(_STALE_RATE_PAGES))
def test_a_changed_row_behind_surviving_dollar_amounts_refuses(tmp_path, capsys, case):
    baseline, live = _STALE_RATE_PAGES[case]
    job = _with_baseline(tmp_path, baseline, live)
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "unattended_source_changed"), summary
    assert job.writes() == []
    assert job.source_reads() == ["source:" + PRICING_SOURCE]


def test_the_same_numbers_and_units_on_a_rotated_page_still_activate(tmp_path, capsys):
    baseline = (
        b"<html><script nonce='a1'>v=1</script>flash input $0.75 / 1M tokens "
        b"output $3.75 / 1M tokens, 1.5 million free</html>"
    )
    live = baseline.replace(b"nonce='a1'>v=1", b"nonce='z9q8'>v=22")
    job = _with_baseline(tmp_path, baseline, live)
    code, summary = job.run(capsys)
    assert (code, summary["state"]) == (0, "activated"), summary


def test_swapped_rows_refuse_although_every_amount_is_still_on_the_page(
    tmp_path, capsys
):
    baseline = b"<html>flash input $0.75 output $3.75 pro $1.50 $9.00</html>"
    live = b"<html>flash input $1.50 output $9.00 pro $0.75 $3.75</html>"
    job = _with_baseline(tmp_path, baseline, live)
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "unattended_source_changed")
    assert job.writes() == []


def test_the_dollar_amounts_are_compared_in_order_not_as_a_set(tmp_path, capsys):
    # Whole dollar amounts carry no decimals, so only the order of the dollar
    # list itself can tell these two pages apart.
    baseline = b"<html>$300 credit, $50 fee. flash $0.75 $3.75</html>"
    live = b"<html>$50 credit, $300 fee. flash $0.75 $3.75</html>"
    job = _with_baseline(tmp_path, baseline, live)
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "unattended_source_changed")
    assert job.writes() == []


def test_a_longer_dollar_amount_in_the_baseline_is_not_the_rate(tmp_path, capsys):
    baseline = b"<html>flash input $0.755 output $3.755</html>"
    job = _with_baseline(tmp_path, baseline, baseline)
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "unattended_baseline_rate_not_priced")
    assert job.source_reads() == []
    assert job.writes() == []


def test_a_longer_dollar_amount_on_the_live_page_is_not_the_rate(tmp_path, capsys):
    baseline = b"<html>flash input $0.75 output $3.75</html>"
    live = b"<html>flash input $0.755 output $3.755</html>"
    job = _with_baseline(tmp_path, baseline, live)
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "observation_rate_not_evidenced")
    assert job.writes() == []


def test_an_active_binding_filed_under_another_key_refuses_before_any_fetch(
    tmp_path, capsys
):
    """The preflight checks the binding is the deployment the ledger names.

    The controller makes the same check later, but only after the live page
    was fetched and pinned; the preflight must refuse first, with nothing read
    from the vendor and nothing written.
    """
    job = Unattended(tmp_path)
    state = job.lane.state()
    ledger = json.loads(
        fake.decode_raw(state["objects"][release.LEDGER_OBJECT]["raw_b64"])
    )
    key = ledger["active_deployment_digest"]
    name = release.OBJECT_PREFIX + f"deployments/{key}/binding.json"
    stored = json.loads(fake.decode_raw(state["objects"][name]["raw_b64"]))
    moved = {**stored, "revision_name": stored["revision_name"] + "-x"}
    del moved["deployment_digest"]
    moved["deployment_digest"] = release.canonical_digest(moved)
    assert moved["deployment_digest"] != key
    state["objects"][name]["raw_b64"] = fake.encode_raw(release.canonical_bytes(moved))
    write_json(job.lane.state_path, state)
    code, summary = job.run(capsys)
    assert (code, summary["error"]) == (1, "active_binding_unavailable"), summary
    assert job.source_reads() == []
    assert job.writes() == []


# The job's identity holds read and replace on ledger.json only until amendment
# E grants the rest. A denial on any other object the run touches must stop it
# with the ledger, the service and the traffic untouched.
_PREFIX = release.OBJECT_PREFIX
_DENIALS = {
    "read_release_index": ("read", _PREFIX + "renewal/release-index.json"),
    "read_grant": ("read", _PREFIX + "renewal/unattended-grant.json"),
    "read_baseline": ("read", _PREFIX + "renewal/sources/"),
    "read_deployments": ("read", _PREFIX + "deployments/"),
    "read_policies": ("read", _PREFIX + "policies/"),
    "create_sources": ("create", _PREFIX + "renewal/sources/"),
    "create_policies": ("create", _PREFIX + "policies/"),
    "create_deployments": ("create", _PREFIX + "deployments/"),
}
# A denied create lands nothing on its own path, but the immutable objects the
# run created before it stay; nothing else is ever written.
_WRITTEN_BEFORE_DENIAL = {
    "create_policies": ["renewal/sources/"],
    "create_deployments": ["renewal/sources/", "policies/"],
}


class _Denied(RuntimeError):
    """Shaped as the native adapter's NativeError for a 403."""

    def __init__(self, call):
        super().__init__(f"{call}: http 403: forbidden")
        self.status = 403


def _objects_by_locator(job):
    """Store the index and grant where the sealed vector reads them."""
    state = job.lane.state()
    locators = {}
    for key, path in (
        ("release-index.json", job.lane.index_path),
        ("unattended-grant.json", job.grant_path),
    ):
        name = _PREFIX + "renewal/" + key
        state["objects"][name] = object_entry(path.read_bytes(), 11)
        locators[key] = f"object:gs://{release.BUCKET}/{name}#11"
    write_json(job.lane.state_path, state)
    job.journal_start = len(state["journal"])
    return locators


@pytest.mark.parametrize("case", [None, *sorted(_DENIALS)])
def test_a_denied_object_path_stops_the_run_before_the_ledger_moves(
    tmp_path, capsys, case
):
    job = Unattended(tmp_path)
    locators = _objects_by_locator(job)
    clients = recording_clients(job.lane.state_path)
    objects = clients["objects"]
    read, create = objects.read, objects.create
    operation, prefix = _DENIALS[case] if case else (None, None)

    def denied_read(name, **kwargs):
        if operation == "read" and name.startswith(prefix):
            raise _Denied("object_metadata")
        return read(name, **kwargs)

    def denied_create(name, raw, *, if_generation_match):
        if operation == "create" and name.startswith(prefix):
            raise _Denied("create_object")
        return create(name, raw, if_generation_match=if_generation_match)

    objects.read, objects.create = denied_read, denied_create
    argv = job.argv(
        index=locators["release-index.json"], grant=locators["unattended-grant.json"]
    )
    code, summary = run_main(argv, capsys, clients=clients)
    if case is None:
        assert (code, summary["state"]) == (0, "activated"), summary
        return
    assert code == 1, summary
    assert summary["state"] in ("refused", "failed"), summary
    assert "403" in summary["error"] or summary["error"].endswith("_unavailable"), (
        summary
    )
    written = [entry["call"] for entry in job.writes()]
    assert "deploy_revision" not in written and "update_traffic" not in written
    names = [entry.get("name", "") for entry in job.writes()]
    assert release.LEDGER_OBJECT not in names
    expected = [_PREFIX + item for item in _WRITTEN_BEFORE_DENIAL.get(case, [])]
    assert len(names) == len(expected)
    assert all(name.startswith(want) for name, want in zip(names, expected)), names


# Known residuals, pinned so a change to them is seen: the token lists do not
# read a decimal that ends a sentence or the exponent of a number in scientific
# notation. Both still activate, and only ever at the grant's rates.
_UNSEEN_CHANGES = {
    "decimal_before_a_full_stop": (
        b"<html>flash $0.75 $3.75. Batch input is 0.38.</html>",
        b"<html>flash $0.75 $3.75. Batch input is 10.38.</html>",
    ),
    "changed_exponent": (
        b"<html>flash $0.75 $3.75 or 7.5e-7 per unit</html>",
        b"<html>flash $0.75 $3.75 or 7.5e-6 per unit</html>",
    ),
}


@pytest.mark.parametrize("case", sorted(_UNSEEN_CHANGES))
def test_a_change_the_token_lists_cannot_see_keeps_the_grant_rates(
    tmp_path, capsys, case
):
    baseline, live = _UNSEEN_CHANGES[case]
    job = _with_baseline(tmp_path, baseline, live)
    code, summary = job.run(capsys)
    assert (code, summary["state"]) == (0, "activated"), summary
    pricing = json.loads(job.output.read_text(encoding="utf-8"))["policy"]["pricing"]
    assert (pricing["input_usd_per_million"], pricing["output_usd_per_million"]) == (
        "0.750000",
        "3.750000",
    )
