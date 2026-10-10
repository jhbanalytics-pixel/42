"""The typed summary state of an Ask (contract C1, 3.4a).

Two states per Ask record, set by code alone and stored under `answer_meta` beside `answer` and `run`: how the run
ended (execution) and what became of the one-line summary (summary), with the ordered list of removals and the outcome
of the one rewrite. The answer contract is closed, so the state cannot live inside the answer.

This module is standard library only and does no I/O at import: the API, the export, the dossier build and the release
smoke all load it, and a job image imports it through core.agent.ask.

build / build_failed   the producers, called by ask.run_ask and agent_app.execute
verify_stored          judges a stored value against the record it sits in
meta_view              the wire value f42-api returns for a raw record
check_wire             judges a wire value, for readers that only have the API response (the smoke)

Nothing used to decide here is read from the value being judged except the record's own status, ids and facts that
can be recomputed from its answer, which are what the metadata is compared with. The digest is recomputed, never
checked for shape alone, and the pairing rules, enums and stage to cause table are constants in this file.
"""

from __future__ import annotations

import hashlib
import json

VERSION = 1
EXECUTION_STATES = ("completed", "stopped_on_request", "stopped_on_budget", "refused_budget_spent", "failed")
STOP_REASONS = ("budget_full", "model_call_unverified", "price_unreadable")
SUMMARY_STATES = ("shown", "shown_rewritten", "fixed_text", "removed", "blank_unexplained", "no_answer")
REMOVAL_STAGES = ("first_check", "support_check", "recheck", "field_check", "critic")
REMOVAL_CAUSES = ("K2", "K3", "K6", "K8", "K9", "claim_cut", "claim_narrowed", "field_unchecked", "unattributed")
REWRITE_OUTCOMES = ("not_attempted", "kept", "removed_after_check", "used_up", "no_budget", "call_failed", "too_long",
                    "empty", "repeated_removed_claim")
STAGE_CAUSES = {
    "first_check": ("K2", "K3", "K6", "K8", "K9"),
    "recheck": ("K2", "K3", "K6", "K8", "K9"),
    "support_check": ("claim_cut", "claim_narrowed"),
    "field_check": ("K3", "K6", "K9", "field_unchecked"),
    "critic": ("claim_cut",),
}
SUPPORT_REWRITE_OUTCOMES = ("used_up", "no_budget", "call_failed", "too_long", "empty", "repeated_removed_claim")
LATER_STAGES = ("recheck", "field_check", "critic")
ANSWER_STATUSES = ("complete", "partial", "insufficient_evidence")

# What a reader is told (C1 2.6). Reader text never prints a check code. The wording is proposed copy.
READER_SENTENCES = {
    "removal:first_check|recheck:K2": "The one-line summary was removed because it used a figure that no checked finding holds.",
    "removal:first_check|recheck|field_check:K3": (
        "The one-line summary was removed because it named a place no cited post is located in, or relied on a post "
        "outside the question's window or market."),
    "removal:first_check|recheck:K6": (
        "The one-line summary was removed because it used a term or source the trust rules do not allow."),
    "removal:first_check|recheck:K8": (
        "The one-line summary was removed because it quoted words that are not in the posts it cites."),
    "removal:first_check|recheck|field_check:K9": (
        "The one-line summary was removed because it made a forecast, and forecasts stay held until they beat a "
        "simple no-change forecast."),
    "removal:support_check|critic:claim_cut": (
        "The one-line summary was removed after a claim it may have rested on did not pass its checks."),
    "removal:support_check:claim_narrowed": (
        "The one-line summary was removed after a claim it may have rested on was narrowed."),
    "removal:field_check:K6": (
        "The one-line summary was removed because the text check found it describes people in a way the trust rules "
        "do not allow."),
    "removal:field_check:field_unchecked": (
        "The one-line summary was too long to check with the posts it rests on, so it was left out."),
    "removal:any:unattributed": "The one-line summary was removed by the checks.",
    "rewrite:removed_after_check": "A rewritten summary did not pass the checks either.",
    "shown_rewritten": "This summary was rewritten once from the findings that passed.",
    "blank_unexplained": "No one-line summary was written for this answer.",
    "no_answer": "There is no answer: the run did not finish.",
    "legacy_or_unverified": "The one-line summary is not available for this answer.",
}
STATUS_WORDS = {
    ("stopped_on_budget", "budget_full"): "Stopped at this question's model budget, not for lack of evidence",
    ("stopped_on_budget", "model_call_unverified"): (
        "Stopped because a model call failed or did not report what it cost, not for lack of evidence"),
    ("stopped_on_budget", "price_unreadable"): (
        "Stopped because the cost of a model call could not be worked out, not for lack of evidence"),
    ("stopped_on_request", None): "Stopped before an answer was written",
    ("refused_budget_spent", None): "Not researched: the model budget for today is spent",
}

STORED_KEYS = {"v", "ask_id", "check_run_id", "execution", "summary", "bound", "digest"}
WIRE_KEYS = {"check", "v", "execution", "summary"}


def _blank(text) -> bool:
    return not str(text or "").strip()


def bound_facts(answer) -> dict:
    """What every reader can recompute from the answer in the same record. No text, so masking cannot change it."""
    if answer is None:
        return {"answer_status": None, "summary_blank": None, "claims": None}
    return {"answer_status": answer.get("status"), "summary_blank": _blank(answer.get("short_answer")),
            "claims": len(answer.get("claims") or [])}


def digest_of(meta: dict) -> str:
    body = {k: v for k, v in meta.items() if k != "digest"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_removals(removals) -> list:
    """The removals as stored, or ValueError: each pair legal for its stage, none repeated, causes in enum order."""
    out, seen = [], set()
    for item in removals:
        stage, cause = (item["stage"], item["cause"]) if isinstance(item, dict) else item
        if stage not in REMOVAL_STAGES:
            raise ValueError(f"unknown removal stage {stage!r}")
        if cause not in REMOVAL_CAUSES or (cause != "unattributed" and cause not in STAGE_CAUSES[stage]):
            raise ValueError(f"cause {cause!r} is not legal for stage {stage!r}")
        if (stage, cause) in seen:
            raise ValueError(f"duplicate removal {stage}/{cause}")
        seen.add((stage, cause))
        out.append({"stage": stage, "cause": cause})
    for stage in REMOVAL_STAGES:
        causes = [r["cause"] for r in out if r["stage"] == stage]
        if causes != sorted(causes, key=REMOVAL_CAUSES.index):
            raise ValueError(f"causes of {stage} are out of order")
    return out


def build(*, ask_id: str, check_run_id, execution_state: str, stop_reason, summary_state: str, removals,
          rewrite: str, answer) -> dict:
    """The stored value, built from closed-enum constants and the answer that will be stored. ValueError when the
    arguments are outside the closed sets or break a rule of the removal list; pairing with the record status is
    judged by verify_stored."""
    if not isinstance(ask_id, str) or not ask_id:
        raise ValueError("ask_id must be a non-empty string")
    if check_run_id is not None and not isinstance(check_run_id, str):
        raise ValueError("check_run_id must be a string or None")
    if execution_state not in EXECUTION_STATES:
        raise ValueError(f"unknown execution state {execution_state!r}")
    if summary_state not in SUMMARY_STATES:
        raise ValueError(f"unknown summary state {summary_state!r}")
    if rewrite not in REWRITE_OUTCOMES:
        raise ValueError(f"unknown rewrite outcome {rewrite!r}")
    if (stop_reason is not None) != (execution_state == "stopped_on_budget"):
        raise ValueError("stop_reason is set exactly when the state is stopped_on_budget")
    if stop_reason is not None and stop_reason not in STOP_REASONS:
        raise ValueError(f"unknown stop reason {stop_reason!r}")
    meta = {"v": VERSION, "ask_id": ask_id, "check_run_id": check_run_id,
            "execution": {"state": execution_state, "stop_reason": stop_reason},
            "summary": {"state": summary_state, "removals": _check_removals(removals), "rewrite": rewrite},
            "bound": bound_facts(answer)}
    meta["digest"] = digest_of(meta)
    return meta


def build_failed(*, ask_id: str, check_run_id) -> dict:
    """The state of a run that raised: no answer, so every bound fact is null."""
    return build(ask_id=ask_id, check_run_id=check_run_id, execution_state="failed", stop_reason=None,
                 summary_state="no_answer", removals=[], rewrite="not_attempted", answer=None)


# The pairing and reason rules (C1 2.4), one named clause each so a test can disable one and see which negative case
# notices. Each is (problem code, predicate over the facts); pairing clauses run before reasons clauses.
def _facts(meta: dict, record: dict) -> dict:
    answer = record.get("answer")
    return {
        "state": meta["execution"]["state"], "stop": meta["execution"]["stop_reason"],
        "summary": meta["summary"]["state"],
        "removals": [(r["stage"], r["cause"]) for r in meta["summary"]["removals"]],
        "rewrite": meta["summary"]["rewrite"], "record_status": record.get("status"),
        "answer_null": answer is None, "answer_status": None if answer is None else answer.get("status"),
        "blank": None if answer is None else _blank(answer.get("short_answer")),
        "bound": meta.get("bound") if "bound" in meta else bound_facts(answer),
    }


def _fixed(f):
    return f["answer_status"] == "insufficient_evidence" and f["summary"] == "fixed_text"


def _live(f):
    return f["answer_status"] in ("complete", "partial")


def _stages(f):
    return {stage for stage, _ in f["removals"]}


def _in_order(f):
    for stage in REMOVAL_STAGES:
        causes = [c for s, c in f["removals"] if s == stage]
        if causes != sorted(causes, key=REMOVAL_CAUSES.index):
            return False
    return True


CLAUSES = {
    "P1": ("pairing", lambda f: f["state"] != "failed" or (
        f["record_status"] == "failed" and f["answer_null"] and f["summary"] == "no_answer"
        and all(v is None for v in f["bound"].values()))),
    "P2": ("pairing", lambda f: f["state"] == "failed" or (
        f["record_status"] in ("complete", "stopped") and not f["answer_null"] and f["summary"] != "no_answer")),
    "P4": ("pairing", lambda f: f["state"] != "stopped_on_request" or (
        f["record_status"] == "stopped" and _fixed(f))),
    "P5a": ("pairing", lambda f: f["state"] != "stopped_on_budget" or f["stop"] is not None),
    "P5b": ("pairing", lambda f: f["state"] == "stopped_on_budget" or f["stop"] is None),
    "P5c": ("pairing", lambda f: f["state"] != "stopped_on_budget" or _fixed(f)),
    "P6": ("pairing", lambda f: f["state"] != "refused_budget_spent" or _fixed(f)),
    "P7p": ("pairing", lambda f: f["summary"] != "shown" or (f["blank"] is False and _live(f))),
    "P8p": ("pairing", lambda f: f["summary"] != "shown_rewritten" or (f["blank"] is False and _live(f))),
    "P9p": ("pairing", lambda f: f["summary"] != "fixed_text" or (
        f["blank"] is False and f["answer_status"] == "insufficient_evidence")),
    "P10p": ("pairing", lambda f: f["summary"] != "removed" or (
        f["blank"] is True and f["answer_status"] == "partial" and f["state"] == "completed")),
    "P11p": ("pairing", lambda f: f["summary"] != "blank_unexplained" or (
        f["blank"] is True and _live(f) and f["state"] == "completed")),
    "P7r": ("reasons", lambda f: f["summary"] != "shown" or (f["rewrite"] == "not_attempted" and not f["removals"])),
    "P8r": ("reasons", lambda f: f["summary"] != "shown_rewritten" or (f["rewrite"] == "kept" and f["removals"])),
    "P9r": ("reasons", lambda f: f["summary"] != "fixed_text" or (
        not f["removals"] and f["rewrite"] in ("not_attempted", "removed_after_check"))),
    "P10r": ("reasons", lambda f: f["summary"] != "removed" or bool(f["removals"])),
    "P11r": ("reasons", lambda f: f["summary"] != "blank_unexplained" or (
        not f["removals"] and f["rewrite"] == "not_attempted")),
    "P12r": ("reasons", lambda f: f["summary"] != "no_answer" or (
        not f["removals"] and f["rewrite"] == "not_attempted")),
    "R1_legal": ("reasons", lambda f: all(
        c == "unattributed" or c in STAGE_CAUSES.get(s, ()) for s, c in f["removals"])),
    "R1_unique": ("reasons", lambda f: len(set(f["removals"])) == len(f["removals"])),
    "R1_order": ("reasons", _in_order),
    "R2_kept": ("reasons", lambda f: f["rewrite"] != "kept" or f["summary"] == "shown_rewritten"),
    "R2_removed_after_check": ("reasons", lambda f: f["rewrite"] != "removed_after_check" or f["summary"] == "fixed_text"
                               or (f["summary"] == "removed" and bool(_stages(f) & set(LATER_STAGES)))),
    "R2_support_outcomes": ("reasons", lambda f: f["rewrite"] not in SUPPORT_REWRITE_OUTCOMES or (
        f["summary"] == "removed" and "support_check" in _stages(f))),
}


def _clause_problem(facts: dict):
    for code, predicate in CLAUSES.values():
        if not predicate(facts):
            return code
    return None


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _shape_ok(meta) -> bool:
    if not isinstance(meta, dict) or set(meta) != STORED_KEYS:
        return False
    execution, summary, bound = meta["execution"], meta["summary"], meta["bound"]
    if not (_is_int(meta["v"]) and isinstance(meta["ask_id"], str) and isinstance(meta["digest"], str)
            and (meta["check_run_id"] is None or isinstance(meta["check_run_id"], str))):
        return False
    if not (isinstance(execution, dict) and set(execution) == {"state", "stop_reason"}
            and isinstance(execution["state"], str)
            and (execution["stop_reason"] is None or isinstance(execution["stop_reason"], str))):
        return False
    if not (isinstance(summary, dict) and set(summary) == {"state", "removals", "rewrite"}
            and isinstance(summary["state"], str) and isinstance(summary["rewrite"], str)
            and isinstance(summary["removals"], list)):
        return False
    if not all(isinstance(r, dict) and set(r) == {"stage", "cause"} and isinstance(r["stage"], str)
               and isinstance(r["cause"], str) for r in summary["removals"]):
        return False
    return (isinstance(bound, dict) and set(bound) == {"answer_status", "summary_blank", "claims"}
            and (bound["answer_status"] is None or isinstance(bound["answer_status"], str))
            and (bound["summary_blank"] is None or isinstance(bound["summary_blank"], bool))
            and (bound["claims"] is None or _is_int(bound["claims"])))


def _enums_ok(execution: dict, summary: dict) -> bool:
    return (execution["state"] in EXECUTION_STATES
            and (execution["stop_reason"] is None or execution["stop_reason"] in STOP_REASONS)
            and summary["state"] in SUMMARY_STATES and summary["rewrite"] in REWRITE_OUTCOMES
            and all(r["stage"] in REMOVAL_STAGES and r["cause"] in REMOVAL_CAUSES for r in summary["removals"]))


def verify_stored(record: dict):
    """The problem code for a stored value, or None when every step passes (C1 5.1, steps 1 and 3 to 11). A running
    record may carry none; a terminal record that carries none has nothing to verify and reports `missing`."""
    meta = record.get("answer_meta")
    if record.get("status") == "running":
        return None if meta is None else "while_running"
    if meta is None:
        return "missing"
    if isinstance(meta, dict) and "check" in meta:
        return "forbidden_key"
    if not _shape_ok(meta):
        return "shape"
    if meta["v"] != VERSION:
        return "version"
    if not _enums_ok(meta["execution"], meta["summary"]):
        return "enum"
    if meta["digest"] != digest_of(meta):
        return "digest"
    run = record.get("run")
    run_id = run.get("run_id") if isinstance(run, dict) else None
    if meta["ask_id"] != record.get("ask_id") or (
            meta["check_run_id"] is not None and run_id is not None and meta["check_run_id"] != run_id):
        return "ids"
    if meta["bound"] != bound_facts(record.get("answer")):
        return "bound"
    return _clause_problem(_facts(meta, record))


def meta_view(record: dict):
    """The wire value for record["answer_meta"]. Apply it once to a raw record: a wire value has no digest to
    recompute, so passing one back in reads as unverified, the safe direction."""
    meta = record.get("answer_meta")
    if record.get("status") == "running":
        return None if meta is None else {"check": "unverified", "problem": "while_running"}
    if meta is None:
        return {"check": "legacy_unknown"}
    problem = verify_stored(record)
    if problem is not None:
        return {"check": "unverified", "problem": problem}
    return {"check": "verified", "v": VERSION,
            "execution": dict(meta["execution"]),
            "summary": {"state": meta["summary"]["state"], "removals": [dict(r) for r in meta["summary"]["removals"]],
                        "rewrite": meta["summary"]["rewrite"]}}


def check_wire(record: dict):
    """The problem code for a wire value, or None. It has no digest, ids or bound facts, so it recomputes the bound
    facts from the record's own answer and runs the same enum and clause checks as verify_stored."""
    wire = record.get("answer_meta")
    if not (isinstance(wire, dict) and set(wire) == WIRE_KEYS and wire["check"] == "verified" and _is_int(wire["v"])):
        return "shape"
    execution, summary = wire["execution"], wire["summary"]
    shaped = (isinstance(execution, dict) and set(execution) == {"state", "stop_reason"}
              and isinstance(execution["state"], str)
              and (execution["stop_reason"] is None or isinstance(execution["stop_reason"], str))
              and isinstance(summary, dict) and set(summary) == {"state", "removals", "rewrite"}
              and isinstance(summary["state"], str) and isinstance(summary["rewrite"], str)
              and isinstance(summary["removals"], list)
              and all(isinstance(r, dict) and set(r) == {"stage", "cause"} and isinstance(r["stage"], str)
                      and isinstance(r["cause"], str) for r in summary["removals"]))
    if not shaped:
        return "shape"
    if wire["v"] != VERSION:
        return "version"
    if not _enums_ok(execution, summary):
        return "enum"
    facts = _facts({"execution": execution, "summary": summary}, record)
    return _clause_problem(facts)


def snapshot(answer: dict) -> dict:
    """The part of an answer a stage can change that the ledger needs: whether the summary is blank, and each claim's
    id and words. Taken before the stage runs, because stages may edit the answer in place."""
    return {"blank": _blank(answer.get("short_answer")),
            "claims": {c.get("id"): c.get("text") for c in answer.get("claims") or [] if isinstance(c, dict)}}


_K_RULES = ("K2", "K3", "K6", "K8", "K9")
_FIELD_RULES = ("K3", "K6", "K9")


class SummaryLedger:
    """What one gate pass did to the summary, observed from the values the gate already holds: the stage's own
    returned verdict rows (their `rule`), and the claim lists before and after. No reason text is read, and nothing
    here comes from the model: a model can cause a removal, but the stage and cause recorded are those of the code
    path that fired."""

    def __init__(self):
        self.removals: list[tuple[str, str]] = []
        self.rewrite = "not_attempted"

    def note_rewrite(self, outcome: str) -> None:
        if outcome not in REWRITE_OUTCOMES:
            raise ValueError(f"unknown rewrite outcome {outcome!r}")
        self.rewrite = outcome

    def observe(self, stage: str, before: dict, after: dict, rows=()) -> None:
        """Record a removal when the summary was non-blank before the stage and is blank after it."""
        if before["blank"] or not _blank(after.get("short_answer")):
            return
        causes = self._causes(stage, before, after, rows)
        for cause in sorted(set(causes), key=REMOVAL_CAUSES.index):
            if (stage, cause) not in self.removals:
                self.removals.append((stage, cause))
        if self.rewrite == "kept" and stage in LATER_STAGES:
            self.rewrite = "removed_after_check"

    @staticmethod
    def _causes(stage: str, before: dict, after: dict, rows) -> list:
        summary_cuts = [r for r in rows if r.get("claim_id") == "short_answer" and r.get("verdict") == "cut"]
        if stage in ("first_check", "recheck"):
            found = [r.get("rule") for r in summary_cuts if r.get("rule") in _K_RULES]
        elif stage == "field_check":
            found = [r["rule"] if r.get("rule") in _FIELD_RULES else "field_unchecked"
                     for r in summary_cuts if r.get("rule") in _FIELD_RULES or r.get("rule") == "field_check"]
        else:
            kept = {c.get("id"): c.get("text") for c in after.get("claims") or [] if isinstance(c, dict)}
            found = []
            if any(cid not in kept for cid in before["claims"]):
                found.append("claim_cut")
            if stage == "support_check" and any(cid in kept and kept[cid] != text
                                                for cid, text in before["claims"].items()):
                found.append("claim_narrowed")
        return found or ["unattributed"]

    def finalize(self, answer: dict, execution_state: str) -> tuple[str, list, str]:
        """(summary state, removals, rewrite outcome) for the answer that will be stored, or ValueError when what
        was observed contradicts the answer, in which case no metadata is stored."""
        if execution_state != "completed" or answer.get("status") == "insufficient_evidence":
            # A fixed text replaced whatever came before, so the ledger resets (C1 2.4).
            rewrite = "removed_after_check" if (execution_state == "completed" and self.rewrite in (
                "kept", "removed_after_check")) else "not_attempted"
            return "fixed_text", [], rewrite
        if _blank(answer.get("short_answer")):
            if self.removals:
                return "removed", list(self.removals), self.rewrite
            return "blank_unexplained", [], "not_attempted"
        if self.rewrite == "kept":
            return "shown_rewritten", list(self.removals), "kept"
        if self.removals:
            raise ValueError("a summary was removed and is not blank, with no kept rewrite")
        return "shown", [], "not_attempted"
