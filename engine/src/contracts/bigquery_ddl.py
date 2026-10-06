"""Narrow parser for the accepted Open Intelligence BigQuery table DDL."""

from __future__ import annotations

import re

Field = tuple[str, str, str, str | None, tuple]

_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_PRIMITIVE_TYPES = {
    "BIGNUMERIC",
    "BOOL",
    "BYTES",
    "DATE",
    "DATETIME",
    "FLOAT64",
    "GEOGRAPHY",
    "INT64",
    "JSON",
    "NUMERIC",
    "STRING",
    "TIME",
    "TIMESTAMP",
}
_HEADER = re.compile(
    rf"\ACREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"
    rf"`[A-Za-z0-9][A-Za-z0-9_-]*\.{_IDENTIFIER}\.{_IDENTIFIER}`\s*",
    re.I,
)
_TAIL = re.compile(
    rf"\APARTITION\s+BY\s+"
    rf"(?P<partition>(?:DATE\s*\(\s*{_IDENTIFIER}\s*\)|{_IDENTIFIER}))\s+"
    rf"CLUSTER\s+BY\s+"
    rf"(?P<cluster>{_IDENTIFIER}(?:\s*,\s*{_IDENTIFIER})*)\s+"
    r"OPTIONS\s*\((?P<options>.*)\)\s*;\s*\Z",
    re.I | re.S,
)
_DESCRIPTION = re.compile(
    r"\Adescription\s*=\s*'(?P<value>(?:''|[^'])*)'\Z",
    re.I | re.S,
)
_PARTITION_EXPIRATION = re.compile(
    r"\Apartition_expiration_days\s*=\s*(?P<days>[1-9][0-9]*)\Z",
    re.I,
)


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    angle_depth = 0
    paren_depth = 0
    quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'":
            if quoted and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == "<":
                angle_depth += 1
            elif char == ">":
                angle_depth -= 1
            elif char == "(":
                paren_depth += 1
            elif char == ")":
                paren_depth -= 1
            elif char == "," and angle_depth == 0 and paren_depth == 0:
                parts.append(text[start:index].strip())
                start = index + 1
            if angle_depth < 0 or paren_depth < 0:
                raise ValueError("unbalanced accepted DDL grammar")
        index += 1
    if quoted or angle_depth or paren_depth:
        raise ValueError("unbalanced accepted DDL grammar")
    parts.append(text[start:].strip())
    if any(not part for part in parts):
        raise ValueError("empty accepted DDL element")
    return parts


def _table_body(sql: str, start: int) -> tuple[str, str]:
    if start >= len(sql) or sql[start] != "(":
        raise ValueError("accepted DDL must contain a table field list")
    depth = 0
    quoted = False
    index = start
    while index < len(sql):
        char = sql[index]
        if char == "'":
            if quoted and index + 1 < len(sql) and sql[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return sql[start + 1 : index], sql[index + 1 :]
        index += 1
    raise ValueError("unclosed CREATE TABLE field list")


def _type_and_options(text: str) -> tuple[str, str | None]:
    angle_depth = 0
    quoted = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "'":
            if quoted and index + 1 < len(text) and text[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted:
            if char == "<":
                angle_depth += 1
            elif char == ">":
                angle_depth -= 1
            elif angle_depth == 0 and text[index : index + 7].upper() == "OPTIONS":
                return text[:index].strip(), text[index:].strip()
        index += 1
    return text.strip(), None


def _parse_description_option(options: str | None) -> str | None:
    if options is None:
        return None
    match = re.fullmatch(
        r"OPTIONS\s*\(\s*description\s*=\s*'((?:''|[^'])*)'\s*\)",
        options,
        re.I | re.S,
    )
    if match is None:
        raise ValueError("unsupported column OPTIONS grammar")
    return match.group(1).replace("''", "'")


def _unwrap_generic(type_text: str, prefix: str) -> str | None:
    match = re.fullmatch(rf"{prefix}\s*<(?P<inner>.*)>", type_text, re.I | re.S)
    return match.group("inner").strip() if match else None


def _parse_fields(body: str, *, nested: bool = False) -> tuple[Field, ...]:
    fields: list[Field] = []
    for definition in _split_top_level(body):
        match = re.match(
            rf"\A(?P<name>`{_IDENTIFIER}`|{_IDENTIFIER})\s+(?P<rest>.+)\Z",
            definition,
            re.S,
        )
        if match is None:
            raise ValueError("unsupported field definition")
        name = match.group("name").strip("`")
        type_text, options = _type_and_options(match.group("rest"))
        description = _parse_description_option(options)
        if not nested and description is None:
            raise ValueError("top-level fields require descriptions")
        required = bool(re.search(r"\s+NOT\s+NULL\s*\Z", type_text, re.I))
        if required:
            type_text = re.sub(r"\s+NOT\s+NULL\s*\Z", "", type_text, flags=re.I).strip()

        nested_fields: tuple[Field, ...] = ()
        array_inner = _unwrap_generic(type_text, "ARRAY")
        struct_inner = _unwrap_generic(type_text, "STRUCT")
        if array_inner is not None:
            if required:
                raise ValueError("ARRAY fields cannot use NOT NULL in accepted DDL")
            mode = "REPEATED"
            array_struct = _unwrap_generic(array_inner, "STRUCT")
            if array_struct is not None:
                field_type = "RECORD"
                nested_fields = _parse_fields(array_struct, nested=True)
            else:
                field_type = array_inner.upper()
                if field_type not in _PRIMITIVE_TYPES:
                    raise ValueError("unsupported ARRAY element type")
        elif struct_inner is not None:
            field_type = "RECORD"
            mode = "REQUIRED" if required else "NULLABLE"
            nested_fields = _parse_fields(struct_inner, nested=True)
        else:
            field_type = type_text.upper()
            if field_type not in _PRIMITIVE_TYPES:
                raise ValueError("unsupported field type")
            mode = "REQUIRED" if required else "NULLABLE"
        fields.append((name, field_type, mode, description, nested_fields))
    return tuple(fields)


def _parse_table_options(text: str) -> tuple[int | None, str]:
    expiration: int | None = None
    description: str | None = None
    for option in _split_top_level(text):
        description_match = _DESCRIPTION.fullmatch(option)
        expiration_match = _PARTITION_EXPIRATION.fullmatch(option)
        if description_match:
            if description is not None:
                raise ValueError("duplicate table description")
            description = description_match.group("value").replace("''", "'")
        elif expiration_match:
            if expiration is not None:
                raise ValueError("duplicate partition retention")
            expiration = int(expiration_match.group("days"))
        else:
            raise ValueError("unsupported table OPTIONS grammar")
    if description is None:
        raise ValueError("table description is required")
    return expiration, description


def parse_table_ddl(sql: str) -> dict[str, object]:
    """Parse one fully rendered accepted v2 CREATE TABLE statement."""

    if "{project}" in sql or "{dataset}" in sql:
        raise ValueError("accepted DDL must be fully rendered")
    if "--" in sql or "/*" in sql:
        raise ValueError("comments are outside the accepted DDL grammar")
    header = _HEADER.match(sql.strip())
    if header is None:
        raise ValueError("accepted DDL must use CREATE TABLE IF NOT EXISTS")
    stripped = sql.strip()
    body, tail = _table_body(stripped, header.end())
    tail_match = _TAIL.fullmatch(tail.strip())
    if tail_match is None:
        raise ValueError("unsupported table tail grammar")
    expiration, description = _parse_table_options(tail_match.group("options"))
    partition = re.sub(r"\s+", "", tail_match.group("partition"))
    cluster = tuple(item.strip() for item in tail_match.group("cluster").split(","))
    return {
        "fields": _parse_fields(body),
        "partition": partition,
        "cluster": cluster,
        "expiration": expiration,
        "description": description,
    }
