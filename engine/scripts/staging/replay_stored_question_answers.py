"""Replay stored Ask answers against the prompt and schema digests they were made under.

Two steps. ``capture`` is a read only copy of every object version under the staging
question store prefix into a local mirror, with a manifest of name, generation, size and
SHA256. ``replay`` runs offline over that mirror: for each admitted request it resolves the
recorded planning and answering bindings to their adapters through the exact digest
catalogue, rebuilds both provider requests under those old adapters and checks every
recorded digest, then reprojects the stored raw answer and compares it byte for byte with
the published result through the same validators the status reader uses.

The replay bucket refuses every write, so a replay can never change what it reads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.open_intelligence.brain_contract import canonical_bytes

BUCKET = "listening-post-staging-cache"
PREFIX = "open-intelligence/v2/staging/general-questions/"
PROJECT = "ogilvy-trends-v2"
MANIFEST_VERSION = "stored_question_mirror_v1"
REPORT_VERSION = "stored_question_replay_v1"
SCOPE_FIELDS = (
    "client_scope_id",
    "market_scope",
    "brand_config_id",
    "audience_lens_ids",
    "theme_id",
)
_MAX_BYTES = 4 * 1024 * 1024


class ReplayWriteRefused(Exception):
    pass


def _object_file(name, generation):
    return quote(name, safe="") + "#" + str(generation)


def capture(client, destination):
    """Copy every version under the store prefix; the destination must not exist yet."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "objects").mkdir()
    entries = []
    for blob in client.list_blobs(BUCKET, prefix=PREFIX, versions=True):
        if type(blob.size) is not int or not 0 <= blob.size <= _MAX_BYTES:
            raise ValueError(f"object_too_large {blob.name}")
        raw = blob.download_as_bytes(if_generation_match=blob.generation, raw_download=True)
        if len(raw) != blob.size:
            raise ValueError(f"object_size_mismatch {blob.name}")
        path = destination / "objects" / _object_file(blob.name, blob.generation)
        with path.open("xb") as handle:
            handle.write(raw)
        entries.append(
            {
                "name": blob.name,
                "generation": blob.generation,
                "size": blob.size,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "live": blob.time_deleted is None,
            }
        )
    entries.sort(key=lambda item: (item["name"], item["generation"]))
    manifest = {
        "contract_version": MANIFEST_VERSION,
        "bucket": BUCKET,
        "prefix": PREFIX,
        "objects": entries,
    }
    # Named newline: the manifest bytes are hashed, so a Windows run must write what Linux does.
    with (destination / "manifest.json").open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    return manifest


class _MirrorBlob:
    def __init__(self, bucket, name, generation=None, size=None):
        self.bucket, self.name, self.generation, self.size = bucket, name, generation, size

    def download_as_bytes(self, *, if_generation_match, **_):
        if if_generation_match != self.generation:
            raise ValueError("mirror_generation_mismatch")
        return self.bucket._raw(self.name, self.generation)

    def upload_from_string(self, *_, **__):
        raise ReplayWriteRefused(self.name)


class MirrorBucket:
    """Read only stand in for the staging bucket, served from a captured mirror."""

    name = BUCKET

    def __init__(self, root):
        self.root = Path(root)
        manifest = json.loads((self.root / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("contract_version") != MANIFEST_VERSION or manifest.get("bucket") != BUCKET:
            raise ValueError("mirror_manifest_invalid")
        self.entries = {}
        self.live = {}
        for item in manifest["objects"]:
            key = (item["name"], item["generation"])
            if key in self.entries:
                raise ValueError("mirror_manifest_invalid")
            self.entries[key] = item
            if item["live"]:
                if item["name"] in self.live:
                    raise ValueError("mirror_manifest_invalid")
                self.live[item["name"]] = item["generation"]
        self.write_attempts = []
        self.integrity_failures = []

    def _raw(self, name, generation):
        item = self.entries[(name, generation)]
        raw = (self.root / "objects" / _object_file(name, generation)).read_bytes()
        if len(raw) != item["size"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
            self.integrity_failures.append({"name": name, "generation": generation})
            raise ValueError("mirror_object_invalid")
        return raw

    def get_blob(self, name, *, generation=None, **_):
        if generation is None:
            generation = self.live.get(name)
            if generation is None:
                return None
        item = self.entries.get((name, generation))
        if item is None:
            return None
        return _MirrorBlob(self, name, generation, item["size"])

    def blob(self, name):
        self.write_attempts.append(name)
        return _MirrorBlob(self, name)


def replay_request(bucket, request_id, *, snapshot_validator=None, records=None):
    """Replay one admitted request; returns a report row and never raises for a bad request."""
    from src.analysis.open_intelligence.general_question_calls import GeneralQuestionCalls
    from src.analysis.open_intelligence.general_question_execution import (
        question_answer_validators,
    )
    from src.analysis.open_intelligence.general_question_parent_context import (
        parent_planning_aliases,
        read_parent_context,
        recorded_response_versions,
    )
    from src.analysis.open_intelligence.general_question_planning import (
        planning_request_for_call,
    )
    from src.analysis.open_intelligence.general_question_result import _selected
    from src.analysis.open_intelligence.general_question_store import (
        _LEDGER,
        GeneralQuestionStore,
        QuestionStoreError,
        _QuestionObjects,
    )

    row = {"request_id": request_id, "checks": {}}
    try:
        objects = _QuestionObjects(bucket)
        control = objects.read(_LEDGER).value
        admission = control["requests"][request_id]
        stored_request = objects.read(
            f"requests/{request_id}/request.json",
            generation=int(admission["request_generation"]),
        ).value
        policy = objects.read(f"policies/{admission['policy_digest']}/policy.json").value
        store = GeneralQuestionStore(
            bucket, policy=policy, deployment_digest=admission["deployment_digest"]
        )
        scope = {field: stored_request[field] for field in SCOPE_FIELDS}
        context = store.read_request(request_id, scope=scope)
        request, intake = context["request"], context["intake"]
        row["admitted_at"] = admission["admitted_at"]
        row["policy_digest"] = admission["policy_digest"]
        row["deployment_digest"] = admission["deployment_digest"]
        calls_state = admission.get("execution", {}).get("calls", {})
        row["calls"] = sorted(stage for stage, call in calls_state.items() if call["response"])
        row["result"] = admission.get("result") is not None
        calls = GeneralQuestionCalls(store)
        bindings = {}
        for stage in row["calls"]:
            bindings[stage] = calls.read_response(request_id, stage=stage, scope=scope)
        if "planning" in bindings:
            planning_request_for_call(
                bindings["planning"]["binding"],
                request=request,
                intake=intake,
                policy=store._stored_policy(request["policy_digest"]),
                parent_context=read_parent_context(store, request, intake),
            )
            row["checks"]["planning_request_rebuilt"] = True
        versions = recorded_response_versions(
            {"request": request, "admission": admission},
            parent_aliases=parent_planning_aliases(store, request, intake),
        )
        if versions is not None:
            row["versions"] = {
                stage: {
                    key: versions[stage][key]
                    for key in (
                        "adapter",
                        "system_instruction_digest",
                        "response_schema_digest",
                        "model",
                    )
                }
                for stage in ("planning", "answering")
                if versions[stage] is not None
            }
        if row["result"]:
            _, answer_validator = question_answer_validators(store, scope)
            if snapshot_validator is not None:
                from src.analysis.open_intelligence.general_question_runtime import (
                    validate_persisted_question_answer,
                )

                def answer_validator(response, *, request, plan, snapshot):
                    return validate_persisted_question_answer(
                        response,
                        store=store,
                        scope=scope,
                        request=request,
                        plan=plan,
                        snapshot=snapshot,
                        validate_snapshot=snapshot_validator,
                    )

            reprojections = []

            def counted(response, **kwargs):
                checked = answer_validator(response, **kwargs)
                reprojections.append(True)
                return checked

            record = _selected(store, context, counted)
            row["result_state"] = record["state"]
            row["result_status"] = record["response"]["intelligence"]["status"]
            row["checks"]["published_result_reprojected"] = bool(reprojections)
            if records is not None and reprojections:
                records[request_id] = {"request": request, "record": record}
        row["replay_class"] = _replay_class(row)
        row["outcome"] = "replayed"
    except QuestionStoreError as error:
        row["outcome"], row["error"] = "refused", error.code
    except ReplayWriteRefused as error:
        row["outcome"], row["error"] = "write_attempted", str(error)
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        row["outcome"], row["error"] = "refused", type(error).__name__ + ": " + str(error)[:200]
    return row


def _replay_class(row):
    """Which evidence a replayed row carries; only answer_reprojected proves an old answer."""
    if row["checks"].get("published_result_reprojected"):
        return "answer_reprojected"
    if "answering" in row["calls"]:
        return "answer_call_without_reprojected_result"
    if row["result"]:
        return "result_without_answer_call"
    return "no_result"


def replay(root, *, snapshot_validator=None):
    from src.analysis.open_intelligence.general_question_store import _LEDGER, _QuestionObjects

    bucket = MirrorBucket(root)
    control = _QuestionObjects(bucket).read(_LEDGER)
    if control is None:
        raise ValueError("mirror_ledger_missing")
    rows = [
        replay_request(bucket, request_id, snapshot_validator=snapshot_validator)
        for request_id in sorted(control.value["requests"])
    ]
    adapters, classes = {}, {}
    for row in rows:
        if "replay_class" in row:
            classes[row["replay_class"]] = classes.get(row["replay_class"], 0) + 1
        for stage, version in row.get("versions", {}).items():
            key = stage + ":" + version["adapter"]
            adapters[key] = adapters.get(key, 0) + 1
    report = {
        "contract_version": REPORT_VERSION,
        "manifest_sha256": hashlib.sha256((Path(root) / "manifest.json").read_bytes()).hexdigest(),
        "request_count": len(rows),
        "outcomes": {
            outcome: sum(row["outcome"] == outcome for row in rows)
            for outcome in sorted({row["outcome"] for row in rows})
        },
        "replay_classes": dict(sorted(classes.items())),
        "adapters": dict(sorted(adapters.items())),
        "write_attempts": bucket.write_attempts,
        "mirror_integrity_failures": bucket.integrity_failures,
        "requests": rows,
    }
    return report


PACKET_VERSION = "answer_support_review_packet_v1"
VERDICTS = ("supported", "contradicted", "unverified")
ASSESSMENT_FIELDS = (
    "verdict",
    "quote_relevant",
    "market_and_time_valid",
    "notes",
    "assessor",
    "assessed_at",
)


def _claim_rows(response):
    intelligence = response["intelligence"]
    receipts = {item["receipt_id"]: item for item in intelligence["receipts"]}
    rows = []
    for claim in intelligence["claims"]:
        cited = []
        for receipt_id in claim["receipt_ids"]:
            receipt = receipts[receipt_id]
            spans = [
                {
                    "source_field": binding["source_field"],
                    "start": binding["start"],
                    "end": binding["end"],
                    "quote": receipt[binding["source_field"]][binding["start"] : binding["end"]],
                    "quote_sha256": binding["quote_sha256"],
                }
                for binding in receipt.get("quote_bindings") or []
                if binding["claim_id"] == claim["claim_id"]
            ]
            purposes = sorted(
                item["evidence_purpose"]
                for item in receipt.get("evidence_purposes") or []
                if item["claim_id"] == claim["claim_id"]
            )
            cited.append(
                {
                    "receipt_id": receipt_id,
                    "citation_label": receipt["citation_label"],
                    "market": receipt["market"],
                    "published_at": receipt["published_at"],
                    "collected_at": receipt["collected_at"],
                    "source_label": receipt["source_label"],
                    "source_family": receipt["source_family"],
                    "url": receipt["url"],
                    "content_digest": receipt["content_digest"],
                    "excerpt": receipt["excerpt"],
                    "quote_spans": spans,
                    "evidence_purposes": purposes,
                }
            )
        rows.append(
            {
                "claim_id": claim["claim_id"],
                "kind": claim["kind"],
                "support_state": claim["support_state"],
                "text": claim["text"],
                "limitations": claim["limitations"],
                "parent_claim_ids": claim["parent_claim_ids"],
                "cited": cited,
                "assessment": dict.fromkeys(ASSESSMENT_FIELDS),
            }
        )
    return rows


def support_review_packet(root, destination, *, snapshot_validator=None):
    """Claims of every reprojected answer, with cited spans and blank assessment fields.

    Only answers whose published result the replay reprojected are included. The packet
    carries question and source text for an assessor; it is never filled here.
    """
    from src.analysis.open_intelligence.general_question_store import _LEDGER, _QuestionObjects

    bucket = MirrorBucket(root)
    control = _QuestionObjects(bucket).read(_LEDGER)
    if control is None:
        raise ValueError("mirror_ledger_missing")
    records = {}
    for request_id in sorted(control.value["requests"]):
        replay_request(bucket, request_id, snapshot_validator=snapshot_validator, records=records)
    if bucket.write_attempts or bucket.integrity_failures:
        raise ValueError("mirror_replay_unclean")
    answers, left_out = [], {}
    for request_id in sorted(control.value["requests"]):
        item = records.get(request_id)
        status = None if item is None else item["record"]["response"]["intelligence"]["status"]
        if status not in ("complete", "partial"):
            reason = "not_reprojected" if item is None else "status_" + status
            left_out[reason] = left_out.get(reason, 0) + 1
            continue
        response = item["record"]["response"]
        intelligence = response["intelligence"]
        answers.append(
            {
                "request_id": request_id,
                "question": item["request"]["question"],
                "as_of": item["request"]["as_of"],
                "status": intelligence["status"],
                "window": intelligence["window"],
                "market_scope": intelligence["resolved_scope"]["market_scope"],
                "answer": response["answer"],
                "limitations": intelligence["limitations"],
                "claims": _claim_rows(response),
            }
        )
    packet = {
        "contract_version": PACKET_VERSION,
        "manifest_sha256": hashlib.sha256((Path(root) / "manifest.json").read_bytes()).hexdigest(),
        "verdicts": list(VERDICTS),
        "assessment_fields": list(ASSESSMENT_FIELDS),
        "left_out": dict(sorted(left_out.items())),
        "answers": answers,
    }
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    raw = canonical_bytes(packet)
    with (destination / "packet.json").open("xb") as handle:
        handle.write(raw)
    with (destination / "packet.md").open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(_packet_markdown(packet, hashlib.sha256(raw).hexdigest()))
    return packet


def _packet_markdown(packet, digest):
    lines = [
        "# Answer support review",
        "",
        f"Packet digest {digest}. Mirror manifest {packet['manifest_sha256']}.",
        "Requests left out, by reason: "
        + (", ".join(f"{key} {value}" for key, value in packet["left_out"].items()) or "none")
        + ".",
        "",
        "For each claim, read only the cited records. Record a verdict of supported,"
        " contradicted or unverified, whether the quoted span is relevant to the claim,"
        " and whether the record's market and time fit the question. A literal quote is"
        " not support on its own. Absence of a contrary record is not consensus. Record"
        " your name and the UTC time. The person who built or ran the system does not"
        " assess.",
        "",
    ]
    for answer in packet["answers"]:
        lines += [
            f"## Request {answer['request_id']}",
            "",
            f"Question: {answer['question']}",
            "",
            f"Status {answer['status']}, as of {answer['as_of']}.",
            f"Markets {', '.join(answer['market_scope'])}; window {answer['window']['start']}"
            f" to {answer['window']['end']}.",
            "",
            f"Answer as shown: {answer['answer']}",
            "",
        ]
        for claim in answer["claims"]:
            lines += [
                f"### Claim {claim['claim_id']} ({claim['kind']}, {claim['support_state']})",
                "",
                claim["text"],
                "",
            ]
            if claim["parent_claim_ids"]:
                lines += [f"Builds on claims: {', '.join(claim['parent_claim_ids'])}.", ""]
            for cited in claim["cited"]:
                lines.append(
                    f"Cited {cited['citation_label']}: {cited['source_label']}, market"
                    f" {cited['market']}, published {cited['published_at']},"
                    f" purposes {', '.join(cited['evidence_purposes']) or 'none recorded'}."
                )
                for span in cited["quote_spans"]:
                    lines.append(f"Quoted span: {span['quote']}")
                lines += [f"Record text: {cited['excerpt']}", ""]
            lines += [
                "Verdict: ",
                "Quote relevant: ",
                "Market and time valid: ",
                "Notes: ",
                "Assessor: ",
                "Assessed at (UTC): ",
                "",
            ]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    captured = commands.add_parser("capture")
    captured.add_argument("--destination", required=True)
    replayed = commands.add_parser("replay")
    replayed.add_argument("--mirror", required=True)
    replayed.add_argument("--report", required=True)
    packeted = commands.add_parser("packet")
    packeted.add_argument("--mirror", required=True)
    packeted.add_argument("--destination", required=True)
    args = parser.parse_args(argv)
    if args.command == "capture":
        from google.cloud import storage

        manifest = capture(storage.Client(project=PROJECT), args.destination)
        print(json.dumps({"objects": len(manifest["objects"])}))
        return 0
    if args.command == "packet":
        packet = support_review_packet(args.mirror, args.destination)
        claims = sum(len(answer["claims"]) for answer in packet["answers"])
        print(json.dumps({"answers": len(packet["answers"]), "claims": claims}))
        return 0
    report = replay(args.mirror)
    with Path(args.report).open("xb") as handle:
        handle.write(canonical_bytes(report))
    print(
        json.dumps(
            {
                "request_count": report["request_count"],
                "outcomes": report["outcomes"],
                "replay_classes": report["replay_classes"],
                "adapters": report["adapters"],
                "write_attempts": len(report["write_attempts"]),
                "mirror_integrity_failures": len(report["mirror_integrity_failures"]),
            }
        )
    )
    clean = (
        set(report["outcomes"]) <= {"replayed"}
        and not report["write_attempts"]
        and not report["mirror_integrity_failures"]
    )
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
