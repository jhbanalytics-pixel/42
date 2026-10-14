"""Run and reconcile complete pytest node inventories across independent CI runners."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def partition(nodes, index, count):
    if count < 1 or not 0 <= index < count or not nodes or len(nodes) != len(set(nodes)):
        raise ValueError("invalid shard or duplicate/empty inventory")
    return nodes[index::count]


def digest(nodes):
    return hashlib.sha256(json.dumps(nodes, separators=(",", ":")).encode()).hexdigest()


def validate(records, count, head):
    if len(records) != count or {r["index"] for r in records} != set(range(count)):
        raise ValueError("missing or duplicate shard")
    inventory = records[0]["inventory"]
    if inventory != sorted(inventory) or not all(isinstance(n, str) and n for n in inventory):
        raise ValueError("invalid inventory")
    combined = []
    for record in records:
        if record["head"] != head or record["count"] != count or record["inventory"] != inventory:
            raise ValueError("head or full inventory mismatch")
        expected = partition(inventory, record["index"], count)
        if record["nodes"] != expected:
            raise ValueError("assigned node list mismatch")
        if any(type(record[k]) is not int or record[k] != 0 for k in ("collect_exit", "run_exit", "guard_exit")):
            raise ValueError("collection, pytest or guard failed")
        actual = [node for node, _ in record["cases"]]
        if len(actual) != len(set(actual)) or set(actual) != set(expected):
            raise ValueError("JUnit omitted, duplicated or substituted a node")
        if any(case.find("failure") is not None or case.find("error") is not None for _, case in record["cases"]):
            raise ValueError("JUnit records a failed test")
        combined.extend(record["cases"])
    if sorted(node for node, _ in combined) != inventory:
        raise ValueError("aggregate does not cover every collected node exactly once")
    return combined


class Inventory:
    def __init__(self, selected=None):
        self.nodes = []
        self.selected = selected

    def pytest_collection_finish(self, session):
        self.nodes = sorted(item.nodeid for item in session.items)
        if self.selected is not None and self.nodes != sorted(self.selected):
            raise ValueError("run collection differs from assigned nodes")
        for item in session.items:
            item.user_properties.append(("ci_nodeid", item.nodeid))


def run(root, index, count, output):
    import pytest
    from tests_support import ci_guard_refusals

    output.mkdir(parents=True, exist_ok=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], encoding="utf-8").strip()
    collector = Inventory()
    collected = int(pytest.main([root, "--collect-only", "-q", "-p", "no:cacheprovider"], plugins=[collector]))
    inventory = collector.nodes
    receipt = dict(head=head, inventory=inventory, inventory_sha256=digest(inventory), index=index, count=count,
                   nodes=[], collect_exit=collected, run_exit=5, guard_exit=1)
    if collected == 0:
        receipt["nodes"] = partition(inventory, index, count)
        if not receipt["nodes"]:
            raise ValueError("shard has no assigned nodes")
        receipt["run_exit"] = int(pytest.main(
            [*receipt["nodes"], "-q", "-p", "no:cacheprovider", "-ra", f"--junitxml={output / 'junit.xml'}"],
            plugins=[Inventory(receipt["nodes"])],
        ))
    log = os.environ["CORE_OFFLINE_GUARD_LOG"]
    receipt["guard_exit"] = ci_guard_refusals.main(["ci_guard_refusals.py", log])
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    (output / "offline-guard.jsonl").write_bytes(Path(log).read_bytes() if Path(log).exists() else b"")
    return max(receipt["collect_exit"], receipt["run_exit"], receipt["guard_exit"])


def read_packet(path):
    from tests_support import ci_guard_refusals

    packet = json.loads(path.read_text(encoding="utf-8"))
    if packet.get("inventory_sha256") != digest(packet["inventory"]):
        raise ValueError("inventory digest mismatch")
    guard = path.parent / "offline-guard.jsonl"
    if not guard.is_file() or ci_guard_refusals.main(["ci_guard_refusals.py", str(guard)]) != packet["guard_exit"]:
        raise ValueError("guard artifact missing or inconsistent")
    cases = []
    for case in ET.parse(path.parent / "junit.xml").getroot().iter("testcase"):
        node = [p.get("value") for p in case.findall("./properties/property") if p.get("name") == "ci_nodeid"]
        if len(node) != 1 or not node[0]:
            raise ValueError("JUnit testcase has no unique node identity")
        cases.append((node[0], case))
    packet["cases"] = cases
    return packet


def aggregate(artifacts, count, head, junit):
    records = [read_packet(p) for p in sorted(artifacts.rglob("receipt.json"))]
    cases = validate(records, count, head)
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", name="core", tests=str(len(cases)), failures="0", errors="0",
                          skipped=str(sum(c.find("skipped") is not None for _, c in cases)))
    for _, case in sorted(cases, key=lambda entry: entry[0]):
        suite.append(copy.deepcopy(case))
    junit.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(junit, encoding="utf-8", xml_declaration=True)
    print(f"Every collected core node accounted exactly once: {len(cases)} across {count} shards at {head}")
    return 0


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("run")
    worker.add_argument("--root", required=True)
    worker.add_argument("--shard", type=int, required=True)
    worker.add_argument("--count", type=int, required=True)
    worker.add_argument("--out", type=Path, required=True)
    merge = commands.add_parser("aggregate")
    merge.add_argument("--artifacts", type=Path, required=True)
    merge.add_argument("--count", type=int, required=True)
    merge.add_argument("--head", required=True)
    merge.add_argument("--junit", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "run":
            return run(args.root, args.shard, args.count, args.out)
        return aggregate(args.artifacts, args.count, args.head, args.junit)
    except (OSError, ValueError, KeyError, ET.ParseError) as error:
        print(f"Incomplete core shard evidence: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main())
