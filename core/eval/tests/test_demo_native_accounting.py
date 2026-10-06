import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import core.eval.demo_native_accounting as accounting
from core.eval.demo_native_accounting import (
    AccountingError,
    DemoNativeAccounting,
    UncertainWriteError,
    run_row_sha256,
)
from core.eval.demo_pairs import prepare_funding_transfer, source_proof_hash


DAY = date(2026, 10, 1)
SOURCE_DAY = date(2026, 9, 30)
SOURCE_HASH = "a" * 64
BASELINE = [
    "l3-ask-20260930-r3-NOW-01-spend-12638a48",
    "l3-ask-20260930-r3-NOW-01-retry-1-spend-3f5031e1",
    "l3-ask-20260930-r3-NOW-01-continuation-1-spend-5df280f9",
    "l3-ask-20260930-r3-RISE-02-spend-8e9d4c20",
    "l3-ask-20260930-r3-CRE-01-spend-6a21605f",
    "l3-ask-20260930-r3-SPR-03-spend-8a5f5498",
    "l3-ask-20260930-r3-WHY-03-spend-19e2f107",
]
BASELINE_USD = ["1.357150", "1.158263", "15.000000", "1.646701", "2.388517", "1.805746", "1.643623"]
SOURCE_ROWS = [
    {"run_id": run_id, "run_date": SOURCE_DAY.isoformat(), "model_usd_micros": int(Decimal(usd) * 1_000_000)}
    for run_id, usd in zip(BASELINE, BASELINE_USD)
]
SOURCE_HASH = source_proof_hash(SOURCE_ROWS)


def _decode(value):
    return json.loads(value) if isinstance(value, str) else value


def _model_usd(row):
    record = _decode(row.get("record")) or {}
    counts = _decode(row.get("counts")) or {}
    value = (record.get("run") or {}).get("model_usd")
    if value is None:
        value = counts.get("model_usd")
    return Decimal(str(value or 0))


def _date(value):
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


class FakeWarehouse:
    def __init__(self):
        self.rows = []
        self.query_calls = []
        self.execute_calls = []
        self.marker_keys = set()
        self.readback_limit = None
        self.execute_mode = None
        self.daily_usd = Decimal("4.793311")
        for run_id, usd in zip(BASELINE, BASELINE_USD):
            self.rows.append({
                "run_id": run_id, "stage": "understand_spend", "run_date": SOURCE_DAY,
                "status": "ok", "counts": {"model_usd": usd}, "record": None,
            })
        self.rows.append({
            "run_id": "existing-oct1-model-usage", "stage": "ask", "run_date": DAY,
            "status": "complete", "counts": None, "record": {"run": {"model_usd": "4.793311"}},
        })

    def claim_once(self, key):
        if key in self.marker_keys:
            return False
        self.marker_keys.add(key)
        return True

    def execute(self, sql, params):
        self.execute_calls.append((sql, params))
        assert sql.lstrip().startswith("INSERT INTO `ogilvy-trends-v2.intelligence_42_agent.runs`")
        assert "UNNEST(@rows_json)" in sql
        assert "PARSE_JSON" in sql
        expected_columns = [
            "run_id", "stage", "run_date", "status", "started_at", "finished_at", "counts", "error",
            "question", "tier", "plan", "calls", "credits", "tokens", "seconds", "outcome", "answer",
            "record", "model_usd",
        ]
        insert_columns = sql.split(")\nSELECT", 1)[0].split("(", 1)[1].replace("\n", "").split(",")
        assert [column.strip() for column in insert_columns] == expected_columns
        assert sql.upper().count("INSERT INTO") == 1
        assert all(word not in sql.upper() for word in ("UPDATE ", "DELETE ", "MERGE ", "TRUNCATE "))
        assert all(isinstance(item, str) for item in params["rows_json"])
        payloads = [json.loads(item) for item in params["rows_json"]]
        if self.execute_mode == "raise_before":
            raise TimeoutError("write outcome unknown")
        if self.execute_mode == "partial_then_raise":
            self.rows.append(self._stored(payloads[0]))
            raise TimeoutError("write outcome unknown")
        if self.execute_mode == "commit_then_raise":
            self.rows.extend(self._stored(row) for row in payloads)
            raise TimeoutError("write outcome unknown")
        self.rows.extend(self._stored(row) for row in payloads)

    @staticmethod
    def _stored(row):
        result = {key: row.get(key) for key in accounting.RUN_COLUMNS}
        result["run_date"] = date.fromisoformat(row["run_date"])
        result["started_at"] = result["started_at"] or "2026-10-01T08:00:00+00:00"
        result["finished_at"] = result["finished_at"] or "2026-10-01T08:00:00+00:00"
        for field in ("started_at", "finished_at"):
            timestamp = datetime.fromisoformat(result[field].replace("Z", "+00:00"))
            result[field] = timestamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return result

    def query(self, sql, params):
        self.query_calls.append((sql, params))
        if "JSON_VALUE(r.record, '$.ask_id') = @ask_id" in sql:
            rows = [row for row in self.rows if (_decode(row.get("record")) or {}).get("ask_id") == params["ask_id"]]
            return [{"row_json": json.dumps(row, default=self._json_default)} for row in rows]
        if "TO_JSON_STRING(r) AS row_json" in sql:
            ids = set(params["run_ids"])
            rows = [row for row in self.rows if row["run_id"] in ids]
            if self.readback_limit is not None and self.execute_calls:
                rows = rows[:self.readback_limit]
                self.readback_limit = None
            return [{"row_json": json.dumps(row, default=self._json_default)} for row in rows]
        if "COUNT(*) AS row_count" in sql:
            ids = set(params["run_ids"])
            rows = [row for row in self.rows if row["run_id"] in ids]
            return [{"row_count": len(rows), "usd": sum((_model_usd(row) for row in rows), Decimal(0))}]
        if "WHERE r.run_date = @day" in sql:
            rows = [row for row in self.rows if _date(row["run_date"]) == params["day"]]
            return [{"usd": sum((_model_usd(row) for row in rows), Decimal(0))}]
        raise AssertionError(f"unexpected query: {sql}")

    @staticmethod
    def _json_default(value):
        if isinstance(value, date):
            return value.isoformat()
        raise TypeError(type(value).__name__)


def _adapter(warehouse, cap=Decimal("40")):
    return DemoNativeAccounting(
        execute=warehouse.execute,
        query=warehouse.query,
        claim_once=warehouse.claim_once,
        daily_cap_for_day=lambda day: cap,
    )


def _transfer(warehouse, amount="5.000000"):
    return _adapter(warehouse).transfer_unspent(
        amount_usd=Decimal(amount), source_date=SOURCE_DAY, run_date=DAY,
        baseline_run_ids=BASELINE, source_proof_sha=SOURCE_HASH, native_net_micros=25_000_000,
    )


def _funding(warehouse):
    return _transfer(warehouse)["funding"]


def _ask_row(run_id="r_20261001_demo_001", amount="2.500000", day=DAY, numeric_model=False):
    model_usd = float(amount) if numeric_model else amount
    run = {"run_id": run_id, "model_usd": model_usd, "credits": 0, "tokens": 100}
    record = {"ask_id": f"a_{run_id}", "run": run, "claims": [], "evidence": []}
    answer = {"status": "partial", "claims": [], "gaps": []}
    return {
        "run_id": run_id, "stage": "ask", "run_date": day.isoformat(), "status": "complete",
        "started_at": "2026-10-01T10:00:00+02:00", "finished_at": "2026-10-01T10:00:03+02:00",
        "question": "What changed in the conversation?", "tier": "T1", "credits": 0.0, "seconds": 3.0,
        "outcome": "partial", "answer": json.dumps(answer), "record": json.dumps(record),
    }


def test_transfer_writes_two_parameterized_rows_and_proves_net_and_headroom(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    warehouse = FakeWarehouse()

    result = _transfer(warehouse, "1.123456")

    assert result["status"] == "stored"
    assert result["funding"]["verified"] is True
    assert result["funding"]["allocated_micros"] == 1_123_456
    assert result["funding"]["native_net_micros"] == 25_000_000
    assert result["native_net_usd"] == "25.000000"
    assert result["daily_spend_usd"] == "5.916767"
    assert result["daily_headroom_usd"] == "34.083233"
    assert len(warehouse.execute_calls) == 1
    rows = [json.loads(item) for item in warehouse.execute_calls[0][1]["rows_json"]]
    plan = prepare_funding_transfer(
        run_date=DAY, daily_headroom_micros=35_206_689, source_proof_sha=SOURCE_HASH,
        source_rows=SOURCE_ROWS, unknown_rows=[SOURCE_ROWS[4], SOURCE_ROWS[5]], amount_micros=1_123_456,
    )
    assert len(rows) == 2
    assert rows[0]["run_id"] == plan["correction_rows"][0]["run_id"]
    assert rows[0]["counts"]["model_usd"] == "-1.123456"
    assert rows[1]["run_id"] == plan["correction_rows"][1]["run_id"]
    assert rows[1]["counts"]["model_usd"] == "1.123456"
    assert all("model_usd" not in (row.get("record") or {}).get("run", {}) for row in rows)
    assert "COUNT(*) AS row_count" in "\n".join(sql for sql, _ in warehouse.query_calls)


def test_transfer_refuses_negative_out_of_source_range_and_non_micro_amounts(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    for amount in ("-0.000001", "12.974289", "1.0000001"):
        warehouse = FakeWarehouse()
        with pytest.raises(AccountingError):
            _transfer(warehouse, amount)
        assert not warehouse.execute_calls


def test_transfer_refuses_daily_cap_or_baseline_mismatch(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    too_tight = FakeWarehouse()
    with pytest.raises(AccountingError, match="cap"):
        _adapter(too_tight, Decimal("5")).transfer_unspent(
            amount_usd=Decimal("1"), source_date=SOURCE_DAY, run_date=DAY,
            baseline_run_ids=BASELINE, source_proof_sha=SOURCE_HASH, native_net_micros=25_000_000,
        )
    wrong_net = FakeWarehouse()
    with pytest.raises(AccountingError, match="net"):
        _adapter(wrong_net).transfer_unspent(
            amount_usd=Decimal("1"), source_date=SOURCE_DAY, run_date=DAY,
            baseline_run_ids=BASELINE, source_proof_sha=SOURCE_HASH, native_net_micros=24_000_000,
        )
    assert not too_tight.execute_calls and not wrong_net.execute_calls


def test_transfer_rejects_arbitrary_hash_and_wrong_per_row_amount_even_when_net_matches(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    bad_hash = FakeWarehouse()
    with pytest.raises(AccountingError, match="source proof hash"):
        _adapter(bad_hash).transfer_unspent(
            amount_usd=Decimal("1"), source_date=SOURCE_DAY, run_date=DAY,
            baseline_run_ids=BASELINE, source_proof_sha="b" * 64, native_net_micros=25_000_000,
        )
    changed = FakeWarehouse()
    changed.rows[0]["counts"]["model_usd"] = "1.357151"
    changed.rows[2]["counts"]["model_usd"] = "14.999999"
    with pytest.raises(AccountingError, match="approved"):
        _transfer(changed, "1.000000")
    changed_date = FakeWarehouse()
    changed_date.rows[0]["run_date"] = DAY
    with pytest.raises(AccountingError, match="date"):
        _transfer(changed_date, "1.000000")
    assert not bad_hash.execute_calls and not changed.execute_calls and not changed_date.execute_calls


def test_transfer_refuses_wrong_day_and_duplicate_existing_run_ids(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: date(2026, 10, 2))
    stale = FakeWarehouse()
    with pytest.raises(AccountingError, match="date"):
        _transfer(stale)
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    duplicate = FakeWarehouse()
    duplicate.rows.append(dict(duplicate.rows[0]))
    with pytest.raises(AccountingError, match="baseline"):
        _transfer(duplicate)
    assert not stale.execute_calls and not duplicate.execute_calls


def test_transfer_resumes_exact_pair_without_double_credit(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    warehouse = FakeWarehouse()
    first = _transfer(warehouse)
    second = _transfer(warehouse)

    assert first["status"] == "stored"
    assert second["status"] == "resumed"
    assert len(warehouse.execute_calls) == 1
    assert sum((_model_usd(row) for row in warehouse.rows if row["run_id"].startswith("l3-demo-20261001")), Decimal(0)) == 0


def test_uncertain_write_that_committed_is_read_back_without_retry(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    warehouse = FakeWarehouse()
    warehouse.execute_mode = "commit_then_raise"
    with pytest.raises(UncertainWriteError):
        _transfer(warehouse)
    warehouse.execute_mode = None

    resumed = _transfer(warehouse)

    assert resumed["status"] == "resumed"
    assert len(warehouse.execute_calls) == 1
    assert resumed["daily_spend_usd"] == "9.793311"


def test_uncertain_write_without_rows_cannot_auto_retry(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    warehouse = FakeWarehouse()
    warehouse.execute_mode = "raise_before"
    with pytest.raises(UncertainWriteError):
        _transfer(warehouse)
    warehouse.execute_mode = None

    with pytest.raises(AccountingError, match="marker"):
        _transfer(warehouse)
    assert len(warehouse.execute_calls) == 1


def test_partial_transfer_write_or_readback_stops_without_retry(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    partial = FakeWarehouse()
    partial.execute_mode = "partial_then_raise"
    with pytest.raises(UncertainWriteError):
        _transfer(partial)
    partial.execute_mode = None
    with pytest.raises(AccountingError, match="partial"):
        _transfer(partial)
    assert len(partial.execute_calls) == 1

    hidden = FakeWarehouse()
    hidden.readback_limit = 1
    with pytest.raises(AccountingError, match="readback"):
        _transfer(hidden)
    assert len(hidden.execute_calls) == 1


def test_conversion_app_row_and_signed_credit_are_atomic_and_resume(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    warehouse = FakeWarehouse()
    funding = _funding(warehouse)
    ask = _ask_row()

    stored = _adapter(warehouse).convert_prebooked_ask(
        real_ask_row=ask, funding=funding, guarded_charge=Decimal("3.000000"),
    )
    resumed = _adapter(warehouse).convert_prebooked_ask(
        real_ask_row=ask, funding=funding, guarded_charge=Decimal("3.000000"),
    )

    assert stored["status"] == "stored"
    assert resumed["status"] == "resumed"
    assert stored["ask_row"]["run_id"] == ask["run_id"]
    assert stored["ask_row_sha256"] == resumed["ask_row_sha256"]
    assert stored["ask_row_sha256"] == run_row_sha256(ask)
    assert stored["ask_row_sha256"] == run_row_sha256(stored["ask_row"])
    assert stored["accounting"] == {
        "apppriced_usd": "2.5", "guarded_usd": "3.000000", "available_before_usd": "5.000000",
        "released_usd": "2.5", "retained_usd": "0.5",
    }
    assert len(warehouse.execute_calls) == 2
    inserted = [json.loads(item) for item in warehouse.execute_calls[1][1]["rows_json"]]
    assert len(inserted) == 2
    app = next(row for row in inserted if row["stage"] == "ask")
    credit = next(row for row in inserted if row["stage"] == "understand_spend")
    assert _decode(app["record"])["run"]["model_usd"] == "2.500000"
    assert "model_usd" not in (_decode(app.get("counts")) or {})
    assert credit["counts"]["model_usd"] == "-2.5"
    assert credit["counts"]["retained_ceiling_micros"] == 500_000
    assert credit["counts"]["consumed_after_micros"] == 3_000_000
    assert credit["counts"]["source_run_id"] == funding["reservation_run_id"]
    assert credit["counts"]["source_raw_hash"] == SOURCE_HASH
    assert not ("record" in credit and (_decode(credit.get("record")) or {}).get("run", {}).get("model_usd") is not None)


def test_two_conversions_consume_guards_and_preserve_fractional_app_prices(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    warehouse = FakeWarehouse()
    funding = _funding(warehouse)
    first_ask = _ask_row(run_id="r_20261001_demo_101", amount="0.1234565", numeric_model=True)
    first = _adapter(warehouse).convert_prebooked_ask(
        real_ask_row=first_ask, funding=funding, guarded_charge=Decimal("3.000000"),
    )
    assert first["accounting"] == {
        "apppriced_usd": "0.1234565", "guarded_usd": "3.000000", "available_before_usd": "5.000000",
        "released_usd": "0.1234565", "retained_usd": "2.8765435",
    }
    first_rows = [json.loads(item) for item in warehouse.execute_calls[-1][1]["rows_json"]]
    first_app = next(row for row in first_rows if row["stage"] == "ask")
    first_credit = next(row for row in first_rows if row.get("counts") and row["counts"].get("what") == "demo_prebook_conversion")
    assert _decode(first_app["record"])["run"]["model_usd"] == 0.1234565
    assert first_credit["counts"]["model_usd"] == "-0.1234565"
    assert first_credit["counts"]["app_priced_micros"] == 123_457
    assert first_credit["counts"]["retained_ceiling_micros"] == 2_876_543
    assert first_credit["counts"]["consumed_after_micros"] == 3_000_000

    after_first = dict(funding, consumed_micros=3_000_000, app_run_ids=[first_ask["run_id"]])
    second_ask = _ask_row(run_id="r_20261001_demo_102", amount="1.0000005", numeric_model=True)
    second = _adapter(warehouse).convert_prebooked_ask(
        real_ask_row=second_ask, funding=after_first, guarded_charge=Decimal("2.000000"),
    )
    assert second["accounting"] == {
        "apppriced_usd": "1.0000005", "guarded_usd": "2.000000", "available_before_usd": "2.000000",
        "released_usd": "1.0000005", "retained_usd": "0.9999995",
    }
    second_rows = [json.loads(item) for item in warehouse.execute_calls[-1][1]["rows_json"]]
    second_credit = next(row for row in second_rows if row.get("counts") and row["counts"].get("what") == "demo_prebook_conversion")
    second_app = next(row for row in second_rows if row["stage"] == "ask")
    assert _decode(second_app["record"])["run"]["model_usd"] == 1.0000005
    assert second_credit["counts"]["model_usd"] == "-1.0000005"
    assert second_credit["counts"]["consumed_after_micros"] == 5_000_000

    after_second = dict(after_first, consumed_micros=5_000_000,
                        app_run_ids=[first_ask["run_id"], second_ask["run_id"]])
    resumed = _adapter(warehouse).convert_prebooked_ask(
        real_ask_row=second_ask, funding=after_second, guarded_charge=Decimal("2.000000"),
    )
    assert resumed["status"] == "resumed"
    assert len(warehouse.execute_calls) == 3


def test_conversion_refuses_a_different_run_with_the_same_app_ask_id(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    warehouse = FakeWarehouse()
    funding = _funding(warehouse)
    collision = _ask_row(run_id="r_older_attempt")
    collision_record = json.loads(collision["record"])
    collision_record["ask_id"] = json.loads(_ask_row()["record"])["ask_id"]
    collision["record"] = json.dumps(collision_record)
    warehouse.rows.append(FakeWarehouse._stored(collision))

    with pytest.raises(AccountingError, match="ask_id"):
        _adapter(warehouse).convert_prebooked_ask(
            real_ask_row=_ask_row(), funding=funding, guarded_charge=Decimal("3"),
        )

    assert len(warehouse.execute_calls) == 1


def test_run_row_hash_normalizes_integral_json_floats_only():
    submitted = _ask_row(numeric_model=True)
    submitted_record = json.loads(submitted["record"])
    submitted_record["run"]["credits"] = 0.0
    submitted_record["run"]["marker"] = True
    submitted["record"] = json.dumps(submitted_record)

    readback = json.loads(json.dumps(submitted))
    readback["credits"] = 0
    readback_record = json.loads(readback["record"])
    readback_record["run"]["credits"] = 0
    readback["record"] = json.dumps(readback_record)

    assert run_row_sha256(submitted) == run_row_sha256(readback)

    bool_changed = json.loads(json.dumps(readback))
    bool_record = json.loads(bool_changed["record"])
    bool_record["run"]["marker"] = 1
    bool_changed["record"] = json.dumps(bool_record)
    assert run_row_sha256(readback) != run_row_sha256(bool_changed)

    fractional_changed = json.loads(json.dumps(readback))
    fractional_record = json.loads(fractional_changed["record"])
    fractional_record["run"]["model_usd"] = 2.5000001
    fractional_changed["record"] = json.dumps(fractional_record)
    assert run_row_sha256(readback) != run_row_sha256(fractional_changed)


@pytest.mark.parametrize(
    ("app_amount", "guard", "consumed"),
    [("3.000000", "2.000000", 0), ("2.000000", "6.000000", 0), ("2.000000", "2.000000", 4_500_001)],
)
def test_conversion_refuses_amounts_outside_guard_or_remaining_phase(monkeypatch, app_amount, guard, consumed):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    warehouse = FakeWarehouse()
    funding = _funding(warehouse)
    funding["consumed_micros"] = consumed
    with pytest.raises(AccountingError):
        _adapter(warehouse).convert_prebooked_ask(
            real_ask_row=_ask_row(amount=app_amount), funding=funding, guarded_charge=Decimal(guard),
        )
    assert len(warehouse.execute_calls) == 1


def test_conversion_refuses_missing_actual_price_wrong_date_and_unverified_funding(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    warehouse = FakeWarehouse()
    funding = _funding(warehouse)
    missing = _ask_row()
    record = json.loads(missing["record"])
    del record["run"]["model_usd"]
    missing["record"] = json.dumps(record)
    with pytest.raises(AccountingError, match="model_usd"):
        _adapter(warehouse).convert_prebooked_ask(
            real_ask_row=missing, funding=funding, guarded_charge=Decimal("3"),
        )
    wrong_date = _ask_row(day=date(2026, 10, 2))
    with pytest.raises(AccountingError, match="date"):
        _adapter(warehouse).convert_prebooked_ask(
            real_ask_row=wrong_date, funding=funding, guarded_charge=Decimal("3"),
        )
    funding["verified"] = False
    with pytest.raises(AccountingError, match="verified"):
        _adapter(warehouse).convert_prebooked_ask(
            real_ask_row=_ask_row(), funding=funding, guarded_charge=Decimal("3"),
        )
    assert len(warehouse.execute_calls) == 1


def test_conversion_uncertain_commit_resumes_without_duplicate_charge(monkeypatch):
    monkeypatch.setattr(accounting, "_today_sast", lambda: DAY)
    warehouse = FakeWarehouse()
    funding = _funding(warehouse)
    ask = _ask_row()
    warehouse.execute_mode = "commit_then_raise"
    with pytest.raises(UncertainWriteError):
        _adapter(warehouse).convert_prebooked_ask(
            real_ask_row=ask, funding=funding, guarded_charge=Decimal("3"),
        )
    warehouse.execute_mode = None

    resumed = _adapter(warehouse).convert_prebooked_ask(
        real_ask_row=ask, funding=funding, guarded_charge=Decimal("3"),
    )

    assert resumed["status"] == "resumed"
    assert len(warehouse.execute_calls) == 2
    assert sum((_model_usd(row) for row in warehouse.rows if row["run_id"] == ask["run_id"]), Decimal(0)) == Decimal("2.5")
