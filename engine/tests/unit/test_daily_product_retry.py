"""Same day retries, the read only query seam, metering and the zero model origin."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.daily_dynamic_meter import DynamicMeter
from src.analysis.open_intelligence.daily_product_io import (
    ProductBudget,
    _QueryClient,
    canonical_rows,
)
from src.analysis.open_intelligence.daily_products import DailyProducts
from src.analysis.open_intelligence.daily_stages import _admitted_capture
from src.analysis.open_intelligence.persistence import TARGET_LOCATION

from tests.unit import test_daily_products_nonempty as nonempty
from tests.unit.daily_product_authority_fixture import funded_product_authority
from tests.unit.test_daily_product_completion import Objects
from tests.unit.test_daily_stages import CUTOFF, Fixture, manifest
from tests.unit.test_open_intelligence_release_profiles import STAGING_RUN_ID

DAY = date(2026, 9, 19)
# The SQL line comment marker, spelled as the guard spells it.
LINE_COMMENT = "-" * 2
MODEL_PRODUCTS = ("trend_analysis", "daily_summary", "seed_insights", "creator_briefs")


def _harness(tmp_path, monkeypatch, *, limits=None):
    fx = funded_product_authority(tmp_path, monkeypatch, limits=limits)
    stages = Fixture(monkeypatch)
    stages.run(deny=("compose",))
    entry = _admitted_capture(stages.clients, stages.record("capture"))
    day = (CUTOFF - timedelta(days=1)).date()
    monkeypatch.setattr(nonempty, "DAY", day)
    monkeypatch.setattr(nonempty, "CUTOFF", CUTOFF)
    boundary = nonempty.BoundaryIO()

    class Warehouse(nonempty.Warehouse):
        location = TARGET_LOCATION
        _credentials = SimpleNamespace(
            service_account_email=fx.authority.manifest.service_identity, quota_project_id=None
        )

        def _rows(self, sql, params):
            if sql.strip() == "SELECT 1":
                return []
            if "STRING_AGG(NULLIF(v2persons" in sql:
                return [{"persons": "", "orgs": "", "slang": "newdancewave"}]
            if "brand24_mention_sentiment" in sql:
                return []
            if ".trend_scores`" in sql and "AVG(" in sql:
                return []
            if sql.startswith("SELECT * FROM"):
                table = sql.split("`", 2)[1].rsplit(".", 1)[1]
                rows = self.io.read_rows(table)
                if "consumers" in params:
                    rows = [row for row in rows if row["consumer"] in params["consumers"]]
                return rows
            return super()._rows(sql, params)

    boundary.client = Warehouse(boundary)
    objects = Objects()

    def products():
        return DailyProducts(
            objects=objects,
            build={
                "source_sha": fx.authority.manifest.source_sha,
                "image_uri": fx.authority.manifest.image_uri,
            },
            now=lambda: CUTOFF,
            run_id=STAGING_RUN_ID,
            io_factory=lambda **kwargs: boundary,
            dynamic_meter=DynamicMeter(boundary.client),
        )

    def run(instance):
        return instance.run(entry=entry, manifest=manifest("compose"), authority_receipt=fx.receipt)

    return SimpleNamespace(
        fx=fx, boundary=boundary, products=products, run=run, objects=objects, day=day
    )


def _state(instance):
    return next(iter(instance._active.values()))


def _usage_ids_of(rows):
    # The spend each usage row records; its identifier and stamp are minted per flush.
    return sorted(
        tuple(
            str(row.get(field))
            for field in (
                "consumer",
                "market",
                "gemini_model",
                "calls",
                "prompt_tokens",
                "completion_tokens",
            )
        )
        for row in rows
    )


def _usage_ids(harness):
    return _usage_ids_of(harness.boundary.rows.get("gemini_usage", []))


def test_retry_after_a_transient_model_failure_keeps_usage_and_never_redispatches(
    tmp_path, monkeypatch
):
    harness = _harness(tmp_path, monkeypatch)
    models = harness.boundary.models
    original = type(models).generate_content
    state = {"fail": True}

    def flaky(self, **kwargs):
        if state["fail"] and "summary_text" in kwargs["config"].response_schema["properties"]:
            raise RuntimeError("transient vertex failure")
        return original(self, **kwargs)

    monkeypatch.setattr(type(models), "generate_content", flaky)
    with pytest.raises(Exception):  # noqa: B017
        harness.run(harness.products())
    usage = _usage_ids(harness)
    briefs = len(harness.boundary.rows["trend_analysis"])
    calls = models.calls
    assert usage
    assert briefs == 6

    state["fail"] = False
    # The failed summary call's effect is unknown, so the retry replays the briefs and
    # then refuses rather than dispatching that call again.
    with pytest.raises(ValueError, match=r"paid_attempt_unknown|products_effect_unknown"):
        harness.run(harness.products())
    assert models.calls == calls
    assert _usage_ids(harness) == usage
    assert len(harness.boundary.rows["trend_analysis"]) == briefs


def test_retry_deletes_only_rows_this_operation_recorded_writing(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    harness.run(harness.products())
    scores = list(harness.boundary.rows["trend_scores"])
    foreign_score = {**scores[0], "query_group": "wave1_pilot_topic"}
    foreign_usage = {
        **harness.boundary.rows["gemini_usage"][0],
        "consumer": "comment_sentiment",
        "usage_id": "usage_wave1",
    }
    harness.boundary.rows["trend_scores"].append(foreign_score)
    harness.boundary.rows["gemini_usage"].append(foreign_usage)

    # Another writer's row in the shared day refuses the readback, and survives it.
    with pytest.raises(ValueError, match="products_readback_differs:trend_scores"):
        harness.run(harness.products())
    assert foreign_score in harness.boundary.rows["trend_scores"]
    assert foreign_usage in harness.boundary.rows["gemini_usage"]
    assert len(harness.boundary.rows["trend_scores"]) == len(scores) + 1


def test_a_changed_row_under_a_recorded_key_refuses_before_any_delete(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    harness.run(harness.products())
    rows = harness.boundary.rows["trend_scores"]
    rows[0] = {**rows[0], "trend_score": 99.0}
    before = {table: len(found) for table, found in harness.boundary.rows.items()}
    deletes = len(harness.boundary.deletes)
    with pytest.raises(ValueError, match="products_partition_foreign:trend_scores"):
        harness.run(harness.products())
    assert len(harness.boundary.deletes) == deletes
    assert {table: len(found) for table, found in harness.boundary.rows.items()} == before


def test_same_day_retry_replaces_each_partition_instead_of_duplicating(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    first = harness.products()
    harness.run(first)
    written = {table: list(rows) for table, rows in harness.boundary.rows.items()}
    calls = harness.boundary.models.calls
    assert written["trend_scores"]
    assert written["seed_graph"]

    retry = harness.products()
    result = harness.run(retry)

    for table in written:
        # Wall clock stamps may move between attempts; the row set may not grow or shrink.
        assert len(harness.boundary.rows[table]) == len(written[table]), table
    assert _usage_ids(harness) == _usage_ids_of(written["gemini_usage"])
    assert {table for table, _count in harness.boundary.deletes} == set(written)
    assert result["products"]["trend_analysis"]["row_count"] == 6
    assert harness.boundary.models.calls == calls
    metering = _state(retry)["budget"].metering()
    assert metering["model_calls"] == 0
    assert metering["rows_written"] == sum(len(rows) for rows in written.values())


def test_metering_reports_rows_written_and_dispatched_model_calls(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    instance = harness.products()
    harness.run(instance)
    metering = _state(instance)["budget"].metering()
    assert metering["rows_written"] == sum(len(rows) for rows in harness.boundary.rows.values())
    assert metering["model_calls"] == harness.boundary.models.calls


def test_outcome_records_the_written_digest_beside_the_readback_digest(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    instance = harness.products()
    result = harness.run(instance)
    written = canonical_rows(harness.boundary.rows["trend_analysis"])
    outcome = result["products"]["trend_analysis"]
    assert outcome["output_digest"] == canonical_digest(written)
    assert outcome["readback_digest"] == canonical_digest(written)


def test_zero_model_origin_runs_deterministic_producers_and_marks_model_products_unfunded(
    tmp_path, monkeypatch
):
    harness = _harness(tmp_path, monkeypatch, limits={"max_model_calls": 0})
    instance = harness.products()
    result = harness.run(instance)
    assert harness.boundary.models.calls == 0
    assert harness.boundary.rows["trend_scores"]
    assert harness.boundary.rows["seed_graph"]
    assert harness.boundary.rows["pan_african_stories"]
    for name in MODEL_PRODUCTS:
        assert result["products"][name]["state"] == "unfunded"
        assert result["products"][name]["row_count"] == 0
        assert name not in harness.boundary.rows
    assert "gemini_usage" not in harness.boundary.rows
    assert result["products"]["seed_candidates"]["state"] in {"completed", "empty"}
    assert _state(instance)["budget"].metering()["model_calls"] == 0


def test_capped_model_origin_still_refuses_beyond_its_allowance(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch, limits={"max_model_calls": 1})
    instance = harness.products()
    with pytest.raises(ValueError):
        harness.run(instance)
    assert harness.boundary.models.calls <= 1


class _Recording:
    project, location = "ogilvy-trends-v2", TARGET_LOCATION

    def __init__(self):
        self.calls = []

    def query(self, sql, **kwargs):
        self.calls.append(sql)
        raise AssertionError("dispatched")


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM `ogilvy-trends-v2.trends_v2_staging.trend_scores` WHERE TRUE",
        "INSERT INTO t SELECT 1",
        "SELECT 1; DROP TABLE t",
        "WITH x AS (SELECT 1) SELECT * FROM x; MERGE t USING x ON TRUE",
        "CREATE TABLE t AS SELECT 1",
        "SELECT 1; EXECUTE IMMEDIATE 'SELECT 2'",
        "EXECUTE IMMEDIATE 'SELECT 1'",
        "SELECT 1; LOAD DATA INTO t FROM FILES (format = 'CSV', uris = ['gs://b/x'])",
        "SELECT 1; SET @@dataset_id = 'other'",
        "SELECT 1;",
        "DECLARE x INT64 DEFAULT 1",
        # A literal or identifier closed early by an ignored escape, or a comment ended by
        # a carriage return, must not hide a second statement.
        "SELECT '''a\\''' # '''; DELETE FROM t WHERE TRUE",
        'SELECT """a\\""" # """; DELETE FROM t WHERE TRUE',
        "SELECT 1 AS `a\\` # `; DELETE FROM t WHERE TRUE",
        "SELECT 1 # x\r; DELETE FROM t WHERE TRUE",
        f"SELECT 1 {LINE_COMMENT} x\r; DELETE FROM t WHERE TRUE",
        # The same shapes hiding a second read: the blanking itself must not be fooled.
        *(
            sql.replace("DELETE FROM t WHERE TRUE", "SELECT 2")
            for sql in (
                "SELECT '''a\\''' # '''; DELETE FROM t WHERE TRUE",
                'SELECT """a\\""" # """; DELETE FROM t WHERE TRUE',
                "SELECT 1 AS `a\\` # `; DELETE FROM t WHERE TRUE",
                "SELECT 1 # x\r; DELETE FROM t WHERE TRUE",
                f"SELECT 1 {LINE_COMMENT} x\r; DELETE FROM t WHERE TRUE",
            )
        ),
        # A write word inside a literal is refused as the earlier guard refused it.
        "SELECT 'DELETE FROM t' AS x",
        # Each added word refuses on its own, past a leading read and with no separator.
        *(
            f"WITH a AS (SELECT 1 AS n) SELECT n FROM a WHERE {word}"
            for word in ("EXECUTE", "IMMEDIATE", "LOAD", "SET", "DECLARE", "COMMIT", "ROLLBACK")
        ),
        # A backslash or carriage return left outside every literal means the text was not
        # read as the warehouse reads it, and refuses on its own.
        "SELECT 1 AS n WHERE 1 = 1 \\",
        "SELECT 'a\\' AS x",
        "SELECT 1 AS n\rFROM t",
        "SELECT 'a\r' AS x",
    ],
)
def test_product_query_seam_is_read_only(sql):
    client = _Recording()
    budget = ProductBudget(
        {"max_model_calls": 0, "max_bytes_billed": 100, "max_rows_written": 0, "max_credits": 0}
    )
    with pytest.raises(ValueError, match="products_query_not_read_only"):
        _QueryClient(client, budget).query(sql)
    assert client.calls == []
    assert budget.query_count == 0
    budget.require_complete()


@pytest.mark.parametrize(
    "setting",
    [
        {"destination": "ogilvy-trends-v2.trends_v2_staging.trend_scores"},
        {"write_disposition": "WRITE_TRUNCATE"},
        {"create_disposition": "CREATE_IF_NEEDED"},
    ],
)
def test_product_query_seam_refuses_a_writing_job_config(setting):
    from google.cloud import bigquery

    client = _Recording()
    budget = ProductBudget(
        {"max_model_calls": 0, "max_bytes_billed": 100, "max_rows_written": 0, "max_credits": 0}
    )
    config = bigquery.QueryJobConfig(**setting)
    with pytest.raises(ValueError, match="products_query_not_read_only"):
        _QueryClient(client, budget).query("SELECT 1", job_config=config)
    assert client.calls == []


def test_first_seen_outcome_digests_the_written_keys_against_the_view_read(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    result = harness.run(harness.products())
    keys = sorted({(row["market"], row["term"]) for row in harness.boundary.rows["seed_graph"]})
    outcome = result["products"]["v_seed_first_seen"]
    assert outcome["output_digest"] == canonical_digest([list(key) for key in keys])
    assert outcome["readback_digest"] == outcome["output_digest"]


def test_first_seen_view_missing_a_written_key_refuses(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    original = type(harness.boundary).read_rows

    def partial(self, table, **kwargs):
        rows = original(self, table, **kwargs)
        return rows[1:] if table == "v_seed_first_seen" else rows

    monkeypatch.setattr(type(harness.boundary), "read_rows", partial)
    with pytest.raises(ValueError, match="products_first_seen_incomplete"):
        harness.run(harness.products())


def _finish(harness, instance, result):
    from src.analysis.open_intelligence.run_receipts import build_run_receipt

    from tests.unit.test_open_intelligence_release_profiles import staging_receipt_fields

    receipt = build_run_receipt(**staging_receipt_fields())
    instance.dynamic_client.query("SELECT 1").result()
    return receipt, instance.finish(result, dynamic_receipt=receipt)


def test_finish_reads_the_dynamic_view_empty_before_release(tmp_path, monkeypatch):
    from src.analysis.open_intelligence.run_receipts import run_receipt_digest

    harness = _harness(tmp_path, monkeypatch)
    instance = harness.products()
    receipt, complete = _finish(harness, instance, harness.run(instance))
    view = complete["products"]["v_desk_dynamic_signals_v2"]
    assert view["output_digest"] == run_receipt_digest(receipt)
    assert view["readback_digest"] == canonical_digest([])
    assert any(".v_desk_dynamic_signals_v2`" in sql for sql in harness.boundary.queries)
    assert complete["max_model_calls"] == 100


def test_finish_refuses_a_dynamic_view_already_showing_the_run(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    instance = harness.products()
    result = harness.run(instance)
    harness.boundary.rows["v_desk_dynamic_signals_v2"] = [{"run_id": STAGING_RUN_ID}]
    with pytest.raises(ValueError, match="products_dynamic_view_premature"):
        _finish(harness, instance, result)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT STRING_AGG(NULLIF(v2persons, ''), ';') AS persons FROM `p.d.t`",
        f"SELECT 1 AS x {LINE_COMMENT} a trailing comment; with a separator",
        "SELECT 'it\\'s' AS x",
    ],
)
def test_product_query_seam_admits_honest_reads(sql):
    class Accepting(_Recording):
        def query(self, sql, **kwargs):
            self.calls.append(sql)
            return SimpleNamespace(result=lambda **kwargs: [], total_bytes_billed=0)

    client = Accepting()
    budget = ProductBudget(
        {"max_model_calls": 0, "max_bytes_billed": 100, "max_rows_written": 0, "max_credits": 0}
    )
    _QueryClient(client, budget).query(sql).result()
    assert client.calls == [sql]


def test_retry_never_deletes_the_same_key_on_another_day(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    harness.run(harness.products())
    earlier = {**harness.boundary.rows["trend_scores"][0], "trend_date": date(2026, 9, 1)}
    harness.boundary.rows["trend_scores"].append(earlier)
    with pytest.raises(ValueError, match="products_readback_differs:trend_scores"):
        harness.run(harness.products())
    assert earlier in harness.boundary.rows["trend_scores"]


def test_a_byte_identical_foreign_duplicate_refuses_before_any_delete(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    harness.run(harness.products())
    rows = harness.boundary.rows["trend_scores"]
    rows.append(dict(rows[0]))
    count, deletes = len(rows), len(harness.boundary.deletes)
    with pytest.raises(ValueError, match="products_partition_foreign:trend_scores"):
        harness.run(harness.products())
    assert len(harness.boundary.rows["trend_scores"]) == count
    assert len(harness.boundary.deletes) == deletes


def test_a_changed_row_raced_under_a_recorded_key_is_never_deleted(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    harness.run(harness.products())
    raced = {**harness.boundary.rows["trend_scores"][0], "trend_score": 42.0}

    def race(table):
        if table == "trend_scores" and raced not in harness.boundary.rows[table]:
            harness.boundary.rows[table].append(raced)

    harness.boundary.before_delete = race
    with pytest.raises(ValueError, match="products_readback_differs:trend_scores"):
        harness.run(harness.products())
    assert raced in harness.boundary.rows["trend_scores"]


def test_an_identical_row_raced_before_the_delete_rolls_it_back(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    harness.run(harness.products())
    rows = harness.boundary.rows["trend_scores"]
    count, raced = len(rows), dict(rows[0])

    def race(table):
        if table == "trend_scores" and len(harness.boundary.rows[table]) == count:
            harness.boundary.rows[table].append(raced)

    harness.boundary.before_delete = race
    with pytest.raises(ValueError, match="products_delete_count_differs"):
        harness.run(harness.products())
    assert len(harness.boundary.rows["trend_scores"]) == count + 1


def test_an_empty_usage_flush_deletes_no_recorded_usage(tmp_path, monkeypatch):
    from src.analysis.open_intelligence.daily_product_completion import CompletionStore
    from src.analysis.open_intelligence.daily_product_io import BoundedProductIO

    harness = _harness(tmp_path, monkeypatch)
    first = harness.products()
    harness.run(first)
    usage = _usage_ids(harness)
    state = _state(first)
    io = BoundedProductIO(
        harness.boundary,
        admission=state["admission"],
        budget=ProductBudget(state["admission"].manifest.limits),
        trend_date=harness.day,
        ledger=CompletionStore(harness.objects),
        operation_id=manifest("compose")["operation_id"],
    )
    deletes = len(harness.boundary.deletes)
    assert io.sink("gemini_usage")([]) == 0
    assert len(harness.boundary.deletes) == deletes
    assert _usage_ids(harness) == usage


@pytest.mark.parametrize(
    "other",
    [
        {"query_group": "food_rituals_braai"},
        {"trend_date": date(2026, 9, 1)},
    ],
    ids=["other_key_same_market", "same_key_other_day"],
)
def test_each_delete_clause_binds_on_its_own(other):
    # Even handed a fingerprint that matches another row, the delete keeps to the recorded
    # key and the day, so a key or date clause widened on its own turns this red.
    columns = ("market", "query_group", "trend_date", "trend_score")
    ours = {"market": "za", "query_group": "music_amapiano", "trend_date": DAY, "trend_score": 1.0}
    theirs = {**ours, **other}
    boundary = nonempty.BoundaryIO()
    boundary.rows["trend_scores"] = [ours, theirs]
    client = _QueryClient(
        nonempty.Warehouse(boundary),
        ProductBudget(
            {"max_model_calls": 0, "max_bytes_billed": 100, "max_rows_written": 0, "max_credits": 0}
        ),
    )
    client.delete_recorded(
        "trend_scores",
        trend_date=DAY,
        keys={("za", "music_amapiano")},
        columns=columns,
        fingerprints={nonempty._fingerprint(row, columns) for row in (ours, theirs)},
        expected=1,
    )
    assert boundary.rows["trend_scores"] == [theirs]


def _warehouse_client(boundary):
    return _QueryClient(
        nonempty.Warehouse(boundary),
        ProductBudget(
            {"max_model_calls": 0, "max_bytes_billed": 100, "max_rows_written": 0, "max_credits": 0}
        ),
    )


def test_a_failed_delete_assertion_leaves_every_row_in_place():
    # The delete runs before its count is asserted, so only the transaction around both
    # keeps a failed assertion from committing what the delete removed.
    columns = ("market", "query_group", "trend_date", "trend_score")
    ours = {"market": "za", "query_group": "music_amapiano", "trend_date": DAY, "trend_score": 1.0}
    boundary = nonempty.BoundaryIO()
    boundary.rows["trend_scores"] = [ours]
    with pytest.raises(ValueError, match="products_delete_count_differs"):
        _warehouse_client(boundary).delete_recorded(
            "trend_scores",
            trend_date=DAY,
            keys={("za", "music_amapiano")},
            columns=columns,
            fingerprints={nonempty._fingerprint(ours, columns)},
            expected=2,
        )
    assert boundary.rows["trend_scores"] == [ours]
    assert boundary.deletes == []


def test_a_delete_lands_only_once_its_transaction_commits():
    columns = ("market", "query_group", "trend_date", "trend_score")
    ours = {"market": "za", "query_group": "music_amapiano", "trend_date": DAY, "trend_score": 1.0}
    boundary = nonempty.BoundaryIO()
    boundary.rows["trend_scores"] = [ours]
    _warehouse_client(boundary).delete_recorded(
        "trend_scores",
        trend_date=DAY,
        keys={("za", "music_amapiano")},
        columns=columns,
        fingerprints={nonempty._fingerprint(ours, columns)},
        expected=1,
    )
    assert boundary.rows["trend_scores"] == []
    assert boundary.deletes == [("trend_scores", 1)]


@pytest.mark.parametrize(
    "column",
    ["trend score", "a`b", "", "1a", "x" * 301, "a.b", "a)b"],
    ids=["space", "backtick", "empty", "leading_digit", "too_long", "dotted", "paren"],
)
def test_a_recorded_column_that_is_not_a_plain_name_is_never_sent(column):
    # The delete is dispatched past the read only seam, so the recorded column names are
    # the only text in it that did not come from this module.
    client = _Recording()
    budget = ProductBudget(
        {"max_model_calls": 0, "max_bytes_billed": 100, "max_rows_written": 0, "max_credits": 0}
    )
    io = _QueryClient(client, budget)
    columns = ("market", column)
    with pytest.raises(ValueError, match="product_write_record_invalid"):
        io.delete_recorded(
            "trend_scores",
            trend_date=DAY,
            keys={("za", "music_amapiano")},
            columns=columns,
            fingerprints={1},
            expected=1,
        )
    with pytest.raises(ValueError, match="product_write_record_invalid"):
        io.fingerprinted_rows("trend_scores", trend_date=DAY, columns=columns)
    assert client.calls == []
    assert budget.query_count == 0


def test_a_fingerprint_the_server_did_not_return_as_an_integer_refuses_before_any_delete(
    tmp_path, monkeypatch
):
    harness = _harness(tmp_path, monkeypatch)
    harness.run(harness.products())
    warehouse = harness.boundary.client
    read = warehouse._fingerprinted

    def as_text(sql, params):
        # INT64 carried as its decimal text, as the REST representation carries it.
        return [{**row, "_fingerprint": str(row["_fingerprint"])} for row in read(sql, params)]

    warehouse._fingerprinted = as_text
    before = {table: list(found) for table, found in harness.boundary.rows.items()}
    deletes = len(harness.boundary.deletes)
    with pytest.raises(ValueError, match="products_partition_foreign:"):
        harness.run(harness.products())
    assert len(harness.boundary.deletes) == deletes
    assert harness.boundary.rows == before


def _fresh_io(harness, instance):
    from src.analysis.open_intelligence.daily_product_completion import CompletionStore
    from src.analysis.open_intelligence.daily_product_io import BoundedProductIO

    state = _state(instance)
    return BoundedProductIO(
        harness.boundary,
        admission=state["admission"],
        budget=ProductBudget(state["admission"].manifest.limits),
        trend_date=harness.day,
        ledger=CompletionStore(harness.objects),
        operation_id=manifest("compose")["operation_id"],
    )


@pytest.mark.parametrize("split", [False, True], ids=["one_write", "two_writes"])
def test_an_attempt_never_writes_two_rows_under_one_key(tmp_path, monkeypatch, split):
    # A retry reads a second row under a recorded key as another writer's, so an attempt
    # that wrote two would refuse every retry of its own day.
    from src.analysis.open_intelligence.daily_product_completion import CompletionStore

    harness = _harness(tmp_path, monkeypatch)
    first = harness.products()
    harness.run(first)
    row = dict(harness.boundary.rows["trend_scores"][0])
    twin = {**row, "trend_score": 7.0}
    # The day starts empty, so this attempt's own readback of its first write agrees.
    harness.boundary.rows["trend_scores"] = []
    io = _fresh_io(harness, first)
    sink = io.sink("trend_scores")
    operation = manifest("compose")["operation_id"]
    records = len(CompletionStore(harness.objects).prior_writes(operation, "trend_scores"))
    stored = len(harness.boundary.rows["trend_scores"])
    if split:
        assert sink([row]) == 1
        records, stored = records + 1, stored + 1
    with pytest.raises(ValueError, match="products_write_key_duplicate:trend_scores"):
        sink([twin] if split else [row, twin])
    assert len(harness.boundary.rows["trend_scores"]) == stored
    assert len(CompletionStore(harness.objects).prior_writes(operation, "trend_scores")) == records


def test_write_records_of_one_table_that_disagree_on_columns_refuse_before_any_read(
    tmp_path, monkeypatch
):
    from src.analysis.open_intelligence.daily_product_completion import CompletionStore

    harness = _harness(tmp_path, monkeypatch)
    harness.run(harness.products())
    ledger = CompletionStore(harness.objects)
    operation = manifest("compose")["operation_id"]
    records = ledger.prior_writes(operation, "trend_scores")
    ledger.record_write(
        operation,
        "trend_scores",
        len(records) + 1,
        columns=records[0]["columns"][:-1],
        keys=records[0]["keys"],
        digests=records[0]["digests"],
    )
    before = {table: list(found) for table, found in harness.boundary.rows.items()}
    deletes = len(harness.boundary.deletes)
    with pytest.raises(ValueError, match="product_write_record_invalid"):
        harness.run(harness.products())
    assert len(harness.boundary.deletes) == deletes
    assert harness.boundary.rows == before
