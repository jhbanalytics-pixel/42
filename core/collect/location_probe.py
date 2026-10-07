"""A finite C1/C2 capture on the existing probe job. Default invocation prints the plan."""

import argparse
import hashlib
import json
import math
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from core.collect.parse import parse_with_creators
from core.collect.socialcrawl_client import BASE_URL, _reported, load_caps, params_hash, quote_for
from core.collect.x_discovery import project_discovery, status_url, verified_lookup


ROOT = Path(__file__).resolve().parents[2]
DEPENDENCIES = ("core/collect/socialcrawl_client.py", "core/collect/parse.py", "core/collect/x_discovery.py",
                "core/collect/location_sources.py", "core/config/caps.yaml", "core/detect/geo.py",
                "core/collect/job.py", "core/collect/probe.py", "core/collect/stores.py", "core/collect/chain.py",
                "core/collect/ids.py", "core/detect/items.py", "core/config/profile_countries.json")
OK = ("ok", "cached", "empty")
STOP = ("cap_reached", "balance_floor", "insufficient_credits")


class ProbeRefused(Exception):
    pass


def plan():
    return {"job": "f42-probe", "share": "build", "schedule_started": True,
            "max_credits": 15, "max_lookups": 2, "max_http_attempts": 13,
            "geo_scope": "existing probe configuration; request scope only",
            "calls": [
                {"route": "tiktok/profile", "market": "NG", "params": {"handle": "jessykiss_madridista"}},
                {"route": "instagram/profile/about", "market": "ZA", "params": {"handle": "primo9teen"}},
                {"route": "youtube/search/advanced", "params": {
                    "query": "BBNaija", "location": "6.52,3.37", "location_radius": "50km", "region": "NG",
                    "max_results": 5, "max_pages": 1, "includeExtras": "true",
                    "published_after": "2026-10-04T00:00:00Z", "published_before": "2026-10-07T00:00:00Z"}},
                {"route": "twitter/ai-search", "params": {
                    "query": "Find recent posts by the listed account and return their X status URLs",
                    "from_handles": "ARISEtv", "from_date": "2026-10-05", "to_date": "2026-10-06"}}]}


def source_hashes():
    files = (*DEPENDENCIES, "core/collect/location_probe.py")
    return {name: hashlib.sha256((ROOT / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest() for name in files}


def plan_hash():
    body = json.dumps({"plan": plan(), "source_hashes": source_hashes()}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class RequestGuard:
    def __init__(self, transport):
        self.transport = transport
        self.allowed = {(c["route"], params_hash("GET", c["params"])) for c in plan()["calls"]}
        self.lookups = set()
        self.counts, self.last = {}, {}
        self.attempts, self.allocated = 0, 0
        self.refused = False

    def sources(self, body):
        urls = []
        for row in project_discovery(body)["data"]["sources"]:
            url = row["url"]
            if url.split("/")[-3].lower() == "arisetv":
                urls.append(url)
        urls = urls[:plan()["max_lookups"]]
        self.lookups = {url.lower() for url in urls}
        return urls

    def __call__(self, method, url, *, params=None, json=None, headers=None, timeout=None):
        try:
            if method != "GET" or not url.startswith(BASE_URL + "/") or json is not None:
                raise ProbeRefused("unapproved request")
            route = url[len(BASE_URL) + 1:]
            params = params or {}
            key = (route, params_hash(method, params))
            balance = route == "credits/balance" and not params
            lookup = route == "twitter/tweet" and set(params) == {"url"} and \
                status_url(params["url"]) is not None and params["url"].lower() in self.lookups
            if not (balance or key in self.allowed or lookup):
                raise ProbeRefused("unapproved route or parameters")
            prior = self.counts.get(key, 0)
            if prior and (balance or prior >= 2 or self.last.get(key) != "zero_charge_refund"):
                raise ProbeRefused("request retry bound")
            if self.attempts >= plan()["max_http_attempts"]:
                raise ProbeRefused("request cardinality bound")
            quote = 0 if balance else quote_for(route, method, params)
            if not prior and self.allocated + quote > plan()["max_credits"]:
                raise ProbeRefused("probe credit hold")
        except ProbeRefused:
            self.refused = True
            raise
        if not prior:
            self.allocated += quote
        self.counts[key] = prior + 1
        self.attempts += 1
        self.last[key] = None
        response = self.transport(method, url, params=params or None, json=None, headers=headers, timeout=timeout)
        status, body, response_headers = response
        if status in (502, 503) and not (_reported(body if isinstance(body, dict) else None, response_headers) or 0):
            self.last[key] = "zero_charge_refund"
        if route == "twitter/ai-search":
            data = body.get("data") if isinstance(body, dict) else None
            invalid = not isinstance(data, dict) or not isinstance(data.get("sources"), list)
            body = project_discovery(body)
            for name in ("success", "cached"):
                if name in body and not isinstance(body[name], bool):
                    body.pop(name)
            credit = body.get("credits_used")
            if not isinstance(credit, (int, float)) or isinstance(credit, bool) or not math.isfinite(credit) or credit < 0:
                body.pop("credits_used", None)
            request = body.get("request_id")
            if not isinstance(request, str) or not re.fullmatch(r"req-[A-Za-z0-9_-]{1,100}", request):
                body.pop("request_id", None)
            if invalid:
                body["success"] = False
        return status, body, response_headers


def run(client, *, clock):
    p = plan()
    caps = load_caps()
    if (client.share != "build" or client.mode != "live" or client.retry_routes
            or client.share_caps["build"] > caps["BUILD_DAILY"]["after_schedule"]
            or isinstance(client.http, RequestGuard)):
        raise ProbeRefused("probe client configuration")
    guard = RequestGuard(client.http)
    client.http = guard
    out = {"run_id": client.run_id, "plan_sha256": plan_hash(), "records": [], "native_ids": [],
           "credits_charged": 0, "http_attempts": 0, "stopped": None,
           "parsed": {"posts": [], "observations": [], "counters": [], "creators": []}}
    why = client._over_cap(p["max_credits"])
    if why:
        out.update(stopped="cap_reached", reason=why)
        return out
    from core.collect.job import detect_fns

    item_id, geo = detect_fns()

    def capture(route, params, *, discovery=False, market="NG"):
        quote = quote_for(route, "GET", params)
        if out["credits_charged"] + quote > p["max_credits"]:
            out["stopped"] = "probe_cap"
            return None
        try:
            result = client.discover_x(params, market=market, use_cache=False) if discovery else client.call(
                route, params, market=market, lane="location_probe", seed_key=route, use_cache=False)
        except Exception as exc:
            out["stopped"] = "ledger_error"
            out["records"].append({"route": route, "status": "error", "failure": type(exc).__name__})
            return None
        out["credits_charged"] += result.credits_charged or 0
        body = result.body if isinstance(result.body, dict) else {}
        record = {"route": route, "market": market, "params_hash": result.params_hash, "status": result.status,
                  "credits_quoted": result.credits_quoted, "credits_charged": result.credits_charged,
                  "attempts": result.attempts, "failure": result.failure, "request_id": body.get("request_id")}
        out["records"].append(record)
        if (result.status in STOP or result.failure == "store" or guard.refused
                or (result.credits_charged or 0) > quote
                or out["credits_charged"] > p["max_credits"]):
            out["stopped"] = result.status if result.status in STOP else "probe_boundary"
        return result

    def parse_result(result, params, market="NG"):
        if result is None or result.status not in OK or not result.body:
            return
        try:
            parsed = parse_with_creators(result.route, params, market, result.body, clock(), client.run_id,
                                         item_id_fn=item_id, geo_fn=geo,
                                         lane="panel" if result.route in ("tiktok/profile", "instagram/profile/about") else "expansion")
            if result.route in ("tiktok/profile", "instagram/profile/about") and not parsed["creators"]:
                raise ValueError("profile owner absent")
        except Exception as exc:
            out["records"][-1].update(status="unparsable", parse_failure=type(exc).__name__)
            return
        for key in out["parsed"]:
            out["parsed"][key] += parsed[key]
        if result.route == "youtube/search/advanced":
            out["records"][-1]["hydration_nonnull"] = {
                field: sum(post[field] is not None for post in parsed["posts"])
                for field in ("views", "likes", "comments", "duration_s")}
            out["records"][-1]["post_count"] = len(parsed["posts"])
            out["records"][-1]["warnings"] = result.body.get("_warnings", [])

    for call in p["calls"][:-1]:
        market = call.get("market", "NG")
        parse_result(capture(call["route"], call["params"], market=market), call["params"], market)
        if out["stopped"]:
            break
    discovery = p["calls"][-1]
    if not out["stopped"]:
        result = capture(discovery["route"], discovery["params"], discovery=True)
        if result and result.status in OK and not out["stopped"]:
            window = tuple(date.fromisoformat(discovery["params"][k]) for k in ("from_date", "to_date"))
            for url in guard.sources(result.body):
                native = capture("twitter/tweet", {"url": url})
                if native and native.status in ("ok", "cached"):
                    if verified_lookup(native.body, url, ("ARISEtv",), window=window):
                        parse_result(native, {"url": url})
                        out["native_ids"].append(url.rsplit("/", 1)[1])
                    else:
                        out["records"][-1]["status"] = "lookup_refused"
                if out["stopped"]:
                    break
    out["http_attempts"] = guard.attempts
    return out


def main(argv=None, *, env=None, factory=None, clock=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-plan-sha256")
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({**plan(), "plan_sha256": plan_hash(), "source_hashes": source_hashes()}, sort_keys=True))
        return 0
    env = os.environ if env is None else env
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = clock()
    if (args.plan or args.approved_plan_sha256 != plan_hash() or env.get("CLOUD_RUN_JOB") != "f42-probe"
            or not env.get("CLOUD_RUN_EXECUTION") or env.get("CLOUD_RUN_TASK_INDEX") != "0"
            or env.get("CLOUD_RUN_TASK_ATTEMPT") != "0" or now.tzinfo is None
            or now.astimezone(timezone.utc).date() <= date(2026, 10, 6)):
        print(json.dumps({"status": "refused", "reason": "execution approval, runtime or closed-window guard"}))
        return 2
    if factory is None:
        from core.collect.probe import live_client

        factory = live_client
    run_id = "location-probe-" + hashlib.sha256(env["CLOUD_RUN_EXECUTION"].encode("utf-8")).hexdigest()[:24]
    result = run(factory(run_id, schedule_started=True), clock=clock)
    print(json.dumps(result, sort_keys=True))
    return 1 if result["stopped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
