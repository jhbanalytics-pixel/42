"""The owner's retained read preparation writes only new files and derives every value."""

from __future__ import annotations

import hashlib
import json
import socket

import pytest
from scripts.staging import prepare_retained_replay_read as prepare

from tests.unit.test_retained_replay_manifest import BUILD, retained


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture
def pinned(monkeypatch, retained):  # noqa: F811
    from src.analysis.open_intelligence import protected_context_registry

    entry = retained["entry"]
    monkeypatch.setattr(prepare, "CUTOFF", entry.cutoff_date)
    monkeypatch.setattr(
        protected_context_registry,
        "profile_for",
        lambda cutoff: entry if cutoff == entry.cutoff_date else None,
    )
    monkeypatch.setattr(prepare, "CAPTURE_SIZE", len(retained["raw"]))
    monkeypatch.setattr(prepare, "CAPTURE_SHA256", hashlib.sha256(retained["raw"]).hexdigest())
    return retained


def bq_output(*rows):
    return json.dumps(list(rows))


def test_save_result_row_writes_a_row_that_hashes_to_the_registry_pin(tmp_path, pinned, capsys):
    code = prepare.main(["save-result-row", str(tmp_path)], stdin=bq_output(pinned["row"]))
    assert code == 0
    saved = json.loads((tmp_path / "result_row.json").read_text(encoding="utf-8"))
    assert saved == pinned["row"]
    out = capsys.readouterr().out
    assert f"registry_result_digest {pinned['entry'].result_digest}" in out
    assert f"row_payload_sha256 {pinned['entry'].result_digest}" in out
    assert "result_row_matches_registry yes" in out


def test_save_result_row_never_overwrites(tmp_path, pinned):
    (tmp_path / "result_row.json").write_text("{}", encoding="utf-8")
    code = prepare.main(["save-result-row", str(tmp_path)], stdin=bq_output(pinned["row"]))
    assert code == 1
    assert (tmp_path / "result_row.json").read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "not json",
        "[]",
        "{}",
        "two",
    ],
)
def test_save_result_row_refuses_anything_but_exactly_one_row(tmp_path, pinned, stdin):
    if stdin == "two":
        stdin = bq_output(pinned["row"], pinned["row"])
    code = prepare.main(["save-result-row", str(tmp_path)], stdin=stdin)
    assert code == 1
    assert not (tmp_path / "result_row.json").exists()


def test_save_result_row_reports_zero_rows_as_not_found(tmp_path, pinned, capsys):
    code = prepare.main(["save-result-row", str(tmp_path)], stdin="[]")
    assert code == 1
    assert not (tmp_path / "result_row.json").exists()
    out = capsys.readouterr().out
    assert "result_row_not_found" in out
    assert "result_row_count_not_one" not in out


@pytest.mark.parametrize(
    ("stdin", "code"),
    [
        ("BigQuery error in query operation: Access Denied", "result_row_output_invalid"),
        ('{"error": {"code": 403, "message": "Access Denied"}}', "result_row_count_not_one"),
        ("", "result_row_output_invalid"),
    ],
)
def test_save_result_row_shows_what_it_read_when_it_refuses(tmp_path, pinned, capsys, stdin, code):
    assert prepare.main(["save-result-row", str(tmp_path)], stdin=stdin) == 1
    out = capsys.readouterr().out
    assert code in out
    assert f"input_start {stdin!r}" in out


def test_save_result_row_shows_at_most_500_characters(tmp_path, pinned, capsys):
    stdin = "x" * 400 + "y" * 400
    assert prepare.main(["save-result-row", str(tmp_path)], stdin=stdin) == 1
    out = capsys.readouterr().out
    assert f"input_start {stdin[:500]!r}" in out
    assert "y" * 101 not in out


@pytest.mark.parametrize(
    ("field", "value"),
    [("result_id", "exr_" + "0" * 64), ("result_digest", "0" * 64)],
)
def test_save_result_row_checks_the_row_id_and_digest_against_the_registry(
    tmp_path, pinned, capsys, field, value
):
    row = {**pinned["row"], field: value}
    code = prepare.main(["save-result-row", str(tmp_path)], stdin=bq_output(row))
    assert code == 1
    assert not (tmp_path / "result_row.json").exists()
    assert "result_row_matches_registry no" in capsys.readouterr().out


def test_find_capture_searches_the_root_it_is_given(tmp_path, pinned, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    windows = tmp_path / "mnt" / "c" / "Users" / "owner" / "Documents"
    windows.mkdir(parents=True)
    (windows / prepare.CAPTURE_NAME).write_bytes(pinned["raw"])
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(prepare.Path, "home", classmethod(lambda cls: home))
    assert prepare.main(["find-capture", str(work)]) == 1
    assert not (work / "capture.json").exists()
    root = tmp_path / "mnt" / "c" / "Users"
    assert prepare.main(["find-capture", str(work), "--root", str(root)]) == 0
    assert (work / "capture.json").read_bytes() == pinned["raw"]


def test_find_capture_refuses_a_root_that_is_not_a_folder(tmp_path, pinned, capsys):
    work = tmp_path / "work"
    work.mkdir()
    code = prepare.main(["find-capture", str(work), "--root", str(tmp_path / "absent")])
    assert code == 1
    assert "search_root_missing" in capsys.readouterr().out


def test_root_applies_only_to_find_capture(tmp_path, pinned):
    code = prepare.main(
        ["save-result-row", str(tmp_path), "--root", str(tmp_path)], stdin=bq_output(pinned["row"])
    )
    assert code == 1
    assert not (tmp_path / "result_row.json").exists()


def test_save_result_row_refuses_a_row_that_is_not_the_pinned_result(tmp_path, pinned, capsys):
    row = {**pinned["row"], "canonical_result_json": '{"x":1}'}
    code = prepare.main(["save-result-row", str(tmp_path)], stdin=bq_output(row))
    assert code == 1
    assert not (tmp_path / "result_row.json").exists()
    assert "result_row_matches_registry no" in capsys.readouterr().out


def test_find_capture_copies_the_pinned_local_copy_once(tmp_path, pinned, monkeypatch):
    home = tmp_path / "home"
    keep = home / "Documents" / "private"
    keep.mkdir(parents=True)
    (keep / prepare.CAPTURE_NAME).write_bytes(pinned["raw"])
    decoy = home / "Downloads"
    decoy.mkdir()
    (decoy / prepare.CAPTURE_NAME).write_bytes(pinned["raw"] + b" ")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(prepare.Path, "home", classmethod(lambda cls: home))
    assert prepare.main(["find-capture", str(work)]) == 0
    assert (work / "capture.json").read_bytes() == pinned["raw"]
    assert prepare.main(["find-capture", str(work)]) == 1


def test_find_capture_refuses_when_no_pinned_copy_exists(tmp_path, pinned, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    (home / prepare.CAPTURE_NAME).write_bytes(b"other")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(prepare.Path, "home", classmethod(lambda cls: home))
    assert prepare.main(["find-capture", str(work)]) == 1
    assert not (work / "capture.json").exists()
    assert "no_local_copy" in capsys.readouterr().out


def write_inputs(work, retained, *, row=True):  # noqa: F811
    work.mkdir(exist_ok=True)
    (work / "capture.json").write_bytes(retained["raw"])
    if row:
        (work / "result_row.json").write_text(json.dumps(retained["row"]), encoding="utf-8")


def run_manifest(work, monkeypatch):
    from scripts.staging import run_replay_manifest

    monkeypatch.setattr(run_replay_manifest, "checkout_state", lambda: (BUILD, ""))
    monkeypatch.setattr(run_replay_manifest, "hidden_index_entries", lambda: ())
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    return run_replay_manifest.run_replay_manifest(manifest, base_dir=work, build_version=BUILD)


def test_prepare_writes_a_manifest_the_runner_verifies(tmp_path, pinned, monkeypatch, capsys):
    work = tmp_path / "work"
    write_inputs(work, pinned)
    monkeypatch.setattr(prepare, "_provider", lambda: prepare_provider())
    assert prepare.main(["prepare", str(work)]) == 0
    out = capsys.readouterr().out
    assert f"capture_sha256 {hashlib.sha256(pinned['raw']).hexdigest()}" in out
    assert "result_row present" in out
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    (bundle,) = manifest["bundles"]
    assert bundle["result_row_path"] == "result_row.json"
    assert bundle["quality_measured"] is False
    signals = json.loads((work / "signals.json").read_text(encoding="utf-8"))
    assert signals
    assert all(
        row["membership_complete"] is False
        and row["evidence_ready"] is False
        and row["geo_proven"] is False
        for row in signals
    )
    receipt = run_manifest(work, monkeypatch)
    assert receipt["refused"] == []
    assert receipt["authority"] == "authority_verified"
    assert receipt["real"] is True
    (section,) = receipt["cutoffs"]
    assert section["quality"]["measured"] is False
    assert all(
        section["quality"][name]["rate"] is None
        for name in ("receipt_completeness", "evidence_coverage", "geo_coverage")
    )


def test_prepare_without_a_row_leaves_authority_unverified(tmp_path, pinned, monkeypatch, capsys):
    work = tmp_path / "work"
    write_inputs(work, pinned, row=False)
    monkeypatch.setattr(prepare, "_provider", lambda: prepare_provider())
    assert prepare.main(["prepare", str(work)]) == 0
    assert "result_row absent" in capsys.readouterr().out
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["bundles"][0]["result_row_path"] is None
    receipt = run_manifest(work, monkeypatch)
    assert receipt["authority"] == "capture_self_consistent_authority_unverified"
    assert receipt["real"] is False


def test_prepare_never_overwrites(tmp_path, pinned, monkeypatch):
    work = tmp_path / "work"
    write_inputs(work, pinned)
    monkeypatch.setattr(prepare, "_provider", lambda: prepare_provider())
    (work / "manifest.json").write_text("{}", encoding="utf-8")
    assert prepare.main(["prepare", str(work)]) == 1
    assert (work / "manifest.json").read_text(encoding="utf-8") == "{}"
    assert not (work / "signals.json").exists()


def test_prepare_refuses_a_capture_that_is_not_the_pinned_copy(tmp_path, pinned, monkeypatch):
    work = tmp_path / "work"
    write_inputs(work, pinned)
    (work / "capture.json").write_bytes(pinned["raw"] + b" ")
    assert prepare.main(["prepare", str(work)]) == 1
    assert not (work / "manifest.json").exists()


def prepare_provider():
    from tests.unit.test_provenance_replay_v3 import Provider

    return Provider()


def bigquery_row(values):
    """A real BigQuery result row, as the client's job.result() yields it."""
    from google.cloud.bigquery.table import Row

    names = list(values)
    return Row(tuple(values[name] for name in names), {name: i for i, name in enumerate(names)})


class FakeJob:
    def __init__(self, *, total_bytes_processed=None, rows=(), total_bytes_billed=None):
        self.total_bytes_processed = total_bytes_processed
        self.total_bytes_billed = total_bytes_billed
        self.job_id = "job_fake"
        self._rows = [bigquery_row(row) for row in rows]

    def result(self):
        return iter(self._rows)


class FakeClient:
    """Answers the dry run with dry_bytes, then the run with rows, and records each call."""

    def __init__(self, rows, *, dry_bytes=4_096, error=None, error_on=None):
        self.rows = rows
        self.dry_bytes = dry_bytes
        self.error = error
        self.error_on = error_on
        self.calls = []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        dry = job_config.dry_run
        if self.error is not None and self.error_on == ("dry" if dry else "run"):
            raise self.error
        if dry:
            return FakeJob(total_bytes_processed=self.dry_bytes)
        return FakeJob(rows=self.rows, total_bytes_billed=10_485_760)


def read_row(tmp_path, client):
    return prepare.main(["read-result-row", str(tmp_path)], client=client)


def assert_no_row_values(out, rows):
    """A refusal shows counts and column names only, never the row's contents."""
    for row in rows:
        for value in row.values():
            if isinstance(value, str) and len(value) >= 8:
                assert value not in out
    assert "stored_artifact" not in out
    assert "snapshot_digest" not in out


def test_the_cap_is_64_mib():
    assert prepare.RESULT_ROW_MAXIMUM_BYTES_BILLED == 64 * 1024 * 1024


def test_an_estimate_equal_to_the_cap_is_allowed(tmp_path, pinned, capsys):
    cap = prepare.RESULT_ROW_MAXIMUM_BYTES_BILLED
    client = FakeClient([dict(pinned["row"])], dry_bytes=cap)
    assert read_row(tmp_path, client) == 0
    assert len(client.calls) == 2
    assert f"dry_run_bytes {cap}" in capsys.readouterr().out
    assert (tmp_path / "result_row.json").exists()


def test_the_fake_job_yields_real_bigquery_rows(pinned):
    from google.cloud.bigquery.table import Row

    (row,) = list(FakeJob(rows=[dict(pinned["row"])]).result())
    assert type(row) is Row
    assert not isinstance(row, dict)
    assert dict(row.items()) == pinned["row"]


def test_missing_default_credentials_refuse_cleanly(tmp_path, pinned, capsys, monkeypatch):
    from google.auth.exceptions import DefaultCredentialsError

    def unavailable():
        raise DefaultCredentialsError("Your default credentials were not found.")

    monkeypatch.setattr(prepare, "_bigquery_client", unavailable)
    assert prepare.main(["read-result-row", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "credentials_unavailable" in captured.out
    assert "Your default credentials were not found." in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert not (tmp_path / "result_row.json").exists()


@pytest.mark.parametrize("error_on", ["dry", "run"])
def test_a_credentials_refresh_failure_refuses_cleanly(tmp_path, pinned, capsys, error_on):
    from google.auth.exceptions import RefreshError

    error = RefreshError("Reauthentication is needed. Please run gcloud auth login.")
    client = FakeClient([dict(pinned["row"])], error=error, error_on=error_on)
    assert read_row(tmp_path, client) == 1
    captured = capsys.readouterr()
    assert "credentials_refresh_failed" in captured.out
    assert "Reauthentication is needed. Please run gcloud auth login." in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert not (tmp_path / "result_row.json").exists()


def test_the_overwrite_check_runs_before_the_client_is_built(tmp_path, pinned, monkeypatch):
    (tmp_path / "result_row.json").write_text("{}", encoding="utf-8")
    built = []
    monkeypatch.setattr(prepare, "_bigquery_client", lambda: built.append(1) or FakeClient([]))
    assert prepare.main(["read-result-row", str(tmp_path)]) == 1
    assert built == []
    assert (tmp_path / "result_row.json").read_text(encoding="utf-8") == "{}"


def test_read_result_row_dry_runs_uncached_under_the_cap_then_reads_by_parameter(
    tmp_path, pinned, capsys
):
    from google.cloud import bigquery

    client = FakeClient([dict(pinned["row"])])
    assert read_row(tmp_path, client) == 0
    assert [config.dry_run for _sql, config in client.calls] == [True, False]
    result_id = pinned["entry"].result_id
    for sql, config in client.calls:
        assert sql == prepare.RESULT_ROW_SQL
        assert "@result_id" in sql
        assert result_id not in sql
        assert config.use_query_cache is False
        assert config.maximum_bytes_billed == prepare.RESULT_ROW_MAXIMUM_BYTES_BILLED
        assert config.query_parameters == [
            bigquery.ScalarQueryParameter("result_id", "STRING", result_id)
        ]
    assert 0 < prepare.RESULT_ROW_MAXIMUM_BYTES_BILLED <= 1 << 30
    saved = json.loads((tmp_path / "result_row.json").read_text(encoding="utf-8"))
    assert saved == pinned["row"]
    out = capsys.readouterr().out
    assert "dry_run_bytes 4096" in out
    assert "result_row_matches_registry yes" in out


def test_read_result_row_names_every_ledger_column_and_formats_the_time():
    sql = prepare.RESULT_ROW_SQL
    for field in prepare.RESULT_ROW_FIELDS - {"completed_at"}:
        assert field in sql
    assert 'FORMAT_TIMESTAMP("%Y-%m-%dT%H:%M:%E6SZ", completed_at, "UTC") AS completed_at' in sql
    assert (
        "`ogilvy-trends-v2.trends_v2_staging_approvals.open_intelligence_execution_results_v1`"
        in sql
    )
    assert "WHERE result_id = @result_id" in sql


def test_read_result_row_refuses_above_the_cap_before_the_real_run(tmp_path, pinned, capsys):
    over = prepare.RESULT_ROW_MAXIMUM_BYTES_BILLED + 1
    client = FakeClient([dict(pinned["row"])], dry_bytes=over)
    assert read_row(tmp_path, client) == 1
    assert len(client.calls) == 1
    assert not (tmp_path / "result_row.json").exists()
    out = capsys.readouterr().out
    assert f"dry_run_bytes {over}" in out
    assert "result_row_over_cap" in out


@pytest.mark.parametrize("dry_bytes", [None, -1, "10"])
def test_read_result_row_refuses_a_dry_run_without_a_byte_estimate(tmp_path, pinned, dry_bytes):
    client = FakeClient([dict(pinned["row"])], dry_bytes=dry_bytes)
    assert read_row(tmp_path, client) == 1
    assert len(client.calls) == 1
    assert not (tmp_path / "result_row.json").exists()


@pytest.mark.parametrize("error_on", ["dry", "run"])
def test_read_result_row_prints_the_bigquery_error(tmp_path, pinned, capsys, error_on):
    from google.api_core import exceptions

    error = exceptions.Forbidden("Access Denied: Table results_v1: permission denied")
    client = FakeClient([dict(pinned["row"])], error=error, error_on=error_on)
    assert read_row(tmp_path, client) == 1
    assert not (tmp_path / "result_row.json").exists()
    out = capsys.readouterr().out
    assert "bigquery_error" in out
    assert "Access Denied: Table results_v1: permission denied" in out
    assert "result_row_query_failed" in out


def test_read_result_row_reports_zero_rows_as_not_found(tmp_path, pinned, capsys):
    assert read_row(tmp_path, FakeClient([])) == 1
    assert not (tmp_path / "result_row.json").exists()
    out = capsys.readouterr().out
    assert "result_row_not_found" in out
    assert "rows_returned 0" in out
    assert_no_row_values(out, [pinned["row"]])


def test_read_result_row_refuses_more_than_one_row(tmp_path, pinned, capsys):
    rows = [dict(pinned["row"]), dict(pinned["row"])]
    assert read_row(tmp_path, FakeClient(rows)) == 1
    assert not (tmp_path / "result_row.json").exists()
    out = capsys.readouterr().out
    assert "result_row_count_not_one" in out
    assert "rows_returned 2" in out
    assert_no_row_values(out, rows)


@pytest.mark.parametrize("change", ["missing", "extra", "not_string"])
def test_read_result_row_refuses_other_columns(tmp_path, pinned, capsys, change):
    row = dict(pinned["row"])
    if change == "missing":
        row.pop("approval_id")
    elif change == "extra":
        row["extra"] = "x"
    else:
        row["status"] = 1
    assert read_row(tmp_path, FakeClient([row])) == 1
    assert not (tmp_path / "result_row.json").exists()
    out = capsys.readouterr().out
    assert "result_row_fields_invalid" in out
    assert "rows_returned 1" in out
    assert_no_row_values(out, [row])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("result_id", "exr_" + "0" * 64),
        ("result_digest", "0" * 64),
        ("canonical_result_json", '{"x":1}'),
    ],
)
def test_read_result_row_refuses_a_row_that_is_not_the_pinned_result(
    tmp_path, pinned, capsys, field, value
):
    row = {**pinned["row"], field: value}
    assert read_row(tmp_path, FakeClient([row])) == 1
    assert not (tmp_path / "result_row.json").exists()
    out = capsys.readouterr().out
    assert "result_row_matches_registry no" in out
    assert "result_row_not_the_pinned_result" in out


def test_read_result_row_never_overwrites_and_queries_nothing(tmp_path, pinned):
    (tmp_path / "result_row.json").write_text("{}", encoding="utf-8")
    client = FakeClient([dict(pinned["row"])])
    assert read_row(tmp_path, client) == 1
    assert client.calls == []
    assert (tmp_path / "result_row.json").read_text(encoding="utf-8") == "{}"


def test_a_row_read_by_the_client_lets_the_runner_verify(tmp_path, pinned, monkeypatch):
    work = tmp_path / "work"
    write_inputs(work, pinned, row=False)
    assert read_row(work, FakeClient([dict(pinned["row"])])) == 0
    monkeypatch.setattr(prepare, "_provider", lambda: prepare_provider())
    assert prepare.main(["prepare", str(work)]) == 0
    receipt = run_manifest(work, monkeypatch)
    assert receipt["authority"] == "authority_verified"
    assert receipt["real"] is True


def test_the_client_is_only_built_for_read_result_row(tmp_path, pinned, monkeypatch):
    built = []
    monkeypatch.setattr(prepare, "_bigquery_client", lambda: built.append(1) or FakeClient([]))
    assert prepare.main(["find-capture", str(tmp_path), "--root", str(tmp_path)]) == 1
    assert built == []
    assert prepare.main(["read-result-row", str(tmp_path)]) == 1
    assert built == [1]


def test_the_default_client_targets_the_project_of_the_ledger(monkeypatch):
    from google.cloud import bigquery

    built = []
    monkeypatch.setattr(bigquery, "Client", lambda **kwargs: built.append(kwargs) or "client")
    assert prepare._bigquery_client() == "client"
    assert built == [{"project": "ogilvy-trends-v2"}]
