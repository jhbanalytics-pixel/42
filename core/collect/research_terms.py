from pathlib import Path
from urllib.parse import urlsplit

import yaml


FIELDS = ("term", "market", "source_urls")
METADATA_FIELDS = ("source", "active", "location_evidence")
SOURCES = ("research_r2",)
MARKETS = ("ZA", "NG", "KE")
MANIFEST = Path(__file__).resolve().parents[1] / "config" / "research_search_seeds.yaml"


def load_terms(market=None, path=MANIFEST):
    if market is not None and market not in MARKETS:
        raise ValueError("unknown market")

    records = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError("research term manifest must be a list")

    seen = set()
    normalized = []
    for record in records:
        if (not isinstance(record, dict) or not set(FIELDS).issubset(record)
                or not set(record).issubset(FIELDS + METADATA_FIELDS)):
            raise ValueError("research term records must contain term, market, source_urls and approved metadata only")
        if not isinstance(record["term"], str) or not record["term"].strip():
            raise ValueError("research term must be a non-empty string")
        if not isinstance(record["market"], str) or record["market"] not in MARKETS:
            raise ValueError("research term has an unknown market")
        if record.get("source") not in SOURCES:
            raise ValueError("research term has an unknown source")
        if not isinstance(record.get("active"), bool):
            raise ValueError("research term active field must be a boolean")
        if record.get("location_evidence") is not False:
            raise ValueError("research terms cannot be location evidence")
        urls = record["source_urls"]
        if (not isinstance(urls, list) or not urls or any(
                not isinstance(url, str) or not url.strip() or urlsplit(url).scheme != "https"
                or not urlsplit(url).hostname for url in urls)):
            raise ValueError("research term source_urls must be a non-empty list of https URLs")

        key = record["market"], record["term"].casefold()
        if key in seen:
            continue
        seen.add(key)
        if market is None or record["market"] == market:
            normalized.append({
                "term": record["term"],
                "market": record["market"],
                "source": record["source"],
                "source_urls": list(urls),
                "active": record["active"],
                "location_evidence": False,
            })
    return normalized
