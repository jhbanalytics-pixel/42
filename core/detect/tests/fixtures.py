"""Fixture rows for the detection view tests. Every builder returns plain dicts keyed by column name."""

from datetime import date, datetime, time, timedelta, timezone

D = date(2026, 9, 20)


def day(i):
    """The date i days before D."""
    return D - timedelta(days=i)


def at(d, hour=12):
    return datetime.combine(d, time(hour), tzinfo=timezone.utc)


def rid(stage, d):
    return f"{stage}-{d:%Y%m%d}"


def run(stage, d, run_id=None, status="ok", hour=12):
    return {
        "run_id": run_id or rid(stage, d), "stage": stage, "run_date": d, "status": status,
        "started_at": at(d, hour - 1), "finished_at": at(d, hour),
    }


def health(series, d, market="ZA", platform="tiktok", protocol="p1", lane_class="unbiased_rank",
           valid=True, units_ok=3, k=None, run_id=None):
    return {
        "day": d, "market": market, "platform": platform, "route": series, "series": series,
        "protocol": protocol, "lane_class": lane_class, "calls": 3, "calls_ok": 3 if valid else 0,
        "units_planned": units_ok, "units_ok": units_ok, "items": 50, "k": k, "valid": valid,
        "invalid_reason": None if valid else "calls", "run_id": run_id or rid("collect", d),
    }


def counter(item_id, series, d, value, market="ZA", platform="tiktok", protocol="p1",
            lane_class="unbiased_rank", unit="appearances", is_board=False, pull_seq=None,
            source="live", read_day=None, hour=12, run_id=None):
    read_day = read_day or d
    return {
        "obs_date": d, "market": market, "platform": platform, "item_id": item_id, "series": series,
        "route": series, "protocol": protocol, "is_board": is_board, "lane_class": lane_class,
        "unit": unit, "pull_seq": pull_seq, "value": value, "source": source,
        "observed_at": at(read_day, hour), "available_at": at(read_day, hour),
        "run_id": run_id or rid("collect", read_day),
    }


def item_daily(item_id, d, posts, market="ZA", platform="facebook", lane_class="panel",
               series="panel_fb_hub", protocol="p1", run_id=None, geo_known_posts=None, local_posts=None):
    return {
        "metric_date": d, "market": market, "platform": platform, "item_id": item_id,
        "lane_class": lane_class, "series": series, "protocol": protocol, "posts": posts,
        "geo_known_posts": geo_known_posts, "local_posts": local_posts,
        "run_id": run_id or rid("aggregate", d), "rule_version": "r1",
    }


def cmap(item_id, kind="hashtag"):
    return {"item_id": item_id, "kind": kind, "canonical_key": item_id, "label": item_id,
            "status": "active", "valid_from": at(day(400)), "valid_to": None}


def obs(post_id, d, lane_class, lane, market="ZA", platform="tiktok", series=None, protocol=None, hour=12):
    return {"post_id": post_id, "observed_at": at(d, hour), "observed_date": d, "market": market,
            "platform": platform, "series": series, "protocol": protocol, "lane": lane,
            "lane_class": lane_class, "run_id": rid("collect", d)}


def post(post_id, creator_id, d, platform="tiktok", tier="micro", **extra):
    return {"post_id": post_id, "platform": platform, "creator_id": creator_id,
            "creator_tier_at_post": tier, "published_at": at(d, 9), "post_date": d, **extra}


def creator(creator_id, coord_score=0):
    return {"creator_id": creator_id, "platform": "tiktok", "handle": creator_id,
            "coord_score": coord_score, "account_created_at": at(day(400))}
