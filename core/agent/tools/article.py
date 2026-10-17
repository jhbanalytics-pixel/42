"""The agent's read_article tool: the full text of a news article a live news search found in this run.

A Google News result carries a headline and a snippet. A poll's table, a survey's base or a result's breakdown sit in
the article body, so a quantitative answer needs the page itself. read_article opens one page with web/scrape (1
credit, never with a proxy or pdf_parse, as core/collect/local_sources.py does) and stores its main text as a new
evidence record, article_<hash>, that keeps the news result's publisher, link, date and market. Quotes and figures are
then checked word for word against that text like any post.

Bounds, in code: only the URL of a news record already in this run's evidence (the model cannot name a page), at most
MAX_ARTICLES per question, the site's robots.txt read first (a page it forbids or a robots read that fails is not
fetched), and the question's credit budget and the ask share's daily cap through the same client as every live call.
"""

from __future__ import annotations

import hashlib
import re

from core.agent.context import Refused, RunContext

ARTICLE_ROUTE = "web/scrape"
MAX_ARTICLES = 8
ARTICLE_CHARS = 20_000
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)[^)]*\)")


def article_id(url: str) -> str:
    return "article_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def plain_text(markdown: str) -> str:
    """Markdown as reading text: images out, links as their words, emphasis, heading and quote marks out, one line per
    paragraph, at most ARTICLE_CHARS."""
    text = _LINK.sub(r"\1", _IMAGE.sub("", markdown or ""))
    lines = [re.sub(r"\s+", " ", re.sub(r"^\s*[#>]+\s*|[*`]+", "", line)).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)[:ARTICLE_CHARS]


def read_article(ctx: RunContext, client, evidence_id: str, robots=None) -> dict:
    record = ctx.evidence.get(evidence_id)
    if not isinstance(record, dict) or record.get("platform") != "news" or str(evidence_id).startswith("article_"):
        raise Refused("read_article opens only a news result found by a live news search in this question; "
                      f"{str(evidence_id)[:60]!r} is not one")
    url = record.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise Refused("this news result has no web link to open")
    aid = article_id(url)
    if aid in ctx.evidence:
        return {"evidence_id": aid, "status": "ok", "already_read": True, "credits_spent": 0.0}
    read = sum(1 for eid in ctx.evidence if str(eid).startswith("article_"))
    if read >= MAX_ARTICLES:
        raise Refused(f"at most {MAX_ARTICLES} articles are read per question")
    if ctx.calls_left() <= 0:
        raise Refused(f"no SocialCrawl calls left for this question ({ctx.budget['calls']} allowed)")
    if robots is None or not robots.allowed(url):
        ctx.sc_calls.append({"route": ARTICLE_ROUTE, "params": {"url": url}, "status": "robots_refused"})
        return {"evidence_id": None, "status": "robots_refused", "credits_spent": 0.0,
                "reason": "the site's robots.txt does not allow this page to be read, or could not be read"}
    params = {"url": url, "only_main_content": "true"}
    quote = client.quote(ARTICLE_ROUTE, params)
    if quote > ctx.credits_left():
        raise Refused(f"{ARTICLE_ROUTE} costs {quote} credits, over the {ctx.credits_left()} left in this question")
    result = client.call(ARTICLE_ROUTE, params, lane="agent_live", run_id=ctx.run_id,
                         max_credits=min(float(quote), ctx.credits_left()))
    from core.agent.tools.socialcrawl import read_credits

    status = result.get("status")
    charged = read_credits(result.get("credits_charged"))
    if charged is None:
        charged, status = float(quote), "error"
    ctx.calls_made += 1
    ctx.credits_spent += charged
    ctx.emit("socialcrawl", route=ARTICLE_ROUTE, status=status, credits=charged)
    ctx.sc_calls.append({"route": ARTICLE_ROUTE, "params": {"url": url}, "status": status})
    text = plain_text(result.get("page") or "") if status == "ok" else ""
    if not text:
        return {"evidence_id": None, "status": status if status != "ok" else "empty", "credits_spent": charged,
                "reason": str(result.get("reason") or "")}
    ctx.evidence[aid] = {
        "id": aid, "platform": "news", "handle": record.get("handle"), "url": url,
        "posted_at": record.get("posted_at"), "market": record.get("market"), "text": text,
        "engagement": None, "flags": list(record.get("flags") or []), "source_market": record.get("source_market"),
    }
    return {"evidence_id": aid, "status": "ok", "credits_spent": charged, "characters": len(text),
            "text": "<untrusted_content>" + text[:4_000] + "</untrusted_content>"}
