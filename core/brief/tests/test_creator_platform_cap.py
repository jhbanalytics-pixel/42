from datetime import date, datetime, time, timedelta, timezone

from core.brief import evidence, pack_order
from core.brief import job
from core.detect.tests import duck


def test_pack_creator_cap_is_scoped_to_platform_identity():
    con = duck.connect(views=False)
    d = date(2026, 10, 13)
    published_at = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
    for platform in ("tiktok", "instagram"):
        for n in range(2):
            post_id = f"{platform}-{n}"
            duck.load(con, "core.posts", [{
                "post_id": post_id, "platform": platform, "creator_id": "shared-raw-id",
                "published_at": published_at, "post_date": published_at.date(), "engagement": 100 - n,
                "geo_market": "ZA", "geo_confidence": 0.9, "geo_source": "ext_region",
            }])
            duck.load(con, "core.post_items", [{"post_id": post_id, "item_id": "i1"}])
            duck.load(con, "core.post_observations", [{
                "post_id": post_id, "observed_at": published_at, "observed_date": published_at.date(),
                "market": "ZA", "lane": "sweep", "lane_class": "unbiased_rank",
            }])

    tz = timezone(timedelta(hours=2))
    start = datetime.combine(d - timedelta(days=6), time.min, tz)
    rows = evidence._pack_rows(duck.Client(con), {
        "item_id": "i1", "market": "ZA", "d": d, "start": start,
        "end": start + timedelta(days=7), "outlet_cap": pack_order.OUTLET_CAP,
    }, "core", "agent")

    assert {row["post_id"] for row in rows if row["post_id"] is not None} == {
        "tiktok-0", "tiktok-1", "instagram-0", "instagram-1",
    }


def test_candidate_local_breadth_counts_platform_scoped_creator_identity():
    con = duck.connect(views=False)
    d = date(2026, 10, 13)
    published_at = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
    for platform in ("tiktok", "instagram"):
        post_id = f"{platform}-0"
        duck.load(con, "core.posts", [{
            "post_id": post_id, "platform": platform, "creator_id": "shared-raw-id",
            "published_at": published_at, "post_date": published_at.date(), "engagement": 100,
            "geo_market": "ZA", "geo_confidence": 0.9, "geo_source": "ext_region",
        }])
        duck.load(con, "core.post_items", [{"post_id": post_id, "item_id": "i1"}])
        duck.load(con, "core.post_observations", [{
            "post_id": post_id, "observed_at": published_at, "observed_date": published_at.date(),
            "market": "ZA", "lane": "sweep", "lane_class": "unbiased_rank",
        }])

    statement = job.QUERIES["candidates"]
    local_first = statement[:statement.index("ordered AS (")].rstrip().removesuffix(",")
    rows = duck.query(con, local_first + " SELECT item_id, creators FROM local_first", {"d": d, "market": "ZA"})

    assert rows == [{"item_id": "i1", "creators": 2}]
