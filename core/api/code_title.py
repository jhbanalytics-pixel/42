"""The plain title a card shows when it has no written title (Albert, 12 Oct 2026, "code titles").

The brief writer's title shows only when it passed its checks (core/brief/title_purity.py); when it did not, the card
used to show its raw cluster label ("bafanabafana, mofokeng, relebohile"). A topic or hashtag card without a written
title now shows "Posts about Bafana Bafana, Mofokeng and Relebohile", built here from the label alone:

- the label's terms are split at commas and at dashes, a word written twice is spaced ("bafanabafana" is "Bafana
  Bafana"), and each term is said once: a term whose words all sit in a longer term, or that repeats a term already
  said in another order, is left out;
- a term that holds a mask, a handle, a link or a birth label is left out, and so is one the privacy projection
  masked because it names a hidden person (mask_hidden_terms); when no term is left the title is "Posts about this
  topic";
- a term already cased in the label keeps its casing; otherwise it takes the form the most posts of the card write
  it in (its explanation counts as one more, on an explained card), lower case included, the first seen on a tie;
  only the casing comes from a post, never a space, so no post splits a word;
- a hashtag keeps its # form; a word left in lower case starts each word with a capital.

It never adds a word the label does not hold, never says why, and changes nothing that is stored. It reads the card
as the reader gets it, after the privacy projection (core/api/today.py without_hidden). Creator, sound and other cards
keep their label. app/frontend/src/codeTitle.js is the same rule for the app; both are pinned by
core/api/tests/fixtures/code_titles.json."""
import re
import unicodedata

KINDS = ("topic", "hashtag")
FALLBACK = "this topic"
_UNNAMED = re.compile(r"[Tt][Oo][Pp][Ii][Cc] [0-9a-fA-F]{8}")
_DROPPED = ("@", "*", "[link]", "://", "/")
_SEPARATORS = re.compile(r",|\u2013|\u2014|\s-\s")
_DOUBLED_HALF = 5
# Python and JavaScript disagree on these six as whitespace, so both read them alike: the zero width no-break space
# goes, the rest are spaces.
_INVISIBLE = {0xFEFF: None, 0x85: " ", 0x1C: " ", 0x1D: " ", 0x1E: " ", 0x1F: " "}


def _clean(text):
    return unicodedata.normalize("NFC", text.translate(_INVISIBLE))


def _squash(text):
    return " ".join(text.split())


def _word_char(ch):
    return unicodedata.category(ch)[0] in "LMN"


def _key(text):
    return "".join(ch for ch in text if _word_char(ch)).upper()


def _lower(ch):
    return ch == ch.lower() and ch != ch.upper()


def _runs(text):
    """The runs of words in a text that only whitespace separates, each a list of words. Any other character ends a
    run, and a hashtag's body is not a word here."""
    runs, run, word, tag = [], [], "", False
    for ch in text + "\n":
        if _word_char(ch):
            word += ch
            continue
        if word and tag:
            runs.append(run)
            run = []
        elif word:
            run.append(word)
        word, tag = "", ch == "#"
        if not ch.isspace():
            runs.append(run)
            run = []
    runs.append(run)
    return [r for r in runs if r]


def _hashtags(text):
    """The hashtags in a text, # and body, the body being letters, marks, digits and underscores."""
    out, i = [], 0
    while i < len(text):
        if text[i] == "#":
            j = i + 1
            while j < len(text) and (_word_char(text[j]) or text[j] == "_"):
                j += 1
            if j > i + 1:
                out.append(text[i:j])
            i = j
        else:
            i += 1
    return out


def _doubled(term):
    if term.startswith("#") or " " in term or not term.isalpha() or len(term) % 2 or len(term) < 2 * _DOUBLED_HALF:
        return term
    half = len(term) // 2
    return term[:half] + " " + term[half:] if term[:half] == term[half:] else term


def _words(term):
    return [w.lstrip("#").upper() for w in term.split()]


def _dropped(term):
    return not _key(term) or _UNNAMED.fullmatch(term) or any(mark in term for mark in _DROPPED)


def _terms(label):
    """The label's terms to name, in order, each once; None when the label has none to name."""
    raw = [_squash(t) for t in _SEPARATORS.split(label)]
    raw = [t for t in raw if t]
    terms = [_doubled(t) for t in raw if not _dropped(t)]
    out = []
    for term in terms:
        mine = _words(term)
        if any(_key(term) == _key(o) or sorted(mine) == sorted(_words(o)) for o in out):
            continue
        if any(set(mine) <= set(_words(o)) and len(_words(o)) > len(mine) for o in terms):
            continue
        out.append(term)
    return out, len(raw) > len(terms)


def _texts(card):
    texts = [e["text"] for e in card.get("evidence") or [] if isinstance(e, dict) and isinstance(e.get("text"), str)]
    if card.get("explained") is True and isinstance(card.get("explanation"), str):
        texts.append(card["explanation"])
    return [_clean(t) for t in texts]


def _post_form(term, texts):
    """The form the most texts write the term in, exactly its letters and spaces in any casing, the first seen on a
    tie; None when no text holds it."""
    want, n = term.upper(), len(term.split())
    counts, order = {}, []
    for text in texts:
        if term.startswith("#"):
            found = [f for f in _hashtags(text) if f.upper() == want]
        else:
            found = [" ".join(run[i:i + n]) for run in _runs(text) for i in range(len(run) - n + 1)
                     if " ".join(run[i:i + n]).upper() == want]
        for form in dict.fromkeys(found):
            if form not in counts:
                order.append(form)
            counts[form] = counts.get(form, 0) + 1
    if not order:
        return None
    return min(order, key=lambda f: (-counts[f], order.index(f)))


def _capitalised(term):
    if term.startswith("#"):
        return term
    return " ".join(w[0].upper() + w[1:] if _lower(w[0]) else w for w in term.split())


def _cased(term, texts):
    if term != term.lower():
        return term
    form = _post_form(term, texts)
    if form is not None and (form != form.lower() or term.startswith("#")):
        return form
    return _capitalised(term)


def _subject(card):
    label = card.get("title")
    if card.get("kind") not in KINDS or not isinstance(label, str):
        return None
    label = _squash(_clean(label))
    if not label or _UNNAMED.fullmatch(label):
        return None
    terms, dropped = _terms(label)
    if not terms:
        return FALLBACK if dropped else None
    texts = _texts(card)
    names = [_cased(t, texts) for t in terms]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def code_title(card):
    """The plain title for a topic or hashtag card from its label, or None when the card keeps its label."""
    subject = _subject(card)
    return f"Posts about {subject}" if subject else None


def _title_key(text):
    return "".join(ch for ch in text.upper() if unicodedata.category(ch)[0] in "LN")


def written_title(card):
    """The writer's checked title, shown only on an explained card (core/brief/payload.py) and only when it says more
    than the label (app/frontend/src/ui/TrendCard.jsx writtenTitle), else None."""
    title, label = card.get("title_written"), card.get("title")
    if card.get("explained") is not True or not isinstance(title, str):
        return None
    title = _squash(_clean(title))
    if not title:
        return None
    return None if isinstance(label, str) and _title_key(title) == _title_key(label) else title


def shown_code_title(card):
    """The code title a card shows: only on a card from a brief that writes titles (it carries title_written, as
    every brief since 6 October 2026 does) and that shows none, so the title check dropped it or never ran."""
    return code_title(card) if "title_written" in card and not written_title(card) else None


def shown_title(card):
    """The title a reader sees: the written title, else the code title, else the label."""
    return written_title(card) or shown_code_title(card) or card.get("title")


def code_count_line(card, line):
    """A merged card's count line ends "(<label> only)"; under a code title it names the card the same way."""
    subject = _subject(card) if shown_code_title(card) else None
    label = card.get("title")
    end = f" ({label} only)" if isinstance(label, str) else None
    if subject and isinstance(line, str) and end and line.endswith(end):
        return line[:-len(end)] + f" ({subject} only)"
    return line


def mask_hidden_terms(label, people):
    """The label with each term that names a hidden person replaced by the mask, or the label unchanged.

    people holds the hidden people's handles and display names. A term names one when its letters are a name's or a
    handle's letters (so "thabomokoena" and "Thabo Mokoena" both name "Thabo Mokoena"), or when its words hold a name
    of two or more words in order. A one word name inside a longer term is left alone, as the projection leaves a
    name such as Music that is also a word."""
    if not isinstance(label, str) or not people:
        return label
    keys = {_key(_clean(p)) for p in people} - {""}
    multi = [_words(_squash(_clean(p).lstrip("@"))) for p in people]
    multi = [w for w in multi if len(w) > 1]
    parts = _SEPARATORS.split(label)
    out, changed = [], False
    for part in parts:
        term = _squash(_clean(part))
        words = _words(term)
        hit = term and (_key(term) in keys or any(
            words[i:i + len(m)] == m for m in multi for i in range(len(words) - len(m) + 1)))
        out.append("***" if hit else term)
        changed = changed or bool(hit)
    return ", ".join(t for t in out if t) if changed else label
