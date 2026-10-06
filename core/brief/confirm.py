"""The confirm lane of the morning brief (BUILD.md 1.12, ENGINE.md section 3 "confirm and explain", SOURCES.md costed
rows 24 and 25): one cheap cross-platform search per top candidate, inside the confirm share.

    confirm(candidates, *, sc, market, d, max_credits=None, share_used=0, seen=None, client=None, ingest=None,
            clock=None, stop=None) -> {item_id: result}

candidates: dicts in rank order with item_id, kind, label, canonical_key, first_seen (a date or None) and
    seen_platforms (the platforms where 42 already has a sighting of the item).
sc: lane L1's SocialCrawlClient opened with share='confirm', or anything with the same call(), run_id and mode:
    call(route, params, *, market, item_id, lane) -> object with status, route, items, credits_charged, reason
    and body. A live search/multi body puts its rows in data.items, each naming its platform, and keeps only
    per-platform call metadata (credits, endpoint, rows, rows_kept, status) in data.sources.
share_used: confirm credits already spent today, for example by the markets confirmed before this one.
seen: the shared seen id, passed to tiktok/search/top when given so the vendor skips posts 42 already has.
client, ingest, clock: the BigQuery client, the ingest function from load_ingest() and the clock. When all
    are given, sc is not in replay mode and market is ZA, NG or KE, every result with status ok, empty or
    cached goes through lane L1's core.collect.parse.ingest into posts (MERGE on post_id) and
    post_observations, with the call's params, run_id=sc.run_id, fetched_at=clock() as the call returns and
    seed_key=the candidate's item_id. An ingest that raises is counted and the lane goes on.
stop: when given, asked before every call; once it returns true no further call starts, as at a halting status.

Ingested posts are not evidence in the run that ingests them: the brief job builds its evidence packs before
confirm and does not read them again, so these posts become citable from the next evidence read (the next brief,
or an answer). Within the run, confirm adds only presence lines to the pack facts.

Per candidate: one search/multi (query the label, else the canonical key; since the first sighting, else d-7;
platforms those of instagram, youtube, reddit, twitter, threads and facebook where the item has no sighting yet),
then for a hashtag or sound in ZA, NG or KE one tiktok/search/top with country=market and sort_by=date-posted.
Both calls carry lane='confirm'.

result: {"status": "done" | "stopped", "platforms_found": sorted platforms that returned at least one row,
    "credits": credits charged, "calls": [{"route", "status", "credits", "platforms", "reason"}]}, plus
    "ingest": {"posts", "observations", "errors"} when ingest runs, and "ingest_error" on a call whose ingest
    raised.
The lane stops before a call whose list price (one credit a platform page) would take share_used plus the
credits charged so far past max_credits (by default ENGINE_DAILY's confirm share in core/config/caps.yaml), and
at once on cap_reached, balance_floor or insufficient_credits or when stop says so; every candidate from there on is
"stopped".
forbidden, error and every other status are recorded and the lane goes on. The client still checks the share
cap, the monthly cap and the balance floor on every call.
"""

import sys
from datetime import UTC, datetime, timedelta
from functools import partial

from core.collect.socialcrawl_client import load_caps

MULTI, TIKTOK = "search/multi", "tiktok/search/top"
ROUTES = {MULTI, TIKTOK}
MULTI_PLATFORMS = ("instagram", "youtube", "reddit", "twitter", "threads", "facebook")
TIKTOK_KINDS = {"hashtag", "sound"}
TIKTOK_MARKETS = {"ZA", "NG", "KE"}
INGEST_MARKETS = {"ZA", "NG", "KE"}
INGEST_STATUSES = {"ok", "empty", "cached"}  # the client statuses that carry a vendor body; ingest checks too
HALT = {"cap_reached", "balance_floor", "insufficient_credits"}
SINCE_DAYS = 7


def load_ingest():
    """Lane L1's core.collect.parse.ingest with lane='confirm' and the confirm lane's item id and geo functions
    bound, or None while this branch does not have it yet (it reaches full-42 at the evening merge)."""
    try:
        from core.collect import parse
        from core.collect.job import safe_geo
        from core.detect import geo, items

        ingest = parse.ingest
    except (ImportError, AttributeError):
        return None
    except Exception as exc:  # a broken import after a merge must not lose the brief
        print(f"confirm ingest unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    return partial(ingest, lane="confirm",
                   item_id_fn=lambda k, r, p: items.item_id(k, items.canonical_key(k, r, p)),
                   geo_fn=safe_geo(geo.geo_for_post))


def _plan(cand, market, d, seen_id=None):
    query = cand.get("label") or cand.get("canonical_key")
    seen = set(cand.get("seen_platforms") or [])
    missing = [p for p in MULTI_PLATFORMS if p not in seen]
    since = cand.get("first_seen") or d - timedelta(days=SINCE_DAYS)
    calls = []
    if missing:
        calls.append((MULTI, {"query": query, "since": since.isoformat(), "platforms": ",".join(missing)},
                      len(missing)))
    if cand.get("kind") in TIKTOK_KINDS and market in TIKTOK_MARKETS:
        params = {"query": query, "country": market, "sort_by": "date-posted"}
        if seen_id:
            params["seen"] = seen_id
        calls.append((TIKTOK, params, 1))
    return calls


def _platforms(rows):
    rows = rows if isinstance(rows, list) else []
    return sorted({str(row["platform"]).lower() for row in rows if isinstance(row, dict) and row.get("platform")})


def _kept(sources):
    """The platforms whose call metadata in data.sources says it kept at least one row."""
    if not isinstance(sources, dict):
        return []
    return sorted(str(p).lower() for p, s in sources.items()
                  if isinstance(s, dict) and isinstance(s.get("rows_kept"), (int, float)) and s["rows_kept"] > 0)


def _found(route, result):
    """search/multi: the platform on each data.items row, else the sources whose rows_kept is above 0, else the
    platform on each of the result's items (a result without a body)."""
    if route == TIKTOK:
        return ["tiktok"] if result.items else []
    body = getattr(result, "body", None)
    data = body.get("data") if isinstance(body, dict) else None
    data = data if isinstance(data, dict) else {}
    return _platforms(data.get("items")) or _kept(data.get("sources")) or _platforms(result.items)


def _stopped():
    return {"status": "stopped", "platforms_found": [], "credits": 0.0, "calls": []}


def _ingest(ingest, client, result, market, *, params, run_id, fetched_at, seed_key, tally, call):
    """One result into posts and post_observations. An ingest that raises is counted, never raised."""
    try:
        wrote = ingest(client, result, market, params=params, run_id=run_id, fetched_at=fetched_at,
                       seed_key=seed_key)
        posts, observations = len(wrote["posts"]), len(wrote["observations"])
    except Exception as e:
        tally["errors"] += 1
        call["ingest_error"] = f"{type(e).__name__}: {e}"
        return
    tally["posts"] += posts
    tally["observations"] += observations


def confirm(candidates, *, sc, market, d, max_credits=None, share_used=0, seen=None, client=None, ingest=None,
            clock=None, stop=None):
    """See the module docstring."""
    if max_credits is None:
        max_credits = load_caps()["ENGINE_DAILY"]["confirm"]
    out, spent, halted = {}, float(share_used), False
    live = (ingest is not None and client is not None and getattr(sc, "mode", None) != "replay"
            and market in INGEST_MARKETS)
    clock = clock or (lambda: datetime.now(UTC))
    for cand in candidates:
        item_id = cand["item_id"]
        if halted:
            out[item_id] = _stopped()
            continue
        entry = {"status": "done", "platforms_found": [], "credits": 0.0, "calls": []}
        if live:
            entry["ingest"] = {"posts": 0, "observations": 0, "errors": 0}
        found = set()
        for route, params, price in _plan(cand, market, d, seen):
            if spent + price > max_credits or (stop is not None and stop()):
                halted = True
                break
            result = sc.call(route, params, market=market, item_id=item_id, lane="confirm")
            fetched_at = clock()
            credits = float(result.credits_charged or 0)
            spent += credits
            entry["credits"] += credits
            platforms = _found(route, result)
            found.update(platforms)
            call = {"route": route, "status": result.status, "credits": credits, "platforms": platforms,
                    "reason": result.reason or ""}
            entry["calls"].append(call)
            if live and result.status in INGEST_STATUSES:
                _ingest(ingest, client, result, market, params=params, run_id=sc.run_id, fetched_at=fetched_at,
                        seed_key=item_id, tally=entry["ingest"], call=call)
            if result.status in HALT:
                halted = True
                break
        if halted:
            entry["status"] = "stopped"
        entry["platforms_found"] = sorted(found)
        out[item_id] = entry
    return out
