"""Executable source inventory: registry keys, dispatcher surfaces and configuration evidence."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import ClassVar

import pytest
from src.analysis.open_intelligence.source_inventory import (
    ABSENT,
    CONNECTOR_PROFILES,
    LIFECYCLE_STATES,
    ROLLOUT_CONFIG_PATH,
    ROUTE_SWITCHES,
    SOCIALCRAWL_MODULE,
    SOURCE_ESTATE_DOC,
    SOURCES_CONFIG_PATH,
    STATE_APPROVED_UNPROVEN,
    STATE_DEGRADED,
    STATE_EXCLUDED,
    STATE_KNOWN_EMPTY,
    STATE_QUALIFIED_LIVE,
    STATE_UNAVAILABLE,
    WAVE1_SPEC_STATES,
    Switch,
    activation_policy,
    configuration_evidence,
    connector_cap_reads,
    connector_read_defaults,
    connector_read_idioms,
    dispatcher_surfaces,
    executable_inventory,
    ladder_increments,
    pending_quality_stages,
    read_config_path,
    read_config_reading,
    rollout_capability_reconciliation,
    route_activation_request,
    route_evidence,
    source_estate_document,
    staging_source_estate,
    unregistered_connector_modules,
)
from src.analysis.open_intelligence.source_policy import (
    POLICY_MARKETS,
    feature_blockers,
    load_executable_connectors,
    render_inventory_table,
)
from src.ingestion.connectors.ensemble import (
    DEFAULT_ENDPOINTS,
    TIKTOK_COMMENTS_ENDPOINT,
    _ensemble_enabled,
)
from src.ingestion.connectors.socialcrawl import PHASE_ORDER, WAVE1_ROUTE_SPECS
from src.utils.config_loader import load_yaml

ENGINE_DIR = Path(__file__).resolve().parents[2]
ROLLOUT_POLICY = (load_yaml(ROLLOUT_CONFIG_PATH) or {})["policy"]

# Reviewed baseline at c17ce95. A registry change must change this list and the
# profiles together; the inventory itself is derived, never read from here.
REVIEWED_SOURCE_BASELINE = frozenset(
    {
        "rss",
        "apple_music",
        "spotify",
        "bigquery_trends",
        "youtube",
        "youtube_scrape",
        "gdelt",
        "ensemble",
        "reddit",
        "socialcrawl",
        "wikipedia",
        "bluesky",
        "semrush",
        "google_trends_rss",
        "app_charts",
        "audiomack",
        "cloudflare_radar",
        "pulsar",
    }
)


class SingleFetch:
    def fetch(self, **kwargs):
        return None


def fake_config(**overrides):
    config = {
        "rss_feeds": {"za": ["https://a.example/feed"], "ng": [], "ke": ["https://b.example/f"]},
        "socialcrawl": {
            "enabled": True,
            "phases": ["discover", "creators", "search"],
            "markets": {
                "za": {"creator_tiers": ["tier_1"]},
                "ng": {"terms": ["x"]},
                "ke": {"a": 1},
            },
        },
        "apple_music": {"markets": {}},
        "spotify": {"enabled": False},
        "semrush": {"enabled": False},
        "ensemble_budget": {"enabled": False},
        "bigquery_trends": {"active": True, "expected_per_market": {"za": True, "ke": False}},
        "gdelt": {"gdelt_events_enabled": False},
    }
    config.update(overrides)
    return config


def test_inventory_is_derived_without_importing_the_producer():
    # A fresh interpreter, so an earlier suite that imports the producer as a
    # top level module cannot make this assertion pass vacuously.
    probe = (
        "import sys; "
        "from src.analysis.open_intelligence.source_inventory import executable_inventory; "
        "inventory = executable_inventory(); "
        "print(len(inventory), any(name.endswith('run_rss_now') for name in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ENGINE_DIR,
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": str(ENGINE_DIR)},
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    count, producer_loaded = result.stdout.split()
    assert int(count) == len(REVIEWED_SOURCE_BASELINE) * len(POLICY_MARKETS)
    assert producer_loaded == "False"
    inventory = executable_inventory()
    assert set(inventory) == {(s, m) for s in REVIEWED_SOURCE_BASELINE for m in POLICY_MARKETS}
    assert inventory[("rss", "za")] is not inventory[("rss", "ng")]


def test_inventory_follows_the_registry_not_the_reviewed_list():
    registry = tuple(load_executable_connectors())
    inventory = executable_inventory(registry)
    assert {source for source, _ in inventory} == {key for key, _ in registry}
    grown = executable_inventory([*registry, ("new_vendor", SingleFetch)])
    assert set(grown) - set(inventory) == {("new_vendor", m) for m in POLICY_MARKETS}
    assert grown[("new_vendor", "za")] == {"fetch"}


def test_single_fetch_connectors_have_fetch_as_their_only_surface():
    inventory = executable_inventory()
    single = REVIEWED_SOURCE_BASELINE - {"gdelt", "socialcrawl", "ensemble"}
    for source in single:
        for market in POLICY_MARKETS:
            assert inventory[(source, market)] == {"fetch"}, source
    assert inventory[("gdelt", "za")] == {"fetch", "fetch_events"}


def test_socialcrawl_dispatch_expands_to_phases_and_funded_routes():
    surfaces = executable_inventory()[("socialcrawl", "za")]
    assert surfaces == set(PHASE_ORDER) | set(WAVE1_ROUTE_SPECS)
    assert "fetch" not in surfaces
    assert {"creators", "youtube/video/comments", "reddit/post/comments"} <= surfaces
    assert "tiktok/profile" not in surfaces


def test_ensemble_dispatch_expands_to_implemented_endpoint_names_only():
    surfaces = executable_inventory()[("ensemble", "ng")]
    assert {endpoint["name"] for endpoint in DEFAULT_ENDPOINTS} <= surfaces
    assert TIKTOK_COMMENTS_ENDPOINT["name"] in surfaces
    assert "fetch" not in surfaces
    assert all("/" not in surface for surface in surfaces)


def test_dispatcher_surfaces_keep_plain_classes_and_refuse_surfaceless_ones():
    assert dispatcher_surfaces("plain", SingleFetch) == {"fetch"}

    class NoSurface:
        def collect(self):
            return None

    with pytest.raises(ValueError, match="connector_surface_missing"):
        dispatcher_surfaces("plain", NoSurface)


def test_disabled_connectors_keep_their_surfaces_inventoried():
    inventory = executable_inventory()
    for source in ("spotify", "semrush", "pulsar", "audiomack", "bluesky", "reddit"):
        assert inventory[(source, "ke")] == {"fetch"}, source
    assert len(inventory[("ensemble", "ke")]) > 10


def test_every_registry_source_has_a_reviewed_profile_and_no_orphan_profile():
    registry_keys = {key for key, _ in load_executable_connectors()}
    assert set(CONNECTOR_PROFILES) == registry_keys
    assert "brand24" not in CONNECTOR_PROFILES


def test_read_config_path_substitutes_market_and_reports_absence():
    config = fake_config()
    assert read_config_path(config, "socialcrawl.enabled", market="za") is True
    assert read_config_path(config, "rss_feeds.{market}", market="ng") == []
    assert read_config_path(config, "bluesky", market="za") is None
    assert read_config_path(config, "socialcrawl.markets.{market}.tiers", market="ke") is None


def test_configuration_evidence_reconciles_switches_into_lifecycle_states():
    evidence = configuration_evidence(fake_config(), connectors=load_executable_connectors())
    assert evidence[("rss", "za")].state == STATE_APPROVED_UNPROVEN
    assert evidence[("rss", "ng")].state == STATE_KNOWN_EMPTY
    assert evidence[("socialcrawl", "ke")].state == STATE_APPROVED_UNPROVEN
    assert evidence[("spotify", "za")].state == STATE_UNAVAILABLE
    assert evidence[("semrush", "za")].state == STATE_EXCLUDED
    assert evidence[("bigquery_trends", "za")].state == STATE_APPROVED_UNPROVEN
    assert evidence[("bigquery_trends", "ke")].state == STATE_KNOWN_EMPTY
    assert evidence[("bluesky", "za")].state == STATE_UNAVAILABLE
    assert evidence[("bluesky", "za")].switch_value is None
    assert evidence[("ensemble", "za")].state == STATE_UNAVAILABLE
    assert {item.state for item in evidence.values()} <= LIFECYCLE_STATES
    assert STATE_QUALIFIED_LIVE not in {item.state for item in evidence.values()}
    assert all(item.next_owner_action for item in evidence.values())


def test_route_evidence_applies_route_switches_under_the_source_state():
    evidence = route_evidence(fake_config(), connectors=load_executable_connectors())
    assert evidence[("socialcrawl", "za", "discover")]["state"] == STATE_APPROVED_UNPROVEN
    assert evidence[("socialcrawl", "za", "news")]["state"] == STATE_EXCLUDED
    assert evidence[("socialcrawl", "za", "news")]["evidence"]["switch"] == "socialcrawl.phases"
    wave1 = evidence[("socialcrawl", "za", "youtube/video/comments")]
    assert wave1["state"] == STATE_APPROVED_UNPROVEN
    assert wave1["platform"] == "youtube"
    assert wave1["budget_stage"] == "stage_1_wave_1"
    assert wave1["evidence"]["route_state"] == "blocked_fixture_unapproved"
    assert evidence[("gdelt", "za", "fetch_events")]["state"] == STATE_EXCLUDED
    assert evidence[("gdelt", "za", "fetch")]["state"] == STATE_APPROVED_UNPROVEN
    assert evidence[("ensemble", "za", "tiktok_comments")]["state"] == STATE_UNAVAILABLE
    assert evidence[("spotify", "ng", "fetch")]["market"] == "ng"


def test_shipped_configuration_states_match_the_current_estate():
    evidence = configuration_evidence()
    states = {source: evidence[(source, "za")].state for source in REVIEWED_SOURCE_BASELINE}
    assert states["ensemble"] == STATE_UNAVAILABLE
    assert states["reddit"] == STATE_UNAVAILABLE
    assert states["spotify"] == STATE_UNAVAILABLE
    assert states["audiomack"] == STATE_UNAVAILABLE
    assert states["bluesky"] == STATE_UNAVAILABLE
    assert states["semrush"] == STATE_EXCLUDED
    assert states["pulsar"] == STATE_EXCLUDED
    assert states["socialcrawl"] == STATE_APPROVED_UNPROVEN
    assert evidence[("bigquery_trends", "ke")].state == STATE_KNOWN_EMPTY
    assert evidence[("bigquery_trends", "za")].state == STATE_APPROVED_UNPROVEN
    assert not any(item.state == STATE_QUALIFIED_LIVE for item in evidence.values())


def test_staging_source_estate_renders_every_source_market_with_surfaces_and_state():
    rows, digest = staging_source_estate()
    assert len(rows) == len(REVIEWED_SOURCE_BASELINE) * len(POLICY_MARKETS)
    assert len(digest) == 64
    assert staging_source_estate()[1] == digest
    table = render_inventory_table(rows)
    lines = table.splitlines()
    assert lines[0] == "| source | market | surfaces | state |"
    assert len(lines) == len(rows) + 2
    assert any(
        line.startswith("| socialcrawl | za | ") and "reddit/post/comments" in line
        for line in lines
    )
    assert not any(row["state"] == STATE_QUALIFIED_LIVE for row in rows)
    assert all(not route["live"] for row in rows for route in row["routes"])


def test_switches_read_each_gate_the_way_its_connector_reads_it():
    connectors = load_executable_connectors()
    config = fake_config(
        gdelt={"gdelt_events_enabled": True},
        ensemble={"enabled": True},
        ensemble_budget={"enabled": False},
        apple_music={"active": True},
        socialcrawl={"enabled": True},
        semrush={"enabled": 1},
        bluesky={"enabled": True},
        bigquery_trends={"days": 7},
    )
    evidence = configuration_evidence(config, connectors=connectors)
    assert evidence[("gdelt", "za")].state == STATE_APPROVED_UNPROVEN
    assert evidence[("gdelt", "za")].switch == "gdelt.active"
    assert evidence[("ensemble", "za")].state == STATE_APPROVED_UNPROVEN
    assert evidence[("ensemble", "za")].switch == "ensemble.enabled"
    assert evidence[("apple_music", "ng")].state == STATE_APPROVED_UNPROVEN
    assert evidence[("socialcrawl", "ke")].state == STATE_APPROVED_UNPROVEN
    assert evidence[("semrush", "za")].state == STATE_EXCLUDED
    assert evidence[("bluesky", "za")].state == STATE_APPROVED_UNPROVEN
    assert evidence[("bluesky", "za")].switch == "bluesky.enabled"
    assert evidence[("bigquery_trends", "za")].state == STATE_APPROVED_UNPROVEN
    off = configuration_evidence(
        fake_config(gdelt={"active": False}, ensemble_budget={"enabled": False}),
        connectors=connectors,
    )
    assert off[("gdelt", "ke")].state == STATE_EXCLUDED
    assert off[("gdelt", "ke")].switch == "gdelt.active"
    assert off[("ensemble", "ke")].state == STATE_UNAVAILABLE
    assert off[("ensemble", "ke")].switch == "ensemble_budget.enabled"
    assert off[("apple_music", "ke")].state == STATE_APPROVED_UNPROVEN


def test_switch_modes_judge_a_present_reading_as_each_mode_declares():
    # The connector idioms themselves are held against the connector trees by
    # test_every_declared_switch_default_is_the_connectors_own_default; this one
    # is the unit test of the four modes over a synthetic dict, which is what it
    # always was and what its name now says.
    settings = {"a": {"flag": 1, "off": False, "empty": {}, "true": True}}
    truthy = Switch(("a.flag",))
    assert truthy.read(settings, "za") == ("a.flag", 1, True)
    assert Switch(("a.missing",), absent_on=True).read(settings, "za") == ("a.missing", None, True)
    assert Switch(("a.missing",)).read(settings, "za") == ("a.missing", None, False)
    unless_false = Switch(("a.empty",), mode="unless_false")
    assert unless_false.read(settings, "za") == ("a.empty", {}, True)
    assert Switch(("a.off",), mode="unless_false").read(settings, "za") == ("a.off", False, False)
    assert Switch(("a.flag",), mode="true_only").read(settings, "za")[2] is False
    assert Switch(("a.true",), mode="true_only").read(settings, "za")[2] is True
    precedence = Switch(("a.missing", "a.off", "a.true"), absent_on=True)
    assert precedence.read(settings, "za") == ("a.off", False, False)
    with pytest.raises(ValueError, match="switch_mode_unknown"):
        Switch(("a.flag",), mode="sometimes")
    # A gate that names no path reads nothing and would answer absent_on to every
    # question, which is a declaration that says nothing, not a default.
    with pytest.raises(ValueError, match="switch_paths_missing"):
        Switch(())
    with pytest.raises(ValueError, match="switch_paths_missing"):
        Switch((), absent_on=True)


def test_policy_digest_ignores_operational_config_edits_and_moves_with_state():
    shipped = load_yaml(SOURCES_CONFIG_PATH)
    _rows, digest = staging_source_estate()
    edited = copy.deepcopy(shipped)
    edited["rss_feeds"]["za"].append({"name": "Extra", "url": "https://extra.example/feed"})
    edited["youtube_queries"]["za"]["scrape_max_results"] = 999
    edited["gdelt"]["days"] = 9
    assert staging_source_estate(config=edited)[1] == digest
    flipped = copy.deepcopy(shipped)
    flipped["semrush"]["enabled"] = True
    assert staging_source_estate(config=flipped)[1] != digest


def test_connector_cap_reads_are_taken_from_the_connector_source_not_a_catalog():
    reads = connector_cap_reads(SOCIALCRAWL_MODULE)
    assert {"geo_verify", "tiktok_search_pages", "youtube_search_pages", "reddit_pages"} <= reads
    assert {"subreddits", "creators_per_platform", "twitter_handles"} <= reads
    assert "tiktok_comment_posts" not in reads
    # Response fields are not configuration. A reader that counted every literal
    # ``.get("name")`` in the module would call a vendor payload key a capability.
    assert not {"data", "credits_used", "engagement", "username"} & reads
    assert "instagram_comment_posts" not in reads
    assert "audience_tier_1_weekly" not in reads
    assert "extra_platforms" not in reads


def test_declared_rollout_capabilities_match_what_the_connector_reads():
    findings = rollout_capability_reconciliation()
    assert set(findings) == {"pagination", "comment_surfaces", "audience", "extra_platforms"}
    assert all(item.agrees for item in findings.values()), {
        name: (item.declared, item.shipped_in_code) for name, item in findings.items()
    }
    pagination = findings["pagination"]
    assert (pagination.declared, pagination.shipped_in_code) == (True, True)
    assert pagination.missing_config_keys == ()
    assert set(pagination.stages) == {"s1_tiktok_page2", "s2_youtube_page2", "s3_reddit_depth"}
    comments = findings["comment_surfaces"]
    assert (comments.declared, comments.shipped_in_code) == (False, False)
    assert comments.missing_config_keys == (
        "socialcrawl.caps.instagram_comment_posts",
        "socialcrawl.caps.tiktok_comment_posts",
    )


def test_a_capability_declared_shipped_while_the_code_ignores_its_keys_is_a_disagreement():
    rollout = {
        "policy": ROLLOUT_POLICY,
        "capabilities": {"comment_surfaces": True},
        "stages": [
            {
                "id": "s4_tiktok_comments",
                "requires_capability": "comment_surfaces",
                "config_delta": {"socialcrawl.caps.tiktok_comment_posts": 10},
                "credits_per_day": 30,
                "expect_rows_per_day": 350,
            }
        ],
    }
    findings = rollout_capability_reconciliation(rollout)
    assert findings["comment_surfaces"].declared is True
    assert findings["comment_surfaces"].shipped_in_code is False
    assert findings["comment_surfaces"].agrees is False


def test_ladder_increments_read_quality_and_volume_from_the_expected_rows():
    increments = {item.stage_id: item for item in ladder_increments()}
    geo = increments["s5_geo_full_watchlist"]
    assert (geo.volume_increment, geo.capability_shipped, geo.credits_per_day) == (False, True, 12)
    assert geo.config_keys == ("socialcrawl.caps.geo_verify",)
    comments = increments["s4_tiktok_comments"]
    assert (comments.volume_increment, comments.capability_shipped) == (True, False)
    pagination = increments["s1_tiktok_page2"]
    assert (pagination.volume_increment, pagination.capability_shipped) == (True, True)
    assert pending_quality_stages(ladder_increments(), activated=()) == (
        "s5_geo_full_watchlist",
        "s6_demographics_weekly",
    )
    assert pending_quality_stages(ladder_increments(), activated=("s5_geo_full_watchlist",)) == (
        "s6_demographics_weekly",
    )


def test_activation_policy_comes_from_the_rollout_file_and_refuses_a_missing_control():
    policy = activation_policy()
    assert policy.min_row_integrity_pct == 90
    assert policy.clean_crons_required == 3
    assert policy.burn_overrun_pct == 25
    assert policy.max_increments_open == 1
    assert policy.quality_before_volume is True
    for missing in ("min_row_integrity_pct", "clean_crons_required", "quality_before_volume"):
        thinned = {key: value for key, value in ROLLOUT_POLICY.items() if key != missing}
        with pytest.raises(ValueError, match="activation_policy_missing"):
            activation_policy({"policy": thinned})


def test_the_shipped_ladder_holds_pagination_behind_the_quality_increments():
    from src.analysis.open_intelligence.source_policy import (
        RouteActivationRequest,
        RouteProbe,
        evaluate_route_activation,
    )

    increments = {item.stage_id: item for item in ladder_increments()}
    pagination = increments["s1_tiktok_page2"]
    probe = RouteProbe(
        route="tiktok/search/top",
        retained_reference=f"gs://staging-probes/{'c' * 64}.json",
        payload_digest="c" * 64,
        normalizer_digest="c" * 64,
        observations=30,
        integrity_observations=30,
    )

    class Gate:
        allowed = True
        reasons = ()
        current_balance = 19_607
        run_allowance = 100

    decision = evaluate_route_activation(
        RouteActivationRequest(
            route="tiktok/search/top",
            stage_id=pagination.stage_id,
            market="za",
            route_state="approved_unproven",
            increment_credits=pagination.credits_per_day,
            volume_increment=pagination.volume_increment,
            pending_quality_stages=pending_quality_stages(ladder_increments(), activated=()),
            increments_open=0,
            capability_shipped=pagination.capability_shipped,
            clean_run_ids=("2026-09-16", "2026-09-17", "2026-09-18"),
            burn_overrun_pct=0,
            probe=probe,
            funded_gate=Gate(),
            policy=activation_policy(),
        )
    )
    assert decision.activate is False
    assert decision.reasons == ("quality_increment_pending",)


def youtube_config(**markets):
    # Every promised part on, and carrying what the connector needs to read for
    # each one: the flags alone are a promise, the handles and the per run video
    # count are what the connector actually reads once the flag is on.
    block = {
        market: {
            "queries": ["a"],
            "scrape_enabled": True,
            "trending_enabled": True,
            "comment_threads_enabled": True,
            "comment_videos_per_run": 5,
            "playlist_items_enabled": True,
            "playlist_creator_handles": ["@creator"],
        }
        for market in POLICY_MARKETS
    }
    for market, overrides in markets.items():
        block[market].update(overrides)
    return fake_config(youtube_queries=block)


def test_a_configured_source_with_a_sub_feature_switched_off_is_degraded():
    evidence = configuration_evidence(
        youtube_config(za={"comment_threads_enabled": False}),
        connectors=load_executable_connectors(),
    )
    degraded = evidence[("youtube", "za")]
    assert degraded.state == STATE_DEGRADED
    assert degraded.switch == "youtube_queries.za.comment_threads_enabled"
    assert degraded.switch_value is False
    assert "part" in degraded.reason or "degraded" in degraded.reason.lower()
    assert degraded.next_owner_action
    assert evidence[("youtube", "ng")].state == STATE_APPROVED_UNPROVEN
    assert evidence[("youtube_scrape", "za")].state == STATE_APPROVED_UNPROVEN


def test_a_source_switched_off_is_not_reported_as_degraded():
    evidence = configuration_evidence(
        fake_config(youtube_queries={"za": {}}), connectors=load_executable_connectors()
    )
    assert evidence[("youtube", "za")].state == STATE_EXCLUDED
    assert evidence[("youtube", "ke")].state == STATE_EXCLUDED


def test_the_six_lifecycle_states_are_recorded_separately():
    config = youtube_config(za={"playlist_items_enabled": False})
    evidence = configuration_evidence(config, connectors=load_executable_connectors())
    observed = {item.state for item in evidence.values()}
    assert observed == {
        STATE_UNAVAILABLE,
        STATE_EXCLUDED,
        STATE_KNOWN_EMPTY,
        STATE_DEGRADED,
        STATE_APPROVED_UNPROVEN,
    }
    assert STATE_QUALIFIED_LIVE in LIFECYCLE_STATES
    assert STATE_QUALIFIED_LIVE not in observed


def test_no_shipped_source_is_degraded_today_and_the_youtube_parts_all_read_on():
    shipped = load_yaml(SOURCES_CONFIG_PATH)
    evidence = configuration_evidence()
    assert not [item for item in evidence.values() if item.state == STATE_DEGRADED]
    for market in POLICY_MARKETS:
        block = shipped["youtube_queries"][market]
        assert block["trending_enabled"] is True, market
        assert block["comment_threads_enabled"] is True, market
        assert block["playlist_items_enabled"] is True, market


def test_promised_feature_blockers_name_the_unproven_and_unserved_features():
    rows, _digest = staging_source_estate()
    blockers = {
        (item.feature, item.market): item
        for item in feature_blockers(rows, as_of=date(2026, 9, 18))
    }
    assert all(market in POLICY_MARKETS for _feature, market in blockers)
    music = blockers[("music_charts", "za")]
    assert music.providers == ("apple_music", "spotify")
    assert music.reason == "unproven"
    community = blockers[("community_discussion", "za")]
    assert community.providers == ("reddit",)
    assert community.reason == "no_provider"
    social = blockers[("social_posts_comments_creators", "ke")]
    assert social.providers == ("ensemble", "socialcrawl")
    assert social.reason == "unproven"
    assert {item.feature for item in blockers.values() if item.required} >= {
        "news_discovery",
        "social_posts_comments_creators",
    }


def test_the_source_estate_document_is_generated_and_cannot_drift():
    # The committed docs/operations/source-estate.md is outside the engine build
    # context, so the byte comparison with it runs in
    # tests/repository/test_source_estate_document.py over the checkout. This
    # test checks the generated content and reads no file.
    document = source_estate_document()
    assert SOURCE_ESTATE_DOC.parts[-3:] == ("docs", "operations", "source-estate.md")
    rows, digest = staging_source_estate()
    assert digest in document
    assert "| source | market | surfaces | state |" in document
    assert document.count("\n| ") >= len(rows)
    for state in (STATE_UNAVAILABLE, STATE_EXCLUDED, STATE_APPROVED_UNPROVEN, STATE_KNOWN_EMPTY):
        assert state in document
    assert "community_discussion" in document
    assert "comment_surfaces" in document
    assert "qualified_live" in document


# -- hostile review closure ------------------------------------------------


def test_an_absent_sub_feature_flag_reads_as_off_where_the_connector_defaults_off():
    # youtube.py reads each of these with a default of False, so a market block
    # that names none of them runs with every promised part switched off. The
    # estate must read that as the degradation it is, not as a full feature.
    config = youtube_config()
    for market in POLICY_MARKETS:
        del config["youtube_queries"][market]["comment_threads_enabled"]
    evidence = configuration_evidence(config, connectors=load_executable_connectors())
    for market in POLICY_MARKETS:
        item = evidence[("youtube", market)]
        assert item.state == STATE_DEGRADED, market
        assert item.switch == f"youtube_queries.{market}.comment_threads_enabled"
        assert item.switch_value is None

    stripped = youtube_config()
    for market in POLICY_MARKETS:
        for flag in (
            "trending_enabled",
            "comment_threads_enabled",
            "playlist_items_enabled",
        ):
            del stripped["youtube_queries"][market][flag]
    bare = configuration_evidence(stripped, connectors=load_executable_connectors())
    assert {bare[("youtube", market)].state for market in POLICY_MARKETS} == {STATE_DEGRADED}


def test_a_part_switched_on_with_nothing_for_the_connector_to_read_is_degraded():
    # The connector honours the flag and then reads an empty handle list or a
    # zero video count, so the promised part produces nothing while the flag
    # says it is on.
    empty_handles = youtube_config(za={"playlist_creator_handles": []})
    evidence = configuration_evidence(empty_handles, connectors=load_executable_connectors())
    assert evidence[("youtube", "za")].state == STATE_DEGRADED
    assert evidence[("youtube", "za")].switch == "youtube_queries.za.playlist_creator_handles"
    no_videos = youtube_config(ng={"comment_videos_per_run": 0})
    evidence = configuration_evidence(no_videos, connectors=load_executable_connectors())
    assert evidence[("youtube", "ng")].state == STATE_DEGRADED
    assert evidence[("youtube", "ng")].switch == "youtube_queries.ng.comment_videos_per_run"
    assert evidence[("youtube", "ke")].state == STATE_APPROVED_UNPROVEN


def test_the_estate_reads_a_switch_the_same_way_the_watchdog_reads_it():
    # scripts/engine_pulse.py raises playlist_items_disabled when the flag is
    # absent on any market. Two readers of one flag may not disagree, and the
    # ABSENT case, the only one the first pass covered, is the one case where
    # they agreed anyway: `false` and `FALSE` in quotes read off in the estate and
    # ON in the watchdog, and `no` raised in the estate and read ON in the
    # watchdog. Both now read through source_inventory.flag_reads_on, so the
    # readings below are one reading and not two.
    from scripts.engine_pulse import _flag_state_from_sources

    connectors = load_executable_connectors()
    config = youtube_config()
    for market in POLICY_MARKETS:
        del config["youtube_queries"][market]["playlist_items_enabled"]
    assert _flag_state_from_sources(config)["playlist_items_disabled"] is True
    evidence = configuration_evidence(config, connectors=connectors)
    assert {evidence[("youtube", market)].state for market in POLICY_MARKETS} == {STATE_DEGRADED}

    for reading, gate_fires in (
        ("false", True),
        ("FALSE", True),
        ("true", False),
        (False, True),
        (None, True),
        (True, False),
    ):
        one_market = youtube_config(za={"playlist_items_enabled": reading})
        assert _flag_state_from_sources(one_market)["playlist_items_disabled"] is gate_fires, (
            reading
        )
        state = configuration_evidence(one_market, connectors=connectors)[("youtube", "za")].state
        assert (state == STATE_DEGRADED) is gate_fires, reading

    # Text that spells no boolean is refused by both rather than guessed at by
    # either, which is what "one reader" has to mean when the value is unreadable.
    unreadable = youtube_config(za={"playlist_items_enabled": "no"})
    with pytest.raises(ValueError, match="switch_value_unreadable"):
        _flag_state_from_sources(unreadable)
    with pytest.raises(ValueError, match="switch_value_unreadable"):
        configuration_evidence(unreadable, connectors=connectors)


def test_a_quoted_boolean_is_refused_rather_than_read_as_truthy():
    connectors = load_executable_connectors()
    for value in ("no", "yes", "0", "off", "disabled", ""):
        with pytest.raises(ValueError, match="switch_value_unreadable"):
            configuration_evidence(fake_config(reddit={"enabled": value}), connectors=connectors)
    with pytest.raises(ValueError, match="switch_value_unreadable"):
        configuration_evidence(
            youtube_config(za={"trending_enabled": "maybe"}), connectors=connectors
        )
    # An unambiguous boolean in text is read as the boolean it spells, so a
    # quoted false switches off and can never promote or suppress.
    spelled = configuration_evidence(
        fake_config(wikipedia={"enabled": "true"}, reddit={"enabled": "False"}),
        connectors=connectors,
    )
    assert spelled[("wikipedia", "za")].state == STATE_APPROVED_UNPROVEN
    assert spelled[("reddit", "za")].state == STATE_UNAVAILABLE
    quoted_partial = configuration_evidence(
        youtube_config(za={"trending_enabled": "false"}), connectors=connectors
    )
    assert quoted_partial[("youtube", "za")].state == STATE_DEGRADED


def test_deleting_a_cancelled_vendors_configuration_never_promotes_it():
    connectors = load_executable_connectors()
    tidied = fake_config()
    tidied.pop("ensemble_budget")
    assert "ensemble" not in tidied
    evidence = configuration_evidence(tidied, connectors=connectors)
    for market in POLICY_MARKETS:
        item = evidence[("ensemble", market)]
        assert item.state == STATE_UNAVAILABLE, market
        assert item.condition == "unconfigured"
        assert "ensemble" in item.reason
    routes = route_evidence(tidied, connectors=connectors)
    ensemble_routes = [
        record for (source, _market, _route), record in routes.items() if source == "ensemble"
    ]
    assert len(ensemble_routes) > 20
    assert {record["state"] for record in ensemble_routes} == {STATE_UNAVAILABLE}


def test_a_source_with_no_configuration_block_at_all_is_unconfigured_not_available():
    connectors = load_executable_connectors()
    present = configuration_evidence(fake_config(), connectors=connectors)
    # apple_music.active is absent inside a present apple_music block, which is
    # the connector's own default and reads on.
    assert present[("apple_music", "za")].state == STATE_APPROVED_UNPROVEN
    assert present[("apple_music", "za")].condition == "switch_on_unproven"
    deleted = fake_config()
    deleted.pop("apple_music")
    absent = configuration_evidence(deleted, connectors=connectors)
    assert absent[("apple_music", "za")].state == STATE_UNAVAILABLE
    assert absent[("apple_music", "za")].condition == "unconfigured"
    assert absent[("bluesky", "za")].condition == "unconfigured"


def test_the_four_unavailable_conditions_stay_distinct_in_the_record():
    evidence = configuration_evidence()
    conditions = {
        source: evidence[(source, "za")].condition
        for source in ("spotify", "audiomack", "bluesky", "ensemble", "reddit")
    }
    assert conditions == {
        "spotify": "vendor_access_dormant",
        "audiomack": "credentials_pending",
        "bluesky": "unconfigured",
        "ensemble": "vendor_account_cancelled",
        "reddit": "vendor_account_cancelled",
    }
    assert len(set(conditions.values())) == 4
    document = source_estate_document()
    for condition in set(conditions.values()):
        assert condition in document, condition


def test_the_document_names_every_connector_module_the_registry_does_not_run():
    absent = unregistered_connector_modules()
    assert "brand24" in absent
    document = source_estate_document()
    for name in absent:
        assert name in document, name
    assert "brand24" not in {key for key, _ in load_executable_connectors()}


def document_section(document, heading):
    lines = document.splitlines()
    start = lines.index(f"## {heading}")
    rest = list(lines[start + 1 :])
    end = next((index for index, line in enumerate(rest) if line.startswith("## ")), len(rest))
    return [line for line in rest[:end] if line.startswith("|")][2:]


def test_the_document_route_table_reconciles_with_the_state_counts():
    document = source_estate_document()
    rows, _digest = staging_source_estate()
    counted = {}
    for row in rows:
        for route in row["routes"]:
            counted[route["state"]] = counted.get(route["state"], 0) + 1
    printed = {}
    route_lines = document_section(document, "Routes")
    for line in route_lines:
        state = line.rstrip(" |").rsplit("|", 1)[-1].strip()
        printed[state] = printed.get(state, 0) + 1
    assert printed == counted
    legend = {}
    for line in document_section(document, "States"):
        cells = [cell.strip() for cell in line.strip("| ").split("|")]
        legend[cells[0]] = int(cells[-1])
    assert {state: count for state, count in legend.items() if count} == counted
    # A reader who counts the states table can find every route it counted.
    assert len(route_lines) == sum(counted.values())
    assert len(document_section(document, "Estate")) == len(rows)


def test_the_activation_controls_read_their_own_keys_not_a_neighbouring_one():
    policy = activation_policy()
    shipped = load_yaml(ROLLOUT_CONFIG_PATH)["policy"]
    assert policy.max_increments_open == shipped["max_increments_open"]
    assert policy.min_probe_observations == shipped["min_probe_observations"]
    for missing in ("max_increments_open", "min_probe_observations"):
        thinned = {key: value for key, value in shipped.items() if key != missing}
        with pytest.raises(ValueError, match=f"activation_policy_missing:{missing}"):
            activation_policy({"policy": thinned})
    # max_stages_per_day is a different quantity: one change a day, not one
    # increment open at a time. Reading it as the increment control would make
    # the two move together, so they are read separately.
    renamed = {**shipped, "max_stages_per_day": 9}
    assert (
        activation_policy({"policy": renamed}).max_increments_open == shipped["max_increments_open"]
    )


def test_every_ladder_increment_names_the_vendor_route_it_opens_or_refuses():
    increments = {item.stage_id: item for item in ladder_increments()}
    assert increments["s1_tiktok_page2"].activation_route == "tiktok/search/top"
    assert increments["s5_geo_full_watchlist"].activation_route == "tiktok/profile/region"
    undeclared = [item.stage_id for item in increments.values() if item.activation_route is None]
    assert "s7_extra_platforms" in undeclared
    request = route_activation_request(
        increments["s7_extra_platforms"],
        market="za",
        route_state=STATE_APPROVED_UNPROVEN,
        clean_run_ids=("2026-09-16", "2026-09-17", "2026-09-18"),
        burn_overrun_pct=0,
        increments_open=0,
        probe=None,
        funded_gate=None,
        activated=(),
    )
    assert request is None


def test_the_activation_request_is_derived_rather_than_asserted_by_its_caller():
    from src.analysis.open_intelligence.source_policy import RouteProbe, evaluate_route_activation

    increments = {item.stage_id: item for item in ladder_increments()}
    pagination = increments["s1_tiktok_page2"]
    digest = "d" * 64
    probe = RouteProbe(
        route="tiktok/search/top",
        retained_reference=f"gs://staging-probes/{digest}.json",
        payload_digest=digest,
        normalizer_digest=digest,
        observations=30,
        integrity_observations=30,
    )

    class Gate:
        allowed = True
        reasons = ()
        current_balance = 19_607
        run_allowance = 100

    request = route_activation_request(
        pagination,
        market="za",
        route_state=STATE_APPROVED_UNPROVEN,
        clean_run_ids=("2026-09-16", "2026-09-17", "2026-09-18"),
        burn_overrun_pct=0,
        increments_open=0,
        probe=probe,
        funded_gate=Gate(),
        activated=(),
    )
    assert request.route == "tiktok/search/top"
    assert request.capability_shipped is True
    unshipped = route_activation_request(
        increments["s4_tiktok_comments"],
        market="za",
        route_state=STATE_APPROVED_UNPROVEN,
        clean_run_ids=("2026-09-16", "2026-09-17", "2026-09-18"),
        burn_overrun_pct=0,
        increments_open=0,
        probe=None,
        funded_gate=Gate(),
        activated=(),
    )
    # comment_surfaces is declared false and the connector reads neither of its
    # keys, so the request carries that whatever a caller would have asserted.
    assert unshipped.capability_shipped is False
    assert "capability_not_shipped" in evaluate_route_activation(unshipped).reasons
    assert request.volume_increment is True
    assert request.increment_credits == pagination.credits_per_day
    # Pinned, not read back from the producer under test: comparing the request
    # against a fresh call to pending_quality_stages proved only that the function
    # is deterministic. These are the shipped ladder's unactivated quality stages,
    # in the order the file declares them, so a stage that stops being a quality
    # stage moves this line rather than moving both sides of it together.
    assert request.pending_quality_stages == ("s5_geo_full_watchlist", "s6_demographics_weekly")
    assert request.pending_quality_stages == pending_quality_stages(
        ladder_increments(), activated=()
    )
    decision = evaluate_route_activation(request)
    assert decision.activate is False
    assert decision.reasons == ("quality_increment_pending",)


# -- second hostile review closure -----------------------------------------


def _connector_module_path(connector_class) -> Path:
    module = sys.modules.get(connector_class.__module__)
    if module is None:
        import importlib

        module = importlib.import_module(connector_class.__module__)
    return Path(module.__file__)


def _declared_switches(profile):
    """Every Switch a profile reads, source level and part level alike."""
    gates = list(profile.switches)
    for part in profile.partials:
        gates.append(part.switch)
        gates.extend(part.inputs)
    return gates


def _checkable_paths(key, profile):
    """Every switch path of a profile that names that connector's OWN configuration.

    A profile may gate on another source's block, as wikipedia gates on
    semrush.enabled. That key belongs to the other connector's tree, so holding
    it against this one's would be a coincidence of leaf names and not a check.
    """
    gates = _declared_switches(profile)
    gates.extend(gate for (source, _route), gate in ROUTE_SWITCHES.items() if source == key)
    for gate in gates:
        for path in gate.paths:
            root = path.split(".", 1)[0]
            if root in CONNECTOR_PROFILES and root != key:
                continue
            yield gate, path.rsplit(".", 1)[-1]


def test_every_declared_switch_default_is_the_connectors_own_default():
    # The commit that introduced these declarations said each one is held against
    # the connector's own syntax tree. Nothing held them: the defaults were hand
    # written literals with a comment for a justification, so flipping
    # trending_enabled to default True inside youtube.py changed nothing here and
    # broke no test. This is that check. It reads each connector's tree, finds the
    # literal default beside every configuration key the code reads, and holds the
    # declared absent_on against it.
    checked: list[tuple[str, str]] = []
    for key, connector_class in load_executable_connectors():
        profile = CONNECTOR_PROFILES[key]
        defaults = connector_read_defaults(_connector_module_path(connector_class))
        for gate, leaf in _checkable_paths(key, profile):
            if leaf not in defaults:
                continue
            read = defaults[leaf]
            assert len(read) == 1, f"{key}.{leaf} is read with {read}"
            assert gate.absent_on is bool(read[0]), (
                f"{key}.{leaf} declares absent_on={gate.absent_on} and "
                f"{connector_class.__module__} reads it with default {read[0]!r}"
            )
            checked.append((key, leaf))
    # The check has to stay a check: a refactor that stops resolving defaults
    # would otherwise leave it passing over nothing at all.
    assert len(checked) >= 20, checked
    assert ("youtube", "trending_enabled") in checked
    assert ("youtube", "comment_videos_per_run") in checked
    assert ("gdelt", "gdelt_events_enabled") in checked


def test_every_declared_switch_mode_is_the_connectors_own_idiom():
    # A default is half the claim. The other half is the rule the connector
    # applies to what it read, and `not cfg.get("active", True)` and
    # `cfg.get("active", True) is False` share a default while disagreeing about a
    # present null, a zero and an empty list. This check read the disagreement
    # off spotify.py: spotify.active was declared unless_false while the
    # connector reads it with the truthy idiom.
    checked: list[tuple[str, str]] = []
    for key, connector_class in load_executable_connectors():
        idioms = connector_read_idioms(_connector_module_path(connector_class))
        for gate, leaf in _checkable_paths(key, CONNECTOR_PROFILES[key]):
            if leaf not in idioms:
                continue
            assert idioms[leaf] == (gate.mode,), (
                f"{key}.{leaf} declares mode={gate.mode} and "
                f"{connector_class.__module__} reads it as {idioms[leaf]}"
            )
            checked.append((key, leaf))
    assert len(checked) >= 20, checked
    assert ("spotify", "active") in checked
    assert ("youtube", "comment_videos_per_run") in checked
    assert ("semrush", "enabled") in checked
    assert ("gdelt", "active") in checked


def test_connector_read_idioms_reads_the_use_and_not_the_name():
    idioms = connector_read_idioms(
        _connector_module_path(dict(load_executable_connectors())["youtube"])
    )
    assert idioms["trending_enabled"] == ("truthy",)
    assert idioms["comment_videos_per_run"] == ("count",)
    assert idioms["queries"] == ("truthy",)
    spotify = connector_read_idioms(
        _connector_module_path(dict(load_executable_connectors())["spotify"])
    )
    assert spotify["active"] == ("truthy",)
    semrush = connector_read_idioms(
        _connector_module_path(dict(load_executable_connectors())["semrush"])
    )
    assert semrush["enabled"] == ("true_only",)
    gdelt = connector_read_idioms(
        _connector_module_path(dict(load_executable_connectors())["gdelt"])
    )
    assert gdelt["active"] == ("unless_false",)
    # A use this cannot read reports nothing rather than a guess.
    assert "region_code" not in gdelt


def test_connector_read_defaults_reads_literals_constants_and_nothing_else():
    defaults = connector_read_defaults(
        _connector_module_path(dict(load_executable_connectors())["youtube"])
    )
    assert defaults["trending_enabled"] == (False,)
    assert defaults["comment_videos_per_run"] == (5,)
    assert defaults["playlist_items_per_creator"] == (20,)
    assert defaults["queries"] == ([],)
    # A one argument .get is not a default, and a computed default is not one
    # this can hold a declaration to, so neither is reported.
    assert "region_code" not in defaults


def test_a_present_null_is_read_as_the_connector_reads_it_not_as_an_absent_key():
    # ensemble.py reads `"enabled" in block` and then bool(block["enabled"]), so a
    # present null is OFF for the connector. Reading a present null as an absent
    # key left the gate on its absent_on default of on, and one character in
    # sources.yaml carried a cancelled vendor into the client facing document as
    # approved_unproven.
    connectors = load_executable_connectors()
    tidied = fake_config()
    tidied.pop("ensemble_budget")
    nulled = {**tidied, "ensemble": {"enabled": None}}
    assert _ensemble_enabled(nulled) is False
    evidence = configuration_evidence(nulled, connectors=connectors)
    for market in POLICY_MARKETS:
        item = evidence[("ensemble", market)]
        assert item.state == STATE_UNAVAILABLE, market
        assert item.condition == "vendor_account_cancelled"
        assert item.switch == "ensemble.enabled"
        assert item.switch_value is None
    legacy = {**tidied, "ensemble_budget": {"enabled": None}}
    assert _ensemble_enabled(legacy) is False
    assert (
        configuration_evidence(legacy, connectors=connectors)[("ensemble", "za")].state
        == STATE_UNAVAILABLE
    )
    # An ABSENT key still reads the declared default, which is what the connector
    # does when the key is not there at all.
    absent = {**tidied, "ensemble_budget": {}}
    assert _ensemble_enabled(absent) is True
    assert (
        configuration_evidence(absent, connectors=connectors)[("ensemble", "za")].state
        == STATE_APPROVED_UNPROVEN
    )


def test_read_config_reading_tells_an_absent_path_from_a_present_null():
    config = {"a": {"present_null": None, "nested": {"x": 1}}}
    assert read_config_reading(config, "a.present_null", market="za") is None
    assert read_config_reading(config, "a.missing", market="za") is ABSENT
    assert read_config_reading(config, "missing.deep", market="za") is ABSENT
    assert read_config_reading(config, "a.nested.x", market="za") == 1
    # read_config_path keeps its own contract: both read as None for the readers
    # that do not need to tell them apart.
    assert read_config_path(config, "a.present_null", market="za") is None
    assert read_config_path(config, "a.missing", market="za") is None
    with pytest.raises(TypeError, match="absent_reading_has_no_truth_value"):
        bool(ABSENT)


def test_a_present_null_on_a_count_is_the_crash_the_connector_would_take():
    # youtube.py does int(yt_config.get("comment_videos_per_run", DEFAULT)), so a
    # present null is a TypeError in the connector, not the default. Reading it as
    # an absent key printed approved_unproven over a market that cannot run.
    connectors = load_executable_connectors()
    with pytest.raises(ValueError, match="comment_videos_per_run"):
        configuration_evidence(
            youtube_config(za={"comment_videos_per_run": None}), connectors=connectors
        )
    with pytest.raises(ValueError, match="max_calls_per_run"):
        configuration_evidence(
            youtube_config(ng={"max_calls_per_run": "many"}), connectors=connectors
        )


def test_a_count_the_connector_slices_by_is_modelled_as_a_count_not_as_two_cases():
    # The first pass named two blind conditions by hand. Four more of the same
    # shape, including the direct sibling of one that WAS named, read as fully
    # approved. A count the connector slices by is off at zero and below wherever
    # it appears, because the connector honours its flag and fetches nothing.
    connectors = load_executable_connectors()
    blind = {
        "playlist_items_per_creator": 0,
        "comment_videos_per_run": -3,
        "max_calls_per_run": 0,
        "queries": [],
        "playlist_creator_handles": [],
    }
    for key, value in blind.items():
        evidence = configuration_evidence(youtube_config(za={key: value}), connectors=connectors)
        item = evidence[("youtube", "za")]
        assert item.state == STATE_DEGRADED, f"{key}={value!r}"
        assert item.switch == f"youtube_queries.za.{key}", f"{key}={value!r}"
        assert evidence[("youtube", "ke")].state == STATE_APPROVED_UNPROVEN
    # One is still on, so the rule is a rule and not a refusal of everything.
    healthy = configuration_evidence(
        youtube_config(za={"comment_videos_per_run": 1}), connectors=connectors
    )
    assert healthy[("youtube", "za")].state == STATE_APPROVED_UNPROVEN


def test_a_count_switch_reads_counts_and_refuses_what_int_cannot_read():
    settings = {"a": {"n": 3, "zero": 0, "negative": -2, "text": "4", "null": None}}
    assert Switch(("a.n",), mode="count").read(settings, "za") == ("a.n", 3, True)
    assert Switch(("a.zero",), mode="count").read(settings, "za") == ("a.zero", 0, False)
    assert Switch(("a.negative",), mode="count").read(settings, "za")[2] is False
    assert Switch(("a.text",), mode="count").read(settings, "za") == ("a.text", 4, True)
    assert Switch(("a.missing",), mode="count", absent_on=True).read(settings, "za")[2] is True
    # A present null is int(None) in the connector, which raises; the estate
    # raises too, naming the key the connector would have crashed on.
    with pytest.raises(ValueError, match=r"switch_value_unreadable:a\.null"):
        Switch(("a.null",), mode="count").read(settings, "za")


def test_the_spotify_active_flag_is_declared_with_its_connectors_own_idiom():
    # spotify.py reads `not config.get("active", True)`: the truthy idiom with a
    # default of on, not the `is False` idiom the other three "active" flags use.
    # Declared as unless_false it read a present null and a zero as on where the
    # connector reads them off.
    connectors = load_executable_connectors()
    for value in (None, 0, []):
        evidence = configuration_evidence(
            fake_config(spotify={"enabled": True, "active": value}), connectors=connectors
        )
        item = evidence[("spotify", "za")]
        assert item.state == STATE_UNAVAILABLE, value
        assert item.switch == "spotify.active", value
    on = configuration_evidence(
        fake_config(spotify={"enabled": True, "active": True}), connectors=connectors
    )
    assert on[("spotify", "za")].state == STATE_APPROVED_UNPROVEN
    absent = configuration_evidence(fake_config(spotify={"enabled": True}), connectors=connectors)
    assert absent[("spotify", "za")].state == STATE_APPROVED_UNPROVEN


def test_a_route_level_switch_reads_a_quoted_boolean_the_way_a_source_level_one_does():
    # The route level gate called the raw reader and bypassed the quoted boolean
    # rule entirely, so `gdelt_events_enabled: "false"` raised out of
    # source_estate_document with an error that named no key at all.
    connectors = load_executable_connectors()
    for text in ("false", "FALSE", " False "):
        records = route_evidence(
            fake_config(gdelt={"active": True, "gdelt_events_enabled": text}),
            connectors=connectors,
        )
        assert records[("gdelt", "za", "fetch_events")]["state"] == STATE_EXCLUDED, text
        assert records[("gdelt", "za", "fetch")]["state"] == STATE_APPROVED_UNPROVEN, text
    spelled_on = route_evidence(
        fake_config(gdelt={"active": True, "gdelt_events_enabled": "true"}),
        connectors=connectors,
    )
    assert spelled_on[("gdelt", "za", "fetch_events")]["state"] == STATE_APPROVED_UNPROVEN
    # Text that spells no boolean is refused, and the refusal names the key.
    with pytest.raises(ValueError, match=r"switch_value_unreadable:gdelt\.gdelt_events_enabled"):
        route_evidence(
            fake_config(gdelt={"active": True, "gdelt_events_enabled": "maybe"}),
            connectors=connectors,
        )


def test_reads_on_refuses_text_rather_than_guessing_at_it():
    # Nothing reaches this helper with text through Switch any more, because
    # _switch_value has spelled it out or refused it first. The refusal stays, so
    # a future caller that reaches it directly is refused rather than guessed at.
    from src.analysis.open_intelligence.source_inventory import _reads_on

    assert _reads_on(None) is False
    assert _reads_on([]) is False
    assert _reads_on([1]) is True
    with pytest.raises(ValueError, match="switch_value_unreadable"):
        _reads_on("false")


def test_the_wave1_route_state_is_derived_from_the_connectors_own_spec_state():
    # The route state was the literal STATE_APPROVED_UNPROVEN for every Wave 1
    # route whatever the spec said, so the document printed approved_unproven for
    # 24 routes whose own condition column said blocked_fixture_unapproved, and a
    # future spec state of "retired" would still have printed approved_unproven.
    assert WAVE1_SPEC_STATES["blocked_fixture_unapproved"] == STATE_APPROVED_UNPROVEN
    declared = {spec.state for spec in WAVE1_ROUTE_SPECS.values()}
    assert declared <= set(WAVE1_SPEC_STATES)
    records = route_evidence(fake_config(), connectors=load_executable_connectors())
    wave1 = records[("socialcrawl", "za", "youtube/video/comments")]
    assert wave1["condition"] == "blocked_fixture_unapproved"
    assert wave1["state"] == WAVE1_SPEC_STATES["blocked_fixture_unapproved"]

    class Retired:
        state = "retired"
        channel_family = "short_video"

    class FakeModule:
        WAVE1_ROUTE_SPECS: ClassVar[dict] = {"youtube/video/comments": Retired()}

    from src.analysis.open_intelligence.source_inventory import _route_switch

    with pytest.raises(ValueError, match="wave1_route_state_unknown"):
        _route_switch(fake_config(), "socialcrawl", "za", "youtube/video/comments", FakeModule)


def test_a_stage_with_no_capability_is_shipped_only_when_the_connector_reads_its_keys():
    # The null capability branch decides capability_shipped for the one quality
    # stage the document prints as shipped, and replacing it with True survived
    # the whole suite.
    rollout = load_yaml(ROLLOUT_CONFIG_PATH)
    stages = []
    for stage in rollout["stages"]:
        if stage.get("requires_capability") is None:
            stages.append(stage)
    assert stages, "the ladder no longer carries a stage without a capability"
    shipped = {
        item.stage_id: item.capability_shipped
        for item in ladder_increments(rollout)
        if item.requires_capability is None
    }
    assert shipped["s5_geo_full_watchlist"] is True
    ignored = {
        **rollout,
        "stages": [
            {
                **stage,
                "config_delta": {"socialcrawl.caps.a_key_the_connector_never_reads": 1},
            }
            if stage.get("requires_capability") is None
            else stage
            for stage in rollout["stages"]
        ],
    }
    unshipped = {
        item.stage_id: item.capability_shipped
        for item in ladder_increments(ignored)
        if item.requires_capability is None
    }
    assert unshipped["s5_geo_full_watchlist"] is False
    empty = {
        **rollout,
        "stages": [
            {**stage, "config_delta": {}} if stage.get("requires_capability") is None else stage
            for stage in rollout["stages"]
        ],
    }
    assert all(
        item.capability_shipped is False
        for item in ladder_increments(empty)
        if item.requires_capability is None
    )


def test_a_wave1_spec_state_is_looked_up_and_not_assumed(monkeypatch):
    """The wave 1 route state comes from the table, so a remap moves the route.

    The table holds one entry today, which makes a constant look identical to a
    lookup on the shipped data. Remapping the one declared spec state separates
    them: a route that reads its own declaration follows the table, and a route
    that returns a fixed state does not.
    """
    connectors = load_executable_connectors()
    route = ("socialcrawl", "za", "youtube/video/comments")
    shipped = route_evidence(fake_config(), connectors=connectors)[route]
    assert shipped["state"] == STATE_APPROVED_UNPROVEN
    assert shipped["evidence"]["route_state"] == "blocked_fixture_unapproved"
    monkeypatch.setitem(WAVE1_SPEC_STATES, "blocked_fixture_unapproved", STATE_KNOWN_EMPTY)
    remapped = route_evidence(fake_config(), connectors=connectors)[route]
    assert remapped["state"] == STATE_KNOWN_EMPTY
    assert remapped["evidence"]["route_state"] == "blocked_fixture_unapproved"


def test_a_connector_without_a_profile_refuses_rather_than_going_unreported():
    """An executable connector with no profile is a hole in the statement.

    Skipping it would drop the source from the estate silently, so the document
    would claim a complete reconciliation of a registry it had not read in full.
    """
    with pytest.raises(ValueError, match=r"connector_profile_missing:no_profile_for_this_one"):
        configuration_evidence(fake_config(), connectors=[("no_profile_for_this_one", object)])
    keyed = dict(load_executable_connectors())
    assert "no_profile_for_this_one" not in keyed
    assert set(keyed) <= set(CONNECTOR_PROFILES)


def test_the_document_prints_the_mixed_rows_it_says_it_prints():
    """The mixed row section is what keeps the row state from understating a row.

    The paragraph above the estate table sends a reader here for exactly these
    rows, so the section printing its standing sentence while mixed rows exist
    would send the reader to a claim that nothing is mixed.
    """
    document = source_estate_document(fake_config())
    section = document.split("### Rows to read with their routes", 1)[1].split("## ", 1)[0]
    assert "| source | market | row state | weakest state in scope | routes excluded |" in section
    assert "| gdelt | za | intentionally_excluded | approved_unproven | fetch_events |" in section
    assert "No row prints an operator route exclusion" not in section
    # The shipped estate has no mixed row, and there the sentence is the answer.
    shipped = source_estate_document()
    standing = shipped.split("### Rows to read with their routes", 1)[1].split("## ", 1)[0]
    assert "No row prints an operator route exclusion" in standing
    assert "| routes excluded |" not in standing


def test_two_registry_entries_under_one_key_refuse_rather_than_hiding_one():
    """One key can only carry one source, so a duplicate silently loses a source.

    The inventory is keyed by source and market, so a second entry under a key
    overwrites the first and the estate reports one source where two run.
    """
    registry = load_executable_connectors()
    doubled = [*registry, registry[0]]
    with pytest.raises(ValueError, match="connector_key_duplicate"):
        executable_inventory(connectors=doubled)
    assert len(executable_inventory(connectors=registry)) == len(registry) * len(POLICY_MARKETS)


def test_a_stage_whose_config_delta_is_not_a_mapping_refuses():
    """A ladder stage names its keys in a mapping, and a list of them names none.

    Reading such a stage as carrying no keys would report it as shipping nothing
    the connector reads, which is a finding about the connector rather than the
    malformed stage it actually is.
    """
    rollout = copy.deepcopy(load_yaml(ROLLOUT_CONFIG_PATH))
    stage = next(item for item in rollout["stages"] if item.get("requires_capability") is None)
    stage["config_delta"] = ["socialcrawl.caps.tiktok_search_pages"]
    with pytest.raises(ValueError, match=rf"rollout_stage_delta_invalid:{stage['id']}"):
        ladder_increments(rollout)


def test_two_call_sites_disagreeing_about_a_default_are_both_reported(tmp_path):
    """A key read with two defaults has no single default to hold a claim to.

    Reporting the last one read would let a declaration match one call site while
    the other one ships the opposite, which is the disagreement itself going
    unreported.
    """
    module = tmp_path / "two_minds.py"
    module.write_text(
        "def early(config):\n"
        '    return config.get("dual", True)\n\n\n'
        "def late(config):\n"
        '    return config.get("dual", False)\n\n\n'
        "def settled(config):\n"
        '    return config.get("agreed", 5) + config.get("agreed", 5)\n',
        encoding="utf-8",
    )
    defaults = connector_read_defaults(module)
    assert defaults["dual"] == (True, False)
    # A key both sites agree about still reads as the one default it has.
    assert defaults["agreed"] == (5,)


def test_a_comparison_against_something_other_than_a_boolean_reads_as_no_idiom(tmp_path):
    """``is None`` is not ``is False``, and guessing between them inverts a switch.

    A reading compared against None asks whether the key was configured at all,
    not whether it was switched off, so it names no Switch mode. Reading it as
    one would hold a declaration to a rule the connector does not apply.
    """
    module = tmp_path / "compared.py"
    module.write_text(
        "def unset(config):\n"
        '    return config.get("maybe", None) is None\n\n\n'
        "def missing(config):\n"
        '    return config.get("other", 0) is not None\n\n\n'
        "def switched(config):\n"
        '    return config.get("flag", True) is False\n',
        encoding="utf-8",
    )
    idioms = connector_read_idioms(module)
    assert "maybe" not in idioms
    assert "other" not in idioms
    assert idioms["flag"] == ("unless_false",)
