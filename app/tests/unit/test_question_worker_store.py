import time
import io
import json
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from src.api.question_worker_result import ResultVerificationError
from src.api.question_worker_store import read_request_bytes, read_result_record
from src.api.question_worker_protocol import WorkerProtocolError
from tests.unit.test_question_worker_deadline import raw_request
from tests.unit.test_question_worker_result import EXPECTED, raw_fixture, receipt


class VersionedStore:
    def __init__(self, *, generation=17, size=None, data=None):
        self.data = raw_fixture() if data is None else data
        self.generation = generation
        self.size = len(self.data) if size is None else size
        self.reads = []
        self.metadata_hook = lambda: None
        self.body_hook = lambda: None

    def bucket(self, name):
        assert name == "listening-post-staging-cache"
        return self

    def blob(self, name, *, generation):
        assert (
            name
            == "open-intelligence/v2/staging/general-questions/requests/00000000-0000-0000-0000-000000000001/results/12f88cc6df5957af047efeb333fb07bacd186b4d8307aa8163378d364fe1629e.json"
        )
        assert generation == 17
        return self

    def reload(self, *, if_generation_match, timeout, retry):
        assert if_generation_match == 17 and retry is None and 0 < timeout <= 10
        self.reads.append("metadata")
        self.metadata_hook()

    def download_as_bytes(
        self, *, if_generation_match, start, end, raw_download, timeout, retry
    ):
        assert if_generation_match == 17 and retry is None and 0 < timeout <= 10
        assert start == 0 and end == 4 * 1024 * 1024 and raw_download is True
        self.reads.append("body")
        self.body_hook()
        return self.data[start : end + 1]


def read(store, deadline=None, pointer=None):
    return read_result_record(
        store,
        invocation=EXPECTED,
        receipt=pointer or receipt(),
        deadline=time.monotonic() + 10 if deadline is None else deadline,
    )


def test_result_read_selects_the_exact_generation_and_preserves_the_verified_record():
    store = VersionedStore()
    assert read(store)["response"]["reason"] == "request_expired"
    assert store.reads == ["metadata", "body"]


@pytest.mark.parametrize(
    "change",
    [
        {"generation": 18},
        {"generation": None},
        {"size": 4 * 1024 * 1024 + 1},
        {"size": -1},
        {"size": True},
    ],
)
def test_wrong_generation_or_oversize_metadata_prevents_body_read(change):
    store = VersionedStore(**change)
    with pytest.raises(ResultVerificationError):
        read(store)
    assert store.reads == ["metadata"]


def test_lying_size_does_not_bypass_the_bounded_raw_read():
    store = VersionedStore(size=1, data=b" " * (4 * 1024 * 1024 + 50))
    with pytest.raises(ResultVerificationError):
        read(store)
    assert store.reads == ["metadata", "body"]


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), True, -1])
def test_invalid_or_expired_deadline_performs_no_storage_read(deadline):
    store = VersionedStore()
    with pytest.raises(ResultVerificationError):
        read(store, deadline=deadline)
    assert store.reads == []


def test_invalid_pointer_cannot_choose_a_storage_path():
    store = VersionedStore()
    with pytest.raises(ResultVerificationError):
        read(store, pointer=receipt() | {"result_digest": "../../other"})
    assert store.reads == []


@pytest.mark.parametrize("stage", ["metadata", "body"])
def test_storage_failure_is_controlled_without_raw_service_text(stage):
    store = VersionedStore()

    def fail():
        raise RuntimeError("private service response")

    if stage == "metadata":
        store.metadata_hook = fail
    else:
        store.body_hook = fail
    with pytest.raises(
        ResultVerificationError, match="result_store_unavailable"
    ) as error:
        read(store)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("stage", ["metadata", "body"])
def test_deadline_expiring_during_read_does_not_acknowledge_a_result(
    stage, monkeypatch
):
    store = VersionedStore()
    clock = [1.0]
    monkeypatch.setattr(
        "src.api.question_worker_store.time.monotonic", lambda: clock[0]
    )

    def expire():
        clock[0] = 11.0

    if stage == "metadata":
        store.metadata_hook = expire
    else:
        store.body_hook = expire
    with pytest.raises(ResultVerificationError, match="result_deadline_reached"):
        read(store, deadline=10.0)
    assert store.reads == (
        ["metadata"] if stage == "metadata" else ["metadata", "body"]
    )


@pytest.mark.parametrize("media_generation", ["17", "18", None])
@pytest.mark.parametrize("kind", ["result", "request"])
def test_real_storage_sdk_pins_metadata_and_bounded_media_requests_without_network(
    media_generation,
    kind,
):
    import requests
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage
    from urllib3.response import HTTPResponse

    observed = []
    data = raw_fixture() if kind == "result" else raw_request()
    object_name = "open-intelligence/v2/staging/general-questions/requests/00000000-0000-0000-0000-000000000001/results/12f88cc6df5957af047efeb333fb07bacd186b4d8307aa8163378d364fe1629e.json"
    if kind == "request":
        object_name = "open-intelligence/v2/staging/general-questions/requests/00000000-0000-0000-0000-000000000001/request.json"

    class MemorySession(requests.Session):
        is_mtls = False

        def request(self, method, url, **kwargs):
            parsed = urlparse(url)
            query = parse_qs(parsed.query)
            assert method == "GET"
            assert parsed.hostname == "storage.googleapis.com"
            assert unquote(parsed.path).endswith(
                "/b/listening-post-staging-cache/o/" + object_name
            )
            if kind == "request" and not parsed.path.startswith("/download/"):
                assert "generation" not in query and "ifGenerationMatch" not in query
            else:
                assert query["generation"] == ["17"]
                assert query["ifGenerationMatch"] == ["17"]
            assert 0 < kwargs["timeout"] <= 10
            observed.append(parsed.path)
            response = requests.Response()
            response.url = url
            if parsed.path.startswith("/download/"):
                headers = {
                    key.lower(): value
                    for key, value in kwargs.get("headers", {}).items()
                }
                assert headers["range"] == "bytes=0-4194304"
                response.status_code = 206
                response._content = data
                response.headers.update(
                    {
                        "Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}",
                        "Content-Length": str(len(data)),
                    }
                )
                if media_generation is not None:
                    response.headers["X-Goog-Generation"] = media_generation
            else:
                response.status_code = 200
                response._content = json.dumps(
                    {
                        "bucket": "listening-post-staging-cache",
                        "name": object_name,
                        "generation": "17",
                        "size": str(len(data)),
                    }
                ).encode()
            response._content_consumed = True
            response.raw = HTTPResponse(
                body=io.BytesIO(response._content),
                headers=response.headers,
                preload_content=False,
                decode_content=False,
            )
            return response

    client = storage.Client(
        project="offline-fixture",
        credentials=AnonymousCredentials(),
        _http=MemorySession(),
    )

    def perform_read():
        if kind == "result":
            return read(client)
        return read_request_bytes(
            client, invocation=EXPECTED, deadline=time.monotonic() + 10
        )

    if media_generation == "17":
        try:
            result = perform_read()
        except ResultVerificationError as error:
            raise AssertionError(
                f"Offline SDK failure: {error}; {error.__context__}"
            ) from error
        if kind == "result":
            assert result["response"]["reason"] == "request_expired"
        else:
            assert result == data
    else:
        with pytest.raises(ResultVerificationError, match="result_generation_mismatch"):
            perform_read()
    assert len(observed) == 2


def test_invalid_task_never_selects_an_immutable_request_object():
    store = VersionedStore()
    with pytest.raises(WorkerProtocolError):
        read_request_bytes(
            store,
            invocation=EXPECTED | {"request_id": "../different"},
            deadline=time.monotonic() + 10,
        )
    assert store.reads == []
