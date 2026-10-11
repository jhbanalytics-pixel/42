"""The durable-effects manifest of a services-only release whose prior release is another release than a80, with the proof that it
changes no storage (W8-REL 4, DE-04; plan 3.4d).

    py -3.13 -m core.setup.release.durable_prior --repo <checkout> --base-commit <40 hex> --owner "<Full Name>" --out <file>

The checkout must be clean and its HEAD is the release commit. The manifest is the repository template read at that commit, filled
with the release id, commit and tree, the owner, and the prior release as the base of the diff, with the statements of the five Cloud
Run effects worded for a prior release that left a tag (the prior tag stays, so no effect touches it). It states no storage change
and carries the proof the claim is held to, recomputed here from git and never taken from a file under review:

  schema    every statement core/schema/*.sql would run at the release commit is a statement the prior release ran (none is added)
  files     no .sql file and nothing under core/schema, core/detect, core/collect, core/understand or core/brief changed in the range
  tables    every table the changed sources name is defined by the schema statements at the release commit

The compat verdicts of the effects stay as the template has them (pending): they are settled from the harness receipts, which is the
lead's step, and this tool settles none of them. The output file is created once and never replaced. Exit 0 written, 1 refused.
"""
from __future__ import annotations

import argparse
import copy
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


def build_manifest(repo, head, base, owner):
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
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[0-9a-f]{40}", args.base_commit):
            raise Refused("--base-commit must be the full 40 character commit")
        if args.out.exists():
            raise Refused(f"{args.out.name} already exists; it is never replaced")
        if git(args.repo, "status", "--porcelain=v1").strip():
            raise Refused("The checkout is not clean")
        head = git(args.repo, "rev-parse", "HEAD").strip()
        manifest = build_manifest(args.repo, head, args.base_commit, args.owner)
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
