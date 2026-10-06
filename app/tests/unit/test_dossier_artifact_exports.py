"""The artifact's two exports: stored HTML, a PDF made from those same bytes, and clean metadata."""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
from pathlib import Path

import pytest
from pypdf import PdfReader

from src.api import (
    ask_export,
    client_purpose,
    dossier_artifacts,
    dossier_resolver,
    dossier_store,
    intelligence_dossier,
    pdf_exporter,
)
import base64

from tests.unit.dossier_pdf_fakes import (
    FAKE_FONT,
    TOOL_NAMES,
    install_font,
    page_text,
    synthetic_pdf,
)
from tests.unit.test_dossier_resolver import dossier
from tests.unit.test_dossier_store import PREFIX

INVESTIGATION = "inv_fixture"
SCOPE = "a" * 64
VERSION = "b" * 64
QUESTION = "Which emerging behaviour should the brand act on in six weeks?"


@pytest.fixture(autouse=True)
def export_font(monkeypatch):
    install_font(monkeypatch)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def client_read_payload() -> dict:
    return {
        "contract_version": "intelligence_dossier_v1",
        "investigation_id": INVESTIGATION,
        "concise_answer": "Repair is becoming a weekend social activity.",
        "claims": [
            {
                "claim_id": "clm_1",
                "kind": "observation",
                "text": "Repair tutorials recur across independent sources.",
                "citations": ["gqctx_one"],
            }
        ],
        "evidence": [
            {"evidence_id": "gqctx_one", "published_at": "2026-08-20T00:00:00Z"}
        ],
        "excluded_count": 3,
        "excluded_reasons": ["claim status is pending, not approved"],
        "artifact_readiness": {"state": "approval_required", "failed": ["decision"]},
    }


def rendered(html_bytes: bytes) -> bytes:
    return synthetic_pdf(page_text(html_bytes))


def build(render=rendered, policy=None, client_read=None, title=QUESTION):
    return dossier_artifacts.build_artifact_exports(
        investigation_id=INVESTIGATION,
        scope_digest=SCOPE,
        dossier_version=VERSION,
        selected_claim_ids=["clm_1"],
        client_read=client_read or client_read_payload(),
        purpose_policy=policy,
        render_pdf=render,
        title=title,
    )


def metadata_values(data: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(data), strict=True)
    values = [f"{key} {value}" for key, value in (reader.metadata or {}).items()]
    if "/Metadata" in reader.trailer["/Root"]:
        values.append(reader.trailer["/Root"]["/Metadata"].get_object().get_data().decode("latin-1"))
    return values


def extracted_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data), strict=True)
    return " ".join(" ".join(page.extract_text().split()) for page in reader.pages)


def allowed_lines(client_read: dict, title: str | None = QUESTION) -> list[str]:
    """Every text run the Client Read payload may put on the page, in page order.

    The investigation is named by its question, and by its id only when no
    question is known. Dates are written the way the app writes them. There is
    no readiness line: readiness is sealed when the artifact is prepared, so
    after approval it could only state something no longer true.
    """
    lines = ["Client Read", title or "Investigation " + client_read["investigation_id"]]
    if client_read["concise_answer"]:
        lines += ["Answer", client_read["concise_answer"]]
    if client_read["claims"]:
        lines.append("Findings")
        for claim in client_read["claims"]:
            lines += [claim["kind"], claim["text"]]
            if claim["citations"]:
                lines.append("Sources: " + ", ".join(claim["citations"]))
    if client_read["evidence"]:
        lines.append("Evidence")
        for item in client_read["evidence"]:
            lines += [item["evidence_id"], ask_export._date_text(item["published_at"])]
    if client_read["excluded_count"]:
        lines.append(f"{client_read['excluded_count']} item(s) withheld from this read.")
    return lines


# The stored HTML is the PDF's input


def test_the_stored_html_is_standalone_and_renderable_by_the_reviewed_path():
    html = dossier_artifacts.render_html(client_read_payload(), title=QUESTION)
    lowered = html.lower()
    assert "<title" not in lowered
    assert "generator" not in lowered
    assert "http" not in lowered
    # The face travels inside the file, so a browser opening the download or
    # the Client Read frame loads it without a request. The asset name after
    # it is the copy the reviewed renderer resolves in its approved root; a
    # browser that loaded the first source never asks for the second.
    embedded = "data:font/woff2;base64," + base64.b64encode(FAKE_FONT).decode("ascii")
    assert (
        "@font-face{font-family:Newsreader;src:url('" + embedded + "') format('woff2'),"
        "url('newsreader-variable-tcB4qQtO.woff2') format('woff2')}"
    ) in html
    assert re.findall(r"url\('([^']*)'\)", html) == [embedded, "newsreader-variable-tcB4qQtO.woff2"]
    assert "font-family:newsreader," in lowered
    assert "<ol" not in lowered and "<ul" not in lowered
    assert page_text(html.encode("utf-8")) == allowed_lines(client_read_payload())


@pytest.mark.parametrize("state", ["approval_required", "client_ready", "blocked"])
def test_the_export_carries_no_readiness_line_that_would_be_stale_once_approved(state):
    payload = client_read_payload()
    payload["artifact_readiness"] = {
        "state": state,
        "failed": [] if state == "client_ready" else ["decision"],
    }
    html = dossier_artifacts.render_html(payload, title=QUESTION)
    runs = page_text(html.encode("utf-8"))
    assert "Readiness" not in runs
    for code in ("approval_required", "client_ready", "blocked", "decision"):
        assert code not in html


def test_the_export_names_the_investigation_by_its_question_and_writes_dates_as_words():
    html = dossier_artifacts.render_html(client_read_payload(), title=QUESTION)
    runs = page_text(html.encode("utf-8"))
    assert runs[1] == QUESTION
    assert INVESTIGATION not in html
    assert "20 Aug 2026" in runs
    assert "2026-08-20" not in html
    escaped = dossier_artifacts.render_html(client_read_payload(), title="<b>Tom & Jerry</b>?")
    assert "&lt;b&gt;Tom &amp; Jerry&lt;/b&gt;?" in escaped
    untitled = dossier_artifacts.render_html(client_read_payload())
    assert page_text(untitled.encode("utf-8"))[1] == "Investigation " + INVESTIGATION


def test_the_export_face_is_read_only_from_the_approved_root_at_its_pinned_digest(
    monkeypatch, tmp_path
):
    monkeypatch.undo()
    config = pdf_exporter._load_runtime_config()
    name = "newsreader-variable-tcB4qQtO.woff2"
    monkeypatch.setattr(
        pdf_exporter,
        "_load_runtime_config",
        lambda: dict(config, assets=dict(config["assets"], root=str(tmp_path))),
    )
    dossier_artifacts._export_font_bytes.cache_clear()
    with pytest.raises(dossier_artifacts.ExportFontUnavailable):
        dossier_artifacts._export_font_bytes()
    (tmp_path / name).write_bytes(b"not the approved face")
    with pytest.raises(dossier_artifacts.ExportFontUnavailable):
        dossier_artifacts._export_font_bytes()
    with pytest.raises(dossier_artifacts.ExportFontUnavailable):
        dossier_artifacts.render_html(client_read_payload(), title=QUESTION)
    with pytest.raises(dossier_artifacts.ExportFontUnavailable):
        build()
    font = _approved_font()
    if font is None:
        pytest.skip("the approved face is not on this host")
    shutil.copyfile(font, tmp_path / name)
    assert dossier_artifacts._export_font_bytes() == font.read_bytes()
    dossier_artifacts._export_font_bytes.cache_clear()


def test_prepared_exports_bind_both_digests_before_any_approval():
    seen = []

    def render(html_bytes):
        seen.append(html_bytes)
        return rendered(html_bytes)

    built = build(render)
    artifact, pdf = built["artifact"], built["pdf"]
    assert seen == [artifact["html"].encode("utf-8")]
    exports = artifact["manifest"]["export_digests"]
    assert exports == {
        "html": sha256(artifact["html"].encode("utf-8")),
        "client_read": sha256(dossier_artifacts.record_bytes(artifact["client_read"])),
        "pdf": sha256(pdf),
    }
    assert artifact["contract_version"] == "dossier_artifact_v1"
    assert artifact["artifact_id"] == "art_" + artifact["artifact_version"][:16]
    assert dossier_artifacts.parse_artifact(artifact) == artifact
    decision = dict(artifact["manifest"], state="approved")
    assert dossier_artifacts.is_artifact_approved(
        manifest=artifact["manifest"], decision=decision
    )
    moved = dict(exports, pdf="0" * 64)
    assert not dossier_artifacts.is_artifact_approved(
        manifest=artifact["manifest"], decision=dict(decision, export_digests=moved)
    )


def test_a_manifest_whose_pdf_digest_moved_is_not_an_artifact():
    artifact = build()["artifact"]
    forged = dict(artifact)
    forged["manifest"] = dict(
        artifact["manifest"],
        export_digests=dict(artifact["manifest"]["export_digests"], pdf="0" * 64),
    )
    with pytest.raises(dossier_artifacts.ArtifactInvalid):
        dossier_artifacts.parse_artifact(forged)


def test_the_pdf_metadata_names_no_tool():
    raw = rendered(dossier_artifacts.render_html(client_read_payload()).encode("utf-8"))
    assert any("Skia" in value for value in metadata_values(raw))
    pdf = build()["pdf"]
    values = " ".join(metadata_values(pdf)).lower()
    assert not [name for name in TOOL_NAMES if name in values]
    assert "/producer" not in values and "/creator" not in values
    assert extracted_text(pdf) == " ".join(allowed_lines(client_read_payload()))


def test_a_renderer_that_returns_no_pdf_seals_nothing():
    with pytest.raises(dossier_artifacts.ArtifactInvalid):
        build(lambda html_bytes: b"<html>not a pdf</html>")
    with pytest.raises(dossier_artifacts.ArtifactInvalid):
        dossier_artifacts.scrub_pdf_metadata(b"")


def test_a_renderer_refusal_propagates_before_anything_is_sealed():
    def refuse(html_bytes):
        raise pdf_exporter.PdfExportError("pdf_browser_unavailable")

    with pytest.raises(pdf_exporter.PdfExportError):
        build(refuse)


# The synthetic PDF writes a standard Latin-1 font, so the variants that reach
# the PDF are Latin-1 ones; full width and zero width variants are proved on
# the payload, which is scanned before anything is rendered.
@pytest.mark.parametrize("variant", ["Election", "ELECTION", "elec\xadtion"])
def test_a_prohibited_term_surviving_into_the_pdf_blocks_the_export(variant):
    policy = client_purpose.load_client_purpose_policy("bsa")
    build(policy=policy)

    def leaking(html_bytes):
        return synthetic_pdf([*page_text(html_bytes), f"The {variant} desk."])

    with pytest.raises(client_purpose.ClientPurposeProhibited):
        build(leaking, policy=policy)

    def leaking_metadata(html_bytes):
        return synthetic_pdf(page_text(html_bytes), title=f"{variant} brief")

    # The Info dictionary is removed before the scan, so a title cannot carry
    # a term out; the scan still runs over what remains.
    assert build(leaking_metadata, policy=policy)["pdf"]


@pytest.mark.parametrize(
    "variant",
    [
        "\uff25\uff2c\uff25\uff23\uff34\uff29\uff2f\uff2e",
        "elec\u200btion",
        "ELEC\u2060TION",
    ],
)
def test_a_unicode_variant_in_the_client_read_is_refused_before_rendering(variant):
    policy = client_purpose.load_client_purpose_policy("bsa")
    assert client_purpose.evaluate_client_purpose(policy, [variant])["state"] == "permitted"
    payload = client_read_payload()
    payload["claims"][0]["text"] = f"Coverage of the {variant}."
    calls = []
    with pytest.raises(client_purpose.ClientPurposeProhibited):
        build(lambda html_bytes: calls.append(html_bytes), policy=policy, client_read=payload)
    assert calls == []


# The PDF object beside the artifact record


class Bucket:
    """The smallest generation-aware double the create-once PDF writer needs."""

    def __init__(self):
        self.objects = {}
        self.content_types = {}
        self.counter = 0

    def blob(self, name):
        bucket = self

        class Blob:
            generation = None

            def upload_from_string(self, data, *, content_type, if_generation_match):
                from google.api_core.exceptions import PreconditionFailed

                current = bucket.objects.get(name)
                if if_generation_match != (current[1] if current else 0):
                    raise PreconditionFailed("precondition")
                bucket.counter += 1
                bucket.objects[name] = (data, bucket.counter)
                bucket.content_types[name] = content_type
                self.generation = bucket.counter

        return Blob()

    def get_blob(self, name):
        if name not in self.objects:
            return None
        bucket = self

        class Blob:
            generation = bucket.objects[name][1]

            def download_as_bytes(self, *, if_generation_match=None):
                return bucket.objects[name][0]

        return Blob()

    def list_blobs(self, *, prefix):
        return [
            type("Listed", (), {"name": name})()
            for name in sorted(self.objects)
            if name.startswith(prefix)
        ]


def real_investigation():
    value = dossier()
    return value["investigation_id"]


def _built_pdf(investigation_id):
    payload = dict(client_read_payload(), investigation_id=investigation_id)
    built = dossier_artifacts.build_artifact_exports(
        investigation_id=investigation_id,
        scope_digest=SCOPE,
        dossier_version=VERSION,
        selected_claim_ids=["clm_1"],
        client_read=payload,
        purpose_policy=None,
        render_pdf=rendered,
    )
    return built["artifact"], built["pdf"]


@pytest.mark.parametrize("taken", ["precondition", "forbidden"])
def test_an_identical_pdf_retry_on_the_staging_grants_reads_back(taken):
    from tests.unit.object_creator_bucket import ObjectCreatorBucket

    bucket = ObjectCreatorBucket(taken)
    artifact, pdf = _built_pdf(real_investigation())
    written = dossier_artifacts.create_artifact_pdf(
        bucket=bucket, prefix=PREFIX, artifact=artifact, pdf=pdf
    )
    assert bucket.content_types[written["object_name"]] == "application/pdf"
    again = dossier_artifacts.create_artifact_pdf(
        bucket=bucket, prefix=PREFIX, artifact=artifact, pdf=pdf
    )
    assert (written["created"], again["created"]) == (True, False)
    assert again["generation"] == written["generation"]
    assert bucket.overwrites == []


def test_a_pdf_the_identity_may_not_create_is_storage_unavailable():
    from tests.unit.object_creator_bucket import ObjectCreatorBucket

    bucket = ObjectCreatorBucket("forbidden")
    bucket.create_denied = True
    artifact, pdf = _built_pdf(real_investigation())
    with pytest.raises(dossier_store.DossierStoreError) as caught:
        dossier_artifacts.create_artifact_pdf(
            bucket=bucket, prefix=PREFIX, artifact=artifact, pdf=pdf
        )
    assert caught.value.code == "dossier_store_unavailable"


def test_the_pdf_is_stored_once_beside_the_record_and_read_back_by_digest():
    bucket = Bucket()
    investigation_id = real_investigation()
    payload = dict(client_read_payload(), investigation_id=investigation_id)
    built = dossier_artifacts.build_artifact_exports(
        investigation_id=investigation_id,
        scope_digest=SCOPE,
        dossier_version=VERSION,
        selected_claim_ids=["clm_1"],
        client_read=payload,
        purpose_policy=None,
        render_pdf=rendered,
    )
    artifact, pdf = built["artifact"], built["pdf"]
    with pytest.raises(dossier_artifacts.ArtifactInvalid):
        dossier_artifacts.create_artifact_pdf(
            bucket=bucket, prefix=PREFIX, artifact=artifact, pdf=pdf + b"\n"
        )
    assert bucket.objects == {}
    written = dossier_artifacts.create_artifact_pdf(
        bucket=bucket, prefix=PREFIX, artifact=artifact, pdf=pdf
    )
    name = f"{PREFIX}artifacts/{investigation_id}/{artifact['artifact_id']}.pdf"
    assert written["object_name"] == name
    assert bucket.content_types[name] == "application/pdf"
    again = dossier_artifacts.create_artifact_pdf(
        bucket=bucket, prefix=PREFIX, artifact=artifact, pdf=pdf
    )
    assert again["created"] is False
    assert (
        dossier_artifacts.read_artifact_pdf(bucket=bucket, prefix=PREFIX, artifact=artifact)
        == pdf
    )
    bucket.objects[name] = (pdf + b"%tampered", bucket.objects[name][1])
    with pytest.raises(dossier_artifacts.ArtifactInvalid):
        dossier_artifacts.read_artifact_pdf(bucket=bucket, prefix=PREFIX, artifact=artifact)
    del bucket.objects[name]
    assert (
        dossier_artifacts.read_artifact_pdf(bucket=bucket, prefix=PREFIX, artifact=artifact)
        is None
    )


def test_a_prepared_artifact_is_found_again_by_its_content_without_rendering():
    bucket = Bucket()
    investigation_id = real_investigation()
    payload = dict(client_read_payload(), investigation_id=investigation_id)
    built = dossier_artifacts.build_artifact_exports(
        investigation_id=investigation_id,
        scope_digest=SCOPE,
        dossier_version=VERSION,
        selected_claim_ids=["clm_1"],
        client_read=payload,
        purpose_policy=None,
        render_pdf=rendered,
    )
    artifact = built["artifact"]
    html = dossier_artifacts.render_html(payload)
    lookup = dict(
        bucket=bucket,
        prefix=PREFIX,
        investigation_id=investigation_id,
        scope_digest=SCOPE,
        dossier_version=VERSION,
        selected_claim_ids=["clm_1"],
        client_read=payload,
        html=html,
    )
    assert dossier_artifacts.find_prepared_artifact(**lookup) is None
    dossier_artifacts.create_artifact_pdf(
        bucket=bucket, prefix=PREFIX, artifact=artifact, pdf=built["pdf"]
    )
    dossier_artifacts.create_artifact(bucket=bucket, prefix=PREFIX, artifact=artifact)
    assert dossier_artifacts.find_prepared_artifact(**lookup) == artifact
    assert dossier_artifacts.find_prepared_artifact(**dict(lookup, selected_claim_ids=["clm_2"])) is None
    assert dossier_artifacts.find_prepared_artifact(**dict(lookup, scope_digest="c" * 64)) is None


# The actual browser, where this host has one


def _local_chrome() -> str | None:
    for candidate in (
        os.environ.get("CHROME_PATH"),
        "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _approved_font() -> Path | None:
    config = pdf_exporter._load_runtime_config()
    expected = config["assets"]["fonts"]["newsreader-variable-tcB4qQtO.woff2"]["sha256"]
    root = Path(__file__).resolve().parents[2]
    for candidate in (
        Path(config["assets"]["root"]) / "newsreader-variable-tcB4qQtO.woff2",
        root / "frontend/node_modules/ogilvy-intelligence-design-system/dist/fonts/newsreader-variable.woff2",
    ):
        if candidate.is_file() and sha256(candidate.read_bytes()) == expected:
            return candidate
    return None


def test_a_browser_rendered_pdf_passes_the_renderer_gate_and_loses_its_tool_metadata(
    monkeypatch, tmp_path
):
    """The pinned runtime's own audit and output gate, with the host's browser.

    The packaged browser of the final image is not on a unit test host. This
    drives the same input audit, the same page.pdf options and the same text
    and font gate with the browser this host has, then the export scrub.
    """
    chrome, font = _local_chrome(), _approved_font()
    if chrome is None or font is None:
        pytest.skip("no local browser or approved font on this host")
    playwright = pytest.importorskip("playwright.sync_api")
    value = dossier()
    payload = intelligence_dossier.read_dossier(
        value["investigation_id"], dossier_resolver.projection_record(value, [])
    )
    monkeypatch.setattr(dossier_artifacts, "_export_font_bytes", font.read_bytes)
    html = dossier_artifacts.render_html(payload).encode("utf-8")
    assets = tmp_path / "assets"
    assets.mkdir()
    shutil.copyfile(font, assets / "newsreader-variable-tcB4qQtO.woff2")
    prepared = pdf_exporter._prepare_render_input(html, assets, tmp_path / "work")
    assert prepared["font_names"] == ["Newsreader"]
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=chrome, headless=True)
        try:
            page = browser.new_page()
            requested = []
            page.on("request", lambda request: requested.append(request.url))
            page.goto(prepared["html_path"].as_uri(), wait_until="load")
            page.emulate_media(media="screen")
            raw = page.pdf(format="A4", print_background=True, prefer_css_page_size=True)
        finally:
            browser.close()
    # The embedded face was used: the page itself and the data URL are the
    # only loads, and the asset copy beside it was never asked for.
    assert [url for url in requested if not url.startswith("data:")] == [
        prepared["html_path"].as_uri()
    ]
    pdf_exporter._validate_pdf_bytes(
        raw, 25 * 1024 * 1024, prepared["expected_text"], prepared["font_names"]
    )
    assert any("Skia" in item or "Chrome" in item for item in metadata_values(raw))
    scrubbed = dossier_artifacts.scrub_pdf_metadata(raw)
    values = " ".join(metadata_values(scrubbed)).lower()
    assert not [name for name in TOOL_NAMES if name in values]
    assert extracted_text(scrubbed) == " ".join(page_text(html))
    assert dossier_store._digest(sha256(scrubbed))


# Contradicting evidence in the working projection


CHALLENGE = "Challenge finding: this source record bears against the proposition."


def test_contradicting_evidence_is_read_from_challenge_findings_and_never_invented():
    from tests.unit.test_dossier_resolver import claim_record

    value = dossier()
    plain = dossier_resolver.projection_record(value, [])
    assert all(item["contradicting_evidence_ids"] == [] for item in plain["claims"])
    claim_record(value, "clm_1")["limitations"].append(CHALLENGE)
    marked = {
        item["claim_id"]: item["contradicting_evidence_ids"]
        for item in dossier_resolver.projection_record(value, [])["claims"]
    }
    receipts = claim_record(value, "clm_1")["receipt_ids"]
    # clm_1 bears against the proposition itself; clm_2 derives from it and
    # clm_3 from clm_2. clm_4 and clm_5 are unrelated to it.
    assert marked == {
        "clm_1": receipts,
        "clm_2": receipts,
        "clm_3": receipts,
        "clm_4": [],
        "clm_5": [],
    }
    client_read = intelligence_dossier.read_dossier(
        value["investigation_id"], dossier_resolver.projection_record(value, [])
    )
    assert "contradicting_evidence_ids" not in str(client_read)


def test_the_artifact_font_is_the_one_the_answer_export_packages():
    from src.api import ask_export

    assert dossier_artifacts.PDF_FONT_FILE == ask_export.PDF_FONT_FILE
    assert dossier_artifacts.PDF_FONT_FILE in pdf_exporter._load_runtime_config()["assets"]["fonts"]
