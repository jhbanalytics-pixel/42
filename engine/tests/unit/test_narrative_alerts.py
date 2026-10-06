"""Narrative alerts: the BSA preset, the event and threshold rules, acknowledgement and the
protected test sink, with detection lag, acknowledgement lag and source freshness apart."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from src.analysis.open_intelligence import client_overlay as co
from src.analysis.open_intelligence import narrative_alerts as na
from src.analysis.open_intelligence.brain_contract import canonical_digest

ROOT = Path(__file__).resolve().parents[2]
PRESET_PATH = ROOT / "configs" / "narrative_alerts" / "bsa.yaml"
T0 = datetime(2026, 9, 25, 6, 0, tzinfo=UTC)
CALIBRATION_REF = "c" * 64
MONITOR = {"principal": "monitor@example.test", "role": "narrative_monitor"}


def iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def bsa_overlay():
    return co.compiled_overlay_for_brand("bsa")


def bsa_policy():
    return na.load_alert_policy("bsa", overlay=bsa_overlay())


def frozen(document: dict, rule_id: str, **overrides) -> dict:
    """A fixture calibration; the shipped preset carries none."""
    changed = copy.deepcopy(document)
    changed["narrative_rules"][rule_id]["calibration"] = {
        "state": "frozen",
        "calibration_ref": CALIBRATION_REF,
        "momentum_threshold": 2.0,
        "minimum_independent_evidence": 3,
        "observation_window_seconds": 86400,
        "cooldown_seconds": 21600,
        **overrides,
    }
    return changed


def raw_preset() -> dict:
    return yaml.safe_load(PRESET_PATH.read_text(encoding="utf-8"))


def calibrated_policy(*rule_ids: str) -> dict:
    document = raw_preset()
    for rule_id in rule_ids:
        document = frozen(document, rule_id)
    return na.compile_alert_policy(document, overlay=bsa_overlay())


def evidence(index: int, *, published: datetime, available: datetime) -> dict:
    return {
        "evidence_id": f"ev_{index:03d}",
        "evidence_version": f"{index:064x}",
        "published_at": iso(published),
        "available_at": iso(available),
    }


def signal(run_id: str, *, state: str = "ready", velocity: str = "rising") -> dict:
    return {
        "signal_id": "sig_" + "a" * 64,
        "run_id": run_id,
        "evidence_state": state,
        "velocity_band": velocity,
        "breadth_band": "broad",
        "agreement": "agreeing",
        "market": "za",
        "lineage_digest": "b" * 64,
    }


def narrative(rule_id: str = "safety_and_crime", **overrides) -> dict:
    value = {
        "kind": "narrative",
        "rule_id": rule_id,
        "previous": signal("run_1", velocity="steady"),
        "current": signal("run_2"),
        "momentum": 2.5,
        "independent_evidence_count": 3,
        "window_seconds": 86400,
        "evidence": [
            evidence(1, published=T0 - timedelta(hours=9), available=T0 - timedelta(hours=3)),
            evidence(2, published=T0 - timedelta(hours=8), available=T0 - timedelta(hours=2)),
            evidence(3, published=T0 - timedelta(hours=10), available=T0 - timedelta(hours=4)),
        ],
        "counter_signals": [],
    }
    if rule_id == "safety_and_crime":
        for item in value["evidence"]:
            item["evidence_type"] = "reporting"
    value.update(overrides)
    return value


def advisory(*, revision: str = "2026-09-24.1", verified: bool = True) -> dict:
    return {
        "kind": "advisory_revision",
        "rule_id": "advisory_revision",
        "advisory": {
            "issuer": "gov.uk",
            "advisory_id": "south-africa",
            "revision": revision,
        },
        "verification": {
            "state": "verified" if verified else "unverified",
            "evidence_ref": "ev_010",
            "verified_at": iso(T0 - timedelta(minutes=30)) if verified else None,
        },
        "evidence": [
            evidence(10, published=T0 - timedelta(hours=5), available=T0 - timedelta(hours=1)),
        ],
    }


def impersonation(*, verified: bool) -> dict:
    return {
        "kind": "official_account_impersonation",
        "rule_id": "official_account_impersonation",
        "account": {"platform": "x", "handle": "@BrandSouthAfrica_"},
        "verification": {
            "state": "verified" if verified else "unverified",
            "evidence_ref": "ev_020",
            "verified_at": iso(T0 - timedelta(minutes=10)) if verified else None,
        },
        "evidence": [
            evidence(20, published=T0 - timedelta(hours=2), available=T0 - timedelta(hours=1)),
        ],
    }


# Preset


def test_bsa_preset_binds_to_the_overlay_risk_topics_exactly():
    policy = bsa_policy()
    overlay = bsa_overlay()
    risk_rules = [
        rule_id
        for rule_id, rule in policy["narrative_rules"].items()
        if rule["source"] == "risk_topic"
    ]
    assert risk_rules == list(overlay["risk_topics"])
    assert policy["overlay_digest"] == overlay["overlay_digest"]
    assert policy["narrative_rules"]["tariff_trade_access"]["parent_dimension"] == "exports"
    assert policy["delivery"] == "test_sink"
    assert policy["policy_digest"] == canonical_digest(
        {key: value for key, value in na._thaw(policy).items() if key != "policy_digest"}
    )


def test_bsa_preset_ships_no_invented_threshold_or_owner():
    policy = bsa_policy()
    for rule in policy["narrative_rules"].values():
        assert dict(rule["calibration"]) == {"state": "uncalibrated"}
    assert policy["acknowledgement"]["operational_owner"] == "unknown"
    assert policy["acknowledgement"]["escalation_contact"] == "unknown"


def test_preset_refuses_a_risk_set_that_differs_from_the_overlay():
    document = raw_preset()
    del document["narrative_rules"]["energy_water_infrastructure"]
    with pytest.raises(na.NarrativeAlertInvalid, match="policy_risk_topics_mismatch"):
        na.compile_alert_policy(document, overlay=bsa_overlay())


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("delivery",), "email", "policy_delivery_invalid"),
        (("brand_config_id",), "general", "policy_binding_invalid"),
        (("acknowledgement", "window_seconds"), 0, "policy_acknowledgement_invalid"),
        (("acknowledgement", "assigned_role"), "someone_else", "policy_acknowledgement_invalid"),
        (
            ("narrative_rules", "tariff_trade_access", "parent_dimension"),
            "not_a_dimension",
            "policy_rule_invalid",
        ),
        (("event_rules", "advisory_revision", "risk"), "not_a_risk", "policy_event_rule_invalid"),
    ],
)
def test_preset_refuses_invalid_fields(path, value, code):
    document = raw_preset()
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(na.NarrativeAlertInvalid, match=code):
        na.compile_alert_policy(document, overlay=bsa_overlay())


def test_preset_refuses_a_partial_calibration():
    document = frozen(raw_preset(), "safety_and_crime")
    del document["narrative_rules"]["safety_and_crime"]["calibration"]["cooldown_seconds"]
    with pytest.raises(na.NarrativeAlertInvalid, match="policy_calibration_invalid"):
        na.compile_alert_policy(document, overlay=bsa_overlay())


def test_a_changed_threshold_is_a_different_policy_digest():
    first = calibrated_policy("safety_and_crime")
    document = frozen(raw_preset(), "safety_and_crime", momentum_threshold=2.1)
    second = na.compile_alert_policy(document, overlay=bsa_overlay())
    assert first["policy_digest"] != second["policy_digest"]


# Narrative threshold rules


def test_uncalibrated_rule_holds_even_on_a_strong_spike():
    outcome = na.evaluate_observation(
        bsa_policy(), narrative(momentum=50.0), detected_at=T0, prior_alerts=()
    )
    assert outcome == {"outcome": "held", "reason": "threshold_uncalibrated", "record": None}


def test_narrative_spike_alerts_with_the_three_timings_apart():
    policy = calibrated_policy("safety_and_crime")
    outcome = na.evaluate_observation(policy, narrative(), detected_at=T0, prior_alerts=())
    assert outcome["outcome"] == "alert"
    alert = outcome["record"]
    assert alert["rule_id"] == "safety_and_crime"
    assert alert["trigger"] == "narrative_threshold"
    assert alert["material_change"] == ["velocity_band"]
    assert alert["triggered_at"] == iso(T0)
    assert alert["source_published_at"] == iso(T0 - timedelta(hours=8))
    assert alert["source_available_at"] == iso(T0 - timedelta(hours=2))
    assert alert["timing"] == {
        "source_freshness_seconds": 6 * 3600,
        "detection_lag_seconds": 2 * 3600,
        "acknowledgement_lag_seconds": None,
    }
    assert alert["collection_cadence"] == "daily"
    assert alert["external_response"] == "human_review_required"
    assert alert["response"] == {
        "alert_id": alert["alert_id"],
        "evidence_version": canonical_digest(alert["evidence"]),
        "triggered_at": iso(T0),
        "assigned_role": "narrative_monitor",
        "acknowledged_at": None,
        "acknowledged_by": None,
        "status": "open",
        "decision_ref": None,
        "follow_up_at": None,
    }
    assert alert["policy_digest"] == policy["policy_digest"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"momentum": 1.99}, "below_threshold"),
        ({"independent_evidence_count": 2}, "insufficient_independent_evidence"),
        ({"window_seconds": 3600}, "observation_window_mismatch"),
        ({"current": signal("run_2", state="thin")}, "readiness_not_ready"),
        ({"current": signal("run_2", velocity="steady")}, "no_material_change"),
    ],
)
def test_narrative_rule_holds_below_its_frozen_conditions(overrides, reason):
    policy = calibrated_policy("safety_and_crime")
    outcome = na.evaluate_observation(
        policy, narrative(**overrides), detected_at=T0, prior_alerts=()
    )
    assert outcome == {"outcome": "held", "reason": reason, "record": None}


def test_tariff_subcluster_triggers_on_its_own_threshold():
    policy = calibrated_policy("tariff_trade_access")
    outcome = na.evaluate_observation(
        policy, narrative("tariff_trade_access"), detected_at=T0, prior_alerts=()
    )
    assert outcome["outcome"] == "alert"
    assert outcome["record"]["risk"] == "tariff_trade_access"


def test_repeated_run_is_a_duplicate_and_a_new_run_inside_cooldown_is_suppressed():
    policy = calibrated_policy("safety_and_crime")
    first = na.evaluate_observation(policy, narrative(), detected_at=T0, prior_alerts=())
    alert = first["record"]
    again = na.evaluate_observation(
        policy, narrative(), detected_at=T0 + timedelta(minutes=5), prior_alerts=(alert,)
    )
    assert again == {"outcome": "duplicate", "reason": "already_alerted", "record": None}
    later_run = narrative(previous=signal("run_2", velocity="steady"), current=signal("run_3"))
    cooled = na.evaluate_observation(
        policy, later_run, detected_at=T0 + timedelta(hours=5), prior_alerts=(alert,)
    )
    assert cooled == {"outcome": "duplicate", "reason": "cooldown", "record": None}
    after = na.evaluate_observation(
        policy, later_run, detected_at=T0 + timedelta(hours=7), prior_alerts=(alert,)
    )
    assert after["outcome"] == "alert"


def test_counter_signal_is_shown_where_observed_and_stated_absent_otherwise():
    policy = calibrated_policy("corruption_and_state_capacity")
    counter = {
        "kind": "asset_recovery",
        **evidence(5, published=T0 - timedelta(hours=4), available=T0 - timedelta(hours=1)),
    }
    observed = na.evaluate_observation(
        policy,
        narrative("corruption_and_state_capacity", counter_signals=[counter]),
        detected_at=T0,
        prior_alerts=(),
    )["record"]
    assert observed["counter_signals"] == {"state": "observed", "items": [counter]}
    absent = na.evaluate_observation(
        policy, narrative("corruption_and_state_capacity"), detected_at=T0, prior_alerts=()
    )["record"]
    assert absent["counter_signals"] == {"state": "absent", "items": []}
    other = calibrated_policy("safety_and_crime")
    unconfigured = na.evaluate_observation(other, narrative(), detected_at=T0, prior_alerts=())
    assert unconfigured["record"]["counter_signals"] == {"state": "not_configured", "items": []}


def test_counter_signal_of_an_unconfigured_kind_is_refused():
    policy = calibrated_policy("corruption_and_state_capacity")
    counter = {
        "kind": "press_release",
        **evidence(5, published=T0 - timedelta(hours=4), available=T0 - timedelta(hours=1)),
    }
    with pytest.raises(na.NarrativeAlertInvalid, match="counter_signal_invalid"):
        na.evaluate_observation(
            policy,
            narrative("corruption_and_state_capacity", counter_signals=[counter]),
            detected_at=T0,
            prior_alerts=(),
        )


def test_safety_evidence_types_stay_distinct():
    policy = calibrated_policy("safety_and_crime")
    typed = narrative(
        evidence=[
            {
                **evidence(1, published=T0 - timedelta(hours=9), available=T0 - timedelta(hours=3)),
                "evidence_type": "lived_experience",
            },
            {
                **evidence(2, published=T0 - timedelta(hours=8), available=T0 - timedelta(hours=2)),
                "evidence_type": "reporting",
            },
            {
                **evidence(
                    3, published=T0 - timedelta(hours=10), available=T0 - timedelta(hours=4)
                ),
                "evidence_type": "reporting",
            },
        ]
    )
    alert = na.evaluate_observation(policy, typed, detected_at=T0, prior_alerts=())["record"]
    assert alert["evidence_types"] == {"lived_experience": 1, "reporting": 2}
    typed["evidence"][0]["evidence_type"] = "rumour"
    with pytest.raises(na.NarrativeAlertInvalid, match="evidence_invalid"):
        na.evaluate_observation(policy, typed, detected_at=T0, prior_alerts=())


# Event rules


def test_verified_advisory_revision_alerts_regardless_of_volume():
    outcome = na.evaluate_observation(bsa_policy(), advisory(), detected_at=T0, prior_alerts=())
    assert outcome["outcome"] == "alert"
    alert = outcome["record"]
    assert alert["trigger"] == "verified_event"
    assert alert["risk"] == "safety_and_crime"
    assert alert["timing"]["detection_lag_seconds"] == 3600
    assert alert["timing"]["source_freshness_seconds"] == 4 * 3600
    assert alert["readiness"] == "separate_from_trend_promotion"


def test_repeated_ingestion_of_the_same_advisory_revision_is_one_alert():
    policy = bsa_policy()
    first = na.evaluate_observation(policy, advisory(), detected_at=T0, prior_alerts=())["record"]
    again = na.evaluate_observation(
        policy, advisory(), detected_at=T0 + timedelta(days=2), prior_alerts=(first,)
    )
    assert again == {"outcome": "duplicate", "reason": "already_alerted", "record": None}
    next_revision = na.evaluate_observation(
        policy,
        advisory(revision="2026-09-26.1"),
        detected_at=T0 + timedelta(days=2),
        prior_alerts=(first,),
    )
    assert next_revision["outcome"] == "alert"


def test_unverified_advisory_is_held():
    outcome = na.evaluate_observation(
        bsa_policy(), advisory(verified=False), detected_at=T0, prior_alerts=()
    )
    assert outcome == {"outcome": "held", "reason": "advisory_unverified", "record": None}


def test_unverified_impersonation_is_a_candidate_never_an_escalation():
    outcome = na.evaluate_observation(
        bsa_policy(), impersonation(verified=False), detected_at=T0, prior_alerts=()
    )
    assert outcome["outcome"] == "candidate"
    record = outcome["record"]
    assert record["record_kind"] == "impersonation_candidate"
    assert record["verification_state"] == "unverified"
    assert "response" not in record
    assert record["timing"]["detection_lag_seconds"] == 3600


def test_verified_impersonation_escalates_once():
    policy = bsa_policy()
    first = na.evaluate_observation(
        policy, impersonation(verified=True), detected_at=T0, prior_alerts=()
    )
    assert first["outcome"] == "alert"
    assert first["record"]["risk"] == "misinformation_targeting_the_nation_brand"
    again = na.evaluate_observation(
        policy,
        impersonation(verified=True),
        detected_at=T0 + timedelta(hours=1),
        prior_alerts=(first["record"],),
    )
    assert again["outcome"] == "duplicate"


def test_detection_before_source_availability_is_refused():
    with pytest.raises(na.NarrativeAlertInvalid, match="detection_before_availability"):
        na.evaluate_observation(
            bsa_policy(), advisory(), detected_at=T0 - timedelta(hours=2), prior_alerts=()
        )


def test_naive_detection_time_is_refused():
    with pytest.raises(na.NarrativeAlertInvalid, match="timezone_required"):
        na.evaluate_observation(
            bsa_policy(), advisory(), detected_at=T0.replace(tzinfo=None), prior_alerts=()
        )


# Acknowledgement


def advisory_alert(policy=None):
    policy = policy or bsa_policy()
    return na.evaluate_observation(policy, advisory(), detected_at=T0, prior_alerts=())["record"]


def test_acknowledgement_seconds_is_the_plan_kernel():
    assert na.acknowledgement_seconds(T0, T0 + timedelta(seconds=90)) == 90.0
    with pytest.raises(ValueError, match="timezone_required"):
        na.acknowledgement_seconds(T0.replace(tzinfo=None), T0)
    with pytest.raises(ValueError, match="acknowledgement_before_trigger"):
        na.acknowledgement_seconds(T0, T0 - timedelta(seconds=1))


@pytest.mark.parametrize(
    ("seconds", "status", "within"),
    [(3599, "acknowledged", True), (3601, "acknowledged_late", False)],
)
def test_acknowledgement_inside_and_outside_the_hour(seconds, status, within):
    policy = bsa_policy()
    alert = advisory_alert(policy)
    at = T0 + timedelta(seconds=seconds)
    acked = na.acknowledge_alert(alert, MONITOR, at, policy=policy)
    assert acked["response"]["acknowledged_at"] == iso(at)
    assert acked["response"]["acknowledged_by"] == MONITOR
    assert acked["response"]["status"] == status
    assert acked["timing"]["acknowledgement_lag_seconds"] == seconds
    assert acked["acknowledged_within_window"] is within
    assert acked["timing"]["detection_lag_seconds"] == alert["timing"]["detection_lag_seconds"]
    assert acked["external_response"] == "human_review_required"
    assert acked["response"]["decision_ref"] is None
    assert alert["response"]["acknowledged_at"] is None


def test_acknowledgement_uses_the_trigger_time_not_the_source_time():
    policy = bsa_policy()
    alert = advisory_alert(policy)
    acked = na.acknowledge_alert(alert, MONITOR, T0 + timedelta(minutes=10), policy=policy)
    assert acked["timing"]["acknowledgement_lag_seconds"] == 600


def test_duplicate_acknowledgement_is_refused():
    policy = bsa_policy()
    acked = na.acknowledge_alert(
        advisory_alert(policy), MONITOR, T0 + timedelta(minutes=1), policy=policy
    )
    with pytest.raises(na.NarrativeAlertInvalid, match="acknowledgement_duplicate"):
        na.acknowledge_alert(acked, MONITOR, T0 + timedelta(minutes=2), policy=policy)


@pytest.mark.parametrize(
    "actor",
    [
        {"principal": "viewer@example.test", "role": "viewer"},
        {"principal": "", "role": "narrative_monitor"},
        {"role": "narrative_monitor"},
        None,
    ],
)
def test_unauthorized_acknowledgement_is_refused(actor):
    policy = bsa_policy()
    with pytest.raises(na.NarrativeAlertInvalid, match="acknowledgement_unauthorized"):
        na.acknowledge_alert(advisory_alert(policy), actor, T0, policy=policy)


def test_acknowledgement_under_another_policy_is_refused():
    policy = bsa_policy()
    other = calibrated_policy("safety_and_crime")
    with pytest.raises(na.NarrativeAlertInvalid, match="alert_policy_mismatch"):
        na.acknowledge_alert(advisory_alert(policy), MONITOR, T0, policy=other)


def test_acknowledging_a_candidate_is_refused():
    policy = bsa_policy()
    candidate = na.evaluate_observation(
        policy, impersonation(verified=False), detected_at=T0, prior_alerts=()
    )["record"]
    with pytest.raises(na.NarrativeAlertInvalid, match="alert_invalid"):
        na.acknowledge_alert(candidate, MONITOR, T0, policy=policy)


def test_absent_acknowledgement_is_open_then_overdue_with_the_owner_gap():
    policy = bsa_policy()
    alert = advisory_alert(policy)
    inside = na.alert_status(alert, policy=policy, now=T0 + timedelta(seconds=3600))
    assert inside == {
        "alert_id": alert["alert_id"],
        "rule_id": "advisory_revision",
        "status": "open",
        "acknowledgement_lag_seconds": None,
        "escalation": None,
    }
    overdue = na.alert_status(alert, policy=policy, now=T0 + timedelta(seconds=3601))
    assert overdue["status"] == "unacknowledged_overdue"
    assert overdue["acknowledgement_lag_seconds"] is None
    assert overdue["escalation"] == {
        "assigned_role": "narrative_monitor",
        "operational_owner": "unknown",
        "escalation_contact": "unknown",
        "overdue_seconds": 1,
    }


def test_status_of_an_acknowledged_alert_carries_its_lag():
    policy = bsa_policy()
    acked = na.acknowledge_alert(
        advisory_alert(policy), MONITOR, T0 + timedelta(seconds=3601), policy=policy
    )
    status = na.alert_status(acked, policy=policy, now=T0 + timedelta(days=1))
    assert status["status"] == "acknowledged_late"
    assert status["acknowledgement_lag_seconds"] == 3601
    assert status["escalation"] is None


def test_status_with_a_non_utc_offset_is_the_same_instant():
    policy = bsa_policy()
    alert = advisory_alert(policy)
    sast = timezone(timedelta(hours=2))
    now = (T0 + timedelta(seconds=3601)).astimezone(sast)
    assert na.alert_status(alert, policy=policy, now=now)["status"] == "unacknowledged_overdue"


# Test sink


def test_sink_refuses_a_remote_or_missing_root(tmp_path):
    with pytest.raises(na.NarrativeAlertInvalid, match="sink_root_invalid"):
        na.TestSink("gs://bucket/alerts", policy=bsa_policy())
    with pytest.raises(na.NarrativeAlertInvalid, match="sink_root_invalid"):
        na.TestSink(tmp_path / "missing", policy=bsa_policy())
    with pytest.raises(na.NarrativeAlertInvalid, match="sink_root_invalid"):
        na.TestSink("relative/path", policy=bsa_policy())


def test_sink_writes_once_and_never_overwrites(tmp_path):
    policy = bsa_policy()
    sink = na.TestSink(tmp_path, policy=bsa_policy())
    alert = advisory_alert(policy)
    receipt = sink.deliver("alert_raised", alert, at=T0)
    path = Path(receipt["path"])
    assert path.parent == tmp_path
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["delivery"] == "test_sink"
    assert stored["event"] == "alert_raised"
    assert stored["record"] == alert
    assert receipt["sha256"] == na.sha256_file(path)
    with pytest.raises(na.NarrativeAlertInvalid, match="sink_entry_exists"):
        sink.deliver("alert_raised", alert, at=T0)
    assert json.loads(path.read_text(encoding="utf-8"))["record"] == alert


def test_sink_refuses_an_unknown_event(tmp_path):
    with pytest.raises(na.NarrativeAlertInvalid, match="sink_event_invalid"):
        na.TestSink(tmp_path, policy=bsa_policy()).deliver("email_sent", advisory_alert(), at=T0)


# Rehearsal of the six B03 events through the test sink


def test_rehearsal_of_the_six_events_records_three_timings_apart(tmp_path):
    policy = calibrated_policy("safety_and_crime", "corruption_and_state_capacity")
    sink = na.TestSink(tmp_path, policy=policy)
    alerts: list[dict] = []

    def run(observation, detected_at):
        outcome = na.evaluate_observation(
            policy, observation, detected_at=detected_at, prior_alerts=tuple(alerts)
        )
        event = {
            "alert": "alert_raised",
            "candidate": "candidate_recorded",
            "duplicate": "duplicate_suppressed",
            "held": "held",
        }[outcome["outcome"]]
        record = outcome["record"] or {
            "rule_id": observation["rule_id"],
            "observation_key": na.observation_key(observation),
            "reason": outcome["reason"],
        }
        sink.deliver(event, record, at=detected_at)
        if outcome["outcome"] == "alert":
            alerts.append(outcome["record"])
        return outcome

    advisory_outcome = run(advisory(), T0)
    candidate_outcome = run(impersonation(verified=False), T0 + timedelta(minutes=1))
    spike_outcome = run(narrative(), T0 + timedelta(minutes=2))
    counter = {
        "kind": "enforcement_action",
        **evidence(7, published=T0 - timedelta(hours=6), available=T0 - timedelta(hours=1)),
    }
    counter_outcome = run(
        narrative("corruption_and_state_capacity", counter_signals=[counter]),
        T0 + timedelta(minutes=3),
    )
    duplicate_outcome = run(advisory(), T0 + timedelta(minutes=4))

    acked = na.acknowledge_alert(
        advisory_outcome["record"], MONITOR, T0 + timedelta(minutes=20), policy=policy
    )
    sink.deliver("alert_acknowledged", acked, at=T0 + timedelta(minutes=20))
    overdue = na.alert_status(
        spike_outcome["record"], policy=policy, now=T0 + timedelta(minutes=2, seconds=3601)
    )
    sink.deliver("alert_overdue", overdue, at=T0 + timedelta(minutes=2, seconds=3601))

    assert [
        advisory_outcome["outcome"],
        candidate_outcome["outcome"],
        spike_outcome["outcome"],
        counter_outcome["outcome"],
        duplicate_outcome["outcome"],
    ] == ["alert", "candidate", "alert", "alert", "duplicate"]
    assert counter_outcome["record"]["counter_signals"]["state"] == "observed"
    assert overdue["status"] == "unacknowledged_overdue"

    rows = na.rehearsal_rows(sink.entries())
    assert [row["event"] for row in rows] == [
        "alert_raised",
        "candidate_recorded",
        "alert_raised",
        "alert_raised",
        "duplicate_suppressed",
        "alert_acknowledged",
        "alert_overdue",
    ]
    by_event = {(row["event"], row["rule_id"]): row for row in rows}
    ack_row = by_event[("alert_acknowledged", "advisory_revision")]
    assert ack_row["source_freshness_seconds"] == 4 * 3600
    assert ack_row["detection_lag_seconds"] == 3600
    assert ack_row["acknowledgement_lag_seconds"] == 20 * 60
    spike_row = by_event[("alert_raised", "safety_and_crime")]
    assert spike_row["detection_lag_seconds"] == 2 * 3600 + 120
    assert spike_row["acknowledgement_lag_seconds"] is None
    overdue_row = by_event[("alert_overdue", "safety_and_crime")]
    assert overdue_row["status"] == "unacknowledged_overdue"
    assert overdue_row["acknowledgement_lag_seconds"] is None
    duplicate_row = by_event[("duplicate_suppressed", "advisory_revision")]
    assert duplicate_row["reason"] == "already_alerted"
    for row in rows:
        assert row["delivery"] == "test_sink"


# Review findings


def test_sink_refuses_a_record_identity_that_is_not_an_alert_id(tmp_path):
    (tmp_path / "20260925T060000000000Z-alert_raised-x").mkdir()
    sink = na.TestSink(tmp_path, policy=bsa_policy())
    with pytest.raises(na.NarrativeAlertInvalid, match="sink_record_invalid"):
        sink.deliver("alert_raised", {"alert_id": "x/../../escape"}, at=T0)
    assert not (tmp_path.parent / "escape.json").exists()


def test_sink_refuses_a_second_delivery_of_the_same_alert_event(tmp_path):
    sink = na.TestSink(tmp_path, policy=bsa_policy())
    alert = advisory_alert()
    sink.deliver("alert_raised", alert, at=T0)
    with pytest.raises(na.NarrativeAlertInvalid, match="sink_entry_exists"):
        sink.deliver("alert_raised", alert, at=T0 + timedelta(minutes=1))
    assert len(sink.entries()) == 1


def test_independent_evidence_count_above_the_evidence_is_refused():
    policy = calibrated_policy("safety_and_crime")
    with pytest.raises(na.NarrativeAlertInvalid, match="observation_invalid"):
        na.evaluate_observation(
            policy,
            narrative(independent_evidence_count=99),
            detected_at=T0,
            prior_alerts=(),
        )


def test_an_alert_with_a_moved_trigger_time_is_refused():
    policy = bsa_policy()
    alert = advisory_alert(policy)
    moved = copy.deepcopy(alert)
    moved["triggered_at"] = iso(T0 + timedelta(hours=5))
    moved["response"]["triggered_at"] = moved["triggered_at"]
    with pytest.raises(na.NarrativeAlertInvalid, match="alert_invalid"):
        na.acknowledge_alert(moved, MONITOR, T0 + timedelta(hours=5), policy=policy)
    swapped = copy.deepcopy(alert)
    swapped["evidence"][0]["evidence_id"] = "ev_other"
    with pytest.raises(na.NarrativeAlertInvalid, match="alert_invalid"):
        na.acknowledge_alert(swapped, MONITOR, T0, policy=policy)


def test_a_frozen_alert_is_acknowledged_like_a_plain_one():
    policy = bsa_policy()
    alert = advisory_alert(policy)
    acked = na.acknowledge_alert(
        na._freeze(alert), MONITOR, T0 + timedelta(minutes=1), policy=policy
    )
    assert acked["response"]["status"] == "acknowledged"


def test_cooldown_holds_in_both_directions():
    policy = calibrated_policy("safety_and_crime")
    later = na.evaluate_observation(
        policy, narrative(), detected_at=T0 + timedelta(hours=1), prior_alerts=()
    )["record"]
    earlier_run = narrative(previous=signal("run_0", velocity="steady"), current=signal("run_1"))
    replay = na.evaluate_observation(
        policy, earlier_run, detected_at=T0 + timedelta(minutes=59), prior_alerts=(later,)
    )
    assert replay == {"outcome": "duplicate", "reason": "cooldown", "record": None}


@pytest.mark.parametrize(
    "prior",
    [
        {"record_kind": "alert"},
        {"record_kind": "alert", "rule_id": "safety_and_crime", "dedup_key": "k"},
        {
            "record_kind": "alert",
            "rule_id": "safety_and_crime",
            "dedup_key": "k",
            "triggered_at": "yesterday",
        },
    ],
)
def test_a_malformed_prior_alert_is_refused(prior):
    policy = calibrated_policy("safety_and_crime")
    with pytest.raises(na.NarrativeAlertInvalid, match="prior_alerts_invalid"):
        na.evaluate_observation(policy, narrative(), detected_at=T0, prior_alerts=(prior,))


def test_counter_signal_available_after_detection_is_refused():
    policy = calibrated_policy("corruption_and_state_capacity")
    counter = {
        "kind": "asset_recovery",
        **evidence(5, published=T0 + timedelta(days=2), available=T0 + timedelta(days=2)),
    }
    with pytest.raises(na.NarrativeAlertInvalid, match="counter_signal_invalid"):
        na.evaluate_observation(
            policy,
            narrative("corruption_and_state_capacity", counter_signals=[counter]),
            detected_at=T0,
            prior_alerts=(),
        )


def test_advisory_keys_do_not_collide_across_field_boundaries():
    policy = bsa_policy()
    first = advisory()
    first["advisory"] = {"issuer": "a|b", "advisory_id": "c", "revision": "d"}
    second = advisory()
    second["advisory"] = {"issuer": "a", "advisory_id": "b|c", "revision": "d"}
    alert = na.evaluate_observation(policy, first, detected_at=T0, prior_alerts=())["record"]
    outcome = na.evaluate_observation(policy, second, detected_at=T0, prior_alerts=(alert,))
    assert outcome["outcome"] == "alert"


@pytest.mark.parametrize(
    ("platform", "handle"),
    [("x", "BrandSouthAfrica_"), ("twitter", "@brandsouthafrica_"), ("X", "@BrandSouthAfrica_")],
)
def test_the_same_impersonated_handle_escalates_once(platform, handle):
    policy = bsa_policy()
    first = na.evaluate_observation(
        policy, impersonation(verified=True), detected_at=T0, prior_alerts=()
    )["record"]
    repeat = impersonation(verified=True)
    repeat["account"] = {"platform": platform, "handle": handle}
    outcome = na.evaluate_observation(
        policy, repeat, detected_at=T0 + timedelta(hours=1), prior_alerts=(first,)
    )
    assert outcome == {"outcome": "duplicate", "reason": "already_alerted", "record": None}


def test_typed_rule_requires_every_evidence_item_typed():
    policy = calibrated_policy("safety_and_crime")
    observation = narrative()
    del observation["evidence"][0]["evidence_type"]
    with pytest.raises(na.NarrativeAlertInvalid, match="evidence_invalid"):
        na.evaluate_observation(policy, observation, detected_at=T0, prior_alerts=())


def test_narrative_evidence_cannot_claim_a_verified_advisory_revision():
    policy = calibrated_policy("safety_and_crime")
    observation = narrative()
    for item in observation["evidence"]:
        item["evidence_type"] = "verified_advisory_revision"
    with pytest.raises(na.NarrativeAlertInvalid, match="evidence_invalid"):
        na.evaluate_observation(policy, observation, detected_at=T0, prior_alerts=())


def test_advisory_alert_types_its_evidence_as_a_verified_revision():
    alert = advisory_alert()
    assert alert["evidence_types"] == {"verified_advisory_revision": 1}


def test_only_the_cited_advisory_item_counts_as_a_verified_revision():
    observation = advisory()
    observation["evidence"].append(
        evidence(11, published=T0 - timedelta(hours=4), available=T0 - timedelta(hours=2))
    )
    alert = na.evaluate_observation(bsa_policy(), observation, detected_at=T0, prior_alerts=())[
        "record"
    ]
    assert alert["evidence_types"] == {"verified_advisory_revision": 1}


def test_unverified_verification_carries_no_verified_time():
    candidate = impersonation(verified=False)
    candidate["verification"]["verified_at"] = iso(T0)
    with pytest.raises(na.NarrativeAlertInvalid, match="verification_invalid"):
        na.evaluate_observation(bsa_policy(), candidate, detected_at=T0, prior_alerts=())
    verified = impersonation(verified=True)
    verified["verification"]["verified_at"] = None
    with pytest.raises(na.NarrativeAlertInvalid, match="verification_invalid"):
        na.evaluate_observation(bsa_policy(), verified, detected_at=T0, prior_alerts=())


# Second review findings


def test_evidence_published_before_the_frozen_window_holds():
    policy = calibrated_policy("safety_and_crime")
    observation = narrative()
    for item in observation["evidence"]:
        item["published_at"] = iso(T0 - timedelta(days=400))
    outcome = na.evaluate_observation(policy, observation, detected_at=T0, prior_alerts=())
    assert outcome == {"outcome": "held", "reason": "evidence_outside_window", "record": None}


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("external_response",), "approved"),
        (("response", "decision_ref"), "approved"),
        (("risk",), "energy_water_infrastructure"),
        (("label",), "Other"),
        (("timing", "detection_lag_seconds"), 0),
        (("verification", "state"), "unverified"),
        (("response", "status"), "acknowledged"),
    ],
)
def test_any_tampered_alert_field_is_refused(path, value):
    policy = bsa_policy()
    alert = copy.deepcopy(advisory_alert(policy))
    target = alert
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(na.NarrativeAlertInvalid, match="alert_invalid"):
        na.acknowledge_alert(alert, MONITOR, T0 + timedelta(minutes=1), policy=policy)
    with pytest.raises(na.NarrativeAlertInvalid, match="alert_invalid"):
        na.alert_status(alert, policy=policy, now=T0 + timedelta(minutes=1))


def test_a_tampered_acknowledgement_is_refused():
    policy = bsa_policy()
    acked = na.acknowledge_alert(
        advisory_alert(policy), MONITOR, T0 + timedelta(seconds=3601), policy=policy
    )
    forged = copy.deepcopy(acked)
    forged["response"]["status"] = "acknowledged"
    forged["acknowledged_within_window"] = True
    with pytest.raises(na.NarrativeAlertInvalid, match="alert_invalid"):
        na.alert_status(forged, policy=policy, now=T0 + timedelta(days=1))


def test_prior_alerts_from_another_policy_or_forged_are_refused():
    policy = bsa_policy()
    document = raw_preset()
    document["policy_id"] = "other_alerts"
    other = na.compile_alert_policy(document, overlay=bsa_overlay())
    foreign = na.evaluate_observation(other, advisory(), detected_at=T0, prior_alerts=())["record"]
    with pytest.raises(na.NarrativeAlertInvalid, match="prior_alerts_invalid"):
        na.evaluate_observation(policy, advisory(), detected_at=T0, prior_alerts=(foreign,))
    forged = copy.deepcopy(advisory_alert(policy))
    forged["label"] = "Forged"
    with pytest.raises(na.NarrativeAlertInvalid, match="prior_alerts_invalid"):
        na.evaluate_observation(policy, advisory(), detected_at=T0, prior_alerts=(forged,))


@pytest.mark.parametrize(
    ("event", "record_of"),
    [
        ("alert_acknowledged", "open_alert"),
        ("alert_raised", "acknowledged"),
        ("candidate_recorded", "open_alert"),
        ("alert_overdue", "open_alert"),
        ("held", "open_alert"),
    ],
)
def test_sink_refuses_an_event_that_does_not_match_the_record(tmp_path, event, record_of):
    policy = bsa_policy()
    alert = advisory_alert(policy)
    records = {
        "open_alert": alert,
        "acknowledged": na.acknowledge_alert(alert, MONITOR, T0, policy=policy),
    }
    with pytest.raises(na.NarrativeAlertInvalid, match="sink_record_invalid"):
        na.TestSink(tmp_path, policy=bsa_policy()).deliver(event, records[record_of], at=T0)


def test_sink_entries_are_read_only(tmp_path):
    receipt = na.TestSink(tmp_path, policy=bsa_policy()).deliver(
        "alert_raised", advisory_alert(), at=T0
    )
    assert Path(receipt["path"]).stat().st_mode & 0o222 == 0


def test_acknowledgement_before_the_trigger_is_a_refusal():
    policy = bsa_policy()
    with pytest.raises(na.NarrativeAlertInvalid, match="acknowledgement_before_trigger"):
        na.acknowledge_alert(
            advisory_alert(policy), MONITOR, T0 - timedelta(seconds=1), policy=policy
        )


def test_advisory_issuer_case_does_not_create_a_second_alert():
    policy = bsa_policy()
    first = advisory_alert(policy)
    upper = advisory()
    upper["advisory"]["issuer"] = "GOV.UK"
    outcome = na.evaluate_observation(policy, upper, detected_at=T0, prior_alerts=(first,))
    assert outcome == {"outcome": "duplicate", "reason": "already_alerted", "record": None}


def test_counter_signals_are_distinct_from_each_other_and_the_evidence():
    policy = calibrated_policy("corruption_and_state_capacity")
    counter = {
        "kind": "asset_recovery",
        **evidence(5, published=T0 - timedelta(hours=4), available=T0 - timedelta(hours=1)),
    }
    with pytest.raises(na.NarrativeAlertInvalid, match="counter_signal_invalid"):
        na.evaluate_observation(
            policy,
            narrative("corruption_and_state_capacity", counter_signals=[counter, counter]),
            detected_at=T0,
            prior_alerts=(),
        )
    reused = {
        "kind": "asset_recovery",
        **evidence(1, published=T0 - timedelta(hours=9), available=T0 - timedelta(hours=3)),
    }
    with pytest.raises(na.NarrativeAlertInvalid, match="counter_signal_invalid"):
        na.evaluate_observation(
            policy,
            narrative("corruption_and_state_capacity", counter_signals=[reused]),
            detected_at=T0,
            prior_alerts=(),
        )


def test_impersonation_records_carry_the_normalised_account():
    policy = bsa_policy()
    candidate = na.evaluate_observation(
        policy, impersonation(verified=False), detected_at=T0, prior_alerts=()
    )["record"]
    assert candidate["account"] == {"platform": "x", "handle": "brandsouthafrica_"}
    assert candidate["observed_account"] == {"platform": "x", "handle": "@BrandSouthAfrica_"}
    alert = na.evaluate_observation(
        policy, impersonation(verified=True), detected_at=T0, prior_alerts=()
    )["record"]
    assert alert["account"] == {"platform": "x", "handle": "brandsouthafrica_"}


def test_verification_must_cite_its_evidence_and_follow_publication():
    stale = advisory()
    stale["verification"]["verified_at"] = iso(T0 - timedelta(days=30))
    with pytest.raises(na.NarrativeAlertInvalid, match="verification_invalid"):
        na.evaluate_observation(bsa_policy(), stale, detected_at=T0, prior_alerts=())
    uncited = advisory()
    uncited["verification"]["evidence_ref"] = "ev_elsewhere"
    with pytest.raises(na.NarrativeAlertInvalid, match="verification_invalid"):
        na.evaluate_observation(bsa_policy(), uncited, detected_at=T0, prior_alerts=())


# Third review findings


def test_sink_keeps_a_held_entry_per_rule_at_one_run_time(tmp_path):
    policy = bsa_policy()
    sink = na.TestSink(tmp_path, policy=policy)
    for rule_id in ("safety_and_crime", "corruption_and_state_capacity"):
        outcome = na.evaluate_observation(
            policy, narrative(rule_id), detected_at=T0, prior_alerts=()
        )
        record = {
            "rule_id": rule_id,
            "observation_key": na.observation_key(narrative(rule_id)),
            "reason": outcome["reason"],
        }
        sink.deliver("held", record, at=T0)
    rows = na.rehearsal_rows(sink.entries())
    assert sorted(row["rule_id"] for row in rows) == [
        "corruption_and_state_capacity",
        "safety_and_crime",
    ]
    assert {row["reason"] for row in rows} == {"threshold_uncalibrated"}


@pytest.mark.parametrize(
    ("event", "record"),
    [
        ("held", {"reason": "threshold_uncalibrated"}),
        ("held", {"rule_id": "safety_and_crime", "reason": "threshold_uncalibrated"}),
        (
            "held",
            {
                "rule_id": "not_a_rule",
                "observation_key": "sig_" + "a" * 64 + ":run_2",
                "reason": "threshold_uncalibrated",
            },
        ),
        (
            "held",
            {
                "rule_id": "safety_and_crime",
                "observation_key": "sig_" + "a" * 64 + ":run_2",
                "reason": "cooldown",
            },
        ),
        (
            "duplicate_suppressed",
            {
                "rule_id": "safety_and_crime",
                "observation_key": "sig_" + "a" * 64 + ":run_2",
                "reason": "below_threshold",
            },
        ),
        ("held", {"rule_id": "safety_and_crime", "observation_key": "", "reason": "cooldown"}),
    ],
)
def test_sink_refuses_a_hold_or_duplicate_the_rules_cannot_produce(tmp_path, event, record):
    with pytest.raises(na.NarrativeAlertInvalid, match="sink_record_invalid"):
        na.TestSink(tmp_path, policy=bsa_policy()).deliver(event, record, at=T0)


def test_sink_refuses_an_edited_alert(tmp_path):
    policy = bsa_policy()
    edited = copy.deepcopy(advisory_alert(policy))
    edited["response"]["decision_ref"] = "approved"
    with pytest.raises(na.NarrativeAlertInvalid, match="sink_record_invalid"):
        na.TestSink(tmp_path, policy=policy).deliver("alert_raised", edited, at=T0)


def test_advisory_identity_is_case_insensitive_throughout():
    policy = bsa_policy()
    first = advisory_alert(policy)
    shouted = advisory()
    shouted["advisory"] = {
        "issuer": "GOV.UK",
        "advisory_id": "South-Africa",
        "revision": "2026-09-24.1",
    }
    outcome = na.evaluate_observation(policy, shouted, detected_at=T0, prior_alerts=(first,))
    assert outcome == {"outcome": "duplicate", "reason": "already_alerted", "record": None}


# Fourth review findings


def test_two_held_signals_under_one_rule_at_one_run_time_are_both_kept(tmp_path):
    policy = bsa_policy()
    sink = na.TestSink(tmp_path, policy=policy)
    second = narrative(previous=signal("run_2", velocity="steady"), current=signal("run_3"))
    for observation in (narrative(), second):
        outcome = na.evaluate_observation(policy, observation, detected_at=T0, prior_alerts=())
        sink.deliver(
            "held",
            {
                "rule_id": "safety_and_crime",
                "observation_key": na.observation_key(observation),
                "reason": outcome["reason"],
            },
            at=T0,
        )
    keys = [entry["record"]["observation_key"] for entry in sink.entries()]
    assert keys == ["sig_" + "a" * 64 + ":run_2", "sig_" + "a" * 64 + ":run_3"]


def test_observation_key_matches_the_alert_dedup_key():
    policy = calibrated_policy("safety_and_crime")
    for observation in (narrative(), advisory(), impersonation(verified=True)):
        alert = na.evaluate_observation(policy, observation, detected_at=T0, prior_alerts=())[
            "record"
        ]
        assert na.observation_key(observation) == alert["dedup_key"]


def test_sink_reads_back_in_delivery_order_at_one_stamp(tmp_path):
    policy = bsa_policy()
    sink = na.TestSink(tmp_path, policy=policy)
    held = {
        "rule_id": "safety_and_crime",
        "observation_key": na.observation_key(narrative()),
        "reason": "threshold_uncalibrated",
    }
    sink.deliver("held", held, at=T0)
    sink.deliver("alert_raised", advisory_alert(policy), at=T0)
    assert [entry["event"] for entry in sink.entries()] == ["held", "alert_raised"]


# Fifth review findings


def test_an_advisory_alerted_under_an_earlier_preset_version_stays_one_alert():
    first_version = bsa_policy()
    alert = advisory_alert(first_version)
    next_version = calibrated_policy("safety_and_crime")
    assert next_version["policy_digest"] != first_version["policy_digest"]
    outcome = na.evaluate_observation(
        next_version, advisory(), detected_at=T0 + timedelta(days=1), prior_alerts=(alert,)
    )
    assert outcome == {"outcome": "duplicate", "reason": "already_alerted", "record": None}


def test_a_prior_alert_of_another_preset_is_still_refused():
    policy = bsa_policy()
    foreign = copy.deepcopy(advisory_alert(policy))
    document = raw_preset()
    document["policy_id"] = "other_alerts"
    other = na.compile_alert_policy(document, overlay=bsa_overlay())
    with pytest.raises(na.NarrativeAlertInvalid, match="prior_alerts_invalid"):
        na.evaluate_observation(other, advisory(), detected_at=T0, prior_alerts=(foreign,))


def test_event_timing_comes_from_the_cited_item():
    observation = advisory()
    observation["evidence"].append(
        evidence(11, published=T0 - timedelta(hours=2), available=T0 - timedelta(minutes=45))
    )
    alert = na.evaluate_observation(bsa_policy(), observation, detected_at=T0, prior_alerts=())[
        "record"
    ]
    assert alert["source_published_at"] == iso(T0 - timedelta(hours=5))
    assert alert["source_available_at"] == iso(T0 - timedelta(hours=1))
    assert alert["timing"]["detection_lag_seconds"] == 3600
    assert alert["timing"]["source_freshness_seconds"] == 4 * 3600
