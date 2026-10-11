"""Which post each comment or transcript span in an answer sits under, kept beside the answer and not in it (C5 v2
section 5.1; spans are children from Release D).

The evidence shape in core/eval/answer.schema.json is the one Release A readers accept, so it cannot hold a parent_id.
The producer writes the link to the record instead, under "evidence_links": {"v": 1, "parents": {child id: post id}}.
The shape is version 1 as Release C wrote it; Release D adds span ids to the same map, and a Release C reader follows
only the comment ids in it. A record without it (stored before it existed, or by a reader that dropped it) is judged by
its address alone."""
import re

KEY = "evidence_links"
VERSION = 1
COMMENT_ID = re.compile(r"^[a-z0-9]+_comment_")  # a comment record's id: "<platform>_comment_<native>"
SPAN_ID = re.compile(r"_span_[0-9]+$")  # a transcript span's id: "<post id>_span_<n>"


def read(record):
    """{comment id: post id} from a record, or {} when the record holds no link or one that is not the producer's shape:
    exactly the keys v and parents, v the whole number 1, and every id a non-empty string."""
    links = record.get(KEY) if isinstance(record, dict) else None
    if not isinstance(links, dict) or set(links) != {"v", "parents"}:
        return {}
    version, parents = links["v"], links["parents"]
    if type(version) is not int or version != VERSION or not isinstance(parents, dict):
        return {}
    if any(not isinstance(k, str) or not k or not isinstance(v, str) or not v for k, v in parents.items()):
        return {}
    return dict(parents)


def attach(evidence, links):
    """evidence with each comment or span that holds no parent_id given the one the links name. Only those two are
    given one: a link that names any other record is not followed. The stored records are not changed; the copies are
    for judging, never for showing."""
    if not links or not isinstance(evidence, list):
        return evidence
    return [{**e, "parent_id": links[e["id"]]}
            if isinstance(e, dict) and e.get("parent_id") is None and isinstance(e.get("id"), str) and e["id"] in links
            and (COMMENT_ID.match(e["id"]) or SPAN_ID.search(e["id"]))
            else e for e in evidence]


def prune(links, shown_ids):
    """The links among the ids a reader is shown, in the record's shape, or None when there are none."""
    parents = {c: p for c, p in links.items() if c in shown_ids and p in shown_ids}
    return {"v": VERSION, "parents": parents} if parents else None
