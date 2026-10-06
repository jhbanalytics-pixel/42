from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlparse

CONTRACT_VERSION = "2.0.0"
FROZEN_AT = "2026-08-25T00:00:00Z"
DEFAULT_FIXTURE_DIRECTORY = (
    Path(__file__).resolve().parents[2] / "tests/fixtures/open_intelligence/v2"
)
LIVE_INTELLIGENCE_JOURNEY_IDS = (
    "journey_crisis_affluent_za_v1",
    "journey_brand_south_africa_narratives_v1",
    "journey_election_brand_role_v1",
)
FIXTURE_KINDS = {
    "dynamic_signal_ready": "dynamic_signal",
    "evidence_ready": "evidence_readiness",
    "evidence_thin": "evidence_readiness",
    "evidence_contradictory": "evidence_readiness",
    "evidence_unchecked": "evidence_readiness",
    "lineage_merge": "lineage",
    "lineage_split": "lineage",
    "source_lab_all_statuses": "source_lab",
    "evidence_plan_election": "evidence_plan",
    "cited_answer_election": "cited_answer",
    "pulse_watched_signal_line": "pulse_watched_signal_line",
    "golden_01_emerging_without_keyword": "golden_task",
    "golden_02_why_moving": "golden_task",
    "golden_03_cross_market_difference": "golden_task",
    "golden_04_carriers": "golden_task",
    "golden_05_history": "golden_task",
    "golden_06_brand_role": "golden_task",
    "golden_07_audience_lens": "golden_task",
    "golden_08_source_agreement": "golden_task",
    "golden_09_source_gap": "golden_task",
    "golden_10_custom": "golden_task",
    "golden_11_election_brand_role": "golden_task",
}
EXPECTED_INTENTS = {
    "golden_01_emerging_without_keyword": "landscape",
    "golden_02_why_moving": "explanation",
    "golden_03_cross_market_difference": "comparison",
    "golden_04_carriers": "creator",
    "golden_05_history": "historical_analogue",
    "golden_06_brand_role": "brand_role",
    "golden_07_audience_lens": "audience",
    "golden_08_source_agreement": "source_coverage",
    "golden_09_source_gap": "source_coverage",
    "golden_10_custom": "custom",
    "golden_11_election_brand_role": "brand_role",
}
TASK_SEMANTICS = {
    "golden_01_emerging_without_keyword": (
        "unprompted phrase clusters",
        "unprompted fixture repair circles",
    ),
    "golden_02_why_moving": ("baseline acceleration", "accelerated after a fixture parts exchange"),
    "golden_03_cross_market_difference": ("three fixture markets", "za emphasizes repair steps"),
    "golden_04_carriers": ("first carrier timestamp", "fixture maker one carried the move first"),
    "golden_05_history": ("analogue one", "two of three observed sequences"),
    "golden_06_brand_role": ("occupied tutorial space", "the tutorial space is occupied"),
    "golden_07_audience_lens": (
        "inferred practical lens",
        "the inferred fixture lens fits practical instruction",
    ),
    "golden_08_source_agreement": ("agreement disagreement and absence", "forum and video agree"),
    "golden_09_source_gap": (
        "dark audio surface consequence",
        "the dark audio surface prevents sound-led comparison",
    ),
    "golden_10_custom": (
        "rotating repair stations",
        "run a fixture workshop with rotating repair stations",
    ),
    "golden_11_election_brand_role": (
        "fixture election coverage register",
        "24 synthetic posts in the closed fixture window",
    ),
}
PLAN_EXPECTATIONS = {
    "golden_01_emerging_without_keyword": (
        "Select the unprompted move to investigate.",
        {"unprompted discovery evidence", "two independent source families"},
        False,
        0,
    ),
    "golden_02_why_moving": (
        "Choose which observed driver to test in a brief.",
        {"dated movement evidence", "source-family baseline comparison"},
        True,
        30,
    ),
    "golden_03_cross_market_difference": (
        "Choose one market adaptation for the synthetic move.",
        {"one receipt per fixture market", "cross-market comparable window"},
        False,
        0,
    ),
    "golden_04_carriers": (
        "Choose which carrier sequence to investigate.",
        {"first carrier timestamps", "spread sequence receipts"},
        False,
        0,
    ),
    "golden_05_history": (
        "Decide whether observed recurrence warrants monitoring.",
        {"three synthetic analogues", "current difference", "observed follow-on outcomes"},
        True,
        730,
    ),
    "golden_06_brand_role": (
        "Choose one credible unoccupied fixture brand role.",
        {"occupied space evidence", "unmet space evidence"},
        False,
        0,
    ),
    "golden_07_audience_lens": (
        "Choose whether to apply the inferred fixture lens.",
        {"lens basis source window and confidence", "cross-source fit evidence"},
        False,
        0,
    ),
    "golden_08_source_agreement": (
        "Decide whether source disagreement blocks action.",
        {"agreement evidence", "disagreement evidence", "absence register"},
        False,
        0,
    ),
    "golden_09_source_gap": (
        "Decide whether to fund a synthetic source pilot.",
        {"dark surface status", "decision consequence"},
        False,
        0,
    ),
    "golden_10_custom": (
        "Choose one unusual synthetic workshop format.",
        {"workshop request evidence", "demonstration handover evidence"},
        False,
        0,
    ),
    "golden_11_election_brand_role": (
        "Choose a neutral election information response for human review.",
        {
            "local conversation themes",
            "associated factors without causation",
            "brand role and safety evidence",
            "human review before export",
        },
        False,
        0,
    ),
}
SECTION_SUPPORT = {
    "golden_01_emerging_without_keyword": {
        "finding": (
            {"unprompted fixture repair circles"},
            {"[E1]": "unprompted phrase clusters", "[E2]": "unprompted repair circle"},
        )
    },
    "golden_02_why_moving": {
        "finding": (
            {"accelerated after a fixture parts exchange"},
            {"[E1]": "baseline acceleration", "[E2]": "dated the parts exchange"},
        )
    },
    "golden_03_cross_market_difference": {
        "finding": (
            {
                "za emphasizes repair steps",
                "ng emphasizes group swap events",
                "ke emphasizes price comparison",
            },
            {"[E1]": "za emphasizes repair steps", "[E2]": "ng evidence", "[E3]": "ke evidence"},
        )
    },
    "golden_04_carriers": {
        "finding": (
            {"fixture maker one carried the move first", "fixture spreader two"},
            {"[E1]": "first carrier timestamp", "[E2]": "spread record"},
        )
    },
    "golden_05_history": {
        "finding": (
            {"two of three observed sequences", "not a forecast", "current move differs"},
            {
                "[E1]": "analogue one",
                "[E2]": "analogue two",
                "[E3]": "analogue three",
                "[E4]": "current move",
            },
        )
    },
    "golden_06_brand_role": {
        "finding": (
            {"tutorial space is occupied", "unmet parts directory"},
            {"[E1]": "occupied tutorial space", "[E2]": "unmet parts directory"},
        )
    },
    "golden_07_audience_lens": {
        "finding": (
            {"inferred fixture lens fits practical instruction"},
            {"[E1]": "inferred practical lens", "[E2]": "practical instruction cues"},
        )
    },
    "golden_08_source_agreement": {
        "finding": (
            {"forum and video agree", "news disagrees", "audio is absent"},
            {
                "[E1]": "forum rising",
                "[E2]": "agrees",
                "[E3]": "declines",
                "[E4]": "audio family as absent",
            },
        )
    },
    "golden_09_source_gap": {
        "finding": (
            {"dark audio surface prevents sound-led comparison"},
            {"[E1]": "dark audio surface consequence", "[E2]": "cannot substitute"},
        )
    },
    "golden_10_custom": {
        "finding": (
            {"rotating repair stations", "short demonstration handovers"},
            {"[E1]": "rotating repair stations", "[E2]": "short station handovers"},
        )
    },
    "golden_11_election_brand_role": {
        "conversation_themes": (
            {"24 synthetic posts", "18 to 25 august 2026"},
            {"[E1]": "practical venue access", "[E2]": "expressed willingness"},
        ),
        "associated_factors": (
            {"associate clear venue information", "without establishing cause"},
            {"[E1]": "practical venue access", "[E2]": "expressed willingness"},
        ),
        "audience_lenses": (
            {"inferred fixture lens", "0.61 confidence"},
            {"[E2]": "expressed willingness"},
        ),
        "brand_roles": (
            {
                "current fixture brand behavior is absent",
                "unmet neutral access space",
                "brand safety risk",
            },
            {
                "[E1]": "practical venue access",
                "[E2]": "clear logistics",
                "[E3]": "partisan endorsement",
                "[E5]": "no observed current fixture brand behavior",
            },
        ),
        "safety_flags": (
            {"misinformation", "no qualifying polarization"},
            {"[E4]": "no qualifying polarization", "[E5]": "misinformation flag"},
        ),
        "evidence_readiness": (
            {"ready", "two qualifying"},
            {"[E1]": "practical venue access", "[E2]": "clear logistics"},
        ),
        "recommendations": (
            {"provide neutral voting logistics", "avoid partisan endorsement"},
            {
                "[E1]": "practical venue access",
                "[E2]": "clear logistics",
                "[E3]": "partisan endorsement",
            },
        ),
    },
}
GOLDEN_PAYLOAD_ROOTS = {
    "golden_01_emerging_without_keyword": "cdbb8ff277e1326d65082769f4b2cf497c81d3af9307f4313db592d54de35ebe",
    "golden_02_why_moving": "897acee0f5c637504640fb46172af5cc9e81d9e0562486a5d15a0d32fe6df6c5",
    "golden_03_cross_market_difference": "f74ff0dbe804ee560758aba08fbda3261b0274b41027d45e1e58116bb413918d",
    "golden_04_carriers": "7660cc845a6f3d51967f7dcc488bcb66134fad17506a36dc4bfcab5930804fd9",
    "golden_05_history": "147e54b3aebeaf0984b3c6a4c73928d36d0f842dc235f2fe1167c51dc53a701c",
    "golden_06_brand_role": "0b9270c83ee636b1bd83e48b21907f20291174de97d801e2ba78c77004bceb88",
    "golden_07_audience_lens": "deaeb1927e6c0f36d35be3690915e4be3a582f7e816e09a3f805175e852cb822",
    "golden_08_source_agreement": "8bf21edd9affeadc7f74721c093b0f08ab488c94effcb9bead698a3682462bb9",
    "golden_09_source_gap": "f3b24ab64616f426393f42df61747153b08243ab8118add9579893dd15521461",
    "golden_10_custom": "b14db5d66880a7e28d27094b5e51e0f9b424819e04f721159ff7691143a7f749",
    "golden_11_election_brand_role": "5d0455536bcd71c94a08250576d005e734fd7103684816ccce5c9f189173510d",
}
CITED_ELECTION_PAYLOAD_ROOT = "d75a96f12d51c376f217fa5b8a196254330b04e8019d98eaa7d745dd289067d6"
CUSTOM_QUESTION = "How should an unusual synthetic repair workshop be structured?"
EVIDENCE_REVIEW_ROOTS = {
    "cited_answer_election": "35210088de4b86fd54837eb6d5375e31ca883489eac0cce627de84f0511139c9",
    "dynamic_signal_ready": "3b6d63fef5567b0ae1973e5d08bb81fc8cd36d2a27d2fe35764838d16c00d9c6",
    "evidence_contradictory": "6f8f7bea0eef82354a3e6d1626b16488992fdc6a39414315224b292b2805671c",
    "evidence_ready": "830a64e35afcd554be9b0f1d29b15b84107f2c0c02c201ea307557196583e782",
    "evidence_thin": "bb1175cb482cdc6548f37e3b0041c251927675d565354698621387c2d1affe41",
    "evidence_unchecked": "37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570",
    "golden_01_emerging_without_keyword": "644652fd37450c7549452d28c0729ecda4c536c2851ade7750d5a2716835fafd",
    "golden_02_why_moving": "af1e320a64de8853b7aa8ae23ad6ffc5457627d761a2f7cc84c519364de0e817",
    "golden_03_cross_market_difference": "d234194b6c7add947a53527e053e7e8941371a01166f7990f87aae040cb7c61c",
    "golden_04_carriers": "741e0fb87bc76822285b9e67df332b416a133b16fab0aab364d0e8cfff636516",
    "golden_05_history": "e06200514f86a51c1216198b3f08121aa6d18ac9c9745526c5c3fdf92e181a79",
    "golden_06_brand_role": "012f4957789e32b82e10a8e2da5191897fd3225f02c9e4dcbb7938a3949b60d1",
    "golden_07_audience_lens": "f1811760be7eca116880c40f207594e8fef4abe9e67a5c4e014220bbb983663f",
    "golden_08_source_agreement": "d8397258c102e20c5f555989e612d1df851f14a3f73aca0e59b1e37e1bd09aae",
    "golden_09_source_gap": "6f648c29907eb372dd1d69ecbe5bb934d95929b792d733739f798fb588f41cf2",
    "golden_10_custom": "ccf5eefcaf0c1d961b702a52f1ee9dccdc42638eb1f2d472632a265010ab7a3f",
    "golden_11_election_brand_role": "35210088de4b86fd54837eb6d5375e31ca883489eac0cce627de84f0511139c9",
}
SCOPE_FIELDS = {
    "contract_version",
    "run_id",
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
}
ENVELOPE_FIELDS = {
    "fixture_id",
    "fixture_kind",
    "contract_version",
    "sanitized",
    "frozen_at",
    "payload",
}
MANIFEST_FIELDS = {"contract_version", "fixture_count", "fixtures", "manifest_sha256"}
EVIDENCE_FIELDS = {
    "evidence_id",
    "signal_id",
    "row_id",
    "source_family",
    "platform",
    "source_label",
    "author_label",
    "excerpt",
    "metric_label",
    "url",
    "published_at",
    "claim_role",
    "direction",
    "geo_confidence",
    "availability",
}
ANSWER_EVIDENCE_FIELDS = EVIDENCE_FIELDS | {"citation_label"}
PLAN_FIELDS = SCOPE_FIELDS | {
    "plan_id",
    "intent",
    "decision",
    "markets",
    "window",
    "audience_lenses",
    "source_families",
    "historical_comparison",
    "evidence_requirements",
    "output_form",
    "evidence_state",
}
REQUEST_FIELDS = SCOPE_FIELDS | {"question", "decision", "window", "output_form"}
ANSWER_FIELDS = {"title", "scope", "sections"}
SECTION_FIELDS = {"section_id", "heading", "body", "evidence_ids"}
LENS_FIELDS = {"lens_id", "basis", "source", "window", "confidence", "claim"}
BUDGET_FIELDS = {
    "input_ceiling",
    "output_ceiling",
    "input_used",
    "output_used",
    "remaining_max_output_tokens",
    "exhausted",
    "metering_status",
}
GOLDEN_FIELDS = {
    "request",
    "evidence_plan",
    "answer",
    "evidence",
    "evidence_state",
    "limitations",
    "missing_work",
    "budget",
    "expected_intent",
    "contradictions",
    "human_review_required",
    "audience_lenses",
}
DYNAMIC_PAYLOAD_FIELDS = SCOPE_FIELDS | {"signal", "evidence"}
DYNAMIC_SIGNAL_FIELDS = {
    "signal_id",
    "signal_date",
    "market",
    "label",
    "why_now",
    "possible_response",
    "novelty_score",
    "velocity_score",
    "breadth_score",
    "independence_score",
    "historical_similarity",
    "geo_confidence",
    "evidence_state",
    "topic_tags",
    "evidence_ids",
}
READINESS_FIELDS = {
    "signal_id",
    "evidence_state",
    "qualifying_family_count",
    "direction_by_family",
    "evidence",
}
LINEAGE_EDGE_FIELDS = SCOPE_FIELDS | {
    "signal_date",
    "market",
    "from_signal_id",
    "to_signal_id",
    "relation",
    "overlap_score",
    "created_at",
}
SOURCE_FIELDS = {
    "vendor",
    "http_method",
    "route_path",
    "endpoint_id",
    "route_role",
    "source_family",
    "market",
    "status",
    "calls",
    "credits",
    "rows",
    "integrity",
    "geo_precision",
    "unique_lift",
    "last_success_at",
    "downstream_consumers",
    "official_capability",
    "official_parameters",
    "official_price_components",
    "blocking_reason",
    "kill_test_result",
    "review_date",
    "last_checked_at",
    "balance",
    "observed_at",
    "balance_read_status",
    "recent_deductions",
    "deductions_interval",
    "funding_math_status",
    "funded_increase_amount",
    "funded_increase_observed",
    "selected_top_up_credits",
    "baseline_credits_per_day",
    "baseline_window",
    "runway_days",
    "monthly_optional_credit_cap",
    "monthly_optional_credits_used",
    "optional_calls_enabled",
}
SOURCE_PAYLOAD_FIELDS = SCOPE_FIELDS | {
    "catalog_date",
    "catalog_digest",
    "synthetic_route_count",
    "fixture_source",
    "sources",
}
PULSE_FIELDS = SCOPE_FIELDS | {
    "signal_id",
    "signal_name",
    "continuity_day",
    "what_changed",
    "evidence_state",
    "deep_link",
    "material_change",
}
READINESS_STATES = {"ready", "thin", "contradictory", "unchecked"}
LINEAGE_RELATIONS = {"continues", "merges_into", "splits_into"}
SOURCE_STATUSES = {
    "active",
    "pilot",
    "available_unwired",
    "blocked",
    "permanently_rejected",
    "inventory_only",
}
# The Wave 1 kill test verdict carries its measured zeros and names its reason
# in both blocking_reason and kill_test_result; review_date stays null because
# no writer produces a reviewed rejection (ruled 5 Sep 2026).
KILL_TEST_REASONS = {"zero_unique_observations", "zero_marginal_downstream_rows"}
SOURCE_ROUTE_ROLES = {
    "evidence",
    "utility_balance",
    "utility_catalog",
    "identity_discovery",
    "identity_hygiene",
    "inventory",
}
EVIDENCE_ROLES = {"identity", "direction", "context", "contradiction", "geo"}
EVIDENCE_DIRECTIONS = {"rising", "stable", "declining", "conflicting", "not_applicable"}
EVIDENCE_AVAILABILITY = {"available", "aged_out", "unavailable"}
INTENTS = {
    "landscape",
    "explanation",
    "comparison",
    "trajectory",
    "audience",
    "creator",
    "brand_role",
    "whitespace",
    "risk",
    "historical_analogue",
    "campaign_opportunity",
    "source_coverage",
    "custom",
}
IDENTIFIER_PATTERN = re.compile(r"^(?:sig|ev|endpoint|srcperf)_[0-9a-f]{64}$")
EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
PHONE_PATTERN = re.compile(
    r"(?:\+\d{8,15}\b|\+\d{1,3}(?:[ .()\-]*\d){7,14}\b|"
    r"\b0\d{2}[ -]?\d{3}[ -]?\d{4}\b)"
)
PRODUCT_PATTERN = re.compile(
    r"brand24|socialcrawl|bigquery|vertex|graph score|signal index|trend_scores|signal_candidates_v2|signal_evidence_v2",
    re.IGNORECASE,
)
CONFUSABLES = str.maketrans(
    {
        "\u0405": "S",
        "\u0455": "s",
        "\u0410": "A",
        "\u0430": "a",
        "\u0412": "B",
        "\u0415": "E",
        "\u0435": "e",
        "\u041a": "K",
        "\u041c": "M",
        "\u041d": "H",
        "\u041e": "O",
        "\u043e": "o",
        "\u0420": "P",
        "\u0440": "p",
        "\u0421": "C",
        "\u0441": "c",
        "\u0422": "T",
        "\u0425": "X",
        "\u0445": "x",
        "\u0406": "I",
        "\u0456": "i",
        "\u0408": "J",
        "\u0458": "j",
        "\u03a5": "Y",
        "\u03c5": "y",
    }
)
POLLING_DISCLAIMER = "social conversation is not representative polling"
ELECTION_SECTIONS = {
    "scope",
    "conversation_themes",
    "associated_factors",
    "audience_lenses",
    "brand_roles",
    "safety_flags",
    "evidence_readiness",
    "recommendations",
    "limitations",
}
FIXTURE_ACRONYMS = {"ZA", "NG", "KE"}


class FixtureValidationError(ValueError):
    def __init__(self, fixture_id: str, rule: str):
        self.fixture_id = fixture_id
        self.rule = rule
        super().__init__(f"{fixture_id}: {rule}")


@dataclass(frozen=True, slots=True)
class FixtureResult:
    fixture_id: str
    fixture_kind: str
    sha256: str
    payload: Any


@dataclass(frozen=True, slots=True)
class FixturePackageResult:
    contract_version: str
    fixture_count: int
    manifest_sha256: str
    fixtures: tuple[FixtureResult, ...]


def _fail(fixture_id: str, rule: str) -> None:
    raise FixtureValidationError(fixture_id, rule)


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _review_root(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _evidence_review_projection(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in payload["evidence"]]


def _validate_evidence_root(fixture_id: str, payload: dict[str, Any]) -> None:
    expected = EVIDENCE_REVIEW_ROOTS.get(fixture_id)
    if expected is None:
        return
    try:
        observed = _review_root(_evidence_review_projection(payload))
    except (KeyError, TypeError):
        return
    if observed != expected:
        _fail(fixture_id, "evidence_review_root_mismatch")


def _validate_golden_root(fixture_id: str, payload: dict[str, Any]) -> None:
    expected = (
        CITED_ELECTION_PAYLOAD_ROOT
        if fixture_id == "cited_answer_election"
        else GOLDEN_PAYLOAD_ROOTS[fixture_id]
    )
    if _review_root(payload) != expected:
        _fail(fixture_id, "golden_semantic_root_mismatch")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _load_json(path: Path, fixture_id: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _fail(fixture_id, "json_invalid")
    if not isinstance(value, dict):
        _fail(fixture_id, "json_object_required")
    return value, raw


def _manifest_digest(entries: list[dict[str, str]]) -> str:
    lines = "".join(
        f"{entry['fixture_id']} {entry['sha256']}\n"
        for entry in sorted(entries, key=lambda entry: entry["fixture_id"])
    )
    return hashlib.sha256(lines.encode()).hexdigest()


def _exact(fixture_id: str, value: Any, fields: set[str], rule: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        _fail(fixture_id, rule)
    return value


def _as_list(fixture_id: str, value: Any, rule: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(fixture_id, rule)
    return value


def _as_str(fixture_id: str, value: Any, rule: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        _fail(fixture_id, rule)
    return value


def _as_number(fixture_id: str, value: Any, rule: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(fixture_id, rule)
    return value


def _parse_date(fixture_id: str, value: Any, rule: str) -> date:
    text = _as_str(fixture_id, value, rule)
    try:
        return date.fromisoformat(text)
    except ValueError:
        _fail(fixture_id, rule)


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _scripts(text: str) -> set[str]:
    found = set()
    for character in text:
        name = unicodedata.name(character, "")
        for script in ("LATIN", "CYRILLIC", "GREEK"):
            if script in name:
                found.add(script)
    return found


def _safe_string(fixture_id: str, text: str) -> None:
    normalized = _normalized(text)
    skeleton = normalized.translate(CONFUSABLES)
    if PRODUCT_PATTERN.search(normalized) or PRODUCT_PATTERN.search(skeleton):
        _fail(fixture_id, "product_term_forbidden")
    if len(_scripts(normalized)) > 1:
        _fail(fixture_id, "unicode_mixed_script_forbidden")
    if EMAIL_PATTERN.search(normalized) or PHONE_PATTERN.search(normalized):
        _fail(fixture_id, "pii_pattern_forbidden")
    for url in re.findall(r"https?://[^\s\"}]+", normalized):
        if not (urlparse(url).hostname or "").lower().endswith(".invalid"):
            _fail(fixture_id, "url_must_use_invalid_tld")


def _walk_safety(fixture_id: str, value: Any, key: str | None = None) -> None:
    if isinstance(value, dict):
        for child_key, child in value.items():
            _walk_safety(fixture_id, child, child_key)
        return
    if isinstance(value, list):
        for child in value:
            _walk_safety(fixture_id, child, key)
        return
    if isinstance(value, str):
        _safe_string(fixture_id, value)
    if key == "client_scope_id" and value not in {"fixture_scope", "qa_canary"}:
        _fail(fixture_id, "identity_must_be_synthetic")
    if key == "brand_config_id" and value not in {"fixture_brand", "qa"}:
        _fail(fixture_id, "identity_must_be_synthetic")
    if key == "theme_id" and value not in {"fixture_theme", "qa"}:
        _fail(fixture_id, "identity_must_be_synthetic")
    if key == "vendor" and value != "fixture_vendor":
        _fail(fixture_id, "identity_must_be_synthetic")
    if (
        key == "author_label"
        and value is not None
        and (not isinstance(value, str) or re.fullmatch(r"@fixture_[a-z0-9_]+", value) is None)
    ):
        _fail(fixture_id, "identity_must_be_synthetic")
    if (
        key == "source_label"
        and value is not None
        and (
            not isinstance(value, str)
            or re.fullmatch(r"Fixture [a-z0-9][a-z0-9 -]*", value) is None
        )
    ):
        _fail(fixture_id, "identity_must_be_synthetic")
    if (
        key == "excerpt"
        and value is not None
        and (not isinstance(value, str) or not value.startswith("Synthetic fixture "))
    ):
        _fail(fixture_id, "identity_must_be_synthetic")
    if key == "excerpt" and isinstance(value, str):
        remainder = value.removeprefix("Synthetic fixture ")
        for sentence in re.split(r"(?<=[.!?])\s+", remainder):
            for match in re.finditer(r"\b[A-Z][A-Za-z-]*\b", sentence):
                token = match.group(0)
                if token not in FIXTURE_ACRONYMS:
                    _fail(fixture_id, "identity_must_be_synthetic")
    if (
        key == "metric_label"
        and value is not None
        and (not isinstance(value, str) or "synthetic" not in _normalized(value).lower())
    ):
        _fail(fixture_id, "identity_must_be_synthetic")
    if key == "row_id" and (not isinstance(value, str) or not value.startswith("fixture_")):
        _fail(fixture_id, "identity_must_be_synthetic")
    if key in {"account_id", "request_id", "secret", "passcode", "api_key"}:
        _fail(fixture_id, "sensitive_field_forbidden")


def _identifier(fixture_id: str, value: Any) -> str:
    text = _as_str(fixture_id, value, "identifier_invalid")
    if not IDENTIFIER_PATTERN.fullmatch(text):
        _fail(fixture_id, "identifier_invalid")
    return text


def _scope_from(value: dict[str, Any]) -> dict[str, Any]:
    return {field: value[field] for field in SCOPE_FIELDS}


def _validate_scope(fixture_id: str, value: Any) -> dict[str, Any]:
    scope = _exact(fixture_id, value, SCOPE_FIELDS, "scope_fields_invalid")
    if scope["contract_version"] != CONTRACT_VERSION:
        _fail(fixture_id, "contract_version_invalid")
    for field in ("run_id", "client_scope_id", "brand_config_id", "theme_id"):
        if not _as_str(fixture_id, scope[field], "scope_type_invalid"):
            _fail(fixture_id, "scope_type_invalid")
    markets = _as_list(fixture_id, scope["market_scope"], "scope_type_invalid")
    lenses = _as_list(fixture_id, scope["audience_lens_ids"], "scope_type_invalid")
    if not markets or any(not isinstance(item, str) for item in markets + lenses):
        _fail(fixture_id, "scope_type_invalid")
    return scope


def _validate_window(fixture_id: str, value: Any, rule: str) -> None:
    window = _exact(fixture_id, value, {"start_date", "end_date"}, rule)
    if _parse_date(fixture_id, window["end_date"], rule) < _parse_date(
        fixture_id, window["start_date"], rule
    ):
        _fail(fixture_id, "evidence_plan_window_invalid")


def _validate_lenses(fixture_id: str, value: Any) -> None:
    for raw in _as_list(fixture_id, value, "audience_lens_type_invalid"):
        lens = _exact(fixture_id, raw, LENS_FIELDS, "audience_lens_fields_invalid")
        if lens["basis"] not in {"inferred", "measured"}:
            _fail(fixture_id, "audience_basis_invalid")
        for field in ("lens_id", "source", "claim"):
            if not _as_str(fixture_id, lens[field], "audience_lens_type_invalid"):
                _fail(fixture_id, "audience_lens_type_invalid")
        _validate_window(fixture_id, lens["window"], "audience_lens_window_invalid")
        confidence = _as_number(fixture_id, lens["confidence"], "audience_confidence_invalid")
        if not 0 <= confidence <= 1:
            _fail(fixture_id, "audience_confidence_invalid")


def _validate_evidence(fixture_id: str, value: Any, cited: bool) -> tuple[set[str], dict[str, str]]:
    evidence = _as_list(fixture_id, value, "evidence_type_invalid")
    ids: set[str] = set()
    labels: dict[str, str] = {}
    fields = ANSWER_EVIDENCE_FIELDS if cited else EVIDENCE_FIELDS
    for raw in evidence:
        item = _exact(fixture_id, raw, fields, "evidence_fields_invalid")
        evidence_id = _identifier(fixture_id, item["evidence_id"])
        _identifier(fixture_id, item["signal_id"])
        if evidence_id in ids:
            _fail(fixture_id, "duplicate_evidence_id")
        ids.add(evidence_id)
        for field in ("row_id", "source_family", "platform"):
            if not _as_str(fixture_id, item[field], "evidence_type_invalid"):
                _fail(fixture_id, "evidence_type_invalid")
        for field in ("source_label", "author_label", "excerpt", "metric_label"):
            _as_str(fixture_id, item[field], "evidence_display_type_invalid", True)
        url = _as_str(fixture_id, item["url"], "evidence_url_type_invalid", True)
        if url is not None and not (urlparse(url).hostname or "").endswith(".invalid"):
            _fail(fixture_id, "url_must_use_invalid_tld")
        published_at = _as_str(
            fixture_id, item["published_at"], "evidence_published_at_type_invalid", True
        )
        if published_at is not None:
            try:
                parsed_published_at = datetime.fromisoformat(published_at)
            except ValueError:
                _fail(fixture_id, "evidence_published_at_invalid")
            if not published_at.endswith("Z") or parsed_published_at.tzinfo != UTC:
                _fail(fixture_id, "evidence_published_at_invalid")
        if item["claim_role"] not in EVIDENCE_ROLES:
            _fail(fixture_id, "evidence_claim_role_invalid")
        if item["direction"] not in EVIDENCE_DIRECTIONS:
            _fail(fixture_id, "evidence_direction_invalid")
        if item["availability"] not in EVIDENCE_AVAILABILITY:
            _fail(fixture_id, "evidence_availability_invalid")
        confidence = _as_number(
            fixture_id, item["geo_confidence"], "evidence_geo_confidence_invalid"
        )
        if not 0 <= confidence <= 1:
            _fail(fixture_id, "evidence_geo_confidence_invalid")
        if item["availability"] in {"aged_out", "unavailable"} and any(
            item[field] is not None for field in ("url", "author_label", "excerpt", "metric_label")
        ):
            _fail(fixture_id, "evidence_availability_nullability_invalid")
        if cited:
            label = _as_str(fixture_id, item["citation_label"], "citation_label_invalid")
            if not re.fullmatch(r"\[E\d+\]", label) or label in labels:
                _fail(fixture_id, "citation_label_invalid")
            labels[label] = evidence_id
    return ids, labels


def _validate_answer(
    fixture_id: str, value: Any, evidence_ids: set[str], labels: dict[str, str]
) -> None:
    answer = _exact(fixture_id, value, ANSWER_FIELDS, "answer_fields_invalid")
    _as_str(fixture_id, answer["title"], "answer_type_invalid")
    _as_str(fixture_id, answer["scope"], "answer_type_invalid")
    sections = _as_list(fixture_id, answer["sections"], "answer_sections_type_invalid")
    seen = set()
    for raw in sections:
        section = _exact(fixture_id, raw, SECTION_FIELDS, "answer_section_fields_invalid")
        section_id = _as_str(fixture_id, section["section_id"], "answer_section_type_invalid")
        _as_str(fixture_id, section["heading"], "answer_section_type_invalid")
        body = _as_str(fixture_id, section["body"], "answer_body_type_invalid")
        if section_id in seen:
            _fail(fixture_id, "answer_section_duplicate")
        seen.add(section_id)
        used = _as_list(fixture_id, section["evidence_ids"], "answer_evidence_ids_invalid")
        if any(not isinstance(item, str) for item in used) or not set(used) <= evidence_ids:
            _fail(fixture_id, "citation_unresolved")
        for sentence in re.split(r"(?<=[.!?])\s+", body):
            if not sentence.strip() or section_id in {"scope", "limitations"}:
                continue
            sentence_labels = set(re.findall(r"\[E\d+\]", sentence))
            if not sentence_labels:
                _fail(fixture_id, "sentence_citation_required")
            if not sentence_labels <= set(labels) or {
                labels[label] for label in sentence_labels
            } - set(used):
                _fail(fixture_id, "citation_unresolved")


def _validate_plan(fixture_id: str, value: Any) -> dict[str, Any]:
    plan = _exact(fixture_id, value, PLAN_FIELDS, "evidence_plan_fields_invalid")
    _validate_scope(fixture_id, _scope_from(plan))
    for field in ("plan_id", "decision", "output_form"):
        if not _as_str(fixture_id, plan[field], "evidence_plan_type_invalid"):
            _fail(fixture_id, "evidence_plan_type_invalid")
    if plan["intent"] not in INTENTS:
        _fail(fixture_id, "intent_invalid")
    if _as_list(fixture_id, plan["markets"], "evidence_plan_type_invalid") != plan["market_scope"]:
        _fail(fixture_id, "scope_envelope_mismatch")
    _validate_window(fixture_id, plan["window"], "evidence_plan_window_invalid")
    _validate_lenses(fixture_id, plan["audience_lenses"])
    for field in ("source_families", "evidence_requirements"):
        items = _as_list(fixture_id, plan[field], "evidence_plan_type_invalid")
        if any(not isinstance(item, str) for item in items):
            _fail(fixture_id, "evidence_plan_type_invalid")
        if field == "source_families" and not items:
            _fail(fixture_id, "evidence_plan_source_families_required")
    historical = _exact(
        fixture_id,
        plan["historical_comparison"],
        {"required", "window_days"},
        "historical_comparison_fields_invalid",
    )
    if not isinstance(historical["required"], bool) or not isinstance(
        historical["window_days"], int
    ):
        _fail(fixture_id, "historical_comparison_type_invalid")
    if plan["evidence_state"] not in READINESS_STATES:
        _fail(fixture_id, "evidence_state_invalid")
    return plan


def _validate_request(fixture_id: str, value: Any) -> dict[str, Any]:
    request = _exact(fixture_id, value, REQUEST_FIELDS, "request_fields_invalid")
    _validate_scope(fixture_id, _scope_from(request))
    for field in ("question", "decision", "output_form"):
        if not _as_str(fixture_id, request[field], "request_type_invalid"):
            _fail(fixture_id, "request_type_invalid")
    _validate_window(fixture_id, request["window"], "request_window_invalid")
    return request


def _validate_budget(fixture_id: str, value: Any) -> None:
    budget = _exact(fixture_id, value, BUDGET_FIELDS, "budget_fields_invalid")
    numbers = {}
    for field in (
        "input_ceiling",
        "output_ceiling",
        "input_used",
        "output_used",
        "remaining_max_output_tokens",
    ):
        number = _as_number(fixture_id, budget[field], "budget_type_invalid")
        if not isinstance(number, int) or number < 0:
            _fail(fixture_id, "budget_type_invalid")
        numbers[field] = number
    if not isinstance(budget["exhausted"], bool):
        _fail(fixture_id, "budget_type_invalid")
    if budget["metering_status"] not in {"ready", "persisted", "failed"}:
        _fail(fixture_id, "budget_metering_status_invalid")
    if (
        numbers["input_used"] > numbers["input_ceiling"]
        or numbers["output_used"] > numbers["output_ceiling"]
    ):
        _fail(fixture_id, "budget_usage_invalid")
    remaining = max(0, numbers["output_ceiling"] - numbers["output_used"])
    if numbers["remaining_max_output_tokens"] != remaining:
        _fail(fixture_id, "budget_remaining_invalid")
    if budget["exhausted"] != (remaining == 0):
        _fail(fixture_id, "budget_exhaustion_invalid")


def _validate_claims(fixture_id: str, value: Any) -> None:
    text = _normalized(json.dumps(value, ensure_ascii=False, sort_keys=True)).lower()
    causal = re.sub(r"association is not causation|without establishing (?:a )?cause", "", text)
    if re.search(r"\b(?:caused?|causal|drove|drives?|driven|led to|leads? to)\b", causal):
        _fail(fixture_id, "causal_claim_forbidden")
    polling = text.replace(POLLING_DISCLAIMER, "")
    if re.search(
        r"represent(?:s|ed)? voter opinion|voters? (?:think|believe|prefer)|poll(?:ing)? results?|representative sample|representative polling",
        polling,
    ):
        _fail(fixture_id, "polling_claim_forbidden")
    if re.search(
        r"\b(?:male|female|men|women|boys|girls|gender)\b|\baged?\b|eighteen to twenty four|18[ -]24",
        text,
    ):
        _fail(fixture_id, "demographic_claim_forbidden")
    prediction = text.replace("not a forecast", "")
    if re.search(
        r"\bwill (?:definitely )?(?:return|happen|occur)|\bnext month\b|\bforecast|\bpredict|\bcertain to\b|\bguaranteed to\b",
        prediction,
    ):
        _fail(fixture_id, "prediction_claim_forbidden")
    if re.search(r"export is approved|approved for export|export is allowed", text):
        _fail(fixture_id, "election_export_claim_forbidden")


def _validate_election(fixture_id: str, payload: dict[str, Any]) -> None:
    if payload.get("human_review_required") is not True:
        _fail(fixture_id, "election_review_required")
    if payload.get("export_allowed") is not False:
        _fail(fixture_id, "election_export_block_required")
    if POLLING_DISCLAIMER not in _normalized(json.dumps(payload, ensure_ascii=False)).lower():
        _fail(fixture_id, "election_polling_disclaimer_required")
    if "evidence" in payload:
        family_directions: dict[str, set[str]] = {}
        for item in payload["evidence"]:
            if not isinstance(item, dict):
                continue
            direction = item.get("direction")
            family = item.get("source_family")
            if direction in {"rising", "stable", "declining", "conflicting"} and isinstance(
                family, str
            ):
                family_directions.setdefault(family, set()).add(direction)
        if any(len(directions) > 1 for directions in family_directions.values()):
            _fail(fixture_id, "evidence_family_direction_conflict")
        qualifying = [next(iter(directions)) for directions in family_directions.values()]
        if len(family_directions) >= 2 and len(set(qualifying)) == 1:
            derived = "ready"
        elif len(family_directions) >= 2:
            derived = "contradictory"
        elif len(family_directions) == 1:
            derived = "thin"
        else:
            derived = "unchecked"
        plan = payload.get("evidence_plan")
        plan_state = plan.get("evidence_state") if isinstance(plan, dict) else None
        if payload.get("evidence_state") != derived or plan_state != derived:
            _fail(fixture_id, "election_readiness_invalid")
        if derived == "ready" and payload.get("contradictions"):
            _fail(fixture_id, "election_readiness_invalid")
    if "answer" in payload:
        answer = payload["answer"]
        sections = answer.get("sections") if isinstance(answer, dict) else None
        if (
            not isinstance(sections, list)
            or {item.get("section_id") for item in sections if isinstance(item, dict)}
            != ELECTION_SECTIONS
        ):
            _fail(fixture_id, "election_sections_invalid")
        evidence = payload.get("evidence")
        if isinstance(evidence, list):
            evidence_by_label = {
                item.get("citation_label"): item for item in evidence if isinstance(item, dict)
            }
            sections_by_id = {
                item.get("section_id"): item for item in sections if isinstance(item, dict)
            }
            conversation = sections_by_id.get("conversation_themes", {}).get("body")
            stated_match = (
                re.search(r"\b(\d+) synthetic posts\b", conversation)
                if isinstance(conversation, str)
                else None
            )
            metric_matches = [
                re.match(r"(\d+) synthetic posts\b", metric_label)
                for item in (
                    evidence_by_label.get("[E1]", {}),
                    evidence_by_label.get("[E2]", {}),
                )
                if isinstance((metric_label := item.get("metric_label")), str)
            ]
            if stated_match is None or len(metric_matches) != 2 or not all(metric_matches):
                _fail(fixture_id, "election_metric_total_invalid")
            cited_total = sum(int(match.group(1)) for match in metric_matches if match)
            if cited_total != int(stated_match.group(1)):
                _fail(fixture_id, "election_metric_total_invalid")
            readiness = sections_by_id.get("evidence_readiness", {}).get("body")
            labels = (
                set(re.findall(r"\[E\d+\]", readiness)) if isinstance(readiness, str) else set()
            )
            directions = {evidence_by_label.get(label, {}).get("direction") for label in labels}
            if (
                not isinstance(readiness, str)
                or "rise" not in readiness.lower()
                or labels != {"[E1]", "[E2]"}
                or directions != {"rising"}
            ):
                _fail(fixture_id, "election_direction_wording_invalid")


def _validate_semantics(fixture_id: str, payload: dict[str, Any]) -> None:
    marker_id = (
        "golden_11_election_brand_role" if fixture_id == "cited_answer_election" else fixture_id
    )
    evidence_marker, answer_marker = TASK_SEMANTICS[marker_id]
    evidence_text = _normalized(
        json.dumps(payload["evidence"], ensure_ascii=False, sort_keys=True)
    ).lower()
    answer_text = _normalized(
        json.dumps(payload["answer"], ensure_ascii=False, sort_keys=True)
    ).lower()
    if evidence_marker not in evidence_text:
        _fail(fixture_id, "golden_semantic_evidence_invalid")
    if answer_marker not in answer_text:
        _fail(fixture_id, "golden_semantic_answer_invalid")


def _validate_task_contract(
    fixture_id: str,
    payload: dict[str, Any],
    request: dict[str, Any],
    plan: dict[str, Any],
) -> None:
    marker_id = (
        "golden_11_election_brand_role" if fixture_id == "cited_answer_election" else fixture_id
    )
    decision, requirements, historical_required, window_days = PLAN_EXPECTATIONS[marker_id]
    if request["decision"] != decision or plan["decision"] != decision:
        _fail(fixture_id, "golden_plan_semantic_invalid")
    if set(plan["evidence_requirements"]) != requirements:
        _fail(fixture_id, "golden_plan_semantic_invalid")
    if plan["historical_comparison"] != {
        "required": historical_required,
        "window_days": window_days,
    }:
        _fail(fixture_id, "golden_plan_semantic_invalid")
    if marker_id == "golden_10_custom" and request["question"] != CUSTOM_QUESTION:
        _fail(fixture_id, "custom_question_required")

    evidence = payload["evidence"]
    evidence_by_label = {
        item.get("citation_label"): item
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("citation_label"), str)
    }
    answer = payload["answer"]
    answer_sections = {
        item.get("section_id"): item
        for item in answer.get("sections", [])
        if isinstance(item, dict) and isinstance(item.get("section_id"), str)
    }
    for section_id, (claim_terms, support_by_label) in SECTION_SUPPORT[marker_id].items():
        section = answer_sections.get(section_id)
        if not isinstance(section, dict) or not isinstance(section.get("body"), str):
            _fail(fixture_id, "golden_section_claim_invalid")
        body = _normalized(section["body"]).lower()
        if any(term not in body for term in claim_terms):
            _fail(fixture_id, "golden_section_claim_invalid")
        cited_labels = set(re.findall(r"\[E\d+\]", section["body"]))
        if cited_labels != set(support_by_label):
            _fail(fixture_id, "golden_section_support_invalid")
        expected_ids = set()
        for label, support_term in support_by_label.items():
            item = evidence_by_label.get(label)
            if not isinstance(item, dict):
                _fail(fixture_id, "golden_section_support_invalid")
            support_text = _normalized(json.dumps(item, ensure_ascii=False, sort_keys=True)).lower()
            if support_term not in support_text:
                _fail(fixture_id, "golden_section_support_invalid")
            expected_ids.add(item["evidence_id"])
        if set(section.get("evidence_ids", [])) != expected_ids:
            _fail(fixture_id, "citation_unresolved")


def _validate_repeated_golden_values(
    fixture_id: str,
    payload: dict[str, Any],
    request: dict[str, Any],
    plan: dict[str, Any],
) -> None:
    if request["window"] != plan["window"] or request["output_form"] != plan["output_form"]:
        _fail(fixture_id, "scope_envelope_mismatch")
    lens_ids = [lens["lens_id"] for lens in payload["audience_lenses"]]
    if (
        lens_ids != request["audience_lens_ids"]
        or lens_ids != plan["audience_lens_ids"]
        or any(lens["window"] != request["window"] for lens in payload["audience_lenses"])
    ):
        _fail(fixture_id, "scope_envelope_mismatch")

    answer = payload["answer"]
    sections = {section["section_id"]: section for section in answer["sections"]}
    scope = sections.get("scope")
    if not isinstance(scope, dict) or not scope["body"].startswith(answer["scope"]):
        _fail(fixture_id, "scope_envelope_mismatch")

    answer_text = json.dumps(answer, ensure_ascii=False).lower()
    confidence_mentions = {
        float(value)
        for value in re.findall(r"\b(0(?:\.\d+)?|1(?:\.0+)?) confidence\b", answer_text)
    }
    if not confidence_mentions <= {lens["confidence"] for lens in payload["audience_lenses"]}:
        _fail(fixture_id, "audience_confidence_mismatch")

    readiness = sections.get("evidence_readiness")
    if isinstance(readiness, dict) and payload["evidence_state"] not in readiness["body"].lower():
        _fail(fixture_id, "evidence_state_invalid")


def _validate_dynamic(fixture_id: str, payload: dict[str, Any]) -> None:
    _exact(fixture_id, payload, DYNAMIC_PAYLOAD_FIELDS, "dynamic_fields_invalid")
    _validate_scope(fixture_id, _scope_from(payload))
    signal = _exact(
        fixture_id, payload["signal"], DYNAMIC_SIGNAL_FIELDS, "dynamic_signal_fields_invalid"
    )
    _identifier(fixture_id, signal["signal_id"])
    if signal["evidence_state"] not in READINESS_STATES:
        _fail(fixture_id, "evidence_state_invalid")
    ids, _ = _validate_evidence(fixture_id, payload["evidence"], False)
    _validate_evidence_root(fixture_id, payload)
    if not isinstance(signal["evidence_ids"], list) or set(signal["evidence_ids"]) != ids:
        _fail(fixture_id, "citation_unresolved")


def _validate_readiness(fixture_id: str, payload: dict[str, Any]) -> None:
    _exact(fixture_id, payload, READINESS_FIELDS, "readiness_fields_invalid")
    _identifier(fixture_id, payload["signal_id"])
    state = payload["evidence_state"]
    if state not in READINESS_STATES:
        _fail(fixture_id, "evidence_state_invalid")
    count, directions = payload["qualifying_family_count"], payload["direction_by_family"]
    if not isinstance(count, int) or isinstance(count, bool) or not isinstance(directions, dict):
        _fail(fixture_id, "readiness_type_invalid")
    if any(
        not isinstance(key, str) or value not in EVIDENCE_DIRECTIONS
        for key, value in directions.items()
    ):
        _fail(fixture_id, "evidence_direction_invalid")
    ids, _ = _validate_evidence(fixture_id, payload["evidence"], False)
    _validate_evidence_root(fixture_id, payload)
    valid = {
        "ready": count >= 2 and len(directions) == count and len(set(directions.values())) == 1,
        "thin": count == 1 and len(directions) == 1,
        "contradictory": count >= 2
        and len(directions) == count
        and len(set(directions.values())) > 1,
        "unchecked": count == 0 and not directions,
    }
    if len(ids) != count or not valid[state]:
        _fail(fixture_id, "evidence_readiness_logic_invalid")


def _validate_lineage(fixture_id: str, payload: dict[str, Any]) -> None:
    _exact(fixture_id, payload, {"edges"}, "lineage_fields_invalid")
    edges = _as_list(fixture_id, payload["edges"], "lineage_type_invalid")
    if len(edges) != 2:
        _fail(fixture_id, "lineage_many_to_many_invalid")
    for raw in edges:
        edge = _exact(fixture_id, raw, LINEAGE_EDGE_FIELDS, "lineage_edge_fields_invalid")
        _validate_scope(fixture_id, _scope_from(edge))
        _identifier(fixture_id, edge["from_signal_id"])
        _identifier(fixture_id, edge["to_signal_id"])
        if edge["relation"] not in LINEAGE_RELATIONS:
            _fail(fixture_id, "lineage_relation_invalid")
        if not 0 <= _as_number(fixture_id, edge["overlap_score"], "lineage_overlap_invalid") <= 1:
            _fail(fixture_id, "lineage_overlap_invalid")
    relation = {edge["relation"] for edge in edges}
    from_count = len({edge["from_signal_id"] for edge in edges})
    to_count = len({edge["to_signal_id"] for edge in edges})
    valid = (
        fixture_id == "lineage_merge"
        and relation == {"merges_into"}
        and from_count == 2
        and to_count == 1
    ) or (
        fixture_id == "lineage_split"
        and relation == {"splits_into"}
        and from_count == 1
        and to_count == 2
    )
    if not valid:
        _fail(fixture_id, "lineage_many_to_many_invalid")


def _validate_source_lab(fixture_id: str, payload: dict[str, Any]) -> None:
    _exact(fixture_id, payload, SOURCE_PAYLOAD_FIELDS, "source_payload_fields_invalid")
    _validate_scope(fixture_id, _scope_from(payload))
    if payload["fixture_source"] != "synthetic_shape_only":
        _fail(fixture_id, "source_fixture_scope_invalid")
    sources = _as_list(fixture_id, payload["sources"], "source_type_invalid")
    if payload["synthetic_route_count"] != 6 or len(sources) != 6:
        _fail(fixture_id, "source_synthetic_count_invalid")
    observed = {
        "calls",
        "credits",
        "rows",
        "integrity",
        "geo_precision",
        "unique_lift",
        "last_success_at",
    }
    statuses = set()
    for raw in sources:
        source = _exact(fixture_id, raw, SOURCE_FIELDS, "source_fields_invalid")
        status = source["status"]
        if not isinstance(status, str) or status not in SOURCE_STATUSES:
            _fail(fixture_id, "source_status_invalid")
        statuses.add(status)
        if source["route_role"] not in SOURCE_ROUTE_ROLES:
            _fail(fixture_id, "source_route_role_invalid")
        if source["http_method"] not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            _fail(fixture_id, "source_http_method_invalid")
        _identifier(fixture_id, source["endpoint_id"])
        if status in {"active", "pilot"}:
            valid = (
                all(source[field] is not None for field in observed - {"unique_lift"})
                and source["blocking_reason"] is None
                and source["kill_test_result"] is None
                and source["review_date"] is None
                and bool(source["downstream_consumers"])
            )
        elif status == "permanently_rejected":
            # The run keeps what it measured: rows is the observation count and
            # unique_lift the marginal downstream count, so a route killed for
            # zero marginal rows still carries its observations. The two are
            # never both positive.
            valid = (
                source["rows"] is not None
                and source["unique_lift"] is not None
                and not (source["rows"] > 0 and source["unique_lift"] > 0)
                and (source["blocking_reason"] != "zero_unique_observations" or source["rows"] == 0)
                and (
                    source["blocking_reason"] != "zero_marginal_downstream_rows"
                    or source["unique_lift"] == 0
                )
                and all(source[field] is None for field in observed - {"rows", "unique_lift"})
                and source["blocking_reason"] in KILL_TEST_REASONS
                and source["kill_test_result"] == source["blocking_reason"]
                and source["review_date"] is None
                and source["downstream_consumers"] == []
            )
        else:
            valid = (
                all(source[field] is None for field in observed)
                and source["downstream_consumers"] == []
            )
            if status == "available_unwired":
                valid = valid and bool(source["official_price_components"])
            elif status == "blocked":
                valid = valid and bool(source["blocking_reason"])
        if not valid:
            _fail(fixture_id, "source_status_nullability_invalid")
    if statuses != SOURCE_STATUSES:
        _fail(fixture_id, "source_status_set_invalid")


def _validate_golden(fixture_id: str, payload: dict[str, Any], cited: bool = False) -> None:
    expected = GOLDEN_FIELDS | ({"export_allowed"} if "election" in fixture_id else set())
    if cited:
        expected |= SCOPE_FIELDS | {
            "question_id",
            "status",
            "error",
            "created_at",
            "updated_at",
            "export_allowed",
        }
    _exact(fixture_id, payload, expected, "golden_fields_invalid")
    answer = payload["answer"]
    sections = answer.get("sections") if isinstance(answer, dict) else None
    semantic_types_valid = (
        isinstance(payload["evidence"], list)
        and isinstance(sections, list)
        and all(
            isinstance(section, dict) and isinstance(section.get("body"), str)
            for section in sections
        )
    )
    if semantic_types_valid:
        _validate_semantics(fixture_id, payload)
    request = _validate_request(fixture_id, payload["request"])
    plan = _validate_plan(fixture_id, payload["evidence_plan"])
    if _scope_from(request) != _scope_from(plan):
        _fail(fixture_id, "scope_envelope_mismatch")
    if "election" in fixture_id:
        _validate_election(fixture_id, payload)
    if semantic_types_valid:
        _validate_task_contract(fixture_id, payload, request, plan)
    if cited:
        _validate_scope(fixture_id, _scope_from(payload))
        if _scope_from(payload) != _scope_from(request):
            _fail(fixture_id, "scope_envelope_mismatch")
    _validate_lenses(fixture_id, payload["audience_lenses"])
    if payload["audience_lenses"] != plan["audience_lenses"]:
        _fail(fixture_id, "scope_envelope_mismatch")
    ids, labels = _validate_evidence(fixture_id, payload["evidence"], True)
    _validate_evidence_root(fixture_id, payload)
    _validate_answer(fixture_id, payload["answer"], ids, labels)
    _validate_budget(fixture_id, payload["budget"])
    if (
        payload["evidence_state"] not in READINESS_STATES
        or payload["evidence_state"] != plan["evidence_state"]
    ):
        _fail(fixture_id, "evidence_state_invalid")
    for field in ("limitations", "missing_work", "contradictions"):
        items = _as_list(fixture_id, payload[field], "golden_list_type_invalid")
        if any(not isinstance(item, str) for item in items):
            _fail(fixture_id, "golden_list_type_invalid")
        if field == "limitations" and not items:
            _fail(fixture_id, "golden_limitations_required")
    if not isinstance(payload["human_review_required"], bool):
        _fail(fixture_id, "golden_human_review_type_invalid")
    marker_id = "golden_11_election_brand_role" if cited else fixture_id
    expected_intent = EXPECTED_INTENTS[marker_id]
    if payload["expected_intent"] != expected_intent or plan["intent"] != expected_intent:
        _fail(
            fixture_id,
            "custom_intent_required" if marker_id == "golden_10_custom" else "intent_invalid",
        )
    if cited:
        status = payload["status"]
        if status not in {"pending", "running", "complete", "partial", "failed"}:
            _fail(fixture_id, "lifecycle_state_invalid")
        if status in {"complete", "partial"} and (
            payload["answer"] is None or payload["error"] is not None
        ):
            _fail(fixture_id, "lifecycle_state_invalid")
        elif status == "partial" and not payload["missing_work"]:
            _fail(fixture_id, "lifecycle_missing_work_invalid")
        elif status in {"pending", "running"} or (
            status == "failed"
            and (payload["answer"] is not None or not isinstance(payload["error"], dict))
        ):
            _fail(fixture_id, "lifecycle_state_invalid")
    _validate_repeated_golden_values(fixture_id, payload, request, plan)
    _validate_golden_root(fixture_id, payload)


def _validate_fixture(fixture_id: str, fixture: dict[str, Any]) -> None:
    _exact(fixture_id, fixture, ENVELOPE_FIELDS, "envelope_fields_invalid")
    if fixture["fixture_id"] != fixture_id:
        _fail(fixture_id, "fixture_id_filename_mismatch")
    if fixture["fixture_kind"] != FIXTURE_KINDS[fixture_id]:
        _fail(fixture_id, "fixture_kind_invalid")
    if fixture["contract_version"] != CONTRACT_VERSION:
        _fail(fixture_id, "contract_version_invalid")
    if fixture["sanitized"] is not True:
        _fail(fixture_id, "sanitized_required")
    if fixture["frozen_at"] != FROZEN_AT:
        _fail(fixture_id, "frozen_at_invalid")
    payload = fixture["payload"]
    if not isinstance(payload, dict):
        _fail(fixture_id, "payload_object_required")
    _walk_safety(fixture_id, fixture)
    _validate_claims(fixture_id, payload)
    kind = fixture["fixture_kind"]
    if kind == "dynamic_signal":
        _validate_dynamic(fixture_id, payload)
    elif kind == "evidence_readiness":
        _validate_readiness(fixture_id, payload)
    elif kind == "lineage":
        _validate_lineage(fixture_id, payload)
    elif kind == "source_lab":
        _validate_source_lab(fixture_id, payload)
    elif kind == "evidence_plan":
        expected = PLAN_FIELDS | {
            "human_review_required",
            "limitations",
            "missing_work",
            "export_allowed",
        }
        _exact(fixture_id, payload, expected, "evidence_plan_fields_invalid")
        plan = _validate_plan(fixture_id, {field: payload[field] for field in PLAN_FIELDS})
        decision, requirements, historical_required, window_days = PLAN_EXPECTATIONS[
            "golden_11_election_brand_role"
        ]
        if (
            plan["decision"] != decision
            or set(plan["evidence_requirements"]) != requirements
            or plan["historical_comparison"]
            != {"required": historical_required, "window_days": window_days}
            or plan["evidence_state"] != "ready"
        ):
            _fail(fixture_id, "golden_plan_semantic_invalid")
        _validate_election(fixture_id, payload)
    elif kind == "cited_answer":
        _validate_golden(fixture_id, payload, True)
    elif kind == "pulse_watched_signal_line":
        _exact(fixture_id, payload, PULSE_FIELDS, "pulse_fields_invalid")
        _validate_scope(fixture_id, _scope_from(payload))
        _identifier(fixture_id, payload["signal_id"])
        if payload["evidence_state"] not in READINESS_STATES:
            _fail(fixture_id, "evidence_state_invalid")
    elif kind == "golden_task":
        _validate_golden(fixture_id, payload)


def _load_package(directory: Path) -> FixturePackageResult:
    manifest, raw_manifest = _load_json(directory / "manifest.json", "manifest")
    _exact("manifest", manifest, MANIFEST_FIELDS, "manifest_fields_invalid")
    if raw_manifest != _canonical_bytes(manifest):
        _fail("manifest", "noncanonical_json")
    if manifest["contract_version"] != CONTRACT_VERSION:
        _fail("manifest", "contract_version_invalid")
    entries = _as_list("manifest", manifest["fixtures"], "manifest_entries_invalid")
    ids = []
    for raw in entries:
        entry = _exact("manifest", raw, {"fixture_id", "sha256"}, "manifest_entry_fields_invalid")
        fixture_id = _as_str("manifest", entry["fixture_id"], "manifest_entries_invalid")
        digest = _as_str("manifest", entry["sha256"], "fixture_digest_invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            _fail("manifest", "fixture_digest_invalid")
        ids.append(fixture_id)
    if len(ids) != len(set(ids)):
        _fail("manifest", "duplicate_fixture_id")
    if ids != sorted(ids):
        _fail("manifest", "manifest_not_sorted")
    if set(ids) != set(FIXTURE_KINDS):
        _fail("manifest", "manifest_fixture_set_invalid")
    if manifest["fixture_count"] != 22 or len(entries) != 22:
        _fail("manifest", "fixture_count_invalid")
    expected_files = {
        "manifest.json",
        *(f"{fixture_id}.json" for fixture_id in FIXTURE_KINDS),
        *(f"{journey_id}.json" for journey_id in LIVE_INTELLIGENCE_JOURNEY_IDS),
    }
    try:
        actual_files = {path.name for path in directory.iterdir() if path.is_file()}
    except OSError:
        _fail("package", "package_directory_invalid")
    if actual_files - expected_files:
        _fail("package", "unknown_files")
    if expected_files - actual_files:
        _fail("package", "missing_files")
    if manifest["manifest_sha256"] != _manifest_digest(entries):
        _fail("manifest", "manifest_digest_mismatch")
    results = []
    for entry in entries:
        fixture_id = entry["fixture_id"]
        fixture, raw = _load_json(directory / f"{fixture_id}.json", fixture_id)
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            _fail(fixture_id, "fixture_digest_mismatch")
        if raw != _canonical_bytes(fixture):
            _fail(fixture_id, "noncanonical_json")
        _validate_fixture(fixture_id, fixture)
        results.append(
            FixtureResult(
                fixture_id, fixture["fixture_kind"], entry["sha256"], _freeze(fixture["payload"])
            )
        )
    return FixturePackageResult(
        CONTRACT_VERSION, len(results), manifest["manifest_sha256"], tuple(results)
    )


def load_fixture_package(directory: str | Path = DEFAULT_FIXTURE_DIRECTORY) -> FixturePackageResult:
    try:
        return _load_package(Path(directory))
    except FixtureValidationError:
        raise
    except Exception:
        raise FixtureValidationError("package", "unexpected_local_error") from None


def load_live_intelligence_journeys(
    directory: str | Path = DEFAULT_FIXTURE_DIRECTORY,
) -> FixturePackageResult:
    from src.analysis.open_intelligence.brain import evaluate_live_intelligence_journey

    root = Path(directory)
    entries = []
    results = []
    try:
        for journey_id in LIVE_INTELLIGENCE_JOURNEY_IDS:
            payload, raw = _load_json(root / f"{journey_id}.json", journey_id)
            if payload.get("journey_id") != journey_id:
                _fail(journey_id, "journey_identity_invalid")
            evaluate_live_intelligence_journey(payload)
            digest = hashlib.sha256(raw).hexdigest()
            entries.append({"fixture_id": journey_id, "sha256": digest})
            results.append(
                FixtureResult(
                    journey_id,
                    "live_intelligence_journey",
                    digest,
                    _freeze(payload),
                )
            )
        return FixturePackageResult(
            "42_live_intelligence_semantic_closure_v1",
            len(results),
            _manifest_digest(entries),
            tuple(results),
        )
    except FixtureValidationError:
        raise
    except Exception as error:
        raise FixtureValidationError("live_intelligence_journeys", str(error)) from error
