"""Current metadata comparison for the six fixed source-copy relations."""

import hashlib
import json

from src.analysis.open_intelligence.general_question_copy_validation import (
    source_copy_schema_requirements,
)
from src.analysis.open_intelligence.general_question_schema import _compare, _encode, _fields
from src.analysis.open_intelligence.general_question_schema_read import _read_question_schema_group


def validate_current_source_copy_schemas(table_resources):
    """Revalidate detached schemas, without treating declarations as observed authority."""
    declarations = source_copy_schema_requirements()
    if type(table_resources) is not list or len(table_resources) != len(declarations):
        raise ValueError("copy_schema_invalid")
    try:
        encoded = _encode(table_resources)
        if len(encoded) > 1_000_000:
            raise ValueError("copy_schema_invalid")
        resources = json.loads(encoded)
    except (TypeError, OverflowError, RecursionError) as error:
        raise ValueError("copy_schema_invalid") from error
    seen, count = set(), [0]
    schemas, declaration_digests = {}, {}
    for resource in resources:
        if type(resource) is not dict or type(resource.get("tableReference")) is not dict:
            raise ValueError("copy_schema_invalid")
        reference = resource["tableReference"]
        if set(reference) != {"projectId", "datasetId", "tableId"} or any(
            type(value) is not str for value in reference.values()
        ):
            raise ValueError("copy_schema_invalid")
        relation = ".".join(reference[field] for field in ("projectId", "datasetId", "tableId"))
        if relation not in declarations or relation in seen:
            raise ValueError("copy_schema_invalid")
        seen.add(relation)
        schema = resource.get("schema")
        if type(schema) is not dict:
            raise ValueError("copy_schema_invalid")
        actual = _fields(schema.get("fields"), count)
        expected = {name: (kind, mode, {}) for name, kind, mode in declarations[relation]}
        _compare(actual, expected)
        if reference["tableId"].startswith("open_intelligence_source_copy_") and set(actual) != set(
            expected
        ):
            raise ValueError("copy_schema_invalid")
        schemas[relation] = hashlib.sha256(_encode(actual)).hexdigest()
        declaration_digests[relation] = hashlib.sha256(_encode(expected)).hexdigest()
    return {
        "table_resources": resources,
        "metadata_digest": hashlib.sha256(encoded).hexdigest(),
        "schema_digests": schemas,
        "declaration_digests": declaration_digests,
        "source_authority": False,
    }


def read_question_copy_schemas(invocation, *, store, scope, runtime_identity, credentials, now):
    """Read the fixed copy group using the existing guarded metadata transport."""
    observed = _read_question_schema_group(
        invocation,
        store=store,
        scope=scope,
        runtime_identity=runtime_identity,
        credentials=credentials,
        now=now,
        group="copy",
    )
    result = validate_current_source_copy_schemas(observed["table_resources"])
    result["observations"] = observed["observations"]
    return result
