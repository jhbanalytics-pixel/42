from types import SimpleNamespace

import pytest
from google.cloud import bigquery
from src.analysis.open_intelligence.daily_dynamic_meter import DynamicMeter
from src.analysis.open_intelligence.daily_product_io import ProductBudget


class Job:
    total_bytes_billed = 20
    num_dml_affected_rows = 2
    output_rows = 2
    errors = None

    def result(self, **kwargs):
        return []


class Client:
    project = "ogilvy-trends-v2"
    location = "us-central1"
    _credentials = object()

    def __init__(self):
        self.calls = []
        self.job = Job()
        self.children = []
        self.listed = []

    def query(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        return self.job

    def load_table_from_json(self, rows, table, **kwargs):
        self.calls.append((rows, table, kwargs))
        return self.job

    def list_jobs(self, **kwargs):
        # BigQuery lists a script's statements as child jobs of the parent script job.
        self.listed.append(kwargs)
        return iter(self.children)


def setup():
    client = Client()
    meter = DynamicMeter(client)
    budget = ProductBudget({"max_bytes_billed": 100, "max_rows_written": 5, "max_model_calls": 1})
    meter.bind(budget)
    return SimpleNamespace(client=client, meter=meter, budget=budget)


def test_unbound_meter_refuses_before_dispatch():
    client = Client()
    with pytest.raises(ValueError, match="dynamic_meter_unbound"):
        DynamicMeter(client).query("SELECT 1")
    assert client.calls == []


def test_queries_share_remaining_budget_and_disable_retries():
    fx = setup()
    job = fx.meter.query("SELECT 1", job_config=bigquery.QueryJobConfig(maximum_bytes_billed=80))
    with pytest.raises(ValueError, match="dynamic_job_pending"):
        fx.meter.require_complete(fx.budget)
    job.result()
    job.result()
    fx.meter.query("SELECT 2").result()
    assert fx.budget.query_count == 2
    assert fx.budget.total_bytes_billed == 40
    assert [call[1]["job_config"].maximum_bytes_billed for call in fx.client.calls] == [80, 80]
    assert all(
        call[1]["retry"] is None and call[1]["job_retry"] is None for call in fx.client.calls
    )
    assert fx.meter._credentials is fx.client._credentials
    fx.meter.require_complete(fx.budget)


def test_load_and_transaction_reserve_rows_before_dispatch():
    fx = setup()
    fx.meter.load_table_from_json(
        [{"n": 1}, {"n": 2}], "ogilvy-trends-v2.trends_v2_staging.tmp"
    ).result()
    fx.meter.query_write("BEGIN TRANSACTION; SELECT 1; COMMIT TRANSACTION;", row_bound=2).result()
    assert fx.budget.rows_written == 4
    with pytest.raises(ValueError, match="dynamic_write_budget_exhausted"):
        fx.meter.query_write("BEGIN TRANSACTION; SELECT 1; COMMIT TRANSACTION;", row_bound=2)
    assert len(fx.client.calls) == 2


@pytest.mark.parametrize("field", ["total_bytes_billed", "num_dml_affected_rows"])
def test_unknown_query_cost_or_writes_stays_unresolved(field):
    fx = setup()
    setattr(fx.client.job, field, None)
    with pytest.raises(ValueError, match="dynamic_job_usage_unknown"):
        fx.meter.query_write("BEGIN TRANSACTION; COMMIT TRANSACTION;", row_bound=2).result()
    with pytest.raises(ValueError, match="products_effect_unknown"):
        fx.meter.query("SELECT 1")
    assert len(fx.client.calls) == 1


def test_unreserved_mutation_refuses_before_dispatch():
    fx = setup()
    with pytest.raises(ValueError, match="dynamic_write_reservation_missing"):
        fx.meter.query("DELETE FROM `ogilvy-trends-v2.trends_v2_staging.trend_scores` WHERE TRUE")
    assert fx.client.calls == []


def test_budget_limit_replacement_is_not_an_approval():
    fx = setup()
    fx.budget.limits = {**fx.budget.limits, "max_bytes_billed": 999}
    with pytest.raises(ValueError, match="dynamic_budget_binding_differs"):
        fx.meter.query("SELECT 1")
    assert fx.client.calls == []


def child(statement_type, rows=None, errors=None):
    return SimpleNamespace(statement_type=statement_type, num_dml_affected_rows=rows, errors=errors)


def script(fx, children, *, reported=3, parent=None):
    # A SCRIPT parent job carries no DML count of its own; its statements do.
    fx.client.job.num_dml_affected_rows = parent
    fx.client.job.result = lambda **kwargs: [{"_affected_rows": reported}]
    fx.client.children = children
    return fx.meter.query_write(
        "BEGIN TRANSACTION; COMMIT TRANSACTION;",
        row_bound=3,
        reported_rows_field="_affected_rows",
    )


PERSIST_CHILDREN = [
    child("BEGIN_TRANSACTION"),
    child("UPDATE", 1),
    child("ASSERT"),
    child("INSERT", 1),
    child("INSERT", 1),
    child("SELECT"),
    child("COMMIT_TRANSACTION"),
]


def test_script_rows_are_measured_by_its_child_statements():
    fx = setup()
    job = script(fx, PERSIST_CHILDREN)
    assert job.result() == [{"_affected_rows": 3}]
    fx.meter.require_complete(fx.budget)
    (listed,) = fx.client.listed
    assert listed["parent_job"] is fx.client.job
    assert listed["retry"] is None
    # The request itself stops the listing one past the limit, not only its consumer.
    assert listed["max_results"] == 1001


def test_a_merge_statement_counts_its_rows():
    fx = setup()
    job = script(
        fx,
        [
            child("BEGIN_TRANSACTION"),
            child("MERGE", 2),
            child("INSERT", 1),
            child("COMMIT_TRANSACTION"),
        ],
    )
    assert job.result() == [{"_affected_rows": 3}]
    fx.meter.require_complete(fx.budget)


def test_a_script_of_exactly_the_child_limit_is_measured():
    fx = setup()
    children = [*PERSIST_CHILDREN, *[child("SELECT")] * (1000 - len(PERSIST_CHILDREN))]
    assert len(children) == 1000
    job = script(fx, children)
    assert job.result() == [{"_affected_rows": 3}]
    fx.meter.require_complete(fx.budget)


@pytest.mark.parametrize(
    "children,parent",
    [
        ([], None),
        (PERSIST_CHILDREN[:4], None),
        ([*PERSIST_CHILDREN, child("DELETE", 1)], None),
        ([child("UPDATE", 1), child("INSERT", None), child("INSERT", 1)], None),
        ([child("UPDATE", 1), child(None), child("INSERT", 2)], None),
        ([child("UPDATE", 1), child("INSERT", 2, errors=[{"reason": "x"}])], None),
        (PERSIST_CHILDREN, 2),
        ([child("UPDATE", 0)] * 1001, None),
        # The two below agree with the reported sum, so only their own check refuses them.
        ([child("UPDATE", 1), child("INSERT", None), child("INSERT", 2)], None),
        ([*PERSIST_CHILDREN, *[child("SELECT")] * (1001 - len(PERSIST_CHILDREN))], None),
    ],
    ids=[
        "no_children",
        "short",
        "extra_dml",
        "child_without_count",
        "unknown_statement",
        "failed_child",
        "parent_disagrees",
        "unbounded",
        "child_without_count_sum_agrees",
        "over_the_limit_sum_agrees",
    ],
)
def test_a_script_count_the_server_does_not_prove_stays_unresolved(children, parent):
    # The script's own _affected_rows is self reported; unless the server's per statement
    # counts prove it, the effect is unknown rather than measured.
    fx = setup()
    job = script(fx, children, parent=parent)
    with pytest.raises(ValueError, match="dynamic_job_usage_unknown"):
        job.result()
    with pytest.raises(ValueError, match="products_effect_unknown"):
        fx.meter.query("SELECT 1")


def test_composition_sql_reports_all_affected_rows_from_server_counts():
    from src.analysis.open_intelligence.persistence import TABLE_BINDINGS, _daily_transaction_sql

    sql = _daily_transaction_sql("ogilvy-trends-v2", "trends_v2_staging", {}, None)
    assert "DECLARE inserted_receipt INT64 DEFAULT 0;" in sql
    assert "SET inserted_receipt = @@row_count;" in sql
    assert "ASSERT @@row_count = 1 AS 'daily_mutex_invalid';" in sql
    expression = "1 + inserted_receipt + " + " + ".join(
        f"inserted_{table}" for table in TABLE_BINDINGS
    )
    assert expression + " AS _affected_rows" in sql


def test_failed_creation_cannot_authorize_deleting_an_existing_table():
    fx = setup()
    deleted = []

    def failed(*args, **kwargs):
        raise ValueError("already_exists")

    fx.client.create_table = failed
    fx.client.delete_table = lambda *args, **kwargs: deleted.append(args)
    table = "ogilvy-trends-v2.trends_v2_staging.tmp"
    with pytest.raises(ValueError, match="already_exists"):
        fx.meter.create_table(table)
    with pytest.raises(ValueError, match="dynamic_cleanup_target_invalid"):
        fx.meter.delete_table(table)
    assert deleted == []


def test_json_upload_uses_the_installed_native_signature_and_disables_retries():
    from inspect import signature

    fx = setup()

    def native_load(rows, table, **kwargs):
        bound = signature(bigquery.Client.load_table_from_json).bind(
            fx.client, rows, table, **kwargs
        )
        assert bound.arguments["num_retries"] == 0
        return fx.client.job

    fx.client.load_table_from_json = native_load
    fx.meter.load_table_from_json(
        [{"n": 1}, {"n": 2}], "ogilvy-trends-v2.trends_v2_staging.tmp"
    ).result()
