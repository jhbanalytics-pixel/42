from datetime import datetime

import pytest

from core.agent.context import Refused, RunContext
from core.eval.ask_r2 import BudgetRefused, OperationRefused
from core.agent.tools.warehouse import retrieval_refusal_category, search_posts


def _ctx():
    return RunContext(run_id="trace-test", tier="T0", as_of=datetime(2026, 9, 28, 6, 0))


def _post(post_id):
    return {
        "post_id": post_id,
        "platform": "tiktok",
        "url": f"https://example.test/{post_id}",
        "creator_id": f"creator-{post_id}",
        "handle": f"handle-{post_id}",
        "published_at": datetime(2026, 9, 27, 9, 0),
        "post_date": datetime(2026, 9, 27).date(),
        "geo_market": "ZA",
        "geo_source": "geotag",
        "home_market": None,
        "source_sightings": [],
        "text": f"post {post_id}",
        "views": 10,
        "likes": 1,
        "comments": None,
        "shares": None,
        "engagement": 11,
        "distance": 0.1,
    }


class _Warehouse:
    def __init__(self, keyword_rows, semantic_rows=(), keyword_error=None, semantic_error=None):
        self.keyword_rows = list(keyword_rows)
        self.semantic_rows = list(semantic_rows)
        self.keyword_error = keyword_error
        self.semantic_error = semantic_error

    def dry_run(self, sql, params):
        return {
            "bytes": 1_000,
            "tables": [
                "ogilvy-trends-v2.intelligence_42_agent.tvf_search_posts",
                "ogilvy-trends-v2.intelligence_42_core.creators",
                "ogilvy-trends-v2.intelligence_42_core.posts",
                "ogilvy-trends-v2.intelligence_42_core.v_post_source_markets",
            ],
        }

    def run(self, sql, params, max_bytes_billed):
        if "tvf_search_posts" not in sql:
            if self.keyword_error is not None:
                raise self.keyword_error
            return [dict(row) for row in self.keyword_rows]
        if self.semantic_error is not None:
            raise self.semantic_error
        return [dict(row) for row in self.semantic_rows]


def _traces(ctx):
    return [event for event in ctx.events if event.get("step") == "retrieval_trace"]


def test_search_posts_traces_keyword_and_semantic_rows_separately():
    ctx = _ctx()
    out = search_posts(ctx, _Warehouse([_post("keyword")], [_post("semantic")]), "braai")

    keyword, semantic = _traces(ctx)
    assert (keyword["mode"], keyword["query_id"], keyword["row_count"], keyword["post_ids"], keyword["status"]) == (
        "keyword", out["query_ids"][0], 1, ["keyword"], "success"
    )
    assert (semantic["mode"], semantic["query_id"], semantic["row_count"], semantic["post_ids"], semantic["status"]) == (
        "semantic", out["query_ids"][1], 1, ["semantic"], "success"
    )
    assert ctx.queries[keyword["query_id"]]["rows"] == [_post("keyword")]
    assert ctx.queries[semantic["query_id"]]["rows"] == [_post("semantic")]
    assert keyword["query_id"] != semantic["query_id"]
    assert keyword["refusal_category"] is None and semantic["refusal_category"] is None


def test_search_posts_traces_semantic_refusal_without_treating_it_as_empty():
    ctx = _ctx()
    error = Refused("The query would scan 3 bytes, over the 2 byte cap. Narrow it.")
    out = search_posts(ctx, _Warehouse([_post("keyword")], semantic_error=error), "braai")

    keyword, semantic = _traces(ctx)
    assert keyword["status"] == "success" and keyword["post_ids"] == ["keyword"]
    assert semantic == {
        "step": "retrieval_trace",
        "mode": "semantic",
        "query_id": None,
        "row_count": None,
        "post_ids": [],
        "status": "refused",
        "refusal_category": "byte_cap",
        "error_type": "Refused",
    }
    assert "query would scan" not in repr(semantic)
    assert out["query_ids"] == [keyword["query_id"]]
    assert list(ctx.queries) == [keyword["query_id"]]


def test_search_posts_traces_unknown_semantic_failure_as_error():
    ctx = _ctx()
    search_posts(ctx, _Warehouse([_post("keyword")], semantic_error=TimeoutError("warehouse read timed out")), "braai")

    keyword, semantic = _traces(ctx)
    assert keyword["status"] == "success"
    assert semantic["status"] == "error"
    assert semantic["refusal_category"] == "unknown"
    assert semantic["error_type"] == "TimeoutError"


def test_search_posts_traces_a_keyword_failure_and_still_runs_the_semantic_leg():
    ctx = _ctx()
    error = Refused("Only SELECT is allowed.")

    out = search_posts(ctx, _Warehouse([], [_post("semantic")], keyword_error=error), "braai")

    keyword, semantic = _traces(ctx)
    assert keyword == {
        "step": "retrieval_trace",
        "mode": "keyword",
        "query_id": None,
        "row_count": None,
        "post_ids": [],
        "status": "refused",
        "refusal_category": "guard",
        "error_type": "Refused",
    }
    assert (semantic["mode"], semantic["status"], semantic["post_ids"]) == ("semantic", "success", ["semantic"])
    assert out["keyword"] == "unavailable" and out["keyword_reason"] == "Only SELECT is allowed."
    assert [e["id"] for e in out["evidence"]] == ["semantic"]


def test_search_posts_traces_both_failures_and_raises_with_both_reasons():
    ctx = _ctx()
    keyword_error = Refused("Only SELECT is allowed.")

    with pytest.raises(Refused) as caught:
        search_posts(ctx, _Warehouse([], keyword_error=keyword_error, semantic_error=TimeoutError("timed out")),
                     "braai")

    assert "Only SELECT is allowed." in str(caught.value) and "timed out" in str(caught.value)
    keyword, semantic = _traces(ctx)
    assert (keyword["mode"], keyword["status"], keyword["refusal_category"]) == ("keyword", "refused", "guard")
    assert (semantic["mode"], semantic["status"], semantic["error_type"]) == ("semantic", "error", "TimeoutError")


def test_search_posts_marks_successful_zero_rowsets_empty():
    ctx = _ctx()
    search_posts(ctx, _Warehouse([]), "braai")

    keyword, semantic = _traces(ctx)
    assert [(event["mode"], event["row_count"], event["post_ids"], event["status"]) for event in (keyword, semantic)] == [
        ("keyword", 0, [], "empty"),
        ("semantic", 0, [], "empty"),
    ]


def test_retrieval_refusal_category_uses_known_signals_only():
    google_forbidden = type(
        "Forbidden",
        (Exception,),
        {"__module__": "google.api_core.exceptions", "code": 403},
    )

    assert retrieval_refusal_category(Refused("The query would scan 3 bytes, over the 2 byte cap.")) == "byte_cap"
    assert retrieval_refusal_category(Refused("Only SELECT is allowed.")) == "guard"
    assert retrieval_refusal_category(google_forbidden("denied")) == "permission"
    assert retrieval_refusal_category(RuntimeError("remote model connection unavailable")) == "model_connection"
    assert retrieval_refusal_category(RuntimeError("404 function not found")) == "unknown"


def test_generic_python_timeouts_are_not_model_connection_errors():
    assert retrieval_refusal_category(ConnectionError("warehouse connection failed")) == "unknown"
    assert retrieval_refusal_category(TimeoutError("warehouse request timed out")) == "unknown"


def test_generic_google_timeouts_are_not_model_connection_errors():
    service_unavailable = type(
        "ServiceUnavailable",
        (Exception,),
        {"__module__": "google.api_core.exceptions", "code": 503},
    )
    deadline_exceeded = type(
        "DeadlineExceeded",
        (Exception,),
        {"__module__": "google.api_core.exceptions", "code": 504},
    )

    assert retrieval_refusal_category(service_unavailable("temporarily unavailable")) == "unknown"
    assert retrieval_refusal_category(deadline_exceeded("deadline exceeded")) == "unknown"


def test_local_ask_refusals_are_guards():
    assert retrieval_refusal_category(OperationRefused("blocked")) == "guard"
    assert retrieval_refusal_category(BudgetRefused("budget reached")) == "guard"
