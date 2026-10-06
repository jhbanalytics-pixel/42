import hashlib
import importlib
import json
import os
import subprocess
import threading
import pytest


def diagnostic(**overrides):
    value = {
        "contract_version": "pdf_boundary_diagnostic_v1",
        "reason": "pdf_browser_invalid",
        "branch": "recorded_live_outside_lineage",
        "pid": 31,
        "ppid": 1,
        "start_time": 99,
        "process_state": "S",
        "previous_ppid": 20,
        "previous_start_time": 99,
        "driver_pid": 20,
        "browser_pid": 24,
        "executable_class": "browser",
        "exception_class": None,
    }
    value.update(overrides)
    return value


def exporter():
    try:
        return importlib.import_module("src.api.pdf_exporter")
    except ModuleNotFoundError:
        pytest.fail("pdf exporter module is missing")


def valid_input(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "proof.woff2").write_bytes(b"synthetic-font-fixture")
    html = b"""
    <style>
      @font-face { font-family: Proof; src: url('proof.woff2'); }
      body { font-family: Proof; }
    </style>
    <p>Bounded local PDF proof.</p>
    """
    return html, assets


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("not bytes", "pdf_input_invalid"),
        (b"\xff", "pdf_input_invalid"),
        (b"<script>alert(1)</script>", "pdf_html_unsafe"),
        (b'<img src="https://example.invalid/x.png">', "pdf_html_unsafe"),
        (b'<iframe src="local.html"></iframe>', "pdf_html_unsafe"),
        (b'<meta http-equiv="refresh" content="0;url=local.html">', "pdf_html_unsafe"),
        (
            b'<style>@font-face{src:url("../font.woff2")}</style><p>x</p>',
            "pdf_asset_invalid",
        ),
    ],
)
def test_input_and_asset_escapes_refuse_before_worker(tmp_path, value, reason):
    module = exporter()
    assets = tmp_path / "assets"
    assets.mkdir()
    with pytest.raises(module.PdfExportError, match=reason):
        module.render_stored_html_pdf(value, asset_root=assets)


def test_input_size_is_bounded_before_decode(tmp_path):
    module = exporter()
    assets = tmp_path / "assets"
    assets.mkdir()
    with pytest.raises(module.PdfExportError, match="pdf_input_too_large"):
        module.render_stored_html_pdf(b"x" * (5 * 1024 * 1024 + 1), asset_root=assets)


def test_missing_or_linked_font_refuses(tmp_path):
    module = exporter()
    assets = tmp_path / "assets"
    assets.mkdir()
    html = (
        b'<style>@font-face{font-family:Proof;src:url("missing.woff2")}</style><p>x</p>'
    )
    with pytest.raises(module.PdfExportError, match="pdf_font_unavailable"):
        module._prepare_render_input(html, assets, tmp_path / "work")

    target = assets / "real.woff2"
    target.write_bytes(b"font")
    link = assets / "linked.woff2"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("host does not permit symlink fixtures")
    linked = (
        b'<style>@font-face{font-family:Proof;src:url("linked.woff2")}</style><p>x</p>'
    )
    with pytest.raises(module.PdfExportError, match="pdf_asset_invalid"):
        module._prepare_render_input(linked, assets, tmp_path / "linked-work")


def test_valid_local_input_is_copied_without_links(tmp_path):
    module = exporter()
    html, assets = valid_input(tmp_path)
    prepared = module._prepare_render_input(html, assets, tmp_path / "work")
    assert prepared["html_path"].read_bytes() == html
    assert prepared["expected_text"] == "Bounded local PDF proof."
    assert prepared["font_names"] == ["Proof"]
    copied = prepared["html_path"].parent / "proof.woff2"
    assert copied.read_bytes() == b"synthetic-font-fixture"
    assert not copied.is_symlink()


def test_minimal_environment_excludes_parent_canary_and_credentials(
    tmp_path, monkeypatch
):
    module = exporter()
    monkeypatch.setenv("PARENT_SECRET_CANARY", "must-not-cross")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "forbidden")
    environment = module._renderer_environment(tmp_path)
    assert environment == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }


def test_runtime_file_hash_mismatch_and_missing_browser_are_bounded(tmp_path):
    module = exporter()
    browser = tmp_path / "browser"
    with pytest.raises(module.PdfExportError, match="pdf_browser_unavailable"):
        module._verify_file_hash(browser, "a" * 64, "pdf_browser_unavailable")
    browser.write_bytes(b"changed")
    with pytest.raises(module.PdfExportError, match="pdf_browser_invalid"):
        module._verify_file_hash(browser, "a" * 64, "pdf_browser_invalid")
    module._verify_file_hash(
        browser, hashlib.sha256(b"changed").hexdigest(), "pdf_browser_invalid"
    )


def test_valid_request_refuses_unapproved_asset_root(tmp_path):
    module = exporter()
    html, assets = valid_input(tmp_path)
    with pytest.raises(module.PdfExportError, match="pdf_asset_invalid"):
        module.render_stored_html_pdf(html, asset_root=assets)


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b'{"ok":true}', b'{"ok":false,"reason":"private"}'],
)
def test_malformed_worker_reply_never_crosses_the_boundary(raw):
    module = exporter()
    with pytest.raises(module.PdfExportError, match="pdf_worker_invalid"):
        module._decode_worker_reply(raw)


def test_known_worker_failure_maps_to_bounded_public_reason():
    module = exporter()
    raw = b'{"contract_version":"pdf_renderer_reply_v1","ok":false,"reason":"pdf_sandbox_unavailable"}'
    with pytest.raises(module.PdfExportError, match="pdf_sandbox_unavailable"):
        module._decode_worker_reply(raw)


def test_worker_emits_bounded_failure_with_successful_protocol_exit(capsys):
    worker = importlib.import_module("src.api.pdf_renderer_worker")
    assert worker.main([]) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply == {
        "contract_version": "pdf_renderer_reply_v1",
        "ok": False,
        "reason": "pdf_environment_invalid",
    }


def test_pdf_validation_rejects_malformed_empty_and_oversized_output():
    module = exporter()
    with pytest.raises(module.PdfExportError, match="pdf_output_invalid"):
        module._validate_pdf_bytes(b"not pdf", 25 * 1024 * 1024)
    with pytest.raises(module.PdfExportError, match="pdf_output_empty"):
        module._validate_pdf_bytes(b"", 25 * 1024 * 1024)
    with pytest.raises(module.PdfExportError, match="pdf_output_too_large"):
        module._validate_pdf_bytes(b"%PDF" + b"x" * 100, 16)


def test_runtime_config_binds_approved_linux_hashes():
    module = exporter()
    config = module._load_runtime_config()
    assert config["contract_version"] == "pdf_runtime_v1"
    assert config["browser"] == {
        "version": "151.0.7922.34",
        "revision": "1234",
        "executable": "/opt/playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell",
        "archive_sha256": "3cfc2bd00d1bafcf8a68dc74c9c92bb7150ddc8d26ade948a776316e1cec4f14",
        "executable_sha256": "e11fc9ce65c96313476f7ee9844b6fb6a9220fb048693cfe9eee00acf4170a9f",
    }
    assert (
        config["driver"]["node_sha256"]
        == "f3432a45b03b2da0d270095fdd8813dc34cbea73f5fc8b18c7a384b7cf9b333a"
    )
    assert (
        config["driver"]["source_sha256"]
        == "9a15469de4b0e15773169d32df12a2a694298a8b03ee68655c504532a27d4eaa"
    )
    assert config["driver"]["generated_environment"] == {
        "PW_CLI_DISPLAY_VERSION": "1.62.0",
        "PW_LANG_NAME": "python",
        "PW_LANG_NAME_VERSION": "3.13",
    }
    assert config["packages"]["playwright"] == {
        "version": "1.62.0",
        "wheel_sha256": "ba33bae6a13b3d9d354c751cb618af357d20fe1d57767cbcce52079bbef17ad3",
    }
    assert config["packages"]["pypdf"] == {
        "version": "6.18.1",
        "wheel_sha256": "ee93a2665670ecf57ee81d197a4ca548f3dc15f9cefc56e59b8140866aaa3de5",
    }
    assert config["packages"]["tinycss2"]["version"] == "1.5.1"
    assert config["packages"]["webencodings"]["version"] == "0.6.1"
    assert config["assets"]["fonts"]["newsreader-variable-tcB4qQtO.woff2"] == {
        "family": "Newsreader",
        "sha256": "1faa3380ac0e87e057b180e03fd94bd708a612afb67d2590677be4508909fae9",
    }
    assert config["assets"]["fonts"]["recursive-variable-C3fm0w2j.woff2"][
        "families"
    ] == [
        "Recursive Mono",
        "Recursive Sans",
    ]


def test_runtime_config_sha256_pins_are_complete_lowercase_hex():
    config = exporter()._load_runtime_config()
    pins = [
        config["browser"]["archive_sha256"],
        config["browser"]["executable_sha256"],
        config["driver"]["browser_manifest_sha256"],
        config["driver"]["node_sha256"],
        config["driver"]["source_sha256"],
        *(item["wheel_sha256"] for item in config["packages"].values()),
        *(item["sha256"] for item in config["assets"]["fonts"].values()),
    ]

    assert all(len(pin) == 64 and set(pin) <= set("0123456789abcdef") for pin in pins)


def test_worker_options_require_sandbox_and_disable_active_browser_features(tmp_path):
    module = exporter()
    worker = importlib.import_module("src.api.pdf_renderer_worker")
    config = module._load_runtime_config()
    environment = module._renderer_environment(tmp_path)
    assert worker.browser_launch_options(config, environment) == {
        "executable_path": config["browser"]["executable"],
        "headless": True,
        "chromium_sandbox": True,
        "env": environment,
    }
    assert worker.browser_context_options() == {
        "java_script_enabled": False,
        "service_workers": "block",
        "accept_downloads": False,
    }


def test_worker_url_gate_admits_only_local_root_and_embedded_data(tmp_path):
    worker = importlib.import_module("src.api.pdf_renderer_worker")
    local = tmp_path / "asset.css"
    local.write_text("body{}", encoding="utf-8")
    outside = tmp_path.parent / "outside.css"
    outside.write_text("body{}", encoding="utf-8")
    assert worker.admitted_request_url(local.as_uri(), tmp_path)
    assert worker.admitted_request_url("data:image/png;base64,AA==", tmp_path)
    assert not worker.admitted_request_url(outside.as_uri(), tmp_path)
    assert not worker.admitted_request_url("https://example.invalid/a.css", tmp_path)


def test_worker_request_is_canonical_and_contains_no_parent_environment(tmp_path):
    module = exporter()
    html, assets = valid_input(tmp_path)
    prepared = module._prepare_render_input(html, assets, tmp_path / "work")
    request = module._worker_request(prepared, tmp_path / "out.pdf")
    encoded = module._canonical(request)
    assert json.loads(encoded) == request
    assert "PARENT_SECRET_CANARY" not in encoded.decode()
    assert set(request) == {
        "contract_version",
        "html_path",
        "output_path",
        "expected_text",
        "font_names",
    }


def test_linked_asset_root_refuses_before_resolution(tmp_path):
    module = exporter()
    html, assets = valid_input(tmp_path)
    linked = tmp_path / "linked-assets"
    try:
        linked.symlink_to(assets, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit directory symlink fixtures")
    with pytest.raises(module.PdfExportError, match="pdf_asset_invalid"):
        module._prepare_render_input(html, linked, tmp_path / "work")


@pytest.mark.skipif(os.name != "nt", reason="junction fixture requires Windows")
def test_junction_asset_root_refuses_before_resolution(tmp_path):
    module = exporter()
    html, assets = valid_input(tmp_path)
    linked = tmp_path / "junction-assets"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(linked), str(assets)],
        capture_output=True,
    )
    if result.returncode:
        pytest.skip("host does not permit junction fixtures")
    with pytest.raises(module.PdfExportError, match="pdf_asset_invalid"):
        module._prepare_render_input(html, linked, tmp_path / "work")


@pytest.mark.parametrize(
    "css",
    [
        "@import 'https://example.invalid/nested.css';",
        "body{background:u\\72l(https://example.invalid/leak)}",
        "body{background:url('..\\2f secret.png')}",
        "body{background:url(var(--resource))}",
    ],
)
def test_external_css_resource_escapes_refuse_before_worker(tmp_path, css):
    module = exporter()
    html, assets = valid_input(tmp_path)
    (assets / "theme.css").write_text(css, encoding="utf-8")
    html = html.replace(b"<style>", b'<link rel="stylesheet" href="theme.css"><style>')
    with pytest.raises(module.PdfExportError):
        module._prepare_render_input(html, assets, tmp_path / "work")


def test_external_css_refusal_never_launches_worker(tmp_path, monkeypatch):
    module = exporter()
    html, assets = valid_input(tmp_path)
    (assets / "theme.css").write_text(
        "body{background:url('https://example.invalid/leak')}", encoding="utf-8"
    )
    html = html.replace(b"<style>", b'<link rel="stylesheet" href="theme.css"><style>')
    monkeypatch.setattr(
        module,
        "_run_isolated_process",
        lambda *_args, **_kwargs: pytest.fail("unsafe CSS reached worker launch"),
    )
    with pytest.raises(module.PdfExportError, match="pdf_html_unsafe"):
        module.render_stored_html_pdf(html, asset_root=assets)


def test_nested_local_css_assets_are_audited_and_copied(tmp_path):
    module = exporter()
    html, assets = valid_input(tmp_path)
    (assets / "image.png").write_bytes(b"image")
    (assets / "nested.css").write_text(
        "body{background:url('image.png')}", encoding="utf-8"
    )
    (assets / "theme.css").write_text("@import url('nested.css');", encoding="utf-8")
    html = html.replace(b"<style>", b'<link rel="stylesheet" href="theme.css"><style>')
    prepared = module._prepare_render_input(html, assets, tmp_path / "work")
    assert (prepared["html_path"].parent / "theme.css").is_file()
    assert (prepared["html_path"].parent / "nested.css").is_file()
    assert (prepared["html_path"].parent / "image.png").is_file()


def test_css_cycles_and_svg_network_resources_refuse(tmp_path):
    module = exporter()
    html, assets = valid_input(tmp_path)
    (assets / "a.css").write_text("@import 'b.css';", encoding="utf-8")
    (assets / "b.css").write_text("@import 'a.css';", encoding="utf-8")
    cycle = html.replace(b"<style>", b'<link rel="stylesheet" href="a.css"><style>')
    with pytest.raises(module.PdfExportError, match="pdf_asset_invalid"):
        module._prepare_render_input(cycle, assets, tmp_path / "cycle")
    (assets / "leak.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.invalid/x.png"/></svg>',
        encoding="utf-8",
    )
    image = html.replace(b"<p>", b'<img src="leak.svg"><p>')
    with pytest.raises(module.PdfExportError, match="pdf_html_unsafe"):
        module._prepare_render_input(image, assets, tmp_path / "svg")


def test_inline_style_uses_css_declaration_parser(tmp_path):
    module = exporter()
    html, assets = valid_input(tmp_path)
    styled = html.replace(b"<p>", b'<p style="color: red">')
    module._prepare_render_input(styled, assets, tmp_path / "work")


def test_font_face_cannot_prefer_unbound_local_font(tmp_path):
    module = exporter()
    html, assets = valid_input(tmp_path)
    unsafe = html.replace(
        b"src: url('proof.woff2')",
        b"src: local('Arial'), url('proof.woff2')",
    )
    with pytest.raises(module.PdfExportError, match="pdf_font_unavailable"):
        module._prepare_render_input(unsafe, assets, tmp_path / "work")


@pytest.mark.parametrize(
    "style",
    [
        "background:u\\72l(https://example.invalid/leak)",
        "background:image-set(url('https://example.invalid/leak') 1x)",
        "list-style:url('..\\2f secret.png')",
    ],
)
def test_inline_style_resource_escapes_refuse(tmp_path, style):
    module = exporter()
    html, assets = valid_input(tmp_path)
    escaped = html.replace(b"<p>", f'<p style="{style}">'.encode())
    with pytest.raises(module.PdfExportError):
        module._prepare_render_input(escaped, assets, tmp_path / "work")


def test_css_parser_missing_or_wrong_version_is_bounded(monkeypatch):
    module = exporter()
    monkeypatch.setattr(
        module.importlib.metadata,
        "version",
        lambda name: "0.0" if name == "tinycss2" else "0.6.1",
    )
    with pytest.raises(module.PdfExportError, match="pdf_runtime_invalid"):
        module._load_css_parser()

    def missing(_name):
        raise module.importlib.metadata.PackageNotFoundError()

    monkeypatch.setattr(module.importlib.metadata, "version", missing)
    with pytest.raises(module.PdfExportError, match="pdf_runtime_invalid"):
        module._load_css_parser()


def test_passive_citation_anchor_is_allowed_but_resource_url_is_not(tmp_path):
    module = exporter()
    html, assets = valid_input(tmp_path)
    cited = html.replace(
        b"<p>",
        b'<a href="https://example.invalid/source">Source</a><a href="#note">Note</a><p>',
    )
    module._prepare_render_input(cited, assets, tmp_path / "cited")
    resource = html.replace(b"<p>", b'<img src="https://example.invalid/leak"><p>')
    with pytest.raises(module.PdfExportError, match="pdf_html_unsafe"):
        module._prepare_render_input(resource, assets, tmp_path / "resource")


def test_process_wide_render_gate_refuses_overlap_and_releases(monkeypatch, tmp_path):
    module = exporter()
    entered = threading.Event()
    release = threading.Event()
    calls = []
    monkeypatch.setattr(module, "_load_runtime_config", lambda: {"limits": {}})

    def held(*_args, **_kwargs):
        calls.append(1)
        entered.set()
        release.wait(2)
        raise module.PdfExportError("pdf_browser_unavailable")

    monkeypatch.setattr(module, "_render_with_permit", held)

    def invoke():
        module.render_stored_html_pdf(b"<p>x</p>", asset_root=tmp_path)

    first = threading.Thread(target=lambda: _capture_error(invoke), daemon=True)
    first.start()
    assert entered.wait(1)
    with pytest.raises(module.PdfExportError, match="pdf_render_busy"):
        invoke()
    assert len(calls) == 1
    release.set()
    first.join(2)
    with pytest.raises(module.PdfExportError, match="pdf_browser_unavailable"):
        invoke()
    assert len(calls) == 2


def _capture_error(call):
    try:
        call()
    except Exception:
        pass


def test_cleanup_failure_is_always_bounded(monkeypatch, tmp_path):
    module = exporter()

    class Process:
        pid = 999999
        returncode = 0

        def poll(self):
            return 0

        def communicate(self, timeout):
            return b"{}", b""

    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(
        module,
        "_terminate_process_tree",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("private cleanup")),
    )
    with pytest.raises(module.PdfExportError, match="pdf_cleanup_failed"):
        module._run_isolated_process(
            ["worker"], module._renderer_environment(tmp_path), timeout_seconds=1
        )


def test_root_exit_pipe_timeout_maps_to_bounded_failure(monkeypatch, tmp_path):
    module = exporter()

    class Process:
        pid = 999999
        returncode = 0

        def poll(self):
            return 0

        def communicate(self, timeout):
            raise subprocess.TimeoutExpired("worker", timeout)

    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(module, "_terminate_process_tree", lambda *_args: None)
    with pytest.raises(module.PdfExportError, match="pdf_worker_failed"):
        module._run_isolated_process(
            ["worker"], module._renderer_environment(tmp_path), timeout_seconds=1
        )


def test_native_diagnostic_is_forwarded_canonically_only_under_existing_flag(
    monkeypatch, capfd
):
    module = exporter()
    raw = (
        b"PDF_BOUNDARY_DIAGNOSTIC "
        + json.dumps(diagnostic()).encode("utf-8")
        + b"\n"
    )
    monkeypatch.delenv("PDF_RENDERER_NATIVE", raising=False)
    module._forward_native_diagnostic(raw)
    assert capfd.readouterr().err == ""
    monkeypatch.setenv("PDF_RENDERER_NATIVE", "1")
    module._forward_native_diagnostic(raw)
    emitted = capfd.readouterr().err.encode("utf-8")
    assert emitted == b"PDF_BOUNDARY_DIAGNOSTIC " + module._canonical(diagnostic()) + b"\n"


@pytest.mark.parametrize(
    "raw",
    [
        b"not a diagnostic",
        b"PDF_BOUNDARY_DIAGNOSTIC {}\n",
        b'PDF_BOUNDARY_DIAGNOSTIC {"branch":"monitor_dead","branch":"monitor_stop_timeout"}\n',
        b"PDF_BOUNDARY_DIAGNOSTIC "
        + json.dumps(diagnostic(secret="must-not-log")).encode()
        + b"\n",
        b"PDF_BOUNDARY_DIAGNOSTIC "
        + json.dumps(diagnostic(pid=True)).encode()
        + b"\n",
        b"PDF_BOUNDARY_DIAGNOSTIC "
        + json.dumps(diagnostic(branch="forged")).encode()
        + b"\n",
        b"PDF_BOUNDARY_DIAGNOSTIC "
        + json.dumps(diagnostic(exception_class="SecretError")).encode()
        + b"\n",
        b"PDF_BOUNDARY_DIAGNOSTIC "
        + json.dumps(diagnostic(process_state="SS")).encode()
        + b"\n",
        b"PDF_BOUNDARY_DIAGNOSTIC "
        + json.dumps(diagnostic(ppid=-1)).encode()
        + b"\n",
        b"PDF_BOUNDARY_DIAGNOSTIC "
        + json.dumps(diagnostic(executable_class=[])).encode()
        + b"\n",
        b"PDF_BOUNDARY_DIAGNOSTIC "
        + json.dumps(diagnostic(contract_version="forged")).encode()
        + b"\n",
        b"PDF_BOUNDARY_DIAGNOSTIC "
        + json.dumps(diagnostic(reason="pdf_request_invalid")).encode()
        + b"\n",
        (b"PDF_BOUNDARY_DIAGNOSTIC " + b" " * 2050 + b"\n"),
        (b"PDF_BOUNDARY_DIAGNOSTIC " + json.dumps(diagnostic()).encode() + b"\n") * 2,
        b"x" * 4097,
    ],
)
def test_malformed_or_sensitive_diagnostics_are_never_forwarded(
    monkeypatch, capfd, raw
):
    module = exporter()
    monkeypatch.setenv("PDF_RENDERER_NATIVE", "1")
    module._forward_native_diagnostic(raw)
    assert capfd.readouterr().err == ""


def test_isolated_process_keeps_stdout_bytes_and_forwards_only_valid_diagnostic(
    monkeypatch, tmp_path, capfd
):
    module = exporter()
    stdout = (
        b'{"contract_version":"pdf_renderer_reply_v1","ok":false,'
        b'"reason":"pdf_browser_invalid"}'
    )
    stderr = (
        b"PDF_BOUNDARY_DIAGNOSTIC "
        + json.dumps(diagnostic()).encode("utf-8")
        + b"\n"
    )

    class Process:
        pid = 999999
        returncode = 0

        def poll(self):
            return 0

        def communicate(self, timeout):
            assert timeout == 1
            return stdout, stderr

    monkeypatch.setenv("PDF_RENDERER_NATIVE", "1")
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(module, "_terminate_process_tree", lambda *_args: None)

    returned = module._run_isolated_process(
        ["worker"], module._renderer_environment(tmp_path), timeout_seconds=1
    )

    assert returned == stdout
    assert capfd.readouterr().err.encode("utf-8") == (
        b"PDF_BOUNDARY_DIAGNOSTIC " + module._canonical(diagnostic()) + b"\n"
    )
