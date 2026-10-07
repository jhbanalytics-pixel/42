"""Plain words for core/trust/gate.py's held reasons, shared by Today, Discover, Radar, topic pages, Compare and
alerts, so a held item reads the same wherever it is shown."""
import re

from core.brief.payload import NOT_RUN_REASONS

# core/trust/gate.py's held reasons, matched whole, each with its plain words. Any other reason stays as written.
# G3 and G4b also match the wording gate.py used before, so briefs stored with it still read plainly.
# core/api/tests/test_held_words.py checks that every held reason gate_card writes has plain words here.
_SHARE = r"(0(?:\.\d+)?|1(?:\.0+)?)"
GATE_WORDS = (
    (re.compile(r"Data issue: \d+ of the last 3 market-days invalid on the main platform"),
     lambda m: "Not enough clean data on the main platform"),
    (re.compile(r"Found by search(?: only: not yet in the feeds 42 measures every day|: seen only in .+)"),
     lambda m: "Only found through our own searches so far"),
    (re.compile(r"Market unconfirmed: source market evidence is (?:missing or invalid|inconsistent)"),
     lambda m: "We could not confirm which market this comes from"),
    (re.compile(r"Global: (\d+) of (\d+) card source posts in the last 7 days were located in this market or came "
                r"from its feeds"),
     lambda m: f"Mostly posted outside this market ({m[1]} of {m[2]} posts local)"),
    (re.compile(rf"Paid-led: sponsored or brand-owned share {_SHARE}"),
     lambda m: f"Mostly sponsored or brand posts ({round(float(m[1]) * 100)}%)"),
    (re.compile(r"Paid-led: (#\S+) is on the campaign hashtag list"), lambda m: f"{m[1]} is a known campaign hashtag"),
    (re.compile(r"Not assessed: political (?:topic that no neutral source has confirmed yet"
                r"|item not Corroborated in an unbiased lane)"),
     lambda m: "Political: waiting for independent confirmation"),
)


# An explanation held back (G10), as the brief job (core/brief/job.py) and Today's own card check
# (core/api/today.py REJECT_EXPLAINED) word it. Tester report, 5 October 2026: "Explanation failed its checks" read as
# a broken market, so it reads as what happened to the explanation. A topic a busy model left unexplained carries the
# job's fixed busy wording (core/brief/payload.py NOT_RUN_REASONS) in failed_reason: no check ran on it, so it says so.
EXPLANATION_HELD = frozenset({"Explanation failed its checks", "The explanation did not pass its checks"})
EXPLANATION_WORDS = "The explanation did not pass our checks"
BUSY_WORDS = "Not explained in time: the model was busy"


def plain_reason(out):
    """Swap a gate reason for its plain words, keeping the gate's own text in reason_raw."""
    raw = out.get("reason_text")
    if not isinstance(raw, str):
        return out
    if raw.strip() in EXPLANATION_HELD:
        busy = isinstance(out.get("failed_reason"), str) and out["failed_reason"].strip() in NOT_RUN_REASONS
        out["reason_text"], out["reason_raw"] = BUSY_WORDS if busy else EXPLANATION_WORDS, raw
        return out
    for rx, words in GATE_WORDS:
        m = rx.fullmatch(raw.strip())
        if m:
            out["reason_text"], out["reason_raw"] = words(m), raw
            break
    return out
