"""The CI offline guard (tests_support/offline_guard/sitecustomize.py).

The guard is a Python audit hook that Python loads at start-up when its directory is on PYTHONPATH. It refuses
sockets and name lookups that leave the machine, subprocesses that are not on a short list, remote git, and
reads of credential files. The decision tests below call guard.check with synthetic audit events, so nothing here
touches a network or starts a process. The wiring tests start a child interpreter with the guard on PYTHONPATH
and ask it to do something the guard must refuse that cannot leave the machine even if the guard were absent.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
GUARD_DIR = ROOT / "tests_support" / "offline_guard"
GUARD_FILE = GUARD_DIR / "sitecustomize.py"


def load_guard():
    """The guard module under a name that is not sitecustomize, so importing it installs no hook here."""
    spec = importlib.util.spec_from_file_location("offline_guard_under_test", GUARD_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard():
    return load_guard()


@pytest.fixture(autouse=True)
def no_refusal_log(monkeypatch):
    """The decision tests refuse things on purpose. In CI the log variable is set for the whole job, and every one
    of those synthetic refusals would land in it, burying a real one. The tests that look at the log set their own."""
    monkeypatch.delenv("CORE_OFFLINE_GUARD_LOG", raising=False)
    monkeypatch.setenv("PYTHONPATH", str(GUARD_DIR))


def refused(guard, event, args):
    with pytest.raises((OSError, PermissionError)) as caught:
        guard.check(event, args)
    assert "offline guard" in str(caught.value)


def allowed(guard, event, args):
    assert guard.check(event, args) is None


def test_importing_the_guard_under_another_name_installs_no_hook(guard):
    assert guard.GUARD_ID == "f42-offline-guard-v1"
    assert guard.INSTALLED is False


@pytest.mark.parametrize("host", ["example.com", b"example.com", "8.8.8.8", "10.0.0.5", "169.254.169.254",
                                  "metadata.google.internal", "::ffff:8.8.8.8", "2001:4860:4860::8888"])
def test_name_lookups_that_leave_the_machine_are_refused(guard, host):
    refused(guard, "socket.getaddrinfo", (host, 443, 0, 0, 0, 0))


@pytest.mark.parametrize("host", ["localhost", b"localhost", "127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1",
                                  None, ""])
def test_local_name_lookups_are_allowed(guard, host):
    allowed(guard, "socket.getaddrinfo", (host, 8000, 0, 0, 0, 0))


@pytest.mark.parametrize("event", ["socket.gethostbyname", "socket.gethostbyaddr"])
def test_the_other_lookup_events_are_refused_for_remote_hosts(guard, event):
    refused(guard, event, ("example.com",))
    allowed(guard, event, ("localhost",))


@pytest.mark.parametrize("address", [("8.8.8.8", 443), ("169.254.169.254", 80), ("10.1.2.3", 5432),
                                     ("::ffff:8.8.8.8", 443, 0, 0), ("2001:4860:4860::8888", 443, 0, 0),
                                     ("metadata.google.internal", 80), ("example.com", 443)])
@pytest.mark.parametrize("event", ["socket.connect", "socket.sendto", "socket.sendmsg"])
def test_sockets_to_other_machines_are_refused(guard, event, address):
    refused(guard, event, (object(), address))


@pytest.mark.parametrize("address", [("127.0.0.1", 80), ("127.0.0.9", 80), ("::1", 80, 0, 0), ("localhost", 80),
                                     ("::ffff:127.0.0.1", 80, 0, 0), ("0.0.0.0", 80)])
def test_loopback_sockets_are_allowed(guard, address):
    allowed(guard, "socket.connect", (object(), address))


def test_a_socket_address_of_an_unknown_shape_is_refused(guard):
    refused(guard, "socket.connect", (object(), 12345))
    refused(guard, "socket.connect", (object(), None))


def test_unix_sockets_are_allowed_only_under_the_temporary_directory(guard):
    inside = os.path.join(tempfile.gettempdir(), "pytest-of-ci", "worker.sock")
    allowed(guard, "socket.connect", (object(), inside))
    refused(guard, "socket.connect", (object(), "/var/run/docker.sock"))
    refused(guard, "socket.connect", (object(), b"/var/run/docker.sock"))


@pytest.mark.parametrize("command", [["curl", "https://example.com"], ["wget", "x"], ["gcloud", "auth", "list"],
                                     ["gsutil", "ls"], ["bq", "query"], ["ssh", "host"], ["scp", "a", "b"],
                                     ["nc", "host", "80"], ["docker", "run", "x"], ["gh", "pr", "list"],
                                     ["pip", "install", "x"], ["pip3", "download", "x"], ["terraform", "apply"],
                                     ["kubectl", "get", "pods"], ["/usr/bin/curl", "-s", "x"],
                                     ["C:\\Windows\\System32\\curl.exe", "x"], ["unknown-tool"]])
def test_executables_off_the_list_are_refused(guard, command):
    refused(guard, "subprocess.Popen", (command[0], command, None, None))


@pytest.mark.parametrize("command", [[sys.executable, "-c", "pass"], ["python3", "-m", "pytest"],
                                     ["python3.13", "x.py"], ["python.exe", "x.py"], ["py", "-3.13", "x.py"],
                                     ["node", "--version"], ["git", "status"], ["git", "rev-parse", "HEAD"],
                                     ["git", "-c", "user.name=T", "commit", "-m", "x"],
                                     ["git", "-C", "somewhere", "show", "origin/full-42:core/x.py"],
                                     ["bash", "--noprofile", "--norc", "-c", "echo hi"], ["/bin/sh", "-c", "true"]])
def test_executables_on_the_list_are_allowed(guard, command):
    allowed(guard, "subprocess.Popen", (command[0], command, None, None))


@pytest.mark.parametrize("command", [["git", "push"], ["git", "pull"], ["git", "fetch", "origin"],
                                     ["git", "clone", "https://example.com/r.git"], ["git", "ls-remote", "origin"],
                                     ["git", "remote", "update"], ["git", "submodule", "update", "--init"],
                                     ["git", "-c", "x=y", "-C", "d", "fetch"], ["git", "--git-dir", "d", "push"],
                                     ["git", "show", "https://example.com/r.git"], ["git", "diff", "git@host:r.git"],
                                     ["git.exe", "push"]])
def test_remote_git_is_refused(guard, command):
    refused(guard, "subprocess.Popen", (command[0], command, None, None))


@pytest.mark.parametrize("command", [[sys.executable, "-m", "pip", "install", "x"],
                                     [sys.executable, "-m", "pip", "download", "x"],
                                     [sys.executable, "-m", "ensurepip"], ["python3", "-m", "pip", "wheel", "x"]])
def test_package_installs_through_python_are_refused(guard, command):
    refused(guard, "subprocess.Popen", (command[0], command, None, None))


def test_a_windows_command_line_string_is_read_the_same_way(guard):
    refused(guard, "subprocess.Popen", ("curl.exe", 'curl.exe "https://example.com"', None, None))
    allowed(guard, "subprocess.Popen", (sys.executable, f'"{sys.executable}" -c "pass"', None, None))


def test_os_system_is_always_refused(guard):
    refused(guard, "os.system", ("echo hi",))


@pytest.mark.parametrize("event,args", [("os.exec", ("/usr/bin/curl", ["curl", "x"], None)),
                                        ("os.posix_spawn", ("/usr/bin/gcloud", ["gcloud"], None)),
                                        ("os.spawn", (0, "/usr/bin/ssh", ["ssh"], None))])
def test_the_other_ways_to_start_a_program_follow_the_same_list(guard, event, args):
    refused(guard, event, args)


def test_python_may_be_started_by_exec_and_spawn(guard):
    allowed(guard, "os.exec", (sys.executable, [sys.executable, "-c", "pass"], None))
    allowed(guard, "os.posix_spawn", (sys.executable, [sys.executable], None))


@pytest.mark.parametrize("path", [
    "/home/runner/.config/gcloud/application_default_credentials.json",
    "C:\\Users\\someone\\AppData\\Roaming\\gcloud\\credentials.db",
    "/home/runner/.ssh/id_rsa", "/root/.aws/credentials", "/home/runner/.docker/config.json",
    "/home/runner/.netrc", "/home/runner/.git-credentials", "/home/runner/.config/gh/hosts.yml",
    "/home/runner/.kube/config", "/home/runner/.npmrc", "/home/runner/.pypirc",
    "/run/secrets/token", "/var/run/secrets/kubernetes.io/serviceaccount/token",
    "/tmp/work/../../home/runner/.ssh/id_rsa", "C:/Users/Someone/.SSH/ID_RSA",
    "/some/dir/application_default_credentials.json"])
def test_credential_files_cannot_be_opened(guard, path):
    with pytest.raises(PermissionError) as caught:
        guard.check("open", (path, "r", 0))
    assert "offline guard" in str(caught.value)


def test_the_file_named_by_google_application_credentials_cannot_be_opened(guard, monkeypatch, tmp_path):
    key = tmp_path / "key.json"
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(key))
    with pytest.raises(PermissionError):
        guard.check("open", (str(key), "r", 0))
    with pytest.raises(PermissionError):
        guard.check("open", (os.fsencode(str(key)), "rb", 0))


def test_listing_a_credential_directory_is_refused(guard):
    for event in ("os.listdir", "os.scandir"):
        with pytest.raises(PermissionError):
            guard.check(event, ("/home/runner/.config/gcloud",))


@pytest.mark.parametrize("path", [str(ROOT / "core" / "api" / "contract.md"), "/tmp/pytest-of-ci/a.json",
                                  "/usr/lib/python3.13/json/__init__.py", "C:\\work\\repo\\core\\x.py", 3, None])
def test_ordinary_files_and_file_descriptors_are_not_blocked(guard, path):
    allowed(guard, "open", (path, "r", 0))


def test_other_events_pass_untouched(guard):
    allowed(guard, "import", ("json", None, None, None, None))
    allowed(guard, "exec", (object(),))


def test_a_refusal_is_written_to_the_log_when_one_is_named(guard, monkeypatch, tmp_path):
    log = tmp_path / "guard.jsonl"
    monkeypatch.setenv("CORE_OFFLINE_GUARD_LOG", str(log))
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_x.py::test_y (call)")
    refused(guard, "socket.getaddrinfo", ("example.com", 443, 0, 0, 0, 0))
    refused(guard, "subprocess.Popen", ("curl", ["curl"], None, None))
    lines = [__import__("json").loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [line["event"] for line in lines] == ["socket.getaddrinfo", "subprocess.Popen"]
    assert all(line["test"] == "test_x.py::test_y (call)" for line in lines)
    assert "example.com" not in log.read_text(encoding="utf-8")


def child(code, *, guarded=True, extra_env=None):
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "GOOGLE_APPLICATION_CREDENTIALS")}
    if guarded:
        env["PYTHONPATH"] = str(GUARD_DIR)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                          encoding="utf-8", timeout=60)


def test_python_loads_the_guard_when_its_directory_is_on_pythonpath():
    done = child("import sitecustomize, sys; print(sitecustomize.GUARD_ID, sitecustomize.INSTALLED)")
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["f42-offline-guard-v1", "True"]


def test_a_child_cannot_look_up_an_address_that_leaves_the_machine():
    # A numeric host needs no DNS query, so even with no guard this sends nothing.
    code = "import socket\ntry:\n socket.getaddrinfo('198.51.100.7', 9)\nexcept OSError as e:\n print('REFUSED', e)\n"
    done = child(code)
    assert "REFUSED offline guard" in done.stdout, (done.stdout, done.stderr)


def test_a_child_cannot_start_a_program_that_is_off_the_list():
    # `curl --version` prints a version and opens no connection, so even with no guard this sends nothing.
    code = "import subprocess\ntry:\n subprocess.run(['curl', '--version'])\nexcept OSError as e:\n print('REFUSED', e)\n"
    done = child(code)
    assert "REFUSED offline guard" in done.stdout, (done.stdout, done.stderr)


def test_a_child_cannot_read_a_credential_file(tmp_path):
    # The file does not exist. The guard answers before the file system does, so a PermissionError (and not a
    # FileNotFoundError) shows the guard refused the name.
    key = tmp_path / "application_default_credentials.json"
    code = "\n".join((
        "try:",
        f"    open({str(key)!r}).read()",
        "except PermissionError as e:",
        "    print('REFUSED', e)",
        "except FileNotFoundError:",
        "    print('NOT FOUND')",
    ))
    done = child(code)
    assert "REFUSED offline guard" in done.stdout, (done.stdout, done.stderr)


def test_a_python_child_started_without_the_guard_directory_is_refused_by_a_guarded_parent():
    """A child that does not load the guard would run whatever it is given unguarded, so the guard refuses to
    start it. The grandchild asks for a clean environment, which drops PYTHONPATH."""
    code = "\n".join((
        "import subprocess, sys",
        "try:",
        "    subprocess.run([sys.executable, '-c', 'pass'], env={'PATH': ''})",
        "except OSError as e:",
        "    print('REFUSED', e)",
    ))
    done = child(code)
    assert "REFUSED offline guard" in done.stdout, (done.stdout, done.stderr)


def test_the_guard_is_active_in_this_process_when_the_run_requires_it():
    """The workflow sets F42_REQUIRE_OFFLINE_GUARD=1, so a run whose PYTHONPATH lost the guard fails here instead
    of running unguarded. Without the variable there is no requirement."""
    if os.environ.get("F42_REQUIRE_OFFLINE_GUARD") != "1":
        assert os.environ.get("F42_REQUIRE_OFFLINE_GUARD") in (None, "", "0")
        return
    guard = sys.modules.get("sitecustomize")
    assert guard is not None and getattr(guard, "GUARD_ID", None) == "f42-offline-guard-v1", (
        "the offline guard did not load; PYTHONPATH must name tests_support/offline_guard")
    assert guard.INSTALLED is True
    with pytest.raises(OSError, match="offline guard"):
        __import__("socket").getaddrinfo("198.51.100.7", 9)


PYTHON_EXE = sys.executable
KEEPS_GUARD = {"PATH": "/usr/bin", "PYTHONPATH": os.pathsep.join(["/somewhere/else", str(GUARD_DIR)])}


@pytest.mark.parametrize("command", [
    ["env", "curl", "https://example.com"], ["env", "-i", "curl", "x"], ["env", "FOO=bar", "wget", "x"],
    ["env", "-u", "HOME", "curl", "x"], ["env", "--unset=HOME", "curl", "x"], ["env", "-S", "curl x"],
    ["env", "-C", "/tmp", "curl", "x"], ["env", "--", "curl", "x"], ["/usr/bin/env", "gcloud", "auth", "list"],
    ["env", "env", "curl", "x"], ["env", "xargs", "curl"], ["env", "git", "push"],
    ["env", PYTHON_EXE, "-m", "pip", "install", "x"],
    ["xargs", "curl"], ["xargs", "-n", "1", "curl"], ["xargs", "-I", "{}", "curl", "{}"], ["xargs", "-n1", "wget"],
    ["xargs", "-0", "-P", "4", "-a", "list", "ssh"], ["xargs", "--max-args=1", "curl"], ["xargs", "env", "curl"],
    ["find", ".", "-exec", "curl", "{}", ";"], ["find", ".", "-execdir", "wget", "{}", "+"],
    ["find", ".", "-name", "x", "-ok", "ssh", "x", ";"], ["find", ".", "-okdir", "gcloud", "{}", ";"],
    ["find", ".", "-exec", "env", "curl", "{}", ";"]])
def test_an_allowed_program_cannot_start_a_program_that_is_off_the_list(guard, command):
    refused(guard, "subprocess.Popen", (command[0], command, None, KEEPS_GUARD))


@pytest.mark.parametrize("command", [
    ["env"], ["env", "FOO=1", "true"], ["env", "-i", "echo", "hi"], ["xargs"], ["xargs", "echo"],
    ["xargs", "-n", "1", "echo"], ["find", "."], ["find", ".", "-name", "x"], ["find", ".", "-exec", "echo", "{}", ";"],
    ["find", ".", "-exec", "git", "status", ";"], ["env", "git", "status"]])
def test_those_programs_stay_usable_for_local_work(guard, command):
    allowed(guard, "subprocess.Popen", (command[0], command, None, KEEPS_GUARD))


@pytest.mark.parametrize("code", [
    "require('child_process').execSync('curl https://example.com')",
    "const {spawn} = require('node:child_process'); spawn('curl', ['x'])",
    "import('node:child_process').then(m => m.exec('wget x'))",
    "fetch('https://example.com').then(r => r.text())",
    "await fetch('http://example.com')",
    "require('https').get('https://example.com')", "import http from 'node:http'",
    "const net = require('net'); net.connect(80, 'example.com')", "require('dgram')", "require('dns').lookup('x')",
    "new WebSocket('wss://example.com')", "process.binding('tcp_wrap')", "process.dlopen(module, 'x.node')"])
@pytest.mark.parametrize("flag", ["-e", "--eval", "-p", "--print"])
def test_node_inline_code_that_starts_a_program_or_reaches_the_network_is_refused(guard, flag, code):
    command = ["node", "--input-type=module", flag, code]
    refused(guard, "subprocess.Popen", ("node", command, None, KEEPS_GUARD))


@pytest.mark.parametrize("code", [
    "console.log(1 + 1)", "import { readFileSync } from 'node:fs'; console.log(readFileSync(process.argv[1], 'utf8'))",
    "const { validateAnswer } = await import(input.module); console.log(validateAnswer)",
    "const path = require('node:path'); console.log(path.join('a', 'b'))"])
def test_node_inline_code_that_only_computes_is_allowed(guard, code):
    allowed(guard, "subprocess.Popen", ("node", ["node", "--input-type=module", "-e", code], None, KEEPS_GUARD))


def test_node_equals_form_of_eval_is_read_too(guard):
    refused(guard, "subprocess.Popen", ("node", ["node", "--eval=fetch('https://example.com')"], None, KEEPS_GUARD))


@pytest.mark.parametrize("words", [["node", "-r", "./preload.js", "x.js"], ["node", "--require=./preload.js", "x.js"],
                                   ["node", "--import", "./preload.mjs", "x.js"], ["node", "--loader=./l.mjs", "x.js"],
                                   ["node", "-"]])
def test_node_preloads_and_code_from_stdin_are_refused(guard, words):
    refused(guard, "subprocess.Popen", ("node", words, None, KEEPS_GUARD))


def test_a_node_script_file_is_read_before_it_is_allowed(guard, tmp_path):
    bad = tmp_path / "bad.mjs"
    bad.write_text("import { execSync } from 'node:child_process';\nexecSync('curl https://example.com');\n",
                   encoding="utf-8")
    good = tmp_path / "good.mjs"
    good.write_text("console.log(process.argv.length);\n", encoding="utf-8")
    refused(guard, "subprocess.Popen", ("node", ["node", str(bad)], None, KEEPS_GUARD))
    refused(guard, "subprocess.Popen", ("node", ["node", "--no-warnings", str(bad), "arg"], None, KEEPS_GUARD))
    allowed(guard, "subprocess.Popen", ("node", ["node", str(good)], None, KEEPS_GUARD))
    allowed(guard, "subprocess.Popen", ("node", ["node", str(tmp_path / "missing.mjs")], None, KEEPS_GUARD))
    allowed(guard, "subprocess.Popen", ("node", ["node", "--version"], None, KEEPS_GUARD))


def test_node_started_through_env_xargs_or_find_is_read_the_same_way(guard):
    code = "fetch('https://example.com')"
    refused(guard, "subprocess.Popen", ("env", ["env", "node", "-e", code], None, KEEPS_GUARD))
    refused(guard, "subprocess.Popen", ("find", ["find", ".", "-exec", "node", "-e", code, ";"], None, KEEPS_GUARD))


@pytest.mark.parametrize("flags", [["-I"], ["-E"], ["-S"], ["-Ic"], ["-IBc"], ["-B", "-I"], ["-u", "-S"], ["-sI"]])
def test_python_children_started_isolated_are_refused_because_they_would_not_load_the_guard(guard, flags):
    tail = ["pass"] if flags[-1].endswith("c") else ["-c", "pass"]
    refused(guard, "subprocess.Popen", (PYTHON_EXE, [PYTHON_EXE, *flags, *tail], None, KEEPS_GUARD))


@pytest.mark.parametrize("words", [["-B", "-c", "pass"], ["-W", "ignore", "-c", "pass"], ["-u", "-m", "pytest"],
                                   ["-c", "-I"], ["x.py", "-I"], ["-m", "json.tool", "-S"], ["--version"], ["-V"]])
def test_python_flags_that_leave_the_guard_loaded_are_allowed(guard, words):
    allowed(guard, "subprocess.Popen", (PYTHON_EXE, [PYTHON_EXE, *words], None, KEEPS_GUARD))


@pytest.mark.parametrize("env", [{"PATH": "/usr/bin"}, {}, {"PYTHONPATH": ""}, {"PYTHONPATH": "/somewhere/else"},
                                 {"PYTHONPATH": str(GUARD_DIR) + "-not"}, {b"PATH": b"/usr/bin"}, ["PATH=/usr/bin"]])
def test_a_python_child_whose_environment_drops_the_guard_directory_is_refused(guard, env):
    refused(guard, "subprocess.Popen", (PYTHON_EXE, [PYTHON_EXE, "-c", "pass"], None, env))
    refused(guard, "os.exec", (PYTHON_EXE, [PYTHON_EXE, "-c", "pass"], env))
    refused(guard, "os.posix_spawn", (PYTHON_EXE, [PYTHON_EXE, "-c", "pass"], env))
    refused(guard, "os.spawn", (0, PYTHON_EXE, [PYTHON_EXE, "-c", "pass"], env))


def test_a_python_child_whose_environment_keeps_the_guard_directory_is_allowed(guard):
    allowed(guard, "subprocess.Popen", (PYTHON_EXE, [PYTHON_EXE, "-c", "pass"], None, KEEPS_GUARD))
    allowed(guard, "subprocess.Popen", (PYTHON_EXE, [PYTHON_EXE, "-c", "pass"], None,
                                        {b"PYTHONPATH": str(GUARD_DIR).encode()}))
    allowed(guard, "os.exec", (PYTHON_EXE, [PYTHON_EXE, "-c", "pass"], KEEPS_GUARD))


def test_a_python_child_that_inherits_the_environment_is_judged_by_this_processs_pythonpath(guard, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(GUARD_DIR))
    allowed(guard, "subprocess.Popen", (PYTHON_EXE, [PYTHON_EXE, "-c", "pass"], None, None))
    monkeypatch.delenv("PYTHONPATH")
    refused(guard, "subprocess.Popen", (PYTHON_EXE, [PYTHON_EXE, "-c", "pass"], None, None))


def test_programs_that_are_not_python_do_not_need_the_guard_in_their_environment(guard):
    allowed(guard, "subprocess.Popen", ("git", ["git", "status"], None, {"PATH": "/usr/bin"}))
    allowed(guard, "subprocess.Popen", ("node", ["node", "--version"], None, {}))


@pytest.mark.parametrize("change", [["env", "-i"], ["env", "-u", "PYTHONPATH"], ["env", "--unset=PYTHONPATH"],
                                    ["env", "PYTHONPATH=/elsewhere"], ["env", "PYTHONPATH="]])
def test_env_cannot_be_used_to_strip_the_guard_from_a_python_child(guard, change):
    command = [*change, PYTHON_EXE, "-c", "pass"]
    refused(guard, "subprocess.Popen", (command[0], command, None, KEEPS_GUARD))


def test_env_that_keeps_the_guard_directory_may_start_python(guard):
    command = ["env", f"PYTHONPATH={GUARD_DIR}", PYTHON_EXE, "-c", "pass"]
    allowed(guard, "subprocess.Popen", (command[0], command, None, {"PATH": "/usr/bin"}))
    allowed(guard, "subprocess.Popen", ("env", ["env", "FOO=1", PYTHON_EXE, "-c", "pass"], None, KEEPS_GUARD))


def test_git_cannot_run_an_alias_that_starts_another_program(guard):
    refused(guard, "subprocess.Popen", ("git", ["git", "-c", "alias.x=!sh -c id", "x"], None, None))
    allowed(guard, "subprocess.Popen", ("git", ["git", "-c", "alias.co=checkout", "co"], None, None))


@pytest.mark.parametrize("path", [".ssh/id_rsa", "./.ssh/id_rsa", "home/.aws/credentials", ".netrc",
                                  "../.git-credentials", ".config/gcloud/credentials.db"])
def test_a_relative_path_to_a_credential_file_is_refused_too(guard, path):
    with pytest.raises(PermissionError):
        guard.check("open", (path, "r", 0))


# Programs on the list that can start another program through their own options or script language

def popen(guard, command, check):
    check(guard, "subprocess.Popen", (command[0], command, None, KEEPS_GUARD))


@pytest.mark.parametrize("command", [
    ["awk", 'BEGIN { system("curl https://example.com") }'], ["awk", 'BEGIN{system ("curl x")}'],
    ["awk", "-F,", '{ system("wget " $1) }', "list"], ["awk", "-v", "x=1", 'BEGIN { system("id") }'],
    ["awk", 'BEGIN { "curl x" | getline line }'], ["awk", 'BEGIN { cmd = "curl x"; cmd | getline }'],
    ["awk", '{ print $1 | "sh" }'], ["awk", '{ printf "%s\\n", $1 | "curl -d @- x" }'],
    ["awk", 'BEGIN { "date" |& getline }'], ["awk", '@load "filefuncs"; BEGIN { }'],
    ["awk", "--load", "filefuncs", "BEGIN { }"], ["awk", "-l", "filefuncs", "BEGIN { }"],
    ["awk", "-e", 'BEGIN { system("id") }'], ["awk", '--source=BEGIN{system("id")}'],
    ["awk", "-e", "BEGIN { print 1 }", "-e", 'BEGIN { system("id") }'],
    ["env", "awk", 'BEGIN { system("id") }'], ["xargs", "awk", 'BEGIN { system("id") }']])
def test_awk_cannot_start_a_program(guard, command):
    popen(guard, command, refused)


def test_an_awk_program_file_is_read_before_it_is_allowed(guard, tmp_path):
    bad = tmp_path / "bad.awk"
    bad.write_text('BEGIN { system("curl https://example.com") }\n', encoding="utf-8")
    good = tmp_path / "good.awk"
    good.write_text("{ print $1 }\n", encoding="utf-8")
    popen(guard, ["awk", "-f", str(bad), "data"], refused)
    popen(guard, ["awk", f"--file={bad}"], refused)
    popen(guard, ["awk", "-f", str(good), "data"], allowed)
    popen(guard, ["awk", "-f", str(tmp_path / "missing.awk")], allowed)


@pytest.mark.parametrize("command", [
    ["awk", "{ print $1 }", "file"], ["awk", "-F,", "{ print $2 }", "file"], ["awk", "-v", "x=1", "BEGIN { print x }"],
    ["awk", "/a|b/ { print }"], ["awk", "$1 == 1 || $2 == 2 { n++ } END { print n }"],
    ["awk", '{ s += length($0) } END { printf "%d\\n", s }'], ["awk", 'BEGIN { print "system" }'],
    ["awk", 'BEGIN { printf "%s|%s\\n", 1, 2 }'], ["awk", 'BEGIN { x = "system(" }'],
    ["awk", 'BEGIN { print "a|b" }'], ["awk", "--version"]])
def test_awk_stays_usable_for_local_work(guard, command):
    popen(guard, command, allowed)


@pytest.mark.parametrize("command", [
    ["sed", "e"], ["sed", "e curl x"], ["sed", "1e curl x"], ["sed", "$e id"], ["sed", "/x/e id"], ["sed", "1,3e id"],
    ["sed", "1!e id"], ["sed", "p;e id"], ["sed", "{p;e id}"], ["sed", "s/x/id/e"], ["sed", "s/x/id/ge"],
    ["sed", "s|x|id|pe"], ["sed", "s,x,id,2e"], ["sed", "s/x/y/;s/a/b/e"], ["sed", "-n", "-e", "p", "-e", "e id"],
    ["sed", "-ne", "e id"], ["sed", "--expression=e id"], ["sed", "-E", "s/(x)/id/e"], ["sed", "-i", "s/x/id/e", "f"],
    ["sed", "s/a/b/\ne id"], ["sed", "1k id"], ["sed", "-if", "e id", "file"], ["sed", ":a;e id"],
    ["sed", "/x/,+2e id"], ["sed", "2,~4e id"],
    ["env", "sed", "e id"], ["find", ".", "-exec", "sed", "s/x/id/e", "{}", ";"]])
def test_gnu_sed_cannot_run_a_command_through_e(guard, command):
    popen(guard, command, refused)


def test_a_sed_script_file_is_read_before_it_is_allowed(guard, tmp_path):
    bad = tmp_path / "bad.sed"
    bad.write_text("p\ns/x/id/e\n", encoding="utf-8")
    good = tmp_path / "good.sed"
    good.write_text("s/x/y/g\n/a/d\n", encoding="utf-8")
    popen(guard, ["sed", "-f", str(bad), "data"], refused)
    popen(guard, ["sed", f"--file={bad}", "data"], refused)
    popen(guard, ["sed", "-f", str(good), "data"], allowed)


@pytest.mark.parametrize("command", [
    ["sed", "-n", "1p"], ["sed", "s/x/y/"], ["sed", "s/x/y/g"], ["sed", "-e", "s/x/y/", "-e", "/a/d"],
    ["sed", "-i", "s/a/b/", "file"], ["sed", "-n", "/start/,/end/p"], ["sed", "s/a/e/"], ["sed", "s/e/e/g"],
    ["sed", "s/x/y/w out.txt"], ["sed", "s/x/y/w out;e id"], ["sed", "1d;$d"], ["sed", "y/abc/xyz/"], ["sed", "a\\\nline"], ["sed", "1i text e"],
    ["sed", "/x/{s/a/b/;p}"], ["sed", "-E", "s/(a|e)+/x/g"], ["sed", "s/\\//e/"], ["sed", ":a;N;$!ba;s/\\n/ /g"],
    ["sed", "--sandbox", "s/x/y/"], ["sed", "--version"], ["sed", "-n", "$="], ["sed", "r file"], ["sed", "/e/p"],
    ["sed", "\\,x,p"], ["sed", "/x/,+2p"], ["sed", "2,~4d"], ["sed", "-i.bake", "s/a/b/", "f"]])
def test_sed_stays_usable_for_local_work(guard, command):
    popen(guard, command, allowed)


@pytest.mark.parametrize("command", [
    ["tar", "-I", "curl", "-cf", "a.tar", "x"], ["tar", "-Icurl", "-xf", "a.tar"], ["tar", "-cIcurl", "x"],
    ["tar", "-xzf", "a.tar", "-I", "sh"], ["tar", "--use-compress-program=curl", "-cf", "a.tar", "x"],
    ["tar", "--use-compress-program", "curl", "-cf", "a.tar", "x"], ["tar", "--use-compress-prog=curl", "-cf", "a"],
    ["tar", "--use-c=curl", "-cf", "a"], ["tar", "--to-command=curl", "-xf", "a.tar"],
    ["tar", "--to-command", "sh", "-xf", "a.tar"], ["tar", "--to-c=sh", "-xf", "a.tar"],
    ["tar", "-xf", "a.tar", "--checkpoint=1", "--checkpoint-action=exec=curl"],
    ["tar", "-xf", "a.tar", "--checkpoint-action", "exec=sh"], ["tar", "-F", "script", "-cf", "a.tar", "x"],
    ["tar", "--info-script=script", "-cf", "a.tar", "x"], ["tar", "--new-volume-script=s", "-cf", "a.tar", "x"],
    ["tar", "--rsh-command=curl", "-cf", "host:a.tar", "x"], ["tar", "cIf", "curl", "a.tar", "x"],
    ["tar", "-cf", "user@host:a.tar", "x"], ["tar", "-cf", "host:a.tar", "x"], ["tar", "--file=host:a.tar", "-x"],
    ["env", "tar", "-I", "curl", "-cf", "a.tar", "x"]])
def test_tar_cannot_start_a_program_or_reach_a_remote_archive(guard, command):
    popen(guard, command, refused)


@pytest.mark.parametrize("command", [
    ["tar", "-cf", "a.tar", "x"], ["tar", "-czf", "a.tgz", "dir"], ["tar", "-xzf", "a.tgz", "-C", "out"],
    ["tar", "xf", "a.tar"], ["tar", "cvf", "a.tar", "x"], ["tar", "-tf", "a.tar"], ["tar", "-C/tmp/Inbox", "-xf", "a"],
    ["tar", "-cf", "C:\\work\\a.tar", "x"], ["tar", "-cf", "C:/work/a.tar", "x"], ["tar", "--file=a.tar", "-x"],
    ["tar", "--totals", "-cf", "a.tar", "x"], ["tar", "--list", "-f", "a.tar"], ["tar", "-xf", "a.tar", "Index.txt"],
    ["tar", "--force-local", "-cf", "ab:b.tar", "x"],
    ["tar", "-xf", "a.tar", "--checkpoint=1"], ["tar", "--exclude=I*", "-cf", "a.tar", "x"],
    ["tar", "-cf", "a.tar", "-T", "list"], ["tar", "--version"]])
def test_tar_stays_usable_for_local_work(guard, command):
    popen(guard, command, allowed)


@pytest.mark.parametrize("command", [
    ["sort", "--compress-program=curl", "f"], ["sort", "--compress-program", "sh", "f"],
    ["sort", "--compress-prog=sh", "f"], ["sort", "--co=sh", "f"], ["sort", "-S", "1M", "--compress-program=sh", "f"],
    ["env", "sort", "--compress-program=curl", "f"]])
def test_sort_cannot_start_a_compress_program(guard, command):
    popen(guard, command, refused)


@pytest.mark.parametrize("command", [
    ["sort", "f"], ["sort", "-u", "f"], ["sort", "-t,", "-k2", "-n", "f"], ["sort", "--check", "f"],
    ["sort", "--output=out", "f"], ["sort", "-S", "1M", "-T", "/tmp", "f"], ["sort", "--parallel=2", "f"],
    ["sort", "--reverse", "f"], ["sort", "--version"]])
def test_sort_stays_usable_for_local_work(guard, command):
    popen(guard, command, allowed)


@pytest.mark.parametrize("command", [
    ["zip", "-TT", "curl", "-r", "a.zip", "x"], ["zip", "-rTT", "curl", "a.zip", "x"],
    ["zip", "-T", "-TT", "sh", "a.zip", "x"], ["zip", "-TTcurl", "a.zip", "x"],
    ["zip", "--unzip-command", "curl", "a.zip", "x"], ["zip", "--unzip-command=curl", "a.zip", "x"],
    ["zip", "-qTT", "sh", "a.zip", "x"], ["env", "zip", "-TT", "curl", "a.zip", "x"]])
def test_zip_cannot_start_an_unzip_command(guard, command):
    popen(guard, command, refused)


@pytest.mark.parametrize("command", [
    ["zip", "a.zip", "x"], ["zip", "-r", "a.zip", "dir"], ["zip", "-rq", "a.zip", "dir"], ["zip", "-T", "a.zip"],
    ["zip", "-9", "-j", "a.zip", "x"], ["zip", "-x", "*.tmp", "-r", "a.zip", "dir"],
    ["zip", "-tt", "2026-01-01", "a.zip"], ["zip", "-r", "TT.zip", "dir"], ["unzip", "-t", "a.zip"],
    ["zip", "--version"]])
def test_zip_stays_usable_for_local_work(guard, command):
    popen(guard, command, allowed)


def test_the_docstring_names_the_options_it_reads_through():
    text = GUARD_FILE.read_text(encoding="utf-8").split('"""')[1]
    for phrase in ("awk", "sed", "tar", "sort", "zip"):
        assert phrase in text, phrase


def test_the_docstring_names_what_the_guard_cannot_see():
    text = GUARD_FILE.read_text(encoding="utf-8").split('"""')[1]
    for phrase in ("shell", "node", "C extension", "gRPC"):
        assert phrase in text, phrase


def test_the_decision_tests_leave_the_refusal_log_empty_even_when_the_job_names_one(tmp_path):
    """In CI the log is named for the whole job. Running a few of the refusing tests with it named must write
    nothing, so a refusal that does land in it came from somewhere else."""
    if os.environ.get("F42_GUARD_LOG_INNER") == "1":
        pytest.skip("this is the inner run")
    log = tmp_path / "inner.jsonl"
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_ADDOPTS"}
    env.update({"F42_GUARD_LOG_INNER": "1", "CORE_OFFLINE_GUARD_LOG": str(log),
                "PYTHONPATH": os.pathsep.join([str(GUARD_DIR), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)})
    done = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__)), "-q", "-p", "no:cacheprovider", "-x",
         "-k", "test_os_system_is_always_refused or test_executables_off_the_list_are_refused"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert " passed" in done.stdout
    assert not log.exists() or log.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("words", [["C:\\WINDOWS\\system32\\cmd.exe", "/c", "ver"],
                                   ["C:\\WINDOWS\\system32\\cmd.exe", "/c", "command /c ver"],
                                   ["C:\\WINDOWS\\system32\\cmd.exe", "/c", "cmd /c ver"]])
def test_the_version_probes_the_platform_module_makes_on_windows_stay_refused(guard, words):
    """The six refusals at collection on a Windows run are these three probes, made twice. platform.uname() makes
    them only when sys.platform is win32, so the Linux job never does, and the guard makes no exception for a
    program that is off its list."""
    refused(guard, "subprocess.Popen", (words[0], words, None, KEEPS_GUARD))
