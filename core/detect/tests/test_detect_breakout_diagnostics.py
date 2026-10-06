"""Auditable eligibility counts for creator breakouts, with no identity details."""

from datetime import datetime, timedelta, timezone

from core.detect import breakout


D = datetime(2026, 9, 30, tzinfo=timezone.utc)


def _creator_rows(index, *, tier="micro", coord_score=0, geo_market="ZA", geo_confidence=0.7,
                  home_market="NG", handle=None):
    creator_id = f"creator-{index}"
    handle = handle or creator_id
    target_published = D - timedelta(days=2)
    rows = []
    for prior_index in range(5):
        published = target_published - timedelta(days=prior_index + 1)
        rows.append({
            "post_id": f"prior-{index}-{prior_index}",
            "platform": "tiktok",
            "creator_id": creator_id,
            "published_at": published,
            "first_read_at": published + timedelta(days=1),
            "first_views": None,
            "in_window": False,
            "base_views": 500,
            "base_read_at": published + timedelta(days=1),
            "tier": tier,
            "coord_score": coord_score,
            "geo_market": geo_market,
            "geo_confidence": geo_confidence,
            "home_market": home_market,
            "handle": handle,
        })
    rows.append({
        "post_id": f"breakout-{index}",
        "platform": "tiktok",
        "creator_id": creator_id,
        "published_at": target_published,
        "first_read_at": target_published + timedelta(days=1),
        "first_views": 2000,
        "in_window": True,
        "base_views": None,
        "base_read_at": None,
        "tier": tier,
        "coord_score": coord_score,
        "geo_market": geo_market,
        "geo_confidence": geo_confidence,
        "home_market": home_market,
        "handle": handle,
    })
    return rows


def _sighting(post_id, *, market="ZA", item_id="tiktok:sound-1"):
    return {"post_id": post_id, "kind": "sound", "canonical_key": "sound-1",
            "item_id": item_id, "market": market}


def _install_reader(monkeypatch, state):
    monkeypatch.setattr(breakout, "queries", lambda: ("posts", "sightings"))

    def query(_client, sql, _params, *_args, **_kwargs):
        if sql == "posts":
            return state["posts"]
        if sql == "sightings":
            return state["sightings"]
        if sql == breakout.APPENDED_SQL:
            return [{"n": 1}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(breakout, "query", query)


def test_diagnostics_explain_seventeen_large_breakouts_without_joined_sightings(monkeypatch):
    posts = [row for index in range(17)
             for row in _creator_rows(index, tier="mega" if index < 15 else "macro")]
    sightings = [_sighting(f"unmatched-{index}") for index in range(298)]
    state = {"posts": posts, "sightings": sightings}
    _install_reader(monkeypatch, state)

    out = breakout.run_breakout(object(), D.date(), "run-1", "r1")

    assert len(out["breakouts"]) == 17
    assert out["signals"] == []
    assert out["eligibility"] == {
        "posts": {
            "input_rows": 102,
            "in_window": 17,
            "with_first_views": 17,
            "with_five_prior": 17,
            "at_least_1000_views": 17,
            "at_least_three_times_usual": 17,
            "breakouts": 17,
            "small_breakouts": 0,
            "breakout_tiers": {"nano": 0, "micro": 0, "macro": 2, "mega": 15, "other": 0},
        },
        "sightings": {
            "input_rows": 298,
            "for_breakouts": 0,
            "non_generic": 0,
            "small": 0,
            "local": 0,
            "unflagged_local": 0,
            "flagged_local": 0,
            "item_groups": 0,
            "max_distinct_unflagged_creators": 0,
            "qualifying_item_groups": 0,
        },
        "empty_reason": "no_breakout_sightings",
    }
    assert all(isinstance(value, int) for key, value in out["eligibility"]["posts"].items()
               if key != "breakout_tiers")
    assert all(isinstance(value, int) for value in out["eligibility"]["posts"]["breakout_tiers"].values())
    assert all(isinstance(value, int) for value in out["eligibility"]["sightings"].values())


def test_empty_reason_tracks_the_first_gate_that_eliminates_candidates(monkeypatch):
    cases = [
        ("no_sound_format_sightings", "micro", 0, "ZA", 0.7, "NG", []),
        ("no_small_creator_breakouts", "macro", 0, "ZA", 0.7, "NG", [_sighting("breakout-0")]),
        ("no_local_sightings", "micro", 0, "ZA", 0.69, "NG", [_sighting("breakout-0")]),
        ("only_coordinated_creators", "micro", 1, "ZA", 0.7, "NG", [_sighting("breakout-0")]),
    ]
    for reason, tier, coord, geo, confidence, home, sightings in cases:
        state = {"posts": _creator_rows(0, tier=tier, coord_score=coord, geo_market=geo,
                                         geo_confidence=confidence, home_market=home),
                 "sightings": sightings}
        _install_reader(monkeypatch, state)

        out = breakout.run_breakout(object(), D.date(), "run-1", "r1")

        assert out["signals"] == []
        assert out["eligibility"]["empty_reason"] == reason


def test_two_distinct_handles_fail_the_creator_gate(monkeypatch):
    posts = (_creator_rows(0, handle="same") + _creator_rows(1, handle="@SAME") +
             _creator_rows(2, handle="third"))
    sightings = [_sighting(f"breakout-{index}") for index in range(3)]
    state = {"posts": posts, "sightings": sightings}
    _install_reader(monkeypatch, state)

    out = breakout.run_breakout(object(), D.date(), "run-1", "r1")

    assert out["signals"] == []
    assert out["eligibility"]["sightings"]["max_distinct_unflagged_creators"] == 2
    assert out["eligibility"]["empty_reason"] == "insufficient_unrelated_creators"


def test_append_writes_only_after_three_local_small_unflagged_creators(monkeypatch):
    posts = _creator_rows(0) + _creator_rows(1)
    state = {"posts": posts, "sightings": [_sighting("breakout-0"), _sighting("breakout-1")]}
    _install_reader(monkeypatch, state)
    writes = []
    monkeypatch.setattr(breakout.aggregate, "_run", lambda *args: writes.append(args))
    monkeypatch.setattr(breakout, "rows_param", lambda rows: rows)

    empty = breakout.append_signals(object(), D.date(), "run-1", "r1")
    assert empty["breakouts"] == 2
    assert empty["signals"] == 0
    assert empty["appended"] == 0
    assert empty["eligibility"]["empty_reason"] == "insufficient_unrelated_creators"
    assert writes == []

    state["posts"] += _creator_rows(2)
    state["sightings"].append(_sighting("breakout-2"))
    positive = breakout.append_signals(object(), D.date(), "run-2", "r1")

    assert positive["breakouts"] == 3
    assert positive["signals"] == 1
    assert positive["appended"] == 1
    assert positive["eligibility"]["empty_reason"] is None
    assert len(writes) == 1
