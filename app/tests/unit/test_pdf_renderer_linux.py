import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src.api import pdf_exporter
from src.api import pdf_renderer_worker


posix_only = pytest.mark.skipif(
    os.name != "posix", reason="Detached Playwright process groups require Linux"
)


def _pid_exists(pid):
    return Path(f"/proc/{pid}/stat").is_file()


def _assert_container_mode():
    mode = os.environ.get("PDF_RENDERER_PID1_MODE")
    if mode == "no-init":
        assert os.getpid() == 1
    elif mode == "init":
        assert os.getpid() != 1


@posix_only
@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_or_cancellation_removes_detached_descendant_group(tmp_path, cancel):
    _assert_container_mode()
    state = tmp_path / "pids.json"
    script = tmp_path / "tree.py"
    script.write_text(
        "import json,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)\n"
        "open(sys.argv[1],'w').write(json.dumps({'root':__import__('os').getpid(),'child':child.pid}))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    cancel_event = threading.Event()
    if cancel:
        threading.Timer(0.2, cancel_event.set).start()
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"],
        start_new_session=True,
    )
    try:
        with pytest.raises(
            pdf_exporter.PdfExportError,
            match="pdf_worker_cancelled" if cancel else "pdf_worker_timeout",
        ):
            pdf_exporter._run_isolated_process(
                [sys.executable, str(script), str(state)],
                pdf_exporter._renderer_environment(tmp_path),
                timeout_seconds=0.5,
                cancel_event=cancel_event,
            )
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)
    deadline = time.monotonic() + 5
    while not state.is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
    pids = json.loads(state.read_text(encoding="utf-8"))
    assert not _pid_exists(pids["root"])
    assert not _pid_exists(pids["child"])


@posix_only
def test_actual_linux_renderer_requires_packaged_browser_and_native_gate(
    tmp_path, monkeypatch
):
    _assert_container_mode()
    config = pdf_exporter._load_runtime_config()
    browser = Path(config["browser"]["executable"])
    if not browser.is_file() or os.environ.get("PDF_RENDERER_NATIVE") != "1":
        pytest.skip("Actual packaged browser gate runs in the final Linux image")
    asset_root = Path(config["assets"]["root"])
    font = asset_root / "newsreader-variable-tcB4qQtO.woff2"
    assert font.is_file()
    html = f"""
    <style>@font-face{{font-family:Newsreader;src:url('{font.name}')}}body{{font-family:Newsreader}}</style>
    <p>Bounded Linux renderer proof.</p>
    """.encode()
    monkeypatch.setenv("PARENT_SECRET_CANARY", "must-not-cross")
    result = pdf_exporter.render_stored_html_pdf(html, asset_root=asset_root)
    assert result.startswith(b"%PDF-")
    assert len(result) < 25 * 1024 * 1024
    fallback = html.replace(
        b"body{font-family:Newsreader}", b"body{font-family:Missing,sans-serif}"
    )
    with pytest.raises(pdf_exporter.PdfExportError, match="pdf_font_missing"):
        pdf_exporter.render_stored_html_pdf(fallback, asset_root=asset_root)
    from playwright.sync_api import sync_playwright

    environment = pdf_exporter._renderer_environment(tmp_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            executable_path=str(browser),
            headless=True,
            chromium_sandbox=False,
            env=environment,
        )
        try:
            page = browser.new_page()
            page.set_content("<p>Unsandboxed control</p>")
            with pytest.raises(
                pdf_renderer_worker._WorkerError, match="pdf_sandbox_unavailable"
            ):
                pdf_renderer_worker._verify_sandbox_state()
        finally:
            browser.close()


def _boundary_monitor():
    monitor = pdf_renderer_worker._BrowserBoundaryMonitor.__new__(
        pdf_renderer_worker._BrowserBoundaryMonitor
    )
    monitor.worker_pid = 1
    monitor.root = Path("/private")
    monitor.environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/private",
        "TMPDIR": "/private",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }
    monitor.generated = {
        "PW_LANG_NAME": "python",
        "PW_LANG_NAME_VERSION": "3.13",
        "PW_CLI_DISPLAY_VERSION": "1.62.0",
    }
    monitor.node_path = Path("/driver/node")
    monitor.browser_path = Path("/browser/chrome")
    monitor.node_identity = (1, 2, 3, 4, 5)
    monitor.browser_identity = (6, 7, 8, 9, 10)
    monitor.driver_pid = None
    monitor.browser_pid = None
    monitor.records = {}
    monitor.fault = None
    monitor.fault_diagnostic = None
    monitor.lock = threading.RLock()
    monitor.event = threading.Event()
    return monitor


def _mock_boundaries(
    monkeypatch, monitor, node_environment, browser_environment, tail_path=None
):
    table = {2: (1, 20, "S"), 3: (2, 30, "S"), 4: (3, 40, "S")}
    paths = {
        2: monitor.node_path,
        3: monitor.browser_path,
        4: monitor.browser_path if tail_path is None else tail_path,
    }
    identities = {
        monitor.node_path: monitor.node_identity,
        monitor.browser_path: monitor.browser_identity,
        Path("/unknown"): (11, 12, 13, 14, 15),
    }
    environments = {2: node_environment, 3: browser_environment}
    monkeypatch.setattr(pdf_renderer_worker, "_process_table", lambda: table)
    monkeypatch.setattr(
        pdf_renderer_worker, "_process_environment", lambda pid: environments[pid]
    )
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, **_: paths[int(self.parent.name)] if self.name == "exe" else self,
    )
    monkeypatch.setattr(
        pdf_renderer_worker, "_file_identity", lambda path: identities[path]
    )


@pytest.mark.parametrize("boundary", ["node_value", "node_canary", "browser_home"])
def test_node9_and_browser_root6_environments_are_exact(monkeypatch, boundary):
    monitor = _boundary_monitor()
    node = {**monitor.environment, **monitor.generated}
    browser = dict(monitor.environment)
    if boundary == "node_value":
        node["PW_LANG_NAME"] = "wrong"
    elif boundary == "node_canary":
        node["PARENT_SECRET_CANARY"] = "must-not-cross"
    else:
        browser["HOME"] = "/wrong"
    _mock_boundaries(monkeypatch, monitor, node, browser)
    with pytest.raises(
        pdf_renderer_worker._WorkerError, match="pdf_environment_invalid"
    ):
        monitor._scan()


def test_monitor_binds_expected_lineage_and_executables(monkeypatch):
    monitor = _boundary_monitor()
    _mock_boundaries(
        monkeypatch,
        monitor,
        {**monitor.environment, **monitor.generated},
        dict(monitor.environment),
    )
    monitor._scan()
    assert monitor.driver_pid == 2
    assert monitor.browser_pid == 3
    assert set(monitor.records) == {2, 3, 4}


@pytest.mark.parametrize("tail", [Path("/unknown"), Path("/browser/chrome")])
def test_monitor_rejects_unknown_or_changed_live_executable(monkeypatch, tail):
    monitor = _boundary_monitor()
    _mock_boundaries(
        monkeypatch,
        monitor,
        {**monitor.environment, **monitor.generated},
        dict(monitor.environment),
        tail_path=tail,
    )
    if tail == monitor.browser_path:
        monitor.browser_identity = (99, 99, 99, 99, 99)
    with pytest.raises(pdf_renderer_worker._WorkerError, match="pdf_browser_invalid"):
        monitor._scan()


@pytest.mark.parametrize("state,refuses", [("Z", False), ("S", True)])
def test_monitor_allows_only_same_identity_zombie_readlink_race(
    monkeypatch, state, refuses
):
    monitor = _boundary_monitor()
    table = {2: (1, 20, state)}
    monitor.records[2] = (20, 1, monitor.node_identity)
    monkeypatch.setattr(pdf_renderer_worker, "_process_table", lambda: table)
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, **_: (_ for _ in ()).throw(OSError("unreadable")),
    )
    if refuses:
        with pytest.raises(
            pdf_renderer_worker._WorkerError, match="pdf_browser_invalid"
        ):
            monitor._scan()
    else:
        monitor._scan()


def test_recorded_live_process_cannot_leave_request_lineage(monkeypatch):
    monitor = _boundary_monitor()
    monitor.records[4] = (40, 3, monitor.browser_identity)
    monkeypatch.setattr(
        pdf_renderer_worker, "_process_table", lambda: {4: (99, 40, "S")}
    )
    with pytest.raises(pdf_renderer_worker._WorkerError, match="pdf_browser_invalid"):
        monitor._scan()


def test_monitor_unexpected_exception_becomes_bounded_fault(monkeypatch):
    monitor = _boundary_monitor()
    monkeypatch.setattr(
        monitor,
        "_scan",
        lambda: (_ for _ in ()).throw(RuntimeError("private monitor failure")),
    )
    monkeypatch.setattr(monitor.event, "wait", lambda _timeout: False)
    monitor._run()
    assert monitor.fault == "pdf_browser_invalid"
    assert monitor.fault_diagnostic["branch"] == "monitor_unexpected_exception"
    assert monitor.fault_diagnostic["exception_class"] == "RuntimeError"
    retained = monitor.fault_diagnostic
    monkeypatch.setattr(monitor, "_scan", lambda: None)
    with pytest.raises(pdf_renderer_worker._WorkerError) as verified:
        monitor.verify()
    assert verified.value.diagnostic is retained

    class FinishedThread:
        def join(self, timeout):
            assert timeout == 1

        def is_alive(self):
            return False

    monitor.thread = FinishedThread()
    with pytest.raises(pdf_renderer_worker._WorkerError) as stopped:
        monitor.stop()
    assert stopped.value.diagnostic is retained


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (Exception(), "Exception"),
        (RuntimeError(), "RuntimeError"),
        (TypeError(), "TypeError"),
        (KeyError(), "KeyError"),
        (ValueError(), "ValueError"),
        (OSError(), "OSError"),
        (FileNotFoundError(), "FileNotFoundError"),
        (PermissionError(), "PermissionError"),
        (ProcessLookupError(), "ProcessLookupError"),
        (TimeoutError(), "TimeoutError"),
        (ArithmeticError(), "OtherError"),
    ],
)
def test_monitor_exception_names_are_allowlisted(error, expected):
    assert pdf_renderer_worker._exception_class(error) == expected


@pytest.mark.parametrize("branch", sorted(pdf_renderer_worker._DIAGNOSTIC_BRANCHES))
def test_each_browser_branch_preserves_the_public_reason(branch):
    error = pdf_renderer_worker._browser_error(branch)
    assert str(error) == "pdf_browser_invalid"
    assert error.diagnostic["branch"] == branch


def test_worker_diagnostic_keeps_public_reply_bytes_and_excludes_sensitive_values(
    monkeypatch, tmp_path, capfd
):
    html = tmp_path / "input.html"
    html.write_text("<p>secret client text</p>", encoding="utf-8")
    output = tmp_path / "output.pdf"
    request = {
        "expected_text": "secret client text",
        "font_names": ["SensitiveFont"],
    }
    error = pdf_renderer_worker._browser_error(
        "unexpected_executable",
        pid=31,
        row=(20, 99, "S"),
        previous=(99, 20, (1, 2, 3, 4, 5)),
        driver_pid=20,
        browser_pid=24,
        executable_class="other",
    )
    monkeypatch.setattr(pdf_renderer_worker, "_load_config", lambda: {})
    monkeypatch.setattr(
        pdf_renderer_worker,
        "_read_request",
        lambda _path: (request, tmp_path, html, output),
    )
    monkeypatch.setattr(pdf_renderer_worker, "_validate_worker_environment", lambda *_: None)
    monkeypatch.setattr(pdf_renderer_worker, "_verify_runtime", lambda *_: None)
    monkeypatch.setattr(
        pdf_renderer_worker,
        "_render",
        lambda *_: (_ for _ in ()).throw(error),
    )

    assert pdf_renderer_worker.main(["request.json"]) == 0
    captured = capfd.readouterr()
    assert captured.out.encode() == (
        b'{"contract_version":"pdf_renderer_reply_v1","ok":false,'
        b'"reason":"pdf_browser_invalid"}'
    )
    assert captured.err.startswith("PDF_BOUNDARY_DIAGNOSTIC ")
    assert "secret client text" not in captured.err
    assert "SensitiveFont" not in captured.err
    assert "unexpected_executable" in captured.err


def test_monitor_stop_refuses_a_stuck_thread():
    monitor = _boundary_monitor()

    class StuckThread:
        def join(self, timeout):
            assert timeout == 1

        def is_alive(self):
            return True

    monitor.thread = StuckThread()
    with pytest.raises(pdf_renderer_worker._WorkerError, match="pdf_browser_invalid"):
        monitor.stop()


def test_monitor_serializes_overlapping_scans_without_losing_state(monkeypatch):
    monitor = _boundary_monitor()
    first_entered = threading.Event()
    release_first = threading.Event()
    second_returned = threading.Event()
    calls = []
    errors = []

    def scan_locked():
        calls.append(threading.get_ident())
        if len(calls) == 1:
            first_entered.set()
            assert release_first.wait(2)
        monitor.records[len(calls)] = (len(calls), 1, monitor.node_identity)

    monkeypatch.setattr(monitor, "_scan_locked", scan_locked)

    def scan(done=None):
        try:
            monitor._scan()
        except Exception as error:
            errors.append(error)
        finally:
            if done is not None:
                done.set()

    first = threading.Thread(target=scan)
    second = threading.Thread(target=scan, args=(second_returned,))
    first.start()
    assert first_entered.wait(1)
    second.start()
    assert not second_returned.wait(0.1)
    assert len(calls) == 1
    release_first.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(calls) == 2
    assert set(monitor.records) == {1, 2}


def test_monitor_stops_before_playwright_exit_on_font_failure(tmp_path, monkeypatch):
    import playwright.sync_api

    events = []

    class Monitor:
        def verify(self):
            events.append("verify")

        def stop(self):
            events.append("monitor_stop")

    class Page:
        def route(self, *_args):
            pass

        def goto(self, *_args, **_kwargs):
            pass

        def emulate_media(self, **_kwargs):
            pass

        def pdf(self, **_kwargs):
            return b"%PDF fixture"

    class Context:
        def set_offline(self, _value):
            pass

        def new_page(self):
            return Page()

        def close(self):
            events.append("context_close")

    class Browser:
        def new_context(self, **_kwargs):
            return Context()

        def close(self):
            events.append("browser_close")

    class Manager:
        def __enter__(self):
            return type(
                "Playwright",
                (),
                {
                    "chromium": type(
                        "Chromium", (), {"launch": lambda *_a, **_k: Browser()}
                    )()
                },
            )()

        def __exit__(self, *_args):
            events.append("manager_exit")

    monkeypatch.setattr(
        pdf_renderer_worker,
        "_BrowserBoundaryMonitor",
        lambda *_args: Monitor(),
    )
    monkeypatch.setattr(pdf_renderer_worker, "_verify_sandbox_state", lambda: None)
    monkeypatch.setattr(
        pdf_renderer_worker,
        "validate_pdf_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            pdf_renderer_worker._WorkerError("pdf_font_missing")
        ),
    )
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: Manager())
    for key, value in pdf_exporter._renderer_environment(tmp_path).items():
        monkeypatch.setenv(key, value)
    html = tmp_path / "index.html"
    html.write_text("<p>proof</p>", encoding="utf-8")
    with pytest.raises(pdf_renderer_worker._WorkerError, match="pdf_font_missing"):
        pdf_renderer_worker._render(
            {"expected_text": "proof", "font_names": ["Newsreader"]},
            tmp_path,
            html,
            tmp_path / "out.pdf",
            {"browser": {"executable": "/browser"}},
        )
    assert events.index("monitor_stop") < events.index("manager_exit")
    assert events.index("monitor_stop") < events.index("browser_close")


def test_pdf_font_identity_rejects_embedded_fallback(monkeypatch):
    monkeypatch.setattr(
        pdf_renderer_worker,
        "_read_pdf",
        lambda _data: ("Expected text", ["AAAAAA+ArialMT"]),
    )
    with pytest.raises(pdf_renderer_worker._WorkerError, match="pdf_font_missing"):
        pdf_renderer_worker.validate_pdf_bytes(
            b"%PDF fixture",
            max_output_bytes=1024,
            expected_text="Expected text",
            font_names=["Newsreader"],
        )


def test_saved_chromium_type3_newsreader_has_real_glyph_streams():
    fixture = Path(__file__).parents[1] / "fixtures/pdf/newsreader-chromium151.pdf"
    data = fixture.read_bytes()
    assert (
        hashlib.sha256(data).hexdigest()
        == "9c665218a6a7f3d4aff65d47a1aa593120ffb3dece3d8912b51c632d21fbd577"
    )
    result = pdf_renderer_worker.validate_pdf_bytes(
        data,
        max_output_bytes=25 * 1024 * 1024,
        expected_text="Bounded Linux renderer proof.",
        font_names=["Newsreader"],
    )
    assert any("Newsreader" in name for name in result["embedded_fonts"])


class _PdfObject(dict):
    def get_object(self):
        return self


class _Glyph:
    def __init__(self, data=b"glyph"):
        self.data = data

    def get_object(self):
        return self

    def get_data(self):
        return self.data


def _type3_page(change):
    descriptor = _PdfObject(
        {"/FontFamily": "Newsreader", "/FontName": "/AAAAAA+Newsreader-Regular"}
    )
    font = _PdfObject(
        {
            "/Subtype": "/Type3",
            "/FontDescriptor": descriptor,
            "/CharProcs": _PdfObject({"/A": _Glyph()}),
        }
    )
    change(font, descriptor)
    return _PdfObject({"/Resources": _PdfObject({"/Font": _PdfObject({"/F1": font})})})


@pytest.mark.parametrize(
    "change",
    [
        lambda font, _descriptor: font.pop("/CharProcs"),
        lambda font, _descriptor: font.update({"/CharProcs": _PdfObject()}),
        lambda font, _descriptor: font.update(
            {"/CharProcs": _PdfObject({"/A": _Glyph(b"")})}
        ),
        lambda font, _descriptor: font.update(
            {"/CharProcs": _PdfObject({"/A": _PdfObject()})}
        ),
        lambda font, _descriptor: font.pop("/FontDescriptor"),
        lambda _font, descriptor: descriptor.update(
            {"/FontFamily": "Foreign", "/FontName": "/AAAAAA+Foreign-Regular"}
        ),
    ],
)
def test_type3_font_requires_glyph_streams_and_requested_descriptor(change):
    names = pdf_renderer_worker._font_objects(_type3_page(change))
    assert not any("Newsreader" in name for name in names)


def test_sandbox_state_requires_renderer_seccomp_and_no_new_privileges(monkeypatch):
    monkeypatch.setattr(pdf_renderer_worker, "_descendants", lambda _pid: {22})

    def read_bytes(path):
        assert path.name == "cmdline"
        return b"chrome\0--type=renderer\0"

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda self, **_: (
            "Name:\tchrome\nNoNewPrivs:\t0\nSeccomp:\t0\nSeccomp_filters:\t0\n"
        ),
    )
    with pytest.raises(
        pdf_renderer_worker._WorkerError, match="pdf_sandbox_unavailable"
    ):
        pdf_renderer_worker._verify_sandbox_state()
    statuses = {"self": 1, "22": 1}

    def status(path, **_):
        filters = statuses[path.parent.name]
        return (
            f"Name:\tchrome\nNoNewPrivs:\t1\nSeccomp:\t2\nSeccomp_filters:\t{filters}\n"
        )

    monkeypatch.setattr(Path, "read_text", status)
    with pytest.raises(
        pdf_renderer_worker._WorkerError, match="pdf_sandbox_unavailable"
    ):
        pdf_renderer_worker._verify_sandbox_state()
    statuses["22"] = 2
    pdf_renderer_worker._verify_sandbox_state()


@posix_only
def test_root_exit_with_detached_pipe_holder_is_bounded_and_reaped(tmp_path):
    _assert_container_mode()
    state = tmp_path / "pids.json"
    script = tmp_path / "exit.py"
    script.write_text(
        "import json,os,subprocess,sys\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)\n"
        "open(sys.argv[1],'w').write(json.dumps({'child':child.pid}))\n"
        "import time;time.sleep(0.1)\n",
        encoding="utf-8",
    )
    with pytest.raises(pdf_exporter.PdfExportError, match="pdf_worker_failed"):
        pdf_exporter._run_isolated_process(
            [sys.executable, str(script), str(state)],
            pdf_exporter._renderer_environment(tmp_path),
            timeout_seconds=2,
        )
    pid = json.loads(state.read_text(encoding="utf-8"))["child"]
    assert not _pid_exists(pid)
