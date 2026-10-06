"""Literal quotation spans over stored source text for the general question lane."""

import hashlib


def quote_span(text: str, quote: str, occurrence: int = 0) -> dict:
    """Locate one literal occurrence of ``quote`` inside ``text``.

    The returned mapping holds exactly ``start``, ``end`` and ``quote_sha256``.
    Offsets count Python Unicode code points, so ``text[start:end] == quote``
    holds for the stored text as it was given. The digest is SHA256 over the
    UTF-8 bytes of the quote. Matching is exact: no normalisation, no case
    folding and no whitespace tolerance. Occurrences are literal and non
    overlapping, found with ``str.find`` resuming after each prior match, so
    ``occurrence`` selects the nth such match counting from zero.

    Raises ``ValueError`` when either text argument is not a string, when the
    quote is empty or absent, or when ``occurrence`` is not a non negative
    integer within the available exact matches.
    """
    if not isinstance(text, str) or not isinstance(quote, str):
        raise ValueError("quote_span needs a string text and a string quote")
    if isinstance(occurrence, bool) or not isinstance(occurrence, int):
        raise ValueError("quote_span occurrence must be an integer")
    if occurrence < 0:
        raise ValueError("quote_span occurrence must not be negative")
    if not quote:
        raise ValueError("quote_span quote must not be empty")

    start = -1
    resume_at = 0
    for _ in range(occurrence + 1):
        start = text.find(quote, resume_at)
        if start < 0:
            raise ValueError(f"quote occurrence {occurrence} is not present in the text")
        resume_at = start + len(quote)

    return {
        "start": start,
        "end": start + len(quote),
        "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
    }
