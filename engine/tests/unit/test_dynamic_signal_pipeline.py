"""Bounded, zero-write dynamic signal pipeline tests."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.cloud import bigquery
from src.analysis.open_intelligence.graph import GraphRules, SignalComponent
from src.analysis.open_intelligence.pipeline import (
    ACCEPTED_SEED_CANDIDATE_STATUSES,
    EVENT_COLUMNS,
    SEED_CANDIDATE_COLUMNS,
    SEED_GRAPH_COLUMNS,
    SOURCE_CEILINGS,
    CompositionDependencyMissing,
    EvidenceProjection,
    EvidenceProjectionLimitExceeded,
    SourceRowCeilingExceeded,
    SourceWindow,
    run_dynamic_signal_identity,
)
from src.contracts.open_intelligence import CONTRACT_VERSION, ResolvedScope

SIGNAL_DATE = date(2026, 8, 26)
PROJECT = "ogilvy-trends-v2"
DATASET = "trends_v2_staging"
COMMON_EVIDENCE_COLUMNS = (
    "id",
    "source",
    "platform",
    "market",
    "content_type",
    "query_group",
    "query_term",
    "author_name",
    "author_handle",
    "title",
    "text",
    "url",
    "published_at",
    "collected_at",
    "hashtags",
    "views",
    "likes",
    "comments",
    "shares",
    "engagement_total",
    "pipeline_run_id",
    "v2tone",
    "v2persons",
    "v2orgs",
    "v2locations",
    "v2gcam",
)
ENRICHED_FACTUAL_COLUMNS = (
    *COMMON_EVIDENCE_COLUMNS,
    "author_handle_norm",
    "regional_score",
    "slang_terms",
    "search_velocity_score",
    "tone_avg",
    "tone_polarity",
    "topic_groups",
    "classification_layer",
    "sentiment_lexicon_score",
)
RAW_FACTUAL_COLUMNS = COMMON_EVIDENCE_COLUMNS


def scope() -> ResolvedScope:
    return ResolvedScope(
        client_scope_id="ogilvy_internal",
        market_scope=("za", "ng", "ke"),
        brand_config_id="ogilvy",
        audience_lens_ids=(),
        theme_id="open_intelligence",
        run_id="dynamic_test_run",
        contract_version=CONTRACT_VERSION,
    )


def event_row(term: str = "alpha", market: str = "za") -> dict[str, object]:
    return {
        "ledger_id": f"ledger_{market}_{term}",
        "trend_date": SIGNAL_DATE,
        "market": market,
        "entity_key": term,
        "entity_aliases": [f"{term} alias"],
        "event_kind": "search_term",
        "state_label": "emerging",
        "as_of": datetime(2026, 8, 26, tzinfo=UTC),
        "corroborating_sources": ["rss", "gdelt"],
        "source_count": 2,
        "confidence": 0.8,
    }


def graph_row(term: str = "beta", market: str = "za") -> dict[str, object]:
    return {
        "market": market,
        "term": term,
        "term_type": "token",
        "platform": "youtube",
        "trend_date": SIGNAL_DATE,
        "event_date": SIGNAL_DATE,
        "row_count": 4,
        "topic_groups": ["music"],
        "near_topics": [],
        "co_occur_terms": ["alpha"],
        "sample_row_ids": [f"row_{market}_{term}"],
    }


def candidate_row(term: str = "gamma", status: str = "approved") -> dict[str, object]:
    return {
        "candidate_id": f"candidate_{term}",
        "proposed_date": SIGNAL_DATE,
        "market": "za",
        "candidate_type": "keyword",
        "candidate_value": term,
        "source": "seed_graph",
        "lane": "weekly",
        "score": 0.7,
        "evidence_topics": ["music"],
        "sample_row_ids": [f"row_za_{term}"],
        "status": status,
    }


def evidence_row(
    row_id: str,
    *,
    market: str = "za",
    source_table: str = "enriched_content",
    match_count: int = 1,
) -> dict[str, object]:
    row = {
        "id": row_id,
        "source": "rss",
        "platform": "news",
        "market": market,
        "content_type": "article",
        "query_group": "music",
        "query_term": "amapiano",
        "author_name": "Reporter",
        "author_handle": None,
        "title": f"Title {row_id}",
        "text": f"Text {row_id}",
        "url": f"https://example.com/{row_id}",
        "published_at": datetime(2026, 8, 26, tzinfo=UTC),
        "collected_at": datetime(2026, 8, 26, tzinfo=UTC),
        "hashtags": "#amapiano",
        "views": 10.0,
        "likes": 2.0,
        "comments": 1.0,
        "shares": 1.0,
        "engagement_total": 14.0,
        "pipeline_run_id": "run_source",
        "v2tone": "1,2,3",
        "v2persons": "Artist",
        "v2orgs": "Label",
        "v2locations": "Johannesburg",
        "v2gcam": "c1",
        "match_count": match_count,
        "source_table": source_table,
    }
    if source_table == "enriched_content":
        row.update(
            {
                "author_handle_norm": None,
                "regional_score": 0.75,
                "slang_terms": "piano",
                "search_velocity_score": 0.55,
                "tone_avg": 1.5,
                "tone_polarity": 3.0,
                "topic_groups": ["music_amapiano"],
                "classification_layer": "regex",
                "sentiment_lexicon_score": 0.25,
            }
        )
    return row


def component(*terms: str) -> SignalComponent:
    identities = tuple(f"za|keyword|{term}" for term in terms)
    return SignalComponent(
        market="za",
        member_identities=identities,
        terms=tuple(terms),
        row_receipts=(),
        source_families=(),
        platforms=(),
        creator_ids=(),
        label=terms[0],
        label_member_identity=identities[0],
    )


class FakeJob:
    def __init__(self, rows):
        self.rows = rows

    def result(self, **kwargs):
        return iter(self.rows)


class FakeClient:
    def __init__(
        self,
        *,
        project: str = PROJECT,
        event_rows=(),
        graph_rows=(),
        candidate_rows=(),
        enriched_rows=(),
        raw_rows=(),
    ):
        self.project = project
        self.rows = {
            "event_ledger": tuple(event_rows),
            "seed_graph": tuple(graph_rows),
            "seed_candidates": tuple(candidate_rows),
            "enriched_content": tuple(enriched_rows),
            "raw_content": tuple(raw_rows),
        }
        self.calls = []

    def query(self, sql, *, job_config, location):
        table = next(name for name in self.rows if f".{name}`" in sql)
        self.calls.append((table, sql, job_config, location))
        rows = self.rows[table]
        if table in {"enriched_content", "raw_content"}:
            values = parameter_values(job_config)
            keys = set(zip(values["sample_markets"], values["sample_ids"], strict=True))
            rows = tuple(row for row in rows if (row["market"], row["id"]) in keys)
        return FakeJob(rows)


def parameter_values(config: bigquery.QueryJobConfig) -> dict[str, object]:
    return {
        parameter.name: parameter.values if hasattr(parameter, "values") else parameter.value
        for parameter in config.query_parameters
    }


@pytest.mark.parametrize(
    "window",
    [
        lambda: SourceWindow(
            datetime(2026, 8, 20, tzinfo=UTC),
            SIGNAL_DATE,
            ("za",),
            ("ogilvy-trends-v2.trends_v2_staging.enriched_content",),
            False,
        ),
        lambda: SourceWindow(
            SIGNAL_DATE,
            date(2026, 8, 20),
            ("za",),
            ("ogilvy-trends-v2.trends_v2_staging.enriched_content",),
            False,
        ),
        lambda: SourceWindow(
            SIGNAL_DATE,
            SIGNAL_DATE,
            (),
            ("ogilvy-trends-v2.trends_v2_staging.enriched_content",),
            False,
        ),
        lambda: SourceWindow(
            SIGNAL_DATE,
            SIGNAL_DATE,
            ("ZA",),
            ("ogilvy-trends-v2.trends_v2_staging.enriched_content",),
            False,
        ),
        lambda: SourceWindow(
            SIGNAL_DATE,
            SIGNAL_DATE,
            ("za", "za"),
            ("ogilvy-trends-v2.trends_v2_staging.enriched_content",),
            False,
        ),
        lambda: SourceWindow(
            SIGNAL_DATE,
            SIGNAL_DATE,
            ("za", "ng"),
            ("ogilvy-trends-v2.trends_v2_staging.enriched_content",),
            False,
        ),
        lambda: SourceWindow(SIGNAL_DATE, SIGNAL_DATE, ("za",), (), False),
        lambda: SourceWindow(
            SIGNAL_DATE,
            SIGNAL_DATE,
            ("za",),
            ("trends_v2_staging.enriched_content",),
            False,
        ),
        lambda: SourceWindow(
            SIGNAL_DATE,
            SIGNAL_DATE,
            ("za",),
            ("ogilvy-trends-v2.trends_v2.enriched_content",),
            False,
        ),
        lambda: SourceWindow(
            SIGNAL_DATE,
            SIGNAL_DATE,
            ("za",),
            ("ogilvy-trends-v2.trends_v2_staging.enriched_content",),
            1,
        ),
    ],
)
def test_source_window_rejects_invalid_contract_values(window):
    with pytest.raises(ValueError):
        window()


def test_source_window_accepts_exact_inclusive_incomplete_staging_window():
    window = SourceWindow(
        date(2026, 8, 20),
        SIGNAL_DATE,
        ("ng", "za"),
        (
            "ogilvy-trends-v2.trends_v2_staging.enriched_content",
            "ogilvy-trends-v2.trends_v2_staging.raw_content",
        ),
        False,
    )

    assert window.start_date == date(2026, 8, 20)
    assert window.end_date == SIGNAL_DATE
    assert window.complete_partitions is False


def test_source_window_requires_explicit_completeness():
    with pytest.raises(TypeError):
        SourceWindow(
            SIGNAL_DATE,
            SIGNAL_DATE,
            ("za",),
            ("ogilvy-trends-v2.trends_v2_staging.enriched_content",),
        )


def identity_bytes(result) -> bytes:
    payload = [
        {
            "candidate_score": item.candidate_score,
            "candidate_type": item.candidate_type,
            "market": item.market,
            "observed_dates": [str(value) for value in item.observed_dates],
            "platforms": list(item.platforms),
            "row_ids": list(item.row_ids),
            "source_families": list(item.source_families),
            "term": item.term,
            "topic_tags": list(item.topic_tags),
        }
        for item in result.observations
    ]
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def encode_part(value: str | None) -> str:
    if value is None:
        return "N:"
    return f"V{len(value.encode('utf-8'))}:{value}"


def nested(values: tuple[str, ...]) -> str:
    return "".join(encode_part(value) for value in sorted(values))


def expected_member_id(
    *,
    member_identity: str,
    row_id: str,
    source_families: tuple[str, ...],
    platforms: tuple[str, ...],
) -> str:
    market, candidate_type, canonical_value = member_identity.split("|", 2)
    canonical = "".join(
        encode_part(value)
        for value in (
            "ogilvy_internal",
            SIGNAL_DATE.isoformat(),
            market,
            candidate_type,
            canonical_value,
            nested(source_families),
            nested(platforms),
            row_id,
        )
    )
    return "mem_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def projection_bytes(projection: EvidenceProjection) -> bytes:
    payload = {
        "candidate_inputs": {
            "event_ledger": [item.term for item in projection.candidate_inputs.event_ledger],
            "seed_graph": [item.term for item in projection.candidate_inputs.seed_graph],
            "seed_candidates": [item.term for item in projection.candidate_inputs.seed_candidates],
        },
        "memberships_by_component": {
            key: [
                {
                    "candidate_type": item.candidate_type,
                    "canonical_value": item.canonical_value,
                    "member_id": item.member_id,
                    "member_identity": item.member_identity,
                    "platforms": list(item.platforms),
                    "qualifies_evidence": item.qualifies_evidence,
                    "row_id": item.row_id,
                    "source_families": list(item.source_families),
                }
                for item in values
            ]
            for key, values in projection.memberships_by_component.items()
        },
        "receipts_by_member": {
            key: [item.row_id for item in values]
            for key, values in projection.receipts_by_member.items()
        },
        "candidate_only_receipts": {
            key: list(values) for key, values in projection.candidate_only_receipts.items()
        },
        "unresolved_sample_ids": list(projection.unresolved_sample_ids),
        "missing_work": list(projection.missing_work),
        "source_window": {
            "start_date": projection.source_window.start_date.isoformat(),
            "end_date": projection.source_window.end_date.isoformat(),
            "markets": list(projection.source_window.markets),
            "source_tables": list(projection.source_window.source_tables),
            "complete_partitions": projection.source_window.complete_partitions,
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_source_ceiling_stops_before_composition(monkeypatch):
    client = FakeClient(event_rows=(event_row(),) * (SOURCE_CEILINGS["event_ledger"] + 1))
    compose = Mock()
    monkeypatch.setattr("src.analysis.open_intelligence.pipeline.compose_components", compose)

    with pytest.raises(SourceRowCeilingExceeded, match=r"event_ledger.*25000"):
        run_dynamic_signal_identity(
            SIGNAL_DATE,
            scope(),
            client,
            DATASET,
            False,
        )

    compose.assert_not_called()
    assert tuple(call[0] for call in client.calls) == ("event_ledger",)


@pytest.mark.parametrize(
    ("client", "dataset"),
    [
        (FakeClient(project="other"), DATASET),
        (FakeClient(), "trends_v2"),
        (FakeClient(), "trends_v2_staging_qa"),
    ],
)
def test_target_refusal_happens_before_query(client, dataset):
    with pytest.raises(ValueError, match="exact staging target"):
        run_dynamic_signal_identity(SIGNAL_DATE, scope(), client, dataset, False)
    assert client.calls == []


def test_persist_refusal_happens_before_query():
    client = FakeClient()
    with pytest.raises(CompositionDependencyMissing, match="persistence"):
        run_dynamic_signal_identity(SIGNAL_DATE, scope(), client, DATASET, True)
    assert client.calls == []


def test_queries_are_exact_bounded_and_parameterized():
    client = FakeClient(
        event_rows=[event_row()],
        graph_rows=[graph_row()],
        candidate_rows=[candidate_row()],
    )

    result = run_dynamic_signal_identity(SIGNAL_DATE, scope(), client, DATASET, False)

    assert tuple(call[0] for call in client.calls) == (
        "event_ledger",
        "seed_graph",
        "seed_candidates",
        "enriched_content",
        "raw_content",
    )
    for table, sql, config, location in client.calls[:3]:
        assert "SELECT *" not in sql.upper()
        expected_columns = {
            "event_ledger": EVENT_COLUMNS,
            "seed_graph": SEED_GRAPH_COLUMNS,
            "seed_candidates": SEED_CANDIDATE_COLUMNS,
        }[table]
        assert sql.startswith(f"SELECT {', '.join(expected_columns)}\n")
        assert f"LIMIT {SOURCE_CEILINGS[table] + 1}" in sql
        assert "ORDER BY" in sql
        assert location == "US"
        values = parameter_values(config)
        assert values["start_date"] == SIGNAL_DATE
        assert values["end_date"] == SIGNAL_DATE
        assert values["markets"] == ["ke", "ng", "za"]
    candidate_params = parameter_values(client.calls[2][2])
    assert candidate_params["statuses"] == ["applied", "approved"]
    assert frozenset({"approved", "applied"}) == ACCEPTED_SEED_CANDIDATE_STATUSES
    assert result.input_counts == {
        "event_ledger": 1,
        "seed_graph": 1,
        "seed_candidates": 1,
    }
    assert result.observation_count == 3


def test_nonsemantic_seed_graph_noise_is_counted_but_not_admitted() -> None:
    noise = graph_row("________")
    signal = graph_row("valid signal")
    client = FakeClient(graph_rows=[noise, signal])

    result = run_dynamic_signal_identity(SIGNAL_DATE, scope(), client, DATASET, False)

    assert result.error_state == "evidence_projection_incomplete:missing_sample_id"
    assert result.input_counts["seed_graph"] == 2
    assert tuple(item.term for item in result.observations) == ("valid signal",)


def test_unaccepted_candidate_status_is_filtered_even_if_returned():
    client = FakeClient(
        candidate_rows=[
            candidate_row("approved"),
            candidate_row("applied", "applied"),
            candidate_row("rejected", "rejected"),
        ]
    )

    result = run_dynamic_signal_identity(SIGNAL_DATE, scope(), client, DATASET, False)

    assert result.input_counts["seed_candidates"] == 2
    assert tuple(item.term for item in result.candidate_inputs.seed_candidates) == (
        "applied",
        "approved",
    )
    assert tuple(item.term for item in result.observations) == ("applied", "approved")


def test_v3_seed_candidate_score_is_null_and_cannot_change_identity_order():
    low = candidate_row("neutral")
    low["score"] = 0.01
    high = candidate_row("neutral")
    high["score"] = 0.99

    low_result = run_dynamic_signal_identity(
        SIGNAL_DATE, scope(), FakeClient(candidate_rows=[low]), DATASET, False
    )
    high_result = run_dynamic_signal_identity(
        SIGNAL_DATE, scope(), FakeClient(candidate_rows=[high]), DATASET, False
    )

    assert low_result.candidate_inputs.seed_candidates[0].candidate_score is None
    assert high_result.candidate_inputs.seed_candidates[0].candidate_score is None
    assert identity_bytes(low_result) == identity_bytes(high_result)
    assert "audience_weighted_seed_candidates_excluded" not in low_result.missing_work
    assert "audience_weighted_seed_candidates_excluded" not in high_result.missing_work


def test_candidate_rows_enter_identity_with_neutral_facts_and_explicit_receipts():
    client = FakeClient(
        event_rows=[event_row()],
        graph_rows=[graph_row()],
        candidate_rows=[candidate_row()],
    )

    result = run_dynamic_signal_identity(SIGNAL_DATE, scope(), client, DATASET, False)
    observations = result.observations

    ledger = next(item for item in observations if item.term == "alpha")
    graph = next(item for item in observations if item.term == "beta")
    candidate = next(item for item in observations if item.term == "gamma")
    assert ledger.row_ids == ("ledger_za_alpha",)
    assert ledger.source_families == ("news",)
    assert graph.source_families == ("youtube",)
    assert graph.vendor_family == "google_youtube"
    assert graph.channel_family == "youtube"
    assert graph.platforms == ("youtube",)
    assert candidate.row_ids == ("candidate_gamma", "row_za_gamma")
    assert candidate.source_families == ()
    assert candidate.platforms == ("seed_candidates",)
    assert candidate.topic_tags == ("music",)
    assert candidate.observed_dates == (SIGNAL_DATE.isoformat(),)
    assert candidate.candidate_score is None
    assert tuple(item.term for item in observations) == ("alpha", "beta", "gamma")


def test_projected_evidence_uses_route_aware_identity_before_construction() -> None:
    from src.analysis.open_intelligence.pipeline import _projected_evidence_row

    row = evidence_row("social_comment")
    row.update(
        {
            "source": "socialcrawl",
            "platform": "youtube",
            "content_type": "comment",
            "endpoint": "youtube/video/comments",
        }
    )
    projected = _projected_evidence_row(row, "enriched_content")
    assert (projected.vendor_family, projected.channel_family) == ("socialcrawl", "youtube")


def test_projected_evidence_rejects_missing_approved_identity() -> None:
    from src.analysis.open_intelligence.pipeline import _projected_evidence_row

    row = evidence_row("social_unknown")
    row.update({"source": "socialcrawl", "platform": "facebook", "content_type": "post"})
    with pytest.raises(ValueError, match="source identity"):
        _projected_evidence_row(row, "enriched_content")


def test_missing_dependencies_return_typed_zero_write_partial(monkeypatch):
    client = FakeClient(
        event_rows=[event_row()],
        graph_rows=[graph_row()],
        candidate_rows=[candidate_row()],
        enriched_rows=[evidence_row("row_za_beta"), evidence_row("row_za_gamma")],
    )
    compose = Mock()
    monkeypatch.setattr("src.analysis.open_intelligence.pipeline.compose_components", compose)

    result = run_dynamic_signal_identity(SIGNAL_DATE, scope(), client, DATASET, False)

    assert result.components == ()
    assert result.component_count == 0
    assert result.persistable_count == 0
    assert result.row_counts == dict.fromkeys(
        ("candidates", "evidence", "membership", "lineage", "predictions"), 0
    )
    assert result.missing_work == (
        "composition_rule_bundle_unavailable",
        "evidence_receipt_policy_unavailable",
        "geo_provenance_unavailable",
        "incomplete_current_window",
        "no_qualifying_evidence",
        "semantic_provider_unavailable",
    )
    assert tuple(item.term for item in result.candidate_inputs.observations) == (
        "alpha",
        "beta",
        "gamma",
    )
    assert tuple(item.term for item in result.observations) == ("alpha", "beta", "gamma")
    projection = result.evidence_projection
    assert projection is not None
    assert projection.candidate_inputs == result.candidate_inputs
    assert projection.memberships_by_component == {}
    assert projection.candidate_only_receipts["za|keyword|alpha"] == ("ledger_za_alpha",)
    assert projection.candidate_only_receipts["za|keyword|beta"][0].startswith("seed_graph_")
    assert projection.candidate_only_receipts["za|keyword|gamma"] == ("candidate_gamma",)
    assert projection.receipts_by_member["za|keyword|alpha"] == ()
    assert tuple(item.row_id for item in projection.receipts_by_member["za|keyword|beta"]) == (
        "row_za_beta",
    )
    assert tuple(item.row_id for item in projection.receipts_by_member["za|keyword|gamma"]) == (
        "row_za_gamma",
    )
    assert projection.unresolved_sample_ids == ()
    assert projection.source_window.start_date == SIGNAL_DATE - timedelta(days=6)
    assert projection.source_window.end_date == SIGNAL_DATE
    assert projection.source_window.complete_partitions is False
    assert all(window.complete_partitions is False for window in result.source_windows.values())
    assert result.persistence_result is None
    assert result.dry_run is True
    compose.assert_not_called()


def test_missing_rule_does_not_call_supplied_semantic_provider_or_graph(monkeypatch):
    provider_call = Mock(return_value={})
    provider = SimpleNamespace(version="unused_semantics_v1", similarities=provider_call)
    compose = Mock()
    monkeypatch.setattr("src.analysis.open_intelligence.pipeline.compose_components", compose)

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        FakeClient(graph_rows=[graph_row()], enriched_rows=[evidence_row("row_za_beta")]),
        DATASET,
        False,
        semantic_provider=provider,
    )

    assert result.component_count == 0
    assert result.components == ()
    assert result.evidence_projection is not None
    assert result.evidence_projection.memberships_by_component == {}
    assert tuple(
        item.row_id for item in result.evidence_projection.receipts_by_member["za|keyword|beta"]
    ) == ("row_za_beta",)
    assert result.missing_work == (
        "composition_rule_bundle_unavailable",
        "evidence_receipt_policy_unavailable",
        "geo_provenance_unavailable",
        "incomplete_current_window",
        "no_qualifying_evidence",
    )
    provider_call.assert_not_called()
    compose.assert_not_called()


def test_missing_semantic_does_not_call_graph_or_fabricate_singletons(monkeypatch):
    compose = Mock()
    monkeypatch.setattr("src.analysis.open_intelligence.pipeline.compose_components", compose)

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        FakeClient(graph_rows=[graph_row()], enriched_rows=[evidence_row("row_za_beta")]),
        DATASET,
        False,
        rule_bundle=provisional_rules(),
    )

    assert result.component_count == 0
    assert result.components == ()
    assert result.evidence_projection is not None
    assert result.evidence_projection.memberships_by_component == {}
    assert result.missing_work == (
        "evidence_receipt_policy_unavailable",
        "geo_provenance_unavailable",
        "incomplete_current_window",
        "no_qualifying_evidence",
        "semantic_provider_unavailable",
    )
    compose.assert_not_called()


def test_missing_dependencies_retain_exact_unresolved_sample_ids(monkeypatch):
    compose = Mock()
    monkeypatch.setattr("src.analysis.open_intelligence.pipeline.compose_components", compose)

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        FakeClient(graph_rows=[graph_row()]),
        DATASET,
        False,
    )

    projection = result.evidence_projection
    assert projection is not None
    assert projection.unresolved_sample_ids == ("row_za_beta",)
    assert projection.receipts_by_member["za|keyword|beta"] == ()
    assert result.missing_work == (
        "composition_rule_bundle_unavailable",
        "evidence_projection_incomplete",
        "evidence_receipt_policy_unavailable",
        "geo_provenance_unavailable",
        "incomplete_current_window",
        "no_qualifying_evidence",
        "semantic_provider_unavailable",
    )
    assert result.error_state == "evidence_projection_incomplete:missing_sample_id"
    assert result.row_counts == dict.fromkeys(
        ("candidates", "evidence", "membership", "lineage", "predictions"), 0
    )
    compose.assert_not_called()


def test_provisional_dependencies_call_composition_once(monkeypatch):
    client = FakeClient(event_rows=[event_row()])
    components = (component("alpha"),)
    compose = Mock(return_value=components)
    monkeypatch.setattr("src.analysis.open_intelligence.pipeline.compose_components", compose)
    provider = SimpleNamespace(version="replay_lexical_v1", similarities=lambda observations: {})
    rules = SimpleNamespace(
        rule_version="provisional_v1",
        status="provisional",
        graph_rules=GraphRules(0.7, 0.55, 2),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=rules,
        semantic_provider=provider,
    )

    compose.assert_called_once_with(
        result.observations,
        provider,
        rules.graph_rules,
    )
    assert result.components == components
    assert result.component_count == 1
    assert result.persistable_count == 0
    assert result.missing_work == (
        "evidence_receipt_policy_unavailable",
        "geo_provenance_unavailable",
        "incomplete_current_window",
        "metric_provider_unavailable",
        "no_qualifying_evidence",
    )


def test_source_row_order_does_not_change_candidate_inputs_or_projection(monkeypatch):
    gamma_alt = candidate_row("gamma")
    gamma_alt["candidate_id"] = "candidate_gamma_alt"
    gamma_alt["sample_row_ids"] = ["row_za_gamma_alt"]
    forward = FakeClient(
        event_rows=[event_row("alpha"), event_row("delta")],
        graph_rows=[graph_row("beta"), graph_row("epsilon")],
        candidate_rows=[
            candidate_row("gamma"),
            gamma_alt,
            candidate_row("zeta", "applied"),
        ],
        enriched_rows=[
            evidence_row("row_za_beta"),
            evidence_row("row_za_epsilon"),
            evidence_row("row_za_gamma"),
            evidence_row("row_za_gamma_alt"),
            evidence_row("row_za_zeta"),
        ],
    )
    reverse = FakeClient(
        event_rows=list(reversed(forward.rows["event_ledger"])),
        graph_rows=list(reversed(forward.rows["seed_graph"])),
        candidate_rows=list(reversed(forward.rows["seed_candidates"])),
        enriched_rows=list(reversed(forward.rows["enriched_content"])),
    )

    def build_all(observations, _provider, _rules):
        return (component(*(item.term for item in observations)),)

    monkeypatch.setattr("src.analysis.open_intelligence.pipeline.compose_components", build_all)

    left = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        forward,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )
    right = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        reverse,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )

    assert left.candidate_inputs == right.candidate_inputs
    assert left.observations == right.observations
    assert identity_bytes(left) == identity_bytes(right)
    assert left.evidence_projection is not None
    assert right.evidence_projection is not None
    assert projection_bytes(left.evidence_projection) == projection_bytes(right.evidence_projection)
    assert left.evidence_projection.candidate_only_receipts["za|keyword|gamma"] == (
        "candidate_gamma",
        "candidate_gamma_alt",
    )


def test_overlapping_source_rows_are_canonicalized_before_composition(monkeypatch):
    client = FakeClient(
        graph_rows=[graph_row("shared")],
        candidate_rows=[candidate_row("shared")],
    )
    compose = Mock(return_value=())
    monkeypatch.setattr("src.analysis.open_intelligence.pipeline.compose_components", compose)
    provider = SimpleNamespace(version="replay_lexical_v1", similarities=lambda observations: {})
    rules = SimpleNamespace(
        rule_version="provisional_v1",
        status="provisional",
        graph_rules=GraphRules(0.7, 0.55, 2),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=rules,
        semantic_provider=provider,
    )

    assert result.observation_count == 1
    assert len(result.observations) == 1
    shared = result.observations[0]
    assert shared.term == "shared"
    assert shared.row_ids == ("candidate_shared", "row_za_shared")
    assert shared.platforms == ("seed_candidates", "youtube")
    assert shared.candidate_score is None
    compose.assert_called_once_with(result.observations, provider, rules.graph_rules)


def test_unknown_candidate_input_becomes_missing_work_before_composition(monkeypatch):
    bad_event = event_row()
    bad_event["corroborating_sources"] = ["unknown_vendor"]
    client = FakeClient(event_rows=[bad_event])
    compose = Mock()
    monkeypatch.setattr("src.analysis.open_intelligence.pipeline.compose_components", compose)

    result = run_dynamic_signal_identity(SIGNAL_DATE, scope(), client, DATASET, False)

    assert result.missing_work == ("candidate_input_invalid",)
    assert result.error_state == "candidate_input_invalid:source_family"
    assert result.input_counts["event_ledger"] == 1
    assert result.observation_count == 0
    compose.assert_not_called()


def provisional_rules():
    return SimpleNamespace(
        rule_version="provisional_v1",
        status="provisional",
        graph_rules=GraphRules(0.7, 0.55, 2),
    )


def semantic_provider():
    return SimpleNamespace(version="replay_lexical_v1", similarities=lambda observations: {})


def test_projection_prefers_enriched_and_queries_raw_only_for_exact_misses(monkeypatch):
    alpha = graph_row("alpha")
    alpha["sample_row_ids"] = ["enriched_wins"]
    beta = graph_row("beta")
    beta["sample_row_ids"] = ["raw_only"]
    client = FakeClient(
        graph_rows=[alpha, beta],
        enriched_rows=[evidence_row("enriched_wins")],
        raw_rows=[
            evidence_row("enriched_wins", source_table="raw_content"),
            evidence_row("raw_only", source_table="raw_content"),
        ],
    )
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("alpha", "beta"),)),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )

    projection = result.evidence_projection
    assert projection is not None
    assert tuple(EvidenceProjection.__dataclass_fields__) == (
        "candidate_inputs",
        "memberships_by_component",
        "receipts_by_member",
        "candidate_only_receipts",
        "unresolved_sample_ids",
        "missing_work",
        "source_window",
    )
    assert projection.candidate_inputs == result.candidate_inputs
    alpha_receipts = projection.receipts_by_member["za|keyword|alpha"]
    beta_receipts = projection.receipts_by_member["za|keyword|beta"]
    assert alpha_receipts[0].source_table == "enriched_content"
    assert beta_receipts[0].source_table == "raw_content"
    assert tuple(alpha_receipts[0].__dataclass_fields__) == (
        "row_id",
        "market",
        "source",
        "platform",
        "vendor_family",
        "channel_family",
        "content_type",
        "query_group",
        "query_term",
        "author_name",
        "author_handle",
        "author_handle_norm",
        "title",
        "text",
        "url",
        "published_at",
        "collected_at",
        "hashtags",
        "views",
        "likes",
        "comments",
        "shares",
        "engagement_total",
        "regional_score",
        "search_velocity_score",
        "slang_terms",
        "pipeline_run_id",
        "v2tone",
        "v2persons",
        "v2orgs",
        "v2locations",
        "v2gcam",
        "tone_avg",
        "tone_polarity",
        "topic_groups",
        "classification_layer",
        "sentiment_lexicon_score",
        "source_table",
    )
    assert alpha_receipts[0].query_group == "music"
    assert alpha_receipts[0].query_term == "amapiano"
    assert alpha_receipts[0].hashtags == "#amapiano"
    assert alpha_receipts[0].regional_score == 0.75
    assert alpha_receipts[0].search_velocity_score == 0.55
    assert alpha_receipts[0].topic_groups == ("music_amapiano",)
    assert alpha_receipts[0].v2locations == "Johannesburg"
    assert beta_receipts[0].query_group == "music"
    assert beta_receipts[0].regional_score is None
    assert beta_receipts[0].search_velocity_score is None
    assert beta_receipts[0].topic_groups is None
    assert beta_receipts[0].tone_avg is None
    assert projection.source_window.start_date == SIGNAL_DATE - timedelta(days=6)
    assert projection.source_window.end_date == SIGNAL_DATE
    assert projection.source_window.markets == ("ke", "ng", "za")
    assert projection.source_window.source_tables == (
        "ogilvy-trends-v2.trends_v2_staging.enriched_content",
        "ogilvy-trends-v2.trends_v2_staging.raw_content",
    )
    raw_call = next(call for call in client.calls if call[0] == "raw_content")
    assert parameter_values(raw_call[2])["sample_ids"] == ["raw_only"]
    for table in ("enriched_content", "raw_content"):
        call = next(call for call in client.calls if call[0] == table)
        sql = call[1]
        values = parameter_values(call[2])
        expected_columns = (
            ENRICHED_FACTUAL_COLUMNS if table == "enriched_content" else RAW_FACTUAL_COLUMNS
        )
        assert all(f"evidence.`{column}`" in sql for column in expected_columns)
        forbidden_columns = {
            "genz_score",
            "creator_watchlist_tier",
            "creator_watchlist_score",
            "slang_score",
        }
        assert not any(f"evidence.`{column}`" in sql for column in forbidden_columns)
        if table == "raw_content":
            assert not any(
                f"evidence.`{column}`" in sql
                for column in set(ENRICHED_FACTUAL_COLUMNS) - set(RAW_FACTUAL_COLUMNS)
            )
        assert "COUNT(*) OVER (PARTITION BY evidence.market, evidence.id) AS match_count" in sql
        assert "evidence.market = requested.market AND evidence.id = requested.id" in sql
        assert "ANY_VALUE" not in sql
        assert "ROW_NUMBER" not in sql
        assert "MAX_BY" not in sql
        assert "MIN_BY" not in sql
        join_clause = sql.split("JOIN requested", 1)[1].split("WHERE", 1)[0]
        assert "query_term" not in join_clause
        assert "author" not in join_clause
        assert "url" not in join_clause
        assert values["start_date"] == SIGNAL_DATE - timedelta(days=6)
        assert values["end_date"] == SIGNAL_DATE


def test_event_ledger_aggregate_keeps_one_membership_with_nested_facts(monkeypatch):
    event = event_row("alpha")
    event["corroborating_sources"] = ["trends", "rss"]
    client = FakeClient(event_rows=[event])
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("alpha"),)),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )

    projection = result.evidence_projection
    assert projection is not None
    memberships = next(iter(projection.memberships_by_component.values()))
    assert len(memberships) == 1
    membership = memberships[0]
    assert membership.row_id == "ledger_za_alpha"
    assert membership.source_families == ("news", "search")
    assert membership.platforms == ("news",)
    assert membership.qualifies_evidence is False
    assert projection.candidate_only_receipts == {"za|keyword|alpha": ("ledger_za_alpha",)}


def test_membership_identity_uses_candidate_facts_not_evidence_availability(monkeypatch):
    row = graph_row("alpha")
    row["sample_row_ids"] = ["sample_alpha"]
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("alpha"),)),
    )

    def run(enriched_rows):
        return run_dynamic_signal_identity(
            SIGNAL_DATE,
            scope(),
            FakeClient(graph_rows=[row], enriched_rows=enriched_rows),
            DATASET,
            False,
            rule_bundle=provisional_rules(),
            semantic_provider=semantic_provider(),
        ).evidence_projection

    present = run([evidence_row("sample_alpha")])
    missing = run([])
    assert present is not None
    assert missing is not None
    present_key, present_memberships = next(iter(present.memberships_by_component.items()))
    missing_key, missing_memberships = next(iter(missing.memberships_by_component.items()))
    assert present_key == missing_key
    assert present_memberships[0].member_id == missing_memberships[0].member_id
    assert present_memberships[0].row_id == missing_memberships[0].row_id
    assert present_memberships[0].qualifies_evidence is False
    assert missing_memberships[0].qualifies_evidence is False


def test_duplicate_enriched_sample_returns_typed_ambiguous_zero_write_result(monkeypatch):
    row = graph_row("alpha")
    row["sample_row_ids"] = ["duplicate"]
    client = FakeClient(
        graph_rows=[row],
        enriched_rows=[evidence_row("duplicate", match_count=2)],
        raw_rows=[evidence_row("duplicate", source_table="raw_content")],
    )
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("alpha"),)),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )

    projection = result.evidence_projection
    assert projection is not None
    assert projection.unresolved_sample_ids == ("duplicate",)
    assert projection.missing_work == ("ambiguous_sample_id", "evidence_projection_incomplete")
    assert projection.receipts_by_member["za|keyword|alpha"] == ()
    membership = next(iter(projection.memberships_by_component.values()))[0]
    assert membership.qualifies_evidence is False
    assert "ambiguous_sample_id" in result.missing_work
    assert result.row_counts == dict.fromkeys(
        ("candidates", "evidence", "membership", "lineage", "predictions"), 0
    )
    assert result.persistence_result is None
    assert "raw_content" not in tuple(call[0] for call in client.calls)


def test_duplicate_raw_sample_returns_typed_ambiguous_zero_write_result(monkeypatch):
    row = graph_row("alpha")
    row["sample_row_ids"] = ["duplicate"]
    client = FakeClient(
        graph_rows=[row],
        raw_rows=[evidence_row("duplicate", source_table="raw_content", match_count=2)],
    )
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("alpha"),)),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )

    projection = result.evidence_projection
    assert projection is not None
    assert projection.unresolved_sample_ids == ("duplicate",)
    assert "ambiguous_sample_id" in projection.missing_work
    assert projection.receipts_by_member["za|keyword|alpha"] == ()
    assert next(iter(projection.memberships_by_component.values()))[0].qualifies_evidence is False
    assert result.row_counts == dict.fromkeys(
        ("candidates", "evidence", "membership", "lineage", "predictions"), 0
    )
    raw_call = next(call for call in client.calls if call[0] == "raw_content")
    assert parameter_values(raw_call[2])["sample_ids"] == ["duplicate"]


def test_duplicate_rows_never_use_collected_timestamp_as_a_tie_break(monkeypatch):
    row = graph_row("alpha")
    row["sample_row_ids"] = ["duplicate"]
    older = evidence_row("duplicate", match_count=2)
    older["collected_at"] = datetime(2026, 8, 25, tzinfo=UTC)
    newer = evidence_row("duplicate", match_count=2)
    newer["collected_at"] = datetime(2026, 8, 26, tzinfo=UTC)
    client = FakeClient(graph_rows=[row], enriched_rows=[newer, older])
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("alpha"),)),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )

    projection = result.evidence_projection
    assert projection is not None
    assert projection.unresolved_sample_ids == ("duplicate",)
    assert "ambiguous_sample_id" in projection.missing_work
    assert projection.receipts_by_member["za|keyword|alpha"] == ()


def test_raw_row_sharing_valid_enriched_id_is_never_read_or_conflicting(monkeypatch):
    row = graph_row("alpha")
    row["sample_row_ids"] = ["conflict"]
    client = FakeClient(
        graph_rows=[row],
        enriched_rows=[evidence_row("conflict")],
        raw_rows=[evidence_row("conflict", source_table="raw_content")],
    )
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("alpha"),)),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )

    projection = result.evidence_projection
    assert projection is not None
    assert projection.unresolved_sample_ids == ()
    assert projection.missing_work == ()
    receipt = projection.receipts_by_member["za|keyword|alpha"][0]
    assert receipt.source_table == "enriched_content"
    assert next(iter(projection.memberships_by_component.values()))[0].qualifies_evidence is False
    assert "raw_content" not in tuple(call[0] for call in client.calls)


def test_unexpected_raw_overlap_cannot_overwrite_valid_enriched(monkeypatch):
    row = graph_row("alpha")
    row["sample_row_ids"] = ["stable"]
    client = FakeClient(graph_rows=[row])
    calls = []

    def load_rows(_client, *, table, keys, start_date, end_date):
        calls.append((table, keys, start_date, end_date))
        if table == "enriched_content":
            return (evidence_row("stable"),)
        assert keys == ()
        raw = evidence_row("stable", source_table="raw_content")
        raw["title"] = "Raw must not overwrite"
        return (raw,)

    monkeypatch.setattr("src.analysis.open_intelligence.pipeline._load_evidence_rows", load_rows)
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("alpha"),)),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )

    projection = result.evidence_projection
    assert projection is not None
    receipt = projection.receipts_by_member["za|keyword|alpha"][0]
    assert receipt.source_table == "enriched_content"
    assert receipt.title == "Title stable"
    assert calls[1][0] == "raw_content"
    assert calls[1][1] == ()


def test_candidate_id_is_candidate_only_and_only_sample_ids_enter_evidence_query(monkeypatch):
    row = candidate_row("gamma")
    row["sample_row_ids"] = ["sample_gamma"]
    client = FakeClient(
        candidate_rows=[row],
        enriched_rows=[evidence_row("sample_gamma")],
    )
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("gamma"),)),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )

    projection = result.evidence_projection
    assert projection is not None
    memberships = next(iter(projection.memberships_by_component.values()))
    assert len(memberships) == 1
    membership = memberships[0]
    assert membership.member_identity == "za|keyword|gamma"
    assert membership.row_id == "candidate_gamma"
    assert membership.qualifies_evidence is False
    assert membership.member_id == expected_member_id(
        member_identity=membership.member_identity,
        row_id="candidate_gamma",
        source_families=(),
        platforms=("seed_candidates",),
    )
    assert len(membership.member_id) == len("mem_") + 64
    assert projection.candidate_only_receipts == {"za|keyword|gamma": ("candidate_gamma",)}
    assert tuple(item.row_id for item in projection.receipts_by_member["za|keyword|gamma"]) == (
        "sample_gamma",
    )
    evidence_calls = [
        call for call in client.calls if call[0] in {"enriched_content", "raw_content"}
    ]
    assert evidence_calls
    for _, _, config, _ in evidence_calls:
        assert parameter_values(config)["sample_ids"] == ["sample_gamma"]


def test_repeated_sample_fans_out_to_distinct_memberships(monkeypatch):
    alpha = graph_row("alpha")
    beta = graph_row("beta")
    alpha["sample_row_ids"] = ["shared"]
    beta["sample_row_ids"] = ["shared"]
    client = FakeClient(
        graph_rows=[alpha, beta],
        enriched_rows=[evidence_row("shared")],
    )
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("alpha", "beta"),)),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )

    projection = result.evidence_projection
    assert projection is not None
    memberships = next(iter(projection.memberships_by_component.values()))
    assert len(memberships) == 2
    assert {item.member_identity for item in memberships} == {
        "za|keyword|alpha",
        "za|keyword|beta",
    }
    assert len({item.member_id for item in memberships}) == 2
    assert all(item.row_id != "shared" for item in memberships)
    assert tuple(item.row_id for item in projection.receipts_by_member["za|keyword|alpha"]) == (
        "shared",
    )
    assert tuple(item.row_id for item in projection.receipts_by_member["za|keyword|beta"]) == (
        "shared",
    )


def test_several_sample_receipts_do_not_multiply_one_member(monkeypatch):
    alpha = graph_row("alpha")
    alpha["sample_row_ids"] = ["sample_two", "sample_one"]
    client = FakeClient(
        graph_rows=[alpha],
        enriched_rows=[evidence_row("sample_one"), evidence_row("sample_two")],
    )
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("alpha"),)),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )

    projection = result.evidence_projection
    assert projection is not None
    memberships = next(iter(projection.memberships_by_component.values()))
    assert len(memberships) == 1
    assert tuple(item.row_id for item in projection.receipts_by_member["za|keyword|alpha"]) == (
        "sample_one",
        "sample_two",
    )


def test_alias_mapping_and_shared_sample_keep_distinct_canonical_memberships(monkeypatch):
    event = event_row("canonical")
    event["entity_aliases"] = ["alias"]
    alias = graph_row("alias")
    other = graph_row("other")
    alias["sample_row_ids"] = ["shared"]
    other["sample_row_ids"] = ["shared"]
    client = FakeClient(
        event_rows=[event],
        graph_rows=[alias, other],
        enriched_rows=[evidence_row("shared")],
    )
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("canonical", "other"),)),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )

    projection = result.evidence_projection
    assert projection is not None
    memberships = next(iter(projection.memberships_by_component.values()))
    assert len(memberships) == 2
    assert {item.member_identity for item in memberships} == {
        "za|keyword|canonical",
        "za|keyword|other",
    }
    assert all(item.row_id != "shared" for item in memberships)
    assert tuple(item.row_id for item in projection.receipts_by_member["za|keyword|canonical"]) == (
        "shared",
    )
    assert tuple(item.row_id for item in projection.receipts_by_member["za|keyword|other"]) == (
        "shared",
    )


def test_projection_refuses_more_than_five_samples_before_evidence_query(monkeypatch):
    row = graph_row("alpha")
    row["sample_row_ids"] = [f"sample_{index}" for index in range(6)]
    client = FakeClient(graph_rows=[row])
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("alpha"),)),
    )

    with pytest.raises(EvidenceProjectionLimitExceeded, match=r"seed_graph.*5"):
        run_dynamic_signal_identity(
            SIGNAL_DATE,
            scope(),
            client,
            DATASET,
            False,
            rule_bundle=provisional_rules(),
            semantic_provider=semantic_provider(),
        )

    assert not {call[0] for call in client.calls} & {"enriched_content", "raw_content"}


def test_missing_sample_returns_typed_incomplete_zero_write_result(monkeypatch):
    row = graph_row("alpha")
    row["sample_row_ids"] = ["missing"]
    client = FakeClient(graph_rows=[row])
    monkeypatch.setattr(
        "src.analysis.open_intelligence.pipeline.compose_components",
        Mock(return_value=(component("alpha"),)),
    )

    result = run_dynamic_signal_identity(
        SIGNAL_DATE,
        scope(),
        client,
        DATASET,
        False,
        rule_bundle=provisional_rules(),
        semantic_provider=semantic_provider(),
    )

    projection = result.evidence_projection
    assert projection is not None
    assert projection.unresolved_sample_ids == ("missing",)
    membership = next(iter(projection.memberships_by_component.values()))[0]
    assert membership.qualifies_evidence is False
    assert projection.receipts_by_member["za|keyword|alpha"] == ()
    assert projection.missing_work == ("evidence_projection_incomplete",)
    assert result.missing_work == (
        "evidence_projection_incomplete",
        "evidence_receipt_policy_unavailable",
        "geo_provenance_unavailable",
        "incomplete_current_window",
        "metric_provider_unavailable",
        "no_qualifying_evidence",
    )
    assert result.error_state == "evidence_projection_incomplete:missing_sample_id"
    assert result.row_counts == dict.fromkeys(
        ("candidates", "evidence", "membership", "lineage", "predictions"), 0
    )
    assert result.persistence_result is None
