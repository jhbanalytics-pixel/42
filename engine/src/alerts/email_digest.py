"""Email digest alerting for trend-tier transitions.

Fires one email per pipeline run covering every (market, query_group) that
upgraded tier vs its most recent prior score. HTML body with a section per
market (sorted by count of upgrades, then by market code), and a plain-text
fallback for clients that block HTML. Sent via Gmail SMTP using an app
password so no OAuth state needs to be persisted.

Controlled by env vars:
  GMAIL_USER             -- Gmail sender account (e.g. jhb.analytics@gmail.com)
  GMAIL_APP_PASSWORD     -- 16-char Google app password (strip spaces)
  EMAIL_RECIPIENTS       -- comma-separated recipient list
  EMAIL_ALERTS_ENABLED   -- 'true' enables delivery; anything else is a dry run
"""

from __future__ import annotations

import html
import os
import secrets
import smtplib
import ssl
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

TIER_ORDER = {"Below threshold": 0, "Monitoring": 1, "Emerging": 2, "Trending": 3}

# PULSE v2 mailer (the editorial Ogilvy x Google skin). Default OFF: when
# unset the digest renders the v1 body byte-for-byte. Flip to "true" via the
# MAILER_V2_ENABLED env var / GitHub Actions secret once the v2 layout is
# signed off. Read with the same idiom as EMAIL_ALERTS_ENABLED.
MAILER_V2_FLAG = "MAILER_V2_ENABLED"
# Inbox-summary "open the full read" target. Placeholder "#" until a hosted
# URL exists; override via the MAILER_FULL_URL env var, or let the GCS archive
# (below) supply it per-send. The v2 inbox summary links here.
MAILER_FULL_URL = os.environ.get("MAILER_FULL_URL", "#")
# GCS bucket for the hosted full-read archive. When set, each PULSE send
# uploads the rendered HTML to gs://<bucket>/pulse/<date>.html and uses its
# public URL as the full-read link. Unset (the default) means no upload and
# the link falls back to MAILER_FULL_URL. All-Google stack, ~$0. The bucket
# and its public-read access are provisioned out of band (not by this code).
MAILER_ARCHIVE_BUCKET = os.environ.get("MAILER_ARCHIVE_BUCKET", "").strip()

# Inline-fallback byte ceiling. When no hosted full-read URL exists (bucket unset
# or the per-send upload returned ""), the full rich PULSE body ships inline. On
# a 24-brief day that body is ~90KB, close to Gmail's 102,400-byte decoded clip.
# Past the clip Gmail hides the tail behind a "[Message clipped] View entire
# message" link, which buries the footer and the board long tail. Trim the inline
# body (drop board long-tail briefs from the bottom up) until it clears this
# ceiling, leaving headroom under the hard 102,400 clip for the MIME/base64
# envelope and any client-side rewriting.
INLINE_BODY_CEILING_BYTES = 95_000


def _safe_href_url(url: str) -> str:
    """Return url only when it is a non-empty https link for v1 digest anchors."""
    u = (url or "").strip()
    if not u or u == "#":
        return ""
    if not u.lower().startswith("https://"):
        return ""
    return u


def _safe_full_read_url(url: str) -> str:
    """Return url only when it is a non-empty https target for the teaser CTA."""
    u = (url or "").strip()
    if not u or u == "#":
        return ""
    if not u.lower().startswith("https://"):
        logger.warning("Rejecting non-https full-read URL for PULSE teaser")
        return ""
    return u


# Seven days is generous for a daily read and short enough that a forwarded
# link does not outlive the read it was sent for. google-cloud-storage refuses
# anything above seven days for a V4 signature, so this is also the ceiling.
ARCHIVE_URL_TTL = timedelta(days=7)


SIGNER_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


def _archive_signer_credentials() -> Any:
    """Return credentials carrying the fields the IAM signBlob path needs.

    On Cloud Run the ADC principal is ``compute_engine.Credentials``: a bearer
    token and no private key. ``generate_signed_url`` cannot sign from that on
    its own and raises "you need a private key to sign credentials". Handing it
    ``service_account_email`` plus ``access_token`` switches it to the signBlob
    API, which is why the runtime service account needs
    ``iam.serviceAccounts.signBlob`` on itself.

    The token has to be scoped for cloud-platform. It is tempting to reuse
    ``storage.Client._credentials``, but google-cloud-storage builds those with
    ``Client.SCOPE``, the three devstorage scopes and nothing else, and
    iamcredentials rejects a devstorage token with
    "403 ACCESS_TOKEN_SCOPE_INSUFFICIENT". So the signer asks ADC for its own
    cloud-platform credential rather than borrowing the upload one.

    Two fields have to be resolved before they are read, and both are unset on
    a freshly built credential. ``token`` is None until it talks to the
    metadata server, and ``service_account_email`` reads the literal string
    ``"default"``, which would make signBlob target a service account of that
    name and fail. A refresh populates both.
    """
    import google.auth

    creds, _ = google.auth.default(scopes=SIGNER_SCOPES)
    unresolved = getattr(creds, "service_account_email", None) in (None, "", "default")
    if not getattr(creds, "token", None) or unresolved:
        from google.auth.transport.requests import Request as _Request

        creds.refresh(_Request())
    return creds


def archive_html_to_gcs(html: str, run_date: date, _client: Any = None) -> str:
    """Upload the rendered digest HTML to the archive bucket, return a signed URL.

    Writes ``pulse/<YYYY-MM-DD>-<token>.html`` to ``MAILER_ARCHIVE_BUCKET`` and
    returns a V4 signed GET url valid for ``ARCHIVE_URL_TTL``. Returns ``""``
    when the bucket is unset, the upload fails, or the signature cannot be
    produced (never raises, so a hosting blip cannot block the send).

    The bucket is PRIVATE and the signature is the only access control. An
    earlier version relied on an unguessable object name against a bucket that
    granted roles/storage.objectViewer to allUsers. That role carries
    storage.objects.list, so an anonymous list of the bucket returned every
    object and the secret path protected nothing. A failure to sign therefore
    degrades to no hosted link at all, never to a plain object URL, which would
    only work while the bucket stayed public.

    Signing under Application Default Credentials has no local private key, so
    the runtime service account needs iam.serviceAccounts.signBlob on itself.

    ``_client`` is an injectable storage client so tests exercise the upload
    path without the SDK or network.
    """
    if not MAILER_ARCHIVE_BUCKET:
        return ""
    try:
        client = _client
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        # The token still keeps two same-day sends from overwriting each other
        # and keeps the object name out of a predictable sequence. It is no
        # longer load-bearing for access; the signature is.
        token = secrets.token_urlsafe(12)
        blob_name = f"pulse/{run_date.isoformat()}-{token}.html"
        blob = client.bucket(MAILER_ARCHIVE_BUCKET).blob(blob_name)
        # private, not public: a shared intermediary cache must not be able to
        # hand this to a client that carries no signature.
        blob.cache_control = "private, max-age=86400"
        blob.upload_from_string(html, content_type="text/html; charset=utf-8")
        signer = _archive_signer_credentials()
        url = blob.generate_signed_url(
            version="v4",
            expiration=ARCHIVE_URL_TTL,
            method="GET",
            service_account_email=signer.service_account_email,
            access_token=signer.token,
        )
        logger.info(
            "PULSE archived bucket=%s date=%s object_prefix=%s ttl_days=%s",
            MAILER_ARCHIVE_BUCKET,
            run_date.isoformat(),
            f"pulse/{run_date.isoformat()}-{token[:8]}",
            ARCHIVE_URL_TTL.days,
        )
        return url
    except Exception as exc:
        logger.warning("PULSE GCS archive failed (non-fatal): %s", exc)
        return ""


def _mailer_v2_enabled() -> bool:
    """True when the PULSE v2 mailer is switched on via env. Default False."""
    return os.environ.get(MAILER_V2_FLAG, "").strip().lower() == "true"


SIGNAL_PHRASES: dict[str, tuple[str, str]] = {
    # key -> (high-value phrase for value >= 0.6, moderate phrase for 0.2 <= value < 0.6)
    "velocity": (
        "Spiking fast: mentions jumping vs recent baseline",
        "Picking up pace vs recent days",
    ),
    "diversity": (
        "Broad coverage: picked up by many independent outlets",
        "Spread across multiple outlets, not a single source",
    ),
    "engagement": (
        "High engagement: posts getting strong interaction",
        "Engagement above baseline",
    ),
    "creator_spread": (
        "Many creators posting about it, not concentrated",
        "Creators starting to pick up on it",
    ),
    "regional": (
        "Hitting multiple regions at once",
        "Showing cross-regional spread",
    ),
    "watchlist": (
        "Flagged topic on priority watchlist",
        "On our watchlist",
    ),
    "search_velocity": (
        "Search interest climbing fast",
        "Search interest ticking up",
    ),
    "slang": (
        "New terms and slang emerging around this",
        "Some language drift showing up",
    ),
}


@dataclass
class TrendAlert:
    """All the data needed to render one tier-upgrade entry in a digest."""

    market: str
    query_group: str
    trend_score: float
    tier: str
    prior_tier: str | None
    item_count: int
    source_diversity: int
    signals: dict[str, tuple[float, float, float]]
    samples: list[dict[str, str]]
    top_persons: list[str]
    top_orgs: list[str]
    tone_avg: float | None


@dataclass
class TrendMover:
    """Trend that shifted materially vs its prior score without crossing a tier.

    Surfaces early signal, stuff building before it triggers a tier-upgrade
    alert. Carries the same context bundle as TrendAlert (headline sample,
    top persons, top orgs, tone) so the reader can act without opening
    dashboards.
    """

    market: str
    query_group: str
    current_score: float
    prior_score: float
    delta: float
    tier: str
    prior_tier: str | None
    item_count: int
    source_diversity: int
    samples: list[dict[str, str]]
    top_persons: list[str]
    top_orgs: list[str]
    tone_avg: float | None


@dataclass
class IssueFlag:
    """One line of trouble from the pipeline run surfaced in the digest."""

    label: str
    detail: str


@dataclass
class HealedIssue:
    """An RSS feed that broke and was auto-fixed in the same run.

    Surfaced above the still-broken list so the reader sees what the
    pipeline fixed without intervention versus what actually needs hands
    on a keyboard.
    """

    label: str
    original_url: str
    new_url: str
    reason: str = ""


def score_to_tier(trend_score: float, thresholds: dict[str, float]) -> str:
    """Map a composite score to its tier label using scoring.yaml thresholds.

    Default fallbacks align with the 28 May 2026 recalibration in
    configs/scoring.yaml (Trending 0.45 / Emerging 0.30 / Monitoring
    0.18). Callers should pass loaded thresholds; the defaults exist
    only to keep the function safe when invoked from tests or one-off
    scripts that forget to load scoring.yaml.
    """
    if trend_score >= thresholds.get("trending", 0.45):
        return "Trending"
    if trend_score >= thresholds.get("emerging", 0.30):
        return "Emerging"
    if trend_score >= thresholds.get("monitoring", 0.18):
        return "Monitoring"
    return "Below threshold"


def tier_upgraded(today_tier: str, prior_tier: str | None) -> bool:
    """True when today's tier is strictly higher than the prior tier.

    First-time scoring (prior_tier is None) fires only if today lands at
    Emerging or above, to avoid flooding on the first week of fresh data.
    """
    today_rank = TIER_ORDER.get(today_tier, 0)
    if prior_tier is None:
        return today_rank >= TIER_ORDER["Emerging"]
    return today_rank > TIER_ORDER.get(prior_tier, 0)


def build_alert(
    today_row: dict[str, Any],
    prior_row: dict[str, Any] | None,
    weights: dict[str, float],
    thresholds: dict[str, float],
    enriched_samples: list[dict[str, Any]] | None = None,
    top_persons: list[str] | None = None,
    top_orgs: list[str] | None = None,
) -> TrendAlert:
    """Assemble a TrendAlert from a trend_scores row + optional enriched context."""

    def _get(row: dict, key: str, default: float = 0.0) -> float:
        val = row.get(key)
        if val is None:
            return default
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    signal_defs: list[tuple[str, str, str]] = [
        ("velocity", "velocity_score", "velocity"),
        ("diversity", "diversity_score", "diversity"),
        ("engagement", "engagement_score", "engagement"),
        ("creator_spread", "creator_score", "creator_spread"),
        ("regional", "regional_score", "regional_score"),
        ("watchlist", "watchlist_score", "watchlist_score"),
        ("search_velocity", "search_velocity_score", "search_velocity_score"),
        ("slang", "slang_score", "slang_score"),
        ("tone", "tone_score", "tone_score"),
    ]
    signals: dict[str, tuple[float, float, float]] = {}
    for display_name, row_key, weight_key in signal_defs:
        value = _get(today_row, row_key)
        weight = float(weights.get(weight_key, 0.0))
        contribution = round(value * weight, 4)
        signals[display_name] = (value, weight, contribution)

    tone_avg = today_row.get("tone_score") if (today_row.get("tone_rows") or 0) > 0 else None

    trend_score = _get(today_row, "trend_score")
    tier = score_to_tier(trend_score, thresholds)
    prior_tier = None
    if prior_row is not None:
        prior_score = _get(prior_row, "trend_score")
        prior_tier = score_to_tier(prior_score, thresholds)

    return TrendAlert(
        market=str(today_row.get("market", "")),
        query_group=str(today_row.get("query_group", "")),
        trend_score=trend_score,
        tier=tier,
        prior_tier=prior_tier,
        item_count=int(today_row.get("item_count") or 0),
        source_diversity=int(today_row.get("source_diversity") or 0),
        signals=signals,
        samples=list(enriched_samples or []),
        top_persons=list(top_persons or []),
        top_orgs=list(top_orgs or []),
        tone_avg=float(tone_avg) if tone_avg is not None else None,
    )


def _reason_phrases(alert: TrendAlert) -> list[str]:
    """Plain-English reasons for why a trend was flagged."""
    ranked = sorted(alert.signals.items(), key=lambda kv: kv[1][2], reverse=True)
    out: list[str] = []
    for name, (value, _w, contribution) in ranked:
        if len(out) >= 3:
            break
        if contribution <= 0 or value < 0.2:
            continue
        phrases = SIGNAL_PHRASES.get(name)
        if not phrases:
            continue
        out.append(phrases[0] if value >= 0.6 else phrases[1])

    if alert.tone_avg is not None:
        tone = alert.tone_avg
        if tone < 0.35:
            out.append("Strong negative tone: coverage is hostile or worried")
        elif tone < 0.45:
            out.append("Negative tone: people concerned, not just discussing")
        elif tone > 0.65:
            out.append("Strong positive buzz in coverage")
        elif tone > 0.55:
            out.append("Positive tone overall")

    return out


def _tier_badge_colour(tier: str) -> str:
    return {
        "Trending": "#c0392b",
        "Emerging": "#d68910",
        "Monitoring": "#2874a6",
    }.get(tier, "#555555")


def _render_item_html(alert: TrendAlert) -> str:
    prior = alert.prior_tier or "First detection"
    tier_colour = _tier_badge_colour(alert.tier)
    badge = (
        f'<span style="background:{tier_colour};color:#fff;padding:2px 8px;'
        f'border-radius:10px;font-size:12px;font-weight:bold;">{html.escape(alert.tier)}</span>'
    )

    parts: list[str] = [
        '<div style="border-left:4px solid ' + tier_colour + ";padding:12px 16px;"
        'margin:12px 0;background:#fafafa;">',
        f'<div style="font-size:16px;font-weight:bold;margin-bottom:4px;">'
        f"{html.escape(alert.query_group)} {badge} "
        f'<span style="color:#666;font-size:13px;">score {alert.trend_score:.3f}</span></div>',
        f'<div style="color:#666;font-size:13px;margin-bottom:10px;">'
        f"{html.escape(prior)} to {html.escape(alert.tier)}</div>",
    ]

    top = alert.samples[0] if alert.samples else None
    if top:
        headline = (top.get("title") or top.get("text") or "").strip()
        source = (top.get("source") or "").strip()
        if headline:
            source_line = (
                f'<div style="font-size:12px;color:#888;margin-top:4px;">via '
                f"{html.escape(source)}</div>"
                if source
                else ""
            )
            parts.append(
                '<blockquote style="margin:8px 0;padding:8px 12px;border-left:3px solid #ddd;'
                'font-style:italic;color:#222;">'
                f"&ldquo;{html.escape(headline[:200])}&rdquo;{source_line}</blockquote>"
            )

    parts.append(
        f'<div style="font-size:13px;color:#444;margin-bottom:8px;">'
        f"{alert.item_count} mentions across {alert.source_diversity} different sources."
        f"</div>"
    )

    reasons = _reason_phrases(alert)
    if reasons:
        items = "".join(f"<li>{html.escape(r)}</li>" for r in reasons)
        parts.append(
            '<div style="font-size:13px;margin-bottom:8px;">'
            "<strong>Why we flagged it:</strong>"
            f'<ul style="margin:4px 0 0 18px;padding:0;">{items}</ul></div>'
        )

    if alert.top_persons or alert.top_orgs:
        players: list[str] = []
        if alert.top_persons:
            players.append(
                f"<strong>People:</strong> {html.escape(', '.join(alert.top_persons[:5]))}"
            )
        if alert.top_orgs:
            players.append(f"<strong>Orgs:</strong> {html.escape(', '.join(alert.top_orgs[:5]))}")
        parts.append(
            '<div style="font-size:13px;color:#333;margin-bottom:8px;">'
            + "<br>".join(players)
            + "</div>"
        )

    linked = [s for s in alert.samples[:3] if s.get("url")]
    if linked:
        links: list[str] = []
        for s in linked:
            title = (s.get("title") or s.get("text") or s.get("url", "")).strip()[:100]
            url = _safe_href_url(s.get("url") or "")
            if not url:
                continue
            links.append(
                f'<li><a href="{html.escape(url)}" style="color:#1a5fb4;">'
                f"{html.escape(title)}</a></li>"
            )
        parts.append(
            '<div style="font-size:13px;"><strong>Read more:</strong>'
            f'<ul style="margin:4px 0 0 18px;padding:0;">{"".join(links)}</ul></div>'
        )

    parts.append("</div>")
    return "".join(parts)


def _render_item_text(alert: TrendAlert, index: int) -> list[str]:
    prior = alert.prior_tier or "First detection"
    header = (
        f"{index}. {alert.query_group} ({prior} -> {alert.tier}, score {alert.trend_score:.3f})"
    )
    lines: list[str] = [header]

    top = alert.samples[0] if alert.samples else None
    if top:
        headline = (top.get("title") or top.get("text") or "").strip()
        source = (top.get("source") or "").strip()
        if headline:
            lines.append("")
            lines.append(f'"{headline[:200]}"')
            if source:
                lines.append(f"via {source}")

    lines.append(f"{alert.item_count} mentions across {alert.source_diversity} sources.")

    reasons = _reason_phrases(alert)
    if reasons:
        lines.append("Why we flagged it:")
        for r in reasons:
            lines.append(f"  - {r}")

    if alert.top_persons:
        lines.append(f"People: {', '.join(alert.top_persons[:5])}")
    if alert.top_orgs:
        lines.append(f"Orgs: {', '.join(alert.top_orgs[:5])}")

    linked = [s for s in alert.samples[:3] if s.get("url")]
    if linked:
        lines.append("Read more:")
        for s in linked:
            title = (s.get("title") or s.get("text") or "").strip()[:100]
            url = s.get("url") or ""
            lines.append(f"  -> {title}")
            lines.append(f"     {url}")

    return lines


def _delta_label(delta: float) -> str:
    sign = "+" if delta > 0 else ""
    return f"{sign}{delta:.3f}"


def _delta_colour(delta: float) -> str:
    if delta >= 0.03:
        return "#1e8449"
    if delta >= 0.01:
        return "#2874a6"
    if delta <= -0.03:
        return "#c0392b"
    if delta <= -0.01:
        return "#af601a"
    return "#555555"


def _render_mover_item_html(mover: TrendMover) -> str:
    delta_colour = _delta_colour(mover.delta)
    tier_colour = _tier_badge_colour(mover.tier)
    tier_badge = (
        f'<span style="background:{tier_colour};color:#fff;padding:2px 7px;'
        f'border-radius:10px;font-size:11px;font-weight:bold;">{html.escape(mover.tier)}</span>'
    )

    parts: list[str] = [
        '<div style="border-left:3px solid ' + delta_colour + ";padding:10px 14px;"
        'margin:10px 0;background:#fafafa;">',
        '<div style="font-size:15px;font-weight:bold;margin-bottom:4px;">'
        f"{html.escape(mover.query_group)} {tier_badge} "
        f'<span style="color:{delta_colour};font-size:13px;">'
        f"{_delta_label(mover.delta)}</span> "
        f'<span style="color:#666;font-size:12px;">'
        f"{mover.prior_score:.3f} &rarr; {mover.current_score:.3f}"
        f"</span></div>",
    ]

    top = mover.samples[0] if mover.samples else None
    if top:
        headline = (top.get("title") or top.get("text") or "").strip()
        source = (top.get("source") or "").strip()
        if headline:
            source_line = (
                f'<div style="font-size:12px;color:#888;margin-top:4px;">via '
                f"{html.escape(source)}</div>"
                if source
                else ""
            )
            parts.append(
                '<blockquote style="margin:6px 0;padding:6px 10px;border-left:3px solid #ddd;'
                'font-style:italic;color:#222;font-size:13px;">'
                f"&ldquo;{html.escape(headline[:180])}&rdquo;{source_line}</blockquote>"
            )

    stats_line = f"{mover.item_count} mentions across {mover.source_diversity} sources"
    if mover.tone_avg is not None:
        tone = mover.tone_avg
        if tone < 0.4:
            stats_line += ", negative tone"
        elif tone > 0.6:
            stats_line += ", positive tone"
    parts.append(f'<div style="font-size:12px;color:#555;margin-bottom:6px;">{stats_line}.</div>')

    if mover.top_persons or mover.top_orgs:
        pieces: list[str] = []
        if mover.top_persons:
            pieces.append(
                f"<strong>People:</strong> {html.escape(', '.join(mover.top_persons[:4]))}"
            )
        if mover.top_orgs:
            pieces.append(f"<strong>Orgs:</strong> {html.escape(', '.join(mover.top_orgs[:4]))}")
        parts.append(
            '<div style="font-size:12px;color:#333;margin-bottom:6px;">'
            + "<br>".join(pieces)
            + "</div>"
        )

    linked = [s for s in mover.samples[:2] if s.get("url")]
    if linked:
        links: list[str] = []
        for s in linked:
            title = (s.get("title") or s.get("text") or s.get("url", "")).strip()[:90]
            url = _safe_href_url(s.get("url") or "")
            if not url:
                continue
            links.append(
                f'<li><a href="{html.escape(url)}" style="color:#1a5fb4;">'
                f"{html.escape(title)}</a></li>"
            )
        parts.append(
            '<div style="font-size:12px;"><ul style="margin:4px 0 0 18px;padding:0;">'
            f"{''.join(links)}</ul></div>"
        )

    parts.append("</div>")
    return "".join(parts)


def _render_movers_html(movers_by_market: dict[str, list[TrendMover]]) -> str:
    parts: list[str] = [
        '<h3 style="border-bottom:2px solid #333;padding-bottom:6px;margin-top:24px;">'
        "What's moving</h3>",
        '<div style="color:#666;font-size:13px;margin:0 0 8px 0;">'
        "Biggest score shifts vs yesterday. Worth watching before they trip a tier."
        "</div>",
    ]
    for market in sorted(movers_by_market.keys()):
        movers = movers_by_market[market]
        if not movers:
            continue
        parts.append(f'<h4 style="margin:16px 0 6px 0;">{market.upper()}</h4>')
        for mover in movers:
            parts.append(_render_mover_item_html(mover))
    return "".join(parts)


def _render_mover_item_text(mover: TrendMover, index: int) -> list[str]:
    lines = [
        f"{index}. {mover.query_group} ({mover.tier}, "
        f"{_delta_label(mover.delta)} "
        f"{mover.prior_score:.3f} -> {mover.current_score:.3f})"
    ]
    top = mover.samples[0] if mover.samples else None
    if top:
        headline = (top.get("title") or top.get("text") or "").strip()
        source = (top.get("source") or "").strip()
        if headline:
            lines.append(f'  "{headline[:180]}"')
            if source:
                lines.append(f"   via {source}")

    stats_line = f"  {mover.item_count} mentions / {mover.source_diversity} sources"
    if mover.tone_avg is not None:
        tone = mover.tone_avg
        if tone < 0.4:
            stats_line += ", negative tone"
        elif tone > 0.6:
            stats_line += ", positive tone"
    lines.append(stats_line)

    if mover.top_persons:
        lines.append(f"  People: {', '.join(mover.top_persons[:4])}")
    if mover.top_orgs:
        lines.append(f"  Orgs: {', '.join(mover.top_orgs[:4])}")

    linked = [s for s in mover.samples[:2] if s.get("url")]
    if linked:
        for s in linked:
            lines.append(f"  -> {s.get('url')}")
    return lines


def _render_movers_text(movers_by_market: dict[str, list[TrendMover]]) -> list[str]:
    lines: list[str] = ["", "What's moving", "=" * 40]
    lines.append("Biggest score shifts vs yesterday.")
    for market in sorted(movers_by_market.keys()):
        movers = movers_by_market[market]
        if not movers:
            continue
        lines.append("")
        lines.append(f"{market.upper()}")
        for i, mover in enumerate(movers, 1):
            lines.append("")
            lines.extend(_render_mover_item_text(mover, i))
    return lines


def _render_issues_html(issues: list[IssueFlag], heading: str = "Things that broke") -> str:
    parts = [
        f'<h3 style="border-bottom:2px solid #c0392b;padding-bottom:6px;margin-top:24px;'
        f'color:#c0392b;">{html.escape(heading)}</h3>',
        '<ul style="margin:0;padding:0 0 0 18px;font-size:13px;color:#444;">',
    ]
    for issue in issues:
        parts.append(
            f"<li><strong>{html.escape(issue.label)}:</strong> {html.escape(issue.detail)}</li>"
        )
    parts.append("</ul>")
    return "".join(parts)


def _render_issues_text(issues: list[IssueFlag], heading: str = "Things that broke") -> list[str]:
    lines = ["", heading, "=" * 40]
    for issue in issues:
        lines.append(f"- {issue.label}: {issue.detail}")
    return lines


def _render_healed_html(healed: list[HealedIssue]) -> str:
    parts = [
        '<h3 style="border-bottom:2px solid #1e8449;padding-bottom:6px;margin-top:24px;'
        'color:#1e8449;">Auto-fixed (this run)</h3>',
        '<div style="color:#666;font-size:13px;margin:0 0 8px 0;">'
        "Feeds that 404'd during this run and were repointed in this run's working "
        "config copy. The change is not persisted across runs yet, so a recurring "
        "404 may reappear until the feed URL is committed."
        "</div>",
        '<ul style="margin:0;padding:0 0 0 18px;font-size:13px;color:#444;">',
    ]
    for item in healed:
        reason_suffix = f", {html.escape(item.reason)}" if item.reason else ""
        safe_new = _safe_href_url(item.new_url)
        if safe_new:
            new_link = (
                f'&rarr; <a href="{html.escape(safe_new)}" style="color:#1a5fb4;">'
                f"{html.escape(item.new_url)}</a>"
            )
        else:
            new_link = f"&rarr; {html.escape(item.new_url)}"
        parts.append(
            f"<li><strong>{html.escape(item.label)}:</strong> "
            f'<span style="color:#888;text-decoration:line-through;">'
            f"{html.escape(item.original_url)}</span> "
            f"{new_link}{reason_suffix}</li>"
        )
    parts.append("</ul>")
    return "".join(parts)


def _render_healed_text(healed: list[HealedIssue]) -> list[str]:
    lines = ["", "Auto-fixed (this run)", "=" * 40]
    lines.append(
        "Feeds that 404'd during this run and were repointed in this run's "
        "working config copy. Not persisted across runs yet, so a recurring "
        "404 may reappear until the feed URL is committed."
    )
    for item in healed:
        suffix = f" ({item.reason})" if item.reason else ""
        lines.append(f"- {item.label}: {item.original_url} -> {item.new_url}{suffix}")
    return lines


_STATS_LABELS: list[tuple[str, str]] = [
    ("raw_rows", "raw_content rows"),
    ("enriched_rows", "enriched_content rows"),
    ("scored_rows", "trend_scores rows"),
    ("elapsed_seconds", "elapsed (seconds)"),
    ("errors_count", "connector errors"),
    ("unclassified_rows", "unclassified rows"),
    ("briefs_skipped", "briefs skipped (empty after retry)"),
]


def _render_stats_html(stats: dict[str, Any]) -> str:
    parts: list[str] = [
        '<h4 style="margin:24px 0 6px 0;">Run stats</h4>',
        '<ul style="margin:0;padding:0 0 0 18px;font-size:13px;color:#555;">',
    ]
    for key, label in _STATS_LABELS:
        val = stats.get(key)
        if val is not None:
            parts.append(f"<li>{html.escape(label)}: {val}</li>")
    parts.append("</ul>")
    return "".join(parts)


def _render_stats_text(stats: dict[str, Any]) -> list[str]:
    lines = ["", "Run stats", "=" * 40]
    for key, label in _STATS_LABELS:
        val = stats.get(key)
        if val is not None:
            lines.append(f"  {label}: {val}")
    return lines


def _status_badge_colour(status_tag: str) -> str:
    """Pick a tag colour for the Phase 2 brief status (Key vs Rising)."""
    return "#c0392b" if status_tag == "Key" else "#2874a6"


# Country and status visual constants. Centralised so the brief block
# and the movers block render with identical iconography. Aligned with
# Jo's PDF mock: country flag emoji, status emoji, sentiment dot.
COUNTRY_FLAGS: dict[str, str] = {
    "za": "\U0001f1ff\U0001f1e6",  # 🇿🇦
    "ng": "\U0001f1f3\U0001f1ec",  # 🇳🇬
    "ke": "\U0001f1f0\U0001f1ea",  # 🇰🇪
}
COUNTRY_NAMES: dict[str, str] = {
    "za": "South Africa",
    "ng": "Nigeria",
    "ke": "Kenya",
}
STATUS_EMOJI: dict[str, str] = {
    "Key": "\U0001f525",  # 🔥
    "Rising": "⬆️",  # ⬆️
}


def _sentiment_dot(sentiment_summary: str) -> str:
    """Return a coloured circle emoji reflecting sentiment polarity.

    Heuristic: scan the sentiment_summary string for explicit polarity
    words. Falls back to neutral when no signal is present. Mirrors the
    🟢/🟡/🔴 pattern in Jo's PDF mock.

    Tone=0.0 short-circuit: when the sentiment_summary references a
    "tone score of 0.00" (or 0.0), the underlying tone signal is
    absent (no GDELT contribution), NOT maximally negative. Earlier
    versions matched the literal word "negative" inside phrases like
    "tone score of 0.00 indicates very negative sentiment", inflating
    the polarity dot to 🔴 on cards with no real negative signal
    (Thapelo flagged 7 May 2026: same 0.00 tone score got 4
    different polarity reads across cards). Treat any
    "0.00"/"0.0"/"of 0" tone reference as 🟡 unknown.
    """
    text = (sentiment_summary or "").lower()
    # Tone=0 in the text means "no GDELT signal", short-circuit to
    # neutral / unknown regardless of any 'negative' keyword sitting
    # downstream of the score reference.
    zero_tone_markers = (
        "tone score of 0.00",
        "tone score of 0.0",
        "average tone score is 0.00",
        "average tone score is 0.0",
        "average tone score of 0.00",
        "average tone score of 0.0",
        "tone signal not available",
    )
    if any(marker in text for marker in zero_tone_markers):
        return "\U0001f7e1"  # 🟡 (no signal)
    if "negative" in text or "discontent" in text or "outrage" in text:
        return "\U0001f534"  # 🔴
    if "positive" in text and "negative" not in text:
        return "\U0001f7e2"  # 🟢
    return "\U0001f7e1"  # 🟡 (neutral / mixed / default)


def _normalize_sentiment_text(sentiment_summary: str) -> str:
    """Rewrite sentiment_summary lines that contradict the tone signal.

    Earlier brief generations included Gemini outputs like 'The average
    tone score of 0.00 indicates a predominantly very negative sentiment',
    which the email card then renders as a confusing label-vs-truth
    mismatch (tone=0.0 means no signal, not max negative). This helper
    detects the tone=0 + 'negative' contradiction in the existing
    sentiment_summary string and substitutes a calibrated equivalent.

    Idempotent. Safe to call on any sentiment_summary; non-matching
    strings pass through unchanged.
    """
    if not sentiment_summary:
        return sentiment_summary
    lower = sentiment_summary.lower()
    has_zero_tone = any(
        m in lower
        for m in (
            "tone score of 0.00",
            "tone score of 0.0",
            "average tone score is 0.00",
            "average tone score is 0.0",
            "average tone score of 0.00",
            "average tone score of 0.0",
        )
    )
    has_negative_claim = "negative" in lower
    if has_zero_tone and has_negative_claim:
        return (
            "Tone signal not available from news sources for this topic; "
            "sentiment inferred from sample posts is mixed to neutral."
        )
    return sentiment_summary


def _render_header_html(run_date: date) -> str:
    """Branded header lockup (Google x Ogilvy) above every digest.

    Plain text typography only; no images, no remote assets, so it
    renders consistently in Gmail, Apple Mail, Outlook 365 web, and
    most mobile clients without requiring image-loading consent.
    """
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="border-collapse:collapse;margin:0 0 16px 0;background:#0a0e14;'
        'border-radius:6px;">'
        '<tr><td style="padding:18px 20px;">'
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#fff;'
        "font-size:11px;letter-spacing:2px;font-weight:bold;text-transform:uppercase;"
        'opacity:0.7;">Trend Engine</div>'
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:22px;'
        'font-weight:bold;color:#fff;margin-top:6px;">'
        '<span style="color:#4285F4;">Google</span> '
        '<span style="color:#9aa0a6;font-weight:normal;">&times;</span> '
        '<span style="color:#E0001B;">Ogilvy</span>'
        "</div>"
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;'
        f'color:#9aa0a6;margin-top:6px;">'
        f"Sub-Saharan Africa cultural trends, {run_date.isoformat()}</div>"
        "</td></tr></table>"
    )


def _render_summary_card_html(
    market_to_alerts: dict[str, list[TrendAlert]],
    movers_by_market: dict[str, list[TrendMover]] | None,
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]] | None,
    stats: dict[str, Any] | None,
) -> str:
    """Top-line metric row above the detail sections.

    Surfaces the four numbers a stakeholder skims first: tier upgrades,
    score movers, AI briefs, and Vertex spend (when available). Gives
    the reader an at-a-glance feel for the day before drilling into any
    section.
    """
    upgrades = sum(len(v) for v in market_to_alerts.values())
    movers = sum(len(v) for v in (movers_by_market or {}).values())
    briefs = len(briefs_by_topic or {})
    skipped = int((stats or {}).get("briefs_skipped") or 0)
    key_briefs = sum(
        1 for b in (briefs_by_topic or {}).values() if str(b.get("status_tag")) == "Key"
    )

    cells: list[tuple[str, str]] = [
        (str(upgrades), "tier upgrades"),
        (str(movers), "score movers"),
        (str(briefs), "briefs landed"),
        (str(key_briefs), "tagged Key"),
    ]
    if skipped:
        cells.append((str(skipped), "briefs skipped"))

    cell_html = "".join(
        '<td align="center" style="padding:10px 8px;border-right:1px solid #e5e7eb;'
        'font-family:Arial,Helvetica,sans-serif;">'
        f'<div style="font-size:22px;font-weight:bold;color:#0a0e14;">{html.escape(value)}</div>'
        f'<div style="font-size:11px;color:#6b7280;text-transform:uppercase;'
        f'letter-spacing:0.5px;margin-top:2px;">{html.escape(label)}</div>'
        "</td>"
        for value, label in cells
    )
    return (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="border-collapse:collapse;margin:0 0 20px 0;background:#fafafa;'
        'border:1px solid #e5e7eb;border-radius:6px;">'
        f"<tr>{cell_html}</tr>"
        "</table>"
    )


def _render_platform_count_label(entry: str) -> str:
    """Format a platform_counts entry as 'platform (N)' for the email footer.

    Input: 'platform | N items' (the orchestrator format).
    Output: 'TikTok (124)'. Strips the 'items' suffix and reduces noise
    in the platform list so the eye reads count + name in one glance.
    Falls back to the raw entry when the pipe-delimited shape is missing.
    """
    parts = [p.strip() for p in entry.split("|")]
    if len(parts) >= 2 and parts[0]:
        platform = parts[0]
        # 'N items' -> just 'N'.
        count_token = parts[1].split(" ")[0] if parts[1] else parts[1]
        return f"{platform} ({count_token})" if count_token else platform
    return entry.strip() or "web"


def _render_creator_label(entry: str) -> str:
    """Format a top_creators entry for the email chip.

    Input: '@handle | platform | N mentions' (the orchestrator format).
    Output: '@handle (platform)' to keep the chip compact. Falls back to
    the raw entry when the pipe-delimited shape is missing so the email
    never crashes on malformed data.
    """
    parts = [p.strip() for p in entry.split("|")]
    if len(parts) >= 2 and parts[0]:
        handle = parts[0]
        platform = parts[1] or "web"
        return f"{handle} ({platform})"
    return entry.strip() or "@unknown"


def _render_social_ref_cell_html(idx: int, entry: str) -> str:
    """Render one social-reference cell from a 'url | platform | title' string.

    Mirrors Jo's PDF mock layout: each cell has a small 'Link N' eyebrow,
    a clickable platform-tagged anchor, and a truncated title beneath.
    Falls back gracefully when the entry shape is malformed (anchor goes
    on the raw entry text so the cell still renders).
    """
    parts = [p.strip() for p in entry.split("|")]
    url = parts[0] if parts else entry.strip()
    platform = parts[1] if len(parts) >= 2 and parts[1] else "web"
    title = parts[2] if len(parts) >= 3 else ""
    safe_url = html.escape(url, quote=True)
    safe_platform = html.escape(platform)
    safe_title = html.escape(title) if title else ""
    return (
        '<td valign="top" align="center" '
        'style="padding:8px 10px;background:#fafafa;border:1px solid #e5e7eb;'
        'border-radius:4px;width:20%;">'
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:10px;'
        "color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;"
        f'margin-bottom:4px;">Link {idx}</div>'
        f'<a href="{safe_url}" style="font-family:Arial,Helvetica,sans-serif;'
        "font-size:12px;font-weight:bold;color:#4285F4;text-decoration:none;"
        f'display:block;margin-bottom:4px;">{safe_platform}</a>'
        + (
            f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;'
            f'color:#6b7280;line-height:1.4;">{safe_title}</div>'
            if safe_title
            else ""
        )
        + "</td>"
    )


def _render_brief_card_html(market: str, topic: str, brief: dict[str, Any]) -> str:
    """Render one brief card matching Jo's PDF mock layout.

    Card structure:
      [topic + status pill]                              [country flag + name]
      ── description rationale (full width) ──
      ── activation idea (full width, accented background) ──
      [Key Metric 1] [Key Metric 2] [Key Metric 3]   (3-up grid, falls back to stack)
      Platforms: TikTok · YouTube                      Sentiment: 🟢 Positive
    """
    status = str(brief.get("status_tag") or "Rising")
    status_colour = _status_badge_colour(status)
    status_icon = STATUS_EMOJI.get(status, "")
    flag = COUNTRY_FLAGS.get(market, "")
    market_label = COUNTRY_NAMES.get(market, market.upper())
    sentiment = _normalize_sentiment_text(str(brief.get("sentiment_summary") or "").strip())
    sentiment_dot = _sentiment_dot(sentiment) if sentiment else ""

    description = str(brief.get("description_rationale") or "").strip()
    activation = str(brief.get("activation_idea") or "").strip()
    key_metrics = [str(m) for m in (brief.get("key_metrics") or [])][:3]
    platforms = [str(p) for p in (brief.get("platforms") or [])][:4]

    # Header row: topic + status pill on the left, country flag + name on right.
    header = (
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="border-collapse:collapse;margin-bottom:8px;">'
        "<tr>"
        '<td style="font-family:Arial,Helvetica,sans-serif;font-size:16px;'
        'font-weight:bold;color:#0a0e14;vertical-align:middle;">'
        f"{html.escape(topic)} "
        f'<span style="display:inline-block;background:{status_colour};color:#fff;'
        f"padding:3px 9px;border-radius:12px;font-size:11px;font-weight:bold;"
        f'margin-left:6px;vertical-align:middle;">{status_icon} {html.escape(status)}'
        "</span>"
        "</td>"
        '<td align="right" style="font-family:Arial,Helvetica,sans-serif;'
        'font-size:12px;color:#4b5563;white-space:nowrap;vertical-align:middle;">'
        f"{flag} {html.escape(market_label)}"
        "</td>"
        "</tr>"
        "</table>"
    )

    body_parts: list[str] = [header]

    if description:
        body_parts.append(
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;'
            'line-height:1.5;color:#1f2937;margin:0 0 12px 0;">'
            f"{html.escape(description)}"
            "</div>"
        )

    if activation:
        body_parts.append(
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;'
            "line-height:1.5;color:#0a0e14;margin:0 0 12px 0;padding:10px 12px;"
            'background:#eff6ff;border-left:3px solid #4285F4;border-radius:3px;">'
            '<div style="font-size:11px;font-weight:bold;text-transform:uppercase;'
            'letter-spacing:0.6px;color:#4285F4;margin-bottom:4px;">'
            "Activation idea"
            "</div>"
            f"{html.escape(activation)}"
            "</div>"
        )

    # Paste-ready prompts. These are the campaign mandate per the brief:
    # creators copy them directly into Gemini Nano Banana (image) and Lyria
    # (audio). Render in monospace inside a soft-grey panel so the team can
    # spot the prompt block at a glance.
    nano_banana = str(brief.get("nano_banana_prompt") or "").strip()
    lyria = str(brief.get("lyria_prompt") or "").strip()
    visual_anchor = str(brief.get("visual_anchor") or "").strip()
    # Visual anchor sits between activation idea and the paste-ready prompt
    # blocks. One short italic line that tells the reader what the trend
    # actually looks like on screen, before they read the prompt.
    if visual_anchor:
        body_parts.append(
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;'
            "color:#4b5563;font-style:italic;margin:0 0 10px 0;padding:6px 10px;"
            'background:#f9fafb;border-left:2px solid #9aa0a6;border-radius:3px;">'
            '<span style="font-style:normal;font-weight:bold;text-transform:uppercase;'
            'letter-spacing:0.6px;color:#6b7280;font-size:10px;margin-right:6px;">'
            "Visual anchor:</span>"
            f"{html.escape(visual_anchor)}</div>"
        )
    if nano_banana or lyria:
        prompt_blocks: list[str] = []
        if nano_banana:
            prompt_blocks.append(
                '<div style="margin-bottom:8px;">'
                '<div style="font-family:Arial,Helvetica,sans-serif;font-size:10px;'
                "font-weight:bold;text-transform:uppercase;letter-spacing:0.6px;"
                'color:#E0001B;margin-bottom:4px;">Nano Banana prompt'
                '<span style="color:#9aa0a6;font-weight:normal;text-transform:none;'
                'letter-spacing:0;font-size:10px;margin-left:6px;">paste-ready, '
                "creators copy verbatim into Gemini</span></div>"
                "<pre style=\"font-family:'SF Mono',Consolas,Monaco,monospace;"
                "font-size:12px;line-height:1.5;background:#0a0e14;color:#f3f4f6;"
                "padding:10px 12px;border-radius:4px;white-space:pre-wrap;"
                f'word-break:break-word;margin:0;">{html.escape(nano_banana)}</pre>'
                "</div>"
            )
        if lyria:
            prompt_blocks.append(
                "<div>"
                '<div style="font-family:Arial,Helvetica,sans-serif;font-size:10px;'
                "font-weight:bold;text-transform:uppercase;letter-spacing:0.6px;"
                'color:#E0001B;margin-bottom:4px;">Lyria prompt'
                '<span style="color:#9aa0a6;font-weight:normal;text-transform:none;'
                'letter-spacing:0;font-size:10px;margin-left:6px;">paste-ready audio '
                "spec for Lyria</span></div>"
                "<pre style=\"font-family:'SF Mono',Consolas,Monaco,monospace;"
                "font-size:12px;line-height:1.5;background:#0a0e14;color:#f3f4f6;"
                "padding:10px 12px;border-radius:4px;white-space:pre-wrap;"
                f'word-break:break-word;margin:0;">{html.escape(lyria)}</pre>'
                "</div>"
            )
        body_parts.append('<div style="margin:0 0 12px 0;">' + "".join(prompt_blocks) + "</div>")

    if key_metrics:
        # 3-up table grid for the data insights row. Each cell narrows on
        # mobile clients via inherent table behaviour.
        cell_html = "".join(
            '<td valign="top" style="padding:8px 10px;background:#f9fafb;'
            "border:1px solid #e5e7eb;border-radius:4px;"
            "font-family:Arial,Helvetica,sans-serif;font-size:12px;"
            f'color:#1f2937;line-height:1.4;width:33%;">{html.escape(metric)}</td>'
            for metric in key_metrics
        )
        # Pad with empty cells if fewer than 3 metrics so layout stays clean.
        for _ in range(3 - len(key_metrics)):
            cell_html += '<td style="padding:0;width:33%;"></td>'
        body_parts.append(
            '<table role="presentation" width="100%" cellspacing="6" cellpadding="0" '
            'style="border-collapse:separate;margin:8px 0;">'
            "<tr>"
            '<td style="font-family:Arial,Helvetica,sans-serif;font-size:10px;'
            "font-weight:bold;text-transform:uppercase;letter-spacing:0.6px;"
            'color:#6b7280;padding-bottom:4px;" colspan="3">'
            "Key metrics"
            "</td></tr>"
            f"<tr>{cell_html}</tr></table>"
        )

    # Top creators row. Each entry comes in as
    # '@handle | platform | N mentions' so we split for clean rendering.
    top_creators = [str(c) for c in (brief.get("top_creators") or [])]
    if top_creators:
        chip_html = "".join(
            '<span style="display:inline-block;font-family:Arial,Helvetica,sans-serif;'
            "font-size:11px;color:#1f2937;background:#f3f4f6;border:1px solid #e5e7eb;"
            'padding:3px 8px;border-radius:12px;margin:2px 4px 2px 0;">'
            f"{html.escape(_render_creator_label(entry))}"
            "</span>"
            for entry in top_creators
        )
        body_parts.append(
            '<div style="margin:0 0 10px 0;">'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:10px;'
            "font-weight:bold;text-transform:uppercase;letter-spacing:0.6px;"
            'color:#6b7280;margin-bottom:6px;">Top creators to brief</div>'
            f"<div>{chip_html}</div>"
            "</div>"
        )

    # Social references row mirroring Jo's PDF mock 'Link 1 / Channel'
    # five-cell layout. Each entry comes in as 'url | platform | title';
    # we render as a row of platform-tagged anchors so the team can jump
    # straight to the source post.
    social_refs = [str(s) for s in (brief.get("social_refs") or [])]
    if social_refs:
        link_html = "".join(
            _render_social_ref_cell_html(idx + 1, entry) for idx, entry in enumerate(social_refs)
        )
        body_parts.append(
            '<div style="margin:0 0 10px 0;">'
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:10px;'
            "font-weight:bold;text-transform:uppercase;letter-spacing:0.6px;"
            'color:#6b7280;margin-bottom:6px;">Social references</div>'
            '<table role="presentation" width="100%" cellspacing="6" cellpadding="0" '
            'style="border-collapse:separate;">'
            f"<tr>{link_html}</tr></table>"
            "</div>"
        )

    # Footer row: platforms left, sentiment right. Prefer platform_counts
    # (data-driven 'TikTok | 124 items' entries) over the bare platform
    # list when it's available, so the team sees how heavily each
    # platform contributed without leaving the email.
    raw_platform_counts = [str(p) for p in (brief.get("platform_counts") or [])]
    if raw_platform_counts:
        platforms_display = " · ".join(
            html.escape(_render_platform_count_label(entry)) for entry in raw_platform_counts
        )
    elif platforms:
        platforms_display = " · ".join(html.escape(p) for p in platforms)
    else:
        platforms_display = "&mdash;"
    footer_left = (
        '<span style="font-family:Arial,Helvetica,sans-serif;font-size:11px;'
        "color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;"
        'font-weight:bold;">Platforms: </span>'
        f'<span style="font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#1f2937;">'
        f"{platforms_display}"
        "</span>"
    )
    footer_right = ""
    if sentiment:
        footer_right = (
            f'<span style="font-family:Arial,Helvetica,sans-serif;font-size:12px;'
            f'color:#1f2937;">{sentiment_dot} {html.escape(sentiment)}</span>'
        )
    body_parts.append(
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="border-collapse:collapse;margin-top:8px;border-top:1px solid #e5e7eb;'
        'padding-top:8px;">'
        "<tr>"
        f'<td style="padding-top:8px;">{footer_left}</td>'
        f'<td align="right" style="padding-top:8px;">{footer_right}</td>'
        "</tr></table>"
    )

    return (
        '<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:6px;'
        'padding:16px 18px;margin:12px 0;box-shadow:0 1px 2px rgba(0,0,0,0.04);">'
        + "".join(body_parts)
        + "</div>"
    )


def _render_brief_block_html(
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]],
) -> str:
    """Render the Phase 2 daily-briefs section grouped per market.

    Cards match Jo's PDF mock: topic + status pill, country flag, full
    description, accented activation block, 3-up key metrics row,
    platform list and sentiment dot in the footer.
    """
    if not briefs_by_topic:
        return ""

    by_market: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for (market, topic), brief in briefs_by_topic.items():
        by_market.setdefault(market, []).append((topic, brief))

    parts: list[str] = [
        '<h3 style="font-family:Arial,Helvetica,sans-serif;'
        "border-bottom:2px solid #0a0e14;padding-bottom:8px;margin-top:32px;"
        'color:#0a0e14;">Creative concept &amp; activation briefs</h3>',
        '<div style="font-family:Arial,Helvetica,sans-serif;color:#6b7280;'
        'font-size:13px;margin:0 0 12px 0;line-height:1.5;">'
        "AI-generated trend briefs in the shape of the Ogilvy &times; Google Nano "
        "Banana / Lyria activation framework. Each card carries description &amp; "
        "rationale, an activation idea, three proof points, primary platforms, "
        "and a sentiment read.</div>",
    ]
    for market in sorted(by_market.keys()):
        flag = COUNTRY_FLAGS.get(market, "")
        market_label = COUNTRY_NAMES.get(market, market.upper())
        parts.append(
            '<h4 style="font-family:Arial,Helvetica,sans-serif;margin:18px 0 8px 0;'
            f'color:#0a0e14;font-size:14px;">{flag} {html.escape(market_label)}</h4>'
        )
        for topic, brief in by_market[market]:
            parts.append(_render_brief_card_html(market, topic, brief))
    return "".join(parts)


def _render_brief_block_text(
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]],
) -> list[str]:
    """Plain-text fallback for the Phase 2 daily briefs section."""
    if not briefs_by_topic:
        return []
    by_market: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for (market, topic), brief in briefs_by_topic.items():
        by_market.setdefault(market, []).append((topic, brief))

    lines: list[str] = ["", "Daily briefs", "=" * 40]
    for market in sorted(by_market.keys()):
        lines.append("")
        lines.append(f"{market.upper()}")
        lines.append("-" * 40)
        for topic, brief in by_market[market]:
            status = str(brief.get("status_tag") or "Rising")
            lines.append("")
            lines.append(f"{topic} [{status}]")
            description = str(brief.get("description_rationale") or "").strip()
            if description:
                lines.append(description)
            activation = str(brief.get("activation_idea") or "").strip()
            if activation:
                lines.append(f"Activation: {activation}")
            nano_banana = str(brief.get("nano_banana_prompt") or "").strip()
            if nano_banana:
                lines.append("")
                lines.append("Nano Banana prompt (paste-ready):")
                lines.append(nano_banana)
            lyria = str(brief.get("lyria_prompt") or "").strip()
            if lyria:
                lines.append("")
                lines.append("Lyria prompt (paste-ready):")
                lines.append(lyria)
            for metric in brief.get("key_metrics") or []:
                lines.append(f"  - {metric}")
            platforms = brief.get("platforms") or []
            if platforms:
                lines.append(f"Platforms: {' / '.join(str(p) for p in platforms)}")
            sentiment = _normalize_sentiment_text(str(brief.get("sentiment_summary") or "").strip())
            if sentiment:
                lines.append(f"Sentiment: {sentiment}")
            top_creators = brief.get("top_creators") or []
            if top_creators:
                lines.append("Top creators:")
                for entry in top_creators:
                    lines.append(f"  - {_render_creator_label(str(entry))}")
            social_refs = brief.get("social_refs") or []
            if social_refs:
                lines.append("Social references:")
                for entry in social_refs:
                    parts = [p.strip() for p in str(entry).split("|")]
                    url = parts[0] if parts else str(entry)
                    platform = parts[1] if len(parts) >= 2 else "web"
                    lines.append(f"  - [{platform}] {url}")
    return lines


def _render_daily_summary_html(daily_summary: dict[str, Any]) -> str:
    """Render the cross-trend executive summary at the top of the digest.

    Three-part block: through_line as the headline, summary_text as the
    paragraph beneath, and call_to_action in an accented panel so the
    skim-reader sees the "do this week" line even if they read nothing
    else. Renders only when summary_text is non-empty; otherwise returns
    empty string and the email falls back to the prior layout.
    """
    summary_text = str(daily_summary.get("summary_text") or "").strip()
    if not summary_text:
        return ""
    through_line = str(daily_summary.get("through_line") or "").strip()
    cta = str(daily_summary.get("call_to_action") or "").strip()

    parts: list[str] = [
        '<div style="margin:0 0 20px 0;padding:18px 20px;background:#f9fafb;'
        "border:1px solid #e5e7eb;border-left:4px solid #4285F4;"
        'border-radius:6px;font-family:Arial,Helvetica,sans-serif;">',
        '<div style="font-size:11px;font-weight:bold;text-transform:uppercase;'
        'letter-spacing:0.6px;color:#4285F4;margin-bottom:6px;">'
        "Today's through-line</div>",
    ]
    if through_line:
        parts.append(
            '<div style="font-size:18px;font-weight:bold;line-height:1.35;'
            f'color:#0a0e14;margin-bottom:10px;">{html.escape(through_line)}</div>'
        )
    parts.append(
        '<div style="font-size:13px;line-height:1.55;color:#1f2937;margin-bottom:'
        f'12px;">{html.escape(summary_text)}</div>'
    )
    if cta:
        parts.append(
            '<div style="font-size:12px;line-height:1.5;color:#0a0e14;'
            "padding:10px 12px;background:#fff8e1;border-left:3px solid #ffb900;"
            'border-radius:3px;margin-top:8px;">'
            '<span style="font-weight:bold;text-transform:uppercase;font-size:10px;'
            'letter-spacing:0.6px;color:#b08600;margin-right:6px;">Action this week:'
            "</span>"
            f"{html.escape(cta)}</div>"
        )
    parts.append("</div>")
    return "".join(parts)


def _briefs_displays_for_pulse(
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Turn the {(market, topic): brief_dict} map into the two aligned lists
    the PULSE v2 renderers take: ``briefs`` and ``displays`` (same order,
    same length, positionally aligned).

    Each render-brief is the stored brief dict with ``market`` and ``topic``
    injected from the key (the renderers read ``brief['market']`` and
    ``brief['topic']``). The display is the brief's own ``display`` field
    (attached upstream in generate_briefs); an absent display degrades to
    an empty dict, which the renderers handle by dropping those elements.

    Ordering is trend_score descending across ALL markets, then market then
    topic as deterministic tiebreakers. Score-first (not market-first) so the
    hero is the single strongest trend, the move cards and the masthead shift
    line span markets, and a strong ZA or NG trend is never buried below every
    KE topic. A market-first sort made the lead and the shift line read as a
    single-market digest, which is wrong for a tri-market SSA engine.
    """
    items = list(briefs_by_topic.items())

    def _score(kv: tuple[tuple[str, str], dict[str, Any]]) -> float:
        try:
            return float(kv[1].get("trend_score") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    items.sort(key=lambda kv: (-_score(kv), kv[0][0], kv[0][1]))

    briefs: list[dict[str, Any]] = []
    displays: list[dict[str, Any]] = []
    for (market, topic), brief in items:
        render_brief = {**brief, "market": market, "topic": topic}
        briefs.append(render_brief)
        display = brief.get("display")
        displays.append(display if isinstance(display, dict) else {})
    return briefs, displays


def _pulse_inbox_body(
    rich_html: str,
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]] | None,
    run_date: date,
    daily_summary: dict[str, Any] | None,
    full_url: str,
) -> str:
    """Choose the inbox body for a PULSE v2 send (Path A).

    When a real hosted full-read URL exists, the inbox gets the lean
    ``render_inbox_summary`` (the verdict, the top headlines, and a button to
    the hosted rich PULSE), which reads premium in Outlook's web-safe fallback.
    With no URL (the GCS bucket is not yet provisioned) the rich body ships
    inline exactly as before, so the cron is unchanged until hosting is live.
    """
    if not full_url:
        return rich_html
    from src.alerts.email_render import render_inbox_summary
    from src.alerts.email_render.brand import load_brand

    briefs, displays = _briefs_displays_for_pulse(briefs_by_topic or {})
    return render_inbox_summary(
        briefs, displays, load_brand("ogilvy"), run_date, full_url, daily_summary=daily_summary
    )


def _guard_inline_body_size(
    rich_html: str,
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]] | None,
    run_date: date,
    daily_summary: dict[str, Any] | None,
    ceiling: int = INLINE_BODY_CEILING_BYTES,
) -> str:
    """Keep the inline-fallback PULSE body under the Gmail clip ceiling.

    Only the inline path needs this (the teaser path links out to GCS and is
    always small). When the full rich body fits under ``ceiling`` decoded bytes
    it ships unchanged. When it is over, the board long tail is the cheap thing
    to drop. The trim is MARKET-AWARE: ``briefs`` is score-sorted descending by
    ``_briefs_displays_for_pulse``, and a flat global top-N cut would drop the
    lowest-scoring market wholesale on a low-score day (the 28 Jun incident,
    where ZA sat at the bottom of the cross-market order and vanished). Instead,
    cap each market's contribution and step the per-market cap down until the
    body clears the ceiling, keeping the global score order (so the hero stays
    the top trend) but never zeroing a market: at cap 1 every market still keeps
    its top brief. Returns the rich body unchanged when it already fits.
    """
    if len(rich_html.encode("utf-8")) <= ceiling:
        return rich_html

    from src.alerts.email_render import render_pulse_html
    from src.alerts.email_render.brand import load_brand

    briefs, displays = _briefs_displays_for_pulse(briefs_by_topic or {})
    brand = load_brand("ogilvy")
    pan_african_stories = None
    if os.environ.get("PAN_AFRICAN_ENABLED", "").strip().lower() in {"1", "true", "yes"}:
        from src.analysis.pan_african import read_pan_african_stories

        pan_african_stories = read_pan_african_stories(run_date)
    micro_briefs_by_market = None
    if isinstance(daily_summary, dict):
        micro_briefs_by_market = daily_summary.get("micro_briefs_by_market")

    pairs = list(zip(briefs, displays, strict=False))
    counts_per_market: dict[str, int] = {}
    for b, _ in pairs:
        m = str(b.get("market") or "")
        counts_per_market[m] = counts_per_market.get(m, 0) + 1
    longest = max(counts_per_market.values(), default=0)

    # Step the per-market cap down from the longest market to 1. pairs stays in
    # global score order, so the kept set keeps the highest-scoring briefs per
    # market and the hero (briefs[0]) is always retained.
    best = rich_html
    for per_cap in range(longest, 0, -1):
        kept_seen: dict[str, int] = {}
        kept = []
        for b, d in pairs:
            m = str(b.get("market") or "")
            if kept_seen.get(m, 0) < per_cap:
                kept.append((b, d))
                kept_seen[m] = kept_seen.get(m, 0) + 1
        kept_briefs = [b for b, _ in kept]
        kept_displays = [d for _, d in kept]
        trimmed = render_pulse_html(
            kept_briefs,
            kept_displays,
            brand,
            run_date,
            daily_summary=daily_summary,
            pan_african_stories=pan_african_stories,
            micro_briefs_by_market=micro_briefs_by_market,
        )
        best = trimmed
        if len(trimmed.encode("utf-8")) <= ceiling:
            return trimmed
    return best


def render_html(
    market_to_alerts: dict[str, list[TrendAlert]],
    run_date: date,
    movers_by_market: dict[str, list[TrendMover]] | None = None,
    issues: list[IssueFlag] | None = None,
    stats: dict[str, Any] | None = None,
    healed: list[HealedIssue] | None = None,
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]] | None = None,
    daily_summary: dict[str, Any] | None = None,
) -> str:
    """Render the full HTML email body.

    Sections appear in order only when they have content: tier upgrades,
    top movers, auto-resolved feeds, still-broken issues, pipeline stats.
    Stats always render at the bottom when supplied so the reader sees run
    health even on quiet days. When `healed` is non-empty the still-broken
    heading flips to "Still broken" to give the pair cleaner framing.

    When the PULSE v2 mailer flag (MAILER_V2_ENABLED) is on, the body is
    composed by the editorial v2 renderer instead. The v1 path below is
    untouched and is exactly what ships when the flag is off.
    """
    if _mailer_v2_enabled():
        from src.alerts.email_render import render_pulse_html
        from src.alerts.email_render.brand import load_brand

        briefs, displays = _briefs_displays_for_pulse(briefs_by_topic or {})
        # Wave 2 pan-African section: the stage wrote rows to BigQuery during the
        # run but this render path does not hold them, so read them back when the
        # flag is on. The reader and render_pan_african both self-gate / degrade
        # to nothing, so a quiet day or a read error just omits the section.
        pan_african_stories = None
        if os.environ.get("PAN_AFRICAN_ENABLED", "").strip().lower() in {"1", "true", "yes"}:
            from src.analysis.pan_african import read_pan_african_stories

            pan_african_stories = read_pan_african_stories(run_date)
        # Wave 3 micro-briefs: run_rss_now.py stashes the day's selection on
        # daily_summary_dict (no new parameter threaded through send_email_digest
        # / send_daily_digest). Absent on any flag-off or pre-Wave-3 caller.
        micro_briefs_by_market = None
        if isinstance(daily_summary, dict):
            micro_briefs_by_market = daily_summary.get("micro_briefs_by_market")
        return render_pulse_html(
            briefs,
            displays,
            load_brand("ogilvy"),
            run_date,
            daily_summary=daily_summary,
            pan_african_stories=pan_african_stories,
            micro_briefs_by_market=micro_briefs_by_market,
        )

    markets = sorted(
        market_to_alerts.keys(),
        key=lambda m: (-len(market_to_alerts[m]), m),
    )
    total = sum(len(alerts) for alerts in market_to_alerts.values())
    parts: list[str] = [
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:760px;'
        "margin:0 auto;padding:16px;color:#1f2937;background:#ffffff;"
        'box-sizing:border-box;">',
        _render_header_html(run_date),
    ]
    if daily_summary:
        parts.append(_render_daily_summary_html(daily_summary))
    parts.append(
        _render_summary_card_html(market_to_alerts, movers_by_market, briefs_by_topic, stats)
    )

    if total > 0:
        parts.append(
            '<h3 style="font-family:Arial,Helvetica,sans-serif;'
            "border-bottom:2px solid #0a0e14;padding-bottom:8px;margin-top:24px;"
            f'color:#0a0e14;">Tier upgrades</h3>'
            f'<div style="font-family:Arial,Helvetica,sans-serif;color:#6b7280;'
            f'font-size:13px;margin:0 0 12px 0;">'
            f"{total} upgrade{'s' if total != 1 else ''} across "
            f"{len(markets)} market{'s' if len(markets) != 1 else ''}.</div>"
        )
        for market in markets:
            alerts = sorted(market_to_alerts[market], key=lambda a: a.trend_score, reverse=True)
            flag = COUNTRY_FLAGS.get(market, "")
            market_label = COUNTRY_NAMES.get(market, market.upper())
            parts.append(
                '<h4 style="font-family:Arial,Helvetica,sans-serif;'
                'margin:18px 0 8px 0;color:#0a0e14;font-size:14px;">'
                f"{flag} {html.escape(market_label)}: "
                f"{len(alerts)} upgrade{'s' if len(alerts) != 1 else ''}</h4>"
            )
            for alert in alerts:
                parts.append(_render_item_html(alert))
    else:
        # Quiet-day empty state. Renders only when there are no upgrades
        # AND nothing more meaningful (briefs, summary) is taking the
        # primary slot. Keeps the email visually consistent across days.
        parts.append(
            '<div style="font-family:Arial,Helvetica,sans-serif;color:#6b7280;'
            'font-size:13px;margin:8px 0 16px 0;">No tier upgrades today.</div>'
        )

    if movers_by_market and any(movers_by_market.values()):
        parts.append(_render_movers_html(movers_by_market))

    if briefs_by_topic:
        parts.append(_render_brief_block_html(briefs_by_topic))

    if healed:
        parts.append(_render_healed_html(healed))

    if issues:
        heading = "Still broken" if healed else "Things that broke"
        parts.append(_render_issues_html(issues, heading=heading))

    if stats:
        parts.append(_render_stats_html(stats))

    parts.append(
        '<div style="margin-top:32px;padding:14px 16px;border-top:2px solid #0a0e14;'
        "background:#fafafa;font-family:Arial,Helvetica,sans-serif;font-size:11px;"
        'color:#6b7280;line-height:1.5;">'
        f'<div style="font-weight:bold;color:#0a0e14;margin-bottom:4px;">'
        f"Trends Engine V2 &middot; {run_date.isoformat()}</div>"
        "Powered by WPP. This digest is generated from the daily Sub-Saharan Africa "
        "trends pipeline. The dashboard has been carefully set up on the basis of the "
        "underlying data available; no rights can be derived from the contents. "
        "WPP cannot be held liable for any omissions or inaccuracies. Please reach out "
        "to your client service representative with questions or suggestions."
        "</div>"
    )
    parts.append("</div>")
    return "".join(parts)


def render_text(
    market_to_alerts: dict[str, list[TrendAlert]],
    run_date: date,
    movers_by_market: dict[str, list[TrendMover]] | None = None,
    issues: list[IssueFlag] | None = None,
    stats: dict[str, Any] | None = None,
    healed: list[HealedIssue] | None = None,
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]] | None = None,
    daily_summary: dict[str, Any] | None = None,
) -> str:
    """Render the plain-text fallback body."""
    markets = sorted(
        market_to_alerts.keys(),
        key=lambda m: (-len(market_to_alerts[m]), m),
    )
    total = sum(len(alerts) for alerts in market_to_alerts.values())
    lines: list[str] = [f"Trends Digest, {run_date.isoformat()}"]

    if daily_summary:
        through_line = str(daily_summary.get("through_line") or "").strip()
        summary_text = str(daily_summary.get("summary_text") or "").strip()
        cta = str(daily_summary.get("call_to_action") or "").strip()
        if through_line or summary_text:
            lines.append("")
            lines.append("Today's through-line")
            lines.append("=" * 40)
            if through_line:
                lines.append(through_line)
            if summary_text:
                lines.append("")
                lines.append(summary_text)
            if cta:
                lines.append("")
                lines.append(f"Action this week: {cta}")

    if total > 0:
        lines.append(
            f"{total} tier upgrade{'s' if total != 1 else ''} across "
            f"{len(markets)} market{'s' if len(markets) != 1 else ''}."
        )
        for market in markets:
            alerts = sorted(market_to_alerts[market], key=lambda a: a.trend_score, reverse=True)
            lines.append("")
            count = len(alerts)
            lines.append(f"{market.upper()}: {count} upgrade{'s' if count != 1 else ''}")
            lines.append("=" * 40)
            for i, alert in enumerate(alerts, 1):
                lines.append("")
                lines.extend(_render_item_text(alert, i))
    else:
        lines.append("No tier upgrades today.")

    if movers_by_market and any(movers_by_market.values()):
        lines.extend(_render_movers_text(movers_by_market))

    if briefs_by_topic:
        lines.extend(_render_brief_block_text(briefs_by_topic))

    if healed:
        lines.extend(_render_healed_text(healed))

    if issues:
        heading = "Still broken" if healed else "Things that broke"
        lines.extend(_render_issues_text(issues, heading=heading))

    if stats:
        lines.extend(_render_stats_text(stats))

    lines.append("")
    lines.append(f"Trends Engine V2, {run_date.isoformat()}.")
    return "\n".join(lines)


def build_subject(
    market_to_alerts: dict[str, list[TrendAlert]],
    run_date: date,
    movers_by_market: dict[str, list[TrendMover]] | None = None,
    issues: list[IssueFlag] | None = None,
    healed: list[HealedIssue] | None = None,
    daily_summary: dict[str, Any] | None = None,
) -> str:
    # PULSE v2: lead the subject with the day's cultural verdict (the
    # through_line) instead of the ops upgrade/mover count, so the inbox preview
    # reads as an editorial hook, not a pipeline status line. Falls through to
    # the ops subject when the v2 flag is off or no verdict exists.
    if _mailer_v2_enabled() and daily_summary:
        from src.alerts.email_render._util import clean_copy

        verdict = clean_copy(str(daily_summary.get("through_line") or "")).strip().rstrip(".")
        if verdict:
            # Cap the verdict on a word boundary. The full through_line runs
            # ~180 chars, which truncates mid-word in the inbox subject preview
            # (the preheader carries the fuller line). 78 chars keeps the hook
            # scannable and front-loaded.
            cap = 78
            if len(verdict) > cap:
                verdict = verdict[:cap].rsplit(" ", 1)[0].rstrip(",.;:") + "..."
            return f"Trends Digest, {run_date.isoformat()} | {verdict}"

    def _plural(n: int, word: str) -> str:
        return f"{n} {word}" if n == 1 else f"{n} {word}s"

    total_upgrades = sum(len(alerts) for alerts in market_to_alerts.values())
    total_movers = sum(len(v) for v in movers_by_market.values()) if movers_by_market else 0
    issue_count = len(issues) if issues else 0
    healed_count = len(healed) if healed else 0

    # Always include the upgrade count (even when zero) so subject-line
    # parsing stays consistent across days. Earlier behavior omitted the
    # zero-upgrade case entirely, so the subject went from
    # "2 upgrades (NG 1, ZA 1) | 9 movers" yesterday to "9 movers" today,
    # making same-shape parsing harder. Zero-upgrade days are still
    # informationally distinct from quiet days because they retain the
    # mover count.
    parts: list[str] = []
    if total_upgrades:
        breakdown = ", ".join(
            f"{m.upper()} {len(alerts)}" for m, alerts in sorted(market_to_alerts.items())
        )
        parts.append(f"{_plural(total_upgrades, 'upgrade')} ({breakdown})")
    elif total_movers or issue_count or healed_count:
        # Anchor zero-upgrade days with the literal "0 upgrades" so
        # downstream parsing can tell apart "0 upgrades + N movers"
        # from "quiet day".
        parts.append("0 upgrades")
    if total_movers:
        parts.append(_plural(total_movers, "mover"))
    if healed_count:
        parts.append(f"{healed_count} auto-fixed")
    if issue_count:
        parts.append(_plural(issue_count, "issue"))
    if not parts:
        parts.append("quiet day")

    return f"Trends Digest, {run_date.isoformat()} | " + " | ".join(parts)


def send_email_digest(
    market_to_alerts: dict[str, list[TrendAlert]],
    run_date: date | None = None,
    *,
    movers_by_market: dict[str, list[TrendMover]] | None = None,
    issues: list[IssueFlag] | None = None,
    stats: dict[str, Any] | None = None,
    healed: list[HealedIssue] | None = None,
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]] | None = None,
    daily_summary: dict[str, Any] | None = None,
    user: str | None = None,
    password: str | None = None,
    recipients: list[str] | None = None,
) -> str:
    """Send one digest email covering every market.

    Returns a status string naming the exact outcome so the caller can record a
    deliberate dry run apart from a real send failure:

      "sent"           the email was handed to the SMTP server
      "dry_run"        EMAIL_ALERTS_ENABLED != true, nothing was sent (not a failure)
      "no_creds"       Gmail user/password missing, skipped
      "no_recipients"  no recipients configured, skipped
      "smtp_failed"    the send was attempted but SMTP/auth/socket failed

    The four non-"sent" values are all falsy-by-intent only at the caller's
    mapping layer; this function returns the explicit string. detector.
    send_daily_digest collapses no_creds / no_recipients / smtp_failed to
    "send_failed" and passes "dry_run" through, so a dry run is no longer
    mislabelled as a send failure on the pipeline marker row.
    """
    # UTC date, not the host's local date. The BQ trend_date is UTC, so a
    # non-UTC host taking date.today() could stamp the subject, footer, and
    # GCS blob name a day off from the data.
    run_date = run_date or datetime.now(UTC).date()
    enabled = os.environ.get("EMAIL_ALERTS_ENABLED", "").strip().lower() == "true"
    user = user or os.environ.get("GMAIL_USER", "").strip()
    password = password or os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if recipients is None:
        raw = os.environ.get("EMAIL_RECIPIENTS", "").strip()
        recipients = [r.strip() for r in raw.split(",") if r.strip()]

    subject = build_subject(
        market_to_alerts, run_date, movers_by_market, issues, healed, daily_summary=daily_summary
    )
    text_body = render_text(
        market_to_alerts,
        run_date,
        movers_by_market,
        issues,
        stats,
        healed,
        briefs_by_topic=briefs_by_topic,
        daily_summary=daily_summary,
    )
    html_body = render_html(
        market_to_alerts,
        run_date,
        movers_by_market,
        issues,
        stats,
        healed,
        briefs_by_topic=briefs_by_topic,
        daily_summary=daily_summary,
    )

    if not enabled:
        logger.info(
            "Email alerts disabled (EMAIL_ALERTS_ENABLED != true). Dry-run subject=%r to=%r",
            subject,
            recipients,
        )
        return "dry_run"
    if not user or not password:
        logger.warning("Gmail credentials missing; skipping email alert.")
        return "no_creds"
    if not recipients:
        logger.warning("No recipients configured; skipping email alert.")
        return "no_recipients"

    # PULSE v2 (Path A): archive the rich editorial body to GCS, then send the
    # lean inbox teaser linking to that hosted full read. Runs only AFTER the
    # enabled/credentials/recipients gates so a dry run never overwrites a prior
    # pulse/<date>-<token>.html object (the public URL the already-sent email
    # links to). Archiving is non-fatal and inert when the bucket is unset; when
    # no hosted URL results (bucket unset, or upload blip), the rich body ships
    # inline exactly as before, so the cron is unchanged until hosting is live.
    #
    # When the bucket IS configured, the full-read URL must come from this
    # send's own upload. If that upload fails (archived_url == ""), do NOT fall
    # back to the static MAILER_FULL_URL: that URL does not host today's read,
    # so the teaser would link to a stale page and the rich body would be
    # dropped. Force full_url = "" instead so the rich PULSE ships inline. The
    # static MAILER_FULL_URL is only a valid full-read target when no bucket is
    # set (the pre-hosting configuration where a fixed page is the intended
    # destination).
    if _mailer_v2_enabled():
        if MAILER_ARCHIVE_BUCKET:
            full_url = _safe_full_read_url(archive_html_to_gcs(html_body, run_date))
        else:
            raw = MAILER_FULL_URL if MAILER_FULL_URL not in ("", "#") else ""
            full_url = _safe_full_read_url(raw)
        # No hosted URL means the rich body ships inline. Trim its board long tail
        # if it would breach the Gmail clip ceiling before it goes into the inbox
        # summary fork (which no-ops to the rich body when full_url is "").
        if not full_url:
            html_body = _guard_inline_body_size(html_body, briefs_by_topic, run_date, daily_summary)
        html_body = _pulse_inbox_body(html_body, briefs_by_topic, run_date, daily_summary, full_url)
        briefs, displays = _briefs_displays_for_pulse(briefs_by_topic or {})
        from src.alerts.email_render import render_inbox_summary_text

        text_body = render_inbox_summary_text(
            briefs, displays, run_date, full_url or "", daily_summary=daily_summary
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(user, password)
            server.sendmail(user, recipients, msg.as_string())
        logger.info("Email digest sent: subject=%r recipients=%d", subject, len(recipients))
        return "sent"
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("Email send failed: %s", exc)
        return "smtp_failed"


__all__ = [
    "TIER_ORDER",
    "HealedIssue",
    "IssueFlag",
    "TrendAlert",
    "TrendMover",
    "build_alert",
    "build_subject",
    "render_html",
    "render_text",
    "score_to_tier",
    "send_email_digest",
    "tier_upgraded",
]
