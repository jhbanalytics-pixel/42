"""The stand-in for the question engine in the client lens browser journey.

The question engine is a separate process that the app starts for each
operation (question_worker_process.invoke_engine) and that reads and writes the
question store in Cloud Storage. Neither can run on a test host. This module
stands in for exactly that boundary and nothing above it: GeneralQuestionRoutes
calls StandInEngine.run where it would call the real worker's run, and the Fieldwork
detail reader gets read_observed_question_detail in place of the store reader.

Every input the app sends is checked with the real worker protocol encoder,
and every admission, status and observe reply this module makes goes through
the real protocol decoders, so the app code above the boundary meets the same
shapes it meets in staging.

The stand-in keeps one rule of the engine because the journey has to meet it:
a follow-up cannot take a parent or an anchor admitted under another client
lens, in either direction, and is refused as parent_context_invalid. That is
general_question_parent_capsule.build_capsule in the engine. Answers are the
stored partial reply fixture the export tests use, with one limitation line that
names the lens it was asked under, so the journey can see that each answer kept
its own framing. The stand-in does no retrieval and makes no model call.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from src.api import question_worker_protocol as protocol
from src.api.question_worker_process import EngineReply, WorkerProcessError

APP_ROOT = Path(__file__).resolve().parents[2]
REPLY = json.loads(
    (APP_ROOT / "tests" / "fixtures" / "general-question-stored-reply-partial.json").read_text(
        encoding="utf-8"
    )
)
POLICY_DIGEST = "1" * 64
DEPLOYMENT_DIGEST = "2" * 64
FRAMING = {
    None: "Stand-in engine framing: general 42, no client lens.",
    "bsa_pulse_lens": "Stand-in engine framing: Brand South Africa Pulse lens.",
}


def _hex(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _outcome(reply: dict, result_record=None):
    return SimpleNamespace(
        engine_reply=EngineReply(reply=reply, stderr_observed_bytes=0, stderr_observed_sha256=_hex("")),
        result_record=result_record,
    )


def _bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


class StandInEngine:
    """What the app's worker object answers, kept in memory for one server process."""

    storage_client = None

    def __init__(self):
        self.lock = threading.Lock()
        self.requests: dict[str, dict] = {}
        self.refusals: list[dict] = []

    def _invocation(self, request_id: str) -> dict:
        return {
            "contract_version": "general_cultural_question_v1",
            "request_id": request_id,
            "request_digest": _hex("request:" + request_id),
            "intake_digest": _hex("intake:" + request_id),
            "policy_digest": POLICY_DIGEST,
            "deployment_digest": DEPLOYMENT_DIGEST,
        }

    def _admit(self, payload: dict) -> dict:
        request_id = payload["request_id"]
        lens = payload.get("client_lens")
        parent = payload.get("parent_request_id")
        anchor = payload.get("thread_anchor_request_id")
        with self.lock:
            existing = self.requests.get(request_id)
            if existing is not None and existing["transport"] != payload["transport"]:
                raise WorkerProcessError("run_id_conflict")
            if parent is not None:
                for reference in dict.fromkeys(item for item in (anchor, parent) if item):
                    bound = self.requests.get(reference)
                    if bound is None or bound["scope"] != payload["scope"]:
                        self.refusals.append({"request_id": request_id, "code": "parent_unavailable"})
                        raise WorkerProcessError("parent_unavailable")
                    # The engine's capsule rule: a thread keeps the lens it was asked under.
                    if bound["client_lens"] != lens:
                        self.refusals.append(
                            {"request_id": request_id, "code": "parent_context_invalid"}
                        )
                        raise WorkerProcessError("parent_context_invalid")
            if existing is None:
                self.requests[request_id] = {
                    "request_id": request_id,
                    "transport": copy.deepcopy(payload["transport"]),
                    "scope": copy.deepcopy(payload["scope"]),
                    "selected_market": payload["selected_market"],
                    "client_lens": copy.deepcopy(lens),
                    "parent_request_id": parent,
                    "thread_anchor_request_id": anchor,
                    "result": None,
                }
        reply = {
            "contract_version": "general_question_admission_result_v1",
            "job_id": "chat_" + UUID(request_id).hex,
            "invocation": self._invocation(request_id),
            "request_generation": "1",
            "intake_generation": "1",
            "deadline_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        }
        return protocol.decode_admission_reply(_bytes(reply), payload)

    def _answer(self, stored: dict) -> dict:
        """The stored reply fixture, answered for this request under its own lens."""
        if stored["result"] is None:
            intelligence = copy.deepcopy(REPLY)
            intelligence["request_id"] = stored["request_id"]
            intelligence["request_digest"] = _hex("request:" + stored["request_id"])
            lens_id = (stored["client_lens"] or {}).get("client_lens_id")
            intelligence["limitations"] = [FRAMING[lens_id], *intelligence["limitations"]]
            stored["result"] = {
                "state": intelligence["status"],
                "plan_digest": _hex("plan:" + stored["request_id"]),
                "response": {
                    "answer": "Stand-in answer.",
                    "sources": [],
                    "intelligence": intelligence,
                },
            }
        return stored["result"]

    def _status(self, payload: dict):
        with self.lock:
            stored = self.requests.get(payload["request_id"])
            if stored is None or stored["scope"] != payload["scope"]:
                raise WorkerProcessError("request_unknown")
            record = self._answer(stored)
            lens = stored["client_lens"]
        reply = {
            **self._invocation(payload["request_id"]),
            "contract_version": "general_question_status_v1",
            "state": record["state"],
            "deadline_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            "result_digest": _hex("result:" + payload["request_id"]),
            "result_generation": "1",
        }
        if lens is not None:
            reply["client_lens"] = lens
        decoded = protocol.decode_status_reply(_bytes(reply), payload)
        return _outcome(decoded, copy.deepcopy(record))

    def _observe(self, payload: dict):
        if payload["request_id"] is None:
            # The Fieldwork inventory is not part of this journey.
            raise WorkerProcessError("observation_invalid")
        with self.lock:
            stored = self.requests.get(payload["request_id"])
            if stored is None or stored["scope"] != payload["scope"]:
                raise WorkerProcessError("observation_request_unavailable")
            record = self._answer(stored)
        reply = {
            "contract_version": "general_question_observe_reply_v1",
            "mode": "detail",
            "ledger_generation": "1",
            "invocation": self._invocation(payload["request_id"]),
            "request_generation": "1",
            "intake_generation": "1",
            "observed_state": record["state"],
            "result_pointer": {
                **{
                    key: self._invocation(payload["request_id"])[key]
                    for key in ("request_id", "request_digest", "intake_digest")
                },
                "result_digest": _hex("result:" + payload["request_id"]),
                "result_generation": "1",
                "state": record["state"],
            },
            "reserved_microusd": 100000,
            "missing_work": [],
        }
        return _outcome(protocol.decode_observe_reply(_bytes(reply), payload))

    async def run(self, operation, payload, *, deadline):
        protocol.encode_worker_input(operation, payload)
        if operation == "admit":
            return _outcome(self._admit(payload))
        if operation == "status":
            return self._status(payload)
        if operation == "observe":
            return self._observe(payload)
        raise WorkerProcessError("worker_operation_unavailable")

    def read_observed_question_detail(self, storage_client, *, observe_input, observed_reply, deadline):
        """What the store reader returns: the stored request, intake, result and lens."""
        with self.lock:
            stored = self.requests[observe_input["request_id"]]
            record = self._answer(stored)
            return {
                "request_bytes": _bytes(
                    {
                        "question": stored["transport"]["message"],
                        "history": [
                            {"role": turn["role"], "text": turn["content"]}
                            for turn in stored["transport"]["history"]
                        ],
                        "requested_window": None,
                    }
                ),
                "intake_bytes": _bytes({"selected_market": stored["selected_market"]}),
                "result_record": copy.deepcopy(record),
                "plan_window": copy.deepcopy(record["response"]["intelligence"]["window"]),
                "client_lens": copy.deepcopy(stored["client_lens"]),
            }

    def summary(self) -> dict:
        with self.lock:
            return {
                "requests": [
                    {
                        "request_id": item["request_id"],
                        "question": item["transport"]["message"],
                        "client_lens_id": (item["client_lens"] or {}).get("client_lens_id"),
                        "configuration_digest": (item["client_lens"] or {}).get(
                            "configuration_digest"
                        ),
                        "parent_request_id": item["parent_request_id"],
                        "thread_anchor_request_id": item["thread_anchor_request_id"],
                    }
                    for item in self.requests.values()
                ],
                "refusals": list(self.refusals),
            }
