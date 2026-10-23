"""The agent's read_article tool: the full text of a news article a live news search found in this run.

A Google News result carries a headline and a snippet. A poll's table, a survey's base or a result's breakdown sit in
the article body, so a quantitative answer needs the page itself. read_article opens one page with web/scrape (1
credit, never with a proxy or pdf_parse, as core/collect/local_sources.py does) and stores its main text as a new
evidence record, article_<hash>, that keeps the news result's publisher, link, date and market. Quotes and figures are
then checked word for word against that text like any post.

Bounds, in code: only the URL of a news record already in this run's evidence (the model cannot name a page), at most
MAX_ARTICLES paid page reads per question, whether or not the page gave text, and a page already tried is not paid
for again; the site's robots.txt read first (a page it forbids or a robots read that fails is not fetched), and the
question's credit budget and the ask share's daily cap through the same client as every live call.

The robots.txt read goes from 42's own service, so it has its own getter (RobotsGet), not the collect job's http_get:
it reaches only hosts that resolve to public addresses, re-checks every redirect, reads at most ROBOTS_MAX_BYTES and
gives up after ROBOTS_DEADLINE_S across its attempts. Any of those failing reads as rules unknown: the page is not
fetched.

The text shown to the research model is fenced as untrusted content with any fence tag in the page neutralised
(socialcrawl._fence); the stored evidence keeps the page's words, since every later prompt fences it again.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit

from core.agent.context import Refused, RunContext

ARTICLE_ROUTE = "web/scrape"
MAX_ARTICLES = 8
ARTICLE_CHARS = 20_000
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)[^)]*\)")
ROBOTS_MAX_BYTES = 512_000
ROBOTS_DEADLINE_S = 15.0  # across the read and its retry (local_sources.Robots tries twice)
ROBOTS_TIMEOUT = (5, 5)  # (connect, read between bytes) seconds
ROBOTS_REDIRECTS = 3
_REDIRECTS = (301, 302, 303, 307, 308)
_resolve = socket.getaddrinfo


def article_id(url: str) -> str:
    return "article_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def plain_text(markdown: str) -> str:
    """Markdown as reading text: images out, links as their words, emphasis, heading and quote marks out, one line per
    paragraph, at most ARTICLE_CHARS."""
    text = _LINK.sub(r"\1", _IMAGE.sub("", markdown or ""))
    lines = [re.sub(r"\s+", " ", re.sub(r"^\s*[#>]+\s*|[*`]+", "", line)).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)[:ARTICLE_CHARS]


class RobotsReadRefused(Exception):
    """A robots.txt read not made or not finished: a host that is not public, too many redirects, a body past
    ROBOTS_MAX_BYTES or a read past ROBOTS_DEADLINE_S. Robots reads it as rules unknown, so the page is not fetched."""


def public_host(host: str, resolve=None) -> bool:
    """Whether every address host resolves to is a public one (not private, loopback, link-local, reserved,
    multicast or unspecified, IPv4-mapped included). A host that does not resolve is not public."""
    try:
        addresses = {ipaddress.ip_address(host.strip("[]").split("%", 1)[0])}  # a literal address is not looked up
    except ValueError:
        try:
            infos = (resolve or _resolve)(host, None)
            addresses = {ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos}
        except (OSError, ValueError, TypeError, IndexError, UnicodeError):
            return False
    for address in addresses:
        mapped = getattr(address, "ipv4_mapped", None)
        if not address.is_global or address.is_multicast or (mapped is not None and not mapped.is_global):
            return False
    return bool(addresses)


class RobotsGet:
    """get(url) -> (status, text) for local_sources.Robots, guarded as the module docstring says. The deadline starts
    at the first read of a url, so Robots' retry of the same url shares it. It is a wall clock: the read runs in a
    worker thread that is given up on, and its response closed, once the deadline passes, so a host that resolves
    slowly or a server that drips a byte at a time cannot hold the research thread past it."""

    def __init__(self, http=None, resolve=None, clock=None):
        self.http, self.resolve, self.clock = http, resolve, clock or time.monotonic
        self.started: dict = {}

    def __call__(self, url):
        deadline = self.started.setdefault(url, self.clock() + ROBOTS_DEADLINE_S)
        state: dict = {}

        def work():
            try:
                state["result"] = self._read(url, deadline, state)
            except BaseException as exc:  # handed to the caller below
                state["error"] = exc

        worker = threading.Thread(target=work, name="robots-read", daemon=True)
        worker.start()
        worker.join(max(0.0, deadline - self.clock()))
        if worker.is_alive():
            state["abandoned"] = True
            _close(state.get("response"))
            raise RobotsReadRefused("past its deadline")
        if "error" in state:
            raise state["error"]
        return state["result"]

    def _read(self, url, deadline, state):
        http = self.http
        if http is None:
            import requests

            http = requests.get
        from core.collect.local_sources import USER_AGENT

        for _ in range(ROBOTS_REDIRECTS + 1):
            if self.clock() > deadline or state.get("abandoned"):
                raise RobotsReadRefused("past its deadline")
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname or \
                    not public_host(parts.hostname, self.resolve):
                raise RobotsReadRefused("not a public host")
            response = http(url, headers={"User-Agent": USER_AGENT}, timeout=ROBOTS_TIMEOUT, stream=True,
                            allow_redirects=False)
            state["response"] = response
            try:
                if state.get("abandoned"):
                    raise RobotsReadRefused("past its deadline")
                location = (getattr(response, "headers", None) or {}).get("Location")
                if response.status_code in _REDIRECTS and location:
                    url = urljoin(url, location)
                    continue
                body = bytearray()
                for chunk in response.iter_content(chunk_size=16_384):
                    body += chunk or b""
                    if len(body) > ROBOTS_MAX_BYTES:
                        raise RobotsReadRefused("past its byte cap")
                    if self.clock() > deadline or state.get("abandoned"):
                        raise RobotsReadRefused("past its deadline")
                return response.status_code, bytes(body).decode(response.encoding or "utf-8", errors="replace")
            finally:
                _close(response)
        raise RobotsReadRefused("too many redirects")


def _close(response):
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # closing an abandoned read is best effort
            pass


def _paid_reads(ctx: RunContext) -> list[dict]:
    """This question's web/scrape calls that were made and paid for (a robots refusal is neither)."""
    return [c for c in ctx.sc_calls if c.get("route") == ARTICLE_ROUTE and c.get("status") != "robots_refused"]


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
    paid = _paid_reads(ctx)
    tried = next((c for c in reversed(paid) if (c.get("params") or {}).get("url") == url), None)
    if tried is not None:  # paid for once already and it gave no text: not paid for again
        status = tried.get("status")
        return {"evidence_id": None, "status": "empty" if status == "ok" else status, "already_tried": True,
                "credits_spent": 0.0,
                "reason": "this page was already read in this question and gave no text"}
    read = sum(1 for eid in ctx.evidence if str(eid).startswith("article_"))
    if max(read, len(paid)) >= MAX_ARTICLES:
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
    left = ctx.search_credits_left()
    if quote > left:
        raise Refused(f"{ARTICLE_ROUTE} costs {quote} credits, over the {left:g} left for live searches; the rest of "
                      "this researcher's credits are for comments and transcripts")
    result = client.call(ARTICLE_ROUTE, params, lane="agent_live", run_id=ctx.run_id,
                         max_credits=min(float(quote), left))
    from core.agent.tools.socialcrawl import _fence, read_credits

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
    # Cut first, then fence: a tag the cut splits cannot form a whole tag, and _fence neutralises every whole one.
    return {"evidence_id": aid, "status": "ok", "credits_spent": charged, "characters": len(text),
            "text": _fence(text[:4_000])}
