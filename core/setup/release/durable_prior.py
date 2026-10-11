"""The durable-effects manifest of a services-only release whose prior release is another release than a80, with the proof that it
changes no storage (W8-REL 4, DE-04; plan 3.4d).

    py -3.13 -m core.setup.release.durable_prior --repo <checkout> --base-commit <40 hex> --owner "<Full Name>" --out <file>
        [--old-reader-receipt <file>]

The checkout must be clean and its HEAD is the release commit. The manifest is the repository template read at that commit, filled
with the release id, commit and tree, the owner, and the prior release as the base of the diff, with the statements of the five Cloud
Run effects worded for a prior release that left a tag (the prior tag stays, so no effect touches it). It states no storage change
and carries the proof the claim is held to, recomputed here from git and never taken from a file under review:

  schema    every statement core/schema/*.sql would run at the release commit is a statement the prior release ran (none is added)
  files     no .sql file and nothing under core/schema, core/detect, core/collect, core/understand or core/brief changed in the range
  tables    every table the changed sources name is defined by the schema statements at the release commit

A tree that adds a writer of the evidence_links key to the stored Ask record (the base has none) also gets one stored_answer effect for
it, written from what the tree contains: the consumers are the product files and lines that name the key, found in git at the release
commit, and the key is declared in the statement. It changes no SQL, and its forward recovery is none_needed: the previous readers pass
the key through unread, a rollback leaves it in place, and a record a previous writer saves later has no key.

The compat verdicts of the effects stay as the template has them (pending): they are settled from the harness receipts, which is the
lead's step. With --old-reader-receipt the five verdicts of the stored effect alone are settled from that receipt, and only after it is
checked against this tree: it says pass with no problem, it was made against the base and at the release commit, and its harness hash
equals the one recomputed here from the two harness files at the release commit. The output file is created once and never replaced.
Exit 0 written, 1 refused.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from core.setup import durable_effects_check as dec  # noqa: E402
from core.setup.release import natives  # noqa: E402

TEMPLATE = "core/setup/release/durable-effects-manifest.template.json"
SCHEMA_DIR = "core/schema/"
JOB_OWNED = ("core/schema/", "core/detect/", "core/collect/", "core/understand/", "core/brief/")
TABLE = re.compile(r"intelligence_42_[a-z]+\.([a-z_0-9]+)")


class Refused(Exception):
    pass


def git(repo, *args):
    done = subprocess.run([natives.native("git"), *args], cwd=repo, capture_output=True, timeout=120)
    if done.returncode != 0:
        raise Refused("git could not read the checkout")
    return done.stdout.decode("utf-8")


def schema_texts(files):
    return [text for path, text in sorted(files) if path.startswith(SCHEMA_DIR) and path.endswith(".sql")]


def changed_paths(repo, base, head):
    return sorted(line for line in git(repo, "diff", "--name-only", base, head, "--", "core").splitlines() if line)


def referenced_tables(repo, head, paths):
    """The table names the changed non-test Python and SQL sources at head name through a dataset of 42, in order."""
    found = set()
    for path in paths:
        if dec.is_test_path(path) or not path.endswith((".py", ".sql")):
            continue
        try:
            found |= set(TABLE.findall(git(repo, "show", f"{head}:{path}")))
        except Refused:
            continue
    return sorted(found)


def no_storage_change(repo, base, head):
    """(the claim with its proof, the problems that make it false)."""
    base_files, head_files = dec.git_tree_files(base, "core", repo), dec.git_tree_files(head, "core", repo)
    base_texts, head_texts = schema_texts(base_files), schema_texts(head_files)
    paths = changed_paths(repo, base, head)
    owned = [p for p in paths if p.endswith(".sql") or p.startswith(JOB_OWNED)]
    statements = dec.schema_statements(head_texts)
    defined = [t for t in referenced_tables(repo, head, paths) if any(re.search(r"`[^`]*\b" + re.escape(t) + r"`", s) for s in statements)]
    claim = {"claimed": True, "references": [{"table": t, "column": ""} for t in defined],
             "proof": {"base": base, "head": head, "schema_statements_added": len(dec.new_statements(head_texts, base_texts)),
                       "schema_sha256": {"base": dec.sha_text("\n".join(dec.schema_statements(base_texts))),
                                         "head": dec.sha_text("\n".join(statements))},
                       "changed_files": len(paths), "changed_files_sha256": dec.sha_text("\n".join(paths)), "changed_sql_or_job_owned": owned}}
    problems = dec.check_no_storage_change(claim, head_texts, base_texts)
    problems += [f"a file the jobs or the schema own changed: {p}" for p in owned]
    return claim, problems


WORDING = {
    "E-TRAFFIC": "both services pinned by name to the prior release revisions and left pinned (W8-REL 2.8)",
    "E-ENV-AUD": "AGENT_AUDIENCE set on the API candidate to the canonical agent URL; the prior release API already carries it",
    "E-ENV-URL": "the promoted API AGENT_URL changes from the agent tag URL of the prior release to the agent tag URL of this release",
}


HARNESS_FILES = ("core/api/tests/old_reader_harness.py", "core/api/tests/compat_harness.py")
LINK_KEY = "evidence_links"
LINK_MODULE = "comment_links"
LINK_NAME = r"evidence_links|comment_links"
SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx")
STORED_ID = "E-STORED-EVIDENCE-LINKS"
STORED_TARGET = "intelligence_42_agent.runs#record#$.evidence_links"
STORED_STATEMENT = ("The Ask record stored in intelligence_42_agent.runs, column record, gains the key evidence_links at $.evidence_links, an "
                    "object with the version key v equal to 1 and the key parents, an object that maps the id of a comment shown in the answer "
                    "to the id of the post it sits under, which the answer shows too. f42-agent writes it when an Ask ends and at least one "
                    "shown comment has its post shown too; records written before this release, and records where no such pair is shown, "
                    "do not have it.")
STORED_RECOVERY = ("The previous readers pass the key through and do not act on it, a rollback leaves the stored key in place, and a record a "
                   "previous writer saves later has no key and the candidate judges its comments by their address alone")
CRLF, LF = b"\r\n", b"\n"


def harness_sha256(repo, head):
    """The hash the old-reader harness writes into its receipt (harness_sha256 in old_reader_harness.py), recomputed from the two harness
    files as they are in git at head. It is restated here because the harness imports the test stack; a test pins the two equal."""
    lines = []
    for path in HARNESS_FILES:
        blob = subprocess.run([natives.native("git"), "show", f"{head}:{path}"], cwd=repo, capture_output=True, timeout=120)
        if blob.returncode != 0:
            raise Refused("The old-reader receipt cannot be checked: a harness file is not in the release commit")
        lines.append(f"{path.rsplit('/', 1)[-1]}:{hashlib.sha256(blob.stdout.replace(CRLF, LF)).hexdigest()}\n")
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def read_receipt(repo, head, base, path):
    """The text, with LF line ends, of an old-reader receipt that passes and was made for this release, or Refused. The expected values
    are this checkout's: the release commit, the base commit and the harness hash recomputed from git, never the receipt's own."""
    try:
        raw = Path(path).read_bytes()
        receipt = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError):
        raise Refused("The old-reader receipt could not be read") from None
    if not isinstance(receipt, dict):
        raise Refused("The old-reader receipt is not an object")
    if receipt.get("schema_version") != 1 or receipt.get("verdict") != "pass" or receipt.get("problems") != []:
        raise Refused("The old-reader receipt does not say pass with no problem")
    receipt_base = receipt.get("base_commit")
    if not (isinstance(receipt_base, str) and re.fullmatch(r"[0-9a-f]{7,40}", receipt_base) and base.startswith(receipt_base)):
        raise Refused("The old-reader receipt was not made against the prior release commit")
    if receipt.get("candidate_commit") != head:
        raise Refused("The old-reader receipt was not made at the release commit")
    if receipt.get("harness_sha256") != harness_sha256(repo, head):
        raise Refused("The old-reader receipt was not made by the harness at the release commit")
    return raw.decode("utf-8-sig").replace("\r\n", "\n")


def is_key(node):
    """True for the evidence_links key written as a literal or as comment_links.KEY."""
    return ((isinstance(node, ast.Constant) and node.value == LINK_KEY)
            or (isinstance(node, ast.Attribute) and node.attr == "KEY" and isinstance(node.value, ast.Name) and node.value.id == LINK_MODULE))


def link_lines(path, text):
    """{line: access} for the lines of one source file that name the evidence_links key or the module that reads it. A Python file is
    read as syntax, so a docstring or a comment that names the key is not a consumer. Access is write where the line stores the key
    (a subscript assignment, a dict key being built) or defines the producer; every other use is a read."""
    found = {}

    def note(line, access):
        found[line] = "write" if "write" in (access, found.get(line)) else "read"

    if not path.endswith(".py"):
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(LINK_NAME, line) and not line.lstrip().startswith(("//", "*", "/*")):
                note(number, "read")
        return found
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Constant) and node.value == LINK_KEY:
            note(node.lineno, "read")
        elif isinstance(node, ast.Attribute) and (node.attr == LINK_MODULE or (isinstance(node.value, ast.Name) and node.value.id == LINK_MODULE)):
            note(node.lineno, "read")
        elif isinstance(node, ast.ImportFrom) and any(alias.name == LINK_MODULE for alias in node.names):
            note(node.lineno, "read")
        elif isinstance(node, ast.FunctionDef) and node.name == LINK_MODULE:
            note(node.lineno, "write")
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) and is_key(node.slice):
            note(node.lineno, "write")
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if key is not None and is_key(key):
                    note(key.lineno, "write")
    return found


def link_consumers(repo, rev):
    """The consumers of the evidence_links key at rev, from git: [{access, component, file, image, line}] for every non-test product file
    under core/ and app/ that names it, one entry per line. Release tooling under core/setup/ is not a consumer of a stored Ask."""
    done = subprocess.run([natives.native("git"), "grep", "-l", "-I", "-E", LINK_NAME, rev, "--", "core", "app"],
                          cwd=repo, capture_output=True, timeout=120)
    if done.returncode not in (0, 1):
        raise Refused("git could not read the checkout")
    prefix = f"{rev}:"
    consumers = []
    for line in done.stdout.decode("utf-8").splitlines():
        path = line[len(prefix):] if line.startswith(prefix) else line
        if dec.is_test_path(path) or path.startswith("core/setup/") or not path.endswith(SOURCE_SUFFIXES):
            continue
        for number, access in sorted(link_lines(path, git(repo, "show", f"{rev}:{path}")).items()):
            consumers.append({"access": access, "component": path.rsplit("/", 1)[-1], "file": path, "image": "f42-web", "line": number})
    return sorted(consumers, key=lambda c: (c["file"], c["line"]))


def stored_effect(repo, head, base, owner, number, receipt):
    """The stored_answer effect for the evidence_links key, or None when the tree adds no writer of it: a writer at head and none at base.
    `receipt` is the checked receipt text (the five verdicts become pass) or None (they stay pending, as in the template)."""
    consumers = link_consumers(repo, head)
    if not any(c["access"] == "write" for c in consumers) or any(c["access"] == "write" for c in link_consumers(repo, base)):
        return None
    cite = f"old-reader-receipt.json sha256 {dec.sha_text(receipt)[:12]}" if receipt is not None else ""
    tests = {
        "a80_readers": f"{cite}, layers 2 and 3: the previous API and agent, started from a git archive of the base, on records that carry the key, which they give back as stored",
        "a80_writers": f"{cite}, layer 4: records the previous writers leave, read by the candidate readers",
        "a80_ddl_writers": "no SQL change: the schema statements of the range are those of the base and no SQL or job owned file changed (no_storage_change proof, recomputed from git)",
        "candidate_readers_old_records": f"{cite}, layer 4",
        "candidate_writers_old_readers": f"{cite}, layers 1 and 2: the candidate corpus with link rows (a link, a comment with no stored post, a hidden post, a link to a post no longer shown, a dropped link) and without the key",
    }
    compat = {key: {"test": tests[key], "verdict": "pass"} if receipt is not None else {"test": "HR-45", "verdict": "pending"} for key in dec.COMPAT_KEYS}
    return {"additive": True, "apply_kind": "code", "apply_order": number, "apply_phase": "with_candidate", "authority_or_holds": False,
            "change": "write_shape", "compat": compat, "consumers": consumers, "destructive": False, "effect_id": STORED_ID, "evidence": {},
            "forward_recovery": {"kind": "none_needed", "note": STORED_RECOVERY, "statement_ref": "", "statement_sha256": ""},
            "kind": "stored_answer", "native_readback": {"expected_result_sha256": "", "kind": "helper_phase", "target": "AfterSmoke"},
            "owner": {"lane": 3, "person": owner}, "statement_ref": STORED_STATEMENT, "statement_sha256": dec.sha_text(STORED_STATEMENT),
            "target": STORED_TARGET}


def build_manifest(repo, head, base, owner, old_reader_receipt=None):
    receipt = read_receipt(repo, head, base, old_reader_receipt) if old_reader_receipt is not None else None
    claim, problems = no_storage_change(repo, base, head)
    if problems:
        raise Refused("The claim of no storage change is false: " + problems[0])
    manifest = json.loads(git(repo, "show", f"{head}:{TEMPLATE}"))
    tree = git(repo, "rev-parse", f"{head}^{{tree}}").strip()
    release_id = f"rel-{head[:7]}-01"
    manifest.update(release_id=release_id, source={"commit": head, "tree": tree}, generated_from={"base": base, "head": head},
                    no_storage_change=claim)
    for effect in manifest["effects"]:
        effect["owner"]["person"] = owner
        effect["target"] = effect["target"].replace("<release id>", release_id)
        if effect["effect_id"] in WORDING:
            effect["statement_ref"] = WORDING[effect["effect_id"]]
            effect["forward_recovery"] = {**effect["forward_recovery"], "note": WORDING[effect["effect_id"]]}
    stored = stored_effect(repo, head, base, owner, 1 + max(e["apply_order"] for e in manifest["effects"]), receipt)
    if stored is not None:
        manifest["effects"].append(stored)
    manifest["manifest_sha256"] = dec.manifest_hash(manifest)
    invalid = dec.validate_manifest(manifest)
    if invalid:
        raise Refused("The manifest is not valid: " + invalid[0])
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Write the durable-effects manifest of a release on top of a prior release.")
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--old-reader-receipt", type=Path, help="the old-reader receipt made at the release commit against the base")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.base_commit):
            raise Refused("--base-commit must be the full 40 character commit")
        if args.out.exists():
            raise Refused(f"{args.out.name} already exists; it is never replaced")
        if git(args.repo, "status", "--porcelain=v1").strip():
            raise Refused("The checkout is not clean")
        head = git(args.repo, "rev-parse", "HEAD").strip()
        manifest = build_manifest(args.repo, head, args.base_commit, args.owner, args.old_reader_receipt)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    except (Refused, dec.Refusal) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 1
    print(f"wrote {args.out}\nrelease_id {manifest['release_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
