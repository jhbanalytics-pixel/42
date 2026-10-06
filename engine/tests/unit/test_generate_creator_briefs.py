from copy import deepcopy
from datetime import UTC, date, datetime

import pytest


def source():
    return {
        "analysis_id": "analysis-1",
        "trend_date": date(2026, 7, 6),
        "market": "za",
        "query_group": "music_amapiano",
        "trend_score": 0.5,
        "cycle_id": None,
        "nano_banana_prompt": "Rooftop dancers",
        "lyria_prompt": "Music rhythm",
        "campaign_angles": ["Source-backed angle"],
        "analyzed_at": datetime(2026, 7, 6, 12, tzinfo=UTC),
    }


def test_projection_uses_only_retained_fields():
    from src.analysis.generate_creator_briefs import project_creator_brief

    original = source()
    row = project_creator_brief(original)
    assert row == {
        "brief_id": "analysis-1",
        "trend_date": original["trend_date"],
        "market": "za",
        "trend_name": "music_amapiano",
        "trend_score": 0.5,
        "cycle_id": None,
        "nano_banana_prompt": "Rooftop dancers",
        "lyria_prompt": "Music rhythm",
        "campaign_angles": ["Source-backed angle"],
        "nano_banana_trend_trigger": None,
        "endorsement_disclosure": None,
        "creator_tier": None,
        "status": "generated",
        "generated_at": original["analyzed_at"],
        "reviewed_at": None,
        "dispatched_at": None,
        "exported_to_sheet": False,
    }
    row["campaign_angles"].append("changed")
    assert original["campaign_angles"] == ["Source-backed angle"]


@pytest.mark.parametrize(
    "field",
    [
        "analysis_id",
        "trend_date",
        "market",
        "query_group",
        "nano_banana_prompt",
        "lyria_prompt",
        "analyzed_at",
    ],
)
def test_missing_required_retained_fields_refuse(field):
    from src.analysis.generate_creator_briefs import project_creator_brief

    row = source()
    row.pop(field)
    with pytest.raises(ValueError, match=field):
        project_creator_brief(row)


def test_persistence_is_idempotent_and_reads_back():
    from src.analysis.generate_creator_briefs import persist_creator_briefs, project_creator_brief

    row = project_creator_brief(source())
    stored = {}
    writes = []

    def reader(ids):
        return [deepcopy(stored[key]) for key in ids if key in stored]

    def sink(rows):
        writes.append(deepcopy(rows))
        stored.update({item["brief_id"]: deepcopy(item) for item in rows})
        return len(rows)

    assert persist_creator_briefs([row], row_sink=sink, row_reader=reader) == 1
    assert persist_creator_briefs([row], row_sink=sink, row_reader=reader) == 0
    assert len(writes) == 1


@pytest.mark.parametrize("case", ["missing", "different", "duplicate"])
def test_readback_must_match(case):
    from src.analysis.generate_creator_briefs import persist_creator_briefs, project_creator_brief

    row = project_creator_brief(source())
    calls = 0

    def reader(ids):
        nonlocal calls
        calls += 1
        if calls == 1 or case == "missing":
            return []
        if case == "duplicate":
            return [row, row]
        return [{**row, "trend_name": "different"}]

    with pytest.raises(ValueError, match="readback"):
        persist_creator_briefs([row], row_sink=lambda rows: len(rows), row_reader=reader)


def test_conflicting_existing_row_refuses_before_write():
    from src.analysis.generate_creator_briefs import persist_creator_briefs, project_creator_brief

    row = project_creator_brief(source())
    writes = []
    with pytest.raises(ValueError, match="conflict"):
        persist_creator_briefs(
            [row],
            row_sink=lambda rows: writes.extend(rows),
            row_reader=lambda ids: [{**row, "trend_score": 0.9}],
        )
    assert writes == []
