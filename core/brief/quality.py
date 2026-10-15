"""Shared admission of the existing retained critic contract."""

_BOOL_FIELDS = ("ruled_out", "news_driven", "scheduled_event", "local_reaction", "local_why_now")


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


def cultural_critic_passes(value):
    return critic_is_valid(value) and value["ruled_out"] is True and value["local_why_now"] is True
