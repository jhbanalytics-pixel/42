"""Turn one probe run's stored responses into value-free samples and a probe report (BUILD.md task 0.5).

Runs locally as f42-builder after the f42-probe job. It reads that run's raw_responses and credit_ledger
rows with a SELECT parameterised by run_id. In memory it computes, per market, the share of
tiktok/trending feed=local videos whose ext.region is that market and the overlap between the NG and KE
feeds. It writes core/collect/samples/REPORT.md (route, status, charged, reported and rows per ledger
row, those two metrics, and any sample the gate withheld) and one core/collect/samples/<probe id>_<route
slug>.json per response.

A sample is the body's key-and-type skeleton and holds no value of any kind. Every scalar becomes its
type name ("<str>", "<int>", "<float>", "<bool>", "<null>"). A list becomes its length mark and the
skeleton of its first element. A dict keeps its keys only when every key is in VOCABULARY, the field
names of the Stage 0 probe responses; otherwise it collapses to one "<key>" entry holding the skeleton of
its first value. A residue gate then withholds any sample that still holds a number, an email, a url, a
domain, an @handle or a phone-like run of 9 or more digits.

    py -3.13 -m core.collect.probe_report probe-20260929T060000Z

--bodies reads a probe --bodies run the same way and writes a redacted fixture for parser tests,
core/collect/tests/fixtures/socialcrawl_bodies.json, one entry per route, merged into the file so a
rerun adds the routes it reached. A redacted body keeps every key that is a known field name or looks
like one (letters and underscores only); a dict with any other key collapses to one "<key>" entry. Every
value becomes its type name, a string with its form ("<str url>", "<str date>", "<str number>",
"<str time>", "<str empty>" or "<str>"), and a list keeps its length mark and at most LIST_CAP elements.
The same residue gate withholds a route whose entry still holds a value. The REPORT.md section it writes
lists the credits charged per route by group and every kept field name outside VOCABULARY, which is the
list to read for a handle before the fixture is committed.

    py -3.13 -m core.collect.probe_report --bodies probe-bodies-20260929T060000Z
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from core.collect.chain import PROJECT, SAST
from core.collect.probe import BODY_ROUTE_NAMES, GROUPS, PRICE_GROUPS, bodies_plan, plan
from core.collect.socialcrawl_client import split_vendor_labels
from core.collect.stores import DATASET

SAMPLES = Path(__file__).resolve().parent / "samples"
BODY_FIXTURE = Path(__file__).resolve().parent / "tests" / "fixtures" / "socialcrawl_bodies.json"
BODIES_HEADING = "## Comment, transcript and parser bodies"
LIST_CAP = 3
LOCAL_FLOOR = 0.4
TREND_PROBES = {"1-za": "ZA", "1-ng": "NG", "1-ke": "KE"}

# A sample is a value-free skeleton: every scalar becomes its type name, a list keeps its first element's
# skeleton and its length, and a dict keeps its keys only when every key is a known SocialCrawl field name.
# PROBE_FIELDS is the set of field names in the key-and-type dump of the Stage 0 probe responses
# (42_probe_shapes.txt, 28 September 2026). The dump covered data only, so ENVELOPE_FIELDS adds the
# top-level response envelope names seen in a live tiktok/trending body. Nothing else was added.
PROBE_FIELDS = (
    "address", "amount", "angle_id", "angle_label", "answer", "apple_music", "article_count", "articles",
    "artist_id", "artist_url", "audio_url", "author", "author_followers", "author_id", "avatar_url", "balance",
    "balance_after", "base_credits", "boards", "brand", "buyer", "cache_state", "cached", "cached_at",
    "canonical_url", "capabilities", "caption", "categoryId", "categoryTitle", "change_tracking", "channel_id",
    "chart", "chart_title", "city", "coauthors", "comments", "computed", "confidence", "content",
    "content_advisory", "content_category", "content_language", "cost", "count", "countries",
    "countries_searched", "country", "country_code", "country_not_included", "coverage", "created_at",
    "credit_tier", "credits", "credits_charged", "credits_refunded", "credits_used", "defaultAudioLanguage",
    "default_language", "deferred", "degraded", "deleted", "depth", "description", "discarded", "disclosed",
    "display_name", "domain", "download_count", "dropped", "dropped_ids", "duplicates_removed", "duration",
    "duration_seconds", "effective_query", "endpoint", "endpoints", "engagement", "engagement_rate", "engine",
    "error", "estimate", "estimated_reach", "ext", "external_source", "extra_credits", "extraction", "facebook",
    "facebook_places_id", "family", "fetch", "fetched_at", "filters", "final_url", "first_seen", "fits_offer",
    "flags", "free_values", "genres", "hasPaidProductPlacement", "has_more", "has_viewer_saved", "head_id",
    "highlights", "html", "http_status", "id", "image_url", "industry", "industry_id", "industry_label",
    "instagram", "intent", "items", "judged", "kept", "kind", "label", "label_credits_max", "label_credits_min",
    "labelled", "labels", "language", "language_code", "languages", "largest_story", "last_seen", "lat",
    "latency_ms", "leg_id", "legs", "legs_returned", "license", "likes", "likes_hidden", "lng", "location",
    "madeForKids", "markdown", "media", "media_type", "media_urls", "member_ids", "metered_values",
    "methodology_version", "mode", "multi_article_stories", "music_id", "name", "next_cursor", "niche",
    "not_found", "notes", "nsfw", "ok", "on_screen_texts", "origin", "outlet_count", "outlets", "p", "page",
    "page_count", "page_state", "pages", "param", "passes", "pending", "pending_ids", "period_days", "pinned",
    "pk", "placement", "plan", "platform", "platforms", "popularity_curve", "post", "posts", "presets",
    "proxy_tier", "published_at", "published_at_epoch", "published_at_precision", "quarantined_count", "query",
    "query_source", "rank", "ranking_query", "raw_html", "reaction_counts", "recent_deductions", "reddit",
    "refunded", "region", "release_date", "relevance", "repeats", "reposts", "request_id", "request_ids",
    "results", "rows", "rows_cached", "rows_expected", "rows_fetched", "rows_kept", "rows_returned", "saves",
    "scored", "scrape_id", "screenshot_url", "search_query", "search_status", "seller", "sense", "shares",
    "short_name", "shortcode", "since", "size", "skipped", "skipped_judge", "snippet", "source", "source_name",
    "sources", "spam", "spoiler", "sponsored", "status", "status_code", "stopped", "stories", "story_grouping",
    "subqueries", "subtitle", "summary", "tags", "taxonomy", "text", "threads", "threshold", "thumbnail_url",
    "title", "too_short", "top_creators", "topic", "topicCategories", "topic_source", "total", "total_articles",
    "total_page_count", "trend", "twitter", "type", "undisclosed", "unjudged", "unsupported", "updated_at",
    "urgency", "url", "username", "value", "vendor_labels", "verified", "video_count", "video_url", "views",
    "walk", "warnings", "weight", "what", "youtube",
)
ENVELOPE_FIELDS = (
    "cached", "credits_remaining", "credits_used", "data", "endpoint", "pagination", "has_more", "next_cursor",
    "page_size", "platform", "request_id", "success", "error", "message", "code",
)
VOCABULARY = PROBE_FIELDS + tuple(name for name in ENVELOPE_FIELDS if name not in PROBE_FIELDS)
TYPE_NAMES = {bool: "<bool>", int: "<int>", float: "<float>", str: "<str>", type(None): "<null>"}

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
SPELLED_EMAIL = re.compile(r"[\w.+-]+\s*[\[(]?\s*\bat\b\s*[\])]?\s*[\w-]+(\s*[\[(]?\s*\bdot\b\s*[\])]?\s*[a-z]{2,})+",
                           re.I)
URL = re.compile(r"(https?://|www\.)[^\s\"'<>]+", re.I)
DOMAIN = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b(?:/[^\s\"'<>]*)?", re.I)
DIGIT_RUN = re.compile(r"[+(]?\d[\d\s().\-/_,]*\d\)?")
LEFT_AT = re.compile(r"[@\uff20]\w")


def skeleton(value):
    """The key-and-type skeleton of value. No string, number, date or id from value survives."""
    if isinstance(value, dict):
        if all(key in VOCABULARY for key in value):
            return {key: skeleton(v) for key, v in value.items()}
        return {"<key>": skeleton(next(iter(value.values())))}
    if isinstance(value, list):
        return [f"<list of {len(value)}>"] + ([skeleton(value[0])] if value else [])
    return TYPE_NAMES.get(type(value), "<" + type(value).__name__ + ">")


FIELD = re.compile(r"[A-Za-z][A-Za-z_]{0,39}")
STRING_FORMS = (
    (re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?"), "<str date>"),
    (re.compile(r"[+-]?\d+(\.\d+)?"), "<str number>"),
    (re.compile(r"(\d+:)?\d{1,2}:\d{2}([.,]\d+)?"), "<str time>"),
)


def _string_form(text):
    text = text.strip()
    if not text:
        return "<str empty>"
    if URL.search(text) or DOMAIN.fullmatch(text):
        return "<str url>"
    return next((name for pattern, name in STRING_FORMS if pattern.fullmatch(text)), "<str>")


def redact(value):
    """A body with its field names and no value: see the module docstring."""
    if isinstance(value, dict):
        if all(isinstance(k, str) and (k in VOCABULARY or FIELD.fullmatch(k)) for k in value):
            return {k: redact(v) for k, v in value.items()}
        return {"<key>": redact(next(iter(value.values())))}
    if isinstance(value, list):
        return [f"<list of {len(value)}>"] + [redact(v) for v in value[:LIST_CAP]]
    if isinstance(value, str):
        return _string_form(value)
    return TYPE_NAMES.get(type(value), "<" + type(value).__name__ + ">")


def new_keys(node):
    """Every key in a redacted node that is not in VOCABULARY, sorted."""
    found = set()
    if isinstance(node, dict):
        found |= {k for k in node if k not in VOCABULARY and k != "<key>"}
        for v in node.values():
            found |= set(new_keys(v))
    elif isinstance(node, list):
        for v in node:
            found |= set(new_keys(v))
    return sorted(found)


def _bad_text(text):
    if any(p.search(text) for p in (EMAIL, SPELLED_EMAIL, URL, DOMAIN, LEFT_AT)):
        return True
    return any(sum(c.isdigit() for c in m.group(0)) >= 9 for m in DIGIT_RUN.finditer(text))


def residue(sample):
    """True when any key or string in sample holds an email, url, domain, @handle or phone-like run, or
    when sample holds a number at all. The backstop behind skeleton()."""
    if isinstance(sample, dict):
        return any(_bad_text(str(k)) or residue(v) for k, v in sample.items())
    if isinstance(sample, list):
        return any(residue(v) for v in sample)
    if isinstance(sample, (int, float)) and not isinstance(sample, bool):
        return True
    return isinstance(sample, str) and _bad_text(sample)


def _dig(row, *path):
    for key in path:
        if not isinstance(row, dict):
            return None
        row = row.get(key)
    return row


def rows_of(body):
    return split_vendor_labels(body)[1] if isinstance(body, dict) else []


def _region(row):
    return _dig(row, "post", "ext", "region") or _dig(row, "ext", "region")


def _video_id(row):
    return _dig(row, "post", "id") or _dig(row, "id")


def local_share(body, market):
    """(videos from market, videos, share or None) for one tiktok/trending feed=local response."""
    rows = rows_of(body)
    hits = sum(1 for r in rows if str(_region(r) or "").upper() == market)
    return hits, len(rows), (hits / len(rows) if rows else None)


def overlap(ng_body, ke_body):
    """(shared video ids, NG ids, KE ids) between the NG and KE feeds."""
    ng = {str(_video_id(r)) for r in rows_of(ng_body) if _video_id(r) is not None}
    ke = {str(_video_id(r)) for r in rows_of(ke_body) if _video_id(r) is not None}
    return len(ng & ke), len(ng), len(ke)


def _key(value):
    return str(value)


def table(ledger, raw):
    """One report row per ledger row, in log order; each live row is paired with its raw response."""
    queues = {}
    for r in sorted(raw, key=lambda r: _key(r["fetched_at"])):
        queues.setdefault((r["route"], r["params_hash"]), []).append(r)
    out = []
    for led in sorted(ledger, key=lambda r: _key(r["logged_at"])):
        queue = [] if led.get("cache_hit") else queues.get((led["route"], led["params_hash"]), [])
        r = queue.pop(0) if queue else None
        body = r["body"] if r else None
        status = "cached" if led.get("cache_hit") else (r["http_status"] if r and r["http_status"] is not None
                                                         else "no response")
        reported = body.get("credits_used") if isinstance(body, dict) else None
        out.append({"probe": r["seed_key"] if r else None, "route": led["route"], "status": status,
                    "charged": led["credits_charged"], "reported": reported, "rows": len(rows_of(body))})
    return out


def _num(value):
    return "n/a" if value is None else f"{float(value):g}"


def render(run_id, rows, shares, ov, missing, withheld=()):
    lines = [
        "# SocialCrawl probe report",
        "",
        f"Run {run_id}, written {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC by core/collect/probe_report.py.",
        "Charged is what the ledger booked; reported is the vendor's credits_used from the response body.",
        "",
        "| probe | route | status | charged | reported | rows |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['probe'] or ''} | {r['route']} | {r['status']} | {_num(r['charged'])} | "
                     f"{_num(r['reported'])} | {r['rows']} |")
    total = sum(float(r["charged"] or 0) for r in rows)
    lines += ["", f"Total charged: {total:g} credits over {len(rows)} ledger rows.", ""]
    lines.append("Not called (skipped or stopped): " + ", ".join(missing) + "." if missing
                 else "Every planned probe was called.")
    if withheld:
        lines.append("Withheld by the redaction gate: " + ", ".join(withheld) + ".")

    lines += ["", "## TikTok feed=local: share of videos whose ext.region is the market", ""]
    low = []
    for market in ("ZA", "NG", "KE"):
        hits, total_rows, share = shares.get(market, (0, 0, None))
        if share is None:
            lines.append(f"- {market}: no videos")
            continue
        note = ""
        if share < LOCAL_FLOOR:
            note = ", under 40%"
            low.append(market)
        lines.append(f"- {market}: {hits} of {total_rows} ({share:.0%}){note}")

    lines += ["", "## NG and KE feeds", ""]
    shared, ng, ke = ov if ov else (0, 0, 0)
    if not ng or not ke:
        lines.append(f"Overlap not measured: a feed returned no videos ({ng} NG, {ke} KE).")
    elif shared:
        lines.append(f"The feeds overlap: NG and KE share {shared} video ids ({ng} NG, {ke} KE).")
    else:
        lines.append(f"NG and KE feeds are distinct: 0 shared video ids ({ng} NG, {ke} KE).")
    if low or (shared and ng and ke):
        lines += ["", "Under 40% local or overlapping feeds: per BUILD.md 0.5, move that budget to the X trends "
                      "seed, hub panels and country-filtered search before task 1.3."]
    return "\n".join(lines) + "\n"


def write_samples(raw, outdir=SAMPLES):
    """Write one skeleton sample per response: probe id, route and the body's key-and-type skeleton.

    Returns (paths written, file names withheld). The residue gate withholds any file that still holds
    a number, an email, a url, a domain, an @handle or a phone-like run.
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    written, withheld = [], []
    for r in raw:
        name = f"{r.get('seed_key') or 'unknown'}_{r['route'].replace('/', '-')}.json"
        sample = {"probe": r.get("seed_key"), "route": r["route"], "body": skeleton(r.get("body"))}
        if residue(sample):
            withheld.append(name)
            continue
        path = outdir / name
        path.write_text(json.dumps(sample, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        written.append(path)
    return written, withheld


def write_bodies(raw, path=BODY_FIXTURE, run_id=""):
    """Merge one redacted entry per --bodies response into the fixture at path, keyed by route.

    Returns (routes written, routes withheld by the residue gate).
    """
    ids = {p["id"] for p in bodies_plan(BODY_ROUTE_NAMES)[0]}
    path = Path(path)
    fixture = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    fixture["_note"] = ("Redacted SocialCrawl bodies from probe runs in the --bodies mode: every value is its type "
                        "name, a list shows its length and at most three elements, and a dict with a key that is "
                        "not a field name shows one <key> entry. Never evidence.")
    written, withheld = [], []
    for r in raw:
        if r.get("seed_key") not in ids:
            continue
        entry = {"probe": r["seed_key"], "run": run_id, "body": redact(r.get("body"))}
        if residue(entry):
            withheld.append(r["route"])
            continue
        fixture[r["route"]] = entry
        written.append(r["route"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return written, withheld


def render_bodies(run_id, rows, withheld=(), new_keys=()):
    """The REPORT.md section for a --bodies run: credits charged per route by group, and what was kept.
    A --routes run that reached only the Ask price routes is totalled and checked over those groups alone."""
    calls = bodies_plan(BODY_ROUTE_NAMES)[0]
    group = {p["id"]: p["group"] for p in calls}
    seen = {group[r["probe"]] for r in rows if r["probe"] in group}
    extra = [g for g in PRICE_GROUPS if g in seen]
    shown = extra if seen and seen <= set(PRICE_GROUPS) else list(GROUPS) + extra
    lines = [
        BODIES_HEADING, "",
        f"Run {run_id}. Redacted bodies are in core/collect/tests/fixtures/socialcrawl_bodies.json.", "",
        "| probe | group | route | status | charged | reported | rows |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['probe'] or ''} | {group.get(r['probe'], '')} | {r['route']} | {r['status']} | "
                     f"{_num(r['charged'])} | {_num(r['reported'])} | {r['rows']} |")
    totals = {g: sum(float(r["charged"] or 0) for r in rows if group.get(r["probe"]) == g) for g in shown}
    lines += ["", " ".join(f"{g.capitalize()}: {totals[g]:g} credits." for g in shown)
              + f" Total: {sum(float(r['charged'] or 0) for r in rows):g} credits.", ""]
    called = {r["probe"] for r in rows}
    missing = [p["id"] for p in calls if p["id"] not in called and p["group"] in shown]
    lines.append("Not called (skipped or stopped): " + ", ".join(missing) + "." if missing
                 else "Every planned route was called.")
    if withheld:
        lines.append("Withheld by the redaction gate: " + ", ".join(withheld) + ".")
    if new_keys:
        lines.append("Field names kept that are not in the Stage 0 vocabulary; read them for a handle before "
                     "committing the fixture: " + ", ".join(new_keys) + ".")
    return "\n".join(lines) + "\n"


def merge_section(text, section):
    """text with its bodies section, if any, replaced by section at the end."""
    head = text.split(BODIES_HEADING)[0].rstrip("\n")
    return f"{head}\n\n{section}" if head else section


def read_run(run_id):
    """This run's raw_responses and credit_ledger rows, each from a SELECT parameterised by run_id."""
    from google.cloud import bigquery

    bq = bigquery.Client(project=PROJECT)
    config = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("run_id", "STRING", run_id)])
    core = f"{PROJECT}.{DATASET}"
    raw = [dict(r) for r in bq.query(
        "SELECT route, params_hash, seed_key, market, fetched_at, http_status, credits_charged, cache_hit, "
        f"TO_JSON_STRING(body) AS body FROM `{core}.raw_responses` WHERE run_id = @run_id ORDER BY fetched_at",
        job_config=config).result()]
    for r in raw:
        r["body"] = json.loads(r["body"]) if r["body"] not in (None, "null") else None
    ledger = [dict(r) for r in bq.query(
        "SELECT route, params_hash, market, calls, credits_quoted, credits_charged, cache_hit, logged_at "
        f"FROM `{core}.credit_ledger` WHERE run_id = @run_id ORDER BY logged_at",
        job_config=config).result()]
    return ledger, raw


def main(argv=None):
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("run_id", help="the run_id the probe job printed, probe-<utc timestamp>")
    args.add_argument("--out", default=str(SAMPLES), help="folder for the samples and REPORT.md")
    args.add_argument("--bodies", action="store_true", help="a probe --bodies run: write the redacted fixture")
    args.add_argument("--fixture", default=str(BODY_FIXTURE), help="the fixture file --bodies merges into")
    opts = args.parse_args(argv)

    ledger, raw = read_run(opts.run_id)
    if opts.bodies:
        written, withheld = write_bodies(raw, opts.fixture, opts.run_id)
        fixture = json.loads(Path(opts.fixture).read_text(encoding="utf-8"))
        kept = new_keys([fixture[route]["body"] for route in written])
        report = Path(opts.out) / "REPORT.md"
        old = report.read_text(encoding="utf-8") if report.exists() else ""
        report.write_text(merge_section(old, render_bodies(opts.run_id, table(ledger, raw), withheld, kept)),
                          encoding="utf-8")
        print(f"{len(ledger)} ledger rows, {len(raw)} responses, {len(written)} bodies written to {opts.fixture}, "
              f"{len(withheld)} withheld, report at {report}")
        return 0
    bodies = {r["seed_key"]: r["body"] for r in raw if r.get("seed_key") in TREND_PROBES}
    shares = {m: local_share(bodies.get(pid), m) for pid, m in TREND_PROBES.items()}
    ov = overlap(bodies.get("1-ng"), bodies.get("1-ke"))
    called = {r.get("seed_key") for r in raw}
    missing = [p["id"] for p in plan(datetime.now(SAST).date()) if p["id"] not in called]

    written, withheld = write_samples(raw, opts.out)
    report = Path(opts.out) / "REPORT.md"
    report.write_text(render(opts.run_id, table(ledger, raw), shares, ov, missing, withheld), encoding="utf-8")
    print(f"{len(ledger)} ledger rows, {len(raw)} responses, {len(written)} samples written, "
          f"{len(withheld)} withheld, report at {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
