"""The committed source estate document is the generated one, byte for byte.

``docs/operations/source-estate.md`` sits at the repository root, which the
engine build context does not carry, so the comparison lives here, where the
publish build runs it over the checkout, rather than in ``tests/unit``, which
the engine test image runs over its own copy of the engine tree. A missing
document fails this test: it is never a skip. The content checks on the
generated document stay in ``tests/unit/test_source_inventory.py``.
"""

from __future__ import annotations

from pathlib import Path

from src.analysis.open_intelligence.source_inventory import (
    SOURCE_ESTATE_DOC,
    source_estate_document,
)

REPOSITORY = Path(__file__).resolve().parents[3]
SOURCE_ESTATE_FILE = REPOSITORY / "docs" / "operations" / "source-estate.md"


def test_the_committed_source_estate_document_is_the_generated_one():
    assert SOURCE_ESTATE_DOC == SOURCE_ESTATE_FILE
    assert SOURCE_ESTATE_FILE.is_file(), f"{SOURCE_ESTATE_FILE.relative_to(REPOSITORY)} is missing"
    assert SOURCE_ESTATE_FILE.read_text(encoding="utf-8") == source_estate_document()
