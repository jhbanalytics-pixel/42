"""Daily cross-trend summary orchestrator.

Second Gemini pass that reads across the per-topic briefs produced by
``generate_briefs`` and writes one cohesive "Today in SSA" narrative
to ``daily_summary``. The summary lands at the top of the email digest
so stakeholders see the through-line before drilling into the per-topic
cards.

Cost
====

One Gemini call per day at roughly 2K input + 600 output tokens.
Pricing: ~$0.0021 per call, ~$0.063 per month. Inside the GCP envelope.

Failure mode
============

Wrapped in fail-loud at the SDK boundary; the caller decides whether
to retry. Persistence failures log and continue so the email still
ships even if BQ rejects the row.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
from google.cloud import bigquery as bq

from src.analysis.gemini_client import BriefResponse, GeminiClient
from src.analysis.generate_briefs import _should_retry_empty
from src.analysis.prompts.daily_summary import (
    RESPONSE_SCHEMA,
    SYSTEM_INSTRUCTION,
    build_summary_prompt,
)
from src.utils.bigquery import get_client, get_dataset, insert_dataframe
from src.utils.gemini_usage import (
    UsageSink,
    persist_gemini_usage,
    record_usage,
    usage_rows,
)
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)


@dataclass
class DailySummary:
    """One day's cross-trend executive summary."""

    trend_date: date
    summary_text: str
    through_line: str
    call_to_action: str
    markets: list[str]
    brief_count: int
    key_topics: list[str]
    rising_topics: list[str]
    prompt_tokens: int
    completion_tokens: int
    model: str
    # Seed score topline (Jo, 22 Jun): optional follow-vs-seed call naming the
    # topic best positioned to drive Nanobanana/Lyria usage. Empty when none.
    seed_recommend: str = ""

    def to_bq_row(self) -> dict[str, Any]:
        """Map to a row dict matching the daily_summary BQ schema."""
        return {
            "summary_id": str(uuid.uuid4()),
            "trend_date": self.trend_date,
            "summary_text": self.summary_text,
            "through_line": self.through_line,
            "call_to_action": self.call_to_action,
            "markets": list(self.markets),
            "brief_count": int(self.brief_count),
            "key_topics": list(self.key_topics),
            "rising_topics": list(self.rising_topics),
            "gemini_model": self.model,
            "prompt_tokens": int(self.prompt_tokens),
            "completion_tokens": int(self.completion_tokens),
            "seed_recommend": self.seed_recommend,
            "generated_at": datetime.now(UTC),
        }

    def to_email_dict(self) -> dict[str, Any]:
        """Plain-dict view for the email layer (no dataclass dependency)."""
        return {
            "summary_text": self.summary_text,
            "through_line": self.through_line,
            "call_to_action": self.call_to_action,
            "key_topics": list(self.key_topics),
            "rising_topics": list(self.rising_topics),
            "seed_recommend": self.seed_recommend,
        }


def _briefs_dict_to_input(
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]],
    trend_scores_by_topic: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Shape the briefs dict into the prompt input list.

    ``trend_scores_by_topic`` carries the velocity_score + item_count signals
    the summary prompt expects. Defaults to empty so callers can pass it in
    when they have it (orchestrator path) or skip when they don't (test
    path); the prompt treats missing values as zero gracefully.
    """
    out: list[dict[str, Any]] = []
    scores = trend_scores_by_topic or {}
    for (market, topic), brief in briefs_by_topic.items():
        score_row = scores.get((market, topic), {})
        out.append(
            {
                "market": market,
                "topic_group": topic,
                "status_tag": brief.get("status_tag") or "Rising",
                "trend_score": float(score_row.get("trend_score") or 0.0),
                "velocity_score": float(score_row.get("velocity_score") or 0.0),
                "seed_score": float(score_row.get("seed_score") or 0.0),
                "item_count": int(score_row.get("item_count") or 0),
                "description_rationale": brief.get("description_rationale") or "",
            }
        )
    # Sort: Key topics first, then Rising. Within each group, order by
    # descending trend_score with velocity_score as the tiebreaker. Keeps
    # the prompt's narrative priority aligned with the brief's tier semantics.
    out.sort(
        key=lambda r: (
            0 if r["status_tag"] == "Key" else 1,
            -float(r["trend_score"]),
            -float(r["velocity_score"]),
        )
    )
    return out


def _existing_summary_for(client: bq.Client, dataset: str, trend_date: date) -> bool:
    """Idempotency check: True when a summary row already exists for trend_date.

    Mirrors generate_briefs' existing-keys guard so reruns of the cron
    do not double-spend on the cross-trend Gemini call.
    """
    sql = f"""
    SELECT COUNT(*) AS n
    FROM `{client.project}.{dataset}.daily_summary`
    WHERE trend_date = @trend_date
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
    )
    rows = list(client.query(sql, job_config=job_config).result())
    return bool(rows and rows[0].n)


def _to_summary(
    trend_date: date,
    response: BriefResponse,
    markets: list[str],
    brief_count: int,
) -> DailySummary:
    parsed = response.parsed
    return DailySummary(
        trend_date=trend_date,
        summary_text=str(parsed.get("summary_text") or ""),
        through_line=str(parsed.get("through_line") or ""),
        call_to_action=str(parsed.get("call_to_action") or ""),
        markets=list(markets),
        brief_count=int(brief_count),
        key_topics=[str(x) for x in (parsed.get("key_topics") or [])][:5],
        rising_topics=[str(x) for x in (parsed.get("rising_topics") or [])][:5],
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        model=response.model,
        seed_recommend=str(parsed.get("seed_recommend") or "").strip(),
    )


def _is_empty_summary(summary: DailySummary) -> bool:
    """Return True when the summary's narrative fields are empty.

    Schema validation can pass with all-empty strings (Gemini occasionally
    emits the minimum schema-valid response when it hits the safety filter
    or judges the signal as too thin). An empty summary at the top of the
    email is worse than no summary block at all, so callers should retry
    or skip persistence in that case.
    """
    return not (
        summary.summary_text.strip()
        and summary.through_line.strip()
        and summary.call_to_action.strip()
    )


def _cta_names_rising(cta: str, rising_topics: list[str]) -> bool:
    """Return True when call_to_action references any rising topic.

    Match semantics are byte-identical to the accuracy watchdog's
    ``check_action_pick_in_rising`` (scripts/accuracy_watchdog.py): a
    rising token ``market/topic_group`` matches if the token OR its
    slash->space form appears as a substring of ``cta.lower()``. The
    predicate is duplicated here on purpose (not imported from the
    watchdog script) so the generator and the gate share one definition
    of the rule and a single test can pin them together.
    """
    cta_l = cta.lower()
    rising = [str(t).lower() for t in rising_topics]
    return any(t in cta_l or t.replace("/", " ") in cta_l for t in rising)


_RETRY_INSTRUCTION_SUFFIX = (
    "\n\nPRODUCE OUTPUT NOW. Every required field MUST be a non-empty string "
    "or non-empty list. summary_text needs at least 4 sentences naming "
    "specific topics and markets. through_line is one sentence capturing "
    "the cross-cutting story. call_to_action is one to two sentences naming "
    "what the team does this week. Do not return empty strings or empty "
    "arrays for any field."
)


def _topic_group_to_cultural_driver(topic_group: str) -> str:
    """Map a topic_group prefix to the cultural driver phrase.

    Used by the deterministic fallback to inject brand-pivot context
    into call_to_action without depending on Gemini. Each phrase is a
    single sentence the strategist would write themselves: names the
    cause, frames the audience, suggests the brand pivot. Phrases are
    intentionally generic-but-grounded so they read true on most days
    without fabricating specifics that the briefs do not support.
    """
    prefix_map: dict[str, str] = {
        "music_amapiano": (
            "Township-club aesthetic remains the cross-cutting Mzansi sound; "
            "creative briefs should anchor in real venues and dancers, not "
            "stock club imagery."
        ),
        "music_afrobeats": (
            "Afrobeats is the continental export carrying Lagos energy "
            "globally; creative should respect the artists, not generic "
            "Afro-fusion shorthand."
        ),
        "music_gengetone": (
            "Gengetone and Arbantone are the Nairobi street-pop reset; "
            "creative needs the actual matatu / kibanda texture, not "
            "imported Amapiano cues."
        ),
        "music_genre": (
            "Music remains the cultural through-line; creative briefs "
            "should use the market-native genre, not cross-border defaults."
        ),
        "politics_": (
            "Political beats drive volume but reward awareness, not "
            "adjacency; brand messaging stays at arm's length."
        ),
        "fintech_mpesa": (
            "Daily-hustle finance content drives M-Pesa engagement; "
            "creative should anchor in real small-business context, "
            "not aspirational fintech copy."
        ),
        "economy_": (
            "Economic pressure is reshaping consumer aspirations; "
            "messaging needs to pivot from luxury to resilience."
        ),
        "diaspora_": (
            "Diaspora-reality content is replacing the 'leave home' "
            "cliche; creative should sit with the return narrative, not "
            "the departure one."
        ),
        "fashion_": (
            "Local fashion / market-day aesthetics carry the visual "
            "trend; creative should source actual local imagery."
        ),
        "food_": (
            "Food and ritual content cuts across audiences; creative "
            "should anchor in named dishes and locations."
        ),
        "infra_": (
            "Infrastructure pressure (load-shedding, water, transport) "
            "shapes the everyday; messaging should acknowledge the "
            "context, not bypass it."
        ),
        "education_": (
            "Education milestones drive seasonal volume; creative "
            "should match the moment (matric, NSFAS, exams) precisely."
        ),
        "sports_": (
            "Sports content carries pan-market reach; creative should "
            "embrace the live-moment energy, not retrospective summaries."
        ),
    }

    # Exact key match first.
    if topic_group in prefix_map:
        return prefix_map[topic_group]
    # Prefix match (e.g. politics_tinubu matches politics_).
    for prefix, driver in prefix_map.items():
        if prefix.endswith("_") and topic_group.startswith(prefix):
            return driver
    # Generic fallback if topic_group does not match any known cultural
    # driver. Avoids hardcoding a fake driver for unfamiliar topics.
    return (
        "The signal points to a real cultural movement; creative should "
        "anchor in the cards' specifics rather than write to the topic name."
    )


def _ranked_rising_topics(
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]],
    scores: dict[tuple[str, str], dict[str, Any]],
) -> list[tuple[str, str, float]]:
    """Rising (market, topic, velocity) tuples ranked best-first for a CTA pick.

    Positive-velocity Rising topics first (descending velocity), then the
    remaining Rising topics (also descending velocity). Empty when no Rising
    topic is present. Shared by the deterministic fallback and the LLM-path
    re-anchor so both agree on which rising topic the CTA should name.
    """
    rising = [
        (m, t, float(scores.get((m, t), {}).get("velocity_score") or 0.0))
        for (m, t), b in briefs_by_topic.items()
        if str(b.get("status_tag") or "") == "Rising"
    ]
    positive = sorted((r for r in rising if r[2] > 0.0), key=lambda r: r[2], reverse=True)
    nonpositive = sorted((r for r in rising if r[2] <= 0.0), key=lambda r: r[2], reverse=True)
    return positive + nonpositive


def _drop_stalled_rising(
    rising_topics: list[str],
    scores: dict[tuple[str, str], dict[str, Any]],
) -> list[str]:
    """Drop rising tokens that have a score row showing flat or negative velocity.

    The CTA is grounded against the persisted rising_topics list, so a stalled
    topic left in that list lets the CTA name a trend that is not moving. The
    prompt prose already asks Gemini to prefer accelerating topics; this is the
    code gate that enforces it. Only tokens that HAVE a score row with
    velocity_score <= 0 are dropped. A token with no matching score row is left
    in place: it is not provably stalled, and the downstream CTA-grounding step
    already validates such tokens against the briefs and re-anchors if needed.
    Order is preserved.
    """
    kept: list[str] = []
    for token in rising_topics:
        m, sep, t = str(token).partition("/")
        score_row = scores.get((m, t)) if sep and m and t else None
        if score_row is not None and float(score_row.get("velocity_score") or 0.0) <= 0.0:
            continue
        kept.append(token)
    return kept


def _deterministic_fallback_summary(
    trend_date: date,
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]],
    trend_scores_by_topic: dict[tuple[str, str], dict[str, Any]] | None,
    markets: list[str],
    last_response: BriefResponse | None,
) -> DailySummary:
    """Backstop summary built without Gemini when both LLM attempts return empty.

    Better than blocking the email's top block forever waiting for Gemini
    to cooperate. Uses the per-topic briefs + scoring signals already in
    BigQuery to produce a factual, conservative narrative. The output is
    intentionally short and signal-light so it reads as a backstop rather
    than the polished daily voice; the email card styling is unchanged.

    Token counts on the persisted row carry the actual Gemini spend
    (both wasted attempts) so the cost ledger stays truthful.
    """
    scores = trend_scores_by_topic or {}

    # Per-market top topic by trend_score.
    top_per_market: dict[str, tuple[str, float, float, str]] = {}
    for (market, topic), brief in briefs_by_topic.items():
        s = scores.get((market, topic), {})
        ts = float(s.get("trend_score") or 0.0)
        vs = float(s.get("velocity_score") or 0.0)
        tag = str(brief.get("status_tag") or "Rising")
        cur = top_per_market.get(market)
        if cur is None or ts > cur[1]:
            top_per_market[market] = (topic, ts, vs, tag)

    market_lines: list[str] = []
    for m in sorted(top_per_market):
        topic, ts, vs, tag = top_per_market[m]
        market_lines.append(
            f"{m.upper()} leads with {topic} at {ts:.2f} ({tag}, velocity {vs:.2f})"
        )

    # Cultural-strategist tone: avoid database-readout phrasing
    # ("Cross-trend signal sourced from N briefs", "velocity 0.24") and
    # avoid leaking that this is a fallback. Reads like a short morning
    # note built from real signals, not as an error log.
    market_phrases: list[str] = []
    for m in sorted(top_per_market):
        topic, _ts, _vs, tag = top_per_market[m]
        market_label = {"za": "Mzansi", "ng": "Naija", "ke": "Kenya"}.get(m, m.upper())
        if tag == "Key":
            market_phrases.append(f"{market_label} centres on {topic}")
        else:
            market_phrases.append(f"the {market_label} side-current is {topic}")

    # Pick a per-day rotation seed from the trend_date so the fallback's
    # template choice rotates across days even when the topic mix is
    # similar. Without this rotation the through-line read identical
    # multiple days running (Thapelo flagged this 7 May 2026: same opener
    # daily means stakeholders pattern-match and stop reading).
    day_index = trend_date.toordinal()

    # Today's all-market top topic + its delta-vs-yesterday velocity
    # signal. Used to inject TODAY-specific names into the narrative so
    # the fallback never produces identical wording two days running.
    if top_per_market:
        all_top = max(top_per_market.values(), key=lambda v: v[1])
        all_top_topic = all_top[0]
    else:
        all_top_topic = ""

    # summary_text rotation. Five templates that all carry the same
    # market_phrases payload but differ in framing voice.
    market_phrase_str = "; ".join(market_phrases) if market_phrases else ""
    summary_templates = [
        # Strategy stand-up shape.
        (
            f"Cross-market read this morning: {market_phrase_str}. "
            "Treat the per-topic cards below as the working evidence; "
            "today's opening question for creative is whether the same "
            "story is still alive on the feed tomorrow."
        ),
        # Mood-of-the-day shape.
        (
            f"The cultural temperature today: {market_phrase_str}. "
            "Each of those topics carries a distinct creative brief in "
            "the cards below; lean into the named cultural anchors, "
            "not the topic labels."
        ),
        # Operator-handover shape.
        (
            f"Brief snapshot: {market_phrase_str}. The detail sits in "
            "the per-topic cards. Ask the cards what they want creative "
            "to do, then check whether your current campaign is rowing "
            "with that current or against it."
        ),
        # Editor's-note shape.
        (
            f"Today's read across the three markets: {market_phrase_str}. "
            "Hold the cards at arm's length; the value sits in the "
            "specific creators and posts they cite, not in the topic "
            "label itself."
        ),
        # Producer's shape (most factual, no framing voice).
        (
            f"Today on the feed: {market_phrase_str}. The per-topic "
            "cards carry the creator handles, paste-ready prompts, "
            "and source posts you would brief from."
        ),
    ]
    if market_phrase_str:
        summary_text = summary_templates[day_index % len(summary_templates)]
    else:
        # Quiet-day fallback also rotates so consecutive quiet days
        # still vary.
        quiet_templates = [
            "Quiet cultural day across the three markets. Read the "
            "per-topic cards below for the texture. Treat this as a "
            "watching brief and reconvene tomorrow.",
            "Light signal across all markets today. The cards below "
            "carry what is there; nothing on the list yet justifies "
            "a campaign pivot. Reconvene tomorrow.",
            "Thin day on the feed. Check the cards below for any "
            "topic worth tracking into next week, otherwise treat as "
            "a watching brief.",
        ]
        summary_text = quiet_templates[day_index % len(quiet_templates)]

    # Through-line: cultural observation, NOT a data point. Detect
    # category dominance from topic_group prefixes, then pick from
    # multiple phrasings rotated by trend_date so consecutive days
    # don't read identical when the dominance pattern persists.
    music_count = sum(1 for (_, t) in briefs_by_topic if t.startswith("music_"))
    politics_count = sum(1 for (_, t) in briefs_by_topic if t.startswith("politics_"))
    economy_count = sum(
        1 for (_, t) in briefs_by_topic if t.startswith(("economy_", "fintech_", "diaspora_"))
    )

    music_phrasings = [
        "Music is doing the cross-market lifting today; cultural "
        "movement runs through clubs, kasi streets, and the feed.",
        "Sound carries the day across the three markets; the genre "
        "specifics differ, but the energy travels.",
        "Music holds the centre of the conversation; brands that "
        "sponsor or score get carried with it.",
        "The shared current today is rhythm, not message; creative "
        "direction lives in the audio brief, not the copy.",
    ]
    music_politics_phrasings = [
        "Music remains the dominant cross-market unifier; politics "
        "drives the divide between markets.",
        "Sound binds today; politics splits. The cross-market story "
        "lives in the music cards; the local-market story in politics.",
        "Two parallel currents: music as the connective tissue, "
        "politics as the friction. Brand work belongs with the first.",
    ]
    economy_phrasings = [
        "Hustle-survival content is replacing aspirational copy across "
        "the markets; the audience wants resilience, not luxury.",
        "Daily-hustle finance content is doing the lifting; copy that "
        "speaks to the daily grind outperforms aspirational framing.",
        "Resilience reads as the day's through-line; messaging that "
        "acknowledges the squeeze beats messaging that ignores it.",
        "The economy is talking back through every market today; "
        "brand work that pretends otherwise lands flat.",
    ]
    politics_phrasings = [
        "Politics and protest set today's cultural tone; brand "
        "messaging needs awareness, not adjacency.",
        "Political discourse is loud across every market today; brand "
        "voices stay at arm's length, not arm-in-arm.",
        "The political beat is the loudest signal today; creative "
        "should hold context awareness without claiming ownership.",
    ]
    top_topic_phrasings = [
        f"{all_top_topic} leads the cross-market conversation; "
        "see per-topic cards for the cultural texture.",
        f"{all_top_topic} sets the pace today; the rest of the cards "
        "show whether the rest of the markets are following or diverging.",
        f"Today's cross-market lead is {all_top_topic}; treat its card "
        "as the headline brief and read outward from there.",
    ]

    if music_count >= 4 and politics_count >= 2:
        through_line = music_politics_phrasings[day_index % len(music_politics_phrasings)]
    elif music_count >= max(politics_count, economy_count, 1) + 1:
        through_line = music_phrasings[day_index % len(music_phrasings)]
    elif economy_count >= max(music_count, politics_count, 1):
        through_line = economy_phrasings[day_index % len(economy_phrasings)]
    elif politics_count >= max(music_count, economy_count, 1):
        through_line = politics_phrasings[day_index % len(politics_phrasings)]
    elif top_per_market:
        through_line = top_topic_phrasings[day_index % len(top_topic_phrasings)]
    else:
        through_line = "Quiet cross-market day; treat as a watching brief and reconvene tomorrow."

    # Action: name a brand-pivot implication, not just a topic to track.
    # Picker prefers Rising topics with POSITIVE velocity (genuinely
    # accelerating); falls back to highest absolute velocity if none
    # positive; final fallback is highest-trend_score Key topic. The
    # earlier picker sometimes pointed creative at a deflating Rising
    # topic (Thapelo flagged 7 May 2026: "Track za/politics_crises"
    # while velocity was -0.024).
    ranked_rising = _ranked_rising_topics(briefs_by_topic, scores)
    rising_positive = [r for r in ranked_rising if r[2] > 0.0]

    if rising_positive:
        rm, rt, _rv = rising_positive[0]
        driver = _topic_group_to_cultural_driver(rt)
        call_to_action = (
            f"Track {rm}/{rt} this week. {driver} Brands working in this "
            "space need to lean into the cultural shift, not generic copy."
        )
    elif top_per_market:
        all_top = max(top_per_market.values(), key=lambda v: v[1])
        topic = all_top[0]
        driver = _topic_group_to_cultural_driver(topic)
        call_to_action = (
            f"Lean creative into {topic} this week. {driver} The brief is to "
            "name the cultural moment, not chase the trend."
        )
        # No Rising topic is accelerating, so creative leans on the top Key
        # topic above. But the persisted rising_topics list is still populated
        # from the Rising briefs, and the accuracy watchdog
        # (check_action_pick_in_rising) checks the CTA against THAT list. Name
        # the top rising token with a softer "watch" verb so the CTA satisfies
        # the watchdog without telling creative to chase a flat or deflating
        # trend.
        if ranked_rising:
            wm, wt, _wv = ranked_rising[0]
            call_to_action += f" Watch {wm}/{wt} this week for whether it picks up."
    else:
        call_to_action = (
            "No high-confidence direction today. Review per-topic briefs and "
            "reconvene tomorrow before committing creative spend."
        )

    # Top 3 by score for key_topics, top 3 Rising by velocity for rising_topics.
    sorted_by_score = sorted(
        briefs_by_topic.keys(),
        key=lambda k: float(scores.get(k, {}).get("trend_score") or 0.0),
        reverse=True,
    )
    key_topics = [f"{m}/{t}" for (m, t) in sorted_by_score[:5]]
    sorted_rising = sorted(
        [k for k, b in briefs_by_topic.items() if str(b.get("status_tag") or "") == "Rising"],
        key=lambda k: float(scores.get(k, {}).get("velocity_score") or 0.0),
        reverse=True,
    )
    rising_topics = [f"{m}/{t}" for (m, t) in sorted_rising[:5]]

    return DailySummary(
        trend_date=trend_date,
        summary_text=summary_text,
        through_line=through_line,
        call_to_action=call_to_action,
        markets=list(markets),
        brief_count=len(briefs_by_topic),
        key_topics=key_topics,
        rising_topics=rising_topics,
        # Carry the wasted Gemini spend on the row so the daily cost
        # ledger stays truthful (we did pay for two attempts before
        # falling back).
        prompt_tokens=last_response.prompt_tokens if last_response else 0,
        completion_tokens=last_response.completion_tokens if last_response else 0,
        model=(last_response.model if last_response else "deterministic-fallback"),
    )


def _existing_non_empty_summary_for(client: bq.Client, dataset: str, trend_date: date) -> bool:
    """Idempotency check that ignores rows with empty narrative fields.

    The original idempotency guard (``_existing_summary_for``) treats any
    row for the date as "done", which means an empty-summary row from a
    failed first pass blocks a legitimate regen on the next cron. Use the
    non-empty check from ``generate_daily_summary`` so re-runs can self-heal
    a previously empty row.
    """
    sql = f"""
    SELECT COUNT(*) AS n
    FROM `{client.project}.{dataset}.daily_summary`
    WHERE trend_date = @trend_date
      AND COALESCE(summary_text, '') != ''
      AND COALESCE(through_line, '') != ''
      AND COALESCE(call_to_action, '') != ''
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
    )
    rows = list(client.query(sql, job_config=job_config).result())
    return bool(rows and rows[0].n)


def _delete_all_summary_for(client: bq.Client, dataset: str, trend_date: date) -> None:
    """Remove ALL daily_summary rows for trend_date so a force-regen INSERT
    overwrites cleanly instead of producing a duplicate row.

    Used by the persist branch when force=True is passed by the caller.
    Without this, force-regen on top of an existing populated row (e.g.
    from a prior fallback synthesis) leaves the old row in place and adds
    a new row, so two rows live at the same trend_date and Looker queries
    that don't ORDER BY generated_at DESC may surface stale text.
    """
    sql = f"""
    DELETE FROM `{client.project}.{dataset}.daily_summary`
    WHERE trend_date = @trend_date
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
    )
    client.query(sql, job_config=job_config).result()


def _delete_empty_summary_for(client: bq.Client, dataset: str, trend_date: date) -> None:
    """Remove the empty placeholder row so the regen INSERT does not duplicate.

    BigQuery DML is only used here, scoped to a single date and only when
    every narrative field is empty, so the destructive surface is minimal.
    """
    sql = f"""
    DELETE FROM `{client.project}.{dataset}.daily_summary`
    WHERE trend_date = @trend_date
      AND COALESCE(summary_text, '') = ''
      AND COALESCE(through_line, '') = ''
      AND COALESCE(call_to_action, '') = ''
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
    )
    client.query(sql, job_config=job_config).result()


def _fetch_brand24_insights(
    client: bq.Client, dataset: str, trend_date: date
) -> list[dict[str, Any]]:
    """Pull Brand24's ai-insights rows collected on trend_date, one dict per market.

    The brand24 connector lands each ai-insights call as four raw_content rows
    (content_type='brand24_ai_insight', platform='aggregate'), one per narrative
    field, with query_term shaped '<project_id>:<field>' and text carrying the
    field value. This re-assembles them into a per-market dict
    {market, headline, trends, insights, recommendations} for the summary prompt.

    Returns an empty list when the flag is off, the rows are absent, or the query
    fails, so the summary degrades to its current no-grounding behaviour rather
    than blocking on an optional enrichment.
    """
    sql = f"""
    SELECT market, query_term, text
    FROM `{client.project}.{dataset}.raw_content`
    WHERE DATE(collected_at) = @trend_date
      AND content_type = 'brand24_ai_insight'
    """
    job_config = bq.QueryJobConfig(
        query_parameters=[bq.ScalarQueryParameter("trend_date", "DATE", trend_date)]
    )
    try:
        rows = list(client.query(sql, job_config=job_config).result())
    except Exception:
        # Legacy readback: Brand24 was retired on 21 Aug 2026; rows exist only
        # for dates before that.
        logger.warning(
            "brand24 ai-insights fetch failed for %s, summary runs ungrounded", trend_date
        )
        return []

    by_market: dict[str, dict[str, Any]] = {}
    valid_fields = {"headline", "trends", "insights", "recommendations"}
    for r in rows:
        market = str(r.market or "").strip().lower()
        field = str(r.query_term or "").split(":", 1)[-1].strip().lower()
        if not market or field not in valid_fields:
            continue
        by_market.setdefault(market, {"market": market})[field] = str(r.text or "")

    order = {"za": 0, "ng": 1, "ke": 2}
    return [by_market[m] for m in sorted(by_market, key=lambda m: order.get(m, 9))]


# This stage's name in the shared gemini_usage ledger. market is NULL on its
# rows: daily_summary is one cross-market call, and its table carries a
# ``markets`` array rather than one market.
USAGE_CONSUMER = "daily_summary"


def generate_daily_summary(
    *,
    trend_date: date,
    briefs_by_topic: dict[tuple[str, str], dict[str, Any]],
    trend_scores_by_topic: dict[tuple[str, str], dict[str, Any]] | None = None,
    gemini_client: GeminiClient | None = None,
    bq_client: bq.Client | None = None,
    dataset: str | None = None,
    persist: bool = True,
    force: bool = False,
    usage_sink: UsageSink | None = None,
    row_sink: Callable[[list[dict[str, Any]]], int] | None = None,
) -> DailySummary | None:
    """Generate one cross-trend summary for the date and persist it.

    Returns None when there are no briefs to summarise (caller should
    skip the email-top section in that case). Returns a DailySummary
    otherwise.

    Idempotent by default: if a summary row already exists for
    ``trend_date``, this function returns None unless ``force=True``.
    """
    if not briefs_by_topic:
        logger.info("generate_daily_summary: no briefs for %s, skipping", trend_date)
        return None

    bq_client = bq_client or get_client()
    dataset = dataset or get_dataset()
    gemini_client = gemini_client or GeminiClient()

    if not force and _existing_non_empty_summary_for(bq_client, dataset, trend_date):
        logger.info(
            "generate_daily_summary: non-empty row already exists for %s, skipping",
            trend_date,
        )
        return None

    usage_tally: dict = {}
    sink = usage_sink or persist_gemini_usage

    inputs = _briefs_dict_to_input(briefs_by_topic, trend_scores_by_topic)
    brand24_insights = _fetch_brand24_insights(bq_client, dataset, trend_date)
    prompt = build_summary_prompt(
        trend_date=trend_date.isoformat(), briefs=inputs, brand24_insights=brand24_insights
    )
    # Both attempts bill whether or not the narrative shipped, so the ledger
    # flush is in a finally.
    try:
        response = gemini_client.generate_brief(
            prompt=prompt,
            response_schema=RESPONSE_SCHEMA,
            # Slightly higher temperature than per-topic briefs to encourage
            # narrative variation across days.
            temperature=0.5,
            # 8192 (was 2048) for Gemini 3.x thinking-model headroom: the bounded
            # thinking budget bills against output, and a tight ceiling starves the
            # visible narrative to empty (the regression-3 empty-summary failure).
            max_output_tokens=8192,
            system_instruction=SYSTEM_INSTRUCTION,
        )
        record_usage(usage_tally, None, response)

        markets = sorted({m for (m, _) in briefs_by_topic})
        summary = _to_summary(trend_date, response, markets, brief_count=len(briefs_by_topic))
        used_fallback = False

        # Retry once if the first call returned an empty narrative. Mirrors
        # generate_briefs._is_empty_brief + retry path; cost-leak guard on
        # raw_text shape avoids burning a second paid call on safety-filter
        # refusal stubs.
        if _is_empty_summary(summary):
            if not _should_retry_empty(response):
                logger.warning(
                    "generate_daily_summary: empty response for %s and raw_text "
                    "is not JSON-shaped (likely a safety-filter refusal); skipping "
                    "retry to avoid wasted spend",
                    trend_date,
                )
            else:
                logger.warning(
                    "generate_daily_summary: empty response for %s on first attempt "
                    "(prompt_tokens=%d completion_tokens=%d), retrying with stricter "
                    "prompt and higher temperature",
                    trend_date,
                    response.prompt_tokens,
                    response.completion_tokens,
                )
                retry_response = gemini_client.generate_brief(
                    prompt=prompt + _RETRY_INSTRUCTION_SUFFIX,
                    response_schema=RESPONSE_SCHEMA,
                    temperature=0.7,
                    max_output_tokens=8192,
                    system_instruction=SYSTEM_INSTRUCTION,
                )
                # The first, empty call billed too; both land in the ledger.
                record_usage(usage_tally, None, retry_response)
                retry_summary = _to_summary(
                    trend_date, retry_response, markets, brief_count=len(briefs_by_topic)
                )
                # Keep the spend ledger truthful: both calls were billed.
                retry_summary.prompt_tokens += summary.prompt_tokens
                retry_summary.completion_tokens += summary.completion_tokens
                summary = retry_summary

        if _is_empty_summary(summary):
            logger.warning(
                "generate_daily_summary: summary for %s still empty after retry; "
                "synthesizing deterministic fallback from briefs + trend_scores "
                "so the email digest top block does not render blank",
                trend_date,
            )
            # last_response carries the most recent Gemini call's tokens so the
            # spend ledger on the persisted row reflects what we actually paid.
            last_response = locals().get("retry_response", response)
            summary = _deterministic_fallback_summary(
                trend_date=trend_date,
                briefs_by_topic=briefs_by_topic,
                trend_scores_by_topic=trend_scores_by_topic,
                markets=markets,
                last_response=last_response,
            )
            used_fallback = True
            # Sum BOTH Gemini attempts into the fallback's spend ledger so we
            # do not lose visibility on the wasted calls.
            retry_resp_for_tokens = locals().get("retry_response")
            summary.prompt_tokens = response.prompt_tokens + (
                retry_resp_for_tokens.prompt_tokens if retry_resp_for_tokens else 0
            )
            summary.completion_tokens = response.completion_tokens + (
                retry_resp_for_tokens.completion_tokens if retry_resp_for_tokens else 0
            )

        # Velocity gate. The prompt prose asks Gemini to pick an accelerating
        # rising topic, but nothing enforced it, so the CTA could name a Rising
        # topic sitting at zero or negative velocity (a stalled trend). Drop the
        # flat and deflating entries from the persisted rising_topics so the CTA
        # grounding below can only land on a topic that is genuinely moving. The
        # fallback path already constrains its pick to positive velocity, so only
        # the Gemini path needs this.
        if not used_fallback and summary.rising_topics:
            gated_rising = _drop_stalled_rising(summary.rising_topics, trend_scores_by_topic or {})
            if gated_rising != summary.rising_topics:
                logger.warning(
                    "generate_daily_summary: dropped %d stalled (velocity <= 0) "
                    "rising topic(s) for %s before CTA grounding",
                    len(summary.rising_topics) - len(gated_rising),
                    trend_date,
                )
                summary.rising_topics = gated_rising

        # Ground the CTA in the engine's own rising list. The Gemini path is
        # loose: the model can name a topic that is not in that day's
        # rising_topics, which trips the accuracy watchdog's
        # action_pick_in_rising check and reads as an ungrounded
        # recommendation at the top of the digest (the most visible line in
        # the email). The deterministic fallback already constrains its pick
        # to Rising, so only validate the LLM path. When the model strays
        # off-list, do NOT regenerate (avoids a paid call and a possible
        # second miss); rewrite the CTA on a validated rising pick using the
        # same fallback machinery, matching the watchdog's rule exactly.
        if (
            not used_fallback
            and summary.rising_topics
            and not _cta_names_rising(summary.call_to_action, summary.rising_topics)
        ):
            # summary.rising_topics on this path is raw Gemini output, never
            # validated against the briefs. Anchoring the digest's most visible
            # line on it blindly can (a) name a topic Gemini hallucinated that no
            # brief supports, or (b) hit a malformed token with no slash, which
            # partitions to an empty topic and prints "Track market/ this week".
            # Pick the first rising entry that BOTH parses (slash present, both
            # parts non-empty) AND exists in the briefs (or scores), so the CTA
            # anchors on a real, persisted rising topic.
            valid_keys = set(briefs_by_topic) | set(trend_scores_by_topic or {})
            rm = rt = ""
            for token in summary.rising_topics:
                cand_m, sep, cand_t = str(token).partition("/")
                if sep and cand_m and cand_t and (cand_m, cand_t) in valid_keys:
                    rm, rt = cand_m, cand_t
                    break

            if not rt:
                # No persisted rising token parses-and-exists. Fall back to the
                # deterministic Rising picker over the briefs (the same ranking
                # the fallback path uses), constrained to POSITIVE velocity so the
                # re-anchored CTA never names a stalled trend, then add the chosen
                # token to the persisted rising_topics so the watchdog's check on
                # the stored row still passes. If the briefs carry no accelerating
                # Rising topic at all, leave the CTA untouched: a malformed rewrite
                # would be worse than the model's original line.
                ranked = _ranked_rising_topics(briefs_by_topic, trend_scores_by_topic or {})
                ranked = [r for r in ranked if r[2] > 0.0]
                if ranked:
                    rm, rt, _rv = ranked[0]
                    anchor = f"{rm}/{rt}"
                    if anchor not in summary.rising_topics:
                        summary.rising_topics = [anchor, *summary.rising_topics][:5]

            if rt:
                driver = _topic_group_to_cultural_driver(rt)
                logger.warning(
                    "generate_daily_summary: Gemini CTA for %s named a topic outside "
                    "the rising list; re-anchoring on validated rising pick %s/%s",
                    trend_date,
                    rm,
                    rt,
                )
                summary.call_to_action = (
                    f"Track {rm}/{rt} this week. {driver} Brands working in this "
                    "space need to lean into the cultural shift, not generic copy."
                )
            else:
                logger.warning(
                    "generate_daily_summary: Gemini CTA for %s is off the rising "
                    "list but no validated rising topic exists to re-anchor on; "
                    "leaving the CTA as written",
                    trend_date,
                )

        if persist:
            # Pre-INSERT cleanup. Two distinct cases:
            # - force=True: caller explicitly asked for regen. Clear ANY
            #   existing row for the date (empty or populated) so the new
            #   row overwrites cleanly. Without this, a force-regen on top
            #   of a populated row leaves both rows in place and Looker can
            #   render either one depending on query ordering. This is the
            #   path the regen workflow uses.
            # - force=False (cron path): clear only EMPTY placeholder rows.
            #   A populated row hitting this path means a non-empty row
            #   already existed and the entry guard should have skipped us;
            #   if we still got here we should not be destroying that row.
            # A swallowed DELETE failure used to fall through to the INSERT and
            # leave two rows for the same trend_date; the watchdog reads LIMIT 1
            # with no ORDER BY so its verdict went nondeterministic. On force the
            # cleanup is load-bearing, so abort the INSERT when it fails. On the
            # cron path, re-check for a surviving row and skip the INSERT if one
            # is still there rather than duplicating it.
            try:
                if row_sink is None and force:
                    _delete_all_summary_for(bq_client, dataset, trend_date)
                elif row_sink is None:
                    _delete_empty_summary_for(bq_client, dataset, trend_date)
            except Exception as exc:
                logger.error(
                    "generate_daily_summary: failed to clear pre-existing "
                    "row(s) for %s before regen: %s",
                    trend_date,
                    exc,
                )
                if force:
                    raise
                try:
                    still_present = _existing_summary_for(bq_client, dataset, trend_date)
                except Exception:
                    # If we cannot even confirm the row state, refuse to risk a
                    # duplicate on the cron path.
                    still_present = True
                if still_present:
                    logger.error(
                        "generate_daily_summary: a row for %s survived the failed "
                        "cleanup; skipping INSERT to avoid a duplicate",
                        trend_date,
                    )
                    return summary
            try:
                rows = [summary.to_bq_row()]
                if row_sink is None:
                    insert_dataframe(pd.DataFrame(rows), "daily_summary")
                else:
                    written = row_sink(rows)
                    if type(written) is not int or written != len(rows):
                        raise ValueError("daily_summary row sink incomplete")
                # Cost via the per-model estimator, keyed off the model that
                # actually ran. The old hardcoded 0.30 / 2.50 literals were the
                # 2.5-flash rates and under-reported the live 3.5-flash cost by
                # ~6x, which hid spend from the cost watchdog and morning-check.
                from src.analysis.gemini_client import _estimate_cost_usd

                logger.info(
                    "generate_daily_summary: persisted (tokens=%d, cost~$%.5f)",
                    summary.prompt_tokens + summary.completion_tokens,
                    _estimate_cost_usd(
                        summary.model, summary.prompt_tokens, summary.completion_tokens
                    ),
                )
            except Exception as exc:
                if row_sink is not None:
                    raise
                logger.error(
                    "generate_daily_summary: persistence failed (non-fatal): %s",
                    exc,
                )

    finally:
        # persist=False is an explicit "write nothing to BigQuery" contract, and
        # the ledger is a BigQuery table. The Gemini calls still billed, and the
        # watchdog's billing reconciliation is what surfaces that spend; a write
        # that breaks the caller's flag is not.
        if persist:
            sink(usage_rows(trend_date, USAGE_CONSUMER, usage_tally))
    return summary


__all__ = ["DailySummary", "generate_daily_summary"]
