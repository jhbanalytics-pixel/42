"""The browser journey's test only seams: off by default, and impossible to turn on in a deployment.

The seams replace the Google verifier, the bucket and the PDF renderer in a
local process the browser test starts. These tests prove the boundary around
them: production code never reaches the module, the image and the source
upload never carry it, the pinned verifier still refuses the credential the
fake provider signs, and install() refuses in any process that looks deployed.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Forbidden

from src.api import main
from tests.journey import dossier_journey_seams as seams

APP_ROOT = Path(__file__).resolve().parents[2]
KEY = bytes(range(32))


def claims(**overrides):
    now = int(time.time())
    value = {
        "iss": "https://accounts.google.com",
        "aud": seams.TEST_AUDIENCE,
        "sub": "100000000000000000042",
        "hd": seams.TEST_HOSTED_DOMAIN,
        "nonce": "n" * 43,
        "iat": now,
        "exp": now + 600,
    }
    value.update(overrides)
    return value


def modules():
    return SimpleNamespace(
        main=SimpleNamespace(
            desk=SimpleNamespace(build_desk_payload=None),
            general_question_routes=SimpleNamespace(coverage_route=None),
        ),
        workspace_scope=SimpleNamespace(),
        pdf_exporter=SimpleNamespace(),
        dossier_artifacts=SimpleNamespace(),
    )


def test_no_production_module_reaches_the_seams_or_reads_a_journey_setting():
    for path in sorted((APP_ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "tests.journey" not in text, path
        assert "tests/journey" not in text, path
        assert "DOSSIER_JOURNEY" not in text, path
        assert "dossier_journey" not in text, path


def test_the_image_and_the_source_upload_never_carry_the_tests_directory():
    dockerfile = (APP_ROOT / "Dockerfile.general-question").read_text(encoding="utf-8")
    copied_into_app = re.findall(r"^COPY\s+(?:--\S+\s+)*(\S+)\s+(/app/\S*)$", dockerfile, re.M)
    assert copied_into_app, "the image copies nothing into /app"
    assert not [source for source, _target in copied_into_app if "tests" in source]
    assert ("lp/src/", "/app/src/") in copied_into_app
    ignored = (APP_ROOT / ".gcloudignore").read_text(encoding="utf-8").splitlines()
    assert "tests/" in ignored


def test_the_pinned_verifier_refuses_the_fake_providers_credential(monkeypatch):
    from google.oauth2 import id_token

    # Certificates published under the test key id, so the refusal cannot be
    # a missing key: the pinned verifier does not accept an HMAC signature.
    monkeypatch.setattr(
        id_token, "_fetch_certs", lambda request, url: {seams.TEST_KEY_ID: "not a certificate"}
    )
    credential = seams.sign_test_credential(claims(), KEY)
    assert main._verify_review_credential.__module__ == "src.api.main"
    with pytest.raises(ValueError):
        main._verify_review_credential(credential, seams.TEST_AUDIENCE)


@pytest.mark.parametrize("marker", seams.DEPLOYED_MARKERS)
def test_install_refuses_in_a_deployed_process_and_changes_nothing(marker):
    target = modules()
    before = repr(target)
    with pytest.raises(seams.JourneySeamRefused):
        seams.install(
            main=target.main,
            workspace_scope=target.workspace_scope,
            pdf_exporter=target.pdf_exporter,
            dossier_artifacts=target.dossier_artifacts,
            key=KEY,
            bucket=seams.GrantedBucket(),
            renderer=object(),
            font=b"face",
            environ={marker: "open-intelligence-staging"},
        )
    assert repr(target) == before


def test_install_refuses_a_short_key_and_puts_every_stand_in_in_place_locally():
    target = modules()
    before = repr(target)
    with pytest.raises(seams.JourneySeamRefused):
        seams.install(
            main=target.main,
            workspace_scope=target.workspace_scope,
            pdf_exporter=target.pdf_exporter,
            dossier_artifacts=target.dossier_artifacts,
            key=b"short",
            bucket=seams.GrantedBucket(),
            renderer=object(),
            font=b"face",
            environ={},
        )
    assert repr(target) == before
    bucket = seams.GrantedBucket()
    renderer = object()
    seams.install(
        main=target.main,
        workspace_scope=target.workspace_scope,
        pdf_exporter=target.pdf_exporter,
        dossier_artifacts=target.dossier_artifacts,
        key=KEY,
        bucket=bucket,
        renderer=renderer,
        font=b"face",
        environ={"DEPLOYMENT_PROFILE": " "},
    )
    assert target.dossier_artifacts._export_font_bytes() == b"face"
    assert target.main._new_investigation_storage_client().bucket(bucket.name) is bucket
    with pytest.raises(ValueError):
        target.main._new_investigation_storage_client().bucket("another-bucket")
    assert target.workspace_scope._workspace_bucket() is bucket
    assert target.pdf_exporter.render_stored_html_pdf is renderer
    credential = seams.sign_test_credential(claims(), KEY)
    assert target.main._verify_review_credential(credential, seams.TEST_AUDIENCE)["sub"] == (
        "100000000000000000042"
    )


def test_the_test_verifier_accepts_only_its_own_unexpired_credential_for_the_audience():
    verify = seams.make_test_verifier(KEY)
    value = claims()
    good = seams.sign_test_credential(value, KEY)
    assert verify(good, seams.TEST_AUDIENCE) == value
    head, body, signature = good.split(".")
    forged_body = seams._b64(json.dumps(claims(sub="100000000000000000099")).encode())
    none_head = seams._b64(json.dumps({"alg": "none", "kid": seams.TEST_KEY_ID, "typ": "JWT"}).encode())
    refused = [
        seams.sign_test_credential(claims(), bytes(reversed(KEY))),
        seams.sign_test_credential(claims(aud="another-client"), KEY),
        seams.sign_test_credential(claims(iss="https://issuer.test"), KEY),
        seams.sign_test_credential(claims(exp=int(time.time()) - 1), KEY),
        seams.sign_test_credential(claims(exp=str(int(time.time()) + 600)), KEY),
        f"{head}.{forged_body}.{signature}",
        f"{none_head}.{body}.",
        f"{head}.{body}",
        "",
    ]
    for credential in refused:
        with pytest.raises(ValueError):
            verify(credential, seams.TEST_AUDIENCE)


def test_the_bucket_holds_the_store_to_create_read_and_list():
    bucket = seams.GrantedBucket()
    bucket.blob("a/one.json").upload_from_string(b"1", content_type="application/json", if_generation_match=0)
    assert bucket.get_blob("a/one.json").download_as_bytes() == b"1"
    assert [blob.name for blob in bucket.list_blobs(prefix="a/")] == ["a/one.json"]
    with pytest.raises(Forbidden):
        bucket.blob("a/one.json").upload_from_string(
            b"2", content_type="application/json", if_generation_match=None
        )
    assert bucket.get_blob("a/one.json").download_as_bytes() == b"1"
    assert bucket.summary() == {"names": ["a/one.json"], "overwrites": ["a/one.json"], "uploads": 2}


def test_the_local_face_must_be_the_approved_one(tmp_path):
    from src.api import pdf_exporter

    (tmp_path / "newsreader-variable-tcB4qQtO.woff2").write_bytes(b"another face")
    with pytest.raises(seams.JourneySeamRefused):
        seams.approved_face(pdf_exporter, tmp_path)
