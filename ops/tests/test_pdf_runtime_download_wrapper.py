"""Pins for the download wrapper the app image build runs around the reviewed PDF installer.

The reviewed installer at ops/build/install_pdf_runtime.py gives each fetch one attempt on
its socket timeout and prints only pdf_runtime_install_refused when a fetch fails, so a slow
answer from snapshot.debian.org took the app image build down without naming a URL. The app
Dockerfile now runs the download through a wrapper it carries as a heredoc: the wrapper
imports the reviewed installer unchanged from the stage's own copy, wraps its _download at
runtime to report every fetch with the bytes received, attempts a fetch again after a
transport fault up to a fixed limit, never attempts again after a size, digest or redirect
refusal, and prints the exception chain before the refusal line when it gives up. The
download as a whole runs under a global budget, so a mirror that stalls on every fetch stops
the build with a readable message rather than running on until the job timeout kills it.

These tests extract the wrapper from the Dockerfile and run it as a subprocess against a stub
installer whose fetches follow a scripted plan, so the retry policy is exercised exactly as the
image build would run it and without the network. The budget is proved against a stub whose
fetches take a set time and against a budget the driver shrinks to match, so nothing waits out
the real budget.
"""

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP_DOCKERFILE = ROOT / "app" / "Dockerfile.general-question"
WRAPPER_PATH = "/tmp/pdf-build-tools/download_pdf_runtime.py"
WRAPPER_BLOCK = re.compile(
    r"COPY <<'PDF_DOWNLOAD_EOF' /tmp/pdf-build-tools/download_pdf_runtime\.py\n(.*?)\nPDF_DOWNLOAD_EOF\n",
    re.DOTALL,
)
MANIFEST_PIN = re.compile(r"--manifest-sha256 ([0-9a-f]{64})")
JOB_TIMEOUT = re.compile(r"^timeout: (\d+)s$", re.MULTILINE)
REVIEWED_NAMES = ("download_runtime", "load_build_manifest", "_download", "verify_archive", "_digest")
ATTEMPTS = 3
BUDGET_SECONDS = 900.0
BUDGET_REFUSAL = "pdf_runtime_download_budget_exceeded"
CLOUDBUILD = ROOT / "cloudbuild.app.publish.yaml"
REVIEWED_MANIFEST = ROOT / "ops" / "build" / "pdf_runtime_dependencies.json"

STUB_INSTALLER = '''
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

PLAN = json.loads(os.environ["STUB_PLAN"])
LOG = Path(os.environ["STUB_LOG"])
PACKAGES = int(os.environ.get("STUB_PACKAGES", "1"))
SLOW_SECONDS = float(os.environ.get("STUB_SLOW_SECONDS", "0"))


def load_build_manifest(path, expected_sha256):
    LOG.open("a", encoding="utf-8").write(f"manifest {Path(path).name} {expected_sha256}\\n")
    names = ["a.deb"] + [f"a{number}.deb" for number in range(1, PACKAGES)]
    return {
        "runtime": {},
        "browser": {"url": "https://cdn.playwright.dev/builds/browser.zip", "archive_size": 5},
        "debian": {
            "packages": [
                {"url": "https://snapshot.debian.org/pool/" + name, "size": 3} for name in names
            ]
        },
    }


def _download(url, path, expected, runtime):
    action = PLAN.pop(0)
    LOG.open("a", encoding="utf-8").write(f"fetch {url} {action}\\n")
    if action in ("slow", "slow_timeout"):
        time.sleep(SLOW_SECONDS)
    if action in ("ok", "slow"):
        with urllib.request.urlopen("data:," + "x" * expected["size"]) as response:
            raw = response.read(1024)
        Path(path).write_bytes(raw)
        return
    if action == "digest":
        raise ValueError("download_invalid")
    if action == "oserror":
        raise OSError(5, "Input/output error")
    if action in ("timeout", "slow_timeout"):
        fault = TimeoutError("timed out")
    elif action == "reset":
        fault = ConnectionResetError(104, "Connection reset by peer")
    else:
        fault = urllib.error.HTTPError(url, int(action), "answer", {}, None)
    try:
        raise fault
    except Exception as error:
        raise ValueError("download_invalid") from error


def download_runtime(manifest, download_root, browser_root):
    root = Path(download_root)
    root.mkdir(parents=True)
    browser = manifest["browser"]
    _download(browser["url"], root / "browser.zip", {"size": browser["archive_size"], "sha256": "x"}, manifest["runtime"])
    for package in manifest["debian"]["packages"]:
        _download(
            package["url"],
            root / package["url"].rsplit("/", 1)[-1],
            package,
            manifest["runtime"],
        )
    return {"browser_members": {}, "debian_archives": len(manifest["debian"]["packages"])}
'''

DRIVER = '''
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("download_pdf_runtime", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.PAUSE_SECONDS = 0
budget = os.environ.get("STUB_BUDGET_SECONDS")
if budget:
    module.BUDGET_SECONDS = float(budget)
raise SystemExit(module.main(sys.argv[2:]))
'''


def dockerfile_text() -> str:
    return APP_DOCKERFILE.read_text(encoding="utf-8")


def wrapper_source() -> str:
    match = WRAPPER_BLOCK.search(dockerfile_text())
    assert match, "the app Dockerfile carries no download wrapper heredoc"
    return match.group(1)


def wrapper_constant(name: str):
    module = ast.parse(wrapper_source())
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"the wrapper defines no {name}")


def build_stage() -> str:
    text = dockerfile_text()
    return text.split("FROM ", 2)[1]


def final_stage() -> str:
    return dockerfile_text().rsplit("FROM ", 1)[1]


def run_wrapper(
    tmp_path: Path,
    plan: list[str],
    *,
    packages: int = 1,
    budget: float | None = None,
    slow_seconds: float = 0.0,
) -> tuple[subprocess.CompletedProcess, list[str]]:
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "install_pdf_runtime.py").write_text(STUB_INSTALLER, encoding="utf-8")
    (tools / "download_pdf_runtime.py").write_text(wrapper_source() + "\n", encoding="utf-8")
    (tools / "manifest.json").write_bytes(b"{}")
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    log = tmp_path / "calls.log"
    environment = dict(
        os.environ,
        STUB_PLAN=json.dumps(plan),
        STUB_LOG=str(log),
        STUB_PACKAGES=str(packages),
        STUB_SLOW_SECONDS=str(slow_seconds),
    )
    if budget is not None:
        environment["STUB_BUDGET_SECONDS"] = str(budget)
    result = subprocess.run(
        [
            sys.executable,
            str(driver),
            str(tools / "download_pdf_runtime.py"),
            "--manifest",
            str(tools / "manifest.json"),
            "--manifest-sha256",
            "0" * 64,
            "--download-root",
            str(tmp_path / "downloads"),
            "--browser-root",
            str(tmp_path / "browser"),
        ],
        capture_output=True,
        env=environment,
        timeout=120,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    return result, calls


def stdout_lines(result: subprocess.CompletedProcess) -> list[str]:
    return result.stdout.decode("utf-8").splitlines()


def fetch_calls(calls: list[str]) -> list[str]:
    return [call for call in calls if call.startswith("fetch ")]


def test_the_build_stage_runs_the_download_through_the_wrapper_with_the_reviewed_arguments() -> None:
    stage = build_stage()
    assert f"COPY <<'PDF_DOWNLOAD_EOF' {WRAPPER_PATH}" in stage
    assert "install_pdf_runtime.py download" not in dockerfile_text()
    run = re.search(r"RUN python /tmp/pdf-build-tools/install_pdf_runtime\.py verify-inputs.*?\n\n", stage, re.DOTALL)
    assert run, "the build stage no longer verifies inputs before downloading"
    command = " ".join(run.group(0).replace("\\\n", " ").split())
    pins = sorted(set(MANIFEST_PIN.findall(command)))
    assert len(pins) == 1, pins
    assert (
        f"&& python {WRAPPER_PATH} --manifest /tmp/pdf-build-tools/pdf_runtime_dependencies.json"
        f" --manifest-sha256 {pins[0]} --download-root /tmp/pdf-runtime-downloads"
        " --browser-root /opt/playwright/chromium_headless_shell-1234"
    ) in command


def test_the_wrapper_stays_out_of_the_final_stage() -> None:
    assert "download_pdf_runtime" not in final_stage()
    assert "PDF_DOWNLOAD_EOF" not in final_stage()


def test_the_wrapper_imports_the_reviewed_installer_rather_than_redefining_it() -> None:
    module = ast.parse(wrapper_source())
    imported = {
        alias.name
        for node in ast.walk(module)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "install_pdf_runtime" in imported
    defined = {node.name for node in ast.walk(module) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert not defined.intersection(REVIEWED_NAMES), defined.intersection(REVIEWED_NAMES)
    source = wrapper_source()
    assert "sys.path.insert(0, str(Path(__file__).resolve().parent))" in source
    assert "installer._download = traced" in source
    assert "urllib.request.urlopen = counting_urlopen" in source
    assert f"ATTEMPTS = {ATTEMPTS}" in source


def test_a_clean_run_prints_the_reviewed_result_and_every_fetch_with_its_bytes(tmp_path: Path) -> None:
    result, calls = run_wrapper(tmp_path, ["ok", "ok"])
    lines = stdout_lines(result)
    assert result.returncode == 0, result.stderr
    assert calls == [
        "manifest manifest.json " + "0" * 64,
        "fetch https://cdn.playwright.dev/builds/browser.zip ok",
        "fetch https://snapshot.debian.org/pool/a.deb ok",
    ]
    assert json.loads(lines[-1]) == {"browser_members": {}, "debian_archives": 1}
    assert lines[0].startswith("fetch 1 attempt 1/3 ok https://cdn.playwright.dev/builds/browser.zip received 5 bytes in ")
    assert lines[1].startswith("fetch 2 attempt 1/3 ok https://snapshot.debian.org/pool/a.deb received 3 bytes in ")


@pytest.mark.parametrize("fault", ["timeout", "reset", "503", "429"])
def test_a_transport_fault_is_attempted_again_and_the_run_completes(tmp_path: Path, fault: str) -> None:
    result, calls = run_wrapper(tmp_path, ["ok", fault, "ok"])
    lines = stdout_lines(result)
    assert result.returncode == 0, result.stderr
    assert [call.split()[-1] for call in calls[1:]] == ["ok", fault, "ok"]
    assert lines[1].startswith("fetch 2 attempt 1/3 failed https://snapshot.debian.org/pool/a.deb received 0 of 3 bytes in ")
    assert lines[2] == "cause 0 ValueError: download_invalid"
    assert lines[3].startswith("cause 1 ")
    assert any(line.startswith("fetch 2 attempt 2/3 ok ") for line in lines)
    assert json.loads(lines[-1]) == {"browser_members": {}, "debian_archives": 1}


def test_repeated_transport_faults_stop_at_the_attempt_limit_and_name_the_cause(tmp_path: Path) -> None:
    result, calls = run_wrapper(tmp_path, ["timeout", "timeout", "timeout", "ok", "ok"])
    lines = stdout_lines(result)
    assert result.returncode == 1
    assert [call.split()[-1] for call in calls[1:]] == ["timeout"] * ATTEMPTS
    assert lines[-1] == "pdf_runtime_install_refused"
    assert sum(1 for line in lines if line.startswith("fetch 1 attempt ") and " failed " in line) == ATTEMPTS
    assert f"fetch 1 attempt {ATTEMPTS}/{ATTEMPTS} failed https://cdn.playwright.dev/builds/browser.zip" in "\n".join(lines)
    assert "cause 1 TimeoutError: timed out" in lines
    assert "Traceback (most recent call last):" in result.stderr.decode("utf-8")


@pytest.mark.parametrize("refusal", ["digest", "404", "403"])
def test_a_refusal_that_is_not_a_transport_fault_is_never_attempted_again(tmp_path: Path, refusal: str) -> None:
    result, calls = run_wrapper(tmp_path, [refusal, "ok", "ok", "ok"])
    lines = stdout_lines(result)
    assert result.returncode == 1
    assert [call.split()[-1] for call in calls[1:]] == [refusal]
    assert lines[-1] == "pdf_runtime_install_refused"
    assert lines[0].startswith("fetch 1 attempt 1/3 failed https://cdn.playwright.dev/builds/browser.zip received 0 of 5 bytes in ")
    if refusal != "digest":
        assert f"cause 1 http status {refusal}" in lines


def test_the_wrapper_carries_a_global_budget_over_the_whole_download() -> None:
    source = wrapper_source()
    assert wrapper_constant("BUDGET_SECONDS") == BUDGET_SECONDS
    assert "deadline = time.monotonic() + BUDGET_SECONDS" in source
    assert "raise budget_refusal(index, url, attempt, overrun, counter)" in source


def test_the_budget_covers_a_slow_mirror_and_still_reports_inside_the_job_timeout() -> None:
    manifest = json.loads(REVIEWED_MANIFEST.read_bytes())
    fetches = 1 + len(manifest["debian"]["packages"])
    attempt_ceiling = manifest["runtime"]["total_timeout_seconds"]
    job_timeout = int(JOB_TIMEOUT.search(CLOUDBUILD.read_text(encoding="utf-8")).group(1))
    assert fetches == 88
    # An honestly slow mirror has to finish: the budget allows every fetch ten seconds, far
    # above the half second a fetch of the degraded stage that took the build down.
    assert BUDGET_SECONDS >= 10 * fetches
    # No attempt starts once the budget has passed, so the download overruns it by at most
    # one attempt, which the reviewed installer caps at its own total timeout. The refusal
    # has to reach the log with the job timeout still far away.
    assert BUDGET_SECONDS + attempt_ceiling <= job_timeout / 2


def test_an_honestly_slow_mirror_still_finishes_inside_the_budget(tmp_path: Path) -> None:
    result, calls = run_wrapper(
        tmp_path, ["slow"] * 4, packages=3, budget=10.0, slow_seconds=0.1
    )
    lines = stdout_lines(result)
    assert result.returncode == 0, result.stderr
    assert len(fetch_calls(calls)) == 4
    assert BUDGET_REFUSAL not in result.stdout.decode("utf-8")
    assert json.loads(lines[-1]) == {"browser_members": {}, "debian_archives": 3}


def test_a_stalling_mirror_stops_at_the_budget_rather_than_running_to_the_job_timeout(
    tmp_path: Path,
) -> None:
    result, calls = run_wrapper(
        tmp_path, ["slow"] * 4, packages=3, budget=0.5, slow_seconds=2.0
    )
    lines = stdout_lines(result)
    assert result.returncode == 1
    assert fetch_calls(calls) == ["fetch https://cdn.playwright.dev/builds/browser.zip slow"]
    assert any(
        line.startswith(
            f"{BUDGET_REFUSAL} the pdf runtime download exceeded its budget of 0.5 seconds by "
        )
        for line in lines
    ), lines
    assert "budget completed fetches 1" in lines
    assert "budget stopped on fetch 2 https://snapshot.debian.org/pool/a.deb" in lines
    assert "budget last transport cause none" in lines
    assert lines[-1] == "pdf_runtime_install_refused"


def test_the_budget_message_names_the_last_transport_cause_it_saw(tmp_path: Path) -> None:
    result, calls = run_wrapper(
        tmp_path,
        ["timeout", "slow", "slow", "slow"],
        packages=3,
        budget=0.5,
        slow_seconds=2.0,
    )
    lines = stdout_lines(result)
    assert result.returncode == 1
    assert [call.split()[-1] for call in fetch_calls(calls)] == ["timeout", "slow"]
    assert "budget completed fetches 1" in lines
    assert "budget stopped on fetch 2 https://snapshot.debian.org/pool/a.deb" in lines
    assert "budget last transport cause TimeoutError: timed out" in lines
    assert lines[-1] == "pdf_runtime_install_refused"


def test_the_budget_stops_a_retry_of_a_slow_transport_fault(tmp_path: Path) -> None:
    result, calls = run_wrapper(
        tmp_path, ["slow_timeout", "ok", "ok"], budget=0.5, slow_seconds=2.0
    )
    lines = stdout_lines(result)
    assert result.returncode == 1
    assert [call.split()[-1] for call in fetch_calls(calls)] == ["slow_timeout"]
    assert any(
        line.startswith(f"{BUDGET_REFUSAL} ") and "stopped before fetch 1 attempt 2/3" in line
        for line in lines
    ), lines
    assert "budget completed fetches 0" in lines
    assert "budget stopped on fetch 1 https://cdn.playwright.dev/builds/browser.zip" in lines
    assert "budget last transport cause TimeoutError: timed out" in lines
    assert lines[-1] == "pdf_runtime_install_refused"


def test_an_oserror_escaping_the_download_names_its_url_and_is_not_attempted_again(
    tmp_path: Path,
) -> None:
    result, calls = run_wrapper(tmp_path, ["oserror", "ok", "ok"])
    lines = stdout_lines(result)
    assert result.returncode == 1
    assert [call.split()[-1] for call in fetch_calls(calls)] == ["oserror"]
    assert lines[0].startswith(
        "fetch 1 attempt 1/3 failed https://cdn.playwright.dev/builds/browser.zip"
        " received 0 of 5 bytes in "
    )
    assert "cause 0 OSError: [Errno 5] Input/output error" in lines
    assert "cause 0 errno 5 Input/output error" in lines
    assert lines[-1] == "pdf_runtime_install_refused"


def test_the_wrapper_and_the_dockerfile_are_plain_ascii_with_unix_line_endings() -> None:
    raw = APP_DOCKERFILE.read_bytes()
    assert raw.decode("ascii")
    assert b"\r" not in raw
