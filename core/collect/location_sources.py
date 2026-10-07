"""Location requests require reviewed inputs. Request region never becomes post geo."""

import re
from datetime import timedelta


ROUTES = ("youtube/search/advanced", "tiktok/location/posts", "tiktok/profile/region")


def profile_country(body, handle):
    data = body.get("data") if isinstance(body, dict) and body.get("success") is not False else None
    author = data.get("author") if isinstance(data, dict) else None
    if not isinstance(author, dict) or str(author.get("username") or "").lower() != str(handle).lower():
        return None
    country = author.get("location")
    return country.upper() if isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country) else None


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
