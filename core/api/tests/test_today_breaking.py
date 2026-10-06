"""Breaking on Today (contract.md section 4, Breaking): the items the hourly Breaking rule judged Breaking in the
last few hours, read from v_breaking_signals_current, listed per market apart from the morning cards. Empty when
there are none, plain words only, the suppression list applied, and a failed read never fails Today."""
import datetime as dt
import hashlib
import json
import logging
import re

import pytest

from core.api import today
from core.api import store as store_mod
from core.api.store import BigQueryStore, FixtureStore

D30 = "2026-09-30"
SAST = dt.timezone(dt.timedelta(hours=2))
# Two hours after the ZA fixture row's hour ended (13:00 to 14:00 SAST), four after the NG row's.
NOW = dt.datetime(2026, 9, 30, 16, 0, tzinfo=SAST)
ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def iid(kind, key):
    return hashlib.sha256(f"{kind}|{key}".encode("utf-8")).hexdigest()


ZA_STEP = iid("hashtag", "#fixture_za_step")
NG_OWAMBE = iid("hashtag", "#fixture_ng_owambe")
HIDDEN_CREATOR = iid("creator", "tiktok:fixture_ng_hidden")
OPEN_CREATOR = iid("creator", "tiktok:fixture_ng_open")


class Patched(FixtureStore):
    def __init__(self, **overrides):
        super().__init__()
        for name, fn in overrides.items():
            setattr(self, name, fn)


@pytest.fixture(autouse=True)
def clean():
    store_mod.SUPPRESSIONS.clear()
    yield
    store_mod.SUPPRESSIONS.clear()


def market(obj, code):
    return next(m for m in obj["markets"] if m["market"] == code)


def without(obj, key):
    """obj with key left out of every market, to compare Today with and without Breaking."""
    return {**obj, "markets": [{k: v for k, v in m.items() if k != key} for m in obj["markets"]]}


def test_fixture_store_reads_breaking_rows_since():
    fx = FixtureStore()
    rows = fx.breaking_signals("2026-09-30T10:00:00+02:00")
    assert {(r["market"], r["item_id"]) for r in rows} == {("ZA", ZA_STEP), ("NG", NG_OWAMBE)}
    assert [r["market"] for r in fx.breaking_signals("2026-09-30T12:00:00+02:00")] == ["ZA"]
    assert fx.breaking_signals("2026-09-30T15:00:00+02:00") == []


def test_fixture_store_without_the_file_reads_none(tmp_path):
    assert FixtureStore(root=tmp_path).breaking_signals("2026-09-30T10:00:00+02:00") is None


def test_today_lists_breaking_items_per_market():
    t = today.build_today(FixtureStore(), now=NOW)
    za, ng, ke = market(t, "ZA"), market(t, "NG"), market(t, "KE")
    assert za["breaking"] == [{"item_id": ZA_STEP, "market": "ZA", "market_label": "South Africa", "kind": "hashtag",
                               "title": "#fixture_za_step", "time_text": "14:00", "ago_text": "2 hours ago",
                               "note": None}]
    assert [b["item_id"] for b in ng["breaking"]] == [NG_OWAMBE]
    assert ng["breaking"][0]["title"] == "#fixture_ng_owambe"
    assert ng["breaking"][0]["ago_text"] == "4 hours ago"
    # expected6 0: never a ratio, always this label (core/detect/breaking.py)
    assert ng["breaking"][0]["note"] == "New, no prior posts"
    assert ke["breaking"] == []


def test_breaking_lines_carry_no_iso_times_ratios_or_counts():
    t = today.build_today(FixtureStore(), now=NOW)
    for m in t["markets"]:
        for b in m["breaking"]:
            assert set(b) == {"item_id", "market", "market_label", "kind", "title", "time_text", "ago_text", "note"}
            shown = [b["title"], b["market_label"], b["time_text"], b["ago_text"], b["note"] or ""]
            assert not any(ISO.search(s) for s in shown)
            assert b["title"] != b["item_id"]


def test_breaking_is_empty_when_no_current_rows():
    late = dt.datetime(2026, 10, 2, 9, 0, tzinfo=SAST)
    t = today.build_today(FixtureStore(), now=late)
    assert all(m["breaking"] == [] for m in t["markets"])
    gone = Patched(breaking_signals=lambda since: None)  # the view does not exist yet
    assert all(m["breaking"] == [] for m in today.build_today(gone, now=NOW)["markets"])
    empty = Patched(breaking_signals=lambda since: [])
    assert all(m["breaking"] == [] for m in today.build_today(empty, now=NOW)["markets"])


def test_breaking_reads_only_the_last_hours():
    asked = []

    def rows(since):
        asked.append(since)
        return FixtureStore().breaking_signals(since)

    today.build_today(Patched(breaking_signals=rows), now=NOW)
    assert asked == ["2026-09-30T10:00:00+02:00"]


def test_breaking_rows_from_the_store_outside_the_window_or_market_are_left_out():
    rows = [
        {"hour": "2026-09-30T13:00:00+02:00", "market": "ZA", "item_id": ZA_STEP, "ratio": 4.0, "expected6": 2.0},
        {"hour": "2026-09-30T03:00:00+02:00", "market": "NG", "item_id": NG_OWAMBE, "ratio": 4.0, "expected6": 2.0},
        {"hour": "2026-09-30T13:00:00+02:00", "market": "GH", "item_id": NG_OWAMBE, "ratio": 4.0, "expected6": 2.0},
        {"hour": "2026-09-30T12:00:00+02:00", "market": "ZA", "item_id": ZA_STEP, "ratio": 3.5, "expected6": 2.0},
    ]
    t = today.build_today(Patched(breaking_signals=lambda since: rows), now=NOW)
    za = market(t, "ZA")["breaking"]
    assert [(b["item_id"], b["time_text"]) for b in za] == [(ZA_STEP, "14:00")]  # one line per item, its latest hour
    assert market(t, "NG")["breaking"] == [] and market(t, "KE")["breaking"] == []


def test_breaking_newest_first_and_within_the_hour_words():
    rows = [
        {"hour": "2026-09-30T12:00:00+02:00", "market": "ZA", "item_id": ZA_STEP, "ratio": 4.0, "expected6": 2.0},
        {"hour": "2026-09-30T15:00:00+02:00", "market": "ZA", "item_id": iid("topic", "fixture za rising topic"),
         "ratio": 4.0, "expected6": 2.0},
    ]
    t = today.build_today(Patched(breaking_signals=lambda since: rows), now=NOW)
    za = market(t, "ZA")["breaking"]
    assert [b["title"] for b in za] == ["fixture za rising topic", "#fixture_za_step"]
    assert [b["ago_text"] for b in za] == ["In the last hour", "3 hours ago"]


def test_breaking_without_a_readable_label_says_its_kind_never_its_id():
    rows = [{"hour": "2026-09-30T13:00:00+02:00", "market": "ZA", "item_id": "f" * 64, "ratio": 4.0,
             "expected6": 2.0}]
    t = today.build_today(Patched(breaking_signals=lambda since: rows,
                                  map_items=lambda ids: [{"item_id": "f" * 64, "kind": "sound", "label": "f" * 64}]),
                          now=NOW)
    assert market(t, "ZA")["breaking"][0]["title"] == "Sound item"
    t = today.build_today(Patched(breaking_signals=lambda since: rows, map_items=lambda ids: None), now=NOW)
    assert market(t, "ZA")["breaking"][0]["title"] == "Trend item"


def creator_rows():
    rows = [{"hour": "2026-09-30T13:00:00+02:00", "market": "NG", "item_id": item, "ratio": 4.0, "expected6": 2.0}
            for item in (HIDDEN_CREATOR, OPEN_CREATOR, NG_OWAMBE)]
    labels = [{"item_id": HIDDEN_CREATOR, "kind": "creator", "canonical_key": "tiktok:fixture_ng_hidden",
               "label": "fixture_ng_hidden"},
              {"item_id": OPEN_CREATOR, "kind": "creator", "canonical_key": "tiktok:fixture_ng_open",
               "label": "fixture_ng_open"},
              {"item_id": NG_OWAMBE, "kind": "hashtag", "canonical_key": "#fixture_ng_owambe",
               "label": "#fixture_ng_owambe with @fixture_ng_hidden"}]
    return rows, labels


def test_breaking_leaves_out_a_suppressed_creator_and_masks_their_handle():
    rows, labels = creator_rows()
    t = today.build_today(Patched(breaking_signals=lambda since: rows, map_items=lambda ids: labels), now=NOW)
    ng = market(t, "NG")["breaking"]
    assert {b["item_id"] for b in ng} == {OPEN_CREATOR, NG_OWAMBE}
    assert "fixture_ng_hidden" not in json.dumps(ng).lower()


def test_breaking_names_no_creator_when_the_suppression_list_cannot_be_read():
    rows, labels = creator_rows()

    def broken():
        raise RuntimeError("suppression view unreadable")

    t = today.build_today(Patched(breaking_signals=lambda since: rows, map_items=lambda ids: labels,
                                  suppressed_creators=broken), now=NOW)
    ng = market(t, "NG")["breaking"]
    assert [b["item_id"] for b in ng] == [NG_OWAMBE]
    assert "fixture_ng_hidden" not in json.dumps(ng).lower()


def test_a_failed_breaking_read_leaves_today_intact(caplog):
    def broken(since):
        raise RuntimeError("view gone")

    base = today.build_today(FixtureStore(), now=NOW)
    with caplog.at_level(logging.WARNING, logger="core.api.today"):
        t = today.build_today(Patched(breaking_signals=broken), now=NOW)
    assert all(m["breaking"] == [] for m in t["markets"])
    assert without(t, "breaking") == without(base, "breaking")
    assert any("breaking" in r.getMessage().lower() for r in caplog.records)


def test_a_failed_label_read_leaves_today_intact(caplog):
    def broken(ids):
        raise RuntimeError("map gone")

    with caplog.at_level(logging.WARNING, logger="core.api.today"):
        t = today.build_today(Patched(map_items=broken), now=NOW)
    assert t["markets"] and all(m["breaking"] == [] for m in t["markets"])


def test_breaking_is_not_read_for_a_past_brief():
    asked = []

    def rows(since):
        asked.append(since)
        return []

    t = today.build_today(Patched(breaking_signals=rows), D30, now=dt.datetime(2026, 10, 2, 9, 0, tzinfo=SAST))
    assert asked == [] and all(m["breaking"] == [] for m in t["markets"])
    today.build_today(Patched(breaking_signals=rows), D30, now=NOW)
    assert len(asked) == 1


class FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class FakeClient:
    def __init__(self, rows=None):
        self.calls, self.rows = [], rows or []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return FakeJob(self.rows)


def test_bigquery_store_reads_the_current_breaking_view(monkeypatch):
    client = FakeClient([{"hour": dt.datetime(2026, 9, 30, 11, 0, tzinfo=dt.timezone.utc), "market": "ZA",
                          "item_id": ZA_STEP, "ratio": None, "expected6": 0.0}])
    bq = BigQueryStore(client=client)
    monkeypatch.setattr(bq, "_find", lambda name: f"`p.intelligence_42_core.{name}`")
    rows = bq.breaking_signals("2026-09-30T10:00:00+02:00")
    sql, cfg = client.calls[0]
    assert "v_breaking_signals_current" in sql and "@since" in sql and "2026-09-30" not in sql
    assert cfg.maximum_bytes_billed == 2_000_000_000
    assert rows[0]["hour"] == "2026-09-30T13:00:00+02:00"
    monkeypatch.setattr(bq, "_find", lambda name: None)
    assert bq.breaking_signals("2026-09-30T10:00:00+02:00") is None
