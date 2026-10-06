"""Read the native collection window rows once and save them with their readback.

Read only: the runner sends the reviewed collection window query with typed
parameters (the window, the strata, the declared source and platform route
maps, the expected and known quiet routes, and the limit), never names a destination table and never writes to BigQuery. A
dry run sends the same query as a dry run and saves nothing. A real read saves
the raw rows JSON and the readback JSON beside each other with one UTC stamp,
opening both files exclusively so an earlier read is never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.analysis.open_intelligence.coverage_window import (
    COLLECTION_WINDOW_QUERY_PATH,
    RESERVED_ROUTES,
    window_readback,
)

PROJECT = "ogilvy-trends-v2"
LOCATION = "US"
ROWS_PREFIX = "coverage_window_rows"
READBACK_PREFIX = "coverage_window_readback"
_WRITING = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|TRUNCATE|ALTER|EXPORT|LOAD)\b",
    re.IGNORECASE,
)
_MARKET_ROUTE = re.compile(r"([a-z][a-z0-9_]*):([a-z][a-z0-9_]*)\Z")
# The key may hold spaces or colons (feed names); the route after the last
# colon is always a connector key.
_KEY_ROUTE = re.compile(r"(.+):([a-z][a-z0-9_]*)\Z")


def read_only_query_text(text: str) -> str:
    """Return the query text, refusing any statement that could write."""
    code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    if _WRITING.search(code):
        raise ValueError("query_text_writes")
    return text


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("timestamp_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp_needs_offset")
    return parsed


def _pair(pattern: re.Pattern[str], value: str) -> tuple[str, str]:
    match = pattern.fullmatch(value)
    if match is None or match.group(2) in RESERVED_ROUTES:
        raise argparse.ArgumentTypeError("route_invalid")
    return match.group(1), match.group(2)


def _route(value: str) -> tuple[str, str]:
    return _pair(_MARKET_ROUTE, value)


def _mapped_route(value: str) -> tuple[str, str]:
    return _pair(_KEY_ROUTE, value)


def _limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("query_limit_invalid") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError("query_limit_invalid")
    return parsed


def _bytes_cap(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("maximum_bytes_billed_invalid") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError("maximum_bytes_billed_invalid")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--window-start", required=True, type=_instant)
    parser.add_argument("--window-end", required=True, type=_instant)
    parser.add_argument("--market", required=True, action="append", dest="markets")
    parser.add_argument(
        "--source-route",
        action="append",
        type=_mapped_route,
        default=[],
        dest="source_routes",
        help="source:route for a fixed raw source value",
    )
    parser.add_argument(
        "--platform-route",
        action="append",
        type=_mapped_route,
        default=[],
        dest="platform_routes",
        help="platform:route for rows whose source is not listed",
    )
    parser.add_argument("--expected", action="append", type=_route, default=[], help="market:route")
    parser.add_argument(
        "--known-quiet", action="append", type=_route, default=[], help="market:route"
    )
    parser.add_argument("--query-limit", required=True, type=_limit)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--maximum-bytes-billed",
        type=_bytes_cap,
        default=None,
        help="byte cap BigQuery enforces on the real read; required unless --dry-run",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.dry_run and args.maximum_bytes_billed is None:
        parser.error("maximum_bytes_billed_required")
    if not args.window_start < args.window_end:
        parser.error("window_invalid")
    if len(set(args.markets)) != len(args.markets):
        parser.error("market_strata_invalid")
    for name in ("source_routes", "platform_routes"):
        keys = [key for key, _ in getattr(args, name)]
        if len(set(keys)) != len(keys):
            parser.error(f"{name}_ambiguous")
    if len(set(args.expected)) != len(args.expected):
        parser.error("expected_sources_duplicate")
    if any(market not in args.markets for market, _ in args.expected):
        parser.error("expected_market_not_a_stratum")
    if any(market not in {m for m, _ in args.expected} for market in args.markets):
        parser.error("stratum_without_expected_route")
    if any(route not in args.expected for route in args.known_quiet):
        parser.error("known_quiet_not_expected")
    return args


def _pair_array(bigquery, name: str, key: str, pairs: Sequence[tuple[str, str]]):
    """An ARRAY<STRUCT<key STRING, route STRING>> parameter, typed even when empty."""
    struct_type = bigquery.StructQueryParameterType(
        bigquery.ScalarQueryParameterType("STRING", name=key),
        bigquery.ScalarQueryParameterType("STRING", name="route"),
    )
    values = [
        bigquery.StructQueryParameter(
            None,
            bigquery.ScalarQueryParameter(key, "STRING", value),
            bigquery.ScalarQueryParameter("route", "STRING", route),
        )
        for value, route in pairs
    ]
    return bigquery.ArrayQueryParameter(name, struct_type, values)


def build_job_config(
    bigquery,
    *,
    window_start: datetime,
    window_end: datetime,
    market_strata: Sequence[str],
    source_routes: Sequence[tuple[str, str]],
    platform_routes: Sequence[tuple[str, str]],
    expected_sources: Sequence[tuple[str, str]],
    known_quiet: Sequence[tuple[str, str]],
    query_limit: int,
    dry_run: bool = False,
    maximum_bytes_billed: int | None = None,
):
    """Build the typed, read only job configuration for one window read.

    A real read always carries ``maximum_bytes_billed``, so BigQuery itself
    refuses a job that would scan more than the cap; a dry run bills nothing.
    """
    parameters = [
        bigquery.ScalarQueryParameter("window_start", "TIMESTAMP", window_start),
        bigquery.ScalarQueryParameter("window_end", "TIMESTAMP", window_end),
        bigquery.ArrayQueryParameter("market_strata", "STRING", list(market_strata)),
        _pair_array(bigquery, "source_routes", "source", source_routes),
        _pair_array(bigquery, "platform_routes", "platform", platform_routes),
        _pair_array(bigquery, "expected_sources", "market", expected_sources),
        _pair_array(bigquery, "known_quiet", "market", known_quiet),
        bigquery.ScalarQueryParameter("query_limit", "INT64", query_limit),
    ]
    config = bigquery.QueryJobConfig(query_parameters=parameters)
    if dry_run:
        config.dry_run = True
        config.use_query_cache = False
    elif maximum_bytes_billed is None:
        raise ValueError("maximum_bytes_billed_required")
    else:
        config.maximum_bytes_billed = maximum_bytes_billed
    return config


def _json_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _encode(payload: object) -> bytes:
    return (json.dumps(payload, default=_json_value, indent=2, sort_keys=False) + "\n").encode(
        "utf-8"
    )


def _save(paths: Sequence[Path], contents: Sequence[bytes]) -> None:
    """Write every payload, refusing before any file exists when one cannot land.

    The payloads arrive fully serialized. Every target must be new and its
    directory must exist before the first file is opened, so a refusal leaves
    nothing behind. The output directory is append only: nothing here ever
    removes or replaces a file, and a write that fails midway is left as it is.
    """
    for path in paths:
        if path.exists():
            raise FileExistsError(str(path))
        if not path.parent.is_dir():
            raise FileNotFoundError(str(path.parent))
    for path, content in zip(paths, contents, strict=True):
        with path.open("xb") as stream:
            stream.write(content)


def main(
    argv: Sequence[str] | None = None,
    *,
    client: object | None = None,
    bigquery: object | None = None,
    now: datetime | None = None,
) -> int:
    args = _parse(argv)
    if bigquery is None:
        from google.cloud import bigquery
    if client is None:
        client = bigquery.Client(project=PROJECT, location=LOCATION)
    sql = read_only_query_text(Path(COLLECTION_WINDOW_QUERY_PATH).read_text(encoding="utf-8"))
    config = build_job_config(
        bigquery,
        window_start=args.window_start,
        window_end=args.window_end,
        market_strata=args.markets,
        source_routes=args.source_routes,
        platform_routes=args.platform_routes,
        expected_sources=args.expected,
        known_quiet=args.known_quiet,
        query_limit=args.query_limit,
        dry_run=args.dry_run,
        maximum_bytes_billed=args.maximum_bytes_billed,
    )
    job = client.query(sql, job_config=config)
    if args.dry_run:
        print(
            json.dumps(
                {"dry_run": True, "total_bytes_processed": job.total_bytes_processed},
                separators=(",", ":"),
            )
        )
        return 0
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    rows_path = args.output_dir / f"{ROWS_PREFIX}_{stamp}.json"
    readback_path = args.output_dir / f"{READBACK_PREFIX}_{stamp}.json"
    try:
        rows = [dict(row) for row in job.result()]
        rows_bytes = _encode(rows)
    except (OSError, TypeError, ValueError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2
    try:
        readback = window_readback(
            rows,
            window=(args.window_start, args.window_end),
            market_strata=args.markets,
            expected_sources=args.expected,
            known_quiet=args.known_quiet,
            query_limit=args.query_limit,
            job_id=job.job_id,
            output_sha256=hashlib.sha256(rows_bytes).hexdigest(),
            route_maps={
                "source_routes": [list(pair) for pair in args.source_routes],
                "platform_routes": [list(pair) for pair in args.platform_routes],
            },
        )
    except ValueError as refusal:
        # A refused native read keeps its evidence: the raw rows land on their
        # own, stamped and append only, and no readback is written.
        print(f"window_readback_refused: {refusal}", file=sys.stderr)
        try:
            _save((rows_path,), (rows_bytes,))
            print(json.dumps({"refused": str(refusal), "rows_output": str(rows_path)}))
        except OSError as error:
            print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2
    try:
        readback_bytes = _encode(readback)
        _save((rows_path, readback_path), (rows_bytes, readback_bytes))
    except (OSError, ValueError) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "job_id": job.job_id,
                "rows": len(rows),
                "maximum_bytes_billed": args.maximum_bytes_billed,
                "zero_hides_nothing": readback["zero_hides_nothing"],
                "rows_output": str(rows_path),
                "readback_output": str(readback_path),
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
