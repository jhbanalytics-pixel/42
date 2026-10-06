# 42: read-only staging checks for the 2 Oct release (post-deploy paste for L1)

Written 2 Oct 2026 from the code, not from staging (the cloud cannot reach staging). Every table, view, column, job, Scheduler entry, route and string below was checked against 001396c9 or against the branch that adds it. The release itself was read as `origin/fix/next-release-merge-qifmxs` at 4e9bee97 (the "Merge next release into one branch" head at the time of writing; there is no `fix/next-release-20261002` on origin). If the lead merges a different head, re-check the "only if carried" lines.

Nothing here writes. Every BigQuery read is dry-run first and refused above 1 GB, then run with `maximum_bytes_billed` = 1 GB, through the Python client (never the bq CLI). The passcode is read hidden into `F42_PASSCODE` inside the same pasted block, sent only as the `x-passcode` header (core/api/auth.py:252), and never printed or written. Receipts go to `~/dev/42-receipts`, one file per group with a UTC stamp, under `set -C`.

## Counts (107 rows: the 92 "Code on 001396c9" and built-here rows of remaining-2026-10-02.md, plus IG-7, IG-9, 2.5, 3.3, 1.11, 1, 1.12, 9, 5a and U1 to U6)

| Coverage | Rows |
|---|---|
| A read can prove the row once its data exists (30) | IG-10, 2.4, 2.5, 2.7, 2.12, 3.3, 3.6, 5, 7, 8, 12, 17, 1.12, 9, IG-9, QA-2, QA-4, QA-7, QA-10, QA-13, QA-15, QA-16, QA-17, UI-1, UI-8, UI-12, UI-21, UI-22, UI-23, U2 |
| A read proves part; the rest is in the last section (25) | 1.13, 1.14, 2, 2.1, 2.2, 2.6, 2.8, 2.9, 2.10, 2.11, 2.13, 3.1, 5a, 10, 11, 14, 15, 16, 18, 19, 22, 26, U1, U4, U5 |
| No read proves it (52) | 1.8, 1.11, 1, 1.17, 1.19, 3, 3.2, 3.4, 3.5, 4, 20, 21, 23, 24, 27, 28, 29, 30, 30a, 31, 32, 35, IG-7, IG-11, QA-1, QA-3, QA-5, QA-6, QA-8, QA-9, QA-11, QA-12, QA-14, UI-2 to UI-7, UI-9 to UI-11, UI-13 to UI-20, U3, U6 (switch-off guards are still checked for 29, 30a, 31, 35, IG-7) |

## How to run (L1, Albert's PC, Git Bash)

1. Paste section 0 once. It writes two small read-only helpers to `~/dev/42-receipts/tools-20261002/` (only if they are not there yet) and checks timing, identities and the release sha on `/api/health`.
2. Paste groups G1 to G9 in any order. Each block is one subshell: it sets its own variables, writes its raw output to a stamped receipt, and prints one `ROW <id>: PASS|NOT PROVEN (...)` line per check.
3. Each printed line reads `ROW <check id>:<row ids>: PASS|NOT PROVEN (detail)`; the check id is the one in the row index below.
4. Edit only the line `REL_SHA=` at the top of S0 and G1 (12 characters, `git rev-parse --short=12 <release commit>`; the API's `F42_VERSION` is 12 characters, core/api/deploy.sh:13). `D` defaults to today in SAST; set `D=YYYY-MM-DD` on the first line of a block to check another day.

When each check can pass:

| When | What becomes provable |
|---|---|
| Right after the deploy (2 Oct evening) | S0, G1.1 to G1.5, G1.9, G3.5, G5 bundle and route checks, G8.2, G9.3, all `-off` guards |
| After the first chain on the release (Sat 3 Oct, collect 02:00, brief 06:15 SAST) | G1.6, G1.7, G2, G3, G4, G5 data checks, G7.1, G8.1, G9.1 to G9.2 (run with D=2026-10-03) |
| Sun 4 Oct, after the second morning | G1.6 and G4.2 (two mornings), G2.3 (two seed days) |
| Mon 5 Oct after 07:30 SAST | G1.8 and G2.6 (drift and calendar analogues), G8.3 to G8.5, G5.11 (learn, scorecard, forecast scores, weekly quality) |
| After a funded Ask or a write someone else makes | G6 (Ask records), G7.2 (a breakout watch), G5.7 (a suppression row) |

A `-off` suffix marks a guard that proves a switch is still off as intended (pulse, Breaking, digest, scheduled asks, T2). It is not proof of the row's feature.

## Row index

"Check" names the check id; rows with no read check are in the last section with the reason. Branch tags: (R) in the release head 4e9bee97; (only if carried) a pushed branch that is not in it.

| Row | Check | Proof | Not proven when |
|---|---|---|---|
| Release | S0.4, G1.1, G1.5 | `/api/health` version = REL_SHA; every enabled job runs the digest of `jobs:<short sha>`; f42-api and f42-agent `F42_VERSION` = REL_SHA | any job or service on another image |
| 1.13 | G1.6, G4.2 | collect, understand, detect, brief executions succeeded on D; every market's brief `published_at` by 06:30 SAST on D-1 and D | a failed execution or a late market (forced detect failure: no read, see last section) |
| 1.14 | S0.4, G4.4 | health ok with checks auth, bigquery, agent ok; `/api/today` 200 | health not ok or Today not 200 (Ask part: G6.1 after a funded Ask) |
| 2.2, understand memory 6f9d1f6e (R) | G1.2, G3.2 | f42-understand limits memory 2Gi, cpu 2 (deploy_jobs.py:123 at R); `counts.cluster` has za, ng, ke, pan without `error` | other limits, or a market with an error |
| 2.1 (run only) | G3.1 | understand ok, `counts.enriched` > 0, no `enrich_error` | (the 200-post hand check: no read) |
| 2.3, 12, topic-spread ea64b5cf (R), origin-news c92128f8 (R) | G3.4, G3.5, G5.5 | detect `counts.spread`, `counts.news`, `counts.agent_views` status ok; views v_item_spread, v_item_origin, v_news_followthrough exist; topic pages return `spread`, `origin`, `news` and at least one is filled | a failed apply, a missing view, or every sampled topic empty |
| 2.4, 5 | G2.3 | yield rows (`yield_posts` not null) in seed_queue on D-1 and D for ZA, NG, KE; no item above 25% of a market's expansion credits in credit_ledger | a missing day or market, or a share above 0.25 |
| 2.5, 18, NA-6 code, gdelt-age d805a728 (R) | G1.7, G2.4 | f42-gdelt logged "GDELT seeds written for D"; no seed_queue query on D carries an age word, an age number or a possessive decade | no log line, or any matching seed |
| 2.6, 14, video-reading 2b7673f9 (R) | G3.3, G9.1 | understand `counts.video.read` > 0 and no `counts.video.error`; video credits within 200 | `{"off": true}`, an error, or read 0 (the 20-clip hand check: no read) |
| 2.9, 16, watch-breakout ddf5a526 (R) | G7.1, G7.2, G7.4 | detect `counts.watch` present; watch_matches rows on D; a watch whose rule is `{"breakout": true}` matched; `/api/alerts` 200 | no watch exists or none fired (creating one is a write) |
| 2.9 digest, alert-email 20ab75c2 (R) | G1.3-off, G1.4-off, G7.5-off | f42-digest job and f42-digest-0645 absent while DIGEST_READY is False (schedule.py:67 at R); no `digest` runs row | job or schedule present before Albert's secret |
| 2.10 (listing) | G1.9 | 12 alert policies named "42 ALERT <name>" (monitoring.py:33, :64) | fewer than 12 (the forced zero-rows run: no read) |
| 2.11, 15 (run only) | G3.7 | coaction runs row ok on D, coord_signals rows counted | failed coaction (campaign library: no read) |
| 2.12, YouTube trending, local-evidence (R) | G2.1, G2.2, G5.9 | every local series in collection_health on D has calls_ok > 0 (board_* and panel_ig_gossip, plus board_youtube); public feed records > 0 per market, no `public_feed_error` | any series with 0 ok calls, or a market with 0 feed records |
| 2.13 | G3.8 (input only) | breakout_signals rows on D listed for the eye review | (the review itself: no read) |
| 3.1, 19, compare-history-suppression 539e253d (R) | G5.6, G5.7 | `/api/compare?mode=markets` 200 for a Today item; no suppressed creator id in Compare or History responses | not 200, or a suppressed id found, or no suppression row exists |
| 3.3, 22, item-centroids 85269b49 (R) | G3.6 | detect `counts.item_centroids.centroids` > 0; new hashtag or sound versions in cultural_map since the deploy | failed step or 0 centroids |
| 3.6, calendar-analogues 44426363 (R) | G1.8, G2.6 | f42-drift logged "calendar analogues: appended" on or after Mon 5 Oct; every calendar row in the next 90 days has a calendar_analogues row | no log line, or calendar rows without an analogue row |
| 7, 26, learn-forecast-scoring 0740c51b (R) | G8.2, G8.4, G8.5 | forecast_score and weekly_quality exist; after Mon 5 Oct a learn row ok, forecast_score rows and weekly_quality rows for ALL, ZA, NG, KE | tables missing (schema not applied) or no rows |
| 8, promoted-forecasts ba4f7c02 / dccbce76 (R) | G8.1 | forecasts issue rows on D, detect `counts.forecast` not failed | 0 issue rows (promotion wording needs an Ask: no read) |
| 2.7, 17, IG-3 | G8.3, G5.11 | engine_scorecard rows for week_start 2026-09-28 in ZA, NG, KE; Coverage `scorecard` filled | fewer than 3 markets, or Coverage still shows the waiting text |
| 2.7 reference events dfa507a8 (only if carried) | G8.6 | `lead_time.value` not null in some market | null with reason "no reference entries" |
| 11 (Emerging part) | G3.9 | item_state on D has at least one `emerging` item | none yet (Rising part: no read) |
| 1.12, 9, 5a | G4.1 | brief ok on D and at least one explained card published | 0 explained cards (held reasons listed in the receipt) |
| IG-9, brief-write-suppression 4254bf8e (R), youtube-masking a10438b4 (R) | G4.3, G5.7 | no creator_id from v_suppressed_creators in D's brief payloads or in Today, Discover, topic, Compare, History responses | an id found, or no suppression row exists (adding one is a write) |
| QA-4, dropped-held-reason 3aebbf5b (R) | G4.5 | every dropped item with reason held_back reads "Held back today: ..." or "Held back today; see why below", never the bare "Held back" | a bare "Held back", or no such item today |
| UI-21, today-card-plain-words c3f00971 (R) | G4.6, G7.4, G5.14 | no "New to 42" or "Check pattern" in `/api/today` or `/api/alerts`; "New to 42" absent from the served bundle | either string found |
| UI-23, series-plain-names 799aa5c0 (R) | G4.7 | no "TikTok local feed" or "X hub accounts" in `/api/today` | either found |
| UI-1, UI-22 | G4.8 | heading starts "Taking off" only when a market has cards or more | mismatch |
| 30a strip, today-breaking-strip 6fb4ee46 (R) | G4.9, G3.5, G5.14 | every market in `/api/today` carries `breaking`; breaking_signals and v_breaking_signals_current exist; the bundle has "Breaking in the last few hours" | key, table, view or string missing |
| 30a, IG-10, breaking-signals 56856dca (R) | G1.3-off, G1.4-off, G3.10-off, G9.1 | f42-pulse, f42-breaking, f42-pulse-3h, f42-breaking-hourly absent; no breaking_signals rows since the deploy; pulse credits 0 | any present before Albert's go |
| 29, IG-7, scheduled-asks ec85fffe / 34449596 (R) | G1.3-off, G1.4-off | f42-scheduled-asks and f42-scheduled-asks-0700 absent while SCHEDULED_ASKS_READY is False | present |
| 31, 35, t2-skills-flag fd84462a (R) | G1.5 (-off), G5.12-off, G6.2-off | f42-agent has no `F42_T2_READY=1`; `/api/health` `t2_ready` false; no T2 Ask row since the deploy | flag on before Albert says so |
| 2.8, 10 (API part) | G5.1 | `/api/discover` 200 for ZA, NG, KE and `/api/discover/radar` 200 | any non-200 (journeys: no read) |
| UI-8, held-words-shared e257ae7a (R) | G5.2 | no `reason_text` in Discover, topic, Compare or alerts is a raw gate sentence (core/api/held_words.py patterns) | any raw gate sentence, or no held reason seen |
| QA-10 | G5.3 | a lifecycle rule sentence from core/api/discover.py LIFECYCLE appears in `/api/discover` | none of the five sentences |
| QA-13 | G5.4 | the topic page of a Today item returns evidence with at least one post | empty evidence |
| QA-15 | G5.8 | the newest ask in `/api/history/asks` opens through `/api/ask/{id}` with 200 | 404 "No Ask with that id" |
| QA-16 | G5.9 | `/api/coverage` has no "SocialCrawl" | found |
| QA-17, coverage-google-trends da500be3 (only if carried) | G2.5, G5.10 | collect `counts.google_bq_signals` > 0 and no `google_bq_error`; google_search_signals rows for ZA and NG on D; Coverage `not_seen` without "Google Trends (coming)" | no rows, or the old wording (expected while da500be3 is not carried) |
| QA-2 | G5.14 | "Trend numbers were not stored" absent from the served bundle | present |
| QA-7 | G5.14 | "Not ready yet" absent from the served bundle | present |
| UI-12, source-panel-empty d4a90844 (R) | G5.14 | "Pick a source under a claim" absent from the served bundle | present |
| U2 held-detail 602f0334 (only if carried) | G5.14 | "The check detail could not be read just now." present in the bundle | absent (expected while not carried) |
| watch-tone-surge a67b55a9 (only if carried) | G7.3 | v_item_tone_daily exists in intelligence_42_agent and detect `counts.watch.unread` absent | view missing (expected while not carried) |
| 2 (stored log), 1.14 Ask, U1, U4, U5 | G6.1, G6.3 to G6.5 | after a funded Ask: a complete ask row with steps; `run_date` is the SAST date of `started_at` (U4); `query_receipts` in the record (U1); `error.status` on model_unavailable (U5) | no Ask since the deploy, or the field missing |
| 6, NA-1 spend, caps | G9.1, G9.2, G9.3, G9.4 | ledger by share within caps.yaml on D; model_usd on D within MODEL_DAILY_USD (20 from 3 Oct); health `model_daily_cap_usd` 20; reconcile ok for D-1 | over a cap, or reconcile not ok |

## Names verified (file:line at 001396c9 unless marked R = 4e9bee97 or a named branch)

- Datasets: `intelligence_42_core` and `intelligence_42_agent` in project `ogilvy-trends-v2`, location US (core/schema/core.sql:1-2, agent.sql:1-2, core/detect/sqlrun.py:20-21).
- Tables and partitions: runs (run_date), briefs (brief_date), watch_matches (match_date), engine_scorecard (week_start), forecasts (issue_date), claim_checks, watches, v_watches_current (agent.sql:4-108); collection_health (day), seed_queue (seed_date), credit_ledger (trend_date), item_state (metric_date), coord_signals (metric_date), breakout_signals (metric_date), calendar (moment_date), calendar_analogues (moment_date, computed_at), cultural_map (kind, valid_from), post_enrichment (video_notes), google_search_signals (DATE(fetched_at)), v_suppressed_creators (core.sql and google_search_signals.sql). R adds breaking_signals and v_breaking_signals_current (core.sql:228, :290), forecast_score and weekly_quality (agent.sql:58, :65).
- Detect views: v_item_spread (spread.sql:20), v_item_origin (news.sql:151), v_news_followthrough (news.sql:95), v_sensitive_items (agent_views.sql:94); v_item_tone_daily only on feat/watch-tone-surge-20261002 (agent_views.sql:148 there).
- runs stages and counts keys: collect `public_feed_records_by_market`, `public_feed_error`, `google_bq_signals`, `google_bq_error` (collect/job.py:785, :794 R); understand `enriched`, `enrich_error`, `cluster.{za,ng,ke,pan}.error`, `video` (understand/job.py:243-285 R); detect `spread`, `news`, `agent_views`, `item_centroids`, `coaction`, `breakout`, `watch`, `seeds`, `forecast` (detect/job.py:224-245 R); brief `cards`, `held` (brief/job.py:793); learn rows dated week_start (detect/learn.py:144, :176 R); Ask rows stage `ask` with `record` JSON holding `steps` and `error` (api/agent_app.py:101-106, :262-286 R).
- Jobs (deploy_jobs.py JOBS at R): f42-probe, f42-gdelt, f42-collect, f42-understand (2Gi, 2 cpu), f42-detect, f42-brief, f42-reconcile, f42-watchdog, f42-drift, f42-learn; off: f42-scheduled-asks, f42-pulse, f42-breaking, f42-digest. Image `us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/jobs@<digest>` (deploy_jobs.py:63-64, :374 R).
- Scheduler (schedule.py SCHEDULES at R, location us-central1): on f42-collect-0200, f42-brief-0615, f42-reconcile-2330, f42-gdelt-0130, f42-watchdog-quarter, f42-watchdog-hourly, f42-drift-mon-0700, f42-learn-mon-0730; off f42-scheduled-asks-0700, f42-pulse-3h, f42-breaking-hourly, f42-digest-0645.
- Services f42-api and f42-agent, env `F42_VERSION` (deploy_flags.env:11, :17), `F42_T2_READY` (agent_app.py:297-302 R). Routes: `/api/health` (open; `version`, `model_daily_cap_usd`, `t2_ready`, app.py:146-175 R), `/api/today`, `/api/discover`, `/api/discover/radar`, `/api/topics/{id}?market=`, `/api/compare?mode=markets&items=&markets=`, `/api/suppressions`, `/api/history/asks`, `/api/history/search`, `/api/ask/{id}`, `/api/coverage`, `/api/alerts`, `/api/watches` (app.py at R). Static bundle under `/assets` (open, app.py:1028-1031).
- Log lines: "seed_queue: N GDELT seeds written for D" (collect/gdelt.py:589), "calendar analogues: appended" (collect/drift.py:185 R), "42 ALERT <name>" (setup/monitoring.py:64-65).
- Ledger shares (credit_ledger.job = the client's share): collect 1000, confirm 150, reserve 50, ask 600, build, eval, pulse 60, video 200 (socialcrawl_client.py:672-681 R, caps.yaml R).

---

## Section 0: preflight (paste once)

```bash
( set -C
P=ogilvy-trends-v2; REG=us-central1
REL_SHA=PASTE_12_CHAR_RELEASE_SHA
STAMP=$(date -u +%Y%m%dT%H%M%SZ); RD="$HOME/dev/42-receipts"; T="$RD/tools-20261002"
mkdir -p "$RD" "$T"
R="$RD/postdeploy-S0-preflight-$STAMP.txt"; : > "$R" || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

[ -e "$T/bqcheck_v1.py" ] || cat > "$T/bqcheck_v1.py" <<'PY'
"""Read-only BigQuery check for the post-deploy paste.
py -3.13 bqcheck_v1.py ROW_ID SQL_FILE [name=value ...]
Refuses a file that is not a plain SELECT, dry-runs it, refuses above 1 GB, runs it capped at 1 GB, prints every row
as JSON and one line "ROW <id>: PASS|NOT PROVEN (<note>)" from the first row's verdict and note columns."""
import json
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
CAP = 1_000_000_000
row_id, path, *pairs = sys.argv[1:]
sql = open(path, encoding="utf-8").read()
plain = re.sub(r"--[^\n]*", "", sql).lower()
if re.search(r"\b(insert|update|delete|merge|create|drop|alter|truncate|grant|revoke|call|execute)\b", plain):
    print(f"ROW {row_id}: NOT PROVEN (refused: {path} is not read-only)")
    sys.exit(0)
from google.cloud import bigquery  # noqa: E402


def param(name, value):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return bigquery.ScalarQueryParameter(name, "DATE", value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", value):
        return bigquery.ScalarQueryParameter(name, "TIMESTAMP", value)
    return bigquery.ScalarQueryParameter(name, "STRING", value)


params = [param(*p.split("=", 1)) for p in pairs]
client = bigquery.Client(project="ogilvy-trends-v2", location="US")
try:
    dry = client.query(sql, job_config=bigquery.QueryJobConfig(
        dry_run=True, use_query_cache=False, query_parameters=params))
    size = dry.total_bytes_processed or 0
    print(f"# {row_id} dry run {size:,} bytes")
    if size > CAP:
        print(f"ROW {row_id}: NOT PROVEN (dry run {size:,} bytes is over the 1 GB cap; not run)")
        sys.exit(0)
    job = client.query(sql, job_config=bigquery.QueryJobConfig(
        maximum_bytes_billed=CAP, query_parameters=params))
    rows = [dict(r.items()) for r in job.result(timeout=300)]
except Exception as exc:  # a missing table or column is a result, not a crash
    first = (str(exc).splitlines() or [""])[0][:300]
    print(f"ROW {row_id}: NOT PROVEN (query error {type(exc).__name__}: {first})")
    sys.exit(0)
print(f"# {row_id} billed {job.total_bytes_billed or 0:,} bytes")
for r in rows:
    print(json.dumps(r, default=str, ensure_ascii=False))
top = rows[0] if rows else {}
print(f"ROW {row_id}: {'PASS' if top.get('verdict') == 'PASS' else 'NOT PROVEN'} ({top.get('note') or 'no rows'})")
PY

[ -e "$T/f42api_v1.py" ] || cat > "$T/f42api_v1.py" <<'PY'
"""Read-only GETs against the staging API for the post-deploy paste. API_URL comes from the environment; a gated
GET sends F42_PASSCODE from the environment as the x-passcode header and never prints or writes it."""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")


def get(path, params=None, gated=True, timeout=120):
    """(status, parsed JSON or None, text). Status 0 when the request did not complete."""
    url = os.environ["API_URL"].rstrip("/") + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method="GET")
    if gated:
        req.add_header("x-passcode", os.environ.get("F42_PASSCODE", ""))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code, body = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        code, body = exc.code, exc.read()
    except Exception as exc:
        return 0, None, type(exc).__name__
    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    print(f"# GET {path} {params or ''} -> {code}")
    print(text[:20000] + (" ...[cut]" if len(text) > 20000 else ""))
    return code, data, text


def walk(obj):
    """Every (key, value) pair in nested dicts and lists."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def row(rid, ok, note):
    print(f"ROW {rid}: {'PASS' if ok else 'NOT PROVEN'} ({note})")
PY

{
echo "# S0 preflight $STAMP REL_SHA=$REL_SHA"
# S0.1 timing: Google APIs resolve IPv6 first and can stall
SLOW=""
for U in https://bigquery.googleapis.com/ https://run.googleapis.com/ https://oauth2.googleapis.com/ https://monitoring.googleapis.com/ https://logging.googleapis.com/; do
  A=$(curl -sS -o /dev/null --max-time 20 -w '%{time_total}' "$U" 2>/dev/null) || A="fail"
  B=$(curl -4 -sS -o /dev/null --max-time 20 -w '%{time_total}' "$U" 2>/dev/null) || B="fail"
  echo "timing $U default=$A ipv4=$B"
  awk -v a="$A" 'BEGIN{exit !(a=="fail" || a+0 > 5)}' && SLOW="$SLOW $U"
done
[ -z "$SLOW" ] && echo "ROW S0.1: PASS (every endpoint answered within 5 s by default)" \
              || echo "ROW S0.1: NOT PROVEN (slow or failed by default:$SLOW; compare the ipv4 times above before going on)"

# S0.2 gcloud identity: the owner's default config; no impersonation needed for reads
ACC=$(gcloud config get-value account 2>/dev/null); PRJ=$(gcloud config get-value project 2>/dev/null)
IMP=$(gcloud config get-value auth/impersonate_service_account 2>/dev/null)
echo "gcloud account=$ACC project=$PRJ impersonation=${IMP:-none}"
[ -n "$ACC" ] && echo "ROW S0.2: PASS (gcloud account $ACC, impersonation ${IMP:-none})" \
              || echo "ROW S0.2: NOT PROVEN (no gcloud account configured)"

# S0.3 ADC account (what the Python BigQuery client uses); the token is used once and never printed
py -3.13 - <<'PY'
import sys
sys.stdout.reconfigure(encoding="utf-8")
try:
    import google.auth
    import google.auth.transport.requests
    import requests
    from google.cloud import bigquery  # noqa: F401
    creds, project = google.auth.default()
    creds.refresh(google.auth.transport.requests.Request())
    email = getattr(creds, "service_account_email", None)
    if not email:
        info = requests.post("https://oauth2.googleapis.com/tokeninfo", data={"access_token": creds.token}, timeout=20)
        email = info.json().get("email")
    print(f"ADC account={email} adc_project={project} quota_project={getattr(creds, 'quota_project_id', None)}")
    print(f"ROW S0.3: {'PASS' if email else 'NOT PROVEN'} (ADC account {email or 'unknown: no email scope'})")
except Exception as exc:
    print(f"ROW S0.3: NOT PROVEN (ADC or google-cloud-bigquery not usable: {type(exc).__name__})")
PY

# S0.4 release sha on the open health route
API_URL=$(gcloud run services describe f42-api --project=$P --region=$REG --format='value(status.url)' 2>/dev/null)
export API_URL REL_SHA
echo "f42-api url=$API_URL"
py -3.13 - "$T" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import os
import f42api_v1 as api
rel = os.environ["REL_SHA"]
code, h, _ = api.get("/api/health", gated=False)
h = h or {}
checks = h.get("checks") or {}
ok = code == 200 and h.get("ok") is True and h.get("version") == rel
api.row("S0.4", ok, f"health {code}, ok={h.get('ok')}, version={h.get('version')} want {rel}, checks={checks}, "
        f"t2_ready={h.get('t2_ready')}, model_daily_cap_usd={h.get('model_daily_cap_usd')}")
PY
} >> "$R" 2>&1
echo "Receipt: $R"; grep -a '^ROW ' "$R"
)
```

---

## G1: jobs and schedules

```bash
( set -C
P=ogilvy-trends-v2; REG=us-central1
REL_SHA=PASTE_12_CHAR_RELEASE_SHA
D=${D:-$(date -u -d '+2 hours' +%F)}
STAMP=$(date -u +%Y%m%dT%H%M%SZ); RD="$HOME/dev/42-receipts"; T="$RD/tools-20261002"
R="$RD/postdeploy-G1-jobs-$STAMP.txt"; W="$RD/postdeploy-G1-work-$STAMP"
[ -e "$T/f42api_v1.py" ] || { echo "Run section 0 first."; exit 1; }
mkdir "$W" || exit 1; : > "$R" || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
{
echo "# G1 jobs and schedules $STAMP D=$D REL_SHA=$REL_SHA"
gcloud artifacts docker tags list "us-central1-docker.pkg.dev/$P/intelligence-42/jobs" --project=$P --format=json > "$W/tags.json" 2> "$W/tags.err"
for J in f42-probe f42-gdelt f42-collect f42-understand f42-detect f42-brief f42-reconcile f42-watchdog f42-drift f42-learn \
         f42-scheduled-asks f42-pulse f42-breaking f42-digest; do
  gcloud run jobs describe "$J" --project=$P --region=$REG --format=json > "$W/job-$J.json" 2> "$W/job-$J.err"
done
for J in f42-collect f42-understand f42-detect f42-brief; do
  gcloud run jobs executions list --job="$J" --project=$P --region=$REG --limit=6 --format=json > "$W/exec-$J.json" 2> "$W/exec-$J.err"
done
for S in f42-api f42-agent; do
  gcloud run services describe "$S" --project=$P --region=$REG --format=json > "$W/svc-$S.json" 2> "$W/svc-$S.err"
done
gcloud scheduler jobs list --project=$P --location=$REG --format=json > "$W/scheduler.json" 2> "$W/scheduler.err"
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="f42-gdelt" AND textPayload:"GDELT seeds written"' \
  --project=$P --freshness=3d --limit=10 --format=json > "$W/log-gdelt.json" 2> "$W/log-gdelt.err"
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="f42-drift" AND textPayload:"calendar analogues:"' \
  --project=$P --freshness=8d --limit=10 --format=json > "$W/log-drift.json" 2> "$W/log-drift.err"
py -3.13 - "$W" "$REL_SHA" "$D" <<'PY'
import datetime as dt
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
W, REL, D = sys.argv[1:4]


def load(name):
    try:
        with open(os.path.join(W, name), encoding="utf-8") as f:
            text = f.read().strip()
        return json.loads(text) if text else None
    except (OSError, ValueError):
        return None


def find(obj, key):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from find(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from find(v, key)


def row(rid, ok, note):
    print(f"ROW {rid}: {'PASS' if ok else 'NOT PROVEN'} ({note})")


def sast_day(ts):
    t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return (t + dt.timedelta(hours=2)).date().isoformat()


ON = ["f42-probe", "f42-gdelt", "f42-collect", "f42-understand", "f42-detect", "f42-brief", "f42-reconcile",
      "f42-watchdog", "f42-drift", "f42-learn"]
OFF = ["f42-scheduled-asks", "f42-pulse", "f42-breaking", "f42-digest"]

# G1.1 every enabled job on the release image digest (deploy_jobs.py deploys IMAGE@digest of jobs:<short sha>)
digest = None
for t in load("tags.json") or []:
    tag = str(t.get("tag", "")).rsplit("/", 1)[-1]
    if len(tag) >= 7 and REL.startswith(tag):
        digest = str(t.get("version", "")).rsplit("/", 1)[-1]
off_image = []
for j in ON:
    images = [str(i) for i in find(load(f"job-{j}.json"), "image")]
    print(f"# {j} image {images[:1]}")
    if not images or not digest or not any(i.endswith(digest) for i in images):
        off_image.append(j)
row("G1.1:release", digest is not None and not off_image,
    f"jobs:{REL[:7]}.. digest {digest}; jobs not on it: {', '.join(off_image) or 'none'}")

# G1.2 f42-understand memory 2Gi and cpu 2 (feat/understand-memory, deploy_jobs.py:123 at R)
limits = [l for l in find(load("job-f42-understand.json"), "limits") if isinstance(l, dict)]
lim = limits[0] if limits else {}
row("G1.2:2.2+1.13-memory", lim.get("memory") == "2Gi" and str(lim.get("cpu")) in ("2", "2000m"),
    f"f42-understand limits {lim or 'not found'}")

# G1.3-off the four switched-off jobs are not deployed
present = [j for j in OFF if load(f"job-{j}.json")]
row("G1.3-off:29+30a+IG-10+digest", not present, f"off jobs deployed: {', '.join(present) or 'none'} (pulse, breaking, digest, scheduled asks stay off)")

# G1.4 Scheduler: 8 entries enabled, the 4 off entries absent
sched = {str(s.get("name", "")).rsplit("/", 1)[-1]: s.get("state") for s in (load("scheduler.json") or [])}
want_on = ["f42-collect-0200", "f42-brief-0615", "f42-reconcile-2330", "f42-gdelt-0130", "f42-watchdog-quarter",
           "f42-watchdog-hourly", "f42-drift-mon-0700", "f42-learn-mon-0730"]
want_off = ["f42-scheduled-asks-0700", "f42-pulse-3h", "f42-breaking-hourly", "f42-digest-0645"]
bad_on = [n for n in want_on if sched.get(n) != "ENABLED"]
row("G1.4:1.5-schedules", not bad_on, f"not ENABLED: {', '.join(f'{n}={sched.get(n)}' for n in bad_on) or 'none'}")
bad_off = [n for n in want_off if n in sched]
row("G1.4-off:29+30a+digest", not bad_off, f"off entries present: {', '.join(bad_off) or 'none'}")

# G1.5 services on the release, T2 off on f42-agent
envs = {}
for s in ("f42-api", "f42-agent"):
    pairs = [e for lst in find(load(f"svc-{s}.json"), "env") if isinstance(lst, list) for e in lst if isinstance(e, dict)]
    envs[s] = {e.get("name"): e.get("value") for e in pairs}
ver = {s: envs[s].get("F42_VERSION") for s in envs}
row("G1.5:release", all(v == REL for v in ver.values()), f"F42_VERSION {ver} want {REL}")
row("G1.5-off:31+35", envs["f42-agent"].get("F42_T2_READY") != "1",
    f"f42-agent F42_T2_READY={envs['f42-agent'].get('F42_T2_READY')!r} (rows 31, 35 stay off until Albert)")

# G1.6 the chain's executions on D (SAST) all succeeded
chain = {}
for j in ("f42-collect", "f42-understand", "f42-detect", "f42-brief"):
    runs = [e for e in (load(f"exec-{j}.json") or [])
            if sast_day((e.get("metadata") or {}).get("creationTimestamp", "1970-01-01T00:00:00Z")) == D]
    ok = [e for e in runs if ((e.get("status") or {}).get("succeededCount") or 0) >= 1]
    chain[j] = f"{len(ok)}/{len(runs)} ok"
row("G1.6:1.13-chain", all(v.split("/")[0] != "0" for v in chain.values()), f"executions on {D}: {chain}")

# G1.7 GDELT seeds written for D (gdelt.py:589)
lines = [str(e.get("textPayload", "")) for e in (load("log-gdelt.json") or [])]
hit = [l for l in lines if f"GDELT seeds written for {D}" in l]
row("G1.7:2.5+18", bool(hit), hit[0].strip() if hit else f"no 'GDELT seeds written for {D}' line in 3 days")

# G1.8 drift job's calendar analogues step (drift.py:185 at R), Monday 5 Oct onwards
logs = [(str(e.get("timestamp", "")), str(e.get("textPayload", ""))) for e in (load("log-drift.json") or [])]
appended = [t for t, l in logs if "calendar analogues: appended" in l and t[:10] >= "2026-10-05"]
row("G1.8:3.6", bool(appended), f"appended at {appended[0]}" if appended else
    f"no 'calendar analogues: appended' since 5 Oct; lines seen: {[l[:120] for _, l in logs][:3]}")
PY

# G1.9 monitoring policies (row 2.10 listing half), via the Monitoring REST API with ADC
py -3.13 - <<'PY'
import sys
sys.stdout.reconfigure(encoding="utf-8")
try:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/monitoring.read"])
    s = AuthorizedSession(creds)
    names, token = [], None
    while True:
        r = s.get("https://monitoring.googleapis.com/v3/projects/ogilvy-trends-v2/alertPolicies",
                  params={"pageSize": 200, **({"pageToken": token} if token else {})}, timeout=60)
        r.raise_for_status()
        body = r.json()
        names += [p.get("displayName", "") for p in body.get("alertPolicies", [])]
        token = body.get("nextPageToken")
        if not token:
            break
    ours = sorted(n for n in names if n.startswith("42 ALERT "))
    print("# policies:", ours)
    want = ["collection_missing", "job_failed", "brief_late", "credits_low", "model_spend", "zero_rows",
            "schema_drift", "agent_error_rate", "reconcile", "drift", "seeds_failed", "agent_views_failed"]
    missing = [w for w in want if f"42 ALERT {w}" not in ours]
    print(f"ROW G1.9:2.10: {'PASS' if not missing else 'NOT PROVEN'} ({len(ours)} '42 ALERT' policies; missing: {', '.join(missing) or 'none'})")
except Exception as exc:
    print(f"ROW G1.9:2.10: NOT PROVEN (policy list failed: {type(exc).__name__})")
PY
} >> "$R" 2>&1
echo "Receipt: $R"; grep -a '^ROW ' "$R"
)
```

---

## G2: collect and feeds

```bash
( set -C
D=${D:-$(date -u -d '+2 hours' +%F)}; D1=$(date -u -d "$D -1 day" +%F)
STAMP=$(date -u +%Y%m%dT%H%M%SZ); RD="$HOME/dev/42-receipts"; T="$RD/tools-20261002"
R="$RD/postdeploy-G2-collect-$STAMP.txt"; W="$RD/postdeploy-G2-work-$STAMP"
[ -e "$T/bqcheck_v1.py" ] || { echo "Run section 0 first."; exit 1; }
mkdir "$W" || exit 1; : > "$R" || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
BQ() { py -3.13 "$T/bqcheck_v1.py" "$@"; }

cat > "$W/g2_1.sql" <<'SQL'
-- G2.1 (2.12): every local source series of D's latest ok collect run read at least once (collection_health)
WITH run AS (
  SELECT run_id FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
  WHERE run_date = @d AND stage = 'collect' AND status = 'ok'
  ORDER BY finished_at DESC LIMIT 1),
h AS (
  SELECT market, series, SUM(calls) AS calls, SUM(calls_ok) AS calls_ok, LOGICAL_AND(valid) AS valid,
    STRING_AGG(DISTINCT invalid_reason) AS invalid_reason
  FROM `ogilvy-trends-v2.intelligence_42_core.collection_health`
  WHERE day = @d AND run_id IN (SELECT run_id FROM run)
    AND series IN ('board_app_store_iphone', 'board_audiomack', 'board_boomplay', 'board_google_play',
                   'board_kworb_spotify', 'board_nairaland', 'board_shazam', 'board_shazam_city', 'board_turntable',
                   'panel_ig_gossip', 'board_youtube')
  GROUP BY market, series)
SELECT
  IF((SELECT COUNT(*) FROM run) = 1 AND COUNT(*) > 0 AND COUNTIF(IFNULL(calls_ok, 0) = 0) = 0,
     'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('ok collect runs on day: %d; series read: %d; with 0 ok calls: %s', (SELECT COUNT(*) FROM run), COUNT(*),
         IFNULL(STRING_AGG(IF(IFNULL(calls_ok, 0) = 0, CONCAT(market, ' ', series), NULL), ', '), 'none')) AS note,
  TO_JSON_STRING(ARRAY_AGG(STRUCT(market, series, calls, calls_ok, valid, invalid_reason) ORDER BY market, series)) AS detail
FROM h
SQL

cat > "$W/g2_2.sql" <<'SQL'
-- G2.2 (2.12 feeds; 2.12/QA-17 search signals in G2.5): public feed records per market on D's latest ok collect run
WITH r AS (
  SELECT run_id, counts FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
  WHERE run_date = @d AND stage = 'collect' AND status = 'ok'
  ORDER BY finished_at DESC LIMIT 1)
SELECT
  IF(SAFE_CAST(JSON_VALUE(r.counts, '$.public_feed_records_by_market.ZA') AS INT64) > 0
     AND SAFE_CAST(JSON_VALUE(r.counts, '$.public_feed_records_by_market.NG') AS INT64) > 0
     AND SAFE_CAST(JSON_VALUE(r.counts, '$.public_feed_records_by_market.KE') AS INT64) > 0
     AND JSON_VALUE(r.counts, '$.public_feed_error') IS NULL, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('collect %s: feed records %s, public_feed_error %s, seeds_used %s, news_seeds %s',
         IFNULL(r.run_id, 'none'),
         IFNULL(TO_JSON_STRING(JSON_QUERY(r.counts, '$.public_feed_records_by_market')), 'null'),
         IFNULL(JSON_VALUE(r.counts, '$.public_feed_error'), 'none'),
         IFNULL(JSON_VALUE(r.counts, '$.seeds_used'), 'null'),
         IFNULL(JSON_VALUE(r.counts, '$.news_seeds'), 'null')) AS note
FROM (SELECT 1 AS one) AS x LEFT JOIN r ON TRUE
SQL

cat > "$W/g2_3.sql" <<'SQL'
-- G2.3 (2.4, FEATURES 5): earned seeds used on D-1 and D in every market, and no item above 25% of a market's
-- expansion credits (credit_ledger lane expansion, job collect; item_id stands in for the cluster)
WITH y AS (
  SELECT seed_date, market, COUNT(*) AS yield_rows, SUM(yield_posts) AS posts
  FROM `ogilvy-trends-v2.intelligence_42_core.seed_queue`
  WHERE seed_date BETWEEN @d1 AND @d AND yield_posts IS NOT NULL
  GROUP BY seed_date, market),
c AS (
  SELECT trend_date, market, IFNULL(item_id, '(none)') AS item_id, SUM(credits_charged) AS credits
  FROM `ogilvy-trends-v2.intelligence_42_core.credit_ledger`
  WHERE trend_date BETWEEN @d1 AND @d AND job = 'collect' AND lane = 'expansion'
  GROUP BY trend_date, market, item_id),
s AS (
  SELECT trend_date, market, MAX(credits) / NULLIF(SUM(credits), 0) AS top_share
  FROM c GROUP BY trend_date, market)
SELECT
  IF((SELECT COUNT(*) FROM y WHERE market IN ('ZA', 'NG', 'KE')) = 6
     AND IFNULL((SELECT MAX(top_share) FROM s), 0) <= 0.25, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('market-days with yield rows: %d of 6; top expansion item share: %s',
         (SELECT COUNT(*) FROM y WHERE market IN ('ZA', 'NG', 'KE')),
         IFNULL(CAST(ROUND((SELECT MAX(top_share) FROM s), 3) AS STRING), 'no expansion credits')) AS note,
  (SELECT TO_JSON_STRING(ARRAY_AGG(STRUCT(seed_date, market, yield_rows, posts) ORDER BY seed_date, market)) FROM y) AS yields,
  (SELECT TO_JSON_STRING(ARRAY_AGG(STRUCT(trend_date, market, ROUND(top_share, 3) AS top_share) ORDER BY trend_date, market)) FROM s) AS shares
SQL

cat > "$W/g2_4.sql" <<'SQL'
-- G2.4 (2.5, 18, NA-6 code, d805a728): no seed for D carries an age word, an age number or a possessive decade
WITH q AS (
  SELECT market, lane, kind, query FROM `ogilvy-trends-v2.intelligence_42_core.seed_queue`
  WHERE seed_date = @d AND query IS NOT NULL),
hit AS (
  SELECT * FROM q WHERE REGEXP_CONTAINS(LOWER(query),
    r'\b(teens?|teenagers?|teenage|youths?|kids?|child|children|gen ?z|genz|millennials?|boomers?|elderly|seniors?|underage)\b|\b(thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)\b|\b\d{1,2} ?(yo|y/o|years? old|year-old)\b|\b(his|her|their|my|your|our) (early |mid |late )?[1-9]0s\b'))
SELECT
  IF((SELECT COUNT(*) FROM q) > 0 AND (SELECT COUNT(*) FROM hit) = 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('seeds for day: %d; age matches: %d', (SELECT COUNT(*) FROM q), (SELECT COUNT(*) FROM hit)) AS note,
  (SELECT TO_JSON_STRING(ARRAY_AGG(STRUCT(market, lane, kind, query) LIMIT 50)) FROM hit) AS matches
SQL

cat > "$W/g2_5.sql" <<'SQL'
-- G2.5 (QA-17 data, 2.12): Google search signals collected for ZA and NG on D (KE has no public-table rows)
WITH r AS (
  SELECT run_id, counts FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
  WHERE run_date = @d AND stage = 'collect' AND status = 'ok'
  ORDER BY finished_at DESC LIMIT 1),
g AS (
  SELECT market, source, COUNT(*) AS n FROM `ogilvy-trends-v2.intelligence_42_core.google_search_signals`
  WHERE DATE(fetched_at) BETWEEN DATE_SUB(@d, INTERVAL 1 DAY) AND @d
  GROUP BY market, source)
SELECT
  IF(SAFE_CAST(JSON_VALUE(r.counts, '$.google_bq_signals') AS INT64) > 0 AND JSON_VALUE(r.counts, '$.google_bq_error') IS NULL
     AND (SELECT SUM(n) FROM g WHERE market = 'ZA') > 0 AND (SELECT SUM(n) FROM g WHERE market = 'NG') > 0,
     'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('collect %s: google_bq_signals %s, google_bq_error %s, search_signals %s; rows %s',
         IFNULL(r.run_id, 'none'), IFNULL(JSON_VALUE(r.counts, '$.google_bq_signals'), 'null'),
         IFNULL(JSON_VALUE(r.counts, '$.google_bq_error'), 'none'), IFNULL(JSON_VALUE(r.counts, '$.search_signals'), 'null'),
         IFNULL((SELECT TO_JSON_STRING(ARRAY_AGG(STRUCT(market, source, n) ORDER BY market, source)) FROM g), '[]')) AS note
FROM (SELECT 1 AS one) AS x LEFT JOIN r ON TRUE
SQL

cat > "$W/g2_6.sql" <<'SQL'
-- G2.6 (3.6): every calendar row in the next 90 days has a calendar_analogues row (evidence, no_evidence or no_analogue)
WITH cal AS (
  SELECT DISTINCT moment_date, market, name FROM `ogilvy-trends-v2.intelligence_42_core.calendar`
  WHERE moment_date BETWEEN @d AND DATE_ADD(@d, INTERVAL 90 DAY)),
an AS (
  SELECT moment_date, market, name, MAX(computed_at) AS last_computed
  FROM `ogilvy-trends-v2.intelligence_42_core.calendar_analogues`
  WHERE moment_date BETWEEN @d AND DATE_ADD(@d, INTERVAL 90 DAY)
  GROUP BY moment_date, market, name),
j AS (
  SELECT cal.*, an.last_computed FROM cal LEFT JOIN an USING (moment_date, market, name))
SELECT
  IF(COUNT(*) > 0 AND COUNTIF(last_computed IS NULL) = 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('calendar rows: %d; without an analogue row: %d; newest computed_at: %s', COUNT(*),
         COUNTIF(last_computed IS NULL), IFNULL(CAST(MAX(last_computed) AS STRING), 'none')) AS note,
  TO_JSON_STRING(ARRAY_AGG(IF(last_computed IS NULL, STRUCT(moment_date, market, name), NULL) IGNORE NULLS LIMIT 40)) AS missing
FROM j
SQL

{
echo "# G2 collect and feeds $STAMP D=$D D1=$D1"
BQ G2.1:2.12 "$W/g2_1.sql" d=$D
BQ G2.2:2.12-feeds "$W/g2_2.sql" d=$D
BQ G2.3:2.4+5 "$W/g2_3.sql" d=$D d1=$D1
BQ G2.4:2.5+18+NA-6 "$W/g2_4.sql" d=$D
BQ G2.5:QA-17 "$W/g2_5.sql" d=$D
BQ G2.6:3.6 "$W/g2_6.sql" d=$D
} >> "$R" 2>&1
echo "Receipt: $R"; grep -a '^ROW ' "$R"
)
```

---

## G3: understand and detect tables

```bash
( set -C
P=ogilvy-trends-v2; REG=us-central1
D=${D:-$(date -u -d '+2 hours' +%F)}
STAMP=$(date -u +%Y%m%dT%H%M%SZ); RD="$HOME/dev/42-receipts"; T="$RD/tools-20261002"
R="$RD/postdeploy-G3-detect-$STAMP.txt"; W="$RD/postdeploy-G3-work-$STAMP"
[ -e "$T/bqcheck_v1.py" ] || { echo "Run section 0 first."; exit 1; }
mkdir "$W" || exit 1; : > "$R" || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
BQ() { py -3.13 "$T/bqcheck_v1.py" "$@"; }
REV=$(gcloud run services describe f42-api --project=$P --region=$REG --format='value(status.latestReadyRevisionName)' 2>/dev/null)
SINCE=$(gcloud run revisions describe "$REV" --project=$P --region=$REG --format='value(metadata.creationTimestamp)' 2>/dev/null)
SINCE=${SINCE:-2026-10-02T00:00:00Z}

cat > "$W/g3_1.sql" <<'SQL'
-- G3.1 (2.1 run): understand ok on D, posts enriched, no enrichment error
WITH r AS (
  SELECT run_id, status, counts FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
  WHERE run_date = @d AND stage = 'understand' ORDER BY finished_at DESC LIMIT 1)
SELECT
  IF(r.status = 'ok' AND SAFE_CAST(JSON_VALUE(r.counts, '$.enriched') AS INT64) > 0
     AND JSON_VALUE(r.counts, '$.enrich_error') IS NULL, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('understand %s %s: enriched %s, enrich_failed %s, enrich_error %s', IFNULL(r.run_id, 'none'), IFNULL(r.status, '-'),
         IFNULL(JSON_VALUE(r.counts, '$.enriched'), 'null'), IFNULL(JSON_VALUE(r.counts, '$.enrich_failed'), 'null'),
         IFNULL(JSON_VALUE(r.counts, '$.enrich_error'), 'none')) AS note,
  TO_JSON_STRING(JSON_QUERY(r.counts, '$.step_seconds')) AS step_seconds
FROM (SELECT 1 AS one) AS x LEFT JOIN r ON TRUE
SQL

cat > "$W/g3_2.sql" <<'SQL'
-- G3.2 (2.2 run, understand memory): every cluster market (za, ng, ke, pan) without an error on D
WITH r AS (
  SELECT run_id, status, counts FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
  WHERE run_date = @d AND stage = 'understand' ORDER BY finished_at DESC LIMIT 1)
SELECT
  IF(r.status = 'ok'
     AND JSON_QUERY(r.counts, '$.cluster.za') IS NOT NULL AND JSON_VALUE(r.counts, '$.cluster.za.error') IS NULL
     AND JSON_QUERY(r.counts, '$.cluster.ng') IS NOT NULL AND JSON_VALUE(r.counts, '$.cluster.ng.error') IS NULL
     AND JSON_QUERY(r.counts, '$.cluster.ke') IS NOT NULL AND JSON_VALUE(r.counts, '$.cluster.ke.error') IS NULL
     AND JSON_QUERY(r.counts, '$.cluster.pan') IS NOT NULL AND JSON_VALUE(r.counts, '$.cluster.pan.error') IS NULL,
     'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('understand %s %s: cluster %s', IFNULL(r.run_id, 'none'), IFNULL(r.status, '-'),
         SUBSTR(IFNULL(TO_JSON_STRING(JSON_QUERY(r.counts, '$.cluster')), 'null'), 1, 600)) AS note
FROM (SELECT 1 AS one) AS x LEFT JOIN r ON TRUE
SQL

cat > "$W/g3_3.sql" <<'SQL'
-- G3.3 (2.6, 14): video reading read clips on D (VIDEO_DAILY 20 clips, 200 credits at R)
WITH r AS (
  SELECT run_id, status, counts FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
  WHERE run_date = @d AND stage = 'understand' ORDER BY finished_at DESC LIMIT 1)
SELECT
  IF(SAFE_CAST(JSON_VALUE(r.counts, '$.video.read') AS INT64) > 0 AND JSON_VALUE(r.counts, '$.video.error') IS NULL,
     'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('understand %s: video %s; post_enrichment rows with video_notes (all time): %d', IFNULL(r.run_id, 'none'),
         IFNULL(TO_JSON_STRING(JSON_QUERY(r.counts, '$.video')), 'null'),
         (SELECT COUNT(*) FROM `ogilvy-trends-v2.intelligence_42_core.post_enrichment` WHERE video_notes IS NOT NULL)) AS note
FROM (SELECT 1 AS one) AS x LEFT JOIN r ON TRUE
SQL

cat > "$W/g3_4.sql" <<'SQL'
-- G3.4 (2.3, 12): detect applied v_item_spread, the news views and the agent views on D
WITH r AS (
  SELECT run_id, status, counts FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
  WHERE run_date = @d AND stage = 'detect' ORDER BY finished_at DESC LIMIT 1)
SELECT
  IF(r.status = 'ok' AND JSON_VALUE(r.counts, '$.spread.status') = 'ok' AND JSON_VALUE(r.counts, '$.news.status') = 'ok'
     AND JSON_VALUE(r.counts, '$.agent_views.status') = 'ok', 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('detect %s %s: spread %s, news %s, agent_views %s', IFNULL(r.run_id, 'none'), IFNULL(r.status, '-'),
         IFNULL(TO_JSON_STRING(JSON_QUERY(r.counts, '$.spread')), 'null'),
         IFNULL(TO_JSON_STRING(JSON_QUERY(r.counts, '$.news')), 'null'),
         IFNULL(TO_JSON_STRING(JSON_QUERY(r.counts, '$.agent_views')), 'null')) AS note
FROM (SELECT 1 AS one) AS x LEFT JOIN r ON TRUE
SQL

cat > "$W/g3_5.sql" <<'SQL'
-- G3.5 (2.3, 12, 30a strip, 7, 26 schema): the tables and views the release reads exist
WITH t AS (
  SELECT 'core' AS ds, table_name, table_type FROM `ogilvy-trends-v2.intelligence_42_core.INFORMATION_SCHEMA.TABLES`
  UNION ALL
  SELECT 'agent', table_name, table_type FROM `ogilvy-trends-v2.intelligence_42_agent.INFORMATION_SCHEMA.TABLES`),
want AS (
  SELECT * FROM UNNEST([
    STRUCT('core' AS ds, 'v_item_spread' AS table_name), ('core', 'v_breaking_signals_current'), ('core', 'breaking_signals'),
    ('core', 'v_suppressed_creators'), ('core', 'v_sensitive_items'), ('agent', 'v_item_origin'),
    ('agent', 'v_news_followthrough'), ('agent', 'v_news_bridge'), ('agent', 'v_watches_current'),
    ('agent', 'forecast_score'), ('agent', 'weekly_quality'), ('agent', 'engine_scorecard')]))
SELECT
  IF(COUNTIF(t.table_name IS NULL) = 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('missing: %s', IFNULL(STRING_AGG(IF(t.table_name IS NULL, CONCAT(want.ds, '.', want.table_name), NULL), ', '), 'none')) AS note
FROM want LEFT JOIN t ON t.ds = want.ds AND t.table_name = want.table_name
SQL

cat > "$W/g3_6.sql" <<'SQL'
-- G3.6 (3.3, 22): hashtag and sound centroids written by detect on D, and new cultural_map versions since the deploy
WITH r AS (
  SELECT run_id, status, counts FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
  WHERE run_date = @d AND stage = 'detect' ORDER BY finished_at DESC LIMIT 1)
SELECT
  IF(SAFE_CAST(JSON_VALUE(r.counts, '$.item_centroids.centroids') AS INT64) > 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('detect %s: item_centroids %s; hashtag or sound versions since %t: %d', IFNULL(r.run_id, 'none'),
         IFNULL(TO_JSON_STRING(JSON_QUERY(r.counts, '$.item_centroids')), 'null'), @since,
         (SELECT COUNT(*) FROM `ogilvy-trends-v2.intelligence_42_core.cultural_map`
          WHERE kind IN ('hashtag', 'sound') AND valid_from >= @since)) AS note
FROM (SELECT 1 AS one) AS x LEFT JOIN r ON TRUE
SQL

cat > "$W/g3_7.sql" <<'SQL'
-- G3.7 (2.11, 15 run only): coaction ran ok on D and its signals are counted
SELECT
  IF(COUNTIF(stage = 'coaction' AND status = 'ok') > 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('coaction runs ok %d, failed %d; coord_signals rows on day: %d',
         COUNTIF(stage = 'coaction' AND status = 'ok'), COUNTIF(stage = 'coaction' AND status != 'ok'),
         (SELECT COUNT(*) FROM `ogilvy-trends-v2.intelligence_42_core.coord_signals` WHERE metric_date = @d)) AS note
FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
WHERE run_date = @d AND stage = 'coaction'
SQL

cat > "$W/g3_8.sql" <<'SQL'
-- G3.8 (2.13 input for the eye review): breakout_signals rows on D
SELECT
  IF(COUNT(*) > 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('breakout rows on day: %d (input only; five real breakouts still need an eye review)', COUNT(*)) AS note,
  TO_JSON_STRING(ARRAY_AGG(STRUCT(market, item_id, creators, posts, top_ratio, held_flagged) ORDER BY top_ratio DESC LIMIT 25)) AS rows_
FROM `ogilvy-trends-v2.intelligence_42_core.breakout_signals`
WHERE metric_date = @d
SQL

cat > "$W/g3_9.sql" <<'SQL'
-- G3.9 (11, Emerging part): item_state of D's latest ok detect run has at least one emerging item
WITH r AS (
  SELECT run_id FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
  WHERE run_date = @d AND stage = 'detect' AND status = 'ok' ORDER BY finished_at DESC LIMIT 1),
s AS (
  SELECT market, state, COUNT(*) AS n FROM `ogilvy-trends-v2.intelligence_42_core.item_state`
  WHERE metric_date = @d AND run_id IN (SELECT run_id FROM r)
  GROUP BY market, state)
SELECT
  IF(IFNULL(SUM(IF(state = 'emerging', n, 0)), 0) > 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('emerging items: %d; rising items: %d', IFNULL(SUM(IF(state = 'emerging', n, 0)), 0),
         IFNULL(SUM(IF(state = 'rising', n, 0)), 0)) AS note,
  TO_JSON_STRING(ARRAY_AGG(STRUCT(market, state, n) ORDER BY market, state)) AS states
FROM s
SQL

cat > "$W/g3_10.sql" <<'SQL'
-- G3.10-off (30a, IG-10): no Breaking rows written since the deploy while PULSE_READY is False
SELECT
  IF(COUNT(*) = 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('breaking_signals rows since %t: %d (Breaking stays off until Albert says go)', @since, COUNT(*)) AS note
FROM `ogilvy-trends-v2.intelligence_42_core.breaking_signals`
WHERE DATE(hour) >= DATE(@since) AND hour >= @since
SQL

{
echo "# G3 understand and detect $STAMP D=$D since=$SINCE"
BQ G3.1:2.1-run "$W/g3_1.sql" d=$D
BQ G3.2:2.2-run "$W/g3_2.sql" d=$D
BQ G3.3:2.6+14 "$W/g3_3.sql" d=$D
BQ G3.4:2.3+12 "$W/g3_4.sql" d=$D
BQ G3.5:objects "$W/g3_5.sql"
BQ G3.6:3.3+22 "$W/g3_6.sql" d=$D since=$SINCE
BQ G3.7:2.11+15-run "$W/g3_7.sql" d=$D
BQ G3.8:2.13-input "$W/g3_8.sql" d=$D
BQ G3.9:11-emerging "$W/g3_9.sql" d=$D
BQ G3.10-off:30a+IG-10 "$W/g3_10.sql" since=$SINCE
} >> "$R" 2>&1
echo "Receipt: $R"; grep -a '^ROW ' "$R"
)
```

---

## G4: brief and Today

```bash
( set -C
P=ogilvy-trends-v2; REG=us-central1
D=${D:-$(date -u -d '+2 hours' +%F)}; D1=$(date -u -d "$D -1 day" +%F)
STAMP=$(date -u +%Y%m%dT%H%M%SZ); RD="$HOME/dev/42-receipts"; T="$RD/tools-20261002"
R="$RD/postdeploy-G4-today-$STAMP.txt"; W="$RD/postdeploy-G4-work-$STAMP"
[ -e "$T/f42api_v1.py" ] || { echo "Run section 0 first."; exit 1; }
mkdir "$W" || exit 1; : > "$R" || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
BQ() { py -3.13 "$T/bqcheck_v1.py" "$@"; }
API_URL=$(gcloud run services describe f42-api --project=$P --region=$REG --format='value(status.url)' 2>/dev/null); export API_URL
[ -n "${F42_PASSCODE:-}" ] || { read -rsp 'Staging passcode (hidden, not stored): ' F42_PASSCODE; echo; }
export F42_PASSCODE

cat > "$W/g4_1.sql" <<'SQL'
-- G4.1 (1.12, 9, 5a): D's current briefs per market, explained cards published, held count
WITH b AS (
  SELECT market, status, published_at, payload FROM `ogilvy-trends-v2.intelligence_42_agent.briefs`
  WHERE brief_date = @d
  QUALIFY ROW_NUMBER() OVER (PARTITION BY market ORDER BY published_at DESC) = 1),
m AS (
  SELECT market, status, published_at,
    ARRAY_LENGTH(JSON_QUERY_ARRAY(payload, '$.cards')) AS cards,
    (SELECT COUNTIF(JSON_VALUE(c, '$.explained') = 'true') FROM UNNEST(JSON_QUERY_ARRAY(payload, '$.cards')) AS c) AS explained,
    ARRAY_LENGTH(JSON_QUERY_ARRAY(payload, '$.held_back.items')) AS held
  FROM b)
SELECT
  IF(IFNULL(SUM(explained), 0) > 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('markets %d; cards %d; explained %d; held %d', COUNT(*), IFNULL(SUM(cards), 0), IFNULL(SUM(explained), 0),
         IFNULL(SUM(held), 0)) AS note,
  TO_JSON_STRING(ARRAY_AGG(STRUCT(market, status, published_at, cards, explained, held) ORDER BY market)) AS markets
FROM m
SQL

cat > "$W/g4_2.sql" <<'SQL'
-- G4.2 (1.13): two mornings (D-1 and D), every market's brief published by 06:30 SAST
WITH b AS (
  SELECT brief_date, market, MIN(published_at) AS first_published
  FROM `ogilvy-trends-v2.intelligence_42_agent.briefs`
  WHERE brief_date BETWEEN @d1 AND @d AND market IN ('ZA', 'NG', 'KE')
  GROUP BY brief_date, market)
SELECT
  IF(COUNTIF(first_published <= TIMESTAMP(DATETIME(brief_date, TIME '06:30:00'), 'Africa/Johannesburg')) = 6,
     'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('market-mornings published by 06:30 SAST: %d of 6',
         COUNTIF(first_published <= TIMESTAMP(DATETIME(brief_date, TIME '06:30:00'), 'Africa/Johannesburg'))) AS note,
  TO_JSON_STRING(ARRAY_AGG(STRUCT(brief_date, market, first_published) ORDER BY brief_date, market)) AS mornings
FROM b
SQL

cat > "$W/g4_3.sql" <<'SQL'
-- G4.3 (IG-9, 4254bf8e): no suppressed creator_id appears in D's brief payloads
WITH s AS (
  SELECT DISTINCT creator_id FROM `ogilvy-trends-v2.intelligence_42_core.v_suppressed_creators`),
b AS (
  SELECT market, TO_JSON_STRING(payload) AS p FROM `ogilvy-trends-v2.intelligence_42_agent.briefs` WHERE brief_date = @d)
SELECT
  IF((SELECT COUNT(*) FROM s) > 0 AND (SELECT COUNT(*) FROM b) > 0
     AND (SELECT COUNT(*) FROM b JOIN s ON STRPOS(b.p, s.creator_id) > 0) = 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('suppressed creators: %d; brief rows on day: %d; payloads naming one: %d (no suppression row means nothing to prove)',
         (SELECT COUNT(*) FROM s), (SELECT COUNT(*) FROM b),
         (SELECT COUNT(*) FROM b JOIN s ON STRPOS(b.p, s.creator_id) > 0)) AS note
SQL

{
echo "# G4 brief and Today $STAMP D=$D D1=$D1"
BQ G4.1:1.12+9+5a "$W/g4_1.sql" d=$D
BQ G4.2:1.13 "$W/g4_2.sql" d=$D d1=$D1
BQ G4.3:IG-9 "$W/g4_3.sql" d=$D
py -3.13 - "$T" "$D" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import f42api_v1 as api

D = sys.argv[2]
code, today, text = api.get("/api/today", {"date": D})
if code == 401:
    for rid in ("G4.4:1.14", "G4.5:QA-4", "G4.6:UI-21", "G4.7:UI-23", "G4.8:UI-1+UI-22", "G4.9:30a-strip"):
        api.row(rid, False, "passcode refused (401); stop and re-enter it")
    sys.exit(0)
api.row("G4.4:1.14", code == 200, f"/api/today?date={D} -> {code}, status {(today or {}).get('status')}")
markets = (today or {}).get("markets") or []

dropped = [i for m in markets for i in (m.get("dropped") or {}).get("items") or [] if i.get("reason") == "held_back"]
bare = [i for i in dropped if i.get("reason_text") == "Held back"]
api.row("G4.5:QA-4", bool(dropped) and not bare,
        f"held_back dropped items {len(dropped)}, bare 'Held back' {len(bare)}" + ("" if dropped else " (no case today)"))

old = [w for w in ("New to 42", "Check pattern") if w in (text or "")]
api.row("G4.6:UI-21", code == 200 and not old, f"old words found: {old or 'none'}")

series = [w for w in ("TikTok local feed", "X hub accounts") if w in (text or "")]
api.row("G4.7:UI-23", code == 200 and not series, f"old series names found: {series or 'none'}")

any_cards = any((m.get("cards") or []) + (m.get("more") or []) for m in markets)
heading = (today or {}).get("heading") or ""
api.row("G4.8:UI-1+UI-22", code == 200 and heading.startswith("Taking off") == any_cards,
        f"heading {heading!r}, any cards {any_cards}")

api.row("G4.9:30a-strip", code == 200 and bool(markets) and all("breaking" in m for m in markets),
        f"markets with a breaking key: {sum('breaking' in m for m in markets)} of {len(markets)}; "
        f"lines: {sum(len(m.get('breaking') or []) for m in markets)} (empty while the pulse is off)")
PY
} >> "$R" 2>&1
echo "Receipt: $R"; grep -a '^ROW ' "$R"
)
```

---

## G5: API routes per page (and the served bundle)

```bash
( set -C
P=ogilvy-trends-v2; REG=us-central1
D=${D:-$(date -u -d '+2 hours' +%F)}
STAMP=$(date -u +%Y%m%dT%H%M%SZ); RD="$HOME/dev/42-receipts"; T="$RD/tools-20261002"
R="$RD/postdeploy-G5-pages-$STAMP.txt"
[ -e "$T/f42api_v1.py" ] || { echo "Run section 0 first."; exit 1; }
: > "$R" || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
API_URL=$(gcloud run services describe f42-api --project=$P --region=$REG --format='value(status.url)' 2>/dev/null); export API_URL
[ -n "${F42_PASSCODE:-}" ] || { read -rsp 'Staging passcode (hidden, not stored): ' F42_PASSCODE; echo; }
export F42_PASSCODE
{
echo "# G5 pages $STAMP D=$D"
py -3.13 - "$T" "$D" <<'PY'
import re
import sys
import urllib.request

sys.path.insert(0, sys.argv[1])
import os
import f42api_v1 as api

D = sys.argv[2]
code, today, _ = api.get("/api/today", {"date": D})
if code == 401:
    print("ROW G5: NOT PROVEN (passcode refused (401); stop and re-enter it)")
    sys.exit(0)
markets = (today or {}).get("markets") or []
picks = []  # (item_id, market) from Today: cards first, then held items
for m in markets:
    for c in (m.get("cards") or []) + (m.get("more") or []) + ((m.get("held_back") or {}).get("items") or []):
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(c.get("item_id") or "")):
            picks.append((c["item_id"], m["market"]))
picks = list(dict.fromkeys(picks))[:5]
print("# sampled items:", picks)

# G5.1 (2.8, 10) Discover and Radar
disc = {}
for mk in ("ZA", "NG", "KE"):
    disc[mk] = api.get("/api/discover", {"market": mk})
rc, _, _ = api.get("/api/discover/radar", {"market": "ZA"})
codes = {mk: v[0] for mk, v in disc.items()}
api.row("G5.1:2.8+10", all(c == 200 for c in codes.values()) and rc == 200, f"discover {codes}, radar {rc}")

# G5.3 (QA-10) lifecycle rule in plain words (core/api/discover.py LIFECYCLE)
rules = ("New or returning, with 5 or more creators and 8 posts in 3 days",
         "Clearly up, at least twice its usual level, on 2 days, or on 1 day plus another platform or market",
         "Growth slowing while posting stays near its high", "Reached large creators and news, or 3 or more platforms",
         "At or below 60% of its peak on each of the last 3 days")
dtext = " ".join(v[2] or "" for v in disc.values())
api.row("G5.3:QA-10", any(r in dtext for r in rules), f"plain lifecycle sentences seen: {sum(r in dtext for r in rules)}")

# G5.4, G5.5 topic pages for Today items
topics = {}
for item, mk in picks:
    topics[(item, mk)] = api.get(f"/api/topics/{item}", {"market": mk})
ev = [(k, v[1].get("evidence")) for k, v in topics.items() if v[0] == 200 and isinstance(v[1], dict)]
api.row("G5.4:QA-13", any(e for _, e in ev),
        f"topics 200: {len(ev)} of {len(topics)}; with evidence posts: {sum(1 for _, e in ev if e)}")
keys_ok = all(all(k in v[1] for k in ("spread", "origin", "news")) for v in topics.values() if v[0] == 200 and v[1])
filled = sum(1 for v in topics.values() if v[0] == 200 and v[1] and (v[1].get("origin") or v[1].get("news") or v[1].get("spread")))
api.row("G5.5:2.3+12", bool(ev) and keys_ok and filled > 0,
        f"topics with spread/origin/news keys: {keys_ok}; with any of them filled: {filled} of {len(ev)}")

# G5.6 (3.1, 19) Compare across markets for one Today item
cc = 0
if picks:
    cc, cmp_, ctext = api.get("/api/compare", {"mode": "markets", "items": picks[0][0], "markets": "ZA,NG,KE"})
else:
    cmp_, ctext = None, ""
api.row("G5.6:3.1+19", cc == 200, f"compare markets for {picks[0][0] if picks else 'no item'} -> {cc}")

# G5.2 (UI-8) held reasons are plain on Discover, topic, Compare and alerts (core/api/held_words.py)
raw = re.compile(r"Data issue: \d+ of the last 3 market-days invalid|Found by search: seen only in|Market unconfirmed: "
                 r"source market evidence|Global: \d+ of \d+ card source posts|Paid-led: |Not assessed: political item")
ac, alerts, atext = api.get("/api/alerts")
bodies = [v[1] for v in disc.values()] + [v[1] for v in topics.values()] + [cmp_, alerts]
reasons = [v for b in bodies for k, v in api.walk(b) if k == "reason_text" and isinstance(v, str)]
rawhits = [r for r in reasons if raw.search(r)]
api.row("G5.2:UI-8", bool(reasons) and not rawhits, f"reason_text seen {len(reasons)}, raw gate text {rawhits[:3] or 'none'}")

# G5.7 (IG-9, 539e253d, a10438b4) suppressed creators off the pages
sc, sup, _ = api.get("/api/suppressions")
ids = sorted({r.get("creator_id") for r in (sup or {}).get("suppressions") or []
              if r.get("creator_id") and r.get("status") != "lifted"})
hc, _, htext = api.get("/api/history/search", {"q": picks[0][0] if picks else "a"})
pages = " ".join(v[2] or "" for v in disc.values()) + " ".join(v[2] or "" for v in topics.values()) + (ctext or "") + (htext or "")
found = [i for i in ids if i in pages]
api.row("G5.7:IG-9+3.1+19", sc == 200 and bool(ids) and not found,
        f"suppression list {sc}, active ids {len(ids)}, found on pages {len(found)}" + ("" if ids else " (no suppression row: nothing to prove)"))

# G5.8 (QA-15) saved ask opens from History
hc, hist, _ = api.get("/api/history/asks", {"limit": 5})
asks = (hist or {}).get("asks") or []
oc = api.get(f"/api/ask/{asks[0]['ask_id']}")[0] if asks else 0
api.row("G5.8:QA-15", oc == 200, f"history asks {hc} ({len(asks)}), newest ask -> {oc}")

# G5.9 to G5.11 Coverage
cvc, cov, cvtext = api.get("/api/coverage", {"date": D})
api.row("G5.9:QA-16", cvc == 200 and "SocialCrawl" not in (cvtext or ""), f"coverage {cvc}, names SocialCrawl: {'SocialCrawl' in (cvtext or '')}")
api.row("G5.10:QA-17", cvc == 200 and "Google Trends (coming)" not in ((cov or {}).get("not_seen") or []),
        f"not_seen {((cov or {}).get('not_seen'))} (passes only if da500be3 is carried)")
api.row("G5.11:17+2.7", cvc == 200 and bool((cov or {}).get("scorecard")),
        f"scorecard {'filled' if (cov or {}).get('scorecard') else 'empty'}; text {(cov or {}).get('scorecard_text')!r} (after Mon 5 Oct)")

# G5.12-off (31, 35) T2 off on the open health route
hc, health, _ = api.get("/api/health", gated=False)
api.row("G5.12-off:31+35", hc == 200 and (health or {}).get("t2_ready") is False, f"t2_ready {(health or {}).get('t2_ready')}")

# G5.14 the served bundle (open /assets): strings that only the release build has or lacks
base = os.environ["API_URL"].rstrip("/")
seen, queue, bundle = set(), ["/"], ""
while queue and len(seen) < 60:
    path = queue.pop(0)
    if path in seen:
        continue
    seen.add(path)
    try:
        with urllib.request.urlopen(base + path, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        print(f"# bundle {path} failed {type(exc).__name__}")
        continue
    bundle += body
    queue += ["/" + p.lstrip("/") for p in re.findall(r"/?assets/[\w.\-]+\.js", body)]
    queue += ["/assets/" + p for p in re.findall(r"[\"'`]\./([\w.\-]+\.js)[\"'`]", body)]
print(f"# bundle files read: {len(seen)}, characters: {len(bundle)}")
has = lambda s: s in bundle  # noqa: E731
ok_read = len(seen) > 1
api.row("G5.14:30a-strip", ok_read and has("Breaking in the last few hours"), "release marker string present")
api.row("G5.14:QA-2", ok_read and not has("Trend numbers were not stored"), "old held-item sentence absent")
api.row("G5.14:QA-7", ok_read and not has("Not ready yet"), "'Not ready yet' absent")
api.row("G5.14:UI-12", ok_read and not has("Pick a source under a claim"), "empty source-panel prompt absent")
api.row("G5.14:UI-21", ok_read and not has("New to 42"), "'New to 42' absent")
api.row("G5.14:U2", ok_read and has("The check detail could not be read just now."),
        "U2 sentence present (passes only if fix/held-detail-unavailable-20261002 602f0334 is carried)")
PY
} >> "$R" 2>&1
echo "Receipt: $R"; grep -a '^ROW ' "$R"
)
```

---

## G6: Ask records (meaningful only after a funded Ask on the release)

```bash
( set -C
P=ogilvy-trends-v2; REG=us-central1
STAMP=$(date -u +%Y%m%dT%H%M%SZ); RD="$HOME/dev/42-receipts"; T="$RD/tools-20261002"
R="$RD/postdeploy-G6-ask-$STAMP.txt"; W="$RD/postdeploy-G6-work-$STAMP"
[ -e "$T/bqcheck_v1.py" ] || { echo "Run section 0 first."; exit 1; }
mkdir "$W" || exit 1; : > "$R" || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
BQ() { py -3.13 "$T/bqcheck_v1.py" "$@"; }
REV=$(gcloud run services describe f42-agent --project=$P --region=$REG --format='value(status.latestReadyRevisionName)' 2>/dev/null)
SINCE=$(gcloud run revisions describe "$REV" --project=$P --region=$REG --format='value(metadata.creationTimestamp)' 2>/dev/null)
SINCE=${SINCE:-2026-10-02T00:00:00Z}; SD=$(date -u -d "${SINCE%%T*} -1 day" +%F)

cat > "$W/a.sql" <<'SQL'
-- G6.1 (1.14 Ask, FEATURES 2 stored log): asks since the deploy, complete ones with a stored step log
WITH a AS (
  SELECT run_id, status, tier, outcome, started_at, run_date, seconds, model_usd,
    ARRAY_LENGTH(JSON_QUERY_ARRAY(record, '$.steps')) AS steps
  FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
  WHERE run_date >= @sd AND stage = 'ask' AND started_at >= @since)
SELECT
  IF(COUNTIF(status = 'complete' AND steps > 0) > 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('asks since deploy %d; complete with steps %d; failed %d (none means no funded Ask yet)', COUNT(*),
         COUNTIF(status = 'complete' AND steps > 0), COUNTIF(status = 'failed')) AS note,
  TO_JSON_STRING(ARRAY_AGG(STRUCT(run_id, status, tier, outcome, started_at, seconds, model_usd, steps) ORDER BY started_at DESC LIMIT 20)) AS asks
FROM a
SQL

cat > "$W/b.sql" <<'SQL'
-- G6.2-off (31, 35): no T2 Ask ran since the deploy while F42_T2_READY is off
SELECT IF(COUNT(*) = 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('T2 ask rows since deploy: %d', COUNT(*)) AS note
FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
WHERE run_date >= @sd AND stage = 'ask' AND started_at >= @since AND tier = 'T2'
SQL

cat > "$W/c.sql" <<'SQL'
-- G6.3 (U4, only if b31c6602 is carried): an Ask row's run_date is the SAST date of its start
SELECT
  IF(COUNT(*) > 0 AND COUNTIF(run_date != DATE(started_at, 'Africa/Johannesburg')) = 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('asks %d; run_date off the SAST day %d', COUNT(*), COUNTIF(run_date != DATE(started_at, 'Africa/Johannesburg'))) AS note
FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
WHERE run_date >= @sd AND stage = 'ask' AND started_at >= @since
SQL

cat > "$W/d.sql" <<'SQL'
-- G6.4 (U1, only if fadba785/a436ee5e are carried): complete asks keep query_receipts in the runs record
SELECT
  IF(COUNTIF(status = 'complete') > 0 AND COUNTIF(status = 'complete' AND JSON_QUERY(record, '$.query_receipts') IS NULL) = 0,
     'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('complete asks %d; without query_receipts %d', COUNTIF(status = 'complete'),
         COUNTIF(status = 'complete' AND JSON_QUERY(record, '$.query_receipts') IS NULL)) AS note
FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
WHERE run_date >= @sd AND stage = 'ask' AND started_at >= @since
SQL

cat > "$W/e.sql" <<'SQL'
-- G6.5 (U5, only if eac2e6fd is carried): a model_unavailable Ask keeps the SDK status code
SELECT
  IF(COUNT(*) > 0 AND COUNTIF(JSON_VALUE(record, '$.error.status') IS NULL) = 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('model_unavailable asks %d; without error.status %d (none means no case to prove)', COUNT(*),
         COUNTIF(JSON_VALUE(record, '$.error.status') IS NULL)) AS note,
  TO_JSON_STRING(ARRAY_AGG(STRUCT(run_id, JSON_VALUE(record, '$.error.status') AS status,
                                  JSON_VALUE(record, '$.error.sdk_module') AS sdk_module) LIMIT 10)) AS rows_
FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
WHERE run_date >= @sd AND stage = 'ask' AND started_at >= @since
  AND JSON_VALUE(record, '$.error.error') = 'model_unavailable'
SQL

{
echo "# G6 Ask records $STAMP since=$SINCE"
BQ G6.1:1.14-ask+2 "$W/a.sql" sd=$SD since=$SINCE
BQ G6.2-off:31+35 "$W/b.sql" sd=$SD since=$SINCE
BQ G6.3:U4 "$W/c.sql" sd=$SD since=$SINCE
BQ G6.4:U1 "$W/d.sql" sd=$SD since=$SINCE
BQ G6.5:U5 "$W/e.sql" sd=$SD since=$SINCE
} >> "$R" 2>&1
echo "Receipt: $R"; grep -a '^ROW ' "$R"
)
```

---

## G7: alerts and watches

```bash
( set -C
P=ogilvy-trends-v2; REG=us-central1
D=${D:-$(date -u -d '+2 hours' +%F)}
STAMP=$(date -u +%Y%m%dT%H%M%SZ); RD="$HOME/dev/42-receipts"; T="$RD/tools-20261002"
R="$RD/postdeploy-G7-alerts-$STAMP.txt"; W="$RD/postdeploy-G7-work-$STAMP"
[ -e "$T/f42api_v1.py" ] || { echo "Run section 0 first."; exit 1; }
mkdir "$W" || exit 1; : > "$R" || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
BQ() { py -3.13 "$T/bqcheck_v1.py" "$@"; }
API_URL=$(gcloud run services describe f42-api --project=$P --region=$REG --format='value(status.url)' 2>/dev/null); export API_URL
REV=$(gcloud run services describe f42-api --project=$P --region=$REG --format='value(status.latestReadyRevisionName)' 2>/dev/null)
SINCE=$(gcloud run revisions describe "$REV" --project=$P --region=$REG --format='value(metadata.creationTimestamp)' 2>/dev/null)
SINCE=${SINCE:-2026-10-02T00:00:00Z}; SD=$(date -u -d "${SINCE%%T*} -1 day" +%F)
[ -n "${F42_PASSCODE:-}" ] || { read -rsp 'Staging passcode (hidden, not stored): ' F42_PASSCODE; echo; }
export F42_PASSCODE

cat > "$W/g7_1.sql" <<'SQL'
-- G7.1 (2.9, 16): detect's watch step ran on D and watch_matches rows were appended
WITH r AS (
  SELECT run_id, counts FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
  WHERE run_date = @d AND stage = 'detect' ORDER BY finished_at DESC LIMIT 1)
SELECT
  IF((SELECT COUNT(*) FROM `ogilvy-trends-v2.intelligence_42_agent.watch_matches` WHERE match_date = @d) > 0,
     'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('detect %s watch %s; watch_matches on day %d; active watches %d', IFNULL(r.run_id, 'none'),
         IFNULL(TO_JSON_STRING(JSON_QUERY(r.counts, '$.watch')), 'null'),
         (SELECT COUNT(*) FROM `ogilvy-trends-v2.intelligence_42_agent.watch_matches` WHERE match_date = @d),
         (SELECT COUNT(*) FROM `ogilvy-trends-v2.intelligence_42_agent.v_watches_current` WHERE status = 'active')) AS note
FROM (SELECT 1 AS one) AS x LEFT JOIN r ON TRUE
SQL

cat > "$W/g7_2.sql" <<'SQL'
-- G7.2 (2.9 breakout rule, ddf5a526): an active watch with rule {"breakout": true} matched on D
WITH w AS (
  SELECT watch_id FROM `ogilvy-trends-v2.intelligence_42_agent.v_watches_current`
  WHERE status = 'active' AND JSON_VALUE(rule, '$.breakout') = 'true')
SELECT
  IF((SELECT COUNT(*) FROM `ogilvy-trends-v2.intelligence_42_agent.watch_matches`
      WHERE match_date = @d AND watch_id IN (SELECT watch_id FROM w)) > 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('breakout watches %d; their matches on day %d (no breakout watch means creating one first, a write)',
         (SELECT COUNT(*) FROM w),
         (SELECT COUNT(*) FROM `ogilvy-trends-v2.intelligence_42_agent.watch_matches`
          WHERE match_date = @d AND watch_id IN (SELECT watch_id FROM w))) AS note
SQL

cat > "$W/g7_3.sql" <<'SQL'
-- G7.3 (watch-tone-surge a67b55a9, only if carried): v_item_tone_daily exists
SELECT IF(COUNT(*) = 1, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('v_item_tone_daily present: %d (expected 0 while feat/watch-tone-surge-20261002 is not carried)', COUNT(*)) AS note
FROM `ogilvy-trends-v2.intelligence_42_agent.INFORMATION_SCHEMA.TABLES`
WHERE table_name = 'v_item_tone_daily'
SQL

cat > "$W/g7_5.sql" <<'SQL'
-- G7.5-off (2.9 digest, 20ab75c2): no digest runs row since the deploy while DIGEST_READY is False
SELECT IF(COUNT(*) = 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('digest rows since deploy: %d', COUNT(*)) AS note
FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
WHERE run_date >= @sd AND stage = 'digest' AND started_at >= @since
SQL

{
echo "# G7 alerts and watches $STAMP D=$D since=$SINCE"
BQ G7.1:2.9+16 "$W/g7_1.sql" d=$D
BQ G7.2:2.9-breakout "$W/g7_2.sql" d=$D
BQ G7.3:tone-surge "$W/g7_3.sql"
BQ G7.5-off:2.9-digest "$W/g7_5.sql" sd=$SD since=$SINCE
py -3.13 - "$T" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import f42api_v1 as api
ac, alerts, atext = api.get("/api/alerts")
if ac == 401:
    print("ROW G7.4:16: NOT PROVEN (passcode refused (401))")
    sys.exit(0)
wc, _, _ = api.get("/api/watches")
old = [w for w in ("New to 42", "Check pattern") if w in (atext or "")]
api.row("G7.4:16", ac == 200 and wc == 200, f"alerts {ac} ({len((alerts or {}).get('alerts') or [])} alerts, "
        f"{len((alerts or {}).get('waiting') or [])} waiting), watches {wc}")
api.row("G7.4:UI-21", ac == 200 and not old, f"old words in alerts: {old or 'none'}")
PY
} >> "$R" 2>&1
echo "Receipt: $R"; grep -a '^ROW ' "$R"
)
```

---

## G8: forecasts and learn

```bash
( set -C
D=${D:-$(date -u -d '+2 hours' +%F)}
WK=$(date -u -d "$D -$(( $(date -u -d "$D" +%u) + 6 )) days" +%F)
STAMP=$(date -u +%Y%m%dT%H%M%SZ); RD="$HOME/dev/42-receipts"; T="$RD/tools-20261002"
R="$RD/postdeploy-G8-learn-$STAMP.txt"; W="$RD/postdeploy-G8-work-$STAMP"
[ -e "$T/bqcheck_v1.py" ] || { echo "Run section 0 first."; exit 1; }
mkdir "$W" || exit 1; : > "$R" || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
BQ() { py -3.13 "$T/bqcheck_v1.py" "$@"; }

cat > "$W/g8_1.sql" <<'SQL'
-- G8.1 (8): engine forecasts issued on D, and detect's forecast step not failed
WITH r AS (
  SELECT run_id, counts FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
  WHERE run_date = @d AND stage = 'detect' ORDER BY finished_at DESC LIMIT 1),
f AS (
  SELECT target, rule, COUNT(*) AS n FROM `ogilvy-trends-v2.intelligence_42_agent.forecasts`
  WHERE issue_date = @d GROUP BY target, rule)
SELECT
  IF(IFNULL((SELECT SUM(n) FROM f), 0) > 0 AND IFNULL(JSON_VALUE(r.counts, '$.forecast.status'), '') != 'failed',
     'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('forecast rows issued on day %d; detect forecast %s', IFNULL((SELECT SUM(n) FROM f), 0),
         IFNULL(TO_JSON_STRING(JSON_QUERY(r.counts, '$.forecast')), 'null')) AS note,
  (SELECT TO_JSON_STRING(ARRAY_AGG(STRUCT(target, rule, n))) FROM f) AS cohorts
FROM (SELECT 1 AS one) AS x LEFT JOIN r ON TRUE
SQL

cat > "$W/g8_2.sql" <<'SQL'
-- G8.2 (7, 26): forecast_score and weekly_quality exist (agent.sql:58 and :65 at R; a schema apply is part of the release)
SELECT IF(COUNT(*) = 2, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('present: %s', IFNULL(STRING_AGG(table_name, ', '), 'none')) AS note
FROM `ogilvy-trends-v2.intelligence_42_agent.INFORMATION_SCHEMA.TABLES`
WHERE table_name IN ('forecast_score', 'weekly_quality')
SQL

cat > "$W/g8_3.sql" <<'SQL'
-- G8.3 (2.7, 17, IG-3): engine_scorecard rows for the week from an ok learn run, ZA NG KE
WITH s AS (
  SELECT s.market, s.run_id, JSON_VALUE(s.lead_time, '$.value') AS lead_value, JSON_VALUE(s.lead_time, '$.reason') AS lead_reason,
    JSON_VALUE(s.time_to_detect, '$.value') AS ttd
  FROM `ogilvy-trends-v2.intelligence_42_agent.engine_scorecard` s
  JOIN `ogilvy-trends-v2.intelligence_42_agent.runs` r ON r.run_id = s.run_id AND r.stage = 'learn' AND r.status = 'ok'
  WHERE s.week_start = @w AND r.run_date >= @w)
SELECT
  IF(COUNT(DISTINCT IF(market IN ('ZA', 'NG', 'KE'), market, NULL)) = 3, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('scorecard markets for week %t: %d of 3', @w, COUNT(DISTINCT IF(market IN ('ZA', 'NG', 'KE'), market, NULL))) AS note,
  TO_JSON_STRING(ARRAY_AGG(STRUCT(market, run_id, ttd, lead_value, lead_reason))) AS rows_
FROM s
SQL

cat > "$W/g8_4.sql" <<'SQL'
-- G8.4 (26): forecast_score rows for the week and the learn run's forecast_score counts
WITH l AS (
  SELECT run_id, status, counts FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
  WHERE run_date = @w AND stage = 'learn' ORDER BY finished_at DESC LIMIT 1)
SELECT
  IF((SELECT COUNT(*) FROM `ogilvy-trends-v2.intelligence_42_agent.forecast_score` WHERE week_start = @w) > 0,
     'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('forecast_score rows %d; learn %s %s forecast_score %s',
         (SELECT COUNT(*) FROM `ogilvy-trends-v2.intelligence_42_agent.forecast_score` WHERE week_start = @w),
         IFNULL(l.run_id, 'none'), IFNULL(l.status, '-'),
         IFNULL(TO_JSON_STRING(JSON_QUERY(l.counts, '$.forecast_score')), 'null')) AS note
FROM (SELECT 1 AS one) AS x LEFT JOIN l ON TRUE
SQL

cat > "$W/g8_5.sql" <<'SQL'
-- G8.5 (7): weekly_quality rows for ALL, ZA, NG, KE for the week
SELECT
  IF(COUNT(DISTINCT IF(market IN ('ALL', 'ZA', 'NG', 'KE'), market, NULL)) = 4, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('weekly_quality markets for week %t: %d of 4', @w, COUNT(DISTINCT IF(market IN ('ALL', 'ZA', 'NG', 'KE'), market, NULL))) AS note,
  TO_JSON_STRING(ARRAY_AGG(STRUCT(market, run_id, score, counted, notes) LIMIT 8)) AS rows_
FROM `ogilvy-trends-v2.intelligence_42_agent.weekly_quality`
WHERE week_start = @w
SQL

cat > "$W/g8_6.sql" <<'SQL'
-- G8.6 (2.7 reference events dfa507a8, only if carried): lead time has a value in some market
SELECT
  IF(COUNTIF(JSON_VALUE(lead_time, '$.value') IS NOT NULL) > 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('rows %d; lead_time with a value %d; reasons %s', COUNT(*), COUNTIF(JSON_VALUE(lead_time, '$.value') IS NOT NULL),
         IFNULL(STRING_AGG(DISTINCT JSON_VALUE(lead_time, '$.reason')), 'none')) AS note
FROM `ogilvy-trends-v2.intelligence_42_agent.engine_scorecard`
WHERE week_start = @w
SQL

{
echo "# G8 forecasts and learn $STAMP D=$D week_start=$WK"
BQ G8.1:8 "$W/g8_1.sql" d=$D
BQ G8.2:7+26 "$W/g8_2.sql"
BQ G8.3:2.7+17+IG-3 "$W/g8_3.sql" w=$WK
BQ G8.4:26 "$W/g8_4.sql" w=$WK
BQ G8.5:7 "$W/g8_5.sql" w=$WK
BQ G8.6:2.7-ref-events "$W/g8_6.sql" w=$WK
} >> "$R" 2>&1
echo "Receipt: $R"; grep -a '^ROW ' "$R"
)
```

---

## G9: costs and caps

```bash
( set -C
P=ogilvy-trends-v2; REG=us-central1
D=${D:-$(date -u -d '+2 hours' +%F)}; D1=$(date -u -d "$D -1 day" +%F)
STAMP=$(date -u +%Y%m%dT%H%M%SZ); RD="$HOME/dev/42-receipts"; T="$RD/tools-20261002"
R="$RD/postdeploy-G9-costs-$STAMP.txt"; W="$RD/postdeploy-G9-work-$STAMP"
[ -e "$T/bqcheck_v1.py" ] || { echo "Run section 0 first."; exit 1; }
mkdir "$W" || exit 1; : > "$R" || exit 1
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
BQ() { py -3.13 "$T/bqcheck_v1.py" "$@"; }
API_URL=$(gcloud run services describe f42-api --project=$P --region=$REG --format='value(status.url)' 2>/dev/null); export API_URL

cat > "$W/g9_1.sql" <<'SQL'
-- G9.1 (6, IG-10, 14, 30a): SocialCrawl credits by share on D within caps.yaml (job = the client's share)
WITH l AS (
  SELECT job, SUM(credits_charged) AS credits, COUNT(*) AS rows_
  FROM `ogilvy-trends-v2.intelligence_42_core.credit_ledger`
  WHERE trend_date = @d GROUP BY job),
cap AS (
  SELECT * FROM UNNEST([STRUCT('collect' AS job, 1000.0 AS cap), ('confirm', 150.0), ('reserve', 50.0), ('ask', 600.0),
                        ('video', 200.0), ('pulse', 0.0), ('build', 600.0), ('eval', 100.0)]))
SELECT
  IF(COUNTIF(l.credits > IFNULL(cap.cap, 0)) = 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('over cap: %s; total %.1f', IFNULL(STRING_AGG(IF(l.credits > IFNULL(cap.cap, 0),
         FORMAT('%s %.1f > %.0f', l.job, l.credits, IFNULL(cap.cap, 0)), NULL), ', '), 'none'), IFNULL(SUM(l.credits), 0)) AS note,
  TO_JSON_STRING(ARRAY_AGG(STRUCT(l.job, ROUND(l.credits, 1) AS credits, l.rows_, cap.cap) ORDER BY l.job)) AS shares
FROM l LEFT JOIN cap USING (job)
SQL

cat > "$W/g9_2.sql" <<'SQL'
-- G9.2 (NA-1): model spend on D within MODEL_DAILY_USD (80 on 1-2 Oct, 20 from 3 Oct; core/config/caps.yaml)
SELECT
  IF(IFNULL(SUM(model_usd), 0) <= IF(@d BETWEEN DATE '2026-10-01' AND DATE '2026-10-02', 80, 20), 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('model_usd on day %.2f of %d', IFNULL(SUM(model_usd), 0), IF(@d BETWEEN DATE '2026-10-01' AND DATE '2026-10-02', 80, 20)) AS note,
  TO_JSON_STRING(ARRAY_AGG(STRUCT(stage, model_usd) ORDER BY model_usd DESC LIMIT 15)) AS top_rows
FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
WHERE run_date = @d AND model_usd IS NOT NULL
SQL

cat > "$W/g9_4.sql" <<'SQL'
-- G9.4 (6): the nightly reconcile for D-1 finished ok
SELECT IF(COUNTIF(status = 'ok') > 0, 'PASS', 'NOT PROVEN') AS verdict,
  FORMAT('reconcile rows for %t: ok %d, other %d', @d1, COUNTIF(status = 'ok'), COUNTIF(status != 'ok')) AS note
FROM `ogilvy-trends-v2.intelligence_42_agent.runs`
WHERE run_date = @d1 AND stage = 'reconcile'
SQL

{
echo "# G9 costs and caps $STAMP D=$D"
BQ G9.1:6+IG-10+14 "$W/g9_1.sql" d=$D
BQ G9.2:NA-1 "$W/g9_2.sql" d=$D
BQ G9.4:6-reconcile "$W/g9_4.sql" d1=$D1
py -3.13 - "$T" "$D" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import f42api_v1 as api
D = sys.argv[2]
hc, h, _ = api.get("/api/health", gated=False)
cap = (h or {}).get("model_daily_cap_usd")
want = 80.0 if D in ("2026-10-01", "2026-10-02") else 20.0
api.row("G9.3:NA-1-cap", hc == 200 and cap == want, f"health model_daily_cap_usd {cap}, want {want} for {D}")
PY
} >> "$R" 2>&1
echo "Receipt: $R"; grep -a '^ROW ' "$R"
)
```

---

## Rows with no read check, and why

A read cannot prove these. Where a later read helps, it is named.

| Row | Why no read proves it | What does |
|---|---|---|
| 1.8 | The recorded four-language search runs `tvf_search_posts`, which calls the embedding model (spend), as f42-builder | One recorded run per language by L3 |
| 1.11, 1, 3, nationality 6adaee17 (R) | Needs a funded ZA, NG, KE Ask and the L5 reviewer's verdict | Albert's funded go inside the USD 20 cap; G6 reads the rows after |
| 1.13 (second half) | A forced detect failure is a deliberate failed run (a write) | L1 runs it once with Albert's go; G4.2 then reads the banner morning |
| 1.15, 2.8, 3.1, 10, 19 (journeys) | Browser journeys behind the passcode | Albert unlocks the browser on his PC; G5 covers only the API side |
| 1.16, 1.18, 1.20, Op, 5a "three mornings" | Data days (3 good mornings, replay set) and named reviewers | Calendar time, then G4.1 on each morning |
| 1.17, ask-comment-transcript-parse add6fbb1 (R) | Needs a funded T2 Ask that calls get_comments or get_transcript, and T2 is off | Albert switches T2 on and funds one T2 Ask |
| 1.19, 11 (Rising) | 14 valid observed days (about 12-13 Oct), backtest `--apply`, Albert's sign-off | Data days; G3.9 shows the states meanwhile |
| 2.1, 2.2 (the BUILD checks) | 200-post hand check and the discovery review are human marks | A reviewer (G3.1 and G3.2 only show the steps ran) |
| 2.6, 14 (the BUILD check) | 20 clips hand-checked | A reviewer (G3.3 shows clips were read) |
| 2.9, 16 (test watch) | Creating a test watch is a write; a real rising item needs data days; the digest needs Albert's GMAIL_APP_PASSWORD secret | Albert or L4 creates a watch; G7.1 and G7.2 then read the matches |
| 2.10 (second half) | A forced zero-rows run is a write | Albert's go |
| 2.11, 15 | Labelled campaign library (NA-8) | Albert and colleagues |
| 2.13 | Five real breakouts reviewed by eye | A reviewer, using the G3.8 rows |
| 3.2, 20, 21 | `feat/sensitive-set-complete-20261002` is not on origin, so `v_sensitive_items_complete` cannot be named or read; NA-9 open | Push of that branch, Albert's NA-9 answer |
| 3.4, 23 | A funded T3 run (and Albert's T3 figures) | After the run: `/api/investigations?status=complete` and the investigations rows |
| 3.5, 24, IG-5, IG-6 | Freezing a dossier is a write through the app | After a freeze: GET `/api/dossiers/{id}/versions/{v}/export` returns a PDF |
| 4 | Saving a finding is a write | After a save: `/api/history/findings` |
| 27 | Native-speaker checks | NA-3 reviewers |
| 28, 32 | A spike click and a creator-fit ask are paid T1 asks | Funded asks; G6.1 reads them |
| 29, IG-7, 30, 30a, 31, 35 (switch-on) | Albert's secrets and flags (webhook, PULSE_READY, F42_T2_READY, AGENT.md yes) | Albert; the `-off` guards above show they are still off |
| IG-11 ask-model-timeout 5b121b48 (R) | Only shows when a Vertex call hangs; cannot be caused by a read | A slow-model incident, or the unit test already on the branch |
| QA-8 | Only shows when an Ask hits the model budget | A funded Ask over budget (not worth forcing) |
| promoted forecasts ba4f7c02, dccbce76 (R) | Needs an Ask that logs a forecast from a promoted cohort, and promotion needs weeks of resolved forecasts | Data weeks, then a funded Ask |
| 528c789d spike-series-names, 485e3346 api-pins (R) | Test-only and build-pin changes; nothing on staging changes | None (S0.4 health ok covers the pins) |
| U3 topic not-assessed 1290eb9c, U6 demo checker 6f81dff9 (not in R) | Front-end wording that depends on page state; the checker drives a passcode browser | A look on Albert's PC, or L4 runs the checker |
| QA-1, QA-3, QA-5, QA-6, QA-9, QA-11, QA-12, QA-14, UI-2 to UI-7, UI-9 to UI-11, UI-13 to UI-20 | Visual or console behaviour (focus ring, layout, duplicate names, 404s in the console); the bundle strings cannot show placement | A look on Albert's PC behind the passcode |

## Names I could not verify, and risks found while checking

- The release head: there is no `fix/next-release-20261002` on origin. I used `fix/next-release-merge-qifmxs` 4e9bee97. It does not carry data/reference-events-wk40 dfa507a8, the six U branches (a436ee5e, eac2e6fd, b31c6602, 602f0334, 1290eb9c, 6f81dff9), feat/watch-tone-surge a67b55a9 or fix/coverage-google-trends-wording da500be3. Those lines are marked "only if carried" and will read NOT PROVEN until they are merged.
- `feat/sensitive-set-complete-20261002` is not on origin, so `v_sensitive_items_complete` could not be verified (rows 3.2, 20, 21).
- gcloud JSON field paths (job `image`, `limits`, service `env`, execution `succeededCount`, Artifact Registry tag `tag`/`version`, logging `textPayload`) are standard gcloud output, not repo code. The G1 Python searches for them by key name rather than by a fixed path.
- G2.1 assumes local source fetches write collection_health rows under their series names (core/collect/local_sources.py:515-542 builds health per fetch). Public feeds (news_rss, board_music_country, radio_playlist) are checked through collect counts instead, because I could not confirm they write collection_health rows.
- G2.3 uses `credit_ledger.item_id` per market and day as the stand-in for "cluster" in "no cluster above 25% of expansion credits". The weekly drift report (f42-drift, Mon 5 Oct) is the formal measure.
- G2.4's age pattern is a coarse read-only screen, not the gdelt.py blocklist. A hit means a person looks, not that the code failed.
- Risk, video reading (2.6, 14): caps.yaml at R sets VIDEO_DAILY to 20 clips and 200 credits, but f42-understand is still deployed with no SocialCrawl key (deploy_jobs.py:122-123 at R passes secret False). Transcript and screen-text calls will return "SOCIALCRAWL_OGILVY_API_KEY is not set" (socialcrawl_client.py:744-746), so clips are read from frames only and the video share spends 0 credits. G3.3 and G9.1 will show it: `video.read` above 0 with video credits at 0.
- Risk, schema apply: forecast_score, weekly_quality and breaking_signals (with v_breaking_signals_current) exist only after `core/schema/apply.py --apply` runs from the release. Without that step, G3.5 and G8.2 read NOT PROVEN, the Monday learn run writes no forecast scores, and Today's Breaking read fails soft (empty strip).
