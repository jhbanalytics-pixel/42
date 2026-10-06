"""The Telegram public-channel phase: off by default and then invisible; on, free, polite and capped, and its posts
count as local one step lower through the existing own-feed field (source_market), nothing new."""

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.brief import explain
from core.collect import chain, job, public_feed_collect, telegram_collect
from core.collect.tests.test_job import (BothClient, FakeBQ, FakeGeo, FakeGet, FakeJobs, NOW, TUESDAY,
                                         fake_item_id)
from core.collect.tests.test_public_feed_job import _transport as public_transport
from core.public_feeds import telegram
from core.trust import claims

SAMPLES = Path(__file__).resolve().parents[2] / "public_feeds" / "tests" / "samples"
BRIEFLY_PAGE = (SAMPLES / "za_telegram_brieflycoza_20261004.html").read_bytes()
NAIROBBY_PAGE = (SAMPLES / "ke_telegram_nairobby_handwritten.html").read_bytes()
HTML = {"content-type": "text/html; charset=utf-8"}
DAY = date(2026, 10, 4)
AT = datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc)   # 19:00 SAST, 20:00 EAT on 4 Oct


class Transport:
    def __init__(self, pages=None, robots=(404, {"content-type": "text/html"}, b""), default=(302, {}, b"")):
        self.pages, self.robots, self.default, self.calls = pages or {}, robots, default, []

    def __call__(self, url, *, timeout, max_bytes):
        self.calls.append(url)
        assert timeout == 20 and max_bytes == 2 * 1024 * 1024
        if url == "https://t.me/robots.txt":
            return self.robots
        return self.pages.get(url, self.default)


def _run(transport, channels, **kwargs):
    sleeps = []
    out = telegram_collect.run(DAY, "collect-telegram-test", transport=transport, clock=kwargs.pop("clock", lambda: AT),
                               sleep=sleeps.append, channels=channels, **kwargs)
    return out, sleeps


def _channels(*handles):
    return tuple(telegram.channel_for(h) for h in handles)


def test_a_saved_page_becomes_local_own_feed_posts_of_the_channels_market():
    transport = Transport({"https://t.me/s/brieflycoza": (200, HTML, BRIEFLY_PAGE)})
    out, sleeps = _run(transport, _channels("brieflycoza"))

    assert transport.calls == ["https://t.me/robots.txt", "https://t.me/s/brieflycoza"]
    assert sleeps == []
    assert len(out["posts"]) == len(out["observations"]) == 20
    post = next(p for p in out["posts"] if p["native_id"] == "brieflycoza/51370")
    assert post["platform"] == "telegram" and post["url"] == "https://t.me/brieflycoza/51370"
    assert post["creator_id"] == "brieflycoza"
    assert post["published_at"] == "2026-10-04T16:23:11+00:00" and post["post_date"] == "2026-10-04"
    assert post["views"] == 1 and post["text"].startswith("Leo Bozell accuses")
    assert post["source_regime"] == "telegram_preview" and post["vendor"] == "telegram"
    # No location is inferred from the channel: the market comes only from the sighting.
    assert all(p["geo_market"] is None and p["geo_confidence"] is None and p["geo_source"] is None
               for p in out["posts"])
    assert all("[url omitted]" in p["text"] or "http" not in p["text"] for p in out["posts"])
    obs = out["observations"][0]
    assert (obs["market"], obs["source_market"], obs["source_region"]) == ("ZA", "ZA", None)
    assert (obs["route"], obs["series"], obs["lane"], obs["lane_class"]) == (
        "telegram_preview", "panel_telegram", "panel", "panel")
    assert obs["protocol"] == "telegram_preview?channel=brieflycoza" and obs["seed_key"] == "brieflycoza"
    assert out["creators"] == [{"creator_id": "brieflycoza", "platform": "telegram", "handle": "brieflycoza",
                                "display_name": "Briefly News", "followers": None, "verified": None,
                                "profile_location": None, "first_seen": AT.isoformat(),
                                "last_seen": AT.isoformat()}]
    record = out["records"][0]
    assert record["status"] == "ok" and record["calls"] == 1 and record["items"] == 20
    assert record["series"] == "panel_telegram" and record["lane_class"] == "panel"
    raw = out["raw_rows"][0]
    assert raw["credits_quoted"] == raw["credits_charged"] == 0 and raw["route"] == "telegram_preview"
    assert raw["body"]["accepted_count"] == 20
    assert "tgme_widget_message" not in str(raw["body"])
    assert out["summary"] == {"channels_planned": 1, "channels_attempted": 1, "channels_ok": 1, "posts": 20,
                              "posts_by_market": {"ZA": 20}, "statuses": {"ok": 1}, "credits": 0}


def test_the_posts_take_the_existing_own_feed_rule_one_step_lower():
    """The brief's own-feed mark and K5's source-only step read source_market, as for the gossip panel's pages."""
    transport = Transport({"https://t.me/s/Nairobby": (200, HTML, NAIROBBY_PAGE)})
    out, _ = _run(transport, _channels("Nairobby"))
    obs = {o["post_id"]: o for o in out["observations"]}
    records = [{**p, "id": p["post_id"], "source_market": obs[p["post_id"]]["source_market"], "market": None}
               for p in out["posts"]]

    assert records and all(obs[r["id"]]["source_market"] == "KE" for r in records)
    for record in records:
        assert claims.located_market(record) is None
        assert claims.source_market(record) == "KE"
        assert explain._own_feed_local(record, "KE") is True
        assert explain._own_feed_local(record, "NG") is False


def test_only_the_run_day_and_the_day_before_are_kept():
    transport = Transport({"https://t.me/s/Nairobby": (200, HTML, NAIROBBY_PAGE)})
    out, _ = _run(transport, _channels("Nairobby"))

    assert sorted(p["native_id"] for p in out["posts"]) == ["nairobby/7001", "nairobby/7002", "nairobby/7003"]
    assert out["raw_rows"][0]["body"]["dropped"] == {"service_message": 1, "no_text": 1, "other_channel": 1,
                                                     "missing_date": 1, "duplicate": 1, "before_window": 1}
    forwarded = next(p for p in out["posts"] if p["native_id"] == "nairobby/7003")
    assert forwarded["vendor_labels"] == {"forwarded": True}
    tagged = next(p for p in out["posts"] if p["native_id"] == "nairobby/7001")
    assert tagged["hashtags"] == ["NairobiVibes", "Matwana"] and tagged["views"] == 1200


def test_reads_are_polite_and_robots_is_read_once():
    pages = {"https://t.me/s/brieflycoza": (200, HTML, BRIEFLY_PAGE),
             "https://t.me/s/Nairobby": (200, HTML, NAIROBBY_PAGE)}
    transport = Transport(pages)
    out, sleeps = _run(transport, _channels("brieflycoza", "Nairobby", "dearsafrica"))

    assert transport.calls == ["https://t.me/robots.txt", "https://t.me/s/brieflycoza", "https://t.me/s/Nairobby",
                               "https://t.me/s/dearsafrica"]
    assert sleeps == [telegram.POLITE_DELAY_SECONDS] * 2 and telegram.POLITE_DELAY_SECONDS >= 4
    # dearsafrica answers a redirect (the preview turned off): recorded, nothing kept.
    assert [r["status"] for r in out["records"]] == ["ok", "ok", "no_preview"]
    assert out["records"][2]["calls"] == 1 and out["records"][2]["post_ids"] == []


@pytest.mark.parametrize("robots, reason", [
    ((403, {}, b""), "robots.txt answered HTTP 403"),
    ((200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /s/\n"), "robots.txt did not allow"),
])
def test_robots_refusal_fetches_no_page(robots, reason):
    transport = Transport({"https://t.me/s/brieflycoza": (200, HTML, BRIEFLY_PAGE)}, robots=robots)
    out, sleeps = _run(transport, _channels("brieflycoza", "Nairobby"))

    assert transport.calls == ["https://t.me/robots.txt"]
    assert {r["status"] for r in out["records"]} == {"robots_disallowed"}
    assert all(r["reason"].startswith(reason) and r["calls"] == 0 for r in out["records"])
    assert out["posts"] == [] and sleeps == []


def test_failures_are_recorded_per_channel_and_never_raise():
    def boom(url, *, timeout, max_bytes):
        if url.endswith("robots.txt"):
            return 404, {}, b""
        if url.endswith("brieflycoza"):
            raise TimeoutError("request exceeded its total deadline")
        if url.endswith("Nairobby"):
            return 500, {}, b""
        return 200, {"content-type": "application/json"}, b"{}"

    out, _ = _run(boom, _channels("brieflycoza", "Nairobby", "dearsafrica"))

    assert [(r["status"], r["ok"]) for r in out["records"]] == [
        ("error", False), ("http_500", False), ("error", False)]
    assert out["records"][0]["reason"] == "TimeoutError"
    assert out["posts"] == []


def test_the_per_run_caps_hold(monkeypatch):
    monkeypatch.setattr(telegram, "MAX_CHANNELS_PER_RUN", 2)
    transport = Transport({"https://t.me/s/brieflycoza": (200, HTML, BRIEFLY_PAGE),
                           "https://t.me/s/Nairobby": (200, HTML, NAIROBBY_PAGE)})
    out, _ = _run(transport, _channels("brieflycoza", "Nairobby", "dearsafrica"))
    assert len(out["records"]) == 2 and "https://t.me/s/dearsafrica" not in transport.calls

    monkeypatch.setattr(telegram, "MAX_CHANNELS_PER_RUN", 45)
    monkeypatch.setattr(telegram, "MAX_POSTS_PER_RUN", 12)
    transport = Transport({"https://t.me/s/brieflycoza": (200, HTML, BRIEFLY_PAGE),
                           "https://t.me/s/Nairobby": (200, HTML, NAIROBBY_PAGE)})
    out, _ = _run(transport, _channels("brieflycoza", "Nairobby"))
    assert len(out["posts"]) == len(out["observations"]) == 12
    assert out["raw_rows"][0]["body"]["dropped"] == {"over_run_cap": 8}
    assert out["records"][1]["status"] == "not_made" and out["records"][1]["calls"] == 0
    assert "https://t.me/s/Nairobby" not in transport.calls


def test_the_phase_time_budget_stops_further_reads():
    ticks = iter([AT] * 6 + [AT + timedelta(seconds=telegram.PHASE_BUDGET_SECONDS + 1)] * 20)
    transport = Transport({"https://t.me/s/brieflycoza": (200, HTML, BRIEFLY_PAGE)})
    out, _ = _run(transport, _channels("brieflycoza", "Nairobby", "dearsafrica"), clock=lambda: next(ticks))

    assert out["records"][0]["status"] == "ok"
    assert [r["status"] for r in out["records"][1:]] == ["not_made", "not_made"]
    assert all("budget" in r["reason"] for r in out["records"][1:])
    assert transport.calls == ["https://t.me/robots.txt", "https://t.me/s/brieflycoza"]


def test_a_stopped_collect_reads_nothing():
    transport = Transport()
    out, _ = _run(transport, _channels("brieflycoza"), stopped=lambda: "insufficient_credits")

    assert transport.calls == [] and out["records"][0]["status"] == "stopped"


def test_the_job_and_the_phase_agree_on_the_route():
    assert job.TELEGRAM_ROUTE == telegram_collect.ROUTE


# The collect job with the switch off (as shipped) and on.

def _main(transport, bq=None, runs=None):
    bq, runs = bq or FakeBQ(), runs or chain.MemoryRunsStore()
    code = job.main(["--run-date", TUESDAY.isoformat()], env={}, runs=runs, jobs=FakeJobs(), bq=bq,
                    make_client=lambda run_id, caps: BothClient(), fns=(fake_item_id, FakeGeo()), clock=lambda: NOW,
                    get=FakeGet(), public_feed_transport=transport)
    return code, bq, runs.rows[-1]["counts"]


TUESDAY_PAGE = (
    '<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message js-widget_message" '
    'data-post="brieflycoza/1"><div class="tgme_widget_message_text js-message_text">Load-shedding is back '
    '#Eskom</div><span class="tgme_widget_message_views">2.5K</span><a class="tgme_widget_message_date" '
    'href="https://t.me/brieflycoza/1"><time datetime="2026-09-28T20:00:00+00:00"></time></a></div></div>'
).encode()


def _job_transport():
    calls, public = public_transport()

    def transport(url, *, timeout, max_bytes):
        if url.startswith("https://t.me/"):
            calls.append(url)
            if url == "https://t.me/robots.txt":
                return 404, {}, b""
            if url == "https://t.me/s/brieflycoza":
                return 200, HTML, TUESDAY_PAGE
            return 302, {"location": "https://t.me/x"}, b""
        return public(url, timeout=timeout, max_bytes=max_bytes)

    return calls, transport


def _snapshot(bq, counts):
    tables = {name: bq.loaded(name) for name in ("post_observations", "collection_health", "raw_responses",
                                                  "item_counter_daily")}
    strip = lambda rows: sorted(repr(sorted((k, v) for k, v in r.items() if k != "run_id")) for r in rows)
    return ({k: strip(v) for k, v in tables.items()},
            sorted(repr(sorted((k, v) for k, v in p.items() if k != "run_id")) for p in bq.posts.values()),
            {k: v for k, v in counts.items()})


def test_switched_off_the_job_reads_writes_and_counts_nothing_from_telegram(monkeypatch, capsys):
    assert telegram.TELEGRAM_READY is False
    calls, transport = _job_transport()
    monkeypatch.setattr(job, "telegram_phase", lambda *a, **k: pytest.fail("the Telegram phase ran while off"))

    code, bq, counts = _main(transport)

    assert code == 0
    assert not any(url.startswith("https://t.me/") for url in calls)
    assert not any(k.startswith("telegram") for k in counts)
    assert not [p for p in bq.posts.values() if p["platform"] == "telegram"]
    for name in ("post_observations", "collection_health", "raw_responses"):
        assert not [r for r in bq.loaded(name) if r.get("route") == "telegram_preview"]
    assert not [c for c in bq.creators.values() if c["platform"] == "telegram"]

    assert job.main(["--plan", "--run-date", "2026-09-30"], env={}, bq=object(), runs=object(), jobs=object()) == 0
    assert "t.me" not in capsys.readouterr().out.lower()


def test_switched_off_the_run_is_the_same_as_without_the_phase(monkeypatch):
    """Off, the whole run (rows written and run counts) equals a run whose collect never reaches the Telegram code."""
    _, off_transport = _job_transport()
    _, bq_off, counts_off = _main(off_transport)

    monkeypatch.setattr(telegram, "TELEGRAM_READY", False)
    monkeypatch.setattr(job, "telegram_phase", lambda *a, **k: None)
    _, base_transport = _job_transport()
    _, bq_base, counts_base = _main(base_transport)

    assert _snapshot(bq_off, counts_off)[:2] == _snapshot(bq_base, counts_base)[:2]
    assert counts_off.keys() == counts_base.keys()
    assert {k: v for k, v in counts_off.items() if k != "run_id"} == \
        {k: v for k, v in counts_base.items() if k != "run_id"}


def test_switched_on_the_job_adds_free_telegram_rows_and_leaves_the_rest(monkeypatch):
    _, off_transport = _job_transport()
    _, bq_off, counts_off = _main(off_transport)

    monkeypatch.setattr(telegram, "TELEGRAM_READY", True)
    monkeypatch.setattr(telegram, "POLITE_DELAY_SECONDS", 0)
    calls, on_transport = _job_transport()
    code, bq_on, counts_on = _main(on_transport)

    assert code == 0
    assert calls.count("https://t.me/robots.txt") == 1
    assert sum(url.startswith("https://t.me/s/") for url in calls) == 45
    assert counts_on["telegram_summary"]["channels_planned"] == 45
    assert counts_on["telegram_summary"]["statuses"] == {"ok": 1, "no_preview": 44}
    assert counts_on["telegram_summary"]["posts_by_market"] == {"ZA": 1}
    assert counts_on["telegram_records"] == counts_on["telegram_raw_rows"] == 45
    assert counts_on["telegram_error"] is None
    assert counts_on["telegram_raw_rows_written"] == 45 and counts_on["telegram_raw_error"] is None
    # Nothing else moves: credits, SocialCrawl calls, public feeds.
    for key in ("credits_charged", "credits_by_share", "calls", "calls_ok", "not_made", "day_changed",
                "public_feed_records", "public_feed_records_by_market", "public_feed_summary", "public_feed_raw_rows",
                "local_records"):
        assert counts_on[key] == counts_off[key], key
    assert counts_on["posts"] == counts_off["posts"] + 1
    assert counts_on["observations"] == counts_off["observations"] + 1

    post = next(p for p in bq_on.posts.values() if p["platform"] == "telegram")
    assert post["native_id"] == "brieflycoza/1" and post["views"] == 2500
    assert getattr(post["hashtags"], "values", post["hashtags"]) == ["Eskom"]
    assert post["source_regime"] == "telegram_preview"   # the MERGE that refreshes views on a later read
    assert post["geo_market"] is None
    obs = [r for r in bq_on.loaded("post_observations") if r["route"] == "telegram_preview"]
    assert [(o["market"], o["source_market"], o["lane_class"]) for o in obs] == [("ZA", "ZA", "panel")]
    health = [r for r in bq_on.loaded("collection_health") if r["route"] == "telegram_preview"]
    assert len(health) == 45 and Counter(h["market"] for h in health) == {"NG": 20, "KE": 19, "ZA": 6}
    raw = [r for r in bq_on.loaded("raw_responses") if r["route"] == "telegram_preview"]
    assert len(raw) == 45 and all(r["credits_charged"] == 0 for r in raw)
    assert ("telegram", "brieflycoza") in {(c["platform"], c["creator_id"]) for c in bq_on.creators.values()}
    # The public feed rows are exactly the switched-off run's.
    for name in ("raw_responses", "collection_health", "post_observations"):
        strip = lambda rows: sorted(repr(sorted((k, v) for k, v in r.items() if k != "run_id")) for r in rows
                                    if r.get("route") != "telegram_preview")
        assert strip(bq_on.loaded(name)) == strip(bq_off.loaded(name)), name


def test_switched_on_a_phase_exception_never_fails_the_collect(monkeypatch):
    monkeypatch.setattr(telegram, "TELEGRAM_READY", True)
    monkeypatch.setattr(telegram_collect, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    _, transport = _job_transport()

    code, _, counts = _main(transport)

    assert code == 0 and counts["telegram_error"] == "RuntimeError" and counts["telegram_summary"] is None


def test_switched_on_the_plan_lists_every_channel(monkeypatch, capsys):
    monkeypatch.setattr(telegram, "TELEGRAM_READY", True)
    assert job.main(["--plan", "--run-date", "2026-09-30"], env={}, bq=object(), runs=object(), jobs=object()) == 0
    out = capsys.readouterr().out
    assert "Telegram public channels: 45 web preview reads, 0 SocialCrawl credits" in out
    assert "  ZA Briefly News https://t.me/s/brieflycoza" in out


def test_a_repair_run_never_reads_telegram(monkeypatch):
    monkeypatch.setattr(telegram, "TELEGRAM_READY", True)
    monkeypatch.setattr(job, "telegram_phase", lambda *a, **k: pytest.fail("the Telegram phase ran in a repair"))
    run = job.collect(BothClient(), TUESDAY, "repair-test", item_id_fn=fake_item_id, geo_fn=FakeGeo(),
                      clock=lambda: NOW, public_feed_transport=lambda *a, **k: (404, {}, b""),
                      only_routes=("tiktok/trending",))
    assert run.telegram_summary is None
