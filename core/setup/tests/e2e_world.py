"""The world of the end to end release test: a real packet made by the packet command line, a real git repository at the release
commit, and the real SERVICES-PASTE.ps1 run in a pwsh process against the real readback helper.

Only the cloud is faked. gcloud is e2e_gcloud.py behind a gcloud.cmd the resolver finds; the network, the Google libraries and the
token issuer are doubled by e2e_shim/sitecustomize.py inside every Python child. git, py, bash and tar are the machine's own. The
console is the one thing the driver replaces in the paste (Read-Typed and Test-Interactive), because a test has no console.

Nothing the paste or the helper writes is seeded: no freeze-inputs.json, no manifest, no sidecar, no readback, no smoke receipt, no
ledger and no preservation state. They exist after a run only because a run wrote them.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from core.setup.release import natives, packet
from core.setup.tests import packet_world as pw
from core.setup.tests import release_world as rw

ROOT = pw.ROOT
HERE = Path(__file__).resolve().parent
SHIM = HERE / "e2e_shim"
FAKE_GCLOUD = HERE / "e2e_gcloud.py"
PASSCODE = "e2e-typed-passcode-by-albert"
ON_WINDOWS = sys.platform == "win32"

DRIVER = r'''
param([string]$Config)
$ErrorActionPreference = 'Stop'
$cfg = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$splat = @{ Action = $cfg.action; Lock = $cfg.lock; Review = $cfg.review; Bindings = $cfg.bindings; Receipt = $cfg.receipt; Repo = $cfg.repo; DefinitionsOnly = $true }
if ($cfg.quiet) { $splat.QuietWindowVerifiedAtUtc = $cfg.quiet }
. $cfg.paste @splat
$Script:TestNativeFolders = @{ gcloud = @($cfg.folders.gcloud); git = @($cfg.folders.git); py = @($cfg.folders.py); bash = @($cfg.folders.bash) }
function Test-Interactive { return $true }
function Read-Typed {
    param([string]$Prompt, [switch]$Secure)
    Add-Content -LiteralPath $cfg.prompts -Value (@{ prompt = $Prompt; secure = [bool]$Secure } | ConvertTo-Json -Compress) -Encoding utf8
    if ($Secure) { return (ConvertTo-SecureString $cfg.passcode -AsPlainText -Force) }
    return [regex]::Match($Prompt, 'Type (\w+) to continue').Groups[1].Value
}
$Script:TestDoubles = @('Test-Interactive', 'Read-Typed')
Invoke-Release
'''


def deploy_consistent_world():
    """The a80 world, with the secret references the deploy flags set (core/api/deploy_flags.env), so a candidate the fake deploy
    makes from those flags is the baseline plus the declared deltas and nothing else."""
    world = pw.a80_world(declared=())
    for service, name in (("f42-agent", "SOCIALCRAWL_OGILVY_API_KEY"), ("f42-api", "UI_PASSCODE")):
        secret = {"valueFrom": {"secretKeyRef": {"name": name, "key": "latest"}}}
        world.svc[service]["env"][name] = secret
        for revision in (rw.A80_REV[service], rw.OLDER_REV[service]):
            world.set_env(revision, name, secret)
    return world


@dataclass
class Run:
    action: str
    returncode: int
    stdout: str
    stderr: str
    started: dt.datetime
    run_dir: Path | None
    prompts: list = field(default_factory=list)

    @property
    def text(self):
        return self.stdout + "\n" + self.stderr


class E2E:
    def __init__(self, tmp, monkeypatch):
        self.tmp = Path(tmp)
        repo = self.tmp / "bench" / "repo"
        (repo / "core" / "agent").mkdir(parents=True)
        (repo / "core" / "agent" / "answer_state.py").write_bytes((ROOT / "core/agent/answer_state.py").read_bytes().replace(b"\r\n", b"\n"))
        self.world = deploy_consistent_world()
        self.bench = bench = pw.Bench(self.tmp / "bench", monkeypatch, world=self.world, repo=repo)
        bench.lock_dir = self.tmp / "locks"
        bench.prepare_inputs()
        for argv in (bench.capture_argv(), bench.bind_argv(), bench.lock_argv(), bench.review_argv(), bench.receipt_argv()):
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = packet.main(argv)
            assert code == 0, (argv[0], err.getvalue())
        self.repo, self.release_dir, self.rid, self.target = bench.repo, bench.packet, bench.release_id, bench.target
        self.paste = self.repo / "core/setup/release/SERVICES-PASTE.ps1"
        self.fakebin = self.tmp / "fakebin"
        self.fakebin.mkdir()
        (self.fakebin / "gcloud.cmd").write_text(f'@echo off\r\n"{sys.executable}" -S -B "{FAKE_GCLOUD}" %*\r\n', encoding="ascii", newline="")
        self.state, self.calls = self.tmp / "world.json", self.tmp / "calls.jsonl"
        self.state.write_text(json.dumps(self.world.__dict__), encoding="utf-8")
        self.calls.write_text("", encoding="utf-8")
        self.driver = self.tmp / "driver.ps1"
        self.driver.write_text(DRIVER, encoding="utf-8", newline="\n")
        self.count = 0

    # the world as the fake gcloud left it
    def world_now(self):
        world = rw.World()
        world.__dict__.update(json.loads(self.state.read_text(encoding="utf-8")))
        return world

    def set_world(self, **changes):
        data = json.loads(self.state.read_text(encoding="utf-8"))
        data.update(changes)
        self.state.write_text(json.dumps(data), encoding="utf-8")

    def logged(self, tool=None):
        entries = [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [e for e in entries if tool is None or e["tool"] == tool]

    # a pwsh process that runs the paste, as Albert's console would
    def run(self, action, *, inflight=0, quiet_minutes_ago=2, timeout=900):
        self.count += 1
        prompts = self.tmp / f"prompts-{self.count}.jsonl"
        prompts.write_text("", encoding="utf-8")
        folders = {name: os.path.dirname(natives.resolve(name)) for name in ("git", "py", "bash")}
        config = {"paste": str(self.paste), "action": action, "lock": str(self.bench.lock_file()), "review": str(self.bench.review),
                  "bindings": str(self.bench.bindings), "receipt": str(self.bench.receipt), "repo": str(self.repo), "prompts": str(prompts),
                  "quiet": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=quiet_minutes_ago)).isoformat(), "passcode": PASSCODE,
                  "folders": {**folders, "gcloud": str(self.fakebin)}}
        config_path = self.tmp / f"config-{self.count}.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "F42_SMOKE_PASSCODE", "CLOUDSDK_CORE_DISABLE_FILE_LOGGING",
                                                               "PYTHONDONTWRITEBYTECODE", "GIT_OPTIONAL_LOCKS") and not k.startswith("F42_NATIVE_")}
        env.update(PYTHONPATH=str(SHIM), F42_E2E_WORLD=str(self.state), F42_E2E_CALLS=str(self.calls), F42_E2E_TESTS_ROOT=str(ROOT),
                   F42_E2E_GCLOUD_DIR=str(self.fakebin), F42_E2E_PASSCODE=PASSCODE, F42_E2E_CALLER=pw.CALLER,
                   F42_E2E_FIXTURES=str(ROOT / "core/api/fixtures"), F42_E2E_INFLIGHT=str(inflight))
        started = dt.datetime.now(dt.timezone.utc)
        done = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(self.driver), "-Config", str(config_path)],
                              capture_output=True, stdin=subprocess.DEVNULL, encoding="utf-8", timeout=timeout, env=env)
        found = sorted((self.release_dir / "runs").glob(f"*-{action}")) if (self.release_dir / "runs").is_dir() else []
        asked = [json.loads(line) for line in prompts.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        return Run(action, done.returncode, done.stdout, done.stderr, started, found[-1] if found else None, asked)

    # what the run left, read from the files
    def readbacks(self, phase):
        folder = self.release_dir / "readbacks"
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(folder.glob(f"{phase}-*.json"))] if folder.is_dir() else []

    def json_file(self, name):
        return json.loads((self.release_dir / name).read_text(encoding="utf-8"))
