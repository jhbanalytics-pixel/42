"""F11: the children of the release paste start gcloud and git from the install folders the paste uses, never by PATH lookup.

core/setup/release/natives.py is the resolver the children share. These tests run in the core partition, so they start no
program: a recording stand-in for subprocess.run shows which executable each child launches. The tests that run a real child,
and the parity of the resolver with the paste, are in test_services_paste.py and test_deploy_candidate.py (deploy partition).
"""
import io
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

from core.setup import durable_effects_check as dec
from core.setup.release import bound_readback as helper
from core.setup.release import lock as locklib
from core.setup.release import natives
from core.setup.release import packet
from core.setup.release import services_only as so

ROOT = Path(__file__).resolve().parents[3]
WINDOWS = os.name == "nt"
FILES = {"gcloud": "gcloud.cmd", "git": "git.exe", "py": "py.exe", "bash": "bash.exe"}

# The folders of packet section 3a, written out here and not read from the module: base name, then the parts under it.
PINNED = {
    "gcloud": [("local", ()), ("x86", ()), ("pf", ())],
    "git": [("local", ("Programs", "Git", "cmd")), ("pf", ("Git", "cmd")), ("x86", ("Git", "cmd"))],
    "bash": [("local", ("Programs", "Git", "bin")), ("pf", ("Git", "bin")), ("x86", ("Git", "bin"))],
    "py": [("win", ()), ("local", ("Programs", "Python", "Launcher")), ("pf", ("Python Launcher",))],
}
PINNED["gcloud"] = [(base, ("Google", "Cloud SDK", "google-cloud-sdk", "bin")) for base, _ in PINNED["gcloud"]]


def fake_roots(tmp_path):
    base = {"local": tmp_path / "local", "pf": tmp_path / "pf", "x86": tmp_path / "x86", "win": tmp_path / "win"}
    roots = natives.Roots(local=str(base["local"]), program_files=str(base["pf"]), program_files_x86=str(base["x86"]),
                          windows_folder=str(base["win"]))
    return roots, base


def pinned_folders(name, base):
    return [os.path.join(base[b], *parts) for b, parts in PINNED[name]]


def put(folder, name, text="x"):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / FILES[name]).write_text(text, encoding="utf-8")
    return str(folder / FILES[name])


def plant(folder):
    """What a hostile PATH offers ahead of the install folders: the script, the command file and the program of each name."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("gcloud", "git", "py"):
        for suffix in (".ps1", ".cmd", ".exe"):
            (folder / f"{name}{suffix}").write_text("planted", encoding="utf-8")
    return folder


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Fake install roots that the resolver is told to use, every program in its first folder, and a planted PATH first."""
    roots, base = fake_roots(tmp_path)
    monkeypatch.setattr(natives, "known_roots", lambda: roots)
    paths = {name: put(pinned_folders(name, base)[0], name) for name in ("gcloud", "git", "py")}
    planted = plant(tmp_path / "planted")
    monkeypatch.setenv("PATH", str(planted) + os.pathsep + os.environ.get("PATH", ""))
    for variable in natives.ENV_NAMES.values():
        monkeypatch.delenv(variable, raising=False)
    return roots, base, paths, planted


def test_f11_the_install_folders_are_the_pinned_ones_in_the_pinned_order(tmp_path):
    if not WINDOWS:
        assert natives.install_folders("gcloud", fake_roots(tmp_path)[0]) == []
        return
    roots, base = fake_roots(tmp_path)
    for name in PINNED:
        assert natives.install_folders(name, roots) == pinned_folders(name, base), name


def test_f11_a_root_that_is_empty_adds_no_folder_as_in_the_paste(tmp_path):
    if not WINDOWS:
        return
    roots, base = fake_roots(tmp_path)
    roots = roots._replace(program_files_x86="")
    assert natives.install_folders("gcloud", roots) == [os.path.join(base[b], *parts) for b, parts in PINNED["gcloud"] if b != "x86"]
    assert natives.install_folders("git", roots) == [os.path.join(base[b], *parts) for b, parts in PINNED["git"] if b != "x86"]


def test_f11_resolve_takes_the_first_folder_that_holds_the_program(tmp_path):
    if not WINDOWS:
        return
    roots, base = fake_roots(tmp_path)
    second = put(pinned_folders("git", base)[1], "git")
    assert natives.resolve("git", roots) == second
    first = put(pinned_folders("git", base)[0], "git")
    assert natives.resolve("git", roots) == first


@pytest.mark.parametrize("what", ["scripts only", "directory of the name", "empty folders", "program only on PATH"])
def test_f11_resolve_refuses_a_script_a_directory_and_a_program_found_only_on_path(tmp_path, monkeypatch, what):
    if not WINDOWS:
        return
    roots, base = fake_roots(tmp_path)
    folder = Path(pinned_folders("gcloud", base)[0])
    folder.mkdir(parents=True)
    if what == "scripts only":
        (folder / "gcloud.ps1").write_text("x", encoding="utf-8")
        (folder / "gcloud").write_text("x", encoding="utf-8")
    elif what == "directory of the name":
        (folder / "gcloud.cmd").mkdir()
    planted = plant(tmp_path / "planted")
    monkeypatch.setenv("PATH", str(planted) + os.pathsep + os.environ.get("PATH", ""))
    with pytest.raises(natives.NativeRefused) as caught:
        natives.resolve("gcloud", roots)
    assert "gcloud" in str(caught.value)


def test_f11_native_takes_the_path_the_paste_passed_when_it_is_the_one_the_resolver_finds(world):
    if not WINDOWS:
        return
    roots, base, paths, planted = world
    assert natives.native("gcloud", {"F42_NATIVE_GCLOUD": paths["gcloud"]}) == paths["gcloud"]
    assert natives.native("git", {"F42_NATIVE_GIT": paths["git"]}) == paths["git"]
    assert natives.native("py", {"F42_NATIVE_PY": paths["py"]}) == paths["py"]
    # the same file written with another case and another separator is the same file
    assert natives.native("gcloud", {"F42_NATIVE_GCLOUD": paths["gcloud"].upper().replace("\\", "/")}) == paths["gcloud"].upper().replace("\\", "/")


def test_f11_native_without_a_passed_path_resolves_from_the_install_folders_and_not_from_path(world):
    if not WINDOWS:
        return
    roots, base, paths, planted = world
    for name in ("gcloud", "git", "py"):
        assert natives.native(name, {}) == paths[name]
        assert natives.native(name, {natives.ENV_NAMES[name]: ""}) == paths[name]


@pytest.mark.parametrize("bad", ["ps1", "bare name", "relative", "missing file", "other file", "script beside the program", "planted program",
                                 "second folder while the first holds the program"])
def test_f11_native_refuses_a_passed_path_that_is_a_script_a_name_a_relative_path_or_not_the_resolved_program(world, bad):
    if not WINDOWS:
        return
    roots, base, paths, planted = world
    folder = Path(paths["gcloud"]).parent
    if bad == "ps1":
        value = str(folder / "gcloud.ps1")
        (folder / "gcloud.ps1").write_text("x", encoding="utf-8")
    elif bad == "bare name":
        value = "gcloud"
    elif bad == "relative":
        value = "bin/gcloud.cmd"
    elif bad == "missing file":
        value = str(folder / "nowhere" / "gcloud.cmd")
    elif bad == "other file":
        value = put(folder, "git")
    elif bad == "script beside the program":
        value = str(folder / "gcloud.cmd.ps1")
        (folder / "gcloud.cmd.ps1").write_text("x", encoding="utf-8")
    elif bad == "planted program":
        value = str(planted / "gcloud.cmd")
    else:
        value = put(pinned_folders("gcloud", base)[1], "gcloud")
    with pytest.raises(natives.NativeRefused):
        natives.native("gcloud", {"F42_NATIVE_GCLOUD": value})


def test_f11_off_windows_the_names_are_kept_as_before(tmp_path):
    roots, _ = fake_roots(tmp_path)
    for name in ("gcloud", "git", "py"):
        assert natives.native(name, {natives.ENV_NAMES[name]: "ignored"}, roots, windows=False) == name
        assert natives.resolve(name, roots, windows=False) == name
        assert natives.install_folders(name, roots, windows=False) == []


def test_f11_the_children_do_not_look_a_program_up_by_name():
    for rel in ("core/setup/release/bound_readback.py", "core/setup/release/packet.py", "core/setup/durable_effects_check.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "shutil.which" not in text and not re.search(r'\[\s*"(git|gcloud)"\s*,', text), rel


EMPTY_TAR = io.BytesIO()
with tarfile.open(fileobj=EMPTY_TAR, mode="w"):
    pass


class Recorder:
    def __init__(self):
        self.argv = []

    def __call__(self, command, *args, **kwargs):
        self.argv.append(list(command))
        text = "{}" if kwargs.get("encoding") else EMPTY_TAR.getvalue()
        return subprocess.CompletedProcess(command, 0, stdout=text, stderr="" if kwargs.get("encoding") else b"")


def child_launches(tmp_path):
    reader = packet.baseline_reader(30)
    return {
        "helper gcloud read": ("gcloud", lambda: reader.config()),
        "helper git show": ("git", lambda: helper.GcloudReader({"gcloud": 5, "http": 5}).source_file_sha256("a" * 40, "core/api/smoke.py")),
        "checker git archive": ("git", lambda: dec.git_tree_files("HEAD", "core", cwd=tmp_path)),
        "packet git": ("git", lambda: packet.git(tmp_path, "rev-parse", "HEAD")),
        "packet git show": ("git", lambda: packet.blob_sha256(tmp_path, "a" * 40, "core/api/smoke.py")),
        "capture-baseline reader": ("gcloud", lambda: packet.baseline_reader(30).service("f42-agent")),
    }


@pytest.mark.parametrize("child", ["helper gcloud read", "helper git show", "checker git archive", "packet git", "packet git show", "capture-baseline reader"])
def test_f11_each_child_launches_the_install_path_even_with_a_planted_program_first_on_path(world, monkeypatch, tmp_path, child):
    roots, base, paths, planted = world
    recorder = Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    program, call = child_launches(tmp_path)[child]
    if WINDOWS:
        call()
        assert recorder.argv and os.path.normcase(recorder.argv[0][0]) == os.path.normcase(paths[program]), (child, recorder.argv)
        assert not any(os.path.dirname(os.path.normcase(a[0])) == os.path.normcase(str(planted)) for a in recorder.argv)
    else:
        call()
        assert recorder.argv[0][0] == program


@pytest.mark.parametrize("child", ["helper gcloud read", "helper git show", "checker git archive", "packet git", "packet git show"])
def test_f11_each_child_uses_the_path_passed_to_it_when_there_is_one(world, monkeypatch, tmp_path, child):
    if not WINDOWS:
        return
    roots, base, paths, planted = world
    second = {name: put(pinned_folders(name, base)[1], name) for name in ("git", "gcloud")}
    for name in second:
        os.remove(paths[name])
    for name, value in second.items():
        monkeypatch.setenv(natives.ENV_NAMES[name], value)
    recorder = Recorder()
    monkeypatch.setattr(subprocess, "run", recorder)
    program, call = child_launches(tmp_path)[child]
    call()
    assert os.path.normcase(recorder.argv[0][0]) == os.path.normcase(second[program])


BAD_PASSED = [
    ("F42_NATIVE_GCLOUD", lambda paths, planted: str(planted / "gcloud.cmd")),
    ("F42_NATIVE_GCLOUD", lambda paths, planted: str(Path(paths["gcloud"]).with_suffix(".ps1"))),
    ("F42_NATIVE_GCLOUD", lambda paths, planted: "gcloud"),
    ("F42_NATIVE_GIT", lambda paths, planted: str(planted / "git.exe")),
    ("F42_NATIVE_GIT", lambda paths, planted: "git"),
]


@pytest.mark.parametrize("variable, value_of", BAD_PASSED)
def test_f11_a_child_refuses_a_passed_path_that_is_a_planted_program_a_script_or_a_bare_name_and_runs_nothing(world, monkeypatch, tmp_path, variable, value_of):
    if not WINDOWS:
        return
    roots, base, paths, planted = world
    Path(paths["gcloud"]).with_suffix(".ps1").write_text("x", encoding="utf-8")
    monkeypatch.setenv(variable, value_of(paths, planted))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("a child ran a program that is not the install one"))
    reader = helper.GcloudReader({"gcloud": 5, "http": 5})
    if variable.endswith("GCLOUD"):
        with pytest.raises(so.Stop) as stop:
            reader.config()
        assert stop.value.code == "NATIVE"
        return
    with pytest.raises(so.Stop) as stop:
        reader.source_file_sha256("a" * 40, "core/api/smoke.py")
    assert stop.value.code == "NATIVE"
    with pytest.raises(packet.Refused):
        packet.git(tmp_path, "rev-parse", "HEAD")
    with pytest.raises(packet.Refused):
        packet.blob_sha256(tmp_path, "a" * 40, "core/api/smoke.py")
    with pytest.raises(dec.Refusal):
        dec.git_tree_files("HEAD", "core", cwd=tmp_path)


def test_f11_the_lock_binds_the_resolver_and_its_test():
    assert "core/setup/release/natives.py" in locklib.REPO_FILES
    assert "core/setup/tests/test_natives.py" in locklib.TEST_FILES
    assert (ROOT / "core/setup/release/natives.py").is_file() and (ROOT / "core/setup/tests/test_natives.py").is_file()
