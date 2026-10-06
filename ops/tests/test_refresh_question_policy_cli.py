"""Command line over the C04 renewal controller: observe, renew and renew --dry-run.

Every test runs against the local double in fixtures/release_fake_adapter.py,
extended here with a byte reader that records the references it was asked for.
Nothing touches a network.
"""

import inspect
import json
import re
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from ops.deploy import refresh_question_policy, release
from ops.deploy import release_native_adapter as native
from ops.tests.test_foundation_release import (
    ADAPTER_PATH,
    DOC_PATH,
    LEDGER_NAME,
    MANIFEST_PATH,
    MANIFEST_SHA256,
    NOW,
    PRICING_PAGE,
    PRICING_SOURCE,
    ROOT,
    Renewal,
    fake,
    make_policy,
    observation,
    renewal_grant,
    sha,
    stamp,
    write_json,
)

THIS_FILE = Path(__file__).resolve()
MODULE = "ops.deploy.refresh_question_policy"
MODEL = make_policy()["model"]
OBJECT_PREFIX = (
    "gs://listening-post-staging-cache/" + release.OBJECT_PREFIX + "renewal/"
)


def recording_clients(state_path):
    """The fixture double plus a byte reader that journals every reference.

    The reader also resolves ``object:`` locators against objects that have
    been written, which is what the native reader does through its own object
    client. The stock double answers those only out of a preloaded table, so
    without this an object this tool just wrote would read back as missing.
    """
    clients = fake.clients(state_path)
    inner = clients["bytes"]
    objects = clients["objects"]
    state = clients["_state"]
    prefix = "object:gs://" + release.BUCKET + "/"

    class RecordingBytes:
        def read(self, reference):
            state.data.setdefault("byte_reads", []).append(reference)
            state.save()
            if isinstance(reference, str) and reference.startswith(prefix):
                name, _marker, generation = reference[len(prefix) :].rpartition("#")
                stored = objects.read(name, generation=generation)
                if stored is not None:
                    return stored["raw"]
            return inner.read(reference)

    clients["bytes"] = RecordingBytes()
    return clients


def run_main(argv, capsys, **kwargs):
    code = refresh_question_policy.main([str(item) for item in argv], **kwargs)
    lines = [
        line for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    return code, json.loads(lines[-1])


def run_module(*args):
    completed = subprocess.run(
        [sys.executable, "-m", MODULE, *[str(item) for item in args]],
        capture_output=True,
        cwd=str(ROOT),
        timeout=180,
    )
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
    )


class Lane:
    """A renewal scenario with the three documents written as local files."""

    def __init__(self, tmp_path, *, observed=None):
        self.renewal = Renewal(tmp_path)
        self.dir = tmp_path
        self.state_path = self.renewal.scenario.state_path
        self.observed = observed or observation(observed_at=NOW - timedelta(hours=1))
        self.index_path = write_json(
            tmp_path / "release-index.json", self.renewal.index
        )
        self.observation_path = write_json(tmp_path / "observation.json", self.observed)
        self.grant_path = write_json(tmp_path / "grant.json", renewal_grant())
        self.output = tmp_path / "renewal-receipt.json"

    def adapter(self, factory="clients", path=ADAPTER_PATH):
        return [
            "--adapter",
            f"{path}:{factory}",
            "--adapter-state",
            str(self.state_path),
            "--resources",
            str(MANIFEST_PATH),
        ]

    def renew_argv(self, *extra, output=None):
        return [
            "renew",
            "--release-index",
            self.index_path,
            "--observation",
            self.observation_path,
            "--grant",
            self.grant_path,
            "--output",
            output or self.output,
            *self.adapter(),
            *extra,
        ]

    def state(self):
        return self.renewal.scenario.current()

    def calls(self):
        return self.renewal.calls()


def observe_argv(
    lane,
    output,
    *extra,
    input_rate="0.750000",
    output_rate="3.750000",
    receipt=None,
):
    return [
        "observe",
        "--model",
        MODEL,
        "--input-usd-per-million",
        input_rate,
        "--output-usd-per-million",
        output_rate,
        "--output",
        output,
        "--grant",
        lane.grant_path,
        "--receipt",
        receipt or (Path(str(output)).parent / (Path(str(output)).name + ".receipt")),
        "--resources",
        str(MANIFEST_PATH),
        "--adapter",
        f"{ADAPTER_PATH}:clients",
        "--adapter-state",
        lane.state_path,
        *extra,
    ]


def refresh_with(lane, document, clients=None, grant=None):
    """Run the library against this lane with a byte reader that sees objects."""
    clients = clients or recording_clients(lane.state_path)
    clients["manifest"] = {"value": lane.renewal.manifest, "sha256": MANIFEST_SHA256}
    return refresh_question_policy.refresh_observed_policy(
        release_index=lane.renewal.index,
        observation=document,
        grant=grant or renewal_grant(),
        clients=clients,
    )


# Command line surface


def test_module_exposes_main_and_help_for_each_command():
    assert callable(getattr(refresh_question_policy, "main", None))
    for command, flags in {
        "observe": {
            "--adapter",
            "--adapter-state",
            "--model",
            "--input-usd-per-million",
            "--output-usd-per-million",
            "--location",
            "--service-tier",
            "--currency",
            "--output",
        },
        "renew": {
            "--release-index",
            "--observation",
            "--grant",
            "--adapter",
            "--adapter-state",
            "--output",
            "--dry-run",
        },
    }.items():
        code, stdout, stderr = run_module(command, "--help")
        assert code == 0, stderr
        for flag in flags:
            assert flag in stdout, (command, flag)
        assert "--source" not in stdout, command
    code, stdout, _stderr = run_module()
    assert code == 2 and stdout == ""


def test_documented_renewal_commands_only_name_existing_flags():
    text = DOC_PATH.read_text(encoding="utf-8")
    documented = {}
    for match in re.finditer(
        r"refresh_question_policy (observe|renew)((?:\s+--[a-z-]+(?:\s+[^\s`-][^\s`]*)?)+)",
        text,
    ):
        documented.setdefault(match.group(1), set()).update(
            re.findall(r"--[a-z-]+", match.group(2))
        )
    assert set(documented) == {"observe", "renew"}
    assert "--dry-run" in documented["renew"]
    for command, flags in documented.items():
        code, stdout, stderr = run_module(command, "--help")
        assert code == 0, stderr
        for flag in flags:
            assert flag in stdout, (command, flag)


# observe


def test_observe_writes_a_document_the_controller_accepts_and_renews(tmp_path, capsys):
    lane = Lane(tmp_path)
    output = tmp_path / "observation-written.json"
    receipt_path = tmp_path / "observe-receipt.json"
    code, summary = run_main(
        observe_argv(lane, output, receipt=receipt_path),
        capsys,
        clients=recording_clients(lane.state_path),
    )
    assert (code, summary["state"]) == (0, "observed"), summary
    document = json.loads(output.read_text(encoding="utf-8"))
    assert set(document) == refresh_question_policy._OBSERVATION_FIELDS
    assert document["contract_version"] == "42_pricing_observation_v2"
    assert document["source_url"] == PRICING_SOURCE
    assert document["source_sha256"] == sha(PRICING_PAGE)
    assert document["observed_at"] == stamp(NOW)
    assert document["model"] == MODEL
    assert (document["input_usd_per_million"], document["output_usd_per_million"]) == (
        "0.750000",
        "3.750000",
    )
    observed_at, rates = refresh_question_policy._check_observation(document, NOW)
    assert observed_at == NOW and [str(rate) for rate in rates] == [
        "0.750000",
        "3.750000",
    ]
    result = refresh_with(lane, document)
    assert result["state"] == "activated", result


def test_observe_pins_the_bytes_in_an_immutable_object_named_by_their_digest(
    tmp_path, capsys
):
    lane = Lane(tmp_path)
    output = tmp_path / "observation-written.json"
    receipt_path = tmp_path / "observe-receipt.json"
    code, summary = run_main(
        observe_argv(lane, output, receipt=receipt_path),
        capsys,
        clients=recording_clients(lane.state_path),
    )
    assert code == 0, summary
    name = refresh_question_policy.EVIDENCE_PREFIX + sha(PRICING_PAGE) + ".html"
    assert summary["source_object_name"] == name
    stored = lane.state()["objects"][name]
    assert fake.decode_raw(stored["raw_b64"]) == PRICING_PAGE
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["source_object"] == (
        f"object:gs://listening-post-staging-cache/{name}#{stored['generation']}"
    )
    created = [
        entry
        for entry in lane.state()["journal"]
        if entry["call"] == "create_object" and entry["name"] == name
    ]
    assert created and created[0]["if_generation_match"] == 0
    assert created[0]["sha256"] == sha(PRICING_PAGE)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["contract_version"] == refresh_question_policy.RECEIPT_CONTRACT
    assert receipt["command"] == "observe"
    assert receipt["inputs"]["source_url"] == PRICING_SOURCE
    assert receipt["source_object_generation"] == stored["generation"]
    assert receipt["observation"] == document


def test_observe_reuses_identical_pinned_bytes_and_refuses_different_ones(
    tmp_path, capsys
):
    lane = Lane(tmp_path)
    first = tmp_path / "first.json"
    code, summary = run_main(
        observe_argv(lane, first, receipt=tmp_path / "first-receipt.json"),
        capsys,
        clients=recording_clients(lane.state_path),
    )
    assert code == 0, summary
    name = summary["source_object_name"]
    generation = summary["source_object_generation"]
    second = tmp_path / "second.json"
    code, summary = run_main(
        observe_argv(lane, second, receipt=tmp_path / "second-receipt.json"),
        capsys,
        clients=recording_clients(lane.state_path),
    )
    assert (code, summary["state"]) == (0, "observed"), summary
    assert summary["source_object_generation"] == generation
    assert [
        entry["action"]
        for entry in json.loads(
            (tmp_path / "second-receipt.json").read_text(encoding="utf-8")
        )["mutations"]
    ] == ["reuse_immutable_artifact"]
    state = lane.state()
    state["objects"][name]["raw_b64"] = fake.encode_raw(b"someone replaced the bytes")
    write_json(lane.state_path, state)
    code, summary = run_main(
        observe_argv(lane, tmp_path / "third.json", receipt=tmp_path / "third.receipt"),
        capsys,
        clients=recording_clients(lane.state_path),
    )
    assert (code, summary["error"]) == (1, "immutable_object_conflict"), summary


def test_observe_refuses_before_writing_when_the_grant_is_absent_or_expired(
    tmp_path, capsys
):
    lane = Lane(tmp_path)
    before = lane.calls()
    expired = dict(renewal_grant(), expires_at=(NOW - timedelta(minutes=1)).isoformat())
    write_json(lane.grant_path, expired)
    code, summary = run_main(
        observe_argv(lane, tmp_path / "a.json", receipt=tmp_path / "a.receipt"),
        capsys,
        clients=recording_clients(lane.state_path),
    )
    assert (code, summary["error"]) == (1, "grant_expired"), summary
    assert lane.calls() == before
    assert not (tmp_path / "a.json").exists()
    lane.grant_path.unlink()
    code, summary = run_main(
        observe_argv(lane, tmp_path / "b.json", receipt=tmp_path / "b.receipt"),
        capsys,
        clients=recording_clients(lane.state_path),
    )
    assert (code, summary["error"]) == (1, "grant_unreadable"), summary
    assert lane.calls() == before


def test_observe_dry_run_renders_the_object_write_and_writes_no_object(
    tmp_path, capsys
):
    lane = Lane(tmp_path)
    before = lane.calls()
    output = tmp_path / "observation-written.json"
    receipt_path = tmp_path / "observe-receipt.json"
    code, summary = run_main(
        observe_argv(lane, output, "--dry-run", receipt=receipt_path),
        capsys,
        clients=recording_clients(lane.state_path),
    )
    assert (code, summary["state"]) == (0, "dry_run"), summary
    assert lane.calls() == before
    assert not output.exists()
    name = refresh_question_policy.EVIDENCE_PREFIX + sha(PRICING_PAGE) + ".html"
    assert name not in lane.state()["objects"]
    requests = summary["requests"]
    assert [item["call"] for item in requests] == ["create_object"]
    assert requests[0]["object"] == name
    assert requests[0]["if_generation_match"] == 0
    assert requests[0]["sha256"] == sha(PRICING_PAGE)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["contract_version"] == refresh_question_policy.RECEIPT_CONTRACT
    assert receipt["command"] == "observe --dry-run"
    assert receipt["state"] == "dry_run"
    assert receipt["requests"] == requests


def test_renew_proves_against_the_pinned_object_and_never_reads_the_live_page(
    tmp_path, capsys
):
    lane = Lane(tmp_path)
    output = tmp_path / "observation-written.json"
    code, summary = run_main(
        observe_argv(lane, output, receipt=tmp_path / "observe-receipt.json"),
        capsys,
        clients=recording_clients(lane.state_path),
    )
    assert code == 0, summary
    document = json.loads(output.read_text(encoding="utf-8"))
    write_json(lane.observation_path, document)
    state = lane.state()
    # The live page is gone from under us, exactly as a moved body would be.
    state["bytes"]["source:" + PRICING_SOURCE] = fake.encode_raw(b"<html>moved</html>")
    state["byte_reads"] = []
    write_json(lane.state_path, state)
    code, summary = run_main(
        lane.renew_argv(), capsys, clients=recording_clients(lane.state_path)
    )
    assert (code, summary["state"]) == (0, "activated"), summary
    reads = lane.state()["byte_reads"]
    assert ("source:" + PRICING_SOURCE) not in reads
    assert document["source_object"] in reads


def test_renew_refuses_a_pinned_object_whose_bytes_do_not_match_the_digest(
    tmp_path, capsys
):
    lane = Lane(tmp_path)
    output = tmp_path / "observation-written.json"
    code, summary = run_main(
        observe_argv(lane, output, receipt=tmp_path / "observe-receipt.json"),
        capsys,
        clients=recording_clients(lane.state_path),
    )
    assert code == 0, summary
    document = json.loads(output.read_text(encoding="utf-8"))
    write_json(lane.observation_path, document)
    state = lane.state()
    name = refresh_question_policy.EVIDENCE_PREFIX + sha(PRICING_PAGE) + ".html"
    state["objects"][name]["raw_b64"] = fake.encode_raw(
        b"<html>0.75 3.75 swapped</html>"
    )
    write_json(lane.state_path, state)
    before = lane.calls()
    code, summary = run_main(
        lane.renew_argv(), capsys, clients=recording_clients(lane.state_path)
    )
    assert (code, summary["error"]) == (1, "observation_source_mismatch"), summary
    assert lane.calls() == before


def test_a_locator_must_name_the_digest_it_claims_and_sit_in_the_evidence_prefix():
    digest = sha(PRICING_PAGE)
    good = (
        "object:gs://listening-post-staging-cache/"
        + refresh_question_policy.EVIDENCE_PREFIX
        + digest
        + ".html#7"
    )
    name, generation = refresh_question_policy._check_evidence_locator(good, digest)
    assert generation == "7"
    assert name.endswith(digest + ".html")
    shape = (
        "object:gs://other-bucket/"
        + refresh_question_policy.EVIDENCE_PREFIX
        + digest
        + ".html#1",
        "object:gs://listening-post-staging-cache/"
        + refresh_question_policy.EVIDENCE_PREFIX
        + digest
        + ".html",
    )
    for locator in shape:
        with pytest.raises(refresh_question_policy.Refused) as raised:
            refresh_question_policy._check_evidence_locator(locator, digest)
        assert str(raised.value) == "observation_object_invalid", locator
    # A locator that names content other than the digest it is checked against
    # is refused before anything is read.
    misnamed = (
        "object:gs://listening-post-staging-cache/"
        + refresh_question_policy.EVIDENCE_PREFIX
        + sha(b"some other body")
        + ".html#1",
        "object:gs://listening-post-staging-cache/somewhere/else.html#1",
        "object:gs://listening-post-staging-cache/"
        + refresh_question_policy.EVIDENCE_PREFIX
        + digest
        + ".txt#1",
    )
    for locator in misnamed:
        with pytest.raises(refresh_question_policy.Refused) as raised:
            refresh_question_policy._check_evidence_locator(locator, digest)
        assert str(raised.value) == "observation_object_name_mismatch", locator


def test_a_forged_body_under_a_name_that_does_not_match_is_refused_unread(
    tmp_path, capsys
):
    """The reviewer's forge: a small body pinned under someone else's name."""
    lane = Lane(tmp_path)
    forged = b"<html>0.75 3.75</html>"
    name = refresh_question_policy.EVIDENCE_PREFIX + sha(PRICING_PAGE) + ".html"
    state = lane.state()
    state["objects"][name] = {"generation": "55", "raw_b64": fake.encode_raw(forged)}
    write_json(lane.state_path, state)
    document = dict(
        lane.observed,
        contract_version="42_pricing_observation_v2",
        source_sha256=sha(forged),
        source_object=f"object:gs://listening-post-staging-cache/{name}#55",
    )
    write_json(lane.observation_path, document)
    before = lane.calls()
    code, summary = run_main(
        lane.renew_argv(), capsys, clients=recording_clients(lane.state_path)
    )
    assert (code, summary["error"]) == (1, "observation_object_name_mismatch"), summary
    assert lane.calls() == before
    assert not lane.state().get("byte_reads")


def test_observe_refuses_an_existing_output_before_reading_the_source(tmp_path, capsys):
    lane = Lane(tmp_path)
    state = lane.state()
    del state["bytes"]["source:" + PRICING_SOURCE]
    write_json(lane.state_path, state)
    output = tmp_path / "observation-written.json"
    code, summary = run_main(observe_argv(lane, output), capsys)
    assert (code, summary["error"]) == (1, "observation_source_unreadable"), summary
    output.write_bytes(b"existing")
    code, summary = run_main(observe_argv(lane, output), capsys)
    assert (code, summary["error"]) == (1, "output_exists"), summary
    assert output.read_bytes() == b"existing"


def test_observe_never_accepts_a_source_url_argument(tmp_path, capsys):
    lane = Lane(tmp_path)
    output = tmp_path / "observation-written.json"
    for flag in ("--source-url", "--source", "--url"):
        with pytest.raises(SystemExit) as raised:
            refresh_question_policy.main(
                [str(item) for item in observe_argv(lane, output)]
                + [flag, "https://example.invalid/pricing"]
            )
        assert raised.value.code == 2
    assert not output.exists()


# renew


def test_renew_happy_path_writes_the_receipt_the_library_returns(tmp_path, capsys):
    (tmp_path / "library").mkdir()
    (tmp_path / "cli").mkdir()
    library = Renewal(tmp_path / "library")
    expected = library.refresh(observation(observed_at=NOW - timedelta(hours=1)))
    assert expected["state"] == "activated", expected
    lane = Lane(tmp_path / "cli")
    code, summary = run_main(lane.renew_argv(), capsys)
    assert (code, summary["state"]) == (0, "activated"), summary
    assert summary["output"] == str(lane.output)
    receipt = json.loads(lane.output.read_text(encoding="utf-8"))
    assert receipt["contract_version"] == refresh_question_policy.RECEIPT_CONTRACT
    assert receipt["command"] == "renew"
    assert set(receipt["inputs"]) == {"release_index", "observation", "grant"}
    assert receipt["inputs"]["observation"]["path"] == str(lane.observation_path)
    envelope = {"contract_version", "command", "inputs"}
    assert {key: value for key, value in receipt.items() if key not in envelope} == (
        expected
    )
    assert lane.calls() == library.calls()
    ledger, _generation = lane.renewal.scenario.ledger_now()
    assert ledger["active_deployment_digest"] == receipt["binding"]["deployment_digest"]
    calls_after = lane.calls()
    code, summary = run_main(
        lane.renew_argv(output=tmp_path / "cli" / "second.json"), capsys
    )
    assert (code, summary["state"]) == (0, "already_activated"), summary
    assert lane.calls() == calls_after


def test_renew_stale_observation_refuses_with_the_controller_reason(tmp_path, capsys):
    lane = Lane(tmp_path, observed=observation(observed_at=NOW - timedelta(hours=25)))
    before = lane.calls()
    code, summary = run_main(lane.renew_argv(), capsys)
    assert (code, summary["state"], summary["error"]) == (
        1,
        "refused",
        "observation_stale",
    ), summary
    receipt = json.loads(lane.output.read_text(encoding="utf-8"))
    assert (receipt["state"], receipt["error"]) == ("refused", "observation_stale")
    assert receipt["mutations"] == [] and receipt["ledger_moved"] is False
    assert lane.calls() == before


def test_renew_refuses_an_existing_output_before_any_native_call(tmp_path, capsys):
    lane = Lane(tmp_path)
    lane.output.write_bytes(b"existing")
    broken = write_json(tmp_path / "broken-state.json", {})
    argv = lane.renew_argv()
    argv[argv.index("--adapter-state") + 1] = broken
    code, summary = run_main(argv, capsys)
    assert (code, summary["error"]) == (1, "output_exists"), summary
    assert lane.output.read_bytes() == b"existing"
    code, summary = run_main([*argv, "--dry-run"], capsys)
    assert (code, summary["error"]) == (1, "output_exists"), summary
    assert lane.output.read_bytes() == b"existing"


def test_renew_dry_run_sends_no_write_and_lists_the_requests_renew_then_sends(
    tmp_path, capsys
):
    lane = Lane(tmp_path)
    before = lane.calls()
    ledger_before, generation_before = lane.renewal.scenario.ledger_now()
    dry_receipt = tmp_path / "dry-run-receipt.json"
    code, summary = run_main(lane.renew_argv("--dry-run", output=dry_receipt), capsys)
    assert (code, summary["state"]) == (0, "dry_run"), summary
    # The dry run is evidence too, so it writes its own receipt.
    written = json.loads(dry_receipt.read_text(encoding="utf-8"))
    assert written["contract_version"] == refresh_question_policy.RECEIPT_CONTRACT
    assert written["command"] == "renew --dry-run"
    assert set(written["inputs"]) == {"release_index", "observation", "grant"}
    assert written["state"] == "dry_run"
    assert not lane.output.exists()
    assert lane.calls() == before
    assert lane.renewal.scenario.ledger_now() == (ledger_before, generation_before)
    assert summary["observation_source_sha256"] == sha(PRICING_PAGE)
    assert summary["ledger_generation"] == generation_before
    requests = summary["requests"]
    assert [item["call"] for item in requests] == [
        "create_object",
        "create_object",
        "deploy_revision",
        "create_object",
        "update_traffic",
    ]
    assert all(item["action"] in {"write", "deploy"} for item in requests)
    policy_digest = summary["policy"]["policy_digest"]
    deployment_digest = summary["binding"]["deployment_digest"]
    assert requests[0]["object"].endswith(f"policies/{policy_digest}/policy.json")
    assert requests[1]["object"].endswith(
        f"deployments/{deployment_digest}/binding.json"
    )
    assert requests[2]["revision"] == summary["binding"]["revision_name"]
    assert requests[3]["object"] == LEDGER_NAME
    assert requests[3]["if_generation_match"] == int(generation_before)
    assert requests[4]["traffic"][0]["revision"] == summary["binding"]["revision_name"]
    # The route etag is only issued once the new revision exists, so the plan
    # says it cannot know it rather than printing the pre deploy value.
    assert requests[4]["etag"] is None
    assert requests[4]["etag_unknown_until_deployed"] is True

    code, real = run_main(lane.renew_argv(), capsys)
    assert (code, real["state"]) == (0, "activated"), real
    receipt = json.loads(lane.output.read_text(encoding="utf-8"))
    assert receipt["binding"] == summary["binding"]
    assert receipt["policy"] == summary["policy"]
    sent = [
        entry
        for entry in lane.state()["journal"][len(before) :]
        if entry["call"] != "guard"
    ]
    assert [entry["call"] for entry in sent] == [item["call"] for item in requests]
    assert sent[0]["name"] == requests[0]["object"]
    assert sent[1]["name"] == requests[1]["object"]
    assert sent[2]["revision"] == requests[2]["revision"]
    assert sent[2]["idempotency_key"] == requests[2]["idempotency_key"]
    assert sent[3]["name"] == LEDGER_NAME
    assert sent[3]["if_generation_match"] == requests[3]["if_generation_match"]
    assert sent[3]["sha256"] == requests[3]["sha256"]
    assert sent[4]["traffic"] == requests[4]["traffic"]
    # Everything the plan claimed to know matches what went out, and the one
    # field it declined to guess is the one it could not have known.
    assert sent[4]["etag"] and sent[4]["etag"] != requests[4]["etag"]


def test_renew_dry_run_refuses_what_renew_refuses_and_records_it(tmp_path, capsys):
    lane = Lane(tmp_path, observed=observation(observed_at=NOW - timedelta(hours=25)))
    before = lane.calls()
    code, summary = run_main(lane.renew_argv("--dry-run"), capsys)
    assert (code, summary["state"], summary["error"]) == (
        1,
        "refused",
        "observation_stale",
    ), summary
    written = json.loads(lane.output.read_text(encoding="utf-8"))
    assert written["command"] == "renew --dry-run"
    assert (written["state"], written["error"]) == ("refused", "observation_stale")
    assert set(written["inputs"]) == {"release_index", "observation", "grant"}
    assert lane.calls() == before


def test_renew_reads_object_locators_through_the_byte_reader(tmp_path, capsys):
    lane = Lane(tmp_path)
    locators = {
        "release-index": OBJECT_PREFIX + "release-index.json#11",
        "observation": OBJECT_PREFIX + "observation.json#12",
        "grant": OBJECT_PREFIX + "grant.json#13",
    }
    documents = {
        "release-index": lane.renewal.index,
        "observation": lane.observed,
        "grant": renewal_grant(),
    }
    state = lane.state()
    for name, locator in locators.items():
        state["bytes"]["object:" + locator] = fake.encode_raw(
            json.dumps(documents[name]).encode("utf-8")
        )
    write_json(lane.state_path, state)
    argv = [
        "renew",
        "--release-index",
        "object:" + locators["release-index"],
        "--observation",
        "object:" + locators["observation"],
        "--grant",
        "object:" + locators["grant"],
        "--output",
        lane.output,
        *lane.adapter(),
    ]
    code, summary = run_main(argv, capsys, clients=recording_clients(lane.state_path))
    assert (code, summary["state"]) == (0, "activated"), summary
    reads = lane.state()["byte_reads"]
    assert reads[:3] == ["object:" + locators[name] for name in locators]
    assert "source:" + PRICING_SOURCE in reads[3:]
    assert summary["inputs"]["observation"]["sha256"] == sha(
        json.dumps(documents["observation"]).encode("utf-8")
    )


def test_renew_refuses_a_locator_outside_the_approved_bucket_or_unreadable(
    tmp_path, capsys
):
    lane = Lane(tmp_path)
    before = lane.calls()
    for locator, error in (
        ("object:gs://other-bucket/renewal/grant.json#1", "grant_locator_invalid"),
        (
            "object:gs://listening-post-staging-cache/renewal/grant.json",
            "grant_locator_invalid",
        ),
        ("object:" + OBJECT_PREFIX + "missing.json#1", "grant_unreadable"),
    ):
        argv = lane.renew_argv()
        argv[argv.index("--grant") + 1] = locator
        code, summary = run_main(
            argv, capsys, clients=recording_clients(lane.state_path)
        )
        assert (code, summary["error"]) == (1, error), (locator, summary)
        assert not lane.output.exists()
    assert lane.calls() == before
    reads = lane.state().get("byte_reads", [])
    assert reads == ["object:" + OBJECT_PREFIX + "missing.json#1"]


def test_renew_loads_the_adapter_from_the_command_line(tmp_path):
    lane = Lane(tmp_path)
    argv = [
        "renew",
        "--release-index",
        lane.index_path,
        "--observation",
        lane.observation_path,
        "--grant",
        lane.grant_path,
        "--output",
        lane.output,
        *lane.adapter(factory="recording_clients", path=THIS_FILE),
    ]
    code, stdout, stderr = run_module(*argv)
    assert code == 0, stderr
    summary = json.loads(
        [line for line in stdout.splitlines() if line.startswith("{")][-1]
    )
    assert summary["state"] == "activated"
    assert "source:" + PRICING_SOURCE in lane.state()["byte_reads"]
    receipt = json.loads(lane.output.read_text(encoding="utf-8"))
    assert receipt["state"] == "activated"


# One fetch per run, and a source whose body moves between reads.


def unstable_clients(state_path, bodies):
    """A reader that answers each source read with the next body, and records reads."""
    clients = recording_clients(state_path)
    inner = clients["bytes"]
    remaining = list(bodies)

    class UnstableBytes:
        def read(self, reference):
            if reference.startswith("source:"):
                inner.read(reference)
                return remaining.pop(0)
            return inner.read(reference)

    clients["bytes"] = UnstableBytes()
    return clients


def broken_clients(state_path, error):
    """A reader that fails the way a transport failure fails, by raising."""
    clients = recording_clients(state_path)
    inner = clients["bytes"]

    class BrokenBytes:
        def read(self, reference):
            if reference.startswith("source:"):
                inner.read(reference)
                raise error
            return inner.read(reference)

    clients["bytes"] = BrokenBytes()
    return clients


def source_reads(lane):
    return [
        reference
        for reference in lane.state().get("byte_reads", [])
        if reference.startswith("source:")
    ]


def test_observe_reads_the_source_once_and_survives_a_body_that_moves(tmp_path, capsys):
    """The live pricing page answers different bytes to every request.

    A second fetch inside the proof would compare the digest of one response
    against the digest of another and refuse a correct observation.
    """
    lane = Lane(tmp_path)
    bodies = [
        PRICING_PAGE.replace(b"</html>", b"<!-- first -->  </html>"),
        PRICING_PAGE.replace(b"</html>", b"<!-- second -->   </html>"),
        PRICING_PAGE.replace(b"</html>", b"<!-- third -->    </html>"),
    ]
    assert len({sha(body) for body in bodies}) == 3
    output = tmp_path / "observation-written.json"
    code, summary = run_main(
        observe_argv(lane, output),
        capsys,
        clients=unstable_clients(lane.state_path, bodies),
    )
    assert (code, summary["state"]) == (0, "observed"), summary
    assert source_reads(lane) == ["source:" + PRICING_SOURCE]
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["source_sha256"] == sha(bodies[0])
    assert summary["source_sha256"] == sha(bodies[0])


def test_observe_separates_a_reader_outage_from_a_refused_reference(tmp_path, capsys):
    lane = Lane(tmp_path)
    output = tmp_path / "observation-written.json"
    code, summary = run_main(
        observe_argv(lane, output, receipt=tmp_path / "outage.receipt"),
        capsys,
        clients=broken_clients(lane.state_path, OSError("connection reset")),
    )
    assert (code, summary["error"]) == (1, "observation_source_unavailable"), summary
    assert not output.exists()
    state = lane.state()
    del state["bytes"]["source:" + PRICING_SOURCE]
    write_json(lane.state_path, state)
    code, summary = run_main(
        observe_argv(lane, output, receipt=tmp_path / "declined.receipt"),
        capsys,
        clients=recording_clients(lane.state_path),
    )
    assert (code, summary["error"]) == (1, "observation_source_unreadable"), summary
    assert not output.exists()
    with pytest.raises(refresh_question_policy.Refused) as raised:
        refresh_question_policy._read_source_bytes(
            recording_clients(lane.state_path), "https://cloud.google.com.evil.test/x"
        )
    assert str(raised.value) == "observation_source_not_approved"
    assert source_reads(lane) == ["source:" + PRICING_SOURCE] * 2


def test_the_proof_never_fetches_and_renew_reads_the_source_once(tmp_path, capsys):
    signature = inspect.signature(refresh_question_policy._verify_source_bytes)
    assert list(signature.parameters) == ["raw", "observation", "rates"]
    lane = Lane(tmp_path)
    code, summary = run_main(
        lane.renew_argv(), capsys, clients=recording_clients(lane.state_path)
    )
    assert (code, summary["state"]) == (0, "activated"), summary
    assert source_reads(lane) == ["source:" + PRICING_SOURCE]


def test_the_proof_still_recomputes_the_digest_it_compares(tmp_path, capsys):
    """Renew proves the observation against bytes it read, never against itself."""
    lane = Lane(tmp_path)
    claimed = sha(b"bytes nobody served")
    document = dict(lane.observed, source_sha256=claimed)
    write_json(lane.observation_path, document)
    before = lane.calls()
    code, summary = run_main(lane.renew_argv(), capsys)
    assert (code, summary["state"], summary["error"]) == (
        1,
        "refused",
        "observation_source_mismatch",
    ), summary
    assert lane.calls() == before


# Recovery, freshness, and what the rate check is actually worth.


def test_a_renewal_broken_between_the_ledger_and_the_route_is_not_reported_well(
    tmp_path, capsys
):
    """Break a real run partway, then rerun it, and read what the rerun says.

    The ledger write lands and the route fails, so the ledger names a
    deployment Cloud Run is not serving. A rerun that decided from the ledger
    alone would call that already_activated and exit 0 over an outage.
    """
    lane = Lane(tmp_path)
    state = lane.state()
    state["failures"]["update_traffic"] = "failed"
    write_json(lane.state_path, state)
    code, first = run_main(lane.renew_argv(), capsys)
    assert code == 1, first
    assert first["state"] == refresh_question_policy.LEDGER_MOVED_STATE, first

    ledger, _generation = lane.renewal.scenario.ledger_now()
    split_digest = ledger["active_deployment_digest"]
    served = lane.state()["service"]["trafficStatuses"][0]["revision"]
    assert served != first["deployment_digest"]

    state = lane.state()
    del state["failures"]["update_traffic"]
    write_json(lane.state_path, state)
    calls_before_rerun = lane.calls()
    code, second = run_main(
        lane.renew_argv(output=tmp_path / "second-receipt.json"), capsys
    )
    assert code == 1, second
    assert second["state"] == refresh_question_policy.SPLIT_STATE, second
    assert second["error"] == "ledger_active_revision_not_served"
    split = second["split"]
    assert split["ledger_active_deployment_digest"] == split_digest
    assert split["renewal_deployment_digest"] == first["deployment_digest"]
    assert split["ledger_claims_active"] is True
    assert split["serving"] is False
    assert split["expected_revision_name"] == second["binding"]["revision_name"]
    assert split["expected_traffic"][0]["percent"] == 100
    assert split["served_revisions"] == [served]
    assert "approval_required" in split["remedy"]
    assert "natively" in split["remedy"]
    # It repairs nothing by itself, and says so rather than retrying.
    assert lane.calls() == calls_before_rerun
    written = json.loads((tmp_path / "second-receipt.json").read_text(encoding="utf-8"))
    assert written["state"] == refresh_question_policy.SPLIT_STATE
    assert written["split"] == split


def test_a_completed_renewal_rerun_is_already_activated_from_the_served_revision(
    tmp_path, capsys
):
    lane = Lane(tmp_path)
    code, first = run_main(lane.renew_argv(), capsys)
    assert (code, first["state"]) == (0, "activated"), first
    code, second = run_main(
        lane.renew_argv(output=tmp_path / "second-receipt.json"), capsys
    )
    assert (code, second["state"]) == (0, "already_activated"), second
    assert second["active"] is True
    written = json.loads((tmp_path / "second-receipt.json").read_text(encoding="utf-8"))
    assert written["served_revisions"] == [written["binding"]["revision_name"]]
    assert written["ledger_claims_active"] is True


def test_an_observation_too_close_to_expiry_to_outlive_a_question_is_refused(
    tmp_path, capsys
):
    margin = refresh_question_policy.OBSERVATION_MARGIN
    validity = refresh_question_policy.OBSERVATION_VALIDITY
    assert margin.total_seconds() == 2 * 600.0 + 240
    too_old = observation(observed_at=NOW - (validity - margin) - timedelta(seconds=1))
    lane = Lane(tmp_path, observed=too_old)
    before = lane.calls()
    code, summary = run_main(lane.renew_argv(), capsys)
    assert (code, summary["error"]) == (
        1,
        "observation_expires_before_a_question_can_run",
    ), summary
    assert lane.calls() == before
    # One second the other side of the same boundary still runs.
    fresh = observation(observed_at=NOW - (validity - margin) + timedelta(seconds=1))
    (tmp_path / "ok").mkdir()
    other = Lane(tmp_path / "ok", observed=fresh)
    code, summary = run_main(other.renew_argv(), capsys)
    assert (code, summary["state"]) == (0, "activated"), summary


REALISTIC_PAGE = (
    b"<!doctype html><html><head><title>Vertex AI pricing</title>"
    b"<script nonce='a1b2c3'>window.p=1</script></head><body>"
    b"<table><tr><th>Model</th><th>Input</th><th>Output</th></tr>"
    b"<tr><td>flash</td><td>$0.75</td><td>$3.75</td></tr>"
    b"<tr><td>pro</td><td>$1.50</td><td>$9.00</td></tr>"
    b"<tr><td>lite</td><td>$0.10</td><td>$0.40</td></tr></table>"
    b"<p>Free tier covers 0.8 million characters per month, "
    b"with 7.5 requests per second and 37.5 GB of storage. "
    b"Committed use discounts reach 9 percent at 0.375 of list.</p>"
    b"<footer>Updated 2026. Prices in USD. 1.5x for global tier.</footer>"
    b"</body></html>"
)


def test_the_rate_check_is_a_substring_search_and_the_test_says_what_that_means(
    tmp_path, capsys
):
    """What the byte check does and does not catch, against a realistic body.

    A seventy four byte stub made this look like a rate check. It is not one.
    Every wrong pairing below is accepted because its digits occur somewhere in
    the page, and only digits that appear nowhere at all are refused.
    """
    text = REALISTIC_PAGE.decode("ascii")
    assert text.count("0.8") >= 1 and text.count("0.75") >= 1
    accepted = [
        ("0.750000", "3.750000"),  # correct
        ("3.750000", "0.750000"),  # swapped
        ("1.500000", "9.000000"),  # the other model's rates
        ("7.500000", "37.500000"),  # both rates a factor of ten out
        ("9.000000", "9.000000"),  # the same number for both fields
        ("0.800000", "3.750000"),  # a number that is not a rate at all
    ]
    for index, (input_rate, output_rate) in enumerate(accepted):
        (tmp_path / f"a{index}").mkdir()
        lane = Lane(tmp_path / f"a{index}")
        state = lane.state()
        state["bytes"]["source:" + PRICING_SOURCE] = fake.encode_raw(REALISTIC_PAGE)
        write_json(lane.state_path, state)
        code, summary = run_main(
            observe_argv(
                lane,
                tmp_path / f"a{index}" / "doc.json",
                input_rate=input_rate,
                output_rate=output_rate,
                receipt=tmp_path / f"a{index}" / "r.json",
            ),
            capsys,
            clients=recording_clients(lane.state_path),
        )
        assert (code, summary["state"]) == (0, "observed"), (input_rate, summary)
    (tmp_path / "absent").mkdir()
    lane = Lane(tmp_path / "absent")
    state = lane.state()
    state["bytes"]["source:" + PRICING_SOURCE] = fake.encode_raw(REALISTIC_PAGE)
    write_json(lane.state_path, state)
    code, summary = run_main(
        observe_argv(
            lane,
            tmp_path / "absent" / "doc.json",
            input_rate="4.242000",
            output_rate="3.750000",
            receipt=tmp_path / "absent" / "r.json",
        ),
        capsys,
        clients=recording_clients(lane.state_path),
    )
    assert (code, summary["error"]) == (1, "observation_rate_not_evidenced"), summary


def test_a_rate_that_differs_from_the_approved_profile_is_surfaced(tmp_path, capsys):
    lane = Lane(
        tmp_path,
        observed=observation(observed_at=NOW - timedelta(hours=1), page=REALISTIC_PAGE),
    )
    state = lane.state()
    state["bytes"]["source:" + PRICING_SOURCE] = fake.encode_raw(REALISTIC_PAGE)
    write_json(lane.state_path, state)
    code, summary = run_main(lane.renew_argv(), capsys)
    assert (code, summary["state"]) == (0, "activated"), summary
    assert summary["rate_changed"] is False
    written = json.loads(lane.output.read_text(encoding="utf-8"))
    assert written["rate_changed"] is False

    (tmp_path / "moved").mkdir()
    moved = Lane(
        tmp_path / "moved",
        observed=observation(
            observed_at=NOW - timedelta(hours=1),
            input_rate="0.375000",
            output_rate="3.750000",
            page=REALISTIC_PAGE,
        ),
    )
    state = moved.state()
    state["bytes"]["source:" + PRICING_SOURCE] = fake.encode_raw(REALISTIC_PAGE)
    write_json(moved.state_path, state)
    code, summary = run_main(moved.renew_argv(), capsys)
    assert (code, summary["state"]) == (0, "activated"), summary
    assert summary["rate_changed"] is True
    written = json.loads(moved.output.read_text(encoding="utf-8"))
    assert written["rate_changed"] is True


def test_the_managed_job_argument_vector_is_no_longer_the_bare_module(tmp_path):
    """The bare module cannot renew anything; the shipped vector names a form.

    The vector used to be the bare module, which argparse rejects. It is now
    the sealed renew-unattended form, pinned in
    test_refresh_question_policy_unattended.py; the attended renew form still
    parses with object locators.
    """
    config = json.loads(
        (ROOT / "infra" / "runtime" / "daily-staging.json").read_bytes()
    )
    shipped = config["jobs"]["intelligence-42-price-policy-staging"]["args"]
    assert shipped[:2] == ["-m", "ops.deploy.refresh_question_policy"]
    assert shipped != ["-m", "ops.deploy.refresh_question_policy"]
    with pytest.raises(SystemExit) as raised:
        refresh_question_policy.build_parser().parse_args([])
    assert raised.value.code == 2
    assert refresh_question_policy.build_parser().parse_args(shipped[2:]).command == (
        "renew-unattended"
    )
    corrected = CORRECTED_JOB_ARGS[2:]
    parsed = refresh_question_policy.build_parser().parse_args(corrected)
    assert parsed.command == "renew"
    assert parsed.observation.startswith("object:gs://listening-post-staging-cache/")
    assert parsed.adapter_state == "adc"


_RENEWAL_OBJECTS = (
    "object:gs://listening-post-staging-cache/open-intelligence/v2/staging/"
    "general-questions/renewal/"
)
CORRECTED_JOB_ARGS = [
    "-m",
    "ops.deploy.refresh_question_policy",
    "renew",
    "--release-index",
    _RENEWAL_OBJECTS + "release-index.json#<generation>",
    "--observation",
    _RENEWAL_OBJECTS + "observation.json#<generation>",
    "--grant",
    _RENEWAL_OBJECTS + "grant.json#<generation>",
    "--adapter",
    "/app/ops/deploy/release_native_adapter.py:factory",
    "--adapter-state",
    "adc",
    "--output",
    "/tmp/renewal-receipt.json",
]
# All four ways the ledger and the served revision can stand, and what a lost
# write response is worth.


def _activated(lane, capsys):
    code, receipt = run_main(lane.renew_argv(), capsys)
    assert (code, receipt["state"]) == (0, "activated"), receipt
    return receipt


@pytest.mark.parametrize("newer_first", [False, True])
@pytest.mark.parametrize("active_and_serving", [True, False])
def test_existing_activation_evaluates_all_policy_bindings(
    tmp_path, capsys, newer_first, active_and_serving
):
    lane = Lane(tmp_path)
    receipt = _activated(lane, capsys)
    clients = recording_clients(lane.state_path)
    newer = receipt["binding"]
    older = {
        **newer,
        "deployment_digest": "0" * 64,
        "revision_name": "retired-renewal-revision",
    }
    clients["objects"].create(
        release.OBJECT_PREFIX
        + f"deployments/{older['deployment_digest']}/binding.json",
        release.canonical_bytes(older),
        if_generation_match=0,
    )
    bindings = [newer, older] if newer_first else [older, newer]
    ledger = {
        "bindings": {
            item["deployment_digest"]: receipt["policy"]["policy_digest"]
            for item in bindings
        },
        "active_deployment_digest": newer["deployment_digest"]
        if active_and_serving
        else "1" * 64,
    }
    if not active_and_serving:
        clients["_state"].data["service"]["traffic"] = []
        clients["_state"].data["service"]["trafficStatuses"] = []
    calls_before = lane.calls()

    result = refresh_question_policy._existing_activation(
        clients, ledger, receipt["policy"]
    )

    if active_and_serving:
        assert result["state"] == "already_activated", result
        assert result["binding"]["deployment_digest"] == newer["deployment_digest"]
    else:
        assert result["error"] == "renewal_policy_bound_to_retired_deployments"
        assert result["active"] is False
        assert {
            item["renewal_deployment_digest"]: (
                item["ledger_claims_active"],
                item["serving"],
            )
            for item in result["split"]["bound_deployments"]
        } == {
            older["deployment_digest"]: (False, False),
            newer["deployment_digest"]: (False, False),
        }
    assert lane.calls() == calls_before


def test_all_four_ledger_and_serving_combinations_are_decided_explicitly(
    tmp_path, capsys
):
    """Only one of the four is a success and none of them falls through.

    The three unhappy ones each leave the app refusing every question with
    approval_required, so an exit zero on any of them would report health over
    an outage.
    """
    outcomes = {}

    # claimed and serving: the renewal finished.
    (tmp_path / "both").mkdir()
    lane = Lane(tmp_path / "both")
    _activated(lane, capsys)
    code, again = run_main(
        lane.renew_argv(output=tmp_path / "both" / "again.json"), capsys
    )
    outcomes["claimed_and_serving"] = (code, again["state"])
    assert again["active"] is True

    # claimed, not serving: the route never landed.
    (tmp_path / "ledger-ahead").mkdir()
    lane = Lane(tmp_path / "ledger-ahead")
    state = lane.state()
    state["failures"]["update_traffic"] = "failed"
    write_json(lane.state_path, state)
    code, _first = run_main(lane.renew_argv(), capsys)
    assert code == 1
    state = lane.state()
    del state["failures"]["update_traffic"]
    write_json(lane.state_path, state)
    code, again = run_main(
        lane.renew_argv(output=tmp_path / "ledger-ahead" / "again.json"), capsys
    )
    outcomes["claimed_not_serving"] = (code, again["state"])

    # serving, not claimed: traffic moved and the ledger did not follow.
    (tmp_path / "served-ahead").mkdir()
    lane = Lane(tmp_path / "served-ahead")
    receipt = _activated(lane, capsys)
    state = lane.state()
    ledger = json.loads(fake.decode_raw(state["objects"][LEDGER_NAME]["raw_b64"]))
    ledger["active_deployment_digest"] = lane.renewal.scenario.binding[
        "deployment_digest"
    ]
    state["objects"][LEDGER_NAME]["raw_b64"] = fake.encode_raw(
        release.canonical_bytes(ledger)
    )
    write_json(lane.state_path, state)
    code, again = run_main(
        lane.renew_argv(output=tmp_path / "served-ahead" / "again.json"), capsys
    )
    outcomes["serving_not_claimed"] = (code, again["state"])
    assert again["split"]["renewal_deployment_digest"] == (receipt["deployment_digest"])

    # neither: the policy is bound to a deployment nothing activates or serves.
    (tmp_path / "inert").mkdir()
    lane = Lane(tmp_path / "inert")
    receipt = _activated(lane, capsys)
    state = lane.state()
    ledger = json.loads(fake.decode_raw(state["objects"][LEDGER_NAME]["raw_b64"]))
    ledger["active_deployment_digest"] = lane.renewal.scenario.binding[
        "deployment_digest"
    ]
    state["objects"][LEDGER_NAME]["raw_b64"] = fake.encode_raw(
        release.canonical_bytes(ledger)
    )
    state["service"]["traffic"] = []
    state["service"]["trafficStatuses"] = []
    write_json(lane.state_path, state)
    code, again = run_main(
        lane.renew_argv(output=tmp_path / "inert" / "again.json"), capsys
    )
    outcomes["neither"] = (code, again["state"])

    assert outcomes == {
        "claimed_and_serving": (0, "already_activated"),
        "claimed_not_serving": (1, refresh_question_policy.SPLIT_STATE),
        "serving_not_claimed": (1, refresh_question_policy.SERVED_AHEAD_STATE),
        "neither": (1, refresh_question_policy.INERT_STATE),
    }
    # Exactly one of the four exits zero, and it is the serving one.
    assert [code for code, _state in outcomes.values()].count(0) == 1


def test_a_revision_taking_a_sliver_of_traffic_is_not_already_activated(
    tmp_path, capsys
):
    """Membership in the traffic list is not the question the acting path asks."""
    lane = Lane(tmp_path)
    receipt = _activated(lane, capsys)
    revision = receipt["binding"]["revision_name"]
    state = lane.state()
    other = lane.renewal.scenario.binding["revision_name"]
    state["service"]["traffic"] = [
        {
            "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
            "revision": revision,
            "percent": 1,
            "tag": state["service"]["traffic"][0]["tag"],
        },
        {
            "type": "TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION",
            "revision": other,
            "percent": 99,
            "tag": state["service"]["traffic"][0]["tag"],
        },
    ]
    state["service"]["trafficStatuses"] = [
        dict(item) for item in state["service"]["traffic"]
    ]
    write_json(lane.state_path, state)
    code, again = run_main(lane.renew_argv(output=tmp_path / "sliver.json"), capsys)
    assert code == 1, again
    assert again["state"] == refresh_question_policy.SPLIT_STATE, again
    assert again["split"]["serving"] is False
    assert revision in again["split"]["served_revisions"]


def test_a_lost_ledger_write_response_is_not_called_a_concurrent_deployment(
    tmp_path, capsys
):
    """The ledger moved and the response did not come back. Read it, then label it."""
    lane = Lane(tmp_path)
    clients = recording_clients(lane.state_path)
    inner = clients["objects"]

    class LosesTheLedgerResponse:
        def read(self, name, *, generation=None):
            return inner.read(name, generation=generation)

        def create(self, name, raw, *, if_generation_match):
            result = inner.create(name, raw, if_generation_match=if_generation_match)
            if name == LEDGER_NAME:
                raise TimeoutError("response lost after the write committed")
            return result

    clients["objects"] = LosesTheLedgerResponse()
    clients["manifest"] = {"value": lane.renewal.manifest, "sha256": MANIFEST_SHA256}
    receipt = refresh_question_policy.refresh_observed_policy(
        release_index=lane.renewal.index,
        observation=lane.observed,
        grant=renewal_grant(),
        clients=clients,
    )
    assert receipt["error"] == "ledger_write_response_lost", receipt
    assert receipt["ledger_moved"] is True
    assert receipt["state"] == refresh_question_policy.LEDGER_MOVED_STATE
    assert (
        receipt["activation"]["active_deployment_digest"]
        == (receipt["binding"]["deployment_digest"])
    )
    stored, generation = lane.renewal.scenario.ledger_now()
    assert (
        stored["active_deployment_digest"] == (receipt["binding"]["deployment_digest"])
    )
    assert receipt["activation"]["generation"] == generation


def test_a_genuinely_refused_ledger_precondition_is_still_stale_generation(
    tmp_path, capsys
):
    lane = Lane(tmp_path)
    clients = recording_clients(lane.state_path)
    inner = clients["objects"]
    state = clients["_state"]

    class RacesTheLedger:
        def read(self, name, *, generation=None):
            return inner.read(name, generation=generation)

        def create(self, name, raw, *, if_generation_match):
            if name == LEDGER_NAME:
                stored = state.data["objects"][LEDGER_NAME]
                state.data["objects"][LEDGER_NAME] = {
                    "generation": "9001",
                    "raw_b64": stored["raw_b64"],
                }
                state.save()
                raise fake.PreconditionFailed("generation_mismatch")
            return inner.create(name, raw, if_generation_match=if_generation_match)

    clients["objects"] = RacesTheLedger()
    clients["manifest"] = {"value": lane.renewal.manifest, "sha256": MANIFEST_SHA256}
    receipt = refresh_question_policy.refresh_observed_policy(
        release_index=lane.renewal.index,
        observation=lane.observed,
        grant=renewal_grant(),
        clients=clients,
    )
    assert receipt["error"] == "stale_ledger_generation", receipt
    assert receipt["ledger_moved"] is False


def test_the_native_byte_reader_shape_is_driven_and_its_answer_is_not_hidden(
    tmp_path, capsys
):
    """Drive the real native reader class, not the fixture's lookup table.

    The fixture answers ``source:`` out of a preloaded table, which is why a
    green suite said nothing about the native path returning None. This drives
    ``release_native_adapter._Bytes`` itself, built the way ``build`` builds
    it, over an adapter whose transport stands in for the pricing host. While
    the host serves nothing, observe must refuse rather than proceed; once it
    serves the page, the same class resolves ``source:`` by an anonymous GET
    and observe must succeed. Either way the answer is asserted and cannot
    pass unnoticed, and the owner credential never travels to the host.
    """
    lane = Lane(tmp_path)
    page = PRICING_PAGE
    name = refresh_question_policy.EVIDENCE_PREFIX + sha(page) + ".html"

    class PricingHost:
        """The transport behind the adapter: 404 until the page is served."""

        def __init__(self):
            self.page = None
            self.requests = []

        def __call__(self, request):
            self.requests.append(request)
            if self.page is None:
                return {"status": 404, "headers": {}, "body": b""}
            return {"status": 200, "headers": {}, "body": self.page}

    class Objects:
        """The object client the native byte reader is built on."""

        def __init__(self):
            self.bucket = release.BUCKET
            self.store = {}

        def _read_from(self, bucket, object_name, generation=None):
            assert bucket == self.bucket
            stored = self.store.get(object_name)
            if stored is None or (
                generation is not None and str(generation) != stored["generation"]
            ):
                return None
            return stored

        def read(self, object_name, *, generation=None):
            return self._read_from(self.bucket, object_name, generation)

        def create(self, object_name, raw, *, if_generation_match):
            assert int(if_generation_match) == 0 and object_name not in self.store
            self.store[object_name] = {"generation": "11", "raw": bytes(raw)}
            return {"generation": "11"}

    objects = Objects()
    host = PricingHost()
    adapter = native.NativeAdapter("owner-gcloud", transport=host)
    reader = native._Bytes(adapter, objects)
    # The native reader as it stands in this tree, while the host serves nothing.
    assert reader.read("source:" + PRICING_SOURCE) is None
    clients = fake.clients(lane.state_path)
    clients["objects"] = objects
    clients["bytes"] = reader
    code, summary = run_main(
        observe_argv(
            lane, tmp_path / "native.json", receipt=tmp_path / "native.receipt"
        ),
        capsys,
        clients=clients,
    )
    assert (code, summary["error"]) == (1, "observation_source_unreadable"), summary
    assert objects.store == {}

    # The host now serves the page and the same class resolves it natively.
    host.page = page
    clients = fake.clients(lane.state_path)
    clients["objects"] = objects
    clients["bytes"] = reader
    code, summary = run_main(
        observe_argv(
            lane, tmp_path / "native2.json", receipt=tmp_path / "native2.receipt"
        ),
        capsys,
        clients=clients,
    )
    assert (code, summary["state"]) == (0, "observed"), summary
    assert summary["source_object_name"] == name
    assert objects.store[name]["raw"] == page
    document = json.loads((tmp_path / "native2.json").read_text(encoding="utf-8"))
    assert document["source_object"].endswith("#11")
    # And the renewal reads those bytes back through the same native reader.
    assert reader.read(document["source_object"]) == page
    # Every request the host saw was the anonymous GET for the pricing page.
    assert host.requests, "the native reader never asked the pricing host"
    assert {request["url"] for request in host.requests} == {PRICING_SOURCE}
    assert all("Authorization" not in request["headers"] for request in host.requests)


def test_an_unknown_model_is_refused(tmp_path, capsys):
    lane = Lane(tmp_path)
    argv = observe_argv(lane, tmp_path / "doc.json", receipt=tmp_path / "doc.receipt")
    argv[argv.index("--model") + 1] = "gemini-9.9-imaginary"
    before = lane.calls()
    code, summary = run_main(argv, capsys, clients=recording_clients(lane.state_path))
    assert (code, summary["error"]) == (1, "observation_model_unknown"), summary
    assert lane.calls() == before
    assert not (tmp_path / "doc.json").exists()
    other = observation(
        observed_at=NOW - timedelta(hours=1), model="gemini-9.9-imaginary"
    )
    write_json(lane.observation_path, other)
    code, summary = run_main(lane.renew_argv(), capsys)
    assert (code, summary["error"]) == (1, "observation_model_unknown"), summary


def _rebound(binding, **changes):
    """A binding with changed fields whose own digest is recomputed to match."""
    value = {**binding, **changes}
    del value["deployment_digest"]
    value["deployment_digest"] = release.canonical_digest(value)
    return value


def _forge_active_binding(lane, receipt, *, recompute):
    """Rewrite the stored renewal binding to name the old, still served revision."""
    key = receipt["binding"]["deployment_digest"]
    old_revision = lane.renewal.scenario.binding["revision_name"]
    if recompute:
        forged = _rebound(receipt["binding"], revision_name=old_revision)
        assert forged["deployment_digest"] != key
    else:
        forged = {**receipt["binding"], "revision_name": old_revision}
    state = lane.state()
    name = release.OBJECT_PREFIX + f"deployments/{key}/binding.json"
    state["objects"][name]["raw_b64"] = fake.encode_raw(release.canonical_bytes(forged))
    for entry in state["service"]["traffic"] + state["service"]["trafficStatuses"]:
        entry["revision"] = old_revision
    write_json(lane.state_path, state)
    return key


def test_an_active_binding_that_does_not_recompute_to_its_ledger_key_is_refused(
    tmp_path, capsys
):
    """The key the ledger holds is the digest the stored binding must recompute to.

    A binding rewritten to name the old served revision, its own digest
    recomputed so it is self consistent, still sits under the key of the
    renewal deployment. Judged by its own content it looks served, so the run
    must refuse it by name rather than trust it or fall over on it.
    """
    lane = Lane(tmp_path)
    receipt = _activated(lane, capsys)
    _forge_active_binding(lane, receipt, recompute=True)
    before = lane.calls()

    code, again = run_main(lane.renew_argv(output=tmp_path / "again.json"), capsys)

    assert (code, again["state"], again["error"]) == (
        1,
        "refused",
        "active_binding_unavailable",
    ), again
    assert lane.calls() == before


@pytest.mark.parametrize("recompute", [True, False])
def test_a_bound_binding_is_validated_before_it_can_be_already_activated(
    tmp_path, capsys, recompute
):
    """The existing activation check validates exactly as the acting path does.

    Judged by its own revision name the forged binding is served in full and
    the ledger names its key active, so a check that read it without
    validating it and recomputing its digest would say already_activated.
    """
    lane = Lane(tmp_path)
    receipt = _activated(lane, capsys)
    key = _forge_active_binding(lane, receipt, recompute=recompute)
    clients = recording_clients(lane.state_path)
    ledger = {
        "bindings": {key: receipt["policy"]["policy_digest"]},
        "active_deployment_digest": key,
    }

    with pytest.raises(refresh_question_policy.Refused) as raised:
        refresh_question_policy._existing_activation(clients, ledger, receipt["policy"])
    assert str(raised.value) == "renewal_binding_invalid"


@pytest.mark.parametrize("newer_first", [False, True])
def test_several_bindings_report_the_worst_state_with_its_own_remedy(
    tmp_path, capsys, newer_first
):
    """One claimed but unserved binding among several is a split, not inert."""
    lane = Lane(tmp_path)
    receipt = _activated(lane, capsys)
    clients = recording_clients(lane.state_path)
    newer = receipt["binding"]
    older = _rebound(newer, revision_name="listening-post-staging-retired-renewal")
    clients["objects"].create(
        release.OBJECT_PREFIX
        + f"deployments/{older['deployment_digest']}/binding.json",
        release.canonical_bytes(older),
        if_generation_match=0,
    )
    bindings = [newer, older] if newer_first else [older, newer]
    ledger = {
        "bindings": {
            item["deployment_digest"]: receipt["policy"]["policy_digest"]
            for item in bindings
        },
        "active_deployment_digest": newer["deployment_digest"],
    }
    clients["_state"].data["service"]["traffic"] = []
    clients["_state"].data["service"]["trafficStatuses"] = []

    result = refresh_question_policy._existing_activation(
        clients, ledger, receipt["policy"]
    )

    assert result["state"] == refresh_question_policy.SPLIT_STATE, result
    assert result["error"] == "ledger_active_revision_not_served"
    assert result["binding"]["deployment_digest"] == newer["deployment_digest"]
    assert newer["revision_name"] in result["split"]["remedy"]
    assert "route that revision natively" in result["split"]["remedy"]
    assert {
        item["renewal_deployment_digest"]: (
            item["ledger_claims_active"],
            item["serving"],
        )
        for item in result["split"]["bound_deployments"]
    } == {
        older["deployment_digest"]: (False, False),
        newer["deployment_digest"]: (True, False),
    }
