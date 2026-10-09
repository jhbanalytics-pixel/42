"""The HTML export of one finished Ask record (contract.md section 6, Export).

Lifted from app/src/api/ask_export.py: its escaping, its safe link check and
its serif page. The record must be complete or stopped with an answer, and
every claim must cite evidence the answer carries, with every quote found in
its record's text, or nothing is written.
"""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import date, datetime
from urllib.parse import urlsplit

from core.api import summary_state

EXPORTABLE = ("complete", "stopped")
# One line a copy carries when content was left out of it for people 42 no longer shows (core/api/privacy.py).
EXPORT_NOTICE = "Some content was left out of this copy because it concerned people 42 no longer shows."

_MARKET = {"ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya"}
_LABEL = {
    "observed": "Observed",
    "corroborated": "Corroborated",
    "single_source": "Single source",
    "inferred": "Inferred",
}
_ANSWER_STATUS = {
    "partial": "Partial answer",
    "insufficient_evidence": "Not enough evidence to answer",
    "refused": "Refused",
}
_PLATFORM = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "youtube": "YouTube",
    "reddit": "Reddit",
    "x": "X",
    "twitter": "X",
    "threads": "Threads",
    "bluesky": "Bluesky",
    "facebook": "Facebook",
    "linkedin": "LinkedIn",
    "telegram": "Telegram",
    "news": "News",
    "music_chart": "Music chart",
    "wikipedia": "Wikipedia",
    "web": "Web",
}
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_QUOTE_MARKS = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
})


# Visual QA, 5 October 2026: the export printed "59 located_posts (query q_14)", an empty Short answer and record
# ids such as obs1_7bfd... in its gaps. A unit reads as words, the query ids move to one line under the footer (every
# figure still carries its query), an empty short answer says why, and record ids become a count.
_UNITS = {
    "located_posts": "posts with a known location",
    "located_creators": "creators with a known location",
    "posts_read": "posts read",
    "unique_creators": "different creators",
}
_SINGULAR = {"posts": "post", "creators": "creator", "items": "item", "days": "day", "credits": "credit",
             "accounts": "account", "views": "view", "platforms": "platform", "topics": "topic",
             "searches": "search", "markets": "market", "hashtags": "hashtag"}
_RECORD_ID = re.compile(r"\bobs\d*_[0-9a-f]{8,}\b", re.I)
_BUDGET_STOP = "model cost or usage could not be verified within the per-question budget"
def short_answer_fallback(answer, why=None) -> str:
    """What stands in for a short answer that is not there: what did pass, as the Ask page says it
    (app/frontend/src/ask42.jsx noShortAnswer), then why the summary is missing. why is the sentence of the verified
    state, or the neutral sentence when the state is legacy or unverified (core/api/summary_state.py)."""
    claims = answer.get("claims") or []
    if not claims:
        return "Nothing passed the checks to sum up."
    evidence = [e for e in answer.get("evidence") or [] if isinstance(e, dict)]
    platforms = list(dict.fromkeys(_PLATFORM.get(e.get("platform"), e.get("platform")) for e in evidence
                                   if e.get("platform")))
    found = f"{len(claims)} checked finding" + ("" if len(claims) == 1 else "s")
    where = ""
    if evidence:
        where = f" from {len(evidence)} post" + ("" if len(evidence) == 1 else "s")
        if platforms:
            named = platforms[0] if len(platforms) == 1 else ", ".join(platforms[:-1]) + " and " + platforms[-1]
            where += " on " + named
    verb = " is" if len(claims) == 1 else " are"
    return found + where + verb + " below. " + (why or summary_state.NEUTRAL)


def _unit(value, unit):
    text = str(unit or "").strip()
    if text in _UNITS:
        text = _UNITS[text]
    elif re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", text, flags=re.I):
        text = text.replace("_", " ")
    if value == 1 and text:
        first, _, rest = text.partition(" ")
        if first.lower() in _SINGULAR:
            single = _SINGULAR[first.lower()]
            single = single[0].upper() + single[1:] if first[0].isupper() else single
            text = single + (" " + rest if rest else "")
    return text


def _searched(text):
    text = str(text or "")
    ids = _RECORD_ID.findall(text)
    if not ids:
        return text
    rest = re.sub(r"[\s,:]+$", "", re.sub(r"\s*\bobs\d*_[0-9a-f]{8,}\b,?", "", text, flags=re.I)).strip()
    if re.search(r"\bposts$", rest, re.I):
        return f"{len(ids)} " + (re.sub(r"posts$", "post", rest, flags=re.I) if len(ids) == 1 else rest)
    return (rest + ", " if rest else "") + f"{len(ids)} post{'' if len(ids) == 1 else 's'}"


def _status_words(answer, meta=None):
    words = summary_state.status_words(meta)
    if words:
        return words  # a verified state says how the run ended; the gap text is only read when it is not there
    if answer.get("status") == "insufficient_evidence":
        whys = {gap.get("why") for gap in answer.get("gaps") or [] if isinstance(gap, dict)}
        if _BUDGET_STOP in whys:
            return "Stopped at this question's model budget, not for lack of evidence"
        if "stopped on request" in whys:
            return "Stopped before an answer was written"
    return _ANSWER_STATUS.get(answer.get("status"))


class ExportRefused(Exception):
    def __init__(self, code, message):
        self.status = 409
        self.code = code
        self.message = message
        super().__init__(message)


def _e(value):
    return html.escape("" if value is None else str(value))


def _p(text, css=None):
    attribute = f' class="{css}"' if css else ""
    return f"<p{attribute}>{_e(text)}</p>"


def _normal(text):
    text = unicodedata.normalize("NFKC", str(text or "")).translate(_QUOTE_MARKS)
    return " ".join(text.split())


def _check(record):
    if not isinstance(record, dict) or record.get("status") not in EXPORTABLE:
        raise ExportRefused("not_ready", "Only a complete or stopped answer can be exported.")
    answer = record.get("answer")
    if not isinstance(answer, dict):
        raise ExportRefused("not_ready", "This ask has no answer to export.")
    evidence = {item.get("id"): item for item in answer.get("evidence") or []}
    for claim in answer.get("claims") or []:
        if not claim.get("evidence_ids"):
            raise ExportRefused("not_ready", f"Claim {claim.get('id')} cites no evidence.")
        for evidence_id in claim["evidence_ids"]:
            if evidence_id not in evidence:
                raise ExportRefused(
                    "not_ready", f"Claim {claim.get('id')} cites {evidence_id}, which is not in the answer."
                )
        for quote in claim.get("quotes") or []:
            source = evidence.get(quote.get("evidence_id"))
            if source is None:
                raise ExportRefused(
                    "not_ready",
                    f"Claim {claim.get('id')} quotes {quote.get('evidence_id')}, which is not in the answer.",
                )
            wanted = _normal(quote.get("text"))
            if not wanted or wanted not in _normal(source.get("text")):
                raise ExportRefused(
                    "not_ready",
                    f"A quote in claim {claim.get('id')} is not in the text of {quote.get('evidence_id')}.",
                )
    return answer


def _day(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _date_text(value):
    day = _day(value)
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}" if day else str(value or "date not recorded")


def _window_text(start, end):
    first, last = _day(start), _day(end)
    if first is None or last is None or first.year != last.year:
        return f"{_date_text(start)} to {_date_text(end)}"
    if first == last:
        return _date_text(end)
    if first.month != last.month:
        return f"{first.day} {_MONTHS[first.month - 1]} to {_date_text(end)}"
    return f"{first.day} to {_date_text(end)}"


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{value:,}".replace(",", " ") if isinstance(value, int) else repr(value)


def _count(value, word):
    return f"{_number(value)} {word}{'' if value == 1 else 's'}"


def _web_link(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
        return None
    return value.strip()


def _meta_line(record):
    parts = []
    market = record.get("market")
    if market:
        parts.append(_MARKET.get(market, market))
    run = record.get("run") or {}
    window = run.get("window") or {}
    if window.get("from") and window.get("to"):
        parts.append(_window_text(window["from"], window["to"]))
    if run.get("posts") is not None:
        parts.append(_count(run["posts"], "post"))
    if run.get("platforms") is not None:
        parts.append(_count(run["platforms"], "platform"))
    return " · ".join(parts)


_STYLE = (
    "html{background:#fff;color:#1b1b1b}"
    "body{margin:0 auto;max-width:44rem;padding:2rem 1.5rem;"
    "font-family:Georgia,'Times New Roman',serif;"
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


def render_answer_html(record: dict) -> str:
    """The export page for one finished Ask record, or ExportRefused."""
    answer = _check(record)
    evidence = answer.get("evidence") or []
    number_of = {item.get("id"): index for index, item in enumerate(evidence, start=1)}

    out = ["<header>", _p("42 Ogilvy Intelligence", "brand"), "<h1>42 answer</h1>"]
    out.append(_p("Question: " + str(record.get("question") or "")))
    meta = _meta_line(record)
    if meta:
        out.append(_p(meta, "meta"))
    state = summary_state.reader_meta(record)  # a raw record is judged here; a wire value only if it fits the record
    if _status_words(answer, state):
        out.append(_p(_status_words(answer, state), "review"))
    if summary_state.stopped_early_line(record, state):
        out.append(_p("Stopped before the end; this is what passed the checks by then.", "review"))
    if record.get("privacy"):
        out.append(_p(EXPORT_NOTICE, "review"))
    out.append("</header><main>")

    out.append("<section><h2>Short answer</h2>")
    short = str(answer.get("short_answer") or "").strip()
    out.append(_p(short) if short else _p(short_answer_fallback(answer, summary_state.summary_sentence(state)), "note"))
    if state.get("check") == "verified" and state["summary"]["state"] == "shown_rewritten" and short:
        out.append(_p(summary_state.module().READER_SENTENCES["shown_rewritten"], "note"))
    out.append("</section>")

    claims = answer.get("claims") or []
    queries = []
    if claims:
        out.append("<section><h2>What we found</h2>")
        for claim in claims:
            out.append("<article>")
            label = _LABEL.get(claim.get("label"), claim.get("label") or "Unlabelled")
            out.append(f"<p><strong>{_e(label)}</strong> {_e(claim.get('text'))}</p>")
            cites = [
                f'<a href="#source-{number_of[evidence_id]}">[{number_of[evidence_id]}]</a>'
                for evidence_id in claim.get("evidence_ids") or []
            ]
            if cites:
                out.append('<p class="note">Sources: ' + " ".join(cites) + "</p>")
            for quote in claim.get("quotes") or []:
                n = number_of[quote["evidence_id"]]
                out.append(
                    f"<blockquote>“{_e(quote.get('text'))}” "
                    f'<a href="#source-{n}">[{n}]</a></blockquote>'
                )
            for number in claim.get("numbers") or []:
                query = str(number.get("query_id") or "")
                if query and query not in queries:
                    queries.append(query)
                title = f' title="{html.escape("Query " + query, quote=True)}"' if query else ""
                out.append(f'<p class="note"{title}>'
                           f"{_e(_number(number.get('value')))} {_e(_unit(number.get('value'), number.get('unit')))}</p>")
            if claim.get("kind") == "proposal":
                if claim.get("basis"):
                    out.append(_p("Basis: " + str(claim["basis"]), "note"))
                if claim.get("falsifier"):
                    out.append(_p("What would change this: " + str(claim["falsifier"]), "note"))
            out.append("</article>")
        out.append("</section>")

    if answer.get("so_what"):
        out.append("<section><h2>So what</h2>")
        out.extend(_p(item.get("text")) for item in answer["so_what"])
        out.append("</section>")

    if answer.get("watch_next"):
        out.append("<section><h2>Watch next</h2>")
        for item in answer["watch_next"]:
            prefix = "Forecast: " if item.get("forecast") else ""
            out.append(_p(prefix + str(item.get("text") or "")))
        out.append("</section>")

    if answer.get("gaps"):
        out.append("<section><h2>What we do not know</h2>")
        for gap in answer["gaps"]:
            out.append("<article>")
            out.append(_p(gap.get("what")))
            if gap.get("searched"):
                out.append(_p("Searched: " + _searched(gap["searched"]), "note"))
            if gap.get("why"):
                out.append(_p("Why: " + str(gap["why"]).replace("_", " "), "note"))
            out.append("</article>")
        out.append("</section>")

    if answer.get("context"):
        out.append("<section><h2>Context</h2>")
        out.append(_p("General background, not evidence.", "note"))
        out.append(_p(answer["context"]))
        out.append("</section>")

    if evidence:
        out.append("<section><h2>Sources</h2>")
        for index, item in enumerate(evidence, start=1):
            platform = item.get("platform") or ""
            out.append(f'<article id="source-{index}">')
            out.append(
                f"<h3>[{index}] {_e(_PLATFORM.get(platform, platform))} · {_e(item.get('handle'))}</h3>"
            )
            out.append(_p(_date_text(item.get("posted_at")), "meta"))
            link = _web_link(item.get("url"))
            if link:
                out.append(f'<p><a href="{html.escape(link, quote=True)}">{_e(link)}</a></p>')
            else:
                out.append(_p(str(item.get("url") or "No link recorded") + " (not linked)", "note"))
            out.append(f"<blockquote>{_e(item.get('text'))}</blockquote>")
            out.append("</article>")
        out.append("</section>")

    run = record.get("run") or {}
    out.append("</main><footer>")
    footer = []
    if run.get("credits") is not None:
        footer.append(_count(run["credits"], "credit"))
    if run.get("seconds") is not None:
        footer.append(_count(run["seconds"], "second"))
    if run.get("run_id"):
        footer.append("run " + str(run["run_id"]))
    footer.append("ask " + str(record.get("ask_id") or ""))
    out.append(_p(" · ".join(footer), "note"))
    if queries:
        out.append(_p("Figures come from " + ("query " if len(queries) == 1 else "queries ") + ", ".join(queries), "note"))
    out.append(_p("Exported from 42. This copy was rebuilt from the stored answer and is not a live read.", "note"))
    out.append("</footer>")

    return (
        '<!doctype html><html lang="en-ZA"><head><meta charset="utf-8">'
        "<title>42 answer</title><style>" + _STYLE + "</style></head><body>"
        + "".join(out)
        + "</body></html>"
    )


def export_filename(ask_id) -> str:
    return "42-answer-" + re.sub(r"[^A-Za-z0-9_-]", "", str(ask_id)) + ".html"
