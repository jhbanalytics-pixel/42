"""The run stamp: which code produced a runs row or a brief payload.

    stamp.build()                       the stamp of the running process
    stamp.stamp_row(row)                a runs row with the stamp as JSON text in its stamp column
    stamp.stamp_payload(payload)        a brief market payload with the stamp under its stamp key
    stamp.verify(stamp)                 recompute what can be recomputed and say what cannot

The stamp is {"v": 1, "git_sha", "image_digest", "prompts", "declared", "computed"}.

declared  git_sha and image_digest. A container cannot read its own digest or commit, so the deploy hands them in
          as F42_GIT_SHA and F42_IMAGE_DIGEST (git_sha falls back to F42_VERSION, which the services already
          carry). They are checked for shape and nothing more, so verify() lists them as unverifiable and never
          as confirmed. A value that does not have the shape is not recorded: null, not a guess.
computed  prompts. The sha256 of each file that holds model prompt text (PROMPT_FILES and every
          core/skills/*/SKILL.md, which Ask loads into its prompts), read from the files in this process's own tree.
          stamp_row and stamp_payload take no stamp: they build it themselves from the env and tree they are given,
          and refuse a row or payload that already carries one, so a row or payload cannot carry a hash it was not
          given by the code that ran. verify() recomputes them from the tree it is
          given, which proves the stamp matches those files. A file missing on both sides proves nothing, so
          verify() lists it as unverifiable.

The runs column and the payload key are additive and nullable; a reader that does not know them ignores them. The
column needs core/schema/apply.py first (ALTER TABLE runs ADD COLUMN IF NOT EXISTS stamp JSON): a row carrying a
stamp is refused by BigQuery's streaming insert until the column exists. Bump STAMP_VERSION when a field's meaning
changes.
"""
import hashlib
import json
import os
import re
from pathlib import Path

STAMP_VERSION = 1
ROOT = Path(__file__).resolve().parents[2]
# Every Python module that holds model prompt text; a test fails when a module defines a *_SYSTEM prompt, passes
# system_instruction or system= to the model, or declares tools whose descriptions it did not define, and the module
# that holds the text is not here. toolset.py holds the tool descriptions and schemas Ask's research loop sends to the
# model; sql_query.py holds the warehouse map that the sql_query description ends with. The Ask skills are SKILL.md
# files, found by SKILLS_GLOB.
PROMPT_FILES = (
    "core/agent/ask.py",
    "core/agent/critic.py",
    "core/agent/investigate.py",
    "core/agent/tools/sql_query.py",
    "core/agent/toolset.py",
    "core/agent/writer.py",
    "core/brief/explain.py",
    "core/llm/gemini.py",
    "core/understand/cluster.py",
    "core/understand/enrich.py",
    "core/understand/video.py",
)
SKILLS_GLOB = "core/skills/*/SKILL.md"


def prompt_files(root=None):
    """PROMPT_FILES, then the SKILL.md files under root in path order."""
    root = ROOT if root is None else Path(root)
    return PROMPT_FILES + tuple(sorted(p.relative_to(root).as_posix() for p in root.glob(SKILLS_GLOB)))


DECLARED = ("git_sha", "image_digest")
SHA = re.compile(r"[0-9a-f]{7,40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _declared(value, shape):
    value = (value or "").strip()
    return value if shape.fullmatch(value) else None


def _hash(root, rel):
    try:
        return "sha256:" + hashlib.sha256((Path(root) / rel).read_bytes()).hexdigest()
    except OSError:
        return None


def build(env=None, root=None):
    """The stamp of this process. Never raises: a field it cannot establish is None."""
    env = os.environ if env is None else env
    root = ROOT if root is None else root
    return {
        "v": STAMP_VERSION,
        "git_sha": _declared(env.get("F42_GIT_SHA") or env.get("F42_VERSION"), SHA),
        "image_digest": _declared(env.get("F42_IMAGE_DIGEST"), DIGEST),
        "prompts": {rel: _hash(root, rel) for rel in prompt_files(root)},
        "declared": list(DECLARED),
        "computed": ["prompts"],
    }


def verify(stamp, root=None):
    """Recompute the prompt hashes under root. {"prompts": the files whose hash differs from the stamp's, or that the
    stamp lacks, "unverifiable": the declared fields the stamp carries, which no recomputation can confirm, and
    every prompt file that is missing both from the stamp's hashes and from root, which proves nothing}."""
    root = ROOT if root is None else Path(root)
    recorded = stamp.get("prompts") or {}
    differ, unverifiable = [], [name for name in DECLARED if stamp.get(name)]
    for rel in sorted(set(prompt_files(root)) | set(recorded), key=lambda r: (r not in PROMPT_FILES, r)):
        now = _hash(root, rel)
        if now is None and recorded.get(rel) is None:
            unverifiable.append(rel)
        elif recorded.get(rel) != now:
            differ.append(rel)
    return {"prompts": differ, "unverifiable": unverifiable}


def stamp_row(row, env=None, root=None):
    """row with its stamp column set to the stamp of this process as JSON text, as counts is written. The stamp is
    built here from env and root (build's arguments), never passed in. Raises ValueError for a row that already
    carries one: a stamp that was not written by the code that ran is worth nothing."""
    if row.get("stamp") is not None:
        raise ValueError("the row already carries a stamp")
    return {**row, "stamp": json.dumps(build(env, root), sort_keys=True)}


def stamp_payload(payload, env=None, root=None):
    """payload with a stamp key; the original is not changed. The stamp is built here, as for stamp_row. Raises
    ValueError for a payload that already carries one."""
    if payload.get("stamp") is not None:
        raise ValueError("the payload already carries a stamp")
    return {**payload, "stamp": build(env, root)}
