"""Current suppression on every reader of a stored or held copy (C5 v2: rules R1 to R11, sections 5 and 6).

A copy made while a person was visible keeps their handle, links and words. Every read builds a display copy from
the stored one with the list read now: the stored record is never changed (R5), and nothing here caches who is
hidden between requests (R2). When the list cannot be read the safe result is a copy with no posts, not a copy
that trusts nobody is hidden (R4)."""
import copy
import datetime as dt
import json
import logging
import re
import time

from core.api import today
from core.api.export import EXPORT_NOTICE  # noqa: F401  (the one line an export carries)
from core.api.store import creator_key

log = logging.getLogger("f42.api.privacy")

# Fixed words (C5 v2 section 5.7). Constants, never built from stored text.
SUMMARY_WITHHELD = "This summary was left out because part of it rested on posts 42 no longer shows."
GAP_WHAT = "Some findings were left out because they rested on posts 42 no longer shows."
CLAIM_WITHHELD = "This finding was left out because it rested on a post 42 no longer shows."
UNAVAILABLE_NOTE = "42 could not check its hidden-people list just now, so the posts behind this answer are not shown."
PEOPLE_UNAVAILABLE = "People view unavailable."

OBS_ID = re.compile(r"^obs1_[0-9a-f]{32}$")
MARGIN = dt.timedelta(days=3)
SUPPRESSION_MAX_AGE_S = 30  # a run in flight may use a list this old (rule R2, section 8.4)
READ = object()  # "read the list now"; None stands for UNAVAILABLE
_clock = time.monotonic


class LazyStore:
    """A store opened on first use, so a reader that finds nobody to hide, or whose list a test supplies, never opens
    one. Attribute access forwards to the store get_store returns."""

    def __init__(self, getter):
        self._getter, self._store = getter, None

    def __getattr__(self, name):
        if self._store is None:
            self._store = self._getter()
        return getattr(self._store, name)


class PeopleUnavailable(Exception):
    """The hidden-people list, or the post lookup that decides who wrote a post, could not be read."""


def read_hidden(store):
    """(keys, ids, names) of the people hidden now, or None when the list cannot be read (R4). Beyond what the people
    routes read, the current suppression rows add their own handle keys and creator ids: a suppression that names a
    platform and handle with no creators row behind it is not in the view, and the handle alone must still hide the
    person (C5 v2 section 3)."""
    base = today.hidden_people(store)
    if base is None:
        return None
    try:
        rows = store.suppressions()
    except Exception as exc:
        log.warning("the suppressions could not be read (%s); the list is unavailable", type(exc).__name__)
        return None
    keys, ids, names = base
    held = [r for r in rows or [] if isinstance(r, dict) and r.get("status") != "lifted"]
    extra_keys = {creator_key(r.get("platform"), r.get("handle")) for r in held if r.get("platform") and r.get("handle")}
    extra_ids = {r["creator_id"] for r in held if r.get("creator_id")}
    fresh = extra_ids - set(ids)
    if fresh:  # a row that names only an id: that creator's own handle is masked in text too
        try:
            extra_keys |= {creator_key(c.get("platform"), c.get("handle")) for c in store.creators_by_id(sorted(fresh)) or []}
        except Exception as exc:
            log.warning("the creators lookup failed (%s); the list is unavailable", type(exc).__name__)
            return None
    return keys | (extra_keys - {None}), ids | extra_ids, names


def _resolve(hidden, store):
    return read_hidden(store) if hidden is READ else hidden


def nothing_hidden(hidden):
    return hidden is not None and not hidden[0] and not hidden[1]


class FreshList:
    """A list read no older than SUPPRESSION_MAX_AGE_S, for a run in flight: its stream and its polls (case G)."""

    def __init__(self, store):
        self.store, self._at, self._hidden = store, None, None

    def get(self):
        if self._at is None or _clock() - self._at >= SUPPRESSION_MAX_AGE_S:
            self._hidden, self._at = read_hidden(self.store), _clock()
        return self._hidden


def _day(value):
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value).date()
    except ValueError:
        return None


def evidence_gone(evidence, hidden, store, creators=None):
    """The ids of the evidence records that belong to a hidden person (5.1). hidden None (UNAVAILABLE) gives every
    id. Stored posts are matched by who wrote them, found in one batched, date-bounded lookup of posts: a post the
    lookup cannot place is gone too. Raises PeopleUnavailable when that lookup fails. creators memoises post id to
    creator id across calls (the fact never changes; whether that person is hidden is never cached)."""
    records = [e for e in evidence or [] if isinstance(e, dict)]
    if hidden is None:
        return {e.get("id") for e in records}
    keys, ids, _ = hidden
    gone, stored = set(), []
    for e in records:
        if creator_key(e.get("platform"), e.get("handle")) in keys or e.get("creator_id") in ids:
            gone.add(e.get("id"))
        elif isinstance(e.get("id"), str) and OBS_ID.match(e["id"]):
            stored.append(e)
    creators = {} if creators is None else creators
    todo = [e for e in stored if e["id"] not in creators]
    if todo:
        days = [_day(e.get("posted_at")) for e in todo]
        known = [d for d in days if d is not None]
        found = {}
        if known:
            try:
                reader = getattr(store, "post_creators", None)
                rows = reader([e["id"] for e, d in zip(todo, days) if d is not None],
                              (min(known) - MARGIN).isoformat(), (max(known) + MARGIN).isoformat()) \
                    if callable(reader) else None
            except Exception as exc:
                log.warning("the post lookup failed (%s); the copy is not shown", type(exc).__name__)
                raise PeopleUnavailable from exc
            if rows is None:
                raise PeopleUnavailable
            found = {r["post_id"]: r.get("creator_id") for r in rows}
        for e in todo:
            if e["id"] in found:
                creators[e["id"]] = found[e["id"]]
    for e in stored:
        if e["id"] not in creators or creators[e["id"]] in ids:
            gone.add(e["id"])  # a miss is never read as "not hidden"
    return gone


def mask(value, hidden):
    """value with the hidden people's handles, links and names masked in every string (R10); hidden None masks every
    @mention and profile link, as no handle list exists."""
    return today.without_hidden(value, hidden)


def _marker(state, posts=0, claims=0, summary=False):
    out = {"v": 1, "state": state, "withheld": {"posts": posts, "claims": claims, "summary": summary}}
    if state == "unavailable":
        out["note"] = UNAVAILABLE_NOTE
    return out


def _inbound(marker):
    """The marker f42-agent sent over its authenticated hop: a notice, kept only if it has exactly the shape code
    writes. A marker from anywhere else is discarded (R6)."""
    if not isinstance(marker, dict) or marker.get("v") != 1 or marker.get("state") not in ("applied", "unavailable"):
        return None
    w = marker.get("withheld")
    if (not isinstance(w, dict) or set(w) != {"posts", "claims", "summary"}
            or any(type(w[k]) is not int or w[k] < 0 for k in ("posts", "claims"))
            or not isinstance(w["summary"], bool)):
        return None
    return _marker(marker["state"], w["posts"], w["claims"], w["summary"])


def _merge(a, b):
    if a is None or b is None:
        return a or b
    state = "unavailable" if "unavailable" in (a["state"], b["state"]) else "applied"
    return _marker(state, a["withheld"]["posts"] + b["withheld"]["posts"],
                   a["withheld"]["claims"] + b["withheld"]["claims"],
                   a["withheld"]["summary"] or b["withheld"]["summary"])


def _claim_ids(claim):
    ids = set(claim.get("evidence_ids") or [])
    ids |= {q.get("evidence_id") for q in claim.get("quotes") or [] if isinstance(q, dict)}
    return ids


def _json(value):
    return json.dumps(value, sort_keys=True, default=str)


def project_record(record, store, hidden=READ, inbound=None):
    """An Ask record (or an investigation's) as a reader may see it now (5.2). The input is not changed. inbound is
    the privacy marker of f42-agent's response to f42-api's own call, merged with this pass's (R7). The typed
    summary state, answer_meta, is read from the raw record by its own code and never rewritten here."""
    if not isinstance(record, dict):
        return record
    base = {k: v for k, v in record.items() if k != "privacy"}
    carried = _inbound(inbound)
    hidden = _resolve(hidden, store)
    if nothing_hidden(hidden):
        return base if carried is None else {**base, "privacy": carried}
    answer = base.get("answer")
    evidence = answer.get("evidence") if isinstance(answer, dict) else None
    try:
        gone = evidence_gone(evidence, hidden, store)
    except PeopleUnavailable:
        hidden = None
        gone = evidence_gone(evidence, None, store)
    unavailable = hidden is None
    out = copy.deepcopy(base)
    meta = out.pop("answer_meta", None)
    claims_gone = posts_gone = 0
    summary_gone = False
    ans = out.get("answer")
    if isinstance(ans, dict):
        posts_gone = len([e for e in ans.get("evidence") or [] if isinstance(e, dict) and e.get("id") in gone])
        ans["evidence"] = [e for e in ans.get("evidence") or [] if not (isinstance(e, dict) and e.get("id") in gone)]
        claims = ans.get("claims") if isinstance(ans.get("claims"), list) else []
        kept = [c for c in claims if isinstance(c, dict) and not unavailable and not _claim_ids(c) & gone]
        claims_gone = len(claims) - len(kept)
        present = {c.get("id") for c in kept}
        ans["claims"] = kept
        for key in ("so_what", "watch_next"):
            if isinstance(ans.get(key), list):
                ans[key] = [i for i in ans[key] if isinstance(i, dict) and set(i.get("claim_ids") or []) <= present]
        summary = ans.get("short_answer")
        if (claims_gone or unavailable) and isinstance(summary, str) and summary.strip():
            ans["short_answer"], summary_gone = SUMMARY_WITHHELD, True
        if claims_gone or posts_gone:
            gaps = ans.get("gaps") if isinstance(ans.get("gaps"), list) else []
            ans["gaps"] = gaps + [{"what": GAP_WHAT, "searched": "", "why": "withheld_privacy"}]
        if ans.get("status") == "complete" and not kept:
            ans["status"] = "partial"
        _ranked(out, claims_gone, present, hidden, unavailable)
    before = _json(out)
    out = mask(out, hidden)
    changed = _json(out) != before
    if meta is not None:
        out["answer_meta"] = meta
    if unavailable:
        marker = _marker("unavailable", posts_gone, claims_gone, summary_gone)
    elif posts_gone or claims_gone or summary_gone or changed:
        marker = _marker("applied", posts_gone, claims_gone, summary_gone)
    else:
        marker = None
    marker = _merge(marker, carried)
    if marker:
        out["privacy"] = marker
    return out


def _ranked(record, claims_gone, present, hidden, unavailable):
    run = record.get("run")
    ranked = run.get("ranked_list") if isinstance(run, dict) else None
    if not isinstance(ranked, dict) or not isinstance(ranked.get("items"), list):
        return
    keys = set() if hidden is None else hidden[0]
    items, dropped = [], False
    for item in ranked["items"]:
        if not isinstance(item, dict) or (item.get("claim_id") not in present and (claims_gone or unavailable)):
            dropped = True
            continue
        item = dict(item)
        if dropped:
            item["tied_with_previous"] = False
        dropped = False
        handle = item.get("usage_handle")
        if handle and (unavailable or creator_key(ranked.get("platform"), handle) in keys):
            item["usage_handle"] = None
        items.append(item)
    if items:
        run["ranked_list"] = {**ranked, "items": items}
    else:
        run.pop("ranked_list", None)


def project_dossier(view, store, hidden=READ):
    """A dossier version as a reader may see it now (5.3). The stored body is never changed, and content_hash stays
    the stored row's hash (R11)."""
    if not isinstance(view, dict):
        return view
    base = {k: v for k, v in view.items() if k != "privacy"}
    hidden = _resolve(hidden, store)
    if nothing_hidden(hidden):
        return base
    try:
        gone = evidence_gone(base.get("evidence"), hidden, store)
    except PeopleUnavailable:
        hidden = None
        gone = evidence_gone(base.get("evidence"), None, store)
    unavailable = hidden is None
    out = copy.deepcopy(base)
    posts_gone = len([e for e in out.get("evidence") or [] if isinstance(e, dict) and e.get("id") in gone])
    out["evidence"] = [e for e in out.get("evidence") or [] if not (isinstance(e, dict) and e.get("id") in gone)]
    withheld, summary_gone = 0, False
    for claim in out.get("claims") or []:
        if unavailable or _claim_ids(claim) & gone:
            claim.update(text=CLAIM_WITHHELD, evidence_ids=[], quotes=[], withheld=True)
            for key in ("basis", "falsifier"):
                if key in claim:
                    claim[key] = ""
            withheld += 1
    if withheld and isinstance(out.get("summary"), str) and out["summary"].strip():
        out["summary"], summary_gone = SUMMARY_WITHHELD, True
    before = _json(out)
    stored_hash = out.get("content_hash")
    out = mask(out, hidden)
    changed = _json(out) != before
    if "content_hash" in base:
        out["content_hash"] = stored_hash
    if unavailable:
        out["privacy"] = _marker("unavailable", posts_gone, withheld, summary_gone)
    elif posts_gone or withheld or changed:
        out["privacy"] = _marker("applied", posts_gone, withheld, summary_gone)
    return out


def project_summary(summary, store, hidden=READ):
    """A dossier list row: title and question masked, counts untouched."""
    hidden = _resolve(hidden, store)
    if nothing_hidden(hidden):
        return summary
    masked = mask({k: summary.get(k) for k in ("title", "question")}, hidden)
    return {**summary, **masked}


def selectable(claim, source_evidence, hidden, store, gone=None):
    """Whether a source claim may be kept in a dossier now: none of its evidence is a hidden person's and masking
    its words with the list changes nothing (section 7). hidden None (UNAVAILABLE) makes nothing selectable."""
    if hidden is None:
        return False
    if _claim_ids(claim) & (evidence_gone(source_evidence, hidden, store) if gone is None else gone):
        return False
    words = {k: claim.get(k) for k in ("text", "basis", "falsifier")}
    words["quotes"] = [q.get("text") for q in claim.get("quotes") or [] if isinstance(q, dict)]
    return _json(mask(words, hidden)) == _json(words)


def project_event(kind, data, hidden, store, gone, seen):
    """One event of a run in flight, or None to withhold it (5.5). gone collects the ids of the evidence left out of
    this stream so a claim citing one is left out too; seen memoises post id to creator id for the stream. hidden
    None (UNAVAILABLE) sends no evidence or claim event."""
    if kind == "evidence":
        record = data.get("evidence") if isinstance(data.get("evidence"), dict) else {}
        try:
            left_out = hidden is None or bool(evidence_gone([record], hidden, store, seen))
        except PeopleUnavailable:
            left_out = True
        if left_out:
            gone.add(record.get("id"))
            return None
        return mask(data, hidden)
    if kind == "claim":
        claim = data.get("claim") if isinstance(data.get("claim"), dict) else {}
        if hidden is None or _claim_ids(claim) & gone:
            return None
        return mask(data, hidden)
    return mask(data, hidden)


NO_STORE = {"Cache-Control": "no-store"}


def selectable_claims(claims, evidence, hidden, store):
    """{claim id: whether it may be kept in a dossier now} for the source answer's claims (see selectable)."""
    if hidden is None:
        return {cid: False for cid in claims}
    gone = evidence_gone(evidence, hidden, store)
    return {cid: not _claim_ids(c) & gone and selectable(c, evidence, hidden, store, gone) for cid, c in claims.items()}


def claims_on_hidden_posts(record, store, hidden=READ):
    """The ids of the claims of an Ask record that cite a hidden person's post now; raises PeopleUnavailable when the
    list cannot be read."""
    hidden = _resolve(hidden, store)
    if hidden is None:
        raise PeopleUnavailable
    answer = record.get("answer") if isinstance(record, dict) else None
    if not isinstance(answer, dict):
        return []
    gone = evidence_gone(answer.get("evidence"), hidden, store)
    return [c.get("id") for c in answer.get("claims") or [] if isinstance(c, dict) and _claim_ids(c) & gone]


def withhold_digest(resp, store):
    """The alerts of a digest with every post of a hidden person reduced to "A post on <platform>": no handle, no
    address, no text (section 8.6). When the list cannot be read no post keeps a handle or an address. A sent mail
    cannot be recalled, so this acts on creation."""
    hidden = read_hidden(store)
    if nothing_hidden(hidden):
        return resp
    out = copy.deepcopy(resp)
    cards = [a["card"] for a in out.get("alerts") or [] if isinstance(a.get("card"), dict)]
    records = [e for c in cards for e in c.get("evidence") or [] if isinstance(e, dict)]
    try:
        gone = evidence_gone(records, hidden, store)
    except PeopleUnavailable:
        hidden, gone = None, evidence_gone(records, None, store)
    for card in cards:
        card["evidence"] = [{"id": e.get("id"), "platform": e.get("platform"), "withheld": True}
                            if isinstance(e, dict) and e.get("id") in gone else e for e in card.get("evidence") or []]
    return mask(out, hidden)
