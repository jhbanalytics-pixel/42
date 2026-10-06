from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from urllib.parse import urlsplit

from core.eval.ask_r2 import BUILDER_EMAIL, PROJECT
from core.eval.demo_operator import AGENT_URI, REQUEST_TIMEOUT_SECONDS


MAX_BYTES_BILLED = 50_000_000
RUN_SQL = (
    "SELECT run_id, stage, record FROM `ogilvy-trends-v2.intelligence_42_agent.runs` "
    "WHERE run_id = @run_id AND stage = 'ask'"
)
CLAIM_CHECKS_SQL = (
    "SELECT answer_or_brief_id, claim_id, rule, verdict, checker, reason, run_id "
    "FROM `ogilvy-trends-v2.intelligence_42_agent.claim_checks` "
    "WHERE run_id = @run_id AND answer_or_brief_id = @ask_id"
)
_ASK_PATH = re.compile(r"^/api/ask/(a_[0-9]{8}_[0-9a-f]{8})$")
_REQUIRED_CLAIM_FIELDS = ("answer_or_brief_id", "claim_id", "rule", "verdict", "checker", "run_id")
_CLAIM_FIELDS = (*_REQUIRED_CLAIM_FIELDS, "reason")


class FridayLiveNativeRefused(RuntimeError):
    pass


def _trusted_origin(agent_uri):
    parsed = urlsplit(agent_uri) if isinstance(agent_uri, str) else None
    if (agent_uri != AGENT_URI or parsed is None or parsed.scheme != "https"
            or parsed.hostname != urlsplit(AGENT_URI).hostname or parsed.port is not None
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise FridayLiveNativeRefused("agent_origin_untrusted")
    return AGENT_URI


def _verify_builder_identity(credentials, project, identity_verifier):
    if project != PROJECT or getattr(credentials, "_target_principal", None) != BUILDER_EMAIL:
        raise FridayLiveNativeRefused("builder_identity_mismatch")
    if identity_verifier is None:
        from core.eval.ask_r2 import _verify_builder_adc

        identity_verifier = _verify_builder_adc
    try:
        verified = identity_verifier()
    except Exception:
        raise FridayLiveNativeRefused("builder_identity_unverified") from None
    if (not isinstance(verified, dict) or verified.get("project") != PROJECT
            or verified.get("principal") != BUILDER_EMAIL):
        raise FridayLiveNativeRefused("builder_identity_mismatch")


def _default_refresh_request():
    from google.auth.transport.requests import Request

    class BoundedRequest(Request):
        def __call__(self, *args, **kwargs):
            kwargs["timeout"] = REQUEST_TIMEOUT_SECONDS
            return super().__call__(*args, **kwargs)

    return BoundedRequest()


def build_builder_agent_transport(*, agent_uri=AGENT_URI, credentials=None, project=None,
                                  auth_loader=None, identity_verifier=None, identity_factory=None,
                                  refresh_request_factory=None, session=None):
    origin = _trusted_origin(agent_uri)
    if credentials is None or project is None:
        if auth_loader is None:
            import google.auth

            auth_loader = google.auth.default
        try:
            credentials, project = auth_loader(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        except Exception:
            raise FridayLiveNativeRefused("builder_auth_unavailable") from None
    _verify_builder_identity(credentials, project, identity_verifier)
    if identity_factory is None:
        from google.auth.impersonated_credentials import IDTokenCredentials

        identity_factory = IDTokenCredentials
    try:
        identity = identity_factory(credentials, target_audience=origin, include_email=True)
        refresh_request = (refresh_request_factory or _default_refresh_request)()
        if session is None:
            import requests

            session = requests.Session()
    except Exception:
        raise FridayLiveNativeRefused("builder_transport_unavailable") from None
    return BuilderAgentTransport(origin, identity, refresh_request, session)


class BuilderAgentTransport:
    def __init__(self, origin, identity, refresh_request, session):
        self.origin = _trusted_origin(origin)
        if not callable(getattr(identity, "refresh", None)) or not callable(getattr(session, "request", None)):
            raise FridayLiveNativeRefused("builder_transport_invalid")
        self.identity = identity
        self.refresh_request = refresh_request
        self.session = session

    @staticmethod
    def _timeout(value):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0 or value > REQUEST_TIMEOUT_SECONDS):
            raise FridayLiveNativeRefused("request_timeout_invalid")
        return value

    def _request(self, method, path, *, timeout, body=None):
        if method == "POST":
            allowed = path == "/api/ask"
        else:
            allowed = isinstance(path, str) and _ASK_PATH.fullmatch(path) is not None
        if not allowed:
            raise FridayLiveNativeRefused("agent_path_untrusted")
        try:
            self.identity.refresh(self.refresh_request)
            token = self.identity.token
        except Exception:
            raise FridayLiveNativeRefused("identity_refresh_failed") from None
        if not isinstance(token, str) or not token:
            raise FridayLiveNativeRefused("identity_token_unavailable")
        kwargs = {
            "headers": {"Authorization": f"Bearer {token}", "Accept": "application/json"},
            "timeout": self._timeout(timeout),
            "allow_redirects": False,
        }
        if method == "POST":
            kwargs["json"] = body
        try:
            return self.session.request(method, self.origin + path, **kwargs)
        except Exception:
            raise FridayLiveNativeRefused("agent_request_failed") from None

    def post(self, path, *, json, timeout):
        return self._request("POST", path, timeout=timeout, body=json)

    def get(self, path, *, timeout):
        return self._request("GET", path, timeout=timeout)


def _final_claim_ids(record):
    answer = record.get("answer")
    claims = answer.get("claims") if isinstance(answer, dict) else None
    if not isinstance(claims, list):
        return None
    ids = []
    for claim in claims:
        if not isinstance(claim, dict):
            return None
        claim_id = claim.get("id", claim.get("claim_id"))
        if not isinstance(claim_id, str) or not claim_id:
            return None
        ids.append(claim_id)
    return ids if len(ids) == len(set(ids)) else None


class NativeAskClaimTally:
    def __init__(self, client, bigquery_module, *, query_timeout_seconds=REQUEST_TIMEOUT_SECONDS):
        if not callable(getattr(client, "query", None)):
            raise FridayLiveNativeRefused("native_client_invalid")
        self.client = client
        self.bigquery = bigquery_module
        self.query_timeout = query_timeout_seconds

    def _query(self, sql, params):
        config = self.bigquery.QueryJobConfig()
        config.maximum_bytes_billed = MAX_BYTES_BILLED
        config.query_parameters = [
            self.bigquery.ScalarQueryParameter(name, "STRING", value) for name, value in params
        ]
        job = self.client.query(sql, job_config=config, retry=None, job_retry=None)
        result = job.result(timeout=self.query_timeout, retry=None, job_retry=None)
        rows = [dict(row) for row in result]
        total_rows = getattr(result, "total_rows", None)
        receipt = {
            "job_id": getattr(job, "job_id", None),
            "sql_sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            "maximum_bytes_billed": MAX_BYTES_BILLED,
            "bytes_processed": getattr(job, "total_bytes_processed", None),
            "total_rows": total_rows,
            "rows_read": len(rows),
        }
        return rows, receipt, type(total_rows) is int and total_rows == len(rows)

    @staticmethod
    def _same_run_rows(rows):
        return not rows or all(row == rows[0] for row in rows[1:])

    def __call__(self, record):
        receipts = []
        result = {"accounted": False, "native_runs_record": None, "native_claim_rows": [],
                  "claim_check_tally": None, "final_answer_claim_ids": [],
                  "native_readback_receipts": receipts}
        if not isinstance(record, dict) or not isinstance(record.get("ask_id"), str) or not record["ask_id"]:
            return result
        run = record.get("run") if isinstance(record.get("run"), dict) else {}
        run_id = run.get("run_id") or record["ask_id"]
        claim_ids = _final_claim_ids(record)
        if not isinstance(run_id, str) or not run_id or claim_ids is None:
            return result
        result["final_answer_claim_ids"] = claim_ids
        try:
            run_rows, receipt, complete = self._query(RUN_SQL, (("run_id", run_id),))
            receipts.append(receipt)
            if not complete:
                return result
            if len(run_rows) != 1 and (not run_rows or not self._same_run_rows(run_rows)):
                return result
            native = run_rows[0] if run_rows else None
            if (not isinstance(native, dict) or native.get("run_id") != run_id or native.get("stage") != "ask"
                    or not isinstance(native.get("record"), str)):
                return result
            try:
                native_record = json.loads(native["record"])
            except (json.JSONDecodeError, TypeError):
                return result
            if native_record != record:
                return result
            result["native_runs_record"] = {
                "run_id": native["run_id"], "stage": native["stage"], "record": native["record"],
                "duplicate_rows": len(run_rows),
            }
            claim_rows, claim_receipt, complete = self._query(CLAIM_CHECKS_SQL, (
                ("run_id", run_id), ("ask_id", record["ask_id"]),
            ))
            receipts.append(claim_receipt)
            if not complete:
                return result
            result["native_claim_rows"] = claim_rows
        except Exception as exc:
            result["readback_error_type"] = type(exc).__name__
            return result
        if any(not isinstance(row, dict) or any(key not in row for key in _CLAIM_FIELDS)
               or any(not isinstance(row.get(key), str) or not row[key] for key in _REQUIRED_CLAIM_FIELDS)
               or row.get("reason") is not None and not isinstance(row.get("reason"), str)
               or row.get("run_id") != run_id or row.get("answer_or_brief_id") != record["ask_id"]
               for row in claim_rows):
            return result
        if not set(claim_ids).issubset({row["claim_id"] for row in claim_rows}):
            if claim_ids:
                return result
        grouped = Counter((row["rule"], row["verdict"], row["checker"]) for row in claim_rows)
        groups = [
            {"rule": rule, "verdict": verdict, "checker": checker, "rows": count}
            for (rule, verdict, checker), count in sorted(grouped.items())
        ]
        result["claim_check_tally"] = {
            "rows": len(claim_rows), "final_answer_claim_count": len(claim_ids), "groups": groups,
        }
        result["accounted"] = True
        return result


def build_native_ask_claim_tally():
    try:
        import google.auth
        from google.cloud import bigquery as bigquery_module

        credentials, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except Exception:
        raise FridayLiveNativeRefused("builder_auth_unavailable") from None
    _verify_builder_identity(credentials, project, None)
    try:
        client = bigquery_module.Client(project=PROJECT, credentials=credentials)
    except Exception:
        raise FridayLiveNativeRefused("native_client_unavailable") from None
    return NativeAskClaimTally(client, bigquery_module)
