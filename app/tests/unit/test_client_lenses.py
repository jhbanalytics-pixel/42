"""The app reads the same authorized lens registry the engine enforces.

The app decides what a console may offer and what it records at a boundary; the
engine re-resolves the same entry against the configuration bytes it holds. The
two must therefore read one registry, so this suite pins the app copy to the
engine copy and to the configuration document those bytes belong to.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from src.api import client_lenses

REPOSITORY = Path(__file__).resolve().parents[3]
ENGINE_REGISTRY = REPOSITORY / "engine" / "configs" / "client_lenses.yaml"
ENGINE_OVERLAY = REPOSITORY / "engine" / "configs" / "topic_overlays" / "bsa.yaml"
BSA_LENS = "bsa_pulse_lens"
OVERLAY_DIGEST = "e1baafa865b5c9419225102d752c651c5b6e4fada034a2317f2d6f8a395336bf"


def test_the_app_and_engine_read_one_registry_pinned_to_the_configuration_bytes():
    assert ENGINE_REGISTRY.is_file(), "the engine registry is the same file the app serves"
    app_bytes = client_lenses.CLIENT_LENSES_PATH.read_bytes()
    assert app_bytes == ENGINE_REGISTRY.read_bytes()
    assert hashlib.sha256(app_bytes).hexdigest() == client_lenses.CONFIG_DIGEST
    entry = yaml.safe_load(app_bytes)["lenses"][BSA_LENS]
    assert entry["configuration_digest"] == OVERLAY_DIGEST
    assert ENGINE_OVERLAY.is_file()
    assert hashlib.sha256(ENGINE_OVERLAY.read_bytes()).hexdigest() == OVERLAY_DIGEST


def test_general_42_is_the_default_and_carries_no_lens():
    assert client_lenses.DEFAULT_CLIENT_LENS_ID is None
    assert (
        client_lenses.resolve_client_lens(
            client_scope_id="ogilvy_default", brand_config_id=None, client_lens_id=None
        )
        is None
    )


def test_the_console_roster_offers_only_this_scope_s_authorized_lenses():
    assert client_lenses.authorized_client_lenses(client_scope_id="ogilvy_default") == []
    roster = client_lenses.authorized_client_lenses(client_scope_id="bsa_pulse")
    assert roster == [
        {
            "client_lens_id": BSA_LENS,
            "label": "Brand South Africa Pulse",
            "configuration_digest": OVERLAY_DIGEST,
        }
    ]


def test_the_authorized_lens_resolves_and_a_brand_name_alone_does_not():
    lens = client_lenses.resolve_client_lens(
        client_scope_id="bsa_pulse", brand_config_id="bsa", client_lens_id=BSA_LENS
    )
    assert lens.client_lens_id == BSA_LENS
    assert lens.configuration_digest == OVERLAY_DIGEST
    assert lens.envelope == {
        "lens_binding_version": "client_lens_binding_v1",
        "client_lens_id": BSA_LENS,
        "configuration_digest": OVERLAY_DIGEST,
    }
    assert (
        client_lenses.resolve_client_lens(
            client_scope_id="bsa_pulse", brand_config_id="bsa", client_lens_id=None
        )
        is None
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"client_scope_id": "ogilvy_default"},
        {"client_scope_id": ""},
        {"brand_config_id": None},
        {"brand_config_id": "other_brand"},
        {"client_lens_id": "bsa"},
        {"client_lens_id": "unregistered_lens"},
        {"client_lens_id": "BSA_PULSE_LENS"},
        {"client_lens_id": ""},
        {"client_lens_id": 7},
    ],
)
def test_an_unauthorized_lens_is_refused(overrides):
    values = {
        "client_scope_id": "bsa_pulse",
        "brand_config_id": "bsa",
        "client_lens_id": BSA_LENS,
    }
    values.update(overrides)
    with pytest.raises(client_lenses.ClientLensUnavailable):
        client_lenses.resolve_client_lens(**values)


def test_a_changed_registry_file_is_refused(tmp_path):
    changed = tmp_path / "client_lenses.yaml"
    changed.write_text(
        client_lenses.CLIENT_LENSES_PATH.read_text(encoding="utf-8").replace(
            "bsa_pulse", "other_scope"
        ),
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(client_lenses.ClientLensUnavailable):
        client_lenses.resolve_client_lens(
            client_scope_id="bsa_pulse",
            brand_config_id="bsa",
            client_lens_id=BSA_LENS,
            path=changed,
        )
    with pytest.raises(client_lenses.ClientLensUnavailable):
        client_lenses.authorized_client_lenses(client_scope_id="bsa_pulse", path=changed)


def _registry(tmp_path, monkeypatch, mutate):
    """A registry the loader will read: the byte pin moves with the bytes.

    The shipped pin shields every shape check below it, so each of those checks
    is only observable once the pin is satisfied for the document under test.
    """
    document = yaml.safe_load(client_lenses.CLIENT_LENSES_PATH.read_text(encoding="utf-8"))
    mutate(document)
    path = tmp_path / "client_lenses.yaml"
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(
        client_lenses, "CONFIG_DIGEST", hashlib.sha256(path.read_bytes()).hexdigest()
    )
    return path


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda doc: doc.update(contract_version="client_lens_registry_v0"),
            id="contract_version",
        ),
        pytest.param(
            lambda doc: doc.update(default_client_lens_id=BSA_LENS), id="default_lens"
        ),
        pytest.param(lambda doc: doc.update(default_client_lens_id=""), id="default_empty"),
        pytest.param(lambda doc: doc.update(lenses={}), id="no_lenses"),
        pytest.param(lambda doc: doc.update(lenses=[]), id="lenses_not_a_mapping"),
        pytest.param(
            lambda doc: doc["lenses"][BSA_LENS].pop("overlay_id"), id="missing_field"
        ),
        pytest.param(
            lambda doc: doc["lenses"][BSA_LENS].update(extra="x"), id="extra_field"
        ),
        pytest.param(
            lambda doc: doc["lenses"].__setitem__("other_lens", doc["lenses"].pop(BSA_LENS)),
            id="key_is_not_the_entry",
        ),
        pytest.param(
            lambda doc: doc["lenses"][BSA_LENS].update(enabled="true"), id="enabled_not_a_bool"
        ),
        pytest.param(
            lambda doc: doc["lenses"][BSA_LENS].update(client_scope_id="   "), id="blank_scope"
        ),
        pytest.param(
            lambda doc: doc["lenses"][BSA_LENS].update(label=7), id="label_not_a_string"
        ),
    ],
)
def test_every_registry_shape_check_refuses_once_the_byte_pin_is_satisfied(
    tmp_path, monkeypatch, mutate
):
    path = _registry(tmp_path, monkeypatch, mutate)
    with pytest.raises(client_lenses.ClientLensUnavailable) as caught:
        client_lenses.authorized_client_lenses(client_scope_id="bsa_pulse", path=path)
    assert caught.value.code == "client_lens_registry_invalid"


def test_a_disabled_entry_is_offered_to_nobody_and_resolves_for_nobody(
    tmp_path, monkeypatch
):
    path = _registry(
        tmp_path, monkeypatch, lambda doc: doc["lenses"][BSA_LENS].update(enabled=False)
    )

    assert client_lenses.authorized_client_lenses(client_scope_id="bsa_pulse", path=path) == []
    with pytest.raises(client_lenses.ClientLensUnavailable) as caught:
        client_lenses.resolve_client_lens(
            client_scope_id="bsa_pulse",
            brand_config_id="bsa",
            client_lens_id=BSA_LENS,
            path=path,
        )
    assert caught.value.code == "client_lens_unauthorized"


def test_the_roster_offers_only_the_scope_the_entry_names(tmp_path, monkeypatch):
    path = _registry(
        tmp_path,
        monkeypatch,
        lambda doc: doc["lenses"][BSA_LENS].update(client_scope_id="other_scope"),
    )

    assert client_lenses.authorized_client_lenses(client_scope_id="bsa_pulse", path=path) == []
    assert [
        entry["client_lens_id"]
        for entry in client_lenses.authorized_client_lenses(
            client_scope_id="other_scope", path=path
        )
    ] == [BSA_LENS]


@pytest.mark.parametrize("client_lens_id", [7, 0, True, b"bsa_pulse_lens", ["bsa_pulse_lens"], ""])
def test_a_lens_id_that_is_not_a_nonempty_string_is_refused_by_shape(client_lens_id):
    """The strict string check is the refusal, so the code names the shape."""
    with pytest.raises(client_lenses.ClientLensUnavailable) as caught:
        client_lenses.resolve_client_lens(
            client_scope_id="bsa_pulse", brand_config_id="bsa", client_lens_id=client_lens_id
        )
    assert caught.value.code == "client_lens_id_invalid"


def test_a_stored_binding_is_named_only_for_its_own_client_scope():
    assert (
        client_lenses.registered_label(BSA_LENS, OVERLAY_DIGEST, client_scope_id="bsa_pulse")
        == "Brand South Africa Pulse"
    )
    for scope in ("ogilvy_default", "other_scope", None, ""):
        assert (
            client_lenses.registered_label(BSA_LENS, OVERLAY_DIGEST, client_scope_id=scope)
            is None
        )


def test_a_disabled_entry_names_no_stored_binding(tmp_path, monkeypatch):
    path = _registry(
        tmp_path, monkeypatch, lambda doc: doc["lenses"][BSA_LENS].update(enabled=False)
    )

    assert (
        client_lenses.registered_label(
            BSA_LENS, OVERLAY_DIGEST, client_scope_id="bsa_pulse", path=path
        )
        is None
    )


def test_an_entry_moved_to_another_scope_names_no_stored_binding_here(tmp_path, monkeypatch):
    path = _registry(
        tmp_path,
        monkeypatch,
        lambda doc: doc["lenses"][BSA_LENS].update(client_scope_id="other_scope"),
    )

    assert (
        client_lenses.registered_label(
            BSA_LENS, OVERLAY_DIGEST, client_scope_id="bsa_pulse", path=path
        )
        is None
    )
    assert (
        client_lenses.registered_label(
            BSA_LENS, OVERLAY_DIGEST, client_scope_id="other_scope", path=path
        )
        == "Brand South Africa Pulse"
    )
