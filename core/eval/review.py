"""The weekly human review routine (TRUST.md section 6, BUILD.md task 1.20).

Monday: prepare_week draws the claim and card sample and writes the sheet reviewers fill in.
After review: record_week reads the filled sheet, stores the labels and scores the week.
Rows are read and written through injected callables, so BigQuery is wired by the caller.

Verdicts use the rubric.md vocabulary: supported, contradicted, unverified (TRUST.md's
"unverifiable" is read as unverified). Cards are real or not_real, useful yes or no.
Every reviewer checks South Africa in full. NG and KE rows are evidence_only: the reviewer
checks that the post exists and says what the claim says, with no tone or language
judgement, so those rows never count toward the non-English support figure.

Language accuracy itself comes from the native-speaker check: native_sample draws 15 quotes
from the week's rotation slot, the speaker marks each gloss and tone label right or wrong,
and score_native flags any language under 80% as tone capped at Single source. It neither caps
nor clears a language on fewer than 5 quotes. The Pidgin, Yoruba, Hausa and Igbo, and Swahili
and Sheng slots have no named reviewer, so they get no sheet and stay capped.
"""

import csv
import io
import json
import math
import random
import re
from collections import Counter
from datetime import date

MARKETS = {"ZA": "full", "NG": "evidence_only", "KE": "evidence_only"}
GROUPS = {"ZA": "ZA", "NG": "NG_KE", "KE": "NG_KE"}
CLAIM_VERDICTS = {"supported", "contradicted", "unverified"}
CARD_VERDICTS = {"real", "not_real"}
ALIASES = {"unverifiable": "unverified", "not real": "not_real", "notreal": "not_real"}
USEFUL = {"yes", "no"}
RANDOM_CLAIMS, RISK_CLAIMS, RANDOM_CARDS, TOP_RANK = 30, 10, 11, 3
DOUBLE_SHARE = 0.10
KAPPA_MIN, GRADER_MIN = 0.6, 0.85
CUMULATIVE_WEEKS = 4
MIN_CALIBRATION = 300
# Below this n the rule of three is loose enough to mislead (3/n passes 1 at n=2), so the exact bound is used.
RULE_OF_THREE_MIN = 30
SHEET_COLUMNS = ["week", "kind", "id", "market", "stratum", "review_scope", "double", "non_english", "language",
                 "url", "text", "quote", "verdict", "useful", "reviewer", "reason"]
FEEDBACK_FIELDS = ["week", "kind", "id", "market", "stratum", "double", "non_english", "language", "verdict",
                   "useful"]
NATIVE_FEEDBACK_FIELDS = ["week", "id", "language", "gloss_correct", "tone_correct"]
FEEDBACK_SOURCE = "42_review_v1"  # marks this routine's rows in the shared feedback table

# TRUST.md section 6, in order: one slot a week. Codes are ISO 639 where one exists.
ROTATION = [
    ("isiZulu and isiXhosa", ("zu", "xh")), ("Sesotho", ("st",)), ("Setswana", ("tn",)),
    ("Sepedi and Xitsonga", ("nso", "ts")), ("Afrikaans", ("af",)), ("Pidgin", ("pcm",)), ("Yoruba", ("yo",)),
    ("Hausa and Igbo", ("ha", "ig")), ("Swahili and Sheng", ("sw", "sheng")),
]
ROTATION_START = (2026, 40)  # the first slot runs in ISO week 2026-W40
LANGUAGES = [code for _, codes in ROTATION for code in codes]
NATIVE_QUOTES = 15
NATIVE_MIN = 5  # under this many quotes score_native neither caps nor clears a language
LANGUAGE_FLOOR, LANGUAGE_TARGET = 0.80, 0.90  # under the floor a language is flagged
TONE_CAPPED = "tone capped at Single source"
# No Lagos or Nairobi reviewer is named (docs/full-42/progress/L3.md, Decisions from Albert), so these slots get
# no sheet and their languages stay tone capped at Single source until Albert names one.
UNREVIEWED_SLOTS = ("Pidgin", "Yoruba", "Hausa and Igbo", "Swahili and Sheng")
NO_REVIEWER = tuple(code for name, codes in ROTATION if name in UNREVIEWED_SLOTS for code in codes)
UNREVIEWED = "unreviewed: no Lagos or Nairobi reviewer named"
NATIVE_COLUMNS = ["week", "id", "language", "market", "url", "quote", "gloss", "tone", "gloss_correct",
                  "tone_correct", "reviewer", "reason"]

CAUSAL = re.compile(r"\b(because|driven by|due to|led to)\b", re.IGNORECASE)
# Common words per language that rarely occur in English. "pole" and "una" are out: both are English words.
LANGUAGE_MARKERS = {
    "pcm": {"abeg", "wetin", "wahala", "sabi", "pikin", "comot", "oga", "dey", "sef", "abi", "shey"},
    "sheng": {"niaje", "manze", "msee", "fiti", "mbogi", "rieng", "mathree", "buda"},
    "zu": {"ngiyabonga", "sawubona", "yebo", "hhayi", "ukuthi", "kakhulu", "angazi", "manje", "sharp-sharp"},
    "sw": {"hapana", "asante", "habari", "sana", "rafiki", "hii", "kweli", "sasa", "wewe", "mimi"},
    "af": {"dankie", "baie", "lekker", "nie", "asseblief", "hoekom", "julle", "hulle", "ek", "jy", "mooi",
           "sommer"},
    "st": {"dumela", "ntate", "haholo", "leboha", "hantle", "joang", "hle", "ausi"},
    "tn": {"dumelang", "leboga", "thata", "jang", "sentle", "rra", "gape", "tlhe"},
}
MARKERS = set().union(*LANGUAGE_MARKERS.values())


def _words(text):
    return re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)?", (text or "").lower())


def non_english(text):
    """True when a quote looks non-English: mostly non-ASCII letters, or a dialect marker word."""
    words = _words(text)
    if not words:
        return False
    letters = "".join(words).replace("-", "")
    if sum(not ch.isascii() for ch in letters) / len(letters) > 0.1:
        return True
    return any(w in MARKERS for w in words)


def language_of(text):
    """The language whose marker words appear most often in text, or "" when none do."""
    words = _words(text)
    hits = {lang: sum(w in markers for w in words) for lang, markers in LANGUAGE_MARKERS.items()}
    best = max(hits, key=hits.get)
    return best if hits[best] else ""


def _quote(claim):
    if claim.get("quote"):
        return claim["quote"]
    return " / ".join(q.get("text", "") for q in claim.get("quotes") or [])


def _is_non_english(claim):
    lang = (claim.get("quote_lang") or "").lower()
    return (lang not in ("", "en")) or non_english(_quote(claim))


def _language(claim):
    """The quote's language code: quote_lang when set, else a marker guess, else "unknown" if non-English."""
    lang = (claim.get("quote_lang") or "").lower()
    if lang and lang != "en":
        return lang
    if not _is_non_english(claim):
        return ""
    return language_of(_quote(claim)) or "unknown"


def risk_score(claim):
    """One point each for causal wording, a partial support verdict and a non-English quote.

    Numbers score one point, and one more when any number lacks the query that produced it,
    so an unsourced number always outranks a sourced one.
    """
    score = 0
    numbers = claim.get("numbers") or []
    if numbers:
        score += 1
    if any(not n.get("query") for n in numbers):
        score += 1
    if CAUSAL.search(claim.get("text") or ""):
        score += 1
    if (claim.get("support") or "").lower() == "partial":
        score += 1
    if _is_non_english(claim):
        score += 1
    return score


def _scope(market):
    if market not in MARKETS:
        raise ValueError(f"market {market!r} is not one of {sorted(MARKETS)}")
    return MARKETS[market]


def _mark_double(rows, rng):
    k = int(len(rows) * DOUBLE_SHARE + 0.5)
    chosen = set(rng.sample(range(len(rows)), k))
    for i, row in enumerate(rows):
        row["double"] = i in chosen
    return rows


def _row(kind, week, item, stratum, **extra):
    return {"kind": kind, "week": week, "id": item["id"], "market": item["market"], "stratum": stratum,
            "review_scope": _scope(item["market"]), "url": item.get("url", ""), "text": item.get("text", ""),
            **extra}


def claim_sample(claims, *, week, seed):
    """30 uniformly random claims plus the 10 highest-risk others, deterministic per week and seed."""
    pool = sorted(claims, key=lambda c: c["id"])
    for c in pool:
        _scope(c.get("market"))
    rng = random.Random(f"claims:{week}:{seed}")
    picked = rng.sample(pool, min(RANDOM_CLAIMS, len(pool)))
    ids = {c["id"] for c in picked}
    rest = [(c, rng.random()) for c in pool if c["id"] not in ids]
    rest.sort(key=lambda pair: (-risk_score(pair[0]), pair[1]))
    risky = [c for c, _ in rest[:RISK_CLAIMS]]
    rows = []
    for stratum, group in (("random", picked), ("risk", risky)):
        for c in group:
            rows.append(_row("claim", week, c, stratum, quote=_quote(c), non_english=_is_non_english(c),
                             language=_language(c), risk=risk_score(c)))
    return _mark_double(rows, rng)


def card_sample(cards, *, week, seed):
    """Every top-3 card per market plus 11 random others, deterministic per week and seed."""
    pool = sorted(cards, key=lambda c: c["id"])
    for c in pool:
        _scope(c.get("market"))
    rng = random.Random(f"cards:{week}:{seed}")
    top = sorted((c for c in pool if (c.get("rank") or TOP_RANK + 1) <= TOP_RANK),
                 key=lambda c: (c["market"], c["rank"]))
    others = [c for c in pool if (c.get("rank") or TOP_RANK + 1) > TOP_RANK]
    picked = rng.sample(others, min(RANDOM_CARDS, len(others)))
    rows = []
    for stratum, group in (("top3", top), ("random", picked)):
        for c in group:
            rows.append(_row("card", week, c, stratum, quote=c.get("quote", ""), non_english=False,
                             language="", rank=c.get("rank")))
    return _mark_double(rows, rng)


def review_sheet(rows):
    """CSV for reviewers. A double-reviewed row appears twice, one line per reviewer."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=SHEET_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        line = {**row, "double": str(bool(row["double"])).lower(),
                "non_english": str(bool(row.get("non_english"))).lower(),
                "verdict": "", "useful": "", "reviewer": "", "reason": ""}
        writer.writerow(line)
        if row["double"]:
            writer.writerow(line)
    return out.getvalue()


def _verdict(value):
    v = (value or "").strip().lower()
    return ALIASES.get(v, v.replace(" ", "_"))


def read_sheet(csv_text):
    """Validate a filled sheet and return one label per line. Refuses blank or unknown values."""
    labels, problems = [], []
    for line in csv.DictReader(io.StringIO(csv_text)):
        item = line.get("id") or "?"
        kind = (line.get("kind") or "").strip()
        verdict = _verdict(line.get("verdict"))
        useful = (line.get("useful") or "").strip().lower()
        reviewer = (line.get("reviewer") or "").strip()
        market = (line.get("market") or "").strip()
        vocab = {"claim": CLAIM_VERDICTS, "card": CARD_VERDICTS}.get(kind)
        if vocab is None:
            problems.append(f"{item}: kind {kind!r} is not claim or card")
            continue
        if market not in MARKETS:
            problems.append(f"{item}: market {market!r} is not one of {sorted(MARKETS)}")
            continue
        if not verdict:
            problems.append(f"{item}: verdict is blank")
        elif verdict not in vocab:
            problems.append(f"{item}: verdict {line.get('verdict')!r} is not one of {sorted(vocab)}")
        if kind == "card" and useful not in USEFUL:
            problems.append(f"{item}: useful {line.get('useful')!r} is not yes or no")
        if not reviewer:
            problems.append(f"{item}: reviewer is blank")
        labels.append({**line, "kind": kind, "market": market, "review_scope": MARKETS[market],
                       "verdict": verdict, "useful": useful, "reviewer": reviewer,
                       "language": (line.get("language") or "").strip().lower(),
                       "double": line.get("double", "").strip().lower() == "true",
                       "non_english": line.get("non_english", "").strip().lower() == "true"})
    if problems:
        raise ValueError("sheet refused:\n" + "\n".join(problems))
    return labels


def cohens_kappa(a, b):
    """Cohen's kappa for two raters; None when there are no pairs or only one category is ever used."""
    n = len(a)
    if n == 0 or n != len(b) or len(set(a) | set(b)) < 2:
        return None
    ca, cb = Counter(a), Counter(b)
    p_o = sum(x == y for x, y in zip(a, b)) / n
    p_e = sum(ca[c] * cb[c] for c in ca) / (n * n)
    return (p_o - p_e) / (1 - p_e)


def _binom_cdf(x, n, p):
    if p <= 0:
        return 1.0
    if p >= 1:
        return 0.0 if x < n else 1.0
    lp, lq = math.log(p), math.log1p(-p)
    lgn = math.lgamma(n + 1)
    return sum(math.exp(lgn - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * lp + (n - k) * lq)
               for k in range(x + 1))


def clopper_pearson_upper(x, n, *, level=0.95):
    """Exact one-sided upper bound on a binomial rate: the p where P(X <= x) = 1 - level."""
    if x >= n:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if _binom_cdf(x, n, mid) > 1 - level:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def error_upper_bound(errors, n):
    """95% upper bound on the error rate.

    At zero errors: the exact 1 - 0.05 ** (1 / n) below 30 items, the rule of three (3 / n, which
    TRUST.md quotes) from 30 up. Clopper-Pearson whenever there is an error.
    """
    if n == 0:
        return None, None
    if errors == 0:
        if n < RULE_OF_THREE_MIN:
            return min(1.0, 1 - 0.05 ** (1 / n)), "exact"
        return 3 / n, "rule_of_three"
    return clopper_pearson_upper(errors, n), "clopper_pearson"


def _items(labels):
    """Group label lines into items keyed by week, kind and id."""
    items = {}
    for lab in labels:
        items.setdefault((lab["week"], lab["kind"], lab["id"]), []).append(lab)
    return items


def _claim_verdict(lines):
    verdicts = {lab["verdict"] for lab in lines}
    for v in ("contradicted", "unverified"):
        if v in verdicts:
            return v
    return "supported"


def _claims_block(items):
    verdicts = [_claim_verdict(lines) for lines in items]
    n = len(verdicts)
    contradicted, unverified = verdicts.count("contradicted"), verdicts.count("unverified")
    errors = contradicted + unverified
    upper, bound = error_upper_bound(errors, n)
    return {"n": n, "errors": errors, "contradicted": contradicted, "unverified": unverified,
            "error_rate": errors / n if n else None, "upper_95": upper, "bound": bound}


def _share(items, test):
    return sum(test(lines) for lines in items) / len(items) if items else None


def _real(lines):
    return all(lab["verdict"] == "real" for lab in lines)


def _cards_block(items):
    top = [lines for lines in items if lines[0]["stratum"] == "top3"]
    rand = [lines for lines in items if lines[0]["stratum"] == "random"]
    # No pooled precision: top-3 is the Brief precision metric (TRUST.md section 7), random is the base rate.
    return {"n": len(items), "precision_top3": _share(top, _real), "precision_random": _share(rand, _real),
            "useful": _share(items, lambda lines: all(lab["useful"] == "yes" for lab in lines))}


def _kappa(items):
    out = {}
    for kind in ("claims", "cards"):
        a, b = [], []
        for lines in items.values():
            if lines[0]["kind"] != kind[:-1]:
                continue
            first = lines[0]
            second = next((lab for lab in lines[1:] if lab["reviewer"] != first["reviewer"]), None)
            if second is not None:
                a.append(first["verdict"])
                b.append(second["verdict"])
        out[kind] = {"n": len(a), "value": cohens_kappa(a, b)}
    return out


def _supported(lines):
    return _claim_verdict(lines) == "supported"


def _non_english_block(items):
    """Support rate on non-English claims, pooled and per quote language; any language under the floor is flagged."""
    by_lang = {}
    for lines in items:
        by_lang.setdefault(lines[0].get("language") or "unknown", []).append(lines)
    by_language = {}
    for lang, group in sorted(by_lang.items()):
        share = _share(group, _supported)
        by_language[lang] = {"n": len(group), "support": share, "flagged": share < LANGUAGE_FLOOR}
    return {"n": len(items), "support": _share(items, _supported), "by_language": by_language,
            "flagged": [lang for lang, block in by_language.items() if block["flagged"]]}


def _random_claims(items):
    return [lines for (_, kind, _), lines in items.items() if kind == "claim" and lines[0]["stratum"] == "random"]


def score_week(labels, *, prior=(), grader=None):
    """Score one week of labels. prior holds up to three earlier weeks for the 4-week bound.

    Error rates use the random stratum only; the risk stratum is reported, never pooled in.
    A claim is an error when any reviewer marks it contradicted or unverified, and a card is
    real or useful only when every reviewer says so. grader maps claim id to the promptfoo verdict.
    """
    weeks = {lab["week"] for lab in labels}
    if len(weeks) != 1:
        raise ValueError(f"labels must hold exactly one week, got {sorted(weeks)}")
    week = weeks.pop()
    items = _items(labels)
    claims = [lines for (_, kind, _), lines in items.items() if kind == "claim"]
    cards = [lines for (_, kind, _), lines in items.items() if kind == "card"]
    random_claims = _random_claims(items)
    risk = [_claim_verdict(lines) for lines in claims if lines[0]["stratum"] == "risk"]

    by_group = {}
    for group in ("ZA", "NG_KE"):
        by_group[group] = {"claims": _claims_block([x for x in random_claims if GROUPS[x[0]["market"]] == group]),
                           "cards": _cards_block([x for x in cards if GROUPS[x[0]["market"]] == group])}

    non_english_claims = [lines for lines in claims
                          if lines[0]["non_english"] and all(lab["review_scope"] == "full" for lab in lines)]
    kappa = _kappa(items)

    grader_block = None
    if grader is not None:
        pairs = [(lab["verdict"], _verdict(grader[lab["id"]]))
                 for lab in labels if lab["kind"] == "claim" and lab["id"] in grader]
        grader_block = {"n": len(pairs),
                        "agreement": sum(h == g for h, g in pairs) / len(pairs) if pairs else None}

    history = list(prior) + list(labels)
    all_weeks = sorted({lab["week"] for lab in history})
    if len(all_weeks) > CUMULATIVE_WEEKS:
        raise ValueError(f"cumulative window covers {len(all_weeks)} weeks; pass at most 4 weeks in total")
    all_items = _items(history)
    cumulative = {"weeks": all_weeks, **_claims_block(_random_claims(all_items)), "kappa": _kappa(all_items)}

    measured = [k["value"] for k in kappa.values() if k["value"] is not None]
    return {
        "week": week,
        "claims": _claims_block(random_claims),
        "risk": {"n": len(risk), "errors": sum(v != "supported" for v in risk)},
        "cards": _cards_block(cards),
        "by_group": by_group,
        "non_english_support": _non_english_block(non_english_claims),
        "kappa": kappa,
        "grader": grader_block,
        "cumulative": cumulative,
        "passes": {
            "kappa": all(v >= KAPPA_MIN for v in measured) if measured else None,
            "grader": (grader_block["agreement"] >= GRADER_MIN
                       if grader_block and grader_block["agreement"] is not None else None),
        },
    }


def _correct(lab):
    if isinstance(lab, str):
        v = _verdict(lab)
        if v not in CLAIM_VERDICTS:
            raise ValueError(f"label {lab!r} is neither a boolean nor one of {sorted(CLAIM_VERDICTS)}")
        return v == "supported"
    return bool(lab)


def conformal_threshold(scores, labels, *, ids=None, alpha=0.03, delta=0.05, min_labels=MIN_CALIBRATION):
    """Lowest checker score t such that claims scoring t or more are wrong at most alpha of the time.

    labels are booleans (any truthy non-string is True) or claim verdict strings, where only
    "supported" is correct; any other string is refused, so "True" or "yes" cannot silently read
    as wrong. With ids, lines for the same claim collapse to one label, wrong if any reviewer
    marked it wrong, before counting toward min_labels. Returns None below min_labels claims, or
    when no threshold can be certified: keep supported-only.

    Split-conformal calibration on the human-labelled claims, in its Learn-then-Test form
    (Angelopoulos et al. 2021), because TRUST.md asks for the wrong share among accepted claims
    and plain split conformal (reference 15) bounds only the chance a claim is both wrong and
    accepted. Thresholds are tested from the highest score down with an exact Clopper-Pearson
    bound, the finite-sample correction, and the walk stops at the first one that fails. With
    confidence 1 - delta, accepted claims are then wrong at most alpha of the time, as long as
    new claims are exchangeable with the calibration set.
    """
    if len(scores) != len(labels):
        raise ValueError(f"{len(scores)} scores but {len(labels)} labels")
    ok = [_correct(lab) for lab in labels]
    if ids is None:
        pairs = list(zip(scores, ok))
    else:
        if len(ids) != len(labels):
            raise ValueError(f"{len(ids)} ids but {len(labels)} labels")
        claims = {}
        for claim_id, s, good in zip(ids, scores, ok):
            if claim_id in claims and claims[claim_id][0] != s:
                raise ValueError(f"claim {claim_id!r} has two checker scores, {claims[claim_id][0]} and {s}")
            claims[claim_id] = (s, claims.get(claim_id, (s, True))[1] and good)
        pairs = list(claims.values())
    n = len(pairs)
    if n < min_labels:
        return None
    pairs.sort(key=lambda p: -p[0])
    # Below this many accepted claims even zero errors cannot certify alpha, so testing starts there.
    first = math.ceil(math.log(delta) / math.log(1 - alpha))
    best, accepted, wrong = None, 0, 0
    for i, (s, good) in enumerate(pairs):
        accepted += 1
        wrong += not good
        if (i + 1 < n and pairs[i + 1][0] == s) or accepted < first:
            continue
        if clopper_pearson_upper(wrong, accepted, level=1 - delta) > alpha:
            break
        best = s
    return best


def to_feedback(label):
    """One label as a row of intelligence_42_agent.feedback (who, what, reason); what is a JSON object.

    A label with gloss_correct is a native-speaker label and is stored as kind "native".
    """
    if "gloss_correct" in label:
        what = {"kind": "native", **{field: label[field] for field in NATIVE_FEEDBACK_FIELDS}}
        gloss = what["gloss_correct"]
        what["gloss_correct"], what["tone_correct"] = None if gloss is None else bool(gloss), bool(what["tone_correct"])
    else:
        what = {field: label.get(field, "") for field in FEEDBACK_FIELDS}
        what["double"], what["non_english"] = bool(what["double"]), bool(what["non_english"])
    what["source"] = FEEDBACK_SOURCE
    return {"who": label["reviewer"], "what": json.dumps(what, sort_keys=True, ensure_ascii=False),
            "reason": label.get("reason") or ""}


def from_feedback(row):
    """Invert to_feedback: a feedback row back into a label score_week or score_native can read."""
    what = json.loads(row["what"])
    what.pop("source", None)
    back = {**what, "reviewer": row["who"], "reason": row["reason"] or ""}
    if what["kind"] != "native":
        back["review_scope"] = _scope(what["market"])
    return back


def review_labels(rows, kinds):
    """Feedback rows back as labels, keeping only this routine's rows of the given kinds.

    The feedback table also holds app taps and free text; any row whose what is not a JSON
    object carrying our source is skipped, never raised on.
    """
    labels = []
    for row in rows:
        try:
            what = json.loads(row.get("what") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(what, dict) and what.get("source") == FEEDBACK_SOURCE and what.get("kind") in kinds:
            labels.append(from_feedback(row))
    return labels


def rotation(week):
    """The (slot name, language codes) under native review in ISO week "YYYY-Www"."""
    year, num = (int(part) for part in week.split("-W"))
    start = date.fromisocalendar(*ROTATION_START, 1)
    weeks = (date.fromisocalendar(year, num, 1) - start).days // 7
    return ROTATION[weeks % len(ROTATION)]


def native_slot(week):
    """The week's rotation slot and its status: "due", or UNREVIEWED when no native speaker is named for it."""
    name, codes = rotation(week)
    return {"week": week, "slot": name, "languages": codes,
            "status": UNREVIEWED if name in UNREVIEWED_SLOTS else "due"}


def native_sample(quotes, *, week, seed, language):
    """Up to 15 quotes for the native-speaker check, deterministic per week, seed and language.

    language is one code from the week's rotation slot, or None for the whole slot. A language
    outside this week's slot is refused, so the rotation cannot be skipped by accident. For the
    whole slot the 15 are split between its languages in turn (8 and 7 for two), and a language
    with too few quotes leaves its places to the other, so no language goes unchecked while it
    has quotes. A quote with no tone label is never drawn, since the check asks whether the tone
    is right. quotes are dicts with id, language, market, text (or quote), url, gloss and tone,
    as core/understand/sql/native_quotes.sql returns them. An unreviewed slot (native_slot) draws
    nothing, so no sheet goes out that nobody can fill.
    """
    name, codes = rotation(week)
    if language is not None and language not in codes:
        raise ValueError(f"{language!r} is not in {week}'s rotation slot {name} {codes}")
    if name in UNREVIEWED_SLOTS:
        return []
    langs = list(codes) if language is None else [language]
    pool = sorted((q for q in quotes if (q.get("language") or "").lower() in langs and (q.get("tone") or "").strip()),
                  key=lambda q: q["id"])
    by_lang = {code: [q for q in pool if q["language"].lower() == code] for code in langs}
    quota, left = dict.fromkeys(langs, 0), NATIVE_QUOTES
    while left and any(quota[code] < len(by_lang[code]) for code in langs):
        for code in langs:
            if left and quota[code] < len(by_lang[code]):
                quota[code] += 1
                left -= 1
    rng = random.Random(f"native:{week}:{seed}:{language or name}")
    picked = [q for code in langs for q in rng.sample(by_lang[code], quota[code])]
    return [{"week": week, "id": q["id"], "language": q["language"].lower(), "market": q.get("market", ""),
             "url": q.get("url", ""), "quote": q.get("quote") or q.get("text", ""), "gloss": q.get("gloss", ""),
             "tone": q.get("tone", "")} for q in picked]


def native_sheet(rows):
    """CSV for the native speaker: the quote, the machine gloss and tone label, and two yes or no columns."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=NATIVE_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, "gloss_correct": "", "tone_correct": "", "reviewer": "", "reason": ""})
    return out.getvalue()


def read_native_sheet(csv_text):
    """Validate a filled native sheet. Refuses a blank or non yes/no mark, a missing reviewer or an unknown language.

    A quote shown with no gloss (native_quotes.sql writes none) is checked for tone alone: its
    gloss_correct may be left blank and is read as None.
    """
    labels, problems = [], []
    for line in csv.DictReader(io.StringIO(csv_text)):
        item = line.get("id") or "?"
        language = (line.get("language") or "").strip().lower()
        reviewer = (line.get("reviewer") or "").strip()
        marks = {}
        if language not in LANGUAGES:
            problems.append(f"{item}: language {line.get('language')!r} is not one of {LANGUAGES}")
        for col in ("gloss_correct", "tone_correct"):
            value = (line.get(col) or "").strip().lower()
            if col == "gloss_correct" and not value and not (line.get("gloss") or "").strip():
                marks[col] = None
                continue
            if value not in USEFUL:
                problems.append(f"{item}: {col} {line.get(col)!r} is not yes or no")
            marks[col] = value == "yes"
        if not reviewer:
            problems.append(f"{item}: reviewer is blank")
        labels.append({**line, "language": language, "reviewer": reviewer, **marks})
    if problems:
        raise ValueError("native sheet refused:\n" + "\n".join(problems))
    return labels


def _native_block(items):
    n = len(items)
    glossed = [lines for lines in items if any(lab["gloss_correct"] is not None for lab in lines)]
    gloss = _share(glossed, lambda lines: all(lab["gloss_correct"] is not False for lab in lines))
    tone = _share(items, lambda lines: all(lab["tone_correct"] for lab in lines))
    both = _share(items, lambda lines: all(lab["gloss_correct"] is not False and lab["tone_correct"]
                                           for lab in lines))
    basis = ("tone only" if not glossed else "gloss and tone" if len(glossed) == n
             else "gloss and tone, tone only where no gloss")
    return {"n": n, "gloss_correct": gloss, "tone_correct": tone, "accuracy": both, "accuracy_basis": basis}


def score_native(labels):
    """Native accuracy per language: a quote counts only when its gloss and tone label are both right,
    by every speaker who checked it; a quote with no gloss counts on its tone label alone, and
    gloss_correct is the share of glossed quotes only (None when none had a gloss). accuracy_basis
    says which: "tone only" when no quote had a gloss, so a language that clears on tone alone reads
    as such.

    status per language: "unreviewed" for a NO_REVIEWER language, which stays tone capped whatever
    it scores (Albert's decision); "capped" under 80%, or under NATIVE_MIN quotes when even a
    perfect score on the rest up to NATIVE_QUOTES could not reach 80%; "insufficient" for any other
    language under NATIVE_MIN quotes, neither capped nor cleared; otherwise "cleared". capped lists
    every tone capped language: the labelled ones plus every NO_REVIEWER language, labelled or not.
    Each rotation group is also held to the 90% language accuracy target (TRUST.md section 7), with
    meets_target None under NATIVE_MIN.
    """
    items = {}
    for lab in labels:
        items.setdefault((lab["week"], lab["id"]), []).append(lab)
    by_lang = {}
    for lines in items.values():
        by_lang.setdefault(lines[0]["language"], []).append(lines)
    by_language = {}
    for lang, group in sorted(by_lang.items()):
        block = _native_block(group)
        if lang in NO_REVIEWER:
            block["status"] = "unreviewed"
        elif block["n"] < NATIVE_MIN:
            n, best = block["n"], max(block["n"], NATIVE_QUOTES)
            block["status"] = ("capped" if (round(block["accuracy"] * n) + best - n) / best < LANGUAGE_FLOOR
                               else "insufficient")
        else:
            block["status"] = "capped" if block["accuracy"] < LANGUAGE_FLOOR else "cleared"
        block["tone_capped"] = TONE_CAPPED if block["status"] in ("unreviewed", "capped") else None
        by_language[lang] = block
    by_group = {}
    for name, codes in ROTATION:
        group = [lines for code in codes for lines in by_lang.get(code, [])]
        if group:
            block = _native_block(group)
            block["meets_target"] = block["accuracy"] >= LANGUAGE_TARGET if block["n"] >= NATIVE_MIN else None
            by_group[name] = block
    return {"by_language": by_language, "by_group": by_group,
            "capped": [lang for lang, block in by_language.items() if block["status"] == "capped"]
            + list(NO_REVIEWER),
            "insufficient": [lang for lang, block in by_language.items() if block["status"] == "insufficient"],
            "unreviewed": list(NO_REVIEWER)}


def prepare_week(*, week, seed, load_claims, load_cards, save_sheet):
    """Draw the week's sample through the loaders and hand the sheet to save_sheet(week, text)."""
    rows = (claim_sample(load_claims(week), week=week, seed=seed)
            + card_sample(load_cards(week), week=week, seed=seed))
    text = review_sheet(rows)
    save_sheet(week, text)
    return text


def record_week(csv_text, *, load_prior, save_labels, grader=None):
    """Read a filled sheet, store its labels as feedback rows, and score the week with up to three prior weeks.

    save_labels receives to_feedback rows; load_prior(week) returns earlier weeks' feedback rows,
    of which only claim and card rows from this routine are read.
    """
    labels = read_sheet(csv_text)
    save_labels([to_feedback(lab) for lab in labels])
    week = labels[0]["week"] if labels else None
    prior = review_labels(load_prior(week), {"claim", "card"})
    return score_week(labels, prior=prior, grader=grader)


def record_native_week(csv_text, *, load_prior, save_labels):
    """Read a filled native sheet, store its labels as feedback rows, and score them with earlier native rows.

    save_labels receives to_feedback rows; load_prior(week) returns earlier weeks' feedback rows,
    of which only native rows from this routine are read.
    """
    labels = read_native_sheet(csv_text)
    save_labels([to_feedback(lab) for lab in labels])
    week = labels[0]["week"] if labels else None
    prior = review_labels(load_prior(week), {"native"})
    return score_native(prior + labels)
