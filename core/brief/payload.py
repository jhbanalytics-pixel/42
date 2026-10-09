"""One market's briefs payload (BUILD.md 1.12) in the shape of core/api/contract.md sections 4 and 8.

Pure Python: no cloud or model calls. f42-api adds tag, dropped and coverage itself.

critic lists, for each item whose explanation reached the critic, the critic's own answer as it returned it
(core/brief/explain.py CRITIC_FIELDS) with the item_id. It is kept for audit beside the cards and held items, never
on them, so no reader shows it: f42-api builds Today from the named fields and creator pages read cards.
"""

from collections import Counter

LABELS = {"ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya"}

# The brief job's fixed wording for a Today-bound item a busy model left unexplained (core/brief/job.py
# _explain_all): the deadline or the time limit came first, or the model kept refusing calls until the run stopped
# asking. An item that was not run carries no other failed_reason.
MODEL_BUSY = "Model busy: not explained before the deadline"
MODEL_REFUSED = "Model busy: the model kept refusing calls, so this was not explained"
NOT_RUN_REASONS = (MODEL_BUSY, MODEL_REFUSED)
# The held reason of a Today-bound item the explanation loop never reached (a cap, a day change or the deadline stopped
# the run before it) when the model was not busy: no explanation was tried, so no check failed.
NOT_REACHED_TEXT = "Not explained: the run stopped before this topic was reached"
NOT_ASSESSED_REASONS = {
    "outside_candidate_pool": "Outside the morning candidate pool; checks did not run.",
    "judged_limit_reached": "The morning assessment limit was reached before this topic; checks did not run.",
    "deadline_reached": "The morning deadline was reached before this topic; checks did not run.",
}

STATE_WORDS = {
    "new_to_42": "New to 42", "spike": "Spike", "on_the_boards": "On the boards", "emerging": "Emerging",
    "rising": "Rising", "peaking": "Peaking", "mainstream": "Mainstream", "fading": "Fading",
    "recurring": "Recurring", "seasonal": "Seasonal",
}

FLAG_WORDS = {
    "likely_coordinated": "Likely coordinated", "check_pattern": "Check pattern",
    "not_assessed": "Not assessed (thin sample)", "market_unconfirmed": "Market unconfirmed",
    "data_issue": "Data issue",
}
# The gate may hand over a code or a word; "clear" means no flag.
FLAG_CODES = {w.lower(): c for c, w in FLAG_WORDS.items()} | {c: c for c in FLAG_WORDS}
FLAG_CODES |= {"not assessed": "not_assessed", "clear": None}
# item_state authenticity values a card shows when the gate gives no flag.
ITEM_FLAGS = {"check_pattern", "not_assessed"}

# core/trust/gate.py Decision fields, read from a dict or by attribute.
DECISION_FIELDS = ("publish", "where", "flag", "reason", "rule", "numbers_only")

RULE_REASONS = {
    "G1": "data_issue", "G4": "likely_coordinated", "G4b": "political_unconfirmed", "G5": "paid_led",
    "G5b": "paid_led", "G6": "not_local", "G3": "not_confirmed", "G10": "explanation_failed",
}
REASON_TEXT = {
    "data_issue": "Data issue", "likely_coordinated": "Likely coordinated",
    "political_unconfirmed": "Political, not corroborated", "paid_led": "Paid-led",
    "not_local": "Not local to this market", "too_few_creators": "Too few creators",
    "not_confirmed": "Not confirmed", "explanation_failed": "Explanation failed its checks",
}

CARDS, SHOWN = 5, 10
SHOWN_POSTS = 3  # the first evidence posts a card shows, besides its thumbnails


def _iso(d):
    return d if isinstance(d, str) else d.isoformat()


def _fmt(v):
    if float(v).is_integer():
        return f"{int(v):,}"
    return f"{v:,.1f}"


def _is_growth(unit):
    return "times" in unit.split() or "ratio" in unit.split()


def _untested(c):
    return bool(c.get("untested")) or c["state"] == "new_to_42"


def _misses_floors(c):
    creators, posts = c.get("creators3") or 0, c.get("posts3") or 0
    if c["state"] == "new_to_42":
        return creators < 3
    return creators < 5 or posts < 8 or (c.get("top_creator_share3") or 0) > 0.4


def _worth(c):
    w = c.get("worth_raw")
    return (w is None, -(w or 0), c["item_id"])


# A count of one reads "1 creator", "1 post" (tester report, 5 October 2026: "1 creators and 1 posts").
ONE_NOUNS = {"creators": "creator", "posts": "post"}


def _count_line(numbers):
    """Numbers that share a window name it once: "10 creators and 10 posts in 3 days, 2.8 times usual"."""
    groups = []
    for n in numbers[:3]:
        noun, sep, window = n["unit"].partition(" in ")
        if n["value"] == 1:
            noun = ONE_NOUNS.get(noun, noun)
        part = f"{_fmt(n['value'])} {noun}"
        if sep and groups and groups[-1][0] == window:
            groups[-1][1].append(part)
        else:
            groups.append([window if sep else None, [part]])
    out = []
    for window, parts in groups:
        text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        out.append(f"{text} in {window}" if window else text)
    return ", ".join(out)


def _decision(c):
    d = c["decision"]
    return d if isinstance(d, dict) else {f: getattr(d, f) for f in DECISION_FIELDS}


def _flag(c, dec):
    if dec.get("flag"):
        return FLAG_CODES[dec["flag"].lower()]
    if c.get("geo_status") == "market_unconfirmed":
        return "market_unconfirmed"
    return c.get("authenticity") if c.get("authenticity") in ITEM_FLAGS else None


def _shown_first(evidence, shown_above):
    """The card's posts with those a higher card already shows moved after the rest, order kept otherwise."""
    return ([e for e in evidence if e["id"] not in shown_above]
            + [e for e in evidence if e["id"] in shown_above])


def _card(c, market, day, rank, shown_above=frozenset()):
    dec = _decision(c)
    untested = _untested(c)
    claims = c.get("claims") or []
    rests_on = c.get("explanation_claim_ids") or []
    explained = (not dec.get("numbers_only") and bool(c.get("explanation")) and bool(rests_on)
                 and set(rests_on) <= {cl["id"] for cl in claims})
    if not explained:
        claims, rests_on = [], []
    numbers = [n for n in c.get("numbers") or [] if not (untested and _is_growth(n["unit"]))]
    flag = _flag(c, dec)
    sp = c.get("sparkline") or {"unit": "posts a day", "points": []}
    points = [{"date": _iso(p["date"]), "value": p.get("value"),
               "expected_low": None if untested else p.get("expected_low"),
               "expected_high": None if untested else p.get("expected_high")} for p in sp["points"]]
    specificity = c.get("specificity")
    evidence = c.get("evidence") or []
    if isinstance(specificity, dict) and specificity.get("status") == "pass":
        local_order = {evidence_id: i for i, evidence_id in enumerate(specificity.get("local_evidence_ids") or [])}
        evidence = sorted(evidence, key=lambda e: (e.get("id") not in local_order, local_order.get(e.get("id"), 0)))
    evidence = _shown_first(evidence, shown_above)
    also = list(c.get("also") or [])
    count_line = _count_line(numbers)
    if also and count_line:
        # The numbers are the card's own item's, never a total over the items merged into it.
        count_line += f" ({c['title']} only)"
    # explained, else what the job reports: not_run (no explanation was attempted to the end) or failed_checks (G10
    # after the claim checks).
    status = "explained" if explained else (c.get("explanation_status") or "not_run")
    return {
        "item_id": c["item_id"], "market": market, "date": day, "rank": rank, "kind": c["kind"],
        "market_scope": "market" if c.get("market_scope") == "market" else "global",
        "market_posts7": c.get("market_posts7"), "total_posts7": c.get("total_posts7"),
        "market_share7": c.get("market_share7"),
        "title": c["title"], "title_written": _title_written(c, explained),
        "state": c["state"], "state_word": STATE_WORDS[c["state"]],
        "flag": flag, "flag_word": FLAG_WORDS.get(flag),
        "explained": explained, "explanation": c["explanation"] if explained else None,
        "explanation_claim_ids": list(rests_on), "claims": claims,
        "specificity": specificity,
        "count_line": count_line, "numbers": numbers,
        "sparkline": {"unit": sp.get("unit", "posts a day"), "points": points},
        "thumbnails": [e["id"] for e in evidence if e.get("thumbnail_url")][:2],
        "evidence_ids": [e["id"] for e in evidence], "evidence": evidence,
        "ask": f"What is behind {c['title']} in {LABELS[market]} this week?",
        "explanation_status": status,
        # The job's fixed wording for the check that held the explanation back; no model or scraped text.
        "failed_reason": _failed_reason(c, status),
        # Items merged into this card because their posts are this card's (core/brief/job.py), in rank order.
        "also": also,
        # A news event explains the rise and local creators add their own reaction; claims sit one step lower.
        "news_driven": explained and c.get("news_driven") is True,
    }


def _title_written(c, explained):
    """The writer's title that passed its checks (core/brief/explain.py), on an explained card only, else None; the
    card keeps title, its cluster label, either way."""
    title = c.get("title_written")
    return title.strip() if explained and isinstance(title, str) and title.strip() else None


def _failed_reason(c, status):
    """failed_reason as the job wrote it, on an item whose explanation failed its checks, or on one not run whose
    reason is a busy model's fixed wording (NOT_RUN_REASONS); else None."""
    reason = c.get("failed_reason")
    if status == "failed_checks" or (status == "not_run" and reason in NOT_RUN_REASONS):
        return reason
    return None


def _held_item(c):
    dec = _decision(c)
    # The brief job holds some items on its own grounds and names the contract reason code itself.
    reason = c.get("held_reason") or RULE_REASONS.get(dec.get("rule"))
    if reason is None:
        reason = "too_few_creators" if _misses_floors(c) else "not_confirmed"
    evidence = c.get("evidence") or []
    # The same figures a card would show, growth dropped while untested, so a reader can weigh the hold.
    numbers = [n for n in c.get("numbers") or [] if not (_untested(c) and _is_growth(n["unit"]))]
    held = {
        "item_id": c["item_id"], "title": c["title"], "rule": dec.get("rule"), "reason": reason,
        "reason_text": dec.get("reason") or REASON_TEXT[reason],
        "evidence_ids": [e["id"] for e in evidence], "evidence": evidence,
        "numbers": numbers, "count_line": _count_line(numbers),
        # The job's fixed wording for the check that held the explanation back, as on cards.
        "failed_reason": _failed_reason(c, c.get("explanation_status")),
        # failed_checks when the explanation ran and failed its checks; not_run when it never ran (a busy model's items
        # also carry its fixed wording as failed_reason). An item held on other grounds reads not_run.
        "explanation_status": c.get("explanation_status") or "not_run",
    }
    # Items merged into this one before it was held (core/brief/job.py _merge) stay listed, as on a card, so rule 5
    # holds for them too. The key is left out when nothing was merged, so every other held item keeps its shape.
    if c.get("also"):
        held["also"] = list(c["also"])
    return held


# The brief job's own holds, summarised by what held them (singular, plural); every other hold by its reason.
HELD_SUMMARY = {
    "Platform-generic tag": ("platform-generic tag", "platform-generic tags"),
    "Fewer than 3 posts 42 can show": ("with fewer than 3 posts", "with fewer than 3 posts"),
    "No readable name": ("without a readable name", "without a readable name"),
    "Evidence could not be read": ("with evidence that could not be read", "with evidence that could not be read"),
    NOT_REACHED_TEXT: ("not explained before the run stopped", "not explained before the run stopped"),
}


def _held_group(item):
    return item["reason_text"] if item["reason_text"] in HELD_SUMMARY else REASON_TEXT[item["reason"]].lower()


def _held_label(group, n):
    words = HELD_SUMMARY.get(group)
    return group if words is None else words[n != 1]


def _held_text(items):
    if not items:
        return "Nothing held back"
    counts = Counter(_held_group(i) for i in items)
    if len(counts) == 1:
        group = next(iter(counts))
        return f"{len(items)} held back: {_held_label(group, len(items))}"
    return f"{len(items)} held back: " + ", ".join(f"{n} {_held_label(g, n)}" for g, n in counts.most_common())


def _headline_ok(headline, market, shown):
    if headline.get("market", market) != market:
        return False
    card = next((c for c in shown if c["item_id"] == headline.get("item_id")), None)
    if card is None or not card["explained"] or not headline.get("claim_ids"):
        return False
    return set(headline["claim_ids"]) <= {cl["id"] for cl in card["claims"]}


def build_market_payload(market, brief_date, candidates, *, moments, boards, banners, headline=None, issues=(),
                         not_assessed=None, selection_receipt=None):
    day = _iso(brief_date)
    by_where = {"today": [], "held_back": [], "moments": []}
    for c in candidates:
        by_where[_decision(c)["where"]].append(c)

    ranked = sorted(by_where["today"], key=_worth)[:SHOWN]
    shown, shown_posts = [], set()
    for r, c in enumerate(ranked, 1):
        card = _card(c, market, day, r, shown_posts)
        shown.append(card)
        shown_posts |= set(card["evidence_ids"][:SHOWN_POSTS]) | set(card["thumbnails"])
    held = [_held_item(c) for c in sorted(by_where["held_back"], key=_worth)]
    seasonal = [{"date": day, "name": c["title"], "kind": "seasonal_item", "source": "detect",
                 "item_ids": [c["item_id"]]} for c in sorted(by_where["moments"], key=_worth)]

    refused = headline is not None and not _headline_ok(headline, market, shown)
    if any(b["kind"] == "data_issue" for b in banners):
        status = "data_issue"
    elif refused or any(not c["explained"] for c in shown) or any(
            i["reason"] == "explanation_failed" for i in held):
        status = "partial"
    else:
        status = "published"

    return {
        "market": market, "label": LABELS[market], "status": status,
        "headline": None if refused or headline is None else {
            "text": headline["text"], "market": market, "item_id": headline["item_id"],
            "claim_ids": list(headline["claim_ids"])},
        "banners": list(banners), "cards": shown[:CARDS], "more": shown[CARDS:],
        "held_back": {"count": len(held), "text": _held_text(held), "items": held},
        "not_assessed": not_assessed, "selection_receipt": selection_receipt,
        "moments": list(moments) + seasonal, "boards": list(boards),
        "coverage": {"issues": list(issues)},
        "critic": [{"item_id": c["item_id"], **c["critic"]} for c in candidates
                   if isinstance(c.get("critic"), dict)],
    }


def brief_row(market, brief_date, run_id, published_at, payload, rule_version):
    return {
        "brief_date": _iso(brief_date), "market": market, "run_id": run_id,
        "published_at": _iso(published_at), "status": payload["status"], "payload": payload,
        "rule_version": rule_version,
    }
