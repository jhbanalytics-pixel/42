"""A client lens is selected by authorization, and its missing inputs stay missing.

The lens is the optional configuration U02 adds beside the seven scope values.
Nothing here selects it from a brand name: only a registry entry authorizes one,
and the entry pins the exact configuration bytes it authorizes. The client inputs
the shipped lens still lacks are read from the configuration itself and refused
by name, so no consumer can substitute a plausible value for one of them.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from src.analysis.open_intelligence import client_lens
from src.analysis.open_intelligence.general_question_plan import build_question_planning_context
from src.analysis.open_intelligence.general_question_policy import build_intake_context
from src.analysis.open_intelligence.general_question_request import normalize_question_request

from tests.unit.test_topic_overlay_bsa import bsa_scope, general_scope

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "configs" / "client_lenses.yaml"
OVERLAY_PATH = ROOT / "configs" / "topic_overlays" / "bsa.yaml"
BSA_LENS = "bsa_pulse_lens"
OVERLAY_DIGEST = "e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf"
# The client inputs the first client configuration does not have. Each name is
# derived from the configuration document, never declared here, and each one
# refuses by that name until the value itself arrives.
OUTSTANDING = (
    "ambassador_refs",
    "bsa_comparator_set_v1",
    "bsa_search_visibility_policy_v1",
    "entities.entity_bsa.pending_inputs",
    "entities.entity_gsa.pending_inputs",
    "entities.entity_pyp.known_ambassador_handles",
    "entities.entity_pyp.pending_inputs",
    "verified_handles",
)


def request_for(message, *, scope, index=21):
    return normalize_question_request(
        {"message": message},
        scope=scope,
        request_id=str(UUID(int=index)),
        admitted_at=datetime(2026, 9, 9, 8, tzinfo=UTC),
        policy_digest="a" * 64,
    )


def planning_request(message, *, scope, index=21):
    request = request_for(message, scope=scope, index=index)
    return build_question_planning_context(
        request, build_intake_context(request, selected_market="za")
    )["request"]


def resolved(**overrides):
    values = {
        "client_scope_id": "bsa_pulse",
        "brand_config_id": "bsa",
        "client_lens_id": BSA_LENS,
    }
    values.update(overrides)
    return client_lens.resolve_client_lens(**values)


def complete_input(input_id):
    return {
        "contract_version": "client_lens_input_v1",
        "input_id": input_id,
        "entries": [
            {
                "entry_id": "fixture_service",
                "canonical_domain": "fixture.example",
                "locale_aliases": {"za": "fixture.example/za"},
                "proof_ref": "fixture_proof",
            }
        ],
    }


def registry_document():
    return yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))


def write_registry(directory, document):
    path = Path(directory) / "client_lenses.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_registry_is_versioned_general_42_is_the_default_and_bsa_pins_its_bytes():
    document = registry_document()

    assert document["contract_version"] == "client_lens_registry_v1"
    assert document["default_client_lens_id"] is None
    assert set(document["lenses"]) == {BSA_LENS}
    entry = document["lenses"][BSA_LENS]
    assert entry["client_scope_id"] == "bsa_pulse"
    assert entry["brand_config_id"] == "bsa"
    assert entry["overlay_id"] == "bsa"
    assert entry["configuration_digest"] == OVERLAY_DIGEST == client_lens.overlay_digest("bsa")
    assert entry["enabled"] is True
    raw = REGISTRY_PATH.read_text(encoding="utf-8")
    for forbidden in ("\u2014", "\u2013", "-" * 2):
        assert forbidden not in raw


def test_general_42_is_the_default_and_names_no_lens():
    assert client_lens.DEFAULT_CLIENT_LENS_ID is None
    assert client_lens.resolve_client_lens(
        client_scope_id="ogilvy_default", brand_config_id=None, client_lens_id=None
    ) is None
    assert client_lens.client_lens_binding(None) == {
        "lens_binding_version": "client_lens_binding_v1",
        "client_lens_id": None,
        "configuration_digest": None,
        "unresolved_client_inputs": [],
    }


def test_a_brand_name_alone_never_selects_a_lens():
    """The BSA scope without an explicit lens id stays general: no name match."""
    assert client_lens.resolve_client_lens(
        client_scope_id="bsa_pulse", brand_config_id="bsa", client_lens_id=None
    ) is None
    # A well shaped name that no entry carries is unauthorized; a name outside the
    # identifier shape is refused by shape, so the regex itself stays observable.
    for client_lens_id, code in (
        ("bsa", "client_lens_unauthorized"),
        ("bsa_pulse", "client_lens_unauthorized"),
        ("BSA_PULSE_LENS", "client_lens_id_invalid"),
        ("", "client_lens_id_invalid"),
        (42, "client_lens_id_invalid"),
        (["bsa_pulse_lens"], "client_lens_id_invalid"),
    ):
        with pytest.raises(client_lens.ClientLensUnavailable) as caught:
            client_lens.resolve_client_lens(
                client_scope_id="bsa_pulse",
                brand_config_id="bsa",
                client_lens_id=client_lens_id,
            )
        assert caught.value.code == code


def test_the_authorized_lens_resolves_with_its_configuration_digest():
    lens = resolved()

    assert lens.client_lens_id == BSA_LENS
    assert lens.client_scope_id == "bsa_pulse"
    assert lens.brand_config_id == "bsa"
    assert lens.overlay_id == "bsa"
    assert lens.configuration_digest == OVERLAY_DIGEST
    assert lens.overlay["entities"]["entity_bsa"]["property"] == "Brand South Africa"
    binding = client_lens.client_lens_binding(lens)
    assert binding["client_lens_id"] == BSA_LENS
    assert binding["configuration_digest"] == OVERLAY_DIGEST
    assert binding["lens_binding_version"] == "client_lens_binding_v1"


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"client_scope_id": "ogilvy_default"}, "client_lens_unauthorized"),
        ({"client_scope_id": "other_scope"}, "client_lens_unauthorized"),
        ({"brand_config_id": None}, "client_lens_binding_invalid"),
        ({"brand_config_id": "other_brand"}, "client_lens_binding_invalid"),
        ({"client_lens_id": "unregistered_lens"}, "client_lens_unauthorized"),
    ],
)
def test_a_lens_outside_its_own_authorization_is_refused(overrides, code):
    with pytest.raises(client_lens.ClientLensUnavailable) as caught:
        resolved(**overrides)
    assert caught.value.code == code


def test_a_disabled_entry_is_refused(tmp_path):
    document = registry_document()
    document["lenses"][BSA_LENS]["enabled"] = False
    with pytest.raises(client_lens.ClientLensUnavailable) as caught:
        resolved(registry_path=write_registry(tmp_path, document))
    assert caught.value.code == "client_lens_unauthorized"


def test_configuration_bytes_that_moved_are_refused_before_the_document_is_used(tmp_path):
    overlays = tmp_path / "topic_overlays"
    overlays.mkdir()
    raw = yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))
    raw["entities"]["entity_bsa"]["aliases"].append("Brand South Africa Limited")
    (overlays / "bsa.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8"
    )

    with pytest.raises(client_lens.ClientLensUnavailable) as caught:
        resolved(overlay_root=overlays)
    assert caught.value.code == "client_lens_configuration_changed"


def test_a_registry_that_pins_the_wrong_digest_is_refused(tmp_path):
    document = registry_document()
    document["lenses"][BSA_LENS]["configuration_digest"] = "f" * 64
    with pytest.raises(client_lens.ClientLensUnavailable) as caught:
        resolved(registry_path=write_registry(tmp_path, document))
    assert caught.value.code == "client_lens_configuration_changed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(contract_version="client_lens_registry_v2"),
        lambda d: d.update(default_client_lens_id=BSA_LENS),
        lambda d: d["lenses"][BSA_LENS].pop("configuration_digest"),
        lambda d: d["lenses"][BSA_LENS].update(extra_field="x"),
        lambda d: d.update(lenses=[]),
    ],
)
def test_a_changed_registry_shape_is_refused(tmp_path, mutate):
    document = registry_document()
    mutate(document)
    with pytest.raises(client_lens.ClientLensUnavailable) as caught:
        resolved(registry_path=write_registry(tmp_path, document))
    assert caught.value.code in (
        "client_lens_registry_invalid",
        "client_lens_unauthorized",
    )


def test_the_shipped_lens_reports_exactly_the_outstanding_client_inputs():
    lens = resolved()

    assert lens.unresolved_client_inputs == OUTSTANDING
    assert lens.resolved_client_inputs == {}
    binding = client_lens.client_lens_binding(lens)
    assert binding["unresolved_client_inputs"] == list(OUTSTANDING)
    # The two named documents are the ones the first client has not supplied.
    assert lens.overlay["comparator_set_ref"] == "bsa_comparator_set_v1"
    assert lens.overlay["search_visibility_policy_ref"] == "bsa_search_visibility_policy_v1"


@pytest.mark.parametrize("name", OUTSTANDING)
def test_every_outstanding_client_input_refuses_by_name(name):
    lens = resolved()
    with pytest.raises(client_lens.ClientInputUnavailable) as caught:
        client_lens.require_client_input(lens, name)
    assert caught.value.code == "client_input_unavailable"
    assert caught.value.input_name == name


def test_an_unknown_input_name_is_refused_rather_than_treated_as_supplied():
    lens = resolved()
    with pytest.raises(client_lens.ClientInputUnavailable) as caught:
        client_lens.require_client_input(lens, "invented_input")
    assert caught.value.code == "client_input_unavailable"


def test_a_supplied_input_document_resolves_and_the_mechanism_is_real(tmp_path):
    inputs = tmp_path / "client_lens_inputs"
    inputs.mkdir()
    (inputs / "bsa_comparator_set_v1.yaml").write_text(
        yaml.safe_dump(complete_input("bsa_comparator_set_v1"), allow_unicode=True),
        encoding="utf-8",
    )

    lens = resolved(inputs_root=inputs)

    assert "bsa_comparator_set_v1" not in lens.unresolved_client_inputs
    assert "bsa_search_visibility_policy_v1" in lens.unresolved_client_inputs
    supplied = client_lens.require_client_input(lens, "bsa_comparator_set_v1")
    assert supplied["entries"][0]["canonical_domain"] == "fixture.example"
    assert supplied["entries"][0]["locale_aliases"] == {"za": "fixture.example/za"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["entries"][0].update(canonical_domain=None),
        lambda d: d["entries"][0].update(canonical_domain=""),
        lambda d: d["entries"][0].update(canonical_domain="TBC"),
        lambda d: d["entries"][0].update(locale_aliases=None),
        lambda d: d["entries"][0].update(locale_aliases={}),
        lambda d: d["entries"][0].update(locale_aliases={"za": "TBD"}),
        lambda d: d["entries"][0].update(proof_ref=None),
        lambda d: d["entries"][0].update(proof_ref="placeholder"),
        lambda d: d.update(entries=[]),
        lambda d: d.update(input_id="other_input"),
        lambda d: d.update(contract_version="client_lens_input_v2"),
    ],
)
def test_an_incomplete_or_placeholder_input_document_stays_unresolved(tmp_path, mutate):
    """A null domain, a missing locale alias or a placeholder never counts as supplied."""
    inputs = tmp_path / "client_lens_inputs"
    inputs.mkdir()
    document = complete_input("bsa_comparator_set_v1")
    mutate(document)
    (inputs / "bsa_comparator_set_v1.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )

    lens = resolved(inputs_root=inputs)

    assert "bsa_comparator_set_v1" in lens.unresolved_client_inputs
    assert lens.resolved_client_inputs == {}
    with pytest.raises(client_lens.ClientInputUnavailable):
        client_lens.require_client_input(lens, "bsa_comparator_set_v1")


def test_the_planning_view_carries_the_lens_and_refuses_the_claims_it_cannot_support():
    lens = resolved()
    request = planning_request(
        "How does South Africa compare with Kenya on BRICS mention share?", scope=bsa_scope()
    )

    view = client_lens.lens_planning_view(lens, request)

    assert view["client_lens_id"] == BSA_LENS
    assert view["configuration_digest"] == OVERLAY_DIGEST
    assert view["lens_binding_version"] == "client_lens_binding_v1"
    assert view["overlay_id"] == "bsa"
    assert view["client_scope_id"] == "bsa_pulse"
    assert view["run_id"] == "question_" + UUID(int=21).hex
    assert view["prevalence_claims_allowed"] is False
    assert view["comparator_claims_allowed"] is False
    assert view["search_visibility_claims_allowed"] is False
    assert view["unresolved_client_inputs"] == list(OUTSTANDING)
    assert view["uncovered_markets"] == list(lens.overlay["uncovered_markets"])
    assert "Prominence" in view["relevant_dimensions"]


def test_the_planning_view_allows_a_comparator_claim_only_once_its_input_is_supplied(tmp_path):
    inputs = tmp_path / "client_lens_inputs"
    inputs.mkdir()
    for input_id in ("bsa_comparator_set_v1", "bsa_search_visibility_policy_v1"):
        (inputs / f"{input_id}.yaml").write_text(
            yaml.safe_dump(complete_input(input_id), allow_unicode=True), encoding="utf-8"
        )

    lens = resolved(inputs_root=inputs)
    request = planning_request(
        "How does South Africa compare with Kenya on BRICS mention share?", scope=bsa_scope()
    )
    view = client_lens.lens_planning_view(lens, request)

    assert view["comparator_claims_allowed"] is True
    assert view["search_visibility_claims_allowed"] is True
    assert "bsa_comparator_set_v1" not in view["unresolved_client_inputs"]


def test_the_lens_never_widens_the_request_it_is_bound_to():
    """A lens binds to the request's own scope; a foreign request is refused."""
    general = planning_request("What changed this week?", scope=general_scope(), index=22)
    with pytest.raises(client_lens.ClientLensUnavailable) as caught:
        client_lens.admit_request_lens(general, client_lens_id=BSA_LENS)
    assert caught.value.code == "client_lens_unauthorized"

    bsa = planning_request("What changed this week?", scope=bsa_scope(), index=23)
    lens, binding = client_lens.admit_request_lens(bsa, client_lens_id=BSA_LENS)
    assert lens.client_lens_id == BSA_LENS
    assert binding["configuration_digest"] == OVERLAY_DIGEST
    assert client_lens.admit_request_lens(bsa, client_lens_id=None) == (
        None,
        client_lens.client_lens_binding(None),
    )
    assert client_lens.admit_request_lens(general, client_lens_id=None)[0] is None


def test_the_lens_adds_no_market_authority_to_the_request():
    lens = resolved()
    request = planning_request("What changed this week?", scope=bsa_scope(), index=24)
    view = client_lens.lens_planning_view(lens, request)

    assert set(lens.overlay["authorized_markets"]) == {"za", "ng", "ke"}
    assert set(request["market_scope"]) <= {"za", "ng", "ke"}
    assert not set(view["uncovered_markets"]) & set(request["market_scope"])
    frozen = copy.deepcopy(request)
    client_lens.lens_planning_view(lens, request)
    assert request == frozen


def test_a_bound_lens_injects_no_generation_language_into_a_commuting_question():
    """The audience grammar is the request's own; the lens adds no audience."""
    from tests.unit.test_engine_audience_neutrality import DEMOGRAPHIC_DEFAULT, _strings

    lens = resolved()
    request = planning_request(
        "Which commuter routes into Johannesburg changed during the taxi strike?",
        scope=bsa_scope(),
        index=31,
    )
    view = client_lens.lens_planning_view(lens, request)

    assert view["audience_framing"] == "none"
    assert view["audience_lens_ids"] == []
    assert view["prevalence_claims_allowed"] is False
    assert view["matched_entities"] == []
    assert [text for text in _strings(view) if DEMOGRAPHIC_DEFAULT.search(text)] == []


def test_a_named_audience_question_stays_a_requested_sample_under_the_lens():
    lens = resolved()
    request = planning_request(
        "What are Gen Z students in Cape Town saying about Play Your Part and jobs in South Africa?",
        scope=bsa_scope(audience_lens_ids=["explicit_gen_z_lens"]),
        index=32,
    )
    view = client_lens.lens_planning_view(lens, request)

    assert view["audience_framing"] == "requested_sample"
    assert view["audience_lens_ids"] == ["explicit_gen_z_lens"]
    assert view["prevalence_claims_allowed"] is False
    assert view["matched_entities"] == ["entity_pyp"]
    assert view["client_lens_id"] == BSA_LENS


def test_a_follow_up_keeps_the_lens_binding_of_the_question_it_follows():
    """A follow-up under a lens resolves the same lens and the same digest."""
    first = planning_request("What is being said about Play Your Part?", scope=bsa_scope(), index=33)
    follow_up = planning_request("And what changed since then?", scope=bsa_scope(), index=34)

    _first_lens, first_binding = client_lens.admit_request_lens(first, client_lens_id=BSA_LENS)
    follow_lens, follow_binding = client_lens.admit_request_lens(
        follow_up, client_lens_id=BSA_LENS
    )

    assert first_binding == follow_binding
    assert follow_lens.configuration_digest == OVERLAY_DIGEST
    assert client_lens.lens_planning_view(follow_lens, follow_up)["client_lens_id"] == BSA_LENS
    # A follow-up that drops the lens is general 42 again, never a silent carry over.
    assert client_lens.admit_request_lens(follow_up, client_lens_id=None)[0] is None


def test_changed_property_meaning_changes_the_evidence_the_lens_matches(tmp_path):
    """A property's meaning lives in the configuration, not in a label."""
    overlays = tmp_path / "topic_overlays"
    overlays.mkdir()
    raw = yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))
    raw["entities"]["entity_pyp"]["rule"] = "exact_phrase_or_handle"
    (overlays / "bsa.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    registry = registry_document()
    registry["lenses"][BSA_LENS]["configuration_digest"] = client_lens.overlay_digest(
        "bsa", root=overlays
    )
    changed = resolved(
        overlay_root=overlays, registry_path=write_registry(tmp_path, registry)
    )
    shipped = resolved()
    text = "everyone must play your part in the office cleanup"

    from src.enrichment import topic_overlays

    assert not topic_overlays.entity_matches(_thawed(shipped), "entity_pyp", text)
    assert topic_overlays.entity_matches(_thawed(changed), "entity_pyp", text)
    assert changed.configuration_digest != shipped.configuration_digest


def _thawed(lens):
    import json

    return json.loads(json.dumps(lens.overlay, default=lambda value: dict(value)))


REGISTRY_DIGEST = "e1a2727e5bd95c260a49c56a94d088ca6039f130825fbb887763253777d0f410"


def test_the_engine_pins_its_own_registry_bytes_inside_the_engine_image():
    """The engine image copies only engine/, so this copy carries its own pin."""
    assert REGISTRY_PATH.is_file()
    assert hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest() == REGISTRY_DIGEST


def test_an_input_id_never_leaves_the_inputs_directory(tmp_path):
    """A named input is an identifier, not a path: traversal reads nothing."""
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "leak.yaml").write_text(
        yaml.safe_dump(complete_input("../secret/leak"), allow_unicode=True), encoding="utf-8"
    )
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "bsa_comparator_set_v1.yaml").write_text(
        yaml.safe_dump(complete_input("bsa_comparator_set_v1"), allow_unicode=True),
        encoding="utf-8",
    )

    assert client_lens._input_document("../secret/leak", inputs) is None
    assert client_lens._input_document("/etc/passwd", inputs) is None
    assert client_lens._input_document("bsa_comparator_set_v1", inputs) is not None


def test_the_overlay_cross_check_stops_a_registry_entry_reaching_another_client(tmp_path):
    """The entry may not hand one client's configuration to another client scope."""
    overlays = tmp_path / "topic_overlays"
    overlays.mkdir()
    raw = yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))
    (overlays / "bsa.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    digest = client_lens.overlay_digest("bsa", root=overlays)
    for field, value in (("client_scope_id", "other_client"), ("brand_config_id", "other_brand")):
        registry = registry_document()
        entry = registry["lenses"][BSA_LENS]
        entry[field] = value
        entry["configuration_digest"] = digest
        with pytest.raises(client_lens.ClientLensUnavailable) as caught:
            client_lens.resolve_client_lens(
                client_scope_id=entry["client_scope_id"],
                brand_config_id=entry["brand_config_id"],
                client_lens_id=BSA_LENS,
                registry_path=write_registry(tmp_path, registry),
                overlay_root=overlays,
            )
        assert caught.value.code == "client_lens_binding_invalid"


def test_a_planning_view_refuses_a_request_from_another_client_scope():
    lens = resolved()
    own = planning_request("What is being said about Play Your Part?", scope=bsa_scope())

    assert client_lens.lens_planning_view(lens, own)["client_lens_id"] == BSA_LENS
    for foreign in (
        {**own, "client_scope_id": "ogilvy_default"},
        {**own, "client_scope_id": None},
        planning_request("What is moving this week?", scope=general_scope(), index=22),
    ):
        with pytest.raises(client_lens.ClientLensUnavailable) as caught:
            client_lens.lens_planning_view(lens, foreign)
        assert caught.value.code == "client_lens_unauthorized"


@pytest.mark.parametrize(
    "client_lens_id",
    ["BSA_PULSE_LENS", "9bsa", "_bsa", "bsa-pulse-lens", "bsa.pulse", "bsa pulse", "a" * 65],
)
def test_a_lens_identifier_outside_the_identifier_shape_is_refused_by_shape(client_lens_id):
    """The identifier regex is the refusal, so the code names the shape, not the roster."""
    with pytest.raises(client_lens.ClientLensUnavailable) as caught:
        client_lens.resolve_client_lens(
            client_scope_id="bsa_pulse", brand_config_id="bsa", client_lens_id=client_lens_id
        )
    assert caught.value.code == "client_lens_id_invalid"


@pytest.mark.parametrize("overlay_id", ["../bsa", "BSA", "bsa/../bsa", "", 42])
def test_an_overlay_id_outside_the_identifier_shape_never_reaches_the_filesystem(overlay_id):
    with pytest.raises(client_lens.ClientLensUnavailable) as caught:
        client_lens.overlay_digest(overlay_id)
    assert caught.value.code == "client_lens_unauthorized"


def _registry_refusal(tmp_path, mutate):
    registry = registry_document()
    mutate(registry)
    with pytest.raises(client_lens.ClientLensUnavailable) as caught:
        client_lens.load_client_lens_registry(write_registry(tmp_path, registry))
    assert caught.value.code == "client_lens_registry_invalid"


def test_the_registry_entry_key_must_be_the_entry_it_names(tmp_path):
    def rekey(registry):
        registry["lenses"]["other_lens"] = registry["lenses"].pop(BSA_LENS)

    _registry_refusal(tmp_path, rekey)


@pytest.mark.parametrize(
    "digest",
    ["", "not a digest", "e1baafa8", "E1BAAFA865B5C9419225102D752C651C5B6E4FADA034A2317F2D6F8A395336BF", 42],
)
def test_the_registry_digest_must_be_a_sha256_of_the_right_shape(tmp_path, digest):
    _registry_refusal(
        tmp_path, lambda registry: registry["lenses"][BSA_LENS].update(configuration_digest=digest)
    )


@pytest.mark.parametrize("enabled", ["true", 1, 0, None, "yes"])
def test_the_registry_enabled_flag_must_be_a_bool(tmp_path, enabled):
    _registry_refusal(
        tmp_path, lambda registry: registry["lenses"][BSA_LENS].update(enabled=enabled)
    )


def test_a_disabled_entry_is_offered_to_nobody_and_resolves_for_nobody(tmp_path):
    registry = registry_document()
    registry["lenses"][BSA_LENS]["enabled"] = False
    path = write_registry(tmp_path, registry)

    assert client_lens.authorized_client_lenses(
        client_scope_id="bsa_pulse", registry_path=path
    ) == []
    with pytest.raises(client_lens.ClientLensUnavailable) as caught:
        client_lens.resolve_client_lens(
            client_scope_id="bsa_pulse",
            brand_config_id="bsa",
            client_lens_id=BSA_LENS,
            registry_path=path,
        )
    assert caught.value.code == "client_lens_unauthorized"


def test_a_brand_name_alone_no_longer_selects_a_configuration(tmp_path):
    """Only an enabled entry for the request's own client scope selects and pins."""
    from src.enrichment import topic_overlays

    assert topic_overlays.overlay_for_scope(general_scope()) is None
    assert topic_overlays.overlay_for_scope(bsa_scope())["overlay_id"] == "bsa"
    # The same brand name under a client scope no entry names selects nothing.
    for scope in (
        bsa_scope(client_scope_id="ogilvy_default"),
        bsa_scope(client_scope_id="another_client"),
    ):
        with pytest.raises(topic_overlays.TopicOverlayInvalid) as caught:
            topic_overlays.overlay_for_scope(scope)
        assert caught.value.code == "client_lens_unauthorized"


def test_the_scope_configuration_is_pinned_to_the_bytes_its_entry_authorizes(tmp_path):
    """A moved configuration document is refused rather than loaded."""
    from src.enrichment import topic_overlays

    overlays = tmp_path / "topic_overlays"
    overlays.mkdir()
    raw = yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))
    raw["uncovered_markets"] = ["gh"]
    (overlays / "bsa.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(topic_overlays.TopicOverlayInvalid) as caught:
        topic_overlays.overlay_for_scope(bsa_scope(), root=overlays)
    assert caught.value.code == "client_lens_configuration_changed"


ALT_LENS = "bsa_pulse_alt_lens"
ALT_OVERLAY = "bsa_alt"
# One text each side of the difference between the two configurations below.
ELECTION_TEXT = "Plan the election coverage for the desk"
REFERENDUM_TEXT = "Plan the referendum coverage for the desk"


def _overlay_pair(tmp_path):
    """Two configurations for one brand that prohibit two different things.

    The registry is one to one to one in the shipped tree, so with a single entry
    the lens can select nothing the scope would not have selected anyway. Two
    entries are what make the selection observable at all.
    """
    overlays = tmp_path / "topic_overlays"
    overlays.mkdir()
    base = yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))

    alternate = copy.deepcopy(base)
    alternate["overlay_id"] = ALT_OVERLAY
    for purpose in alternate["purpose_policy"]["prohibited_purposes"]:
        if purpose["code"] == "electoral_purpose":
            purpose["patterns"] = ["\\breferendum\\b"]
    for overlay_id, document in (("bsa", base), (ALT_OVERLAY, alternate)):
        (overlays / f"{overlay_id}.yaml").write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    return overlays


def _two_entry_registry(tmp_path, overlays, **alternate_overrides):
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    document = registry_document()
    entry = document["lenses"][BSA_LENS]
    entry["configuration_digest"] = client_lens.overlay_digest("bsa", root=overlays)
    alternate = {
        **copy.deepcopy(entry),
        "client_lens_id": ALT_LENS,
        "label": "Brand South Africa Pulse, alternate",
        "overlay_id": ALT_OVERLAY,
        "configuration_digest": client_lens.overlay_digest(ALT_OVERLAY, root=overlays),
    }
    alternate.update(alternate_overrides)
    document["lenses"][ALT_LENS] = alternate
    return write_registry(tmp_path, document)


def test_two_lenses_for_one_brand_choose_two_different_purpose_policies(tmp_path):
    """The lens has to change the configuration that runs, not only travel beside it.

    The two entries name two documents whose purpose policies prohibit different
    texts, so the same scope and the same text reach opposite outcomes depending
    on which lens the request carries. Nothing structural is asserted here: the
    proof is which policy actually ran.
    """
    from src.analysis.open_intelligence import client_overlay

    overlays = _overlay_pair(tmp_path)
    registry = _two_entry_registry(tmp_path, overlays)

    def lens_for(lens_id):
        return client_lens.resolve_client_lens(
            client_scope_id="bsa_pulse",
            brand_config_id="bsa",
            client_lens_id=lens_id,
            registry_path=registry,
            overlay_root=overlays,
        )

    shipped, alternate = lens_for(BSA_LENS), lens_for(ALT_LENS)
    assert shipped.overlay_id == "bsa" and alternate.overlay_id == ALT_OVERLAY

    def refuse(lens, text):
        return client_overlay.refuse_prohibited_client_purpose(
            bsa_scope(), [text], stage="admission",
            root=overlays, lens=lens, registry_path=registry,
        )

    # Two different compiled purpose policies, not two references to one.
    compiled = {
        lens.client_lens_id: client_overlay.compiled_overlay_for_scope(
            bsa_scope(), root=overlays, lens=lens, registry_path=registry
        )["purpose_policy_digest"]
        for lens in (shipped, alternate)
    }
    assert compiled[BSA_LENS] != compiled[ALT_LENS]

    with pytest.raises(client_overlay.ClientPurposeProhibited) as caught:
        refuse(shipped, ELECTION_TEXT)
    assert "electoral_purpose" in caught.value.purpose_codes
    assert refuse(alternate, ELECTION_TEXT)["state"] == "permitted"

    with pytest.raises(client_overlay.ClientPurposeProhibited):
        refuse(alternate, REFERENDUM_TEXT)
    assert refuse(shipped, REFERENDUM_TEXT)["state"] == "permitted"

    # With two entries enabled for one client scope and brand, nothing selects
    # without a lens, so a caller that dropped the lens cannot silently land on
    # one of the two configurations.
    from src.enrichment import topic_overlays

    with pytest.raises(topic_overlays.TopicOverlayInvalid) as ambiguous:
        client_overlay.refuse_prohibited_client_purpose(
            bsa_scope(), [ELECTION_TEXT], stage="admission",
            root=overlays, registry_path=registry,
        )
    assert ambiguous.value.code == "client_lens_unauthorized"


def test_the_scope_selector_weighs_enabled_brand_client_scope_and_ambiguity(tmp_path):
    """The four predicates that decide which entry authorizes a configuration."""
    overlays = _overlay_pair(tmp_path)
    scope = {"client_scope_id": "bsa_pulse", "brand_config_id": "bsa"}

    def select(registry):
        return client_lens.authorize_scope_configuration(
            scope, registry_path=registry, overlay_root=overlays
        )

    def refused(registry):
        with pytest.raises(client_lens.ClientLensUnavailable) as caught:
            select(registry)
        return caught.value.code

    # Two enabled entries for one client scope and brand authorize nothing.
    assert refused(_two_entry_registry(tmp_path, overlays)) == "client_lens_unauthorized"

    # A disabled second entry is not a second entry, so the first one selects.
    disabled = _two_entry_registry(tmp_path / "disabled", overlays, enabled=False)
    assert select(disabled)["client_lens_id"] == BSA_LENS

    # Nor is one carrying another brand configuration.
    other_brand = _two_entry_registry(tmp_path / "brand", overlays, brand_config_id="other")
    assert select(other_brand)["client_lens_id"] == BSA_LENS

    # Nor is one belonging to another client scope.
    other_scope = _two_entry_registry(
        tmp_path / "client_scope", overlays, client_scope_id="another_client"
    )
    assert select(other_scope)["client_lens_id"] == BSA_LENS

    # A brand no entry carries, and a client scope no entry names, select nothing.
    single = _two_entry_registry(tmp_path / "single", overlays, enabled=False)
    for missing in (
        {"client_scope_id": "bsa_pulse", "brand_config_id": "unlisted"},
        {"client_scope_id": "ogilvy_default", "brand_config_id": "bsa"},
    ):
        with pytest.raises(client_lens.ClientLensUnavailable) as caught:
            client_lens.authorize_scope_configuration(
                missing, registry_path=single, overlay_root=overlays
            )
        assert caught.value.code == "client_lens_unauthorized"

    # General 42 carries no brand configuration, so it reaches no entry at all.
    assert (
        client_lens.authorize_scope_configuration(
            {"client_scope_id": "ogilvy_default", "brand_config_id": None},
            registry_path=single,
            overlay_root=overlays,
        )
        is None
    )

    # And the bytes the surviving entry pins are still checked before a load.
    moved = registry_document()
    moved["lenses"][BSA_LENS]["configuration_digest"] = "f" * 64
    moved_at = tmp_path / "moved"
    moved_at.mkdir(parents=True, exist_ok=True)
    with pytest.raises(client_lens.ClientLensUnavailable) as caught:
        client_lens.authorize_scope_configuration(
            scope, registry_path=write_registry(moved_at, moved), overlay_root=overlays
        )
    assert caught.value.code == "client_lens_configuration_changed"


def test_a_resolved_lens_is_rechecked_against_the_bytes_where_it_is_used(tmp_path):
    """The pin at resolution does not keep the document still afterwards.

    A resolved lens carries the digest its entry authorized. The document it
    names can move between the moment the lens resolved and the moment the
    answer path uses it, so the use site checks the bytes again rather than
    trusting the claim the lens is carrying.
    """
    from src.enrichment import topic_overlays

    overlays = _overlay_pair(tmp_path)
    registry = _two_entry_registry(tmp_path, overlays, enabled=False)
    lens = client_lens.resolve_client_lens(
        client_scope_id="bsa_pulse",
        brand_config_id="bsa",
        client_lens_id=BSA_LENS,
        registry_path=registry,
        overlay_root=overlays,
    )
    assert (
        topic_overlays.overlay_for_scope(
            bsa_scope(), root=overlays, lens=lens, registry_path=registry
        )["overlay_id"]
        == "bsa"
    )

    document = yaml.safe_load((overlays / "bsa.yaml").read_text(encoding="utf-8"))
    document["uncovered_markets"] = ["gh"]
    (overlays / "bsa.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    assert client_lens.overlay_digest("bsa", root=overlays) != lens.configuration_digest
    with pytest.raises(topic_overlays.TopicOverlayInvalid) as caught:
        topic_overlays.overlay_for_scope(
            bsa_scope(), root=overlays, lens=lens, registry_path=registry
        )
    assert caught.value.code == "client_lens_configuration_changed"


def test_a_lens_bound_to_another_brand_is_refused_before_its_bytes_are_read(tmp_path):
    """Which request a lens belongs to is settled before what it configures.

    A lens whose brand configuration is not this request's own is refused as a
    binding failure whatever state its own document is in, so the refusal an
    audit record carries names the reason the lens was rejected rather than
    whatever the loader happened to notice first.
    """
    from src.enrichment import topic_overlays

    overlays = _overlay_pair(tmp_path)
    foreign = copy.deepcopy(yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8")))
    foreign["overlay_id"] = "other_brand"
    foreign["brand_config_id"] = "other_brand"
    (overlays / "other_brand.yaml").write_text(
        yaml.safe_dump(foreign, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    document = registry_document()
    document["lenses"][BSA_LENS]["enabled"] = False
    document["lenses"]["other_brand_lens"] = {
        "client_lens_id": "other_brand_lens",
        "label": "Another brand",
        "client_scope_id": "bsa_pulse",
        "brand_config_id": "other_brand",
        "overlay_id": "other_brand",
        "configuration_digest": client_lens.overlay_digest("other_brand", root=overlays),
        "enabled": True,
    }
    at = tmp_path / "foreign"
    at.mkdir(parents=True, exist_ok=True)
    registry = write_registry(at, document)
    lens = client_lens.resolve_client_lens(
        client_scope_id="bsa_pulse",
        brand_config_id="other_brand",
        client_lens_id="other_brand_lens",
        registry_path=registry,
        overlay_root=overlays,
    )
    assert lens.brand_config_id == "other_brand"

    # Its own document then goes missing, which is what the loader would notice.
    (overlays / "other_brand.yaml").unlink()
    with pytest.raises(topic_overlays.TopicOverlayInvalid) as caught:
        topic_overlays.overlay_for_scope(
            bsa_scope(), root=overlays, lens=lens, registry_path=registry
        )
    assert caught.value.code == "overlay_binding_invalid"


def test_the_scope_selector_is_reachable_through_production_with_a_substituted_registry(
    tmp_path,
):
    """overlay_for_scope has to forward the registry, or the selector is untestable."""
    from src.enrichment import topic_overlays

    overlays = _overlay_pair(tmp_path)
    single = _two_entry_registry(tmp_path, overlays, enabled=False)
    assert (
        topic_overlays.overlay_for_scope(bsa_scope(), root=overlays, registry_path=single)[
            "overlay_id"
        ]
        == "bsa"
    )
    both = _two_entry_registry(tmp_path / "both", overlays)
    with pytest.raises(topic_overlays.TopicOverlayInvalid) as caught:
        topic_overlays.overlay_for_scope(bsa_scope(), root=overlays, registry_path=both)
    assert caught.value.code == "client_lens_unauthorized"


def test_an_authorized_lens_is_what_selects_the_configuration_on_the_answer_path():
    """A resolved lens selects its own configuration and refuses a foreign scope."""
    from src.analysis.open_intelligence import client_overlay
    from src.enrichment import topic_overlays

    lens = resolved()
    assert topic_overlays.overlay_for_scope(bsa_scope(), lens=lens)["overlay_id"] == "bsa"
    assert (
        client_overlay.compiled_overlay_for_scope(bsa_scope(), lens=lens)["brand_config_id"]
        == "bsa"
    )
    with pytest.raises(topic_overlays.TopicOverlayInvalid) as caught:
        topic_overlays.overlay_for_scope(bsa_scope(client_scope_id="ogilvy_default"), lens=lens)
    assert caught.value.code == "overlay_binding_invalid"


# The lens a stored request was admitted under, exactly as admission records it.
BSA_BINDING = {
    "lens_binding_version": "client_lens_binding_v1",
    "client_lens_id": BSA_LENS,
    "configuration_digest": OVERLAY_DIGEST,
}


def planning_sdk(message, *, scope, client_lens=None, index=31):
    """The exact bytes production hands the planning model for one admitted request."""
    import json

    from src.analysis.open_intelligence.general_question_planning import (
        build_question_planning_request,
    )
    from src.analysis.open_intelligence.general_question_policy import build_question_policy

    policy = build_question_policy(pricing_verified_at=datetime(2026, 9, 9, 7, tzinfo=UTC))
    request = normalize_question_request(
        {"message": message},
        scope=scope,
        request_id=str(UUID(int=index)),
        admitted_at=datetime(2026, 9, 9, 8, tzinfo=UTC),
        policy_digest=policy["policy_digest"],
        client_lens=client_lens,
    )
    intake = build_intake_context(request, selected_market="za")
    sdk = build_question_planning_request(request, intake, policy=policy, remaining_seconds=30)
    return sdk, json.loads(sdk.contents)


def test_the_planner_sees_what_the_bound_lens_means_and_general_42_sees_nothing():
    """The same wording is planned differently under the lens it was admitted under.

    General 42 adds nothing to the planning bytes. Under the BSA lens the planner
    is told which configured property the question names, so Global South
    Africans is the diaspora property and Global South Academy is no property at
    all, and it is told what the lens cannot support and which markets stay gaps.
    """
    question = "What are Global South Africans saying about moving back to South Africa?"
    general_sdk, general = planning_sdk(question, scope=general_scope())
    assert "client_lens" not in general

    lensed_sdk, lensed = planning_sdk(question, scope=bsa_scope(), client_lens=BSA_BINDING)
    view = lensed["client_lens"]
    assert view["client_lens_id"] == BSA_LENS
    assert view["configuration_digest"] == OVERLAY_DIGEST
    assert view["matched_entities"] == ["entity_gsa"]
    assert "Talent" in view["relevant_dimensions"]
    assert view["prevalence_claims_allowed"] is False
    assert view["audience_framing"] == "none"
    assert "United Kingdom" in view["uncovered_markets"]
    assert view["unresolved_client_inputs"] == list(OUTSTANDING)
    assert lensed_sdk.input_digest != general_sdk.input_digest

    _, academy = planning_sdk(
        "What is Global South Academy saying about moving back to South Africa?",
        scope=bsa_scope(),
        client_lens=BSA_BINDING,
    )
    assert academy["client_lens"]["matched_entities"] == []


def test_the_planner_refuses_a_lens_whose_pinned_bytes_have_moved():
    """A record asserts a lens; planning re-resolves it before any model sees it."""
    with pytest.raises(client_lens.ClientLensUnavailable) as caught:
        planning_sdk(
            "What are Global South Africans saying?",
            scope=bsa_scope(),
            client_lens={**BSA_BINDING, "configuration_digest": "0" * 64},
        )
    assert caught.value.code == "client_lens_configuration_changed"


def test_a_comparator_question_is_given_the_comparator_set_only_once_it_is_supplied(
    tmp_path, monkeypatch
):
    """Benchmarking needs the client's comparator set, which is still owed.

    The planner is told the set is owed rather than handed a plausible list. Once
    the input document exists, the same wording gives the planner its entries.
    """
    question = "How does South Africa's BRICS mention share rank against Kenya?"
    _, owed = planning_sdk(question, scope=bsa_scope(), client_lens=BSA_BINDING)
    assert owed["client_lens"]["comparator_set"] == {
        "input_id": "bsa_comparator_set_v1",
        "status": "owed",
        "entries": [],
    }
    _, unrelated = planning_sdk(
        "What are Global South Africans saying?", scope=bsa_scope(), client_lens=BSA_BINDING
    )
    assert unrelated["client_lens"]["comparator_set"] is None

    inputs = tmp_path / "client_lens_inputs"
    inputs.mkdir()
    (inputs / "bsa_comparator_set_v1.yaml").write_text(
        yaml.safe_dump(complete_input("bsa_comparator_set_v1")), encoding="utf-8"
    )
    monkeypatch.setattr(client_lens, "CLIENT_LENS_INPUTS_DIR", inputs)
    _, supplied = planning_sdk(question, scope=bsa_scope(), client_lens=BSA_BINDING)
    comparator = supplied["client_lens"]["comparator_set"]
    assert comparator["status"] == "supplied"
    assert comparator["entries"] == complete_input("bsa_comparator_set_v1")["entries"]
    assert supplied["client_lens"]["comparator_claims_allowed"] is True
