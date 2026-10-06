"""Native adapter for the release lifecycle: Cloud Run Admin API v2, Cloud Tasks
v2 and the storage JSON API over one injectable HTTP transport.

release.py loads ``factory(state)``. The state string names the credential
source only: ``owner-gcloud`` mints a bearer token through the absolute gcloud
path with the owner account, ``adc`` uses application default credentials.
Every mutation is one REST request with an explicit body, so the request the
tests see is the request that runs; nothing here shells out for a mutation.
Every request and response is recorded on the adapter for the receipt. The
bearer token is never recorded and never written anywhere.
"""

import copy
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parents[2]
for _entry in (str(_ROOT / "engine"), str(_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from ops.deploy import release  # noqa: E402
from src.analysis.open_intelligence.general_question_policy import (  # noqa: E402
    _PRICING_SOURCE,
)

RUN_ENDPOINT = "https://run.googleapis.com/v2/"
TASKS_ENDPOINT = "https://cloudtasks.googleapis.com/v2/"
STORAGE_ENDPOINT = "https://storage.googleapis.com/storage/v1/"
UPLOAD_ENDPOINT = "https://storage.googleapis.com/upload/storage/v1/"
BUILD_ENDPOINT = "https://cloudbuild.googleapis.com/v1/"
# An image digest is the sha256 of its manifest, so the manifest fetched by digest
# is the native byte source for an image reference. The registry must be asked
# for every manifest media type it may hold, or it converts and the bytes differ.
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)
# The only hosts a ``source:`` reference may name: the pricing page the policy
# module reads. The set is derived from that module, never chosen here.
SOURCE_HOSTS = frozenset({urllib.parse.urlsplit(_PRICING_SOURCE).netloc})
BUCKET = release.BUCKET
OWNER_ACCOUNT = "jhb.analytics@gmail.com"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
STATES = ("owner-gcloud", "adc")
TASK_PAGE_SIZE = 1000
IDEMPOTENCY_HEADER = "X-Idempotency-Key"
REQUEST_TIMEOUT_SECONDS = 60.0
_run_command = subprocess.run


class NativeError(RuntimeError):
    def __init__(self, call, status, detail):
        super().__init__(f"{call}: http {status}: {detail}")
        self.call = call
        self.status = status


class PreconditionFailed(NativeError):
    pass


# Credential sources


def _gcloud_path():
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        raise ValueError("gcloud_unavailable")
    return (
        Path(root) / "Google" / "Cloud SDK" / "google-cloud-sdk" / "bin" / "gcloud.cmd"
    )


def _gcloud_token():
    path = _gcloud_path()
    if not path.is_file():
        raise ValueError("gcloud_unavailable")
    completed = _run_command(
        [
            str(path),
            "auth",
            "print-access-token",
            f"--account={OWNER_ACCOUNT}",
            "--quiet",
        ],
        shell=False,
        check=False,
        capture_output=True,
        timeout=40,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if completed.returncode != 0:
        raise ValueError("owner_token_unavailable")
    token = completed.stdout.decode("utf-8", "strict").strip()
    if not token:
        raise ValueError("owner_token_empty")
    return token


def _adc_token():
    import google.auth
    from google.auth.transport.requests import Request

    credentials, _project = google.auth.default(scopes=(CLOUD_PLATFORM_SCOPE,))
    credentials.refresh(Request())
    if not credentials.token:
        raise ValueError("adc_token_empty")
    return credentials.token


# Transport


class RedirectRefused(urllib.error.URLError):
    """A redirected request is refused; nothing here follows one."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow no redirect at all and name the refusal.

    Two separate harms sit behind one 3xx. Every request this adapter sends
    carries the owner bearer token, and the redirect handler in the standard
    library copies the request headers onto the redirected request, stripping
    only the content headers, so one Location pointing anywhere hands that token
    to whatever host it names. And 301, 302 and 303 turn a POST into a GET with
    the body dropped, so a pause or a wait that never ran would come back 200
    from the redirect target while the record still names the original URL, and
    the status check would count a call that did not happen as a success.

    Following with the credential stripped closes neither harm completely: it
    would still let the redirecting host choose which server answers a release
    request, and the answer to a release request decides what the release
    believes about production. A redirect on a Cloud Run, Cloud Tasks or storage
    JSON API call is not something to accommodate, so every 3xx stops here by
    name, with the status and both origins in the message and the credential
    still where it started.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        origin = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        raise RedirectRefused(
            f"redirect_refused: http {code} "
            f"{origin.scheme}://{origin.netloc}{origin.path} -> "
            f"{target.scheme}://{target.netloc}{target.path}"
        )


_opener = urllib.request.build_opener(_NoRedirect())


def urllib_transport(request):
    """Send one request; the response is the status, the headers and the raw body."""
    http = urllib.request.Request(
        request["url"],
        data=request.get("body"),
        method=request["method"],
        headers=request["headers"],
    )
    try:
        with _opener.open(http, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return {
                "status": response.status,
                "headers": dict(response.headers),
                "body": response.read(),
            }
    except urllib.error.HTTPError as error:
        return {
            "status": error.code,
            "headers": dict(error.headers or {}),
            "body": error.read(),
        }


def _digest(raw):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _query(url, **params):
    return url + "?" + urllib.parse.urlencode(params)


def _object_url(bucket, name, **params):
    url = STORAGE_ENDPOINT + f"b/{bucket}/o/" + urllib.parse.quote(name, safe="")
    return _query(url, **params) if params else url


def _redact(call, value):
    """Keep private environment values out of the record the receipt carries."""
    if not isinstance(value, dict):
        return value
    if call == "get_service":
        return release.redacted_service(value)
    if call == "get_revision":
        return release.redacted_service({"template": value})["template"]
    if call == "deploy_revision" and isinstance(value.get("template"), dict):
        return release.redacted_service(value)
    return value


def build_request(call, **kwargs):
    """The exact request one adapter call sends; the dry run renders these."""
    if call in ("get_service", "get_revision"):
        return {"call": call, "method": "GET", "url": RUN_ENDPOINT + kwargs["name"]}
    if call == "deploy_revision":
        return {
            "call": call,
            "method": "PATCH",
            "url": _query(RUN_ENDPOINT + kwargs["service_name"], updateMask="template"),
            "json": {"template": kwargs["template"], "etag": kwargs.get("etag")},
        }
    if call == "update_traffic":
        return {
            "call": call,
            "method": "PATCH",
            "url": _query(RUN_ENDPOINT + kwargs["service_name"], updateMask="traffic"),
            "json": {"traffic": kwargs["traffic"], "etag": kwargs["etag"]},
        }
    if call == "read_operation":
        seconds = format(float(kwargs["timeout_seconds"]), ".3f").rstrip("0")
        return {
            "call": call,
            "method": "POST",
            "url": RUN_ENDPOINT + kwargs["name"] + ":wait",
            "json": {"timeout": seconds.rstrip(".") + "s"},
        }
    if call == "get_queue":
        return {"call": call, "method": "GET", "url": TASKS_ENDPOINT + kwargs["name"]}
    if call == "list_tasks":
        params = {"responseView": "BASIC", "pageSize": str(TASK_PAGE_SIZE)}
        if kwargs.get("page_token"):
            params["pageToken"] = kwargs["page_token"]
        return {
            "call": call,
            "method": "GET",
            "url": _query(TASKS_ENDPOINT + kwargs["name"] + "/tasks", **params),
        }
    if call in ("pause_queue", "resume_queue"):
        verb = ":pause" if call == "pause_queue" else ":resume"
        return {
            "call": call,
            "method": "POST",
            "url": TASKS_ENDPOINT + kwargs["name"] + verb,
            "json": {},
        }
    if call == "get_build":
        return {"call": call, "method": "GET", "url": BUILD_ENDPOINT + kwargs["name"]}
    if call == "image_manifest":
        host, _, path = kwargs["image"].partition("/")
        return {
            "call": call,
            "method": "GET",
            "url": f"https://{host}/v2/{path}/manifests/sha256:{kwargs['digest']}",
            "media": True,
            "accept": MANIFEST_ACCEPT,
        }
    if call in ("fetch_source", "fetch_asset"):
        # Anonymous: neither the pricing page nor the served revision is a Google
        # API, so the owner credential never travels to either host.
        return {
            "call": call,
            "method": "GET",
            "url": kwargs["url"],
            "media": True,
            "anonymous": True,
        }
    bucket = kwargs.get("bucket", BUCKET)
    if call == "object_metadata":
        params = {}
        if kwargs.get("generation") is not None:
            params["generation"] = str(kwargs["generation"])
        return {
            "call": call,
            "method": "GET",
            "url": _object_url(bucket, kwargs["name"], **params),
        }
    if call == "object_media":
        return {
            "call": call,
            "method": "GET",
            "url": _object_url(
                bucket,
                kwargs["name"],
                alt="media",
                generation=str(kwargs["generation"]),
            ),
            "media": True,
        }
    if call == "create_object":
        return {
            "call": call,
            "method": "POST",
            "url": _query(
                UPLOAD_ENDPOINT + f"b/{bucket}/o",
                uploadType="media",
                name=kwargs["name"],
                ifGenerationMatch=str(int(kwargs["if_generation_match"])),
            ),
            "raw": bytes(kwargs["raw"]),
            "content_type": "application/json",
        }
    raise ValueError("unknown_call")


class NativeAdapter:
    def __init__(self, state, *, transport=None):
        if state not in STATES:
            raise ValueError("adapter_state_invalid")
        self.state = state
        self.transport = transport or urllib_transport
        self.record = []
        self._token = None

    def token(self):
        if self._token is None:
            self._token = (
                _gcloud_token() if self.state == "owner-gcloud" else _adc_token()
            )
        return self._token

    def render(self, call, **kwargs):
        request = build_request(call, **kwargs)
        rendered = {key: request[key] for key in ("call", "method", "url")}
        if "json" in request:
            rendered["body"] = request["json"]
        if "raw" in request:
            rendered["body"] = _digest(request["raw"])
        return rendered

    def perform(self, call, note=None, **kwargs):
        """Send the request for one call and record what went out and came back."""
        if call == "deploy_revision" and not kwargs.get("etag"):
            raise ValueError("service_etag_missing")
        request = build_request(call, **kwargs)
        headers = {}
        if not request.get("anonymous"):
            headers["Authorization"] = "Bearer " + self.token()
        note = dict(note or {})
        if note.get("idempotency_key") is not None:
            headers[IDEMPOTENCY_HEADER] = str(note["idempotency_key"])
        data = None
        entry = {"call": call, "method": request["method"], "url": request["url"]}
        if "json" in request:
            data = json.dumps(
                request["json"], sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
            entry["request"] = _redact(call, request["json"])
        elif "raw" in request:
            data = request["raw"]
            headers["Content-Type"] = request["content_type"]
            entry["request"] = _digest(data)
        if request.get("accept"):
            headers["Accept"] = request["accept"]
        elif not request.get("media"):
            headers["Accept"] = "application/json"
        response = self.transport(
            {
                "method": request["method"],
                "url": request["url"],
                "headers": headers,
                "body": data,
            }
        )
        if not isinstance(response, dict) or "status" not in response:
            raise NativeError(call, 0, "transport_response_invalid")
        status = int(response["status"])
        body = bytes(response.get("body") or b"")
        parsed = True
        if request.get("media") and status == 200:
            payload = body
            entry["response"] = _digest(body)
        else:
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (UnicodeError, ValueError):
                parsed = False
                payload = {"raw": body[:1000].decode("utf-8", "replace")}
            entry["response"] = _redact(call, payload)
        entry["status"] = status
        entry.update(note)
        self.record.append(entry)
        if status == 200 and not parsed:
            raise NativeError(call, status, "response_not_json")
        return status, payload

    def get(self, call, **kwargs):
        status, payload = self.perform(call, **kwargs)
        if status == 404:
            return None
        if status != 200:
            raise NativeError(call, status, payload)
        return payload

    def require_ok(self, call, status, payload):
        if status != 200:
            raise NativeError(call, status, payload)
        return payload


def _operation_name(call, status, operation):
    if status != 200:
        raise NativeError(call, status, operation)
    name = operation.get("name") if isinstance(operation, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise NativeError(call, status, "operation_name_missing")
    return name


class _Run:
    def __init__(self, adapter):
        self.adapter = adapter

    def get_service(self, name):
        return self.adapter.get("get_service", name=name)

    def get_revision(self, name):
        return self.adapter.get("get_revision", name=name)

    def deploy_revision(
        self,
        service_name,
        *,
        revision,
        template,
        idempotency_key,
        expected_current_template=None,
        expected_current_service=None,
    ):
        if template.get("revision") != revision:
            raise ValueError("template_revision_mismatch")
        pricing = revision.startswith(release.SERVICE_NAME + "-p-")
        if pricing and not isinstance(expected_current_template, dict):
            raise ValueError("pricing_expected_template_missing")
        if pricing and not isinstance(expected_current_service, dict):
            raise ValueError("pricing_expected_service_missing")
        service = self.get_service(service_name)
        if not isinstance(service, dict) or service.get("name") != service_name:
            raise ValueError("service_unavailable")
        etag = service.get("etag")
        if not isinstance(etag, str) or not etag:
            raise ValueError("service_etag_missing")
        if pricing:
            if (
                etag != expected_current_service.get("etag")
                or service.get("template") != expected_current_template
                or expected_current_service.get("template") != expected_current_template
            ):
                raise ValueError("pricing_service_drift")
            old_revision = expected_current_template.get("revision")
            if not isinstance(old_revision, str) or not old_revision:
                raise ValueError("pricing_service_drift")
            full_revision = (
                service_name + "/revisions/" + old_revision
            )
            generation = service.get("generation")
            condition = service.get("terminalCondition")
            traffic = service.get("traffic")
            statuses = service.get("trafficStatuses")
            settled_fields = (
                "terminalCondition",
                "generation",
                "observedGeneration",
                "latestCreatedRevision",
                "latestReadyRevision",
                "traffic",
                "trafficStatuses",
            )
            if (
                service.get("reconciling", False) is not False
                or expected_current_service.get("reconciling", False) is not False
                or not isinstance(condition, dict)
                or condition.get("type") != "Ready"
                or condition.get("state") != "CONDITION_SUCCEEDED"
                or not isinstance(generation, str)
                or not generation.isdecimal()
                or int(generation) <= 0
                or service.get("observedGeneration") != generation
                or service.get("latestCreatedRevision") != full_revision
                or service.get("latestReadyRevision") != full_revision
                or not isinstance(traffic, list)
                or len(traffic) != 1
                or not isinstance(traffic[0], dict)
                or traffic[0].get("percent") != 100
                or traffic[0].get("revision") != old_revision
                or not isinstance(statuses, list)
                or len(statuses) != 1
                or not isinstance(statuses[0], dict)
                or {
                    key: statuses[0].get(key)
                    for key in ("type", "revision", "percent", "tag")
                }
                != traffic[0]
                or any(
                    service.get(field) != expected_current_service.get(field)
                    for field in settled_fields
                )
            ):
                raise ValueError("pricing_service_drift")
            try:
                expected_next = copy.deepcopy(expected_current_template)
                expected_next["revision"] = revision
                old_entries = expected_next["containers"][0]["env"]
                new_entries = template["containers"][0]["env"]
                digest_name = "GENERAL_QUESTION_DEPLOYMENT_DIGEST"
                old_digest = [
                    entry for entry in old_entries if entry["name"] == digest_name
                ]
                new_digest = [
                    entry for entry in new_entries if entry["name"] == digest_name
                ]
                if len(old_digest) != 1 or len(new_digest) != 1:
                    raise ValueError("pricing_service_drift")
                if (
                    set(old_digest[0]) != {"name", "value"}
                    or set(new_digest[0]) != {"name", "value"}
                    or not isinstance(new_digest[0]["value"], str)
                    or not new_digest[0]["value"]
                ):
                    raise ValueError("pricing_service_drift")
                old_digest[0]["value"] = new_digest[0]["value"]
                if expected_next != template:
                    raise ValueError("pricing_service_drift")
            except (KeyError, IndexError, TypeError, AttributeError):
                raise ValueError("pricing_service_drift") from None
        status, operation = self.adapter.perform(
            "deploy_revision",
            note={"idempotency_key": idempotency_key},
            service_name=service_name,
            template=template,
            etag=etag,
        )
        return {"operation": _operation_name("deploy_revision", status, operation)}

    def update_traffic(self, service_name, *, etag, traffic, idempotency_key):
        status, operation = self.adapter.perform(
            "update_traffic",
            note={"idempotency_key": idempotency_key},
            service_name=service_name,
            traffic=traffic,
            etag=etag,
        )
        return {"operation": _operation_name("update_traffic", status, operation)}

    def read_operation(self, name, timeout_seconds):
        status, operation = self.adapter.perform(
            "read_operation", name=name, timeout_seconds=timeout_seconds
        )
        self.adapter.require_ok("read_operation", status, operation)
        if not operation.get("done"):
            return {
                "operation": operation.get("name"),
                "state": "pending",
                "error": None,
            }
        error = operation.get("error")
        if error is not None:
            detail = f"{error.get('code')}: {error.get('message')}"
            return {
                "operation": operation.get("name"),
                "state": "failed",
                "error": detail,
            }
        return {"operation": operation.get("name"), "state": "succeeded", "error": None}


class _Tasks:
    def __init__(self, adapter):
        self.adapter = adapter

    def get_queue(self, name):
        return self.adapter.get("get_queue", name=name)

    def list_tasks(self, name, *, page_token=None):
        status, page = self.adapter.perform(
            "list_tasks", name=name, page_token=page_token
        )
        return self.adapter.require_ok("list_tasks", status, page)

    def pause(self, name):
        status, queue = self.adapter.perform("pause_queue", name=name)
        return self.adapter.require_ok("pause_queue", status, queue)

    def resume(self, name):
        status, queue = self.adapter.perform("resume_queue", name=name)
        return self.adapter.require_ok("resume_queue", status, queue)


class _Objects:
    def __init__(self, adapter, bucket):
        self.adapter = adapter
        self.bucket = bucket

    def _read_from(self, bucket, name, generation=None):
        """Metadata first, then the media pinned to the generation the metadata named."""
        if bucket != self.bucket:
            raise ValueError("object_bucket_not_approved")
        metadata = self.adapter.get(
            "object_metadata", bucket=bucket, name=name, generation=generation
        )
        if metadata is None:
            return None
        pinned = str(metadata["generation"])
        status, raw = self.adapter.perform(
            "object_media", bucket=bucket, name=name, generation=pinned
        )
        if status == 404:
            return None
        self.adapter.require_ok("object_media", status, raw)
        if "size" in metadata and int(metadata["size"]) != len(raw):
            raise NativeError("object_media", status, "object_size_mismatch")
        stored = {"generation": pinned, "raw": raw}
        if "timeCreated" in metadata:
            stored["time_created"] = metadata["timeCreated"]
        return stored

    def read(self, name, *, generation=None):
        return self._read_from(self.bucket, name, generation)

    def create(self, name, raw, *, if_generation_match):
        status, metadata = self.adapter.perform(
            "create_object",
            note={"name": name, "if_generation_match": int(if_generation_match)},
            bucket=self.bucket,
            name=name,
            raw=raw,
            if_generation_match=if_generation_match,
        )
        if status == 412:
            raise PreconditionFailed("create_object", status, "generation_mismatch")
        self.adapter.require_ok("create_object", status, metadata)
        return {"generation": str(metadata["generation"])}


class _Bytes:
    """The byte reader behind ``validate_release_index``.

    ``read`` takes a bare reference and resolves the two kinds that carry their
    own source: ``object:gs://<approved bucket>/name#generation`` from the
    evidence bucket, and ``source:<https url>`` from the pricing host by an
    anonymous GET. Any other bucket is refused by name and any other host is
    unavailable. The release index grammar already refuses a bucket the resource
    manifest does not cover, but that check is one expression away from being
    the only barrier, so the reader that would spend the owner credential checks
    the bucket itself.

    ``reader(index, asset_origin=...)`` binds the reader to one index and returns
    the callable the validator consumes; the bound reader resolves every
    symbolic reference to the most native source that exists for it:

    - ``commit``: the revision Cloud Build recorded for the build named by the
      index, and only when that build succeeded in the project and pushed the
      app image the index names, so the bytes are what was built, not what was
      typed;
    - ``engine_image`` and ``app_image``: the manifest fetched from Artifact
      Registry by digest, and only after the build receipt the index names for
      that image says it pushed exactly that digest. Without the build the check
      would be circular, since a digest that hashes the manifest it was used to
      fetch proves only that the registry holds that manifest;
    - ``policy`` and ``deployment``: the stored policy and binding objects, read
      back as the preimage of their digests, and only when the stored bytes are
      the canonical bytes the digest was minted over;
    - ``asset:<path>``: the bytes the tagged revision serves for that path,
      fetched without a credential;
    - ``object:...``: as ``read``;
    - the references listed in ``evidence_objects``, which under
      ``42_staging_release_v3`` include the raw authority objects and the source
      binding: the uploaded object each binding pins by generation.
    """

    def __init__(self, adapter, objects):
        self.adapter = adapter
        self.objects = objects

    def read(self, reference):
        if not isinstance(reference, str):
            return None
        if reference.startswith("source:"):
            return self._source(reference[len("source:") :])
        if not reference.startswith("object:"):
            return None
        uri, marker, generation = reference[len("object:") :].rpartition("#")
        if not marker or not uri.startswith("gs://") or not generation.isdigit():
            return None
        bucket, slash, name = uri[len("gs://") :].partition("/")
        if not slash or not bucket or not name:
            return None
        stored = self.objects._read_from(bucket, name, generation)
        return None if stored is None else stored["raw"]

    def _source(self, url):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.netloc not in SOURCE_HOSTS:
            return None
        return self._anonymous("fetch_source", url)

    def _anonymous(self, call, url):
        status, payload = self.adapter.perform(call, url=url)
        if status == 404:
            return None
        self.adapter.require_ok(call, status, payload)
        return bytes(payload)

    def reader(self, index, *, asset_origin):
        return _BoundReader(self, index, asset_origin).read


def _unique_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = item
    return value


class _BoundReader:
    def __init__(self, unbound, index, asset_origin):
        self.unbound = unbound
        self.adapter = unbound.adapter
        self.objects = unbound.objects
        self.index = index
        self.asset_origin = asset_origin.rstrip("/")
        self.builds = {}

    def read(self, reference):
        if not isinstance(reference, str):
            return None
        if reference.startswith(("object:", "source:")):
            return self.unbound.read(reference)
        evidence = self.index.get("evidence_objects")
        if isinstance(evidence, dict) and reference in evidence:
            item = evidence[reference]
            return self.unbound.read(f"object:{item['uri']}#{item['generation']}")
        if reference == "commit":
            return self._commit()
        if reference in ("engine_image", "app_image"):
            field = "engine_build" if reference == "engine_image" else "app_build"
            if reference == "engine_image":
                self._engine_commit()
            return self._manifest(self.index[reference], self.index[field])
        if reference == "policy":
            return self._preimage(
                release.POLICY_OBJECT_NAME.format(
                    policy_digest=self.index["policy_digest"]
                ),
                "policy_digest",
            )
        if reference == "deployment":
            return self._preimage(
                release.BINDING_OBJECT_NAME.format(
                    deployment_digest=self.index["deployment_digest"]
                ),
                "deployment_digest",
            )
        if reference.startswith("asset:"):
            return self._asset(reference[len("asset:") :])
        return None

    def _build(self, name, image_reference):
        """The build receipt for one image, read once per reader and accepted
        only when it succeeded in the project and pushed that exact digest."""
        if name not in self.builds:
            self.builds[name] = self.adapter.get("get_build", name=name)
        build = self.builds[name]
        if build is None:
            return None
        release._require(
            build.get("projectId") == release.PROJECT,
            "release_index_build_project_mismatch",
        )
        release._require(
            build.get("status") == "SUCCESS", "release_index_build_not_successful"
        )
        images = (build.get("results") or {}).get("images") or []
        digests = {item.get("digest") for item in images if isinstance(item, dict)}
        expected = "sha256:" + image_reference.rpartition("@sha256:")[2]
        release._require(expected in digests, "release_index_build_image_mismatch")
        return build

    @staticmethod
    def _revision(build):
        revision = ((build.get("source") or {}).get("connectedRepository") or {}).get(
            "revision"
        )
        if not isinstance(revision, str) or not revision:
            return None
        return revision

    def _commit(self):
        build = self._build(self.index["app_build"], self.index["app_image"])
        if build is None:
            return None
        revision = self._revision(build)
        if revision is None:
            return None
        return revision.encode("ascii", "strict")

    def _engine_commit(self):
        """The engine build is tied to the release commit here as well as when
        the index is built, so an index edited afterwards to name an engine
        build of another commit does not verify. The commit is the index's
        own, which the app build receipt independently confirms."""
        build = self._build(self.index["engine_build"], self.index["engine_image"])
        if build is None:
            return
        release._require(
            self._revision(build) == self.index["commit"],
            "engine_build_commit_mismatch",
        )

    def _manifest(self, reference, build_name):
        image, _, digest = reference.rpartition("@sha256:")
        if not image.startswith(release.IMAGE_REGISTRY) or len(digest) != 64:
            return None
        # The registry host is fixed by that prefix, but the repository path is
        # still text from the index, so a dot segment in it must not reach a URL.
        if any(part in ("", ".", "..") for part in image.split("/")[1:]):
            return None
        # The build receipt is read first: it is the one source outside the index
        # that says this digest was ever pushed, and a refusal there must stop
        # the fetch rather than follow it.
        if self._build(build_name, reference) is None:
            return None
        status, payload = self.adapter.perform(
            "image_manifest", image=image, digest=digest
        )
        if status == 404:
            return None
        self.adapter.require_ok("image_manifest", status, payload)
        return bytes(payload)

    def _preimage(self, name, digest_field):
        """The stored object's own bytes must be the canonical bytes its digest
        was minted over. Parsing and recanonicalising instead would accept a
        pretty printed, escape rewritten, whitespace padded or duplicate key
        object as the object the digest names, which it is not."""
        stored = self.objects.read(name)
        if stored is None:
            return None
        raw = bytes(stored["raw"])
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_keys)
        except (UnicodeError, ValueError):
            return None
        if type(value) is not dict or digest_field not in value:
            return None
        if release.canonical_bytes(value) != raw:
            return None
        rest = {key: item for key, item in value.items() if key != digest_field}
        return release.canonical_bytes(rest)

    def _asset(self, path):
        parts = urllib.parse.urlsplit(path)
        if parts.scheme or parts.netloc or path.startswith("/"):
            return None
        segments = path.split("/")
        if any(segment in ("", ".", "..") for segment in segments):
            return None
        url = self.asset_origin + "/" + urllib.parse.quote(path, safe="/-._~")
        return self.unbound._anonymous("fetch_asset", url)


class _Clock:
    def now(self):
        return datetime.now(UTC)

    def monotonic(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(float(seconds))


def build(state, *, transport=None):
    adapter = NativeAdapter(state, transport=transport)
    objects = _Objects(adapter, BUCKET)
    return {
        "run": _Run(adapter),
        "tasks": _Tasks(adapter),
        "objects": objects,
        "bytes": _Bytes(adapter, objects),
        "clock": _Clock(),
        "_native": adapter,
    }


def factory(state):
    return build(state)
