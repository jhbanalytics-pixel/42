"""Reproducible local audit of a frozen candidate tree (E03).

Three checks run over the engine, app and ops scopes of a candidate root and
land in one machine readable receipt, schema audit_candidate_v2.

First, every installed secrets, dependency and security scanner runs with an
exact argument vector, a fixed working directory and a timeout. The receipt
keeps the scanner version, the configuration file it used, the exit code, a
digest of the raw output and a parsed, redacted findings list. Matched secret
text never reaches the receipt: it is replaced by a stable private digest so
a reviewer can tell two findings apart without reading the value.

Second, a scanner that is not installed, fails its version probe, times out,
or exits with an unexpected code is recorded as an unfinished check. Nothing
in this module can mark a check as passed; that word belongs to the
adjudication slot, which the receipt creates empty.

Third, the repository payload scan reads every tracked file as bytes and
reports assistance attribution tokens, personal directory paths and
prohibited prose punctuation with path and line. The attribution tokens are
assembled at runtime from fragments so that this file does not flag itself.
"""

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tokenize
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = "audit_candidate_v2"
RECEIPT_NAME = "audit_candidate_v2.json"
SCANNER_NAMES = ("gitleaks", "pip-audit", "bandit", "semgrep", "ruff", "bun", "trivy")
SCOPES = ("engine", "app", "ops")
PROBE_TIMEOUT_S = 60
RUN_TIMEOUT_S = 900
VERSION_ARGS = {
    "gitleaks": ("version",),
    "pip-audit": ("--version",),
    "bandit": ("--version",),
    "semgrep": ("--version",),
    "ruff": ("--version",),
    "bun": ("--version",),
    "trivy": ("--version",),
}
# Exit codes each scanner uses to say "ran to completion, with or without
# findings". Anything else is a failed run and stays unfinished.
COMPLETION_EXIT_CODES = {
    "gitleaks": {0, 1},
    "pip-audit": {0, 1},
    "bandit": {0, 1},
    "semgrep": {0, 1},
    "ruff": {0, 1},
    "bun": {0, 1},
    "trivy": {0},
}
NETWORK_NOTES = {
    "pip-audit": "queries the PyPI vulnerability database per pinned package",
    "semgrep": "downloads the named rule pack from the semgrep registry",
    "bun": "posts the lockfile package list to the npm registry advisory endpoint",
    "trivy": "downloads the vulnerability database on first run",
}
WALK_EXCLUDES = {
    ".git",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".qlty",
}
REDACTION_DOMAIN = b"42-audit-candidate-redaction\x00"
FIXTURE_MARKER = "tests/fixtures/"

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
DASH_RUN = re.compile(r"-{2,}")
DIVIDER_RUN = re.compile(r"^\s*-{3,}\s*|\s*-{3,}\s*$")
DASH_ONLY_LINE = re.compile(r"^[\s\-|:=~*]*$")
TABLE_SEPARATOR = re.compile(r"^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-*:?\s*\|?\s*$")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(`+)[^`]*?\1")
HTML_COMMENT_DELIMITERS = re.compile(r"<!--|-->")
JSON_STRING = re.compile(r'"(?:[^"\\\n]|\\.)*"')
PERSONAL_PATH = re.compile(
    r"(?P<prefix>[A-Za-z]:[\\/]|/[a-z]/|/)(?P<folder>Users|home)[\\/]"
    r"(?P<name>[^\\/\s\"'<>|`)\]]+)"
)
QUOTED_LITERAL = re.compile(r"'([^']+)'|\"([^\"]+)\"")
YAML_COMMENT = re.compile(r"(?:^|\s)#(.*)$")
SQL_LINE_COMMENT = re.compile(r"--(.*)$")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RUFF_SETTINGS_PATH = re.compile(r'^Settings path: "(.*)"$', re.MULTILINE)

# Attribution vocabulary, assembled at runtime from fragments so the shared
# payload scan never flags the detector's own source. The names are those of
# assistants, models and vendors that appear in generated code and trailers.
_BARE_FRAGMENTS = (
    ("cla", "ude"),
    ("anthro", "pic"),
    ("chat", "g", "pt"),
    ("open", "ai"),
    ("copi", "lot"),
    ("cod", "ex"),
)
_MODEL_PREFIX_FRAGMENTS = ("g", "p", "t")
_CREDIT_ONLY_FRAGMENTS = (("gem", "ini"),)
_CREDIT_PHRASE_FRAGMENTS = (
    ("generated ", "with"),
    ("co-authored", "-by"),
    ("co-authored", " by"),
)
# The runtime settings that legitimately carry the product name through the
# Vertex integration. A line assigning one of these keys is not a credit.
ALLOWLISTED_SETTING_KEYS = ("GEMINI_MODEL", "GEMINI_LOCATION")
SWEEP_RULE = (
    "before writing, every string in the receipt has personal path prefixes "
    "replaced by a tilde, except a string that parses as a URL with a scheme "
    "and a host, which is left intact"
)


def _join(fragments: tuple[str, ...]) -> str:
    return "".join(fragments)


def attribution_vocabulary() -> dict:
    bare = [_join(parts) for parts in _BARE_FRAGMENTS]
    credit_only = [_join(parts) for parts in _CREDIT_ONLY_FRAGMENTS]
    phrases = [_join(parts) for parts in _CREDIT_PHRASE_FRAGMENTS]
    model_prefix = _join(_MODEL_PREFIX_FRAGMENTS)
    bare_pattern = re.compile(
        r"\b(?:"
        + "|".join(re.escape(token) for token in bare)
        + r")\b|\b"
        + model_prefix
        + r"(?:-?\d|\b)",
        re.IGNORECASE,
    )
    names = bare + credit_only + [model_prefix]
    credit_pattern = re.compile(
        r"(?:"
        + "|".join(re.escape(phrase) for phrase in phrases)
        + r")[:\s]+[^\n]{0,60}?\b(?:"
        + "|".join(re.escape(name) for name in names)
        + r")\b",
        re.IGNORECASE,
    )
    setting_pattern = re.compile(
        r"^\s*(?:" + "|".join(ALLOWLISTED_SETTING_KEYS) + r")(?=\s*[=:])"
    )
    return {
        "bare": bare + [model_prefix],
        "credit_only": credit_only,
        "credit_phrases": phrases,
        "bare_pattern": bare_pattern,
        "credit_pattern": credit_pattern,
        "setting_pattern": setting_pattern,
    }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def redaction_digest(value: str) -> str:
    """Stable private digest of a matched secret, never the value itself."""
    return hashlib.sha256(REDACTION_DOMAIN + value.encode("utf-8")).hexdigest()[:16]


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def _probe_dirs() -> list[Path]:
    home = Path.home()
    dirs = [Path(sys.executable).resolve().parent]
    python_scripts = home / "AppData" / "Local" / "Programs" / "Python"
    if python_scripts.is_dir():
        dirs.extend(sorted(p / "Scripts" for p in python_scripts.glob("Python3*")))
    dirs.append(home / ".bun" / "bin")
    winget = home / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    if winget.is_dir():
        dirs.extend(sorted(winget.glob("Gitleaks.Gitleaks_*")))
    return dirs


def _locate(name: str) -> Path | None:
    suffixes = (".exe", "") if os.name == "nt" else ("",)
    for directory in _probe_dirs():
        for suffix in suffixes:
            candidate = directory / f"{name}{suffix}"
            if candidate.is_file():
                return candidate
    found = shutil.which(name)
    return Path(found) if found else None


def _run(argv: list[str], *, cwd: Path, timeout: int) -> dict:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv, cwd=str(cwd), capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None,
            "stdout": exc.stdout or b"",
            "stderr": exc.stderr or b"",
            "timed_out": True,
            "duration_s": round(time.monotonic() - started, 3),
            "error": f"timeout after {timeout}s",
        }
    except OSError as exc:
        return {
            "exit_code": None,
            "stdout": b"",
            "stderr": b"",
            "timed_out": False,
            "duration_s": round(time.monotonic() - started, 3),
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "timed_out": False,
        "duration_s": round(time.monotonic() - started, 3),
        "error": None,
    }


def _last_line(data: bytes, limit: int = 200) -> str | None:
    text = ANSI_ESCAPE.sub("", decode(data))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return _redact_text(lines[-1])[:limit] if lines else None


def _redact_text(text: str) -> str:
    """Replace quoted literals with their digest and personal paths with a tilde."""
    quoted = QUOTED_LITERAL.sub(
        lambda m: f"<redacted:{redaction_digest(m.group(1) or m.group(2))}>", text
    )
    return PERSONAL_PATH.sub("~", quoted)


def _is_url(value: str) -> bool:
    parts = urlsplit(value)
    return bool(parts.scheme) and bool(parts.netloc)


def _sweep(value):
    """Return a copy of value with every personal path in any string redacted.

    A string that parses as a URL with a scheme and a host is left intact,
    because a URL path segment named home or Users is a route, not a person.
    """
    if isinstance(value, dict):
        return {key: _sweep(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sweep(item) for item in value]
    if isinstance(value, str) and not _is_url(value):
        return PERSONAL_PATH.sub("~", value)
    return value


def discover_scanners() -> dict:
    """Probe every scanner by absolute path, then PATH, and record its version."""
    table = {}
    for name in SCANNER_NAMES:
        path = _locate(name)
        entry = {
            "status": "not_installed",
            "command": None,
            "path": None,
            "version": None,
            "probe": {"argv": [], "exit_code": None, "error": None},
        }
        if path is not None:
            argv = [str(path), *VERSION_ARGS[name]]
            result = _run(argv, cwd=Path.cwd(), timeout=PROBE_TIMEOUT_S)
            entry["path"] = str(path)
            entry["probe"]["argv"] = argv
            entry["probe"]["exit_code"] = result["exit_code"]
            first_line = next(
                (
                    line.strip()
                    for line in decode(result["stdout"]).splitlines()
                    if line.strip()
                ),
                None,
            )
            if result["exit_code"] == 0 and first_line:
                entry["status"] = "installed"
                entry["command"] = [str(path)]
                entry["version"] = first_line
            else:
                entry["status"] = "probe_failed"
                entry["probe"]["error"] = (
                    result["error"]
                    or _last_line(result["stderr"])
                    or "no version output"
                )
        table[name] = entry
    return table


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _home_relative(location: str) -> str:
    """Return one scanner location written relative to the home directory.

    A location under a home directory becomes a tilde path through the same
    personal path rule the receipt sweep applies. A location outside every home
    directory is written relative to this account's home directory instead, so
    the receipt carries no absolute path either way. A value that is not an
    absolute path, such as a scanner flag or a target relative to the working
    directory, is returned unchanged.
    """
    swept = PERSONAL_PATH.sub("~", location)
    if not Path(swept).is_absolute():
        return swept
    try:
        home = Path.home()
    except RuntimeError:
        return swept
    try:
        return Path(os.path.relpath(swept, home)).as_posix()
    except ValueError:
        return swept


def _plan_runs(name: str, scope: str, scope_dir: Path, root: Path) -> list[dict]:
    """Return one planned invocation per target, with argv relative to cwd."""
    if name == "gitleaks":
        argv = [
            "dir",
            ".",
            "--no-banner",
            "--report-format",
            "json",
            "--report-path",
            "-",
            "--exit-code",
            "1",
        ]
        config = scope_dir / ".gitleaks.toml"
        if config.is_file():
            argv += ["--config", ".gitleaks.toml"]
        return [{"argv": argv, "cwd": scope_dir, "config": _config_note(config, root)}]
    if name == "bandit":
        argv = ["-r", ".", "-f", "json", "-q"]
        config = scope_dir / ".bandit"
        origin = "scope"
        if not config.is_file():
            config = root / "engine" / ".bandit"
            origin = "inherited from engine"
        if config.is_file():
            argv += ["-c", os.path.relpath(config, scope_dir)]
        note = _config_note(config, root)
        if note:
            note["origin"] = origin
        return [{"argv": argv, "cwd": scope_dir, "config": note}]
    if name == "pip-audit":
        runs = []
        for lock in sorted(scope_dir.rglob("requirements*.lock")):
            if any(part in WALK_EXCLUDES for part in lock.parts):
                continue
            argv = [
                "-r",
                lock.relative_to(scope_dir).as_posix(),
                "--no-deps",
                "--disable-pip",
                "-f",
                "json",
                "--progress-spinner",
                "off",
            ]
            runs.append({"argv": argv, "cwd": scope_dir, "config": None})
        return runs
    if name == "semgrep":
        argv = [
            "scan",
            "--config",
            "p/default",
            "--json",
            "--metrics",
            "off",
            "--quiet",
            ".",
        ]
        return [{"argv": argv, "cwd": scope_dir, "config": {"rules": "p/default"}}]
    if name == "ruff":
        argv = ["check", ".", "--output-format", "json", "--no-cache"]
        return [
            {"argv": argv, "cwd": scope_dir, "config": {"settings_path": "unresolved"}}
        ]
    if name == "bun":
        runs = []
        for lock in sorted(scope_dir.rglob("bun.lock")):
            if any(part in WALK_EXCLUDES for part in lock.parts):
                continue
            runs.append(
                {"argv": ["audit", "--json"], "cwd": lock.parent, "config": None}
            )
        return runs
    if name == "trivy":
        argv = [
            "fs",
            "--scanners",
            "vuln,secret,misconfig",
            "--format",
            "json",
            "--quiet",
            ".",
        ]
        return [{"argv": argv, "cwd": scope_dir, "config": None}]
    raise ValueError(f"unknown scanner {name}")


def _config_note(config: Path, root: Path) -> dict | None:
    if not config.is_file():
        return None
    data = config.read_bytes()
    return {"path": _relative(config, root), "sha256": sha256_bytes(data)}


def _parse_json(stdout: bytes):
    text = decode(stdout).strip()
    if not text:
        return None
    return json.loads(text)


def _parse_findings(name: str, stdout: bytes) -> tuple[list[dict], dict]:
    """Return redacted findings plus parser notes; raise on unparseable output."""
    data = _parse_json(stdout)
    notes: dict = {}
    findings: list[dict] = []
    if name == "gitleaks":
        for item in data or []:
            secret = str(item.get("Secret") or "")
            findings.append(
                {
                    "rule_id": item.get("RuleID"),
                    "path": item.get("File"),
                    "line": item.get("StartLine"),
                    "entropy": item.get("Entropy"),
                    "fingerprint": item.get("Fingerprint"),
                    "secret_digest": redaction_digest(secret),
                    "secret_length": len(secret),
                }
            )
    elif name == "bandit":
        data = data or {}
        for item in data.get("results", []):
            findings.append(
                {
                    "rule_id": item.get("test_id"),
                    "test_name": item.get("test_name"),
                    "severity": item.get("issue_severity"),
                    "confidence": item.get("issue_confidence"),
                    "path": item.get("filename"),
                    "line": item.get("line_number"),
                    "more_info": item.get("more_info"),
                }
            )
        notes["errors"] = len(data.get("errors", []))
    elif name == "pip-audit":
        data = data or {}
        skipped = 0
        for dependency in data.get("dependencies", []):
            if dependency.get("skip_reason"):
                skipped += 1
            for vuln in dependency.get("vulns", []):
                findings.append(
                    {
                        "rule_id": vuln.get("id"),
                        "package": dependency.get("name"),
                        "version": dependency.get("version"),
                        "fix_versions": vuln.get("fix_versions"),
                        "aliases": vuln.get("aliases"),
                    }
                )
        notes["dependencies"] = len(data.get("dependencies", []))
        notes["skipped"] = skipped
    elif name == "semgrep":
        data = data or {}
        for item in data.get("results", []):
            findings.append(
                {
                    "rule_id": item.get("check_id"),
                    "path": item.get("path"),
                    "line": (item.get("start") or {}).get("line"),
                    "severity": (item.get("extra") or {}).get("severity"),
                }
            )
        notes["errors"] = len(data.get("errors", []))
    elif name == "ruff":
        for item in data or []:
            findings.append(
                {
                    "rule_id": item.get("code"),
                    "path": item.get("filename"),
                    "line": (item.get("location") or {}).get("row"),
                    "column": (item.get("location") or {}).get("column"),
                }
            )
    elif name == "bun":
        for package, advisories in (data or {}).items():
            for advisory in advisories:
                findings.append(
                    {
                        "rule_id": advisory.get("id"),
                        "package": package,
                        "severity": advisory.get("severity"),
                        "title": advisory.get("title"),
                        "vulnerable_versions": advisory.get("vulnerable_versions"),
                        "url": advisory.get("url"),
                    }
                )
    elif name == "trivy":
        for result in (data or {}).get("Results", []):
            target = result.get("Target")
            for item in result.get("Vulnerabilities", []) or []:
                findings.append(
                    {
                        "rule_id": item.get("VulnerabilityID"),
                        "package": item.get("PkgName"),
                        "version": item.get("InstalledVersion"),
                        "severity": item.get("Severity"),
                        "target": target,
                    }
                )
            for item in result.get("Secrets", []) or []:
                findings.append(
                    {
                        "rule_id": item.get("RuleID"),
                        "path": target,
                        "line": item.get("StartLine"),
                        "severity": item.get("Severity"),
                    }
                )
            for item in result.get("Misconfigurations", []) or []:
                findings.append(
                    {
                        "rule_id": item.get("ID"),
                        "path": target,
                        "severity": item.get("Severity"),
                    }
                )
    return findings, notes


def _ruff_settings_path(command: list[str], cwd: Path, root: Path) -> dict:
    """Ask ruff which settings file it resolved, or record its built-in defaults."""
    shown = _run(
        [*command, "check", ".", "--show-settings", "--no-cache"], cwd=cwd, timeout=120
    )
    match = RUFF_SETTINGS_PATH.search(decode(shown["stdout"]))
    if match is None:
        return {"settings_path": None, "resolution": "ruff built-in defaults"}
    return {
        "settings_path": _relative(Path(match.group(1)), root),
        "resolution": "settings file",
    }


def _execute_run(
    name: str, command: list[str], plan: dict, root: Path, timeout: int
) -> dict:
    argv = [*command, *plan["argv"]]
    if name == "ruff":
        plan = {**plan, "config": _ruff_settings_path(command, plan["cwd"], root)}
    result = _run(argv, cwd=plan["cwd"], timeout=timeout)
    record = {
        "argv": [*(_home_relative(item) for item in command), *plan["argv"]],
        "cwd": _relative(plan["cwd"], root),
        "config": plan["config"],
        "timeout_s": timeout,
        "exit_code": result["exit_code"],
        "duration_s": result["duration_s"],
        "stdout_sha256": sha256_bytes(result["stdout"]),
        "stdout_bytes": len(result["stdout"]),
        "stderr_sha256": sha256_bytes(result["stderr"]),
        "stderr_tail": _last_line(result["stderr"]),
        "network": NETWORK_NOTES.get(name),
        "findings_count": None,
        "findings": [],
        "parser_notes": {},
        "reason": None,
        "error": _redact_text(result["error"]) if result["error"] else None,
    }
    if result["timed_out"]:
        record["status"] = "timeout"
        record["reason"] = "timeout"
        return record
    if result["error"]:
        record["status"] = "failed"
        record["reason"] = "launch_error"
        return record
    if result["exit_code"] not in COMPLETION_EXIT_CODES[name]:
        record["status"] = "failed"
        record["reason"] = "exit_code_outside_completion_set"
        record["error"] = f"exit code {result['exit_code']} outside the completion set"
        return record
    if not result["stdout"].strip():
        record["status"] = "unfinished"
        record["reason"] = "empty_output"
        record["error"] = "scanner wrote no report to stdout"
        return record
    try:
        findings, notes = _parse_findings(name, result["stdout"])
    except (ValueError, TypeError, AttributeError) as exc:
        record["status"] = "failed"
        record["reason"] = "output_not_parseable"
        record["error"] = f"output not parseable: {type(exc).__name__}"
        return record
    record["status"] = "completed"
    record["findings"] = findings
    record["findings_count"] = len(findings)
    record["parser_notes"] = notes
    return record


def run_scanners(
    root: Path, *, output_dir: Path, scanners: dict, timeout: int = RUN_TIMEOUT_S
) -> dict:
    """Run every installed scanner over each scope; record everything, pass nothing.

    A run counts as completed only when the exit code sits in the scanner's
    completion set and stdout holds a parseable report. Empty stdout with a
    completion exit code is the shape of a scanner that died before reporting,
    so it is recorded as unfinished with reason empty_output.
    """
    root = Path(root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for name, entry in scanners.items():
        record = {key: value for key, value in entry.items() if key != "command"}
        if record.get("path"):
            record["path"] = _home_relative(record["path"])
        probe = record.get("probe")
        if isinstance(probe, dict) and probe.get("argv"):
            record["probe"] = {
                **probe,
                "argv": [_home_relative(item) for item in probe["argv"]],
            }
        record["runs"] = []
        if entry.get("status") != "installed" or not entry.get("command"):
            record["check_state"] = "unfinished"
            record["unfinished_reason"] = entry.get("status", "not_installed")
            results[name] = record
            continue
        for scope in SCOPES:
            scope_dir = root / scope
            if not scope_dir.is_dir():
                record["runs"].append(
                    {
                        "scope": scope,
                        "status": "not_applicable",
                        "reason": "scope_missing",
                    }
                )
                continue
            plans = _plan_runs(name, scope, scope_dir, root)
            if not plans:
                record["runs"].append(
                    {"scope": scope, "status": "not_applicable", "reason": "no_target"}
                )
                continue
            for plan in plans:
                run = _execute_run(name, entry["command"], plan, root, timeout)
                run["scope"] = scope
                record["runs"].append(run)
        executed = [run for run in record["runs"] if run["status"] != "not_applicable"]
        incomplete = [run for run in executed if run["status"] != "completed"]
        if executed and not incomplete:
            record["check_state"] = "completed"
            record["unfinished_reason"] = None
        else:
            record["check_state"] = "unfinished"
            record["unfinished_reason"] = (
                incomplete[0]["reason"] if incomplete else "no_target"
            )
        results[name] = record
    results = _sweep(results)
    (output_dir / "scanner_runs.json").write_bytes(
        json.dumps(results, indent=2, sort_keys=True).encode("utf-8")
    )
    return results


def _git(root: Path, *args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    result = _run([git, "-C", str(root), *args], cwd=root, timeout=120)
    if result["exit_code"] != 0:
        return None
    return decode(result["stdout"])


def tracked_files(root: Path) -> tuple[list[str], str, list[str]]:
    """Tracked paths relative to root, the listing source, and listed paths absent on disk."""
    root = Path(root)
    if (root / ".git").exists():
        listing = _git(root, "ls-files", "-z", "--full-name")
        if listing is not None:
            listed = sorted(p for p in listing.split("\0") if p)
            missing = [p for p in listed if not (root / p).is_file()]
            return [p for p in listed if p not in set(missing)], "git_ls_files", missing
    paths = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in WALK_EXCLUDES)
        for filename in filenames:
            paths.append((Path(directory) / filename).relative_to(root).as_posix())
    return sorted(paths), "filesystem_walk", []


def tree_digest(root: Path, paths: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_bytes((root / relative).read_bytes()).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _skip_reason(data: bytes) -> str | None:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf16_not_scanned"
    if b"\0" in data[:8192]:
        return "binary"
    return None


def _blank_span(match: re.Match) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def _md_prose_lines(text: str):
    in_fence = False
    kept = []
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            kept.append("")
            continue
        kept.append("" if in_fence else line)
    stripped = INLINE_CODE.sub(_blank_span, "\n".join(kept))
    for number, line in enumerate(stripped.split("\n"), start=1):
        if TABLE_SEPARATOR.match(line) or DASH_ONLY_LINE.match(line):
            continue
        yield number, HTML_COMMENT_DELIMITERS.sub(" ", line)


def _txt_prose_lines(text: str):
    for number, line in enumerate(text.splitlines(), start=1):
        if not DASH_ONLY_LINE.match(line):
            yield number, line


def _py_prose_lines(text: str):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            body = getattr(node, "body", [])
            if not body or not isinstance(body[0], ast.Expr):
                continue
            value = body[0].value
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            for offset, line in enumerate(value.value.splitlines()):
                yield value.lineno + offset, line
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                yield token.start[0], token.string[1:]
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return


def _js_comment_lines(text: str):
    state = "code"
    buffer = []
    line = 1
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        pair = text[index : index + 2]
        if state == "code":
            if pair == "//":
                state = "line"
                index += 2
                continue
            if pair == "/*":
                state = "block"
                index += 2
                continue
            if char in "'\"`":
                state = char
        elif state in ("'", '"', "`"):
            if char == "\\":
                index += 1
            elif char == state:
                state = "code"
        elif state == "line":
            if char == "\n":
                yield line, "".join(buffer)
                buffer = []
                state = "code"
            else:
                buffer.append(char)
        elif state == "block":
            if pair == "*/":
                yield line, "".join(buffer)
                buffer = []
                state = "code"
                index += 2
                continue
            if char == "\n":
                yield line, "".join(buffer)
                buffer = []
            else:
                buffer.append(char)
        if char == "\n":
            line += 1
        index += 1
    if buffer:
        yield line, "".join(buffer)


def _sql_comment_lines(text: str):
    in_block = False
    for number, line in enumerate(text.splitlines(), start=1):
        if in_block:
            end = line.find("*/")
            if end == -1:
                yield number, line
                continue
            yield number, line[:end]
            line = line[end + 2 :]
            in_block = False
        start = line.find("/*")
        if start != -1:
            in_block = True
            yield number, line[start + 2 :]
            line = line[:start]
        match = SQL_LINE_COMMENT.search(line)
        if match:
            comment = match.group(1)
            if not DASH_ONLY_LINE.match(comment):
                yield number, comment


def _yaml_comment_lines(text: str):
    for number, line in enumerate(text.splitlines(), start=1):
        match = YAML_COMMENT.search(line)
        if not match:
            continue
        before = line[: match.start()]
        if before.count('"') % 2 or before.count("'") % 2:
            continue
        yield number, match.group(1)


def _json_string_lines(text: str):
    for match in JSON_STRING.finditer(text):
        yield text.count("\n", 0, match.start()) + 1, match.group(0)


PROSE_EXTRACTORS = {
    ".md": _md_prose_lines,
    ".txt": _txt_prose_lines,
    ".py": _py_prose_lines,
    ".js": _js_comment_lines,
    ".jsx": _js_comment_lines,
    ".mjs": _js_comment_lines,
    ".sql": _sql_comment_lines,
    ".yaml": _yaml_comment_lines,
    ".yml": _yaml_comment_lines,
    ".json": _json_string_lines,
}


def _punctuation_hits(relative: str, text: str) -> list[dict]:
    extractor = PROSE_EXTRACTORS.get(Path(relative).suffix.lower())
    if extractor is None or FIXTURE_MARKER in relative:
        return []
    hits = []
    for number, segment in extractor(text):
        segment = DIVIDER_RUN.sub("", segment)
        if DASH_ONLY_LINE.match(segment):
            continue
        for kind, marker in (("em_dash", EM_DASH), ("en_dash", EN_DASH)):
            column = segment.find(marker)
            if column != -1:
                hits.append(
                    {
                        "path": relative,
                        "line": number,
                        "kind": kind,
                        "column": column + 1,
                    }
                )
        match = DASH_RUN.search(segment)
        if match:
            hits.append(
                {
                    "path": relative,
                    "line": number,
                    "kind": "double_hyphen",
                    "column": match.start() + 1,
                }
            )
    return hits


def _attribution_hits(relative: str, text: str, vocabulary: dict) -> list[dict]:
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = vocabulary["setting_pattern"].sub(_blank_span, line)
        seen = set()
        for match in vocabulary["bare_pattern"].finditer(line):
            token = match.group(0).lower()
            if token not in seen:
                seen.add(token)
                hits.append(
                    {"path": relative, "line": number, "kind": "token", "token": token}
                )
        for match in vocabulary["credit_pattern"].finditer(line):
            hits.append(
                {
                    "path": relative,
                    "line": number,
                    "kind": "credit",
                    "token": match.group(0)[:80],
                }
            )
    return hits


def _personal_form(match: re.Match) -> str:
    prefix = match.group("prefix")
    if ":" in prefix:
        return "windows_drive"
    if len(prefix) == 3:
        return "shell_drive"
    return "posix_users" if match.group("folder") == "Users" else "posix_home"


def _personal_path_hits(relative: str, text: str) -> list[dict]:
    hits = []
    for number, line in enumerate(text.splitlines(), start=1):
        for match in PERSONAL_PATH.finditer(line):
            name = match.group("name")
            hits.append(
                {
                    "path": relative,
                    "line": number,
                    "form": _personal_form(match),
                    "name_digest": redaction_digest(name),
                    "name_length": len(name),
                }
            )
    return hits


def scan_repository_payload(root: Path) -> dict:
    """Scan every tracked file for attribution, personal paths and prose punctuation."""
    root = Path(root)
    paths, source, missing = tracked_files(root)
    vocabulary = attribution_vocabulary()
    hits = {"attribution": [], "personal_path": [], "punctuation": []}
    skipped = [{"path": p, "reason": "missing_from_working_tree"} for p in missing]
    decode_errors = []
    scanned = 0
    for relative in paths:
        data = (root / relative).read_bytes()
        reason = _skip_reason(data)
        if reason is not None:
            skipped.append({"path": relative, "reason": reason})
            continue
        scanned += 1
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            decode_errors.append(relative)
            text = decode(data)
        hits["attribution"].extend(_attribution_hits(relative, text, vocabulary))
        hits["personal_path"].extend(_personal_path_hits(relative, text))
        hits["punctuation"].extend(_punctuation_hits(relative, text))
    return {
        "file_source": source,
        "files_scanned": scanned,
        "files_skipped": len(skipped),
        "skipped": skipped,
        "decode_errors": decode_errors,
        "rules": {
            "attribution": {
                "bare_tokens": vocabulary["bare"],
                "credit_only_tokens": vocabulary["credit_only"],
                "credit_phrases": vocabulary["credit_phrases"],
                "allowlisted_setting_keys": list(ALLOWLISTED_SETTING_KEYS),
                "policy": (
                    "bare tokens are reported on any line; credit-only tokens are "
                    "reported only after a credit phrase; on a line assigning an "
                    "allowlisted setting key only the key identifier is blanked, "
                    "never the value span, and the rest of the line is scanned"
                ),
            },
            "personal_path": {
                "pattern": PERSONAL_PATH.pattern,
                "forms": ["windows_drive", "shell_drive", "posix_users", "posix_home"],
                "recorded": "form, name digest and name length, never the matched text",
                "sweep": SWEEP_RULE,
            },
            "punctuation": {
                "kinds": ["em_dash", "en_dash", "double_hyphen"],
                "extensions": sorted(PROSE_EXTRACTORS),
                "exclusions": [
                    "fenced code blocks and inline code spans in markdown",
                    "markdown table separator lines",
                    "lines made only of dashes, pipes, colons or whitespace",
                    "divider runs of three or more dashes at the start or end of a segment",
                    "html comment delimiters in markdown",
                    "sql line comment delimiters",
                    f"any path containing {FIXTURE_MARKER}",
                ],
            },
        },
        "counts": {name: len(items) for name, items in hits.items()},
        "hits": hits,
    }


def _candidate_identity(root: Path, source: str) -> dict:
    identity = {"commit": None, "branch": None, "dirty": None, "file_source": source}
    if source != "git_ls_files":
        return identity
    head = _git(root, "rev-parse", "HEAD")
    branch = _git(root, "branch", "--show-current")
    status = _git(root, "status", "--porcelain")
    identity["commit"] = head.strip() if head else None
    identity["branch"] = branch.strip() if branch else None
    identity["dirty"] = bool(status.strip()) if status is not None else None
    return identity


def _strip_volatile(value):
    if isinstance(value, dict):
        return {
            key: _strip_volatile(item)
            for key, item in value.items()
            if key not in {"generated_at", "duration_s", "receipt_digest"}
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def write_audit_receipt(
    root: Path, *, output_dir: Path, scanner_results: dict, payload: dict
) -> dict:
    """Assemble the audit_candidate_v1 receipt and write it under output_dir.

    The receipt never carries an absolute path: the root is a name plus a
    digest, run paths are repository relative, and every remaining string is
    swept for personal directory paths. Its state reads unfinished while any
    scanner is missing or unfinished; the other state is complete pending
    adjudication, never clean.
    """
    root = Path(root)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths, source, missing = tracked_files(root)
    scopes = {}
    for scope in SCOPES:
        scoped = [p for p in paths if p.startswith(f"{scope}/")]
        scoped_missing = [p for p in missing if p.startswith(f"{scope}/")]
        scopes[scope] = {
            "present": (root / scope).is_dir(),
            "tracked_files": len(scoped) + len(scoped_missing),
            "digested_files": len(scoped),
            "missing_files": scoped_missing,
            "tree_digest": tree_digest(root, scoped),
        }
    missing_scanners = [name for name in SCANNER_NAMES if name not in scanner_results]
    unfinished = [
        name
        for name in SCANNER_NAMES
        if scanner_results.get(name, {}).get("check_state") != "completed"
    ]
    receipt = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "root": {
            "name": root.resolve().name,
            "path_digest": redaction_digest(root.resolve().as_posix()),
        },
        "candidate": _candidate_identity(root, source),
        "scopes": scopes,
        "state": "unfinished" if unfinished else "complete_pending_adjudication",
        "unfinished_scanners": unfinished,
        "missing_scanners": missing_scanners,
        "scanners": scanner_results,
        "payload_scan": payload,
        "adjudication": [],
    }
    receipt = _sweep(receipt)
    canonical = json.dumps(
        _strip_volatile(receipt), sort_keys=True, separators=(",", ":")
    )
    receipt["receipt_digest"] = sha256_bytes(canonical.encode("utf-8"))
    (output_dir / RECEIPT_NAME).write_bytes(
        json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8")
    )
    return receipt


ADJUDICATION_SCHEMA = "audit_adjudication_v1"
ADJUDICATION_NAME = "audit_adjudication_v1.json"
ADJUDICATION_STATES = ("accepted", "deferred")


def adjudicate(receipt: dict, decisions: dict) -> list[dict]:
    """One adjudication row per scanner in the receipt; nothing here reads as a pass.

    A scanner whose check did not complete stays unfinished whatever the
    decision says, and its row must carry the exact install path that would
    close the check. A completed scanner needs a decision with a state from
    ADJUDICATION_STATES and a nonempty note. Every decision must name a
    scanner the receipt holds, and every completed scanner must be decided.
    """
    scanners = receipt.get("scanners") or {}
    unknown = sorted(set(decisions) - set(scanners))
    if unknown:
        raise ValueError(f"adjudication_scanner_unknown:{unknown}")
    rows = []
    for name in sorted(scanners):
        entry = scanners[name]
        decision = decisions.get(name)
        executed = [
            run
            for run in entry.get("runs", [])
            if run.get("status") != "not_applicable"
        ]
        row = {
            "scanner": name,
            "version": entry.get("version"),
            "check_state": entry.get("check_state"),
            "configuration": [run.get("config") for run in executed],
            "exit_codes": [run.get("exit_code") for run in executed],
            "findings": sum(run.get("findings_count") or 0 for run in executed),
        }
        if entry.get("check_state") != "completed":
            if decision is not None and decision.get("state") in ADJUDICATION_STATES:
                raise ValueError(f"adjudication_unfinished_cannot_accept:{name}")
            install_path = (decision or {}).get("install_path")
            if not isinstance(install_path, str) or not install_path.strip():
                raise ValueError(f"adjudication_install_path_required:{name}")
            row["state"] = "unfinished"
            row["reason"] = entry.get("unfinished_reason")
            row["install_path"] = install_path.strip()
            rows.append(row)
            continue
        if decision is None:
            raise ValueError(f"adjudication_missing:{name}")
        if decision.get("state") not in ADJUDICATION_STATES:
            raise ValueError(f"adjudication_state_invalid:{name}")
        note = decision.get("note")
        if not isinstance(note, str) or not note.strip():
            raise ValueError(f"adjudication_note_required:{name}")
        row["state"] = decision["state"]
        row["note"] = note.strip()
        rows.append(row)
    return rows


def write_adjudication(receipt: dict, decisions: dict, *, output_dir: Path) -> dict:
    rows = adjudicate(receipt, decisions)
    document = {
        "schema": ADJUDICATION_SCHEMA,
        "receipt_digest": receipt.get("receipt_digest"),
        "candidate": receipt.get("candidate"),
        "rows": rows,
        "unfinished": [row["scanner"] for row in rows if row["state"] == "unfinished"],
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / ADJUDICATION_NAME).write_bytes(
        json.dumps(_sweep(document), indent=2, sort_keys=True).encode("utf-8")
    )
    return _sweep(document)


def _summary_lines(receipt: dict) -> list[str]:
    lines = [
        f"candidate commit: {receipt['candidate']['commit']}",
        f"state: {receipt['state']} unfinished={receipt['unfinished_scanners']}",
    ]
    for name, entry in receipt["scanners"].items():
        counts = [
            str(run.get("findings_count"))
            for run in entry["runs"]
            if run.get("status") not in (None, "not_applicable")
        ]
        lines.append(
            f"{name}: {entry['status']} version={entry.get('version')} "
            f"check={entry['check_state']} exit_codes="
            f"{[run.get('exit_code') for run in entry['runs'] if 'exit_code' in run]} "
            f"findings={counts}"
        )
    for name, count in receipt["payload_scan"].get("counts", {}).items():
        lines.append(f"payload {name}: {count}")
    lines.append(f"receipt digest: {receipt['receipt_digest']}")
    return lines


def _select_scanners(table: dict, selection: str) -> dict:
    if selection == "all":
        return table
    wanted = (
        set()
        if selection == "none"
        else {item.strip() for item in selection.split(",")}
    )
    unknown = wanted - set(SCANNER_NAMES)
    if unknown:
        raise ValueError(f"unknown scanner selection: {sorted(unknown)}")
    selected = {}
    for name, entry in table.items():
        if name in wanted:
            selected[name] = entry
        else:
            selected[name] = {**entry, "status": "not_selected", "command": None}
    return selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ops.certification.audit_candidate",
        description="Run every installed scanner and the payload hygiene scan over a candidate tree.",
    )
    parser.add_argument("--root", required=True, help="candidate repository root")
    parser.add_argument("--output", required=True, help="directory for the receipt")
    parser.add_argument(
        "--scanners",
        default="all",
        help="all, none, or a comma separated subset of " + ",".join(SCANNER_NAMES),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=RUN_TIMEOUT_S,
        help="seconds allowed per scanner run",
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"root is not a directory: {root}", file=sys.stderr)
        return 1
    output_dir = Path(args.output)
    try:
        table = _select_scanners(discover_scanners(), args.scanners)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    results = run_scanners(
        root, output_dir=output_dir, scanners=table, timeout=args.timeout
    )
    payload = scan_repository_payload(root)
    receipt = write_audit_receipt(
        root, output_dir=output_dir, scanner_results=results, payload=payload
    )
    print("\n".join(_summary_lines(receipt)))
    print(f"receipt: {(output_dir / RECEIPT_NAME).resolve().as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
