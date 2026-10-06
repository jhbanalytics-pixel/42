"""Provenance guard for the PULSE v2 mailer.

The mailer's core rule is keep the art, bind the data: no visible element
renders a value the pipeline did not produce. ``assert_sourced`` is the
checkpoint a renderer calls before it emits a value into the HTML. If the
backing field is missing or empty, the renderer must omit the element, never
invent a prettier value, so calling this on an empty value raises and the
caller's own guard (an ``if value:`` check) is what keeps the element out.

Renderers wrap optional elements in ``if field:`` and only reach
``assert_sourced`` on the truthy path, so a raise here means a renderer tried
to print something unsourced and is a real bug, not graceful degradation.
"""


class UnsourcedValueError(ValueError):
    """A renderer tried to emit a value with no backing pipeline field."""


def assert_sourced(value: object, field_name: str) -> object:
    """Return ``value`` if it is a real, non-empty field; raise otherwise.

    A value is sourced when it is not None and, for strings, not blank after
    stripping. Numbers (including 0) and non-empty containers count as sourced
    because 0 mentions or an empty-but-present list is still real data the
    caller chose to render.
    """
    if value is None:
        raise UnsourcedValueError(f"{field_name} has no backing field, omit the element")
    if isinstance(value, str) and not value.strip():
        raise UnsourcedValueError(f"{field_name} is blank, omit the element")
    return value
