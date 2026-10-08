"""Offline guard for the CI test run.

Python imports a module called sitecustomize at start-up, so putting this directory on PYTHONPATH loads the guard
into the interpreter that runs pytest and into every Python child that inherits the variable. The guard is a
Python audit hook (PEP 578). It raises before the operation happens when a test would

  * look up or connect to a host that is not this machine (sockets, name lookups, datagrams),
  * start a program that is not on the short list below, or run remote git, or run pip,
  * open or list a credential file.

It is the portable counterpart of the Windows guard used for local runs. It needs no ctypes and no platform
module, so the same file runs on Linux and Windows.

What it cannot see: a program that an allowed shell starts, because the audit hook belongs to this interpreter
only. The CI workflow therefore also holds no credentials and no secrets, so a program that did slip through
would have nothing to sign in with.

Set CORE_OFFLINE_GUARD_LOG to a file name to get one JSON line per refusal (event, a category, the running test).
The line never holds a host name, a path or a command, only the category.
"""
import ipaddress
import json
import os
import posixpath
import re
import sys
import time

GUARD_ID = "f42-offline-guard-v1"
INSTALLED = False

LOCAL_NAMES = {"", "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}

# Programs a test may start. Everything else is refused, so a new tool shows up as a named refusal.
PYTHON = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|pythonw|py|pypy3?)$")
SHELLS = {"bash", "sh", "dash"}
PROGRAMS = {
    "node", "nodejs", "git", "env", "true", "false", "echo", "printf", "cat", "ls", "mkdir", "cp", "mv", "rm",
    "touch", "chmod", "head", "tail", "wc", "sort", "uniq", "diff", "cmp", "sed", "awk", "grep", "find", "xargs",
    "tr", "cut", "tee", "dirname", "basename", "readlink", "realpath", "pwd", "sleep", "date", "uname", "id",
    "whoami", "nproc", "which", "test", "tar", "gzip", "gunzip", "zip", "unzip", "sha256sum", "shasum", "md5sum",
    "ldconfig", "getconf",
}
REMOTE_GIT = {"push", "pull", "fetch", "clone", "ls-remote", "submodule", "request-pull", "send-email", "svn",
              "imap-send", "fetch-pack", "send-pack", "remote-http", "remote-https", "archive-remote"}
GIT_OPTIONS_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
PYTHON_MODULES_REFUSED = {"pip", "pip3", "ensurepip"}

# Credential files, as lower case paths with forward slashes. A directory marker ends in a slash and also matches
# the directory itself.
CREDENTIAL_DIRS = (
    "/.config/gcloud/", "/appdata/roaming/gcloud/", "/.ssh/", "/.aws/", "/.azure/", "/.kube/", "/.config/gh/",
    "/run/secrets/", "/var/run/secrets/",
)
CREDENTIAL_FILES = (
    "/.docker/config.json", "/.netrc", "/.git-credentials", "/.npmrc", "/.pypirc",
    "/application_default_credentials.json",
)


def _refuse(event, kind, error=OSError):
    _record(event, kind)
    raise error(f"offline guard: {kind}")


def _record(event, kind):
    path = os.environ.get("CORE_OFFLINE_GUARD_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": time.time(), "event": event, "detail": kind,
                                     "test": os.environ.get("PYTEST_CURRENT_TEST", "collection")}) + "\n")
    except OSError:
        pass


def _host_is_local(host):
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        try:
            host = bytes(host).decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    host = host.strip().lower().rstrip(".")
    if host in LOCAL_NAMES:
        return True
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback or address.is_unspecified


def _temp_roots():
    roots = {"/tmp", "/var/tmp"}
    for name in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            roots.add(value)
    return [os.path.normcase(os.path.abspath(root)) for root in roots]


def _unix_path_is_temporary(path):
    try:
        path = os.path.normcase(os.path.abspath(os.fsdecode(path)))
    except (TypeError, ValueError):
        return False
    for root in _temp_roots():
        try:
            if os.path.commonpath([path, root]) == root:
                return True
        except ValueError:
            continue
    return False


def _address_is_local(address):
    if isinstance(address, tuple):
        return bool(address) and _host_is_local(address[0])
    if isinstance(address, (str, bytes, os.PathLike)):
        if isinstance(address, bytes) and address[:1] == b"\0":
            return False
        return _unix_path_is_temporary(address)
    return False


def _windows_words(command):
    return [part[1:-1] if part[:1] == '"' and part[-1:] == '"' and len(part) > 1 else part
            for part in re.findall(r'"[^"]*"|\S+', command)]


def _words(command):
    if isinstance(command, (bytes, bytearray)):
        command = os.fsdecode(bytes(command))
    if isinstance(command, str):
        return _windows_words(command)
    if isinstance(command, (list, tuple)):
        return [os.fsdecode(word) if isinstance(word, (bytes, os.PathLike)) else str(word) for word in command]
    return []


def _program_name(executable, words):
    chosen = executable if executable not in (None, "") else (words[0] if words else "")
    if isinstance(chosen, (bytes, os.PathLike)):
        chosen = os.fsdecode(chosen)
    name = posixpath.basename(str(chosen).strip('"').replace("\\", "/")).lower()
    return name[:-4] if name.endswith(".exe") else name


def _git_subcommand(words):
    index = 1
    while index < len(words):
        word = words[index]
        if word in GIT_OPTIONS_WITH_VALUE:
            index += 2
        elif word.startswith("-"):
            index += 1
        else:
            return word, words[index + 1:]
    return None, []


REMOTE_ARGUMENT = re.compile(r"(?:^|[=\s])(?:[a-z][a-z0-9+.\-]*://|[\w.\-]+@[\w.\-]+:)", re.I)


def _check_program(event, executable, words):
    name = _program_name(executable, words)
    if name in PROGRAMS or name in SHELLS or PYTHON.match(name):
        if name == "git":
            sub, rest = _git_subcommand(words)
            if sub in REMOTE_GIT or (sub == "remote" and "update" in rest):
                _refuse(event, "remote git")
            if any(REMOTE_ARGUMENT.search(word) for word in words[1:]):
                _refuse(event, "git with a remote address")
        if PYTHON.match(name):
            for flag, module in zip(words, words[1:]):
                if flag == "-m" and module.lower() in PYTHON_MODULES_REFUSED:
                    _refuse(event, "package install")
        return
    _refuse(event, "program off the list")


def _normal_path(path):
    if isinstance(path, (bytes, bytearray)):
        path = os.fsdecode(bytes(path))
    elif isinstance(path, os.PathLike):
        path = os.fspath(path)
        if isinstance(path, bytes):
            path = os.fsdecode(path)
    if not isinstance(path, str):
        return None
    return posixpath.normpath(path.replace("\\", "/")).lower()


def _named_credential_paths():
    paths = []
    key = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if key:
        paths.append(_normal_path(key))
    config = os.environ.get("CLOUDSDK_CONFIG")
    if config:
        paths.append(_normal_path(config) + "/")
    return paths


def _check_path(event, path):
    normal = _normal_path(path)
    if normal is None:
        return
    held = normal + "/"
    if any(marker in held for marker in CREDENTIAL_DIRS) or any(normal.endswith(name) for name in CREDENTIAL_FILES):
        _refuse(event, "credential file", PermissionError)
    for named in _named_credential_paths():
        if named and (normal == named.rstrip("/") or held.startswith(named)):
            _refuse(event, "credential file", PermissionError)


def _lookup(event, args):
    if not _host_is_local(args[0]):
        _refuse(event, "name lookup off this machine")


def _socket(event, args):
    if not _address_is_local(args[1]):
        _refuse(event, "socket off this machine")


def _nameinfo(event, args):
    if not _address_is_local(args[0]):
        _refuse(event, "name lookup off this machine")


def _popen(event, args):
    _check_program(event, args[0], _words(args[1]))


def _system(event, args):
    _refuse(event, "shell command")


def _exec(event, args):
    _check_program(event, args[0], _words(args[1]))


def _spawn(event, args):
    _check_program(event, args[1], _words(args[2]))


def _open(event, args):
    _check_path(event, args[0])


HANDLERS = {
    "socket.getaddrinfo": _lookup, "socket.gethostbyname": _lookup, "socket.gethostbyname_ex": _lookup,
    "socket.gethostbyaddr": _lookup, "socket.getnameinfo": _nameinfo,
    "socket.connect": _socket, "socket.sendto": _socket, "socket.sendmsg": _socket,
    "subprocess.Popen": _popen, "os.system": _system, "os.exec": _exec, "os.posix_spawn": _exec,
    "os.spawn": _spawn,
    "open": _open, "os.listdir": _open, "os.scandir": _open,
}
CLOSED_ON_ERROR = {"socket.", "subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.spawn"}


def check(event, args):
    """Raise OSError (PermissionError for a credential file) when the event is one the guard refuses."""
    handler = HANDLERS.get(event)
    if handler is None:
        return None
    try:
        handler(event, args)
    except OSError:
        raise
    except Exception:
        if any(event.startswith(prefix) for prefix in CLOSED_ON_ERROR):
            _refuse(event, "event the guard could not read")
    return None


def install():
    global INSTALLED
    if not INSTALLED:
        sys.addaudithook(check)
        INSTALLED = True


if __name__ == "sitecustomize":
    install()
