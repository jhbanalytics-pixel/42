from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest


def _wave1_capability(route_set_sha256="exact"):
    from src.analysis.open_intelligence import funded_lane
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SET_SHA256

    from tests.unit.test_wave1_durable_capability import _bound_consumption

    authority, consumption = _bound_consumption()
    if route_set_sha256 == "exact":
        route_set_sha256 = WAVE1_ROUTE_SET_SHA256
    return funded_lane._issue_wave1_execution_capability(
        authority, consumption, route_set_sha256=route_set_sha256
    )


def test_wave1_route_set_digest_binds_every_spec_field_except_state() -> None:
    import re

    from src.ingestion.connectors.socialcrawl import (
        WAVE1_ROUTE_SET_SHA256,
        WAVE1_ROUTE_SPECS,
        wave1_route_set_sha256,
    )

    assert re.fullmatch(r"[0-9a-f]{64}", WAVE1_ROUTE_SET_SHA256)
    assert wave1_route_set_sha256(WAVE1_ROUTE_SPECS) == WAVE1_ROUTE_SET_SHA256
    reordered = dict(reversed(list(WAVE1_ROUTE_SPECS.items())))
    assert wave1_route_set_sha256(reordered) == WAVE1_ROUTE_SET_SHA256

    approved = {route: replace(spec, state="approved") for route, spec in WAVE1_ROUTE_SPECS.items()}
    assert wave1_route_set_sha256(approved) == WAVE1_ROUTE_SET_SHA256

    for field, value in (
        ("credit_cost", 2),
        ("maximum_calls", 9),
        ("maximum_debit", 99),
        ("phase", "other_phase"),
        ("required_parameters", ("other",)),
        ("maximum_pages", 2),
        ("channel_family", "other"),
        ("vendor_family", "other"),
    ):
        mutated = dict(WAVE1_ROUTE_SPECS)
        mutated["reddit/post/comments"] = replace(mutated["reddit/post/comments"], **{field: value})
        assert wave1_route_set_sha256(mutated) != WAVE1_ROUTE_SET_SHA256, field


def test_capability_issuance_binds_the_exact_route_set_digest() -> None:
    from src.analysis.open_intelligence.funded_lane import Wave1ExecutionBlocked
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SET_SHA256

    exact = _wave1_capability()
    assert exact.route_set_sha256 == WAVE1_ROUTE_SET_SHA256
    unbound = _wave1_capability(route_set_sha256=None)
    assert unbound.route_set_sha256 is None
    for wrong in ("0" * 64, WAVE1_ROUTE_SET_SHA256.upper(), "", 7):
        with pytest.raises(Wave1ExecutionBlocked, match="route set"):
            _wave1_capability(route_set_sha256=wrong)


def test_wave1_contract_artifact_carries_the_route_set_digest() -> None:
    import json
    from pathlib import Path

    from scripts.run_rss_now import _wave1_contract
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SET_SHA256

    artifact = _wave1_contract(None, datetime(2026, 9, 2, tzinfo=UTC))
    assert artifact["route_set_sha256"] == WAVE1_ROUTE_SET_SHA256
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "open_intelligence"
        / "wave1_contract_v1.json"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    assert payload["route_set_sha256"] == WAVE1_ROUTE_SET_SHA256
    assert payload["routes"] == json.loads(json.dumps(artifact["routes"]))
    assert payload["max_credits"] == 63


@pytest.mark.parametrize("route_set_sha256", [None])
def test_wave1_plan_refuses_without_route_authority(monkeypatch, route_set_sha256) -> None:
    from types import MappingProxyType

    from src.ingestion.connectors import socialcrawl
    from src.ingestion.connectors.socialcrawl import FundedSocialCrawlContext, SocialCrawlConnector

    usages = []
    context = FundedSocialCrawlContext(
        credential_lane="ogilvy_funded",
        secret_id="SOCIALCRAWL_OGILVY_API_KEY",
        run_allowance=100,
        phase_close_hook=usages.append,
        pre_call_authority_hook=lambda _debit: None,
        stage_name="stage_1_wave_1",
        execution_capability=_wave1_capability(route_set_sha256=route_set_sha256),
        wave1_requests=tuple(_wave1_requests()),
    )
    SocialCrawlConnector.configure_funded_run(context)
    try:
        with pytest.raises(ValueError, match="Wave 1 route is blocked"):
            SocialCrawlConnector()._validate_wave1_plan()
        monkeypatch.setattr(
            socialcrawl,
            "WAVE1_ROUTE_SPECS",
            MappingProxyType(
                {
                    route: replace(spec, state="approved")
                    for route, spec in socialcrawl.WAVE1_ROUTE_SPECS.items()
                }
            ),
        )
        with pytest.raises(ValueError, match="Wave 1 route is blocked"):
            SocialCrawlConnector()._validate_wave1_plan()
    finally:
        SocialCrawlConnector.reset_credits()


def test_wave1_plan_accepts_route_authority_while_specs_stay_blocked() -> None:
    from src.ingestion.connectors.socialcrawl import (
        WAVE1_ROUTE_SPECS,
        FundedSocialCrawlContext,
        SocialCrawlConnector,
    )

    assert all(spec.state == "blocked_fixture_unapproved" for spec in WAVE1_ROUTE_SPECS.values())
    usages = []
    context = FundedSocialCrawlContext(
        credential_lane="ogilvy_funded",
        secret_id="SOCIALCRAWL_OGILVY_API_KEY",
        run_allowance=100,
        phase_close_hook=usages.append,
        pre_call_authority_hook=lambda _debit: None,
        stage_name="stage_1_wave_1",
        execution_capability=_wave1_capability(),
        wave1_requests=tuple(_wave1_requests()),
    )
    SocialCrawlConnector.configure_funded_run(context)
    try:
        plan = SocialCrawlConnector()._validate_wave1_plan()
    finally:
        SocialCrawlConnector.reset_credits()
    assert plan == tuple(_wave1_requests())


def test_wave1_plan_accepts_a_subset_of_the_frozen_routes_and_refuses_foreign_or_empty() -> None:
    # The twelfth Wave 1 pilot on staging, 4 Sep 2026, made zero SocialCrawl calls:
    # the released run carried no TikTok music seed and no reddit evidence, and the
    # plan check demanded every frozen route. An empty seed list omits its routes;
    # a foreign route or an empty plan still refuses.
    from src.ingestion.connectors.socialcrawl import (
        FundedSocialCrawlContext,
        SocialCrawlConnector,
        Wave1RouteRequest,
    )

    subset = tuple(
        item for item in _wave1_requests() if not item.route.startswith(("tiktok/", "reddit/"))
    )
    assert {item.route for item in subset} == {
        "instagram/music/trending",
        "instagram/audio/reels",
        "instagram/search/reels",
        "youtube/shorts/trending",
        "youtube/video/comments",
    }

    def context(requests):
        return FundedSocialCrawlContext(
            credential_lane="ogilvy_funded",
            secret_id="SOCIALCRAWL_OGILVY_API_KEY",
            run_allowance=100,
            phase_close_hook=lambda usage: None,
            pre_call_authority_hook=lambda _debit: None,
            stage_name="stage_1_wave_1",
            execution_capability=_wave1_capability(),
            wave1_requests=tuple(requests),
        )

    SocialCrawlConnector.configure_funded_run(context(subset))
    try:
        assert SocialCrawlConnector()._validate_wave1_plan() == subset
    finally:
        SocialCrawlConnector.reset_credits()
    for bad in (
        (),
        (*subset, Wave1RouteRequest("tiktok/profile", {"handle": "x"}, "qualification")),
    ):
        SocialCrawlConnector.configure_funded_run(context(bad))
        try:
            with pytest.raises(ValueError, match="non-empty subset"):
                SocialCrawlConnector()._validate_wave1_plan()
        finally:
            SocialCrawlConnector.reset_credits()


def test_missing_capability_refuses_before_secret_or_network() -> None:
    from src.analysis.open_intelligence.funded_lane import Wave1ExecutionBlocked
    from src.analysis.open_intelligence.funded_lane_runtime import prepare_funded_lane

    effects: list[str] = []

    def secret_reader(_name: str) -> str:
        effects.append("secret")
        return "forbidden"

    with pytest.raises(Wave1ExecutionBlocked, match="capability is unavailable"):
        prepare_funded_lane(
            client=object(),
            dataset="trends_v2_staging_funded",
            run_id="run_wave1",
            execution_id="execution_wave1",
            as_of=datetime(2026, 8, 27, 10, tzinfo=UTC),
            activation_stage=1,
            environment="staging",
            secret_reader=secret_reader,
            ledger_writer=lambda _row: effects.append("ledger"),
            stage_name="stage_1_wave_1",
            execution_capability=None,
        )

    assert effects == []


@pytest.mark.parametrize("value", [None, "A" * 64, "a" * 63, "a" * 65, "not-a-digest"])
def test_wave1_guard_rejects_raw_digest_values(value: str | None) -> None:
    from src.analysis.open_intelligence.funded_lane import (
        Wave1ExecutionBlocked,
        _require_wave1_execution_capability,
    )

    with pytest.raises(Wave1ExecutionBlocked):
        _require_wave1_execution_capability(value, action="vendor_call")


def test_wave1_runtime_closes_four_global_phases_and_one_run(monkeypatch) -> None:
    from src.analysis.open_intelligence import funded_lane
    from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS, WAVE1_STAGE
    from src.analysis.open_intelligence.funded_lane_runtime import (
        FundedLaneRuntime,
        Wave1PhaseUsage,
    )
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

    now = datetime(2026, 8, 27, 10, tzinfo=UTC)
    rows: list[dict[str, object]] = []
    gate = SimpleNamespace(
        run_allowance=Decimal("100"),
        month_opening_balance=Decimal("250100"),
        current_balance=Decimal("250100"),
        monthly_balance_delta=Decimal("0"),
        monthly_ledger_debit=Decimal("0"),
        monthly_vendor_reported=Decimal("0"),
        attribution_state="complete",
    )
    runtime = FundedLaneRuntime(
        run_id="run_wave1",
        execution_id="execution_wave1",
        as_of=now,
        gate=gate,
        ledger_writer=rows.append,
        clock=lambda: now,
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
        source_value_writer=lambda _values: None,
    )

    for phase, cap in WAVE1_PHASE_CAPS.items():
        runtime.phase_close(
            Wave1PhaseUsage(
                market=None,
                phase=phase,
                calls=1,
                budget_debit_credits=cap,
                vendor_reported_credits=cap,
            )
        )
    runtime.close(
        post_balance=Decimal("250000"),
        recorded_at=now,
        source_value_results={
            f"/v1/{route}": evaluate_wave1_source_value(
                unique_observations=1,
                marginal_candidates=1,
                marginal_evidence=0,
            )
            for route in WAVE1_ROUTE_SPECS
        },
        gdelt_runtime_proof=SimpleNamespace(
            query_job_ids=(("events", "job-events"), ("gcam", "job-gcam")),
            query_bytes_processed=(("events", 100), ("gcam", 200)),
            persistence=SimpleNamespace(complete=True),
        ),
    )

    assert [row["phase"] for row in rows if row["event_type"] == "phase_close"] == list(
        WAVE1_PHASE_CAPS
    )
    assert sum(row["event_type"] == "phase_close" for row in rows) == 4
    assert sum(row["event_type"] == "run_close" for row in rows) == 1
    assert all(row["market"] is None for row in rows)


def test_wave1_route_matrix_is_frozen_and_never_uses_default_price() -> None:
    from src.ingestion.connectors.socialcrawl import CREDIT_COST, WAVE1_ROUTE_SPECS

    expected = {
        "tiktok/song": 1,
        "tiktok/song/videos": 1,
        "instagram/music/trending": 5,
        "instagram/audio/reels": 1,
        "instagram/search/reels": 9,
        "youtube/shorts/trending": 15,
        "youtube/video/comments": 5,
        "reddit/post/comments": 9,
    }
    assert {route: spec.credit_cost for route, spec in WAVE1_ROUTE_SPECS.items()} == expected
    assert {route: CREDIT_COST[route] for route in expected} == expected
    assert all(
        spec.maximum_debit == spec.credit_cost * spec.maximum_calls
        for spec in WAVE1_ROUTE_SPECS.values()
    )
    assert {route: WAVE1_ROUTE_SPECS[route].maximum_calls for route in expected} == {
        "tiktok/song": 5,
        "tiktok/song/videos": 5,
        "instagram/music/trending": 2,
        "instagram/audio/reels": 5,
        "instagram/search/reels": 2,
        "youtube/shorts/trending": 1,
        "youtube/video/comments": 3,
        "reddit/post/comments": 2,
    }
    assert WAVE1_ROUTE_SPECS["tiktok/song"].required_parameters == ("clipId",)
    assert WAVE1_ROUTE_SPECS["tiktok/song/videos"].required_parameters == ("clipId",)
    assert WAVE1_ROUTE_SPECS["instagram/audio/reels"].required_parameters == ("audio_id",)
    assert WAVE1_ROUTE_SPECS["youtube/video/comments"].required_parameters == ("url",)
    assert all(spec.maximum_pages == 1 for spec in WAVE1_ROUTE_SPECS.values())
    assert all(spec.state == "blocked_fixture_unapproved" for spec in WAVE1_ROUTE_SPECS.values())


def test_wave1_route_guard_refuses_before_http() -> None:
    from src.analysis.open_intelligence.funded_lane import Wave1ExecutionBlocked
    from src.ingestion.connectors.socialcrawl import SocialCrawlConnector

    class Session:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("HTTP must remain dark")

    session = Session()
    connector = SocialCrawlConnector(market="za")
    with pytest.raises(Wave1ExecutionBlocked):
        connector._call(session, "tiktok/song", {"musicId": "1"}, "wave1_tiktok_sound")
    assert session.calls == 0


def test_wave1_identity_keeps_vendor_and_channel_separate() -> None:
    from src.analysis.open_intelligence.candidates import WAVE1_SOURCE_FAMILY_MAP_VERSION
    from src.analysis.open_intelligence.graph import wave1_pair_is_independent

    assert WAVE1_SOURCE_FAMILY_MAP_VERSION == "channel_family_v2"
    assert wave1_pair_is_independent("socialcrawl", "youtube", "google_youtube", "youtube") is False
    assert wave1_pair_is_independent("gdelt", "news", "rss", "news") is False
    assert wave1_pair_is_independent("socialcrawl", "short_video", "gdelt", "news") is True


def test_wave1_independence_uses_maximum_unique_vendor_and_channel_subset() -> None:
    from src.analysis.open_intelligence.graph import wave1_independence_count

    rows = (
        ("row_a", "socialcrawl", "short_video"),
        ("row_b", "socialcrawl", "reddit"),
        ("row_c", "google_youtube", "youtube"),
        ("row_d", "gdelt", "news"),
        ("row_e", "rss", "news"),
    )
    assert wave1_independence_count(rows) == 3


def test_every_graph_vote_path_requires_distinct_vendor_and_channel() -> None:
    from src.analysis.open_intelligence.candidates import Observation
    from src.analysis.open_intelligence.graph import GraphRules, build_relationship_votes

    left = Observation(
        market="za",
        term="left",
        candidate_type="keyword",
        row_ids=("left",),
        source_families=("youtube",),
        platforms=("youtube",),
        vendor_family="socialcrawl",
        channel_family="youtube",
    )
    right = Observation(
        market="za",
        term="right",
        candidate_type="keyword",
        row_ids=("right",),
        source_families=("youtube",),
        platforms=("youtube",),
        vendor_family="google_youtube",
        channel_family="youtube",
    )
    votes = build_relationship_votes(
        left,
        right,
        semantic_similarity=0.0,
        rules=GraphRules(
            semantic_vote_floor=0.8,
            component_similarity_floor=0.5,
            temporal_overlap_days=3,
        ),
    )
    assert votes.independent_source_family is False


def test_global_wave1_row_needs_retained_geo_provenance() -> None:
    from src.analysis.open_intelligence.candidates import resolve_wave1_market

    assert (
        resolve_wave1_market(
            vendor_market=None,
            retained_geo_market=None,
            geo_method_id=None,
            geo_receipt_id=None,
        )
        is None
    )
    assert (
        resolve_wave1_market(
            vendor_market=None,
            retained_geo_market="za",
            geo_method_id="gdelt_event_market_v1",
            geo_receipt_id="receipt_za_1",
        )
        == "za"
    )


def test_wave1_dedup_uses_platform_native_id_then_url() -> None:
    from src.analysis.open_intelligence.candidates import deduplicate_wave1_content

    rows = (
        {"row_id": "a", "platform": "youtube", "native_id": "v1", "url": "https://x/v1"},
        {"row_id": "b", "platform": "youtube", "native_id": "v1", "url": "https://x/other"},
        {"row_id": "c", "platform": "instagram", "native_id": "v1", "url": "https://x/v1"},
        {"row_id": "d", "platform": "instagram", "native_id": "v2", "url": "https://x/v1"},
    )
    assert tuple(row["row_id"] for row in deduplicate_wave1_content(rows)) == ("a",)


@pytest.mark.parametrize(
    ("unique_observations", "marginal_candidates", "marginal_evidence", "state"),
    [
        (0, 1, 1, "permanently_rejected"),
        (1, 0, 0, "permanently_rejected"),
        (1, 1, 0, "passed"),
    ],
)
def test_source_value_gate_cannot_approve_zero_value(
    unique_observations: int,
    marginal_candidates: int,
    marginal_evidence: int,
    state: str,
) -> None:
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    result = evaluate_wave1_source_value(
        unique_observations=unique_observations,
        marginal_candidates=marginal_candidates,
        marginal_evidence=marginal_evidence,
    )
    assert result.state == state
    assert result.human_override_allowed is False


def test_source_lab_keeps_wave1_routes_inventory_only_with_exact_identity() -> None:
    from tests.unit.test_source_lab_persistence import _rows

    row = next(item for item in _rows() if item["route_path"] == "/v1/tiktok/song")
    assert row["status"] == "inventory_only"
    assert row["source_family"] == "short_video"
    assert row["vendor_family"] == "socialcrawl"
    assert row["channel_family"] == "short_video"
    assert row["funded_lane"] == "stage_1_wave_1"
    assert row["calls"] is None
    assert row["kill_test_result"] is None


def test_funded_ledger_accepts_only_four_wave1_phase_closes() -> None:
    from src.analysis.open_intelligence.funded_lane_persistence import (
        FundedLaneLedgerBatch,
        LedgerBatchInvalid,
    )

    from tests.unit.test_funded_lane_persistence import _row

    phases = (
        "wave1_tiktok_sound",
        "wave1_instagram_reels",
        "wave1_youtube_shorts_comments",
        "wave1_reddit_comments",
    )
    rows = tuple(
        _row(
            ledger_id=f"ledger_{phase}",
            market=None,
            phase=phase,
            event_type="phase_close",
            calls=1,
            budget_debit_credits=Decimal("1"),
            vendor_reported_credits=Decimal("1"),
        )
        for phase in phases
    )
    close = _row(
        ledger_id="ledger_run_close",
        market=None,
        phase="run_close",
        event_type="run_close",
        calls=4,
        budget_debit_credits=Decimal("4"),
        vendor_reported_credits=Decimal("4"),
    )
    assert len(FundedLaneLedgerBatch(rows=(*rows, close)).rows) == 5
    with pytest.raises(LedgerBatchInvalid):
        FundedLaneLedgerBatch(rows=(*rows[:-1], close))


def test_wave1_schema_definitions_keep_shared_row_contracts_and_add_exact_grain() -> None:
    root = Path(__file__).resolve().parents[2]
    evidence = (root / "infra/bigquery_schemas/signal_evidence_v2.sql").read_text("utf-8")
    membership = (root / "infra/bigquery_schemas/signal_membership_v2.sql").read_text("utf-8")
    performance = (root / "infra/bigquery_schemas/source_performance_daily_v2.sql").read_text(
        "utf-8"
    )
    assert evidence.count("vendor_family STRING OPTIONS") == 1
    assert evidence.count("channel_family STRING OPTIONS") == 1
    assert membership.count("vendor_families ARRAY<STRING>") == 1
    assert membership.count("channel_families ARRAY<STRING>") == 1
    assert "vendor_family STRING OPTIONS" in performance
    assert "channel_family STRING OPTIONS" in performance
    assert "funded_lane STRING" in performance
    events = (root / "infra/bigquery_schemas/gdelt_events_wave1_v1.sql").read_text("utf-8")
    market = (root / "infra/bigquery_schemas/gdelt_event_market_wave1_v1.sql").read_text("utf-8")
    gcam = (root / "infra/bigquery_schemas/gdelt_gcam_wave1_v1.sql").read_text("utf-8")
    assert "GLOBALEVENTID INT64 NOT NULL" in events
    assert "CLUSTER BY event_root_code" in events
    assert "CLUSTER BY market, evidence_role" in market
    assert "document_url STRING NOT NULL" in gcam
    assert "v20_1 FLOAT64" in gcam


def test_wave1_migration_apply_refuses_before_credentials() -> None:
    from scripts.migrations import create_open_intelligence_v2 as migration

    with pytest.raises(migration.MigrationError, match="durable migration wrapper"):
        migration.main(
            ["--target", "staging", "--apply"],
            credential_loader=lambda: pytest.fail("credentials must remain dark"),
            client_factory=lambda **_kwargs: pytest.fail("client must remain dark"),
            request_factory=lambda: pytest.fail("request must remain dark"),
        )


def _wave1_requests():
    from src.ingestion.connectors.socialcrawl import Wave1RouteRequest

    return (
        Wave1RouteRequest("tiktok/song", {"clipId": "song_1"}, "qualification"),
        Wave1RouteRequest("tiktok/song/videos", {"clipId": "song_1"}, "qualification"),
        Wave1RouteRequest("instagram/music/trending", {}, "qualification"),
        Wave1RouteRequest("instagram/audio/reels", {"audio_id": "audio_1"}, "qualification"),
        Wave1RouteRequest("instagram/search/reels", {"query": "culture"}, "qualification"),
        Wave1RouteRequest("youtube/shorts/trending", {}, "qualification"),
        Wave1RouteRequest(
            "youtube/video/comments", {"url": "https://youtube.invalid/short"}, "qualification"
        ),
        Wave1RouteRequest(
            "reddit/post/comments", {"url": "https://reddit.invalid/post"}, "qualification"
        ),
    )


def _approved_wave1_context(monkeypatch, usages, requests=None):
    from src.ingestion.connectors.socialcrawl import FundedSocialCrawlContext

    return FundedSocialCrawlContext(
        credential_lane="ogilvy_funded",
        secret_id="SOCIALCRAWL_OGILVY_API_KEY",
        run_allowance=100,
        phase_close_hook=usages.append,
        pre_call_authority_hook=lambda _debit: None,
        stage_name="stage_1_wave_1",
        execution_capability=_wave1_capability(),
        wave1_requests=tuple(requests or _wave1_requests()),
    )


def test_wave1_maximum_seed_plan_validates_at_route_phase_and_run_caps(monkeypatch) -> None:
    from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS
    from src.ingestion.connectors.socialcrawl import (
        WAVE1_ROUTE_SPECS,
        SocialCrawlConnector,
        Wave1RouteRequest,
    )

    requests = []
    for index in range(5):
        seed = f"song-{index}"
        requests.extend(
            (
                Wave1RouteRequest("tiktok/song", {"clipId": seed}, "qualification"),
                Wave1RouteRequest("tiktok/song/videos", {"clipId": seed}, "qualification"),
            )
        )
    requests.extend(
        Wave1RouteRequest("instagram/search/reels", {"query": f"term-{index}"}, "qualification")
        for index in range(2)
    )
    requests.extend(
        Wave1RouteRequest(
            "youtube/video/comments",
            {"url": f"https://youtube.invalid/short-{index}"},
            "qualification",
        )
        for index in range(3)
    )
    requests.extend(
        Wave1RouteRequest(
            "reddit/post/comments",
            {"url": f"https://reddit.invalid/post-{index}"},
            "qualification",
        )
        for index in range(2)
    )
    context = _approved_wave1_context(monkeypatch, [], requests)
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    assert SocialCrawlConnector(market="za")._validate_wave1_plan() == tuple(requests)
    assert sum(WAVE1_ROUTE_SPECS[request.route].credit_cost for request in requests) == 61
    phase_debits = {
        phase: sum(
            WAVE1_ROUTE_SPECS[request.route].credit_cost
            for request in requests
            if WAVE1_ROUTE_SPECS[request.route].phase == phase
        )
        for phase in WAVE1_PHASE_CAPS
    }
    assert phase_debits == {
        "wave1_tiktok_sound": 10,
        "wave1_instagram_reels": 18,
        "wave1_youtube_shorts_comments": 15,
        "wave1_reddit_comments": 18,
    }
    assert all(phase_debits[phase] <= cap for phase, cap in WAVE1_PHASE_CAPS.items())

    extra_trending = (
        *requests,
        Wave1RouteRequest("instagram/music/trending", {}, "qualification"),
        Wave1RouteRequest("instagram/music/trending", {}, "ingestion"),
    )
    SocialCrawlConnector.configure_funded_run(
        _approved_wave1_context(monkeypatch, [], extra_trending)
    )
    with pytest.raises(ValueError, match="execution debit limit"):
        SocialCrawlConnector(market="za")._validate_wave1_plan()


def test_actual_seed_builder_plan_is_fewer_calls_and_fits_live_worst_case_ceiling(
    monkeypatch,
) -> None:
    import json
    from pathlib import Path

    from scripts.run_rss_now import (
        WAVE1_SEED_MANIFEST_CONTRACT,
        _wave1_requests_from_seed_manifest,
    )
    from src.analysis.open_intelligence.source_lab import normalize_catalog
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS, SocialCrawlConnector

    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "source_lab"
        / "wave1_metered_catalog_rows.json"
    )
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    catalog = normalize_catalog(
        raw,
        checked_at=datetime(2026, 9, 27, 13, 10, tzinfo=UTC),
        expected_platforms=4,
        expected_endpoints=9,
        wave1_route_ids=tuple(WAVE1_ROUTE_SPECS),
    )
    seed_manifest = {
        "contract_version": WAVE1_SEED_MANIFEST_CONTRACT,
        "replacement_run_id": "run_20260903_dynamic_apply_v2_r16",
        "row_set_digest": "a" * 64,
        "source_sha": "b" * 40,
        "tiktok_music_ids": [{"music_id": "music_1", "market": "za"}],
        "reddit_urls": [{"url": "https://reddit.invalid/post_1", "market": "za"}],
        "instagram_search_terms": [],
        "youtube_short_urls": [{"url": "https://youtube.invalid/short_1", "market": "ke"}],
    }
    requests = _wave1_requests_from_seed_manifest(seed_manifest)
    context = _approved_wave1_context(monkeypatch, [], requests)
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)
    try:
        plan = SocialCrawlConnector()._validate_wave1_plan()
    finally:
        SocialCrawlConnector.reset_credits()

    catalog_by_route = {
        endpoint.route_path.removeprefix("/v1/"): endpoint for endpoint in catalog.endpoints
    }
    worst_case = 0
    for request in plan:
        range_token = catalog_by_route[request.route].credits_label.split(" ", 1)[0]
        worst_case += int(range_token.split("-")[-1])

    assert plan == requests
    assert len(plan) == 4
    assert {request.call_role for request in plan} == {"qualification"}
    assert worst_case == 16
    assert worst_case <= 63


def test_wave1_vendor_overage_stops_before_the_next_http(monkeypatch) -> None:
    from src.ingestion.connectors.socialcrawl import SocialCrawlConnector

    events = []
    context = replace(
        _approved_wave1_context(monkeypatch, []),
        pre_call_authority_hook=lambda debit: events.append(("authority", debit)),
    )
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Response:
        def __init__(self, route):
            self.route = route

        def json(self):
            from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

            return {
                "success": True,
                "credits_used": (
                    4 if self.route == "tiktok/song" else WAVE1_ROUTE_SPECS[self.route].credit_cost
                ),
                "data": {"items": []},
            }

    class Session:
        def get(self, url, *, params, timeout, stream=False):
            route = url.rsplit("/v1/", 1)[1]
            events.append(("http", route))
            return Response(route)

    connector = SocialCrawlConnector(market="za")
    connector._timeout = 45
    with pytest.raises(ValueError, match="quoted authority"):
        connector._run_wave1_routes(Session())
    assert events == [
        ("authority", 1),
        ("http", "tiktok/song"),
    ]


def test_wave1_response_byte_ceiling_stops_streaming_and_closes_response() -> None:
    from src.ingestion.connectors.socialcrawl import (
        WAVE1_RESPONSE_MAX_BYTES,
        _read_wave1_payload,
    )

    class Response:
        closed = False

        def iter_content(self, chunk_size):
            assert chunk_size > 0
            yield b"{" + (b"x" * WAVE1_RESPONSE_MAX_BYTES)

        def close(self):
            self.closed = True

    response = Response()
    with pytest.raises(ValueError, match="response exceeds byte ceiling"):
        _read_wave1_payload(response)
    assert response.closed is True


def test_wave1_response_item_ceiling_refuses_before_admission(monkeypatch) -> None:
    from src.ingestion.connectors.socialcrawl import (
        WAVE1_RESPONSE_MAX_ITEMS,
        SocialCrawlConnector,
    )

    context = _approved_wave1_context(monkeypatch, [], _wave1_requests())
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Response:
        def json(self):
            return {
                "success": True,
                "credits_used": 1,
                "data": {"items": [{}] * (WAVE1_RESPONSE_MAX_ITEMS + 1)},
            }

    class Session:
        def get(self, *_args, **_kwargs):
            assert _kwargs["stream"] is True
            return Response()

    connector = SocialCrawlConnector(market="za")
    connector._timeout = 45
    with pytest.raises(ValueError, match="response item ceiling"):
        connector._run_wave1_routes(Session())


def test_wave1_transport_failure_attributes_the_attempt_to_its_phase(monkeypatch) -> None:
    from src.ingestion.connectors.socialcrawl import SocialCrawlConnector

    usages = []
    context = _approved_wave1_context(monkeypatch, usages)
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Session:
        def get(self, _url, *, params, timeout, stream=False):
            raise RuntimeError("transport failed after request attempt")

    connector = SocialCrawlConnector(market="za")
    connector._timeout = 45
    with pytest.raises(RuntimeError, match="transport failed"):
        connector._run_wave1_routes(Session())

    usage = next(item for item in usages if item.phase == "wave1_tiktok_sound")
    assert usage.calls == 1
    assert usage.budget_debit_credits == 1
    assert usage.vendor_reported_credits == 0


def test_wave1_qualification_rows_do_not_create_downstream_source_value(
    monkeypatch,
) -> None:
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS, SocialCrawlConnector

    context = _approved_wave1_context(monkeypatch, [])
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Response:
        def __init__(self, route):
            self.route = route

        def json(self):
            slug = self.route.replace("/", "-")
            return {
                "success": True,
                "credits_used": WAVE1_ROUTE_SPECS[self.route].credit_cost,
                "data": {
                    "items": [
                        {
                            "market": "za",
                            "query_term": f"candidate-{slug}",
                            "native_id": f"native-{slug}",
                            "url": f"https://evidence.invalid/{slug}",
                        }
                    ]
                },
            }

    class Session:
        def get(self, url, *, params, timeout, stream=False):
            return Response(url.rsplit("/v1/", 1)[1])

    connector = SocialCrawlConnector(market="za")
    connector._timeout = 45
    assert connector._run_wave1_routes(Session()) == ()
    values = SocialCrawlConnector.wave1_source_values()
    assert tuple(values) == tuple(f"/v1/{route}" for route in WAVE1_ROUTE_SPECS)
    assert all(result.unique_observations == 0 for result in values.values())
    assert all(result.marginal_candidates == 0 for result in values.values())
    assert all(result.marginal_evidence == 0 for result in values.values())


def test_wave1_context_runs_once_globally_without_legacy_phase_runners(monkeypatch) -> None:
    from src.ingestion.connectors.socialcrawl import SocialCrawlConnector

    context = _approved_wave1_context(monkeypatch, [])
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)
    calls = []
    global_rows = tuple(
        {
            "source": "socialcrawl",
            "platform": "youtube",
            "market": market,
            "content_type": "comment",
            "endpoint": "youtube/video/comments",
            "vendor_family": "socialcrawl",
            "channel_family": "youtube",
            "source_family": "youtube",
            "geo_method_id": "retained_source_geo_v1",
            "geo_receipt_id": f"geo_{market}",
            "native_id": f"native_{market}",
            "source_family_map_version": "channel_family_v2",
        }
        for market in ("za", "ng", "ke")
    )
    monkeypatch.setattr(
        SocialCrawlConnector,
        "_run_wave1_routes",
        lambda self, session: calls.append(self.market) or global_rows,
    )
    for name in (
        "_phase_discover",
        "_phase_creators",
        "_phase_search",
        "_phase_reddit",
        "_phase_accounts",
        "_phase_news",
        "_phase_facebook",
    ):
        monkeypatch.setattr(
            SocialCrawlConnector,
            name,
            lambda *_args, phase=name: pytest.fail(f"legacy runner reached: {phase}"),
        )
    monkeypatch.setattr(
        "src.ingestion.connectors.socialcrawl.load_sources",
        lambda: {"socialcrawl": {"enabled": True}},
    )
    monkeypatch.setattr("src.ingestion.connectors.socialcrawl.get_secret", lambda _name: "key")
    monkeypatch.setenv("SOCIALCRAWL_CREDENTIAL_LANE", "ogilvy_funded")
    monkeypatch.setenv("TRENDS_ENV", "staging")

    frames = {market: SocialCrawlConnector(market=market).fetch() for market in ("za", "ng", "ke")}

    assert calls == ["za"]
    assert {market: tuple(frame["market"]) for market, frame in frames.items()} == {
        "za": ("za",),
        "ng": ("ng",),
        "ke": ("ke",),
    }


def test_wave1_runner_invokes_only_frozen_routes_and_emits_four_usages(monkeypatch) -> None:
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS, SocialCrawlConnector

    usages = []
    context = _approved_wave1_context(monkeypatch, usages)
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Response:
        def __init__(self, route):
            self.route = route

        def json(self):
            return {
                "success": True,
                "credits_used": WAVE1_ROUTE_SPECS[self.route].credit_cost,
                "data": {},
            }

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, *, params, timeout, stream=False):
            route = url.rsplit("/v1/", 1)[1]
            self.calls.append((route, dict(params), timeout))
            return Response(route)

    session = Session()
    connector = SocialCrawlConnector(market="za")
    connector._timeout = 45
    assert connector._run_wave1_routes(session) == ()
    assert tuple(route for route, _params, _timeout in session.calls) == tuple(WAVE1_ROUTE_SPECS)
    assert tuple(item.phase for item in usages) == (
        "wave1_tiktok_sound",
        "wave1_instagram_reels",
        "wave1_youtube_shorts_comments",
        "wave1_reddit_comments",
    )
    assert all(item.market is None for item in usages)


def test_wave1_partial_failure_accounts_every_attempt_and_emits_four_conservative_usages(
    monkeypatch,
) -> None:
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS, SocialCrawlConnector

    usages = []
    context = _approved_wave1_context(monkeypatch, usages)
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Response:
        def json(self):
            return {"success": True, "credits_used": 1, "data": {}}

    class Session:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return Response()
            raise RuntimeError("route failed after one completed call")

    connector = SocialCrawlConnector(market="za")
    connector._timeout = 45
    with pytest.raises(RuntimeError, match="route failed"):
        connector._run_wave1_routes(Session())

    assert len(usages) == 4
    first = next(item for item in usages if item.phase == WAVE1_ROUTE_SPECS["tiktok/song"].phase)
    assert (first.calls, first.budget_debit_credits, first.vendor_reported_credits) == (2, 2, 1)
    assert SocialCrawlConnector.credits_used() == 2


def test_wave1_billed_failure_records_reported_spend_before_success_refusal(monkeypatch) -> None:
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS, SocialCrawlConnector

    usages = []
    context = _approved_wave1_context(monkeypatch, usages)
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Response:
        def json(self):
            return {"success": False, "credits_used": 4, "error": {"type": "fixture"}}

    class Session:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return Response()

    connector = SocialCrawlConnector(market="za")
    connector._timeout = 45
    with pytest.raises(ValueError, match="quoted authority"):
        connector._run_wave1_routes(Session())

    phase = WAVE1_ROUTE_SPECS["tiktok/song"].phase
    usage = next(item for item in usages if item.phase == phase)
    assert (usage.calls, usage.budget_debit_credits, usage.vendor_reported_credits) == (1, 4, 4)
    assert SocialCrawlConnector.credits_used() == 4


def test_wave1_reported_overage_stops_before_next_call_authority(monkeypatch) -> None:
    from src.ingestion.connectors.socialcrawl import SocialCrawlConnector

    requests = list(_wave1_requests())
    instagram = requests.pop(2)
    requests = [
        instagram,
        replace(instagram, call_role="ingestion"),
        *requests,
    ]
    context = _approved_wave1_context(monkeypatch, [], requests)
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Response:
        def json(self):
            return {"success": True, "credits_used": 9, "data": {}}

    class Session:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return Response()

    session = Session()
    connector = SocialCrawlConnector(market="za")
    connector._timeout = 45
    with pytest.raises(ValueError, match="quoted authority"):
        connector._run_wave1_routes(session)
    assert session.calls == 1


def test_wave1_runner_applies_geo_and_dedup_before_returning_ingestion_rows(monkeypatch) -> None:
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS, SocialCrawlConnector

    seeded = list(_wave1_requests())
    seeded[6] = replace(seeded[6], market="za")
    requests = (
        *seeded,
        replace(seeded[2], call_role="ingestion"),
    )
    context = _approved_wave1_context(monkeypatch, [], requests)
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Response:
        def __init__(self, route, sequence):
            self.route = route
            self.sequence = sequence

        def json(self):
            items = []
            if self.sequence == 2 or (
                self.route == "youtube/video/comments" and self.sequence == 1
            ):
                base = {
                    "source": "socialcrawl",
                    "platform": "instagram" if self.route.startswith("instagram") else "youtube",
                    "content_type": "reel" if self.route.startswith("instagram") else "short",
                    "row_id": f"{self.route}_a",
                    "native_id": "shared_native",
                    "url": "https://social.invalid/shared",
                    "vendor_market": None,
                    "retained_geo_market": "za",
                    "geo_method_id": "retained_source_geo_v1",
                    "geo_receipt_id": "geo_1",
                }
                items = [base, {**base, "row_id": f"{self.route}_b"}]
            return {
                "success": True,
                "credits_used": WAVE1_ROUTE_SPECS[self.route].credit_cost,
                "data": {"items": items},
            }

    class Session:
        def __init__(self):
            self.counts = {}

        def get(self, url, *, params, timeout, stream=False):
            route = url.rsplit("/v1/", 1)[1]
            self.counts[route] = self.counts.get(route, 0) + 1
            return Response(route, self.counts[route])

    connector = SocialCrawlConnector(market="za")
    connector._timeout = 45
    rows = connector._run_wave1_routes(Session())
    assert len(rows) == 1
    assert rows[0]["market"] == "za"
    assert rows[0]["vendor_family"] == "socialcrawl"
    assert rows[0]["channel_family"] in {"short_video", "youtube"}
    assert rows[0]["source_family_map_version"] == "channel_family_v2"
    values = SocialCrawlConnector.wave1_source_values()
    active_routes = tuple(route for route, result in values.items() if result.state == "passed")
    assert active_routes == (f"/v1/{rows[0]['endpoint']}",)


def test_wave1_runner_chains_missing_audio_and_uses_manifest_comment_qualifications(
    monkeypatch,
) -> None:
    from scripts.run_rss_now import _wave1_requests_from_seed_manifest
    from src.ingestion.connectors.socialcrawl import (
        WAVE1_ROUTE_SPECS,
        SocialCrawlConnector,
        Wave1RouteRequest,
    )

    seed = {
        "contract_version": "wave1-r3-seed-manifest-v2",
        "replacement_run_id": "run_20260903_dynamic_apply_v2_r16",
        "row_set_digest": "a" * 64,
        "source_sha": "b" * 40,
        "tiktok_music_ids": [{"music_id": "song-1", "market": "za"}],
        "reddit_urls": [{"url": "https://reddit.invalid/post", "market": "za"}],
        "instagram_search_terms": [{"term": "culture", "market": "za"}],
        "youtube_short_urls": [{"url": "https://youtube.invalid/short", "market": "za"}],
    }
    # The seeded plan carries no trending pair. Instagram's pair remains within
    # its phase cap; YouTube's new 15 credit quote permits one qualification only.
    context = _approved_wave1_context(
        monkeypatch,
        [],
        (
            *_wave1_requests_from_seed_manifest(seed),
            Wave1RouteRequest("instagram/music/trending", {}, "qualification"),
            Wave1RouteRequest("instagram/music/trending", {}, "ingestion"),
        ),
    )
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Response:
        def __init__(self, route, sequence):
            self.route = route
            self.sequence = sequence

        def json(self):
            items = []
            if self.route == "instagram/music/trending" and self.sequence == 2:
                items = [
                    {
                        "source": "socialcrawl",
                        "platform": "instagram",
                        "content_type": "reel",
                        "row_id": "ig-row",
                        "native_id": "ig-native",
                        "audio_id": "audio-1",
                        "url": "https://instagram.invalid/reel",
                        "vendor_market": None,
                        "retained_geo_market": "za",
                        "geo_method_id": "retained_source_geo_v1",
                        "geo_receipt_id": "geo-ig",
                    }
                ]
            return {
                "success": True,
                "credits_used": WAVE1_ROUTE_SPECS[self.route].credit_cost,
                "data": {"items": items},
            }

    class Session:
        def __init__(self):
            self.calls = []
            self.counts = {}

        def get(self, url, *, params, timeout, stream=False):
            route = url.rsplit("/v1/", 1)[1]
            self.calls.append((route, dict(params)))
            self.counts[route] = self.counts.get(route, 0) + 1
            return Response(route, self.counts[route])

    session = Session()
    connector = SocialCrawlConnector(market="za")
    connector._timeout = 45
    rows = connector._run_wave1_routes(session)
    assert len(rows) == 1
    assert rows[0]["endpoint"] == "instagram/music/trending"
    assert ("instagram/audio/reels", {"audio_id": "audio-1"}) in session.calls
    assert (
        "youtube/video/comments",
        {"url": "https://youtube.invalid/short"},
    ) in session.calls
    assert all(route != "youtube/shorts/trending" for route, _params in session.calls)


@pytest.mark.parametrize("limiting_budget,expected_audio_calls", [("phase", 2), ("execution", 1)])
def test_wave1_audio_chain_reserves_remaining_plan_before_paid_calls(
    monkeypatch, limiting_budget, expected_audio_calls
) -> None:
    from src.ingestion.connectors.socialcrawl import (
        WAVE1_ROUTE_SPECS,
        SocialCrawlConnector,
        Wave1RouteRequest,
    )

    requests = [
        Wave1RouteRequest("instagram/music/trending", {}, "qualification"),
        Wave1RouteRequest("instagram/music/trending", {}, "ingestion"),
    ]
    instagram_terms = 2 if limiting_budget == "phase" else 1
    requests.extend(
        Wave1RouteRequest("instagram/search/reels", {"query": f"term-{index}"}, "qualification")
        for index in range(instagram_terms)
    )
    if limiting_budget == "execution":
        for index in range(5):
            requests.extend(
                (
                    Wave1RouteRequest("tiktok/song", {"clipId": f"song-{index}"}, "qualification"),
                    Wave1RouteRequest(
                        "tiktok/song/videos", {"clipId": f"song-{index}"}, "qualification"
                    ),
                )
            )
        requests.extend(
            Wave1RouteRequest(
                "youtube/video/comments",
                {"url": f"https://youtube.invalid/short-{index}"},
                "qualification",
            )
            for index in range(3)
        )
        requests.extend(
            Wave1RouteRequest(
                "reddit/post/comments",
                {"url": f"https://reddit.invalid/post-{index}"},
                "qualification",
            )
            for index in range(2)
        )
    usages = []
    context = _approved_wave1_context(monkeypatch, usages, requests)
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Response:
        def __init__(self, route, sequence):
            self.route = route
            self.sequence = sequence

        def json(self):
            items = (
                [
                    {"post": {"id": f"reel-{index}", "ext": {"music_id": f"audio-{index}"}}}
                    for index in range(5)
                ]
                if self.route == "instagram/music/trending" and self.sequence == 2
                else []
            )
            return {
                "success": True,
                "credits_used": WAVE1_ROUTE_SPECS[self.route].credit_cost,
                "data": {"items": items},
            }

    class Session:
        def __init__(self):
            self.calls = []
            self.counts = {}

        def get(self, url, *, params, timeout, stream=False):
            route = url.rsplit("/v1/", 1)[1]
            self.calls.append((route, dict(params)))
            self.counts[route] = self.counts.get(route, 0) + 1
            return Response(route, self.counts[route])

    session = Session()
    connector = SocialCrawlConnector(market="za")
    connector._timeout = 45
    connector._run_wave1_routes(session)
    audio_calls = [params["audio_id"] for route, params in session.calls if route == "instagram/audio/reels"]
    assert audio_calls == [f"audio-{index}" for index in range(expected_audio_calls)]
    assert len(session.calls) == len(requests) + expected_audio_calls
    assert SocialCrawlConnector.credits_used() == sum(
        WAVE1_ROUTE_SPECS[route].credit_cost for route, _params in session.calls
    )
    assert SocialCrawlConnector.credits_used() == (
        30 if limiting_budget == "phase" else 63
    )
    assert len(usages) == 4


def test_new_youtube_trending_price_refuses_a_pair_before_http(monkeypatch) -> None:
    from src.ingestion.connectors.socialcrawl import (
        SocialCrawlConnector,
        Wave1RouteRequest,
    )

    requests = (
        Wave1RouteRequest("youtube/shorts/trending", {}, "qualification"),
        Wave1RouteRequest("youtube/shorts/trending", {}, "ingestion"),
    )
    context = _approved_wave1_context(monkeypatch, [], requests)
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)
    with pytest.raises(ValueError, match="route call limit"):
        SocialCrawlConnector(market="za")._validate_wave1_plan()


def test_wave1_chain_reads_the_vendor_nested_audio_id_and_degrades_without_one(
    monkeypatch,
) -> None:
    # Attempt 17 on staging, 4 Sep 2026: the vendor nests the id under
    # post.ext.music_id and the chain read a root audio_id, then raised and
    # dropped every admitted row. A page without an id is a short chain.
    from src.ingestion.connectors.socialcrawl import SocialCrawlConnector, Wave1RouteRequest

    def run(items_for_trending):
        context = _approved_wave1_context(
            monkeypatch,
            [],
            (
                Wave1RouteRequest("instagram/music/trending", {}, "qualification"),
                Wave1RouteRequest("instagram/music/trending", {}, "ingestion"),
            ),
        )
        SocialCrawlConnector.reset_credits()
        SocialCrawlConnector.configure_funded_run(context)

        class Response:
            def __init__(self, route, sequence):
                self.route = route
                self.sequence = sequence

            def json(self):
                items = items_for_trending if self.sequence == 2 else []
                return {
                    "success": True,
                    "credits_used": 0,
                    "data": {"items": items if self.route == "instagram/music/trending" else []},
                }

        class Session:
            def __init__(self):
                self.calls = []
                self.counts = {}

            def get(self, url, *, params, timeout, stream=False):
                route = url.rsplit("/v1/", 1)[1]
                self.calls.append((route, dict(params)))
                self.counts[route] = self.counts.get(route, 0) + 1
                return Response(route, self.counts[route])

        session = Session()
        connector = SocialCrawlConnector(market="za")
        connector._timeout = 45
        rows = connector._run_wave1_routes(session)
        return session.calls, rows

    nested = [
        {
            "post": {
                "id": "ig-1",
                "url": "https://instagram.invalid/reel/ig-1",
                "ext": {"music_id": "audio-nested"},
            }
        },
        {"post": {"id": "ig-2", "url": "https://www.instagram.com/reels/audio/998877/"}},
        {
            "metadata": {"is_trending_in_clips": True},
            "track": {"audio_cluster_id": "2252835162207684", "audio_asset_id": "1494662545202817"},
        },
    ]
    calls, _rows = run(nested)
    assert ("instagram/audio/reels", {"audio_id": "audio-nested"}) in calls
    assert ("instagram/audio/reels", {"audio_id": "998877"}) in calls
    assert ("instagram/audio/reels", {"audio_id": "2252835162207684"}) in calls

    calls, rows = run([{"post": {"id": "ig-3", "url": "https://instagram.invalid/reel/ig-3"}}])
    assert all(route != "instagram/audio/reels" for route, _params in calls)
    assert rows == ()


def test_seeded_wave1_call_rows_carry_the_seed_market_as_retained_geography() -> None:
    # Attempt 17 on staging, 4 Sep 2026: seeded calls returned vendor items the
    # adapter could not place, so every route measured zero. The seed's market is
    # the retained geography and the receipt names the seed; a call with no
    # market yields rows the adapter drops.
    from types import SimpleNamespace

    from src.analysis.open_intelligence.candidates import adapt_wave1_evidence_rows
    from src.ingestion.connectors.socialcrawl import SocialCrawlConnector, Wave1RouteRequest

    context = SimpleNamespace(execution_capability=SimpleNamespace(run_id="run_seeded"))
    post_item = {
        "post": {
            "id": "reel-1",
            "url": "https://www.instagram.com/reel/reel-1/",
            "content": {"text": "amapiano weekend"},
            "author": {"username": "dj_one", "display_name": "DJ One"},
            "engagement": {"views": 1200, "likes": 40, "comments": 3, "shares": None},
            "published_at": "2026-09-03T10:00:00Z",
            "ext": {"music_id": "77"},
        },
        "computed": {},
    }
    comment_item = {
        "comment": {
            "id": "c-1",
            "url": "https://youtube.invalid/watch?v=1&lc=c-1",
            "text": "this slaps",
            "author": {"username": "@fan"},
            "engagement": {"likes": 2},
            "published_at": "2026-09-03T11:00:00Z",
        }
    }
    seeded = Wave1RouteRequest(
        "instagram/search/reels", {"query": "amapiano"}, "qualification", market="za"
    )
    row = SocialCrawlConnector._wave1_row_from_item(post_item, seeded, context)
    assert row["platform"] == "instagram"
    assert row["content_type"] == "instagram/post"
    assert row["native_id"] == "reel-1"
    assert row["query_term"] == "query=amapiano"
    assert row["retained_geo_market"] == "za"
    assert row["geo_method_id"] == "wave1_seed_market_v1"
    assert row["geo_receipt_id"].startswith("geo_")
    assert row["row_id"].startswith("wave1_")
    assert row["endpoint"] == "instagram/search/reels"
    assert row["views"] == 1200.0
    other = SocialCrawlConnector._wave1_row_from_item(
        comment_item,
        Wave1RouteRequest("youtube/video/comments", {"url": "u"}, "qualification", market="ke"),
        context,
    )
    assert (other["platform"], other["content_type"], other["native_id"]) == (
        "youtube",
        "youtube/comment",
        "c-1",
    )
    assert other["text"] == "this slaps"
    bare = SocialCrawlConnector._wave1_row_from_item(
        {"comment": {"id": 4471, "text": "no permalink", "url": None}},
        Wave1RouteRequest(
            "youtube/video/comments",
            {"url": "https://youtu.be/8hP0HnoBIIM"},
            "qualification",
            market="ng",
        ),
        context,
    )
    assert bare["native_id"] == "4471"
    assert bare["url"] == "https://youtu.be/8hP0HnoBIIM#comment-4471"
    assert adapt_wave1_evidence_rows((bare,))[0]["market"] == "ng"
    adapted = adapt_wave1_evidence_rows((row, other))
    assert [(item["market"], item["channel_family"]) for item in adapted] == [
        ("za", "short_video"),
        ("ke", "youtube"),
    ]
    unseeded = SocialCrawlConnector._wave1_row_from_item(
        post_item, Wave1RouteRequest("instagram/music/trending", {}, "ingestion"), context
    )
    assert unseeded["retained_geo_market"] is None
    assert adapt_wave1_evidence_rows((unseeded,)) == ()
    assert SocialCrawlConnector._wave1_row_from_item({"post": {"id": "x"}}, seeded, context) is None
    passthrough = {"row_id": "r", "native_id": "n", "url": "u"}
    assert SocialCrawlConnector._wave1_row_from_item(passthrough, seeded, context) == {
        **passthrough,
        "endpoint": "instagram/search/reels",
    }


def test_wave1_measurement_covers_only_the_routes_the_seeds_filled() -> None:
    from src.ingestion.connectors.socialcrawl import SocialCrawlConnector

    rows = (
        {
            "endpoint": "instagram/search/reels",
            "market": "za",
            "native_id": "a",
            "url": "https://x/a",
            "query_term": "query=amapiano",
        },
    )
    measured = SocialCrawlConnector.measure_wave1_source_values(rows, ("instagram/search/reels",))
    assert set(measured) == {"/v1/instagram/search/reels"}
    assert measured["/v1/instagram/search/reels"].state == "passed"
    everything = SocialCrawlConnector.measure_wave1_source_values(rows)
    assert len(everything) == 8
    assert everything["/v1/reddit/post/comments"].state == "permanently_rejected"


def test_global_wave1_rows_keep_resolved_market_in_existing_ingestion_shell(monkeypatch) -> None:
    import pandas as pd
    from scripts import run_rss_now

    monkeypatch.setattr(
        run_rss_now.SocialCrawlConnector,
        "_funded_context",
        SimpleNamespace(stage_name="stage_1_wave_1"),
    )

    class Connector:
        def __init__(self, *, market):
            self._fetch_failures = []
            self._endpoint_failures = []

        def safe_fetch(self):
            return pd.DataFrame([{"market": "ng", "id": "wave1_row"}])

        @classmethod
        def empty_dataframe(cls):
            return pd.DataFrame()

    _source, frame, _failures, error = run_rss_now._fetch_one_connector(
        "za", "socialcrawl", Connector
    )
    assert error is None
    assert frame.iloc[0]["market"] == "ng"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda requests: (replace(requests[0], params={"musicId": "wrong"}), *requests[1:]),
            "parameters",
        ),
        (
            lambda requests: (
                *requests,
                replace(requests[2], call_role="ingestion"),
                replace(requests[2], call_role="ingestion"),
            ),
            "route call",
        ),
        (lambda requests: (replace(requests[0], page=2), *requests[1:]), "page"),
        (
            lambda requests: (replace(requests[0], call_role="ingestion"), *requests[1:]),
            "call role",
        ),
    ],
)
def test_wave1_runner_refuses_invalid_plan_before_http(monkeypatch, mutation, message) -> None:
    from src.ingestion.connectors.socialcrawl import SocialCrawlConnector

    context = _approved_wave1_context(monkeypatch, [], mutation(_wave1_requests()))
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Session:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("HTTP must remain dark")

    session = Session()
    with pytest.raises(ValueError, match=message):
        SocialCrawlConnector(market="za")._run_wave1_routes(session)
    assert session.calls == 0


@pytest.mark.parametrize("limit", ["phase", "execution"])
def test_wave1_runner_refuses_phase_or_63_credit_overrun_before_http(monkeypatch, limit) -> None:
    from types import MappingProxyType

    from src.ingestion.connectors import socialcrawl
    from src.ingestion.connectors.socialcrawl import SocialCrawlConnector

    usages = []
    context = _approved_wave1_context(monkeypatch, usages)
    specs = dict(socialcrawl.WAVE1_ROUTE_SPECS)
    requests = list(_wave1_requests())
    if limit == "phase":
        specs["instagram/music/trending"] = replace(
            specs["instagram/music/trending"],
            credit_cost=10,
            maximum_calls=4,
            maximum_debit=40,
        )
        requests.extend(
            [
                replace(requests[2], call_role="ingestion"),
                replace(requests[2], call_role="ingestion"),
            ]
        )
    else:
        costs = {
            "tiktok/song": 10,
            "tiktok/song/videos": 10,
            "instagram/music/trending": 8,
            "instagram/audio/reels": 8,
            "instagram/search/reels": 8,
            "youtube/shorts/trending": 7,
            "youtube/video/comments": 7,
            "reddit/post/comments": 6,
        }
        specs = {
            route: replace(spec, credit_cost=cost, maximum_debit=cost)
            for route, spec in specs.items()
            for cost in (costs[route],)
        }
    monkeypatch.setattr(socialcrawl, "WAVE1_ROUTE_SPECS", MappingProxyType(specs))
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(replace(context, wave1_requests=tuple(requests)))

    class Session:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("HTTP must remain dark")

    session = Session()
    with pytest.raises(ValueError, match=f"{limit} debit"):
        SocialCrawlConnector(market="za")._run_wave1_routes(session)
    assert session.calls == 0


def test_wave1_blocked_fixture_refuses_before_http(monkeypatch) -> None:
    from src.analysis.open_intelligence.funded_lane import Wave1ExecutionBlocked
    from src.ingestion.connectors.socialcrawl import FundedSocialCrawlContext, SocialCrawlConnector

    context = FundedSocialCrawlContext(
        "ogilvy_funded",
        "SOCIALCRAWL_OGILVY_API_KEY",
        100,
        lambda _usage: None,
        lambda _debit: None,
        "stage_1_wave_1",
        "a" * 64,
        _wave1_requests(),
    )
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Session:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("HTTP must remain dark")

    session = Session()
    with pytest.raises(Wave1ExecutionBlocked, match="capability is unavailable"):
        SocialCrawlConnector(market="za")._run_wave1_routes(session)
    assert session.calls == 0


def test_wave1_runner_rejects_vendor_reported_route_debit_overrun(monkeypatch) -> None:
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS, SocialCrawlConnector

    context = _approved_wave1_context(monkeypatch, [])
    SocialCrawlConnector.reset_credits()
    SocialCrawlConnector.configure_funded_run(context)

    class Response:
        def __init__(self, route):
            self.route = route

        def json(self):
            reported = (
                WAVE1_ROUTE_SPECS[self.route].maximum_debit + 1
                if self.route == "tiktok/song"
                else WAVE1_ROUTE_SPECS[self.route].credit_cost
            )
            return {"success": True, "credits_used": reported, "data": {}}

    class Session:
        calls = 0

        def get(self, url, *, params, timeout, stream=False):
            self.calls += 1
            return Response(url.rsplit("/v1/", 1)[1])

    connector = SocialCrawlConnector(market="za")
    connector._timeout = 45
    with pytest.raises(ValueError, match="quoted authority"):
        connector._run_wave1_routes(Session())


@pytest.mark.parametrize(
    ("source", "platform", "content_type", "endpoint", "expected"),
    [
        ("socialcrawl", "tiktok", "video", None, ("socialcrawl", "short_video")),
        ("socialcrawl", "instagram", "reel", None, ("socialcrawl", "short_video")),
        ("socialcrawl", "youtube", "comment", None, ("socialcrawl", "youtube")),
        ("socialcrawl", "reddit", "comment", None, ("socialcrawl", "reddit")),
        ("YouTube Channel", "youtube", "video", None, ("google_youtube", "youtube")),
        ("gdelt", "news", "article", None, ("gdelt", "news")),
        ("rss", "web", "article", None, ("rss", "news")),
    ],
)
def test_route_aware_source_identity_resolver(source, platform, content_type, endpoint, expected):
    from src.analysis.open_intelligence.candidates import resolve_source_identity

    assert (
        resolve_source_identity(
            source=source,
            platform=platform,
            content_type=content_type,
            endpoint=endpoint,
        )
        == expected
    )


def test_route_aware_source_identity_rejects_unknown_socialcrawl_channel() -> None:
    from src.analysis.open_intelligence.candidates import resolve_source_identity

    with pytest.raises(ValueError, match="source identity"):
        resolve_source_identity(
            source="socialcrawl", platform="facebook", content_type="post", endpoint=None
        )


def test_family_only_identity_is_confined_to_explicit_v1_fixture_adapter() -> None:
    from src.analysis.open_intelligence.candidates import resolve_v1_fixture_source_identity

    assert resolve_v1_fixture_source_identity("staging_fixture_source") == (
        "staging_fixture_source",
        "staging_fixture_source",
    )
    with pytest.raises(ValueError, match="fixture only"):
        resolve_v1_fixture_source_identity("news")


def test_missing_explicit_identity_cannot_add_wave1_independence() -> None:
    from src.analysis.open_intelligence.candidates import Observation
    from src.analysis.open_intelligence.graph import GraphRules, build_relationship_votes

    legacy = Observation(
        market="za",
        term="legacy",
        candidate_type="keyword",
        row_ids=("legacy",),
        source_families=("news",),
        platforms=("news",),
    )
    wave1 = Observation(
        market="za",
        term="wave1",
        candidate_type="keyword",
        row_ids=("wave1",),
        source_families=("youtube",),
        platforms=("youtube",),
        vendor_family="socialcrawl",
        channel_family="youtube",
    )
    votes = build_relationship_votes(
        legacy,
        wave1,
        semantic_similarity=0.0,
        rules=GraphRules(0.8, 0.5, 3),
    )
    assert votes.independent_source_family is False


def test_wave1_adapter_applies_geo_identity_and_dedup_before_admission() -> None:
    from src.analysis.open_intelligence.candidates import adapt_wave1_evidence_rows

    base = {
        "source": "socialcrawl",
        "platform": "youtube",
        "content_type": "comment",
        "endpoint": "youtube/video/comments",
        "native_id": "comment_1",
        "url": "https://youtube.invalid/comment/1",
        "vendor_market": None,
        "retained_geo_market": "za",
        "geo_method_id": "retained_source_geo_v1",
        "geo_receipt_id": "geo_1",
    }
    rows = (
        {**base, "row_id": "b"},
        {**base, "row_id": "a"},
        {
            **base,
            "row_id": "c",
            "native_id": "comment_2",
            "url": "https://youtube.invalid/comment/2",
            "retained_geo_market": None,
            "geo_method_id": None,
            "geo_receipt_id": None,
        },
    )
    admitted = adapt_wave1_evidence_rows(rows)
    assert tuple(row["row_id"] for row in admitted) == ("a",)
    assert admitted[0]["market"] == "za"
    assert admitted[0]["vendor_family"] == "socialcrawl"
    assert admitted[0]["channel_family"] == "youtube"
    assert admitted[0]["source_family"] == "youtube"


def test_source_lab_activation_is_derived_only_from_measured_source_value() -> None:
    from src.analysis.open_intelligence.source_lab import (
        evaluate_wave1_source_value,
        resolve_wave1_activation,
    )

    zero = evaluate_wave1_source_value(
        unique_observations=0, marginal_candidates=1, marginal_evidence=1
    )
    useful = evaluate_wave1_source_value(
        unique_observations=2, marginal_candidates=1, marginal_evidence=0
    )
    assert resolve_wave1_activation(zero) == "permanently_rejected"
    assert resolve_wave1_activation(useful) == "active"


def test_wave1_authority_survives_fetch_raw_enriched_and_projection_handoff() -> None:
    from scripts import run_rss_now
    from src.analysis.open_intelligence.candidates import adapt_wave1_evidence_rows
    from src.analysis.open_intelligence.pipeline import (
        EVIDENCE_COLUMNS_BY_TABLE,
        _projected_evidence_row,
        _unique_evidence_rows,
    )
    from src.ingestion.connectors.socialcrawl import (
        WAVE1_AUTHORITY_COLUMNS,
        SocialCrawlConnector,
    )

    source = {
        "source": "socialcrawl",
        "platform": "youtube",
        "content_type": "comment",
        "endpoint": "reddit/post/comments",
        "row_id": "reddit_comment_1",
        "native_id": "reddit_native_1",
        "url": "https://reddit.invalid/comment/1",
        "vendor_market": None,
        "retained_geo_market": "za",
        "geo_method_id": "retained_source_geo_v1",
        "geo_receipt_id": "geo_reddit_1",
    }
    admitted = adapt_wave1_evidence_rows((source,))[0]
    frame = SocialCrawlConnector._wave1_handoff_frame((admitted,))
    assert set(WAVE1_AUTHORITY_COLUMNS) <= set(frame.columns)
    handed = frame.iloc[0].to_dict()
    assert handed["endpoint"] == "reddit/post/comments"
    assert handed["vendor_family"] == "socialcrawl"
    assert handed["channel_family"] == "reddit"

    raw = run_rss_now.build_raw_row(
        handed, "run_wave1_handoff", datetime(2026, 8, 30, 10, tzinfo=UTC)
    )
    assert all(raw[field] == handed[field] for field in WAVE1_AUTHORITY_COLUMNS)
    enriched = run_rss_now.build_enriched_row(raw)
    assert all(enriched[field] == handed[field] for field in WAVE1_AUTHORITY_COLUMNS)
    assert set(WAVE1_AUTHORITY_COLUMNS) <= set(EVIDENCE_COLUMNS_BY_TABLE["raw_content"])
    projected = _projected_evidence_row(raw, "raw_content")
    assert (projected.vendor_family, projected.channel_family) == (
        "socialcrawl",
        "reddit",
    )
    enriched_projected = _projected_evidence_row(enriched, "enriched_content")
    assert (enriched_projected.vendor_family, enriched_projected.channel_family) == (
        "socialcrawl",
        "reddit",
    )
    for table, record in (("raw_content", raw), ("enriched_content", enriched)):
        assert "retained_geo_market" not in record
        projected, ambiguous = _unique_evidence_rows(({**record, "match_count": 1},), table)
        assert not ambiguous
        assert set(projected) == {("za", record["id"])}
        assert projected[("za", record["id"])].channel_family == "reddit"


def _stored_wave1_evidence(**changes):
    row = {
        "id": "stored_comment_1",
        "market": "za",
        "source": "socialcrawl",
        "platform": "youtube",
        "content_type": "comment",
        "endpoint": "youtube/video/comments",
        "vendor_family": "socialcrawl",
        "channel_family": "youtube",
        "source_family": "youtube",
        "source_family_map_version": "channel_family_v2",
        "native_id": "comment_1",
        "url": "https://youtube.invalid/watch?v=video_1&lc=comment_1",
        "geo_method_id": "retained_source_geo_v1",
        "geo_receipt_id": "geo_comment_1",
        "collected_at": datetime(2026, 9, 6, 0, tzinfo=UTC),
        "match_count": 1,
    }
    row.update(changes)
    return row


@pytest.mark.parametrize("missing", ["geo_method_id", "geo_receipt_id"])
def test_stored_wave1_without_geo_authority_remains_unresolved(missing):
    from src.analysis.open_intelligence.pipeline import _unique_evidence_rows

    projected, ambiguous = _unique_evidence_rows(
        (_stored_wave1_evidence(**{missing: None}),), "enriched_content"
    )
    assert projected == {}
    assert ambiguous == set()


@pytest.mark.parametrize(
    "changes",
    [
        {"market": "us"},
        {"vendor_market": "ng"},
        {"retained_geo_market": "ng"},
        {"retained_geo_market": None},
        {"vendor_family": None},
        {"channel_family": None},
        {"source_family": "news"},
        {"native_id": ""},
        {"url": ""},
    ],
)
def test_stored_wave1_rejects_conflicting_or_incomplete_authority(changes):
    from src.analysis.open_intelligence.pipeline import _unique_evidence_rows

    with pytest.raises(ValueError):
        _unique_evidence_rows((_stored_wave1_evidence(**changes),), "enriched_content")


@pytest.mark.parametrize(
    "changes",
    [
        {"collected_at": datetime(2026, 9, 5, 0, tzinfo=UTC)},
        {"market": "ng", "geo_receipt_id": "geo_comment_ng"},
        {"native_id": "comment_2", "url": "https://youtube.invalid/watch?v=video_1&lc=comment_2"},
    ],
)
def test_stored_wave1_resolves_distinct_requested_ids_without_raw_content_dedup(changes):
    from src.analysis.open_intelligence.pipeline import _unique_evidence_rows

    first = _stored_wave1_evidence()
    second = _stored_wave1_evidence(id="stored_comment_2", **changes)
    projected, ambiguous = _unique_evidence_rows((first, second), "enriched_content")
    assert not ambiguous
    assert set(projected) == {(first["market"], first["id"]), (second["market"], second["id"])}


def test_stored_wave1_duplicate_warehouse_identity_remains_ambiguous():
    from src.analysis.open_intelligence.pipeline import _unique_evidence_rows

    row = _stored_wave1_evidence(match_count=2)
    projected, ambiguous = _unique_evidence_rows((row, dict(row)), "enriched_content")
    assert projected == {}
    assert ambiguous == {("za", "stored_comment_1")}


def test_legacy_raw_row_and_connector_schema_remain_free_of_wave1_authority_fields() -> None:
    from scripts import run_rss_now
    from src.ingestion.connectors.base import RAW_COLUMNS
    from src.ingestion.connectors.socialcrawl import WAVE1_AUTHORITY_COLUMNS

    raw = run_rss_now.build_raw_row(
        {"source": "rss", "platform": "web", "market": "za"},
        "legacy_run",
        datetime(2026, 8, 30, 10, tzinfo=UTC),
    )
    assert not set(WAVE1_AUTHORITY_COLUMNS).intersection(raw)
    assert not set(WAVE1_AUTHORITY_COLUMNS).intersection(RAW_COLUMNS)


def test_wave1_source_value_uses_fixed_input_route_disabled_controls() -> None:
    from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS, SocialCrawlConnector

    rows = (
        {
            "endpoint": "tiktok/song",
            "market": "za",
            "query_term": "culture alpha",
            "native_id": "native_a",
            "url": "https://social.invalid/a",
        },
        {
            "endpoint": "instagram/search/reels",
            "market": "za",
            "query_term": "culture alpha",
            "native_id": "native_b",
            "url": "https://social.invalid/b",
        },
    )
    forward = SocialCrawlConnector.measure_wave1_source_values(rows)
    reverse = SocialCrawlConnector.measure_wave1_source_values(tuple(reversed(rows)))
    assert forward == reverse
    assert tuple(forward) == tuple(f"/v1/{route}" for route in WAVE1_ROUTE_SPECS)
    assert forward["/v1/tiktok/song"].unique_observations == 1
    assert forward["/v1/tiktok/song"].marginal_candidates == 0
    assert forward["/v1/tiktok/song"].marginal_evidence == 1
    assert forward["/v1/tiktok/song"].state == "passed"
    assert forward["/v1/reddit/post/comments"].state == "permanently_rejected"


def test_zero_value_source_lab_row_is_permanent_and_wave1_run_cannot_close(monkeypatch) -> None:
    from src.analysis.open_intelligence import funded_lane
    from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS, WAVE1_STAGE
    from src.analysis.open_intelligence.funded_lane_runtime import (
        FundedLaneActivationBlocked,
        FundedLaneRuntime,
        Wave1PhaseUsage,
    )
    from src.analysis.open_intelligence.source_lab import (
        build_source_performance_rows,
        evaluate_wave1_source_value,
    )
    from src.analysis.open_intelligence.source_lab_persistence import (
        SourceLabBatchInvalid,
        SourceLabRowBatch,
    )
    from src.contracts.open_intelligence import resolve_client_scope
    from src.ingestion.connectors.socialcrawl import SocialCrawlConnector

    from tests.unit.test_source_lab_inventory import snapshots

    catalog, balance = snapshots()
    zero = evaluate_wave1_source_value(
        unique_observations=0,
        marginal_candidates=0,
        marginal_evidence=0,
    )
    source_rows = build_source_performance_rows(
        scope=resolve_client_scope(run_id="wave1_value_run"),
        catalog=catalog,
        balance=balance,
        expectation_version="socialcrawl_catalog_2026_08_27",
        wave1_source_values={"/v1/tiktok/song": zero},
    )
    persisted = SourceLabRowBatch(rows=source_rows)
    measured = next(row for row in persisted.rows if row["route_path"] == "/v1/tiktok/song")
    assert measured["status"] == "permanently_rejected"
    assert measured["rows"] == 0
    assert measured["unique_lift"] == 0.0
    assert measured["kill_test_result"] == "zero_unique_observations"

    now = datetime(2026, 8, 30, 10, tzinfo=UTC)
    ledger_rows = []
    source_value_writes = []
    gate = SimpleNamespace(
        run_allowance=Decimal("100"),
        month_opening_balance=Decimal("250100"),
        current_balance=Decimal("250100"),
        monthly_balance_delta=Decimal("0"),
        monthly_ledger_debit=Decimal("0"),
        monthly_vendor_reported=Decimal("0"),
        attribution_state="complete",
    )
    runtime = FundedLaneRuntime(
        run_id="wave1_value_run",
        execution_id="wave1_value_execution",
        as_of=now,
        gate=gate,
        ledger_writer=ledger_rows.append,
        clock=lambda: now,
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
        source_value_writer=source_value_writes.append,
    )
    for phase in WAVE1_PHASE_CAPS:
        runtime.phase_close(Wave1PhaseUsage(None, phase, 0, 0, 0))
    source_values = SocialCrawlConnector.measure_wave1_source_values(())
    with pytest.raises(FundedLaneActivationBlocked, match="permanently_rejected"):
        runtime.close(
            post_balance=Decimal("250100"),
            recorded_at=now,
            source_value_results=source_values,
        )
    assert sum(row["event_type"] == "phase_close" for row in ledger_rows) == 4
    assert sum(row["event_type"] == "run_close" for row in ledger_rows) == 1
    assert source_value_writes == [source_values]

    forged = list(source_rows)
    index = next(i for i, row in enumerate(forged) if row["route_path"] == "/v1/tiktok/song")
    forged[index] = {**forged[index], "status": "active", "blocking_reason": None}
    with pytest.raises(SourceLabBatchInvalid, match="active Wave 1 source value"):
        SourceLabRowBatch(rows=tuple(forged))


def test_source_value_writer_failure_occurs_after_durable_close_and_retries_without_second_close(
    monkeypatch,
) -> None:
    from src.analysis.open_intelligence import funded_lane
    from src.analysis.open_intelligence.funded_lane import WAVE1_PHASE_CAPS, WAVE1_STAGE
    from src.analysis.open_intelligence.funded_lane_runtime import (
        FundedLaneActivationBlocked,
        FundedLaneRuntime,
        Wave1PhaseUsage,
    )
    from src.ingestion.connectors.socialcrawl import SocialCrawlConnector

    now = datetime(2026, 8, 30, 10, tzinfo=UTC)
    events = []
    fail = {"value": True}

    def ledger_writer(row):
        events.append(("ledger", row["event_type"]))

    def source_writer(values):
        events.append(("source", tuple(values)))
        if fail["value"]:
            raise RuntimeError("source write failed")

    gate = SimpleNamespace(
        run_allowance=Decimal("100"),
        month_opening_balance=Decimal("250100"),
        current_balance=Decimal("250100"),
        monthly_balance_delta=Decimal("0"),
        monthly_ledger_debit=Decimal("0"),
        monthly_vendor_reported=Decimal("0"),
        attribution_state="complete",
    )
    runtime = FundedLaneRuntime(
        run_id="run_writer_retry",
        execution_id="execution_writer_retry",
        as_of=now,
        gate=gate,
        ledger_writer=ledger_writer,
        clock=lambda: now,
        stage_name=WAVE1_STAGE,
        execution_capability=_wave1_capability(),
        source_value_writer=source_writer,
    )
    for phase in WAVE1_PHASE_CAPS:
        runtime.phase_close(Wave1PhaseUsage(None, phase, 0, 0, 0))
    values = SocialCrawlConnector.measure_wave1_source_values(())

    with pytest.raises(FundedLaneActivationBlocked, match="persistence failed"):
        runtime.close(
            post_balance=Decimal("250100"),
            recorded_at=now,
            source_value_results=values,
        )
    assert events.index(("ledger", "run_close")) < next(
        index for index, event in enumerate(events) if event[0] == "source"
    )
    assert sum(event == ("ledger", "run_close") for event in events) == 1

    fail["value"] = False
    assert runtime.retry_source_value_persistence() is True
    assert runtime.retry_source_value_persistence() is False
    with pytest.raises(ValueError, match="already closed"):
        runtime.close(
            post_balance=Decimal("250100"),
            recorded_at=now,
            source_value_results=values,
        )
    assert sum(event == ("ledger", "run_close") for event in events) == 1


def test_production_wave1_close_evaluates_measured_rows_and_zero_downstream_lift(monkeypatch):
    from scripts import run_rss_now
    from src.analysis.open_intelligence import funded_lane_runtime
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    captured = {}

    class Runtime:
        stage_name = "stage_1_wave_1"

        def close(self, **kwargs):
            captured.update(kwargs)
            return "closed"

    monkeypatch.setattr(
        funded_lane_runtime,
        "read_funded_balance",
        lambda **_kwargs: Decimal("250100"),
    )
    measured = {
        "tiktok/song": evaluate_wave1_source_value(
            unique_observations=3,
            marginal_candidates=0,
            marginal_evidence=2,
        )
    }
    monkeypatch.setattr(
        run_rss_now.SocialCrawlConnector,
        "wave1_source_values",
        classmethod(lambda cls: measured),
        raising=False,
    )
    gdelt_proof = object()
    assert (
        run_rss_now._close_funded_socialcrawl(
            Runtime(),
            datetime(2026, 8, 30, 10, tzinfo=UTC),
            gdelt_runtime_proof=gdelt_proof,
        )
        == "closed"
    )
    assert captured["source_value_results"] == measured
    assert captured["gdelt_runtime_proof"] is gdelt_proof


def test_wave1_source_value_writer_ledgers_measured_values_in_the_funded_dataset(monkeypatch):
    from src.analysis.open_intelligence import funded_lane_runtime
    from src.analysis.open_intelligence.funded_lane import (
        APPROVED_CATALOG_DIGEST,
        APPROVED_METADATA_DIGEST,
    )
    from src.analysis.open_intelligence.funded_lane_runtime import FundedRunCloseReceipt
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    now = datetime(2026, 9, 5, 14, tzinfo=UTC)
    captured = {}

    def persist(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(funded_lane_runtime, "persist_wave1_source_value_rows", persist)
    runtime = SimpleNamespace(
        execution_id="execution_wave1_value",
        run_id="run_wave1_value",
        close_receipt=FundedRunCloseReceipt(
            attribution_state="complete",
            calls=1,
            budget_debit_credits=Decimal("5"),
            vendor_reported_credits=Decimal("5"),
            balance_delta=Decimal("5"),
            attribution_gap_credits=Decimal("0"),
            run_balance_delta=Decimal("5"),
        ),
        close_recorded_at=now,
        close_vendor_overage=False,
        close_verdict="succeeded",
        _source_sha=None,
        gate=SimpleNamespace(
            catalog_digest=APPROVED_CATALOG_DIGEST, metadata_digest=APPROVED_METADATA_DIGEST
        ),
        _clock=lambda: now,
    )
    writer = funded_lane_runtime.make_wave1_source_value_writer(
        client=object(), dataset="trends_v2_staging_funded", runtime=runtime
    )
    values = {
        "/v1/tiktok/song": evaluate_wave1_source_value(
            unique_observations=0,
            marginal_candidates=0,
            marginal_evidence=0,
        )
    }
    writer(values)
    assert captured["project"] == "ogilvy-trends-v2"
    assert captured["dataset"] == "trends_v2_staging_funded"
    (row,) = captured["rows"]
    assert row.route_path == "/v1/tiktok/song"
    assert row.state == "permanently_rejected"
    assert row.close_verdict == "succeeded"
    assert row.source_sha is None
    assert row.catalog_digest == APPROVED_CATALOG_DIGEST


def test_wave1_source_value_writer_refuses_a_client_without_the_funded_identity() -> None:
    from src.analysis.open_intelligence import funded_lane_runtime
    from src.analysis.open_intelligence.funded_lane import (
        APPROVED_CATALOG_DIGEST,
        APPROVED_METADATA_DIGEST,
    )
    from src.analysis.open_intelligence.funded_lane_runtime import FundedRunCloseReceipt
    from src.analysis.open_intelligence.source_lab import evaluate_wave1_source_value

    now = datetime(2026, 9, 5, 14, tzinfo=UTC)
    runtime = SimpleNamespace(
        execution_id="execution_wave1_identity",
        run_id="run_wave1_identity",
        close_receipt=FundedRunCloseReceipt(
            attribution_state="complete",
            calls=1,
            budget_debit_credits=Decimal("5"),
            vendor_reported_credits=Decimal("5"),
            balance_delta=Decimal("5"),
            attribution_gap_credits=Decimal("0"),
            run_balance_delta=Decimal("5"),
        ),
        close_recorded_at=now,
        close_vendor_overage=False,
        close_verdict="succeeded",
        _source_sha="b" * 40,
        gate=SimpleNamespace(
            catalog_digest=APPROVED_CATALOG_DIGEST, metadata_digest=APPROVED_METADATA_DIGEST
        ),
        _clock=lambda: now,
    )
    foreign = SimpleNamespace(
        project="ogilvy-trends-v2",
        location="US",
        _credentials=SimpleNamespace(
            service_account_email="trends-engine-staging@ogilvy-trends-v2.iam.gserviceaccount.com",
            quota_project_id=None,
        ),
        query=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no query")),
    )
    writer = funded_lane_runtime.make_wave1_source_value_writer(
        client=foreign, dataset="trends_v2_staging_funded", runtime=runtime
    )

    with pytest.raises(ValueError, match="exact funded writer"):
        writer(
            {
                "/v1/tiktok/song": evaluate_wave1_source_value(
                    unique_observations=0,
                    marginal_candidates=0,
                    marginal_evidence=0,
                )
            }
        )
