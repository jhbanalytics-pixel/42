from __future__ import annotations

import hashlib
import importlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

FIXTURE_DIR = Path("tests/fixtures/open_intelligence/v2")
MANIFEST_FIELDS = {"contract_version", "fixture_count", "fixtures", "manifest_sha256"}
ENVELOPE_FIELDS = {
    "fixture_id",
    "fixture_kind",
    "contract_version",
    "sanitized",
    "frozen_at",
    "payload",
}
REQUIRED_FIXTURE_IDS = {
    "dynamic_signal_ready",
    "evidence_ready",
    "evidence_thin",
    "evidence_contradictory",
    "evidence_unchecked",
    "lineage_merge",
    "lineage_split",
    "source_lab_all_statuses",
    "evidence_plan_election",
    "cited_answer_election",
    "pulse_watched_signal_line",
    "golden_01_emerging_without_keyword",
    "golden_02_why_moving",
    "golden_03_cross_market_difference",
    "golden_04_carriers",
    "golden_05_history",
    "golden_06_brand_role",
    "golden_07_audience_lens",
    "golden_08_source_agreement",
    "golden_09_source_gap",
    "golden_10_custom",
    "golden_11_election_brand_role",
}
EXPECTED_KINDS = {
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
    **{
        f"golden_{index:02d}_{suffix}": "golden_task"
        for index, suffix in (
            (1, "emerging_without_keyword"),
            (2, "why_moving"),
            (3, "cross_market_difference"),
            (4, "carriers"),
            (5, "history"),
            (6, "brand_role"),
            (7, "audience_lens"),
            (8, "source_agreement"),
            (9, "source_gap"),
            (10, "custom"),
            (11, "election_brand_role"),
        )
    },
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
FULL_ID = re.compile(r"^(?:sig|ev|endpoint|srcperf)_[0-9a-f]{64}$")
EXPECTED_MANIFEST_ROOT = "3440cd263099157f43e107ea55a6a56c835bbc1a5e7e86ef8519ce1abd181ebf"
EXPECTED_GOLDEN_PAYLOAD_ROOTS = {
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
EXPECTED_CITED_ELECTION_PAYLOAD_ROOT = (
    "d75a96f12d51c376f217fa5b8a196254330b04e8019d98eaa7d745dd289067d6"
)
CUSTOM_QUESTION = "How should an unusual synthetic repair workshop be structured?"
EXPECTED_EVIDENCE_REVIEW_ROOTS = {
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
EVIDENCE_FIELD_ORDER = (
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
)
ANSWER_EVIDENCE_FIELDS = EVIDENCE_FIELDS | {"citation_label"}
EVIDENCE_DIRECTIONS = {"rising", "stable", "declining", "conflicting", "not_applicable"}
EVIDENCE_ROLES = {"identity", "direction", "context", "contradiction", "geo"}
EVIDENCE_AVAILABILITY = {"available", "aged_out", "unavailable"}
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
ANSWER_SECTION_FIELDS = {"section_id", "heading", "body", "evidence_ids"}
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
LINEAGE_EDGE_FIELDS = SCOPE_FIELDS | {
    "signal_date",
    "market",
    "from_signal_id",
    "to_signal_id",
    "relation",
    "overlap_score",
    "created_at",
}
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
SOURCE_ROUTE_ROLES = {
    "evidence",
    "utility_balance",
    "utility_catalog",
    "identity_discovery",
    "identity_hygiene",
    "inventory",
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
PULSE_PAYLOAD_FIELDS = SCOPE_FIELDS | {
    "signal_id",
    "signal_name",
    "continuity_day",
    "what_changed",
    "evidence_state",
    "deep_link",
    "material_change",
}
CITED_FIELDS = (
    GOLDEN_FIELDS
    | SCOPE_FIELDS
    | {
        "question_id",
        "status",
        "error",
        "created_at",
        "updated_at",
        "export_allowed",
    }
)
TASK_SEMANTICS = {
    "golden_01_emerging_without_keyword": (
        "unprompted phrase clusters",
        "unprompted fixture repair circles",
    ),
    "golden_02_why_moving": (
        "baseline acceleration",
        "accelerated after a fixture parts exchange",
    ),
    "golden_03_cross_market_difference": (
        "three fixture markets",
        "za emphasizes repair steps",
    ),
    "golden_04_carriers": (
        "first carrier timestamp",
        "fixture maker one carried the move first",
    ),
    "golden_05_history": (
        "analogue one",
        "two of three observed sequences",
    ),
    "golden_06_brand_role": (
        "occupied tutorial space",
        "the tutorial space is occupied",
    ),
    "golden_07_audience_lens": (
        "inferred practical lens",
        "the inferred fixture lens fits practical instruction",
    ),
    "golden_08_source_agreement": (
        "agreement disagreement and absence",
        "forum and video agree",
    ),
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
TASK_PLAN_EXPECTATIONS = {
    "golden_01_emerging_without_keyword": {
        "decision": "Select the unprompted move to investigate.",
        "requirements": {"unprompted discovery evidence", "two independent source families"},
        "historical": (False, 0),
    },
    "golden_02_why_moving": {
        "decision": "Choose which observed driver to test in a brief.",
        "requirements": {"dated movement evidence", "source-family baseline comparison"},
        "historical": (True, 30),
    },
    "golden_03_cross_market_difference": {
        "decision": "Choose one market adaptation for the synthetic move.",
        "requirements": {"one receipt per fixture market", "cross-market comparable window"},
        "historical": (False, 0),
    },
    "golden_04_carriers": {
        "decision": "Choose which carrier sequence to investigate.",
        "requirements": {"first carrier timestamps", "spread sequence receipts"},
        "historical": (False, 0),
    },
    "golden_05_history": {
        "decision": "Decide whether observed recurrence warrants monitoring.",
        "requirements": {
            "three synthetic analogues",
            "current difference",
            "observed follow-on outcomes",
        },
        "historical": (True, 730),
    },
    "golden_06_brand_role": {
        "decision": "Choose one credible unoccupied fixture brand role.",
        "requirements": {"occupied space evidence", "unmet space evidence"},
        "historical": (False, 0),
    },
    "golden_07_audience_lens": {
        "decision": "Choose whether to apply the inferred fixture lens.",
        "requirements": {"lens basis source window and confidence", "cross-source fit evidence"},
        "historical": (False, 0),
    },
    "golden_08_source_agreement": {
        "decision": "Decide whether source disagreement blocks action.",
        "requirements": {"agreement evidence", "disagreement evidence", "absence register"},
        "historical": (False, 0),
    },
    "golden_09_source_gap": {
        "decision": "Decide whether to fund a synthetic source pilot.",
        "requirements": {"dark surface status", "decision consequence"},
        "historical": (False, 0),
    },
    "golden_10_custom": {
        "decision": "Choose one unusual synthetic workshop format.",
        "requirements": {"workshop request evidence", "demonstration handover evidence"},
        "historical": (False, 0),
    },
    "golden_11_election_brand_role": {
        "decision": "Choose a neutral election information response for human review.",
        "requirements": {
            "local conversation themes",
            "associated factors without causation",
            "brand role and safety evidence",
            "human review before export",
        },
        "historical": (False, 0),
    },
}
TASK_SECTION_SUPPORT = {
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


def _module():
    return importlib.import_module("src.contracts.open_intelligence_fixtures")


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _review_root(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _evidence_review_projection(payload: dict) -> list[dict]:
    projected = []
    for item in payload["evidence"]:
        fields = EVIDENCE_FIELD_ORDER + (("citation_label",) if "citation_label" in item else ())
        assert set(item) == set(fields)
        projected.append({field: item[field] for field in fields})
    return projected


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(_canonical_bytes(value))


def _manifest_digest(entries: list[dict]) -> str:
    lines = "".join(
        f"{entry['fixture_id']} {entry['sha256']}\n"
        for entry in sorted(entries, key=lambda entry: entry["fixture_id"])
    )
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _refresh_manifest(directory: Path, fixture_id: str | None = None) -> None:
    manifest_path = directory / "manifest.json"
    manifest = _read_json(manifest_path)
    if fixture_id is not None:
        entry = next(item for item in manifest["fixtures"] if item["fixture_id"] == fixture_id)
        entry["sha256"] = hashlib.sha256(
            (directory / f"{fixture_id}.json").read_bytes()
        ).hexdigest()
    manifest["manifest_sha256"] = _manifest_digest(manifest["fixtures"])
    _write_json(manifest_path, manifest)


def _mutate_fixture(
    directory: Path,
    fixture_id: str,
    mutate: Callable[[dict], None],
    *,
    refresh: bool = True,
) -> None:
    path = directory / f"{fixture_id}.json"
    fixture = _read_json(path)
    mutate(fixture)
    _write_json(path, fixture)
    if refresh:
        _refresh_manifest(directory, fixture_id)


def _copy_package(tmp_path: Path) -> Path:
    target = tmp_path / "v2"
    shutil.copytree(FIXTURE_DIR, target)
    return target


def _golden(directory: Path, fixture_id: str) -> dict:
    return _read_json(directory / f"{fixture_id}.json")["payload"]


def test_canonical_package_is_complete_valid_and_immutable():
    package = _module().load_fixture_package(FIXTURE_DIR)

    assert package.fixture_count == 22
    assert package.contract_version == "2.0.0"
    assert {fixture.fixture_id for fixture in package.fixtures} == REQUIRED_FIXTURE_IDS
    with pytest.raises((AttributeError, TypeError)):
        package.fixture_count = 0
    dynamic = next(
        fixture for fixture in package.fixtures if fixture.fixture_id == "dynamic_signal_ready"
    )
    with pytest.raises((AttributeError, TypeError)):
        dynamic.payload["evidence"].append({})


def test_files_envelopes_and_kinds_match_the_independent_contract_list():
    files = {path.name for path in FIXTURE_DIR.glob("*.json")}
    assert files == {
        "manifest.json",
        *(f"{fixture_id}.json" for fixture_id in REQUIRED_FIXTURE_IDS),
        "journey_crisis_affluent_za_v1.json",
        "journey_brand_south_africa_narratives_v1.json",
        "journey_election_brand_role_v1.json",
    }

    for fixture_id in REQUIRED_FIXTURE_IDS:
        fixture = _read_json(FIXTURE_DIR / f"{fixture_id}.json")
        assert set(fixture) == ENVELOPE_FIELDS
        assert fixture["fixture_id"] == fixture_id
        assert fixture["fixture_kind"] == EXPECTED_KINDS[fixture_id]
        assert fixture["contract_version"] == "2.0.0"
        assert fixture["sanitized"] is True
        assert fixture["frozen_at"] == "2026-08-25T00:00:00Z"


def test_fixture_json_ignore_exception_is_narrow():
    package_files = [FIXTURE_DIR / "manifest.json"] + [
        FIXTURE_DIR / f"{fixture_id}.json" for fixture_id in sorted(REQUIRED_FIXTURE_IDS)
    ]
    for path in package_files:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", str(path)],
            check=False,
        )
        assert result.returncode == 1

    for path in (Path("fixture-check-other.json"), Path("tests/fixtures/other.json")):
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", str(path)],
            check=False,
        )
        assert result.returncode == 0


def test_fixture_and_manifest_bytes_have_independently_derived_digests():
    manifest_path = FIXTURE_DIR / "manifest.json"
    manifest = _read_json(manifest_path)
    assert set(manifest) == MANIFEST_FIELDS
    assert manifest_path.read_bytes() == _canonical_bytes(manifest)
    assert manifest["fixture_count"] == 22
    assert [item["fixture_id"] for item in manifest["fixtures"]] == sorted(REQUIRED_FIXTURE_IDS)

    for entry in manifest["fixtures"]:
        path = FIXTURE_DIR / f"{entry['fixture_id']}.json"
        parsed = _read_json(path)
        assert path.read_bytes() == _canonical_bytes(parsed)
        assert entry["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])

    assert manifest["manifest_sha256"] == _manifest_digest(manifest["fixtures"])
    assert manifest["manifest_sha256"] == EXPECTED_MANIFEST_ROOT


def test_reviewed_golden_payload_roots_are_literal_and_complete():
    observed = {
        fixture_id: _review_root(_golden(FIXTURE_DIR, fixture_id))
        for fixture_id in sorted(EXPECTED_INTENTS)
    }
    assert observed == EXPECTED_GOLDEN_PAYLOAD_ROOTS
    cited = _read_json(FIXTURE_DIR / "cited_answer_election.json")["payload"]
    assert _review_root(cited) == EXPECTED_CITED_ELECTION_PAYLOAD_ROOT


def test_golden_custom_plan_limitations_confidence_scope_and_state_are_consistent():
    custom = _golden(FIXTURE_DIR, "golden_10_custom")
    assert custom["request"]["question"] == CUSTOM_QUESTION
    assert custom["expected_intent"] == custom["evidence_plan"]["intent"] == "custom"

    for fixture_id in EXPECTED_INTENTS:
        payload = _golden(FIXTURE_DIR, fixture_id)
        request = payload["request"]
        plan = payload["evidence_plan"]
        assert plan["source_families"]
        assert payload["limitations"]
        assert request["window"] == plan["window"]
        assert request["output_form"] == plan["output_form"]
        assert payload["evidence_state"] == plan["evidence_state"]
        lens_ids = [lens["lens_id"] for lens in payload["audience_lenses"]]
        assert lens_ids == request["audience_lens_ids"] == plan["audience_lens_ids"]
        assert all(lens["window"] == request["window"] for lens in payload["audience_lenses"])
        scope_section = next(
            section for section in payload["answer"]["sections"] if section["section_id"] == "scope"
        )
        assert scope_section["body"].startswith(payload["answer"]["scope"])

        confidence_mentions = {
            float(value)
            for value in re.findall(
                r"\b(0(?:\.\d+)?|1(?:\.0+)?) confidence\b",
                json.dumps(payload["answer"], ensure_ascii=False).lower(),
            )
        }
        assert confidence_mentions <= {lens["confidence"] for lens in payload["audience_lenses"]}
        readiness_sections = [
            section
            for section in payload["answer"]["sections"]
            if section["section_id"] == "evidence_readiness"
        ]
        assert all(
            payload["evidence_state"] in section["body"].lower() for section in readiness_sections
        )


def test_reviewed_evidence_roots_cover_every_accepted_field():
    assert set(EVIDENCE_FIELD_ORDER) == EVIDENCE_FIELDS
    observed = {}
    for fixture_id in EXPECTED_EVIDENCE_REVIEW_ROOTS:
        payload = _read_json(FIXTURE_DIR / f"{fixture_id}.json")["payload"]
        observed[fixture_id] = _review_root(_evidence_review_projection(payload))
    assert observed == EXPECTED_EVIDENCE_REVIEW_ROOTS

    payload = _golden(FIXTURE_DIR, "golden_01_emerging_without_keyword")
    baseline = _review_root(_evidence_review_projection(payload))
    replacements = {
        "evidence_id": "ev_" + "f" * 64,
        "signal_id": "sig_" + "f" * 64,
        "row_id": "fixture_review_row",
        "source_family": "audio",
        "platform": "fixture_platform_review",
        "source_label": "Fixture source review",
        "author_label": "@fixture_review_author",
        "excerpt": "Synthetic fixture review evidence records repair circles.",
        "metric_label": "19 synthetic posts",
        "url": "https://evidence.invalid/review",
        "published_at": "2026-08-24T10:00:00Z",
        "claim_role": "context",
        "direction": "stable",
        "geo_confidence": 0.92,
        "availability": "unavailable",
        "citation_label": "[E9]",
    }
    for field, replacement in replacements.items():
        changed = json.loads(json.dumps(payload))
        changed["evidence"][0][field] = replacement
        assert _review_root(_evidence_review_projection(changed)) != baseline


def test_evidence_display_fields_timestamps_ranges_and_enums_are_valid():
    source_pattern = re.compile(r"^Fixture [a-z0-9][a-z0-9 -]*$")
    author_pattern = re.compile(r"^@fixture_[a-z0-9_]+$")
    for fixture_id in EXPECTED_EVIDENCE_REVIEW_ROOTS:
        evidence = _read_json(FIXTURE_DIR / f"{fixture_id}.json")["payload"]["evidence"]
        for item in evidence:
            assert item["source_label"] is None or source_pattern.fullmatch(item["source_label"])
            assert item["author_label"] is None or author_pattern.fullmatch(item["author_label"])
            assert item["excerpt"] is None or item["excerpt"].startswith("Synthetic fixture ")
            assert item["metric_label"] is None or "synthetic" in item["metric_label"].lower()
            if item["published_at"] is not None:
                assert item["published_at"].endswith("Z")
                assert datetime.fromisoformat(item["published_at"]).tzinfo == UTC
            assert item["url"] is None or item["url"].split("/", 3)[2].endswith(".invalid")
            assert 0 <= item["geo_confidence"] <= 1
            assert item["claim_role"] in EVIDENCE_ROLES
            assert item["direction"] in EVIDENCE_DIRECTIONS
            assert item["availability"] in EVIDENCE_AVAILABILITY


def test_literal_schemas_and_enums_match_the_accepted_contract():
    for fixture_id in REQUIRED_FIXTURE_IDS:
        fixture = _read_json(FIXTURE_DIR / f"{fixture_id}.json")
        assert set(fixture) == ENVELOPE_FIELDS
        kind = fixture["fixture_kind"]
        payload = fixture["payload"]
        if kind == "dynamic_signal":
            assert set(payload) == DYNAMIC_PAYLOAD_FIELDS
            assert set(payload["signal"]) == DYNAMIC_SIGNAL_FIELDS
            assert all(set(item) == EVIDENCE_FIELDS for item in payload["evidence"])
        elif kind == "evidence_readiness":
            assert set(payload) == {
                "signal_id",
                "evidence_state",
                "qualifying_family_count",
                "direction_by_family",
                "evidence",
            }
            assert all(set(item) == EVIDENCE_FIELDS for item in payload["evidence"])
        elif kind == "lineage":
            assert set(payload) == {"edges"}
            assert all(set(edge) == LINEAGE_EDGE_FIELDS for edge in payload["edges"])
        elif kind == "source_lab":
            assert set(payload) == SOURCE_PAYLOAD_FIELDS
            assert all(set(item) == SOURCE_FIELDS for item in payload["sources"])
            assert all(item["route_role"] in SOURCE_ROUTE_ROLES for item in payload["sources"])
        elif kind == "evidence_plan":
            assert set(payload) == PLAN_FIELDS | {
                "human_review_required",
                "limitations",
                "missing_work",
                "export_allowed",
            }
        elif kind == "golden_task":
            expected = GOLDEN_FIELDS | ({"export_allowed"} if "election" in fixture_id else set())
            assert set(payload) == expected
            assert set(payload["request"]) == REQUEST_FIELDS
            assert set(payload["evidence_plan"]) == PLAN_FIELDS
            assert set(payload["answer"]) == ANSWER_FIELDS
            assert all(
                set(section) == ANSWER_SECTION_FIELDS for section in payload["answer"]["sections"]
            )
        elif kind == "cited_answer":
            assert set(payload) == CITED_FIELDS
            assert set(payload["request"]) == REQUEST_FIELDS
            assert set(payload["evidence_plan"]) == PLAN_FIELDS
            assert set(payload["answer"]) == ANSWER_FIELDS
        elif kind == "pulse_watched_signal_line":
            assert set(payload) == PULSE_PAYLOAD_FIELDS
        for item in payload.get("evidence", []):
            expected = (
                ANSWER_EVIDENCE_FIELDS
                if kind in {"golden_task", "cited_answer"}
                else EVIDENCE_FIELDS
            )
            assert set(item) == expected
            assert item["claim_role"] in EVIDENCE_ROLES
            assert item["direction"] in EVIDENCE_DIRECTIONS
            assert item["availability"] in EVIDENCE_AVAILABILITY


def test_nested_scope_envelopes_and_lenses_are_complete():
    for fixture_id in EXPECTED_INTENTS:
        payload = _golden(FIXTURE_DIR, fixture_id)
        request_scope = {field: payload["request"][field] for field in SCOPE_FIELDS}
        plan_scope = {field: payload["evidence_plan"][field] for field in SCOPE_FIELDS}
        assert plan_scope == request_scope
        assert set(payload["evidence_plan"]) == PLAN_FIELDS
        assert set(payload["evidence_plan"]["window"]) == {"start_date", "end_date"}
        for lens in payload["audience_lenses"]:
            assert set(lens) == LENS_FIELDS
        for lens in payload["evidence_plan"]["audience_lenses"]:
            assert set(lens) == LENS_FIELDS

    cited = _read_json(FIXTURE_DIR / "cited_answer_election.json")["payload"]
    root_scope = {field: cited[field] for field in SCOPE_FIELDS}
    plan_scope = {field: cited["evidence_plan"][field] for field in SCOPE_FIELDS}
    request_scope = {field: cited["request"][field] for field in SCOPE_FIELDS}
    assert plan_scope == request_scope == root_scope


def test_each_golden_task_has_distinct_directly_supporting_evidence_and_answer():
    evidence_hashes = set()
    for fixture_id, (evidence_phrase, answer_phrase) in TASK_SEMANTICS.items():
        payload = _golden(FIXTURE_DIR, fixture_id)
        evidence_text = json.dumps(payload["evidence"], ensure_ascii=False).lower()
        answer_text = json.dumps(payload["answer"], ensure_ascii=False).lower()
        assert evidence_phrase in evidence_text
        assert answer_phrase in answer_text
        evidence_hashes.add(hashlib.sha256(_canonical_bytes(payload["evidence"])).hexdigest())
    assert len(evidence_hashes) == 11


def test_each_golden_task_has_a_unique_concrete_visible_plan():
    decisions = set()
    requirement_sets = set()
    for fixture_id, expected in TASK_PLAN_EXPECTATIONS.items():
        payload = _golden(FIXTURE_DIR, fixture_id)
        request = payload["request"]
        plan = payload["evidence_plan"]
        assert request["decision"] == expected["decision"]
        assert plan["decision"] == expected["decision"]
        assert set(plan["evidence_requirements"]) == expected["requirements"]
        required, window_days = expected["historical"]
        assert plan["historical_comparison"] == {
            "required": required,
            "window_days": window_days,
        }
        decisions.add(expected["decision"])
        requirement_sets.add(tuple(sorted(expected["requirements"])))
    assert len(decisions) == 11
    assert len(requirement_sets) == 11


def test_each_task_section_claim_is_supported_by_its_exact_cited_evidence():
    for fixture_id, sections in TASK_SECTION_SUPPORT.items():
        payload = _golden(FIXTURE_DIR, fixture_id)
        evidence_by_label = {
            item["citation_label"]: json.dumps(item, ensure_ascii=False).lower()
            for item in payload["evidence"]
        }
        answer_sections = {item["section_id"]: item for item in payload["answer"]["sections"]}
        for section_id, (claim_terms, support_by_label) in sections.items():
            section = answer_sections[section_id]
            raw_body = section["body"]
            body = raw_body.lower()
            assert all(term in body for term in claim_terms)
            assert set(re.findall(r"\[E\d+\]", raw_body)) == set(support_by_label)
            assert set(section["evidence_ids"]) == {
                payload_id
                for label, payload_id in (
                    (item["citation_label"], item["evidence_id"]) for item in payload["evidence"]
                )
                if label in support_by_label
            }
            for label, support_term in support_by_label.items():
                assert support_term in evidence_by_label[label]


def test_history_task_uses_three_analogues_and_bounded_observed_recurrence():
    payload = _golden(FIXTURE_DIR, "golden_05_history")
    assert payload["request"]["question"] == (
        "What happened before, what is different now, and what usually follows?"
    )
    evidence_text = " ".join(item["excerpt"].lower() for item in payload["evidence"])
    assert all(
        phrase in evidence_text for phrase in ("analogue one", "analogue two", "analogue three")
    )
    answer_text = json.dumps(payload["answer"], ensure_ascii=False).lower()
    assert "two of three observed sequences" in answer_text
    assert "not a forecast" in answer_text
    assert "swap" not in answer_text


def test_election_readiness_and_sentence_support_derive_from_evidence():
    for fixture_id in ("golden_11_election_brand_role", "cited_answer_election"):
        payload = _read_json(FIXTURE_DIR / f"{fixture_id}.json")["payload"]
        family_directions: dict[str, set[str]] = {}
        for item in payload["evidence"]:
            if item["direction"] in {"rising", "stable", "declining", "conflicting"}:
                family_directions.setdefault(item["source_family"], set()).add(item["direction"])
        assert all(len(directions) == 1 for directions in family_directions.values())
        qualifying = [next(iter(directions)) for directions in family_directions.values()]
        if len(family_directions) >= 2 and len(set(qualifying)) == 1:
            derived = "ready"
        elif len(family_directions) >= 2:
            derived = "contradictory"
        elif len(family_directions) == 1:
            derived = "thin"
        else:
            derived = "unchecked"
        assert derived == "ready"
        assert payload["evidence_state"] == derived
        assert payload["evidence_plan"]["evidence_state"] == derived

        sections = {item["section_id"]: item for item in payload["answer"]["sections"]}
        evidence_by_label = {item["citation_label"]: item for item in payload["evidence"]}
        conversation = sections["conversation_themes"]["body"]
        stated_post_volume = int(re.search(r"\b(\d+) synthetic posts\b", conversation).group(1))
        cited_post_volume = sum(
            int(
                re.match(
                    r"(\d+) synthetic posts\b", evidence_by_label[label]["metric_label"]
                ).group(1)
            )
            for label in ("[E1]", "[E2]")
        )
        assert cited_post_volume == stated_post_volume == 24

        readiness = sections["evidence_readiness"]["body"]
        rise_labels = set(re.findall(r"\[E\d+\]", readiness))
        assert "rise" in readiness.lower()
        assert rise_labels == {"[E1]", "[E2]"}
        assert {evidence_by_label[label]["direction"] for label in rise_labels} == {"rising"}

        recommendation_sentences = re.split(r"(?<=[.!?])\s+", sections["recommendations"]["body"])
        assert set(re.findall(r"\[E\d+\]", recommendation_sentences[0])) == {
            "[E1]",
            "[E2]",
        }
        assert set(re.findall(r"\[E\d+\]", recommendation_sentences[1])) == {"[E3]"}
        assert set(re.findall(r"\[E\d+\]", sections["evidence_readiness"]["body"])) == {
            "[E1]",
            "[E2]",
        }
        assert set(re.findall(r"\[E\d+\]", sections["safety_flags"]["body"])) == {
            "[E4]",
            "[E5]",
        }
        sections_using_e4 = {
            section_id for section_id, section in sections.items() if "[E4]" in section["body"]
        }
        assert sections_using_e4 == {"safety_flags"}


@pytest.mark.parametrize("fixture_id", sorted(TASK_SEMANTICS))
def test_task_specific_evidence_and_answer_swaps_are_rejected(tmp_path: Path, fixture_id: str):
    fixture_ids = sorted(TASK_SEMANTICS)
    donor_id = fixture_ids[(fixture_ids.index(fixture_id) + 1) % len(fixture_ids)]

    evidence_directory = _copy_package(tmp_path / "evidence")
    donor = _golden(evidence_directory, donor_id)
    _mutate_fixture(
        evidence_directory,
        fixture_id,
        lambda fixture: fixture["payload"].__setitem__("evidence", donor["evidence"]),
    )
    with pytest.raises(_module().FixtureValidationError, match="golden_semantic_evidence_invalid"):
        _module().load_fixture_package(evidence_directory)

    answer_directory = _copy_package(tmp_path / "answer")
    donor = _golden(answer_directory, donor_id)
    _mutate_fixture(
        answer_directory,
        fixture_id,
        lambda fixture: fixture["payload"].__setitem__("answer", donor["answer"]),
    )
    with pytest.raises(_module().FixtureValidationError, match="golden_semantic_answer_invalid"):
        _module().load_fixture_package(answer_directory)


def test_reviewed_null_author_change_is_rejected_after_digest_refresh(tmp_path: Path):
    directory = _copy_package(tmp_path)
    _mutate_fixture(
        directory,
        "golden_01_emerging_without_keyword",
        lambda fixture: fixture["payload"]["evidence"][0].__setitem__("author_label", None),
    )
    with pytest.raises(_module().FixtureValidationError, match="evidence_review_root_mismatch"):
        _module().load_fixture_package(directory)


def test_evidence_readiness_fixtures_encode_the_four_accepted_states():
    expected = {
        "evidence_ready": ("ready", 2, {"rising"}),
        "evidence_thin": ("thin", 1, {"rising"}),
        "evidence_contradictory": ("contradictory", 2, {"rising", "declining"}),
        "evidence_unchecked": ("unchecked", 0, set()),
    }
    for fixture_id, (state, family_count, directions) in expected.items():
        payload = _read_json(FIXTURE_DIR / f"{fixture_id}.json")["payload"]
        assert payload["evidence_state"] == state
        assert payload["qualifying_family_count"] == family_count
        assert set(payload["direction_by_family"].values()) == directions
        assert len(payload["evidence"]) == family_count
        assert all("citation_label" not in item for item in payload["evidence"])


def test_lineage_fixtures_encode_many_to_many_edges_with_accepted_relations():
    merge = _read_json(FIXTURE_DIR / "lineage_merge.json")["payload"]["edges"]
    split = _read_json(FIXTURE_DIR / "lineage_split.json")["payload"]["edges"]

    assert len(merge) == 2
    assert {edge["relation"] for edge in merge} == {"merges_into"}
    assert len({edge["from_signal_id"] for edge in merge}) == 2
    assert len({edge["to_signal_id"] for edge in merge}) == 1
    assert len(split) == 2
    assert {edge["relation"] for edge in split} == {"splits_into"}
    assert len({edge["from_signal_id"] for edge in split}) == 1
    assert len({edge["to_signal_id"] for edge in split}) == 2
    assert all(FULL_ID.fullmatch(edge["from_signal_id"]) for edge in merge + split)
    assert all(FULL_ID.fullmatch(edge["to_signal_id"]) for edge in merge + split)


def test_source_lab_contains_all_statuses_with_exact_metric_nullability():
    payload = _read_json(FIXTURE_DIR / "source_lab_all_statuses.json")["payload"]
    sources = {source["status"]: source for source in payload["sources"]}
    observed = {
        "calls",
        "credits",
        "rows",
        "integrity",
        "geo_precision",
        "unique_lift",
        "last_success_at",
    }
    assert payload["fixture_source"] == "synthetic_shape_only"
    assert payload["synthetic_route_count"] == 6
    assert set(sources) == {
        "active",
        "pilot",
        "available_unwired",
        "blocked",
        "permanently_rejected",
        "inventory_only",
    }
    for status in ("active", "pilot"):
        assert all(sources[status][field] is not None for field in observed - {"unique_lift"})
        assert sources[status]["blocking_reason"] is None
        assert sources[status]["kill_test_result"] is None
    for status in ("available_unwired", "blocked", "inventory_only"):
        assert all(sources[status][field] is None for field in observed)
    assert sources["inventory_only"]["route_role"] == "inventory"
    assert sources["inventory_only"]["official_price_components"] == []
    assert sources["available_unwired"]["official_price_components"]
    assert sources["blocked"]["blocking_reason"]
    killed = sources["permanently_rejected"]
    assert (killed["rows"], killed["unique_lift"]) == (0, 0.0)
    assert all(killed[field] is None for field in observed - {"rows", "unique_lift"})
    assert killed["blocking_reason"] == killed["kill_test_result"] == "zero_unique_observations"
    assert killed["review_date"] is None


def test_golden_tasks_expose_the_complete_consumer_shape_and_expected_routing():
    for fixture_id, expected_intent in EXPECTED_INTENTS.items():
        payload = _golden(FIXTURE_DIR, fixture_id)
        assert {
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
        } <= set(payload)
        assert payload["expected_intent"] == expected_intent
        assert payload["evidence_plan"]["intent"] == expected_intent
        assert payload["answer"]["sections"]
        assert payload["evidence"]
        assert payload["limitations"]
        assert set(payload["budget"]) == {
            "input_ceiling",
            "output_ceiling",
            "input_used",
            "output_used",
            "remaining_max_output_tokens",
            "exhausted",
            "metering_status",
        }


def test_every_substantive_answer_sentence_resolves_to_local_evidence():
    for fixture_id in EXPECTED_INTENTS:
        payload = _golden(FIXTURE_DIR, fixture_id)
        evidence_ids = {item["evidence_id"] for item in payload["evidence"]}
        citation_ids = {item["citation_label"]: item["evidence_id"] for item in payload["evidence"]}
        for section in payload["answer"]["sections"]:
            if section["section_id"] in {"scope", "limitations"}:
                continue
            assert section["evidence_ids"]
            assert set(section["evidence_ids"]) <= evidence_ids
            for sentence in re.split(r"(?<=[.!?])\s+", section["body"]):
                if not sentence.strip():
                    continue
                labels = set(re.findall(r"\[E\d+\]", sentence))
                assert labels
                assert labels <= set(citation_ids)
                assert {citation_ids[label] for label in labels} <= set(section["evidence_ids"])


def test_election_and_audience_fixtures_are_fail_closed():
    election_ids = {
        "evidence_plan_election",
        "cited_answer_election",
        "golden_11_election_brand_role",
    }
    for fixture_id in election_ids:
        payload = _read_json(FIXTURE_DIR / f"{fixture_id}.json")["payload"]
        assert payload["human_review_required"] is True
        assert payload["export_allowed"] is False
        text = json.dumps(payload, ensure_ascii=False).lower()
        assert "social conversation is not representative polling" in text
        assert "export_approved" not in text
        assert "caused" not in text
        assert "will definitely" not in text

    required_election_sections = {
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
    for fixture_id in ("cited_answer_election", "golden_11_election_brand_role"):
        payload = _read_json(FIXTURE_DIR / f"{fixture_id}.json")["payload"]
        assert {
            section["section_id"] for section in payload["answer"]["sections"]
        } == required_election_sections
        answer_text = json.dumps(payload["answer"], ensure_ascii=False).lower()
        for phrase in (
            "synthetic posts",
            "18 to 25 august 2026",
            "current fixture brand behavior",
            "unmet",
            "brand safety",
            "misinformation",
            "polarization",
            "social conversation is not representative polling",
        ):
            assert phrase in answer_text

    audience = _golden(FIXTURE_DIR, "golden_07_audience_lens")["audience_lenses"]
    assert audience
    for lens in audience:
        assert lens["basis"] in {"inferred", "measured"}
        assert lens["source"]
        assert set(lens["window"]) == {"start_date", "end_date"}
        assert 0 <= lens["confidence"] <= 1
        assert not re.search(r"\b(age|aged|gender|women|men)\b", json.dumps(lens).lower())


def test_all_payload_identifiers_urls_and_fixture_identities_are_synthetic():
    forbidden = re.compile(
        r"brand24|socialcrawl|bigquery|vertex|graph score|signal index|trend_scores|"
        r"signal_candidates_v2|signal_evidence_v2|@gmail|@ogilvy|@wpp",
        re.IGNORECASE,
    )
    for fixture_id in REQUIRED_FIXTURE_IDS:
        fixture = _read_json(FIXTURE_DIR / f"{fixture_id}.json")
        text = json.dumps(fixture, ensure_ascii=False)
        assert not forbidden.search(text)
        assert not re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", text)
        for value in re.findall(r"https?://[^\s\"}]+", text):
            assert ".invalid" in value
        for identifier in re.findall(r"\b(?:sig|ev|endpoint|srcperf)_(?!id\b)[0-9a-z]+\b", text):
            assert FULL_ID.fullmatch(identifier)


def _change_manifest_entry_id(directory: Path) -> None:
    manifest = _read_json(directory / "manifest.json")
    manifest["fixtures"][0]["fixture_id"] = manifest["fixtures"][1]["fixture_id"]
    manifest["manifest_sha256"] = _manifest_digest(manifest["fixtures"])
    _write_json(directory / "manifest.json", manifest)


def _make_unsorted(directory: Path) -> None:
    manifest = _read_json(directory / "manifest.json")
    manifest["fixtures"][0], manifest["fixtures"][1] = (
        manifest["fixtures"][1],
        manifest["fixtures"][0],
    )
    manifest["manifest_sha256"] = _manifest_digest(manifest["fixtures"])
    _write_json(directory / "manifest.json", manifest)


def _make_noncanonical(directory: Path) -> None:
    fixture_id = "evidence_ready"
    path = directory / f"{fixture_id}.json"
    path.write_text(json.dumps(_read_json(path), ensure_ascii=False), encoding="utf-8")
    _refresh_manifest(directory, fixture_id)


def _break_manifest_digest(directory: Path) -> None:
    manifest = _read_json(directory / "manifest.json")
    manifest["manifest_sha256"] = "0" * 64
    _write_json(directory / "manifest.json", manifest)


def _filename_mismatch(directory: Path) -> None:
    _mutate_fixture(
        directory,
        "dynamic_signal_ready",
        lambda fixture: fixture.__setitem__("fixture_id", "evidence_ready"),
    )


def _broken_citation(fixture: dict) -> None:
    fixture["payload"]["answer"]["sections"][1]["evidence_ids"] = ["ev_" + "f" * 64]


def _answer_wording(wording: str) -> Callable[[dict], None]:
    def mutate(fixture: dict) -> None:
        fixture["payload"]["answer"]["sections"][1]["body"] = wording + " [E1]."

    return mutate


Mutation = tuple[str, str, Callable[[Path], None]]


MUTATIONS: tuple[Mutation, ...] = (
    ("unknown_file", "unknown_files", lambda directory: _write_json(directory / "extra.json", {})),
    (
        "missing_file",
        "missing_files",
        lambda directory: (directory / "evidence_ready.json").unlink(),
    ),
    ("duplicate_id", "duplicate_fixture_id", _change_manifest_entry_id),
    ("filename_mismatch", "fixture_id_filename_mismatch", _filename_mismatch),
    ("unsorted_manifest", "manifest_not_sorted", _make_unsorted),
    ("noncanonical_bytes", "noncanonical_json", _make_noncanonical),
    (
        "fixture_digest",
        "fixture_digest_mismatch",
        lambda directory: _mutate_fixture(
            directory,
            "evidence_ready",
            lambda fixture: fixture["payload"].__setitem__("qualifying_family_count", 3),
            refresh=False,
        ),
    ),
    ("manifest_digest", "manifest_digest_mismatch", _break_manifest_digest),
    (
        "unapproved_kind",
        "fixture_kind_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "evidence_ready",
            lambda fixture: fixture.__setitem__("fixture_kind", "other"),
        ),
    ),
    (
        "contract_version",
        "contract_version_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "evidence_ready",
            lambda fixture: fixture.__setitem__("contract_version", "3.0.0"),
        ),
    ),
    (
        "sanitization",
        "sanitized_required",
        lambda directory: _mutate_fixture(
            directory, "evidence_ready", lambda fixture: fixture.__setitem__("sanitized", False)
        ),
    ),
    (
        "timestamp",
        "frozen_at_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "evidence_ready",
            lambda fixture: fixture.__setitem__("frozen_at", "2026-08-25"),
        ),
    ),
    (
        "real_url",
        "url_must_use_invalid_tld",
        lambda directory: _mutate_fixture(
            directory,
            "dynamic_signal_ready",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(
                "url", "https://example.com/post"
            ),
        ),
    ),
    (
        "pii",
        "pii_pattern_forbidden",
        lambda directory: _mutate_fixture(
            directory,
            "dynamic_signal_ready",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(
                "excerpt", "Contact person@example.invalid"
            ),
        ),
    ),
    (
        "identity",
        "identity_must_be_synthetic",
        lambda directory: _mutate_fixture(
            directory,
            "dynamic_signal_ready",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(
                "author_label", "@real_person"
            ),
        ),
    ),
    (
        "product_term",
        "product_term_forbidden",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["request"].__setitem__(
                "question", "What does Brand24 show?"
            ),
        ),
    ),
    (
        "truncated_identifier",
        "identifier_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "dynamic_signal_ready",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__("evidence_id", "ev_abc"),
        ),
    ),
    (
        "enum",
        "evidence_state_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "evidence_ready",
            lambda fixture: fixture["payload"].__setitem__("evidence_state", "readyish"),
        ),
    ),
    (
        "source_nullability",
        "source_status_nullability_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "source_lab_all_statuses",
            lambda fixture: next(
                item
                for item in fixture["payload"]["sources"]
                if item["status"] == "available_unwired"
            ).__setitem__("calls", 1),
        ),
    ),
    (
        "citation",
        "citation_unresolved",
        lambda directory: _mutate_fixture(
            directory, "golden_01_emerging_without_keyword", _broken_citation
        ),
    ),
    (
        "causal",
        "causal_claim_forbidden",
        lambda directory: _mutate_fixture(
            directory,
            "dynamic_signal_ready",
            lambda fixture: fixture["payload"]["signal"].__setitem__(
                "why_now", "Synthetic fixture discussion caused willingness to vote."
            ),
        ),
    ),
    (
        "polling",
        "polling_claim_forbidden",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            _answer_wording("This is representative polling"),
        ),
    ),
    (
        "demographic",
        "demographic_claim_forbidden",
        lambda directory: _mutate_fixture(
            directory, "golden_07_audience_lens", _answer_wording("Women aged 18 prefer this")
        ),
    ),
    (
        "prediction",
        "prediction_claim_forbidden",
        lambda directory: _mutate_fixture(
            directory, "golden_05_history", _answer_wording("This will definitely happen next")
        ),
    ),
    (
        "election_review",
        "election_review_required",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: fixture["payload"].__setitem__("human_review_required", False),
        ),
    ),
    (
        "custom_routing",
        "custom_intent_required",
        lambda directory: _mutate_fixture(
            directory,
            "golden_10_custom",
            lambda fixture: fixture["payload"]["evidence_plan"].__setitem__("intent", "landscape"),
        ),
    ),
)


def _set_section_body(fixture: dict, section_id: str, body: object) -> None:
    section = next(
        item
        for item in fixture["payload"]["answer"]["sections"]
        if item["section_id"] == section_id
    )
    section["body"] = body


def _append_uncited_sentence(fixture: dict) -> None:
    section = next(
        item
        for item in fixture["payload"]["answer"]["sections"]
        if item["section_id"] not in {"scope", "limitations"}
    )
    section["body"] += " A second synthetic conclusion has no citation."


def _remove_section(fixture: dict, section_id: str) -> None:
    fixture["payload"]["answer"]["sections"] = [
        item
        for item in fixture["payload"]["answer"]["sections"]
        if item["section_id"] != section_id
    ]


def _swap_mutation(target: str, donor: str, part: str, rule: str) -> Mutation:
    def mutate(directory: Path) -> None:
        donor_value = _golden(directory, donor)[part]
        _mutate_fixture(
            directory,
            target,
            lambda fixture: fixture["payload"].__setitem__(part, donor_value),
        )

    return (f"swap_{part}_{target}", rule, mutate)


def _evidence_field_mutation(field: str, value: object, rule: str) -> Mutation:
    return (
        f"reviewed_evidence_field_{field}",
        rule,
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(field, value),
        ),
    )


def _change_top_level_golden_field(fixture: dict, field: str) -> None:
    payload = fixture["payload"]
    if field == "request":
        payload["request"]["question"] = "Which bounded synthetic move merits review?"
    elif field == "evidence_plan":
        payload["evidence_plan"]["plan_id"] = "ep_fixture_review"
    elif field == "answer":
        payload["answer"]["title"] = "Reviewed synthetic fixture intelligence brief"
    elif field == "evidence":
        payload["evidence"][0]["platform"] = "fixture_platform_review"
    elif field == "evidence_state":
        payload["evidence_state"] = "thin"
        payload["evidence_plan"]["evidence_state"] = "thin"
    elif field == "limitations":
        payload["limitations"][0] = "Synthetic fixture evidence has a reviewed limitation."
    elif field == "missing_work":
        payload["missing_work"] = ["Review one synthetic fixture source."]
    elif field == "budget":
        payload["budget"]["input_used"] += 1
    elif field == "expected_intent":
        payload["expected_intent"] = "custom"
        payload["evidence_plan"]["intent"] = "custom"
    elif field == "contradictions":
        payload["contradictions"] = ["One synthetic fixture contradiction remains."]
    elif field == "human_review_required":
        payload["human_review_required"] = True
    elif field == "audience_lenses":
        lens = {
            "basis": "inferred",
            "claim": "The reviewed synthetic fixture lens reflects practical cues.",
            "confidence": 0.5,
            "lens_id": "fixture_review_lens",
            "source": "Synthetic fixture evidence",
            "window": dict(payload["request"]["window"]),
        }
        payload["audience_lenses"] = [lens]
        payload["evidence_plan"]["audience_lenses"] = [dict(lens)]
        payload["request"]["audience_lens_ids"] = ["fixture_review_lens"]
        payload["evidence_plan"]["audience_lens_ids"] = ["fixture_review_lens"]
    elif field == "export_allowed":
        payload["export_allowed"] = True
    else:
        raise AssertionError(field)


EVIDENCE_FIELD_MUTATIONS: tuple[Mutation, ...] = (
    _evidence_field_mutation("evidence_id", "ev_" + "f" * 64, "citation_unresolved"),
    _evidence_field_mutation("signal_id", "sig_" + "f" * 64, "evidence_review_root_mismatch"),
    _evidence_field_mutation("row_id", "fixture_review_row", "evidence_review_root_mismatch"),
    _evidence_field_mutation("source_family", "audio", "evidence_review_root_mismatch"),
    _evidence_field_mutation(
        "platform", "fixture_platform_review", "evidence_review_root_mismatch"
    ),
    _evidence_field_mutation(
        "source_label", "Fixture source review", "evidence_review_root_mismatch"
    ),
    _evidence_field_mutation(
        "author_label", "@fixture_review_author", "evidence_review_root_mismatch"
    ),
    _evidence_field_mutation(
        "excerpt",
        "Synthetic fixture reviewed unprompted phrase clusters formed around repair circles.",
        "evidence_review_root_mismatch",
    ),
    _evidence_field_mutation("metric_label", "19 synthetic posts", "evidence_review_root_mismatch"),
    _evidence_field_mutation(
        "url", "https://evidence.invalid/review", "evidence_review_root_mismatch"
    ),
    _evidence_field_mutation(
        "published_at", "2026-08-24T10:00:00Z", "evidence_review_root_mismatch"
    ),
    _evidence_field_mutation("claim_role", "context", "evidence_review_root_mismatch"),
    _evidence_field_mutation("direction", "stable", "evidence_review_root_mismatch"),
    _evidence_field_mutation("geo_confidence", 0.92, "evidence_review_root_mismatch"),
    _evidence_field_mutation("availability", "visible", "evidence_availability_invalid"),
    _evidence_field_mutation("citation_label", "[E9]", "golden_section_support_invalid"),
)


REVIEW_MUTATIONS: tuple[Mutation, ...] = (
    (
        "custom_question_named_template",
        "custom_question_required",
        lambda directory: _mutate_fixture(
            directory,
            "golden_10_custom",
            lambda fixture: fixture["payload"]["request"].__setitem__(
                "question", "What is newly emerging without a supplied keyword?"
            ),
        ),
    ),
    (
        "empty_plan_source_families",
        "evidence_plan_source_families_required",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence_plan"].__setitem__("source_families", []),
        ),
    ),
    (
        "audience_confidence_answer_drift",
        "audience_confidence_mismatch",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: (
                fixture["payload"]["audience_lenses"][0].__setitem__("confidence", 0.2),
                fixture["payload"]["evidence_plan"]["audience_lenses"][0].__setitem__(
                    "confidence", 0.2
                ),
            ),
        ),
    ),
    (
        "empty_golden_limitations",
        "golden_limitations_required",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"].__setitem__("limitations", []),
        ),
    ),
    (
        "request_plan_window_drift",
        "scope_envelope_mismatch",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence_plan"]["window"].__setitem__(
                "start_date", "2026-08-19"
            ),
        ),
    ),
    (
        "request_plan_output_form_drift",
        "scope_envelope_mismatch",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence_plan"].__setitem__(
                "output_form", "comparison"
            ),
        ),
    ),
    (
        "lens_id_scope_drift",
        "scope_envelope_mismatch",
        lambda directory: _mutate_fixture(
            directory,
            "golden_07_audience_lens",
            lambda fixture: (
                fixture["payload"]["request"].__setitem__(
                    "audience_lens_ids", ["fixture_other_lens"]
                ),
                fixture["payload"]["evidence_plan"].__setitem__(
                    "audience_lens_ids", ["fixture_other_lens"]
                ),
            ),
        ),
    ),
    (
        "lens_window_scope_drift",
        "scope_envelope_mismatch",
        lambda directory: _mutate_fixture(
            directory,
            "golden_07_audience_lens",
            lambda fixture: (
                fixture["payload"]["audience_lenses"][0]["window"].__setitem__(
                    "start_date", "2026-08-19"
                ),
                fixture["payload"]["evidence_plan"]["audience_lenses"][0]["window"].__setitem__(
                    "start_date", "2026-08-19"
                ),
            ),
        ),
    ),
    (
        "answer_scope_drift",
        "scope_envelope_mismatch",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["answer"].__setitem__(
                "scope", "Reviewed synthetic fixture scope"
            ),
        ),
    ),
    (
        "cited_answer_created_at_drift",
        "golden_semantic_root_mismatch",
        lambda directory: _mutate_fixture(
            directory,
            "cited_answer_election",
            lambda fixture: fixture["payload"].__setitem__("created_at", "2026-08-25T07:11:00Z"),
        ),
    ),
    (
        "election_metric_999",
        "election_metric_total_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(
                "metric_label", "999 synthetic posts, 18 to 25 August 2026"
            ),
        ),
    ),
    (
        "election_directions_stable",
        "election_direction_wording_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: (
                fixture["payload"]["evidence"][0].__setitem__("direction", "stable"),
                fixture["payload"]["evidence"][1].__setitem__("direction", "stable"),
            ),
        ),
    ),
    (
        "source_label_fixture_google",
        "identity_must_be_synthetic",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(
                "source_label", "Fixture Google"
            ),
        ),
    ),
    (
        "published_at_invalid",
        "evidence_published_at_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(
                "published_at", "not-a-timestamp"
            ),
        ),
    ),
    (
        "second_sentence_uncited",
        "sentence_citation_required",
        lambda directory: _mutate_fixture(
            directory, "golden_01_emerging_without_keyword", _append_uncited_sentence
        ),
    ),
    (
        "missing_election_safety_section",
        "election_sections_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: _remove_section(fixture, "safety_flags"),
        ),
    ),
    (
        "export_claim",
        "election_export_claim_forbidden",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: _set_section_body(
                fixture, "recommendations", "Export is approved [E1]."
            ),
        ),
    ),
    (
        "causal_drove",
        "causal_claim_forbidden",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: _set_section_body(
                fixture, "associated_factors", "Fixture information drove willingness to vote [E1]."
            ),
        ),
    ),
    (
        "polling_represents_opinion",
        "polling_claim_forbidden",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: _set_section_body(
                fixture, "conversation_themes", "These posts represent voter opinion [E1]."
            ),
        ),
    ),
    (
        "demographic_words",
        "demographic_claim_forbidden",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: _set_section_body(
                fixture,
                "audience_lenses",
                "Male voters aged eighteen to twenty four prefer this [E1].",
            ),
        ),
    ),
    (
        "prediction_will_return",
        "prediction_claim_forbidden",
        lambda directory: _mutate_fixture(
            directory,
            "golden_05_history",
            lambda fixture: _set_section_body(
                fixture, "finding", "This pattern will return next month [E1]."
            ),
        ),
    ),
    (
        "real_full_name",
        "identity_must_be_synthetic",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(
                "excerpt",
                "Synthetic fixture unprompted phrase clusters formed around repair circles with Nelson Mandela.",
            ),
        ),
    ),
    (
        "cyrillic_confusable_product",
        "product_term_forbidden",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["request"].__setitem__(
                "question", "What does \u0405ocialCrawl show?"
            ),
        ),
    ),
    (
        "fullwidth_product",
        "product_term_forbidden",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["request"].__setitem__(
                "question", "What does \uff33ocialCrawl show?"
            ),
        ),
    ),
    (
        "mixed_script_identity",
        "unicode_mixed_script_forbidden",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(
                "author_label", "@fixture_\u0430uthor"
            ),
        ),
    ),
    (
        "availability_enum",
        "evidence_availability_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(
                "availability", "visible"
            ),
        ),
    ),
    (
        "claim_role_enum",
        "evidence_claim_role_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__("claim_role", "proof"),
        ),
    ),
    (
        "direction_enum",
        "evidence_direction_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__("direction", "falling"),
        ),
    ),
    (
        "extra_payload_field",
        "golden_fields_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"].__setitem__("extra", True),
        ),
    ),
    (
        "extra_readiness_evidence_field",
        "evidence_fields_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "evidence_ready",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__("citation_label", "[E1]"),
        ),
    ),
    (
        "budget_exhausted_with_remaining",
        "budget_exhaustion_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["budget"].__setitem__("exhausted", True),
        ),
    ),
    (
        "budget_over_ceiling",
        "budget_usage_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["budget"].__setitem__("output_used", 4001),
        ),
    ),
    (
        "budget_wrong_type",
        "budget_type_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["budget"].__setitem__("input_used", "1800"),
        ),
    ),
    (
        "failed_with_answer",
        "lifecycle_state_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "cited_answer_election",
            lambda fixture: fixture["payload"].__setitem__("status", "failed"),
        ),
    ),
    (
        "partial_without_missing_work",
        "lifecycle_missing_work_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "cited_answer_election",
            lambda fixture: fixture["payload"].update({"status": "partial", "missing_work": []}),
        ),
    ),
    (
        "evidence_plan_no_window",
        "evidence_plan_fields_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "evidence_plan_election",
            lambda fixture: fixture["payload"].pop("window"),
        ),
    ),
    (
        "evidence_plan_reversed_window",
        "evidence_plan_window_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "evidence_plan_election",
            lambda fixture: fixture["payload"]["window"].update(
                {"start_date": "2026-08-26", "end_date": "2026-08-25"}
            ),
        ),
    ),
    (
        "source_route_role",
        "source_route_role_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "source_lab_all_statuses",
            lambda fixture: fixture["payload"]["sources"][0].__setitem__("route_role", "other"),
        ),
    ),
    (
        "answer_body_null",
        "answer_body_type_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: _set_section_body(fixture, "finding", None),
        ),
    ),
    (
        "evidence_url_wrong_type",
        "evidence_url_type_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__("url", 7),
        ),
    ),
    (
        "scope_drift",
        "scope_envelope_mismatch",
        lambda directory: _mutate_fixture(
            directory,
            "cited_answer_election",
            lambda fixture: fixture["payload"]["evidence_plan"].__setitem__("market_scope", ["ng"]),
        ),
    ),
    (
        "incomplete_lens",
        "audience_lens_fields_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_07_audience_lens",
            lambda fixture: fixture["payload"]["audience_lenses"][0].pop("source"),
        ),
    ),
    (
        "export_allowed_true",
        "election_export_block_required",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: fixture["payload"].__setitem__("export_allowed", True),
        ),
    ),
    (
        "synthetic_prefix_organization",
        "identity_must_be_synthetic",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(
                "excerpt",
                "Synthetic fixture unprompted phrase clusters formed around repair circles with Coca-Cola.",
            ),
        ),
    ),
    (
        "synthetic_prefix_formatted_phone",
        "pii_pattern_forbidden",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(
                "excerpt",
                "Synthetic fixture unprompted phrase clusters formed around repair circles. Call +27 82 123 4567.",
            ),
        ),
    ),
    (
        "unsupported_cooking_recommendation",
        "golden_section_claim_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: _set_section_body(
                fixture, "recommendations", "Launch a synthetic cooking event [E1]."
            ),
        ),
    ),
    (
        "synthetic_prefix_google",
        "identity_must_be_synthetic",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(
                "excerpt",
                "Synthetic fixture Google joined unprompted phrase clusters formed around repair circles.",
            ),
        ),
    ),
    (
        "synthetic_prefix_madonna",
        "identity_must_be_synthetic",
        lambda directory: _mutate_fixture(
            directory,
            "golden_01_emerging_without_keyword",
            lambda fixture: fixture["payload"]["evidence"][0].__setitem__(
                "excerpt",
                "Synthetic fixture Madonna joined unprompted phrase clusters formed around repair circles.",
            ),
        ),
    ),
    (
        "piggyback_cooking_recommendation",
        "golden_semantic_root_mismatch",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: _set_section_body(
                fixture,
                "recommendations",
                "Provide neutral voting logistics and launch a synthetic cooking event in any fixture response [E1] [E2]. Avoid partisan endorsement [E3].",
            ),
        ),
    ),
    (
        "history_absent_swap_detail",
        "golden_semantic_root_mismatch",
        lambda directory: _mutate_fixture(
            directory,
            "golden_05_history",
            lambda fixture: _set_section_body(
                fixture,
                "finding",
                next(
                    item["body"]
                    for item in fixture["payload"]["answer"]["sections"]
                    if item["section_id"] == "finding"
                ).replace("follow-on workshops", "swap workshops"),
            ),
        ),
    ),
    (
        "election_duplicate_qualifying_family",
        "election_readiness_invalid",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: fixture["payload"]["evidence"][1].__setitem__(
                "source_family", fixture["payload"]["evidence"][0]["source_family"]
            ),
        ),
    ),
    (
        "election_conflicting_direction_within_family",
        "evidence_family_direction_conflict",
        lambda directory: _mutate_fixture(
            directory,
            "golden_11_election_brand_role",
            lambda fixture: fixture["payload"]["evidence"][1].update(
                {
                    "source_family": fixture["payload"]["evidence"][0]["source_family"],
                    "direction": "declining",
                }
            ),
        ),
    ),
)

_SWAP_IDS = sorted(TASK_SEMANTICS)
SWAP_MUTATIONS: tuple[Mutation, ...] = tuple(
    mutation
    for index, fixture_id in enumerate(_SWAP_IDS)
    for mutation in (
        _swap_mutation(
            fixture_id,
            _SWAP_IDS[(index + 1) % len(_SWAP_IDS)],
            "evidence",
            "golden_semantic_evidence_invalid",
        ),
        _swap_mutation(
            fixture_id,
            _SWAP_IDS[(index + 1) % len(_SWAP_IDS)],
            "answer",
            "golden_semantic_answer_invalid",
        ),
    )
)
ALL_MUTATIONS = MUTATIONS + REVIEW_MUTATIONS + EVIDENCE_FIELD_MUTATIONS + SWAP_MUTATIONS


@pytest.mark.parametrize("field", sorted(GOLDEN_FIELDS | {"export_allowed"}))
@pytest.mark.parametrize("operation", ["remove", "change"])
def test_every_top_level_golden_payload_field_mutation_fails_after_digest_refresh(
    tmp_path: Path, field: str, operation: str
):
    directory = _copy_package(tmp_path)
    fixture_id = (
        "golden_11_election_brand_role"
        if field == "export_allowed"
        else "golden_01_emerging_without_keyword"
    )

    def mutate(fixture: dict) -> None:
        if operation == "remove":
            fixture["payload"].pop(field)
        else:
            _change_top_level_golden_field(fixture, field)

    _mutate_fixture(directory, fixture_id, mutate)

    with pytest.raises(_module().FixtureValidationError):
        _module().load_fixture_package(directory)


@pytest.mark.parametrize(
    ("name", "rule", "mutate"), ALL_MUTATIONS, ids=[item[0] for item in ALL_MUTATIONS]
)
def test_validator_fails_closed_on_named_defects(
    tmp_path: Path,
    name: str,
    rule: str,
    mutate: Callable[[Path], None],
):
    directory = _copy_package(tmp_path)
    mutate(directory)

    with pytest.raises(_module().FixtureValidationError) as caught:
        _module().load_fixture_package(directory)

    message = str(caught.value)
    assert rule in message
    assert len(message) < 240
    assert "payload" not in message.lower()

    completed = subprocess.run(
        [sys.executable, "scripts/verify_open_intelligence_fixtures.py", str(directory)],
        capture_output=True,
        check=False,
        encoding="utf-8",
    )
    assert completed.returncode == 1
    assert rule in completed.stdout
    assert len(completed.stdout) < 280
    assert "payload" not in completed.stdout.lower()
    assert completed.stderr == ""


def test_mutation_harness_restores_every_byte_after_each_case(tmp_path: Path):
    directory = _copy_package(tmp_path)
    original = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}

    for _name, rule, mutate in ALL_MUTATIONS:
        try:
            mutate(directory)
            with pytest.raises(_module().FixtureValidationError, match=rule):
                _module().load_fixture_package(directory)
        finally:
            for path in list(directory.iterdir()):
                if path.is_file() and path.name not in original:
                    path.unlink()
            for name, content in original.items():
                (directory / name).write_bytes(content)
        assert {
            path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()
        } == original


def test_cited_answer_error_names_its_own_fixture(tmp_path: Path):
    directory = _copy_package(tmp_path)
    _mutate_fixture(
        directory,
        "cited_answer_election",
        lambda fixture: fixture["payload"].__setitem__("human_review_required", False),
    )

    with pytest.raises(_module().FixtureValidationError) as caught:
        _module().load_fixture_package(directory)

    assert str(caught.value) == "cited_answer_election: election_review_required"


def test_cli_sanitizes_unexpected_local_errors(monkeypatch, capsys):
    verifier = importlib.import_module("scripts.verify_open_intelligence_fixtures")
    monkeypatch.setattr(
        verifier,
        "load_fixture_package",
        lambda _directory: (_ for _ in ()).throw(RuntimeError("private local path")),
    )

    assert verifier.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == "FAIL verifier: unexpected_local_error\n"
    assert captured.err == ""
