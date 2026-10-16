"""Read-only location audit: how much of what 42 collects can be placed, by what, and how precisely.

    py -3.13 -m core.setup.location_audit --plan
        prints every query; builds no client, no network
    py -3.13 -m core.setup.location_audit --out location-audit.md
        runs the count queries and writes aggregate counts only; safe to paste into a cloud chat
    py -3.13 -m core.setup.location_audit --label-sheet C:\\path\\outside\\the\\repo\\location-labels.csv
        writes a stratified sample of recent posts WITH their text and link, for a person to label by hand;
        stays on this PC, never pasted or uploaded anywhere
    py -3.13 -m core.setup.location_audit --score C:\\path\\location-labels.csv --out location-score.md
        reads the labelled sheet back and writes counts only; safe to paste

Run from the root of a checkout of the 42 repo, on Albert's PC, with the default BigQuery client whose application
default credentials impersonate f42-builder (RULES.md rule 13). Never the bq CLI (RULES.md rule 12). Modelled on
core/setup/inventory.py and reusing its project, location and refusal helpers: every statement is one read-only
SELECT or WITH, dry-run first and refused when the estimate is over MAX_BYTES, run with maximum_bytes_billed at
MAX_BYTES, and the run stops before a query that would take it past TOTAL_BYTES.

The count queries return counts, shares and code-level categories only (markets, platforms, series, routes,
geo sources, country codes). The one count query that reads post text (place_mention_reach) does so inside SQL
to count matches against the core/detect/geo.py gazetteer and returns numbers. Every returned row is checked
again before it is written: a string survives only in a known category column and only when short and
code-shaped, otherwise that query is withheld whole and says so.

The label sheet is the exception and says so: it holds post text and links, because a person cannot judge where a
post was made without them. It is written only to a path outside the repo, and only when asked for by name. Its
sample is frozen by a fixed salt, so the same window always gives the same posts.
"""
import argparse
import csv
import datetime as dt
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

try:
    from core.detect import geo
    from core.setup import inventory as inv
except ImportError:  # run from outside the repo root: look for it beside the working directory
    sys.path.insert(0, str(Path.cwd()))
    from core.detect import geo
    from core.setup import inventory as inv

PROJECT, LOCATION = inv.PROJECT, inv.LOCATION
Refused, first_line, bytes_text = inv.Refused, inv.first_line, inv.bytes_text
ROOT = Path(__file__).resolve().parents[2]
MAX_BYTES = 2 * 1024 ** 3            # per query, the agent views' cap (DATA.md section 7)
TOTAL_BYTES = 10 * 1024 ** 3         # the whole run
WINDOW_DAYS = 14
POST_LOOKBACK_DAYS = 730             # posts.post_date can be far older than the sighting (profile videos)
TEXT_LOOKBACK_DAYS = 60              # queries that read text keep to recent posts, to stay under MAX_BYTES
ROW_LIMIT = 4000
CORE = f"`{PROJECT}`.intelligence_42_core"
MARKETS = geo.MARKETS
SALT = "location-labels-v1"
QUOTAS = {"ZA": 300, "NG": 150, "KE": 150}
TEXT_CHARS = 600
LOCATION_ROUTES = ("instagram/location/posts", "tiktok/location/posts", "youtube/search/advanced",
                   "facebook/search/posts")

CATEGORIES = {"market", "platform", "series", "route", "lane_class", "source", "bucket", "geo_market"}
SAFE_STRING = re.compile(r"^[A-Za-z0-9_:./=?&+, -]{0,96}$")
DML = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|EXPORT|CALL|DECLARE|EXECUTE|GRANT|"
                 r"REVOKE|BEGIN|LOAD)\b", re.IGNORECASE)
# Content columns a count query may not name. text is allowed only where reads_content says.
CONTENT = re.compile(r"\b(transcript|url|thumbnail_url|handle|display_name|body|profile_location|payload|"
                     r"embedding|creator_id|native_id)\b", re.IGNORECASE)
CONTENT_INNER = re.compile(r"\btext\b", re.IGNORECASE)

LABEL_COLUMNS = ("label_country", "label_province", "label_city", "label_basis", "label_note")
BASES = ("tag", "caption", "profile", "video", "cannot_tell")
SHEET_COLUMNS = ("sample_id", "market", "platform", "series", "route", "geo_market", "geo_source",
                 "geo_confidence", "place_id", "post_date", "url", "text") + LABEL_COLUMNS


class Withheld(Exception):
    pass


@dataclass
class Query:
    name: str
    purpose: str
    sql: str
    params: dict = field(default_factory=dict)
    reads_content: bool = False


def fill(sql):
    return " ".join(sql.replace("{core}", CORE).split())


def check_statement(q, *, sheet=False):
    """One read-only SELECT or WITH statement on this project; count queries name no content column."""
    sql = q.sql
    if not re.match(r"^(SELECT|WITH)\s", sql) or ";" in sql:
        raise ValueError(f"{q.name}: refusing a statement that is not a single SELECT")
    if DML.search(sql):
        raise ValueError(f"{q.name}: refusing a statement with a write or script keyword")
    if re.search(r"SELECT\s+(\w+\.)?\*|\.\*", sql, re.IGNORECASE):
        raise ValueError(f"{q.name}: refusing SELECT *")
    for ref in re.findall(r"`([^`]+)`", sql):
        if ref != PROJECT:
            raise ValueError(f"{q.name}: refusing a reference outside project {PROJECT}: {ref}")
    if sheet:
        return
    if CONTENT.search(sql):
        raise ValueError(f"{q.name}: refusing a statement that names a content column")
    if CONTENT_INNER.search(sql) and not q.reads_content:
        raise ValueError(f"{q.name}: reads post text without being marked to")


def scalar_params(params):
    from google.cloud import bigquery

    out = []
    for name, value in sorted(params.items()):
        if isinstance(value, (list, tuple)):
            out.append(bigquery.ArrayQueryParameter(name, "STRING", list(value)))
            continue
        kind = "DATE" if isinstance(value, dt.date) else "INT64" if isinstance(value, int) else "STRING"
        out.append(bigquery.ScalarQueryParameter(name, kind, value))
    return out


def safe_value(column, value):
    if value is None or isinstance(value, (bool, int, float, dt.date, dt.datetime)):
        return value
    if isinstance(value, str) and column in CATEGORIES and SAFE_STRING.match(value):
        return value
    raise Withheld(f"column {column} returned a value that is not a count, date or short code")


def guard_rows(rows):
    out = []
    for r in rows:
        items = r.items() if hasattr(r, "items") else dict(r).items()
        out.append({k: safe_value(k, v) for k, v in items})
    return out


def execute(client, q, log, budget, *, sheet=False):
    """Dry run, refuse over MAX_BYTES or the run budget, then run capped. Returns the raw rows."""
    from google.api_core.exceptions import GoogleAPIError
    from google.cloud import bigquery

    check_statement(q, sheet=sheet)
    params = scalar_params(q.params)
    entry = {"name": q.name, "purpose": q.purpose, "estimate": None, "processed": None, "billed": None,
             "status": "ok", "rows": None, "truncated": False}
    log.append(entry)
    try:
        dry = client.query(q.sql, job_config=bigquery.QueryJobConfig(
            dry_run=True, use_query_cache=False, query_parameters=params), location=LOCATION)
        entry["estimate"] = dry.total_bytes_processed or 0
        if entry["estimate"] > MAX_BYTES:
            entry["status"] = "refused"
            raise Refused(f"dry run estimate {entry['estimate']:,} bytes is over the {MAX_BYTES:,} byte cap")
        if budget["spent"] + entry["estimate"] > TOTAL_BYTES:
            entry["status"] = "refused"
            raise Refused(f"the run budget of {TOTAL_BYTES:,} bytes would be passed")
        job = client.query(q.sql, job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=MAX_BYTES, query_parameters=params), location=LOCATION)
        rows = list(job.result())
    except GoogleAPIError as error:
        entry["status"] = f"failed: {type(error).__name__}: {first_line(error)}"
        raise
    entry["processed"] = job.total_bytes_processed
    entry["billed"] = job.total_bytes_billed
    budget["spent"] += entry["billed"] or entry["processed"] or 0
    return entry, rows


def run_count(client, q, log, budget):
    entry, rows = execute(client, q, log, budget)
    try:
        entry["rows"] = guard_rows(rows[:ROW_LIMIT])
    except Withheld as error:
        entry["status"] = f"withheld: {error}"
    entry["truncated"] = len(rows) > ROW_LIMIT
    return entry["rows"]


# Sightings in the window, one row per post, market and series, with the post's stored location.
SEEN = """
WITH seen AS (
  SELECT DISTINCT o.post_id, o.market, o.platform, o.series, o.route
  FROM {core}.post_observations o
  WHERE o.observed_date BETWEEN @since AND @until AND o.market IN UNNEST(@markets)
), located AS (
  SELECT s.market, s.platform, s.series, s.route, s.post_id,
    COALESCE(p.geo_source, 'none') AS source, p.geo_market,
    CASE WHEN p.geo_confidence >= 0.7 AND p.geo_market = s.market THEN 'in_market'
         WHEN p.geo_confidence >= 0.7 THEN 'elsewhere'
         WHEN p.geo_source = 'language' THEN 'language_hint'
         ELSE 'unknown' END AS bucket
  FROM seen s JOIN {core}.posts p ON p.post_id = s.post_id
  WHERE p.post_date BETWEEN DATE_SUB(@since, INTERVAL {lookback} DAY) AND @until
)
"""


def seen(sql):
    return fill(SEEN.replace("{lookback}", str(POST_LOOKBACK_DAYS)) + sql)


def place_pattern(market):
    """The gazetteer of core/detect/geo.py for one market as one RE2 pattern over LOWER(text)."""
    names = sorted(geo.PLACES[market], key=len, reverse=True)
    alts = "|".join(re.escape(n).replace(r"\ ", "[ _-]?") for n in names)
    return f"(^|[^a-z0-9])({alts})([^a-z0-9]|$)"


def queries(since, until):
    base = {"since": since, "until": until, "markets": list(MARKETS)}
    out = [
        Query("located_by_source",
              "posts seen per market, platform and series, by how they were located (one post can sit in two "
              "series, so rows do not add up to the market total; use located_by_platform for totals)",
              seen("SELECT market, platform, series, source, bucket, COUNT(DISTINCT post_id) AS posts "
                   "FROM located GROUP BY market, platform, series, source, bucket "
                   "ORDER BY market, platform, series, source, bucket"), dict(base)),
        Query("located_by_platform",
              "distinct posts per market and platform: share placed in the market, elsewhere, or not at all",
              seen("SELECT market, platform, COUNT(DISTINCT post_id) AS posts, "
                   "COUNT(DISTINCT IF(bucket = 'in_market', post_id, NULL)) AS in_market, "
                   "COUNT(DISTINCT IF(bucket = 'elsewhere', post_id, NULL)) AS elsewhere, "
                   "COUNT(DISTINCT IF(bucket = 'language_hint', post_id, NULL)) AS language_hint, "
                   "COUNT(DISTINCT IF(bucket = 'unknown', post_id, NULL)) AS unknown "
                   "FROM located GROUP BY market, platform ORDER BY market, platform"), dict(base)),
        Query("elsewhere_countries",
              "where posts collected for a market were placed when not in that market (diaspora and leakage)",
              seen("SELECT market, platform, geo_market, COUNT(DISTINCT post_id) AS posts FROM located "
                   "WHERE bucket = 'elsewhere' GROUP BY market, platform, geo_market "
                   "HAVING COUNT(DISTINCT post_id) >= 5 ORDER BY market, posts DESC"), dict(base)),
        Query("location_routes",
              "posts from routes that are fetched by place: how many, from how many places, how located",
              seen("SELECT market, route, COUNT(DISTINCT post_id) AS posts, "
                   "COUNT(DISTINCT IF(bucket = 'in_market', post_id, NULL)) AS in_market, "
                   "COUNT(DISTINCT IF(bucket = 'unknown', post_id, NULL)) AS unknown FROM located "
                   "WHERE route IN UNNEST(@routes) GROUP BY market, route ORDER BY market, route"),
              {**base, "routes": list(LOCATION_ROUTES)}),
        Query("location_hubs",
              "distinct place ids swept per market and route, and days with a sweep",
              fill("SELECT o.market, o.route, COUNT(DISTINCT o.seed_key) AS places, "
                   "COUNT(DISTINCT o.observed_date) AS days, COUNT(DISTINCT o.post_id) AS posts "
                   "FROM {core}.post_observations o WHERE o.observed_date BETWEEN @since AND @until "
                   "AND o.market IN UNNEST(@markets) AND o.route IN UNNEST(@routes) "
                   "GROUP BY o.market, o.route ORDER BY o.market, o.route"),
              {**base, "routes": list(LOCATION_ROUTES)}),
        Query("health_located_share",
              "collection_health located_share per market and platform, weighted by items, over the window",
              fill("SELECT h.market, h.platform, COUNT(DISTINCT h.day) AS days, SUM(h.items) AS items, "
                   "SAFE_DIVIDE(SUM(h.items * h.located_share), SUM(IF(h.located_share IS NULL, 0, h.items))) "
                   "AS located_share FROM {core}.collection_health h WHERE h.day BETWEEN @since AND @until "
                   "AND h.market IN UNNEST(@markets) GROUP BY h.market, h.platform "
                   "ORDER BY h.market, h.platform"), dict(base)),
    ]
    for market in MARKETS:
        out.append(Query(
            f"place_mention_reach_{market.lower()}",
            f"{market} posts whose text names a {market} city or province from the geo.py gazetteer: the ceiling "
            "for text-based province placement before precision is checked by hand (posts published from "
            f"{TEXT_LOOKBACK_DAYS} days before the window, so the totals here are a little under located_by_platform)",
            seen("SELECT s.market, s.platform, COUNT(DISTINCT s.post_id) AS posts, "
                 "COUNT(DISTINCT IF(REGEXP_CONTAINS(LOWER(p.text), @pattern), s.post_id, NULL)) AS names_place "
                 "FROM located s JOIN {core}.posts p ON p.post_id = s.post_id "
                 "WHERE s.market = @market AND p.post_date BETWEEN DATE_SUB(@since, INTERVAL "
                 f"{TEXT_LOOKBACK_DAYS} DAY) AND @until GROUP BY s.market, s.platform ORDER BY s.platform"),
            {**base, "market": market, "pattern": place_pattern(market)}, reads_content=True))
    return out


def sheet_query(since, until):
    """A frozen stratified sample per market: round robin over platform, geo source and series, then capped."""
    sql = seen(
        ", pick AS (SELECT l.market, l.platform, l.series, l.route, l.post_id, l.source, ROW_NUMBER() OVER (PARTITION BY l.market, l.platform, l.source, l.series "
        "ORDER BY FARM_FINGERPRINT(CONCAT(l.post_id, @salt))) AS rn FROM located l WHERE TRUE "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY l.market, l.post_id ORDER BY l.series) = 1), "
        "ranked AS (SELECT pick.market, pick.platform, pick.series, pick.route, pick.post_id, pick.source, ROW_NUMBER() OVER (PARTITION BY pick.market ORDER BY pick.rn, "
        "FARM_FINGERPRINT(CONCAT(pick.post_id, @salt))) AS k FROM pick), "
        "place AS (SELECT o.post_id, o.market, ANY_VALUE(o.seed_key) AS place_id FROM {core}.post_observations o "
        "WHERE o.observed_date BETWEEN @since AND @until AND o.route IN UNNEST(@routes) "
        "GROUP BY o.post_id, o.market) "
        "SELECT r.market, r.platform, r.series, r.route, p.geo_market, r.source AS geo_source, p.geo_confidence, "
        "pl.place_id, p.post_date, p.url, SUBSTR(p.text, 1, @chars) AS text "
        "FROM ranked r JOIN {core}.posts p ON p.post_id = r.post_id "
        "LEFT JOIN place pl ON pl.post_id = r.post_id AND pl.market = r.market "
        f"WHERE p.post_date BETWEEN DATE_SUB(@since, INTERVAL {TEXT_LOOKBACK_DAYS} DAY) AND @until "
        "AND ((r.market = 'ZA' AND r.k <= @za) OR (r.market = 'NG' AND r.k <= @ng) OR (r.market = 'KE' AND r.k <= @ke)) "
        "ORDER BY r.market, r.k")
    return Query("label_sheet", "stratified sample for hand labelling, with text and links", sql,
                 {"since": since, "until": until, "markets": list(MARKETS), "salt": SALT, "chars": TEXT_CHARS,
                  "routes": list(LOCATION_ROUTES), "za": QUOTAS["ZA"], "ng": QUOTAS["NG"], "ke": QUOTAS["KE"]})


def window(until=None, days=WINDOW_DAYS):
    until = until or (dt.date.today() - dt.timedelta(days=1))
    return until - dt.timedelta(days=days - 1), until


def pct(part, whole):
    return f"{100 * part / whole:.1f}%" if whole else "n/a"


def render_table(rows):
    if not rows:
        return ["(no rows)"]
    cols = list(rows[0].keys())
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        cells = []
        for c in cols:
            v = r[c]
            cells.append(f"{v:.3f}" if isinstance(v, float) else "" if v is None else str(v))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def render(log, since, until, when, spent):
    lines = [f"# Location audit, {since} to {until}", "",
             f"Run {when}. Read-only counts from {PROJECT}; {bytes_text(spent)} bytes billed in all. "
             "Placed means geo_confidence 0.7 or more (core/detect/geo.py); language is a hint, never placed. "
             "Country only: 42 stores no province yet.", ""]
    for e in log:
        lines += [f"## {e['name']}", "", e["purpose"], "",
                  f"Status {e['status']}; estimate {bytes_text(e['estimate'])}, billed {bytes_text(e['billed'])}"
                  + ("; truncated" if e["truncated"] else ""), ""]
        if e["rows"] is not None:
            lines += render_table(e["rows"]) + [""]
    return "\n".join(lines) + "\n"


def plan(since, until):
    lines = [f"Window {since} to {until}. Count queries:", ""]
    for q in queries(since, until):
        lines += [f"-- {q.name}: {q.purpose}", q.sql, ""]
    q = sheet_query(since, until)
    lines += ["Only with --label-sheet (holds text and links, stays on this PC):", "", f"-- {q.name}", q.sql]
    return "\n".join(lines)


def inside_repo(path):
    try:
        path.resolve().relative_to(ROOT)
        return True
    except ValueError:
        return False


def write_sheet(rows, path):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(SHEET_COLUMNS)
        for i, r in enumerate(rows, 1):
            r = dict(r.items() if hasattr(r, "items") else r)
            text = " ".join(str(r.get("text") or "").split())
            w.writerow([i] + [r.get(c) for c in SHEET_COLUMNS[1:11]] + [text] + [""] * len(LABEL_COLUMNS))
    return len(rows) if hasattr(rows, "__len__") else None


def score(path):
    """Counts only from a labelled sheet: what a person could place, and how often 42's country agreed."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    missing = [c for c in ("market", "platform", "geo_market", "geo_source", "geo_confidence") + LABEL_COLUMNS
               if rows and c not in rows[0]]
    if missing:
        raise ValueError(f"sheet is missing columns: {', '.join(missing)}")
    code = lambda v: (v or "").strip().upper()
    done = [r for r in rows if (r["label_basis"] or "").strip().lower() in BASES]
    by_market = defaultdict(Counter)
    agree = defaultdict(Counter)
    platform = defaultdict(Counter)
    for r in done:
        m, basis = code(r["market"]), r["label_basis"].strip().lower()
        country, province = code(r["label_country"]), (r["label_province"] or "").strip()
        c = by_market[m]
        c["labelled"] += 1
        c["cannot_tell"] += basis == "cannot_tell"
        c["country_known"] += bool(country) and basis != "cannot_tell"
        c["province_known"] += bool(province) and basis != "cannot_tell"
        c["city_known"] += bool((r["label_city"] or "").strip()) and basis != "cannot_tell"
        c[f"basis_{basis}"] += 1
        p = platform[(m, (r["platform"] or "none").strip().lower())]
        p["labelled"] += 1
        p["province_known"] += bool(province) and basis != "cannot_tell"
        try:
            placed = float(r["geo_confidence"] or 0) >= geo.KNOWN
        except ValueError:
            placed = False
        if placed and country:
            a = agree[(m, (r["geo_source"] or "none").strip().lower())]
            a["placed_and_labelled"] += 1
            a["agrees"] += code(r["geo_market"]) == country
    lines = ["# Location label score", "",
             f"{len(done)} of {len(rows)} rows labelled with a basis from {', '.join(BASES)}. Counts only.", "",
             "## What a person could place, per market", "",
             "| market | labelled | country known | province known | city known | cannot tell |", "|---|---|---|---|---|---|"]
    for m in sorted(by_market):
        c = by_market[m]
        lines.append(f"| {m} | {c['labelled']} | {c['country_known']} ({pct(c['country_known'], c['labelled'])}) | "
                     f"{c['province_known']} ({pct(c['province_known'], c['labelled'])}) | "
                     f"{c['city_known']} ({pct(c['city_known'], c['labelled'])}) | {c['cannot_tell']} |")
    lines += ["", "## Basis of the human label", "", "| market | " + " | ".join(BASES) + " |",
              "|---|" + "---|" * len(BASES)]
    for m in sorted(by_market):
        lines.append(f"| {m} | " + " | ".join(str(by_market[m][f'basis_{b}']) for b in BASES) + " |")
    lines += ["", "## Province reachable by hand, per platform", "", "| market | platform | labelled | province known |",
              "|---|---|---|---|"]
    for (m, p), c in sorted(platform.items()):
        lines.append(f"| {m} | {p} | {c['labelled']} | {c['province_known']} ({pct(c['province_known'], c['labelled'])}) |")
    lines += ["", "## 42's stored country against the label (placed posts only)", "",
              "| market | geo source | placed and labelled | agrees | precision |", "|---|---|---|---|---|"]
    for (m, s), a in sorted(agree.items()):
        lines.append(f"| {m} | {s} | {a['placed_and_labelled']} | {a['agrees']} | "
                     f"{pct(a['agrees'], a['placed_and_labelled'])} |")
    return "\n".join(lines) + "\n"


def main(argv=None, client=None):
    parser = argparse.ArgumentParser(description="Read-only location audit of what 42 collects.")
    parser.add_argument("--plan", action="store_true", help="print the planned queries; no client, no network")
    parser.add_argument("--out", type=Path, help="markdown file for counts (audit or score)")
    parser.add_argument("--label-sheet", type=Path, help="CSV for hand labelling; must be outside the repo")
    parser.add_argument("--score", type=Path, help="a labelled sheet to score; writes counts only")
    parser.add_argument("--until", type=dt.date.fromisoformat, help="last day of the window (default yesterday)")
    args = parser.parse_args(argv)
    since, until = window(args.until)
    if args.plan:
        print(plan(since, until))
        return 0
    if args.score:
        text = score(args.score)
        out = args.out or Path("location-score.md")
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}: counts only, safe to paste")
        return 0
    if not args.out and not args.label_sheet:
        parser.error("give --out, --label-sheet, --score or --plan")
    if args.label_sheet and inside_repo(args.label_sheet):
        parser.error("--label-sheet holds post text and links: write it outside the repo, never commit or upload it")
    if client is None:
        from google.cloud import bigquery
        client = bigquery.Client(project=PROJECT, location=LOCATION)
    log, budget = [], {"spent": 0}
    if args.out:
        for q in queries(since, until):
            try:
                run_count(client, q, log, budget)
            except Refused as error:
                log[-1]["status"] = f"refused: {error}"
            except Exception as error:  # one failed query never stops the rest
                if log and log[-1]["name"] == q.name:
                    log[-1]["status"] = log[-1]["status"] if log[-1]["status"] != "ok" else \
                        f"failed: {type(error).__name__}: {first_line(error)}"
                else:
                    log.append({"name": q.name, "purpose": q.purpose, "estimate": None, "billed": None,
                                "status": f"not run: {error}", "rows": None, "truncated": False})
        when = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        args.out.write_text(render(log, since, until, when, budget["spent"]), encoding="utf-8")
        print(f"wrote {args.out}: {len(log)} queries, {budget['spent']:,} bytes billed; counts only, safe to paste")
    if args.label_sheet:
        sheet_log = []
        _, rows = execute(client, sheet_query(since, until), sheet_log, budget, sheet=True)
        args.label_sheet.parent.mkdir(parents=True, exist_ok=True)
        write_sheet(rows, args.label_sheet)
        print(f"wrote {args.label_sheet}: {len(rows)} posts WITH text and links. Keep it on this PC; "
              "label it, then run --score on it and share only the score file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
