"""core/config/caps.yaml must equal the caps table in docs/full-42/SETUP.md, the one place caps are set."""

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from core.config.caps import model_daily_usd, video_daily

ROOT = Path(__file__).resolve().parents[3]
CAPS = yaml.safe_load((ROOT / "core" / "config" / "caps.yaml").read_text(encoding="utf-8"))
SETUP = (ROOT / "docs" / "full-42" / "SETUP.md").read_text(encoding="utf-8")


def _table():
    """Rows of the caps table under "Cost and credit guards", keyed by cap name."""
    section = SETUP.split("## Cost and credit guards", 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and re.fullmatch(r"[A-Z_]+", cells[0]):
            rows[cells[0]] = (cells[1], cells[2])
    return rows


def _numbers(text):
    return [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text)]


ROWS = _table()


def test_table_found():
    for name in ("ENGINE_DAILY", "ASK_DAILY", "EVAL_DAILY", "BUILD_DAILY", "MONTHLY", "BALANCE_FLOOR",
                 "PULSE_DAILY", "SCHEDULED_DAILY", "MODEL_DAILY_USD", "VIDEO_DAILY"):
        assert name in ROWS, name


def test_engine_daily_and_shares():
    value = ROWS["ENGINE_DAILY"][0]
    assert CAPS["ENGINE_DAILY"]["total"] == _numbers(value)[0]
    shares = {k: int(v) for k, v in re.findall(r"(collect|confirm|reserve) (\d+)", value)}
    assert shares == {k: CAPS["ENGINE_DAILY"][k] for k in ("collect", "confirm", "reserve")}
    assert sum(shares.values()) == CAPS["ENGINE_DAILY"]["total"]


def test_ask_daily():
    assert CAPS["ASK_DAILY"] == _numbers(ROWS["ASK_DAILY"][0])[0]


def test_eval_daily():
    live, refresh = _numbers(ROWS["EVAL_DAILY"][0])
    assert CAPS["EVAL_DAILY"] == {"live": live, "refresh": refresh}


def test_build_daily():
    value = ROWS["BUILD_DAILY"][0]
    before, after = _numbers(value)
    assert "until the morning schedule starts, then" in value
    assert CAPS["BUILD_DAILY"] == {"before_schedule": before, "after_schedule": after}


def test_monthly_and_throttle_order():
    value, stops = ROWS["MONTHLY"]
    assert CAPS["MONTHLY"]["total"] == _numbers(value)[0]
    order = sorted(("ask", "build"), key=lambda s: stops.lower().index(s))
    assert CAPS["MONTHLY"]["throttle_order"] == order
    assert "morning run is protected" in stops
    assert CAPS["MONTHLY"]["protected"] == ["collect", "confirm", "reserve"]


def test_pulse_daily():
    # Albert, 29 September: the intraday pulse's own share, read by core/collect/pulse.py
    assert CAPS["PULSE_DAILY"] == _numbers(ROWS["PULSE_DAILY"][0])[0] == 60


def test_video_daily_is_a_share_of_its_own_at_albert_s_numbers():
    # Video reading (BUILD.md 2.6): Albert set 20 clips and 200 credits a day on 2 October, raised to 60 and 600
    # on 3 October.
    value = ROWS["VIDEO_DAILY"][0]
    clips, credits = _numbers(value)[:2]
    assert CAPS["VIDEO_DAILY"] == {"clips": clips, "credits": credits} == {"clips": 60, "credits": 600}
    assert "outside ENGINE_DAILY" in value
    assert video_daily() == {"clips": 60, "credits": 600}


def test_video_daily_reads_the_file_it_is_given(tmp_path):
    path = tmp_path / "caps.yaml"
    path.write_text("VIDEO_DAILY:\n  clips: 20\n  credits: 300\n", encoding="utf-8")
    assert video_daily(path) == {"clips": 20, "credits": 300}
    path.write_text("MODEL_DAILY_USD:\n  default: 20\n", encoding="utf-8")
    assert video_daily(path) == {"clips": 0, "credits": 0}


def test_scheduled_daily_is_a_share_of_ask_daily():
    # Albert, 29 September: scheduled questions spend inside ASK_DAILY, read by core/api/scheduled.py
    value = ROWS["SCHEDULED_DAILY"][0]
    assert CAPS["SCHEDULED_DAILY"] == _numbers(value)[0] == 120
    assert "share of ASK_DAILY" in value
    assert CAPS["SCHEDULED_DAILY"] <= CAPS["ASK_DAILY"]


def test_balance_floor():
    assert CAPS["BALANCE_FLOOR"] == _numbers(ROWS["BALANCE_FLOOR"][0])[0]


WINDOW = ('MODEL_DAILY_USD:\n  default: 20\n  temporary:\n    amount: 80\n'
          '    starts_on: "2026-10-01"\n    ends_on: "2026-10-02"\n')


def test_model_daily_usd_uses_inclusive_sast_dates(tmp_path):
    path = tmp_path / "caps.yaml"
    path.write_text(WINDOW, encoding="utf-8")
    assert model_daily_usd(path, now=datetime(2026, 9, 30, 21, 59, 59, tzinfo=timezone.utc)) == 20.0
    assert model_daily_usd(path, now=datetime(2026, 9, 30, 22, 0, tzinfo=timezone.utc)) == 80.0
    assert model_daily_usd(path, now=datetime(2026, 10, 2, 21, 59, 59, tzinfo=timezone.utc)) == 80.0
    assert model_daily_usd(path, now=datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc)) == 20.0
    assert model_daily_usd(path, now=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)) == 20.0


def test_model_daily_usd_changes_on_a_second_call_in_the_same_process(tmp_path):
    path = tmp_path / "caps.yaml"
    path.write_text(WINDOW, encoding="utf-8")
    before_expiry = datetime(2026, 10, 2, 21, 59, 59, tzinfo=timezone.utc)
    after_expiry = datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc)
    assert model_daily_usd(path, now=before_expiry) == 80.0
    assert model_daily_usd(path, now=after_expiry) == 20.0


def test_model_daily_usd_is_albert_s_3_october_level():
    # Albert, 3 October: USD 50 a day, no temporary window.
    assert model_daily_usd(now=datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)) == 50.0
    assert model_daily_usd(now=datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)) == 50.0


def test_model_daily_usd_config_and_setup_table_stay_aligned():
    value = ROWS["MODEL_DAILY_USD"][0]
    assert CAPS["MODEL_DAILY_USD"] == {"default": 50}
    assert _numbers(value)[0] == 50 and "USD 50 a day" in value
    assert "raised 3 Oct 2026 at Albert's request" in value


def test_every_number_in_caps_yaml_is_checked():
    def numbers(node):
        if isinstance(node, dict):
            return [n for v in node.values() for n in numbers(v)]
        if isinstance(node, list):
            return [n for v in node for n in numbers(v)]
        return [node] if isinstance(node, int) else []

    in_table = [n for name, (value, _) in ROWS.items() if name in CAPS for n in _numbers(value)]
    for n in numbers(CAPS):
        assert n in in_table, n
