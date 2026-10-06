"""Pure Evidence Room investigation-plan contract boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
from types import MappingProxyType

CONTRACT_VERSION = "2.1.0"
STORAGE_VERSION = "investigation_record_v1"
APPROVED_MARKETS = frozenset({"za", "ng", "ke"})
OUTPUT_MODES = frozenset(
    {"internal_working_paper", "client_brief", "evidence_appendix", "slide_outline"}
)
# The client lens a frame may name: exactly the binding envelope the question
# path carries. None is general 42, which every stored frame already spells by
# having no client_lens key, so general 42 ids and payloads are unchanged.
LENS_BINDING_VERSION = "client_lens_binding_v1"
LENS_BINDING_FIELDS = ("lens_binding_version", "client_lens_id", "configuration_digest")
_LENS_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
FRAME_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
    "decision_question",
    "time_horizon_days",
    "brand_context",
    "known_assumptions",
    "change_my_mind_if",
    "research_role_id",
    "research_role_version",
    "output_mode",
)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _strings(value: object, field: str, *, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{field} must be a sequence")
    items = tuple(_text(item, field) for item in value)
    if nonempty and not items:
        raise ValueError(f"{field} must be nonempty")
    if len(items) != len(set(items)):
        raise ValueError(f"{field} must be unique")
    return items


def _positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _client_lens(value: object) -> Mapping[str, str] | None:
    """The frame's lens: None for general 42, else one exact binding envelope.

    Only the shape is checked. Whether the lens is authorized is decided where
    an investigation is created, so a stored frame stays readable after the
    registry disables or repins its lens.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != set(LENS_BINDING_FIELDS):
        raise ValueError("client lens is invalid")
    lens_id = value["client_lens_id"]
    digest = value["configuration_digest"]
    if (
        value["lens_binding_version"] != LENS_BINDING_VERSION
        or type(lens_id) is not str
        or not lens_id
        or lens_id != lens_id.strip()
        or type(digest) is not str
        or _LENS_DIGEST.fullmatch(digest) is None
    ):
        raise ValueError("client lens is invalid")
    return MappingProxyType({field: value[field] for field in LENS_BINDING_FIELDS})


def _utc(value: object, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValueError(f"{field} must be UTC")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class InvestigationFrame:
    client_scope_id: str
    market_scope: tuple[str, ...]
    brand_config_id: str | None
    audience_lens_ids: tuple[str, ...]
    theme_id: str | None
    run_id: str
    contract_version: str
    decision_question: str
    time_horizon_days: int
    brand_context: str | None
    known_assumptions: tuple[str, ...]
    change_my_mind_if: tuple[str, ...]
    research_role_id: str | None
    research_role_version: str | None
    output_mode: str
    client_lens: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "client_scope_id", _text(self.client_scope_id, "client scope id"))
        markets = _strings(self.market_scope, "market scope", nonempty=True)
        if any(market != market.lower() or market not in APPROVED_MARKETS for market in markets):
            raise ValueError("market scope is invalid")
        object.__setattr__(self, "market_scope", markets)
        object.__setattr__(
            self, "brand_config_id", _optional_text(self.brand_config_id, "brand config id")
        )
        object.__setattr__(
            self, "audience_lens_ids", _strings(self.audience_lens_ids, "audience lens ids")
        )
        object.__setattr__(self, "theme_id", _optional_text(self.theme_id, "theme id"))
        object.__setattr__(self, "run_id", _text(self.run_id, "run id"))
        if self.contract_version != CONTRACT_VERSION:
            raise ValueError("contract_version_unsupported")
        object.__setattr__(
            self, "decision_question", _text(self.decision_question, "decision question")
        )
        object.__setattr__(
            self, "time_horizon_days", _positive_integer(self.time_horizon_days, "time horizon days")
        )
        object.__setattr__(
            self, "brand_context", _optional_text(self.brand_context, "brand context")
        )
        object.__setattr__(
            self, "known_assumptions", _strings(self.known_assumptions, "known assumptions")
        )
        object.__setattr__(
            self, "change_my_mind_if", _strings(self.change_my_mind_if, "change my mind if")
        )
        role_id = _optional_text(self.research_role_id, "research role id")
        role_version = _optional_text(self.research_role_version, "research role version")
        if (role_id is None) != (role_version is None):
            raise ValueError("research role id and version must both be null or set")
        object.__setattr__(self, "research_role_id", role_id)
        object.__setattr__(self, "research_role_version", role_version)
        if self.output_mode not in OUTPUT_MODES:
            raise ValueError("output mode is unsupported")
        object.__setattr__(self, "client_lens", _client_lens(self.client_lens))


@dataclass(frozen=True, slots=True)
class ResearchPlanInputs:
    questions: tuple[str, ...]
    required_source_families: tuple[str, ...]
    socialcrawl_lanes: tuple[str, ...]
    historical_window_days: int
    credit_ceiling: int
    token_budget: Mapping[str, int]
    stopping_conditions: tuple[str, ...]
    known_gaps: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "questions", _strings(self.questions, "questions"))
        object.__setattr__(
            self,
            "required_source_families",
            _strings(self.required_source_families, "required source families"),
        )
        object.__setattr__(
            self, "socialcrawl_lanes", _strings(self.socialcrawl_lanes, "SocialCrawl lanes")
        )
        object.__setattr__(
            self,
            "historical_window_days",
            _positive_integer(self.historical_window_days, "historical window days"),
        )
        object.__setattr__(
            self, "credit_ceiling", _nonnegative_integer(self.credit_ceiling, "credit ceiling")
        )
        if not isinstance(self.token_budget, Mapping):
            raise ValueError("token budget must be an object")
        budget: dict[str, int] = {}
        for key, value in self.token_budget.items():
            name = _text(key, "token budget key")
            budget[name] = _nonnegative_integer(value, f"token budget {name}")
        object.__setattr__(self, "token_budget", MappingProxyType(dict(budget)))
        object.__setattr__(
            self,
            "stopping_conditions",
            _strings(self.stopping_conditions, "stopping conditions"),
        )
        object.__setattr__(self, "known_gaps", _strings(self.known_gaps, "known gaps"))


def investigation_id_for_frame(frame: InvestigationFrame) -> str:
    if not isinstance(frame, InvestigationFrame):
        raise ValueError("investigation frame is invalid")
    # The lens joins the hashed payload only when one is named, so a general 42
    # frame hashes exactly the fifteen values it always has and keeps its id.
    payload = {
        field.name: getattr(frame, field.name)
        for field in fields(InvestigationFrame)
        if field.name != "client_lens"
    }
    if frame.client_lens is not None:
        payload["client_lens"] = dict(frame.client_lens)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "inv_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def default_plan_for_frame(frame: InvestigationFrame) -> ResearchPlanInputs:
    if not isinstance(frame, InvestigationFrame):
        raise ValueError("investigation frame is invalid")
    return ResearchPlanInputs(
        questions=(
            frame.decision_question,
            "What changed inside the declared market and time horizon?",
            "What evidence supports and opposes acting now?",
            "Which materially different responses should be compared?",
        ),
        required_source_families=("social", "search", "news", "history"),
        socialcrawl_lanes=(),
        historical_window_days=365,
        credit_ceiling=0,
        token_budget={},
        stopping_conditions=(
            "Stop if a factual claim has no resolvable receipt.",
            "Stop if a blocking contradiction remains unresolved.",
            "Stop before any paid source call without an approved funding gate.",
        ),
        known_gaps=("optional_socialcrawl_funding_unverified",),
    )


def investigation_frame_payload(frame: InvestigationFrame) -> dict[str, object]:
    if not isinstance(frame, InvestigationFrame):
        raise ValueError("investigation frame is invalid")
    payload: dict[str, object] = {
        "client_scope_id": frame.client_scope_id,
        "market_scope": list(frame.market_scope),
        "brand_config_id": frame.brand_config_id,
        "audience_lens_ids": list(frame.audience_lens_ids),
        "theme_id": frame.theme_id,
        "run_id": frame.run_id,
        "contract_version": frame.contract_version,
        "decision_question": frame.decision_question,
        "time_horizon_days": frame.time_horizon_days,
        "brand_context": frame.brand_context,
        "known_assumptions": list(frame.known_assumptions),
        "change_my_mind_if": list(frame.change_my_mind_if),
        "research_role_id": frame.research_role_id,
        "research_role_version": frame.research_role_version,
        "output_mode": frame.output_mode,
    }
    if frame.client_lens is not None:
        payload["client_lens"] = dict(frame.client_lens)
    return payload


def investigation_frame_from_payload(payload: object) -> InvestigationFrame:
    """A stored frame: the fifteen values, or those plus one named lens envelope.

    An explicit null lens is refused, so general 42 has exactly one spelling,
    the one every stored object already has.
    """
    if not isinstance(payload, Mapping) or set(payload) not in (
        set(FRAME_FIELDS),
        {*FRAME_FIELDS, "client_lens"},
    ):
        raise ValueError("stored investigation frame is invalid")
    if "client_lens" in payload and payload["client_lens"] is None:
        raise ValueError("stored investigation frame is invalid")
    try:
        return InvestigationFrame(
            client_scope_id=payload["client_scope_id"],
            market_scope=tuple(payload["market_scope"]),
            brand_config_id=payload["brand_config_id"],
            audience_lens_ids=tuple(payload["audience_lens_ids"]),
            theme_id=payload["theme_id"],
            run_id=payload["run_id"],
            contract_version=payload["contract_version"],
            decision_question=payload["decision_question"],
            time_horizon_days=payload["time_horizon_days"],
            brand_context=payload["brand_context"],
            known_assumptions=tuple(payload["known_assumptions"]),
            change_my_mind_if=tuple(payload["change_my_mind_if"]),
            research_role_id=payload["research_role_id"],
            research_role_version=payload["research_role_version"],
            output_mode=payload["output_mode"],
            client_lens=payload.get("client_lens"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("stored investigation frame is invalid") from error


RESPONSE_FIELDS = (
    "investigation_id",
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
    "run_id",
    "contract_version",
    "research_role_id",
    "research_role_version",
    "status",
    "research_plan",
    "created_at",
    "updated_at",
)


def _validate_plan_ready_response(
    response: object, frame: InvestigationFrame
) -> dict[str, object]:
    response_fields = set(RESPONSE_FIELDS)
    if frame.client_lens is not None:
        response_fields.add("client_lens")
    if not isinstance(response, Mapping) or set(response) != response_fields:
        raise ValueError("stored investigation response is invalid")
    if response["investigation_id"] != investigation_id_for_frame(frame):
        raise ValueError("stored investigation identity mismatch")
    if frame.client_lens is not None and response["client_lens"] != dict(frame.client_lens):
        raise ValueError("stored investigation scope mismatch")
    expected_scope = investigation_frame_payload(frame)
    for field in (
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "contract_version",
        "research_role_id",
        "research_role_version",
    ):
        if response[field] != expected_scope[field]:
            raise ValueError("stored investigation scope mismatch")
    if response["status"] != "plan_ready":
        raise ValueError("stored investigation status is invalid")
    timestamp = response["created_at"]
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ValueError("stored investigation timestamp is invalid")
    try:
        parsed_timestamp = datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("stored investigation timestamp is invalid") from error
    if parsed_timestamp.utcoffset() != timedelta(0) or response["updated_at"] != timestamp:
        raise ValueError("stored investigation timestamp is invalid")
    plan = response["research_plan"]
    if not isinstance(plan, Mapping) or set(plan) != {
        "questions",
        "required_source_families",
        "socialcrawl_lanes",
        "historical_window_days",
        "credit_ceiling",
        "token_budget",
        "stopping_conditions",
        "known_gaps",
    }:
        raise ValueError("stored research plan is invalid")
    for field in (
        "questions",
        "required_source_families",
        "socialcrawl_lanes",
        "stopping_conditions",
        "known_gaps",
    ):
        if not isinstance(plan[field], list):
            raise ValueError("stored research plan is invalid")
    if not isinstance(plan["token_budget"], dict):
        raise ValueError("stored research plan is invalid")
    ResearchPlanInputs(
        questions=tuple(plan["questions"]),
        required_source_families=tuple(plan["required_source_families"]),
        socialcrawl_lanes=tuple(plan["socialcrawl_lanes"]),
        historical_window_days=plan["historical_window_days"],
        credit_ceiling=plan["credit_ceiling"],
        token_budget=plan["token_budget"],
        stopping_conditions=tuple(plan["stopping_conditions"]),
        known_gaps=tuple(plan["known_gaps"]),
    )
    return dict(response)


def build_investigation_storage_record(
    frame: InvestigationFrame, response: object
) -> dict[str, object]:
    return {
        "storage_version": STORAGE_VERSION,
        "frame": investigation_frame_payload(frame),
        "response": _validate_plan_ready_response(response, frame),
    }


def validate_investigation_storage_record(
    record: object,
) -> tuple[InvestigationFrame, dict[str, object]]:
    if not isinstance(record, Mapping) or set(record) != {
        "storage_version",
        "frame",
        "response",
    }:
        raise ValueError("stored investigation record is invalid")
    if record["storage_version"] != STORAGE_VERSION:
        raise ValueError("contract version is invalid")
    frame = investigation_frame_from_payload(record["frame"])
    response = _validate_plan_ready_response(record["response"], frame)
    return frame, response


def build_plan_ready(
    *,
    frame: InvestigationFrame,
    plan: ResearchPlanInputs,
    investigation_id: str,
    created_at: datetime,
    active_role_versions: Mapping[str, str],
) -> dict[str, object]:
    if not isinstance(frame, InvestigationFrame):
        raise ValueError("investigation frame is invalid")
    if not isinstance(plan, ResearchPlanInputs):
        raise ValueError("research plan inputs are invalid")
    if not isinstance(active_role_versions, Mapping):
        raise ValueError("active role versions are invalid")
    if frame.research_role_id is not None and (
        active_role_versions.get(frame.research_role_id) != frame.research_role_version
    ):
        raise ValueError("research role version is not active")
    investigation_id = _text(investigation_id, "investigation id")
    if not investigation_id.startswith("inv_"):
        raise ValueError("investigation id is invalid")
    created_at = _utc(created_at, "created_at")
    timestamp = created_at.isoformat().replace("+00:00", "Z")
    response: dict[str, object] = {
        "investigation_id": investigation_id,
        "client_scope_id": frame.client_scope_id,
        "market_scope": list(frame.market_scope),
        "brand_config_id": frame.brand_config_id,
        "audience_lens_ids": list(frame.audience_lens_ids),
        "theme_id": frame.theme_id,
        "run_id": frame.run_id,
        "contract_version": frame.contract_version,
        "research_role_id": frame.research_role_id,
        "research_role_version": frame.research_role_version,
        "status": "plan_ready",
        "research_plan": {
            "questions": list(plan.questions),
            "required_source_families": list(plan.required_source_families),
            "socialcrawl_lanes": list(plan.socialcrawl_lanes),
            "historical_window_days": plan.historical_window_days,
            "credit_ceiling": plan.credit_ceiling,
            "token_budget": dict(plan.token_budget),
            "stopping_conditions": list(plan.stopping_conditions),
            "known_gaps": list(plan.known_gaps),
        },
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    # The response names the lens only when the frame does, as the frame payload does.
    if frame.client_lens is not None:
        response["client_lens"] = dict(frame.client_lens)
    return response
