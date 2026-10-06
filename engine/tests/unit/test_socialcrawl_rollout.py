"""Unit tests for the SocialCrawl staged-rollout proposer.

The point of these is the money property: a ramp against a finite prepaid
balance must stop on its own. No network, no BigQuery.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import scripts.socialcrawl_rollout as rollout
from src.ingestion.connectors.socialcrawl import WAVE1_ROUTE_SPECS

# The ladder controls the activation gate reads, and one retained probe for the
# route s1 opens. A stage is proposed only when the gate allows it, so a fixture
# that carries no probe is a fixture of a ramp that cannot move.
PROBE_DIGEST = "a" * 64
# The fixture ladder is declared on the funded lane, the lane that spends the
# account whose balance and ledger the proposer reads. A ladder on any other
# lane is held before any stage is considered (tests at the end of this file).
POLICY = {
    "credential_lane": "ogilvy_funded",
    "min_runway_days": 45,
    "max_stages_per_day": 1,
    "clean_crons_required": 3,
    "balance_floor_hard": 5000,
    "burn_overrun_pct": 25,
    "min_row_integrity_pct": 90,
    "min_probe_observations": 20,
    "max_increments_open": 1,
    "quality_before_volume": True,
}
PROBES = {
    "tiktok/search/top": {
        "retained_reference": f"gs://staging-probes/{PROBE_DIGEST}.json",
        "payload_digest": PROBE_DIGEST,
        "normalizer_digest": PROBE_DIGEST,
        "observations": 30,
        "integrity_observations": 30,
    }
}
CFG = {
    "policy": POLICY,
    "capabilities": {"pagination": True, "audience": False},
    "retained_probes": PROBES,
    "stages": [
        {
            "id": "s1",
            "description": "cheap stage",
            "activation_route": "tiktok/search/top",
            "requires_capability": "pagination",
            "config_delta": {"socialcrawl.caps.tiktok_search_pages": 2},
            "credits_per_day": 42,
        },
        {
            "id": "s2",
            "description": "needs a capability we do not have",
            "activation_route": "tiktok/user/audience",
            "requires_capability": "audience",
            "config_delta": {"socialcrawl.caps.audience_weekly": True},
            "credits_per_day": 19,
        },
        {
            "id": "s3",
            "description": "very expensive stage",
            "activation_route": "instagram/post/comments",
            "requires_capability": None,
            "config_delta": {"socialcrawl.caps.instagram_comment_posts": 5},
            "credits_per_day": 5000,
        },
    ],
}

SOURCES = {"socialcrawl": {"enabled": True, "caps": {}}}
CLEAN = [{"d": f"2026-07-{d}", "rows": 3000, "ok": 3, "bad": 0} for d in (21, 22, 23)]


def _run(balance=19607, history=None, sources=None, cfg=None, burn=290):
    # load_sources is patched rather than Path.read_text: the activation gate
    # reads the connector's own syntax tree, and a blanket read_text patch would
    # hand it the sources file instead of the connector.
    with (
        patch.object(rollout, "load_rollout", return_value=cfg or CFG),
        patch.object(rollout, "load_sources", return_value=sources or SOURCES),
        patch.object(rollout, "fetch_balance", return_value=balance),
        patch.object(rollout, "fetch_history", return_value=CLEAN if history is None else history),
        patch.object(rollout, "_declared_burn_from_capacity", return_value=burn),
    ):
        return rollout.evaluate("2026-07-24")


def test_proposes_the_first_affordable_stage():
    p = _run()
    assert p.verdict == "PROPOSE"
    assert p.stage_id == "s1"
    assert p.config_delta == {"socialcrawl.caps.tiktok_search_pages": 2}


def test_allowed_daily_is_derived_from_balance_not_hardcoded():
    """The whole policy: 20,000 is all we get, so the ceiling follows the balance."""
    p = _run(balance=19607)
    assert p.allowed_daily == 19607 // 45
    p2 = _run(balance=9000)
    assert p2.allowed_daily == 9000 // 45
    assert p2.allowed_daily < p.allowed_daily


def test_ramp_stops_when_headroom_closes():
    """Balance still clears the runway floor, but leaves no room for the stage."""
    # 14000/45 = 311 allowed against a 290 burn, so 21/day of headroom vs a 42/day stage.
    p = _run(balance=14000)
    assert p.runway_days is not None
    assert p.runway_days >= 45
    assert p.headroom is not None
    assert p.headroom < 42
    assert p.verdict == "NO_PROPOSAL"
    assert any("headroom" in s for s in p.skipped)


def test_runway_floor_halts_before_the_ladder_is_even_read():
    """A balance under the floor is a HOLD, not a skipped-stage list."""
    p = _run(balance=13000)  # 13000/290 = 44.8 days, under the 45-day floor
    assert p.verdict == "HOLD"
    assert any(g.name == "runway_above_min" and not g.passed for g in p.gates)
    assert p.skipped == []


def test_a_stage_priced_beyond_the_balance_is_skipped_not_proposed():
    """s3 costs 5000/day; no balance in this ladder can ever fund it."""
    cfg = {**CFG, "capabilities": {"pagination": False, "audience": False}}
    p = _run(cfg=cfg)
    assert p.verdict == "NO_PROPOSAL"
    assert any(s.startswith("s3:") for s in p.skipped)


def test_missing_connector_capability_skips_not_proposes():
    cfg = {**CFG, "capabilities": {"pagination": False, "audience": False}}
    p = _run(cfg=cfg)
    assert p.verdict == "NO_PROPOSAL"
    assert any("s1" in s and "capability" in s for s in p.skipped)


def test_hard_balance_floor_halts_everything():
    p = _run(balance=4000)
    assert p.verdict == "HOLD"
    assert any(g.name == "balance_above_hard_floor" and not g.passed for g in p.gates)


def test_a_failed_cron_in_the_window_pauses_the_ramp():
    dirty = [
        {"d": "2026-07-21", "rows": 3000, "ok": 3, "bad": 0},
        {"d": "2026-07-22", "rows": 0, "ok": 2, "bad": 1},
        {"d": "2026-07-23", "rows": 3000, "ok": 3, "bad": 0},
    ]
    p = _run(history=dirty)
    assert p.verdict == "HOLD"
    assert any(g.name == "consecutive_clean_crons" and not g.passed for g in p.gates)


def test_unreadable_balance_is_unknown_not_a_green_light():
    p = _run(balance=None)
    assert p.verdict == "UNKNOWN"
    assert p.stage_id is None


def test_already_applied_stage_is_not_reproposed():
    applied = {"socialcrawl": {"enabled": True, "caps": {"tiktok_search_pages": 2}}}
    p = _run(sources=applied)
    assert p.stage_id != "s1"


def test_it_never_returns_a_config_write():
    """The proposer is read-only by contract; it emits a delta, never applies one."""
    p = _run()
    assert isinstance(p.config_delta, dict)
    assert "Apply by hand" in rollout.render(p)


# -- burn model ------------------------------------------------------------
# Burn used to be the declared `actual_units_target`, never a reading. On
# 27 Jul 2026 that read 290 against ~346 actually billed, so runway was
# overstated by a fifth and headroom by more than double. These pin the
# measured path and the fallback that bootstraps it.


def _days(credits, rows=3000):
    """A history window where each day carries a credit reading."""
    return [
        {"d": f"2026-07-{20 + i}", "rows": rows, "credits": c, "ok": 3, "bad": 0}
        for i, c in enumerate(credits)
    ]


def test_burn_is_measured_once_enough_days_carry_credits():
    p = _run(history=_days([340, 350, 346]))
    assert p.burn_source == "measured"
    assert p.burn_median == 346


def test_burn_falls_back_to_declared_before_the_column_has_data():
    """Days predating the column report no credits; that is not zero burn."""
    p = _run(history=CLEAN, burn=290)
    assert p.burn_source == "declared"
    assert p.burn_median == 290


def test_unmeasured_zero_days_are_dropped_not_averaged_in():
    """A zero from the pre-column era must not halve the median."""
    history = _days([0, 0, 0, 0, 340, 350, 346])
    p = _run(history=history)
    assert p.burn_source == "measured"
    assert p.burn_median == 346


def test_two_days_of_readings_is_not_yet_enough():
    p = _run(history=_days([340, 350]), burn=290)
    assert p.burn_source == "declared"
    assert p.burn_median == 290


def test_measured_burn_shortens_runway_and_headroom():
    """The bug this fixes: a low declared burn flatters both numbers."""
    declared = _run(history=CLEAN, burn=290)
    measured = _run(history=_days([346, 346, 346]), burn=290)
    assert measured.runway_days < declared.runway_days
    assert measured.headroom < declared.headroom


def test_burn_overrun_beyond_the_policy_pauses_the_ramp():
    """burn_overrun_pct was declared in the policy and never enforced."""
    cfg = {**CFG, "policy": {**CFG["policy"], "burn_overrun_pct": 25}}
    p = _run(history=_days([500, 510, 505]), cfg=cfg, burn=290)
    assert p.verdict == "HOLD"
    gate = next(g for g in p.gates if g.name == "burn_within_model")
    assert gate.passed is False
    assert "+74%" in gate.detail


def test_burn_inside_the_overrun_band_still_proposes():
    cfg = {**CFG, "policy": {**CFG["policy"], "burn_overrun_pct": 25}}
    p = _run(history=_days([300, 305, 300]), cfg=cfg, burn=290)
    assert p.verdict == "PROPOSE"
    assert next(g for g in p.gates if g.name == "burn_within_model").passed is True


def test_no_overrun_gate_while_burn_is_only_declared():
    """Comparing the declared number against itself is not evidence."""
    p = _run(history=CLEAN, burn=290)
    assert not any(g.name == "burn_within_model" for g in p.gates)


# -- the activation gate is the gate ---------------------------------------
# evaluate_route_activation used to have no caller at all: this script built its
# own gates and proposed a stage the estate's gate had never seen.


def test_a_stage_the_activation_gate_refuses_is_never_proposed():
    """No retained probe for the route means no proposal, whatever the budget says."""
    cfg = {**CFG, "retained_probes": {}}
    p = _run(cfg=cfg)
    assert p.verdict == "NO_PROPOSAL"
    assert p.stage_id is None
    assert any("probe_missing" in refusal for refusal in p.activation_refusals)
    assert any("s1" in skipped and "activation gate" in skipped for skipped in p.skipped)


def test_a_stage_that_names_no_vendor_route_cannot_be_probed_or_proposed():
    stages = [{**CFG["stages"][0], "activation_route": None}, *CFG["stages"][1:]]
    p = _run(cfg={**CFG, "stages": stages})
    assert p.verdict == "NO_PROPOSAL"
    assert any("names no vendor route" in skipped for skipped in p.skipped)


def test_a_volume_stage_waits_behind_an_unactivated_quality_stage():
    """The ordering gate the ladder file declares, enforced on the proposal itself."""
    stages = [
        {**CFG["stages"][0], "expect_rows_per_day": 900},
        {
            "id": "s0_quality",
            "description": "an accuracy stage that adds no rows",
            "activation_route": "tiktok/profile/region",
            "requires_capability": None,
            "config_delta": {"socialcrawl.caps.geo_verify": 60},
            "credits_per_day": 12,
            "expect_rows_per_day": 0,
        },
    ]
    p = _run(cfg={**CFG, "stages": stages})
    assert p.stage_id != "s1"
    refusals = dict(refusal.split(": ", 1) for refusal in p.activation_refusals)
    assert "quality_increment_pending" in refusals["s1"]
    assert "quality_increment_pending" not in refusals["s0_quality"]
    # The quality stage is held only by its own missing probe, never by ordering.
    assert "probe_missing" in refusals["s0_quality"]


def test_the_gate_reads_the_measured_balance_rather_than_assuming_one():
    """A balance the run could not read never reaches the gate as a number."""
    p = _run(balance=None)
    assert p.verdict == "UNKNOWN"
    assert p.activation_refusals == []
    reading = rollout.FundedGateReading(
        allowed=True, reasons=(), current_balance=None, run_allowance=0
    )
    from src.analysis.open_intelligence.source_policy import is_funded_gate

    assert is_funded_gate(reading) is True
    assert is_funded_gate(object()) is False


# -- second hostile review closure -----------------------------------------


def test_a_day_on_which_every_run_failed_is_a_day_the_clean_window_sees():
    """A total failure day carries no rows and no successes, so it used to vanish.

    The window kept only days with rows or successes, which dropped the failure
    day entirely and let the days either side of it read as consecutive clean
    runs. The ramp then climbed over the top of an outage.
    """
    outage = [
        {"d": "2026-07-20", "rows": 3000, "ok": 3, "bad": 0},
        {"d": "2026-07-21", "rows": 3000, "ok": 3, "bad": 0},
        {"d": "2026-07-22", "rows": 0, "ok": 0, "bad": 4},
        {"d": "2026-07-23", "rows": 3000, "ok": 3, "bad": 0},
    ]
    p = _run(history=outage)
    assert p.verdict == "HOLD"
    gate = next(g for g in p.gates if g.name == "consecutive_clean_crons")
    assert gate.passed is False
    # Three genuinely clean days in a row still propose, so this is the outage
    # and not a window that refuses everything.
    clean = [*outage[:2], {"d": "2026-07-23", "rows": 3000, "ok": 3, "bad": 0}]
    assert _run(history=clean).verdict == "PROPOSE"


def test_the_gate_is_given_route_states_and_not_the_source_switch_state():
    """The request used to carry the state of the SOURCE, read from its switch alone.

    The source state says nothing about a route the configuration switched off
    underneath it, and the ladder names vendor endpoints the estate does not model
    by those names at all, so "the state of the route it opens" was the source's
    state wearing the route's name.
    """
    states = rollout._socialcrawl_route_states(SOURCES)
    assert set(states) == set(rollout.POLICY_MARKETS)
    assert states["za"], "no socialcrawl routes were modelled at all"
    assert set(states["za"]) & set(WAVE1_ROUTE_SPECS)
    # The ladder's vendor routes are a different vocabulary from the estate's, so
    # none of them is modelled and the weakest modelled route state is what the
    # request carries.
    assert "tiktok/search/top" not in states["za"]
    assert rollout._route_state_for(states["za"], "tiktok/search/top") == min(
        states["za"].values(), key=rollout.STATE_ORDER.index
    )
    # A route the estate DOES model answers for itself, weaker or stronger.
    named = dict(states["za"])
    named["tiktok/search/top"] = "intentionally_excluded"
    assert rollout._route_state_for(named, "tiktok/search/top") == "intentionally_excluded"
    # One phase switched off drags the state the gate is given below the source
    # state, which reads approved_unproven from the switch alone.
    off = {"socialcrawl": {"enabled": True, "caps": {}, "phases": ["discover"]}}
    narrowed = rollout._socialcrawl_route_states(off)
    assert narrowed["za"]["news"] == "intentionally_excluded"
    assert rollout._route_state_for(narrowed["za"], "tiktok/search/top") == "intentionally_excluded"
    p = _run(sources=off)
    assert p.verdict == "NO_PROPOSAL"
    assert any("route_not_approved" in refusal for refusal in p.activation_refusals)


def test_a_retained_probe_that_cannot_be_read_is_named_rather_than_read_as_absent():
    """An unreadable probe is not a missing probe, and the refusal has to say which."""
    with pytest.raises(ValueError, match="retained_probe_unreadable:tiktok/search/top"):
        rollout.retained_probes({"retained_probes": {"tiktok/search/top": "see the ticket"}})
    assert rollout.retained_probes({"retained_probes": {}}) == {}
    read = rollout.retained_probes(CFG)
    assert set(read) == {"tiktok/search/top"}
    assert read["tiktok/search/top"].payload_digest == PROBE_DIGEST


def test_a_stage_the_activation_ladder_does_not_carry_is_refused_not_guessed_at():
    refusal = rollout.activation_refusal(
        None,
        ladder=(),
        policy=rollout.activation_policy(CFG),
        probes={},
        gate=rollout.FundedGateReading(
            allowed=True, reasons=(), current_balance=19607, run_allowance=100
        ),
        clean_run_ids=("a", "b", "c"),
        burn_overrun_pct=0,
        increments_open=0,
        route_states=rollout._socialcrawl_route_states(SOURCES),
        activated=(),
    )
    assert "is not on the activation ladder" in refusal


def test_a_window_shorter_than_the_policy_asks_for_is_not_a_clean_window():
    """Three clean runs means three, and two clean runs is not three of them.

    The window is sliced to the length the policy asks for, so a history with
    fewer runs than that slices to itself and every run in it can be clean. The
    length is what separates a short history from a satisfied policy: a route
    activated off two clean runs is activated off evidence nobody asked for.
    """
    assert POLICY["clean_crons_required"] == 3
    short = [{"d": f"2026-07-{d}", "rows": 3000, "ok": 3, "bad": 0} for d in (22, 23)]
    p = _run(history=short)
    assert p.verdict == "HOLD"
    gate = next(g for g in p.gates if g.name == "consecutive_clean_crons")
    assert gate.passed is False
    assert "2 of 3 clean runs in window" in gate.detail
    # An empty history is the same refusal and not a vacuous pass.
    empty = _run(history=[])
    assert empty.verdict == "HOLD"
    assert next(g for g in empty.gates if g.name == "consecutive_clean_crons").passed is False


# Which account the balance is read from.
#
# The burn and runway gates are only as true as the balance they divide. The
# funded account (secret SOCIALCRAWL_OGILVY_API_KEY) holds the credits; the
# account behind SOCIALCRAWL_API_KEY holds none. Reading the empty account made
# every gate see a balance of zero, so the balance read goes through the repo's
# secret reader for the funded secret and never through that environment value.

FUNDED_SECRET_ID = "SOCIALCRAWL_OGILVY_API_KEY"


class _BalanceResponse:
    def __init__(self, balance):
        self._balance = balance

    def json(self):
        return {"success": True, "data": {"balance": self._balance}}


def _fetch_balance_with(monkeypatch, secret_value, *, balance=250041):
    monkeypatch.setenv("SOCIALCRAWL_API_KEY", "zero-balance-env-key")
    secret_calls: list[str] = []
    sent_keys: list[str] = []

    def _secret(secret_id, *_args, **_kwargs):
        secret_calls.append(secret_id)
        return secret_value

    def _get(url, headers=None, timeout=None):
        assert url == rollout.BALANCE_URL
        sent_keys.append((headers or {}).get("x-api-key"))
        return _BalanceResponse(balance)

    with (
        patch.object(rollout, "get_secret", side_effect=_secret),
        patch.object(rollout.requests, "get", side_effect=_get),
    ):
        result = rollout.fetch_balance()
    return result, secret_calls, sent_keys


def test_balance_is_read_from_the_funded_secret_not_the_zero_balance_env_key(monkeypatch):
    result, secret_calls, sent_keys = _fetch_balance_with(monkeypatch, "funded-key")
    assert result == 250041
    assert secret_calls == [FUNDED_SECRET_ID]
    assert sent_keys == ["funded-key"]


def test_the_balance_secret_is_the_one_the_funded_lane_reads():
    from src.analysis.open_intelligence import funded_lane_runtime

    assert rollout.BALANCE_SECRET_ID == FUNDED_SECRET_ID
    assert funded_lane_runtime.SECRET_ID == FUNDED_SECRET_ID


@pytest.mark.parametrize("secret_value", ["", "  funded-key  "])
def test_a_missing_or_malformed_funded_secret_is_unreadable_not_the_env_key(
    monkeypatch, secret_value
):
    result, secret_calls, sent_keys = _fetch_balance_with(monkeypatch, secret_value)
    assert result is None
    assert secret_calls == [FUNDED_SECRET_ID]
    assert sent_keys == []


def test_the_unreadable_balance_gate_names_the_funded_secret_and_no_value(monkeypatch):
    p = _run(balance=None)
    gate = next(g for g in p.gates if g.name == "balance_readable")
    assert gate.passed is False
    assert FUNDED_SECRET_ID in gate.detail
    assert "SOCIALCRAWL_API_KEY unset" not in gate.detail


# Balance and burn describe the same account.
#
# The balance is the funded account's, so the burn and the clean runs come from
# the funded ledger (trends_v2_staging_funded.socialcrawl_credit_ledger_v1)
# rather than from pipeline_runs, which records the jhb_core cron spending the
# other key. And the ladder's stages are only proposed for a ladder declared on
# the lane that spends that account: a ladder on the jhb_core lane cannot spend
# funded credits, so it is held with a named reason, as the zero balance held it.


class _LedgerJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class _LedgerClient:
    def __init__(self, calls, rows, **kwargs):
        calls.append({"client": kwargs})
        self._calls = calls
        self._rows = rows

    def query(self, sql, job_config=None, **kwargs):
        params = {p.name: p.value for p in job_config.query_parameters}
        self._calls.append({"sql": sql, "params": params, "kwargs": kwargs})
        return _LedgerJob(self._rows)


def _ledger_history(rows):
    from decimal import Decimal
    from types import SimpleNamespace

    calls: list[dict] = []
    ledger_rows = [
        SimpleNamespace(d=d, credit_total=Decimal(credits), ok=ok, bad=bad)
        for d, credits, ok, bad in rows
    ]

    def _client(**kwargs):
        return _LedgerClient(calls, ledger_rows, **kwargs)

    with patch("google.cloud.bigquery.Client", side_effect=_client):
        history = rollout.fetch_history("2026-09-23")
    return history, calls


def test_history_is_read_from_the_funded_ledger_not_pipeline_runs():
    history, calls = _ledger_history([("2026-09-22", "61.5", 1, 0)])
    query = next(c for c in calls if "sql" in c)
    assert (
        "`ogilvy-trends-v2.trends_v2_staging_funded.socialcrawl_credit_ledger_v1`" in query["sql"]
    )
    assert "pipeline_runs" not in query["sql"]
    assert "trends_v2_dev" not in query["sql"]
    assert "credential_lane = @credential_lane" in query["sql"]
    assert query["params"]["credential_lane"] == "ogilvy_funded"
    assert str(query["params"]["start"]) == "2026-09-10"
    assert str(query["params"]["end"]) == "2026-09-23"
    # A fractional debit rounds up: the burn a runway divides by is never understated.
    assert history == [{"d": "2026-09-22", "rows": 0, "credits": 62, "ok": 1, "bad": 0}]


def test_the_ledger_history_counts_only_phase_debits_and_complete_closes():
    _history, calls = _ledger_history([])
    sql = next(c for c in calls if "sql" in c)["sql"]
    assert "event_type IN ('phase_close', 'attribution_gap')" in sql
    assert "budget_debit_credits" in sql
    assert "attribution_state = 'complete'" in sql


def test_the_ledger_and_the_balance_are_the_same_lane():
    assert rollout.BALANCE_CREDENTIAL_LANE == "ogilvy_funded"
    assert rollout.LEDGER_DATASET == "trends_v2_staging_funded"


def _held(cfg_policy_lane):
    policy = dict(POLICY)
    if cfg_policy_lane is None:
        policy.pop("credential_lane")
    else:
        policy["credential_lane"] = cfg_policy_lane
    return _run(balance=250041, cfg={**CFG, "policy": policy})


def test_a_ladder_on_the_jhb_core_lane_is_held_against_the_funded_balance():
    p = _held("jhb_core")
    assert p.verdict == "HOLD"
    assert p.stage_id is None
    assert p.config_delta == {}
    gate = next(g for g in p.gates if g.name == "ladder_lane_spends_balance_account")
    assert gate.passed is False
    assert "jhb_core" in gate.detail
    assert "SOCIALCRAWL_OGILVY_API_KEY" in gate.detail
    assert "ogilvy_funded" in gate.detail
    # Nothing is derived from a balance the ladder's lane cannot spend.
    assert p.runway_days is None
    assert p.allowed_daily is None
    assert p.headroom is None


@pytest.mark.parametrize("lane", [None, "", "prod", "OGILVY_FUNDED"])
def test_an_undeclared_or_unknown_ladder_lane_is_held_not_assumed(lane):
    p = _held(lane)
    assert p.verdict == "HOLD"
    assert p.stage_id is None
    gate = next(g for g in p.gates if g.name == "ladder_lane_spends_balance_account")
    assert gate.passed is False
    assert "credential_lane" in gate.detail


def test_a_ladder_on_the_funded_lane_is_evaluated_on_the_funded_numbers():
    p = _held("ogilvy_funded")
    gate = next(g for g in p.gates if g.name == "ladder_lane_spends_balance_account")
    assert gate.passed is True
    assert p.allowed_daily == 250041 // 45
    assert p.verdict == "PROPOSE"


def test_the_shipped_ladder_declares_the_jhb_core_lane_and_is_held():
    shipped = rollout.load_rollout()
    assert (shipped.get("policy") or {}).get("credential_lane") == "jhb_core"
    p = _run(balance=250041, cfg=shipped)
    assert p.verdict == "HOLD"
    assert p.stage_id is None
    assert not next(g for g in p.gates if g.name == "ladder_lane_spends_balance_account").passed
