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
    key = tmp_path / "application_default_credentials.json"
    key.write_text("{}", encoding="utf-8")
    code = (f"try:\n open({str(key)!r}).read()\nexcept PermissionError as e:\n print('REFUSED', e)\n")
    done = child(code)
    assert "REFUSED offline guard" in done.stdout, (done.stdout, done.stderr)


def test_a_child_without_the_guard_directory_runs_unguarded():
    done = child("import sys; print('sitecustomize' in sys.modules and hasattr(sys.modules['sitecustomize'], 'GUARD_ID'))",
                 guarded=False)
    assert done.stdout.strip() == "False", (done.stdout, done.stderr)


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
