import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from src.contracts import open_intelligence_budget as budget
from src.contracts import open_intelligence_fixtures as fixtures
from src.contracts.open_intelligence import (
    ASYNC_STATES,
    CONTRACT_VERSION,
    EVIDENCE_STATES,
    IDENTITY_HEALTH_STATES,
    LINEAGE_RELATIONS,
    MONTHLY_OPTIONAL_CREDIT_CAP,
    OUTCOME_STATES,
    POST_EVIDENCE_STATES,
    PROFILE_EVIDENCE_STATES,
    QUESTION_INTENTS,
    RUNWAY_FLOOR_DAYS,
    TOKEN_CEILINGS,
    encode_identifier_part,
    resolve_client_scope,
)

DOC = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "open_intelligence_v2.md"


def _doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def _doc_enum_after(lead: str) -> tuple[str, ...]:
    # The backticked values, in doc order, of the contract sentence that opens with lead.
    match = re.search(rf"{re.escape(lead)}([^.]*)\.", _doc_text())
    assert match, lead
    return tuple(re.findall(r"`([a-z_]+)`", match.group(1)))


def _doc_identity_health_states() -> tuple[str, ...]:
    # The first column of the identity health table, in doc order.
    lines = _doc_text().splitlines()
    start = lines.index("The identity health enum is exact within A2:")
    states: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("| `"):
            states.append(line.split("`")[1])
        elif states:
            break
    return tuple(states)


def _doc_runway_floor_days() -> int:
    match = re.search(r"runway falls below (\d+) measured baseline days", _doc_text())
    assert match
    return int(match.group(1))


def test_contract_primitives_match_the_accepted_values():
    # SOURCE_LAB_STATUSES and ROUTE_ROLES are held to every layer that carries them in
    # tests/unit/test_source_lab_status_enum.py, so they are not repeated here (5 Sep 2026).
    assert CONTRACT_VERSION == "2.0.0"
    assert EVIDENCE_STATES == ("ready", "thin", "contradictory", "unchecked")
    assert LINEAGE_RELATIONS == ("continues", "merges_into", "splits_into")
    assert OUTCOME_STATES == ("peaked", "sustained", "fizzled", "noise", "unresolved")
    assert MONTHLY_OPTIONAL_CREDIT_CAP == 25000


def test_contract_vocabularies_match_their_runtime_consumers_and_the_contract_doc():
    # These seven had no reader outside this test, so a retyped literal only proved the
    # file agreed with itself. Each is now held to the module that validates it at
    # runtime, or to the contract doc sentence that defines it (5 Sep 2026).
    for vocabulary in (
        ASYNC_STATES,
        IDENTITY_HEALTH_STATES,
        POST_EVIDENCE_STATES,
        PROFILE_EVIDENCE_STATES,
        QUESTION_INTENTS,
    ):
        assert vocabulary
        assert len(set(vocabulary)) == len(vocabulary)
    assert set(QUESTION_INTENTS) == fixtures.INTENTS
    assert {
        consumer: {"input": input_ceiling, "output": output_ceiling}
        for consumer, (input_ceiling, output_ceiling) in budget._CONSUMER_CEILINGS.items()
    } == TOKEN_CEILINGS
    # The fixture module checks the async lifecycle against an inline set at its
    # validation site rather than a named constant, so the doc sentence is the
    # shared definition. The evidence states, identity health states and runway
    # floor have no runtime consumer at all and are pinned to the doc alone.
    assert _doc_enum_after("The async state machine is exactly") == ASYNC_STATES
    assert _doc_enum_after("Profile evidence state is exactly") == PROFILE_EVIDENCE_STATES
    assert _doc_enum_after("Post evidence state is exactly") == POST_EVIDENCE_STATES
    assert _doc_identity_health_states() == IDENTITY_HEALTH_STATES
    assert _doc_runway_floor_days() == RUNWAY_FLOOR_DAYS


def test_identifier_encoder_keeps_null_empty_and_utf8_distinct():
    assert encode_identifier_part(None) == "N:"
    assert encode_identifier_part("") == "V0:"
    assert encode_identifier_part("é") == "V2:é"


def test_identifier_encoder_uses_canonical_scalar_values_and_rejects_unsupported_values():
    assert encode_identifier_part(0) == "V1:0"
    assert encode_identifier_part(None) == "N:"
    assert encode_identifier_part("") == "V0:"
    assert encode_identifier_part("a|b") == "V3:a|b"
    assert encode_identifier_part("é") == "V2:é"

    for value in (True, 1.0, [], {}, b"x", ("nested",)):
        with pytest.raises(TypeError):
            encode_identifier_part(value)


def test_omitted_scope_uses_the_declared_default_and_returns_the_seven_field_envelope():
    resolved = resolve_client_scope(run_id="run_001")

    assert resolved.client_scope_id == "ogilvy_default"
    assert resolved.market_scope == ("za", "ng", "ke")
    assert resolved.brand_config_id == "ogilvy_default"
    assert resolved.audience_lens_ids == ()
    assert resolved.theme_id == "ogilvy_42"
    assert resolved.run_id == "run_001"
    assert resolved.contract_version == CONTRACT_VERSION
    assert tuple(resolved.__dataclass_fields__) == (
        "client_scope_id",
        "market_scope",
        "brand_config_id",
        "audience_lens_ids",
        "theme_id",
        "run_id",
        "contract_version",
    )
    with pytest.raises(FrozenInstanceError):
        resolved.theme_id = "changed"


def test_scope_resolution_allows_only_configured_narrowing():
    resolved = resolve_client_scope(
        run_id="run_002",
        client_scope_id="ogilvy_default",
        market_scope=("za", "ke"),
        audience_lens_ids=(),
    )

    assert resolved.market_scope == ("za", "ke")
    assert resolved.audience_lens_ids == ()

    with pytest.raises(ValueError, match="lower case"):
        resolve_client_scope(run_id="run_003", market_scope=("ZA",))
    with pytest.raises(ValueError, match="nonempty"):
        resolve_client_scope(run_id="run_004", market_scope=())
    with pytest.raises(ValueError, match="configured"):
        resolve_client_scope(run_id="run_005", market_scope=("gh",))
    with pytest.raises(ValueError, match="configured"):
        resolve_client_scope(run_id="run_006", audience_lens_ids=("parents",))


def test_scope_resolution_rejects_disabled_unknown_or_mismatched_config_values(tmp_path):
    with pytest.raises(ValueError, match="unknown"):
        resolve_client_scope(run_id="run_007", client_scope_id="missing")
    with pytest.raises(ValueError, match="unknown"):
        resolve_client_scope(run_id="run_007a", client_scope_id="")
    disabled_config = tmp_path / "client_scopes.yaml"
    disabled_config.write_text(
        """contract_version: \"2.0.0\"
default_scope_id: disabled_scope
scopes:
  disabled_scope:
    client_scope_id: disabled_scope
    market_scope: [za]
    brand_config_id: disabled_brand
    audience_lens_ids: []
    theme_id: disabled_theme
    enabled: false
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="disabled"):
        resolve_client_scope(
            run_id="run_008", client_scope_id="disabled_scope", config_path=disabled_config
        )
    with pytest.raises(ValueError, match="brand"):
        resolve_client_scope(run_id="run_009", brand_config_id="unknown_brand")
    with pytest.raises(ValueError, match="theme"):
        resolve_client_scope(run_id="run_010", theme_id="unknown_theme")


def test_scope_resolution_rejects_a_config_with_a_different_contract_version(tmp_path):
    mismatched_config = tmp_path / "client_scopes.yaml"
    mismatched_config.write_text(
        """contract_version: \"9.0.0\"
default_scope_id: fixture_scope
scopes:
  fixture_scope:
    client_scope_id: fixture_scope
    market_scope: [za]
    brand_config_id: fixture_brand
    audience_lens_ids: []
    theme_id: fixture_theme
    enabled: true
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="contract_version"):
        resolve_client_scope(run_id="run_011", config_path=mismatched_config)
