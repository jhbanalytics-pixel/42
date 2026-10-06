# Closure crosswalk

`crosswalk.json` carries the three sets that decide what the programme closure
rule protects, each entry tagged with the document and the line it was derived
from, plus the sha256 of every source document that was read.

**It is a tripwire, not evidence.** The thirteen plan documents live outside this
repository and nothing in the repository can read them, so nothing here can
check this file against them. What the tests over it do is compare three copies
that have to agree, the module, this artefact and the test, and report when one
of them moves without the others. That catches drift, which is what has twice
gone wrong here, and it cannot detect a wrong derivation: a set derived
incorrectly from the documents on the day this file was written, or a line
quoted here that the document does not contain, passes every test in the tree.
Read the entries as a claim to be checked against the documents by hand, never
as a check that has already been made.

## How it was derived

All three sets come from the plan documents of record, never from the module.

**`requirement_ids` (84).** The 84 rows of the acceptance crosswalk table in the
master staging completion plan, one row per line over lines 191 to 274, each
entry recording that line and the owning or proving tasks named on the row.
Corroborated twice over: the union of the `requirements` arrays across the 35
tasks in the plan validation record is the same 84 identifiers, and the
acceptance ledger's register carries exactly 84 requirement rows.

**`human_signoff_requirements` (19).** From the acceptance ledger's register,
the six column tables whose fourth column states responsibility per row. A row
is included when that column names a person, as "human reviewer", "human
reviewers", "reviewers" or the programme owner by name. Each entry records the
line and the verbatim responsibility text, so the criterion is checkable.

**`programme_exit_gates` (5).** Four come from the single line in the decisions
record of 13 September that enumerates task exits as human gates, with their
reviewer roles. The fifth, E04, comes from its own exit step in the evaluation
and certification package, which requires the owner's explicit candidate
acceptance.

## Where this still disagrees with the module

The requirement identifiers and the exit gates agree with the module exactly,
and the tests assert that equality. Each section records that agreement in
`agrees_with_module` and in its own `in_module_not_sourced` and
`sourced_not_in_module` lists, and the tests read those lists rather than
trusting them.

The human sign off roster does not agree. The documents source 19 rows; the
module protects 21. F11 and F12 are protected there but their responsibility
column reads "Backend" and names no person. The looser criterion that would
source them, the requirement text naming the owner, also picks up P302 and P502,
which the module does not protect, so no single documented criterion yields 21.
Dropping F11 and F12 would take protection away from two rows that have it, so
they stay and the excess is pinned by the test: the roster must be a superset of
the documented 19, and the difference must be exactly F11 and F12.

## What the recorded comparison refers to

`module_under_comparison` records the module's path and the line each of the
three literals is defined on. It records no commit. A recorded commit can only
ever go stale, and this one did: it named a commit two behind the branch, from
before the exit gates grew from two to five, while the gate section went on
recording B04, E01 and L01 as gates the module did not protect after it had
started protecting them. Both rots sat in the tree uncaught. The test now reads
those line numbers against the module in the working tree, so a definition that
moves fails the test instead of rotting the file, and it reads each section's
disagreement lists against the module live.

Regenerate this file whenever those literals move or the documents change.

## What the tripwire cannot see

The tests over this file are not evidence from the plan documents. They are
consistency checks over a vendored copy, and the difference matters. A hostile
review mutated this artefact eight ways and six of those mutations survived,
including rewriting a sourced line to say the opposite of what it says while
keeping the substrings a test was grepping for. Those particular checks are
tighter now, and the shape of the gap is unchanged:

- **The digests are carried, not checked.** The thirteen source documents live
  outside this repository. Nothing here can hash them. The test pins all
  thirteen digest strings as literals of its own, so the artefact's copy cannot
  be rewritten on its own, and that is the whole of what the pin does. It cannot
  detect a plan document changing under the crosswalk. Closing that gap needs
  the documents vendored alongside this file.
- **Artefact and module sit in one commit.** A coordinated edit that changes the
  module and this file together passes every test here. The tests catch one side
  drifting from the other, which is what actually happened twice; they do not
  catch a change made deliberately on both sides at once.
- **The quoted text is checked against itself.** The test checks that each
  requirement row's quoted text is the row its own parsed task list describes,
  that each roster entry's responsibility column parses into parties and that
  one of them, read whole, is the person the stated criterion requires, and that
  each gate entry's quoted line is the line the test pins in full and names, in
  the part of itself that makes the claim, exactly the gates cited to it. Those
  are equality and full-match checks over parsed context rather than substring
  searches, because a substring search accepted a line rewritten to say the
  opposite. They remain a check that the artefact is internally honest, not a
  check that the document says it.

Regenerating this file must never write to the plan documents. The plan
verifier's index refresh rewrites the master file and would break the digest pin
recorded here.
