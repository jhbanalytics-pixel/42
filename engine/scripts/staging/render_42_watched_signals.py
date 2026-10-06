"""Render validated watched PULSE lines as a local fixture-mode HTML artifact."""

from __future__ import annotations

import argparse
import html
import json
import sys
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.open_intelligence.run_receipts import build_run_receipt
from src.analysis.open_intelligence.watched_signal_delivery import render_watched_lines

WATCHED_SIGNAL_CAP = 2
INPUT_FIELDS = frozenset({"fixture_mode", "today", "release_receipt", "lines"})


class RenderRefusal(ValueError):
    pass


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise RenderRefusal("input_invalid")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise RenderRefusal("input_invalid") from error


def _receipt(value: object):
    if not isinstance(value, Mapping):
        raise RenderRefusal("input_invalid")
    fields = dict(value)
    try:
        for name in ("signal_date", "observation_start", "observation_end"):
            fields[name] = _date(fields[name])
        completed_at = fields["completed_at"]
        if not isinstance(completed_at, str):
            raise ValueError()
        fields["completed_at"] = datetime.fromisoformat(completed_at)
        return build_run_receipt(**fields)
    except (KeyError, TypeError, ValueError) as error:
        raise RenderRefusal("release_receipt_invalid") from error


def _entries(value: object) -> tuple[tuple[dict, tuple[str, ...]], ...]:
    if not isinstance(value, list):
        raise RenderRefusal("input_invalid")
    entries = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"line", "reasons"}:
            raise RenderRefusal("input_invalid")
        if not isinstance(item["line"], Mapping) or not isinstance(item["reasons"], list):
            raise RenderRefusal("input_invalid")
        entries.append((dict(item["line"]), tuple(item["reasons"])))
    return tuple(entries)


def load_input(path: Path) -> tuple[tuple[tuple[dict, tuple[str, ...]], ...], object, date]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RenderRefusal("input_invalid") from error
    if (
        not isinstance(payload, Mapping)
        or set(payload) != INPUT_FIELDS
        or payload["fixture_mode"] is not True
    ):
        raise RenderRefusal("input_invalid")
    return _entries(payload["lines"]), _receipt(payload["release_receipt"]), _date(payload["today"])


def render_html(artifacts: tuple[dict, ...]) -> str:
    items = []
    for artifact in artifacts:
        line = artifact["line"]
        items.append(
            '<li style="margin:0 0 12px;">'
            f'<a href="{html.escape(artifact["deep_link"], quote=True)}" '
            'style="color:#1a5fb4;font-weight:bold;">'
            f"{html.escape(line['signal_name'])}</a><br>"
            f"{html.escape(line['what_changed'])}<br>"
            f"{html.escape(line['material_change'])}<br>"
            f"Reasons: {html.escape(', '.join(artifact['reasons']))}"
            "</li>"
        )
    return (
        "<!doctype html><html><body>"
        '<p style="color:#666;font-size:12px;">Fixture mode. Unsent local staging render.</p>'
        '<ul style="margin:0;padding:0 0 0 18px;font-size:13px;color:#333;">'
        f"{''.join(items)}</ul></body></html>"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        entries, receipt, today = load_input(args.input)
        html_output = render_html(
            render_watched_lines(entries, cap=WATCHED_SIGNAL_CAP, release=receipt, today=today)
        )
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(html_output)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
