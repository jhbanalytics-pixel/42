import asyncio
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.api.question_worker_bundle import verify_bundle
from src.api.question_worker_process import (
    EngineReply,
    WorkerProcessError,
    invoke_engine,
)
from src.api.question_worker_protocol import (
    _INVOCATION_KEYS,
    _scope,
    encode_worker_input,
)
from src.api.question_worker_result import ResultVerificationError
from src.api.question_worker_store import read_result_record


@dataclass(frozen=True)
class WorkerOutcome:
    engine_reply: EngineReply
    result_record: dict | None


@dataclass(frozen=True)
class QuestionWorker:
    bundle_root: Path
    interpreter: Path
    stamp_path: Path
    lp_commit: str
    bundle_digest: str
    storage_client: object
    try_acquire: object
    release: object

    async def run(self, operation, payload, *, deadline):
        encode_worker_input(operation, payload)
        if type(deadline) not in {int, float} or not math.isfinite(deadline):
            raise WorkerProcessError("worker_deadline_invalid")

        def remaining():
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise WorkerProcessError("worker_deadline_reached")
            return seconds

        remaining()
        acquired = False
        record = None

        async def verify_result(reply):
            nonlocal record
            bound = payload
            pointer = reply
            if operation == "status":
                bound = {
                    key: reply[key] for key in _INVOCATION_KEYS - {"contract_version"}
                }
                bound["contract_version"] = "general_cultural_question_v1"
                pointer = {
                    key: reply[key]
                    for key in [
                        "request_id",
                        "request_digest",
                        "intake_digest",
                        "result_digest",
                        "result_generation",
                    ]
                }
                pointer["state"] = "held" if reply["state"] == "held" else "terminal"
            verified = await asyncio.to_thread(
                read_result_record,
                self.storage_client,
                invocation=bound,
                receipt=pointer,
                deadline=deadline,
            )
            if operation == "status":
                if verified["state"] != reply["state"]:
                    raise ResultVerificationError("result_status_mismatch")
                scope = verified["response"]["intelligence"].get("resolved_scope")
                ceiling = payload["scope"]
                if (
                    not _scope(scope)
                    or any(
                        scope[key] != ceiling[key]
                        for key in ["client_scope_id", "brand_config_id", "theme_id"]
                    )
                    or not set(scope["market_scope"]).issubset(ceiling["market_scope"])
                    or not set(scope["audience_lens_ids"]).issubset(
                        ceiling["audience_lens_ids"]
                    )
                ):
                    raise ResultVerificationError("result_scope_mismatch")
            record = verified
            return True

        try:
            if operation == "execute":
                while True:
                    remaining()
                    if self.try_acquire() is True:
                        acquired = True
                        break
                    await asyncio.sleep(min(0.05, remaining()))
            engine = await invoke_engine(
                self.bundle_root,
                self.interpreter,
                operation,
                payload,
                deadline=deadline,
                verify_runtime=lambda: verify_bundle(
                    self.bundle_root,
                    self.stamp_path,
                    expected_lp_commit=self.lp_commit,
                    expected_bundle_digest=self.bundle_digest,
                ),
                verify_result=verify_result,
            )
            if (
                operation == "status"
                and record is None
                and datetime.now(UTC)
                >= datetime.fromisoformat(engine.reply["deadline_at"])
            ):
                raise WorkerProcessError("worker_status_expired")
            return WorkerOutcome(engine, record)
        finally:
            if acquired:
                self.release()
