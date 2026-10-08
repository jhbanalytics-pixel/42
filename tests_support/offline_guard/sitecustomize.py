"""Offline guard for the CI test run.

Python imports a module called sitecustomize at start-up, so putting this directory on PYTHONPATH loads the guard
into the interpreter that runs pytest and into every Python child that inherits the variable. The guard is a
Python audit hook (PEP 578). It raises before the operation happens when a test would

  * look up or connect to a host that is not this machine (sockets, name lookups, datagrams),
  * start a program that is not on the short list below, or run remote git, or run pip,
  * open or list a credential file.

A program on the list can start another one, so env, xargs and find are read through to the program they launch,
and node is refused when its code (inline or in the script file it is given) starts a program or reaches the
network. A Python child is refused unless it will load this guard: it must not be started with -I, -E or -S, and
its environment must keep this directory on PYTHONPATH.

It is the portable counterpart of the Windows guard used for local runs. It needs no ctypes and no platform
module, so the same file runs on Linux and Windows.

What it cannot see: a program that an allowed shell starts (bash -c and sh -c), because the audit hook belongs to
this interpreter only; whatever a node script does through a module it imports from another file, since node code
is only searched for the names of the modules and calls that reach the network, in the inline code or the one
script file it is given; and sockets opened by a C extension or by gRPC, which go straight to the operating system
and never raise an audit event. The CI workflow therefore also holds no credentials and no secrets, so a program
that did slip through would have nothing to sign in with.

Set CORE_OFFLINE_GUARD_LOG to a file name to get one JSON line per refusal (event, a category, the running test).
The line never holds a host name, a path or a command, only the category.
"""
import ipaddress
import json
import os
import posixpath
import re
import shlex
import sys
import time

GUARD_ID = "f42-offline-guard-v1"
GUARD_DIR = os.path.normcase(os.path.dirname(os.path.abspath(__file__)))
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
XARGS_SHORT_WITH_VALUE = set("adEILnPs")
XARGS_LONG_WITH_VALUE = {"--arg-file", "--delimiter", "--max-args", "--max-chars", "--max-lines", "--max-procs",
                         "--process-slot-var"}
NODE_PRELOAD = {"-r", "--require", "--import", "--loader", "--experimental-loader"}
NODE_WITH_VALUE = {"--input-type", "--conditions", "-C", "--title", "--env-file", "--stack-size"}
NODE_REACHES_OUT = re.compile(
    r"child_process|\bfetch\s*\(|\bWebSocket\b|\bXMLHttpRequest\b|\bEventSource\b|process\s*\.\s*(?:binding|dlopen)"
    r"|['\"`](?:node:)?(?:https?|http2|net|tls|dns|dgram|cluster|worker_threads|inspector|repl)(?:/promises)?['\"`]")
NODE_FILE_LIMIT = 2_000_000
CHAIN_LIMIT = 8

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


def _env_mapping(env):
    """The environment a child will get, as upper case names to text, or None when it cannot be read."""
    if env is None:
        env = os.environ
    try:
        items = list(env.items())
    except AttributeError:
        return None
    mapping = {}
    try:
        for key, value in items:
            mapping[os.fsdecode(key).upper()] = os.fsdecode(value)
    except (TypeError, ValueError):
        return None
    return mapping


def _keeps_guard(mapping):
    if mapping is None:
        return False
    return any(entry and os.path.normcase(os.path.abspath(entry)) == GUARD_DIR
               for entry in mapping.get("PYTHONPATH", "").split(os.pathsep))


def _python_started_isolated(words):
    """True when the options turn off site or the environment, so sitecustomize (and this guard) would not load."""
    index = 1
    while index < len(words):
        word = words[index]
        index += 1
        if word == "-" or not word.startswith("-"):
            return False
        if word.startswith("--"):
            continue
        for position, letter in enumerate(word[1:], 1):
            if letter in "IES":
                return True
            if letter in "cm":
                return False
            if letter in "WXQ":
                if position == len(word) - 1:
                    index += 1
                break
    return False


def _unwrap_env(words, mapping):
    """The command that `env` starts, and the environment it starts it with."""
    mapping = None if mapping is None else dict(mapping)
    rest = list(words[1:])
    while rest:
        word = rest[0]
        if word == "--":
            rest.pop(0)
            break
        if word in ("-i", "--ignore-environment", "-"):
            mapping = None if mapping is None else {}
            rest.pop(0)
        elif word in ("-u", "--unset") or word.startswith("--unset="):
            name = rest[1] if word in ("-u", "--unset") and len(rest) > 1 else word.partition("=")[2]
            if mapping is not None:
                mapping.pop(name.upper(), None)
            del rest[:2 if word in ("-u", "--unset") else 1]
        elif word in ("-C", "--chdir", "-a", "--argv0"):
            del rest[:2]
        elif word.startswith(("--chdir=", "--argv0=")):
            rest.pop(0)
        elif word in ("-S", "--split-string") or word.startswith("--split-string=") or (
                word.startswith("-S") and len(word) > 2):
            separate = word in ("-S", "--split-string")
            value = (rest[1] if len(rest) > 1 else "") if separate else (
                word.partition("=")[2] if word.startswith("--") else word[2:])
            del rest[:2 if separate else 1]
            try:
                rest[0:0] = shlex.split(value)
            except ValueError:
                _refuse("subprocess.Popen", "program the guard could not read")
        elif word.startswith("-"):
            rest.pop(0)
        elif "=" in word and not word.startswith("="):
            name, _, value = word.partition("=")
            if mapping is not None:
                mapping[name.upper()] = value
            rest.pop(0)
        else:
            break
    return rest, mapping


def _unwrap_xargs(words):
    rest = list(words[1:])
    while rest:
        word = rest[0]
        if word == "--":
            rest.pop(0)
            break
        if word.startswith("--"):
            rest.pop(0)
            if word in XARGS_LONG_WITH_VALUE and rest:
                rest.pop(0)
        elif word.startswith("-") and len(word) > 1:
            rest.pop(0)
            for position, letter in enumerate(word[1:], 1):
                if letter in XARGS_SHORT_WITH_VALUE:
                    if position == len(word) - 1 and rest:
                        rest.pop(0)
                    break
        else:
            break
    return rest


def _find_commands(words):
    commands = []
    index = 1
    while index < len(words):
        if words[index] in ("-exec", "-execdir", "-ok", "-okdir"):
            end = index + 1
            while end < len(words) and words[end] not in (";", "+"):
                end += 1
            if end > index + 1:
                commands.append(words[index + 1:end])
            index = end
        index += 1
    return commands


def _node_reaches_out(code):
    return bool(NODE_REACHES_OUT.search(code))


def _check_node(event, words):
    index = 1
    while index < len(words):
        word = words[index]
        index += 1
        if word == "-":
            _refuse(event, "node reading its code from standard input")
        if word in NODE_PRELOAD or word.startswith(("--require=", "--import=", "--loader=", "--experimental-loader=")):
            _refuse(event, "node preloading code")
        if word in ("-e", "--eval", "-p", "--print", "-pe"):
            code = words[index] if index < len(words) else ""
        elif word.startswith(("--eval=", "--print=")):
            code = word.partition("=")[2]
        elif word in NODE_WITH_VALUE:
            index += 1
            continue
        elif word.startswith("-"):
            continue
        else:
            code = _read_node_file(word)
        if _node_reaches_out(code):
            _refuse(event, "node code that starts a program or reaches the network")
        return


def _read_node_file(name):
    try:
        if os.path.isfile(name) and os.path.getsize(name) <= NODE_FILE_LIMIT:
            with open(name, encoding="utf-8", errors="replace") as stream:
                return stream.read()
    except OSError:
        pass
    return ""


def _check_program(event, executable, words, env=None):
    _check_chain(event, executable, words, _env_mapping(env), 0)


def _check_chain(event, executable, words, mapping, depth):
    if depth > CHAIN_LIMIT:
        _refuse(event, "program chain too deep")
    name = _program_name(executable, words)
    if not (name in PROGRAMS or name in SHELLS or PYTHON.match(name)):
        _refuse(event, "program off the list")
    if name == "git":
        sub, rest = _git_subcommand(words)
        if sub in REMOTE_GIT or (sub == "remote" and "update" in rest):
            _refuse(event, "remote git")
        if any(REMOTE_ARGUMENT.search(word) for word in words[1:]):
            _refuse(event, "git with a remote address")
        if any(flag == "-c" and re.match(r"alias\.[^=]*=!", value, re.I) for flag, value in zip(words, words[1:])):
            _refuse(event, "git alias that starts a program")
    elif name == "env":
        command, mapping = _unwrap_env(words, mapping)
        if command:
            _check_chain(event, None, command, mapping, depth + 1)
    elif name == "xargs":
        command = _unwrap_xargs(words)
        if command:
            _check_chain(event, None, command, mapping, depth + 1)
    elif name == "find":
        for command in _find_commands(words):
            _check_chain(event, None, command, mapping, depth + 1)
    elif name in ("node", "nodejs"):
        _check_node(event, words)
    elif PYTHON.match(name):
        for flag, module in zip(words, words[1:]):
            if flag == "-m" and module.lower() in PYTHON_MODULES_REFUSED:
                _refuse(event, "package install")
        if _python_started_isolated(words):
            _refuse(event, "python child that would not load the guard")
        if not _keeps_guard(mapping):
            _refuse(event, "python child without the guard directory")


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
    held = "/" + normal + "/"
    if any(marker in held for marker in CREDENTIAL_DIRS) or any(("/" + normal).endswith(name)
                                                                for name in CREDENTIAL_FILES):
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
    _check_program(event, args[0], _words(args[1]), args[3])


def _system(event, args):
    _refuse(event, "shell command")


def _exec(event, args):
    _check_program(event, args[0], _words(args[1]), args[2])


def _spawn(event, args):
    _check_program(event, args[1], _words(args[2]), args[3])


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
