"""Keyword-match topic classifier with Brand24 label mapping and slang fallback.

Reads per-market topic taxonomies from configs/topic_groups/{market}.yaml.
Assigns each row's combined (title + text + hashtags) text to zero or more
topic_groups via word-boundary regex match. Returns a sorted list for
stable downstream ordering. Empty list means unclassified.

Three-layer classification (26 May 2026 update) + geo-collision strip
(28 May 2026 update):

1. Brand24 aggregated-label direct mapping. Brand24's topic + hashtag +
   author + link endpoints return aggregated bucket labels like
   "Music and Entertainment", "Mobile Payment Services", "Chilean Regional
   News". Per-market dict maps known labels to taxonomy topics or to a
   ``__drop__`` sentinel that signals the row should be excluded entirely.
   This recovers about half the previously 94 percent unclassified Brand24
   rows the regex layer was missing.
2. Word-boundary regex over title + text + hashtags (existing behavior).
3. Slang-terms fallback. If layers 1 and 2 produce no match but the
   row's ``slang_terms`` array is non-empty, infer topic from a per-market
   slang-to-topic map. Recovers pidgin, Sheng, and Mzansi slang content
   that uses the slang term without uttering the canonical taxonomy
   keyword.
4. Geo-collision strip. For topics whose slang collides with a foreign
   place / brand (currently ``economy_sapa_hustle`` and
   ``fashion_ankara_asoebi``), drop the topic if the row's haystack
   matches the shared geo blocklist + script + Latin-density backstops
   in ``src/utils/geo_blocklist.py``. Stops Sapa Vietnam tourism rows
   from carrying ``economy_sapa_hustle`` into enriched_content; previously
   the same check ran only at brief-pull time so contaminated rows still
   reached the dashboard topic_groups column.

Classification is pure: same input produces same output, no network or
database access. Compiled regex patterns are cached per market for
performance (~ms per row at 3K rows/day scale).

Extension points for the M4 embeddings ship:
- Replace classify_topics body with embedding-similarity match against
  per-topic anchor vectors. Callers see no change.
- Drop the regex + Brand24 + slang layers once embeddings consistently
  outperform on labeling rate.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

from src.utils.geo_blocklist import (
    TOPIC_GEO_BLOCKLIST,
    text_matches_geo_blocklist,
)
from src.utils.language_guard import (
    combined_text,
    is_hard_foreign_text,
    should_skip_content_type,
)
from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

TOPIC_GROUPS_DIR = Path(__file__).parent.parent.parent / "configs" / "topic_groups"

# Sentinel returned when a Brand24 aggregated label should cause the row
# to be dropped from downstream scoring + brief generation. The caller in
# enrichment.py treats an empty topic_groups list as "unclassified" which
# still counts toward source pool; the __drop__ sentinel is a stronger
# signal that the row is commercial spam, foreign news, or aggregator
# noise and should not enter trend_scores at all.
DROP_SENTINEL = "__drop__"


def _language_guard_on() -> bool:
    """True when the ingest-time hard-foreign-language guard is enabled.

    Dark by default. Set LANGUAGE_GUARD_ENABLED=true to drop hard-foreign rows
    at classification, before they reach scoring. Read per call so the flag can
    flip on the live job without a code change and toggle in tests + shadow-runs.
    """
    return os.environ.get("LANGUAGE_GUARD_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


# A family one keyword hit behind the leader still survives; a family further
# behind is treated as a graze and dropped. 1 keeps genuinely cross-cutting
# posts (a tie) while removing a single stray hit on a dominated family.
_DOMINANCE_MARGIN = 1


def _dominance_on() -> bool:
    """True when topic-dominance arbitration is enabled.

    Dark by default. Set TOPIC_DOMINANCE_ENABLED=true to score each matched
    family by its distinct keyword-hit count and keep only the strongest family
    plus any within _DOMINANCE_MARGIN, so a post that merely grazes a second
    topic is not tagged into it (an amapiano clip with one political word stops
    landing in politics). Read per call so it flips on the live job without a
    code change and toggles in tests + shadow-runs.
    """
    return os.environ.get("TOPIC_DOMINANCE_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


# Per-market Brand24 aggregated-label-to-topic mapping. Validated against
# the 14-day BQ probe on 26 May 2026 that found 94 percent of Brand24
# rows arriving without topic_groups labels because the strict regex layer
# misses aggregated bucket labels.
#
# Format: {market: {brand24_label_lowercase: topic_name_or_DROP_SENTINEL}}
#
# Add new mappings here as Phase 2 BQ re-audits surface additional
# Brand24 output labels. The weekly Gemini taxonomy proposal cron will
# auto-surface new candidates for this dict.
_BRAND24_LABEL_TO_TOPIC: dict[str, dict[str, str]] = {
    "za": {
        # Mapped to taxonomy
        "music and entertainment": "music_amapiano",
        "sports and athletics": "sports_rugby",
        "food and cooking": "food_rituals_braai",
        "education and learning": "education_matric_nsfas",
        "finance and investment": "finance_stokvel",
        "politics and government": "politics_crises",
        "electricity and power": "infra_power_eskom",
        # Drop noise
        "vehicle sales listings": DROP_SENTINEL,
        "legal corruption cases": DROP_SENTINEL,
        "retail shopping commerce": DROP_SENTINEL,
        "book reviews content": DROP_SENTINEL,
        "real estate listings": DROP_SENTINEL,
        "general news content": DROP_SENTINEL,
        "social media engagement": DROP_SENTINEL,
        "chilean regional news": DROP_SENTINEL,
        "china xinhua news": DROP_SENTINEL,
        "gma public affairs": DROP_SENTINEL,
        "raai laxmi": DROP_SENTINEL,
        "starttimes": DROP_SENTINEL,
        "viva entertainment": DROP_SENTINEL,
        "gma network": DROP_SENTINEL,
        "visionsoul.co.za": DROP_SENTINEL,
    },
    "ng": {
        # Mapped to taxonomy
        "music and entertainment": "music_afrobeats",
        "sports and athletics": "sports_football",
        "food and cooking": "food_jollof",
        "employment and recruitment": "economy_sapa_hustle",
        "fashion and beauty": "fashion_ankara_asoebi",
        "film and cinema": "film_nollywood",
        "politics and government": "politics_tinubu",
        "finance and investment": "economy_sapa_hustle",
        "celebration and weddings": "culture_owambe",
        # Drop noise
        "social media engagement": DROP_SENTINEL,
        "social interactions": DROP_SENTINEL,
        "chilean regional news": DROP_SENTINEL,
        "china xinhua news": DROP_SENTINEL,
        "general news content": DROP_SENTINEL,
        "real estate listings": DROP_SENTINEL,
        "retail shopping commerce": DROP_SENTINEL,
        "starttimes": DROP_SENTINEL,
        "billboard": DROP_SENTINEL,
        "tvcnews.tv": DROP_SENTINEL,
    },
    "ke": {
        # Mapped to taxonomy
        "music and entertainment": "music_gengetone",
        "sports and athletics": "sports_football",
        "food and cooking": "food_nyamachoma",
        "fashion and beauty": "fashion_mitumba",
        "mobile payment services": "fintech_mpesa",
        "mobile money services": "fintech_mpesa",
        "politics and government": "politics_maandamano",
        "transport and logistics": "transport_matatu",
        "employment and careers": "economy_hustle",
        # Drop noise
        "multilingual mixed content": DROP_SENTINEL,
        "african business conferences": DROP_SENTINEL,
        "retail shopping commerce": DROP_SENTINEL,
        "vehicle sales listings": DROP_SENTINEL,
        "china xinhua news": DROP_SENTINEL,
        "chinese tv content": DROP_SENTINEL,
        "raai laxmi": DROP_SENTINEL,
        "starttimes": DROP_SENTINEL,
        "ke.sportpesa.com": DROP_SENTINEL,
        "citizen tv kenya": DROP_SENTINEL,
        "ktnnews.com": DROP_SENTINEL,
        "general news content": DROP_SENTINEL,
        "#babyshop": DROP_SENTINEL,
    },
}


# Per-market slang-to-topic fallback. Used only when the Brand24 label
# layer and the regex layer both produce no match AND the row has at
# least one slang term detected by the slang scorer. Recovers content
# where the canonical taxonomy keyword is absent but the cultural
# pidgin / Sheng / Mzansi slang term is present.
_SLANG_TO_TOPIC: dict[str, dict[str, str]] = {
    "za": {
        "log drum": "music_amapiano",
        "piano": "music_amapiano",
        "amapiano": "music_amapiano",
        "softlife": "genz_lifestyle",
        "soft life": "genz_lifestyle",
        "delulu": "genz_lifestyle",
        "demure": "genz_lifestyle",
        "springbok": "sports_rugby",
        "bokke": "sports_rugby",
        "bafana": "sports_rugby",
        "matric": "education_matric_nsfas",
        "nsfas": "education_matric_nsfas",
        "stokvel": "finance_stokvel",
        "braai": "food_rituals_braai",
        "eskom": "infra_power_eskom",
        "load shedding": "infra_power_eskom",
        "loadshedding": "infra_power_eskom",
    },
    "ng": {
        "wahala": "culture_owambe",
        "owambe": "culture_owambe",
        "asoebi": "culture_owambe",
        "aso ebi": "culture_owambe",
        "gele": "culture_owambe",
        "japa": "diaspora_japa",
        "abroad": "diaspora_japa",
        "relocate": "diaspora_japa",
        "sapa": "economy_sapa_hustle",
        "broke": "economy_sapa_hustle",
        "shege": "economy_sapa_hustle",
        "afrobeats": "music_afrobeats",
        "davido": "music_afrobeats",
        "wizkid": "music_afrobeats",
        "asake": "music_afrobeats",
        "burna": "music_afrobeats",
        "rema": "music_afrobeats",
        "tems": "music_afrobeats",
        "tyla": "music_afrobeats",
        "nollywood": "film_nollywood",
        "jollof": "food_jollof",
        "tinubu": "politics_tinubu",
        "ankara": "fashion_ankara_asoebi",
        "agbada": "fashion_ankara_asoebi",
        "super eagles": "sports_football",
        "osimhen": "sports_football",
        "lookman": "sports_football",
        # High-signal slang in keywords/ng.yaml but absent from taxonomy regex
        # until 30 Jun 2026. BQ: 597 NG unclassified rows carried slang_terms
        # while classification_slang_rows stayed 0; nepa/nysc/asuu/danfo were
        # the mappable slice (lagos/naija/abuja are geo markers, not topics).
        "nepa": "economy_sapa_hustle",
        "no light": "economy_sapa_hustle",
        "danfo": "economy_sapa_hustle",
        "asuu": "politics_tinubu",
        "nysc": "politics_tinubu",
    },
    "ke": {
        "gengetone": "music_gengetone",
        "arbantone": "music_gengetone",
        "bongo flava": "music_gengetone",
        "sheng": "genz_sheng",
        "niko kadi": "genz_sheng",
        "una diglo": "genz_sheng",
        "mafrumbanya": "genz_sheng",
        "maandamano": "politics_maandamano",
        "finance bill": "politics_maandamano",
        "anguka nayo": "politics_maandamano",
        "mitumba": "fashion_mitumba",
        "thrift": "fashion_mitumba",
        "gikomba": "fashion_mitumba",
        "matatu": "transport_matatu",
        "boda boda": "transport_matatu",
        "mpesa": "fintech_mpesa",
        "safaricom": "fintech_mpesa",
        "fuliza": "fintech_mpesa",
        "jua kali": "economy_hustle",
        "mama mboga": "economy_hustle",
        "biashara": "economy_hustle",
        "mtaani": "economy_hustle",
        "nyama choma": "food_nyamachoma",
        "ugali": "food_nyamachoma",
        "harambee stars": "sports_football",
        "arsenal kenya": "sports_football",
    },
}


_WORD_CHAR = re.compile(r"\w")


def _anchored_pattern(kw: str) -> str | None:
    """Build a token-boundary regex string for one keyword, or None to skip.

    Each keyword edge is anchored by its own character class, not a blanket
    ``\\b`` on both ends. A ``\\b`` only fires between a word and a non-word
    character, so anchoring a keyword whose edge is itself non-word kills the
    pattern. The clearest case is a leading ``#``: a space-to-``#`` transition
    is non-word to non-word, so ``\\b#nyamachoma\\b`` never matches anything,
    not even literal ``#nyamachoma`` text. That left every ``#``-prefixed
    taxonomy keyword across za/ng/ke dead.

    Rules:
    - A leading ``#`` is stripped. The hashtag content (``nyamachoma``) is
      what scraped text actually carries, with or without the ``#``, so the
      pattern anchors on the core token and matches both forms.
    - A word-character edge keeps ``\\b`` (preserves the embedded-substring
      guard, e.g. ``piano`` still does not match inside ``amapiano``).
    - Any other non-word edge falls back to a whitespace/string-edge
      lookaround so the pattern anchors on a token edge instead of dying.
    - Internal whitespace is relaxed to ``\\s+`` so "kabza de small" matches
      "kabza  de  small". Special chars are escaped via ``re.escape``.
    """
    core = kw.strip().lstrip("#")
    if not core:
        return None
    escaped = re.escape(core).replace(r"\ ", r"\s+")
    lead = r"\b" if _WORD_CHAR.match(core[0]) else r"(?<!\S)"
    trail = r"\b" if _WORD_CHAR.match(core[-1]) else r"(?!\S)"
    return lead + escaped + trail


# Per-market football topic key. ZA folds football into sports_rugby (a single
# "major SA sports teams" bucket); NG and KE have a dedicated sports_football.
# Used by the fixture-pattern rule below to know which topic an "X vs Y"
# football fixture routes to.
_FOOTBALL_TOPIC_BY_MARKET: dict[str, str] = {
    "za": "sports_rugby",
    "ng": "sports_football",
    "ke": "sports_football",
}

# Curated football entities (national teams + major clubs) used to GUARD the
# "X vs Y" / "X versus Y" fixture pattern. The fixture rule fires only when one
# side of the vs/versus is a known football entity, OR a football keyword
# already appears elsewhere in the row (see _football_fixture_match). This
# keeps "Apple vs Samsung", "Trump vs Biden", "Marvel vs DC" from classifying
# as football while still catching "Portugal vs Chile" or "Arsenal vs Chelsea".
# Lowercased; matched on token boundaries. Multi-word entities are matched with
# relaxed internal whitespace via _anchored_pattern.
_FOOTBALL_FIXTURE_ENTITIES: tuple[str, ...] = (
    # National teams (SSA + common World Cup / AFCON opponents)
    "bafana bafana",
    "bafana",
    "super eagles",
    "harambee stars",
    # Bare home-market country names (nigeria / south africa / kenya) are
    # deliberately NOT entities: "Pakistan vs South Africa" (cricket) and
    # "Kenya vs Tanzania" (fintech) would false-positive. The national-team
    # names above cover genuine home fixtures, and the keyword-co-occurrence
    # branch still catches a home "X vs Y" when real football context is present.
    "ghana",
    "egypt",
    "morocco",
    "senegal",
    "cameroon",
    "ivory coast",
    "algeria",
    "tunisia",
    "argentina",
    "brazil",
    "france",
    "england",
    "spain",
    "germany",
    "portugal",
    "italy",
    "netherlands",
    "belgium",
    "croatia",
    "uruguay",
    "chile",
    "mexico",
    # Major clubs
    "real madrid",
    "barcelona",
    "manchester united",
    "manchester city",
    "man united",
    "man city",
    "man utd",
    "liverpool",
    "arsenal",
    "chelsea",
    "tottenham",
    "psg",
    "bayern munich",
    "bayern",
    "juventus",
    "inter milan",
    "ac milan",
    "atletico madrid",
    "borussia dortmund",
    "napoli",
    # SSA league clubs
    "orlando pirates",
    "kaizer chiefs",
    "mamelodi sundowns",
    "supersport united",
    "gor mahia",
    "afc leopards",
)

# An entity alternation, longest-first so "manchester united" wins over a bare
# "manchester" fragment. Each entity is anchored on token boundaries so
# "arsenal" does not match inside "arsenale". Built once at import.
_FOOTBALL_ENTITY_ALT = "|".join(
    re.escape(e).replace(r"\ ", r"\s+")
    for e in sorted(_FOOTBALL_FIXTURE_ENTITIES, key=len, reverse=True)
)

# "X vs Y" / "X versus Y" where AT LEAST ONE side is a known football entity.
# The vs/versus token must stand alone on word boundaries, so "vsync" or a
# stray "v" do not trigger it. Two guarded shapes, OR-joined:
#   entity (vs|versus) <anything>    e.g. "Arsenal vs Spurs"
#   <anything> (vs|versus) entity    e.g. "the lads vs Chelsea"
# This is the entity-flank guard, the only guard _football_fixture_match uses.
# A bare single-letter "v" is deliberately excluded: too noisy for the <2%
# false-positive bar, and football social text overwhelmingly uses "vs".
_VS_CONNECTOR = r"(?:vs\.?|versus)"
_FOOTBALL_FIXTURE_RE = re.compile(
    r"(?:"
    rf"(?:\b(?:{_FOOTBALL_ENTITY_ALT})\b)\s+{_VS_CONNECTOR}\s+\w"
    r"|"
    rf"\w\s+{_VS_CONNECTOR}\s+(?:\b(?:{_FOOTBALL_ENTITY_ALT})\b)"
    r")",
    re.IGNORECASE,
)


def _football_fixture_match(haystack: str, market: str) -> bool:
    """True when the row is a football fixture for ``market``.

    Entity-flank guard (precision-first; the shadow requires <2% false
    positives, so a bare ``\\w+ vs \\w+`` is deliberately NOT used): an
    ``X vs Y`` / ``X versus Y`` phrase where at least one side is a curated
    football entity (national team or major club). This classifies
    "Portugal vs Chile" or "Arsenal vs Chelsea" but not "Apple vs Samsung",
    "Trump vs Biden", or "Marvel vs DC".

    Note: this runs only when the market football topic's own patterns did NOT
    match (the call site gates on football_topic not in matched), so a
    keyword-co-occurrence condition would be unreachable here and is omitted.
    """
    if market not in _FOOTBALL_TOPIC_BY_MARKET:
        return False
    return bool(_FOOTBALL_FIXTURE_RE.search(haystack))


def _compile_patterns(
    topic_groups: dict[str, list[str]],
) -> dict[str, list[re.Pattern]]:
    """Compile each topic's keywords into token-boundary regex patterns.

    Anchoring is per-edge (see ``_anchored_pattern``) so ``#``-prefixed and
    other non-word-edged keywords match instead of dying on a misplaced
    ``\\b``. Malformed patterns log WARNING and are skipped without crashing
    the classifier.
    """
    compiled: dict[str, list[re.Pattern]] = {}
    for topic, keywords in topic_groups.items():
        patterns: list[re.Pattern] = []
        for kw in keywords:
            if not kw or not isinstance(kw, str):
                continue
            pattern_str = _anchored_pattern(kw)
            if pattern_str is None:
                continue
            try:
                patterns.append(re.compile(pattern_str, re.IGNORECASE))
            except re.error as exc:
                logger.warning("topic_classifier bad keyword %r in %r: %s", kw, topic, exc)
        compiled[topic] = patterns
    return compiled


@lru_cache(maxsize=3)
def _cached_patterns(market: str) -> dict[str, list[re.Pattern]]:
    """Per-market compiled pattern cache. Invalidated by process restart."""
    topic_groups = load_topic_groups(market)
    return _compile_patterns(topic_groups)


def load_topic_groups(market: str) -> dict[str, list[str]]:
    """Load topic taxonomy for a market from configs/topic_groups/{m}.yaml.

    Returns a flat dict of topic_group -> keyword list. Raises
    FileNotFoundError if the market config is missing, with the expected
    path in the message. Fails fast by design; silent fallback would
    route every row to unclassified without an operator noticing.
    """
    path = TOPIC_GROUPS_DIR / f"{market}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Topic taxonomy not found for market {market!r}: {path}")
    with path.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    groups_raw = doc.get("topic_groups", {}) or {}
    out: dict[str, list[str]] = {}
    for topic, spec in groups_raw.items():
        if isinstance(spec, dict):
            out[topic] = list(spec.get("keywords") or [])
        elif isinstance(spec, list):
            out[topic] = list(spec)
    return out


def _brand24_label_match(query_term: str | None, market: str) -> str | None:
    """Return the taxonomy topic or DROP_SENTINEL for a known Brand24 label.

    Returns None if no mapping exists (caller falls through to regex layer).
    Case-insensitive lookup. Strips whitespace.
    """
    if not query_term:
        return None
    key = str(query_term).strip().lower()
    if not key:
        return None
    return _BRAND24_LABEL_TO_TOPIC.get(market, {}).get(key)


def _slang_topic_match(slang_terms: list[str] | None, market: str) -> str | None:
    """Return the first taxonomy topic matched by any slang term in the row.

    Returns None if no slang term matches the per-market slang-to-topic
    map. Used only as a fallback after Brand24-label and regex layers
    both fail to classify.
    """
    if not slang_terms:
        return None
    slang_map = _SLANG_TO_TOPIC.get(market, {})
    for term in slang_terms:
        if not term:
            continue
        key = str(term).strip().lower()
        if not key:
            continue
        topic = slang_map.get(key)
        if topic:
            return topic
    return None


# GDELT GKG theme families -> per-market taxonomy topic. HIGH-CONFIDENCE,
# EVENT-DRIVEN tokens only (validated against a live gdelt-bq sample 29 May
# 2026; tuned for precision after a 26 May measurement showed broad codes
# flooding the topics). Each family is a list of substring tokens matched
# against the uppercased text. Deliberately EXCLUDES broad governance/macro
# codes (GENERAL_GOVERNMENT, LEADER, ECON_, EPU_ECONOMY, WB_* generic): those
# force-fit routine governance/macro news into the Gen Z politics/economy
# topics (under the broad map NG politics_tinubu took 186/336 GDELT rows,
# swamping the Gen Z conversational signal). The tokens below fire on actual
# events Gen Z reacts to. Generic governance/health/crime news stays
# unclassified, which is correct: it is not a Gen Z cultural trend.
_GDELT_THEME_FAMILIES: dict[str, list[str]] = {
    "politics": ["ELECTION", "IMPEACHMENT", "REFERENDUM", "POLITICAL_PARTY"],
    "protest": ["PROTEST", "UNREST", "RIOT", "DEMONSTRATION"],
    "economy": [
        "INFLATION",
        "UNEMPLOYMENT",
        "POVERTY",
        "FUELPRICE",
        "COSTOFLIVING",
        # 30 Jun 2026: unclassified GDELT residual carried ECON_/ENV_OIL /
        # WORLDCURRENCIES tokens the narrow map missed (e.g.
        # ECON_WORLDCURRENCIES_DOLLAR, ENV_OIL). Substrings only, still
        # routed per-market to the economy topic, not broad WB_* governance.
        "DOLLAR",
        "CURRENCY",
        "WORLDCURRENCIES",
        "MONETARY",
        "FUEL",
        "OIL",
    ],
    "migration": ["MIGRATION", "REFUGEE", "IMMIGRATION", "ASYLUM", "DIASPORA"],
    "education": ["EDUCATION", "SCHOOL", "UNIVERSITY", "STUDENT"],
    "energy": ["ELECTRICITY", "POWER_OUTAGE", "BLACKOUT", "LOADSHEDDING"],
    "sport": ["SOCCER", "FOOTBALL", "RUGBY", "OLYMPIC", "WORLDCUP"],
}

# Per-market routing from theme family to an actual taxonomy topic. A family
# absent from a market's dict is not mapped there (e.g. migration only routes
# on NG via diaspora_japa; protest only on KE via politics_maandamano).
_GDELT_FAMILY_TO_TOPIC: dict[str, dict[str, str]] = {
    "za": {
        "politics": "politics_crises",
        "protest": "politics_crises",
        "economy": "finance_stokvel",
        "education": "education_matric_nsfas",
        "energy": "infra_power_eskom",
        "sport": "sports_rugby",
    },
    "ng": {
        "politics": "politics_tinubu",
        "protest": "politics_tinubu",
        "economy": "economy_sapa_hustle",
        "migration": "diaspora_japa",
        "sport": "sports_football",
    },
    "ke": {
        "politics": "politics_maandamano",
        "protest": "politics_maandamano",
        "economy": "economy_hustle",
        "sport": "sports_football",
    },
}

# Deterministic tie-break order when two families score equally.
_GDELT_FAMILY_PRIORITY: tuple[str, ...] = (
    "protest",
    "politics",
    "economy",
    "migration",
    "education",
    "energy",
    "sport",
)


def _gdelt_theme_topic(text: str, market: str) -> str | None:
    """Map GDELT GKG theme codes in ``text`` to a single taxonomy topic.

    Rescue layer for GDELT rows the regex layer left unclassified. GKG
    articles carry many theme codes, so a union would over-tag and dilute
    the 1/N row weight; instead this picks the single DOMINANT mappable
    family (most token hits in the uppercased text) and routes it to the
    market's topic. Returns None when no family is mapped for this market
    or no family token appears.
    """
    routing = _GDELT_FAMILY_TO_TOPIC.get(market, {})
    if not routing or not text:
        return None
    haystack = text.upper()
    best_family: str | None = None
    best_score = 0
    for family in _GDELT_FAMILY_PRIORITY:
        if family not in routing:
            continue
        tokens = _GDELT_THEME_FAMILIES.get(family, [])
        score = sum(haystack.count(tok) for tok in tokens)
        if score > best_score:
            best_score = score
            best_family = family
    if best_family is None or best_score == 0:
        return None
    return routing.get(best_family)


def _strip_geo_collisions(
    topics: list[str],
    title: str,
    text: str,
    hashtags: str,
) -> list[str]:
    """Drop topics in ``TOPIC_GEO_BLOCKLIST`` when the row's haystack
    matches a foreign-collision marker for that topic.

    Used by ``classify_topics`` as the final layer so contaminated rows
    never enter ``enriched_content.topic_groups`` with the collision-prone
    tag. The shared blocklist in ``src/utils/geo_blocklist.py`` is the
    same data the brief generator applies at sample-pull time; lifting
    it to classification time stops the contaminated rows from reaching
    the dashboard topic_groups column at all.

    Topics outside the blocklist (the majority) pass through untouched
    so this layer is a no-op for the typical classification path.
    """
    if not topics:
        return topics
    # Raw haystack (mixed-case, original Unicode) so the script-block
    # and Latin-density backstops see the diacritics. text_matches_geo_blocklist
    # lowercases + normalises internally for the keyword layer.
    raw = " ".join([title or "", text or "", hashtags or ""])
    kept: list[str] = []
    for topic in topics:
        if topic in TOPIC_GEO_BLOCKLIST and text_matches_geo_blocklist(raw, topic):
            continue
        kept.append(topic)
    return kept


# Wave 1 classification-layer labels. The winning layer is carried to
# enriched_content.classification_layer and accumulated per-layer in
# pipeline_runs (gated behind CLASSIFICATION_INSTRUMENTATION_ENABLED). "drop"
# is the foreign-guard / Brand24-noise sentinel path; it is neither classified
# nor unclassified, so it is excluded from the labelling-rate denominator.
LAYER_BRAND24 = "brand24"
LAYER_REGEX = "regex"
LAYER_GDELT = "gdelt"
LAYER_SLANG = "slang"
LAYER_EMBEDDING = "embedding"
LAYER_UNCLASSIFIED = "unclassified"
LAYER_DROP = "drop"


def classify_topics_with_layer(
    title: str,
    text: str,
    hashtags: str,
    market: str,
    query_term: str | None = None,
    slang_terms: list[str] | None = None,
    content_type: str | None = None,
) -> tuple[list[str], str]:
    """Layer-aware classifier. Returns ``(topics, winning_layer)``.

    Same layered logic as ``classify_topics`` (which now delegates here), with
    the winning layer reported alongside the topics for Wave 1 instrumentation:
      brand24      Layer 1 aggregated-label map produced the topic
      regex        Layer 2 word-boundary regex (incl. football fixture)
      gdelt        Layer 2.5 GDELT theme rescue
      slang        Layer 3 slang fallback
      unclassified no layer matched (empty topic list)
      drop         foreign-guard or Brand24 drop-list sentinel (``__drop__``)

    The embedding layer is not reached here; it runs later in enrichment over
    the keyword-unclassified residual and stamps ``embedding`` itself.
    """
    # Layer 0: hard-foreign-language guard (dark behind LANGUAGE_GUARD_ENABLED).
    # Drops a row langdetect identifies as hard-foreign BEFORE it enters
    # scoring, so foreign-language contamination (Brazilian-Portuguese in NG
    # japa, Turkish in NG ankara, Vietnamese in NG sapa) cannot inflate a
    # topic's volume or velocity. Conservative by construction so it never
    # strips Pidgin or Sheng: hard-foreign codes only (never tl/id/so), 100+
    # chars of deduped text, 0.90 confidence, and gdelt + music/video rows
    # skipped (their text is machine codes / titles, not language). Tuned
    # against the 8 Jun shadow-validate. See src/utils/language_guard.py.
    if (
        _language_guard_on()
        and not should_skip_content_type(content_type)
        and is_hard_foreign_text(combined_text(title, text))
    ):
        return [DROP_SENTINEL], LAYER_DROP

    # Layer 1: Brand24 aggregated-label direct mapping
    brand24_match = _brand24_label_match(query_term, market)
    if brand24_match == DROP_SENTINEL:
        return [DROP_SENTINEL], LAYER_DROP
    if brand24_match:
        # Geo strip applies even to Brand24-mapped topics. If a Brand24
        # row mapped to fashion_ankara_asoebi carries Turkish-Ankara
        # markers it should still be dropped from the topic, not
        # silently tagged.
        topics = _strip_geo_collisions([brand24_match], title, text, hashtags)
        # A geo strip can empty the list; that is an unclassified outcome, not
        # a brand24 win.
        return topics, (LAYER_BRAND24 if topics else LAYER_UNCLASSIFIED)

    # Layer 2: existing regex over text
    haystack = " ".join([title or "", text or "", hashtags or ""]).lower()
    if haystack.strip():
        try:
            patterns = _cached_patterns(market)
        except FileNotFoundError:
            raise
        dominance = _dominance_on()
        matched: list[str] = []
        scores: dict[str, int] = {}
        for topic, pattern_list in patterns.items():
            if dominance:
                # Count distinct keyword hits so the strongest family can win.
                hits = sum(1 for pattern in pattern_list if pattern.search(haystack))
                if hits:
                    matched.append(topic)
                    scores[topic] = hits
            else:
                for pattern in pattern_list:
                    if pattern.search(haystack):
                        matched.append(topic)
                        break
        # Guarded football-fixture rule. Classify an "X vs Y" / "X versus Y"
        # fixture to the market's football topic, but only when a curated
        # football entity flanks the vs (see _football_fixture_match). Runs
        # inside Layer 2 so the topic joins the same matched set, sort, and
        # geo strip.
        football_topic = _FOOTBALL_TOPIC_BY_MARKET.get(market)
        if (
            football_topic
            and football_topic not in matched
            and _football_fixture_match(haystack, market)
        ):
            matched.append(football_topic)
            scores[football_topic] = 1
        if matched:
            if dominance and scores:
                # Keep the top-scoring family plus any within the margin. A tie
                # keeps all, so a genuinely cross-cutting post survives, while a
                # one-keyword graze on a dominated family is dropped.
                top = max(scores.values())
                matched = [t for t in matched if scores.get(t, 1) >= top - _DOMINANCE_MARGIN]
            topics = _strip_geo_collisions(sorted(matched), title, text, hashtags)
            if topics:
                return topics, LAYER_REGEX
            # Regex matched but the geo strip removed everything: fall through
            # to the rescue layers rather than claiming a regex win.

    # Layer 2.5: GDELT theme rescue. Only for GDELT rows the regex layer
    # left unclassified. Gate on content_type (unique to GDELT; its
    # platform is "news" and source is the outlet name). Single dominant
    # family, geo-stripped.
    if content_type == "gdelt_gkg":
        theme_topic = _gdelt_theme_topic(text or "", market)
        if theme_topic:
            topics = _strip_geo_collisions([theme_topic], title, text, hashtags)
            if topics:
                return topics, LAYER_GDELT

    # Layer 3: slang_terms fallback
    slang_match = _slang_topic_match(slang_terms, market)
    if slang_match:
        topics = _strip_geo_collisions([slang_match], title, text, hashtags)
        if topics:
            return topics, LAYER_SLANG

    return [], LAYER_UNCLASSIFIED


def classify_topics(
    title: str,
    text: str,
    hashtags: str,
    market: str,
    query_term: str | None = None,
    slang_terms: list[str] | None = None,
    content_type: str | None = None,
) -> list[str]:
    """Return sorted list of topic_group names matching the row's text.

    Thin back-compatible wrapper over ``classify_topics_with_layer`` that
    drops the layer. Existing callers (and the embedding-rescue path) are
    unchanged: same topics, same ``["__drop__"]`` sentinel, same empty-list
    semantics. See ``classify_topics_with_layer`` for the layer order.
    """
    topics, _layer = classify_topics_with_layer(
        title=title,
        text=text,
        hashtags=hashtags,
        market=market,
        query_term=query_term,
        slang_terms=slang_terms,
        content_type=content_type,
    )
    return topics


__all__ = [
    "DROP_SENTINEL",
    "LAYER_BRAND24",
    "LAYER_DROP",
    "LAYER_EMBEDDING",
    "LAYER_GDELT",
    "LAYER_REGEX",
    "LAYER_SLANG",
    "LAYER_UNCLASSIFIED",
    "classify_topics",
    "classify_topics_with_layer",
    "load_topic_groups",
]
