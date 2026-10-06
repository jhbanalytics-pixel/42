"""Conservative source-and-content evidence for a known foreign-local collision."""

import re
from urllib.parse import urlsplit

CONTEXT_GEO_POLICY = "corroborated_foreign_local_v1"
_TARGET_REFERENCES = {
    "za": r"south afric(?:a|an)|mzansi|cape town|johannesburg|durban|soweto|pretoria",
    "ng": r"nigeria(?:n)?|lagos|abuja|naija",
    "ke": r"kenya(?:n)?|nairobi|mombasa",
}


def _explicit_comparison(text):
    comparison = re.search(
        r"\bcompar(?:e|ing|ison|ed)\b|\bversus\b|\bvs\b|\brelative to\b|\b(?:better|different) than\b",
        text,
        re.IGNORECASE,
    )
    foreign_subject = re.search(
        r"\bunited states\b|\bamerican\b|\bvirginia\b|\bblacksburg\b", text, re.IGNORECASE
    ) or re.search(r"\bUS\b|\bU\.S\.", text)
    return bool(comparison and foreign_subject)


def foreign_local_exclusion(payload, request, plan):
    if payload.get("platform") != "reddit":
        return False
    try:
        url = urlsplit(payload.get("url") or "")
    except ValueError:
        return False
    if (
        url.scheme not in {"http", "https"}
        or url.hostname not in {"reddit.com", "www.reddit.com"}
        or not url.path.lower().startswith(("/r/virginiatech/", "/r/blacksburg/"))
    ):
        return False
    text = " ".join(str(payload.get(key) or "") for key in ("title", "text"))
    if not all(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in (
            r"\bvirginia tech\b",
            r"\bblacksburg\b",
            r"\blocal (?:fun|events?)\b|\bthis weekend\b|\blabor day\b",
        )
    ):
        return False
    if any(
        re.search(r"\b(?:" + _TARGET_REFERENCES[market] + r")\b", text, re.IGNORECASE)
        for market in plan["markets"]
        if market in _TARGET_REFERENCES
    ):
        return False
    question = request["question"]
    if _explicit_comparison(question):
        return False
    local_reset = (
        re.search(r"\b(?:only|exclusively|just)\b", question, re.IGNORECASE)
        and any(
            re.search(r"\b(?:" + _TARGET_REFERENCES[market] + r")\b", question, re.IGNORECASE)
            for market in plan["markets"]
            if market in _TARGET_REFERENCES
        )
        and not (
            re.search(
                r"\bunited states\b|\bamerican\b|\bvirginia\b|\bblacksburg\b",
                question,
                re.IGNORECASE,
            )
            or re.search(r"\bUS\b|\bU\.S\.", question)
        )
    )
    prior_user = next(
        (
            turn.get("text", "")
            for turn in reversed(request.get("history", []))
            if turn.get("role") == "user"
        ),
        "",
    )
    inherited_comparison = (
        not local_reset
        and plan.get("intent") == "comparison"
        and re.search(r"\b(?:first|second) example\b|\bthat comparison\b", question, re.IGNORECASE)
        and _explicit_comparison(prior_user)
        and any(
            _explicit_comparison(str(item.get("question") or ""))
            for item in plan.get("requirements", [])
        )
    )
    return not bool(inherited_comparison)
