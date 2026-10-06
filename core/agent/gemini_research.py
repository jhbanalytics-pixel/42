"""Ask's research loop on Gemini function calling (ask.default_research runs it).

The model sees only 42's fourteen tools (core/agent/toolset.py). The guards (toolset.guard) run here before every
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

from core.agent import ask
from core.agent.context import Refused, RunContext
from core.agent.model_budget import BudgetRefused
from core.agent.toolset import DESCRIPTIONS, SCHEMAS, TOOL_NAMES, build_functions, guard, run_plain
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
                  "resolve_dates")
PARALLEL_WORKERS = 4
# Failed calls a read tool (PARALLEL_TOOLS) may make in one question before the loop stops running it. A live Ask on 4 October spent 39
# steps guessing table names and never wrote an answer; past this point the model is told to finish with what it has.
MAX_TOOL_FAILURES = 3


# Set by tests; the loop reads the clock only through this.
clock: Callable[[], float] = time.monotonic


def server_error(exc: BaseException) -> bool:
    """A 5xx from Vertex: the request failed on Google's side and is worth one more try. Anything else is not."""
    code = getattr(exc, "code", None)
    return (type(code) is int and 500 <= code <= 599) or type(exc).__name__ == "ServerError"


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
        return f"Invalid arguments: {e.message}"[:300]
    return None


def _generate(client, model, history, config):
    """One call, retried once after a short wait on a 429 (temporary contention on the global endpoint). A 5xx is
    retried by the HTTP client itself, which runs with one retry when no per-question budget guards the call."""
    try:
        return client.models.generate_content(model=model, contents=history, config=config)
    except Exception as exc:
        if getattr(exc, "code", None) != 429 and "RESOURCE_EXHAUSTED" not in str(exc):
            raise
        time.sleep(RETRY_WAIT_S)
        return client.models.generate_content(model=model, contents=history, config=config)


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


def _run_together(ctx: RunContext, options, named: list[tuple[str, dict]]) -> list[tuple[tuple[str, bool], RunContext]]:
    """Run read-only warehouse calls at the same time, each on its own lane of ctx (RunContext.lane: shared query
    records under one lock, its own evidence, events and spend), so no two threads write the same field. The caller
    folds each lane back in call order."""
    jobs = []
    for name, args in named:
        lane = ctx.lane(dict(ctx.budget))
        jobs.append((lane, build_functions(lane, options.warehouse, options.client, options.tables), name, args))
    with ThreadPoolExecutor(max_workers=min(PARALLEL_WORKERS, len(jobs))) as pool:
        outs = list(pool.map(lambda job: _run_call(job[0], job[1], job[2], job[3]), jobs))
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
    config = _config(options.system_prompt)
    tier = ctx.budget  # the tier's limits, or a T2 researcher's own
    history = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
    result = {"note": "", "tokens": {"input": 0, "output": 0}, "usd": 0.0, "stopped": False}
    texts: list[str] = []
    failures: dict[str, int] = {}
    extra_at_start = ctx.model_usd_extra  # model spend inside tools counts against the tier's budget too
    for turn in range(tier["max_turns"]):
        turn_started = clock()
        reservation = None
        if model_budget is not None:
            response, budget_note = None, None
            for attempt in (0, 1):
                try:
                    reservation = model_budget.reserve(options.model, _input_bound(options.model, history, config),
                                                       RESEARCH_MAX_OUTPUT_TOKENS, research=True)
                except BudgetRefused as exc:
                    if str(exc) == "research_model_budget_exhausted":
                        budget_note = "Research stopped: the model budget for research is spent."
                        break
                    if str(exc) in ("question_model_budget_exhausted", "prior_call_usage_unknown"):
                        result["stopped"] = True
                        budget_note = "Research stopped: the model budget for this question is spent."
                        break
                    if attempt:  # the retry is refused for another reason: the server error stands
                        raise failed from None
                    raise
                try:
                    response = client.models.generate_content(model=options.model, contents=history, config=config)
                    break
                except Exception as exc:
                    # A 5xx is Google's failure: its reserve is booked in full, as for any failed call, and one more
                    # try runs on a fresh reserve. Any other failure, or a second 5xx, stops the question as before.
                    retry = not attempt and server_error(exc) and not should_stop()
                    model_budget.fail(reservation, stop=not retry)
                    try:
                        exc.before_dispatch = False
                    except Exception:
                        wrapped = RuntimeError(
                            f"Gemini research dispatch failed after reservation ({type(exc).__name__})")
                        wrapped.before_dispatch = False
                        raise wrapped from exc
                    if not retry:
                        raise
                    failed = exc
                    time.sleep(RETRY_WAIT_S)
            if budget_note:
                texts.append(budget_note)
                break
            usage = _budget_usage(response, options.model)
            if usage is None:
                model_budget.settle(reservation, None)
                result["stopped"] = True
                _, parts = _parts(response)
                texts += [p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False)]
                texts.append("Research stopped: Gemini usage was unavailable, so the full per-call reserve was booked.")
                break
            usage_safe = model_budget.settle(reservation, usage)
        else:
            response = _generate(client, options.model, history, config)
            usage = usage_of(response, options.model)
            usage_safe = True
        result["tokens"]["input"] += usage["input_tokens"]
        result["tokens"]["output"] += usage["output_tokens"]
        result["usd"] += usage["usd"]
        ctx.research_usd = result["usd"]
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
            every = [(call, call.name, dict(call.args or {})) for call in batch]
            shut = [failures.get(name, 0) >= MAX_TOOL_FAILURES for _, name, _ in every]
            named = [c for c, closed in zip(every, shut) if not closed]
            for _, name, args in named:
                kind, text, platform = ask.describe_tool(name, args, emit)
                emit.step(kind, text, platform=platform)
            if not named:
                results = []
            elif len(named) == 1:
                _, name, args = named[0]
                results = [(_run_call(ctx, functions, name, args), None)]
            else:
                results = _run_together(ctx, options, [(name, args) for _, name, args in named])
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
                    response={"error": out} if is_error else {"output": out})))
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
