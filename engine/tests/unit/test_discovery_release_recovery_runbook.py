"""The recovery runbook names every refusal the release and rollback paths emit."""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_RUNBOOK = _ROOT / "docs" / "runbooks" / "discovery-release-recovery.md"
_RELEASE = _ROOT / "scripts" / "staging" / "release_open_intelligence_run.py"
_ADMISSION = (
    _ROOT / "src" / "analysis" / "open_intelligence" / "general_question_release_admission.py"
)

# A refusal code is the ``code=`` argument of a raised refusal, either bare or
# wrapped in the live profile helper. ``reason_code=`` is telemetry, not a
# refusal, so the pattern refuses to match it.
_REFUSAL = re.compile(r'(?<!reason_)\bcode=(?:_live_code\(profile, )?"([a-z_]+)"')
_TABLED = re.compile(r"^\| `([a-z_]+)` \|", re.MULTILINE)


def _emitted() -> set[str]:
    return set(_REFUSAL.findall(_RELEASE.read_text(encoding="utf-8")))


def _tabled() -> set[str]:
    return set(_TABLED.findall(_RUNBOOK.read_text(encoding="utf-8")))


def test_every_emitted_refusal_code_is_tabled_in_the_runbook():
    missing = sorted(_emitted() - _tabled())
    assert missing == []


def test_the_runbook_tables_no_code_the_release_path_never_emits():
    phantom = sorted(_tabled() - _emitted())
    assert phantom == []


def test_the_six_live_release_refusals_are_tabled():
    tabled = _tabled()
    for code in (
        "release_run_not_completed",
        "release_partitions_incomplete",
        "release_already_released",
        "release_run_empty",
        "release_evidence_differs",
        "release_duplicate",
    ):
        assert code in tabled


def _refusal_calls(source: Path) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ReleaseRefusal"
    ]


def _refusal_code(call: ast.Call) -> tuple[str, str | None]:
    """How one refusal carries its code: through the helper, as a literal, or not."""
    for keyword in call.keywords:
        if keyword.arg != "code":
            continue
        value = keyword.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "_live_code"
        ):
            return ("live", ast.literal_eval(value.args[1]))
        try:
            return ("plain", ast.literal_eval(value))
        except ValueError:
            return ("dynamic", None)
    return ("none", None)


def _is_replay_attribute(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "is_replay"
        and isinstance(test.value, ast.Name)
        and test.value.id == "profile"
    )


def _is_bare_replay_refusal(statement: ast.stmt) -> bool:
    """``if profile.is_replay: raise ReleaseRefusal(<no code>)``, and nothing else."""
    if not isinstance(statement, ast.If) or statement.orelse:
        return False
    if not _is_replay_attribute(statement.test):
        return False
    if len(statement.body) != 1 or not isinstance(statement.body[0], ast.Raise):
        return False
    call = statement.body[0].exc
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "ReleaseRefusal"
        and _refusal_code(call)[0] == "none"
    )


def _hand_written_replay_gates(tree: ast.AST) -> set[str]:
    """Codes dropped by a replay gate written out by hand rather than through the helper.

    ``_live_code`` is one way to write the gate. The other is one condition with
    an inner ``if profile.is_replay`` raising a refusal that carries no code, and
    the coded refusal for a live profile after it. The condition is the same and
    the replay drops the code the same way, so reading only the helper classifies
    a hand written gate as carried always, which is the wrong mark.
    """
    codes: set[str] = set()
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for index, statement in enumerate(block):
                if not _is_bare_replay_refusal(statement):
                    continue
                for following in block[index + 1 :]:
                    for call in ast.walk(following):
                        if (
                            isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Name)
                            and call.func.id == "ReleaseRefusal"
                        ):
                            kind, code = _refusal_code(call)
                            if kind == "plain" and code is not None:
                                codes.add(code)
    return codes


def _live_gated() -> set[str]:
    """Codes the retained replay drops, walked out of the release script.

    Both forms of the gate. ``_live_code`` returns the code for a live profile and
    ``None`` for the retained replay, so a code raised through it is dropped by the
    replay; ``_hand_written_replay_gates`` reads the same gate written out.
    """
    tree = ast.parse(_RELEASE.read_text(encoding="utf-8"))
    codes = set()
    for call in _refusal_calls(_RELEASE):
        kind, code = _refusal_code(call)
        if kind == "live" and code is not None:
            codes.add(code)
    return codes | _hand_written_replay_gates(tree)


def _is_not_replay(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.UnaryOp)
        and isinstance(test.op, ast.Not)
        and _is_replay_attribute(test.operand)
    )


def _release_refusal_reach() -> tuple[dict[str, list[bool]], dict[str, bool]]:
    """Every coded refusal in the release script, against the guard it sits under.

    Returns the per code list of raise sites, each true when that site is only
    evaluated for a live profile, and the per function answer to the same
    question. A site is guarded when it sits inside a ``not profile.is_replay``
    block, or inside a helper every call site of which is itself guarded. The
    helper answer is settled by repeating the pass until it stops moving.
    """
    tree = ast.parse(_RELEASE.read_text(encoding="utf-8"))
    raises: list[tuple[str, str, bool]] = []
    calls: list[tuple[str, bool, str]] = []

    def walk(node: ast.AST, function: str, guarded: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name, False)
                continue
            if isinstance(child, ast.If):
                inner = guarded or _is_not_replay(child.test)
                for statement in child.body:
                    walk(statement, function, inner)
                for statement in child.orelse:
                    walk(statement, function, guarded)
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                if child.func.id == "ReleaseRefusal":
                    kind, code = _refusal_code(child)
                    if kind == "plain" and code is not None:
                        raises.append((function, code, guarded))
                else:
                    calls.append((child.func.id, guarded, function))
            walk(child, function, guarded)

    walk(tree, "<module>", False)
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    live_only = dict.fromkeys(names, False)
    for _ in range(len(names) + 1):
        settled = True
        for name in names:
            sites = [(guard, caller) for callee, guard, caller in calls if callee == name]
            answer = bool(sites) and all(
                guard or live_only.get(caller, False) for guard, caller in sites
            )
            if answer != live_only[name]:
                live_only[name] = answer
                settled = False
        if settled:
            break
    by_code: dict[str, list[bool]] = {}
    for function, code, guarded in raises:
        by_code.setdefault(code, []).append(guarded or live_only.get(function, False))
    return by_code, live_only


def _only_evaluated_for_a_live_profile() -> set[str]:
    """Codes every raise site of which the retained replay never reaches."""
    by_code, _ = _release_refusal_reach()
    return {code for code, sites in by_code.items() if all(sites)}


def _release_table() -> dict[str, str]:
    """The release refusal table: each code against the carriage the runbook claims."""
    rows = {}
    for line in _RUNBOOK.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"\| `([a-z_]+)` \| ([a-z ]+) \| .+ \|", line)
        if match is not None:
            rows[match.group(1)] = match.group(2)
    return rows


def test_the_runbook_marks_exactly_the_codes_the_retained_replay_drops():
    # The runbook used to claim every tabled code was carried only by a live
    # profile. It is not true: only the codes raised through _live_code are
    # dropped by the retained replay, and the rest are carried whatever the
    # profile. This derives both sides and refuses either drifting from the other.
    table = _release_table()
    marks = set(table.values())
    assert marks <= {"live profile only", "always"}
    live_only = {code for code, carried in table.items() if carried == "live profile only"}
    assert live_only == _live_gated()
    assert set(table) - live_only == set(table) - _live_gated()
    # Driving the retained replay through the live path raises this one, and the
    # unknown profile refusal is raised before a profile exists, so neither can be
    # live gated however the table is worded.
    assert table["release_profile_invalid"] == "always"
    assert table["release_profile_unknown"] == "always"


def test_the_runbook_names_exactly_the_codes_whose_always_mark_is_vacuous():
    # A code the retained replay never evaluates is carried always only because
    # the replay never reaches the condition. The runbook names those codes and
    # this derives the same set out of the release script, so neither the claim
    # nor the code can move without the other.
    dropped = _live_gated()
    vacuous = _only_evaluated_for_a_live_profile() - dropped
    text = _RUNBOOK.read_text(encoding="utf-8")
    marker = "never evaluated under the retained replay"
    sentences = [part for part in text.split("\n\n") if marker in part]
    assert len(sentences) == 1
    claimed = set(re.findall(r"`([a-z_]+)`", sentences[0]))
    assert claimed & set(_release_table()) == vacuous
    # The mark itself stays always, because the code is carried when it is raised.
    table = _release_table()
    assert {table[code] for code in vacuous} == {"always"}
    # And the codes the runbook calls genuinely carried are the rest.
    assert (set(table) - dropped) - vacuous == {
        "release_profile_unknown",
        "release_profile_invalid",
        "release_profile_adapter_unavailable",
        "release_generation_pair_differs",
    }


_NUMERALS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}


def _prose_numeral(pattern: str) -> int:
    """One numeral written in the runbook's prose, read across its line wrapping."""
    prose = " ".join(_RUNBOOK.read_text(encoding="utf-8").split())
    matches = re.findall(pattern, prose)
    assert len(matches) == 1, (pattern, matches)
    return _NUMERALS[matches[0].lower()]


def test_the_runbooks_release_code_numerals_are_the_sets_they_count():
    # The sets are derived and the numerals that count them were not, so a numeral
    # could disagree with its own table and nothing would say so.
    table = _release_table()
    dropped = _live_gated()
    through_helper = {
        code
        for call in _refusal_calls(_RELEASE)
        for kind, code in [_refusal_code(call)]
        if kind == "live" and code is not None
    }
    vacuous = _only_evaluated_for_a_live_profile() - dropped
    assert _prose_numeral(r"(\w+) of the codes below are dropped by the retained replay") == len(
        dropped
    )
    assert _prose_numeral(r"(\w+) are raised through `_live_code`") == len(through_helper)
    assert _prose_numeral(r"(\w+) of them are never evaluated under the retained replay") == len(
        vacuous
    )
    assert _prose_numeral(r"The remaining (\w+) are genuinely carried") == len(
        set(table) - dropped
    ) - len(vacuous)
    assert _prose_numeral(r"Of the (\w+) release codes tabled above") == len(table)
    assert _prose_numeral(r"The remaining (\w+) can only be raised") == len(table) - 2


def test_every_release_code_the_runbook_tables_carries_a_derived_carriage_mark():
    # The carriage column is only evidence if every release code is in it. The
    # rollback table has two columns and is excluded by the row pattern, so the
    # release codes are the emitted set less the rollback ones.
    rollback = {code for code in _tabled() if code.startswith("rollback_")}
    assert set(_release_table()) == _tabled() - rollback
    assert rollback <= _emitted()


def _admission_result() -> dict[str, object]:
    """What the legacy R16 admission reports about itself, walked out of its source."""
    for node in ast.walk(ast.parse(_ADMISSION.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ReleasedRunAdmission"
        ):
            fields = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
            return {
                "evidence_authority": ast.literal_eval(fields["evidence_authority"]),
                "remaining_checks": set(ast.literal_eval(fields["remaining_checks"])),
            }
    raise AssertionError("the admission builds no result")


def test_the_runbook_names_only_admission_checks_the_code_leaves_open():
    # The runbook says release admission does not refuse a copy. That claim is
    # read off the admission itself: it issues no evidence authority and reports
    # copy validation as still to do. The runbook may name no check the code has
    # closed, so closing one here fails this test rather than quietly outdating
    # the document.
    admitted = _admission_result()
    assert admitted["evidence_authority"] is False
    bullets = [
        bullet
        for bullet in _RUNBOOK.read_text(encoding="utf-8").split("\n- ")
        if "validate_released_run_admission" in bullet
    ]
    assert len(bullets) == 1
    claimed = set(re.findall(r"`([a-z_]+)`", bullets[0]))
    checks = {name for name in claimed if name.endswith("_validation")}
    assert checks
    assert checks <= admitted["remaining_checks"]
    assert "current_source_copy_validation" in checks
