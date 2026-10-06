"""Guard the shared runtime flag file and the staging build boundary.

Regression for the 10 Jun incident: the old resend-email-only.yml had no
MAILER_V2_ENABLED, send_daily_digest fell back to the legacy v1 layout, and the
wrong digest reached the full exec list. The fix put the flags in one file that
every path consumes.

The imported staging repository carries a build recipe only. It must preserve
the flag source without reading, deploying or updating any production job.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_FLAGS_FILE = _ROOT / "configs" / "cron_flags.env"
_CLOUDBUILD = _ROOT / "cloudbuild.staging.yaml"

# Flags that change WHAT the recipient sees when an email renders.
_RENDER_FLAGS = ["MAILER_V2_ENABLED"]

# The full feature-flag set the cron paths must carry.
_DURABLE_FLAGS = [
    "MAILER_V2_ENABLED",
    "FORECAST_ENABLED",
    "COMMENT_SENTIMENT_ENABLED",
    "DRIVING_HASHTAGS_ENABLED",
    "EMBEDDING_CLASSIFIER_ENABLED",
    "LANGUAGE_GUARD_ENABLED",
    "SEARCH_VELOCITY_ENABLED",
    "SEED_GRAPH_ENABLED",
    "NEAR_MISS_CAPTURE_ENABLED",
    "SEED_PATH_RENDER_ENABLED",
    "SEED_CANDIDATES_ENABLED",
    "CORROB_COUNTS_ENABLED",
    "SEED_BEHAVIOUR_EMAIL_ENABLED",
    "RECONCILE_ENABLED",
]


def _flag_file() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _FLAGS_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, val = stripped.partition("=")
        assert sep, f"malformed line in cron_flags.env: {line!r}"
        out[key.strip()] = val.strip()
    return out


def test_flag_file_carries_the_durable_set():
    flags = _flag_file()
    for flag in _DURABLE_FLAGS:
        assert flag in flags, f"{flag} missing from configs/cron_flags.env"
        assert flags[flag] != "", f"{flag} has an empty value in configs/cron_flags.env"


def test_render_flags_present_in_flag_file():
    flags = _flag_file()
    for flag in _RENDER_FLAGS:
        assert flag in flags, f"{flag} missing from configs/cron_flags.env"


def test_staging_build_does_not_consume_runtime_flag_file():
    text = _CLOUDBUILD.read_text(encoding="utf-8")
    assert "configs/cron_flags.env" not in text


def test_staging_build_cannot_update_production_jobs():
    text = _CLOUDBUILD.read_text(encoding="utf-8")
    for forbidden in (
        "gcloud ",
        "run jobs",
        "trends-engine-pipeline",
        "trends-engine-phase2",
        "trends-engine-regen",
        "trends-engine-resend",
        " push",
        "images:",
    ):
        assert forbidden not in text
