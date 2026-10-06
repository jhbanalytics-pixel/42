import hashlib
import importlib.metadata
import io
import json
import os
import sys
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname


_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _ROOT / "configs/pdf_runtime.json"
_ENVIRONMENT = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ"}
_DRIVER_ENVIRONMENT = _ENVIRONMENT | {
    "PW_LANG_NAME",
    "PW_LANG_NAME_VERSION",
    "PW_CLI_DISPLAY_VERSION",
}
_REASONS = {
    "pdf_browser_invalid",
    "pdf_browser_unavailable",
    "pdf_environment_invalid",
    "pdf_font_missing",
    "pdf_network_blocked",
    "pdf_output_empty",
    "pdf_output_invalid",
    "pdf_output_too_large",
    "pdf_package_invalid",
    "pdf_request_invalid",
    "pdf_sandbox_unavailable",
    "pdf_text_missing",
}
_DIAGNOSTIC_BRANCHES = {
    "runtime_executable_hash",
    "lineage_missing_after_refresh",
    "recorded_start_changed",
    "recorded_live_outside_lineage",
    "seen_start_changed",
    "live_executable_unreadable",
    "driver_parent_mismatch",
    "browser_sibling",
    "unexpected_executable",
    "monitor_unexpected_exception",
    "monitor_dead",
    "monitor_stop_timeout",
}
_EXCEPTION_CLASSES = {
    "Exception",
    "RuntimeError",
    "TypeError",
    "KeyError",
    "ValueError",
    "OSError",
    "FileNotFoundError",
    "PermissionError",
    "ProcessLookupError",
    "TimeoutError",
}


class _WorkerError(RuntimeError):
    def __init__(self, reason, diagnostic=None):
        self.diagnostic = diagnostic
        super().__init__(reason)


def _exception_class(error):
    name = type(error).__name__
    return name if name in _EXCEPTION_CLASSES else "OtherError"


def _browser_error(
    branch,
    *,
    pid=None,
    row=None,
    previous=None,
    driver_pid=None,
    browser_pid=None,
    executable_class=None,
    exception=None,
):
    if branch not in _DIAGNOSTIC_BRANCHES:
        raise ValueError("diagnostic branch is invalid")
    ppid, start_time, process_state = row if row is not None else (None, None, None)
    previous_start, previous_parent, _identity = (
        previous if previous is not None else (None, None, None)
    )
    diagnostic = {
        "contract_version": "pdf_boundary_diagnostic_v1",
        "reason": "pdf_browser_invalid",
        "branch": branch,
        "pid": pid,
        "ppid": ppid,
        "start_time": start_time,
        "process_state": process_state,
        "previous_ppid": previous_parent,
        "previous_start_time": previous_start,
        "driver_pid": driver_pid,
        "browser_pid": browser_pid,
        "executable_class": executable_class,
        "exception_class": (
            _exception_class(exception) if exception is not None else None
        ),
    }
    return _WorkerError("pdf_browser_invalid", diagnostic)


def _emit_boundary_diagnostic(error):
    if str(error) != "pdf_browser_invalid" or error.diagnostic is None:
        return
    sys.stderr.buffer.write(
        b"PDF_BOUNDARY_DIAGNOSTIC " + _canonical(error.diagnostic) + b"\n"
    )
    sys.stderr.buffer.flush()


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_config():
    try:
        value = json.loads(_CONFIG_PATH.read_bytes())
        if (
            value["contract_version"] != "pdf_runtime_v1"
            or value["sandbox"] is not True
        ):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise _WorkerError("pdf_request_invalid") from None


def _verify_runtime(config):
    for package, details in config["packages"].items():
        try:
            if importlib.metadata.version(package) != details["version"]:
                raise _WorkerError("pdf_package_invalid")
        except importlib.metadata.PackageNotFoundError:
            raise _WorkerError("pdf_package_invalid") from None
    import playwright

    package = Path(playwright.__file__).resolve().parent
    files = {
        package / "driver/node": config["driver"]["node_sha256"],
        package / "driver/package/browsers.json": config["driver"][
            "browser_manifest_sha256"
        ],
        package / "_impl/_driver.py": config["driver"]["source_sha256"],
    }
    for path, expected in files.items():
        if not path.is_file() or path.is_symlink() or _hash(path) != expected:
            raise _WorkerError("pdf_package_invalid")
    browser = Path(config["browser"]["executable"])
    if not browser.is_file() or browser.is_symlink():
        raise _WorkerError("pdf_browser_unavailable")
    if _hash(browser) != config["browser"]["executable_sha256"]:
        raise _browser_error("runtime_executable_hash")


def _read_request(path):
    try:
        request_path = Path(path).resolve(strict=True)
        value = json.loads(request_path.read_bytes())
        if set(value) != {
            "contract_version",
            "html_path",
            "output_path",
            "expected_text",
            "font_names",
        }:
            raise ValueError()
        if value["contract_version"] != "pdf_renderer_request_v1":
            raise ValueError()
        root = request_path.parent
        html = Path(value["html_path"]).resolve(strict=True)
        output = Path(value["output_path"]).resolve(strict=False)
        if (
            not html.is_file()
            or html.is_symlink()
            or not html.is_relative_to(root)
            or output.parent != root
            or output.exists()
            or not isinstance(value["expected_text"], str)
            or not value["expected_text"].strip()
            or not isinstance(value["font_names"], list)
            or not value["font_names"]
            or any(
                not isinstance(name, str) or Path(name).name != name
                for name in value["font_names"]
            )
        ):
            raise ValueError()
        return value, root, html, output
    except (
        OSError,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ):
        raise _WorkerError("pdf_request_invalid") from None


def _font_objects(page):
    try:
        resources = page["/Resources"].get_object()
        fonts = resources.get("/Font", {}).get_object()
    except Exception:
        return []
    found = []
    for item in fonts.values():
        try:
            font = item.get_object()
            candidates = [font]
            for descendant in font.get("/DescendantFonts", []):
                candidates.append(descendant.get_object())
            for candidate in candidates:
                descriptor = candidate.get("/FontDescriptor")
                if descriptor is None:
                    continue
                descriptor = descriptor.get_object()
                conventional = any(
                    descriptor.get(key) is not None
                    for key in ("/FontFile", "/FontFile2", "/FontFile3")
                )
                if conventional:
                    found.append(str(candidate.get("/BaseFont", "unknown")))
                    continue
                if str(candidate.get("/Subtype")) != "/Type3":
                    continue
                char_procs = candidate.get("/CharProcs")
                if char_procs is None:
                    continue
                char_procs = char_procs.get_object()
                if not char_procs:
                    continue
                streams = []
                for glyph in char_procs.values():
                    glyph = glyph.get_object()
                    data = glyph.get_data()
                    if not isinstance(data, bytes) or not data:
                        raise ValueError()
                    streams.append(data)
                if len(streams) != len(char_procs):
                    continue
                identities = [
                    descriptor.get("/FontFamily"),
                    descriptor.get("/FontName"),
                ]
                found.extend(str(value) for value in identities if value)
        except Exception:
            continue
    return sorted(set(found))


def validate_pdf_bytes(data, *, max_output_bytes, expected_text=None, font_names=None):
    if not isinstance(data, bytes):
        raise _WorkerError("pdf_output_invalid")
    if not data:
        raise _WorkerError("pdf_output_empty")
    if len(data) > max_output_bytes:
        raise _WorkerError("pdf_output_too_large")
    try:
        text, embedded = _read_pdf(data)
    except _WorkerError:
        raise
    except Exception:
        raise _WorkerError("pdf_output_invalid") from None
    if expected_text and " ".join(expected_text.split()) not in text:
        raise _WorkerError("pdf_text_missing")
    if font_names:
        normalized = [
            "".join(character for character in name.casefold() if character.isalnum())
            for name in embedded
        ]
        for required in font_names:
            identity = "".join(
                character for character in required.casefold() if character.isalnum()
            )
            if not identity or not any(identity in name for name in normalized):
                raise _WorkerError("pdf_font_missing")
    return {"text": text, "embedded_fonts": embedded}


def _read_pdf(data):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted or not reader.pages:
        raise ValueError()
    text = " ".join(" ".join(page.extract_text().split()) for page in reader.pages)
    embedded = sorted({name for page in reader.pages for name in _font_objects(page)})
    return text, embedded


def _descendants(root_pid):
    table = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            fields = stat[stat.rfind(")") + 2 :].split()
            table[int(entry.name)] = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
    members = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent in table.items():
            if parent in members and pid not in members:
                members.add(pid)
                changed = True
    return members - {root_pid}


def _process_table():
    table = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            fields = raw[raw.rfind(")") + 2 :].split()
            table[int(entry.name)] = (int(fields[1]), int(fields[19]), fields[0])
        except (OSError, ValueError, IndexError):
            continue
    return table


def _file_identity(path):
    value = Path(path).stat()
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _process_environment(pid):
    try:
        raw = (Path("/proc") / str(pid) / "environ").read_bytes()
        values = {}
        for item in raw.split(b"\0"):
            if not item:
                continue
            key, separator, value = item.partition(b"=")
            if not separator or key in values:
                raise ValueError()
            values[key.decode("ascii")] = value.decode("utf-8")
        return values
    except (OSError, UnicodeDecodeError, ValueError):
        raise _WorkerError("pdf_environment_invalid") from None


class _BrowserBoundaryMonitor:
    def __init__(self, config, root, environment):
        import playwright

        package = Path(playwright.__file__).resolve().parent
        self.worker_pid = os.getpid()
        self.root = Path(root)
        self.environment = dict(environment)
        self.generated = dict(config["driver"]["generated_environment"])
        self.node_path = (package / "driver/node").resolve(strict=True)
        self.browser_path = Path(config["browser"]["executable"]).resolve(strict=True)
        self.node_identity = _file_identity(self.node_path)
        self.browser_identity = _file_identity(self.browser_path)
        self.driver_pid = None
        self.browser_pid = None
        self.records = {}
        self.fault = None
        self.fault_diagnostic = None
        self.lock = threading.RLock()
        self.event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    @staticmethod
    def _under(pid, ancestor, table):
        seen = set()
        while pid in table and pid not in seen:
            seen.add(pid)
            pid = table[pid][0]
            if pid == ancestor:
                return True
        return False

    def _scan(self):
        with self.lock:
            self._scan_locked()

    def _scan_locked(self):
        table = _process_table()
        members = {pid for pid in table if self._under(pid, self.worker_pid, table)}
        missing_live = [
            pid
            for pid in self.records
            if pid in table and table[pid][2] != "Z" and pid not in members
        ]
        if missing_live:
            refreshed = _process_table()
            for pid in missing_live:
                row = refreshed.get(pid)
                if row is None or row[2] == "Z":
                    continue
                if not self._under(pid, self.worker_pid, refreshed):
                    previous = self.records[pid]
                    identity_class = (
                        "node"
                        if previous[2] == self.node_identity
                        else "browser"
                        if previous[2] == self.browser_identity
                        else "other"
                    )
                    raise _browser_error(
                        "lineage_missing_after_refresh",
                        pid=pid,
                        row=row,
                        previous=previous,
                        driver_pid=self.driver_pid,
                        browser_pid=self.browser_pid,
                        executable_class=identity_class,
                    )
            table = refreshed
            members = {pid for pid in table if self._under(pid, self.worker_pid, table)}
        for pid, (recorded_start, _recorded_parent, _identity) in self.records.items():
            row = table.get(pid)
            if row is None:
                continue
            if row[1] != recorded_start:
                raise _browser_error(
                    "recorded_start_changed",
                    pid=pid,
                    row=row,
                    previous=self.records[pid],
                    driver_pid=self.driver_pid,
                    browser_pid=self.browser_pid,
                )
            if row[2] != "Z" and pid not in members:
                raise _browser_error(
                    "recorded_live_outside_lineage",
                    pid=pid,
                    row=row,
                    previous=self.records[pid],
                    driver_pid=self.driver_pid,
                    browser_pid=self.browser_pid,
                )
        for pid in sorted(members):
            parent, start, state = table[pid]
            previous = self.records.get(pid)
            if previous is not None and previous[0] != start:
                raise _browser_error(
                    "seen_start_changed",
                    pid=pid,
                    row=(parent, start, state),
                    previous=previous,
                    driver_pid=self.driver_pid,
                    browser_pid=self.browser_pid,
                )
            try:
                executable = (Path("/proc") / str(pid) / "exe").resolve(strict=True)
                identity = _file_identity(executable)
            except OSError:
                current = _process_table().get(pid)
                if current is not None and not (
                    current[1] == start and current[2] == "Z"
                ):
                    raise _browser_error(
                        "live_executable_unreadable",
                        pid=pid,
                        row=current,
                        previous=previous,
                        driver_pid=self.driver_pid,
                        browser_pid=self.browser_pid,
                        executable_class="unreadable",
                    ) from None
                continue
            if executable == self.node_path and identity == self.node_identity:
                if parent != self.worker_pid or (
                    self.driver_pid is not None and self.driver_pid != pid
                ):
                    raise _browser_error(
                        "driver_parent_mismatch",
                        pid=pid,
                        row=(parent, start, state),
                        previous=previous,
                        driver_pid=self.driver_pid,
                        browser_pid=self.browser_pid,
                        executable_class="node",
                    )
                expected = {**self.environment, **self.generated}
                if _process_environment(pid) != expected:
                    raise _WorkerError("pdf_environment_invalid")
                self.driver_pid = pid
            elif executable == self.browser_path and identity == self.browser_identity:
                if self.browser_pid is None:
                    if self.driver_pid is None or not self._under(
                        pid, self.driver_pid, table
                    ):
                        continue
                    if _process_environment(pid) != self.environment:
                        raise _WorkerError("pdf_environment_invalid")
                    self.browser_pid = pid
                elif pid != self.browser_pid and not self._under(
                    pid, self.browser_pid, table
                ):
                    raise _browser_error(
                        "browser_sibling",
                        pid=pid,
                        row=(parent, start, state),
                        previous=previous,
                        driver_pid=self.driver_pid,
                        browser_pid=self.browser_pid,
                        executable_class="browser",
                    )
            else:
                raise _browser_error(
                    "unexpected_executable",
                    pid=pid,
                    row=(parent, start, state),
                    previous=previous,
                    driver_pid=self.driver_pid,
                    browser_pid=self.browser_pid,
                    executable_class="other",
                )
            self.records[pid] = (start, parent, identity)

    def _run(self):
        while not self.event.wait(0.005):
            try:
                self._scan()
            except _WorkerError as error:
                with self.lock:
                    self.fault = str(error)
                    self.fault_diagnostic = error.diagnostic
                return
            except BaseException as error:
                with self.lock:
                    self.fault = "pdf_browser_invalid"
                    self.fault_diagnostic = _browser_error(
                        "monitor_unexpected_exception", exception=error
                    ).diagnostic
                return

    def verify(self):
        self._scan()
        with self.lock:
            if self.fault is not None:
                raise _WorkerError(self.fault, self.fault_diagnostic)
            if not self.event.is_set() and not self.thread.is_alive():
                raise _browser_error("monitor_dead")
            if self.driver_pid is None or self.browser_pid is None:
                raise _WorkerError("pdf_environment_invalid")

    def stop(self):
        self.event.set()
        self.thread.join(timeout=1)
        if self.thread.is_alive():
            raise _browser_error("monitor_stop_timeout")
        with self.lock:
            if self.fault is not None:
                raise _WorkerError(self.fault, self.fault_diagnostic)


def _validate_worker_environment(config, root):
    expected = dict(config["worker_environment"])
    expected.update(HOME=str(root), TMPDIR=str(root))
    if os.name != "posix" or dict(os.environ) != expected:
        raise _WorkerError("pdf_environment_invalid")


def _verify_sandbox_state():
    try:
        parent_status = (Path("/proc") / "self" / "status").read_text(encoding="utf-8")
        parent_fields = dict(
            line.split(":", 1) for line in parent_status.splitlines() if ":" in line
        )
        parent_filters = parent_fields.get("Seccomp_filters", "").strip()
        if (
            parent_fields.get("Seccomp", "").strip() != "2"
            or not parent_filters.isdigit()
        ):
            raise ValueError()
        parent_filters = int(parent_filters)
    except (OSError, ValueError):
        raise _WorkerError("pdf_sandbox_unavailable") from None
    renderer_seen = False
    for pid in _descendants(os.getpid()):
        try:
            command = (
                (Path("/proc") / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ")
            )
            status = (Path("/proc") / str(pid) / "status").read_text(encoding="utf-8")
        except OSError:
            raise _WorkerError("pdf_sandbox_unavailable") from None
        if b"--no-sandbox" in command:
            raise _WorkerError("pdf_sandbox_unavailable")
        if b"--type=renderer" not in command:
            continue
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        filters = fields.get("Seccomp_filters", "").strip()
        if (
            fields.get("NoNewPrivs", "").strip() != "1"
            or fields.get("Seccomp", "").strip() != "2"
            or not filters.isdigit()
            or int(filters) <= parent_filters
        ):
            raise _WorkerError("pdf_sandbox_unavailable")
        renderer_seen = True
    if not renderer_seen:
        raise _WorkerError("pdf_sandbox_unavailable")


def browser_launch_options(config, environment):
    return {
        "executable_path": config["browser"]["executable"],
        "headless": True,
        "chromium_sandbox": True,
        "env": dict(environment),
    }


def browser_context_options():
    return {
        "java_script_enabled": False,
        "service_workers": "block",
        "accept_downloads": False,
    }


def admitted_request_url(url, root):
    parsed = urlsplit(url)
    if parsed.scheme in {"data", "blob"}:
        return True
    if parsed.scheme != "file":
        return False
    try:
        local = Path(url2pathname(unquote(parsed.path))).resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    return local.is_relative_to(root)


def _render(value, root, html, output, config):
    from playwright.sync_api import Error, sync_playwright

    blocked = []
    browser = context = None
    environment = {key: os.environ[key] for key in _ENVIRONMENT}
    monitor = _BrowserBoundaryMonitor(config, root, environment)
    try:
        with sync_playwright() as playwright:
            try:
                try:
                    browser = playwright.chromium.launch(
                        **browser_launch_options(config, environment)
                    )
                except Error as error:
                    reason = (
                        "pdf_sandbox_unavailable"
                        if "sandbox" in str(error).lower()
                        else "pdf_browser_unavailable"
                    )
                    raise _WorkerError(reason) from None
                context = browser.new_context(**browser_context_options())
                context.set_offline(True)
                page = context.new_page()

                def route_handler(route, request):
                    if admitted_request_url(request.url, root):
                        route.continue_()
                        return
                    blocked.append(request.url)
                    route.abort()

                page.route("**/*", route_handler)
                page.goto(html.as_uri(), wait_until="load", timeout=12000)
                monitor.verify()
                _verify_sandbox_state()
                if blocked:
                    raise _WorkerError("pdf_network_blocked")
                page.emulate_media(media="screen")
                data = page.pdf(
                    format="A4", print_background=True, prefer_css_page_size=True
                )
                validate_pdf_bytes(
                    data,
                    max_output_bytes=25 * 1024 * 1024,
                    expected_text=value["expected_text"],
                    font_names=value["font_names"],
                )
                output.write_bytes(data)
                return data
            finally:
                active_monitor = monitor
                monitor = None
                active_monitor.stop()
    finally:
        try:
            if monitor is not None:
                monitor.stop()
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    output = None
    try:
        if len(args) != 1:
            raise _WorkerError("pdf_environment_invalid")
        config = _load_config()
        value, root, html, output = _read_request(args[0])
        _validate_worker_environment(config, root)
        _verify_runtime(config)
        data = _render(value, root, html, output, config)
        reply = {
            "contract_version": "pdf_renderer_reply_v1",
            "ok": True,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        code = 0
    except _WorkerError as error:
        if output is not None:
            output.unlink(missing_ok=True)
        _emit_boundary_diagnostic(error)
        reason = str(error) if str(error) in _REASONS else "pdf_request_invalid"
        reply = {
            "contract_version": "pdf_renderer_reply_v1",
            "ok": False,
            "reason": reason,
        }
        code = 0
    except Exception:
        if output is not None:
            output.unlink(missing_ok=True)
        reply = {
            "contract_version": "pdf_renderer_reply_v1",
            "ok": False,
            "reason": "pdf_request_invalid",
        }
        code = 1
    sys.stdout.buffer.write(_canonical(reply))
    sys.stdout.buffer.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
