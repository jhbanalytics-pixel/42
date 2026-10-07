import re
from datetime import date, datetime

from core.trust.claims import _norm


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_DATE = re.compile(r"(?<![\w/\\-])\d{4}-\d{2}-\d{2}(?![\w/\\:+-]|[ T]\d{2}:\d{2})")
_PROTECTED = re.compile(
    r'```[\s\S]*?```|`[^`\n]*`|https?://[^\s<>]+|www\.[^\s<>]+'
    r'|!?\[[^\]]*\]\([^)]*\)|\[[^\]\n]*\]|【[^】\n]*】|<[^>\n]*>'
    r'|"[^"\n]*"|“[^”\n]*”|‘[^’\n]*’|«[^»]*»|‹[^›]*›|(?<!\w)\x27[^\x27\n]*\x27(?!\w)',
    re.IGNORECASE,
)


def format_date_text(text, *, reference=None, quotes=()):
    if not isinstance(text, str):
        return text
    try:
        year = reference.year if isinstance(reference, (date, datetime)) else datetime.fromisoformat(str(reference)).year
    except (ValueError, TypeError):
        year = None
    normalized = _norm(text)
    protected = [match.span() for match in _PROTECTED.finditer(normalized)]
    for quote in quotes:
        quote = _norm(quote)
        if quote:
            protected.extend((match.start(), match.start() + len(quote))
                             for match in re.finditer("(?=" + re.escape(quote) + ")", normalized))

    def replace(match):
        prefix = re.search(r"\S*\Z", text[:match.start()]).group()
        suffix = re.match(r"\S*", text[match.end():]).group()
        if prefix.strip("([{") or suffix.strip(".,;:!?)]}"):
            return match.group()
        try:
            day = date.fromisoformat(match.group())
        except ValueError:
            return match.group()
        end = len(_norm(text[:match.end()]))
        start = end - len(match.group())
        if any(left < end and start < right for left, right in protected):
            return match.group()
        return f"{day.day} {_MONTHS[day.month - 1]}" + (f" {day.year}" if day.year != year else "")

    return _DATE.sub(replace, text)


def format_generated_dates(answer, *, reference=None):
    claims = answer.get("claims") or []
    quotes = [quote["text"] for claim in claims for quote in claim.get("quotes") or [] if quote.get("text")]

    def formatted(value):
        return format_date_text(value, reference=reference, quotes=quotes)

    shown = dict(answer)
    for key in ("short_answer", "context", "explanation", "title_written"):
        if key in shown:
            shown[key] = formatted(shown[key])
    for key in ("claims", "so_what", "watch_next"):
        if isinstance(shown.get(key), list):
            shown[key] = [{**entry, "text": formatted(entry["text"])} if "text" in entry else entry
                          for entry in shown[key]]
    if isinstance(shown.get("gaps"), list):
        shown["gaps"] = [{key: formatted(value) if key in ("what", "searched", "why") else value
                          for key, value in entry.items()} if isinstance(entry, dict) else entry
                         for entry in shown["gaps"]]
    return shown
