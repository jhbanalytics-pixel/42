from __future__ import annotations

import hashlib
import importlib
import importlib.util
from pathlib import Path

import pytest
import yaml

EXPECTED_DIGEST = "e1a90542a42ff327cfa20b7145494ce127e5e820e25f0d8e26c5a14978ff659d"


def scope_module():
    name = "src.api.investigation_scopes"
    assert importlib.util.find_spec(name) is not None, "investigation scope module is missing"
    return importlib.import_module(name)


def test_real_2_1_scope_config_is_audience_neutral_and_digest_pinned():
    module = scope_module()
    raw = module.INVESTIGATION_SCOPES_PATH.read_bytes()
    parsed = yaml.safe_load(raw)

    assert hashlib.sha256(raw).hexdigest() == EXPECTED_DIGEST == module.CONFIG_DIGEST
    assert parsed == {
        "contract_version": "2.1.0",
        "default_scope_id": "ogilvy_default",
        "scopes": {
            "ogilvy_default": {
                "client_scope_id": "ogilvy_default",
                "market_scope": ["za", "ng", "ke"],
                "brand_config_id": None,
                "audience_lens_ids": [],
                "theme_id": None,
                "enabled": True,
            },
            "bsa_pulse": {
                "client_scope_id": "bsa_pulse",
                "market_scope": ["za", "ng", "ke"],
                "brand_config_id": "bsa",
                "audience_lens_ids": [],
                "theme_id": None,
                "enabled": True,
            },
        },
    }
    for scope in parsed["scopes"].values():
        assert scope["audience_lens_ids"] == [] and scope["theme_id"] is None
    assert parsed["scopes"]["bsa_pulse"]["market_scope"] == parsed["scopes"]["ogilvy_default"]["market_scope"]


def test_bsa_scope_resolves_as_brand_configuration_without_market_escalation():
    module = scope_module()
    resolved = module.resolve_investigation_scope(
        client_scope_id="bsa_pulse",
        market_scope=("za",),
        brand_config_id="bsa",
        audience_lens_ids=(),
        theme_id=None,
    )
    assert resolved.brand_config_id == "bsa" and resolved.market_scope == ("za",)
    for overrides in (
        {"market_scope": ("us",)},
        {"brand_config_id": None},
        {"audience_lens_ids": ("bsa_diaspora_inferred",)},
    ):
        with pytest.raises(module.InvestigationScopeInvalid):
            module.resolve_investigation_scope(
                **{
                    "client_scope_id": "bsa_pulse",
                    "market_scope": ("za",),
                    "brand_config_id": "bsa",
                    "audience_lens_ids": (),
                    "theme_id": None,
                    **overrides,
                }
            )


def test_resolver_accepts_configured_market_subset_and_empty_audience():
    module = scope_module()
    resolved = module.resolve_investigation_scope(
        client_scope_id="ogilvy_default",
        market_scope=("za", "ke"),
        brand_config_id=None,
        audience_lens_ids=(),
        theme_id=None,
    )

    assert resolved == module.ResolvedInvestigationScope(
        client_scope_id="ogilvy_default",
        market_scope=("za", "ke"),
        brand_config_id=None,
        audience_lens_ids=(),
        theme_id=None,
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_scope_id": "unknown"},
        {"market_scope": ()},
        {"market_scope": ("ZA",)},
        {"market_scope": ("za", "za")},
        {"market_scope": ("uk",)},
        {"brand_config_id": "invented_brand"},
        {"audience_lens_ids": ("gen_z_inferred",)},
        {"theme_id": "invented_theme"},
    ],
)
def test_resolver_rejects_unknown_scope_without_revealing_registry(overrides):
    module = scope_module()
    values = {
        "client_scope_id": "ogilvy_default",
        "market_scope": ("za", "ng", "ke"),
        "brand_config_id": None,
        "audience_lens_ids": (),
        "theme_id": None,
    }
    values.update(overrides)

    with pytest.raises(module.InvestigationScopeInvalid) as caught:
        module.resolve_investigation_scope(**values)
    assert str(caught.value) == "scope_invalid"


def test_resolver_rejects_changed_or_malformed_config(tmp_path: Path):
    module = scope_module()
    changed = tmp_path / "changed.yaml"
    changed.write_text(
        module.INVESTIGATION_SCOPES_PATH.read_text(encoding="utf-8").replace(
            "audience_lens_ids: []", "audience_lens_ids: [gen_z_inferred]"
        ),
        encoding="utf-8",
        newline="\n",
    )
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("contract_version: 2.1.0\n", encoding="utf-8", newline="\n")

    for path in (changed, malformed):
        with pytest.raises(module.InvestigationScopeInvalid) as caught:
            module.resolve_investigation_scope(
                client_scope_id="ogilvy_default",
                market_scope=("za",),
                brand_config_id=None,
                audience_lens_ids=(),
                theme_id=None,
                path=path,
            )
        assert str(caught.value) == "scope_invalid"
