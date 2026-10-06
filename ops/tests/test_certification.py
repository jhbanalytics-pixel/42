import hashlib
import hmac
import json
import os
import re
from collections.abc import Collection
from pathlib import Path

import pytest

from ops.certification import audit_candidate, close_programme
from ops.certification.close_programme import (
    _unmet_requirements,
    certify_requirements,
    requirement_universe,
)

# The plan documents live outside the repository, so the crosswalk derived from
# them is vendored here. Every entry carries the document and the line it came
# from, so a failure names the row to go and read. The tests over that artefact
# are a tripwire over three copies that must agree, module, artefact and test,
# and not evidence from the documents: all three sit in this commit, so nothing
# there can catch a derivation that was wrong when it was made.
#
# The master plan itself is read when MASTER_PLAN_PATH names it on this machine,
# and the test that reads it skips when it does not. A fixture in this commit is
# not that document and does not stand in for it, so the two checks are separate
# and the one that cannot run here says so instead of passing against a copy.
CLOSURE_CROSSWALK_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "closure_crosswalk"
    / "crosswalk.json"
)
_MASTER_PLAN_PATH = os.environ.get("MASTER_PLAN_PATH")
MASTER_PLAN_FILE = Path(_MASTER_PLAN_PATH) if _MASTER_PLAN_PATH else None
CROSSWALK_SECTIONS = (
    "requirement_ids",
    "human_signoff_requirements",
    "programme_exit_gates",
)
CANDIDATE = "candidate-819905d"
# How many distinct people each exit needs, as the decisions record names them:
# three strategist roles for E02, two blind claim reviewers for E01, cluster
# reviewers for L01 and the client reviewer for B04. E04 is the owner's own
# acceptance, which is one person. The same counts are parsed out of the quoted
# document line further down, so this copy and the module cannot drift apart.
GATE_SIGNER_COUNTS = {"E01": 2, "E02": 3, "L01": 2, "B04": 1, "E04": 1}
# One name per seat, all of them different. No exit is held twice by one hand,
# which every exit is held to, and no exit shares a signer with another, which
# none of them is held to: the fixture is wider than the rule so that a test
# varying one seat varies one thing. E04's single seat is the owner's own,
# because the document that sources that exit requires his own acceptance.
# "thabo" is deliberately absent from all of them, so there is one assessor who
# holds roster rows and no gate.
GATE_SIGNERS = {
    "E01": ("lerato", "naledi"),
    "E02": ("nomsa", "tumi", "farai"),
    "L01": ("sipho", "zanele"),
    "B04": ("yusuf",),
    "E04": ("albert",),
}
# The first seat of each exit, for the tests that vary one hand per exit, and
# the seats those tests leave holding their own names.
GATE_LEAD_SIGNERS = {gate: names[0] for gate, names in GATE_SIGNERS.items()}
GATE_CO_SIGNERS = frozenset(
    name for names in GATE_SIGNERS.values() for name in names[1:]
)
# The exits the documents name a role or a population for, which any accepted
# issuer may sign. E04 is not one of them.
ANY_ISSUER_GATES = ("E01", "E02", "L01", "B04")
# The names the default fixtures sign with: "albert" holds every roster row and
# the owner's own exit, and every other seat carries a name of its own.
ISSUERS = frozenset(
    {"albert", *(name for names in GATE_SIGNERS.values() for name in names)}
)
# Who is one person is the caller's statement, not something this module reads
# off a string. These fixtures hand over a roster of identities: every spelling
# and every name of one assessor maps to that assessor's own identity, and two
# assessors never share one. The identities are deliberately not the names, so a
# rule that counted names instead of identities is visible in every count.
IDENTITY_PREFIX = "person-"
ID_PATTERN = re.compile(r"^[A-Z]{1,3}\d{2,3}$")

# The evidence ledger seals routine machine output. The authority register seals
# what a person put their name to, and it is a separate anchor with a separate
# key: the process that produces CI evidence must not be able to mint a human
# sign off. A record is authentic only when it names an anchor the caller
# supplied for its kind and carries a digest computed with that anchor's key over
# its own canonical body, so a dictionary handed to the resolver is not evidence.
LEDGER_ANCHOR = "evidence-ledger"
REGISTER_ANCHOR = "authority-register"
LEDGER_KEY = b"42-evidence-ledger-key"
REGISTER_KEY = b"42-authority-register-key"
TRUST_ANCHORS = {LEDGER_ANCHOR: LEDGER_KEY}
HUMAN_TRUST_ANCHORS = {REGISTER_ANCHOR: REGISTER_KEY}

AS_OF = "2026-09-18T09:00:00+00:00"
RECORDED_AT = "2026-09-16T08:00:00+00:00"
EXPIRES_AT = "2026-10-16T08:00:00+00:00"
SIGNED_AT = "2026-09-16T09:30:00+00:00"

# The twelve BSA rows and the nine named rows that only a human may close. The
# list is pinned here independently of the module so a change to either side is
# visible in this file.
HUMAN_SIGNOFF_IDS = frozenset(
    {f"BSA{index:02d}" for index in range(1, 13)}
    | {"F05", "F11", "F12", "G07", "G09", "P106", "P203", "P204", "P306"}
)
# The five task exits the programme documents as human gates, mirroring the
# module so a change to either side is visible in this file.
EXIT_GATE_IDS = ("E01", "E02", "L01", "B04", "E04")
# The field a record declares its attached material under.
EVIDENCE_DECLARATION = "evidence_fields"
# Every row and gate a person can be credited with: the ceiling on a stated
# minimum_distinct_signers.
# Every roster row and every exit gate seat is a slot one person can hold.
SIGNABLE_TOTAL = len(HUMAN_SIGNOFF_IDS) + sum(GATE_SIGNER_COUNTS.values())


def seal(body, *, authority, key):
    """Return body with its anchor name and the digest an anchor holder makes."""
    sealed = {name: value for name, value in body.items() if name != "digest"}
    sealed["authority"] = authority
    payload = json.dumps(sealed, sort_keys=True, separators=(",", ":"))
    sealed["digest"] = hmac.new(
        key, payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return sealed


def human_signoff(
    *,
    actor="albert",
    actor_kind="human",
    signed_at=SIGNED_AT,
    statement="Reviewed on the final candidate and accepted.",
):
    return {
        "actor": actor,
        "actor_kind": actor_kind,
        "signed_at": signed_at,
        "statement": statement,
    }


def human_provenance(**overrides):
    """The positive provenance block every signed record has to carry.

    A record that carries a person's signature has to account for its own
    production positively, so this block travels with every signature the
    fixtures build. Passing provenance=None builds the record without it.
    """
    block = {
        "actor_kind": "human",
        "statement": "Gathered and reviewed by the assessor who signed it.",
    }
    block.update(overrides)
    return block


def crosswalk_ids(text):
    """The requirement ids of the acceptance crosswalk table, in table order."""
    lines = text.splitlines()
    start = lines.index("### Acceptance crosswalk")
    ids = []
    for line in lines[start + 1 :]:
        if line.startswith(("### ", "Coverage:")):
            break
        match = re.match(r"^\| ([A-Z]+\d+) \|", line)
        if match:
            ids.append(match.group(1))
    return ids


def closure_crosswalk():
    """The sets derived from the plan documents, as vendored into the tree."""
    return json.loads(CLOSURE_CROSSWALK_PATH.read_text(encoding="utf-8"))


def crosswalk_locators(section, identifiers):
    """Name the document and line behind each identifier, for a failure message."""
    entries = closure_crosswalk()[section]["entries"]
    return [
        f"{identifier} <- {entries[identifier]['document']}"
        f":{entries[identifier]['line']}"
        for identifier in sorted(identifiers)
    ]


def proof_body(
    requirement,
    candidate=CANDIDATE,
    *,
    record_id=None,
    candidates=None,
    origin_candidate=...,
    recorded_at=RECORDED_AT,
    expires_at=EXPIRES_AT,
    signoff=...,
    provenance=...,
    bridge=...,
):
    """Build an unsealed proof body: everything but the anchor and the digest."""
    body = {
        "record_id": f"proof-{requirement}" if record_id is None else record_id,
        "record_type": "proof",
        "requirements": [requirement],
        "candidates": [candidate] if candidates is None else list(candidates),
        "origin_candidate": candidate if origin_candidate is ... else origin_candidate,
        "recorded_at": recorded_at,
        "expires_at": expires_at,
    }
    if signoff is ...:
        signoff = human_signoff() if requirement in HUMAN_SIGNOFF_IDS else None
    if signoff is not None:
        body["human_signoff"] = signoff
        if provenance is ...:
            provenance = human_provenance()
    if provenance is not ... and provenance is not None:
        body["provenance"] = provenance
    if bridge is not ...:
        body["compatibility_bridge"] = bridge
    return body


def anchor_for(requirement):
    """The anchor a row of this kind is sealed by: register for the human roster."""
    if requirement in HUMAN_SIGNOFF_IDS:
        return REGISTER_ANCHOR, REGISTER_KEY
    return LEDGER_ANCHOR, LEDGER_KEY


def proof_record(
    requirement,
    candidate=CANDIDATE,
    *,
    authority=None,
    key=None,
    **body_kwargs,
):
    default_authority, default_key = anchor_for(requirement)
    return seal(
        proof_body(requirement, candidate, **body_kwargs),
        authority=default_authority if authority is None else authority,
        key=default_key if key is None else key,
    )


def decision_body(
    requirement,
    *,
    issuer="albert",
    candidate=CANDIDATE,
    scope_delta="Market KE deferred to the second client release.",
    accepted=True,
    revoked_at=None,
    record_id="decision-7",
    decided_at=RECORDED_AT,
    expires_at=EXPIRES_AT,
    signoff=...,
    provenance=...,
    candidates=None,
    record_type="decision",
    drop_record_id=False,
):
    body = {
        "record_id": record_id,
        "record_type": record_type,
        "issuer": issuer,
        "requirement": requirement,
        "scope_delta": scope_delta,
        "accepted": accepted,
        "candidates": [candidate] if candidates is None else list(candidates),
        "revoked_at": revoked_at,
        "decided_at": decided_at,
        "expires_at": expires_at,
    }
    if signoff is ...:
        signoff = human_signoff()
    if signoff is not None:
        body["human_signoff"] = signoff
        if provenance is ...:
            provenance = human_provenance()
    if provenance is not ... and provenance is not None:
        body["provenance"] = provenance
    if drop_record_id:
        del body["record_id"]
    return body


def decision_record(
    requirement, *, authority=REGISTER_ANCHOR, key=REGISTER_KEY, **body_kwargs
):
    return seal(decision_body(requirement, **body_kwargs), authority=authority, key=key)


def resolver(records):
    return lambda record_id: records.get(record_id)


def default_identity_roster():
    """Every name these fixtures may offer as an issuer, and whose name it is.

    A roster wider than the issuers a given run accepts is the ordinary case:
    the organisation holds one roster of people and a closure names the ones it
    accepts out of it. Every spelling of one assessor, and every name one
    assessor goes by, maps to that assessor's one identity.
    """
    roster = {
        name: f"{IDENTITY_PREFIX}{name}"
        for name in (*ASSESSORS, *CEILING_ASSESSORS)
    }
    for spelling in ONE_ASSESSOR_SPELLINGS:
        roster[spelling] = f"{IDENTITY_PREFIX}andre"
    for spelling in INTERNAL_SPACING_SPELLINGS:
        roster[spelling] = f"{IDENTITY_PREFIX}assessor-one"
    for spelling in INVISIBLE_SPELLINGS:
        roster[spelling] = f"{IDENTITY_PREFIX}kagiso"
    for name in ONE_PERSON_UNDER_TWO_NAMES:
        roster[name] = f"{IDENTITY_PREFIX}mbali"
    return roster


def identity_of(name):
    """The identity the default roster states for one name."""
    return default_identity_roster()[name]


def identities_of(names):
    """The identities behind a set of names, as a result reports them."""
    return sorted({identity_of(name) for name in names})


def full_evidence():
    return {
        requirement: {"state": "achieved", "proof_ids": [f"proof-{requirement}"]}
        for requirement in sorted(requirement_universe())
    }


def full_proofs():
    return {
        f"proof-{requirement}": proof_record(requirement)
        for requirement in requirement_universe()
    }


def unsealed_proofs():
    """A complete, plausible looking evidence set that no anchor ever sealed."""
    return {
        f"proof-{requirement}": proof_body(requirement)
        for requirement in requirement_universe()
    }


def certify(
    evidence,
    *,
    proofs=None,
    decisions=None,
    required=None,
    candidate=CANDIDATE,
    issuers=None,
    identity_roster=...,
    trust_anchors=None,
    human_trust_anchors=None,
    minimum_distinct_signers=1,
    as_of=AS_OF,
    trusted_clock=...,
    exit_gates=None,
    exit_gate_records=None,
):
    return certify_requirements(
        sorted(requirement_universe()) if required is None else required,
        evidence,
        candidate=candidate,
        accepted_issuers=ISSUERS if issuers is None else issuers,
        identity_roster=(
            default_identity_roster() if identity_roster is ... else identity_roster
        ),
        proof_resolver=resolver(full_proofs() if proofs is None else proofs),
        decision_resolver=resolver({} if decisions is None else decisions),
        trust_anchors=dict(TRUST_ANCHORS) if trust_anchors is None else trust_anchors,
        human_trust_anchors=(
            dict(HUMAN_TRUST_ANCHORS)
            if human_trust_anchors is None
            else human_trust_anchors
        ),
        minimum_distinct_signers=minimum_distinct_signers,
        as_of=as_of,
        trusted_clock=(lambda: AS_OF) if trusted_clock is ... else trusted_clock,
        exit_gates=dict(GATE_REFERENCES) if exit_gates is None else exit_gates,
        exit_gate_resolver=resolver(
            full_exit_gate_records() if exit_gate_records is None else exit_gate_records
        ),
    )


def test_green_subset_cannot_close_the_programme():
    evidence = {"G01": {"state": "achieved", "proof_ids": ["p1"]}}
    assert _unmet_requirements({"G01", "D01"}, evidence) == ["D01"]


def test_empty_evidence_cannot_certify_a_required_row():
    assert _unmet_requirements({"F11"}, {}) == ["F11"]
    assert _unmet_requirements({"F11"}, {"F11": {}}) == ["F11"]


def test_achieved_without_proof_ids_is_unmet():
    evidence = {"F11": {"state": "achieved", "proof_ids": []}}
    assert _unmet_requirements({"F11"}, evidence) == ["F11"]
    evidence = {"F11": {"state": "achieved"}}
    assert _unmet_requirements({"F11"}, evidence) == ["F11"]


def test_amendment_needs_a_user_decision():
    assert _unmet_requirements({"BSA07"}, {"BSA07": {"state": "amended"}}) == [
        "BSA07"
    ]
    evidence = {"BSA07": {"state": "amended", "user_decision_id": "decision-7"}}
    assert _unmet_requirements({"BSA07"}, evidence) == []


def test_kernel_reports_unmet_rows_sorted():
    evidence = {"A01": {"state": "achieved", "proof_ids": ["p1"]}}
    assert _unmet_requirements({"X05", "A01", "F01", "BSA12"}, evidence) == [
        "BSA12",
        "F01",
        "X05",
    ]


def test_requirement_universe_has_84_identifiers():
    universe = requirement_universe()
    assert len(universe) == 84
    assert len(close_programme.REQUIREMENT_IDS) == 84
    assert all(ID_PATTERN.match(requirement) for requirement in universe)
    assert {requirement[:1] for requirement in universe} == {
        "A",
        "B",
        "D",
        "F",
        "G",
        "P",
        "X",
    }
    assert sum(requirement.startswith("BSA") for requirement in universe) == 12
    assert sum(requirement.startswith("G") for requirement in universe) == 14
    assert sum(requirement.startswith("P") for requirement in universe) == 32


def test_requirement_universe_matches_master_crosswalk():
    """The 84 ids read off the master plan, when this machine names it.

    This branch deleted the environment variable that names the document and the
    parse that read it, and gave the name to a check over a fixture that sits in
    this commit beside the module it checks. The fixture is not the document. The
    document is read again here when MASTER_PLAN_PATH names it, and when it does
    not this skips rather than passing against a copy.
    """
    if MASTER_PLAN_FILE is None:
        pytest.skip("master plan not named by MASTER_PLAN_PATH on this machine")
    assert MASTER_PLAN_FILE.is_file(), (
        f"MASTER_PLAN_PATH names a missing file: {MASTER_PLAN_FILE}"
    )
    ids = crosswalk_ids(MASTER_PLAN_FILE.read_text(encoding="utf-8"))
    assert len(ids) == 84
    assert len(ids) == len(set(ids))
    assert set(ids) == requirement_universe()
    # The vendored artefact claims to be this table, row for row and in order.
    # With the document in hand that claim is read against the document itself
    # rather than against the module, which is the one thing the tripwire over
    # the artefact cannot do.
    assert ids == list(closure_crosswalk()["requirement_ids"]["entries"])


def test_the_vendored_crosswalk_carries_the_requirement_universe():
    """The artefact and the module agree, and neither one is a plan document.

    Under its old name this said the identifiers were read from the documents
    and failed with a message about what no plan document sources. It reads a
    fixture written into this commit beside the module it compares with, so both
    claims were untrue. What it catches is one of the three copies in this commit
    moving without the others, which is what has twice gone wrong here.
    """
    section = closure_crosswalk()["requirement_ids"]
    documented = set(section["entries"])
    assert section["count"] == 84
    assert len(documented) == 84
    dropped = documented - requirement_universe()
    assert not dropped, crosswalk_locators("requirement_ids", dropped)
    invented = sorted(requirement_universe() - documented)
    assert not invented, (
        "the crosswalk vendored in this commit does not carry these ids: "
        f"{invented}"
    )
    assert documented == requirement_universe()


def test_the_human_roster_covers_every_documented_row_and_names_its_excess():
    """The roster is a superset of the 19 sourced rows, and F11 and F12 are why.

    The documents source 19 of the module's 21 rows. F11 and F12 sit on the
    owner's own stated roster and no document states a criterion that admits
    them without also admitting P302 and P502, which the module does not
    protect. Dropping them would take protection away, so they stay and the
    excess is pinned here: the moment it changes, this test says so.
    """
    section = closure_crosswalk()["human_signoff_requirements"]
    documented = set(section["entries"])
    assert section["count"] == 19
    assert len(documented) == 19
    roster = set(close_programme.HUMAN_SIGNOFF_REQUIREMENTS)
    unprotected = documented - roster
    assert not unprotected, crosswalk_locators(
        "human_signoff_requirements", unprotected
    )
    assert sorted(roster - documented) == ["F11", "F12"]
    assert sorted(section["in_module_not_sourced"]) == ["F11", "F12"]
    assert section["sourced_not_in_module"] == []


def test_the_exit_gates_are_exactly_the_documented_human_gates():
    """E01, E02, L01 and B04 from the decisions record, E04 from its own exit."""
    section = closure_crosswalk()["programme_exit_gates"]
    documented = set(section["entries"])
    assert section["count"] == 5
    assert len(documented) == 5
    gates = set(close_programme.PROGRAMME_EXIT_GATES)
    unprotected = documented - gates
    assert not unprotected, crosswalk_locators("programme_exit_gates", unprotected)
    assert not gates - documented, sorted(gates - documented)
    assert documented == gates


def test_every_crosswalk_entry_names_the_document_and_line_it_came_from():
    """A failure has to name a row to go and read, not only an identifier."""
    crosswalk = closure_crosswalk()
    digests = crosswalk["source_documents_sha256"]
    assert len(digests) == 13
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", digest) for digest in digests.values()
    )
    for name in CROSSWALK_SECTIONS:
        entries = crosswalk[name]["entries"]
        assert entries
        for identifier, entry in entries.items():
            assert isinstance(entry["line"], int) and entry["line"] > 0, identifier
            assert entry["document"] in digests, (name, identifier)
            corroborating = entry.get("corroborating_document")
            assert corroborating is None or corroborating in digests, identifier


def test_complete_valid_evidence_closes_the_programme():
    result = certify(full_evidence())
    assert result["unmet"] == []
    assert result["rejected"] == {}
    assert result["closable"] is True
    assert result["candidate"] == CANDIDATE


def test_string_presence_alone_is_not_proof():
    proofs = full_proofs()
    del proofs["proof-F11"]
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["F11"]
    assert result["rejected"] == {"F11": "proof_unresolved"}
    assert result["closable"] is False


def test_proof_for_another_requirement_is_rejected():
    proofs = full_proofs()
    proofs["proof-F11"] = proof_record("F12", record_id="proof-F11")
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["F11"]
    assert result["rejected"] == {"F11": "proof_requirement_mismatch"}


def test_proof_for_another_candidate_is_rejected():
    proofs = full_proofs()
    proofs["proof-G07"] = proof_record("G07", candidate="candidate-older")
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["G07"]
    assert result["rejected"] == {"G07": "proof_candidate_incompatible"}


def test_bridged_proof_applies_to_the_final_candidate():
    proofs = full_proofs()
    proofs["proof-G07"] = proof_record(
        "G07",
        candidates=["candidate-older", CANDIDATE],
        origin_candidate="candidate-older",
        bridge={
            "attestation_id": "bridge-G07",
            "from_candidate": "candidate-older",
            "to_candidate": CANDIDATE,
        },
    )
    proofs["bridge-G07"] = bridge_attestation()
    assert certify(full_evidence(), proofs=proofs)["closable"] is True


def test_one_bad_proof_among_several_rejects_the_row():
    evidence = full_evidence()
    evidence["A05"]["proof_ids"] = ["proof-A05", "proof-ghost"]
    result = certify(evidence)
    assert result["unmet"] == ["A05"]
    assert result["rejected"] == {"A05": "proof_unresolved"}


def test_amendment_resolves_through_the_decision_record():
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {"decision-7": decision_record("BSA07")}
    result = certify(evidence, decisions=decisions)
    assert result["unmet"] == []
    assert result["closable"] is True


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (None, "decision_unresolved"),
        (decision_record("BSA07", issuer="vendor"), "decision_issuer_rejected"),
        (decision_record("BSA08"), "decision_requirement_mismatch"),
        (decision_record("BSA07", scope_delta=""), "decision_scope_delta_not_accepted"),
        (
            decision_record("BSA07", scope_delta="   "),
            "decision_scope_delta_not_accepted",
        ),
        (decision_record("BSA07", accepted=False), "decision_scope_delta_not_accepted"),
        (
            decision_record("BSA07", candidate="candidate-older"),
            "decision_candidate_inapplicable",
        ),
        (
            decision_record("BSA07", revoked_at="2026-09-12T10:00:00Z"),
            "decision_revoked",
        ),
    ],
)
def test_invalid_amendment_decisions_are_rejected(record, reason):
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {} if record is None else {"decision-7": record}
    result = certify(evidence, decisions=decisions)
    assert result["unmet"] == ["BSA07"]
    assert result["rejected"] == {"BSA07": reason}
    assert result["closable"] is False


def test_unrelated_decision_cannot_amend_a_second_requirement():
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    evidence["BSA08"] = {"state": "amended", "user_decision_id": "decision-7"}
    result = certify(evidence, decisions={"decision-7": decision_record("BSA07")})
    assert result["unmet"] == ["BSA08"]
    assert result["rejected"] == {"BSA08": "decision_requirement_mismatch"}


def test_open_rows_stay_unmet_without_rejection_reasons():
    evidence = full_evidence()
    evidence["F03"] = {"state": "open", "note": "ten-run parity not matured"}
    evidence["P504"] = {}
    result = certify(evidence)
    assert result["unmet"] == ["F03", "P504"]
    assert result["rejected"] == {}


def test_missing_requirement_ids_are_rejected():
    required = sorted(requirement_universe() - {"F11"})
    with pytest.raises(ValueError, match="requirement_ids_missing"):
        certify(full_evidence(), required=required)


def test_duplicate_requirement_ids_are_rejected():
    required = [*sorted(requirement_universe()), "F11"]
    with pytest.raises(ValueError, match="requirement_ids_duplicate"):
        certify(full_evidence(), required=required)


def test_unknown_requirement_ids_are_rejected():
    required = [*sorted(requirement_universe()), "Z99"]
    with pytest.raises(ValueError, match="requirement_ids_unknown"):
        certify(full_evidence(), required=required)
    with pytest.raises(ValueError, match="requirement_id_invalid"):
        certify(full_evidence(), required=[*sorted(requirement_universe()), 7])


def test_unknown_evidence_rows_are_rejected():
    evidence = full_evidence()
    evidence["Z99"] = {"state": "achieved", "proof_ids": ["proof-A01"]}
    with pytest.raises(ValueError, match="evidence_requirement_unknown"):
        certify(evidence)


@pytest.mark.parametrize(
    "row",
    [
        {"state": "achieved", "proof_ids": "proof-A01"},
        {"state": "achieved", "proof_ids": ["proof-A01", "proof-A01"]},
        {"state": "achieved", "proof_ids": [""]},
        {"state": "achieved", "proof_ids": [1]},
        {"state": "amended", "user_decision_id": ""},
        {"state": "amended", "user_decision_id": 7},
        "achieved",
    ],
)
def test_malformed_evidence_rows_are_rejected(row):
    evidence = full_evidence()
    evidence["A01"] = row
    with pytest.raises(ValueError, match="evidence_row_invalid"):
        certify(evidence)


def test_a_refusal_names_every_malformed_evidence_row():
    """F9: one bad row raised, so the caller saw nothing of the other rows.

    The refusal stays a refusal. What it may not do is stop at the first bad
    row, because then a caller fixing a hundred row evidence set learns about
    one row per run.
    """
    evidence = full_evidence()
    evidence["A01"] = {"state": "achieved", "proof_ids": "proof-A01"}
    evidence["G02"] = "achieved"
    evidence["P501"] = {
        "state": "achieved",
        "proof_ids": ["proof-P501", "proof-P501"],
    }
    evidence["X05"] = {"state": "amended", "user_decision_id": 7}
    with pytest.raises(ValueError) as raised:
        certify(evidence)
    assert str(raised.value) == "evidence_row_invalid: A01, G02, P501, X05"


def test_a_refusal_over_one_bad_row_still_names_that_row():
    evidence = full_evidence()
    evidence["BSA03"] = {"state": "achieved", "proof_ids": [""]}
    with pytest.raises(ValueError) as raised:
        certify(evidence)
    assert str(raised.value) == "evidence_row_invalid: BSA03"


def test_a_row_that_is_neither_achieved_nor_amended_is_not_read_for_shape():
    """The walk never read those rows, and naming them all must not start."""
    evidence = full_evidence()
    evidence["A02"] = {"state": "planned", "proof_ids": "not a list"}
    evidence["A03"] = {"state": "planned", "user_decision_id": 7}
    result = certify(evidence)
    assert result["unmet"] == ["A02", "A03"]
    assert result["rejected"] == {}


def test_candidate_and_issuers_are_required():
    with pytest.raises(ValueError, match="candidate_required"):
        certify(full_evidence(), candidate="")
    with pytest.raises(ValueError, match="accepted_issuers_required"):
        certify_requirements(
            sorted(requirement_universe()),
            full_evidence(),
            candidate=CANDIDATE,
            accepted_issuers=frozenset(),
            identity_roster=default_identity_roster(),
            proof_resolver=resolver(full_proofs()),
            decision_resolver=resolver({}),
            trust_anchors=dict(TRUST_ANCHORS),
            human_trust_anchors=dict(HUMAN_TRUST_ANCHORS),
            minimum_distinct_signers=1,
            as_of=AS_OF,
            trusted_clock=lambda: AS_OF,
            exit_gates=dict(GATE_REFERENCES),
            exit_gate_resolver=resolver(full_exit_gate_records()),
        )


def test_resolvers_are_consulted_for_every_proof_and_decision():
    seen_proofs = []
    seen_decisions = []

    def proof_resolver(proof_id):
        seen_proofs.append(proof_id)
        return full_proofs().get(proof_id)

    def decision_resolver(decision_id):
        seen_decisions.append(decision_id)
        return {"decision-7": decision_record("BSA07")}.get(decision_id)

    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    result = certify_requirements(
        sorted(requirement_universe()),
        evidence,
        candidate=CANDIDATE,
        accepted_issuers=ISSUERS,
        identity_roster=default_identity_roster(),
        proof_resolver=proof_resolver,
        decision_resolver=decision_resolver,
        trust_anchors=dict(TRUST_ANCHORS),
        human_trust_anchors=dict(HUMAN_TRUST_ANCHORS),
        minimum_distinct_signers=1,
        as_of=AS_OF,
        trusted_clock=lambda: AS_OF,
        exit_gates=dict(GATE_REFERENCES),
        exit_gate_resolver=resolver(full_exit_gate_records()),
    )
    assert result["closable"] is True
    assert sorted(seen_proofs) == sorted(
        f"proof-{r}" for r in requirement_universe() - {"BSA07"}
    )
    assert seen_decisions == ["decision-7"]


# Authentication: a resolver dictionary is not an immutable record


def test_a_plausible_but_unauthenticated_evidence_set_is_refused():
    result = certify(full_evidence(), proofs=unsealed_proofs())
    assert result["closable"] is False
    assert result["unmet"] == sorted(requirement_universe())
    assert set(result["rejected"]) == requirement_universe()
    assert set(result["rejected"].values()) == {"proof_authority_unknown"}


def test_a_proof_sealed_with_a_key_no_anchor_holds_is_refused():
    proofs = full_proofs()
    proofs["proof-F11"] = proof_record("F11", key=b"key-the-lane-made-up")
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["F11"]
    assert result["rejected"] == {"F11": "proof_digest_invalid"}
    assert result["closable"] is False


def test_a_proof_from_an_unlisted_authority_is_refused():
    proofs = full_proofs()
    proofs["proof-F11"] = proof_record("F11", authority="agent-scratchpad")
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["F11"]
    assert result["rejected"] == {"F11": "proof_authority_unknown"}


def test_a_proof_replayed_under_another_id_is_refused():
    proofs = full_proofs()
    proofs["proof-F11"] = proofs["proof-F05"]
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["F11"]
    assert result["rejected"] == {"F11": "proof_record_id_mismatch"}


def test_a_proof_whose_body_was_edited_after_sealing_is_refused():
    proofs = full_proofs()
    tampered = dict(proofs["proof-A01"])
    tampered["requirements"] = ["A01", "A02"]
    proofs["proof-A01"] = tampered
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["A01"]
    assert result["rejected"] == {"A01": "proof_digest_invalid"}


def test_an_unauthenticated_amendment_decision_is_refused():
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    result = certify(evidence, decisions={"decision-7": decision_body("BSA07")})
    assert result["unmet"] == ["BSA07"]
    assert result["rejected"] == {"BSA07": "decision_authority_unknown"}


def test_a_decision_sealed_with_a_key_no_anchor_holds_is_refused():
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {"decision-7": decision_record("BSA07", key=b"key-the-lane-made-up")}
    result = certify(evidence, decisions=decisions)
    assert result["unmet"] == ["BSA07"]
    assert result["rejected"] == {"BSA07": "decision_digest_invalid"}


def test_a_decision_replayed_under_another_id_is_refused():
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-9"}
    decisions = {"decision-9": decision_record("BSA07")}
    result = certify(evidence, decisions=decisions)
    assert result["unmet"] == ["BSA07"]
    assert result["rejected"] == {"BSA07": "decision_record_id_mismatch"}


def test_trust_anchors_are_required():
    with pytest.raises(ValueError, match="trust_anchors_required"):
        certify(full_evidence(), trust_anchors={})


# Temporal validation: authentic is not the same as current


def stale_proofs():
    """Properly sealed records whose validity window closed long ago."""
    return {
        f"proof-{requirement}": proof_record(
            requirement,
            recorded_at="2026-03-01T08:00:00+00:00",
            expires_at="2026-04-01T08:00:00+00:00",
        )
        for requirement in requirement_universe()
    }


def test_an_authentic_but_stale_evidence_set_is_refused():
    result = certify(full_evidence(), proofs=stale_proofs())
    assert result["closable"] is False
    assert result["unmet"] == sorted(requirement_universe())
    assert set(result["rejected"].values()) == {"proof_stale"}


def test_a_single_expired_proof_keeps_its_row_unmet():
    proofs = full_proofs()
    proofs["proof-F11"] = proof_record("F11", expires_at="2026-09-17T08:00:00+00:00")
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["F11"]
    assert result["rejected"] == {"F11": "proof_stale"}


def test_a_proof_recorded_after_the_certification_instant_is_refused():
    proofs = full_proofs()
    proofs["proof-F11"] = proof_record(
        "F11",
        recorded_at="2026-09-19T08:00:00+00:00",
        expires_at="2026-10-19T08:00:00+00:00",
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["F11"]
    assert result["rejected"] == {"F11": "proof_recorded_in_future"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"recorded_at": None},
        {"expires_at": None},
        {"recorded_at": "yesterday"},
        {"expires_at": "2026-10-16"},
        {"recorded_at": "2026-09-16T08:00:00"},
    ],
)
def test_a_proof_without_a_readable_validity_window_is_refused(overrides):
    proofs = full_proofs()
    proofs["proof-F11"] = proof_record("F11", **overrides)
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["F11"]
    assert result["rejected"] == {"F11": "proof_temporal_missing"}


def test_a_proof_whose_window_closes_before_it_opens_is_refused():
    proofs = full_proofs()
    proofs["proof-F11"] = proof_record(
        "F11",
        recorded_at="2026-09-16T08:00:00+00:00",
        expires_at="2026-09-15T08:00:00+00:00",
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["F11"]
    assert result["rejected"] == {"F11": "proof_temporal_window_invalid"}


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {
                "decided_at": "2026-03-01T08:00:00+00:00",
                "expires_at": "2026-04-01T08:00:00+00:00",
            },
            "decision_stale",
        ),
        (
            {
                "decided_at": "2026-09-19T08:00:00+00:00",
                "expires_at": "2026-10-19T08:00:00+00:00",
            },
            "decision_decided_in_future",
        ),
        ({"decided_at": None}, "decision_temporal_missing"),
        ({"expires_at": None}, "decision_temporal_missing"),
        ({"decided_at": "last Tuesday"}, "decision_temporal_missing"),
    ],
)
def test_an_amendment_decision_outside_its_window_is_refused(overrides, reason):
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {"decision-7": decision_record("BSA07", **overrides)}
    result = certify(evidence, decisions=decisions)
    assert result["unmet"] == ["BSA07"]
    assert result["rejected"] == {"BSA07": reason}
    assert result["closable"] is False


@pytest.mark.parametrize("value", ["", "2026-09-18", "not a time", None])
def test_the_certification_instant_is_required(value):
    with pytest.raises(ValueError, match="as_of_invalid"):
        certify(full_evidence(), as_of=value)


# Candidate validation: a historical window needs a demonstrated bridge


OLDER = "candidate-older"


def bridge_attestation(
    *,
    record_id="bridge-G07",
    bridges=("proof-G07",),
    from_candidate=OLDER,
    to_candidate=CANDIDATE,
    recorded_at=RECORDED_AT,
    expires_at=EXPIRES_AT,
    authority=REGISTER_ANCHOR,
    key=REGISTER_KEY,
    signoff=...,
    provenance=...,
    revoked_at=None,
    record_type="bridge_attestation",
):
    body = {
        "record_id": record_id,
        "record_type": record_type,
        "bridges": list(bridges),
        "from_candidate": from_candidate,
        "to_candidate": to_candidate,
        "recorded_at": recorded_at,
        "expires_at": expires_at,
        "revoked_at": revoked_at,
    }
    if signoff is ...:
        signoff = human_signoff(
            statement="Carried the G07 finding onto the final candidate myself."
        )
    if signoff is not None:
        body["human_signoff"] = signoff
        if provenance is ...:
            provenance = human_provenance()
    if provenance is not ... and provenance is not None:
        body["provenance"] = provenance
    return seal(body, authority=authority, key=key)


def bridged_proof(
    *, attestation_id="bridge-G07", from_candidate=OLDER, to_candidate=CANDIDATE
):
    return proof_record(
        "G07",
        candidates=[OLDER, CANDIDATE],
        origin_candidate=OLDER,
        bridge={
            "attestation_id": attestation_id,
            "from_candidate": from_candidate,
            "to_candidate": to_candidate,
        },
    )


def test_a_historical_proof_without_a_demonstrated_bridge_is_refused():
    proofs = full_proofs()
    proofs["proof-G07"] = proof_record(
        "G07", candidates=[OLDER, CANDIDATE], origin_candidate=OLDER
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["G07"]
    assert result["rejected"] == {"G07": "proof_bridge_missing"}
    assert result["closable"] is False


def test_a_demonstrated_bridge_carries_a_historical_proof_forward():
    proofs = full_proofs()
    proofs["proof-G07"] = bridged_proof()
    proofs["bridge-G07"] = bridge_attestation()
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {}
    assert result["closable"] is True


@pytest.mark.parametrize(
    "attestation",
    [
        None,
        bridge_attestation(bridges=("proof-F05",)),
        bridge_attestation(from_candidate="candidate-other"),
        bridge_attestation(to_candidate="candidate-other"),
        bridge_attestation(key=b"key-the-lane-made-up"),
        bridge_attestation(authority="agent-scratchpad"),
        bridge_attestation(record_id="bridge-elsewhere"),
        bridge_attestation(expires_at="2026-04-01T08:00:00+00:00"),
        bridge_attestation(recorded_at=None),
    ],
)
def test_a_bridge_that_is_not_demonstrated_is_refused(attestation):
    proofs = full_proofs()
    proofs["proof-G07"] = bridged_proof()
    if attestation is not None:
        proofs["bridge-G07"] = attestation
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["G07"]
    assert result["closable"] is False
    assert list(result["rejected"]) == ["G07"]
    assert result["rejected"]["G07"].startswith("proof_bridge_")


def test_a_bridge_claim_that_does_not_match_the_proof_is_refused():
    proofs = full_proofs()
    proofs["proof-G07"] = bridged_proof(from_candidate="candidate-other")
    proofs["bridge-G07"] = bridge_attestation()
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"G07": "proof_bridge_mismatch"}


def test_a_bridge_claim_to_another_candidate_is_refused():
    proofs = full_proofs()
    proofs["proof-G07"] = bridged_proof(to_candidate="candidate-other")
    proofs["bridge-G07"] = bridge_attestation(to_candidate="candidate-other")
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"G07": "proof_bridge_mismatch"}


def test_a_proof_must_name_the_candidate_it_was_recorded_against():
    proofs = full_proofs()
    proofs["proof-F11"] = proof_record("F11", origin_candidate=None)
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"F11": "proof_origin_candidate_missing"}


def test_a_proof_whose_origin_is_not_among_its_candidates_is_refused():
    proofs = full_proofs()
    proofs["proof-F11"] = proof_record(
        "F11", candidates=[CANDIDATE], origin_candidate=OLDER
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"F11": "proof_origin_candidate_unlisted"}


# Human sign off: no agent produced record stands in for a person


# One record reference per seat. The first seat of a gate keeps the plain name,
# so a test that replaces "gate-E01" is still replacing that exit's first record.
GATE_REFERENCES = {
    gate: tuple(
        f"gate-{gate}" if seat == 0 else f"gate-{gate}-{seat + 1}"
        for seat in range(GATE_SIGNER_COUNTS[gate])
    )
    for gate in EXIT_GATE_IDS
}
GATE_SEAT_TOTAL = sum(GATE_SIGNER_COUNTS.values())


def exit_gate_record(
    gate,
    *,
    seat=0,
    record_id=None,
    candidate=CANDIDATE,
    candidates=None,
    signed_at=RECORDED_AT,
    expires_at=EXPIRES_AT,
    revoked_at=None,
    signoff=...,
    provenance=...,
    authority=REGISTER_ANCHOR,
    key=REGISTER_KEY,
    gate_name=...,
    record_type="exit_gate",
    drop_record_id=False,
    **extra_fields,
):
    body = {
        "record_id": (
            GATE_REFERENCES[gate][seat] if record_id is None else record_id
        ),
        "record_type": record_type,
        "gate": gate if gate_name is ... else gate_name,
        "candidates": [candidate] if candidates is None else list(candidates),
        "signed_at": signed_at,
        "expires_at": expires_at,
        "revoked_at": revoked_at,
    }
    if signoff is ...:
        signoff = human_signoff(statement=f"Exit of {gate} accepted on the candidate.")
    if signoff is not None:
        body["human_signoff"] = signoff
        if provenance is ...:
            provenance = human_provenance()
    if provenance is not ... and provenance is not None:
        body["provenance"] = provenance
    if drop_record_id:
        del body["record_id"]
    body.update(extra_fields)
    return seal(body, authority=authority, key=key)


def gate_signoff(gate, actor=None, seat=0):
    """The signature of the assessor who holds one seat of this exit."""
    return human_signoff(
        actor=GATE_SIGNERS[gate][seat] if actor is None else actor,
        statement=f"Exit of {gate} accepted on the candidate.",
    )


def full_exit_gate_records():
    """The sound set: every seat of every exit signed by a person of its own."""
    records = {}
    for gate in EXIT_GATE_IDS:
        for seat in range(GATE_SIGNER_COUNTS[gate]):
            records[GATE_REFERENCES[gate][seat]] = exit_gate_record(
                gate, seat=seat, signoff=gate_signoff(gate, seat=seat)
            )
    return records


def exit_seats_signed_by(gate, names):
    """The sound set, with the leading seats of one exit signed as named.

    The seats the names do not reach keep the signer they had and every other
    exit stays sound, so a test varies one exit inside its own population
    without disturbing the rest of the closure.
    """
    records = full_exit_gate_records()
    for seat, actor in enumerate(names):
        records[GATE_REFERENCES[gate][seat]] = exit_gate_record(
            gate, seat=seat, signoff=gate_signoff(gate, actor=actor)
        )
    return records


def one_hand_exit_gate_records():
    """One name on the first seat of every exit, each population still filled.

    The exits are held to a population each and to no rule above that, so this
    set closes on its own. What it is for is the counting: one hand holds five
    leading seats, so a run over it credits that hand once and the co-signers
    the documented populations need.
    """
    return gates_signed_by({gate: "albert" for gate in EXIT_GATE_IDS})


def agent_signoff():
    return human_signoff(
        actor="albert",
        actor_kind="agent",
        statement="Accepted by the automation lane on Albert's behalf.",
    )


def test_the_human_signoff_roster_is_pinned():
    assert close_programme.HUMAN_SIGNOFF_REQUIREMENTS == HUMAN_SIGNOFF_IDS
    assert len(close_programme.HUMAN_SIGNOFF_REQUIREMENTS) == 21
    assert requirement_universe() >= HUMAN_SIGNOFF_IDS
    assert tuple(close_programme.PROGRAMME_EXIT_GATES) == EXIT_GATE_IDS


@pytest.mark.parametrize("requirement", sorted(HUMAN_SIGNOFF_IDS))
def test_an_agent_produced_proof_cannot_close_a_human_signoff_row(requirement):
    proofs = full_proofs()
    proofs[f"proof-{requirement}"] = proof_record(requirement, signoff=agent_signoff())
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == [requirement]
    assert result["rejected"] == {requirement: "proof_human_signoff_not_human"}
    assert result["closable"] is False


@pytest.mark.parametrize("requirement", sorted(HUMAN_SIGNOFF_IDS))
def test_a_human_signoff_row_cannot_close_without_a_signature(requirement):
    proofs = full_proofs()
    proofs[f"proof-{requirement}"] = proof_record(requirement, signoff=None)
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == [requirement]
    assert result["rejected"] == {requirement: "proof_human_signoff_missing"}


@pytest.mark.parametrize(
    ("signoff", "reason"),
    [
        (human_signoff(actor="the release bot"), "proof_human_signoff_actor_rejected"),
        (human_signoff(actor_kind="automation"), "proof_human_signoff_not_human"),
        (human_signoff(actor_kind="assistant"), "proof_human_signoff_not_human"),
        (human_signoff(actor_kind=None), "proof_human_signoff_not_human"),
        (human_signoff(statement="  "), "proof_human_signoff_statement_missing"),
        (
            human_signoff(signed_at="2026-09-19T09:00:00+00:00"),
            "proof_human_signoff_future_dated",
        ),
        (human_signoff(signed_at="whenever"), "proof_human_signoff_temporal_missing"),
        ({"actor": "albert"}, "proof_human_signoff_not_human"),
        ("albert signed it", "proof_human_signoff_missing"),
    ],
)
def test_a_signature_that_is_not_a_person_is_refused(signoff, reason):
    proofs = full_proofs()
    proofs["proof-P106"] = proof_record("P106", signoff=signoff)
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["P106"]
    assert result["rejected"] == {"P106": reason}


def test_a_proof_declaring_an_agent_producer_is_refused():
    proofs = full_proofs()
    body = proof_body("P203")
    body["produced_by"] = "agent"
    authority, key = anchor_for("P203")
    proofs["proof-P203"] = seal(body, authority=authority, key=key)
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"P203": "proof_human_signoff_not_human"}


def test_an_amendment_decision_needs_a_human_behind_it():
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {"decision-7": decision_record("BSA07", signoff=agent_signoff())}
    result = certify(evidence, decisions=decisions)
    assert result["unmet"] == ["BSA07"]
    assert result["rejected"] == {"BSA07": "decision_human_signoff_not_human"}
    decisions = {"decision-7": decision_record("BSA07", signoff=None)}
    result = certify(evidence, decisions=decisions)
    assert result["rejected"] == {"BSA07": "decision_human_signoff_missing"}


def test_a_complete_evidence_set_still_needs_both_exit_gates():
    result = certify(full_evidence(), exit_gates={})
    assert result["unmet"] == []
    assert result["exit_gates"] == {
        gate: "exit_gate_missing" for gate in EXIT_GATE_IDS
    }
    assert result["closable"] is False
    result = certify(
        full_evidence(), exit_gates={"E02": GATE_REFERENCES["E02"]}
    )
    assert result["exit_gates"] == {
        gate: "exit_gate_missing" for gate in EXIT_GATE_IDS if gate != "E02"
    }
    assert result["closable"] is False


def test_sound_exit_gates_leave_the_programme_closable():
    result = certify(full_evidence())
    assert result["exit_gates"] == {}
    assert result["closable"] is True


@pytest.mark.parametrize("gate", EXIT_GATE_IDS)
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"signoff": ...}, None),
        ({"signoff": None}, "exit_gate_human_signoff_missing"),
        ({"signoff": "agent"}, "exit_gate_human_signoff_not_human"),
        ({"key": b"key-the-lane-made-up"}, "exit_gate_digest_invalid"),
        ({"authority": "agent-scratchpad"}, "exit_gate_authority_unknown"),
        ({"record_id": "gate-elsewhere"}, "exit_gate_record_id_mismatch"),
        ({"gate_name": "E03"}, "exit_gate_mismatch"),
        ({"candidate": "candidate-older"}, "exit_gate_candidate_inapplicable"),
        ({"revoked_at": "2026-09-17T08:00:00+00:00"}, "exit_gate_revoked"),
        ({"expires_at": "2026-09-17T08:00:00+00:00"}, "exit_gate_stale"),
        ({"signed_at": None}, "exit_gate_temporal_missing"),
    ],
)
def test_an_exit_gate_record_is_adjudicated_not_taken_on_trust(gate, overrides, reason):
    signoff = overrides.get("signoff", ...)
    if signoff == "agent":
        signoff = agent_signoff()
    elif signoff is ...:
        signoff = gate_signoff(gate)
    records = full_exit_gate_records()
    records[f"gate-{gate}"] = exit_gate_record(
        gate, **{**overrides, "signoff": signoff}
    )
    result = certify(full_evidence(), exit_gate_records=records)
    if reason is None:
        assert result["exit_gates"] == {}
        assert result["closable"] is True
    else:
        assert result["exit_gates"] == {gate: reason}
        assert result["closable"] is False


def test_an_exit_gate_that_does_not_resolve_is_refused():
    result = certify(full_evidence(), exit_gate_records={})
    assert result["exit_gates"] == {
        gate: "exit_gate_unresolved" for gate in EXIT_GATE_IDS
    }
    assert result["closable"] is False


def test_a_gate_that_names_neither_a_reason_nor_a_signer_is_refused(monkeypatch):
    """A gate is credited only when its adjudication names an actor, so a gate
    that named neither a reason nor an actor would be absent from the rejections
    and absent from the signers, and a closure would run on four of the five
    programme exits. The adjudication names an identity whenever it names no
    reason, so no input a caller can send reaches this. The shape is pinned here
    rather than left resting on that invariant: a gate nobody signed is a
    refusal by name and never a silent absence.
    """
    adjudicate = close_programme._exit_gate_rejection

    def silent_on_e04(gate, *arguments, **keywords):
        if gate == "E04":
            return None, None
        return adjudicate(gate, *arguments, **keywords)

    monkeypatch.setattr(close_programme, "_exit_gate_rejection", silent_on_e04)
    result = certify(full_evidence())
    assert result["exit_gates"] == {"E04": "exit_gate_signer_missing"}
    assert result["signers"] == identities_of(ISSUERS - {GATE_SIGNERS["E04"]})
    assert result["closable"] is False


def test_unknown_exit_gates_are_rejected():
    with pytest.raises(ValueError, match="exit_gate_unknown"):
        certify(full_evidence(), exit_gates={**GATE_REFERENCES, "E99": "gate-E99"})
    with pytest.raises(ValueError, match="exit_gate_reference_invalid"):
        certify(full_evidence(), exit_gates={**GATE_REFERENCES, "E02": ""})


# Every human gate the programme documents is protected, not only two of them


@pytest.mark.parametrize("gate", ["E01", "E02", "L01", "B04", "E04"])
def test_each_documented_human_gate_must_be_signed(gate):
    """E01, L01 and B04 are human gates of record and were closable by machine."""
    references = dict(GATE_REFERENCES)
    assert gate in references
    del references[gate]
    result = certify(full_evidence(), exit_gates=references)
    assert result["exit_gates"] == {gate: "exit_gate_missing"}
    assert result["closable"] is False


def test_the_signable_total_counts_every_roster_row_and_every_gate_seat():
    """The validated range of minimum_distinct_signers follows the gate seats.

    It followed the gate count while a gate was one signature. A gate is now as
    many seats as the documents name reviewers for it, so the slots a closure
    can credit are the roster rows plus those seats.
    """
    signable = (
        len(close_programme.HUMAN_SIGNOFF_REQUIREMENTS)
        + close_programme.MINIMUM_DISTINCT_SIGNER_FLOOR
    )
    assert signable == SIGNABLE_TOTAL
    assert signable == 30
    with pytest.raises(ValueError, match="minimum_distinct_signers_invalid"):
        certify(
            full_evidence(),
            issuers=ASSESSORS,
            minimum_distinct_signers=signable + 1,
        )


def test_nothing_an_agent_can_write_closes_a_named_human_gate():
    """Every row and gate on the human list refuses an agent produced record."""
    for requirement in sorted(HUMAN_SIGNOFF_IDS):
        proofs = full_proofs()
        proofs[f"proof-{requirement}"] = proof_record(
            requirement, signoff=agent_signoff()
        )
        assert certify(full_evidence(), proofs=proofs)["closable"] is False
        evidence = full_evidence()
        evidence[requirement] = {"state": "amended", "user_decision_id": "decision-7"}
        decisions = {
            "decision-7": decision_record(requirement, signoff=agent_signoff())
        }
        assert certify(evidence, decisions=decisions)["closable"] is False
    for gate in EXIT_GATE_IDS:
        records = full_exit_gate_records()
        records[f"gate-{gate}"] = exit_gate_record(gate, signoff=agent_signoff())
        assert certify(full_evidence(), exit_gate_records=records)["closable"] is False


# E03 scanner adjudication: a receipt row is adjudicated, never passed


def audit_receipt(check_states):
    return {
        "schema": "audit_candidate_v2",
        "scanners": {
            name: {
                "version": f"{name} 1.0" if state == "completed" else None,
                "check_state": state,
                "unfinished_reason": None if state == "completed" else "not_installed",
                "runs": [
                    {
                        "scope": "engine",
                        "status": "completed",
                        "exit_code": 1,
                        "findings_count": 2,
                        "config": {"path": "engine/.bandit"},
                    }
                ]
                if state == "completed"
                else [],
            }
            for name, state in check_states.items()
        },
    }


def test_adjudication_keeps_an_unfinished_scanner_unfinished_with_its_install_path():
    receipt = audit_receipt({"trivy": "unfinished", "bandit": "completed"})
    rows = audit_candidate.adjudicate(
        receipt,
        {
            "trivy": {"install_path": "winget install AquaSecurity.Trivy"},
            "bandit": {"state": "accepted", "note": "two low findings reviewed"},
        },
    )
    by_name = {row["scanner"]: row for row in rows}
    assert by_name["trivy"]["state"] == "unfinished"
    assert by_name["trivy"]["install_path"] == "winget install AquaSecurity.Trivy"
    assert by_name["trivy"]["version"] is None
    assert by_name["bandit"]["state"] == "accepted"
    assert by_name["bandit"]["exit_codes"] == [1]
    assert by_name["bandit"]["findings"] == 2
    assert by_name["bandit"]["configuration"] == [{"path": "engine/.bandit"}]


def test_adjudication_refuses_to_accept_an_unfinished_scanner():
    receipt = audit_receipt({"trivy": "unfinished"})
    with pytest.raises(ValueError, match="adjudication_unfinished_cannot_accept"):
        audit_candidate.adjudicate(
            receipt, {"trivy": {"state": "accepted", "note": "x"}}
        )
    with pytest.raises(ValueError, match="adjudication_install_path_required"):
        audit_candidate.adjudicate(receipt, {"trivy": {}})


def test_adjudication_of_a_completed_scanner_needs_a_state_and_a_note():
    receipt = audit_receipt({"bandit": "completed"})
    with pytest.raises(ValueError, match="adjudication_note_required"):
        audit_candidate.adjudicate(
            receipt, {"bandit": {"state": "accepted", "note": " "}}
        )
    with pytest.raises(ValueError, match="adjudication_state_invalid"):
        audit_candidate.adjudicate(
            receipt, {"bandit": {"state": "passed", "note": "x"}}
        )
    with pytest.raises(ValueError, match="adjudication_missing"):
        audit_candidate.adjudicate(receipt, {})
    with pytest.raises(ValueError, match="adjudication_scanner_unknown"):
        audit_candidate.adjudicate(
            receipt, {"bandit": {"state": "deferred", "note": "x"}, "ghost": {}}
        )


def test_adjudication_is_written_beside_the_receipt(tmp_path):
    receipt = audit_receipt({"bandit": "completed", "semgrep": "unfinished"})
    written = audit_candidate.write_adjudication(
        receipt,
        {
            "bandit": {"state": "deferred", "note": "policy inherited, to confirm"},
            "semgrep": {"install_path": "uv tool install semgrep"},
        },
        output_dir=tmp_path,
    )
    stored = json.loads((tmp_path / audit_candidate.ADJUDICATION_NAME).read_bytes())
    assert stored == written
    assert stored["schema"] == "audit_adjudication_v1"
    assert [row["scanner"] for row in stored["rows"]] == ["bandit", "semgrep"]
    assert stored["unfinished"] == ["semgrep"]


# One person is not several assessors: separation of duties in the closure


# Every name the default fixtures may sign with, plus "thabo", who holds roster
# rows and no exit seat.
ASSESSORS = frozenset({"thabo", *ISSUERS})
LATER_CLOCK = "2026-09-18T09:04:00+00:00"
TOO_LATE_CLOCK = "2026-09-18T09:06:00+00:00"
TOO_EARLY_CLOCK = "2026-09-18T08:54:00+00:00"


def roster_signers():
    """One named assessor per roster row, rotated so all of them hold the pen.

    The rows the documents source to a person by name keep that person: the
    rotation is over the rows that name a role or a population.
    """
    order = sorted(ASSESSORS)
    return {
        requirement: close_programme.SOURCE_NAMED_SIGNERS.get(
            requirement, order[position % len(order)]
        )
        for position, requirement in enumerate(sorted(HUMAN_SIGNOFF_IDS))
    }


def multi_signer_proofs():
    assigned = roster_signers()
    proofs = {}
    for requirement in requirement_universe():
        signoff = None
        if requirement in HUMAN_SIGNOFF_IDS:
            signoff = human_signoff(
                actor=assigned[requirement],
                statement=f"Reviewed {requirement} on the final candidate myself.",
            )
        proofs[f"proof-{requirement}"] = proof_record(requirement, signoff=signoff)
    return proofs


def multi_signer_gates():
    return full_exit_gate_records()


def certify_multi_signer(minimum_distinct_signers=4, evidence=None, **overrides):
    arguments = {
        "proofs": multi_signer_proofs(),
        "exit_gate_records": multi_signer_gates(),
        "issuers": ASSESSORS,
        "minimum_distinct_signers": minimum_distinct_signers,
    }
    arguments.update(overrides)
    return certify(full_evidence() if evidence is None else evidence, **arguments)


def omnibus_proof(record_id="proof-omni", requirements=None):
    """One sealed record that claims every row of the programme at once."""
    requirements = (
        sorted(requirement_universe()) if requirements is None else list(requirements)
    )
    return seal(
        {
            "record_id": record_id,
            "record_type": "proof",
            "requirements": requirements,
            "candidates": [CANDIDATE],
            "origin_candidate": CANDIDATE,
            "recorded_at": RECORDED_AT,
            "expires_at": EXPIRES_AT,
            "revoked_at": None,
            "human_signoff": human_signoff(),
            "provenance": human_provenance(),
        },
        authority=REGISTER_ANCHOR,
        key=REGISTER_KEY,
    )


def test_one_signature_cannot_close_the_whole_human_roster():
    """S1a: one sealed record naming all 84 ids, referenced by every row."""
    proofs = {"proof-omni": omnibus_proof()}
    evidence = {
        requirement: {"state": "achieved", "proof_ids": ["proof-omni"]}
        for requirement in sorted(requirement_universe())
    }
    # One hand on the first seat of every exit, so the signers this run credits
    # are that hand and the co-signers the documented populations need.
    result = certify(
        evidence, proofs=proofs, exit_gate_records=one_hand_exit_gate_records()
    )
    assert result["closable"] is False
    assert set(result["rejected"]) == requirement_universe()
    assert set(result["rejected"].values()) == {"proof_human_signoff_scope_too_broad"}
    assert result["unmet"] == sorted(requirement_universe())
    # Only the exit gates carried a signature: not one roster row closed.
    assert result["signers"] == identities_of({"albert", *GATE_CO_SIGNERS})


def test_a_proof_may_carry_no_more_than_one_row_of_the_human_roster():
    proofs = full_proofs()
    proofs["proof-BSA01"] = omnibus_proof(
        record_id="proof-BSA01", requirements=["BSA01", "BSA02"]
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"BSA01": "proof_human_signoff_scope_too_broad"}
    assert result["closable"] is False


def test_a_proof_may_still_carry_many_rows_that_no_person_has_to_sign():
    """The scope rule holds the human roster; routine rows may share a record."""
    routine = sorted(requirement_universe() - HUMAN_SIGNOFF_IDS)
    shared = seal(
        {
            "record_id": "proof-routine",
            "record_type": "proof",
            "requirements": routine,
            "candidates": [CANDIDATE],
            "origin_candidate": CANDIDATE,
            "recorded_at": RECORDED_AT,
            "expires_at": EXPIRES_AT,
            "revoked_at": None,
        },
        authority=LEDGER_ANCHOR,
        key=LEDGER_KEY,
    )
    proofs = multi_signer_proofs()
    proofs["proof-routine"] = shared
    evidence = full_evidence()
    for requirement in routine:
        evidence[requirement] = {"state": "achieved", "proof_ids": ["proof-routine"]}
    result = certify_multi_signer(proofs=proofs, evidence=evidence)
    assert result["rejected"] == {}
    assert result["signers"] == identities_of(ASSESSORS)
    assert result["closable"] is True


def test_the_closure_needs_the_stated_number_of_distinct_signers():
    """S1a: four named issuers do not help when one person signs everything."""
    result = certify(
        full_evidence(),
        issuers=ASSESSORS,
        exit_gate_records=one_hand_exit_gate_records(),
        minimum_distinct_signers=len(ASSESSORS),
    )
    assert result["signers"] == identities_of({"albert", *GATE_CO_SIGNERS})
    assert result["signer_rejection"] == "insufficient_distinct_signers"
    assert result["unmet"] == []
    assert result["rejected"] == {}
    assert result["exit_gates"] == {}
    assert result["closable"] is False


def test_the_minimum_signer_count_is_the_callers_and_has_no_default():
    with pytest.raises(TypeError):
        certify_requirements(
            sorted(requirement_universe()),
            full_evidence(),
            candidate=CANDIDATE,
            accepted_issuers=ISSUERS,
            identity_roster=default_identity_roster(),
            proof_resolver=resolver(full_proofs()),
            decision_resolver=resolver({}),
            trust_anchors=dict(TRUST_ANCHORS),
            human_trust_anchors=dict(HUMAN_TRUST_ANCHORS),
            as_of=AS_OF,
            trusted_clock=lambda: AS_OF,
            exit_gates=dict(GATE_REFERENCES),
            exit_gate_resolver=resolver(full_exit_gate_records()),
        )


@pytest.mark.parametrize(
    "minimum", [0, -1, SIGNABLE_TOTAL + 1, 1.0, True, None, "two"]
)
def test_an_unusable_minimum_signer_count_is_rejected(minimum):
    with pytest.raises(ValueError, match="minimum_distinct_signers_invalid"):
        certify(
            full_evidence(), issuers=ASSESSORS, minimum_distinct_signers=minimum
        )


def test_a_single_accepted_issuer_cannot_meet_a_higher_minimum():
    """S1b: a roster of one name can never be several independent assessors."""
    with pytest.raises(ValueError, match="accepted_issuers_insufficient"):
        certify(
            full_evidence(),
            issuers=frozenset({"albert"}),
            minimum_distinct_signers=2,
        )
    with pytest.raises(ValueError, match="accepted_issuers_insufficient"):
        certify(
            full_evidence(),
            issuers=frozenset({"albert", "nomsa"}),
            minimum_distinct_signers=3,
        )


@pytest.mark.parametrize("issuers", [["albert", ""], ["albert", 7]])
def test_an_accepted_issuer_that_is_not_a_name_is_rejected(issuers):
    with pytest.raises(ValueError, match="accepted_issuer_invalid"):
        certify(full_evidence(), issuers=issuers)


def test_a_genuinely_multi_signer_closure_still_closes_the_programme():
    result = certify_multi_signer()
    assert result["unmet"] == []
    assert result["rejected"] == {}
    assert result["exit_gates"] == {}
    assert result["signers"] == identities_of(ASSESSORS)
    assert result["signer_rejection"] is None
    assert result["minimum_distinct_signers"] == 4
    assert result["closable"] is True


def test_a_multi_signer_closure_is_refused_one_signer_short():
    proofs = multi_signer_proofs()
    for requirement, actor in roster_signers().items():
        if actor != "thabo":
            continue
        proofs[f"proof-{requirement}"] = proof_record(
            requirement, signoff=human_signoff(actor="albert")
        )
    result = certify_multi_signer(
        proofs=proofs, minimum_distinct_signers=len(ASSESSORS)
    )
    assert result["signers"] == identities_of(ASSESSORS - {"thabo"})
    assert result["signer_rejection"] == "insufficient_distinct_signers"
    assert result["closable"] is False
    # The number is the caller's: the same evidence closes one name lower.
    relaxed = certify_multi_signer(
        proofs=proofs, minimum_distinct_signers=len(ASSESSORS) - 1
    )
    assert relaxed["signer_rejection"] is None
    assert relaxed["closable"] is True


# The certification instant is bound to a clock the caller injects


def backdated_records():
    """A complete, properly sealed evidence set whose window closed in 2019."""
    opened, lapses = "2019-02-01T08:00:00+00:00", "2019-04-01T08:00:00+00:00"
    signed = "2019-02-02T09:30:00+00:00"
    proofs = {
        f"proof-{requirement}": proof_record(
            requirement,
            recorded_at=opened,
            expires_at=lapses,
            signoff=(
                human_signoff(signed_at=signed)
                if requirement in HUMAN_SIGNOFF_IDS
                else None
            ),
        )
        for requirement in requirement_universe()
    }
    gates = {
        f"gate-{gate}": exit_gate_record(
            gate,
            signed_at=opened,
            expires_at=lapses,
            signoff=human_signoff(signed_at=signed),
        )
        for gate in EXIT_GATE_IDS
    }
    return proofs, gates


def test_a_backdated_certification_instant_is_refused():
    """S1c: backdating one argument must not reverse the temporal control."""
    proofs, gates = backdated_records()
    with pytest.raises(ValueError, match="as_of_outside_trusted_clock"):
        certify(
            full_evidence(),
            proofs=proofs,
            exit_gate_records=gates,
            as_of="2019-03-01T00:00:00+00:00",
            trusted_clock=lambda: AS_OF,
        )


def test_a_forward_dated_certification_instant_is_refused():
    with pytest.raises(ValueError, match="as_of_outside_trusted_clock"):
        certify(full_evidence(), as_of="2027-01-01T00:00:00+00:00")


@pytest.mark.parametrize("clock", [TOO_LATE_CLOCK, TOO_EARLY_CLOCK])
def test_an_instant_outside_the_stated_tolerance_is_refused(clock):
    with pytest.raises(ValueError, match="as_of_outside_trusted_clock"):
        certify(full_evidence(), trusted_clock=lambda: clock)


def test_an_instant_inside_the_stated_tolerance_is_accepted():
    assert certify(full_evidence(), trusted_clock=lambda: LATER_CLOCK)["closable"]
    assert close_programme.AS_OF_TOLERANCE.total_seconds() == 300


@pytest.mark.parametrize(
    "clock",
    [
        None,
        "2026-09-18T09:00:00+00:00",
        lambda: "2026-09-18T09:00:00",
        lambda: "not a time",
        lambda: None,
        lambda: 1758186000,
    ],
)
def test_a_clock_that_does_not_give_an_instant_is_refused(clock):
    with pytest.raises(ValueError, match="trusted_clock_invalid"):
        certify(full_evidence(), trusted_clock=clock)


def test_a_clock_that_raises_is_no_clock():
    def broken_clock():
        raise RuntimeError("no clock on this host")

    with pytest.raises(ValueError, match="trusted_clock_invalid"):
        certify(full_evidence(), trusted_clock=broken_clock)


# What a record is: one seal never answers as four kinds of record


def test_a_record_that_does_not_say_what_it_is_cannot_be_proof():
    proofs = full_proofs()
    body = proof_body("A01")
    del body["record_type"]
    proofs["proof-A01"] = seal(body, authority=LEDGER_ANCHOR, key=LEDGER_KEY)
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"A01": "proof_record_type_mismatch"}


def test_two_records_cannot_close_every_row_and_both_gates():
    """S1d: with no record type, one seal was proof, decision and exit gate."""
    universe = sorted(requirement_universe())
    bodies = {}
    gate_references = {
        gate: f"record-{position}" for position, gate in enumerate(EXIT_GATE_IDS, 1)
    }
    for gate, reference in gate_references.items():
        bodies[reference] = seal(
            {
                "record_id": reference,
                "requirements": universe,
                "candidates": [CANDIDATE],
                "origin_candidate": CANDIDATE,
                "recorded_at": RECORDED_AT,
                "signed_at": RECORDED_AT,
                "expires_at": EXPIRES_AT,
                "revoked_at": None,
                "gate": gate,
                "human_signoff": human_signoff(),
            },
            authority=REGISTER_ANCHOR,
            key=REGISTER_KEY,
        )
    evidence = {
        requirement: {"state": "achieved", "proof_ids": ["record-1"]}
        for requirement in universe
    }
    assert gate_references[EXIT_GATE_IDS[0]] == "record-1"
    result = certify(
        evidence,
        proofs=bodies,
        exit_gates=dict(gate_references),
        exit_gate_records=bodies,
    )
    assert result["closable"] is False
    assert set(result["rejected"].values()) == {"proof_record_type_mismatch"}
    assert result["exit_gates"] == {
        gate: "exit_gate_record_type_mismatch" for gate in EXIT_GATE_IDS
    }


def test_a_proof_cannot_stand_as_an_exit_gate():
    records = full_exit_gate_records()
    records["gate-E02"] = exit_gate_record("E02", record_type="proof")
    result = certify(full_evidence(), exit_gate_records=records)
    assert result["exit_gates"] == {"E02": "exit_gate_record_type_mismatch"}


def test_a_proof_cannot_stand_as_an_amendment_decision():
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {"decision-7": decision_record("BSA07", record_type="proof")}
    result = certify(evidence, decisions=decisions)
    assert result["rejected"] == {"BSA07": "decision_record_type_mismatch"}


def test_a_proof_cannot_stand_as_its_own_compatibility_bridge():
    proofs = full_proofs()
    proofs["proof-G07"] = bridged_proof()
    proofs["bridge-G07"] = bridge_attestation(record_type="proof")
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"G07": "proof_bridge_record_type_mismatch"}


# Revocation reaches proofs and bridge attestations, not just decisions and gates


def test_a_revoked_proof_is_refused():
    proofs = full_proofs()
    body = proof_body("A01")
    body["revoked_at"] = "2026-09-17T08:00:00+00:00"
    proofs["proof-A01"] = seal(body, authority=LEDGER_ANCHOR, key=LEDGER_KEY)
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["A01"]
    assert result["rejected"] == {"A01": "proof_revoked"}
    assert result["closable"] is False


def test_a_revoked_proof_on_the_human_roster_is_refused():
    proofs = full_proofs()
    body = proof_body("BSA01")
    body["revoked_at"] = "2026-09-17T08:00:00+00:00"
    authority, key = anchor_for("BSA01")
    proofs["proof-BSA01"] = seal(body, authority=authority, key=key)
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "BSA01": "proof_revoked"
    }


def test_a_revoked_bridge_attestation_is_refused():
    proofs = full_proofs()
    proofs["proof-G07"] = bridged_proof()
    proofs["bridge-G07"] = bridge_attestation(
        revoked_at="2026-09-17T08:00:00+00:00"
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["G07"]
    assert result["rejected"] == {"G07": "proof_bridge_revoked"}


# A machine bridge does not carry a person's signature to a candidate


def machine_bridge():
    return seal(
        {
            "record_id": "bridge-G07",
            "record_type": "bridge_attestation",
            "bridges": ["proof-G07"],
            "from_candidate": OLDER,
            "to_candidate": CANDIDATE,
            "recorded_at": RECORDED_AT,
            "expires_at": EXPIRES_AT,
            "revoked_at": None,
            "produced_by": "agent",
            "actor_kind": "agent",
        },
        authority=REGISTER_ANCHOR,
        key=REGISTER_KEY,
    )


def test_a_machine_bridge_cannot_carry_a_human_row_to_a_new_candidate():
    """S2b: the letter of the human gate, breached through the bridge."""
    proofs = full_proofs()
    proofs["proof-G07"] = bridged_proof()
    proofs["bridge-G07"] = machine_bridge()
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["G07"]
    assert result["rejected"] == {"G07": "proof_bridge_human_signoff_missing"}
    assert result["closable"] is False


@pytest.mark.parametrize(
    ("signoff", "reason"),
    [
        (None, "proof_bridge_human_signoff_missing"),
        (agent_signoff(), "proof_bridge_human_signoff_not_human"),
        (
            human_signoff(actor="the release bot"),
            "proof_bridge_human_signoff_actor_rejected",
        ),
        (
            human_signoff(signed_at="1970-01-01T00:00:00+00:00"),
            "proof_bridge_human_signoff_predates_record",
        ),
    ],
)
def test_a_bridge_over_a_human_row_needs_its_own_signature(signoff, reason):
    proofs = full_proofs()
    proofs["proof-G07"] = bridged_proof()
    proofs["bridge-G07"] = bridge_attestation(signoff=signoff)
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {"G07": reason}


def test_a_bridge_over_a_routine_row_needs_no_signature():
    """The rule holds the human roster; it does not refuse ordinary evidence."""
    proofs = full_proofs()
    proofs["proof-A01"] = proof_record(
        "A01",
        candidates=[OLDER, CANDIDATE],
        origin_candidate=OLDER,
        bridge={
            "attestation_id": "bridge-A01",
            "from_candidate": OLDER,
            "to_candidate": CANDIDATE,
        },
    )
    proofs["bridge-A01"] = bridge_attestation(
        record_id="bridge-A01",
        bridges=("proof-A01",),
        signoff=None,
        authority=LEDGER_ANCHOR,
        key=LEDGER_KEY,
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {}
    assert result["closable"] is True


# A record reaches the candidate it was earned on, and bridges to any other


def test_a_record_cannot_reach_a_candidate_it_never_bridged_to():
    """S2c: declaring the final candidate as the origin skipped the bridge."""
    proofs = full_proofs()
    proofs["proof-G07"] = proof_record(
        "G07", candidates=[OLDER, CANDIDATE], origin_candidate=CANDIDATE
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["G07"]
    assert result["rejected"] == {"G07": "proof_candidate_scope_unattested"}
    assert result["closable"] is False


def test_a_bridged_record_may_name_only_the_candidates_its_bridge_reaches():
    proofs = full_proofs()
    proofs["proof-G07"] = proof_record(
        "G07",
        candidates=[OLDER, CANDIDATE, "candidate-third"],
        origin_candidate=OLDER,
        bridge={
            "attestation_id": "bridge-G07",
            "from_candidate": OLDER,
            "to_candidate": CANDIDATE,
        },
    )
    proofs["bridge-G07"] = bridge_attestation()
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "G07": "proof_candidate_scope_unattested"
    }


# Human sign offs and machine evidence do not share a trust anchor


def test_the_anchor_that_seals_routine_evidence_cannot_close_a_human_row():
    """S2d: the difference between the gate meaning a person and meaning a key."""
    proofs = full_proofs()
    proofs["proof-BSA01"] = proof_record(
        "BSA01", authority=LEDGER_ANCHOR, key=LEDGER_KEY
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["BSA01"]
    assert result["rejected"] == {"BSA01": "proof_authority_unknown"}
    assert result["closable"] is False


def test_the_routine_anchor_cannot_seal_an_exit_gate_or_an_amendment():
    records = full_exit_gate_records()
    records["gate-E04"] = exit_gate_record(
        "E04", authority=LEDGER_ANCHOR, key=LEDGER_KEY
    )
    assert certify(full_evidence(), exit_gate_records=records)["exit_gates"] == {
        "E04": "exit_gate_authority_unknown"
    }
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {
        "decision-7": decision_record(
            "BSA07", authority=LEDGER_ANCHOR, key=LEDGER_KEY
        )
    }
    assert certify(evidence, decisions=decisions)["rejected"] == {
        "BSA07": "decision_authority_unknown"
    }


def test_human_trust_anchors_are_required():
    with pytest.raises(ValueError, match="human_trust_anchors_required"):
        certify(full_evidence(), human_trust_anchors={})


@pytest.mark.parametrize(
    "human_anchors",
    [
        {LEDGER_ANCHOR: b"a-key-of-its-own"},
        {"another-name": LEDGER_KEY},
        {REGISTER_ANCHOR: REGISTER_KEY, LEDGER_ANCHOR: LEDGER_KEY},
    ],
)
def test_the_human_anchors_must_not_be_the_routine_anchors(human_anchors):
    with pytest.raises(ValueError, match="human_trust_anchors_not_separate"):
        certify(full_evidence(), human_trust_anchors=human_anchors)


def test_routine_evidence_may_still_be_sealed_by_the_stronger_anchor():
    """The separation is one way: it must not refuse a person sealing CI output."""
    proofs = full_proofs()
    proofs["proof-A01"] = proof_record(
        "A01", authority=REGISTER_ANCHOR, key=REGISTER_KEY
    )
    assert certify(full_evidence(), proofs=proofs)["closable"] is True


# What a record declares about its producer, read all the way down


@pytest.mark.parametrize(
    "mutation",
    [
        {"produced_by": "ai"},
        {"produced_by": "machine"},
        {"producer": "the release pipeline"},
        {"provenance": {"actor_kind": "agent"}},
        {"provenance": {"chain": [{"produced_by": "agent"}]}},
        {"origin_kind": "ai"},
    ],
)
def test_a_producer_that_is_not_a_person_is_refused_at_any_depth(mutation):
    """S3a: the closed list of thirteen words never saw any of these."""
    proofs = full_proofs()
    body = proof_body("P203")
    body.update(mutation)
    authority, key = anchor_for("P203")
    proofs["proof-P203"] = seal(body, authority=authority, key=key)
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"P203": "proof_human_signoff_not_human"}
    assert result["closable"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        {"actor_kind": "machine"},
        {"on_behalf_of": {"actor_kind": "agent"}},
        {"signer_kind": "bot"},
        {"delegates": [{"actor_kind": "automation"}]},
    ],
)
def test_a_signature_block_that_declares_a_machine_is_refused(mutation):
    proofs = full_proofs()
    signoff = human_signoff()
    signoff.update(mutation)
    proofs["proof-P106"] = proof_record("P106", signoff=signoff)
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "P106": "proof_human_signoff_not_human"
    }


def test_the_positive_actor_check_is_the_gate_not_a_list_of_words():
    assert not hasattr(close_programme, "NON_HUMAN_ACTOR_KINDS")
    assert close_programme.HUMAN_ACTOR_KIND == "human"
    assert "actor_kind" in close_programme.ACTOR_DECLARING_FIELDS
    assert "produced_by" in close_programme.ACTOR_DECLARING_FIELDS


# A signature sits inside the window of the record it signs


def test_a_signature_dated_before_its_record_opened_is_refused():
    proofs = full_proofs()
    proofs["proof-P106"] = proof_record(
        "P106", signoff=human_signoff(signed_at="1970-01-01T00:00:00+00:00")
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["unmet"] == ["P106"]
    assert result["rejected"] == {"P106": "proof_human_signoff_predates_record"}
    assert result["closable"] is False


def test_a_signature_on_a_record_that_has_lapsed_is_refused():
    """The record has to be current before its signature is even read."""
    proofs = full_proofs()
    proofs["proof-P106"] = proof_record(
        "P106",
        recorded_at=RECORDED_AT,
        expires_at="2026-09-17T08:00:00+00:00",
        signoff=human_signoff(signed_at="2026-09-17T07:00:00+00:00"),
    )
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "P106": "proof_stale"
    }


def test_an_exit_gate_signature_sits_inside_the_gate_window():
    records = full_exit_gate_records()
    records["gate-E02"] = exit_gate_record(
        "E02", signoff=human_signoff(signed_at="1970-01-01T00:00:00+00:00")
    )
    assert certify(full_evidence(), exit_gate_records=records)["exit_gates"] == {
        "E02": "exit_gate_human_signoff_predates_record"
    }


def test_an_amendment_signature_sits_inside_the_decision_window():
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {
        "decision-7": decision_record(
            "BSA07", signoff=human_signoff(signed_at="1970-01-01T00:00:00+00:00")
        )
    }
    assert certify(evidence, decisions=decisions)["rejected"] == {
        "BSA07": "decision_human_signoff_predates_record"
    }


# Five weakenings the suite did not catch


@pytest.mark.parametrize(
    "actor_kind", ["Human", "HUMAN", " human", "human ", "human\t"]
)
def test_an_actor_kind_that_is_not_exactly_human_is_refused(actor_kind):
    proofs = full_proofs()
    proofs["proof-P106"] = proof_record(
        "P106", signoff=human_signoff(actor_kind=actor_kind)
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"P106": "proof_human_signoff_not_human"}
    assert result["closable"] is False


def test_a_digest_that_agrees_only_in_its_first_characters_is_refused():
    proofs = full_proofs()
    record = dict(proofs["proof-A01"])
    genuine = record["digest"]
    forged = genuine[:16] + "".join(
        "0" if character != "0" else "1" for character in genuine[16:]
    )
    assert forged != genuine
    assert forged[:16] == genuine[:16]
    record["digest"] = forged
    proofs["proof-A01"] = record
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"A01": "proof_digest_invalid"}
    assert result["closable"] is False


@pytest.mark.parametrize(
    ("builder", "reason"),
    [
        ("proof", "proof_record_id_mismatch"),
        ("gate", "exit_gate_record_id_mismatch"),
        ("decision", "decision_record_id_mismatch"),
    ],
)
def test_a_record_that_carries_no_record_id_at_all_is_refused(builder, reason):
    if builder == "proof":
        body = proof_body("A01")
        del body["record_id"]
        proofs = full_proofs()
        proofs["proof-A01"] = seal(body, authority=LEDGER_ANCHOR, key=LEDGER_KEY)
        assert certify(full_evidence(), proofs=proofs)["rejected"] == {"A01": reason}
    elif builder == "gate":
        records = full_exit_gate_records()
        records["gate-E02"] = exit_gate_record("E02", drop_record_id=True)
        assert certify(full_evidence(), exit_gate_records=records)["exit_gates"] == {
            "E02": reason
        }
    else:
        evidence = full_evidence()
        evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
        decisions = {"decision-7": decision_record("BSA07", drop_record_id=True)}
        assert certify(evidence, decisions=decisions)["rejected"] == {"BSA07": reason}


def test_a_renewal_claim_does_not_extend_a_record_that_has_lapsed():
    proofs = full_proofs()
    body = proof_body(
        "A01",
        recorded_at="2026-03-01T08:00:00+00:00",
        expires_at="2026-04-01T08:00:00+00:00",
    )
    body["renewed"] = True
    body["renewed_until"] = "2027-01-01T08:00:00+00:00"
    proofs["proof-A01"] = seal(body, authority=LEDGER_ANCHOR, key=LEDGER_KEY)
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"A01": "proof_stale"}
    assert result["closable"] is False


@pytest.mark.parametrize("requirements", [None, [], "A01"])
def test_a_proof_that_names_no_requirements_closes_nothing(requirements):
    proofs = full_proofs()
    body = proof_body("A01")
    if requirements is None:
        del body["requirements"]
    else:
        body["requirements"] = requirements
    proofs["proof-A01"] = seal(body, authority=LEDGER_ANCHOR, key=LEDGER_KEY)
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"A01": "proof_requirement_mismatch"}
    assert result["closable"] is False


# A record is read once: what was sealed is what is adjudicated


class TwoFacedRecord(dict):
    """A mapping that seals one body through items() and answers another."""

    def __init__(self, body, sealed_requirements):
        super().__init__(body)
        self._sealed_requirements = list(sealed_requirements)

    def items(self):
        shown = dict(self)
        shown["requirements"] = list(self._sealed_requirements)
        return shown.items()


def test_a_record_that_seals_one_body_and_answers_another_is_refused():
    """S4a: the digest read items() while every check read get()."""
    body = proof_body("A01")
    body["requirements"] = ["A01", "A02"]
    record = TwoFacedRecord(body, ["A01"])
    record["authority"] = LEDGER_ANCHOR
    record["digest"] = close_programme.record_digest(record, LEDGER_KEY)
    assert record.get("requirements") == ["A01", "A02"]
    assert dict(record.items())["requirements"] == ["A01"]
    proofs = full_proofs()
    proofs["proof-A01"] = record
    evidence = full_evidence()
    evidence["A02"] = {"state": "achieved", "proof_ids": ["proof-A01"]}
    result = certify(evidence, proofs=proofs)
    assert result["rejected"] == {"A02": "proof_requirement_mismatch"}
    assert result["unmet"] == ["A02"]
    assert result["closable"] is False


def test_a_record_carrying_a_value_no_json_ledger_can_hold_is_refused():
    """S4b: NaN and Infinity were sealed and no strict ledger round trips them."""
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            close_programme.record_digest(
                {"record_id": "proof-A01", "score": value}, LEDGER_KEY
            )
    proofs = full_proofs()
    body = proof_body("A01")
    body["score"] = float("nan")
    proofs["proof-A01"] = seal(body, authority=LEDGER_ANCHOR, key=LEDGER_KEY)
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"A01": "proof_digest_invalid"}
    assert result["closable"] is False


# The evidence row is read once, and what closes a row is what was adjudicated


class HollowProofIds(list):
    """Empty to anything that iterates it, truthy to anything that asks bool()."""

    def __bool__(self):
        return True


class StatefulRow(dict):
    """A row that answers None to the first read of proof_ids and a list after."""

    def __init__(self, *arguments, **keywords):
        super().__init__(*arguments, **keywords)
        self.reads = 0

    def get(self, name, default=None):
        if name == "proof_ids":
            self.reads += 1
            return None if self.reads == 1 else ["proof-never-resolved"]
        return super().get(name, default)


class AchievedLookalike:
    """Not a string, but equal to one. A state the kernel must not read."""

    def __eq__(self, other):
        return other == "achieved"

    def __hash__(self):
        return hash("achieved")


def certify_with_counting_resolvers(evidence, **overrides):
    """Run a closure and report how many times the record resolvers were called."""
    calls = []

    def proof_resolver(record_id):
        calls.append(record_id)
        return full_proofs().get(record_id)

    def decision_resolver(record_id):
        calls.append(record_id)
        return None

    arguments = {
        "candidate": CANDIDATE,
        "accepted_issuers": ISSUERS,
        "identity_roster": default_identity_roster(),
        "proof_resolver": proof_resolver,
        "decision_resolver": decision_resolver,
        "trust_anchors": dict(TRUST_ANCHORS),
        "human_trust_anchors": dict(HUMAN_TRUST_ANCHORS),
        "minimum_distinct_signers": 1,
        "as_of": AS_OF,
        "trusted_clock": lambda: AS_OF,
        "exit_gates": dict(GATE_REFERENCES),
        "exit_gate_resolver": resolver(full_exit_gate_records()),
    }
    arguments.update(overrides)
    result = certify_requirements(
        sorted(requirement_universe()), evidence, **arguments
    )
    return result, calls


def test_a_row_that_is_truthy_but_empty_resolves_nothing_and_closes_nothing():
    """F1: bool(proof_ids) accepted a row that iteration found empty."""
    evidence = {
        requirement: {"state": "achieved", "proof_ids": HollowProofIds()}
        for requirement in sorted(requirement_universe())
    }
    result, calls = certify_with_counting_resolvers(evidence)
    assert calls == []
    assert result["unmet"] == sorted(requirement_universe())
    assert result["rejected"] == {}
    assert result["closable"] is False


def test_a_row_that_answers_one_body_and_then_another_closes_nothing():
    """F1: the row was read twice and the second reading was never adjudicated."""
    evidence = {
        requirement: StatefulRow({"state": "achieved"})
        for requirement in sorted(requirement_universe())
    }
    result, calls = certify_with_counting_resolvers(evidence)
    assert calls == []
    assert result["unmet"] == sorted(requirement_universe())
    assert result["closable"] is False


class TwoFacedEvidence(dict):
    """An evidence map that shows one set of rows to iteration and another to items()."""

    def items(self):
        shown = dict(self)
        shown["Z99"] = {"state": "achieved", "proof_ids": ["proof-A01"]}
        return shown.items()


def test_an_evidence_map_that_hides_a_row_from_its_own_keys_is_refused():
    """F1: the map is read once, so the rows checked are the rows adjudicated."""
    evidence = TwoFacedEvidence(full_evidence())
    assert "Z99" not in set(evidence)
    assert "Z99" in dict(evidence.items())
    with pytest.raises(ValueError, match="evidence_requirement_unknown"):
        certify(evidence)


def test_a_row_state_that_is_not_a_string_is_refused():
    """F1: the state is read, not merely compared, so it has to be a string."""
    evidence = full_evidence()
    evidence["A01"] = {"state": AchievedLookalike(), "proof_ids": ["proof-A01"]}
    with pytest.raises(ValueError, match="evidence_row_invalid"):
        certify(evidence)


def test_a_row_carrying_a_hollow_proof_list_leaves_only_that_row_unmet():
    evidence = full_evidence()
    evidence["A01"] = {"state": "achieved", "proof_ids": HollowProofIds()}
    result = certify(evidence)
    assert result["unmet"] == ["A01"]
    assert result["rejected"] == {}
    assert result["closable"] is False


# Two anchor keys that HMAC treats as one key are not two anchors


def test_an_anchor_key_hmac_pads_into_the_routine_key_is_not_separate():
    """F2: HMAC zero pads a key shorter than its block, so these are one key."""
    with pytest.raises(ValueError, match="human_trust_anchors_not_separate"):
        certify(
            full_evidence(),
            human_trust_anchors={REGISTER_ANCHOR: LEDGER_KEY + b"\x00" * 10},
        )


def test_an_anchor_key_hmac_digests_into_the_routine_key_is_not_separate():
    """F2: HMAC replaces a key longer than its block with its own digest."""
    long_key = b"L" * 100
    with pytest.raises(ValueError, match="human_trust_anchors_not_separate"):
        certify(
            full_evidence(),
            trust_anchors={LEDGER_ANCHOR: long_key},
            human_trust_anchors={
                REGISTER_ANCHOR: hashlib.sha256(long_key).digest()
            },
        )


def test_the_same_key_spelled_as_text_and_as_bytes_is_not_separate():
    with pytest.raises(ValueError, match="human_trust_anchors_not_separate"):
        certify(
            full_evidence(),
            trust_anchors={LEDGER_ANCHOR: b"one-key-under-two-names"},
            human_trust_anchors={REGISTER_ANCHOR: "one-key-under-two-names"},
        )


# One bridge attestation carries one roster row


def test_a_bridge_attestation_may_carry_no_more_than_one_roster_row():
    """F3: the roster scope rule never reached the attestation's own list."""
    proofs = full_proofs()
    proofs["proof-G07"] = bridged_proof()
    proofs["bridge-G07"] = bridge_attestation(bridges=("proof-G07", "proof-F05"))
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {
        "G07": "proof_bridge_human_signoff_scope_too_broad"
    }
    assert result["closable"] is False


def test_one_attestation_cannot_carry_the_whole_roster_to_this_candidate():
    """F3: one signature standing in for twenty one assessors, through bridges."""
    proofs = full_proofs()
    for requirement in sorted(HUMAN_SIGNOFF_IDS):
        proofs[f"proof-{requirement}"] = proof_record(
            requirement,
            candidates=[OLDER, CANDIDATE],
            origin_candidate=OLDER,
            bridge={
                "attestation_id": "bridge-all",
                "from_candidate": OLDER,
                "to_candidate": CANDIDATE,
            },
        )
    proofs["bridge-all"] = bridge_attestation(
        record_id="bridge-all",
        bridges=tuple(f"proof-{row}" for row in sorted(HUMAN_SIGNOFF_IDS)),
        signoff=human_signoff(statement="Carried the whole roster across myself."),
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["closable"] is False
    assert set(result["rejected"]) == HUMAN_SIGNOFF_IDS
    assert set(result["rejected"].values()) == {
        "proof_bridge_human_signoff_scope_too_broad"
    }


def test_a_bridge_over_a_routine_row_may_still_carry_several_proofs():
    """The rule holds the roster; ordinary evidence may share an attestation."""
    proofs = full_proofs()
    for requirement in ("A01", "A02"):
        proofs[f"proof-{requirement}"] = proof_record(
            requirement,
            candidates=[OLDER, CANDIDATE],
            origin_candidate=OLDER,
            bridge={
                "attestation_id": "bridge-routine",
                "from_candidate": OLDER,
                "to_candidate": CANDIDATE,
            },
        )
    proofs["bridge-routine"] = bridge_attestation(
        record_id="bridge-routine",
        bridges=("proof-A01", "proof-A02"),
        signoff=None,
        authority=LEDGER_ANCHOR,
        key=LEDGER_KEY,
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {}
    assert result["closable"] is True


# The accepted issuers are a roster of names, never one name read letter by letter


@pytest.mark.parametrize("issuers", ["albert nomsa", "albert", b"albert"])
def test_an_issuer_roster_that_is_not_a_collection_of_names_is_rejected(issuers):
    """F4: a bare string iterated into characters and passed every check."""
    with pytest.raises(ValueError, match="accepted_issuer_invalid"):
        certify(full_evidence(), issuers=issuers)


def test_an_issuer_roster_is_a_membership_test_and_not_a_substring_test():
    """F4: 'alb' was an accepted issuer whenever 'albert' was."""
    proofs = full_proofs()
    proofs["proof-P106"] = proof_record(
        "P106", signoff=human_signoff(actor="alb")
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"P106": "proof_human_signoff_actor_rejected"}
    assert result["closable"] is False


# A person signing off on machine produced evidence is the ordinary shape


def signed_proof_with(requirement, **fields):
    body = proof_body(requirement)
    body.update(fields)
    authority, key = anchor_for(requirement)
    return seal(body, authority=authority, key=key)


def test_a_person_may_sign_off_on_the_machine_produced_evidence_they_attach():
    """F6: the walk read attached artefact metadata as a claim about the signer."""
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203",
        evidence_artifacts=[
            {
                "path": "ci/report.xml",
                "produced_by": "pytest 9.1.1",
                "sha256": "0" * 64,
            }
        ],
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {}
    assert result["closable"] is True


def test_an_attached_document_carrying_producer_metadata_is_not_a_claim():
    """F6: every PDF in the world carries a producer field."""
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203",
        attachments=[
            {
                "name": "board-minute.pdf",
                "metadata": {"producer": "Adobe PDF Library 23.1"},
            }
        ],
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {}
    assert result["closable"] is True


# The record says positively who made it, rather than failing to say a machine did


def test_a_record_declaring_a_machine_under_an_unknown_name_is_refused():
    """F5: the walk knew five field names and a record may use any name."""
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203",
        generated_by="nightly-certification-agent v4",
        authored_by="automation",
        execution={"runner_kind": "agent", "agent_id": "bot-7"},
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {
        "P203": "proof_human_signoff_provenance_undeclared"
    }
    assert result["closable"] is False


def test_a_provenance_block_that_declares_no_person_is_refused():
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203", provenance={"chain": ["gathered", "reviewed"]}
    )
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "P203": "proof_human_signoff_provenance_undeclared"
    }


def test_a_provenance_block_that_declares_a_person_is_accepted():
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203",
        provenance={
            "actor_kind": "human",
            "statement": "Gathered and reviewed by the assessor who signed.",
        },
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {}
    assert result["closable"] is True


def test_a_signature_block_carrying_an_undeclared_field_is_refused():
    proofs = full_proofs()
    signoff = human_signoff()
    signoff["generated_by"] = "the release pipeline"
    proofs["proof-P106"] = proof_record("P106", signoff=signoff)
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "P106": "proof_human_signoff_provenance_undeclared"
    }


# Distinct signers are rows covered, not names counted


def test_co_signatures_on_one_row_are_not_independent_assessors():
    """F7: three co-signers on one easy row met a minimum of four."""
    proofs = full_proofs()
    evidence = full_evidence()
    extra = []
    for position, actor in enumerate(sorted(ASSESSORS - {"albert"})):
        record_id = f"proof-BSA01-{position}"
        proofs[record_id] = proof_record(
            "BSA01",
            record_id=record_id,
            signoff=human_signoff(
                actor=actor, statement=f"Co-signed BSA01 as {actor}."
            ),
        )
        extra.append(record_id)
    evidence["BSA01"] = {"state": "achieved", "proof_ids": ["proof-BSA01", *extra]}
    # The co-signers hold nothing but that one row, so no exit credits them
    # a slot of their own and the row is all they can be counted for.
    result = certify(
        evidence,
        proofs=proofs,
        issuers=ASSESSORS,
        exit_gate_records=one_hand_exit_gate_records(),
        minimum_distinct_signers=7,
    )
    assert result["signers"] == identities_of(ASSESSORS)
    assert result["signer_rejection"] == "insufficient_distinct_signers"
    assert result["closable"] is False


def test_one_hand_may_lead_four_exits_when_each_population_is_met():
    """Each exit carries the population its own line names and nothing more.

    The documents name a reviewer population per exit and name no rule against
    one person sitting on two of them, so four exits led by one hand close when
    every one of those populations is filled by people.
    """
    records = gates_signed_by({gate: "nomsa" for gate in ANY_ISSUER_GATES})
    result = certify_multi_signer(exit_gate_records=records)
    assert result["signers"] == identities_of(ASSESSORS)
    assert result["exit_gates"] == {}
    assert result["signer_rejection"] is None
    assert result["closable"] is True


def test_a_covering_roster_of_signers_still_closes_the_programme():
    result = certify_multi_signer()
    assert result["signer_rejection"] is None
    assert result["closable"] is True


# A bridge that was never adjudicated widens nothing


def test_an_unadjudicated_bridge_does_not_widen_a_proof_scope():
    """F8: a bridge naming a record that does not exist advertised a candidate."""
    proofs = full_proofs()
    proofs["proof-A01"] = proof_record(
        "A01",
        candidates=[CANDIDATE, "candidate-future"],
        bridge={
            "attestation_id": "no-such-attestation",
            "from_candidate": CANDIDATE,
            "to_candidate": "candidate-future",
        },
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"A01": "proof_candidate_scope_unattested"}
    assert result["closable"] is False


# The trusted clock is described as the control it is


def test_the_module_says_what_the_trusted_clock_control_is_for():
    """F10: the docstring claimed a control the injected clock cannot give."""
    text = " ".join(close_programme.__doc__.split())
    assert "for operator error and host clock skew" in text
    assert "not a control against an adversary" in text
    assert "not taken on the caller's word" not in text


# One person is one assessor, whatever the spelling


# Five spellings of one human being. They differ by case, by a trailing space,
# by a tab, by an invisible zero width space and by NFD against NFC, and every
# one of them normalises to the same name.
ONE_ASSESSOR_SPELLINGS = (
    "Andre\u0301",
    "andr\u00e9",
    "andr\u00e9 ",
    "andr\u00e9\t",
    "andr\u200b\u00e9",
)
ONE_ASSESSOR = "andr\u00e9"
# A Latin name with U+0430, the Cyrillic small letter a, in place of its first
# letter. It renders as "albert" and is not the name "albert".
HOMOGLYPH_ALBERT = "\u0430lbert"


def roster_signed_by(name):
    """Every roster row signed by one name, bar the rows a document names.

    A row the documents source to a person keeps that person: handing it to
    somebody else is refused now, and the point these tests make is about
    counting people rather than about who may sign what.
    """
    return {
        requirement: close_programme.SOURCE_NAMED_SIGNERS.get(requirement, name)
        for requirement in HUMAN_SIGNOFF_IDS
    }


def signers_of(assignment, statement):
    """Proofs for the whole universe with the roster rows signed as assigned."""
    proofs = {}
    for requirement in requirement_universe():
        signoff = None
        if requirement in HUMAN_SIGNOFF_IDS:
            signoff = human_signoff(
                actor=assignment[requirement], statement=statement
            )
        proofs[f"proof-{requirement}"] = proof_record(requirement, signoff=signoff)
    return proofs


def gates_signed_by(assignment):
    """Every seat of every exit, with the first seat named by the assignment.

    An exit the assignment does not name keeps its own first signer, and the
    seats after the first always keep theirs. An assignment therefore varies one
    hand per exit while the documented population of each exit is still filled.
    """
    records = {}
    for gate in EXIT_GATE_IDS:
        for seat in range(GATE_SIGNER_COUNTS[gate]):
            actor = GATE_SIGNERS[gate][seat]
            if seat == 0:
                actor = assignment.get(gate, actor)
            records[GATE_REFERENCES[gate][seat]] = exit_gate_record(
                gate,
                seat=seat,
                signoff=human_signoff(
                    actor=actor,
                    statement=f"Exit of {gate} accepted on the candidate.",
                ),
            )
    return records


def test_five_spellings_of_one_name_are_one_assessor():
    """N1: one human, listed five times, counted as five distinct signers.

    Four exits in one hand under four spellings, one hand on the roster and the
    owner on the rows and the exit the documents name for him. Seven people
    sign, and a count over spellings would have made it eleven.
    """
    gates = gates_signed_by(dict(zip(ANY_ISSUER_GATES, ONE_ASSESSOR_SPELLINGS)))
    result = certify(
        full_evidence(),
        proofs=signers_of(
            roster_signed_by("lerato"), "Reviewed on the final candidate myself."
        ),
        exit_gate_records=gates,
        issuers=[
            *ONE_ASSESSOR_SPELLINGS,
            "lerato",
            "nomsa",
            "sipho",
            "thabo",
            "albert",
            *GATE_CO_SIGNERS,
        ],
        minimum_distinct_signers=8,
    )
    assert result["signers"] == identities_of(
        {ONE_ASSESSOR, "lerato", "albert", *GATE_CO_SIGNERS}
    )
    assert result["signer_rejection"] == "insufficient_distinct_signers"
    assert result["closable"] is False


def test_the_exits_read_spellings_of_one_name_as_one_identity():
    """N1: four spellings on four exits are one person, and are reported as one.

    A count over strings made four signers of them. The exits no longer refuse
    one hand for holding more than one of them, so what this pins is the
    reading: the result names the identity the roster gives, once, and never
    the spellings it was signed with.
    """
    roster = roster_signers()
    gates = gates_signed_by(dict(zip(ANY_ISSUER_GATES, ONE_ASSESSOR_SPELLINGS)))
    result = certify(
        full_evidence(),
        proofs=signers_of(roster, "Reviewed on the final candidate myself."),
        exit_gate_records=gates,
        issuers=[*ONE_ASSESSOR_SPELLINGS, *ASSESSORS],
        minimum_distinct_signers=5,
    )
    assert result["signers"] == identities_of({ONE_ASSESSOR, *ASSESSORS})
    assert result["exit_gates"] == {}
    assert result["signer_rejection"] is None
    assert result["closable"] is True


def test_the_population_of_an_exit_holds_at_a_minimum_of_one():
    """The stated minimum is not what the population of an exit is read off.

    A minimum of one is a weaker bound than the population each exit carries,
    not permission to set that population aside. An exit whose seats are
    answered by fewer people than its own line names is refused at a one as it
    is refused at any other number the caller may state.
    """
    lead = GATE_SIGNERS["E02"][0]
    result = certify(
        full_evidence(),
        exit_gate_records=exit_seats_signed_by("E02", (lead, lead)),
        minimum_distinct_signers=1,
    )
    assert result["unmet"] == []
    assert result["rejected"] == {}
    assert result["exit_gates"] == {"E02": "exit_gate_signers_insufficient"}
    assert result["closable"] is False


def test_two_exits_may_be_held_by_one_name_at_a_minimum_of_one():
    """A seat on one exit and a seat on another, in one hand, and both filled."""
    shared = {**GATE_LEAD_SIGNERS, "B04": GATE_LEAD_SIGNERS["E01"]}
    result = certify(
        full_evidence(),
        exit_gate_records=gates_signed_by(shared),
        minimum_distinct_signers=1,
    )
    assert result["exit_gates"] == {}
    assert result["signer_rejection"] is None
    assert result["closable"] is True


def test_the_exit_population_reads_the_normalised_name_at_a_minimum_of_one():
    """Three spellings of one name fill one seat of an exit, at any minimum."""
    result = certify(
        full_evidence(),
        exit_gate_records=exit_seats_signed_by("E02", ONE_ASSESSOR_SPELLINGS[:3]),
        issuers=[*ISSUERS, *ONE_ASSESSOR_SPELLINGS],
        minimum_distinct_signers=1,
    )
    assert result["exit_gates"] == {"E02": "exit_gate_signers_insufficient"}
    assert result["closable"] is False


def test_a_name_that_mixes_scripts_is_not_an_accepted_issuer():
    """N1: U+0430 renders as a Latin a and is not one."""
    with pytest.raises(ValueError, match="accepted_issuer_invalid"):
        certify(full_evidence(), issuers=[*ASSESSORS, HOMOGLYPH_ALBERT])


@pytest.mark.parametrize("name", ["\u200b", "  ", "\t", "\u00ad"])
def test_a_name_that_normalises_to_nothing_is_not_an_accepted_issuer(name):
    with pytest.raises(ValueError, match="accepted_issuer_invalid"):
        certify(full_evidence(), issuers=["albert", name])


def test_a_homoglyph_of_an_accepted_name_cannot_sign(name=HOMOGLYPH_ALBERT):
    proofs = full_proofs()
    proofs["proof-P106"] = proof_record(
        "P106", signoff=human_signoff(actor=name)
    )
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "P106": "proof_human_signoff_actor_rejected"
    }


@pytest.mark.parametrize("spelling", ["ALBERT", " albert", "albert\t", "al\u200bbert"])
def test_an_accepted_name_spelled_another_way_is_still_that_person(spelling):
    """N1: refusing an honest variation is as wrong as counting it as a second."""
    proofs = full_proofs()
    proofs["proof-P106"] = proof_record(
        "P106", signoff=human_signoff(actor=spelling)
    )
    result = certify(
        full_evidence(),
        proofs=proofs,
        exit_gate_records=one_hand_exit_gate_records(),
    )
    assert result["rejected"] == {}
    # The subject, unchanged: every spelling folds into the one person, so the
    # roster is that one name and never that name plus the variation counted
    # again as a second assessor.
    assert result["signers"] == identities_of({"albert", *GATE_CO_SIGNERS})
    # Each exit is accepted on its own terms and its population is filled, and
    # no rule beyond those populations holds the exits apart, so the one hand
    # on the first seat of all five of them refuses nothing.
    assert result["exit_gates"] == {}
    assert result["signer_rejection"] is None
    assert result["closable"] is True


def test_an_amendment_issuer_is_read_under_the_same_rule():
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {"decision-7": decision_record("BSA07", issuer="ALBERT ")}
    assert certify(evidence, decisions=decisions)["rejected"] == {}
    decisions = {"decision-7": decision_record("BSA07", issuer=HOMOGLYPH_ALBERT)}
    assert certify(evidence, decisions=decisions)["rejected"] == {
        "BSA07": "decision_issuer_rejected"
    }


# Identity is the roster the caller holds, never a rule over strings


# Five spellings of one name that no rule over strings tells apart. Each carries
# one invisible character from a category the normalisation does not touch: a
# combining grapheme joiner, a variation selector, a Mongolian free variation
# selector and a blank braille pattern. None of them is a letter, so the mixed
# script rule never sees them either, and a spreadsheet paste carries one by
# accident. They are five names and one person.
INVISIBLE_SPELLINGS = (
    "kagiso",
    "ka\u034fgiso",
    "kag\ufe00iso",
    "kagi\u2800so",
    "kagis\u180bo",
)
# One assessor who goes by two genuinely different names. No normalisation folds
# these together and none should: only the roster says they are one person.
ONE_PERSON_UNDER_TWO_NAMES = ("mbali khumalo", "m. khumalo")


def test_five_invisible_spellings_of_one_name_hold_one_identity():
    """The fourth review closed the programme on one name spelled five ways.

    Every one of these survives the normalisation, so under a rule over strings
    they are five accepted issuers and five assessors. The roster says they are
    one person, and the result names that person once however many exits the
    spellings were spread over.
    """
    result = certify(
        full_evidence(),
        exit_gate_records=gates_signed_by(
            dict(zip(ANY_ISSUER_GATES, INVISIBLE_SPELLINGS))
        ),
        issuers=[*INVISIBLE_SPELLINGS, "albert", *GATE_CO_SIGNERS],
        minimum_distinct_signers=1,
    )
    assert result["exit_gates"] == {}
    assert result["signers"] == identities_of(
        {"albert", "kagiso", *GATE_CO_SIGNERS}
    )
    assert result["signer_rejection"] is None
    assert result["closable"] is True


def test_invisible_spellings_do_not_fill_the_population_of_one_exit():
    """The same five names, on the seats of one exit, are still one reviewer.

    This is where the folding has to bite now: the population of an exit is
    people, so three seats answered by three spellings of one name are one
    reviewer signing three times and the exit is short of its own count.
    """
    result = certify(
        full_evidence(),
        exit_gate_records=exit_seats_signed_by("E02", INVISIBLE_SPELLINGS[:3]),
        issuers=[*ISSUERS, *INVISIBLE_SPELLINGS],
        minimum_distinct_signers=1,
    )
    assert result["exit_gates"] == {"E02": "exit_gate_signers_insufficient"}
    assert result["closable"] is False


def test_five_invisible_spellings_of_one_name_are_not_five_assessors():
    """The same five names counted as five signers against a minimum of five."""
    result = certify(
        full_evidence(),
        proofs=signers_of(
            roster_signed_by("lerato"), "Reviewed on the final candidate myself."
        ),
        exit_gate_records=gates_signed_by(
            dict(zip(ANY_ISSUER_GATES, INVISIBLE_SPELLINGS))
        ),
        issuers=[
            *INVISIBLE_SPELLINGS,
            "lerato",
            "nomsa",
            "sipho",
            "thabo",
            "albert",
            *GATE_CO_SIGNERS,
        ],
        minimum_distinct_signers=8,
    )
    assert result["signers"] == identities_of(
        {"kagiso", "lerato", "albert", *GATE_CO_SIGNERS}
    )
    assert result["signer_rejection"] == "insufficient_distinct_signers"
    assert result["closable"] is False


def test_two_names_of_one_person_are_one_signer_on_one_exit():
    """Two names no normalisation folds, on two seats of the same exit.

    Only the roster says these are one person, and the population of an exit is
    people, so the two seats they hold between them are one reviewer and the
    exit is short of the two its own line names.
    """
    result = certify(
        full_evidence(),
        exit_gate_records=exit_seats_signed_by(
            "E01", ONE_PERSON_UNDER_TWO_NAMES
        ),
        issuers=[*ISSUERS, *ONE_PERSON_UNDER_TWO_NAMES],
        minimum_distinct_signers=1,
    )
    assert result["exit_gates"] == {"E01": "exit_gate_signers_insufficient"}
    assert result["closable"] is False


def test_the_roster_is_read_through_the_same_normalisation_as_a_signature():
    """A roster spelling and an issuer spelling need not match character for
    character. Both are folded once, and one folded name carries one identity,
    so a roster written in another case still names the same person."""
    roster = {
        **{
            name: f"{IDENTITY_PREFIX}{name}"
            for name in ISSUERS
            if name != "albert"
        },
        " ALBERT\t": f"{IDENTITY_PREFIX}albert",
    }
    result = certify(
        full_evidence(),
        issuers=[*(ISSUERS - {"albert"}), "Albert "],
        identity_roster=roster,
    )
    assert result["rejected"] == {}
    assert result["signers"] == identities_of(ISSUERS)
    assert result["closable"] is True


def test_a_name_the_roster_maps_but_the_closure_does_not_accept_cannot_sign():
    """The roster may be wider than the run: it says who people are, not who
    this closure accepts."""
    records = full_exit_gate_records()
    records["gate-E04"] = exit_gate_record(
        "E04",
        signoff=human_signoff(
            actor="thabo", statement="Exit of E04 accepted on the candidate."
        ),
    )
    assert "thabo" in default_identity_roster()
    assert "thabo" not in ISSUERS
    result = certify(full_evidence(), issuers=ISSUERS, exit_gate_records=records)
    assert result["exit_gates"] == {
        "E04": "exit_gate_human_signoff_actor_rejected"
    }
    assert result["closable"] is False


def test_the_result_reports_the_identity_and_not_the_name_signed_with():
    """A count by name is visible here: the identities are not the names."""
    result = certify(full_evidence(), exit_gate_records=one_hand_exit_gate_records())
    assert result["signers"] == identities_of({"albert", *GATE_CO_SIGNERS})
    assert "albert" not in result["signers"]


def test_the_identity_roster_is_the_callers_and_has_no_default():
    """Who counts as one person is not this module's decision to take."""
    with pytest.raises(TypeError):
        certify_requirements(
            sorted(requirement_universe()),
            full_evidence(),
            candidate=CANDIDATE,
            accepted_issuers=ISSUERS,
            proof_resolver=resolver(full_proofs()),
            decision_resolver=resolver({}),
            trust_anchors=dict(TRUST_ANCHORS),
            human_trust_anchors=dict(HUMAN_TRUST_ANCHORS),
            minimum_distinct_signers=1,
            as_of=AS_OF,
            trusted_clock=lambda: AS_OF,
            exit_gates=dict(GATE_REFERENCES),
            exit_gate_resolver=resolver(full_exit_gate_records()),
        )


def test_a_roster_that_does_not_map_every_accepted_issuer_is_refused():
    """An issuer with no identity is refused rather than guessed at."""
    roster = {
        name: identity
        for name, identity in default_identity_roster().items()
        if name != "yusuf"
    }
    with pytest.raises(ValueError, match="identity_roster_incomplete"):
        certify(full_evidence(), issuers=ASSESSORS, identity_roster=roster)


def test_two_spellings_of_one_name_may_not_carry_two_identities():
    """The roster is read through the same normalisation the signatures are."""
    roster = {**default_identity_roster(), "ALBERT ": "person-someone-else"}
    with pytest.raises(ValueError, match="identity_roster_inconsistent"):
        certify(full_evidence(), identity_roster=roster)


@pytest.mark.parametrize(
    ("roster", "reason"),
    [
        (None, "identity_roster_required"),
        ({}, "identity_roster_required"),
        ("albert", "identity_roster_required"),
        ({"albert": ""}, "identity_roster_identity_invalid"),
        ({"albert": "   "}, "identity_roster_identity_invalid"),
        ({"albert": 7}, "identity_roster_identity_invalid"),
        ({"albert": ["person-albert"]}, "identity_roster_identity_invalid"),
        ({"\u200b": "person-albert"}, "identity_roster_name_invalid"),
        ({"": "person-albert"}, "identity_roster_name_invalid"),
        ({7: "person-albert"}, "identity_roster_name_invalid"),
    ],
)
def test_an_identity_roster_that_is_not_a_mapping_of_people_is_refused(roster, reason):
    with pytest.raises(ValueError, match=reason):
        certify(
            full_evidence(),
            issuers=frozenset({"albert"}),
            identity_roster=roster,
        )


def test_a_minimum_above_the_number_of_identities_is_refused():
    """Three names that are two people cannot be three independent assessors."""
    with pytest.raises(ValueError, match="accepted_issuers_insufficient"):
        certify(
            full_evidence(),
            issuers=[*ONE_PERSON_UNDER_TWO_NAMES, "albert"],
            minimum_distinct_signers=3,
        )


def test_a_control_character_inside_a_name_does_not_make_a_second_assessor():
    """M: the normalisation no longer mapping a control character to a space.

    U+0001 is not whitespace, so nothing splits on it and no collapse reaches
    it. Only the mapping to a space makes this the assessor the roster knows.
    """
    roster = roster_signed_by("assessor\u0001one")
    result = certify(
        full_evidence(),
        proofs=signers_of(roster, "Reviewed on the final candidate myself."),
        issuers=[SPACED_ASSESSOR, *ISSUERS],
        minimum_distinct_signers=5,
    )
    assert result["rejected"] == {}
    assert result["signers"] == identities_of({SPACED_ASSESSOR, *ISSUERS})
    assert result["closable"] is True


# An exit gate and an amendment reach the candidate they were issued against


@pytest.mark.parametrize("gate", EXIT_GATE_IDS)
def test_an_exit_gate_may_not_be_issued_against_a_wider_candidate_list(gate):
    """N2: a signature made on an earlier candidate pre-named the final one."""
    records = full_exit_gate_records()
    records[f"gate-{gate}"] = exit_gate_record(
        gate, candidates=[OLDER, CANDIDATE]
    )
    result = certify(full_evidence(), exit_gate_records=records)
    assert result["exit_gates"] == {gate: "exit_gate_candidate_scope_unbounded"}
    assert result["closable"] is False


def test_an_exit_gate_naming_a_later_candidate_too_is_refused():
    records = full_exit_gate_records()
    records["gate-E04"] = exit_gate_record(
        "E04", candidates=[CANDIDATE, "candidate-future"]
    )
    assert certify(full_evidence(), exit_gate_records=records)["exit_gates"] == {
        "E04": "exit_gate_candidate_scope_unbounded"
    }


def test_an_amendment_decision_may_not_be_issued_against_a_wider_candidate_list():
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {
        "decision-7": decision_record("BSA07", candidates=[OLDER, CANDIDATE])
    }
    result = certify(evidence, decisions=decisions)
    assert result["rejected"] == {"BSA07": "decision_candidate_scope_unbounded"}
    assert result["closable"] is False


@pytest.mark.parametrize("candidates", [[CANDIDATE, 7], "candidate-819905d", []])
def test_an_exit_gate_candidate_list_that_is_not_a_list_of_names_is_refused(
    candidates,
):
    records = full_exit_gate_records()
    records["gate-E04"] = exit_gate_record("E04", candidates=candidates)
    assert certify(full_evidence(), exit_gate_records=records)["exit_gates"] == {
        "E04": "exit_gate_candidate_inapplicable"
    }


def test_a_gate_and_a_decision_naming_this_candidate_alone_still_close():
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {"decision-7": decision_record("BSA07", candidates=[CANDIDATE])}
    result = certify(evidence, decisions=decisions)
    assert result["rejected"] == {}
    assert result["exit_gates"] == {}
    assert result["closable"] is True


# The accepted issuers are read once, not twice


class TwoFacedIssuers(Collection):
    """A roster that shows one set of names to its first reader and more after."""

    def __init__(self, names, smuggled):
        self._names = list(names)
        self._smuggled = smuggled
        self.reads = 0

    def __iter__(self):
        self.reads += 1
        if self.reads == 1:
            return iter(self._names)
        return iter([*self._names, self._smuggled])

    def __len__(self):
        return len(self._names)

    def __contains__(self, item):
        return item in self._names or item == self._smuggled


@pytest.mark.parametrize("smuggled", ["", "the release bot"])
def test_an_issuer_roster_read_twice_cannot_smuggle_a_signer_past_validation(
    smuggled,
):
    """N3: the roster was iterated to validate it and again to build the set."""
    issuers = TwoFacedIssuers(sorted(ASSESSORS), smuggled)
    records = full_exit_gate_records()
    records["gate-E04"] = exit_gate_record(
        "E04", signoff=human_signoff(actor=smuggled)
    )
    result = certify(full_evidence(), issuers=issuers, exit_gate_records=records)
    assert result["exit_gates"] == {
        "E04": "exit_gate_human_signoff_actor_rejected"
    }
    assert smuggled not in result["signers"]
    assert result["closable"] is False


# The honest shapes of a human sign off are not refused


def test_a_provenance_chain_may_name_the_runner_that_built_and_the_person_who_signed():
    """N4: the walk descended into the chain and refused the first step."""
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203",
        provenance=human_provenance(
            chain=[
                {"produced_by": "the build runner", "statement": "gathered the run"},
                {"actor_kind": "human", "statement": "reviewed and signed it"},
            ]
        ),
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {}
    assert result["closable"] is True


def test_a_provenance_chain_whose_last_step_is_a_machine_is_refused():
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203",
        provenance=human_provenance(
            chain=[
                {"actor_kind": "human", "statement": "reviewed it"},
                {"produced_by": "the release pipeline"},
            ]
        ),
    )
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "P203": "proof_human_signoff_not_human"
    }


def test_a_signature_block_may_name_the_reviewer_role_and_the_key_it_was_made_with():
    """N4: the documents define these gates by role, and a role was refused."""
    records = full_exit_gate_records()
    signoff = gate_signoff("B04")
    signoff["role"] = "the client reviewer"
    signoff["key_id"] = "authority-register/albert/2026-09"
    records["gate-B04"] = exit_gate_record("B04", signoff=signoff)
    result = certify(full_evidence(), exit_gate_records=records)
    assert result["exit_gates"] == {}
    assert result["closable"] is True


def test_a_provenance_block_may_carry_prose_under_a_name_of_its_own():
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203",
        provenance=human_provenance(
            note="Albert gathered the run himself and read every row of it.",
            method="read in person, against the ledger",
        ),
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {}
    assert result["closable"] is True


@pytest.mark.parametrize(
    "field",
    ["requirements", "candidates", "human_signoff", "provenance", "evidence_fields"],
)
def test_a_declaration_of_evidence_may_not_name_a_field_of_this_kernel(field):
    """A kernel field is not attached material, and declaring it is not a name.

    The declaration is how a signer accounts for what they attached, and a field
    this kernel defines is not something a signer attached. Admitting one would
    let a record declare its own requirements list, its own signature block or
    the declaration itself as the evidence the signature is about, which is an
    account of nothing. Permitting the field at top level is not the same as
    reading it as evidence, so this refusal is load bearing and not decorative.
    """
    body = proof_body("P106")
    body[EVIDENCE_DECLARATION] = [field]
    proofs = full_proofs()
    proofs["proof-P106"] = seal(body, authority=REGISTER_ANCHOR, key=REGISTER_KEY)
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {
        "P106": "proof_human_signoff_provenance_undeclared"
    }
    assert result["closable"] is False


def test_a_signer_may_attach_evidence_under_a_name_they_choose():
    """N4: only two attachment names were admitted and a signer has more."""
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203",
        evidence_fields=["board_pack"],
        board_pack=[{"name": "board-minute.pdf", "pages": 14}],
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {}
    assert result["closable"] is True


@pytest.mark.parametrize(
    "fields",
    [
        {
            "evidence_fields": ["pipeline"],
            "pipeline": {"produced_by": "nightly agent v4"},
        },
        {"evidence_fields": ["board_pack"], "board_pack": ""},
        {"evidence_fields": ["record_type"]},
        {"evidence_fields": ["actor_kind"], "actor_kind": "human"},
        {"evidence_fields": "board_pack", "board_pack": [{"name": "x"}]},
        {"evidence_fields": ["board_pack"]},
    ],
)
def test_a_declared_evidence_field_cannot_launder_a_claim_about_the_signer(fields):
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with("P203", **fields)
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "P203": "proof_human_signoff_provenance_undeclared"
    }


def test_a_declared_evidence_field_may_not_declare_who_produced_the_record():
    """A declared field was off the provenance surface, so a gate could declare
    a machine producer under a name of its own and close on it."""
    records = full_exit_gate_records()
    records["gate-E04"] = exit_gate_record(
        "E04",
        signoff=gate_signoff("E04"),
        evidence_fields=["pipeline"],
        pipeline={"produced_by": "nightly-certification-agent v4"},
    )
    result = certify(full_evidence(), exit_gate_records=records)
    assert result["exit_gates"] == {
        "E04": "exit_gate_human_signoff_provenance_undeclared"
    }
    assert result["closable"] is False


def test_a_declared_evidence_field_may_not_declare_an_actor_at_any_depth():
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203",
        evidence_fields=["board_pack"],
        board_pack=[{"exhibits": [{"signer_kind": "human"}]}],
    )
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "P203": "proof_human_signoff_provenance_undeclared"
    }


def test_an_attachment_recorded_as_a_reference_string_is_evidence():
    """The same value wrapped in a list passed while the bare string did not."""
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203",
        evidence_fields=["board_pack"],
        board_pack="s3://42-evidence/board-minute.pdf",
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {}
    assert result["closable"] is True


@pytest.mark.parametrize("field", sorted(close_programme.DECLARED_EVIDENCE_FIELDS))
@pytest.mark.parametrize("material", ["", 7, None])
def test_a_built_in_attachment_name_is_held_to_the_shape_a_declared_name_is(
    field, material
):
    """The two names this kernel defines for attached material were exempt from
    the check the names a signer declares are held to, so an attachment of an
    empty string closed the row under "attachments" and the same value was
    refused under a name of the signer's own choosing. A field carrying no
    material is not an account of what the signature is about, whichever name it
    carries.
    """
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with("P203", **{field: material})
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {
        "P203": "proof_human_signoff_provenance_undeclared"
    }
    assert result["closable"] is False


@pytest.mark.parametrize("field", sorted(close_programme.DECLARED_EVIDENCE_FIELDS))
def test_a_built_in_attachment_name_still_carries_a_reference_string(field):
    """The check above reads the material and does not narrow what a signer may
    attach under the names this kernel defines."""
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203", **{field: "s3://42-evidence/board-minute.pdf"}
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {}
    assert result["closable"] is True


def test_a_provenance_chain_whose_last_step_signed_it_needs_no_second_claim():
    """The chain was decorative: the top level had to declare a person anyway."""
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203",
        provenance={
            "statement": "The history of this record, oldest step first.",
            "chain": [
                {"produced_by": "the build runner", "statement": "gathered the run"},
                {"actor_kind": "human", "statement": "read it and signed it"},
            ],
        },
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {}
    assert result["closable"] is True


def test_a_chain_whose_last_step_is_a_machine_is_refused_with_no_other_claim():
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with(
        "P203",
        provenance={
            "statement": "The history of this record, oldest step first.",
            "chain": [
                {"actor_kind": "human", "statement": "read it"},
                {"produced_by": "the release pipeline"},
            ],
        },
    )
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "P203": "proof_human_signoff_not_human"
    }


def test_an_empty_chain_is_not_a_claim_that_a_person_made_the_record():
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with("P203", provenance={"chain": []})
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "P203": "proof_human_signoff_provenance_undeclared"
    }


def test_a_signature_block_may_carry_the_signature_itself():
    """The field set held the role and the key id and not the signature."""
    records = full_exit_gate_records()
    signoff = gate_signoff("B04")
    signoff["signature"] = "MEUCIQDf2b1c0a9e8d7c6b5a49382716" * 2
    signoff["key_id"] = "authority-register/albert/2026-09"
    records["gate-B04"] = exit_gate_record("B04", signoff=signoff)
    result = certify(full_evidence(), exit_gate_records=records)
    assert result["exit_gates"] == {}
    assert result["closable"] is True


# A record that carries a signature accounts for its own production


@pytest.mark.parametrize("requirement", ["BSA01", "P106", "P203"])
def test_a_signed_proof_without_a_provenance_block_is_refused(requirement):
    """N4: the positive shape was optional, so nothing ever had to carry it."""
    proofs = full_proofs()
    proofs[f"proof-{requirement}"] = proof_record(requirement, provenance=None)
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {
        requirement: "proof_human_signoff_provenance_undeclared"
    }
    assert result["closable"] is False


@pytest.mark.parametrize("gate", EXIT_GATE_IDS)
def test_an_exit_gate_without_a_provenance_block_is_refused(gate):
    records = full_exit_gate_records()
    records[f"gate-{gate}"] = exit_gate_record(gate, provenance=None)
    result = certify(full_evidence(), exit_gate_records=records)
    assert result["exit_gates"] == {
        gate: "exit_gate_human_signoff_provenance_undeclared"
    }
    assert result["closable"] is False


def test_an_amendment_decision_without_a_provenance_block_is_refused():
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {"decision-7": decision_record("BSA07", provenance=None)}
    assert certify(evidence, decisions=decisions)["rejected"] == {
        "BSA07": "decision_human_signoff_provenance_undeclared"
    }


def test_a_bridge_over_a_roster_row_needs_a_provenance_block_too():
    proofs = full_proofs()
    proofs["proof-G07"] = bridged_proof()
    proofs["bridge-G07"] = bridge_attestation(provenance=None)
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "G07": "proof_bridge_human_signoff_provenance_undeclared"
    }


@pytest.mark.parametrize(
    "provenance",
    ["a person made it", ["a person made it"], 7, {"statement": "a person made it"}],
)
def test_a_provenance_block_that_is_not_a_positive_declaration_is_refused(provenance):
    proofs = full_proofs()
    proofs["proof-P106"] = proof_record("P106", provenance=provenance)
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "P106": "proof_human_signoff_provenance_undeclared"
    }


# A record is read once, all the way down its sequences


class SmugglingRequirements(list):
    """A list that answers what was sealed, then something wider, then again."""

    def __init__(self, sealed, smuggled):
        super().__init__(sealed)
        self._sealed = list(sealed)
        self._smuggled = list(smuggled)
        self.reads = 0

    def __iter__(self):
        self.reads += 1
        if self.reads % 2 == 1:
            return iter(self._sealed)
        return iter(self._smuggled)


def test_flattening_copies_every_sequence_into_a_plain_list():
    """M05: nothing in the suite read the sequence half of the flattening."""
    nested = {
        "tuple": (1, (2, 3)),
        "list": [SmugglingRequirements(["x"], ["x", "y"])],
    }
    plain = close_programme._plain(nested)
    assert plain == {"tuple": [1, [2, 3]], "list": [["x"]]}
    assert type(plain["tuple"]) is list
    assert type(plain["tuple"][1]) is list
    assert type(plain["list"][0]) is list


def test_a_sealed_list_that_answers_wider_after_the_digest_is_refused():
    """M05: a genuine seal over one row, adjudicated as two, without the key."""
    record = proof_record("A01")
    record["requirements"] = SmugglingRequirements(["A01"], ["A01", "A02"])
    proofs = full_proofs()
    proofs["proof-A01"] = record
    evidence = full_evidence()
    evidence["A02"] = {"state": "achieved", "proof_ids": ["proof-A01"]}
    result = certify(evidence, proofs=proofs)
    assert result["unmet"] == ["A02"]
    assert result["rejected"] == {"A02": "proof_digest_invalid"}
    assert result["closable"] is False


# What the closure rule does and does not say about a hand on two exits


def test_four_of_the_five_exits_in_one_hand_still_closes():
    """The exits are held to their own populations and to no rule above them."""
    assignment = dict.fromkeys(ANY_ISSUER_GATES, "nomsa")
    result = certify_multi_signer(
        minimum_distinct_signers=5, exit_gate_records=gates_signed_by(assignment)
    )
    assert result["exit_gates"] == {}
    assert result["signer_rejection"] is None
    assert result["closable"] is True


def test_two_of_the_five_exits_in_one_hand_still_closes():
    assignment = dict(GATE_LEAD_SIGNERS)
    assignment["B04"] = assignment["E01"]
    result = certify_multi_signer(
        minimum_distinct_signers=5, exit_gate_records=gates_signed_by(assignment)
    )
    assert result["exit_gates"] == {}
    assert result["signer_rejection"] is None
    assert result["closable"] is True


def test_a_bridge_attestation_cannot_stand_as_a_proof():
    """One record is one kind: a seal that bridges does not also prove."""
    proofs = full_proofs()
    proofs["proof-A01"] = bridge_attestation(
        record_id="proof-A01",
        bridges=("proof-A01",),
        signoff=None,
        authority=LEDGER_ANCHOR,
        key=LEDGER_KEY,
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {"A01": "proof_record_type_mismatch"}
    assert result["closable"] is False


def test_evidence_that_expires_at_the_certification_instant_is_stale():
    """A window that closes at the instant is closed at the instant."""
    proofs = full_proofs()
    proofs["proof-A01"] = proof_record("A01", expires_at=AS_OF)
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {"A01": "proof_stale"}
    records = full_exit_gate_records()
    records["gate-E04"] = exit_gate_record("E04", expires_at=AS_OF)
    assert certify(full_evidence(), exit_gate_records=records)["exit_gates"] == {
        "E04": "exit_gate_stale"
    }
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {"decision-7": decision_record("BSA07", expires_at=AS_OF)}
    assert certify(evidence, decisions=decisions)["rejected"] == {
        "BSA07": "decision_stale"
    }
    proofs = full_proofs()
    proofs["proof-G07"] = bridged_proof()
    proofs["bridge-G07"] = bridge_attestation(expires_at=AS_OF)
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "G07": "proof_bridge_stale"
    }


# The signable arithmetic, pinned rather than recomputed


# Placeholder identifiers standing for the distinct people a closure can credit
# at most. There are twenty one roster rows and nine exit gate seats, so there
# are thirty slots, and the most people they can hold is twenty nine: the
# documents name one person for both G09 and E04, so those two slots are one
# hand. "albert" is that hand and the rest name nobody.
CEILING_ASSESSORS = (
    "albert",
    *(f"assessor-{position:02d}" for position in range(1, 29)),
)


def test_the_signable_ceiling_is_thirty_rows_and_gate_seats():
    """The ceiling counted gates while a gate was one signature; it is seats.

    Twenty one roster rows and nine gate seats are thirty slots. The most people
    they can hold is twenty nine, because the documents name one person for both
    G09 and E04 and those two slots are therefore one hand.
    """
    assert len(close_programme.HUMAN_SIGNOFF_REQUIREMENTS) == 21
    assert close_programme.MINIMUM_DISTINCT_SIGNER_FLOOR == 9
    assert SIGNABLE_TOTAL == 30
    assert len(CEILING_ASSESSORS) == 29
    assert len(set(close_programme.SOURCE_NAMED_SIGNERS.values())) == 1


def test_every_row_and_every_gate_seat_can_credit_a_signer_of_its_own():
    """A gate seat is a coverage slot of its own, so a population is people.

    Twenty nine people hold thirty slots between them: one of them holds both of
    the slots the documents name a person for and everybody else holds one.
    """
    named = close_programme.SOURCE_NAMED_SIGNERS
    # The first placeholder is the one hand that holds both named slots; every
    # other slot takes the next placeholder and holds nothing else.
    spare = list(CEILING_ASSESSORS[1:])
    roster = {
        requirement: named.get(requirement) or spare.pop(0)
        for requirement in sorted(HUMAN_SIGNOFF_IDS)
    }
    records = {}
    for gate in EXIT_GATE_IDS:
        for seat in range(GATE_SIGNER_COUNTS[gate]):
            actor = (named.get(gate) if seat == 0 else None) or spare.pop(0)
            records[GATE_REFERENCES[gate][seat]] = exit_gate_record(
                gate, seat=seat, signoff=gate_signoff(gate, actor=actor)
            )
    assert not spare
    result = certify(
        full_evidence(),
        proofs=signers_of(roster, "Reviewed this row on the final candidate."),
        exit_gate_records=records,
        issuers=frozenset(CEILING_ASSESSORS),
        minimum_distinct_signers=len(CEILING_ASSESSORS),
    )
    assert result["signers"] == identities_of(CEILING_ASSESSORS)
    assert result["signer_rejection"] is None
    assert result["minimum_distinct_signers"] == 29
    assert result["closable"] is True


def test_an_exit_gate_alone_credits_an_independent_signer():
    """A signer who holds no roster row and one gate seat is still a signer."""
    result = certify(
        full_evidence(),
        proofs=signers_of(
            roster_signed_by("albert"),
            "Reviewed this row on the final candidate.",
        ),
        issuers=ASSESSORS,
        minimum_distinct_signers=close_programme.MINIMUM_DISTINCT_SIGNER_FLOOR,
    )
    assert result["signers"] == identities_of(ISSUERS)
    assert result["signer_rejection"] is None
    assert result["closable"] is True


# The seat total the module records, and how it is reported


@pytest.mark.parametrize("minimum", [1, 2, 3, 4, 5, 6, 7, 8, 9])
def test_a_minimum_at_or_under_the_documented_seat_total_is_reported_beside_it(
    minimum,
):
    """A caller under the seat total is not refused, and reads both numbers.

    The answer is not to refuse the configuration: a low bound is weaker than
    the populations the exits carry, not incoherent, and refusing it would
    refuse a caller who asked for less than they will get. What the caller is
    owed is the arithmetic, so the result carries the seats the documents name
    beside the number they stated. It is a count of seats. One identity may
    hold a seat on two exits, so it is not a count of people this run was held
    to, and this test does not show that it is.
    """
    result = certify(full_evidence(), minimum_distinct_signers=minimum)
    assert result["minimum_distinct_signers"] == minimum
    assert result["effective_minimum_distinct_signers"] == 9
    assert result["effective_minimum_distinct_signers"] == (
        close_programme.MINIMUM_DISTINCT_SIGNER_FLOOR
    )
    assert result["signer_rejection"] is None
    assert result["closable"] is True


def test_a_minimum_above_the_floor_is_the_number_that_binds():
    """Above the floor the caller's number is the one that holds, and is named."""
    result = certify_multi_signer(minimum_distinct_signers=len(ASSESSORS))
    assert result["minimum_distinct_signers"] == 10
    assert result["effective_minimum_distinct_signers"] == 10
    assert result["signer_rejection"] is None
    assert result["closable"] is True


def test_a_refused_closure_still_reports_the_seats_beside_the_stated_number():
    """Both numbers are reported whether or not the closure is closable."""
    lead = GATE_SIGNERS["E02"][0]
    result = certify(
        full_evidence(),
        exit_gate_records=exit_seats_signed_by("E02", (lead, lead)),
        minimum_distinct_signers=1,
    )
    assert result["minimum_distinct_signers"] == 1
    assert result["effective_minimum_distinct_signers"] == 9
    assert result["closable"] is False


def test_the_module_records_the_signer_floor_the_documents_write_down():
    """The test named for the document read a constant and three substrings.

    It asserted that the floor equalled the length of a list in the same module
    and that three phrases appeared in the same module's docstring. The floor is
    a claim about a document, so it is read off the document line the crosswalk
    quotes, which is where the reviewer populations are written down.
    """
    counts = documented_gate_signer_counts()
    assert counts == GATE_SIGNER_COUNTS
    assert close_programme.PROGRAMME_EXIT_GATE_SIGNERS == counts
    assert close_programme.MINIMUM_DISTINCT_SIGNER_FLOOR == sum(counts.values())
    assert close_programme.MINIMUM_DISTINCT_SIGNER_FLOOR == 9
    # Five populations was never five people: the line names three for one exit
    # and two for another before it names a single reviewer for a third.
    assert close_programme.MINIMUM_DISTINCT_SIGNER_FLOOR != len(
        close_programme.PROGRAMME_EXIT_GATES
    )
    text = " ".join(close_programme.__doc__.split())
    assert "which is five distinct people" not in text
    assert "No single number for minimum_distinct_signers is written down" in text
    assert (
        "Five reviewer populations is not five people" in text
    )


def test_the_module_says_plainly_what_the_reported_number_is_and_is_not():
    """The docstring claimed a rule across the exits the documents do not state.

    It said the five exits carry nine distinct identities between them whatever
    the caller asks for, which was true only of the strict reading that refused
    two exits one signer. That reading is gone, so the prose says what the
    number is: the seats the populations come to, and not a count of people.
    """
    text = " ".join(close_programme.__doc__.split())
    assert "the rule that no two exits share a name is conditioned" not in text
    assert "a configured 1 turns the exit rule off" not in text
    assert (
        "The rule that no two exits are held by one identity is unconditional"
        not in text
    )
    assert (
        "effective_minimum_distinct_signers as the floor that actually bound"
        not in text
    )
    assert "That per exit rule is unconditional" in text
    assert (
        "It is not a floor on how many different people a closure needs" in text
    )
    assert (
        "name nothing against one person holding a seat on two exits" in text
    )
    assert (
        "a reader should not take it for the number of people a closure was"
        " held to" in text
    )


def test_the_module_records_that_nothing_calls_the_validated_path():
    text = " ".join(close_programme.__doc__.split())
    assert "certify_requirements has no caller anywhere in this repository" in text
    assert "is no longer exported" in text
    assert "accepts a row on the truthiness of its proof ids alone" not in text


def test_the_kernel_shape_check_is_not_exported():
    """It was the shape anything downstream would reach for, and it closed the
    programme on a proof list that was empty to iteration."""
    assert not hasattr(close_programme, "unmet_requirements")
    assert "unmet_requirements" not in dir(close_programme)


@pytest.mark.parametrize(
    "proof_ids",
    [HollowProofIds(), {"proof-F11"}, "proof-F11", [7], ["p", "p"], [""]],
)
def test_the_shape_check_reads_a_proof_list_it_cannot_read_as_unmet(proof_ids):
    """The row was accepted on bool(proof_ids) and never read any further."""
    evidence = {"F11": {"state": "achieved", "proof_ids": proof_ids}}
    assert _unmet_requirements({"F11"}, evidence) == ["F11"]


def test_the_shape_check_reads_a_row_that_is_not_a_row_as_unmet():
    assert _unmet_requirements({"F11"}, {"F11": ["achieved"]}) == ["F11"]
    assert _unmet_requirements({"F11"}, {"F11": {"state": 7}}) == ["F11"]


def test_the_signature_field_set_is_pinned():
    """M: the closed set widened by one name no test happened to spell."""
    assert close_programme.SIGNATURE_FIELDS == frozenset(
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
    assert close_programme.DECLARED_EVIDENCE_FIELDS == frozenset(
        {"evidence_artifacts", "attachments"}
    )
    assert close_programme.ACTOR_DECLARING_FIELDS == frozenset(
        {"actor_kind", "origin_kind", "produced_by", "producer", "signer_kind"}
    )


def test_a_proof_id_entry_that_is_not_a_list_is_refused():
    """M: the check widened past list, and a set was adjudicated instead."""
    evidence = full_evidence()
    evidence["A01"] = {"state": "achieved", "proof_ids": {"proof-A01"}}
    with pytest.raises(ValueError, match="evidence_row_invalid: A01"):
        certify(evidence)


@pytest.mark.parametrize("accepted", [1, 1.0, "true", "True"])
def test_an_acceptance_that_is_not_the_boolean_true_is_not_an_acceptance(accepted):
    """M: the flag compared loosely, so the integer one became an acceptance."""
    evidence = full_evidence()
    evidence["BSA07"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {"decision-7": decision_record("BSA07", accepted=accepted)}
    assert certify(evidence, decisions=decisions)["rejected"] == {
        "BSA07": "decision_scope_delta_not_accepted"
    }


def test_the_normalisation_says_what_it_is_and_what_it_is_not():
    """The docstring claimed the invisible characters and caught two categories."""
    text = " ".join(close_programme._normalised_name.__doc__.split())
    assert "not the control" in text
    assert "convenience" in text
    assert "Identity is decided by the roster the caller supplies" in text


def test_the_module_says_identity_is_the_callers_roster():
    text = " ".join(close_programme.__doc__.split())
    assert "counted by identity" in text
    assert "never by name" in text


# The vendored crosswalk is read for what it says, not only for its shape. It is
# a tripwire and not evidence: what follows checks that the artefact is
# internally honest and agrees with the module in this tree.


MASTER_PLAN = "docs/plans/2026-09-12-42-staging-completion.md"
LEDGER_DOCUMENT = "BACKEND_ACCEPTANCE_LEDGER.md"
DECISIONS_DOCUMENT = "42_ALBERT_DECISIONS_20260913.md"
EVALUATION_DOCUMENT = (
    "docs/plans/2026-09-12-42-evaluation-certification.md"
)
# The acceptance crosswalk table runs one row per line, and these are the lines.
REQUIREMENT_TABLE_LINES = range(191, 275)
# The register rows whose responsibility column names a person, by line.
ROSTER_LOCATORS = {
    "P106": 180,
    "P203": 183,
    "P204": 184,
    "P306": 197,
    "G07": 227,
    "G09": 229,
    "F05": 241,
    "BSA01": 271,
    "BSA02": 272,
    "BSA03": 273,
    "BSA04": 274,
    "BSA05": 275,
    "BSA06": 276,
    "BSA07": 277,
    "BSA08": 278,
    "BSA09": 279,
    "BSA10": 280,
    "BSA11": 281,
    "BSA12": 282,
}
# The two lines the gates are cited to, pinned whole. Checking that a gate
# identifier and the words "human gates" appear somewhere in the quoted line let
# the line be rewritten to say the opposite while keeping both substrings, which
# is the mutation that survived. The line is compared entire, and the part of it
# that carries the gates is parsed and read as the list it is.
DECISIONS_GATE_LINE = (
    "Name the real reviewers: three strategist roles for E02, two blind claim"
    " reviewers for E01, cluster reviewers for L01 closed week review, and the"
    " client reviewer for B04 | E01, E02, L01, B04 human gates"
)
EVALUATION_GATE_LINE = (
    "- [ ] Obtain Albert's explicit candidate acceptance. Close the programme"
    " only if the requirement checker returns an empty list and the human"
    " decisions apply to that candidate. A changed source, contract or UI dist"
    " reopens affected evidence."
)
# The tail of the decisions line: the gates it names, and nothing else.
DECISIONS_GATE_CLAIM = re.compile(
    r"(?P<gates>[A-Z]\d{2}(?:, [A-Z]\d{2})*) human gates"
)
# The head of the decisions line names a reviewer population per exit: a run of
# lower case words, then "for", then the exit. Each clause is read whole.
DECISIONS_POPULATION_CLAUSE = re.compile(
    r"(?P<population>[a-z][a-z ]*?) for (?P<gate>[A-Z]\d{2})"
)
# The number words the line may use. Anything larger is not on it and reading a
# number that is not written there would be inventing one.
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def documented_gate_signer_counts():
    """How many people each exit needs, read off the lines that name them.

    A population written with a numeral in front of it is that many people. A
    population written as a bare plural, "cluster reviewers", names more than
    one and no more than that, so it is read as two, which is the least a plural
    admits and the only count that line supports without inventing one. A
    population written in the singular, "the client reviewer", is one. E04 is
    not on that line at all: its own exit step requires one named person's
    explicit acceptance, which is one.
    """
    counts = {}
    head = DECISIONS_GATE_LINE.split(" | ")[0]
    for clause in DECISIONS_POPULATION_CLAUSE.finditer(head):
        words = clause.group("population").split()
        if words[0] in NUMBER_WORDS:
            counts[clause.group("gate")] = NUMBER_WORDS[words[0]]
        elif words[-1].endswith("s"):
            counts[clause.group("gate")] = 2
        else:
            counts[clause.group("gate")] = 1
    assert EVALUATION_GATE_LINE.startswith(
        "- [ ] Obtain Albert's explicit candidate acceptance."
    )
    counts["E04"] = 1
    return {gate: counts[gate] for gate in EXIT_GATE_IDS}
# Four gates come from one line of the decisions record; E04 from its own step.
GATE_LOCATORS = {
    "E01": (DECISIONS_DOCUMENT, 13),
    "E02": (DECISIONS_DOCUMENT, 13),
    "L01": (DECISIONS_DOCUMENT, 13),
    "B04": (DECISIONS_DOCUMENT, 13),
    "E04": (EVALUATION_DOCUMENT, 400),
}
# The criterion the roster entries state, and what satisfying it looks like. The
# responsibility column is a list of responsible parties, written with "+", ","
# or "and" between them. A part is read whole and matched whole: searching for a
# substring accepted "no human reviewer" and "not for human reviewers" as rows
# that name a person, which is the mutation that survived.
RESPONSIBILITY_SEPARATORS = re.compile(r"\s*(?:\+|,|\band\b)\s*")
RESPONSIBILITY_PART_NAMES_A_PERSON = re.compile(
    r"(?:designated\s+)?human\s+reviewers?|reviewers|albert", re.IGNORECASE
)
# The digests of the thirteen documents that were read. They are carried, not
# checked: the documents live outside this repository, so pinning them here is
# what stops the artefact's own copy being rewritten on its own.
CROSSWALK_SOURCE_DIGESTS = {
    "docs/plans/2026-09-12-42-staging-completion.md": (
        "c76fe5e2a7c97f829cbcebf765d3d1bcddfb3503d90bfdce0cbb83d6834efa3d"
    ),
    "docs/plans/42-plan-validation.json": (
        "1cd2bf576bf22a033be5421b95c503bd2665802ac5bc7ed8adfac9e6365d726f"
    ),
    "BACKEND_ACCEPTANCE_LEDGER.md": (
        "0b8a91f8441073c03297ed51ce12494e51b0cac842b0d952ff23e75fa1f45de3"
    ),
    "42_ALBERT_DECISIONS_20260913.md": (
        "f8f87a37e3cd388ba6d6fe335e0f5077b46d552d393e70c13c58dc2f08dcff0c"
    ),
    "docs/plans/2026-09-12-42-evaluation-certification.md": (
        "b35b413783952796a08fe58e0d19f52b14242065e5820e5d2c8189d660e6097b"
    ),
    "docs/plans/2026-09-12-42-bsa-first-client.md": (
        "e6082e4539bfcbd84b3732465d1543fa7ad4769d4b4bb786c68feca90748ccbf"
    ),
    "docs/plans/2026-09-12-42-data-intelligence.md": (
        "f75a806de67241e13ee7b110ff09649a758607a0e368b028aa1b4f2d8fd0c248"
    ),
    "docs/plans/2026-09-12-42-plan-contracts.md": (
        "9d4dc479959cd3c30bf0638516ef1fcf9bf54d5640a231046c7f7319ad350a1e"
    ),
    "docs/plans/2026-09-12-42-product-journeys.md": (
        "bc6cee728e0b9f21989a33d9d540c1fa55b5231d16277888d083e7b9477f524b"
    ),
    "docs/plans/2026-09-12-42-staging-foundation.md": (
        "7e05601ffd9d5daf47c216342f431f33fdb2f99682d3f134926d9ec2b3abc8cc"
    ),
    "docs/plans/42-plan-review-resolution.md": (
        "b42c7c9db41c0249c31e3a527c264ef5127268c1aa66d40b2654e34ed20c27d6"
    ),
    "42_HANDOVER_CHECKPOINT_2026-09-14.md": (
        "c98155dfcc50435ddb5ceae951217f58112ea7b2874b5ab8f4eb7ba61ae67bb2"
    ),
    "task-state.json": (
        "bd1103669da336e2d64c5db1e946daca5c78128cf9dbe7386a6b99529736bed2"
    ),
}


def test_the_crosswalk_carries_the_digest_of_every_document_it_read():
    """M: every source digest replaced with zeroes passed the shape check."""
    digests = closure_crosswalk()["source_documents_sha256"]
    assert digests == CROSSWALK_SOURCE_DIGESTS
    assert len(set(digests.values())) == 13


def test_every_requirement_is_cited_to_its_own_row_of_the_acceptance_table():
    """M: every citation pointed at line 1 and the shape check was content."""
    entries = closure_crosswalk()["requirement_ids"]["entries"]
    assert list(entries) == list(close_programme.REQUIREMENT_IDS)
    assert [entry["line"] for entry in entries.values()] == list(
        REQUIREMENT_TABLE_LINES
    )
    for identifier, entry in entries.items():
        assert entry["document"] == MASTER_PLAN, identifier
        assert entry["corroborating_document"] == (
            "docs/plans/42-plan-validation.json"
        )
        tasks = entry["owning_or_proving_tasks"]
        assert tasks, identifier
        assert all(re.fullmatch(r"[A-Z]\d{2}", task) for task in tasks), identifier
        assert entry["text"] == f"| {identifier} | {', '.join(tasks)} |", identifier


def test_every_roster_row_is_cited_to_a_register_row_that_names_a_person():
    """M: the criterion was carried beside text that does not satisfy it."""
    entries = closure_crosswalk()["human_signoff_requirements"]["entries"]
    assert {
        identifier: entry["line"] for identifier, entry in entries.items()
    } == ROSTER_LOCATORS
    for identifier, entry in entries.items():
        assert entry["document"] == LEDGER_DOCUMENT, identifier
        assert entry["criterion"] == (
            "register responsibility column names a person"
        ), identifier
        # The whole column is parsed into the parties it lists, and one of them
        # has to be a person when it is read whole. A substring anywhere in the
        # column is not that, and was not this criterion.
        parts = [
            part.strip()
            for part in RESPONSIBILITY_SEPARATORS.split(entry["responsibility"])
        ]
        assert all(parts), (identifier, entry["responsibility"])
        assert any(
            RESPONSIBILITY_PART_NAMES_A_PERSON.fullmatch(part) for part in parts
        ), (identifier, entry["responsibility"])
    assert len({entry["responsibility"] for entry in entries.values()}) == 7


def test_every_exit_gate_is_cited_to_the_text_that_makes_it_a_human_gate():
    """M: the sourced line was rewritten to say the opposite of what it says.

    The substrings the check grepped for survived that rewrite, so the quoted
    line is compared whole here and the claim inside it is parsed: the gates the
    line names are read out of it and must be exactly the gates cited to it.
    """
    section = closure_crosswalk()["programme_exit_gates"]
    entries = section["entries"]
    assert {
        identifier: (entry["document"], entry["line"])
        for identifier, entry in entries.items()
    } == GATE_LOCATORS
    decided = [
        gate for gate in entries if entries[gate]["document"] == DECISIONS_DOCUMENT
    ]
    assert decided == ["E01", "E02", "L01", "B04"]
    claim = DECISIONS_GATE_CLAIM.search(DECISIONS_GATE_LINE)
    assert claim is not None
    assert claim.group("gates").split(", ") == decided
    for gate in decided:
        entry = entries[gate]
        assert entry["text"] == DECISIONS_GATE_LINE, gate
        assert entry["criterion"] == (
            "named by the decisions record as a human gate"
        ), gate
    assert entries["E04"]["text"] == EVALUATION_GATE_LINE
    assert EVALUATION_GATE_LINE.startswith(
        "- [ ] Obtain Albert's explicit candidate acceptance."
    )
    assert entries["E04"]["criterion"] == (
        "E04's own exit step requires the owner's explicit candidate acceptance"
    )
    # The same line names a reviewer population per exit, and the module carries
    # those counts. Read them out of the line here, so the module's copy and the
    # document's own words cannot drift apart without this test saying so.
    counts = documented_gate_signer_counts()
    assert set(counts) == set(entries)
    assert counts == {"E01": 2, "E02": 3, "L01": 2, "B04": 1, "E04": 1}
    assert close_programme.PROGRAMME_EXIT_GATE_SIGNERS == counts
    assert sum(counts.values()) != len(entries)
    # B04, E01 and L01 were still recorded here as gates the module did not
    # protect, two commits after it started protecting them.
    assert section["sourced_not_in_module"] == []
    assert section["in_module_not_sourced"] == []
    assert section["agrees_with_module"] is True


def test_the_crosswalk_is_compared_against_the_module_in_this_tree():
    """M: the snapshot named a commit two behind, from when the gates were two."""
    crosswalk = closure_crosswalk()
    snapshot = crosswalk["module_under_comparison"]
    assert snapshot["path"] == "ops/certification/close_programme.py"
    assert "commit" not in snapshot
    assert set(snapshot["literals"]) == {
        "REQUIREMENT_IDS",
        "HUMAN_SIGNOFF_REQUIREMENTS",
        "PROGRAMME_EXIT_GATES",
    }
    source = Path(close_programme.__file__).read_text(encoding="utf-8").split("\n")
    for name, line in snapshot["literals"].items():
        assert source[line - 1].startswith(f"{name} = "), (name, line)
    assert crosswalk["requirement_ids"]["agrees_with_module"] is True
    assert crosswalk["human_signoff_requirements"]["agrees_with_module"] is False
    assert crosswalk["requirement_ids"]["sourced_not_in_module"] == []
    assert crosswalk["requirement_ids"]["in_module_not_sourced"] == []


def test_the_crosswalk_says_it_is_a_tripwire_and_not_evidence():
    """Six of eight artefact mutations survived: this file is not evidence.

    The three copies it compares, module, artefact and test, all sit in this
    commit, and the thirteen documents do not. What these tests catch is one
    copy drifting from the others. A derivation that was wrong when it was made,
    or rewritten deliberately on every side at once, is not something they can
    see, and the README must say so where a reader will meet it.
    """
    readme = (CLOSURE_CROSSWALK_PATH.parent / "README.md").read_text(
        encoding="utf-8"
    )
    text = " ".join(readme.split())
    assert "tripwire" in text
    assert "It is a tripwire, not evidence" in text
    assert "it cannot detect a wrong derivation" in text
    assert "instead of against a copy of the module's own expression" not in text


# Internal spacing is spelling too, and spelling is not identity


# One placeholder name under four spacings: a doubled space, a line break and a
# no break space, all of which collapse to the one name.
INTERNAL_SPACING_SPELLINGS = (
    "assessor one",
    "assessor  one",
    "Assessor\none",
    "assessor\u00a0one",
)
SPACED_ASSESSOR = "assessor one"


def test_internal_spacing_does_not_make_a_second_assessor():
    """Stripping the ends of a name is not collapsing the spacing inside it."""
    gates = gates_signed_by(
        dict(zip(ANY_ISSUER_GATES, INTERNAL_SPACING_SPELLINGS))
    )
    result = certify(
        full_evidence(),
        proofs=signers_of(
            roster_signed_by("lerato"), "Reviewed on the final candidate myself."
        ),
        exit_gate_records=gates,
        issuers=[
            *INTERNAL_SPACING_SPELLINGS,
            "lerato",
            "nomsa",
            "sipho",
            "thabo",
            "albert",
            *GATE_CO_SIGNERS,
        ],
        minimum_distinct_signers=8,
    )
    assert result["signers"] == identities_of(
        {SPACED_ASSESSOR, "lerato", "albert", *GATE_CO_SIGNERS}
    )
    assert result["signer_rejection"] == "insufficient_distinct_signers"
    assert result["closable"] is False


# What the anchor separation does, and what it does not do


def test_the_module_says_a_human_anchor_is_a_key_and_not_a_person():
    """The docstring said the separation kept a machine out of the human rows.

    It keeps the routine anchor out. A human trust anchor is a key, nothing
    binds that key to the identity a signature names, and one holder of one such
    key closes every row and every gate the programme reserves for a person. The
    module said the opposite of that and now says it plainly.
    """
    text = " ".join(close_programme.__doc__.split())
    assert (
        "separate from the anchors that seal routine machine evidence, so the"
        " process that produces CI output cannot mint a human sign off"
        not in text
    )
    assert "It does not keep a machine out of the human rows." in text
    assert "A human trust anchor is a key and not a person" in text
    assert (
        "nothing here binds a key to the identity a signature names" in text
    )
    assert (
        "cannot tell a machine holding a human anchor key from the person that"
        " key was issued to" in text
    )
    assert (
        "Any single holder of any one human anchor key mints every approval the"
        " programme reserves for a person" in text
    )
    assert (
        "the identity roster, the reviewer populations the exits are held to"
        " and the signer list this result reports are defeated together" in text
    )
    assert (
        "The key identifier and the signature are carried in the signature"
        " block and neither is ever read." in text
    )


def test_the_module_discloses_the_three_fields_it_carries_and_never_reads():
    """Role, key identifier and signature widen a set and are read by nothing.

    The third was presented as a fix for a block that had no room for the
    signature. There is room and there is no reader, and the module says so
    rather than leaving the framing to a commit message.
    """
    text = " ".join(close_programme.__doc__.split())
    assert (
        "Three of the names a signature block may carry are carried and never"
        " read." in text
    )
    assert "no roster of roles is an input here" in text
    assert (
        "reading it is the whole of the binding this module does not have"
        in text
    )
    assert (
        "authentication is the digest this module recomputes with the anchor's"
        " own key, and the signature field adds nothing to it" in text
    )
    assert "role" in close_programme.SIGNATURE_FIELDS
    assert "key_id" in close_programme.SIGNATURE_FIELDS
    assert "signature" in close_programme.SIGNATURE_FIELDS


# The production surface is walked at every depth the comment claims


@pytest.mark.parametrize(
    "mutation",
    [
        {"compatibility_bridge": {"produced_by": "nightly-certification-agent"}},
        {"scope_delta": {"producer": "the release pipeline"}},
        {"bridges": [{"origin_kind": "ai"}]},
        {"gate": {"signer_kind": "bot"}},
    ],
)
def test_a_machine_declared_under_a_nested_kernel_field_is_refused(mutation):
    """The collector read the top level, the signature and the provenance only.

    A field of this kernel that carries a mapping was never walked, so a machine
    declaration nested one level inside an unrelated field closed a roster row.
    """
    proofs = full_proofs()
    proofs["proof-P203"] = signed_proof_with("P203", **mutation)
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "P203": "proof_human_signoff_not_human"
    }


def test_an_exit_gate_declaring_a_machine_under_a_nested_field_is_refused():
    records = full_exit_gate_records()
    records["gate-B04"] = exit_gate_record(
        "B04",
        signoff=gate_signoff("B04"),
        scope_delta={"produced_by": "the certification runner"},
    )
    result = certify(full_evidence(), exit_gate_records=records)
    assert result["exit_gates"] == {
        "B04": "exit_gate_human_signoff_not_human"
    }
    assert result["closable"] is False


def module_comment_text():
    """The module source as one line, with the comment markers taken off.

    A comment is pinned the way a docstring is, so the marker at the start of
    each line must not be part of what is searched for.
    """
    source = Path(close_programme.__file__).read_text(encoding="utf-8")
    stripped = [re.sub(r"^\s*#\s?", "", line) for line in source.split("\n")]
    return " ".join(" ".join(stripped).split())


def test_the_comment_states_the_surface_the_collector_actually_walks():
    """The comment claimed any depth and the collector walked three places."""
    text = module_comment_text()
    assert (
        "The surface a record speaks about its own production with is the whole"
        " record apart from the material a signature is about" in text
    )
    assert (
        "so a machine declaration nested under any other field was never looked"
        " at" in text
    )


# A row or a gate the documents source to a named person takes that person


def documented_named_signers():
    """The rows and gates the vendored crosswalk sources to a person by name.

    Read out of the artefact rather than restated: a roster row whose
    responsibility column lists the owner as one of its parties, and a gate
    whose sourced text requires that person's own acceptance.
    """
    crosswalk = closure_crosswalk()
    named = {}
    for identifier, entry in crosswalk["human_signoff_requirements"][
        "entries"
    ].items():
        parts = [
            part.strip()
            for part in RESPONSIBILITY_SEPARATORS.split(entry["responsibility"])
        ]
        for part in parts:
            folded = part.casefold()
            if folded == "albert":
                named[identifier] = folded
    for identifier, entry in crosswalk["programme_exit_gates"]["entries"].items():
        if "Albert's explicit candidate acceptance" in entry["text"]:
            named[identifier] = "albert"
    return named


def test_the_rows_and_gates_sourced_to_a_person_are_taken_from_the_crosswalk():
    """M: a required identity invented in the module would source to nothing."""
    named = documented_named_signers()
    assert named == {"G09": "albert", "E04": "albert"}
    assert close_programme.SOURCE_NAMED_SIGNERS == named


def test_a_row_sourced_to_a_person_refuses_a_signature_by_anyone_else():
    """The register names the owner on G09 and any accepted issuer signed it."""
    proofs = full_proofs()
    proofs["proof-G09"] = proof_record(
        "G09",
        signoff=human_signoff(
            actor="lerato", statement="Reviewed G09 on the final candidate."
        ),
    )
    result = certify(full_evidence(), proofs=proofs)
    assert result["rejected"] == {
        "G09": "proof_human_signoff_required_signer_mismatch"
    }
    assert result["unmet"] == ["G09"]
    assert result["closable"] is False


def test_the_owner_exit_refuses_a_signature_by_anyone_else():
    """E04's own exit step requires the owner's explicit acceptance."""
    records = full_exit_gate_records()
    records["gate-E04"] = exit_gate_record(
        "E04",
        signoff=human_signoff(
            actor="lerato", statement="Exit of E04 accepted on the candidate."
        ),
    )
    result = certify(full_evidence(), exit_gate_records=records)
    assert result["exit_gates"] == {
        "E04": "exit_gate_human_signoff_required_signer_mismatch"
    }
    assert result["closable"] is False


def test_an_amendment_of_a_row_sourced_to_a_person_refuses_another_issuer():
    """Amending the owner's row is closing it, and it is held the same way."""
    evidence = full_evidence()
    evidence["G09"] = {"state": "amended", "user_decision_id": "decision-7"}
    decisions = {
        "decision-7": decision_record(
            "G09",
            issuer="lerato",
            signoff=human_signoff(
                actor="lerato", statement="Amended G09 on the final candidate."
            ),
        )
    }
    result = certify(evidence, decisions=decisions)
    assert result["rejected"] == {
        "G09": "decision_human_signoff_required_signer_mismatch"
    }
    assert result["closable"] is False


def test_a_row_sourced_to_a_person_the_closure_does_not_accept_cannot_close():
    """A required name the run never accepted is a refusal, not a free pass."""
    proofs = full_proofs()
    proofs["proof-G09"] = proof_record(
        "G09",
        signoff=human_signoff(
            actor="lerato", statement="Reviewed G09 on the final candidate."
        ),
    )
    result = certify(
        full_evidence(), proofs=proofs, issuers=frozenset(ISSUERS - {"albert"})
    )
    assert result["rejected"]["G09"] == (
        "proof_human_signoff_required_signer_unavailable"
    )
    assert result["closable"] is False


def test_the_named_person_still_closes_the_row_and_the_gate_they_are_named_on():
    """The rule refuses another name; it does not refuse the name it requires."""
    result = certify(full_evidence())
    assert "G09" not in result["rejected"]
    assert "E04" not in result["exit_gates"]


# Each exit needs the population the documents name, not one signature


def test_each_exit_needs_as_many_distinct_people_as_its_documented_population():
    """One signature per exit could not express a population of three at all."""
    for gate, count in GATE_SIGNER_COUNTS.items():
        if count < 2:
            continue
        short = {
            name: (references[:-1] if name == gate else references)
            for name, references in GATE_REFERENCES.items()
        }
        result = certify(full_evidence(), exit_gates=short)
        assert result["exit_gates"] == {
            gate: "exit_gate_signers_insufficient"
        }, gate
        assert result["closable"] is False


def test_one_person_cannot_hold_two_seats_of_one_exit():
    """Three seats filled by two people is not three reviewers."""
    records = full_exit_gate_records()
    records[GATE_REFERENCES["E02"][2]] = exit_gate_record(
        "E02",
        seat=2,
        signoff=gate_signoff("E02", actor=GATE_SIGNERS["E02"][0]),
    )
    result = certify(full_evidence(), exit_gate_records=records)
    assert result["exit_gates"] == {"E02": "exit_gate_signers_insufficient"}
    assert result["closable"] is False


def test_every_seat_of_an_exit_is_adjudicated_and_not_only_the_first():
    """A second seat was never read at all when a gate carried one reference."""
    records = full_exit_gate_records()
    records[GATE_REFERENCES["E01"][1]] = exit_gate_record(
        "E01", seat=1, signoff=gate_signoff("E01", seat=1), revoked_at=RECORDED_AT
    )
    result = certify(full_evidence(), exit_gate_records=records)
    assert result["exit_gates"] == {"E01": "exit_gate_revoked"}
    assert result["closable"] is False


def test_a_seat_of_one_exit_may_be_held_by_a_signer_of_another():
    """A person sits on two exits, and every population is still filled.

    The exits carry a reviewer population each and the documents name no rule
    against one person holding a seat on two of them, so this closes. Each exit
    is still counted in identities against its own line.
    """
    records = full_exit_gate_records()
    records[GATE_REFERENCES["E02"][2]] = exit_gate_record(
        "E02",
        seat=2,
        signoff=gate_signoff("E02", actor=GATE_SIGNERS["L01"][1]),
    )
    result = certify(full_evidence(), exit_gate_records=records)
    assert result["exit_gates"] == {}
    assert result["signer_rejection"] is None
    assert result["closable"] is True


def test_an_exit_short_of_its_documented_count_is_still_refused():
    """The companion of the rule above: the per exit count is untouched.

    One person may sit on two exits. What no exit will take is fewer distinct
    identities than the documents name for it, and the seat borrowed from
    another exit above does not buy that either.
    """
    for gate, count in GATE_SIGNER_COUNTS.items():
        if count < 2:
            continue
        borrowed = next(
            name for other, names in GATE_SIGNERS.items() if other != gate
            for name in names
        )
        records = exit_seats_signed_by(gate, (borrowed,) * count)
        result = certify(full_evidence(), exit_gate_records=records)
        assert result["exit_gates"] == {
            gate: "exit_gate_signers_insufficient"
        }, gate
        assert result["closable"] is False, gate


def test_four_people_close_the_five_exits_between_them():
    """The roster the closure is actually run against is four people.

    Four named reviewers, every exit filled to the count its own line names and
    the owner's own exit signed by him. This is the reachable case, and a rule
    that no two exits share a signer would put it out of reach: the five exits
    name nine seats between them, and E02 alone takes three.
    """
    four = ("albert", "lerato", "naledi", "nomsa")
    seating = {
        "E01": ("lerato", "naledi"),
        "E02": ("lerato", "naledi", "nomsa"),
        "L01": ("nomsa", "lerato"),
        "B04": ("naledi",),
        "E04": ("albert",),
    }
    records = {}
    for gate, names in seating.items():
        assert len(names) == GATE_SIGNER_COUNTS[gate], gate
        for seat, actor in enumerate(names):
            records[GATE_REFERENCES[gate][seat]] = exit_gate_record(
                gate, seat=seat, signoff=gate_signoff(gate, actor=actor)
            )
    result = certify(
        full_evidence(),
        exit_gate_records=records,
        issuers=four,
        minimum_distinct_signers=len(four),
    )
    assert result["signers"] == identities_of(four)
    assert result["exit_gates"] == {}
    assert result["signer_rejection"] is None
    assert result["closable"] is True


@pytest.mark.parametrize(
    "offered", [(), "", ["gate-E01", ""], ["gate-E01", 7], ["gate-E01", "gate-E01"]]
)
def test_a_gate_reference_list_that_is_not_a_list_of_references_is_refused(offered):
    references = {**GATE_REFERENCES, "E01": offered}
    with pytest.raises(ValueError, match="exit_gate_reference_invalid"):
        certify(full_evidence(), exit_gates=references)


def test_a_single_reference_still_names_one_seat():
    """An exit whose documented population is one takes one reference plainly."""
    references = {**GATE_REFERENCES, "B04": GATE_REFERENCES["B04"][0]}
    result = certify(full_evidence(), exit_gates=references)
    assert result["exit_gates"] == {}
    assert result["closable"] is True


def test_the_floor_is_the_seats_the_documents_name_and_not_the_gate_count():
    """The number reported is the nine seats, never the five exits.

    Five reviewer populations is not five people and it is not five seats. The
    exits name nine seats between them and that is what is reported. Whether
    nine people fill them is a separate question this does not answer.
    """
    result = certify(full_evidence(), minimum_distinct_signers=2)
    assert result["minimum_distinct_signers"] == 2
    assert result["effective_minimum_distinct_signers"] == 9
    assert result["signer_rejection"] is None
    assert result["closable"] is True


def test_a_declaration_entry_that_is_not_a_name_takes_no_field_off_the_surface():
    """M: a declaration read without reading what it holds.

    An empty string is not a field name a record can declare, so a field
    carrying one is not attached material and stays on the surface the record
    speaks about its own production with. Taking the declaration at face value
    let that field off the surface, and the machine declaration inside it was
    then only caught by the account check with a refusal that names the wrong
    thing.
    """
    body = proof_body("P203")
    body["evidence_fields"] = ["", "board_pack"]
    body["board_pack"] = ["s3://42-evidence/board-minute.pdf"]
    body[""] = {"produced_by": "nightly-certification-agent v4"}
    authority, key = anchor_for("P203")
    proofs = full_proofs()
    proofs["proof-P203"] = seal(body, authority=authority, key=key)
    assert certify(full_evidence(), proofs=proofs)["rejected"] == {
        "P203": "proof_human_signoff_not_human"
    }
