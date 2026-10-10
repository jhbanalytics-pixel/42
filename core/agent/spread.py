"""Order items so that any first part of the list is spread across platforms and creators.

Ask uses it twice: the topic sweep, to choose which of its candidate posts to keep, and the writer's pack, to decide
which post blocks are dropped when the pack is full. Strength says how well an item matches the question; spread says
that no one platform or creator crowds out the rest. Neither changes what a claim may cite: the checks read the posts
themselves.
"""

from __future__ import annotations

from typing import Callable, Hashable, Sequence, TypeVar

T = TypeVar("T")
PER_CREATOR = 3


def spread_order(items: Sequence[T], *, platform: Callable[[T], Hashable], creator: Callable[[T], Hashable],
                 strength: Callable[[T], float], per_creator: int = PER_CREATOR) -> list[T]:
    """Every item once. Within a platform the strongest comes first. The platforms take turns, the one whose best
    remaining item is strongest first, and in a turn a platform gives its strongest item whose creator has not yet
    been given per_creator places. Items over that cap follow all the others, strongest first. Equal strength keeps
    the order given."""
    ranked = sorted(range(len(items)), key=lambda i: (-strength(items[i]), i))
    queues: dict[Hashable, list[int]] = {}
    for i in ranked:
        queues.setdefault(platform(items[i]), []).append(i)
    placed: dict[Hashable, int] = {}
    out: list[int] = []
    while True:
        turn = sorted((p for p, q in queues.items() if q), key=lambda p: (-strength(items[queues[p][0]]), queues[p][0]))
        gave = False
        for p in turn:
            for pos, i in enumerate(queues[p]):
                who = creator(items[i])
                if who is None or placed.get(who, 0) < per_creator:
                    out.append(i)
                    if who is not None:
                        placed[who] = placed.get(who, 0) + 1
                    del queues[p][pos]
                    gave = True
                    break
        if not gave:
            break
    rest = sorted(i for q in queues.values() for i in q)
    out += sorted(rest, key=lambda i: (-strength(items[i]), i))
    return [items[i] for i in out]
