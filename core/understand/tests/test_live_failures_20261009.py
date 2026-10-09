"""The two understand failures staging showed on 8 and 9 Oct 2026, reproduced offline.

1. The vector index build refused post_enrichment: "Column 'embedding' must have the same array length, while the
   minimum length is 0 and the maximum length is 768". enrich writes rows with no vector (enrich_insert.sql leaves
   embedding out), BigQuery stores those as empty arrays, and the index build wants one length throughout. The run
   ended ok with the failure only in counts.index_error.
2. On 9 Oct every market that had posts recorded only "Input number: ... cannot round-trip through string
   representation; error in PARSE_JSON expression". cluster_checkpoint.sql (first shipped in the a80 jobs image,
   absent from the image that ran on 8 Oct) parses its parameters with PARSE_JSON in the default exact number mode.

BigQuery is not reachable from the tests. StrictJson stands in for its PARSE_JSON: a call without wide_number_mode is
exact and refuses a number whose text it cannot reproduce from the double it parses to (more than 15 significant
digits, or exponent notation), a call with wide_number_mode => 'round' accepts every number. That rule is a model of
the documented behaviour, not a measurement of the real service: the payload that failed on 9 Oct was never stored,
so which number it refused is not known.
"""
import json
import re
import sys
from pathlib import Path
from datetime import date, datetime, timezone
from decimal import Decimal

import numpy as np
import pytest

from core.understand import cluster, embed, job
from core.understand.tests.test_cluster import CORE, DAY, NetModel, core_db, duck_execute, synthetic_clusters
from core.understand.tests.test_embed import FakeWarehouse, fake_chain, finished_run, sql

SQL_DIR = cluster.SQL_DIR
RAGGED_MESSAGE = ("400 GET https://bigquery.googleapis.com/bigquery/v2/projects/ogilvy-trends-v2/queries/job_x"
                  "?maxResults=0&location=US&prettyPrint=false: Column 'embedding' must have the same array length, "
                  "while the minimum length is 0 and the maximum length is 768.")
ROUND_TRIP = "400 Invalid input: Input number: {} cannot round-trip through string representation; error in PARSE_JSON expression"


class BadRequest(Exception):
    pass


@pytest.fixture(autouse=True)
def job_clock(monkeypatch):
    """10:00 SAST on DAY, the run date fake_chain hands out, so enrichment and clustering run."""
    monkeypatch.setattr(job, "now", lambda: datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc), raising=False)


@pytest.fixture(autouse=True)
def quiet_net(monkeypatch):
    monkeypatch.setenv("GEMINI_PRICE_INPUT_PER_M", "1")
    monkeypatch.setenv("GEMINI_PRICE_OUTPUT_PER_M", "5")
    monkeypatch.setattr(cluster, "net_model", NetModel)
    monkeypatch.setattr(cluster, "spend_today", lambda execute, now: (0.0, 20.0))
    monkeypatch.setattr(cluster, "book_spend", lambda execute, **kw: round(kw["usd"], 6))


# Failure 1: the vector index

def test_enrichment_rows_are_written_without_a_vector():
    for name in ("enrich_insert", "enrich_insert_sensitive"):
        head = re.search(r"INSERT INTO `[^`]+post_enrichment` \(([^)]*)\)", sql(name)).group(1)
        assert "embedding" not in head, f"{name}.sql would write a vector"


class StoredEmbeddings(FakeWarehouse):
    """post_enrichment as BigQuery sees it: the length of every row's embedding. embedding_count reads the lengths,
    and the index build refuses a column whose arrays are not all one length, with BigQuery's own message."""

    def __init__(self, lengths):
        super().__init__(shape="job", stored=sum(1 for n in lengths if n))
        self.lengths = lengths

    def __call__(self, text, params, max_bytes=None):
        if text == sql("embedding_count"):
            self.calls.append((text, params))
            row = {"n": sum(1 for n in self.lengths if n > 0)}
            row["unindexable"] = sum(1 for n in self.lengths if n != 768)
            return {"rows": [row]}
        if text == sql("index"):
            self.calls.append((text, params))
            if len(set(self.lengths)) > 1:
                raise BadRequest(RAGGED_MESSAGE)
            return {"num_dml_affected_rows": None}
        return super().__call__(text, params, max_bytes)


def test_embedding_count_counts_the_rows_that_are_not_768_long():
    con = core_db()
    vector = [0.1] * 768
    con.executemany(f"INSERT INTO {CORE}.post_enrichment VALUES (?, ?)",
                    [("a", vector), ("b", vector), ("c", []), ("d", []), ("e", []), ("f", [0.1] * 5), ("g", None)])
    [row] = duck_execute(con)(sql("embedding_count"), {})
    assert (row["n"], row["unindexable"]) == (2 + 1, 3 + 1 + 1)  # n: any stored vector (as before); not 768: empty, short, NULL


def test_a_refused_index_records_how_many_rows_broke_it():
    fake = StoredEmbeddings([768] * 5_000 + [0] * 412)
    counts = embed.run_embed(fake, run_date=DAY)
    assert counts["index"] == "failed"
    assert "must have the same array length" in counts["index_error"]
    assert counts["index_unindexable_rows"] == 412
    assert counts["embedded_total"] == 5_000


class ExistingIndexWarehouse(StoredEmbeddings):
    """An index that already exists: the build statement succeeds, and the rows that cannot be indexed are still
    counted by embedding_count."""

    def __call__(self, text, params, max_bytes=None):
        if text == sql("index"):
            self.calls.append((text, params))
            return {"num_dml_affected_rows": None}
        return super().__call__(text, params, max_bytes)


def test_unindexable_rows_are_recorded_when_the_index_already_exists():
    counts = embed.run_embed(ExistingIndexWarehouse([768] * 5_000 + [0] * 412), run_date=DAY)
    assert counts["index"] == "created_or_exists" and "index_error" not in counts
    assert counts["index_unindexable_rows"] == 412


def test_an_index_that_builds_adds_no_unindexable_key():
    counts = embed.run_embed(StoredEmbeddings([768] * 5_000), run_date=DAY)
    assert counts["index"] == "created_or_exists" and "index_unindexable_rows" not in counts


def index_run(monkeypatch, lengths, cluster_error=None):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)
    monkeypatch.setattr(job, "run_enrich", lambda execute, **kw: {"enriched": 3})
    if cluster_error:
        monkeypatch.setattr(job, "run_cluster", lambda execute, **kw: (_ for _ in ()).throw(cluster_error))
    else:
        monkeypatch.setattr(job, "run_cluster", lambda execute, *, market, **kw: {"market": market, "clusters": 4})
    assert job.main(execute=StoredEmbeddings(lengths)) == 0
    return log


def test_a_refused_index_is_recorded_and_leaves_the_run_ok_and_not_partial(monkeypatch, capsys):
    """Nothing reads the index (tvf_search_posts scores exact cosine), so a refusal is a count, never an alert."""
    log = index_run(monkeypatch, [768] * 5_000 + [0] * 412)
    finished = finished_run(log)
    assert finished[2] == "ok" and finished[4] is None
    counts = finished[3]
    assert counts["index"] == "failed" and "same array length" in counts["index_error"]
    assert counts["index_unindexable_rows"] == 412
    assert not {"partial", "partial_reason", "partial_error", "data_issue"} & set(counts)
    assert job.degraded_steps(counts) == []
    assert log[-1] == ("start_next", "understand", DAY), "detect still starts"
    assert capsys.readouterr().err == ""


def test_a_clean_index_leaves_the_run_unmarked(monkeypatch, capsys):
    counts = finished_run(index_run(monkeypatch, [768] * 5_000))[3]
    assert counts["index"] == "created_or_exists" and "index_unindexable_rows" not in counts
    assert "partial" not in counts and "partial" not in capsys.readouterr().err


def test_a_cluster_stack_failure_is_still_partial_whether_or_not_the_index_failed(monkeypatch, capsys):
    counts = finished_run(index_run(monkeypatch, [768] * 5_000 + [0] * 3, RuntimeError("stack gone")))[3]
    assert counts["partial_reason"] == "cluster_stack_failed" and counts["data_issue"] == job.TOPICS_FAILED_TEXT
    assert counts["index"] == "failed" and "same array length" in counts["index_error"]
    err = capsys.readouterr().err
    assert "understand partial: cluster_stack_failed" in err and "index_failed" not in err
    assert "index" not in job.degraded_reasons(counts)


# Failure 2: PARSE_JSON in the cluster checkpoint

def bq_number_refused(token):
    """Why the model of BigQuery's exact mode refuses this JSON number text, or None."""
    mantissa = re.split(r"[eE]", token)[0]
    if re.search(r"[eE]", token):
        return "exponent notation"
    digits = re.sub(r"[^0-9]", "", mantissa).lstrip("0")
    return "more than 15 significant digits" if len(digits) > 15 else None


class StrictJson:
    """Wraps an executor: every PARSE_JSON(@name) without wide_number_mode => 'round' refuses a number the model
    cannot take, with the message BigQuery gave on 9 Oct. Every refusal is kept in refused."""

    def __init__(self, execute):
        self.execute, self.refused = execute, []

    def __call__(self, text, params, max_bytes=None):
        for name, mode in re.findall(r"PARSE_JSON\(@(\w+)(?:\s*,\s*wide_number_mode\s*=>\s*'(\w+)')?\)", text):
            if mode == "round" or name not in params or not isinstance(params[name], str):
                continue

            def refuse(token):
                if bq_number_refused(token):
                    shown = f"{float(token):.6g}"
                    self.refused.append((name, token))
                    raise BadRequest(ROUND_TRIP.format(shown))
                return float(token)

            json.loads(params[name], parse_float=refuse)
        return self.execute(text, params)


def small_valued(n=2):
    """Clusters as a 768 wide embedding gives them: centroid components near zero are as small as 1e-05 and a
    membership probability can be smaller still."""
    clusters = synthetic_clusters(n, dim=8)
    for c in clusters:
        c["centroid"] = [0.010384, -0.000012, 0.5, 3.2e-05, 0.0, -0.25, 1e-06, 0.1]
        c["members"] = [(f"{c['cluster_id']}-p{i}", 0.000008 if i else 0.5) for i in range(2)]
    return clusters


def run_with(monkeypatch, execute_wrapper, clusters):
    con = core_db()
    con.execute("SET threads = 1")
    duck = duck_execute(con)
    monkeypatch.setattr(cluster, "fit_topics", lambda docs, emb: ([0] * len(docs), [1.0] * len(docs), {}))
    monkeypatch.setattr(cluster, "build_clusters", lambda *args: clusters)
    strict = execute_wrapper(duck)

    def execute(text, params):
        if text == cluster.load("cluster_posts"):
            return [{"post_id": f"p{i}", "text": "", "today": True, "embedding": [1.0, 0.0]}
                    for i in range(cluster.MIN_POSTS)]
        return strict(text, params)

    return con, strict, execute


def test_a_real_768_wide_payload_carries_numbers_in_exponent_form():
    rng = np.random.default_rng(7)
    posts = [{"post_id": f"p{i}", "today": True, "embedding": rng.normal(0, 0.036, 768).astype(np.float32).tolist(),
              "hashtags": [], "sound_id": None, "creator_id": None, "market": "za", "platform": "tiktok"}
             for i in range(60)]
    matrix = cluster.embedding_matrix(posts)
    clusters = cluster.build_clusters(posts, [0] * 60, [0.000009] * 60, {0: ["kota"]}, DAY, "za", matrix)
    [batch] = cluster.write_batches(cluster.plan(clusters, [{"kind": "new", "item_id": None, "cosine": None,
                                                             "votes": [], "candidates": []}], [], DAY, "za"))
    tokens = re.findall(r"-?\d+(?:\.\d+)?[eE][+-]?\d+", batch["map_rows"] + batch["cluster_rows"] + batch["member_rows"])
    assert tokens, "a 768 wide centroid has components under 1e-04, which Python writes as 3.2e-05"
    assert all(bq_number_refused(t) for t in tokens)


def test_the_checkpoint_as_it_was_is_refused_by_the_default_number_mode(monkeypatch):
    """The checkpoint script before this change, its PARSE_JSON calls in the default mode, on the same run."""
    def before(duck):
        def execute(text, params, max_bytes=None):
            return strict(text.replace(", wide_number_mode => 'round'", ""), params)

        strict = StrictJson(duck)
        return execute

    con, _, execute = run_with(monkeypatch, before, small_valued())
    with pytest.raises(BadRequest, match=r"cannot round-trip through string representation; error in PARSE_JSON"):
        cluster.run_cluster(execute, run_date=DAY, market="za")


def test_every_parse_json_of_the_checkpoint_asks_for_rounding():
    text = sql("cluster_checkpoint")
    calls = re.findall(r"PARSE_JSON\([^)]*\)", text)
    assert len(calls) == 4
    assert all(re.fullmatch(r"PARSE_JSON\(@\w+, wide_number_mode => 'round'\)", c) for c in calls)


def test_every_parse_json_of_a_bound_parameter_in_the_repo_asks_for_rounding():
    """A bound parameter that carries floats is refused by the default exact mode when a number cannot round-trip.
    Every PARSE_JSON(@name) in any sql file or python sql string under core, tests aside, must say
    wide_number_mode => 'round'."""
    root = Path(cluster.__file__).resolve().parents[1]
    bound = []
    for pattern in ("*.sql", "*.py"):
        for path in sorted(root.rglob(pattern)):
            if "tests" in path.relative_to(root).parts:
                continue
            for call in re.findall(r"PARSE_JSON\(\s*@\w+[^)]*\)", path.read_text(encoding="utf-8")):
                bound.append((path.relative_to(root).as_posix(), call))
    assert {name for name, _ in bound} >= {
        "understand/sql/cluster_checkpoint.sql", "eval/sql/weekly_quality_insert.sql", "eval/blind_test.py",
        "understand/embed.py", "understand/enrich.py"}
    unrounded = [(name, call) for name, call in bound
                 if not re.fullmatch(r"PARSE_JSON\(\s*@\w+,\s*wide_number_mode\s*=>\s*'round'\s*\)", call)]
    assert unrounded == []


def test_a_run_with_small_numbers_writes_its_clusters_once_the_checkpoint_rounds(monkeypatch):
    con, strict, execute = run_with(monkeypatch, StrictJson, small_valued())
    counts = cluster.run_cluster(execute, run_date=DAY, market="za")
    assert strict.refused == []
    assert "error" not in counts and counts["clusters"] == 2 and counts["members"] == 4
    assert con.execute(f"SELECT COUNT(*) FROM {CORE}.clusters WHERE cluster_date = ?", [DAY]).fetchone() == (2,)
    plan = con.execute("SELECT status FROM \"ogilvy-trends-v2\".intelligence_42_agent.runs "
                       "WHERE stage = 'understand_cluster_plan' ORDER BY status").fetchall()
    assert [s for (s,) in plan] == ["batch", "ready"]


# The 9 Oct counts, replayed through the job

def refused_market(market, **kw):
    raise BadRequest(ROUND_TRIP.format("0.010384"))


def test_the_9_oct_cluster_errors_make_the_run_partial_at_this_head(monkeypatch, capsys):
    """Evidence 9 Oct: ke, ng and pan hold only an error and the run was ok with no partial. The jobs image that ran
    (a80be1d) has no outcome rule in job.py. From d485d94 the same counts are partial."""
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)
    monkeypatch.setattr(job, "run_enrich", lambda execute, **kw: {"enriched": 3721})

    def run(execute, *, market, **kw):
        if market == "za":
            return {"market": "za", "posts": 12, "today_posts": 0, "skipped": "too_few_posts"}
        refused_market(market)

    monkeypatch.setattr(job, "run_cluster", run)
    assert job.main(execute=FakeWarehouse()) == 0
    counts = finished_run(log)[3]
    assert finished_run(log)[2] == "ok"
    assert counts["partial"] is True and counts["partial_reason"] == "cluster_stack_failed"
    assert "cannot round-trip through string representation" in counts["partial_error"]
    assert set(job.degraded_steps(counts)) == {"cluster:ke", "cluster:ng", "cluster:pan"}
    err = capsys.readouterr().err
    assert "understand partial: cluster_stack_failed" in err and "understand degraded:" in err
