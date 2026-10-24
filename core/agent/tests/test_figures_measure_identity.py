"""Audit F7D-ASK-001: a figure's identity includes what it measures. One record giving one subject the same value for
two measures is two figures, and two tables that differ only in their measure are not one table."""

from core.agent import figures
from core.agent.tests.test_figures import fig, make_ctx, record

TEXT = "Ipsos poll. Support: ANC 40%. Turnout: ANC 40%."


def test_one_subject_with_one_value_for_two_measures_keeps_both_figures():
    ctx = make_ctx()
    record(ctx, "e1", TEXT)
    kept, cut = figures.verify([fig("e1", "Support: ANC 40%", "ANC", 40, measure="Support"),
                                fig("e1", "Turnout: ANC 40%", "ANC", 40, measure="Turnout")], ctx)
    assert sorted(f["measure"] for f in kept) == ["Support", "Turnout"], cut


def test_the_same_figure_twice_is_still_one():
    ctx = make_ctx()
    record(ctx, "e1", TEXT)
    kept, _ = figures.verify([fig("e1", "Support: ANC 40%", "ANC", 40, measure="Support"),
                              fig("e1", "Support: ANC 40%", "ANC", 40, measure="support")], ctx)
    assert len(kept) == 1


def _row(eid, measure, handle, population="", sample="", fieldwork=""):
    return {"subject": "ANC", "value": 40.0, "unit": "%", "quote": f"{measure}: ANC 40%", "measure": measure,
            "source": "Ipsos", "fieldwork": fieldwork, "sample": sample, "population": population, "evidence_id": eid,
            "platform": "x", "handle": handle, "url": f"https://src.example/{eid}",
            "posted_at": "2026-10-13T08:00:00", "market": "ZA", "source_market": None}


def test_tables_with_the_same_rows_but_different_measures_stay_apart():
    built = figures.tables([_row("e1", "Support", "News24"), _row("e2", "Turnout", "eNCA")])
    assert sorted(t["title"] for t in built) == ["Support", "Turnout"]
    assert all(t["also_reported_by"] == [] for t in built)


def test_tables_with_the_same_measure_and_rows_still_merge():
    built = figures.tables([_row("e1", "Support", "News24"), _row("e2", "Support", "eNCA")])
    assert len(built) == 1 and len(built[0]["also_reported_by"]) == 1


def test_tables_for_different_populations_stay_apart():
    built = figures.tables([_row("e1", "Support", "News24", "Gauteng voters"),
                            _row("e2", "Support", "eNCA", "Western Cape voters")])
    assert sorted(t["population"] for t in built) == ["Gauteng voters", "Western Cape voters"]


def test_tables_that_state_no_measure_still_merge_when_their_rows_match():
    built = figures.tables([_row("e1", "", "News24"), _row("e2", "", "eNCA")])
    assert len(built) == 1 and built[0]["title"] == "Reported percentages"


def test_a_copy_that_drops_the_sample_or_fieldwork_still_merges():
    built = figures.tables([_row("e1", "Support", "News24", sample="1,200 adults", fieldwork="1-5 October"),
                            _row("e2", "Support", "eNCA")])
    assert len(built) == 1 and len(built[0]["also_reported_by"]) == 1


def test_tables_with_two_stated_fieldwork_periods_stay_apart():
    built = figures.tables([_row("e1", "Support", "News24", fieldwork="1-5 October"),
                            _row("e2", "Support", "eNCA", fieldwork="1-5 September")])
    assert len(built) == 2
