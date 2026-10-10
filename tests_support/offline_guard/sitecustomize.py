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
its environment must keep this directory on PYTHONPATH. Five more listed programs have an option or a script
language that starts a program, so those are read too: awk (system(), a pipe to or from a command, @load and -l),
sed (the e command and the s flag e, read the way sed reads a script, inline or in a -f file), tar (-I, -F,
`--use-compress-program`, `--to-command`, `--checkpoint-action`, the volume script options, and an archive named
host:file, which tar sends through a remote shell), sort (`--compress-program`) and zip (-TT and `--unzip-command`).
Long options are matched by their shortest abbreviation getopt accepts.
A script file given to awk or sed that cannot be read (standard input, a missing file, a directory, one past the
size limit) is refused. awk is read after backslash-newline continuations are joined: system(, @ and the network
files in the raw text, pipes and getline with the strings emptied both in the joined program and in the program as
written, since gawk does not continue a comment that ends in a backslash and a join across one can hide a pipe.
awk is also refused when AWKPATH is in its environment, because -f then finds the program in a directory the guard
did not read. tar is refused when TAPE, RSH or TAR_OPTIONS is in its environment, and zip when ZIPOPT or ZIP is.
pwsh is allowed for the release paste tests: -NoProfile, then -File with a .ps1 under the repository or the
temporary directory, or -Command with the paste test's own frame over a .ps1 under the repository. The -File form
admits any temporary script, the same accepted blind spot as bash -c. An earlier version refused pwsh outright, so
this is a deliberate widening that is already done and not a regression to undo.

It is the portable counterpart of the Windows guard used for local runs. It needs no ctypes, so the same file runs
on Linux and Windows. It loads the platform module on Windows only, once, before the hook goes in, so that the
standard library's own version probe is not read as a program a test started (see warm_platform_cache).

What it cannot see: a program that an allowed shell starts (bash -c and sh -c), because the audit hook belongs to
this interpreter only; whatever a node script does through a module it imports from another file, since node code
is only searched for the names of the modules and calls that reach the network, in the inline code or the one
script file it is given; an awk program that pulls in another file with @include; and sockets opened by a
C extension or by gRPC, which go straight to the operating system and never raise an audit event. The CI workflow
therefore also holds no credentials and no secrets, so a program that did slip through would have nothing to sign
in with.

Set CORE_OFFLINE_GUARD_LOG to a file name to get one JSON line per refusal (event, a category, the running test).
The line never holds a host name, a path or a command, only the category. The one addition is a program off the
list, which is named by its bare program name (lower case, no directory, no .exe, no arguments, other characters
shown as ?, cut at 40), so that a refusal in the log can be traced to the program.
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
    "ldconfig", "getconf", "pwsh",
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


def _abbreviates(word, name, floor=4):
    """True when word is the long option `name`, or an abbreviation of it that getopt would accept (at least `floor`
    characters, so `--co` reaches `--compress-program`), with or without an attached =value."""
    flag = word.partition("=")[0]
    return flag.startswith("--") and len(flag) >= floor and name.startswith(flag)


def _short_bundle(word, with_value, stop=""):
    """The first letter of a short option word that takes a value, with the rest of the word as its attached value
    (None when the value is the next word), and the letters before it. A letter in `stop` ends the scan: what
    follows it is its own argument, not more options."""
    for position, letter in enumerate(word[1:], 1):
        if letter in stop:
            return None, None, word[1:position]
        if letter in with_value:
            attached = word[position + 1:]
            return letter, (attached or None), word[1:position]
    return None, None, word[1:]


SED_VALUE_OPTIONS = set("efl")
SED_LONG_VALUE = {"--expression", "--file", "--line-length"}


def _sed_scripts(words):
    """The sed scripts a command line gives, each as (text, is_file_name). The first word that is not an option is
    the script when no -e or -f was given. Also whether `--sandbox` was an option, which makes GNU sed refuse e itself."""
    scripts, positional, index, given, sandbox = [], [], 1, False, False
    while index < len(words):
        word = words[index]
        index += 1
        if word == "--":
            positional.extend(words[index:])
            break
        if word.startswith("--"):
            flag, equals, value = word.partition("=")
            if _abbreviates(flag, "--sandbox", 4):
                sandbox = True
            for name, is_file in (("--expression", False), ("--file", True)):
                if _abbreviates(flag, name, 4):
                    if not equals and index < len(words):
                        value, index = words[index], index + 1
                    scripts.append((value, is_file))
                    given = True
                    break
            else:
                if any(_abbreviates(flag, name, 4) for name in SED_LONG_VALUE) and not equals:
                    index += 1
            continue
        if word.startswith("-") and len(word) > 1:
            letter, value, _ = _short_bundle(word, SED_VALUE_OPTIONS, stop="i")
            if letter in ("e", "f"):
                if value is None and index < len(words):
                    value, index = words[index], index + 1
                scripts.append((value or "", letter == "f"))
                given = True
            elif letter == "l" and value is None:
                index += 1
            continue
        positional.append(word)
    if not given and positional:
        scripts.append((positional[0], False))
    return scripts, sandbox


def _sed_runs_a_command(script):
    """True when a sed script has the GNU e command, or an s command with the e flag. The script is walked the way
    sed reads it, so an e inside a regex, a replacement, a label, a file name or the text of a, i and c is not one."""
    text, size, at = script, len(script), 0

    def delimited(start, delimiter):
        """The index after the closing delimiter of a regex or replacement that starts at `start`."""
        index = start
        while index < size:
            if text[index] == "\\":
                index += 2
                continue
            if text[index] == delimiter:
                return index + 1
            index += 1
        return None

    def line_end(start):
        index = text.find("\n", start)
        return size if index < 0 else index + 1

    while at < size:
        while at < size and text[at] in " \t\n;{":
            at += 1
        if at >= size:
            return False
        # an address: a number, $, first~step, /regex/ or \cregexc, then flags, a comma and a second address
        for _ in range(2):
            if at >= size:
                break
            if text[at] in "0123456789$":
                at += 1
                while at < size and text[at] in "0123456789~+$":
                    at += 1
            elif text[at] in "/\\":
                delimiter = text[at]
                if delimiter == "\\":
                    at += 1
                    if at >= size:
                        return True
                    delimiter = text[at]
                at = delimited(at + 1, delimiter)
                if at is None:
                    return True
                while at < size and text[at] in "IM":
                    at += 1
            else:
                break
            while at < size and text[at] in " \t":
                at += 1
            if at < size and text[at] == ",":
                at += 1
                while at < size and text[at] in " \t":
                    at += 1
                if at < size and text[at] in "+~":
                    at += 1
                continue
            break
        while at < size and text[at] in " \t!":
            at += 1
        if at >= size:
            return False
        command = text[at]
        at += 1
        if command == "e":
            return True
        if command == "s":
            if at >= size:
                return True
            delimiter = text[at]
            at = delimited(at + 1, delimiter)
            if at is not None:
                at = delimited(at, delimiter)
            if at is None:
                return True  # a script the walk cannot finish is not trusted
            while at < size and text[at] in "gpiImMe0123456789":
                if text[at] == "e":
                    return True
                at += 1
            if at < size and text[at] == "w":
                at = line_end(at)
        elif command == "y":
            if at >= size:
                return True
            delimiter = text[at]
            at = delimited(at + 1, delimiter)
            if at is not None:
                at = delimited(at, delimiter)
            if at is None:
                return True
        elif command in "aic":
            index = at
            while index < size:
                if text[index] == "\\" and index + 1 < size:
                    index += 2
                    continue
                if text[index] == "\n":
                    break
                index += 1
            at = min(size, index + 1)
        elif command in "rRwW":
            at = line_end(at)
        elif command in "btTv:":
            stops = ";\n} \t" if command == ":" else ";\n}"
            while at < size and text[at] not in stops:
                at += 1
        elif command in "lLqQ":
            while at < size and text[at] in " \t0123456789":
                at += 1
        elif command in "{}pPdDnNgGhHxz=F#":
            if command == "#":
                at = line_end(at)
        else:
            return True  # a command letter sed does not have: refuse rather than guess
    return False


def _check_sed(event, words):
    scripts, sandbox = _sed_scripts(words)
    if sandbox:
        return
    for text, is_file in scripts:
        if is_file:
            text = _script_text(event, text)
        if _sed_runs_a_command(text):
            _refuse(event, "sed script that runs a command")


AWK_VALUE_OPTIONS = set("FvfeliE")
# Two readings of the program, after backslash-newline continuations are joined into a space. system(, @ and the
# network files are searched in the raw text, strings and regex literals included: a quote inside a regex literal can
# pair with a later quote and hide code from a reader that strips strings, so a string that holds one is refused too.
# A pipe is searched with the strings emptied, so a ; or } inside a string cannot end the print pattern early. A
# program with a quote inside a /regex/ span cannot be stripped with confidence, so any pipe in it is refused.
AWK_RAW_RUNS = re.compile(r"\bsystem\s*\(|@\w|/inet\d?/|/dev/(?:tcp|udp)/")
AWK_PIPE_RUNS = re.compile(r"\|&|(?<!\|)\|(?!\|)\s*getline\b|\bprintf?\b[^;}\n]*(?<!\|)\|(?![|&])")
AWK_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"')
AWK_LONE_PIPE = re.compile(r"(?<!\|)\|(?!\|)")
AWK_QUOTE_IN_REGEX = re.compile(r'/[^/\n]*"[^/\n]*/')
AWK_CONTINUATION = re.compile(r"\\\r?\n")


def _awk_runs_a_program(program):
    unjoined, program = program, AWK_CONTINUATION.sub(" ", program)
    if AWK_RAW_RUNS.search(program):
        return True
    # gawk does not continue a comment that ends in a backslash, so the line after it runs as code. Joining that line
    # onto the comment lets a quote in the comment pair with a later quote and hide a pipe, so the strings are also
    # emptied in the program as written, and a pipe in either reading is refused.
    if AWK_PIPE_RUNS.search(AWK_STRING.sub('""', program)) or AWK_PIPE_RUNS.search(AWK_STRING.sub('""', unjoined)):
        return True
    return bool(AWK_QUOTE_IN_REGEX.search(program)) and bool(AWK_LONE_PIPE.search(program))


def _check_awk(event, words, mapping):
    if mapping is None or "AWKPATH" in mapping:
        _refuse(event, "awk with AWKPATH in its environment")
    programs, positional, index, given = [], [], 1, False
    while index < len(words):
        word = words[index]
        index += 1
        if word == "--":
            positional.extend(words[index:])
            break
        if word.startswith("--"):
            flag, equals, value = word.partition("=")
            if _abbreviates(flag, "--load", 4):
                _refuse(event, "awk loading an extension")
            for name, is_file in (("--file", True), ("--source", False), ("--include", True), ("--exec", True)):
                if _abbreviates(flag, name, 5):
                    if not equals and index < len(words):
                        value, index = words[index], index + 1
                    programs.append((value, is_file))
                    given = given or name in ("--file", "--source", "--exec")
                    break
            else:
                if any(_abbreviates(flag, name, 5) for name in ("--assign", "--field-separator")) and not equals:
                    index += 1
            continue
        if word.startswith("-") and len(word) > 1:
            letter, value, _ = _short_bundle(word, AWK_VALUE_OPTIONS)
            if letter == "l":
                _refuse(event, "awk loading an extension")
            if letter in ("f", "e", "i", "E"):
                if value is None and index < len(words):
                    value, index = words[index], index + 1
                programs.append((value or "", letter != "e"))
                given = given or letter != "i"
            elif letter in ("F", "v") and value is None:
                index += 1
            continue
        positional.append(word)
    if not given and positional:
        programs.append((positional[0], False))
    for text, is_file in programs:
        if is_file:
            text = _script_text(event, text)
        if _awk_runs_a_program(text):
            _refuse(event, "awk program that starts a program")


TAR_VALUE_LETTERS = set("bCfFgHIKLNTVX")
TAR_RUNS_A_PROGRAM = ("--to-command", "--use-compress-program", "--checkpoint-action", "--info-script",
                      "--new-volume-script", "--rsh-command", "--rmt-command")
TAR_NOT_AN_ABBREVIATION = {"--checkpoint"}
# TAPE and RSH name the archive and its shell, and TAR_OPTIONS is put in front of the command line, so it can carry
# `--to-command` and the rest without any of them appearing in the words the guard reads.
TAR_ENVIRONMENT = ("TAPE", "RSH", "TAR_OPTIONS")


def _tar_archive_is_remote(value):
    return bool(re.match(r"^[^/\\]*:", value)) and not re.match(r"^[A-Za-z]:", value)


def _check_tar(event, words, mapping):
    if mapping is None or any(name in mapping for name in TAR_ENVIRONMENT):
        _refuse(event, "tar with TAPE, RSH or TAR_OPTIONS in its environment")
    rest, archives, local_only = list(words[1:]), [], False
    if rest and not rest[0].startswith("-"):
        bundle, rest = rest[0], rest[1:]
        for letter in bundle:
            if letter in TAR_VALUE_LETTERS and rest:
                value, rest = rest[0], rest[1:]
                if letter in "IF":
                    _refuse(event, "tar starting a program")
                if letter == "f":
                    archives.append(value)
            elif letter in "IF":
                _refuse(event, "tar starting a program")
    index = 0
    while index < len(rest):
        word = rest[index]
        index += 1
        if word == "--":
            break
        if word.startswith("--"):
            flag, equals, value = word.partition("=")
            if flag == "--force-local":
                local_only = True
            if any(_abbreviates(flag, name) and flag not in TAR_NOT_AN_ABBREVIATION for name in TAR_RUNS_A_PROGRAM):
                _refuse(event, "tar starting a program")
            if len(flag) >= 5 and "--file".startswith(flag):
                if not equals and index < len(rest):
                    value, index = rest[index], index + 1
                archives.append(value)
            continue
        if word.startswith("-") and len(word) > 1:
            letter, value, _ = _short_bundle(word, TAR_VALUE_LETTERS)
            if letter in ("I", "F"):
                _refuse(event, "tar starting a program")
            if letter == "f":
                if value is None and index < len(rest):
                    value, index = rest[index], index + 1
                archives.append(value or "")
            elif letter is not None and value is None:
                index += 1
    if not local_only and any(_tar_archive_is_remote(value) for value in archives):
        _refuse(event, "tar archive on another machine")


def _check_sort(event, words):
    for word in words[1:]:
        if word == "--":
            break
        if _abbreviates(word, "--compress-program"):
            _refuse(event, "sort starting a compress program")


def _check_zip(event, words, mapping):
    if mapping is None or "ZIPOPT" in mapping or "ZIP" in mapping:
        _refuse(event, "zip with ZIPOPT or ZIP in its environment")
    for word in words[1:]:
        if word == "--":
            break
        if word.startswith("--"):
            if _abbreviates(word, "--unzip-command"):
                _refuse(event, "zip starting an unzip command")
        elif word.startswith("-") and "TT" in word:
            _refuse(event, "zip starting an unzip command")


def _node_reaches_out(code):
    return bool(NODE_REACHES_OUT.search(code))


REPO_ROOT = os.path.dirname(os.path.dirname(GUARD_DIR))
PWSH_FLAGS = {"-noprofile", "-noninteractive"}
PWSH_PASTE = re.compile(
    r"\$ErrorActionPreference = 'Stop'; \. '([^'\r\n]+\.ps1)'((?: -[A-Za-z]+ [A-Za-z0-9_.-]+)*) -DefinitionsOnly; "
    r"try \{ ([A-Za-z]+-[A-Za-z]+(?: '[A-Za-z0-9 ]*')?); 'RESULT:ok' \} "
    r"catch \{ 'RESULT:' \+ \$_\.Exception\.Message \}")


def _inside(path, roots):
    try:
        path = os.path.normcase(os.path.abspath(os.fsdecode(path)))
        for root in roots:
            if os.path.commonpath([path, root]) == root:
                return True
    except (TypeError, ValueError):
        pass
    return False


def _script_in(path, roots):
    return path.lower().endswith(".ps1") and os.path.isabs(path) and _inside(path, roots)


def _check_pwsh(event, words):
    """pwsh is for the release paste tests, but the guard cannot hold it to them: -NoProfile (and optionally
    -NonInteractive), then either -File with a .ps1 under the repository or the temporary directory and `-Name value`
    script arguments that are absolute paths there too, or -Command with exactly the paste test's own frame, which
    dot-sources a .ps1 under the repository with -DefinitionsOnly and calls one function by name. -File admits any
    temporary script whatever it holds, which is the same accepted blind spot as bash -c. Only -Command is pinned
    to the paste frame."""
    repo, temp = [os.path.normcase(REPO_ROOT)], [os.path.normcase(REPO_ROOT), *_temp_roots()]
    flags, index = set(), 1
    while index < len(words) and words[index].lower() in PWSH_FLAGS:
        flags.add(words[index].lower())
        index += 1
    if "-noprofile" not in flags or index >= len(words):
        _refuse(event, "pwsh other than the release paste tests")
    mode, rest = words[index].lower(), words[index + 1:]
    if mode == "-file" and rest and _script_in(rest[0], temp):
        arguments = rest[1:]
        if len(arguments) % 2 == 0 and all(
                name.startswith("-") and name[1:].isalpha() and os.path.isabs(value) and _inside(value, temp)
                for name, value in zip(arguments[::2], arguments[1::2])):
            return
    elif mode == "-command" and len(rest) == 1:
        found = PWSH_PASTE.fullmatch(rest[0])
        if found and _script_in(found.group(1), repo):
            return
    _refuse(event, "pwsh other than the release paste tests")


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
            code = _read_script_file(word)
        if _node_reaches_out(code):
            _refuse(event, "node code that starts a program or reaches the network")
        return


def _read_script_file(name):
    try:
        if os.path.isfile(name) and os.path.getsize(name) <= NODE_FILE_LIMIT:
            with open(name, encoding="utf-8", errors="replace") as stream:
                return stream.read()
    except OSError:
        pass
    return ""


SCRIPT_STREAM = re.compile(r"^(?:-|/dev/(?:stdin|fd/.*)|/proc/[^/]+/fd/.*)$")


def _script_text(event, name):
    """The text of an awk or sed script file, or a refusal: a script read from standard input, a missing or
    unreadable file, a directory and a file past the size limit cannot be searched, so they are not trusted."""
    if not SCRIPT_STREAM.match(str(name).replace("\\", "/")):
        try:
            if os.path.isfile(name) and os.path.getsize(name) <= NODE_FILE_LIMIT:
                with open(name, encoding="utf-8", errors="replace") as stream:
                    return stream.read()
        except OSError:
            pass
    _refuse(event, "script file the guard could not read")


def _check_program(event, executable, words, env=None):
    _check_chain(event, executable, words, _env_mapping(env), 0)


def _check_chain(event, executable, words, mapping, depth):
    if depth > CHAIN_LIMIT:
        _refuse(event, "program chain too deep")
    name = _program_name(executable, words)
    if not (name in PROGRAMS or name in SHELLS or PYTHON.match(name)):
        _refuse(event, "program off the list: " + re.sub(r"[^a-z0-9._+-]", "?", name)[:40])
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
    elif name == "pwsh":
        _check_pwsh(event, words)
    elif name == "awk":
        _check_awk(event, words, mapping)
    elif name == "sed":
        _check_sed(event, words)
    elif name == "tar":
        _check_tar(event, words, mapping)
    elif name == "sort":
        _check_sort(event, words)
    elif name == "zip":
        _check_zip(event, words, mapping)
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


def warm_platform_cache():
    """Ask the platform module for the operating system once, before the hook is in, on Windows only.

    platform.uname() reads the system through a WMI query and, when the query raises OSError, falls back to three
    cmd.exe probes (ver, command /c ver, cmd /c ver). The query times out when several runs share the machine, and
    numpy.testing calls platform.machine() when it is imported, which scipy does. Without this, the probes run
    under the hook, are refused, and put three program refusals into the log at collection although no test
    started anything. platform keeps the answer, so the later call is a lookup and starts nothing. Only the
    standard library's own version probe runs here; nothing a test starts is let through, and the refusal of the
    three probes as a test's own command is unchanged. On Linux and macOS platform.uname() starts no program."""
    if sys.platform != "win32":
        return
    try:
        import platform
        platform.uname()
    except Exception:
        pass


def install():
    global INSTALLED
    if not INSTALLED:
        warm_platform_cache()
        sys.addaudithook(check)
        INSTALLED = True


if __name__ == "sitecustomize":
    install()
