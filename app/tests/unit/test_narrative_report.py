import pytest

from src.api import narrative_report


def client_read():
    return {
        "contract_version": "intelligence_dossier_v1",
        "investigation_id": "inv_fixture",
        "concise_answer": "An admitted answer.",
        "claims": [
            {
                "claim_id": "clm_1",
                "kind": "observation",
                "text": "An admitted finding.",
                "citations": ["ev_1"],
            }
        ],
        "evidence": [{"evidence_id": "ev_1", "published_at": "2026-09-01T00:00:00Z"}],
        "excluded_count": 0,
        "excluded_reasons": [],
        "artifact_readiness": {"state": "client_ready", "failed": []},
    }


def test_general_report_preserves_only_admitted_read_content():
    report = narrative_report.build_narrative_report(
        client_read=client_read(),
        investigation_id="inv_fixture",
        scope_digest="a" * 64,
        dossier_version="b" * 64,
        preset=narrative_report.general_preset("ogilvy_default"),
    )
    assert report["selected_claim_ids"] == ["clm_1"]
    assert report["sections"][0] == {
        "kind": "read",
        "state": "available",
        "reason": None,
        "items": ["An admitted answer."],
    }
    assert report["sections"][2] == {
        "kind": "heat",
        "state": "unavailable",
        "reason": "source_not_admitted",
        "items": [],
    }
    assert report["source_appendix"] == client_read()["evidence"]


def test_raw_or_changed_client_read_refuses():
    raw = client_read() | {"unexpected": True}
    with pytest.raises(
        narrative_report.NarrativeReportError, match="narrative_read_invalid"
    ):
        narrative_report.build_narrative_report(
            client_read=raw,
            investigation_id="inv_fixture",
            scope_digest="a" * 64,
            dossier_version="b" * 64,
            preset=narrative_report.general_preset("ogilvy_default"),
        )
    raw_projection = {
        "investigation_id": "inv_fixture",
        "concise_answer": "Internal answer",
        "claims": [],
        "evidence": [],
        "decision": "approved",
    }
    with pytest.raises(
        narrative_report.NarrativeReportError, match="narrative_read_invalid"
    ):
        narrative_report.build_narrative_report(
            client_read=raw_projection,
            investigation_id="inv_fixture",
            scope_digest="a" * 64,
            dossier_version="b" * 64,
            preset=narrative_report.general_preset("ogilvy_default"),
        )


def test_empty_admitted_read_has_honest_unavailable_sections():
    value = client_read()
    value["concise_answer"] = None
    value["claims"] = []
    value["evidence"] = []
    value["excluded_count"] = 1
    value["excluded_reasons"] = ["claim status is rejected, not approved"]
    value["artifact_readiness"] = {
        "state": "blocked",
        "failed": ["blocking_question"],
    }

    report = narrative_report.build_narrative_report(
        client_read=value,
        investigation_id="inv_fixture",
        scope_digest="a" * 64,
        dossier_version="b" * 64,
        preset=narrative_report.general_preset("ogilvy_default"),
    )

    assert report["selected_claim_ids"] == []
    assert report["sections"][0] == {
        "kind": "read",
        "state": "unavailable",
        "reason": "answer_not_admitted",
        "items": [],
    }
    assert report["sections"][1] == {
        "kind": "findings",
        "state": "unavailable",
        "reason": "findings_not_admitted",
        "items": [],
    }
    assert report["sections"][5] == {
        "kind": "source_appendix",
        "state": "unavailable",
        "reason": "evidence_not_admitted",
        "items": [],
    }
    assert report["sections"][6]["items"] == [
        "claim status is rejected, not approved",
        "blocking_question",
        "Heat, origin and carrier, and geography details are not available in this report.",
    ]


@pytest.mark.parametrize("scope_id", [None, "", " padded", "trailing ", 7])
def test_general_preset_requires_a_valid_resolved_scope_id(scope_id):
    with pytest.raises(
        narrative_report.NarrativeReportError, match="narrative_preset_invalid"
    ):
        narrative_report.build_narrative_report(
            client_read=client_read(),
            investigation_id="inv_fixture",
            scope_digest="a" * 64,
            dossier_version="b" * 64,
            preset=narrative_report.general_preset(scope_id),
        )


@pytest.mark.parametrize("scope_id", ["client-east", "has space", "x" * 129])
def test_general_preset_reuses_the_existing_scope_identifier_grammar(scope_id):
    report = narrative_report.build_narrative_report(
        client_read=client_read(),
        investigation_id="inv_fixture",
        scope_digest="a" * 64,
        dossier_version="b" * 64,
        preset=narrative_report.general_preset(scope_id),
    )
    assert report["selected_claim_ids"] == ["clm_1"]


def test_preset_scope_must_equal_the_resolved_server_scope():
    with pytest.raises(
        narrative_report.NarrativeReportError, match="narrative_preset_invalid"
    ):
        narrative_report.validate_preset(
            narrative_report.general_preset("client-east"),
            client_scope_id="client-west",
        )


def test_html_is_a_readable_escaped_report_with_citations_and_timestamps():
    value = client_read()
    value["concise_answer"] = "<script>unsafe</script>"
    report = narrative_report.build_narrative_report(
        client_read=value,
        investigation_id="inv_fixture",
        scope_digest="a" * 64,
        dossier_version="b" * 64,
        preset=narrative_report.general_preset("ogilvy_default"),
    )
    html = narrative_report.render_html(report)
    assert "<script>" not in html
    assert "&lt;script&gt;unsafe&lt;/script&gt;" in html
    assert "<pre>" not in html
    assert "<h2>Findings</h2>" in html
    assert "An admitted finding." in html
    assert "ev_1" in html
    assert "2026-09-01T00:00:00Z" in html
    assert 'src="' not in html
    assert "<script" not in html


def test_html_keeps_long_source_ids_readable_on_narrow_viewports():
    value = client_read()
    evidence_id = "ev_" + "a" * 64
    value["claims"][0]["citations"] = [evidence_id]
    value["evidence"][0]["evidence_id"] = evidence_id
    report = narrative_report.build_narrative_report(
        client_read=value,
        investigation_id="inv_fixture",
        scope_digest="a" * 64,
        dossier_version="b" * 64,
        preset=narrative_report.general_preset("ogilvy_default"),
    )

    html = narrative_report.render_html(report)

    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert "overflow-wrap:anywhere" in html
    assert html.count(evidence_id) == 2
    assert "<strong>" not in html
    assert "intelligence_dossier_v1" not in html
    assert '<div class="findings">\n<p>' in html
    assert '<div class="limitations">\n<p>' in html


def test_report_validator_refuses_incomplete_selection_and_extra_fields():
    report = narrative_report.build_narrative_report(
        client_read=client_read(),
        investigation_id="inv_fixture",
        scope_digest="a" * 64,
        dossier_version="b" * 64,
        preset=narrative_report.general_preset("ogilvy_default"),
    )
    changed = dict(report, selected_claim_ids=[])
    with pytest.raises(
        narrative_report.NarrativeReportError, match="narrative_report_invalid"
    ):
        narrative_report.validate_report(changed, client_read=client_read())
    changed = dict(report, extra=True)
    with pytest.raises(
        narrative_report.NarrativeReportError, match="narrative_report_invalid"
    ):
        narrative_report.validate_report(changed, client_read=client_read())


@pytest.mark.parametrize(
    "change",
    [
        ("read", {"kind": "read", "state": "available", "reason": None, "items": []}),
        (
            "findings",
            {
                "kind": "findings",
                "state": "available",
                "reason": None,
                "items": [{"claim_id": "clm_1", "text": "invented"}],
            },
        ),
        (
            "heat",
            {"kind": "heat", "state": "available", "reason": None, "items": ["invented"]},
        ),
        (
            "limitations",
            {"kind": "limitations", "state": "available", "reason": None, "items": [{}]},
        ),
    ],
)
def test_standalone_report_grammar_refuses_invalid_section_items(change):
    report = narrative_report.build_narrative_report(
        client_read=client_read(),
        investigation_id="inv_fixture",
        scope_digest="a" * 64,
        dossier_version="b" * 64,
        preset=narrative_report.general_preset("ogilvy_default"),
    )
    kind, section = change
    report["sections"][narrative_report.SECTION_ORDER.index(kind)] = section

    with pytest.raises(
        narrative_report.NarrativeReportError, match="narrative_report_invalid"
    ):
        narrative_report.validate_report(report)
