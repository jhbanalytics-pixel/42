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

from core.agent import answer_view
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
    """What stands in for a short answer that is not there: the count of what did pass, then why the summary is
    missing. why is the sentence of the verified state, or the neutral sentence when the state is legacy or unverified
    (core/api/summary_state.py). The Ask page writes the same line (noShortAnswer in app/frontend/src/ask42.jsx).
    app/frontend/src/ui/__tests__/answer-meta.test.jsx pins the page to the words this function wrote for each summary
    state, in the fixture it reads, so a change to the words here means making that fixture again."""
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
    return " · ".join(parts)


_LABEL_ORDER = ("inferred", "single_source", "observed", "corroborated")


def confidence_tag(claims) -> str | None:
    """One tag for the whole answer from the labels of its claims: the label when every claim carries it, otherwise
    "Mixed" (each claim keeps its own label under it). None when there is no claim."""
    labels = {c.get("label") for c in claims if isinstance(c, dict)}
    if not labels:
        return None
    if len(labels) == 1:
        label = next(iter(labels))
        return _LABEL.get(label, label or "Unlabelled")
    return "Mixed confidence"


def scope_line(record, answer) -> str:
    """What the answer rests on, then how much was read, then the store: "Answer based on 3 posts by 3 creators and
    4 comments · 169 posts read on 8 platforms · 3 208 posts in the store for 4 to 10 October 2026"."""
    evidence = [e for e in answer.get("evidence") or [] if isinstance(e, dict)]
    posts = [e for e in evidence if not answer_view.is_comment(e)]
    comments = [e for e in evidence if answer_view.is_comment(e)]
    creators = {(e.get("platform"), str(e.get("handle") or "").lstrip("@").lower()) for e in posts if e.get("handle")}
    run = record.get("run") or {}
    parts = []
    if posts or comments:
        based = "Answer based on "
        if posts:
            based += _count(len(posts), "post") + " by " + _count(len(creators) or len(posts), "creator")
        if comments:
            based += (" and " if posts else "") + _count(len(comments), "comment")
        parts.append(based)
    if run.get("posts") is not None:
        read = _count(run["posts"], "post") + " read"
        if run.get("platforms") is not None:
            read += " on " + _count(run["platforms"], "platform")
        parts.append(read)
    store = run.get("store") if isinstance(run.get("store"), dict) else {}
    window = run.get("window") or {}
    if store.get("posts") is not None:
        line = _count(store["posts"], "post") + " in the store"
        if window.get("from") and window.get("to"):
            line += " for " + _window_text(window["from"], window["to"])
        parts.append(line)
    return " · ".join(parts)


def _platform_counts(run):
    """Posts each platform returned, once per platform, and the calls that failed (a failed call is a note, not a
    row)."""
    totals, failed = {}, {}
    for source in run.get("source_status") or []:
        if not isinstance(source, dict) or not source.get("platform"):
            continue
        name = _PLATFORM.get(source["platform"], source["platform"])
        if source.get("status") in (None, "ok", "partial", "empty") and isinstance(source.get("items"), (int, float)):
            totals[name] = totals.get(name, 0) + int(source["items"])
        elif source.get("status") not in (None, "ok", "partial", "empty"):
            failed[name] = failed.get(name, 0) + 1
    return totals, failed


def _step_text(text):
    words = " ".join(str(text or "").split())
    if not words:
        return "Working on the question"
    if re.match(r"^Counting in the warehouse\b", words, re.I):
        return "Counting posts in the archive"
    if re.match(r"^Checking saved findings\b", words, re.I):
        return "Checking findings already saved on this question"
    if re.match(r"^A warehouse count\b", words, re.I):
        return re.sub(r"^A warehouse count", "A count in the archive", words, flags=re.I)
    if re.match(r"^Using\s+\S+$", words):
        return "Running a lookup"
    words = re.sub(r"^Critic:\s*", "Reviewing the claims: ", words)
    return re.sub(r",?\s*tier T\d\b", "", words)


def _timeline(steps):
    """Consecutive steps that read the same become one row with a count; the heading counts the rows."""
    rows = []
    for step in steps or []:
        text = _step_text(step.get("text") if isinstance(step, dict) else step)
        if rows and rows[-1][0] == text:
            rows[-1][1] += 1
        else:
            rows.append([text, 1])
    return rows


def _post_line(item, quote_text):
    text = " ".join(str(quote_text or item.get("text") or "").split())
    return text if len(text) <= 160 else text[:157].rstrip() + "..."


def _facts(item):
    parts = [_date_text(item.get("posted_at"))] if item.get("posted_at") else []
    views = (item.get("engagement") or {}).get("views")
    if isinstance(views, (int, float)) and not isinstance(views, bool):
        parts.append(_count(int(views), "view"))
    return " · ".join(parts)


_STYLE = (
    ":root{color-scheme:light dark;--ink:#1b1b1b;--quiet:#555;--rule:#d4d4d4;--wash:#f5f5f2;--brand:#b3122e;--bg:#fff}"
    "@media (prefers-color-scheme:dark){:root{--ink:#ececec;--quiet:#a9a9a9;--rule:#3a3a3a;--wash:#1d1d1d;"
    "--brand:#ff6b81;--bg:#121212}}"
    "html{background:var(--bg);color:var(--ink)}"
    "body{margin:0 auto;max-width:46rem;padding:2rem 1.5rem 3rem;"
    "font-family:system-ui,'Segoe UI',Helvetica,Arial,sans-serif;font-size:11.5pt;line-height:1.5}"
    "h1{font-size:21pt;line-height:1.2;margin:.3rem 0 .5rem}"
    "h2{font-size:13pt;margin:1.8rem 0 .5rem;border-top:1px solid var(--rule);padding-top:.8rem}"
    "h3{font-size:11.5pt;margin:.9rem 0 .2rem}h4{font-size:10pt;margin:.7rem 0 .2rem;color:var(--quiet);"
    "text-transform:uppercase;letter-spacing:.04em}"
    "p{margin:.25rem 0}.brand{color:var(--brand);font-weight:600}.meta,.note,.kind{color:var(--quiet)}"
    ".note,.kind{font-size:10pt}.review{border:1px solid var(--brand);padding:.4rem .6rem;margin:.6rem 0}"
    ".lead h2+p{font-size:13pt;line-height:1.45;margin:.6rem 0}"
    ".tag{display:inline-block;border:1px solid var(--ink);padding:.05rem .5rem;font-size:10pt;font-weight:600}"
    ".scope{color:var(--quiet);font-size:10pt;margin:.5rem 0}"
    "article{margin:.9rem 0;break-inside:avoid}"
    ".finding{border-left:3px solid var(--ink);padding-left:.9rem}"
    ".posts,.said{list-style:none;margin:.5rem 0;padding:0}"
    ".posts li{background:var(--wash);padding:.4rem .6rem;margin:.3rem 0}"
    ".said li{padding:.2rem 0 .2rem .8rem;border-left:2px solid var(--rule);color:var(--quiet);font-size:10.5pt}"
    "blockquote{margin:.4rem 0;padding-left:.8rem;border-left:3px solid var(--rule)}"
    ".figures{display:flex;flex-wrap:wrap;gap:.6rem;list-style:none;padding:0;margin:.4rem 0}"
    ".figures li{border:1px solid var(--rule);padding:.4rem .8rem;min-width:7rem}"
    ".figures li{font-size:12pt}"
    "details{margin:.8rem 0}summary{cursor:pointer;color:var(--quiet)}"
    "a{color:inherit}@media print{details{display:block}}"
)


def _render_post_tile(item, number, quote_text):
    platform = _PLATFORM.get(item.get("platform") or "", item.get("platform") or "")
    link = _web_link(item.get("url"))
    open_post = f' · <a href="{html.escape(link, quote=True)}">Open post</a>' if link else ""
    facts = _facts(item)
    return (
        f'<li><a href="#source-{number}">[{number}]</a> <strong>{_e(platform)}</strong> {_e(item.get("handle"))}'
        + (f' <span class="note">{_e(facts)}</span>' if facts else "")
        + f"{open_post}<br>{_e(_post_line(item, quote_text))}</li>"
    )


def _render_commenters(comments, quotes, number_of, by_url):
    """One quiet row per post the comments sit under, the comments beneath it."""
    out = ['<div class="commenters"><h4>What commenters said</h4>']
    groups = {}
    for item in comments:
        groups.setdefault(item.get("url") or "", []).append(item)
    for url, items in groups.items():
        parent = by_url.get(url)
        handle = answer_view.comment_parent_handle(items[0]) or (parent or {}).get("handle")
        whose = f"@{str(handle).lstrip('@')}'s post" if handle else "a post"
        link = _web_link(url)
        label = f'<a href="{html.escape(link, quote=True)}">{_e(whose)}</a>' if link else _e(whose)
        out.append(f'<p class="note">On {label}</p><ul class="said">')
        for item in items:
            n = number_of[item["id"]]
            out.append(f'<li><a href="#source-{n}">[{n}]</a> {_e(_post_line(item, quotes.get(item["id"])))}</li>')
        out.append("</ul>")
    out.append("</div>")
    return "".join(out)


def render_answer_html(record: dict) -> str:
    """The export page for one finished Ask record, or ExportRefused. It follows the page's order: the question, what
    is behind it, one confidence tag and what it rests on; the findings with their posts and, apart, what commenters
    said; the numbers and the platforms read; what could not be checked; how it was researched; technical details."""
    answer = _check(record)
    record = answer_view.present(record)
    answer = record["answer"]
    evidence = answer.get("evidence") or []
    number_of = {item.get("id"): index for index, item in enumerate(evidence, start=1)}
    by_id = {item.get("id"): item for item in evidence}
    by_url = {item.get("url"): item for item in evidence if not answer_view.is_comment(item)}
    claims = answer.get("claims") or []
    run = record.get("run") or {}

    out = ["<header>", _p("42 Ogilvy Intelligence", "brand"), "<h1>" + _e(record.get("question") or "42 answer") + "</h1>"]
    meta = _meta_line(record)
    if meta:
        out.append(_p(meta, "meta"))
    wire = record.get("answer_meta")  # the wire value the route prepared; this renderer judges nothing
    state = wire if summary_state.is_wire(wire) else {"check": "unverified", "problem": "shape"}
    if _status_words(answer, state):
        out.append(_p(_status_words(answer, state), "review"))
    if summary_state.stopped_early_line(record, state):
        out.append(_p("Stopped before the end; this is what passed the checks by then.", "review"))
    if record.get("privacy"):
        out.append(_p(EXPORT_NOTICE, "review"))
    tag = confidence_tag(claims)
    if tag:
        out.append(f'<p><span class="tag">{_e(tag)}</span></p>')
    scope = scope_line(record, answer)
    if scope:
        out.append(_p(scope, "scope"))
    out.append("</header><main>")

    out.append('<section class="lead"><h2>Short answer</h2>')
    short = str(answer.get("short_answer") or "").strip()
    out.append(_p(short) if short else _p(short_answer_fallback(answer, summary_state.summary_sentence(state)), "note"))
    if state.get("check") == "verified" and state["summary"]["state"] == "shown_rewritten" and short:
        out.append(_p(summary_state.module().READER_SENTENCES["shown_rewritten"], "note"))
    out.append("</section>")

    figures, queries = [], []
    if claims:
        out.append("<section><h2>What we found</h2>")
        for claim in claims:
            quotes = {q.get("evidence_id"): q.get("text") for q in claim.get("quotes") or []}
            cited = [by_id[i] for i in claim.get("evidence_ids") or [] if i in by_id]
            out.append('<article class="finding">')
            label = _LABEL.get(claim.get("label"), claim.get("label") or "Unlabelled")
            out.append(f"<p><strong>{_e(label)}</strong> {_e(claim.get('text'))}</p>")
            cites = [
                f'<a href="#source-{number_of[evidence_id]}">[{number_of[evidence_id]}]</a>'
                for evidence_id in claim.get("evidence_ids") or []
            ]
            if cites:
                out.append('<p class="note">Sources: ' + " ".join(cites) + "</p>")
            posts = [item for item in cited if not answer_view.is_comment(item)]
            comments = [item for item in cited if answer_view.is_comment(item)]
            if posts:
                out.append('<ul class="posts">' + "".join(
                    _render_post_tile(item, number_of[item["id"]], quotes.get(item["id"])) for item in posts) + "</ul>")
            if comments:
                out.append(_render_commenters(comments, quotes, number_of, by_url))
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
                words = f"{_number(number.get('value'))} {_unit(number.get('value'), number.get('unit'))}"
                if not any(words == known[0] for known in figures):
                    figures.append((words, query, _number(number.get("value")),
                                    _unit(number.get("value"), number.get("unit"))))
            if claim.get("kind") == "proposal":
                if claim.get("basis"):
                    out.append(_p("Basis: " + str(claim["basis"]), "note"))
                if claim.get("falsifier"):
                    out.append(_p("What would change this: " + str(claim["falsifier"]), "note"))
            out.append("</article>")
        out.append("</section>")

    if figures or _platform_counts(run)[0]:
        out.append("<section><h2>The numbers</h2>")
        if figures:
            out.append('<ul class="figures">' + "".join(
                f'<li{(" title=" + chr(34) + html.escape("Query " + q, quote=True) + chr(34)) if q else ""}>'
                f"{_e(value)} {_e(unit)}</li>" for _, q, value, unit in figures) + "</ul>")
        totals, failed = _platform_counts(run)
        if totals:
            out.append("<h3>Posts read by platform</h3>")
            out.append('<p class="note">' + _e(" · ".join(f"{name} {_number(count)}" for name, count in totals.items())) + "</p>")
        for name, count in failed.items():
            out.append(_p(f"{count} {name} search{'es' if count != 1 else ''} failed", "note"))
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

    gaps = [g for g in answer.get("gaps") or [] if isinstance(g, dict)]
    if gaps:
        out.append("<section><h2>What we do not know</h2>")
        for gap in gaps[:3]:
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

    rows = _timeline(record.get("steps"))
    if rows:
        out.append(f"<section><details><summary>How this was researched · {_count(len(rows), 'step')}</summary><ol>")
        out.extend(f"<li>{_e(text)}" + (f" ×{times}" if times > 1 else "") + "</li>" for text, times in rows)
        out.append("</ol></details></section>")

    if evidence:
        out.append("<section><h2>Sources</h2>")
        for index, item in enumerate(evidence, start=1):
            platform = item.get("platform") or ""
            out.append(f'<article id="source-{index}">')
            if answer_view.is_comment(item):
                handle = answer_view.comment_parent_handle(item)
                whose = f"@{handle}'s post" if handle else "a post"
                out.append(f"<h3>[{index}] Comment on {_e(whose)} ({_e(_PLATFORM.get(platform, platform))})</h3>")
                out.append(_p("Comment by " + str(item.get("handle") or "an unnamed account"), "meta"))
            else:
                out.append(f"<h3>[{index}] {_e(_PLATFORM.get(platform, platform))} · {_e(item.get('handle'))}</h3>")
            out.append(_p(_date_text(item.get("posted_at")), "meta"))
            link = _web_link(item.get("url"))
            if link:
                out.append(f'<p><a href="{html.escape(link, quote=True)}">{_e(link)}</a></p>')
            else:
                out.append(_p(str(item.get("url") or "No link recorded") + " (not linked)", "note"))
            out.append(f"<blockquote>{_e(item.get('text'))}</blockquote>")
            out.append("</article>")
        out.append("</section>")

    technical = []
    for gap in (run.get("technical_gaps") or gaps[3:]):
        if isinstance(gap, dict):
            technical.append(gap)
    notices = [n for n in run.get("notices") or [] if isinstance(n, str)]
    if technical or notices:
        out.append("<section><details><summary>Technical details</summary>")
        for notice in notices:
            out.append(_p(notice, "note"))
        for gap in technical:
            out.append("<article>")
            out.append(_p(gap.get("what")))
            if gap.get("searched"):
                out.append(_p("Searched: " + _searched(gap["searched"]), "note"))
            if gap.get("why"):
                out.append(_p("Why: " + str(gap["why"]).replace("_", " "), "note"))
            out.append("</article>")
        out.append("</details></section>")

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
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>42 answer</title><style>" + _STYLE + "</style></head><body>"
        + "".join(out)
        + "</body></html>"
    )


def export_filename(ask_id) -> str:
    return "42-answer-" + re.sub(r"[^A-Za-z0-9_-]", "", str(ask_id)) + ".html"
