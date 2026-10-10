"""A packet and a PowerShell driver for the jobs release paste tests (W8-REL-B JU-02, JU-08, JU-09, JP-02). The real paste text is
dot-sourced in a pwsh process with -DefinitionsOnly, then Read-Native, Run-Logged, the prompt and Start-Sleep are replaced by doubles
that record every call to a file. Nothing external runs: no git, no gcloud, no python.

The packet is built in a temporary directory: a copy of the repository files the lock lists, a release directory with baseline-J,
the baseline chain manifest, the durable-effects manifest and the dry run receipt, and the lock, the review, the bindings and the
execution receipt. A test may change any file before the lock is built (to mutate the paste) or after (to break the lock).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.setup.release import lock as locklib
from core.setup.tests.paste_world import CALLER, COMMIT, ROOT, TREE

RID = "rel-d666ef6-01"

DRIVER = r'''
param([string]$Config)
$ErrorActionPreference = 'Stop'
$cfg = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$Script:Cfg = $cfg
function Env-Snap { return @{ file_logging = [string]$env:CLOUDSDK_CORE_DISABLE_FILE_LOGGING; bytecode = [string]$env:PYTHONDONTWRITEBYTECODE; locks = [string]$env:GIT_OPTIONAL_LOCKS } }
$Script:ClockMinutes = 0
$Script:WordCalls = @{}
$Script:RunCount = 0
function Log-Call($entry) { ($entry | ConvertTo-Json -Compress -Depth 6) | Add-Content -LiteralPath $Script:Cfg.calls -Encoding utf8 }
$splat = @{ Action = $cfg.action; Lock = $cfg.lock; Review = $cfg.review; Bindings = $cfg.bindings; Receipt = $cfg.receipt; Repo = $cfg.repo; DefinitionsOnly = $true }
. $cfg.paste @splat
function Get-UtcNow { return [DateTimeOffset]::UtcNow.AddMinutes($Script:ClockMinutes) }

function Read-Native([string]$Exe, [string[]]$Arguments, [string]$InputText) {
    $key = ((@($Exe) + $Arguments) -join ' ')
    Log-Call @{ kind = 'read'; argv = (@($Exe) + $Arguments); env = (Env-Snap) }
    switch -Regex ($key) {
        '^git rev-parse HEAD$' { return $Script:Cfg.commit }
        '^git rev-parse HEAD\^\{tree\}$' { return $Script:Cfg.tree }
        '^git status --porcelain=v1$' { if ([int]$Script:Cfg.dirty_after_runs -ge 0 -and $Script:RunCount -ge [int]$Script:Cfg.dirty_after_runs) { return ' M core/x.py' }; return $Script:Cfg.status }
        '^gcloud config list' { return $Script:Cfg.config_json }
        default { throw "unexpected read: $key" }
    }
}

function Run-Logged([string]$Name, [string[]]$Argv, [int[]]$Accept = @(), [string]$WorkDir = '') {
    $Script:RunCount++
    if ($Script:Cfg.inject -and $Script:RunCount -eq [int]$Script:Cfg.inject_at_run) { . ([scriptblock]::Create($Script:Cfg.inject)) }
    $tokenPath = Join-Path $Script:RunDir 'update-token.json'
    $tokenText = if (Test-Path -LiteralPath $tokenPath) { Get-Content -LiteralPath $tokenPath -Raw } else { $null }
    Log-Call @{ kind = 'run'; name = $Name; argv = $Argv; workdir = $WorkDir; run_dir_existed = (Test-Path -LiteralPath $Script:RunDir -PathType Container); env = (Env-Snap); token = $tokenText }
    $code = 0
    if ($Argv[0] -match '(^|[\\/])tar(\.exe)?$') {
        $target = $Argv[[array]::IndexOf($Argv, '-C') + 1]
        if (-not (Test-Path -LiteralPath $target -PathType Container)) { $code = 2 }
    }
    $scripted = $Script:Cfg.exits.PSObject.Properties[$Name]
    if ($null -ne $scripted) { $code = [int]$scripted.Value }
    $text = @()
    if ($code -eq 0 -and ($Argv -join ' ') -like '*bound_readback.py*') {
        $phase = $Argv[[array]::IndexOf($Argv, '--phase') + 1]
        if ($Script:Cfg.no_readback -notcontains $phase) {
            $folder = Join-Path $Script:Paths.root 'readbacks'
            New-Item -ItemType Directory -Path $folder -Force | Out-Null
            $body = @{ schema_version = 1; phase = $phase }
            if ($phase -eq 'BeforeJobsUpdate') {
                $body.quiet_snapshot = @{ taken_at = (Get-UtcNow).AddMinutes(-[double]$Script:Cfg.snapshot_age_minutes).ToString('o') }
            }
            ($body | ConvertTo-Json -Compress -Depth 5) | Set-Content -LiteralPath (Join-Path $folder "$phase-01.json") -Encoding utf8
        }
    }
    [IO.File]::WriteAllLines((Join-Path $Script:RunDir ($Name + '.log')), [string[]]$text)
    [string]$code | Set-Content -LiteralPath (Join-Path $Script:RunDir ($Name + '.exit.txt')) -Encoding utf8
    if ($code -ne 0 -and $code -notin $Accept) { throw "$Name exited $code. Stop; the release may be partial. The declared branch is printed above." }
    return $code
}

if (-not $Script:Cfg.real_console) {
function Test-Interactive { return (-not $Script:Cfg.no_interactive) }

function Read-Typed {
    param([string]$Prompt)
    $Script:ClockMinutes += [double]$Script:Cfg.minutes_per_prompt
    Log-Call @{ kind = 'prompt'; prompt = $Prompt; run_dir_existed = ($null -ne (Get-Variable -Scope Script -Name RunDir -ErrorAction SilentlyContinue)) -and (Test-Path -LiteralPath $Script:RunDir -PathType Container); runs_so_far = $Script:RunCount }
    if ($Script:Cfg.no_interactive) { throw 'no interactive host' }
    $word = [regex]::Match($Prompt, 'Type (\w+) to continue').Groups[1].Value
    $answer = $Script:Cfg.words.PSObject.Properties[$word]
    $Script:WordCalls[$word] = 1 + [int]$Script:WordCalls[$word]
    if ($null -ne $answer -and $answer.Value -is [array]) { return [string]$answer.Value[[Math]::Min($Script:WordCalls[$word], $answer.Value.Count) - 1] }
    if ($null -ne $answer) { return [string]$answer.Value }
    return $word
}
}

function Start-Sleep { param($Seconds) Log-Call @{ kind = 'sleep'; seconds = $Seconds } }

$Script:TestDoubles = @('Read-Native', 'Run-Logged', 'Get-UtcNow', 'Start-Sleep')
if (-not $cfg.real_console) { $Script:TestDoubles += @('Read-Typed', 'Test-Interactive') }
foreach ($alias in @($cfg.aliases)) { Set-Alias -Scope Global -Name $alias.name -Value $alias.value }
if ($cfg.attack) { . ([scriptblock]::Create($cfg.attack)) }
try { Invoke-Release } finally { Log-Call @{ kind = 'env_at_end'; env = (Env-Snap) }; Log-Call @{ kind = 'end' } }
'''


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    calls: list

    @property
    def runs(self):
        return [c for c in self.calls if c["kind"] == "run"]

    @property
    def names(self):
        return [c["name"] for c in self.runs]

    @property
    def external(self):
        return [c for c in self.calls if c["kind"] in ("read", "run")]

    @property
    def prompts(self):
        return [c for c in self.calls if c["kind"] == "prompt"]

    def run(self, name):
        return next(c for c in self.runs if c["name"] == name)


class JobsPasteWorld:
    def __init__(self, tmp_path, action="JobsUpdate", *, mutate_paste=None, receipt=None, bindings=None, declared_steps=None):
        self.tmp = Path(tmp_path)
        self.action, self.rid = action, RID
        self.repo = self.tmp / "repo"
        for rel in locklib.REPO_FILES:
            if (ROOT / rel).is_file():
                target = self.repo / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / rel, target)
        self.paste = self.repo / "core/setup/release/JOBS-PASTE.ps1"
        if mutate_paste:
            self.paste.write_text(mutate_paste(self.paste.read_text(encoding="utf-8")), encoding="utf-8", newline="")
        self.release_dir = self.tmp / "releases" / RID
        self.release_dir.mkdir(parents=True)
        self.baseline = self.write(self.tmp / "baseline" / "baseline-J.json", {"schema_version": 1, "kind": "baseline-J"})
        self.durable = self.write(self.release_dir / "durable-effects.json", {"schema_version": 1, "effects": [{"apply_kind": "schema"}, {"apply_kind": "job_image"}]})
        self.chain = self.write(self.release_dir / "chain-evidence-2026-10-10.json", {"schema_version": 1, "kind": "chain-evidence"})
        self.dry_run = self.write(self.release_dir / "apply-dry-run-receipt.json", {"schema_version": 1, "views": "no drift"})
        self.bindings = self.write(self.tmp / "bindings.json", {
            "schema_version": 1, "mode": "jobs", "status": "BOUND_FOR_INDEPENDENT_REVIEW", "release_id": RID, "target": COMMIT, "tree": TREE,
            "callerAccount": CALLER, "releaseDir": str(self.release_dir), "baselinePath": str(self.baseline), "baselineChainPath": str(self.chain),
            "durableManifestPath": str(self.durable), "dryRunReceiptPath": str(self.dry_run),
            "schemaReadbackReceiptPath": str(self.release_dir / "schema-readback-receipt.json"), "schemaReadbackReceiptSha256": "e1" * 32,
            **(bindings or {})})
        self.lock = self.build_lock()
        self.review = self.write(self.tmp / "review.json", locklib.review_for(self.lock, COMMIT))
        self.receipt = self.write(self.tmp / "receipt.json", {
            "schema_version": 1, "release_id": RID, "mode": "jobs", "target": COMMIT, "tree": TREE,
            "declared_steps": declared_steps or ["JobsCandidate", "JobsUpdate", "JobsRollback"],
            "later_explicit_user_instruction": True, "quiet_window_confirmed": True, "no_new_manual_starts_until_execution_ends": True,
            **(receipt or {})})

    @staticmethod
    def write(path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2), encoding="utf-8", newline="\n")
        return path

    def build_lock(self):
        bound = {"bindings": self.bindings, "baseline": self.baseline, "durable_manifest": self.durable, "baseline_chain": self.chain,
                 "dry_run_receipt": self.dry_run}
        return self.write(self.tmp / "lock.json", locklib.build_lock(self.repo, RID, bound, names=locklib.JOBS_BOUND_NAMES))

    def relock(self):
        self.lock = self.build_lock()
        self.write(self.review, locklib.review_for(self.lock, COMMIT))

    def run(self, *, exits=None, interactive=True, action=None, status="", words=None, snapshot_age=1.0, no_readback=(),
            minutes_per_prompt=0, aliases=(), extra=None, paste=None, dirty_after_runs=-1):
        calls = self.tmp / "calls.jsonl"
        calls.write_text("", encoding="utf-8")
        config = {
            "paste": str(paste or self.paste), "dirty_after_runs": dirty_after_runs, "action": action or self.action, "lock": str(self.lock), "review": str(self.review), "bindings": str(self.bindings),
            "receipt": str(self.receipt), "repo": str(self.repo), "calls": str(calls), "commit": COMMIT, "tree": TREE, "status": status,
            "config_json": json.dumps({"core": {"account": CALLER, "project": "ogilvy-trends-v2"}}), "exits": exits or {},
            "no_interactive": not interactive, "words": words or {}, "snapshot_age_minutes": snapshot_age,
            "no_readback": list(no_readback), "minutes_per_prompt": minutes_per_prompt, "aliases": list(aliases), "real_console": False, "attack": "",
            "inject": "", "inject_at_run": 0, **(extra or {})}
        self.write(self.tmp / "config.json", config)
        driver = self.tmp / "driver.ps1"
        driver.write_text(DRIVER, encoding="utf-8", newline="\n")
        clean = {k: v for k, v in os.environ.items() if k not in ("CLOUDSDK_CORE_DISABLE_FILE_LOGGING", "PYTHONDONTWRITEBYTECODE", "GIT_OPTIONAL_LOCKS")}
        proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(driver), "-Config", str(self.tmp / "config.json")],
                              capture_output=True, stdin=subprocess.DEVNULL, encoding="utf-8", timeout=120, env=clean)
        entries = [json.loads(line) for line in calls.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        return Result(proc.returncode, proc.stdout, proc.stderr, entries)
