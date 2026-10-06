"""Test only seams for the dossier browser journey, and nothing else.

The journey runs the real FastAPI app with real API and store code. Three
things cannot run on a test host, so each is stood in for here, and only by a
caller that installs these seams into a local process on purpose:

* Google sign in. The page gets a fake provider from the browser test, and the
  server gets a verifier that accepts only a credential the fake signed with a
  key the test generated for this run. The real verifier is replaced in the
  local process only; production code has no switch that reaches this module,
  and this module is under tests/, which the image and the source upload
  exclude.
* Cloud Storage. The bucket is the object creator stand-in that holds the
  staging serving identity to its two grants: create a free name, read and
  list. Replacing an object is refused. A lock makes it safe for the server's
  worker threads.
* The pinned PDF renderer, which needs the packaged browser and fonts of the
  final image. The existing renderer double is used instead.

install() refuses to run in a deployed process, so none of this can be turned
on where the real service runs, whatever the environment says.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path

from tests.unit.object_creator_bucket import ObjectCreatorBucket

TEST_KEY_ID = "dossier-journey-test-only"
TEST_ISSUER = "https://accounts.google.com"
TEST_AUDIENCE = "dossier-journey.apps.test"
TEST_HOSTED_DOMAIN = "journey.test"
MIN_KEY_BYTES = 32

# Any of these names in the environment marks a deployed process: Cloud Run
# sets the first three, and the deploy sets the profile.
DEPLOYED_MARKERS = ("K_SERVICE", "K_REVISION", "K_CONFIGURATION", "DEPLOYMENT_PROFILE")


class JourneySeamRefused(RuntimeError):
    """The seams were asked to install where they must never run."""


def refuse_deployed(environ) -> None:
    present = [name for name in DEPLOYED_MARKERS if environ.get(name, "").strip()]
    if present:
        raise JourneySeamRefused(
            "test only seams refuse to install in a deployed process: " + ", ".join(present)
        )


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _checked_key(key: bytes) -> bytes:
    if type(key) is not bytes or len(key) < MIN_KEY_BYTES:
        raise JourneySeamRefused("the journey signing key must be at least 32 random bytes")
    return key


def sign_test_credential(claims: dict, key: bytes) -> str:
    """A compact HS256 token under the test key id; the browser fake mints the same shape."""
    header = {"alg": "HS256", "kid": TEST_KEY_ID, "typ": "JWT"}
    signing_input = (
        _b64(json.dumps(header, separators=(",", ":")).encode("utf-8"))
        + "."
        + _b64(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    )
    signature = hmac.new(_checked_key(key), signing_input.encode("ascii"), hashlib.sha256)
    return signing_input + "." + _b64(signature.digest())


def make_test_verifier(key: bytes, *, clock=time.time):
    """The verifier the local server uses in place of the pinned Google one.

    It does the pinned verifier's part of the contract, signature, audience,
    issuer and expiry, for tokens under the test key id only, and returns the
    claims. Everything after that (hosted domain, allowlist, nonce binding) is
    the app's own unchanged code.
    """
    key = _checked_key(key)

    def verify(credential: str, audience: str) -> dict:
        parts = credential.split(".") if type(credential) is str else []
        if len(parts) != 3:
            raise ValueError("credential is not a compact token")
        try:
            header = json.loads(_unb64(parts[0]))
            claims = json.loads(_unb64(parts[1]))
            signature = _unb64(parts[2])
        except ValueError:
            raise ValueError("credential is not readable") from None
        if header != {"alg": "HS256", "kid": TEST_KEY_ID, "typ": "JWT"}:
            raise ValueError("credential is not a journey test credential")
        expected = hmac.new(
            key, (parts[0] + "." + parts[1]).encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("credential signature is invalid")
        if type(claims) is not dict or claims.get("aud") != audience:
            raise ValueError("credential audience is wrong")
        if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            raise ValueError("credential issuer is wrong")
        expires = claims.get("exp")
        if type(expires) is not int or expires <= clock():
            raise ValueError("credential has expired")
        return claims

    return verify


class GrantedBucket(ObjectCreatorBucket):
    """The object creator stand-in, safe for the server's worker threads."""

    def __init__(self):
        super().__init__(taken="precondition")
        self.lock = threading.RLock()

    def blob(self, name):
        return _LockedBlob(self, super().blob(name))

    def get_blob(self, name):
        with self.lock:
            found = super().get_blob(name)
        return None if found is None else _LockedBlob(self, found)

    def list_blobs(self, *, prefix, max_results=None, delimiter=None):
        with self.lock:
            listed = super().list_blobs(
                prefix=prefix, max_results=max_results, delimiter=delimiter
            )
            return [_LockedBlob(self, item) for item in listed]

    def summary(self) -> dict:
        with self.lock:
            return {
                "names": sorted(self.objects),
                "overwrites": list(self.overwrites),
                "uploads": len(self.uploads),
            }


class _LockedBlob:
    def __init__(self, bucket, blob):
        self._bucket = bucket
        self._blob = blob

    def __getattr__(self, name):
        return getattr(self._blob, name)

    def download_as_bytes(self, **kwargs):
        with self._bucket.lock:
            return self._blob.download_as_bytes(**kwargs)

    def upload_from_string(self, data, **kwargs):
        with self._bucket.lock:
            return self._blob.upload_from_string(data, **kwargs)


class _StorageClient:
    def __init__(self, bucket):
        self._bucket = bucket

    def bucket(self, name):
        if name != self._bucket.name:
            raise ValueError("only the staging cache bucket is granted")
        return self._bucket


def review_environment(subject: str) -> dict:
    """The review configuration: one reviewer who holds all four roles, in list form."""
    return {
        "REVIEW_OAUTH_AUDIENCE": TEST_AUDIENCE,
        "REVIEW_OAUTH_ISSUER": TEST_ISSUER,
        "REVIEW_HOSTED_DOMAIN": TEST_HOSTED_DOMAIN,
        "REVIEW_SUBJECT_ALLOWLIST": json.dumps(
            {
                subject: [
                    "dossier_editor",
                    "claim_approver",
                    "relationship_approver",
                    "client_read_approver",
                ]
            }
        ),
    }


UNAVAILABLE_COVERAGE = {
    "contract_version": "general_question_coverage_v1",
    "state": "unavailable",
    "window": None,
    "cutoff_date": None,
}
EMPTY_DESK = {"topics": [], "updated": None, "freshness": {"status": "unknown", "age_hours": None}}


async def _unavailable_coverage(request):
    return dict(UNAVAILABLE_COVERAGE)


def approved_face(pdf_exporter, assets_root) -> bytes:
    """The approved Newsreader face from a local build, at the digest the renderer pins."""
    config = pdf_exporter._load_runtime_config()
    name = "newsreader-variable-tcB4qQtO.woff2"
    data = (Path(assets_root) / name).read_bytes()
    if hashlib.sha256(data).hexdigest() != config["assets"]["fonts"][name]["sha256"]:
        raise JourneySeamRefused("the local build does not carry the approved face")
    return data


def install(
    *,
    main,
    workspace_scope,
    pdf_exporter,
    dossier_artifacts,
    key: bytes,
    bucket,
    renderer,
    font: bytes,
    environ=None,
):
    """Put the stand-ins into one local process. Refused in a deployed process."""
    refuse_deployed(os.environ if environ is None else environ)
    verifier = make_test_verifier(key)
    # The export embeds the approved face, which a deployed process reads from
    # the renderer's asset root in the image and a local one from its build.
    dossier_artifacts._export_font_bytes = lambda: font
    # The shell around the console also reads the desk (BigQuery) and the Ask
    # coverage (the question worker bundle). Neither is part of this journey
    # and neither exists on a test host, so both answer as empty.
    main.desk.build_desk_payload = lambda region: dict(EMPTY_DESK)
    main._ingest_cache_key = lambda: "0:0:0"
    main.general_question_routes.coverage_route = _unavailable_coverage
    client = _StorageClient(bucket)
    main._new_investigation_storage_client = lambda: client
    workspace_scope._workspace_bucket = lambda: bucket
    main._verify_review_credential = verifier
    pdf_exporter.render_stored_html_pdf = renderer
    # The deployed runtime check reads the Cloud Run identity from the metadata
    # server, which a local process does not have.
    main._validate_investigation_runtime = lambda: None
    return verifier
