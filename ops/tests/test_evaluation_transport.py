import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from ops.evaluation import run_questions
from ops.evaluation.run_questions import (
    AllowanceRefused,
    CaseLedger,
    ManifestRefused,
    TransportFailure,
    TransportResponse,
    TransportTimeout,
    build_manifest,
    development_bank,
    holdout_template,
    load_bank,
    run_manifest,
    validate_holdout,
)

# The frozen bank and holdout template live in the protected evidence tree
# beside the operations checkout, never inside the repository.
EVIDENCE = Path(__file__).resolve().parents[4] / (
    ".superpowers/sdd/2026-09-12-42-staging-completion/evaluation"
)
POLICY = {
    "policy_id": "general_question_allowance_v1",
    "as_of": "2026-09-12",
    "max_requests": 100,
    "cap_microusd": 10_000_000,
    "reserve_per_request_microusd": 100_000,
    "admitted_requests": 37,
    "reserved_microusd": 3_700_000,
    "request_deadline_seconds": 240,
    "poll_interval_seconds": 5,
    "retry_backoff_seconds": 10,
}
REQUEST_A = "0f88a887-1439-4b45-b12b-3ad05e7134d8"
REQUEST_B = "1637d0ae-b991-4e14-b2cc-f860535ab494"


def job_id(request_id):
    return "chat_" + request_id.replace("-", "")


BANK_DOCUMENT = EVIDENCE.parents[3] / "GENERAL_INTELLIGENCE_EVALUATION.md"


def published():
    return development_bank(BANK_DOCUMENT.read_bytes().decode("ascii"))


def frozen_bank():
    bank, _digest = load_bank(EVIDENCE / "development-bank-36.json")
    return bank


def synthetic_bank(cases=2):
    families = [
        {"key": key, "name": name, "decision": decision}
        for key, name, decision in run_questions.FAMILIES
    ]
    return {
        "contract_version": "development_bank_v1",
        "kind": "development_bank",
        "source": {"document": "synthetic", "sha256": "0" * 64},
        "families": families,
        "cases": [
            {
                "case_key": f"S{index + 1:02d}",
                "family": families[index]["key"],
                "kind": "unseen",
                "sequence": 1,
                "parent_case_key": None,
                "wording": f"Question {index + 1}?",
                "market": "za",
            }
            for index in range(cases)
        ],
    }


def lifecycle(request_id, state, engine_state, *, reason=None, deadline=None):
    return {
        "request_id": request_id,
        "state": state,
        "engine_state": engine_state,
        "reason_code": reason,
        "deadline_at": deadline or "2099-01-01T00:00:00Z",
    }


def pending(request_id, deadline=None):
    return {
        "pending": True,
        "lifecycle": lifecycle(request_id, "pending", "admitted", deadline=deadline),
    }


def terminal(request_id, state, *, reason=None):
    body = {"answer": "x", "sources": [], "intelligence": {"status": state}}
    if state != "complete":
        body["error"] = True
    return body | {"lifecycle": lifecycle(request_id, state, state, reason=reason)}


class FakeClock:
    def __init__(self):
        self.seconds = 1000.0
        self.now = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)
        self.slept = []

    def monotonic(self):
        return self.seconds

    def utcnow(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.seconds += seconds
        self.now += timedelta(seconds=seconds)


class FakeTransport:
    """Mirror of POST /api/chat/send, GET /api/chat/status and the
    observation reads, driven by scripted replies."""

    def __init__(self, *, send=(), status=(), inventory=None, details=None):
        self.send_script = list(send)
        self.status_script = list(status)
        self.inventory_rows = list(inventory or [])
        self.details = dict(details or {})
        self.sent = []
        self.status_calls = []
        self.inventory_calls = 0
        self.detail_calls = []

    def _next(self, script, default):
        if not script:
            return default
        step = script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return step()
        return step

    def send(self, body, headers):
        assert headers == {"X-Passcode": "secret"}
        self.sent.append(dict(body))
        return self._next(self.send_script, TransportResponse(503, "{}"))

    def status(self, job, headers):
        assert headers == {"X-Passcode": "secret"}
        self.status_calls.append(job)
        return self._next(
            self.status_script,
            TransportResponse(200, json.dumps(pending(str(UUID(hex=job[5:]))))),
        )

    def inventory(self, headers):
        self.inventory_calls += 1
        return TransportResponse(
            200, json.dumps({"scope": {}, "coverage": {}, "rows": self.inventory_rows})
        )

    def detail(self, request_id, headers):
        self.detail_calls.append(request_id)
        if request_id not in self.details:
            return TransportResponse(404, '{"detail": "unknown"}')
        return TransportResponse(200, json.dumps(self.details[request_id]))


def ok(payload, status=200):
    return TransportResponse(status, json.dumps(payload))


def accepted(request_id):
    return ok({"job_id": job_id(request_id)}, 202)


def run(tmp_path, transport, *, bank=None, clock=None, nonces=None):
    clock = clock or FakeClock()
    nonces = iter(nonces or ["nonce-000000000000000001", "nonce-000000000000000002"])
    manifest = build_manifest(
        bank or synthetic_bank(1),
        policy=POLICY,
        run_id="run-01",
        nonce_factory=lambda: next(nonces),
    )
    result = run_manifest(
        manifest,
        transport=transport,
        ledger_dir=tmp_path,
        clock=clock,
        passcode="secret",
    )
    ledger = [
        json.loads(line)
        for line in (tmp_path / "case-ledger.jsonl").read_text().splitlines()
    ]
    return result, ledger, clock


def events(ledger, case_id):
    return [
        (row["event"], row.get("state")) for row in ledger if row["case_id"] == case_id
    ]


# Bank and holdout


def test_frozen_bank_matches_the_published_document_and_its_hash():
    bank, digest = load_bank(EVIDENCE / "development-bank-36.json")
    assert bank == published()
    assert (
        bank["source"]["sha256"]
        == hashlib.sha256(BANK_DOCUMENT.read_bytes()).hexdigest()
    )
    assert [case["case_key"] for case in bank["cases"]] == [
        f"Q{n:02d}" for n in range(1, 37)
    ]
    assert len(bank["families"]) == 12
    assert [case["sequence"] for case in bank["cases"]] == [1, 2, 3] * 12
    assert all(case["wording"].strip() for case in bank["cases"])
    assert bank["cases"][0]["wording"].startswith("What changed in South African")
    assert bank["cases"][35]["family"] == "bsa"
    assert len(digest) == 64
    manifest = build_manifest(bank, policy=POLICY, run_id="run-01")
    assert manifest["bank_sha256"] == digest


def test_tampered_bank_document_is_refused():
    text = BANK_DOCUMENT.read_bytes().decode("ascii").replace("Q36", "Q37", 1)
    with pytest.raises(ValueError, match="bank_source_mismatch"):
        development_bank(text)


def test_frozen_files_are_ascii_with_lf():
    for name in ("development-bank-36.json", "final-holdout-84-template.json"):
        raw = (EVIDENCE / name).read_bytes()
        raw.decode("ascii")
        assert b"\r" not in raw
        assert raw.endswith(b"\n")


def test_manifest_over_the_bank_fits_the_dated_allowance():
    manifest = build_manifest(frozen_bank(), policy=POLICY, run_id="run-01")
    authorization = manifest["authorization"]
    assert authorization == {
        "requests_needed": 36,
        "reserve_needed_microusd": 3_600_000,
        "remaining_slots": 63,
        "remaining_reserve_microusd": 6_300_000,
        "fits": True,
        "refusal_code": None,
    }
    assert manifest["submittable"] is True
    assert manifest["unworded_cases"] == 0
    assert len({case["case_id"] for case in manifest["cases"]}) == 36
    assert len({case["nonce"] for case in manifest["cases"]}) == 36
    for case in manifest["cases"]:
        assert run_questions.IDEMPOTENCY_KEY.fullmatch(case["nonce"])
        assert case["deadline_seconds"] == 240


def test_case_ids_are_stable_across_runs_and_nonces_are_not():
    first = build_manifest(frozen_bank(), policy=POLICY, run_id="run-01")
    second = build_manifest(frozen_bank(), policy=POLICY, run_id="run-02")
    assert [c["case_id"] for c in first["cases"]] == [
        c["case_id"] for c in second["cases"]
    ]
    assert not {c["nonce"] for c in first["cases"]} & {
        c["nonce"] for c in second["cases"]
    }


def test_manifest_refuses_by_name_when_slots_do_not_fit():
    with pytest.raises(AllowanceRefused, match="allowance_slots_exceeded") as raised:
        build_manifest(
            frozen_bank(),
            policy=POLICY | {"admitted_requests": 80},
            run_id="run-01",
        )
    assert raised.value.code == "allowance_slots_exceeded"
    assert raised.value.authorization["remaining_slots"] == 20


def test_manifest_refuses_by_name_when_reserve_does_not_fit():
    with pytest.raises(AllowanceRefused, match="allowance_reserve_exceeded"):
        build_manifest(
            frozen_bank(),
            policy=POLICY | {"cap_microusd": 7_000_000},
            run_id="run-01",
        )


def test_manifest_requires_a_complete_policy_snapshot():
    policy = dict(POLICY)
    del policy["cap_microusd"]
    with pytest.raises(ValueError, match="policy_field_missing"):
        build_manifest(frozen_bank(), policy=policy, run_id="run-01")


def test_holdout_template_has_84_typed_unfilled_slots():
    template = holdout_template()
    assert len(template["slots"]) == 84
    families = [family["key"] for family in template["families"]]
    assert len(families) == 12
    for family in families:
        kinds = [s["kind"] for s in template["slots"] if s["family"] == family]
        assert kinds == [
            "unseen",
            "unseen",
            "unseen",
            "paraphrase",
            "scope_changing_follow_up",
            "challenge",
            "unsupported",
        ]
    for slot in template["slots"]:
        assert slot["wording"] is None
        assert slot["author"] is None
        assert slot["answerability"] is None
    frozen = json.loads((EVIDENCE / "final-holdout-84-template.json").read_text())
    assert frozen == template


def test_unfilled_holdout_refuses_a_manifest():
    with pytest.raises(ManifestRefused, match="holdout_slot_unfilled"):
        build_manifest(holdout_template(), policy=POLICY, run_id="run-01")
    with pytest.raises(ManifestRefused, match="holdout_slot_unfilled") as raised:
        validate_holdout(holdout_template())
    assert raised.value.slot == "discovery-unseen-1"


def test_partly_filled_holdout_still_refuses():
    template = holdout_template()
    for slot in template["slots"]:
        slot.update(wording="w?", author="human", answerability=True, market="za")
    template["slots"][40]["answerability"] = None
    with pytest.raises(ManifestRefused, match="holdout_slot_unfilled") as raised:
        validate_holdout(template)
    assert raised.value.slot == template["slots"][40]["slot_key"]


def test_filled_holdout_builds_84_requests_and_needs_more_than_63_slots():
    template = holdout_template()
    for slot in template["slots"]:
        slot.update(wording="w?", author="human", answerability=True, market="za")
    with pytest.raises(AllowanceRefused, match="allowance_slots_exceeded") as raised:
        build_manifest(template, policy=POLICY, run_id="run-01")
    assert raised.value.authorization["requests_needed"] == 84


def test_run_refuses_an_unworded_manifest_before_any_transport_call(tmp_path):
    transport = FakeTransport()
    bank = frozen_bank()
    for case in bank["cases"]:
        case["wording"] = None
    manifest = build_manifest(bank, policy=POLICY, run_id="run-01")
    assert manifest["submittable"] is False
    assert manifest["unworded_cases"] == 36
    with pytest.raises(ManifestRefused, match="case_wording_missing"):
        run_manifest(
            manifest,
            transport=transport,
            ledger_dir=tmp_path,
            clock=FakeClock(),
            passcode="secret",
        )
    assert transport.sent == []
    assert not (tmp_path / "case-ledger.jsonl").exists()


# Transport state machine


def test_happy_path_stores_case_id_and_nonce_before_the_post(tmp_path):
    transport = FakeTransport(
        send=[accepted(REQUEST_A)],
        status=[
            ok(pending(REQUEST_A)),
            ok(terminal(REQUEST_A, "complete")),
            ok(terminal(REQUEST_A, "complete")),
        ],
    )
    result, ledger, _ = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == "complete"
    assert case["request_id"] == REQUEST_A
    assert case["job_id"] == job_id(REQUEST_A)
    assert transport.sent == [
        {
            "message": "Question 1?",
            "history": [],
            "market": "za",
            "idempotency_key": "nonce-000000000000000001",
        }
    ]
    planned = ledger[0]
    assert planned["event"] == "planned"
    assert planned["case_id"] == case["case_id"]
    assert planned["nonce"] == "nonce-000000000000000001"
    assert ledger[1]["event"] == "post_attempt"
    assert events(ledger, case["case_id"]) == [
        ("planned", "planned"),
        ("post_attempt", "planned"),
        ("observation", "submitted"),
        ("observation", "pending"),
        ("observation", "complete"),
        ("observation", "complete"),
        ("final", "complete"),
    ]
    assert [row["sequence"] for row in ledger] == list(range(1, len(ledger) + 1))


def test_rate_limit_retries_with_the_same_nonce_until_the_original_deadline(
    tmp_path,
):
    transport = FakeTransport(send=[TransportResponse(429, '{"detail": "slow"}')] * 40)
    result, _ledger, clock = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == "failed"
    assert case["reason"] == "rate_limited"
    assert len(transport.sent) == 25
    assert {body["idempotency_key"] for body in transport.sent} == {
        "nonce-000000000000000001"
    }
    assert transport.status_calls == []
    assert clock.slept == [10] * 24


def test_rate_limit_then_acceptance_completes(tmp_path):
    transport = FakeTransport(
        send=[TransportResponse(429, "{}"), accepted(REQUEST_A)],
        status=[ok(terminal(REQUEST_A, "complete"))] * 2,
    )
    result, _, _ = run(tmp_path, transport)
    assert result["cases"][0]["state"] == "complete"
    assert len(transport.sent) == 2


def test_timeout_without_acceptance_reconciles_and_stays_unknown(tmp_path):
    transport = FakeTransport(send=[TransportTimeout("read timed out")])
    result, ledger, _ = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == "unknown"
    assert case["reason"] == "reconciliation_no_match"
    assert len(transport.sent) == 1
    assert transport.inventory_calls == 1
    assert transport.status_calls == []
    assert ("reconciliation", "planned") in events(ledger, case["case_id"])


def test_ambiguous_acknowledgement_adopts_the_accepted_request(tmp_path):
    def accept_then_raise():
        transport.inventory_rows.append(
            {
                "kind": "research",
                "entity_id": REQUEST_A,
                "operation_id": "gq_" + REQUEST_A.replace("-", ""),
                "status": "running",
                "updated_at": "2026-09-14T08:00:01Z",
                "market_scope": ["za"],
            }
        )
        transport.details[REQUEST_A] = {
            "contract_version": "general_question_detail_v1",
            "request_id": REQUEST_A,
            "observed_state": "admitted",
            "question": "Question 1?",
            "history": [],
            "selected_market": "za",
            "requested_window": None,
            "response": None,
            "reserved_microusd": 100000,
            "missing_work": ["terminal_record_unavailable"],
        }
        raise TransportFailure("connection reset")

    transport = FakeTransport(
        send=[accept_then_raise],
        status=[ok(terminal(REQUEST_A, "complete"))] * 2,
    )
    result, ledger, _ = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == "complete"
    assert case["request_id"] == REQUEST_A
    assert len(transport.sent) == 1
    assert transport.detail_calls == [REQUEST_A]
    assert ("reconciliation", "submitted") in events(ledger, case["case_id"])


def test_ambiguous_acknowledgement_with_two_candidates_stays_unknown(tmp_path):
    rows = []
    details = {}
    for request_id in (REQUEST_A, REQUEST_B):
        rows.append(
            {
                "kind": "research",
                "entity_id": request_id,
                "operation_id": "gq_" + request_id.replace("-", ""),
                "status": "running",
                "updated_at": None,
                "market_scope": ["za"],
            }
        )
        details[request_id] = {
            "request_id": request_id,
            "question": "Question 1?",
            "selected_market": "za",
            "observed_state": "admitted",
        }
    transport = FakeTransport(
        send=[TransportTimeout("t")], inventory=rows, details=details
    )
    result, _, _ = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == "unknown"
    assert case["reason"] == "reconciliation_ambiguous"
    assert len(transport.sent) == 1


def test_service_unavailable_reconciles_then_fails_without_resubmission(tmp_path):
    transport = FakeTransport(
        send=[
            TransportResponse(
                503, '{"detail": "General question worker is unavailable"}'
            )
        ]
    )
    result, _, _ = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == "failed"
    assert case["reason"] == "service_unavailable"
    assert len(transport.sent) == 1
    assert transport.inventory_calls == 1


def test_malformed_acceptance_body_reconciles_instead_of_resubmitting(tmp_path):
    transport = FakeTransport(send=[TransportResponse(202, "<html>gateway</html>")])
    result, _, _ = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == "unknown"
    assert case["reason"] == "reconciliation_no_match"
    assert len(transport.sent) == 1


def test_malformed_status_body_is_retried_and_then_resolves(tmp_path):
    transport = FakeTransport(
        send=[accepted(REQUEST_A)],
        status=[
            TransportResponse(200, "not json"),
            ok(terminal(REQUEST_A, "partial")),
            ok(terminal(REQUEST_A, "partial")),
        ],
    )
    result, ledger, _ = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == "partial"
    assert ("observation", "submitted") in events(ledger, case["case_id"])[2:4]


def test_mismatched_job_id_in_status_is_unknown_not_adopted(tmp_path):
    transport = FakeTransport(
        send=[accepted(REQUEST_A)],
        status=[ok(terminal(REQUEST_B, "complete"))],
    )
    result, _, _ = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == "unknown"
    assert case["reason"] == "job_id_mismatch"
    assert case["request_id"] == REQUEST_A
    assert len(transport.sent) == 1


def test_repeated_terminal_response_records_one_transition(tmp_path):
    transport = FakeTransport(
        send=[accepted(REQUEST_A)],
        status=[ok(terminal(REQUEST_A, "refused"))] * 5,
    )
    result, ledger, _ = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == "refused"
    assert len(transport.status_calls) == 2
    transitions = [
        row
        for row in ledger
        if row["event"] == "observation"
        and row["state"] == "refused"
        and row["transition"]
    ]
    assert len(transitions) == 1
    assert [row["state"] for row in ledger if row["event"] == "observation"][-2:] == [
        "refused",
        "refused",
    ]


def test_conflicting_terminal_repeat_is_unknown(tmp_path):
    transport = FakeTransport(
        send=[accepted(REQUEST_A)],
        status=[
            ok(terminal(REQUEST_A, "complete")),
            ok(terminal(REQUEST_A, "refused")),
        ],
    )
    result, _, _ = run(tmp_path, transport)
    assert result["cases"][0]["state"] == "unknown"
    assert result["cases"][0]["reason"] == "terminal_conflict"


def test_original_deadline_expiry_fails_the_case_without_extension(tmp_path):
    deadline = "2026-09-14T08:01:00Z"
    transport = FakeTransport(
        send=[accepted(REQUEST_A)],
        status=[ok(pending(REQUEST_A, deadline))] * 100,
    )
    result, _ledger, clock = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == "failed"
    assert case["reason"] == "deadline_expired"
    assert case["deadline_at"] == deadline
    assert clock.now >= datetime(2026, 9, 14, 8, 1, tzinfo=UTC)
    assert len(transport.status_calls) < 100
    assert len(transport.sent) == 1


def test_status_timeouts_past_the_deadline_end_unknown(tmp_path):
    transport = FakeTransport(
        send=[accepted(REQUEST_A)], status=[TransportTimeout("t")] * 100
    )
    result, _, _ = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == "unknown"
    assert case["reason"] == "status_unreachable"
    assert len(transport.sent) == 1


@pytest.mark.parametrize(
    ("engine_state", "expected", "reason"),
    [
        ("insufficient_evidence", "refused", "evidence_insufficient"),
        ("needs_clarification", "refused", None),
        ("unavailable", "failed", "model_timeout"),
        ("held", "failed", "held"),
    ],
)
def test_engine_terminal_states_map_onto_the_eight(
    tmp_path, engine_state, expected, reason
):
    transport = FakeTransport(
        send=[accepted(REQUEST_A)],
        status=[
            ok(
                terminal(REQUEST_A, engine_state)
                if reason is None
                else terminal(REQUEST_A, engine_state, reason=reason)
            )
        ]
        * 2,
    )
    result, _, _ = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == expected
    assert case["reason"] == reason


def test_held_without_engine_reason_retains_the_metric_marker(tmp_path):
    transport = FakeTransport(
        send=[accepted(REQUEST_A)],
        status=[ok(terminal(REQUEST_A, "held"))] * 2,
    )
    result, _, _ = run(tmp_path, transport)
    [case] = result["cases"]
    assert case["state"] == "failed"
    assert case["reason"] == "held"


def test_unrecognized_lifecycle_state_is_unknown(tmp_path):
    transport = FakeTransport(
        send=[accepted(REQUEST_A)], status=[ok(terminal(REQUEST_A, "done"))] * 2
    )
    result, _, _ = run(tmp_path, transport)
    assert result["cases"][0]["state"] == "unknown"
    assert result["cases"][0]["reason"] == "lifecycle_state_unrecognized"


def test_follow_up_takes_both_ids_only_from_its_completed_parent(tmp_path):
    bank = synthetic_bank(2)
    bank["cases"][1]["kind"] = "scope_changing_follow_up"
    bank["cases"][1]["parent_case_key"] = bank["cases"][0]["case_key"]
    transport = FakeTransport(
        send=[accepted(REQUEST_A), accepted(REQUEST_B)],
        status=[ok(terminal(REQUEST_A, "complete"))] * 2
        + [ok(terminal(REQUEST_B, "complete"))] * 2,
    )
    result, _, _ = run(tmp_path, transport, bank=bank)
    assert [case["state"] for case in result["cases"]] == ["complete", "complete"]
    assert transport.sent[1]["parent_request_id"] == REQUEST_A
    assert transport.sent[1]["thread_anchor_request_id"] == REQUEST_A
    assert "parent_request_id" not in transport.sent[0]


def test_follow_up_is_not_sent_when_the_parent_did_not_complete(tmp_path):
    bank = synthetic_bank(2)
    bank["cases"][1]["kind"] = "challenge"
    bank["cases"][1]["parent_case_key"] = bank["cases"][0]["case_key"]
    transport = FakeTransport(
        send=[accepted(REQUEST_A)],
        status=[ok(terminal(REQUEST_A, "partial"))] * 2,
    )
    result, _, _ = run(tmp_path, transport, bank=bank)
    assert [case["state"] for case in result["cases"]] == ["partial", "failed"]
    assert result["cases"][1]["reason"] == "parent_not_complete"
    assert len(transport.sent) == 1


def test_ledger_is_append_only_and_survives_a_second_run(tmp_path):
    transport = FakeTransport(
        send=[accepted(REQUEST_A)], status=[ok(terminal(REQUEST_A, "complete"))] * 2
    )
    run(tmp_path, transport)
    first = (tmp_path / "case-ledger.jsonl").read_text()
    transport = FakeTransport(
        send=[accepted(REQUEST_B)], status=[ok(terminal(REQUEST_B, "complete"))] * 2
    )
    run(tmp_path, transport, nonces=["nonce-000000000000000009"])
    second = (tmp_path / "case-ledger.jsonl").read_text()
    assert second.startswith(first)
    assert len(second.splitlines()) == 2 * len(first.splitlines())


def test_ledger_rejects_records_without_identity(tmp_path):
    ledger = CaseLedger(tmp_path / "case-ledger.jsonl", clock=FakeClock())
    with pytest.raises(ValueError, match="ledger_identity_required"):
        ledger.append({"event": "planned", "state": "planned"})
