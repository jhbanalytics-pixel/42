"""Location requests require reviewed inputs. Request region never becomes post geo."""

import re
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path


ROUTES = ("youtube/search/advanced", "tiktok/location/posts", "tiktok/profile")
COUNTRY_ROUTES = {"tiktok": "tiktok/profile", "instagram": "instagram/profile/about"}
# A successful account lookup that gave no recognised country is not bought again for this many days.
NO_COUNTRY_RETRY_DAYS = 30
UNSERVED = "credit_room"  # the reason an account got no country call for want of credit room
PLATFORM_ORDER = ("tiktok", "instagram")


@lru_cache(maxsize=1)
def _countries():
    codes = json.loads((Path(__file__).resolve().parents[1] / "config/profile_countries.json").read_text(encoding="utf-8"))
    return codes, {name.casefold(): code for code, name in codes.items()}


def country_code(value):
    if not isinstance(value, str):
        return None
    codes, names = _countries()
    value = " ".join(value.split())
    return value.upper() if value.upper() in codes else names.get(value.casefold())


def profile_author(body, handle):
    data = body.get("data") if isinstance(body, dict) and body.get("success") is not False else None
    author = data.get("author") if isinstance(data, dict) else None
    if isinstance(author, dict) and str(author.get("username") or "").casefold() == str(handle).casefold():
        return author
    return None


def profile_country(body, handle, *, platform="tiktok"):
    author = profile_author(body, handle)
    if author is None:
        return None
    if platform == "tiktok":
        country = author.get("location")
        return country_code(country) if isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country.strip()) else None
    if platform == "instagram":
        ext = author.get("ext")
        return country_code(ext.get("country")) if isinstance(ext, dict) else None
    return None


def _within(fetched_at, today, days):
    """True when fetched_at (a datetime, or an ISO string) falls on one of the last days days up to today."""
    if isinstance(fetched_at, str):
        try:
            fetched_at = datetime.fromisoformat(fetched_at)
        except ValueError:
            return False
    if not isinstance(fetched_at, datetime):
        return False
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    age = (today - fetched_at.astimezone(timezone.utc).date()).days
    return 0 <= age <= days


class ProfileCache:
    def __init__(self):
        self.known, self.accounts, self.attempted, self.unserved = {}, {}, set(), set()
        self.posts, self.creators = [], []

    @staticmethod
    def key(platform, handle):
        limit = {"tiktok": 24, "instagram": 30}.get(platform)
        if limit and isinstance(handle, str) and re.fullmatch(rf"[A-Za-z0-9_.]{{1,{limit}}}", handle):
            return platform, handle.casefold()
        return None

    def country(self, platform, handle):
        return self.known.get(self.key(platform, handle))

    def remember(self, platform, handle, raw):
        key, code = self.key(platform, handle), country_code(raw)
        if key and code:
            self.known[key] = code

    def seed(self, rows, *, today=None):
        """Stored receipts into the cache. With today, a successful profile lookup that gave no recognised country
        and was fetched within NO_COUNTRY_RETRY_DAYS counts as attempted, so the account is not looked up again
        daily; a receipt with no readable fetch time never does."""
        for row in rows:
            key = self.key(row.get("platform"), row.get("handle"))
            if key and row.get("country_source") == UNSERVED:
                self.unserved.add(key)
                continue
            sources = (COUNTRY_ROUTES.get(row.get("platform")),)
            if row.get("platform") == "instagram":
                sources += ("instagram/search/reels",)
            if key and row.get("country_source") in sources:
                if row.get("country_type") not in (None, "string", "null"):
                    continue
                raw = row.get("country")
                code = country_code(raw)
                if key[0] == "tiktok" and not (isinstance(raw, str) and re.fullmatch(r"[A-Za-z]{2}", raw.strip())):
                    code = None
                if code or (today is not None and key[1] and row.get("country_source") == COUNTRY_ROUTES.get(key[0])
                            and _within(row.get("fetched_at"), today, NO_COUNTRY_RETRY_DAYS)):
                    self.attempted.add(key)
                if key not in self.known and code:
                    self.remember(key[0], key[1], code)

    def signals(self, platform, handle, signals):
        if self.key(platform, handle) is None:
            return dict(signals)
        explicit = country_code(signals.get("home_market"))
        return {**signals, "home_market": explicit or self.country(platform, handle) or signals.get("home_market")}

    def bind(self, post, platform, market, handle, signals):
        key = self.key(platform, handle)
        if key and market in ("ZA", "NG", "KE"):
            self.accounts.setdefault(key, {"platform": platform, "handle": handle, "market": market})
            self.posts.append((post, platform, market, handle, dict(signals)))

    def track_creator(self, row, *, source=None):
        if source == COUNTRY_ROUTES.get(row.get("platform")):
            self.remember(row.get("platform"), row.get("handle"), row.get("home_market"))
        self.creators.append(row)

    def needed(self):
        """Accounts still to look up: the two platforms in turn, TikTok first. Within a platform the accounts
        left unserved for credit room come first, then the one with most market posts, then by account key."""
        posts = Counter(self.key(platform, handle) for _, platform, _, handle, _ in self.posts)
        lanes = {platform: sorted((key for key in self.accounts if key[0] == platform and key not in self.known
                                   and key not in self.attempted),
                                  key=lambda key: (key not in self.unserved, -posts[key], key))
                 for platform in PLATFORM_ORDER}
        order = []
        for turn in range(max(map(len, lanes.values()))):
            order += [lanes[platform][turn] for platform in PLATFORM_ORDER if turn < len(lanes[platform])]
        return [self.accounts[key] for key in order]

    def apply(self, geo_fn):
        for post, platform, market, handle, signals in self.posts:
            if self.country(platform, handle):
                result = geo_fn(platform, market, **self.signals(platform, handle, signals))
                post.update(geo_market=result[0], geo_confidence=result[1], geo_source=result[2])
        for row in self.creators:
            country = self.country(row.get("platform"), row.get("handle"))
            if country is not None:
                row["home_market"] = country


def plan(day, config):
    calls, held = [], []
    for market, settings in config.get("markets", {}).items():
        options = settings.get("location_collection") or {}
        market = market.upper()
        if market not in ("ZA", "NG", "KE"):
            continue
        for entry in options.get("youtube", []):
            geo = entry.get("location")
            radius = entry.get("location_radius")
            if not entry.get("reviewed") or not geo or not radius or not entry.get("query"):
                held.append({"market": market, "route": ROUTES[0], "reason": "missing_reviewed_geo"})
                continue
            params = {"query": entry["query"], "location": geo, "location_radius": radius,
                      "region": market, "order": "date", "max_results": 50,
                      "published_after": (day - timedelta(days=1)).isoformat() + "T00:00:00Z",
                      "published_before": day.isoformat() + "T00:00:00Z"}
            calls.append({"row": "C1", "route": ROUTES[0], "params": params, "market": market, "lane": "expansion"})
        for place in options.get("tiktok_places", []):
            if (not isinstance(place, dict) or not place.get("reviewed") or not isinstance(place.get("id"), str)
                    or not place["id"].strip()):
                held.append({"market": market, "route": ROUTES[1], "reason": "invalid_place_id"})
                continue
            calls.append({"row": "C1", "route": ROUTES[1], "params": {"location_id": place["id"]},
                          "market": market, "lane": "expansion"})
        for handle in options.get("tiktok_profiles", []):
            if not isinstance(handle, str) or not re.fullmatch(r"[A-Za-z0-9_.]{1,24}", handle):
                held.append({"market": market, "route": ROUTES[2], "reason": "invalid_profile"})
                continue
            calls.append({"row": "C1", "route": ROUTES[2], "params": {"handle": handle},
                          "market": market, "lane": "panel"})
    return calls, held
