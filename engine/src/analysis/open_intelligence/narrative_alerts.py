"""General narrative alert rules, acknowledgement and the protected test sink.

A client preset (``configs/narrative_alerts/<brand>.yaml``) names the risks, their labels
and their calibration. The risk ids must equal the client overlay's ``risk_topics``, so
the alert layer never widens or narrows the lens B02 approved. A dimension subcluster
such as tariff and trade access carries its own rule and threshold.

Two trigger families:

* Narrative threshold rules sit on the watched signal material change mechanism
  (``validated_watched_change``) and the approved readiness state. They fire only under a
  frozen, versioned calibration: threshold, minimum independent evidence, observation
  window and cooldown. An uncalibrated rule holds; nothing here supplies a default.
* Event rules fire on one independently verified event (an advisory revision, an
  official account impersonation), independent of volume and separate from trend
  promotion readiness. An unverified impersonation is recorded as a candidate and never
  escalates; the same advisory revision or impersonated handle never alerts twice.

Three timings are kept apart on every record. Source freshness is the newest evidence's
availability minus its publication. Detection lag is the trigger time minus that
availability. Acknowledgement lag is the acknowledgement minus the trigger time. The
collection cadence is carried on the record because daily collection is not
instantaneous monitoring.

Acknowledgement records that a named role has seen the alert. It is never approval of a
recommended external response. Delivery goes only to a local test sink that writes each
entry once and never overwrites; nothing here sends email, posts a reply or touches PULSE.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import yaml

from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.watched_signal_delivery import validated_watched_change

_ROOT = Path(__file__).resolve().parents[3]
NARRATIVE_ALERTS_DIR = _ROOT / "configs" / "narrative_alerts"
POLICY_VERSION = "narrative_alert_policy_v1"
POLICY_FIELDS = (
    "contract_version",
    "policy_id",
    "brand_config_id",
    "client_scope_id",
    "collection_cadence",
    "delivery",
    "acknowledgement",
    "narrative_rules",
    "event_rules",
)
ACKNOWLEDGEMENT_FIELDS = (
    "window_seconds",
    "authorized_roles",
    "assigned_role",
    "operational_owner",
    "escalation_contact",
)
RULE_SOURCES = ("risk_topic", "dimension_subcluster")
CALIBRATION_FIELDS = (
    "state",
    "calibration_ref",
    "momentum_threshold",
    "minimum_independent_evidence",
    "observation_window_seconds",
    "cooldown_seconds",
)
EVENT_RULES = ("advisory_revision", "official_account_impersonation")
COLLECTION_CADENCES = ("daily",)
EVIDENCE_FIELDS = ("evidence_id", "evidence_version", "published_at", "available_at")
SINK_EVENTS = (
    "alert_raised",
    "candidate_recorded",
    "duplicate_suppressed",
    "held",
    "alert_acknowledged",
    "alert_overdue",
)
_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ALERT_ID = re.compile(r"alert_[0-9a-f]{64}\Z")
# Only the advisory event rule produces a verified advisory revision; a narrative
# observation cannot claim one for its own evidence.
EVENT_EVIDENCE_TYPES = ("verified_advisory_revision",)
PLATFORM_ALIASES = {"twitter": "x"}
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")


class NarrativeAlertInvalid(ValueError):
    """A preset, observation, alert, acknowledgement or sink request is refused."""


def _fail(code: str):
    raise NarrativeAlertInvalid(code)


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(code)
    return value


def _ident(value: object, code: str) -> str:
    if _ID.fullmatch(_text(value, code)) is None:
        _fail(code)
    return value


def _positive_int(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _fail(code)
    return value


def _exact(value: object, fields: tuple[str, ...], code: str) -> dict:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        _fail(code)
    return dict(value)


def _parse_instant(value: object, code: str) -> datetime:
    if not isinstance(value, str) or _INSTANT.fullmatch(value) is None:
        _fail(code)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail(code)


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        _fail("timezone_required")
    return value


def _format_instant(value: datetime) -> str:
    utc = value.astimezone(UTC)
    text = utc.strftime("%Y-%m-%dT%H:%M:%S")
    if utc.microsecond:
        text += f".{utc.microsecond:06d}"
    return text + "Z"


def _seconds(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def acknowledgement_seconds(triggered_at: datetime, acknowledged_at: datetime) -> float:
    if triggered_at.utcoffset() is None or acknowledged_at.utcoffset() is None:
        raise ValueError("timezone_required")
    elapsed = (acknowledged_at - triggered_at).total_seconds()
    if elapsed < 0:
        raise ValueError("acknowledgement_before_trigger")
    return elapsed


# Preset


def _calibration(value: object) -> dict:
    if not isinstance(value, Mapping) or "state" not in value:
        _fail("policy_calibration_invalid")
    if value["state"] == "uncalibrated":
        if set(value) != {"state"}:
            _fail("policy_calibration_invalid")
        return {"state": "uncalibrated"}
    if value["state"] != "frozen":
        _fail("policy_calibration_invalid")
    calibration = _exact(value, CALIBRATION_FIELDS, "policy_calibration_invalid")
    if _DIGEST.fullmatch(str(calibration["calibration_ref"])) is None:
        _fail("policy_calibration_invalid")
    threshold = calibration["momentum_threshold"]
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(threshold)
        or threshold <= 0
    ):
        _fail("policy_calibration_invalid")
    for field in (
        "minimum_independent_evidence",
        "observation_window_seconds",
        "cooldown_seconds",
    ):
        _positive_int(calibration[field], "policy_calibration_invalid")
    return calibration


def _kinds(value: object, code: str) -> list[str]:
    if not isinstance(value, list):
        _fail(code)
    kinds = [_ident(item, code) for item in value]
    if len(set(kinds)) != len(kinds):
        _fail(code)
    return kinds


def _narrative_rule(rule_id: str, value: object, overlay) -> dict:
    if not isinstance(value, Mapping) or value.get("source") not in RULE_SOURCES:
        _fail("policy_rule_invalid")
    fields = ["label", "source", "evidence_types", "counter_signals", "calibration"]
    if value["source"] == "dimension_subcluster":
        fields.append("parent_dimension")
    rule = _exact(value, tuple(fields), "policy_rule_invalid")
    _ident(rule_id, "policy_rule_invalid")
    _text(rule["label"], "policy_rule_invalid")
    if rule["source"] == "dimension_subcluster" and (
        rule["parent_dimension"] not in overlay["dimensions"] or rule_id in overlay["dimensions"]
    ):
        _fail("policy_rule_invalid")
    return {
        **rule,
        "evidence_types": _kinds(rule["evidence_types"], "policy_rule_invalid"),
        "counter_signals": _kinds(rule["counter_signals"], "policy_rule_invalid"),
        "calibration": _calibration(rule["calibration"]),
    }


def compile_alert_policy(document: object, *, overlay) -> Mapping:
    """Validate one preset against its compiled client overlay and freeze it with a digest.

    The digest covers every validated field and the overlay digest, so a changed threshold,
    label, role or lens is a different policy, never a hidden runtime tweak.
    """
    policy = _exact(document, POLICY_FIELDS, "policy_shape_invalid")
    if policy["contract_version"] != POLICY_VERSION:
        _fail("policy_version_invalid")
    _ident(policy["policy_id"], "policy_shape_invalid")
    if (
        policy["brand_config_id"] != overlay["brand_config_id"]
        or policy["client_scope_id"] != overlay["client_scope_id"]
    ):
        _fail("policy_binding_invalid")
    if policy["collection_cadence"] not in COLLECTION_CADENCES:
        _fail("policy_cadence_invalid")
    if policy["delivery"] != "test_sink":
        _fail("policy_delivery_invalid")
    ack = _exact(
        policy["acknowledgement"], ACKNOWLEDGEMENT_FIELDS, "policy_acknowledgement_invalid"
    )
    _positive_int(ack["window_seconds"], "policy_acknowledgement_invalid")
    roles = _kinds(ack["authorized_roles"], "policy_acknowledgement_invalid")
    if not roles or ack["assigned_role"] not in roles:
        _fail("policy_acknowledgement_invalid")
    for field in ("operational_owner", "escalation_contact"):
        _text(ack[field], "policy_acknowledgement_invalid")
    rules = policy["narrative_rules"]
    if not isinstance(rules, Mapping) or not rules:
        _fail("policy_rule_invalid")
    compiled_rules = {
        rule_id: _narrative_rule(rule_id, rule, overlay) for rule_id, rule in rules.items()
    }
    risk_rules = [
        rule_id for rule_id, rule in compiled_rules.items() if rule["source"] == "risk_topic"
    ]
    if risk_rules != list(overlay["risk_topics"]):
        _fail("policy_risk_topics_mismatch")
    events = policy["event_rules"]
    if not isinstance(events, Mapping) or set(events) != set(EVENT_RULES):
        _fail("policy_event_rule_invalid")
    compiled_events = {}
    for rule_id in EVENT_RULES:
        event = _exact(events[rule_id], ("label", "risk"), "policy_event_rule_invalid")
        _text(event["label"], "policy_event_rule_invalid")
        if event["risk"] not in overlay["risk_topics"]:
            _fail("policy_event_rule_invalid")
        compiled_events[rule_id] = event
    validated = {
        **copy.deepcopy(policy),
        "acknowledgement": {**ack, "authorized_roles": roles},
        "narrative_rules": compiled_rules,
        "event_rules": compiled_events,
        "overlay_digest": overlay["overlay_digest"],
    }
    return _freeze({**validated, "policy_digest": canonical_digest(validated)})


def load_alert_policy(brand_config_id: str, *, overlay, root: Path | None = None) -> Mapping:
    """Read one preset beside the topic overlays and bind it to the compiled overlay."""
    _ident(brand_config_id, "policy_unavailable")
    if overlay["brand_config_id"] != brand_config_id:
        _fail("policy_binding_invalid")
    path = (NARRATIVE_ALERTS_DIR if root is None else Path(root)) / f"{brand_config_id}.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise NarrativeAlertInvalid("policy_unavailable") from error
    return compile_alert_policy(document, overlay=overlay)


# Observations


def _evidence(value: object, allowed_types, *, require_type: bool = False) -> list[dict]:
    if not isinstance(value, list) or not value:
        _fail("evidence_invalid")
    items = []
    for item in value:
        if not isinstance(item, Mapping):
            _fail("evidence_invalid")
        typed = "evidence_type" in item
        if require_type and not typed:
            _fail("evidence_invalid")
        record = _exact(
            item,
            (*EVIDENCE_FIELDS, "evidence_type") if typed else EVIDENCE_FIELDS,
            "evidence_invalid",
        )
        _text(record["evidence_id"], "evidence_invalid")
        if _DIGEST.fullmatch(str(record["evidence_version"])) is None:
            _fail("evidence_invalid")
        published = _parse_instant(record["published_at"], "evidence_invalid")
        available = _parse_instant(record["available_at"], "evidence_invalid")
        if available < published:
            _fail("evidence_invalid")
        if typed and record["evidence_type"] not in allowed_types:
            _fail("evidence_invalid")
        items.append(record)
    if len({item["evidence_id"] for item in items}) != len(items):
        _fail("evidence_invalid")
    return items


def _counter_signals(
    value: object, rule: Mapping, detected_at: datetime, evidence: list[dict]
) -> dict:
    if not isinstance(value, list):
        _fail("counter_signal_invalid")
    seen = {item["evidence_id"] for item in evidence}
    items = []
    for item in value:
        if not isinstance(item, Mapping) or item.get("kind") not in rule["counter_signals"]:
            _fail("counter_signal_invalid")
        fields = {key: item[key] for key in item if key != "kind"}
        try:
            _evidence([fields], ())
        except NarrativeAlertInvalid:
            _fail("counter_signal_invalid")
        if _parse_instant(fields["available_at"], "counter_signal_invalid") > detected_at:
            _fail("counter_signal_invalid")
        if fields["evidence_id"] in seen:
            _fail("counter_signal_invalid")
        seen.add(fields["evidence_id"])
        items.append(dict(item))
    if not rule["counter_signals"]:
        return {"state": "not_configured", "items": []}
    return {"state": "observed" if items else "absent", "items": items}


def _timing(
    evidence: list[dict], detected_at: datetime, anchor: dict | None = None
) -> tuple[dict, dict]:
    """Time from the anchor item: the cited item for an event, the newest for a narrative."""
    newest = max(
        evidence, key=lambda item: _parse_instant(item["available_at"], "evidence_invalid")
    )
    if detected_at < _parse_instant(newest["available_at"], "evidence_invalid"):
        _fail("detection_before_availability")
    if anchor is not None:
        newest = anchor
    published = _parse_instant(newest["published_at"], "evidence_invalid")
    available = _parse_instant(newest["available_at"], "evidence_invalid")
    timing = {
        "source_freshness_seconds": _seconds((available - published).total_seconds()),
        "detection_lag_seconds": _seconds((detected_at - available).total_seconds()),
        "acknowledgement_lag_seconds": None,
    }
    return newest, timing


def _verification(value: object, evidence: list[dict]) -> dict:
    """A verification cites one supplied evidence item and follows its publication."""
    verification = _exact(value, ("state", "evidence_ref", "verified_at"), "verification_invalid")
    if verification["state"] not in ("verified", "unverified"):
        _fail("verification_invalid")
    cited = [item for item in evidence if item["evidence_id"] == verification["evidence_ref"]]
    if not cited:
        _fail("verification_invalid")
    if verification["state"] == "verified":
        verified_at = _parse_instant(verification["verified_at"], "verification_invalid")
        if verified_at < _parse_instant(cited[0]["published_at"], "verification_invalid"):
            _fail("verification_invalid")
    elif verification["verified_at"] is not None:
        _fail("verification_invalid")
    return verification


def _cited(evidence: list[dict], verification: Mapping) -> dict:
    return next(item for item in evidence if item["evidence_id"] == verification["evidence_ref"])


def _prior(prior_alerts: object, policy: Mapping) -> list[dict]:
    """Prior alerts must be intact alerts of this preset, current or earlier version.

    A recalibrated preset has a new digest, but an advisory revision or handle that
    already alerted under an earlier version of the same preset stays one alert. An alert
    of this exact version is checked in full; an earlier version's alert is checked for
    its seal, since its acknowledgement terms belong to the version it was raised under.
    """
    if not isinstance(prior_alerts, (list, tuple)):
        _fail("prior_alerts_invalid")
    prior = []
    for alert in prior_alerts:
        try:
            if isinstance(alert, Mapping) and alert.get("policy_digest") != policy["policy_digest"]:
                if (
                    alert.get("policy_id") != policy["policy_id"]
                    or alert.get("client_scope_id") != policy["client_scope_id"]
                ):
                    _fail("alert_policy_mismatch")
                prior.append(_sealed_alert(alert))
            else:
                prior.append(_open_alert(alert, policy))
        except NarrativeAlertInvalid as error:
            raise NarrativeAlertInvalid("prior_alerts_invalid") from error
    return prior


def _held(reason: str) -> dict:
    return {"outcome": "held", "reason": reason, "record": None}


def _duplicate(reason: str) -> dict:
    return {"outcome": "duplicate", "reason": reason, "record": None}


_ACK_RESPONSE_FIELDS = ("alert_id", "acknowledged_at", "acknowledged_by", "status")


def _seal_id(record: Mapping) -> str:
    """The alert id seals every field except the acknowledgement fields, which are rechecked."""
    body = _thaw(record)
    body.pop("alert_id", None)
    body.pop("acknowledged_within_window", None)
    body["timing"] = {
        key: value for key, value in body["timing"].items() if key != "acknowledgement_lag_seconds"
    }
    body["response"] = {
        key: value for key, value in body["response"].items() if key not in _ACK_RESPONSE_FIELDS
    }
    return "alert_" + canonical_digest(body)


def _alert(
    policy: Mapping,
    *,
    rule_id: str,
    risk: str,
    label: str,
    trigger: str,
    dedup_key: str,
    evidence: list[dict],
    detected_at: datetime,
    material_change: list[str] | None,
    readiness: str,
    counter_signals: dict,
    evidence_types: dict[str, int] | None = None,
    extra: dict | None = None,
    anchor: dict | None = None,
) -> dict:
    newest, timing = _timing(evidence, detected_at, anchor)
    triggered_at = _format_instant(detected_at)
    evidence_version = canonical_digest(evidence)
    types: dict[str, int] = {}
    for item in evidence:
        if "evidence_type" in item:
            types[item["evidence_type"]] = types.get(item["evidence_type"], 0) + 1
    if evidence_types is not None:
        types = evidence_types
    ack = policy["acknowledgement"]
    record = {
        "record_kind": "alert",
        "alert_id": None,
        "policy_id": policy["policy_id"],
        "policy_digest": policy["policy_digest"],
        "client_scope_id": policy["client_scope_id"],
        "rule_id": rule_id,
        "risk": risk,
        "label": label,
        "trigger": trigger,
        "dedup_key": dedup_key,
        "material_change": material_change,
        "readiness": readiness,
        "evidence": evidence,
        "evidence_types": dict(sorted(types.items())),
        "counter_signals": counter_signals,
        "source_published_at": newest["published_at"],
        "source_available_at": newest["available_at"],
        "triggered_at": triggered_at,
        "timing": timing,
        "collection_cadence": policy["collection_cadence"],
        "external_response": "human_review_required",
        "acknowledged_within_window": None,
        "response": {
            "alert_id": None,
            "evidence_version": evidence_version,
            "triggered_at": triggered_at,
            "assigned_role": ack["assigned_role"],
            "acknowledged_at": None,
            "acknowledged_by": None,
            "status": "open",
            "decision_ref": None,
            "follow_up_at": None,
        },
        **(extra or {}),
    }
    alert_id = _seal_id(record)
    record["alert_id"] = alert_id
    record["response"]["alert_id"] = alert_id
    return record


def _seen(prior: list[Mapping], rule_id: str, dedup_key: str) -> bool:
    return any(
        alert.get("rule_id") == rule_id and alert.get("dedup_key") == dedup_key for alert in prior
    )


def _narrative(policy, observation, detected_at, prior) -> dict:
    fields = (
        "kind",
        "rule_id",
        "previous",
        "current",
        "momentum",
        "independent_evidence_count",
        "window_seconds",
        "evidence",
        "counter_signals",
    )
    value = _exact(observation, fields, "observation_invalid")
    rule = policy["narrative_rules"].get(value["rule_id"])
    if rule is None:
        _fail("observation_invalid")
    evidence = _evidence(
        value["evidence"],
        tuple(kind for kind in rule["evidence_types"] if kind not in EVENT_EVIDENCE_TYPES),
        require_type=bool(rule["evidence_types"]),
    )
    counter = _counter_signals(value["counter_signals"], rule, detected_at, evidence)
    momentum = value["momentum"]
    count = value["independent_evidence_count"]
    if (
        isinstance(momentum, bool)
        or not isinstance(momentum, (int, float))
        or not math.isfinite(momentum)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        or count > len(evidence)
    ):
        _fail("observation_invalid")
    _positive_int(value["window_seconds"], "observation_invalid")
    try:
        reasons = validated_watched_change(dict(value["previous"]), dict(value["current"]))
    except (KeyError, TypeError, ValueError) as error:
        raise NarrativeAlertInvalid("signal_invalid") from error
    _timing(evidence, detected_at)
    calibration = rule["calibration"]
    if calibration["state"] != "frozen":
        return _held("threshold_uncalibrated")
    if not reasons:
        return _held("no_material_change")
    if value["current"]["evidence_state"] != "ready":
        return _held("readiness_not_ready")
    if value["window_seconds"] != calibration["observation_window_seconds"]:
        return _held("observation_window_mismatch")
    window_start = detected_at - timedelta(seconds=calibration["observation_window_seconds"])
    if any(
        _parse_instant(item["published_at"], "evidence_invalid") < window_start for item in evidence
    ):
        return _held("evidence_outside_window")
    if count < calibration["minimum_independent_evidence"]:
        return _held("insufficient_independent_evidence")
    if momentum < calibration["momentum_threshold"]:
        return _held("below_threshold")
    rule_id = value["rule_id"]
    dedup_key = observation_key(value)
    if _seen(prior, rule_id, dedup_key):
        return _duplicate("already_alerted")
    for alert in prior:
        if alert.get("rule_id") != rule_id:
            continue
        since = (
            detected_at - _parse_instant(alert["triggered_at"], "prior_alerts_invalid")
        ).total_seconds()
        if abs(since) < calibration["cooldown_seconds"]:
            return _duplicate("cooldown")
    alert = _alert(
        policy,
        rule_id=rule_id,
        risk=rule_id,
        label=rule["label"],
        trigger="narrative_threshold",
        dedup_key=dedup_key,
        evidence=evidence,
        detected_at=detected_at,
        material_change=list(reasons),
        readiness="ready",
        counter_signals=counter,
    )
    return {"outcome": "alert", "reason": None, "record": alert}


def _verified_before(verification: dict, detected_at: datetime) -> None:
    if _parse_instant(verification["verified_at"], "verification_invalid") > detected_at:
        _fail("verification_after_detection")


def _advisory(policy, observation, detected_at, prior) -> dict:
    value = _exact(
        observation,
        ("kind", "rule_id", "advisory", "verification", "evidence"),
        "observation_invalid",
    )
    advisory = _exact(
        value["advisory"], ("issuer", "advisory_id", "revision"), "observation_invalid"
    )
    for field in advisory.values():
        _text(field, "observation_invalid")
    evidence = _evidence(value["evidence"], ())
    verification = _verification(value["verification"], evidence)
    _timing(evidence, detected_at)
    if verification["state"] != "verified":
        return _held("advisory_unverified")
    _verified_before(verification, detected_at)
    rule = policy["event_rules"]["advisory_revision"]
    dedup_key = observation_key(value)
    if _seen(prior, "advisory_revision", dedup_key):
        return _duplicate("already_alerted")
    alert = _alert(
        policy,
        rule_id="advisory_revision",
        risk=rule["risk"],
        label=rule["label"],
        trigger="verified_event",
        dedup_key=dedup_key,
        evidence=evidence,
        detected_at=detected_at,
        material_change=None,
        readiness="separate_from_trend_promotion",
        counter_signals={"state": "not_configured", "items": []},
        evidence_types={EVENT_EVIDENCE_TYPES[0]: 1},
        extra={"verification": verification},
        anchor=_cited(evidence, verification),
    )
    return {"outcome": "alert", "reason": None, "record": alert}


def _impersonation(policy, observation, detected_at, prior) -> dict:
    value = _exact(
        observation,
        ("kind", "rule_id", "account", "verification", "evidence"),
        "observation_invalid",
    )
    account = _exact(value["account"], ("platform", "handle"), "observation_invalid")
    normalised = _normalised_account(account)
    evidence = _evidence(value["evidence"], ())
    verification = _verification(value["verification"], evidence)
    newest, timing = _timing(evidence, detected_at, _cited(evidence, verification))
    rule = policy["event_rules"]["official_account_impersonation"]
    dedup_key = observation_key(value)
    if verification["state"] != "verified":
        return {
            "outcome": "candidate",
            "reason": "identity_unverified",
            "record": {
                "record_kind": "impersonation_candidate",
                "policy_digest": policy["policy_digest"],
                "rule_id": "official_account_impersonation",
                "risk": rule["risk"],
                "dedup_key": dedup_key,
                "account": normalised,
                "observed_account": account,
                "verification_state": "unverified",
                "verification": verification,
                "evidence": evidence,
                "source_published_at": newest["published_at"],
                "source_available_at": newest["available_at"],
                "detected_at": _format_instant(detected_at),
                "timing": timing,
                "collection_cadence": policy["collection_cadence"],
            },
        }
    _verified_before(verification, detected_at)
    if _seen(prior, "official_account_impersonation", dedup_key):
        return _duplicate("already_alerted")
    alert = _alert(
        policy,
        rule_id="official_account_impersonation",
        risk=rule["risk"],
        label=rule["label"],
        trigger="verified_event",
        dedup_key=dedup_key,
        evidence=evidence,
        detected_at=detected_at,
        material_change=None,
        readiness="separate_from_trend_promotion",
        counter_signals={"state": "not_configured", "items": []},
        extra={
            "account": normalised,
            "observed_account": account,
            "verification": verification,
        },
        anchor=_cited(evidence, verification),
    )
    return {"outcome": "alert", "reason": None, "record": alert}


def _normalised_account(account: Mapping) -> dict:
    platform = _text(account["platform"], "observation_invalid").lower()
    platform = PLATFORM_ALIASES.get(platform, platform)
    _ident(platform, "observation_invalid")
    handle = _text(account["handle"], "observation_invalid").removeprefix("@").lower()
    if not handle:
        _fail("observation_invalid")
    return {"platform": platform, "handle": handle}


def observation_key(observation: object) -> str:
    """The identity one observation deduplicates on; held and duplicate entries carry it.

    A narrative observation is its signal and run; an advisory is its issuer, id and
    revision, case folded; an impersonation is its normalised platform and handle.
    """
    try:
        kind = observation["kind"]
        if kind == "narrative":
            current = observation["current"]
            return f"{current['signal_id']}:{current['run_id']}"
        if kind == "advisory_revision":
            advisory = observation["advisory"]
            return canonical_digest(
                [
                    _text(advisory[field], "observation_invalid").casefold()
                    for field in ("issuer", "advisory_id", "revision")
                ]
            )
        if kind == "official_account_impersonation":
            account = _normalised_account(observation["account"])
            return canonical_digest([account["platform"], account["handle"]])
    except (KeyError, TypeError) as error:
        raise NarrativeAlertInvalid("observation_invalid") from error
    _fail("observation_invalid")


_EVALUATORS = {
    "narrative": _narrative,
    "advisory_revision": _advisory,
    "official_account_impersonation": _impersonation,
}


def evaluate_observation(
    policy: Mapping, observation: object, *, detected_at: datetime, prior_alerts
) -> dict:
    """Return one outcome: alert, candidate, held (with reason) or duplicate (with reason).

    ``detected_at`` is the trigger time. ``prior_alerts`` are the alerts already raised
    under this preset; they decide deduplication and cooldown.
    """
    _aware(detected_at)
    prior = _prior(prior_alerts, policy)
    if not isinstance(observation, Mapping):
        _fail("observation_invalid")
    evaluator = _EVALUATORS.get(observation.get("kind"))
    if evaluator is None or (
        observation.get("kind") != "narrative" and observation.get("rule_id") != observation["kind"]
    ):
        _fail("observation_invalid")
    return evaluator(policy, observation, detected_at, prior)


# Acknowledgement and status


def _open_alert(alert: object, policy: Mapping) -> dict:
    if (
        not isinstance(alert, Mapping)
        or alert.get("record_kind") != "alert"
        or not isinstance(alert.get("response"), Mapping)
        or not isinstance(alert.get("timing"), Mapping)
    ):
        _fail("alert_invalid")
    if alert.get("policy_digest") != policy["policy_digest"]:
        _fail("alert_policy_mismatch")
    value = _sealed_alert(alert)
    _acknowledgement_state(value, _parse_instant(value["triggered_at"], "alert_invalid"), policy)
    return value


def _sealed_alert(alert: object) -> dict:
    if (
        not isinstance(alert, Mapping)
        or alert.get("record_kind") != "alert"
        or not isinstance(alert.get("response"), Mapping)
        or not isinstance(alert.get("timing"), Mapping)
    ):
        _fail("alert_invalid")
    value = _thaw(alert)
    response = value["response"]
    try:
        expected = _seal_id(value)
        evidence_version = canonical_digest(value["evidence"])
        _parse_instant(value["triggered_at"], "alert_invalid")
    except (AttributeError, KeyError, TypeError, ValueError):
        _fail("alert_invalid")
    if (
        value.get("alert_id") != expected
        or response.get("alert_id") != expected
        or response.get("triggered_at") != value["triggered_at"]
        or response.get("evidence_version") != evidence_version
    ):
        _fail("alert_invalid")
    return value


def _acknowledgement_state(value: dict, triggered: datetime, policy: Mapping) -> None:
    """The unsealed acknowledgement fields must agree with each other and the policy."""
    response = value["response"]
    lag = value["timing"].get("acknowledgement_lag_seconds")
    within = value.get("acknowledged_within_window")
    if response.get("acknowledged_at") is None:
        if (
            response.get("acknowledged_by") is not None
            or response.get("status") != "open"
            or lag is not None
            or within is not None
        ):
            _fail("alert_invalid")
        return
    at = _parse_instant(response["acknowledged_at"], "alert_invalid")
    actor = response.get("acknowledged_by")
    elapsed = (at - triggered).total_seconds()
    expected_within = 0 <= elapsed <= policy["acknowledgement"]["window_seconds"]
    if (
        elapsed < 0
        or not isinstance(actor, dict)
        or set(actor) != {"principal", "role"}
        or not isinstance(actor["principal"], str)
        or not actor["principal"].strip()
        or actor["role"] not in policy["acknowledgement"]["authorized_roles"]
        or lag != _seconds(elapsed)
        or within is not expected_within
        or response.get("status") != ("acknowledged" if expected_within else "acknowledged_late")
    ):
        _fail("alert_invalid")


def acknowledge_alert(alert: object, actor: object, at: datetime, *, policy: Mapping) -> dict:
    """Record that an authorized role has seen the alert; the actor is server supplied.

    The lag runs from the alert's trigger time. Acknowledgement never approves a proposed
    external response, so ``external_response`` and ``decision_ref`` stay as they were.
    """
    value = _open_alert(alert, policy)
    if (
        not isinstance(actor, Mapping)
        or not isinstance(actor.get("principal"), str)
        or not actor["principal"].strip()
        or actor.get("role") not in policy["acknowledgement"]["authorized_roles"]
    ):
        _fail("acknowledgement_unauthorized")
    if value["response"]["acknowledged_at"] is not None:
        _fail("acknowledgement_duplicate")
    triggered = _parse_instant(value["triggered_at"], "alert_invalid")
    try:
        elapsed = acknowledgement_seconds(triggered, _aware(at))
    except NarrativeAlertInvalid:
        raise
    except ValueError as error:
        raise NarrativeAlertInvalid(str(error)) from error
    within = elapsed <= policy["acknowledgement"]["window_seconds"]
    value["response"] = {
        **value["response"],
        "acknowledged_at": _format_instant(at),
        "acknowledged_by": {"principal": actor["principal"], "role": actor["role"]},
        "status": "acknowledged" if within else "acknowledged_late",
    }
    value["timing"] = {**value["timing"], "acknowledgement_lag_seconds": _seconds(elapsed)}
    value["acknowledged_within_window"] = within
    return value


def alert_status(alert: object, *, policy: Mapping, now: datetime) -> dict:
    """Open inside the window, overdue after it with the owner and escalation named as known.

    An owner the client has not supplied stays ``unknown``; this never invents a person.
    """
    value = _open_alert(alert, policy)
    triggered = _parse_instant(value["triggered_at"], "alert_invalid")
    elapsed = (_aware(now) - triggered).total_seconds()
    if elapsed < 0:
        _fail("status_before_trigger")
    base = {"alert_id": value["alert_id"], "rule_id": value["rule_id"]}
    if value["response"]["acknowledged_at"] is not None:
        return {
            **base,
            "status": value["response"]["status"],
            "acknowledgement_lag_seconds": value["timing"]["acknowledgement_lag_seconds"],
            "escalation": None,
        }
    ack = policy["acknowledgement"]
    if elapsed <= ack["window_seconds"]:
        return {**base, "status": "open", "acknowledgement_lag_seconds": None, "escalation": None}
    return {
        **base,
        "status": "unacknowledged_overdue",
        "acknowledgement_lag_seconds": None,
        "escalation": {
            "assigned_role": ack["assigned_role"],
            "operational_owner": ack["operational_owner"],
            "escalation_contact": ack["escalation_contact"],
            "overdue_seconds": _seconds(elapsed - ack["window_seconds"]),
        },
    }


# Test sink


_STATUS_FIELDS = {"alert_id", "rule_id", "status", "acknowledgement_lag_seconds", "escalation"}
HELD_REASONS = frozenset(
    {
        "threshold_uncalibrated",
        "no_material_change",
        "readiness_not_ready",
        "observation_window_mismatch",
        "evidence_outside_window",
        "insufficient_independent_evidence",
        "below_threshold",
        "advisory_unverified",
    }
)
DUPLICATE_REASONS = frozenset({"already_alerted", "cooldown"})


def _event_matches(event: str, record: Mapping, policy: Mapping) -> bool:
    """Each sink event carries only an intact record of the kind it names."""
    kind = record.get("record_kind")
    if event in ("alert_raised", "alert_acknowledged"):
        try:
            value = _open_alert(record, policy)
        except NarrativeAlertInvalid:
            return False
        acknowledged = value["response"]["acknowledged_at"] is not None
        return acknowledged is (event == "alert_acknowledged")
    if event == "candidate_recorded":
        return kind == "impersonation_candidate"
    if event == "alert_overdue":
        return set(record) == _STATUS_FIELDS and record["status"] == "unacknowledged_overdue"
    reasons = HELD_REASONS if event == "held" else DUPLICATE_REASONS
    rules = {*policy["narrative_rules"], *policy["event_rules"]}
    return (
        set(record) == {"rule_id", "observation_key", "reason"}
        and record["rule_id"] in rules
        and isinstance(record["observation_key"], str)
        and record["observation_key"].strip() != ""
        and record["reason"] in reasons
    )


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class TestSink:
    """A protected local directory: one file per entry, created exclusively, never rewritten."""

    __test__ = False

    def __init__(self, root: object, *, policy: Mapping):
        if isinstance(root, str) and "://" in root:
            _fail("sink_root_invalid")
        path = Path(root) if isinstance(root, (str, Path)) else None
        if path is None or not path.is_absolute() or not path.is_dir():
            _fail("sink_root_invalid")
        self.root = path
        self.policy = policy

    def deliver(self, event: str, record: Mapping, *, at: datetime) -> dict:
        if event not in SINK_EVENTS:
            _fail("sink_event_invalid")
        if not isinstance(record, Mapping) or not _event_matches(event, record, self.policy):
            _fail("sink_record_invalid")
        stamp = _aware(at).astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        if "alert_id" in record:
            ident = record["alert_id"]
            if not isinstance(ident, str) or _ALERT_ID.fullmatch(ident) is None:
                _fail("sink_record_invalid")
            if any(self.root.glob(f"*-{event}-{ident}.json")):
                _fail("sink_entry_exists")
        else:
            ident = canonical_digest(_thaw(record))[:16]
        entry = {
            "delivery": "test_sink",
            "event": event,
            "delivered_at": _format_instant(at),
            "record": _thaw(record),
        }
        payload = json.dumps(entry, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        sequence = len(list(self.root.glob("*.json"))) + 1
        path = self.root / f"{sequence:06d}-{stamp}-{event}-{ident}.json"
        if path.resolve().parent != self.root.resolve():
            _fail("sink_record_invalid")
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
        except FileExistsError as error:
            raise NarrativeAlertInvalid("sink_entry_exists") from error
        path.chmod(0o444)
        return {"path": str(path), "sha256": sha256_file(path)}

    def entries(self) -> list[dict]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob("*.json"))
        ]


def rehearsal_rows(entries: list[dict]) -> list[dict]:
    """One row per sink entry with the three timings in their own columns."""
    rows = []
    for entry in entries:
        record = entry["record"]
        timing = record.get("timing") or {}
        response = record.get("response") or {}
        rows.append(
            {
                "event": entry["event"],
                "delivery": entry["delivery"],
                "delivered_at": entry["delivered_at"],
                "rule_id": record.get("rule_id"),
                "status": response.get("status", record.get("status")),
                "reason": record.get("reason"),
                "source_freshness_seconds": timing.get("source_freshness_seconds"),
                "detection_lag_seconds": timing.get("detection_lag_seconds"),
                "acknowledgement_lag_seconds": timing.get(
                    "acknowledgement_lag_seconds", record.get("acknowledgement_lag_seconds")
                ),
            }
        )
    return rows
