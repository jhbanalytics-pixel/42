"""Deterministic contract tests for Open Intelligence staging views."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_VIEWS_DIR = _ROOT / "infra" / "bigquery_views"

_BOARD_FIELDS = (
    "contract_version",
    "run_id",
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "signal_id",
    "signal_date",
    "market",
    "label",
    "cluster_build_version",
    "discovery_mode",
    "topic_tags",
    "novelty_score",
    "velocity_score",
    "breadth_score",
    "independence_score",
    "historical_similarity",
    "geo_confidence",
    "evidence_state",
    "summary",
    "why_now",
    "possible_response",
    "limitations",
    "contradictions",
    "evidence_ids",
    "human_review_required",
    "created_at",
)

_EVIDENCE_FIELDS = (
    "contract_version",
    "run_id",
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "signal_date",
    "market",
    "signal_id",
    "evidence_id",
    "row_id",
    "source_family",
    "platform",
    "source_label",
    "author_label",
    "excerpt",
    "metric_label",
    "url",
    "published_at",
    "claim_role",
    "direction",
    "geo_confidence",
    "evidence_state",
    "availability",
    "created_at",
)

_SOURCE_LAB_FIELDS = (
    "contract_version",
    "run_id",
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "source_performance_id",
    "metric_date",
    "vendor",
    "http_method",
    "route_path",
    "endpoint_id",
    "route_role",
    "source_family",
    "market",
    "status",
    "platform",
    "resource",
    "catalog_digest",
    "official_credits",
    "official_credits_label",
    "official_archetype",
    "catalog_paginated",
    "catalog_metered",
    "cache_ttl_seconds",
    "delivery",
    "docs_url",
    "calls",
    "credits",
    "rows",
    "integrity",
    "geo_precision",
    "unique_lift",
    "last_success_at",
    "downstream_consumers",
    "official_capability",
    "official_parameters",
    "official_price_components",
    "blocking_reason",
    "kill_test_result",
    "review_date",
    "last_checked_at",
    "balance",
    "observed_at",
    "balance_read_status",
    "recent_deductions",
    "deductions_interval",
    "funding_math_status",
    "funded_increase_amount",
    "funded_increase_observed",
    "selected_top_up_credits",
    "baseline_credits_per_day",
    "baseline_window",
    "runway_days",
    "monthly_optional_credit_cap",
    "monthly_optional_credits_used",
    "optional_calls_enabled",
)

_HEALTH_FIELDS = (
    "contract_version",
    "run_id",
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "signal_date",
    "market",
    "candidate_count",
    "ready_count",
    "thin_count",
    "contradictory_count",
    "unchecked_count",
    "evidence_count",
    "analysis_count",
    "prediction_count",
    "latest_created_at",
)

_DESK_DYNAMIC_FIELDS = (
    "contract_version",
    "run_id",
    "client_scope_id",
    "market_scope",
    "signal_date",
    "observation_start",
    "observation_end",
    "observation_method",
    "run_completed_at",
    "signal_id",
    "market",
    "signal_name",
    "discovery_mode",
    "evidence_state",
    "why_now",
    "possible_response",
    "prediction_id",
    "predicted_at",
    "receipts",
)

_DESK_DYNAMIC_V2_FIELDS = (
    *_DESK_DYNAMIC_FIELDS,
    "velocity_score",
    "novelty_score",
    "breadth_score",
    "independence_score",
    "historical_similarity",
    "geo_confidence",
    "topic_tags",
    "evidence_summary",
    "ribbon_series",
)
# The two instrument members the desk renders as a Proof Ribbon, derived inside
# the view from the released rows (5 Sep 2026): a receipt per available evidence
# row with an excerpt and a publish time, and one strand per source family that
# holds a qualifying receipt. Their shape is the design system contract, so the
# literals asserted in the v2 contract are the contract's own words.
_DESK_INSTRUMENT_FIELDS = ("evidence_summary", "ribbon_series")

_CONTRACTS = {
    "v_desk_dynamic_signals_v1.sql": {
        "fields": _DESK_DYNAMIC_FIELDS,
        "relations": (
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "open_intelligence_run_receipts_v1",
            ),
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "signal_predictions_v2",
            ),
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "signal_candidates_v2",
            ),
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "signal_membership_v2",
            ),
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "signal_evidence_v2",
            ),
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "signal_analysis_v2",
            ),
            ("from", "cte", "run_receipts"),
            ("from", "cte", "predictions"),
            ("from", "cte", "memberships"),
            ("from", "cte", "evidence"),
            ("from", "cte", "analysis"),
            ("from", "cte", "released_runs"),
            ("inner_join", "cte", "candidates"),
            ("inner_join", "cte", "released_predictions"),
            ("inner_join", "cte", "member_types"),
            ("left_join", "cte", "available_receipts"),
            ("left_join", "cte", "same_run_analysis"),
        ),
    },
    "v_desk_dynamic_signals_v2.sql": {
        "fields": _DESK_DYNAMIC_V2_FIELDS,
        "relations": (
            ("from", "physical", "{project}", "{dataset}", "open_intelligence_run_receipts_v1"),
            ("from", "physical", "{project}", "{dataset}", "signal_candidates_v2"),
            ("from", "cte", "provenance_candidates"),
            ("inner_join", "cte", "provenance_receipts"),
            ("from", "physical", "{project}", "{dataset}", "signal_membership_v2"),
            ("inner_join", "cte", "provenance_receipts"),
            ("left_join", "cte", "provenance_candidates"),
            ("from", "cte", "provenance_receipts"),
            ("left_join", "cte", "provenance_candidate_checks"),
            ("left_join", "cte", "provenance_membership_checks"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "physical", "{project}", "{dataset}", "signal_candidates_v2"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "physical", "{project}", "{dataset}", "signal_evidence_v2"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "physical", "{project}", "{dataset}", "signal_membership_v2"),
            ("inner_join", "cte", "valid_provenance_runs"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "physical", "{project}", "{dataset}", "signal_candidates_v2"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "physical", "{project}", "{dataset}", "signal_evidence_v2"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "physical", "{project}", "{dataset}", "signal_membership_v2"),
            ("inner_join", "cte", "valid_provenance_runs"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "physical", "{project}", "{dataset}", "signal_lineage_v2"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "physical", "{project}", "{dataset}", "signal_predictions_v2"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "unnest", "unnest"),
            ("from", "physical", "{project}", "{dataset}", "signal_outcomes_v2"),
            ("from", "physical", "{project}", "{dataset}", "signal_analysis_v2"),
            ("from", "cte", "projection_candidates"),
            ("from", "cte", "projection_evidence"),
            ("from", "cte", "projection_membership"),
            ("from", "cte", "row_set_analysis"),
            ("from", "cte", "row_set_candidates"),
            ("from", "cte", "row_set_evidence"),
            ("from", "cte", "row_set_lineage"),
            ("from", "cte", "row_set_membership"),
            ("from", "cte", "row_set_outcomes"),
            ("from", "cte", "row_set_predictions"),
            ("from", "derived"),
            ("from", "physical", "{project}", "{dataset}", "signal_evidence_v2"),
            ("from", "physical", "{project}", "{dataset}", "open_intelligence_run_receipts_v1"),
            ("from", "cte", "valid_provenance_runs"),
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "open_intelligence_quality_release_records_v2",
            ),
            ("from", "cte", "quality_releases"),
            ("inner_join", "cte", "run_receipts"),
            ("left_join", "cte", "packet_items_agg"),
            ("left_join", "cte", "projection_candidates_agg"),
            ("left_join", "cte", "projection_evidence_agg"),
            ("left_join", "cte", "projection_membership_agg"),
            ("left_join", "cte", "row_set_analysis_agg"),
            ("left_join", "cte", "row_set_candidates_agg"),
            ("left_join", "cte", "row_set_evidence_agg"),
            ("left_join", "cte", "row_set_lineage_agg"),
            ("left_join", "cte", "row_set_membership_agg"),
            ("left_join", "cte", "row_set_outcomes_agg"),
            ("left_join", "cte", "row_set_predictions_agg"),
            ("from", "physical", "{project}", "{dataset}", "signal_predictions_v2"),
            ("from", "physical", "{project}", "{dataset}", "signal_candidates_v2"),
            ("from", "physical", "{project}", "{dataset}", "signal_membership_v2"),
            ("from", "physical", "{project}", "{dataset}", "signal_evidence_v2"),
            ("from", "physical", "{project}", "{dataset}", "signal_analysis_v2"),
            ("from", "cte", "run_receipts"),
            ("inner_join", "cte", "quality_authority"),
            ("from", "cte", "predictions"),
            ("from", "cte", "evidence"),
            ("from", "cte", "analysis"),
            ("from", "cte", "evidence"),
            ("inner_join", "cte", "released_predictions"),
            ("inner_join", "cte", "released_runs"),
            ("from", "cte", "evidence"),
            ("from", "cte", "instrument_candidates"),
            ("from", "cte", "instrument_candidates"),
            ("inner_join", "cte", "instrument_stance"),
            ("from", "cte", "instrument_receipts"),
            ("from", "cte", "instrument_receipts"),
            ("from", "cte", "instrument_points"),
            ("from", "cte", "instrument_point_set"),
            ("from", "cte", "instrument_points"),
            ("inner_join", "cte", "instrument_family_points"),
            ("from", "cte", "instrument_strands"),
            ("inner_join", "cte", "released_runs"),
            ("from", "cte", "released_predictions"),
            ("inner_join", "cte", "released_runs"),
            ("left_join", "cte", "instrument_summary"),
            ("left_join", "cte", "instrument_withheld"),
            ("left_join", "cte", "instrument_series"),
            ("from", "cte", "released_runs"),
            ("inner_join", "cte", "candidates"),
            ("inner_join", "cte", "released_predictions"),
            ("inner_join", "cte", "memberships"),
            ("left_join", "cte", "available_receipts"),
            ("left_join", "cte", "same_run_analysis"),
            ("left_join", "cte", "instrument_signal"),
        ),
    },
    "v_signal_board_v2.sql": {
        "fields": _BOARD_FIELDS,
        "relations": (
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "signal_candidates_v2",
            ),
            ("from", "cte", "ranked_candidates"),
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "signal_analysis_v2",
            ),
            ("from", "cte", "selected_candidates"),
            ("left_join", "cte", "analysis_matches"),
        ),
    },
    "v_signal_evidence_v2.sql": {
        "fields": _EVIDENCE_FIELDS,
        "relations": (
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "signal_evidence_v2",
            ),
        ),
    },
    "v_source_lab_v2.sql": {
        "fields": _SOURCE_LAB_FIELDS,
        "relations": (
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "source_performance_daily_v2",
            ),
            ("from", "cte", "ranked_sources"),
        ),
    },
    "v_open_intelligence_health_v2.sql": {
        "fields": _HEALTH_FIELDS,
        "relations": (
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "signal_candidates_v2",
            ),
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "signal_evidence_v2",
            ),
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "signal_analysis_v2",
            ),
            (
                "from",
                "physical",
                "{project}",
                "{dataset}",
                "signal_predictions_v2",
            ),
            ("from", "cte", "health_rows"),
        ),
    },
}

_BANNED_DEPENDENCIES = (
    "trends_v2_staging_qa",
    "trend_scores",
    "trend_analysis",
    "daily_summary",
    "v_trend_briefs",
    "brand24",
    "receiptkey",
    "receipt_key",
    "query_group",
    "trend_score",
    "citation_label",
    "demographic",
    "gender",
)


def _strip_comments(sql: str) -> str:
    without_blocks = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return re.sub(r"-{2}[^\n]*", " ", without_blocks)


def _normalized(sql: str) -> str:
    return re.sub(r"\s+", " ", _strip_comments(sql)).strip().lower()


def _keyword_positions(text: str, keyword: str, start: int = 0) -> list[int]:
    positions: list[int] = []
    depth = 0
    quote: str | None = None
    index = start
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if quote == "'" and index + 1 < len(text) and text[index + 1] == "'":
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and text[index : index + len(keyword)].lower() == keyword:
            before = text[index - 1] if index else " "
            after_index = index + len(keyword)
            after = text[after_index] if after_index < len(text) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                positions.append(index)
        index += 1
    return positions


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                if quote == "'" and index + 1 < len(text) and text[index + 1] == "'":
                    index += 2
                    continue
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
        index += 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _cte_body(sql: str, cte_name: str) -> str:
    clean = _strip_comments(sql)
    start_match = re.search(rf"\b{re.escape(cte_name)}\s+as\s*\(", clean, re.I)
    assert start_match, f"missing CTE: {cte_name}"
    opening = clean.index("(", start_match.start())
    depth = 0
    quote: str | None = None
    index = opening
    while index < len(clean):
        char = clean[index]
        if quote:
            if char == quote:
                if quote == "'" and index + 1 < len(clean) and clean[index + 1] == "'":
                    index += 2
                    continue
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return clean[opening + 1 : index]
        index += 1
    raise AssertionError(f"unclosed CTE: {cte_name}")


def _split_union_all(text: str) -> tuple[str, ...]:
    positions = _keyword_positions(text, "union all")
    parts: list[str] = []
    start = 0
    for position in positions:
        parts.append(text[start:position].strip())
        start = position + len("union all")
    parts.append(text[start:].strip())
    return tuple(parts)


def _final_select_parts(sql: str) -> tuple[str, str]:
    clean = _strip_comments(sql)
    select_at = _keyword_positions(clean, "select")[-1]
    from_at = _keyword_positions(clean, "from", select_at)[0]
    return clean[select_at + len("select") : from_at], clean[from_at:]


def _output_name(expression: str) -> str:
    expression = expression.strip()
    alias = re.search(r"\bas\s+`?([a-z_][a-z0-9_]*)`?\s*$", expression, re.I)
    if alias:
        return alias.group(1).lower()
    return expression.replace("`", "").rsplit(".", 1)[-1].strip().lower()


def _parsed_outputs(sql: str) -> tuple[str, ...]:
    select_list, _ = _final_select_parts(sql)
    return tuple(_output_name(part) for part in _split_top_level(select_list))


def _output_expressions(sql: str) -> dict[str, str]:
    select_list, _ = _final_select_parts(sql)
    return {
        _output_name(part): re.sub(r"\s+", " ", part.strip()).lower()
        for part in _split_top_level(select_list)
    }


def _canonical_expression(expression: str) -> str:
    canonical = re.sub(r"\s+", " ", expression).strip().lower()
    canonical = re.sub(r"\(\s+", "(", canonical)
    canonical = re.sub(r"\s+\)", ")", canonical)
    return canonical


def _final_where_predicate(sql: str) -> str:
    _, tail = _final_select_parts(sql)
    where_positions = _keyword_positions(tail, "where")
    assert len(where_positions) == 1, "final query must have exactly one WHERE"
    start = where_positions[0] + len("where")
    end = len(tail)
    for keyword in ("group by", "order by", "qualify", "limit"):
        positions = _keyword_positions(tail, keyword, start)
        if positions:
            end = min(end, positions[0])
    return _canonical_expression(tail[start:end].rstrip(" ;\r\n"))


SqlToken = tuple[str, str]
Relation = tuple[str, ...]

_RELATION_BOUNDARIES = {
    "where",
    "group",
    "order",
    "qualify",
    "having",
    "window",
    "union",
    "limit",
}
_JOIN_PREFIXES = {"left", "right", "full", "inner", "cross"}


def _sql_tokens(sql: str) -> tuple[SqlToken, ...]:
    clean = _strip_comments(sql)
    tokens: list[SqlToken] = []
    index = 0
    while index < len(clean):
        char = clean[index]
        if char.isspace():
            index += 1
            continue
        if char == "`":
            end = clean.find("`", index + 1)
            assert end >= 0, "unclosed backticked identifier"
            tokens.append(("backtick", clean[index + 1 : end]))
            index = end + 1
            continue
        if char in "'\"":
            quote = char
            end = index + 1
            while end < len(clean):
                if clean[end] == quote:
                    if end + 1 < len(clean) and clean[end + 1] == quote:
                        end += 2
                        continue
                    break
                end += 1
            assert end < len(clean), "unclosed SQL string"
            tokens.append(("string", clean[index : end + 1]))
            index = end + 1
            continue
        word = re.match(r"[a-z_][a-z0-9_]*", clean[index:], re.I)
        if word:
            tokens.append(("word", word.group(0).lower()))
            index += len(word.group(0))
            continue
        number = re.match(r"\d+(?:\.\d+)?", clean[index:])
        if number:
            tokens.append(("number", number.group(0)))
            index += len(number.group(0))
            continue
        tokens.append(("symbol", char))
        index += 1
    return tuple(tokens)


def _matching_parenthesis(tokens: tuple[SqlToken, ...], opening: int) -> int:
    assert tokens[opening] == ("symbol", "(")
    depth = 0
    for index in range(opening, len(tokens)):
        if tokens[index] == ("symbol", "("):
            depth += 1
        elif tokens[index] == ("symbol", ")"):
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError("unclosed relation parenthesis")


def _parse_relation(tokens: tuple[SqlToken, ...], index: int) -> tuple[Relation, int]:
    assert index < len(tokens), "missing relation after FROM or JOIN"
    kind, value = tokens[index]
    if kind == "backtick":
        parts = tuple(part.lower() for part in value.split("."))
        assert 1 <= len(parts) <= 3, f"unrecognized relation identifier: {value}"
        if len(parts) == 1:
            return ("quoted_relation", parts[0]), index + 1
        return ("physical", *parts), index + 1
    if (kind, value) == ("symbol", "("):
        closing = _matching_parenthesis(tokens, index)
        inner = tokens[index + 1 : closing]
        assert inner, "empty parenthesized relation"
        assert inner[0] in {
            ("word", "select"),
            ("word", "with"),
        }, "unrecognized parenthesized relation"
        return ("derived",), closing + 1
    assert kind == "word", f"unrecognized relation token: {tokens[index]}"
    path = [value]
    cursor = index + 1
    while (
        cursor + 1 < len(tokens)
        and tokens[cursor] == ("symbol", ".")
        and tokens[cursor + 1][0] == "word"
    ):
        path.append(tokens[cursor + 1][1])
        cursor += 2
    if cursor < len(tokens) and tokens[cursor] == ("symbol", "("):
        closing = _matching_parenthesis(tokens, cursor)
        relation_kind = "unnest" if path == ["unnest"] else "table_function"
        return (relation_kind, ".".join(path)), closing + 1
    if len(path) == 1:
        return ("cte", path[0]), cursor
    assert len(path) <= 3, f"unrecognized unquoted relation: {'.'.join(path)}"
    return ("unquoted_physical", *path), cursor


def _skip_relation_alias(tokens: tuple[SqlToken, ...], index: int) -> int:
    if index < len(tokens) and tokens[index] == ("word", "as"):
        assert index + 1 < len(tokens)
        assert tokens[index + 1][0] == "word"
        return index + 2
    if index < len(tokens) and tokens[index][0] == "word":
        value = tokens[index][1]
        if value not in _RELATION_BOUNDARIES | _JOIN_PREFIXES | {"join"}:
            return index + 1
    return index


def _relation_topology(sql: str) -> tuple[Relation, ...]:
    tokens = _sql_tokens(sql)
    relations: list[Relation] = []
    for index, token in enumerate(tokens):
        if token == ("word", "from"):
            relation, cursor = _parse_relation(tokens, index + 1)
            relations.append(("from", *relation))
            cursor = _skip_relation_alias(tokens, cursor)
            while cursor < len(tokens) and tokens[cursor] == ("symbol", ","):
                relation, cursor = _parse_relation(tokens, cursor + 1)
                relations.append(("comma", *relation))
                cursor = _skip_relation_alias(tokens, cursor)
            if cursor < len(tokens):
                next_token = tokens[cursor]
                allowed_word = next_token[0] == "word" and next_token[1] in (
                    _RELATION_BOUNDARIES | _JOIN_PREFIXES | {"join"}
                )
                allowed_symbol = next_token in {
                    ("symbol", ")"),
                    ("symbol", ";"),
                }
                assert allowed_word or allowed_symbol, (
                    f"unrecognized FROM relation continuation: {next_token}"
                )
        elif token == ("word", "join"):
            join_kind = "inner"
            if index and tokens[index - 1][0] == "word":
                prefix = tokens[index - 1][1]
                if prefix in _JOIN_PREFIXES:
                    join_kind = prefix
            relation, _ = _parse_relation(tokens, index + 1)
            relations.append((f"{join_kind}_join", *relation))
    return tuple(relations)


def _source_filter_clauses(sql: str) -> tuple[tuple[str, str], ...]:
    normalized = _normalized(sql)
    references = tuple(
        re.finditer(
            r"\bfrom\s+`\{project\}\.\{dataset\}\.([a-z0-9_]+)`",
            normalized,
        )
    )
    clauses: list[tuple[str, str]] = []
    for reference in references:
        tail = normalized[reference.end() :]
        where = re.match(
            r"(?:\s+(?:as\s+)?[a-z_][a-z0-9_]*)?"
            r"(?:\s+(?:(?:inner|left)\s+)?join\s+.+?)?\s+where\s+(.+?)"
            r"(?=\s+group\s+by\b|\s+union\s+all\b|\s*\)|;)",
            tail,
        )
        assert where, f"missing source boundary QA filter: {reference.group(1)}"
        clauses.append((reference.group(1), where.group(1).strip()))
    return tuple(clauses)


def _window_contract(sql: str, alias: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized = _normalized(sql)
    match = re.search(
        rf"row_number\s*\(\s*\)\s*over\s*\(\s*partition by (.*?) "
        rf"order by (.*?)\)\s+as\s+{re.escape(alias)}\b",
        normalized,
    )
    assert match, f"missing {alias} ROW_NUMBER window"
    return (
        tuple(part.strip() for part in _split_top_level(match.group(1))),
        tuple(part.strip() for part in _split_top_level(match.group(2))),
    )


def _assert_common_contract(filename: str, sql: str) -> None:
    contract = _CONTRACTS[filename]
    normalized = _normalized(sql)
    expected_view = filename.removesuffix(".sql")
    target = re.search(
        r"create\s+or\s+replace\s+view\s+"
        r"`\{project\}\.\{dataset\}\.([a-z0-9_]+)`\s+as\b",
        normalized,
    )
    assert target
    assert target.group(1) == expected_view
    assert _parsed_outputs(sql) == contract["fields"]
    relations = _relation_topology(sql)
    assert relations == contract["relations"]
    for relation in relations:
        if relation[1] == "physical":
            assert len(relation) == 5
            assert relation[2] == "{project}"
            assert relation[3] == "{dataset}"
    assert not re.search(r"\bselect\s+(?:[a-z_][a-z0-9_]*\.)?\*", normalized)
    source_filters = _source_filter_clauses(sql)
    assert source_filters
    for table, clause in source_filters:
        if filename != "v_desk_dynamic_signals_v2.sql":
            assert clause == "client_scope_id != 'qa_canary'"
        elif table == "open_intelligence_quality_release_records_v2":
            assert clause == ("release_contract_version = 'open_intelligence_quality_release_v2'")
        elif table == "open_intelligence_run_receipts_v1" and "run_id in" in clause:
            assert clause == (
                "client_scope_id != 'qa_canary' and run_id in "
                "(select run_id from valid_provenance_runs"
            )
        elif ".run_id = q.run_id" in clause:
            assert re.search(r"\b[a-z_]+\.client_scope_id != 'qa_canary'", clause)
        elif re.fullmatch(r"[a-z_]+\.client_scope_id != 'qa_canary'", clause):
            # Hoisted digest CTEs keep the QA boundary under their base alias; the per-run
            # filter lives on each read of the CTE, not in its body.
            continue
        elif table == "signal_predictions_v2":
            assert clause == "client_scope_id != 'qa_canary' and not display_eligible"
        else:
            assert clause == "client_scope_id != 'qa_canary'"
    for banned in _BANNED_DEPENDENCIES:
        assert banned not in normalized


def _assert_board_contract(sql: str) -> None:
    normalized = _normalized(sql)
    partition, ordering = _window_contract(sql, "candidate_rank")
    assert partition == ("client_scope_id", "market", "signal_id")
    assert ordering == ("signal_date desc", "created_at desc", "run_id desc")
    assert re.search(r"\bcandidate_rank\s*=\s*1\b", normalized)
    join = re.search(
        r"left\s+join\s+analysis_matches\s+a\s+"
        r"on\s+(.+?)(?=\s+where\b|\s+group\s+by\b|\s+order\s+by\b|;|$)",
        normalized,
    )
    assert join, "missing same run analysis LEFT JOIN"
    terms = {part.strip() for part in re.split(r"\s+and\s+", join.group(1))}
    assert terms == {
        "a.client_scope_id = c.client_scope_id",
        "a.signal_date = c.signal_date",
        "a.market = c.market",
        "a.signal_id = c.signal_id",
        "a.run_id = c.run_id",
    }
    assert re.search(
        r"count\s*\(\s*\*\s*\)\s+as\s+analysis_match_count",
        normalized,
    )
    assert re.search(
        r"group by client_scope_id, signal_date, market, signal_id, run_id",
        normalized,
    )
    assert _final_where_predicate(sql) == (
        "if(coalesce(a.analysis_match_count, 0) <= 1, true, "
        "error('duplicate same-run signal analysis rows'))"
    )
    assert normalized.count("row_number") == 1
    expressions = _output_expressions(sql)
    for field in (
        "summary",
        "why_now",
        "possible_response",
        "limitations",
        "contradictions",
        "evidence_ids",
        "human_review_required",
    ):
        assert expressions[field] == f"a.{field}"
    assert expressions["evidence_state"] == "c.evidence_state"
    assert expressions["created_at"] == ("coalesce(a.analyzed_at, c.created_at) as created_at")


def _assert_evidence_contract(sql: str) -> None:
    normalized = _normalized(sql)
    assert "row_number" not in normalized
    assert "qualify" not in normalized
    assert "group by" not in normalized
    assert " join " not in normalized


def _assert_source_lab_contract(sql: str) -> None:
    normalized = _normalized(sql)
    partition, ordering = _window_contract(sql, "source_rank")
    assert partition == (
        "client_scope_id",
        "vendor",
        "http_method",
        "route_path",
        "coalesce(market, 'global')",
    )
    assert ordering == ("metric_date desc", "last_checked_at desc", "run_id desc")
    assert re.search(r"\bsource_rank\s*=\s*1\b", normalized)
    assert normalized.count("row_number") == 1


def _assert_health_contract(sql: str) -> None:
    normalized = _normalized(sql)
    assert "row_number" not in normalized
    assert "qualify" not in normalized
    assert normalized.count("union all") == 3
    branches = _split_union_all(_cte_body(sql, "health_rows"))
    observed_branches: list[tuple[str, str, str, str]] = []
    for branch in branches:
        references = re.findall(r"`\{project\}\.\{dataset\}\.([a-z0-9_]+)`", branch)
        assert len(references) == 1
        expressions = _output_expressions(branch)
        observed_branches.append(
            (
                references[0],
                expressions["row_kind"],
                expressions["evidence_state"],
                expressions["activity_at"],
            )
        )
    assert tuple(observed_branches) == (
        (
            "signal_candidates_v2",
            "'candidate' as row_kind",
            "evidence_state",
            "created_at as activity_at",
        ),
        (
            "signal_evidence_v2",
            "'evidence' as row_kind",
            "null as evidence_state",
            "created_at as activity_at",
        ),
        (
            "signal_analysis_v2",
            "'analysis' as row_kind",
            "null as evidence_state",
            "analyzed_at as activity_at",
        ),
        (
            "signal_predictions_v2",
            "'prediction' as row_kind",
            "null as evidence_state",
            "predicted_at as activity_at",
        ),
    )
    _, tail = _final_select_parts(sql)
    group_at = _keyword_positions(tail, "group by")[0]
    group_text = tail[group_at + len("group by") :].rstrip(" ;\r\n")
    grain = tuple(part.strip().lower() for part in _split_top_level(group_text))
    assert grain == (
        "contract_version",
        "run_id",
        "client_scope_id",
        "signal_date",
        "market",
    )
    expressions = _output_expressions(sql)
    expected_counts = {
        "candidate_count": "countif(row_kind = 'candidate') as candidate_count",
        "ready_count": (
            "countif(row_kind = 'candidate' and evidence_state = 'ready') as ready_count"
        ),
        "thin_count": ("countif(row_kind = 'candidate' and evidence_state = 'thin') as thin_count"),
        "contradictory_count": (
            "countif(row_kind = 'candidate' and evidence_state = 'contradictory') "
            "as contradictory_count"
        ),
        "unchecked_count": (
            "countif(row_kind = 'candidate' and evidence_state = 'unchecked') as unchecked_count"
        ),
        "evidence_count": "countif(row_kind = 'evidence') as evidence_count",
        "analysis_count": "countif(row_kind = 'analysis') as analysis_count",
        "prediction_count": "countif(row_kind = 'prediction') as prediction_count",
        "latest_created_at": "max(activity_at) as latest_created_at",
    }
    for field, expression in expected_counts.items():
        assert expressions[field] == expression


def _assert_desk_dynamic_contract(sql: str, *, allow_qualities: bool = False) -> None:
    """Every gate that stops an unreleased or unprovable run reaching a desk."""
    normalized = _normalized(sql)
    # Release gating. A run that failed, wrote partial partitions, was never
    # released, or whose window has not closed can never surface a signal.
    for required in (
        "run_contract_version = 'open_intelligence_run_receipt_v1'",
        "status = 'completed'",
        "complete_partitions",
        "display_release_state = 'enabled'",
        "observation_start <= observation_end",
        "signal_date = observation_end",
        "observation_end < current_date('africa/johannesburg')",
    ):
        assert required in normalized, f"missing run release gate: {required}"
    # Exactly one released prediction, and only evidence the engine still calls
    # usable, may reach the caller.
    if allow_qualities:
        assert "and not display_eligible" in normalized
        assert "open_intelligence_quality_release_v2" in normalized
        assert "m.member_identity = c.label_member_identity" in normalized
        assert "m.canonical_value = c.label" in normalized
        assert "member_types" not in normalized
        assert "min(candidate_type)" not in normalized
    else:
        assert "where display_eligible" in normalized
    assert "availability = 'available'" in normalized
    # Discovery mode is engine owned. One membership type, or the row is out.
    if not allow_qualities:
        assert "count(distinct candidate_type) as distinct_candidate_types" in normalized
        assert "m.distinct_candidate_types = 1" in normalized
        assert "m.canonical_value = c.label" in normalized
    # The exact approved mapping, and nothing beyond it. A mode the engine
    # cannot yet prove per signal must not appear.
    for source, caller in (
        ("keyword", "phrase"),
        ("slang", "phrase"),
        ("headline", "phrase"),
        ("hashtag", "hashtag"),
        ("sound", "sound"),
        ("creator", "creator"),
        ("entity", "entity"),
    ):
        assert f"when '{source}' then '{caller}'" in normalized
    for forbidden in ("'event'", "'cooccurrence'", "'embedding'", "'cross_platform'"):
        assert forbidden not in normalized, f"unprovable discovery mode present: {forbidden}"
    # Cross-run analysis may never decorate a signal.
    assert "if(a.analysis_row_count = 1, a.why_now, null)" in normalized
    # The signal's market must be inside the run's own resolved scope.
    assert "c.market in unnest(r.market_scope)" in normalized
    # Engine order only. No score, reach or popularity sort.
    banned_fields = () if allow_qualities else ("novelty_score", "velocity_score", "breadth_score")
    for banned in ("order by c.", *banned_fields):
        assert banned not in normalized, f"caller-side ranking present: {banned}"


def _assert_desk_dynamic_v2_contract(sql: str) -> None:
    _assert_desk_dynamic_contract(sql, allow_qualities=True)
    normalized = _normalized(sql)
    assert "'desk_dynamic_signal_v2' as contract_version" in normalized
    assert "dynamic_quality_projection_v2" in normalized
    assert "dynamic_quality_projection_v1" not in normalized
    projection_gate = normalized.split("and q.candidate_projection_digest =", 1)[1].split(
        "and r.row_set_digest =", 1
    )[0]
    assert "signal_predictions_v2" not in projection_gate
    assert "r.row_set_digest =" in normalized
    for table in (
        "signal_candidates_v2",
        "signal_evidence_v2",
        "signal_membership_v2",
        "signal_lineage_v2",
        "signal_predictions_v2",
        "signal_outcomes_v2",
        "signal_analysis_v2",
    ):
        assert table in normalized
    for field in (
        "velocity_score",
        "novelty_score",
        "breadth_score",
        "independence_score",
        "historical_similarity",
        "geo_confidence",
        "topic_tags",
    ):
        assert f"c.{field} as {field}" in normalized
    for field in _DESK_INSTRUMENT_FIELDS:
        assert f"s.{field} as {field}" in normalized
    for literal in (
        "'1.0.0' as contractversion",
        "'channel_family_v2' as groupingauthority",
        "'channel_family_v2' as independenceauthority",
        "'cumulative_receipt_share_0_1' as normalization",
        "struct(0.0 as min, 1.0 as max) as valuedomain",
        "true as closed",
        "when e.direction = 'not_applicable' then 'neutral'",
        "and e.published_at is not null",
        "and trim(e.excerpt) != ''",
        "when v.mixed_family then []",
        "the ribbon is empty",
    ):
        assert literal in normalized, f"instrument contract literal missing: {literal}"
    assert "left join instrument_signal s" in normalized


def _assert_view_contract(filename: str, sql: str) -> None:
    _assert_common_contract(filename, sql)
    validators = {
        "v_desk_dynamic_signals_v1.sql": _assert_desk_dynamic_contract,
        "v_desk_dynamic_signals_v2.sql": _assert_desk_dynamic_v2_contract,
        "v_signal_board_v2.sql": _assert_board_contract,
        "v_signal_evidence_v2.sql": _assert_evidence_contract,
        "v_source_lab_v2.sql": _assert_source_lab_contract,
        "v_open_intelligence_health_v2.sql": _assert_health_contract,
    }
    validators[filename](sql)


def _view_sql(filename: str) -> str:
    path = _VIEWS_DIR / filename
    assert path.is_file(), f"missing accepted view: {filename}"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("filename", tuple(_CONTRACTS))
def test_open_intelligence_view_matches_accepted_contract(filename: str):
    _assert_view_contract(filename, _view_sql(filename))


def test_desk_canonical_membership_exposure_requires_whole_run_authority():
    from src.analysis.open_intelligence.persistence import (
        TARGET_PROJECT,
        _completed_provenance_runs_sql,
        _membership_json_sql,
    )

    sql = _view_sql("v_desk_dynamic_signals_v2.sql")
    authority = (
        _completed_provenance_runs_sql(project=TARGET_PROJECT, dataset="trends_v2_staging")
        .replace(TARGET_PROJECT, "{project}")
        .replace("trends_v2_staging", "{dataset}")
    )
    assert f"WITH valid_provenance_runs AS (\n{authority}\n)" in sql
    for alias in ("membership", "row_set_membership_row"):
        expression = _membership_json_sql(
            alias, project="{project}", dataset="{dataset}", from_receipt=True
        )
        assert expression in sql
        assert "ERROR(" not in expression
        assert (
            f"JOIN valid_provenance_runs AS {alias}_authority ON "
            f"{alias}_authority.run_id = {alias}.run_id"
        ) in sql
    assert "AND run_id IN (SELECT run_id FROM valid_provenance_runs)" in sql


def test_v2_contract_rejects_each_removed_quality_field():
    filename = "v_desk_dynamic_signals_v2.sql"
    sql = _view_sql(filename)
    for field in _DESK_DYNAMIC_V2_FIELDS[len(_DESK_DYNAMIC_FIELDS) :]:
        alias = "s" if field in _DESK_INSTRUMENT_FIELDS else "c"
        mutated = sql.replace(f"  {alias}.{field} AS {field},\n", "", 1)
        if mutated == sql:
            mutated = sql.replace(f"  {alias}.{field} AS {field}\n", "", 1)
        assert mutated != sql
        with pytest.raises(AssertionError):
            _assert_view_contract(filename, mutated)


def test_contract_rejects_cross_run_board_join():
    filename = "v_signal_board_v2.sql"
    sql = _view_sql(filename)
    original = "a.run_id = c.run_id"
    assert original in sql
    mutated = sql.replace(original, "a.analyzed_at = c.created_at", 1)
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_missing_qa_guard():
    filename = "v_signal_evidence_v2.sql"
    sql = _view_sql(filename)
    original = "WHERE client_scope_id != 'qa_canary'"
    assert original in sql
    mutated = sql.replace(original, "", 1)
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_select_star():
    filename = "v_signal_evidence_v2.sql"
    sql = _view_sql(filename)
    original = "SELECT\n  contract_version,"
    assert original in sql
    mutated = sql.replace(original, "SELECT\n  *,", 1)
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_citation_label_leakage():
    filename = "v_signal_evidence_v2.sql"
    sql = _view_sql(filename)
    original = "  evidence_state,\n  availability,"
    assert original in sql
    mutated = sql.replace(
        original,
        "  evidence_state,\n  citation_label,\n  availability,",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_wrong_source_lab_ordering():
    filename = "v_source_lab_v2.sql"
    sql = _view_sql(filename)
    original = "ORDER BY metric_date DESC, last_checked_at DESC, run_id DESC"
    assert original in sql
    mutated = sql.replace(
        original,
        "ORDER BY last_checked_at DESC, metric_date DESC, run_id DESC",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_health_latest_collapse():
    filename = "v_open_intelligence_health_v2.sql"
    sql = _view_sql(filename)
    original = "  market;"
    assert original in sql
    mutated = sql.replace(
        original,
        "  market\nQUALIFY ROW_NUMBER() OVER (\n"
        "  PARTITION BY client_scope_id, market\n"
        "  ORDER BY latest_created_at DESC\n"
        ") = 1;",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_board_fails_closed_on_duplicate_same_run_analysis():
    normalized = _normalized(_view_sql("v_signal_board_v2.sql"))
    assert re.search(
        r"count\s*\(\s*\*\s*\)\s+as\s+analysis_match_count",
        normalized,
    )
    assert _final_where_predicate(_view_sql("v_signal_board_v2.sql")) == (
        "if(coalesce(a.analysis_match_count, 0) <= 1, true, "
        "error('duplicate same-run signal analysis rows'))"
    )


def test_board_retains_one_candidate_when_same_run_analysis_is_absent():
    normalized = _normalized(_view_sql("v_signal_board_v2.sql"))
    assert "from selected_candidates c left join analysis_matches a" in normalized
    assert _final_where_predicate(_view_sql("v_signal_board_v2.sql")) == (
        "if(coalesce(a.analysis_match_count, 0) <= 1, true, "
        "error('duplicate same-run signal analysis rows'))"
    )


def test_contract_rejects_board_without_duplicate_analysis_error():
    filename = "v_signal_board_v2.sql"
    sql = _view_sql(filename)
    original = "ERROR('Duplicate same-run signal analysis rows')"
    assert original in sql
    mutated = sql.replace(original, "TRUE", 1)
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_tautological_qa_guard():
    filename = "v_signal_evidence_v2.sql"
    sql = _view_sql(filename)
    original = "WHERE client_scope_id != 'qa_canary'"
    assert original in sql
    mutated = sql.replace(original, f"{original} OR TRUE", 1)
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_wrong_scope_qa_guard():
    filename = "v_signal_board_v2.sql"
    sql = _view_sql(filename)
    original = "    created_at,\n    ROW_NUMBER()"
    assert original in sql
    mutated = sql.replace(
        original,
        "    created_at,\n    client_scope_id != 'qa_canary' AS unused_qa_guard,\n    ROW_NUMBER()",
        1,
    ).replace(
        "WHERE client_scope_id != 'qa_canary'",
        "WHERE theme_id != 'qa_canary'",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_irrelevant_qa_guard_occurrence():
    filename = "v_signal_evidence_v2.sql"
    sql = _view_sql(filename)
    original = "WHERE client_scope_id != 'qa_canary'"
    assert original in sql
    mutated = sql.replace(
        original,
        "WHERE TRUE AND \"client_scope_id != 'qa_canary'\" IS NOT NULL",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_outside_production_table_reference():
    filename = "v_signal_board_v2.sql"
    sql = _view_sql(filename)
    original = "    created_at,\n    ROW_NUMBER()"
    assert original in sql
    mutated = sql.replace(
        original,
        "    created_at,\n"
        "    (SELECT COUNT(*) FROM `outside.production.unapproved_table`) "
        "AS unused_external,\n"
        "    ROW_NUMBER()",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_desk_v2_empty_family_hashes_as_json_null_like_the_release_builders():
    # r16, 4 Sep 2026: the release builders wrap TO_JSON_STRING(ARRAY_AGG(...)) in
    # IFNULL, but BigQuery renders an empty aggregate as the string null before that
    # IFNULL can fire, so every released receipt hashes an empty family as null. A
    # view that substitutes [] never matches a run without lineage or outcomes rows.
    sql = (_VIEWS_DIR / "v_desk_dynamic_signals_v2.sql").read_text(encoding="utf-8")
    fallbacks = re.findall(r"IFNULL\((?:row_set|projection)_[a-z]+_agg\.payload, '([^']*)'\)", sql)
    assert len(fallbacks) == 10
    assert set(fallbacks) == {"null"}


def test_contract_rejects_row_multiplying_cross_join_unnest():
    filename = "v_signal_board_v2.sql"
    sql = _view_sql(filename)
    original = "  FROM ranked_candidates\n  WHERE candidate_rank = 1"
    assert original in sql
    mutated = sql.replace(
        original,
        "  FROM ranked_candidates\n"
        "  CROSS JOIN UNNEST([1, 2]) AS multiplier\n"
        "  WHERE candidate_rank = 1",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_wrong_health_row_kind():
    filename = "v_open_intelligence_health_v2.sql"
    sql = _view_sql(filename)
    original = "    'candidate' AS row_kind,"
    assert original in sql
    mutated = sql.replace(original, "    'evidence' AS row_kind,", 1)
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_swapped_health_source_branches():
    filename = "v_open_intelligence_health_v2.sql"
    sql = _view_sql(filename)
    mutated = (
        sql.replace("signal_candidates_v2", "source_swap_sentinel", 1)
        .replace("signal_evidence_v2", "signal_candidates_v2", 1)
        .replace("source_swap_sentinel", "signal_evidence_v2", 1)
    )
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_wrong_health_evidence_state_expression():
    filename = "v_open_intelligence_health_v2.sql"
    sql = _view_sql(filename)
    original = "    evidence_state,\n    created_at AS activity_at"
    assert original in sql
    mutated = sql.replace(
        original,
        "    NULL AS evidence_state,\n    created_at AS activity_at",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_fixed_health_activity_timestamp():
    filename = "v_open_intelligence_health_v2.sql"
    sql = _view_sql(filename)
    original = "    analyzed_at AS activity_at"
    assert original in sql
    mutated = sql.replace(
        original,
        "    TIMESTAMP('2000-01-01') AS activity_at",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_two_part_physical_table_reference():
    filename = "v_signal_board_v2.sql"
    sql = _view_sql(filename)
    original = "    created_at,\n    ROW_NUMBER()"
    assert original in sql
    mutated = sql.replace(
        original,
        "    created_at,\n"
        "    (SELECT COUNT(*) FROM `production.unapproved_table`) "
        "AS unused_external,\n"
        "    ROW_NUMBER()",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_comma_joined_derived_relation():
    filename = "v_signal_board_v2.sql"
    sql = _view_sql(filename)
    original = "  FROM ranked_candidates\n  WHERE candidate_rank = 1"
    assert original in sql
    mutated = sql.replace(
        original,
        "  FROM ranked_candidates,\n"
        "  (SELECT 1 AS multiplier UNION ALL SELECT 2 AS multiplier) multipliers\n"
        "  WHERE candidate_rank = 1",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


def test_contract_rejects_null_rejecting_board_predicate():
    filename = "v_signal_board_v2.sql"
    sql = _view_sql(filename)
    original = "  ERROR('Duplicate same-run signal analysis rows')\n);"
    assert original in sql
    mutated = sql.replace(
        original,
        "  ERROR('Duplicate same-run signal analysis rows')\n)\n"
        "AND a.analysis_match_count IS NOT NULL;",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_view_contract(filename, mutated)


@pytest.mark.parametrize("filename", tuple(_CONTRACTS))
def test_views_never_open_a_cte_inside_a_correlated_subquery(filename: str):
    # BigQuery cannot resolve an outer alias inside the WITH clause of a subquery; the v2 desk
    # view shipped that way and failed to compile on its first live CREATE OR REPLACE.
    assert "= (WITH " not in _view_sql(filename)
