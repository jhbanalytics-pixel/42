"""The locality_v2 detect step (C4 v3 section 8): one transaction of two inserts, then write-time verification.

run_locality_step writes, for one detect run, the member rows of every item and market (locality_members.sql) and the
summary row of every key of the day's series (locality_summary.sql) in one BigQuery transaction. Both statements carry
the guard of section 8.2, so a second call for the same detect run writes nothing. The step then reads the members
and summaries back and, key by key, recounts them in Python with core/trust/locality.py verify(): only a key that
agrees on every count, the digest and the status gets a row in item_locality_verified, and both views require that
row. A key that fails is, to every consumer, a missing row, and the step raises LocalityFailed after verifying the
others. A call for a detect run whose rows are already written verifies the keys that are not yet verified, so a
crash between the commit and the verification writes does not strand the run (ruling finding F5).

The step knows nothing of admission: whether the result controls anything is LOCALITY_AUTHORITY and the readers.
"""

import time
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import bigquery

from core.trust.locality import METRIC_VERSION, verify

from . import sqlrun

SQL = Path(__file__).parent / "sql"
SCHEMA_VERSION = 1

# A ceiling on the bytes one transaction may bill, so a runaway scan is refused and not paid for. The two statements
# have not had a dry run (C4 v3 section 27): this is a refusal ceiling, 10 GiB, to be replaced by the measured figure
# with headroom after the first dry run. The step refuses to run without a cap.
MAX_BYTES_BILLED = 10 * 1024 ** 3
JOB_TIMEOUT_MS = 600_000

_KEY = "run_date = @d AND detect_run_id = @detect_run_id AND metric_version = @metric_version"
MEMBERS_SQL = ("SELECT market, item_id, post_id, creator_key, locality_class, feed_sighted, geo_market, "
               "geo_confidence, geo_source "
               f"FROM {{core}}.item_locality_post WHERE {_KEY}")
SUMMARY_SQL = f"SELECT * FROM {{core}}.item_locality WHERE {_KEY}"
VERIFIED_SQL = f"SELECT DISTINCT market, item_id FROM {{core}}.item_locality_verified WHERE {_KEY}"
SERIES_WITHOUT_ROW_SQL = (
    "SELECT COUNT(*) n FROM (SELECT st.item_id, st.market FROM {core}.v_series_test_current st "
    "WHERE st.metric_date = @d AND st.market IN ('ZA', 'NG', 'KE') "
    "EXCEPT DISTINCT SELECT l.item_id, l.market FROM {core}.item_locality l WHERE l.run_date = @d "
    "AND l.detect_run_id = @detect_run_id AND l.metric_version = @metric_version)")

# Daily shadow comparison (section 10): detect's geo_status against v2's checked status and label for the run, and
# the 20 items with the widest breadth among those where the two statuses differ. Read only; nothing reads it back.
CROSSTAB_SQL = (
    "SELECT s.market, s.geo_status v1_status, l.checked_status v2_status, l.checked_label v2_label, COUNT(*) items "
    "FROM {core}.item_state s JOIN {core}.v_item_locality_checked l "
    "ON l.run_date = s.metric_date AND l.item_id = s.item_id AND l.market = s.market AND l.detect_run_id = s.run_id "
    "WHERE s.metric_date = @d AND s.run_id = @detect_run_id GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4")
DISAGREE_SQL = (
    "SELECT s.market, s.item_id, s.geo_status v1_status, s.geo_known_posts7 v1_known, s.local_share v1_share, "
    "l.checked_status v2_status, l.checked_label v2_label, l.known_posts v2_known, l.local_posts v2_local, "
    "l.breadth_creators FROM {core}.item_state s JOIN {core}.v_item_locality_checked l "
    "ON l.run_date = s.metric_date AND l.item_id = s.item_id AND l.market = s.market AND l.detect_run_id = s.run_id "
    "WHERE s.metric_date = @d AND s.run_id = @detect_run_id AND s.geo_status != l.checked_status "
    "ORDER BY l.breadth_creators DESC, s.item_id LIMIT 20")


class LocalityFailed(Exception):
    """One or more keys did not verify. counts carries the step's counts, refused the first keys that failed."""

    def __init__(self, message, counts):
        super().__init__(message)
        self.counts = counts


def _cutoff(detect):
    started = detect.started_at
    if isinstance(started, str):
        started = datetime.fromisoformat(started.replace("Z", "+00:00"))
    return started if started.tzinfo else started.replace(tzinfo=timezone.utc)


def _script(core, agent):
    members = sqlrun.render((SQL / "locality_members.sql").read_text(encoding="utf-8"), core, agent)
    summary = sqlrun.render((SQL / "locality_summary.sql").read_text(encoding="utf-8"), core, agent)
    return f"BEGIN TRANSACTION;\n{members.strip().rstrip(';')};\n{summary.strip().rstrip(';')};\nCOMMIT TRANSACTION;"


def run_locality_step(client, d, detect, core=sqlrun.CORE, agent=sqlrun.AGENT, *, max_bytes=MAX_BYTES_BILLED):
    """Write and verify the locality_v2 rows of one detect run. Returns the step's counts. Raises ValueError when no
    byte cap is set, LocalityFailed when a key fails verification, and lets any other error through."""
    if not max_bytes:
        raise ValueError("the locality step refuses to run without maximum_bytes_billed")
    started = time.monotonic()
    cutoff = _cutoff(detect)
    params = {"d": d, "detect_run_id": detect.run_id, "metric_version": METRIC_VERSION}
    existing = sqlrun.query(client, "SELECT COUNT(*) n FROM {core}.item_locality WHERE " + _KEY, params, core=core,
                            agent=agent)[0]["n"]
    bytes_billed = None
    if not existing:
        config = bigquery.QueryJobConfig(
            query_parameters=[sqlrun._param("d", d), sqlrun._param("cutoff", cutoff),
                              sqlrun._param("detect_run_id", detect.run_id), sqlrun._param("metric_version", METRIC_VERSION)],
            maximum_bytes_billed=max_bytes, job_timeout_ms=JOB_TIMEOUT_MS)
        job = client.query(_script(core, agent), job_config=config)
        job.result()
        bytes_billed = getattr(job, "total_bytes_billed", None)
    summaries = sqlrun.query(client, SUMMARY_SQL, params, core=core, agent=agent)
    members = {}
    for m in sqlrun.query(client, MEMBERS_SQL, params, core=core, agent=agent):
        members.setdefault((m["market"], m["item_id"]), []).append(m)
    done = {(r["market"], r["item_id"]) for r in sqlrun.query(client, VERIFIED_SQL, params, core=core, agent=agent)}
    verified_now, refused = [], []
    for row in summaries:
        key = (row["market"], row["item_id"])
        if key in done:
            continue
        mine = members.get(key, [])
        if verify(row, mine):
            verified_now.append({
                "run_date": d.isoformat(), "market": row["market"], "item_id": row["item_id"],
                "detect_run_id": detect.run_id, "metric_version": METRIC_VERSION,
                "verified_at": datetime.now(timezone.utc).isoformat(), "member_rows": len(mine),
                "population_digest": row["population_digest"]})
        else:
            refused.append({"market": row["market"], "item_id": row["item_id"]})
    if verified_now:
        errors = client.insert_rows_json(f"{core}.item_locality_verified", verified_now)
        if errors:
            raise RuntimeError(f"append to {core}.item_locality_verified failed: {errors}")
    statuses = {}
    for row in summaries:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
    without = sqlrun.query(client, SERIES_WITHOUT_ROW_SQL, params, core=core, agent=agent)[0]["n"]
    counts = {
        "rows_written": 0 if existing else len(summaries), "member_rows": sum(len(v) for v in members.values()),
        "keys": len(summaries), "keys_verified": len(done) + len(verified_now), "keys_refused": len(refused),
        "refused": refused[:10], "status_counts": dict(sorted(statuses.items())),
        "series_keys_without_row": without, "duration_s": round(time.monotonic() - started, 3),
        "bytes_billed": bytes_billed}
    if refused:
        first = ", ".join(f"{r['market']}:{r['item_id']}" for r in refused[:5])
        raise LocalityFailed(f"{len(refused)} locality keys failed write-time verification, first: {first}", counts)
    return counts


def shadow_comparison(client, d, detect_run_id, core=sqlrun.CORE, agent=sqlrun.AGENT):
    """The read-only cross-tabulation of section 10: detect's geo_status against the checked v2 status and label, and
    the 20 disagreements with the widest breadth. Nothing reads the result back."""
    params = {"d": d, "detect_run_id": detect_run_id}
    return {"by_status": sqlrun.query(client, CROSSTAB_SQL, params, core=core, agent=agent),
            "disagree_top20": sqlrun.query(client, DISAGREE_SQL, params, core=core, agent=agent)}
