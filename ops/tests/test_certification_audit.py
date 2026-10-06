import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ops.certification import audit_candidate
from ops.certification.audit_candidate import (
    RECEIPT_NAME,
    SCANNER_NAMES,
    SCHEMA,
    _sweep,
    discover_scanners,
    redaction_digest,
    run_scanners,
    scan_repository_payload,
    write_audit_receipt,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCOPES = ("engine", "app", "ops")

# The tokens below are assembled at runtime so that this test file never
# carries an attribution token or a personal path in its own bytes.
CREDIT_TRAILER = "Co-Auth" + "ored-By: " + "Cla" + "ude <noreply@example.com>"
VENDOR_WORD = "Anthro" + "pic"
PERSONAL_PATH = "C:/" + "Users/" + "someone/dev/project/notes.txt"
POSIX_PERSONAL_PATH = "/c/" + "Users/" + "someone/dev/project"
SYNTHETIC_SECRET = "AKIA" + "ZQ7RXW4PMB" + "2NC5T3"
EM_DASH = "\u2014"
EN_DASH = "\u2013"
DOUBLE_HYPHEN = "-" + "-"


def make_tree(root: Path, files: dict[str, str]) -> Path:
    for scope in SCOPES:
        (root / scope).mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


def stub_scanner(tmp_path: Path, *, exit_code: int, stdout: str = "") -> list[str]:
    stub = tmp_path / "stub_scanner.py"
    stub.write_text(
        f"import sys\nsys.stdout.write({stdout!r})\nsys.exit({exit_code})\n",
        encoding="utf-8",
    )
    return [sys.executable, str(stub)]


def scanner_entry(command: list[str] | None, status: str = "installed") -> dict:
    return {
        "status": status,
        "command": command,
        "path": command[-1] if command else None,
        "version": "stub 0.0" if command else None,
        "probe": {"argv": [], "exit_code": None, "error": None},
    }


PERSONAL_MARKERS = (
    b"C:/" + b"Users/",
    b"C:\\" + b"Users\\",
    b"c:/" + b"users/",
    b"/" + b"Users/",
    b"/" + b"home/",
)


def assert_no_personal_path_bytes(written: bytes) -> None:
    lowered = written.lower()
    present = [marker for marker in PERSONAL_MARKERS if marker.lower() in lowered]
    assert present == []


def receipt_bytes(output_dir: Path) -> bytes:
    return b"".join(
        path.read_bytes() for path in sorted(output_dir.rglob("*")) if path.is_file()
    )


@pytest.fixture(scope="module")
def discovered() -> dict:
    return discover_scanners()


def test_scanner_table_names_every_required_scanner():
    assert SCANNER_NAMES == (
        "gitleaks",
        "pip-audit",
        "bandit",
        "semgrep",
        "ruff",
        "bun",
        "trivy",
    )


def test_discover_scanners_records_version_or_absence(discovered):
    assert set(discovered) == set(SCANNER_NAMES)
    for name, entry in discovered.items():
        assert entry["status"] in {"installed", "not_installed", "probe_failed"}, name
        if entry["status"] == "installed":
            assert entry["version"], name
            assert Path(entry["path"]).is_absolute(), name
        elif entry["status"] == "not_installed":
            assert entry["version"] is None
            assert entry["command"] is None
        else:
            assert entry["probe"]["error"], name


def test_discover_scanners_with_nothing_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(audit_candidate.shutil, "which", lambda *_args, **_kw: None)
    monkeypatch.setattr(audit_candidate, "_probe_dirs", lambda: [tmp_path / "empty"])
    table = discover_scanners()
    assert all(entry["status"] == "not_installed" for entry in table.values())
    assert all(entry["version"] is None for entry in table.values())


def test_not_installed_scanner_is_unfinished_not_passed(tmp_path):
    root = make_tree(tmp_path / "tree", {"engine/a.py": "x = 1\n"})
    output = tmp_path / "out"
    table = {"trivy": scanner_entry(None, status="not_installed")}
    results = run_scanners(root, output_dir=output, scanners=table)
    entry = results["trivy"]
    assert entry["check_state"] == "unfinished"
    assert entry["unfinished_reason"] == "not_installed"
    assert entry["runs"] == []
    assert "passed" not in json.dumps(entry).lower()


def test_probe_failed_scanner_is_unfinished(tmp_path):
    root = make_tree(tmp_path / "tree", {"engine/a.py": "x = 1\n"})
    table = {"semgrep": scanner_entry(None, status="probe_failed")}
    table["semgrep"]["probe"]["error"] = "ImportError: cannot import name"
    results = run_scanners(root, output_dir=tmp_path / "out", scanners=table)
    assert results["semgrep"]["check_state"] == "unfinished"
    assert results["semgrep"]["unfinished_reason"] == "probe_failed"


def test_nonzero_exit_is_recorded_with_its_code(tmp_path):
    root = make_tree(tmp_path / "tree", {"engine/a.py": "x = 1\n"})
    table = {"gitleaks": scanner_entry(stub_scanner(tmp_path, exit_code=7))}
    results = run_scanners(root, output_dir=tmp_path / "out", scanners=table)
    entry = results["gitleaks"]
    assert entry["check_state"] == "unfinished"
    assert len(entry["runs"]) == 3
    for run in entry["runs"]:
        assert run["exit_code"] == 7
        assert run["status"] == "failed"
        assert Path(run["argv"][0]).name == Path(sys.executable).name
        assert run["argv"][2] == "dir"
        assert run["scope"] in SCOPES


def test_expected_exit_with_parsed_output_completes(tmp_path):
    root = make_tree(tmp_path / "tree", {"engine/a.py": "x = 1\n"})
    table = {
        "gitleaks": scanner_entry(stub_scanner(tmp_path, exit_code=0, stdout="[]"))
    }
    results = run_scanners(root, output_dir=tmp_path / "out", scanners=table)
    entry = results["gitleaks"]
    assert entry["check_state"] == "completed"
    assert [run["findings_count"] for run in entry["runs"]] == [0, 0, 0]
    assert entry["runs"][0]["stdout_sha256"] == hashlib.sha256(b"[]").hexdigest()


def test_redaction_digest_is_stable_and_not_the_value():
    digest = redaction_digest(SYNTHETIC_SECRET)
    assert digest == redaction_digest(SYNTHETIC_SECRET)
    assert digest != redaction_digest(SYNTHETIC_SECRET + "x")
    assert SYNTHETIC_SECRET not in digest
    assert len(digest) == 16


def test_synthetic_secret_is_reported_with_value_redacted(tmp_path, discovered):
    if discovered["gitleaks"]["status"] != "installed":
        pytest.skip("gitleaks is not installed on this machine")
    root = make_tree(
        tmp_path / "tree",
        {"engine/settings.py": f'AWS_ACCESS_KEY_ID = "{SYNTHETIC_SECRET}"\n'},
    )
    output = tmp_path / "out"
    results = run_scanners(
        root, output_dir=output, scanners={"gitleaks": discovered["gitleaks"]}
    )
    engine_run = next(
        run for run in results["gitleaks"]["runs"] if run["scope"] == "engine"
    )
    assert engine_run["status"] == "completed"
    assert engine_run["exit_code"] == 1
    assert engine_run["findings_count"] >= 1
    finding = engine_run["findings"][0]
    assert finding["secret_digest"] == redaction_digest(SYNTHETIC_SECRET)
    assert finding["path"].endswith("settings.py")
    assert finding["line"] == 1
    receipt = write_audit_receipt(
        root,
        output_dir=output,
        scanner_results=results,
        payload=scan_repository_payload(root),
    )
    assert receipt["schema"] == "audit_candidate_v2"
    written = receipt_bytes(output)
    leaked_to_disk = SYNTHETIC_SECRET.encode("utf-8") in written
    leaked_to_results = SYNTHETIC_SECRET in json.dumps(results)
    assert not leaked_to_disk
    assert not leaked_to_results
    assert_no_personal_path_bytes(written)


def test_attribution_token_and_personal_path_are_reported(tmp_path):
    root = make_tree(
        tmp_path / "tree",
        {
            "engine/README.md": f"Notes.\n\n{CREDIT_TRAILER}\n",
            "app/config.py": f'PLAN = "{PERSONAL_PATH}"\n',
            "ops/run.sh": f"cd {POSIX_PERSONAL_PATH}\n",
            "engine/docs/vendor.md": f"The {VENDOR_WORD} console.\n",
        },
    )
    payload = scan_repository_payload(root)
    attribution = {(hit["path"], hit["line"]) for hit in payload["hits"]["attribution"]}
    assert ("engine/README.md", 3) in attribution
    assert ("engine/docs/vendor.md", 1) in attribution
    personal = {
        (hit["path"], hit["line"], hit["form"], hit["name_digest"])
        for hit in payload["hits"]["personal_path"]
    }
    someone = redaction_digest("someone")
    assert ("app/config.py", 1, "windows_drive", someone) in personal
    assert ("ops/run.sh", 1, "shell_drive", someone) in personal
    assert all("match" not in hit for hit in payload["hits"]["personal_path"])
    assert payload["counts"]["attribution"] == len(payload["hits"]["attribution"])
    assert payload["counts"]["personal_path"] == 2


def test_word_containing_vendor_fragment_is_not_reported(tmp_path):
    root = make_tree(
        tmp_path / "tree", {"engine/words.txt": "phil" + VENDOR_WORD.lower() + "\n"}
    )
    payload = scan_repository_payload(root)
    assert payload["hits"]["attribution"] == []


def test_markdown_table_separator_and_code_are_not_reported(tmp_path):
    fence = "`" * 3
    root = make_tree(
        tmp_path / "tree",
        {
            "engine/TABLE.md": (
                "| Term | Meaning |\n"
                "|---|---|\n"
                "| a | b |\n"
                "\n"
                f"{fence}bash\n"
                f"tool {DOUBLE_HYPHEN}flag\n"
                f"{fence}\n"
                "\n"
                f"Use `{DOUBLE_HYPHEN}max_rows` explicitly.\n"
                "\n"
                "---\n"
            ),
        },
    )
    payload = scan_repository_payload(root)
    assert payload["hits"]["punctuation"] == []


def test_dividers_and_multiline_code_spans_are_not_reported(tmp_path):
    tick = "`"
    root = make_tree(
        tmp_path / "tree",
        {
            "engine/fixtures.py": (
                "# Brand fixtures " + "-" * 40 + "\n"
                "# " + "-" * 8 + " Display " + "-" * 8 + "\n"
                "x = 1\n"
            ),
            "engine/SPANS.md": (
                f"Fonts ({tick}ui-monospace,\n"
                f"Menlo{tick}) and {tick}{DOUBLE_HYPHEN}data-mono{tick} stay.\n"
                f"Real prose {DOUBLE_HYPHEN} here.\n"
            ),
        },
    )
    payload = scan_repository_payload(root)
    hits = [(hit["path"], hit["line"]) for hit in payload["hits"]["punctuation"]]
    assert hits == [("engine/SPANS.md", 3)]


def test_prose_punctuation_is_reported_by_kind(tmp_path):
    root = make_tree(
        tmp_path / "tree",
        {
            "engine/PROSE.md": f"One{EM_DASH}two.\nThree {EN_DASH} four.\nFive {DOUBLE_HYPHEN} six.\n",
            "engine/mod.py": (
                f'"""Doc{EM_DASH}string."""\n'
                f"x = '{EM_DASH}'\n"
                f"y = 1  # note {DOUBLE_HYPHEN} here\n"
            ),
            "engine/q.sql": f"-- header {EN_DASH} note\nSELECT 1;\n",
            "engine/c.yaml": f"key: value # with {EM_DASH} dash\nother: 'a {EM_DASH} b'\n",
            "engine/d.json": f'{{"text": "a {DOUBLE_HYPHEN} b", "n": 1}}\n',
            "engine/e.js": f"const a = 'x {EM_DASH} y'; // comment {EM_DASH} here\n",
            "ops/tests/fixtures/f.md": f"fixture {EM_DASH} data\n",
        },
    )
    payload = scan_repository_payload(root)
    hits = {
        (hit["path"], hit["line"], hit["kind"])
        for hit in payload["hits"]["punctuation"]
    }
    assert hits == {
        ("engine/PROSE.md", 1, "em_dash"),
        ("engine/PROSE.md", 2, "en_dash"),
        ("engine/PROSE.md", 3, "double_hyphen"),
        ("engine/mod.py", 1, "em_dash"),
        ("engine/mod.py", 3, "double_hyphen"),
        ("engine/q.sql", 1, "en_dash"),
        ("engine/c.yaml", 1, "em_dash"),
        ("engine/d.json", 1, "double_hyphen"),
        ("engine/e.js", 1, "em_dash"),
    }
    assert payload["counts"]["punctuation"] == 9


def test_receipt_digest_changes_when_tree_changes(tmp_path):
    root = make_tree(
        tmp_path / "tree", {"engine/a.py": "x = 1\n", "app/b.py": "y = 2\n"}
    )
    table = {"trivy": scanner_entry(None, status="not_installed")}
    results = run_scanners(root, output_dir=tmp_path / "out1", scanners=table)
    first = write_audit_receipt(
        root,
        output_dir=tmp_path / "out1",
        scanner_results=results,
        payload=scan_repository_payload(root),
    )
    again = write_audit_receipt(
        root,
        output_dir=tmp_path / "out2",
        scanner_results=results,
        payload=scan_repository_payload(root),
    )
    assert first["receipt_digest"] == again["receipt_digest"]
    (root / "engine" / "a.py").write_bytes(b"x = 2\n")
    changed = write_audit_receipt(
        root,
        output_dir=tmp_path / "out3",
        scanner_results=results,
        payload=scan_repository_payload(root),
    )
    assert changed["receipt_digest"] != first["receipt_digest"]
    assert (
        changed["scopes"]["engine"]["tree_digest"]
        != first["scopes"]["engine"]["tree_digest"]
    )
    assert (
        changed["scopes"]["app"]["tree_digest"] == first["scopes"]["app"]["tree_digest"]
    )
    assert first["adjudication"] == []
    assert set(first["scopes"]) == set(SCOPES)
    written = json.loads(
        (tmp_path / "out1" / "audit_candidate_v2.json").read_text("utf-8")
    )
    assert written["receipt_digest"] == first["receipt_digest"]


def test_receipt_records_candidate_commit_for_this_repository(tmp_path):
    receipt = write_audit_receipt(
        REPO_ROOT,
        output_dir=tmp_path / "out",
        scanner_results={},
        payload={"counts": {}, "hits": {}, "rules": {}},
    )
    commit = receipt["candidate"]["commit"]
    assert commit is None or len(commit) == 40


def cli(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ops.certification.audit_candidate", *args],
        cwd=str(cwd),
        capture_output=True,
        timeout=120,
        check=False,
    )


def test_cli_parser_help_and_missing_root():
    helped = cli("--help")
    assert helped.returncode == 0
    text = helped.stdout.decode("utf-8", errors="replace")
    assert "--root" in text and "--output" in text and "--scanners" in text
    missing = cli("--output", "x")
    assert missing.returncode == 2
    assert b"--root" in missing.stderr


def test_cli_writes_receipt_with_payload_only(tmp_path):
    root = make_tree(tmp_path / "tree", {"engine/a.py": "x = 1\n"})
    output = tmp_path / "out"
    done = cli("--root", str(root), "--output", str(output), "--scanners", "none")
    assert done.returncode == 0, done.stderr.decode("utf-8", errors="replace")
    receipt = json.loads((output / "audit_candidate_v2.json").read_text("utf-8"))
    assert receipt["schema"] == "audit_candidate_v2"
    assert set(receipt["scanners"]) == set(SCANNER_NAMES)
    assert all(
        entry["check_state"] == "unfinished" for entry in receipt["scanners"].values()
    )
    assert all(
        entry["unfinished_reason"] == "not_selected"
        for entry in receipt["scanners"].values()
    )
    assert "unfinished" in done.stdout.decode("utf-8", errors="replace")
    assert receipt["state"] == "unfinished"
    assert receipt["missing_scanners"] == []


VALID_EMPTY_OUTPUT = {
    "gitleaks": "[]",
    "pip-audit": '{"dependencies": []}',
    "bandit": '{"results": [], "errors": []}',
    "semgrep": '{"results": [], "errors": []}',
    "ruff": "[]",
    "bun": "{}",
    "trivy": '{"Results": []}',
}


def lock_tree(tmp_path: Path) -> Path:
    return make_tree(
        tmp_path / "tree",
        {
            "engine/a.py": "x = 1\n",
            "engine/requirements.lock": "pkg==1.0\n",
            "app/frontend/bun.lock": "{}\n",
        },
    )


@pytest.mark.parametrize("name", ["pip-audit", "bandit", "semgrep", "gitleaks", "ruff"])
def test_empty_stdout_inside_completion_set_is_unfinished(tmp_path, name):
    root = lock_tree(tmp_path)
    table = {name: scanner_entry(stub_scanner(tmp_path, exit_code=1))}
    results = run_scanners(root, output_dir=tmp_path / "out", scanners=table)
    entry = results[name]
    executed = [run for run in entry["runs"] if run["status"] != "not_applicable"]
    assert executed
    assert all(run["status"] == "unfinished" for run in executed)
    assert all(run["reason"] == "empty_output" for run in executed)
    assert all(run["exit_code"] == 1 for run in executed)
    assert all(run["findings_count"] is None for run in executed)
    assert entry["check_state"] == "unfinished"
    assert entry["unfinished_reason"] == "empty_output"
    receipt = write_audit_receipt(
        root,
        output_dir=tmp_path / "out",
        scanner_results=results,
        payload=scan_repository_payload(root),
    )
    assert receipt["state"] == "unfinished"
    assert name in receipt["unfinished_scanners"]


def test_receipt_state_completes_only_when_every_scanner_completed(tmp_path):
    root = lock_tree(tmp_path)
    table = {}
    for name, stdout in VALID_EMPTY_OUTPUT.items():
        stub = tmp_path / f"stub_{name}.py"
        stub.write_text(
            f"import sys\nsys.stdout.write({stdout!r})\nsys.exit(0)\n", encoding="utf-8"
        )
        table[name] = scanner_entry([sys.executable, str(stub)])
    results = run_scanners(root, output_dir=tmp_path / "out", scanners=table)
    assert all(entry["check_state"] == "completed" for entry in results.values())
    receipt = write_audit_receipt(
        root,
        output_dir=tmp_path / "out",
        scanner_results=results,
        payload=scan_repository_payload(root),
    )
    assert receipt["state"] == "complete_pending_adjudication"
    assert receipt["unfinished_scanners"] == []
    assert receipt["missing_scanners"] == []
    partial = write_audit_receipt(
        root,
        output_dir=tmp_path / "out2",
        scanner_results={"gitleaks": results["gitleaks"]},
        payload=scan_repository_payload(root),
    )
    assert partial["state"] == "unfinished"
    assert "trivy" in partial["missing_scanners"]


def test_allowlisted_setting_skips_only_the_identifier(tmp_path):
    product = "gem" + "ini"
    root = make_tree(
        tmp_path / "tree",
        {
            "engine/settings.env": (
                f"GEMINI_MODEL={product}-2.5-flash\n"
                f"GEMINI_MODEL = 'x'  # {CREDIT_TRAILER}\n"
                f"OTHER_KEY = 'x'  # {CREDIT_TRAILER}\n"
                f"GEMINI_LOCATION=global  # generated with {product}\n"
                f"GEMINI_MODEL = 'generated with {product}'\n"
                f"GEMINI_MODEL = 'x'; y = '{CREDIT_TRAILER}'\n"
            ),
        },
    )
    payload = scan_repository_payload(root)
    hits = {(hit["line"], hit["kind"]) for hit in payload["hits"]["attribution"]}
    assert (1, "token") not in hits and (1, "credit") not in hits
    assert {
        (2, "token"),
        (2, "credit"),
        (3, "token"),
        (3, "credit"),
        (4, "credit"),
        (5, "credit"),
        (6, "token"),
        (6, "credit"),
    } <= hits
    assert payload["rules"]["attribution"]["allowlisted_setting_keys"] == [
        "GEMINI_MODEL",
        "GEMINI_LOCATION",
    ]
    policy = payload["rules"]["attribution"]["policy"]
    assert "identifier" in policy and "value" in policy


def test_receipt_bytes_carry_no_personal_path(tmp_path):
    root = make_tree(tmp_path / "tree", {"engine/a.py": "x = 1\n"})
    output = tmp_path / "out"
    table = {
        "gitleaks": scanner_entry(stub_scanner(tmp_path, exit_code=0, stdout="[]"))
    }
    results = run_scanners(root, output_dir=output, scanners=table)
    receipt = write_audit_receipt(
        root,
        output_dir=output,
        scanner_results=results,
        payload=scan_repository_payload(root),
    )
    assert_no_personal_path_bytes(receipt_bytes(output))
    assert receipt["root"] == {
        "name": "tree",
        "path_digest": receipt["root"]["path_digest"],
    }
    assert len(receipt["root"]["path_digest"]) == 16
    run = receipt["scanners"]["gitleaks"]["runs"][0]
    assert run["cwd"] == "engine"
    assert all(
        not Path(item).is_absolute() or item.startswith("~") for item in run["argv"][:2]
    )


def test_posix_personal_paths_are_reported(tmp_path):
    root = make_tree(
        tmp_path / "tree",
        {
            "engine/Dockerfile": "WORKDIR /" + "home/" + "someone/app\n",
            "app/notes.md": "See /" + "Users/" + "someone/work\n",
        },
    )
    payload = scan_repository_payload(root)
    hits = {
        (hit["path"], hit["form"], hit["name_digest"])
        for hit in payload["hits"]["personal_path"]
    }
    assert hits == {
        ("engine/Dockerfile", "posix_home", redaction_digest("someone")),
        ("app/notes.md", "posix_users", redaction_digest("someone")),
    }


def test_binary_and_utf16_files_are_named_in_skipped(tmp_path):
    root = make_tree(tmp_path / "tree", {"engine/a.py": "x = 1\n"})
    (root / "engine" / "blob.bin").write_bytes(b"PK\x03\x04\x00\x00binary")
    (root / "engine" / "notes16.md").write_bytes(
        f"Notes {CREDIT_TRAILER}\n".encode("utf-16")
    )
    payload = scan_repository_payload(root)
    skipped = {(item["path"], item["reason"]) for item in payload["skipped"]}
    assert skipped == {
        ("engine/blob.bin", "binary"),
        ("engine/notes16.md", "utf16_not_scanned"),
    }
    assert payload["files_skipped"] == 2
    assert payload["files_scanned"] == 1


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=str(cwd),
        capture_output=True,
        check=True,
    )


def test_missing_tracked_file_is_listed_not_dropped(tmp_path):
    root = make_tree(
        tmp_path / "tree", {"engine/a.py": "x = 1\n", "engine/gone.py": "y = 2\n"}
    )
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "seed")
    (root / "engine" / "gone.py").unlink()
    payload = scan_repository_payload(root)
    assert payload["file_source"] == "git_ls_files"
    assert {"path": "engine/gone.py", "reason": "missing_from_working_tree"} in payload[
        "skipped"
    ]
    receipt = write_audit_receipt(
        root, output_dir=tmp_path / "out", scanner_results={}, payload=payload
    )
    assert receipt["scopes"]["engine"]["missing_files"] == ["engine/gone.py"]
    assert receipt["scopes"]["engine"]["tracked_files"] == 2
    assert receipt["scopes"]["engine"]["digested_files"] == 1


def test_stderr_tail_and_ruff_message_are_redacted(tmp_path):
    root = make_tree(tmp_path / "tree", {"engine/a.py": "x = 1\n"})
    literal = "sek" + "ret-value"
    stub = tmp_path / "noisy.py"
    stub.write_text(
        "import sys\n"
        f"sys.stderr.write(\"rejected token '{literal}' here\\n\")\n"
        "sys.exit(7)\n",
        encoding="utf-8",
    )
    ruff_stub = tmp_path / "ruffy.py"
    ruff_stub.write_text(
        "import sys\n"
        'sys.stdout.write(\'[{"code": "S105", "filename": "a.py", '
        '"location": {"row": 1, "column": 1}, '
        f'"message": "hardcoded {literal}"}}]\')\n'
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    table = {
        "gitleaks": scanner_entry([sys.executable, str(stub)]),
        "ruff": scanner_entry([sys.executable, str(ruff_stub)]),
    }
    results = run_scanners(root, output_dir=tmp_path / "out", scanners=table)
    dumped = json.dumps(results)
    assert literal not in dumped
    tail = results["gitleaks"]["runs"][0]["stderr_tail"]
    assert redaction_digest(literal) in tail
    finding = results["ruff"]["runs"][0]["findings"][0]
    assert finding["rule_id"] == "S105" and "message" not in finding


def test_schema_is_v2_and_receipt_file_follows():
    assert SCHEMA == "audit_candidate_v2"
    assert RECEIPT_NAME == "audit_candidate_v2.json"


def test_sweep_leaves_urls_intact_and_rewrites_paths(tmp_path):
    url = "https://example.com/" + "home/page"
    dockerfile = "WORKDIR /" + "home/app"
    ci_path = "/" + "home/runner/work"
    assert _sweep(url) == url
    assert _sweep(dockerfile) == "WORKDIR ~"
    assert _sweep(ci_path) == "~/work"
    assert _sweep({"a": [url, ci_path]}) == {"a": [url, "~/work"]}
    root = make_tree(tmp_path / "tree", {"engine/a.py": "x = 1\n"})
    payload = scan_repository_payload(root)
    assert "URL" in payload["rules"]["personal_path"]["sweep"]
