"""Candidate extraction is deterministic under input permutation and hash seeds."""

from __future__ import annotations

import ast
import json
import os
import random
import subprocess
import sys
from itertools import permutations
from pathlib import Path

import pytest
from src.analysis.open_intelligence import graph as graph_module
from src.analysis.open_intelligence.brain_contract import canonical_digest
from src.analysis.open_intelligence.candidates import Observation
from src.analysis.open_intelligence.graph import (
    PROVENANCE_CLUSTER_BUILD_VERSION,
    AnchoredGraphRules,
    GraphRules,
    build_edges,
    build_provenance_components,
    build_provenance_edges,
    build_relationship_votes,
)

ENGINE_ROOT = Path(__file__).resolve().parents[2]
HASH_SEEDS = ("0", "1", "4242")
# The project's own model client modules, discovered by path so the exclusion names the
# thing being excluded and nothing else: every client module directly under src/analysis.
MODEL_CLIENT_MODULES = tuple(
    sorted(
        f"src.analysis.{path.stem}"
        for path in (ENGINE_ROOT / "src" / "analysis").glob("*_client.py")
    )
)
_ORIGINS = {
    ("za", "budgeting"): ("socialcrawl", "reddit"),
    ("za", "commuting"): ("rss", "news"),
    ("za", "saving"): ("google_youtube", "youtube"),
    ("za", "taxi"): ("gdelt", "news"),
    ("za", "loadshedding"): ("google_trends", "search"),
    ("ng", "naira"): ("rss", "news"),
    ("ng", "fuel"): ("socialcrawl", "short_video"),
}
_CO_OCCURRENCE = {
    ("za", "budgeting"): ("commuting", "saving"),
    ("za", "commuting"): ("budgeting", "saving"),
    ("za", "saving"): ("budgeting", "commuting"),
    ("za", "taxi"): ("loadshedding",),
    ("za", "loadshedding"): ("taxi",),
    ("ng", "naira"): ("fuel",),
    ("ng", "fuel"): ("naira",),
}
_WEIGHTS = {("za", "budgeting"): 3.0, ("za", "taxi"): 2.0, ("ng", "naira"): 2.0}
PAIRS = (
    ("za|keyword|budgeting", "za|keyword|commuting"),
    ("za|keyword|budgeting", "za|keyword|saving"),
    ("za|keyword|commuting", "za|keyword|saving"),
    ("za|keyword|loadshedding", "za|keyword|taxi"),
    ("ng|keyword|fuel", "ng|keyword|naira"),
)
SCORES = dict.fromkeys(PAIRS, 0.9)


def _envelope(market: str, term: str) -> dict:
    vendor, channel = _ORIGINS[(market, term)]
    return {
        "contract_version": "candidate_source_provenance_v1",
        "coverage_basis": "sampled_record_support",
        "source_snapshot_digest": "a" * 64,
        "resolution_state": "resolved",
        "pairs": (
            {
                "vendor_family": vendor,
                "channel_family": channel,
                "source_table": "enriched_content",
                "sample_refs": ({"row_id": f"row_{market}_{term}", "row_digest": "b" * 64},),
            },
        ),
        "unresolved_sample_refs": (),
    }


def observations() -> tuple[Observation, ...]:
    return tuple(
        Observation(
            market,
            term,
            "keyword",
            (f"row_{market}_{term}",),
            source_families=(_ORIGINS[(market, term)][1],),
            platforms=(_ORIGINS[(market, term)][1],),
            observed_dates=("2026-09-06",),
            co_occurring_terms=_CO_OCCURRENCE[(market, term)],
            weight=_WEIGHTS.get((market, term), 1.0),
            verified_search_spike=term == "loadshedding",
        )
        for market, term in _ORIGINS
    )


def provenance() -> dict:
    return {f"{market}|keyword|{term}": _envelope(market, term) for market, term in _ORIGINS}


def rules(component_member_ceiling: int = 10) -> AnchoredGraphRules:
    return AnchoredGraphRules(0.7, 3, 100, component_member_ceiling)


def extract(items, *, seed: int | None = None, ceiling: int = 10):
    ordered = list(items)
    pairs = list(PAIRS)
    if seed is not None:
        random.Random(seed).shuffle(ordered)
        random.Random(seed + 1).shuffle(pairs)
    edges = build_provenance_edges(
        ordered, pairs, SCORES, rules(ceiling), source_provenance_by_member=provenance()
    )
    components = build_provenance_components(
        ordered, pairs, SCORES, rules(ceiling), source_provenance_by_member=provenance()
    )
    return edges, components


def digests(edges, components) -> dict[str, str]:
    membership = [
        {
            "market": component.market,
            "members": list(component.member_identities),
            "label": component.label,
            "label_member_identity": component.label_member_identity,
            "row_receipts": list(component.row_receipts),
            "source_families": list(component.source_families),
        }
        for component in components
    ]
    receipts = [
        {
            "market": edge.market,
            "left": edge.left_identity,
            "right": edge.right_identity,
            "votes": {name: getattr(edge.votes, name) for name in edge.votes.__dataclass_fields__},
            "score": edge.semantic_similarity,
        }
        for edge in edges
    ]
    return {
        "membership": canonical_digest(membership),
        "labels": canonical_digest([component.label for component in components]),
        "graph": canonical_digest({"components": membership, "edges": receipts}),
        "version": canonical_digest(
            {
                "build_version": PROVENANCE_CLUSTER_BUILD_VERSION,
                "component_versions": [component.build_version for component in components],
            }
        ),
        "edge_receipts": canonical_digest(receipts),
    }


def baseline() -> dict[str, str]:
    return digests(*extract(observations()))


def test_memberships_labels_and_digests_survive_input_permutation():
    expected = baseline()
    items = observations()
    edges, components = extract(items)
    assert [component.label for component in components] == ["naira", "budgeting", "taxi"]
    assert len(edges) == 5
    for seed in range(12):
        assert digests(*extract(items, seed=seed)) == expected
    for ordering in permutations(items[:3]):
        assert digests(*extract((*ordering, *items[3:]))) == expected


def test_extraction_is_identical_across_hash_seeds(tmp_path):
    expected = baseline()
    script = tmp_path / "extract.py"
    script.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {str(ENGINE_ROOT)!r})\n"
        f"sys.path.insert(0, {str(ENGINE_ROOT / 'tests' / 'unit')!r})\n"
        "import test_dynamic_signal_determinism as module\n"
        "seed = int(sys.argv[1])\n"
        "edges, components = module.extract(module.observations(), seed=seed)\n"
        "loaded = sorted(name for name in sys.modules if name in module.MODEL_CLIENT_MODULES)\n"
        "print(json.dumps({'digests': module.digests(edges, components), 'clients': loaded}))\n",
        encoding="utf-8",
    )
    for index, hash_seed in enumerate(HASH_SEEDS):
        completed = subprocess.run(
            [sys.executable, str(script), str(index)],
            capture_output=True,
            cwd=str(ENGINE_ROOT),
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            check=False,
        )
        assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
        payload = json.loads(completed.stdout.decode("utf-8"))
        assert payload["digests"] == expected, hash_seed
        assert payload["clients"] == [], hash_seed


def test_merged_and_split_candidates_keep_bounded_coherence_and_lineage():
    items = observations()
    edges, components = extract(items)
    by_label = {component.label: component for component in components}
    merged = by_label["budgeting"]
    assert merged.member_identities == (
        "za|keyword|budgeting",
        "za|keyword|commuting",
        "za|keyword|saving",
    )
    assert merged.row_receipts == ("row_za_budgeting", "row_za_commuting", "row_za_saving")
    split = by_label["taxi"]
    assert split.member_identities == ("za|keyword|loadshedding", "za|keyword|taxi")
    assert split.row_receipts == ("row_za_loadshedding", "row_za_taxi")
    assert not set(merged.member_identities) & set(split.member_identities)
    observed = {f"{item.market}|keyword|{item.term}": item for item in items}
    for component in components:
        assert component.build_version == PROVENANCE_CLUSTER_BUILD_VERSION
        assert component.label_member_identity in component.member_identities
        assert observed[component.label_member_identity].term == component.label
        assert len(component.member_identities) <= rules().component_member_ceiling
        assert component.row_receipts == tuple(
            sorted(
                row for member in component.member_identities for row in observed[member].row_ids
            )
        )
        assert any(
            edge.left_identity in component.member_identities
            and edge.right_identity in component.member_identities
            for edge in edges
        )
    assert digests(*extract(items, ceiling=3)) == digests(edges, components)
    with pytest.raises(ValueError, match="composition_component_ceiling_exceeded"):
        extract(items, ceiling=2)


def test_two_relationship_rule_and_observed_labels_are_retained():
    left, right = observations()[:2]
    votes = build_relationship_votes(
        left, right, semantic_similarity=0.0, rules=GraphRules(0.7, 0.55, 2)
    )
    assert votes.qualifying_count >= 2
    lonely = Observation(
        "za",
        "isolated",
        "keyword",
        ("row_isolated",),
        source_families=("news",),
        platforms=("news",),
        observed_dates=("2026-01-01",),
    )
    assert build_edges((left, lonely), {}, GraphRules(0.7, 0.55, 2)) == ()
    edges, components = extract(observations())
    assert all(edge.votes.qualifying_count >= 2 for edge in edges)
    assert {component.label for component in components} <= {item.term for item in observations()}


def test_no_model_client_reaches_the_extraction_path():
    assert MODEL_CLIENT_MODULES, "the exclusion must name at least one client module"
    package = ENGINE_ROOT / "src" / "analysis" / "open_intelligence"
    for module in ("graph", "candidates", "composition", "source_provenance"):
        tree = ast.parse((package / f"{module}.py").read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        offending = [name for name in imported if name in MODEL_CLIENT_MODULES]
        assert offending == [], module
    assert not any(
        getattr(value, "__name__", None) in MODEL_CLIENT_MODULES
        for value in vars(graph_module).values()
    )
