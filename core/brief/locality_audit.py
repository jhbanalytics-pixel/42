"""The locality_audit block of the morning payload (C4 v3 section 11.2, review N1): a block of its own, beside
not_assessed and never inside it, so core/api/today.py _not_assessed is untouched by the switch.

A not_local item that sits outside the 90-row candidate pool is not a not_assessed entry, because its locality check
did run and the validator of not_assessed refuses the whole block on a reason it does not know. It is listed here with
its counts, up to LIST_CAP, with the total kept apart from the listing."""

from core.trust.locality import METRIC_VERSION

BLOCK_VERSION = 1
LIST_CAP = 50


def build_locality_audit(detect_run_id, not_local_rows, unreadable_total, represented_ids=()):
    """not_local_rows: the rows of the not_local_audit statement. represented_ids: items already shown as a card, a
    held item or a not_assessed entry, which the audit must not list a second time (review M3). The total counts
    every not_local item the statement found and is not reduced by the listing."""
    total = max((r["not_local_total"] for r in not_local_rows), default=0)
    skip = set(represented_ids)
    items = [{"item_id": r["item_id"], "title": r.get("label") or r["item_id"], "known_posts": r["known_posts"],
              "local_posts": r["local_posts"], "local_share": r["local_share"]}
             for r in not_local_rows if r["item_id"] not in skip][:LIST_CAP]
    return {"block_version": BLOCK_VERSION, "detect_run_id": detect_run_id, "metric_version": METRIC_VERSION,
            "not_local_total": total, "listed": len(items), "unreadable_total": unreadable_total, "items": items}
