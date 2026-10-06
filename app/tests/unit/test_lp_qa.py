"""Freshness stamp logic in scripts/lp_qa.py.

The seed-discover payload carries a week_end label for the current ISO week,
which is a future date on every day except Sunday. The stamp must be the max
date at or before today. week_end is a declared label, so it never warns; a
future date under any other key is still a defect and still warns.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import lp_qa

TODAY = "2026-07-06"


def test_stamp_is_max_date_not_after_today():
    payload = {"trend_date": "2026-07-05", "created_at": "2026-07-06T01:00:00Z"}
    stamp, future = lp_qa._find_recent_date(payload, TODAY)
    assert stamp == "2026-07-06"
    assert future == []


def test_future_week_end_is_not_a_finding():
    """week_end labels the current ISO week, so it is future-dated on six days in
    seven. It must stay out of the stamp AND out of the findings: a warning that
    fires on a healthy payload every day trains the reader to ignore warnings."""
    payload = {
        "week_start": "2026-07-06",
        "week_end": "2026-07-12",
        "as_of": "2026-07-06",
    }
    stamp, future = lp_qa._find_recent_date(payload, TODAY)
    assert stamp == "2026-07-06"
    assert future == []


def test_unexpected_future_date_still_warns():
    """Only the declared forward-looking labels are exempt. A future date on any
    other key is still the data defect the check exists to catch."""
    payload = {"week_end": "2026-07-12", "trend_date": "2026-07-20"}
    stamp, future = lp_qa._find_recent_date(payload, TODAY)
    assert future == ["2026-07-20"]


def test_week_end_exemption_is_key_scoped_not_value_scoped():
    """The same date under a different key must still warn, so the exemption
    cannot be implemented by filtering the value out of the whole payload."""
    payload = {"week_end": "2026-07-12", "next_run": "2026-07-12"}
    stamp, future = lp_qa._find_recent_date(payload, TODAY)
    assert future == ["2026-07-12"]


def test_all_future_dates_yield_no_stamp():
    payload = {"forecast_day": "2026-07-09", "outlook_end": "2026-07-12"}
    stamp, future = lp_qa._find_recent_date(payload, TODAY)
    assert stamp is None
    assert future == ["2026-07-09", "2026-07-12"]


def test_string_payload_and_no_dates():
    stamp, future = lp_qa._find_recent_date("no dates here", TODAY)
    assert stamp is None
    assert future == []


def test_ipv4_addresses_are_tried_first():
    """Cloud Run answers with IPv6 ahead of IPv4 and no IPv6 address on this
    network is reachable. socket.create_connection walks the list in order and
    spends the full socket timeout on each entry, so leaving IPv6 first turned a
    30 second probe into a multi-minute one and wrote invented latency into
    qa_log.md. Reorder, do not filter: an IPv6-only network still has a path."""
    import socket

    v6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2607:f8b0::1", 443, 0, 0))
    v4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.1.1", 443))
    real = lp_qa._real_getaddrinfo
    try:
        lp_qa._real_getaddrinfo = lambda *a, **k: [v6, v6, v4, v6, v4]
        got = lp_qa._ipv4_first("example.invalid", 443)
    finally:
        lp_qa._real_getaddrinfo = real

    assert [i[0] for i in got] == [
        socket.AF_INET,
        socket.AF_INET,
        socket.AF_INET6,
        socket.AF_INET6,
        socket.AF_INET6,
    ]
    # Nothing is dropped, so an IPv6-only host still resolves.
    assert len(got) == 5


@pytest.mark.parametrize(
    "base",
    (
        "http://listening-post-590353929363.us-central1.run.app",
        "https://evil.test",
        "https://listening-post-590353929363.us-central1.run.app:444",
        "https://user@listening-post-590353929363.us-central1.run.app",
        "https://listening-post-590353929363.us-central1.run.app/api/health",
        "https://listening-post-590353929363.us-central1.run.app?x=1",
    ),
)
def test_invalid_qa_destination_refuses_before_any_probe_or_passcode(monkeypatch, base):
    monkeypatch.setattr(lp_qa, "_get", lambda *_args, **_kwargs: pytest.fail("network"))
    monkeypatch.setattr(
        lp_qa, "_passcode", lambda *_args, **_kwargs: pytest.fail("credential")
    )
    with pytest.raises(ValueError, match="qa_destination_invalid"):
        lp_qa.qa(base)


def test_mismatched_service_refuses_before_any_probe_or_passcode(monkeypatch):
    monkeypatch.setattr(lp_qa, "_get", lambda *_args, **_kwargs: pytest.fail("network"))
    monkeypatch.setattr(
        lp_qa, "_passcode", lambda *_args, **_kwargs: pytest.fail("credential")
    )
    with pytest.raises(ValueError, match="qa_destination_invalid"):
        lp_qa.qa(lp_qa.BASE, service="listening-post-staging")


def test_supported_destination_normalizes_one_trailing_slash_and_keeps_direct_calls(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        lp_qa,
        "_get",
        lambda url, *_args, **_kwargs: (
            calls.append(url) or (200, 1.0, {"ok": True, "passcode": False})
        ),
    )
    result = lp_qa.qa(lp_qa.BASE + "/")
    assert result["verdict"] == "HEALTHY"
    assert calls[0] == lp_qa.BASE + "/api/health"


class _Response:
    def __init__(self, body=b"{}"):
        self.body = body
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


@pytest.mark.parametrize(
    "url",
    (
        "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/services/listening-post",
        "https://secretmanager.googleapis.com/v1/projects/ogilvy-trends-v2/secrets/UI_PASSCODE/versions/latest:access",
        "https://secretmanager.googleapis.com/v1/projects/ogilvy-trends-v2/secrets/UI_PASSCODE/versions/7:access",
    ),
)
def test_supported_google_api_destinations_construct_one_bearer_request(
    monkeypatch, url
):
    requests = []

    def opener(*handlers):
        assert len(handlers) == 1
        assert isinstance(handlers[0], lp_qa._NoCredentialRedirect)
        return type(
            "Opener",
            (),
            {
                "open": staticmethod(
                    lambda req, **_kwargs: requests.append(req) or _Response()
                )
            },
        )()

    monkeypatch.setattr(lp_qa.urllib.request, "build_opener", opener)
    assert lp_qa._api(url, "synthetic-token") == {}
    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == "Bearer synthetic-token"


@pytest.mark.parametrize(
    "url",
    (
        "https://evil.test/v1/projects/ogilvy-trends-v2/secrets/x/versions/latest:access",
        "https://run.googleapis.com/v2/projects/other/locations/us-central1/services/listening-post",
        "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/europe/services/listening-post",
        "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/services/listening-post?x=1",
        "https://secretmanager.googleapis.com/v1/projects/ogilvy-trends-v2/secrets/x/versions/nope:access",
    ),
)
def test_invalid_google_api_destinations_refuse_before_request_or_opener(
    monkeypatch, url
):
    monkeypatch.setattr(
        lp_qa.urllib.request,
        "Request",
        lambda *_args, **_kwargs: pytest.fail("request"),
    )
    monkeypatch.setattr(
        lp_qa.urllib.request, "build_opener", lambda *_args: pytest.fail("opener")
    )
    with pytest.raises(ValueError, match="google_api_destination_invalid"):
        lp_qa._api(url, "synthetic-token")


@pytest.mark.parametrize(
    ("call", "header"),
    (
        (
            lambda: lp_qa._get(lp_qa.BASE + "/api/health", "synthetic-passcode"),
            "X-Passcode",
        ),
        (
            lambda: lp_qa._api(
                "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/services/listening-post",
                "synthetic-token",
            ),
            "Authorization",
        ),
    ),
)
def test_redirect_handler_refuses_to_forward_each_credential_header(
    monkeypatch, call, header
):
    opened = []

    def opener(*handlers):
        assert len(handlers) == 1
        handler = handlers[0]

        def open_request(req, **_kwargs):
            opened.append(req.full_url)
            assert header.lower() in {
                name.lower() for name, _value in req.header_items()
            }
            assert (
                handler.redirect_request(
                    req, None, 302, "redirect", {}, "https://evil.test/"
                )
                is None
            )
            raise lp_qa.urllib.error.HTTPError(req.full_url, 302, "redirect", {}, None)

        return type("Opener", (), {"open": staticmethod(open_request)})()

    monkeypatch.setattr(lp_qa.urllib.request, "build_opener", opener)
    if header == "X-Passcode":
        result = call()
        assert result[0] == 302
    else:
        with pytest.raises(lp_qa.urllib.error.HTTPError) as raised:
            call()
        assert raised.value.code == 302
    expected = (
        lp_qa.BASE + "/api/health"
        if header == "X-Passcode"
        else "https://run.googleapis.com/v2/projects/ogilvy-trends-v2/locations/us-central1/services/listening-post"
    )
    assert opened == [expected]
