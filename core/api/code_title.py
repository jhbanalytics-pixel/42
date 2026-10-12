"""The plain title a card shows when it has no written title (Albert, 12 Oct 2026, "code titles").

The brief writer's title shows only when it passed its checks (core/brief/title_purity.py); when it did not, the card
used to show its raw cluster label ("bafanabafana, mofokeng, relebohile"). A topic or hashtag card without a written
title now shows "Posts about Bafana Bafana, Mofokeng and Relebohile", built here from the label alone:

- the label's terms, in order, each said once (a term whose words all sit in a longer term is left out);
- a hashtag keeps its # form; a term already cased in the label keeps its casing;
- otherwise a hashtag takes the casing most of the card's posts give it, lower case included, and a word takes the
  casing the card's own posts give it (its explanation too, on an explained card): the most used cased form of
  exactly its letters, a spaced form ("Mamelodi Sundowns") before a joined one;
- else a doubled word is spaced ("bafanabafana" is "Bafana Bafana") and each word starts with a capital, while a
  hashtag stays as the label writes it.

It never adds a word the label does not hold, never says why, and changes nothing that is stored. It reads the card
as the reader gets it, after the privacy projection (core/api/today.py without_hidden), so a masked handle or name
leaves the label as it is. Creator, sound and other cards keep their label. app/frontend/src/codeTitle.js is the
same rule for the app; both are pinned by core/api/tests/fixtures/code_titles.json."""
import re

KINDS = ("topic", "hashtag")
_UNNAMED = re.compile(r"Topic [0-9a-f]{8}", re.IGNORECASE)
_MASKED = ("@", "*", "[link]", "://")
_ALNUM = re.compile(r"[^\W_]+")
_RUN = re.compile(r"[^\W_]+(?:\s+[^\W_]+)*")
_HASHTAG = re.compile(r"#(\w+)")
_WINDOW = 6


def _key(text):
    return "".join(_ALNUM.findall(text)).lower()


def _words(term):
    return {w.lstrip("#").lower() for w in term.split()}


def _terms(label):
    terms = [" ".join(t.split()) for t in label.split(",")]
    terms = [t for t in terms if t]
    if not terms or any(not _key(t) for t in terms):
        return None
    out = []
    for i, term in enumerate(terms):
        if any(_key(other) == _key(term) for other in terms[:i]):
            continue
        if any(_words(term) < _words(other) for other in terms):
            continue
        out.append(term)
    return out


def _texts(card):
    texts = [e["text"] for e in card.get("evidence") or [] if isinstance(e, dict) and isinstance(e.get("text"), str)]
    if card.get("explained") is True and isinstance(card.get("explanation"), str):
        texts.append(card["explanation"])
    return texts


def _post_form(term, texts):
    """A hashtag's most used form in the texts, lower case included, so one post's casing never outvotes the rest;
    a word's most used cased form of its letters, spaced forms first. None when the texts give none."""
    counts, order = {}, []
    if term.startswith("#"):
        body = term[1:].lower()
        forms = ("#" + m for t in texts for m in _HASHTAG.findall(t) if m.lower() == body)
    else:
        key = _key(term)
        forms = (" ".join(tokens[i:j]) for t in texts for run in _RUN.findall(t) for tokens in [run.split()]
                 for i in range(len(tokens)) for j in range(i + 1, min(i + _WINDOW, len(tokens)) + 1)
                 if "".join(tokens[i:j]).lower() == key)
    for form in forms:
        if term.startswith("#") or form != form.lower():
            if form not in counts:
                order.append(form)
            counts[form] = counts.get(form, 0) + 1
    if not order:
        return None
    return min(order, key=lambda f: (-len(f.split()), -counts[f], order.index(f)))


def _plain(term):
    if not term.startswith("#") and " " not in term and term.isalpha() and len(term) >= 6:
        half = len(term) // 2
        if len(term) % 2 == 0 and term[:half] == term[half:]:
            term = term[:half] + " " + term[half:]
    if term.startswith("#"):
        return term
    return " ".join(w[0].upper() + w[1:] if w[0].islower() else w for w in term.split())


def _cased(term, texts):
    if term != term.lower():
        return term
    return _post_form(term, texts) or _plain(term)


def _subject(card):
    label = card.get("title")
    if card.get("kind") not in KINDS or not isinstance(label, str):
        return None
    label = " ".join(label.split())
    if not label or _UNNAMED.fullmatch(label) or any(mark in label for mark in _MASKED):
        return None
    terms = _terms(label)
    if not terms:
        return None
    texts = _texts(card)
    names = [_cased(t, texts) for t in terms]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def code_title(card):
    """The plain title for a topic or hashtag card from its label, or None when the card keeps its label."""
    subject = _subject(card)
    return f"Posts about {subject}" if subject else None


def written_title(card):
    """The writer's checked title, shown only on an explained card (core/brief/payload.py) and only when it says more
    than the label (app/frontend/src/ui/TrendCard.jsx writtenTitle), else None."""
    title, label = card.get("title_written"), card.get("title")
    if card.get("explained") is not True or not isinstance(title, str) or not title.strip():
        return None
    return None if isinstance(label, str) and _key(title) == _key(label) else " ".join(title.split())


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
