from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_bytes

from tests.unit import test_dynamic_quality_review as review_fixtures

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/staging/register_open_intelligence_quality_review_receipt.py"


def _load():
    assert SCRIPT.is_file(), "approved quality-review registration CLI is missing"
    spec = importlib.util.spec_from_file_location("quality_review_registration", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _receipt_bytes() -> bytes:
    packet = review_fixtures.live_quality.build_review_packet(
        review_fixtures.RUN_ID, review_fixtures._batch()
    )
    return canonical_bytes(review_fixtures._receipt(packet))


def test_cli_requires_the_exact_review_or_apply_grammar(tmp_path):
    cli = _load()
    receipt = tmp_path / "review.json"
    receipt.write_bytes(_receipt_bytes())
    digest = __import__("hashlib").sha256(receipt.read_bytes()).hexdigest()
    assert cli._parse_cli(
        ["review", "--receipt-file", str(receipt.resolve()), "--receipt-sha256", digest]
    ) == ("review", receipt.resolve(), digest)
    for argv in (
        [],
        ["review"],
        ["other", "--receipt-file", str(receipt.resolve()), "--receipt-sha256", digest],
        ["review", "--receipt-sha256", digest, "--receipt-file", str(receipt.resolve())],
        ["review", "--receipt-file", "relative.json", "--receipt-sha256", digest],
        ["review", "--receipt-file", str(receipt.resolve()), "--receipt-sha256", "A" * 64],
    ):
        with pytest.raises(cli.RegistrationRefusal):
            cli._parse_cli(argv)


def test_review_is_local_read_only_and_emits_exact_fields(tmp_path, monkeypatch, capsys):
    cli = _load()
    receipt = tmp_path / "review.json"
    raw = _receipt_bytes()
    receipt.write_bytes(raw)
    digest = __import__("hashlib").sha256(raw).hexdigest()
    monkeypatch.setattr(
        cli.bigquery,
        "Client",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("review created a cloud client")),
    )
    assert (
        cli.main(["review", "--receipt-file", str(receipt.resolve()), "--receipt-sha256", digest])
        == 0
    )
    output = capsys.readouterr()
    assert output.err == ""
    payload = json.loads(output.out)
    assert tuple(sorted(payload)) == (
        "artifact_sha256",
        "candidate_projection_digest",
        "contract_version",
        "packet_digest",
        "review_receipt_digest",
        "run_id",
        "source_window_digest",
    )
    assert payload["artifact_sha256"] == digest


def test_apply_calls_only_the_fixed_registration_routine(tmp_path, monkeypatch, capsys):
    cli = _load()
    receipt = tmp_path / "review.json"
    raw = _receipt_bytes()
    receipt.write_bytes(raw)
    digest = __import__("hashlib").sha256(raw).hexdigest()
    parsed = json.loads(raw)
    recorded_at = datetime(2026, 8, 31, 12, tzinfo=UTC)
    row = {
        "review_store_contract_version": "open_intelligence_quality_review_store_v1",
        "run_id": parsed["run_id"],
        "canonical_review_receipt_json": raw.decode(),
        "review_receipt_digest": parsed["receipt_digest"],
        "artifact_sha256": digest,
        "source_window_digest": parsed["source_window_digest"],
        "candidate_projection_digest": parsed["candidate_projection_digest"],
        "packet_digest": parsed["packet_digest"],
        "registered_by": cli.APPROVED_BY,
        "registered_at": recorded_at,
    }
    events = []

    class Job:
        def result(self, **kwargs):
            events.append(("result", kwargs))
            return (row,)

    class Client:
        def query(self, sql, **kwargs):
            events.append(("query", sql, kwargs))
            return Job()

    monkeypatch.setattr(
        cli.bigquery, "Client", lambda **kwargs: events.append(("client", kwargs)) or Client()
    )
    assert (
        cli.main(["apply", "--receipt-file", str(receipt.resolve()), "--receipt-sha256", digest])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["contract_version"] == "open_intelligence_quality_review_registration_v1"
    assert payload["registered_by"] == cli.APPROVED_BY
    assert events[1][1] == (
        "CALL `ogilvy-trends-v2.trends_v2_staging."
        "sp_register_open_intelligence_quality_review_receipt_v1`"
        "(@canonical_review_receipt_json, @artifact_sha256)"
    )
    assert events[1][2]["retry"] is None
    assert events[1][2]["job_retry"] is None
    assert events[2][1]["retry"] is None
    assert events[2][1]["job_retry"] is None


def test_apply_retries_once_after_an_uncertain_registration_response(tmp_path, monkeypatch):
    cli = _load()
    receipt = tmp_path / "review.json"
    raw = _receipt_bytes()
    receipt.write_bytes(raw)
    digest = __import__("hashlib").sha256(raw).hexdigest()
    parsed = json.loads(raw)
    row = {
        "review_store_contract_version": "open_intelligence_quality_review_store_v1",
        "run_id": parsed["run_id"],
        "canonical_review_receipt_json": raw.decode(),
        "review_receipt_digest": parsed["receipt_digest"],
        "artifact_sha256": digest,
        "source_window_digest": parsed["source_window_digest"],
        "candidate_projection_digest": parsed["candidate_projection_digest"],
        "packet_digest": parsed["packet_digest"],
        "registered_by": cli.APPROVED_BY,
        "registered_at": datetime(2026, 8, 31, 12, tzinfo=UTC),
    }

    class Job:
        def __init__(self, attempt):
            self.attempt = attempt

        def result(self, **_kwargs):
            if self.attempt == 1:
                raise TimeoutError("registration response was lost")
            return (row,)

    class Client:
        def __init__(self):
            self.attempts = 0

        def query(self, _sql, **_kwargs):
            self.attempts += 1
            return Job(self.attempts)

    client = Client()
    monkeypatch.setattr(cli.bigquery, "Client", lambda **_kwargs: client)

    assert cli._apply_receipt(parsed, raw, digest) == row
    assert client.attempts == 2


def test_noncanonical_duplicate_or_digest_drift_refuses(tmp_path):
    cli = _load()
    raw = _receipt_bytes()
    cases = (
        raw + b"\n",
        raw.replace(b'"run_id":', b'"run_id":"duplicate","run_id":', 1),
        raw,
    )
    for index, content in enumerate(cases):
        path = tmp_path / f"bad-{index}.json"
        path.write_bytes(content)
        digest = __import__("hashlib").sha256(content).hexdigest()
        if index == 2:
            digest = "0" * 64
        with pytest.raises(cli.RegistrationRefusal):
            cli._load_receipt(path.resolve(), digest)


def test_executable_boundary_uses_one_canonical_error_line(capsys):
    cli = _load()
    assert cli.run_executable(["bad"]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "quality_review_receipt_invalid_arguments"}
    assert len(output.err.splitlines()) == 1
