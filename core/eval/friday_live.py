import copy
from contextlib import contextmanager
import json
import math
import os
import tempfile
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from core.config.caps import CAPS_FILE

QUESTIONS = (
    ("ZA", "what is trending in south africa"),
    ("NG", "what is trending in nigeria"),
    ("KE", "what is trending in kenya"),
)
TERMINAL_STATUSES = {"complete", "failed", "stopped"}
_NAMESPACE_LOCKS = {}
_NAMESPACE_LOCKS_GUARD = threading.Lock()


class FridayLiveRefused(RuntimeError):
    pass


def eval_live_cap(path=CAPS_FILE):
    """EVAL_DAILY live from the caps file: the SocialCrawl credits an evaluation may spend live in a day. A file
    without it reads as zero. RULES 14: evaluations run in replay mode and spend no live credits."""
    config = yaml.safe_load(Path(path).read_text(encoding="utf-8")).get("EVAL_DAILY") or {}
    return int(config.get("live") or 0)


def _money(value, name):
    if isinstance(value, bool) or value is None:
        raise FridayLiveRefused(f"{name}_unknown")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise FridayLiveRefused(f"{name}_invalid") from None
    if not amount.is_finite() or amount < 0:
        raise FridayLiveRefused(f"{name}_invalid")
    return amount


def _money_text(value):
    return format(value, "f")


def _write_json(path, value, *, exclusive=False):
    path = Path(path)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return
    handle, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def _body(response):
    try:
        return response.json()
    except Exception:
        return {"raw_text": getattr(response, "text", None)}


@contextmanager
def _exclusive_file_lock(path):
    key = os.path.normcase(str(Path(path).resolve()))
    with _NAMESPACE_LOCKS_GUARD:
        local_lock = _NAMESPACE_LOCKS.setdefault(key, threading.Lock())
    if not local_lock.acquire(blocking=False):
        raise FridayLiveRefused("namespace_budget_lock_busy")
    stream = None
    locked = False
    try:
        stream = Path(path).open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            raise FridayLiveRefused("namespace_budget_lock_busy") from None
        locked = True
        yield
    finally:
        try:
            if stream is not None and locked:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            if stream is not None:
                stream.close()
            local_lock.release()


class FridayLiveRunner:
    def __init__(self, *, namespace_dir, namespace_id, transport, budget_admission, claim_tally,
                 total_budget_usd, request_timeout_seconds=20, terminal_deadline_seconds=600,
                 poll_interval_seconds=2, monotonic=None, sleep=None, resume=False, live_credits_allowed=0):
        if isinstance(live_credits_allowed, bool) or not isinstance(live_credits_allowed, int) or live_credits_allowed < 0:
            raise FridayLiveRefused("live_credits_allowed_invalid")
        # Replay unless the caller asks for live credits and the caps file allows at least that many a day.
        if live_credits_allowed > eval_live_cap():
            raise FridayLiveRefused("live_credits_refused_by_caps")
        self.mode = "live" if live_credits_allowed > 0 else "replay"
        if not isinstance(namespace_id, str) or not namespace_id.strip():
            raise FridayLiveRefused("namespace_id_required")
        if not callable(getattr(transport, "post", None)) or not callable(getattr(transport, "get", None)):
            raise FridayLiveRefused("transport_invalid")
        if not callable(budget_admission):
            raise FridayLiveRefused("budget_admission_missing")
        if claim_tally is not None and not callable(claim_tally):
            raise FridayLiveRefused("claim_tally_invalid")
        self.total_budget = _money(total_budget_usd, "total_budget")
        if self.total_budget <= 0:
            raise FridayLiveRefused("total_budget_invalid")
        self.request_timeout = float(request_timeout_seconds)
        self.deadline_seconds = float(terminal_deadline_seconds)
        self.poll_interval = float(poll_interval_seconds)
        if (not math.isfinite(self.request_timeout) or self.request_timeout <= 0
                or not math.isfinite(self.deadline_seconds) or self.deadline_seconds < 0
                or not math.isfinite(self.poll_interval) or self.poll_interval < 0):
            raise FridayLiveRefused("timeout_bounds_invalid")
        self.transport = transport
        self.budget_admission = budget_admission
        self.claim_tally = claim_tally
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.namespace_dir = Path(namespace_dir).resolve()
        self.namespace_dir.mkdir(parents=True, exist_ok=True)
        self.namespace_path = self.namespace_dir / "namespace.json"
        if self.namespace_path.exists():
            if not resume:
                raise FridayLiveRefused("namespace_not_fresh")
            manifest = _read_json(self.namespace_path)
            if manifest.get("namespace_id") != namespace_id:
                raise FridayLiveRefused("namespace_id_mismatch")
            original_cap = _money(manifest.get("original_fresh_cap_usd"), "namespace_cap")
            if original_cap != self.total_budget:
                raise FridayLiveRefused("namespace_budget_cap_mismatch")
        else:
            if resume or any(self.namespace_dir.iterdir()):
                raise FridayLiveRefused("namespace_not_fresh")
            _write_json(self.namespace_path, {
                "namespace_id": namespace_id,
                "original_fresh_cap_usd": _money_text(self.total_budget),
            }, exclusive=True)
        self.namespace_id = namespace_id

    def _marker_path(self, market):
        return self.namespace_dir / f"{market}.attempt.json"

    def _market(self, market):
        for known_market, question in QUESTIONS:
            if market == known_market:
                return question
        raise FridayLiveRefused("market_not_in_friday_plan")

    def _reserved_total(self):
        total = Decimal("0")
        for market, _ in QUESTIONS:
            path = self._marker_path(market)
            if path.exists():
                marker = _read_json(path)
                ceiling = marker.get("known_ceiling_usd")
                if ceiling is not None:
                    total += _money(ceiling, "stored_ceiling")
        return total

    def _save_marker(self, path, marker):
        _write_json(path, marker)

    def _save_report(self, outcomes=None, unattempted=None):
        outcomes = list(outcomes or [])
        if not outcomes:
            for market, _ in QUESTIONS:
                path = self._marker_path(market)
                if not path.exists():
                    continue
                marker = _read_json(path)
                if marker.get("result"):
                    outcomes.append(marker["result"])
                else:
                    outcomes.append({"market": market, "status": marker.get("state")})
        reserved = self._reserved_total()
        remaining = max(Decimal("0"), self.total_budget - reserved)
        report = {
            "namespace_id": self.namespace_id,
            "planned_markets": [market for market, _ in QUESTIONS],
            "markets": outcomes,
            "reserved_ceiling_usd": _money_text(reserved),
            "remaining_budget_usd": _money_text(remaining),
            "unattempted_markets": list(unattempted or []),
            "direct_native_writes": 0,
        }
        report["status"] = "all_markets_terminal" if len(outcomes) == len(QUESTIONS) and not unattempted else "held_or_partial"
        _write_json(self.namespace_dir / "report.json", report)
        return report

    def _result(self, market, marker, *, status, record=None, tally_result=None, tally_status=None,
                reason=None, fully_accounted=False, cost_accounted=True, native_runs_record_match=None):
        answer = record.get("answer") if isinstance(record, dict) else None
        run = record.get("run") if isinstance(record, dict) else None
        run = run if isinstance(run, dict) else {}
        claims = answer.get("claims") if isinstance(answer, dict) else None
        answer_status = answer.get("status") if isinstance(answer, dict) else None
        answer_candidate = (answer_status in {"complete", "partial"} and isinstance(claims, list)
                            and len(claims) >= 2)
        reported_model_usd = run.get("model_usd")
        cost_accounted = True
        if reported_model_usd is not None:
            try:
                reported_amount = _money(reported_model_usd, "application_reported_spend")
                ceiling = _money(marker.get("known_ceiling_usd"), "stored_ceiling")
                cost_accounted = reported_amount <= ceiling
            except FridayLiveRefused:
                cost_accounted = True
        result = {
            "market": market,
            "question": marker["request"]["question"],
            "ask_id": marker.get("ask_id"),
            "status": status,
            "reason": reason,
            "terminal_readback": record,
            "application_reported_model_spend": {
                "model_usd": reported_model_usd,
                "tokens": run.get("tokens"),
            },
            "provider_charges": {"status": "unproven", "measured_usd": None},
            "budget_admission": marker.get("budget_admission"),
            "reservation": {
                "kind": "local_pre_dispatch_ceiling",
                "known_ceiling_usd": marker.get("known_ceiling_usd"),
                "native_write": False,
            },
            "claim_tally": tally_result,
            "claim_tally_status": tally_status,
            "native_runs_record_match": native_runs_record_match,
            "fully_accounted": fully_accounted,
            "cost_accounted": cost_accounted,
            "answer_candidate": {
                "answer_status": answer_status,
                "answer_claim_count": len(claims) if isinstance(claims, list) else None,
                "meets_existing_candidate_shape": answer_candidate,
            },
            "candidate_to_l5": answer_candidate,
            "pass_to_l5": answer_candidate and fully_accounted and cost_accounted,
            "raw_receipts": {
                "post": str(self.namespace_dir / f"{market}.post.json"),
                "terminal_get": str(self.namespace_dir / f"{market}.readbacks.jsonl"),
            },
        }
        return result

    def _record_tally(self, record):
        if self.claim_tally is None:
            return None, "unaccounted", False, None
        answer = record.get("answer")
        claims = answer.get("claims") if isinstance(answer, dict) else []
        if not isinstance(claims, list):
            return None, "unaccounted", False, None
        ids = []
        for claim in claims:
            if not isinstance(claim, dict):
                return None, "unaccounted", False, None
            claim_id = claim.get("id", claim.get("claim_id"))
            if not isinstance(claim_id, str) or not claim_id:
                return None, "unaccounted", False, None
            ids.append(claim_id)
        if len(ids) != len(set(ids)):
            return None, "unaccounted", False, None
        try:
            result = self.claim_tally(copy.deepcopy(record))
        except Exception as exc:
            return {"error_type": type(exc).__name__}, "unavailable", False, None
        if not isinstance(result, dict):
            return None, "unaccounted", False, None
        native_row = result.get("native_runs_record")
        native_record = native_row.get("record") if isinstance(native_row, dict) else None
        try:
            if isinstance(native_record, str):
                native_record = json.loads(native_record)
        except (ValueError, TypeError):
            native_record = None
        run = record.get("run") if isinstance(record.get("run"), dict) else {}
        expected_run_id = run.get("run_id") or record.get("ask_id")
        native_match = (isinstance(native_row, dict) and native_row.get("stage") == "ask"
                        and native_row.get("run_id") == expected_run_id and native_record == record)
        final_ids = result.get("final_answer_claim_ids")
        final_ids_match = (isinstance(final_ids, list) and final_ids == ids)
        tally = result.get("claim_check_tally")
        final_claim_count = tally.get("final_answer_claim_count") if isinstance(tally, dict) else None
        complete = (result.get("accounted") is True and native_match
                    and isinstance(result.get("native_claim_rows"), list)
                    and isinstance(tally, dict) and type(final_claim_count) is int
                    and final_claim_count == len(ids) and final_ids_match)
        return result, ("accounted" if complete else "unaccounted"), complete, native_match

    def _read_terminal(self, market, marker, marker_path):
        ask_id = marker.get("ask_id")
        if not ask_id:
            return self._result(market, marker, status="uncertain", reason="ask_id_missing_after_dispatch")
        readbacks_path = self.namespace_dir / f"{market}.readbacks.jsonl"
        deadline = self.monotonic() + self.deadline_seconds
        first = True
        last_reason = None
        while first or self.monotonic() < deadline:
            first = False
            response = None
            try:
                response = self.transport.get(f"/api/ask/{ask_id}", timeout=self.request_timeout)
                body = _body(response)
                receipt = {"status_code": response.status_code, "body": body}
            except Exception as exc:
                body = None
                receipt = {"error_type": type(exc).__name__}
            with readbacks_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            if body is not None and getattr(response, "status_code", None) == 200 and isinstance(body, dict):
                if body.get("ask_id") != ask_id:
                    last_reason = "readback_ask_id_mismatch"
                    break
                elif (body.get("question") != marker["request"]["question"]
                      or body.get("market") != marker["request"]["market"]):
                    last_reason = "readback_request_mismatch"
                    break
                elif body.get("status") in TERMINAL_STATUSES:
                    tally_result, tally_status, fully_accounted, native_match = self._record_tally(body)
                    result = self._result(
                        market, marker, status=body["status"], record=body, tally_result=tally_result,
                        tally_status=tally_status, fully_accounted=fully_accounted,
                        native_runs_record_match=native_match,
                        reason=None if fully_accounted else "claim_tally_incomplete",
                    )
                    if not result["cost_accounted"]:
                        result["reason"] = "application_reported_spend_exceeds_reserved_ceiling"
                    marker.update(state="terminal", result=result)
                    self._save_marker(marker_path, marker)
                    return result
                elif body.get("status") != "running":
                    last_reason = "terminal_status_unrecognized"
            elif getattr(response, "status_code", None) in (401, 403):
                last_reason = "readback_auth_rejected"
                break
            else:
                last_reason = "terminal_record_not_available"
            now = self.monotonic()
            if now >= deadline:
                break
            self.sleep(min(self.poll_interval, max(0, deadline - now)))
        result = self._result(market, marker, status="pending_readback", reason=last_reason or "readback_deadline")
        marker.update(state="awaiting_terminal", result=result)
        self._save_marker(marker_path, marker)
        return result

    def run_market(self, market):
        question = self._market(market)
        marker_path = self._marker_path(market)
        request = {"question": question, "market": market, "mode": self.mode, "wait": False}
        with _exclusive_file_lock(self.namespace_dir / "namespace-budget.lock"):
            if marker_path.exists():
                raise FridayLiveRefused("already_attempted")
            reserved = self._reserved_total()
            remaining = self.total_budget - reserved
            if remaining <= 0:
                raise FridayLiveRefused("remaining_budget_exhausted")
            try:
                budget = self.budget_admission(
                    market=market, question=question, remaining_budget_usd=_money_text(remaining))
            except Exception as exc:
                raise FridayLiveRefused(f"budget_admission_failed_{type(exc).__name__}") from None
            if not isinstance(budget, dict) or budget.get("admitted") is not True:
                raise FridayLiveRefused("budget_not_admitted")
            ceiling = _money(budget.get("known_ceiling_usd"), "known_ceiling")
            if ceiling <= 0:
                raise FridayLiveRefused("known_ceiling_invalid")
            if ceiling > remaining:
                raise FridayLiveRefused("known_ceiling_exceeds_remaining")
            marker = {
                "namespace_id": self.namespace_id,
                "market": market,
                "request": request,
                "known_ceiling_usd": _money_text(ceiling),
                "budget_admission": budget,
                "state": "dispatch_started",
            }
            _write_json(marker_path, marker, exclusive=True)
            self._save_report()
        try:
            response = self.transport.post("/api/ask", json=request, timeout=self.request_timeout)
            post_body = _body(response)
            post_receipt = {"status_code": response.status_code, "body": post_body}
        except Exception as exc:
            post_receipt = {"error_type": type(exc).__name__, "outcome": "uncertain"}
            _write_json(self.namespace_dir / f"{market}.post.json", post_receipt)
            marker.update(state="uncertain", result=self._result(
                market, marker, status="uncertain", reason="post_response_unknown"))
            self._save_marker(marker_path, marker)
            self._save_report()
            return marker["result"]
        _write_json(self.namespace_dir / f"{market}.post.json", post_receipt)
        ask_id = post_body.get("ask_id") if isinstance(post_body, dict) else None
        if not 200 <= response.status_code < 300:
            marker.update(state="post_rejected", result=self._result(
                market, marker, status="post_rejected", reason=f"http_{response.status_code}"))
            self._save_marker(marker_path, marker)
            self._save_report()
            return marker["result"]
        if not isinstance(ask_id, str) or not ask_id:
            marker.update(state="uncertain", result=self._result(
                market, marker, status="uncertain", reason="ask_id_missing_after_dispatch"))
            self._save_marker(marker_path, marker)
            self._save_report()
            return marker["result"]
        marker.update(state="awaiting_terminal", ask_id=ask_id)
        self._save_marker(marker_path, marker)
        result = self._read_terminal(market, marker, marker_path)
        self._save_report()
        return result

    def resume_market(self, market):
        self._market(market)
        marker_path = self._marker_path(market)
        if not marker_path.exists():
            raise FridayLiveRefused("attempt_marker_missing")
        marker = _read_json(marker_path)
        if marker.get("namespace_id") != self.namespace_id:
            raise FridayLiveRefused("attempt_namespace_mismatch")
        if marker.get("state") == "terminal":
            result = marker.get("result")
            if not isinstance(result, dict):
                raise FridayLiveRefused("terminal_result_missing")
            if result.get("fully_accounted") is True:
                return result
            record = result.get("terminal_readback")
            if (not isinstance(record, dict) or record.get("ask_id") != marker.get("ask_id")
                    or record.get("question") != marker.get("request", {}).get("question")
                    or record.get("market") != marker.get("request", {}).get("market")
                    or record.get("status") not in TERMINAL_STATUSES):
                raise FridayLiveRefused("captured_terminal_record_mismatch")
            tally_result, tally_status, fully_accounted, native_match = self._record_tally(record)
            refreshed = self._result(
                market, marker, status=record["status"], record=record, tally_result=tally_result,
                tally_status=tally_status, fully_accounted=fully_accounted,
                native_runs_record_match=native_match,
                reason=None if fully_accounted else "claim_tally_incomplete",
            )
            if not refreshed["cost_accounted"]:
                refreshed["reason"] = "application_reported_spend_exceeds_reserved_ceiling"
            if "next_action" in result:
                refreshed["next_action"] = result["next_action"]
            marker.update(result=refreshed)
            self._save_marker(marker_path, marker)
            self._save_report()
            return refreshed
        if not marker.get("ask_id"):
            return marker.get("result") or self._result(
                market, marker, status="uncertain", reason="ask_id_missing_after_dispatch")
        if marker.get("state") not in {"awaiting_terminal"}:
            raise FridayLiveRefused("attempt_not_resumable")
        result = self._read_terminal(market, marker, marker_path)
        self._save_report()
        return result

    def run_markets(self):
        outcomes = []
        stopped = False
        for index, (market, _) in enumerate(QUESTIONS):
            if stopped:
                break
            try:
                outcome = self.run_market(market)
            except FridayLiveRefused as exc:
                outcome = {"market": market, "status": "held", "reason": str(exc), "fully_accounted": False}
                outcomes.append(outcome)
                stopped = True
                break
            has_next = index < len(QUESTIONS) - 1
            can_continue = (outcome.get("status") in TERMINAL_STATUSES
                            and outcome.get("fully_accounted") is True
                            and outcome.get("cost_accounted") is True)
            outcome["next_action"] = "continue_to_next_market" if has_next and can_continue else (
                "complete" if not has_next and can_continue else "hold")
            outcomes.append(outcome)
            stopped = has_next and not can_continue
        unattempted = [market for market, _ in QUESTIONS[len(outcomes):]] if stopped else []
        return self._save_report(outcomes=outcomes, unattempted=unattempted)
