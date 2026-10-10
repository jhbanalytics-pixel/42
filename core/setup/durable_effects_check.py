"""Durable effects of a release (W8-REL 4): validate the manifest, hold it to the schema files and the a80 jobs, and run
the three read-only checks the release paste calls.

    py -3.13 core/setup/durable_effects_check.py --check readbacks|rollback-blockers|inflight-asks --evidence <run dir>
        [--manifest <file>] [--bindings <file>]

The checker never runs free command or query text taken from the manifest (K4). A native readback is a typed read
(`kind`, `target`, `expected_result_sha256`) mapped to a fixed template here. The one text it may run from a manifest is
a rollback blocker query, only after its hash matches, it is a single SELECT, and none of eight write words appears in
it, all before any dry run; then a dry run under its bytes cap comes first. The in-flight Ask count is a fixed template
in this file whose hash the bindings bind. A refusal exits 1 having made no BigQuery call at all.

Exit 0 pass, 1 refusal or failed check. Output is JSON with schema_version 1.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.setup.release import natives  # noqa: E402

SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[2]
PROJECT = "ogilvy-trends-v2"
EFFECT_KINDS = ("schema", "view", "receipt", "reservation", "stored_answer", "config", "env", "secret", "cloud_run_tag", "cloud_run_traffic")
CHANGES = ("create_table", "add_column", "create_view", "replace_view", "write_shape", "config_key", "env_set", "tag_add", "traffic_pin")
APPLY_PHASES = ("before_candidate", "with_candidate", "after_promotion")
# The fixed allowlist of how an effect reaches the world. `schema` runs core.schema.apply; the three Cloud Run kinds are
# steps the candidate deploy and the Pin already perform; `code` is a shape the shipped image writes, with nothing to run.
APPLY_KINDS = ("schema", "tag_add", "traffic_pin", "env_set", "code")
RECOVERY_KINDS = ("none_needed", "compensating_statement", "flag_off")
HELPER_PHASES = ("BeforeAnyWrite", "Freeze", "BeforeCandidate", "BeforeSmoke", "AfterSmoke", "BeforePromotion", "AfterAgentPromotion",
                 "AfterPromotion", "BeforeRollback", "AfterRollback", "BeforeRetire", "AfterRetire")
# helper_phase: a Cloud Run effect is read back by a phase of the services-only helper, not by BigQuery.
READBACK_KINDS = ("information_schema_columns", "row_count_since_marker", "helper_phase")
READBACK_KEYS = {"kind", "target", "expected_result_sha256"}
COMPAT_KEYS = ("a80_readers", "a80_writers", "a80_ddl_writers", "candidate_readers_old_records", "candidate_writers_old_readers")
EFFECT_FIELDS = ("effect_id", "kind", "target", "change", "apply_order", "apply_phase", "owner", "statement_ref", "statement_sha256", "apply_kind",
                 "additive", "destructive", "consumers", "native_readback", "compat", "forward_recovery", "authority_or_holds", "evidence")
RELEASE_FIELDS = ("schema_version", "release_id", "source", "effects", "no_storage_change", "rollback_blockers", "a80_ddl_writers",
                  "generated_from", "reviewed_by", "manifest_sha256")
UNSAFE_RECOVERY = re.compile(r"\b(DROP|DELETE|TRUNCATE)\b|CREATE\s+OR\s+REPLACE", re.I)
BLOCKER_FORBIDDEN = re.compile(r"\b(INSERT|UPDATE|MERGE|CREATE|ALTER|DROP|DELETE|TRUNCATE)\b", re.I)
IDENT = re.compile(r"[A-Za-z0-9_]+")
DDL_REPLACE = re.compile(r"CREATE\s+OR\s+REPLACE\s+(?:TEMP\s+|TEMPORARY\s+)?(?:MATERIALIZED\s+)?(?:VIEW|TABLE\s+FUNCTION|TABLE|FUNCTION|PROCEDURE)\s+"
                         r"(?:IF\s+NOT\s+EXISTS\s+)?`?([^\s`(]+)`?", re.I)

# The Ask rows in flight: runs whose latest row is still running. Read only; its hash and bytes cap are bound in the bindings.
INFLIGHT_ASKS_SQL = (
    f"SELECT COUNT(*) AS running FROM (SELECT run_id, ARRAY_AGG(status ORDER BY COALESCE(finished_at, started_at) DESC LIMIT 1)[OFFSET(0)] AS status "
    f"FROM `{PROJECT}.intelligence_42_agent.runs` WHERE run_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) AND stage = 'ask' GROUP BY run_id) "
    f"WHERE status = 'running'")


class Refusal(Exception):
    pass


def require(ok, message):
    if not ok:
        raise Refusal(message)


def sha_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def manifest_hash(manifest):
    return sha_text(canonical({k: v for k, v in manifest.items() if k != "manifest_sha256"}))


INFLIGHT_ASKS_SQL_SHA256 = sha_text(INFLIGHT_ASKS_SQL)


# the schema files: what core.schema.apply would run

def split_statements(text):
    """core.schema.apply.split_statements, restated so the checker needs no cloud client to import."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"--[^\n]*", "", text)
    return [s.strip() for s in text.split(";") if s.strip()]


def statement_kind(statement):
    """core.schema.apply.kind, restated: only these forms are ever run; every other statement is skipped by apply."""
    m = re.search(r"^(?:CREATE\s+(SCHEMA|TABLE|VIEW)\s+IF\s+NOT\s+EXISTS\s+`([^`]+)`|ALTER\s+TABLE\s+`([^`]+)`\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s)",
                  statement, re.I)
    if not m:
        return None
    return m.group(1).lower() if m.group(1) else "alter"


def schema_statements(texts):
    """The statements `apply --apply` would run for these file texts (agent.sql, then core.sql)."""
    return [s for text in texts for s in split_statements(text)]


def new_statements(head_texts, a80_texts):
    """The statements at the release commit that were not at a80, in order."""
    old = {sha_text(s) for s in schema_statements(a80_texts)}
    return [s for s in schema_statements(head_texts) if sha_text(s) not in old]


# trees at a commit: read from git, never from the working directory

def git_tree_files(rev, prefix="core", cwd=ROOT):
    """(path, text) of every text file under prefix at rev, from `git archive`; nothing is extracted to disk."""
    try:
        git = natives.native("git")
    except natives.NativeRefused as error:
        raise Refusal(str(error)) from None
    proc = subprocess.run([git, "archive", "--format=tar", rev, prefix], cwd=cwd, capture_output=True, timeout=120)
    require(proc.returncode == 0, f"git archive of {rev} failed")
    files = []
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        for member in tar:
            if member.isfile():
                try:
                    files.append((member.name, tar.extractfile(member).read().decode("utf-8")))
                except UnicodeDecodeError:
                    continue
    return files


def is_test_path(path):
    return "/tests/" in path or path.rsplit("/", 1)[-1].startswith(("test_", "conftest")) or path.startswith("core/eval/fixtures")


def derive_ddl_writers(files):
    """{object name: [files]} for every CREATE OR REPLACE in a non-test text file under core/ except core/schema/. The jobs
    image copies all of core/, so any module can be run by a job: the set is derived, never typed (F17, K3)."""
    out = {}
    for path, text in files:
        if is_test_path(path) or path.startswith("core/schema/") or not path.endswith((".py", ".sql")):
            continue
        for m in DDL_REPLACE.finditer(text):
            name = m.group(1).rstrip(";").rsplit(".", 1)[-1].strip("`{}")
            out.setdefault(name, []).append(path)
    return {name: sorted(set(paths)) for name, paths in sorted(out.items())}


def check_services_only_isolation(effects, referenced_objects, a80_files, head_files, column_proofs=None):
    """DE-12: no effect target and no SQL object the diff references may be in the set of objects the a80 jobs image
    creates or replaces, unless the effect proves the candidate reads only columns the a80 definition already has."""
    owned = set(derive_ddl_writers(a80_files)) | set(derive_ddl_writers(head_files))
    proofs = column_proofs or {}
    problems = []
    for effect in effects:
        name = str(effect.get("target", "")).rsplit(".", 1)[-1]
        if name in owned and not proofs.get(effect.get("effect_id")):
            problems.append(f"effect {effect.get('effect_id')}: {name} is created or replaced by an a80 job")
    for name in sorted(set(referenced_objects)):
        short = name.rsplit(".", 1)[-1]
        if short in owned and not proofs.get(short):
            problems.append(f"the diff references {short}, which an a80 job creates or replaces")
    return problems


POSITIONAL = re.compile(r"INSERT\s+(?:INTO\s+)?`?([\w.{}-]+)`?\s*(?:\(\s*\))?\s*(?:SELECT\s+\*|SELECT\b|VALUES\b)", re.I)
INSERT_NO_COLUMNS = re.compile(r"INSERT\s+(?:INTO\s+)?`?([\w.{}-]+)`?\s+(?!\()(?:SELECT|VALUES|WITH)\b", re.I)


def positional_writers(table, a80_files):
    """DE-13: a80 statements that write the table without naming columns (a new column would shift or fail them)."""
    short = table.rsplit(".", 1)[-1]
    hits = []
    for path, text in a80_files:
        if is_test_path(path) or not path.endswith((".py", ".sql")):
            continue
        for m in INSERT_NO_COLUMNS.finditer(text):
            if m.group(1).rsplit(".", 1)[-1] == short:
                hits.append(path)
    return sorted(set(hits))


def select_star_readers(table, a80_files):
    """DE-13: a80 readers of the table that select * (a new column reaches whatever shape they fill)."""
    short = table.rsplit(".", 1)[-1]
    pattern = re.compile(r"SELECT\s+(?:[\w]+\.)?\*\s+FROM\s+`?[\w.{}-]*" + re.escape(short) + r"\b", re.I)
    return sorted({path for path, text in a80_files if not is_test_path(path) and path.endswith((".py", ".sql")) and pattern.search(text)})


# the manifest

def _is_person(owner):
    return (isinstance(owner, dict) and isinstance(owner.get("lane"), int) and not isinstance(owner.get("lane"), bool)
            and isinstance(owner.get("person"), str) and len(owner["person"].split()) >= 2 and not owner["person"].isupper())


def scan_blocker_query(query):
    """A rollback blocker query is a single SELECT (or WITH ... SELECT) and holds none of the eight write words."""
    text = query.strip().rstrip(";").strip()
    if ";" in text:
        return "more than one statement"
    if not re.match(r"(?:SELECT|WITH)\b", text, re.I):
        return "not a SELECT"
    found = BLOCKER_FORBIDDEN.search(text)
    return f"holds the word {found.group(0).upper()}" if found else None


def validate_blocker(blocker):
    problems = []
    if not isinstance(blocker, dict) or set(blocker) != {"condition_query_ref", "condition_query_sha256", "bytes_cap", "blocks_when"}:
        return ["rollback_blocker keys are not condition_query_ref, condition_query_sha256, bytes_cap, blocks_when"]
    if sha_text(str(blocker["condition_query_ref"])) != blocker["condition_query_sha256"]:
        problems.append("rollback_blocker hash does not match its query text")
    bad = scan_blocker_query(str(blocker["condition_query_ref"]))
    if bad:
        problems.append(f"rollback_blocker query {bad}")
    if not isinstance(blocker["bytes_cap"], int) or isinstance(blocker["bytes_cap"], bool) or blocker["bytes_cap"] <= 0:
        problems.append("rollback_blocker bytes_cap is not a positive integer")
    if blocker["blocks_when"] != "count_gt_0":
        problems.append("rollback_blocker blocks_when is not count_gt_0")
    return problems


def validate_effect(effect, label):
    problems = []
    if not isinstance(effect, dict):
        return [f"{label}: not an object"]
    for field in EFFECT_FIELDS:
        if field not in effect:
            problems.append(f"{label}: missing {field}")
    if problems:
        return problems
    if effect["kind"] == "iam" or effect["kind"] not in EFFECT_KINDS:
        problems.append(f"{label}: kind {effect['kind']!r} is not allowed")
    if effect["change"] not in CHANGES:
        problems.append(f"{label}: unknown change {effect['change']!r}")
    if effect["apply_phase"] not in APPLY_PHASES:
        problems.append(f"{label}: unknown apply_phase")
    if effect["apply_kind"] not in APPLY_KINDS:
        problems.append(f"{label}: apply_kind {str(effect['apply_kind'])[:40]!r} is not in the fixed allowlist")
    if not isinstance(effect["apply_order"], int) or isinstance(effect["apply_order"], bool):
        problems.append(f"{label}: apply_order is not an integer")
    if effect["destructive"] is not False:
        problems.append(f"{label}: destructive must be false")
    if effect["kind"] == "schema" and effect["additive"] is not True:
        problems.append(f"{label}: a schema effect must be additive")
    if not _is_person(effect["owner"]):
        problems.append(f"{label}: owner is not a lane and a named person")
    if effect["kind"] == "schema" and effect["apply_kind"] != "schema":
        problems.append(f"{label}: a schema effect applies through core.schema.apply")
    readback = effect["native_readback"]
    if not isinstance(readback, dict) or set(readback) != READBACK_KEYS:
        problems.append(f"{label}: native_readback must hold exactly kind, target and expected_result_sha256 (no command)")
    elif readback["kind"] not in READBACK_KINDS:
        problems.append(f"{label}: native_readback kind {str(readback['kind'])[:40]!r} is unknown")
    elif readback["kind"] == "helper_phase" and readback["target"] not in HELPER_PHASES:
        problems.append(f"{label}: native_readback names a phase the helper does not have")
    compat = effect["compat"]
    if not isinstance(compat, dict) or any(key not in compat for key in COMPAT_KEYS):
        problems.append(f"{label}: compat lacks one of {', '.join(COMPAT_KEYS)}")
    recovery = effect["forward_recovery"]
    if not isinstance(recovery, dict) or recovery.get("kind") not in RECOVERY_KINDS:
        problems.append(f"{label}: forward_recovery kind is not one of {', '.join(RECOVERY_KINDS)}")
    elif recovery["kind"] != "none_needed":
        text = str(recovery.get("statement_ref", ""))
        if UNSAFE_RECOVERY.search(text):
            problems.append(f"{label}: forward_recovery holds a drop, delete, truncate or replace")
        if sha_text(text) != recovery.get("statement_sha256"):
            problems.append(f"{label}: forward_recovery hash does not match its statement")
    if effect["authority_or_holds"] is True:
        if "rollback_blocker" not in effect:
            problems.append(f"{label}: authority_or_holds needs a rollback_blocker")
        else:
            problems += [f"{label}: {p}" for p in validate_blocker(effect["rollback_blocker"])]
    consumers = effect["consumers"]
    if not isinstance(consumers, list) or any(not isinstance(c, dict) or c.get("image") not in ("f42-web", "jobs") or "file" not in c for c in consumers):
        problems.append(f"{label}: consumers must name component, image (f42-web or jobs), file, line, access")
    return problems


def validate_manifest(manifest):
    """DE-01 and DE-09: every field required, unknown versions and unsafe forms refused, the recorded hash recomputed."""
    if not isinstance(manifest, dict):
        return ["the manifest is not an object"]
    if manifest.get("schema_version") != SCHEMA_VERSION:
        return [f"unknown schema_version {manifest.get('schema_version')!r}"]
    problems = [f"missing {field}" for field in RELEASE_FIELDS if field not in manifest]
    if problems:
        return problems
    if manifest["manifest_sha256"] != manifest_hash(manifest):
        problems.append("manifest_sha256 does not match a recomputation")
    effects = manifest["effects"]
    if not isinstance(effects, list):
        return problems + ["effects is not a list"]
    orders = [e.get("apply_order") for e in effects if isinstance(e, dict)]
    if len(orders) != len(set(orders)):
        problems.append("apply_order has a tie")
    ids = [e.get("effect_id") for e in effects if isinstance(e, dict)]
    if len(ids) != len(set(ids)):
        problems.append("effect_id is repeated")
    for index, effect in enumerate(effects):
        problems += validate_effect(effect, f"effect {effect.get('effect_id', index) if isinstance(effect, dict) else index}")
    if not effects and not (isinstance(manifest["no_storage_change"], dict) and manifest["no_storage_change"].get("claimed") is True):
        problems.append("no effects and no no_storage_change claim")
    holds = [e for e in effects if isinstance(e, dict) and e.get("authority_or_holds") is True]
    blockers = manifest["rollback_blockers"]
    if not isinstance(blockers, list) or len(blockers) != len(holds):
        problems.append("rollback_blockers does not list one blocker per effect with authority_or_holds")
    else:
        for blocker in blockers:
            problems += [f"rollback_blockers: {p}" for p in validate_blocker(blocker)]
    return problems


def check_schema_effects(manifest, head_texts, a80_texts):
    """DE-02, DE-03, DE-11: the statements apply.py would run at the release commit minus a80 equal the manifest's schema
    effects by hash, every one is a form apply.py recognises (so none is skipped silently), and each effect's text hashes
    to its statement_sha256."""
    problems = []
    new = new_statements(head_texts, a80_texts)
    for statement in new:
        if statement_kind(statement) is None:
            problems.append(f"a statement apply.py would skip silently: {statement.splitlines()[0][:70]}")
    schema = [e for e in manifest["effects"] if e.get("kind") == "schema"]
    for effect in schema:
        if sha_text(effect["statement_ref"]) != effect["statement_sha256"]:
            problems.append(f"effect {effect['effect_id']}: statement_sha256 does not match statement_ref")
        if statement_kind(effect["statement_ref"]) is None:
            problems.append(f"effect {effect['effect_id']}: not a statement apply.py recognises")
    listed = {e["statement_sha256"] for e in schema}
    actual = {sha_text(s) for s in new}
    problems += [f"a schema statement is not in the manifest: {s.splitlines()[0][:70]}" for s in new if sha_text(s) not in listed]
    problems += [f"effect {e['effect_id']} lists a statement apply.py would not run" for e in schema if e["statement_sha256"] not in actual]
    return problems


def check_no_storage_change(claim, head_texts, a80_texts):
    """DE-04: a claim of no change proves the existing shape. Every table and column the diff uses is in the schema
    statements recomputed from core/schema/*.sql, and the claim fails when the diff adds a schema statement."""
    problems = []
    if new_statements(head_texts, a80_texts):
        problems.append("the diff adds a schema statement, so a claim of no storage change is false")
    statements = schema_statements(head_texts)
    for reference in claim.get("references", []):
        table, column = reference.get("table", ""), reference.get("column", "")
        defining = [s for s in statements if re.search(r"`[^`]*\b" + re.escape(table) + r"`", s)]
        if not defining:
            problems.append(f"table {table} is not defined in the schema statements")
        elif column and not any(re.search(r"\b" + re.escape(column) + r"\b", s) for s in defining):
            problems.append(f"column {table}.{column} is not defined in the schema statements")
    return problems


def check_consumers(manifest, changed_files):
    """DE-08: every changed file that mentions an effect target appears in that effect's consumers."""
    problems = []
    for effect in manifest["effects"]:
        short = str(effect["target"]).rsplit(".", 1)[-1]
        listed = {c.get("file") for c in effect["consumers"]}
        for path, text in changed_files.items():
            if short and short in text and path not in listed:
                problems.append(f"effect {effect['effect_id']}: {path} mentions {short} but is not in consumers")
    return problems


# BigQuery: every call goes through one object, so a refusal can be shown to have made none

class BigQuery:
    """The real client, imported only when a check reaches it."""

    def __init__(self):
        from google.cloud import bigquery

        self._bq, self._client = bigquery, bigquery.Client(project=PROJECT)

    def dry_run(self, sql, params=None):
        job = self._client.query(sql, job_config=self._bq.QueryJobConfig(dry_run=True, use_query_cache=False, query_parameters=params or []))
        return job.total_bytes_processed

    def query(self, sql, params=None, max_bytes=None):
        config = self._bq.QueryJobConfig(maximum_bytes_billed=max_bytes, query_parameters=params or [])
        return [dict(row) for row in self._client.query(sql, job_config=config).result()]


def readback_sql(readback):
    """The fixed template for a typed native readback, and its parameters. Identifiers are validated, never interpolated raw."""
    require(set(readback) == READBACK_KEYS and readback.get("kind") in READBACK_KINDS, "native_readback is not a typed read")
    target = str(readback["target"])
    if readback["kind"] == "information_schema_columns":
        m = re.fullmatch(r"(\w+)\.(\w+)", target)
        require(m is not None, "native_readback target is not dataset.table")
        return (f"SELECT column_name FROM `{PROJECT}.{m.group(1)}.INFORMATION_SCHEMA.COLUMNS` WHERE table_name = '{m.group(2)}' ORDER BY column_name", [])
    m = re.fullmatch(r"(\w+)\.(\w+)#(\w+)#([\w:.+-]+)", target)
    require(m is not None, "native_readback target is not dataset.table#column#marker")
    return (f"SELECT COUNT(*) AS n FROM `{PROJECT}.{m.group(1)}.{m.group(2)}` WHERE {m.group(3)} >= '{m.group(4)}'", [])


def run_readbacks(manifest, bq, bytes_cap=10 * 1024 * 1024):
    problems = validate_manifest(manifest)
    if problems:
        raise Refusal("the manifest is not valid: " + problems[0])
    results = []
    for effect in manifest["effects"]:
        if effect["native_readback"]["kind"] == "helper_phase":
            results.append({"effect_id": effect["effect_id"], "helper_phase": effect["native_readback"]["target"], "matches": True})
            continue
        sql, params = readback_sql(effect["native_readback"])
        require(bq.dry_run(sql, params) <= bytes_cap, f"effect {effect['effect_id']}: the readback exceeds its bytes cap")
        rows = bq.query(sql, params, bytes_cap)
        got = sha_text(canonical(rows))
        results.append({"effect_id": effect["effect_id"], "result_sha256": got, "matches": got == effect["native_readback"]["expected_result_sha256"]})
    return {"schema_version": SCHEMA_VERSION, "check": "readbacks", "ok": all(r["matches"] for r in results), "results": results}


def run_rollback_blockers(manifest, bq):
    """3.3 row 4. With no blockers it decides offline: blocked false, no query. Otherwise every blocker is hashed and scanned
    before any dry run; a query that cannot run makes blocked true."""
    require(manifest.get("schema_version") == SCHEMA_VERSION, "unknown manifest schema_version")
    blockers = manifest.get("rollback_blockers")
    require(isinstance(blockers, list), "rollback_blockers is not a list")
    if not blockers:
        return {"schema_version": SCHEMA_VERSION, "check": "rollback-blockers", "blocked": False, "blockers": 0}
    for blocker in blockers:
        problems = validate_blocker(blocker)
        if problems:
            raise Refusal(problems[0])
    blocked, details = False, []
    for blocker in blockers:
        try:
            require(bq.dry_run(blocker["condition_query_ref"]) <= blocker["bytes_cap"], "the blocker query exceeds its bytes cap")
            rows = bq.query(blocker["condition_query_ref"], None, blocker["bytes_cap"])
            count = int(next(iter(rows[0].values()))) if rows else 0
            this = count > 0
        except Refusal:
            this = True
        except Exception:
            this = True
        blocked = blocked or this
        details.append({"query_sha256": blocker["condition_query_sha256"], "blocked": this})
    return {"schema_version": SCHEMA_VERSION, "check": "rollback-blockers", "blocked": blocked, "blockers": len(blockers), "details": details}


def run_inflight_asks(bindings, bq):
    """The in-flight Ask count: a fixed template whose hash the bindings bind, a dry run first, a bytes cap."""
    require(bindings.get("schema_version") == SCHEMA_VERSION, "unknown bindings schema_version")
    require(bindings.get("inflightAsksSqlSha256") == INFLIGHT_ASKS_SQL_SHA256 == sha_text(INFLIGHT_ASKS_SQL), "the in-flight Ask template is not the bound one")
    cap = bindings.get("inflightAsksBytesCap")
    require(isinstance(cap, int) and not isinstance(cap, bool) and cap > 0, "inflightAsksBytesCap is not a positive integer")
    require(bq.dry_run(INFLIGHT_ASKS_SQL) <= cap, "the in-flight Ask count exceeds its bytes cap")
    rows = bq.query(INFLIGHT_ASKS_SQL, None, cap)
    return {"schema_version": SCHEMA_VERSION, "check": "inflight-asks", "running": int(rows[0]["running"]) if rows else 0}


def main(argv=None, bq_factory=BigQuery):
    parser = argparse.ArgumentParser(description="Durable effects checks for a release.")
    parser.add_argument("--check", choices=("validate", "readbacks", "rollback-blockers", "inflight-asks"), required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--bindings", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.check == "inflight-asks":
            require(args.bindings is not None, "--bindings is required for inflight-asks")
            result = run_inflight_asks(json.loads(args.bindings.read_text(encoding="utf-8-sig")), bq_factory())
        else:
            require(args.manifest is not None, "--manifest is required")
            manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
            if args.check == "validate":
                problems = validate_manifest(manifest)
                if problems:
                    raise Refusal("the manifest is not valid: " + problems[0])
                result = {"schema_version": SCHEMA_VERSION, "check": "validate", "ok": True, "effects": len(manifest["effects"])}
            elif args.check == "rollback-blockers":
                blockers = manifest.get("rollback_blockers") if isinstance(manifest, dict) else None
                result = run_rollback_blockers(manifest, bq_factory() if blockers else None)
            else:
                result = run_readbacks(manifest, bq_factory())
    except Refusal as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as error:
        print(f"REFUSED: {type(error).__name__}", file=sys.stderr)
        return 1
    args.evidence.mkdir(parents=True, exist_ok=True)
    (args.evidence / f"{args.check}.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("ok", True) is not False else 1


if __name__ == "__main__":
    sys.exit(main())
