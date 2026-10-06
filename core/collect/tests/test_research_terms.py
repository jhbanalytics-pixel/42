from collections import Counter

import pytest
import yaml

from core.collect import research_terms


FIELDS = {"term", "market", "source", "source_urls", "active", "location_evidence"}


def test_research_manifest_has_120_active_terms_with_only_approved_fields():
    rows = research_terms.load_terms()

    assert len(rows) == 120
    assert Counter(row["market"] for row in rows) == {"ZA": 40, "NG": 40, "KE": 40}
    assert all(set(row) == FIELDS for row in rows)
    assert all(row["source"] == "research_r2" and row["active"] is True for row in rows)
    assert all(row["location_evidence"] is False for row in rows)
    assert all(row["source_urls"] and all(url.startswith("https://") for url in row["source_urls"])
               for row in rows)


def test_market_filter_uses_explicit_market_without_inferring_from_term(tmp_path):
    rows = [{"term": "Lagos", "market": "KE", "source": "research_r2",
             "source_urls": ["https://example.com/"], "active": True, "location_evidence": False}]
    path = tmp_path / "terms.yaml"
    path.write_text(yaml.safe_dump(rows, sort_keys=False), encoding="utf-8")

    assert research_terms.load_terms("KE", path) == rows
    assert research_terms.load_terms("NG", path) == []


def test_duplicate_terms_are_deduplicated_by_casefold_within_each_market(tmp_path):
    rows = [
        {"term": "M-Pesa", "market": "KE", "source": "research_r2",
         "source_urls": ["https://example.com/first"], "active": True, "location_evidence": False},
        {"term": "m-pesa", "market": "KE", "source": "research_r2",
         "source_urls": ["https://example.com/second"], "active": True, "location_evidence": False},
        {"term": "M-Pesa", "market": "ZA", "source": "research_r2",
         "source_urls": ["https://example.com/za"], "active": True, "location_evidence": False},
    ]
    path = tmp_path / "terms.yaml"
    path.write_text(yaml.safe_dump(rows, sort_keys=False), encoding="utf-8")

    loaded = research_terms.load_terms(path=path)

    assert [(row["term"], row["market"]) for row in loaded] == [("M-Pesa", "KE"), ("M-Pesa", "ZA")]
    assert loaded[0]["source_urls"] == ["https://example.com/first"]


@pytest.mark.parametrize("invalid", [
    {"term": "", "market": "ZA", "source": "research_r2", "source_urls": ["https://example.com/"],
     "active": True, "location_evidence": False},
    {"term": "Lagos", "market": "NG", "source": "other", "source_urls": ["https://example.com/"],
     "active": True, "location_evidence": False},
    {"term": "Lagos", "market": "NG", "source": "research_r2", "source_urls": ["http://example.com/"],
     "active": True, "location_evidence": False},
    {"term": "Lagos", "market": "NG", "source": "research_r2", "source_urls": ["https://example.com/"],
     "active": 1, "location_evidence": False},
    {"term": "Lagos", "market": "NG", "source": "research_r2", "source_urls": ["https://example.com/"],
     "active": True, "location_evidence": True},
    {"term": "Lagos", "market": "NG", "source": "research_r2", "source_urls": ["https://example.com/"],
     "active": True, "location_evidence": False, "age": 27},
])
def test_loader_rejects_malformed_or_unapproved_records(tmp_path, invalid):
    path = tmp_path / "terms.yaml"
    path.write_text(yaml.safe_dump([invalid], sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError):
        research_terms.load_terms(path=path)
