"""Dossiers: a finished answer assembled, reviewed, frozen and exported (contract.md section 13.2).

The server owns every claim. A dossier's claims are rebuilt by claim_id from the
source Ask record's stored answer, and only claims that passed the checks there
can appear. A client chooses which to keep, their order, the title and short
notes; anything else it sends is ignored. Lifted from app/src/api/intelligence_dossier.py
(the server-side allowlist projection), dossier_store.py (canonical digest of an
append-only version) and dossier_review_store.py (the review log), with the
approval roles dropped. Pure functions: storage and routes live in agent_app.py.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re

from core.api.export import (
    _LABEL, _MARKET, _PLATFORM, _STYLE, _date_text, _e, _normal, _number, _p, _web_link,
)

LABELS = ("observed", "corroborated", "single_source", "inferred")
NEEDS_TICK = ("single_source", "inferred")
FINISHED = ("complete", "stopped")
CLAIM_KEYS = ("text", "label", "kind", "evidence_ids", "quotes", "numbers", "basis", "falsifier")
MAX_TITLE = 200
MAX_NOTE = 1000


class Refused(Exception):
    def __init__(self, status, code, message, claims=None):
        self.status, self.code, self.message, self.claims = status, code, message, claims
        super().__init__(message)


def _bad(message):
    return Refused(400, "bad_request", message)


def answer_of(record):
    """The source's stored answer, or Refused when the record is not a finished answer."""
    if not isinstance(record, dict) or record.get("status") not in FINISHED or not isinstance(record.get("answer"), dict):
        raise Refused(409, "not_ready", "Only a complete or stopped answer can become a dossier.")
    return record["answer"]


def admitted(answer):
    """The source claims that passed the checks, by claim_id, and the ones left out with their reason."""
    claims, left_out = {}, []
    for claim in answer.get("claims") or []:
        cid = claim.get("id") if isinstance(claim, dict) else None
        if not isinstance(cid, str) or not cid or cid in claims:
            continue
        if claim.get("check") == "cut":
            left_out.append({"claim_id": cid, "reason": "cut by the checks"})
        elif claim.get("label") not in LABELS:
            left_out.append({"claim_id": cid, "reason": "carries no evidence label"})
        else:
            claims[cid] = claim
    return claims, left_out


def _ids(value, name, known):
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise _bad(f"{name} must be a list of claim ids.")
    unknown = [v for v in value if v not in known]
    if unknown:
        raise _bad(f"{name} names {', '.join(unknown)}, which the source answer does not hold as checked claims.")
    if len(set(value)) != len(value):
        raise _bad(f"{name} names a claim twice.")
    return value


def selection(body, record, previous):
    """The client's choice (keep, order, title, notes) checked against the source, defaulting to the previous version.

    Claim text, labels, quotes, evidence and numbers in the body are never read."""
    claims, _ = admitted(answer_of(record))
    if previous:
        keep = [c["claim_id"] for c in previous["claims"] if c["kept"] and c["claim_id"] in claims]
        title = previous["title"]
        notes = {c["claim_id"]: c["note"] for c in previous["claims"] if c.get("note") and c["claim_id"] in claims}
    else:
        keep, title, notes = list(claims), (record.get("question") or "Dossier")[:MAX_TITLE], {}
    if "keep" in body:
        keep = _ids(body["keep"], "keep", claims)
    if "order" in body:
        order = _ids(body["order"], "order", claims)
        stray = [cid for cid in order if cid not in keep]
        if stray:
            raise _bad(f"order names {', '.join(stray)}, which is not kept.")
        keep = order + [cid for cid in keep if cid not in order]
    if "title" in body:
        title = body["title"]
        if not isinstance(title, str) or not 0 < len(title.strip()) <= MAX_TITLE:
            raise _bad(f"title must be 1 to {MAX_TITLE} characters.")
        title = title.strip()
    if "notes" in body:
        given = body["notes"]
        if not isinstance(given, dict):
            raise _bad("notes must map claim ids to short notes.")
        _ids(list(given), "notes", claims)
        for cid, note in given.items():
            if not (note is None or (isinstance(note, str) and len(note) <= MAX_NOTE)):
                raise _bad(f"The note on {cid} must be words, up to {MAX_NOTE:,} characters.")
        notes = {**notes, **{cid: (note or "").strip() or None for cid, note in given.items()}}
    return {"keep": keep, "title": title, "notes": notes}


def check_current(from_version, latest_version):
    """Refuse an edit or freeze made from a version that is no longer the latest, so a second tab never buries
    another's draft. None (an older client that sends no from_version) is let through as before."""
    if from_version is None:
        return
    if isinstance(from_version, bool) or not isinstance(from_version, int) or from_version < 1:
        raise _bad("from_version must be the version number the change was made from.")
    if from_version != latest_version:
        raise Refused(409, "stale_version",
                      "This dossier changed since you opened it. Reload to see the latest version.")


def _claim(cid, claim, kept, note):
    out = {"claim_id": cid}
    out.update({key: copy.deepcopy(claim[key]) for key in CLAIM_KEYS if key in claim})
    out.setdefault("evidence_ids", [])
    out.setdefault("quotes", [])
    out.setdefault("numbers", [])
    out.update(kept=kept, note=note)
    return out


def build(record, sel, *, dossier_id, version, created_at, source, who="passcode", people=None):
    """One draft version, rebuilt from the source record with the client's selection.

    people, for a skin report, is who it may name: {"skin_id", "approved", "allowed"} (see shown)."""
    answer = answer_of(record)
    claims, left_out = admitted(answer)
    order = sel["keep"] + [cid for cid in claims if cid not in sel["keep"]]
    built = [_claim(cid, claims[cid], cid in sel["keep"], sel["notes"].get(cid)) for cid in order]
    cited = {eid for c in built if c["kept"] for eid in c["evidence_ids"]}
    body = {
        "dossier_id": dossier_id, "version": version, "state": "draft", "created_at": created_at, "who": who,
        "title": sel["title"], "source": dict(source), "source_ask_id": record.get("ask_id"),
        "question": record.get("question"), "market": record.get("market"),
        "as_of": answer.get("as_of"), "answer_status": answer.get("status"),
        "summary": answer.get("short_answer") if set(sel["keep"]) == set(claims) else None,
        "claims": built,
        "evidence": [copy.deepcopy(e) for e in answer.get("evidence") or [] if e.get("id") in cited],
        "gaps": copy.deepcopy(answer.get("gaps") or []),
        "left_out": left_out,
    }
    if people:
        body["people"] = copy.deepcopy(people)
    return body


def shown(body):
    """The body as a reader sees it. In a skin report (contract.md section 15.1) nobody it may not name shows:
    skins.mask_people works on a copy, so the stored quotes stay verbatim for the freeze re-check."""
    if any(not claim["kept"] for claim in body["claims"]):
        body = {**body, "summary": None}
    people = body.get("people")
    if not people:
        return body
    from core.api.skins import mask_people
    return mask_people(body, people.get("approved"), people.get("allowed"))


def freeze(draft, *, version, created_at, ticks):
    """The frozen copy of a draft, carrying the ticks that stood when it froze."""
    out = copy.deepcopy(draft)
    if any(not claim["kept"] for claim in out["claims"]):
        out["summary"] = None
    out.update(version=version, state="frozen", created_at=created_at, frozen_from=draft["version"],
               reviews={cid: {"ticked": t["ticked"], "note": t.get("note"), "at": t["at"]} for cid, t in ticks.items()})
    return out


def latest_ticks(rows):
    """The latest review row per claim_id (rows in the order they were written)."""
    out = {}
    for row in rows:
        out[row["claim_id"]] = row
    return out


def needs_tick(body, ticks):
    return [{"claim_id": c["claim_id"], "reason": f"{_LABEL[c['label']]} claims need a tick before freezing"}
            for c in body["claims"]
            if c["kept"] and c["label"] in NEEDS_TICK and not (ticks.get(c["claim_id"]) or {}).get("ticked")]


def recheck(body, record):
    """Every kept claim against the stored source (TRUST.md K1): what no longer matches, by claim."""
    answer = answer_of(record)
    claims, _ = admitted(answer)
    evidence = {e.get("id"): e for e in answer.get("evidence") or []}
    carried = {e.get("id") for e in body.get("evidence") or []}
    problems = []
    for claim in body["claims"]:
        if not claim["kept"]:
            continue
        cid = claim["claim_id"]
        why = []
        src = claims.get(cid)
        if src is None:
            problems.append({"claim_id": cid, "reason": "is no longer a checked claim in the source answer"})
            continue
        for key in ("text", "label", "evidence_ids", "quotes"):
            if claim.get(key) != src.get(key, [] if key in ("evidence_ids", "quotes") else None):
                why.append(f"its {key.replace('_', ' ')} differs from the source")
        if [n.get("result_hash") for n in claim["numbers"]] != [n.get("result_hash") for n in src.get("numbers") or []]:
            why.append("a figure's result_hash differs from the source")
        if not claim["evidence_ids"]:
            why.append("it cites no evidence")
        for eid in claim["evidence_ids"]:
            if eid not in evidence:
                why.append(f"it cites {eid}, which does not resolve in the source")
            elif eid not in carried:
                why.append(f"it cites {eid}, which the dossier does not carry")
        for quote in claim["quotes"]:
            source = evidence.get(quote.get("evidence_id"))
            wanted = _normal(quote.get("text"))
            if source is None or not wanted or wanted not in _normal(source.get("text")):
                why.append(f"a quote is not found verbatim in the text of {quote.get('evidence_id')}")
        if why:
            problems.append({"claim_id": cid, "reason": "; ".join(dict.fromkeys(why))})
    return problems


def content_hash(body):
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_html(body):
    """The export page of one frozen version, with numbered, working citations."""
    if body.get("state") != "frozen":
        raise Refused(409, "not_ready", "Only a frozen dossier version can be exported.")
    body = shown(body)
    kept = [c for c in body["claims"] if c["kept"]]
    evidence = body.get("evidence") or []
    number_of = {item.get("id"): n for n, item in enumerate(evidence, start=1)}
    reviews = body.get("reviews") or {}

    out = ["<header>", _p("42 Ogilvy Intelligence", "brand"), f"<h1>{_e(body['title'])}</h1>"]
    out.append(_p("Question: " + str(body.get("question") or "")))
    meta = [_MARKET.get(body.get("market"), body.get("market") or "")] if body.get("market") else []
    meta.append(f"Frozen version {body['version']}, {_date_text(body.get('created_at'))}")
    if body.get("as_of"):
        meta.append("answer of " + _date_text(body["as_of"]))
    out.append(_p(" · ".join(meta), "meta"))
    if body.get("answer_status") in ("partial", "insufficient_evidence"):
        out.append(_p("Partial answer" if body["answer_status"] == "partial" else "Not enough evidence to answer",
                      "review"))
    out.append("</header><main>")

    out.append("<section><h2>Summary</h2>")
    out.append(_p(body.get("summary")))
    out.append("</section>")

    if kept:
        out.append("<section><h2>What we found</h2>")
        for claim in kept:
            out.append("<article>")
            label = _LABEL.get(claim["label"], claim["label"])
            out.append(f"<p><strong>{_e(label)}</strong> {_e(claim.get('text'))}</p>")
            cites = [f'<a href="#source-{number_of[eid]}">[{number_of[eid]}]</a>'
                     for eid in claim["evidence_ids"] if eid in number_of]
            if cites:
                out.append('<p class="note">Sources: ' + " ".join(cites) + "</p>")
            for quote in claim["quotes"]:
                n = number_of.get(quote.get("evidence_id"))
                cite = f' <a href="#source-{n}">[{n}]</a>' if n else ""
                out.append(f"<blockquote>“{_e(quote.get('text'))}”{cite}</blockquote>")
            for figure in claim["numbers"]:
                out.append(_p(f"{_number(figure.get('value'))} {figure.get('unit') or ''} "
                              f"(query {figure.get('query_id')}, run {figure.get('run_id')}, "
                              f"result {figure.get('result_hash')})", "note"))
            if claim.get("kind") == "proposal":
                if claim.get("basis"):
                    out.append(_p("Basis: " + str(claim["basis"]), "note"))
                if claim.get("falsifier"):
                    out.append(_p("What would change this: " + str(claim["falsifier"]), "note"))
            review = reviews.get(claim["claim_id"])
            if review and review.get("ticked"):
                out.append(_p("Reviewed" + (": " + review["note"] if review.get("note") else ""), "note"))
            if claim.get("note"):
                out.append(_p("Reviewer's note: " + claim["note"], "note"))
            out.append("</article>")
        out.append("</section>")

    if body.get("gaps"):
        out.append("<section><h2>What we do not know</h2>")
        for gap in body["gaps"]:
            out.append("<article>")
            out.append(_p(gap.get("what")))
            if gap.get("searched"):
                out.append(_p("Searched: " + str(gap["searched"]), "note"))
            if gap.get("why"):
                out.append(_p("Why: " + str(gap["why"]).replace("_", " "), "note"))
            out.append("</article>")
        out.append("</section>")

    if evidence:
        out.append("<section><h2>Sources</h2>")
        for n, item in enumerate(evidence, start=1):
            platform = item.get("platform") or ""
            out.append(f'<article id="source-{n}">')
            out.append(f"<h3>[{n}] {_e(_PLATFORM.get(platform, platform))} · {_e(item.get('handle'))}</h3>")
            out.append(_p(_date_text(item.get("posted_at")), "meta"))
            link = _web_link(item.get("url"))
            if link:
                out.append(f'<p><a href="{html.escape(link, quote=True)}">{_e(link)}</a></p>')
            else:
                out.append(_p(str(item.get("url") or "No link recorded") + " (not linked)", "note"))
            out.append(f"<blockquote>{_e(item.get('text'))}</blockquote>")
            out.append("</article>")
        out.append("</section>")

    out.append("</main><footer>")
    out.append(_p(f"dossier {body['dossier_id']} · version {body['version']} · ask {body.get('source_ask_id')}", "note"))
    out.append(_p("Exported from 42. Every claim was rebuilt from the stored answer and checked against it "
                  "when this version froze; this copy is not a live read.", "note"))
    out.append("</footer>")
    return ('<!doctype html><html lang="en-ZA"><head><meta charset="utf-8">'
            f"<title>{_e(body['title'])}</title><style>" + _STYLE + "</style></head><body>"
            + "".join(out) + "</body></html>")


def export_filename(dossier_id, version, fmt):
    return f"42-dossier-{re.sub(r'[^A-Za-z0-9_-]', '', str(dossier_id))}-v{int(version)}.{fmt}"
