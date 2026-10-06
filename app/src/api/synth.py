"""Vertex Gemini synthesis, momentum math, and the in-process answer cache.

One model call per fresh query. The model only summarizes retrieved evidence:
it never sets momentum, never invents slang, and its JSON is validated with a
single retry before the caller degrades to the thin-signal layout.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from urllib.parse import urlsplit

logger = logging.getLogger("listening_post.synth")

# Bound a single Vertex call so a stuck model request cannot hang an async Ask
# job forever (the job key would stay pending in _brief_jobs until restart).
# Milliseconds, the unit google-genai HttpOptions.timeout expects.
_VERTEX_TIMEOUT_MS = 120_000

_client = None
_CACHE: dict = {}
_CACHE_MAX_ENTRIES = 200
# The cache is read and mutated from request threads and from the daemon job
# threads that resolve async Ask briefs, so every access is guarded. Without
# this the size-cap eviction can race and drop or corrupt an entry under load.
_CACHE_LOCK = threading.RLock()

# Lazy GCS client for the persistent cache layer. None means not yet built;
# False means the build failed and GCS stays off for the process lifetime.
_storage_client = None


LEGACY_GENERATION_REFUSED = "legacy_vertex_generation_refused"


def legacy_generation_refused() -> bool:
    """True where the deployment profile forbids this module's model seam.

    On the staging profile only the reviewed general question engine may call
    the model, so every generation path in this module refuses there."""
    return os.environ.get("DEPLOYMENT_PROFILE") == "open-intelligence-staging"


def _require_legacy_vertex_generation() -> None:
    if legacy_generation_refused():
        raise RuntimeError(LEGACY_GENERATION_REFUSED)


AUDIENCE_EVIDENCE_RULE = (
    "Audience neutral by default. Do not infer age or demographics from a platform, "
    "post, creator, topic, writing style, or model output. Make an audience claim only "
    "when the supplied evidence names a measured source, collection window, sample, "
    "method, and confidence."
)

PROMPT_CONTRACT = (
    """You are given {n} real posts and aggregate stats about "{query}" from ZA/NG/KE sources over the last 30 days.

Cluster the evidence into 2-4 distinct trends. For each: a one-sentence trend statement in plain sharp English; 2-3 indices of the posts that evidence it; one non-branded "how to use it" line for a marketing planner; the dominant platforms and markets from the stats.

Also return: up to 6 slang terms FROM THE POSTS with plain meanings, and a two-sentence overall read.

Hard rules: never invent a post, statistic, or slang term; only reference provided evidence; if the evidence does not support 2 clear trends, return fewer or none and say why in one sentence; momentum is not yours to claim.

Return strict JSON with exactly this shape:
{{"trends": [{{"statement": "...", "evidence_indices": [0, 1], "how_to_use": "...", "platforms": ["..."], "markets": ["..."]}}], "slang": [{{"term": "...", "meaning": "..."}}], "overall_read": "..."}}
"""
    + "\n\n"
    + AUDIENCE_EVIDENCE_RULE
)


def _cache_ttl() -> float:
    try:
        return float(os.environ.get("CACHE_TTL", "86400"))
    except ValueError:
        return 86400.0


def _safe_key(key) -> str:
    """A GCS-safe object name for a cache key tuple. The readable slug keeps
    the bucket browsable; the digest keeps distinct keys from colliding."""
    parts = key if isinstance(key, tuple) else (key,)
    text = "|".join(str(p) for p in parts)
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text)[:100].strip("_")
    digest = hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return (slug + "-" if slug else "") + digest


def _gcs_bucket():
    """The cache bucket, or None when GCS is off (empty CACHE_BUCKET) or the
    client cannot be built. Every failure is swallowed: the persistent layer
    is an optimization, never a dependency."""
    name = os.environ.get("CACHE_BUCKET", "listening-post-cache")
    if not name:
        return None
    global _storage_client
    if _storage_client is None:
        try:
            from google.cloud import storage

            _storage_client = storage.Client(
                project=os.environ.get("GCP_PROJECT") or None
            )
        except Exception:
            _storage_client = False
    if _storage_client is False:
        return None
    try:
        return _storage_client.bucket(name)
    except Exception:
        return None


def cache_object_name(key) -> str:
    prefix = os.environ.get("CACHE_PREFIX", "")
    if not prefix:
        prefix = "cache/"
    return prefix + _safe_key(key) + ".json"


def _gcs_write(key, value) -> None:
    bucket = _gcs_bucket()
    if bucket is None:
        return
    try:
        blob = bucket.blob(cache_object_name(key))
        blob.upload_from_string(
            json.dumps(value, default=str), content_type="application/json"
        )
    except Exception:
        logger.warning("GCS cache write failed for %r", key)


def _gcs_read(key):
    """A still-fresh value from the bucket, or None. Freshness comes from the
    blob's own updated stamp against the same TTL the in-process cache uses;
    a hit is rehydrated locally with its remaining TTL preserved."""
    bucket = _gcs_bucket()
    if bucket is None:
        return None
    try:
        blob = bucket.get_blob(cache_object_name(key))
        if blob is None or blob.updated is None:
            return None
        age = time.time() - blob.updated.timestamp()
        if age >= _cache_ttl():
            return None
        value = json.loads(blob.download_as_text())
    except Exception:
        logger.warning("GCS cache read failed for %r", key)
        return None
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() - age, value)
    return value


def cache_get(key):
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is not None:
            stored_at, value = entry
            if time.time() - stored_at < _cache_ttl():
                return value
            _CACHE.pop(key, None)
    # GCS read is network IO; do it outside the lock. _gcs_read rehydrates
    # _CACHE under its own brief access, which the RLock keeps consistent.
    return _gcs_read(key)


def cache_set(key, value, ttl=None) -> None:
    """Insert with a hard size cap: beyond it, the oldest entry is evicted.
    The value also lands in the GCS cache bucket so a new instance starts
    warm; that write is best-effort and never fails the caller.

    A ttl shorter than the global one is expressed by backdating the stored
    stamp; such entries (recorded failures) stay in-process only and never
    reach GCS, so a transient outage cannot warm-start as a pinned error."""
    with _CACHE_LOCK:
        if key not in _CACHE and len(_CACHE) >= _CACHE_MAX_ENTRIES:
            oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
            _CACHE.pop(oldest, None)
        stored_at = time.time()
        if ttl is not None:
            stored_at -= max(0.0, _cache_ttl() - float(ttl))
        _CACHE[key] = (stored_at, value)
    if ttl is None:
        _gcs_write(key, value)


def compute_momentum(counts) -> str:
    """Momentum from the daily volume series, computed here and only here.

    Last-7-day mean vs prior-14-day mean: above +25% Rising, below -25%
    Cooling, else Steady. Building when the prior base is low (under 5 a day)
    but the volume is accelerating, and only when the history carries enough
    real days to trust: a couple of nonzero points padded with leading zeros
    reads Steady, never a false Building."""
    counts = [int(c) for c in counts]
    # A history with fewer than this many days of actual volume is too thin to
    # claim acceleration; the leading-zero padding would otherwise drag the
    # prior mean to ~0 and inflate Building on two or three stray points.
    MIN_BUILDING_POINTS = 5
    nonzero_days = sum(1 for c in counts if c > 0)
    if len(counts) < 21:
        counts = [0] * (21 - len(counts)) + counts
    last_mean = sum(counts[-7:]) / 7.0
    prior_mean = sum(counts[-21:-7]) / 14.0
    if prior_mean < 5.0 and nonzero_days >= MIN_BUILDING_POINTS:
        if prior_mean == 0.0:
            return "Building" if last_mean > 0 else "Steady"
        if last_mean >= prior_mean * 1.25:
            return "Building"
    if prior_mean == 0.0:
        return "Steady"
    change = (last_mean - prior_mean) / prior_mean
    if change > 0.25:
        return "Rising"
    if change < -0.25:
        return "Cooling"
    return "Steady"


def _slang_corpus(items):
    """Terms the engine tagged on the retrieved items, plus the raw text blob.
    Only terms found here may surface; the model cannot add glossary entries."""
    terms = set()
    blob = " ".join((it.get("text") or "").lower() for it in items)
    for it in items:
        raw = it.get("slang_terms") or ""
        for t in re.split(r"[,;|]", raw):
            t = t.strip().lower()
            if t:
                terms.add(t)
    return terms, blob


def build_prompt(query: str, items: list, aggregates: dict) -> str:
    head = PROMPT_CONTRACT.format(n=len(items), query=query)
    stats = json.dumps(aggregates, default=str)
    lines = []
    for i, it in enumerate(items):
        text = (it.get("text") or "")[:300]
        lines.append(
            "[{}] ({} | {} | engagement {}) {}".format(
                i, it.get("platform"), it.get("market"), it.get("engagement"), text
            )
        )
    return head + "\nAGGREGATE STATS:\n" + stats + "\n\nPOSTS:\n" + "\n".join(lines)


# A Vertex model id: lowercase letters, digits, dots, hyphens. Anything else
# in OPPORTUNITY_MODEL is treated as invalid and the main model takes over.
_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")


def _opportunity_model() -> str:
    """The model for the small opportunity prompt. OPPORTUNITY_MODEL when it
    looks like a real model id, the main GEMINI_MODEL otherwise."""
    name = os.environ.get("OPPORTUNITY_MODEL", "gemini-2.5-flash-lite").strip()
    if _MODEL_ID_RE.match(name):
        return name
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def _call_model(prompt: str, model: str | None = None) -> str:
    """Single Vertex Gemini call. Vertex endpoint only, lazy client."""
    _require_legacy_vertex_generation()
    from google import genai
    from google.genai import types

    global _client
    if _client is None:
        _client = genai.Client(
            vertexai=True,
            project=os.environ["GCP_PROJECT"],
            location=os.environ.get("GEMINI_LOCATION", "us-central1"),
            http_options=types.HttpOptions(timeout=_VERTEX_TIMEOUT_MS),
        )
    response = _client.models.generate_content(
        model=model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    return response.text or ""


def _validate(data, n_items, platforms, markets, slang_terms, slang_blob):
    """Enforce the response schema and the no-fabrication rules.

    Returns the cleaned dict, or None when the response is unusable and the
    caller should retry or degrade."""
    if not isinstance(data, dict):
        return None
    trends_raw = data.get("trends")
    if not isinstance(trends_raw, list):
        return None

    trends = []
    for t in trends_raw[:4]:
        if not isinstance(t, dict):
            continue
        statement = t.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            continue
        how = t.get("how_to_use")
        if not isinstance(how, str):
            how = ""
        idx_raw = t.get("evidence_indices")
        if not isinstance(idx_raw, list):
            continue
        indices = sorted(
            {i for i in idx_raw if isinstance(i, int) and 0 <= i < n_items}
        )
        if not indices:
            continue
        plats = [
            p
            for p in (t.get("platforms") or [])
            if isinstance(p, str) and p.lower() in platforms
        ]
        mkts = [
            m
            for m in (t.get("markets") or [])
            if isinstance(m, str) and m.lower() in markets
        ]
        trends.append(
            {
                "statement": statement.strip(),
                "how_to_use": how.strip(),
                "evidence_indices": indices,
                "platforms": plats,
                "markets": mkts,
            }
        )

    slang = []
    for s in (data.get("slang") or [])[:6]:
        if not isinstance(s, dict):
            continue
        term = s.get("term")
        meaning = s.get("meaning")
        if not isinstance(term, str) or not isinstance(meaning, str):
            continue
        term = term.strip()
        if term and (term.lower() in slang_terms or term.lower() in slang_blob):
            slang.append({"term": term, "meaning": meaning.strip()})

    read = data.get("overall_read")
    if not isinstance(read, str) or not read.strip():
        return None
    return {"trends": trends, "slang": slang, "overall_read": read.strip()}


# The engine writes no opportunity field; PULSE generates it once per
# (topic, trend_date) and caches the success in the same TTL cache the ask
# answers use. A failure returns "" and is never cached, so a transient
# outage cannot pin an empty lens for a whole day.
OPPORTUNITY_PROMPT = (
    "You are 42, a Sub-Saharan Africa (South Africa, Nigeria, Kenya) cultural intelligence "
    "desk. Find the evidence-backed decision opportunity in the supplied signal.\n\n"
    + AUDIENCE_EVIDENCE_RULE
    + "\n\n"
    "TOPIC SYNTHESIS:\n{synthesis}\n\n"
    "CULTURAL CONTEXT:\n{context}\n\n"
    "Return ONLY a valid JSON object (no markdown, no prose) with exactly this shape:\n"
    '{{"opportunity": "<at most 2 sentences: the hidden angle everyone else is '
    "overlooking, grounded only in the synthesis and context above, and the next useful "
    'investigation or response>"}}'
)


def generate_opportunity(
    topic_id: str, trend_date: str, synthesis: str, context: str
) -> str:
    """One model call per (topic, trend_date), cached on success.

    Returns "" when the inputs are empty, the call fails, or the response is
    unusable; the caller drops the field rather than showing filler."""
    if not (synthesis or context):
        return ""
    key = ("opp", topic_id, trend_date)
    cached = cache_get(key)
    if cached is not None:
        return cached
    prompt = OPPORTUNITY_PROMPT.format(synthesis=synthesis or "", context=context or "")
    try:
        raw = _call_model(prompt, _opportunity_model())
        data = json.loads(raw)
    except Exception:
        logger.warning("opportunity generation failed for %r", topic_id)
        return ""
    opp = data.get("opportunity") if isinstance(data, dict) else None
    if not isinstance(opp, str) or not opp.strip():
        return ""
    opp = opp.strip()
    cache_set(key, opp)
    return opp


REGION_NAMES = {"ZA": "South Africa", "NG": "Nigeria", "KE": "Kenya"}
BRIEF_MOMENTA = ("rising", "building", "steady", "cooling")
BRIEF_VELOCITIES = ("Low", "Medium", "High", "Very high")

# The Signal Desk live-brief contract. The JSON shape is appended verbatim so
# the format() braces never collide with it.
BRIEF_PROMPT_HEAD = (
    "You are 42, a Sub-Saharan Africa (Nigeria, Kenya, South Africa) cultural intelligence "
    "desk writing an evidence-bound strategist brief.\n\n"
    + AUDIENCE_EVIDENCE_RULE
    + "\n\n"
    "Turn the observed signal into a bounded decision: what is happening, why it matters, "
    "what remains uncertain, and one culturally specific response.\n\n"
    "{market_line}\n\n"
    'For the topic "{query}", return ONLY a valid JSON object (no markdown, no prose) '
    "with EXACTLY this shape:\n"
)

BRIEF_JSON_SHAPE = (
    "{\n"
    '  "region": "ZA" | "NG" | "KE",\n'
    '  "label": "<3-5 word category, e.g. \'Sound & party culture\'>",\n'
    '  "momentum": "rising" | "building" | "steady" | "cooling",\n'
    '  "sentiment": <number -1..1>,\n'
    '  "velocity": "Low" | "Medium" | "High" | "Very high",\n'
    '  "why": "<one sentence, max 18 words, why it is moving now>",\n'
    '  "voices": [],\n'
    '  "trend": "<2 sentences: what observed signal is picking up speed in this market>",\n'
    '  "relevance": "<2 sentences: what the data says about scale, fad or moment, '
    'for this market>",\n'
    '  "opportunity": "<2 sentences: the hidden angle and the next decision it supports>",\n'
    '  "ideaFormat": "<editorial, participatory, service, partnership, or investigation>",\n'
    '  "idea": "<1-2 sentences: a culturally specific response grounded in the signal>",\n'
    '  "visualDirection": "<optional concise visual direction, or empty string>",\n'
    '  "audioDirection": "<optional concise audio direction, or empty string>"\n'
    "}\n"
    "Be specific and local. Output JSON only."
)


def build_brief_prompt(query: str, region_pref: str) -> str:
    """The live-brief prompt with the region-forcing line baked in."""
    forced = region_pref in ("za", "ng", "ke")
    if forced:
        reg = region_pref.upper()
        name = REGION_NAMES[reg]
        market_line = (
            f'The user is on the {name} desk. The region MUST be "{reg}" and every '
            f"part of the brief must be specific to {name}: local slang, cities, "
            "platforms, culture."
        )
    else:
        market_line = (
            "Pick the single most culturally relevant region among ZA, NG, KE."
        )
    return (
        BRIEF_PROMPT_HEAD.format(market_line=market_line, query=query)
        + BRIEF_JSON_SHAPE
    )


def _brief_str(data: dict, key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


_AUDIENCE_CLAIM_RE = re.compile(
    r"\b(?:gen\s*z|women|woman|men|male|female|boys?|girls?|demographic|"
    r"aged?\s+\d{1,2}|ages?\s+\d{1,2}|under\s+\d{1,2}|over\s+\d{1,2}|"
    r"\d{1,2}\s*(?:to|[-])\s*\d{1,2}\s*(?:year|yr))\b",
    re.I,
)


def _complete_audience_measurement(value) -> bool:
    if not isinstance(value, dict):
        return False
    window = value.get("window")
    if isinstance(window, dict):
        window_ok = bool(window.get("start") and window.get("end"))
    else:
        window_ok = bool(str(window or "").strip())
    sample = value.get("sample_size")
    confidence = value.get("confidence")
    return bool(
        str(value.get("source") or "").strip()
        and window_ok
        and isinstance(sample, int)
        and not isinstance(sample, bool)
        and sample > 0
        and str(value.get("method") or "").strip()
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and 0 <= float(confidence) <= 1
    )


def _audience_claim_supported(text: str, refs: list) -> bool:
    if not isinstance(text, str) or not _AUDIENCE_CLAIM_RE.search(text):
        return True
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        candidates = [ref.get("audience_measurement"), ref.get("audience_lens")]
        lenses = ref.get("audience_lenses")
        if isinstance(lenses, list):
            candidates.extend(lenses)
        if any(_complete_audience_measurement(candidate) for candidate in candidates):
            return True
    return False


def _validate_brief(data, forced_region):
    """Clamp the live-brief response to the contract. Returns None when the
    response is not even a dict, so the caller can retry once."""
    if not isinstance(data, dict):
        return None
    if not _audience_claim_supported(json.dumps(data, ensure_ascii=False), []):
        return None
    region = forced_region
    if region is None:
        candidate = data.get("region")
        region = candidate if candidate in REGION_NAMES else "ZA"
    momentum = data.get("momentum")
    if momentum not in BRIEF_MOMENTA:
        momentum = None
    sentiment = data.get("sentiment")
    sentiment = (
        max(-1.0, min(1.0, float(sentiment)))
        if isinstance(sentiment, (int, float))
        else 0.0
    )
    velocity = data.get("velocity")
    if velocity not in BRIEF_VELOCITIES:
        velocity = "Medium"
    idea_tool = _brief_str(data, "ideaFormat") or "Editorial response"
    return {
        "region": region,
        "label": _brief_str(data, "label") or "Cultural signal",
        "momentum": momentum,
        "sentiment": sentiment,
        "velocity": velocity,
        "why": _brief_str(data, "why"),
        "trend": _brief_str(data, "trend"),
        "relevance": _brief_str(data, "relevance"),
        "opportunity": _brief_str(data, "opportunity"),
        "ideaTool": idea_tool,
        "idea": _brief_str(data, "idea"),
        "promptNano": _brief_str(data, "visualDirection"),
        "promptLyria": _brief_str(data, "audioDirection"),
    }


def synthesize_brief(query: str, region_pref: str):
    """One Gemini call for the Signal Desk live brief, two retries on a
    failure or a malformed response. Returns the clamped dict or None.

    The model writes prose only: every number the client sees comes from the
    retrieval aggregates, and the cited voices are replaced with real posts."""
    prompt = build_brief_prompt(query, region_pref)
    forced = region_pref.upper() if region_pref in ("za", "ng", "ke") else None
    for attempt in (1, 2, 3):
        try:
            raw = _call_model(prompt)
            data = json.loads(raw)
        except Exception:
            logger.warning("brief synthesis attempt %d failed for %r", attempt, query)
            continue
        cleaned = _validate_brief(data, forced)
        if cleaned is not None:
            return cleaned
        logger.warning(
            "brief synthesis attempt %d returned an invalid shape for %r",
            attempt,
            query,
        )
    return None


REFINE_PROMPT = (
    "You are 42, refining an existing cultural signal brief for the {region_name} "
    'market (topic: "{topic}").\n' + AUDIENCE_EVIDENCE_RULE + "\n"
    "Keep every claim tied to the supplied brief. Stay specific to {region_name}.\n\n"
    "CURRENT BRIEF: {current}\n\n"
    'REFINEMENT INSTRUCTION: "{instruction}"\n\n'
    "Apply the instruction and return ONLY a valid JSON object (no markdown, no "
    "prose) with EXACTLY these keys: why, label, trend, relevance, opportunity, "
    "ideaFormat, idea, visualDirection, audioDirection. Rewrite "
    "only what the instruction touches; keep the rest consistent. Never invent "
    "statistics or quotes. Output JSON only."
)

REFINE_KEYS = (
    "why",
    "label",
    "trend",
    "relevance",
    "opportunity",
    "idea",
    "visualDirection",
    "audioDirection",
)


def refine_brief_prose(topic: dict, instruction: str):
    """One Gemini call to rework a generated brief's prose, one retry on a
    malformed response. Returns the cleaned prose dict or None.

    Only prose comes back: the caller keeps every number, the series, the
    receipts, and the real cited voices from the original topic."""
    region = str(topic.get("region") or "").upper()
    region_name = REGION_NAMES.get(region, "Sub-Saharan Africa")
    brief = topic.get("brief") if isinstance(topic.get("brief"), dict) else {}
    idea = brief.get("idea") if isinstance(brief.get("idea"), dict) else {}
    current = {
        "why": topic.get("why"),
        "label": topic.get("label"),
        "trend": brief.get("trend"),
        "relevance": brief.get("relevance"),
        "opportunity": brief.get("opportunity"),
        "idea": idea.get("text"),
    }
    safe_instruction = instruction.replace("{", "{{").replace("}", "}}")
    prompt = REFINE_PROMPT.format(
        region_name=region_name,
        topic=topic.get("topic") or "",
        current=json.dumps(current),
        instruction=safe_instruction,
    )
    for attempt in (1, 2):
        try:
            raw = _call_model(prompt)
            data = json.loads(raw)
        except Exception:
            logger.warning(
                "refine attempt %d failed for %r", attempt, topic.get("topic")
            )
            continue
        if not isinstance(data, dict):
            logger.warning(
                "refine attempt %d returned an invalid shape for %r",
                attempt,
                topic.get("topic"),
            )
            continue
        if not _audience_claim_supported(json.dumps(data, ensure_ascii=False), []):
            continue
        cleaned = {key: _brief_str(data, key) for key in REFINE_KEYS[:-2]}
        cleaned["ideaTool"] = _brief_str(data, "ideaFormat") or "Editorial response"
        cleaned["promptNano"] = _brief_str(data, "visualDirection")
        cleaned["promptLyria"] = _brief_str(data, "audioDirection")
        return cleaned
    return None


def synthesize(query: str, items: list, aggregates: dict):
    """One Gemini call with one retry on a malformed response.

    Returns {"trends", "slang", "overall_read"} or None when both attempts
    fail, in which case the caller renders the thin-signal layout with stats
    only."""
    platforms = {
        str(e.get("platform") or "").lower()
        for e in aggregates.get("platform_split", [])
    }
    markets = {
        str(e.get("market") or "").lower() for e in aggregates.get("market_split", [])
    }
    slang_terms, slang_blob = _slang_corpus(items)
    prompt = build_prompt(query, items, aggregates)

    for attempt in (1, 2):
        try:
            raw = _call_model(prompt)
            data = json.loads(raw)
        except Exception:
            logger.warning("synthesis attempt %d failed for %r", attempt, query)
            continue
        cleaned = _validate(
            data, len(items), platforms, markets, slang_terms, slang_blob
        )
        if cleaned is not None:
            return cleaned
        logger.warning(
            "synthesis attempt %d returned an invalid shape for %r", attempt, query
        )
    return None


RESEARCH_JSON_SHAPE = """
Return strict JSON with exactly this shape:
{
  "title": "Market Research Perspective: ...",
  "grounded": {
    "objective": {"text": "Objective: marketing problem or opportunity with metrics from refs", "evidence_indices": [1, 2]},
    "user_insights": [
      {"text": "Background insight naming market, cultural mechanic, and metric from refs", "evidence_indices": [1]}
    ],
    "voice_read": []
  },
  "inference": {
    "tension": "The Tension: name the frame (e.g. Capability vs. Constriction) and one paragraph on hustle fatigue or friction",
    "goal": "The decision this evidence supports, with market differences where present",
    "get_to_by": {"get": "GET: current evidence-bound state", "to": "TO: desired response", "by": "BY: action grounded in the supplied signal"},
    "brand_safe_notes": "Resonance: inclusivity, human truths, authenticity we find not manufacture",
    "mandatories": "Tone and production guardrails for this persona and markets",
    "creative_judge": "How we judge the response against the evidence and limitations",
    "confidence": "low|moderate|high",
    "provenance": "inference"
  },
  "activation": {
    "responses": [
      {
        "market": "NG",
        "user_friction": "daily-life friction the user is trying to solve",
        "behaviour_trigger": "behaviour or topic from cited refs that opens the idea",
        "response": "bounded action or investigation naming specific trends from the cited refs",
        "evidence_indices": [2, 4],
        "provenance": "inference"
      }
    ],
    "provenance": "inference"
  }
}

Hard rules:
- evidence_indices are 1-based into the numbered refs list. objective and every user_insights bullet MUST cite at least one index. Never duplicate the same index twice in one list.
- INTELLIGENCE FIRST: objective, user_insights, and tension MUST cite brief, seed, digest, search, or lexicon refs as primary evidence. Post and comment refs are voice proof only: at most one post index per bullet and never as the sole citation for objective or tension.
- objective MUST state a problem or opportunity with at least one metric from cited refs in plain English (e.g. "search velocity 0.72", "mention volume up"). Never paste raw key=value pairs such as trend_score=0.3751 or velocity=0.0749 from the evidence header.
- Each user_insights bullet MUST name a market (ZA/NG/KE or country name) AND cite a metric OR named topic/query_group from the cited refs.
- tension MUST name a titled frame (X vs. Y), tie to cultural mechanics from refs, and name at least one market difference when multiple markets are in scope.
- get_to_by get/to/by are each one sentence and stay within the supplied evidence.
- responses MUST include user_friction, behaviour_trigger, response, and evidence_indices. Each response MUST be a distinct bounded action or investigation, not a rephrase of goal or positioning.
- Never invent stats, handles, URLs, topics, products, audiences, age groups, or gender claims not in the refs. If evidence is thin, set activation.responses to [] and say so in goal.
"""

MIN_ACTIVATION_PROMPT_CHARS = 80
MIN_BEHAVIOUR_BULLET_CHARS = 35

_MARKET_NAMES = {
    "za": "south africa",
    "ng": "nigeria",
    "ke": "kenya",
}

_GENERIC_ACTIVATION_RE = re.compile(
    r"\b(social media captions?|write.{0,20}humou?r|viral content|engaging posts?|"
    r"funny memes?|generic campaign|created with gemini tag|tag gemini|end-card)\b",
    re.I,
)

_RESEARCH_BANNED_PRODUCT_RE = re.compile(
    r"\b(nano banana|gemini nano banana|banana pro|gemini banana|nano\s+banana)\b",
    re.I,
)

_BQ_SCALAR_IN_PROSE_RE = re.compile(
    r"\b(?:trend_score|velocity_score|search_velocity_score|search_velocity|velocity|"
    r"engagement_total|mention_count|relevance_score|item_count)\s*=\s*[\d.]+\b",
    re.I,
)

_RESEARCH_PRODUCT_BASE: tuple[str, ...] = ()


def _research_allowed_products(evidence: dict) -> list[str]:
    return []


def _dedupe_evidence_indices(raw) -> list[int]:
    if not isinstance(raw, list):
        return []
    seen: set[int] = set()
    out: list[int] = []
    for i in raw:
        if not isinstance(i, int) or i in seen:
            continue
        seen.add(i)
        out.append(i)
    return sorted(out)


def _collect_cited_ref_indices(doc_json: dict) -> list[int]:
    """1-based ref indices cited anywhere in the research doc."""
    indices: list[int] = []
    grounded = (
        doc_json.get("grounded") if isinstance(doc_json.get("grounded"), dict) else {}
    )
    objective = grounded.get("objective")
    if isinstance(objective, dict):
        indices.extend(objective.get("evidence_indices") or [])
    for bullet in (
        grounded.get("user_insights") or grounded.get("behavioural_synthesis") or []
    ):
        if isinstance(bullet, dict):
            indices.extend(bullet.get("evidence_indices") or [])
    activation = (
        doc_json.get("activation")
        if isinstance(doc_json.get("activation"), dict)
        else {}
    )
    for prompt in activation.get("responses") or []:
        if isinstance(prompt, dict):
            indices.extend(prompt.get("evidence_indices") or [])
    storyboard = activation.get("storyboard")
    if isinstance(storyboard, dict):
        indices.extend(storyboard.get("evidence_indices") or [])
    for sec in doc_json.get("behaviour_sections") or []:
        if isinstance(sec, dict):
            indices.extend(sec.get("evidence_indices") or [])
    return _dedupe_evidence_indices(indices)


def _ref_display_snippet(ref: dict) -> str:
    """Mirror frontend sourceSnippet / evidenceLabel field precedence."""
    if not isinstance(ref, dict):
        return ""
    detail_lines = ref.get("detail_lines")
    if isinstance(detail_lines, list):
        joined = "; ".join(
            str(line).strip() for line in detail_lines if str(line).strip()
        )
        if joined:
            rt = str(ref.get("ref_type") or "").lower()
            if rt == "lexicon":
                title = str(
                    ref.get("title") or ref.get("name") or ref.get("term") or ""
                ).strip()
                if title and title.lower() not in joined.lower():
                    return f"{title}: {joined}"
            return joined
    rt = str(ref.get("ref_type") or "").lower()
    if rt in ("brief", "digest", "seed"):
        parts = []
        headline = str(ref.get("headline") or ref.get("behaviour") or "").strip()
        if headline:
            parts.append(headline)
        syn = str(
            ref.get("trend_synthesis")
            or ref.get("cultural_context")
            or ref.get("the_shift")
            or ""
        ).strip()
        if syn:
            parts.append(syn[:220])
        if parts:
            return " · ".join(parts)
    if rt in ("post", "comment"):
        text = str(ref.get("text") or "").strip()
        if text:
            return text
    for key in (
        "text",
        "snippet",
        "excerpt",
        "trend_synthesis",
        "cultural_context",
        "headline",
        "behaviour",
        "name",
        "title",
        "term",
        "label",
        "topic",
        "query_group",
    ):
        val = str(ref.get(key) or "").strip()
        if val:
            if key == "query_group":
                return val.replace("_", " ")
            return val
    return ""


def _render_research_ref_items(refs: list, cited_indices: list[int]) -> list[str]:
    """Footer ref rows for cited indices only; skip empty shells."""
    items: list[str] = []
    for i in cited_indices:
        if i < 1 or i > len(refs):
            continue
        ref = refs[i - 1]
        if not isinstance(ref, dict):
            continue
        snippet = _ref_display_snippet(ref)
        if not snippet:
            continue
        url = str(ref.get("url") or ref.get("link") or "")
        link_html = ""
        # Only a web URL becomes a link; any other scheme stays out of the href.
        if _is_web_link(url):
            link_html = (
                f' <a class="ref-link" href="{_html_esc(url)}">{_html_esc(url)}</a>'
            )
        items.append(
            f'<li class="ref-item" id="ref-{i}">'
            f'<span class="ref-num">{i}</span>'
            f'<p class="ref-text">{_html_esc(snippet)}{link_html}</p></li>'
        )
    return items


def _strip_bq_scalars(text: str) -> str:
    cleaned = _BQ_SCALAR_IN_PROSE_RE.sub("", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;])", r"\1", cleaned)
    return cleaned.strip()


def _research_product_ok(text: str, allowed: list[str]) -> bool:
    if _RESEARCH_BANNED_PRODUCT_RE.search(text):
        return False
    blob = text.lower()
    for banned in ("nano banana", "gemini nano", "banana pro"):
        if banned in blob:
            return False
    return True


def _clean_research_prose(
    text: str, allowed_products: list[str], refs: list | None = None
) -> str | None:
    if not isinstance(text, str):
        return None
    cleaned = _strip_bq_scalars(text.strip())
    if len(cleaned) < 10:
        return None
    if not _research_product_ok(cleaned, allowed_products):
        return None
    if not _audience_claim_supported(cleaned, refs or []):
        return None
    return cleaned


def _activation_distinct_from_strategy(prompt: str, strategy_texts: list[str]) -> bool:
    blob = prompt.lower()
    prompt_words = set(re.findall(r"[a-z]{5,}", blob))
    for pos in strategy_texts:
        pos_lower = (pos or "").lower().strip()
        if len(pos_lower) < 40:
            continue
        pos_words = set(re.findall(r"[a-z]{5,}", pos_lower))
        if pos_words:
            overlap = len(pos_words & prompt_words) / len(pos_words)
            if overlap > 0.55:
                return False
        for start in range(0, max(0, len(pos_lower) - 39), 20):
            chunk = pos_lower[start : start + 50].strip()
            if len(chunk) >= 35 and chunk in blob:
                return False
    return True


RESEARCH_PROMPT_HEAD = (
    "You are 42 research desk writing an evidence-bound strategy brief for the "
    '"{persona_label}" research mode in markets {markets}.\n'
    "Use this brief structure: Objective (problem + metrics), Know The User (background bullets), "
    "The Tension (named frame), The goal, Get To By, brand safety, mandatories, creative judge, "
    "then bounded responses or investigations.\n"
    "Write like a sharp SSA insights lead: specific markets, named trends, metrics from refs, "
    "cultural mechanics (hustle, sapa, M-Pesa, platform behaviour), zero generic campaign fluff.\n"
    "Lead with engine intelligence (brief synthesis, seed behaviours, digest through_line, search velocity). "
    "Posts and comments are illustrative voice proof only, not the headline evidence.\n"
    "Use ONLY the numbered evidence refs below. Index every grounded claim with evidence_indices.\n"
    + AUDIENCE_EVIDENCE_RULE
    + "\n"
    "{lane_rules}\n\n"
    "EVIDENCE:\n{evidence}\n\n"
)


def _research_evidence_blob(refs: list) -> str:
    lines = []
    for i, r in enumerate(refs, start=1):
        ref_type = r.get("ref_type") or "ref"
        market = str(r.get("market") or "").upper()
        qg = r.get("query_group") or ""
        rel = r.get("relevance_score")
        header_bits = [f"[{i}]", f"type={ref_type}", f"market={market}"]
        if qg:
            header_bits.append(f"topic={qg}")
        if rel is not None:
            header_bits.append(f"relevance={rel}")
        metric_bits = []
        for key, label in (
            ("search_velocity_score", "search_velocity"),
            ("trend_score", "trend_score"),
            ("velocity_score", "velocity"),
            ("item_count", "posts"),
            ("mention_count", "mentions"),
            ("engagement_total", "engagement"),
        ):
            val = r.get(key)
            if val is not None and val != "" and val != 0:
                metric_bits.append(f"{label}={val}")
        if metric_bits:
            header_bits.append("metrics=" + ", ".join(metric_bits))
        lines.append(" ".join(header_bits))
        detail_lines = r.get("detail_lines")
        if isinstance(detail_lines, list):
            for line in detail_lines:
                text = str(line or "").strip()
                if text:
                    lines.append(f"  detail: {text}")
        ref_metrics = r.get("metrics")
        if isinstance(ref_metrics, dict):
            metric_pairs = []
            for key, val in ref_metrics.items():
                if val in (None, "", 0):
                    continue
                metric_pairs.append(f"{key}={val}")
            if metric_pairs:
                lines.append("  metrics: " + ", ".join(metric_pairs))
        if r.get("headline"):
            lines.append(f"  headline: {r.get('headline')}")
        if r.get("trend_synthesis"):
            lines.append(f"  synthesis: {r.get('trend_synthesis')}")
        if r.get("cultural_context"):
            lines.append(f"  cultural: {r.get('cultural_context')}")
        if r.get("name") or r.get("title"):
            lines.append(f"  title: {r.get('name') or r.get('title')}")
        if r.get("platform") or r.get("author_handle"):
            plat = r.get("platform") or "?"
            handle = r.get("author_handle") or ""
            lines.append(f"  post: {plat} @{handle}".rstrip())
        snippet = r.get("text") or r.get("behaviour") or ""
        if snippet:
            lines.append(f"  text: {snippet}")
    return "\n".join(lines)


def _ref_anchor_tokens(ref: dict) -> set[str]:
    tokens: set[str] = set()
    qg = str(ref.get("query_group") or "")
    if qg:
        tokens.add(qg.lower())
        tokens.add(qg.replace("_", " ").lower())
    for key in (
        "headline",
        "trend_synthesis",
        "cultural_context",
        "name",
        "title",
        "text",
        "behaviour",
    ):
        val = str(ref.get(key) or "")
        for word in re.findall(r"[a-zA-Z]{4,}", val.lower()):
            tokens.add(word)
    market = str(ref.get("market") or "").lower()
    if market in _MARKET_NAMES:
        tokens.add(market)
        tokens.add(_MARKET_NAMES[market])
    return tokens


def _ref_metric_tokens(ref: dict) -> set[str]:
    tokens: set[str] = set()
    for key in (
        "search_velocity_score",
        "trend_score",
        "velocity_score",
        "item_count",
        "mention_count",
        "engagement_total",
        "relevance_score",
    ):
        val = ref.get(key)
        if val is None or val == "":
            continue
        if isinstance(val, float):
            tokens.add(f"{val:.2f}")
            tokens.add(str(round(val, 2)))
        tokens.add(str(val))
    text = str(ref.get("text") or "")
    for num in re.findall(r"\d[\d,]*\.?\d*", text):
        tokens.add(num.replace(",", ""))
    return tokens


def _behaviour_bullet_grounded(text: str, indices: list[int], refs: list) -> bool:
    blob = text.lower()
    if len(text.strip()) < MIN_BEHAVIOUR_BULLET_CHARS:
        return False
    has_market = any(
        m in blob or name in blob
        for m, name in _MARKET_NAMES.items()
        if m.upper() in blob.upper() or name in blob
    ) or any(
        str(refs[i - 1].get("market") or "").upper() in blob.upper()
        for i in indices
        if 0 < i <= len(refs)
    )
    cited = [refs[i - 1] for i in indices if 0 < i <= len(refs)]
    anchor_hits = any(
        t in blob for r in cited for t in _ref_anchor_tokens(r) if len(t) >= 4
    )
    metric_hits = any(t in blob for r in cited for t in _ref_metric_tokens(r))
    return has_market and (anchor_hits or metric_hits)


def _activation_prompt_grounded(prompt: str, indices: list[int], refs: list) -> bool:
    if len(prompt.strip()) < MIN_ACTIVATION_PROMPT_CHARS:
        return False
    if _GENERIC_ACTIVATION_RE.search(prompt):
        return False
    blob = prompt.lower()
    cited = [refs[i - 1] for i in indices if 0 < i <= len(refs)]
    for r in cited:
        qg = str(r.get("query_group") or "")
        if qg and (qg.lower() in blob or qg.replace("_", " ").lower() in blob):
            return True
        for token in _ref_anchor_tokens(r):
            if len(token) >= 5 and token in blob:
                return True
    return False


def _positioning_grounded(text: str, refs: list) -> bool:
    blob = text.lower()
    if len(text.strip()) < 40:
        return False
    markets_named = sum(
        1 for m, name in _MARKET_NAMES.items() if m in blob or name in blob
    )
    if markets_named < 1:
        return False
    anchor_hits = sum(
        1 for r in refs for t in _ref_anchor_tokens(r) if len(t) >= 5 and t in blob
    )
    return anchor_hits >= 2


def _clean_grounded_bullets(raw, n_refs, refs, allowed_products: list[str]) -> list:
    if not isinstance(raw, list):
        return []
    clean = []
    for b in raw[:8]:
        if not isinstance(b, dict):
            continue
        text = b.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        idx_raw = b.get("evidence_indices")
        if not isinstance(idx_raw, list):
            continue
        indices = _dedupe_evidence_indices(
            [i for i in idx_raw if isinstance(i, int) and 1 <= i <= n_refs]
        )
        if not indices:
            continue
        prose = _clean_research_prose(text, allowed_products, refs)
        if prose is None:
            continue
        if not _behaviour_bullet_grounded(prose, indices, refs):
            continue
        clean.append({"text": prose, "evidence_indices": indices})
    return clean


def _clean_grounded_objective(
    raw, n_refs, refs, allowed_products: list[str]
) -> dict | None:
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    idx_raw = raw.get("evidence_indices")
    if not isinstance(idx_raw, list):
        return None
    indices = _dedupe_evidence_indices(
        [i for i in idx_raw if isinstance(i, int) and 1 <= i <= n_refs]
    )
    if not indices:
        return None
    prose = _clean_research_prose(text, allowed_products, refs)
    if prose is None:
        return None
    if not _behaviour_bullet_grounded(prose, indices, refs):
        return None
    return {"text": prose, "evidence_indices": indices}


def _inference_text_grounded(
    text: str, refs: list, allowed_products: list[str], min_len: int = 40
) -> bool:
    cleaned = _clean_research_prose(text, allowed_products, refs)
    if cleaned is None or len(cleaned) < min_len:
        return False
    return _positioning_grounded(cleaned, refs)


def _tension_grounded(text: str, refs: list, allowed_products: list[str]) -> bool:
    cleaned = _clean_research_prose(text, allowed_products, refs)
    if cleaned is None:
        return False
    blob = cleaned.lower()
    if len(cleaned) < 35:
        return False
    if " vs " in blob or " vs. " in blob or blob.startswith("the tension"):
        return _positioning_grounded(cleaned, refs) or any(
            name in blob for name in _MARKET_NAMES.values()
        )
    return _positioning_grounded(cleaned, refs)


def _validate_get_to_by(raw, refs: list, allowed_products: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    parts = {}
    for key in ("get", "to", "by"):
        val = raw.get(key)
        if not isinstance(val, str):
            return None
        cleaned = _clean_research_prose(val, allowed_products, refs)
        if cleaned is None or len(cleaned) < 20:
            return None
        parts[key] = cleaned
    combined = " ".join(parts.values())
    if not (_positioning_grounded(combined, refs) or len(combined) >= 80):
        return None
    return parts


def _validate_storyboard(raw, n_refs, refs, allowed_products: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    parts = {}
    for key in ("friction", "interaction", "resolution", "why_it_works"):
        val = raw.get(key)
        if not isinstance(val, str):
            return None
        cleaned = _clean_research_prose(val, allowed_products, refs)
        if cleaned is None or len(cleaned) < 20:
            return None
        parts[key] = cleaned
    idx_raw = raw.get("evidence_indices")
    if not isinstance(idx_raw, list):
        return None
    indices = _dedupe_evidence_indices(
        [i for i in idx_raw if isinstance(i, int) and 1 <= i <= n_refs]
    )
    if not indices:
        return None
    combined = " ".join(parts.values())
    if not (_positioning_grounded(combined, refs) or len(combined) >= 80):
        return None
    parts["evidence_indices"] = indices
    parts["provenance"] = "inference"
    return parts


_ENGINE_CITATION_TYPES = frozenset(
    {"brief", "digest", "seed", "google_trends_rising", "lexicon"}
)
_VOICE_CITATION_TYPES = frozenset({"post", "comment"})
_MAX_VOICE_CITATION_SHARE = 0.70


def _ref_type_at(refs: list, index: int) -> str:
    if index < 1 or index > len(refs):
        return ""
    return str(refs[index - 1].get("ref_type") or "")


def _indices_mostly_voice(indices: list[int], refs: list) -> bool:
    if not indices:
        return False
    voice = sum(1 for i in indices if _ref_type_at(refs, i) in _VOICE_CITATION_TYPES)
    return voice / len(indices) > _MAX_VOICE_CITATION_SHARE


def _indices_have_engine(indices: list[int], refs: list) -> bool:
    return any(_ref_type_at(refs, i) in _ENGINE_CITATION_TYPES for i in indices)


def _grounded_citations_acceptable(doc_json: dict, refs: list) -> bool:
    grounded = (
        doc_json.get("grounded") if isinstance(doc_json.get("grounded"), dict) else {}
    )
    objective = grounded.get("objective")
    if isinstance(objective, dict):
        obj_idx = objective.get("evidence_indices") or []
        if obj_idx and (
            _indices_mostly_voice(obj_idx, refs)
            or not _indices_have_engine(obj_idx, refs)
        ):
            return False
    for bullet in (
        grounded.get("user_insights") or grounded.get("behavioural_synthesis") or []
    ):
        if not isinstance(bullet, dict):
            continue
        idx = bullet.get("evidence_indices") or []
        if idx and _indices_mostly_voice(idx, refs):
            return False
    inf = (
        doc_json.get("inference") if isinstance(doc_json.get("inference"), dict) else {}
    )
    tension_idx = (
        inf.get("evidence_indices")
        if isinstance(inf.get("evidence_indices"), list)
        else []
    )
    if tension_idx and (
        _indices_mostly_voice(tension_idx, refs)
        or not _indices_have_engine(tension_idx, refs)
    ):
        return False
    all_cited = _collect_cited_ref_indices(doc_json)
    if all_cited and _indices_mostly_voice(all_cited, refs):
        return False
    return True


def _validate_research(
    data: dict, refs: list, *, allowed_products: list[str] | None = None
) -> dict | None:
    n_refs = len(refs)
    allowed = allowed_products or list(_RESEARCH_PRODUCT_BASE)
    if not isinstance(data, dict):
        return None
    grounded = data.get("grounded") if isinstance(data.get("grounded"), dict) else {}
    bullets_raw = grounded.get("user_insights")
    if bullets_raw is None:
        bullets_raw = grounded.get("behavioural_synthesis")
    clean_bullets = _clean_grounded_bullets(bullets_raw, n_refs, refs, allowed)
    objective = _clean_grounded_objective(
        grounded.get("objective"), n_refs, refs, allowed
    )
    if not clean_bullets and objective is None:
        return None
    out = {
        "title": _brief_str(data, "title") or "Market Research Perspective",
        "grounded": {
            "user_insights": clean_bullets,
            "voice_read": [],
        },
        "inference": {},
        "activation": {"responses": [], "provenance": "inference"},
    }
    if objective is not None:
        out["grounded"]["objective"] = objective
    inf = data.get("inference") if isinstance(data.get("inference"), dict) else {}
    inference_out = {}
    strategy_texts: list[str] = []
    for key in ("goal",):
        val = _brief_str(inf, key)
        cleaned = _clean_research_prose(val, allowed, refs) if val else None
        if cleaned and _inference_text_grounded(cleaned, refs, allowed, min_len=40):
            inference_out[key] = cleaned
            strategy_texts.append(cleaned)
    tension = _brief_str(inf, "tension")
    tension_clean = _clean_research_prose(tension, allowed, refs) if tension else None
    if tension_clean and _tension_grounded(tension_clean, refs, allowed):
        inference_out["tension"] = tension_clean
        strategy_texts.append(tension_clean)
    for key in ("brand_safe_notes", "mandatories"):
        val = _brief_str(inf, key)
        cleaned = _clean_research_prose(val, allowed, refs) if val else None
        if cleaned and len(cleaned) >= 30:
            inference_out[key] = cleaned
    creative = _brief_str(inf, "creative_judge")
    creative_clean = (
        _clean_research_prose(creative, allowed, refs) if creative else None
    )
    if creative_clean and len(creative_clean) >= 40:
        inference_out["creative_judge"] = creative_clean
    get_to_by = _validate_get_to_by(inf.get("get_to_by"), refs, allowed)
    if get_to_by:
        inference_out["get_to_by"] = get_to_by
        strategy_texts.append(" ".join(get_to_by.values()))
    positioning = _brief_str(inf, "positioning_frame")
    pos_clean = (
        _clean_research_prose(positioning, allowed, refs) if positioning else None
    )
    if (
        pos_clean
        and _positioning_grounded(pos_clean, refs)
        and not inference_out.get("goal")
    ):
        inference_out["goal"] = pos_clean
        strategy_texts.append(pos_clean)
    if inference_out:
        inference_out["confidence"] = (
            inf.get("confidence")
            if inf.get("confidence") in ("low", "moderate", "high")
            else "moderate"
        )
        inference_out["provenance"] = "inference"
        out["inference"] = inference_out
    act = data.get("activation") if isinstance(data.get("activation"), dict) else {}
    responses = []
    for p in (act.get("responses") or [])[:6]:
        if not isinstance(p, dict):
            continue
        response = p.get("response")
        if not isinstance(response, str) or not response.strip():
            continue
        response_clean = _clean_research_prose(response.strip(), allowed, refs)
        if response_clean is None:
            continue
        idx_raw = p.get("evidence_indices")
        if not isinstance(idx_raw, list):
            continue
        indices = _dedupe_evidence_indices(
            [i for i in idx_raw if isinstance(i, int) and 1 <= i <= n_refs]
        )
        if not indices:
            continue
        if not _activation_prompt_grounded(response_clean, indices, refs):
            continue
        if not _activation_distinct_from_strategy(response_clean, strategy_texts):
            continue
        mk = str(p.get("market") or "").upper()
        friction = _clean_research_prose(
            str(p.get("user_friction") or ""), allowed, refs
        )
        trigger = _clean_research_prose(
            str(p.get("behaviour_trigger") or ""), allowed, refs
        )
        if not friction or not trigger:
            continue
        responses.append(
            {
                "market": mk,
                "user_friction": friction,
                "behaviour_trigger": trigger,
                "response": response_clean,
                "evidence_indices": indices,
                "provenance": "inference",
            }
        )
    out["activation"]["responses"] = responses
    if not _grounded_citations_acceptable(out, refs):
        return None
    return out


def _research_lane_rules(evidence: dict) -> str:
    gate = evidence.get("quality_gate") or {}
    lines = []
    if not gate.get("allow_positioning"):
        lines.append(
            "Do NOT write tension, goal, get_to_by, mandatories, or creative_judge. "
            "Evidence lacks corroboration for strategic read."
        )
    if not gate.get("allow_activation"):
        lines.append(
            "Do NOT write responses. Set activation.responses to []. "
            "Do not invent generic activation filler."
        )
    if gate.get("level") == "moderate":
        lines.append(
            "Signal is moderate only. Keep confidence low or moderate. "
            "Prefer fewer, tighter bullets over speculative reach."
        )
    return (
        "\n".join(lines) if lines else "Full three-lane doc when evidence supports it."
    )


def synthesize_research(evidence: dict):
    refs = evidence.get("refs") or []
    allowed = _research_allowed_products(evidence)
    allowed_line = ", ".join(allowed)
    focus_note = (evidence.get("focus_note") or "").strip()
    focus_line = (
        "FOCUS: This brief was scoped to approved behaviours. Centre the Objective and "
        "The Tension on: " + focus_note + ". Do not drift to unrelated topics.\n\n"
        if focus_note
        else ""
    )
    prompt = (
        focus_line
        + RESEARCH_PROMPT_HEAD.format(
            persona_label=evidence.get("persona_label"),
            markets=", ".join(m.upper() for m in (evidence.get("markets") or [])),
            lane_rules=_research_lane_rules(evidence),
            evidence=_research_evidence_blob(refs),
        )
        + RESEARCH_JSON_SHAPE.replace("{allowed_products}", allowed_line)
    )
    for attempt in (1, 2):
        try:
            raw = _call_model(prompt)
            data = json.loads(raw)
        except Exception:
            logger.warning("research synthesis attempt %d failed", attempt)
            continue
        cleaned = _validate_research(data, refs, allowed_products=allowed)
        if cleaned is not None:
            return cleaned
        logger.warning("research synthesis attempt %d failed validation", attempt)
    return None


def build_thin_research_doc(evidence: dict) -> dict:
    refs = evidence.get("refs") or []
    gate = evidence.get("quality_gate") or {}
    level = gate.get("level") or "thin"
    quotes = [
        r.get("text") or r.get("headline") or r.get("behaviour") or r.get("title") or ""
        for r in refs[:6]
        if r.get("text") or r.get("headline") or r.get("behaviour") or r.get("title")
    ]
    if level == "thin":
        need = 3 if evidence.get("focus_query_groups") else 8
        note = (
            f"Only {len(refs)} refs gathered (need {need}+ for full synthesis on this behaviour). "
            "Pick a scan row with more linked examples, or retry after tomorrow's engine run."
        )
    elif not gate.get("allow_inference"):
        note = (
            "Insufficient corroboration for strategic read. "
            "Need corroborating topic or search evidence above the approved relevance floor."
        )
    else:
        note = "Not enough signal to synthesise behaviours yet."
    j = {
        "title": f"Market Research Perspective: {evidence.get('persona_label')}",
        "grounded": {
            "user_insights": [],
            "voice_read": quotes,
            "note": note,
        },
        "thin": True,
    }
    return {
        "json": j,
        "markdown": render_research_markdown(j),
        "sources": refs,
        "thin": True,
    }


def render_research_markdown(doc_json: dict) -> str:
    lines = [f"# {doc_json.get('title', 'Research')}", ""]
    grounded = (
        doc_json.get("grounded") if isinstance(doc_json.get("grounded"), dict) else {}
    )
    objective = grounded.get("objective")
    if isinstance(objective, dict) and objective.get("text"):
        idx = objective.get("evidence_indices") or []
        foot = "".join(f"[^{i}]" for i in idx)
        lines.extend(["## Objective", f"{objective.get('text')}{foot}", ""])
    bullets = (
        grounded.get("user_insights") or grounded.get("behavioural_synthesis") or []
    )
    if bullets:
        lines.append("## Know the user")
        lines.append("### Background")
    elif grounded.get("note"):
        lines.append("## Know the user")
        lines.append(str(grounded.get("note")))
    for b in bullets:
        if not isinstance(b, dict):
            continue
        idx = b.get("evidence_indices") or []
        foot = "".join(f"[^{i}]" for i in idx)
        lines.append(f"- {b.get('text')}{foot}")
    inf = (
        doc_json.get("inference") if isinstance(doc_json.get("inference"), dict) else {}
    )
    if inf.get("tension"):
        lines.extend(["", "## The tension", inf.get("tension")])
    if inf.get("goal"):
        lines.extend(["", "## The goal", inf.get("goal")])
    elif inf.get("positioning_frame"):
        lines.extend(["", "## The goal", inf.get("positioning_frame")])
    if inf.get("product_magic"):
        lines.extend(["", "## Know the magic", inf.get("product_magic")])
    gt = inf.get("get_to_by") if isinstance(inf.get("get_to_by"), dict) else {}
    if gt.get("get") or gt.get("to") or gt.get("by"):
        lines.extend(["", "## Get to by"])
        if gt.get("get"):
            lines.append(f"GET: {gt.get('get')}")
        if gt.get("to"):
            lines.append(f"TO: {gt.get('to')}")
        if gt.get("by"):
            lines.append(f"BY: {gt.get('by')}")
    if inf.get("brand_safe_notes"):
        lines.extend(["", "## Brand safety", inf.get("brand_safe_notes")])
    if inf.get("mandatories"):
        lines.extend(["", "## Mandatories", inf.get("mandatories")])
    if inf.get("creative_judge"):
        lines.extend(["", "## How we judge the work", inf.get("creative_judge")])
    act = (
        doc_json.get("activation")
        if isinstance(doc_json.get("activation"), dict)
        else {}
    )
    responses = act.get("responses") or []
    if responses:
        lines.append("")
        lines.append("## Suggested responses (inference)")
        for p in responses:
            idx = p.get("evidence_indices") or []
            foot = "".join(f"[^{i}]" for i in idx)
            lines.append(f"### {p.get('market')}{foot}")
            if p.get("user_friction"):
                lines.append(f"User friction: {p.get('user_friction')}")
            if p.get("behaviour_trigger"):
                lines.append(f"Behaviour trigger: {p.get('behaviour_trigger')}")
            if p.get("user_friction") or p.get("behaviour_trigger"):
                lines.append("")
                lines.append("Response:")
            lines.append(p.get("response") or "")
    refs = doc_json.get("_refs") or []
    cited = _collect_cited_ref_indices(doc_json)
    if refs and cited:
        lines.append("")
        for i in cited:
            if i < 1 or i > len(refs):
                continue
            r = refs[i - 1]
            if not isinstance(r, dict):
                continue
            snippet = _ref_display_snippet(r)
            if not snippet:
                continue
            lines.append(f"[^{i}]: {snippet}")
    return "\n".join(lines)


def _is_web_link(url: str) -> bool:
    """A plain web address a reader may open: http or https, a host, no credentials."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
    )


def _html_esc(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_RESEARCH_HTML_CSS = """
:root {
  color-scheme: light dark;
  --serif: Georgia, "Times New Roman", serif;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  --mono: ui-monospace, "Cascadia Mono", "Segoe UI Mono", Consolas, monospace;
  --canvas: #f5f3ef;
  --surface: #ffffff;
  --ink: #1f2937;
  --ink-2: #374151;
  --muted: #6b7280;
  --faint: #9ca3af;
  --line: #d0d7de;
  --accent: #1f4e79;
  --accent-soft: rgba(31, 78, 121, 0.1);
  --inference: #78716c;
  --activation: #b45309;
  --activation-soft: rgba(180, 83, 9, 0.1);
  --radius: 12px;
  --shadow: 0 1px 2px rgba(0, 0, 0, 0.06), 0 4px 14px rgba(0, 0, 0, 0.05);
}
@media (prefers-color-scheme: dark) {
  :root {
    --canvas: #1a1816;
    --surface: #242220;
    --ink: #f3f0ea;
    --ink-2: #d6d0c8;
    --muted: #a8a29e;
    --faint: #78716c;
    --line: #3d3935;
    --accent: #7eb3e8;
    --accent-soft: rgba(126, 179, 232, 0.14);
    --inference: #a8a29e;
    --activation: #fbbf24;
    --activation-soft: rgba(251, 191, 36, 0.12);
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.35), 0 4px 14px rgba(0, 0, 0, 0.4);
  }
}
*, *::before, *::after { box-sizing: border-box; }
body {
  margin: 0;
  font-family: var(--sans);
  font-size: 15px;
  line-height: 1.65;
  color: var(--ink);
  background: var(--canvas);
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 760px; margin: 0 auto; padding: 32px 20px 48px; }
.hero { margin-bottom: 32px; padding-bottom: 24px; border-bottom: 1px solid var(--line); }
.kicker {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--accent);
  font-weight: 600;
  margin: 0 0 12px;
}
.hero h1 {
  font-family: var(--serif);
  font-size: clamp(26px, 5vw, 34px);
  font-weight: 600;
  letter-spacing: -0.02em;
  line-height: 1.15;
  margin: 0 0 16px;
  color: var(--ink);
}
.meta-row { display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  display: inline-block;
  font-family: var(--mono);
  font-size: 11px;
  letter-spacing: 0.04em;
  font-weight: 600;
  padding: 5px 10px;
  border-radius: 6px;
  background: var(--accent-soft);
  color: var(--accent);
  border: 1px solid var(--line);
}
main { display: flex; flex-direction: column; gap: 24px; }
.card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 20px 22px 22px 24px;
  border-left-width: 3px;
  border-left-style: solid;
}
.lane-signal { border-left-color: var(--accent); }
.lane-inference { border-left-color: var(--inference); }
.lane-activation { border-left-color: var(--activation); }
.lane-proof { border-left-color: var(--muted); }
.card-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
  margin-bottom: 14px;
}
.lane-tag {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-soft);
  border-radius: 4px;
  padding: 3px 7px;
}
.lane-tag-activation { color: var(--activation); background: var(--activation-soft); }
.card-title {
  font-family: var(--serif);
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.01em;
  line-height: 1.2;
  margin: 0;
  color: var(--ink);
  flex: 1 1 auto;
}
.badge {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  font-weight: 600;
  color: var(--muted);
  background: var(--canvas);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 3px 7px;
}
.section-sub {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 600;
  margin: 0 0 10px;
}
.prose { margin: 0 0 12px; max-width: 68ch; color: var(--ink-2); }
.prose:last-child { margin-bottom: 0; }
ul.insights {
  margin: 0;
  padding: 0 0 0 18px;
  max-width: 68ch;
}
ul.insights li { margin: 0 0 10px; color: var(--ink-2); }
ul.insights li:last-child { margin-bottom: 0; }
cite, sup.cite { font-style: normal; }
sup.cite a {
  color: var(--accent);
  text-decoration: none;
  font-family: var(--mono);
  font-size: 10px;
  font-weight: 700;
  margin-left: 2px;
}
sup.cite a:hover { text-decoration: underline; }
.gtb { display: flex; flex-direction: column; gap: 12px; max-width: 68ch; }
.gtb-row { margin: 0; color: var(--ink-2); }
.gtb-label {
  display: inline-block;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  font-weight: 700;
  color: var(--accent);
  margin-right: 8px;
}
.activation-list { display: flex; flex-direction: column; gap: 16px; }
.activation-card {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 16px 18px;
  background: var(--canvas);
}
.activation-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.market-code {
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  color: var(--accent);
}
.activation-meta { display: grid; gap: 10px; margin-bottom: 12px; }
.activation-meta div { margin: 0; }
.activation-meta span {
  display: block;
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 600;
  margin-bottom: 4px;
}
.activation-meta p { margin: 0; color: var(--ink-2); font-size: 14px; }
.prompt-block {
  margin-top: 12px;
  padding: 14px 16px;
  border-radius: 8px;
  border: 1px dashed var(--line);
  background: var(--surface);
  font-family: var(--mono);
  font-size: 13px;
  line-height: 1.55;
  color: var(--ink);
  white-space: pre-wrap;
  word-break: break-word;
}
.prompt-k {
  font-size: 9px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 600;
  margin-bottom: 8px;
}
.storyboard-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 14px;
  margin-bottom: 14px;
}
.storyboard-table th,
.storyboard-table td {
  border: 1px solid var(--line);
  padding: 12px 14px;
  text-align: left;
  vertical-align: top;
  color: var(--ink-2);
}
.storyboard-table th {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--accent);
  background: var(--accent-soft);
  font-weight: 600;
}
.storyboard-why { margin: 0; max-width: 68ch; color: var(--ink-2); }
.storyboard-k {
  display: block;
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 600;
  margin-bottom: 6px;
}
.refs {
  margin-top: 36px;
  padding-top: 24px;
  border-top: 1px solid var(--line);
}
.refs h2 {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 0 0 16px;
  font-weight: 600;
}
.ref-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.ref-item {
  display: flex;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface);
  font-size: 13px;
  color: var(--ink-2);
}
.ref-num {
  flex-shrink: 0;
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 700;
  color: var(--accent);
  min-width: 1.4em;
}
.ref-text { margin: 0; line-height: 1.5; }
.ref-link { color: var(--accent); word-break: break-all; }
.refs-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  margin-bottom: 16px;
}
.refs-head h2 { margin: 0; flex: 1 1 auto; }
.ref-group { margin-bottom: 22px; }
.ref-group:last-child { margin-bottom: 0; }
.ref-subgroup { margin-top: 12px; }
.ref-kind {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 600;
}
@media print {
  body { background: #fff; color: #1f2937; }
  .wrap { max-width: none; padding: 0; }
  .card { box-shadow: none; break-inside: avoid; page-break-inside: avoid; }
  .hero { border-bottom-color: #d0d7de; }
}
@media (max-width: 600px) {
  .wrap { padding: 20px 14px 36px; }
  .card { padding: 16px 14px 18px 16px; }
  .storyboard-table { display: block; overflow-x: auto; }
}
"""

_MARKET_FLAGS = {
    "ZA": "\U0001f1ff\U0001f1e6",
    "NG": "\U0001f1f3\U0001f1ec",
    "KE": "\U0001f1f0\U0001f1ea",
}

_ENGINE_HTML_TYPES = frozenset({"brief", "digest", "seed"})
_VOICE_HTML_TYPES = frozenset({"post", "comment"})
_SEARCH_HTML_TYPES = frozenset({"google_trends_rising", "lexicon"})


def _html_ref_type(ref: dict) -> str:
    rt = str(ref.get("ref_type") or ref.get("type") or "").lower()
    if rt:
        return rt
    vk = str(ref.get("voice_kind") or "").lower()
    if vk == "comment":
        return "comment"
    return "post"


def _html_evidence_tier(ref: dict) -> str:
    rt = _html_ref_type(ref)
    if rt in _ENGINE_HTML_TYPES:
        return "engine"
    if rt in _VOICE_HTML_TYPES:
        return "voice"
    if rt in _SEARCH_HTML_TYPES or "search" in rt:
        return "search"
    return "engine"


def _html_group_refs(refs: list) -> dict:
    engine: list[tuple[int, dict]] = []
    voice_posts: list[tuple[int, dict]] = []
    voice_comments: list[tuple[int, dict]] = []
    search: list[tuple[int, dict]] = []
    for i, ref in enumerate(refs or [], start=1):
        if not isinstance(ref, dict):
            continue
        snippet = _ref_display_snippet(ref)
        if not snippet:
            continue
        tier = _html_evidence_tier(ref)
        rt = _html_ref_type(ref)
        if tier == "engine":
            engine.append((i, ref))
        elif rt == "comment" or str(ref.get("voice_kind") or "") == "comment":
            voice_comments.append((i, ref))
        elif tier == "voice":
            voice_posts.append((i, ref))
        else:
            search.append((i, ref))
    return {
        "engine": engine,
        "voice_posts": voice_posts,
        "voice_comments": voice_comments,
        "search": search,
        "counts": {
            "engine": len(engine),
            "voice": len(voice_posts) + len(voice_comments),
            "search": len(search),
            "total": len(engine) + len(voice_posts) + len(voice_comments) + len(search),
        },
    }


def _fmt_count(n) -> str:
    """Compact human count: 2100000 -> 2.1M, 12300 -> 12.3k, 96 -> 96."""
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        v = 0.0
    if v >= 1e9:
        s = f"{v / 1e9:.1f}".rstrip("0").rstrip(".") + "B"
    elif v >= 1e6:
        s = f"{v / 1e6:.1f}".rstrip("0").rstrip(".") + "M"
    elif v >= 1e3:
        s = f"{v / 1e3:.1f}".rstrip("0").rstrip(".") + "k"
    else:
        s = str(int(round(v)))
    return s


def _post_metrics_line(ref: dict) -> str:
    """Per-post metrics line: '12.3k likes · 840 shares · 2.1M views · 96 comments'.

    Reads the ref's metrics dict (built read-side from enriched_content's
    granular counters). Falls back to engagement_total when no granular
    counter survived."""
    metrics = ref.get("metrics") if isinstance(ref.get("metrics"), dict) else {}
    bits = []
    for key, label in (
        ("likes", "likes"),
        ("shares", "shares"),
        ("views", "views"),
        ("comments", "comments"),
    ):
        val = metrics.get(key)
        if val:
            bits.append(f"{_fmt_count(val)} {label}")
    if bits:
        return " · ".join(bits)
    eng = (
        metrics.get("engagement_total")
        or ref.get("engagement_total")
        or ref.get("engagement")
    )
    if eng:
        return f"{_fmt_count(eng)} engagement"
    return ""


def _html_voice_ref_row(index: int, ref: dict, *, kind_label: str) -> str:
    snippet = _ref_display_snippet(ref)
    if not snippet:
        return ""
    plat = _html_esc(str(ref.get("platform") or ""))
    handle = _html_esc(str(ref.get("author_handle") or ref.get("handle") or ""))
    metrics_line = _post_metrics_line(ref)
    url = str(ref.get("url") or "")
    link_html = ""
    if _is_web_link(url):
        link_html = f' <a class="ref-link" href="{_html_esc(url)}">View</a>'
    meta_bits = [kind_label]
    if plat:
        meta_bits.append(plat)
    if handle:
        meta_bits.append("@" + handle.lstrip("@"))
    if metrics_line:
        meta_bits.append(metrics_line)
    thread_comments = (
        ref.get("thread_comments")
        if isinstance(ref.get("thread_comments"), list)
        else []
    )
    if thread_comments:
        meta_bits.append(f"{len(thread_comments)} comments")
    meta = _html_esc(" · ".join(meta_bits))
    thread_html = ""
    if thread_comments:
        rows = []
        for c in thread_comments:
            if not isinstance(c, dict):
                continue
            ctext = _html_esc(str(c.get("text") or ""))
            ch = _html_esc(str(c.get("handle") or ""))
            ceng = int(c.get("engagement") or 0)
            cmeta = " · ".join(
                x for x in (ch, f"{ceng} engagement" if ceng else "") if x
            )
            rows.append(
                f'<li class="ref-thread-item">'
                f'<span class="ref-thread-meta">{_html_esc(cmeta)}</span> '
                f"{ctext}</li>"
            )
        if rows:
            thread_html = (
                f'<p class="pack-voice-k">Comments on this post ({len(thread_comments)})</p>'
                f'<ol class="ref-thread-list">{"".join(rows)}</ol>'
            )
    return (
        f'<li class="ref-item" id="ref-{index}">'
        f'<span class="ref-num">{index}</span>'
        f'<p class="ref-text"><span class="ref-kind">{meta}</span> '
        f"{_html_esc(snippet)}{link_html}</p>{thread_html}</li>"
    )


def _html_engine_ref_row(index: int, ref: dict) -> str:
    snippet = _ref_display_snippet(ref)
    if not snippet:
        return ""
    mk = str(ref.get("market") or "").upper()
    rt = _html_ref_type(ref)
    topic = str(ref.get("query_group") or "").replace("_", " ")
    meta_bits = [x for x in (mk, rt, topic) if x]
    meta = _html_esc(" · ".join(meta_bits))
    detail_html = ""
    detail_lines = ref.get("detail_lines")
    if isinstance(detail_lines, list):
        rows = []
        for line in detail_lines:
            text = str(line or "").strip()
            if text:
                rows.append(f"<li>{_html_esc(text)}</li>")
        if rows:
            detail_html = f'<ul class="ref-detail">{"".join(rows)}</ul>'
    return (
        f'<li class="ref-item" id="ref-{index}">'
        f'<span class="ref-num">{index}</span>'
        f'<p class="ref-text"><span class="ref-kind">{meta}</span> '
        f"{_html_esc(snippet)}</p>{detail_html}</li>"
    )


def _html_search_ref_row(index: int, ref: dict) -> str:
    snippet = _ref_display_snippet(ref)
    if not snippet:
        return ""
    mk = str(ref.get("market") or "").upper()
    rt = _html_ref_type(ref)
    meta = _html_esc(" · ".join(x for x in (mk, rt) if x))
    detail_html = ""
    detail_lines = ref.get("detail_lines")
    if isinstance(detail_lines, list):
        rows = []
        for line in detail_lines:
            text = str(line or "").strip()
            if text:
                rows.append(f"<li>{_html_esc(text)}</li>")
        if rows:
            detail_html = f'<ul class="ref-detail">{"".join(rows)}</ul>'
    return (
        f'<li class="ref-item" id="ref-{index}">'
        f'<span class="ref-num">{index}</span>'
        f'<p class="ref-text"><span class="ref-kind">{meta}</span> '
        f"{_html_esc(snippet)}</p>{detail_html}</li>"
    )


def _render_grouped_evidence_html(refs: list) -> str:
    grouped = _html_group_refs(refs)
    counts = grouped["counts"]
    if not counts["total"]:
        return ""
    badge = (
        f"{counts['total']} sources · {counts['engine']} engine · "
        f"{counts['voice']} voice · {counts['search']} search"
    )
    parts = [
        '<footer class="refs">',
        '<div class="refs-head">',
        "<h2>Evidence sources</h2>",
        f'<span class="badge">{_html_esc(badge)}</span>',
        "</div>",
        '<p class="prose">Engine signal drives the brief. Voice posts and comments are proof receipts linked to those behaviours.</p>',
    ]
    if grouped["engine"]:
        rows = "".join(_html_engine_ref_row(i, r) for i, r in grouped["engine"])
        parts.append(
            f'<div class="ref-group"><p class="section-sub">Engine signal</p><ol class="ref-list">{rows}</ol></div>'
        )
    voice_posts = grouped["voice_posts"]
    voice_comments = grouped["voice_comments"]
    if voice_posts or voice_comments:
        parts.append('<div class="ref-group"><p class="section-sub">Voice proof</p>')
        if voice_posts:
            rows = "".join(
                _html_voice_ref_row(i, r, kind_label="Post") for i, r in voice_posts
            )
            parts.append(
                f'<div class="ref-subgroup"><p class="pack-voice-k">Posts</p><ol class="ref-list">{rows}</ol></div>'
            )
        if voice_comments:
            rows = "".join(
                _html_voice_ref_row(i, r, kind_label="Comment")
                for i, r in voice_comments
            )
            parts.append(
                f'<div class="ref-subgroup"><p class="pack-voice-k">Comments</p><ol class="ref-list">{rows}</ol></div>'
            )
        parts.append("</div>")
    if grouped["search"]:
        rows = "".join(_html_search_ref_row(i, r) for i, r in grouped["search"])
        parts.append(
            f'<div class="ref-group"><p class="section-sub">Search &amp; reach</p><ol class="ref-list">{rows}</ol></div>'
        )
    parts.append("</footer>")
    return "".join(parts)


def _render_consolidated_behaviour_html(doc_json: dict) -> str:
    sections: list[str] = []
    for sec in doc_json.get("behaviour_sections") or []:
        if not isinstance(sec, dict):
            continue
        mk = str(sec.get("market") or "").upper()
        flag = _MARKET_FLAGS.get(mk, "")
        topic = _html_esc(
            sec.get("signal_topic") or sec.get("behaviour") or "Behaviour"
        )
        inner_parts: list[str] = []
        for label, key in (
            ("Behaviour read", "behaviour_read"),
            ("What posts and comments tell us", "voice_read"),
            ("Proof", "proof_stats"),
            ("Strategic implication", "strategic_implication"),
        ):
            val = sec.get(key)
            if val:
                inner_parts.append(
                    f'<p class="section-sub">{_html_esc(label)}</p>'
                    f'<p class="prose">{_html_esc(str(val))}{_html_ref_sups(sec.get("evidence_indices"))}</p>'
                )
        if not inner_parts:
            continue
        sections.append(
            f'<section class="card lane-signal">'
            f'<div class="card-head"><span class="lane-tag">Signal</span>'
            f'<h2 class="card-title">{flag} {mk} · {topic}</h2></div>'
            f"{''.join(inner_parts)}</section>"
        )
    cc = (
        doc_json.get("cross_cutting")
        if isinstance(doc_json.get("cross_cutting"), dict)
        else {}
    )
    if cc.get("summary"):
        themes = ", ".join(str(t) for t in (cc.get("themes") or []) if t)
        theme_html = f'<p class="prose">{_html_esc(themes)}</p>' if themes else ""
        sections.append(
            _html_section(
                "inference",
                "Inference",
                "Cross-cutting themes",
                theme_html + _html_prose(str(cc.get("summary") or "")),
                badge="Consolidated read · inference",
            )
        )
    return "".join(sections)


def _html_ref_sups(indices) -> str:
    parts = []
    for i in _dedupe_evidence_indices(indices or []):
        parts.append(f'<sup class="cite"><a href="#ref-{i}">{i}</a></sup>')
    return "".join(parts)


def _html_prose(text: str, indices=None) -> str:
    body = _html_esc(text)
    refs = _html_ref_sups(indices)
    if not body and not refs:
        return ""
    return f'<p class="prose">{body}{refs}</p>'


def _html_section(
    lane: str,
    tag: str,
    title: str,
    inner: str,
    *,
    badge: str = "",
    tag_variant: str = "",
) -> str:
    tag_cls = f" lane-tag{tag_variant}" if tag_variant else ""
    badge_html = f'<span class="badge">{_html_esc(badge)}</span>' if badge else ""
    return (
        f'<section class="card lane-{lane}">'
        f'<div class="card-head"><span class="lane-tag{tag_cls}">{_html_esc(tag)}</span>'
        f'<h2 class="card-title">{_html_esc(title)}</h2>{badge_html}</div>'
        f"{inner}</section>"
    )


def _html_inference_section(
    title: str, text: str, *, badge: str = "Strategic read · inference"
) -> str:
    inner = _html_prose(text)
    if not inner:
        return ""
    return _html_section("inference", "Inference", title, inner, badge=badge)


def render_research_html(
    doc_json: dict, *, persona_label: str = "", markets: list | None = None
) -> str:
    """Jo export: findings and recommendations as a standalone HTML file."""
    title_raw = doc_json.get("title") or "42 research brief"
    title = _html_esc(title_raw)
    grounded = (
        doc_json.get("grounded") if isinstance(doc_json.get("grounded"), dict) else {}
    )
    inference = (
        doc_json.get("inference") if isinstance(doc_json.get("inference"), dict) else {}
    )
    activation = (
        doc_json.get("activation")
        if isinstance(doc_json.get("activation"), dict)
        else {}
    )
    refs = doc_json.get("_refs") or []

    chips = []
    if persona_label:
        chips.append(f'<span class="chip">{_html_esc(persona_label)}</span>')
    for m in markets or []:
        code = str(m).upper()
        flag = _MARKET_FLAGS.get(code, "")
        chips.append(f'<span class="chip">{flag} {_html_esc(code)}</span>'.strip())
    chips_html = f'<div class="meta-row">{"".join(chips)}</div>' if chips else ""

    sections: list[str] = []
    objective = grounded.get("objective")
    if isinstance(objective, dict) and objective.get("text"):
        inner = _html_prose(objective.get("text"), objective.get("evidence_indices"))
        sections.append(_html_section("signal", "Signal", "Objective", inner))

    bullets = (
        grounded.get("user_insights") or grounded.get("behavioural_synthesis") or []
    )
    if bullets:
        items = []
        for b in bullets:
            if not isinstance(b, dict):
                continue
            txt = b.get("text") or ""
            refs_html = _html_ref_sups(b.get("evidence_indices"))
            items.append(f"<li>{_html_esc(txt)}{refs_html}</li>")
        inner = (
            '<p class="section-sub">Background</p><ul class="insights">'
            + "".join(items)
            + "</ul>"
        )
        sections.append(_html_section("signal", "Signal", "Know the user", inner))
    elif grounded.get("note"):
        inner = _html_prose(str(grounded.get("note")))
        sections.append(_html_section("signal", "Signal", "Know the user", inner))

    if inference.get("tension"):
        sections.append(
            _html_inference_section("The tension", inference.get("tension"))
        )
    goal = inference.get("goal") or inference.get("positioning_frame")
    if goal:
        sections.append(_html_inference_section("The goal", goal))
    if inference.get("product_magic"):
        sections.append(
            _html_inference_section(
                "Know the magic",
                inference.get("product_magic"),
                badge="Product frame · inference",
            )
        )
    gt = (
        inference.get("get_to_by")
        if isinstance(inference.get("get_to_by"), dict)
        else {}
    )
    if gt.get("get") or gt.get("to") or gt.get("by"):
        rows = []
        for label, key in (("Get", "get"), ("To", "to"), ("By", "by")):
            val = gt.get(key)
            if val:
                rows.append(
                    f'<p class="gtb-row"><span class="gtb-label">{label}</span>{_html_esc(val)}</p>'
                )
        inner = '<div class="gtb">' + "".join(rows) + "</div>"
        sections.append(
            _html_section(
                "inference",
                "Inference",
                "Get to by",
                inner,
                badge="Strategic read · inference",
            )
        )
    if inference.get("brand_safe_notes"):
        sections.append(
            _html_inference_section(
                "Brand safety",
                inference.get("brand_safe_notes"),
                badge="Guardrails · inference",
            )
        )
    if inference.get("mandatories"):
        sections.append(
            _html_inference_section(
                "Mandatories",
                inference.get("mandatories"),
                badge="Guardrails · inference",
            )
        )
    if inference.get("creative_judge"):
        sections.append(
            _html_inference_section(
                "How we judge the work",
                inference.get("creative_judge"),
                badge="Creative criteria · inference",
            )
        )

    responses = activation.get("responses") or []
    if responses:
        cards = []
        for p in responses:
            if not isinstance(p, dict):
                continue
            mk = str(p.get("market") or "").upper()
            flag = _MARKET_FLAGS.get(mk, "")
            head = f'<div class="activation-head"><span class="market-code">{flag} {_html_esc(mk)}</span></div>'
            meta_parts = []
            if p.get("user_friction"):
                meta_parts.append(
                    f"<div><span>User friction</span><p>{_html_esc(p.get('user_friction'))}</p></div>"
                )
            if p.get("behaviour_trigger"):
                meta_parts.append(
                    f"<div><span>Behaviour trigger</span><p>{_html_esc(p.get('behaviour_trigger'))}</p></div>"
                )
            meta_html = (
                f'<div class="activation-meta">{"".join(meta_parts)}</div>'
                if meta_parts
                else ""
            )
            prompt = p.get("response") or ""
            prompt_html = ""
            if prompt:
                prompt_html = (
                    f'<div class="prompt-block"><div class="prompt-k">Response</div>'
                    f"{_html_esc(prompt)}</div>"
                )
            proof = _html_ref_sups(p.get("evidence_indices"))
            proof_html = f'<p class="prose">{proof}</p>' if proof else ""
            cards.append(
                f'<div class="activation-card">{head}{meta_html}{prompt_html}{proof_html}</div>'
            )
        inner = '<div class="activation-list">' + "".join(cards) + "</div>"
        sections.append(
            _html_section(
                "activation",
                "Activation",
                "Suggested responses",
                inner,
                badge="Responses · inference",
                tag_variant="-activation",
            )
        )

    storyboard = (
        activation.get("storyboard")
        if isinstance(activation.get("storyboard"), dict)
        else {}
    )
    if storyboard.get("friction"):
        table = (
            '<table class="storyboard-table"><thead><tr>'
            "<th>Friction</th><th>Interaction</th><th>Resolution</th>"
            "</tr></thead><tbody><tr>"
            f"<td>{_html_esc(storyboard.get('friction'))}</td>"
            f"<td>{_html_esc(storyboard.get('interaction'))}</td>"
            f"<td>{_html_esc(storyboard.get('resolution'))}</td>"
            "</tr></tbody></table>"
        )
        why_html = ""
        if storyboard.get("why_it_works"):
            why_html = (
                f'<p class="storyboard-why"><span class="storyboard-k">Why it works</span>'
                f"{_html_esc(storyboard.get('why_it_works'))}"
                f"{_html_ref_sups(storyboard.get('evidence_indices'))}</p>"
            )
        sections.append(
            _html_section(
                "activation",
                "Storyboard",
                "Friction · Interaction · Resolution",
                table + why_html,
                badge="Storyboard · inference",
                tag_variant="-activation",
            )
        )

    if doc_json.get("consolidated") and doc_json.get("behaviour_sections"):
        sections.insert(0, _render_consolidated_behaviour_html(doc_json))

    refs = doc_json.get("_refs") or []
    ref_footer = _render_grouped_evidence_html(refs)
    if not ref_footer:
        cited = _collect_cited_ref_indices(doc_json)
        ref_items = _render_research_ref_items(refs, cited)
        if ref_items:
            ref_footer = (
                '<footer class="refs"><h2>Evidence sources</h2>'
                f'<ol class="ref-list">{"".join(ref_items)}</ol></footer>'
            )

    main_html = f"<main>{''.join(sections)}</main>" if sections else ""
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title>"
        f"<style>{_RESEARCH_HTML_CSS}</style></head>"
        "<body>"
        '<div class="wrap">'
        '<header class="hero">'
        '<p class="kicker">42 · Market research</p>'
        f"<h1>{title}</h1>"
        f"{chips_html}"
        "</header>"
        f"{main_html}"
        f"{ref_footer}"
        "</div></body></html>"
    )


CONSOLIDATED_JSON_SHAPE = """
Return strict JSON with exactly this shape:
{
  "title": "Consolidated Research: ...",
  "behaviour_sections": [
    {
      "market": "NG",
      "signal_topic": "Sapa hustle economy",
      "behaviour_read": "One paragraph naming the approved behaviour in plain English",
      "voice_read": "What the cited posts and comments tell us, with cultural mechanics",
      "proof_stats": "Client-friendly proof: post_count, comment_count, engagement from refs",
      "strategic_implication": "What decision or next investigation this behaviour supports",
      "evidence_indices": [1, 2, 3]
    }
  ],
  "cross_cutting": {
    "themes": ["theme one", "theme two"],
    "summary": "Two to three sentences on what connects the behaviours across markets"
  }
}

Hard rules:
- evidence_indices are 1-based into the numbered refs list for that behaviour section only (see section headers in EVIDENCE).
- Each behaviour_sections entry MUST cite at least one ref index. Prefer citing both a post and a comment ref when both exist in that section.
- proof_stats MUST mention post_count and comment_count from the section metric block when present. Never paste raw trend_score= or velocity= pairs.
- strategic_implication MUST tie the decision or investigation to the behaviour from refs.
- cross_cutting is required when there are 2+ behaviour sections; omit cross_cutting when only one section.
- Never invent stats, handles, URLs, topics, products, audiences, age groups, or gender claims not in the refs.
"""


CONSOLIDATED_PROMPT_HEAD = (
    "You are 42 research desk writing a consolidated multi-behaviour research doc for persona "
    '"{persona_label}" in markets {markets}.\n'
    "Each behaviour section below was approved in a behaviour scan. Write one section per behaviour "
    "with: behaviour read, what posts and comments tell us (cited), proof stats, and strategic implication.\n"
    "When multiple behaviours are present, add cross_cutting themes that connect them.\n"
    "Write like a sharp SSA insights lead: specific markets, named trends, metrics from refs, zero generic fluff.\n"
    + AUDIENCE_EVIDENCE_RULE
    + "\n"
    "{seo_line}"
    "{lane_rules}\n\n"
    "EVIDENCE BY BEHAVIOUR:\n{evidence}\n\n"
)


def _consolidated_evidence_blob(evidence: dict) -> str:
    lines: list[str] = []
    for section in evidence.get("behaviour_sections") or []:
        if not isinstance(section, dict):
            continue
        mk = str(section.get("market") or "").upper()
        topic = section.get("signal_topic") or section.get("behaviour") or ""
        metric = (
            section.get("metric") if isinstance(section.get("metric"), dict) else {}
        )
        lines.append(f"=== {mk} · {topic} ===")
        if metric:
            bits = []
            if metric.get("post_count") is not None:
                bits.append(f"post_count={metric.get('post_count')}")
            if metric.get("comment_count") is not None:
                bits.append(f"comment_count={metric.get('comment_count')}")
            if metric.get("engagement_total") is not None:
                bits.append(f"engagement_total={metric.get('engagement_total')}")
            if bits:
                lines.append("metrics: " + ", ".join(bits))
        if section.get("behaviour"):
            lines.append("behaviour: " + str(section.get("behaviour")))
        refs = section.get("refs") or []
        ref_start = int(section.get("ref_start") or 1)
        for offset, r in enumerate(refs):
            lines.append(
                _research_evidence_blob([r]).replace("[1]", f"[{ref_start + offset}]")
            )
        lines.append("")
    return "\n".join(lines)


def _validate_consolidated_section(
    raw: dict, refs: list, allowed: list[str]
) -> dict | None:
    if not isinstance(raw, dict):
        return None
    mk = str(raw.get("market") or "").upper()
    topic = _brief_str(raw, "signal_topic") or _brief_str(raw, "behaviour_read")[:40]
    if not topic:
        topic = "Behaviour"
    parts = {}
    for key in ("behaviour_read", "voice_read", "proof_stats", "strategic_implication"):
        val = _brief_str(raw, key)
        cleaned = _clean_research_prose(val, allowed, refs) if val else None
        if cleaned is None or len(cleaned) < 25:
            return None
        parts[key] = cleaned
    idx_raw = raw.get("evidence_indices")
    if not isinstance(idx_raw, list):
        return None
    indices = _dedupe_evidence_indices(
        [i for i in idx_raw if isinstance(i, int) and 1 <= i <= len(refs)]
    )
    if not indices:
        return None
    return {"market": mk, "signal_topic": topic, "evidence_indices": indices, **parts}


def _validate_consolidated(
    data: dict, evidence: dict, *, allowed_products: list[str] | None = None
) -> dict | None:
    if not isinstance(data, dict):
        return None
    allowed = allowed_products or _research_allowed_products(evidence)
    sections_in = evidence.get("behaviour_sections") or []
    sections_out: list[dict] = []
    raw_sections = data.get("behaviour_sections") or []
    for i, sec_in in enumerate(sections_in):
        if not isinstance(sec_in, dict):
            continue
        raw = (
            raw_sections[i]
            if i < len(raw_sections) and isinstance(raw_sections[i], dict)
            else {}
        )
        local_refs = sec_in.get("refs") or []
        cleaned = _validate_consolidated_section(raw, local_refs, allowed)
        if cleaned is None:
            continue
        ref_start = int(sec_in.get("ref_start") or 1)
        cleaned["evidence_indices"] = [
            ref_start - 1 + idx for idx in cleaned["evidence_indices"]
        ]
        cleaned["signal_topic"] = sec_in.get("signal_topic") or cleaned.get(
            "signal_topic"
        )
        cleaned["query_group"] = sec_in.get("query_group") or ""
        sections_out.append(cleaned)
    if not sections_out:
        return None
    out = {
        "title": _brief_str(data, "title")
        or f"Consolidated Research: {evidence.get('persona_label')}",
        "behaviour_sections": sections_out,
        "consolidated": True,
    }
    if len(sections_out) >= 2:
        cc = (
            data.get("cross_cutting")
            if isinstance(data.get("cross_cutting"), dict)
            else {}
        )
        themes_raw = cc.get("themes")
        themes = [
            str(t).strip()
            for t in themes_raw
            if isinstance(themes_raw, list) and str(t).strip()
        ][:4]
        summary = _clean_research_prose(
            str(cc.get("summary") or ""), allowed, evidence.get("refs") or []
        )
        if summary and len(summary) >= 40:
            out["cross_cutting"] = {"themes": themes, "summary": summary}
    return out


def synthesize_consolidated_research(evidence: dict):
    _require_legacy_vertex_generation()
    allowed = _research_allowed_products(evidence)
    allowed_line = ", ".join(allowed)
    seo = (evidence.get("seo_notes") or "").strip()
    seo_line = ("SEO NOTES (optional context):\n" + seo + "\n\n") if seo else ""
    prompt = CONSOLIDATED_PROMPT_HEAD.format(
        persona_label=evidence.get("persona_label"),
        markets=", ".join(m.upper() for m in (evidence.get("markets") or [])),
        seo_line=seo_line,
        lane_rules="Consolidated doc: cite refs per behaviour section header.",
        evidence=_consolidated_evidence_blob(evidence),
    ) + CONSOLIDATED_JSON_SHAPE.replace("{allowed_products}", allowed_line)
    for attempt in (1, 2):
        try:
            raw = _call_model(prompt)
            data = json.loads(raw)
        except Exception:
            logger.warning("consolidated synthesis attempt %d failed", attempt)
            continue
        cleaned = _validate_consolidated(data, evidence, allowed_products=allowed)
        if cleaned is not None:
            return cleaned
        logger.warning("consolidated synthesis attempt %d failed validation", attempt)
    return None


def build_thin_consolidated_doc(evidence: dict) -> dict:
    refs = evidence.get("refs") or []
    sections = []
    for sec in evidence.get("behaviour_sections") or []:
        if not isinstance(sec, dict):
            continue
        metric = sec.get("metric") if isinstance(sec.get("metric"), dict) else {}
        proof = (
            f"{metric.get('post_count', 0)} posts, {metric.get('comment_count', 0)} comments, "
            f"{metric.get('engagement_total', 0)} engagement"
        )
        ref_start = int(sec.get("ref_start") or 1)
        ref_end = int(sec.get("ref_end") or ref_start)
        indices = list(range(ref_start, ref_end + 1))[:3]
        sections.append(
            {
                "market": str(sec.get("market") or "").upper(),
                "signal_topic": sec.get("signal_topic") or "",
                "behaviour_read": sec.get("behaviour")
                or "Thin signal on this behaviour.",
                "voice_read": "Examples gathered; synthesis unavailable.",
                "proof_stats": proof,
                "strategic_implication": "More evidence is needed before this behaviour supports a decision.",
                "evidence_indices": indices,
            }
        )
    j = {
        "title": f"Consolidated Research: {evidence.get('persona_label')}",
        "behaviour_sections": sections,
        "thin": True,
        "consolidated": True,
    }
    if len(sections) >= 2:
        j["cross_cutting"] = {
            "themes": ["Cross-market hustle"],
            "summary": "Signal is thin; re-run when more voice posts and comments are available.",
        }
    return {
        "json": j,
        "markdown": render_consolidated_markdown(j, evidence),
        "sources": refs,
        "thin": True,
        "consolidated": True,
    }


def render_consolidated_markdown(doc_json: dict, evidence: dict | None = None) -> str:
    lines = [f"# {doc_json.get('title', 'Consolidated Research')}", ""]
    for sec in doc_json.get("behaviour_sections") or []:
        if not isinstance(sec, dict):
            continue
        mk = sec.get("market") or ""
        topic = sec.get("signal_topic") or ""
        lines.append(f"## {mk} · {topic}")
        idx = sec.get("evidence_indices") or []
        foot = "".join(f"[^{i}]" for i in idx)
        if sec.get("behaviour_read"):
            lines.append(f"### Behaviour read\n{sec.get('behaviour_read')}{foot}")
        if sec.get("voice_read"):
            lines.append(
                f"### What posts and comments tell us\n{sec.get('voice_read')}{foot}"
            )
        if sec.get("proof_stats"):
            lines.append(f"### Proof\n{sec.get('proof_stats')}")
        if sec.get("strategic_implication"):
            lines.append(
                f"### Strategic implication\n{sec.get('strategic_implication')}{foot}"
            )
        lines.append("")
    cc = (
        doc_json.get("cross_cutting")
        if isinstance(doc_json.get("cross_cutting"), dict)
        else {}
    )
    if cc.get("summary"):
        lines.append("## Cross-cutting themes")
        if cc.get("themes"):
            lines.append(", ".join(cc.get("themes") or []))
        lines.append(cc.get("summary"))
    refs = doc_json.get("_refs") or (evidence or {}).get("refs") or []
    cited: set[int] = set()
    for sec in doc_json.get("behaviour_sections") or []:
        for i in sec.get("evidence_indices") or []:
            cited.add(int(i))
    if refs and cited:
        lines.append("")
        for i in sorted(cited):
            if i < 1 or i > len(refs):
                continue
            snippet = _ref_display_snippet(refs[i - 1])
            if snippet:
                lines.append(f"[^{i}]: {snippet}")
    return "\n".join(lines)


_EVIDENCE_PACK_CSS = (
    _RESEARCH_HTML_CSS
    + """
.pack-behaviour { margin-bottom: 28px; }
.pack-voice-k {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 600;
  margin: 16px 0 8px;
}
.pack-row {
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 8px;
  background: var(--surface);
  font-size: 13px;
}
.pack-meta { font-family: var(--mono); font-size: 10px; color: var(--muted); margin-bottom: 6px; }
.pack-text { margin: 0; color: var(--ink-2); line-height: 1.5; }
"""
)


def render_evidence_pack_html(
    doc_json: dict, evidence_graph: dict | None = None
) -> str:
    """Hosted evidence pack: posts and comments grouped per behaviour."""
    graph = evidence_graph if isinstance(evidence_graph, dict) else {}
    title = _html_esc((doc_json or {}).get("title") or "42 evidence pack")
    sections_html: list[str] = []

    behaviour_sections = graph.get("behaviour_sections") or []
    if not behaviour_sections and isinstance(doc_json, dict):
        behaviour_sections = doc_json.get("behaviour_sections") or []

    for sec in behaviour_sections:
        if not isinstance(sec, dict):
            continue
        mk = str(sec.get("market") or "").upper()
        flag = _MARKET_FLAGS.get(mk, "")
        topic = _html_esc(
            sec.get("signal_topic") or sec.get("behaviour") or "Behaviour"
        )
        metric = sec.get("metric") if isinstance(sec.get("metric"), dict) else {}
        metric_bits = []
        if metric.get("post_count") is not None:
            metric_bits.append(f"{metric.get('post_count')} posts")
        if metric.get("comment_count") is not None:
            metric_bits.append(f"{metric.get('comment_count')} comments")
        if metric.get("engagement_total") is not None:
            metric_bits.append(f"{metric.get('engagement_total')} engagement")
        metric_html = " · ".join(metric_bits)

        def _voice_rows_html(rows: list, label: str) -> str:
            if not rows:
                return f'<p class="pack-voice-k">{label}</p><p class="prose">No {label.lower()} in this pull.</p>'
            items = [f'<p class="pack-voice-k">{label}</p>']
            for row in rows:
                if not isinstance(row, dict):
                    continue
                plat = _html_esc(str(row.get("platform") or ""))
                ctype = _html_esc(
                    str(row.get("content_type") or row.get("voice_kind") or "")
                )
                handle = _html_esc(
                    str(row.get("author_handle") or row.get("handle") or "")
                )
                metrics_line = _post_metrics_line(row)
                text = _html_esc(str(row.get("text") or ""))
                url = str(row.get("url") or "")
                link = (
                    f' <a class="ref-link" href="{_html_esc(url)}">View</a>'
                    if _is_web_link(url)
                    else ""
                )
                meta_tail = f" · {_html_esc(metrics_line)}" if metrics_line else ""
                items.append(
                    f'<div class="pack-row"><div class="pack-meta">{plat} · {ctype}'
                    f"{(' · @' + handle) if handle else ''}{meta_tail}</div>"
                    f'<p class="pack-text">{text}{link}</p></div>'
                )
            return "".join(items)

        def _voice_thread_html(post_row: dict) -> str:
            if not isinstance(post_row, dict):
                return ""
            plat = _html_esc(str(post_row.get("platform") or ""))
            handle = _html_esc(
                str(post_row.get("author_handle") or post_row.get("handle") or "")
            )
            eng = int(
                post_row.get("engagement_total") or post_row.get("engagement") or 0
            )
            text = _html_esc(str(post_row.get("text") or ""))
            url = str(post_row.get("url") or "")
            link = (
                f' <a class="ref-link" href="{_html_esc(url)}">View</a>'
                if _is_web_link(url)
                else ""
            )
            thread_comments = (
                post_row.get("thread_comments")
                if isinstance(post_row.get("thread_comments"), list)
                else []
            )
            if not thread_comments:
                ref_thread = post_row.get("thread_comments")
                if isinstance(ref_thread, list):
                    thread_comments = ref_thread
            header = (
                f'<div class="pack-row pack-post"><div class="pack-meta">{plat} · post'
                f"{(' · @' + handle) if handle else ''} · {eng} engagement</div>"
                f'<p class="pack-text">{text}{link}</p>'
            )
            if not thread_comments:
                return header + "</div>"
            comment_rows = []
            for c in thread_comments:
                if not isinstance(c, dict):
                    continue
                ch = _html_esc(str(c.get("handle") or ""))
                ceng = int(c.get("engagement") or 0)
                ctext = _html_esc(str(c.get("text") or ""))
                comment_rows.append(
                    f'<div class="pack-row pack-comment"><div class="pack-meta">'
                    f"{('@' + ch + ' · ') if ch else ''}{ceng} engagement</div>"
                    f'<p class="pack-text">{ctext}</p></div>'
                )
            comments_html = (
                f'<p class="pack-voice-k">Comments on this post ({len(thread_comments)})</p>'
                + "".join(comment_rows)
            )
            return header + comments_html + "</div>"

        posts = sec.get("voice_posts") or []
        comments = sec.get("voice_comments") or []
        if not posts and not comments:
            refs = sec.get("refs") or []
            posts = [r for r in refs if (r.get("voice_kind") or "post") != "comment"]
            comments = [r for r in refs if (r.get("voice_kind") or "") == "comment"]

        post_blocks = "".join(_voice_thread_html(p) for p in posts)
        orphan_html = _voice_rows_html(comments, "Standalone comments")
        inner = (
            f'<div class="pack-thread-group">{post_blocks}</div>' if post_blocks else ""
        ) + orphan_html
        sections_html.append(
            f'<section class="card lane-proof pack-behaviour">'
            f'<div class="card-head"><span class="lane-tag">Proof</span>'
            f'<h2 class="card-title">{flag} {mk} · {topic}</h2></div>'
            f'<p class="prose">{_html_esc(metric_html)}</p>{inner}</section>'
        )

    main_html = (
        f"<main>{''.join(sections_html)}</main>"
        if sections_html
        else '<main><p class="prose">No evidence rows stored for this artifact.</p></main>'
    )
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title>"
        f"<style>{_EVIDENCE_PACK_CSS}</style></head>"
        "<body>"
        '<div class="wrap">'
        '<header class="hero">'
        '<p class="kicker">42 · Evidence pack</p>'
        f"<h1>{title}</h1>"
        "</header>"
        f"{main_html}"
        "</div></body></html>"
    )


REFINE_RESEARCH_PROMPT = (
    'Refine this research doc JSON for persona "{persona}". Instruction: "{instruction}".\n'
    "Return JSON with the same keys. Rewrite prose only. Do NOT change evidence_indices.\n"
    "CURRENT:\n{current}\n"
)


def refine_research_prose(artifact: dict, instruction: str) -> dict | None:
    doc = artifact.get("doc_json") if isinstance(artifact.get("doc_json"), dict) else {}
    frozen_indices = []
    frozen_objective = None
    g = doc.get("grounded") or {}
    obj = g.get("objective")
    if isinstance(obj, dict):
        frozen_objective = list(obj.get("evidence_indices") or [])
    for b in g.get("user_insights") or g.get("behavioural_synthesis") or []:
        if isinstance(b, dict):
            frozen_indices.append(list(b.get("evidence_indices") or []))
    prompt = REFINE_RESEARCH_PROMPT.format(
        persona=artifact.get("persona_label") or "",
        instruction=instruction,
        current=json.dumps(doc),
    )
    for attempt in (1, 2):
        try:
            raw = _call_model(prompt)
            data = json.loads(raw)
        except Exception:
            continue
        refs = (artifact.get("evidence_graph") or {}).get("refs") or []
        allowed = _research_allowed_products(
            {
                "product_frame": artifact.get("product_frame")
                or (artifact.get("evidence_graph") or {}).get("product_frame")
            }
        )
        cleaned = _validate_research(data, refs, allowed_products=allowed)
        if cleaned is None:
            continue
        new_bullets = (cleaned.get("grounded") or {}).get("user_insights") or []
        for i, frozen in enumerate(frozen_indices):
            if i < len(new_bullets):
                new_bullets[i]["evidence_indices"] = frozen
        if frozen_objective is not None and isinstance(cleaned.get("grounded"), dict):
            obj_out = cleaned["grounded"].get("objective")
            if isinstance(obj_out, dict):
                obj_out["evidence_indices"] = frozen_objective
        return cleaned
    return None
