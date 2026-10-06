"""Server-rendered printable one-pager for a query.

Pure rendering: the payload arrives fully assembled (and cached) from the ask
pipeline, so every number and quote on the card traces to a query result.
Identity tokens come from the PULSE mailer render layer.
"""

import html as _html

_MARKET_LABELS = {
    "za": "South Africa",
    "ng": "Nigeria",
    "ke": "Kenya",
    "all": "All markets",
}

_PLATFORM_LABELS = {
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "youtube": "YouTube",
    "threads": "Threads",
    "reddit": "Reddit",
    "twitter": "X",
    "news": "News",
    "rss": "News",
    "web": "Web",
}

_CSS = """
  * { box-sizing: border-box; }
  body { margin: 0; background: #ece4d4; color: #0d0d0f;
         font-family: Georgia, 'Times New Roman', serif; }
  .sheet { max-width: 780px; margin: 0 auto; background: #f4efe4; }
  .masthead { background: #0d0d0f; color: #f4efe4; padding: 30px 34px 24px; }
  .lockup { margin: 0; font-size: 42px; font-weight: 700; letter-spacing: 0.10em; }
  .lockup .tick { color: #ec3a1e; }
  .strap { margin: 8px 0 0; color: #ddd3bd; font-family: 'Courier New', monospace;
           font-size: 12px; text-transform: uppercase; letter-spacing: 0.16em; }
  .queryline { margin: 18px 0 0; color: #f4efe4; font-size: 20px; font-style: italic; }
  .meta { margin: 6px 0 0; color: #998e76; font-family: 'Courier New', monospace;
          font-size: 11px; letter-spacing: 0.08em; }
  .body { padding: 26px 34px 34px; }
  .section { margin: 0 0 10px; color: #b62a12; font-family: 'Courier New', monospace;
             font-size: 12px; text-transform: uppercase; letter-spacing: 0.16em; }
  .trend { border: 1px solid #d8cdb6; border-left: 4px solid #ec3a1e;
           background: #fbf8f0; padding: 14px 16px; margin: 0 0 12px;
           page-break-inside: avoid; }
  .statement { margin: 0; font-size: 18px; line-height: 1.35; }
  .chiprow { margin: 8px 0 0; font-family: 'Courier New', monospace; font-size: 11px;
             color: #3a352b; letter-spacing: 0.04em; }
  .chip { display: inline-block; border: 1px solid #b62a12; color: #b62a12;
          padding: 1px 8px; margin-right: 8px; text-transform: uppercase;
          letter-spacing: 0.10em; }
  .move { margin: 10px 0 0; font-size: 14px; line-height: 1.45; }
  .move strong { font-family: 'Courier New', monospace; font-size: 11px;
                 text-transform: uppercase; letter-spacing: 0.10em; color: #b62a12; }
  .note { border: 1px dashed #b8902a; background: #fbf8f0; color: #3a352b;
          padding: 12px 16px; margin: 0 0 12px; font-size: 14px; line-height: 1.45; }
  .quote { border-top: 1px solid #d8cdb6; padding: 12px 2px; page-break-inside: avoid; }
  .quote p { margin: 0; font-size: 14px; line-height: 1.5; }
  .quote .src { margin: 6px 0 0; font-family: 'Courier New', monospace; font-size: 11px;
                color: #8c826c; letter-spacing: 0.06em; }
  .stamp { border-top: 2px solid #0d0d0f; margin-top: 22px; padding-top: 12px;
           font-family: 'Courier New', monospace; font-size: 11px; color: #8c826c;
           letter-spacing: 0.08em; }
  @media print {
    body { background: #ffffff; }
    .sheet { max-width: none; }
    .masthead { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
    .trend, .chip { print-color-adjust: exact; -webkit-print-color-adjust: exact; }
  }
  @page { margin: 14mm; }
"""


def _esc(value) -> str:
    return _html.escape(str(value if value is not None else ""))


def _human_int(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return ("%.1fM" % (n / 1_000_000)).replace(".0M", "M")
    if n >= 1_000:
        return ("%.1fK" % (n / 1_000)).replace(".0K", "K")
    return str(n)


def _platform_label(platform: str) -> str:
    p = (platform or "").strip().lower()
    if not p:
        return "Social"
    return _PLATFORM_LABELS.get(p, p.title())


def _trend_block(trend: dict) -> str:
    plats = ", ".join(_platform_label(p) for p in trend.get("platforms") or [])
    mkts = ", ".join(
        _MARKET_LABELS.get((m or "").lower(), str(m).upper()) for m in trend.get("markets") or []
    )
    where = " | ".join(x for x in (plats, mkts) if x)
    parts = [
        '<div class="trend">',
        '<p class="statement">' + _esc(trend.get("statement")) + "</p>",
        '<p class="chiprow"><span class="chip">'
        + _esc(trend.get("momentum") or "Steady")
        + "</span>",
        "Backed by " + _esc(trend.get("evidence_count") or 0) + " posts",
    ]
    if where:
        parts.append(" | " + _esc(where))
    parts.append("</p>")
    how = (trend.get("how_to_use") or "").strip()
    if how:
        parts.append('<p class="move"><strong>How to use it</strong> ' + _esc(how) + "</p>")
    parts.append("</div>")
    return "".join(parts)


def _quote_block(quote: dict) -> str:
    src = " | ".join(
        x
        for x in (
            _esc(quote.get("handle")),
            _esc(_platform_label(quote.get("platform"))),
            _esc((quote.get("market") or "").upper()),
            _esc(_human_int(quote.get("engagement"))) + " engagement",
        )
        if x
    )
    return (
        '<div class="quote"><p>' + _esc(quote.get("text")) + "</p>"
        '<p class="src">' + src + "</p></div>"
    )


def render_card(payload: dict, generated_at) -> str:
    query = payload.get("query") or ""
    market = (payload.get("market") or "all").lower()
    market_label = _MARKET_LABELS.get(market, market.upper())
    trends = payload.get("trends") or []
    quotes = (payload.get("quotes") or [])[:6]
    total = payload.get("total_matches")
    match_label = (
        f"{total:,} matches in the retrieved 30-day window"
        if type(total) is int and total >= 0
        else "Match count unavailable"
    )
    stamp = generated_at.strftime("%d %b %Y, %H:%M UTC")

    head = (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>42 | " + _esc(query) + "</title>"
        "<style>" + _CSS + "</style></head><body>"
    )

    masthead = (
        '<header class="masthead">'
        '<h1 class="lockup">42<span class="tick">.</span></h1>'
        '<p class="strap">Sub-Saharan Africa cultural trends</p>'
        '<p class="queryline">' + _esc(query) + "</p>"
        '<p class="meta">'
        + _esc(market_label)
        + " | "
        + _esc(match_label)
        + "</p>"
        "</header>"
    )

    body_parts = ['<main class="body">']
    if trends:
        body_parts.append('<p class="section">Saved interpretation</p>')
        for t in trends:
            body_parts.append(_trend_block(t))
    else:
        body_parts.append('<p class="section">Retrieved evidence</p>')
        if payload.get("overall_read"):
            body_parts.append('<div class="note">' + _esc(payload["overall_read"]) + "</div>")

    body_parts.append('<div class="note">The response does not include original-source links. Any saved interpretation needs source review.</div>')

    if quotes:
        body_parts.append('<p class="section">The voice</p>')
        for q in quotes:
            body_parts.append(_quote_block(q))
        if payload.get("limited_voice"):
            body_parts.append('<div class="note">Limited voice sample for this topic.</div>')

    body_parts.append(
        '<p class="stamp">Rendered on '
        + _esc(stamp)
        + ". Retrieved records and any saved reading retain their original observation window.</p>"
    )
    body_parts.append("</main>")

    return head + '<div class="sheet">' + masthead + "".join(body_parts) + "</div></body></html>"
