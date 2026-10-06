import json
from types import SimpleNamespace

import pytest

from core.eval.friday_live import FridayLiveRunner
from core.eval.friday_live_native import (
    AGENT_URI,
    BUILDER_EMAIL,
    MAX_BYTES_BILLED,
    PROJECT,
    FridayLiveNativeRefused,
    NativeAskClaimTally,
    build_builder_agent_transport,
    build_native_ask_claim_tally,
)


class FakeIdentity:
    def __init__(self):
        self.refreshes = 0
        self.token = None

    def refresh(self, request):
        self.refreshes += 1
        self.token = f"fixture-token-{self.refreshes}"


class FakeSession:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeJobConfig:
    pass


class FakeBigQuery:
    QueryJobConfig = FakeJobConfig

    @staticmethod
    def ScalarQueryParameter(name, kind, value):
        return SimpleNamespace(name=name, type_=kind, value=value)


class FakeResult:
    def __init__(self, rows, total_rows=None):
        self.rows = rows
        self.total_rows = len(rows) if total_rows is None else total_rows

    def __iter__(self):
        return iter(self.rows)


class FakeJob:
    def __init__(self, rows, *, job_id):
        self._rows = rows
        self.job_id = job_id
        self.total_bytes_processed = 1234
        self.result_calls = []

    def result(self, **kwargs):
        self.result_calls.append(kwargs)
        return FakeResult(self._rows)


class FakeClient:
    def __init__(self, *row_sets):
        self.row_sets = list(row_sets)
        self.calls = []

    def query(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        rows = self.row_sets.pop(0)
        return FakeJob(rows, job_id=f"job-{len(self.calls)}")


def record():
    return {
        "ask_id": "a_20261002_1234abcd",
        "run": {"run_id": "r_native_1"},
        "answer": {"claims": [{"id": "c1"}]},
    }


def native_rows(ask_record=None):
    raw = json.dumps(ask_record or record(), ensure_ascii=False, separators=(",", ":"))
    return [{"run_id": "r_native_1", "stage": "ask", "record": raw}]


def claim_rows():
    return [
        {"answer_or_brief_id": "a_20261002_1234abcd", "claim_id": "c1", "rule": "K4",
         "verdict": "cut", "checker": "code", "reason": "earlier cut", "run_id": "r_native_1"},
        {"answer_or_brief_id": "a_20261002_1234abcd", "claim_id": "c1", "rule": "K4",
         "verdict": "pass", "checker": "code", "reason": "later pass", "run_id": "r_native_1"},
        {"answer_or_brief_id": "a_20261002_1234abcd", "claim_id": "c1", "rule": "K10",
         "verdict": "pass", "checker": "code", "reason": "final pass", "run_id": "r_native_1"},
    ]


def tally(client):
    return NativeAskClaimTally(client, FakeBigQuery)(record())


def test_native_readback_preserves_raw_run_and_both_k4_verdicts_with_bounded_queries():
    raw_record = native_rows()[0]["record"]
    client = FakeClient(native_rows(), claim_rows())

    result = tally(client)

    assert result["accounted"] is True
    assert result["native_runs_record"]["record"] == raw_record
    assert result["native_claim_rows"] == claim_rows()
    assert result["final_answer_claim_ids"] == ["c1"]
    assert result["claim_check_tally"]["final_answer_claim_count"] == 1
    assert "passed" not in result["claim_check_tally"]
    k4 = [group for group in result["claim_check_tally"]["groups"] if group["rule"] == "K4"]
    assert {(group["verdict"], group["checker"], group["rows"]) for group in k4} == {
        ("cut", "code", 1), ("pass", "code", 1),
    }
    assert [call[1]["job_config"].maximum_bytes_billed for call in client.calls] == [MAX_BYTES_BILLED] * 2
    assert [[(p.name, p.type_, p.value) for p in call[1]["job_config"].query_parameters]
            for call in client.calls] == [
        [("run_id", "STRING", "r_native_1")],
        [("run_id", "STRING", "r_native_1"), ("ask_id", "STRING", "a_20261002_1234abcd")],
    ]
    assert [receipt["job_id"] for receipt in result["native_readback_receipts"]] == ["job-1", "job-2"]
    assert all(len(receipt["sql_sha256"]) == 64 for receipt in result["native_readback_receipts"])


@pytest.mark.parametrize("row_change", [
    {"run_id": "other_run"},
    {"stage": "brief"},
])
def test_wrong_run_or_stage_is_unaccounted(row_change):
    run = {**native_rows()[0], **row_change}
    result = NativeAskClaimTally(FakeClient([run], claim_rows()), FakeBigQuery)(record())

    assert result["accounted"] is False
    assert len(result["native_readback_receipts"]) == 1


def test_wrong_ask_id_in_claim_checks_is_unaccounted():
    rows = [{**claim_rows()[0], "answer_or_brief_id": "a_20261002_wrong0000"}]
    result = NativeAskClaimTally(FakeClient(native_rows(), rows), FakeBigQuery)(record())

    assert result["accounted"] is False


def test_byte_different_duplicate_run_records_are_refused():
    rows = native_rows()
    rows.append({**rows[0], "record": json.dumps(record(), indent=2)})
    result = NativeAskClaimTally(FakeClient(rows, claim_rows()), FakeBigQuery)(record())

    assert result["accounted"] is False


def test_byte_equivalent_duplicate_run_records_are_unambiguous():
    rows = native_rows()
    rows.append(dict(rows[0]))
    result = NativeAskClaimTally(FakeClient(rows, claim_rows()), FakeBigQuery)(record())

    assert result["accounted"] is True
    assert result["native_runs_record"]["record"] == rows[0]["record"]


def test_live_native_factory_does_not_accept_an_injected_client():
    with pytest.raises(TypeError):
        build_native_ask_claim_tally(client=FakeClient(), bigquery_module=FakeBigQuery)


def test_rejects_query_result_page_count_mismatch():
    class IncompleteJob(FakeJob):
        def result(self, **kwargs):
            return FakeResult(self._rows, total_rows=len(self._rows) + 1)

    class IncompleteClient(FakeClient):
        def query(self, sql, **kwargs):
            self.calls.append((sql, kwargs))
            return IncompleteJob(self.row_sets.pop(0), job_id="incomplete")

    result = NativeAskClaimTally(IncompleteClient(native_rows()), FakeBigQuery)(record())

    assert result["accounted"] is False
    assert result["native_readback_receipts"][0]["rows_read"] == 1


def test_builder_transport_refreshes_before_each_request_and_never_redirects():
    identity = FakeIdentity()
    identity_args = []
    session = FakeSession(SimpleNamespace(status_code=202), SimpleNamespace(status_code=200))
    transport = build_builder_agent_transport(
        credentials=SimpleNamespace(_target_principal=BUILDER_EMAIL),
        project=PROJECT,
        identity_verifier=lambda: {"principal": BUILDER_EMAIL, "project": PROJECT},
        identity_factory=lambda credentials, target_audience, include_email: identity_args.append(
            (credentials._target_principal, target_audience, include_email)) or identity,
        refresh_request_factory=object,
        session=session,
    )

    transport.post("/api/ask", json={"question": "test"}, timeout=4)
    transport.get("/api/ask/a_20261002_1234abcd", timeout=4)

    assert len(session.calls) == 2
    assert identity.refreshes == 2
    assert identity_args == [(BUILDER_EMAIL, AGENT_URI, True)]
    assert all(call[1].startswith(AGENT_URI + "/api/ask") for call in session.calls)
    assert all(call[2]["allow_redirects"] is False for call in session.calls)
    assert [call[2]["headers"]["Authorization"] for call in session.calls] == [
        "Bearer fixture-token-1", "Bearer fixture-token-2",
    ]


@pytest.mark.parametrize("overrides", [
    {"agent_uri": "http://f42-agent-fibxg5ynpq-uc.a.run.app"},
    {"project": "other-project"},
    {"principal": "other@service-account.invalid"},
])
def test_transport_refuses_untrusted_origin_or_identity(overrides):
    credentials = SimpleNamespace(_target_principal=overrides.get("principal", BUILDER_EMAIL))
    kwargs = {
        "credentials": credentials,
        "project": overrides.get("project", PROJECT),
        "agent_uri": overrides.get("agent_uri", AGENT_URI),
        "identity_verifier": lambda: {"principal": BUILDER_EMAIL, "project": PROJECT},
        "identity_factory": lambda *args, **kwargs: FakeIdentity(),
        "refresh_request_factory": object,
        "session": FakeSession(),
    }

    with pytest.raises(FridayLiveNativeRefused):
        build_builder_agent_transport(**kwargs)


def test_post_error_is_sanitized_sent_once_and_never_written_to_receipts(tmp_path):
    identity = FakeIdentity()
    session = FakeSession(TimeoutError("fixture-token-1 must not be echoed"))
    transport = build_builder_agent_transport(
        credentials=SimpleNamespace(_target_principal=BUILDER_EMAIL),
        project=PROJECT,
        identity_verifier=lambda: {"principal": BUILDER_EMAIL, "project": PROJECT},
        identity_factory=lambda *args, **kwargs: identity,
        refresh_request_factory=object,
        session=session,
    )

    runner = FridayLiveRunner(
        namespace_dir=tmp_path / "friday-fresh",
        namespace_id="friday-fresh",
        transport=transport,
        budget_admission=lambda **kwargs: {"admitted": True, "known_ceiling_usd": "0.10"},
        claim_tally=None,
        total_budget_usd="1.00",
        request_timeout_seconds=4,
        terminal_deadline_seconds=0,
        poll_interval_seconds=0,
    )

    result = runner.run_market("ZA")

    assert len(session.calls) == 1
    assert result["status"] == "uncertain"
    assert b"fixture-token" not in b"".join(path.read_bytes() for path in tmp_path.rglob("*.json"))
