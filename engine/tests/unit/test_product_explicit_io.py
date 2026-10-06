import re
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest
from src.analysis import generate_briefs as briefs
from src.analysis import generate_daily_summary as summary
from src.analysis import generate_seed_intelligence as seeds
from src.analysis import pan_african as pan
from src.analysis import seed_candidates as candidates
from src.analysis import seed_graph as graph
from src.analysis.gemini_client import GeminiClient
from src.scoring import velocity

from tests.unit.test_generate_briefs import _FAT_SAMPLE, _mock_brief_response, _topics_df
from tests.unit.test_seed_candidates import _sg_row
from tests.unit.test_seed_graph import _row
from tests.unit.test_seed_intelligence import _briefs, _fake_response

DAY = date(2026, 7, 6)
DATASET = "trends_v2_staging"


def forbidden(*args, **kwargs):
    raise AssertionError("global I/O escaped")


def sink_rows(target):
    def sink(rows):
        target.extend(rows)
        return len(rows)

    return sink


@pytest.fixture
def explicit_client(monkeypatch):
    for module in (briefs, summary, seeds, graph, candidates, pan):
        for name in ("get_client", "get_dataset", "insert_dataframe", "merge_dataframe"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, forbidden)
    client = MagicMock()
    client.project = "ogilvy-trends-v2"
    client.query.return_value.result.side_effect = lambda: (
        [SimpleNamespace(n=0)]
        if re.search(
            r"^\s*SELECT\s+COUNT\(\*\)\s+AS\s+n\s+FROM\b",
            client.query.call_args.args[0],
            re.IGNORECASE,
        )
        else []
    )
    client.query.return_value.to_dataframe.return_value = pd.DataFrame()
    client.get_table.return_value.schema = []
    return client


def prepare_briefs(monkeypatch):
    monkeypatch.setattr(briefs, "_query_top_topics", lambda *args: _topics_df().iloc[:1])
    monkeypatch.setattr(briefs, "_sample_rows_for_topic", lambda *args, **kwargs: _FAT_SAMPLE)
    monkeypatch.setattr(briefs, "_existing_brief_keys", lambda *args: set())
    monkeypatch.setattr(briefs.time, "sleep", lambda seconds: None)


def test_briefs_explicit_sink_owns_rows_and_usage(monkeypatch, explicit_client):
    prepare_briefs(monkeypatch)
    model = MagicMock()
    model.generate_brief.return_value = _mock_brief_response()
    rows, usage = [], []
    result = briefs.generate_briefs(
        trend_date=DAY,
        bq_client=explicit_client,
        dataset=DATASET,
        gemini_client=model,
        row_sink=sink_rows(rows),
        usage_sink=sink_rows(usage),
    )
    assert result.persist_succeeded
    assert len(rows) == 1
    assert rows[0]["query_group"] == "music_amapiano"
    assert rows[0]["trend_date"] == DAY
    assert sum(row["calls"] for row in usage) == 1


def test_briefs_explicit_sink_failure_is_visible(monkeypatch, explicit_client):
    prepare_briefs(monkeypatch)
    model = MagicMock()
    model.generate_brief.return_value = _mock_brief_response()

    def fail(rows):
        raise RuntimeError("staging row sink failed")

    result = briefs.generate_briefs(
        trend_date=DAY,
        bq_client=explicit_client,
        dataset=DATASET,
        gemini_client=model,
        row_sink=fail,
        usage_sink=lambda rows: len(rows),
    )
    assert result.persist_attempted
    assert not result.persist_succeeded
    assert "staging row sink failed" in result.persist_error


def sdk_response(parsed):
    return SimpleNamespace(
        text="{}",
        parsed=parsed,
        usage_metadata=SimpleNamespace(
            prompt_token_count=10, candidates_token_count=20, thoughts_token_count=0
        ),
    )


def test_summary_retries_cross_actual_sdk_boundary(monkeypatch, explicit_client):
    monkeypatch.setattr(summary, "_existing_non_empty_summary_for", lambda *args: False)
    calls = []
    responses = [
        sdk_response({}),
        sdk_response(
            {
                "summary_text": "Music stories spread across the markets.",
                "through_line": "Shared music rituals.",
                "call_to_action": "Read the source posts.",
                "key_topics": [],
                "rising_topics": [],
            }
        ),
    ]

    def generate_content(**kwargs):
        calls.append(kwargs)
        if len(calls) > 2:
            raise RuntimeError("attempt cap exceeded")
        return responses[len(calls) - 1]

    model = GeminiClient(
        project="ogilvy-trends-v2",
        client=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)),
    )
    rows, usage = [], []
    result = summary.generate_daily_summary(
        trend_date=DAY,
        briefs_by_topic=_briefs(),
        bq_client=explicit_client,
        dataset=DATASET,
        gemini_client=model,
        row_sink=sink_rows(rows),
        usage_sink=sink_rows(usage),
    )
    assert result is not None
    assert len(calls) == 2
    assert len(rows) == 1
    assert sum(row["calls"] for row in usage) == 2
    assert result.prompt_tokens == 20
    assert result.completion_tokens == 40


def test_summary_sdk_attempt_cap_refuses_retry_before_second_paid_call(
    monkeypatch, explicit_client
):
    monkeypatch.setattr(summary, "_existing_non_empty_summary_for", lambda *args: False)
    attempted, paid, usage = [], [], []

    def generate_content(**kwargs):
        attempted.append(kwargs)
        if paid:
            raise RuntimeError("attempt cap exhausted")
        paid.append(kwargs)
        return sdk_response({})

    model = GeminiClient(
        project="ogilvy-trends-v2",
        client=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)),
    )
    with pytest.raises(RuntimeError, match="attempt cap exhausted"):
        summary.generate_daily_summary(
            trend_date=DAY,
            briefs_by_topic=_briefs(),
            bq_client=explicit_client,
            dataset=DATASET,
            gemini_client=model,
            row_sink=forbidden,
            usage_sink=sink_rows(usage),
        )
    assert len(attempted) == 2
    assert len(paid) == 1
    assert sum(row["calls"] for row in usage) == 1


@pytest.mark.parametrize("failure", ["raise", "partial"])
def test_summary_explicit_sink_failure_propagates(monkeypatch, explicit_client, failure):
    monkeypatch.setattr(summary, "_existing_non_empty_summary_for", lambda *args: False)
    model = MagicMock()
    model.generate_brief.return_value = _mock_brief_response(
        {
            "summary_text": "Music stories.",
            "through_line": "Music.",
            "call_to_action": "Read the sources.",
            "key_topics": [],
            "rising_topics": [],
        }
    )

    def sink(rows):
        if failure == "raise":
            raise RuntimeError("explicit sink failed")
        return 0

    with pytest.raises((RuntimeError, ValueError), match="sink"):
        summary.generate_daily_summary(
            trend_date=DAY,
            briefs_by_topic=_briefs(),
            bq_client=explicit_client,
            dataset=DATASET,
            gemini_client=model,
            row_sink=sink,
            usage_sink=lambda rows: len(rows),
        )


def test_seed_intelligence_explicit_read_and_sink(explicit_client):
    model = MagicMock()
    model.generate_brief.return_value = _fake_response(
        [{"behaviour": "Shared rituals", "markets": ["za"], "evidence": ["source-1"]}]
    )
    rows, usage = [], []
    result = seeds.generate_seed_intelligence(
        trend_date=DAY,
        briefs_by_topic=_briefs(),
        bq_client=explicit_client,
        dataset=DATASET,
        gemini_client=model,
        row_sink=sink_rows(rows),
        usage_sink=sink_rows(usage),
    )
    assert len(result) == len(rows) == 1
    assert rows[0]["behaviour"] == "Shared rituals"
    assert DATASET in explicit_client.query.call_args.args[0]
    assert sum(row["calls"] for row in usage) == 1


@pytest.mark.parametrize("failure", ["raise", "partial"])
def test_seed_intelligence_explicit_sink_failure_propagates(explicit_client, failure):
    model = MagicMock()
    model.generate_brief.return_value = _fake_response([{"behaviour": "Shared rituals"}])

    def sink(rows):
        if failure == "raise":
            raise RuntimeError("explicit sink failed")
        return 0

    with pytest.raises((RuntimeError, ValueError), match="sink"):
        seeds.generate_seed_intelligence(
            trend_date=DAY,
            briefs_by_topic=_briefs(),
            bq_client=explicit_client,
            dataset=DATASET,
            gemini_client=model,
            row_sink=sink,
            usage_sink=lambda rows: len(rows),
        )


def test_graph_explicit_source_and_sink(explicit_client):
    explicit_client.query.return_value.to_dataframe.return_value = pd.DataFrame(
        [
            _row(
                text="#freshritual freshritual",
                collected_at=datetime(2026, 7, 6, tzinfo=UTC),
                v2gcam=None,
            )
        ]
    )
    rows = []
    counts = graph.build_and_persist_seed_graph(
        DAY, client=explicit_client, dataset="captured_sources", row_sink=sink_rows(rows)
    )
    assert counts["za"] > 0
    assert sum(counts.values()) == len(rows)
    assert "captured_sources.enriched_content" in explicit_client.query.call_args.args[0]
    explicit_client.load_table_from_file.assert_not_called()


def test_graph_explicit_source_missing_columns_refuses(explicit_client):
    explicit_client.query.return_value.to_dataframe.return_value = pd.DataFrame(
        [{"id": "r1", "market": "za", "text": "#freshritual"}]
    )
    with pytest.raises(ValueError, match="source_columns"):
        graph.build_and_persist_seed_graph(
            DAY, client=explicit_client, dataset="captured_sources", row_sink=forbidden
        )


def test_graph_explicit_partition_writer(explicit_client):
    assert graph.persist_seed_graph([], DAY, client=explicit_client, dataset=DATASET) == 0
    assert explicit_client.load_table_from_file.call_args.args[1] == (
        "ogilvy-trends-v2.trends_v2_staging.seed_graph$20260706"
    )


def test_candidate_stage_threads_read_and_write_clients(monkeypatch, explicit_client):
    seen = []

    def weekly(day, *, client, dataset):
        seen.append((day, client, dataset))
        return False

    monkeypatch.setattr(candidates, "weekly_lane_already_ran", weekly)
    monkeypatch.setattr(candidates, "fetch_week_proposed_counts", lambda *a, **k: {})
    monkeypatch.setattr(candidates, "fetch_seed_graph_window", lambda *a, **k: [_sg_row()])
    monkeypatch.setattr(candidates, "fetch_distinctiveness_baseline", lambda *a, **k: {})
    monkeypatch.setattr(candidates, "fetch_first_seen_map", lambda *a, **k: {})
    monkeypatch.setattr(candidates, "fetch_rejection_memory", lambda *a, **k: set())
    monkeypatch.setattr(candidates, "load_config_terms", lambda market: set())
    monkeypatch.setattr(candidates, "fetch_existing_candidate_ids", lambda *a, **k: set())
    rows = []
    count = candidates.run_seed_candidates_stage(
        DAY, lane="weekly", client=explicit_client, dataset=DATASET, row_sink=sink_rows(rows)
    )
    assert seen == [(DAY, explicit_client, DATASET)]
    assert count == len(rows)
    assert count > 0
    assert all(row["status"] == "pending" for row in rows)


def test_candidate_explicit_writer(explicit_client):
    row = {"candidate_id": "c1", "proposed_date": DAY, "market": "za"}
    assert candidates.persist_candidates([row], client=explicit_client, dataset=DATASET) == 1
    assert explicit_client.load_table_from_dataframe.call_args.args[1] == (
        "ogilvy-trends-v2.trends_v2_staging.seed_candidates"
    )


def test_pan_explicit_reads_and_sink_ignores_environment_flag(monkeypatch, explicit_client):
    monkeypatch.setenv("PAN_AFRICAN_ENABLED", "false")
    explicit_client.query.return_value.to_dataframe.return_value = pd.DataFrame(
        [
            {
                "market": "za",
                "query_group": "music_amapiano",
                "velocity_score": 0.4,
                "item_count": 30,
            },
            {
                "market": "ng",
                "query_group": "music_afrobeats",
                "velocity_score": 0.3,
                "item_count": 20,
            },
        ]
    )
    rows = []
    stories = pan.run_pan_african_stage(
        DAY, client=explicit_client, dataset=DATASET, row_sink=sink_rows(rows), enabled=True
    )
    assert len(stories) == len(rows) == 1
    assert rows[0]["total_item_count"] == 50
    assert DATASET in explicit_client.query.call_args.args[0]


def test_pan_explicit_disabled_never_reads(explicit_client):
    assert (
        pan.run_pan_african_stage(DAY, client=explicit_client, dataset=DATASET, enabled=False) == []
    )
    explicit_client.query.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["scalar", "batch", "windows"])
def test_velocity_injected_query_is_closed_to_supplied_date(monkeypatch, entrypoint):
    monkeypatch.setattr(velocity, "run_query", forbidden)
    captured = []

    def query(sql, *, params):
        captured.append((sql, params))
        return pd.DataFrame()

    kwargs = {"query_runner": query, "trend_date": DAY}
    if entrypoint == "scalar":
        velocity.compute_velocity_score("za", "music_amapiano", 10, **kwargs)
    elif entrypoint == "batch":
        velocity.compute_velocity_scores_for_today(
            {("za", "music_amapiano"): {"item_count": 10}}, **kwargs
        )
    else:
        velocity.compute_velocity_windows_for_today(
            {("za", "music_amapiano"): {"item_count": 10}}, **kwargs
        )
    assert len(captured) == 1
    assert "CURRENT_DATE" not in captured[0][0]
    assert "@trend_date" in captured[0][0]
    assert captured[0][1]["trend_date"] == DAY


def test_velocity_explicit_reader_requires_cutoff(monkeypatch):
    query = MagicMock()
    with pytest.raises(ValueError, match="trend_date"):
        velocity.compute_velocity_scores_for_today(
            {("za", "topic"): {"item_count": 1}}, query_runner=query
        )
    query.assert_not_called()
