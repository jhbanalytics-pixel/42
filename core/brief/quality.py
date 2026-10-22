"""Shared admission of the existing retained critic contract.

A valid critic answer passes the cultural reading only when it rules the simpler explanation out. Its why-now is then
one a cited local post states (local_why_now true), or, when no post states the local cause (Albert, 22 Oct 2026), it
is inferred from adoption: code, never the model, counts a format, sound or practice adopted by ADOPTION_MIN_CREATORS
or more independent local creators on ADOPTION_MIN_DAYS or more days among the posts the writer may cite
(core/brief/explain.py adopted_pattern). A simpler explanation that is not ruled out holds the reading either way, so a
news or event spike stays held.
"""

_BOOL_FIELDS = ("ruled_out", "news_driven", "scheduled_event", "local_reaction", "local_why_now")
# The owner's card rule: a distinct format, sound or practice adopted by 3 or more independent local creators on 2 or
# more days (Albert, 22 Oct 2026).
ADOPTION_MIN_CREATORS = 3
ADOPTION_MIN_DAYS = 2


def critic_is_valid(value):
    if not isinstance(value, dict):
        return False
    if any(type(value.get(field)) is not bool for field in _BOOL_FIELDS):
        return False
    if any(not isinstance(value.get(field), str) or not value[field].strip()
           for field in ("reason", "non_cultural_explanation")):
        return False
    news, event, reaction = (value[field] for field in ("news_driven", "scheduled_event", "local_reaction"))
    if value["ruled_out"] and (news or event or reaction):
        return False
    return not (news and event) and (not reaction or news or event)


def adoption_counted(record):
    """True when record, a code count from core/brief/explain.py adopted_pattern ({pattern, post_ids, creators,
    days}), shows the adoption floor: ADOPTION_MIN_CREATORS or more independent local creators on ADOPTION_MIN_DAYS or
    more days, over at least that many posts. Anything else, None included, is False."""
    if not isinstance(record, dict):
        return False
    creators, days, ids = record.get("creators"), record.get("days"), record.get("post_ids")
    return (type(creators) is int and creators >= ADOPTION_MIN_CREATORS
            and type(days) is int and days >= ADOPTION_MIN_DAYS
            and isinstance(ids, list) and len(set(map(str, ids))) >= ADOPTION_MIN_CREATORS)


def cultural_critic_passes(value, adoption=None):
    """True when the critic's valid answer rules the simpler explanation out and the why-now is shown: stated by a
    cited local post (local_why_now true), or inferred from adoption, the code's own count (adoption_counted). The
    model's answer is never read for adoption."""
    if not critic_is_valid(value) or value["ruled_out"] is not True:
        return False
    return value["local_why_now"] is True or adoption_counted(adoption)


def why_now_inferred(value, adoption=None):
    """True when the reading passes only on the code-counted adoption, with no local why-now a post states."""
    return cultural_critic_passes(value, adoption) and value["local_why_now"] is not True
