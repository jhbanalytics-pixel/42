import copy
import hashlib
import json
from decimal import Decimal

import pytest

from core.api import dossiers
from core.eval import demo_native_accounting, demo_saved_work


IOL_ID = "obs1_dce53bd6bb321b436ac9d0078763dc04"
IOL_QUOTE = (
    "WhatsApp messages presented to the Madlanga Commission have detailed allegations that taxi "
    "bosses Joe “Ferrari” Sibanyoni and Bafana “King of the Sky” Sindane used threats, intimidation "
    "and the prospect of violent disruption to extract money from businessmen operating in Mpumalanga."
)
SAFE_TEXT = (
    "The post reports allegations of threats, intimidation and violent disruption to extract money "
    "from businessmen in Mpumalanga."
)


def test_app_ask_run_date_uses_sast_without_rewriting_timestamps():
    for created_at, expected_day in (
        ("2026-10-01T22:14:53+01:00", "2026-10-01"),
        ("2026-10-02T00:30:00+03:00", "2026-10-01"),
    ):
        record = {
            "ask_id": "ask-test", "created_at": created_at, "finished_at": created_at,
            "status": "failed", "question": "test", "answer": None,
            "error": {"error": "internal"}, "run": {"run_id": "run-test", "seconds": 1},
        }
        row = demo_saved_work._app_ask_row(record, {"tier": "T1"})
        assert row["run_date"] == expected_day
        assert row["started_at"] == created_at
        assert row["finished_at"] == created_at

    record["created_at"] = "2026-10-02T00:30:00"
    with pytest.raises(demo_saved_work.SelectionMismatch, match="must include a timezone"):
        demo_saved_work._app_ask_row(record, {"tier": "T1"})


def _hash(value):
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_integral(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _normalize_integral(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_integral(item) for item in value]
    return value


def _integral_native_row(row):
    output = copy.deepcopy(row)
    for key in ("answer", "record"):
        value = output.get(key)
        was_text = isinstance(value, str)
        if was_text:
            value = json.loads(value)
        value = _normalize_integral(value)
        output[key] = json.dumps(value) if was_text else value
    for key, value in list(output.items()):
        if key not in ("answer", "record"):
            output[key] = _normalize_integral(value)
    return output


def _native_row_with_credit(value):
    def transform(row):
        output = _integral_native_row(row)
        record = json.loads(output["record"])
        record["run"]["credits"] = value
        output["record"] = json.dumps(record)
        output["credits"] = value
        return output
    return transform

def _receipt_bytes(receipt):
    text = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text.encode("utf-8")


def _receipt_sha256(receipt_bytes):
    return "sha256:" + hashlib.sha256(receipt_bytes).hexdigest()


def _refresh_receipt(values):
    values["raw_receipt_bytes"] = _receipt_bytes(values["selected_receipt"])
    values["raw_receipt_sha256"] = _receipt_sha256(values["raw_receipt_bytes"])


def _source_answer():
    return {
        "status": "complete",
        "as_of": "2026-09-30T12:00:00+02:00",
        "short_answer": "A broad national account that the saved view must omit.",
        "claims": [
            {
                "id": "c1",
                "text": SAFE_TEXT,
                "label": "observed",
                "kind": "observation",
                "evidence_ids": [IOL_ID],
                "quotes": [{"evidence_id": IOL_ID, "text": IOL_QUOTE}],
                "numbers": [],
                "check": "verified",
            },
            {
                "id": "c2",
                "text": "A broad national ranking and momentum claim.",
                "label": "single_source",
                "kind": "observation",
                "evidence_ids": [],
                "quotes": [],
                "numbers": [],
                "check": "verified",
            },
        ],
        "evidence": [
            {
                "id": IOL_ID,
                "platform": "news",
                "handle": "publisher",
                "url": "https://example.test/source/1",
                "text": IOL_QUOTE,
                "posted_at": "2026-09-28T12:00:00+02:00",
            },
        ],
        "so_what": [{"text": "Broad implication to omit.", "claim_ids": ["c1"]}],
        "watch_next": [],
        "gaps": [],
    }


def _case():
    answer = _source_answer()
    selected_claims = [copy.deepcopy(answer["claims"][0])]
    selected_evidence = [copy.deepcopy(answer["evidence"][0])]
    raw_hash = _hash(answer)
    record = {
        "ask_id": "ask-demo-1",
        "question": "What happened in the selected demo?",
        "parent_id": None,
        "market": "ZA",
        "status": "complete",
        "created_at": "2026-10-01T12:00:00+02:00",
        "finished_at": "2026-10-01T12:01:00+02:00",
        "answer": copy.deepcopy(answer),
        "run": {
            "run_id": "demo-run-1",
            "tier": "T1",
            "mode": "live",
            "credits": 1,
            "tokens": {"input": 1000, "output": 100},
            "seconds": 60.0,
            "model_usd": 2.02448,
            "window": "30d",
            "posts": 5,
            "platforms": ["news"],
            "source_status": [],
            "followups": [],
            "notices": [],
        },
        "steps": [{"seq": 1, "at": "2026-10-01T12:00:01+02:00", "text": "Searching source evidence"}],
        "error": None,
    }
    receipt = {
        "question_id": "DEMO-05",
        "attempt": 1,
        "run_id": "demo-run-1",
        "raw_answer": copy.deepcopy(answer),
        "context": {
            "window_start": "2026-09-24",
            "window_end": "2026-09-30",
            "evidence": [{
                "post_id": IOL_ID,
                "published_at": "2026-09-28T12:00:00+02:00",
                "text": IOL_QUOTE,
            }],
        },
    }
    selection = {
        "question_id": "DEMO-05",
        "status": "selected",
        "selected_attempt": 1,
        "run_ids": {"1": "demo-run-1", "2": "demo-run-2"},
        "raw_hashes": {"1": raw_hash, "2": "sha256:" + "2" * 64},
        "admitted_claim_ids": {"1": ["c1"], "2": []},
        "saved_payload": {
            "question_id": "DEMO-05",
            "status": "partial",
            "claims": selected_claims,
            "evidence": selected_evidence,
        },
        "full_pass": False,
    }
    funding = {
        "schema_version": "demo-funding-v1",
        "phase_id": "l3-demo-20261001",
        "run_date": "2026-10-01",
        "reservation_run_id": "demo-batch-1",
        "allocated_micros": 13_000_000,
        "consumed_micros": 0,
        "source_proof_sha": "4" * 64,
        "native_net_micros": 25_000_000,
        "baseline_run_ids": [f"baseline-{n}" for n in range(7)],
        "app_run_ids": [],
    }
    values = {
        "selection": selection,
        "selected_receipt": receipt,
        "app_record": record,
        "safe_payload": selection["saved_payload"],
        "admitted_claim_ids": ["c1"],
        "raw_receipt_bytes": _receipt_bytes(receipt),
        "raw_receipt_sha256": None,
        "funding": funding,
        "guarded_charge_usd": "2.025712",
    }
    values["raw_receipt_sha256"] = _receipt_sha256(values["raw_receipt_bytes"])
    return values


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = copy.deepcopy(body)

    def json(self):
        return copy.deepcopy(self._body)


class _NativeAdapter:
    def __init__(self, *, accounting_override=None, row_override=None, row_transform=None):
        self.calls = []
        self.last_row = None
        self.accounting_override = accounting_override
        self.row_override = row_override
        self.row_transform = row_transform
    def convert_prebooked_ask(self, *, real_ask_row, funding, guarded_charge):
        self.calls.append((copy.deepcopy(real_ask_row), funding, guarded_charge))
        self.last_row = copy.deepcopy(real_ask_row)
        readback_source = copy.deepcopy(self.row_override or real_ask_row)
        readback_row = self.row_transform(readback_source) if self.row_transform else readback_source
        record = json.loads(real_ask_row["record"])
        apppriced = format(Decimal(str(record["run"]["model_usd"])), "f")
        retained = format(Decimal(guarded_charge) - Decimal(apppriced), "f")
        accounting = {
            "apppriced_usd": apppriced,
            "guarded_usd": guarded_charge,
            "available_before_usd": "13.000000",
            "released_usd": apppriced,
            "retained_usd": retained,
        }
        if self.accounting_override:
            accounting.update(self.accounting_override)
        return {
            "status": "stored",
            "ask_row": readback_row,
            "ask_row_sha256": demo_native_accounting.run_row_sha256(real_ask_row),
            "accounting": accounting,
        }


class _AppClient:
    def __init__(self, native_adapter):
        self.native_adapter = native_adapter
        self.calls = []
        self.body = None
        self.fail_post = False
        self.change_readback_hash = False

    def post(self, path, *, json):
        self.calls.append(("POST", path, copy.deepcopy(json)))
        if self.fail_post:
            raise TimeoutError("outcome unknown")
        assert path == "/api/dossiers"
        assert set(json) == {"from"}
        ask_id = json["from"]["ask_id"]
        record = json_module.loads(self.native_adapter.last_row["record"])
        assert record["ask_id"] == ask_id
        selected = dossiers.selection({}, record, None)
        body = dossiers.build(
            record,
            selected,
            dossier_id="d_saved_demo",
            version=1,
            created_at=record["finished_at"],
            source={"ask_id": ask_id},
        )
        content_hash = dossiers.content_hash(body)
        self.body = {
            **body,
            "content_hash": content_hash,
            "ticks": [],
            "needs_tick": dossiers.needs_tick(body, {}),
        }
        return _Response(201, self.body)

    def get(self, path):
        self.calls.append(("GET", path))
        assert path == "/api/dossiers/d_saved_demo"
        body = copy.deepcopy(self.body)
        if self.change_readback_hash:
            body["content_hash"] = "sha256:" + "f" * 64
        return _Response(200, body)


json_module = json


def _persist(values, native):
    return demo_saved_work.persist_attempt_view(
        selected_receipt=values["selected_receipt"],
        app_record=values["app_record"],
        safe_payload=values["safe_payload"],
        admitted_claim_ids=values["admitted_claim_ids"],
        raw_receipt_bytes=values["raw_receipt_bytes"],
        raw_receipt_sha256=values["raw_receipt_sha256"],
        funding=values["funding"],
        guarded_charge_usd=values["guarded_charge_usd"],
        native_adapter=native,
    )


def _create(values, persisted, client):
    return demo_saved_work.create_saved_work(
        selection=values["selection"],
        selected_receipt=values["selected_receipt"],
        app_record=values["app_record"],
        selected_existing_ask=persisted,
        raw_receipt_bytes=values["raw_receipt_bytes"],
        raw_receipt_sha256=values["raw_receipt_sha256"],
        app_client=client,
    )


def test_persists_w2_safe_view_without_an_l5_claim_manifest_or_dossier_call():
    values = _case()
    native = _NativeAdapter()
    original_record = copy.deepcopy(values["app_record"])
    result = _persist(values, native)

    row, funding, guarded = native.calls[0]
    saved_record = json.loads(row["record"])
    assert saved_record["run"]["model_usd"] == 2.02448
    assert saved_record["answer"] == {
        "status": "partial",
        "as_of": original_record["answer"]["as_of"],
        "short_answer": "",
        "claims": [original_record["answer"]["claims"][0]],
        "evidence": [original_record["answer"]["evidence"][0]],
        "so_what": [],
        "watch_next": [],
        "gaps": [],
    }
    assert original_record == values["app_record"]
    assert funding is values["funding"] and guarded == "2.025712"
    assert result["ask_id"] == "ask-demo-1"
    assert result["run_id"] == "demo-run-1"
    assert result["scope_admitted_claim_ids"] == ["c1"]
    assert result["scope_admission_manifest_sha256"]
    assert result["source_answer_sha256"] == values["selection"]["raw_hashes"]["1"]
    assert result["source_receipt_sha256"] == values["raw_receipt_sha256"]
    assert result["saved_view_status"] == "partial"
    assert "dossier_id" not in result
    assert values["raw_receipt_bytes"].decode("utf-8") not in row["record"]


def test_creates_dossier_from_the_selected_previously_saved_w2_ask():
    values = _case()
    native = _NativeAdapter()
    persisted = _persist(values, native)
    client = _AppClient(native)

    result = _create(values, persisted, client)

    assert len(native.calls) == 1
    assert [call[0] for call in client.calls] == ["POST", "GET"]
    assert client.calls[0][1:] == (
        "/api/dossiers",
        {"from": {"ask_id": "ask-demo-1"}},
    )
    assert result["ask_id"] == "ask-demo-1"
    assert result["dossier_id"] == "d_saved_demo"
    assert result["dossier_content_hash"] == client.body["content_hash"]
    assert [claim["claim_id"] for claim in client.body["claims"] if claim["kept"]] == ["c1"]
    assert result["handoff_receipt"]["source_run_id"] == "demo-run-1"
    assert result["handoff_receipt"]["scope_admission_manifest_sha256"] == persisted["scope_admission_manifest_sha256"]
    assert result["handoff_receipt"]["source_receipt_sha256"] == values["raw_receipt_sha256"]


def test_refuses_claim_ids_that_disagree_with_the_independent_w2_assessment():
    values = _case()
    values["admitted_claim_ids"] = ["c2"]
    native = _NativeAdapter()

    with pytest.raises(demo_saved_work.SelectionMismatch):
        _persist(values, native)

    assert native.calls == []


def test_refuses_claim_text_changed_from_raw_receipt_before_native_write():
    values = _case()
    values["safe_payload"]["claims"][0]["text"] = "Changed claim text."
    native = _NativeAdapter()

    with pytest.raises(demo_saved_work.SelectionMismatch):
        _persist(values, native)

    assert native.calls == []


def test_refuses_native_accounting_mismatch_before_returning_saved_ask():
    values = _case()
    native = _NativeAdapter(accounting_override={"released_usd": "2.025712"})

    with pytest.raises(demo_saved_work.AccountingMismatch):
        _persist(values, native)

    assert len(native.calls) == 1


def test_refuses_native_readback_hash_mismatch_before_returning_saved_ask():
    values = _case()
    native = _NativeAdapter(row_override={"ask_id": "ask-demo-1", "status": "complete"})

    with pytest.raises(demo_saved_work.AskReadbackMismatch):
        _persist(values, native)

    assert len(native.calls) == 1


def test_dossier_post_outcome_is_not_retried():
    values = _case()
    native = _NativeAdapter()
    persisted = _persist(values, native)
    client = _AppClient(native)
    client.fail_post = True

    with pytest.raises(demo_saved_work.DossierPostUncertain):
        _create(values, persisted, client)

    assert len(native.calls) == 1
    assert len(client.calls) == 1


def test_dossier_readback_hash_mismatch_is_reported_without_retry():
    values = _case()
    native = _NativeAdapter()
    persisted = _persist(values, native)
    client = _AppClient(native)
    client.change_readback_hash = True

    with pytest.raises(demo_saved_work.DossierReadbackMismatch):
        _create(values, persisted, client)

    assert [call[0] for call in client.calls] == ["POST", "GET"]
    assert len(native.calls) == 1


def test_dossier_is_refused_if_w7_selects_a_different_saved_attempt():
    values = _case()
    native = _NativeAdapter()
    persisted = _persist(values, native)
    client = _AppClient(native)
    values["selection"]["selected_attempt"] = 2

    with pytest.raises(demo_saved_work.SelectionMismatch):
        _create(values, persisted, client)

    assert client.calls == []
    assert len(native.calls) == 1


def test_persists_failed_noanswer_call_without_releasing_more_than_priced_cost():
    values = _case()
    values["selected_receipt"]["raw_answer"] = None
    values["app_record"]["answer"] = None
    values["app_record"]["status"] = "failed"
    values["app_record"]["error"] = {"error": "internal"}
    values["safe_payload"] = None
    values["admitted_claim_ids"] = []
    _refresh_receipt(values)
    values["app_record"]["run"]["model_usd"] = 0.25
    native = _NativeAdapter()

    result = _persist(values, native)

    saved_record = json.loads(native.last_row["record"])
    assert saved_record["status"] == "failed"
    assert saved_record["answer"] is None
    assert saved_record["run"]["model_usd"] == 0.25
    assert native.last_row["answer"] is None
    assert native.last_row["outcome"] == "internal"
    assert result["saved_view_status"] == "no_answer"
    assert result["recorded_model_usd"] == "0.25"
    assert result["reservation_release_usd"] == "0.25"


def test_unknown_failed_cost_keeps_the_reserved_hold_without_a_fake_zero_row():
    values = _case()
    values["selected_receipt"]["raw_answer"] = None
    values["app_record"]["answer"] = None
    values["app_record"]["status"] = "failed"
    values["app_record"]["error"] = {"error": "timeout"}
    values["app_record"]["run"]["model_usd"] = None
    values["safe_payload"] = None
    values["admitted_claim_ids"] = []
    _refresh_receipt(values)
    native = _NativeAdapter()

    with pytest.raises(demo_saved_work.AccountingMismatch, match="cost is unknown"):
        _persist(values, native)

    assert native.calls == []


def test_app_price_above_guard_is_refused_before_native_conversion():
    values = _case()
    values["guarded_charge_usd"] = "2.000000"
    native = _NativeAdapter()

    with pytest.raises(demo_saved_work.AccountingMismatch):
        _persist(values, native)

    assert native.calls == []


def test_dossier_post_is_refused_if_selector_view_differs_from_saved_view():
    values = _case()
    native = _NativeAdapter()
    persisted = _persist(values, native)
    client = _AppClient(native)
    values["selection"]["saved_payload"]["claims"][0]["text"] = "Changed after the Ask was saved."

    with pytest.raises(demo_saved_work.SelectionMismatch):
        _create(values, persisted, client)

    assert client.calls == []


def test_raw_receipt_manifest_hash_must_match_full_original_bytes_before_write():
    values = _case()
    values["raw_receipt_sha256"] = "sha256:" + "0" * 64
    native = _NativeAdapter()

    with pytest.raises(demo_saved_work.SelectionMismatch):
        _persist(values, native)

    assert native.calls == []


def test_selected_attempt_object_must_equal_the_parsed_full_receipt():
    values = _case()
    values["selected_receipt"]["run_id"] = "different-run"
    native = _NativeAdapter()

    with pytest.raises(demo_saved_work.SelectionMismatch):
        _persist(values, native)

    assert native.calls == []


def test_external_receipt_hash_metadata_must_match_exact_bytes():
    values = _case()
    values["selected_receipt"]["receipt_sha256"] = "sha256:" + "f" * 64
    native = _NativeAdapter()

    with pytest.raises(demo_saved_work.SelectionMismatch):
        _persist(values, native)

    assert native.calls == []


def test_external_receipt_hash_metadata_is_accepted_only_when_it_matches_exact_bytes():
    values = _case()
    values["selected_receipt"]["receipt_sha256"] = values["raw_receipt_sha256"]
    native = _NativeAdapter()

    persisted = _persist(values, native)

    assert persisted["source_receipt_sha256"] == values["raw_receipt_sha256"]
    assert len(native.calls) == 1


def test_fractional_micro_app_price_stays_exact_in_record_and_release():
    values = _case()
    values["app_record"]["run"]["model_usd"] = 2.0244805
    native = _NativeAdapter()
    persisted = _persist(values, native)

    saved_record = json.loads(native.last_row["record"])
    assert saved_record["run"]["model_usd"] == 2.0244805
    assert persisted["accounting"]["apppriced_usd"] == "2.0244805"
    assert persisted["accounting"]["released_usd"] == "2.0244805"
    assert persisted["accounting"]["retained_usd"] == "0.0012315"
    assert persisted["recorded_model_usd"] == "2.0244805"
    assert persisted["reservation_release_usd"] == "2.0244805"
    assert persisted["recorded_model_usd_ceiling_micros"] == 2_024_481
    assert persisted["reservation_release_ceiling_micros"] == 2_024_481

    result = _create(values, persisted, _AppClient(native))
    assert result["accounting"]["apppriced_usd"] == "2.0244805"
    assert result["accounting"]["released_usd"] == "2.0244805"


def test_record_readback_accepts_exact_integral_float_to_int_normalization():
    values = _case()
    values["app_record"]["run"]["credits"] = 0.0
    values["app_record"]["run"]["tokens"]["input"] = 0.0
    native = _NativeAdapter(row_transform=_integral_native_row)
    raw_answer_hash = values["selection"]["raw_hashes"]["1"]
    receipt_hash = values["raw_receipt_sha256"]

    persisted = _persist(values, native)
    result = _create(values, persisted, _AppClient(native))

    assert persisted["source_answer_sha256"] == raw_answer_hash
    assert persisted["source_receipt_sha256"] == receipt_hash
    assert persisted["record_sha256"]
    assert result["dossier_id"] == "d_saved_demo"


@pytest.mark.parametrize("readback_credit", [1, False])
def test_record_readback_rejects_unequal_values_and_boolean_numbers(readback_credit):
    values = _case()
    values["app_record"]["run"]["credits"] = 0.0
    native = _NativeAdapter(row_transform=_native_row_with_credit(readback_credit))

    with pytest.raises(demo_saved_work.AskReadbackMismatch):
        _persist(values, native)

    assert len(native.calls) == 1
