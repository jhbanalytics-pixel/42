"""Versioned client topic overlays, loaded beside the topic anchors.

An overlay is an optional lens a scope names through ``brand_config_id``. General
42 names none. An overlay changes which evidence is relevant and how it is framed;
it never widens market authority, never adds an audience default, and never turns
an observed sample into a population claim.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent.parent
TOPIC_OVERLAYS_DIR = _ROOT / "configs" / "topic_overlays"
OVERLAY_VERSION = "topic_overlay_v1"
APPROVED_MARKETS = frozenset({"za", "ng", "ke"})
ENTITY_RULES = frozenset(
    {"exact_phrase_or_handle", "exclude_global_south", "require_south_africa_cooccurrence"}
)
AUDIENCE_FRAMING = {
    "default_generation_language": "none",
    "named_audience": "requested_sample",
    "sample_to_population": "refused",
}
OVERLAY_FIELDS = frozenset(
    {
        "contract_version",
        "overlay_id",
        "brand_config_id",
        "client_scope_id",
        "provenance",
        "authorized_markets",
        "uncovered_markets",
        "audience_framing",
        "entities",
        "negative_fixtures",
        "dimensions",
    }
)
# The client block B02 compiles; the loader carries it through unread so the
# same document serves both the topic lens and the client overlay compiler.
CLIENT_OVERLAY_FIELDS = frozenset(
    {
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
    }
)
_OVERLAY_ID = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SOUTH_AFRICA = re.compile(r"\bsouth\s+africa(?:n|ns)?\b", re.IGNORECASE)
_GLOBAL_SOUTH_OTHER = re.compile(r"\bglobal\s+south\b(?!\s+africans\b)", re.IGNORECASE)


class TopicOverlayInvalid(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _strings(value: object, code: str, *, nonempty: bool) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise TopicOverlayInvalid(code)
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or item != item.strip():
            raise TopicOverlayInvalid(code)
        items.append(item)
    if len(items) != len(set(items)):
        raise TopicOverlayInvalid(code)
    return items


def _entity(entity_id: str, value: object) -> dict:
    code = f"entity_invalid:{entity_id}"
    if not isinstance(value, dict) or not {"property", "rule", "aliases"} <= set(value):
        raise TopicOverlayInvalid(code)
    if not isinstance(value["property"], str) or not value["property"].strip():
        raise TopicOverlayInvalid(code)
    if value["rule"] not in ENTITY_RULES:
        raise TopicOverlayInvalid(code)
    _strings(value["aliases"], code, nonempty=True)
    for field in ("exclusions", "pending_inputs", "cooccurrence_any", "known_ambassador_handles"):
        if field in value:
            _strings(value[field], code, nonempty=False)
    if value["rule"] == "require_south_africa_cooccurrence" and not value.get("cooccurrence_any"):
        raise TopicOverlayInvalid(code)
    return copy.deepcopy(value)


def _dimension(dimension_id: str, value: object) -> dict:
    code = f"dimension_invalid:{dimension_id}"
    if not isinstance(value, dict) or not {"label", "terms"} <= set(value):
        raise TopicOverlayInvalid(code)
    if not isinstance(value["label"], str) or not value["label"].strip():
        raise TopicOverlayInvalid(code)
    _strings(value["terms"], code, nonempty=True)
    return copy.deepcopy(value)


def load_topic_overlay(overlay_id: str, *, root: Path | None = None) -> dict:
    """Read and validate one overlay; a missing or malformed file is one refusal."""
    if not isinstance(overlay_id, str) or _OVERLAY_ID.fullmatch(overlay_id) is None:
        raise TopicOverlayInvalid("overlay_id_invalid")
    path = (TOPIC_OVERLAYS_DIR if root is None else Path(root)) / f"{overlay_id}.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise TopicOverlayInvalid("overlay_unavailable") from error
    return validate_topic_overlay(raw, overlay_id=overlay_id)


def validate_topic_overlay(raw: object, *, overlay_id: str) -> dict:
    """Validate one parsed overlay document; the client block passes through unread."""
    if (
        not isinstance(raw, dict)
        or not set(raw) >= OVERLAY_FIELDS
        or not set(raw) <= OVERLAY_FIELDS | CLIENT_OVERLAY_FIELDS
    ):
        raise TopicOverlayInvalid("overlay_shape_invalid")
    if raw["contract_version"] != OVERLAY_VERSION or raw["overlay_id"] != overlay_id:
        raise TopicOverlayInvalid("overlay_version_invalid")
    for field in ("brand_config_id", "client_scope_id"):
        if not isinstance(raw[field], str) or not raw[field].strip():
            raise TopicOverlayInvalid("overlay_binding_invalid")
    provenance = raw["provenance"]
    if (
        not isinstance(provenance, dict)
        or set(provenance) != {"source_document", "source_sha256"}
        or not isinstance(provenance["source_document"], str)
        or not provenance["source_document"].strip()
        or any(sep in provenance["source_document"] for sep in ("/", "\\", ":"))
        or not isinstance(provenance["source_sha256"], str)
        or _DIGEST.fullmatch(provenance["source_sha256"]) is None
    ):
        raise TopicOverlayInvalid("provenance_invalid")
    markets = _strings(raw["authorized_markets"], "markets_invalid", nonempty=True)
    if not set(markets) <= APPROVED_MARKETS:
        raise TopicOverlayInvalid("markets_invalid")
    _strings(raw["uncovered_markets"], "markets_invalid", nonempty=False)
    if raw["audience_framing"] != AUDIENCE_FRAMING:
        raise TopicOverlayInvalid("audience_framing_invalid")
    if not isinstance(raw["entities"], dict) or not raw["entities"]:
        raise TopicOverlayInvalid("entities_invalid")
    entities = {key: _entity(key, value) for key, value in raw["entities"].items()}
    if not isinstance(raw["dimensions"], dict) or not raw["dimensions"]:
        raise TopicOverlayInvalid("dimensions_invalid")
    dimensions = {key: _dimension(key, value) for key, value in raw["dimensions"].items()}
    fixtures = raw["negative_fixtures"]
    if not isinstance(fixtures, dict) or not set(fixtures) <= set(entities):
        raise TopicOverlayInvalid("negative_fixtures_invalid")
    for value in fixtures.values():
        _strings(value, "negative_fixtures_invalid", nonempty=True)
    return {
        **copy.deepcopy(raw),
        "authorized_markets": markets,
        "entities": entities,
        "dimensions": dimensions,
    }


def overlay_for_scope(
    scope: dict, *, root: Path | None = None, lens=None, registry_path=None
) -> dict | None:
    """General 42 has no overlay; an authorized lens selects and pins the rest.

    A brand name on its own selects nothing. When the request carries a resolved
    client lens, that lens names the document and the bytes it authorizes. When
    it carries none, the single enabled registry entry for this request's own
    client scope and brand configuration is what selects, and the bytes that
    entry pins are checked before the document is read.

    The registry this reads is a parameter, so the entry that selects can be
    substituted in a test the same way the document root already can. Without
    that, nothing could reach this selector through the code production runs.
    """
    brand = scope.get("brand_config_id") if isinstance(scope, dict) else None
    if brand is None:
        return None
    from src.analysis.open_intelligence import client_lens as _client_lens

    try:
        if lens is None:
            entry = _client_lens.authorize_scope_configuration(
                scope, overlay_root=root, registry_path=registry_path
            )
            overlay_id, digest = entry["overlay_id"], entry["configuration_digest"]
        else:
            if (
                not isinstance(lens, _client_lens.ClientLens)
                or lens.brand_config_id != brand
                or lens.client_scope_id != scope.get("client_scope_id")
            ):
                raise TopicOverlayInvalid("overlay_binding_invalid")
            overlay_id, digest = lens.overlay_id, lens.configuration_digest
            if _client_lens.overlay_digest(overlay_id, root=root) != digest:
                raise TopicOverlayInvalid("client_lens_configuration_changed")
    except _client_lens.ClientLensUnavailable as error:
        raise TopicOverlayInvalid(error.code) from error
    overlay = load_topic_overlay(overlay_id, root=root)
    if overlay["brand_config_id"] != brand or overlay["overlay_id"] != overlay_id:
        raise TopicOverlayInvalid("overlay_binding_invalid")
    return overlay


def _phrase(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias)
    # A hashtag or a handle has no word boundary before its sigil, so the guard is
    # the character before it rather than a word boundary.
    head = r"(?<![\w#@])" if alias[:1] in ("#", "@") else r"\b"
    return re.compile(head + escaped + r"(?![\w])", re.IGNORECASE)


def _matched_aliases(entity: dict, text: str) -> list[str]:
    return [alias for alias in entity["aliases"] if _phrase(alias).search(text)]


def entity_matches(overlay: dict, entity_id: str, text: str) -> bool:
    """Apply the entity's disambiguation rule to one text; unmatched text is never an entity."""
    entity = overlay["entities"][entity_id]
    if not isinstance(text, str) or not text.strip():
        return False
    matched = _matched_aliases(entity, text)
    if not matched:
        return False
    rule = entity["rule"]
    if rule == "exact_phrase_or_handle":
        return not any(_phrase(term).search(text) for term in entity.get("exclusions", []))
    if rule == "exclude_global_south":
        return _GLOBAL_SOUTH_OTHER.search(text) is None
    hashtag = any(alias.startswith("#") for alias in matched)
    south_africa_term = _SOUTH_AFRICA.search(text) is not None or any(
        _phrase(alias).search(text) for alias in overlay["entities"]["entity_bsa"]["aliases"]
    )
    ambassador = any(_phrase(h).search(text) for h in entity.get("known_ambassador_handles", []))
    return hashtag or south_africa_term or ambassador


def overlay_planning_view(overlay: dict, request: dict) -> dict:
    """What a planner may see from the overlay for one normalized request.

    Framing follows the request's own audience grammar: no lens means no audience
    language at all, a requested lens means an observed sample that is never
    presented as population prevalence.
    """
    question = request["question"]
    matched = [
        entity_id
        for entity_id in overlay["entities"]
        if entity_matches(overlay, entity_id, question)
    ]
    dimensions = [
        dimension["label"]
        for dimension in overlay["dimensions"].values()
        if any(_phrase(term).search(question) for term in dimension["terms"])
    ]
    requested = bool(request["audience_lens_ids"])
    return {
        "overlay_id": overlay["overlay_id"],
        "brand_config_id": overlay["brand_config_id"],
        "client_scope_id": request["client_scope_id"],
        "run_id": request["run_id"],
        "contract_version": request["contract_version"],
        "matched_entities": matched,
        "relevant_dimensions": dimensions,
        "audience_framing": "requested_sample" if requested else "none",
        "audience_lens_ids": list(request["audience_lens_ids"]),
        "prevalence_claims_allowed": overlay["audience_framing"]["sample_to_population"]
        != "refused",
        "uncovered_markets": list(overlay["uncovered_markets"]),
    }
