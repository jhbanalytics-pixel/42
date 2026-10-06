from types import SimpleNamespace

import pytest

from core.agent.tools.sql_query import MAX_ROWS, BigQueryWarehouse


class FakeJob:
    def __init__(self):
        self.result_kwargs = None

    def result(self, *, max_results, job_retry="default"):
        self.result_kwargs = {"max_results": max_results, "job_retry": job_retry}
        return [{"post_id": "p1"}]


class FakeClient:
    def __init__(self):
        self.job = FakeJob()
        self.query_kwargs = None

    def query(self, sql, *, job_config, job_retry="default"):
        self.query_kwargs = {"sql": sql, "job_config": job_config, "job_retry": job_retry}
        return self.job


@pytest.mark.parametrize(
    ("sql", "disable_retry"),
    [
        ("SELECT * FROM intelligence_42_agent.tvf_search_posts(@q, NULL, @since, @until, @k)", True),
        ("SELECT * FROM `ogilvy-trends-v2.intelligence_42_agent.tvf_search_posts`(@q, NULL, @since, @until, @k)", True),
        ("SELECT * FROM `ogilvy-trends-v2`.intelligence_42_agent.tvf_search_posts(@q, NULL, @since, @until, @k)", True),
        ("SELECT * FROM intelligence_42_core.posts", False),
        ("SELECT 'tvf_search_posts()' AS label", False),
    ],
)
def test_run_disables_job_retries_only_for_semantic_embedding_queries(monkeypatch, sql, disable_retry):
    warehouse = BigQueryWarehouse()
    client = FakeClient()
    warehouse._client = client
    monkeypatch.setattr(
        warehouse,
        "_bq",
        lambda: SimpleNamespace(QueryJobConfig=lambda **kwargs: SimpleNamespace(**kwargs)),
    )

    rows = warehouse.run(sql, {}, max_bytes_billed=1234)

    assert client.query_kwargs["job_config"].maximum_bytes_billed == 1234
    assert client.query_kwargs["job_retry"] is (None if disable_retry else "default")
    assert client.job.result_kwargs == {
        "max_results": MAX_ROWS + 1,
        "job_retry": None if disable_retry else "default",
    }
    assert rows == [{"post_id": "p1"}]
