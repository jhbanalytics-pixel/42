"""Provider-neutral SEO and AI visibility observation adapter."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

REQUEST_CONTRACT_VERSION = "seo_visibility_request_v2"
QUERY_AUTHORITY_CONTRACT_VERSION = "seo_public_query_authority_v1"
OBSERVATION_CONTRACT_VERSION = "seo_visibility_observation_v1"
APPROVED_MARKETS = frozenset({"za", "ng", "ke"})
_QUERY_AUTHORITY_READER_CAPABILITY = object()
_QUERY_AUTHORITY_READERS: dict[int, tuple[object, object]] = {}
REQUEST_FIELDS = (
    "adapter_contract_version",
    "request_id",
    "client_scope_id",
    "run_id",
    "markets",
    "window_start",
    "window_end",
    "queries",
    "brand_domains",
    "competitor_domains",
    "decision",
    "query_authority_receipt_id",
)
QUERY_AUTHORITY_RECEIPT_FIELDS = (
    "authority_contract_version",
    "query_authority_receipt_id",
    "request_id",
    "client_scope_id",
    "run_id",
    "query_set_digest",
    "decision_digest",
    "authority_basis",
    "approver_principal",
    "issued_at",
    "expires_at",
    "parent_public_receipt_ids",
)


def _issue_query_authority_reader(reader: Callable[[str], object]):
    if not callable(reader):
        raise ValueError("query authority reader is invalid")

    def issued(receipt_id: str):
        return reader(receipt_id)

    _QUERY_AUTHORITY_READERS[id(issued)] = (
        _QUERY_AUTHORITY_READER_CAPABILITY,
        issued,
    )
    return issued


def _is_issued_query_authority_reader(reader: object) -> bool:
    return _QUERY_AUTHORITY_READERS.get(id(reader)) == (
        _QUERY_AUTHORITY_READER_CAPABILITY,
        reader,
    )


OBSERVATION_INPUT_FIELDS = (
    "observation_contract_version",
    "request_id",
    "client_scope_id",
    "run_id",
    "market",
    "query_text",
    "query_digest",
    "window_start",
    "window_end",
    "observed_at",
    "surface",
    "provider",
    "provider_endpoint",
    "result_position",
    "result_type",
    "result_url",
    "result_domain",
    "title",
    "snippet_digest",
    "ai_answer_present",
    "ai_citation_present",
    "ai_citation_target",
    "measurement_state",
    "confidence",
    "limitations",
    "source_receipt_id",
    "cost_unit",
    "cost_debit",
)
SURFACES = frozenset(
    {"search_demand", "organic_serp", "ai_overview", "ai_citation", "brand_presence"}
)
MEASUREMENT_STATES = frozenset({"measured", "inferred", "unavailable"})
FRESHNESS_LIMIT = timedelta(hours=24)
MEASURED_VALUE_FIELDS = (
    "result_position",
    "result_type",
    "result_url",
    "result_domain",
    "title",
    "snippet_digest",
    "ai_answer_present",
    "ai_citation_present",
    "ai_citation_target",
    "confidence",
)

_UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")
_DOMAIN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_NONPUBLIC_RESEARCH = re.compile(
    r"(?:\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|"
    r"(?:\+\d{1,3}[ -]?)?(?:\d[ -]?){7,14}\b|"
    r"\b(?:confidential|internal only|personal data|unpublished|home address|"
    r"private address|personal phone|personal email|date of birth|identity number|"
    r"salary|income|net worth|bank account|medical record|marital status|whereabouts)\b)",
    re.IGNORECASE,
)
_OPAQUE_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{2,127}\Z")
_QUERY_AUTHORITY_RECEIPT_ID = re.compile(r"seqa_[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class SeoVisibilityRequest:
    adapter_contract_version: str
    request_id: str
    client_scope_id: str
    run_id: str
    markets: tuple[str, ...]
    window_start: str
    window_end: str
    queries: tuple[str, ...]
    brand_domains: tuple[str, ...]
    competitor_domains: tuple[str, ...]
    decision: str
    query_authority_receipt_id: str


@dataclass(frozen=True, slots=True)
class SeoVisibilityObservation:
    observation_contract_version: str
    observation_id: str
    request_id: str
    client_scope_id: str
    run_id: str
    market: str
    query_text: str
    query_digest: str
    window_start: str
    window_end: str
    observed_at: str
    surface: str
    provider: str | None
    provider_endpoint: str | None
    result_position: int | None
    result_type: str | None
    result_url: str | None
    result_domain: str | None
    title: str | None
    snippet_digest: str | None
    ai_answer_present: bool | None
    ai_citation_present: bool | None
    ai_citation_target: str | None
    measurement_state: str
    confidence: float | None
    limitations: tuple[str, ...]
    source_receipt_id: str | None
    cost_unit: str | None
    cost_debit: float
    created_at: str


@dataclass(frozen=True, slots=True)
class HistoricalComparison:
    state: str
    directly_comparable: bool
    query_set_continuity: bool
    provider_continuity: bool
    result_format_continuity: bool
    observation_ids: tuple[str, ...]
    transfer_limits: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StrategistClaimAssessment:
    allowed: bool
    export_allowed: bool
    human_review_required: bool
    observation_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


def _exact_text(value: object, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum:
        raise ValueError(f"{field} must be an exact nonempty string")
    return value


def _timestamp(value: object, field: str) -> tuple[str, datetime]:
    text = _exact_text(value, field)
    if _UTC_TIMESTAMP.fullmatch(text) is None:
        raise ValueError("window must use closed UTC timestamps")
    return text, datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _ordered_strings(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
    item_maximum: int = 256,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{field} is outside its exact bound")
    items = tuple(_exact_text(item, field, maximum=item_maximum) for item in value)
    if len({item.casefold() for item in items}) != len(items):
        raise ValueError(f"{field} must be ordered and deduplicated")
    return items


def _domains(value: object, field: str, maximum: int) -> tuple[str, ...]:
    domains = _ordered_strings(value, field, minimum=0, maximum=maximum, item_maximum=253)
    if any(domain != domain.lower() or _DOMAIN.fullmatch(domain) is None for domain in domains):
        raise ValueError(f"{field} contains a noncanonical domain")
    return domains


def _optional_text(value: object, field: str, *, maximum: int = 2048) -> str | None:
    if value is None:
        return None
    return _exact_text(value, field, maximum=maximum)


def _optional_bool(value: object, field: str) -> bool | None:
    if value is not None and type(value) is not bool:
        raise ValueError(f"{field} must be boolean or null")
    return value


def _optional_confidence(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("confidence must be numeric or null")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError("confidence must be between zero and one")
    return result


def _https_url(value: object, field: str) -> str | None:
    text = _optional_text(value, field)
    if text is None:
        return None
    parsed = urlsplit(text)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{field} must be an absolute HTTPS URL")
    return text


def _digest(value: object, field: str) -> str | None:
    text = _optional_text(value, field, maximum=64)
    if text is not None and re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise ValueError(f"{field} must be a lower-case SHA256")
    return text


def _canonical_digest(value: object) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validate_query_authority(
    *,
    receipt_id: str,
    request_id: str,
    client_scope_id: str,
    run_id: str,
    queries: tuple[str, ...],
    decision: str,
    query_authority_reader: Callable[[str], object],
) -> None:
    try:
        matches = query_authority_reader(receipt_id)
    except Exception as exc:
        raise ValueError("query authority is unavailable") from exc
    if not isinstance(matches, list | tuple) or len(matches) != 1:
        raise ValueError("query authority must resolve exactly once")
    receipt = matches[0]
    if not isinstance(receipt, Mapping) or set(receipt) != set(QUERY_AUTHORITY_RECEIPT_FIELDS):
        raise ValueError("query authority receipt is malformed")
    if receipt["authority_contract_version"] != QUERY_AUTHORITY_CONTRACT_VERSION:
        raise ValueError("query authority contract version is unsupported")
    resolved_id = _exact_text(
        receipt["query_authority_receipt_id"],
        "query_authority_receipt_id",
        maximum=69,
    )
    if _QUERY_AUTHORITY_RECEIPT_ID.fullmatch(resolved_id) is None or resolved_id != receipt_id:
        raise ValueError("query authority receipt identity is invalid")
    identity_payload = {
        field: receipt[field]
        for field in QUERY_AUTHORITY_RECEIPT_FIELDS
        if field != "query_authority_receipt_id"
    }
    if resolved_id != f"seqa_{_canonical_digest(identity_payload)}":
        raise ValueError("query authority receipt content address is invalid")
    authority_basis = _exact_text(receipt["authority_basis"], "authority_basis", maximum=64)
    approver = receipt["approver_principal"]
    parents = receipt["parent_public_receipt_ids"]
    if authority_basis == "human_public_review":
        if approver != "albert.meintjes@ogilvy.co.za" or parents != []:
            raise ValueError("query authority human review is invalid")
    elif authority_basis == "deterministic_public_derivative":
        if approver is not None:
            raise ValueError("query authority derivative approver is invalid")
        if not isinstance(parents, list) or not 1 <= len(parents) <= 25:
            raise ValueError("query authority derivative requires a public parent receipt")
        parent_ids = _ordered_strings(
            parents,
            "parent_public_receipt_ids",
            minimum=1,
            maximum=25,
            item_maximum=69,
        )
        if any(
            _QUERY_AUTHORITY_RECEIPT_ID.fullmatch(parent_id) is None for parent_id in parent_ids
        ):
            raise ValueError("query authority parent receipt is invalid")
    else:
        raise ValueError("query authority basis is unsupported")
    _, issued_at = _timestamp(receipt["issued_at"], "issued_at")
    _, expires_at = _timestamp(receipt["expires_at"], "expires_at")
    validated_at = datetime.now(UTC)
    if (
        expires_at < issued_at
        or expires_at - issued_at > timedelta(hours=24)
        or validated_at < issued_at
        or validated_at > expires_at
    ):
        raise ValueError("query authority receipt is expired")
    bindings = (
        (receipt["request_id"], request_id),
        (receipt["client_scope_id"], client_scope_id),
        (receipt["run_id"], run_id),
        (receipt["query_set_digest"], _canonical_digest(queries)),
        (receipt["decision_digest"], hashlib.sha256(decision.encode("utf-8")).hexdigest()),
    )
    if any(actual != expected for actual, expected in bindings):
        raise ValueError("query authority does not bind the request")


def _require_provider_evidence(values: Mapping[str, object]) -> None:
    if not values["provider"] or not values["provider_endpoint"]:
        raise ValueError("measured evidence requires a named provider and endpoint")
    if not values["source_receipt_id"]:
        raise ValueError("measured evidence requires a source receipt")


def _validate_surface(values: Mapping[str, object]) -> None:
    surface = values["surface"]
    state = values["measurement_state"]
    ai_values = (
        values["ai_answer_present"],
        values["ai_citation_present"],
        values["ai_citation_target"],
    )
    if surface not in {"ai_overview", "ai_citation", "brand_presence"} and any(
        value is not None for value in ai_values
    ):
        raise ValueError("AI fields require a direct AI measurement surface")
    occupied_result_values = (
        values["result_position"],
        values["result_url"],
        values["result_domain"],
        values["title"],
        values["snippet_digest"],
    )
    if surface == "search_demand" and any(value is not None for value in occupied_result_values):
        raise ValueError("search demand cannot carry organic result occupation")
    if (
        surface == "organic_serp"
        and state != "unavailable"
        and (
            not isinstance(values["result_position"], int)
            or isinstance(values["result_position"], bool)
            or values["result_position"] < 1
            or not values["result_type"]
            or not values["result_url"]
            or not values["result_domain"]
        )
    ):
        raise ValueError("organic result evidence is incomplete")
    if surface in {"ai_overview", "ai_citation", "brand_presence"} and state != "unavailable":
        if state != "measured" or values["result_type"] != "direct_provider_measurement":
            raise ValueError("AI evidence requires direct provider measurement")
        required_dimensions = ("engine=", "language=", "device=", "location=")
        if any(
            not any(
                item.startswith(prefix) and len(item) > len(prefix)
                for item in values["limitations"]
            )
            for prefix in required_dimensions
        ):
            raise ValueError("direct measurement dimensions are incomplete")
    if (
        surface == "ai_overview"
        and state != "unavailable"
        and (
            type(values["ai_answer_present"]) is not bool
            or any(
                value is not None
                for value in (values["ai_citation_present"], values["ai_citation_target"])
            )
        )
    ):
        raise ValueError("AI Overview evidence is invalid")
    if surface == "ai_overview" and any(value is not None for value in occupied_result_values):
        raise ValueError("AI Overview cannot carry organic result occupation")
    if surface == "brand_presence" and any(value is not None for value in ai_values):
        raise ValueError("brand presence cannot carry AI citation fields")
    if surface == "ai_citation" and state != "unavailable":
        if (
            values["ai_answer_present"] is not True
            or type(values["ai_citation_present"]) is not bool
        ):
            raise ValueError("direct AI citation evidence is invalid")
        if values["ai_citation_present"] and not values["ai_citation_target"]:
            raise ValueError("AI citation target is required")
        if values["ai_citation_present"] and (values["result_url"] != values["ai_citation_target"]):
            raise ValueError("AI citation target must equal result URL")
        if not values["ai_citation_present"] and values["ai_citation_target"] is not None:
            raise ValueError("AI citation target requires a present citation")
        if not values["ai_citation_present"] and any(
            value is not None for value in (values["result_url"], values["result_domain"])
        ):
            raise ValueError("absent citation cannot retain a result target")


def build_observation(
    payload: Mapping[str, object], *, created_at: str
) -> SeoVisibilityObservation:
    if not isinstance(payload, Mapping) or set(payload) != set(OBSERVATION_INPUT_FIELDS):
        raise ValueError("observation fields are invalid")
    if payload["observation_contract_version"] != OBSERVATION_CONTRACT_VERSION:
        raise ValueError("observation contract version is unsupported")
    market = _exact_text(payload["market"], "market", maximum=2)
    if market not in APPROVED_MARKETS:
        raise ValueError("market is unsupported")
    query_text = _exact_text(payload["query_text"], "query_text")
    query_digest = _digest(payload["query_digest"], "query_digest")
    if query_digest != hashlib.sha256(query_text.encode("utf-8")).hexdigest():
        raise ValueError("query_digest does not bind query_text")
    window_start, start = _timestamp(payload["window_start"], "window_start")
    window_end, end = _timestamp(payload["window_end"], "window_end")
    observed_at, observed = _timestamp(payload["observed_at"], "observed_at")
    created_text, created = _timestamp(created_at, "created_at")
    if end < start or end - start > timedelta(days=90) or observed < end or created < observed:
        raise ValueError("observation window or timestamps are invalid")
    surface = _exact_text(payload["surface"], "surface")
    if surface not in SURFACES:
        raise ValueError("surface is unsupported")
    state = _exact_text(payload["measurement_state"], "measurement_state")
    if state not in MEASUREMENT_STATES:
        raise ValueError("measurement state is unsupported")
    result_domain = _optional_text(payload["result_domain"], "result_domain", maximum=253)
    if result_domain is not None and (
        result_domain != result_domain.lower() or _DOMAIN.fullmatch(result_domain) is None
    ):
        raise ValueError("result_domain is invalid")
    result_url = _https_url(payload["result_url"], "result_url")
    citation_target = _https_url(payload["ai_citation_target"], "ai_citation_target")
    position = payload["result_position"]
    if position is not None and (
        isinstance(position, bool) or not isinstance(position, int) or position < 1
    ):
        raise ValueError("result_position is invalid")
    cost_debit = payload["cost_debit"]
    if isinstance(cost_debit, bool) or not isinstance(cost_debit, int | float):
        raise ValueError("cost_debit must be numeric")
    cost_debit = float(cost_debit)
    if not math.isfinite(cost_debit) or cost_debit != 0:
        raise ValueError("default authority allows zero paid cost only")
    values = {
        "observation_contract_version": OBSERVATION_CONTRACT_VERSION,
        "request_id": _exact_text(payload["request_id"], "request_id", maximum=128),
        "client_scope_id": _exact_text(payload["client_scope_id"], "client_scope_id", maximum=128),
        "run_id": _exact_text(payload["run_id"], "run_id", maximum=128),
        "market": market,
        "query_text": query_text,
        "query_digest": query_digest,
        "window_start": window_start,
        "window_end": window_end,
        "observed_at": observed_at,
        "surface": surface,
        "provider": _optional_text(payload["provider"], "provider"),
        "provider_endpoint": _optional_text(payload["provider_endpoint"], "provider_endpoint"),
        "result_position": position,
        "result_type": _optional_text(payload["result_type"], "result_type"),
        "result_url": result_url,
        "result_domain": result_domain,
        "title": _optional_text(payload["title"], "title"),
        "snippet_digest": _digest(payload["snippet_digest"], "snippet_digest"),
        "ai_answer_present": _optional_bool(payload["ai_answer_present"], "ai_answer_present"),
        "ai_citation_present": _optional_bool(
            payload["ai_citation_present"], "ai_citation_present"
        ),
        "ai_citation_target": citation_target,
        "measurement_state": state,
        "confidence": _optional_confidence(payload["confidence"]),
        "limitations": _ordered_strings(
            payload["limitations"], "limitations", minimum=1, maximum=20, item_maximum=512
        ),
        "source_receipt_id": _optional_text(
            payload["source_receipt_id"], "source_receipt_id", maximum=256
        ),
        "cost_unit": _optional_text(payload["cost_unit"], "cost_unit", maximum=64),
        "cost_debit": cost_debit,
    }
    if state in {"measured", "inferred"}:
        _require_provider_evidence(values)
    if state == "unavailable" and any(values[field] is not None for field in MEASURED_VALUE_FIELDS):
        raise ValueError("unavailable observations require null measured values")
    _validate_surface(values)
    if result_url is not None and result_domain != urlsplit(result_url).hostname:
        raise ValueError("result URL and domain disagree")
    observation_id = _canonical_digest(values)
    return SeoVisibilityObservation(
        observation_id=observation_id,
        created_at=created_text,
        **values,
    )


def require_zero_paid_call_authority(requested_paid_calls: object) -> int:
    if type(requested_paid_calls) is not int or requested_paid_calls != 0:
        raise ValueError("default authority permits exactly zero paid calls")
    return 0


def assess_historical_comparison(observations: object, *, as_of: str) -> HistoricalComparison:
    if not isinstance(observations, tuple | list) or any(
        type(observation) is not SeoVisibilityObservation for observation in observations
    ):
        raise ValueError("historical observations are invalid")
    _, as_of_time = _timestamp(as_of, "as_of")
    available = tuple(
        sorted(
            (
                observation
                for observation in observations
                if observation.measurement_state == "measured"
                and datetime.strptime(observation.created_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                    tzinfo=UTC
                )
                <= as_of_time
            ),
            key=lambda observation: (observation.created_at, observation.observation_id),
        )
    )
    observation_ids = tuple(observation.observation_id for observation in available)
    if len(available) < 2:
        return HistoricalComparison(
            state="unavailable",
            directly_comparable=False,
            query_set_continuity=False,
            provider_continuity=False,
            result_format_continuity=False,
            observation_ids=observation_ids,
            transfer_limits=("insufficient_as_of_observations",),
        )
    query_continuity = len({observation.query_digest for observation in available}) == 1
    market_continuity = len({observation.market for observation in available}) == 1
    client_scope_continuity = len({observation.client_scope_id for observation in available}) == 1
    provider_continuity = len({observation.provider for observation in available}) == 1
    endpoint_continuity = len({observation.provider_endpoint for observation in available}) == 1
    format_continuity = (
        len({(observation.surface, observation.result_type) for observation in available}) == 1
    )
    limits = []
    if not query_continuity:
        limits.append("query_set_changed")
    if not market_continuity:
        limits.append("market_changed")
    if not client_scope_continuity:
        limits.append("client_scope_changed")
    if not provider_continuity:
        limits.append("provider_changed")
    if not endpoint_continuity:
        limits.append("provider_endpoint_changed")
    if not format_continuity:
        limits.append("result_format_changed")
    directly_comparable = not limits
    return HistoricalComparison(
        state="comparable" if directly_comparable else "historical_break",
        directly_comparable=directly_comparable,
        query_set_continuity=query_continuity,
        provider_continuity=provider_continuity and endpoint_continuity,
        result_format_continuity=format_continuity,
        observation_ids=observation_ids,
        transfer_limits=tuple(limits),
    )


def _stored_observation(row: Mapping[str, object]) -> SeoVisibilityObservation:
    if not isinstance(row, Mapping):
        raise ValueError("stored observation is invalid")
    fields = SeoVisibilityObservation.__dataclass_fields__
    if any(field not in row for field in fields):
        raise ValueError("stored observation is incomplete")
    values = {field: row[field] for field in fields}
    identity_payload = {
        field: value
        for field, value in values.items()
        if field not in {"observation_id", "created_at"}
    }
    if values["observation_id"] != _canonical_digest(identity_payload):
        raise ValueError("stored observation identity is invalid")
    try:
        return SeoVisibilityObservation(**values)
    except TypeError as exc:
        raise ValueError("stored observation is invalid") from exc


def strategist_projection(
    stored_row: Mapping[str, object] | None,
    *,
    as_of: str,
    read_error: Exception | None = None,
) -> dict[str, object]:
    if read_error is not None and not isinstance(read_error, Exception):
        raise ValueError("read error is invalid")
    if stored_row is None and read_error is not None:
        projection = dict.fromkeys(SeoVisibilityObservation.__dataclass_fields__)
        projection["measurement_state"] = "unavailable"
        projection["limitations"] = ("read_failed",)
        return projection
    observation = _stored_observation(stored_row)
    _, as_of_time = _timestamp(as_of, "as_of")
    _, observed_at = _timestamp(observation.observed_at, "observed_at")
    projection = asdict(observation)
    reason = None
    if read_error is not None:
        reason = "read_failed"
    elif as_of_time < observed_at or as_of_time - observed_at > FRESHNESS_LIMIT:
        reason = "stale"
    if reason is not None:
        projection["measurement_state"] = "unavailable"
        for field in MEASURED_VALUE_FIELDS:
            projection[field] = None
        projection["limitations"] = tuple(dict.fromkeys((*observation.limitations, reason)))
    return projection


_CAUSAL_CLAIM = re.compile(
    r"\b(?:caus(?:e|ed|es|al)|drives?|drove|led to|resulted in|because of|due to|"
    r"boost(?:ed|s|ing)?|increas(?:e|ed|es|ing)|decreas(?:e|ed|es|ing)|"
    r"improv(?:e|ed|es|ing)|worsen(?:ed|s|ing)|"
    r"makes?\s+(?:demand|interest|sales|preference)\s+(?:stronger|weaker|higher|lower)|"
    r"produc(?:e|ed|es|ing)\s+(?:stronger|weaker|higher|lower)\s+"
    r"(?:demand|interest|sales|preference))\b",
    re.I,
)
_DEMOGRAPHIC_CLAIM = re.compile(
    r"\b(?:gen z|millennials?|demographic|women|men|teenagers?|youth|children|"
    r"older adults?|parents?|age group|income group|affluent|low-income|"
    r"aged?\s+\d{1,2}\s+(?:to|-)\s+\d{1,2}|"
    r"aged?\s+[a-z-]+\s+to\s+[a-z-]+)\b",
    re.I,
)
_POPULATION_CLAIM = re.compile(
    r"\b(?:people believe|public thinks|everyone|the population|population believes)\b", re.I
)
_ELECTION_CLAIM = re.compile(r"\b(?:election|vote|voter|ballot|candidate|political party)\b", re.I)
_AI_REFERENCE = re.compile(r"\bAI\b", re.I)
_VISIBILITY_REFERENCE = re.compile(r"\b(?:visibility|visible|appears?)\b", re.I)
_GENERALIZATION_REFERENCE = re.compile(r"\b(?:overall|across|everywhere|all)\b", re.I)
_NEGATED_CITATION = re.compile(
    r"\b(?:(?:did not|does not|do not|was not|were not|is not|never)\s+"
    r"(?:directly\s+)?cit(?:e|ed|es|ing)|omit(?:s|ted|ting)?|excluded?|"
    r"absent from|without)\b",
    re.I,
)
_CLAIM_KINDS = frozenset(
    {
        "descriptive",
        "ai_citation",
        "ai_visibility",
        "demographic",
        "causal",
        "population",
        "election",
    }
)


def assess_strategist_claim(
    claim_text: object,
    projections: object,
    *,
    claim_kind: str,
) -> StrategistClaimAssessment:
    claim = _exact_text(claim_text, "claim_text", maximum=2000)
    kind = _exact_text(claim_kind, "claim_kind", maximum=32)
    if kind not in _CLAIM_KINDS:
        raise ValueError("claim kind is unsupported")
    if not isinstance(projections, tuple | list) or any(
        not isinstance(projection, Mapping) for projection in projections
    ):
        raise ValueError("strategist projections are invalid")
    observation_ids = tuple(
        _exact_text(projection.get("observation_id"), "observation_id", maximum=64)
        for projection in projections
    )
    ready = bool(projections) and all(
        projection.get("measurement_state") == "measured" for projection in projections
    )
    query_digests = {
        projection.get("query_digest")
        for projection in projections
        if isinstance(projection.get("query_digest"), str)
    }
    reasons = []
    if not ready:
        reasons.append("evidence_not_ready")
    if kind == "causal" or _CAUSAL_CLAIM.search(claim):
        reasons.append("causal_claim_unsupported")
    if kind == "demographic" or _DEMOGRAPHIC_CLAIM.search(claim):
        reasons.append("demographic_claim_unsupported")
    if kind == "population" or _POPULATION_CLAIM.search(claim):
        reasons.append("population_claim_unsupported")
    if kind == "descriptive" and ready:
        supported_descriptions = {
            (
                f"{projection['result_domain']} was observed at position "
                f"{projection['result_position']} for this query."
            ).casefold()
            for projection in projections
            if projection.get("measurement_state") == "measured"
            and isinstance(projection.get("result_domain"), str)
            and type(projection.get("result_position")) is int
        }
        if claim.casefold() not in supported_descriptions:
            reasons.append("descriptive_claim_unsupported")
    if kind == "ai_visibility" and ready:
        domain_queries: dict[str, set[str]] = {}
        for projection in projections:
            domain = projection.get("result_domain")
            query_digest = projection.get("query_digest")
            if isinstance(domain, str) and isinstance(query_digest, str):
                domain_queries.setdefault(domain, set()).add(query_digest)
        supported_visibility_claims = {
            f"{domain} appeared in {len(digests)} measured queries.".casefold()
            for domain, digests in domain_queries.items()
        }
        if claim.casefold() not in supported_visibility_claims:
            reasons.append("ai_visibility_claim_unsupported")
    broad_ai_claim = (
        _AI_REFERENCE.search(claim) is not None
        and _VISIBILITY_REFERENCE.search(claim) is not None
        and _GENERALIZATION_REFERENCE.search(claim) is not None
    )
    if (kind == "ai_visibility" or broad_ai_claim) and len(query_digests) < 2:
        reasons.append("single_query_overall_visibility")
    if kind == "ai_citation":
        direct = tuple(
            projection
            for projection in projections
            if projection.get("measurement_state") == "measured"
            and projection.get("surface") == "ai_citation"
            and projection.get("ai_answer_present") is True
            and projection.get("ai_citation_present") is True
            and isinstance(projection.get("ai_citation_target"), str)
            and projection.get("ai_citation_target") == projection.get("result_url")
        )
        if not direct:
            reasons.append("citation_not_observed")
        else:
            target_domains = {
                urlsplit(str(projection["ai_citation_target"])).hostname for projection in direct
            }
            supported_claims = {
                f"The AI cited {domain} for this query.".casefold()
                for domain in target_domains
                if domain
            }
            if claim.casefold() not in supported_claims:
                reasons.append(
                    "citation_polarity_unsupported"
                    if _NEGATED_CITATION.search(claim)
                    else "citation_target_unsupported"
                )
    query_context = " ".join(
        projection.get("query_text", "")
        for projection in projections
        if isinstance(projection.get("query_text"), str)
    )
    election = kind == "election" or _ELECTION_CLAIM.search(f"{claim} {query_context}") is not None
    blocked = bool(reasons)
    if election and not blocked:
        reasons.append("election_human_review_required")
    return StrategistClaimAssessment(
        allowed=not blocked,
        export_allowed=not blocked and not election,
        human_review_required=election,
        observation_ids=observation_ids,
        reason_codes=tuple(reasons),
    )


def validate_request(
    payload: Mapping[str, object],
    *,
    query_authority_reader: Callable[[str], object] | None = None,
) -> SeoVisibilityRequest:
    if not _is_issued_query_authority_reader(query_authority_reader):
        raise ValueError("query authority is required; authority reader is unissued")
    if not isinstance(payload, Mapping) or set(payload) != set(REQUEST_FIELDS):
        raise ValueError("request fields are invalid")
    if payload["adapter_contract_version"] != REQUEST_CONTRACT_VERSION:
        raise ValueError("request contract version is unsupported")
    markets = _ordered_strings(payload["markets"], "markets", minimum=1, maximum=3)
    if any(market not in APPROVED_MARKETS for market in markets):
        raise ValueError("markets contains an unsupported market")
    window_start, start = _timestamp(payload["window_start"], "window_start")
    window_end, end = _timestamp(payload["window_end"], "window_end")
    if end < start or end - start > timedelta(days=90) or end > datetime.now(UTC):
        raise ValueError("window must be closed and no longer than ninety days")
    queries = _ordered_strings(payload["queries"], "queries", minimum=1, maximum=25)
    decision = _exact_text(payload["decision"], "decision", maximum=1000)
    request_id = _exact_text(payload["request_id"], "request_id", maximum=128)
    client_scope_id = _exact_text(payload["client_scope_id"], "client_scope_id", maximum=128)
    run_id = _exact_text(payload["run_id"], "run_id", maximum=128)
    if any(
        _OPAQUE_IDENTIFIER.fullmatch(value) is None
        for value in (request_id, client_scope_id, run_id)
    ):
        raise ValueError("request identifiers must be opaque")
    brand_domains = _domains(payload["brand_domains"], "brand_domains", 5)
    competitor_domains = _domains(payload["competitor_domains"], "competitor_domains", 10)
    caller_strings = (
        request_id,
        client_scope_id,
        run_id,
        decision,
        *queries,
        *brand_domains,
        *competitor_domains,
    )
    if any(_NONPUBLIC_RESEARCH.search(value) for value in caller_strings):
        raise ValueError("queries must be approved public research terms")
    query_authority_receipt_id = _exact_text(
        payload["query_authority_receipt_id"],
        "query_authority_receipt_id",
        maximum=69,
    )
    if _QUERY_AUTHORITY_RECEIPT_ID.fullmatch(query_authority_receipt_id) is None:
        raise ValueError("query authority receipt identity is invalid")
    _validate_query_authority(
        receipt_id=query_authority_receipt_id,
        request_id=request_id,
        client_scope_id=client_scope_id,
        run_id=run_id,
        queries=queries,
        decision=decision,
        query_authority_reader=query_authority_reader,
    )
    return SeoVisibilityRequest(
        adapter_contract_version=REQUEST_CONTRACT_VERSION,
        request_id=request_id,
        client_scope_id=client_scope_id,
        run_id=run_id,
        markets=markets,
        window_start=window_start,
        window_end=window_end,
        queries=queries,
        brand_domains=brand_domains,
        competitor_domains=competitor_domains,
        decision=decision,
        query_authority_receipt_id=query_authority_receipt_id,
    )
