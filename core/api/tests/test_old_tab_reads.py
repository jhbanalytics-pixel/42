"""Old browser tabs (C2 v2.1, CT-06): a tab loaded from the live a80be1d release keeps reading this release's f42-api
routes until it reloads. Today, Discover, Radar, Topic, Coverage, Fieldwork and History are served by f42-api itself, so
nothing forwards them to an older service; this file is what would catch a head response that drops or retypes a key
the a80 pages read.

old_tab_pins.json holds, for each endpoint those pages call (the five history reads one apiece), every key path the
a80be1d f42-api sent over core/api/fixtures, with the clock at 2026-09-30 08:00 SAST, that the a80 page sources name,
with the JSON types seen across the endpoint's requests. An a80 client reads head's answers to the same requests: every
pinned path must be present and its type one the a80 response had. New keys are allowed. Types are null, bool, int,
float, string, array and object; a whole number where a80 sent a fraction counts as a change, because a page can test
Number.isInteger. The pins and the request list are data, not recomputed here."""
import datetime as dt
import importlib
import json
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.api import app as api_mod
from core.api import auth

PASS = "old-tab-pass-4Hq7"
PINS = json.loads(Path(__file__).with_name("old_tab_pins.json").read_text(encoding="utf-8"))
ROUTES = ["today", "discover", "radar", "topic", "coverage", "fieldwork", "history_asks", "history_briefs",
          "history_findings", "history_item", "history_search"]
FIXED = dt.datetime(2026, 9, 30, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
CLOCK_MODULES = ("today", "discover", "fieldwork", "store", "history", "coverage")


class FixedClock(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED.astimezone(tz) if tz else FIXED.replace(tzinfo=None)


def type_name(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    return "array" if isinstance(value, list) else "object"


def walk(value, path, seen):
    seen.setdefault(path, set()).add(type_name(value))
    if isinstance(value, dict):
        for key, child in value.items():
            walk(child, f"{path}.{key}" if path else key, seen)
    elif isinstance(value, list):
        for child in value:
            walk(child, path + "[]", seen)


def problems_of(pins, bodies):
    """What an a80 page would miss: each pinned path absent from every body, or typed outside what a80 sent."""
    seen = {}
    for body in bodies:
        walk(body, "", seen)
    found = []
    for path, wanted in pins.items():
        if path not in seen:
            found.append(f"{path or '(root)'}: missing")
        elif not seen[path] <= set(wanted.split("|")):
            found.append(f"{path or '(root)'}: {'|'.join(sorted(seen[path]))} is not one of {wanted}")
    return found


def today_problems_of(pins, bodies):
    for body in bodies:
        for market in body["markets"]:
            for item in market["held_back"]["items"]:
                assert item["claims"] == []
                assert item["explanation"] is None
                assert item["explanation_claim_ids"] == []
    empty_contents = ("markets[].held_back.items[].claims[]", "markets[].held_back.items[].explanation_claim_ids[]")
    return problems_of({path: wanted for path, wanted in pins.items()
                        if not any(path.startswith(prefix) for prefix in empty_contents)}, bodies)


@pytest.fixture(scope="module")
def head():
    """Head's answer to every request of every route, on fixtures, with the clock the pins were taken at."""
    patch = pytest.MonkeyPatch()
    from core.api import store as store_mod
    from core.api.tests.test_today import FixtureStore

    patch.setattr(store_mod, "get_store", lambda: FixtureStore())
    patch.setenv("UI_PASSCODE", PASS)
    patch.setenv("F42_DATA", "fixtures")
    patch.setenv("F42_AGENT", "fixture")
    patch.setenv("F42_FIXTURE_DELAY", "0")
    patch.delenv("AGENT_URL", raising=False)
    for name in CLOCK_MODULES:
        importlib.import_module(f"core.api.{name}")
    shim = types.SimpleNamespace(**{k: getattr(dt, k) for k in dir(dt) if not k.startswith("__")})
    shim.datetime = FixedClock
    for name, module in list(sys.modules.items()):
        if name.startswith("core.api") and getattr(module, "dt", None) is dt:
            patch.setattr(module, "dt", shim)
    auth.auth_limiter.hits.clear()
    client = TestClient(api_mod.app)
    answers = {}
    for route in ROUTES:
        for path in PINS["requests"][route]:
            r = client.get(path, headers={"X-Passcode": PASS})
            answers[path] = (r.status_code, r.json())
    patch.undo()
    return answers


def test_the_pins_cover_the_seven_pages_routes_and_every_history_read():
    assert sorted(PINS["requests"]) == sorted(ROUTES) == sorted(PINS["pins"])
    asked = [p.split("?")[0] for r in ROUTES for p in PINS["requests"][r]]
    for read in ("/api/today", "/api/discover", "/api/discover/radar", "/api/topics/", "/api/coverage", "/api/fieldwork",
                 "/api/history/asks", "/api/history/briefs", "/api/history/findings", "/api/history/items/",
                 "/api/history/search"):
        assert any(p == read or (read.endswith("/") and p.startswith(read)) for p in asked), read
    assert all(len(PINS["pins"][r]) >= 4 for r in ROUTES)


# The count of pinned key paths per route, written out. Deleting a pin from old_tab_pins.json (or from the route that
# held it) lowers a count and fails here, so the suite cannot go green by losing the pin that would have failed.
PIN_COUNTS = {"today": 209, "discover": 163, "radar": 63, "topic": 143, "coverage": 116, "fieldwork": 83,
              "history_asks": 10, "history_briefs": 19, "history_findings": 26, "history_item": 54,
              "history_search": 4}
PIN_TOTAL = 890


def test_the_pins_are_counted_per_route_and_in_all():
    assert sorted(PIN_COUNTS) == sorted(ROUTES)
    assert {route: len(PINS["pins"][route]) for route in ROUTES} == PIN_COUNTS
    assert sum(PIN_COUNTS.values()) == PIN_TOTAL == sum(len(pins) for pins in PINS["pins"].values())


@pytest.mark.parametrize("route", ROUTES)
def test_every_request_an_old_tab_makes_answers_with_the_status_a80_gave(head, route):
    wrong = {p: (head[p][0], s) for p, s in PINS["requests"][route].items() if head[p][0] != s}
    assert wrong == {}, wrong


@pytest.mark.parametrize("route", ROUTES)
def test_head_keeps_every_key_the_a80_page_reads_with_its_a80_type(head, route):
    bodies = [head[p][1] for p, s in PINS["requests"][route].items() if s == 200]
    found = (today_problems_of if route == "today" else problems_of)(PINS["pins"][route], bodies)
    assert found == [], found[:20]


@pytest.mark.parametrize("narrative", [
    {"claims": [{"text": "raw held claim"}]},
    {"claims": [{"quotes": [{"text": "raw held quote"}]}]},
    {"claims": [{"numbers": [{"value": 1, "unit": "raw held number text"}]}]},
    {"explanation": "raw held explanation"},
    {"explanation_claim_ids": ["raw held claim id"]},
])
def test_today_compatibility_rejects_raw_held_narrative(narrative):
    item = {"claims": [], "explanation": None, "explanation_claim_ids": [], **narrative}
    with pytest.raises(AssertionError):
        today_problems_of({}, [{"markets": [{"held_back": {"items": [item]}}]}])


def test_today_empty_claim_compatibility_keeps_unrelated_pins_and_populated_checks():
    prefix = "markets[].held_back.items[]"
    pins = {prefix + ".claims": "array", prefix + ".claims[].text": "string",
            prefix + ".explanation": "null|string", prefix + ".explanation_claim_ids": "array",
            prefix + ".evidence": "array"}
    item = {"claims": [], "explanation": None, "explanation_claim_ids": [], "evidence": []}
    body = {"markets": [{"held_back": {"items": [item]}}]}
    assert today_problems_of(pins, [body]) == []
    assert problems_of(pins, [body]) == [prefix + ".claims[].text: missing"]
    item.pop("evidence")
    assert today_problems_of(pins, [body]) == [prefix + ".evidence: missing"]
    assert problems_of({"claims[].text": "string"}, [{"claims": [{"text": 1}]}]) == [
        "claims[].text: int is not one of string"]


def test_the_check_names_a_missing_key_a_changed_type_and_a_number_that_lost_its_fraction():
    pins = {"": "object", "a": "int", "b": "string|null", "c": "float", "d[]": "object", "d[].x": "string"}
    body = {"a": 1, "b": "t", "c": 0.5, "d": [{"x": "y", "extra": 1}], "new": True}
    assert problems_of(pins, [body]) == []
    assert problems_of(pins, [{**body, "a": None}]) == ["a: null is not one of int"]
    assert problems_of(pins, [{**body, "c": 1}]) == ["c: int is not one of float"]
    assert problems_of(pins, [{**body, "b": 3}]) == ["b: int is not one of string|null"]
    gone = {k: v for k, v in body.items() if k != "d"}
    assert problems_of(pins, [gone]) == ["d[]: missing", "d[].x: missing"]
    assert problems_of(pins, [{**body, "d": []}]) == ["d[]: missing", "d[].x: missing"]
    assert problems_of(pins, [{**body, "d": [{}]}]) == ["d[].x: missing"]
    assert problems_of(pins, [[]]) == ["(root): array is not one of object"] + [f"{p}: missing" for p in pins if p]


def test_an_old_tab_pin_set_that_a_response_misses_is_found_in_a_real_route(head):
    """The pins really bite on a head response: drop one key the Today page reads and the check names it."""
    bodies = [json.loads(json.dumps(head[p][1])) for p, s in PINS["requests"]["today"].items() if s == 200]
    for body in bodies:
        for market in body["markets"]:
            market.pop("held_back", None)
    found = problems_of(PINS["pins"]["today"], bodies)
    assert any(line.startswith("markets[].held_back") for line in found), found[:5]
