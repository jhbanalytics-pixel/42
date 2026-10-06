"""Pure pair-coverage selection of context facts; grants no evidence or market authority.

The selector takes already admitted facts and picks the ones that survive a capacity
limit. Matching is computed per requirement from that requirement's own terms plus any
validated alias, topic or entity relations supplied by the caller. A normalized literal
hit is recorded as one retrieval signal with its method name; it is not a relevance
label. Excerpts are matched in full and returned unchanged.
"""

import unicodedata
from datetime import datetime

FACT_FIELDS = (
    "market",
    "row_id",
    "content_digest",
    "excerpt",
    "source_label",
    "source_family",
    "platform",
    "published_at",
    "collected_at",
    "lane",
)
REQUIREMENT_FIELDS = ("requirement_id", "mandatory", "search_terms")
IDENTITY_FIELDS = ("market", "row_id", "content_digest")
RELATION_METHODS = ("alias", "entity", "topic")
LITERAL_METHOD = "normalized_literal"
BROAD_METHOD = "broad"


def _invalid():
    raise ValueError("context_selection_invalid")


def _text(value):
    if type(value) is not str or not value.strip():
        _invalid()
    return value


def _normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _identity(item):
    return tuple(item[field] for field in IDENTITY_FIELDS)


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        _invalid()
    if parsed.tzinfo is None:
        _invalid()
    return parsed.timestamp()


def _validated_facts(facts):
    if type(facts) is not list:
        _invalid()
    seen = set()
    observed = []
    for fact in facts:
        if type(fact) is not dict or any(field not in fact for field in FACT_FIELDS):
            _invalid()
        for field in ("market", "row_id", "content_digest", "lane", "source_label", "platform"):
            _text(fact[field])
        if type(fact["excerpt"]) is not str or (
            fact["source_family"] is not None and type(fact["source_family"]) is not str
        ):
            _invalid()
        key = _identity(fact)
        if key in seen:
            _invalid()
        seen.add(key)
        collected = _time(fact["collected_at"])
        published = None if fact["published_at"] is None else _time(fact["published_at"])
        observed.append(collected if published is None else published)
    return observed


def _validated_requirements(requirements):
    if type(requirements) is not list or not requirements:
        _invalid()
    result = {}
    for requirement in requirements:
        if type(requirement) is not dict or any(
            field not in requirement for field in REQUIREMENT_FIELDS
        ):
            _invalid()
        requirement_id = _text(requirement["requirement_id"])
        if (
            requirement_id in result
            or type(requirement["mandatory"]) is not bool
            or type(requirement["search_terms"]) is not list
        ):
            _invalid()
        result[requirement_id] = {
            "mandatory": requirement["mandatory"],
            "terms": sorted({_normalize(_text(term)) for term in requirement["search_terms"]}),
            "aliases": set(),
            "identities": {},
        }
    return result


def _validated_relations(relations, requirements):
    if relations is None:
        return
    if type(relations) is not list:
        _invalid()
    for relation in relations:
        if (
            type(relation) is not dict
            or set(relation) - {"requirement_id", "method", "term", "identity"}
            or any(field not in relation for field in ("requirement_id", "method", "term"))
            or relation["requirement_id"] not in requirements
            or relation["method"] not in RELATION_METHODS
        ):
            _invalid()
        requirement = requirements[relation["requirement_id"]]
        term = _text(relation["term"])
        if relation["method"] == "alias":
            if "identity" in relation:
                _invalid()
            requirement["aliases"].add(_normalize(term))
            continue
        identity = relation.get("identity")
        if type(identity) is not dict or set(identity) != set(IDENTITY_FIELDS):
            _invalid()
        key = tuple(_text(identity[field]) for field in IDENTITY_FIELDS)
        requirement["identities"].setdefault(key, set()).add((relation["method"], term))


def _match(facts, requirements):
    """Retrieval signals per fact, keyed by requirement, from that requirement's own terms."""
    signals = []
    for fact in facts:
        text = _normalize(fact["excerpt"])
        identity = _identity(fact)
        found = {}
        for requirement_id, requirement in requirements.items():
            hits = set()
            if not requirement["terms"]:
                hits.add((BROAD_METHOD, None))
            hits.update((LITERAL_METHOD, term) for term in requirement["terms"] if term in text)
            hits.update(("alias", alias) for alias in requirement["aliases"] if alias in text)
            hits.update(requirement["identities"].get(identity, ()))
            if hits:
                found[requirement_id] = sorted(hits, key=lambda hit: (hit[0], hit[1] or ""))
        signals.append(found)
    return signals


def _pair_rank(candidate):
    fact = candidate["fact"]
    return (
        -candidate["distinct"],
        -candidate["observed"],
        fact["lane"],
        fact["row_id"],
        fact["content_digest"],
    )


def _fill_rank(candidate, family_round):
    fact = candidate["fact"]
    return (
        -candidate["distinct"],
        family_round,
        -candidate["observed"],
        fact["market"],
        fact["lane"],
        fact["row_id"],
        fact["content_digest"],
    )


def _family_rounds(pool):
    rounds, counters = {}, {}
    for candidate in sorted(pool, key=lambda item: _fill_rank(item, 0)):
        family = candidate["fact"]["source_family"]
        rounds[_identity(candidate["fact"])] = counters.get(family, 0)
        counters[family] = counters.get(family, 0) + 1
    return rounds


def _allocate(candidates, requirements, markets, limit):
    """Mandatory pairs, then optional pairs, then a global fill; one slot per fact."""
    pairs = {
        mandatory: sorted(
            (requirement_id, market)
            for requirement_id, requirement in requirements.items()
            if requirement["mandatory"] is mandatory
            for market in markets
        )
        for mandatory in (True, False)
    }
    selected, used, covered = [], set(), set()

    def take(candidate):
        selected.append(candidate)
        used.add(_identity(candidate["fact"]))
        covered.update(
            (requirement_id, candidate["fact"]["market"]) for requirement_id in candidate["signals"]
        )

    for group in (pairs[True], pairs[False]):
        for requirement_id, market in group:
            if len(selected) >= limit:
                break
            if (requirement_id, market) in covered:
                continue
            options = [
                candidate
                for candidate in candidates
                if _identity(candidate["fact"]) not in used
                and candidate["fact"]["market"] == market
                and requirement_id in candidate["signals"]
            ]
            if options:
                take(min(options, key=_pair_rank))
    pool = [candidate for candidate in candidates if _identity(candidate["fact"]) not in used]
    rounds = _family_rounds(pool)
    ordered = sorted(pool, key=lambda item: _fill_rank(item, rounds[_identity(item["fact"])]))
    for candidate in ordered[: max(0, limit - len(selected))]:
        take(candidate)
    uncovered = [
        {"requirement_id": requirement_id, "market": market}
        for requirement_id, market in pairs[True]
        if (requirement_id, market) not in covered
    ]
    return selected, uncovered


def select_context_facts(facts, requirements, markets, *, limit, relations=None):
    if type(limit) is not int or limit < 1:
        _invalid()
    if type(markets) not in (list, tuple) or not markets or len(set(markets)) != len(markets):
        _invalid()
    for market in markets:
        _text(market)
    observed = _validated_facts(facts)
    plan = _validated_requirements(requirements)
    _validated_relations(relations, plan)
    signals = _match(facts, plan)
    candidates = [
        {
            "fact": fact,
            "signals": found,
            "observed": observed[index],
            "distinct": len(found),
        }
        for index, (fact, found) in enumerate(zip(facts, signals, strict=True))
        if found and fact["market"] in markets
    ]
    selected, uncovered = _allocate(candidates, plan, markets, limit)
    output = []
    for candidate in selected:
        flat = [
            {"requirement_id": requirement_id, "method": method, "term": term}
            for requirement_id, hits in sorted(candidate["signals"].items())
            for method, term in hits
        ]
        output.append({**candidate["fact"], "retrieval_signals": flat})
    coverage = {
        requirement_id: [
            index
            for index, candidate in enumerate(selected)
            if requirement_id in candidate["signals"]
        ]
        for requirement_id in plan
    }
    return {
        "facts": output,
        "coverage": coverage,
        "omitted_eligible_count": len(candidates) - len(selected),
        "uncovered_required_pairs": uncovered,
    }
