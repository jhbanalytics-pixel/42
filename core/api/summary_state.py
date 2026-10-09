"""The typed summary state on the API side (C1 v2 sections 3, 5 and 6).

core/agent/answer_state.py (lane 1) builds and judges the value: execute stores it, and every reader sends the browser
its wire form, never the stored one. This module only calls it. Nothing here sets a state, and a value that fails a
check is reported as unverified or legacy, never repaired. Raw stored values cross every internal hop except the one
from f42-agent to f42-api, which carries the wire form its own code made (C1 5.2)."""
import logging

log = logging.getLogger("f42.api.summary_state")

NEUTRAL = "The one-line summary is not available for this answer."
OMITTED = "The one-line summary is left out because not every finding is kept."
WIRE_CHECKS = ("verified", "legacy_unknown", "unverified")
# A fault in the state code reads as a value that could not be read, a code C1 5.1 already lists.
UNAVAILABLE = {"check": "unverified", "problem": "shape"}


def module():
    """core.agent.answer_state, loaded on use: it is lane 1's file and standard library only."""
    from core.agent import answer_state
    return answer_state


def wire(record):
    """The wire value for a RAW stored record: meta_view. Never raises; a fault reads as unverified."""
    try:
        return module().meta_view(record)
    except Exception as exc:
        log.error("ask %s: answer_meta not read: %s", record.get("ask_id") if isinstance(record, dict) else None,
                  type(exc).__name__)
        return dict(UNAVAILABLE)


def is_wire(value):
    return isinstance(value, dict) and value.get("check") in WIRE_CHECKS


def with_wire(record, from_agent=False):
    """record with answer_meta replaced by its wire value, applied before any privacy projection (C1 condition 1).
    A raw stored record goes through meta_view, so a planted wire-shaped value reads as forbidden_key. A record that
    f42-agent sent has already been through it: its wire value is kept only if it still fits the record
    (check_wire) and is otherwise unverified, so an edit on the hop cannot raise a state."""
    if not isinstance(record, dict) or "answer" not in record and "status" not in record:
        return record
    meta = record.get("answer_meta")
    if from_agent and is_wire(meta):
        return {**record, "answer_meta": _checked_wire(record, meta)}
    return {**record, "answer_meta": wire(record)}


def _checked_wire(record, meta):
    if meta["check"] == "verified":
        try:
            problem = module().check_wire(record)
        except Exception as exc:
            log.error("answer_meta not checked: %s", type(exc).__name__)
            return dict(UNAVAILABLE)
        return meta if problem is None else {"check": "unverified", "problem": str(problem)}
    if meta["check"] == "unverified" and isinstance(meta.get("problem"), str) and set(meta) == {"check", "problem"}:
        return meta
    if meta["check"] == "legacy_unknown" and set(meta) == {"check"}:
        return meta
    return {"check": "unverified", "problem": "shape"}


def reader_meta(record):
    """The wire value an export or a dossier reads for a record that is either raw (from the store) or already in wire
    form (from the API's own boundary). A raw value is judged by meta_view; a wire value only if it fits the record."""
    meta = record.get("answer_meta")
    if is_wire(meta):
        return _checked_wire(record, meta)
    return wire(record)


def removal_sentence(stage, cause):
    sentences = module().READER_SENTENCES
    for key, text in sentences.items():
        if not key.startswith("removal:"):
            continue
        _, stages, wanted = key.split(":", 2)
        if wanted == cause and (stages == "any" or stage in stages.split("|")):
            return text
    return sentences["removal:any:unattributed"]


def summary_sentence(meta):
    """The sentence that stands for a one-line summary that is not shown, from a wire value. A value that is not
    verified, or states nothing about a missing summary, gets the neutral sentence."""
    if not isinstance(meta, dict) or meta.get("check") != "verified":
        return NEUTRAL
    sentences = module().READER_SENTENCES
    summary = meta["summary"]
    state = summary["state"]
    if state == "removed" and summary["removals"]:
        first = summary["removals"][0]
        text = removal_sentence(first["stage"], first["cause"])
        if summary["rewrite"] == "removed_after_check":
            text += " " + sentences["rewrite:removed_after_check"]
        return text
    if state in ("blank_unexplained", "no_answer"):
        return sentences[state]
    return NEUTRAL


def status_words(meta):
    """The words for how the run ended when the state is verified and says more than the answer status does; else
    None, and the caller keeps its own words."""
    if not isinstance(meta, dict) or meta.get("check") != "verified":
        return None
    execution = meta["execution"]
    return module().STATUS_WORDS.get((execution["state"], execution["stop_reason"]))


def stopped_early_line(record, meta):
    """Whether the 'Stopped before the end' line is shown: a stopped record, unless the verified state says the run
    completed and the stop came after the answer was final."""
    if record.get("status") != "stopped":
        return False
    return not (isinstance(meta, dict) and meta.get("check") == "verified"
                and meta["execution"]["state"] == "completed")
