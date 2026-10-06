"""Build the full, bound SDK request for general-question planning."""

import copy
import hashlib
from collections import Counter

from src.analysis.open_intelligence.brain_contract import canonical_bytes, canonical_digest
from src.analysis.open_intelligence.canary_request import CanaryRequest
from src.analysis.open_intelligence.general_question_plan import (
    GENERAL_QUESTION_PLAN_SCHEMA,
    GENERAL_QUESTION_PLANNING_INSTRUCTION,
    GENERAL_QUESTION_PLANNING_INSTRUCTION_V1,
    build_question_planning_context,
    validate_question_plan,
)
from src.analysis.open_intelligence.general_question_policy import validate_question_policy
from src.analysis.open_intelligence.general_question_transport import question_model_configs

_PLAN_FIELDS = frozenset(GENERAL_QUESTION_PLAN_SCHEMA["properties"])
_PLAN_BINDING_FIELDS = frozenset(
    {"contract_version", "request_id", "request_digest", "intake_digest", "plan_digest"}
)
EVIDENCE_PURPOSES = ("support", "challenge", "context")
CHALLENGE_PLAN_VERSION = "general_question_plan_v3"
DISCOVERY_CHALLENGE_PLAN_VERSION = "general_question_plan_v4"
CHALLENGE_PLAN_VERSIONS = frozenset({CHALLENGE_PLAN_VERSION, DISCOVERY_CHALLENGE_PLAN_VERSION})
_PAIRED_INTENTS = frozenset({"explanation", "comparison"})
_DISCOVERY_PAIRED_INTENTS = _PAIRED_INTENTS | {"discovery"}
_CHALLENGE_ADAPTERS = frozenset({"challenge_v5", "discovery_challenge_v6"})
_SOURCE_WINDOW_ADAPTERS = frozenset({"source_window_v4", *_CHALLENGE_ADAPTERS})
_PLANNING_INSTRUCTIONS = {
    "v1": GENERAL_QUESTION_PLANNING_INSTRUCTION_V1,
    "continuity_v2": GENERAL_QUESTION_PLANNING_INSTRUCTION,
    "parent_context_v3": GENERAL_QUESTION_PLANNING_INSTRUCTION
    + " Authenticated parent_context is bounded context, not approved claims. Current explicit question scope overrides every inherited default. A current non-null selected_market differing from the parent's recorded selected_market supplies the next default; otherwise use the immediate parent's frame. The anchor never resets that frame. Select at most four parent_receipt_aliases, and only from the visible source_previews aliases, when the current question refers to those sources. These aliases still require current source admission. Never derive source IDs from prose or treat selected-record-count ancestry as cultural evidence.",
}
_PLANNING_INSTRUCTIONS["source_window_v4"] = (
    _PLANNING_INSTRUCTIONS["parent_context_v3"]
    + " The bound source_window_ceiling is an approved source profile ceiling, not proof of matching rows, complete daily coverage or current source authority. For a fresh undated question with no applicable prior dates, use the declared fourteen-day default_observation_window ending at that ceiling. Authenticated parent dates and relevant prior requested dates take precedence over this fallback. Preserve explicit current dates and structured requested_window exactly; never clamp or intersect them with the source ceiling. Resolve explicit relative calendar intervals against truthful request.as_of: past seven closed calendar days ends yesterday. Availability-relative requests such as latest available week may use the source ceiling instead. These are distinct requests. If source_window_ceiling and default_observation_window are null and the undated question has no applicable prior dates, return status needs_clarification with window null and clarification exactly: No supported default observation window is available. Please specify the dates to investigate. Explicit prose dates may still resolve normally against truthful as_of and remain subject to unchanged source admission."
)
_PLANNING_INSTRUCTIONS["challenge_v5"] = (
    _PLANNING_INSTRUCTIONS["source_window_v4"]
    + " Every requirement carries evidence_purpose: support, challenge or context. For a causal, comparative or challenged proposition emit paired requirements: at least one support requirement and at least one challenge requirement that searches the same markets and window for disagreement, alternative explanations and limiting evidence. A challenge reuses the authenticated original frame and only the exact parent source references visible in source_previews; question and history prose never authorize a source and never supply source identifiers. Keep the pair inside the same requirement and retrieval limits, reserve no extra model call, and never force a contradictory finding where none exists. A question resting on a national quantity that no admitted evidence establishes gets needs_clarification or bounded requirements that treat the quantity as unestablished."
)
# challenge_v5 stays byte identical for stored snapshots; discovery pairing is a new version.
_PLANNING_INSTRUCTIONS["discovery_challenge_v6"] = (
    _PLANNING_INSTRUCTIONS["challenge_v5"]
    + " A discovery question also carries a proposition: the themes the answer will report as what people were discussing. For a discovery question emit paired requirements too: at least one support requirement for those candidate themes and at least one challenge requirement that searches the same markets and window for evidence against them, such as competing topics, dissenting or contrary accounts and evidence that limits how widely a theme was discussed. Name no theme as established before retrieval, and keep the pair inside the same requirement limit."
)


def _challenge_schema(schema):
    item = schema["properties"]["requirements"]["items"]
    item["properties"]["evidence_purpose"] = {"type": "STRING", "enum": list(EVIDENCE_PURPOSES)}
    item["required"].append("evidence_purpose")
    return schema


def _planning_schema(adapter, parent_aliases):
    schema = copy.deepcopy(GENERAL_QUESTION_PLAN_SCHEMA)
    if parent_aliases is not None:
        aliases = list(parent_aliases)
        schema["properties"]["parent_receipt_aliases"] = {
            "type": "ARRAY",
            "items": {"type": "STRING", **({"enum": aliases} if aliases else {})},
            "maxItems": min(4, len(aliases)),
        }
        schema["required"].append("parent_receipt_aliases")
    if adapter in _CHALLENGE_ADAPTERS:
        _challenge_schema(schema)
    return schema


def planning_schema_digest(adapter, *, parent_aliases=None) -> str:
    """The exact schema digest of a planning adapter, parent-less or bound to the visible aliases."""
    if adapter not in _PLANNING_INSTRUCTIONS:
        raise ValueError("planning adapter is invalid")
    return canonical_digest(_planning_schema(adapter, parent_aliases))


def planning_adapter_id(
    system_instruction_digest, response_schema_digest=None, *, parent_aliases=None
) -> str:
    """Resolve a recorded planning prompt digest to its adapter id; a model alias never does.

    With a schema digest the pair binds exactly against the catalogue: the
    digest must equal the adapter's parent-less schema, or its schema bound to
    the supplied visible parent aliases, so a crossed pair refuses.
    """
    for name, instruction in _PLANNING_INSTRUCTIONS.items():
        if hashlib.sha256(instruction.encode("utf-8")).hexdigest() == system_instruction_digest:
            if response_schema_digest is not None and response_schema_digest != (
                planning_schema_digest(name, parent_aliases=parent_aliases)
            ):
                raise ValueError("planning binding is invalid")
            return name
    raise ValueError("planning binding is invalid")


def build_question_planning_request(
    request: dict,
    intake: dict,
    *,
    policy: dict,
    remaining_seconds: float,
    _adapter=None,
    parent_context=None,
) -> CanaryRequest:
    """Prepare bytes only; admission and durable call claims govern execution."""
    policy = validate_question_policy(policy)
    if _adapter is None:
        _adapter = (
            "source_window_v4"
            if intake.get("contract_version") == "general_question_intake_context_v3"
            else "parent_context_v3"
            if "parent_context_ref" in intake
            else "continuity_v2"
        )
    if _adapter not in _PLANNING_INSTRUCTIONS:
        raise ValueError("planning adapter is invalid")
    source_window = intake.get("contract_version") == "general_question_intake_context_v3"
    if source_window != (_adapter in _SOURCE_WINDOW_ADAPTERS) or (
        not source_window and ("parent_context_ref" in intake) != (_adapter == "parent_context_v3")
    ):
        raise ValueError("planning binding is invalid")
    instruction = _PLANNING_INSTRUCTIONS[_adapter]
    context = build_question_planning_context(request, intake, parent_context=parent_context)
    if context["request"]["policy_digest"] != policy["policy_digest"]:
        raise ValueError("request_invalid")
    stage = policy["stages"]["planning"]
    schema = _planning_schema(
        _adapter,
        [row["alias"] for row in context["parent_context"]["source_previews"]]
        if "parent_context_ref" in intake
        else None,
    )
    count_config, generation_config = question_model_configs(
        system_instruction=instruction,
        response_schema=schema,
        thinking_level=stage["thinking_level"],
        max_output_tokens=stage["output_tokens"],
        remaining_seconds=remaining_seconds,
    )
    return CanaryRequest(
        model=policy["model"],
        contents=canonical_bytes(context).decode("utf-8"),
        count_tokens_config=count_config,
        generation_config=generation_config,
        input_digest=canonical_digest(context),
        system_instruction_digest=hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
        response_schema_digest=canonical_digest(schema),
    )


def planning_request_for_call(call, *, request, intake, policy, parent_context=None):
    adapter = planning_adapter_id(call.get("system_instruction_digest"))
    sdk_request = build_question_planning_request(
        request,
        intake,
        policy=policy,
        remaining_seconds=60,
        _adapter=adapter,
        parent_context=parent_context,
    )
    if any(
        call.get(field) != getattr(sdk_request, field)
        for field in (
            "input_digest",
            "system_instruction_digest",
            "response_schema_digest",
            "model",
        )
    ) or (
        call.get("thinking_level")
        != sdk_request.generation_config.thinking_config.thinking_level.value
        or call.get("max_output_tokens") != sdk_request.generation_config.max_output_tokens
    ):
        raise ValueError("planning binding is invalid")
    return sdk_request


def validate_challenge_plan(draft, *, request, intake, parent_context=None) -> dict:
    """Validate a challenge_v5 draft into a general_question_plan_v3 plan.

    Every requirement carries evidence_purpose. The older validator keeps its
    exact key set and checks everything else; the pairing rule lives here. A
    causal or comparative plan, or any plan carrying a challenge, needs at
    least one support and at least one challenge requirement inside the
    unchanged requirement limit. Markets, window and parent source references
    stay plan level, so a challenge searches the authenticated frame.
    """
    return _validate_purposed_plan(
        draft,
        request=request,
        intake=intake,
        parent_context=parent_context,
        paired_intents=_PAIRED_INTENTS,
        contract_version=CHALLENGE_PLAN_VERSION,
    )


def validate_discovery_challenge_plan(draft, *, request, intake, parent_context=None) -> dict:
    """Validate a discovery_challenge_v6 draft into a general_question_plan_v4 plan.

    The challenge_v5 rules apply unchanged, and a ready discovery plan also
    needs a support and a challenge requirement, so a discovery answer can
    report a completed challenge search. The plan names its own version, so a
    stored plan is rechecked under this rule and never under the looser one.
    """
    return _validate_purposed_plan(
        draft,
        request=request,
        intake=intake,
        parent_context=parent_context,
        paired_intents=_DISCOVERY_PAIRED_INTENTS,
        contract_version=DISCOVERY_CHALLENGE_PLAN_VERSION,
    )


_PLAN_VALIDATORS = {
    CHALLENGE_PLAN_VERSION: validate_challenge_plan,
    DISCOVERY_CHALLENGE_PLAN_VERSION: validate_discovery_challenge_plan,
}
_ADAPTER_VALIDATORS = {
    "challenge_v5": validate_challenge_plan,
    "discovery_challenge_v6": validate_discovery_challenge_plan,
}


def planned_draft_validator(adapter):
    """The validator for a draft planned under ``adapter``, so the plan version follows it."""
    if adapter not in _PLANNING_INSTRUCTIONS:
        raise ValueError("planning adapter is invalid")
    return _ADAPTER_VALIDATORS.get(adapter, validate_question_plan)


def _validate_purposed_plan(
    draft, *, request, intake, parent_context, paired_intents, contract_version
) -> dict:
    if not isinstance(draft, dict) or not isinstance(draft.get("requirements"), list):
        raise ValueError("semantic_output_invalid")
    base = copy.deepcopy(draft)
    purposes = []
    for requirement in base["requirements"]:
        if not isinstance(requirement, dict) or "evidence_purpose" not in requirement:
            raise ValueError("semantic_output_invalid")
        purpose = requirement.pop("evidence_purpose")
        if purpose not in EVIDENCE_PURPOSES:
            raise ValueError("semantic_output_invalid")
        purposes.append(purpose)
    plan = validate_question_plan(
        base, request=request, intake=intake, parent_context=parent_context
    )
    for requirement, purpose in zip(plan["requirements"], purposes, strict=True):
        requirement["evidence_purpose"] = purpose
    counts = Counter(purposes)
    paired = plan["intent"] in paired_intents or counts["challenge"] > 0
    if plan["status"] == "ready" and paired and not (counts["support"] and counts["challenge"]):
        raise ValueError("semantic_output_invalid")
    plan["contract_version"] = contract_version
    plan.pop("plan_digest")
    plan["plan_digest"] = canonical_digest(plan)
    return plan


def validate_stored_question_plan(value: object, *, request: dict, intake: dict) -> dict:
    fields = _PLAN_FIELDS | (
        {"parent_receipt_aliases"} if "parent_context_ref" in intake else set()
    )
    if not isinstance(value, dict) or set(value) != fields | _PLAN_BINDING_FIELDS:
        raise ValueError("semantic_output_invalid")
    version = value["contract_version"]
    validator = (
        _PLAN_VALIDATORS.get(version, validate_question_plan)
        if type(version) is str
        else validate_question_plan
    )
    rebuilt = validator({key: value[key] for key in fields}, request=request, intake=intake)
    if value != rebuilt:
        raise ValueError("semantic_output_invalid")
    return rebuilt
