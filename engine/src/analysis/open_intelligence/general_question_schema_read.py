"""Fixed staging metadata reads; returned schemas remain unbound data."""

import json
import os
from datetime import timedelta
from time import monotonic
from urllib.parse import parse_qs, urlparse

from google.cloud import bigquery

from src.analysis.open_intelligence.general_question_control import (
    QuestionStoreError,
    control_time,
    utc_time,
)
from src.analysis.open_intelligence.general_question_deployment import validate_question_invocation
from src.analysis.open_intelligence.general_question_schema import (
    _DATASET,
    _PROJECT,
    _TABLES,
    _fields,
    validate_source_schemas,
)

_IDENTITY = "listening-post-staging@ogilvy-trends-v2.iam.gserviceaccount.com"


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("physical_schema_invalid")
        value[key] = item
    return value


def _read_question_schema_group(
    invocation, *, store, scope, runtime_identity, credentials, now, group
):
    """Read one closed schema group under the existing invocation."""
    started = monotonic()
    now = utc_time(now)
    context = validate_question_invocation(
        invocation, store=store, scope=scope, runtime_identity=runtime_identity
    )
    if getattr(credentials, "service_account_email", None) != _IDENTITY or getattr(
        credentials, "quota_project_id", None
    ) not in (None, _PROJECT):
        raise QuestionStoreError("identity_invalid")
    if any(
        os.environ.get(key)
        for key in ("BIGQUERY_EMULATOR_HOST", "GOOGLE_API_USE_CLIENT_CERTIFICATE")
    ) or os.environ.get("GOOGLE_API_USE_MTLS_ENDPOINT", "auto") not in ("auto", "never"):
        raise QuestionStoreError("query_environment_invalid")
    if group == "source":
        table_names = _TABLES
    elif group == "copy":
        from src.analysis.open_intelligence.general_question_copy_validation import (
            source_copy_schema_requirements,
        )

        prefix = f"{_PROJECT}.{_DATASET}."
        requirements = source_copy_schema_requirements()
        if any(type(name) is not str or not name.startswith(prefix) for name in requirements):
            raise ValueError("physical_schema_invalid")
        table_names = tuple(name.removeprefix(prefix) for name in requirements)
    else:
        raise ValueError("physical_schema_invalid")
    deadline = control_time(context["admission"]["deadline_at"])

    def current():
        return now + timedelta(seconds=monotonic() - started)

    def remaining():
        seconds = (deadline - current()).total_seconds()
        if seconds <= 0:
            raise QuestionStoreError("request_expired")
        return min(5.0, seconds)

    remaining()
    paths = {
        f"/bigquery/v2/projects/{_PROJECT}/datasets/{_DATASET}/tables/{name}": name
        for name in table_names
    }
    client = bigquery.Client(
        project=_PROJECT,
        credentials=credentials,
        location="US",
        client_options={"api_endpoint": "https://bigquery.googleapis.com"},
    )
    original = client._http.request
    pending = set(paths)
    resources = []
    observations = []
    byte_count = 0
    field_count = [0]

    def guarded(method, url, **kwargs):
        nonlocal byte_count
        target = urlparse(url)
        if (
            method != "GET"
            or target.scheme != "https"
            or target.netloc != "bigquery.googleapis.com"
            or target.path not in pending
            or target.fragment
            or parse_qs(target.query) not in ({}, {"prettyPrint": ["false"]})
        ):
            raise QuestionStoreError("schema_http_path_invalid")
        pending.remove(target.path)
        kwargs["allow_redirects"] = False
        kwargs["timeout"] = remaining()
        response = original(method, url, **kwargs)
        remaining()
        if response.status_code != 200:
            raise QuestionStoreError("schema_http_unavailable")
        byte_count += len(response.content)
        if byte_count > 1_000_000:
            raise ValueError("physical_schema_invalid")
        resource = json.loads(response.content, object_pairs_hook=_unique_object)
        if type(resource) is not dict or resource.get("tableReference") != {
            "projectId": _PROJECT,
            "datasetId": _DATASET,
            "tableId": paths[target.path],
        }:
            raise ValueError("physical_schema_identity_invalid")
        schema = resource.get("schema")
        if type(schema) is not dict:
            raise ValueError("physical_schema_invalid")
        _fields(schema.get("fields"), field_count)
        resources.append(resource)
        observations.append({"table_id": paths[target.path], "received_at": current().isoformat()})
        return response

    client._http.request = guarded
    try:
        for name in table_names:
            client.get_table(f"{_PROJECT}.{_DATASET}.{name}", retry=None, timeout=remaining())
        remaining()
        return {
            "table_resources": resources,
            "observations": observations,
            "source_authority": False,
        }
    finally:
        client.close()


def read_question_source_schemas(
    invocation, *, store, scope, runtime_identity, credentials, cluster_build_version, now
):
    """Read eight fixed schemas under the existing invocation, without issuing authority."""
    if cluster_build_version not in ("hybrid_graph_v1", "hybrid_graph_v2", "hybrid_graph_v3"):
        raise ValueError("physical_schema_version_invalid")
    read = _read_question_schema_group(
        invocation,
        store=store,
        scope=scope,
        runtime_identity=runtime_identity,
        credentials=credentials,
        now=now,
        group="source",
    )
    result = validate_source_schemas(
        read["table_resources"], cluster_build_version=cluster_build_version
    )
    result.update(
        observations=read["observations"],
        source_receipt_version_bound=False,
        requested_cluster_build_version=cluster_build_version,
    )
    return result
