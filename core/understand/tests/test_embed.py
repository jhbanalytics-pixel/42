import json
import re
import sys
import types
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from core.understand import embed, job

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
FILES = ("model", "embed", "index", "tvf_search_posts")
DAY = date(2026, 9, 28)


def sql(name):
    return (SQL_DIR / f"{name}.sql").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", FILES)
def test_sql_parses_as_bigquery(name):
    statements = [s for s in sqlglot.parse(sql(name), read="bigquery") if s is not None]
    assert statements
    if name != "index":
        # sqlglot has no grammar for CREATE VECTOR INDEX and keeps it as a Command; everything else must parse fully.
        assert not any(isinstance(s, exp.Command) for s in statements)


@pytest.mark.parametrize("name", FILES)
def test_every_create_is_if_not_exists(name):
    creates = re.findall(r"\bCREATE\s+(?:TEMP\s+)?(?:TABLE\s+FUNCTION|TABLE|MODEL|VECTOR\s+INDEX)\s+(IF\s+NOT\s+EXISTS)?",
                         sql(name), re.I)
    assert creates, f"{name}.sql creates nothing"
    assert all(creates), f"{name}.sql has a CREATE without IF NOT EXISTS"


@pytest.mark.parametrize("name", FILES)
def test_no_destructive_statement_or_expiry(name):
    text = sql(name)
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|REPLACE|MERGE|UPDATE)\b", text, re.I)
    assert not re.search(r"expir", text, re.I)


def test_model_is_remote_gemini_embedding_on_f42_vertex_in_us():
    text = sql("model")
    assert "`ogilvy-trends-v2.intelligence_42_core.embed_gemini`" in text
    assert re.search(r"REMOTE\s+WITH\s+CONNECTION\s+`ogilvy-trends-v2\.us\.f42-vertex`", text)
    assert re.search(r"ENDPOINT\s*=\s*'gemini-embedding-001'", text)


def test_embed_sql_appends_768_document_embeddings_with_named_params():
    text = sql("embed")
    params = set(re.findall(r"@(\w+)", text))
    assert params == {"from_date", "to_date", "max_rows"}
    assert not re.search(r"\{\w*\}|%s|%\(", text), "embed.sql must not be string-formatted"
    assert re.search(r"INSERT\s+INTO\s+`ogilvy-trends-v2\.intelligence_42_core\.post_enrichment`\s*\(post_id,\s*embedding\)", text)
    assert "768 AS output_dimensionality" in text
    assert "'RETRIEVAL_DOCUMENT' AS task_type" in text
    assert "TRUE AS flatten_json_output" in text
    assert "ml_generate_embedding_status" in text
    assert re.search(r"observed_date\s+BETWEEN\s+@from_date\s+AND\s+@to_date", text)
    assert not re.search(r"post_date\s+BETWEEN", text), "the window is the sighting day, not the publish day"
    assert re.search(r"LIMIT\s+@max_rows", text)
    assert "transcript" in text
    last = sqlglot.parse(text, read="bigquery")[-1]
    assert isinstance(last, exp.Select)
    assert {"embedded", "failed", "skipped", "chars_sent"} <= set(last.named_selects)


def test_embed_sql_comment_says_how_a_failed_post_is_retried():
    header = sql("embed").split("CREATE", 1)[0]
    assert "next run sends it again" not in header
    assert re.search(r"later sighting", header) and re.search(r"backfill", header, re.I)


PROJECT_DB = '"ogilvy-trends-v2"'
CORE = f"{PROJECT_DB}.intelligence_42_core"


def fixture_warehouse(posts, observations, embedded=()):
    import duckdb  # test-only: runs embed.sql, transpiled from BigQuery, on fixture tables

    con = duckdb.connect()
    con.execute(f"ATTACH ':memory:' AS {PROJECT_DB}")
    con.execute(f"CREATE SCHEMA {CORE}")
    con.execute(f"CREATE TABLE {CORE}.posts (post_id VARCHAR, text VARCHAR, transcript VARCHAR, post_date DATE)")
    con.execute(f"CREATE TABLE {CORE}.post_observations (post_id VARCHAR, observed_date DATE)")
    con.execute(f"CREATE TABLE {CORE}.post_enrichment (post_id VARCHAR, embedding DOUBLE[])")
    con.executemany(f"INSERT INTO {CORE}.posts VALUES (?, ?, ?, ?)", posts)
    con.executemany(f"INSERT INTO {CORE}.post_observations VALUES (?, ?)", observations)
    if embedded:
        con.executemany(f"INSERT INTO {CORE}.post_enrichment VALUES (?, ?)", [(pid, [0.5] * 768) for pid in embedded])
    return con


def good_vector(post_id):
    return [0.1] * 768, ""


def run_embed_sql(con, from_date, to_date, max_rows=50_000, model=good_vector):
    # Runs embed.sql on DuckDB with only ML.GENERATE_EMBEDDING stubbed: model(post_id) gives the vector and
    # status Vertex would return, and optionally its statistics JSON (NULL when not given), and embed.sql's own ok
    # test decides what is written. The stub passes the input columns through, as the model does.
    params = {"from_date": from_date, "to_date": to_date, "max_rows": max_rows}
    for statement in sqlglot.parse(sql("embed"), read="bigquery"):
        if "ML.GENERATE_EMBEDDING" in statement.sql("bigquery").upper():
            con.execute("CREATE TEMP TABLE model_out (post_id VARCHAR, content VARCHAR, "
                        "ml_generate_embedding_result DOUBLE[], ml_generate_embedding_status VARCHAR, "
                        "ml_generate_embedding_statistics JSON)")
            sent = con.execute("SELECT post_id, content FROM to_send").fetchall()
            if sent:
                con.executemany("INSERT INTO model_out VALUES (?, ?, ?, ?, ?)",
                                [(pid, content, *(tuple(model(pid)) + (None,))[:3]) for pid, content in sent])
            source = next(t for t in statement.find_all(exp.Table) if isinstance(t.this, exp.GenerateEmbedding))
            source.replace(exp.to_table("model_out").as_(source.alias))
        # sqlglot writes BigQuery's BYTE_LENGTH as OCTET_LENGTH, which DuckDB takes for BLOBs only; STRLEN is DuckDB's
        # UTF-8 byte count of a string.
        for node in list(statement.find_all(exp.ByteLength)):
            node.replace(exp.Anonymous(this="STRLEN", expressions=[node.this]))
        duck = statement.sql("duckdb")
        result = con.execute(duck, {k: v for k, v in params.items() if f"${k}" in duck})
    names = [d[0] for d in result.description]
    # DuckDB's COUNT_IF over no rows is NULL where BigQuery's COUNTIF is 0; run_embed reads NULL as 0 too.
    return {name: value or 0 for name, value in zip(names, result.fetchone())}


def stored_ids(con):
    return sorted(r[0] for r in con.execute(f"SELECT post_id FROM {CORE}.post_enrichment "
                                            "WHERE len(embedding) > 0").fetchall())


def test_a_post_published_yesterday_and_first_seen_today_is_embedded_today():
    con = fixture_warehouse(
        posts=[("seen_today", "Amapiano at the braai", None, DAY - timedelta(days=1)),
               ("seen_before", "old news", None, DAY),
               ("old_post", "Throwback", "a transcript", DAY - timedelta(days=400)),
               ("no_text", "  ", "", DAY),
               ("already", "done before", None, DAY - timedelta(days=1))],
        observations=[("seen_today", DAY), ("seen_before", DAY - timedelta(days=3)),
                      ("old_post", DAY), ("old_post", DAY), ("no_text", DAY), ("already", DAY)],
        embedded=["already"])
    counts = run_embed_sql(con, DAY, DAY)
    assert stored_ids(con) == ["already", "old_post", "seen_today"]
    assert counts == {"embedded": 2, "failed": 0, "skipped": 2,
                      "chars_sent": len("Amapiano at the braai") + len("Throwback\na transcript"), "tokens": 7 + 8}


def test_a_row_vertex_fails_is_counted_failed_and_not_written():
    con = fixture_warehouse(posts=[("p_ok", "Jollof wars", None, DAY), ("p_bad", "Load shedding memes", None, DAY)],
                            observations=[("p_ok", DAY), ("p_bad", DAY)])

    def model(post_id):
        return ([], "A retryable error occurred: RESOURCE_EXHAUSTED") if post_id == "p_bad" else good_vector(post_id)

    counts = run_embed_sql(con, DAY, DAY, model=model)
    assert counts == {"embedded": 1, "failed": 1, "skipped": 0,
                      "chars_sent": len("Jollof wars") + len("Load shedding memes"), "tokens": 4 + 7}
    assert stored_ids(con) == ["p_ok"]
    assert con.execute(f"SELECT count(*) FROM {CORE}.post_enrichment WHERE post_id = 'p_bad'").fetchone()[0] == 0


def test_window_posts_bounds_the_posts_scan_to_400_days_before_the_window():
    text = sql("embed")
    create = next(s for s in sqlglot.parse(text, read="bigquery")
                  if isinstance(s, exp.Create) and s.this.name == "window_posts")
    where = create.expression.args["where"].sql("bigquery")
    assert re.search(r"\w+\.post_date >= DATE_SUB\(@from_date, INTERVAL '?400'? DAY\)", where), where
    assert re.search(r"older than 400 days", text) and re.search(r"Stage 1", text)
    con = fixture_warehouse(
        posts=[("seen_today", "Amapiano at the braai", None, DAY - timedelta(days=1)),
               ("at_bound", "Throwback", None, DAY - timedelta(days=400)),
               ("too_old", "Ancient meme", None, DAY - timedelta(days=401))],
        observations=[("seen_today", DAY), ("at_bound", DAY), ("too_old", DAY)])
    counts = run_embed_sql(con, DAY, DAY)
    assert stored_ids(con) == ["at_bound", "seen_today"]
    assert counts == {"embedded": 2, "failed": 0, "skipped": 0,
                      "chars_sent": len("Amapiano at the braai") + len("Throwback"), "tokens": 7 + 3}


@pytest.mark.parametrize("chars_sent", [0, 700, 4_000_000, 40_000_000])
def test_model_usd_prices_tokens_not_chars_sent(chars_sent):
    assert embed.EMBED_USD_PER_MILLION_TOKENS == 0.15
    fake = FakeWarehouse(counts={"embedded": 1, "failed": 0, "skipped": 0, "chars_sent": chars_sent, "tokens": 0})
    assert embed.run_embed(fake, run_date=DAY)["model_usd"] == 0, "a character count says nothing of the bill"


def test_embed_sql_skips_a_post_embedded_already_on_a_second_run():
    con = fixture_warehouse(posts=[("p1", "Jollof wars", None, DAY)], observations=[("p1", DAY)])
    assert run_embed_sql(con, DAY, DAY)["embedded"] == 1
    again = con.cursor()
    assert run_embed_sql(again, DAY, DAY) == {"embedded": 0, "failed": 0, "skipped": 1, "chars_sent": 0, "tokens": 0}
    assert stored_ids(again) == ["p1"]


def test_embed_sql_sends_at_most_max_rows_per_slice():
    con = fixture_warehouse(posts=[(f"p{i}", f"post {i}", None, DAY) for i in range(5)],
                            observations=[(f"p{i}", DAY) for i in range(5)])
    counts = run_embed_sql(con, DAY, DAY, max_rows=3)
    assert counts["embedded"] == 3 and counts["chars_sent"] == 3 * len("post 0")
    assert len(stored_ids(con)) == 3


def test_the_vector_search_tvf_file_is_gone():
    # Staging refuses VECTOR_SEARCH inside a table function ("Unsupported query pattern in the 1st argument").
    assert not (SQL_DIR / "tvf_search_items.sql").exists()
    code = [" ".join(line.split("--", 1)[0] for line in sql(name).splitlines()) for name in FILES]
    assert not any("VECTOR_SEARCH" in text.upper() for text in code)


def tvf():
    return sqlglot.parse_one(sql("tvf_search_posts"), read="bigquery")


def test_tvf_signature_query_embedding_and_columns():
    text = sql("tvf_search_posts")
    assert re.search(r"CREATE\s+TABLE\s+FUNCTION\s+IF\s+NOT\s+EXISTS\s+"
                     r"`ogilvy-trends-v2\.intelligence_42_agent\.tvf_search_posts`\s*\(\s*q STRING,\s*market STRING,"
                     r"\s*since DATE,\s*until DATE,\s*k INT64\s*\)", text)
    assert "`ogilvy-trends-v2.intelligence_42_core.embed_gemini`" in text
    assert "'RETRIEVAL_QUERY' AS task_type" in text
    assert "768 AS output_dimensionality" in text
    assert "TRUE AS flatten_json_output" in text
    assert "@" not in text, "a table function takes named parameters, not query parameters"
    assert tvf().expression.named_selects == ["post_id", "distance", "platform", "url", "creator_id", "text",
                                              "published_at", "geo_market", "engagement"]


def test_tvf_header_says_exact_search_and_when_to_revisit_the_index():
    header = sql("tvf_search_posts").split("CREATE", 1)[0]
    assert re.search(r"exact", header, re.I) and re.search(r"brute.force", header, re.I)
    assert re.search(r"VECTOR_SEARCH outside a (table function|TVF)", header, re.I)
    assert re.search(r"hundred thousand", header, re.I)


def _selects(node):
    return [n for n in node.walk() if isinstance(n, exp.Select)]


def test_tvf_scores_exact_cosine_distance_over_embeddings_in_the_window_and_market():
    distances = [n for n in tvf().walk() if isinstance(n, exp.Anonymous) and n.name.upper() == "DISTANCE"]
    assert len(distances) == 1
    dot = distances[0].parent
    assert isinstance(dot, exp.Dot) and dot.this.sql("bigquery").upper() == "ML"
    assert distances[0].expressions[-1].sql("bigquery") == "'COSINE'"
    scoring = distances[0].find_ancestor(exp.Select)
    tables = {t.sql("bigquery").split(" AS ")[0] for t in scoring.find_all(exp.Table)}
    assert {"`ogilvy-trends-v2.intelligence_42_core.post_enrichment`",
            "`ogilvy-trends-v2.intelligence_42_core.posts`"} <= tables
    where = scoring.args["where"].sql("bigquery")
    assert re.search(r"ARRAY_LENGTH\(\w+\.embedding\) = 768", where)
    assert re.search(r"\w+\.post_date BETWEEN since AND until", where)
    assert re.search(r"market IS NULL OR \w+\.geo_market = market", where)


def test_tvf_reads_posts_once_inside_the_date_window():
    create = tvf()
    posts = [t for t in create.find_all(exp.Table)
             if t.sql("bigquery").split(" AS ")[0] == "`ogilvy-trends-v2.intelligence_42_core.posts`"]
    assert len(posts) == 1, "posts is joined again outside the date-pruned scoring CTE"
    scoring = posts[0].find_ancestor(exp.Select)
    assert re.search(r"\w+\.post_date BETWEEN since AND until", scoring.args["where"].sql("bigquery"))
    assert not create.expression.unnest().args.get("joins")


def test_tvf_header_notes_the_staging_copy_and_the_bytes_ceiling():
    header = " ".join(sql("tvf_search_posts").split("CREATE", 1)[0].replace("--", " ").split())
    assert "staging was created before this fix" in header
    assert "IF NOT EXISTS leaves it in place" in header and "Albert's step" in header
    assert "unpartitioned" in header
    assert "6.1 KB" in header and "2 GB" in header and "320,000" in header


def test_tvf_dedupes_by_post_id_after_scoring_then_keeps_k_last():
    create = tvf()
    distance = next(n for n in create.walk() if isinstance(n, exp.Anonymous) and n.name.upper() == "DISTANCE")
    scoring = distance.find_ancestor(exp.Select)
    assert scoring.args.get("group") is None and scoring.args.get("limit") is None
    dedupe = [s for s in _selects(create) if s.args.get("group") is not None]
    assert len(dedupe) == 1
    assert [g.sql("bigquery") for g in dedupe[0].args["group"].expressions] == ["post_id"]
    kept = {e.alias: e.this for e in dedupe[0].expressions if isinstance(e, exp.Alias)}
    assert isinstance(kept["distance"], exp.Min) and kept["distance"].this.sql("bigquery") == "distance"
    assert not any(isinstance(n, exp.Anonymous) and n.name.upper() == "DISTANCE" for n in dedupe[0].walk())
    windows = [n for n in create.walk() if isinstance(n, exp.Window)]
    assert len(windows) == 1 and isinstance(windows[0].this, exp.RowNumber)
    assert windows[0].args["order"].sql("bigquery") == "ORDER BY distance, post_id"
    assert windows[0].find_ancestor(exp.Select) is not dedupe[0]
    outer = create.expression.unnest()
    assert isinstance(outer, exp.Select) and outer is not dedupe[0]
    assert re.search(r"\b\w+\.rn <= k$", outer.args["where"].sql("bigquery")), "results are not cut to k last"
    assert outer.args.get("limit") is None


def _ancestors(node):
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def test_embedding_count_reads_non_empty_embeddings_only():
    text = sql("embedding_count")
    statement = sqlglot.parse_one(text, read="bigquery")
    assert isinstance(statement, exp.Select)
    assert statement.named_selects == ["n"]
    assert "`ogilvy-trends-v2.intelligence_42_core.post_enrichment`" in text
    assert "ARRAY_LENGTH(embedding) > 0" in statement.args["where"].sql("bigquery")
    code = " ".join(line.split("--", 1)[0] for line in text.splitlines())
    assert not re.search(r"\b(CREATE|INSERT|DROP|TRUNCATE|DELETE|REPLACE|MERGE|UPDATE)\b", code, re.I)


def test_index_comment_names_the_live_tvf():
    text = sql("index")
    assert "tvf_search_posts" in text and "tvf_search_items" not in text


def test_job_docstring_explains_a_backfill_on_a_day_already_run():
    doc = " ".join(job.__doc__.split())
    assert "FORCE_RERUN=1" in doc
    assert "start_next" in doc and "AlreadyDone" in doc


def test_job_usage_text_says_the_daily_run_enriches_and_a_backfill_does_not():
    usage = [line.strip() for line in job.__doc__.splitlines() if line.strip().startswith(("python -m", "EMBED_DAYS"))]
    daily, backfill = usage
    assert "embeds and enriches" in daily
    assert "enrich" in backfill and "skip" in backfill


def test_index_is_ivf_cosine_on_embedding():
    text = sql("index")
    assert re.search(r"ON\s+`ogilvy-trends-v2\.intelligence_42_core\.post_enrichment`\s*\(embedding\)", text)
    assert "index_type = 'IVF'" in text and "distance_type = 'COSINE'" in text


class FakeWarehouse:
    def __init__(self, counts=None, shape="rows", stored=5_000):
        self.calls = []
        # The counts row embed.sql returns; tokens as the real one reports them, 700 characters at four a token.
        self.counts = counts or {"embedded": 5, "failed": 1, "skipped": 2, "chars_sent": 700, "tokens": 175}
        self.shape = shape
        self.stored = stored

    def __call__(self, text, params, max_bytes=None):
        self.calls.append((text, params))
        if text == sql("embedding_count"):
            row = {"n": self.stored}
            return [row] if self.shape == "rows" else {"rows": [row]}
        if text != sql("embed"):
            return {"num_dml_affected_rows": None}
        row = dict(self.counts)
        return [row] if self.shape == "rows" else {"rows": [row], "num_dml_affected_rows": row["embedded"]}

    def embed_windows(self):
        return [(p["from_date"], p["to_date"]) for t, p in self.calls if t == sql("embed")]


def test_daily_run_order_and_window():
    fake = FakeWarehouse()
    counts = embed.run_embed(fake, run_date=DAY)
    assert [t for t, _ in fake.calls] == [sql("model"), sql("embed"), sql("embedding_count"), sql("index"),
                                          sql("tvf_search_posts")]
    assert fake.embed_windows() == [(DAY, DAY)]
    embed_params = [p for t, p in fake.calls if t == sql("embed")]
    assert embed_params == [{"from_date": DAY, "to_date": DAY, "max_rows": 50_000}]
    assert all(isinstance(p[k], date) for p in embed_params for k in ("from_date", "to_date"))
    assert all(p == {} for t, p in fake.calls if t != sql("embed"))
    assert counts == {"embedded": 5, "failed": 1, "skipped": 2, "chars_sent": 700, "tokens": 175,
                      "model_usd": 0.000026, "embedded_total": 5_000, "index": "created_or_exists"}


def test_slice_limit_is_a_parameter_passed_to_every_slice():
    assert embed.SLICE_LIMIT == 50_000
    fake = FakeWarehouse()
    embed.run_embed(fake, run_date=DAY, days=14, slice_limit=1_000)
    assert [p["max_rows"] for t, p in fake.calls if t == sql("embed")] == [1_000, 1_000]
    with pytest.raises(ValueError):
        embed.run_embed(FakeWarehouse(), run_date=DAY, slice_limit=0)


@pytest.mark.parametrize("shape", ["rows", "job"])
def test_index_is_deferred_while_no_embedding_is_stored(shape):
    # Staging: CREATE VECTOR INDEX fails while every embedding is NULL ("Failed to calculate array_min_len").
    fake = FakeWarehouse(counts={"embedded": 0, "failed": 4, "skipped": 0, "chars_sent": 90, "tokens": 23}, shape=shape,
                         stored=0)
    counts = embed.run_embed(fake, run_date=DAY)
    texts = [t for t, _ in fake.calls]
    assert sql("index") not in texts
    assert texts == [sql("model"), sql("embed"), sql("embedding_count"), sql("tvf_search_posts")]
    assert counts == {"embedded": 0, "failed": 4, "skipped": 0, "chars_sent": 90, "tokens": 23, "model_usd": 0.000003,
                      "embedded_total": 0, "index": "deferred_no_embeddings"}


@pytest.mark.parametrize("stored", [1, 2_931, 4_999])
def test_index_is_deferred_below_5000_embeddings_and_the_search_still_gets_built(stored):
    # Staging: CREATE VECTOR INDEX with IVF refuses a table under 5,000 rows ("Total rows 2931 is smaller than min
    # allowed 5000"). tvf_search_posts scores exact cosine and needs no index.
    fake = FakeWarehouse(shape="job", stored=stored)
    counts = embed.run_embed(fake, run_date=DAY)
    texts = [t for t, _ in fake.calls]
    assert sql("index") not in texts
    assert texts[-2:] == [sql("embedding_count"), sql("tvf_search_posts")]
    assert counts["index"] == "deferred_below_5000" and counts["embedded_total"] == stored


def test_index_is_created_once_5000_embeddings_are_stored():
    assert embed.INDEX_MIN_ROWS == 5_000
    fake = FakeWarehouse(shape="job", stored=5_000)
    counts = embed.run_embed(fake, run_date=DAY)
    assert [t for t, _ in fake.calls][-2:] == [sql("index"), sql("tvf_search_posts")]
    assert counts["index"] == "created_or_exists"


def test_a_failed_index_creation_is_recorded_and_the_embed_run_goes_on():
    class BadRequest(Exception):
        pass

    class Refusing(FakeWarehouse):
        def __call__(self, text, params, max_bytes=None):
            if text == sql("index"):
                self.calls.append((text, params))
                raise BadRequest("400 the vector index could not be created")
            return super().__call__(text, params, max_bytes)

    fake = Refusing(stored=5_001)
    counts = embed.run_embed(fake, run_date=DAY)
    assert [t for t, _ in fake.calls][-2:] == [sql("index"), sql("tvf_search_posts")]
    assert counts["index"] == "failed"
    assert counts["index_error"] == "BadRequest: 400 the vector index could not be created"
    assert counts["embedded"] == 5 and counts["embedded_total"] == 5_001


def test_backfill_is_90_days_in_7_day_slices_newest_first():
    fake = FakeWarehouse(shape="job")
    counts = embed.run_embed(fake, run_date=DAY, days=90)
    texts = [t for t, _ in fake.calls]
    assert texts.count(sql("model")) == 1 and texts[0] == sql("model")
    windows = fake.embed_windows()
    assert len(windows) == 13
    assert windows[0] == (DAY - timedelta(days=6), DAY)
    assert all(start <= end and (end - start).days < 7 for start, end in windows)
    assert all(later[0] == earlier[1] + timedelta(days=1) for later, earlier in zip(windows, windows[1:]))
    covered = {start + timedelta(days=i) for start, end in windows for i in range((end - start).days + 1)}
    assert covered == {DAY - timedelta(days=i) for i in range(90)}
    assert sum((end - start).days + 1 for start, end in windows) == 90
    assert counts == {"embedded": 65, "failed": 13, "skipped": 26, "chars_sent": 9_100, "tokens": 2_275,
                      "model_usd": 0.000341, "embedded_total": 5_000, "index": "created_or_exists"}


def test_run_embed_refuses_a_bad_window():
    with pytest.raises(ValueError):
        embed.run_embed(FakeWarehouse(), run_date=DAY, days=0)


def test_run_embed_fails_loudly_without_a_counts_row():
    def empty(text, params):
        return []

    with pytest.raises(RuntimeError):
        embed.run_embed(empty, run_date=DAY)


def test_job_exits_clearly_when_chain_helper_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.collect.chain", None)
    fake = FakeWarehouse()
    with pytest.raises(SystemExit) as err:
        job.main(execute=fake)
    assert "evening merge" in str(err.value.code)
    assert fake.calls == []


@pytest.fixture(autouse=True)
def job_clock(monkeypatch):
    """The job's clock at 10:00 SAST on DAY, the run date fake_chain hands out, so enrichment runs."""
    monkeypatch.setattr(job, "now", lambda: datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc), raising=False)


@dataclass
class FakeRun:
    run_id: str
    stage: str
    run_date: date


def fake_chain(monkeypatch, begin_raises=None):
    mod = types.ModuleType("core.collect.chain")
    log = []

    class UpstreamNotReady(Exception):
        def __init__(self, message, run):
            super().__init__(message)
            self.run = run

    class AlreadyDone(Exception):
        def __init__(self, message, run):
            super().__init__(message)
            self.run = run

    def begin(stage, run_date=None, **kw):
        log.append(("begin", stage))
        run = FakeRun("understand-1", stage, DAY)
        if begin_raises:
            raise getattr(mod, begin_raises)("refused", run)
        return run

    def finish(run, status, counts, error=None, **kw):
        log.append(("finish", run.run_id, status, counts, error))

    def start_next(stage, run_date=None, **kw):
        log.append(("start_next", stage, run_date))
        return "f42-detect"

    def restart_next(stage, run_date=None, **kw):
        return None  # no runs store here, so there is never a lost start to restart

    mod.UpstreamNotReady, mod.AlreadyDone = UpstreamNotReady, AlreadyDone
    mod.begin, mod.finish, mod.start_next, mod.restart_next = begin, finish, start_next, restart_next
    monkeypatch.setitem(sys.modules, "core.collect.chain", mod)
    return log


def test_model_cap_readback_uses_the_current_sast_date_without_starting_a_run(monkeypatch, capsys):
    from core.config import caps

    instant = datetime(2026, 10, 1, 0, 30, tzinfo=timezone.utc)
    real_model_daily_usd = caps.model_daily_usd
    expected = real_model_daily_usd(now=instant)
    assert expected == 50.0
    cap_reads, work_calls = [], []

    def read_cap(*, now):
        cap_reads.append(now)
        return real_model_daily_usd(now=now)

    monkeypatch.setattr(caps, "model_daily_usd", read_cap)
    monkeypatch.setattr(job, "now", lambda: instant)
    monkeypatch.setattr(sys, "argv", ["core.understand.job", "--model-cap-readback"])
    chain_log = fake_chain(monkeypatch, begin_raises="AlreadyDone")
    for name in ("bigquery_execute", "run_embed", "run_enrich", "run_cluster"):
        monkeypatch.setattr(job, name, lambda *args, _name=name, **kwargs: work_calls.append(_name))

    assert job.main() == 0
    assert cap_reads == [instant]
    assert capsys.readouterr().out == "model_daily_usd=50.00 sast_date=2026-10-01\n"
    assert chain_log == [] and work_calls == []


def memory_chain(monkeypatch):
    from core.collect import chain

    monkeypatch.setenv("CHAIN_UNDERSTAND", "1")
    runs = chain.MemoryRunsStore()
    collect = chain.begin("collect", DAY, runs=runs)
    chain.finish(collect, "ok", {}, runs=runs)
    monkeypatch.setattr(chain, "default_runs", lambda: runs)
    monkeypatch.setattr(chain, "today", lambda: DAY)
    downstream = []
    monkeypatch.setattr(chain, "start_next", lambda stage, run_date=None, **kw: downstream.append((stage, run_date)))
    return chain, runs, downstream


def test_job_runs_understand_then_starts_detect(monkeypatch):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)
    fake = FakeWarehouse()
    assert job.main(execute=fake) == 0
    step_seconds = log[1][3]["step_seconds"]
    assert set(step_seconds) == {"embed", "enrich", "clustering"}
    assert all(isinstance(value, (int, float)) and value >= 0 for value in step_seconds.values())
    expected_counts = {"embedded": 5, "failed": 1, "skipped": 2, "chars_sent": 700, "tokens": 175, "model_usd": 0.0,
                       "booked_model_usd": 0.000026, "embedded_total": 5_000, "index": "created_or_exists", "enriched": 0,
                       "enrich_failed": 0, "enrich_skipped": 0, "enrich_batch_collected": 0,
                       "enrich_batch_pending": 0, "enrich_candidate": 0, "enrich_planned": 0, "enrich_attempted": 0,
                       "enrich_deferred": 0, "enrich_time_budget_exhausted": False, "enrich_elapsed": 0.0,
                       "enrich_unknown_spend": False, "enrich_unknown_spend_estimate_usd": 0.0,
                       "cluster": {m: {"posts": 0, "today_posts": 0, "skipped": "too_few_posts"}
                                   for m in ("za", "ng", "ke", "pan")},
                       "video": {"off": True}}
    assert log[0] == ("begin", "understand")
    assert log[1][:3] == ("finish", "understand-1", "ok")
    assert {key: value for key, value in log[1][3].items() if key != "step_seconds"} == expected_counts
    assert log[1][4] is None
    assert log[2] == ("start_next", "understand", DAY)
    assert fake.embed_windows() == [(DAY, DAY)]
    texts = [t for t, _ in fake.calls]
    booked = [p for t, p in fake.calls if t == embed.SPEND_ROW_SQL]
    assert [json.loads(p["counts"]) for p in booked] == [{"model_usd": 15.36, "what": "embed_ceiling"},
                                                         {"model_usd": -15.359974, "what": "embed_correction"}]
    assert round(sum(json.loads(p["counts"])["model_usd"] for p in booked), 6) == 0.000026
    rows = [i for i, t in enumerate(texts) if t == embed.SPEND_ROW_SQL]
    assert rows == [texts.index(sql("embed")) - 1, texts.index(sql("embed")) + 1], \
        "the window's ceiling is booked before it is sent and corrected right after, before enrichment reads spend"


def test_job_phase_times_flush_safe_stdout_markers_on_an_enrichment_error(monkeypatch):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    monkeypatch.setattr(job, "peak_rss_mb", lambda: None)  # where it cannot be read; its field is tested on its own
    log = fake_chain(monkeypatch)
    instant = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)
    moments = iter([instant, instant + timedelta(seconds=62), instant + timedelta(minutes=2),
                    instant + timedelta(minutes=4, seconds=30), instant + timedelta(minutes=5),
                    instant + timedelta(minutes=9, seconds=15)])
    monkeypatch.setattr(job, "now", lambda: next(moments))
    phases, bookings = [], []
    monkeypatch.setattr(job, "run_embed", lambda execute, **kwargs: phases.append("embed") or {
        "embedded": 3, "model_usd": 1.0, "booked_usd": 0.75})
    monkeypatch.setattr(job, "book_spend", lambda execute, **kwargs: bookings.append(kwargs) or kwargs["usd"])

    def fail_enrichment(execute, **kwargs):
        phases.append("enrich")
        error = RuntimeError("PRIVATE_POST_TEXT credential=private-value https://private.example/post")
        error.enrich_counts = {"enriched": 7, "model_usd": 0.5, "booked_usd": 0.25}
        raise error

    def cluster(execute, *, market, **kwargs):
        phases.append(f"cluster_{market}")
        return {"market": market, "posts": 1, "model_usd": 0.2, "booked_usd": 0.1}

    monkeypatch.setattr(job, "run_enrich", fail_enrichment)
    monkeypatch.setattr(job, "run_cluster", cluster)
    class FlushTrackingStream:
        def __init__(self):
            self.parts, self.flush_count = [], 0

        def write(self, value):
            self.parts.append(value)
            return len(value)

        def flush(self):
            self.flush_count += 1

        def getvalue(self):
            return "".join(self.parts)

    stream = FlushTrackingStream()
    monkeypatch.setattr(sys, "stdout", stream)

    assert job.main(execute=lambda text, params: []) == 0
    assert phases == ["embed", "enrich", "cluster_za", "cluster_ng", "cluster_ke", "cluster_pan"]
    counts = next(entry[3] for entry in log if entry[0] == "finish")
    assert counts["step_seconds"] == {"embed": 62.0, "enrich": 150.0, "clustering": 255.0}
    assert counts["enriched"] == 7 and counts["enrich_error"].startswith("RuntimeError:")
    assert counts["model_usd"] == 0.65 and counts["booked_model_usd"] == 1.65
    assert bookings[0]["usd"] == 0.25
    assert stream.flush_count == 6
    assert stream.getvalue().splitlines() == [
        "understand_phase run_id=understand-1 phase=embed event=start elapsed_seconds=0.000",
        "understand_phase run_id=understand-1 phase=embed event=end elapsed_seconds=62.000",
        "understand_phase run_id=understand-1 phase=enrich event=start elapsed_seconds=0.000",
        "understand_phase run_id=understand-1 phase=enrich event=end elapsed_seconds=150.000",
        "understand_phase run_id=understand-1 phase=clustering event=start elapsed_seconds=0.000",
        "understand_phase run_id=understand-1 phase=clustering event=end elapsed_seconds=255.000",
    ]


def test_book_spend_appends_one_ok_understand_spend_row_and_books_no_credit():
    text = embed.SPEND_ROW_SQL
    assert re.match(r"\s*INSERT INTO `ogilvy-trends-v2\.intelligence_42_agent\.runs`", text)
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|REPLACE|MERGE|UPDATE|ALTER)\b", text, re.I)
    assert "'understand_spend'" in text and "'ok'" in text
    assert set(re.findall(r"@(\w+)", text)) == {"run_id", "run_date", "counts"}
    calls = []
    booked = embed.book_spend(lambda t, p: calls.append((t, p)), run_id="understand-1", run_date=DAY, usd=0.25,
                              what="embed")
    assert booked == 0.25
    [(t, params)] = calls
    assert t == text and params["run_date"] == DAY and params["run_id"].startswith("understand-1-spend-")
    assert json.loads(params["counts"]) == {"model_usd": 0.25, "what": "embed"}
    embed.book_spend(lambda t, p: calls.append((t, p)), run_id="understand-1", run_date=DAY, usd=0.25, what="embed")
    assert calls[1][1]["run_id"] != params["run_id"], "each booking is its own row"
    for amount in (0, 0.0, -0.4):
        assert embed.book_spend(lambda t, p: calls.append((t, p)), run_id="understand-1", run_date=DAY,
                                usd=amount, what="embed") == 0.0
    assert len(calls) == 2, "nothing, and never a credit, is booked as it happens"


def test_job_backfill_mode_from_env(monkeypatch):
    monkeypatch.setenv("EMBED_DAYS", "90")
    fake_chain(monkeypatch)
    fake = FakeWarehouse()
    assert job.main(execute=fake) == 0
    assert len(fake.embed_windows()) == 13


def test_job_records_failure_and_does_not_start_detect(monkeypatch, capsys):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    monkeypatch.setattr(job, "peak_rss_mb", lambda: None)  # where it cannot be read; its field is tested on its own
    log = fake_chain(monkeypatch)

    def broken(text, params):
        raise RuntimeError("connection f42-vertex not found")

    with pytest.raises(RuntimeError):
        job.main(execute=broken)
    assert log[-1][:3] == ("finish", "understand-1", "failed")
    assert "f42-vertex" in log[-1][4]
    assert set(log[-1][3]["step_seconds"]) == {"embed"}
    assert all(isinstance(value, (int, float)) and value >= 0 for value in log[-1][3]["step_seconds"].values())
    assert capsys.readouterr().out.splitlines() == [
        "understand_phase run_id=understand-1 phase=embed event=start elapsed_seconds=0.000",
        "understand_phase run_id=understand-1 phase=embed event=end elapsed_seconds=0.000",
    ]
    assert not any(entry[0] == "start_next" for entry in log)


def test_a_refused_ceiling_booking_sends_nothing_and_stops_soft_like_an_unreadable_spend(monkeypatch):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)

    class LedgerDown(FakeWarehouse):
        def __call__(self, text, params, max_bytes=None):
            if text == embed.SPEND_ROW_SQL:
                raise RuntimeError("runs append refused")
            return super().__call__(text, params, max_bytes)

    fake = LedgerDown()
    assert job.main(execute=fake) == 0
    assert fake.embed_windows() == [], "a window whose ceiling could not be booked is never sent"
    counts = log[1][3]
    assert log[1][:3] == ("finish", "understand-1", "ok")
    assert counts["spend_unknown"] is True and counts["spend_error"] == "RuntimeError" and counts["embedded"] == 0
    assert counts["model_usd"] == 0.0 and counts["booked_model_usd"] == 0.0
    assert log[2] == ("start_next", "understand", DAY)


@pytest.mark.parametrize("refusal,code", [("AlreadyDone", 0), ("UpstreamNotReady", 1)])
def test_job_refused_by_chain_does_no_work(monkeypatch, refusal, code):
    log = fake_chain(monkeypatch, begin_raises=refusal)
    fake = FakeWarehouse()
    assert job.main(execute=fake) == code
    assert fake.calls == []
    assert log == [("begin", "understand")]



def test_a_rerun_after_detect_failed_to_start_starts_it_without_redoing_understand(monkeypatch):
    from core.collect import chain

    monkeypatch.delenv("EMBED_DAYS", raising=False)
    for name in ("FORCE_RERUN", "CLOUD_RUN_EXECUTION", "RUN_DATE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CHAIN_UNDERSTAND", "1")
    runs = chain.MemoryRunsStore()
    collect = chain.begin("collect", DAY, runs=runs)
    chain.finish(collect, "ok", {}, runs=runs)
    monkeypatch.setattr(chain, "default_runs", lambda: runs)
    monkeypatch.setattr(chain, "today", lambda: DAY)
    posts = []

    class AdminApi:
        def run(self, job_name, env):
            posts.append((job_name, env))
            if len(posts) == 1:
                raise RuntimeError("503 from the Admin API")
            return {}

    monkeypatch.setattr(chain, "CloudRunJobs", AdminApi)
    with pytest.raises(RuntimeError, match="503"):
        job.main(execute=FakeWarehouse())
    assert runs.latest("understand", DAY)["status"] == "ok" and runs.latest("detect", DAY) is None
    again = FakeWarehouse()
    assert job.main(execute=again) == 0
    assert again.calls == [], "understand already ran ok, so nothing is embedded or booked again"
    assert posts == [("f42-detect", {"RUN_DATE": DAY.isoformat()})] * 2
    runs.append({"run_id": "detect-x", "stage": "detect", "run_date": DAY.isoformat(), "status": "running",
                 "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None, "counts": None,
                 "error": None})
    assert job.main(execute=FakeWarehouse()) == 0
    assert len(posts) == 2, "a detect that has a runs row is never started again"


# Embed spend per window, on today's date, inside the daily cap

W1 = (DAY - timedelta(days=6), DAY)  # the newest 7-day window of a backfill


class FailsOnSecondWindow(FakeWarehouse):
    def __call__(self, text, params, max_bytes=None):
        if text == sql("embed") and len(self.embed_windows()) == 1:
            self.calls.append((text, params))
            raise RuntimeError("window two refused")
        return super().__call__(text, params, max_bytes)


def test_each_window_is_booked_as_it_finishes_so_a_later_failure_keeps_it():
    booked = []
    with pytest.raises(RuntimeError) as err:
        embed.run_embed(FailsOnSecondWindow(), run_date=DAY, days=14,
                        book=lambda usd, what: booked.append((usd, what)) or usd)
    assert booked == [(15.36, "embed_ceiling"), (-15.359974, "embed_correction"), (15.36, "embed_ceiling")], \
        "window one was booked and corrected before window two was sent; window two's ceiling stays booked"
    kept = err.value.embed_counts
    assert kept["embedded"] == 5 and kept["model_usd"] == 0.000026 and kept["booked_usd"] == 15.360026


def test_per_window_bookings_add_up_to_the_runs_spend_with_no_rounding_left_over():
    booked = []
    counts = embed.run_embed(FakeWarehouse(), run_date=DAY, days=90, book=lambda usd, what: booked.append(usd) or usd)
    assert len(booked) == 26 and round(sum(booked), 6) == counts["model_usd"] == counts["booked_usd"] == 0.000341


def test_the_job_keeps_the_spend_of_windows_before_a_failure_booked(monkeypatch):
    monkeypatch.setenv("EMBED_DAYS", "14")
    log = fake_chain(monkeypatch)
    fake = FailsOnSecondWindow()
    with pytest.raises(RuntimeError):
        job.main(execute=fake)
    assert log[-1][:3] == ("finish", "understand-1", "failed")
    assert log[-1][3]["embedded"] == 5
    assert log[-1][3]["booked_model_usd"] == 15.360026 and log[-1][3]["model_usd"] == 0.0, \
        "window one is booked and window two's ceiling too, so the failed row carries none of it again"
    assert [json.loads(p["counts"]) for t, p in fake.calls if t == embed.SPEND_ROW_SQL] == [
        {"model_usd": 15.36, "what": "embed_ceiling"}, {"model_usd": -15.359974, "what": "embed_correction"},
        {"model_usd": 15.36, "what": "embed_ceiling"}]


def test_a_backfill_books_its_embed_spend_on_todays_date_not_the_run_date(monkeypatch):
    monkeypatch.setenv("EMBED_DAYS", "14")
    log = fake_chain(monkeypatch)
    monkeypatch.setattr(job, "now", lambda: datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))
    fake = FakeWarehouse()
    assert job.main(execute=fake) == 0
    booked = [p for t, p in fake.calls if t == embed.SPEND_ROW_SQL]
    assert [p["run_date"] for p in booked] == [date(2026, 10, 5)] * 4, "the cap reads today, so the spend goes there"
    assert log[1][3]["booked_model_usd"] == embed._usd(2 * 175) == 0.000053
    assert log[1][3]["model_usd"] == 0.0 and log[1][3]["enrich_skipped_backfill"] is True


def test_run_embed_checks_the_cap_before_each_window_and_stops_when_it_is_spent():
    reads = iter([(19.0, 20.0), (20.0, 20.0)])
    fake = FakeWarehouse()
    counts = embed.run_embed(fake, run_date=DAY, days=14, spent_today=lambda: next(reads))
    assert fake.embed_windows() == [W1]
    assert counts["cap_limited"] is True and counts["embedded"] == 5 and "spend_unknown" not in counts
    assert [t for t, _ in fake.calls][-2:] == [sql("index"), sql("tvf_search_posts")], "the search still gets built"


def test_a_window_that_would_pass_the_cap_is_cut_to_the_posts_that_fit_at_2048_tokens():
    # At 19.9 of 20 spent, USD 0.1 is left: 325 posts at 2,048 tokens (USD 0.0003072 each), not 50,000.
    reads = iter([(0.0, 20.0), (19.9, 20.0)])
    fake = FakeWarehouse()
    counts = embed.run_embed(fake, run_date=DAY, days=14, spent_today=lambda: next(reads))
    assert [p["max_rows"] for t, p in fake.calls if t == sql("embed")] == [embed.SLICE_LIMIT, 325]
    assert counts["cap_limited"] is True and counts["embedded"] == 10


def test_the_morning_window_keeps_slice_limit_and_is_not_cap_limited():
    fake = FakeWarehouse()
    counts = embed.run_embed(fake, run_date=DAY, spent_today=lambda: (0.0, 20.0))
    assert [p["max_rows"] for t, p in fake.calls if t == sql("embed")] == [embed.SLICE_LIMIT]
    assert "cap_limited" not in counts
    fake = FakeWarehouse()
    counts = embed.run_embed(fake, run_date=DAY, slice_limit=1_000, spent_today=lambda: (19.0, 20.0))
    assert [p["max_rows"] for t, p in fake.calls if t == sql("embed")] == [1_000] and "cap_limited" not in counts, \
        "a smaller window that fits whole is sent whole"


def test_run_embed_stops_when_todays_spend_cannot_be_read():
    reads = iter([(0.0, 20.0)])
    fake = FakeWarehouse()
    counts = embed.run_embed(fake, run_date=DAY, days=14, spent_today=lambda: next(reads))
    assert fake.embed_windows() == [W1], "the second read raises StopIteration, so window two is never sent"
    assert counts["spend_unknown"] is True and counts["spend_error"] == "StopIteration"
    assert "cap_limited" not in counts


def test_the_job_reads_todays_spend_before_embedding_and_embeds_nothing_when_it_cannot(monkeypatch):
    from core.agent.ask import SPEND_SQL

    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)

    class SpendUnreadable(FakeWarehouse):
        def __call__(self, text, params, max_bytes=None):
            if text == SPEND_SQL:
                self.calls.append((text, params))
                raise RuntimeError("runs is unreachable")
            return super().__call__(text, params, max_bytes)

    fake = SpendUnreadable()
    assert job.main(execute=fake) == 0
    assert fake.embed_windows() == []
    spend_reads = [p for t, p in fake.calls if t == SPEND_SQL]
    assert spend_reads[0] == {"day": DAY}
    counts = log[1][3]
    assert counts["spend_unknown"] is True and counts["spend_error"] == "RuntimeError" and counts["embedded"] == 0
    assert log[2] == ("start_next", "understand", DAY)


# The window is sized from the room left, and its ceiling is booked before it is sent

class FullPosts(FakeWarehouse):
    """Every window sends as many posts as max_rows allows, each cut at 4,000 characters and billing the model's full
    2,048-token input: the most it can cost."""

    def __call__(self, text, params, max_bytes=None):
        if text == sql("embed"):
            n = params["max_rows"]
            self.counts = {"embedded": n, "failed": 0, "skipped": 0, "chars_sent": n * 4_000, "tokens": n * 2_048}
        return super().__call__(text, params, max_bytes)


def test_embedding_stops_before_the_next_window_when_its_spend_day_changes():
    spend_day = date(2026, 10, 2)
    started = datetime(2026, 10, 2, 21, 50, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 10, 2, 22, 10, tzinfo=timezone.utc)
    current = [started]
    booked = []

    class CrossesMidnight(FullPosts):
        def __call__(self, text, params, max_bytes=None):
            result = super().__call__(text, params, max_bytes)
            if text == sql("embed"):
                current[0] = after_midnight
            return result

    fake = CrossesMidnight()
    counts = embed.run_embed(fake, run_date=DAY, days=14, spend_day=spend_day, clock=lambda: current[0],
                             spent_today=lambda: (19.0, 40.0),
                             book=lambda usd, what: booked.append((usd, what, spend_day)) or usd)

    assert len(fake.embed_windows()) == 1
    assert counts["day_changed"] is True
    assert booked[0] == (15.36, "embed_ceiling", spend_day)
    assert booked[1] == (0.0, "embed_correction", spend_day)


@pytest.mark.parametrize("rollover_during", ["spend_read", "ceiling_booking"])
def test_embedding_does_not_start_work_when_its_spend_day_changes_before_the_query(rollover_during):
    spend_day = date(2026, 10, 2)
    started = datetime(2026, 10, 2, 21, 50, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 10, 2, 22, 10, tzinfo=timezone.utc)
    current = [started]
    booked = []

    def spent_today():
        if rollover_during == "spend_read":
            current[0] = after_midnight
        return 0.0, 40.0

    def book(amount, what):
        booked.append((amount, what, spend_day))
        if rollover_during == "ceiling_booking" and what == "embed_ceiling":
            current[0] = after_midnight
        return amount

    fake = FullPosts()
    counts = embed.run_embed(fake, run_date=DAY, days=14, spend_day=spend_day, clock=lambda: current[0],
                             spent_today=spent_today, book=book)

    assert fake.embed_windows() == []
    assert counts["day_changed"] is True
    if rollover_during == "spend_read":
        assert booked == []
    else:
        assert booked == [(15.36, "embed_ceiling", spend_day), (-15.36, "embed_correction", spend_day)]


class Killed(BaseException):
    """The process dying: nothing in run_embed or the job catches it, so no step after it runs."""


def test_embed_sql_keeps_4000_characters_and_the_ceiling_prices_the_tokens_they_can_bill_not_the_characters():
    assert "), 4000) AS content" in sql("embed") and embed.MAX_CHARS_PER_POST == 4_000
    assert embed.POST_CEILING_USD > embed._usd(4_000 / embed.CHARS_PER_TOKEN), \
        "a 4,000-character isiZulu or emoji-heavy post bills more than four characters a token"
    assert round(embed.SLICE_LIMIT * embed.POST_CEILING_USD, 6) == 15.36, "one full window can cost USD 15.36"


def test_2048_token_posts_at_19_of_20_spent_keep_the_day_at_or_under_the_cap():
    booked = []
    fake = FullPosts()
    counts = embed.run_embed(fake, run_date=DAY, spent_today=lambda: (19.0 + sum(booked), 20.0),
                             book=lambda usd, what: booked.append(usd) or usd)
    assert [p["max_rows"] for t, p in fake.calls if t == sql("embed")] == [3_255]
    assert counts["cap_limited"] is True and counts["model_usd"] == 0.999936
    assert 19.0 + counts["model_usd"] <= 20.0 and round(19.0 + sum(booked), 6) <= 20.0


def test_a_backfill_of_full_windows_stops_at_the_cap_counting_its_own_ceilings():
    booked = []
    fake = FullPosts()
    counts = embed.run_embed(fake, run_date=DAY, days=90, spent_today=lambda: (sum(booked), 20.0),
                             book=lambda usd, what: booked.append(usd) or usd)
    assert [p["max_rows"] for t, p in fake.calls if t == sql("embed")] == [50_000, 15_104]
    assert counts["cap_limited"] is True and counts["model_usd"] == 19.999949 <= 20.0
    assert round(sum(booked), 6) == counts["model_usd"]


def test_a_window_with_room_for_no_post_is_not_sent_and_books_nothing():
    booked = []
    fake = FakeWarehouse()
    counts = embed.run_embed(fake, run_date=DAY, spent_today=lambda: (19.99999, 20.0),
                             book=lambda usd, what: booked.append(usd) or usd)
    assert fake.embed_windows() == [] and booked == []
    assert counts["cap_limited"] is True and counts["embedded"] == 0


def test_a_windows_ceiling_is_booked_before_it_is_sent_and_corrected_to_its_spend_after():
    order, fake = [], FakeWarehouse()

    def execute(text, params, max_bytes=None):
        if text == sql("embed"):
            order.append("embed")
        return fake(text, params, max_bytes)

    counts = embed.run_embed(execute, run_date=DAY, book=lambda usd, what: order.append((what, usd)) or usd)
    assert order == [("embed_ceiling", 15.36), "embed", ("embed_correction", -15.359974)]
    assert counts["booked_usd"] == counts["model_usd"] == 0.000026


def test_a_ceiling_that_cannot_be_booked_stops_the_run_before_the_window_is_sent():
    def refuse(usd, what):
        raise RuntimeError("runs append refused")

    fake = FakeWarehouse()
    counts = embed.run_embed(fake, run_date=DAY, days=14, book=refuse, spent_today=lambda: (0.0, 20.0))
    assert fake.embed_windows() == []
    assert counts["spend_unknown"] is True and counts["spend_error"] == "RuntimeError"
    assert counts["model_usd"] == counts["booked_usd"] == 0.0 and "cap_limited" not in counts


def test_a_kill_after_a_window_ran_leaves_its_ceiling_booked(monkeypatch):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)

    class KilledAfterWindow(FakeWarehouse):
        def __call__(self, text, params, max_bytes=None):
            result = super().__call__(text, params, max_bytes)
            if text == sql("embed"):
                raise Killed()  # the window ran and billed; the process dies before its correction is booked
            return result

    fake = KilledAfterWindow()
    with pytest.raises(Killed):
        job.main(execute=fake)
    assert [json.loads(p["counts"]) for t, p in fake.calls if t == embed.SPEND_ROW_SQL] == [
        {"model_usd": 15.36, "what": "embed_ceiling"}], "the cap sees at least the window's true spend"
    assert log == [("begin", "understand")], "no understand row is written, so the ledger row alone holds it"


def test_the_correction_brings_the_day_to_the_true_spend(monkeypatch):
    monkeypatch.setenv("EMBED_DAYS", "14")
    log = fake_chain(monkeypatch)
    fake = FakeWarehouse()
    assert job.main(execute=fake) == 0
    booked = [json.loads(p["counts"])["model_usd"] for t, p in fake.calls if t == embed.SPEND_ROW_SQL]
    assert booked == [15.36, -15.359974, 15.36, -15.359973]
    counts = log[1][3]
    assert round(sum(booked) + counts["model_usd"], 6) == embed._usd(2 * 175) == counts["booked_model_usd"]


def test_a_failed_backfill_leaves_no_embed_spend_on_its_run_date(monkeypatch):
    # Window one's correction cannot be booked. Its ceiling stays booked today, above the true spend, and the failed
    # understand row, dated RUN_DATE, carries no spend on a day the cap never reads.
    monkeypatch.setenv("EMBED_DAYS", "14")
    log = fake_chain(monkeypatch)
    monkeypatch.setattr(job, "now", lambda: datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))

    class CorrectionRefused(FakeWarehouse):
        def __call__(self, text, params, max_bytes=None):
            if text == embed.SPEND_ROW_SQL and json.loads(params["counts"])["what"] != "embed_ceiling":
                raise RuntimeError("runs append refused")
            return super().__call__(text, params, max_bytes)

    fake = CorrectionRefused()
    with pytest.raises(RuntimeError):
        job.main(execute=fake)
    assert log[-1][:3] == ("finish", "understand-1", "failed")
    assert log[-1][3]["model_usd"] == 0.0 and log[-1][3]["booked_model_usd"] == 15.36
    written = [(p["run_date"], json.loads(p["counts"])["model_usd"]) for t, p in fake.calls if t == embed.SPEND_ROW_SQL]
    assert written == [(date(2026, 10, 5), 15.36)]


def test_a_window_that_raises_keeps_its_ceiling_and_the_failed_row_takes_none_of_it_back(monkeypatch):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)

    class WindowRefused(FakeWarehouse):
        def __call__(self, text, params, max_bytes=None):
            if text == sql("embed"):
                raise RuntimeError("Vertex quota exceeded")  # how much it billed before failing is unknown
            return super().__call__(text, params, max_bytes)

    with pytest.raises(RuntimeError):
        job.main(execute=WindowRefused())
    assert log[-1][:3] == ("finish", "understand-1", "failed")
    assert log[-1][3]["model_usd"] == 0.0 and log[-1][3]["booked_model_usd"] == 15.36


@pytest.mark.parametrize("now, cap", [
    (datetime(2026, 10, 2, 21, 59, 59, tzinfo=timezone.utc), 50.0),
    (datetime(2026, 10, 2, 22, 0, 0, tzinfo=timezone.utc), 50.0),
])
def test_the_spend_read_uses_the_cap_for_its_passed_time_and_the_ask_byte_ceiling(now, cap):
    from core.agent.ask import SPEND_SQL
    from core.agent.tools.sql_query import MAX_BYTES_BILLED

    seen = []

    def execute(text, params, max_bytes=None):
        seen.append((text, max_bytes))
        return [{"usd": 1.5}]

    assert embed.spend_today(execute, now) == (1.5, cap)
    assert seen == [(SPEND_SQL, MAX_BYTES_BILLED)] and MAX_BYTES_BILLED


def test_bigquery_execute_bills_no_more_than_max_bytes_when_given(monkeypatch):
    from google.cloud import bigquery

    queries = []

    class FakeJob:
        num_dml_affected_rows = None

        def result(self):
            return []

    class FakeClient:
        def __init__(self, project):
            pass

        def query(self, text, job_config=None, **kwargs):
            queries.append(job_config)
            return FakeJob()

    monkeypatch.setattr(bigquery, "Client", FakeClient)
    execute = job.bigquery_execute()
    execute("SELECT 1", {"day": DAY}, max_bytes=10_000_000)
    execute("SELECT 1", {})
    assert [c.maximum_bytes_billed for c in queries] == [10_000_000, None]


def test_bigquery_execute_never_restarts_a_failed_query_job(monkeypatch):
    # The library's default job_retry resubmits a failed query as a new job within the same call, so an embed script
    # that failed after ML.GENERATE_EMBEDDING billed would embed again under one booked ceiling.
    # This proves only that job_retry=None is passed to query and result. That None turns the resubmit off is the
    # library's behaviour, read in google-cloud-bigquery 3.25.0: job/query.py lines 1554 to 1653 and _job_helpers.py
    # lines 126 to 150.
    from google.cloud import bigquery

    calls = []

    class FakeJob:
        num_dml_affected_rows = None

        def result(self, **kwargs):
            calls.append(("result", kwargs))
            return []

    class FakeClient:
        def __init__(self, project):
            pass

        def query(self, text, **kwargs):
            calls.append(("query", kwargs))
            return FakeJob()

    monkeypatch.setattr(bigquery, "Client", FakeClient)
    execute = job.bigquery_execute()
    execute(sql("embed"), {"day": DAY}, max_bytes=10_000_000)
    execute("INSERT ...", {"rows": "[]"})
    queries = [kwargs for kind, kwargs in calls if kind == "query"]
    assert len(queries) == 2
    assert all("job_retry" in kwargs and kwargs["job_retry"] is None for kwargs in queries)
    assert all("job_id" not in kwargs for kwargs in queries), "a job_id would stop the transport retry recovering"
    assert all(kwargs.get("job_retry") is None for kind, kwargs in calls if kind == "result")


def test_job_docstring_says_no_query_job_is_restarted():
    doc = " ".join(job.__doc__.split())
    assert "job_retry=None" in doc


def test_embed_docstring_says_a_running_windows_ceiling_counts_in_todays_spend():
    doc = " ".join(embed.__doc__.split())
    assert "While a window runs" in doc and "USD 15.36" in doc and "Ask may refuse" in doc


# Clustering runs in the job after enrichment, per market and then pooled, and fails soft (BUILD.md 2.2)

def clustered(seen, fail=None, spend=None):
    """A run_cluster stand-in: logs each market and the rest of its arguments, raises for the market named in fail,
    else returns its counts. spend, a dict of the label net's model_usd and booked_usd, goes into the counts, or onto
    the exception."""
    def run(execute, *, run_date, market, **kw):
        seen.append(("cluster", market, run_date, kw))
        if market == fail:
            err = RuntimeError("cluster_write refused at https://bigquery.googleapis.com/x\nsecond line")
            err.failed_batch = 1
            err.unwritten_cluster_ids = ["c2", "c3"]
            for k, v in (spend or {}).items():
                setattr(err, k, v)
            raise err
        return {"market": market, "posts": 40, "today_posts": 12, "clusters": 2, "members": 12, "new": 2,
                **(spend or {})}
    return run


def test_the_job_clusters_each_market_then_pan_after_enrichment_and_before_it_finishes(monkeypatch):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)
    monkeypatch.setattr(job, "run_enrich", lambda execute, **kw: log.append(("enrich",)) or {"enriched": 0})
    monkeypatch.setattr(job, "run_cluster", clustered(log))
    assert job.main(execute=FakeWarehouse()) == 0
    assert [e[0] if e[0] != "cluster" else e[:3] for e in log] == [
        "begin", "enrich", ("cluster", "za", DAY), ("cluster", "ng", DAY), ("cluster", "ke", DAY),
        ("cluster", "pan", DAY), "finish", "start_next"]
    finished = next(e for e in log if e[0] == "finish")
    assert finished[2] == "ok" and finished[4] is None
    assert finished[3]["cluster"] == {m: {"posts": 40, "today_posts": 12, "clusters": 2, "members": 12, "new": 2}
                                      for m in ("za", "ng", "ke", "pan")}
    assert finished[3]["model_usd"] == 0.0 and finished[3]["booked_model_usd"] == 0.000026, \
        "this stand-in's label net spends nothing, so the understand row's spend is embed's alone"


def test_the_label_nets_spend_is_booked_on_the_jobs_day_and_joins_the_understand_row(monkeypatch):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)
    monkeypatch.setattr(job, "run_enrich", lambda execute, **kw: {"enriched": 0})
    # Each market's net spent 0.002 and booked 0.0015 of it; ng's write then raised, its spend riding on the error.
    monkeypatch.setattr(job, "run_cluster", clustered(log, fail="ng", spend={"model_usd": 0.002,
                                                                             "booked_usd": 0.0015}))
    assert job.main(execute=FakeWarehouse()) == 0
    calls = [e for e in log if e[0] == "cluster"]
    assert [{key: kw[key] for key in ("run_id", "day")} for *_, kw in calls] == \
        [{"run_id": "understand-1", "day": DAY}] * 4
    assert all(callable(kw["clock"]) for *_, kw in calls)
    finished = next(e for e in log if e[0] == "finish")[3]
    assert finished["booked_model_usd"] == round(0.000026 + 4 * 0.0015, 6)
    assert finished["model_usd"] == round(4 * 0.0005, 6), "the unbooked part rides on the understand row"
    assert all("model_usd" not in c and "booked_usd" not in c for c in finished["cluster"].values())
    assert finished["cluster"]["ng"]["failed_batch"] == 1


def test_a_market_whose_clustering_raises_is_recorded_and_the_rest_still_cluster(monkeypatch):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)
    monkeypatch.setattr(job, "run_enrich", lambda execute, **kw: {"enriched": 0})
    monkeypatch.setattr(job, "run_cluster", clustered(log, fail="ng"))
    assert job.main(execute=FakeWarehouse()) == 0
    assert [e[1] for e in log if e[0] == "cluster"] == ["za", "ng", "ke", "pan"]
    finished = next(e for e in log if e[0] == "finish")
    assert finished[2] == "ok"
    assert finished[3]["cluster"]["ng"] == {"error": "RuntimeError: cluster_write refused at <url>",
                                            "failed_batch": 1, "unwritten_cluster_ids": ["c2", "c3"]}
    assert finished[3]["cluster"]["ke"]["clusters"] == 2
    assert log[-1] == ("start_next", "understand", DAY)


def test_a_failed_import_of_the_cluster_stack_fails_clustering_soft_not_the_job(monkeypatch):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)
    monkeypatch.setattr(job, "run_enrich", lambda execute, **kw: {"enriched": 0})
    monkeypatch.setitem(sys.modules, "core.understand.cluster", None)  # the BERTopic stack will not import
    assert job.main(execute=FakeWarehouse()) == 0
    finished = next(e for e in log if e[0] == "finish")
    assert finished[2] == "ok" and set(finished[3]["cluster"]) == {"za", "ng", "ke", "pan"}
    assert all(c["error"].startswith(("ImportError", "ModuleNotFoundError")) for c in finished[3]["cluster"].values())
    assert finished[3]["embedded"] == 5
    assert log[-1] == ("start_next", "understand", DAY)


def test_the_job_clusters_the_markets_cluster_py_takes_in_its_order():
    from core.understand import cluster

    assert job.CLUSTER_MARKETS == cluster.MARKETS


def test_the_real_run_cluster_counts_a_quiet_night_as_too_few_posts(monkeypatch):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)
    monkeypatch.setattr(job, "run_enrich", lambda execute, **kw: {"enriched": 0})
    assert job.main(execute=FakeWarehouse()) == 0
    assert log[1][3]["cluster"] == {m: {"posts": 0, "today_posts": 0, "skipped": "too_few_posts"}
                                    for m in ("za", "ng", "ke", "pan")}


def test_failed_enrichment_and_cluster_writes_land_where_coverage_reads_them_and_detect_still_starts(monkeypatch):
    # Coverage shows the run as degraded from these counts (core/api/store.py, runs_of_day).
    from core.api.store import degraded_writes

    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)

    def enrich_raises(execute, **kw):
        raise RuntimeError("enrichment write refused")

    def cluster_raises(execute, *, market, **kw):
        if market in ("ng", "pan"):
            raise RuntimeError("cluster_write refused")
        return {"market": market, "clusters": 1}

    monkeypatch.setattr(job, "run_enrich", enrich_raises)
    monkeypatch.setattr(job, "run_cluster", cluster_raises)
    assert job.main(execute=FakeWarehouse()) == 0
    finished = next(e for e in log if e[0] == "finish")
    assert finished[2] == "ok" and finished[4] is None, "fail-soft: the run stays ok so the morning brief is never held"
    assert degraded_writes(finished[3]) == ["enrich", "cluster:ng", "cluster:pan"]
    assert log[-1] == ("start_next", "understand", DAY)


def test_a_clean_run_reads_as_not_degraded(monkeypatch):
    from core.api.store import degraded_writes

    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)
    monkeypatch.setattr(job, "run_enrich", lambda execute, **kw: {"enriched": 0})
    monkeypatch.setattr(job, "run_cluster", clustered(log))
    assert job.main(execute=FakeWarehouse()) == 0
    assert degraded_writes(next(e for e in log if e[0] == "finish")[3]) == []


def test_the_markets_coverage_reads_for_degraded_clustering_are_the_jobs():
    from core.api import store

    assert store.CLUSTER_MARKETS == job.CLUSTER_MARKETS


def test_job_docstring_says_clustering_runs_per_market_and_fails_soft():
    doc = " ".join(job.__doc__.split())
    assert "run_cluster" in doc and "pan" in doc and "Clustering fails soft" in doc


# A window that raises leaves its ceiling booked, so retries stop at one window of uncorrected ceilings a day

def runs_table(rows):
    import duckdb

    con = duckdb.connect()
    con.execute(f"ATTACH ':memory:' AS {PROJECT_DB}")
    con.execute(f"CREATE SCHEMA {PROJECT_DB}.intelligence_42_agent")
    con.execute(f"CREATE TABLE {PROJECT_DB}.intelligence_42_agent.runs (run_id VARCHAR, stage VARCHAR, run_date DATE, "
                "status VARCHAR, started_at TIMESTAMP, finished_at TIMESTAMP, counts JSON)")
    con.executemany(f"INSERT INTO {PROJECT_DB}.intelligence_42_agent.runs VALUES (?, ?, ?, 'ok', ?, ?, ?)",
                    [(rid, stage, day, at, at, json.dumps(c)) for rid, stage, day, at, c in rows])
    return con


def run_uncorrected_sql(con, day):
    statement = sqlglot.parse_one(sql("embed_uncorrected"), read="bigquery")
    result = con.execute(statement.sql("duckdb"), {"day": day})
    names = [d[0] for d in result.description]
    return dict(zip(names, result.fetchone()))


def test_embed_uncorrected_sql_is_one_read_only_select_of_runs():
    statements = [s for s in sqlglot.parse(sql("embed_uncorrected"), read="bigquery") if s is not None]
    assert len(statements) == 1 and isinstance(statements[0], exp.Select)
    text = sql("embed_uncorrected")
    assert not re.search(r"\b(INSERT|CREATE|DROP|TRUNCATE|DELETE|REPLACE|MERGE|UPDATE)\b", text, re.I)
    assert "@day" in text and "intelligence_42_agent.runs" in text


def test_embed_uncorrected_sums_the_last_ceiling_of_each_run_that_booked_no_correction_for_it():
    t = datetime(2026, 9, 28, 6, 0)

    def spend(run, n, what, usd, day=DAY):
        return (f"{run}-spend-{n:08x}", "understand_spend", day, t + timedelta(minutes=n),
                {"model_usd": usd, "what": what})

    con = runs_table([
        spend("paired", 1, "embed_ceiling", 7.5), spend("paired", 2, "embed_correction", -7.49),
        spend("raised", 3, "embed_ceiling", 7.5),
        spend("second", 4, "embed_ceiling", 7.5), spend("second", 5, "embed_correction", -7.4),
        spend("second", 6, "embed_ceiling", 3.0),
        spend("yesterday", 7, "embed_ceiling", 7.5, day=DAY - timedelta(days=1)),
        spend("enrich", 8, "enrich_batch_estimate:s1", 2.0),
        ("understand-9", "understand", DAY, t, {"model_usd": 7.5}),
    ])
    assert run_uncorrected_sql(con, DAY)["usd"] == pytest.approx(10.5)
    assert run_uncorrected_sql(con, DAY + timedelta(days=1))["usd"] == 0


def test_embed_uncorrected_reads_the_run_ids_book_spend_writes():
    written = []
    embed.book_spend(lambda text, params: written.append(params), run_id="understand-1", run_date=DAY, usd=7.5,
                     what="embed_ceiling")
    con = runs_table([(written[0]["run_id"], "understand_spend", DAY, datetime(2026, 9, 28, 6, 0),
                       json.loads(written[0]["counts"]))])
    assert run_uncorrected_sql(con, DAY)["usd"] == pytest.approx(7.5)


def test_run_embed_refuses_a_window_once_todays_uncorrected_ceilings_reach_one_window():
    booked, fake = [], FakeWarehouse()
    counts = embed.run_embed(fake, run_date=DAY, spent_today=lambda: (15.36, 20.0), uncorrected=lambda: 15.36,
                             book=lambda usd, what: booked.append(usd) or usd)
    assert fake.embed_windows() == [] and booked == []
    assert counts["retry_capped"] is True and counts["uncorrected_usd"] == 15.36 and counts["embedded"] == 0


def test_run_embed_sends_while_uncorrected_ceilings_are_under_one_window():
    fake = FakeWarehouse()
    counts = embed.run_embed(fake, run_date=DAY, spent_today=lambda: (0.0, 20.0), uncorrected=lambda: 15.359999,
                             book=lambda usd, what: usd)
    assert fake.embed_windows() == [(DAY, DAY)] and "retry_capped" not in counts


def test_run_embed_sends_nothing_when_uncorrected_ceilings_cannot_be_read():
    def unreadable():
        raise RuntimeError("runs read refused")

    booked, fake = [], FakeWarehouse()
    counts = embed.run_embed(fake, run_date=DAY, spent_today=lambda: (0.0, 20.0), uncorrected=unreadable,
                             book=lambda usd, what: booked.append(usd) or usd)
    assert fake.embed_windows() == [] and booked == []
    assert counts["spend_unknown"] is True and counts["spend_error"] == "RuntimeError"


def test_uncorrected_today_reads_the_sql_on_the_day_it_is_given_with_the_byte_ceiling_ask_reads_spend_with():
    from core.agent.tools.sql_query import MAX_BYTES_BILLED

    seen = []

    def execute(text, params, max_bytes=None):
        seen.append((text, params, max_bytes))
        return {"rows": [{"usd": 3.0}]}

    assert embed.uncorrected_today(execute, DAY) == 3.0
    assert seen == [(sql("embed_uncorrected"), {"day": DAY}, MAX_BYTES_BILLED)] and MAX_BYTES_BILLED
    assert embed.uncorrected_today(lambda text, params, max_bytes=None: [], DAY) == 0.0


def test_a_retry_after_a_window_raised_today_books_no_ceiling_and_the_job_goes_on(monkeypatch):
    monkeypatch.setenv("EMBED_DAYS", "14")
    log = fake_chain(monkeypatch)
    monkeypatch.setattr(job, "now", lambda: datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))

    class RaisedEarlier(FakeWarehouse):
        def __call__(self, text, params, max_bytes=None):
            if text == sql("embed_uncorrected"):
                self.calls.append((text, params))
                return [{"usd": 15.36}]
            return super().__call__(text, params, max_bytes)

    fake = RaisedEarlier()
    assert job.main(execute=fake) == 0
    assert [p for t, p in fake.calls if t == sql("embed_uncorrected")] == [{"day": date(2026, 10, 5)}], \
        "the ceilings are read on today's date, where book_spend books them, not on RUN_DATE"
    assert fake.embed_windows() == [] and [p for t, p in fake.calls if t == embed.SPEND_ROW_SQL] == []
    counts = log[1][3]
    assert log[1][2] == "ok" and counts["retry_capped"] is True and counts["uncorrected_usd"] == 15.36
    assert log[2] == ("start_next", "understand", DAY)


# A job's clock can pass midnight: every spend read and booking stays on the day the job started

def test_a_job_whose_clock_crosses_midnight_reads_and_books_on_one_day_and_the_cap_holds(monkeypatch):
    from core.agent.ask import SPEND_SQL
    from core.config.caps import model_daily_usd

    CAP = model_daily_usd(now=datetime(2026, 9, 28, 21, 50, tzinfo=timezone.utc))
    # A 90-day backfill of DAY started at 23:50 SAST with USD 1 left under the cap that day; the first window fits, but
    # the next window sees 00:10 SAST and must not start on the previous day's budget.
    monkeypatch.setenv("EMBED_DAYS", "90")
    chain, runs, downstream = memory_chain(monkeypatch)
    start = datetime(2026, 9, 28, 21, 50, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 9, 28, 22, 10, tzinfo=timezone.utc)
    current = [start]
    monkeypatch.setattr(job, "now", lambda: current[0])
    enriched = []
    monkeypatch.setattr(job, "run_enrich", lambda execute, **kw: enriched.append(kw) or {"enriched": 0})

    class Ledger(FullPosts):
        """Answers each spend read from the spend booked on the day it asks for."""

        def __init__(self):
            super().__init__()
            self.by_day = {DAY: CAP - 1.0}

        def __call__(self, text, params, max_bytes=None):
            if text == SPEND_SQL:
                self.calls.append((text, params))
                return [{"usd": self.by_day.get(params["day"], 0.0)}]
            if text == sql("embed_uncorrected"):
                self.calls.append((text, params))
                return [{"usd": 0.0}]
            if text == embed.SPEND_ROW_SQL:
                day = params["run_date"]
                self.by_day[day] = round(self.by_day.get(day, 0.0) + json.loads(params["counts"])["model_usd"], 6)
            result = super().__call__(text, params, max_bytes)
            if text == sql("embed"):
                current[0] = after_midnight
            return result

    fake = Ledger()
    assert job.main(execute=fake) == 1
    assert len(fake.embed_windows()) == 1
    assert {p["day"] for t, p in fake.calls if t == SPEND_SQL} == {DAY}
    assert {p["day"] for t, p in fake.calls if t == sql("embed_uncorrected")} == {DAY}
    assert {p["run_date"] for t, p in fake.calls if t == embed.SPEND_ROW_SQL} == {DAY}
    assert fake.by_day[DAY] <= CAP and set(fake.by_day) == {DAY}, "the cap on the booking day holds"
    assert enriched and enriched[0]["day"] == DAY, "enrichment reads and books on the day the job started too"
    assert runs.latest("understand", DAY)["status"] == "failed"
    assert runs.latest("understand", DAY)["error"] == "day_changed"
    assert downstream == []
    retry = chain.begin("understand", DAY)
    assert retry.run_date == DAY and runs.latest("understand", DAY)["status"] == "running"


@pytest.mark.parametrize("source", ["enrich", "cluster"])
def test_job_marks_enrichment_and_cluster_rollovers_retryable(monkeypatch, source):
    chain, runs, downstream = memory_chain(monkeypatch)
    start = datetime(2026, 9, 28, 21, 50, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 9, 28, 22, 10, tzinfo=timezone.utc)
    current = [start]
    monkeypatch.setattr(job, "now", lambda: current[0])
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    monkeypatch.setattr(job, "run_embed", lambda execute, **kw: {"model_usd": 0.0})

    def run_enrich(execute, **kw):
        if source == "enrich":
            current[0] = after_midnight
            return {"day_changed": True, "model_usd": 0.0, "booked_usd": 0.0}
        return {"model_usd": 0.0, "booked_usd": 0.0}

    def run_cluster(execute, **kw):
        if source == "cluster":
            return {"market": kw["market"], "net_error": "day_changed", "net_failed": True,
                    "model_usd": 0.0, "booked_usd": 0.0}
        return {"market": kw["market"], "posts": 0, "today_posts": 0, "skipped": "too_few_posts",
                "model_usd": 0.0, "booked_usd": 0.0}

    monkeypatch.setattr(job, "run_enrich", run_enrich)
    monkeypatch.setattr(job, "run_cluster", run_cluster)

    assert job.main(execute=lambda text, params: []) == 1
    assert runs.latest("understand", DAY)["status"] == "failed"
    assert runs.latest("understand", DAY)["error"] == "day_changed"
    assert downstream == []
    retry = chain.begin("understand", DAY)
    assert retry.run_date == DAY and runs.latest("understand", DAY)["status"] == "running"


# A per-post ceiling at the model's 2,048-token input, and a correction from the tokens Vertex reports

def test_the_post_ceiling_is_the_models_2048_token_input():
    assert embed.MAX_TOKENS_PER_POST == 2_048
    assert embed.POST_CEILING_USD == pytest.approx(2_048 * 0.15 / 1_000_000) and embed.POST_CEILING_USD >= 0.0003072
    assert embed.WINDOW_CEILING_USD == 15.36, "one full window of 50,000 posts at 2,048 tokens each"


@pytest.mark.parametrize("tokens,usd", [(0, 0), (175, 0.000026), (1_000_000, 0.15), (10_000_000, 1.5)])
def test_model_usd_prices_the_tokens_embed_sql_reports(tokens, usd):
    fake = FakeWarehouse(counts={"embedded": 1, "failed": 0, "skipped": 0, "chars_sent": 99_999, "tokens": tokens})
    assert embed.run_embed(fake, run_date=DAY)["model_usd"] == usd


def test_embed_sql_sums_the_token_counts_vertex_reports_and_prices_the_rest_at_3_bytes_a_token():
    text = sql("embed")
    assert "ml_generate_embedding_statistics" in text and "'$.token_count'" in text
    last = sqlglot.parse(text, read="bigquery")[-1]
    assert "tokens" in last.named_selects
    emoji = "\N{FACE WITH TEARS OF JOY}" * 4_000  # 4,000 characters, 16,000 UTF-8 bytes
    con = fixture_warehouse(
        posts=[("counted", "Amapiano at the braai", None, DAY), ("uncounted", "Jollof wars", None, DAY),
               ("emoji", emoji, None, DAY)],
        observations=[("counted", DAY), ("uncounted", DAY), ("emoji", DAY)])

    def model(post_id):
        return ([0.1] * 768, "", json.dumps({"token_count": 7, "truncated": False})) if post_id == "counted" \
            else good_vector(post_id)

    counts = run_embed_sql(con, DAY, DAY, model=model)
    # counted: 7 as reported; uncounted: 11 bytes at 3 a token, 4; emoji: 16,000 bytes, capped at 2,048.
    assert counts["tokens"] == 7 + 4 + 2_048
    assert counts["embedded"] == 3


def test_a_run_for_a_past_day_skips_clustering_as_it_skips_enrichment(monkeypatch):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)
    monkeypatch.setattr(job, "now", lambda: datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))
    seen = []
    monkeypatch.setattr(job, "run_cluster", clustered(seen))
    assert job.main(execute=FakeWarehouse()) == 0
    assert seen == [], "clustering a past day would move last_seen back and fold old posts into today's centroids"
    counts = log[1][3]
    assert counts["cluster"] == {"skipped": "backfill"} and counts["enrich_skipped_backfill"] is True
    assert log[2] == ("start_next", "understand", DAY)


# Peak memory per phase, and the memory one market's clustering leaves behind

def test_every_phase_line_carries_the_peak_rss_so_far(monkeypatch, capsys):
    monkeypatch.delenv("EMBED_DAYS", raising=False)
    fake_chain(monkeypatch)
    peaks = iter([100.0, 150.25, 150.25, 300.0, 300.0, 812.5])
    monkeypatch.setattr(job, "peak_rss_mb", lambda: next(peaks))
    assert job.main(execute=FakeWarehouse()) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("understand_phase ")]
    assert [ln.split(" ", 4)[2:4] + [ln.rsplit(" ", 1)[1]] for ln in lines] == [
        ["phase=embed", "event=start", "peak_rss_mb=100.0"], ["phase=embed", "event=end", "peak_rss_mb=150.2"],
        ["phase=enrich", "event=start", "peak_rss_mb=150.2"], ["phase=enrich", "event=end", "peak_rss_mb=300.0"],
        ["phase=clustering", "event=start", "peak_rss_mb=300.0"],
        ["phase=clustering", "event=end", "peak_rss_mb=812.5"]]


def test_peak_rss_reads_this_process_from_getrusage_in_mib():
    import resource

    peak = job.peak_rss_mb()
    assert isinstance(peak, float) and peak > 0
    assert peak == pytest.approx(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, rel=0.5)


def test_peak_rss_is_none_without_the_resource_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "resource", None)
    assert job.peak_rss_mb() is None


def test_memory_is_collected_after_each_market_is_clustered(monkeypatch):
    import gc

    monkeypatch.delenv("EMBED_DAYS", raising=False)
    log = fake_chain(monkeypatch)
    monkeypatch.setattr(job, "run_enrich", lambda execute, **kw: {"enriched": 0})
    monkeypatch.setattr(job, "run_cluster", clustered(log, fail="ng"))
    monkeypatch.setattr(gc, "collect", lambda *a: log.append(("gc",)) or 0)
    assert job.main(execute=FakeWarehouse()) == 0
    assert [e[:2] for e in log if e[0] in ("cluster", "gc")] == [
        ("cluster", "za"), ("gc",), ("cluster", "ng"), ("gc",), ("cluster", "ke"), ("gc",), ("cluster", "pan"), ("gc",)]
