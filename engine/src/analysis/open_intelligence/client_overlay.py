"""Compile one client overlay into an immutable lens, and the two client kernels.

A client overlay is the topic overlay U02 loads plus the client block: verified
handles, ambassador references, the spokesperson rule, risk topics, languages,
the comparator and search visibility references, and three versioned policies
(purpose, lens, noise). The compiler validates every field, refuses an unknown
field, an empty entity, an unverified handle without an observation time and a
placeholder, and returns a read only mapping with a digest over the validated
document. General 42 names no overlay, so nothing here runs for it.

The two kernels are pure functions: ``evaluate_calibration`` wraps the plan's
``calibration_difference`` behind value, unit and scope checks, and
``assess_coordination`` extends the identity kernel with disclosed temporal,
content and account evidence. Neither kernel claims accuracy or intent.

``assess_cluster_priority`` takes the origin authority set from its caller. An
origin authority digest a record asserts for itself proves only that its writer
can format hex, so it is never authority here: an origin the injected set does
not declare counts as unknown, and priority reaches elevated on verified origins
alone.
"""

from __future__ import annotations

import copy
import math
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.general_question_context_identity import (
    evidence_groups,
    observation_key,
    origin_authority_projection,
    validate_origin_authorities,
)
from src.enrichment import topic_overlays

CLIENT_OVERLAY_VERSION = "client_overlay_v1"
PURPOSE_POLICY_VERSION = "client_purpose_policy_v1"
LENS_POLICY_VERSION = "client_lens_policy_v1"
NOISE_POLICY_VERSION = "client_noise_policy_v1"
COORDINATION_POLICY_VERSION = "coordination_policy_v1"
CALIBRATION_THRESHOLD = 0.10

CLIENT_FIELDS = (
    "client_overlay_contract_version",
    "verified_handles",
    "ambassador_refs",
    "spokesperson_policy",
    "risk_topics",
    "languages",
    "comparator_set_ref",
    "search_visibility_policy_ref",
    "purpose_policy",
    "lens_policy",
    "noise_controls",
)
OVERLAY_FIELDS = topic_overlays.OVERLAY_FIELDS | frozenset(CLIENT_FIELDS)
PURPOSE_CODES = (
    "political_party_purpose",
    "electoral_purpose",
    "voter_targeting_purpose",
    "agency_position_live_regulatory_matter",
)
PURPOSE_STAGES = {
    "admission": "client_purpose_refused_at_admission",
    "projection": "client_purpose_refused_at_projection",
    "export": "client_purpose_refused_at_export",
}
SPOKESPERSON_POLICY = {
    "role": "mention_context_only",
    "individual_profiling": "refused",
    "official_sentiment_scoring": "refused",
}
LENS_CLUSTERS = {
    "investment": "source_authority_over_volume",
    "energy": "verified_independent_international_pickup",
    "land": "author_geography_reported_explicitly",
}
NOISE_CODES = (
    "betting_spam",
    "crypto_forex_promotion",
    "giveaways",
    "ticket_resale",
    "promotional_adult_content",
    "follow_farming",
    "pure_scoreline_noise",
)
WATCH_RULES = ("opportunity", "event")
DIMENSION_DISTINCTIONS = {
    "tourism": ("travel_intent", "review"),
    "talent": ("inbound", "outbound"),
    "prominence": ("mention_share", "reach", "comparator"),
}
HANDLE_FIELDS = ("entity_id", "platform", "handle", "observed_at", "evidence_ref")
AMBASSADOR_FIELDS = ("ref", "entity_id", "handle", "observed_at", "evidence_ref")
COORDINATION_POLICY_FIELDS = (
    "policy_version",
    "synchrony_window_seconds",
    "min_accounts",
    "recurring_carrier_min_observations",
)
COORDINATION_FIELDS = (
    "market",
    "platform",
    "source_row_id",
    "content_digest",
    "account_id",
    "published_at",
    "repost_of",
)
COMPETING_EXPLANATIONS = (
    "independent reuse of a shared phrase or source",
    "a platform share feature whose ancestry was not disclosed",
    "a newsroom or wire copy republished by unrelated accounts",
    "coordinated posting",
)

_PLACEHOLDER = re.compile(
    r"(?i)(?:(?<![a-z0-9])(?:tbc|tbd|todo|placeholder|pending|unsupplied|xxx+)(?![a-z0-9])"
    r"|<[^<>]*>|\{\{[^{}]*\}\})"
)
_HANDLE = re.compile(r"@[A-Za-z0-9_.]{1,64}\Z")
_REF = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[A-Za-z]{4})?\Z")
_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")


class ClientOverlayInvalid(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ClientPurposeProhibited(ValueError):
    """A prohibited client purpose at one enforcement stage, never overridable."""

    def __init__(self, stage: str, evaluation: dict) -> None:
        self.stage = stage
        self.code = PURPOSE_STAGES[stage]
        self.purpose_codes = tuple(evaluation["purpose_codes"])
        self.policy_version = evaluation["policy_version"]
        self.evaluation = evaluation
        super().__init__(self.code)


class CalibrationInvalid(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CoordinationInvalid(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str):
    raise ClientOverlayInvalid(code)


def _string(value: object, code: str, *, placeholders: bool = True) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        _fail(code)
    if placeholders and _PLACEHOLDER.search(value):
        _fail(f"placeholder_input:{code}")
    return value


def _strings(value: object, code: str, *, nonempty: bool, placeholders: bool = True) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        _fail(code)
    items = [_string(item, code, placeholders=placeholders) for item in value]
    if len(items) != len(set(items)):
        _fail(code)
    return items


def _patterns(value: object, code: str) -> list[str]:
    # A pattern is a rule, not a supplied value; the placeholder check applies to values.
    items = _strings(value, code, nonempty=True, placeholders=False)
    for item in items:
        try:
            re.compile(item)
        except re.error:
            _fail(code)
    return items


def _instant(value: object, code: str) -> str:
    if not isinstance(value, str) or _INSTANT.fullmatch(value) is None:
        _fail(code)
    try:
        datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        _fail(code)
    return value


def _exact(value: object, fields: tuple[str, ...], code: str) -> dict:
    if not isinstance(value, dict) or set(value) != set(fields):
        _fail(code)
    return value


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, MappingProxyType):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _base_document(document: object) -> dict:
    """The U02 loader's own validation over the same bytes, then the client shape."""
    if not isinstance(document, dict):
        _fail("overlay_shape_invalid")
    unknown = set(document) - OVERLAY_FIELDS
    if unknown:
        _fail("unknown_field:" + ",".join(sorted(unknown)))
    missing = OVERLAY_FIELDS - set(document)
    if missing:
        _fail("missing_field:" + ",".join(sorted(missing)))
    try:
        base = topic_overlays.validate_topic_overlay(document, overlay_id=document["overlay_id"])
    except topic_overlays.TopicOverlayInvalid as error:
        raise ClientOverlayInvalid(error.code) from error
    if document["client_overlay_contract_version"] != CLIENT_OVERLAY_VERSION:
        _fail("client_overlay_version_invalid")
    return base


def _entities(document: dict) -> dict:
    entities = document["entities"]
    for entity_id, entity in entities.items():
        code = f"entity_invalid:{entity_id}"
        if not entity.get("aliases") or not entity["property"].strip():
            _fail(f"entity_empty:{entity_id}")
        _strings(entity["aliases"], code, nonempty=True)
    for entity_id, rule in (
        ("entity_bsa", "exact_phrase_or_handle"),
        ("entity_gsa", "exclude_global_south"),
        ("entity_pyp", "require_south_africa_cooccurrence"),
    ):
        if entity_id not in entities or entities[entity_id]["rule"] != rule:
            _fail(f"entity_rule_invalid:{entity_id}")
    if entities["entity_bsa"]["property"] != "Brand South Africa":
        _fail("entity_rule_invalid:entity_bsa")
    if "Global South" not in entities["entity_gsa"].get("exclusions", []):
        _fail("entity_rule_invalid:entity_gsa")
    if set(entities["entity_pyp"]["cooccurrence_any"]) != {
        "hashtag_form",
        "south_africa_entity_term",
        "known_ambassador_handle",
    }:
        _fail("entity_rule_invalid:entity_pyp")
    return entities


def _handles(document: dict, entities: dict) -> list[dict]:
    value = document["verified_handles"]
    if not isinstance(value, list):
        _fail("verified_handles_invalid")
    handles = []
    for index, item in enumerate(value):
        code = f"handle_invalid:{index}"
        if not isinstance(item, dict) or not {"entity_id", "handle"} <= set(item):
            _fail(code)
        handle = _string(item["handle"], code)
        if _HANDLE.fullmatch(handle) is None:
            _fail(code)
        if item.get("observed_at") is None:
            _fail(f"handle_unverified:{handle}")
        _exact(item, HANDLE_FIELDS, code)
        if item["entity_id"] not in entities:
            _fail(code)
        _string(item["platform"], code)
        _instant(item["observed_at"], f"handle_unverified:{handle}")
        _string(item["evidence_ref"], code)
        handles.append(copy.deepcopy(item))
    if len({(h["platform"], h["handle"].casefold()) for h in handles}) != len(handles):
        _fail("verified_handles_invalid")
    return handles


def _ambassadors(document: dict, entities: dict) -> list[dict]:
    value = document["ambassador_refs"]
    if not isinstance(value, list):
        _fail("ambassador_refs_invalid")
    refs = []
    for index, item in enumerate(value):
        code = f"ambassador_invalid:{index}"
        if not isinstance(item, dict) or "ref" not in item:
            _fail(code)
        ref = _string(item["ref"], code)
        if item.get("observed_at") is None:
            _fail(f"ambassador_unverified:{ref}")
        _exact(item, AMBASSADOR_FIELDS, code)
        if item["entity_id"] != "entity_pyp" or "entity_pyp" not in entities:
            _fail(code)
        handle = _string(item["handle"], code)
        if _HANDLE.fullmatch(handle) is None:
            _fail(code)
        _instant(item["observed_at"], f"ambassador_unverified:{ref}")
        _string(item["evidence_ref"], code)
        refs.append(copy.deepcopy(item))
    if len({r["ref"] for r in refs}) != len(refs):
        _fail("ambassador_refs_invalid")
    return refs


def _purpose_policy(value: object) -> dict:
    code = "purpose_policy_invalid"
    _exact(value, ("policy_version", "source_definition", "prohibited_purposes"), code)
    if value["policy_version"] != PURPOSE_POLICY_VERSION:
        _fail("purpose_policy_version_invalid")
    _string(value["source_definition"], code)
    purposes = value["prohibited_purposes"]
    if not isinstance(purposes, list) or [
        p.get("code") for p in purposes if isinstance(p, dict)
    ] != list(PURPOSE_CODES):
        _fail(code)
    for purpose in purposes:
        if purpose["code"] == "agency_position_live_regulatory_matter":
            _exact(purpose, ("code", "position_claim_patterns", "regulatory_matter_patterns"), code)
            _patterns(purpose["position_claim_patterns"], code)
            _patterns(purpose["regulatory_matter_patterns"], code)
        else:
            _exact(purpose, ("code", "patterns"), code)
            _patterns(purpose["patterns"], code)
    return copy.deepcopy(value)


def _lens_policy(value: object) -> dict:
    code = "lens_policy_invalid"
    _exact(
        value,
        (
            "policy_version",
            "clusters",
            "authority_definition",
            "pickup_definition",
            "geography_rule",
        ),
        code,
    )
    if value["policy_version"] != LENS_POLICY_VERSION:
        _fail("lens_policy_version_invalid")
    clusters = value["clusters"]
    if not isinstance(clusters, dict) or set(clusters) != set(LENS_CLUSTERS):
        _fail(code)
    for cluster, priority in LENS_CLUSTERS.items():
        _exact(clusters[cluster], ("priority_basis", "source_note"), code)
        if clusters[cluster]["priority_basis"] != priority:
            _fail(code)
        _string(clusters[cluster]["source_note"], code)
    authority = _exact(
        value["authority_definition"],
        ("basis", "authoritative_source_roles", "minimum_authoritative_origins", "missing"),
        code,
    )
    if authority["basis"] != "admitted_metadata_and_reviewed_source_roles":
        _fail(code)
    _strings(authority["authoritative_source_roles"], code, nonempty=True)
    if type(authority["minimum_authoritative_origins"]) is not int or (
        authority["minimum_authoritative_origins"] < 1
    ):
        _fail(code)
    pickup = _exact(
        value["pickup_definition"],
        ("basis", "international_source_roles", "requires_established_origin", "missing"),
        code,
    )
    if pickup["basis"] != "admitted_metadata_and_reviewed_source_roles":
        _fail(code)
    _strings(pickup["international_source_roles"], code, nonempty=True)
    if pickup["requires_established_origin"] is not True:
        _fail(code)
    geography = _exact(value["geography_rule"], ("subject_fills_author_geography", "missing"), code)
    if geography["subject_fills_author_geography"] is not False:
        _fail(code)
    if (
        authority["missing"] != "unknown"
        or pickup["missing"] != "unknown"
        or geography["missing"] != "unknown"
    ):
        _fail(code)
    return copy.deepcopy(value)


def _noise_controls(value: object) -> dict:
    code = "noise_controls_invalid"
    _exact(value, ("policy_version", "source_definition", "exclusions", "watch_rules"), code)
    if value["policy_version"] != NOISE_POLICY_VERSION:
        _fail("noise_controls_version_invalid")
    _string(value["source_definition"], code)
    exclusions = value["exclusions"]
    if not isinstance(exclusions, list) or [
        e.get("code") for e in exclusions if isinstance(e, dict)
    ] != list(NOISE_CODES):
        _fail(code)
    for exclusion in exclusions:
        _exact(exclusion, ("code", "definition", "patterns", "retain_when"), code)
        _string(exclusion["definition"], code)
        _patterns(exclusion["patterns"], code)
        _patterns(exclusion["retain_when"], code)
    watch = value["watch_rules"]
    if not isinstance(watch, dict) or set(watch) != set(WATCH_RULES):
        _fail(code)
    for rule in watch.values():
        _exact(rule, ("definition", "patterns"), code)
        _string(rule["definition"], code)
        _patterns(rule["patterns"], code)
    return copy.deepcopy(value)


def _dimensions(document: dict) -> dict:
    dimensions = document["dimensions"]
    for dimension_id, distinctions in DIMENSION_DISTINCTIONS.items():
        code = f"dimension_invalid:{dimension_id}"
        if dimension_id not in dimensions:
            _fail(code)
        value = dimensions[dimension_id].get("distinctions")
        if not isinstance(value, dict) or tuple(value) != distinctions:
            _fail(code)
        for patterns in value.values():
            _patterns(patterns, code)
    return dimensions


def compile_client_overlay(document: dict):
    """Validate one client overlay document and return it read only with its digest.

    The digest is the canonical digest (sorted keys, compact separators, UTF-8,
    no NaN) of the validated document, so any change to any field is a different
    lens. The compiled mapping and everything under it is immutable; the
    ambassador handles supplied are carried into the Play Your Part entity so
    the U02 matcher sees them, and the raw document is never mutated.
    """
    base = _base_document(document)
    entities = _entities(base)
    handles = _handles(document, entities)
    ambassadors = _ambassadors(document, entities)
    _exact(
        document["spokesperson_policy"], tuple(SPOKESPERSON_POLICY), "spokesperson_policy_invalid"
    )
    if document["spokesperson_policy"] != SPOKESPERSON_POLICY:
        _fail("spokesperson_policy_invalid")
    risk_topics = _strings(document["risk_topics"], "risk_topics_invalid", nonempty=True)
    for topic in risk_topics:
        if _REF.fullmatch(topic) is None:
            _fail("risk_topics_invalid")
    languages = _exact(document["languages"], ("domestic", "international"), "languages_invalid")
    for group in ("domestic", "international"):
        for tag in _strings(languages[group], "languages_invalid", nonempty=True):
            if _LANGUAGE.fullmatch(tag) is None:
                _fail("languages_invalid")
    if set(languages["domestic"]) & set(languages["international"]):
        _fail("languages_invalid")
    for field in ("comparator_set_ref", "search_visibility_policy_ref"):
        if _REF.fullmatch(_string(document[field], f"{field}_invalid")) is None:
            _fail(f"{field}_invalid")
    purpose = _purpose_policy(document["purpose_policy"])
    lens = _lens_policy(document["lens_policy"])
    noise = _noise_controls(document["noise_controls"])
    dimensions = _dimensions(base)
    validated = {
        **copy.deepcopy(base),
        "client_overlay_contract_version": CLIENT_OVERLAY_VERSION,
        "verified_handles": handles,
        "ambassador_refs": ambassadors,
        "spokesperson_policy": dict(SPOKESPERSON_POLICY),
        "risk_topics": risk_topics,
        "languages": copy.deepcopy(languages),
        "comparator_set_ref": document["comparator_set_ref"],
        "search_visibility_policy_ref": document["search_visibility_policy_ref"],
        "purpose_policy": purpose,
        "lens_policy": lens,
        "noise_controls": noise,
    }
    validated["dimensions"] = copy.deepcopy(dimensions)
    validated["entities"] = copy.deepcopy(entities)
    validated["entities"]["entity_pyp"]["known_ambassador_handles"] = [
        item["handle"] for item in ambassadors
    ]
    digest = canonical_digest(validated)
    return _freeze(
        {
            **validated,
            "overlay_digest": digest,
            "purpose_policy_digest": canonical_digest(purpose),
            "lens_policy_digest": canonical_digest(lens),
            "noise_policy_digest": canonical_digest(noise),
        }
    )


def compiled_overlay_for_brand(brand_config_id: str, *, root: Path | None = None):
    """Load the brand's document through U02's loader and compile it."""
    return compile_client_overlay(topic_overlays.load_topic_overlay(brand_config_id, root=root))


def compiled_overlay_for_scope(
    scope: dict, *, root: Path | None = None, lens=None, registry_path=None
):
    """General 42 has no client overlay; an authorized lens selects the rest.

    The document this compiles is the one the scope's own registry entry, or the
    resolved lens the request carries, authorizes and pins. A brand name on its
    own no longer reaches a configuration.
    """
    brand = scope.get("brand_config_id") if isinstance(scope, dict) else None
    if brand is None:
        return None
    overlay = topic_overlays.overlay_for_scope(
        scope, root=root, lens=lens, registry_path=registry_path
    )
    compiled = compile_client_overlay(overlay)
    if compiled["brand_config_id"] != brand:
        _fail("overlay_binding_invalid")
    return compiled


def evaluate_client_purpose(policy, texts) -> dict:
    """Which prohibited purposes the texts serve under the policy, with the matched text."""
    evidence = []
    for index, text in enumerate(texts):
        if not isinstance(text, str) or not text.strip():
            continue
        for purpose in policy["prohibited_purposes"]:
            if purpose["code"] == "agency_position_live_regulatory_matter":
                position = next(
                    (
                        m
                        for p in purpose["position_claim_patterns"]
                        if (m := _compiled(p).search(text))
                    ),
                    None,
                )
                matter = next(
                    (
                        m
                        for p in purpose["regulatory_matter_patterns"]
                        if (m := _compiled(p).search(text))
                    ),
                    None,
                )
                if position is not None and matter is not None:
                    evidence.append(
                        {
                            "purpose_code": purpose["code"],
                            "text_index": index,
                            "matched": [position.group(0), matter.group(0)],
                        }
                    )
                continue
            for pattern in purpose["patterns"]:
                match = _compiled(pattern).search(text)
                if match is not None:
                    evidence.append(
                        {
                            "purpose_code": purpose["code"],
                            "text_index": index,
                            "matched": [match.group(0)],
                        }
                    )
                    break
    codes = sorted({item["purpose_code"] for item in evidence})
    return {
        "policy_version": policy["policy_version"],
        "state": "prohibited" if codes else "permitted",
        "purpose_codes": codes,
        "evidence": evidence,
        "review_override": "refused",
    }


def refuse_prohibited_client_purpose(
    scope, texts, *, stage: str, root: Path | None = None, lens=None, registry_path=None
) -> dict | None:
    """Enforce the scope's client purpose policy at one stage; general 42 is untouched."""
    if stage not in PURPOSE_STAGES:
        raise ValueError("purpose_stage_invalid")
    compiled = compiled_overlay_for_scope(
        scope, root=root, lens=lens, registry_path=registry_path
    )
    if compiled is None:
        return None
    evaluation = evaluate_client_purpose(compiled["purpose_policy"], list(texts))
    if evaluation["state"] == "prohibited":
        raise ClientPurposeProhibited(stage, evaluation)
    return evaluation


def classify_dimensions(compiled, text: str) -> dict:
    """The overlay's dimensions and their distinctions matched in one text."""
    if not isinstance(text, str) or not text.strip():
        return {}
    result = {}
    for dimension_id, dimension in compiled["dimensions"].items():
        terms = [t for t in dimension["terms"] if topic_overlays._phrase(t).search(text)]
        distinctions = [
            name
            for name, patterns in dimension.get("distinctions", {}).items()
            if any(_compiled(p).search(text) for p in patterns)
        ]
        if terms or distinctions:
            result[dimension_id] = {"terms": terms, "distinctions": distinctions}
    return result


def apply_noise_controls(compiled, text: str) -> dict:
    """The client's exclusions and watch rules over one text; retained context wins."""
    controls = compiled["noise_controls"]
    excluded = []
    retained = []
    if isinstance(text, str) and text.strip():
        for exclusion in controls["exclusions"]:
            if not any(_compiled(p).search(text) for p in exclusion["patterns"]):
                continue
            if any(_compiled(p).search(text) for p in exclusion["retain_when"]):
                retained.append(exclusion["code"])
            else:
                excluded.append(exclusion["code"])
        watch = [
            rule
            for rule, value in controls["watch_rules"].items()
            if any(_compiled(p).search(text) for p in value["patterns"])
        ]
    else:
        watch = []
    return {
        "policy_version": controls["policy_version"],
        "excluded": bool(excluded),
        "exclusion_codes": excluded,
        "retained_codes": retained,
        "watch": watch,
        "visible": not excluded,
    }


def _lens_records(observations) -> list[dict]:
    if not isinstance(observations, list):
        raise ClientOverlayInvalid("lens_observations_invalid")
    records = []
    for record in observations:
        if not isinstance(record, dict):
            raise ClientOverlayInvalid("lens_observations_invalid")
        try:
            key = observation_key(record)
        except ValueError as error:
            raise ClientOverlayInvalid("lens_observations_invalid") from error
        records.append({**record, "observation_key": key})
    return records


def _verified_origin(record: dict, authorities: dict) -> str | None:
    """The record's origin, only where the injected authority set declares its digest."""
    origin = record.get("origin_id")
    if origin is None:
        return None
    if authorities.get(origin) != record.get("origin_authority_digest"):
        return None
    return origin


def assess_cluster_priority(compiled, cluster: str, observations, *, origin_authorities) -> dict:
    """Apply the lens policy's cluster rule to admitted observations.

    Authority, pickup and geography come only from admitted metadata fields
    (``source_role``, ``origin_id`` with its authority digest, ``author_geography``);
    a missing field is unknown and never filled from the subject or the volume.

    ``origin_authorities`` is the authority set the caller injects: origin ids mapped to
    the authority digest that establishes them. An origin the set does not declare, or one
    whose record asserts a different digest, cannot be verified and counts as unknown
    rather than as an independent authority, so priority reaches elevated on verified
    origins only. A well shaped digest a record asserts for itself proves only that its
    writer can format hex, and thirty copies of one story naming thirty self asserted
    origins stay one unelevated baseline.
    """
    lens = compiled["lens_policy"]
    try:
        authorities = validate_origin_authorities(origin_authorities)
    except ValueError as error:
        raise ClientOverlayInvalid("lens_origin_authorities_invalid") from error
    if cluster not in lens["clusters"]:
        raise ClientOverlayInvalid("lens_cluster_unknown")
    records = _lens_records(observations)
    try:
        groups = evidence_groups(
            [{k: v for k, v in r.items() if k != "observation_key"} for r in records]
        )
    except ValueError as error:
        raise ClientOverlayInvalid("lens_observations_invalid") from error
    basis = lens["clusters"][cluster]["priority_basis"]
    if cluster == "investment":
        roles = set(lens["authority_definition"]["authoritative_source_roles"])
        authoritative = {}
        unknown = 0
        for record in records:
            role = record.get("source_role")
            origin = _verified_origin(record, authorities)
            if role is None or origin is None or origin not in groups["origin_groups"]:
                unknown += 1
                continue
            if role in roles:
                authoritative.setdefault(origin, []).append(record["observation_key"])
        minimum = lens["authority_definition"]["minimum_authoritative_origins"]
        return {
            "cluster": cluster,
            "priority_basis": basis,
            "raw_volume": len(groups["observation_keys"]),
            "authoritative_origins": {k: sorted(v) for k, v in sorted(authoritative.items())},
            "authoritative_origin_count": len(authoritative),
            "unknown_authority_count": unknown,
            "priority": "elevated" if len(authoritative) >= minimum else "baseline",
        }
    if cluster == "energy":
        roles = set(lens["pickup_definition"]["international_source_roles"])
        qualifying = []
        unknown = 0
        for record in records:
            geography = record.get("author_geography")
            role = record.get("source_role")
            origin = _verified_origin(record, authorities)
            if geography is None or role is None or origin is None:
                unknown += 1
                continue
            if (
                geography != "za"
                and role in roles
                and origin in groups["origin_groups"]
                and record.get("repost_of") is None
            ):
                qualifying.append(
                    {
                        "observation_key": record["observation_key"],
                        "author_geography": geography,
                        "source_role": role,
                        "origin_id": origin,
                    }
                )
        return {
            "cluster": cluster,
            "priority_basis": basis,
            "raw_volume": len(groups["observation_keys"]),
            "qualifying_pickup": sorted(qualifying, key=lambda item: item["observation_key"]),
            "unknown_pickup_count": unknown,
            "priority": "elevated" if qualifying else "baseline",
        }
    counts: dict[str, int] = {}
    unknown = 0
    for record in records:
        geography = record.get("author_geography")
        if geography is None:
            unknown += 1
            continue
        counts[geography] = counts.get(geography, 0) + 1
    return {
        "cluster": cluster,
        "priority_basis": basis,
        "raw_volume": len(groups["observation_keys"]),
        "author_geography": dict(sorted(counts.items())),
        "unknown_geography_count": unknown,
        "subject_country_used_for_geography": False,
    }


def calibration_difference(reference: float, actual: float) -> dict:
    """The plan's kernel: a zero baseline is never a zero variance."""
    delta = actual - reference
    return {
        "absolute_difference": delta,
        "relative_difference": abs(delta) / abs(reference) if reference else None,
        "requires_explanation": (
            actual != 0 if reference == 0 else abs(delta) / abs(reference) > CALIBRATION_THRESHOLD
        ),
    }


def _measurement(value: object, side: str) -> dict:
    if not isinstance(value, dict) or set(value) != {"value", "unit", "scope"}:
        raise CalibrationInvalid(f"calibration_field_invalid:{side}")
    number = value["value"]
    if (
        isinstance(number, bool)
        or not isinstance(number, (int, float))
        or not math.isfinite(number)
    ):
        raise CalibrationInvalid(f"calibration_value_invalid:{side}")
    if not isinstance(value["unit"], str) or not value["unit"].strip():
        raise CalibrationInvalid(f"calibration_field_invalid:{side}")
    if not isinstance(value["scope"], dict) or not value["scope"]:
        raise CalibrationInvalid(f"calibration_field_invalid:{side}")
    return value


def evaluate_calibration(expected: dict, observed: dict) -> dict:
    """Compare a reference measurement with an observed one under the plan's kernel.

    Finite numbers, one unit and one scope are required before the kernel runs.
    A difference over the threshold requires a source backed explanation; an
    explanation is never an accuracy pass, the reviewer decides whether the two
    methods answer the same question.
    """
    reference = _measurement(expected, "expected")
    actual = _measurement(observed, "observed")
    if reference["unit"] != actual["unit"]:
        raise CalibrationInvalid("calibration_unit_mismatch")
    if canonical_digest(reference["scope"]) != canonical_digest(actual["scope"]):
        raise CalibrationInvalid("calibration_scope_mismatch")
    difference = calibration_difference(reference["value"], actual["value"])
    return {
        "contract_version": "client_calibration_v1",
        "unit": reference["unit"],
        "scope": copy.deepcopy(reference["scope"]),
        "reference": reference["value"],
        "actual": actual["value"],
        **difference,
        "threshold": CALIBRATION_THRESHOLD,
        "explanation": "source_backed_explanation_required"
        if difference["requires_explanation"]
        else "not_required",
        "accuracy_pass": None,
        "reviewer_decision": "required",
    }


def _coordination_policy(policy: object) -> dict:
    if not isinstance(policy, dict) or set(policy) != set(COORDINATION_POLICY_FIELDS):
        raise CoordinationInvalid("coordination_policy_invalid")
    if policy["policy_version"] != COORDINATION_POLICY_VERSION:
        raise CoordinationInvalid("coordination_policy_invalid")
    for field in COORDINATION_POLICY_FIELDS[1:]:
        if type(policy[field]) is not int or policy[field] < 1:
            raise CoordinationInvalid("coordination_policy_invalid")
    if policy["min_accounts"] < 2:
        raise CoordinationInvalid("coordination_policy_invalid")
    return policy


def _coordination_records(observations: object) -> list[dict]:
    if not isinstance(observations, list):
        raise CoordinationInvalid("coordination_observation_invalid:observations")
    records = []
    for record in observations:
        if not isinstance(record, dict) or not set(COORDINATION_FIELDS) <= set(record):
            raise CoordinationInvalid("coordination_observation_invalid:fields")
        try:
            key = observation_key(record)
        except ValueError as error:
            raise CoordinationInvalid("coordination_observation_invalid:identity") from error
        account = record["account_id"]
        if not isinstance(account, str) or not account.strip():
            raise CoordinationInvalid("coordination_observation_invalid:account_id")
        published = record["published_at"]
        if not isinstance(published, str) or _INSTANT.fullmatch(published) is None:
            raise CoordinationInvalid("coordination_observation_invalid:published_at")
        instant = datetime.fromisoformat(published.replace("Z", "+00:00"))
        repost_of = record["repost_of"]
        if repost_of is not None and (not isinstance(repost_of, str) or not repost_of.strip()):
            raise CoordinationInvalid("coordination_observation_invalid:repost_of")
        records.append({**record, "observation_key": key, "instant": instant})
    return records


def assess_coordination(observations: list[dict], policy: dict) -> dict:
    """Recurring carriers and candidate coordination from disclosed evidence only.

    Ancestry is a disclosed ``repost_of`` reference to another observation on the
    same market and platform; the earliest observation is reported as earliest
    observed, never as an originator. Identical content across distinct accounts
    inside the synchrony window is a candidate with its evidence and competing
    explanations; the same content outside the window, or without disclosed
    ancestry, is left unassessed. The result never claims intent.
    """
    policy = _coordination_policy(policy)
    records = _coordination_records(observations)
    identity_records = [
        {k: v for k, v in r.items() if k not in ("observation_key", "instant")} for r in records
    ]
    identity = evidence_groups(identity_records)
    # A zero origin count over observations that never carried the field is not a measured
    # zero, so the assessment publishes which of the two it is beside the counts.
    projection = origin_authority_projection(identity_records)
    by_key = {}
    for record in records:
        by_key.setdefault(record["observation_key"], record)
    ordered = sorted(by_key.values(), key=lambda r: (r["instant"], r["observation_key"]))
    chains: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {}
    for record in ordered:
        parent = record["repost_of"]
        if parent is None:
            continue
        parent_key = (record["market"], record["platform"], parent)
        if parent_key not in by_key or parent_key == record["observation_key"]:
            raise CoordinationInvalid("coordination_ancestry_invalid")
        root = parent_key
        seen = {record["observation_key"]}
        while by_key[root]["repost_of"] is not None:
            if root in seen:
                raise CoordinationInvalid("coordination_ancestry_invalid")
            seen.add(root)
            root = (root[0], root[1], by_key[root]["repost_of"])
            if root not in by_key:
                raise CoordinationInvalid("coordination_ancestry_invalid")
        chains.setdefault(root, []).append(record["observation_key"])
    repost_chains = [
        {"root_key": root, "member_keys": sorted(members), "disclosed": True}
        for root, members in sorted(chains.items())
    ]
    chained = {key for members in chains.values() for key in members}
    carriers: dict[str, list[tuple[str, str, str]]] = {}
    for record in ordered:
        carriers.setdefault(record["account_id"], []).append(record["observation_key"])
    minimum = policy["recurring_carrier_min_observations"]
    recurring = [
        {"account_id": account, "observation_count": len(keys), "observation_keys": sorted(keys)}
        for account, keys in sorted(carriers.items())
        if len(keys) >= minimum
    ]
    by_content: dict[str, list[dict]] = {}
    for record in ordered:
        if record["observation_key"] in chained:
            continue
        by_content.setdefault(record["content_digest"], []).append(record)
    window = policy["synchrony_window_seconds"]
    candidates = []
    unassessed = []
    for digest, members in sorted(by_content.items()):
        selected = []
        position = 0
        while position < len(members):
            end = position
            for candidate_end in range(position, len(members)):
                span = (
                    members[candidate_end]["instant"] - members[position]["instant"]
                ).total_seconds()
                if span > window:
                    break
                accounts = {
                    member["account_id"] for member in members[position : candidate_end + 1]
                }
                if len(accounts) >= policy["min_accounts"]:
                    end = candidate_end
            if end == position:
                position += 1
                continue
            selected.append(members[position : end + 1])
            position = end + 1
        selected_keys = {
            member["observation_key"] for selection in selected for member in selection
        }
        for selection in selected:
            accounts = sorted({member["account_id"] for member in selection})
            keys = sorted(member["observation_key"] for member in selection)
            span = (selection[-1]["instant"] - selection[0]["instant"]).total_seconds()
            candidates.append(
                {
                    "candidate_id": canonical_digest({"content_digest": digest, "keys": keys}),
                    "content_digest": digest,
                    "account_ids": accounts,
                    "observation_keys": keys,
                    "evidence": {
                        "temporal": {"span_seconds": span, "window_seconds": window},
                        "content": {"identical_content_digest": True},
                        "account": {"distinct_accounts": len(accounts)},
                        "ancestry": "undisclosed",
                    },
                    "competing_explanations": list(COMPETING_EXPLANATIONS),
                    "intent_claim": None,
                }
            )
        leftovers = [member for member in members if member["observation_key"] not in selected_keys]
        accounts = sorted({member["account_id"] for member in leftovers})
        keys = sorted(member["observation_key"] for member in leftovers)
        if leftovers and (selected or len(accounts) >= 2):
            span = (leftovers[-1]["instant"] - leftovers[0]["instant"]).total_seconds()
            unassessed.append(
                {
                    "content_digest": digest,
                    "account_ids": accounts,
                    "observation_keys": keys,
                    "reason": "shared_content_outside_synchrony_window"
                    if selected or span > window
                    else "shared_content_below_minimum_accounts",
                    "ancestry": "undisclosed",
                }
            )
    earliest = ordered[0] if ordered else None
    return {
        "contract_version": "coordination_assessment_v1",
        "policy_version": policy["policy_version"],
        "observation_keys": identity["observation_keys"],
        "independent_origin_count": identity["independent_origin_count"],
        "unknown_origin_count": identity["unknown_origin_count"],
        "origin_authority_projection": projection,
        "earliest_observed": None
        if earliest is None
        else {
            "observation_key": earliest["observation_key"],
            "published_at": earliest["published_at"],
            "originator_claim": False,
        },
        "recurring_carriers": recurring,
        "repost_chains": repost_chains,
        "candidates": candidates,
        "unassessed": unassessed,
        "intent_claims": "none",
    }


__all__ = [
    "CLIENT_FIELDS",
    "CLIENT_OVERLAY_VERSION",
    "COORDINATION_POLICY_VERSION",
    "LENS_POLICY_VERSION",
    "NOISE_POLICY_VERSION",
    "PURPOSE_CODES",
    "PURPOSE_POLICY_VERSION",
    "PURPOSE_STAGES",
    "CalibrationInvalid",
    "ClientOverlayInvalid",
    "ClientPurposeProhibited",
    "CoordinationInvalid",
    "apply_noise_controls",
    "assess_cluster_priority",
    "assess_coordination",
    "calibration_difference",
    "classify_dimensions",
    "compile_client_overlay",
    "compiled_overlay_for_brand",
    "compiled_overlay_for_scope",
    "evaluate_calibration",
    "evaluate_client_purpose",
    "refuse_prohibited_client_purpose",
]
