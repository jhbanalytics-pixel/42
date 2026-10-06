import hashlib
import importlib.metadata
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _ROOT / "configs/pdf_runtime.json"
_WORKER_PATH = Path(__file__).with_name("pdf_renderer_worker.py")
_MAX_STDIO_BYTES = 64 * 1024
_RENDER_PERMIT = threading.Lock()
_FONT_SUFFIXES = {".otf", ".ttf", ".woff", ".woff2"}
_ASSET_SUFFIXES = _FONT_SUFFIXES | {
    ".css",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".webp",
}
_BLOCKED_TAGS = {"base", "embed", "form", "iframe", "object", "script"}
_WORKER_REASONS = {
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
_DIAGNOSTIC_PREFIX = b"PDF_BOUNDARY_DIAGNOSTIC "
_DIAGNOSTIC_FIELDS = {
    "contract_version",
    "reason",
    "branch",
    "pid",
    "ppid",
    "start_time",
    "process_state",
    "previous_ppid",
    "previous_start_time",
    "driver_pid",
    "browser_pid",
    "executable_class",
    "exception_class",
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
_DIAGNOSTIC_EXCEPTIONS = {
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
    "OtherError",
}
_PROCESS_STATES = set("RSDZTtXxKWPI")


class PdfExportError(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def _optional_integer(value, *, positive):
    return value is None or (
        type(value) is int and value >= (1 if positive else 0)
    )


def _validated_diagnostic(stderr):
    if (
        not isinstance(stderr, bytes)
        or len(stderr) > 4096
        or not stderr.endswith(b"\n")
        or stderr.count(b"\n") != 1
        or not stderr.startswith(_DIAGNOSTIC_PREFIX)
    ):
        return None
    payload = stderr[len(_DIAGNOSTIC_PREFIX) : -1]
    if not payload or len(payload) > 2048:
        return None
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if type(value) is not dict or set(value) != _DIAGNOSTIC_FIELDS:
        return None
    if (
        value["contract_version"] != "pdf_boundary_diagnostic_v1"
        or value["reason"] != "pdf_browser_invalid"
        or type(value["branch"]) is not str
        or value["branch"] not in _DIAGNOSTIC_BRANCHES
        or not _optional_integer(value["pid"], positive=True)
        or not _optional_integer(value["ppid"], positive=False)
        or not _optional_integer(value["start_time"], positive=False)
        or not _optional_integer(value["previous_ppid"], positive=False)
        or not _optional_integer(value["previous_start_time"], positive=False)
        or not _optional_integer(value["driver_pid"], positive=True)
        or not _optional_integer(value["browser_pid"], positive=True)
        or (
            value["process_state"] is not None
            and (
                type(value["process_state"]) is not str
                or value["process_state"] not in _PROCESS_STATES
            )
        )
        or (
            value["executable_class"] is not None
            and (
                type(value["executable_class"]) is not str
                or value["executable_class"]
                not in {"node", "browser", "other", "unreadable"}
            )
        )
        or (
            value["exception_class"] is not None
            and (
                type(value["exception_class"]) is not str
                or value["exception_class"] not in _DIAGNOSTIC_EXCEPTIONS
            )
        )
    ):
        return None
    return value


def _forward_native_diagnostic(stderr):
    if os.environ.get("PDF_RENDERER_NATIVE") != "1":
        return
    value = _validated_diagnostic(stderr)
    if value is None:
        return
    sys.stderr.buffer.write(_DIAGNOSTIC_PREFIX + _canonical(value) + b"\n")
    sys.stderr.buffer.flush()


def _load_runtime_config():
    try:
        value = json.loads(
            _CONFIG_PATH.read_bytes().decode("utf-8"), object_pairs_hook=_unique_object
        )
        if set(value) != {
            "assets",
            "browser",
            "contract_version",
            "driver",
            "limits",
            "packages",
            "sandbox",
            "worker_environment",
        }:
            raise ValueError()
        if (
            value["contract_version"] != "pdf_runtime_v1"
            or value["sandbox"] is not True
        ):
            raise ValueError()
        if value["limits"] != {
            "max_asset_bytes": 25 * 1024 * 1024,
            "max_asset_files": 128,
            "max_input_bytes": 5 * 1024 * 1024,
            "max_output_bytes": 25 * 1024 * 1024,
            "timeout_seconds": 15,
        }:
            raise ValueError()
        if value["worker_environment"] != {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "TZ": "UTC",
        }:
            raise ValueError()
        if value["assets"] != {
            "root": "/app/web/dist/assets",
            "fonts": {
                "newsreader-variable-tcB4qQtO.woff2": {
                    "family": "Newsreader",
                    "sha256": "1faa3380ac0e87e057b180e03fd94bd708a612afb67d2590677be4508909fae9",
                },
                "recursive-variable-C3fm0w2j.woff2": {
                    "families": ["Recursive Mono", "Recursive Sans"],
                    "sha256": "145e9fc086d13403528384bdace7f2a4d5ecef72a2b10a749e99382dbecfce79",
                },
            },
        }:
            raise ValueError()
        return value
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ):
        raise PdfExportError("pdf_runtime_invalid") from None


def _reference(raw, base=""):
    value = raw.strip().strip("\"'")
    if not value or value.startswith("#") or value.startswith("data:"):
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        raise PdfExportError("pdf_html_unsafe")
    if parsed.query or "\\" in value or "\x00" in value:
        raise PdfExportError("pdf_asset_invalid")
    path = PurePosixPath(unquote(parsed.path))
    if path.is_absolute() or ".." in path.parts:
        raise PdfExportError("pdf_asset_invalid")
    path = PurePosixPath(base) / path
    path = PurePosixPath(*[part for part in path.parts if part not in {"", "."}])
    if not path.parts or path.suffix.lower() not in _ASSET_SUFFIXES:
        raise PdfExportError("pdf_asset_invalid")
    return path.as_posix()


def _load_css_parser():
    try:
        if (
            importlib.metadata.version("tinycss2") != "1.5.1"
            or importlib.metadata.version("webencodings") != "0.6.1"
        ):
            raise ValueError()
        import tinycss2

        return tinycss2
    except Exception:
        raise PdfExportError("pdf_runtime_invalid") from None


def _css_references(value, base="", declarations=False):
    tinycss2 = _load_css_parser()

    references = []
    families = {}

    def url_value(node):
        if node.type in {"string", "url"}:
            return node.value
        if node.type == "function" and node.lower_name == "url":
            items = [
                item
                for item in node.arguments
                if item.type not in {"whitespace", "comment"}
            ]
            if len(items) == 1 and items[0].type in {"string", "url"}:
                return items[0].value
        raise PdfExportError("pdf_html_unsafe")

    def contains_function(nodes, name):
        for node in nodes or []:
            if getattr(node, "type", None) == "function":
                if node.lower_name == name or contains_function(node.arguments, name):
                    return True
        return False

    def walk(nodes):
        for node in nodes or []:
            kind = getattr(node, "type", None)
            if kind == "error":
                raise PdfExportError("pdf_html_unsafe")
            if kind == "at-rule" and node.lower_at_keyword == "import":
                items = [
                    item
                    for item in node.prelude
                    if item.type not in {"whitespace", "comment"}
                ]
                if not items:
                    raise PdfExportError("pdf_html_unsafe")
                reference = _reference(url_value(items[0]), base)
                if (
                    reference is None
                    or PurePosixPath(reference).suffix.lower() != ".css"
                ):
                    raise PdfExportError("pdf_html_unsafe")
                references.append(reference)
                continue
            if kind == "url":
                references.append(_reference(node.value, base))
            elif kind == "function" and node.lower_name == "url":
                items = [
                    item
                    for item in node.arguments
                    if item.type not in {"whitespace", "comment"}
                ]
                if len(items) != 1 or items[0].type not in {"string", "url"}:
                    raise PdfExportError("pdf_html_unsafe")
                references.append(_reference(items[0].value, base))
            elif kind == "function" and node.lower_name == "expression":
                raise PdfExportError("pdf_html_unsafe")
            if kind == "ident" and node.lower_value in {"behavior", "-moz-binding"}:
                raise PdfExportError("pdf_html_unsafe")
            for attribute in ("prelude", "content", "arguments", "value"):
                child = getattr(node, attribute, None)
                if isinstance(child, (list, tuple)):
                    walk(child)

    rules = (
        tinycss2.parse_declaration_list(value, skip_comments=True, skip_whitespace=True)
        if declarations
        else tinycss2.parse_stylesheet(value, skip_comments=True, skip_whitespace=True)
    )
    walk(rules)
    for rule in rules:
        if (
            getattr(rule, "type", None) != "at-rule"
            or rule.lower_at_keyword != "font-face"
        ):
            continue
        declarations = tinycss2.parse_declaration_list(
            rule.content, skip_comments=True, skip_whitespace=True
        )
        if any(item.type == "error" for item in declarations):
            raise PdfExportError("pdf_html_unsafe")
        family = None
        font_refs = []
        for item in declarations:
            if item.type != "declaration":
                continue
            if item.lower_name == "font-family":
                family = tinycss2.serialize(item.value).strip().strip("\"'")
            if item.lower_name == "src":
                if contains_function(item.value, "local"):
                    raise PdfExportError("pdf_font_unavailable")
                before = len(references)
                walk(item.value)
                font_refs.extend(
                    item for item in references[before:] if item is not None
                )
        if not family or not font_refs:
            raise PdfExportError("pdf_font_unavailable")
        for reference in font_refs:
            if PurePosixPath(reference).suffix.lower() in _FONT_SUFFIXES:
                families[reference] = family
    return [item for item in references if item is not None], families


class _HtmlAudit(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.assets = []
        self.fonts = []
        self.text = []
        self.in_style = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        values = {str(key).lower(): value for key, value in attrs}
        if tag in _BLOCKED_TAGS or any(key.startswith("on") for key in values):
            raise PdfExportError("pdf_html_unsafe")
        if tag == "meta" and str(values.get("http-equiv", "")).lower() == "refresh":
            raise PdfExportError("pdf_html_unsafe")
        for key in ("href", "poster", "src"):
            if values.get(key):
                if tag == "a" and key == "href":
                    parsed = urlsplit(values[key].strip())
                    if parsed.scheme in {"http", "https"} and parsed.netloc:
                        continue
                    if values[key].strip().startswith("#"):
                        continue
                self.add(_reference(values[key]))
        if values.get("style"):
            self.css(values["style"], declarations=True)
        self.in_style = tag == "style"

    def handle_endtag(self, tag):
        if tag.lower() == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style:
            self.css(data)
        else:
            value = " ".join(data.split())
            if value:
                self.text.append(value)

    def css(self, value, declarations=False):
        references, families = _css_references(value, declarations=declarations)
        for reference in references:
            self.add(reference, families.get(reference))

    def add(self, value, family=None):
        if value is None:
            return
        if value not in self.assets:
            self.assets.append(value)
        if (
            PurePosixPath(value).suffix.lower() in _FONT_SUFFIXES
            and value not in self.fonts
        ):
            self.fonts.append((value, family or PurePosixPath(value).name))


def _safe_source(root, relative, font=False):
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        if current.is_symlink() or current.is_junction():
            raise PdfExportError("pdf_asset_invalid")
    try:
        resolved = current.resolve(strict=True)
    except (OSError, RuntimeError):
        raise PdfExportError(
            "pdf_font_unavailable" if font else "pdf_asset_unavailable"
        ) from None
    if not resolved.is_file() or not resolved.is_relative_to(root):
        raise PdfExportError("pdf_asset_invalid")
    return resolved


def _linked_path(path):
    current = Path(path)
    while current != current.parent:
        if current.is_symlink() or current.is_junction():
            return True
        current = current.parent
    return False


def _svg_references(raw, base):
    import xml.etree.ElementTree as ET

    try:
        if b"<!DOCTYPE" in raw.upper():
            raise ET.ParseError()
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise PdfExportError("pdf_asset_invalid") from None
    references = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag in {"script", "foreignobject"}:
            raise PdfExportError("pdf_html_unsafe")
        for name, value in element.attrib.items():
            name = name.rsplit("}", 1)[-1].lower()
            if name.startswith("on"):
                raise PdfExportError("pdf_html_unsafe")
            if name in {"href", "src"}:
                reference = _reference(value, base)
                if reference:
                    references.append(reference)
            elif name == "style":
                found, _ = _css_references(value, base)
                references.extend(found)
        if tag == "style" and element.text:
            found, _ = _css_references(element.text, base)
            references.extend(found)
    return references


def _prepare_render_input(html_bytes, asset_root, work_root):
    if not isinstance(html_bytes, bytes):
        raise PdfExportError("pdf_input_invalid")
    if len(html_bytes) > 5 * 1024 * 1024:
        raise PdfExportError("pdf_input_too_large")
    try:
        text = html_bytes.decode("utf-8")
        supplied_root = Path(asset_root)
        if _linked_path(supplied_root):
            raise PdfExportError("pdf_asset_invalid")
        root = supplied_root.resolve(strict=True)
    except UnicodeDecodeError:
        raise PdfExportError("pdf_input_invalid") from None
    except (OSError, RuntimeError, TypeError):
        raise PdfExportError("pdf_asset_invalid") from None
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise PdfExportError("pdf_asset_invalid")
    audit = _HtmlAudit()
    audit.feed(text)
    audit.close()
    if not audit.text:
        raise PdfExportError("pdf_text_missing")
    if not audit.fonts:
        raise PdfExportError("pdf_font_unavailable")
    target = Path(work_root)
    target.mkdir(parents=True, exist_ok=False)
    total = 0
    pending = [(item, ()) for item in audit.assets]
    copied = set()
    while pending:
        relative, lineage = pending.pop(0)
        if relative in copied:
            continue
        copied.add(relative)
        font = PurePosixPath(relative).suffix.lower() in _FONT_SUFFIXES
        source = _safe_source(root, relative, font)
        raw = source.read_bytes()
        total += len(raw)
        if total > 25 * 1024 * 1024 or len(copied) > 128:
            raise PdfExportError("pdf_asset_limit")
        suffix = PurePosixPath(relative).suffix.lower()
        base = PurePosixPath(relative).parent.as_posix()
        if base == ".":
            base = ""
        if suffix == ".css":
            try:
                css = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise PdfExportError("pdf_asset_invalid") from None
            found, families = _css_references(css, base)
            for item in found:
                if item == relative or item in lineage:
                    raise PdfExportError("pdf_asset_invalid")
                if item not in copied and all(item != queued for queued, _ in pending):
                    pending.append((item, lineage + (relative,)))
                if PurePosixPath(item).suffix.lower() in _FONT_SUFFIXES:
                    pair = (item, families.get(item) or PurePosixPath(item).name)
                    if pair not in audit.fonts:
                        audit.fonts.append(pair)
        elif suffix == ".svg":
            for item in _svg_references(raw, base):
                if item == relative or item in lineage:
                    raise PdfExportError("pdf_asset_invalid")
                if item not in copied and all(item != queued for queued, _ in pending):
                    pending.append((item, lineage + (relative,)))
        destination = target / Path(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
    html_path = target / "index.html"
    html_path.write_bytes(html_bytes)
    return {
        "html_path": html_path,
        "expected_text": " ".join(audit.text)[:4096],
        "font_names": sorted({family for _name, family in audit.fonts}),
    }


def _renderer_environment(work_root):
    root = str(Path(work_root))
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": root,
        "TMPDIR": root,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TZ": "UTC",
    }


def _verify_file_hash(path, expected, reason):
    item = Path(path)
    if not item.is_file() or item.is_symlink() or item.is_junction():
        raise PdfExportError(reason)
    if hashlib.sha256(item.read_bytes()).hexdigest() != expected:
        raise PdfExportError(reason)


def _proc_table():
    table = {}
    if os.name != "posix" or not Path("/proc").is_dir():
        return table
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            value = (entry / "stat").read_text()
            fields = value[value.rfind(")") + 2 :].split()
            table[int(entry.name)] = (int(fields[1]), int(fields[2]), int(fields[19]))
        except (OSError, ValueError, IndexError):
            continue
    return table


def _tree_members(root_pid):
    table = _proc_table()
    members = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _group, _start) in table.items():
            if parent in members and pid not in members:
                members.add(pid)
                changed = True
    return {pid: (table[pid][2], table[pid][1]) for pid in members if pid in table}


def _process_row(pid):
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
        fields = value[value.rfind(")") + 2 :].split()
        return int(fields[1]), int(fields[2]), int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _worker_streams(process):
    """The worker's stdout and stderr pipes, as this process holds them."""
    streams = (getattr(process, "stdout", None), getattr(process, "stderr", None))
    return tuple(stream for stream in streams if stream is not None)


def _process_descriptors(pid):
    """Yield (link, access mode) for each open descriptor; raise if unreadable."""
    root = f"/proc/{pid}"
    for fd in os.listdir(f"{root}/fd"):
        try:
            link = os.readlink(f"{root}/fd/{fd}")
            info = Path(f"{root}/fdinfo/{fd}").read_text()
        except FileNotFoundError:
            continue
        flags = next(
            (
                line.split()[1]
                for line in info.splitlines()
                if line.startswith("flags:")
            ),
            None,
        )
        if flags is not None:
            yield link, int(flags, 8) & os.O_ACCMODE


def _pipe_writers(names, root_start):
    """Processes that hold a write end of the worker's pipes and started after it.

    A descendant that started its own session and outlived its parent has no
    parent link back to the worker, but it still holds the output it inherited,
    and only a writer can keep that output from ending. A process that started
    before the worker cannot descend from it, so it is never taken as one, and a
    holder of a read end alone is not a writer. A descriptor table that cannot be
    read is skipped here; the pipe itself still shows whether a writer is left.
    """
    if not names or root_start is None or os.name != "posix":
        return {}
    own = os.getpid()
    found = {}
    for pid, (_parent, group, start) in _proc_table().items():
        if pid == own or start < root_start:
            continue
        try:
            writer = any(
                link in names and mode in (os.O_WRONLY, os.O_RDWR)
                for link, mode in _process_descriptors(pid)
            )
        except (OSError, ValueError):
            continue
        if writer:
            found[pid] = (start, group)
            for member, row in _tree_members(pid).items():
                if row[0] >= root_start:
                    found[member] = row
    return found


def _pipe_writer_remains(process):
    """Whether any process still holds a write end of an unread worker pipe."""
    for stream in _worker_streams(process):
        if stream.closed:
            continue
        fd = stream.fileno()
        os.set_blocking(fd, False)
        try:
            for _ in range(64):
                if not os.read(fd, 65536):
                    break
            else:
                return True
        except BlockingIOError:
            return True
    return False


class _ProcessTreeMonitor:
    def __init__(self, pid, streams=()):
        self.pid = pid
        # Pipe names and the worker's start are read before the worker can be
        # reaped, so both still identify it once its parent link is gone.
        self.streams = tuple(streams)
        self.pipes = frozenset(
            f"pipe:[{os.fstat(stream.fileno()).st_ino}]" for stream in self.streams
        )
        row = _process_row(pid) if self.pipes else None
        self.root_start = row[2] if row is not None else None
        self.members = {}
        self.event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.event.wait(0.02):
            self.members.update(_tree_members(self.pid))
        self.members.update(_tree_members(self.pid))

    def stop(self):
        self.event.set()
        self.thread.join(timeout=1)
        self.members.update(_tree_members(self.pid))
        # A pipe this process read to its end has no writer left to find.
        if any(not stream.closed for stream in self.streams):
            self.members.update(_pipe_writers(self.pipes, self.root_start))


def _identity_alive(pid, start):
    row = _proc_table().get(pid)
    return row is not None and row[2] == start


def _reap_owned_adopted(members, root_pid):
    if os.getpid() != 1:
        return
    table = _proc_table()
    for pid, (start, _group) in members.items():
        row = table.get(pid)
        if pid == root_pid or row is None or row[0] != 1 or row[2] != start:
            continue
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def _terminate_process_tree(process, monitor):
    monitor.stop()
    if os.name != "posix":
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        return
    members = dict(monitor.members)
    members.update(_tree_members(process.pid))
    own_group = os.getpgrp()
    groups = {group for _start, group in members.values() if group != own_group}
    root_group = members.get(process.pid, (None, None))[1]
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for group in sorted(groups, key=lambda value: value == root_group):
            if any(
                item_group == group and _identity_alive(pid, start)
                for pid, (start, item_group) in members.items()
            ):
                try:
                    os.killpg(group, sig)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(
            _identity_alive(pid, start) for pid, (start, _group) in members.items()
        ):
            _reap_owned_adopted(members, process.pid)
            time.sleep(0.02)
    if process.poll() is None:
        process.kill()
    process.wait(timeout=2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        _reap_owned_adopted(members, process.pid)
        if not any(
            _identity_alive(pid, start) for pid, (start, _group) in members.items()
        ):
            break
        time.sleep(0.02)
    if any(_identity_alive(pid, start) for pid, (start, _group) in members.items()):
        raise PdfExportError("pdf_cleanup_failed")
    if _pipe_writer_remains(process):
        raise PdfExportError("pdf_cleanup_failed")


def _run_isolated_process(command, environment, *, timeout_seconds, cancel_event=None):
    if set(environment) != {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ"}:
        raise PdfExportError("pdf_environment_invalid")
    kwargs = {
        "env": dict(environment),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(command, **kwargs)
    except OSError:
        raise PdfExportError("pdf_worker_unavailable") from None
    monitor = _ProcessTreeMonitor(
        process.pid, _worker_streams(process) if os.name == "posix" else ()
    )
    deadline = time.monotonic() + timeout_seconds
    reason = None
    try:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                reason = "pdf_worker_cancelled"
                break
            if time.monotonic() >= deadline:
                reason = "pdf_worker_timeout"
                break
            time.sleep(0.02)
        stdout, stderr = (
            process.communicate(timeout=1) if reason is None else (b"", b"")
        )
    except BaseException as error:
        try:
            _terminate_process_tree(process, monitor)
        except BaseException:
            raise PdfExportError("pdf_cleanup_failed") from None
        if isinstance(error, PdfExportError):
            raise
        raise PdfExportError("pdf_worker_failed") from None
    else:
        try:
            _terminate_process_tree(process, monitor)
        except BaseException:
            raise PdfExportError("pdf_cleanup_failed") from None
    if reason is not None:
        raise PdfExportError(reason)
    if len(stdout) > _MAX_STDIO_BYTES or len(stderr) > _MAX_STDIO_BYTES:
        raise PdfExportError("pdf_worker_invalid")
    _forward_native_diagnostic(stderr)
    if process.returncode != 0:
        raise PdfExportError("pdf_worker_failed")
    return stdout


def _decode_worker_reply(raw):
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (
            set(value) == {"contract_version", "ok", "bytes", "sha256"}
            and value["ok"] is True
        ):
            if (
                value["contract_version"] != "pdf_renderer_reply_v1"
                or not isinstance(value["bytes"], int)
                or isinstance(value["bytes"], bool)
                or value["bytes"] <= 0
                or not isinstance(value["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
            ):
                raise ValueError()
            return value
        if set(value) == {"contract_version", "ok", "reason"} and value["ok"] is False:
            if (
                value["contract_version"] == "pdf_renderer_reply_v1"
                and value["reason"] in _WORKER_REASONS
            ):
                raise PdfExportError(value["reason"])
        raise ValueError()
    except PdfExportError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, KeyError):
        raise PdfExportError("pdf_worker_invalid") from None


def _validate_pdf_bytes(data, max_output_bytes, expected_text=None, font_names=None):
    from src.api.pdf_renderer_worker import _WorkerError, validate_pdf_bytes

    try:
        return validate_pdf_bytes(
            data,
            max_output_bytes=max_output_bytes,
            expected_text=expected_text,
            font_names=font_names,
        )
    except _WorkerError as error:
        raise PdfExportError(str(error)) from None


def _worker_request(prepared, output_path):
    return {
        "contract_version": "pdf_renderer_request_v1",
        "html_path": str(prepared["html_path"]),
        "output_path": str(output_path),
        "expected_text": prepared["expected_text"],
        "font_names": prepared["font_names"],
    }


def _render_with_permit(
    html_bytes: bytes,
    *,
    asset_root: Path,
    timeout_seconds: int = 15,
    max_input_bytes: int = 5 * 1024 * 1024,
    max_output_bytes: int = 25 * 1024 * 1024,
) -> bytes:
    config = _load_runtime_config()
    if not isinstance(html_bytes, bytes):
        raise PdfExportError("pdf_input_invalid")
    if len(html_bytes) > max_input_bytes:
        raise PdfExportError("pdf_input_too_large")
    for value, ceiling in (
        (timeout_seconds, 15),
        (max_input_bytes, 5 * 1024 * 1024),
        (max_output_bytes, 25 * 1024 * 1024),
    ):
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= ceiling
        ):
            raise PdfExportError("pdf_limits_invalid")
    with tempfile.TemporaryDirectory(prefix="pdf-render-") as directory:
        work = Path(directory)
        prepared = _prepare_render_input(html_bytes, asset_root, work / "document")
        try:
            supplied = Path(asset_root).resolve(strict=True)
            approved = Path(config["assets"]["root"]).resolve(strict=True)
        except (OSError, RuntimeError):
            raise PdfExportError("pdf_asset_invalid") from None
        if supplied != approved:
            raise PdfExportError("pdf_asset_invalid")
        allowed_families = set()
        for item in config["assets"]["fonts"].values():
            allowed_families.update(item.get("families", [item.get("family")]))
        if not set(prepared["font_names"]) <= allowed_families:
            raise PdfExportError("pdf_font_unavailable")
        for name, details in config["assets"]["fonts"].items():
            _verify_file_hash(
                approved / name, details["sha256"], "pdf_font_unavailable"
            )
        _verify_file_hash(
            config["browser"]["executable"],
            config["browser"]["executable_sha256"],
            "pdf_browser_unavailable",
        )
        output = work / "output.pdf"
        request = _worker_request(prepared, output)
        request_path = work / "request.json"
        request_path.write_bytes(_canonical(request))
        stdout = _run_isolated_process(
            [sys.executable, "-I", str(_WORKER_PATH), str(request_path)],
            _renderer_environment(work),
            timeout_seconds=timeout_seconds,
        )
        reply = _decode_worker_reply(stdout)
        try:
            data = output.read_bytes()
        except OSError:
            raise PdfExportError("pdf_output_invalid") from None
        if len(data) > max_output_bytes:
            raise PdfExportError("pdf_output_too_large")
        if (
            reply["bytes"] != len(data)
            or reply["sha256"] != hashlib.sha256(data).hexdigest()
        ):
            raise PdfExportError("pdf_output_invalid")
        _validate_pdf_bytes(
            data, max_output_bytes, prepared["expected_text"], prepared["font_names"]
        )
        return data


def render_stored_html_pdf(
    html_bytes: bytes,
    *,
    asset_root: Path,
    timeout_seconds: int = 15,
    max_input_bytes: int = 5 * 1024 * 1024,
    max_output_bytes: int = 25 * 1024 * 1024,
) -> bytes:
    if not _RENDER_PERMIT.acquire(blocking=False):
        raise PdfExportError("pdf_render_busy")
    try:
        return _render_with_permit(
            html_bytes,
            asset_root=asset_root,
            timeout_seconds=timeout_seconds,
            max_input_bytes=max_input_bytes,
            max_output_bytes=max_output_bytes,
        )
    finally:
        _RENDER_PERMIT.release()
