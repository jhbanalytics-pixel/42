import hashlib
import json
import re
from pathlib import Path

import jsonschema

from core.detect import sqlrun


SOURCE_LIMIT = 12
SOURCE_CHARS = 8000
RECEIPT_WIRE_LIMIT = 16_384
RUN_RECEIPT_WIRE_LIMIT = 1_000_000
COUNTS_WIRE_LIMIT = 9_000_000
_SQL = Path(__file__).parent / "sql/title_purity.sql"
QUERIES = {re.search(r"-- name: (\w+)", sql).group(1): sql for sql in sqlrun.split(_SQL.read_text(encoding="utf-8"))}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "reason", "member_checks"],
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "partial", "unsupported"]},
        "reason": {"type": "string"},
        "member_checks": {"type": "array", "maxItems": SOURCE_LIMIT, "items": {
            "type": "object", "additionalProperties": False, "required": ["post_id", "verdict", "quote"],
            "properties": {"post_id": {"type": "string"},
                           "verdict": {"type": "string", "enum": ["supported", "partial", "unsupported"]},
                           "quote": {"type": "string"}}}},
    },
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def unknown(reason):
    return {"status": "unknown", "reason": reason, "N": None, "member_ids": [], "records": []}


def read_snapshot(client, row, d, market, pack, *, core=sqlrun.CORE, agent=sqlrun.AGENT):
    if row.get("kind") != "topic":
        return None
    code = str(market).lower()
    if code not in {"za", "ng", "ke"} or str(row.get("metric_date")) != d.isoformat() or not row.get("run_id"):
        return unknown("candidate producer binding unavailable")
    params = {"run_date": d, "market": code}
    try:
        from core.understand.cluster import _saved_plan

        rows = sqlrun.query(client, QUERIES["plan"], params, core=core, agent=agent)
        batches, _ = _saved_plan(rows)
        plans = {x.get("plan_id") for x in rows}
        if len(plans) != 1 or not next(iter(plans)):
            return unknown("producer plan identity unavailable")
        checkpoint = rows[0]["checkpoint"]
        checkpoint = json.loads(checkpoint) if isinstance(checkpoint, str) else checkpoint
        if checkpoint.get("market") != code:
            return unknown("producer market mismatch")
        clusters = [x for b in batches for x in json.loads(b["cluster_rows"])]
        expected = [x["cluster_id"] for x in clusters]
        written = rows[0].get("written_ids") or []
        if (len(expected) != len(set(expected)) or len(written) != len(set(written))
                or set(expected) != set(written) or rows[0].get("n") != len(expected)):
            return unknown("producer cluster write incomplete")
        targets = [x for x in clusters if x.get("item_id") == row["item_id"]]
        if len(targets) != 1:
            return unknown("producer cluster missing or ambiguous")
        cid = targets[0]["cluster_id"]
        if not cid.startswith(f"{d:%Y%m%d}-{code}-"):
            return unknown("producer cluster scope mismatch")
        members = [x["post_id"] for b in batches for x in json.loads(b["member_rows"])
                   if x["cluster_id"] == cid]
        if not members or len(members) != len(set(members)):
            return unknown("producer member identity incomplete")
        published = sqlrun.query(client, QUERIES["members"], {"cluster_id": cid}, core=core, agent=agent)
        actual = [x["post_id"] for x in published]
        if (len(actual) != len(set(actual)) or set(actual) != set(members)
                or any(str(x["cluster_date"]) != d.isoformat() or x["market"] != code
                       or x["item_id"] != row["item_id"] for x in published)):
            return unknown("published producer membership differs")
        members = sorted(members)
        evidence = pack.get("evidence") or []
        pack_ids = [x.get("id") for x in evidence]
        if len(pack_ids) != len(set(pack_ids)):
            return unknown("duplicate pack source identity")
        by_id = {x["id"]: x for x in evidence}
        shared = sorted(set(members) & set(by_id))
        if len(shared) > SOURCE_LIMIT:
            return unknown("shared source read exceeds existing evidence bound")
        selected = shared + [i for i in members if i not in shared][:SOURCE_LIMIT - len(shared)]
        contents = sqlrun.query(client, QUERIES["content"],
                               {"run_date": d, "post_ids": json.dumps(selected)}, core=core, agent=agent)
        content_ids = [x["post_id"] for x in contents]
        if len(content_ids) != len(set(content_ids)) or set(content_ids) - set(selected):
            return unknown("source content identity differs")
        records, read_at = [], []
        for source in contents:
            read_at.append(str(source.get("content_read_at")))
            text = source.get("quote_text")
            if source.get("suppressed") or not source.get("record_exists") or not isinstance(text, str) or not text.strip():
                continue
            post_id = source["post_id"]
            if post_id in by_id:
                old = by_id[post_id].get("quote_text") or by_id[post_id].get("text") or ""
                if text != old:
                    return unknown("pack and producer source versions conflict")
            if len(text) > SOURCE_CHARS:
                continue
            record = {"post_id": post_id, "text": text, "source_fields": json.loads(json.dumps({
                k: source.get(k) for k in ("platform", "creator_id", "url", "published_at", "geo_market",
                                          "geo_confidence", "geo_source")}, default=str))}
            records.append({**record, "content_hash": digest(record)})
        reference = {"cluster_id": cid, "cluster_date": d.isoformat(), "market": code,
                     "item_id": row["item_id"]}
        return {"status": "complete", "N": len(members), "member_ids": members, "records": records,
                "producer_plan_id": next(iter(plans)), "cluster_ref": reference, "detect_run_id": row["run_id"],
                "membership_hash": digest({"cluster": reference, "members": members}),
                "content_read_at": sorted(set(read_at))}
    except Exception as exc:
        return unknown(f"producer snapshot unavailable: {type(exc).__name__}")


def context(snapshot):
    return [{"post_id": r["post_id"], "text": r["text"]} for r in snapshot["records"]]


def validate_final_pack(snapshot, pack):
    if snapshot.get("status") != "complete":
        return snapshot
    sources = {r["post_id"]: r for r in snapshot["records"]}
    records = pack.get("evidence") or []
    ids = [r.get("id") for r in records]
    if len(ids) != len(set(ids)):
        return {**snapshot, "status": "unknown", "reason": "duplicate final pack identity"}
    versions = []
    for record in records:
        post_id = record.get("id")
        text = record.get("quote_text") or record.get("text") or ""
        versions.append({"post_id": post_id, "text_hash": digest(text)})
        if post_id in sources and text != sources[post_id]["text"]:
            return {**snapshot, "status": "unknown", "reason": "final pack and producer source versions conflict"}
    return {**snapshot, "final_pack_hash": digest(sorted(versions, key=lambda r: str(r["post_id"])))}


def wire_bytes(value):
    encoded_counts = json.dumps(value)
    return len(json.dumps({"counts": encoded_counts}, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))


def bounded_receipt(audit):
    if wire_bytes(audit) <= RECEIPT_WIRE_LIMIT:
        return audit
    return {"decision": "unknown", "reason": "title receipt byte bound", "N": audit.get("N"),
            "S": 0, "R": 0, "U": audit.get("N"), "title_hash": digest(audit.get("title")),
            "receipt_hash": digest(audit), "member_receipts": []}


def project_receipts(tasks, results):
    receipts, omitted = [], 0
    for candidate in tasks:
        result = results.get(id(candidate)) or {}
        if "title_majority" not in result:
            continue
        audit = bounded_receipt(result["title_majority"])
        entry = {"market": candidate["market"], "item_id": candidate["row"]["item_id"], **audit}
        if wire_bytes(receipts + [entry]) > RUN_RECEIPT_WIRE_LIMIT:
            result["title_written"] = None
            result["checks"].append({"claim_id": None, "rule": "title", "verdict": "cut", "checker": "code",
                                     "detail": "title: run receipt byte bound"})
            result["title_majority"] = {"decision": "unknown", "reason": "run receipt byte bound"}
            omitted += 1
            continue
        receipts.append(entry)
    return receipts, omitted


def assess(snapshot, title, out, *, request_hash, model_id):
    n = snapshot.get("N")
    audit = {**{k: snapshot.get(k) for k in ("producer_plan_id", "cluster_ref", "detect_run_id", "membership_hash",
                                            "final_pack_hash", "content_read_at")},
             "decision": "unknown", "N": n, "S": 0, "R": 0, "U": n,
             "title": title, "title_hash": digest(title), "request_hash": request_hash,
             "response_hash": digest(out) if out is not None else None,
             "checker_identity": model_id, "source_receipts": [], "member_receipts": []}
    if snapshot.get("status") != "complete":
        audit["reason"] = snapshot.get("reason", "producer snapshot unknown")
        return bounded_receipt(audit)
    members = snapshot.get("member_ids") or []
    requested_rows = snapshot.get("records") or []
    requested_ids = [r["post_id"] for r in requested_rows]
    if (type(n) is not int or n <= 0 or len(members) != n or len(set(members)) != n
            or len(requested_ids) != len(set(requested_ids)) or set(requested_ids) - set(members)
            or snapshot.get("membership_hash") != digest({"cluster": snapshot.get("cluster_ref"), "members": members})
            or any(r.get("content_hash") != digest({k: v for k, v in r.items() if k != "content_hash"})
                   for r in requested_rows)):
        audit["reason"] = "producer or content hash differs"
        return bounded_receipt(audit)
    audit["source_receipts"] = [{"post_id": r["post_id"], "content_hash": r["content_hash"],
                                  "text_hash": digest(r["text"]), "source_fields_hash": digest(r["source_fields"]),
                                  "text_chars": len(r["text"])} for r in requested_rows]
    if out is None:
        audit["reason"] = "title support check not run"
        return bounded_receipt(audit)
    if not jsonschema.Draft202012Validator(SCHEMA).is_valid(out):
        audit["reason"] = "malformed title member response"
        return bounded_receipt(audit)
    audit["aggregate_verdict"] = out["verdict"]
    audit["checker_reason_hash"] = digest(out["reason"])
    requested = {r["post_id"]: r for r in requested_rows}
    ids = [x["post_id"] for x in out["member_checks"]]
    if len(ids) != len(set(ids)) or set(ids) - set(requested):
        audit["reason"] = "title member response identity differs"
        return bounded_receipt(audit)
    for check in out["member_checks"]:
        source = requested[check["post_id"]]
        quote = check["quote"]
        supported = check["verdict"] == "supported" and bool(quote.strip()) and quote in source["text"]
        start = source["text"].find(quote) if supported else -1
        audit["member_receipts"].append({"post_id": check["post_id"], "verdict": check["verdict"],
            "validated_support": supported, "content_hash": source["content_hash"], "quote_hash": digest(quote),
            "quote_span": [start, start + len(quote)] if supported else None})
        audit["S"] += supported
    audit["U"] = n - audit["S"]
    if out["verdict"] != "supported":
        audit["reason"] = "original title support did not pass"
    elif 2 * audit["S"] > n:
        audit["decision"] = "pass"
        audit["reason"] = "strict producer majority proved"
    else:
        audit["reason"] = "strict producer majority unproven"
    return bounded_receipt(audit)
