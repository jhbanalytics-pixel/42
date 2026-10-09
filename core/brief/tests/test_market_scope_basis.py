"""Ruling M4: market_scope_basis is present wherever the brief writes market_scope (a card, a not_assessed item) once the
locality authority is v2 or the row was admitted under locality_v2.1, and it is absent under the v1 authority for a row on
the v1 basis, so the shadow payload is what it was. The value is the rule that wrote the scope: locality_v2.1 or v1."""

import json

import pytest

from core.brief import job
from core.brief.tests.test_brief_golden_path import HonestModel, brief, world
from core.brief.tests.test_locality_authority import V2, v2_world, run_brief
from core.brief.tests.test_locality_shadow import add_locality
from core.brief.tests.test_selection_audit import candidates, selected_rows
from core.trust import locality


@pytest.fixture
def authority(monkeypatch):
    """The locality authority constant, patched where the brief reads it."""
    def set_to(value):
        monkeypatch.setattr(locality, "LOCALITY_AUTHORITY", value)
    return set_to


@pytest.mark.parametrize(("authority_value", "row_basis", "want"), [
    ("v1", None, None), ("v1", "v1", None), ("v1", V2, V2),
    ("v2", None, "v1"), ("v2", "v1", "v1"), ("v2", V2, V2)])
def test_scope_basis_by_authority_and_row_basis(authority, authority_value, row_basis, want):
    authority(authority_value)
    assert locality.scope_basis(row_basis) == want


def test_under_v1_a_card_on_the_v1_basis_has_no_basis_key(monkeypatch, authority):
    authority("v1")
    _, payload, _ = brief(world(located=True), HonestModel(True), monkeypatch)
    [card] = payload["cards"]
    assert "market_scope" in card and "market_scope_basis" not in card and "pack_scope_v1" not in card


def test_under_v2_a_card_on_the_v1_basis_says_v1(monkeypatch, authority):
    authority("v2")
    _, payload, _ = brief(world(located=True), HonestModel(True), monkeypatch)
    [card] = payload["cards"]
    assert card["market_scope_basis"] == "v1" and "pack_scope_v1" not in card
    assert card["market_scope"] == "market"                    # the v1 scope is what decided it


def test_a_card_on_the_v2_basis_says_locality_v2_1_under_either_constant(monkeypatch, authority):
    for value in ("v1", "v2"):
        authority(value)
        con = v2_world(status="local")
        add_locality(con, 20, 20)
        _, payload, _, _ = run_brief(con, monkeypatch)
        [card] = payload["cards"]
        assert (card["market_scope_basis"], card["pack_scope_v1"]["market_scope"]) == (V2, "market")


@pytest.mark.parametrize(("authority_value", "want"), [("v1", None), ("v2", "v1")])
def test_a_not_assessed_item_carries_the_basis_beside_its_scope(monkeypatch, authority, authority_value, want):
    authority(authority_value)
    _, _, _, audits = candidates(monkeypatch, selected_rows())
    items = audits["NG"]["not_assessed"]["items"]
    assert items and all("market_scope" in i for i in items)
    assert [i.get("market_scope_basis") for i in items] == [want] * len(items)


def test_a_not_assessed_item_of_a_run_written_under_v2_says_so(monkeypatch, authority):
    authority("v1")
    rows = [dict(r, locality_basis=V2) for r in selected_rows()]
    _, _, _, audits = candidates(monkeypatch, rows)
    assert {i.get("market_scope_basis") for i in audits["NG"]["not_assessed"]["items"]} == {V2}
