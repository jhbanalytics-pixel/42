import copy
import xml.etree.ElementTree as ET

import pytest

from tests_support.ci_core_shards import partition, validate


def packets():
    nodes = [f"core/test_fixture.py::test_case[{i}]" for i in range(9)]
    return [dict(head="abc", inventory=nodes, index=i, count=4, nodes=nodes[i::4],
                 run_exit=0, collect_exit=0, guard_exit=0,
                 cases=[(n, ET.Element("testcase", name=n)) for n in nodes[i::4]]) for i in range(4)]


def test_round_robin_preserves_every_node_exactly_once():
    source = packets()[0]["inventory"]
    groups = [partition(source, i, 4) for i in range(4)]
    assert groups == [source[i::4] for i in range(4)]
    assert sorted(n for group in groups for n in group) == sorted(source)
    assert max(map(len, groups)) - min(map(len, groups)) <= 1
    assert len(validate(packets(), 4, "abc")) == len(source)


@pytest.mark.parametrize("fault", ["missing", "duplicate_shard", "head", "inventory", "wrong_node",
                                  "missing_case", "duplicate_case", "failed_exit", "guard", "collect"])
def test_aggregate_refuses_incomplete_or_mismatched_receipts(fault):
    records = copy.deepcopy(packets())
    if fault == "missing":
        records.pop()
    elif fault == "duplicate_shard":
        records[-1] = copy.deepcopy(records[0])
    elif fault == "head":
        records[0]["head"] = "other"
    elif fault == "inventory":
        records[0]["inventory"] = records[0]["inventory"][:-1]
    elif fault == "wrong_node":
        records[0]["cases"][0] = ("core/test_other.py::test_same_count", ET.Element("testcase"))
    elif fault == "missing_case":
        records[0]["cases"].pop()
    elif fault == "duplicate_case":
        records[0]["cases"].append(records[0]["cases"][0])
    elif fault == "failed_exit":
        records[0]["run_exit"] = 1
    elif fault == "guard":
        records[0]["guard_exit"] = 1
    else:
        records[0]["collect_exit"] = 2
    with pytest.raises(ValueError):
        validate(records, 4, "abc")


def test_assigned_node_tampering_is_rejected_even_with_matching_junit():
    records = packets()
    records[0]["nodes"] = [n for n, _ in records[0]["cases"]][::-1]
    with pytest.raises(ValueError):
        validate(records, 4, "abc")
