"""M4 multilingual-embedding topic classifier (Workstream D2).

The keyword + theme + slang layers in ``topic_classifier`` cap out around
40 percent labelling on the social slices because they can only match
literal tokens. This module is the durable ceiling-fix: it embeds a row's
text with Vertex ``text-multilingual-embedding-002`` and cosine-matches it
against per-topic anchor vectors, so paraphrased / code-switched / slang
content that never utters a taxonomy keyword still classifies.

Design (why a batch pass, not an inline classifier layer):

- ``topic_classifier.classify_topics`` is a PURE per-row function called in
  a tight loop and its purity is a tested contract. Embedding every row
  inline would put thousands of Vertex calls/day inside it and break that
  contract. So this runs as a SEPARATE batch pass over the rows the keyword
  layers left unclassified, in ``enrichment``: collect the residual, embed
  the batch once, match, fill ``topic_groups``.
- Anchor vectors are built from per-topic curated example phrases in
  ``configs/topic_anchors/{market}.yaml`` (5-10 short representative phrases
  drawn from real classified rows, geo-stripped). Each phrase is its own
  vector and a row matches a topic on the BEST (max) cosine over that
  topic's phrases, so a row near any one representative phrase classifies.
  A topic with no curated phrases falls back to its taxonomy description +
  humanised name. Embedded once per process and cached. The v1 anchors were
  the topic descriptions alone; the cloud gate showed they sit too far from
  short social text (2.6 percent lift), hence the curated-phrase rebuild.
- A content-hash cache (sha256 of the text -> vector) dedupes repeated text
  within a single run. The cache file is gitignored and the Cloud Run
  filesystem is ephemeral, so reuse is within-run only; a fresh run starts
  with an empty cache and re-embeds its residual.

Cost control: only the keyword-unclassified residual is embedded (about 60
percent of rows), each text embedded at most once thanks to the cache, at
``text-multilingual-embedding-002`` rates. Gated behind the
``EMBEDDING_CLASSIFIER_ENABLED`` env flag so it ships dark and only spends
once flipped after the offline Safety Gate confirms the lift.

The Vertex client is injectable so unit tests mock the SDK boundary and run
with zero network + zero spend.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Protocol

import yaml

from src.utils.log_redactor import get_logger

logger = get_logger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent
TOPIC_GROUPS_DIR = _ROOT / "configs" / "topic_groups"
TOPIC_ANCHORS_DIR = _ROOT / "configs" / "topic_anchors"
DEFAULT_CACHE_PATH = _ROOT / "data" / "embedding_cache.json"

EMBEDDING_MODEL = "text-multilingual-embedding-002"
DEFAULT_LOCATION = "us-central1"
# Vertex text-multilingual-embedding-002 rejects any predict request with
# more than 250 instances (400 INVALID_ARGUMENT). The residual batch is
# routinely larger (800-900 rows/market), so _embed chunks miss_texts to
# this size. Sending the whole residual in one call silently failed every
# market on the 2026-05-30 cron and the rescue contributed zero rows.
EMBED_BATCH_LIMIT = 250
# The 250-instance cap is not the only request limit. Vertex also rejects a
# request whose texts sum past a ~20k-token aggregate budget (400). A single
# over-long text (a 20-comment YouTube blob, a GDELT V2Themes concat) or a
# full 250-row chunk of average social text can breach it, and the rescue
# wraps the whole market in one try/except so one bad chunk zeroed every
# market's rescue on high-residual days. Cap each text and bound the
# per-request character budget so every chunk stays safely under the cap
# (token estimate ~ chars / 4, so 60k chars ~ 15k tokens).
MAX_CHARS_PER_TEXT = 1000
MAX_CHARS_PER_REQUEST = 60_000
# Cosine threshold for assigning a topic. Tuned in the cloud Safety Gate;
# text-multilingual-embedding-002 cosines compress high for short social
# text so the useful band sits around 0.65-0.70.
DEFAULT_THRESHOLD = 0.65
# Distinctiveness margin: a row is assigned its top topic only when that
# topic beats the runner-up by at least this cosine gap. Generic / noisy
# rows sit near several topics at once (small gap) and are rejected; real
# rows have a clear single peak. This is the precision lever that the
# threshold alone cannot provide given the model's high baseline similarity.
DEFAULT_MARGIN = 0.05
NEAR_MISS_LOW = 0.50

# Per-market (threshold, margin) overrides. A market absent from this map uses
# the instance default (DEFAULT_THRESHOLD, DEFAULT_MARGIN). KE is pinned at the
# global value on purpose: a 2026-06-16 sweep on a 400-row random KE residual
# sample showed lowering KE to 0.62 lifts the label rate 4.2 percent to 10.8
# percent, but the recovered rows are mostly false positives (generic Sheng /
# r/Nairobi chatter magnetized to tech_gemini_ai, plus a Vietnamese
# Gemini-zodiac geo leak), so 0.65 is the precision-safe operating point. The
# real KE lever is anchor curation (the tech_gemini_ai Sheng anchors
# over-attract), not the threshold. See docs/scoring-methodology.md.
MARKET_THRESHOLDS: dict[str, tuple[float, float]] = {
    "ke": (DEFAULT_THRESHOLD, DEFAULT_MARGIN),
}

# Topics whose embedding anchors attract generic in-market identity chatter
# the margin rule cannot separate (the cosine gap for a true match overlaps
# the gap for a generic "I'm from <country>" row). Excluded from the
# embedding-rescue layer per market; they still classify on the keyword and
# slang layers, where they are precise. The cloud gate surfaced KE's
# slang-identity topics (sheng / nyama choma / hustle) vacuuming up generic
# Kenyan rows, so KE drops them from rescue. NG and ZA need no exclusions.
EMBEDDING_RESCUE_EXCLUDE: dict[str, frozenset[str]] = {
    "ke": frozenset({"genz_sheng", "food_nyamachoma", "economy_hustle"}),
}


# Structural-noise markers seen in the residual that carry no topic signal
# (Brand24 follower-count metadata, link-share counts, removed/deleted
# posts). Dropped before embedding so they never over-tag a topic.
_NOISE_PATTERNS = (
    re.compile(r"\(\s*[\d,]{4,}\s+followers", re.I),
    re.compile(r"shared\s+\d+\s+times", re.I),
    re.compile(r"\[\s*removed\b", re.I),
    re.compile(r"\[\s*deleted\s*\]", re.I),
    re.compile(r"removed by (reddit|moderator|a moderator|the)", re.I),
    re.compile(r"^\s*trending on \S+ link", re.I),
)
# Bare praise / greeting that clears nothing on its own. Matched against the
# text after stripping a leading [r/subreddit] tag and trailing punctuation.
_PRAISE_ONLY = re.compile(
    r"^(this is |so |very |really )*"
    r"(beautiful|lovely|nice|good|great|amazing|wonderful|gorgeous|stunning|"
    r"well done|love (it|this)|i love (it|this)|fire|cute|perfect|wow)[\s.!?]*$",
    re.I,
)
_REDDIT_PREFIX = re.compile(r"^\s*\[r/[^\]]+\]\s*", re.I)


def _alpha_len(s: str) -> int:
    """Count alphabetic characters (ignores emoji, digits, punctuation)."""
    return sum(1 for ch in s if ch.isalpha())


def _is_low_signal(text: str) -> bool:
    """True when a residual row is structurally unclassifiable.

    Catches the over-tag sources the cloud gate surfaced: bare greetings /
    one-word praise, hashtag-or-url salad, Brand24 follower-count and
    link-share metadata, and removed/deleted post placeholders. These are
    dropped before embedding so they neither cost a Vertex call nor get
    pulled into a topic by the model's high baseline similarity.
    """
    t = (text or "").strip()
    if _alpha_len(t) < 15:
        return True
    low = t.lower()
    if any(p.search(low) for p in _NOISE_PATTERNS):
        return True
    # Hashtag / mention / url salad: almost no prose once tags are removed.
    stripped = re.sub(r"[#@]\w+|https?://\S+", " ", low)
    if _alpha_len(stripped) < 12:
        return True
    # Bare praise after dropping a [r/sub] tag and any emoji / punctuation
    # (the non-word strip also removes zero-width joiners and emoji).
    core = re.sub(r"[^\w\s.!?']", "", _REDDIT_PREFIX.sub("", t)).strip()
    return bool(_PRAISE_ONLY.match(core))


def _decide(scored: list[tuple[str, float]], threshold: float, margin: float) -> list[str]:
    """Pick the topic for one row from its (topic, cosine) scores.

    Assign the single clearest topic only when it clears ``threshold`` and
    beats the runner-up by at least ``margin``. A row that sits near two or
    more topics at once (gap < margin) is ambiguous and left unclassified.
    Returns a one-element list on a confident match, else ``[]``.
    """
    if not scored:
        return []
    ordered = sorted(scored, key=lambda p: p[1], reverse=True)
    top_topic, top_score = ordered[0]
    if top_score < threshold:
        return []
    second_score = ordered[1][1] if len(ordered) > 1 else 0.0
    if top_score - second_score < margin:
        return []
    return [top_topic]


class _Embedder(Protocol):
    """Minimal embed boundary so tests can inject a fake."""

    def __call__(self, texts: list[str]) -> list[list[float]]: ...


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors. 0.0 on degenerate input."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _anchor_texts_for_market(market: str) -> dict[str, list[str]]:
    """Return the curated example-phrase anchors per topic for a market.

    Reads ``configs/topic_anchors/{market}.yaml`` (5-10 representative
    phrases per topic, curated from real classified rows and geo-stripped).
    The taxonomy set in ``configs/topic_groups/{market}.yaml`` is the
    canonical topic list; any topic missing from the anchors file falls back
    to a single anchor of the humanised topic name plus its description, so
    the classifier still runs when curation is incomplete. Returns
    {topic: [phrase, ...]} with at least one phrase per topic.
    """
    tg_path = TOPIC_GROUPS_DIR / f"{market}.yaml"
    if not tg_path.exists():
        raise FileNotFoundError(f"No topic_groups taxonomy for market={market!r} at {tg_path}")
    with tg_path.open(encoding="utf-8") as f:
        tg_doc = yaml.safe_load(f) or {}
    groups = tg_doc.get("topic_groups", tg_doc) or {}

    curated: dict[str, list[str]] = {}
    anchors_path = TOPIC_ANCHORS_DIR / f"{market}.yaml"
    if anchors_path.exists():
        with anchors_path.open(encoding="utf-8") as f:
            a_doc = yaml.safe_load(f) or {}
        raw = a_doc.get("topic_anchors", a_doc) or {}
        for topic, phrases in raw.items():
            if isinstance(phrases, list):
                clean = [str(p).strip() for p in phrases if str(p).strip()]
                if clean:
                    curated[topic] = clean

    anchors: dict[str, list[str]] = {}
    for topic, body in groups.items():
        if topic in curated:
            anchors[topic] = curated[topic]
            continue
        humanised = topic.split("_", 1)[-1].replace("_", " ")
        desc = ""
        if isinstance(body, dict):
            desc = str(body.get("description") or "").strip()
        anchors[topic] = [f"{humanised}. {desc}".strip() if desc else humanised]
    return anchors


class EmbeddingClassifier:
    """Cosine-match row text against per-topic anchor vectors via Vertex."""

    def __init__(
        self,
        project: str | None = None,
        location: str | None = None,
        embedder: _Embedder | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        margin: float = DEFAULT_MARGIN,
        cache_path: Path | None = None,
        market_thresholds: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        self.project = project or os.environ.get("GCP_PROJECT")
        self.location = location or os.environ.get("VERTEX_LOCATION", DEFAULT_LOCATION)
        self.threshold = threshold
        self.margin = margin
        # Per-market overrides; markets absent fall back to the instance
        # (threshold, margin). Defaults to MARKET_THRESHOLDS so production gets
        # the tuned per-market values without extra wiring.
        self.market_thresholds = (
            MARKET_THRESHOLDS if market_thresholds is None else market_thresholds
        )
        self.cache_path = cache_path or DEFAULT_CACHE_PATH
        # Injected embedder wins (tests). Otherwise a Vertex-backed one is
        # built lazily on first use so importing this module never needs a
        # GCP account or network.
        self._embedder = embedder
        self._anchor_cache: dict[str, dict[str, list[list[float]]]] = {}
        self._vec_cache: dict[str, list[float]] = self._load_cache()

    # -- cache ---------------------------------------------------------------

    def _load_cache(self) -> dict[str, list[float]]:
        try:
            if self.cache_path.exists():
                return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("embedding cache unreadable (%s); starting empty", exc)
        return {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._vec_cache), encoding="utf-8")
        except OSError as exc:
            logger.warning("could not persist embedding cache: %s", exc)

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()

    # -- embedding -----------------------------------------------------------

    def _vertex_embed(self, texts: list[str]) -> list[list[float]]:
        """Lazy Vertex embedder mirroring GeminiClient auth (google-genai)."""
        from google import genai

        if not self.project:
            raise ValueError("GCP_PROJECT not set; cannot embed via Vertex.")
        client = genai.Client(vertexai=True, project=self.project, location=self.location)
        resp = client.models.embed_content(model=EMBEDDING_MODEL, contents=texts)
        return [list(e.values) for e in resp.embeddings]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts, serving repeats + prior runs from the content-hash cache."""
        embedder = self._embedder or self._vertex_embed
        out: list[list[float]] = [[] for _ in texts]
        misses: list[int] = []
        miss_texts: list[str] = []
        for i, t in enumerate(texts):
            cached = self._vec_cache.get(self._key(t))
            if cached is not None:
                out[i] = cached
            else:
                misses.append(i)
                # Cap each text so one over-long row cannot breach the per-text
                # or aggregate token limit. Cached under the original key below.
                miss_texts.append(t[:MAX_CHARS_PER_TEXT])
        if miss_texts:

            def _flush(chunk: list[str]) -> list[list[float]]:
                if not chunk:
                    return []
                try:
                    vecs = embedder(chunk)
                    # The embedder must return exactly one vector per input. A
                    # short (or long) return would misalign every later index
                    # against a neighbour's vector and cache the WRONG vector
                    # under a text hash, a silent permanent mis-classification.
                    # Treat a count mismatch like a failed chunk and raise so the
                    # except below logs it and those rows skip rescue.
                    if len(vecs) != len(chunk):
                        raise ValueError(
                            f"embedder returned {len(vecs)} vectors for {len(chunk)} texts"
                        )
                    return vecs
                except Exception as exc:
                    # Skip just this chunk: its rows get [] vectors (left
                    # unclassified by _cosine) instead of aborting the whole
                    # market's rescue.
                    logger.warning(
                        "embedding chunk of %d texts failed (%s); those rows skip rescue",
                        len(chunk),
                        exc,
                    )
                    return [[] for _ in chunk]

            fresh: list[list[float]] = []
            chunk: list[str] = []
            chunk_chars = 0
            for t in miss_texts:
                if chunk and (
                    len(chunk) >= EMBED_BATCH_LIMIT or chunk_chars + len(t) > MAX_CHARS_PER_REQUEST
                ):
                    fresh.extend(_flush(chunk))
                    chunk, chunk_chars = [], 0
                chunk.append(t)
                chunk_chars += len(t)
            fresh.extend(_flush(chunk))
            for idx, vec in zip(misses, fresh, strict=True):
                out[idx] = list(vec)
                # Only cache real vectors; a skipped chunk's [] must not poison
                # the cache so a later run can retry those rows.
                if vec:
                    self._vec_cache[self._key(texts[idx])] = list(vec)
            self._save_cache()
        return out

    def _anchors(self, market: str) -> dict[str, list[list[float]]]:
        """Return {topic: [phrase_vector, ...]}, embedded once and cached.

        All phrases across all topics are embedded in a single batch (served
        from the content-hash cache on repeats), then regrouped per topic so
        ``classify_batch`` can max-pool a row's cosine over a topic's
        phrases. Topics in ``EMBEDDING_RESCUE_EXCLUDE`` for the market are
        skipped so they are never embedding-rescue candidates.
        """
        if market not in self._anchor_cache:
            anchor_texts = _anchor_texts_for_market(market)
            excluded = EMBEDDING_RESCUE_EXCLUDE.get(market, frozenset())
            flat: list[str] = []
            spans: list[tuple[str, int, int]] = []
            for topic, phrases in anchor_texts.items():
                if topic in excluded:
                    continue
                start = len(flat)
                flat.extend(phrases)
                spans.append((topic, start, len(flat)))
            vectors = self._embed(flat) if flat else []
            self._anchor_cache[market] = {topic: vectors[s:e] for topic, s, e in spans}
        return self._anchor_cache[market]

    # -- public --------------------------------------------------------------

    def classify_batch(self, texts: list[str], market: str) -> list[list[str]]:
        """Return the topic assigned to each text, ``[]`` when unclassified.

        Low-signal rows (``_is_low_signal``) are dropped before embedding so
        they cost nothing and cannot over-tag. The rest are max-pooled
        against each topic's phrase vectors and resolved by ``_decide``
        (clear the threshold AND beat the runner-up by ``margin``), yielding
        at most one confident topic per row.
        """
        if not texts:
            return []
        anchors = self._anchors(market)
        if not anchors:
            return [[] for _ in texts]
        topics = list(anchors)
        results: list[list[str]] = [[] for _ in texts]
        keep_idx = [i for i, t in enumerate(texts) if not _is_low_signal(t)]
        if not keep_idx:
            return results
        row_vecs = self._embed([texts[i] for i in keep_idx])
        thr, mar = self.market_thresholds.get(market, (self.threshold, self.margin))
        for i, rv in zip(keep_idx, row_vecs, strict=False):
            # Max-pool: a topic's score is the best cosine over its phrases.
            scored = [(t, max((_cosine(rv, v) for v in anchors[t]), default=0.0)) for t in topics]
            results[i] = _decide(scored, thr, mar)
        return results

    def classify_batch_scored(
        self, texts: list[str], market: str
    ) -> tuple[list[list[str]], list[list[tuple[str, float]]]]:
        """Additive variant: same topic lists plus per-row top-2 (topic, cosine)."""
        topics_out = self.classify_batch(texts, market)
        scored_side: list[list[tuple[str, float]]] = [[] for _ in texts]
        if not texts:
            return topics_out, scored_side

        anchors = self._anchors(market)
        if not anchors:
            return topics_out, scored_side

        topics = list(anchors)
        keep_idx = [i for i, t in enumerate(texts) if not _is_low_signal(t)]
        if not keep_idx:
            return topics_out, scored_side

        row_vecs = self._embed([texts[i] for i in keep_idx])
        for i, rv in zip(keep_idx, row_vecs, strict=False):
            if not rv:
                continue
            scored = [(t, max((_cosine(rv, v) for v in anchors[t]), default=0.0)) for t in topics]
            scored.sort(key=lambda x: -x[1])
            top2 = [(t, round(c, 4)) for t, c in scored[:2] if c >= NEAR_MISS_LOW]
            scored_side[i] = top2
        return topics_out, scored_side


def is_enabled() -> bool:
    """Whether the embedding pass is flipped on (dark by default)."""
    return os.environ.get("EMBEDDING_CLASSIFIER_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
