"""Summarise a pytest JUnit file for the CI job summary: how many tests ran, passed, failed and were skipped, and
the skipped ones by reason, with the native opt-in tests named apart.

usage: python tests_support/ci_skip_report.py <junit.xml> [--min-total N] [--min-passed N [--ratchet-gap G]]

Prints Markdown. Exits 1 when the file is missing, unreadable or holds no test, so a crashed collection cannot read
as a clean run with nothing skipped. With --min-total N it also exits 1 when fewer than N tests are in the file, so a
run that quietly collected less than the tree holds cannot pass. With --min-passed N it exits 1 when fewer than N
tests passed: skipped, expected-failure, failed and errored tests do not count, so a run that skips most of the tree
cannot meet it however many tests it collected. With --ratchet-gap G (it needs --min-passed) it also exits 1 when more
than G tests passed above the floor, which makes the floor follow the tree up instead of staying where it was pinned.
The workflow, not this script, keeps the opt-in switches off.
"""
import collections
import sys
import xml.etree.ElementTree as ET

# (label, substrings of the skip reason). The first match wins.
OPT_IN = (
    ("live BigQuery dry runs (F42_BQ=1)", ("F42_BQ",)),
    ("packaged Linux browser and boundary image (PDF_RENDERER_NATIVE=1)", ("PDF_RENDERER_NATIVE", "final Linux image")),
)


def classify(reason):
    for label, needles in OPT_IN:
        if any(needle in reason for needle in needles):
            return label
    return None


def read(path):
    cases = []
    for case in ET.parse(path).getroot().iter("testcase"):
        outcome, reason = "passed", ""
        for child in case:
            if child.tag in ("failure", "error"):
                outcome, reason = "failed", child.get("message") or ""
            elif child.tag == "skipped":
                outcome, reason = "skipped", child.get("message") or ""
        cases.append((f"{case.get('classname')}::{case.get('name')}", outcome, reason))
    return cases


def report(cases):
    counts = collections.Counter(outcome for _, outcome, _ in cases)
    opt_in = collections.Counter()
    other = collections.Counter()
    for _, outcome, reason in cases:
        if outcome != "skipped":
            continue
        label = classify(reason)
        if label:
            opt_in[label] += 1
        else:
            other[reason.strip()[:140] or "no reason given"] += 1
    lines = ["### Core test run", "",
             f"{len(cases)} tests: {counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped.", "",
             "Native opt-in tests are never run in this workflow. They are skipped and counted here:", ""]
    if opt_in:
        lines += [f"- {n} skipped: {label}" for label, n in sorted(opt_in.items())]
    else:
        lines.append("- none were skipped for an opt-in reason (this is unexpected: check the skip reasons)")
    lines += ["", "Other skips, by reason:", ""]
    lines += [f"- {n}: {reason}" for reason, n in other.most_common()] or ["- none"]
    return "\n".join(lines) + "\n"


FLAGS = {"--min-total": "total", "--min-passed": "passed", "--ratchet-gap": "gap"}


def options_from(argv):
    """The floors named after the JUnit path as a dict, or None when the arguments are not usage. Each flag takes one
    positive whole number and may appear once; `--ratchet-gap` needs `--min-passed`."""
    options, rest = {}, argv[2:]
    if len(rest) % 2:
        return None
    for flag, value in zip(rest[::2], rest[1::2]):
        key = FLAGS.get(flag)
        if key is None or key in options or not value.isdigit() or int(value) <= 0:
            return None
        options[key] = int(value)
    if "gap" in options and "passed" not in options:
        return None
    return options


def main(argv):
    options = options_from(argv) if len(argv) >= 2 else None
    if options is None:
        print(__doc__)
        return 1
    try:
        cases = read(argv[1])
    except (OSError, ET.ParseError) as error:
        print(f"no readable JUnit file at {argv[1]}: {type(error).__name__}")
        return 1
    if not cases:
        print("the JUnit file holds no test")
        return 1
    print(report(cases))
    messages = []
    floor = options.get("total", 0)
    if len(cases) < floor:
        messages.append(f"{len(cases)} tests ran: fewer tests than the floor of {floor}")
    passed = sum(1 for _, outcome, _ in cases if outcome == "passed")
    if "passed" in options:
        if passed < options["passed"]:
            messages.append(f"{passed} tests passed: fewer than the passed floor of {options['passed']}")
        elif "gap" in options and passed - options["passed"] > options["gap"]:
            messages.append(f"{passed} tests passed: {passed - options['passed']} above the passed floor of "
                            f"{options['passed']}, more than the {options['gap']} allowed; raise the floor")
    for message in messages:
        print(message)
        print(message, file=sys.stderr)
    return 1 if messages else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
