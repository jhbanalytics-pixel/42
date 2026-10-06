"""Boundary tests for the executable source estate policy reconciliation."""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from src.analysis.open_intelligence.source_policy import (
    DIGEST_FIELDS,
    FUNDED_GATE_FIELDS,
    POLICY_MARKETS,
    PRODUCER_PATH,
    PROOF_VALIDITY_DAYS,
    ROUTE_FIELDS,
    ActivationPolicy,
    RouteActivationRequest,
    RouteProbe,
    evaluate_route_activation,
    executable_route_keys,
    feature_blockers,
    fetch_surfaces,
    is_funded_gate,
    load_executable_connectors,
    mixed_state_rows,
    parse_capability_proof,
    policy_digest,
    reconcile_source_estate,
    reconcile_source_policy,
    render_inventory_table,
)

# Reviewed baseline of executable sources and their fetch surfaces at the time
# this test was written. It is a checkpoint for continuous integration, not the
# future authority: a connector added to the producer registry, removed from
# it, or growing a new public fetch surface must change this table and the
# policy rows together.
REVIEWED_SOURCE_BASELINE = (
    "rss",
    "apple_music",
    "spotify",
    "bigquery_trends",
    "youtube",
    "youtube_scrape",
    "gdelt",
    "ensemble",
    "reddit",
    "brand24",
    "socialcrawl",
    "wikipedia",
    "bluesky",
    "semrush",
    "google_trends_rss",
    "app_charts",
    "audiomack",
    "cloudflare_radar",
    "pulsar",
)
REVIEWED_SURFACES = {"gdelt": {"fetch", "fetch_events"}}
RECORDED_ABSENT: frozenset[str] = frozenset({"brand24"})


def policy_rows(inventory, *, state="approved_unproven", live=False):
    return [
        {
            "source": source,
            "market": market,
            "routes": [
                {"route": route, "state": state, "live": live} for route in sorted(surfaces)
            ],
        }
        for (source, market), surfaces in inventory.items()
    ]


class SingleSurface:
    def fetch(self, **kwargs):
        return None


class TwoSurfaces:
    def fetch(self, **kwargs):
        return None

    def fetch_events(self):
        return None

    def _fetch_hidden(self):
        return None

    def safe_fetch(self, **kwargs):
        return None


class NoSurface:
    def collect(self):
        return None


def test_no_source_or_market_can_disappear():
    inventory = {(s, m): {"fetch"} for s in ("rss", "socialcrawl") for m in ("za", "ng", "ke")}
    rows = [{"source": "rss", "market": "za", "routes": []}]
    with pytest.raises(ValueError, match="estate_incomplete"):
        reconcile_source_policy(inventory, rows)


def test_unknown_state_is_not_live():
    rows = [
        {
            "source": "rss",
            "market": m,
            "routes": [{"route": "feed", "state": "catalog_only", "live": True}],
        }
        for m in ("za", "ng", "ke")
    ]
    with pytest.raises(ValueError, match="unproven_live"):
        reconcile_source_policy({("rss", m): {"feed"} for m in ("za", "ng", "ke")}, rows)


def test_empty_inventory_refused():
    with pytest.raises(ValueError, match="inventory_empty"):
        reconcile_source_policy({}, [])


def test_inventory_missing_a_market_refused():
    inventory = {("rss", "za"): {"fetch"}, ("rss", "ng"): {"fetch"}}
    with pytest.raises(ValueError, match="inventory_market_missing"):
        reconcile_source_policy(inventory, policy_rows(inventory))


def test_duplicate_policy_row_is_estate_incomplete():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    rows = policy_rows(inventory)
    rows.append(dict(rows[0]))
    with pytest.raises(ValueError, match="estate_incomplete"):
        reconcile_source_policy(inventory, rows)


def test_route_set_must_equal_inventory_surfaces():
    inventory = {("gdelt", m): {"fetch", "fetch_events"} for m in POLICY_MARKETS}
    rows = policy_rows(inventory)
    rows[0]["routes"] = rows[0]["routes"][:1]
    with pytest.raises(ValueError, match="route_inventory_mismatch"):
        reconcile_source_policy(inventory, rows)
    rows = policy_rows(inventory)
    rows[1]["routes"].append(dict(rows[1]["routes"][0]))
    with pytest.raises(ValueError, match="route_inventory_mismatch"):
        reconcile_source_policy(inventory, rows)


def test_qualified_live_route_passes_and_rows_sort_by_source_then_market():
    inventory = {(s, m): {"fetch"} for s in ("socialcrawl", "rss") for m in POLICY_MARKETS}
    rows = policy_rows(inventory, state="qualified_live", live=True)
    rows.reverse()
    result = reconcile_source_policy(inventory, rows)
    assert [(row["source"], row["market"]) for row in result] == [
        ("rss", "ke"),
        ("rss", "ng"),
        ("rss", "za"),
        ("socialcrawl", "ke"),
        ("socialcrawl", "ng"),
        ("socialcrawl", "za"),
    ]


def test_fetch_surfaces_are_public_fetch_entry_points_only():
    assert fetch_surfaces(SingleSurface) == {"fetch"}
    assert fetch_surfaces(TwoSurfaces) == {"fetch", "fetch_events"}
    with pytest.raises(ValueError, match="connector_surface_missing"):
        fetch_surfaces(NoSurface)
    with pytest.raises(ValueError, match="connector_class_invalid"):
        fetch_surfaces(SingleSurface())


def test_executable_route_keys_cover_every_market_for_every_connector():
    inventory = executable_route_keys([("rss", SingleSurface), ("gdelt", TwoSurfaces)])
    assert inventory == {
        **{("rss", m): {"fetch"} for m in POLICY_MARKETS},
        **{("gdelt", m): {"fetch", "fetch_events"} for m in POLICY_MARKETS},
    }
    assert inventory[("rss", "za")] is not inventory[("rss", "ng")]


@pytest.mark.parametrize(
    ("connectors", "code"),
    [
        ([("rss", SingleSurface), ("rss", TwoSurfaces)], "connector_key_duplicate"),
        ([("", SingleSurface)], "connector_key_invalid"),
        ([("pulsar", NoSurface)], "connector_surface_missing"),
    ],
)
def test_executable_route_keys_refuse_bad_registry(connectors, code):
    with pytest.raises(ValueError, match=code):
        executable_route_keys(connectors)


def test_producer_registry_is_read_without_importing_the_producer():
    before = set(sys.modules)
    connectors = load_executable_connectors()
    added = set(sys.modules) - before
    assert all(isinstance(cls, type) for _, cls in connectors)
    assert not {name for name in added if name.endswith("run_rss_now")}
    assert PRODUCER_PATH.name == "run_rss_now.py"


def test_producer_inventory_matches_reviewed_baseline():
    connectors = load_executable_connectors()
    present = {key for key, _ in connectors}
    absent = set(REVIEWED_SOURCE_BASELINE) - present
    assert absent == set(RECORDED_ABSENT)
    assert present == set(REVIEWED_SOURCE_BASELINE) - set(RECORDED_ABSENT)
    inventory = executable_route_keys(connectors)
    assert set(inventory) == {(s, m) for s in present for m in POLICY_MARKETS}
    for (source, _market), surfaces in inventory.items():
        assert surfaces == REVIEWED_SURFACES.get(source, {"fetch"}), source
    rows = policy_rows(inventory)
    assert len(reconcile_source_policy(inventory, rows)) == len(present) * len(POLICY_MARKETS)


def test_added_executable_connector_without_policy_row_fails():
    connectors = load_executable_connectors()
    rows = policy_rows(executable_route_keys(connectors))
    grown = executable_route_keys([*connectors, ("new_vendor", SingleSurface)])
    with pytest.raises(ValueError, match="estate_incomplete"):
        reconcile_source_policy(grown, rows)


def test_registry_loader_refuses_unreadable_producer(tmp_path: Path):
    no_registry = tmp_path / "no_registry.py"
    no_registry.write_text("MARKETS = ['za']\n", encoding="utf-8")
    with pytest.raises(ValueError, match="producer_registry_missing"):
        load_executable_connectors(no_registry)
    unknown_class = tmp_path / "unknown_class.py"
    unknown_class.write_text("CONNECTORS = [('rss', RSSConnector)]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="producer_connector_import_missing"):
        load_executable_connectors(unknown_class)
    bad_shape = tmp_path / "bad_shape.py"
    bad_shape.write_text(
        "from src.ingestion.connectors.rss import RSSConnector\nCONNECTORS = [('rss',)]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="producer_registry_shape"):
        load_executable_connectors(bad_shape)


def route_record(source, market, route, **overrides):
    record = {
        "vendor": "vendor",
        "account": "account",
        "platform": "platform",
        "market": market,
        "feature": "feature",
        "required": False,
        "state": "approved_unproven",
        "condition": "switch_on_unproven",
        "capability_proof": None,
        "cadence": "daily",
        "freshness": "run_window",
        "native_identity": "native_id",
        "date_rule": "published_at",
        "author_rule": "handle",
        "url_rule": "item_url",
        "geography_method": "market_terms",
        "budget_stage": "free",
        "qualified_contribution": None,
        "next_owner_action": "attach proof",
        "evidence": {"switch": f"{source}.enabled", "value": True},
    }
    record.update(overrides)
    return record


def estate_evidence(inventory, **overrides):
    return {
        (source, market, route): route_record(source, market, route, **overrides)
        for (source, market), surfaces in inventory.items()
        for route in surfaces
    }


def test_estate_rows_carry_every_route_field_and_sort_deterministically():
    inventory = {
        (s, m): {"fetch", "fetch_events"} for s in ("gdelt", "rss") for m in POLICY_MARKETS
    }
    rows = reconcile_source_estate(inventory, estate_evidence(inventory))
    assert [(row["source"], row["market"]) for row in rows] == sorted(
        (s, m) for s in ("gdelt", "rss") for m in POLICY_MARKETS
    )
    for row in rows:
        assert row["state"] == "approved_unproven"
        assert [route["route"] for route in row["routes"]] == ["fetch", "fetch_events"]
        for route in row["routes"]:
            assert set(ROUTE_FIELDS) <= set(route)
            assert route["market"] == row["market"]
            assert route["live"] is False


def test_estate_refuses_missing_route_evidence_and_unmatched_evidence_rows():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    evidence = estate_evidence(inventory)
    del evidence[("rss", "ke", "fetch")]
    with pytest.raises(ValueError, match="route_evidence_missing"):
        reconcile_source_estate(inventory, evidence)
    with pytest.raises(ValueError, match="evidence_unmatched"):
        reconcile_source_estate(
            inventory,
            estate_evidence(inventory),
            [{"source": "rss", "market": "za", "route": "feed", "state": "known_empty"}],
        )


def test_estate_refuses_unknown_states_and_missing_fields():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    with pytest.raises(ValueError, match="state_unknown"):
        reconcile_source_estate(inventory, estate_evidence(inventory, state="catalog_only"))
    broken = estate_evidence(inventory)
    del broken[("rss", "za", "fetch")]["geography_method"]
    with pytest.raises(ValueError, match="route_field_missing"):
        reconcile_source_estate(inventory, broken)
    with pytest.raises(ValueError, match="route_market_mismatch"):
        reconcile_source_estate(
            inventory,
            {key: {**value, "market": "za"} for key, value in estate_evidence(inventory).items()},
        )


def test_qualified_live_needs_proof_and_contribution_and_is_the_only_live_state():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    with pytest.raises(ValueError, match="qualified_without_proof"):
        reconcile_source_estate(inventory, estate_evidence(inventory, state="qualified_live"))
    evidence_rows = [
        {
            "source": "rss",
            "market": m,
            "route": "fetch",
            "state": "qualified_live",
            "condition": "capability_proved",
            "capability_proof": f"pipeline_runs:2026-09-12:rss:{m}:feature",
            "qualified_contribution": 42,
            "next_owner_action": "Re-prove before the validity window closes.",
        }
        for m in POLICY_MARKETS
    ]
    rows = reconcile_source_estate(inventory, estate_evidence(inventory), evidence_rows)
    assert all(row["state"] == "qualified_live" for row in rows)
    assert all(route["live"] is True for row in rows for route in row["routes"])
    assert rows[0]["routes"][0]["qualified_contribution"] == 42
    partial = [
        {**row, "state": "degraded", "condition": "part_switched_off"} for row in evidence_rows
    ]
    rows = reconcile_source_estate(inventory, estate_evidence(inventory), partial)
    assert all(route["live"] is False for row in rows for route in row["routes"])
    assert all(row["state"] == "degraded" for row in rows)


def test_evidence_rows_may_only_override_evidence_fields():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    with pytest.raises(ValueError, match="evidence_field_unknown"):
        reconcile_source_estate(
            inventory,
            estate_evidence(inventory),
            [{"source": "rss", "market": "za", "route": "fetch", "vendor": "other"}],
        )


def test_source_state_is_the_weakest_route_state():
    # Until this slice was reviewed the row took the STRONGEST route state, so a
    # source with one live route and one dead route read as live. The weakest
    # state is what a reader can act on, and the route table carries the detail.
    inventory = {("gdelt", m): {"fetch", "fetch_events"} for m in POLICY_MARKETS}
    evidence = estate_evidence(inventory)
    evidence[("gdelt", "za", "fetch_events")]["state"] = "intentionally_excluded"
    evidence[("gdelt", "ng", "fetch")]["state"] = "unavailable"
    evidence[("gdelt", "ng", "fetch_events")]["state"] = "unavailable"
    evidence[("gdelt", "ke", "fetch")]["state"] = "known_empty"
    evidence[("gdelt", "ke", "fetch_events")]["state"] = "intentionally_excluded"
    rows = {row["market"]: row["state"] for row in reconcile_source_estate(inventory, evidence)}
    assert rows == {
        "za": "intentionally_excluded",
        "ng": "unavailable",
        "ke": "intentionally_excluded",
    }


def test_policy_digest_is_canonical_and_moves_with_state():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    rows = reconcile_source_estate(inventory, estate_evidence(inventory))
    digest = policy_digest(rows)
    assert len(digest) == 64
    assert digest == policy_digest(list(rows))
    moved = reconcile_source_estate(inventory, estate_evidence(inventory, state="known_empty"))
    assert policy_digest(moved) != digest
    with pytest.raises(ValueError, match="policy_rows_invalid"):
        policy_digest([{"source": "rss"}])


def test_render_inventory_table_is_one_line_per_source_market():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    table = render_inventory_table(reconcile_source_estate(inventory, estate_evidence(inventory)))
    assert table.splitlines()[:3] == [
        "| source | market | surfaces | state |",
        "|---|---|---|---|",
        "| rss | ke | fetch | approved_unproven |",
    ]


def test_policy_digest_covers_policy_fields_and_not_the_evidence_reading():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    base = reconcile_source_estate(inventory, estate_evidence(inventory))
    reread = estate_evidence(inventory)
    for record in reread.values():
        record["evidence"] = {"switch": "rss_feeds.za", "value": ["https://other.example"]}
    assert policy_digest(reconcile_source_estate(inventory, reread)) == policy_digest(base)
    moved = estate_evidence(inventory, next_owner_action="promote")
    assert policy_digest(reconcile_source_estate(inventory, moved)) != policy_digest(base)
    assert "evidence" not in DIGEST_FIELDS
    assert set(DIGEST_FIELDS) == set(ROUTE_FIELDS) - {"evidence"}


ESTATE_AS_OF = date(2026, 9, 18)


def feature_blockers_as_of(rows, as_of=ESTATE_AS_OF):
    return feature_blockers(rows, as_of=as_of)


def blocker_rows(inventory, **overrides):
    return reconcile_source_estate(inventory, estate_evidence(inventory, **overrides))


def test_feature_blockers_name_every_unserved_promised_feature():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    blockers = feature_blockers_as_of(blocker_rows(inventory, required=True))
    assert [(item.feature, item.market) for item in blockers] == [
        ("feature", "ke"),
        ("feature", "ng"),
        ("feature", "za"),
    ]
    assert all(item.reason == "unproven" for item in blockers)
    assert all(item.required for item in blockers)
    assert all(item.providers == ("rss",) for item in blockers)
    assert all(item.next_owner_action for item in blockers)


def test_unavailable_provider_of_a_promised_feature_is_a_blocker_with_no_provider():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    blockers = feature_blockers_as_of(blocker_rows(inventory, state="unavailable", required=True))
    assert {item.reason for item in blockers} == {"no_provider"}
    blockers = feature_blockers_as_of(blocker_rows(inventory, state="intentionally_excluded"))
    assert {item.reason for item in blockers} == {"no_provider"}
    assert {item.required for item in blockers} == {False}


def test_a_substitute_clears_the_blocker_only_with_its_own_capability_proof():
    inventory = {
        (source, market): {"fetch"}
        for source in ("spotify", "apple_music")
        for market in POLICY_MARKETS
    }
    evidence = estate_evidence(inventory, feature="music_charts", required=True)
    for market in POLICY_MARKETS:
        evidence[("spotify", market, "fetch")]["state"] = "unavailable"
    rows = reconcile_source_estate(inventory, evidence)
    blockers = feature_blockers_as_of(rows)
    assert [(item.feature, item.market, item.reason) for item in blockers] == [
        ("music_charts", "ke", "unproven"),
        ("music_charts", "ng", "unproven"),
        ("music_charts", "za", "unproven"),
    ]
    assert all(item.providers == ("apple_music", "spotify") for item in blockers)
    proven = reconcile_source_estate(
        inventory,
        evidence,
        [
            {
                "source": "apple_music",
                "market": market,
                "route": "fetch",
                "state": "qualified_live",
                "condition": "capability_proved",
                "capability_proof": (f"pipeline_runs:2026-09-12:apple_music:{market}:music_charts"),
                "qualified_contribution": 12,
                "next_owner_action": "Re-prove before the validity window closes.",
            }
            for market in POLICY_MARKETS
        ],
    )
    assert feature_blockers_as_of(proven) == ()


def test_a_qualified_live_route_without_a_proof_reference_cannot_clear_a_blocker():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    rows = list(blocker_rows(inventory))
    for row in rows:
        row["routes"][0] = {
            **row["routes"][0],
            "state": "qualified_live",
            "live": True,
            "capability_proof": "",
        }
    blockers = feature_blockers_as_of(rows)
    assert {item.reason for item in blockers} == {"unproven"}
    assert all(item.providers == ("rss",) for item in blockers)


def test_degraded_provider_is_reported_as_unproven_not_as_served():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    blockers = feature_blockers_as_of(blocker_rows(inventory, state="degraded"))
    assert {item.reason for item in blockers} == {"unproven"}
    assert {row["state"] for row in blocker_rows(inventory, state="degraded")} == {"degraded"}


ACTIVATION_POLICY = ActivationPolicy(
    min_row_integrity_pct=90,
    min_probe_observations=20,
    clean_crons_required=3,
    burn_overrun_pct=25,
    max_increments_open=1,
    quality_before_volume=True,
)


def probe(route="tiktok/post/comments", **overrides):
    record = {
        "route": route,
        "payload_digest": "a" * 64,
        "normalizer_digest": "a" * 64,
        "observations": 30,
        "integrity_observations": 29,
    }
    record.update(overrides)
    record.setdefault(
        "retained_reference", f"gs://staging-probes/2026-09-16/{record['payload_digest']}.json"
    )
    return RouteProbe(**record)


class Gate:
    """Stand in for the funded lane gate result, which carries these fields."""

    def __init__(self, *, allowed=True, current_balance=Decimal("19607"), run_allowance=None):
        self.allowed = allowed
        self.reasons = () if allowed else ("reserve_floor_reached",)
        self.current_balance = current_balance
        self.run_allowance = Decimal("100") if run_allowance is None else run_allowance


def request(**overrides):
    fields = {
        "route": "tiktok/post/comments",
        "stage_id": "s4_tiktok_comments",
        "market": "za",
        "route_state": "approved_unproven",
        "increment_credits": 30,
        "volume_increment": False,
        "pending_quality_stages": (),
        "increments_open": 0,
        "capability_shipped": True,
        "clean_run_ids": ("2026-09-15", "2026-09-16", "2026-09-17"),
        "burn_overrun_pct": 0,
        "probe": probe(),
        "funded_gate": Gate(),
        "policy": ACTIVATION_POLICY,
    }
    fields.update(overrides)
    return RouteActivationRequest(**fields)


def test_a_complete_increment_activates_one_route_and_names_no_reason():
    decision = evaluate_route_activation(request())
    assert (decision.activate, decision.reasons) == (True, ())
    assert (decision.route, decision.stage_id, decision.market) == (
        "tiktok/post/comments",
        "s4_tiktok_comments",
        "za",
    )
    assert decision.funded_reasons == ()


def test_activation_refuses_an_unmeasured_balance_and_never_assumes_funding():
    decision = evaluate_route_activation(request(funded_gate=Gate(current_balance=None)))
    assert decision.activate is False
    assert "balance_unmeasured" in decision.reasons
    missing = evaluate_route_activation(request(funded_gate=None))
    assert missing.activate is False
    assert "funding_unproven" in missing.reasons


def test_activation_refuses_when_the_funded_gate_refuses_and_carries_its_reasons():
    decision = evaluate_route_activation(request(funded_gate=Gate(allowed=False)))
    assert decision.activate is False
    assert "funding_refused" in decision.reasons
    assert decision.funded_reasons == ("reserve_floor_reached",)


def test_activation_refuses_an_increment_the_run_allowance_cannot_cover():
    decision = evaluate_route_activation(
        request(increment_credits=101, funded_gate=Gate(run_allowance=Decimal("100")))
    )
    assert decision.activate is False
    assert "allowance_below_increment" in decision.reasons
    assert evaluate_route_activation(request(increment_credits=100)).activate is True


def test_activation_refuses_without_a_retained_probe_of_the_exact_route():
    assert "probe_missing" in evaluate_route_activation(request(probe=None)).reasons
    mismatched = evaluate_route_activation(request(probe=probe(route="youtube/search")))
    assert mismatched.activate is False
    assert "probe_route_mismatch" in mismatched.reasons


def test_activation_refuses_a_normalization_that_did_not_read_the_retained_payload():
    decision = evaluate_route_activation(request(probe=probe(normalizer_digest="b" * 64)))
    assert decision.activate is False
    assert "normalization_not_on_retained_payload" in decision.reasons


def test_activation_refuses_below_the_row_integrity_floor():
    decision = evaluate_route_activation(
        request(probe=probe(observations=30, integrity_observations=26))
    )
    assert decision.activate is False
    assert "row_integrity_below_floor" in decision.reasons
    assert (
        evaluate_route_activation(
            request(probe=probe(observations=30, integrity_observations=27))
        ).activate
        is True
    )


def test_activation_refuses_an_empty_probe_rather_than_reading_it_as_perfect():
    decision = evaluate_route_activation(
        request(probe=probe(observations=0, integrity_observations=0))
    )
    assert decision.activate is False
    assert "probe_observed_nothing" in decision.reasons


def test_activation_refuses_before_three_clean_runs_and_on_a_burn_overrun():
    short = evaluate_route_activation(request(clean_run_ids=("2026-09-16", "2026-09-17")))
    assert short.activate is False
    assert "clean_runs_short" in short.reasons
    overrun = evaluate_route_activation(request(burn_overrun_pct=26))
    assert overrun.activate is False
    assert "burn_over_model" in overrun.reasons
    assert evaluate_route_activation(request(burn_overrun_pct=25)).activate is True


def test_activation_opens_one_increment_at_a_time():
    decision = evaluate_route_activation(request(increments_open=1))
    assert decision.activate is False
    assert "increment_already_open" in decision.reasons


def test_volume_is_never_activated_while_a_quality_increment_is_pending():
    decision = evaluate_route_activation(
        request(
            route="tiktok/search/top",
            stage_id="s1_tiktok_page2",
            volume_increment=True,
            probe=probe(route="tiktok/search/top"),
            pending_quality_stages=("s4_tiktok_comments",),
        )
    )
    assert decision.activate is False
    assert "quality_increment_pending" in decision.reasons
    allowed = evaluate_route_activation(
        request(
            route="tiktok/search/top",
            stage_id="s1_tiktok_page2",
            volume_increment=True,
            probe=probe(route="tiktok/search/top"),
        )
    )
    assert allowed.activate is True


def test_activation_refuses_an_unshipped_capability_and_a_route_that_is_not_approved():
    assert "capability_not_shipped" in (
        evaluate_route_activation(request(capability_shipped=False)).reasons
    )
    for state in ("unavailable", "intentionally_excluded", "known_empty", "degraded"):
        decision = evaluate_route_activation(request(route_state=state))
        assert decision.activate is False, state
        assert "route_not_approved" in decision.reasons, state
    assert evaluate_route_activation(request(route_state="qualified_live")).reasons == (
        "route_already_live",
    )


def test_activation_reasons_are_sorted_unique_and_reported_together():
    decision = evaluate_route_activation(
        request(
            probe=None,
            clean_run_ids=(),
            increments_open=2,
            capability_shipped=False,
            funded_gate=None,
        )
    )
    assert decision.activate is False
    assert decision.reasons == tuple(sorted(set(decision.reasons)))
    assert set(decision.reasons) == {
        "capability_not_shipped",
        "clean_runs_short",
        "funding_unproven",
        "increment_already_open",
        "probe_missing",
    }


def test_probe_refuses_an_impossible_or_unretained_reading():
    with pytest.raises(ValueError, match="probe_integrity_invalid"):
        probe(observations=10, integrity_observations=11)
    with pytest.raises(ValueError, match="probe_reference_missing"):
        probe(retained_reference="")
    with pytest.raises(ValueError, match="probe_digest_invalid"):
        probe(payload_digest="short")
    with pytest.raises(ValueError, match="probe_observations_invalid"):
        probe(observations=-1)
    # A probe that names no route is evidence attached to nothing, so it would
    # be counted towards whatever route the caller happened to be proposing.
    with pytest.raises(ValueError, match="probe_route_invalid"):
        probe(route="")
    with pytest.raises(ValueError, match="probe_route_invalid"):
        probe(route=None)


def test_activation_policy_refuses_a_relaxed_or_incomplete_ladder_policy():
    with pytest.raises(ValueError, match="activation_policy_invalid"):
        ActivationPolicy(
            min_row_integrity_pct=0,
            min_probe_observations=20,
            clean_crons_required=3,
            burn_overrun_pct=25,
            max_increments_open=1,
            quality_before_volume=True,
        )
    with pytest.raises(ValueError, match="activation_policy_invalid"):
        ActivationPolicy(
            min_row_integrity_pct=90,
            min_probe_observations=20,
            clean_crons_required=0,
            burn_overrun_pct=25,
            max_increments_open=1,
            quality_before_volume=True,
        )
    with pytest.raises(ValueError, match="activation_policy_invalid"):
        ActivationPolicy(
            min_row_integrity_pct=90,
            min_probe_observations=20,
            clean_crons_required=3,
            burn_overrun_pct=25,
            max_increments_open=0,
            quality_before_volume=True,
        )


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"route_state": "catalog_only"}, "activation_request_invalid:route_state"),
        ({"increment_credits": -1}, "activation_request_invalid:increment_credits"),
        ({"burn_overrun_pct": None}, "activation_request_invalid:burn_overrun_pct"),
        ({"volume_increment": 1}, "activation_request_invalid:volume_increment"),
        ({"pending_quality_stages": ["s4"]}, "activation_request_invalid:pending_quality_stages"),
        ({"probe": "probe"}, "activation_request_invalid:probe"),
        ({"policy": {"min_row_integrity_pct": 90}}, "activation_request_invalid:policy"),
        ({"market": ""}, "activation_request_invalid:market"),
    ],
)
def test_activation_request_refuses_an_unreadable_proposal(overrides, code):
    with pytest.raises(ValueError, match=re.escape(code)):
        request(**overrides)


def test_activation_refuses_anything_that_is_not_a_request():
    with pytest.raises(ValueError, match="activation_request_invalid"):
        evaluate_route_activation({"route": "tiktok/post/comments"})


# -- hostile review closure ------------------------------------------------
# Every test below names one finding the review proved by running code against
# the shipped configuration, and each one fails on a single weakening edit to
# the guard it names.


def test_the_row_state_is_the_weakest_route_state_so_a_dead_route_is_never_hidden():
    inventory = {("gdelt", m): {"fetch", "fetch_events"} for m in POLICY_MARKETS}
    evidence = estate_evidence(inventory)
    evidence[("gdelt", "za", "fetch_events")]["state"] = "unavailable"
    evidence[("gdelt", "za", "fetch_events")]["condition"] = "vendor_account_cancelled"
    evidence[("gdelt", "ng", "fetch")]["state"] = "degraded"
    evidence[("gdelt", "ng", "fetch")]["condition"] = "part_switched_off"
    evidence[("gdelt", "ke", "fetch")]["state"] = "intentionally_excluded"
    evidence[("gdelt", "ke", "fetch")]["condition"] = "operator_excluded"
    rows = {row["market"]: row["state"] for row in reconcile_source_estate(inventory, evidence)}
    assert rows == {
        "za": "unavailable",
        "ng": "degraded",
        "ke": "intentionally_excluded",
    }


def test_every_route_carries_the_condition_behind_its_state():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    rows = reconcile_source_estate(inventory, estate_evidence(inventory))
    assert all(route["condition"] for row in rows for route in row["routes"])
    assert "condition" in ROUTE_FIELDS
    assert "condition" in DIGEST_FIELDS
    blank = estate_evidence(inventory, condition="")
    with pytest.raises(ValueError, match="route_condition_missing"):
        reconcile_source_estate(inventory, blank)


def qualified_row(market, *, source="rss", feature="feature", proof=None, contribution=41):
    return {
        "source": source,
        "market": market,
        "route": "fetch",
        "state": "qualified_live",
        "condition": "capability_proved",
        "capability_proof": (
            f"pipeline_runs:2026-09-12:{source}:{market}:{feature}" if proof is None else proof
        ),
        "qualified_contribution": contribution,
        "next_owner_action": "Re-prove before the ninety day window closes.",
    }


def test_a_qualified_live_proof_must_name_the_source_market_and_feature_it_claims():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    evidence = estate_evidence(inventory)
    rows = reconcile_source_estate(inventory, evidence, [qualified_row(m) for m in POLICY_MARKETS])
    assert all(route["live"] for row in rows for route in row["routes"])
    for broken in (
        "pipeline_runs:2026-09-12:rss:za:other_feature",
        "pipeline_runs:2026-09-12:rss:ng:feature",
        "pipeline_runs:2026-09-12:other_source:za:feature",
        "x",
        "pipeline_runs:not-a-date:rss:za:feature",
        "pipeline_runs:2026-09-12:rss:za",
    ):
        with pytest.raises(ValueError, match="qualified_proof_unbound"):
            reconcile_source_estate(
                inventory,
                evidence,
                [qualified_row("za", proof=broken), *[qualified_row(m) for m in ("ng", "ke")]],
            )


def test_a_qualified_live_contribution_must_be_a_measured_count():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    evidence = estate_evidence(inventory)
    for contribution in (0, -5, "none measured", True, 1.5, None):
        with pytest.raises(ValueError, match=r"qualified_without_proof|qualified_contribution"):
            reconcile_source_estate(
                inventory,
                evidence,
                [
                    qualified_row("za", contribution=contribution),
                    *[qualified_row(m) for m in ("ng", "ke")],
                ],
            )
    rows = reconcile_source_estate(
        inventory, evidence, [qualified_row(m, contribution=1) for m in POLICY_MARKETS]
    )
    assert rows[0]["routes"][0]["qualified_contribution"] == 1


def test_an_empty_proof_is_refused_by_the_record_builder_not_only_by_the_reader():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    with pytest.raises(ValueError, match="qualified_without_proof"):
        reconcile_source_estate(
            inventory,
            estate_evidence(inventory),
            [qualified_row("za", proof=""), *[qualified_row(m) for m in ("ng", "ke")]],
        )


def test_an_evidence_row_cannot_promote_a_route_the_configuration_reads_as_dead():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    dead = estate_evidence(inventory, state="unavailable", condition="vendor_account_cancelled")
    bare = {
        "source": "rss",
        "market": "za",
        "route": "fetch",
        "state": "approved_unproven",
        "condition": "switch_on_unproven",
        "next_owner_action": "attach proof",
    }
    with pytest.raises(ValueError, match="evidence_promotion_unproven"):
        reconcile_source_estate(inventory, dead, [bare])
    with pytest.raises(ValueError, match="evidence_promotion_unexecutable"):
        reconcile_source_estate(inventory, dead, [qualified_row("za")])
    # The same row against a configuration that reads the route as executable
    # is the promotion the estate is built to record.
    rows = reconcile_source_estate(
        inventory, estate_evidence(inventory), [qualified_row(m) for m in POLICY_MARKETS]
    )
    assert all(row["state"] == "qualified_live" for row in rows)


def test_an_evidence_row_that_moves_a_state_must_name_a_condition_and_an_action():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    evidence = estate_evidence(inventory)
    without_action = dict(qualified_row("za"))
    without_action["next_owner_action"] = ""
    with pytest.raises(ValueError, match="evidence_override_unjustified"):
        reconcile_source_estate(inventory, evidence, [without_action])
    without_condition = {
        key: value for key, value in qualified_row("za").items() if key != "condition"
    }
    with pytest.raises(ValueError, match="evidence_override_condition_missing"):
        reconcile_source_estate(inventory, evidence, [without_condition])


def test_a_proof_outside_its_validity_window_leaves_the_blocker_standing():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    rows = reconcile_source_estate(
        inventory,
        estate_evidence(inventory, required=True),
        [
            qualified_row(m, proof=f"pipeline_runs:2019-04-01:rss:{m}:feature")
            for m in POLICY_MARKETS
        ],
    )
    stale = feature_blockers(rows, as_of=date(2026, 9, 18))
    assert {item.reason for item in stale} == {"proof_expired"}
    fresh = reconcile_source_estate(
        inventory,
        estate_evidence(inventory, required=True),
        [qualified_row(m) for m in POLICY_MARKETS],
    )
    assert feature_blockers(fresh, as_of=date(2026, 9, 12)) == ()
    edge = date(2026, 9, 12) + timedelta(days=PROOF_VALIDITY_DAYS)
    assert feature_blockers(fresh, as_of=edge) == ()
    assert {item.reason for item in feature_blockers(fresh, as_of=edge + timedelta(days=1))} == {
        "proof_expired"
    }
    assert {item.reason for item in feature_blockers(fresh, as_of=date(2026, 9, 11))} == {
        "proof_expired"
    }


def test_one_proven_route_of_a_many_route_source_does_not_clear_the_feature():
    surfaces = {f"route_{index}" for index in range(15)}
    inventory = {("socialcrawl", m): set(surfaces) for m in POLICY_MARKETS}
    evidence = estate_evidence(inventory, feature="social", required=True)
    rows = reconcile_source_estate(
        inventory,
        evidence,
        [
            {**qualified_row(m, source="socialcrawl", feature="social"), "route": "route_0"}
            for m in POLICY_MARKETS
        ],
    )
    blockers = feature_blockers(rows, as_of=date(2026, 9, 18))
    assert {item.reason for item in blockers} == {"unproven"}
    assert {item.market for item in blockers} == set(POLICY_MARKETS)
    every_route = reconcile_source_estate(
        inventory,
        evidence,
        [
            {**qualified_row(m, source="socialcrawl", feature="social"), "route": route}
            for m in POLICY_MARKETS
            for route in sorted(surfaces)
        ],
    )
    assert feature_blockers(every_route, as_of=date(2026, 9, 18)) == ()


def test_only_a_qualified_live_route_can_clear_a_blocker():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    rows = list(blocker_rows(inventory, required=True))
    for row in rows:
        row["routes"][0] = {
            **row["routes"][0],
            "state": "approved_unproven",
            "capability_proof": f"pipeline_runs:2026-09-12:rss:{row['market']}:feature",
            "qualified_contribution": 41,
        }
    blockers = feature_blockers(rows, as_of=date(2026, 9, 18))
    assert {item.reason for item in blockers} == {"unproven"}
    assert len(blockers) == len(POLICY_MARKETS)


def test_a_funded_gate_that_is_not_a_measured_gate_is_refused_rather_than_read():
    class Loose:
        allowed = "no"
        reasons = ()
        current_balance = "not measured"
        run_allowance = float("inf")

    with pytest.raises(ValueError, match="activation_request_invalid:funded_gate"):
        request(funded_gate=Loose())

    class Missing:
        allowed = True

    with pytest.raises(ValueError, match="activation_request_invalid:funded_gate"):
        request(funded_gate=Missing())

    class NotANumber:
        allowed = True
        reasons = ()
        current_balance = Decimal("NaN")
        run_allowance = Decimal("Infinity")

    with pytest.raises(ValueError, match="activation_request_invalid:funded_gate"):
        request(funded_gate=NotANumber())


def test_activation_refuses_a_zero_balance_and_an_increment_that_declares_no_cost():
    decision = evaluate_route_activation(
        request(
            increment_credits=0,
            funded_gate=Gate(current_balance=Decimal("0"), run_allowance=Decimal("0")),
        )
    )
    assert decision.activate is False
    assert "balance_exhausted" in decision.reasons
    assert "increment_costs_nothing" in decision.reasons


def test_clean_runs_are_named_runs_so_one_run_cannot_be_counted_three_times():
    repeated = evaluate_route_activation(request(clean_run_ids=("run-a", "run-a", "run-a")))
    assert repeated.activate is False
    assert "clean_runs_not_distinct" in repeated.reasons
    short = evaluate_route_activation(request(clean_run_ids=("run-a", "run-b")))
    assert short.activate is False
    assert "clean_runs_short" in short.reasons
    assert evaluate_route_activation(request()).activate is True


def test_a_probe_is_bound_to_the_payload_it_retained_and_needs_enough_observations():
    with pytest.raises(ValueError, match="probe_reference_unbound"):
        probe(retained_reference="gs://staging-probes/2026-09-16/tiktok-post-comments.json")
    thin = evaluate_route_activation(request(probe=probe(observations=1, integrity_observations=1)))
    assert thin.activate is False
    assert "probe_observations_short" in thin.reasons


# -- second hostile review closure -----------------------------------------


def test_a_row_whose_weakest_route_is_an_operator_exclusion_is_named_with_its_routes():
    # The weakest route state alone understates in the other direction: with one
    # route an operator switched off among live ones, a row prints
    # intentionally_excluded, whose legend reads "switched off by an operator
    # decision", so a live required provider read as switched off. The row state
    # still hides nothing; the mixed rows name the ones a reader must not read
    # from the row state alone.
    inventory = {("gdelt", m): {"fetch", "fetch_events"} for m in POLICY_MARKETS}
    evidence = estate_evidence(inventory)
    evidence[("gdelt", "za", "fetch_events")]["state"] = "intentionally_excluded"
    evidence[("gdelt", "za", "fetch_events")]["condition"] = "route_switched_off"
    for market in ("ng", "ke"):
        for route in ("fetch", "fetch_events"):
            evidence[("gdelt", market, route)]["state"] = "intentionally_excluded"
            evidence[("gdelt", market, route)]["condition"] = "operator_excluded"
    rows = reconcile_source_estate(inventory, evidence)
    by_market = {row["market"]: row for row in rows}
    assert by_market["za"]["state"] == "intentionally_excluded"
    assert by_market["za"]["in_scope_state"] == "approved_unproven"
    # A row every route of which is excluded IS a switched off source, and reads
    # the same in both columns.
    assert by_market["ng"]["state"] == "intentionally_excluded"
    assert by_market["ng"]["in_scope_state"] == "intentionally_excluded"
    mixed = mixed_state_rows(rows)
    assert [(item["source"], item["market"]) for item in mixed] == [("gdelt", "za")]
    assert mixed[0]["in_scope_state"] == "approved_unproven"
    assert mixed[0]["excluded_routes"] == ("fetch_events",)
    # An unavailable route is weaker than an exclusion, so it stays the row state
    # and the row is not mixed: nothing here lets a dead route out of the row.
    dead = estate_evidence(inventory)
    dead[("gdelt", "za", "fetch_events")]["state"] = "unavailable"
    dead_rows = reconcile_source_estate(inventory, dead)
    assert {row["market"]: row["state"] for row in dead_rows}["za"] == "unavailable"
    assert mixed_state_rows(dead_rows) == ()


def test_the_row_integrity_floor_is_the_floor_the_policy_names():
    # The only test of this floor used 30 observations with 26 against 27, which
    # cannot tell a 90 percent floor from an 89 percent one. A hundred
    # observations can, and a relaxed threshold is the classic mutation.
    assert ACTIVATION_POLICY.min_row_integrity_pct == 90
    at_the_floor = evaluate_route_activation(
        request(probe=probe(observations=100, integrity_observations=90))
    )
    assert at_the_floor.activate is True
    one_below = evaluate_route_activation(
        request(probe=probe(observations=100, integrity_observations=89))
    )
    assert one_below.activate is False
    assert "row_integrity_below_floor" in one_below.reasons
    # And the floor moves with the policy rather than being wired to 90.
    relaxed = ActivationPolicy(
        min_row_integrity_pct=89,
        min_probe_observations=20,
        clean_crons_required=3,
        burn_overrun_pct=25,
        max_increments_open=1,
        quality_before_volume=True,
    )
    assert (
        evaluate_route_activation(
            request(probe=probe(observations=100, integrity_observations=89), policy=relaxed)
        ).activate
        is True
    )


def test_two_evidence_rows_for_one_route_are_refused_rather_than_resolved_silently():
    # Two rows that contradict each other are a question, not an answer, and the
    # later one used to win with nothing said.
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    evidence = estate_evidence(inventory)
    row = {
        "source": "rss",
        "market": "za",
        "route": "fetch",
        "state": "approved_unproven",
        "condition": "switch_on_unproven",
        "next_owner_action": "attach proof",
    }
    with pytest.raises(ValueError, match="evidence_duplicate:rss:za:fetch"):
        reconcile_source_estate(
            inventory, evidence, [row, {**row, "condition": "market_expected_empty"}]
        )
    # One row alone is still read.
    assert reconcile_source_estate(inventory, evidence, [row])


def test_each_funded_gate_guard_refuses_an_object_that_trips_only_that_guard():
    # The first pass offered three objects, each of which tripped a guard other
    # than the one it was meant to demonstrate, so removing any single guard left
    # the suite green. One object per guard, each otherwise a valid gate.
    class Valid:
        allowed = True
        reasons = ()
        current_balance = Decimal("19607")
        run_allowance = Decimal("100")

    assert is_funded_gate(Valid())

    def gate(**fields):
        return type("OneGuard", (Valid,), fields)()

    # allowed: a bool and not a truthy string spelling the opposite.
    assert is_funded_gate(gate(allowed="no")) is False
    assert is_funded_gate(gate(allowed=1)) is False
    # reasons: a tuple of non empty strings, because a refusal with no reason
    # reads as a gate that allowed.
    assert is_funded_gate(gate(reasons=["reserve_floor_reached"])) is False
    assert is_funded_gate(gate(reasons=("",))) is False
    assert is_funded_gate(gate(reasons=(None,))) is False
    # balance: None means unmeasured and is read on its own; anything else has to
    # be a counted finite credit, so "not measured" and a NaN are refusals.
    assert is_funded_gate(gate(current_balance=None)) is True
    assert is_funded_gate(gate(current_balance="not measured")) is False
    assert is_funded_gate(gate(current_balance=Decimal("NaN"))) is False
    assert is_funded_gate(gate(current_balance=-1)) is False
    # allowance: never None, and never an infinite one.
    assert is_funded_gate(gate(run_allowance=None)) is False
    assert is_funded_gate(gate(run_allowance=float("inf"))) is False
    assert is_funded_gate(gate(run_allowance=Decimal("Infinity"))) is False
    # A bool is an int in Python, so True would otherwise read as an allowance of
    # one credit and False as a balance of nothing measured rather than as the
    # unreadable gate each of them is.
    assert is_funded_gate(gate(run_allowance=True)) is False
    assert is_funded_gate(gate(run_allowance=False)) is False
    assert is_funded_gate(gate(current_balance=True)) is False
    # a missing field is a refusal, one field at a time
    for field in ("allowed", "reasons", "current_balance", "run_allowance"):
        partial = type(
            "Partial",
            (),
            {name: getattr(Valid, name) for name in FUNDED_GATE_FIELDS if name != field},
        )
        assert is_funded_gate(partial()) is False, field


def test_a_proof_and_a_blocker_refuse_an_as_of_that_is_not_a_date():
    proof = parse_capability_proof("store:2026-09-16:rss:za:feature")
    assert proof is not None
    assert proof.valid_on(date(2026, 9, 16)) is True
    with pytest.raises(ValueError, match="proof_as_of_invalid"):
        proof.valid_on("2026-09-16")
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    rows = reconcile_source_estate(inventory, estate_evidence(inventory))
    with pytest.raises(ValueError, match="blockers_as_of_invalid"):
        feature_blockers(rows, as_of="2026-09-16")
    assert feature_blockers(rows, as_of=date(2026, 9, 16))


def test_an_evidence_row_against_a_configured_state_the_estate_cannot_produce_is_refused():
    inventory = {("rss", m): {"fetch"} for m in POLICY_MARKETS}
    evidence = estate_evidence(inventory)
    evidence[("rss", "za", "fetch")]["state"] = "mostly_fine"
    row = {
        "source": "rss",
        "market": "za",
        "route": "fetch",
        "state": "qualified_live",
        "condition": "switch_on_unproven",
        "next_owner_action": "attach proof",
        "capability_proof": "store:2026-09-16:rss:za:feature",
        "qualified_contribution": 10,
    }
    with pytest.raises(ValueError, match="state_unknown:rss:za:fetch:mostly_fine"):
        reconcile_source_estate(inventory, evidence, [row])


def test_the_activation_policy_refuses_a_control_it_cannot_read():
    fields = {
        "min_row_integrity_pct": 90,
        "min_probe_observations": 20,
        "clean_crons_required": 3,
        "burn_overrun_pct": 25,
        "max_increments_open": 1,
        "quality_before_volume": True,
    }
    for field, bad in (
        ("min_row_integrity_pct", 0),
        ("min_row_integrity_pct", 101),
        ("min_probe_observations", 0),
        ("min_probe_observations", "twenty"),
        ("clean_crons_required", 0),
        ("burn_overrun_pct", -1),
        ("max_increments_open", 0),
        ("quality_before_volume", 1),
        ("quality_before_volume", "yes"),
    ):
        with pytest.raises(ValueError, match=f"activation_policy_invalid:{field}"):
            ActivationPolicy(**{**fields, field: bad})
    assert ActivationPolicy(**fields).quality_before_volume is True
