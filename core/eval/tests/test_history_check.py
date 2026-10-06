"""core/eval/history_check.py against a fixture warehouse: the BUILD.md 3.3 check, never run live here."""

import io
from datetime import date, datetime

from core.agent.tests.test_history import WAVES, DuckWarehouse, fixture_con
from core.agent.tools.dates import SAST
from core.eval import history_check

NOW = datetime(2026, 9, 29, 10, 30, tzinfo=SAST)


def run(argv, wh):
    out = io.StringIO()
    code = history_check.main(argv, warehouse=wh, out=out, now=NOW)
    return code, out.getvalue()


def test_three_recurring_items_pass():
    wh = DuckWarehouse(fixture_con(waves=WAVES + [("i_braai", "ZA", date(2025, 12, 1), date(2025, 12, 5),
                                                   date(2025, 12, 3), 10)]))
    code, text = run([], wh)
    assert code == 0
    assert "i_braai ZA (Braai season): 1 earlier wave\n" in text
    assert "PASS: 3 of 3 items found their earlier waves" in text


def test_with_no_ids_it_picks_items_with_more_than_one_wave_and_fails_short_of_three():
    wh = DuckWarehouse(fixture_con())
    code, text = run([], wh)
    assert code == 1
    # i_heritage has three waves in ZA and i_amapiano two; nothing else in the fixtures has more than one.
    assert "i_heritage ZA" in text and "i_amapiano ZA" in text
    assert "2024-09-10 to 2024-09-30, peak 40 posts on 2024-09-24" in text
    assert "2026-03-01 to 2026-03-20, peak 12 posts on 2026-03-09" in text
    assert "2026-09-15" not in text.split("earlier waves")[1].split("i_amapiano")[0]  # the latest wave is not earlier
    assert "FAIL: 2 of 2 items found their earlier waves; the BUILD check needs 3" in text


def test_each_wave_line_names_its_query_id():
    code, text = run(["i_heritage"], DuckWarehouse(fixture_con()))
    assert text.count("[q_") >= 2


def test_an_item_with_one_wave_fails_the_check():
    code, text = run(["i_heritage", "i_braai", "i_amapiano"], DuckWarehouse(fixture_con()))
    assert code == 1
    assert "i_braai: no earlier wave" in text
    assert "FAIL: 2 of 3 items found their earlier waves" in text


def test_market_narrows_the_pick():
    code, text = run(["--market", "KE"], DuckWarehouse(fixture_con()))
    assert code == 1
    assert "No item in KE has more than one wave" in text


def test_every_query_is_read_only_through_the_agent_guard():
    from core.agent.tools.sql_query import check_sql

    wh = DuckWarehouse(fixture_con())
    run([], wh)
    for sql, _ in wh.runs:
        check_sql(sql)
        assert date(2026, 9, 29).isoformat() not in sql


def test_the_auto_pick_needs_two_waves_of_eight_or_more_posts():
    small = [("i_braai", "ZA", date(2025, 12, 1), date(2025, 12, 5), date(2025, 12, 3), 3)]
    code, text = run([], DuckWarehouse(fixture_con(waves=WAVES + small)))
    assert "i_braai" not in text
    assert "FAIL: 2 of 2 items found their earlier waves; the BUILD check needs 3" in text


def test_a_named_id_counts_only_earlier_waves_of_eight_or_more_posts():
    # Round 2 review note 5 replaced the earlier rule, under which this sub-8 wave counted as an earlier wave.
    small = [("i_braai", "ZA", date(2025, 12, 1), date(2025, 12, 5), date(2025, 12, 3), 3)]
    code, text = run(["i_braai"], DuckWarehouse(fixture_con(waves=WAVES + small)))
    assert "i_braai: no earlier wave" in text and "2025-12-03" not in text
    big = [("i_braai", "ZA", date(2025, 12, 1), date(2025, 12, 5), date(2025, 12, 3), 8)]
    code, text = run(["i_braai"], DuckWarehouse(fixture_con(waves=WAVES + big)))
    assert "i_braai (Braai season): 1 earlier wave\n" in text
