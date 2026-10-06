"""Unit tests for src/utils/secrets.get_secret.

The Secret Manager resolution branch and the staging/prod client gate are the
infra path every connector token passes through when TRENDS_ENV=prod, but
nothing exercised it. These tests mock the SecretManagerServiceClient boundary
so no GCP call runs.
"""

from unittest.mock import MagicMock

import pytest
import src.utils.secrets as secrets


def test_get_secret_dev_reads_env_var(monkeypatch):
    """In dev the Secret Manager path is skipped and the env var is returned."""
    monkeypatch.setenv("TRENDS_ENV", "dev")
    monkeypatch.setenv("MY_TOKEN", "env-value")
    assert secrets.get_secret("my-token") == "env-value"


def test_get_secret_dev_returns_default_when_unset(monkeypatch):
    monkeypatch.setenv("TRENDS_ENV", "dev")
    monkeypatch.delenv("ABSENT_TOKEN", raising=False)
    assert secrets.get_secret("absent-token", default="fallback") == "fallback"


def test_get_secret_client_is_none_in_dev(monkeypatch):
    """The lazy client gate returns None outside staging/prod so no GCP import
    or client is created locally."""
    monkeypatch.setenv("TRENDS_ENV", "dev")
    monkeypatch.delenv("CLOUD_RUN_JOB", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("CLOUD_RUN_EXECUTION", raising=False)
    secrets._get_secret_client.cache_clear()
    assert secrets._get_secret_client() is None


def test_get_secret_cloud_run_dev_uses_secret_manager(monkeypatch):
    """Cloud Run jobs keep TRENDS_ENV=dev but must still read Secret Manager."""
    secrets._get_secret_client.cache_clear()
    monkeypatch.setenv("TRENDS_ENV", "dev")
    monkeypatch.setenv("CLOUD_RUN_JOB", "trends-engine-pipeline")
    monkeypatch.setenv("GCP_PROJECT", "ogilvy-trends-v2")
    fake_client = MagicMock()
    resp = MagicMock()
    resp.payload.data = b"cloud-run-key"
    fake_client.access_secret_version.return_value = resp
    monkeypatch.setattr(secrets, "_get_secret_client", lambda: fake_client)

    assert secrets.get_secret("SOCIALCRAWL_API_KEY") == "cloud-run-key"


def test_get_secret_prod_decodes_secret_manager_payload(monkeypatch):
    """In prod a mocked SecretManagerServiceClient is queried and its UTF-8
    payload decoded; the resource name is built from GCP_PROJECT + secret_id."""
    monkeypatch.setenv("TRENDS_ENV", "prod")
    monkeypatch.setenv("GCP_PROJECT", "ogilvy-trends-v2")
    fake_client = MagicMock()
    resp = MagicMock()
    resp.payload.data = b"sm-secret-value"
    fake_client.access_secret_version.return_value = resp
    monkeypatch.setattr(secrets, "_get_secret_client", lambda: fake_client)

    assert secrets.get_secret("ensembledata-api-token") == "sm-secret-value"
    request = fake_client.access_secret_version.call_args.kwargs["request"]
    assert (
        request["name"]
        == "projects/ogilvy-trends-v2/secrets/ensembledata-api-token/versions/latest"
    )


def test_get_secret_prod_falls_back_to_env_on_sm_error(monkeypatch):
    """A Secret Manager exception is swallowed and the env var is used, so a
    transient SM failure does not blank every connector token."""
    monkeypatch.setenv("TRENDS_ENV", "prod")
    monkeypatch.setenv("GCP_PROJECT", "ogilvy-trends-v2")
    monkeypatch.setenv("ENSEMBLEDATA_API_TOKEN", "env-fallback")
    fake_client = MagicMock()
    fake_client.access_secret_version.side_effect = RuntimeError("permission denied")
    monkeypatch.setattr(secrets, "_get_secret_client", lambda: fake_client)

    assert secrets.get_secret("ensembledata-api-token") == "env-fallback"


# ---------------------------------------------------------------------------
# Secret id aliasing. The live secrets are named youtube-api-key,
# ensembledata-api-token and brand24-api-key, but every caller asks for the
# env-var spelling (YOUTUBE_API_KEY). Secret Manager returned 403 for the
# name that does not exist, the code logged ERROR and fell through to the env
# var Cloud Run had already mounted from the correctly named secret, so it
# worked by accident and shouted about it every run.
# ---------------------------------------------------------------------------


def _cloud_run(monkeypatch):
    secrets._get_secret_client.cache_clear()
    monkeypatch.setenv("CLOUD_RUN_JOB", "trends-engine-pipeline")
    monkeypatch.setenv("GCP_PROJECT", "ogilvy-trends-v2")


def test_get_secret_falls_back_to_hyphenated_secret_name(monkeypatch):
    """A miss on the given id retries the lowercase-hyphen form before env."""
    _cloud_run(monkeypatch)
    monkeypatch.setenv("YOUTUBE_API_KEY", "env-value-should-not-win")

    asked = []

    class _Client:
        def access_secret_version(self, request):
            name = request["name"]
            asked.append(name)
            if name.endswith("/secrets/YOUTUBE_API_KEY/versions/latest"):
                raise RuntimeError("403 Permission denied on resource")
            payload = MagicMock()
            payload.data = b"sm-value"
            resp = MagicMock()
            resp.payload = payload
            return resp

    monkeypatch.setattr(secrets, "_get_secret_client", lambda: _Client())

    assert secrets.get_secret("YOUTUBE_API_KEY") == "sm-value"
    assert any("/secrets/youtube-api-key/versions/latest" in n for n in asked), asked


def test_get_secret_does_not_retry_when_the_first_name_works(monkeypatch):
    """SOCIALCRAWL_API_KEY really is stored under that exact name, so a hit on
    the first try must not cost a second Secret Manager call."""
    _cloud_run(monkeypatch)

    asked = []

    class _Client:
        def access_secret_version(self, request):
            asked.append(request["name"])
            payload = MagicMock()
            payload.data = b"first-try"
            resp = MagicMock()
            resp.payload = payload
            return resp

    monkeypatch.setattr(secrets, "_get_secret_client", lambda: _Client())

    assert secrets.get_secret("SOCIALCRAWL_API_KEY") == "first-try"
    assert len(asked) == 1, asked


def test_secret_manager_miss_covered_by_env_is_not_an_error(monkeypatch, caplog):
    """A Secret Manager miss the env fallback covers is not an ERROR. Errors
    nobody can act on are what hide the errors somebody can."""
    _cloud_run(monkeypatch)
    monkeypatch.setenv("SOME_TOKEN", "from-env")

    class _Client:
        def access_secret_version(self, request):
            raise RuntimeError("404 not found")

    monkeypatch.setattr(secrets, "_get_secret_client", lambda: _Client())

    with caplog.at_level("DEBUG", logger="src.utils.secrets"):
        assert secrets.get_secret("SOME_TOKEN") == "from-env"
    assert not [r for r in caplog.records if r.levelname == "ERROR"], caplog.text


def test_secret_manager_miss_with_no_env_fallback_is_still_an_error(monkeypatch, caplog):
    """When both sources fail the failure is real and must stay loud."""
    _cloud_run(monkeypatch)
    monkeypatch.delenv("GONE_TOKEN", raising=False)

    class _Client:
        def access_secret_version(self, request):
            raise RuntimeError("404 not found")

    monkeypatch.setattr(secrets, "_get_secret_client", lambda: _Client())

    with caplog.at_level("DEBUG", logger="src.utils.secrets"):
        assert secrets.get_secret("GONE_TOKEN") == ""
    assert [r for r in caplog.records if r.levelname == "ERROR"], caplog.text


# The funded SocialCrawl secret is read at the version its IAM grant names.
#
# The approved condition (ops/deploy/iam_delta_v1.json) admits exactly
# SOCIALCRAWL_OGILVY_API_KEY/versions/1, so the reader asks for version 1 by
# name rather than the latest alias. The version is pinned in the module and
# never taken from the caller; every other secret still reads latest.

FUNDED = "SOCIALCRAWL_OGILVY_API_KEY"


def _asking_client(asked, *, fail=False):
    class _Client:
        def access_secret_version(self, request):
            asked.append(request["name"])
            if fail:
                raise RuntimeError("403 permission denied")
            resp = MagicMock()
            resp.payload.data = b"pinned-value"
            return resp

    return _Client()


def test_the_funded_secret_is_read_at_version_one_by_name(monkeypatch):
    _cloud_run(monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(secrets, "_get_secret_client", lambda: _asking_client(asked))

    assert secrets.get_secret(FUNDED) == "pinned-value"
    assert asked == [f"projects/ogilvy-trends-v2/secrets/{FUNDED}/versions/1"]


def test_a_funded_secret_miss_is_not_retried_at_latest_or_another_name(monkeypatch):
    _cloud_run(monkeypatch)
    monkeypatch.delenv(FUNDED, raising=False)
    asked: list[str] = []
    monkeypatch.setattr(secrets, "_get_secret_client", lambda: _asking_client(asked, fail=True))

    assert secrets.get_secret(FUNDED) == ""
    assert asked == [f"projects/ogilvy-trends-v2/secrets/{FUNDED}/versions/1"]


def test_any_spelling_of_the_funded_secret_reads_the_pinned_name_and_version(monkeypatch):
    _cloud_run(monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(secrets, "_get_secret_client", lambda: _asking_client(asked))

    assert secrets.get_secret("socialcrawl-ogilvy-api-key") == "pinned-value"
    assert asked == [f"projects/ogilvy-trends-v2/secrets/{FUNDED}/versions/1"]


def test_other_secrets_still_read_latest(monkeypatch):
    _cloud_run(monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(secrets, "_get_secret_client", lambda: _asking_client(asked))

    assert secrets.get_secret("SOCIALCRAWL_API_KEY") == "pinned-value"
    assert asked == ["projects/ogilvy-trends-v2/secrets/SOCIALCRAWL_API_KEY/versions/latest"]


def test_the_version_pin_is_module_data_and_not_a_caller_argument():
    import inspect

    assert dict(secrets.PINNED_SECRET_VERSIONS) == {FUNDED: "1"}
    with pytest.raises(TypeError):
        secrets.PINNED_SECRET_VERSIONS[FUNDED] = "latest"
    assert list(inspect.signature(secrets.get_secret).parameters) == ["secret_id", "default"]
