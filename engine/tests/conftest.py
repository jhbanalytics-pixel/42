"""Shared pytest fixtures."""

import importlib
import logging
import sys
from pathlib import Path

import pytest

# Win+Py3.13 SIGSEGV guard. CPython 3.13's LogRecord.__init__ sets taskName via
# asyncio.current_task() when logging.logAsyncioTasks is on; under pytest with
# asyncio loaded, that C call faults with an access violation on Windows the
# moment any test emits a log record (e.g. a non-fatal logger.error). Disabling
# the unused taskName enrichment removes the faulting path. Guarded to win32 so
# the Ubuntu CI runner is byte-identical and no test is weakened.
if sys.platform == "win32":
    logging.logAsyncioTasks = False


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure tests run in dev mode with a test GCP project."""
    monkeypatch.setenv("TRENDS_ENV", "dev")
    monkeypatch.setenv("GCP_PROJECT", "ogilvy-trends-v2-test")
    monkeypatch.setenv("BIGQUERY_DATASET", "trends_v2")


# Stages that fire Vertex Gemini calls and flush a gemini_usage ledger row.
# Each binds ``persist_gemini_usage`` at import, so the fixture below rebinds it
# per module rather than patching the source.
_GEMINI_USAGE_WRITERS = (
    "src.analysis.generate_briefs",
    "src.analysis.generate_daily_summary",
    "src.analysis.generate_seed_intelligence",
    "src.analysis.event_ledger",
    "src.analysis.comment_sentiment",
    "src.scoring.driving_hashtags",
)


@pytest.fixture(autouse=True)
def _no_live_gemini_usage_write(monkeypatch):
    """No unit test writes the gemini_usage ledger to BigQuery.

    The ledger write is a SECOND BigQuery write, to a different table than the
    stage's own output, and it resolves ``insert_dataframe`` from
    src.utils.bigquery. Tests that patch the stage module's own
    ``insert_dataframe`` therefore never covered it, so wiring the ledger into
    these stages silently pointed a dozen existing tests at live BigQuery. On
    this machine that means a real network call that stalls for minutes on the
    IPv6 fault before ``persist_gemini_usage`` swallows the timeout, which reads
    as a hung suite rather than as a test doing something it should not.

    A test that wants to assert on ledger rows passes its own ``usage_sink``,
    which takes precedence over this default and is unaffected.
    """
    for module_path in _GEMINI_USAGE_WRITERS:
        module = importlib.import_module(module_path)
        if hasattr(module, "persist_gemini_usage"):
            monkeypatch.setattr(module, "persist_gemini_usage", lambda rows: len(rows))


@pytest.fixture(autouse=True)
def _reset_youtube_search_cache():
    """Clear the YouTube connector's per-process search cache between tests.

    The cache prevents double-charging quota when the same (market, term) is
    fetched twice in a single pipeline run. Across tests in one pytest
    process, it would leak results from one test into the next because the
    cache key only includes published_after, which is often identical within
    the same second.
    """
    from src.ingestion.connectors import youtube as _youtube_module

    _youtube_module._SEARCH_CACHE.clear()
    _youtube_module._HANDLE_CACHE.clear()
    yield
    _youtube_module._SEARCH_CACHE.clear()
    _youtube_module._HANDLE_CACHE.clear()


@pytest.fixture
def test_data_dir() -> Path:
    """Path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_rss_feed_xml() -> str:
    """Minimal valid RSS 2.0 feed XML with two entries."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <link>https://example.com</link>
    <description>Test RSS feed</description>
    <item>
      <title>AI trends in South Africa</title>
      <link>https://example.com/article/1</link>
      <description>Summary of the first article about AI trends.</description>
      <author>test.author@example.com</author>
      <pubDate>Mon, 14 Apr 2026 10:00:00 +0200</pubDate>
    </item>
    <item>
      <title>Tech news from Nigeria</title>
      <link>https://example.com/article/2</link>
      <description>Summary of the second article.</description>
      <pubDate>Mon, 14 Apr 2026 09:00:00</pubDate>
    </item>
  </channel>
</rss>"""
