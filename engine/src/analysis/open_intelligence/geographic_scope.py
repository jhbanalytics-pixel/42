"""Geographic scope decision kernel for one retained observation.

An observation resolves to exactly one scope. ``local`` means behaviour evidenced
in the target market, ``contextual`` means content about the target market that
is not evidenced as local behaviour, ``foreign`` means behaviour evidenced
elsewhere or a known collision, and ``unknown`` means no evidence at all. A
resolved scope cites the record that carries its evidence (a receipt id or a
row id), never a host name and never a collection day, so a copy of one story on
a second host is the same observation and a story collected first is not its
originator. Unknown stays unknown: no default market, no zero confidence.

Methods decide in descending tier order and the first that resolves wins. The
topic collision blocklist comes first because it is the production collision
policy: a row collected under the Nigerian frame that reads as Vietnamese Sapa
tourism is foreign whatever its frame says. A retained geography receipt comes
next, trusted as resolve_wave1_market trusts it: a retained geography with a
method id and a receipt id is authoritative whatever method produced it, and
the kernel does not read the method id's meaning, so separating frame derived
geography from behaviour evidence belongs to the binding slice. The script
marker comes next, above the vendor frame: a script the markets do not write,
or a dense foreign Latin character set, is direct evidence about the content,
while the frame is a statement about the collection query, which is what
leaked in the Sapa case, so foreign content under a cited frame is foreign. The
vendor region carried by its own receipt follows, then the subject marker: the
target country's name or demonym, country only, because city names collide
across countries (Lagos is also a town in Portugal), so a city name alone
resolves nothing. The blocklist is reactive, extended after a leak is seen, so
plain Latin foreign text on a topic it does not list, under a cited frame,
still resolves by the frame; that gap closes by listing the topic, not by the
kernel guessing. The search frame comes last: the market a search was run for,
cited by its own receipt, is contextual evidence and never local behaviour, so
it decides only when nothing above it did, and only contextual.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from src.analysis.open_intelligence.candidates import APPROVED_MARKETS, resolve_wave1_market
from src.utils.geo_blocklist import (
    has_foreign_latin_density,
    has_non_ssa_script,
    row_matches_geo_blocklist,
)

SCOPES: Final = frozenset({"local", "contextual", "foreign", "unknown"})
METHODS: Final = frozenset(
    {
        "blocklist_match",
        "retained_geo_receipt",
        "script_marker",
        "vendor_region",
        "content_marker",
        "search_frame",
        "none",
    }
)
# Ordinal tiers for the deciding method, and the precedence the resolver
# applies: a higher tier decides before a lower one is consulted. They rank
# method strength so a caller can compare two decisions; they are not
# calibrated probabilities. The script marker sits above the vendor region
# because it is direct evidence about the content and the frame is not, so
# foreign content under a cited frame is foreign. One refinement: a foreign
# origin whose content names the target country reports contextual through the
# content marker at the marker's tier, because contextual means about the
# market and not local, and the marker is the weaker of the two records.
METHOD_CONFIDENCE: Final = {
    "blocklist_match": 0.95,
    "retained_geo_receipt": 0.9,
    "script_marker": 0.7,
    "vendor_region": 0.6,
    "content_marker": 0.4,
    "search_frame": 0.3,
}
# Which scopes each deciding method can actually reach, read off the resolver and not off
# the ordering of the tiers: the tier table ranks how strong a deciding method is and says
# nothing about which scope that method can decide. A local scope is reached only through
# ``_origin_scope``, where the resolved origin equals the target market, and only the
# retained geography receipt and the vendor region are ever handed to it. The blocklist
# and the script marker decide foreign only; the content marker and the search frame decide
# contextual only.
# The type enforces this so a record assembled by hand cannot name a method that cannot
# decide local and carry a local scope regardless, which the resolver would never produce
# and every reader downstream treats as behaviour evidenced in the market.
METHOD_SCOPES: Final = {
    "blocklist_match": frozenset({"foreign"}),
    "retained_geo_receipt": frozenset({"local", "foreign"}),
    "script_marker": frozenset({"foreign"}),
    "vendor_region": frozenset({"local", "foreign"}),
    "content_marker": frozenset({"contextual"}),
    "search_frame": frozenset({"contextual"}),
}
# The methods whose evidence reference is the row itself by construction: each of them is
# resolved with ``row`` and never with a receipt id, so a record naming one of them while
# citing anything but its own row did not come out of this kernel for that row.
# This is a fact about the resolver and not a rule ``GeographicScope`` can enforce, which
# is what the wording here used to read as. A record carries the reference it cites and not
# the row it is filed under, so the type has only one of the two values the rule compares
# and cannot check it. Enforcing it belongs to whoever holds both, which is the caller that
# files a record against a row: ``persistence._component_geography`` does it, per row, for
# every batch the composer hands over. The set is declared and exported here because it is
# the kernel's own fact about its own methods, and a caller restating it would be free to
# drift from the resolver.
ROW_CITING_METHODS: Final = frozenset({"blocklist_match", "script_marker", "content_marker"})
# A field this kernel carries is an identifier, a market code, a method name or a topic
# group, never prose, so a bound far above anything honest and far below a payload holds.
# It is applied in bytes as well as in characters because a character count bounds nothing
# a byte store cares about: 256 emoji are 256 characters and 1024 bytes. The value is
# normalised to NFKC first so one value cannot arrive under two spellings, and the
# character categories that let a value look like something it is not are refused outright:
# control characters (Cc), the invisible and format characters (Cf: a zero width space, a
# byte order mark, a word joiner, a soft hyphen, the bidirectional overrides), unpaired
# surrogates (Cs, which do not even encode without surrogatepass), private use and
# unassigned code points (Co, Cn), and the line and paragraph separators (Zl, Zp), which
# are line breaks in fields documented as single lines.
TEXT_LIMIT: Final = 256
TEXT_BYTE_LIMIT: Final = 512
_DECEPTIVE_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# A relative traversal, and an absolute lead, in a value that goes on to build an object
# name. "." and ".." are refused as whole segments wherever they sit.
_TRAVERSAL: Final = re.compile(r"\A[/\\]|(?:\A|[/\\])\.\.?(?:[/\\]|\Z)")
# Country names and demonyms only. City names are deliberately absent because
# they collide (Lagos in Portugal, Kano in Japan), and a colliding name is not
# evidence of anything.
COUNTRY_MARKERS: Final = {
    "za": re.compile(r"\b(?:south afric(?:a|an)|mzansi)\b", re.IGNORECASE),
    "ng": re.compile(r"\b(?:nigeria(?:n)?|naija)\b", re.IGNORECASE),
    "ke": re.compile(r"\b(?:kenya(?:n)?)\b", re.IGNORECASE),
}
# Dotted labels with any last label, so an address is refused with a host.
_HOST_LIKE = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z0-9-]+$", re.IGNORECASE)
# A day in the common spellings, wherever it sits in the text.
_DAY_FORMS = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4}|\d{2}\.\d{2}\.\d{4}")
# A record name carries a letter and a digit or an underscore, so a bare word
# (localhost) and a bare number (20260912) are not names.
_RECORD_LIKE = re.compile(r"^(?=.*[a-z])(?=.*[0-9_]).+$", re.IGNORECASE | re.DOTALL)
_REGION_CODE = re.compile(r"^[a-z]{2}$")


def bounded_text(value: object, message: str) -> str:
    """One identifier shaped field: normalised, stripped, bounded and honestly printable.

    Shared with the callers that guard the same shape of field, so no two of them can
    disagree about what a principal, an identifier, a digest, a day or a rule version may
    contain. The value is normalised and stripped before it is judged, so a field that is
    nothing but whitespace is empty rather than present, and it is the normalised value
    that is returned and used.
    """
    if not isinstance(value, str):
        raise ValueError(message)
    text = unicodedata.normalize("NFKC", value).strip()
    if not text or len(text) > TEXT_LIMIT:
        raise ValueError(message)
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(message) from None
    if len(encoded) > TEXT_BYTE_LIMIT:
        raise ValueError(message)
    if any(unicodedata.category(character) in _DECEPTIVE_CATEGORIES for character in text):
        raise ValueError(message)
    return text


def bounded_name(value: object, message: str) -> str:
    """A bounded field that goes on to build an object name, so it may not leave its prefix."""
    text = bounded_text(value, message)
    if _TRAVERSAL.search(text):
        raise ValueError(message)
    return text


def _text(value: object, field: str) -> str:
    return bounded_text(value, f"{field} must be a nonempty bounded line of plain text")


def _evidence_ref(value: object) -> str:
    """A record name: a receipt id or a row id.

    The resolver only ever cites row_id, geo_receipt_id or vendor_receipt_id, so
    no kernel path can manufacture a host or a day here. This guard is a
    backstop against a caller constructing the record by hand: it refuses
    locations (a slash or a colon), dotted hosts and addresses, days in the
    common spellings wherever they sit, bare numbers and bare words. It does
    not prove that a reference names a real record.
    """
    text = _text(value, "evidence reference")
    if (
        "/" in text
        or ":" in text
        or _HOST_LIKE.match(text)
        or _DAY_FORMS.search(text)
        or not _RECORD_LIKE.match(text)
    ):
        raise ValueError("evidence reference must name a record, not a host or a day")
    return text


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("confidence must be a number")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError("confidence must lie in [0, 1]")
    return number


def _region(value: object, field: str) -> str:
    text = _text(value, field)
    if not _REGION_CODE.match(text):
        raise ValueError(f"{field} must be a lower-case two letter region code")
    return text


@dataclass(frozen=True, slots=True)
class GeographicScope:
    method: str
    evidence_ref: str | None
    scope: str
    confidence: float | None

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError("geographic scope is unsupported")
        if self.method not in METHODS:
            raise ValueError("geographic method is unsupported")
        if self.scope == "unknown":
            if (
                self.method != "none"
                or self.evidence_ref is not None
                or self.confidence is not None
            ):
                raise ValueError("unknown scope carries no method, evidence or confidence")
            return
        if self.method == "none":
            raise ValueError("a resolved scope needs a method")
        # A method that cannot decide a scope cannot carry it, and a method's confidence is
        # its own tier and not a number a caller chooses. Without both, a record naming the
        # content marker with a local scope at any confidence it liked was accepted, and
        # every reader of a resolved record downstream reads local as behaviour evidenced
        # in the market.
        if self.scope not in METHOD_SCOPES[self.method]:
            raise ValueError("geographic method cannot decide that scope")
        object.__setattr__(self, "evidence_ref", _evidence_ref(self.evidence_ref))
        object.__setattr__(self, "confidence", _confidence(self.confidence))
        if self.confidence != METHOD_CONFIDENCE[self.method]:
            raise ValueError("geographic confidence must be the deciding method's own tier")


UNKNOWN: Final = GeographicScope(method="none", evidence_ref=None, scope="unknown", confidence=None)


def _resolved(method: str, evidence_ref: object, scope: str) -> GeographicScope:
    return GeographicScope(
        method=method,
        evidence_ref=evidence_ref,
        scope=scope,
        confidence=METHOD_CONFIDENCE[method],
    )


def _retained_market(
    *,
    vendor_market: object,
    retained_geo_market: object,
    geo_method_id: object,
    geo_receipt_id: object,
) -> str | None:
    """The retained geography, or None when it cannot be cited.

    An approved market goes through resolve_wave1_market unchanged, which already
    returns None without a method id and a receipt id and refuses a vendor that
    disagrees. A geography outside the approved markets follows the same two
    rules here, because that function only speaks the approved set.
    """
    if retained_geo_market is None:
        return None
    if geo_method_id is None or geo_receipt_id is None:
        return None
    if retained_geo_market in APPROVED_MARKETS:
        return resolve_wave1_market(
            vendor_market=vendor_market,
            retained_geo_market=retained_geo_market,
            geo_method_id=geo_method_id,
            geo_receipt_id=geo_receipt_id,
        )
    region = _region(retained_geo_market, "retained geo market")
    _text(geo_method_id, "geo method id")
    _text(geo_receipt_id, "geo receipt id")
    if vendor_market is not None and vendor_market != region:
        raise ValueError("vendor and retained geography disagree")
    return region


def _origin_scope(origin: str, target: str, method: str, receipt: object, row: str, subject: bool):
    if origin == target:
        return _resolved(method, receipt, "local")
    if subject:
        return _resolved("content_marker", row, "contextual")
    return _resolved(method, receipt, "foreign")


def resolve_geographic_scope(
    *,
    market: object,
    row_id: object,
    title: object = None,
    text: object = None,
    url: object = None,
    topic_group: object = None,
    vendor_market: object = None,
    vendor_receipt_id: object = None,
    retained_geo_market: object = None,
    geo_method_id: object = None,
    geo_receipt_id: object = None,
    creator_registration_market: object = None,
    search_frame_market: object = None,
    search_frame_receipt_id: object = None,
) -> GeographicScope:
    """Resolve where one observation's behaviour is evidenced, for ``market``.

    ``creator_registration_market`` is accepted so a caller can pass the row as
    it holds it, and it is validated, but it never decides the scope: where an
    account was registered is not where the behaviour happened.

    ``search_frame_market`` is the market a search was run for, cited by
    ``search_frame_receipt_id``. It says what the query asked, not where the
    behaviour happened, so it is consulted last and decides contextual only.
    """
    if market not in APPROVED_MARKETS:
        raise ValueError("market must be an approved lower-case market")
    target = str(market)
    row = _text(row_id, "row id")
    if creator_registration_market is not None:
        _region(creator_registration_market, "creator registration market")
    fields = {
        key: str(value or "") for key, value in (("title", title), ("text", text), ("url", url))
    }
    content = " ".join(value for value in (fields["title"], fields["text"]) if value)

    if topic_group is not None and row_matches_geo_blocklist(
        fields, _text(topic_group, "topic group")
    ):
        return _resolved("blocklist_match", row, "foreign")
    foreign_content = bool(content) and (
        has_non_ssa_script(content) or has_foreign_latin_density(content) is not None
    )
    # A foreign script cancels the subject reading at the same tier.
    subject = (
        bool(content)
        and not foreign_content
        and COUNTRY_MARKERS[target].search(content) is not None
    )

    origin = _retained_market(
        vendor_market=vendor_market,
        retained_geo_market=retained_geo_market,
        geo_method_id=geo_method_id,
        geo_receipt_id=geo_receipt_id,
    )
    if origin is not None:
        return _origin_scope(origin, target, "retained_geo_receipt", geo_receipt_id, row, subject)
    if foreign_content:
        return _resolved("script_marker", row, "foreign")
    if vendor_market is not None and vendor_receipt_id is not None:
        region = _region(vendor_market, "vendor market")
        return _origin_scope(region, target, "vendor_region", vendor_receipt_id, row, subject)
    if subject:
        return _resolved("content_marker", row, "contextual")
    if search_frame_market is not None and search_frame_receipt_id is not None:
        frame = _region(search_frame_market, "search frame market")
        receipt = _text(search_frame_receipt_id, "search frame receipt id")
        if frame == target:
            return _resolved("search_frame", receipt, "contextual")
    return UNKNOWN


def local_observation_refs(records: Iterable[object]) -> tuple[str, ...]:
    """Distinct evidence references behind the local records, sorted.

    An observation is identified by the record it cites. Two rows citing one
    receipt are one observation whatever their hosts; two people citing their own
    receipts are two, however similar their words.
    """
    refs: set[str] = set()
    for record in records:
        if not isinstance(record, GeographicScope):
            raise TypeError("local observation refs take GeographicScope records")
        if record.scope == "local" and record.evidence_ref is not None:
            refs.add(record.evidence_ref)
    return tuple(sorted(refs))


__all__ = [
    "COUNTRY_MARKERS",
    "METHODS",
    "METHOD_CONFIDENCE",
    "METHOD_SCOPES",
    "ROW_CITING_METHODS",
    "SCOPES",
    "TEXT_BYTE_LIMIT",
    "TEXT_LIMIT",
    "UNKNOWN",
    "GeographicScope",
    "bounded_name",
    "bounded_text",
    "local_observation_refs",
    "resolve_geographic_scope",
]
