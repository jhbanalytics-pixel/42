"""Where the programs started by the release paste's children come from (F11).

The paste (SERVICES-PASTE.ps1, Get-NativePath) finds gcloud, git, py and bash as program files in a fixed list of install
folders and runs them by full path. The programs it starts in turn (the readback helper, the durable effects checker, the
packet tools and deploy_candidate.sh) start gcloud and git, and must not look them up through PATH either, where a script or a
program of the same name put ahead of the real one would be found first. This module is the one place they ask.

The folders are the paste's, in the paste's order, taken from the operating system's known folders (not from environment
variables, which a console can change). The paste hands each child the program it found in F42_NATIVE_GCLOUD, F42_NATIVE_GIT and
F42_NATIVE_PY. A child takes that path only when it is the very file the resolver finds, so a value that came from anywhere else
(a stale variable, a script, a bare name, a program on PATH, another install folder) is refused, and a child that was not given
one resolves it here. Off Windows nothing changes: the plain names are kept, as before.
"""
from __future__ import annotations

import os
from typing import NamedTuple

ENV_NAMES = {"gcloud": "F42_NATIVE_GCLOUD", "git": "F42_NATIVE_GIT", "py": "F42_NATIVE_PY"}
FILES = {"gcloud": "gcloud.cmd", "git": "git.exe", "py": "py.exe", "bash": "bash.exe"}


class NativeRefused(Exception):
    """A program was not found as a program in its install folder, or a path handed over is not that program."""


class Roots(NamedTuple):
    local: str
    program_files: str
    program_files_x86: str
    windows_folder: str


def on_windows():
    return os.name == "nt"


def known_roots():
    """LocalApplicationData, ProgramFiles, ProgramFilesX86 and Windows from the system's known folders, as the paste reads them
    through [Environment]::GetFolderPath. A folder the system does not have is an empty string."""
    import ctypes
    import uuid

    class Guid(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]

    def guid(text):
        u = uuid.UUID(text)
        return Guid(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))

    shell32, ole32 = ctypes.WinDLL("shell32"), ctypes.WinDLL("ole32")
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(Guid), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    def folder(text):
        out = ctypes.c_wchar_p()
        try:
            return out.value or "" if shell32.SHGetKnownFolderPath(ctypes.byref(guid(text)), 0, None, ctypes.byref(out)) == 0 else ""
        finally:
            ole32.CoTaskMemFree(out)

    return Roots(local=folder("F1B32785-6FBA-4FCF-9D55-7B8E7F157091"), program_files=folder("905E63B6-C1BF-494E-B29C-65B732D3D21A"),
                 program_files_x86=folder("7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E"), windows_folder=folder("F38BF404-1D43-42F2-9305-67DE0B28FC23"))


def install_folders(name, roots=None, windows=None):
    """The folders a program may come from, in the order the paste tries them. A root that is empty adds no folder."""
    if not (on_windows() if windows is None else windows):
        return []
    roots = known_roots() if roots is None else roots
    local, files, files86, windir = roots.local, roots.program_files, roots.program_files_x86, roots.windows_folder
    sdk = ("Google", "Cloud SDK", "google-cloud-sdk", "bin")
    table = {
        "gcloud": [(local, sdk), (files86, sdk), (files, sdk)],
        "git": [(local, ("Programs", "Git", "cmd")), (files, ("Git", "cmd")), (files86, ("Git", "cmd"))],
        "bash": [(local, ("Programs", "Git", "bin")), (files, ("Git", "bin")), (files86, ("Git", "bin"))],
        "py": [(windir, ()), (local, ("Programs", "Python", "Launcher")), (files, ("Python Launcher",))],
    }[name]
    return [os.path.join(base, *parts) for base, parts in table if base]


def resolve(name, roots=None, windows=None):
    """The program as a file in the first install folder that holds it. PATH is never searched and a script is never taken."""
    if not (on_windows() if windows is None else windows):
        return name
    folders = install_folders(name, roots, windows=True)
    for folder in folders:
        candidate = os.path.join(folder, FILES[name])
        if os.path.isfile(candidate):
            return candidate
    raise NativeRefused(f"NOT EXECUTABLE: the program '{name}' was not found as a program (a script is never run in its place) "
                        f"in its install folder. Looked in: {'; '.join(folders)}.")


def native(name, environ=None, roots=None, windows=None):
    """The path a child runs for gcloud, git or py: the one the paste passed when it is the resolved program, else the resolved
    program. A passed value that is anything else is refused, never run and never replaced by a PATH lookup."""
    if not (on_windows() if windows is None else windows):
        return name
    environ = os.environ if environ is None else environ
    roots = known_roots() if roots is None else roots
    expected = resolve(name, roots, windows=True)
    passed = environ.get(ENV_NAMES[name], "")
    if not passed:
        return expected
    if not os.path.splitdrive(passed)[0] or not os.path.isabs(passed):
        raise NativeRefused(f"{ENV_NAMES[name]} is not a full path to the program: a name looked up on PATH is never run.")
    if os.path.basename(passed).lower() != FILES[name]:
        raise NativeRefused(f"{ENV_NAMES[name]} does not name {FILES[name]}: a script or another file is never run in its place.")
    try:
        same = os.path.samefile(passed, expected)
    except OSError:
        same = False
    if not same:
        raise NativeRefused(f"{ENV_NAMES[name]} is not the {name} program found in its install folder.")
    return passed
