"""Whole-programme closure kernel and its validating wrapper (E04).

The requirement universe below is copied from the acceptance crosswalk in the
master staging completion plan: the original ledger families plus BSA01 to
BSA12. The kernel reports every required row that lacks current accepted
evidence or an explicit scope amendment. The wrapper refuses incomplete,
duplicated or unknown requirement sets and resolves every proof and decision
through injected immutable records, so a string in an evidence row is never
proof by itself.

A resolved record is not trusted because it is a dictionary. It counts only
when it names the identifier it was resolved under, names a trust anchor the
caller supplied, and carries the digest that anchor's key makes over its own
canonical body. Anything a lane can write for itself fails that check.

Every record is also read against a stated certification instant. A record
must carry a timezone aware validity window, must not be dated after that
instant and must not have lapsed before it, so evidence that was true once
does not certify the programme today.

A proof names the candidate it was recorded against, and it reaches that
candidate and no other. Carrying it onto a later candidate needs a compatibility
bridge that is itself an authenticated, current record of its own type naming
that proof and both candidates, so neither listing the final candidate among a
historical record's candidates nor declaring the final candidate as that
record's own origin is a bridge. An exit gate record and an amendment decision
are held to the same candidate by a bound rather than by a bridge: each names
the one candidate it was issued against and nothing else. Accepting an exit and
amending a scope are single acts about a single candidate, never carried
forward, and a signature made on an earlier candidate that pre-named the final
one used to close the gate because the list it appeared in was unbounded.

Twelve BSA rows, nine named rows and the five programme exit gates close only
on a real person's signature. For those, an authenticated record must also carry a
sign off by a named accepted issuer acting as a human, dated inside the record's
own window and at or before the certification instant. Such a record must also
account for its own production positively: it may carry only the fields this
kernel defines, the evidence its signer attaches and the further evidence fields
that signer declares by name inside the sealed body; its signature block holds
only the parts of a signature, which include the role it was made in and the key
it was made with, because the documents define these gates by role; and it must
carry a provenance block that says a person made it rather than merely fail to
say a machine did. That block is required, not optional, since a positive shape
nothing has to carry is no control at all. A provenance chain is read as a
history whose last step is the claim, so a runner that gathered the evidence and
a person who then read and signed it is an accepted shape. Machine produced
material the signer attaches is read as evidence and not as a claim about who
signed, so a person may sign off on a CI report and attach it.

The second half of the rule is that one person is not several assessors. Every
record states its own type, so one seal never answers as proof, bridge, decision
and exit gate at once. A proof carries at most one row of the human roster and a
bridge attestation carries one roster row's proof, so a single signature cannot
close the roster by either path.

Who is one person is not a fact about strings and this module does not decide
it. The caller supplies identity_roster, which maps each accepted issuer to a
stable identity, and every signer is counted by identity, never by name. Two
entries that resolve to one identity are one signer wherever a count or a
distinctness rule is applied, including the reviewer population each exit gate
is held to, and the result reports identities rather than names. A signature
naming an issuer the roster does not map is refused rather than guessed at, and a
roster that leaves an accepted issuer unmapped is refused before anything is
adjudicated. That roster is a required input with no default, exactly as the
minimum signer count is, because a name resting on a rule over strings is what
the fourth review closed the programme with: five spellings of one name, told
apart only by invisible characters in categories the normalisation does not
touch. Name normalisation stays, in front of the roster lookup, as the
convenience it is: it catches case, spacing and the two invisible categories it
names, so an honest typing variation reaches the same roster row. It is not the
control, and _normalised_name says so.

The signing actor is recorded per roster row and per exit gate seat, and the
caller states how many distinct signers the closure needs. That number is a
coverage rule and not a tally: it is met only when that many identities can each
be credited a row or a gate seat that no other one of them is credited with.
Each exit gate is held to its own reviewer population besides, and a seat of one
exit and a seat of another may be credited to the same identity, because the
documents name a population per exit and name no rule against that.

The records that carry a person's signature are sealed by their own anchors,
separate from the anchors that seal routine machine evidence. Two keys the HMAC
construction reduces to the same key are one anchor, whatever they spell. What
that separation prevents is one thing: the routine anchor cannot mint a human
sign off, so the process that seals CI output cannot close a row or a gate the
programme reserves for a person with the key it already holds. It does not keep
a machine out of the human rows. A human trust anchor is a key and not a person,
and nothing here binds a key to the identity a signature names, so this module
cannot tell a machine holding a human anchor key from the person that key was
issued to. Any single holder of any one human anchor key mints every approval the
programme reserves for a person: the identity roster, the reviewer populations
the exits are held to and the signer list this result reports are defeated
together, by one key, one process and whatever names that holder cares to
invent. The key identifier and the signature are carried in the signature block
and neither is ever read. Binding a key to an identity is not done in this module and no test
here claims it is.

Three of the names a signature block may carry are carried and never read. The
role is not checked against anything, because no roster of roles is an input here
and the reviewer populations the documents name are counted rather than named.
The key identifier is not read either, and reading it is the whole of the binding
this module does not have, which belongs with the wiring that hands the anchors
over. The signature is an opaque string beside the seal: authentication is the
digest this module recomputes with the anchor's own key, and the signature field
adds nothing to it. Widening the allowed field set so a record may carry these
three names without being refused for carrying them is all that was done.

No single number for minimum_distinct_signers is written down, so there is no
default and the decision belongs to the caller. What is written down is a
reviewer population per exit. The decisions record names three strategist roles
for one exit, two blind claim reviewers for another, cluster reviewers for a
third and the client reviewer for a fourth; E04 comes from its own exit step,
which names one person. Five reviewer populations is not five people, and
counting the exits in place of the people they name was a mis-derivation of the
floor. PROGRAMME_EXIT_GATE_SIGNERS carries what each exit needs, read off those
lines, and MINIMUM_DISTINCT_SIGNER_FLOOR is their sum. A bare plural with no
numeral is read as two, the least a plural admits, because reading a larger
number off a line that does not write one would be inventing it. Each exit is
held to that many distinct identities across the records offered for it, so an
exit now carries as many signatures as it needs reviewers; it used to accept one
signature and could not express a population of three at all. That per exit
rule is unconditional. It is not read off the caller's number, and it holds at a
configured 1 as it holds at a configured 30. What the caller's number is
validated against is only that it is at least one, at most the count of signable
rows and gate seats, and at most the number of distinct identities the accepted
issuers resolve to.

MINIMUM_DISTINCT_SIGNER_FLOOR is the sum of those populations, which is the
number of gate seats the five exits name between them. It is not a floor on how
many different people a closure needs. The documents name a reviewer population
per exit and name nothing against one person holding a seat on two exits, so
each exit is held to its own count and nothing is imposed across the exits: an
identity credited a seat of one exit may be credited a seat of another, and the
five exits can be filled by fewer people than they have seats between them. A
strict reading that refused two exits one signer was layered on top of these
lines, and it is not in them.

A configured value below that number is not refused. It is a bound the exits are
likely to exceed rather than an incoherent one, and refusing a caller for asking
less than they will get would be the wrong way round. So that the arithmetic is
the caller's to see rather than theirs to discover, the result reports both
numbers: minimum_distinct_signers as it was stated, and
effective_minimum_distinct_signers as the larger of that number and the seat
total. The second is a count of seats, and a reader should not take it for the
number of people a closure was held to.

One thing a reader should not have to discover for themselves.
certify_requirements has no caller anywhere in this repository: nothing runs the
validated path yet. The kernel shape check that used to sit beside it under the
name unmet_requirements is no longer exported. It was the shape anything
downstream would reach for and it read a row on the truthiness of its proof ids,
so a list that was empty to iteration and truthy to bool() closed all 84 rows
with nothing resolved. It is now _unmet_requirements: private, reading the rows
it is given through _plain and through the same shape rules the certifier
applies, and used by certify_requirements to decide which rows claim anything at
all. It says which rows do not even claim to be met. It adjudicates nothing, and
a row it passes over has been read for shape and for nothing else.

The certification instant is bound against a trusted clock the caller injects
and refused when it sits further from that clock than AS_OF_TOLERANCE. That
control is there for operator error and host clock skew: an instant typed by
hand, or gathered on a host whose clock has drifted. It is not a control
against an adversary, because the clock is injected by the same caller that
states the instant it checks: a clock derived from that instant agrees with it
by construction, and the module cannot see the difference.
"""

import hashlib
import hmac
import json
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import datetime, timedelta

REQUIREMENT_IDS = (
    "A01",
    "A02",
    "A03",
    "A04",
    "A05",
    "A06",
    "A07",
    "A08",
    "BSA01",
    "BSA02",
    "BSA03",
    "BSA04",
    "BSA05",
    "BSA06",
    "BSA07",
    "BSA08",
    "BSA09",
    "BSA10",
    "BSA11",
    "BSA12",
    "D01",
    "F01",
    "F02",
    "F03",
    "F04",
    "F05",
    "F06",
    "F07",
    "F08",
    "F09",
    "F10",
    "F11",
    "F12",
    "G01",
    "G02",
    "G03",
    "G04",
    "G05",
    "G06",
    "G07",
    "G08",
    "G09",
    "G10",
    "G11",
    "G12",
    "G13",
    "G14",
    "P001",
    "P002",
    "P003",
    "P004",
    "P101",
    "P102",
    "P103",
    "P104",
    "P105",
    "P106",
    "P201",
    "P202",
    "P203",
    "P204",
    "P205",
    "P300",
    "P301",
    "P302",
    "P303",
    "P304",
    "P305",
    "P306",
    "P401",
    "P402",
    "P403",
    "P404",
    "P405",
    "P501",
    "P502",
    "P503",
    "P504",
    "P505",
    "X01",
    "X02",
    "X03",
    "X04",
    "X05",
)

# The rows that only a person may close: BSA01 to BSA12 and the nine named rows
# the programme records as human decisions.
#
# The crosswalk against the plan documents sources 19 of these 21 rows from the
# acceptance ledger's responsibility column. F11 and F12 are not sourced there:
# their responsibility column reads "Backend" and names no person. They stay on
# this roster all the same, because they are on the owner's own stated roster
# and striking them would take protection away from two rows that have it. The
# looser criterion that would source them, the requirement text naming the
# owner, also picks up P302 and P502, which this roster does not carry, so no
# single documented criterion yields these 21. The gap is recorded, not closed.
HUMAN_SIGNOFF_REQUIREMENTS = frozenset(
    {f"BSA{index:02d}" for index in range(1, 13)}
    | {"F05", "F11", "F12", "G07", "G09", "P106", "P203", "P204", "P306"}
)

# The task exits that need a person's own signature before the programme can
# close. The decisions record of 13 September names E01, E02, L01 and B04 as
# human gates and names their reviewer roles; E04 comes from its own exit step
# in the evaluation and certification package, the owner's explicit candidate
# acceptance. Nothing in the documents names E02 and E04 as the pair this tuple
# once held, so E01, L01 and B04 were closable on machine evidence alone.
PROGRAMME_EXIT_GATES = ("E01", "E02", "L01", "B04", "E04")

# How many distinct people each exit needs, read off the lines the crosswalk
# quotes. The decisions record's line reads "three strategist roles for E02, two
# blind claim reviewers for E01, cluster reviewers for L01 closed week review,
# and the client reviewer for B04". A numeral names its own count. "cluster
# reviewers" is a bare plural with no numeral, so it is two: a plural names more
# than one, two is the least that admits, and reading a larger number off a line
# that does not write one would be inventing it. "the client reviewer" is
# singular and is one. E04 is not on that line; its own exit step requires one
# named person's explicit acceptance, which is one.
PROGRAMME_EXIT_GATE_SIGNERS = {"E01": 2, "E02": 3, "L01": 2, "B04": 1, "E04": 1}

# The number the documents write down beside minimum_distinct_signers. It used
# to be the number of exits, on the reading that five exits with distinct
# reviewer populations is five distinct people. Five populations is not five
# people: the line above names three for one exit and two for another before it
# names a single reviewer for a third. This is the sum of the populations, which
# is the number of gate seats the five exits name between them, and each exit is
# held to its own count. It is not a count of how many different people a
# closure needs, because one identity may hold a seat on more than one exit. It
# is reported beside the stated number in every result.
MINIMUM_DISTINCT_SIGNER_FLOOR = sum(PROGRAMME_EXIT_GATE_SIGNERS.values())

# The rows and gates whose source names the person who has to sign them, rather
# than a role or a population. The acceptance ledger's responsibility column for
# G09 reads "Backend + frontend + Albert", and E04's own exit step requires
# Albert's explicit candidate acceptance. Everywhere else the documents name a
# role or a reviewer population and any accepted issuer answers for it, which is
# why a name here is the exception and not the rule.
#
# The name is the one the documents write, in the form every other name in this
# module is read in, and it is only ever compared. It is resolved to an identity
# through the caller's roster, exactly as a signature's name is, and matched
# against the identity that signed. Nothing writes it into a record, an approval
# or a result, and a closure that does not accept that name closes neither the
# row nor the gate rather than passing them over.
SOURCE_NAMED_SIGNERS = {"G09": "albert", "E04": "albert"}

# What a record says it is. One seal answers as one kind of record and no other.
PROOF_RECORD_TYPE = "proof"
BRIDGE_RECORD_TYPE = "bridge_attestation"
DECISION_RECORD_TYPE = "decision"
EXIT_GATE_RECORD_TYPE = "exit_gate"

# The fields by which a record declares who or what made it. The surface a record
# speaks about its own production with is the whole record apart from the
# material a signature is about, and wherever one of these fields appears on that
# surface, at any depth, it must say "human" and nothing else. The material a
# signature is about is what a record attaches under the two names this kernel
# defines for attachments below, and what it attaches under a name it declares
# for itself. The first is read as evidence and never as a claim about who
# signed, which is what lets a person sign off on a CI report and attach it. The
# second is held to a stricter rule than this one, stated with the declaration
# below: a declared evidence field may not declare an actor at all, at any depth,
# so a record cannot relabel its own provenance as an attachment. The collector
# used to gather the record's top level, its signature block and its provenance
# block and nothing else, so a machine declaration nested under any other field
# was never looked at. A closed list of non human words was the control before
# that and it was decorative: it missed "ai", it missed "machine" and it never
# looked below the top two levels. The gate is the positive check.
ACTOR_DECLARING_FIELDS = frozenset(
    {"actor_kind", "origin_kind", "produced_by", "producer", "signer_kind"}
)
HUMAN_ACTOR_KIND = "human"

# Where a record speaks about its own production, and where it does not. The
# walk used to read the whole body, so a person signing off on a CI report and
# attaching it was refused by the report's own producer field, and a signed PDF
# was refused by its metadata. That is the ordinary shape of a human sign off.
# The record's own top level, its signature block and its provenance block are
# claims about who signed. Everything else is material, read as evidence.
SIGNATURE_FIELD = "human_signoff"
PROVENANCE_FIELD = "provenance"

# How a record names its own attached material. A signer attaches what they
# attach under the name it has, so two field names cannot be the whole list. A
# record declares the rest itself, inside the body its anchor sealed, and a name
# it declares is read as evidence and never as a claim about who signed.
EVIDENCE_DECLARATION_FIELD = "evidence_fields"

# The fields a record that carries a person's signature may hold. Naming what a
# record may say is the positive check; naming what it may not say was not one,
# because a record declaring "generated_by" met no list and passed. A record
# whose top level this kernel cannot account for is not one it can adjudicate.
KERNEL_RECORD_FIELDS = (
    frozenset(
        {
            "record_id",
            "record_type",
            "authority",
            "digest",
            "requirements",
            "requirement",
            "candidates",
            "origin_candidate",
            "compatibility_bridge",
            "bridges",
            "from_candidate",
            "to_candidate",
            "recorded_at",
            "signed_at",
            "decided_at",
            "expires_at",
            "revoked_at",
            "gate",
            "issuer",
            "scope_delta",
            "accepted",
            SIGNATURE_FIELD,
            PROVENANCE_FIELD,
            EVIDENCE_DECLARATION_FIELD,
        }
    )
    | ACTOR_DECLARING_FIELDS
)

# What a signer attaches: the machine produced material the signature is about.
# These two names are this kernel's own, so a record carries them without
# declaring them, but what they carry is held to the same shape as the material
# under a name the signer declares: a non empty reference, a list or a mapping.
# The exemption gained an attacker nothing, since neither name is read as a claim
# about who signed, but a rule this module states about the material a signature
# is about holds wherever that material is and not only where the signer chose
# the name.
DECLARED_EVIDENCE_FIELDS = frozenset({"evidence_artifacts", "attachments"})

# The parts of a signature. Three of these names, the role, the key identifier
# and the signature, are carried and never read, and admitting them is a pure
# widening of a set whose only use is to refuse a field this kernel cannot
# account for. A signature that named the role it was made in, the key it was
# made with or the signature itself used to be refused for saying so, and now it
# is not; nothing in this module then reads any of the three. The role has
# nothing to be checked against, because no roster of roles is an input here.
# Reading the key identifier is the binding between a key and an identity that
# this module does not have, and that binding belongs with the wiring that hands
# the anchors over. The signature is an opaque string beside the seal, because
# authentication here is the digest recomputed with the anchor's own key. An
# earlier commit called admitting the third of them room the block had lacked
# for the signature; there is room and there is no reader, and that is what this
# comment is for. This set is closed and pinned by name in the tests, because a
# set that is only pinned by the strings some test happens to use is a set that
# can be widened by one name and nobody notices.
SIGNATURE_FIELDS = frozenset(
    {
        "actor",
        "actor_kind",
        "signed_at",
        "statement",
        "role",
        "key_id",
        "signature",
    }
)

# Where a provenance block records the steps that produced the record. The
# earlier steps are the history a record is allowed to have: a runner gathered
# the evidence and a person then read and signed it. Only the last step is a
# claim about who produced the record that is being adjudicated.
PROVENANCE_CHAIN_FIELD = "chain"

# How far the stated certification instant may sit from the caller's trusted
# clock. Five minutes is the allowance the rest of the industry gives signature
# freshness for host clock skew and for the seconds a closure run spends
# gathering records. It is far shorter than the shortest evidence window here,
# so it cannot be used to reach evidence that has lapsed or has not opened.
AS_OF_TOLERANCE = timedelta(minutes=5)

# Scripts that ordinarily appear together inside one written name, folded to one
# family so the mixed script rule below does not refuse an honest name. A
# Japanese name written in kanji and kana is one name in one writing system.
SCRIPT_FAMILIES = {
    "CJK": "JAPANESE",
    "HIRAGANA": "JAPANESE",
    "KATAKANA": "JAPANESE",
}

Resolver = Callable[[str], dict | None]


def _scripts_of(name: str) -> set[str]:
    """Return the scripts the letters of a name are written in.

    There is no script table in the standard library, so each letter's script is
    read from the first word of its own Unicode name: LATIN SMALL LETTER A is
    Latin and CYRILLIC SMALL LETTER A is Cyrillic. Digits, spaces, punctuation
    and combining marks carry no script and are passed over.
    """
    scripts = set()
    for character in name:
        if not character.isalpha():
            continue
        try:
            script = unicodedata.name(character).split()[0]
        except ValueError:
            script = "UNNAMED"
        scripts.add(SCRIPT_FAMILIES.get(script, script))
    return scripts


def _normalised_name(value) -> str | None:
    """Return the one form of a signer's name, or None when it is not a name.

    This is a convenience and not the control. It sits in front of the identity
    roster so that an honest typing variation of a name reaches the roster row
    it belongs to, and so that the obvious duplicates of one spelling are one
    key. Identity is decided by the roster the caller supplies, and nothing that
    matters is decided by what this function folds together.

    What this catches: letter case; leading, trailing and repeated internal
    whitespace, including tabs and other spellings of a space; the characters of
    the format category, such as the zero width space and the soft hyphen, which
    are dropped; the characters of the control category, which become a space;
    canonical and compatibility differences, so NFD and NFC spellings of one
    name are one name; and a single name whose letters are drawn from more than
    one script, which is how a homoglyph such as U+0430, the Cyrillic small
    letter a, is smuggled into an otherwise Latin name.

    What this does not catch, and must not be claimed to catch: invisible
    characters outside those two categories, which is most of them. A combining
    grapheme joiner, a variation selector and a blank braille pattern are a
    nonspacing mark, a nonspacing mark and a symbol, not the control and not the
    format categories; each renders as nothing, each survives this function, and
    none of them is a letter, so the mixed script rule never sees them either.
    Five such spellings of one name closed a five signer programme. Nor does this
    catch two different people who spell their names the same way; one person
    under two genuinely different names, a nickname, an initial or a
    transliteration; a homoglyph inside a single script, such as the digit one
    for a lower case L, or "rn" for "m"; or a name written wholly in another
    script that renders like a Latin one. None of that can be told apart by a
    rule over strings, which is why the caller states the identities.
    """
    if not isinstance(value, str):
        return None
    # A format character such as the zero width space or the soft hyphen carries
    # no width and is dropped outright. A control character such as a tab or a
    # line break does separate what it sits between, so it becomes a space and
    # the collapse below decides what that is worth.
    visible = "".join(
        " " if unicodedata.category(character) == "Cc" else character
        for character in value
        if unicodedata.category(character) != "Cf"
    )
    folded = unicodedata.normalize(
        "NFKC", unicodedata.normalize("NFKC", visible).casefold()
    )
    collapsed = " ".join(folded.split())
    if not collapsed:
        return None
    if len(_scripts_of(collapsed)) > 1:
        return None
    return collapsed


def requirement_universe() -> frozenset[str]:
    """Return the complete set of 84 requirement identifiers."""
    return frozenset(REQUIREMENT_IDS)


def _row_claims_to_be_met(row: dict) -> bool:
    """Whether a row carries a claim this kernel could go and adjudicate.

    A claim is a state this kernel knows and the one thing that state has to
    name: a list of proof ids it can read, or a decision id. Reading the proof
    ids through bool() was not that, and a list that answers empty to iteration
    and true to bool() met every row of the programme with nothing resolved.
    """
    state = row.get("state")
    if not isinstance(state, str):
        return False
    if state == "achieved":
        proof_ids = row.get("proof_ids")
        return (
            isinstance(proof_ids, list)
            and len(proof_ids) > 0
            and not _proof_ids_malformed(proof_ids)
        )
    if state == "amended":
        decision_id = row.get("user_decision_id")
        return isinstance(decision_id, str) and bool(decision_id)
    return False


def _unmet_requirements(required: set[str], evidence: dict[str, dict]) -> list[str]:
    """Name the required rows that do not even claim to be met.

    This is a shape check over evidence rows and it is not an adjudication.
    Nothing resolves here and no record is read, so a row this does not name is
    a row that claims something, never a row that proved anything. It was
    exported as unmet_requirements, where it was the shape anything downstream
    would reach for, and it is private now for that reason. The rows are read
    once through _plain and the claim is read through the same rules the
    certifier applies, which is what certify_requirements uses this for: to know
    which rows it has to go and adjudicate.
    """
    rows = _plain(evidence) if isinstance(evidence, Mapping) else {}
    missing = []
    for requirement in sorted(required):
        row = rows.get(requirement)
        if not isinstance(row, dict) or not _row_claims_to_be_met(row):
            missing.append(requirement)
    return missing


def _anchor_key(key) -> bytes:
    if isinstance(key, bytes):
        return key
    if isinstance(key, str):
        return key.encode("utf-8")
    raise ValueError("trust_anchor_key_invalid")


def _hmac_key_identity(key: bytes) -> bytes:
    """Return the key block the HMAC construction actually uses for this key.

    Byte inequality is not key inequality. HMAC replaces a key longer than the
    hash block with its own digest and zero pads anything shorter to the block,
    so a key and that key with trailing zero bytes are one key, and so are a
    long key and its own digest. Comparing raw bytes let the routine ledger key
    stand as a second anchor under a second name and mint every sign off the
    separation was there to keep it out of.
    """
    block_size = hashlib.sha256().block_size
    if len(key) > block_size:
        key = hashlib.sha256(key).digest()
    return key.ljust(block_size, b"\x00")


def _plain(value):
    """Return a plain copy of a record, read once through the mapping itself.

    A Mapping subclass can answer one body to items() and another to get(). The
    digest was taken over the first and every check read the second, so one seal
    could cover a narrow body while a wider one was adjudicated. Reading the
    record once, here, and sealing and adjudicating that copy closes it.
    """
    if isinstance(value, Mapping):
        return {name: _plain(item) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def record_digest(record: dict, key) -> str:
    """Return the anchor digest over a record's canonical body without its digest."""
    body = {name: value for name, value in record.items() if name != "digest"}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hmac.new(
        _anchor_key(key), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _authentication_rejection(
    record: dict, record_id: str, trust_anchors: Mapping[str, bytes]
) -> str | None:
    """Reject a record that is not bound to this id and sealed by an anchor."""
    if "record_id" not in record or record.get("record_id") != record_id:
        return "record_id_mismatch"
    authority = record.get("authority")
    if not isinstance(authority, str) or authority not in trust_anchors:
        return "authority_unknown"
    digest = record.get("digest")
    if not isinstance(digest, str) or not digest:
        return "digest_invalid"
    try:
        expected = record_digest(record, trust_anchors[authority])
    except (TypeError, ValueError):
        return "digest_invalid"
    if not hmac.compare_digest(digest, expected):
        return "digest_invalid"
    return None


def _authenticate(
    record: Mapping, record_id: str, trust_anchors: Mapping[str, bytes]
) -> tuple[str | None, dict]:
    """Read the record once into a plain body, then authenticate that body."""
    body = _plain(record)
    return _authentication_rejection(body, record_id, trust_anchors), body


def _checked_identities(
    identity_roster, issuers: frozenset[str]
) -> dict[str, str]:
    """Return the identity behind each accepted issuer, as the caller states it.

    The roster is the control and the caller holds it, because who counts as one
    person is not something this module may decide and not something a rule over
    strings can tell. Each name is read through the one normalisation, so a
    roster may spell a person's name more than one way, and two spellings that
    are one name may not carry two identities. An issuer this roster does not
    map is refused here, before anything is adjudicated, rather than guessed at
    later.

    An identity is an opaque token. It is required to be a non empty string and
    it is compared exactly: this module does not normalise it, fold it or invent
    one. A caller whose roster spells one identity two ways has two identities
    here, and that is the caller's roster to correct.

    A roster wider than the accepted issuers is not refused. An organisation
    holds one roster of its people and a closure names the ones it accepts out
    of it; the entries this returns are only the accepted issuers, so a name the
    roster maps and the closure does not accept still cannot sign.
    """
    if not isinstance(identity_roster, Mapping):
        raise ValueError("identity_roster_required")
    roster = _plain(identity_roster)
    if not roster:
        raise ValueError("identity_roster_required")
    stated: dict[str, str] = {}
    for name, identity in roster.items():
        key = _normalised_name(name)
        if key is None:
            raise ValueError("identity_roster_name_invalid")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("identity_roster_identity_invalid")
        if key in stated and stated[key] != identity:
            raise ValueError("identity_roster_inconsistent")
        stated[key] = identity
    if issuers - set(stated):
        raise ValueError("identity_roster_incomplete")
    return {name: stated[name] for name in issuers}


def _checked_trust_anchors(
    trust_anchors, *, label: str = "trust_anchors"
) -> dict[str, bytes]:
    if not isinstance(trust_anchors, Mapping) or not trust_anchors:
        raise ValueError(f"{label}_required")
    checked: dict[str, bytes] = {}
    for name, key in trust_anchors.items():
        if not isinstance(name, str) or not name:
            raise ValueError("trust_anchor_name_invalid")
        anchor_key = _anchor_key(key)
        if not anchor_key:
            raise ValueError("trust_anchor_key_invalid")
        checked[name] = anchor_key
    return checked


def _instant(value) -> datetime | None:
    """Parse a timezone aware ISO-8601 instant; anything else is not a time."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed


def _temporal_rejection(
    record: dict, as_of: datetime, *, recorded_field: str, future_reason: str
) -> str | None:
    """Reject a record with no readable window, a future date or a lapsed one."""
    recorded = _instant(record.get(recorded_field))
    expires = _instant(record.get("expires_at"))
    if recorded is None or expires is None:
        return "temporal_missing"
    if recorded > as_of:
        return future_reason
    if expires <= recorded:
        return "temporal_window_invalid"
    if expires <= as_of:
        return "stale"
    return None


def _bridge_rejection(
    record: dict,
    proof_id: str,
    requirement: str,
    origin: str,
    candidate: str,
    identities: Mapping[str, str],
    proof_resolver: Resolver,
    trust_anchors: Mapping[str, bytes],
    human_trust_anchors: Mapping[str, bytes],
    as_of: datetime,
) -> str | None:
    """Reject historical evidence with no demonstrated bridge to this candidate."""
    bridge = record.get("compatibility_bridge")
    if not isinstance(bridge, dict):
        return "bridge_missing"
    attestation_id = bridge.get("attestation_id")
    if not isinstance(attestation_id, str) or not attestation_id:
        return "bridge_missing"
    if (
        bridge.get("from_candidate") != origin
        or bridge.get("to_candidate") != candidate
    ):
        return "bridge_mismatch"
    attestation = proof_resolver(attestation_id)
    if not isinstance(attestation, Mapping):
        return "bridge_unresolved"
    on_the_roster = requirement in HUMAN_SIGNOFF_REQUIREMENTS
    unauthenticated, attestation = _authenticate(
        attestation,
        attestation_id,
        human_trust_anchors if on_the_roster else trust_anchors,
    )
    if unauthenticated is not None:
        return f"bridge_{unauthenticated}"
    if attestation.get("record_type") != BRIDGE_RECORD_TYPE:
        return "bridge_record_type_mismatch"
    if (
        attestation.get("from_candidate") != origin
        or attestation.get("to_candidate") != candidate
    ):
        return "bridge_mismatch"
    carried = attestation.get("bridges")
    if proof_id not in _string_set(carried):
        return "bridge_mismatch"
    if on_the_roster and carried != [proof_id]:
        # The roster scope rule lived only over a proof's requirements, and an
        # attestation has none: it has an unbounded list of proof ids. One
        # attestation naming the whole roster is one person standing in for
        # every assessor who signed on the candidate it leaves behind.
        return "bridge_human_signoff_scope_too_broad"
    if attestation.get("revoked_at") is not None:
        return "bridge_revoked"
    outdated = _temporal_rejection(
        attestation,
        as_of,
        recorded_field="recorded_at",
        future_reason="recorded_in_future",
    )
    if outdated is not None:
        return f"bridge_{outdated}"
    if on_the_roster:
        # A machine cannot carry a person's sign off to a candidate that person
        # never saw. The attestation that moves a roster row needs its own.
        unsigned, _ = _human_signoff_rejection(
            attestation,
            identities,
            as_of,
            _instant(attestation.get("recorded_at")),
        )
        if unsigned is not None:
            return f"bridge_{unsigned}"
    return None


def _declared_kind(value) -> str | None:
    """Return the actor kind a field declares, or None when it declares no word."""
    if not isinstance(value, str):
        return None
    return value.strip().casefold()


def _attached_material_names(record: Mapping) -> set[str]:
    """Return the names under which this record carries material, not claims.

    The two names this kernel defines for attachments, and every name the record
    declares as evidence of its own. The declaration is read as it is written
    rather than as it must be. A declaration this kernel goes on to refuse still
    names the fields the record meant as attachments, and refusing it belongs to
    the positive account check below and not here. Reading it any other way
    would answer a record that laundered its provenance under a name it declared
    with a refusal that names the wrong thing.
    """
    names = set(DECLARED_EVIDENCE_FIELDS)
    declared = record.get(EVIDENCE_DECLARATION_FIELD)
    if isinstance(declared, (list, tuple)):
        names.update(name for name in declared if isinstance(name, str) and name)
    return names


def _provenance_surface(record: Mapping) -> dict:
    """Return the parts of a record that speak about who produced and signed it.

    That is the whole record apart from the material a signature is about. An
    attached CI report or a signed PDF is material the claim is about, and
    reading a producer field inside it as a claim was refusing the most ordinary
    shape a human sign off takes. Everything else the record carries is a claim
    about the record, at any depth: the collector gathered the top level, the
    signature block and the provenance block and nothing else, so a machine
    declaration one level inside any other field was never walked at all.
    """
    material = _attached_material_names(record)
    return {name: item for name, item in record.items() if name not in material}


def _non_human_declaration(value) -> bool:
    """True when anything in this surface declares a producer that is not a person."""
    if isinstance(value, Mapping):
        for name, item in value.items():
            declared = _declared_kind(item)
            if name in ACTOR_DECLARING_FIELDS and declared != HUMAN_ACTOR_KIND:
                return True
            if _non_human_declaration(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_non_human_declaration(item) for item in value)
    return False


def _non_human_provenance(provenance) -> bool:
    """True when a provenance block declares that a machine produced the record.

    A provenance block may carry a chain, and a chain is a history: a build
    runner gathered the evidence and a person then read it and signed. Reading
    every step of that history as a claim about who produced the record refused
    the most ordinary honest shape there is. Only the last step of the chain is
    that claim; the steps before it are the history the record is entitled to
    have. Everything else in the block is read as a claim, at any depth.
    """
    if not isinstance(provenance, Mapping):
        return _non_human_declaration(provenance)
    for name, item in provenance.items():
        if name == PROVENANCE_CHAIN_FIELD:
            continue
        declared = _declared_kind(item)
        if name in ACTOR_DECLARING_FIELDS and declared != HUMAN_ACTOR_KIND:
            return True
        if _non_human_declaration(item):
            return True
    chain = provenance.get(PROVENANCE_CHAIN_FIELD)
    if isinstance(chain, (list, tuple)):
        return bool(chain) and _non_human_declaration(chain[-1])
    return _non_human_declaration(chain)


def _non_human_surface(surface: Mapping) -> bool:
    """True when what a record says about its own production names a machine."""
    for name, item in surface.items():
        if name == PROVENANCE_FIELD:
            if _non_human_provenance(item):
                return True
            continue
        declared = _declared_kind(item)
        if name in ACTOR_DECLARING_FIELDS and declared != HUMAN_ACTOR_KIND:
            return True
        if _non_human_declaration(item):
            return True
    return False


def _declares_an_actor(value) -> bool:
    """True when anything here names a field by which a record declares a maker.

    This asks only whether the declaration is present, not what it says. A
    declared evidence field is excluded from the provenance surface, so a record
    that declared a producer under a field it had named as attached material was
    relabelling its own provenance as evidence and closing on it. Material the
    signer attaches under the names this kernel knows for attachments is read as
    evidence and is not held to this; a name the record chose for itself, and
    then declared its way out of the surface with, is.
    """
    if isinstance(value, Mapping):
        return any(
            name in ACTOR_DECLARING_FIELDS or _declares_an_actor(item)
            for name, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_declares_an_actor(item) for item in value)
    return False


def _is_attached_material(material) -> bool:
    """Whether a value is material a signature can be about.

    Attached material is a list, a mapping or a reference to it. A reference is
    a non empty string, because an attachment recorded as the one string that
    names it is the same attachment as that string in a list of one, and
    refusing the first while admitting the second was a rule about shape rather
    than about evidence. A field that carries nothing, or carries something that
    is not material at all, accounts for nothing.
    """
    if isinstance(material, str):
        return bool(material)
    return isinstance(material, (list, tuple, Mapping))


def _declared_evidence_fields(record: Mapping) -> frozenset[str] | None:
    """Return the field names a record declares as the material it attaches.

    Naming two attachment field names was not an account of a record either: a
    signer attaches a board pack, an exhibit or a run log under the name that
    material has. The record declares those names itself, inside the body its
    anchor sealed, so the account stays positive and stays the signer's. A
    declaration that names a field of this kernel, a field that declares an
    actor, a field that is not there or a field whose value is not attached
    material is not a declaration of evidence and None is returned, which
    refuses the record. Neither is a field whose value declares, at any depth,
    who or what produced something: a record may not relabel its own provenance
    as an attachment.

    Attached material is a list, a mapping or a reference to it. A reference is
    a non empty string, because an attachment recorded as the one string that
    names it is the same attachment as that string in a list of one, and
    refusing the first while admitting the second was a rule about shape rather
    than about evidence.
    """
    declared = record.get(EVIDENCE_DECLARATION_FIELD)
    if declared is None:
        return frozenset()
    if not isinstance(declared, (list, tuple)):
        return None
    names = set()
    for name in declared:
        if not isinstance(name, str) or not name:
            return None
        if name in KERNEL_RECORD_FIELDS or name in ACTOR_DECLARING_FIELDS:
            return None
        material = record.get(name)
        if not _is_attached_material(material):
            return None
        if _declares_an_actor(material):
            return None
        names.add(name)
    return frozenset(names)


def _undeclared_provenance(record: Mapping) -> bool:
    """True when a record does not account positively for its own production.

    The earlier control asked whether a field name it knew of declared a
    machine, so a record that declared one under a name the list had never
    heard passed. This asks the other question. The fields a record carries
    have to be fields this kernel defines, the evidence its signer attaches or
    the evidence that signer declares by name; a signature block holds only the
    parts of a signature; and the record has to carry a provenance block that
    says a person made it rather than merely fail to say a machine did. That
    block is required wherever a person's signature is, because a positive shape
    nothing has to carry is not a control: every record simply left it out.

    The block's own field names are not held to a list. Prose belongs in a
    provenance block under whatever name it is written under, and the walk above
    already refuses anything in the block that declares a machine at any depth.
    What is required is the positive declaration itself.
    """
    declared_evidence = _declared_evidence_fields(record)
    if declared_evidence is None:
        return True
    # The two attachment names this kernel defines are held to the same shape as
    # the names a signer declares. They were exempt from it, so an attachment of
    # an empty string passed under "attachments" and was refused under a name the
    # signer chose. Nothing was gained by that, because neither name is read as a
    # claim about who signed, and it is closed because the rule this module
    # states about attached material is either true everywhere or not stated.
    for name in DECLARED_EVIDENCE_FIELDS:
        if name in record and not _is_attached_material(record[name]):
            return True
    for name in record:
        if (
            name not in KERNEL_RECORD_FIELDS
            and name not in DECLARED_EVIDENCE_FIELDS
            and name not in declared_evidence
        ):
            return True
    signoff = record.get(SIGNATURE_FIELD)
    if not isinstance(signoff, Mapping) or set(signoff) - SIGNATURE_FIELDS:
        return True
    provenance = record.get(PROVENANCE_FIELD)
    if not isinstance(provenance, Mapping):
        return True
    if _declares_a_person(provenance):
        return False
    # A chain is a history and its last step is the claim, which is how the walk
    # above already reads it. Requiring the block's own top level to declare a
    # person as well made the chain decorative: the shape this design was built
    # for, a runner that gathered the evidence and the person who then read and
    # signed it, only passed if the top level said what the last step says.
    chain = provenance.get(PROVENANCE_CHAIN_FIELD)
    if isinstance(chain, (list, tuple)) and chain:
        last = chain[-1]
        return not (isinstance(last, Mapping) and _declares_a_person(last))
    return True


def _declares_a_person(block: Mapping) -> bool:
    """True when this block says positively that a person made the record."""
    return any(
        _declared_kind(item) == HUMAN_ACTOR_KIND
        for name, item in block.items()
        if name in ACTOR_DECLARING_FIELDS
    )


def _human_signoff_rejection(
    record: dict,
    identities: Mapping[str, str],
    as_of: datetime,
    opened_at: datetime | None,
    required_name: str | None = None,
) -> tuple[str | None, str | None]:
    """Reject anything but a named person's own signature, and say whose it is.

    What this returns is the signer's identity and never the name they signed
    with. Everything downstream counts signers and holds them apart, and a count
    of names is not a count of people.

    Where the documents name the person rather than a role, required_name is
    that name and the signature has to be theirs. The comparison is between
    identities and not between strings: the required name is read through the
    same roster the signature is, so the caller's own statement of who is one
    person decides this as it decides every other count here.
    """
    signoff = record.get(SIGNATURE_FIELD)
    if not isinstance(signoff, dict):
        return "human_signoff_missing", None
    if _non_human_surface(_provenance_surface(record)):
        return "human_signoff_not_human", None
    # Read exactly, not casefolded. The surface walk above is tolerant of
    # spelling, so this comparison is the only thing standing between a record
    # and an actor kind that is not the word the roster rule names.
    if signoff.get("actor_kind") != HUMAN_ACTOR_KIND:
        return "human_signoff_not_human", None
    if _undeclared_provenance(record):
        return "human_signoff_provenance_undeclared", None
    # The name is folded once so an honest variation of a spelling reaches the
    # roster row it belongs to, and the roster, not the spelling, says who this
    # is. A name the roster does not map is refused here and never guessed at.
    actor = _normalised_name(signoff.get("actor"))
    identity = identities.get(actor) if actor is not None else None
    if identity is None:
        return "human_signoff_actor_rejected", None
    if required_name is not None:
        # A row or a gate the documents source to a named person. The name is
        # resolved the way the signature's name was, so two spellings of one
        # person are one person here too, and a run that does not accept the
        # required name cannot close this row at all.
        required_identity = identities.get(required_name)
        if required_identity is None:
            return "human_signoff_required_signer_unavailable", None
        if identity != required_identity:
            return "human_signoff_required_signer_mismatch", None
    statement = signoff.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        return "human_signoff_statement_missing", None
    signed_at = _instant(signoff.get("signed_at"))
    if signed_at is None:
        return "human_signoff_temporal_missing", None
    if signed_at > as_of:
        return "human_signoff_future_dated", None
    # The record has already been read against its own window, so a signature
    # at or before as_of is inside that window once it is not older than the
    # record it signs. There is no upper bound left to state here.
    if opened_at is not None and signed_at < opened_at:
        return "human_signoff_predates_record", None
    return None, identity


def _exit_gate_rejection(
    gate: str,
    record_id: str,
    candidate: str,
    identities: Mapping[str, str],
    exit_gate_resolver: Resolver,
    human_trust_anchors: Mapping[str, bytes],
    as_of: datetime,
) -> tuple[str | None, str | None]:
    """Adjudicate one programme exit gate; never take its record on trust."""
    record = exit_gate_resolver(record_id)
    if not isinstance(record, Mapping):
        return "exit_gate_unresolved", None
    unauthenticated, record = _authenticate(record, record_id, human_trust_anchors)
    if unauthenticated is not None:
        return f"exit_gate_{unauthenticated}", None
    if record.get("record_type") != EXIT_GATE_RECORD_TYPE:
        return "exit_gate_record_type_mismatch", None
    if record.get("gate") != gate:
        return "exit_gate_mismatch", None
    # A gate record reaches the candidate it was issued against and no other.
    # A proof is held to an origin candidate and an authenticated bridge; a gate
    # was held only to appearing somewhere in a self declared and unbounded list,
    # so a signature made on an earlier candidate that pre-named the final one
    # closed the gate. The choice here is to bound the list rather than to grow
    # the bridge machinery onto the gates: accepting an exit is a single act
    # about a single candidate, it is never carried forward, and a bound that is
    # one comparison cannot be got wrong the way a second kind of bridge
    # attestation, with its own signature rules, could be.
    scope = _candidate_scope(record.get("candidates"))
    if scope is None or candidate not in scope:
        return "exit_gate_candidate_inapplicable", None
    if scope != {candidate}:
        return "exit_gate_candidate_scope_unbounded", None
    if record.get("revoked_at") is not None:
        return "exit_gate_revoked", None
    outdated = _temporal_rejection(
        record, as_of, recorded_field="signed_at", future_reason="signed_in_future"
    )
    if outdated is not None:
        return f"exit_gate_{outdated}", None
    unsigned, identity = _human_signoff_rejection(
        record,
        identities,
        as_of,
        _instant(record.get("signed_at")),
        SOURCE_NAMED_SIGNERS.get(gate),
    )
    if unsigned is not None:
        return f"exit_gate_{unsigned}", None
    return None, identity


def _string_set(value) -> set[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return {item for item in value if isinstance(item, str)}
    return set()


def _candidate_scope(value) -> set[str] | None:
    """Return the candidates a record names, or None when that is not a list of them."""
    if not isinstance(value, (list, tuple, set, frozenset)) or not value:
        return None
    if any(not isinstance(item, str) or not item for item in value):
        return None
    return set(value)


def _gate_references(value) -> tuple[str, ...] | None:
    """Return the records offered for one exit gate, or None when unreadable.

    An exit whose documented reviewer population is one is named by one
    reference and reads as the one seat it is. An exit that needs more people
    names one record per seat, because a record carries one signature and a
    population of three cannot be written into one. Two seats named by the same
    record are one record read twice and not two reviewers, so a repeated
    reference is refused rather than counted.
    """
    if isinstance(value, str):
        return (value,) if value else None
    if not isinstance(value, (list, tuple)) or not value:
        return None
    references = tuple(value)
    if any(not isinstance(item, str) or not item for item in references):
        return None
    if len(set(references)) != len(references):
        return None
    return references


def _proof_ids_malformed(proof_ids) -> bool:
    """Whether a row's proof id list is one this kernel cannot read at all."""
    if proof_ids is None:
        return False
    if not isinstance(proof_ids, list):
        return True
    if any(not isinstance(proof_id, str) or not proof_id for proof_id in proof_ids):
        return True
    return len(proof_ids) != len(set(proof_ids))


def _malformed_evidence_rows(evidence: Mapping[str, object]) -> list[str]:
    """Name every row this kernel cannot read, not only the first one it meets.

    The refusal itself is unchanged: an evidence set carrying a malformed row is
    still refused outright, and a caller cannot talk the kernel into reporting
    one instead. What changed is that the refusal names all of them, because a
    caller who learns about one bad row per run learns nothing about the shape
    of the input they handed over.

    Only the rows the adjudicating walk actually reads are read here. A row that
    is neither achieved nor amended was never asked for a proof list or a
    decision id, and naming every bad row must not start asking.
    """
    malformed = []
    for requirement, row in evidence.items():
        if not isinstance(row, dict):
            malformed.append(requirement)
            continue
        state = row.get("state")
        if state is not None and not isinstance(state, str):
            malformed.append(requirement)
            continue
        if state == "achieved":
            if _proof_ids_malformed(row.get("proof_ids")):
                malformed.append(requirement)
        elif state == "amended":
            decision_id = row.get("user_decision_id")
            if decision_id is not None and (
                not isinstance(decision_id, str) or not decision_id
            ):
                malformed.append(requirement)
    return sorted(malformed)


def _proof_rejection(
    requirement: str,
    row: dict,
    candidate: str,
    identities: Mapping[str, str],
    proof_resolver: Resolver,
    trust_anchors: Mapping[str, bytes],
    human_trust_anchors: Mapping[str, bytes],
    as_of: datetime,
) -> tuple[str | None, set[str], bool]:
    """Adjudicate a row's proofs and report whether any of them was adjudicated.

    The met or unmet decision is taken from what this walk actually read. The
    row itself is never asked again: a list that is empty to iteration and
    truthy to bool() closed every row of the programme with no record resolved.
    """
    signers: set[str] = set()
    proof_ids = row.get("proof_ids")
    if proof_ids is None:
        return None, signers, False
    # The whole evidence set is swept for malformed rows before any of it is
    # adjudicated, so a caller is told about all of them at once. This guard
    # stays because it belongs to this function, not to that sweep.
    if _proof_ids_malformed(proof_ids):
        raise ValueError("evidence_row_invalid")
    on_the_roster = requirement in HUMAN_SIGNOFF_REQUIREMENTS
    adjudicated = 0
    for proof_id in proof_ids:
        record = proof_resolver(proof_id)
        if not isinstance(record, Mapping):
            return "proof_unresolved", signers, False
        unauthenticated, record = _authenticate(
            record,
            proof_id,
            human_trust_anchors if on_the_roster else trust_anchors,
        )
        if unauthenticated is not None:
            return f"proof_{unauthenticated}", signers, False
        if record.get("record_type") != PROOF_RECORD_TYPE:
            return "proof_record_type_mismatch", signers, False
        requirements = _string_set(record.get("requirements"))
        if requirement not in requirements:
            return "proof_requirement_mismatch", signers, False
        if len(requirements & HUMAN_SIGNOFF_REQUIREMENTS) > 1:
            # One person is not several assessors. A record that claims more
            # than one row of the human roster is one signature standing in for
            # several, whoever holds the pen.
            return "proof_human_signoff_scope_too_broad", signers, False
        if record.get("revoked_at") is not None:
            return "proof_revoked", signers, False
        outdated = _temporal_rejection(
            record,
            as_of,
            recorded_field="recorded_at",
            future_reason="recorded_in_future",
        )
        if outdated is not None:
            return f"proof_{outdated}", signers, False
        origin = record.get("origin_candidate")
        if not isinstance(origin, str) or not origin:
            return "proof_origin_candidate_missing", signers, False
        candidates = _string_set(record.get("candidates"))
        if candidate not in candidates:
            return "proof_candidate_incompatible", signers, False
        if origin not in candidates:
            return "proof_origin_candidate_unlisted", signers, False
        # A record reaches the candidate it was recorded against and whatever an
        # adjudicated bridge attestation carries it to. Listing a second
        # candidate beside a self declared origin is not a second candidate it
        # was earned on, and neither is a bridge nobody ever read.
        reachable = {origin}
        if origin != candidate:
            unbridged = _bridge_rejection(
                record,
                proof_id,
                requirement,
                origin,
                candidate,
                identities,
                proof_resolver,
                trust_anchors,
                human_trust_anchors,
                as_of,
            )
            if unbridged is not None:
                return f"proof_{unbridged}", signers, False
            reachable.add(candidate)
        if candidates - reachable:
            return "proof_candidate_scope_unattested", signers, False
        if on_the_roster:
            unsigned, identity = _human_signoff_rejection(
                record,
                identities,
                as_of,
                _instant(record.get("recorded_at")),
                SOURCE_NAMED_SIGNERS.get(requirement),
            )
            if unsigned is not None:
                return f"proof_{unsigned}", signers, False
            if identity is not None:
                signers.add(identity)
        adjudicated += 1
    return None, signers, adjudicated > 0


def _decision_rejection(
    requirement: str,
    row: dict,
    candidate: str,
    identities: Mapping[str, str],
    decision_resolver: Resolver,
    human_trust_anchors: Mapping[str, bytes],
    as_of: datetime,
) -> tuple[str | None, str | None, bool]:
    decision_id = row.get("user_decision_id")
    if decision_id is None:
        return None, None, False
    if not isinstance(decision_id, str) or not decision_id:
        raise ValueError("evidence_row_invalid")
    record = decision_resolver(decision_id)
    if not isinstance(record, Mapping):
        return "decision_unresolved", None, False
    unauthenticated, record = _authenticate(
        record, decision_id, human_trust_anchors
    )
    if unauthenticated is not None:
        return f"decision_{unauthenticated}", None, False
    if record.get("record_type") != DECISION_RECORD_TYPE:
        return "decision_record_type_mismatch", None, False
    issuer = _normalised_name(record.get("issuer"))
    if issuer is None or issuer not in identities:
        return "decision_issuer_rejected", None, False
    if record.get("requirement") != requirement:
        return "decision_requirement_mismatch", None, False
    scope_delta = record.get("scope_delta")
    if not isinstance(scope_delta, str) or not scope_delta.strip():
        return "decision_scope_delta_not_accepted", None, False
    if record.get("accepted") is not True:
        return "decision_scope_delta_not_accepted", None, False
    # An amendment is a decision about one candidate, bounded the same way an
    # exit gate is, and for the same reason: nothing else held it to the
    # candidate it was actually taken on.
    scope = _candidate_scope(record.get("candidates"))
    if scope is None or candidate not in scope:
        return "decision_candidate_inapplicable", None, False
    if scope != {candidate}:
        return "decision_candidate_scope_unbounded", None, False
    if record.get("revoked_at") is not None:
        return "decision_revoked", None, False
    outdated = _temporal_rejection(
        record, as_of, recorded_field="decided_at", future_reason="decided_in_future"
    )
    if outdated is not None:
        return f"decision_{outdated}", None, False
    unsigned, identity = _human_signoff_rejection(
        record,
        identities,
        as_of,
        _instant(record.get("decided_at")),
        SOURCE_NAMED_SIGNERS.get(requirement),
    )
    if unsigned is not None:
        return f"decision_{unsigned}", None, False
    return None, identity, True


def _trusted_instant(trusted_clock) -> datetime:
    """Read the caller's trusted clock; anything but an instant is no clock."""
    if not callable(trusted_clock):
        raise ValueError("trusted_clock_invalid")
    try:
        reading = trusted_clock()
    except Exception as error:
        # A clock that throws is no clock, whatever it was meant to read.
        raise ValueError("trusted_clock_invalid") from error
    if isinstance(reading, datetime):
        if reading.tzinfo is None or reading.tzinfo.utcoffset(reading) is None:
            raise ValueError("trusted_clock_invalid")
        return reading
    now = _instant(reading)
    if now is None:
        raise ValueError("trusted_clock_invalid")
    return now


def _independent_signer_count(coverage: Mapping[str, set[str]]) -> int:
    """Return how many signers can each be credited a row or gate of their own.

    Counting names was not a coverage rule. Three people co-signing one easy
    row, beside one person signing the other twenty and every gate, was four
    distinct signers by a tally and one assessor's work by any other reading.
    A signer counts here only when there is a row or a gate that is theirs and
    nobody else's in the same count, which is the largest matching between the
    signers and what they signed.
    """
    matched: dict[str, str] = {}

    def credit(signer: str, tried: set[str]) -> bool:
        for item in sorted(coverage[signer]):
            if item in tried:
                continue
            tried.add(item)
            if item not in matched or credit(matched[item], tried):
                matched[item] = signer
                return True
        return False

    return sum(credit(signer, set()) for signer in sorted(coverage))


def certify_requirements(
    required: Iterable[str],
    evidence: dict[str, dict],
    *,
    candidate: str,
    accepted_issuers: Collection[str],
    identity_roster: Mapping[str, str],
    proof_resolver: Resolver,
    decision_resolver: Resolver,
    trust_anchors: Mapping[str, bytes],
    human_trust_anchors: Mapping[str, bytes],
    minimum_distinct_signers: int,
    as_of: str,
    trusted_clock: Callable[[], str | datetime],
    exit_gates: Mapping[str, str | Sequence[str]],
    exit_gate_resolver: Resolver,
) -> dict:
    """Validate the certification request, then run the closure kernel.

    The required identifiers must be exactly the requirement universe with no
    duplicates. An evidence set carrying a row this kernel cannot read is
    refused outright, before anything in it is adjudicated, and the refusal
    names every such row rather than the first one it met. An achieved row keeps its proof only when every proof id
    resolves to an immutable record that states it is a proof, names this
    requirement and applies to this candidate, directly or through a
    compatibility bridge whose own authenticated attestation states it is a
    bridge and names the proof and both candidates.
    Every resolved record must be sealed by one of the injected trust anchors
    and bound to the identifier it was resolved under, must not be revoked, and
    must sit inside its own validity window at the certification instant as_of.
    That instant is itself bound against trusted_clock and refused when it sits
    further from that clock than AS_OF_TOLERANCE.
    An amended row keeps its decision only when the decision record resolves,
    was issued by an accepted issuer, names this exact requirement, carries an
    accepted scope delta, names this candidate and no other and is not revoked.
    Rows that fail those checks are reported under rejected and stay unmet. The
    rows on the human sign off roster additionally need a person's own signature
    on the record that closes them, sealed by one of the separate
    human_trust_anchors, and every programme exit gate needs the same signature
    on its own authenticated record naming this candidate and no other. Every
    name in accepted_issuers, and every name a record signs with, is read
    through one normalisation, so a name that is empty once normalised or that
    mixes scripts is not a name here. That normalisation is a convenience in
    front of identity_roster, which is where a name becomes a person: the roster
    maps every accepted issuer to a stable identity, an issuer it does not map
    is refused, and every count and every distinctness rule below is over
    identities. A proof may carry at most one roster row, and the closure needs
    at least minimum_distinct_signers distinct people across the roster and the
    gate seats before it is closable. Each exit gate takes one record reference
    or a list of them, and needs as many distinct identities as
    PROGRAMME_EXIT_GATE_SIGNERS records for it. An identity that holds a seat of
    one exit may hold a seat of another, because the exits carry populations of
    their own and nothing across them. MINIMUM_DISTINCT_SIGNER_FLOOR is the sum
    of those populations, which is a seat total and not a count of people; the
    result reports the stated number and, as effective_minimum_distinct_signers,
    the larger of the two. The rows and gates in SOURCE_NAMED_SIGNERS take the
    signature of the person their source names and no other.
    """
    universe = requirement_universe()
    required_ids = list(required)
    if any(not isinstance(requirement, str) for requirement in required_ids):
        raise ValueError("requirement_id_invalid")
    if len(required_ids) != len(set(required_ids)):
        raise ValueError("requirement_ids_duplicate")
    if set(required_ids) - universe:
        raise ValueError("requirement_ids_unknown")
    if universe - set(required_ids):
        raise ValueError("requirement_ids_missing")
    if not isinstance(evidence, dict):
        raise ValueError("evidence_map_required")
    # Read the evidence map once, here, and adjudicate that copy. A row could
    # answer one body to the walk that adjudicated it and another to the walk
    # that decided whether it was met, and a proof list that is empty to
    # iteration and truthy to bool() closed all 84 rows with nothing resolved.
    evidence = _plain(evidence)
    if set(evidence) - universe:
        raise ValueError("evidence_requirement_unknown")
    if not isinstance(candidate, str) or not candidate:
        raise ValueError("candidate_required")
    # A roster of names, normalised once here and read as a set everywhere
    # after. A bare string iterated into single characters, every one of them a
    # non empty string, so it passed this check, counted its characters as
    # distinct signers and turned every membership test into a substring test.
    if isinstance(accepted_issuers, (str, bytes, bytearray)):
        raise ValueError("accepted_issuer_invalid")
    if not isinstance(accepted_issuers, Collection):
        raise ValueError("accepted_issuers_required")
    # Read the roster once and adjudicate that reading. It was iterated to check
    # the names and iterated again to build the set, so a Collection could show
    # one roster to the check and a wider one to the set, and the name it added
    # on the second reading was then credited as the signer of an exit gate.
    offered = list(accepted_issuers)
    normalised_issuers = set()
    for issuer in offered:
        if not isinstance(issuer, str) or not issuer:
            raise ValueError("accepted_issuer_invalid")
        name = _normalised_name(issuer)
        if name is None:
            raise ValueError("accepted_issuer_invalid")
        normalised_issuers.add(name)
    # Names are compared only in the normalised form, here and everywhere after,
    # so the obvious duplicates of one spelling are one accepted issuer.
    issuers = frozenset(normalised_issuers)
    if not issuers:
        raise ValueError("accepted_issuers_required")
    # And then the caller says who those names are. Nothing below this line
    # counts a name or compares two: the roster turns each accepted issuer into
    # an identity here, once, and identities are what the rest of this reads.
    identities = _checked_identities(identity_roster, issuers)
    anchors = _checked_trust_anchors(trust_anchors)
    human_anchors = _checked_trust_anchors(
        human_trust_anchors, label="human_trust_anchors"
    )
    # A human gate that shares its anchor with routine machine evidence is a
    # key, not a person: whatever seals CI output could mint the signature. Two
    # keys are compared by what HMAC makes of them, not by their bytes, so a
    # padded or over long spelling of the routine key is not a second anchor.
    routine_keys = {_hmac_key_identity(key) for key in anchors.values()}
    human_keys = {_hmac_key_identity(key) for key in human_anchors.values()}
    if set(human_anchors) & set(anchors) or (human_keys & routine_keys):
        raise ValueError("human_trust_anchors_not_separate")
    # Every roster row and every gate seat is a slot one person can be credited
    # with, and the caller cannot ask for more independent assessors than there
    # are slots to credit them from.
    signable = len(HUMAN_SIGNOFF_REQUIREMENTS) + MINIMUM_DISTINCT_SIGNER_FLOOR
    if (
        isinstance(minimum_distinct_signers, bool)
        or not isinstance(minimum_distinct_signers, int)
        or minimum_distinct_signers < 1
        or minimum_distinct_signers > signable
    ):
        raise ValueError("minimum_distinct_signers_invalid")
    # A roster of names that is a shorter roster of people cannot furnish more
    # independent assessors than it holds people.
    if len(set(identities.values())) < minimum_distinct_signers:
        raise ValueError("accepted_issuers_insufficient")
    instant = _instant(as_of)
    if instant is None:
        raise ValueError("as_of_invalid")
    if abs(instant - _trusted_instant(trusted_clock)) > AS_OF_TOLERANCE:
        raise ValueError("as_of_outside_trusted_clock")
    if not isinstance(exit_gates, Mapping):
        raise ValueError("exit_gates_required")
    if set(exit_gates) - set(PROGRAMME_EXIT_GATES):
        raise ValueError("exit_gate_unknown")
    offered_gates = {}
    for gate, reference in exit_gates.items():
        references = _gate_references(reference)
        if references is None:
            raise ValueError("exit_gate_reference_invalid")
        offered_gates[gate] = references
    # Routine evidence may be sealed by the stronger anchors too; only the rows
    # and gates that carry a person's name are held to them alone.
    routine_anchors = {**anchors, **human_anchors}
    # Read the whole evidence set for shape before adjudicating any of it, so
    # the refusal names every row that cannot be read rather than the first.
    malformed = _malformed_evidence_rows(evidence)
    if malformed:
        raise ValueError(f"evidence_row_invalid: {', '.join(malformed)}")
    # Which rows claim anything at all, read for shape and nothing more. A row
    # that claims nothing is unmet and no resolver is ever asked about it.
    #
    # Three edits in this module are behaviour neutral on their own, and they are
    # disclosed here rather than counted as guards a weakening edit killed.
    # Removing this pre pass with the skip it feeds below passes every test,
    # because the adjudicating walk refuses the same rows for itself. Removing
    # the "not row_met" guard below passes every test as well. Removing both of
    # them together does not: five named tests fail, among them
    # test_a_row_that_is_truthy_but_empty_resolves_nothing_and_closes_nothing and
    # test_a_row_carrying_a_hollow_proof_list_leaves_only_that_row_unmet. Those
    # two are a redundant pair which is load bearing jointly and unkillable one
    # at a time, each making the other survive a single edit. The third is the
    # length comparison in _row_claims_to_be_met, which is equivalent to a
    # truthiness test once the row has been read as the plain list it must be.
    claimed = set(required_ids) - set(_unmet_requirements(required_ids, evidence))
    met: set[str] = set()
    rejected: dict[str, str] = {}
    coverage: dict[str, set[str]] = {}
    for requirement, row in evidence.items():
        if not isinstance(row, dict):
            raise ValueError("evidence_row_invalid")
        state = row.get("state")
        if state is not None and not isinstance(state, str):
            raise ValueError("evidence_row_invalid")
        if requirement not in claimed:
            continue
        row_signers: set[str] = set()
        row_met = False
        if state == "achieved":
            reason, row_signers, row_met = _proof_rejection(
                requirement,
                row,
                candidate,
                identities,
                proof_resolver,
                routine_anchors,
                human_anchors,
                instant,
            )
        elif state == "amended":
            reason, actor, row_met = _decision_rejection(
                requirement,
                row,
                candidate,
                identities,
                decision_resolver,
                human_anchors,
                instant,
            )
            row_signers = {actor} if actor is not None else set()
        else:
            reason = None
        if reason is not None:
            rejected[requirement] = reason
            continue
        # A row is met because this run adjudicated something for it, never
        # because the row says so when it is asked a second time. This guard and
        # the pre pass above are the redundant pair the comment there discloses.
        if not row_met:
            continue
        met.add(requirement)
        if requirement in HUMAN_SIGNOFF_REQUIREMENTS:
            for actor in row_signers:
                coverage.setdefault(actor, set()).add(requirement)
    unmet = sorted(set(required_ids) - met)
    gate_rejections: dict[str, str] = {}
    for gate in PROGRAMME_EXIT_GATES:
        references = offered_gates.get(gate)
        if references is None:
            gate_rejections[gate] = "exit_gate_missing"
            continue
        gate_reason = None
        held: list[str] = []
        for reference in references:
            reason, gate_actor = _exit_gate_rejection(
                gate,
                reference,
                candidate,
                identities,
                exit_gate_resolver,
                human_anchors,
                instant,
            )
            if reason is not None:
                gate_reason = reason
                break
            if gate_actor is None:
                # A seat credited only when an actor came back is a seat that
                # could be absent from this list and from the signers at once,
                # and a closure would then run on fewer reviewers than the
                # documents name. The adjudication above names an identity
                # whenever it names no reason, so nothing a caller can send
                # arrives here; the shape says so rather than resting on it. A
                # seat nobody signed is a refusal by name, never a silent gap.
                gate_reason = "exit_gate_signer_missing"
                break
            held.append(gate_actor)
        # The documents name a reviewer population per exit, and a population is
        # people. Two seats answered by one identity are one reviewer signing
        # twice, which is why this counts identities and not records.
        if gate_reason is None and (
            len(set(held)) < PROGRAMME_EXIT_GATE_SIGNERS[gate]
        ):
            gate_reason = "exit_gate_signers_insufficient"
        if gate_reason is not None:
            gate_rejections[gate] = gate_reason
            continue
        # Each reviewer an exit needs is a coverage slot of its own, so a
        # population of three can credit three independent assessors, not one.
        for position, gate_actor in enumerate(sorted(set(held))):
            coverage.setdefault(gate_actor, set()).add(
                f"exit_gate:{gate}:{position}"
            )
    signers = set(coverage)
    signer_rejection = None
    # Each exit carries the reviewer population its own line names, and the
    # check above holds every exit to that population in identities. The
    # documents name no rule against one person sitting on two exits, so this
    # module imposes none: an identity that holds a seat of one exit may hold a
    # seat of another, and each exit is still held to its own count.
    if _independent_signer_count(coverage) < minimum_distinct_signers:
        signer_rejection = "insufficient_distinct_signers"
    return {
        "candidate": candidate,
        "as_of": as_of,
        "unmet": unmet,
        "rejected": rejected,
        "exit_gates": gate_rejections,
        # The identities that were credited, never the names they signed with.
        "signers": sorted(signers),
        "minimum_distinct_signers": minimum_distinct_signers,
        # What the caller asked for, beside the seats the documents name. The
        # exit rule needs the reviewer population named on each of the five
        # programme exits whatever number was stated, and those populations come
        # to nine seats. Seats are not people: one identity may hold a seat on
        # two exits, so this is the seat total and not a count of people a
        # closure was held to.
        "effective_minimum_distinct_signers": max(
            minimum_distinct_signers, MINIMUM_DISTINCT_SIGNER_FLOOR
        ),
        "signer_rejection": signer_rejection,
        "closable": not unmet
        and not rejected
        and not gate_rejections
        and signer_rejection is None,
    }
