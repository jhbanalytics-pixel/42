"""Offline adapter for append only demo accounting in the native runs table.

The injected execute callback must run one query job to completion and raise if it cannot confirm job completion.
The caller must serialize all writers and supply a persistent exclusive claim_once marker. BigQuery does not enforce
run_id uniqueness. A timeout after dispatch is uncertain, so this adapter never retries a write.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP


PROJECT = "ogilvy-trends-v2"
DATASET = "intelligence_42_agent"
TABLE = f"`{PROJECT}.{DATASET}.runs`"
PHASE_ID = "l3-demo-20261001"
MAX_ALLOCATED_MICROS = 12_974_288
EXPECTED_NATIVE_NET_MICROS = 25_000_000
MICROS_PER_USD = Decimal(1_000_000)
SAST = timezone(timedelta(hours=2))

RUN_COLUMNS = (
    "run_id", "stage", "run_date", "status", "started_at", "finished_at", "counts", "error", "question",
    "tier", "plan", "calls", "credits", "tokens", "seconds", "outcome", "answer", "record", "model_usd",
)
_JSON_COLUMNS = frozenset(("counts", "plan", "answer", "record"))
_TIME_COLUMNS = frozenset(("started_at", "finished_at"))
_NUMERIC_COLUMNS = frozenset(("credits", "seconds", "model_usd"))
_SOURCE_HASH = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


INSERT_ROWS_SQL = f"""
INSERT INTO {TABLE} ({", ".join(RUN_COLUMNS)})
SELECT
  JSON_VALUE(row_json, '$.run_id'),
  JSON_VALUE(row_json, '$.stage'),
  SAFE.PARSE_DATE('%F', JSON_VALUE(row_json, '$.run_date')),
  JSON_VALUE(row_json, '$.status'),
  COALESCE(SAFE_CAST(JSON_VALUE(row_json, '$.started_at') AS TIMESTAMP), CURRENT_TIMESTAMP()),
  COALESCE(SAFE_CAST(JSON_VALUE(row_json, '$.finished_at') AS TIMESTAMP), CURRENT_TIMESTAMP()),
  PARSE_JSON(NULLIF(JSON_QUERY(row_json, '$.counts'), 'null')),
  JSON_VALUE(row_json, '$.error'),
  JSON_VALUE(row_json, '$.question'),
  JSON_VALUE(row_json, '$.tier'),
  PARSE_JSON(NULLIF(JSON_QUERY(row_json, '$.plan'), 'null')),
  SAFE_CAST(JSON_VALUE(row_json, '$.calls') AS INT64),
  SAFE_CAST(JSON_VALUE(row_json, '$.credits') AS FLOAT64),
  SAFE_CAST(JSON_VALUE(row_json, '$.tokens') AS INT64),
  SAFE_CAST(JSON_VALUE(row_json, '$.seconds') AS FLOAT64),
  JSON_VALUE(row_json, '$.outcome'),
  PARSE_JSON(NULLIF(JSON_QUERY(row_json, '$.answer'), 'null')),
  PARSE_JSON(NULLIF(JSON_QUERY(row_json, '$.record'), 'null')),
  SAFE_CAST(JSON_VALUE(row_json, '$.model_usd') AS FLOAT64)
FROM UNNEST(@rows_json) AS row_json
"""

EXACT_ROWS_SQL = f"""
SELECT TO_JSON_STRING(r) AS row_json
FROM {TABLE} AS r
WHERE r.run_id IN UNNEST(@run_ids)
ORDER BY r.run_id
"""

ASK_ID_ROWS_SQL = f"""
SELECT TO_JSON_STRING(r) AS row_json
FROM {TABLE} AS r
WHERE JSON_VALUE(r.record, '$.ask_id') = @ask_id
ORDER BY r.run_id
"""

NET_SQL = f"""
SELECT COUNT(*) AS row_count,
       COALESCE(SUM(CAST(COALESCE(JSON_VALUE(r.record, '$.run.model_usd'),
                                  JSON_VALUE(r.counts, '$.model_usd')) AS BIGNUMERIC)), CAST(0 AS BIGNUMERIC)) AS usd
FROM {TABLE} AS r
WHERE r.run_id IN UNNEST(@run_ids)
"""

DAILY_SPEND_SQL = f"""
SELECT COALESCE(SUM(CAST(COALESCE(JSON_VALUE(r.record, '$.run.model_usd'),
                                  JSON_VALUE(r.counts, '$.model_usd')) AS BIGNUMERIC)), CAST(0 AS BIGNUMERIC)) AS usd
FROM {TABLE} AS r
WHERE r.run_date = @day
"""


class AccountingError(RuntimeError):
    pass


class UncertainWriteError(AccountingError):
    pass


class DemoNativeAccounting:
    def __init__(self, *, execute: Callable, query: Callable, claim_once: Callable,
                 daily_cap_for_day: Callable[[date], object]):
        if not all(callable(value) for value in (execute, query, claim_once, daily_cap_for_day)):
            raise TypeError("execute, query, claim_once, and daily_cap_for_day must be callable")
        self._execute = execute
        self._query = query
        self._claim_once = claim_once
        self._daily_cap_for_day = daily_cap_for_day

    def transfer_unspent(self, *, amount_usd, source_date, run_date, baseline_run_ids,
                         source_proof_sha, native_net_micros):
        day = _as_date(run_date, "run_date")
        source_day = _as_date(source_date, "source_date")
        approved_ids, approved_source_day, approved_day, approved_net, max_amount = _source_contract()
        if source_day != approved_source_day or day != approved_day:
            raise AccountingError("funding transfer dates do not match the approved source window")
        amount = _input_micros(amount_usd, "amount_usd")
        if amount <= 0 or amount > max_amount:
            raise AccountingError("transfer exceeds the verified known unused source amount")
        baseline = _run_ids(baseline_run_ids, "baseline_run_ids", expected=7)
        if tuple(baseline) != tuple(approved_ids):
            raise AccountingError("baseline_run_ids do not match the seven approved R3 rows")
        proof_hash = _proof_hash(source_proof_sha)
        expected_net = _integer(native_net_micros, "native_net_micros")
        if expected_net != approved_net:
            raise AccountingError("R3 baseline net does not match the funded source proof")

        debit_id, reservation_id = _transfer_run_ids(day, amount, proof_hash)
        _no_id_collisions([*baseline, debit_id, reservation_id])
        debit, reservation = _transfer_rows(amount, source_day, day, proof_hash, debit_id, reservation_id)
        existing = self._fetch_rows([debit_id, reservation_id])
        if existing:
            _verify_pair(existing, [debit, reservation], "transfer")
            return self._transfer_proof(
                status="resumed", baseline=baseline, debit=debit, reservation=reservation,
                expected_net=expected_net, day=day, amount=amount, readback=existing,
            )

        _, recomputed_source_hash = _approved_source_rows(self._fetch_rows(baseline))
        if recomputed_source_hash != proof_hash:
            raise AccountingError("source proof hash does not match the exact approved R3 rows")
        self._verify_net(baseline, expected_net, expected_count=len(baseline), label="baseline")
        before, cap = self._daily_state(day)
        if before + amount > cap:
            raise AccountingError("funding transfer would exceed the fresh daily model cap")
        _require_runtime_day(day)
        self._claim(f"{PHASE_ID}:transfer")
        self._write([debit, reservation])
        try:
            readback = self._fetch_rows([debit_id, reservation_id])
            _verify_pair(readback, [debit, reservation], "transfer readback")
            result = self._transfer_proof(
                status="stored", baseline=baseline, debit=debit, reservation=reservation,
                expected_net=expected_net, day=day, amount=amount, before=before, readback=readback,
            )
        except UncertainWriteError:
            raise
        except Exception as exc:
            raise UncertainWriteError("transfer was dispatched but its complete readback is unproven") from exc
        return result

    def convert_prebooked_ask(self, *, real_ask_row, funding, guarded_charge):
        state = _funding_state(funding)
        day = _as_date(state["run_date"], "funding.run_date")
        _require_runtime_day(day)
        ask = _normalise_ask_row(real_ask_row, day)
        app_price = _row_model_usd(ask)
        app_guard_micros = _ceil_micros(app_price, "record.run.model_usd")
        guard_micros = _input_micros(guarded_charge, "guarded_charge")
        if app_price < 0 or guard_micros < 0:
            raise AccountingError("Ask price and guarded charge must be nonnegative")
        ask_id = ask["run_id"]
        app_ask_id = ask["record"].get("ask_id")
        if not isinstance(app_ask_id, str) or not app_ask_id:
            raise AccountingError("real Ask record.ask_id is required for collision checks")
        credit_id = _credit_run_id(ask_id)
        if ask_id in (state["reservation_run_id"], *state["baseline_run_ids"]):
            raise AccountingError("Ask run_id collides with the funding manifest")

        existing_current = self._fetch_rows([ask_id, credit_id])
        current_present = bool(existing_current)
        if current_present and len(existing_current) != 2:
            raise AccountingError("partial Ask conversion rows already exist; no write or retry is allowed")
        ask_id_rows = self._fetch_ask_id_rows(app_ask_id)
        if len(ask_id_rows) > 1 or (ask_id_rows and ask_id_rows[0]["run_id"] != ask_id):
            raise AccountingError("app record.ask_id already belongs to another runs row")
        if ask_id_rows:
            _verify_run_row(ask_id_rows[0], ask, "Ask record.ask_id")
        elif current_present:
            raise AccountingError("exact Ask row is missing from the app record.ask_id readback")

        _, consumed, net_ids = self._phase_readback(state)
        current_listed = ask_id in state["app_run_ids"]
        if current_present and not current_listed:
            if state["consumed_micros"] != consumed:
                raise AccountingError("funding consumed_micros disagrees with fresh prior conversion rows")
            available_before = state["allocated_micros"] - consumed
            _verify_conversion_pair(
                existing_current, ask, state, app_price, app_guard_micros, guard_micros, available_before
            )
            consumed += guard_micros
            net_ids.extend([ask_id, credit_id])
        elif current_listed:
            if not current_present:
                raise AccountingError("funding lists an Ask whose exact conversion pair is missing")
            current_credit = next(row for row in existing_current if row["run_id"] == credit_id)
            available_before = _integer(_counts(current_credit).get("available_before_micros"), "available_before_micros")
            _verify_conversion_pair(
                existing_current, ask, state, app_price, app_guard_micros, guard_micros, available_before
            )
            if state["consumed_micros"] != consumed:
                raise AccountingError("funding consumed_micros disagrees with fresh conversion rows")
        else:
            if state["consumed_micros"] != consumed:
                raise AccountingError("funding consumed_micros disagrees with fresh prior conversion rows")
            available_before = state["allocated_micros"] - consumed
            if app_guard_micros > guard_micros or guard_micros > available_before:
                raise AccountingError("Ask price, guarded charge, or remaining phase amount is out of bounds")

        if available_before < 0 or consumed > state["allocated_micros"]:
            raise AccountingError("phase accounting exceeds its verified allocation")
        credit = _conversion_credit_row(
            ask_id=ask_id, credit_id=credit_id, day=day, state=state, app_price=app_price,
            app_guard_micros=app_guard_micros, guard_micros=guard_micros,
            available_before=available_before, ask_row=ask,
        )
        if current_present:
            expected_pair = [ask, credit]
            _verify_pair(existing_current, expected_pair, "conversion")
            status = "resumed"
        else:
            before, cap = self._daily_state(day)
            if before > cap:
                raise AccountingError("current daily model spend already exceeds the fresh cap")
            _require_runtime_day(day)
            self._claim(f"{PHASE_ID}:convert:{hashlib.sha256(ask_id.encode('utf-8')).hexdigest()}")
            self._write([ask, credit])
            status = "stored"
            try:
                current = self._fetch_rows([ask_id, credit_id])
                _verify_pair(current, [ask, credit], "conversion readback")
                self._verify_exact_net([ask_id, credit_id], Decimal(0), expected_count=2, label="conversion pair")
                ask_id_rows = self._fetch_ask_id_rows(app_ask_id)
                if len(ask_id_rows) != 1 or ask_id_rows[0]["run_id"] != ask_id:
                    raise AccountingError("Ask record.ask_id readback is missing or duplicated")
                _verify_run_row(ask_id_rows[0], ask, "Ask record.ask_id readback")
                after, after_cap = self._daily_state(day)
                if after > after_cap:
                    raise AccountingError("daily spend exceeds the fresh cap after conversion")
            except Exception as exc:
                raise UncertainWriteError("Ask conversion was dispatched but its readback is unproven") from exc
            existing_current = current
            net_ids.extend([ask_id, credit_id])
            consumed += guard_micros

        expected_consumed = state["consumed_micros"] + (guard_micros if not current_listed else 0)
        if consumed != expected_consumed:
            raise AccountingError("fresh conversion rows do not reproduce phase consumption")
        manifest = _unique_ids(net_ids)
        self._verify_net(manifest, state["native_net_micros"], expected_count=len(manifest), label="R3")
        if status == "resumed":
            self._verify_exact_net([ask_id, credit_id], Decimal(0), expected_count=2, label="conversion pair")
            current_spend, current_cap = self._daily_state(day)
            if current_spend > current_cap:
                raise AccountingError("current daily model spend exceeds the fresh cap")
        fresh_ask = next(row for row in ask_id_rows if row["run_id"] == ask_id)
        _verify_run_row(fresh_ask, ask, "Ask")
        return {
            "status": status,
            "ask_row": fresh_ask,
            "ask_row_sha256": _ask_row_hash(fresh_ask),
            "accounting": {
                "apppriced_usd": _usd_exact(app_price),
                "guarded_usd": _usd(guard_micros),
                "available_before_usd": _usd(available_before),
                "released_usd": _usd_exact(app_price),
                "retained_usd": _usd_exact(Decimal(guard_micros) / MICROS_PER_USD - app_price),
            },
        }

    def _transfer_proof(self, *, status, baseline, debit, reservation, expected_net, day, amount, readback,
                        before=None):
        _, source_hash = _approved_source_rows(self._fetch_rows(baseline))
        if source_hash != _counts(reservation).get("source_proof_sha"):
            raise AccountingError("transfer source proof no longer matches the approved R3 rows")
        manifest = [*baseline, debit["run_id"], reservation["run_id"]]
        self._verify_net(manifest, expected_net, expected_count=len(manifest), label="R3")
        self._verify_exact_net([debit["run_id"], reservation["run_id"]], Decimal(0),
                               expected_count=2, label="transfer pair")
        daily, cap = self._daily_state(day)
        if daily > cap:
            raise AccountingError("daily model spend exceeds the fresh cap after transfer")
        funding = {
            "schema_version": "demo-funding-v1",
            "phase_id": PHASE_ID,
            "run_date": day.isoformat(),
            "reservation_run_id": reservation["run_id"],
            "allocated_micros": amount,
            "consumed_micros": 0,
            "source_proof_sha": _counts(reservation)["source_proof_sha"],
            "native_net_micros": expected_net,
            "baseline_run_ids": list(baseline),
            "app_run_ids": [],
            "verified": True,
        }
        return {
            "status": status,
            "funding": funding,
            "native_net_usd": _usd(expected_net),
            "daily_spend_usd": _usd(daily),
            "daily_headroom_usd": _usd(cap - daily),
            "rows": [next(row for row in readback if row["run_id"] == expected["run_id"])
                     for expected in (debit, reservation)],
        }

    def _phase_readback(self, state):
        debit_id, expected_reservation_id = _transfer_run_ids(
            _as_date(state["run_date"], "run_date"), state["allocated_micros"], state["source_proof_sha"]
        )
        reservation_id = state["reservation_run_id"]
        if reservation_id != expected_reservation_id:
            raise AccountingError("funding reservation run_id does not match its deterministic transfer")
        _, source_day, funding_day, _, _ = _source_contract()
        expected_debit, expected_reservation = _transfer_rows(
            state["allocated_micros"], source_day, funding_day, state["source_proof_sha"], debit_id,
            reservation_id,
        )
        phase_ids = [*state["baseline_run_ids"], debit_id, reservation_id]
        app_ids = list(state["app_run_ids"])
        for app_id in app_ids:
            phase_ids.extend([app_id, _credit_run_id(app_id)])
        phase_ids = _unique_ids(phase_ids)
        rows = self._fetch_rows(phase_ids)
        by_id = _index_exact(rows, phase_ids, "phase")
        _, recomputed_source_hash = _approved_source_rows(
            [by_id[run_id] for run_id in state["baseline_run_ids"]]
        )
        if recomputed_source_hash != state["source_proof_sha"]:
            raise AccountingError("funding source hash does not match the approved R3 readback")
        _verify_pair([by_id[debit_id], by_id[reservation_id]], [expected_debit, expected_reservation], "funding")
        if _counts(by_id[reservation_id]).get("phase_id") != PHASE_ID:
            raise AccountingError("reservation phase metadata does not match")
        reservation_counts = _counts(by_id[reservation_id])
        if reservation_counts.get("source_proof_sha") != state["source_proof_sha"]:
            raise AccountingError("reservation source proof does not match")
        if _integer(reservation_counts.get("allocated_micros"), "reservation allocated_micros") != state["allocated_micros"]:
            raise AccountingError("reservation amount does not match the verified funding manifest")
        if _counts(by_id[debit_id]).get("source_proof_sha") != state["source_proof_sha"]:
            raise AccountingError("debit source proof does not match")
        consumed = 0
        for app_id in state["app_run_ids"]:
            pair = [by_id[app_id], by_id[_credit_run_id(app_id)]]
            app_price = _row_model_usd(pair[0])
            app_guard_micros = _ceil_micros(app_price, "record.run.model_usd")
            credit_counts = _counts(pair[1])
            if _integer(credit_counts.get("app_priced_micros"), "app_priced_micros") != app_guard_micros:
                raise AccountingError("prior Ask price disagrees with its conversion credit")
            guard_micros = _integer(credit_counts.get("guarded_charge_micros"), "guarded_charge_micros")
            _verify_conversion_metadata(
                pair[0], pair[1], state, app_price, app_guard_micros, state["allocated_micros"] - consumed,
                guard_micros=guard_micros, expected_consumed_after=consumed + guard_micros,
            )
            consumed += guard_micros
        self._verify_net(phase_ids, state["native_net_micros"], expected_count=len(phase_ids), label="R3")
        return by_id, consumed, phase_ids

    def _daily_state(self, day):
        cap = _micros_from_input(self._daily_cap_for_day(day), "daily cap", allow_float=True)
        if cap <= 0:
            raise AccountingError("daily model cap must be positive")
        rows = list(self._query(DAILY_SPEND_SQL, {"day": day}) or [])
        if len(rows) != 1:
            raise AccountingError("daily spend query did not return exactly one aggregate row")
        used = _ceil_micros(_decimal_from_query(_field(rows[0], "usd"), "daily spend"), "daily spend")
        return used, cap

    def _verify_net(self, run_ids, expected_micros, *, expected_count, label):
        ids = _unique_ids(run_ids)
        rows = list(self._query(NET_SQL, {"run_ids": ids}) or [])
        if len(rows) != 1:
            raise AccountingError(f"{label} net query did not return exactly one aggregate row")
        count = _integer(_field(rows[0], "row_count"), f"{label} row_count")
        net_value = _decimal_from_query(_field(rows[0], "usd"), f"{label} net")
        net = int((net_value * MICROS_PER_USD).to_integral_value(rounding=ROUND_HALF_UP))
        if count != expected_count:
            raise AccountingError(f"{label} row count mismatch, expected {expected_count}, read {count}")
        if net != expected_micros:
            raise AccountingError(f"{label} native net mismatch, expected {_usd(expected_micros)}, read {_usd(net)}")
        return net

    def _verify_exact_net(self, run_ids, expected_usd, *, expected_count, label):
        ids = _unique_ids(run_ids)
        rows = list(self._query(NET_SQL, {"run_ids": ids}) or [])
        if len(rows) != 1:
            raise AccountingError(f"{label} query did not return exactly one aggregate row")
        count = _integer(_field(rows[0], "row_count"), f"{label} row_count")
        net = _decimal_from_query(_field(rows[0], "usd"), f"{label} net")
        if count != expected_count or net != expected_usd:
            raise AccountingError(f"{label} did not reconcile exactly to {expected_usd}")
        return net

    def _fetch_rows(self, run_ids):
        ids = _unique_ids(run_ids)
        rows = list(self._query(EXACT_ROWS_SQL, {"run_ids": ids}) or [])
        result = []
        for row in rows:
            raw = _field(row, "row_json")
            try:
                decoded = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, json.JSONDecodeError) as exc:
                raise AccountingError("exact row readback was not valid JSON") from exc
            if not isinstance(decoded, dict) or not isinstance(decoded.get("run_id"), str):
                raise AccountingError("exact row readback is missing its run_id")
            result.append(decoded)
        return result

    def _fetch_ask_id_rows(self, ask_id):
        rows = list(self._query(ASK_ID_ROWS_SQL, {"ask_id": ask_id}) or [])
        result = []
        for row in rows:
            raw = _field(row, "row_json")
            try:
                decoded = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, json.JSONDecodeError) as exc:
                raise AccountingError("ask_id readback was not valid JSON") from exc
            if not isinstance(decoded, dict) or not isinstance(decoded.get("run_id"), str):
                raise AccountingError("ask_id readback is missing its run_id")
            result.append(decoded)
        return result

    def _claim(self, key):
        try:
            claimed = self._claim_once(key)
        except Exception as exc:
            raise AccountingError("exclusive execution marker could not be claimed") from exc
        if claimed is not True:
            raise AccountingError("exclusive execution marker was already claimed; write was not retried")

    def _write(self, rows):
        payloads = [json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False) for row in rows]
        try:
            self._execute(INSERT_ROWS_SQL, {"rows_json": payloads})
        except Exception as exc:
            raise UncertainWriteError("native insert outcome is uncertain; this adapter will not retry") from exc


def _transfer_rows(amount, source_day, day, proof_hash, debit_id, reservation_id):
    amount_usd = _usd(amount)
    common = {
        "status": "ok", "started_at": None, "finished_at": None, "counts": None, "record": None,
    }
    debit_counts = {
        "model_usd": _usd(-amount), "what": "demo_unused_transfer_debit", "phase_id": PHASE_ID,
        "source_proof_sha": proof_hash, "allocated_micros": amount,
    }
    reservation_counts = {
        "model_usd": amount_usd, "what": "demo_unused_transfer_reservation", "phase_id": PHASE_ID,
        "source_proof_sha": proof_hash, "allocated_micros": amount,
    }
    debit = {**common, "run_id": debit_id, "stage": "understand_spend",
             "run_date": source_day.isoformat(), "counts": debit_counts}
    reservation = {**common, "run_id": reservation_id, "stage": "understand_spend",
                   "run_date": day.isoformat(), "counts": reservation_counts}
    return debit, reservation


def _conversion_credit_row(*, ask_id, credit_id, day, state, app_price, app_guard_micros, guard_micros,
                           available_before, ask_row):
    counts = {
        "model_usd": _usd_exact(-app_price), "what": "demo_prebook_conversion", "phase_id": PHASE_ID,
        "source_run_id": state["reservation_run_id"], "source_raw_hash": state["source_proof_sha"],
        "app_run_id": ask_id, "app_priced_usd": _usd_exact(app_price), "app_priced_micros": app_guard_micros,
        "guarded_charge_micros": guard_micros, "retained_ceiling_micros": guard_micros - app_guard_micros,
        "available_before_micros": available_before,
        "consumed_after_micros": state["allocated_micros"] - available_before + guard_micros,
    }
    return {
        "run_id": credit_id, "stage": "understand_spend", "run_date": day.isoformat(), "status": "ok",
        "started_at": ask_row["started_at"], "finished_at": ask_row["finished_at"], "counts": counts, "record": None,
    }


def _normalise_ask_row(value, day):
    if not isinstance(value, Mapping):
        raise AccountingError("real_ask_row must be a mapping from the app sink")
    row = {column: value.get(column) for column in RUN_COLUMNS}
    row["run_id"] = _valid_run_id(value.get("run_id"), "Ask run_id")
    if value.get("stage") != "ask":
        raise AccountingError("real_ask_row must have stage ask")
    if _as_date(value.get("run_date"), "Ask run_date") != day:
        raise AccountingError("Ask row date does not match the funded accounting day")
    row["run_date"] = day.isoformat()
    if not isinstance(value.get("status"), str) or not value["status"]:
        raise AccountingError("real Ask status is required")
    record = _json_object(value.get("record"), "Ask record")
    run = record.get("run")
    if not isinstance(run, Mapping) or run.get("model_usd") is None:
        raise AccountingError("real Ask record.run.model_usd is required")
    if run.get("run_id") != row["run_id"]:
        raise AccountingError("real Ask record.run.run_id must match the app sink run_id")
    if row["model_usd"] is not None:
        raise AccountingError("real Ask model spend belongs in record.run.model_usd only")
    counts = _json_object(value.get("counts"), "Ask counts", allow_none=True)
    if counts and "model_usd" in counts:
        raise AccountingError("Ask model spend belongs in record.run.model_usd only")
    row["record"] = record
    row["counts"] = counts or None
    answer_value = value.get("answer")
    row["answer"] = _json_value(answer_value, "Ask answer", allow_none=True)
    row["plan"] = _json_value(value.get("plan"), "Ask plan", allow_none=True)
    for name in ("calls", "tokens"):
        if row[name] is not None and (isinstance(row[name], bool) or not isinstance(row[name], int)):
            raise AccountingError(f"Ask {name} must be an INT64")
    for name in ("credits", "seconds"):
        if row[name] is not None and (isinstance(row[name], bool) or not isinstance(row[name], (int, float, Decimal))):
            raise AccountingError(f"Ask {name} must match the runs table numeric type")
        if isinstance(row[name], float) and not math.isfinite(row[name]):
            raise AccountingError(f"Ask {name} must be finite")
    if row["started_at"] is None or row["finished_at"] is None:
        raise AccountingError("real Ask timestamps are required")
    _canonical_time(row["started_at"])
    _canonical_time(row["finished_at"])
    if _row_model_usd(row) < 0:
        raise AccountingError("real Ask model spend must be nonnegative")
    return row


def _verify_pair(actual_rows, expected_rows, label):
    expected_ids = [row["run_id"] for row in expected_rows]
    by_id = _index_exact(actual_rows, expected_ids, label)
    for expected in expected_rows:
        _verify_run_row(by_id[expected["run_id"]], expected, label)
    return by_id


def _index_exact(rows, expected_ids, label):
    expected = set(expected_ids)
    by_id = {}
    for row in rows:
        run_id = row.get("run_id")
        if run_id not in expected or run_id in by_id:
            raise AccountingError(f"{label} readback has an unexpected or duplicate run_id")
        by_id[run_id] = row
    if set(by_id) != expected:
        raise AccountingError(f"{label} readback is partial; expected exactly one row per run_id")
    return by_id


def _verify_run_row(actual, expected, label):
    actual_canonical = _canonical_row(actual)
    expected_canonical = _canonical_row(_expected_row(expected))
    for field in _TIME_COLUMNS:
        if expected.get(field) is None:
            if actual.get(field) is None:
                raise AccountingError(f"{label} row is missing generated {field}")
            actual_canonical.pop(field, None)
            expected_canonical.pop(field, None)
    if actual_canonical != expected_canonical:
        raise AccountingError(f"{label} row content does not match the submitted row")


def _expected_row(row):
    expected = {column: row.get(column) for column in RUN_COLUMNS}
    expected["run_date"] = _as_date(expected["run_date"], "expected run_date").isoformat()
    for field in _JSON_COLUMNS:
        if expected[field] is not None:
            expected[field] = _json_value(expected[field], field)
    return expected


def _verify_conversion_pair(rows, ask, state, app_price, app_guard_micros, guard_micros, available_before):
    ask_id = ask["run_id"]
    credit_id = _credit_run_id(ask_id)
    credit = _verify_pair(rows, [ask, _conversion_credit_row(
        ask_id=ask_id, credit_id=credit_id, day=_as_date(state["run_date"], "run_date"), state=state,
        app_price=app_price, app_guard_micros=app_guard_micros, guard_micros=guard_micros,
        available_before=available_before, ask_row=ask,
    )], "conversion")[credit_id]
    _verify_conversion_metadata(
        ask, credit, state, app_price, app_guard_micros, available_before, guard_micros=guard_micros,
        expected_consumed_after=state["allocated_micros"] - available_before + guard_micros,
    )


def _verify_conversion_metadata(ask, credit, state, app_price, app_guard_micros, available_before, guard_micros=None,
                                expected_consumed_after=None):
    counts = _counts(credit)
    guard = _integer(counts.get("guarded_charge_micros"), "guarded_charge_micros")
    if guard_micros is not None and guard != guard_micros:
        raise AccountingError("stored guarded charge does not match the requested guard")
    if app_guard_micros > guard or guard > available_before:
        raise AccountingError("stored conversion exceeds its guarded or available phase amount")
    if "model_usd" not in counts or _decimal_amount(counts["model_usd"], "credit model_usd") != -app_price:
        raise AccountingError("signed credit does not equal the actual Ask price")
    if _decimal_amount(counts.get("app_priced_usd"), "app_priced_usd") != app_price:
        raise AccountingError("stored exact app price metadata does not match the Ask row")
    if _integer(counts.get("app_priced_micros"), "app_priced_micros") != app_guard_micros:
        raise AccountingError("stored app price metadata does not match the Ask row")
    if _integer(counts.get("retained_ceiling_micros"), "retained_ceiling_micros") != guard - app_guard_micros:
        raise AccountingError("stored retained ceiling does not equal guarded charge less app price")
    if _integer(counts.get("available_before_micros"), "available_before_micros") != available_before:
        raise AccountingError("stored available phase amount does not match recomputed balance")
    consumed_after = _integer(counts.get("consumed_after_micros"), "consumed_after_micros")
    if expected_consumed_after is not None and consumed_after != expected_consumed_after:
        raise AccountingError("stored consumed amount does not match the recomputed phase balance")
    if counts.get("phase_id") != PHASE_ID or counts.get("source_run_id") != state["reservation_run_id"]:
        raise AccountingError("conversion source metadata does not match the funding reservation")
    if counts.get("source_raw_hash") != state["source_proof_sha"] or counts.get("app_run_id") != ask["run_id"]:
        raise AccountingError("conversion proof metadata does not match the real Ask")
    if counts.get("what") != "demo_prebook_conversion":
        raise AccountingError("conversion row type metadata is invalid")
    if _row_model_usd(ask) != app_price:
        raise AccountingError("Ask row spend does not match its conversion credit")


def _funding_state(funding):
    if not isinstance(funding, Mapping):
        raise AccountingError("funding must be the verified demo-funding-v1 mapping")
    if funding.get("verified") is not True:
        raise AccountingError("funding is not verified by exact native readbacks")
    if funding.get("schema_version") != "demo-funding-v1" or funding.get("phase_id") != PHASE_ID:
        raise AccountingError("funding schema or phase_id does not match this demo")
    day = _as_date(funding.get("run_date"), "funding.run_date")
    reservation_id = _valid_run_id(funding.get("reservation_run_id"), "reservation_run_id")
    allocated = _integer(funding.get("allocated_micros"), "allocated_micros")
    consumed = _integer(funding.get("consumed_micros"), "consumed_micros")
    approved_ids, _, approved_day, _, max_amount = _source_contract()
    if day != approved_day:
        raise AccountingError("funding date does not match the approved demo day")
    if allocated <= 0 or allocated > max_amount or consumed < 0 or consumed > allocated:
        raise AccountingError("funding allocation or consumption is outside the verified source bound")
    native_net = _integer(funding.get("native_net_micros"), "native_net_micros")
    if native_net != EXPECTED_NATIVE_NET_MICROS:
        raise AccountingError("funding native net does not match the seven R3 source rows")
    baseline = _run_ids(funding.get("baseline_run_ids"), "baseline_run_ids", expected=7)
    if tuple(baseline) != tuple(approved_ids):
        raise AccountingError("funding baseline does not match the seven approved R3 rows")
    apps = _run_ids(funding.get("app_run_ids"), "app_run_ids", expected=None)
    if len(apps) != len(set(apps)):
        raise AccountingError("funding app_run_ids contains duplicates")
    proof_hash = _proof_hash(funding.get("source_proof_sha"))
    debit_id, expected_reservation_id = _transfer_run_ids(day, allocated, proof_hash)
    if reservation_id != expected_reservation_id:
        raise AccountingError("funding reservation run_id does not match its deterministic transfer")
    _no_id_collisions([*baseline, reservation_id, debit_id, *apps])
    return {
        "run_date": day.isoformat(), "reservation_run_id": reservation_id, "allocated_micros": allocated,
        "consumed_micros": consumed, "native_net_micros": native_net, "baseline_run_ids": baseline,
        "app_run_ids": apps, "source_proof_sha": proof_hash,
    }


def _credit_run_id(ask_run_id):
    digest = hashlib.sha256(ask_run_id.encode("utf-8")).hexdigest()[:16]
    return f"{PHASE_ID}-app-{digest}-credit"


def _source_contract():
    try:
        from core.eval import demo_pairs
    except ImportError as exc:
        raise AccountingError("the approved funding proof helper is unavailable") from exc
    source_unused = 15_000_000 - 2_025_712
    if (source_unused != MAX_ALLOCATED_MICROS or demo_pairs.NEW_POOL_MICROS != source_unused
            or demo_pairs.KNOWN_UNUSED_MICROS != 13_471_903
            or demo_pairs.PRESERVED_OLD_UNUSED_MICROS != 497_615):
        raise AccountingError("the approved unused source allocation changed")
    return (
        demo_pairs.BASELINE_RUN_IDS,
        demo_pairs.SOURCE_RUN_DATE,
        demo_pairs.DEMO_RUN_DATE,
        demo_pairs.BASELINE_BOOKED_MICROS,
        source_unused,
    )


def _approved_source_rows(native_rows):
    try:
        from core.eval.demo_pairs import source_proof_hash
    except ImportError as exc:
        raise AccountingError("the approved funding source hash helper is unavailable") from exc
    baseline_ids, source_day, _, baseline_net, _ = _source_contract()
    by_id = _index_exact(native_rows, baseline_ids, "approved baseline")
    source_rows = []
    for run_id in baseline_ids:
        row = by_id[run_id]
        if row.get("stage") != "understand_spend" or _as_date(row.get("run_date"), "baseline run_date") != source_day:
            raise AccountingError("approved R3 row stage or date changed")
        counts = _counts(row)
        record = _json_object(row.get("record"), "baseline record", allow_none=True) or {}
        if (record.get("run") or {}).get("model_usd") is not None:
            raise AccountingError("approved spend row has model_usd in both native fields")
        if "model_usd" not in counts:
            raise AccountingError("approved spend row is missing counts.model_usd")
        amount = _decimal_amount(counts["model_usd"], "baseline counts.model_usd")
        micros = _to_micros(amount, allow_float=False, name="baseline counts.model_usd")
        source_rows.append({"run_id": run_id, "run_date": source_day.isoformat(), "model_usd_micros": micros})
    if sum(row["model_usd_micros"] for row in source_rows) != baseline_net:
        raise AccountingError("approved R3 rows do not total the frozen native baseline")
    try:
        digest = source_proof_hash(source_rows)
    except Exception as exc:
        raise AccountingError("fresh R3 rows do not match the approved source proof") from exc
    return source_rows, _proof_hash(digest)


def _transfer_run_ids(day, amount_micros, source_proof_sha):
    identity = hashlib.sha256(
        f"{PHASE_ID}|{day.isoformat()}|{amount_micros}|{source_proof_sha}".encode("utf-8")
    ).hexdigest()[:16]
    prefix = f"{PHASE_ID}-transfer-{identity}"
    return f"{prefix}-source-debit", f"{prefix}-current-credit"


def _row_model_usd(row):
    record = _json_object(row.get("record"), "Ask record")
    value = (record.get("run") or {}).get("model_usd")
    if value is None:
        raise AccountingError("real Ask record.run.model_usd is required")
    return _decimal_amount(value, "record.run.model_usd")


def _counts(row):
    value = _json_object(row.get("counts"), "counts", allow_none=True)
    if value is None:
        raise AccountingError("counts JSON is required for a signed accounting row")
    return value


def _json_object(value, name, allow_none=False):
    if value is None and allow_none:
        return None
    result = _json_value(value, name)
    if not isinstance(result, Mapping):
        raise AccountingError(f"{name} must be a JSON object")
    return dict(result)


def _json_value(value, name, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AccountingError(f"{name} is not valid JSON") from exc
    return value


def _canonical_row(value):
    row = dict(value)
    for key in _JSON_COLUMNS:
        if row.get(key) is not None:
            row[key] = _canonical_json(_json_value(row[key], key))
    if row.get("run_date") is not None:
        row["run_date"] = _as_date(row["run_date"], "run_date").isoformat()
    for key in _TIME_COLUMNS:
        if row.get(key) is not None:
            row[key] = _canonical_time(row[key])
    for key in _NUMERIC_COLUMNS:
        if row.get(key) is not None:
            try:
                number = Decimal(str(row[key]))
            except InvalidOperation as exc:
                raise AccountingError(f"{key} readback is not numeric") from exc
            if not number.is_finite():
                raise AccountingError(f"{key} readback is not finite")
            row[key] = format(number.normalize(), "f")
    return row


def _canonical_json(value):
    return json.dumps(_canonical_json_numbers(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=_json_default)


def _canonical_json_numbers(value):
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AccountingError("JSON number must be finite")
        return int(value) if value.is_integer() else value
    if isinstance(value, Mapping):
        return {key: _canonical_json_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_numbers(item) for item in value]
    return value


def _ask_row_hash(row):
    canonical = _canonical_json(_canonical_row(_expected_row(row)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_row_sha256(row):
    if not isinstance(row, Mapping):
        raise AccountingError("run row must be a mapping")
    normalized = _normalise_ask_row(row, _as_date(row.get("run_date"), "run_date"))
    return _ask_row_hash(normalized)


def _canonical_time(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        shown = str(value).replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(shown)
        except ValueError as exc:
            raise AccountingError("timestamp readback is not parseable") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_default(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _as_date(value, name):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise AccountingError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise AccountingError(f"{name} must be an ISO date") from exc


def _today_sast():
    return datetime.now(SAST).date()


def _require_runtime_day(day):
    if _today_sast() != day:
        raise AccountingError("accounting date mismatch; the demo write was refused")


def _input_micros(value, name):
    return _micros_from_input(value, name, allow_float=False)


def _micros_from_input(value, name, *, allow_float):
    if isinstance(value, bool) or (isinstance(value, float) and not allow_float):
        raise AccountingError(f"{name} must use Decimal or an exact decimal string")
    return _to_micros(value, allow_float=allow_float, name=name)


def _decimal_from_query(value, name):
    if value is None:
        return Decimal(0)
    if isinstance(value, float):
        raise AccountingError(f"{name} query must return BigQuery BIGNUMERIC, not FLOAT64")
    return _decimal_amount(value, name)


def _decimal_amount(value, name):
    if isinstance(value, bool):
        raise AccountingError(f"{name} must be an exact decimal value")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AccountingError(f"{name} is not a decimal value") from exc
    if not amount.is_finite():
        raise AccountingError(f"{name} must be finite")
    scale = max(0, -amount.normalize().as_tuple().exponent)
    if scale > 38:
        raise AccountingError(f"{name} exceeds BigQuery BIGNUMERIC precision")
    return amount


def _ceil_micros(value, name):
    amount = _decimal_amount(value, name)
    return int((amount * MICROS_PER_USD).to_integral_value(rounding=ROUND_CEILING))


def _usd_exact(value):
    amount = _decimal_amount(value, "USD amount")
    shown = format(amount, "f")
    if "." in shown:
        shown = shown.rstrip("0").rstrip(".")
    return shown if shown and shown != "-0" else "0"


def _to_micros(value, *, allow_float, name="USD value"):
    if isinstance(value, bool) or (isinstance(value, float) and not allow_float):
        raise AccountingError(f"{name} must use an exact decimal value")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AccountingError(f"{name} is not a decimal value") from exc
    if not number.is_finite():
        raise AccountingError(f"{name} must be finite")
    scaled = number * MICROS_PER_USD
    if scaled != scaled.to_integral_value():
        raise AccountingError(f"{name} must be exact to one USD micro")
    return int(scaled)


def _usd(micros):
    return f"{Decimal(micros) / MICROS_PER_USD:.6f}"


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise AccountingError(f"{name} must be an integer")
    return value


def _valid_run_id(value, name):
    if not isinstance(value, str) or not _RUN_ID.fullmatch(value):
        raise AccountingError(f"{name} must be a valid run_id")
    return value


def _run_ids(value, name, *, expected):
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise AccountingError(f"{name} must be a sequence of run_ids")
    result = [_valid_run_id(item, name) for item in value]
    if expected is not None and len(result) != expected:
        raise AccountingError(f"{name} must contain exactly {expected} run_ids")
    if len(result) != len(set(result)):
        raise AccountingError(f"{name} contains duplicate run_ids")
    return result


def _unique_ids(run_ids):
    ids = list(run_ids)
    if len(ids) != len(set(ids)):
        raise AccountingError("native readback manifest contains duplicate run_ids")
    return ids


def _no_id_collisions(run_ids):
    _unique_ids(run_ids)


def _proof_hash(value):
    if not isinstance(value, str) or not _SOURCE_HASH.fullmatch(value):
        raise AccountingError("source_proof_sha must be a lowercase SHA-256")
    return value


def _field(row, name):
    try:
        return row[name]
    except (KeyError, TypeError):
        return getattr(row, name, None)
