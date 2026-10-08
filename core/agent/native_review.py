"""Read-only native-language review controls for Ask's K5 tone cap."""

from __future__ import annotations

import json
import re
from datetime import date

from core.agent.tools.sql_query import MAX_BYTES_BILLED
from core.eval import review

_FEEDBACK_TABLE = "`ogilvy-trends-v2.intelligence_42_agent.feedback`"
_ENRICHMENT_TABLE = "`ogilvy-trends-v2.intelligence_42_core.post_enrichment`"
_WEEK = re.compile(r"^\d{4}-W\d{2}$")
_ELIGIBLE_LANGUAGES = frozenset(review.LANGUAGES) - frozenset(review.NO_REVIEWER)

_NATIVE_FEEDBACK_SQL = f"""WITH candidates AS (
  SELECT SAFE.PARSE_JSON(fb.what) AS what
  FROM {_FEEDBACK_TABLE} AS fb
  WHERE fb.what LIKE '%42_review_v1%'
)
SELECT TO_JSON_STRING(COALESCE(ARRAY_AGG(STRUCT(
  JSON_QUERY(what, '$.week') AS week,
  JSON_QUERY(what, '$.id') AS id,
  JSON_QUERY(what, '$.language') AS language,
  JSON_QUERY(what, '$.gloss_correct') AS gloss_correct,
  JSON_QUERY(what, '$.tone_correct') AS tone_correct
) ORDER BY JSON_VALUE(what, '$.week'), JSON_VALUE(what, '$.id'), JSON_VALUE(what, '$.language')), [])) AS payload
FROM candidates
WHERE JSON_VALUE(what, '$.source') = '42_review_v1'
  AND JSON_VALUE(what, '$.kind') = 'native'"""


def native_feedback_sql() -> str:
    return _NATIVE_FEEDBACK_SQL


def post_languages_query(post_ids) -> tuple[str, dict[str, str]]:
    wanted = list(dict.fromkeys(post_id for post_id in post_ids if isinstance(post_id, str) and post_id))
    if not wanted:
        return "", {}
    params = {f"post_id_{index}": post_id for index, post_id in enumerate(wanted)}
    ids = ", ".join("@" + name for name in params)
    sql = f"""WITH canonical_enrichment AS (
  SELECT e.post_id, e.langs
  FROM {_ENRICHMENT_TABLE} AS e
  WHERE e.tone IS NOT NULL AND e.post_id IN ({ids})
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY e.post_id
    ORDER BY e.tone, e.stance, ARRAY_TO_STRING(e.langs, '|'), ARRAY_TO_STRING(e.entities, '|')
  ) = 1
)
SELECT TO_JSON_STRING(COALESCE(ARRAY_AGG(STRUCT(post_id, langs) ORDER BY post_id), [])) AS payload
FROM canonical_enrichment"""
    return sql, params


def _aggregate_payload(rows) -> list:
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("native review read did not return one aggregate row")
    payload = rows[0].get("payload")
    if not isinstance(payload, str):
        raise ValueError("native review aggregate payload is missing")
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("native review aggregate payload is malformed") from exc
    if not isinstance(parsed, list):
        raise ValueError("native review aggregate payload is incomplete")
    return parsed


def _validate_label(row) -> dict:
    fields = {"week", "id", "language", "gloss_correct", "tone_correct"}
    if not isinstance(row, dict) or set(row) != fields:
        raise ValueError("native review label has an invalid shape")
    week, post_id, language = row["week"], row["id"], row["language"]
    gloss, tone = row["gloss_correct"], row["tone_correct"]
    if not isinstance(week, str) or not _WEEK.fullmatch(week):
        raise ValueError("native review label has an invalid week")
    try:
        year, week_number = int(week[:4]), int(week[6:])
        date.fromisocalendar(year, week_number, 1)
    except ValueError as exc:
        raise ValueError("native review label has an invalid week") from exc
    if not isinstance(post_id, str) or not post_id or not isinstance(language, str) or not language:
        raise ValueError("native review label is incomplete")
    if (gloss is not None and not isinstance(gloss, bool)) or not isinstance(tone, bool):
        raise ValueError("native review label has invalid marks")
    return {"week": week, "id": post_id, "language": language,
            "gloss_correct": gloss, "tone_correct": tone}


def load_native_statuses(warehouse) -> dict[str, str]:
    rows = warehouse.run(_NATIVE_FEEDBACK_SQL, {}, MAX_BYTES_BILLED)
    labels = [_validate_label(row) for row in _aggregate_payload(rows)]
    score = review.score_native(labels)
    statuses = {language: block["status"] for language, block in score["by_language"].items()}
    statuses.update({language: "unreviewed" for language in review.NO_REVIEWER})
    return statuses


def load_post_languages(warehouse, post_ids) -> dict[str, list[str]]:
    sql, params = post_languages_query(post_ids)
    if not sql:
        return {}
    rows = warehouse.run(sql, params, MAX_BYTES_BILLED)
    records = _aggregate_payload(rows)
    wanted = set(params.values())
    languages = {}
    for row in records:
        if not isinstance(row, dict) or set(row) != {"post_id", "langs"}:
            raise ValueError("native review language row has an invalid shape")
        post_id, codes = row["post_id"], row["langs"]
        if not isinstance(post_id, str) or post_id not in wanted or post_id in languages:
            raise ValueError("native review language row is incomplete")
        if not isinstance(codes, list) or not codes or any(not isinstance(code, str) or not code for code in codes):
            raise ValueError("native review language row is malformed")
        languages[post_id] = list(dict.fromkeys(codes))
    return languages


def tone_cap_ids(records, post_languages, native_statuses, is_non_english, *, native_review_loaded=False,
                 native_review_available=False) -> list[str]:
    statuses = native_statuses or {}
    capped = []
    for record in records:
        post_id = record.get("id")
        non_english_text = is_non_english(record.get("text"))
        standing_cap = record.get("market") in {"NG", "KE"} and non_english_text
        if not native_review_loaded:
            if standing_cap and post_id:
                capped.append(post_id)
            continue
        languages = post_languages.get(post_id)
        if not native_review_available:
            known_non_english = any(language.lower() != "en" for language in languages or [])
            if (known_non_english or non_english_text) and post_id:
                capped.append(post_id)
            continue
        if not languages:
            if (standing_cap or non_english_text) and post_id:
                capped.append(post_id)
            continue
        non_english = {language for language in languages if language.lower() != "en"}
        if any(statuses.get(language) in {"capped", "unreviewed"} for language in non_english):
            if post_id:
                capped.append(post_id)
        elif standing_cap and (not non_english or not all(
                language in _ELIGIBLE_LANGUAGES and statuses.get(language) == "cleared"
                for language in non_english)):
            if post_id:
                capped.append(post_id)
        elif (any(language not in statuses for language in non_english)
              or (not non_english and non_english_text)):
            # No standing cap here (ZA), but TRUST C5 holds tone at single_source until the language passes native
            # checks: a language with no score at all (no labels yet, or outside the rotation) has not passed, and an
            # English tag cannot clear text that reads as non-English. A known score, even "insufficient", is left to
            # the ruling pinned by test_insufficient_known_za_review_does_not_add_a_new_cap.
            if post_id:
                capped.append(post_id)
    return capped
