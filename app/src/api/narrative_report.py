import hashlib
import html
import json
import re

from . import intelligence_dossier


PRESET_VERSION = "narrative_preset_v1"
REPORT_VERSION = "narrative_report_v1"
GENERAL_PRESET_ID = "general_42_v1"
SECTION_ORDER = (
    "read",
    "findings",
    "heat",
    "origin_and_carriers",
    "geography",
    "source_appendix",
    "limitations",
    "response_tracker",
)
REPORT_FIELDS = (
    "contract_version",
    "investigation_id",
    "scope_digest",
    "dossier_version",
    "preset_digest",
    "selected_claim_ids",
    "concise_answer",
    "sections",
    "source_appendix",
    "response_tracker",
)
SECTION_FIELDS = ("kind", "state", "reason", "items")
ABSENT_SOURCE_EXPLANATION = (
    "Heat, origin and carrier, and geography details are not available in this report."
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UNAVAILABLE_COPY = {
    "answer_not_admitted": "No answer is admitted in this Client Read.",
    "findings_not_admitted": "No findings are admitted in this Client Read.",
    "source_not_admitted": "This source field is not admitted in the current Client Read.",
    "evidence_not_admitted": "No source evidence is admitted in this Client Read.",
    "response_projection_not_admitted": "Response tracking is not admitted in the current contract.",
}


class NarrativeReportError(ValueError):
    pass


def canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def general_preset(client_scope_id):
    return {
        "contract_version": PRESET_VERSION,
        "preset_id": GENERAL_PRESET_ID,
        "client_scope_id": client_scope_id,
        "policy_digest": None,
        "identity_assets": [],
        "section_order": list(SECTION_ORDER),
        "delivery_mode": "protected_unsent",
    }


def _invalid(code):
    raise NarrativeReportError(code)


def validate_preset(value, *, client_scope_id=None):
    if not isinstance(value, dict):
        _invalid("narrative_preset_invalid")
    scope_id = value.get("client_scope_id")
    if (
        type(scope_id) is not str
        or not scope_id
        or scope_id != scope_id.strip()
        or (client_scope_id is not None and scope_id != client_scope_id)
        or value != general_preset(scope_id)
    ):
        _invalid("narrative_preset_invalid")
    return json.loads(canonical_bytes(value).decode("utf-8"))


def _section(kind, items, reason=None):
    return {
        "kind": kind,
        "state": "available" if reason is None else "unavailable",
        "reason": reason,
        "items": items,
    }


def _sections(client_read):
    answer = client_read["concise_answer"]
    claims = client_read["claims"]
    evidence = client_read["evidence"]
    limitations = [
        *client_read["excluded_reasons"],
        *client_read["artifact_readiness"]["failed"],
        ABSENT_SOURCE_EXPLANATION,
    ]
    return [
        _section(
            "read",
            [answer] if answer is not None else [],
            None if answer is not None else "answer_not_admitted",
        ),
        _section(
            "findings",
            claims,
            None if claims else "findings_not_admitted",
        ),
        _section("heat", [], "source_not_admitted"),
        _section("origin_and_carriers", [], "source_not_admitted"),
        _section("geography", [], "source_not_admitted"),
        _section(
            "source_appendix",
            evidence,
            None if evidence else "evidence_not_admitted",
        ),
        _section("limitations", limitations),
        _section("response_tracker", [], "response_projection_not_admitted"),
    ]


def _copy(value):
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _digest(value):
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def build_narrative_report(
    *, client_read, investigation_id, scope_digest, dossier_version, preset
):
    try:
        intelligence_dossier.validate_dossier_payload(client_read)
    except (AttributeError, KeyError, TypeError, ValueError):
        _invalid("narrative_read_invalid")
    if (
        client_read.get("investigation_id") != investigation_id
        or not _DIGEST.fullmatch(scope_digest or "")
        or not _DIGEST.fullmatch(dossier_version or "")
    ):
        _invalid("narrative_request_invalid")
    admitted = _copy(client_read)
    bound_preset = validate_preset(preset)
    report = {
        "contract_version": REPORT_VERSION,
        "investigation_id": investigation_id,
        "scope_digest": scope_digest,
        "dossier_version": dossier_version,
        "preset_digest": canonical_digest(bound_preset),
        "selected_claim_ids": [claim["claim_id"] for claim in admitted["claims"]],
        "concise_answer": admitted["concise_answer"],
        "sections": _sections(admitted),
        "source_appendix": admitted["evidence"],
        "response_tracker": {
            "state": "unavailable",
            "reason": "response_projection_not_admitted",
            "items": [],
        },
    }
    return validate_report(report, client_read=admitted)


def validate_report(value, *, client_read=None):
    if type(value) is not dict or set(value) != set(REPORT_FIELDS):
        _invalid("narrative_report_invalid")
    if (
        value["contract_version"] != REPORT_VERSION
        or type(value["investigation_id"]) is not str
        or not value["investigation_id"].strip()
        or not _digest(value["scope_digest"])
        or not _digest(value["dossier_version"])
        or not _digest(value["preset_digest"])
        or not isinstance(value["selected_claim_ids"], list)
        or any(
            type(item) is not str or not item.strip()
            for item in value["selected_claim_ids"]
        )
        or len(value["selected_claim_ids"]) != len(set(value["selected_claim_ids"]))
        or (
            value["concise_answer"] is not None
            and type(value["concise_answer"]) is not str
        )
        or not isinstance(value["source_appendix"], list)
        or not isinstance(value["sections"], list)
        or len(value["sections"]) != len(SECTION_ORDER)
    ):
        _invalid("narrative_report_invalid")
    for expected_kind, section in zip(SECTION_ORDER, value["sections"], strict=True):
        if (
            type(section) is not dict
            or set(section) != set(SECTION_FIELDS)
            or section["kind"] != expected_kind
            or section["state"] not in {"available", "unavailable"}
            or not isinstance(section["items"], list)
            or (section["state"] == "available" and section["reason"] is not None)
            or (
                section["state"] == "unavailable"
                and (
                    section["items"]
                    or type(section["reason"]) is not str
                    or not section["reason"].strip()
                )
            )
        ):
            _invalid("narrative_report_invalid")
    sections = {section["kind"]: section for section in value["sections"]}
    read = sections["read"]
    if read["state"] == "available":
        if len(read["items"]) != 1 or type(read["items"][0]) is not str:
            _invalid("narrative_report_invalid")
    elif read["reason"] != "answer_not_admitted":
        _invalid("narrative_report_invalid")
    findings = sections["findings"]
    if findings["state"] == "available":
        if not findings["items"]:
            _invalid("narrative_report_invalid")
        for item in findings["items"]:
            if (
                type(item) is not dict
                or set(item) != {"claim_id", "kind", "text", "citations"}
                or type(item["claim_id"]) is not str
                or not item["claim_id"].strip()
                or item["kind"]
                not in intelligence_dossier.FINDING_KINDS
                | intelligence_dossier.LIMITATION_KINDS
                or type(item["text"]) is not str
                or not item["text"].strip()
                or not isinstance(item["citations"], list)
                or not item["citations"]
                or any(
                    type(citation) is not str or not citation.strip()
                    for citation in item["citations"]
                )
                or len(item["citations"]) != len(set(item["citations"]))
            ):
                _invalid("narrative_report_invalid")
    elif findings["reason"] != "findings_not_admitted":
        _invalid("narrative_report_invalid")
    for kind in ("heat", "origin_and_carriers", "geography"):
        if sections[kind] != _section(kind, [], "source_not_admitted"):
            _invalid("narrative_report_invalid")
    sources = sections["source_appendix"]
    if sources["state"] == "available":
        if not sources["items"]:
            _invalid("narrative_report_invalid")
        for item in sources["items"]:
            if (
                type(item) is not dict
                or set(item) != {"evidence_id", "published_at"}
                or type(item["evidence_id"]) is not str
                or not item["evidence_id"].strip()
                or intelligence_dossier._parse_datetime(item["published_at"])
                is None
            ):
                _invalid("narrative_report_invalid")
    elif sources["reason"] != "evidence_not_admitted":
        _invalid("narrative_report_invalid")
    limitations = sections["limitations"]
    if (
        limitations["state"] != "available"
        or not limitations["items"]
        or any(type(item) is not str or not item.strip() for item in limitations["items"])
        or ABSENT_SOURCE_EXPLANATION not in limitations["items"]
    ):
        _invalid("narrative_report_invalid")
    if sections["response_tracker"] != _section(
        "response_tracker", [], "response_projection_not_admitted"
    ):
        _invalid("narrative_report_invalid")
    evidence_ids = {item["evidence_id"] for item in sources["items"]}
    claim_ids = [item["claim_id"] for item in findings["items"]]
    if (
        value["selected_claim_ids"] != claim_ids
        or value["source_appendix"] != sources["items"]
        or any(
            citation not in evidence_ids
            for item in findings["items"]
            for citation in item["citations"]
        )
        or (
            read["state"] == "available"
            and value["concise_answer"] != read["items"][0]
        )
        or (read["state"] == "unavailable" and value["concise_answer"] is not None)
    ):
        _invalid("narrative_report_invalid")
    if value["response_tracker"] != {
        "state": "unavailable",
        "reason": "response_projection_not_admitted",
        "items": [],
    }:
        _invalid("narrative_report_invalid")
    if client_read is not None:
        try:
            intelligence_dossier.validate_dossier_payload(client_read)
        except (AttributeError, KeyError, TypeError, ValueError):
            _invalid("narrative_report_invalid")
        expected_selection = [claim["claim_id"] for claim in client_read["claims"]]
        if (
            value["investigation_id"] != client_read["investigation_id"]
            or value["selected_claim_ids"] != expected_selection
            or value["concise_answer"] != client_read["concise_answer"]
            or value["sections"] != _sections(client_read)
            or value["source_appendix"] != client_read["evidence"]
        ):
            _invalid("narrative_report_invalid")
    return _copy(value)


def _text(value):
    return html.escape(str(value), quote=True)


def _unavailable(section):
    reason = section["reason"]
    explanation = _UNAVAILABLE_COPY.get(reason, "This section is unavailable.")
    return '<p class="unavailable">' + _text(explanation) + "</p>"


def render_html(report):
    value = validate_report(report)
    sections = {section["kind"]: section for section in value["sections"]}
    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<meta name="referrer" content="no-referrer">',
        "<title>Narrative report</title>",
        "<style>*{box-sizing:border-box}body{font-family:sans-serif;max-width:52rem;"
        "margin:2rem auto;padding:0 1rem;color:#1a1a1a}h2{margin-top:2rem}"
        "p,li,time,.evidence-id,.citations{overflow-wrap:anywhere;word-break:break-word}"
        "li{margin:.6rem 0}.unavailable,small{color:#555}</style>",
        "</head>",
        "<body>",
        "<h1>Narrative report</h1>",
        "<p><small>Investigation " + _text(value["investigation_id"]) + "</small></p>",
    ]
    for kind in SECTION_ORDER:
        section = sections[kind]
        lines.append("<section>")
        lines.append("<h2>" + kind.replace("_", " ").title() + "</h2>")
        if section["state"] == "unavailable":
            lines.append(_unavailable(section))
        elif kind == "read":
            lines.append("<p>" + _text(section["items"][0]) + "</p>")
        elif kind == "findings":
            lines.append('<div class="findings">')
            for item in section["items"]:
                citations = ", ".join(item["citations"])
                lines.append(
                    "<p>"
                    + _text(item["kind"].title())
                    + ": "
                    + _text(item["text"])
                    + ' <small class="citations">Sources: '
                    + _text(citations)
                    + "</small></p>"
                )
            lines.append("</div>")
        elif kind == "source_appendix":
            lines.append("<ul>")
            for item in section["items"]:
                published_at = item["published_at"]
                lines.append(
                    '<li><span class="evidence-id">'
                    + _text(item["evidence_id"])
                    + '</span> <time datetime="'
                    + _text(published_at)
                    + '">'
                    + _text(published_at)
                    + "</time></li>"
                )
            lines.append("</ul>")
        elif kind == "limitations":
            lines.append('<div class="limitations">')
            lines.extend("<p>" + _text(item) + "</p>" for item in section["items"])
            lines.append("</div>")
        lines.append("</section>")
    lines.extend(["</body>", "</html>", ""])
    return "\n".join(lines)
