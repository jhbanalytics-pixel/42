"""Summarise a pytest JUnit file for the CI job summary: how many tests ran, passed, failed and were skipped, and
the skipped ones by reason, with the native opt-in tests named apart.

usage: python tests_support/ci_skip_report.py <junit.xml>

Prints Markdown. Exits 1 when the file is missing, unreadable or holds no test, so a crashed collection cannot read
as a clean run with nothing skipped. The workflow, not this script, keeps the opt-in switches off.
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


def main(argv):
    if len(argv) != 2:
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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
