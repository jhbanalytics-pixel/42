from core.eval.demo_scope import assess_answer
from core.agent.tools.warehouse import POSTS_JOIN, POST_COLUMNS, _source_sightings_join

IOL_ID = "obs1_dce53bd6bb321b436ac9d0078763dc04"
RISE_ID = "obs1_da53ac54e21872d0554146ef21188590"
NOW_C1 = (
    "Over the last 7 days, the Madlanga Commission hearings generated 45 posts across two "
    "platforms, accumulating 1,414,809 views, focusing on testimony from evidence leader "
    "Matthew Chaskalson and taxi bosses Joe Ferrari Sibanyoni and Bafana Sindane regarding "
    "extortion, leaks, and payments."
)
IOL_QUOTE = (
    "WhatsApp messages presented to the Madlanga Commission have detailed allegations that taxi "
    "bosses Joe “Ferrari” Sibanyoni and Bafana “King of the Sky” Sindane used threats, intimidation "
    "and the prospect of violent disruption to extract money from businessmen operating in Mpumalanga."
)


def test_frozen_now_c1_and_so_what_breadth_are_dropped():
    receipt = {
        "raw_answer": {
            "short_answer": NOW_C1,
            "claims": [{
                "id": "c1",
                "label": "observed",
                "text": NOW_C1,
                "evidence_ids": [
                    IOL_ID,
                    "obs1_9cf628e6c85f81a807cb3c67d15c69e6",
                    "obs1_91b74abecca06fb49efb1c5eabf55858",
                    "obs1_1fc14edf90f44d8e6e699f9fd9b92b26",
                ],
                "numbers": [
                    {"value": 45, "unit": "posts", "query_id": "q_21"},
                    {"value": 2, "unit": "platforms", "query_id": "q_21"},
                    {"value": 1414809, "unit": "views", "query_id": "q_21"},
                ],
                "quotes": [],
            }],
            "so_what": [
                {
                    "text": "South African social discourse outside mainstream party politics and football is heavily dominated by accountability around organized crime, gender-based violence, and violent crime waves.",
                    "claim_ids": ["c1"],
                },
                {
                    "text": "The Madlanga Commission hearings have become an active cross-platform public inquiry event, drawing sustained attention to taxi cartels, extortion rackets, and law enforcement complicity.",
                    "claim_ids": ["c1"],
                },
            ],
        },
        "context": {
            "window_start": "2026-09-24",
            "window_end": "2026-09-30",
            "queries": {
                "q_21": {
                    "sql": "SELECT count(*) AS post_count, sum(views) AS total_views FROM posts WHERE post_date BETWEEN '2026-09-24' AND '2026-09-30' AND LOWER(text) LIKE '%madlanga%'",
                    "purpose": "Quantify Conversation 1: Madlanga Commission",
                }
            },
        },
    }

    result = assess_answer(receipt, "DEMO-05")

    assert result["admitted_claim_ids"] == []
    dropped = {entry["claim_id"] for entry in result["dropped"]}
    assert {"c1", "short_answer", "so_what/0", "so_what/1"} <= dropped
    assert result["safe"] is False


def test_exact_source_quote_inside_window_is_admitted():
    claim_text = "The post reports allegations of threats, intimidation and violent disruption to extract money from businessmen in Mpumalanga."
    receipt = {
        "raw_answer": {
            "short_answer": claim_text,
            "claims": [{
                "id": "c1",
                "label": "observed",
                "text": claim_text,
                "evidence_ids": [IOL_ID],
                "quotes": [{"evidence_id": IOL_ID, "text": IOL_QUOTE}],
                "numbers": [],
            }],
            "so_what": [],
        },
        "context": {
            "window_start": "2026-09-24",
            "window_end": "2026-09-30",
            "evidence": [{
                "post_id": IOL_ID,
                "published_at": "2026-09-28T12:00:00+02:00",
                "text": IOL_QUOTE,
            }],
        },
    }

    result = assess_answer(receipt, "DEMO-05")

    assert result["admitted_claim_ids"] == ["c1"]
    assert result["dropped"] == []
    assert result["safe"] is True


def test_unresolved_aggregate_number_fails_closed_without_queries():
    receipt = {
        "raw_answer": {
            "claims": [{
                "id": "c1",
                "label": "observed",
                "text": "The discussion generated 45 posts.",
                "evidence_ids": [IOL_ID],
                "quotes": [{"evidence_id": IOL_ID, "text": IOL_QUOTE}],
                "numbers": [{"value": 45, "unit": "posts", "query_id": "q_21"}],
            }],
            "so_what": [],
        },
        "context": {
            "window_start": "2026-09-24",
            "window_end": "2026-09-30",
            "evidence": [{
                "post_id": IOL_ID,
                "published_at": "2026-09-28",
                "text": IOL_QUOTE,
            }],
        },
    }

    result = assess_answer(receipt, "DEMO-05")

    assert result["admitted_claim_ids"] == []
    assert result["dropped"][0]["claim_id"] == "c1"
    assert result["safe"] is False


def test_single_post_metrics_can_use_source_evidence_without_query_records():
    claim_text = "This TikTok post recorded 14,680 views, 1,901 likes, 23 comments and 34 shares."
    metrics = {"views": 14680, "likes": 1901, "comments": 23, "shares": 34}
    receipt = {
        "raw_answer": {
            "claims": [{
                "id": "c1",
                "label": "observed",
                "text": claim_text,
                "evidence_ids": [RISE_ID],
                "quotes": [{"evidence_id": RISE_ID, "text": "caption text"}],
                "numbers": [
                    {"value": value, "unit": unit, "query_id": "q_30"}
                    for unit, value in metrics.items()
                ],
            }],
            "so_what": [],
        },
        "context": {
            "window_start": "2026-09-24",
            "window_end": "2026-09-30",
            "evidence": [{
                "post_id": RISE_ID,
                "published_at": "2026-09-24T10:00:00+02:00",
                "text": "caption text",
                **metrics,
            }],
        },
    }

    result = assess_answer(receipt, "DEMO-03")

    assert result["admitted_claim_ids"] == ["c1"]
    assert result["safe"] is True


def test_linked_aggregate_query_without_market_filter_is_dropped():
    claim_text = "This post recorded 1,414,809 views."
    receipt = {
        "raw_answer": {
            "claims": [{
                "id": "c1",
                "label": "observed",
                "text": claim_text,
                "evidence_ids": [IOL_ID],
                "quotes": [{"evidence_id": IOL_ID, "text": IOL_QUOTE}],
                "numbers": [{"value": 1414809, "unit": "views", "query_id": "q_21"}],
            }],
            "so_what": [],
        },
        "context": {
            "window_start": "2026-09-24",
            "window_end": "2026-09-30",
            "evidence": [{
                "post_id": IOL_ID,
                "published_at": "2026-09-28",
                "text": IOL_QUOTE,
                "views": 1414809,
            }],
            "queries": {
                "q_21": {
                    "sql": "SELECT count(*) AS post_count, sum(views) AS total_views FROM posts WHERE post_date BETWEEN '2026-09-24' AND '2026-09-30' AND LOWER(text) LIKE '%madlanga%'",
                    "params": {},
                    "rows": [{"post_count": 45, "total_views": 1414809}],
                }
            },
        },
    }

    result = assess_answer(receipt, "DEMO-05")

    assert result["admitted_claim_ids"] == []
    assert "aggregates across source records and has no market predicate" in result["dropped"][0]["reason"]


def _dated_iol_receipt(claim_text):
    return {
        "raw_answer": {
            "claims": [{
                "id": "c1",
                "label": "observed",
                "text": claim_text,
                "evidence_ids": [IOL_ID],
                "quotes": [{"evidence_id": IOL_ID, "text": IOL_QUOTE}],
                "numbers": [],
            }],
            "so_what": [],
        },
        "context": {
            "window_start": "2026-09-24",
            "window_end": "2026-09-30",
            "evidence": [{
                "post_id": IOL_ID,
                "published_at": "2026-09-28T12:00:00+02:00",
                "text": IOL_QUOTE,
            }],
        },
    }


def test_date_component_does_not_support_a_separate_metric_value():
    receipt = _dated_iol_receipt(
        "This post, dated 28 September 2026, recorded 28 views."
    )

    result = assess_answer(receipt, "DEMO-05")

    assert result["admitted_claim_ids"] == []
    assert result["dropped"][0]["claim_id"] == "c1"


def test_exact_source_date_remains_allowed_without_a_number_receipt():
    receipt = _dated_iol_receipt("This post was published on 28 September 2026.")

    result = assess_answer(receipt, "DEMO-05")

    assert result["admitted_claim_ids"] == ["c1"]
    assert result["safe"] is True


_FETCH_POSTS_SQL = (
    f"SELECT {POST_COLUMNS} {POSTS_JOIN}"
    f"{_source_sightings_join('p', 'source_since', 'source_until')}"
    "WHERE p.post_id IN (@post_id_0)"
)


def _fetch_posts_metric_receipt(row_post_id, *, purpose="fetch_posts: 50 posts listed in query rows"):
    claim_text = "This TikTok post recorded 14,680 views."
    return {
        "raw_answer": {
            "claims": [{
                "id": "c1",
                "label": "observed",
                "text": claim_text,
                "evidence_ids": [RISE_ID],
                "quotes": [{"evidence_id": RISE_ID, "text": "caption text"}],
                "numbers": [{"value": 14680, "unit": "views", "query_id": "q_30"}],
            }],
            "so_what": [],
        },
        "context": {
            "window_start": "2026-09-24",
            "window_end": "2026-09-30",
            "evidence": [{
                "post_id": RISE_ID,
                "published_at": "2026-09-24T10:00:00+02:00",
                "text": "caption text",
                "views": 14680,
                "likes": 1901,
                "comments": 23,
                "shares": 34,
            }],
            "queries": {
                "q_30": {
                    "purpose": purpose,
                    "sql": _FETCH_POSTS_SQL,
                    "params": {
                        "post_id_0": RISE_ID,
                        "source_since": "2026-09-24",
                        "source_until": "2026-09-30",
                    },
                    "rows": [{"post_id": row_post_id, "views": 14680}],
                }
            },
        },
    }


def test_fetch_posts_source_sightings_array_agg_allows_matching_post_row():
    result = assess_answer(_fetch_posts_metric_receipt(RISE_ID), "DEMO-03")

    assert result["admitted_claim_ids"] == ["c1"]
    assert result["safe"] is True


def test_fetch_posts_source_sightings_array_agg_rejects_same_value_on_other_post():
    result = assess_answer(_fetch_posts_metric_receipt("obs1_other_post"), "DEMO-03")

    assert result["admitted_claim_ids"] == []
    assert result["safe"] is False


def test_array_agg_outside_trusted_fetch_posts_purpose_stays_rejected():
    result = assess_answer(
        _fetch_posts_metric_receipt(RISE_ID, purpose="search_posts: 1 posts listed in query rows"),
        "DEMO-03",
    )

    assert result["admitted_claim_ids"] == []
    assert result["safe"] is False
