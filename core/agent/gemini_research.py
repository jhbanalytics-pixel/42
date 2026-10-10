"""Ask's research loop on Gemini function calling (ask.default_research runs it).

The model sees only 42's fifteen tools (core/agent/toolset.py). The guards (toolset.guard) run here before every
call, the tier's turn and USD budgets stop the loop, and progress steps come from the same
ask.describe_tool lines. The model's own turn is appended to the history unchanged, so Gemini's thought signatures
go back with it.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from core.agent import ask, timings
from core.agent.context import Refused, RunContext
from core.agent.model_budget import BudgetRefused
from core.agent.toolset import DESCRIPTIONS, SCHEMAS, TOOL_NAMES, build_functions, fence_for_model, guard, run_plain
from core.llm.gemini import GeminiModel, usage_of

log = logging.getLogger(__name__)

# Set by tests to a fake client; None builds the real Vertex client on first use.
client_factory: Callable[[], object] | None = None

RETRY_WAIT_S = 2.0
RESEARCH_LEVEL = "medium"  # the loop plans across tools; the short structured calls stay on low
RESEARCH_MAX_OUTPUT_TOKENS = 8000
INPUT_FRAMING_TOKENS = 512
# Read-only warehouse tools: no credits, no writes outside the run's own records. Consecutive calls to these in one
# model turn run at the same time; every other tool runs alone, in order.
PARALLEL_TOOLS = ("sql_query", "search_posts", "rising_topics", "recall_findings", "history", "analogues",
                  "resolve_dates", "query_rows")
PARALLEL_WORKERS = 4
# Failed calls a read tool (PARALLEL_TOOLS) may make in one question before the loop stops running it. A live Ask on 4 October spent 39
# steps guessing table names and never wrote an answer; past this point the model is told to finish with what it has.
MAX_TOOL_FAILURES = 3


# Set by tests; the loop reads the clock only through this.
clock: Callable[[], float] = time.monotonic


# Seconds Ask waits before making again a call Vertex refused for capacity (a 429 RESOURCE_EXHAUSTED on its shared
# capacity, on and off on 6 Oct 2026, when Ask gave up on the first one). An Ask is read while it runs, so the waits are
# short and bounded: 35 s in all per call, then the refusal stands. A refusal bills nothing, so each try reserves afresh.
BUSY_WAITS_S = (5.0, 10.0, 20.0)


def server_error(exc: BaseException) -> bool:
    """A 5xx from Vertex: the request failed on Google's side and is worth one more try. Anything else is not."""
    code = getattr(exc, "code", None)
    return (type(code) is int and 500 <= code <= 599) or type(exc).__name__ == "ServerError"


def busy_refusal(exc: BaseException) -> bool:
    """A 429 from Vertex (google-genai's APIError code) with no usage on it: Google refused the call for capacity
    before running it, so it billed nothing and its reserve can be given back. A 429 only in the text, or an error that
    carries usage, is not one."""
    code = getattr(exc, "code", None)
    return (type(code) is int and code == 429 and getattr(exc, "usage", None) is None
            and not getattr(exc, "usd", None))


def wait_busy(attempt: int, should_stop: Callable[[], bool]) -> bool:
    """Wait out the attempt-th refusal in a row (BUSY_WAITS_S), a second at a time so a stop is not held up. False when
    the waits are used up or the ask is stopping: the refusal then stands."""
    if attempt >= len(BUSY_WAITS_S) or should_stop():
        return False
    left = BUSY_WAITS_S[attempt]
    while left > 0:
        step = min(1.0, left)
        time.sleep(step)
        left -= step
        if should_stop():
            return False
    return True


def closed_text(name: str) -> str:
    return (f"Refused: {name} has failed {MAX_TOOL_FAILURES} times in this question and is closed. Do not call it "
            f"again. Finish your note with the evidence you already have and name this as a gap.")


def declarations():
    from google.genai import types

    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(name=name, description=DESCRIPTIONS[name], parameters_json_schema=SCHEMAS[name])
        for name in TOOL_NAMES])]


def _config(system_prompt: str):
    from google.genai import types

    return GeminiModel().config(system=system_prompt, schema=None, max_tokens=RESEARCH_MAX_OUTPUT_TOKENS,
                                level=RESEARCH_LEVEL,
                                tools=declarations(),
                                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))


def _invalid_args(name: str, args: dict) -> str | None:
    """Check the arguments against the tool's schema (additionalProperties false) before the tool runs."""
    import jsonschema

    try:
        jsonschema.validate(args, SCHEMAS[name])
    except jsonschema.ValidationError as e:
        allowed = ", ".join(SCHEMAS[name].get("properties") or ()) or "none"
        return f"Invalid arguments: {e.message[:220]}. Allowed arguments: {allowed}."[:400]
    return None


def _generate(client, model, history, config, should_stop: Callable[[], bool] = lambda: False):
    """One call, made again after a short wait on a 429 (temporary contention on the global endpoint): first after
    RETRY_WAIT_S, then after each of BUSY_WAITS_S. A 5xx is retried by the HTTP client itself, which runs with one
    retry when no per-question budget guards the call."""
    attempt = 0
    while True:
        try:
            return client.models.generate_content(model=model, contents=history, config=config)
        except Exception as exc:
            if getattr(exc, "code", None) != 429 and "RESOURCE_EXHAUSTED" not in str(exc):
                raise
            if attempt == 0:
                time.sleep(RETRY_WAIT_S)
            elif not wait_busy(attempt - 1, should_stop):
                raise
            attempt += 1


def _json_default(value):
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True, mode="json")
        except TypeError:
            return dump(exclude_none=True)
    return str(value)


def _input_bound(model: str, history, config) -> int:
    payload = json.dumps({"model": model, "contents": history, "config": config},
                         default=_json_default, ensure_ascii=False, separators=(",", ":"))
    return len(payload.encode("utf-8")) + INPUT_FRAMING_TOKENS


def _budget_usage(response, model: str) -> dict | None:
    """Read raw required counts before usage_of turns absent fields into zeroes."""
    try:
        meta = getattr(response, "usage_metadata", None)
        if meta is None:
            return None
        prompt = getattr(meta, "prompt_token_count", None)
        response_tokens = getattr(meta, "response_token_count", None)
        candidate_tokens = getattr(meta, "candidates_token_count", None)
        thoughts = getattr(meta, "thoughts_token_count", None)
        tool_use = getattr(meta, "tool_use_prompt_token_count", None)
        cached = getattr(meta, "cached_content_token_count", None)
        total = getattr(meta, "total_token_count", None)
        if type(prompt) is not int or prompt <= 0:
            return None
        for value in (response_tokens, candidate_tokens):
            if value is not None and (type(value) is not int or value < 0):
                return None
        if response_tokens is None and candidate_tokens is None:
            return None
        if response_tokens is not None and candidate_tokens is not None and response_tokens != candidate_tokens:
            return None
        for value in (thoughts, tool_use, cached):
            if value is not None and (type(value) is not int or value < 0):
                return None
        if cached is not None and cached > prompt:
            return None
        output = candidate_tokens if candidate_tokens is not None else response_tokens
        if total is not None and (type(total) is not int or total < 0):
            return None
        if thoughts is None or tool_use is None:
            if type(total) is not int:
                return None
            known_total = prompt + output + (tool_use or 0) + (thoughts or 0)
            if total != known_total:
                return None
            thoughts = 0 if thoughts is None else thoughts
            tool_use = 0 if tool_use is None else tool_use
        elif total is not None and total != prompt + output + tool_use + thoughts:
            return None
        usage = usage_of(response, model)
    except Exception:
        return None
    if (type(usage.get("input_tokens")) is not int or usage["input_tokens"] <= 0
            or type(usage.get("output_tokens")) is not int or usage["output_tokens"] <= 0):
        return None
    return usage


def _check_finish(response, model: str, usage: dict) -> None:
    """A turn that ends for any reason but STOP (a cut-off, a block, a malformed call) is a failed research turn, never a
    quiet empty note. The spend goes on the exception so the ask still counts it."""
    candidate = (getattr(response, "candidates", None) or [None])[0]
    reason = getattr(candidate, "finish_reason", None)
    name = str(getattr(reason, "name", reason) or "")
    if name and name.upper() not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
        exc = RuntimeError(f"{model} ended a research turn with finish reason {name}")
        exc.usage, exc.usd = usage, usage["usd"]
        raise exc


def _parts(response):
    candidate = (getattr(response, "candidates", None) or [None])[0]
    content = getattr(candidate, "content", None)
    return content, list(getattr(content, "parts", None) or [])


def _run_call(ctx: RunContext, functions: dict, name: str, args: dict) -> tuple[str, bool]:
    """One tool call with its schema check and guard, as (text, is_error)."""
    if name not in functions:
        return f"{name} is not available. Only 42's own tools may be used: {', '.join(TOOL_NAMES)}.", True
    try:
        bad = _invalid_args(name, args)
        if bad:
            raise Refused(bad)
        guard(ctx, name, args)
        return run_plain(name, functions[name], args)
    except Refused as e:
        return f"Refused: {e}", True


def _tool_attrs(name: str, out) -> dict:
    """The tool span's closing attrs from the call's own result: its status and, for a count, the rows and bytes the
    result reports. Nothing the model or a post wrote is read."""
    if out is None:
        return {"status": "error"}
    text, is_error = out
    if is_error:
        return {"status": "refused" if str(text).startswith("Refused") else "error"}
    attrs = {"status": "ok"}
    if name == "sql_query":
        try:
            result = json.loads(text)
            attrs.update(rows=result.get("row_count"), bytes=result.get("bytes"))
        except Exception:
            pass
    return attrs


def _timed_call(ctx: RunContext, functions: dict, name: str, args: dict, batch: int | None = None,
                lane: int = 0) -> tuple[str, bool]:
    """_run_call under a tool_call span on ctx's recorder (the lane's own context shares its run's recorder)."""
    span = timings.of(ctx).begin("tool_call", name, tool=name, batch=batch, lane=lane)
    out = None
    try:
        out = _run_call(ctx, functions, name, args)
        return out
    finally:
        span.end(**_tool_attrs(name, out))


def _batches(calls, functions) -> list[list]:
    """The turn's calls in order, with each run of consecutive PARALLEL_TOOLS calls as one batch and every other call
    alone, so a live or paid call never runs beside another and its guard sees every earlier call's spend."""
    batches = []
    for call in calls:
        together = call.name in PARALLEL_TOOLS and call.name in functions
        if together and batches and batches[-1][0].name in PARALLEL_TOOLS and batches[-1][0].name in functions:
            batches[-1].append(call)
        else:
            batches.append([call])
    return batches


def _run_together(ctx: RunContext, options, named: list[tuple[str, dict]],
                  batch: int | None = None) -> list[tuple[tuple[str, bool], RunContext]]:
    """Run read-only warehouse calls at the same time, each on its own lane of ctx (RunContext.lane: shared query
    records under one lock, its own evidence, events and spend), so no two threads write the same field. The caller
    folds each lane back in call order."""
    jobs = []
    for name, args in named:
        lane = ctx.lane(dict(ctx.budget))
        jobs.append((lane, build_functions(lane, options.warehouse, options.client, options.tables), name, args))
    with ThreadPoolExecutor(max_workers=min(PARALLEL_WORKERS, len(jobs))) as pool:
        outs = list(pool.map(lambda item: _timed_call(item[1][0], item[1][1], item[1][2], item[1][3], batch, item[0]),
                             enumerate(jobs)))
    return [(out, job[0]) for out, job in zip(outs, jobs)]


def _fold(ctx: RunContext, lane: RunContext) -> None:
    """Add one finished read lane to ctx as if its call had run alone at this point: a post a later call stores again
    takes that call's record, as a second store does in a sequential turn."""
    ctx.evidence.update(lane.evidence)
    ctx.events += lane.events
    ctx.model_usd_extra += lane.model_usd_extra
    ctx.credits_spent += lane.credits_spent
    ctx.calls_made += lane.calls_made
    ctx.enrich_credits_spent += lane.enrich_credits_spent
    ctx.sc_calls += lane.sc_calls
    ctx.enriched |= lane.enriched
    ctx.transcripts.update(lane.transcripts)
    ctx.forecasts.update(lane.forecasts)


def gemini_research(ctx: RunContext, prompt: str, options, emit, should_stop: Callable[[], bool]) -> dict:
    """Ask's research contract: {"note", "tokens", "usd", "stopped"}."""
    from google.genai import types

    model_budget = getattr(ctx, "model_budget", None)
    client = client_factory() if client_factory else (
        GeminiModel(timeout_s=ask.ASK_GEMINI_TIMEOUT_S, retries=0 if model_budget is not None else 1).client)
    functions = build_functions(ctx, options.warehouse, options.client, options.tables)
    recorder = timings.of(ctx)
    config = _config(options.system_prompt)
    tier = ctx.budget  # the tier's limits, or a T2 researcher's own
    history = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
    result = {"note": "", "tokens": {"input": 0, "output": 0}, "usd": 0.0, "stopped": False}
    texts: list[str] = []
    failures: dict[str, int] = {}
    extra_at_start = ctx.model_usd_extra  # model spend inside tools counts against the tier's budget too
    for turn in range(tier["max_turns"]):
        if should_stop():
            result["stopped"] = True
            break
        turn_started = clock()
        reservation = None
        last_attempt_end = None
        if model_budget is not None:
            response, budget_note, failed = None, None, None
            busy = server_failed = 0
            while True:
                if should_stop():
                    result["stopped"] = True
                    budget_note = "Stopping as asked; keeping what was found so far"
                    break
                try:
                    reservation = model_budget.reserve(options.model, _input_bound(options.model, history, config),
                                                       RESEARCH_MAX_OUTPUT_TOKENS, research=True)
                    local_usd = ctx.research_usd + ctx.model_usd_extra - extra_at_start
                    if local_usd + reservation.ceiling_micros / 1_000_000 > tier["max_budget_usd"]:
                        model_budget.release(reservation)
                        budget_note = "Research stopped: this researcher's model budget is spent."
                        break
                except BudgetRefused as exc:
                    if str(exc) == "research_model_budget_exhausted":
                        budget_note = "Research stopped: the model budget for research is spent."
                        break
                    if str(exc) in ("question_model_budget_exhausted", "prior_call_usage_unknown"):
                        result["stopped"] = True
                        budget_note = "Research stopped: the model budget for this question is spent."
                        break
                    if failed is not None:  # the retry is refused for another reason: the earlier failure stands
                        raise failed from None
                    if model_budget.stopped:
                        # A tool (watch_video's model call, say) stopped the shared budget between turns, with any of
                        # its reasons: usage_unknown, bound_exceeded, ceiling_exceeded and the rest. The question's
                        # model calls are over, as at the writer gate, so research ends as a budget stop. No call is
                        # made, so this only spends less than the failed ask it replaces.
                        result["stopped"] = True
                        budget_note = "Research stopped: this question's model calls were stopped by its budget."
                        break
                    raise
                started_call = time.monotonic()
                span = recorder.begin("model_call", "research_turn", purpose="research_turn", model=options.model,
                                      attempt=busy + server_failed + 1,
                                      wait_s=0.0 if last_attempt_end is None else started_call - last_attempt_end)
                try:
                    response = client.models.generate_content(model=options.model, contents=history, config=config)
                    last_attempt_end = time.monotonic()
                    span.end(status="ok")
                    break
                except Exception as exc:
                    last_attempt_end = time.monotonic()
                    span.end(status=timings.failure_status(exc))
                    if busy_refusal(exc):
                        # Google refused the call for capacity before running it: nothing was billed, so the whole
                        # reserve goes back, and the call is made again on a fresh one after a short wait.
                        model_budget.release(reservation)
                        if not should_stop() and busy < len(BUSY_WAITS_S):
                            emit.step("note", f"The model is busy; trying again in {BUSY_WAITS_S[busy]:g} seconds")
                        if not wait_busy(busy, should_stop):
                            raise
                        busy += 1
                        failed = exc
                        continue
                    # A 5xx is Google's failure: its reserve is booked in full, as for any failed call, and one more
                    # try runs on a fresh reserve. Any other failure, or a second 5xx, stops the question as before.
                    retry = not server_failed and server_error(exc) and not should_stop()
                    ctx.research_usd += model_budget.fail(reservation, stop=not retry)
                    try:
                        exc.before_dispatch = False
                    except Exception:
                        wrapped = RuntimeError(
                            f"Gemini research dispatch failed after reservation ({type(exc).__name__})")
                        wrapped.before_dispatch = False
                        raise wrapped from exc
                    if not retry:
                        raise
                    server_failed += 1
                    failed = exc
                    time.sleep(RETRY_WAIT_S)
            if budget_note:
                texts.append(budget_note)
                break
            usage = _budget_usage(response, options.model)
            if usage is None:
                model_budget.settle(reservation, None)
                ctx.research_usd += reservation.ceiling_micros / 1_000_000
                result["stopped"] = True
                _, parts = _parts(response)
                texts += [p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)]
                texts.append("Research stopped: Gemini usage was unavailable, so the full per-call reserve was booked.")
                break
            span.set(**timings.token_attrs(usage))
            usage_safe = model_budget.settle(reservation, usage)
        else:
            span = recorder.begin("model_call", "research_turn", purpose="research_turn", model=options.model,
                                  attempt=1)
            try:
                response = _generate(client, options.model, history, config, should_stop)
            except Exception as exc:
                span.end(status=timings.failure_status(exc))
                raise
            span.end(status="ok")
            usage = usage_of(response, options.model)
            span.set(**timings.token_attrs(usage))
            usage_safe = True
        result["tokens"]["input"] += usage["input_tokens"]
        result["tokens"]["output"] += usage["output_tokens"]
        result["usd"] += usage["usd"]
        ctx.research_usd += usage["usd"]
        _check_finish(response, options.model, usage)
        content, parts = _parts(response)
        calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
        texts += [p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)]
        if not usage_safe:
            result["stopped"] = True
            texts.append("Research stopped: the model budget or per-call reservation was reached.")
            break
        if not calls:
            break
        history.append(content)
        model_seconds = clock() - turn_started
        answers = []
        for batch in _batches(calls, functions):
            if should_stop() or model_budget is not None and model_budget.stopped:
                result["stopped"] = True
                break
            every = [(call, call.name, dict(call.args or {})) for call in batch]
            shut = [failures.get(name, 0) >= MAX_TOOL_FAILURES for _, name, _ in every]
            named = [c for c, closed in zip(every, shut) if not closed]
            for _, name, args in named:
                kind, text, platform = ask.describe_tool(name, args, emit)
                emit.step(kind, text, platform=platform)
            batch_id = recorder.next_batch()
            if not named:
                results = []
            elif len(named) == 1:
                _, name, args = named[0]
                results = [(_timed_call(ctx, functions, name, args, batch_id), None)]
            else:
                results = _run_together(ctx, options, [(name, args) for _, name, args in named], batch_id)
            ran = iter(zip(named, results))
            for (call, name, args), closed in zip(every, shut):
                if closed:
                    answers.append(types.Part(function_response=types.FunctionResponse(
                        id=getattr(call, "id", None), name=name, response={"error": closed_text(name)})))
                    continue
                _, ((out, is_error), lane) = next(ran)
                if is_error and name in PARALLEL_TOOLS:  # read tools only: a write or live call is never closed
                    failures[name] = failures.get(name, 0) + 1
                    if failures[name] == MAX_TOOL_FAILURES:
                        emit.step("note", ask.closed_step(name))
                if lane is not None:
                    _fold(ctx, lane)
                refused_route = name == "socialcrawl_call" and ask._route_refused(args)
                if is_error and refused_route:
                    emit.step("note", "The live source call was refused and did not run")
                elif is_error:
                    emit.step("note", ask.failed_step(name, ask._first_text(out), ctx.run_id))
                ask._found(emit, ctx)
                answers.append(types.Part(function_response=types.FunctionResponse(
                    id=getattr(call, "id", None), name=name,
                    response={"error": out} if is_error else {"output": fence_for_model(name, out)})))
        history.append(types.Content(role="user", parts=answers))
        log.info("ask research turn: run %s, turn %d, model %.1f s, tools %.1f s, %d calls", ctx.run_id, turn + 1,
                 model_seconds, clock() - turn_started - model_seconds, len(calls))
        if should_stop():
            result["stopped"] = True
            emit.step("note", "Stopping as asked; keeping what was found so far")
            break
        if result["usd"] + ctx.model_usd_extra - extra_at_start >= tier["max_budget_usd"]:
            texts.append("Research stopped: the tier's model budget for this question is spent.")
            break
    result["note"] = "\n".join(texts)
    return result
