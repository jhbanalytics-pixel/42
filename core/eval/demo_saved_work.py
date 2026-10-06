import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from core.api import dossiers


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MICRO = Decimal("0.000001")
_FINISHED = frozenset(dossiers.FINISHED)
_ATTEMPT_STATUSES = _FINISHED | {"failed"}


class SavedWorkError(ValueError):
    pass


class SelectionMismatch(SavedWorkError):
    pass


class AccountingMismatch(SavedWorkError):
    pass


class AskReadbackMismatch(SavedWorkError):
    pass


class DossierPostUncertain(SavedWorkError):
    pass


class DossierReadbackMismatch(SavedWorkError):
    pass


def _hash(value):
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record_hash(value):
    def normalize(item):
        if isinstance(item, bool):
            return item
        if isinstance(item, float) and math.isfinite(item) and item.is_integer():
            return int(item)
        if isinstance(item, Mapping):
            return {key: normalize(child) for key, child in item.items()}
        if isinstance(item, list):
            return [normalize(child) for child in item]
        return item
    return _hash(normalize(value))


def _sha256(value, name):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise SelectionMismatch(f"{name} must be a lowercase SHA-256 digest.")
    return value


def _usd(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise AccountingMismatch(f"{name} must be a USD amount on the microdollar ledger quantum.")
    try:
        amount = Decimal(str(value))
        quantized = amount.quantize(_MICRO)
    except (InvalidOperation, ValueError):
        raise AccountingMismatch(f"{name} must be a USD amount on the microdollar ledger quantum.") from None
    if not amount.is_finite() or amount < 0 or amount != quantized:
        raise AccountingMismatch(f"{name} must be nonnegative and exact to six decimal places.")
    return quantized


def _exact_usd(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise AccountingMismatch(f"{name} must be an exact nonnegative USD amount.")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise AccountingMismatch(f"{name} must be an exact nonnegative USD amount.") from None
    if not amount.is_finite() or amount < 0:
        raise AccountingMismatch(f"{name} must be an exact nonnegative USD amount.")
    return amount


def _usd_text(amount):
    return format(amount, "f")


def _ceil_micros(amount):
    return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _accounting_usd(value, name):
    if not isinstance(value, str):
        raise AccountingMismatch(f"{name} must be a USD string.")
    return _exact_usd(value, name)


def _json_value(value, name):
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise SelectionMismatch(f"{name} must contain plain JSON values.") from None


def _funding_contract(funding, run_date):
    if not isinstance(funding, Mapping):
        raise AccountingMismatch("A verified demo funding proof is required.")
    if funding.get("schema_version") != "demo-funding-v1":
        raise AccountingMismatch("The demo funding proof has an unsupported schema.")
    if funding.get("phase_id") != "l3-demo-20261001":
        raise AccountingMismatch("The demo funding proof names a different phase.")
    if funding.get("run_date") != run_date:
        raise AccountingMismatch("The demo funding proof and Ask row must use the same run date.")
    if not isinstance(funding.get("reservation_run_id"), str) or not funding["reservation_run_id"]:
        raise AccountingMismatch("The demo funding proof is missing its reservation run id.")
    if (not isinstance(funding.get("source_proof_sha"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", funding["source_proof_sha"])):
        raise AccountingMismatch("The demo funding proof is missing its source digest.")
    if funding.get("native_net_micros") != 25_000_000:
        raise AccountingMismatch("The demo funding proof must preserve the authorized USD 25 net.")
    for key in ("allocated_micros", "consumed_micros"):
        value = funding.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AccountingMismatch(f"The demo funding proof has an invalid {key}.")
    if funding["consumed_micros"] > funding["allocated_micros"]:
        raise AccountingMismatch("The demo funding proof consumes more than its allocation.")
    baseline = funding.get("baseline_run_ids")
    prior_app = funding.get("app_run_ids")
    if not isinstance(baseline, list) or len(baseline) != 7 or not all(
        isinstance(item, str) and item for item in baseline
    ):
        raise AccountingMismatch("The demo funding proof must preserve its seven baseline run ids.")
    if not isinstance(prior_app, list) or not all(isinstance(item, str) and item for item in prior_app):
        raise AccountingMismatch("The demo funding proof has invalid prior app run ids.")


def _receipt_identity(selected_receipt, raw_receipt_bytes, manifest_sha256):
    if not isinstance(raw_receipt_bytes, bytes) or not raw_receipt_bytes:
        raise SelectionMismatch("The full immutable receipt bytes are required.")
    manifest_hash = _sha256(manifest_sha256, "raw receipt manifest hash")
    exact_hash = "sha256:" + hashlib.sha256(raw_receipt_bytes).hexdigest()
    if exact_hash != manifest_hash:
        raise SelectionMismatch("The full receipt bytes differ from the trusted manifest hash.")
    try:
        parsed = json.loads(raw_receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError):
        raise SelectionMismatch("The raw receipt bytes are not a UTF-8 JSON object.") from None
    if not isinstance(parsed, Mapping) or not isinstance(selected_receipt, Mapping):
        raise SelectionMismatch("The raw receipt and selected attempt must be JSON objects.")
    selected = dict(selected_receipt)
    if "receipt_sha256" in selected and "receipt_sha256" not in parsed:
        external_hash = _sha256(selected.pop("receipt_sha256"), "external receipt hash")
        if external_hash != exact_hash:
            raise SelectionMismatch("The external receipt hash differs from the exact receipt bytes.")
    if _hash(dict(parsed)) != _hash(selected):
        raise SelectionMismatch("The selected attempt differs from the parsed full receipt.")
    return exact_hash


def _scope_admission_manifest(receipt_hash, question_id, attempt, admitted_claim_ids):
    body = {
        "schema_version": "demo-scope-admission-v1",
        "question_id": question_id,
        "attempt": attempt,
        "raw_receipt_sha256": receipt_hash,
        "admitted_claim_ids": list(admitted_claim_ids),
    }
    return {**body, "scope_admission_manifest_sha256": _hash(body)}


def _object_ids(value, name):
    if not isinstance(value, frozenset) or not all(isinstance(item, str) and item for item in value):
        raise SelectionMismatch(f"{name} must be a frozen set of object ids.")
    return value

def _base_record(selected_receipt, app_record):
    if not isinstance(selected_receipt, Mapping) or not isinstance(app_record, Mapping):
        raise SelectionMismatch("A selected receipt and its app Ask record are required.")
    record = _json_value(app_record, "app_record")
    required = ("ask_id", "question", "status", "created_at", "finished_at", "answer", "run", "steps")
    if any(key not in record for key in required):
        raise SelectionMismatch("The app Ask record is missing a required saved-record field.")
    if record["status"] not in _ATTEMPT_STATUSES:
        raise SelectionMismatch("The app Ask record must retain its real finished or failed status.")
    if not isinstance(record["ask_id"], str) or not record["ask_id"]:
        raise SelectionMismatch("The app Ask record must carry its original ask_id.")
    if not isinstance(record["question"], str) or not record["question"]:
        raise SelectionMismatch("The app Ask record must carry its original question.")
    if not isinstance(record["created_at"], str) or len(record["created_at"]) < 10:
        raise SelectionMismatch("The app Ask record must carry its original created_at.")
    if not isinstance(record["finished_at"], str) or len(record["finished_at"]) < 10:
        raise SelectionMismatch("The app Ask record must carry its original finished_at.")
    if not isinstance(record["run"], dict) or not isinstance(record["steps"], list):
        raise SelectionMismatch("The app Ask record must carry its actual run and steps.")
    run_id = record["run"].get("run_id")
    if not isinstance(run_id, str) or not run_id or selected_receipt.get("run_id") != run_id:
        raise SelectionMismatch("The selected receipt and app Ask must name the same source run.")
    if record["answer"] != selected_receipt.get("raw_answer"):
        raise SelectionMismatch("The app Ask answer differs from the selected raw answer.")
    if record["answer"] is not None and not isinstance(record["answer"], Mapping):
        raise SelectionMismatch("The selected run answer must be a source answer object or null.")
    return record

def _exact_subset(source_rows, selected_rows, key, name):
    if not isinstance(source_rows, list) or not isinstance(selected_rows, list):
        raise SelectionMismatch(f"{name} must be a list from the source answer.")
    by_id = {}
    for row in source_rows:
        if isinstance(row, Mapping) and isinstance(row.get(key), str):
            by_id.setdefault(row[key], []).append(row)
    selected = []
    seen = set()
    for row in selected_rows:
        if not isinstance(row, Mapping) or not isinstance(row.get(key), str):
            raise SelectionMismatch(f"Every selected {name[:-1]} must carry its source {key}.")
        row_id = row[key]
        if row_id in seen or len(by_id.get(row_id, [])) != 1:
            raise SelectionMismatch(f"The selected {name[:-1]} {row_id} is duplicated or ambiguous in the source.")
        if dict(row) != dict(by_id[row_id][0]):
            raise SelectionMismatch(f"The selected {name[:-1]} {row_id} differs from the source.")
        seen.add(row_id)
        selected.append(copy.deepcopy(dict(row)))
    return selected


def _saved_answer(raw_answer, saved_payload, selected_ids, question_id=None):
    if not isinstance(raw_answer, Mapping):
        raise SelectionMismatch("The selected receipt has no answer object.")
    required = ("status", "as_of", "short_answer", "claims", "evidence", "so_what", "watch_next", "gaps")
    if any(key not in raw_answer for key in required):
        raise SelectionMismatch("The original answer does not satisfy the answer contract.")
    if raw_answer["status"] not in ("complete", "partial"):
        raise SelectionMismatch("Only a complete or partial answer can be selected for persistence.")
    if not isinstance(raw_answer["short_answer"], str):
        raise SelectionMismatch("The original answer short_answer must be text.")
    if not isinstance(saved_payload, Mapping):
        raise SelectionMismatch("The selector did not return a saved payload.")
    if question_id is not None and saved_payload.get("question_id") not in (None, question_id):
        raise SelectionMismatch("The saved payload and source receipt name different demo questions.")

    claims = _exact_subset(raw_answer["claims"], saved_payload.get("claims"), "id", "claims")
    claim_ids = [claim["id"] for claim in claims]
    if claim_ids != selected_ids:
        raise SelectionMismatch("The saved claim objects do not match the admitted claim ids.")
    evidence = _exact_subset(raw_answer["evidence"], saved_payload.get("evidence"), "id", "evidence")
    evidence_ids = [row["id"] for row in evidence]
    cited_ids = {
        evidence_id
        for claim in claims
        for evidence_id in claim.get("evidence_ids", [])
        if isinstance(evidence_id, str)
    }
    if not cited_ids or not cited_ids.issubset(set(evidence_ids)) or set(evidence_ids) != cited_ids:
        raise SelectionMismatch("Saved evidence must exactly cover the selected claims' source references.")
    if len(set(evidence_ids)) != len(evidence_ids):
        raise SelectionMismatch("Saved evidence ids must be unique.")

    selected_answer = {
        "status": raw_answer["status"],
        "as_of": raw_answer["as_of"],
        "short_answer": "",
        "claims": claims,
        "evidence": evidence,
        "so_what": [],
        "watch_next": [],
        "gaps": [],
    }
    dropped = (
        len(claims) != len(raw_answer["claims"])
        or len(evidence) != len(raw_answer["evidence"])
        or bool(raw_answer["short_answer"])
        or bool(raw_answer["so_what"])
        or bool(raw_answer["watch_next"])
        or bool(raw_answer["gaps"])
    )
    if dropped or raw_answer["status"] == "partial":
        selected_answer["status"] = "partial"

    admitted, _ = dossiers.admitted(selected_answer)
    if list(admitted) != claim_ids:
        raise SelectionMismatch("Every saved claim must remain admitted by the dossier contract.")
    return selected_answer, dropped


def _view_record(
    selected_receipt,
    app_record,
    safe_payload,
    admitted_claim_ids,
    question_id,
    receipt_hash,
):
    record = _base_record(selected_receipt, app_record)
    raw_answer = record["answer"]
    raw_hash = _hash(raw_answer) if isinstance(raw_answer, Mapping) else None
    attempt = selected_receipt.get("attempt")
    from core.eval.demo_scope import assess_answer

    assessment = assess_answer(dict(selected_receipt), question_id)
    if not isinstance(assessment, Mapping):
        raise SelectionMismatch("The W2 scope assessor returned no admission result.")
    computed_ids = assessment.get("admitted_claim_ids")
    if not isinstance(computed_ids, list) or not all(isinstance(item, str) and item for item in computed_ids):
        raise SelectionMismatch("The W2 scope assessor returned invalid admitted claim ids.")
    if len(set(computed_ids)) != len(computed_ids):
        raise SelectionMismatch("The W2 scope assessor returned duplicate admitted claim ids.")
    if admitted_claim_ids != computed_ids:
        raise SelectionMismatch("The supplied claim ids differ from the independent W2 scope assessment.")
    if bool(assessment.get("safe")) != bool(computed_ids):
        raise SelectionMismatch("The W2 safe flag does not match its admitted claim ids.")
    scope_manifest = _scope_admission_manifest(receipt_hash, question_id, attempt, computed_ids)

    if not computed_ids:
        if safe_payload is not None:
            if not isinstance(safe_payload, Mapping):
                raise SelectionMismatch("A no-answer saved payload must be an object or null.")
            if safe_payload.get("claims") or safe_payload.get("evidence"):
                raise SelectionMismatch("A no-answer saved view cannot carry claim or evidence rows.")
        record["answer"] = None
        return record, raw_hash, [], "no_answer", raw_answer is not None, scope_manifest

    if not assessment.get("safe") or raw_answer is None:
        raise SelectionMismatch("W2 did not admit a source answer for this saved view.")
    selected_answer, dropped = _saved_answer(raw_answer, safe_payload, computed_ids, question_id)
    record["answer"] = selected_answer
    return record, raw_hash, list(computed_ids), selected_answer["status"], dropped, scope_manifest

def _app_ask_row(record, selected_receipt):
    from datetime import datetime

    from core.eval.demo_pairs import SAST

    created_at_text = record.get("created_at")
    if not isinstance(created_at_text, str):
        raise SelectionMismatch("The actual Ask creation time must include a timezone.")
    try:
        created_at = datetime.fromisoformat(created_at_text.replace("Z", "+00:00"))
    except ValueError:
        raise SelectionMismatch("The actual Ask creation time must include a timezone.") from None
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise SelectionMismatch("The actual Ask creation time must include a timezone.")
    run = record["run"]
    seconds = run.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        raise SelectionMismatch("The actual Ask run seconds are required by the app save contract.")
    answer = record["answer"]
    error = record.get("error")
    if not isinstance(error, Mapping):
        error = {}
    return {
        "run_id": run.get("run_id") or record["ask_id"],
        "stage": "ask",
        "run_date": created_at.astimezone(SAST).date().isoformat(),
        "status": record["status"],
        "started_at": record["created_at"],
        "finished_at": record["finished_at"],
        "question": record["question"],
        "tier": run.get("tier") or selected_receipt.get("tier"),
        "credits": run.get("credits"),
        "seconds": seconds,
        "outcome": answer.get("status") if answer else error.get("error", record["status"]),
        "answer": json.dumps(answer, allow_nan=False) if answer is not None else None,
        "record": json.dumps(record, allow_nan=False),
    }


def _run_row_sha256(row):
    from core.eval.demo_native_accounting import run_row_sha256

    try:
        digest = run_row_sha256(row)
    except Exception:
        raise AskReadbackMismatch("The native adapter could not canonicalize its Ask row.") from None
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise AskReadbackMismatch("The native adapter returned an invalid normalized Ask row hash.")
    return digest


def _parsed_row_record(row):
    value = row.get("record")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            raise AskReadbackMismatch("The Ask row readback record is not valid JSON.") from None
    if not isinstance(value, Mapping):
        raise AskReadbackMismatch("The Ask row readback has no record object.")
    return dict(value)


def _accounting(native_result, actual_usd, guarded_usd):
    accounting = native_result.get("accounting")
    if not isinstance(accounting, Mapping):
        raise AccountingMismatch("The native adapter omitted its accounting readback.")
    apppriced = _accounting_usd(accounting.get("apppriced_usd"), "native apppriced_usd")
    guarded = _accounting_usd(accounting.get("guarded_usd"), "native guarded_usd")
    available = _accounting_usd(accounting.get("available_before_usd"), "native available_before_usd")
    released = _accounting_usd(accounting.get("released_usd"), "native released_usd")
    retained = _accounting_usd(accounting.get("retained_usd"), "native retained_usd")
    if apppriced != actual_usd or guarded != guarded_usd or not apppriced <= guarded <= available:
        raise AccountingMismatch("The native funding readback does not cover the exact Ask cost.")
    if released != apppriced or retained != guarded - apppriced:
        raise AccountingMismatch("The native release or retained hold differs from the app priced cost.")
    return {
        "apppriced_usd": _usd_text(apppriced),
        "guarded_usd": _usd_text(guarded),
        "available_before_usd": _usd_text(available),
        "released_usd": _usd_text(released),
        "retained_usd": _usd_text(retained),
    }


def _validate_attempt_receipt(selected_receipt):
    if not isinstance(selected_receipt, Mapping):
        raise SelectionMismatch("The selected raw receipt is required.")
    attempt = selected_receipt.get("attempt")
    if isinstance(attempt, bool) or attempt not in (1, 2):
        raise SelectionMismatch("The selected receipt must name attempt 1 or 2.")
    question_id = selected_receipt.get("question_id")
    if not isinstance(question_id, str) or not question_id:
        raise SelectionMismatch("The selected receipt must name its demo question.")
    return attempt, question_id


def persist_attempt_view(
    *,
    selected_receipt,
    app_record,
    safe_payload,
    admitted_claim_ids,
    raw_receipt_bytes,
    raw_receipt_sha256,
    funding,
    guarded_charge_usd,
    native_adapter,
    frozen_ask_ids=frozenset(),
):
    attempt, question_id = _validate_attempt_receipt(selected_receipt)
    frozen_ask_ids = _object_ids(frozen_ask_ids, "frozen_ask_ids")
    receipt_hash = _receipt_identity(selected_receipt, raw_receipt_bytes, raw_receipt_sha256)
    record, source_answer_hash, saved_ids, saved_status, dropped, scope_manifest = _view_record(
        selected_receipt,
        app_record,
        safe_payload,
        admitted_claim_ids,
        question_id,
        receipt_hash,
    )
    if record["ask_id"] in frozen_ask_ids:
        raise SelectionMismatch("A frozen Ask object cannot be written again.")
    row = _app_ask_row(record, selected_receipt)
    if record["run"].get("model_usd") is None:
        raise AccountingMismatch("The actual Ask cost is unknown; leave the full reservation hold in place.")
    actual_usd = _exact_usd(record["run"].get("model_usd"), "app priced cost")
    guarded_usd = _usd(guarded_charge_usd, "guarded charge")
    if actual_usd > guarded_usd:
        raise AccountingMismatch("The app priced cost exceeds the guarded charge.")
    if guarded_usd == 0:
        raise AccountingMismatch("A positive guarded charge is required for a reserved Ask.")
    _funding_contract(funding, row["run_date"])
    if not callable(getattr(native_adapter, "convert_prebooked_ask", None)):
        raise AccountingMismatch("An injected native accounting adapter is required.")

    row_hash = _run_row_sha256(row)
    native_result = native_adapter.convert_prebooked_ask(
        real_ask_row=row,
        funding=funding,
        guarded_charge=_usd_text(guarded_usd),
    )
    if not isinstance(native_result, Mapping) or native_result.get("status") not in ("stored", "resumed"):
        raise AskReadbackMismatch("The native adapter did not confirm a stored or resumed Ask row.")
    ask_row = native_result.get("ask_row")
    if not isinstance(ask_row, Mapping) or _run_row_sha256(dict(ask_row)) != row_hash:
        raise AskReadbackMismatch("The native Ask row readback differs from the submitted row.")
    if native_result.get("ask_row_sha256") != row_hash:
        raise AskReadbackMismatch("The native Ask row hash differs from the submitted row.")
    if _record_hash(_parsed_row_record(ask_row)) != _record_hash(record):
        raise AskReadbackMismatch("The native Ask record readback differs from the submitted record.")
    accounting = _accounting(native_result, actual_usd, guarded_usd)

    run_id = record["run"]["run_id"]
    app_ceiling = _ceil_micros(actual_usd)
    return {
        "status": native_result["status"],
        "attempt": attempt,
        "question_id": question_id,
        "ask_id": record["ask_id"],
        "run_id": run_id,
        "ask_row_sha256": row_hash,
        "stored_ask_row_sha256": row_hash,
        "record_sha256": _record_hash(record),
        "source_answer_sha256": source_answer_hash,
        "source_receipt_sha256": receipt_hash,
        "scope_admitted_claim_ids": list(saved_ids),
        "scope_admission_manifest": scope_manifest,
        "scope_admission_manifest_sha256": scope_manifest["scope_admission_manifest_sha256"],
        "saved_view_status": saved_status,
        "partial_selection": dropped,
        "recorded_model_usd": accounting["apppriced_usd"],
        "reservation_release_usd": accounting["released_usd"],
        "recorded_model_usd_ceiling_micros": app_ceiling,
        "reservation_release_ceiling_micros": app_ceiling,
        "native_net_micros": funding["native_net_micros"],
        "readback": {
            "run_id": run_id,
            "ask_id": record["ask_id"],
            "recorded_model_usd": accounting["apppriced_usd"],
            "reservation_release_usd": accounting["released_usd"],
            "recorded_model_usd_ceiling_micros": app_ceiling,
            "reservation_release_ceiling_micros": app_ceiling,
        },
        "accounting": accounting,
    }

def _response_body(response, expected_status, error_type, message):
    if getattr(response, "status_code", None) != expected_status:
        raise error_type(message)
    try:
        body = response.json()
    except Exception:
        raise error_type(message) from None
    if not isinstance(body, Mapping):
        raise error_type(message)
    return dict(body)


def _verify_dossier(body, *, ask_id, expected_claim_ids, expected_hash=None, expected_id=None):
    dossier_id = body.get("dossier_id")
    content_hash = body.get("content_hash")
    if not isinstance(dossier_id, str) or not dossier_id.startswith("d_"):
        raise DossierReadbackMismatch("The dossier response has no valid dossier id.")
    if expected_id is not None and dossier_id != expected_id:
        raise DossierReadbackMismatch("The dossier readback returned a different dossier id.")
    if body.get("source_ask_id") != ask_id:
        raise DossierReadbackMismatch("The dossier does not point to the saved Ask.")
    if not isinstance(content_hash, str) or not _SHA256.fullmatch(content_hash):
        raise DossierReadbackMismatch("The dossier response has no valid content hash.")
    if expected_hash is not None and content_hash != expected_hash:
        raise DossierReadbackMismatch("The dossier content hash changed on readback.")
    claims = body.get("claims")
    if not isinstance(claims, list):
        raise DossierReadbackMismatch("The dossier response has no claim list.")
    claim_ids = [claim.get("claim_id") for claim in claims if isinstance(claim, Mapping)]
    if len(claim_ids) != len(claims) or claim_ids != expected_claim_ids or not all(
        claim.get("kept") for claim in claims
    ):
        raise DossierReadbackMismatch("The dossier claims differ from the selected saved answer.")
    unhashed = copy.deepcopy(dict(body))
    for key in ("content_hash", "ticks", "needs_tick"):
        unhashed.pop(key, None)
    if dossiers.content_hash(unhashed) != content_hash:
        raise DossierReadbackMismatch("The dossier content hash does not match its returned content.")
    return dossier_id, content_hash


def create_saved_work(
    *,
    selection,
    selected_receipt,
    app_record,
    selected_existing_ask,
    raw_receipt_bytes,
    raw_receipt_sha256,
    app_client,
    frozen_ask_ids=frozenset(),
    frozen_dossier_ids=frozenset(),
):
    frozen_ask_ids = _object_ids(frozen_ask_ids, "frozen_ask_ids")
    frozen_dossier_ids = _object_ids(frozen_dossier_ids, "frozen_dossier_ids")
    if not isinstance(selection, Mapping) or selection.get("status") != "selected" or selection.get("full_pass") is not False:
        raise SelectionMismatch("Only a selected W7 attempt with full_pass false can create a dossier.")
    attempt, question_id = _validate_attempt_receipt(selected_receipt)
    if selection.get("selected_attempt") != attempt or selection.get("question_id") != question_id:
        raise SelectionMismatch("The selected W7 answer does not match the persisted attempt.")
    receipt_hash = _receipt_identity(selected_receipt, raw_receipt_bytes, raw_receipt_sha256)
    if not isinstance(selected_existing_ask, Mapping) or selected_existing_ask.get("status") not in ("stored", "resumed"):
        raise SelectionMismatch("The selected Ask must already have a verified native readback.")
    if selected_existing_ask.get("attempt") not in (None, attempt) or selected_existing_ask.get("question_id") != question_id:
        raise SelectionMismatch("The persisted Ask belongs to a different attempt.")
    if selected_existing_ask.get("source_receipt_sha256") != receipt_hash:
        raise SelectionMismatch("The persisted Ask belongs to a different source receipt.")

    attempt_key = str(attempt)
    run_id = (selection.get("run_ids") or {}).get(attempt_key)
    raw_hash = (selection.get("raw_hashes") or {}).get(attempt_key)
    _sha256(raw_hash, "selected source answer hash")
    if selected_existing_ask.get("run_id") != run_id or selected_existing_ask.get("source_answer_sha256") != raw_hash:
        raise SelectionMismatch("The selected saved Ask does not match the W7 source run and answer hash.")
    selected_ids = (selection.get("admitted_claim_ids") or {}).get(attempt_key)
    if not isinstance(selected_ids, list) or not selected_ids or not all(
        isinstance(item, str) and item for item in selected_ids
    ):
        raise SelectionMismatch("The selected W7 answer has no admitted claims.")

    record, source_hash, saved_ids, saved_status, dropped, scope_manifest = _view_record(
        selected_receipt,
        app_record,
        selection.get("saved_payload"),
        selected_ids,
        question_id,
        receipt_hash,
    )
    if source_hash != raw_hash or saved_ids != selected_ids:
        raise SelectionMismatch("The selected saved payload differs from the W7 source.")
    if selected_existing_ask.get("scope_admission_manifest") != scope_manifest or selected_existing_ask.get(
        "scope_admission_manifest_sha256"
    ) != scope_manifest["scope_admission_manifest_sha256"]:
        raise SelectionMismatch("The persisted Ask has a different W2 scope admission manifest.")
    ask_id = record["ask_id"]
    if selected_existing_ask.get("ask_id") != ask_id:
        raise SelectionMismatch("The selected saved Ask id differs from the verified Ask id.")
    row = _app_ask_row(record, selected_receipt)
    if _record_hash(record) != selected_existing_ask.get("record_sha256") or _run_row_sha256(row) != selected_existing_ask.get(
        "ask_row_sha256"
    ):
        raise AskReadbackMismatch("The selected W7 view differs from the already-stored Ask record.")

    actual_usd = _exact_usd(record["run"].get("model_usd"), "app priced cost")
    accounting = selected_existing_ask.get("accounting")
    if not isinstance(accounting, Mapping):
        raise AccountingMismatch("The selected saved Ask has no verified accounting readback.")
    apppriced = _accounting_usd(accounting.get("apppriced_usd"), "stored apppriced_usd")
    guarded = _accounting_usd(accounting.get("guarded_usd"), "stored guarded_usd")
    available = _accounting_usd(accounting.get("available_before_usd"), "stored available_before_usd")
    released = _accounting_usd(accounting.get("released_usd"), "stored released_usd")
    retained = _accounting_usd(accounting.get("retained_usd"), "stored retained_usd")
    if apppriced != actual_usd or not apppriced <= guarded <= available or released != apppriced:
        raise AccountingMismatch("The selected Ask funding proof does not match its exact app price.")
    if retained != guarded - apppriced:
        raise AccountingMismatch("The selected Ask retained hold differs from its guarded charge.")
    app_ceiling = _ceil_micros(actual_usd)
    if selected_existing_ask.get("recorded_model_usd") != accounting["apppriced_usd"]:
        raise AccountingMismatch("The saved Ask exact cost differs from its native accounting.")
    if selected_existing_ask.get("reservation_release_usd") != accounting["released_usd"]:
        raise AccountingMismatch("The saved Ask exact release differs from its native accounting.")
    readback = selected_existing_ask.get("readback")
    if not isinstance(readback, Mapping):
        raise AccountingMismatch("The selected Ask omitted its nested readback proof.")
    if readback.get("run_id") != run_id or readback.get("ask_id") != ask_id:
        raise AccountingMismatch("The nested Ask readback identity does not match the selected record.")
    if readback.get("recorded_model_usd") != accounting["apppriced_usd"]:
        raise AccountingMismatch("The nested saved Ask exact cost differs from its native accounting.")
    if readback.get("reservation_release_usd") != accounting["released_usd"]:
        raise AccountingMismatch("The nested saved Ask exact release differs from its native accounting.")
    for field in ("recorded_model_usd_ceiling_micros", "reservation_release_ceiling_micros"):
        if selected_existing_ask.get(field) != app_ceiling or readback.get(field) != app_ceiling:
            raise AccountingMismatch("The saved Ask ceiling micro proof differs from its exact app price.")
    if selected_existing_ask.get("native_net_micros") != 25_000_000:
        raise AccountingMismatch("The saved Ask accounting readback changed the authorized net.")
    if not callable(getattr(app_client, "post", None)) or not callable(getattr(app_client, "get", None)):
        raise DossierPostUncertain("An authenticated app client for the existing dossier route is required.")

    request = {"from": {"ask_id": ask_id}}
    try:
        post_response = app_client.post("/api/dossiers", json=request)
    except Exception:
        raise DossierPostUncertain("Dossier creation outcome is unknown; do not retry automatically.") from None
    post_body = _response_body(
        post_response, 201, DossierPostUncertain, "The existing dossier route did not return a created dossier."
    )
    dossier_id, content_hash = _verify_dossier(
        post_body, ask_id=ask_id, expected_claim_ids=selected_ids
    )
    if dossier_id in frozen_dossier_ids:
        raise DossierReadbackMismatch("The dossier create route returned an id already frozen by L5.")
    try:
        read_response = app_client.get(f"/api/dossiers/{dossier_id}")
    except Exception:
        raise DossierReadbackMismatch("The saved dossier could not be read back; do not retry automatically.") from None
    read_body = _response_body(
        read_response, 200, DossierReadbackMismatch, "The saved dossier readback failed."
    )
    _verify_dossier(
        read_body,
        ask_id=ask_id,
        expected_claim_ids=selected_ids,
        expected_hash=content_hash,
        expected_id=dossier_id,
    )

    source_claim_ids = [
        claim.get("id")
        for claim in selected_receipt["raw_answer"].get("claims") or []
        if isinstance(claim, Mapping)
    ]
    return {
        "ask_id": ask_id,
        "run_id": run_id,
        "ask_row_sha256": selected_existing_ask["ask_row_sha256"],
        "dossier_id": dossier_id,
        "dossier_content_hash": content_hash,
        "scope_admitted_claim_ids": list(saved_ids),
        "scope_admission_manifest": scope_manifest,
        "scope_admission_manifest_sha256": scope_manifest["scope_admission_manifest_sha256"],
        "accounting": dict(selected_existing_ask["accounting"]),
        "handoff_receipt": {
            "question_id": question_id,
            "selected_attempt": attempt,
            "source_run_id": run_id,
            "source_answer_sha256": raw_hash,
            "source_receipt_sha256": receipt_hash,
            "scope_admitted_claim_ids": list(saved_ids),
            "scope_admission_manifest_sha256": scope_manifest["scope_admission_manifest_sha256"],
            "dropped_claim_ids": [claim_id for claim_id in source_claim_ids if claim_id not in selected_ids],
            "saved_view_status": saved_status,
            "partial_selection": dropped,
            "raw_hashes": copy.deepcopy(selection.get("raw_hashes")),
        },
    }
