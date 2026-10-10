"""f42-agent: runs Ask, streams its steps and records the finished run (core/api/contract.md sections 6 and 7)."""

import copy
import importlib
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from core.api import dossiers, finding_save, fixture_states, investigations, privacy, skins, summary_state
from core.api.store import SUPPRESSIONS, viewer_record
# The watch list read and its table live in core/api/watchlist.py so the f42-digest job can read watches
# without fastapi; WATCHES and _lock are the same objects there and here.
from core.api.watchlist import WATCHES, _lock
from core.api.watchlist import current_watches as _current_watches

log = logging.getLogger("f42-agent")

KEEPALIVE_SECONDS = 15
FIXTURES = Path(__file__).resolve().parent / "fixtures"
OFFSETS = {"ZA": 2, "NG": 1, "KE": 3}
EVENT_TYPES = ("step", "evidence", "claim")
# The run object's keys (contract section 6); a failed ask keeps only these, and only as plain JSON.
RUN_KEYS = ("run_id", "tier", "mode", "credits", "tokens", "seconds", "model_usd", "window",
            "posts", "platforms", "source_status", "followups", "notices", "notice", "timings")
MAX_RUN_BYTES = 64_000


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _run_of(exc):
    try:
        return getattr(exc, "run", None)
    except Exception:
        return None


def failed_run(partial):
    """The spend a failed ask already made, safe to store: contract keys, plain JSON, bounded size.

    Falls back to model_usd, credits and tokens alone, so MODEL_DAILY_USD still counts it."""
    if not isinstance(partial, dict):
        return None
    kept = {key: partial[key] for key in RUN_KEYS if key in partial}
    for candidate in (kept, {key: value for key, value in kept.items() if key != "timings"}):  # timings go first
        try:
            text = json.dumps(candidate)
            if len(text) <= MAX_RUN_BYTES:
                return json.loads(text)
        except Exception:  # TypeError, ValueError, RecursionError: anything not plain JSON
            pass
    spend = {key: partial[key] for key in ("model_usd", "credits") if _number(partial.get(key))}
    tokens = partial.get("tokens")
    if isinstance(tokens, dict) and len(tokens) <= 10 and all(
            isinstance(k, str) and _number(v) for k, v in tokens.items()):
        spend["tokens"] = dict(tokens)
    return spend or None
# Query receipts (contract section 7) ride on the runs row only. BigQuery refuses a streamed row over 10 MB and loses the
# whole row, so receipts keep RECEIPT_ROWS rows a query and at most RECEIPT_BYTES, and the row stays under ROW_BYTES
# (measured as sent, the record text escaped inside the row). Past that, rows go first, then the receipts.
RECEIPT_ROWS = 50
RECEIPT_BYTES = 1_000_000
ROW_BYTES = 9_000_000


def _finite(value):
    """value with NaN and Infinity floats as None, since the runs record is a JSON column and rejects them."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    return value


def with_receipts(row, record, receipts, ask_id=None):
    """The runs row's record text with the ask's query receipts, or the plain record text when there are none."""
    if not receipts:
        return row["record"]
    try:
        receipts = {str(k): dict(v) for k, v in _finite(json.loads(json.dumps(receipts, default=str))).items()
                    if isinstance(v, dict)}
        json.dumps(receipts, allow_nan=False)
    except Exception:  # not a mapping of mappings, or not JSON at all: the record still goes in without them
        log.warning("ask %s: query receipts are not plain JSON; storing the run without them", ask_id)
        receipts = None
    def keep_rows(receipt, count):
        receipt["rows"] = receipt["rows"][:count]
        if isinstance(receipt.get("row_indexes"), list):
            receipt["row_indexes"] = receipt["row_indexes"][:count]
        if isinstance(receipt.get("cell_types"), list):
            retained = set(receipt.get("row_indexes", range(len(receipt["rows"]))))
            receipt["cell_types"] = [cell for cell in receipt["cell_types"] if cell["row_index"] in retained]

    for receipt in (receipts or {}).values():
        if isinstance(receipt.get("rows"), list):
            keep_rows(receipt, RECEIPT_ROWS)
        if "supporting_rows" in receipt:
            supporting = receipt["supporting_rows"]
            if type(supporting) is not int or not 0 <= supporting <= len(receipt.get("rows") or []):
                receipt.update(supporting_rows=0, support_unavailable="invalid_support")
    room = min(RECEIPT_BYTES, ROW_BYTES - len(json.dumps(row)))

    def size():
        return len(json.dumps(json.dumps(receipts)))

    for query_id in sorted(receipts or {}, key=lambda q: -len(json.dumps(receipts[q].get("rows")))):
        if size() <= room:
            break
        receipt = receipts[query_id]
        supporting = receipt.get("supporting_rows", 0)
        if receipt.get("rows") and len(receipt["rows"]) > supporting:
            keep_rows(receipt, supporting)
            receipt["rows_dropped"] = "size"
    for query_id in sorted(receipts or {}, key=lambda q: -len(json.dumps(receipts[q].get("rows")))):
        if size() <= room:
            break
        if receipts[query_id].get("rows"):
            keep_rows(receipts[query_id], 0)
            receipts[query_id]["rows_dropped"] = "size"
            if receipts[query_id].get("supporting_rows"):
                receipts[query_id].update(supporting_rows=0, support_unavailable="size")
    if receipts is not None and size() > room:
        log.warning("ask %s: query receipts are too large for the runs row even without rows; dropped", ask_id)
        receipts = None
    return json.dumps({**record, "query_receipts": receipts})


BODY_KEYS = ("question", "market", "parent_id", "tier", "mode", "wait", "from_card")
MAX_PARENT_QUERIES = 20


def parent_queries(answer, receipts) -> list:
    """The SQL behind the numbers a parent's claims still carry, for the follow-up to run again: per cited query id its
    purpose, sql and params from the receipts, never the rows. The answer is the one a reader would see now, so a claim
    the privacy projection dropped cites nothing. Anything unreadable gives none, and never fails the follow-up."""
    try:
        if not isinstance(answer, dict) or not isinstance(receipts, dict):
            return []
        cited = []
        for claim in answer.get("claims") or []:
            for number in claim.get("numbers") or []:
                qid = number.get("query_id") if isinstance(number, dict) else None
                if isinstance(qid, str) and qid not in cited:
                    cited.append(qid)
        out = []
        for qid in cited:
            receipt = receipts.get(qid)
            if isinstance(receipt, dict) and isinstance(receipt.get("sql"), str):
                params = receipt.get("params")
                out.append({"query_id": qid, "purpose": receipt.get("purpose"), "sql": receipt["sql"],
                            "params": params if isinstance(params, dict) else {}})
        return out[:MAX_PARENT_QUERIES]
    except Exception:
        return []
MAX_FINISHED = 200
FINISHED_TTL_S = 3600
MAX_RUNNING = 8
MAX_SINK = 500

ASKS = {}
SINK = []
_PLANNER_READBACK_HOLDS = {}

@asynccontextmanager
async def investigation_lifespan(app):
    await run_in_threadpool(recover_investigations)
    yield


app = FastAPI(title="f42-agent", lifespan=investigation_lifespan)


def now(market=None):
    tz = timezone(timedelta(hours=OFFSETS.get(market, 2)))
    return datetime.now(tz).replace(microsecond=0)


def error(status, code, message):
    return JSONResponse({"error": code, "message": message}, status_code=status)


class Ask:
    def __init__(self, request):
        self.request = request
        self.cond = threading.Condition()
        self.events = []
        self.stop = False
        self.done = None
        self.finished = threading.Event()
        self.started = time.monotonic()
        self.ended = None
        self.receipts = None  # the run's query receipts, for the runs row only (sink), never in the record
        self.record = {
            "ask_id": request["ask_id"], "question": request["question"],
            "parent_id": request.get("parent_id"), "market": request.get("market"),
            "status": "running", "created_at": now(request.get("market")).isoformat(),
            "finished_at": None, "answer": None, "run": None, "steps": [], "error": None,
        }

    def emit(self, event):
        event = dict(event)
        named = event.pop("event", None)
        typed = event.pop("type", None)
        # The wrapper alone numbers and stamps events.
        event.pop("seq", None)
        event.pop("at", None)
        kind = named if named in EVENT_TYPES else typed or (
            "evidence" if "evidence" in event else "claim" if "claim" in event else "step")
        with self.cond:
            if self.done:
                return
            data = {"seq": len(self.events) + 1, "at": now(self.request.get("market")).isoformat(), **event}
            self.events.append((kind, data))
            if kind == "step":
                self.record["steps"].append(data)
            self.cond.notify_all()

    def should_stop(self):
        return self.stop

    def snapshot(self):
        with self.cond:
            return copy.deepcopy(self.record)


def resolve_run_ask():
    if os.environ.get("F42_AGENT") == "fixture":
        return fixture_agent
    try:
        from core.agent.ask import run_ask
        return run_ask
    except ImportError as exc:
        log.warning("core.agent.ask not importable: %s", exc)
    try:
        from core.agent import run_ask
        return run_ask
    except ImportError as exc:
        log.warning("core.agent.run_ask not importable: %s", exc)
        return None


def fixture_state(request, answer, run):
    """The state the real producer would build for the fixture's complete and partial answers, so the pages and the
    journeys over the fixture agent see a verified state (C1 P-19). None if answer_state is not there."""
    try:
        blank = not str(answer.get("short_answer") or "").strip()
        return summary_state.module().build(
            ask_id=request["ask_id"], check_run_id=run.get("run_id"), execution_state="completed", stop_reason=None,
            summary_state="blank_unexplained" if blank else "shown", removals=[], rewrite="not_attempted", answer=answer)
    except Exception as exc:
        log.error("ask %s: answer_meta not built: %s", request["ask_id"], type(exc).__name__)
        return None


def stopped_state(request, answer, run):
    """The state the real producer builds for a run stopped on request: its fixed stop text is a fixed_text summary and
    nothing was removed or rewritten. None if answer_state is not there."""
    try:
        return summary_state.module().build(
            ask_id=request["ask_id"], check_run_id=run.get("run_id"), execution_state="stopped_on_request",
            stop_reason=None, summary_state="fixed_text", removals=[], rewrite="not_attempted", answer=answer)
    except Exception as exc:
        log.error("ask %s: answer_meta not built: %s", request["ask_id"], type(exc).__name__)
        return None


def fixture_agent(request, emit, should_stop):
    question = request["question"].lower()
    fixture = os.environ.get("F42_FIXTURE_STATE")  # one fixture of C1 v2 section 7 (P-19), by its id
    failing = re.search(r"\bfail", question) is not None or fixture == fixture_states.FAILS
    name = "ask_partial.json" if re.search(r"\bthin\b", question) else "ask_complete.json"
    record = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    answer, run = record["answer"], record["run"]
    run["run_id"] = "r_" + request["ask_id"][2:]
    steps = [{k: v for k, v in s.items() if k not in ("seq", "at")} for s in record["steps"]]
    claim = answer["claims"][0]
    script = []
    for i, step in enumerate(steps):
        script.append(step)
        if i == 1:
            script.append({"evidence": answer["evidence"][0]})
        if i == 2:
            script.append({"evidence": answer["evidence"][1]})
        if i == 4:
            script.append({"claim": claim, "check": "checking", "reason": None})
            script.append({"claim": claim, "check": "verified", "reason": None})
    if failing:
        script = steps[:2]
    delay = float(os.environ.get("F42_FIXTURE_DELAY", "0.05"))
    for done, event in enumerate(script):
        if should_stop():
            answer.update(status="insufficient_evidence", claims=[], so_what=[], watch_next=[],
                          short_answer="Stopped on request before any claim passed the checks.")
            answer["gaps"].append({"what": "Research stopped on request",
                                   "searched": f"{done} of {len(script)} research steps", "why": "stopped"})
            run["notices"].append("Stopped on request before the research finished")
            return {"answer": answer, "run": run, "answer_meta": stopped_state(request, answer, run)}
        emit(event)
        time.sleep(delay)
    if failing:
        raise RuntimeError("fixture agent failed on purpose")
    if fixture:
        answer, meta = fixture_states.build(fixture, request, answer, run)
        return {"answer": answer, "run": run, **({"answer_meta": meta} if meta else {})}
    return {"answer": answer, "run": run, "answer_meta": fixture_state(request, answer, run)}


MODEL_UNAVAILABLE = "The model is not available right now (quota or access). Try again later."
# HTTP statuses a model call raises when it is out of quota (429), not enabled for the project (403) or unknown
# to the region (404). Only errors from the model SDKs count: a BigQuery 404 or 403 is a data or grant fault, and
# saying "the model is not available" about it would send the diagnosis to the wrong place.
MODEL_STATUSES = {429, 403, 404}
MODEL_SDKS = ("google.genai",)


def _from_model_sdk(exc):
    module = type(exc).__module__ or ""
    return any(module == m or module.startswith(m + ".") for m in MODEL_SDKS)


def model_failure(exc):
    """{"status", "sdk_module"} of the model SDK error, in exc or anything it was raised from, that carries a quota
    or access status; None when there is none. Only the status and the error's module, never the SDK's message."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if not _from_model_sdk(exc):
            exc = exc.__cause__ or exc.__context__
            continue
        for name in ("status_code", "code", "api_error_status"):
            value = getattr(exc, name, None)
            value = value() if callable(value) else value
            if isinstance(value, int) and value in MODEL_STATUSES:
                return {"status": value, "sdk_module": type(exc).__module__}
        exc = exc.__cause__ or exc.__context__
    return None


def model_unavailable(exc):
    """True when exc, or anything it was raised from, is a model SDK error carrying a quota or access status."""
    return model_failure(exc) is not None


# Shown on an ask whose runs row could not be written: its answer stands, but no stored run backs it.
NOT_RECORDED = ("This run could not be recorded, so its model spend stays counted against today's cap and the answer "
                "will not appear in history")


def recorded(ask_id):
    """Tell core.agent.ask the ask's runs row is written, so its finished spend no longer needs holding."""
    try:
        from core.agent.ask import recorded as release
    except ImportError:
        return
    release(ask_id)


def failed_state(ask, update):
    """The state of a run that raised: built here, from the ask id and the run id alone. None when it cannot be
    built; the Ask is never failed for a metadata fault."""
    try:
        run = update.get("run") if isinstance(update.get("run"), dict) else {}
        return summary_state.module().build_failed(ask_id=ask.request["ask_id"], check_run_id=run.get("run_id"))
    except Exception as exc:
        log.error("ask %s: answer_meta not built: %s", ask.request["ask_id"], type(exc).__name__)
        return None


def keep_state(ask, update, meta):
    """Put the typed summary state into update, beside the answer, when it fits the record as it will be stored
    (C1 3): judged by answer_state.verify_stored against {**record, **update}. A value that does not fit is not stored,
    the record reads as legacy and the ask goes on: a paid answer is not failed for a metadata fault. Only the
    producer's value is accepted, never one the model or a client could have written."""
    if meta is None:
        return
    ask_id = ask.request["ask_id"]
    try:
        problem = summary_state.module().verify_stored({**ask.record, **update, "answer_meta": meta})
    except Exception as exc:
        log.error("ask %s: answer_meta not checked: %s", ask_id, type(exc).__name__)
        return
    if problem is not None:
        log.error("ask %s: answer_meta rejected: %s", ask_id, problem)
        return
    update["answer_meta"] = meta


def execute(ask, run_ask, after=None):
    try:
        result = run_ask(copy.deepcopy(ask.request), ask.emit, ask.should_stop)
        ask.receipts = result.get("query_receipts")
        update = {"status": "stopped" if ask.stop else "complete",
                  "answer": result.get("answer"), "run": result.get("run")}
        meta = result.get("answer_meta")
    except Exception as exc:
        log.exception("ask %s failed", ask.request["ask_id"])
        failure = model_failure(exc)
        error = ({"error": "model_unavailable", "message": MODEL_UNAVAILABLE, **failure} if failure else
                 {"error": "internal", "message": f"The agent hit an error and could not finish ({type(exc).__name__})."})
        update = {"status": "failed", "answer": None, "run": failed_run(_run_of(exc)), "error": error}
        meta = failed_state(ask, update)
    try:  # the metadata code has its own try block: a fault in it never fails a paid answer or a failed record
        keep_state(ask, update, meta)
    except Exception as exc:
        log.error("ask %s: answer_meta not stored: %s", ask.request["ask_id"], type(exc).__name__)
    with ask.cond:
        ask.record.update(update)
        ask.record["finished_at"] = now(ask.request.get("market")).isoformat()
    try:
        stored = sink(ask)
    except Exception:
        log.exception("ask %s: writing the run row failed", ask.request["ask_id"])
        stored = False
    if stored:
        recorded(ask.request["ask_id"])
    else:
        # Its spend stays held on today's cap in this process (core.agent.ask._UNRECORDED), and the run says so.
        with ask.cond:
            run = ask.record.get("run")
            if isinstance(run, dict):
                run["notices"] = [*(run.get("notices") or []), NOT_RECORDED]
    if after is not None:
        try:
            after(ask)
        except Exception:
            log.exception("ask %s: finishing its investigation failed", ask.request["ask_id"])
    with ask.cond:
        ask.done = {"seq": len(ask.events) + 1, "status": ask.record["status"], "url": f"/api/ask/{ask.request['ask_id']}"}
        ask.cond.notify_all()
    with _lock:
        ask.ended = time.monotonic()
        prune()
    ask.finished.set()


def prune():
    """Drop finished asks past FINISHED_TTL_S, then the oldest past MAX_FINISHED. Call with _lock held."""
    cutoff = time.monotonic() - FINISHED_TTL_S
    finished = sorted((a for a in ASKS.values() if a.ended is not None), key=lambda a: a.ended)
    fresh = [a for a in finished if a.ended >= cutoff]
    drop = [a for a in finished if a.ended < cutoff] + fresh[:max(0, len(fresh) - MAX_FINISHED)]
    for a in drop:
        ASKS.pop(a.request["ask_id"], None)


def project():
    return os.environ.get("F42_PROJECT", "ogilvy-trends-v2")


def bigquery_client():
    from google.cloud import bigquery
    return bigquery.Client(project=project())


SAST = timezone(timedelta(hours=2), "SAST")  # Africa/Johannesburg keeps no daylight saving


def cap_day(created_at):
    """The day a run's spend counts against: the SAST date of its start. Every daily cap reads runs by this day
    (spent_today here, model_spend_today in core.agent.ask), while created_at stays on the market's clock."""
    return datetime.fromisoformat(created_at).astimezone(SAST).date().isoformat()


def sink(ask):
    """Write the ask's runs row. True when it is stored; False when BigQuery returns row errors."""
    record = ask.snapshot()
    run = record["run"] or {}
    answer = record["answer"]
    row = {
        "run_id": run.get("run_id") or record["ask_id"],
        "stage": "ask",
        "run_date": cap_day(record["created_at"]),
        "status": record["status"],
        "started_at": record["created_at"],
        "finished_at": record["finished_at"],
        "question": record["question"],
        "tier": run.get("tier") or ask.request.get("tier"),
        "credits": run.get("credits"),
        "seconds": run.get("seconds", round(time.monotonic() - ask.started, 1)),
        "outcome": answer.get("status") if answer else (record["error"] or {}).get("error", record["status"]),
        "answer": json.dumps(answer) if answer is not None else None,
        "record": json.dumps(record),
    }
    row["record"] = with_receipts(row, record, ask.receipts, record["ask_id"])
    if os.environ.get("F42_DATA") == "bigquery":
        errors = bigquery_client().insert_rows_json(f"{project()}.intelligence_42_agent.runs", [row])
        if errors:
            log.error("ask %s: runs insert returned errors: %s", record["ask_id"], errors)
            return False
        return True
    with _lock:
        SINK.append(row)
        del SINK[:-MAX_SINK]
    return True


# T2 on /api/ask (contract sections 6 and 15.2): only the brand lens and the context pack, and only while
# F42_T2_READY is exactly "1" on this service. Off by default; T3 stays on investigations only.
T2_SKILLS = ("brand-implication", "context-pack")


def t2_ready() -> bool:
    return os.environ.get("F42_T2_READY") == "1"


def validate(body):
    if not isinstance(body, dict):
        return "The body must be a JSON object."
    question = body.get("question")
    if not isinstance(question, str) or not 3 <= len(question.strip()) <= 2000:
        return "question must be 3 to 2,000 characters."
    if body.get("market") not in ("ZA", "NG", "KE", None):
        return "market must be ZA, NG, KE or null."
    if body.get("tier") == "T3" or (body.get("tier") == "T2" and not t2_ready()):
        return f"Tier {body['tier']} is not available until Stage 1B; use T0, T1 or leave it out."
    if body.get("tier") == "T2" and body.get("skill") not in T2_SKILLS:
        return ("Tier T2 is open only for the brand lens and the context pack (skill brand-implication or "
                "context-pack); use T0, T1 or leave it out.")
    if body.get("tier") not in ("T0", "T1", "T2", None):
        return "tier must be T0, T1 or null."
    if body.get("mode", "live") not in ("live", "replay"):
        return "mode must be live or replay."
    if not isinstance(body.get("wait", False), bool):
        return "wait must be true or false."
    if not isinstance(body.get("parent_id"), (str, type(None))):
        return "parent_id must be an ask id or null."
    if not isinstance(body.get("from_card"), (dict, type(None))):
        return "from_card must be an object or null."
    if body.get("spike") is not None:
        from core.api.spike import check_spike
        try:
            body["spike"] = check_spike(body["spike"])
        except ValueError as exc:
            return str(exc)
    if body.get("skill") is not None and body["skill"] not in skins.SKILLS:
        return "skill must be one of " + ", ".join(skins.SKILLS) + ", or left out."
    return None


def get_ask(ask_id):
    with _lock:
        return ASKS.get(ask_id)


def not_found(ask_id):
    return error(404, "not_found", f"No ask {ask_id} is held here.")


@app.post("/api/ask")
async def start(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return error(400, "bad_request", "The body must be JSON.")
    problem = validate(body)
    if problem:
        return error(400, "bad_request", problem)
    run_ask = resolve_run_ask()
    if run_ask is None:
        log.error("core.agent.run_ask is not importable; refusing the ask")
        return error(503, "agent_unavailable", "The Ask agent is not installed on this service.")
    req = {key: body.get(key) for key in BODY_KEYS}
    req.update(question=body["question"].strip(), mode=body.get("mode", "live"), wait=body.get("wait", False))
    if body.get("spike") is not None:
        req["spike"] = body["spike"]  # a spike ask from f42-api (contract 14.1), for L3's spike-explain skill
    if body.get("skill") is not None:
        req["skill"] = body["skill"]  # one of L3's Ask skills (contract 15.2), passed to run_ask as sent
    if req["parent_id"]:
        parent = get_ask(req["parent_id"])
        receipts = None
        if parent is not None:
            held, receipts = parent.snapshot(), parent.receipts
        else:
            from core.api.store import get_store
            store = get_store()
            if hasattr(store, "ask_record_raw"):
                raw = await run_in_threadpool(store.ask_record_raw, req["parent_id"])
                held = viewer_record(raw)
                receipts = raw.get("query_receipts") if isinstance(raw, dict) else None
            else:
                held = await run_in_threadpool(store.ask_record, req["parent_id"])
            if held is None:
                return not_found(req["parent_id"])
        # The earlier answer enters a new run only as a reader would see it now (C5 v2 section 7).
        held = await run_in_threadpool(readable, held)
        if (held.get("privacy") or {}).get("state") == "unavailable":
            return people_unavailable()
        req["parent"] = {"question": held["question"], "answer": held["answer"],
                         "window": (held.get("run") or {}).get("window"), "market": held.get("market"),
                         "queries": parent_queries(held["answer"], receipts)}
    req["ask_id"] = f"a_{now(req['market']).strftime('%Y%m%d')}_{secrets.token_hex(4)}"
    ask = Ask(req)
    if "spike" in req:
        ask.record["spike"] = req["spike"]  # so Try again on a followed spike goes back to its topic page
    with _lock:
        prune()
        busy = sum(1 for a in ASKS.values() if a.ended is None) >= MAX_RUNNING
        if not busy:
            ASKS[req["ask_id"]] = ask
    if busy:
        return error(429, "rate_limited",
                     f"{MAX_RUNNING} asks are already running; try again when one finishes.")
    threading.Thread(target=execute, args=(ask, run_ask), daemon=True, name=req["ask_id"]).start()
    if req["wait"]:
        await run_in_threadpool(ask.finished.wait)
        return view_response(await run_in_threadpool(readable, ask.snapshot()))
    url = f"/api/ask/{req['ask_id']}"
    return JSONResponse({"ask_id": req["ask_id"], "status": "running", "events_url": url + "/events", "url": url},
                        status_code=202)


@app.get("/api/ask/{ask_id}")
def read(ask_id: str):
    ask = get_ask(ask_id)
    if not ask:
        return not_found(ask_id)
    record = ask.snapshot()
    if record.get("status") != "running":
        privacy.POLLS.done(("agent", ask_id))
        return view_response(readable(record))
    # A run in flight is polled every couple of seconds: its list is no older than 30 seconds (section 19).
    from core.api.store import get_store
    held = privacy.POLLS.of(("agent", ask_id), privacy.LazyStore(get_store))
    return view_response(readable(record, held.get(), held.creators))


def frame(kind, data):
    return f"id: {data['seq']}\nevent: {kind}\ndata: {json.dumps(data)}\n\n"


def follow(ask, after):
    """Every event after `after`, then live ones, then done. A skin run's events are masked before they leave
    (contract 15.1), naming approved accounts only, since the authors' page tiers are not read per event."""
    from core.api.store import get_store
    sent = after
    skin_id = ask.record.get("skin_id")
    people = skin_people(skin_id) if skin_id else None
    hidden = set()  # handles stripped from this stream's evidence, masked wherever they appear
    store, left_out, memo, said = privacy.LazyStore(get_store), set(), {}, False
    fresh = privacy.FreshList(store)  # a list no older than 30 seconds while the run is in flight (R2)
    if people:
        with ask.cond:
            held = [data for kind, data in ask.events if kind == "evidence"]
        for data in held:  # every author held so far, so a step sent before its evidence, or skipped, is masked too
            skins.mask_event(data, people["approved"], [], hidden)
    while True:
        with ask.cond:
            if len(ask.events) <= sent and ask.done is None:
                ask.cond.wait(KEEPALIVE_SECONDS)
            new = ask.events[sent:]
            done = ask.done
        for kind, data in new:
            shown = skins.mask_event(data, people["approved"], [], hidden) if people else data
            current = fresh.get()
            shown_event = privacy.project_event(kind, shown, current, store, left_out, memo)
            if shown_event is None and current is None and not said:
                said = True  # one step says why no evidence or claim follows (5.5)
                yield frame("step", {"seq": data["seq"], "text": privacy.UNAVAILABLE_NOTE})
            elif shown_event is not None:
                yield frame(kind, shown_event)
            sent = data["seq"]
        if done is not None:
            yield frame("done", done)
            return
        if not new:
            yield ": keepalive\n\n"


@app.get("/api/ask/{ask_id}/events")
def events(ask_id: str, request: Request):
    ask = get_ask(ask_id)
    if ask is None:
        return not_found(ask_id)
    try:
        after = max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        after = 0
    return StreamingResponse(follow(ask, after), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/ask/{ask_id}/stop")
def stop(ask_id: str):
    ask = get_ask(ask_id)
    if ask is None:
        return not_found(ask_id)
    ask.stop = True
    return JSONResponse({"ask_id": ask_id, "status": "stopping"}, status_code=202)


WATCH_KINDS = ("item", "hashtag", "sound", "creator", "brand", "query")
WATCH_MARKETS = ("ZA", "NG", "KE", "all")
STATES = ("new_to_42", "spike", "on_the_boards", "emerging", "rising", "peaking", "mainstream", "fading",
          "recurring", "seasonal")
FLAG_RULES = ("breakout", "tone_flip", "creator_surge")
FEEDBACK_VALUES = ("real", "not_real", "useful", "wrong")
MAX_VALUE = 200
MAX_REASON = 1000
FEEDBACK = []


def _text(value, limit=MAX_VALUE):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit


def watch_target(target):
    if not isinstance(target, dict) or target.get("kind") not in WATCH_KINDS:
        return None, "target.kind must be one of " + ", ".join(WATCH_KINDS) + "."
    field = "item_id" if target["kind"] == "item" else "value"
    if not _text(target.get(field)):
        return None, f"A {target['kind']} target needs {field}, 1 to {MAX_VALUE} characters."
    return {"kind": target["kind"], field: target[field].strip()}, None


def watch_rule(rule):
    if not isinstance(rule, dict) or len(rule) != 1:
        return "rule must hold exactly one of state_in, ratio_over, reach_over, breakout, tone_flip, creator_surge."
    (key, value), = rule.items()
    if key == "state_in":
        if not isinstance(value, list) or not value or any(s not in STATES for s in value):
            return "state_in must be a list of state codes: " + ", ".join(STATES) + "."
    elif key == "ratio_over":
        if not _number(value) or value <= 0:
            return "ratio_over must be a number above 0."
    elif key == "reach_over":
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return "reach_over must be a whole number above 0."
    elif key in FLAG_RULES:
        if value is not True:
            return f"{key} must be true."
    else:
        return f"Unknown rule {key}."
    return None


def watch_row(watch):
    return {**watch, "target": json.dumps(watch["target"]), "rule": json.dumps(watch["rule"])}


def append(table, held, row):
    """Append one row to intelligence_42_agent.<table>, or to the in-memory list outside BigQuery. True when stored."""
    if os.environ.get("F42_DATA") == "bigquery":
        errors = bigquery_client().insert_rows_json(f"{project()}.intelligence_42_agent.{table}", [row])
        if errors:
            log.error("%s insert returned errors: %s", table, errors)
            return False
        return True
    with _lock:
        held.append(row)
        del held[:-MAX_SINK]
    return True


def current_watches(watch_id=None):
    """The latest row per watch_id by status time, newest first (core/api/watchlist.py), read with this module's
    bigquery_client and WATCHES."""
    return _current_watches(watch_id, client=bigquery_client, held=WATCHES)


def not_saved(what):
    return error(500, "internal", f"The {what} could not be saved; try again.")


async def body_of(request):
    try:
        body = await request.json()
    except ValueError:
        return None, error(400, "bad_request", "The body must be JSON.")
    if not isinstance(body, dict):
        return None, error(400, "bad_request", "The body must be a JSON object.")
    return body, None


@app.post("/api/watches")
async def create_watch(request: Request):
    body, problem = await body_of(request)
    if problem:
        return problem
    target, why = watch_target(body.get("target"))
    why = why or (None if body.get("market") in WATCH_MARKETS else "market must be ZA, NG, KE or all.")
    why = why or watch_rule(body.get("rule"))
    label = body.get("label")
    if not why and not (label is None or (isinstance(label, str) and len(label) <= 120)):
        why = "label must be up to 120 characters."
    if why:
        return error(400, "bad_request", why)
    market = body["market"]
    at = status_time(market)
    watch = {"watch_id": f"w_{secrets.token_hex(6)}", "created_at": at, "status_at": at,
             "who": "passcode", "target": target, "market": market, "rule": body["rule"],
             "label": ((label or "").strip() or target.get("value") or target["item_id"])[:120],
             "status": "active"}
    if not await run_in_threadpool(append, "watches", WATCHES, watch_row(watch)):
        return not_saved("watch")
    return JSONResponse(watch, status_code=201)


def status_time(market):
    return datetime.now(timezone(timedelta(hours=OFFSETS.get(market, 2)))).isoformat()


def set_status(watch_id, status):
    held = current_watches(watch_id)
    if not held:
        return error(404, "not_found", f"No watch {watch_id} is held here.")
    watch = held[0]
    if watch["status"] == status:
        return JSONResponse(watch)
    watch.update(status=status, status_at=status_time(watch["market"]))
    if not append("watches", WATCHES, watch_row(watch)):
        return not_saved("watch")
    return JSONResponse(watch)


@app.post("/api/watches/{watch_id}/pause")
def pause_watch(watch_id: str):
    return set_status(watch_id, "paused")


@app.post("/api/watches/{watch_id}/resume")
def resume_watch(watch_id: str):
    return set_status(watch_id, "active")


@app.get("/api/watches")
def list_watches():
    return {"watches": current_watches()}


def feedback_target(target):
    if not isinstance(target, dict):
        return None
    if target.get("kind") == "card":
        if (_text(target.get("item_id")) and target.get("market") in ("ZA", "NG", "KE")
                and isinstance(target.get("date"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", target["date"])):
            return {key: target[key] for key in ("kind", "item_id", "market", "date")}
        return None
    if target.get("kind") == "claim":
        ids = [key for key in ("answer_id", "brief_id") if key in target]
        if len(ids) == 1 and _text(target[ids[0]]) and _text(target.get("claim_id")):
            return {"kind": "claim", ids[0]: target[ids[0]], "claim_id": target["claim_id"]}
    return None


@app.post("/api/feedback")
async def feedback(request: Request):
    body, problem = await body_of(request)
    if problem:
        return problem
    target = feedback_target(body.get("target"))
    reason = body.get("reason")
    if target is None:
        return error(400, "bad_request", "target must be a card (item_id, market, date) or a claim "
                                         "(answer_id or brief_id, and claim_id).")
    if body.get("value") not in FEEDBACK_VALUES:
        return error(400, "bad_request", "value must be real, not_real, useful or wrong.")
    if not (reason is None or (isinstance(reason, str) and len(reason) <= MAX_REASON)):
        return error(400, "bad_request", f"reason must be words, up to {MAX_REASON:,} characters.")
    market = target.get("market")
    row = {"who": "passcode", "what": json.dumps({"target": target, "value": body["value"]}),
           "reason": reason, "at": now(market).isoformat()}
    if not await run_in_threadpool(append, "feedback", FEEDBACK, row):
        return not_saved("feedback")
    return JSONResponse({"ok": True}, status_code=202)


SCHEDULE_MARKETS = ("ZA", "NG", "KE")
SCHEDULE_TIERS = ("T0", "T1")
CADENCES = ("weekly_monday", "daily")
DELIVER = ("in_app", "channel")
MAX_SCHEDULE_QUESTION = 500
SCHEDULE_RUN_DAYS = 90  # how far back GET /api/schedules looks for each schedule's last run
SCHEDULES = []


def schedule_body(body):
    """(question, deliver, None) or (None, None, the reason in plain words)."""
    question, deliver = body.get("question"), body.get("deliver")
    if not isinstance(question, str) or not 3 <= len(question.strip()) <= MAX_SCHEDULE_QUESTION:
        return None, None, f"question must be 3 to {MAX_SCHEDULE_QUESTION} characters."
    if body.get("market") not in SCHEDULE_MARKETS:
        return None, None, "market must be ZA, NG or KE."
    if body.get("tier") not in SCHEDULE_TIERS:
        return None, None, "tier must be T0 or T1."
    if body.get("cadence") not in CADENCES:
        return None, None, "cadence must be weekly_monday or daily."
    if not isinstance(deliver, list) or not deliver or any(d not in DELIVER for d in deliver):
        return None, None, "deliver must list in_app, channel or both."
    return question.strip(), list(dict.fromkeys(deliver)), None


def schedule_row(schedule):
    return {**schedule, "deliver": json.dumps(schedule["deliver"])}


def schedule_of(row):
    out = dict(row)
    out["deliver"] = _json(out["deliver"])
    for key in ("created_at", "status_at"):
        if isinstance(out.get(key), datetime):
            out[key] = out[key].isoformat()
    return out


def current_schedules(schedule_id=None):
    """The latest row per schedule_id, in the same order as the watches (and L1's v_watches_current), newest first."""
    if os.environ.get("F42_DATA") == "bigquery":
        from google.cloud import bigquery
        where = "s.schedule_id = @schedule_id" if schedule_id else "TRUE"
        rows = _stored("schedules",
                       f"SELECT s.schedule_id, s.created_at, s.status_at, s.who, s.question, s.market, s.tier, "
                       f"s.cadence, s.deliver, s.status "
                       f"FROM `{project()}.intelligence_42_agent.schedules` s WHERE {where} "
                       f"QUALIFY ROW_NUMBER() OVER (PARTITION BY s.schedule_id ORDER BY COALESCE(s.status_at, "
                       f"s.created_at) DESC NULLS LAST, s.status DESC, TO_JSON_STRING(s)) = 1 "
                       f"ORDER BY COALESCE(status_at, created_at) DESC",
                       [bigquery.ScalarQueryParameter("schedule_id", "STRING", schedule_id)] if schedule_id else [])
        return [schedule_of(row) for row in rows]
    with _lock:
        rows = list(SCHEDULES)
    latest = {}
    for row in rows:
        if schedule_id in (None, row["schedule_id"]):
            latest.pop(row["schedule_id"], None)
            latest[row["schedule_id"]] = row
    return [schedule_of(row) for row in reversed(latest.values())]


def schedule_runs():
    """The latest scheduled run per schedule_id: its ask, or the skip or failure the job recorded.

    Scheduled run_ids are sched-<YYYY-MM-DD>-<schedule_id> (core/api/scheduled.py)."""
    if os.environ.get("F42_DATA") == "bigquery":
        from google.cloud import bigquery
        since = now().date() - timedelta(days=SCHEDULE_RUN_DAYS)
        rows = _stored("runs",
                       f"SELECT r.run_id, r.stage, r.run_date, r.status, r.outcome, r.finished_at, "
                       f"JSON_VALUE(r.record, '$.ask_id') AS ask_id "
                       f"FROM `{project()}.intelligence_42_agent.runs` r "
                       f"WHERE r.run_date >= @since AND STARTS_WITH(r.run_id, 'sched-') "
                       f"AND r.stage IN ('ask', 'scheduled')",
                       [bigquery.ScalarQueryParameter("since", "DATE", since)])
    else:
        with _lock:
            held = list(SINK)
        rows = [{**row, "ask_id": (_json(row.get("record")) or {}).get("ask_id")} for row in held
                if str(row.get("run_id", "")).startswith("sched-") and row.get("stage") in ("ask", "scheduled")]
    latest = {}
    for row in rows:
        key = (str(row["run_date"]), str(row.get("finished_at") or ""))
        schedule_id = row["run_id"][17:]
        if schedule_id not in latest or key >= latest[schedule_id][0]:
            latest[schedule_id] = (key, row)
    return {sid: row for sid, (_, row) in latest.items()}


def last_run(row):
    if row is None:
        return None
    return {"run_id": row["run_id"], "date": str(row["run_date"]), "status": row["status"],
            "ask_id": row.get("ask_id"), "outcome": row.get("outcome")}


@app.post("/api/schedules")
async def create_schedule(request: Request):
    body, problem = await body_of(request)
    if problem:
        return problem
    question, deliver, why = schedule_body(body)
    if why:
        return error(400, "bad_request", why)
    at = status_time(body["market"])
    schedule = {"schedule_id": f"s_{secrets.token_hex(6)}", "created_at": at, "status_at": at, "who": "passcode",
                "question": question, "market": body["market"], "tier": body["tier"], "cadence": body["cadence"],
                "deliver": deliver, "status": "active"}
    if not await run_in_threadpool(append, "schedules", SCHEDULES, schedule_row(schedule)):
        return not_saved("schedule")
    return JSONResponse(schedule, status_code=201)


def set_schedule_status(schedule_id, status):
    held = current_schedules(schedule_id)
    if not held:
        return error(404, "not_found", f"No schedule {schedule_id} is held here.")
    schedule = held[0]
    if schedule["status"] == status:
        return JSONResponse(schedule)
    schedule.update(status=status, status_at=status_time(schedule["market"]))
    if not append("schedules", SCHEDULES, schedule_row(schedule)):
        return not_saved("schedule")
    return JSONResponse(schedule)


@app.post("/api/schedules/{schedule_id}/pause")
def pause_schedule(schedule_id: str):
    return set_schedule_status(schedule_id, "paused")


@app.post("/api/schedules/{schedule_id}/resume")
def resume_schedule(schedule_id: str):
    return set_schedule_status(schedule_id, "active")


@app.get("/api/schedules")
def list_schedules():
    schedules = current_schedules()
    runs = schedule_runs() if schedules else {}
    out = []
    for schedule in schedules:
        run = last_run(runs.get(schedule["schedule_id"]))
        out.append({**schedule, "last_run": run,
                    "skip_reason": run["outcome"] if run and run["status"] == "skipped" else None})
    return {"schedules": out}


SKINS = []
SKIN_JSON = ("markets", "terms", "hashtags", "accounts", "watch_ids")


def skin_row(skin):
    return {**skin, **{key: json.dumps(skin[key]) for key in SKIN_JSON}}


def skin_of(row):
    out = dict(row)
    for key in SKIN_JSON:
        out[key] = _json(out[key])
    for key in ("created_at", "status_at"):
        if isinstance(out.get(key), datetime):
            out[key] = out[key].isoformat()
    return out


def current_skins(skin_id=None):
    """The latest row per skin_id, in the same order as the watches (and L1's v_watches_current), newest first."""
    if os.environ.get("F42_DATA") == "bigquery":
        from google.cloud import bigquery
        where = "k.skin_id = @skin_id" if skin_id else "TRUE"
        rows = _stored("skins",
                       f"SELECT k.skin_id, k.skin_key, k.created_at, k.status_at, k.who, k.name, k.markets, k.terms, "
                       f"k.hashtags, k.accounts, k.watch_ids, k.template, k.status "
                       f"FROM `{project()}.intelligence_42_agent.skins` k WHERE {where} "
                       f"QUALIFY ROW_NUMBER() OVER (PARTITION BY k.skin_id ORDER BY COALESCE(k.status_at, "
                       f"k.created_at) DESC NULLS LAST, k.status DESC, TO_JSON_STRING(k)) = 1 "
                       f"ORDER BY COALESCE(status_at, created_at) DESC",
                       [bigquery.ScalarQueryParameter("skin_id", "STRING", skin_id)] if skin_id else [])
        return [skin_of(row) for row in rows]
    with _lock:
        rows = list(SKINS)
    latest = {}
    for row in rows:
        if skin_id in (None, row["skin_id"]):
            latest.pop(row["skin_id"], None)
            latest[row["skin_id"]] = row
    return [skin_of(row) for row in reversed(latest.values())]


def no_skin(skin_id):
    return error(404, "not_found", f"No skin {skin_id} is held here.")


def save_skin(skin_id, body):
    """A new skin (skin_id None) or a new row holding an existing skin's full body; its words checked first."""
    try:
        accounts = skins.load_accounts()
    except Exception:
        log.exception("the approved accounts list could not be read")
        return error(500, "internal", "The approved accounts list could not be read.")
    clean, why = skins.validate(body, accounts)
    if why:
        return error(400, "bad_request", why)
    held = None
    if skin_id is not None:
        found = current_skins(skin_id)
        if not found:
            return no_skin(skin_id)
        held = found[0]
    labels = []
    if clean["watch_ids"]:
        watches = {w["watch_id"]: w for w in current_watches()}
        unknown = [w for w in clean["watch_ids"] if w not in watches]
        if unknown:
            return error(400, "bad_request", "watch_ids names watches 42 does not hold: " + ", ".join(unknown) + ".")
        labels = [watches[w].get("label") or "" for w in clean["watch_ids"]]
    try:
        why = skins.check_words([clean["skin_key"], clean["name"], *clean["terms"], *clean["hashtags"], *labels])
    except skins.Unavailable as exc:
        log.error("core.collect.gdelt is not importable; refusing skin writes")
        return error(503, "agent_unavailable", str(exc))
    if why:
        return error(400, "bad_request", why)
    at = status_time(clean["markets"][0])
    skin = {"skin_id": held["skin_id"] if held else f"sk_{secrets.token_hex(6)}", "skin_key": clean["skin_key"],
            "created_at": held["created_at"] if held else at, "status_at": at, "who": "passcode",
            "name": clean["name"], "markets": clean["markets"], "terms": clean["terms"],
            "hashtags": clean["hashtags"], "accounts": clean["accounts"], "watch_ids": clean["watch_ids"],
            "template": clean["template"], "status": held["status"] if held else "active"}
    if not append("skins", SKINS, skin_row(skin)):
        return not_saved("skin")
    return JSONResponse(skin, status_code=200 if held else 201)


@app.post("/api/skins")
async def create_skin(request: Request):
    body, problem = await body_of(request)
    if problem:
        return problem
    return await run_in_threadpool(save_skin, None, body)


@app.put("/api/skins/{skin_id}")
async def edit_skin(skin_id: str, request: Request):
    body, problem = await body_of(request)
    if problem:
        return problem
    return await run_in_threadpool(save_skin, skin_id, body)


@app.post("/api/skins/{skin_id}/archive")
def archive_skin(skin_id: str):
    held = current_skins(skin_id)
    if not held:
        return no_skin(skin_id)
    skin = held[0]
    if skin["status"] == "archived":
        return JSONResponse(skin)
    skin.update(status="archived", status_at=status_time(skin["markets"][0]))
    if not append("skins", SKINS, skin_row(skin)):
        return not_saved("skin")
    return JSONResponse(skin)


@app.get("/api/skins")
def list_skins():
    return {"skins": current_skins()}


@app.get("/api/skins/{skin_id}")
def read_skin(skin_id: str):
    held = current_skins(skin_id)
    return JSONResponse(held[0]) if held else no_skin(skin_id)


def skin_people(skin_id, record=None):
    """Who a skin report may name (contract 15.1): the approved accounts for its skin_key, and the authors of the
    record's evidence that a named page allows now (section 12.1). Without a record only approved accounts."""
    from core.api.store import get_store
    held = current_skins(skin_id)
    evidence = ((record or {}).get("answer") or {}).get("evidence") or []
    return {"skin_id": skin_id, **skins.people_for(held[0]["skin_key"] if held else None, evidence, get_store)}


def shown_record(record):
    """An Ask record as a reader sees it: a skin run's record is masked (contract 15.1), any other is unchanged."""
    if not record or not record.get("skin_id"):
        return record
    if isinstance(record.get("run"), dict) and any(key in record["run"] for key in ("ranked_list", "entity_lists")):
        record = {**record, "run": {k: v for k, v in record["run"].items() if k not in ("ranked_list", "entity_lists")}}
    people = skin_people(record["skin_id"], record)
    return skins.mask_people(record, people["approved"], people["allowed"])


def readable(record, hidden=privacy.READ, creators=None):
    """An Ask record as a reader may see it now: the skin masks first (a skin report names only who it may), then the
    list of hidden people read on this request, which wins over an approved account (C5 v2 5.2, R2). hidden is the
    list when the request has already read it."""
    from core.api.store import get_store
    # The typed summary state is read from the raw record first: privacy changes the answer it is bound to.
    return privacy.project_record(shown_record(summary_state.with_wire(record)), privacy.LazyStore(get_store), hidden,
                                  creators=creators)


def people_unavailable():
    return error(503, "people_unavailable", privacy.PEOPLE_UNAVAILABLE)


def in_one_read(fn, *args):
    """fn with one reading of the hidden-people list shared by its passes (core/api/privacy.py one_read)."""
    with privacy.one_read():
        return fn(*args)


def view_response(view, status=200):
    return JSONResponse(view, status_code=status, headers=privacy.NO_STORE)


def plan_skin_id(plan):
    """The skin a report investigation is scoped to; its plan carries it, since the investigations table has no
    column for it."""
    return plan.get("skin_id") if isinstance(plan, dict) else None


DOSSIER_VERSIONS = []
DOSSIER_REVIEWS = []
MAX_DOSSIER_LIST = 200
_dossier_lock = threading.Lock()
_finding_lock = threading.Lock()


def _stored(table, sql, params):
    """Rows from intelligence_42_agent.<table>, or none while the table does not exist yet."""
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery
    config = bigquery.QueryJobConfig(query_parameters=params, maximum_bytes_billed=2 * 1024 ** 3)
    try:
        return [dict(row) for row in bigquery_client().query(sql, job_config=config).result()]
    except NotFound:
        log.warning("intelligence_42_agent.%s does not exist yet; reading it as empty", table)
        return []


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def dossier_versions(dossier_id):
    """Every stored version of one dossier, oldest first, each with its body as a dict."""
    if os.environ.get("F42_DATA") == "bigquery":
        from google.cloud import bigquery
        rows = _stored("dossier_versions",
                       f"SELECT dossier_id, version, state, body, source_ask_id, content_hash "
                       f"FROM `{project()}.intelligence_42_agent.dossier_versions` "
                       f"WHERE dossier_id = @dossier_id ORDER BY version",
                       [bigquery.ScalarQueryParameter("dossier_id", "STRING", dossier_id)])
    else:
        with _lock:
            rows = [row for row in DOSSIER_VERSIONS if row["dossier_id"] == dossier_id]
    return [{**row, "body": _json(row["body"])} for row in rows]


def dossier_ticks(dossier_id):
    """The latest review row per claim_id: ticks belong to the claim, not the version."""
    if os.environ.get("F42_DATA") == "bigquery":
        from google.cloud import bigquery
        rows = _stored("dossier_reviews",
                       f"SELECT dossier_id, claim_id, ticked, note, who, `at` "
                       f"FROM `{project()}.intelligence_42_agent.dossier_reviews` WHERE dossier_id = @dossier_id "
                       f"QUALIFY ROW_NUMBER() OVER (PARTITION BY claim_id ORDER BY `at` DESC) = 1",
                       [bigquery.ScalarQueryParameter("dossier_id", "STRING", dossier_id)])
    else:
        with _lock:
            rows = [row for row in DOSSIER_REVIEWS if row["dossier_id"] == dossier_id]
    return {cid: {"ticked": row["ticked"], "note": row["note"], "who": row["who"],
                  "at": row["at"].isoformat() if isinstance(row["at"], datetime) else row["at"]}
            for cid, row in dossiers.latest_ticks(rows).items()}


def current_people(body):
    """A skin dossier's body with its named authors checked again now (contract 15.1 and 12.1): an author suppressed
    or no longer at page tier since the dossier was made is masked from then on. The stored version never changes."""
    people = body.get("people")
    if not people or not people.get("allowed"):
        return body
    from core.api.store import get_store
    return {**body, "people": {**people, "allowed": skins.still_nameable(people["allowed"], get_store)}}


def dossier_view(version, ticks):
    """A dossier version as a reader sees it; for a skin report the whole view is masked, the ticks' notes too."""
    from core.api.store import get_store
    body = current_people(version["body"])
    view = dossiers.shown({**body, "content_hash": version["content_hash"], "ticks": ticks,
                           "needs_tick": dossiers.needs_tick(body, ticks)})
    return privacy.project_dossier(view, privacy.LazyStore(get_store))


def refused(exc):
    out = {"error": exc.code, "message": exc.message}
    if exc.claims:
        out["claims"] = exc.claims
    return JSONResponse(out, status_code=exc.status)


def no_dossier(dossier_id):
    return error(404, "not_found", f"No dossier {dossier_id} is stored.")


def source_record(ask_id):
    """The Ask record a dossier is built from: held here while fresh, else read back from runs."""
    held = get_ask(ask_id)
    if held is not None:
        return held.snapshot()
    from core.api.store import get_store
    return get_store().ask_record(ask_id)


def save_finding_from_ask(ask_id):
    """Append checked claims from an eligible stored Ask and confirm the exact Findings row."""
    from core.api.store import get_store

    try:
        saved = get_store()
        record = saved.ask_record(ask_id)
    except Exception:
        return error(503, "save_unconfirmed", "The finding could not be confirmed; try again.")
    if record is None:
        return error(404, "not_found", "No saved Ask with that ID exists.")
    try:
        hits, question_named = privacy.finding_blockers(record, saved)
    except Exception:
        return people_unavailable()
    if hits:
        return error(409, "not_eligible", f"{', '.join(hits)} rest on posts, or name people, 42 no longer shows, "
                                         "so this answer cannot become a finding.")
    if question_named:
        return error(409, "not_eligible", "The question names a person 42 no longer shows, so this answer cannot "
                                         "become a finding.")

    try:
        post_ids = finding_save.evidence_ids(ask_id, record)
        posts = saved.posts_by_id(post_ids)
        row = finding_save.build(ask_id, record, posts)
    except dossiers.Refused as exc:
        return refused(exc)
    except Exception:
        return error(503, "save_unconfirmed", "The finding could not be confirmed; try again.")

    with _finding_lock:
        try:
            existing = saved.finding_rows(row["finding_id"])
        except Exception:
            return error(503, "save_unconfirmed", "The finding could not be confirmed; try again.")
        if not isinstance(existing, list):
            return error(503, "save_unconfirmed", "The finding could not be confirmed; try again.")
        if existing:
            if len(existing) == 1 and finding_save.same_row(row, existing[0]):
                return JSONResponse({"finding_id": row["finding_id"], "source_ask_id": ask_id})
            return error(409, "conflict", "A different Findings row already uses this ID.")

        try:
            saved.insert_finding(row)
        except Exception:
            # The insert may have reached BigQuery before its response was lost. Read it back below.
            pass
        try:
            confirmed = saved.finding_rows(row["finding_id"])
        except Exception:
            return error(503, "save_unconfirmed", "The finding could not be confirmed; try again.")
        if (isinstance(confirmed, list) and len(confirmed) == 1
                and finding_save.same_row(row, confirmed[0])):
            return JSONResponse({"finding_id": row["finding_id"], "source_ask_id": ask_id}, status_code=201)
        return error(503, "save_unconfirmed", "The finding could not be confirmed; try again.")


def save_version(body, ticks, status):
    row = {"dossier_id": body["dossier_id"], "version": body["version"], "created_at": body["created_at"],
           "who": body["who"], "state": body["state"], "body": json.dumps(body),
           "source_ask_id": body["source_ask_id"], "content_hash": dossiers.content_hash(body)}
    if not append("dossier_versions", DOSSIER_VERSIONS, row):
        return not_saved("dossier")
    return view_response(dossier_view({"body": body, "content_hash": row["content_hash"]}, ticks), status)


def create_dossier_from(ask_id, change):
    record = source_record(ask_id)
    if record is None:
        return error(404, "not_found", f"No ask {ask_id} is held here or stored.")
    people = skin_people(record["skin_id"], record) if record.get("skin_id") else None
    return first_version(record, change, {"ask_id": ask_id}, people)


def keepable(record):
    """{claim id: whether the list lets a dossier keep it now} for the source answer, or None when who is hidden
    cannot be checked (C5 v2 section 7: build from what the projection leaves, refuse the rest)."""
    from core.api.store import get_store
    answer = record.get("answer") if isinstance(record, dict) else None
    if not isinstance(answer, dict):
        return {}
    store = privacy.LazyStore(get_store)
    hidden = privacy.read_hidden(store)
    if hidden is None:
        return None
    claims, _ = dossiers.admitted(answer)
    try:
        return privacy.selectable_claims(claims, answer.get("evidence"), hidden, store)
    except privacy.PeopleUnavailable:
        return None


def stored_words(change, latest):
    """change with the title and notes the page sent back unchanged put back to what is stored. The page shows a
    dossier through the list of hidden people and sends the words it was shown on every edit; a title or note equal
    to the masked form of the stored one is the stored one, not new words. Masked text is never stored (R5, A37).
    The list can change between the page load and the edit, so a lift leaves the page holding masked words that the
    list now read no longer reproduces: the form with every handle masked is accepted too, and words that carry a
    mask the stored words do not are refused, never stored."""
    from core.api.store import get_store
    hidden = privacy.read_hidden(privacy.LazyStore(get_store))

    def same(sent, held):
        forms = {privacy.mask({"text": held}, None)["text"]}
        if not privacy.nothing_hidden(hidden):
            forms.add(privacy.mask({"text": held}, hidden)["text"])
        return sent.strip() in forms

    def carries_a_mask(sent, held):
        return any(token in sent and token not in held for token in (skins.MASK, skins.URL_MASK, skins.LINK_MASK))

    def settle(sent, held, what):
        if not isinstance(sent, str):
            return sent
        text = held if isinstance(held, str) else ""  # a claim with no note holds no words, so any mask is new
        if sent.strip() == text.strip():
            return sent
        if isinstance(held, str) and same(sent, held):
            return held
        if carries_a_mask(sent, text):
            raise dossiers.Refused(409, "not_ready", f"The {what} you sent holds words that were hidden when the page "
                                                     "was loaded. Reload the dossier and send your edit again.")
        return sent

    change = dict(change)
    if "title" in change:
        change["title"] = settle(change["title"], latest["title"], "title")
    if isinstance(change.get("notes"), dict):
        held = {c["claim_id"]: c.get("note") for c in latest["claims"]}
        change["notes"] = {cid: settle(note, held.get(cid), "note") for cid, note in change["notes"].items()}
    return change


def not_keepable(ids):
    return error(409, "not_ready", f"{', '.join(ids)} cannot be kept: it rests on a post, or names a person, "
                                   "42 no longer shows.")


def first_version(record, change, source, people=None):
    ok = keepable(record)
    if ok is None:
        return people_unavailable()
    change = dict(change)
    if "keep" not in change:
        change["keep"] = [cid for cid, fine in ok.items() if fine]
    elif isinstance(change["keep"], list) and [c for c in change["keep"] if ok.get(c) is False]:
        return not_keepable([c for c in change["keep"] if ok.get(c) is False])
    try:
        sel = dossiers.selection(change, record, None)
    except dossiers.Refused as exc:
        return refused(exc)
    body = dossiers.build(record, sel, dossier_id=f"d_{secrets.token_hex(6)}", version=1,
                          created_at=status_time(record.get("market")), source=source, people=people)
    return save_version(body, {}, 201)


def create_dossier_from_investigation(investigation_id, change):
    """Version 1 from a finished investigation's answer; its ask_id comes from memory, else the investigations table."""
    with _lock:
        running = RUNNING_INVESTIGATIONS.get(investigation_id)
    if running is not None:
        ask_id, skin_id = running.request["ask_id"], running.record.get("skin_id")
    else:
        rows = investigation_rows(investigation_id)
        if not rows:
            return no_investigation(investigation_id)
        ask_id, skin_id = rows[-1]["ask_id"], plan_skin_id(investigations.view(rows[-1])["plan"])
        if not ask_id:
            return error(409, "not_ready", f"Investigation {investigation_id} is {rows[-1]['status']} and has "
                                           "not finished; only a finished investigation can become a dossier.")
    record = investigation_record(ask_id)
    if record is None:
        return error(404, "not_found", f"The answer of investigation {investigation_id} ({ask_id}) is not held "
                                       "here or stored.")
    if record.get("status") == "running":
        return error(409, "not_ready", f"Investigation {investigation_id} is running and has not finished; "
                                       "only a finished investigation can become a dossier.")
    people = skin_people(skin_id, record) if skin_id else None
    return first_version(record, change, {"investigation_id": investigation_id}, people)


@app.post("/api/dossiers")
async def create_dossier(request: Request):
    body, problem = await body_of(request)
    if problem:
        return problem
    origin = body.get("from")
    if not (isinstance(origin, dict) and len(origin) == 1
            and any(_text(origin.get(key)) for key in ("ask_id", "investigation_id"))):
        return error(400, "bad_request", "from must hold one ask_id or one investigation_id.")
    change = {"title": body["title"]} if "title" in body else {}
    if "investigation_id" in origin:
        return await run_in_threadpool(in_one_read, create_dossier_from_investigation,
                                       origin["investigation_id"].strip(), change)
    return await run_in_threadpool(in_one_read, create_dossier_from, origin["ask_id"].strip(), change)


@app.post("/api/findings")
async def save_finding(request: Request):
    body, problem = await body_of(request)
    if problem:
        return problem
    origin = body.get("from")
    ask_id = origin.get("ask_id") if isinstance(origin, dict) else None
    if (set(body) != {"from"} or not isinstance(origin, dict) or set(origin) != {"ask_id"}
            or not isinstance(ask_id, str) or re.fullmatch(r"a_[0-9]{8}_[0-9a-f]{8}", ask_id) is None):
        return error(400, "bad_request", "from must hold one valid saved ask_id.")
    return await run_in_threadpool(save_finding_from_ask, ask_id)


def edit_dossier_to(dossier_id, change, from_version=None):
    with _dossier_lock:
        versions = dossier_versions(dossier_id)
        if not versions:
            return no_dossier(dossier_id)
        latest = versions[-1]["body"]
        try:
            dossiers.check_current(from_version, latest["version"])
        except dossiers.Refused as exc:
            return refused(exc)
        record = source_record(latest["source_ask_id"])
        if record is None:
            return error(409, "not_ready", f"The source answer {latest['source_ask_id']} can no longer be read.")
        ok = keepable(record)
        if ok is None:
            return people_unavailable()
        try:
            change = stored_words(change, latest)
        except dossiers.Refused as exc:
            return refused(exc)
        if "keep" not in change:
            # The edit carries the earlier choice forward. A claim the list no longer lets it keep is not dropped
            # quietly: the reader is told which, and sends keep without them to go on.
            stuck = [c["claim_id"] for c in latest["claims"] if c["kept"] and ok.get(c["claim_id"]) is False]
            if stuck:
                return error(409, "not_ready", f"{', '.join(stuck)} cannot be kept any more: it rests on a post, or "
                                               "names a person, 42 no longer shows. Send keep without it to save a "
                                               "new version.")
        elif isinstance(change["keep"], list) and [c for c in change["keep"] if ok.get(c) is False]:
            return not_keepable([c for c in change["keep"] if ok.get(c) is False])
        try:
            sel = dossiers.selection(change, record, latest)
        except dossiers.Refused as exc:
            return refused(exc)
        body = dossiers.build(record, sel, dossier_id=dossier_id, version=latest["version"] + 1,
                              created_at=status_time(latest.get("market")), source=latest["source"],
                              people=latest.get("people"))
        return save_version(body, dossier_ticks(dossier_id), 200)


@app.put("/api/dossiers/{dossier_id}")
async def edit_dossier(dossier_id: str, request: Request):
    body, problem = await body_of(request)
    if problem:
        return problem
    change = {key: body[key] for key in ("keep", "order", "title", "notes") if key in body}
    return await run_in_threadpool(in_one_read, edit_dossier_to, dossier_id, change, body.get("from_version"))


def tick_row(dossier_id, claim_id, ticked, note):
    versions = dossier_versions(dossier_id)
    if not versions:
        return no_dossier(dossier_id)
    latest = versions[-1]["body"]
    if claim_id not in {c["claim_id"] for c in latest["claims"]}:
        return error(400, "bad_request", f"{claim_id} is not a claim in this dossier.")
    row = {"dossier_id": dossier_id, "claim_id": claim_id, "ticked": ticked, "note": (note or "").strip() or None,
           "who": "passcode", "at": status_time(latest.get("market"))}
    if not append("dossier_reviews", DOSSIER_REVIEWS, row):
        return not_saved("tick")
    return JSONResponse(row, status_code=201)


@app.post("/api/dossiers/{dossier_id}/ticks")
async def tick_claim(dossier_id: str, request: Request):
    body, problem = await body_of(request)
    if problem:
        return problem
    claim_id, ticked, note = body.get("claim_id"), body.get("ticked"), body.get("note")
    if not _text(claim_id) or not isinstance(ticked, bool):
        return error(400, "bad_request", "A tick needs claim_id and ticked true or false.")
    if not (note is None or (isinstance(note, str) and len(note) <= dossiers.MAX_NOTE)):
        return error(400, "bad_request", f"note must be words, up to {dossiers.MAX_NOTE:,} characters.")
    return await run_in_threadpool(tick_row, dossier_id, claim_id, ticked, note)


@app.post("/api/dossiers/{dossier_id}/freeze")
async def freeze_dossier(dossier_id: str, request: Request):
    body, problem = await body_of(request) if (await request.body()).strip() else ({}, None)
    if problem:
        return problem
    return await run_in_threadpool(in_one_read, freeze_dossier_to, dossier_id, body.get("from_version"))


def freeze_dossier_to(dossier_id, from_version=None):
    with _dossier_lock:
        versions = dossier_versions(dossier_id)
        if not versions:
            return no_dossier(dossier_id)
        latest = versions[-1]
        try:
            dossiers.check_current(from_version, latest["version"])
        except dossiers.Refused as exc:
            return refused(exc)
        body, ticks = latest["body"], dossier_ticks(dossier_id)
        if body["state"] == "frozen":
            return view_response(dossier_view(latest, ticks))
        if not any(c["kept"] for c in body["claims"]):
            return error(409, "not_ready", "Keep at least one claim before freezing.")
        record = source_record(body["source_ask_id"])
        if record is None:
            return error(409, "not_ready", f"The source answer {body['source_ask_id']} can no longer be read.")
        ok = keepable(record)
        if ok is None:
            return people_unavailable()
        try:
            found = dossiers.needs_tick(body, ticks) + dossiers.recheck(body, record)
        except dossiers.Refused as exc:
            return refused(exc)
        found += [{"claim_id": c["claim_id"], "reason": "rests on a post, or names a person, 42 no longer shows"}
                  for c in body["claims"] if c["kept"] and ok.get(c["claim_id"]) is False]
        if found:
            reasons = {}
            for item in found:
                reasons.setdefault(item["claim_id"], []).append(item["reason"])
            claims = [{"claim_id": cid, "reason": "; ".join(why)} for cid, why in reasons.items()]
            return refused(dossiers.Refused(
                409, "not_ready", "Cannot freeze yet: " + "; ".join(f"{c['claim_id']}: {c['reason']}" for c in claims),
                claims))
        frozen = dossiers.freeze(body, version=body["version"] + 1, created_at=status_time(body.get("market")),
                                 ticks=ticks)
        return save_version(frozen, ticks, 201)


def dossier_summary(body, frozen_version):
    return {"dossier_id": body["dossier_id"], "version": body["version"], "state": body["state"],
            "title": body["title"], "created_at": body["created_at"], "source_ask_id": body["source_ask_id"],
            "question": body.get("question"), "market": body.get("market"),
            "kept": sum(1 for c in body["claims"] if c["kept"]), "frozen_version": frozen_version}


@app.get("/api/dossiers")
def list_dossiers(limit: str = "50", before: str | None = None):
    try:
        limit = int(limit)
        cutoff = datetime.fromisoformat(before) if before else None
    except ValueError:
        return error(400, "bad_request", "limit must be a whole number and before an ISO 8601 time.")
    if not 1 <= limit <= MAX_DOSSIER_LIST:
        return error(400, "bad_request", f"limit must be 1 to {MAX_DOSSIER_LIST}.")
    if cutoff is not None and cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    if os.environ.get("F42_DATA") == "bigquery":
        from google.cloud import bigquery
        rows = _stored("dossier_versions",
                       f"SELECT body, frozen_version FROM ("
                       f"SELECT body, created_at, "
                       f"MAX(IF(state = 'frozen', version, NULL)) OVER (PARTITION BY dossier_id) AS frozen_version, "
                       f"ROW_NUMBER() OVER (PARTITION BY dossier_id ORDER BY version DESC) AS n "
                       f"FROM `{project()}.intelligence_42_agent.dossier_versions`) "
                       f"WHERE n = 1 AND (@before IS NULL OR created_at < @before) "
                       f"ORDER BY created_at DESC LIMIT @limit",
                       [bigquery.ScalarQueryParameter("before", "TIMESTAMP", cutoff),
                        bigquery.ScalarQueryParameter("limit", "INT64", limit)])
        listed = [dossier_summary(_json(row["body"]), row["frozen_version"]) for row in rows]
    else:
        with _lock:
            rows = list(DOSSIER_VERSIONS)
        latest, frozen = {}, {}
        for row in rows:
            latest[row["dossier_id"]] = _json(row["body"])
            if row["state"] == "frozen":
                frozen[row["dossier_id"]] = row["version"]
        bodies = sorted((b for b in latest.values()
                         if cutoff is None or datetime.fromisoformat(b["created_at"]) < cutoff),
                        key=lambda b: (datetime.fromisoformat(b["created_at"]), b["dossier_id"]), reverse=True)
        listed = [dossier_summary(b, frozen.get(b["dossier_id"])) for b in bodies[:limit]]
    from core.api.store import get_store
    store = privacy.LazyStore(get_store)
    hidden = privacy.read_hidden(store)
    shown_rows = [privacy.project_summary(row, store, hidden) for row in listed]
    return view_response({"dossiers": shown_rows,
                          "next_before": listed[-1]["created_at"] if len(listed) == limit else None})


@app.get("/api/dossiers/{dossier_id}")
def read_dossier(dossier_id: str):
    versions = dossier_versions(dossier_id)
    if not versions:
        return no_dossier(dossier_id)
    return view_response(dossier_view(versions[-1], dossier_ticks(dossier_id)))


def dossier_version(dossier_id, version):
    return next((v for v in dossier_versions(dossier_id) if v["version"] == version), None)


@app.get("/api/dossiers/{dossier_id}/versions/{version}")
def read_dossier_version(dossier_id: str, version: int):
    held = dossier_version(dossier_id, version)
    if held is None:
        return error(404, "not_found", f"Dossier {dossier_id} has no version {version}.")
    return view_response(dossier_view(held, dossier_ticks(dossier_id)))


@app.get("/api/dossiers/{dossier_id}/versions/{version}/export")
def export_dossier(dossier_id: str, version: int, format: str = "html"):
    if format not in ("html", "pdf"):
        return error(400, "bad_request", "format must be html or pdf.")
    held = dossier_version(dossier_id, version)
    if held is None:
        return error(404, "not_found", f"Dossier {dossier_id} has no version {version}.")
    from core.api.store import get_store
    projected = privacy.project_dossier(current_people(held["body"]), privacy.LazyStore(get_store))
    if (projected.get("privacy") or {}).get("state") == "unavailable":
        return people_unavailable()
    try:
        page = dossiers.render_html(projected)
    except dossiers.Refused as exc:
        return refused(exc)
    headers = {"Content-Disposition": f'attachment; filename="{dossiers.export_filename(dossier_id, version, format)}"'}
    if format == "html":
        return Response(page, media_type="text/html; charset=utf-8", headers=headers)
    from core.api import pdf
    try:
        data = pdf.render_pdf(page)
    except pdf.PdfError as exc:
        if exc.reason == "pdf_render_busy":
            return error(429, "rate_limited", "Another PDF is rendering; try again in a moment.")
        log.error("dossier %s v%s: PDF render failed: %s", dossier_id, version, exc.reason)
        return error(500, "internal", "The PDF could not be rendered; the HTML export still works.")
    return Response(data, media_type="application/pdf", headers=headers)


INVESTIGATIONS = []
RUNNING_INVESTIGATIONS = {}
MAX_INVESTIGATION_LIST = 200
# Held across each budget read and the reservation or row it leads to, so two starts never spend the same money.
_investigation_lock = threading.Lock()


def resolve_investigator():
    """(plan_investigation, estimate_plan, run_ask) from L3, or None until T3 lands (contract 13.3)."""
    if os.environ.get("F42_AGENT") == "fixture":
        return investigations.fixture_planner, investigations.fixture_estimate, fixture_agent
    run_ask = resolve_run_ask()
    if run_ask is None:
        return None
    for name in ("core.agent.investigate", "core.agent"):
        try:
            module = importlib.import_module(name)
        except ImportError as exc:
            log.warning("%s not importable: %s", name, exc)
            continue
        plan, estimate = getattr(module, "plan_investigation", None), getattr(module, "estimate_plan", None)
        if plan is not None and estimate is not None:
            return plan, estimate, run_ask
    return None


def spent_today():
    """Today's credits under ASK_DAILY and model dollars under MODEL_DAILY_USD; None for a figure that cannot be read."""
    day = now().date().isoformat()
    if os.environ.get("F42_DATA") == "bigquery":
        try:
            client = bigquery_client()
        except Exception as exc:
            log.warning("no BigQuery client for today's spend (%s); treating it as unknown", type(exc).__name__)
            return {"credits": None, "model_usd": None}
        return investigations.spent_in_bigquery(client, project(), day)
    with _lock:
        rows = list(SINK)
    return investigations.spent_in_rows(rows, day)


def planner_spend_snapshot(run_id, run_date):
    """One ledger snapshot with the daily total and this planner ceiling's exact run_id."""
    if os.environ.get("F42_DATA") == "bigquery":
        from google.cloud import bigquery

        client = bigquery_client()
        columns = investigations.runs_columns(client, project())
        parts = []
        if "model_usd" in columns:
            parts.append("r.model_usd")
        if "record" in columns:
            parts.append("SAFE_CAST(JSON_VALUE(r.record, '$.run.model_usd') AS FLOAT64)")
        if "counts" in columns:
            parts.append("SAFE_CAST(JSON_VALUE(r.counts, '$.model_usd') AS FLOAT64)")
        if not parts:
            return {"model_usd": None, "planner_model_usd": None}
        usd = parts[0] if len(parts) == 1 else f"COALESCE({', '.join(parts)})"
        sql = ("WITH per_run AS (SELECT r.run_id, MAX(" + usd + ") AS usd "
               f"FROM `{project()}.intelligence_42_agent.runs` r WHERE r.run_date = @d GROUP BY r.run_id) "
               "SELECT IFNULL(SUM(usd), 0) AS model_usd, "
               "MAX(IF(run_id = @run_id, usd, NULL)) AS planner_model_usd FROM per_run")
        config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", run_date),
                              bigquery.ScalarQueryParameter("run_id", "STRING", run_id)],
            maximum_bytes_billed=investigations.MAX_BYTES, use_query_cache=False)
        rows = [dict(row) for row in client.query(sql, job_config=config).result()]
        return rows[0] if rows else {"model_usd": None, "planner_model_usd": None}
    with _lock:
        rows = list(SINK)
    day_rows = [row for row in rows if row.get("run_date") == run_date]
    run_rows = [row for row in day_rows if row.get("run_id") == run_id]
    total = investigations.spent_in_rows(day_rows, run_date)["model_usd"]
    planner = investigations.spent_in_rows(run_rows, run_date)["model_usd"] if run_rows else None
    return {"model_usd": total, "planner_model_usd": planner}


def planner_booking_confirmed(snapshot, ceiling):
    if not isinstance(snapshot, dict):
        return False
    total, planner = snapshot.get("model_usd"), snapshot.get("planner_model_usd")
    return (_number(total) and math.isfinite(total) and _number(planner) and math.isfinite(planner) and
            round(planner, investigations.ROUND_USD) >= round(ceiling, investigations.ROUND_USD))


def money_snapshot(run_date=None):
    day = run_date or now().date().isoformat()
    if now().date().isoformat() != day:
        return {"credits": None, "model_usd": None}, None, False
    try:
        spent = spent_today()
    except Exception as exc:
        log.warning("reading today's spend failed (%s); treating it as unknown", type(exc).__name__)
        spent = {"credits": None, "model_usd": None}
    if now().date().isoformat() != day:
        return spent, None, False
    for key, hold in list(_PLANNER_READBACK_HOLDS.items()):
        if hold["run_date"] != day:
            investigations.RESERVED.release(key)
            _PLANNER_READBACK_HOLDS.pop(key, None)
            continue
        try:
            snapshot = planner_spend_snapshot(hold["run_id"], hold["run_date"])
        except Exception as exc:
            log.error("investigation planner ceiling readback failed (exception_type=%s)", type(exc).__name__)
            snapshot = None
        if planner_booking_confirmed(snapshot, hold["usd"]):
            investigations.RESERVED.release(key)
            _PLANNER_READBACK_HOLDS.pop(key, None)
            spent["model_usd"] = snapshot["model_usd"]
    if now().date().isoformat() != day:
        return spent, None, False
    if _PLANNER_READBACK_HOLDS:
        return spent, None, True
    left = investigations.budget_left(spent, investigations.RESERVED.total())
    if now().date().isoformat() != day:
        return spent, None, False
    return spent, left, False


def money_left():
    return money_snapshot()[1]


def need_t3():
    return error(503, "agent_unavailable", investigations.NEED_T3)


def no_investigation(investigation_id):
    return error(404, "not_found", f"No investigation {investigation_id} is stored.")


def budget_unknown(what):
    return error(409, "not_ready", investigations.BUDGET_UNKNOWN.format(what=what))


def planner_budget_day_changed():
    return error(409, "not_ready", "The model budget day changed; retry the draft.")


def investigation_rows(investigation_id):
    """Every stored row of one investigation, oldest version first."""
    if os.environ.get("F42_DATA") == "bigquery":
        from google.cloud import bigquery
        rows = _stored("investigations",
                       f"SELECT investigation_id, version, created_at, who, status, question, market, plan, estimate, "
                       f"ask_id, run_id FROM `{project()}.intelligence_42_agent.investigations` "
                       f"WHERE investigation_id = @investigation_id ORDER BY version",
                       [bigquery.ScalarQueryParameter("investigation_id", "STRING", investigation_id)])
    else:
        with _lock:
            rows = [row for row in INVESTIGATIONS if row["investigation_id"] == investigation_id]
    return sorted(rows, key=lambda row: row["version"])


def investigation_out(row, first_created_at, **extra):
    """The latest row as the API shows it: created_at is the draft's time, updated_at this row's."""
    out = investigations.view(row)
    out["updated_at"] = out["created_at"]
    out["created_at"] = first_created_at.isoformat() if isinstance(first_created_at, datetime) else first_created_at
    return {**out, **extra}


_runs_have_model_usd = False  # set once runs.model_usd is seen; no column is ever dropped


def runs_have_model_usd():
    """True once intelligence_42_agent.runs has L1's model_usd column; unknown counts as no."""
    global _runs_have_model_usd
    if not _runs_have_model_usd:
        try:
            _runs_have_model_usd = "model_usd" in investigations.runs_columns(bigquery_client(), project())
        except Exception as exc:
            log.warning("reading the runs columns failed (%s); the planner's spend goes in its record only",
                        type(exc).__name__)
    return _runs_have_model_usd


def planner_row(investigation_id, question, run, status, started, started_at, *, run_id=None, run_date=None,
                stage="investigation", actual_model_usd=None):
    """An append-only planner or signed spend row; model_usd is omitted when actual usage is informational."""
    run = run or {}
    record = {"investigation_id": investigation_id, "run": run}
    if actual_model_usd is not None:
        record["actual_model_usd"] = actual_model_usd
    row = {"run_id": run_id or run.get("run_id") or f"r_plan_{investigation_id[2:]}", "stage": stage,
           "run_date": run_date or now().date().isoformat(), "status": status, "started_at": started_at,
           "finished_at": None if status == "running" else now().isoformat(), "question": question,
           "tier": run.get("tier") or "T3", "credits": run.get("credits") or 0,
           "seconds": run.get("seconds", 0 if status == "running" else round(time.monotonic() - started, 1)),
           "outcome": "draft" if status == "ok" else "internal", "answer": None, "record": json.dumps(record)}
    if os.environ.get("F42_DATA") != "bigquery" or runs_have_model_usd():
        row["model_usd"] = run.get("model_usd")
    return row


def save_planner_row(row):
    try:
        saved = append("runs", SINK, row)
    except Exception as exc:
        log.error("investigation planner runs row save failed (exception_type=%s)", type(exc).__name__)
        return False
    if saved is not True:
        if row["stage"] == "investigation" and row["status"] == "running":
            log.error("investigation planner ceiling booking was not confirmed; the planner was not called")
        else:
            log.error("investigation planner %s: its runs row was not confirmed; the ceiling remains booked",
                      row["run_id"])
        return False
    return True


def settle_planner_booking(investigation_id, question, run, status, started, started_at, planner_run_id, run_date):
    """Write planner metadata and a signed adjustment against its confirmed ceiling, returning counted USD or None."""
    run = run or {}
    usage = run.get("model_usd")
    if not (_number(usage) and math.isfinite(usage) and usage >= 0):
        usage = None
    else:
        usage = round(float(usage), 6)
    stored_run = dict(run)
    stored_run.pop("model_usd", None)
    if not save_planner_row(planner_row(investigation_id, question, stored_run, status, started, started_at,
                                       run_id=planner_run_id, run_date=run_date, actual_model_usd=usage)):
        return None
    if usage is None:
        return investigations.PLANNER_USD
    adjustment = round(usage - investigations.PLANNER_USD, 6)
    if adjustment:
        correction_id = f"{planner_run_id}_correction"
        correction = planner_row(investigation_id, question, {"run_id": correction_id, "model_usd": adjustment},
                                 "ok", started, started_at, run_id=correction_id, run_date=run_date,
                                 stage="investigation_spend")
        if not save_planner_row(correction):
            return None
    return usage


def investigation_body(body):
    question, angles = body.get("question"), body.get("angles")
    if not isinstance(question, str) or not 3 <= len(question.strip()) <= 2000:
        return "question must be 3 to 2,000 characters."
    if body.get("market") not in ("ZA", "NG", "KE", None):
        return "market must be ZA, NG, KE or null."
    if angles is not None and not (isinstance(angles, list) and len(angles) <= investigations.MAX_SUB_QUESTIONS
                                   and all(investigations.angle_ok(a) for a in angles)):
        return (f"angles must be a list of up to {investigations.MAX_SUB_QUESTIONS} phrases, each "
                f"{investigations.ANGLE_MIN} to {investigations.ANGLE_CHARS} characters.")
    skin_id = body.get("skin_id")
    if skin_id is not None and not (isinstance(skin_id, str) and skins.ID_RE.match(skin_id)):
        return "skin_id must be a skin id or left out."
    return None


def draft_investigation(body):
    tools = resolve_investigator()
    if tools is None:
        return need_t3()
    plan_investigation = tools[0]
    skin = None
    if body.get("skin_id") is not None:
        held = current_skins(body["skin_id"])
        if not held:
            return no_skin(body["skin_id"])
        skin = held[0]
    investigation_id = f"i_{secrets.token_hex(6)}"
    held = "plan:" + investigation_id
    planner_run_id = f"r_plan_{investigation_id[2:]}"
    with _investigation_lock:
        booking_day = now().date().isoformat()
        spent_before, left, pending = money_snapshot(booking_day)
        if pending:
            return not_saved("planner spend")
        if left is None:
            return budget_unknown("plan")
        if now().date().isoformat() != booking_day:
            return planner_budget_day_changed()
        reserved_before = investigations.RESERVED.total()
        request_cap = round(left["model_usd"] + spent_before["model_usd"] + reserved_before["model_usd"], 4)
        if left["model_usd"] < investigations.PLANNER_USD:
            return error(429, "rate_limited", investigations.MODEL_SPENT)
        investigations.RESERVED.add(held, {"credits": 0, "model_usd": investigations.PLANNER_USD})
        try:
            ceiling = planner_row(investigation_id, body["question"].strip(),
                                  {"run_id": planner_run_id, "model_usd": investigations.PLANNER_USD},
                                  "running", time.monotonic(), now().isoformat(), run_id=planner_run_id,
                                  run_date=booking_day)
            booked = save_planner_row(ceiling)
        except Exception as booking_error:
            log.error("investigation planner ceiling booking failed (exception_type=%s)",
                      type(booking_error).__name__)
            booked = False
        if not booked:
            investigations.RESERVED.release(held)
            return not_saved("planner spend")
        try:
            spend_after = planner_spend_snapshot(planner_run_id, booking_day)
        except Exception as booking_error:
            log.error("investigation planner ceiling readback failed (exception_type=%s)",
                      type(booking_error).__name__)
            spend_after = None
        if not planner_booking_confirmed(spend_after, investigations.PLANNER_USD):
            _PLANNER_READBACK_HOLDS[held] = {"run_id": planner_run_id, "run_date": booking_day,
                                             "usd": investigations.PLANNER_USD}
            log.error("investigation planner ceiling is not visible in the runs ledger; planner not called")
            return not_saved("planner spend")
        if now().date().isoformat() != booking_day:
            investigations.RESERVED.release(held)
            return planner_budget_day_changed()
        fresh_left = round(request_cap - (spend_after["model_usd"] - investigations.PLANNER_USD) -
                           reserved_before["model_usd"], 4)
        if fresh_left < investigations.PLANNER_USD:
            investigations.RESERVED.release(held)
            return error(429, "rate_limited", investigations.MODEL_SPENT)
        left["model_usd"] = fresh_left
        investigations.RESERVED.release(held)
    question, market = body["question"].strip(), body.get("market")
    request = {"investigation_id": investigation_id, "question": question, "market": market,
               "angles": body.get("angles"), "budget_left": dict(left), "max_model_usd": investigations.PLANNER_USD}
    if skin is not None:
        request["skin"] = skin  # the whole lens for a weekly report (contract 15.1); angles only summarise it
    started, started_at = time.monotonic(), now().isoformat()
    try:
        if now().date().isoformat() != booking_day:
            return planner_budget_day_changed()
        result = plan_investigation(copy.deepcopy(request))
    except Exception as exc:
        log.exception("investigation %s: the planner failed", investigation_id)
        try:
            booked = settle_planner_booking(investigation_id, question, failed_run(_run_of(exc)), "failed", started,
                                            started_at, planner_run_id, booking_day)
        except Exception as booking_error:
            log.error("investigation planner failure spend booking failed (exception_type=%s)",
                      type(booking_error).__name__)
            return not_saved("planner spend")
        if booked is None:
            return not_saved("planner spend")
        return error(502, "agent_unavailable", "The planner hit an error and drafted no plan; try again.")
    try:
        result = result if isinstance(result, dict) else {}
        run = failed_run(result.get("run")) or {}
        booked = settle_planner_booking(investigation_id, question, run, "ok", started, started_at,
                                        planner_run_id, booking_day)
    except Exception as booking_error:
        log.error("investigation planner spend booking failed (exception_type=%s)", type(booking_error).__name__)
        return not_saved("planner spend")
    if booked is None:
        return not_saved("planner spend")
    plan, why = investigations.validate_plan(result.get("plan"))
    estimate = investigations.validate_estimate(result.get("estimate"))
    if why or estimate is None:
        log.error("investigation %s: the planner returned an unreadable plan or estimate (%s)", investigation_id, why)
        return error(502, "agent_unavailable", "The planner returned a plan 42 cannot read; try again.")
    spent = booked
    left = {"credits": left["credits"], "model_usd": max(0.0, round(left["model_usd"] - spent, 4))}
    plan = investigations.clamp(plan, left)
    if skin is not None:
        plan["skin_id"] = skin["skin_id"]
    row = investigations.storage_row(investigation_id, 1, status_time(market), "draft", question, market, plan,
                                     estimate, None, planner_run_id)
    if not append("investigations", INVESTIGATIONS, row):
        return not_saved("investigation")
    return JSONResponse(investigation_out(row, row["created_at"], budget_left=left), status_code=201)


@app.post("/api/investigations")
async def create_investigation(request: Request):
    body, problem = await body_of(request)
    if problem:
        return problem
    why = investigation_body(body)
    if why:
        return error(400, "bad_request", why)
    return await run_in_threadpool(draft_investigation, body)


def not_a_draft(current):
    return error(409, "not_ready", f"Investigation {current['investigation_id']} is {current['status']}; "
                                   "only a draft can be edited or started.")


def edit_investigation(investigation_id, raw_plan):
    tools = resolve_investigator()
    if tools is None:
        return need_t3()
    plan, why = investigations.validate_plan(raw_plan)
    if why:
        return error(400, "bad_request", why)
    with _investigation_lock:
        rows = investigation_rows(investigation_id)
        if not rows:
            return no_investigation(investigation_id)
        current = rows[-1]
        if current["status"] != "draft":
            return not_a_draft(current)
        left = money_left()
        if left is None:
            return budget_unknown("re-plan")
        plan = investigations.clamp(plan, left)
        try:
            estimate = investigations.validate_estimate(tools[1](copy.deepcopy(plan)))
        except Exception:
            log.exception("investigation %s: estimate_plan failed", investigation_id)
            estimate = None
        if estimate is None:
            return error(502, "agent_unavailable", "The plan could not be estimated; try again.")
        skin_id = plan_skin_id(investigations.view(current)["plan"])
        if skin_id:
            plan["skin_id"] = skin_id  # kept from the draft; a client cannot set or change it
        row = investigations.storage_row(investigation_id, current["version"] + 1, status_time(current["market"]),
                                         "draft", current["question"], current["market"], plan, estimate, None,
                                         current["run_id"])
        if not append("investigations", INVESTIGATIONS, row):
            return not_saved("investigation")
    return JSONResponse(investigation_out(row, rows[0]["created_at"], budget_left=left))


@app.put("/api/investigations/{investigation_id}/plan")
async def replan_investigation(investigation_id: str, request: Request):
    body, problem = await body_of(request)
    if problem:
        return problem
    return await run_in_threadpool(edit_investigation, investigation_id, body.get("plan"))


def finish_investigation(investigation_id, ask):
    """Runs after the ask's runs row is written: writes the finish row, then ends the reservation whatever happened."""
    try:
        record = ask.snapshot()
        with _investigation_lock:
            rows = investigation_rows(investigation_id)
            current = rows[-1]
            if current["status"] != "running" or current["ask_id"] != record["ask_id"]:
                # The run was closed while this owner still worked (reconcile_investigation). A late owner does not
                # reopen it: the finish row is not published. The runs row written before this keeps the cost.
                log.warning("investigation %s: its owner finished after the run was closed as %s; no finish row",
                            investigation_id, current["status"])
                return
            row = investigations.storage_row(
                investigation_id, current["version"] + 1, status_time(current["market"]), record["status"],
                current["question"], current["market"], investigations.view(current)["plan"],
                investigations.view(current)["estimate"], record["ask_id"],
                (record["run"] or {}).get("run_id") or current["run_id"])
            if not append("investigations", INVESTIGATIONS, row):
                log.error("investigation %s: its finish row was not written", investigation_id)
    except Exception:
        log.exception("investigation %s: writing its finish row failed", investigation_id)
    finally:
        investigations.RESERVED.release(investigation_id)
        with _lock:
            RUNNING_INVESTIGATIONS.pop(investigation_id, None)


@app.post("/api/investigations/{investigation_id}/start")
def start_investigation(investigation_id: str):
    tools = resolve_investigator()
    if tools is None:
        return need_t3()
    with _investigation_lock:
        rows = investigation_rows(investigation_id)
        if not rows:
            return no_investigation(investigation_id)
        current = investigations.view(rows[-1])
        if current["status"] != "draft":
            return not_a_draft(current)
        left = money_left()
        if left is None:
            return budget_unknown("start")
        words = investigations.refusal(current["estimate"], left)
        if words:
            return error(409, "not_ready", words)
        # run_ask takes only the plan's own keys (core/agent/investigate.py validate_plan): the skin_id a report
        # plan carries stays in the stored rows and goes on the Ask record, and older platform names become groups.
        run_plan, why = investigations.validate_plan(current["plan"])
        if why:
            return error(409, "not_ready", f"This plan cannot run as stored: {why} Edit the plan, then start it.")
        granted = investigations.ceilings(current["plan"], left)
        market = current["market"]
        req = {"ask_id": f"a_{now(market).strftime('%Y%m%d')}_{secrets.token_hex(4)}",
               "question": current["question"], "market": market, "parent_id": None, "tier": "T3", "mode": "live",
               "wait": False, "from_card": None, "investigation_id": investigation_id, "plan": run_plan,
               **granted}
        ask = Ask(req)
        ask.record["investigation_id"] = investigation_id
        if plan_skin_id(current["plan"]):
            ask.record["skin_id"] = plan_skin_id(current["plan"])
        with _lock:
            prune()
            busy = sum(1 for a in ASKS.values() if a.ended is None) >= MAX_RUNNING
            if not busy:
                ASKS[req["ask_id"]] = ask
        if busy:
            return error(429, "rate_limited", f"{MAX_RUNNING} asks are already running; try again when one finishes.")
        row = investigations.storage_row(investigation_id, current["version"] + 1, status_time(market), "running",
                                         current["question"], market, current["plan"], current["estimate"],
                                         req["ask_id"], current["run_id"])
        if not append("investigations", INVESTIGATIONS, row):
            with _lock:
                ASKS.pop(req["ask_id"], None)
            return not_saved("investigation")
        investigations.RESERVED.add(investigation_id, {"credits": granted["max_credits"],
                                                       "model_usd": granted["max_model_usd"]})
        with _lock:
            RUNNING_INVESTIGATIONS[investigation_id] = ask
    threading.Thread(target=execute, args=(ask, tools[2], lambda a: finish_investigation(investigation_id, a)),
                     daemon=True, name=req["ask_id"]).start()
    url = f"/api/investigations/{investigation_id}"
    return JSONResponse({"investigation_id": investigation_id, "status": "running", "ask_id": req["ask_id"],
                         "ceilings": granted, "events_url": url + "/events", "url": url}, status_code=202)


def investigation_record(ask_id):
    """The investigation's Ask record: held here while fresh, else read back from the runs rows."""
    held = get_ask(ask_id)
    if held is not None:
        return held.snapshot()
    return stored_investigation_record(ask_id)


def stored_investigation_record(ask_id):
    """The durable Ask record, excluding results that exist only in worker memory."""
    if os.environ.get("F42_DATA") != "bigquery":
        with _lock:
            rows = list(SINK)
        for row in reversed(rows):
            record = _json(row.get("record")) if row.get("stage") == "ask" else None
            if record and record.get("ask_id") == ask_id:
                return viewer_record(record)
    from core.api.store import get_store
    return get_store().ask_record(ask_id)


# f42-agent runs as a single instance (max-instances 1), so a run no thread here holds and no stored record ends,
# once it is older than the longest it may take, belongs to a process that has died (L3).
ORPHAN_AFTER_SECONDS = 3600


def _utcnow():
    return datetime.now(timezone.utc)


def _run_is_lost(row):
    try:
        started = row["created_at"]
        started = datetime.fromisoformat(started) if isinstance(started, str) else started
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        minutes = investigations.view(row)["estimate"]["minutes"]
        return (_utcnow() - started).total_seconds() > ORPHAN_AFTER_SECONDS + minutes * 60
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def reconcile_investigation(investigation_id):
    """Repair a lost finish append from the matching finished Ask, without starting research."""
    with _investigation_lock:
        rows = investigation_rows(investigation_id)
        if not rows or rows[-1]["status"] != "running" or not rows[-1]["ask_id"]:
            return rows
        current = rows[-1]
        with _lock:
            active = RUNNING_INVESTIGATIONS.get(investigation_id)
        if active is not None and not active.finished.is_set():
            return rows
        try:
            record = stored_investigation_record(current["ask_id"])
            if (not record or record.get("ask_id") != current["ask_id"]
                    or record.get("investigation_id") != investigation_id
                    or record.get("status") not in ("complete", "stopped", "failed")
                    or not record.get("finished_at")):
                if not _run_is_lost(current):
                    return rows
                # The one process that could own the run is gone and no record of its end exists: close it.
                row = investigations.storage_row(
                    investigation_id, current["version"] + 1, status_time(current["market"]), "failed",
                    current["question"], current["market"], investigations.view(current)["plan"],
                    investigations.view(current)["estimate"], current["ask_id"], current["run_id"])
                # The hold stays: releasing it changes a hold, which waits for the shared reservation gate (N34).
                if append("investigations", INVESTIGATIONS, row):
                    return [*rows, row]
                log.error("investigation %s: its lost-run row was not written", investigation_id)
                return rows
            row = investigations.storage_row(
                investigation_id, current["version"] + 1, status_time(current["market"]), record["status"],
                current["question"], current["market"], investigations.view(current)["plan"],
                investigations.view(current)["estimate"], current["ask_id"],
                (record.get("run") or {}).get("run_id") or current["run_id"])
            if append("investigations", INVESTIGATIONS, row):
                return [*rows, row]
            log.error("investigation %s: its recovered finish row was not written", investigation_id)
        except Exception as exc:
            log.error("investigation %s: finish recovery failed (exception_type=%s)",
                      investigation_id, type(exc).__name__)
        return rows


def recover_investigations():
    """Reconcile stored running rows at startup. Missing local state alone never proves a dead owner."""
    try:
        if os.environ.get("F42_DATA") == "bigquery":
            rows = _stored("investigations",
                           f"SELECT investigation_id FROM (SELECT investigation_id, status, "
                           f"ROW_NUMBER() OVER (PARTITION BY investigation_id ORDER BY version DESC) AS n "
                           f"FROM `{project()}.intelligence_42_agent.investigations`) "
                           f"WHERE n = 1 AND status = 'running'", [])
        else:
            with _lock:
                latest = {}
                for row in INVESTIGATIONS:
                    key = row["investigation_id"]
                    if row["version"] >= latest.get(key, {"version": 0})["version"]:
                        latest[key] = row
                rows = [row for row in latest.values() if row["status"] == "running"]
        for row in rows:
            try:
                reconcile_investigation(row["investigation_id"])
            except Exception as exc:
                log.error("investigation %s: startup recovery failed (exception_type=%s)",
                          row["investigation_id"], type(exc).__name__)
    except Exception as exc:
        log.error("investigation startup scan failed (exception_type=%s)", type(exc).__name__)


def shown_investigations(rows):
    """The list as a reader may see it now: a question or plan can name a hidden person as the record can."""
    from core.api.store import get_store
    store = privacy.LazyStore(get_store)
    return view_response(privacy.mask({"investigations": rows}, privacy.read_hidden(store)))


@app.get("/api/investigations")
def list_investigations(status: str | None = None):
    if status is not None and status not in investigations.STATUSES:
        return error(400, "bad_request", "status must be one of " + ", ".join(investigations.STATUSES) + ".")
    recover_investigations()
    if os.environ.get("F42_DATA") == "bigquery":
        from google.cloud import bigquery
        rows = _stored("investigations",
                       f"SELECT * EXCEPT (n) FROM (SELECT i.investigation_id, i.version, i.created_at, i.who, "
                       f"i.status, i.question, i.market, i.plan, i.estimate, i.ask_id, i.run_id, "
                       f"MIN(i.created_at) OVER (PARTITION BY i.investigation_id) AS first_created_at, "
                       f"ROW_NUMBER() OVER (PARTITION BY i.investigation_id ORDER BY i.version DESC) AS n "
                       f"FROM `{project()}.intelligence_42_agent.investigations` i) "
                       f"WHERE n = 1 AND (@status IS NULL OR status = @status) "
                       f"ORDER BY first_created_at DESC LIMIT @limit",
                       [bigquery.ScalarQueryParameter("status", "STRING", status),
                        bigquery.ScalarQueryParameter("limit", "INT64", MAX_INVESTIGATION_LIST)])
        return shown_investigations([
            investigation_out({k: v for k, v in row.items() if k != "first_created_at"}, row["first_created_at"])
            for row in rows])
    with _lock:
        rows = list(INVESTIGATIONS)
    latest, first = {}, {}
    for row in rows:
        first.setdefault(row["investigation_id"], row["created_at"])
        if row["version"] >= latest.get(row["investigation_id"], {"version": 0})["version"]:
            latest[row["investigation_id"]] = row
    chosen = sorted((row for row in latest.values() if status in (None, row["status"])),
                    key=lambda row: datetime.fromisoformat(first[row["investigation_id"]]), reverse=True)
    return shown_investigations([investigation_out(row, first[row["investigation_id"]])
                                 for row in chosen[:MAX_INVESTIGATION_LIST]])


@app.get("/api/investigations/{investigation_id}")
def read_investigation(investigation_id: str):
    rows = reconcile_investigation(investigation_id)
    if not rows:
        return no_investigation(investigation_id)
    current = rows[-1]
    record = investigation_record(current["ask_id"]) if current["ask_id"] else None
    from core.api.store import get_store
    hidden = privacy.read_hidden(privacy.LazyStore(get_store))  # read once for the record and the rest of the page
    if record is not None:
        record = readable(record, hidden)  # a skin investigation is masked by the skin, then by the list
    extra = {"budget_left": money_left()} if current["status"] == "draft" else {}  # None when spend is unknown
    return view_response(privacy.mask(investigation_out(current, rows[0]["created_at"], record=record, **extra),
                                      hidden))


def replay(record, after, url):
    """A finished record no longer held here: its stored steps, then done."""
    steps = record.get("steps") or []
    for step in steps:
        if step["seq"] > after:
            yield frame("step", step)
    last = max([step["seq"] for step in steps] + [after])
    yield frame("done", {"seq": last + 1, "status": record["status"], "url": url})


@app.get("/api/investigations/{investigation_id}/events")
def investigation_events(investigation_id: str, request: Request):
    with _lock:
        ask = RUNNING_INVESTIGATIONS.get(investigation_id)
    current = None
    if ask is None:
        rows = investigation_rows(investigation_id)
        if not rows:
            return no_investigation(investigation_id)
        current = rows[-1]
        if not current["ask_id"]:
            return error(409, "not_ready", f"Investigation {investigation_id} has not started.")
        ask = get_ask(current["ask_id"])
    try:
        after = max(0, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        after = 0
    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if ask is not None:
        return StreamingResponse(follow(ask, after), media_type="text/event-stream", headers=headers)
    record = investigation_record(current["ask_id"])
    if record is None or record.get("status") == "running":
        return error(409, "not_ready", f"The run of investigation {investigation_id} is not held here and has no "
                                       "stored record yet.")
    return StreamingResponse(replay(readable(record), after, f"/api/investigations/{investigation_id}"),
                             media_type="text/event-stream", headers=headers)


@app.post("/api/investigations/{investigation_id}/stop")
def stop_investigation(investigation_id: str):
    with _lock:
        ask = RUNNING_INVESTIGATIONS.get(investigation_id)
    if ask is not None:
        ask.stop = True
        return JSONResponse({"investigation_id": investigation_id, "status": "stopping"}, status_code=202)
    rows = investigation_rows(investigation_id)
    if not rows:
        return no_investigation(investigation_id)
    if rows[-1]["status"] == "running":
        return error(409, "not_ready", f"Investigation {investigation_id} is not running on this server; it may "
                                       "have been lost in a restart, and it is closed once it is past its longest time.")
    return error(409, "not_ready", f"Investigation {investigation_id} is {rows[-1]['status']}, not running.")


# Hiding a person from the app (contract section 16). The app only adds to L1's suppression list; there is no lift
# route, because the list also carries removal requests that must stay in force. The one write is the parameterised
# INSERT in insert_suppression; nothing else here writes that table.

CREATOR_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
HANDLE_RE = re.compile(r"(?:@|u/)?([\w.\-]{1,100})")
WEB_ADDRESS_RE = re.compile(r"(?i)^www\.|\.(?:com|net|org|io|ly|me|tv|app|co|za|ng|ke)$")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Only a real ISO date is set aside (year 19xx or 20xx, month 01 to 12, day 01 to 31, not glued to more digits), so
# a number written like a date, 0821-23-4567, is still counted as a phone number.
DATE_TIME_RE = re.compile(r"(?<![\d/.-])(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
                          r"(?:[T ]\d{1,2}:\d{2}(?::\d{2})?)?(?![\d/-])|\b\d{1,2}:\d{2}(?::\d{2})?\b")
DIGIT_RUN_RE = re.compile(r"\+?[\d(][\d\s()./-]*\d")
PHONE_DIGITS = 9  # the shortest national number in ZA, NG or KE written without its leading 0
SUPPRESSION_UNAVAILABLE = "Hiding people is not switched on yet."
SUPPRESSION_BUSY = "Hiding people is busy; try again in a minute."
QUOTA_REASONS = {"rateLimitExceeded", "quotaExceeded"}


def _holds_phone(text):
    """True when text holds a phone number: a run of digits, spaces, dots, slashes, dashes and brackets with nine
    or more digits, once dates and times are set aside."""
    text = DATE_TIME_RE.sub(" ", text)
    return any(sum(ch.isdigit() for ch in m.group()) >= PHONE_DIGITS for m in DIGIT_RUN_RE.finditer(text))


def _holds_contact(text):
    return bool(EMAIL_RE.search(text)) or _holds_phone(text)


def suppression_body(body):
    """(creator_id, platform, handle, reason, who, None), or None fields and the reason in plain words."""
    creator_id, platform, handle = body.get("creator_id"), body.get("platform"), body.get("handle")
    reason, who = body.get("reason"), body.get("who")
    refused = (None,) * 5
    by_id, by_handle = creator_id is not None, platform is not None or handle is not None
    if by_id == by_handle:
        return *refused, "Name the person by creator_id, or by platform and handle, not both."
    if by_id:
        if not isinstance(creator_id, str) or not CREATOR_ID_RE.fullmatch(creator_id):
            return *refused, "creator_id must be a creator id."
    else:
        platform = platform.strip().lower() if isinstance(platform, str) else None
        platform = "x" if platform == "twitter" else platform
        if platform not in investigations.PLATFORMS:
            return *refused, "platform must be one of " + ", ".join(investigations.PLATFORMS) + "."
        handle = handle.strip() if isinstance(handle, str) else ""
        bare = HANDLE_RE.fullmatch(handle)
        if not bare or WEB_ADDRESS_RE.search(bare.group(1)):
            return *refused, ("handle must be a bare handle: an optional @ or u/ and then the name, with no "
                              "spaces, slashes or web address.")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not 3 <= len(reason) <= 300:
        return *refused, "reason must be 3 to 300 characters."
    if _holds_contact(reason):
        return *refused, "reason must not hold an email address or a phone number."
    who = who.strip() if isinstance(who, str) else ""
    if not 2 <= len(who) <= 60:
        return *refused, "who must be your name, 2 to 60 characters."
    if _holds_contact(who):
        return *refused, "who must not hold an email address or a phone number."
    if by_id:
        return creator_id, None, None, reason, who, None
    return None, platform, handle, reason, who, None


def insert_suppression(row):
    """Get ready to append the row to L1's list with one parameterised DML statement, or to the in-memory list when
    F42_DATA is not bigquery, and return the one call that sends it. Nothing is written until that call runs."""
    if os.environ.get("F42_DATA") != "bigquery":
        def send():
            with _lock:
                SUPPRESSIONS.append(dict(row))
        return send
    from google.cloud import bigquery
    sql = (f"INSERT INTO `{project()}.intelligence_42_core.suppressions` "
           "(suppression_id, status_at, status, creator_id, platform, handle, reason, who) "
           "VALUES (@suppression_id, @status_at, @status, @creator_id, @platform, @handle, @reason, @who)")
    params = [bigquery.ScalarQueryParameter("status_at", "TIMESTAMP", datetime.fromisoformat(row["status_at"]))]
    params += [bigquery.ScalarQueryParameter(key, "STRING", row[key])
               for key in ("suppression_id", "status", "creator_id", "platform", "handle", "reason", "who")]
    config = bigquery.QueryJobConfig(query_parameters=params, maximum_bytes_billed=2 * 1024 ** 3)
    client = bigquery_client()
    return lambda: client.query(sql, job_config=config).result()


def _refusal(exc):
    """The plain words for an insert BigQuery refused outright, so nothing was written, or None for any other
    failure. Only a 403 that is about access reads as not switched on; a 403 for quota or rate limits is busy."""
    from google.api_core.exceptions import NotFound
    reasons = {e.get("reason") for e in getattr(exc, "errors", None) or [] if isinstance(e, dict)}
    if reasons & QUOTA_REASONS:
        return SUPPRESSION_BUSY
    if isinstance(exc, NotFound):
        return SUPPRESSION_UNAVAILABLE
    if getattr(exc, "code", None) == 403 and (not reasons or "accessDenied" in reasons):
        return SUPPRESSION_UNAVAILABLE
    return None


def save_suppression(creator_id, platform, handle, reason, who):
    from core.api import store
    try:
        held = store.get_store()
        if creator_id is not None:
            found = held.creators_by_id([creator_id]) or []
            if not found:
                return error(404, "not_found", "No creator with that id.")
        else:
            found = held.creators_by_handle([store.creator_key(platform, handle)]) or []
    except Exception:
        log.exception("suppression: the creators read failed; nothing was hidden")
        return error(500, "internal", "42 could not check who this is, so no one was hidden; try again.")
    row = {"suppression_id": f"sup_app_{secrets.token_hex(6)}", "status_at": status_time(None),
           "status": "suppressed", "creator_id": creator_id, "platform": platform, "handle": handle,
           "reason": reason, "who": who}
    try:
        send = insert_suppression(row)
    except Exception:
        log.exception("suppression: the insert could not be prepared; nothing was sent")
        return error(500, "internal", "The person could not be hidden; nothing was saved. Try again.")
    try:
        send()
    except Exception as exc:
        refused = _refusal(exc)
        if refused:
            log.error("suppression: the insert was refused (%s): %s", type(exc).__name__, refused)
            return error(503, "suppression_unavailable", refused)
        # Once the query is sent it may have committed, so the failure must not say nothing was saved.
        log.exception("suppression: the insert failed after it was sent")
        return error(500, "internal", "The hide may not have been saved; check the hidden list before trying again.")
    return JSONResponse({**row, "matched": len({c["creator_id"] for c in found})}, status_code=201)


@app.post("/api/suppressions")
async def create_suppression(request: Request):
    body, problem = await body_of(request)
    if problem:
        return problem
    *fields, why = suppression_body(body)
    if why:
        return error(400, "bad_request", why)
    return await run_in_threadpool(save_suppression, *fields)


@app.get("/health")
def health():
    run_ask = resolve_run_ask()
    agent = "missing" if run_ask is None else "fixture" if run_ask is fixture_agent else "core.agent"
    with _lock:
        running = sum(1 for a in ASKS.values() if a.ended is None)
    return {"ok": True, "service": "f42-agent", "time": now().isoformat(), "agent": agent, "running": running,
            "t2_ready": t2_ready()}
