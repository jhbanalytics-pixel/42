"""The chain evidence producer (W8-REL-B v2.1 section 2). Read only: it reads Cloud Run job and execution descriptions and
runs fixed SELECT templates, then writes one manifest by exclusive create into the release directory.

    py -3.13 core/setup/release/chain_evidence.py --date <YYYY-MM-DD> --bindings <file> --evidence <run dir> --expect baseline|first-b

--expect says which chain this is. A baseline is the post-A a80 chain the release is measured against: the digest it must have
run on is the bound rollbackJobsDigest, and a chain that fails only DEGRADED on the PARSE_JSON signature may be recorded as a
baseline by the typed line (Q3). first-b is the first chain on Release B's image: the digest is the one FreezeJobs froze, read
only after the release manifest hash is recomputed, and it never gets the allowance.

Exit 0 when a manifest was written, qualifying or not; 1 on a contradiction (nothing written); 3 when a read could not complete
(nothing written). Every expectation comes from the bindings, baseline-J (pinned by hash) or the frozen manifest. A stage's own
rows and an execution's own description are the observations compared with them.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core.setup.release import jobs_only as jo  # noqa: E402
from core.setup.release import services_only as so  # noqa: E402
from core.setup.release.services_only import Probe, Stop, require  # noqa: E402

SCHEMA_VERSION = 1
KIND = "chain-evidence"
ROLES = ("baseline", "first-b")
DECISION = "W8-REL-B Q3"
AGENT = "`ogilvy-trends-v2.intelligence_42_agent`"
CORE = "`ogilvy-trends-v2.intelligence_42_core`"
SUBSTAGES = ("aggregate", "stats", "coaction", "breakout", "watch", "seeds", "forecast", "locality")
BRIEF_MARKETS = ("ZA", "NG", "KE")
SAST = jo.SAST
DEADLINE = jo.BRIEF_DEADLINE_SAST
SCHEDULED_COLLECT = dt.time(2, 0)
SKIPPED = "skipped_duplicate"
CODES = ("IMAGE", "WINDOW", "NOT_ONE_RUN", "TERMINAL", "BOUND", "RETRIED", "ORDER", "UPSTREAM", "SUBSTAGES", "LINEAGE", "DEGRADED",
         "SERVICES", "MANUAL")
PARSE_JSON_SIGNATURE = ("cannot round-trip through string representation", "PARSE_JSON")

# The fixed templates (2.3). Each is one SELECT, carries its partition filter, and is hashed into the bindings.
TEMPLATES = {
    "stage_rows": ("SELECT run_id, stage, status, started_at, finished_at, counts, error IS NOT NULL AS has_error, "
                   f"TO_HEX(SHA256(IFNULL(error, ''))) AS error_sha256 FROM {AGENT}.runs "
                   "WHERE run_date = @d AND stage IN UNNEST(@stages)"),
    "item_state_count": f"SELECT COUNT(*) AS n FROM {CORE}.item_state WHERE metric_date = @d AND run_id = @run_id",
    "series_test_count": f"SELECT COUNT(*) AS n FROM {CORE}.series_test WHERE metric_date = @d AND run_id = @run_id",
    "briefs_by_market": f"SELECT market, COUNT(*) AS n FROM {AGENT}.briefs WHERE brief_date = @d AND run_id = @run_id GROUP BY market",
    "item_locality_count": f"SELECT COUNT(*) AS n FROM {CORE}.item_locality WHERE run_date = @d AND detect_run_id = @run_id",
    "table_exists": f"SELECT COUNT(*) AS n FROM {CORE}.INFORMATION_SCHEMA.TABLES WHERE table_name = 'item_locality'",
}


def template_hashes():
    return {name: so.sha_text(text) for name, text in TEMPLATES.items()}


# BigQuery, behind a two method client: dry_run(sql, params) -> bytes, run(sql, params, max_bytes) -> (rows, billed bytes)

class BqRunner:
    """Runs only the fixed templates, each dry run first, refused over the cap, and keeps the record the manifest shows."""

    def __init__(self, client, cap, templates=None):
        self.client, self.cap, self.templates, self.log = client, cap, templates, []

    def run(self, name, params):
        sql = (TEMPLATES if self.templates is None else self.templates)[name]
        estimate = self.client.dry_run(sql, params)
        require(isinstance(estimate, int) and not isinstance(estimate, bool), "BYTES_CAP", "The dry run gave no byte estimate")
        require(estimate <= self.cap, "BYTES_CAP", f"The dry run of {name} is over the per query cap")
        rows, billed = self.client.run(sql, params, self.cap)
        self.log.append({"template": name, "sha256": so.sha_text(sql), "dry_run_bytes": estimate, "billed_bytes": billed, "cap": self.cap})
        return rows


class GoogleBq:
    """The real client, through the BigQuery library (never the command line tool). Not exercised offline."""

    TYPES = {"d": "DATE", "run_id": "STRING"}
    ARRAY_TYPES = {"stages": "STRING", "days": "DATE"}

    def __init__(self, timeouts):
        self.timeout = timeouts["gcloud"]

    def _job(self, sql, params, *, dry_run, max_bytes=None):
        from google.cloud import bigquery

        client = bigquery.Client(project=so.PROJECT)
        query = [bigquery.ArrayQueryParameter(k, self.ARRAY_TYPES[k], v) if isinstance(v, (list, tuple))
                 else bigquery.ScalarQueryParameter(k, self.TYPES[k], v)
                 for k, v in params.items()]
        config = bigquery.QueryJobConfig(query_parameters=query, dry_run=dry_run, use_query_cache=False,
                                         **({"maximum_bytes_billed": max_bytes} if max_bytes else {}))
        return client.query(sql, job_config=config)

    def dry_run(self, sql, params):
        try:
            return int(self._job(sql, params, dry_run=True).total_bytes_processed)
        except Exception as error:
            raise Probe("A BigQuery dry run could not complete: " + type(error).__name__) from None

    def run(self, sql, params, max_bytes):
        try:
            job = self._job(sql, params, dry_run=False, max_bytes=max_bytes)
            rows = [dict(r.items()) for r in job.result(timeout=self.timeout)]
            return rows, int(job.total_bytes_billed or 0)
        except Exception as error:
            raise Probe("A BigQuery read could not complete: " + type(error).__name__) from None


# what leaves the producer: first lines, no URL, no handle, no token

URL = re.compile(r"https?://\S+")
HANDLE = re.compile(r"(?<![\w])@\w+")
LONG_TOKEN = re.compile(r"[A-Za-z0-9_\-.]{32,}")


def clean_text(text):
    line = str(text).splitlines()[0] if str(text).strip() else ""
    line = LONG_TOKEN.sub("[redacted]", HANDLE.sub("", URL.sub("", line)))
    return re.sub(r"\s+", " ", line).strip()[:240]


def clean_counts(value):
    if isinstance(value, dict):
        return {str(k): clean_counts(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean_counts(v) for v in value]
    if isinstance(value, str):
        return clean_text(value)
    return value


def parse_counts(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        raise Stop("ROW_FORMAT", "A runs row holds counts that are not JSON") from None


def iso(value):
    return jo.aware(value).isoformat() if value is not None else None


# rows

def order_key(row):
    return (jo.aware(row["finished_at"] or row["started_at"]), row["finished_at"] is not None)


def sort_rows(rows):
    return sorted(rows, key=order_key)


def stage_runs(rows, stage):
    """({run_id: rows in FB-1 order} for the stage's non-skipped runs, oldest run first; the skipped_duplicate rows)."""
    mine = [r for r in rows if r["stage"] == stage]
    skipped = [r for r in mine if r["status"] == SKIPPED]
    runs = {}
    for row in mine:
        if row["status"] != SKIPPED:
            runs.setdefault(row["run_id"], []).append(row)
    ordered = sorted(runs.items(), key=lambda item: min(jo.aware(r["started_at"]) for r in item[1]))
    return {rid: sort_rows(group) for rid, group in ordered}, skipped


def running_row(group):
    running = [r for r in group if r["status"] == "running"]
    return running[0] if running else None


def execution_name(row):
    counts = parse_counts(row["counts"]) if row is not None else None
    name = counts.get("execution") if isinstance(counts, dict) else None
    return name if isinstance(name, str) and name else None


def degradation_of(counts):
    counts = counts if isinstance(counts, dict) else {}
    cluster = counts.get("cluster") if isinstance(counts.get("cluster"), dict) else {}
    markets = {m: v for m, v in cluster.items() if isinstance(v, dict)}
    errors = {m: str(v["error"]) for m, v in markets.items() if v.get("error")}
    return {"partial": counts.get("partial"), "partial_reason": counts.get("partial_reason"),
            "embed_error": clean_text(counts["embed_error"]) if counts.get("embed_error") else None,
            "enrich_error": clean_text(counts["enrich_error"]) if counts.get("enrich_error") else None,
            "index": counts.get("index"), "index_error": clean_text(counts["index_error"]) if counts.get("index_error") else None,
            "cluster_error_markets": list(errors), "cluster_error_first_lines": {m: clean_text(e) for m, e in errors.items()},
            "markets_net_failed": [m for m, v in markets.items() if v.get("net_failed")]}


def is_degraded(counts):
    counts = counts if isinstance(counts, dict) else {}
    cluster = counts.get("cluster") if isinstance(counts.get("cluster"), dict) else {}
    return bool(counts.get("enrich_error") or counts.get("embed_error") or counts.get("partial")
                or any(isinstance(v, dict) and v.get("error") for v in cluster.values()))


def only_parse_json_degradation(counts):
    """Q3: no enrich_error, no embed_error, no partial, and every market error's first line carries the signature."""
    counts = counts if isinstance(counts, dict) else {}
    if counts.get("enrich_error") or counts.get("embed_error") or counts.get("partial"):
        return False
    cluster = counts.get("cluster") if isinstance(counts.get("cluster"), dict) else {}
    errors = [str(v["error"]) for v in cluster.values() if isinstance(v, dict) and v.get("error")]
    return bool(errors) and all(all(word in (e.splitlines()[0] if e.strip() else "") for word in PARSE_JSON_SIGNATURE) for e in errors)


# the build

class Evidence:
    """Everything one read gathers, so the verdict is computed from the observations and the bound expectations."""

    def __init__(self, bound, reader, runner, day, role, now):
        self.bound, self.reader, self.runner, self.day, self.role, self.now = bound, reader, runner, day, role, now
        self.failures = []  # (code, stage or None)

    def fail(self, code, stage=None):
        if (code, stage) not in self.failures:
            self.failures.append((code, stage))


def build_manifest(bound, reader, bq_client, day, role, *, now=None):
    now = now or (lambda: dt.datetime.now(dt.timezone.utc))
    require(role in ROLES, "BINDINGS", "The chain role is neither baseline nor first-b")
    jo.validate_jobs_bindings(bound, producer=True)
    # A date's manifest is written once and never overwritten, so a read made before the brief's deadline of that date would keep a half
    # chain on record. The refusal writes nothing, so the same date can be read again after the deadline.
    require(now() >= dt.datetime.combine(day, DEADLINE, tzinfo=SAST), "TOO_EARLY",
            "The chain of this date is not over until the brief's 06:15 SAST deadline; a manifest is written once, so it is read after that moment")
    require(set(bound["templateHashes"]) == set(TEMPLATES) and bound["templateHashes"] == template_hashes(), "BINDINGS",
            "The template hashes the bindings bind differ from the templates this producer holds")
    baseline = jo.load_baseline_j(bound)
    expected = bound["rollbackJobsDigest"] if role == "baseline" else jo.frozen_digest(bound["releaseDir"])
    config = reader.config()
    account = config.get("core", {}).get("account")
    require(account == bound["callerAccount"] and config.get("core", {}).get("project") == so.PROJECT, "IDENTITY",
            "The CLI caller or project differs from the bound caller")
    impersonation = config.get("auth", {}).get("impersonate_service_account") or None
    require(impersonation is None, "IDENTITY", "CLI impersonation is set")
    ev = Evidence(bound, reader, BqRunner(bq_client, bound["chainEvidenceBytesCap"]), day, role, now)

    services = {}
    for name in so.SERVICES:
        try:
            state = jo.service_state(reader, name)
        except Stop:
            state = None
        same = state is not None and state == baseline["services"][name]
        services[name] = {"serving_revision": state["serving"]["name"] if state else None,
                          "image_digest": state["serving"]["image_digest"] if state else None,
                          "state_sha256": so.fingerprint(state) if state else None, "matches_bound": same}
        if not same:
            ev.fail("SERVICES")
    jobs = {name: so.job_view(reader.job(name)) for name in so.JOB_NAMES}

    rows = ev.runner.run("stage_rows", {"d": day, "stages": [*jo.CHAIN_STAGES, *SUBSTAGES]})
    listed = {job: reader.executions(job) for job in so.JOB_NAMES}
    midnight = dt.datetime(day.year, day.month, day.day, tzinfo=SAST)
    a_at = jo.aware(baseline["aTerminal"]["at_utc"])

    stages, bound_names, previous = {}, set(), None
    for stage in jo.CHAIN_STAGES:
        block, previous = stage_block(ev, stage, rows, previous, expected, midnight, jobs, bound_names)
        stages[stage] = block
    collect_exec = stages["collect"]["execution"]
    if collect_exec and collect_exec["start"] is not None and not jo.aware(collect_exec["start"]) > a_at:
        ev.fail("WINDOW", "collect")

    substages = substage_block(ev, rows)
    side = side_executions(ev, listed, stages, rows, midnight, bound_names)
    lineage(ev, stages)

    reasons = [c for c in CODES if any(code == c for code, _ in ev.failures)]
    qualifies = not reasons
    understand_counts = stages["understand"].pop("_counts", None)
    for block in stages.values():
        block.pop("_counts", None)
    by_line = bool(role == "baseline" and reasons == ["DEGRADED"] and only_parse_json_degradation(understand_counts))
    stage_order = {s: i for i, s in enumerate(jo.CHAIN_STAGES)}
    failed = sorted((s for _, s in ev.failures if s is not None), key=stage_order.get)
    verdict = {"qualifies": qualifies, "baseline_by_line": by_line,
               "failed_stage": None if qualifies else ("understand" if by_line else (failed[0] if failed else None)),
               "reasons": reasons, "decision": DECISION if by_line else None}
    return {
        "schema_version": SCHEMA_VERSION, "kind": KIND, "run_date": day.isoformat(), "role": role,
        "read_at_utc": now().isoformat(),
        "reader": {"account": account, "impersonation": impersonation,
                   "commands": [so.sha_text(" ".join(argv)) for argv in getattr(reader, "argv_log", [])]},
        "jobs_image": {"expected_digest": expected,
                       "definitions": {n: {"image": v["image"], "view_sha256": so.fingerprint(v)} for n, v in jobs.items()}},
        "services": services,
        "a_terminal": {"kind": baseline["aTerminal"]["kind"], "at_utc": baseline["aTerminal"]["at_utc"]},
        "stages": stages, "detect_substages": substages, "side_executions": side, "verdict": verdict,
        "generated_from": {"head": bound["target"]}, "bytes_billed": ev.runner.log,
    }


def stage_block(ev, stage, rows, previous, expected, midnight, jobs, bound_names):
    runs, skipped = stage_runs(rows, stage)
    run_ids = list(runs)
    block = {"run_id": run_ids[0] if len(run_ids) == 1 else None, "run_ids": run_ids, "rows": None, "execution": None,
             "attempt": {"declared": None, "inferred": None}, "timestamps": None, "configuration": None,
             "lineage": {"upstream": None, "outputs": None}, "degradation": None,
             "skipped_duplicates": [{"run_id": r["run_id"], "started_at": iso(r["started_at"]), "error_sha256": r["error_sha256"]}
                                    for r in sorted(skipped, key=lambda r: jo.aware(r["started_at"]))]}
    if len(run_ids) != 1:
        ev.fail("NOT_ONE_RUN", stage)
    if not run_ids:
        return block, None
    rid = run_ids[-1]
    group = runs[rid]
    running, terminal = running_row(group), group[-1]
    exec_name = execution_name(running)
    names = {r: execution_name(running_row(g)) for r, g in runs.items()}
    sharing = [r for r, n in names.items() if exec_name and n == exec_name]
    block["attempt"]["inferred"] = {"run_ids": sharing, "position": sharing.index(rid) + 1 if rid in sharing else None, "of": len(sharing)}
    if len(sharing) > 1:
        ev.fail("RETRIED", stage)
    counts = parse_counts(terminal["counts"]) if terminal["status"] != "running" else None
    declared = parse_counts(running["counts"]) if running is not None else None
    block["attempt"]["declared"] = ({"task_attempt": declared.get("task_attempt"), "job": declared.get("job")}
                                    if isinstance(declared, dict) else None)
    block["rows"] = {
        "count": len(group),
        "running": ({"started_at": iso(running["started_at"]), "counts_sha256": so.fingerprint(declared), "execution": exec_name}
                    if running is not None else None),
        "terminal": {"version": len(group), "status": terminal["status"], "finished_at": iso(terminal["finished_at"]),
                     "has_error": bool(terminal["has_error"]), "error_sha256": terminal["error_sha256"],
                     "counts_sha256": so.fingerprint(counts), "counts": clean_counts(counts)},
    }
    block["_counts"] = counts
    if terminal["finished_at"] is None or terminal["status"] != "ok":
        ev.fail("TERMINAL", stage)

    execution = None
    if running is None or exec_name is None:
        ev.fail("BOUND", stage)
    else:
        bound_names.add(exec_name)
        try:
            execution = jo.describe_execution(ev.reader, exec_name)
        except so.NotFound:
            ev.fail("BOUND", stage)
    if execution is not None:
        block["execution"] = {"name": execution["name"], "job": execution["job"], "image_digest": execution["image_digest"],
                              "start": execution["start"], "completion": execution["completion"],
                              "succeededCount": execution["succeeded"], "failedCount": execution["failed"],
                              "cancelledCount": execution["cancelled"], "retriedCount": execution["retried"]}
        if execution["image_digest"] != expected:
            ev.fail("IMAGE", stage)
        started = jo.aware(running["started_at"])
        finished = jo.aware(terminal["finished_at"]) if terminal["finished_at"] is not None else None
        bracketed = (execution["start"] is not None and execution["completion"] is not None and jo.aware(execution["start"]) <= started
                     and (finished is None or jo.aware(execution["completion"]) >= finished))
        counted = execution["succeeded"] == 1 and execution["failed"] == 0 and execution["cancelled"] == 0
        if execution["job"] != jo.STAGE_JOB[stage] or not counted or not bracketed:
            ev.fail("BOUND", stage)
        if execution["retried"]:
            ev.fail("RETRIED", stage)

    gap = None
    if previous is not None:
        if previous["finished_at"] is not None and running is not None:
            gap = (jo.aware(running["started_at"]) - jo.aware(previous["finished_at"])).total_seconds()
        if gap is None or gap < 0:
            ev.fail("ORDER", stage)
    scheduled_gap = None
    if stage == "collect" and execution is not None and execution["start"] is not None:
        scheduled = midnight + dt.timedelta(hours=SCHEDULED_COLLECT.hour)
        scheduled_gap = int((jo.aware(execution["start"]) - scheduled).total_seconds())
        if abs(scheduled_gap) > ev.bound["collectStartToleranceMinutes"] * 60:
            ev.fail("ORDER", stage)
    if stage == "brief" and terminal["finished_at"] is not None:
        if jo.aware(terminal["finished_at"]).astimezone(SAST) >= dt.datetime.combine(ev.day, DEADLINE, SAST):
            ev.fail("ORDER", stage)
    block["timestamps"] = {"row_started_at": iso(running["started_at"]) if running is not None else None,
                           "row_finished_at": iso(terminal["finished_at"]),
                           "execution_start": execution["start"] if execution else None,
                           "execution_completion": execution["completion"] if execution else None,
                           "upstream_gap_seconds": gap, "scheduled_gap_seconds": scheduled_gap}
    job = jobs[jo.STAGE_JOB[stage]]
    block["configuration"] = {"job_view_sha256": so.fingerprint(job), "env_names_sha256": so.fingerprint(sorted(job["env"])),
                              "stamp": None}
    if previous is not None:
        upstream = previous
        block["lineage"]["upstream"] = {"stage": upstream["stage"], "run_id": upstream["run_id"], "status": upstream["status"]}
        if upstream["status"] != "ok":
            ev.fail("UPSTREAM", stage)
    if stage == "understand":
        block["degradation"] = degradation_of(counts)
        if is_degraded(counts):
            ev.fail("DEGRADED", stage)
    return block, {"stage": stage, "run_id": rid, "status": terminal["status"], "finished_at": terminal["finished_at"]}


def substage_block(ev, rows):
    out = {}
    for stage in SUBSTAGES:
        mine = [r for r in rows if r["stage"] == stage]
        if not mine:
            continue
        by_run = {}
        for row in mine:
            by_run.setdefault(row["run_id"], []).append(row)
        latest = {rid: sort_rows(group)[-1] for rid, group in by_run.items()}
        if any(row["status"] != "ok" for row in latest.values()):
            ev.fail("SUBSTAGES", "detect")
        newest = max(latest.values(), key=order_key)
        out[stage] = {"run_id": newest["run_id"], "status": newest["status"], "finished_at": iso(newest["finished_at"]),
                      "row_count": len(mine)}
    return out


def side_executions(ev, listed, stages, rows, midnight, bound_names):
    day = ev.day
    out = []
    for job in so.JOB_NAMES:
        todays = [e for e in listed[job] if jo.on_run_date(e.get("status", {}).get("startTime"), day)]
        unbound = [e for e in todays if e["metadata"]["name"] not in bound_names]
        stage = next((s for s, j in jo.STAGE_JOB.items() if j == job), None)
        if stage is not None:
            # The one start that is not manual: the brief's 06:15 SAST start, which writes a skipped_duplicate row on a published day.
            # It is counted by time as well as by row, so an extra start at any other hour is MANUAL whatever rows exist, and a typed
            # run is MANUAL too (accepted reading of hard line 2); it is listed below either way.
            allowed = set()
            if stage == "brief":
                skipped = len([r for r in rows if r["stage"] == stage and r["status"] == SKIPPED])
                scheduled = dt.datetime.combine(day, DEADLINE, SAST)
                limit = ev.bound["collectStartToleranceMinutes"] * 60
                near = sorted((e for e in unbound if abs((jo.aware(e["status"]["startTime"]) - scheduled).total_seconds()) <= limit),
                              key=lambda e: (jo.aware(e["status"]["startTime"]), e["metadata"]["name"]))
                allowed = {e["metadata"]["name"] for e in near[:skipped]}
            if any(e["metadata"]["name"] not in allowed for e in unbound):
                ev.fail("MANUAL", stage)
        for entry in unbound:
            view = jo.describe_execution(ev.reader, entry["metadata"]["name"])
            out.append({"job": job, "name": view["name"], "start": view["start"], "completion": view["completion"],
                        "counts": {"succeeded": view["succeeded"], "failed": view["failed"], "cancelled": view["cancelled"],
                                   "retried": view["retried"]},
                        "image_digest": view["image_digest"]})
    return out


def lineage(ev, stages):
    day = ev.day
    detect, brief = stages["detect"], stages["brief"]
    drid = detect["run_ids"][-1] if detect["run_ids"] else None
    brid = brief["run_ids"][-1] if brief["run_ids"] else None
    for stage in ("collect", "understand"):
        stages[stage]["lineage"]["outputs"] = {"basis": "terminal_counts"}
    if drid is not None:
        params = {"d": day, "run_id": drid}
        item_state = ev.runner.run("item_state_count", params)[0]["n"]
        series_test = ev.runner.run("series_test_count", params)[0]["n"]
        locality = None
        if ev.runner.run("table_exists", {})[0]["n"]:
            locality = ev.runner.run("item_locality_count", params)[0]["n"]
        detect["lineage"]["outputs"] = {"item_state": item_state, "series_test": series_test, "item_locality": locality}
        if not item_state or not series_test:
            ev.fail("LINEAGE", "detect")
    if brid is not None:
        per_market = {r["market"]: r["n"] for r in ev.runner.run("briefs_by_market", {"d": day, "run_id": brid})}
        brief["lineage"]["outputs"] = {"briefs_by_market": per_market}
        if any(not per_market.get(m) for m in BRIEF_MARKETS):
            ev.fail("LINEAGE", "brief")


def write_manifest(release_dir, manifest):
    path = Path(release_dir) / f"chain-evidence-{manifest['run_date']}.json"
    with path.open("x", encoding="utf-8") as out:
        out.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def main(argv=None, reader_factory=None, bq_factory=None, now=None):
    parser = argparse.ArgumentParser(description="Read-only chain evidence producer.")
    parser.add_argument("--date", required=True)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--expect", choices=ROLES, required=True)
    args = parser.parse_args(argv)
    try:
        bound = json.loads(args.bindings.read_text(encoding="utf-8-sig"))
        jo.validate_jobs_bindings(bound, producer=True)
        day = dt.date.fromisoformat(args.date)
        target = Path(bound["releaseDir"]) / f"chain-evidence-{day.isoformat()}.json"
        require(not target.exists(), "EXISTS", "A chain evidence manifest for this date already exists; it is never overwritten")
        from core.setup.release import bound_readback as helper

        reader = (reader_factory or helper.GcloudReader)(bound["readTimeoutSeconds"])
        client = (bq_factory or (lambda b: GoogleBq(b["readTimeoutSeconds"])))(bound)
        manifest = build_manifest(bound, reader, client, day, args.expect, now=now)
        path = write_manifest(bound["releaseDir"], manifest)
        args.evidence.mkdir(parents=True, exist_ok=True)
        with (args.evidence / f"chain-evidence-{day.isoformat()}.run.json").open("x", encoding="utf-8") as log:
            log.write(json.dumps({"commands": manifest["reader"]["commands"], "bytes_billed": manifest["bytes_billed"]}, indent=2) + "\n")
    except Stop as error:
        print(f"STOP: {error.code}: {error.message}", file=sys.stderr)
        return 1
    except Probe as error:
        print(f"PROBE: {error}", file=sys.stderr)
        return 3
    except (KeyError, ValueError, OSError) as error:
        print(f"STOP: BINDINGS: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"chain evidence: written ({path.name}) verdict qualifies={manifest['verdict']['qualifies']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
