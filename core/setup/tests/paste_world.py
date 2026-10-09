"""A packet and a PowerShell driver for the release paste tests (W8-REL 5.2). The real paste text is dot-sourced in a pwsh
process with -DefinitionsOnly, then Read-Native, Run-Logged, Read-Host and Start-Sleep are replaced by doubles that record
every call to a file. Nothing external runs: no git, no gcloud, no python, no bash.

The packet is built in a temporary directory: a copy of the repository files the lock lists, a release directory with the
baseline, the durable-effects manifest and the two receipts, and the lock, the review, the bindings and the execution
receipt. A test may change any file before the lock is built (to mutate the paste) or after (to break the lock).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core.setup.release import lock as locklib

ROOT = Path(__file__).resolve().parents[3]
COMMIT = "d666ef64cd7a0123456789abcdef0123456789ab"
TREE = "9aaee874e0e06e54d3354ae58bf6e576685b5303"
RID = "rel-d666ef6-01"
A80 = {"f42-agent": "f42-agent-00047-677", "f42-api": "f42-api-00041-lns"}
API_TAG_URL = f"https://{RID}---f42-api-fibxg5ynpq-uc.a.run.app"
FREEZE_HASH = "ab" * 32
DESCRIBE = {"spec": {"template": {"spec": {"containers": [{"env": [{"name": "F42_DATA", "value": "bigquery"}]}]}}}}
CALLER = "jhb.analytics@gmail.com"
INHERITED = "inherited-passcode-should-never-be-used"
TYPED = "typed-passcode-by-albert"

DRIVER = r'''
param([string]$Config)
$ErrorActionPreference = 'Stop'
$cfg = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
$Script:Cfg = $cfg
function Env-Snap { return @{ file_logging = [string]$env:CLOUDSDK_CORE_DISABLE_FILE_LOGGING; bytecode = [string]$env:PYTHONDONTWRITEBYTECODE; locks = [string]$env:GIT_OPTIONAL_LOCKS } }
$Script:ClockMinutes = 0
$Script:WordCalls = @{}
function Log-Call($entry) { ($entry | ConvertTo-Json -Compress -Depth 6) | Add-Content -LiteralPath $Script:Cfg.calls -Encoding utf8 }
function Env-Sha {
    if (Test-Path Env:F42_SMOKE_PASSCODE) {
        $sha = [Security.Cryptography.SHA256]::Create()
        return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($env:F42_SMOKE_PASSCODE))) -replace '-', '').ToLowerInvariant()
    }
    return ''
}
if ($cfg.inherited) { $env:F42_SMOKE_PASSCODE = $cfg.inherited }
$splat = @{ Action = $cfg.action; Lock = $cfg.lock; Review = $cfg.review; Bindings = $cfg.bindings; Receipt = $cfg.receipt; Repo = $cfg.repo; DefinitionsOnly = $true }
if ($cfg.quiet) { $splat.QuietWindowVerifiedAtUtc = $cfg.quiet }
. $cfg.paste @splat
function Get-UtcNow { return [DateTimeOffset]::UtcNow.AddMinutes($Script:ClockMinutes) }

function Read-Native([string]$Exe, [string[]]$Arguments, [string]$InputText) {
    $key = ((@($Exe) + $Arguments) -join ' ')
    Log-Call @{ kind = 'read'; argv = (@($Exe) + $Arguments); input = $InputText; passcode_sha = (Env-Sha); env = (Env-Snap) }
    $later = $Script:Cfg.reads_after.PSObject.Properties[$key]
    if ($null -ne $later -and $Script:RunCount -ge [int]$later.Value.after_runs) { return $later.Value.value }
    $override = $Script:Cfg.reads.PSObject.Properties[$key]
    if ($null -ne $override) { if ($override.Value -is [string] -and $override.Value -eq '!fail') { throw 'Native command failed' }; return $override.Value }
    switch -Regex ($key) {
        '^git rev-parse HEAD$' { return $Script:Cfg.commit }
        '^git rev-parse HEAD\^\{tree\}$' { return $Script:Cfg.tree }
        '^git rev-parse --short=12 HEAD$' { return $Script:Cfg.commit.Substring(0, 12) }
        '^git status --porcelain=v1$' { return '' }
        '^gcloud config list' { return $Script:Cfg.config_json }
        '^gcloud run services describe f42-agent --project ogilvy-trends-v2 --region us-central1 --format=json$' { return $Script:Cfg.describe_json }
        '^py -3.13 -B -m core.setup.release.declared_env_removals --service f42-agent$' { return $Script:Cfg.declared_matches }
        '^py -3.13 core/setup/durable_effects_check.py --check inflight-asks ' {
            if ($Script:Cfg.inflight_refused) { throw 'Native command failed' }
            $evidence = $Arguments[[array]::IndexOf($Arguments, '--evidence') + 1]
            New-Item -ItemType Directory -Path $evidence -Force | Out-Null
            (@{ schema_version = 1; check = 'inflight-asks'; running = [int]$Script:Cfg.inflight_running } | ConvertTo-Json -Compress) | Set-Content -LiteralPath (Join-Path $evidence 'inflight-asks.json') -Encoding utf8
            return ''
        }
        default { throw "unexpected read: $key" }
    }
}

$Script:RunCount = 0
function Run-Logged([string]$Name, [string[]]$Argv, [int[]]$Accept = @(), [string]$WorkDir = '') {
    $Script:RunCount++
    if ($Script:Cfg.inject -and $Script:RunCount -eq [int]$Script:Cfg.inject_at_run) { . ([scriptblock]::Create($Script:Cfg.inject)) }
    Log-Call @{ kind = 'run'; name = $Name; argv = $Argv; passcode_sha = (Env-Sha); workdir = $WorkDir; run_dir_existed = (Test-Path -LiteralPath $Script:RunDir -PathType Container); env = (Env-Snap) }
    $code = 0
    if ($Argv[0] -match '(^|[\\/])tar(\.exe)?$') {
        $target = $Argv[[array]::IndexOf($Argv, '-C') + 1]
        if (-not (Test-Path -LiteralPath $target -PathType Container)) { $code = 2 }
    }
    if ($WorkDir -and -not (Test-Path -LiteralPath $WorkDir -PathType Container)) { $code = 2 }
    $scripted = $Script:Cfg.exits.PSObject.Properties[$Name]
    if ($null -ne $scripted) { $code = [int]$scripted.Value }
    $text = @()
    $joined = $Argv -join ' '
    if ($Argv -contains 'core/api/smoke.py') {
        $url = $Argv[3]
        $text = @($Script:Cfg.smoke_lines | ForEach-Object { $_.Replace('{url}', $url) })
    }
    if ($code -eq 0 -and $joined -like '*bound_readback.py*') {
        $phase = $Argv[[array]::IndexOf($Argv, '--phase') + 1]
        if ($Script:Cfg.no_readback -notcontains $phase) {
            $folder = Join-Path $Script:Paths.root 'readbacks'
            New-Item -ItemType Directory -Path $folder -Force | Out-Null
            $manifestHash = if ($phase -eq 'Freeze') { $Script:Cfg.freeze_hash } else { $null }
            (@{ schema_version = 1; phase = $phase; manifest_sha256 = $manifestHash } | ConvertTo-Json -Compress) | Set-Content -LiteralPath (Join-Path $folder "$phase-01.json") -Encoding utf8
        }
        if ($phase -eq 'Freeze' -and -not $Script:Cfg.no_manifest_on_freeze) {
            $onDisk = if ($Script:Cfg.manifest_hash_on_freeze) { $Script:Cfg.manifest_hash_on_freeze } else { $Script:Cfg.freeze_hash }
            $manifest = @{ schema_version = 1; manifest_sha256 = $onDisk; candidates = @{ 'f42-api' = @{ tag_url = $Script:Cfg.api_tag_url } } }
            ($manifest | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $Script:Paths.manifest -Encoding utf8
            $onDisk | Set-Content -LiteralPath $Script:Paths.sidecar -Encoding utf8
        }
    }
    [IO.File]::WriteAllLines((Join-Path $Script:RunDir ($Name + '.log')), [string[]]$text)
    [string]$code | Set-Content -LiteralPath (Join-Path $Script:RunDir ($Name + '.exit.txt')) -Encoding utf8
    if ($code -ne 0 -and $code -notin $Accept) { throw "$Name exited $code. Stop; the release may be partial. No automatic retry or rollback." }
    return $code
}

if (-not $Script:Cfg.real_console) {
function Test-Interactive { return (-not $Script:Cfg.no_interactive) }

function Read-Typed {
    param([string]$Prompt, [switch]$Secure)
    $AsSecureString = $Secure
    $Script:ClockMinutes += [double]$Script:Cfg.minutes_per_prompt
    Log-Call @{ kind = 'prompt'; secure = [bool]$AsSecureString; prompt = $Prompt; run_dir_existed = ($null -ne (Get-Variable -Scope Script -Name RunDir -ErrorAction SilentlyContinue)) -and (Test-Path -LiteralPath $Script:RunDir -PathType Container); runs_so_far = $Script:RunCount }
    if ($Script:Cfg.no_interactive) { throw 'no interactive host' }
    if ($AsSecureString) {
        if ([string]::IsNullOrEmpty($Script:Cfg.typed)) { return (New-Object Security.SecureString) }
        return (ConvertTo-SecureString $Script:Cfg.typed -AsPlainText -Force)
    }
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
try { Invoke-Release } finally { Log-Call @{ kind = 'env_at_end'; env = (Env-Snap) }; Log-Call @{ kind = 'end'; passcode_sha = (Env-Sha) } }
'''


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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

    def run(self, name):
        return next(c for c in self.runs if c["name"] == name)


class PasteWorld:
    def __init__(self, tmp_path, action="Candidate", *, mutate_paste=None, receipt=None, durable=None, bindings=None, declared_steps=None):
        self.tmp = Path(tmp_path)
        self.action, self.rid = action, RID
        self.repo = self.tmp / "repo"
        for rel in locklib.REPO_FILES:
            if (ROOT / rel).is_file():
                target = self.repo / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / rel, target)
        self.paste = self.repo / "core/setup/release/SERVICES-PASTE.ps1"
        if mutate_paste:
            self.paste.write_text(mutate_paste(self.paste.read_text(encoding="utf-8")), encoding="utf-8", newline="")
        self.release_dir = self.tmp / "releases" / RID
        self.release_dir.mkdir(parents=True)
        self.baseline = self.write(self.tmp / "baseline" / "baseline-A.json", {"schema_version": 1, "kind": "baseline-A",
            "services": {n: {"serving": {"name": r}} for n, r in A80.items()}})
        self.durable = self.write(self.tmp / "durable.json", durable or {"schema_version": 1, "effects": [{"apply_kind": "tag_add"}, {"apply_kind": "traffic_pin"}]})
        self.compat = self.write(self.release_dir / "compat-receipt.json", {"schema_version": 1, "verdict": "pass"})
        self.old = self.write(self.release_dir / "old-reader-receipt.json", {"schema_version": 1, "verdict": "pass"})
        self.bindings = self.write(self.tmp / "bindings.json", {
            "schema_version": 1, "mode": "services-only", "status": "BOUND_FOR_INDEPENDENT_REVIEW", "release_id": RID, "target": COMMIT, "tree": TREE,
            "callerAccount": CALLER, "releaseDir": str(self.release_dir), "baselinePath": str(self.baseline), "durableManifestPath": str(self.durable),
            **(bindings or {})})
        self.lock = self.build_lock()
        self.review = self.write(self.tmp / "review.json", locklib.review_for(self.lock, COMMIT))
        self.receipt = self.write(self.tmp / "receipt.json", {
            "schema_version": 1, "release_id": RID, "target": COMMIT, "tree": TREE, "declared_steps": declared_steps or ["Candidate", "Promote", "Rollback", "Retire"],
            "later_explicit_user_instruction": True, "quiet_window_confirmed": True, "no_new_manual_starts_until_execution_ends": True,
            "single_T1_smoke_authorized": True, "max_live_asks": 1, **(receipt or {})})
        self.paths = {"manifest": self.release_dir / "release-manifest.json", "sidecar": self.release_dir / "release-manifest.sha256",
                      "smoke": self.release_dir / "smoke-receipt.json"}

    @staticmethod
    def write(path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2), encoding="utf-8", newline="\n")
        return path

    def build_lock(self):
        bound = {"bindings": self.bindings, "baseline": self.baseline, "durable_manifest": self.durable, "compat_receipt": self.compat, "old_reader_receipt": self.old}
        lock = locklib.build_lock(self.repo, RID, bound)
        return self.write(self.tmp / "lock.json", lock)

    def relock(self):
        """Rebuild the lock and the review after a deliberate change to a locked file."""
        self.lock = self.build_lock()
        self.write(self.review, locklib.review_for(self.lock, COMMIT))

    # the release directory as earlier actions leave it
    def readback(self, phase, manifest_sha256=None):
        folder = self.release_dir / "readbacks"
        folder.mkdir(exist_ok=True)
        self.write(folder / f"{phase}-01.json", {"schema_version": 1, "phase": phase, "manifest_sha256": manifest_sha256})

    def frozen_manifest(self, manifest_hash=FREEZE_HASH, sidecar=None):
        self.write(self.paths["manifest"], {"schema_version": 1, "manifest_sha256": manifest_hash, "candidates": {"f42-api": {"tag_url": API_TAG_URL}}})
        self.paths["sidecar"].write_text((sidecar or manifest_hash) + "\n", encoding="utf-8")
        self.readback("Freeze", FREEZE_HASH)

    def after_smoke(self, with_receipt=True):
        self.frozen_manifest()
        self.readback("AfterSmoke")
        if with_receipt:
            self.write(self.paths["smoke"], {"schema_version": 1})

    # running
    def run(self, *, exits=None, reads=None, smoke_lines=None, typed=TYPED, interactive=True, inherited=None, quiet="now", no_readback=(),
            no_manifest_on_freeze=False, action=None, extra=None):
        calls = self.tmp / "calls.jsonl"
        calls.write_text("", encoding="utf-8")
        quiet_value = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)).isoformat() if quiet == "now" else quiet
        config = {
            "paste": str(self.paste), "action": action or self.action, "lock": str(self.lock), "review": str(self.review), "bindings": str(self.bindings),
            "receipt": str(self.receipt), "repo": str(self.repo), "calls": str(calls), "quiet": quiet_value, "commit": COMMIT, "tree": TREE,
            "config_json": json.dumps({"core": {"account": CALLER, "project": "ogilvy-trends-v2"}}), "exits": exits or {}, "reads": reads or {},
            "smoke_lines": smoke_lines if smoke_lines is not None else ["PASS health: ok", "6 of 6 checks passed", "SMOKE-RESULT base={url} checks=6 passed=6"],
            "typed": typed, "no_interactive": not interactive, "inherited": inherited, "freeze_hash": FREEZE_HASH, "api_tag_url": API_TAG_URL,
            "no_readback": list(no_readback), "no_manifest_on_freeze": no_manifest_on_freeze, "manifest_hash_on_freeze": None, "reads_after": {},
            "words": {}, "describe_json": json.dumps(DESCRIBE), "declared_matches": "", "inflight_running": 0, "inflight_refused": False,
            "minutes_per_prompt": 0, "aliases": [], "real_console": False, "attack": "", "inject": "", "inject_at_run": 0,
            **(extra or {})}
        self.write(self.tmp / "config.json", config)
        driver = self.tmp / "driver.ps1"
        driver.write_text(DRIVER, encoding="utf-8", newline="\n")
        clean = {k: v for k, v in os.environ.items() if k not in ("CLOUDSDK_CORE_DISABLE_FILE_LOGGING", "PYTHONDONTWRITEBYTECODE", "GIT_OPTIONAL_LOCKS")}
        proc = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File", str(driver), "-Config", str(self.tmp / "config.json")],
                              capture_output=True, stdin=subprocess.DEVNULL, encoding="utf-8", timeout=120, env=clean)
        entries = [json.loads(line) for line in calls.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        return Result(proc.returncode, proc.stdout, proc.stderr, entries)
