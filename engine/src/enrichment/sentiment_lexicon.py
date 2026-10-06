"""Social sentiment lexicon scorer (Wave 2, Group A).

Most of the feed (about 95 percent) is social rows that carry no GDELT V2Tone,
so they have no per-row sentiment signal. GDELT rows get tone_avg / tone_polarity
parsed from V2Tone in enrichment; everything else gets nothing. This module fills
that gap with a lexicon + heuristic scorer tuned for short, slangy SSA social text.

The scorer returns a float in -1.0..1.0 for a text:

  +1.0  strongly positive
   0.0  neutral / no polarity terms found
  -1.0  strongly negative

Heuristics layered over the raw lexicon match:

  negation   a polarity term within a short window after a negator flips sign
             ("not great" reads negative, "no wahala" reads positive).
  intensifier  an intensifier just before a polarity term amplifies its weight
             ("very nice", "well sweet"); a downtoner damps it ("kinda bad").
  slang polarity  per-market slang terms carry their own polarity so local
             expressions ("sapa", "lit", "fire", "scam") score without needing
             a generic English equivalent in the text.

Lexicons live in configs/sentiment_lexicons/{za,ng,ke}.yaml. Each carries
positive and negative term lists (English plus local slang) and optional
per-market negators / intensifiers / downtoners that extend the shared defaults.

This module is pure compute. It does not read any flag and does not touch
BigQuery. The enrichment hook in src/ingestion/enrichment.py decides when to
call it (gated SENTIMENT_LEXICON_ENABLED) and where to store the result.
"""

from __future__ import annotations

import re
from functools import lru_cache

from src.utils.config_loader import CONFIGS_DIR, load_yaml

# Shared, market-agnostic heuristic vocab. Per-market YAML can extend these via
# its own negators / intensifiers / downtoners keys; it never replaces them.
_DEFAULT_NEGATORS: frozenset[str] = frozenset(
    {
        "not",
        "no",
        "never",
        "without",
        "cant",
        "cannot",
        "dont",
        "doesnt",
        "didnt",
        "isnt",
        "wasnt",
        "arent",
        "wont",
        "aint",
        "hardly",
        "barely",
        "neither",
        "nor",
        "nothing",
    }
)

_DEFAULT_INTENSIFIERS: dict[str, float] = {
    "very": 1.5,
    "really": 1.5,
    "so": 1.4,
    "super": 1.6,
    "extremely": 1.8,
    "totally": 1.5,
    "absolutely": 1.7,
    "mad": 1.5,
    "well": 1.4,
    "proper": 1.5,
    "too": 1.4,
    "highly": 1.6,
    "deadass": 1.6,
}

_DEFAULT_DOWNTONERS: dict[str, float] = {
    "kinda": 0.6,
    "kind": 0.6,
    "slightly": 0.5,
    "somewhat": 0.6,
    "bit": 0.6,
    "little": 0.6,
    "barely": 0.5,
    "almost": 0.7,
}

# How many tokens back from a polarity term we look for a negator or modifier.
# Short window keeps "not bad at all" from flipping a term five words away while
# still catching the natural "not very good" / "no real wahala" cadence.
_NEGATION_WINDOW = 3
_MODIFIER_WINDOW = 2

# Saturation so a wall of polarity words does not run the score to a hard rail
# off one repeated term. Net polarity / (abs(net) + K) maps any real signal into
# (-1, 1); K=2.0 means one clear positive term lands near +0.33, three near +0.6.
_SATURATION_K = 2.0

# Token pattern: words, hashtags without the hash, and apostrophe-stripped forms
# so "don't" tokenises to "dont" and matches the negator set above.
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Apostrophe variants stripped before tokenising so contractions collapse into
# the negator forms (dont, isnt). Built from code points (straight apostrophe +
# U+2019 right single quote) so the source carries no ambiguous glyph.
_APOSTROPHE_STRIP = str.maketrans({"'": "", chr(0x2019): ""})


def _tokenize(text: str) -> list[str]:
    """Lowercase, drop apostrophes, split on non-alphanumeric.

    "Don't @ me, it's NOT great!!" -> [dont, me, its, not, great].
    Apostrophes are removed before splitting so contractions collapse into the
    negator forms (dont, isnt) the heuristic checks for.
    """
    if not text:
        return []
    lowered = str(text).lower().translate(_APOSTROPHE_STRIP)
    return _TOKEN_RE.findall(lowered)


def _phrase_present(tokens: list[str], phrase_tokens: tuple[str, ...]) -> list[int]:
    """Return the start indices where a multi-token phrase appears in tokens."""
    if not phrase_tokens:
        return []
    n = len(phrase_tokens)
    hits: list[int] = []
    for i in range(len(tokens) - n + 1):
        if tuple(tokens[i : i + n]) == phrase_tokens:
            hits.append(i)
    return hits


class SentimentLexicon:
    """Per-market lexicon scorer. One instance per market, cached by load_lexicon.

    Construction normalises the YAML lists into:
      _terms: phrase_tokens -> base polarity weight (signed, magnitude scaled).
    Single-word and multi-word terms are both supported; multi-word terms are
    matched as contiguous token runs.
    """

    def __init__(self, market: str, config: dict) -> None:
        self.market = market
        positives = config.get("positive", []) or []
        negatives = config.get("negative", []) or []

        # term phrase (tuple of tokens) -> signed base weight.
        self._terms: dict[tuple[str, ...], float] = {}
        self._max_phrase_len = 1
        for term in positives:
            self._add_term(term, +1.0)
        for term in negatives:
            self._add_term(term, -1.0)

        self._negators = set(_DEFAULT_NEGATORS) | {
            _norm_token(t) for t in (config.get("negators") or [])
        }
        self._intensifiers = dict(_DEFAULT_INTENSIFIERS)
        for word, mult in (config.get("intensifiers") or {}).items():
            self._intensifiers[_norm_token(word)] = float(mult)
        self._downtoners = dict(_DEFAULT_DOWNTONERS)
        for word, mult in (config.get("downtoners") or {}).items():
            self._downtoners[_norm_token(word)] = float(mult)

    def _add_term(self, term: object, sign: float) -> None:
        phrase = tuple(_tokenize(str(term)))
        if not phrase:
            return
        self._terms[phrase] = sign
        if len(phrase) > self._max_phrase_len:
            self._max_phrase_len = len(phrase)

    def score(self, text: str) -> float:
        """Return a sentiment score in -1.0..1.0 for text.

        0.0 when no polarity term is present. Negation flips the sign of a hit;
        intensifiers / downtoners scale its magnitude. The summed signed weight
        is squashed through hits/(abs+K) so the result stays inside (-1, 1).
        """
        tokens = _tokenize(text)
        if not tokens:
            return 0.0

        net = 0.0
        any_hit = False
        # Longer phrases first so a multi-word term ("no wahala") is consumed
        # before its single-word components ("wahala") double-count the same span.
        consumed: set[int] = set()
        for phrase, base in sorted(self._terms.items(), key=lambda kv: -len(kv[0])):
            for start in _phrase_present(tokens, phrase):
                span = set(range(start, start + len(phrase)))
                if span & consumed:
                    continue
                consumed |= span
                any_hit = True
                net += self._weigh_hit(tokens, start, base)

        if not any_hit:
            return 0.0
        return round(net / (abs(net) + _SATURATION_K), 4)

    def _weigh_hit(self, tokens: list[str], start: int, base: float) -> float:
        """Apply negation + modifier heuristics to one term hit.

        Negation flips sign if a negator sits within _NEGATION_WINDOW tokens
        before the term. Intensifier / downtoner within _MODIFIER_WINDOW scales
        magnitude. Both can stack (e.g. "not very good").
        """
        weight = base

        mod_lo = max(0, start - _MODIFIER_WINDOW)
        for j in range(mod_lo, start):
            tok = tokens[j]
            if tok in self._intensifiers:
                weight *= self._intensifiers[tok]
            elif tok in self._downtoners:
                weight *= self._downtoners[tok]

        neg_lo = max(0, start - _NEGATION_WINDOW)
        if any(tokens[j] in self._negators for j in range(neg_lo, start)):
            weight = -weight

        return weight


def _norm_token(word: object) -> str:
    toks = _tokenize(str(word))
    return toks[0] if toks else ""


@lru_cache(maxsize=8)
def load_lexicon(market: str) -> SentimentLexicon:
    """Load and cache the per-market lexicon.

    Missing config returns an empty lexicon that scores every text 0.0 rather
    than raising, so a market without a seed file degrades to neutral instead of
    breaking enrichment. Cached per market so the YAML parse happens once per
    process.
    """
    key = (market or "").strip().lower()
    path = CONFIGS_DIR / "sentiment_lexicons" / f"{key}.yaml"
    config = load_yaml(path) if path.exists() else {}
    return SentimentLexicon(key, config)


def score_text(text: str, market: str) -> float:
    """Convenience wrapper: load the market lexicon and score one text."""
    return load_lexicon(market).score(text)
