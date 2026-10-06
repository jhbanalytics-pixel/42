"""A standalone copy of one completed Ask answer and the sources it cites.

The copy is rebuilt from the stored engine reply that the question worker reads
back for the server scope. Nothing the browser holds reaches it, so a download
can only ever carry what the store recorded for that request. Before a word of
it is written the reply must name this request in a completed, settled state
and pass the same checks the in app view makes before it shows a reply
(intelligence_reply), including every quoted span against the digest the
engine bound to it. A reply the view would withhold is withheld whole here.

Two variants come out of one renderer. The HTML download uses the system serif
stack so it opens anywhere. The PDF variant names the packaged Newsreader face
and carries no title element, so the reviewed renderer can prove the page text
and the font are in the PDF it returns.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from src.api.client_lenses import LENS_BINDING_VERSION, registered_label
from src.api.intelligence_reply import validate_intelligence_reply

DETAIL_VERSION = "general_question_detail_v1"
REPLY_VERSION = "general_cultural_question_v1"
PDF_ASSET_ROOT = Path("/app/web/dist/assets")
PDF_FONT_FILE = "newsreader-variable-tcB4qQtO.woff2"
# A publication time the source never sent is the source's gap. Every other
# missing receipt field is a gap in 42's released data (the released relation
# carries no collection time, and the retained capture no source family
# column), so it is named as that and never blamed on the source.
NOT_RECORDED = "not recorded by the source"
NOT_RELEASED = "Not available in the released data"

REFUSALS = {
    "request_invalid": (400, "The answer reference is invalid."),
    "request_unavailable": (404, "This answer is not available in this workspace."),
    "answer_not_exportable": (
        409,
        "Only a completed answer can be exported. This request has no completed answer to export.",
    ),
    "answer_held": (
        409,
        "This answer is held until its usage record is settled, so it cannot be exported yet.",
    ),
    "answer_invalid": (
        502,
        "The stored answer did not pass its checks, so it was not exported.",
    ),
    "export_unavailable": (
        503,
        "The stored answer could not be read just now. Try again in a moment.",
    ),
    "pdf_busy": (429, "Another PDF is being made. Try again in a moment."),
    "pdf_unavailable": (
        503,
        "A PDF cannot be made on this server right now. Download the HTML copy "
        "instead; it carries the same answer and sources.",
    ),
}

_STATUS = {"complete": "Response complete", "partial": "Partial response"}
_SECTION = {
    "answer": "The read",
    "evidence": "Supporting observations",
    "interpretation": "Interpretation",
    "actions": "Actions",
}
_SECTION_ORDER = tuple(_SECTION)
_KIND = {
    "observation": "Observation",
    "interpretation": "Interpretation",
    "inference": "Inference",
    "proposal": "Proposal",
}
_SUPPORT = {
    "source_record": "Source record",
    "derived": "Derived from cited claims",
    "proposed": "Proposed, no record",
}
_MARKET = {"za": "South Africa", "ng": "Nigeria", "ke": "Kenya"}
_PLATFORM = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "youtube": "YouTube",
    "threads": "Threads",
    "reddit": "Reddit",
    "news": "News",
    "web": "Web",
    "x": "X",
    "twitter": "X",
    "google_search": "Google Search",
    "aggregate": "Aggregate",
    "facebook": "Facebook",
    "bluesky": "Bluesky",
    "web_attention": "Web attention",
    "app_store": "App Store",
}
_METHOD = {"selected_receipt_count": "Selected source records"}
# The same reader copy the in app answer uses for the engine's missing work codes.
_MISSING_WORK = {
    "answer_invalid": "The draft answer did not pass evidence validation. No answer was published.",
    "evidence_insufficient": "The available evidence cannot support this answer.",
    "no_matching_evidence": "No admissible evidence matched this request. The requested answer cannot be established from this read.",
    "parent_source_unavailable": "A previously cited source could not be verified for this request.",
    "retrieval_incomplete": "The evidence retrieval did not complete. No answer was generated from it.",
    "coverage_incomplete": "The available evidence does not cover the requested dates or scope.",
    "corroborated_foreign_local_only": "The matching records describe local events outside the requested market. Relevant evidence is still needed.",
}
_REQUIRED_EVIDENCE = "Missing required evidence: "
_REQUIREMENT_ID = re.compile(r"[A-Za-z0-9_.-]+")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec")
_UNFINISHED = ("unconfirmed", "unavailable", "refused", "needs_clarification")
_LENS_KEYS = {"lens_binding_version", "client_lens_id", "configuration_digest"}
_LENS_DIGEST = re.compile(r"[0-9a-f]{64}")
GENERAL_LENS_LINE = "Client lens: None (general 42)"


class AskExportRefused(Exception):
    def __init__(self, code):
        self.code = code
        self.status_code, self.message = REFUSALS[code]
        super().__init__(code)


class _Invalid(Exception):
    pass


def _require(condition):
    if not condition:
        raise _Invalid()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _span(excerpt, binding):
    return "".join(list(excerpt)[int(binding["start"]) : int(binding["end"])])


def _day(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def admitted_reply(detail, request_id):
    """The stored reply for request_id when it is a settled, completed answer.

    The reply itself must pass validate_intelligence_reply, the server's reading
    of the check the Ask view makes before it shows a reply, so the export
    carries exactly the answers the view shows and withholds the rest.
    """
    try:
        _require(isinstance(detail, dict) and detail.get("contract_version") == DETAIL_VERSION)
        _require(detail.get("request_id") == request_id and _text(detail.get("question")))
    except _Invalid:
        raise AskExportRefused("answer_invalid") from None
    state = detail.get("observed_state")
    if state in _UNFINISHED:
        raise AskExportRefused("answer_not_exportable")
    if state == "held":
        raise AskExportRefused("answer_held")
    try:
        _require(state in _STATUS)
        response = detail.get("response")
        _require(isinstance(response, dict) and response.get("error") is not True)
        reply = response.get("intelligence")
        _require(isinstance(reply, dict) and reply.get("contract_version") == REPLY_VERSION)
        _require(reply.get("request_id") == request_id and reply.get("status") == state)
    except _Invalid:
        raise AskExportRefused("answer_invalid") from None
    shown = validate_intelligence_reply(reply)
    if not shown["ok"]:
        raise AskExportRefused("answer_invalid")
    if "client_lens" in detail and not _lens_binding(detail["client_lens"]):
        raise AskExportRefused("answer_invalid")
    if reply["usage"]["status"] != "resolved":
        raise AskExportRefused("answer_held")
    return reply, shown["claims"], shown["receipts"], shown["readings"]


def _lens_binding(lens):
    return (
        isinstance(lens, dict)
        and set(lens) == _LENS_KEYS
        and lens["lens_binding_version"] == LENS_BINDING_VERSION
        and isinstance(lens["client_lens_id"], str)
        and bool(lens["client_lens_id"])
        and isinstance(lens["configuration_digest"], str)
        and _LENS_DIGEST.fullmatch(lens["configuration_digest"]) is not None
    )


def _lens_lines(detail, client_scope_id):
    """The lens the stored request was admitted under, never the one a page selects.

    A detail with no lens is a general 42 request. A named lens is given its
    registry name only while the registry pins the same configuration, enabled,
    under the client scope the stored request was read under, and is otherwise
    named by the identity the stored request carries.
    """
    lens = detail.get("client_lens")
    if lens is None:
        return GENERAL_LENS_LINE, None
    name = registered_label(
        lens["client_lens_id"], lens["configuration_digest"], client_scope_id=client_scope_id
    )
    return (
        "Client lens: " + (name or lens["client_lens_id"]),
        "Client lens configuration: "
        + lens["client_lens_id"]
        + ", "
        + lens["configuration_digest"],
    )


def _date_text(value):
    day = _day(value) if isinstance(value, str) and len(value) == 10 else None
    if day is None:
        stamp = _timestamp(value)
        if stamp is None:
            return NOT_RECORDED
        day = stamp.astimezone(UTC).date()
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def _window_text(start, end):
    """A window as the in app view writes it (readableWindow in
    askPresentation.js): the year once when both ends share it, the month once
    when they share that too, one date for one day, two full dates across
    years."""
    first, last = _day(start), _day(end)
    if first is None or last is None or first.year != last.year:
        return f"{_date_text(start)} to {_date_text(end)}"
    if first.month != last.month:
        return f"{first.day} {_MONTHS[first.month - 1]} to {_date_text(end)}"
    if first.day != last.day:
        return f"{first.day} to {_date_text(end)}"
    return _date_text(end)


def _as_of_text(value):
    stamp = _timestamp(value).astimezone(UTC)
    return f"{_date_text(value)}, {stamp:%H:%M} UTC"


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}".replace(",", " ") if isinstance(value, int) else repr(value)


def _market(value):
    return _MARKET.get(value, value.upper()) if value else NOT_RELEASED


def _source_name(receipt):
    platform = receipt.get("platform")
    if not platform:
        return receipt["source_label"]
    return _PLATFORM.get(platform.lower(), platform)


def _claim_text(claim, readings):
    text = claim["text"]
    if claim["kind"] != "observation" or len(claim["reading_ids"]) != 1:
        return text
    reading = readings[claim["reading_ids"][0]]
    label = _METHOD.get(reading["method"])
    supplied = (
        f"{reading['method']}: {reading['value']} {reading['unit']} "
        f"({reading['window']['start']} to {reading['window']['end']})."
    )
    if (
        label
        and type(reading["value"]) is int
        and reading["value"] >= 0
        and reading["unit"] == "records"
        and text == supplied
    ):
        return label + text[len(reading["method"]) :]
    return text


def _citations(claim, claims, readings, receipts):
    ids, visited = [], set()

    def add(receipt_id):
        if receipt_id not in ids:
            ids.append(receipt_id)

    def collect(item):
        if item["claim_id"] in visited:
            return
        visited.add(item["claim_id"])
        for receipt_id in item["receipt_ids"]:
            add(receipt_id)
        for reading_id in item["reading_ids"]:
            for receipt_id in readings[reading_id]["source_receipt_ids"]:
                add(receipt_id)
        for parent in item["parent_claim_ids"]:
            collect(claims[parent])

    collect(claim)
    return [receipts[receipt_id]["citation_label"] for receipt_id in ids]


def _web_link(value):
    if not value:
        return None, "Source link: " + NOT_RELEASED
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        parts = None
    if (
        parts is None
        or parts.scheme not in ("http", "https")
        or not parts.netloc
        or parts.username
        or parts.password
    ):
        return None, "Source link: withheld because it is not a web address"
    return value.strip(), None


def _missing_work_items(missing):
    """What is still needed, paired as the in app view pairs it.

    A port of missingWorkItems (generalIntelligence.js). The engine lists each
    unfulfilled mandatory requirement by its planned id ahead of the readable
    line built from the planned question, both in plan order. When the unknown
    ids before the first readable line match those lines one for one, and
    neither list repeats an entry, each readable line keeps its id as its
    requirement reference and each paired id is dropped only at its own
    position; otherwise every entry is shown as supplied.
    """
    readable = [item for item in missing if item.startswith(_REQUIRED_EVIDENCE)]
    first = next(
        (index for index, item in enumerate(missing) if item.startswith(_REQUIRED_EVIDENCE)),
        None,
    )
    positions = (
        []
        if first is None
        else [
            index
            for index, item in enumerate(missing[:first])
            if item not in _MISSING_WORK and _REQUIREMENT_ID.fullmatch(item)
        ]
    )
    ids = [missing[index] for index in positions]
    paired = (
        bool(readable)
        and len(ids) == len(readable)
        and len(set(readable)) == len(readable)
        and len(set(ids)) == len(ids)
    )
    dropped = set(positions) if paired else set()
    references = dict(zip(readable, ids)) if paired else {}
    return [
        {"text": _MISSING_WORK.get(item, item), "reference": references.get(item)}
        for index, item in enumerate(missing)
        if index not in dropped
    ]


def _p(text, css=None):
    attribute = f' class="{css}"' if css else ""
    return f"<p{attribute}>{html.escape(text)}</p>"


_STYLE = (
    "html{background:#fff;color:#1b1b1b}"
    "body{margin:0 auto;max-width:44rem;padding:2rem 1.5rem;font-family:FONT;"
    "font-size:11.5pt;line-height:1.5;font-variant-ligatures:none}"
    "h1{font-size:20pt;margin:.2rem 0 .6rem}h2{font-size:14pt;margin:1.6rem 0 .5rem;"
    "border-top:1px solid #cfcfcf;padding-top:.8rem}h3{font-size:12pt;margin:1rem 0 .3rem}"
    "p{margin:.25rem 0}.brand{color:#b3122e}.meta{color:#4a4a4a}"
    ".review{border:1px solid #b3122e;padding:.4rem .6rem;margin:.6rem 0}"
    ".kind{color:#4a4a4a;font-size:10pt}.note{color:#4a4a4a;font-size:10pt}"
    "article{margin:.8rem 0;break-inside:avoid}"
    "blockquote{margin:.4rem 0;padding-left:.8rem;border-left:3px solid #cfcfcf}"
    "a{color:#1b1b1b}"
)


def render_answer_html(detail, request_id, *, pdf=False, client_scope_id=None):
    """The export page for one stored answer, or AskExportRefused.

    client_scope_id is the client scope the stored request was read under. A
    stored lens is only given its registry name for that scope, so with no
    scope a lens is named by its stored identity alone.
    """
    reply, claims, receipts, readings = admitted_reply(detail, request_id)
    markets = " / ".join(_market(market) for market in reply["resolved_scope"]["market_scope"])
    window = reply["window"]
    scope_line = " · ".join(
        (
            _STATUS[reply["status"]],
            markets,
            _window_text(window["start"], window["end"]),
            "Closed observation window" if window["closed"] else "Open observation window",
        )
    )
    out = []
    out.append("<header>")
    out.append(_p("42 Ogilvy Intelligence", "brand"))
    out.append("<h1>Ask answer</h1>")
    lens_line, lens_configuration = _lens_lines(detail, client_scope_id)
    out.append(_p("Question: " + detail["question"]))
    out.append(_p(lens_line, "meta"))
    out.append(_p(scope_line, "meta"))
    if reply["review_required"]:
        out.append(_p("Review required before downstream use.", "review"))
    out.append("</header><main>")
    for section in reply["sections"]:
        out.append(f"<section><h2>{html.escape(_SECTION[section['kind']])}</h2>")
        for claim_id in section["claim_ids"]:
            claim = claims[claim_id]
            out.append("<article>")
            out.append(_p(f"{_KIND[claim['kind']]} · {_SUPPORT[claim['support_state']]}", "kind"))
            out.append(_p(_claim_text(claim, readings)))
            labels = _citations(claim, claims, readings, receipts)
            if labels:
                out.append(_p("Sources: " + ", ".join(labels), "note"))
            for reading_id in claim["reading_ids"]:
                reading = readings[reading_id]
                value = "Unmeasured" if reading["value"] is None else _number(reading["value"])
                denominator = (
                    "unmeasured" if reading["denominator"] is None else _number(reading["denominator"])
                )
                out.append(
                    _p(
                        f"Measurement: {value} {reading['unit']} · "
                        f"{_METHOD.get(reading['method'], reading['method'])} · "
                        f"{_window_text(reading['window']['start'], reading['window']['end'])} · "
                        f"{'Closed' if reading['window']['closed'] else 'Open'} window · "
                        f"Denominator: {denominator}",
                        "note",
                    )
                )
                out.extend(_p(item, "note") for item in reading["limitations"])
            out.extend(_p(item, "note") for item in claim["limitations"])
            if claim["falsifier"]:
                out.append(_p("What would change this: " + claim["falsifier"], "note"))
            out.append("</article>")
        out.append("</section>")
    if reply["limitations"]:
        out.append("<section><h2>Limits of this read</h2>")
        out.extend(_p(item) for item in reply["limitations"])
        out.append("</section>")
    if reply["missing_work"]:
        out.append("<section><h2>What is still needed</h2>")
        # Each needed item is its readable line. A requirement id the view keeps
        # behind its "Requirement reference" disclosure follows its line as a
        # quiet reference, open on the page so the PDF carries it as text.
        for item in _missing_work_items(reply["missing_work"]):
            out.append(_p(item["text"]))
            if item["reference"] is not None:
                out.append(_p("Requirement reference: " + item["reference"], "note"))
        out.append("</section>")
    if receipts:
        count = len(receipts)
        out.append("<section><h2>Source records</h2>")
        out.append(
            _p(
                f"{count} {'record' if count == 1 else 'records'} in this answer. "
                "Record counts do not establish independent corroboration.",
                "note",
            )
        )
        for receipt in receipts.values():
            out.append("<article>")
            out.append(
                f"<h3>{html.escape(receipt['citation_label'] + ' · ' + _source_name(receipt))}</h3>"
            )
            kind = "Aggregate evidence" if receipt["kind"] == "aggregate" else "Content record"
            out.append(_p(f"{kind} · Market: {_market(receipt['market'])}", "meta"))
            if not receipt["platform"]:
                out.append(_p("Platform: " + NOT_RELEASED))
            if receipt["author"]:
                out.append(_p("Author: " + receipt["author"]))
            if receipt["excerpt"]:
                out.append(f"<blockquote>{html.escape(receipt['excerpt'])}</blockquote>")
            else:
                out.append(_p("Excerpt: " + NOT_RELEASED))
            for binding in receipt.get("quote_bindings", []):
                out.append(_p(f"Cited span: “{_span(receipt['excerpt'], binding)}”"))
            collected = (
                f"Collected: {_date_text(receipt['collected_at'])}"
                if receipt["collected_at"]
                else "Collection time: " + NOT_RELEASED
            )
            out.append(_p(f"Published: {_date_text(receipt['published_at'])} · {collected}"))
            out.append(_p("Source family: " + (receipt["source_family"] or NOT_RELEASED)))
            link, reason = _web_link(receipt["url"])
            if link:
                out.append(f'<p><a href="{html.escape(link, quote=True)}">View original source</a></p>')
            else:
                out.append(_p(reason))
            out.append(_p("Record reference: " + (receipt["source_row_id"] or receipt["receipt_id"]), "note"))
            out.extend(_p(item, "note") for item in receipt["limitations"])
            out.append("</article>")
        out.append("</section>")
    out.append("</main><footer><h2>Request details</h2>")
    out.append(_p("Request reference: " + request_id))
    if lens_configuration is not None:
        out.append(_p(lens_configuration, "note"))
    out.append(_p("As of " + _as_of_text(reply["as_of"])))
    out.append(
        _p(
            "Exported from 42. This copy was rebuilt from the stored answer and is "
            "not a live read.",
            "note",
        )
    )
    out.append("</footer>")
    if pdf:
        head = (
            "<style>@font-face{font-family:Newsreader;src:url('"
            + PDF_FONT_FILE
            + "')}@page{size:A4;margin:16mm}"
            + _STYLE.replace("FONT", "Newsreader")
            + "</style>"
        )
    else:
        head = (
            f"<title>42 Ask answer {html.escape(request_id[:8])}</title><style>"
            + _STYLE.replace("FONT", "Georgia,'Times New Roman',serif")
            + "</style>"
        )
    return (
        '<!doctype html><html lang="en-ZA"><head><meta charset="utf-8">'
        + head
        + "</head><body>"
        + "".join(out)
        + "</body></html>"
    )
