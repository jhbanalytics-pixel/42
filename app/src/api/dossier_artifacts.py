"""Exact-version artifact approval.

An approval applies to one manifest: this investigation, this scope, this
dossier version, this artifact version, this selection and these exact export
bytes. Change any of them and the approval is about something else. The
predicate compares the two records field by field and refuses a malformed
manifest outright, so two records that agree on nonsense cannot approve it.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import html
import io
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path

from . import ask_export, client_purpose, dossier_store, narrative_report

BINDING_FIELDS = (
    "investigation_id",
    "scope_digest",
    "dossier_version",
    "artifact_version",
    "selection_digest",
    "export_digests",
)
APPROVED = "approved"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _digest(value: object) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _binding(manifest: object) -> dict | None:
    """The six binding fields of a well formed manifest, or None."""
    if not isinstance(manifest, Mapping) or set(manifest) != set(BINDING_FIELDS):
        return None
    identity = manifest["investigation_id"]
    exports = manifest["export_digests"]
    if (
        type(identity) is not str
        or not identity.strip()
        or identity != identity.strip()
    ):
        return None
    if not all(
        _digest(manifest[field])
        for field in (
            "scope_digest",
            "dossier_version",
            "artifact_version",
            "selection_digest",
        )
    ):
        return None
    if (
        type(exports) is not dict
        or not exports
        or not all(
            type(key) is str and key and _digest(value)
            for key, value in exports.items()
        )
    ):
        return None
    return {field: manifest[field] for field in BINDING_FIELDS}


def is_artifact_approved(*, manifest: dict, decision: dict) -> bool:
    """True only for an approved decision bound to exactly this manifest."""
    binding = _binding(manifest)
    if binding is None or not isinstance(decision, Mapping):
        return False
    if decision.get("state") != APPROVED:
        return False
    if not all(field in decision for field in BINDING_FIELDS):
        return False
    return all(decision[field] == binding[field] for field in BINDING_FIELDS)


# The artifact record and its manifest
#
# An artifact is one immutable record: the independently gated Client Read
# payload, the standalone HTML rendered from it, and the six-field manifest
# that binds both to this investigation, scope, dossier version and selection.
# The export digests are SHA256 over the exact bytes stored, the artifact
# version is the digest of the manifest without itself, and the artifact id is
# derived from that version, so one id can only ever name one byte sequence.
# The PDF is made from the stored HTML by the reviewed renderer, which the
# caller supplies, and stored beside the record as its own object. Its digest
# is recorded rather than recomputed from the record, because the record does
# not carry the PDF bytes; the stored object is checked against that digest
# whenever it is read. A v1 record prepared before the PDF existed carries no
# pdf digest and has no PDF export.

ARTIFACT_CONTRACT_VERSION = "dossier_artifact_v1"
PDF_EXPORT = "pdf"
ARTIFACT_CONTRACT_VERSION_V2 = "dossier_artifact_v2"
FORMATS = frozenset({"html"})
ARTIFACT_FIELDS = (
    "contract_version",
    "investigation_id",
    "dossier_version",
    "artifact_id",
    "artifact_version",
    "format",
    "manifest",
    "client_read",
    "html",
)
ARTIFACT_V2_FIELDS = (
    "contract_version",
    "investigation_id",
    "dossier_version",
    "artifact_id",
    "artifact_version",
    "format",
    "manifest",
    "client_read",
    "html",
    "content_kind",
    "preset",
    "report",
)
NARRATIVE_CONTENT_KIND = "narrative_report"
_ARTIFACT_ID = re.compile(r"art_[0-9a-f]{16}\Z")


class ArtifactInvalid(ValueError):
    """A record that is not an artifact, or whose bytes do not match its manifest."""


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_bytes(value: object) -> bytes:
    """Compact UTF-8 JSON in the order the record keeps, which the Client Read grammar fixes."""
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def selection_digest(selected_claim_ids: object) -> str:
    """The digest of the selected claim ids, in the order the record keeps them."""
    if not isinstance(selected_claim_ids, (list, tuple)) or not all(
        type(item) is str for item in selected_claim_ids
    ):
        raise ArtifactInvalid("selection is invalid")
    return _sha256(_canonical(list(selected_claim_ids)))


def _text(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# The stored HTML is the PDF's input, byte for byte, so it is written for the
# reviewed renderer as well as for a browser. It carries the packaged Newsreader
# face inside itself as a data URL, so the downloaded file and the Client Read
# frame load nothing, and names the same face by its local asset name after it,
# which is the copy the reviewed renderer resolves inside its approved asset
# root. A browser that loaded the first source never asks for the second. It
# carries no title element and no numbered list, because the renderer proves
# the page text it read from the HTML is the text in the PDF, and neither would
# print as read.
#
# The investigation is named by its question and the evidence dates are written
# as the app writes them. There is no readiness line: readiness is sealed when
# the artifact is prepared, before its approval, so once approved it could only
# state something that is no longer true.
PDF_FONT_FILE = ask_export.PDF_FONT_FILE


class ExportFontUnavailable(ArtifactInvalid):
    """The approved face is not at the renderer's asset root at its pinned digest."""


@functools.lru_cache(maxsize=1)
def _export_font_bytes() -> bytes:
    """The approved Newsreader face, read from the renderer's approved asset root."""
    from . import pdf_exporter

    try:
        config = pdf_exporter._load_runtime_config()
        expected = config["assets"]["fonts"][PDF_FONT_FILE]["sha256"]
        data = (Path(config["assets"]["root"]) / PDF_FONT_FILE).read_bytes()
    except (pdf_exporter.PdfExportError, OSError, KeyError, TypeError) as error:
        raise ExportFontUnavailable("export font is unavailable") from error
    if _sha256(data) != expected:
        raise ExportFontUnavailable("export font is not the approved face")
    return data


def _lens_lines(
    client_lens: Mapping[str, object] | None, client_scope_id: object
) -> tuple[str, str] | None:
    """The lens lines in the Ask export's own words, or None for general 42.

    The registry label is given only while the registry holds the lens enabled
    for client_scope_id at the configuration the frame carries; otherwise the
    lens is named by its stored identity. General 42 adds no line, so a general
    export keeps its bytes.
    """
    if client_lens is None:
        return None
    lens = dict(client_lens) if isinstance(client_lens, Mapping) else None
    if not ask_export._lens_binding(lens):
        raise ArtifactInvalid("client lens is invalid")
    return ask_export._lens_lines({"client_lens": lens}, client_scope_id)


def render_html(
    client_read: Mapping[str, object],
    *,
    title: str | None = None,
    client_lens: Mapping[str, object] | None = None,
    client_scope_id: str | None = None,
) -> str:
    """A standalone document from the Client Read payload: escaped text, nothing to load.

    title is the investigation's question; without one the investigation is
    named by its id. client_lens is the investigation frame's lens and
    client_scope_id the scope it was read under: a named lens is stated under
    the title and its configuration at the end, as the Ask export states them.
    """
    if not isinstance(client_read, Mapping):
        raise ArtifactInvalid("client read payload is invalid")
    if title is not None and (type(title) is not str or not title.strip()):
        raise ArtifactInvalid("client read title is invalid")
    lens_lines = _lens_lines(client_lens, client_scope_id)
    embedded = "data:font/woff2;base64," + base64.b64encode(_export_font_bytes()).decode(
        "ascii"
    )
    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="referrer" content="no-referrer">',
        "<style>@font-face{font-family:Newsreader;src:url('"
        + embedded
        + "') format('woff2'),url('"
        + PDF_FONT_FILE
        + "') format('woff2')}"
        "body{font-family:Newsreader,Georgia,serif;max-width:48rem;margin:2rem auto;"
        "padding:0 1rem;color:#1a1a1a}h2{margin-top:2rem}.item{margin:.5rem 0}"
        "small{color:#555}</style>",
        "</head>",
        "<body>",
        "<h1>Client Read</h1>",
        "<p><small>"
        + (
            _text(title)
            if title is not None
            else "Investigation " + _text(client_read.get("investigation_id"))
        )
        + "</small></p>",
    ]
    if lens_lines is not None:
        lines.append("<p><small>" + _text(lens_lines[0]) + "</small></p>")
    answer = client_read.get("concise_answer")
    if answer:
        lines.append("<h2>Answer</h2><p>" + _text(answer) + "</p>")
    claims = client_read.get("claims")
    if isinstance(claims, list) and claims:
        lines.append("<h2>Findings</h2>")
        for item in claims:
            if not isinstance(item, Mapping):
                continue
            citations = item.get("citations")
            cited = (
                " <small>Sources: "
                + _text(", ".join(str(c) for c in citations))
                + "</small>"
                if isinstance(citations, list) and citations
                else ""
            )
            lines.append(
                '<p class="item"><strong>'
                + _text(item.get("kind"))
                + "</strong> "
                + _text(item.get("text"))
                + cited
                + "</p>"
            )
    evidence = client_read.get("evidence")
    if isinstance(evidence, list) and evidence:
        lines.append("<h2>Evidence</h2>")
        for item in evidence:
            if not isinstance(item, Mapping):
                continue
            lines.append(
                '<p class="item">'
                + _text(item.get("evidence_id"))
                + " <small>"
                + _text(ask_export._date_text(item.get("published_at")))
                + "</small></p>"
            )
    excluded = client_read.get("excluded_count")
    if isinstance(excluded, int) and excluded > 0:
        lines.append(
            "<p><small>"
            + _text(excluded)
            + " item(s) withheld from this read.</small></p>"
        )
    if lens_lines is not None:
        lines.append("<p><small>" + _text(lens_lines[1]) + "</small></p>")
    lines.extend(["</body>", "</html>", ""])
    return "\n".join(lines)


def _export_digests(
    html_text: str,
    client_read: Mapping[str, object],
    *,
    preset: Mapping[str, object] | None = None,
    report: Mapping[str, object] | None = None,
) -> dict:
    exports = {
        "html": _sha256(html_text.encode("utf-8")),
        "client_read": _sha256(record_bytes(client_read)),
    }
    if preset is not None and report is not None:
        exports.update(
            preset=_sha256(_canonical(preset)),
            report=_sha256(_canonical(report)),
        )
    elif preset is not None or report is not None:
        raise ArtifactInvalid("narrative exports are incomplete")
    return exports


def _variable_texts(value: object) -> list[str]:
    if type(value) is str:
        return [value]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _variable_texts(item)]
    if isinstance(value, list):
        return [text for item in value for text in _variable_texts(item)]
    return []


def _refuse_narrative_purpose(
    policy: Mapping[str, object] | None, report: Mapping[str, object]
) -> None:
    if policy is None:
        return
    evaluation = client_purpose.evaluate_client_purpose(policy, _variable_texts(report))
    if evaluation["state"] == "prohibited":
        raise client_purpose.ClientPurposeProhibited(
            client_purpose.EXPORT_REFUSED, evaluation
        )


def _artifact_version(
    *,
    investigation_id: str,
    scope_digest: str,
    dossier_version: str,
    selection: str,
    exports: Mapping[str, str],
    format: str,
) -> str:
    return _sha256(
        _canonical(
            {
                "investigation_id": investigation_id,
                "scope_digest": scope_digest,
                "dossier_version": dossier_version,
                "selection_digest": selection,
                "export_digests": dict(exports),
                "format": format,
            }
        )
    )


def build_artifact(
    *,
    investigation_id: str,
    scope_digest: str,
    dossier_version: str,
    selected_claim_ids: object,
    client_read: Mapping[str, object],
    format: str,
    purpose_policy: Mapping[str, object] | None = None,
    content_kind: str | None = None,
    preset: Mapping[str, object] | None = None,
    pdf_digest: str | None = None,
    title: str | None = None,
    client_lens: Mapping[str, object] | None = None,
    client_scope_id: str | None = None,
) -> dict:
    """The artifact record for one Client Read payload, digests sealed before any write.

    purpose_policy is the scope's client purpose policy, None for general 42. A
    prohibited purpose refuses here, before any export bytes or digest exist.
    pdf_digest is the SHA256 of the PDF made from this record's HTML, which
    build_artifact_exports supplies; only a v1 record carries one. title is
    the investigation's question, which names it in a v1 export, and
    client_lens with client_scope_id names the frame's lens in it (render_html).
    """
    if format not in FORMATS:
        raise ArtifactInvalid("artifact format is unsupported")
    narrative = content_kind is not None or preset is not None
    if pdf_digest is not None and (narrative or not _digest(pdf_digest)):
        raise ArtifactInvalid("pdf digest is invalid")
    if narrative:
        selection = selection_digest(selected_claim_ids)
        if content_kind != NARRATIVE_CONTENT_KIND or preset is None:
            raise ArtifactInvalid("narrative artifact request is invalid")
        try:
            report = narrative_report.build_narrative_report(
                client_read=client_read,
                investigation_id=investigation_id,
                scope_digest=scope_digest,
                dossier_version=dossier_version,
                preset=preset,
            )
        except narrative_report.NarrativeReportError as error:
            raise ArtifactInvalid(str(error)) from error
        if list(selected_claim_ids) != report["selected_claim_ids"]:
            raise ArtifactInvalid("selection does not equal the admitted claims")
        _refuse_narrative_purpose(purpose_policy, report)
        html_text = narrative_report.render_html(report)
        exports = _export_digests(html_text, client_read, preset=preset, report=report)
        contract_version = ARTIFACT_CONTRACT_VERSION_V2
        fields = ARTIFACT_V2_FIELDS
    else:
        client_purpose.refuse_prohibited_export(purpose_policy, client_read)
        html_text = render_html(
            client_read, title=title, client_lens=client_lens, client_scope_id=client_scope_id
        )
        exports = _export_digests(html_text, client_read)
        if pdf_digest is not None:
            exports[PDF_EXPORT] = pdf_digest
        contract_version = ARTIFACT_CONTRACT_VERSION
        fields = ARTIFACT_FIELDS
        selection = selection_digest(selected_claim_ids)
    version = _artifact_version(
        investigation_id=investigation_id,
        scope_digest=scope_digest,
        dossier_version=dossier_version,
        selection=selection,
        exports=exports,
        format=format,
    )
    manifest = {
        "investigation_id": investigation_id,
        "scope_digest": scope_digest,
        "dossier_version": dossier_version,
        "artifact_version": version,
        "selection_digest": selection,
        "export_digests": exports,
    }
    record = {
        "contract_version": contract_version,
        "investigation_id": investigation_id,
        "dossier_version": dossier_version,
        "artifact_id": "art_" + version[:16],
        "artifact_version": version,
        "format": format,
        "manifest": manifest,
        "client_read": json.loads(record_bytes(client_read).decode("utf-8")),
        "html": html_text,
    }
    if narrative:
        record.update(
            content_kind=NARRATIVE_CONTENT_KIND,
            preset=json.loads(record_bytes(preset).decode("utf-8")),
            report=report,
        )
    if tuple(record) != fields:
        raise ArtifactInvalid("artifact record field order is invalid")
    return parse_artifact(record)


def parse_artifact(record: object) -> dict:
    """A stored or offered artifact, admitted only when every digest is its own bytes."""
    if type(record) is not dict:
        raise ArtifactInvalid("artifact record is invalid")
    if record.get("contract_version") == ARTIFACT_CONTRACT_VERSION:
        fields = ARTIFACT_FIELDS
    elif record.get("contract_version") == ARTIFACT_CONTRACT_VERSION_V2:
        fields = ARTIFACT_V2_FIELDS
    else:
        raise ArtifactInvalid("artifact record is invalid")
    if set(record) != set(fields):
        raise ArtifactInvalid("artifact record is invalid")
    if (
        record["contract_version"] == ARTIFACT_CONTRACT_VERSION_V2
        and type(record["format"]) is not str
    ):
        raise ArtifactInvalid("artifact record is invalid")
    if record["format"] not in FORMATS:
        raise ArtifactInvalid("artifact record is invalid")
    binding = _binding(record["manifest"])
    if binding is None or binding != record["manifest"]:
        raise ArtifactInvalid("artifact manifest is invalid")
    if (
        type(record["html"]) is not str
        or not isinstance(record["client_read"], dict)
        or type(record["artifact_id"]) is not str
        or _ARTIFACT_ID.fullmatch(record["artifact_id"]) is None
    ):
        raise ArtifactInvalid("artifact record is invalid")
    if record["contract_version"] == ARTIFACT_CONTRACT_VERSION_V2:
        if record["content_kind"] != NARRATIVE_CONTENT_KIND:
            raise ArtifactInvalid("artifact record is invalid")
        try:
            preset = narrative_report.validate_preset(record["preset"])
            rebuilt_report = narrative_report.build_narrative_report(
                client_read=record["client_read"],
                investigation_id=binding["investigation_id"],
                scope_digest=binding["scope_digest"],
                dossier_version=binding["dossier_version"],
                preset=preset,
            )
            offered_report = narrative_report.validate_report(
                record["report"], client_read=record["client_read"]
            )
            rebuilt_html = narrative_report.render_html(rebuilt_report)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ArtifactInvalid("narrative artifact is invalid") from error
        if (
            offered_report != rebuilt_report
            or record["html"] != rebuilt_html
            or selection_digest(rebuilt_report["selected_claim_ids"])
            != binding["selection_digest"]
        ):
            raise ArtifactInvalid("narrative artifact bytes are not deterministic")
        exports = _export_digests(
            record["html"],
            record["client_read"],
            preset=record["preset"],
            report=record["report"],
        )
    else:
        exports = _export_digests(record["html"], record["client_read"])
        recorded_pdf = binding["export_digests"].get(PDF_EXPORT)
        if recorded_pdf is not None:
            exports[PDF_EXPORT] = recorded_pdf
    version = _artifact_version(
        investigation_id=binding["investigation_id"],
        scope_digest=binding["scope_digest"],
        dossier_version=binding["dossier_version"],
        selection=binding["selection_digest"],
        exports=exports,
        format=record["format"],
    )
    if (
        binding["export_digests"] != exports
        or binding["artifact_version"] != version
        or record["artifact_version"] != version
        or record["artifact_id"] != "art_" + version[:16]
        or binding["investigation_id"] != record["investigation_id"]
        or binding["dossier_version"] != record["dossier_version"]
    ):
        raise ArtifactInvalid("artifact bytes do not match the manifest")
    return {
        field: json.loads(record_bytes(record[field]).decode("utf-8"))
        for field in fields
    }


def artifact_object_name(
    prefix: object, investigation_id: object, artifact_id: object
) -> str:
    dossier_store._validate_prefix(prefix)
    dossier_store._validate_investigation_id(investigation_id)
    if type(artifact_id) is not str or _ARTIFACT_ID.fullmatch(artifact_id) is None:
        raise ValueError("artifact id is invalid")
    return f"{prefix}artifacts/{investigation_id}/{artifact_id}.json"


def create_artifact(*, bucket: object, prefix: str, artifact: Mapping) -> dict:
    """Store one artifact once; an identical retry is the same artifact."""
    record = parse_artifact(dict(artifact))
    name = artifact_object_name(
        prefix, record["investigation_id"], record["artifact_id"]
    )
    written = dossier_store.create_once(
        bucket=bucket,
        name=name,
        body=record_bytes(record),
        conflict_code="artifact_conflict",
        ambiguous_code="artifact_create_ambiguous",
    )
    return {
        "created": written["created"],
        "artifact": record,
        "generation": written["generation"],
        "object_name": name,
    }


def read_artifact(
    *, bucket: object, prefix: str, investigation_id: str, artifact_id: str
) -> dict | None:
    """The stored record, admitted only when its bytes match its manifest and its names.

    An id outside the grammar names nothing and reads nothing. Absence is None;
    a record whose digests or names do not agree with itself is refused.
    """
    try:
        name = artifact_object_name(prefix, investigation_id, artifact_id)
    except ValueError:
        return None
    found = dossier_store._read_object(bucket, name)
    if found is None:
        return None
    record = parse_artifact(dossier_store._load(found[0]))
    if (
        record["investigation_id"] != investigation_id
        or record["artifact_id"] != artifact_id
    ):
        raise ArtifactInvalid("artifact names do not match")
    return record


# The PDF export
#
# A headless browser signs its PDFs: the Info dictionary names the browser as
# Creator and its graphics library as Producer, and XMP metadata may repeat
# them. None of that is the client's business, so the export is rewritten
# without any document metadata before its digest is taken. The rewrite must
# not name its own library either, which is why the metadata is cleared rather
# than edited. What a reader extracts from the page is left exactly as it was.


def _pdf_reader(data: object):
    from pypdf import PdfReader

    if type(data) is not bytes or not data.startswith(b"%PDF-"):
        raise ValueError("not a pdf")
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted or not reader.pages:
        raise ValueError("pdf is unreadable")
    return reader


def _pdf_metadata_present(reader) -> bool:
    return bool(reader.metadata) or "/Metadata" in reader.trailer["/Root"]


def scrub_pdf_metadata(data: bytes) -> bytes:
    """The same pages with no Info dictionary and no XMP stream, or a refusal."""
    from pypdf import PdfWriter
    from pypdf.generic import NameObject

    try:
        reader = _pdf_reader(data)
        writer = PdfWriter(clone_from=reader)
        writer.metadata = None
        root = writer.root_object
        if NameObject("/Metadata") in root:
            del root[NameObject("/Metadata")]
        out = io.BytesIO()
        writer.write(out)
        scrubbed = out.getvalue()
        if _pdf_metadata_present(_pdf_reader(scrubbed)):
            raise ValueError("pdf metadata survived")
    except Exception as error:
        raise ArtifactInvalid("pdf export is invalid") from error
    return scrubbed


def pdf_text(data: bytes) -> str:
    """The page text a reader extracts, whitespace collapsed, the way the renderer checks it."""
    try:
        reader = _pdf_reader(data)
        return " ".join(" ".join(page.extract_text().split()) for page in reader.pages)
    except Exception as error:
        raise ArtifactInvalid("pdf export is invalid") from error


def _html_texts(html_text: str) -> list[str]:
    """Every text node of the page and every attribute value, style included."""
    from html.parser import HTMLParser

    found: list[str] = []

    class Collector(HTMLParser):
        def handle_starttag(self, tag, attrs):
            found.extend(str(value) for _key, value in attrs if value)

        def handle_data(self, data):
            if data.strip():
                found.append(data)

    collector = Collector(convert_charrefs=True)
    collector.feed(html_text)
    collector.close()
    return found


def _normalised(texts: list[str]) -> list[str]:
    """Each text, plus the form a casing, width or invisible character trick reduces to."""
    variants = []
    for text in texts:
        folded = "".join(
            character
            for character in unicodedata.normalize("NFKC", text)
            if unicodedata.category(character) != "Cf"
        ).casefold()
        variants.extend([text, folded])
    return variants


def _refuse_prohibited_texts(
    policy: Mapping[str, object] | None, texts: list[str]
) -> None:
    if policy is None:
        return
    evaluation = client_purpose.evaluate_client_purpose(policy, _normalised(texts))
    if evaluation["state"] == "prohibited":
        raise client_purpose.ClientPurposeProhibited(
            client_purpose.EXPORT_REFUSED, evaluation
        )


def build_artifact_exports(
    *,
    investigation_id: str,
    scope_digest: str,
    dossier_version: str,
    selected_claim_ids: object,
    client_read: Mapping[str, object],
    purpose_policy: Mapping[str, object] | None,
    render_pdf: Callable[[bytes], bytes],
    title: str | None = None,
    client_lens: Mapping[str, object] | None = None,
    client_scope_id: str | None = None,
) -> dict:
    """The v1 artifact with its PDF, both scanned and both digests sealed before any write.

    The policy is applied to the payload before anything is rendered, then to
    everything that leaves: the HTML's text and attributes and the PDF's page
    text. The PDF's metadata is not scanned: scrub_pdf_metadata removes the
    Info dictionary and the XMP stream and refuses unless it can read the
    result back with neither present, so no metadata leaves at all. Each is
    also read through Unicode compatibility folding, so a prohibited term in
    other casing, full width or with invisible characters inside it is still
    that term. render_pdf receives exactly the stored HTML bytes; a renderer
    refusal propagates as raised and nothing is sealed.
    """
    texts = client_purpose.client_read_texts(client_read)
    _refuse_prohibited_texts(purpose_policy, texts)
    html_text = render_html(
        client_read, title=title, client_lens=client_lens, client_scope_id=client_scope_id
    )
    _refuse_prohibited_texts(purpose_policy, _html_texts(html_text))
    pdf = scrub_pdf_metadata(render_pdf(html_text.encode("utf-8")))
    reader_texts = [pdf_text(pdf)]
    _refuse_prohibited_texts(purpose_policy, reader_texts)
    artifact = build_artifact(
        investigation_id=investigation_id,
        scope_digest=scope_digest,
        dossier_version=dossier_version,
        selected_claim_ids=selected_claim_ids,
        client_read=client_read,
        format="html",
        purpose_policy=purpose_policy,
        pdf_digest=_sha256(pdf),
        title=title,
        client_lens=client_lens,
        client_scope_id=client_scope_id,
    )
    if artifact["html"] != html_text:
        raise ArtifactInvalid("artifact html is not the rendered html")
    return {"artifact": artifact, "pdf": pdf}


def artifact_pdf_object_name(
    prefix: object, investigation_id: object, artifact_id: object
) -> str:
    return artifact_object_name(prefix, investigation_id, artifact_id)[: -len(".json")] + ".pdf"


def _recorded_pdf_digest(artifact: Mapping) -> str:
    record = parse_artifact(dict(artifact))
    digest = record["manifest"]["export_digests"].get(PDF_EXPORT)
    if record["contract_version"] != ARTIFACT_CONTRACT_VERSION or digest is None:
        raise ArtifactInvalid("artifact has no pdf export")
    return digest


def create_artifact_pdf(
    *, bucket: object, prefix: str, artifact: Mapping, pdf: bytes
) -> dict:
    """Store the PDF once, only when its bytes are the ones the manifest names."""
    from google.api_core.exceptions import PreconditionFailed

    if type(pdf) is not bytes or _sha256(pdf) != _recorded_pdf_digest(artifact):
        raise ArtifactInvalid("pdf bytes do not match the manifest")
    name = artifact_pdf_object_name(
        prefix, artifact["investigation_id"], artifact["artifact_id"]
    )
    created = True
    try:
        generation = dossier_store._upload(
            bucket, name, pdf, if_generation_match=0, content_type="application/pdf"
        )
    except PreconditionFailed:
        created = False
        existing = dossier_store._read_object(bucket, name)
        if existing is None:
            raise dossier_store.DossierStoreError("artifact_pdf_create_ambiguous") from None
        if existing[0] != pdf:
            raise dossier_store.DossierConflict("artifact_pdf_conflict") from None
        generation = existing[1]
    except dossier_store.DossierStoreError:
        raise
    except Exception as error:
        raise dossier_store.DossierStoreError("dossier_store_unavailable") from error
    return {"created": created, "generation": str(generation), "object_name": name}


def read_artifact_pdf(*, bucket: object, prefix: str, artifact: Mapping) -> bytes | None:
    """The stored PDF when its bytes are the ones the manifest names; None when absent."""
    digest = _recorded_pdf_digest(artifact)
    name = artifact_pdf_object_name(
        prefix, artifact["investigation_id"], artifact["artifact_id"]
    )
    found = dossier_store._read_object(bucket, name)
    if found is None:
        return None
    if _sha256(found[0]) != digest:
        raise ArtifactInvalid("pdf bytes do not match the manifest")
    return found[0]


_ARTIFACT_NAME = re.compile(r"art_[0-9a-f]{16}\.json\Z")


def list_artifacts(*, bucket: object, prefix: str, investigation_id: str) -> list[dict]:
    """Every stored artifact of one investigation, each admitted as read_artifact admits it.

    Ordered by id, so every reader of the same store sees the same order.
    Objects under the directory that are not named like an artifact record,
    the PDFs among them, are not records and are left alone.
    """
    directory = artifact_object_name(prefix, investigation_id, "art_" + "0" * 16)
    directory = directory[: directory.rindex("/") + 1]
    try:
        names = sorted(blob.name for blob in bucket.list_blobs(prefix=directory))
    except Exception as error:
        raise dossier_store.DossierStoreError("dossier_store_unavailable") from error
    records = []
    for name in names:
        if type(name) is not str or not name.startswith(directory):
            raise dossier_store.DossierStoreError("dossier_store_unavailable")
        tail = name[len(directory) :]
        if _ARTIFACT_NAME.fullmatch(tail) is None:
            continue
        record = read_artifact(
            bucket=bucket,
            prefix=prefix,
            investigation_id=investigation_id,
            artifact_id=tail[: -len(".json")],
        )
        if record is None:
            # Listed and then gone. The store never deletes, so it cannot be
            # trusted to answer for this investigation right now.
            raise dossier_store.DossierStoreError("dossier_store_unavailable")
        records.append(record)
    return records


def find_prepared_artifact(
    *,
    bucket: object,
    prefix: str,
    investigation_id: str,
    scope_digest: str,
    dossier_version: str,
    selected_claim_ids: object,
    client_read: Mapping[str, object],
    html: str,
) -> dict | None:
    """A stored v1 artifact with a PDF for exactly this content, or None.

    Preparing the same content again must not render again: a second
    rendering may differ by a byte and so become a second artifact of the same
    content. The match is every digest the content determines; the PDF digest
    is whatever the first preparation sealed. The lowest id wins, so every
    reader of the same store finds the same one.
    """
    wanted_selection = selection_digest(selected_claim_ids)
    wanted_html = _sha256(html.encode("utf-8"))
    wanted_client_read = _sha256(record_bytes(client_read))
    for record in list_artifacts(
        bucket=bucket, prefix=prefix, investigation_id=investigation_id
    ):
        if record["contract_version"] != ARTIFACT_CONTRACT_VERSION:
            continue
        manifest = record["manifest"]
        exports = manifest["export_digests"]
        if (
            PDF_EXPORT in exports
            and manifest["scope_digest"] == scope_digest
            and manifest["dossier_version"] == dossier_version
            and manifest["selection_digest"] == wanted_selection
            and exports["html"] == wanted_html
            and exports["client_read"] == wanted_client_read
        ):
            return record
    return None
