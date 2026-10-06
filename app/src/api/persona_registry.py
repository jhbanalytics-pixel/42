"""Deterministic persona registry for Console Research.

Loads research_personas.yaml, resolves user input fail-closed, scores evidence
refs in Python, and applies the quality gate before synthesis.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from rapidfuzz import fuzz

from . import bq

logger = logging.getLogger("listening_post.persona_registry")


def evidence_floor() -> int:
    raw = os.environ.get("RESEARCH_EVIDENCE_FLOOR", "40").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 40


THIN_FLOOR = 12
RESEARCH_EVIDENCE_FLOOR = evidence_floor()

_ENGINE_REF_TYPES = frozenset({"brief", "digest", "seed"})
_VOICE_REF_TYPES = frozenset({"post", "comment"})
_SEARCH_REF_TYPES = frozenset({"google_trends_rising", "lexicon"})

# Minimum refs retained per type before relevance cut (consolidated docs).
CONSOLIDATED_TYPE_FLOORS = {
    "brief": 8,
    "seed": 5,
    "google_trends_rising": 3,
    "post": 10,
    "comment": 5,
}
DEFAULT_TYPE_FLOORS = {
    "brief": 4,
    "seed": 2,
    "digest": 1,
    "google_trends_rising": 2,
    "lexicon": 1,
    "post": 8,
    "comment": 4,
}

ROOT_DIR = Path(__file__).resolve().parents[2]
PERSONAS_PATH = ROOT_DIR / "configs" / "research_personas.yaml"

DEFAULT_WEIGHTS = {
    "query_group_exact": 1.00,
    "seed_tag_overlap": 0.85,
    "anchor_in_brief": 0.75,
    "anchor_in_post": 0.65,
    "google_trends_term": 0.55,
    "lexicon_term": 0.50,
}

DEFAULT_THRESHOLDS = {
    "min_relevance": 0.55,
    "min_refs_for_inference": 8,
    "min_markets_with_signal": 1,
}

# Known engine topic keys for YAML validation. Extends bq labels with groups
# that appear in persona configs but not the desk label map.
_EXTRA_QUERY_GROUPS = frozenset({"tech_gemini_ai"})

VALID_QUERY_GROUPS = frozenset(bq._TOPIC_LABELS.keys()) | _EXTRA_QUERY_GROUPS

RESOLVE_THRESHOLD = 85
RESOLVE_TIE_MARGIN = 3

_GENERIC_PLACEHOLDER_TEXTS = frozenset(
    {
        "no snippet recorded.",
        "no engine signal recorded.",
        "no post text recorded.",
    }
)

_BRIEF_REF_TYPES = frozenset({"brief", "digest", "seed"})
_POST_REF_TYPES = frozenset({"post"})
_TRENDS_REF_TYPES = frozenset({"google_trends_rising"})

_registry_cache: dict | None = None
_yaml_meta_cache: dict | None = None

_DEFAULT_FRAME_PRESETS: list[dict[str, str]] = []


def _merge_defaults(persona: dict) -> dict:
    out = dict(persona)
    out.setdefault("weights", dict(DEFAULT_WEIGHTS))
    out.setdefault("thresholds", dict(DEFAULT_THRESHOLDS))
    out.setdefault("aliases", [])
    out.setdefault("anchor_tokens", [])
    out.setdefault("seed_tags", [])
    out.setdefault("query_groups", [])
    out.setdefault("default_markets", [])
    return out


def _validate_persona(persona_id: str, persona: dict) -> None:
    for group in persona.get("query_groups") or []:
        if group not in VALID_QUERY_GROUPS:
            raise ValueError(
                f"persona {persona_id!r}: unknown query_group {group!r} "
                f"(not in engine taxonomy)"
            )


def _load_yaml_meta() -> dict:
    global _yaml_meta_cache
    if _yaml_meta_cache is not None:
        return _yaml_meta_cache
    if not PERSONAS_PATH.is_file():
        _yaml_meta_cache = {
            "product_frame_presets": list(_DEFAULT_FRAME_PRESETS),
            "query_group_hints": {},
        }
        return _yaml_meta_cache
    raw = yaml.safe_load(PERSONAS_PATH.read_text(encoding="utf-8")) or {}
    _yaml_meta_cache = {
        "product_frame_presets": (
            raw.get("product_frame_presets")
            if "product_frame_presets" in raw
            else list(_DEFAULT_FRAME_PRESETS)
        ),
        "query_group_hints": raw.get("query_group_hints") or {},
    }
    return _yaml_meta_cache


def _normalize_frame_options(options: list) -> list:
    out = []
    for opt in options or []:
        if not isinstance(opt, dict):
            continue
        value = (opt.get("value") or opt.get("label") or "").strip()
        if not value:
            continue
        out.append({"value": value, "hint": (opt.get("hint") or "").strip()})
    return out


def _signal_topic_meta(group_id: str, hints: dict) -> dict:
    custom = hints.get(group_id) or {}
    if isinstance(custom, str):
        custom = {"hint": custom}
    label = (custom.get("label") or bq.topic_label(group_id)).strip()
    hint = (custom.get("hint") or f"Engine signal from {label} topic briefs and social posts").strip()
    return {"id": group_id, "label": label, "hint": hint}


def load_registry(*, include_disabled: bool = False) -> dict:
    """Parse research_personas.yaml. Skips disabled personas unless asked."""
    global _registry_cache
    if _registry_cache is not None and not include_disabled:
        return _registry_cache

    if not PERSONAS_PATH.is_file():
        raise FileNotFoundError(f"persona config missing: {PERSONAS_PATH}")

    raw = yaml.safe_load(PERSONAS_PATH.read_text(encoding="utf-8")) or {}
    _load_yaml_meta()
    personas_raw = raw.get("personas") or {}
    enabled: dict = {}
    all_personas: dict = {}

    for persona_id, body in personas_raw.items():
        if not isinstance(body, dict):
            continue
        merged = _merge_defaults(body)
        merged["id"] = persona_id
        _validate_persona(persona_id, merged)
        all_personas[persona_id] = merged
        if merged.get("enabled") is True:
            enabled[persona_id] = merged

    if include_disabled:
        return all_personas
    _registry_cache = enabled
    return enabled


def list_enabled_personas() -> list:
    """Picker payload: enabled personas only."""
    reg = load_registry()
    meta = _load_yaml_meta()
    hints = meta.get("query_group_hints") or {}
    default_frames = _normalize_frame_options(meta.get("product_frame_presets"))
    out = []
    for pid, p in sorted(reg.items()):
        groups = list(p.get("query_groups") or [])
        frame_opts = _normalize_frame_options(p.get("product_frame_options"))
        if not frame_opts:
            frame_opts = list(default_frames)
        out.append(
            {
                "id": pid,
                "label": p.get("label") or pid,
                "description": p.get("description") or "",
                "default_markets": list(p.get("default_markets") or []),
                "product_frame": p.get("product_frame") or "",
                "product_frame_options": frame_opts,
                "query_groups": groups,
                "signal_topics": [_signal_topic_meta(g, hints) for g in groups],
            }
        )
    return out


def get_persona(persona_id: str) -> dict | None:
    return load_registry().get((persona_id or "").strip())


def resolve_persona(user_input: str) -> dict | None:
    """Exact id match, then rapidfuzz on enabled aliases/labels. Fail-closed."""
    text = (user_input or "").strip()
    if not text:
        return None

    reg = load_registry()
    key = text.lower().replace(" ", "_")
    if key in reg:
        return reg[key]
    if text in reg:
        return reg[text]

    needle = text.lower()
    best_by_id: dict[str, int] = {}
    for persona_id, persona in reg.items():
        labels = [persona_id.replace("_", " "), persona.get("label") or ""]
        labels.extend(persona.get("aliases") or [])
        for label in labels:
            label_norm = (label or "").strip().lower()
            if not label_norm:
                continue
            score = fuzz.ratio(needle, label_norm)
            if score >= RESOLVE_THRESHOLD:
                prev = best_by_id.get(persona_id, 0)
                if score > prev:
                    best_by_id[persona_id] = score

    if not best_by_id:
        return None

    ranked = sorted(best_by_id.items(), key=lambda x: (-x[1], x[0]))
    top_id, top_score = ranked[0]
    if len(ranked) > 1 and ranked[1][1] >= top_score - RESOLVE_TIE_MARGIN:
        logger.warning("persona resolve tie for %r: %s", text, ranked[:3])
        return None
    return reg[top_id]


def _anchor_hits(text: str, anchors: list) -> int:
    hay = (text or "").lower()
    if not hay or not anchors:
        return 0
    return sum(1 for token in anchors if (token or "").lower() in hay)


def ref_has_substance(ref: dict) -> bool:
    """Drop hollow shells before ref_id assignment and synthesis."""
    if not isinstance(ref, dict):
        return False
    ref_type = str(ref.get("ref_type") or ref.get("source_type") or "")
    detail_lines = ref.get("detail_lines")
    if isinstance(detail_lines, list):
        if any(str(line).strip() for line in detail_lines):
            return True
    metrics = ref.get("metrics")
    if isinstance(metrics, dict) and any(v not in (None, "", 0) for v in metrics.values()):
        return True
    if ref_type in _BRIEF_REF_TYPES:
        return bool(
            str(ref.get("headline") or "").strip()
            or str(ref.get("trend_synthesis") or "").strip()
            or str(ref.get("cultural_context") or "").strip()
            or str(ref.get("behaviour") or "").strip()
            or len(str(ref.get("text") or "").strip()) >= 2
        )
    if ref_type in _VOICE_REF_TYPES:
        return bool(str(ref.get("text") or "").strip())
    if ref_type == "lexicon":
        return len(str(ref.get("term") or ref.get("text") or "").strip()) > 1
    for key in ("text", "headline", "title", "name", "term", "behaviour", "trend_synthesis"):
        val = str(ref.get(key) or "").strip()
        if not val:
            continue
        if val.lower() in _GENERIC_PLACEHOLDER_TEXTS:
            continue
        if len(val) >= 8:
            return True
    return False


def score_ref(candidate: dict, persona: dict) -> float:
    """Weighted relevance sum in Python. Higher is more on-persona."""
    weights = persona.get("weights") or DEFAULT_WEIGHTS
    ref_type = candidate.get("ref_type") or candidate.get("source_type") or ""
    score = 0.0

    query_group = (candidate.get("query_group") or "").strip()
    if query_group and query_group in (persona.get("query_groups") or []):
        score += float(weights.get("query_group_exact", 0))

    seed_tags = {str(t).lower() for t in (candidate.get("seed_tags") or [])}
    persona_tags = {str(t).lower() for t in (persona.get("seed_tags") or [])}
    if seed_tags and persona_tags:
        overlap = len(seed_tags & persona_tags) / len(persona_tags)
        score += float(weights.get("seed_tag_overlap", 0)) * overlap

    anchors = persona.get("anchor_tokens") or []
    text_blob = " ".join(
        str(candidate.get(k) or "")
        for k in ("text", "headline", "title", "name", "term", "behaviour", "note")
    )
    anchor_hits = _anchor_hits(text_blob, anchors)
    if anchor_hits:
        if ref_type in _BRIEF_REF_TYPES:
            score += float(weights.get("anchor_in_brief", 0)) * (anchor_hits / len(anchors))
        elif ref_type in _VOICE_REF_TYPES:
            score += float(weights.get("anchor_in_post", 0)) * (anchor_hits / len(anchors))
        else:
            score += float(weights.get("anchor_in_brief", 0)) * 0.5 * (anchor_hits / len(anchors))

    if ref_type in _TRENDS_REF_TYPES:
        score += float(weights.get("google_trends_term", 0))

    if ref_type == "lexicon":
        score += float(weights.get("lexicon_term", 0))

    if ref_type == "post":
        thread_comments = candidate.get("thread_comments") or []
        if thread_comments:
            thread_n = len(thread_comments)
            candidate["thread_size"] = thread_n
            score += 0.04 * min(thread_n, 10)

    candidate["relevance_score"] = round(score, 4)
    return score


def _voice_ref_type(ref_type: str) -> bool:
    return ref_type in _VOICE_REF_TYPES


def _type_floor_key(ref_type: str) -> str:
    rt = str(ref_type or "")
    if rt in _VOICE_REF_TYPES:
        return rt
    return rt


def _ref_identity(ref: dict) -> str:
    """Stable dedupe key: BQ row id first, then URL, then typed content fields."""
    rid = str(ref.get("id") or ref.get("content_id") or ref.get("ref_id") or "").strip()
    if rid:
        return f"id:{rid}"
    url = str(ref.get("url") or "").strip().lower()
    if url.startswith(("http://", "https://")):
        return f"url:{url}"
    trace = ref.get("bq_trace") if isinstance(ref.get("bq_trace"), dict) else {}
    row_key = str(trace.get("row_key") or "").strip()
    if row_key:
        return f"trace:{row_key}|{ref.get('ref_type')}|{ref.get('market')}"
    behaviour = str(ref.get("behaviour") or "").strip()
    if behaviour:
        return f"beh:{ref.get('ref_type')}|{ref.get('market')}|{behaviour[:120]}"
    headline = str(ref.get("headline") or "").strip()
    if headline:
        return f"head:{ref.get('ref_type')}|{ref.get('market')}|{headline[:120]}"
    text = str(ref.get("text") or "").strip()
    if text:
        return (
            f"txt:{ref.get('ref_type')}|{ref.get('market')}|{ref.get('query_group')}|"
            f"{ref.get('author_handle')}|{text[:160]}"
        )
    name = str(ref.get("name") or ref.get("title") or ref.get("term") or "").strip()
    if name:
        return f"name:{ref.get('ref_type')}|{ref.get('market')}|{name[:120]}"
    return (
        f"fallback:{ref.get('ref_type')}|{ref.get('market')}|{ref.get('query_group')}|"
        f"{ref.get('author_handle')}"
    )


def rank_and_filter(
    candidates: list,
    persona: dict,
    *,
    per_market_cap: int = 80,
    consolidated: bool = False,
) -> list:
    """Quota engine signal before relevance cut; voice fills as proof receipts."""
    threshold = float((persona.get("thresholds") or DEFAULT_THRESHOLDS).get("min_relevance", 0.55))
    floors = CONSOLIDATED_TYPE_FLOORS if consolidated else DEFAULT_TYPE_FLOORS
    scored: list[dict] = []
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        if not ref_has_substance(cand):
            continue
        score_ref(cand, persona)
        scored.append(cand)

    scored.sort(
        key=lambda c: (
            -(c.get("relevance_score") or 0),
            -(len(c.get("thread_comments") or [])),
            str(c.get("market") or ""),
            str(c.get("ref_type") or ""),
        )
    )

    by_type: dict[str, list[dict]] = {}
    for cand in scored:
        rt = str(cand.get("ref_type") or "")
        by_type.setdefault(_type_floor_key(rt), []).append(cand)

    picked: list[dict] = []
    picked_keys: set[str] = set()

    def _take(row: dict) -> None:
        key = _ref_identity(row)
        if key in picked_keys:
            return
        picked_keys.add(key)
        picked.append(row)

    for type_key, floor in floors.items():
        if type_key == "brief":
            pool = (by_type.get("brief") or []) + (by_type.get("digest") or [])
        elif type_key == "lexicon":
            pool = by_type.get(type_key) or []
        else:
            pool = by_type.get(type_key) or []
        for row in pool[: int(floor)]:
            _take(row)

    for cand in scored:
        if _ref_identity(cand) in picked_keys:
            continue
        if float(cand.get("relevance_score") or 0) < threshold:
            continue
        _take(cand)

    per_market: dict[str, int] = {}
    out: list[dict] = []
    for cand in picked:
        market = str(cand.get("market") or "all")
        if per_market.get(market, 0) >= per_market_cap:
            continue
        per_market[market] = per_market.get(market, 0) + 1
        out.append(cand)

    floor_target = RESEARCH_EVIDENCE_FLOOR if consolidated else max(25, RESEARCH_EVIDENCE_FLOOR // 2)
    if len(out) < floor_target:
        out_keys = {_ref_identity(r) for r in out}
        for cand in scored:
            if len(out) >= floor_target:
                break
            key = _ref_identity(cand)
            if key in out_keys:
                continue
            market = str(cand.get("market") or "all")
            if per_market.get(market, 0) >= per_market_cap:
                continue
            per_market[market] = per_market.get(market, 0) + 1
            out_keys.add(key)
            out.append(cand)

    out.sort(
        key=lambda c: (
            0 if str(c.get("ref_type") or "") in _ENGINE_REF_TYPES else 1,
            0 if str(c.get("ref_type") or "") in _SEARCH_REF_TYPES else 2,
            -(c.get("relevance_score") or 0),
            -(len(c.get("thread_comments") or [])),
            str(c.get("market") or ""),
            str(c.get("ref_type") or ""),
        )
    )
    return out


def quality_gate(evidence: list, persona: dict, *, focus_scoped: bool = False) -> dict:
    """thin|moderate|strong plus lane permissions. Strips inference when thin."""
    thresholds = persona.get("thresholds") or DEFAULT_THRESHOLDS
    min_inference = 3 if focus_scoped else int(thresholds.get("min_refs_for_inference", 8))
    min_markets = int(thresholds.get("min_markets_with_signal", 1))

    markets_with_signal = {str(e.get("market") or "") for e in evidence if e.get("market")}
    total = len(evidence)
    scan_proof = focus_scoped and any(
        e.get("source") == "behaviour_scan" or str(e.get("ref_type") or "") == "post"
        for e in evidence
    )

    if total < min_inference or len(markets_with_signal) < min_markets:
        level = "thin"
    elif total < THIN_FLOOR:
        level = "moderate"
    else:
        level = "strong"

    strong_corroboration = any(
        (e.get("ref_type") == "google_trends_rising")
        and float(e.get("relevance_score") or 0) > 0.7
        for e in evidence
    )

    allow_inference = level != "thin"
    allow_positioning = level == "strong" or (level == "moderate" and strong_corroboration)
    allow_activation = allow_positioning

    if focus_scoped and scan_proof and total >= min_inference:
        allow_inference = True
        if level == "thin":
            level = "moderate"
        if total >= 5 or strong_corroboration:
            allow_positioning = True
            allow_activation = True

    if level == "thin":
        allow_inference = False
        allow_positioning = False
        allow_activation = False
    elif level == "moderate" and not strong_corroboration and not (focus_scoped and scan_proof and total >= 5):
        allow_positioning = False
        allow_activation = False

    return {
        "level": level,
        "total_refs": total,
        "markets_with_signal": sorted(m for m in markets_with_signal if m),
        "allow_inference": allow_inference,
        "allow_positioning": allow_positioning,
        "allow_activation": allow_activation,
        "strong_corroboration": strong_corroboration,
    }


__all__ = [
    "load_registry",
    "list_enabled_personas",
    "get_persona",
    "resolve_persona",
    "score_ref",
    "ref_has_substance",
    "rank_and_filter",
    "quality_gate",
]
