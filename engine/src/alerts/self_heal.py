"""Self-healing for RSS 404 / network errors.

Looks at connector failures surfaced by scripts/run_rss_now.py, tries to
discover the replacement feed URL for each broken RSS entry, and rewrites
configs/sources.yaml in place when a candidate validates. Non-404 errors
and non-RSS sources are left alone.

Discovery order for a given broken URL:
  1. Fetch the origin root (scheme + host) and look for
     <link rel="alternate" type="application/rss+xml" href="...">.
  2. Try common fallback paths in order: /rss, /feed, /rss.xml, /feed.xml,
     /index.xml, /atom.xml.
  3. First candidate whose body starts with <?xml or contains <rss or <feed
     wins.

YAML rewrite uses line-based surgical replacement (swap the URL string on
the matching line) so comments and indentation stay intact; only the URL
token is touched.

Durability caveat: the rewrite lands on the running container's working copy
of configs/sources.yaml. On Cloud Run and GitHub Actions that disk is
recreated each run from the baked image, so a heal does NOT persist to the
next run, and self_heal also runs after the RSS fetch, so it cannot help the
current run either. The feature is therefore dormant until durable healing
(a committed sources.yaml change, or GCS-read overrides applied at startup)
is built. The email labels the section "Auto-fixed (this run)" so the
messaging stays honest in the meantime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests
import yaml

from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

FALLBACK_PATHS: tuple[str, ...] = (
    "/rss",
    "/feed",
    "/rss.xml",
    "/feed.xml",
    "/index.xml",
    "/atom.xml",
)

# <link rel="alternate" type="application/rss+xml" href="..."> with attributes
# in any order and single or double quotes. Capture the href value.
_LINK_ALTERNATE_RE = re.compile(
    r"""<link\b[^>]*?
        (?:
          rel\s*=\s*["']alternate["'][^>]*?type\s*=\s*["']application/(?:rss|atom)\+xml["']
        |
          type\s*=\s*["']application/(?:rss|atom)\+xml["'][^>]*?rel\s*=\s*["']alternate["']
        )
        [^>]*?href\s*=\s*["']([^"']+)["']
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

_HREF_IN_LINK_RE = re.compile(
    r"""<link\b[^>]*?href\s*=\s*["']([^"']+)["'][^>]*?
        (?:type\s*=\s*["']application/(?:rss|atom)\+xml["']
         |rel\s*=\s*["']alternate["'][^>]*?type\s*=\s*["']application/(?:rss|atom)\+xml["'])
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

REQUEST_TIMEOUT = 8
VALIDATION_HEAD = 2048

# Same polite-bot UA as RSSConnector. SSA outlets (News24, Premium Times,
# IOL, Guardian Nigeria) bot-block the default python-requests UA, so an
# origin fetch or candidate validation sent without it 403s and the heal
# can never succeed. Kept in sync with RSSConnector.USER_AGENT in rss.py.
USER_AGENT = (
    "Mozilla/5.0 (compatible; OgilvyTrendsEngineV2/1.0; "
    "+https://github.com/jhbanalytics-pixel/trends-engine-v2)"
)


@dataclass
class HealResult:
    """Outcome of a single heal attempt.

    `persisted` tracks whether the fix reached THIS container's disk. It is
    `False` when the YAML rewrite step failed (disk full, permission denied,
    file locked). Note `persisted=True` means written to the running
    container's working copy only: on Cloud Run and GitHub Actions that disk
    is recreated each run from the baked image, so the change does NOT survive
    to the next run. Durable healing (a committed sources.yaml change or
    GCS-read overrides applied at startup) is not built yet, so the email
    labels this section "Auto-fixed (this run)" rather than implying a
    permanent fix.
    """

    original_url: str
    kind: str  # "fixed", "still_broken", "skipped"
    new_url: str | None = None
    reason: str = ""
    persisted: bool = False


def _origin_root(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def _looks_like_feed(body: bytes | str) -> bool:
    """Cheap sniff for an RSS/Atom payload."""
    if isinstance(body, bytes):
        try:
            head = body[:VALIDATION_HEAD].decode("utf-8", errors="ignore")
        except Exception:
            return False
    else:
        head = body[:VALIDATION_HEAD]
    head_stripped = head.lstrip()
    if head_stripped.startswith("<?xml"):
        # Extra check: XML with a feed-ish root tag somewhere early
        return "<rss" in head or "<feed" in head or "<rdf:RDF" in head
    return "<rss" in head or "<feed" in head


def _fetch(url: str) -> requests.Response | None:
    try:
        return requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as exc:
        logger.debug("self_heal fetch failed for %s: %s", url, exc)
        return None


def _validate_feed_url(url: str) -> bool:
    resp = _fetch(url)
    if resp is None or resp.status_code >= 400:
        return False
    return _looks_like_feed(resp.content)


def _discover_from_html(html_body: str, origin: str) -> list[str]:
    """Extract absolute feed URLs from <link alternate> tags in an HTML body."""
    found: list[str] = []
    for match in _LINK_ALTERNATE_RE.finditer(html_body):
        href = match.group(1).strip()
        if href:
            found.append(_absolutise(href, origin))
    for match in _HREF_IN_LINK_RE.finditer(html_body):
        href = match.group(1).strip()
        if href:
            abs_url = _absolutise(href, origin)
            if abs_url not in found:
                found.append(abs_url)
    return found


def _absolutise(href: str, origin: str) -> str:
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        scheme = urlparse(origin).scheme or "https"
        return f"{scheme}:{href}"
    if href.startswith("/"):
        return origin.rstrip("/") + href
    return origin.rstrip("/") + "/" + href


def _is_404_class(error_str: str) -> bool:
    """Heuristic: treat 404 / Not Found / DNS failures as candidates.

    We also heal obvious transport-level dead hosts (NameResolutionError,
    failed DNS) because a gone hostname is the same class of problem as a
    gone path.

    Deliberately NOT treated as 404-class: `max retries exceeded`, SSL
    errors, connection timeouts, and 5xx-style transient failures. Those
    often come from healthy-but-flaky feeds and healing them would waste
    up to 7 HTTP requests chasing a URL that will work again on the next
    run. If a feed is genuinely dead at the transport layer, DNS failures
    above already catch it.
    """
    s = error_str.lower()
    return (
        "404" in s
        or "not found" in s
        or "nameresolutionerror" in s
        or "name or service not known" in s
        or "failed to resolve" in s
    )


def _is_dns_class(error_str: str) -> bool:
    """True when the error is a DNS resolution failure (gone hostname)."""
    s = error_str.lower()
    return (
        "nameresolutionerror" in s or "name or service not known" in s or "failed to resolve" in s
    )


def try_heal_rss_404(error: dict) -> HealResult:
    """Attempt to discover a live replacement feed URL for one RSS error.

    The error dict must carry a `url` key (preferred) or an `error` string
    that contains the broken URL. Returns a HealResult describing the
    outcome. Never raises on network failures.
    """
    url = (error.get("url") or "").strip()
    if not url:
        # Best-effort URL extraction from the error message.
        match = re.search(r"https?://\S+", str(error.get("error") or ""))
        if match:
            url = match.group(0).rstrip(" .,;:)'\"")

    if not url:
        return HealResult(
            original_url="",
            kind="skipped",
            reason="no URL in error payload",
        )

    origin = _origin_root(url)
    if origin is None:
        return HealResult(
            original_url=url,
            kind="skipped",
            reason="could not parse origin from URL",
        )

    # A DNS-class failure means the hostname is gone. Discovery only re-probes
    # the same dead host (origin root + fallback paths all share that host), so
    # every candidate fails by construction and ~56s is wasted. Short-circuit
    # until an alternate-host discovery strategy exists.
    if _is_dns_class(str(error.get("error") or "")):
        return HealResult(
            original_url=url,
            kind="still_broken",
            reason="host unresolvable (DNS failure); skipped discovery",
        )

    tried: list[str] = []
    candidates: list[str] = []

    # Step 1: pull candidates out of the origin root's HTML.
    root_resp = _fetch(origin)
    if root_resp is not None and root_resp.status_code < 400:
        try:
            body = root_resp.text
        except Exception:
            body = ""
        if body:
            candidates.extend(_discover_from_html(body, origin))

    # Step 2: add fallback paths after any discovered links.
    for path in FALLBACK_PATHS:
        candidate = origin.rstrip("/") + path
        if candidate not in candidates:
            candidates.append(candidate)

    # Deduplicate while preserving order, skip the original broken URL.
    seen: set[str] = set()
    ordered: list[str] = []
    for c in candidates:
        if c == url or c in seen:
            continue
        seen.add(c)
        ordered.append(c)

    for candidate in ordered:
        tried.append(candidate)
        if _validate_feed_url(candidate):
            logger.info("self_heal fixed %s -> %s", url, candidate)
            return HealResult(
                original_url=url,
                kind="fixed",
                new_url=candidate,
                reason=f"validated new feed URL after {len(tried)} attempt(s)",
            )

    return HealResult(
        original_url=url,
        kind="still_broken",
        reason=f"no valid candidate after {len(tried)} attempt(s)",
    )


def _rewrite_yaml_url(yaml_text: str, old_url: str, new_url: str) -> tuple[str, bool]:
    """Swap old_url for new_url in the YAML text, touching only the URL token.

    Line-based surgical replacement across every matching line. If the same
    broken URL appears in multiple markets (e.g. an aggregator feed reused
    across regions), all occurrences are rewritten in one pass. Preserves
    comments, indentation, and surrounding keys (`name:`, etc.). Returns
    (new_text, any_replaced).
    """
    if old_url == new_url or not old_url:
        return yaml_text, False
    # Match the URL as an exact token on a `url:` line, bounded by whitespace,
    # a comment hash, or end-of-string. A plain substring replace would corrupt
    # a different feed whose URL contains the broken one as a prefix
    # (e.g. .../feed vs .../feed/extra.xml).
    token_re = re.compile(
        r"(url:\s*)" + re.escape(old_url) + r"(?=\s|#|$)",
    )
    any_replaced = False
    new_lines: list[str] = []
    for line in yaml_text.splitlines(keepends=True):
        new_line, count = token_re.subn(r"\g<1>" + new_url.replace("\\", "\\\\"), line)
        if count:
            any_replaced = True
        new_lines.append(new_line)
    return "".join(new_lines), any_replaced


def heal_errors(
    errors: list[dict],
    sources_yaml_path: Path,
) -> tuple[list[HealResult], list[dict]]:
    """Run heal attempts over a list of pipeline errors.

    Filters to RSS 404-class failures, runs discovery on each, and rewrites
    configs/sources.yaml in place for every successful fix. Comments and
    layout are preserved via line-based replacement.

    Returns (heal_results, remaining_errors) where remaining_errors is the
    subset of input errors for which healing did not land.
    """
    heal_results: list[HealResult] = []
    remaining: list[dict] = []

    candidates: list[dict] = []
    for err in errors:
        if not isinstance(err, dict):
            remaining.append(err)
            continue
        if err.get("source") != "rss":
            remaining.append(err)
            continue
        error_str = str(err.get("error") or "")
        if not _is_404_class(error_str):
            remaining.append(err)
            continue
        candidates.append(err)

    if not candidates:
        return heal_results, remaining

    # Load YAML once so we can resolve URLs from per-market feed lists when
    # the error payload doesn't carry the URL directly.
    try:
        sources_cfg = yaml.safe_load(sources_yaml_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("self_heal could not read %s: %s", sources_yaml_path, exc)
        sources_cfg = {}

    rss_feeds = sources_cfg.get("rss_feeds", {}) or {}

    # Track which input error produced each fixed result, so a fix that later
    # fails to persist can be re-flagged into remaining_errors.
    fixed_origin: list[tuple[HealResult, dict]] = []
    for err in candidates:
        url = (err.get("url") or "").strip()
        if not url:
            # Try to pull from the error string, then fall back to matching
            # the market's feed list when the error text names the feed.
            match = re.search(r"https?://\S+", str(err.get("error") or ""))
            if match:
                url = match.group(0).rstrip(" .,;:)'\"")
            if not url:
                market = (err.get("market") or "").lower()
                error_text = str(err.get("error") or "")
                for feed in rss_feeds.get(market, []) or []:
                    name = str(feed.get("name") or "")
                    if name and name in error_text:
                        url = str(feed.get("url") or "")
                        break

        payload = {**err, "url": url} if url else err
        result = try_heal_rss_404(payload)
        heal_results.append(result)
        if result.kind != "fixed":
            remaining.append(err)
        else:
            fixed_origin.append((result, err))

    # Rewrite sources.yaml for every fix. One pass so we write the file at
    # most once even when multiple feeds get fixed in a single run. Mark
    # each HealResult's persisted flag so the email digest only advertises
    # fixes that actually reached disk.
    fixes = [r for r in heal_results if r.kind == "fixed" and r.new_url]
    if fixes:
        try:
            yaml_text = sources_yaml_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("self_heal could not open %s: %s", sources_yaml_path, exc)
            return heal_results, remaining

        applied_fixes: list[HealResult] = []
        for fix in fixes:
            yaml_text, replaced = _rewrite_yaml_url(yaml_text, fix.original_url, fix.new_url or "")
            if replaced:
                logger.info(
                    "self_heal rewrote %s: %s -> %s",
                    sources_yaml_path,
                    fix.original_url,
                    fix.new_url,
                )
                applied_fixes.append(fix)
            else:
                logger.warning(
                    "self_heal could not locate %s in %s (config drift?)",
                    fix.original_url,
                    sources_yaml_path,
                )

        if applied_fixes:
            try:
                sources_yaml_path.write_text(yaml_text, encoding="utf-8")
                for fix in applied_fixes:
                    fix.persisted = True
            except OSError as exc:
                logger.error("self_heal write failed for %s: %s", sources_yaml_path, exc)
                # applied_fixes all stay persisted=False so the caller can
                # filter them out of the "auto-resolved" email section.

    # A fixed result that did not reach disk (write failed, or the URL could
    # not be located in the YAML) would otherwise vanish from both digest
    # sections: it is not in remaining (kind=="fixed") and the digest hides it
    # because persisted is False. Re-flag the originating error so the broken
    # feed still surfaces under Still-broken.
    for result, err in fixed_origin:
        if not result.persisted and err not in remaining:
            remaining.append(err)

    return heal_results, remaining


__all__ = [
    "FALLBACK_PATHS",
    "HealResult",
    "heal_errors",
    "try_heal_rss_404",
]
