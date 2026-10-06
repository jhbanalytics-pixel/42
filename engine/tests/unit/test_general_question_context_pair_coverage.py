"""Pair coverage wiring: SQL shape, the index cut, admission parity and mutation controls.

Relevance labels live in LABELS and are read only after selection, so the substring rule the
implementation uses can never grade itself.
"""

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from src.analysis.open_intelligence import general_question_context_admission as admission
from src.analysis.open_intelligence import general_question_context_queries as queries
from src.analysis.open_intelligence.general_question_context_selection import select_context_facts
from src.analysis.open_intelligence.general_question_plan import validate_question_plan
from src.analysis.open_intelligence.general_question_policy import build_intake_context
from src.analysis.open_intelligence.general_question_request import normalize_question_request
from src.analysis.open_intelligence.production_snapshot_tables import LANES

MARKETS = ["ke", "ng", "za"]
ADMISSION_MARKETS = ["ke", "ng", "tz", "za"]
PLATFORMS = ("web", "reddit", "youtube", "tiktok", "threads")
REQUIREMENTS = [
    {
        "requirement_id": requirement_id,
        "question": "What is observed?",
        "kind": "content",
        "mandatory": mandatory,
        "search_terms": [term],
    }
    for requirement_id, mandatory, term in (
        ("r1", True, "commuting"),
        ("r2", True, "matatu fares"),
        ("r3", False, "fuel price"),
    )
]
FILLER = " ".join(
    f"paragraph {i} about the airport opening and the stadium roof" for i in range(90)
)
LABELS = {}
ABSENT_PAIRS = [
    {"requirement_id": "r1", "market": "tz"},
    {"requirement_id": "r2", "market": "ng"},
    {"requirement_id": "r2", "market": "tz"},
    {"requirement_id": "r2", "market": "za"},
]


def row(market, row_id, text, observed, *, title=None, platform="web", label):
    LABELS[row_id] = label
    return {
        "market": market,
        "row_id": row_id,
        "content_digest": "digest-" + row_id,
        "text": text,
        "title": title,
        "source_label": "publisher",
        "source_family": "news",
        "platform": platform,
        "published_at": observed,
        "collected_at": observed,
        "lane": "enriched_content",
    }


def corpus():
    rows = [
        row(
            "ke",
            f"early-{i:03}",
            "airport opening ceremony draws crowds",
            "2026-09-01T00:00:00+00:00",
            platform=PLATFORMS[i % 5],
            label="irrelevant",
        )
        for i in range(240)
    ]
    rows += [
        row(
            "ke",
            f"lit-{i:03}",
            "commuting allowance policy for staff",
            "2026-09-02T00:00:00+00:00",
            platform=PLATFORMS[i % 5],
            label="irrelevant",
        )
        for i in range(24)
    ]
    assert len(FILLER) > 4000
    late = (
        (
            "ke",
            "rel-ke-compare",
            "matatu fares rose sharply while households changed commuting routines",
            "2026-09-07T10:00:00+00:00",
            None,
        ),
        (
            "ke",
            "rel-ke-local",
            "Nauli za matatu fares zimepanda na wakazi wanabadili safari zao",
            "2026-09-06T09:00:00+00:00",
            None,
        ),
        (
            "ke",
            "rel-ke-late-term",
            FILLER + " commuting routines shifted this week",
            "2026-09-06T08:00:00+00:00",
            None,
        ),
        (
            "ke",
            "rel-ke-dup-a",
            "matatu fares doubled on the Thika road",
            "2026-09-05T08:00:00+00:00",
            "Fare shock",
        ),
        (
            "ke",
            "rel-ke-dup-b",
            "matatu fares doubled on the Mombasa road",
            "2026-09-05T07:00:00+00:00",
            "Fare shock",
        ),
        (
            "za",
            "rel-za-para",
            "households changed commuting routines after minibus taxi fares rose",
            "2026-09-07T09:00:00+00:00",
            None,
        ),
        (
            "za",
            "rel-za-compare",
            "commuting costs climbed with the fuel price in Gauteng",
            "2026-09-07T08:00:00+00:00",
            None,
        ),
        (
            "ng",
            "rel-ng-late",
            "Lagos households changed commuting routines with danfo fares up",
            "2026-09-07T07:00:00+00:00",
            None,
        ),
        (
            "ng",
            "rel-ng-compare",
            "commuting budgets strained by the fuel price rise",
            "2026-09-07T06:00:00+00:00",
            None,
        ),
    )
    rows += [row(m, i, t, o, title=title, label="relevant") for m, i, t, o, title in late]
    rows.append(
        row(
            "gh",
            "foreign-gh",
            "commuting routines in Accra changed",
            "2026-09-07T05:00:00+00:00",
            label="foreign_local",
        )
    )
    return rows


def relevant_ids(rows):
    return {item["row_id"] for item in rows if LABELS[item["row_id"]] == "relevant"}


def harness_inputs():
    request = normalize_question_request(
        {"message": "What explains rising commuting costs?"},
        scope={
            "client_scope_id": "synthetic_scope",
            "market_scope": MARKETS,
            "brand_config_id": None,
            "audience_lens_ids": [],
            "theme_id": None,
        },
        request_id="00000000-0000-4000-8000-000000000206",
        admitted_at=datetime(2026, 9, 9, 12, tzinfo=UTC),
        policy_digest="a" * 64,
    )
    intake = build_intake_context(request, selected_market="ke")
    plan = validate_question_plan(
        {
            "status": "ready",
            "intent": "discovery",
            "markets": MARKETS,
            "window": {"start": "2026-09-01", "end": "2026-09-08", "closed": True},
            "decision": None,
            "requirements": REQUIREMENTS,
            "clarification": None,
            "limitations": [],
        },
        request=request,
        intake=intake,
    )
    source = {
        "profile_id": "protected_context_20260907_v1",
        "source_relation_set_id": "trends_v2_dev_table_snapshot_v1",
        "operation": "source_snapshot_capture",
        "cutoff_date": "2026-09-07",
        "source_as_of": "2026-09-08T00:00:00+00:00",
        "captured_at": "2026-09-08T00:05:00+00:00",
        "client_scope_id": "synthetic_scope",
        "market_scope": MARKETS,
        "snapshot_digest": "b" * 64,
        "snapshot_tables": [
            {
                "lane": lane,
                "snapshot_table": "ogilvy-trends-v2.trends_v2_staging.open_intelligence_v3_source_20260907_"
                + lane,
                "snapshot_time": "2026-09-08T00:00:00+00:00",
                "schema_digest": "c" * 64,
                "row_count": 10,
                "physical_result_digest": "d" * 64,
                "adapted_result_digest": "e" * 64,
            }
            for lane in LANES
        ],
    }
    return request, plan, intake, source


def index_query(limit):
    request, plan, intake, source = harness_inputs()
    candidate_rows = [
        {
            "lane": "__metadata__",
            "market": None,
            "candidate_key": None,
            "sample_row_ids": [],
            "payload": json.dumps(
                {
                    "candidate_count": 1,
                    "full_matching_count": 1,
                    "overflow": False,
                    "source_snapshot_digest": source["snapshot_digest"],
                    "profile_id": source["profile_id"],
                }
            ),
        },
        {
            "lane": "seed_graph",
            "market": "za",
            "candidate_key": "seed",
            "sample_row_ids": ["seed-za"],
            "payload": json.dumps({"market": "za", "sample_row_ids": ["seed-za"]}),
        },
    ]
    return queries.build_context_index_query(
        request,
        plan,
        intake,
        source,
        candidate_rows=candidate_rows,
        lane="enriched_content",
        candidate_limit=limit,
        partition_rows=[
            {"lane": "__metadata__", "partition_date": None, "partition_count": 1},
            {"lane": "enriched_content", "partition_date": "2026-09-07", "partition_count": 1},
        ],
    )


def keyed_index_query(limit, rows):
    """The keyed shape: every in scope corpus row arrives as a candidate sample key."""
    request, plan, intake, source = harness_inputs()
    keys = {}
    for item in rows:
        if item["market"] in MARKETS:
            keys.setdefault(item["market"], []).append(item["row_id"])
    candidate_rows = [
        {
            "lane": "__metadata__",
            "market": None,
            "candidate_key": None,
            "sample_row_ids": [],
            "payload": json.dumps(
                {
                    "candidate_count": len(keys),
                    "full_matching_count": len(keys),
                    "overflow": False,
                    "source_snapshot_digest": source["snapshot_digest"],
                    "profile_id": source["profile_id"],
                }
            ),
        },
        *(
            {
                "lane": "seed_graph",
                "market": market,
                "candidate_key": "seed-" + market,
                "sample_row_ids": ids,
                "payload": json.dumps({"market": market, "sample_row_ids": ids}),
            }
            for market, ids in sorted(keys.items())
        ),
    ]
    return queries.build_context_index_query(
        request,
        plan,
        intake,
        source,
        candidate_rows=candidate_rows,
        lane="enriched_content",
        candidate_limit=limit,
    )


def parameters(query):
    return {
        item.name: getattr(item, "values", getattr(item, "value", None))
        for item in query.parameters
    }


def sql_match_texts(query, rows, anchor):
    """Per row text the query's own STRPOS operand yields, evaluated in SQLite.

    Every STRPOS against the anchor must share one operand, so the branches cannot drift
    apart and a clipped operand in the SQL shows up as a lost match here. None means the
    query has no such predicate.
    """
    operands = set(re.findall(r"STRPOS\((.+?)," + re.escape(anchor) + r"\)>0", query.sql))
    if not operands:
        return None
    assert len(operands) == 1, "one SQL match operand per anchor"
    (operand,) = operands
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.create_function(
            "LOWER", 1, lambda value: None if value is None else value.lower()
        )
        connection.create_function(
            "CONCAT", -1, lambda *parts: None if None in parts else "".join(parts)
        )
        connection.execute("CREATE TABLE evidence (row_id TEXT,text TEXT,title TEXT)")
        connection.executemany(
            "INSERT INTO evidence VALUES (?,?,?)",
            [(item["row_id"], item["text"], item["title"]) for item in rows],
        )
        return dict(connection.execute(f"SELECT row_id,{operand} FROM evidence").fetchall())


def emulate_branches(query, rows):
    """Rows the eligible and hit branches would emit, using the query's own arrays and match text."""
    params = parameters(query)
    term_texts = sql_match_texts(query, rows, "term")
    relation_texts = sql_match_texts(query, rows, "relation.term")
    assert relation_texts is not None
    relation = list(zip(params["relation_requirement_ids"], params["relation_terms"], strict=True))
    keyed = "eligible_partition_dates" not in params
    keys = set(zip(params["sample_markets"], params["sample_ids"], strict=True))
    assert keyed or term_texts is not None
    matching, hits = [], []
    for item in rows:
        if item["market"] not in params["markets"]:
            continue
        if keyed and (item["market"], item["row_id"]) not in keys:
            continue
        relevance = (
            0
            if term_texts is None
            else sum(term in term_texts[item["row_id"]] for term in params["search_terms"])
        )
        if not relevance and not keyed:
            continue
        payload = json.dumps(
            {
                "market": item["market"],
                "id": item["row_id"],
                "collected_date": item["collected_at"][:10],
            }
        )
        matching.append(
            (
                item["lane"],
                item["market"],
                item["row_id"],
                payload,
                1,
                item["platform"],
                relevance,
                1,
            )
        )
        for requirement_id, mandatory in zip(
            params["requirement_ids"], params["requirement_mandatory"], strict=True
        ):
            terms = [term for owner, term in relation if owner == requirement_id]
            if not terms or any(term in relation_texts[item["row_id"]] for term in terms):
                hits.append(
                    (
                        item["lane"],
                        item["market"],
                        item["row_id"],
                        requirement_id,
                        int(mandatory),
                        item["published_at"] or item["collected_at"],
                    )
                )
    return matching, hits


def run_cut(query, rows):
    """Execute the pair coverage CTEs through the cap over emulated branch rows."""
    cut = query.sql.split("), pair_ranks AS (", 1)[1].split("), output AS (", 1)[0]
    matching, hits = emulate_branches(query, rows)
    with closing(sqlite3.connect(":memory:")) as connection, connection:
        connection.create_function(
            "IF", 3, lambda condition, left, right: left if condition else right
        )
        connection.execute(
            "CREATE TABLE matching (lane TEXT,market TEXT,id TEXT,payload TEXT,match_count INTEGER,source_platform TEXT,term_relevance INTEGER,seed_priority INTEGER)"
        )
        connection.execute(
            "CREATE TABLE requirement_hits (lane TEXT,market TEXT,id TEXT,requirement_id TEXT,mandatory INTEGER,observed_at TEXT)"
        )
        connection.executemany("INSERT INTO matching VALUES (?,?,?,?,?,?,?,?)", matching)
        connection.executemany("INSERT INTO requirement_hits VALUES (?,?,?,?,?,?)", hits)
        picked = connection.execute(
            "WITH pair_ranks AS (" + cut + ") SELECT market,id FROM picked ORDER BY ordering",
            {"candidate_limit": query.candidate_limit},
        ).fetchall()
    covered = {
        (requirement_id, market)
        for _, market, row_id, requirement_id, _, _ in hits
        if (market, row_id) in set(picked)
    }
    return [row_id for _, row_id in picked], covered, len(matching)


def selector_rows(rows):
    return [
        {
            "market": item["market"],
            "row_id": item["row_id"],
            "content_digest": item["content_digest"],
            "excerpt": ((item["text"] or "") + " " + (item["title"] or "")).strip(),
            "source_label": item["source_label"],
            "source_family": item["source_family"],
            "platform": item["platform"],
            "published_at": item["published_at"],
            "collected_at": item["collected_at"],
            "lane": item["lane"],
        }
        for item in rows
    ]


def selector_requirements():
    return [
        {key: item[key] for key in ("requirement_id", "mandatory", "search_terms")}
        for item in REQUIREMENTS
    ]


def covered_pairs(result):
    return {
        (requirement_id, result["facts"][index]["market"])
        for requirement_id, indices in result["coverage"].items()
        for index in indices
    }


def identities(result):
    return [(fact["market"], fact["row_id"], fact["content_digest"]) for fact in result["facts"]]


def admission_facts(rows):
    """Admission shaped facts, with the selection text taken through the admission seam."""
    facts = []
    for item in rows:
        payload = {"text": item["text"], "title": item["title"]}
        facts.append(
            {
                **{
                    key: item[key]
                    for key in (
                        "market",
                        "row_id",
                        "content_digest",
                        "source_label",
                        "source_family",
                        "platform",
                        "published_at",
                        "collected_at",
                        "lane",
                    )
                },
                "excerpt": (item["text"] or item["title"])[:4000],
                "selection_text": admission._selection_text(payload),
            }
        )
    return facts


def assert_index_cut_keeps_relevant_rows(limit=24):
    rows = corpus()
    query = index_query(limit)
    survivors, covered, matching_count = run_cut(query, rows)
    assert len(survivors) == limit
    assert matching_count == 33, "every eligible row matches on its full text"
    assert relevant_ids(rows) <= set(survivors), "relevant identities survive the index cut"
    assert "foreign-gh" not in survivors
    expected = select_context_facts(
        [item for item in selector_rows(rows) if item["market"] in MARKETS],
        selector_requirements(),
        MARKETS,
        limit=limit,
    )
    assert covered == covered_pairs(expected), "pair coverage matches the pure selector"


def assert_keyed_index_cut_keeps_relevant_rows(limit=24):
    rows = corpus()
    query = keyed_index_query(limit, rows)
    survivors, covered, matching_count = run_cut(query, rows)
    assert len(survivors) == limit
    assert matching_count == 273
    assert relevant_ids(rows) <= set(survivors), "relevant identities survive the keyed index cut"
    assert "foreign-gh" not in survivors
    expected = select_context_facts(
        [item for item in selector_rows(rows) if item["market"] in MARKETS],
        selector_requirements(),
        MARKETS,
        limit=limit,
    )
    assert covered == covered_pairs(expected), "keyed pair coverage matches the pure selector"


def assert_admission_allocation_matches_selector(limit=24):
    rows = corpus()
    plan = {"markets": ADMISSION_MARKETS, "requirements": REQUIREMENTS}
    got = admission._allocate_context_facts(admission_facts(rows), plan, limit=limit)
    expected = select_context_facts(
        selector_rows(rows), selector_requirements(), ADMISSION_MARKETS, limit=limit
    )
    assert identities(got) == identities(expected), "admission identities match the pure selector"
    assert got["coverage"] == expected["coverage"], "admission coverage matches the pure selector"
    assert relevant_ids(rows) <= {row_id for _, row_id, _ in identities(got)}, (
        "relevant identities survive admission"
    )
    assert "foreign-gh" not in {row_id for _, row_id, _ in identities(got)}
    assert got["uncovered_required_pairs"] == ABSENT_PAIRS
    return got


def test_index_and_candidate_queries_carry_pair_coverage_before_the_cap():
    query = index_query(24)
    assert query.sql == index_query(24).sql
    names = re.findall(r"\), (\w+) AS \(", query.sql)
    assert names[names.index("requirements") :] == [
        "requirements",
        "relation",
        "matching",
        "requirement_hits",
        "pair_ranks",
        "priority",
        "eligible",
        "balanced",
        "picked",
        "output",
    ]
    picked = query.sql.split("), picked AS (", 1)[1].split("), output AS (", 1)[0]
    assert picked.index("ORDER BY selection_priority,pair_rank,") < picked.index(
        "LIMIT @candidate_limit"
    )
    assert "(SELECT COUNT(*) FROM eligible) AS full_matching_count" in query.sql
    assert "DATE(evidence.collected_at) IN UNNEST(@eligible_partition_dates)" in query.sql
    assert "@source_binding_digest IS NOT NULL" in query.sql
    typed = {item.name: item for item in query.parameters}
    for name, array_type, values in (
        ("requirement_ids", "STRING", ["r1", "r2", "r3"]),
        ("requirement_mandatory", "BOOL", [True, True, False]),
        ("relation_requirement_ids", "STRING", ["r1", "r2", "r3"]),
        ("relation_terms", "STRING", ["commuting", "matatu fares", "fuel price"]),
        ("relation_methods", "STRING", ["normalized_literal"] * 3),
    ):
        assert typed[name].array_type == array_type
        assert typed[name].values == values
    for literal in ("'r1'", "'r2'", "matatu fares", "fuel price", "normalized_literal"):
        assert literal not in query.sql
    request, plan, intake, source = harness_inputs()
    candidate = queries.build_context_candidate_query(
        request, plan, intake, source, candidate_limit=10
    )
    assert (
        candidate.sql
        == queries.build_context_candidate_query(
            request, plan, intake, source, candidate_limit=10
        ).sql
    )
    assert re.findall(r"\), (\w+) AS \(", candidate.sql)[-8:] == [
        "requirements",
        "relation",
        "requirement_hits",
        "pair_ranks",
        "priority",
        "eligible",
        "picked",
        "output",
    ]
    assert candidate.sql.index("ORDER BY selection_priority,pair_rank,") < candidate.sql.index(
        "LIMIT @candidate_limit"
    )
    assert set(typed) - {"sample_markets", "sample_ids", "seed_markets", "seed_ids"} - {
        "eligible_partition_dates",
        "direct_content_fallback",
    } <= {item.name for item in candidate.parameters}


def test_keyed_index_query_carries_pair_coverage_and_keeps_relevant_rows():
    rows = corpus()
    query = keyed_index_query(24, rows)
    assert re.findall(r"\), (\w+) AS \(", query.sql) == [
        "requirements",
        "relation",
        "matching",
        "requirement_hits",
        "pair_ranks",
        "priority",
        "eligible",
        "picked",
        "output",
    ]
    assert "JOIN requested USING(market,id)" in query.sql
    assert (
        "DATE(evidence.collected_at) BETWEEN DATE_SUB(@capture_cutoff, INTERVAL 6 DAY) "
        "AND @capture_cutoff"
    ) in query.sql
    picked = query.sql.split("), picked AS (", 1)[1].split("), output AS (", 1)[0]
    assert picked.index("ORDER BY selection_priority,pair_rank,market,id,payload") < picked.index(
        "LIMIT @candidate_limit"
    )
    assert [item.name for item in query.parameters][-5:] == [
        "requirement_ids",
        "requirement_mandatory",
        "relation_requirement_ids",
        "relation_terms",
        "relation_methods",
    ]
    assert_keyed_index_cut_keeps_relevant_rows()


def test_24_literal_rows_precede_relevant_rows_and_still_lose_the_cut():
    rows = corpus()
    literal = [item for item in rows if item["row_id"].startswith("lit-")]
    assert len(literal) == 24
    relevant = [item for item in rows if LABELS[item["row_id"]] == "relevant"]
    for item in literal:
        assert "commuting" in item["text"]
        assert LABELS[item["row_id"]] == "irrelevant"
    assert max(item["row_id"] for item in literal) < min(item["row_id"] for item in relevant)
    assert max(item["published_at"] for item in literal) < min(
        item["published_at"] for item in relevant
    )
    assert_index_cut_keeps_relevant_rows()


def test_admission_allocation_matches_the_selector_and_reports_post_cap_loss():
    assert_admission_allocation_matches_selector()
    rows = corpus()
    plan = {"markets": ADMISSION_MARKETS, "requirements": REQUIREMENTS}
    facts = admission_facts(rows)
    capped = admission._allocate_context_facts(facts, plan, limit=2)
    unlimited = admission._allocate_context_facts(facts, plan, limit=len(facts))
    assert unlimited["uncovered_required_pairs"] == ABSENT_PAIRS
    assert capped["uncovered_required_pairs"] == sorted(
        [*ABSENT_PAIRS, {"requirement_id": "r1", "market": "za"}],
        key=lambda pair: (pair["requirement_id"], pair["market"]),
    )
    absent_line = (
        "No eligible record was retrieved for these mandatory requirement and market pairs: "
        "r1 in tz, r2 in ng, r2 in tz, r2 in za."
    )
    assert admission._coverage_limitations(capped, unlimited) == [
        "Contextual evidence selection was capped at 2 of 33 eligible records.",
        "Capacity was exhausted before these mandatory requirement and market pairs were "
        "covered: r1 in za.",
        absent_line,
    ]
    assert admission._coverage_limitations(unlimited, unlimited) == [absent_line]


def test_mutation_controls_each_fail_a_relevant_assertion(monkeypatch):
    original_ctes = queries._pair_coverage_ctes
    original_text = admission._selection_text
    original_index = queries.build_context_index_query
    evidence_text = "LOWER(CONCAT(COALESCE(evidence.text,''),' ',COALESCE(evidence.title,'')))"

    def clipped_sql_match(*args, **kwargs):
        query = original_index(*args, **kwargs)
        assert evidence_text in query.sql
        return replace(
            query, sql=query.sql.replace(evidence_text, f"SUBSTR({evidence_text},1,4000)")
        )

    def flat_priority(identity, rank_keys):
        return original_ctes(identity, rank_keys).replace(
            "MIN(IF(pair_rank=1,IF(mandatory,0,1),2)) AS selection_priority,MIN(pair_rank) AS pair_rank",
            "3 AS selection_priority,0 AS pair_rank",
        )

    def lexical_market_order(facts, requirements, markets, *, limit, relations=None):
        chosen = sorted(
            (fact for fact in facts if fact["market"] in markets),
            key=lambda fact: (fact["market"], fact["row_id"]),
        )[:limit]
        return {
            "facts": [{**fact, "retrieval_signals": []} for fact in chosen],
            "coverage": {item["requirement_id"]: [] for item in requirements},
            "omitted_eligible_count": 0,
            "uncovered_required_pairs": [],
        }

    with monkeypatch.context() as context:
        context.setattr(queries, "_pair_coverage_ctes", flat_priority)
        with pytest.raises(AssertionError, match="relevant identities survive the index cut"):
            assert_index_cut_keeps_relevant_rows()
    assert_index_cut_keeps_relevant_rows()
    with monkeypatch.context() as context:
        context.setattr(queries, "_pair_coverage_ctes", flat_priority)
        with pytest.raises(AssertionError, match="relevant identities survive the keyed index cut"):
            assert_keyed_index_cut_keeps_relevant_rows()
    assert_keyed_index_cut_keeps_relevant_rows()
    with monkeypatch.context() as context:
        context.setattr(admission, "select_context_facts", lexical_market_order)
        with pytest.raises(AssertionError, match="admission identities match the pure selector"):
            assert_admission_allocation_matches_selector()
    assert_admission_allocation_matches_selector()
    with monkeypatch.context() as context:
        context.setattr(admission, "_selection_text", lambda payload: original_text(payload)[:4000])
        with pytest.raises(AssertionError, match="admission identities match the pure selector"):
            assert_admission_allocation_matches_selector()
    assert_admission_allocation_matches_selector()
    with monkeypatch.context() as context:
        context.setattr(queries, "build_context_index_query", clipped_sql_match)
        with pytest.raises(AssertionError, match="every eligible row matches on its full text"):
            assert_index_cut_keeps_relevant_rows()
    assert_index_cut_keeps_relevant_rows()
