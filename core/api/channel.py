"""The daily team-channel post to Slack or Teams (core/api/contract.md section 14.3).

A message already posted cannot be taken back, so it carries a closed list of fields: per market the brief state and
a Today link, the headline unless its item is creator-kind, up to three cards, the alerts and the scheduled answers'
status. No handle, no "@", no post text, no question and no answer text. The webhook address is a credential and is
never printed, logged or put in an error.

    F42_DATA=fixtures py -3.13 -m core.api.channel --date 2026-09-30 [--watches <json>] [--scheduled <json>] [--post]
"""
import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from core.api import discover, store as store_mod
from core.api.agent_app import current_watches
from core.api.digest import _day, app_url, topic_url
from core.api.today import LABELS, MARKETS, SAST, NotReady, build_today

MAX_CARDS = 3
MAX_LINES = 10
TIMEOUT = 20
BRIEF_WORDS = {"published": "Published", "partial": "Published with a data issue",
               "data_issue": "Published with a data issue"}
NO_BRIEF = "No brief today"
READY = "Today's brief is ready"
ANSWER_WORDS = {"complete": "Answered", "partial": "Partly answered", "insufficient_evidence": "Not enough evidence",
                "refused": "Refused", "failed": "Failed", "stopped": "Stopped", "running": "Still running"}
ITEM_WATCHES = ("item", "hashtag", "sound")
TEAMS_HOSTS = ("office.com", "office365.com", "logic.azure.com", "powerautomate.com", "powerplatform.com")
BAD_ADDRESS = "CHANNEL_WEBHOOK_URL is not a Slack or Teams incoming webhook address"
NO_SECRET = "CHANNEL_WEBHOOK_URL is not set: dry run only, nothing posted."


class ChannelError(RuntimeError):
    """A failed post, worded without the webhook address."""


def _creator(card):
    return card.get("kind") == "creator" or "@" in str(card.get("title") or "")


def _card_words(card, market):
    if _creator(card):
        word = card.get("state_word")
        return f"A creator is {word} in {LABELS[market]}" if word else f"A creator moved in {LABELS[market]}"
    return ", ".join(str(w) for w in (card.get("title"), card.get("state_word"), card.get("count_line")) if w)


def _headline(resp, markets, link):
    h = resp.get("headline") or {}
    card = next((c for c in (markets.get(h.get("market")) or {}).get("cards", []) +
                 (markets.get(h.get("market")) or {}).get("more", []) if c.get("item_id") == h.get("item_id")), None)
    if card is None or _creator(card) or "@" in str(h.get("text") or ""):
        return (READY, link)
    return (h["text"], link)


def _alert_line(a, base):
    market, kind = a.get("market"), a.get("watch_kind")
    link = topic_url(base, a["item_id"], market)
    if kind == "creator":
        return (f"A watched creator moved in {LABELS[market]}", link)
    if kind in ITEM_WATCHES:
        return (f"{_card_words(a['card'], market)}: {a['fired_because']}", link)
    return (f"A watched search moved in {LABELS[market]}", link)


def _answer_status(record):
    status = (record.get("answer") or {}).get("status") or record.get("status")
    return ANSWER_WORDS.get(status, "Finished")


def _capped(lines, rest_link):
    if len(lines) <= MAX_LINES:
        return lines
    return lines[:MAX_LINES] + [(f"{len(lines) - MAX_LINES} more on the Alerts screen", rest_link)]


def sections(resp, alerts, scheduled, base):
    """The message as sections of (words, link or None) lines, before any flavour's formatting."""
    base = base.rstrip("/")
    today_link = f"{base}/#/today?date={resp['date']}"
    alerts_link = f"{base}/#/alerts"
    markets = {m["market"]: m for m in resp.get("markets") or []}
    briefed = [code for code in MARKETS if (markets.get(code) or {}).get("status") in BRIEF_WORDS]
    top = [(f"42, {_day(resp['date'])}", None)]
    if briefed:
        top.append(_headline(resp, markets, today_link))
    out = [top]
    for code in MARKETS:
        m = markets.get(code) or {}
        lines = [(f"{LABELS[code]}: {BRIEF_WORDS.get(m.get('status'), NO_BRIEF)}", today_link)]
        lines += [(_card_words(c, code), topic_url(base, c["item_id"], code))
                  for c in (m.get("cards") or [])[:MAX_CARDS] if code in briefed]
        out.append(lines)
    if alerts:
        out.append([("Alerts", None)] + _capped([_alert_line(a, base) for a in alerts], alerts_link))
    if scheduled:
        answers = [(f"Scheduled question: {_answer_status(r)}", alerts_link) for r in scheduled]
        out.append([("Scheduled questions", None)] + _capped(answers, alerts_link))
    return out


def plain(parts):
    return "\n\n".join("\n".join(f"{w}: {u}" if u else w for w, u in lines) for lines in parts) + "\n"


def _slack_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack(parts):
    blocks = []
    for lines in parts:
        text = "\n".join(f"{_slack_escape(w)} <{u}|Open in 42>" if u else f"*{_slack_escape(w)}*"
                         for w, u in lines)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}})
    return {"text": _slack_escape(parts[0][0][0]), "blocks": blocks}


def _teams(parts):
    body = []
    for lines in parts:
        for i, (w, u) in enumerate(lines):
            block = {"type": "TextBlock", "wrap": True, "text": f"{w} [Open in 42]({u})" if u else w}
            if i == 0:
                block["separator"] = bool(body)
            if not u:
                block["weight"] = "Bolder"
            body.append(block)
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.4", "body": body}
    return {"type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


def render(resp, alerts, scheduled, base, flavour):
    """The webhook body for flavour "slack" or "teams"."""
    parts = sections(resp, alerts, scheduled, base)
    if flavour == "slack":
        return _slack(parts)
    if flavour == "teams":
        return _teams(parts)
    raise ValueError(f"Unknown channel flavour {flavour!r}")


def flavour_of(url):
    """ "slack" or "teams" from the webhook address; the error never repeats the address."""
    try:
        parts = urlsplit(str(url or "").strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        raise ValueError(BAD_ADDRESS) from None
    if parts.scheme != "https":
        raise ValueError(BAD_ADDRESS)
    if host == "hooks.slack.com":
        return "slack"
    if any(host == h or host.endswith("." + h) for h in TEAMS_HOSTS):
        return "teams"
    raise ValueError(BAD_ADDRESS)


def post(payload, url, http=None):
    """Posts the payload as JSON. A failure raises ChannelError worded as the status or the error class only."""
    http = http or urllib.request
    # Last guard on the whole message: a handle can reach no field, so any "@" means refuse, not post.
    if "@" in json.dumps(payload):
        raise ChannelError("Channel post refused: the message holds an @")
    failure = None
    try:
        req = http.Request(url, data=json.dumps(payload).encode("utf-8"),
                           headers={"Content-Type": "application/json"}, method="POST")
        with http.urlopen(req, timeout=TIMEOUT) as r:
            status = r.status
        if not 200 <= status < 300:
            failure = f"HTTP {status}"
    except urllib.error.HTTPError as exc:
        failure = f"HTTP {exc.code}"
    except Exception as exc:
        failure = type(exc).__name__
    if failure:
        raise ChannelError(f"Channel post failed: {failure}")


def gather(store, watches, date):
    """Today for `date` (markets with no brief row get status None) and that day's alerts with each watch's kind."""
    try:
        resp = build_today(store, date)
        present = {r["market"] for r in store.briefs(date)}
        resp = dict(resp, markets=[m if m["market"] in present else dict(m, status=None) for m in resp["markets"]])
    except NotReady:
        resp = {"date": date, "headline": None, "markets": []}
    try:
        found = discover.build_alerts(store, watches, date=date)
    except NotReady:
        found = {"date": None, "alerts": []}
    kinds = {}
    for w in watches:
        target = json.loads(w["target"]) if isinstance(w.get("target"), str) else w.get("target") or {}
        kinds[w.get("watch_id")] = target.get("kind")
    fired = [dict(a, watch_kind=kinds.get(a["watch_id"])) for a in found["alerts"]] if found["date"] == date else []
    return resp, fired


def _json_list(path):
    held = json.loads(Path(path).read_text(encoding="utf-8"))
    return held.get("watches") or [] if isinstance(held, dict) else held


def main(argv=None):
    p = argparse.ArgumentParser(prog="core.api.channel", description="Render and post the daily team-channel message.")
    p.add_argument("--post", action="store_true", help="post to CHANNEL_WEBHOOK_URL (default: print a dry run)")
    p.add_argument("--date", help="YYYY-MM-DD, default today in South Africa")
    p.add_argument("--watches", help="a JSON file of watches instead of the current watch list")
    p.add_argument("--scheduled", help="a JSON file of the day's scheduled Ask records")
    args = p.parse_args(argv)
    date = args.date or dt.datetime.now(SAST).date().isoformat()
    watches = _json_list(args.watches) if args.watches else current_watches()
    scheduled = json.loads(Path(args.scheduled).read_text(encoding="utf-8")) if args.scheduled else []
    if isinstance(scheduled, dict):
        scheduled = [scheduled]
    url = os.environ.get("CHANNEL_WEBHOOK_URL") or ""
    resp, fired = gather(store_mod.get_store(), watches, date)
    base = app_url()
    if args.post and url:
        try:
            flavour = flavour_of(url)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        try:
            post(render(resp, fired, scheduled, base, flavour), url)
        except ChannelError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"Posted the {flavour} channel message for {date}.")
        return 0
    if args.post:
        print(NO_SECRET, file=sys.stderr)
    print(plain(sections(resp, fired, scheduled, base)), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
