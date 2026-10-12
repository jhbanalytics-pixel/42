"""Replay scenarios for the ranked lead lane: every fixture, the demographic flag variants, the REVIEW4 variants V1 to V9
and the critic path at T2 and T3, each run through the real run_ask with every model call counted by schema.

The module imports nothing that is new in this lane, so the same scenarios run in the tree before the lane (062690d), which
wrote dispatch_parity_062690d.json, and in any later tree. run(scenario) returns what the parity tests compare."""
import json
import re
import tempfile
from pathlib import Path

from core.agent import answer_state, critic, writer
from core.api import smoke
from core.eval import ask_replay

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
BENCH = Path(ask_replay.__file__).parent / "ask_replay_fixtures"
SCHEMA_NAMES = {id(writer.WRITER_SCHEMA): "writer", id(writer.SUPPORT_SCHEMA): "support",
                id(writer.K4_REWRITE_SCHEMA): "k4_rewrite", id(writer.HEADLINE_REWRITE_SCHEMA): "headline_rewrite",
                id(writer.FIELDS_SCHEMA): "fields", id(critic.CRITIC_SCHEMA): "critic"}
NOT_REPLAYS = ("dispatch", "writer_source")  # goldens beside the fixtures
PLATFORMS = list(ask_replay.ask.PLATFORM_GROUPS)
CRITIC_CUTS = (("TOP-ZA-K1CUT", ("c4", "c5", "c6")), ("K1CUT-COUNT", ("c4", "c5", "c6")),
               ("TOP-ZA", ("c4", "c5", "c6")), ("TOP-ZA", ("c7",)))
FOLDERS = {"bench": BENCH, "ranked": FIXTURES / "ranked_briefing", "lead": FIXTURES / "lead_cut",
           "variants": FIXTURES / "lead_cut" / "variants"}


def scenarios():
    """Every scenario, as a dict with a stable id. The id is the key of the parity golden."""
    found = []
    for folder in ("bench", "ranked", "lead"):
        for path in sorted(FOLDERS[folder].glob("*.json")):
            if not path.stem.startswith(NOT_REPLAYS):
                found.append({"id": f"{folder}/{path.stem}", "folder": folder, "qid": path.stem})
    for folder, qid in (("ranked", "TOP-ZA"), ("ranked", "TOP-ZA-K1CUT"), ("lead", "K1CUT-COUNT"),
                        ("ranked", "TOP-ZA-SUPPORTCUT")):
        fx = json.loads((FOLDERS[folder] / f"{qid}.json").read_text(encoding="utf-8"))
        for claim in fx["writer"]["claims"]:
            found.append({"id": f"flag/{qid}/{claim['id']}", "folder": folder, "qid": qid, "flags": [claim["text"][:40]]})
    for path in sorted(FOLDERS["variants"].glob("*.json")):
        flags = json.loads((FOLDERS["variants"] / f"{path.stem}.flags").read_text(encoding="utf-8"))
        found.append({"id": f"variant/{path.stem}", "folder": "variants", "qid": path.stem, "flags": flags})
    for tier in ("T2", "T3"):
        for qid, cut in CRITIC_CUTS:
            found.append({"id": f"critic/{tier}/{qid}/{'-'.join(cut)}", "folder": "ranked" if qid != "K1CUT-COUNT" else "lead",
                          "qid": qid, "tier": tier, "cut": list(cut)})
    return found


def run(scenario, *, skill=None):
    """Run one scenario. Returns the calls by schema, the stored lead, status, claim ids, the typed summary state, the
    state check, the smoke's verdict on the stored record and the full replay result under "result"."""
    calls, captured, flags, cut = {}, {}, scenario.get("flags") or [], set(scenario.get("cut") or [])
    original, real_run_ask = ask_replay.ReplayModel.complete_json, ask_replay.ask.run_ask

    def spy(self, *, system, user, schema, model, max_tokens):
        name = SCHEMA_NAMES.get(id(schema), "other")
        calls[name] = calls.get(name, 0) + 1
        if schema is critic.CRITIC_SCHEMA:
            ids = list(dict.fromkeys(re.findall(r'"id": "(c[0-9]+)"', user)))
            verdicts = [{"claim_id": cid, "verdict": "cut" if cid in cut else "keep", "label": "", "reason": "r",
                         "quote": "", "query": ""} for cid in ids]
            return ({"verdicts": verdicts, "missing_perspectives": [], "followups": [], "overall_risk": "low"},
                    {"input_tokens": 1000, "output_tokens": 100, "usd": 0.001})
        out, usage = original(self, system=system, user=user, schema=schema, model=model, max_tokens=max_tokens)
        if flags and schema is writer.SUPPORT_SCHEMA:
            shown = ask_replay.CLAIM_LINE.match(user)
            if shown and any(shown.group(1).startswith(f) for f in flags):
                out = {**out, "demographic_inference": True}
        return out, usage

    def run_ask(body, *args, **kwargs):
        extra = {"skill": skill} if skill else {}
        if body.get("tier") == "T3":  # a confirmed investigation plan is what T3 runs; the replay research ignores it
            plan = {"sub_questions": [{"id": "q1", "text": "What led the week?", "platforms": [PLATFORMS[0]], "credits": 10}],
                    "gap_round": False, "max_credits": 40, "max_model_usd": ask_replay.ask.model_daily_usd(now=ask_replay.AS_OF)}
            extra.update(plan=plan, max_credits=40, max_model_usd=plan["max_model_usd"])
        out = real_run_ask({**body, **extra}, *args, **kwargs)
        captured["out"] = out
        return out

    saved, folder = ask_replay.FIXTURES, FOLDERS[scenario["folder"]]
    with tempfile.TemporaryDirectory() as tmp:
        if scenario.get("tier"):
            fx = json.loads((folder / f"{scenario['qid']}.json").read_text(encoding="utf-8"))
            fx = {**fx, "id": scenario["id"].replace("/", "_"), "tier": scenario["tier"]}
            fx["vars"] = {**fx["vars"], "id": fx["id"]}
            Path(tmp, f"{fx['id']}.json").write_text(json.dumps(fx), encoding="utf-8")
            folder, qid = Path(tmp), fx["id"]
        else:
            qid = scenario["qid"]
        ask_replay.ReplayModel.complete_json, ask_replay.ask.run_ask, ask_replay.FIXTURES = spy, run_ask, folder
        try:
            try:
                result = ask_replay.run_question(qid)
            except TypeError:  # the replay report cannot price a T3 hold; the run itself finished and was captured
                out = captured["out"]
                stored = {"ask_id": out["answer_meta"]["ask_id"], "status": "complete", "answer": out["answer"],
                          "run": out["run"], "answer_meta": out["answer_meta"]}
                result = {"error": None, "answer": out["answer"], "run": out["run"], "answer_meta": out["answer_meta"],
                          "meta_problem": answer_state.verify_stored(stored), "claim_checks": [], "events": []}
        finally:
            ask_replay.ReplayModel.complete_json, ask_replay.ask.run_ask, ask_replay.FIXTURES = original, real_run_ask, saved
    row = {"id": scenario["id"], "calls": dict(sorted(calls.items())), "error": result["error"], "result": result}
    if result["error"] is None:
        answer, meta = result["answer"], result["answer_meta"]
        stored = {"ask_id": meta["ask_id"], "status": "complete", "answer": answer, "run": result["run"], "answer_meta": meta}
        ok, why = smoke.check_ask_record({**stored, "answer_meta": answer_state.meta_view(stored)})
        row.update({"lead": answer["short_answer"], "status": answer["status"], "claims": [c["id"] for c in answer["claims"]],
                    "summary": {"state": meta["summary"]["state"], "removals": meta["summary"]["removals"],
                                "rewrite": meta["summary"]["rewrite"]},
                    "meta_problem": result["meta_problem"], "smoke": [ok, why]})
    return row


def parity_view(row):
    """What the parity golden pins: the stored values and the calls, never the gaps or rows the lane adds."""
    keys = ("calls", "error", "lead", "status", "claims", "summary", "meta_problem")
    return {k: row.get(k) for k in keys}
