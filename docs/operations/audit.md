# Candidate audit

E03 requires every installed secrets, dependency and security scanner to run over the complete shipped engine, app and ops scope of a frozen candidate, with the exact version, configuration, exit code and findings kept, and with a missing scanner recorded as an unfinished check rather than a silent pass. `ops/certification/audit_candidate.py` is the local, reproducible part of that requirement. It also carries the repository payload hygiene scan that R02 Step 8 needs before any push: assistance attribution tokens, personal directory paths and prohibited prose punctuation, each reported with path and line.

## Running it

```
python -m ops.certification.audit_candidate --root <candidate root> --output <receipt directory>
```

`--scanners` takes `all` (default), `none`, or a comma separated subset of `gitleaks,pip-audit,bandit,semgrep,ruff,bun,trivy`. A scanner that is not selected is still listed in the receipt as unfinished with reason `not_selected`. `--timeout` bounds each scanner run in seconds (default 900). The command exits 0 once the receipt is written; findings never change the exit code, because pass or fail is an adjudication, not a scanner output. It exits 1 when the root is not a directory and 2 on a bad selection or argument error.

The command reads the candidate tree and writes only under the output directory. Scanners run with the candidate scope as their working directory and with cache writes disabled where the scanner offers that switch, so the run leaves the candidate clean. Run it from the repository root of the checkout that holds this module; the candidate root can be another worktree.

## Scanner table

Discovery probes each scanner first by absolute path (the interpreter's own `Scripts` directory, the per-user Python `Scripts` directories, the per-user bun directory and the per-user package manager directory that holds gitleaks) and then on `PATH`. Each scanner is then asked for its version. The result is `installed` with the version line, `not_installed`, or `probe_failed` with the last error line when the executable exists but cannot start.

| Scanner | Scope targets | Configuration recorded | Completion exit codes |
|---|---|---|---|
| gitleaks | `dir .` in each scope | `.gitleaks.toml` in the scope when present, otherwise the default rules | 0, 1 |
| pip-audit | every `requirements*.lock` under each scope, one run per lock, `--no-deps --disable-pip` | none | 0, 1 |
| bandit | `-r .` in each scope | `.bandit` in the scope, else the engine `.bandit` marked as inherited, else none | 0, 1 |
| semgrep | `scan .` in each scope | rule pack `p/default` | 0, 1 |
| ruff | `check .` in each scope, `--no-cache` | the settings path ruff itself resolves, or its built-in defaults | 0, 1 |
| bun | `audit --json` in every directory holding a `bun.lock` | none | 0, 1 |
| trivy | `fs .` in each scope, vuln, secret and misconfig scanners | none | 0 |

Every run records the exact argument vector, the working directory relative to the root, the configuration file path and digest, the timeout, the exit code, the duration, a digest and byte count of stdout, a digest and last line of stderr, the network note, the parsed findings and their count. Any exit code outside the completion set, a timeout or unparseable output marks the run `failed` and the scanner `unfinished`, with the exit code and a `reason` kept. A completion exit code with empty stdout is also `unfinished`, reason `empty_output`: every scanner in the table writes a report document on completion (gitleaks and ruff write an empty list), so silence is the shape of a scanner that died before reporting, and pip-audit's fatal path exits 1 exactly like its findings path.

Four of the scanners need the network for their databases, and each run records that in its `network` field: pip-audit queries the PyPI vulnerability service per pinned package, bun posts the lockfile package list to the npm registry advisory endpoint, semgrep downloads the `p/default` rule pack from its registry, and trivy downloads its vulnerability database on first run. Only the first two ran in the recorded candidate run; semgrep and trivy were unfinished on this machine. The hashed lock files are compiled for Linux, so on this machine pip-audit runs with `--disable-pip` and audits the pinned set directly; unpinned `requirements*.txt` files are not audited because the tool cannot resolve them without installing.

## Redaction

Findings are reduced to a fixed field set per scanner, never the raw output. A gitleaks finding keeps the rule, path, line, entropy and fingerprint, and replaces the matched secret with `secret_digest`, a truncated domain separated SHA-256 of the value, plus its length. The same secret produces the same digest across runs, so a reviewer can adjudicate a finding without ever reading the value, and the receipt bytes never contain it. Bandit findings keep the test id, name, severity, confidence, path and line and drop the source snippet; ruff findings keep the code, path, row and column and drop the message, which can quote a source literal. The last stderr line and any launch error pass through the same redaction: every quoted literal becomes its digest and every personal directory path becomes a tilde.

The receipt itself carries no absolute path. The root is recorded as a name plus a path digest, run working directories and configuration paths are repository relative, scanner locations are written relative to the home directory, and every remaining string in the receipt is swept for personal directory paths before writing, with one recorded exception: a string that parses as a URL with a scheme and a host is left intact, because a route segment named home or Users is not a person. The rule is written into the receipt under the personal path rules as `sweep`. The receipt would pass the module's own personal path rule if it were ever tracked.

## Payload scan

`scan_repository_payload(root)` lists tracked files through `git ls-files` when the root is a git checkout, otherwise it walks the tree, and reads each file as bytes, decoding UTF-8 explicitly. A file the listing names that is absent from the working tree, a file that opens with a UTF-16 byte order mark, and a file holding a NUL byte in its first 8 KiB are not scanned; each is named in the `skipped` list with its reason (`missing_from_working_tree`, `utf16_not_scanned`, `binary`). A UTF-16 file is named, not decoded: a credit inside it stays unread, but it is a listed gap rather than an invisible one. Three classes are reported.

Attribution. Bare tokens naming assistants, models and vendors are reported on any line of any text file. The product name used through Vertex is reported only after a credit phrase (`generated with`, `co-authored-by`), because the engine uses that product legitimately: model identifiers, prompts, the mandated asset tag and the market topic anchors all carry the name. The two runtime settings that hold it, `GEMINI_MODEL` and `GEMINI_LOCATION`, are the explicit allowlist: on a line assigning either key only the key identifier is blanked, never the value span, so the assigned value, a trailing comment and any second statement on that line are scanned like any other text and a credit phrase cannot hide behind the key. The vocabulary is assembled at runtime from fragments so the scanner does not report its own source.

Personal paths. Four forms: a drive letter, colon and the Users folder followed by a user name in either slash style (`windows_drive`), the shell form with a single letter drive segment (`shell_drive`), the Users folder at the filesystem root (`posix_users`) and the home folder at the filesystem root (`posix_home`). A hit records the path, line, form, a digest of the user name and its length, never the matched text, so the receipt does not itself carry the path it reports.

Punctuation. Em dash, en dash and double hyphen runs in prose: the whole text of `.md` and `.txt` files, docstrings and comments in `.py`, comments in `.js`, `.jsx` and `.mjs`, comments in `.sql`, comments in `.yaml` and `.yml`, and string literals in `.json`. Excluded: fenced code blocks and inline code spans in markdown, markdown table separators, lines made only of dashes or pipes, HTML comment delimiters, the SQL comment delimiter itself, divider runs of three or more dashes at the start or end of a segment, and any path under `tests/fixtures/`. A markdown code span may cross lines and is blanked as a whole.

## Receipt

`audit_candidate_v2.json` holds the schema name `audit_candidate_v2` (the v1 shape had a string root, no state fields, no skipped list and raw personal path text in its hits; a v1 receipt is superseded and cannot be read as v2), the generation time, the root name and path digest, the candidate identity (commit, branch, dirty flag, file source), one entry per scope with the tracked file count, the digested file count, any missing files and the tree digest, a `state`, the `unfinished_scanners` and `missing_scanners` lists, the scanner table with every run, the payload scan with rules, counts, skipped files and hits, an `adjudication` list that starts empty, and `receipt_digest`, a SHA-256 over the canonical receipt without its timestamp, durations and the digest field, so two runs over the same tree with the same scanner results agree and a changed tree changes the digest. `scanner_runs.json` beside it is the scanner table alone.

The state is `unfinished` while any of the seven scanners is absent from the table, not installed, not selected, failed, timed out or silent, and `complete_pending_adjudication` otherwise. There is no clean state: findings are adjudicated by a person, and the receipt only records what ran.

The adjudication list is where a reviewer records each finding's disposition, by fingerprint or path and line, with a decision and a reason. The command never writes to it.
