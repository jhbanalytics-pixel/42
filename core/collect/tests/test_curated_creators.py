import copy
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.collect import curated_creators, job, local_sources
from core.collect.socialcrawl_client import load_caps, quote_for


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class Cell:
    def __init__(self, value=None, data_type="s", hyperlink=None):
        self.value = value
        self.data_type = data_type
        self.hyperlink = hyperlink


class UnreadCell:
    @property
    def value(self):
        raise AssertionError("a non-manifest workbook field was read")


class Sheet:
    def __init__(self, title, rows):
        self.title = title
        self.rows = rows

    def iter_rows(self, values_only=False):
        return iter(self.rows)


class Workbook:
    def __init__(self, sheets):
        self.worksheets = sheets


def row(platform, link):
    return [UnreadCell(), UnreadCell(), Cell(platform), UnreadCell(), link, UnreadCell()]


def workbook():
    headers = [
        [Cell(), Cell(), Cell("PLATFORMS"), Cell(), Cell(), Cell()],
        [Cell(), Cell("INFLUENCER NAME"), Cell("Platform"), Cell("Following"), Cell("Links"), Cell("Comments")],
    ]
    return Workbook([
        Sheet("SA - Client Focused - UEFA", headers + [
            row("TikTok", Cell("profile", hyperlink=SimpleNamespace(target="https://www.tiktok.com/@one"))),
            row(None, Cell('=HYPERLINK("https://www.instagram.com/two?hl=en","open")', "f")),
            row("Instagram + FB", Cell("https://www.instagram.com/Three/reels?hl=en")),
            row("Tik Tok", Cell("https://www.tiktok.com/@four/video/123456789?_r=1&_t=2")),
            row("Instagram", Cell("https://www.instagram.com/p/postid/")),
            row("Instagram", Cell("https://www.tiktok.com/@mismatch")),
            row("Instagram", Cell("https://www.instagram.com/three/")),
            row(None, Cell("https://www.instagram.com/accounts/login/")),
            row("Snapchat", Cell("https://www.snapchat.com/add/five")),
            row("TikTok", Cell()),
        ]),
        Sheet("NIGERIA - Client Focused - UEFA", headers + [
            row("TikTok", Cell("https://www.tiktok.com/@one")),
        ]),
        Sheet("Kenya - Client Focused - UEFA ", headers + [
            row("TikTok", Cell("https://www.tiktok.com/@one")),
        ]),
    ])


def test_workbook_import_keeps_only_supported_profile_fields_and_logs_skips():
    records, skipped = curated_creators.parse_workbook(workbook())

    assert records == [
        {"platform": "instagram", "handle": "two", "url": "https://www.instagram.com/two/", "market": "ZA"},
        {"platform": "instagram", "handle": "Three", "url": "https://www.instagram.com/Three/", "market": "ZA"},
        {"platform": "tiktok", "handle": "four", "url": "https://www.tiktok.com/@four", "market": "ZA"},
    ]
    assert skipped == [
        {"market": "ZA", "row": 3, "reason": "duplicate_url"},
        {"market": "ZA", "row": 7, "reason": "invalid_profile_url"},
        {"market": "ZA", "row": 8, "reason": "platform_mismatch"},
        {"market": "ZA", "row": 9, "reason": "duplicate"},
        {"market": "ZA", "row": 10, "reason": "login_url"},
        {"market": "ZA", "row": 11, "reason": "unsupported_priced_route"},
        {"market": "ZA", "row": 12, "reason": "missing_url"},
        {"market": "NG", "row": 3, "reason": "duplicate_url"},
        {"market": "KE", "row": 3, "reason": "duplicate_url"},
    ]
    assert all(set(record) == {"platform", "handle", "url", "market"} for record in records)
    assert all(set(entry) == {"market", "row", "reason"} for entry in skipped)


def test_workbook_import_excludes_every_row_with_a_url_used_on_another_raw_row():
    headers = [
        [Cell(), Cell(), Cell("PLATFORMS"), Cell(), Cell(), Cell()],
        [Cell(), Cell(), Cell("Platform"), Cell(), Cell("Links"), Cell()],
    ]
    duplicate_url = "https://www.tiktok.com/@shared"
    source = Workbook([
        Sheet("SA - Client Focused - UEFA", headers + [
            row("TikTok", Cell(duplicate_url)),
            row("TikTok", Cell("https://www.tiktok.com/@unique")),
            row("Instagram", Cell()),
        ]),
        Sheet("Nigeria - Client Focused - UEFA", headers + [row("TikTok", Cell(duplicate_url))]),
    ])

    records, skipped = curated_creators.parse_workbook(source)

    assert records == [
        {"platform": "tiktok", "handle": "unique", "url": "https://www.tiktok.com/@unique", "market": "ZA"},
    ]
    assert skipped == [
        {"market": "ZA", "row": 3, "reason": "duplicate_url"},
        {"market": "ZA", "row": 5, "reason": "missing_url"},
        {"market": "NG", "row": 3, "reason": "duplicate_url"},
    ]


def test_manifest_keeps_every_seed_and_marks_exact_url_duplicate_groups_inactive():
    records = curated_creators.load_manifest()
    assert {market: sum(record["market"] == market for record in records) for market in job.MARKETS} == {
        "ZA": 167, "NG": 194, "KE": 162}
    platforms = ("facebook", "instagram", "tiktok", "twitter", "youtube")
    assert {market: {platform: sum(record["market"] == market and record["platform"] == platform
                                  for record in records)
                     for platform in platforms if any(record["market"] == market
                                                      and record["platform"] == platform for record in records)}
            for market in job.MARKETS} == {
                "ZA": {"instagram": 87, "tiktok": 38, "twitter": 40, "youtube": 2},
                "NG": {"instagram": 133, "tiktok": 23, "twitter": 37, "youtube": 1},
                "KE": {"facebook": 3, "instagram": 66, "tiktok": 60, "twitter": 32, "youtube": 1},
            }
    base_fields = {"platform", "handle", "url", "market", "source", "active"}
    assert all(set(record) == base_fields or (
        set(record) == base_fields | {"source_tags"} and record["source"] in record["source_tags"]
        and len(record["source_tags"]) == len(set(record["source_tags"]))
    ) for record in records)
    assert {record["source"] for record in records} == {
        "ogilvy_uefa_list", "research_confirmed", "ogilvy_audit_2026-09-30"}
    assert {source: sum(record["source"] == source for record in records)
            for source in ("ogilvy_uefa_list", "research_confirmed", "ogilvy_audit_2026-09-30")} == {
                "ogilvy_uefa_list": 232, "research_confirmed": 126, "ogilvy_audit_2026-09-30": 165}
    assert sum(record["source"] == "research_confirmed" and record["active"] for record in records) == 83
    assert sum("ogilvy_audit_2026-09-30" in record.get("source_tags", []) for record in records) == 193
    assert {market: sum(record["market"] == market and record["active"] for record in records)
            for market in job.MARKETS} == {"ZA": 124, "NG": 155, "KE": 129}
    assert {market: sum(record["market"] == market and not record["active"] for record in records)
            for market in job.MARKETS} == {"ZA": 43, "NG": 39, "KE": 33}

    uefa_holds = [record for record in records
                  if record["source"] == "ogilvy_uefa_list" and not record["active"]]
    assert {(record["market"], record["platform"], record["handle"]) for record in uefa_holds} == {
        ("ZA", "tiktok", "iamfez_"), ("NG", "instagram", "ordusonjenny")}
    research_unpriced_holds = [record for record in records
                               if record["source"] == "research_confirmed" and not record["active"]]
    assert len(research_unpriced_holds) == 43
    assert {record["platform"] for record in research_unpriced_holds} == {"twitter", "youtube"}

    identities = [(record["market"], record["platform"], record["handle"].casefold()) for record in records]
    assert len(identities) == len(set(identities))
    priced_records = [record for record in records
                      if record["platform"] in {"facebook", "instagram", "tiktok"}]
    assert all(record["platform"] in {"facebook", "instagram", "tiktok"}
               for record in records if record["active"])
    assert all(quote_for("prism/profiles", "POST", {
        "items": [{"platform": record["platform"], "handle": record["handle"]}], "include": "posts"
    }) == 2 for record in priced_records)


def test_manifest_accepts_approved_source_and_active_metadata(tmp_path):
    records = [
        {"platform": "instagram", "handle": "confirmed", "url": "https://www.instagram.com/confirmed/",
         "market": "ZA", "source": "research_confirmed", "active": True},
        {"platform": "tiktok", "handle": "inactive", "url": "https://www.tiktok.com/@inactive",
         "market": "NG", "source": "ogilvy_uefa_list", "active": False},
        {"platform": "instagram", "handle": "source-only", "url": "https://www.instagram.com/source-only/",
         "market": "KE", "source": "research_confirmed"},
    ]
    path = tmp_path / "seeds.yaml"
    path.write_text(yaml.safe_dump(records, sort_keys=False), encoding="utf-8")

    loaded = curated_creators.load_manifest(path)

    assert loaded[:2] == records[:2]
    assert loaded[2] == {**records[2], "active": True}
    assert all(set(record) <= {"platform", "handle", "url", "market", "source", "active"} for record in loaded)


def test_audited_source_uses_normal_rotation_and_residual_price_guard(tmp_path, monkeypatch):
    records = []
    for market in curated_creators.MARKETS:
        records.extend([
            {"platform": "instagram", "handle": f"audit_{market.lower()}",
             "url": f"https://www.instagram.com/audit_{market.lower()}/", "market": market,
             "source": "ogilvy_audit_2026-09-30", "active": True,
             "source_tags": ["ogilvy_audit_2026-09-30"]},
            {"platform": "twitter", "handle": f"audit_x_{market.lower()}",
             "url": f"https://x.com/audit_x_{market.lower()}", "market": market,
             "source": "ogilvy_audit_2026-09-30", "active": False,
             "source_tags": ["ogilvy_audit_2026-09-30"]},
        ])
    path = tmp_path / "audited-seeds.yaml"
    path.write_text(yaml.safe_dump(records, sort_keys=False), encoding="utf-8")
    quoted = []

    def quote(route, method, params):
        quoted.extend(item["platform"] for item in params["items"])
        return 1

    monkeypatch.setattr(curated_creators, "quote_for", quote)
    loaded = curated_creators.load_manifest(path)

    rotated = curated_creators.daily_rotation(loaded, "ZA", date(2026, 10, 1), limit=100)
    allowed = curated_creators.limit_from_residual(loaded, date(2026, 10, 1), residual=3, limit=13)

    assert [record["platform"] for record in rotated] == ["instagram"]
    assert curated_creators.DAILY_LIMIT == 160
    assert allowed == 1
    assert quoted and set(quoted) == {"instagram"}


def test_unpriced_profile_canonicalization_is_opt_in_and_never_quotes(monkeypatch):
    cases = [
        ("https://x.com/root_handle30", "X", {
            "platform": "twitter", "handle": "root_handle30", "url": "https://x.com/root_handle30"}),
        ("https://x.com/eNCA", "X", {
            "platform": "twitter", "handle": "eNCA", "url": "https://x.com/eNCA"}),
        ("https://x.com/IOL", "X", {
            "platform": "twitter", "handle": "IOL", "url": "https://x.com/IOL"}),
        ("https://x.com/x", "X", {
            "platform": "twitter", "handle": "x", "url": "https://x.com/x"}),
        ("https://x.com/a12345678901234", "X", {
            "platform": "twitter", "handle": "a12345678901234", "url": "https://x.com/a12345678901234"}),
        ("https://www.youtube.com/@creator.handle", "YouTube", {
            "platform": "youtube", "handle": "creator.handle",
            "url": "https://www.youtube.com/@creator.handle"}),
    ]

    def no_quote(*_args, **_kwargs):
        pytest.fail("unpriced profile canonicalization called the pricing helper")

    monkeypatch.setattr(curated_creators, "quote_for", no_quote)
    for url, declared, expected in cases:
        assert curated_creators._profile(url, declared) == (None, "unsupported_priced_route")
        try:
            profile, reason = curated_creators._profile(url, declared, allow_unpriced=True)
        except TypeError:
            pytest.fail("_profile must expose the explicit allow_unpriced option")
        assert (profile, reason) == (expected, None)


@pytest.mark.parametrize(("url", "declared", "reason"), [
    ("https://x.com/root_handle30/status/123", "X", "invalid_profile_url"),
    ("https://x.com/i/flow/login", "Twitter", "login_url"),
    ("https://x.com/root_handle30?s=20", "Twitter", "invalid_profile_url"),
    ("https://x.com/home", "Twitter", "invalid_profile_url"),
    ("https://x.com/1234567890123456", "Twitter", "invalid_profile_url"),
    ("https://x.com/a.b", "Twitter", "invalid_profile_url"),
    ("https://x.com/é", "Twitter", "invalid_profile_url"),
    ("ftp://x.com/IOL", "Twitter", "invalid_url"),
    ("https://www.youtube.com/@creator.handle/videos", "YouTube", "invalid_profile_url"),
    ("https://www.youtube.com/watch?v=video123", "YouTube", "invalid_profile_url"),
    ("https://www.youtube.com/signin", "YouTube", "login_url"),
    ("https://www.youtube.com/@ab", "YouTube", "invalid_profile_url"),
    ("https://www.youtube.com/@.creator", "YouTube", "invalid_profile_url"),
    ("https://www.youtube.com/@creator.handle#videos", "YouTube", "invalid_url"),
])
def test_unpriced_profile_canonicalization_rejects_nonprofile_urls(url, declared, reason):
    try:
        profile, actual_reason = curated_creators._profile(url, declared, allow_unpriced=True)
    except TypeError:
        pytest.fail("_profile must expose the explicit allow_unpriced option")
    assert profile is None
    assert actual_reason == reason


@pytest.mark.parametrize(("platform", "handle", "url"), [
    ("twitter", "root_handle30", "https://x.com/root_handle30"),
    ("twitter", "eNCA", "https://x.com/eNCA"),
    ("twitter", "IOL", "https://x.com/IOL"),
    ("youtube", "creator.handle", "https://www.youtube.com/@creator.handle"),
])
def test_manifest_stores_only_inactive_unpriced_profiles(tmp_path, monkeypatch, platform, handle, url):
    def no_quote(*_args, **_kwargs):
        pytest.fail("inactive unpriced profile storage called the pricing helper")

    monkeypatch.setattr(curated_creators, "quote_for", no_quote)
    record = {"platform": platform, "handle": handle, "url": url, "market": "ZA",
              "source": "research_confirmed", "active": False}
    path = tmp_path / "seeds.yaml"
    path.write_text(yaml.safe_dump([record], sort_keys=False), encoding="utf-8")

    assert curated_creators.load_manifest(path) == [record]

    path.write_text(yaml.safe_dump([{**record, "active": True}], sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError):
        curated_creators.load_manifest(path)


def test_unpriced_platforms_are_filtered_before_rotation_and_residual_quotes(monkeypatch):
    records = []
    for market in curated_creators.MARKETS:
        records.extend([
            {"platform": "twitter", "handle": f"x_{market.lower()}30", "url": f"https://x.com/x_{market.lower()}30",
             "market": market, "source": "research_confirmed", "active": True},
            {"platform": "youtube", "handle": f"tube.{market.lower()}",
             "url": f"https://www.youtube.com/@tube.{market.lower()}", "market": market,
             "source": "research_confirmed", "active": True},
            {"platform": "instagram", "handle": f"paid-{market}",
             "url": f"https://www.instagram.com/paid-{market}/", "market": market,
             "source": "research_confirmed", "active": True},
        ])
    quoted = []

    def quote(route, method, params):
        quoted.extend(item["platform"] for item in params["items"])
        return 2

    monkeypatch.setattr(curated_creators, "quote_for", quote)

    rotated = curated_creators.daily_rotation(records, "ZA", date(2026, 9, 30), limit=100)
    allowed = curated_creators.limit_from_residual(records, date(2026, 9, 30), residual=6, limit=100)

    assert [record["platform"] for record in rotated] == ["instagram"]
    assert allowed == 1
    assert quoted == ["instagram", "instagram", "instagram"]


def test_manifest_rejects_untagged_external_records_and_invalid_source_or_active(tmp_path):
    base = {"platform": "instagram", "handle": "one", "url": "https://www.instagram.com/one/", "market": "ZA"}
    invalid_records = [
        base,
        {**base, "active": True},
        {**base, "source": "unverified", "active": True},
        {**base, "source": "research_confirmed", "active": 1},
    ]

    for index, record in enumerate(invalid_records):
        path = tmp_path / f"invalid-{index}.yaml"
        path.write_text(yaml.safe_dump([record], sort_keys=False), encoding="utf-8")
        with pytest.raises(ValueError):
            curated_creators.load_manifest(path)


def test_manifest_rejects_explicit_null_source_on_known_manifest(tmp_path, monkeypatch):
    record = {
        "platform": "instagram", "handle": "one", "url": "https://www.instagram.com/one/",
        "market": "ZA", "source": None,
    }
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump([record], sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(curated_creators, "MANIFEST", path)

    with pytest.raises(ValueError):
        curated_creators.load_manifest(path)


@pytest.mark.parametrize("field", ["name", "gender", "age", "followers", "phase"])
def test_manifest_rejects_unapproved_metadata_fields(tmp_path, field):
    record = {
        "platform": "instagram", "handle": "one", "url": "https://www.instagram.com/one/",
        "market": "ZA", "source": "research_confirmed", "active": True, field: "not allowed",
    }
    path = tmp_path / "seeds.yaml"
    path.write_text(yaml.safe_dump([record], sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError):
        curated_creators.load_manifest(path)


def test_rotation_is_market_scoped_repeatable_and_limited():
    records = [{"platform": "instagram", "handle": f"creator-{i}", "url": f"https://www.instagram.com/creator-{i}/",
                "market": "ZA" if i < 40 else "NG", "source": "research_confirmed", "active": True}
               for i in range(80)]
    day = date(2026, 9, 30)
    first = curated_creators.daily_rotation(records, "ZA", day, limit=30)

    assert len(first) == 30
    assert first == curated_creators.daily_rotation(records, "ZA", day, limit=30)
    assert curated_creators.daily_rotation(records, "NG", day)
    assert all(record["market"] == "ZA" for record in first)
    assert curated_creators.daily_rotation(records, "ZA", day + (date(2026, 10, 1) - day), limit=30) != first
    assert len(curated_creators.daily_rotation(records, "ZA", day, limit=100)) == 40


def test_inactive_seeds_are_removed_before_rotation_and_quoting(monkeypatch):
    records = []
    for market in curated_creators.MARKETS:
        records.extend([
            {"platform": "instagram", "handle": f"inactive-{market}",
             "url": f"https://www.instagram.com/inactive-{market}/", "market": market,
             "source": "ogilvy_uefa_list", "active": False},
            {"platform": "instagram", "handle": f"active-{market}",
             "url": f"https://www.instagram.com/active-{market}/", "market": market,
             "source": "research_confirmed", "active": True},
        ])
    quoted = []

    def quote(route, method, params):
        quoted.extend(item["handle"] for item in params["items"])
        return 2

    monkeypatch.setattr(curated_creators, "quote_for", quote)

    rotated = curated_creators.daily_rotation(records, "ZA", date(2026, 9, 30), limit=100)
    allowed = curated_creators.limit_from_residual(records, date(2026, 9, 30), residual=6, limit=100)

    assert [record["handle"] for record in rotated] == ["active-ZA"]
    assert allowed == 1
    assert quoted == ["active-ZA", "active-NG", "active-KE"]


def _curated(planned, market):
    # The culture desk read is an own feed too (D2, 4 Oct 2026); it is the call carrying the hubs.yaml list.
    desk = [{"platform": e["platform"], "handle": e["handle"]}
            for e in job.load_config()["hubs"]["markets"][market.lower()].get("culture_desk") or []]
    return [call for call in planned["calls"][market] if call.route == "prism/profiles"
            and call.source_market == market and call.params["items"] != desk]


def test_job_plan_reads_the_rotation_in_batches_of_25_on_one_protocol_inside_the_cap():
    day = date(2026, 9, 30)
    planned = job.plan(day)
    local_hold = local_sources.total_hold(local_sources.plan(day))
    records = curated_creators.load_manifest()
    for market in job.MARKETS:
        calls = _curated(planned, market)
        sizes = [len(call.params["items"]) for call in calls]
        assert sizes and all(size <= job.PROFILES_PER_CALL for size in sizes)
        assert len({call.protocol for call in calls}) == 1
        assert all(call.row == "23" and call.source_market == call.market == market for call in calls)
        assert sum(call.hold() for call in calls) == 2 * sum(sizes)
        # The collect share in caps.yaml leaves room for each market's whole list.
        assert sum(sizes) == len(curated_creators._readable(records, market))
    assert planned["total"] + local_hold <= load_caps()["ENGINE_DAILY"]["collect"]


def test_daily_profile_batches_fit_residual_planned_holds(monkeypatch):
    # The rotation takes what the collect share leaves after the rest of the plan and the local sources, at
    # 2 credits a profile in each market, so the plan always ends at or under the share.
    real = load_caps()
    for share in (1000, real["ENGINE_DAILY"]["collect"], 2000):
        caps = copy.deepcopy(real)
        caps["ENGINE_DAILY"]["collect"] = share
        monkeypatch.setattr(job, "load_caps", lambda caps=caps: copy.deepcopy(caps))
        for day in (date(2026, 9, 30), date(2026, 10, 5)):
            # Row 14i takes only the room left after the rotation, so the rotation is read without it.
            baseline = job.plan(day, include_curated=False, reels={})
            planned = job.plan(day, reels={})
            assert job.plan(day)["total"] + local_sources.total_hold(local_sources.plan(day)) <= share
            local_hold = local_sources.total_hold(local_sources.plan(day))
            residual = share - baseline["total"] - local_hold
            read = {market: sum(len(c.params["items"]) for c in _curated(planned, market)) for market in job.MARKETS}
            assert planned["total"] == baseline["total"] + 2 * sum(read.values())
            assert planned["total"] + local_hold <= share
            assert residual - 2 * sum(read.values()) < 6 or read == {"ZA": 124, "NG": 155, "KE": 129}


def test_under_a_2000_share_every_active_creator_is_read_each_day_as_one_stable_panel(monkeypatch):
    # A trend reaches Today's candidates from panel posts only when 3 creators carry it within 3 days, and
    # country searches never count towards that floor. Reading each market's whole list every day keeps one
    # membership, so the panel's protocol, and with it its baseline, carries over from day to day.
    caps = copy.deepcopy(load_caps())
    caps["ENGINE_DAILY"]["collect"] = 2000
    monkeypatch.setattr(job, "load_caps", lambda: copy.deepcopy(caps))
    records = curated_creators.load_manifest()
    protocols = {}
    for day in (date(2026, 10, 3) + timedelta(days=n) for n in range(7)):
        planned = job.plan(day)
        local_hold = local_sources.total_hold(local_sources.plan(day))
        assert planned["total"] + local_hold <= 2000
        for market in job.MARKETS:
            calls = _curated(planned, market)
            handles = {(i["platform"], i["handle"]) for c in calls for i in c.params["items"]}
            assert handles == {(r["platform"], r["handle"]) for r in curated_creators._readable(records, market)}
            protocols.setdefault(market, set()).update(c.protocol for c in calls)
    assert all(len(found) == 1 for found in protocols.values())


def test_curated_profile_observations_use_the_tab_market_for_source_market():
    from core.collect.tests.test_job import FakeGeo, fake_item_id

    panels = json.loads((FIXTURES / "parse_panels.json").read_text(encoding="utf-8"))
    body = copy.deepcopy(panels["prism_profiles"])
    body["data"]["results"][0]["posts"]["items"][0]["post"]["author"] = {"username": "culture.desk.za"}
    body["data"]["results"][0]["posts"]["items"][0]["post"]["content"]["text"] = "Fit check"
    runner = job._Runner(
        SimpleNamespace(), job.Collected("run1"), job._CountedIds(fake_item_id), job.safe_geo(FakeGeo()),
        lambda: datetime(2026, 9, 30, 0, 30, tzinfo=timezone.utc), job.Budget(), {}, date(2026, 9, 30))
    call = job.Call("23", "prism/profiles", {"items": [{"platform": "instagram", "handle": "culture.desk.za"}],
                                                "include": "posts"}, "NG", "panel",
                    source_market="NG")

    parsed = runner._parse(call, SimpleNamespace(status="ok", body=body),
                           "2026-09-30T00:30:00Z")

    assert parsed["observations"]
    assert all(observation["market"] == "NG" for observation in parsed["observations"])
    assert all(observation["source_market"] == "NG" for observation in parsed["observations"])
