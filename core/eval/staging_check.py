"""Albert's one-command staging check: BUILD.md 1.11 (five Ask questions), 1.8 (search in four languages) and 2.1
(an enrichment sample) for whichever MODEL_PROVIDER is set, appended as one dated section to
docs/full-42/progress/L3.md.

    py -3.13 -m core.eval.staging_check [--only ask|search|enrich] [--enrich-n 20] [--provider gemini]
        [--questions NOW-01,RISE-02]

Ask: the first five questions in core/eval/friday.yaml (NOW-01, RISE-02, WHY-03, SPR-03, CRE-01: five families,
ZA, NG and KE) go through run_ask at T1 with a SocialCrawl client that refuses every call, since live SocialCrawl
calls run only inside Cloud Run jobs (RULES.md rule 13). Ask answers from the warehouse alone, and each answer is
scored against the 1.11 bar: schema valid, at least 5 cited posts inside the window from 2 or more platforms, no
unknown evidence id, no age claim, every pinned number reproduced by re-running its recorded query. --questions
runs only the named ones of those five, and the estimate counts only them.

Search: when post_enrichment holds no embedding, run_embed embeds the last 7 days first and books its own spend.
Otherwise tvf_search_posts is created if missing. Then it runs once per phrase in PHRASES and the top 5 posts are
recorded for a person to judge. The TVF scores exact cosine and needs no vector index, so a deferred or failed index
does not stop the search.

Enrichment: run_enrich in sync mode on up to --enrich-n posts sighted in the last 7 days, today first, each spend
booked on today in SAST. When fewer new posts exist, stored enrichment rows from the same days fill the sample. The
sheet goes to docs/full-42/progress/l3_enrich_sample_<date>.csv with blank verdict columns; accuracy is hand-checked
and scored with core.understand.enrich.enrich_check_score.

Before anything runs, today's model spend plus the most this run can spend, excluding run_embed, which sizes itself
to the room left under the cap, must fit under MODEL_DAILY_USD, or nothing runs and the exit code is 2. run_ask
books no spend of its own, so each answer's model_usd is booked in runs (embed.book_spend) as soon as it returns.
run_ask attaches no run to an interrupt, so an interrupted question books the T1 hold before the interrupt goes on.
The header's booked total counts every booking, a failed part's included. Nothing is deployed.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from core.agent.context import Refused, result_hash
from core.agent.tools.dates import SAST

ROOT = Path(__file__).resolve().parents[2]
FRIDAY = Path(__file__).resolve().parent / "friday.yaml"
PROGRESS = ROOT / "docs" / "full-42" / "progress" / "L3.md"
SHEET_DIR = PROGRESS.parent
PARTS = ("ask", "search", "enrich")
ASK_QUESTIONS = 5
ASK_TIER = "T1"
DAYS = 7  # the enrichment sample and the embed backfill read the last 7 days, today included
SEARCH_DAYS = 30  # Ask's default window
TOP = 5
REFUSED = 2
REFUSAL = "live SocialCrawl calls run only in Cloud Run jobs"
# One phrase per language, each searched in the market it belongs to (English in every market). Chosen for a clear
# topic a reader can judge a hit against; the isiZulu, Pidgin and Sheng wording wants a native speaker's check.
PHRASES = [
    {"lang": "English", "market": "", "q": "load shedding", "topic": "power cuts"},
    {"lang": "isiZulu", "market": "ZA", "q": "ukudla kwaseSoweto", "topic": "food in Soweto"},
    {"lang": "Nigerian Pidgin", "market": "NG", "q": "fuel price don go up", "topic": "the fuel price has gone up"},
    {"lang": "Sheng", "market": "KE", "q": "form ni gani wikendi hii", "topic": "plans for this weekend"},
]
SEARCH_SQL = ("SELECT post_id, distance, platform, text "
              "FROM `ogilvy-trends-v2.intelligence_42_agent.tvf_search_posts`"
              "(@q, NULLIF(@market, ''), @since, @until, @k) ORDER BY distance, post_id")
SHEET_COLUMNS = ["post_id", "source", "market", "platform", "url", "text", "langs", "code_switched", "entities",
                 "entity_correct", "language_correct", "note"]
AGE = "age or generation term"


@dataclass
class Wiring:
    """Everything the check touches outside itself. execute(sql, params, max_bytes=None) runs one BigQuery job."""
    execute: Callable
    ask_deps: Callable[[], Any]  # () -> core.agent.ask.Deps
    run_ask: Callable
    run_embed: Callable
    run_enrich: Callable
    enrich_model: Any            # the model SyncRunner calls; None builds the provider's own
    spent_today: Callable[[], float]
    now: Callable[[], datetime]
    warehouse: Any = None        # re-runs pinned numbers; the Ask deps' warehouse when None


def real_wiring() -> Wiring:
    from core.agent import ask
    from core.understand import embed, enrich
    from core.understand.job import bigquery_execute

    execute = bigquery_execute()

    def now():
        return datetime.now(SAST)

    return Wiring(execute=execute, ask_deps=ask._default_deps, run_ask=ask.run_ask, run_embed=embed.run_embed,
                  run_enrich=enrich.run_enrich, enrich_model=None,
                  spent_today=lambda: embed.spend_today(execute, now())[0], now=now)


class RefusingClient:
    """The SocialCrawl client for a developer shell: every call is refused."""

    notices = ["Live SocialCrawl calls run only in Cloud Run jobs; this answer uses stored posts only"]

    def quote(self, route, params):
        raise Refused(REFUSAL)

    def call(self, route, params, **kwargs):
        raise Refused(REFUSAL)


def refusing_socialcrawl(mode, run_id=None):
    return RefusingClient()


def pick_questions(path: Path = FRIDAY, n: int = ASK_QUESTIONS) -> list[dict]:
    items = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [dict(item["vars"]) for item in items[:n]]


def choose_questions(ids: str | None) -> list[dict]:
    """The five check questions, or only those named in ids (comma-separated), in friday.yaml order. An id outside
    the five raises ValueError."""
    five = pick_questions()
    if ids is None:
        return five
    wanted = [i.strip() for i in ids.split(",")]
    known = {q["id"] for q in five}
    if not ids.strip() or any(i not in known for i in wanted):
        raise ValueError(f"--questions takes ids from {', '.join(q['id'] for q in five)}, got {ids!r}")
    return [q for q in five if q["id"] in wanted]


def estimate_usd(parts, enrich_n: int, questions: int = ASK_QUESTIONS) -> float:
    """The most this run can spend on models: each chosen ask's full hold at T1, the search phrases' query embeddings and
    enrich_n posts at the longest post's estimate. run_embed is left out: it sizes each window to the cap itself."""
    from core.agent.ask import MODEL, hold_usd
    from core.understand import enrich

    total = 0.0
    if "ask" in parts:
        total += questions * hold_usd(ASK_TIER, MODEL)
    if "search" in parts:
        total += search_usd()
    if "enrich" in parts:
        total += enrich_n * enrich.est_usd({"content": "x" * 4000}, batched=False)
    return round(total, 6)


def search_usd() -> float:
    from core.understand.embed import CHARS_PER_TOKEN, EMBED_USD_PER_MILLION_TOKENS

    return sum(len(p["q"]) / CHARS_PER_TOKEN for p in PHRASES) * EMBED_USD_PER_MILLION_TOKENS / 1_000_000


def _error(exc) -> str:
    return f"{type(exc).__name__}: {(str(exc).splitlines() or [''])[0][:300]}"


# Ask (BUILD.md 1.11)

def _day(value) -> date | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(SAST).date()


def score_answer(answer: dict, run: dict, ctx, warehouse) -> dict:
    """The 1.11 bars for one answer. ctx is the run's RunContext, which holds the query records numbers pin to."""
    from core.agent.answer import validate_answer
    from core.agent.checks import _number_problem, _text_breaches

    problems = validate_answer(answer)
    evidence = {r["id"]: r for r in answer.get("evidence") or [] if isinstance(r, dict) and isinstance(r.get("id"), str)}
    claims = [c for c in answer.get("claims") or [] if isinstance(c, dict)]
    cited = list(dict.fromkeys(e for c in claims for e in c.get("evidence_ids") or [] if isinstance(e, str)))
    unknown = [e for e in cited if e not in evidence]
    window = run.get("window") or {}
    try:
        since, until = date.fromisoformat(window["from"]), date.fromisoformat(window["to"])
        inside = [e for e in cited if e in evidence and since <= (_day(evidence[e].get("posted_at")) or date.min) <= until]
    except (KeyError, TypeError, ValueError):
        inside = []
    platforms = sorted({str(evidence[e]["platform"]) for e in inside if evidence[e].get("platform")})

    records = list(evidence.values())
    age = sum(AGE in _text_breaches(c.get("text"), [evidence[e] for e in c.get("evidence_ids") or [] if e in evidence])
              for c in claims)
    other = [answer.get("short_answer"), answer.get("context")]
    other += [item.get("text") for key in ("so_what", "watch_next") for item in answer.get(key) or []
              if isinstance(item, dict)]
    other += [gap.get("what") for gap in answer.get("gaps") or [] if isinstance(gap, dict)]
    age += sum(AGE in _text_breaches(text, records) for text in other if text)

    numbers = [n for c in claims for n in c.get("numbers") or [] if isinstance(n, dict)]
    reruns = {}
    reproduced = 0 if ctx is None else sum(_number_problem(n, ctx, warehouse, reruns) is None for n in numbers)
    bars = {
        "schema_valid": not problems, "problems": problems, "cited": len(cited), "cited_in_window": len(inside),
        "platforms": platforms, "unknown_ids": len(unknown), "age_claims": age, "numbers": len(numbers),
        "numbers_reproduced": reproduced, "model_usd": float(run.get("model_usd") or 0.0),
    }
    bars["pass"] = (bars["schema_valid"] and len(inside) >= 5 and len(platforms) >= 2 and not unknown and age == 0
                    and reproduced == len(numbers))
    return bars


def check_ask(wiring: Wiring, book, questions: list[dict] | None = None, *, record_dir: Path | None = None) -> dict:
    questions = pick_questions() if questions is None else questions
    base = wiring.ask_deps()
    warehouse = wiring.warehouse or base.warehouse
    ran = {q["id"] for q in questions}
    results = []
    for question in questions:
        captured = {}

        def research(ctx, *args, _inner=base.research, _captured=captured, **kwargs):
            _captured["ctx"] = ctx
            result = _inner(ctx, *args, **kwargs)
            if isinstance(result, dict) and result.get("note"):
                _captured.setdefault("notes", []).append(str(result["note"]))
            return result

        deps = dataclasses.replace(base, socialcrawl=refusing_socialcrawl, research=research)
        markets = question.get("markets") or []
        request = {"question": question["question"], "tier": ASK_TIER, "mode": "live",
                   "market": markets[0] if len(markets) == 1 else None}
        result = {"id": question["id"], "question": question["question"], "markets": markets, "error": None}
        try:
            out = wiring.run_ask(request, lambda event: None, lambda: False, deps=deps)
            answer, run = out["answer"], out["run"]
        except Exception as exc:
            answer, run, result["error"] = None, getattr(exc, "run", None) or {}, _error(exc)
        except BaseException as exc:
            # run_ask attaches the run only to an Exception, so an interrupt carries no spend: book the T1 hold.
            from core.agent.ask import MODEL, hold_usd

            carried = getattr(exc, "run", None)
            usd = float(carried.get("model_usd") or 0.0) if carried else hold_usd(ASK_TIER, MODEL)
            book(usd, f"staging_check_ask:{question['id']}")
            raise
        usd = float(run.get("model_usd") or 0.0)
        result["booked_usd"] = book(usd, f"staging_check_ask:{question['id']}")
        ctx = captured.get("ctx")
        result.update(answer=answer, run=run, model_usd=usd,
                      queries={q: r["result_hash"] for q, r in (ctx.queries.items() if ctx else ())})
        result["bars"] = score_answer(answer, run, ctx, warehouse) if answer else None
        if record_dir is not None and ctx is not None and answer is not None:
            try:
                from core.eval.record_export import record_from_ask, write_record

                failed_sources = [source for source in run.get("source_status") or []
                                  if source.get("status") != "ok"]
                record = record_from_ask(
                    ctx,
                    id=question["id"],
                    question=question["question"],
                    markets=markets,
                    run=run,
                    gaps=[],
                    failed_sources=failed_sources,
                    note=getattr(ctx, "writer_note", "\n\n".join(captured.get("notes") or [])),
                )
                result["record_file"] = write_record(record_dir, record).name
            except Exception as exc:
                result["record_error"] = type(exc).__name__
        results.append(result)
    return {"questions": results, "pass": all(r["bars"] and r["bars"]["pass"] for r in results),
            "booked_usd": sum(r["booked_usd"] for r in results),
            "not_run": [q["id"] for q in pick_questions() if q["id"] not in ran]}


# Search (BUILD.md 1.8)

def _rows(result) -> list[dict]:
    rows = result.get("rows") if isinstance(result, dict) else result
    return [dict(row) for row in rows or []]


def query_hash(sql: str, params: dict) -> str:
    body = json.dumps({"sql": sql, "params": params}, sort_keys=True, default=str, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def check_search(wiring: Wiring, book, today: date) -> dict:
    from core.agent.tools.sql_query import MAX_BYTES_BILLED
    from core.understand import embed

    execute = wiring.execute
    out = {"embed": None, "booked_usd": 0.0}
    stored = int((_rows(execute(embed.load("embedding_count"), {})) or [{}])[0].get("n") or 0)
    out["embeddings_before"] = stored
    if stored == 0:
        counts = wiring.run_embed(
            execute, run_date=today, days=DAYS, book=lambda usd, what: book(usd, what, credit=True),
            spent_today=lambda: embed.spend_today(execute, wiring.now()),
            uncorrected=lambda: embed.uncorrected_today(execute, today))
        booked = counts.pop("booked_usd", 0.0)
        booked += book(counts.get("model_usd", 0) - booked, "staging_check_embed", credit=True)
        out["embed"], out["booked_usd"] = counts, booked
    else:
        execute(embed.load("tvf_search_posts"), {})  # CREATE IF NOT EXISTS; run_embed makes it on the other path
    since, until = today - timedelta(days=SEARCH_DAYS - 1), today
    phrases = []
    for phrase in PHRASES:
        params = {"q": phrase["q"], "market": phrase["market"], "since": since, "until": until, "k": TOP}
        entry = {**phrase, "since": since, "until": until, "query_hash": query_hash(SEARCH_SQL, params)}
        try:
            rows = _rows(execute(SEARCH_SQL, params, max_bytes=MAX_BYTES_BILLED))[:TOP]
            entry.update(rows=rows, result_hash=result_hash(rows))
        except Exception as exc:
            entry.update(rows=[], error=_error(exc))
        phrases.append(entry)
    out["booked_usd"] += book(search_usd(), "staging_check_search")
    out["phrases"] = phrases
    return out


# Enrichment (BUILD.md 2.1)

class CapturingRunner:
    """The sync runner, keeping every post it was sent and the model's output for it."""

    batched = False

    def __init__(self, inner):
        self.inner = inner
        self.sent: dict[str, dict] = {}
        self.outputs: dict[str, Any] = {}

    def run(self, posts):
        result = self.inner.run(posts)
        for post in posts:
            self.sent[post["post_id"]] = post
        for output in result.outputs:
            self.outputs[output["post_id"]] = output.get("output")
        return result


def _sheet_path(sheet_dir: Path, now: datetime) -> Path:
    path = sheet_dir / f"l3_enrich_sample_{now:%Y-%m-%d}.csv"
    return sheet_dir / f"l3_enrich_sample_{now:%Y-%m-%d}_{now:%H%M%S}.csv" if path.exists() else path


def check_enrich(wiring: Wiring, book, today: date, n: int, run_id: str, sheet_dir: Path) -> dict:
    from core.understand import enrich

    execute = wiring.execute
    runner = CapturingRunner(enrich.SyncRunner(wiring.enrich_model))
    days, booked, error = [], 0.0, None
    for back in range(DAYS):
        left = n - len(runner.sent)
        if left <= 0:
            break
        day = today - timedelta(days=back)
        try:
            counts = wiring.run_enrich(execute, runner, run_date=day, run_id=run_id, limit=left, day=today)
        except Exception as exc:
            counts, error = dict(getattr(exc, "enrich_counts", {})), _error(exc)
        spent = counts.get("booked_usd", 0.0)
        spent += book(counts.get("model_usd", 0.0) - spent, "staging_check_enrich")
        booked += spent
        days.append({"day": day, **{k: v for k, v in counts.items() if k != "booked_usd"}})
        if error or counts.get("spend_unknown") or counts.get("cap_limited") or counts.get("model_unavailable"):
            break

    rows = []
    for post_id, post in runner.sent.items():
        output = runner.outputs.get(post_id)
        row = {"post_id": post_id, "source": "this run", "market": post.get("market"),
               "platform": post.get("platform"), "url": post.get("url") or "",
               "text": str(post.get("content") or "")[:200], "langs": "", "code_switched": "", "entities": "",
               "note": ""}
        if post_id not in runner.outputs:
            row["note"] = "not sent: the model was unavailable; nothing was stored"
        elif output is None:
            row["note"] = "the model call failed; nothing was stored"
        elif enrich.valid(output):
            stored = enrich.enrichment_row(post_id, output)
            row.update(langs="|".join(stored["langs"]), code_switched=stored["code_switched"],
                       entities="; ".join(stored["entities"]))
        else:
            row["note"] = "the model's output failed the schema; nothing was stored"
        rows.append(row)

    from_stored = 0
    if len(rows) < n and not error:
        pool, seen = [], set(runner.sent)
        for back in range(DAYS):
            for record in _rows(execute(enrich.load("enrich_check_rows"), {"day": today - timedelta(days=back)})):
                if record["post_id"] not in seen:
                    seen.add(record["post_id"])
                    pool.append(record)
        for record in enrich.enrich_check_sample(pool, today.isoformat(), n - len(rows)):
            rows.append({"post_id": record["post_id"], "source": "stored", "market": record.get("market"),
                         "platform": record.get("platform"), "url": record.get("url") or "",
                         "text": str(record.get("text") or "")[:200], "langs": "|".join(record.get("langs") or []),
                         "code_switched": record.get("code_switched"),
                         "entities": "; ".join(record.get("entities") or []), "note": ""})
            from_stored += 1

    path = _sheet_path(sheet_dir, wiring.now())
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SHEET_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "entity_correct": "", "language_correct": ""})
    return {"sheet": path, "rows": len(rows), "this_run": len(rows) - from_stored, "stored": from_stored,
            "days": days, "error": error, "booked_usd": booked}


# The progress section

def cell(value) -> str:
    return " ".join(str(value if value is not None else "").split()).replace("|", "\\|")


def _yes(ok: bool) -> str:
    return "pass" if ok else "fail"


def render_ask(ask: dict) -> list[str]:
    n = len(ask["questions"])
    if ask.get("not_run"):
        verdict = f"PARTIAL ({n} of {ASK_QUESTIONS} run), not a 1.11 pass"
        verdict += f" (the {n} that ran passed)" if ask["pass"] else ""
        counted = f"{n} of the five questions"
    else:
        verdict, counted = ("PASS" if ask["pass"] else "FAIL"), "Five questions"
    lines = [f"### Ask, BUILD.md 1.11: {verdict}", "",
             f"{counted} from core/eval/friday.yaml at {ASK_TIER}, SocialCrawl refused ({REFUSAL}).", ""]
    if ask.get("not_run"):
        lines += [f"Ran {', '.join(r['id'] for r in ask['questions'])} of the five questions; "
                  f"{', '.join(ask['not_run'])} were not run in this invocation.", ""]
    lines += ["| Question | Run id | Schema | Cited in window (of cited) | Platforms | Unknown ids | Age claims "
              "| Numbers reproduced | Model USD | Result |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in ask["questions"]:
        b = r["bars"]
        if b is None:
            lines.append(f"| {r['id']} | {cell(r['run'].get('run_id'))} | error | | | | | | {r['model_usd']:.4f} "
                         f"| FAIL: {cell(r['error'])} |")
            continue
        lines.append(
            f"| {r['id']} | {cell(r['run'].get('run_id'))} | {_yes(b['schema_valid'])} "
            f"| {b['cited_in_window']} of {b['cited']}, {_yes(b['cited_in_window'] >= 5)} "
            f"| {len(b['platforms'])} ({', '.join(b['platforms'])}), {_yes(len(b['platforms']) >= 2)} "
            f"| {b['unknown_ids']}, {_yes(b['unknown_ids'] == 0)} | {b['age_claims']}, {_yes(b['age_claims'] == 0)} "
            f"| {b['numbers_reproduced']} of {b['numbers']}, {_yes(b['numbers_reproduced'] == b['numbers'])} "
            f"| {b['model_usd']:.4f} | {'PASS' if b['pass'] else 'FAIL'} |")
    lines.append("")
    for r in ask["questions"]:
        lines += [f"<details><summary>{r['id']}: {cell(r['question'])}</summary>", ""]
        if r["error"]:
            lines += [f"Error: {cell(r['error'])}", ""]
        answer, run = r["answer"] or {}, r["run"]
        window = run.get("window") or {}
        lines += [f"Run {cell(run.get('run_id'))} at tier {cell(run.get('tier'))}, window {window.get('from')} to "
                  f"{window.get('to')}, status {cell(answer.get('status'))}, model USD {r['model_usd']:.4f}.", ""]
        if answer:
            lines += [f"Short answer: {cell(answer.get('short_answer'))}", ""]
            evidence = {e.get("id"): e for e in answer.get("evidence") or [] if isinstance(e, dict)}
            for claim in answer.get("claims") or []:
                posts = ", ".join(f"{cell(e)} ({cell((evidence.get(e) or {}).get('url') or 'no record')})"
                                  for e in claim.get("evidence_ids") or [])
                lines.append(f"- {cell(claim.get('id'))} ({cell(claim.get('label'))}): {cell(claim.get('text'))} "
                             f"Posts: {posts}")
            for gap in answer.get("gaps") or []:
                lines.append(f"- Gap: {cell(gap.get('what'))}; searched {cell(gap.get('searched'))}; "
                             f"why {cell(gap.get('why'))}")
        for notice in run.get("notices") or []:
            lines.append(f"- Notice: {cell(notice)}")
        if r.get("record_file"):
            lines.append(f"- Frozen research record: {cell(r['record_file'])}")
        if r.get("record_error"):
            lines.append(f"- Record export failed for {cell(r['id'])}: {cell(r['record_error'])}")
        for problem in (r["bars"] or {}).get("problems") or []:
            lines.append(f"- Contract problem: {cell(problem)}")
        if r["queries"]:
            lines.append("- Queries: " + ", ".join(f"{q} {h}" for q, h in r["queries"].items()))
        lines += ["", "</details>", ""]
    return lines


def render_search(search: dict) -> list[str]:
    lines = ["### Search, BUILD.md 1.8: hand check", "",
             f"Embedded posts before the check: {search['embeddings_before']}."]
    if search["embed"] is not None:
        lines.append(f"run_embed ran on the last {DAYS} days first: {cell(json.dumps(search['embed'], default=str))}.")
    lines += ["On topic is judged by a person from the text below.", ""]
    for p in search["phrases"]:
        lines += [f"{p['lang']}, \"{p['q']}\" ({p['topic']}), market {p['market'] or 'all'}, "
                  f"{p['since']} to {p['until']}, query {p['query_hash']}"
                  + (f", result {p['result_hash']}" if p.get("result_hash") else "") + ".", ""]
        if p.get("error"):
            lines += [f"Error: {cell(p['error'])}", ""]
            continue
        lines += ["| Rank | Post id | Distance | Platform | Text, first 80 characters | On topic |",
                  "|---|---|---|---|---|---|"]
        for i, row in enumerate(p["rows"], 1):
            distance = row.get("distance")
            shown = f"{float(distance):.4f}" if isinstance(distance, (int, float)) else cell(distance)
            lines.append(f"| {i} | {cell(row.get('post_id'))} | {shown} | {cell(row.get('platform'))} "
                         f"| {cell(str(row.get('text') or '')[:80])} | |")
        if not p["rows"]:
            lines.append("| | no rows | | | | |")
        lines.append("")
    return lines


def render_enrich(result: dict, n: int) -> list[str]:
    lines = ["### Enrichment sample, BUILD.md 2.1: hand check", "",
             f"{result['rows']} of {n} posts in {result['sheet'].name}: {result['this_run']} enriched by this run, "
             f"{result['stored']} from stored enrichment rows. Entity and language accuracy are hand-checked: mark "
             "entity_correct and language_correct, then score the sheet with "
             "core.understand.enrich.enrich_check_score.", ""]
    if result["error"]:
        lines += [f"Error: {cell(result['error'])}", ""]
    for day in result["days"]:
        lines.append(f"- run_enrich {day['day']}: " + cell(json.dumps({k: v for k, v in day.items() if k != 'day'},
                                                                      default=str)))
    return lines + [""]


def render(provider: str, now: datetime, results: dict, *, spent: float, estimate: float, cap: float,
           run_id: str, booked: float) -> str:
    from core.agent.ask import MODEL
    from core.understand.enrich import MODEL as FAST

    lines = [f"## Staging check ({provider}, {now:%Y-%m-%d %H:%M} SAST)", "",
             f"Provider {provider}: Ask on {MODEL}, enrichment on {FAST}. Check run {run_id}, run locally by "
             "core/eval/staging_check.py; nothing was deployed. Model spend today before the run USD "
             f"{spent:.4f}, most this run could spend USD {estimate:.4f} excluding run_embed, which sizes itself to the room "
             f"left under the cap, cap USD {cap:g}, booked by this run USD "
             f"{booked:.4f}.", ""]
    for part in PARTS:
        if part not in results:
            continue
        result = results[part]
        if isinstance(result, str):
            lines += [f"### {part}: FAIL", "", f"Error: {cell(result)}", ""]
        elif part == "ask":
            lines += render_ask(result)
        elif part == "search":
            lines += render_search(result)
        else:
            lines += render_enrich(result, results["_n"])
    return "\n".join(lines).rstrip() + "\n"


def append(path: Path, text: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    gap = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(gap + text)


def main(argv=None, wiring: Wiring | None = None) -> int:
    parser = argparse.ArgumentParser(prog="py -3.13 -m core.eval.staging_check",
                                     description="Run the 1.11 Ask, 1.8 search and 2.1 enrichment staging checks "
                                                 "and append the results to the L3 progress file.")
    parser.add_argument("--only", choices=PARTS, help="run one part")
    parser.add_argument("--enrich-n", type=int, default=20, help="posts in the enrichment sample (default 20; "
                                                                  "the full hand check is 200)")
    parser.add_argument("--provider", choices=("gemini",),
                        help="sets MODEL_PROVIDER for this run (gemini, the only provider and the default)")
    parser.add_argument("--questions", metavar="IDS",
                        help="comma-separated friday.yaml ids to ask, from NOW-01, RISE-02, WHY-03, SPR-03, CRE-01 "
                             "(default: all five); the estimate counts only these")
    parser.add_argument("--progress", type=Path, default=PROGRESS, help="the progress file to append to")
    parser.add_argument("--sheet-dir", type=Path, default=SHEET_DIR, help="where the enrichment sheet goes")
    parser.add_argument("--record-dir", type=Path,
                        help="write one local blind_test research record per completed Ask question")
    args = parser.parse_args(argv)
    if args.enrich_n < 1:
        parser.error("--enrich-n must be at least 1")
    try:
        questions = choose_questions(args.questions)
    except ValueError as exc:
        parser.error(str(exc))
    if args.provider:
        os.environ["MODEL_PROVIDER"] = args.provider

    from core.agent.ask import model_daily_usd
    from core.llm.provider import provider
    from core.understand.embed import book_spend

    chosen = provider()
    wiring = wiring or real_wiring()
    parts = [args.only] if args.only else list(PARTS)
    print(f"provider {chosen}, parts {', '.join(parts)}")

    estimate = estimate_usd(parts, args.enrich_n, len(questions))
    try:
        spent = float(wiring.spent_today())
    except Exception as exc:
        print(f"Refused: today's model spend could not be read ({type(exc).__name__}), so nothing ran")
        return REFUSED
    now = wiring.now().astimezone(SAST)
    today = now.date()
    model_cap = model_daily_usd(now=now)
    if spent + estimate > model_cap:
        print(f"Refused: spend today USD {spent:.4f} plus this run's most USD {estimate:.4f} would pass the "
              f"USD {model_cap:g} cap, so nothing ran. Try --only, fewer --questions, or a smaller --enrich-n.")
        return REFUSED

    run_id = f"staging-check-{now:%Y%m%d-%H%M%S}"

    booked = 0.0  # every booking this run made, including those of a part that later raised

    def book(usd, what, credit=False):
        nonlocal booked
        amount = book_spend(wiring.execute, run_id=run_id, run_date=today, usd=usd, what=what, credit=credit)
        booked = round(booked + amount, 6)
        return amount

    results: dict = {"_n": args.enrich_n}
    for part in parts:
        print(f"running {part}")
        try:
            if part == "ask":
                results[part] = check_ask(wiring, book, questions, record_dir=args.record_dir)
            elif part == "search":
                results[part] = check_search(wiring, book, today)
            else:
                results[part] = check_enrich(wiring, book, today, args.enrich_n, run_id, args.sheet_dir)
        except Exception as exc:
            results[part] = _error(exc)
    section = render(chosen, now, results, spent=spent, estimate=estimate, cap=model_cap, run_id=run_id,
                     booked=booked)
    append(args.progress, section)
    print(f"appended to {args.progress}; model USD booked by this run {booked:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
