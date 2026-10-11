"""What a question asks for (standard library only, so the api service and answer_view can import it).

Two shapes of question change how an answer is written and read: a question that asks why, whose answer leads with
the cause, and a question that asks which stories lead, whose answer is a ranked briefing. The writer's rule for each
and the reader's briefing both ask this module, so the question that was written for and the question that is read for
are one test."""

from __future__ import annotations

import re

# A question that asks for a cause: why, how come, what is behind or driving or made or caused it. A forecast
# ("which do you expect to keep growing, and why") asks for a reason for a prediction and no cause the posts can show, so
# it is left to the usual rules.
_WHY = re.compile(r"\bwhy\b|\bhow come\b|\bwhat\s+lies\s+behind\b"
                  r"|\bwhat(?:'s|\s+is|\s+was|\s+are|\s+were)?\s+(?:really\s+|actually\s+)?"
                  r"(?:driving|behind|causing|fuelling|fueling|powering|explaining)\b"
                  r"|\bwhat\s+(?:made|makes|caused|causes|drove|drives|explains)\b", re.IGNORECASE)
_FORECAST = re.compile(r"\b(?:expect|predict|forecast|going\s+to|will)\b", re.IGNORECASE)


def _plain(question) -> str:
    return " ".join(str(question or "").replace("’", "'").split())


def asks_why(question) -> bool:
    """Whether a question asks for a cause, so its answer leads with one. A forecast question is not one."""
    text = _plain(question)
    return bool(_WHY.search(text)) and not _FORECAST.search(text)


# A question that asks which stories lead: a ranking word and a word for what people say or post, about the market as a
# whole. It names no one subject (a brand, a person, an event, a hashtag), asks nothing that has a yes or no answer, and
# is no question for a list of sounds, hashtags or creators, which have their own. A cause is the cause-first rule's and
# a forecast is neither.
_RANK_CUE = re.compile(r"\b(?:most(?!\s+likely)|top|biggest|largest|leading|hottest|loudest|busiest|main"
                       r"|dominat(?:e|es|ed|ing)|what(?:'s|\s+is|\s+are)\s+(?:currently\s+|really\s+|now\s+)?trending)\b",
                       re.IGNORECASE)
_STORY_CUE = re.compile(r"\b(?:talk(?:ing|ed)?|post(?:ing|ed)?|saying|discuss(?:ing|ed)?|conversations?|stor(?:y|ies)"
                        r"|topics?|trend(?:s|ing)?|news|happening|buzz|viral|headlines?|themes?|issues?|moments?)\b",
                        re.IGNORECASE)
_OWN_LIST = re.compile(r"\b(?:creators?|influencers?|accounts?|handles?|sounds?|audio|songs?|hashtags?)\b|[#@]\w", re.IGNORECASE)
_BRAND_RANKED = re.compile(r"\b(?:most|top|biggest|largest|leading|hottest)\b\W+(?:\w+\W+){0,2}brands?\b"
                           r"|\bbrands?\b\W+(?:\w+\W+){0,3}\b(?:most|top|biggest|leading)\b", re.IGNORECASE)
_YES_NO = re.compile(r"^(?:is|are|was|were|has|have|had|does|do|did|can|could|should|will|would"
                     r"|how\s+(?:should|can|could|do|does|would))\b", re.IGNORECASE)
# "about" ends the question ("talking about most", "about this week") or names its subject ("about Checkers").
_ABOUT_SUBJECT = re.compile(r"\babout\s+(?!(?:the\s+)?most\b|this\b|today\b|right\b|now\b|lately\b|currently\b|in\b"
                            r"|across\b|on\b|the\s+(?:week|month|year|market)\b)\w", re.IGNORECASE)
# A capital that is not the start of a sentence names something other than a market or a platform.
_KNOWN_CAPITALS = frozenset({
    "South", "Africa", "African", "Africans", "SA", "ZA", "Nigeria", "Nigerian", "Nigerians", "Kenya", "Kenyan",
    "Kenyans", "TikTok", "Instagram", "YouTube", "X", "Twitter", "Facebook", "Reddit", "Threads", "Bluesky",
    "LinkedIn", "Telegram", "I", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November",
    "December"})


def _names_a_subject(text: str) -> bool:
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        for word in re.findall(r"[A-Za-z][\w']*", sentence)[1:]:
            if word[0].isupper() and word not in _KNOWN_CAPITALS:
                return True
    return False


def asks_ranked(question) -> bool:
    """Whether a question asks which stories lead, so its answer is a ranked briefing."""
    text = _plain(question)
    return (bool(text) and not _YES_NO.search(text) and bool(_RANK_CUE.search(text)) and bool(_STORY_CUE.search(text))
            and not _OWN_LIST.search(text) and not _BRAND_RANKED.search(text) and not _ABOUT_SUBJECT.search(text)
            and not _names_a_subject(text) and not asks_why(text) and not _FORECAST.search(text))
