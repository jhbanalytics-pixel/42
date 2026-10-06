import hashlib
import json
from datetime import UTC, date, datetime

import pytest
from scripts.staging import collect_42_sources as collection
from scripts.staging import read_ingest_run as tool
from src.analysis.open_intelligence.ingest_receipts import receipt_row

EXECUTION = "intelligence-42-ingest-staging-a1b2c"
CUTOFF = date(2026, 9, 24)
RUN_ID = "run-20260925-ingest"
CAP = 50_000_000


def collection_receipt(**overrides):
    values = {
        "run_id": RUN_ID,
        "execution_id": EXECUTION,
        "source_sha": "7bab3970cd75af4d0f686c339cabe9ecfdd3aad4",
        "image_uri": "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine@sha256:"
        + "a" * 64,
        "policy_sha256": tool.EXPECTED_POLICY_SHA256,
        "profile_sha256": tool.EXPECTED_PROFILE_SHA256,
        "cutoff": CUTOFF.isoformat(),
        "market_states": {"za": "collected", "ng": "collected", "ke": "collected"},
        "raw_count": 5,
        "enriched_count": 4,
        "authority_kind": collection.VERIFIED_AUTHORITY,
    }
    values.update(overrides)
    return collection.collection_receipt(**values)


def stored(receipt=None, *, trend_date="2026-09-25", **changes):
    row = receipt_row(collection_receipt() if receipt is None else receipt, trend_date=trend_date)
    row["recorded_at"] = datetime(2026, 9, 25, 4, 30, tzinfo=UTC)
    row.update(changes)
    return row


def raw_rows(run_id=RUN_ID):
    at = datetime(2026, 9, 25, 4, 10, tzinfo=UTC)
    later = datetime(2026, 9, 25, 4, 20, tzinfo=UTC)
    return [
        {"market": "za", "source": "reddit", "pipeline_run_id": run_id, "collected_at": at},
        {"market": "za", "source": "reddit", "pipeline_run_id": run_id, "collected_at": later},
        {"market": "za", "source": "youtube", "pipeline_run_id": run_id, "collected_at": at},
        {"market": "ng", "source": "gdelt", "pipeline_run_id": run_id, "collected_at": at},
        {"market": "ke", "source": "gdelt", "pipeline_run_id": run_id, "collected_at": later},
        {"market": "ke", "source": "gdelt", "pipeline_run_id": "another-run", "collected_at": at},
    ]


class FakeJob:
    def __init__(self, rows, processed, billed):
        self.rows = rows
        self.total_bytes_processed = processed
        self.total_bytes_billed = billed
        self.job_id = f"job-{id(self)}"

    def result(self, timeout=None):
        return list(self.rows)


class FakeClient:
    """Answers the two statements over in memory rows, recording every job it is sent."""

    def __init__(self, receipts=None, raw=None, dry_bytes=None):
        self.receipts = [stored()] if receipts is None else receipts
        self.raw = raw_rows() if raw is None else raw
        self.dry_bytes = dry_bytes or {}
        self.calls = []

    def query(self, sql, job_config=None, **kwargs):
        parameters = {p.name: p.value for p in job_config.query_parameters}
        table = "receipts" if tool.RECEIPT_TABLE_ID in sql else "raw"
        self.calls.append(
            {
                "table": table,
                "dry_run": job_config.dry_run,
                "cap": job_config.maximum_bytes_billed,
                "parameters": parameters,
                "sql": sql,
            }
        )
        planned = self.dry_bytes.get(table, 1000)
        if job_config.dry_run:
            return FakeJob([], planned, None)
        if table == "receipts":
            rows = [
                {
                    **row,
                    "computed_sha256": hashlib.sha256(
                        row["receipt_json"].encode("utf-8")
                    ).hexdigest(),
                }
                for row in self.receipts
                if row["execution_id"] == parameters["execution_id"]
            ]
            return FakeJob(rows, planned, planned)
        start, end = parameters["window_start"], parameters["window_end"]
        groups = {}
        for row in self.raw:
            if row["pipeline_run_id"] != parameters["pipeline_run_id"]:
                continue
            if not start <= row["collected_at"] < end:
                continue
            key = (row["market"], row["source"])
            count, last = groups.get(key, (0, None))
            groups[key] = (count + 1, max(filter(None, (last, row["collected_at"]))))
        rows = [
            {"market": m, "source": s, "row_count": c, "last_collected_at": t}
            for (m, s), (c, t) in sorted(groups.items())
        ]
        return FakeJob(rows, planned, planned)


def read(client=None, *, dry_run=False, cap=CAP, cutoff=CUTOFF, execution=EXECUTION):
    return tool.read_ingest_run(
        FakeClient() if client is None else client,
        execution_id=execution,
        cutoff=cutoff,
        maximum_bytes_billed=cap,
        dry_run=dry_run,
    )


# Expectations derived inside the engine tree


def test_expected_digests_are_the_reconciled_policy_and_entry_point_profile():
    # The job definition at the repository root is held to the same values, with the
    # table ids, by tests/repository/test_read_ingest_run_digests.py.
    assert collection.reconciled_policy_digest() == tool.EXPECTED_POLICY_SHA256
    assert collection.entry_point_profile()["profile_sha256"] == tool.EXPECTED_PROFILE_SHA256


# A clean run


def test_clean_run_verifies_and_groups_raw_rows_by_market_and_source():
    receipt = read()
    assert receipt["state"] == "verified"
    assert receipt["reasons"] == []
    row = receipt["receipt_row"]
    assert row["row_count"] == 1
    assert row["receipt_sha256"] == row["computed_sha256"]
    assert row["admitted_by_daily_stage"] is True
    assert row["receipt"]["run_id"] == RUN_ID
    assert row["recorded_at"] == "2026-09-25T04:30:00+00:00"
    raw = receipt["raw_content"]
    assert raw["pipeline_run_id"] == RUN_ID
    assert raw["groups"] == [
        {
            "market": "ke",
            "source": "gdelt",
            "row_count": 1,
            "last_collected_at": "2026-09-25T04:20:00+00:00",
        },
        {
            "market": "ng",
            "source": "gdelt",
            "row_count": 1,
            "last_collected_at": "2026-09-25T04:10:00+00:00",
        },
        {
            "market": "za",
            "source": "reddit",
            "row_count": 2,
            "last_collected_at": "2026-09-25T04:20:00+00:00",
        },
        {
            "market": "za",
            "source": "youtube",
            "row_count": 1,
            "last_collected_at": "2026-09-25T04:10:00+00:00",
        },
    ]
    assert raw["row_count_total"] == 5
    assert raw["last_collected_by_market"] == {
        "ke": "2026-09-25T04:20:00+00:00",
        "ng": "2026-09-25T04:10:00+00:00",
        "za": "2026-09-25T04:20:00+00:00",
    }
    assert raw["window"] == {
        "start": "2026-09-25T00:00:00+00:00",
        "end": "2026-09-27T00:00:00+00:00",
    }


def test_every_statement_is_dry_run_first_under_the_cap_and_values_are_parameters():
    client = FakeClient()
    receipt = read(client)
    assert [(c["table"], c["dry_run"]) for c in client.calls] == [
        ("receipts", True),
        ("receipts", False),
        ("raw", True),
        ("raw", False),
    ]
    assert {c["cap"] for c in client.calls} == {CAP}
    assert client.calls[0]["parameters"] == {"execution_id": EXECUTION}
    assert client.calls[3]["parameters"]["pipeline_run_id"] == RUN_ID
    for call in client.calls:
        assert EXECUTION not in call["sql"]
        assert RUN_ID not in call["sql"]
    assert [q["state"] for q in receipt["queries"]] == ["succeeded", "succeeded"]
    assert receipt["dry_run_bytes_total"] == 2000
    assert receipt["bytes_billed_total"] == 2000


def test_the_receipt_query_computes_the_digest_in_the_warehouse():
    client = FakeClient()
    read(client)
    assert "TO_HEX(SHA256(receipt_json))" in client.calls[0]["sql"]
    assert "WHERE execution_id = @execution_id" in client.calls[0]["sql"]


def test_raw_query_groups_by_market_and_source_with_max_collected_at_in_a_partition_window():
    client = FakeClient()
    read(client)
    sql = client.calls[2]["sql"]
    assert "pipeline_run_id = @pipeline_run_id" in sql
    assert "MAX(collected_at)" in sql
    assert "GROUP BY market, source" in sql
    assert "collected_at >= @window_start AND collected_at < @window_end" in sql


# Dry run


def test_dry_run_executes_nothing_and_plans_both_statements():
    client = FakeClient()
    receipt = read(client, dry_run=True)
    assert [c["dry_run"] for c in client.calls] == [True, True]
    assert receipt["state"] == "planned"
    assert receipt["mode"] == "dry_run"
    assert [q["state"] for q in receipt["queries"]] == ["planned", "planned"]
    assert receipt["receipt_row"] is None
    assert receipt["raw_content"] is None
    assert receipt["bytes_billed_total"] == 0


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("table", ["receipts", "raw"])
def test_a_statement_over_the_byte_cap_refuses_before_it_runs(dry_run, table):
    client = FakeClient(dry_bytes={table: CAP + 1})
    receipt = read(client, dry_run=dry_run)
    assert receipt["state"] == "refused"
    assert receipt["reasons"] == [f"over_byte_cap:{table}"]
    assert not [c for c in client.calls if not c["dry_run"] and c["table"] == table]
    assert receipt["queries"][-1]["state"] == "refused_over_cap"


def test_a_dry_run_without_a_byte_estimate_refuses():
    client = FakeClient(dry_bytes={"receipts": None})
    receipt = read(client)
    assert receipt["state"] == "refused"
    assert receipt["reasons"] == ["no_byte_estimate:receipts"]
    assert [c["dry_run"] for c in client.calls] == [True]


@pytest.mark.parametrize("cap", [0, -1, 1.5, True, None])
def test_the_cap_must_be_a_positive_integer(cap):
    with pytest.raises(tool.ReadRefused):
        read(cap=cap)


@pytest.mark.parametrize(
    "execution", ["intelligence-42-ingest-staging", "other-job-a1b2c", "x'; DROP", None]
)
def test_the_execution_id_must_name_an_ingest_execution(execution):
    with pytest.raises(tool.ReadRefused):
        read(execution=execution)


# The receipt row


def test_no_row_is_refused_and_raw_content_is_not_read():
    client = FakeClient(receipts=[])
    receipt = read(client)
    assert receipt["state"] == "refused"
    assert receipt["reasons"] == ["receipt_row_missing"]
    assert receipt["raw_content"] is None
    assert {c["table"] for c in client.calls} == {"receipts"}


def test_two_rows_for_the_execution_are_refused():
    receipt = read(FakeClient(receipts=[stored(), stored()]))
    assert receipt["state"] == "refused"
    assert "receipt_row_duplicated" in receipt["reasons"]
    assert receipt["receipt_row"]["row_count"] == 2


def test_a_stored_digest_that_is_not_the_digest_of_the_text_is_refused():
    receipt = read(FakeClient(receipts=[stored(receipt_sha256="0" * 64)]))
    assert receipt["state"] == "refused"
    assert "receipt_sha256_mismatch" in receipt["reasons"]
    assert "receipt_row_invalid" in receipt["reasons"]


def test_non_canonical_receipt_text_is_refused_even_with_a_matching_digest():
    text = json.dumps(collection_receipt(), indent=1)
    row = stored(receipt_json=text, receipt_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    receipt = read(FakeClient(receipts=[row]))
    assert receipt["state"] == "refused"
    assert receipt["reasons"][0] == "receipt_row_invalid"
    assert "receipt_sha256_mismatch" not in receipt["reasons"]


def test_an_unverified_authority_is_refused():
    unverified = collection_receipt(authority_kind=collection.UNVERIFIED_AUTHORITY)
    receipt = read(FakeClient(receipts=[stored(unverified)]))
    assert receipt["state"] == "refused"
    assert "authority_kind_not_verified" in receipt["reasons"]
    assert "receipt_not_admitted" in receipt["reasons"]


def test_a_receipt_with_no_authority_kind_is_refused():
    receipt = read(FakeClient(receipts=[stored(collection_receipt(authority_kind=None))]))
    assert "authority_kind_not_verified" in receipt["reasons"]


@pytest.mark.parametrize(
    ("field", "reason"),
    [("policy_sha256", "policy_sha256_mismatch"), ("profile_sha256", "profile_sha256_mismatch")],
)
def test_a_receipt_under_another_policy_or_profile_is_refused(field, reason):
    receipt = read(FakeClient(receipts=[stored(collection_receipt(**{field: "b" * 64}))]))
    assert receipt["state"] == "refused"
    assert reason in receipt["reasons"]
    assert "receipt_not_admitted" in receipt["reasons"]


def test_a_receipt_for_another_cutoff_is_refused():
    receipt = read(cutoff=date(2026, 9, 23))
    assert receipt["state"] == "refused"
    assert "cutoff_mismatch" in receipt["reasons"]
    assert "trend_date_mismatch" in receipt["reasons"]


def test_a_row_stored_under_the_wrong_start_day_is_refused():
    receipt = read(FakeClient(receipts=[stored(trend_date="2026-09-24")]))
    assert receipt["state"] == "refused"
    assert "trend_date_mismatch" in receipt["reasons"]
    assert "cutoff_mismatch" not in receipt["reasons"]


def test_an_incomplete_collection_is_refused():
    partial = collection_receipt(
        market_states={"za": "collected", "ng": "partial", "ke": "collected"}
    )
    receipt = read(FakeClient(receipts=[stored(partial)]))
    assert receipt["state"] == "refused"
    assert "collection_incomplete" in receipt["reasons"]


# raw_content against the receipt


def test_raw_rows_that_do_not_sum_to_the_receipt_count_are_refused():
    rows = raw_rows()
    del rows[1]
    receipt = read(FakeClient(raw=rows))
    assert receipt["state"] == "refused"
    assert receipt["reasons"] == ["raw_count_mismatch"]
    assert receipt["raw_content"]["row_count_total"] == 4


def test_a_collected_market_with_no_raw_rows_is_refused():
    rows = [row for row in raw_rows() if row["market"] != "ke"]
    rows.append(dict(rows[0]))
    receipt = read(FakeClient(raw=rows))
    assert receipt["state"] == "refused"
    assert receipt["reasons"] == ["raw_market_missing:ke"]


def test_no_raw_rows_for_the_run_is_refused():
    receipt = read(FakeClient(raw=raw_rows("some-other-run")))
    assert receipt["state"] == "refused"
    assert receipt["reasons"][0] == "raw_rows_absent"


def test_raw_rows_outside_the_window_are_not_counted():
    rows = raw_rows()
    rows[0] = {**rows[0], "collected_at": datetime(2026, 9, 27, 0, 0, tzinfo=UTC)}
    receipt = read(FakeClient(raw=rows))
    assert receipt["reasons"] == ["raw_count_mismatch"]


# The written receipt


def test_the_receipt_is_written_once(tmp_path):
    path = tmp_path / "ingest-readback.json"
    receipt = read()
    tool.write_receipt(receipt, path)
    assert json.loads(path.read_text()) == receipt
    with pytest.raises(FileExistsError):
        tool.write_receipt(receipt, path)


def test_main_refuses_an_existing_path_before_any_query(tmp_path):
    path = tmp_path / "exists.json"
    path.write_text("{}")
    client = FakeClient()
    with pytest.raises(SystemExit):
        tool.main(
            ["--execution-id", EXECUTION, "--cutoff", "2026-09-24", "--receipt", str(path)],
            client_factory=lambda: client,
        )
    assert client.calls == []
    assert path.read_text() == "{}"


def test_main_writes_the_receipt_and_exits_by_state(tmp_path, capsys):
    ok = tmp_path / "ok.json"
    args = ["--execution-id", EXECUTION, "--cutoff", "2026-09-24"]
    assert tool.main([*args, "--receipt", str(ok)], client_factory=FakeClient) == 0
    assert json.loads(ok.read_text())["state"] == "verified"
    planned = tmp_path / "planned.json"
    assert (
        tool.main([*args, "--receipt", str(planned), "--dry-run"], client_factory=FakeClient) == 0
    )
    assert json.loads(planned.read_text())["state"] == "planned"
    bad = tmp_path / "bad.json"
    assert (
        tool.main([*args, "--receipt", str(bad)], client_factory=lambda: FakeClient(receipts=[]))
        == 1
    )
    assert json.loads(bad.read_text())["state"] == "refused"
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["state"] == "refused"


def test_main_refuses_a_service_account_identity(tmp_path):
    class ServiceAccountClient(FakeClient):
        class _credentials:
            service_account_email = (
                "intelligence-42-ingest@ogilvy-trends-v2.iam.gserviceaccount.com"
            )

    client = ServiceAccountClient()
    client._credentials = ServiceAccountClient._credentials()
    path = tmp_path / "sa.json"
    with pytest.raises(SystemExit):
        tool.main(
            ["--execution-id", EXECUTION, "--cutoff", "2026-09-24", "--receipt", str(path)],
            client_factory=lambda: client,
        )
    assert client.calls == []
    assert not path.exists()


# The build the daily stage also binds


BUILD = {
    "source_sha": "7bab3970cd75af4d0f686c339cabe9ecfdd3aad4",
    "image_uri": "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/engine@sha256:"
    + "a" * 64,
}


def test_without_a_build_the_admission_is_named_as_digests_only():
    receipt = read()
    assert receipt["receipt_row"]["admission_scope"] == "digests_only"
    assert "source_sha" not in receipt["expected"]


def test_with_the_run_build_the_admission_binds_it_too():
    receipt = tool.read_ingest_run(
        FakeClient(), execution_id=EXECUTION, cutoff=CUTOFF, maximum_bytes_billed=CAP, build=BUILD
    )
    assert receipt["state"] == "verified"
    assert receipt["receipt_row"]["admission_scope"] == "digests_and_build"
    assert receipt["expected"]["source_sha"] == BUILD["source_sha"]


@pytest.mark.parametrize("field", ["source_sha", "image_uri"])
def test_a_receipt_from_another_build_is_refused(field):
    build = {**BUILD, field: BUILD[field][:-1] + "b"}
    receipt = tool.read_ingest_run(
        FakeClient(), execution_id=EXECUTION, cutoff=CUTOFF, maximum_bytes_billed=CAP, build=build
    )
    assert receipt["state"] == "refused"
    assert receipt["reasons"] == [f"{field}_mismatch", "receipt_not_admitted"]


@pytest.mark.parametrize("build", [{"source_sha": BUILD["source_sha"]}, {"image_uri": ""}, "x"])
def test_a_partial_or_malformed_build_is_refused_before_any_query(build):
    client = FakeClient()
    with pytest.raises(tool.ReadRefused):
        tool.read_ingest_run(
            client, execution_id=EXECUTION, cutoff=CUTOFF, maximum_bytes_billed=CAP, build=build
        )
    assert client.calls == []


def test_main_passes_the_build_through(tmp_path):
    path = tmp_path / "build.json"
    args = ["--execution-id", EXECUTION, "--cutoff", "2026-09-24", "--receipt", str(path)]
    args += ["--source-sha", BUILD["source_sha"], "--image-uri", BUILD["image_uri"]]
    assert tool.main(args, client_factory=FakeClient) == 0
    assert json.loads(path.read_text())["receipt_row"]["admission_scope"] == "digests_and_build"


# Service errors still leave a receipt


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("table", ["receipts", "raw"])
def test_a_service_error_is_recorded_as_a_refusal(dry_run, table):
    from google.api_core import exceptions

    class FailingClient(FakeClient):
        def query(self, sql, job_config=None, **kwargs):
            if (tool.RECEIPT_TABLE_ID in sql) == (table == "receipts") and (
                job_config.dry_run == dry_run
            ):
                raise exceptions.Forbidden("denied")
            return super().query(sql, job_config=job_config, **kwargs)

    receipt = read(FailingClient())
    assert receipt["state"] == "refused"
    assert receipt["reasons"] == [f"query_failed:{table}:Forbidden"]
    assert receipt["queries"][-1]["state"] == "failed"


def test_main_refuses_a_missing_receipt_folder_before_any_query(tmp_path):
    client = FakeClient()
    path = tmp_path / "absent" / "r.json"
    with pytest.raises(SystemExit):
        tool.main(
            ["--execution-id", EXECUTION, "--cutoff", "2026-09-24", "--receipt", str(path)],
            client_factory=lambda: client,
        )
    assert client.calls == []


@pytest.mark.parametrize(
    "build",
    [
        {"source_sha": "not-a-sha", "image_uri": BUILD["image_uri"]},
        {"source_sha": BUILD["source_sha"], "image_uri": "engine:latest"},
    ],
)
def test_a_build_the_daily_stage_would_refuse_is_refused_before_any_query(build):
    client = FakeClient()
    with pytest.raises(tool.ReadRefused):
        tool.read_ingest_run(
            client, execution_id=EXECUTION, cutoff=CUTOFF, maximum_bytes_billed=CAP, build=build
        )
    assert client.calls == []


@pytest.mark.parametrize(
    "extra", [["--source-sha", "not-a-sha", "--image-uri", "x"], ["--source-sha", "a" * 40]]
)
def test_main_refuses_a_bad_build_before_the_client_exists(tmp_path, extra):
    made = []
    path = tmp_path / "r.json"
    args = ["--execution-id", EXECUTION, "--cutoff", "2026-09-24", "--receipt", str(path)]
    with pytest.raises(SystemExit):
        tool.main([*args, *extra], client_factory=lambda: made.append(1) or FakeClient())
    assert made == []
    assert not path.exists()


def _failing_result_client(error, table="receipts"):
    class FailingResultClient(FakeClient):
        def query(self, sql, job_config=None, **kwargs):
            job = super().query(sql, job_config=job_config, **kwargs)
            if not job_config.dry_run and (tool.RECEIPT_TABLE_ID in sql) == (table == "receipts"):

                def result(timeout=None):
                    raise error

                job.result = result
            return job

    return FailingResultClient()


def test_an_error_raised_while_reading_results_is_recorded_with_its_dry_run(tmp_path):
    from google.api_core import exceptions

    receipt = read(_failing_result_client(exceptions.BadRequest("bytes billed limit")))
    assert receipt["state"] == "refused"
    assert receipt["reasons"] == ["query_failed:receipts:BadRequest"]
    assert receipt["queries"][-1]["state"] == "failed"
    assert receipt["queries"][-1]["dry_run_bytes"] == 1000
    path = tmp_path / "r.json"
    tool.write_receipt(receipt, path)
    assert json.loads(path.read_text())["state"] == "refused"


@pytest.mark.parametrize(
    ("error", "name"),
    [
        (TimeoutError(), "TimeoutError"),
        ("refresh", "RefreshError"),
        ("transport", "TransportError"),
    ],
)
def test_an_expired_login_or_a_timeout_is_recorded_as_a_refusal(error, name):
    from google.auth import exceptions as auth

    if error == "refresh":
        error = auth.RefreshError("expired")
    elif error == "transport":
        error = auth.TransportError("unreachable")
    receipt = read(_failing_result_client(error, table="raw"))
    assert receipt["state"] == "refused"
    assert receipt["reasons"] == [f"query_failed:raw:{name}"]
