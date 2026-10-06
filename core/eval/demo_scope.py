import re
from math import isfinite
from types import MappingProxyType

from core.agent.checks import _verbatim
from core.agent.writer import _claim_query_scope
from core.api.dossiers import admitted as dossier_admitted


_DEMO_SOURCES = {
    "DEMO-01": {
        "obs1_585d958e17e03bf9f9ccad5c7c2f0b38",
        "obs1_da53ac54e21872d0554146ef21188590",
        "obs1_78054f141c7fb15c269a8c1cfac4a1ae",
    },
    "DEMO-02": {"obs1_4fce7a7d6895ef0915e0b217fbddf290"},
    "DEMO-03": {"obs1_da53ac54e21872d0554146ef21188590"},
    "DEMO-04": {"obs1_4fce7a7d6895ef0915e0b217fbddf290"},
    "DEMO-05": {"obs1_dce53bd6bb321b436ac9d0078763dc04"},
}
_TREND_MARKETS = MappingProxyType({
    "TREND-ZA-01": "ZA",
    "TREND-NG-01": "NG",
    "TREND-KE-01": "KE",
})
_DEMO_DATES = {
    "DEMO-02": "2026-09-29",
    "DEMO-03": "2026-09-24",
    "DEMO-04": "2026-09-29",
    "DEMO-05": "2026-09-28",
}
_BREADTH = re.compile(
    r"\b(?:national(?:ly)?|countrywide|dominates?|dominated|domination|momentum|trending|trend|viral|"
    r"widespread|sustained attention|public discourse|public discussion|social discourse|audience opinion|"
    r"audience reaction|growth|rising|surge|increas(?:e|ed|ing)|most|top|ranking|ranked)\b|"
    r"\bacross\s+(?:\d+|two|three|four|multiple|several|many)\s+"
    r"(?:platforms|markets|posts|sources|creators|authors)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_PEOPLE = re.compile(
    r"\b(?:south african|nigerian|kenyan)\s+(?:fans?|creators?|users?|audiences?|people|viewers?|consumers?)\b|"
    r"\b(?:south africans?|nigerians?|kenyans?)\b",
    re.IGNORECASE,
)
_AGGREGATE_SQL = re.compile(
    r"\b(?:count|countif|sum|avg|average|min|max|array_agg|approx_count_distinct)\s*\(", re.IGNORECASE
)
_METRIC_UNITS = {
    "view": "views",
    "views": "views",
    "like": "likes",
    "likes": "likes",
    "comment": "comments",
    "comments": "comments",
    "share": "shares",
    "shares": "shares",
    "engagement": "engagement",
    "engagements": "engagement",
}
_NUMBER = re.compile(r"(?<![\w])\d[\d,]*(?:\.\d+)?")
_MONTH = r"January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
_MONTH_NUMBER = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_DATE_SPAN = re.compile(
    rf"\b(?:(?P<iso_year>20\d{{2}})-(?P<iso_month>\d{{2}})-(?P<iso_day>\d{{2}})|"
    rf"(?P<day_first>\d{{1,2}})\s+(?P<month_first>{_MONTH})(?:\s+(?P<year_first>20\d{{2}}))?|"
    rf"(?P<month_last>{_MONTH})\s+(?P<day_last>\d{{1,2}})(?:,?\s+(?P<year_last>20\d{{2}}))?)\b",
    re.IGNORECASE,
)
_FETCH_POSTS_SOURCE_AGG = re.compile(
    r"\bFROM\s+intelligence_42_core\.posts\s+p\b.*?"
    r"\bLEFT JOIN\s+\(SELECT\s+sm\.post_id,\s*ARRAY_AGG\(ss\)\s+AS\s+source_sightings\s+"
    r"FROM\s+intelligence_42_core\.v_post_source_markets\s+sm\b.*?"
    r"\bCROSS JOIN UNNEST\(sm\.source_sightings\)\s+AS\s+ss\b.*?"
    r"\bWHERE\s+ss\.obs_date\s+BETWEEN\s+@source_since\s+AND\s+@source_until\s+"
    r"\bGROUP BY\s+sm\.post_id\)\s+o\s+ON\s+o\.post_id\s*=\s*p\.post_id\b.*?"
    r"\bWHERE\s+p\.post_id\s+IN\s*\((?P<ids>[^)]+)\)",
    re.IGNORECASE | re.DOTALL,
)
_POST_ID_PARAM = re.compile(r"post_id_\d+\Z")


def _record_map(value):
    if isinstance(value, dict):
        entries = value.items()
    elif isinstance(value, list):
        entries = enumerate(value)
    else:
        return {}
    records = {}
    for key, record in entries:
        if not isinstance(record, dict):
            continue
        evidence_id = record.get("evidence_id") or record.get("id") or record.get("post_id") or key
        if isinstance(evidence_id, str):
            records[evidence_id] = record
    return records


def _query_map(context):
    queries = context.get("queries") if isinstance(context, dict) else None
    if isinstance(queries, dict):
        return queries
    if isinstance(queries, list):
        return {
            query.get("query_id") or query.get("id"): query
            for query in queries
            if isinstance(query, dict) and isinstance(query.get("query_id") or query.get("id"), str)
        }
    return {}


def _window(context, question_id):
    if not isinstance(context, dict):
        return None
    start, end = context.get("window_start"), context.get("window_end")
    if isinstance(context.get("window"), dict):
        start = start or context["window"].get("from") or context["window"].get("start")
        end = end or context["window"].get("to") or context["window"].get("end")
    if not (isinstance(start, str) and isinstance(end, str)):
        target_date = _DEMO_DATES.get(question_id)
        return (target_date, target_date) if target_date else None
    return start[:10], end[:10]


def _source_date(record):
    for key in ("post_date", "published_at", "posted_at", "created_at", "date"):
        value = record.get(key)
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return None


def _numeric_tokens(text):
    return {token.replace(",", "") for token in _NUMBER.findall(text or "")}


def _date_spans(text, source_dates):
    spans = []
    for match in _DATE_SPAN.finditer(text or ""):
        if match.group("iso_year"):
            parts = (int(match.group("iso_year")), int(match.group("iso_month")), int(match.group("iso_day")))
        elif match.group("day_first"):
            month = _MONTH_NUMBER[match.group("month_first").lower()]
            year = int(match.group("year_first")) if match.group("year_first") else None
            parts = (year, month, int(match.group("day_first")))
        else:
            month = _MONTH_NUMBER[match.group("month_last").lower()]
            year = int(match.group("year_last")) if match.group("year_last") else None
            parts = (year, month, int(match.group("day_last")))
        if any(
            (parts[0] is None or parts[0] == int(source_date[:4]))
            and parts[1:] == (int(source_date[5:7]), int(source_date[8:10]))
            for source_date in source_dates
            if isinstance(source_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date)
        ):
            spans.append(match.span())
    return spans


def _unaccounted_numbers(text, supported, quoted, source_dates):
    spans = _date_spans(text, source_dates)
    return {
        match.group().replace(",", "")
        for match in _NUMBER.finditer(text or "")
        if not any(start <= match.start() and match.end() <= end for start, end in spans)
        and match.group().replace(",", "") not in supported
        and match.group().replace(",", "") not in quoted
    }


def _trusted_fetch_posts_metric(query, evidence_id, metric, value):
    if not isinstance(query, dict) or not re.fullmatch(
        r"fetch_posts:\s*\d+\s+posts listed in query rows", str(query.get("purpose") or ""), re.IGNORECASE
    ):
        return False
    sql = query.get("sql")
    if not isinstance(sql, str):
        return False
    match = _FETCH_POSTS_SOURCE_AGG.search(sql)
    if not match:
        return False
    metadata_agg = re.compile(r"ARRAY_AGG\(ss\)\s+AS\s+source_sightings", re.IGNORECASE)
    other_sql = metadata_agg.sub("", sql, count=1)
    if _AGGREGATE_SQL.search(other_sql):
        return False
    select_sql = re.split(r"\bFROM\b", sql, maxsplit=1, flags=re.IGNORECASE)[0]
    if not re.search(r"\bp\.post_id\b", select_sql, re.IGNORECASE) or not re.search(
        rf"\bp\.{re.escape(metric)}\b", select_sql, re.IGNORECASE
    ):
        return False
    params = query.get("params")
    if not isinstance(params, dict) or not params.get("source_since") or not params.get("source_until"):
        return False
    post_params = re.findall(r"@(?P<name>post_id_\d+)", match.group("ids"), re.IGNORECASE)
    if not any(_POST_ID_PARAM.fullmatch(name) and params.get(name) == evidence_id for name in post_params):
        return False
    rows = query.get("rows")
    return isinstance(rows, list) and any(
        isinstance(row, dict)
        and row.get("post_id") == evidence_id
        and type(row.get(metric)) in (int, float)
        and row.get(metric) == value
        for row in rows
    )


def _claim_reasons(claim, question_id, records, queries, window, receipt_records=None):
    reasons = []
    trend_market = _TREND_MARKETS.get(question_id)
    text = claim.get("text")
    if not isinstance(text, str) or not text.strip():
        reasons.append("claim has no text")
        text = ""
    if _BREADTH.search(text):
        reasons.append("claim makes a national, ranking, momentum or cross-source breadth claim")
    if _UNSUPPORTED_PEOPLE.search(text):
        reasons.append("claim assigns a nationality or audience identity not established by a source record")

    evidence_ids = claim.get("evidence_ids")
    if not isinstance(evidence_ids, list) or not evidence_ids or any(not isinstance(item, str) for item in evidence_ids):
        reasons.append("claim has no valid source citations")
        evidence_ids = []
    if len(set(evidence_ids)) != len(evidence_ids):
        reasons.append("claim repeats a source citation")
    if trend_market is None:
        allowed_sources = _DEMO_SOURCES[question_id]
        if any(evidence_id not in allowed_sources for evidence_id in evidence_ids):
            reasons.append("claim cites a source outside this demo question")
        if len(evidence_ids) > (3 if question_id == "DEMO-01" else 1):
            reasons.append("claim combines more source records than the question asks about")
    else:
        if any(evidence_id not in (receipt_records or {}) for evidence_id in evidence_ids):
            reasons.append("claim cites a source absent from the market-bound receipt context")

    cited_records = {}
    for evidence_id in evidence_ids:
        record = records.get(evidence_id)
        if record is None:
            reasons.append(f"source {evidence_id} does not resolve in the receipt")
            continue
        cited_records[evidence_id] = record
        if trend_market is not None:
            receipt_record = (receipt_records or {}).get(evidence_id)
            if (not isinstance(receipt_record, dict)
                    or receipt_record.get("market") != trend_market
                    or receipt_record.get("flags") != []):
                reasons.append(f"source {evidence_id} lacks direct market evidence")
        source_date = _source_date(record)
        if window is None:
            reasons.append("receipt has no recorded data window")
        elif source_date is None or not window[0] <= source_date <= window[1]:
            reasons.append(f"source {evidence_id} has no date inside the recorded data window")

    quotes = claim.get("quotes") or []
    if not isinstance(quotes, list):
        reasons.append("claim quotes are not a list")
        quotes = []
    quoted_ids = set()
    quoted_texts = []
    for quote in quotes:
        if not isinstance(quote, dict):
            reasons.append("claim has a quote without an evidence citation")
            continue
        evidence_id, quote_text = quote.get("evidence_id"), quote.get("text")
        record = cited_records.get(evidence_id)
        source_text = record.get("text") if record else None
        if not isinstance(quote_text, str) or not isinstance(source_text, str) or not _verbatim(quote_text, source_text):
            reasons.append("claim quote does not resolve verbatim against its cited source")
            continue
        quoted_ids.add(evidence_id)
        quoted_texts.append(quote_text)
    if any(evidence_id not in quoted_ids for evidence_id in cited_records):
        reasons.append("each cited source needs an exact quote")

    numbers = claim.get("numbers") or []
    if not isinstance(numbers, list):
        reasons.append("claim numbers are not a list")
        numbers = []
    supported_numbers = set()
    if numbers and len(cited_records) != 1:
        reasons.append("numeric claims must describe one cited source record")
    for number in numbers:
        if not isinstance(number, dict) or type(number.get("value")) not in (int, float):
            reasons.append("claim has a number without a numeric receipt")
            continue
        value = number["value"]
        if not isfinite(value):
            reasons.append("claim number is not finite")
            continue
        unit = str(number.get("unit") or "").strip().lower()
        metric = _METRIC_UNITS.get(unit)
        if metric is None or len(cited_records) != 1:
            reasons.append("number is an aggregate or is not a single-post engagement metric")
            continue
        source = next(iter(cited_records.values()))
        if type(source.get(metric)) not in (int, float) or source.get(metric) != value:
            reasons.append(f"{unit} does not match the cited source record")
            continue
        supported_numbers.add(str(value).rstrip("0").rstrip(".") if isinstance(value, float) else str(value))
        query_id = number.get("query_id")
        query = queries.get(query_id) if isinstance(query_id, str) else None
        if query is not None:
            sql = query.get("sql") if isinstance(query, dict) else None
            if not isinstance(sql, str) or not sql.strip():
                reasons.append(f"query {query_id} has no recorded SQL scope")
            elif _AGGREGATE_SQL.search(sql):
                evidence_id = next(iter(cited_records), None)
                if not _trusted_fetch_posts_metric(query, evidence_id, metric, value):
                    has_market = re.search(r"\bmarket\s*(?:=|in\s*\()", sql, re.IGNORECASE)
                    detail = " and has no market predicate" if not has_market else ""
                    reasons.append(f"query {query_id} aggregates across source records{detail}")
                elif _claim_query_scope({"numbers": [number]}, {query_id: query}) is None:
                    reasons.append(f"number does not resolve in query {query_id}")
            elif _claim_query_scope({"numbers": [number]}, {query_id: query}) is None:
                reasons.append(f"number does not resolve in query {query_id}")

    quoted_numbers = set().union(*(_numeric_tokens(quote) for quote in quoted_texts)) if quoted_texts else set()
    source_dates = []
    for record in cited_records.values():
        source_date = _source_date(record)
        if source_date:
            source_dates.append(source_date)
    unaccounted = _unaccounted_numbers(text, supported_numbers, quoted_numbers, source_dates)
    if unaccounted:
        reasons.append("claim contains numbers absent from its source quote or single-post metrics")
    return list(dict.fromkeys(reasons))


def _field_is_supported(text, claims):
    for claim in claims:
        if _verbatim(text, claim.get("text")):
            return True
        for quote in claim.get("quotes") or []:
            if isinstance(quote, dict) and _verbatim(text, quote.get("text")):
                return True
    return False


def _drop(dropped, claim_id, reason):
    dropped.append({"claim_id": claim_id, "reason": reason})


def assess_answer(receipt, question_id):
    dropped = []
    if question_id not in _DEMO_SOURCES and question_id not in _TREND_MARKETS:
        return {
            "admitted_claim_ids": [],
            "dropped": [],
            "safe": False,
            "reasons": ["question id is outside the frozen demo set"],
        }
    if not isinstance(receipt, dict) or not isinstance(receipt.get("raw_answer"), dict):
        return {
            "admitted_claim_ids": [],
            "dropped": [],
            "safe": False,
            "reasons": ["receipt has no raw answer object"],
        }
    if receipt.get("question_id") is not None and receipt.get("question_id") != question_id:
        return {
            "admitted_claim_ids": [],
            "dropped": [],
            "safe": False,
            "reasons": ["receipt question id does not match the requested demo question"],
        }

    trend_market = _TREND_MARKETS.get(question_id)
    if trend_market is not None:
        provenance = receipt.get("context_provenance")
        receipt_context = receipt.get("context")
        if (receipt.get("question_id") != question_id
                or receipt.get("markets") != [trend_market]
                or not isinstance(provenance, dict)
                or provenance.get("market") != trend_market
                or provenance.get("source_ids") != []
                or not isinstance(receipt_context, dict)
                or receipt_context.get("market") != trend_market):
            return {
                "admitted_claim_ids": [],
                "dropped": [],
                "safe": False,
                "reasons": ["trend question market binding is absent or mismatched"],
            }

    answer = receipt["raw_answer"]
    context = receipt.get("context")
    context = context if isinstance(context, dict) else {}
    receipt_records = _record_map(context.get("evidence"))
    records = dict(receipt_records)
    if trend_market is None:
        records.update(_record_map(answer.get("evidence")))
    queries = _query_map(context)
    window = _window(context, question_id)
    checked, left_out = dossier_admitted(answer)
    dropped.extend(left_out)
    admitted_claims = []
    seen = set()
    for claim in answer.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        claim_id = claim.get("id")
        if not isinstance(claim_id, str) or not claim_id or claim_id in seen:
            continue
        seen.add(claim_id)
        if claim_id not in checked:
            continue
        reasons = _claim_reasons(claim, question_id, records, queries, window, receipt_records)
        if reasons:
            _drop(dropped, claim_id, "; ".join(reasons))
        else:
            admitted_claims.append(claim)

    admitted_ids = [claim["id"] for claim in admitted_claims]
    admitted_set = set(admitted_ids)
    short_answer = answer.get("short_answer")
    if isinstance(short_answer, str) and short_answer.strip():
        if _BREADTH.search(short_answer) or not _field_is_supported(short_answer, admitted_claims):
            reason = (
                "short answer makes a broad claim"
                if _BREADTH.search(short_answer)
                else "short answer is not supported verbatim by an admitted claim or quote"
            )
            _drop(dropped, "short_answer", reason)

    for index, item in enumerate(answer.get("so_what") or []):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
            continue
        refs = item.get("claim_ids") or []
        if (
            _BREADTH.search(item["text"])
            or not isinstance(refs, list)
            or not refs
            or any(ref not in admitted_set for ref in refs)
            or not _field_is_supported(item["text"], admitted_claims)
        ):
            _drop(dropped, f"so_what/{index}", "so what text is broad or lacks an admitted source-level claim")

    safe = bool(admitted_ids)
    reasons = [] if safe else ["no source-level claim passed the demo scope checks"]
    return {
        "admitted_claim_ids": admitted_ids,
        "dropped": dropped,
        "safe": safe,
        "reasons": reasons,
    }
