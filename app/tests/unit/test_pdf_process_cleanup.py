"""Cleanup of a PDF worker descendant that left the worker's process tree.

The cleanup monitor learns the worker's descendants by sampling parent links in
/proc. A descendant that starts its own session and whose parent exits before a
sample completes is reparented away from the worker and never sampled, while it
still holds the worker's output pipe. Each case here starves the monitor so that
no sample ever sees the descendant, which is what a loaded host does by chance.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.api import pdf_exporter

posix_only = pytest.mark.skipif(
    os.name != "posix", reason="Detached process sessions require Linux"
)

# The worker writes its detached child's pid, waits for the case to be ready,
# then exits while the child keeps the inherited stdout and stderr open.
WORKER = (
    "import json,os,subprocess,sys,time\n"
    "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
    "start_new_session=True)\n"
    "open(sys.argv[1],'w').write(json.dumps({'child':child.pid}))\n"
    "ready=sys.argv[2]\n"
    "deadline=time.monotonic()+10\n"
    "while ready!='-' and not os.path.exists(ready) and time.monotonic()<deadline:\n"
    "    time.sleep(0.01)\n"
    "time.sleep(0.1)\n"
)


def _alive(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return fields[fields.rfind(")") + 2] != "Z"


def _kill(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _starve_monitor(monkeypatch):
    """Keep every monitor sample until the worker has exited and been reaped."""
    original = pdf_exporter._ProcessTreeMonitor._run

    def starved(self):
        time.sleep(0.6)
        original(self)

    monkeypatch.setattr(pdf_exporter._ProcessTreeMonitor, "_run", starved)


def _run_worker(tmp_path, ready="-"):
    script = tmp_path / "worker.py"
    script.write_text(WORKER, encoding="utf-8")
    state = tmp_path / "pids.json"
    reason = None
    try:
        pdf_exporter._run_isolated_process(
            [sys.executable, str(script), str(state), str(ready)],
            pdf_exporter._renderer_environment(tmp_path),
            timeout_seconds=5,
        )
    except pdf_exporter.PdfExportError as error:
        reason = error.reason
    child = json.loads(state.read_text(encoding="utf-8"))["child"]
    return reason, child


@posix_only
def test_detached_pipe_holder_the_monitor_never_sampled_is_killed(
    tmp_path, monkeypatch
):
    _starve_monitor(monkeypatch)
    child = None
    try:
        code, child = _run_worker(tmp_path)
        assert code == "pdf_worker_failed"
        assert not _alive(child)
    finally:
        if child is not None:
            _kill(child)


@posix_only
def test_a_bystander_holding_only_the_read_end_is_not_killed(tmp_path, monkeypatch):
    """Only a writer can keep the worker's output open, so only a writer is hunted."""
    _starve_monitor(monkeypatch)
    original_popen = subprocess.Popen
    bystanders = []

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        bystanders.append(
            original_popen(
                [sys.executable, "-c", "import time;time.sleep(60)"],
                pass_fds=(process.stdout.fileno(), process.stderr.fileno()),
                start_new_session=True,
            )
        )
        return process

    monkeypatch.setattr(pdf_exporter.subprocess, "Popen", popen)
    child = None
    try:
        code, child = _run_worker(tmp_path)
        assert code == "pdf_worker_failed"
        assert not _alive(child)
        assert bystanders and bystanders[0].poll() is None
    finally:
        if child is not None:
            _kill(child)
        for bystander in bystanders:
            bystander.kill()
            bystander.wait(timeout=5)


@posix_only
def test_a_writer_older_than_the_worker_is_not_killed_and_cleanup_fails(
    tmp_path, monkeypatch
):
    """A process that predates the worker is never a worker descendant.

    It can still hold the worker's pipe for writing, here by opening the
    descendant's stdout through /proc. Cleanup must not kill it, and it must not
    report a clean cleanup while a writer the export cannot account for remains.
    """
    _starve_monitor(monkeypatch)
    state = tmp_path / "pids.json"
    ready = tmp_path / "ready"
    older = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import json,os,sys,time\n"
            "state,ready=sys.argv[1],sys.argv[2]\n"
            "deadline=time.monotonic()+10\n"
            "while not os.path.exists(state) and time.monotonic()<deadline:\n"
            "    time.sleep(0.01)\n"
            "time.sleep(0.05)\n"
            "child=json.load(open(state))['child']\n"
            "held=open(f'/proc/{child}/fd/1','wb')\n"
            "open(ready,'w').write('1')\n"
            "time.sleep(60)\n",
            str(state),
            str(ready),
        ],
        start_new_session=True,
    )
    time.sleep(0.05)
    child = None
    try:
        code, child = _run_worker(tmp_path, ready)
        assert ready.is_file()
        assert code == "pdf_cleanup_failed"
        assert older.poll() is None
        assert not _alive(child)
    finally:
        if child is not None:
            _kill(child)
        older.kill()
        older.wait(timeout=5)


@posix_only
def test_unreadable_descriptor_tables_report_cleanup_failure(tmp_path, monkeypatch):
    """Where /proc/<pid>/fd cannot be read, a surviving writer is reported, not hidden."""
    _starve_monitor(monkeypatch)

    def unreadable(_pid):
        raise PermissionError("descriptor table not readable")

    monkeypatch.setattr(pdf_exporter, "_process_descriptors", unreadable, raising=False)
    child = None
    try:
        code, child = _run_worker(tmp_path)
        assert code == "pdf_cleanup_failed"
    finally:
        if child is not None:
            _kill(child)
