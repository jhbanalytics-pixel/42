from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
INPUTS = Path("C:/Users/AlbertMeintjes/dev/42-inputs")
DATA = INPUTS / "ASK-DEMO-PAIRS-2026-10-01-RUN"
APPROVAL_NAME = "ASK-DEMO-PAIRS-2026-10-01-APPROVAL.json"
QUESTIONS_NAME = "DEMO-QUESTIONS.md"
AGENT_ACCESS_NAME = "ASK-DEMO-AGENT-ACCESS-2026-10-01.json"
ARCHIVED_REPORT_NAME = "ASK-2026-09-30-R3-CLOSED-REPORT.md"
ARCHIVED_REPORT_SHA256 = "a881bab9491e27404f4c58c828f19a57c5b7f6972746128da66e48951ffb2205"
AGENT_URI = "https://f42-agent-fibxg5ynpq-uc.a.run.app"
APP_BASE = AGENT_URI
MAX_BYTES_BILLED = 50_000_000
REQUEST_TIMEOUT_SECONDS = 20
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
LIVE_SOURCE_FILES = (
    "core/eval/demo_operator.py", "core/eval/demo_pairs.py", "core/eval/demo_dispatch.py",
    "core/eval/demo_native_accounting.py", "core/eval/demo_saved_work.py", "core/eval/demo_scope.py",
    "core/eval/demo_selection.py", "core/eval/ask_r2.py", "core/eval/staging_check.py",
    "core/eval/trending_replay.py",
    "core/eval/ranked_demo.py", "core/agent/ask.py", "core/agent/context.py",
    "core/agent/tools/warehouse.py",
)


class OperatorRefused(RuntimeError):
    pass


def _canonical_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                       allow_nan=False) + "\n").encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _write_exclusive(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _read_object(path, reason):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise OperatorRefused(reason) from None
    if not isinstance(value, dict):
        raise OperatorRefused(reason)
    return value


def _verify_approval(inputs_dir, questions_sha):
    from core.eval import demo_pairs

    question_path = Path(inputs_dir) / QUESTIONS_NAME
    approval_path = Path(inputs_dir) / APPROVAL_NAME
    try:
        actual_questions_sha = _sha(question_path.read_bytes())
    except OSError:
        raise OperatorRefused("questions_file_missing") from None
    if actual_questions_sha != questions_sha or actual_questions_sha != demo_pairs.DEMO_QUESTIONS_SHA256:
        raise OperatorRefused("questions_hash_mismatch")
    approval = _read_object(approval_path, "approval_file_invalid")
    if (approval.get("question_document_sha256") != actual_questions_sha
            or approval.get("questions") != len(demo_pairs.DEMO_QUESTIONS)
            or approval.get("attempts_per_question") != demo_pairs.MAX_ATTEMPTS
            or approval.get("automatic_retries") != 0
            or approval.get("cumulative_cap_usd") != 25
            or approval.get("known_unused_reservations_usd") != "13.471903"
            or approval.get("unknown_failed_call_and_CRE_bookings_retained") is not True
            or approval.get("native_calls_started") != 0):
        raise OperatorRefused("approval_contract_mismatch")
    source_path = (Path(inputs_dir) / "ASK-2026-09-30-R3-continuation-1-data"
                   / "NOW-01-continuation-1.json")
    try:
        source_sha = _sha(source_path.read_bytes())
    except OSError:
        raise OperatorRefused("source_receipt_missing") from None
    if source_sha != demo_pairs.SOURCE_RECEIPT_SHA256:
        raise OperatorRefused("source_receipt_hash_mismatch")
    try:
        archived_report_sha = _sha((Path(inputs_dir) / ARCHIVED_REPORT_NAME).read_bytes())
    except OSError:
        raise OperatorRefused("archived_report_missing") from None
    if archived_report_sha != ARCHIVED_REPORT_SHA256:
        raise OperatorRefused("archived_report_hash_mismatch")
    return {"questions_sha256": actual_questions_sha, "approval_sha256": _sha(approval_path.read_bytes()),
            "source_receipt_sha256": source_sha, "archived_report_sha256": archived_report_sha,
            "approval_recorded_at": approval.get("recorded_at")}


def _validate_anchor_contexts(contexts, questions):
    if not isinstance(contexts, dict):
        raise OperatorRefused("anchor_contexts_missing")
    expected_ids = set()
    for question in questions:
        context = contexts.get(question.id)
        if not isinstance(context, dict) or context.get("market") != question.market:
            raise OperatorRefused("anchor_context_incomplete")
        if tuple(context.get("source_ids") or ()) != question.source_ids:
            raise OperatorRefused("anchor_context_incomplete")
        if context.get("support_run_id") != question.support_run_id:
            raise OperatorRefused("anchor_context_incomplete")
        posts = context.get("posts")
        if not isinstance(posts, list) or len(posts) != len(question.source_ids):
            raise OperatorRefused("anchor_context_incomplete")
        by_id = {}
        for post in posts:
            if not isinstance(post, dict):
                raise OperatorRefused("anchor_context_incomplete")
            post_id = post.get("id")
            if not isinstance(post_id, str) or post_id in by_id or not isinstance(post.get("text"), str) or not post["text"]:
                raise OperatorRefused("anchor_context_incomplete")
            by_id[post_id] = post
        if set(by_id) != set(question.source_ids):
            raise OperatorRefused("anchor_context_incomplete")
        expected_ids.update(question.source_ids)
    if len(expected_ids) != 5:
        raise OperatorRefused("anchor_set_mismatch")
    return True


def _attempt_is_verified(receipt):
    persistence = receipt.get("attempt_persistence") if isinstance(receipt, dict) else None
    readback = persistence.get("readback") if isinstance(persistence, dict) else None
    if (not isinstance(persistence, dict) or persistence.get("verified") is not True
            or not isinstance(readback, dict) or readback.get("match") is not True):
        return False
    amounts = []
    for key in ("recorded_model_usd", "reservation_release_usd"):
        value = persistence.get(key)
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return False
        if not amount.is_finite() or amount < 0:
            return False
        amounts.append(amount)
    if amounts[0] != amounts[1] or receipt.get("model_usd") != persistence.get("recorded_model_usd"):
        return False
    ceiling = int((amounts[0] * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    if (persistence.get("recorded_model_usd_ceiling_micros") != ceiling
            or persistence.get("reservation_release_ceiling_micros") != ceiling):
        return False
    if persistence.get("native_net_micros") != 25_000_000:
        return False
    if receipt.get("status") in {"ambiguous", "unknown_cost", "failed_before_dispatch"}:
        return False
    return True


def _cost_receipt(question_id, attempt_number, receipt, provenance):
    persistence = receipt.get("attempt_persistence") or {}
    dispatch = receipt.get("dispatch_result") or {}
    return {
        "schema_version": "demo-cost-receipt-v1",
        "question_id": question_id,
        "attempt_number": attempt_number,
        "status": receipt.get("status"),
        "source_run_id": persistence.get("run_id"),
        "ask_id": persistence.get("ask_id"),
        "model_usd": receipt.get("model_usd"),
        "model_usd_ceiling_micros": receipt.get("model_usd_ceiling_micros"),
        "recorded_model_usd": persistence.get("recorded_model_usd"),
        "recorded_model_usd_ceiling_micros": persistence.get("recorded_model_usd_ceiling_micros"),
        "reservation_release_usd": persistence.get("reservation_release_usd"),
        "reservation_release_ceiling_micros": persistence.get("reservation_release_ceiling_micros"),
        "guarded_call_costs": dispatch.get("call_costs") if isinstance(dispatch, dict) else None,
        "provider_invoice_status": "unproven",
        "source_commit": provenance.get("source_commit"),
        "source_hashes": provenance.get("source_hashes"),
        "source_ancestor_proof": provenance.get("source_ancestor_proof"),
        "approval_sha256": provenance.get("approval_sha256"),
        "archived_report_sha256": provenance.get("archived_report_sha256"),
    }


def _raw_digest(raw):
    if not isinstance(raw, dict):
        raise OperatorRefused("raw_attempt_missing")
    try:
        attempt_number = raw.get("attempt_number", raw.get("attempt"))
        if attempt_number not in (1, 2) or isinstance(attempt_number, bool):
            raise ValueError
        if not isinstance(raw.get("question_id"), str) or not isinstance(raw.get("run_id"), str):
            raise ValueError
    except (AttributeError, ValueError):
        raise OperatorRefused("raw_attempt_identity_invalid") from None
    return raw


def run_operator(runtime_factory=None, *, execute=False, recover_existing_only=False,
                 data_dir=DATA, inputs_dir=INPUTS, repo_root=ROOT):
    if execute and recover_existing_only:
        raise OperatorRefused("operator_modes_are_mutually_exclusive")
    previous_provider = os.environ.get("MODEL_PROVIDER")
    os.environ["MODEL_PROVIDER"] = "gemini"
    try:
        from core.eval import demo_pairs

        factory = runtime_factory or production_runtime_factory
        runtime = factory(repo_root=Path(repo_root), data_dir=Path(data_dir), inputs_dir=Path(inputs_dir))
        prepared = runtime.prepare()
        if not isinstance(prepared, dict) or prepared.get("verified") is not True:
            raise OperatorRefused("prepare_unverified")
        _validate_anchor_contexts(prepared.get("anchor_contexts"), demo_pairs.DEMO_QUESTIONS)
        base = {"mode": "recover_existing_only" if recover_existing_only else "execute" if execute else "prepare",
                "questions_sha256": prepared.get("questions_sha256"),
                "source_receipt_sha256": prepared.get("source_receipt_sha256"),
                "archived_report_sha256": prepared.get("archived_report_sha256"),
                "approval_sha256": prepared.get("approval_sha256"),
                "source_commit": prepared.get("source_commit"),
                "source_hashes": prepared.get("source_hashes"),
                "source_ancestor_proof": prepared.get("source_ancestor_proof"),
                "anchor_ids": sorted({source_id for q in demo_pairs.DEMO_QUESTIONS for source_id in q.source_ids})}
        if not execute:
            if recover_existing_only:
                authorize = getattr(runtime, "authorize_execute", None)
                if not callable(authorize) or authorize(prepared) is not True:
                    raise OperatorRefused("recovery_authority_unverified")
                recovery = runtime.recover_existing_only(prepared)
                return {**base, "status": "recovered", "recovery": recovery}
            return {**base, "status": "prepared"}
        authorize = getattr(runtime, "authorize_execute", None)
        if not callable(authorize) or authorize(prepared) is not True:
            raise OperatorRefused("execution_authority_unverified")
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        funding = runtime.transfer_funding(prepared)
        if not isinstance(funding, dict) or funding.get("verified") is not True:
            raise OperatorRefused("funding_transfer_unverified")
        all_attempts = []
        pair_results = []
        stop_reason = None
        for question in demo_pairs.DEMO_QUESTIONS:
            pair = []
            for attempt_number in (1, 2):
                fresh = runtime.refresh_funding(funding, all_attempts)
                if not isinstance(fresh, dict) or fresh.get("verified") is not True:
                    stop_reason = "funding_phase_readback_unverified"
                    break
                funding = fresh
                receipt = runtime.run_attempt(
                    question, attempt_number, funding, prepared["anchor_contexts"][question.id])
                if not isinstance(receipt, dict):
                    stop_reason = "attempt_receipt_invalid"
                    break
                all_attempts.append(receipt)
                pair.append(receipt)
                cost_path = Path(data_dir) / f"{question.id}-attempt-{attempt_number}.cost.json"
                if not receipt.get("_recovered_existing"):
                    _write_exclusive(cost_path, _canonical_bytes(
                        _cost_receipt(question.id, attempt_number, receipt, prepared)))
                if not _attempt_is_verified(receipt):
                    stop_reason = "attempt_accounting_unverified"
                    break
            if stop_reason:
                break
            if len(pair) != 2:
                stop_reason = "pair_incomplete"
                break
            raw_pair = [_raw_digest(runtime.read_raw_attempt(row)) for row in pair]
            if any(raw.get("question_id") != question.id for raw in raw_pair):
                raise OperatorRefused("raw_pair_question_mismatch")
            selection = runtime.select_pair(question.id, raw_pair[0], raw_pair[1])
            if not isinstance(selection, dict) or selection.get("question_id") != question.id:
                raise OperatorRefused("pair_selection_unverified")
            saved_work = None
            if selection.get("status") == "selected" and selection.get("selected_attempt") in (1, 2):
                saved_work = runtime.create_saved_work(question.id, selection, raw_pair, pair)
            manifest = runtime.l5_manifest(question.id, selection, saved_work)
            if (not isinstance(manifest, dict) or manifest.get("accepted_ids") != []
                    or manifest.get("status") != "pending_l5"):
                raise OperatorRefused("l5_handoff_must_start_pending")
            accepted = runtime.read_l5_acceptance(question.id, selection, saved_work)
            if accepted is not None and (not isinstance(accepted, dict) or accepted.get("verified") is not True):
                raise OperatorRefused("l5_acceptance_unverified")
            handoff = {
                "schema_version": "demo-pair-handoff-v1",
                "question_id": question.id,
                "selected_attempt": selection.get("selected_attempt"),
                "selection_status": selection.get("status"),
                "stability_status": selection.get("stability_status"),
                "run_ids": selection.get("run_ids"),
                "raw_hashes": selection.get("raw_hashes"),
                "candidate_claim_ids": selection.get("admitted_claim_ids"),
                "source_commit": prepared.get("source_commit"),
                "source_hashes": prepared.get("source_hashes"),
                "source_ancestor_proof": prepared.get("source_ancestor_proof"),
                "approval_sha256": prepared.get("approval_sha256"),
                "archived_report_sha256": prepared.get("archived_report_sha256"),
                "l5_status": "pending_l5" if accepted is None else "accepted",
                "accepted_l5_ids": [] if accepted is None else accepted["accepted_ids"],
                "saved_work": saved_work,
                "app_relative_path": (saved_work or {}).get("app_relative_path") if isinstance(saved_work, dict) else None,
            }
            _write_exclusive(Path(data_dir) / f"{question.id}.handoff.json", _canonical_bytes(handoff))
            pair_results.append(handoff)
        return {**base, "status": "stopped" if stop_reason else "complete", "stop_reason": stop_reason,
                "attempts_recorded": len(all_attempts), "pairs": pair_results}
    finally:
        if previous_provider is None:
            os.environ.pop("MODEL_PROVIDER", None)
        else:
            os.environ["MODEL_PROVIDER"] = previous_provider


class _AgentClient:
    def __init__(self, session, base_url):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.last_dossier_readback = None

    def _url(self, path):
        if not isinstance(path, str) or not path.startswith("/"):
            raise OperatorRefused("app_path_invalid")
        return self.base_url + path

    def post(self, path, *, json):
        return self.session.post(self._url(path), json=json, timeout=REQUEST_TIMEOUT_SECONDS)

    def get(self, path):
        response = self.session.get(self._url(path), timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            body = response.json()
        except Exception:
            body = None
        self.last_dossier_readback = dict(body) if isinstance(body, dict) else None
        return response


class _ProductionRuntime:
    def __init__(self, repo_root, data_dir, inputs_dir):
        self.repo_root, self.data_dir, self.inputs_dir = map(Path, (repo_root, data_dir, inputs_dir))
        if self.data_dir.resolve() != DATA.resolve() or self.inputs_dir.resolve() != INPUTS.resolve():
            raise OperatorRefused("production_paths_must_be_fixed")
        self.modules = self.wiring = self.native = self.app_client = None
        self.approval = None
        self.frozen_ask_ids = set()
        self.frozen_dossier_ids = set()

    def _load(self):
        from core.eval import ask_r2

        identity = ask_r2._verify_builder_adc()
        if identity.get("principal") != ask_r2.BUILDER_EMAIL or identity.get("project") != ask_r2.PROJECT:
            raise OperatorRefused("builder_identity_mismatch")
        modules = ask_r2._load_modules(self.repo_root)
        if modules.ask.provider() != "gemini" or getattr(modules.no_retry_model, "retries", None) != 0:
            raise OperatorRefused("model_provider_contract_mismatch")
        from core.eval import demo_native_accounting
        from core.eval.demo_native_accounting import DemoNativeAccounting
        import google.auth
        from google.auth.impersonated_credentials import IDTokenCredentials
        from google.auth.transport.requests import AuthorizedSession
        from google.cloud import bigquery

        credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if project != ask_r2.PROJECT or getattr(credentials, "_target_principal", None) != ask_r2.BUILDER_EMAIL:
            raise OperatorRefused("builder_identity_mismatch")
        token_credentials = IDTokenCredentials(credentials, target_audience=AGENT_URI, include_email=True)
        session = AuthorizedSession(token_credentials, max_refresh_attempts=0)
        health = session.get(AGENT_URI + "/health", timeout=REQUEST_TIMEOUT_SECONDS)
        if health.status_code != 200:
            raise OperatorRefused("agent_health_unverified")
        body = health.json()
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise OperatorRefused("agent_health_unverified")
        self.health_proof = {"http_status": health.status_code, "ok": body.get("ok"),
                             "service": body.get("service"), "version": body.get("version")}
        access = _read_object(self.inputs_dir / AGENT_ACCESS_NAME, "agent_access_proof_invalid")
        if (access.get("result") != "reachable" or access.get("agent_uri") != AGENT_URI
                or access.get("agent_health_http_status") != 200 or access.get("builder_identity_verified") is not True):
            raise OperatorRefused("agent_access_proof_invalid")

        client = bigquery.Client(project=project, credentials=credentials)

        def job_config(sql, params):
            config = bigquery.QueryJobConfig()
            config.maximum_bytes_billed = MAX_BYTES_BILLED
            if sql == demo_native_accounting.INSERT_ROWS_SQL:
                config.query_parameters = [bigquery.ArrayQueryParameter("rows_json", "STRING", params["rows_json"])]
            elif sql == demo_native_accounting.EXACT_ROWS_SQL:
                config.query_parameters = [bigquery.ArrayQueryParameter("run_ids", "STRING", params["run_ids"])]
            elif sql == demo_native_accounting.ASK_ID_ROWS_SQL:
                config.query_parameters = [bigquery.ScalarQueryParameter("ask_id", "STRING", params["ask_id"])]
            elif sql == demo_native_accounting.NET_SQL:
                config.query_parameters = [bigquery.ArrayQueryParameter("run_ids", "STRING", params["run_ids"])]
            elif sql == demo_native_accounting.DAILY_SPEND_SQL:
                config.query_parameters = [bigquery.ScalarQueryParameter("day", "DATE", params["day"])]
            else:
                raise OperatorRefused("native_sql_outside_allowlist")
            return config

        def query(sql, params):
            config = job_config(sql, params)
            job = client.query(sql, job_config=config, retry=None, job_retry=None)
            result = job.result(timeout=REQUEST_TIMEOUT_SECONDS, retry=None, job_retry=None)
            expected_rows = result.total_rows
            rows = list(result)
            if expected_rows != len(rows):
                raise OperatorRefused("native_query_pages_incomplete")
            return [dict(row) for row in rows]

        def execute(sql, params):
            if sql != demo_native_accounting.INSERT_ROWS_SQL:
                raise OperatorRefused("native_dml_outside_allowlist")
            payloads = params.get("rows_json")
            if not isinstance(payloads, list) or not payloads:
                raise OperatorRefused("native_dml_rows_invalid")
            job_id = "l3_demo_" + _sha(_canonical_bytes({"sql": sql, "rows": payloads}))[:32]
            job = client.query(sql, job_config=job_config(sql, params), job_id=job_id, retry=None, job_retry=None)
            job.result(timeout=REQUEST_TIMEOUT_SECONDS, retry=None, job_retry=None)
            if job.num_dml_affected_rows != len(payloads):
                raise OperatorRefused("native_dml_row_count_mismatch")

        def claim_once(key):
            path = self.data_dir / "native-claims" / (_sha(key.encode("utf-8")) + ".claim")
            try:
                _write_exclusive(path, _canonical_bytes({"claim": key}))
            except FileExistsError:
                return False
            return True

        def daily_cap(day):
            now = datetime.combine(day, time(12, 0), tzinfo=modules.staging.SAST)
            return modules.ask.model_daily_usd(now=now)

        self.modules = modules
        self.wiring = modules.staging.real_wiring()
        self.native = DemoNativeAccounting(execute=execute, query=query, claim_once=claim_once,
                                           daily_cap_for_day=daily_cap)
        self.agent_session = session
        self.app_client = _AgentClient(session, AGENT_URI)
        self.authority = identity

    def prepare(self):
        from core.eval import ask_r2, demo_pairs

        self.approval = _verify_approval(self.inputs_dir, demo_pairs.DEMO_QUESTIONS_SHA256)
        self._load()
        now = self.wiring.now()
        windows = [self.modules.ask._window(question.text, now) for question in demo_pairs.DEMO_QUESTIONS]
        if not windows or any(window != windows[0] for window in windows):
            raise OperatorRefused("anchor_window_mismatch")
        start, end = windows[0]
        deps = self.wiring.ask_deps()
        warehouse = ask_r2.ReadOnlyWarehouse(deps.warehouse, self.modules.sql.check_sql)
        from core.agent.context import RunContext

        ids = list(dict.fromkeys(source_id for q in demo_pairs.DEMO_QUESTIONS for source_id in q.source_ids))
        ctx = RunContext(run_id="demo-preflight-20261001", tier="T1", as_of=now, window_start=start,
                         window_end=end)
        lookup = self.modules.warehouse.fetch_posts(ctx, warehouse, ids, (start, end))
        evidence = lookup.get("evidence") if isinstance(lookup, dict) else None
        by_id = {post.get("id"): post for post in evidence or [] if isinstance(post, dict)}
        if set(by_id) != set(ids) or len(by_id) != len(ids):
            raise OperatorRefused("anchor_source_missing_or_outside_window")
        contexts = {}
        for question in demo_pairs.DEMO_QUESTIONS:
            contexts[question.id] = {"market": question.market, "source_ids": list(question.source_ids),
                                     "support_run_id": question.support_run_id,
                                     "posts": [by_id[item] for item in question.source_ids],
                                     "provenance": {"kind": "fresh_staging_prefetch", "query_id": lookup.get("query_id"),
                                                    "window": [start.isoformat(), end.isoformat()]}}
        from core.eval import demo_native_accounting

        native_baseline = self.native._fetch_rows(demo_pairs.BASELINE_RUN_IDS)
        baseline, source_sha = demo_native_accounting._approved_source_rows(native_baseline)
        daily = self.fresh_daily_readback(now.astimezone(self.modules.staging.SAST).date())
        if not daily.get("verified") or daily.get("all_pages_consumed") is not True:
            raise OperatorRefused("daily_readback_unverified")
        plan = demo_pairs.prepare_funding_transfer(
            run_date=now.astimezone(self.modules.staging.SAST).date(),
            daily_headroom_micros=daily["daily_cap_micros"] - daily["canonical_total_micros"],
            source_proof_sha=source_sha, source_rows=baseline, unknown_rows=baseline)
        source_proof = self._verify_committed_sources()
        return {"verified": True, "anchor_contexts": contexts, "funding_plan": plan,
                "questions_sha256": self.approval["questions_sha256"],
                "source_receipt_sha256": self.approval["source_receipt_sha256"],
                "approval_sha256": self.approval["approval_sha256"],
                "archived_report_sha256": self.approval["archived_report_sha256"],
                "committed_sources": source_proof["clean"], "source_commit": source_proof["head"],
                "source_hashes": source_proof["hashes"], "source_ancestor_proof": source_proof["ancestors"],
                "health_proof": self.health_proof,
                "run_date": now.astimezone(self.modules.staging.SAST).date().isoformat()}

    def _verify_committed_sources(self):
        import subprocess
        from core.eval.ask_r2 import _git_executable

        git = _git_executable()
        result = subprocess.run([git, "status", "--porcelain", "--untracked-files=no"], cwd=self.repo_root,
                                capture_output=True, text=True, encoding="utf-8", timeout=REQUEST_TIMEOUT_SECONDS)
        head = subprocess.run([git, "rev-parse", "HEAD"], cwd=self.repo_root, capture_output=True,
                              text=True, encoding="utf-8", timeout=REQUEST_TIMEOUT_SECONDS)
        if result.returncode != 0 or result.stdout.strip() or head.returncode != 0:
            return {"clean": False, "head": None, "hashes": {}, "ancestors": {}}
        source_hashes = {}
        for name in LIVE_SOURCE_FILES:
            tracked = subprocess.run([git, "ls-files", "--error-unmatch", name], cwd=self.repo_root,
                                     capture_output=True, text=True, encoding="utf-8", timeout=REQUEST_TIMEOUT_SECONDS)
            if tracked.returncode:
                return {"clean": False, "head": head.stdout.strip(), "hashes": {}, "ancestors": {}}
            changed = subprocess.run([git, "status", "--porcelain", "--", name], cwd=self.repo_root,
                                     capture_output=True, text=True, encoding="utf-8", timeout=REQUEST_TIMEOUT_SECONDS)
            if changed.returncode or changed.stdout.strip():
                return {"clean": False, "head": head.stdout.strip(), "hashes": {}, "ancestors": {}}
            blob = subprocess.run([git, "rev-parse", f"HEAD:{name}"], cwd=self.repo_root,
                                  capture_output=True, text=True, encoding="utf-8", timeout=REQUEST_TIMEOUT_SECONDS)
            clean_hash = subprocess.run([git, "hash-object", "--path", name, name], cwd=self.repo_root,
                                        capture_output=True, text=True, encoding="utf-8", timeout=REQUEST_TIMEOUT_SECONDS)
            if blob.returncode or clean_hash.returncode or blob.stdout.strip() != clean_hash.stdout.strip():
                return {"clean": False, "head": head.stdout.strip(), "hashes": {}, "ancestors": {}}
            source_hashes[name] = _sha((self.repo_root / name).read_bytes())
        from core.eval.ask_r2 import REQUIRED_ANCESTORS

        ancestors = {}
        for ancestor in REQUIRED_ANCESTORS:
            result = subprocess.run([git, "merge-base", "--is-ancestor", ancestor, head.stdout.strip()],
                                    cwd=self.repo_root, capture_output=True, timeout=REQUEST_TIMEOUT_SECONDS)
            ancestors[ancestor] = result.returncode == 0
            if result.returncode:
                return {"clean": False, "head": head.stdout.strip(), "hashes": {}, "ancestors": ancestors}
        return {"clean": True, "head": head.stdout.strip(), "hashes": source_hashes, "ancestors": ancestors}

    def authorize_execute(self, prepared):
        if prepared.get("committed_sources") is not True:
            return False
        from core.eval import ask_r2, demo_pairs

        sources = self._verify_committed_sources()
        if (not sources["clean"] or sources["head"] != prepared.get("source_commit")
                or sources["hashes"] != prepared.get("source_hashes")
                or sources["ancestors"] != prepared.get("source_ancestor_proof")):
            return False
        approval = _verify_approval(self.inputs_dir, demo_pairs.DEMO_QUESTIONS_SHA256)
        if (approval["questions_sha256"] != prepared.get("questions_sha256")
                or approval["source_receipt_sha256"] != prepared.get("source_receipt_sha256")
                or approval["approval_sha256"] != prepared.get("approval_sha256")
                or approval["archived_report_sha256"] != prepared.get("archived_report_sha256")):
            return False
        identity = ask_r2._verify_builder_adc()
        if identity.get("principal") != ask_r2.BUILDER_EMAIL or identity.get("project") != ask_r2.PROJECT:
            return False
        now = self.wiring.now()
        day = now.astimezone(self.modules.staging.SAST).date()
        if day != demo_pairs.DEMO_RUN_DATE or prepared.get("run_date") != day.isoformat():
            return False
        daily = self.fresh_daily_readback(day)
        plan = prepared.get("funding_plan") or {}
        if (daily.get("verified") is not True or daily.get("all_pages_consumed") is not True
                or daily.get("run_date") != day.isoformat()
                or type(plan.get("amount_micros")) is not int
                or plan["amount_micros"] <= 0
                or plan["amount_micros"] > daily["daily_cap_micros"] - daily["canonical_total_micros"]):
            return False
        health = self.agent_session.get(AGENT_URI + "/health", timeout=REQUEST_TIMEOUT_SECONDS)
        if health.status_code != 200 or health.json().get("ok") is not True:
            return False
        return True

    def recover_existing_only(self, prepared):
        from core.eval import demo_native_accounting, demo_pairs, demo_scope, demo_saved_work
        from core.eval.demo_native_accounting import DemoNativeAccounting

        question_id, attempt_number = "DEMO-01", 1
        attempt_key = demo_pairs._attempt_key(question_id, attempt_number)
        receipt_path = self.data_dir / f"{attempt_key}.json"
        marker_path = self.data_dir / f"{attempt_key}.attempted"
        recovery_path = self.data_dir / f"{attempt_key}.recovery.json"
        if not marker_path.is_file() or not receipt_path.is_file():
            raise OperatorRefused("existing_attempt_marker_or_receipt_missing")
        if recovery_path.exists():
            sidecar = _read_object(recovery_path, "existing_recovery_receipt_invalid")
            outer_bytes = receipt_path.read_bytes()
            if (sidecar.get("attempt_key") != attempt_key
                    or sidecar.get("original_receipt_sha256") != _sha(outer_bytes)):
                raise OperatorRefused("existing_recovery_receipt_hash_mismatch")
            return {"attempt_key": attempt_key, "recovery_receipt_path": str(recovery_path),
                    "run_id": sidecar.get("run_id"), "status": "recovered"}

        outer_bytes = receipt_path.read_bytes()
        outer = _read_object(receipt_path, "attempt_receipt_invalid")
        marker = _read_object(marker_path, "attempt_marker_invalid")
        if (outer.get("status") != "ambiguous" or outer.get("failure_type") != "UncertainWriteError"
                or outer.get("question_id") != question_id or outer.get("attempt_number") != attempt_number
                or outer.get("attempt_key") != attempt_key
                or marker.get("attempt_key") != attempt_key
                or marker.get("prompt_source_sha256") != demo_pairs.DEMO_QUESTIONS_SHA256):
            raise OperatorRefused("existing_attempt_not_recoverable")
        dispatch = outer.get("dispatch_result")
        if not isinstance(dispatch, dict):
            raise OperatorRefused("existing_dispatch_receipt_missing")
        raw_path = Path(dispatch.get("raw_receipt_path", ""))
        if not raw_path.is_absolute():
            raw_path = self.data_dir / raw_path
        raw_path = raw_path.resolve(strict=True)
        if self.data_dir.resolve() not in raw_path.parents:
            raise OperatorRefused("raw_receipt_path_outside_phase")
        raw_bytes = raw_path.read_bytes()
        raw_sha = _sha(raw_bytes)
        if raw_sha != dispatch.get("raw_receipt_sha256"):
            raise OperatorRefused("raw_receipt_hash_mismatch")
        raw = _raw_digest(json.loads(raw_bytes.decode("utf-8")))
        if (not isinstance(dispatch.get("raw_receipt"), dict)
                or _canonical_bytes(dispatch["raw_receipt"]) != raw_bytes):
            raise OperatorRefused("raw_receipt_dispatch_binding_mismatch")
        app_record = raw.get("app_record")
        run = app_record.get("run") if isinstance(app_record, dict) else None
        if (raw.get("question_id") != question_id or raw.get("attempt_number") != attempt_number
                or raw.get("attempt_key") != attempt_key or not isinstance(run, dict)
                or raw.get("run_id") != run.get("run_id") or raw.get("unknown_cost") is not False):
            raise OperatorRefused("raw_attempt_recovery_identity_invalid")
        budget = outer.get("budget")
        if not isinstance(budget, dict):
            raise OperatorRefused("original_budget_missing")
        charged = budget.get("charged_micros")
        reserved = budget.get("reserved_micros")
        cap = outer.get("attempt_cap_micros")
        statuses = [call.get("status") for call in budget.get("calls", []) if isinstance(call, dict)]
        allowed_statuses = {"charged_known", "charged_conservative_ceiling"}
        if (type(charged) is not int or type(reserved) is not int or reserved != 0
                or type(cap) is not int or charged <= 0 or charged > cap
                or budget.get("stop_reason") is not None
                or not statuses or any(status not in allowed_statuses for status in statuses)
                or len(statuses) != len(budget.get("calls", []))
                or outer.get("guarded_charge_micros") != cap):
            raise OperatorRefused("original_budget_not_settled_for_recovery")

        funding_path = self.data_dir / "funding" / "transfer.json"
        funding_bytes = funding_path.read_bytes()
        funding = _read_object(funding_path, "funding_transfer_missing")
        plan = prepared.get("funding_plan") or {}
        if (funding.get("verified") is not True or funding.get("run_date") != prepared.get("run_date")
                or funding.get("source_proof_sha") != plan.get("source_proof_sha")
                or funding.get("allocated_micros") != plan.get("amount_micros")):
            raise OperatorRefused("persisted_funding_proof_mismatch")
        native_rows = self.native._fetch_rows(demo_pairs.BASELINE_RUN_IDS)
        _, current_source_sha = demo_native_accounting._approved_source_rows(native_rows)
        if current_source_sha != funding.get("source_proof_sha"):
            raise OperatorRefused("funding_source_readback_mismatch")
        day = date.fromisoformat(funding["run_date"])
        daily = self.fresh_daily_readback(day)
        if (daily.get("verified") is not True or daily.get("all_pages_consumed") is not True
                or daily.get("canonical_total_micros", 0) > daily.get("daily_cap_micros", 0)):
            raise OperatorRefused("daily_readback_unverified")
        run_id = raw["run_id"]
        phase_state = {**funding, "app_run_ids": [run_id]}
        phase_rows, consumed, phase_ids = self.native._phase_readback(phase_state)
        credit_id = demo_native_accounting._credit_run_id(run_id)
        ask_row = phase_rows.get(run_id)
        credit_row = phase_rows.get(credit_id)
        if not isinstance(ask_row, dict) or not isinstance(credit_row, dict):
            raise OperatorRefused("native_attempt_rows_missing")
        credit_counts = demo_native_accounting._counts(credit_row)
        native_guard = credit_counts.get("guarded_charge_micros")
        if (type(native_guard) is not int or native_guard != charged or consumed != native_guard
                or native_guard > cap or phase_state.get("native_net_micros") != 25_000_000):
            raise OperatorRefused("native_attempt_guard_mismatch")

        def deny_write(*_args, **_kwargs):
            raise OperatorRefused("recovery_write_forbidden")

        readonly_native = DemoNativeAccounting(
            execute=deny_write, query=self.native._query, claim_once=deny_write,
            daily_cap_for_day=self.native._daily_cap_for_day)
        assessment = demo_scope.assess_answer(raw, question_id)
        candidates = assessment.get("admitted_claim_ids", [])
        answer = raw.get("raw_answer")
        safe_payload = None
        admitted = []
        if candidates:
            if not isinstance(answer, dict) or not isinstance(answer.get("claims"), list):
                raise OperatorRefused("w2_answer_missing")
            claims_by_id = {}
            for claim in answer["claims"]:
                if not isinstance(claim, dict) or not isinstance(claim.get("id"), str):
                    raise OperatorRefused("w2_claim_identity_invalid")
                claims_by_id.setdefault(claim["id"], []).append(claim)
            if (not isinstance(candidates, list) or len(set(candidates)) != len(candidates)
                    or any(len(claims_by_id.get(item, [])) != 1 for item in candidates)):
                raise OperatorRefused("w2_admitted_claims_ambiguous")
            selected_claims = [claims_by_id[item][0] for item in candidates]
            evidence_ids = list(dict.fromkeys(evidence_id for claim in selected_claims
                                              for evidence_id in claim.get("evidence_ids", [])
                                              if isinstance(evidence_id, str)))
            evidence_by_id = {}
            for item in answer.get("evidence", []):
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    evidence_by_id.setdefault(item["id"], []).append(item)
            if any(len(evidence_by_id.get(item, [])) != 1 for item in evidence_ids):
                raise OperatorRefused("w2_evidence_missing_or_ambiguous")
            admitted = list(candidates)
            safe_payload = {"question_id": question_id, "claims": selected_claims,
                            "evidence": [row for row in answer["evidence"]
                                         if isinstance(row, dict) and row.get("id") in evidence_ids]}
        saved = demo_saved_work.persist_attempt_view(
            selected_receipt=raw, app_record=app_record, safe_payload=safe_payload,
            admitted_claim_ids=admitted, raw_receipt_bytes=raw_bytes, raw_receipt_sha256="sha256:" + raw_sha,
            funding=funding, guarded_charge_usd=format(Decimal(native_guard) / 1_000_000, "f"),
            native_adapter=readonly_native, frozen_ask_ids=frozenset())
        prefixed_hash = saved.get("record_sha256")
        if (not isinstance(prefixed_hash, str) or not prefixed_hash.startswith("sha256:")
                or not _HEX64.fullmatch(prefixed_hash.removeprefix("sha256:"))):
            raise OperatorRefused("w6_record_hash_invalid")
        record_hash = prefixed_hash.removeprefix("sha256:")
        saved_readback = saved.get("readback")
        if (saved.get("status") != "resumed" or saved.get("ask_id") != app_record.get("ask_id")
                or saved.get("run_id") != run_id or saved.get("native_net_micros") != 25_000_000
                or saved.get("ask_row_sha256") != demo_native_accounting.run_row_sha256(ask_row)
                or not isinstance(saved_readback, dict) or saved_readback.get("run_id") != run_id
                or saved_readback.get("ask_id") != app_record.get("ask_id")):
            raise OperatorRefused("existing_w6_readback_unverified")
        exact = saved.get("recorded_model_usd")
        release = saved.get("reservation_release_usd")
        ceiling = saved.get("recorded_model_usd_ceiling_micros")
        release_ceiling = saved.get("reservation_release_ceiling_micros")
        w5_readback = {
            "match": True, "run_id": run_id, "record_sha256": record_hash,
            "recorded_model_usd": exact, "reservation_release_usd": release,
            "recorded_model_usd_ceiling_micros": ceiling,
            "reservation_release_ceiling_micros": release_ceiling,
            "native_net_micros": saved["native_net_micros"],
        }
        persistence = {
            "verified": True, "ask_id": saved["ask_id"], "run_id": run_id,
            "record_sha256": record_hash, "guarded_charge_micros": native_guard,
            "recorded_model_usd": exact, "reservation_release_usd": release,
            "recorded_model_usd_ceiling_micros": ceiling,
            "reservation_release_ceiling_micros": release_ceiling,
            "native_net_micros": saved["native_net_micros"], "readback": w5_readback,
        }
        w6_bytes = _canonical_bytes({"checks": {"persist_existing": saved}})
        w6_path = self.data_dir / "w6" / f"{attempt_key}.json"
        try:
            _write_exclusive(w6_path, w6_bytes)
        except FileExistsError:
            if w6_path.read_bytes() != w6_bytes:
                raise OperatorRefused("existing_w6_receipt_changed") from None
        phase = {
            "verified": True, "phase_id": funding["phase_id"], "run_date": funding["run_date"],
            "native_net_micros": 25_000_000,
            "app_run_ids": [run_id], "consumed_micros": consumed,
            "ask_run_id": run_id, "credit_run_id": credit_id,
            "ask_row_sha256": saved["ask_row_sha256"],
            "credit_row_sha256": demo_native_accounting._ask_row_hash(credit_row),
            "guarded_charge_micros": native_guard,
            "reservation_run_id": funding["reservation_run_id"],
            "allocated_micros": funding["allocated_micros"],
            "source_proof_sha": funding["source_proof_sha"],
            "baseline_run_ids": funding["baseline_run_ids"],
            "phase_run_ids": phase_ids,
        }
        recovery = {
            "schema_version": "demo-attempt-recovery-v1", "attempt_key": attempt_key,
            "question_id": question_id, "attempt_number": attempt_number,
            "status": dispatch.get("status") or dispatch.get("outcome"),
            "failure_type": "UncertainWriteError", "run_id": run_id,
            "original_receipt_sha256": _sha(outer_bytes), "raw_receipt_sha256": raw_sha,
            "guarded_charge_micros": native_guard,
            "original_guarded_charge_micros": outer["guarded_charge_micros"],
            "attempt_cap_micros": cap, "model_usd": exact,
            "model_usd_ceiling_micros": ceiling, "readback": w5_readback,
            "attempt_persistence": persistence, "unknown_cost": False,
            "call_statuses": statuses, "w6_sha256": saved["record_sha256"],
            "w6_sidecar_path": str(w6_path), "w6_sidecar_sha256": _sha(w6_bytes),
            "funding_proof_path": str(funding_path), "funding_proof": funding,
            "funding_proof_sha256": _sha(funding_bytes),
            "native_phase_readback": phase,
        }
        recovery_bytes = _canonical_bytes(recovery)
        try:
            _write_exclusive(recovery_path, recovery_bytes)
        except FileExistsError:
            if recovery_path.read_bytes() != recovery_bytes:
                raise OperatorRefused("recovery_receipt_already_frozen") from None
        return {"attempt_key": attempt_key, "recovery_receipt_path": str(recovery_path),
                "run_id": run_id, "guarded_charge_micros": native_guard,
                "w6_sidecar_path": str(w6_path), "status": "recovered"}

    def fresh_daily_readback(self, day):
        from core.eval.demo_native_accounting import DAILY_SPEND_SQL

        rows = self.native._query(DAILY_SPEND_SQL, {"day": day})
        if len(rows) != 1:
            raise OperatorRefused("daily_aggregate_invalid")
        amount = Decimal(str(rows[0]["usd"]))
        micros = int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
        cap = Decimal(str(self.modules.ask.model_daily_usd(
            now=datetime.combine(day, time(12, 0), tzinfo=self.modules.staging.SAST))))
        cap_micros = int((cap * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
        return {"verified": True, "run_date": day.isoformat(), "all_pages_consumed": True,
                "canonical_rows": [{"run_id": "__daily_total__", "run_date": day.isoformat(),
                                    "model_usd_micros": micros}],
                "canonical_total_micros": micros, "daily_cap_micros": cap_micros}

    def transfer_funding(self, prepared):
        from core.eval import demo_pairs

        plan = prepared["funding_plan"]
        proof_path = self.data_dir / "funding" / "transfer.json"
        if proof_path.exists():
            funding = _read_object(proof_path, "native_funding_proof_invalid")
            if (funding.get("verified") is not True
                    or funding.get("run_date") != prepared["run_date"]
                    or funding.get("source_proof_sha") != plan["source_proof_sha"]
                    or funding.get("allocated_micros") != plan["amount_micros"]
                    or funding.get("native_net_micros") != demo_pairs.BASELINE_BOOKED_MICROS
                    or funding.get("baseline_run_ids") != list(demo_pairs.BASELINE_RUN_IDS)
                    or funding.get("app_run_ids") != []):
                raise OperatorRefused("persisted_funding_proof_mismatch")
            _, consumed, _ = self.native._phase_readback(funding)
            if consumed != funding.get("consumed_micros"):
                raise OperatorRefused("persisted_funding_consumption_mismatch")
            return funding
        transfer = self.native.transfer_unspent(
            amount_usd=Decimal(plan["amount_micros"]) / 1_000_000,
            source_date=demo_pairs.SOURCE_RUN_DATE,
            run_date=prepared["run_date"],
            baseline_run_ids=demo_pairs.BASELINE_RUN_IDS,
            source_proof_sha=plan["source_proof_sha"],
            native_net_micros=demo_pairs.BASELINE_BOOKED_MICROS)
        funding = transfer.get("funding") if isinstance(transfer, dict) else None
        if (not isinstance(funding, dict) or funding.get("verified") is not True
                or transfer.get("status") not in ("stored", "resumed")
                or funding.get("run_date") != prepared["run_date"]
                or funding.get("source_proof_sha") != plan["source_proof_sha"]
                or funding.get("allocated_micros") != plan["amount_micros"]
                or funding.get("consumed_micros") != 0
                or funding.get("native_net_micros") != demo_pairs.BASELINE_BOOKED_MICROS
                or funding.get("baseline_run_ids") != list(demo_pairs.BASELINE_RUN_IDS)
                or funding.get("app_run_ids") != []):
            raise OperatorRefused("native_transfer_funding_proof_mismatch")
        proof_bytes = _canonical_bytes(funding)
        try:
            _write_exclusive(proof_path, proof_bytes)
        except FileExistsError:
            if proof_path.read_bytes() != proof_bytes:
                raise OperatorRefused("native_funding_proof_already_frozen") from None
        return funding

    def refresh_funding(self, funding, attempts):
        app_ids = [row["attempt_persistence"]["run_id"] for row in attempts]
        state = {**funding, "app_run_ids": app_ids}
        _, consumed, _ = self.native._phase_readback(state)
        expected = sum(row.get("guarded_charge_micros", 0) for row in attempts)
        if consumed != expected:
            raise OperatorRefused("funding_consumption_readback_mismatch")
        self.fresh_daily_readback(date.fromisoformat(funding["run_date"]))
        return {**state, "consumed_micros": consumed}

    def run_attempt(self, question, attempt_number, funding, context, *, profile=None,
                    attempt_data_dir=None, prior_data_dir=None, ranking_proof_bytes=None, source_commit=None):
        from core.eval import demo_pairs, demo_dispatch, demo_scope, demo_saved_work

        artifact_dir = Path(attempt_data_dir) if attempt_data_dir is not None else self.data_dir
        prior_dir = Path(prior_data_dir) if prior_data_dir is not None else self.data_dir
        attempt_key = demo_pairs._attempt_key(question.id, attempt_number)
        marker_path = artifact_dir / f"{attempt_key}.attempted"
        receipt_path = artifact_dir / f"{attempt_key}.json"
        if marker_path.exists() or receipt_path.exists():
            if profile is not None:
                raise OperatorRefused("ranked_attempt_already_started")
            if not marker_path.is_file() or not receipt_path.is_file():
                raise OperatorRefused("existing_attempt_marker_or_receipt_incomplete")
            recovered = next((item for item in demo_pairs._existing_receipts(self.data_dir)
                              if item.get("attempt_key") == attempt_key), None)
            if (not isinstance(recovered, dict) or recovered.get("question_id") != question.id
                    or recovered.get("attempt_number") != attempt_number or not _attempt_is_verified(recovered)):
                raise OperatorRefused("existing_attempt_persistence_unverified")
            persistence = recovered["attempt_persistence"]
            run_id = persistence.get("run_id")
            guard = persistence.get("guarded_charge_micros")
            prior_ids = funding.get("app_run_ids")
            prior_consumed = funding.get("consumed_micros")
            if (not isinstance(run_id, str) or not run_id or type(guard) is not int or guard <= 0
                    or not isinstance(prior_ids, list) or type(prior_consumed) is not int or prior_consumed < 0
                    or any(not isinstance(item, str) or not item for item in prior_ids)):
                raise OperatorRefused("existing_attempt_funding_identity_invalid")
            expected_consumed = prior_consumed + guard
            phase_state = {**funding,
                           "app_run_ids": list(dict.fromkeys([*prior_ids, run_id])),
                           "consumed_micros": expected_consumed}
            _, consumed, _ = self.native._phase_readback(phase_state)
            if consumed != expected_consumed:
                raise OperatorRefused("recovered_phase_readback_mismatch")
            return {**recovered, "_recovered_existing": True}

        def dispatch(request, budget):
            dependencies = SimpleNamespace(modules=self.modules, wiring=self.wiring,
                                            fresh_daily_readback=self.fresh_daily_readback)
            if profile is None:
                dispatched = demo_dispatch.dispatch_attempt(request, budget, funding_proof=funding,
                                                            dependencies=dependencies)
            else:
                dispatched = demo_dispatch.dispatch_attempt(request, budget, funding_proof=funding,
                                                            dependencies=dependencies, profile=profile)
            raw = dict(dispatched) if isinstance(dispatched, dict) else {
                "dispatch_result_type": type(dispatched).__name__}
            raw.update({"question_id": request.question_id, "attempt": request.attempt_number,
                        "attempt_number": request.attempt_number, "attempt_key": request.attempt_key})
            raw.setdefault("answer", raw.get("raw_answer"))
            raw_bytes = _canonical_bytes(raw)
            raw_path = artifact_dir / "raw" / f"{request.attempt_key}.json"
            _write_exclusive(raw_path, raw_bytes)
            if not isinstance(dispatched, dict):
                raise OperatorRefused("dispatcher_receipt_invalid")
            if raw.get("id") != question.id or not isinstance(raw.get("run_id"), str):
                raise OperatorRefused("dispatcher_raw_identity_mismatch")
            result = dict(dispatched)
            result["raw_receipt"] = raw
            result["raw_receipt_path"] = str(raw_path)
            result["raw_receipt_sha256"] = _sha(raw_bytes)
            result.pop("raw_receipt_bytes", None)
            return result

        def persist(request, result):
            raw_path = Path(result.get("raw_receipt_path", ""))
            if not raw_path.is_file() or artifact_dir.resolve() not in raw_path.resolve().parents:
                raise OperatorRefused("raw_receipt_not_persisted")
            raw_bytes = raw_path.read_bytes()
            raw_sha = _sha(raw_bytes)
            if raw_sha != result.get("raw_receipt_sha256"):
                raise OperatorRefused("raw_receipt_hash_mismatch")
            raw = _raw_digest(json.loads(raw_bytes.decode("utf-8")))
            if raw != result.get("raw_receipt"):
                raise OperatorRefused("raw_receipt_parse_mismatch")
            assessment = demo_scope.assess_answer(raw, question.id)
            candidates = assessment.get("admitted_claim_ids", [])
            answer = raw.get("raw_answer")
            safe_payload = None
            admitted = []
            if candidates:
                if not isinstance(answer, dict) or not isinstance(answer.get("claims"), list):
                    raise OperatorRefused("w2_answer_missing")
                claims_by_id = {}
                for claim in answer["claims"]:
                    if not isinstance(claim, dict) or not isinstance(claim.get("id"), str):
                        raise OperatorRefused("w2_claim_identity_invalid")
                    claims_by_id.setdefault(claim["id"], []).append(claim)
                if (not isinstance(candidates, list) or len(set(candidates)) != len(candidates)
                        or any(len(claims_by_id.get(item, [])) != 1 for item in candidates)):
                    raise OperatorRefused("w2_admitted_claims_ambiguous")
                selected_claims = [claims_by_id[item][0] for item in candidates]
                evidence_ids = list(dict.fromkeys(
                    evidence_id for claim in selected_claims
                    for evidence_id in claim.get("evidence_ids", []) if isinstance(evidence_id, str)))
                evidence_by_id = {}
                for evidence in answer.get("evidence", []):
                    if isinstance(evidence, dict) and isinstance(evidence.get("id"), str):
                        evidence_by_id.setdefault(evidence["id"], []).append(evidence)
                if any(len(evidence_by_id.get(item, [])) != 1 for item in evidence_ids):
                    raise OperatorRefused("w2_evidence_missing_or_ambiguous")
                admitted = list(candidates)
                safe_payload = {"question_id": question.id, "claims": selected_claims,
                                "evidence": [row for row in answer["evidence"]
                                             if isinstance(row, dict) and row.get("id") in evidence_ids]}
            pending = {"schema_version": "demo-l5-pending-v1", "question_id": question.id,
                       "attempt_number": attempt_number, "source_receipt_sha256": "sha256:" + raw_sha,
                "candidate_claim_ids": candidates, "accepted_ids": [], "status": "pending_l5"}
            manifest_path = artifact_dir / "l5" / f"{request.attempt_key}.json"
            manifest_bytes = _canonical_bytes(pending)
            try:
                _write_exclusive(manifest_path, manifest_bytes)
            except FileExistsError:
                if manifest_path.read_bytes() != manifest_bytes:
                    raise OperatorRefused("l5_manifest_changed") from None
            guard = Decimal(result.get("guarded_charge_micros", 0)) / 1_000_000
            saved = demo_saved_work.persist_attempt_view(
                selected_receipt=raw, app_record=raw.get("app_record"), safe_payload=safe_payload,
                admitted_claim_ids=admitted, raw_receipt_bytes=raw_bytes,
                raw_receipt_sha256="sha256:" + raw_sha,
                funding=funding, guarded_charge_usd=format(guard, "f"), native_adapter=self.native,
                frozen_ask_ids=frozenset(self.frozen_ask_ids))
            exact = saved.get("recorded_model_usd")
            release = saved.get("reservation_release_usd")
            readback = saved.get("readback")
            prefixed_hash = saved.get("record_sha256")
            if (not isinstance(prefixed_hash, str) or not prefixed_hash.startswith("sha256:")
                    or not _HEX64.fullmatch(prefixed_hash.removeprefix("sha256:"))):
                raise OperatorRefused("w6_record_hash_invalid")
            record_hash = prefixed_hash.removeprefix("sha256:")
            if (saved.get("status") not in ("stored", "resumed")
                    or saved.get("ask_row_sha256") != saved.get("stored_ask_row_sha256")
                    or saved.get("native_net_micros") != 25_000_000
                    or not isinstance(readback, dict)
                    or readback.get("run_id") != saved.get("run_id")
                    or readback.get("ask_id") != saved.get("ask_id")
                    or readback.get("recorded_model_usd") != exact
                    or readback.get("reservation_release_usd") != release
                    or readback.get("recorded_model_usd_ceiling_micros") != saved.get("recorded_model_usd_ceiling_micros")
                    or readback.get("reservation_release_ceiling_micros") != saved.get("reservation_release_ceiling_micros")):
                raise OperatorRefused("ask_readback_unverified")
            w6_path = artifact_dir / "w6" / f"{request.attempt_key}.json"
            _write_exclusive(w6_path, _canonical_bytes(saved))
            w5_readback = {
                "match": True,
                "run_id": saved["run_id"],
                "record_sha256": record_hash,
                "recorded_model_usd": exact,
                "reservation_release_usd": release,
                "recorded_model_usd_ceiling_micros": saved["recorded_model_usd_ceiling_micros"],
                "reservation_release_ceiling_micros": saved["reservation_release_ceiling_micros"],
                "native_net_micros": saved["native_net_micros"],
            }
            self.frozen_ask_ids.add(saved["ask_id"])
            return {"verified": True, "ask_id": saved["ask_id"], "run_id": saved["run_id"],
                    "record_sha256": record_hash, "guarded_charge_micros": result["guarded_charge_micros"],
                    "recorded_model_usd": exact, "recorded_model_usd_ceiling_micros": saved["recorded_model_usd_ceiling_micros"],
                    "reservation_release_usd": release,
                    "reservation_release_ceiling_micros": saved["reservation_release_ceiling_micros"],
                    "native_net_micros": saved["native_net_micros"], "readback": w5_readback}

        pair_args = {"question_id": question.id, "attempt_number": attempt_number,
                     "data_dir": artifact_dir, "funding_proof": funding,
                     "provided_context": context, "dispatch": dispatch,
                     "persist_attempt": persist}
        if profile is None:
            return demo_pairs.run_attempt(**pair_args)
        return demo_pairs.run_attempt(**pair_args, profile=profile, prior_data_dir=prior_dir,
                                      ranking_proof_bytes=ranking_proof_bytes,
                                      source_commit=source_commit)

    def read_raw_attempt(self, receipt):
        dispatch = receipt.get("dispatch_result") or {}
        path = Path(dispatch.get("raw_receipt_path", ""))
        if not path.is_file() or self.data_dir.resolve() not in path.resolve().parents:
            raise OperatorRefused("raw_receipt_path_missing")
        data = path.read_bytes()
        if _sha(data) != dispatch.get("raw_receipt_sha256"):
            raise OperatorRefused("raw_receipt_hash_mismatch")
        return _raw_digest(json.loads(data.decode("utf-8")))

    def select_pair(self, question_id, first, second):
        from core.eval.demo_selection import select_pair

        return select_pair(question_id, first, second)

    def l5_manifest(self, question_id, selection, saved_work):
        body = {"schema_version": "demo-l5-handoff-v1", "question_id": question_id,
                "accepted_ids": [], "candidate_claim_ids": selection.get("admitted_claim_ids"),
                "raw_hashes": selection.get("raw_hashes"), "status": "pending_l5",
                "frozen_ask_ids": [saved_work["ask_id"]] if saved_work else [],
                "frozen_dossier_ids": [saved_work["dossier_id"]] if saved_work else []}
        data = _canonical_bytes(body)
        path = self.data_dir / "l5" / f"{question_id}.handoff.json"
        try:
            _write_exclusive(path, data)
        except FileExistsError:
            if path.read_bytes() != data:
                raise OperatorRefused("l5_handoff_already_frozen") from None
        return {"accepted_ids": [], "status": "pending_l5", "sha256": _sha(data), "path": str(path)}

    def read_l5_acceptance(self, question_id, selection, saved_work):
        if not isinstance(saved_work, dict):
            return None
        path = self.data_dir / "l5" / f"{question_id}.accepted.json"
        if not path.exists():
            return None
        value = _read_object(path, "l5_acceptance_invalid")
        if (value.get("schema_version") != "demo-l5-acceptance-v1"
                or value.get("status") != "accepted" or value.get("question_id") != question_id
                or value.get("dossier_id") != saved_work.get("dossier_id")
                or value.get("source_ask_id") != saved_work.get("ask_id")
                or value.get("source_ask_id") != saved_work.get("dossier_source_ask_id")
                or value.get("dossier_content_hash") != saved_work.get("dossier_content_hash")):
            raise OperatorRefused("l5_acceptance_invalid")
        ids = value.get("accepted_object_ids")
        dossier_object_ids = saved_work.get("dossier_object_ids")
        handoff = saved_work.get("handoff_receipt") or {}
        if (not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids)
                or not ids or len(set(ids)) != len(ids) or not isinstance(dossier_object_ids, list)
                or not set(ids).issubset(set(dossier_object_ids))
                or value.get("source_run_id") != handoff.get("source_run_id")
                or value.get("source_answer_sha256") != handoff.get("source_answer_sha256")
                or value.get("source_receipt_sha256") != handoff.get("source_receipt_sha256")
                or value.get("scope_admission_manifest_sha256") != saved_work.get("scope_admission_manifest_sha256")):
            raise OperatorRefused("l5_acceptance_scope_mismatch")
        frozen = {"schema_version": "demo-l5-accepted-objects-v1", "question_id": question_id,
                  "dossier_id": saved_work["dossier_id"], "source_ask_id": saved_work["ask_id"],
                  "dossier_content_hash": saved_work["dossier_content_hash"],
                  "source_run_id": handoff["source_run_id"],
                  "source_answer_sha256": handoff["source_answer_sha256"],
                  "source_receipt_sha256": handoff["source_receipt_sha256"],
                  "scope_admission_manifest_sha256": saved_work["scope_admission_manifest_sha256"],
                  "accepted_object_ids": sorted(ids),
                  "source_sha256": _sha(path.read_bytes())}
        frozen_bytes = _canonical_bytes(frozen)
        frozen_path = self.data_dir / "l5" / f"{question_id}.accepted-frozen.json"
        try:
            _write_exclusive(frozen_path, frozen_bytes)
        except FileExistsError:
            if frozen_path.read_bytes() != frozen_bytes:
                raise OperatorRefused("l5_acceptance_already_frozen") from None
        return {"accepted_ids": sorted(ids), "sha256": _sha(frozen_bytes), "verified": True}

    def create_saved_work(self, question_id, selection, raw_pair, outer_pair):
        from core.eval import demo_saved_work

        attempt = selection.get("selected_attempt")
        selected_raw = raw_pair[attempt - 1]
        selected_outer = outer_pair[attempt - 1]
        raw_path = Path((selected_outer.get("dispatch_result") or {}).get("raw_receipt_path", ""))
        if not raw_path.is_file() or self.data_dir.resolve() not in raw_path.resolve().parents:
            raise OperatorRefused("raw_receipt_path_missing")
        raw_bytes = raw_path.read_bytes()
        if _sha(raw_bytes) != (selected_outer.get("dispatch_result") or {}).get("raw_receipt_sha256"):
            raise OperatorRefused("raw_receipt_hash_mismatch")
        attempt_key = selected_raw.get("attempt_key")
        if not isinstance(attempt_key, str) or not attempt_key:
            raise OperatorRefused("selected_attempt_key_missing")
        saved_ask = _read_object(self.data_dir / "w6" / f"{attempt_key}.json", "w6_receipt_missing")
        receipt_sha = "sha256:" + _sha(raw_bytes)
        result = demo_saved_work.create_saved_work(
            selection=selection, selected_receipt=selected_raw, app_record=selected_raw["app_record"],
            selected_existing_ask=saved_ask, raw_receipt_bytes=raw_bytes,
            raw_receipt_sha256=receipt_sha, app_client=self.app_client,
            frozen_ask_ids=frozenset(self.frozen_ask_ids),
            frozen_dossier_ids=frozenset(self.frozen_dossier_ids))
        result["app_relative_path"] = f"#/dossiers/{result['dossier_id']}"
        readback = self.app_client.last_dossier_readback
        if (not isinstance(readback, dict) or readback.get("dossier_id") != result["dossier_id"]
                or readback.get("source_ask_id") != result["ask_id"]
                or readback.get("content_hash") != result["dossier_content_hash"]):
            raise OperatorRefused("dossier_object_readback_missing")
        claims = readback.get("claims")
        object_ids = [claim.get("claim_id") for claim in claims or [] if isinstance(claim, dict)]
        if (not isinstance(claims, list) or len(object_ids) != len(claims)
                or not all(isinstance(item, str) and item for item in object_ids)
                or len(set(object_ids)) != len(object_ids)):
            raise OperatorRefused("dossier_object_ids_invalid")
        result["dossier_object_ids"] = object_ids
        result["dossier_source_ask_id"] = readback["source_ask_id"]
        self.frozen_dossier_ids.add(result["dossier_id"])
        return result


def production_runtime_factory(*, repo_root=ROOT, data_dir=DATA, inputs_dir=INPUTS):
    return _ProductionRuntime(repo_root, data_dir, inputs_dir)


def main(argv=None, *, runtime_factory=None):
    parser = argparse.ArgumentParser(prog="python -m core.eval.demo_operator")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true", help="run read-only preflight (default)")
    mode.add_argument("--execute", action="store_true", help="transfer the approved reserve and run bounded pairs")
    mode.add_argument("--recover-existing-only", action="store_true",
                      help="verify and append proof for DEMO-01 attempt 1 without dispatch")
    parser.add_argument("--data-dir", type=Path, default=DATA)
    parser.add_argument("--inputs-dir", type=Path, default=INPUTS)
    args = parser.parse_args(argv)
    try:
        result = run_operator(runtime_factory, execute=args.execute, recover_existing_only=args.recover_existing_only,
                              data_dir=args.data_dir, inputs_dir=args.inputs_dir)
    except OperatorRefused as exc:
        print(f"Refused: {exc.args[0] if exc.args else 'operator_preflight_failed'}")
        return 2
    except Exception as exc:
        print(f"Stopped: {type(exc).__name__}")
        return 3
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0 if result.get("status") in {"prepared", "complete", "recovered"} else 3


if __name__ == "__main__":
    raise SystemExit(main())
