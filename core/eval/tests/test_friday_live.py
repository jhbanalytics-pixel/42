import json
from pathlib import Path
from threading import Event, Thread

import pytest

from core.eval.friday_live import FridayLiveRefused, FridayLiveRunner


class Response:
    def __init__(self, status_code, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class Transport:
    def __init__(self, post_result, *records):
        self.post_result = post_result
        self.records = list(records)
        self.posts = []
        self.gets = []
        self.before_post = None

    def post(self, path, *, json, timeout):
        self.posts.append((path, json, timeout))
        if self.before_post:
            self.before_post()
        item = self.post_result.pop(0) if isinstance(self.post_result, list) else self.post_result
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, path, *, timeout):
        self.gets.append((path, timeout))
        item = self.records.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def admission(ceiling="0.50"):
    def approve(*, market, question, remaining_budget_usd):
        return {"admitted": True, "known_ceiling_usd": ceiling, "basis": "offline fixture"}
    return approve


def tally(record):
    claims = (record.get("answer") or {}).get("claims") or []
    ids = [claim["id"] for claim in claims]
    run = record.get("run") or {}
    return {
        "accounted": True,
        "native_runs_record": {
            "stage": "ask", "run_id": run.get("run_id") or record["ask_id"],
            "record": json.dumps(record, ensure_ascii=False),
        },
        "native_claim_rows": [{"claim_id": claim_id, "check_id": "K1", "outcome": "checked"}
                               for claim_id in ids],
        "claim_check_tally": {"rows": len(ids), "final_answer_claim_count": len(ids)},
        "final_answer_claim_ids": ids,
    }


def record(ask_id, status="complete", claims=None, model_usd="0.20", answer_status="complete", market="ZA"):
    markets = {
        "ZA": "what is trending in south africa",
        "NG": "what is trending in nigeria",
        "KE": "what is trending in kenya",
    }
    return {
        "ask_id": ask_id,
        "market": market,
        "question": markets[market],
        "status": status,
        "answer": {"status": answer_status, "claims": claims or []} if status == "complete" else None,
        "run": {"run_id": f"run-{ask_id}", "model_usd": model_usd, "tokens": {"input": 50, "output": 20}},
    }


def make_runner(tmp_path, transport, *, budget_admission=None, claim_tally=tally, total="2.00",
                resume=False, clock=None, deadline=20):
    clock = clock or Clock()
    return FridayLiveRunner(
        namespace_dir=tmp_path / "friday-20261002-fresh",
        namespace_id="friday-20261002-fresh",
        transport=transport,
        budget_admission=budget_admission or admission(),
        claim_tally=claim_tally,
        total_budget_usd=total,
        request_timeout_seconds=4,
        terminal_deadline_seconds=deadline,
        poll_interval_seconds=1,
        monotonic=(clock.monotonic if clock else None),
        sleep=(clock.sleep if clock else None),
        resume=resume,
    )


def test_posts_once_without_wait_then_gets_terminal_and_separates_usage_from_reservation(tmp_path):
    transport = Transport(
        Response(202, {"ask_id": "a_20261002_abcd", "status": "running", "url": "/api/ask/a_20261002_abcd"}),
        Response(200, record("a_20261002_abcd", status="running")),
        Response(200, record("a_20261002_abcd", claims=[{"id": "c1"}]))
    )
    runner = make_runner(tmp_path, transport, total="1.00")

    result = runner.run_market("ZA")

    assert result["status"] == "complete"
    assert len(transport.posts) == 1
    assert transport.posts[0][1] == {
        "question": "what is trending in south africa", "market": "ZA", "mode": "live", "wait": False,
    }
    assert transport.posts[0][2] == 4
    assert [path for path, _ in transport.gets] == [
        "/api/ask/a_20261002_abcd", "/api/ask/a_20261002_abcd",
    ]
    assert result["reservation"]["known_ceiling_usd"] == "0.50"
    assert result["application_reported_model_spend"] == {
        "model_usd": "0.20", "tokens": {"input": 50, "output": 20},
    }
    assert result["provider_charges"] == {"status": "unproven", "measured_usd": None}
    assert "provider_usage" not in result
    assert result["claim_tally"]["claim_check_tally"] == {"rows": 1, "final_answer_claim_count": 1}
    assert result["claim_tally_status"] == "accounted"
    assert result["candidate_to_l5"] is False
    assert Path(result["raw_receipts"]["terminal_get"]).is_file()
    assert (tmp_path / "friday-20261002-fresh" / "ZA.post.json").is_file()
    assert (tmp_path / "friday-20261002-fresh" / "report.json").is_file()
    assert json.loads((tmp_path / "friday-20261002-fresh" / "report.json").read_text(encoding="utf-8"))[
        "direct_native_writes"] == 0


@pytest.mark.parametrize("budget_result", [
    {"admitted": True, "known_ceiling_usd": None},
    {"admitted": False, "known_ceiling_usd": "0.50"},
    {"admitted": True, "known_ceiling_usd": "1.01"},
])
def test_unknown_denied_or_over_remaining_budget_fails_before_post(tmp_path, budget_result):
    calls = []

    def approve(**kwargs):
        calls.append(kwargs)
        return budget_result

    transport = Transport(Response(202, {"ask_id": "unused"}))
    runner = make_runner(tmp_path, transport, budget_admission=approve, total="1.00")

    with pytest.raises(FridayLiveRefused):
        runner.run_market("ZA")

    assert calls[0]["remaining_budget_usd"] == "1.00"
    assert transport.posts == []
    assert not (tmp_path / "friday-20261002-fresh" / "ZA.attempt.json").exists()


@pytest.mark.parametrize("post_result", [
    TimeoutError("dispatch response lost"),
    Response(202, {"status": "running"}),
])
def test_uncertain_or_missing_id_is_marked_and_never_reposted(tmp_path, post_result):
    transport = Transport(post_result)
    runner = make_runner(tmp_path, transport)

    result = runner.run_market("ZA")

    assert result["status"] == "uncertain"
    assert len(transport.posts) == 1
    with pytest.raises(FridayLiveRefused, match="already_attempted"):
        runner.run_market("ZA")
    assert len(transport.posts) == 1


def test_dispatch_marker_is_durable_before_post(tmp_path):
    state_dir = tmp_path / "friday-20261002-fresh"
    transport = Transport(
        Response(202, {"ask_id": "a_20261002_abcd", "status": "running"}),
        Response(200, record("a_20261002_abcd", claims=[])),
    )
    def inspect_marker():
        marker = state_dir / "ZA.attempt.json"
        assert marker.is_file()
        assert '"state": "dispatch_started"' in marker.read_text(encoding="utf-8")

    transport.before_post = inspect_marker
    runner = make_runner(tmp_path, transport)

    runner.run_market("ZA")

    assert len(transport.posts) == 1


def test_stale_resume_with_captured_id_uses_get_only(tmp_path):
    clock = Clock()
    transport = Transport(
        Response(202, {"ask_id": "a_20261002_abcd", "status": "running"}),
        Response(200, record("a_20261002_abcd", status="running")),
        Response(200, record("a_20261002_abcd", claims=[])),
    )
    first = make_runner(tmp_path, transport, clock=clock, deadline=0)

    result = first.run_market("ZA")

    assert result["status"] == "pending_readback"
    second = make_runner(tmp_path, transport, resume=True, clock=clock)
    resumed = second.resume_market("ZA")

    assert resumed["status"] == "complete"
    assert len(transport.posts) == 1
    assert len(transport.gets) == 2


def test_terminal_native_readback_recovers_once_from_captured_record_without_dispatch(tmp_path):
    api_record = record("a_20261002_abcd", claims=[{"id": "c1"}, {"id": "c2"}])
    transport = Transport(
        Response(202, {"ask_id": "a_20261002_abcd", "status": "running"}),
        Response(200, api_record),
    )
    tally_calls = []
    admissions = []

    def read_claims(captured):
        tally_calls.append(captured)
        if len(tally_calls) == 1:
            return {"accounted": False, "claim_check_tally": {"final_answer_claim_count": 2}}
        return tally(captured)

    def admit(*, market, question, remaining_budget_usd):
        admissions.append((market, remaining_budget_usd))
        return {"admitted": True, "known_ceiling_usd": "0.50"}

    first = make_runner(
        tmp_path, transport, claim_tally=read_claims, budget_admission=admit, total="1.00")

    initial = first.run_market("ZA")
    post_receipt = Path(initial["raw_receipts"]["post"]).read_bytes()
    get_receipt = Path(initial["raw_receipts"]["terminal_get"]).read_bytes()
    reserved = initial["reservation"]
    captured = initial["terminal_readback"]
    second = make_runner(
        tmp_path, transport, claim_tally=read_claims, budget_admission=admit,
        total="1.00", resume=True)

    recovered = second.resume_market("ZA")

    assert initial["fully_accounted"] is False
    assert recovered["fully_accounted"] is True
    assert recovered["terminal_readback"] == captured
    assert recovered["claim_tally"]["final_answer_claim_ids"] == ["c1", "c2"]
    assert recovered["reservation"] == reserved
    assert admissions == [("ZA", "1.00")]
    assert len(tally_calls) == 2
    assert len(transport.posts) == 1
    assert len(transport.gets) == 1
    assert Path(recovered["raw_receipts"]["post"]).read_bytes() == post_receipt
    assert Path(recovered["raw_receipts"]["terminal_get"]).read_bytes() == get_receipt
    marker = json.loads((tmp_path / "friday-20261002-fresh" / "ZA.attempt.json").read_text(encoding="utf-8"))
    report = json.loads((tmp_path / "friday-20261002-fresh" / "report.json").read_text(encoding="utf-8"))
    assert marker["result"]["fully_accounted"] is True
    assert report["markets"][0]["fully_accounted"] is True
    assert report["reserved_ceiling_usd"] == "0.50"


@pytest.mark.parametrize("changed_total", ["1.99", "2.01"])
def test_resume_rejects_a_changed_fresh_budget_cap(tmp_path, changed_total):
    transport = Transport(Response(202, {"ask_id": "unused"}))
    make_runner(tmp_path, transport, total="2.00")
    manifest = json.loads((tmp_path / "friday-20261002-fresh" / "namespace.json").read_text(encoding="utf-8"))

    assert manifest["original_fresh_cap_usd"] == "2.00"
    with pytest.raises(FridayLiveRefused, match="namespace_budget_cap_mismatch"):
        make_runner(tmp_path, transport, total=changed_total, resume=True)
    assert transport.posts == []


@pytest.mark.parametrize("changed_field", ["question", "market"])
def test_resume_rejects_terminal_record_with_same_id_but_changed_request(tmp_path, changed_field):
    clock = Clock()
    correct = record("a_20261002_abcd", claims=[])
    mismatched = dict(correct)
    mismatched[changed_field] = "different" if changed_field == "question" else "NG"
    transport = Transport(
        Response(202, {"ask_id": "a_20261002_abcd", "status": "running"}),
        Response(200, dict(correct, status="running")),
        Response(200, dict(mismatched, status="complete")),
    )
    first = make_runner(tmp_path, transport, clock=clock, deadline=0)

    pending = first.run_market("ZA")
    resumed = make_runner(tmp_path, transport, clock=clock, resume=True).resume_market("ZA")

    assert pending["status"] == "pending_readback"
    assert resumed["status"] == "pending_readback"
    assert resumed["reason"] == "readback_request_mismatch"
    assert len(transport.posts) == 1
    assert len(transport.gets) == 2


def test_namespace_budget_lock_serializes_admission_and_marker_creation(tmp_path):
    entered_admission = Event()
    finish_admission = Event()
    admission_markets = []
    za_transport = Transport(
        Response(202, {"ask_id": "za", "status": "running"}),
        Response(200, record("za", claims=[{"id": "z1"}])),
    )
    ng_transport = Transport(Response(202, {"ask_id": "ng", "status": "running"}))

    def approve(*, market, question, remaining_budget_usd):
        admission_markets.append((market, remaining_budget_usd))
        entered_admission.set()
        if market == "ZA":
            assert finish_admission.wait(timeout=5)
        return {"admitted": True, "known_ceiling_usd": "0.75"}

    first = make_runner(tmp_path, za_transport, budget_admission=approve, total="1.00")
    second = make_runner(tmp_path, ng_transport, budget_admission=approve, total="1.00", resume=True)
    first_results = []
    first_errors = []

    def dispatch_first():
        try:
            first_results.append(first.run_market("ZA"))
        except Exception as exc:
            first_errors.append(exc)

    thread = Thread(target=dispatch_first)
    thread.start()
    try:
        assert entered_admission.wait(timeout=2)
        with pytest.raises(FridayLiveRefused, match="namespace_budget_lock_busy"):
            second.run_market("NG")
    finally:
        finish_admission.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert first_errors == []
    assert first_results[0]["status"] == "complete"
    assert admission_markets == [("ZA", "1.00")]
    assert len(za_transport.posts) == 1
    assert ng_transport.posts == []
    report = json.loads((tmp_path / "friday-20261002-fresh" / "report.json").read_text(encoding="utf-8"))
    assert report["reserved_ceiling_usd"] == "0.75"
    assert report["remaining_budget_usd"] == "0.25"


def test_fully_accounted_insufficient_claims_continue_to_next_markets_once(tmp_path):
    transport = Transport(
        [Response(202, {"ask_id": "za", "status": "running"}),
         Response(202, {"ask_id": "ng", "status": "running"}),
         Response(202, {"ask_id": "ke", "status": "running"})],
        Response(200, record("za", claims=[{"id": "z1"}])),
        Response(200, record("ng", claims=[{"id": "n1"}, {"id": "n2"}], answer_status="partial", market="NG")),
        Response(200, record("ke", claims=[{"id": "k1"}, {"id": "k2"}], market="KE")),
    )
    runner = make_runner(tmp_path, transport, total="2.00")

    report = runner.run_markets()

    assert [item[1]["market"] for item in transport.posts] == ["ZA", "NG", "KE"]
    assert all(item[1]["wait"] is False for item in transport.posts)
    assert [item["claim_tally_status"] for item in report["markets"]] == ["accounted"] * 3
    assert [item["candidate_to_l5"] for item in report["markets"]] == [False, True, True]
    assert report["markets"][1]["terminal_readback"]["answer"]["status"] == "partial"
    assert report["markets"][1]["pass_to_l5"] is True
    assert report["markets"][0]["next_action"] == "continue_to_next_market"
    assert report["reserved_ceiling_usd"] == "1.50"
    assert report["remaining_budget_usd"] == "0.50"


def test_native_rows_and_original_answer_survive_with_exact_runs_record_check(tmp_path):
    api_record = record("a_20261002_abcd", claims=[{"id": "c1"}], answer_status="partial")
    transport = Transport(
        Response(202, {"ask_id": "a_20261002_abcd", "status": "running"}),
        Response(200, api_record),
    )

    def native_readback(record):
        proof = tally(record)
        proof["native_claim_rows"] = [
            {"claim_id": "c1", "check_id": "K4", "status": "failed"},
            {"claim_id": "c1", "check_id": "K5", "status": "passed"},
        ]
        record["answer"]["status"] = "mutated by adapter"
        return proof

    runner = make_runner(tmp_path, transport, claim_tally=native_readback)

    result = runner.run_market("ZA")

    assert result["native_runs_record_match"] is True
    assert result["claim_tally"]["native_claim_rows"] == [
        {"claim_id": "c1", "check_id": "K4", "status": "failed"},
        {"claim_id": "c1", "check_id": "K5", "status": "passed"},
    ]
    assert result["terminal_readback"]["answer"]["status"] == "partial"
    assert result["candidate_to_l5"] is False


def test_native_runs_record_mismatch_holds_next_market(tmp_path):
    transport = Transport(
        [Response(202, {"ask_id": "za", "status": "running"}),
         Response(202, {"ask_id": "ng", "status": "running"})],
        Response(200, record("za", claims=[{"id": "z1"}])),
    )

    def mismatched_readback(api_record):
        proof = tally(api_record)
        saved = dict(api_record, question="different")
        proof["native_runs_record"]["record"] = json.dumps(saved, ensure_ascii=False)
        return proof

    runner = make_runner(tmp_path, transport, claim_tally=mismatched_readback)

    report = runner.run_markets()

    assert len(transport.posts) == 1
    assert report["markets"][0]["native_runs_record_match"] is False
    assert report["markets"][0]["claim_tally_status"] == "unaccounted"
    assert report["markets"][0]["next_action"] == "hold"


def test_unaccounted_final_answer_ids_hold_next_market(tmp_path):
    transport = Transport(
        [Response(202, {"ask_id": "za", "status": "running"}),
         Response(202, {"ask_id": "ng", "status": "running"})],
        Response(200, record("za", claims=[{"id": "z1"}])),
    )
    runner = make_runner(
        tmp_path, transport,
        claim_tally=lambda record: {**tally(record), "final_answer_claim_ids": []},
    )

    report = runner.run_markets()

    assert len(transport.posts) == 1
    assert report["markets"][0]["claim_tally_status"] == "unaccounted"
    assert report["markets"][0]["next_action"] == "hold"


def test_remaining_budget_uses_reserved_ceiling_and_does_not_release_on_lower_usage(tmp_path):
    admission_calls = []

    def approve(*, market, question, remaining_budget_usd):
        admission_calls.append((market, remaining_budget_usd))
        return {"admitted": True, "known_ceiling_usd": "0.60"}

    transport = Transport(
        [Response(202, {"ask_id": "za", "status": "running"}),
         Response(202, {"ask_id": "ng", "status": "running"})],
        Response(200, record("za", claims=[{"id": "z1"}], model_usd="0.10")),
    )
    runner = make_runner(tmp_path, transport, budget_admission=approve, total="1.00")

    report = runner.run_markets()

    assert admission_calls == [("ZA", "1.00"), ("NG", "0.40")]
    assert len(transport.posts) == 1
    assert report["markets"][0]["application_reported_model_spend"]["model_usd"] == "0.10"
    assert report["markets"][0]["provider_charges"] == {"status": "unproven", "measured_usd": None}
    assert report["reserved_ceiling_usd"] == "0.60"
    assert report["remaining_budget_usd"] == "0.40"
    assert report["unattempted_markets"] == ["KE"]


def test_terminal_failure_is_saved_and_never_retried(tmp_path):
    transport = Transport(
        Response(202, {"ask_id": "failed", "status": "running"}),
        Response(200, record("failed", status="failed", model_usd="0.50")
                 | {"run": {"model_usd": "0.50", "tokens": {"input": 20, "output": 2}}}),
    )
    runner = make_runner(tmp_path, transport)

    result = runner.run_market("ZA")

    assert result["status"] == "failed"
    assert len(transport.posts) == 1
    with pytest.raises(FridayLiveRefused, match="already_attempted"):
        runner.run_market("ZA")
    assert len(transport.posts) == 1


def test_numeric_application_total_with_conservative_booking_stays_unproven_as_provider_charges(tmp_path):
    transport = Transport(
        Response(202, {"ask_id": "booked", "status": "running"}),
        Response(200, record("booked", claims=[{"id": "c1"}], model_usd="0.80")),
    )
    runner = make_runner(tmp_path, transport, budget_admission=admission("0.90"), total="1.00")

    result = runner.run_market("ZA")

    assert result["application_reported_model_spend"]["model_usd"] == "0.80"
    assert result["terminal_readback"]["run"]["model_usd"] == "0.80"
    assert result["provider_charges"] == {"status": "unproven", "measured_usd": None}
    assert result["cost_accounted"] is True
