"""History (core/api/contract.md section 12.4): an item's waves and analogues, search, past asks, brief dates and
saved findings.

Brief dates list every market row of v_briefs_current whatever its status, a brief that held every item included,
each with the cards Today shows of it and the items it holds back, counted as Today counts them (today.brief_counts).

Waves come from v_item_waves and their daily posts from v_item_daily_current counted as that view counts them.
Analogues are the items whose stored cultural_map centroid is nearest (VECTOR_SEARCH, no embedding call) with a
completed wave in the market, aligned on days since the start of the wave: what happened to each from the item's
current day onward. Search matches labels and aliases in SQL. Nothing here calls a model.

Findings are not named pages: their Evidence keeps its flags, since coordination and paid signals are trust
information. Every body is read through the suppression list as Today reads it (today.without_hidden).
"""
import datetime as dt
import json

from core.api import fast
from core.api.discover import BadRequest, NotReady, _run, figure
from core.api.people import post_evidence
from core.api.today import MARKETS, SAST, NotFound, brief_counts, hidden_people, without_hidden

__all__ = ["BadRequest", "NotFound", "NotReady", "build_history_asks", "build_history_briefs",
           "build_history_findings", "build_history_item", "build_history_search"]

QUIET_DAYS = 28  # v_item_waves ends a wave after 28 quiet days, so a wave quiet that long is complete
ANALOGUES = 5
NEIGHBOURS = 25
SEARCH_LIMIT = 20
MAX_ASKS = 100
MAX_BRIEF_DATES = 366
NO_MAP = "Analogues need 42's cultural map"
NOT_CHECKED = "Whether a finding still holds is not checked yet"
SAVED_FINDING_SCHEMA = "saved-ask-finding-v1"
SAVED_FINDING_UNAVAILABLE = "This saved finding could not be verified."
SAVED_FINDING_FIELDS = {"schema_version", "answer_text", "source_ask_id", "source_run_id", "parent_id",
                        "answer_status", "context", "market", "window", "notices", "gaps", "source_status",
                        "evidence"}
SAVED_CLAIM_FIELDS = {"text", "label", "item_ids", "evidence_post_ids", "query_ids", "run_ids", "result_hashes"}
SAVED_CONTEXT_FIELDS = ("schema_version", "source_ask_id", "source_run_id", "parent_id", "answer_status",
                        "context", "market", "window", "notices", "gaps", "source_status")


def _day(value):
    return dt.date.fromisoformat(str(value)[:10])


def _market(market, optional=False):
    if (market is None and optional) or market in MARKETS:
        return market
    raise BadRequest("market must be ZA, NG or KE.")


def _days_in(days, wave):
    """{date: posts} for the wave's own days, or None when no daily rows cover it."""
    rows = {_day(r["metric_date"]): r["posts"] for r in days or [] if r["item_id"] == wave["item_id"]
            and _day(wave["wave_start"]) <= _day(r["metric_date"]) <= _day(wave["wave_end"])}
    return rows or None


def _wave(w, days, run, today):
    by_day = _days_in(days, w)
    above = None if by_day is None else sum(1 for n in by_day.values() if n >= w["peak_posts"] / 2)
    run_id = run["run_id"] if run else None
    return {"market": w["market"], "wave_start": str(w["wave_start"]), "wave_end": str(w["wave_end"]),
            "peak_date": str(w["peak_date"]),
            "peak_posts": figure(w["peak_posts"], "posts on the peak day", "q_item_waves", run_id, [w]),
            "above_half_days": figure(above, "days at or above half the peak", "q_item_days", run_id,
                                      [w, sorted((str(d), n) for d, n in (by_day or {}).items())]),
            "completed": None if today is None else (today - _day(w["wave_end"])).days > QUIET_DAYS}


def _span(waves):
    return min(str(w["wave_start"]) for w in waves), max(str(w["wave_end"]) for w in waves)


def _analogue(item, label, w, days, d, run_id):
    start, peak = _day(w["wave_start"]), _day(w["peak_date"])
    peak_day = (peak - start).days
    by_day = _days_in(days, w)
    fade_day = None
    if by_day is not None:
        day = peak + dt.timedelta(days=1)
        while by_day.get(day, 0) >= w["peak_posts"] / 2 and day <= _day(w["wave_end"]):
            day += dt.timedelta(days=1)
        fade_day = (day - start).days
    rows = [w, sorted((str(k), n) for k, n in (by_day or {}).items()), {"from_day": d}]
    return {"item_id": item["item_id"], "label": label, "kind": item.get("kind"),
            "wave": {"start": str(w["wave_start"]), "peak_date": str(w["peak_date"]), "end": str(w["wave_end"])},
            "from_day": d,
            "days_to_peak": figure(peak_day - d, f"days from day {d} to the peak (negative: it peaked earlier)",
                                   "q_analogues", run_id, rows),
            "peak_posts": figure(w["peak_posts"], "posts on the peak day", "q_analogues", run_id, rows),
            "days_to_fade": None if fade_day is None else figure(
                fade_day - d, f"days from day {d} to below half the peak (negative: it faded earlier)",
                "q_analogues", run_id, rows)}


def build_history_item(store, item_id, market):
    market = _market(market)
    run = _run(store)
    found = store.map_items([item_id])
    if not found:
        raise NotFound("That trend is not tracked by 42.")
    item = found[0]
    today = _day(run["run_date"])
    waves = store.waves([item_id], market)
    days = store.item_days([item_id], market, *_span(waves)) if waves else None
    bodies = None if waves is None else [
        _wave(w, days, run, today) for w in sorted(waves, key=lambda w: str(w["peak_date"]), reverse=True)]
    open_waves = [w for w in waves or [] if (today - _day(w["wave_end"])).days <= QUIET_DAYS]
    current = max((_day(w["wave_start"]) for w in open_waves), default=None)
    d = (today - current).days if current else 0

    analogues, note = None, NO_MAP
    near = store.nearest_items(item_id, NEIGHBOURS)
    if near is not None:
        note = None
        ids = [n["item_id"] for n in near]
        done = {}
        for w in store.waves(ids, market) or []:
            if (today - _day(w["wave_end"])).days > QUIET_DAYS and (
                    w["item_id"] not in done or str(w["wave_start"]) > str(done[w["item_id"]]["wave_start"])):
                done[w["item_id"]] = w
        chosen = [i for i in ids if i in done][:ANALOGUES]
        if chosen:  # their days and their map rows, read together
            fast.start(store, "item_days", chosen, market, *_span([done[i] for i in chosen]))
            fast.start(store, "map_items", chosen)
        their_days = store.item_days(chosen, market, *_span([done[i] for i in chosen])) if chosen else []
        meta = {r["item_id"]: r for r in store.map_items(chosen) or []} if chosen else {}
        analogues = [_analogue(meta.get(i, {"item_id": i}), (meta.get(i) or {}).get("label"), done[i], their_days,
                               d, run["run_id"]) for i in chosen]
    return without_hidden({
        "item_id": item_id, "market": market, "label": item.get("label") or item.get("canonical_key"),
        "kind": item.get("kind"), "as_of": run["run_date"], "current_day": d, "waves": bodies,
        "recurrences": None if waves is None else figure(
            max(len(waves) - 1, 0), "waves after the first", "q_item_waves", run["run_id"], waves),
        "analogues": analogues, "note": note}, hidden_people(store))


def build_history_search(store, q, market=None):
    q = (q or "").strip()
    if not 2 <= len(q) <= 100:
        raise BadRequest("q must be 2 to 100 characters.")
    market = _market(market, optional=True)
    items = store.search_items(q, SEARCH_LIMIT)
    if items is None:
        raise NotReady("42's map is not built yet")
    run = store.latest_detect_run()
    today = _day(run["run_date"]) if run else None
    waves = store.waves([i["item_id"] for i in items], market) if items else []
    wanted = {}
    for m in sorted({w["market"] for w in waves or []}):
        mine = [w for w in waves if w["market"] == m]
        wanted[m] = (sorted({w["item_id"] for w in mine}), m, *_span(mine))
        fast.start(store, "item_days", *wanted[m])  # every market's days read together
    days = {m: store.item_days(*args) for m, args in wanted.items()}
    return without_hidden({"q": q, "market": market, "items": [
        {"item_id": i["item_id"], "kind": i.get("kind"), "label": i.get("label") or i.get("canonical_key"),
         "aliases": list(i.get("aliases") or []),
         "waves": None if waves is None else [
             _wave(w, days.get(w["market"]), run, today)
             for w in sorted((w for w in waves if w["item_id"] == i["item_id"]),
                             key=lambda w: (str(w["peak_date"]), w["market"]), reverse=True)]}
        for i in items]}, hidden_people(store))


def build_history_asks(store, limit=20, before=None, market=None):
    """market (ZA, NG or KE) keeps the asks about that market, as the header's market picker asks; None keeps all."""
    market = _market(market, optional=True)
    if not 1 <= limit <= MAX_ASKS:
        raise BadRequest(f"limit must be 1 to {MAX_ASKS}.")
    if before is not None:
        try:
            at = dt.datetime.fromisoformat(before)
        except ValueError as exc:
            raise BadRequest("before must be an ISO date and time.") from exc
        before = (at if at.tzinfo else at.replace(tzinfo=SAST)).isoformat()
    rows = store.ask_history(limit, before, market) if market else store.ask_history(limit, before)
    asks = [{"ask_id": r["ask_id"], "question": r.get("question"), "at": str(r["asked_at"]), "status": r.get("status"),
             "answer_status": r.get("answer_status"), "market": r.get("market")} for r in rows]
    return {"asks": asks, "next_before": asks[-1]["at"] if len(asks) == limit else None}


def build_history_briefs(store, limit=30, before=None, market=None):
    """market keeps that market's row of each brief date; None keeps every market."""
    market = _market(market, optional=True)
    if not 1 <= limit <= MAX_BRIEF_DATES:
        raise BadRequest(f"limit must be 1 to {MAX_BRIEF_DATES}.")
    if before is not None:
        try:
            before = dt.date.fromisoformat(before).isoformat()
        except ValueError as exc:
            raise BadRequest("before must be YYYY-MM-DD.") from exc
    by_date = {}
    hidden = hidden_people(store)
    for r in store.brief_history(limit, before, market) if market else store.brief_history(limit, before):
        # Read as Today reads it: a card resting on a suppressed person's post is held there, so it is here too.
        payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        counted = brief_counts(without_hidden(payload, hidden))
        rows = [{"brief_date": str(r["brief_date"]), "market": r["market"], "run_id": r.get("run_id"), **counted}]
        by_date.setdefault(str(r["brief_date"]), []).append(
            {"market": r["market"], "status": r["status"],
             "published_at": None if r.get("published_at") is None else str(r["published_at"]),
             "cards": figure(len(counted["shown"]), "cards shown on Today", "q_history_briefs", r.get("run_id"),
                             rows),
             "held": figure(len(counted["held"]), "items held back", "q_history_briefs", r.get("run_id"), rows)})
    dates = [{"date": d, "markets": sorted(ms, key=lambda m: MARKETS.index(m["market"]) if m["market"] in MARKETS
                                           else len(MARKETS))}
             for d, ms in sorted(by_date.items(), reverse=True)]
    return {"dates": dates, "next_before": dates[-1]["date"] if len(dates) == limit else None}


def _unique_json_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _reject_json_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


def _saved_answer(value):
    if isinstance(value, dict):
        return True, value
    if not isinstance(value, str):
        return False, None
    try:
        decoded = json.loads(value, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant)
    except (TypeError, ValueError):
        return value.lstrip().startswith("{"), None
    return (True, decoded) if isinstance(decoded, dict) else (False, None)


def _valid_saved_envelope(envelope, claims):
    if not isinstance(envelope, dict) or set(envelope) != SAVED_FINDING_FIELDS:
        return False
    if envelope.get("schema_version") != SAVED_FINDING_SCHEMA:
        return False
    for key in ("answer_text", "source_ask_id", "source_run_id"):
        if not isinstance(envelope.get(key), str) or not envelope[key].strip():
            return False
    if envelope.get("parent_id") is not None and not isinstance(envelope["parent_id"], str):
        return False
    if not isinstance(envelope.get("answer_status"), str) or envelope["answer_status"] not in {"complete", "partial"}:
        return False
    if envelope.get("market") is not None and envelope["market"] not in MARKETS:
        return False
    window = envelope.get("window")
    if not isinstance(window, dict):
        return False
    dates = []
    for key in ("from", "to"):
        value = window.get(key)
        if not isinstance(value, str):
            return False
        try:
            parsed = dt.date.fromisoformat(value)
        except ValueError:
            return False
        if parsed.isoformat() != value:
            return False
        dates.append(parsed)
    if dates[0] > dates[1] or any(not isinstance(envelope.get(key), list)
                                  for key in ("notices", "gaps", "source_status", "evidence")):
        return False

    if not isinstance(claims, list):
        return False
    evidence_by_id = {}
    for evidence in envelope["evidence"]:
        if not isinstance(evidence, dict):
            return False
        evidence_id = evidence.get("id")
        flags = evidence.get("flags")
        source_market = evidence.get("source_market")
        if (any(not isinstance(evidence.get(key), str) or not evidence[key].strip()
                for key in ("platform", "handle", "url", "posted_at", "market", "text"))
                or evidence.get("market") not in MARKETS
                or (envelope["market"] is not None and evidence.get("market") != envelope["market"])
                or not isinstance(evidence_id, str) or not evidence_id.strip() or evidence_id in evidence_by_id
                or not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags)
                or (source_market is not None and source_market not in MARKETS)):
            return False
        evidence_by_id[evidence_id] = evidence

    if len(claims) < 2:
        return False
    cited_ids, claim_texts = set(), []
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != SAVED_CLAIM_FIELDS:
            return False
        text, label = claim.get("text"), claim.get("label")
        evidence_ids, run_ids = claim.get("evidence_post_ids"), claim.get("run_ids")
        if (not isinstance(text, str) or not text.strip() or not isinstance(label, str)
                or not isinstance(evidence_ids, list) or not evidence_ids
                or not isinstance(run_ids, list) or run_ids != [envelope["source_run_id"]]):
            return False
        if any(not isinstance(claim.get(key), list) or any(not isinstance(value, str)
                                                           for value in claim[key])
               for key in ("item_ids", "query_ids", "result_hashes")):
            return False
        if any(not isinstance(evidence_id, str) or not evidence_id.strip() or evidence_id not in evidence_by_id
               for evidence_id in evidence_ids) or len(evidence_ids) != len(set(evidence_ids)):
            return False
        cited_ids.update(evidence_ids)
        claim_texts.append(text)

    answer_text = "\n".join(claim_texts)
    if envelope["answer_status"] == "partial":
        answer_text = "Partial answer: " + answer_text
    return cited_ids == set(evidence_by_id) and envelope["answer_text"] == answer_text


def _finding_market(row):
    """The market a saved Finding was asked about, or None when its answer records none (older findings)."""
    _, decoded = _saved_answer(row.get("answer"))
    found = decoded.get("market") if isinstance(decoded, dict) else None
    return found if found in MARKETS else None


def build_history_findings(store, item_id=None, status=None, market=None):
    """market drops findings asked about another market; one with no recorded market stays in every market."""
    market = _market(market, optional=True)
    rows = store.findings(item_id)
    if rows is None:
        return {"findings": None, "note": NOT_CHECKED}
    if market:
        rows = [r for r in rows if _finding_market(r) in (None, market)]
    checked = any(r.get("status") for r in rows)
    if checked and status:
        rows = [r for r in rows if r.get("status") == status]
    saved = {}
    legacy = []
    for index, row in enumerate(rows):
        candidate, envelope = _saved_answer(row.get("answer"))
        if candidate:
            claims = row.get("claims") or []
            saved[index] = envelope if _valid_saved_envelope(envelope, claims) else None
        else:
            legacy.append(row)
    wanted = [pid for row in legacy for claim in row.get("claims") or []
              for pid in claim.get("evidence_post_ids") or []]
    posts = {p["post_id"]: p for p in store.posts_by_id(sorted(set(wanted))) or []} if wanted else {}
    out = []
    for index, r in enumerate(rows):
        finding = {"finding_id": r["finding_id"], "question": r.get("question"), "answer": r.get("answer"),
                   "as_of": None if r.get("as_of") is None else str(r["as_of"]), "status": r.get("status"),
                   "claims": r.get("claims") or [], "evidence": []}
        if index in saved:
            envelope = saved[index]
            if envelope is None:
                finding.update({"answer": None, "claims": [], "evidence": [],
                                "unavailable_reason": SAVED_FINDING_UNAVAILABLE})
            else:
                finding.update({"answer": envelope["answer_text"], "evidence": envelope["evidence"],
                                "valid_from": None if r.get("valid_from") is None else str(r["valid_from"]),
                                "valid_to": None if r.get("valid_to") is None else str(r["valid_to"]),
                                "saved_context": {key: envelope[key] for key in SAVED_CONTEXT_FIELDS}})
        else:
            ids = list(dict.fromkeys(pid for c in r.get("claims") or [] for pid in c.get("evidence_post_ids") or []))
            finding["evidence"] = [post_evidence(posts[pid], keep_flags=True) for pid in ids if pid in posts]
        out.append(finding)
    return without_hidden({"findings": out, "note": None if checked else NOT_CHECKED}, hidden_people(store))
