"""Read client wrapper that accepts an anchored empty BigQuery table list.

BigQuery answers tables.list for an empty dataset without the tables key and
anchors the answer with a zero totalItems count. The shared list reader in
ops/deploy/production_baseline.py treats any answer without its collection as
collection_missing, and the deploy tree is pinned, so the certification
captures handle the empty answer here instead, at the transport.

For a GET of a dataset table list, first page only, whose answer carries no
keys beyond kind, etag and totalItems, with kind bigquery#tableList and an
integer totalItems of exactly 0, the wrapper records a transport
normalization and returns the answer with an empty tables list added. Every
other request and answer passes through unchanged, so an unanchored, nonzero,
paged or unexpected answer still fails as collection_missing.
"""

import re

from ops.deploy.production_baseline import ReadClient, _canonical_digest

TABLE_LIST_URL_RE = re.compile(
    r"https://bigquery\.googleapis\.com/bigquery/v2/projects/[^/]+/datasets/[^/]+/tables"
)
NORMALIZATION = "omitted_empty_bigquery_table_list"
ANCHORED_EMPTY_KEYS = {"kind", "etag", "totalItems"}


def is_anchored_empty_table_list(method: str, url: str, params: dict | None, payload) -> bool:
    return (
        method == "GET"
        and TABLE_LIST_URL_RE.fullmatch(url) is not None
        and not (params or {}).get("pageToken")
        and isinstance(payload, dict)
        and "tables" not in payload
        and set(payload) <= ANCHORED_EMPTY_KEYS
        and payload.get("kind") == "bigquery#tableList"
        and type(payload.get("totalItems")) is int
        and payload["totalItems"] == 0
    )


class AnchoredEmptyTableListClient:
    """Wraps a ReadClient and records each anchored empty table list it
    turns into an empty tables list, in the order the reads happen."""

    def __init__(self, client: ReadClient, normalizations: list[dict] | None = None):
        self.client = client
        self.normalizations = normalizations if normalizations is not None else []

    def authenticated_principal_evidence(self) -> dict:
        return self.client.authenticated_principal_evidence()

    def session_fingerprint(self) -> str:
        return self.client.session_fingerprint()

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        payload = self.client.request_json(method, url, params=params, json_body=json_body)
        if not is_anchored_empty_table_list(method, url, params, payload):
            return payload
        self.normalizations.append(
            {
                "operation": "list_tables",
                "resource": url,
                "collection": "tables",
                "normalization": NORMALIZATION,
                "original_response_digest": _canonical_digest(payload),
            }
        )
        return {**payload, "tables": []}
