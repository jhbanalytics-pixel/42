"""Frozen question bank, request manifest and transport state machine (E01).

The bank freezes the published 36-question test bank as the development
benchmark. The manifest gives every case a stable identifier and a nonce,
computes the authorization and reserve the run needs against a policy
snapshot handed in by the caller, and refuses by name before any submission
when the need does not fit. The state machine drives the existing question
transport through an injected client, persists an append only ledger before
every POST and after every observation, and reconciles a lost acknowledgement
through the status read and the request inventory instead of resubmitting.

Nothing here opens a socket, reads a clock of its own or touches the cloud.
The transport and the clock are injected; the tests drive a fake.
"""

import hashlib
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from uuid import UUID

STATES = (
    "planned",
    "submitted",
    "pending",
    "complete",
    "partial",
    "refused",
    "failed",
    "unknown",
)
# The chat send route accepts this idempotency key grammar and derives the
# request identity from it, so an identical resubmission joins the same request.
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}\Z")
JOB_ID = re.compile(r"chat_[0-9a-f]{32}\Z")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")

BANK_SOURCE = {
    "document": "GENERAL_INTELLIGENCE_EVALUATION.md",
    "location": "operations root, outside the implementation repository",
    "section": "Test bank",
    "lines": "13-50",
    "sha256": "2b946c3cf665c3e95aa969595f2825121fbe64a7c057bc86173b10749fd720c7",
    "statement": (
        "The requested minimum is three questions in each of twelve families. "
        "Freeze the final wording and corpus/policy bindings before the scored "
        "run; retain a separate human-authored holdout for final acceptance."
    ),
    "families_defined_in": {
        "document": (
            ".superpowers/sdd/2026-09-12-42-staging-completion/baseline/"
            "BACKEND_COMPLETION_GOAL.md"
        ),
        "lines": "30-46",
        "sha256": "07cba077b5074e3a78fbe4d713855d9d3dad7563cf9fa0207b83ee9e08c2fd58",
    },
}
# Family names as the published bank prints them, keyed to the goal families.
PUBLISHED_FAMILY_KEYS = {
    "Open discovery": "discovery",
    "Explanation and challenge": "explanation_challenge",
    "Market comparison": "cross_market",
    "Audience and participation": "audience_participation",
    "Cultural carriers": "creators_carriers",
    "History": "history_recurrence",
    "Foresight": "foresight",
    "Financial services": "financial_services",
    "Retail and affordability": "retail_affordability",
    "Mobility and automotive": "mobility_automotive",
    "Broader culture": "broader_culture",
    "BSA": "bsa",
}
FAMILIES = (
    (
        "discovery",
        "Open cultural discovery",
        "Identify emerging behavior without requiring a preselected keyword or brand.",
    ),
    (
        "explanation_challenge",
        "Explanation and challenge",
        (
            "Explain why a behavior may be growing, show rival explanations and "
            "identify what would disprove the interpretation."
        ),
    ),
    (
        "cross_market",
        "Cross-market comparison",
        (
            "Compare the same phenomenon across ZA, NG and KE without turning unequal "
            "collection into false prevalence."
        ),
    ),
    (
        "audience_participation",
        "Audience and participation",
        (
            "Identify observed communities and participation patterns while refusing "
            "demographic certainty without measurements."
        ),
    ),
    (
        "creators_carriers",
        "Creators and cultural carriers",
        (
            "Explain who or what is carrying a movement, with source diversity and "
            "limits on representativeness."
        ),
    ),
    (
        "history_recurrence",
        "History and recurrence",
        (
            "Find comparable earlier episodes using only information available at "
            "each historical point."
        ),
    ),
    (
        "foresight",
        "Foresight",
        (
            "Produce bounded scenarios or forecasts, distinguish them from "
            "observations and evaluate predictions against persistence later."
        ),
    ),
    (
        "financial_services",
        "Financial services strategy",
        (
            "Translate an evidenced cultural tension into a credible role for a bank "
            "with reasons and risks."
        ),
    ),
    (
        "retail_affordability",
        "Retail and affordability",
        (
            "Find a useful retail opportunity from observed behavior without making "
            "unsupported market-size claims."
        ),
    ),
    (
        "mobility_automotive",
        "Mobility and automotive",
        (
            "Investigate changing mobility choices, compare alternatives and "
            "distinguish cultural signals from product facts."
        ),
    ),
    (
        "broader_culture",
        "Culture beyond one category",
        (
            "Handle music, sport, language, food, technology and work questions using "
            "the relevant evidence rather than brand templates."
        ),
    ),
    (
        "bsa",
        "BSA",
        (
            "Answer a country-reputation or national-brand question as one configured "
            "lens with the same evidence rules."
        ),
    ),
)
PROMPTS_PER_FAMILY = 3
HOLDOUT_KINDS = (
    "unseen",
    "unseen",
    "unseen",
    "paraphrase",
    "scope_changing_follow_up",
    "challenge",
    "unsupported",
)
FOLLOW_UP_KINDS = frozenset({"scope_changing_follow_up", "challenge"})
POLICY_FIELDS = (
    "policy_id",
    "as_of",
    "max_requests",
    "cap_microusd",
    "reserve_per_request_microusd",
    "admitted_requests",
    "reserved_microusd",
    "request_deadline_seconds",
    "poll_interval_seconds",
    "retry_backoff_seconds",
)
POLICY_COUNTS = POLICY_FIELDS[2:]
# Lifecycle states the status route projects, mapped onto the eight states.
LIFECYCLE_MAP = {
    "pending": ("pending", None),
    "complete": ("complete", None),
    "partial": ("partial", None),
    "refused": ("refused", None),
    "needs_clarification": ("refused", None),
    "insufficient_evidence": ("refused", "evidence_insufficient"),
    "unavailable": ("failed", None),
    "held": ("failed", "held"),
}


class AllowanceRefused(RuntimeError):
    def __init__(self, code, authorization):
        super().__init__(code)
        self.code = code
        self.authorization = authorization


class ManifestRefused(RuntimeError):
    def __init__(self, code, slot=None):
        super().__init__(code if slot is None else f"{code}: {slot}")
        self.code = code
        self.slot = slot


class TransportTimeout(TimeoutError):
    """The client gave up waiting; the server may still have acted."""


class TransportFailure(OSError):
    """The connection broke; the server may still have acted."""


class TransportResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


def canonical_sha256(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _families():
    return [
        {"key": key, "name": name, "decision": decision}
        for key, name, decision in FAMILIES
    ]


def parse_published_bank(text):
    """The rows of the published test bank table, in published order."""
    rows = []
    for line in text.splitlines():
        if not line.startswith("| Q"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4:
            raise ValueError("bank_row_invalid")
        rows.append(
            {
                "id": cells[0],
                "family_name": cells[1],
                "question": cells[2],
                "proof_demand": cells[3],
            }
        )
    return rows


def development_bank(document_text):
    """The published 36-question bank frozen as the development benchmark.

    The caller passes the exact text of the bank document; any other bytes
    refuse, so the frozen bank can only ever come from the published wording.
    """
    digest = hashlib.sha256(document_text.encode("utf-8")).hexdigest()
    if digest != BANK_SOURCE["sha256"]:
        raise ValueError("bank_source_mismatch")
    rows = parse_published_bank(document_text)
    expected = [f"Q{n:02d}" for n in range(1, len(FAMILIES) * PROMPTS_PER_FAMILY + 1)]
    if [row["id"] for row in rows] != expected:
        raise ValueError("bank_rows_invalid")
    cases = []
    counts = {}
    for row in rows:
        key = PUBLISHED_FAMILY_KEYS.get(row["family_name"])
        if key is None:
            raise ValueError("bank_family_unknown")
        counts[key] = counts.get(key, 0) + 1
        cases.append(
            {
                "case_key": row["id"],
                "family": key,
                "published_family": row["family_name"],
                "kind": "unseen",
                "sequence": counts[key],
                "parent_case_key": None,
                "wording": row["question"],
                "proof_demand": row["proof_demand"],
                "market": "all",
                "market_source": "not_specified_by_bank",
            }
        )
    if len(counts) != len(FAMILIES) or set(counts.values()) != {PROMPTS_PER_FAMILY}:
        raise ValueError("bank_family_counts_invalid")
    return {
        "contract_version": "development_bank_v1",
        "kind": "development_bank",
        "source": dict(BANK_SOURCE),
        "families": _families(),
        "cases": cases,
    }


def holdout_template():
    """84 typed, unfilled slots: three unseen prompts plus one paraphrase,
    scope-changing follow-up, challenge and unsupported case per family."""
    slots = []
    for key, _name, _decision in FAMILIES:
        unseen = 0
        for kind in HOLDOUT_KINDS:
            sequence = None
            if kind == "unseen":
                unseen += 1
                sequence = unseen
                slot_key = f"{key}-unseen-{sequence}"
            else:
                slot_key = f"{key}-{kind}"
            slots.append(
                {
                    "slot_key": slot_key,
                    "family": key,
                    "kind": kind,
                    "sequence": sequence,
                    "parent_slot": f"{key}-unseen-1"
                    if kind in ("paraphrase", *FOLLOW_UP_KINDS)
                    else None,
                    "author": None,
                    "wording": None,
                    "answerability": None,
                    "market": None,
                }
            )
    return {
        "contract_version": "final_holdout_template_v1",
        "kind": "final_holdout",
        "slot_count": len(slots),
        "families": _families(),
        "slots": slots,
    }


def write_json(path, value):
    encoded = json.dumps(value, indent=2, ensure_ascii=True, sort_keys=False) + "\n"
    Path(path).write_bytes(encoded.encode("ascii"))


def load_bank(path):
    bank = json.loads(Path(path).read_bytes().decode("ascii"))
    if bank.get("kind") not in ("development_bank", "final_holdout"):
        raise ValueError("bank_kind_invalid")
    return bank, canonical_sha256(bank)


def validate_holdout(template):
    """Refuse any slot a human has not filled with wording, author and label."""
    slots = template.get("slots")
    expected = holdout_template()["slots"]
    if not isinstance(slots, list) or len(slots) != len(expected):
        raise ManifestRefused("holdout_shape_invalid")
    for slot, shape in zip(slots, expected, strict=True):
        if not isinstance(slot, dict) or any(
            slot.get(field) != shape[field]
            for field in ("slot_key", "family", "kind", "sequence", "parent_slot")
        ):
            raise ManifestRefused("holdout_shape_invalid", slot=shape["slot_key"])
        filled = (
            isinstance(slot.get("wording"), str)
            and slot["wording"].strip()
            and isinstance(slot.get("author"), str)
            and slot["author"].strip()
            and type(slot.get("answerability")) is bool
            and isinstance(slot.get("market"), str)
        )
        if not filled:
            raise ManifestRefused("holdout_slot_unfilled", slot=slot["slot_key"])


def _cases_of(bank):
    if bank.get("kind") == "final_holdout":
        validate_holdout(bank)
        return [
            {
                "case_key": slot["slot_key"],
                "family": slot["family"],
                "kind": slot["kind"],
                "sequence": slot["sequence"],
                "parent_case_key": slot["parent_slot"]
                if slot["kind"] in FOLLOW_UP_KINDS
                else None,
                "wording": slot["wording"],
                "market": slot["market"],
                "answerability": slot["answerability"],
            }
            for slot in bank["slots"]
        ]
    if bank.get("kind") != "development_bank":
        raise ValueError("bank_kind_invalid")
    cases = bank.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("bank_cases_required")
    keys = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("case_key"), str):
            raise ValueError("bank_case_invalid")
        if case["case_key"] in keys:
            raise ValueError("duplicate_case_key")
        keys.add(case["case_key"])
    return [
        {
            "case_key": case["case_key"],
            "family": case.get("family"),
            "kind": case.get("kind", "unseen"),
            "sequence": case.get("sequence"),
            "parent_case_key": case.get("parent_case_key"),
            "wording": case.get("wording"),
            "market": case.get("market"),
            "answerability": case.get("answerability"),
        }
        for case in cases
    ]


def _validate_policy(policy):
    if not isinstance(policy, dict):
        raise ValueError("policy_snapshot_required")
    for field in POLICY_FIELDS:
        if field not in policy:
            raise ValueError(f"policy_field_missing: {field}")
    for field in POLICY_COUNTS:
        if type(policy[field]) is not int or policy[field] < 0:
            raise ValueError(f"policy_field_invalid: {field}")


def build_manifest(bank, *, policy, run_id, nonce_factory=None):
    """Assign identities, compute the authorization need and refuse by name."""
    _validate_policy(policy)
    if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id_invalid")
    nonce_factory = nonce_factory or (lambda: secrets.token_hex(16))
    bank_digest = canonical_sha256(bank)
    cases = []
    nonces = set()
    for case in _cases_of(bank):
        case_id = hashlib.sha256(
            f"{bank_digest}\n{case['case_key']}".encode("ascii")
        ).hexdigest()[:32]
        nonce = nonce_factory()
        if not isinstance(nonce, str) or IDEMPOTENCY_KEY.fullmatch(nonce) is None:
            raise ValueError("nonce_invalid")
        if nonce in nonces:
            raise ValueError("duplicate_nonce")
        nonces.add(nonce)
        cases.append(
            {
                **case,
                "case_id": case_id,
                "nonce": nonce,
                "deadline_seconds": policy["request_deadline_seconds"],
            }
        )
    needed = len(cases)
    reserve = needed * policy["reserve_per_request_microusd"]
    slots = policy["max_requests"] - policy["admitted_requests"]
    remaining = policy["cap_microusd"] - policy["reserved_microusd"]
    code = None
    if needed > slots:
        code = "allowance_slots_exceeded"
    elif reserve > remaining:
        code = "allowance_reserve_exceeded"
    authorization = {
        "requests_needed": needed,
        "reserve_needed_microusd": reserve,
        "remaining_slots": slots,
        "remaining_reserve_microusd": remaining,
        "fits": code is None,
        "refusal_code": code,
    }
    if code is not None:
        raise AllowanceRefused(code, authorization)
    unworded = sum(
        not (isinstance(case["wording"], str) and case["wording"].strip())
        for case in cases
    )
    return {
        "contract_version": "request_manifest_v1",
        "kind": bank["kind"],
        "run_id": run_id,
        "bank_sha256": bank_digest,
        "policy": dict(policy),
        "authorization": authorization,
        "unworded_cases": unworded,
        "submittable": unworded == 0,
        "cases": cases,
    }


class CaseLedger:
    """Append only JSON lines; every row names its case and nonce."""

    def __init__(self, path, *, clock):
        self.path = Path(path)
        self.clock = clock
        self.sequence = 0
        if self.path.exists():
            self.sequence = len(self.path.read_bytes().splitlines())

    def append(self, record):
        for field in ("case_id", "nonce"):
            if not isinstance(record.get(field), str) or not record[field]:
                raise ValueError("ledger_identity_required")
        self.sequence += 1
        row = {"sequence": self.sequence, "recorded_at": _stamp(self.clock)}
        row.update(record)
        line = json.dumps(row, sort_keys=False) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "ab") as handle:
            handle.write(line.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())


def _stamp(clock):
    return clock.utcnow().isoformat().replace("+00:00", "Z")


def _parse_stamp(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _request_id_of(job_id):
    return str(UUID(hex=job_id[5:]))


def _parse_json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _lifecycle_of(payload):
    if not isinstance(payload, dict):
        return None
    lifecycle = payload.get("lifecycle")
    if not isinstance(lifecycle, dict) or not isinstance(lifecycle.get("state"), str):
        return None
    if not isinstance(lifecycle.get("request_id"), str):
        return None
    return lifecycle


class _CaseRun:
    """One case through the eight states over the injected transport."""

    def __init__(self, case, *, manifest, transport, ledger, clock, headers, results):
        self.case = case
        self.policy = manifest["policy"]
        self.run_id = manifest["run_id"]
        self.transport = transport
        self.ledger = ledger
        self.clock = clock
        self.headers = headers
        self.results = results
        self.state = "planned"
        self.reason = None
        self.request_id = None
        self.job_id = None
        self.anchor = None
        self.parent = None
        self.deadline_at = None
        self.deadline_monotonic = None
        self.planned_at = None
        self.observed = 0
        self.terminal = None

    def record(self, event, **detail):
        self.ledger.append(
            {
                "run_id": self.run_id,
                "case_id": self.case["case_id"],
                "case_key": self.case["case_key"],
                "nonce": self.case["nonce"],
                "event": event,
                "state": self.state,
                "request_id": self.request_id,
                **detail,
            }
        )

    def observe(self, new_state=None, reason=None, **detail):
        transition = new_state is not None and new_state != self.state
        previous = self.state
        if transition:
            self.state = new_state
            self.reason = reason
        self.record("observation", transition=transition, from_state=previous, **detail)

    def finish(self, state, reason=None):
        self.state = state
        self.reason = reason
        self.record("final", reason=reason)
        return {
            "case_id": self.case["case_id"],
            "case_key": self.case["case_key"],
            "nonce": self.case["nonce"],
            "family": self.case["family"],
            "kind": self.case["kind"],
            "state": state,
            "reason": reason,
            "request_id": self.request_id,
            "job_id": self.job_id,
            "thread_anchor_request_id": self.anchor,
            "deadline_at": self.deadline_at,
            "terminal": self.terminal,
        }

    def expired(self):
        if self.clock.monotonic() >= self.deadline_monotonic:
            return True
        deadline = _parse_stamp(self.deadline_at)
        return deadline is not None and self.clock.utcnow() >= deadline

    def body(self):
        body = {
            "message": self.case["wording"],
            "history": [],
            "market": self.case["market"],
            "idempotency_key": self.case["nonce"],
        }
        if self.parent is not None:
            body["parent_request_id"] = self.parent["request_id"]
            body["thread_anchor_request_id"] = self.anchor
        return body

    def run(self):
        self.planned_at = self.clock.utcnow()
        self.deadline_monotonic = self.clock.monotonic() + self.case["deadline_seconds"]
        self.record("planned", deadline_seconds=self.case["deadline_seconds"])
        if self.case["kind"] in FOLLOW_UP_KINDS:
            parent = self.results.get(self.case["parent_case_key"])
            if parent is None or parent["state"] != "complete":
                return self.finish("failed", "parent_not_complete")
            self.parent = parent
            self.anchor = parent["thread_anchor_request_id"] or parent["request_id"]
        outcome = self.submit()
        if outcome is not None:
            return outcome
        return self.poll()

    def adopt(self, request_id, job_id=None):
        self.request_id = request_id
        self.job_id = job_id or "chat_" + UUID(request_id).hex
        if self.anchor is None:
            self.anchor = request_id

    def submit(self):
        attempt = 0
        while True:
            attempt += 1
            self.record("post_attempt", attempt=attempt)
            try:
                response = self.transport.send(self.body(), self.headers)
            except (TransportTimeout, TransportFailure) as error:
                self.observe(send_error=type(error).__name__, message=str(error))
                return self.reconcile("unknown", "reconciliation_no_match")
            code = response.status_code
            if code == 429:
                self.observe(send_status=code)
                backoff = self.policy["retry_backoff_seconds"]
                if self.clock.monotonic() + backoff > self.deadline_monotonic:
                    return self.finish("failed", "rate_limited")
                self.clock.sleep(backoff)
                continue
            if code == 202:
                payload = _parse_json(response.text)
                job = payload.get("job_id") if isinstance(payload, dict) else None
                if not isinstance(job, str) or JOB_ID.fullmatch(job) is None:
                    self.observe(send_status=code, malformed=True)
                    return self.reconcile("unknown", "reconciliation_no_match")
                self.adopt(_request_id_of(job), job)
                self.observe("submitted", send_status=code, job_id=job)
                return None
            if code == 503:
                self.observe(send_status=code)
                return self.reconcile("failed", "service_unavailable")
            self.observe(send_status=code)
            return self.finish("failed", f"send_rejected_{code}")

    def reconcile(self, missing_state, missing_reason):
        """Lost acknowledgement: status by job id, then the request inventory.
        Adopt exactly one matching request; never resubmit."""
        matches, listed = self.inventory_matches()
        if len(matches) == 1:
            self.adopt(matches[0])
            self.state = "submitted"
            self.record("reconciliation", via="inventory", candidates=listed)
            return self.poll()
        self.record("reconciliation", via="inventory", candidates=listed)
        if len(matches) > 1:
            return self.finish("unknown", "reconciliation_ambiguous")
        return self.finish(missing_state, missing_reason)

    def inventory_matches(self):
        try:
            response = self.transport.inventory(self.headers)
        except (TransportTimeout, TransportFailure):
            return [], 0
        payload = _parse_json(response.text)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if response.status_code != 200 or not isinstance(rows, list):
            return [], 0
        selected = None if self.case["market"] == "all" else self.case["market"]
        matches = []
        for row in rows:
            if not isinstance(row, dict) or row.get("kind") != "research":
                continue
            if not str(row.get("operation_id", "")).startswith("gq_"):
                continue
            updated = _parse_stamp(row.get("updated_at"))
            if updated is not None and updated < self.planned_at:
                continue
            request_id = row.get("entity_id")
            if self.request_id is not None and request_id != self.request_id:
                continue
            try:
                detail = self.transport.detail(request_id, self.headers)
            except (TransportTimeout, TransportFailure):
                continue
            found = _parse_json(detail.text) if detail.status_code == 200 else None
            if (
                isinstance(found, dict)
                and found.get("question") == self.case["wording"]
                and found.get("selected_market") == selected
            ):
                matches.append(request_id)
        return matches, len(rows)

    def read_status(self):
        """One status read; None when nothing usable came back."""
        try:
            response = self.transport.status(self.job_id, self.headers)
        except (TransportTimeout, TransportFailure) as error:
            self.observe(status_error=type(error).__name__)
            return None
        code = response.status_code
        if code != 200:
            self.observe(status_code=code)
            if 400 <= code < 500 and code != 429:
                return ("rejected", code)
            return None
        lifecycle = _lifecycle_of(_parse_json(response.text))
        if lifecycle is None:
            self.observe(status_code=code, malformed=True)
            return None
        self.observed += 1
        return lifecycle

    def apply(self, lifecycle):
        """Fold one status observation into the machine."""
        if isinstance(lifecycle, tuple):
            return self.finish("failed", f"status_rejected_{lifecycle[1]}")
        if lifecycle["request_id"] != self.request_id:
            self.observe(mismatch=lifecycle["request_id"])
            return self.finish("unknown", "job_id_mismatch")
        if self.deadline_at is None:
            self.deadline_at = lifecycle.get("deadline_at")
        mapped = LIFECYCLE_MAP.get(lifecycle["state"])
        if mapped is None:
            self.observe(lifecycle_state=lifecycle["state"])
            return self.finish("unknown", "lifecycle_state_unrecognized")
        state, default_reason = mapped
        if state == "pending":
            self.observe("pending", lifecycle_state="pending")
            return self.poll()
        reason = lifecycle.get("reason_code") or default_reason
        self.terminal = lifecycle
        self.observe(state, reason, lifecycle_state=lifecycle["state"])
        repeat = self.read_status()
        if isinstance(repeat, dict):
            same = all(
                repeat.get(k) == lifecycle.get(k) for k in ("request_id", "state")
            )
            self.observe(lifecycle_state=repeat["state"], repeat=True)
            if not same:
                return self.finish("unknown", "terminal_conflict")
        else:
            self.record("observation", transition=False, repeat=True, unconfirmed=True)
        return self.finish(state, reason)

    def poll(self):
        while True:
            if self.expired():
                if self.observed:
                    return self.finish("failed", "deadline_expired")
                lifecycle = self.read_status()
                if lifecycle is not None:
                    return self.apply(lifecycle)
                _matches, listed = self.inventory_matches()
                self.record("reconciliation", via="inventory", candidates=listed)
                return self.finish("unknown", "status_unreachable")
            lifecycle = self.read_status()
            if lifecycle is None:
                self.clock.sleep(self.policy["poll_interval_seconds"])
                continue
            if isinstance(lifecycle, tuple) or lifecycle["state"] != "pending":
                return self.apply(lifecycle)
            if lifecycle["request_id"] != self.request_id:
                return self.apply(lifecycle)
            if self.deadline_at is None:
                self.deadline_at = lifecycle.get("deadline_at")
            self.observe("pending", lifecycle_state="pending")
            self.clock.sleep(self.policy["poll_interval_seconds"])


def run_manifest(manifest, *, transport, ledger_dir, clock, passcode):
    """Drive every manifest case serially. Refuses before the first POST when
    the manifest does not fit its allowance or any case lacks wording."""
    if not manifest["authorization"]["fits"]:
        raise ManifestRefused(manifest["authorization"]["refusal_code"])
    if manifest["unworded_cases"]:
        raise ManifestRefused("case_wording_missing")
    for case in manifest["cases"]:
        if case["kind"] in FOLLOW_UP_KINDS and not case.get("parent_case_key"):
            raise ManifestRefused("follow_up_parent_required", slot=case["case_key"])
    headers = {"X-Passcode": passcode}
    ledger = CaseLedger(Path(ledger_dir) / "case-ledger.jsonl", clock=clock)
    results = {}
    for case in manifest["cases"]:
        run = _CaseRun(
            case,
            manifest=manifest,
            transport=transport,
            ledger=ledger,
            clock=clock,
            headers=headers,
            results=results,
        )
        results[case["case_key"]] = run.run()
    return {
        "run_id": manifest["run_id"],
        "bank_sha256": manifest["bank_sha256"],
        "cases": list(results.values()),
    }
