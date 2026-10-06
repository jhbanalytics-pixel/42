"""Alerts for watches, computed from two days of item_state rows (core/api/contract.md section 10.5). Pure, no I/O."""
import json
import re

from core.api.today import STATE_WORDS

NO_MATCHES = "Waiting for creator views detection"
SURGE = "Creator surge: a post at three or more times their usual views"
NO_EARLIER_RUN = "Waiting for a second detect run to compare against"
KEY_KINDS = ("hashtag", "sound", "creator")
WORD_KINDS = ("brand", "query")


def normalise(value):
    return " ".join(str(value or "").lower().lstrip("#@").split())


def _obj(value):
    return json.loads(value) if isinstance(value, str) else value or {}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _targets(watch, target, items, matches):
    kind = target.get("kind")
    if kind == "item":
        return [i for i in items if i.get("item_id") == target.get("item_id")]
    if kind in KEY_KINDS:
        key = normalise(target.get("value"))
        return [i for i in items if i.get("kind") == kind and normalise(i.get("canonical_key")) == key]
    if kind in WORD_KINDS:
        if matches and watch["watch_id"] in matches:
            wanted = set(matches[watch["watch_id"]])
            return [i for i in items if i.get("item_id") in wanted]
        words = " ".join(str(target.get("value") or "").split())
        if not words:
            return []
        pattern = re.compile(r"(?<!\w)" + re.escape(words) + r"(?!\w)", re.IGNORECASE)
        return [i for i in items if any(pattern.search(" ".join(str(name or "").split()))
                                        for name in [i.get("label"), *(i.get("aliases") or [])])]
    return []


# States whose name does not read after "Entered".
STATE_ALERT_WORDS = {"new_to_42": "First spotted", "spike": "Spiked"}


def _fired(rule, today, before):
    if "state_in" in rule:
        states = rule["state_in"]
        if today.get("state") in states and (before or {}).get("state") not in states:
            if today["state"] in STATE_ALERT_WORDS:
                return STATE_ALERT_WORDS[today["state"]]
            return f"Entered {STATE_WORDS.get(today['state'], today['state'])}"
    for key, column, words in (("ratio_over", "main_ratio", "Growth passed {:g} times its baseline"),
                               ("reach_over", "creators3", "Reach passed {:g} creators in 3 days")):
        if key in rule:
            now, then = today.get(column), (before or {}).get(column)
            if _number(now) and now >= rule[key] and not (_number(then) and then >= rule[key]):
                return words.format(rule[key])
    if "breakout" in rule:
        now, then = today.get("breakout_creators"), (before or {}).get("breakout_creators")
        if _number(now) and now >= 1 and not (_number(then) and then >= 1):
            return f"Creator breakout: {now:g} creator{'' if now == 1 else 's'} well above their usual views"
    if "tone_flip" in rule:
        now, then = today.get("tone_today"), today.get("tone_before")
        if _number(now) and _number(then) and now * then < 0:
            return f"Tone flipped from {_sign(then)} to {_sign(now)}"
    return None


def _sign(tone):
    return "positive" if tone > 0 else "negative"


def evaluate(watches, items_today, items_yesterday, matches=None, matches_before=None):
    """Alerts for active watches (contract 10.5, without the card) plus a waiting entry per watch that cannot be
    judged yet.

    breakout reads breakout_creators on each item (the day's creator breakouts from breakout_signals, None when
    there are none) and fires when today has one and the run before did not.

    tone_flip reads tone_today and tone_before on today's item (the item's mean enrichment tone sign in that
    market on the run's date and the day before, from v_item_tone_daily, None under 5 enriched posts) and fires
    when one is above 0 and the other below. The comparison is already day over day, so the run before's row is
    not read for it.

    creator_surge reads matches (watch_matches for the run's date): detect counts a watched creator's posts at
    three or more times their usual views with core/detect/breakout.py and writes a match only when there is one,
    so the API fires on the items detect matched for that watch, and waits while matches is None. Like the other
    rules it fires on entering: not for an item matches_before (watch_matches on the earlier run's date) also
    holds for that watch.

    items_yesterday is None when there is no earlier good detect run: nothing can be said to have changed, so
    every watch waits instead of firing for everything already there."""
    yesterday = {(i.get("item_id"), i.get("market")): i for i in items_yesterday or []}
    out = []
    for watch in watches:
        if watch.get("status") != "active":
            continue
        rule, target = _obj(watch.get("rule")), _obj(watch.get("target"))
        if "creator_surge" in rule and matches is None:
            out.append({"watch_id": watch["watch_id"], "waiting": NO_MATCHES})
            continue
        if items_yesterday is None:
            out.append({"watch_id": watch["watch_id"], "label": watch.get("label"), "waiting": NO_EARLIER_RUN})
            continue
        market = watch.get("market")
        items = [i for i in items_today if market == "all" or i.get("market") == market]
        seen = set()
        if "creator_surge" in rule:
            wanted = (set(matches.get(watch["watch_id"]) or [])
                      - set((matches_before or {}).get(watch["watch_id"]) or []))
            targets = [i for i in items if i.get("item_id") in wanted]
        else:
            targets = _targets(watch, target, items, matches)
        for today in targets:
            key = (today.get("item_id"), today.get("market"))
            if key in seen:
                continue
            seen.add(key)
            because = SURGE if "creator_surge" in rule else _fired(rule, today, yesterday.get(key))
            if because:
                out.append({"watch_id": watch["watch_id"], "label": watch.get("label") or today.get("label"),
                            "item_id": today.get("item_id"), "market": today.get("market"),
                            "fired_because": because, "since": today.get("metric_date")})
    return out
