"""Watched signal projection for the unsent PULSE line.

Upstream computes the approved readiness state and bands. This module only
compares two records of the same signal on the material fields, validates both
records against the approved signal contract before comparing, validates the
approved PULSE line shape and its release proof, and builds the one deep link
the 42 router already serves for a dynamic signal. Nothing here sends anything.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime

from src.analysis.open_intelligence.run_receipts import (
    CANONICAL_MARKETS,
    QA_CLIENT_SCOPE_ID,
    OpenIntelligenceRunReceipt,
    receipt_qualifies_for_display,
)
from src.contracts.open_intelligence_fixtures import (
    CONTRACT_VERSION,
    PULSE_FIELDS,
    READINESS_STATES,
)

MATERIAL_FIELDS = (
    "evidence_state",
    "velocity_band",
    "breadth_band",
    "agreement",
    "market",
    "lineage_digest",
)
WATCHED_SIGNAL_RECORD_FIELDS = ("signal_id", "run_id", *MATERIAL_FIELDS)
# The 42 staging origin (general_question_deployment names the same audience) and the
# topic story hash grammar of app/frontend/src/router.js buildTopicHash, which
# explore.jsx opens for a selected dynamic signal: '#/topic/' + id + '?region=' + scope.
STAGING_ORIGIN = "https://listening-post-staging-fibxg5ynpq-uc.a.run.app"
_ROUTE_REGIONS = frozenset({*CANONICAL_MARKETS, "all"})
_SIGNAL_ID = re.compile(r"sig_[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_BAND_FIELDS = ("velocity_band", "breadth_band", "agreement")
_NUMERIC_TEXT = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?%?\Z", re.IGNORECASE)
_PULSE_TEXT_FIELDS = (
    "run_id",
    "client_scope_id",
    "brand_config_id",
    "theme_id",
    "signal_name",
    "what_changed",
    "material_change",
    "deep_link",
)


def watched_change(previous: dict, current: dict) -> tuple[str, ...]:
    return tuple(key for key in MATERIAL_FIELDS if previous[key] != current[key])


def _signal_record(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("signal_record_invalid")
    for field in WATCHED_SIGNAL_RECORD_FIELDS:
        text = value.get(field)
        if not isinstance(text, str) or not text:
            raise ValueError("signal_record_invalid")
    if _SIGNAL_ID.fullmatch(value["signal_id"]) is None:
        raise ValueError("signal_record_invalid")
    _signal_contract(value)
    return value


def _signal_contract(record: Mapping[str, object]) -> None:
    """The approved signal contract: readiness vocabulary, canonical market, digest lineage.

    Bands and agreement arrive as upstream labels. A numeric value here would mean a
    threshold was applied downstream, which this projection never does.
    """
    if record["evidence_state"] not in READINESS_STATES:
        raise ValueError("signal_contract_invalid")
    if record["market"] not in CANONICAL_MARKETS:
        raise ValueError("signal_contract_invalid")
    if _DIGEST.fullmatch(record["lineage_digest"]) is None:
        raise ValueError("signal_contract_invalid")
    if any(_NUMERIC_TEXT.fullmatch(record[field].strip()) for field in _BAND_FIELDS):
        raise ValueError("signal_contract_invalid")


def validated_watched_change(previous: dict, current: dict) -> tuple[str, ...]:
    """Compare two validated records of one signal; a repeated run never notifies."""
    _signal_record(previous)
    _signal_record(current)
    if previous["signal_id"] != current["signal_id"]:
        raise ValueError("signal_continuity_required")
    reasons = watched_change(previous, current)
    if previous["run_id"] == current["run_id"]:
        if reasons:
            raise ValueError("run_identity_conflict")
        return ()
    return reasons


def _pulse_line(current: object) -> dict[str, object]:
    if not isinstance(current, Mapping) or set(current) != PULSE_FIELDS:
        raise ValueError("pulse_line_invalid")
    if current["contract_version"] != CONTRACT_VERSION:
        raise ValueError("pulse_line_invalid")
    for field in _PULSE_TEXT_FIELDS:
        if not isinstance(current[field], str) or not current[field]:
            raise ValueError("pulse_line_invalid")
    signal_id = current["signal_id"]
    if not isinstance(signal_id, str) or _SIGNAL_ID.fullmatch(signal_id) is None:
        raise ValueError("pulse_line_invalid")
    if current["evidence_state"] not in READINESS_STATES:
        raise ValueError("pulse_line_invalid")
    markets = current["market_scope"]
    lenses = current["audience_lens_ids"]
    if not isinstance(markets, (list, tuple)) or not markets:
        raise ValueError("pulse_line_invalid")
    if any(
        not isinstance(market, str) or not market or market != market.lower() for market in markets
    ):
        raise ValueError("pulse_line_invalid")
    if not isinstance(lenses, (list, tuple)) or any(not isinstance(lens, str) for lens in lenses):
        raise ValueError("pulse_line_invalid")
    day = current["continuity_day"]
    if isinstance(day, bool) or not isinstance(day, int) or day < 1:
        raise ValueError("pulse_line_invalid")
    return dict(current)


def _reasons(reasons: object) -> tuple[str, ...]:
    if not isinstance(reasons, tuple):
        raise ValueError("reasons_invalid")
    if reasons == ():
        raise ValueError("no_material_change")
    if len(set(reasons)) != len(reasons) or any(key not in MATERIAL_FIELDS for key in reasons):
        raise ValueError("reasons_invalid")
    if reasons != tuple(key for key in MATERIAL_FIELDS if key in reasons):
        raise ValueError("reasons_invalid")
    return reasons


def _release_proof(line: Mapping[str, object], release: object, today: object) -> None:
    if isinstance(today, datetime) or not isinstance(today, date):
        raise ValueError("today_invalid")
    if not isinstance(release, OpenIntelligenceRunReceipt):
        raise ValueError("release_proof_required")
    if release.run_id != line["run_id"] or release.client_scope_id != line["client_scope_id"]:
        raise ValueError("release_proof_required")
    if not receipt_qualifies_for_display(
        release, requested_markets=tuple(line["market_scope"]), today=today
    ):
        raise ValueError("release_proof_required")


def signal_deep_link(signal_id: str, market_scope: tuple[str, ...]) -> str:
    """The exact 42 staging deep link for one signal: the topic story route.

    app/frontend/src/router.js defines no signals route. The route 42 opens for a
    selected dynamic signal is buildTopicHash(signalId, region) from explore.jsx,
    so that grammar is reproduced here byte for byte on the staging origin. A
    single-market scope routes to that market; any wider scope routes to all.
    """
    if not isinstance(signal_id, str) or _SIGNAL_ID.fullmatch(signal_id) is None:
        raise ValueError("deep_link_unavailable")
    markets = tuple(market_scope)
    if not markets or any(market not in CANONICAL_MARKETS for market in markets):
        raise ValueError("deep_link_unavailable")
    region = markets[0] if len(markets) == 1 else "all"
    if region not in _ROUTE_REGIONS:
        raise ValueError("deep_link_unavailable")
    return f"{STAGING_ORIGIN}/#/topic/{signal_id}?region={region}"


def _deep_link_continuity(line: Mapping[str, object]) -> str:
    """The line's own deep link must be the one route 42 serves for its signal."""
    deep_link = signal_deep_link(str(line["signal_id"]), tuple(line["market_scope"]))
    if line["deep_link"] != deep_link:
        raise ValueError("deep_link_unavailable")
    return deep_link


def render_watched_line(
    current: dict,
    reasons: tuple[str, ...],
    *,
    release: OpenIntelligenceRunReceipt,
    today: date,
) -> dict:
    """Return one unsent PULSE line artifact for a released run, or refuse."""
    line = _pulse_line(current)
    named = _reasons(reasons)
    if line["client_scope_id"] == QA_CLIENT_SCOPE_ID:
        raise ValueError("qa_identity_refused")
    _release_proof(line, release, today)
    deep_link = _deep_link_continuity(line)
    return {
        "delivery": "unsent",
        "deep_link": deep_link,
        "reasons": named,
        "line": line,
        "release_run_id": release.run_id,
    }


def render_watched_lines(
    entries: Sequence[tuple[dict, tuple[str, ...]]],
    *,
    cap: int,
    release: OpenIntelligenceRunReceipt,
    today: date,
) -> tuple[dict, ...]:
    """Render a bounded batch; the volume cap is checked before any line is built."""
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ValueError("watched_cap_invalid")
    batch = tuple(entries)
    if len(batch) > cap:
        raise ValueError("watched_cap_exceeded")
    return tuple(
        render_watched_line(current, reasons, release=release, today=today)
        for current, reasons in batch
    )
