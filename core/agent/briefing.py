"""The ranked briefing of a stored answer, read at read time and never stored (standard library only, like answer_view).

A question about which stories lead is answered with a group of claims per story. A story is the claims that start
with the same plain name and a colon ("Soweto unrest: ..."). The stored answer has no field for it and gains none, so
a Release C reader sees the same claims it always read. The briefing holds ids and indexes into the answer and no text
of its own: every word and every figure a reader sees is a stored claim's, with the checks it passed.

What a story's size is. One claim of the story pins its posts and its creators to the same query (and its platforms
when that query gives them). A claim that pins posts alone, such as a peak, is not a size. Two claims that both look
like the size leave it open, and so does a claim whose posts and creators come from different queries. Nothing is
filled in for a part the answer does not carry.

What the order is. The stories are in the order the writer gave. They are marked ordered only when every story has a
size and the sizes do not rise from one story to the next by creators and then posts. That is a check of the pinned
figures against each other, not proof that the queries behind them share a window: the writer is asked to rank from the
whole-store rows, and a reader of this file cannot see the queries."""

from __future__ import annotations

import re

KIND = "ranked_stories"
SIZE_UNITS = ("posts", "creators", "platforms")
_STORY_KINDS = ("observation", "interpretation")
_PREFIX = re.compile(r"^(?P<name>[^:\n]{1,60}): (?P<rest>\S.*)$", re.S)
_NOT_A_NAME = re.compile(r"[\d#@/\\<>\[\]{}()|\"]")
_MAX_NAME_WORDS = 8
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def story_name(text) -> str | None:
    """The plain story name a claim's text starts with, or None. A name is a few words with no digit, handle, tag, link
    or bracket, followed by a colon and a space and then the claim's own sentence."""
    match = _PREFIX.match(text) if isinstance(text, str) else None
    if not match:
        return None
    name = match["name"]
    if name != name.strip() or _NOT_A_NAME.search(name) or len(name.split()) > _MAX_NAME_WORDS:
        return None
    return name if any(char.isalpha() for char in name) else None


def _key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _pinned(number) -> bool:
    return (isinstance(number, dict) and isinstance(number.get("value"), (int, float))
            and not isinstance(number.get("value"), bool) and number["value"] > 0
            and isinstance(number.get("query_id"), str) and number["query_id"] != ""
            and isinstance(number.get("run_id"), str) and number["run_id"] != ""
            and isinstance(number.get("result_hash"), str) and bool(_HASH.match(number["result_hash"])))


def _sizes(claim: dict) -> list[dict]:
    """The sizes one claim pins: for each query that gives it both a posts figure and a creators figure, the index of
    the first of each (and of a platforms figure from that same query)."""
    first: dict = {}
    for index, number in enumerate(claim.get("numbers") or []):
        unit = str(number.get("unit") or "").strip().casefold() if isinstance(number, dict) else ""
        if unit in SIZE_UNITS and _pinned(number):
            first.setdefault((number["query_id"], unit), index)
    found = []
    for query in dict.fromkeys(q for q, _ in first):
        if (query, "posts") in first and (query, "creators") in first:
            units = [unit for unit in SIZE_UNITS if (query, unit) in first]
            found.append({"claim_id": claim["id"], "query_id": query,
                          "figures": [{"unit": unit, "index": first[(query, unit)]} for unit in units]})
    return found


def _size(story_claims: list[dict]) -> dict | None:
    found = [size for claim in story_claims for size in _sizes(claim)]
    if len(found) != 1:
        return None
    return {"claim_id": found[0]["claim_id"], "figures": found[0]["figures"]}


def _amount(claims: dict, size: dict, unit: str):
    figure = next(f for f in size["figures"] if f["unit"] == unit)
    return claims[size["claim_id"]]["numbers"][figure["index"]]["value"]


def stories(answer) -> dict | None:
    """The briefing of an answer, or None when it names fewer than two stories. The answer is not changed."""
    claims = [c for c in answer.get("claims") or [] if isinstance(c, dict) and isinstance(c.get("id"), str)] \
        if isinstance(answer, dict) else []
    groups: dict = {}
    for claim in claims:
        name = story_name(claim.get("text")) if claim.get("kind") in _STORY_KINDS else None
        if name is not None:
            group = groups.setdefault(_key(name), {"name": name, "claims": []})
            group["claims"].append(claim)
    if len(groups) < 2:
        return None
    by_id = {c["id"]: c for c in claims}
    out = []
    for rank, group in enumerate(groups.values(), start=1):
        out.append({"rank": rank, "name": group["name"], "claim_ids": [c["id"] for c in group["claims"]],
                    "size": _size(group["claims"]), "so_what": []})
    owner = {cid: story for story in out for cid in story["claim_ids"]}
    for index, line in enumerate(answer.get("so_what") or []):
        ids = line.get("claim_ids") if isinstance(line, dict) else None
        if isinstance(ids, list) and ids and all(isinstance(i, str) for i in ids):
            owners = {id(owner[i]) for i in ids if i in owner}
            if len(owners) == 1 and all(i in owner for i in ids):
                owner[ids[0]]["so_what"].append(index)
    sizes = [(_amount(by_id, s["size"], "creators"), _amount(by_id, s["size"], "posts")) if s["size"] else None
             for s in out]
    ordered = None not in sizes and all(a >= b for a, b in zip(sizes, sizes[1:]))
    return {"kind": KIND, "ordered": ordered, "stories": out,
            "other_claim_ids": [c["id"] for c in claims if c["id"] not in owner]}
