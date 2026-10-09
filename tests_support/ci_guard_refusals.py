"""Fail the CI job when the offline guard refused something the tests did not ask it to refuse.

usage: python tests_support/ci_guard_refusals.py <refusal-log.jsonl>

The guard writes one JSON line per refusal to the file named by CORE_OFFLINE_GUARD_LOG. Product code can catch the
OSError a refusal raises and carry on, so the test run stays green while it was trying to leave the machine. The
only refusals expected are the synthetic ones that core/eval/tests/harness/test_harness_offline_guard.py asks for
on purpose; those are listed and ignored. Any other refusal, a refusal during collection, or a line that cannot be
read as a refusal, exits 1. A missing or empty log means the guard refused nothing and exits 0.
"""
import collections
import json
import sys

OWN_TESTS = "core/eval/tests/harness/test_harness_offline_guard.py"


def read(path):
    try:
        with open(path, encoding="utf-8") as stream:
            lines = [line for line in stream.read().splitlines() if line.strip()]
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        valid = isinstance(row, dict) and all(isinstance(row.get(k), str) for k in ("event", "detail", "test"))
        rows.append(row if valid else None)
    return rows


def is_own(row):
    node = row["test"].replace("\\", "/").split("::", 1)[0]
    return node == OWN_TESTS


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 1
    try:
        rows = read(argv[1])
    except OSError as error:
        print(f"the refusal log at {argv[1]} cannot be read: {type(error).__name__}")
        return 1
    own = [row for row in rows if row is not None and is_own(row)]
    foreign = [row for row in rows if row is None or not is_own(row)]
    if not rows:
        print("The guard refused nothing.")
        return 0
    if own:
        print(f"{len(own)} from the guard's own tests (expected, ignored).")
    if not foreign:
        return 0
    print(f"The guard refused {len(foreign)} operations that no guard test asked for:")
    counts = collections.Counter(
        ("a line that is not a refusal",) if row is None else (row["test"], row["event"], row["detail"])
        for row in foreign)
    for key, count in counts.most_common():
        print(count, *key)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
