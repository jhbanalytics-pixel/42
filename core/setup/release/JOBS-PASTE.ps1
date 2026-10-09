# Release paste for the jobs image release, Release B (W8-REL-B v2.1 3.4, 3.10). Albert runs every command; this file prepares them.
# One action per invocation: JobsCandidate, JobsUpdate or JobsRollback. It makes no external call until the lock, the review, the
# bindings and the execution receipt have all been checked, and then asks, at a console that is really there and before it writes
# anything, for what only Albert can give: DEPLOY for JobsCandidate, IDLE then DEPLOY for JobsUpdate. JobsRollback asks nothing,
# because a rollback never waits. Release B asks for no secret: it runs no live Ask and starts no job.
# The typed words are Albert's and nothing in the session that started this paste may stand in for them. Use a freshly opened console
# for every action and start the paste as its own process with pwsh -NoProfile -File .\core\setup\release\JOBS-PASTE.ps1 and its
# parameters, never by dot sourcing it into a session you have used. A fresh process carries no alias, function or command lookup hook
# that could stand in for a typed word. The paste also refuses such shadows itself (the resolution check below), but a check made in a
# session the caller controls is the second line of defence, not the first.
# The decisions between two commands (the second quiet snapshot, the window before each update, the readback after each one, the
# automatic restore inside the chain group) are made by core/setup/release/jobs_run.py, which this file calls and which judges every
# command it issues against the positive list. This file runs the other commands itself.
param(
    [Parameter(Mandatory)][ValidateSet('JobsCandidate', 'JobsUpdate', 'JobsRollback')][string]$Action,
    [Parameter(Mandatory)][string]$Lock,
    [Parameter(Mandatory)][string]$Review,
    [Parameter(Mandatory)][string]$Bindings,
    [Parameter(Mandatory)][string]$Receipt,
    [string]$Repo = (Get-Location).Path,
    [switch]$DefinitionsOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Script:Helper = 'core/setup/release/bound_readback.py'
$Script:Runner = 'core/setup/release/jobs_run.py'
$Script:ReleasePattern = '^rel-[0-9a-f]{7}-[0-9]{2}$'
$Script:Retries = 3
$Script:RetrySeconds = 10
$Script:SnapshotMinutes = 15
$Script:Tar = if ($env:SystemRoot) { Join-Path $env:SystemRoot 'System32\tar.exe' } else { 'tar' }
$Script:Steps = @('JobsCandidate', 'JobsUpdate', 'JobsRollback')
$Script:TestDoubles = @()
$Script:AllowTestDoubles = $DefinitionsOnly.IsPresent
$Script:NativeNames = @('gcloud', 'git', 'py', 'bash')
$Script:CheckedNames = $null

# The resolution check below is a copy of the one in SERVICES-PASTE.ps1, pinned equal to it by a test. Every command name this paste
# invokes is taken from its own syntax tree and resolved the way the engine would resolve it. A name must resolve to a function of this
# paste or to a cmdlet of a Microsoft.PowerShell module in the PowerShell install folder; never to an alias, to a function of the
# caller, to a cmdlet from elsewhere, or be written with a module prefix. The programs it runs must resolve to a program or a script
# file. A command lookup hook refuses the run too. Only a definitions-only load, which is how the test driver reaches the functions,
# may name functions of its own in $Script:TestDoubles. The check calls no PowerShell command, so nothing can replace any part of it. A
# caller can still make an alias after a check has run, so it runs again at the top of Invoke-Release, inside Confirm-Action and
# Confirm-Update, before each prompt, and before each step.
$Script:ResolutionCheck = {
    $invoker = $ExecutionContext.InvokeCommand
    if ($null -ne $invoker.PreCommandLookupAction) { throw 'NOT EXECUTABLE: a command lookup hook is set in this session (PreCommandLookupAction). Start the paste from a freshly opened console with pwsh -NoProfile -File.' }
    if ($null -ne $invoker.PostCommandLookupAction) { throw 'NOT EXECUTABLE: a command lookup hook is set in this session (PostCommandLookupAction). Start the paste from a freshly opened console with pwsh -NoProfile -File.' }
    if ($null -ne $invoker.CommandNotFoundAction) { throw 'NOT EXECUTABLE: a command lookup hook is set in this session (CommandNotFoundAction). Start the paste from a freshly opened console with pwsh -NoProfile -File.' }
    $tree = $Script:ResolutionCheck.Ast
    while ($null -ne $tree.Parent) { $tree = $tree.Parent }
    $pasteFile = $tree.Extent.File
    if ($null -eq $Script:CheckedNames) {
        $names = [System.Collections.Generic.List[string]]::new()
        $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
        $nodes = $tree.FindAll({ param($node) $node -is [System.Management.Automation.Language.CommandAst] -or $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
        foreach ($node in $nodes) {
            $name = if ($node -is [System.Management.Automation.Language.FunctionDefinitionAst]) { $node.Name } else { $node.GetCommandName() }
            if ($null -ne $name -and $seen.Add($name)) { $names.Add($name) }
        }
        foreach ($name in @('Read-Host', 'Get-Alias')) { if ($seen.Add($name)) { $names.Add($name) } }
        $Script:CheckedNames = $names.ToArray()
    }
    $psFolder = [System.IO.Path]::GetDirectoryName([System.Management.Automation.PSObject].Assembly.Location)
    foreach ($name in $Script:CheckedNames) {
        $found = if ($name.Contains('\')) { $null } else { $invoker.GetCommand($name, [System.Management.Automation.CommandTypes]::All) }
        $ok = $false
        if ($found -is [System.Management.Automation.FunctionInfo]) {
            $file = $found.ScriptBlock.File
            $ok = ($null -ne $file -and [string]::Equals($file, $pasteFile, [System.StringComparison]::OrdinalIgnoreCase)) -or ($Script:AllowTestDoubles -and $Script:TestDoubles -contains $name)
        } elseif ($found -is [System.Management.Automation.CmdletInfo]) {
            $assembly = $found.ImplementingType.Assembly
            $ok = $found.ModuleName.StartsWith('Microsoft.PowerShell.', [System.StringComparison]::OrdinalIgnoreCase) -and ($assembly.GlobalAssemblyCache -or ($assembly.Location.Length -gt 0 -and [System.IO.Path]::GetDirectoryName($assembly.Location) -eq $psFolder))
        }
        if (-not $ok) {
            $kind = if ($null -eq $found) { 'nothing' } elseif ($name.Contains('\')) { 'a qualified name' } else { [string]$found.CommandType }
            throw "NOT EXECUTABLE: the command '$name' does not resolve to a function of this paste or to the expected cmdlet ($kind). Start the paste from a freshly opened console with pwsh -NoProfile -File."
        }
    }
    foreach ($name in $Script:NativeNames) {
        $found = $invoker.GetCommand($name, [System.Management.Automation.CommandTypes]::All)
        if ($null -ne $found -and $found -isnot [System.Management.Automation.ApplicationInfo] -and $found -isnot [System.Management.Automation.ExternalScriptInfo]) {
            throw "NOT EXECUTABLE: the command '$name' does not resolve to a function of this paste or to the expected program ($($found.CommandType)). Start the paste from a freshly opened console with pwsh -NoProfile -File."
        }
    }
    foreach ($qualified in $invoker.GetCommands('*\*', [System.Management.Automation.CommandTypes]'Alias,Function', $true)) {
        $tail = $qualified.Name.Substring($qualified.Name.LastIndexOf('\') + 1)
        if ($tail.Length -gt 0 -and ($Script:CheckedNames -contains $tail -or $Script:NativeNames -contains $tail)) {
            throw "NOT EXECUTABLE: the command '$($qualified.Name)' is an alias or a function named with a module prefix over a command this paste invokes. Start the paste from a freshly opened console with pwsh -NoProfile -File."
        }
    }
}

# Text files of the repository are hashed with a CRLF read as LF, as lock.py does, so a Windows checkout and a Linux one agree.
function Get-Sha([string]$Path, [switch]$Text) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "NOT EXECUTABLE: a bound file is missing: $(Split-Path -Leaf $Path)" }
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($Text) {
        $latin = [Text.Encoding]::GetEncoding('iso-8859-1')
        $bytes = $latin.GetBytes($latin.GetString($bytes).Replace("`r`n", "`n"))
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant() } finally { $sha.Dispose() }
}

function Read-JsonFile([string]$Path, [string]$What) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "NOT EXECUTABLE: $What is missing." }
    try { return (Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json) } catch { throw "NOT EXECUTABLE: $What is not readable JSON." }
}

function Assert-Version($Value, [string]$What) {
    $property = $Value.PSObject.Properties['schema_version']
    $known = ($null -ne $property) -and (($property.Value -is [int]) -or ($property.Value -is [long])) -and ($property.Value -eq 1)
    if (-not $known) { throw "NOT EXECUTABLE: $What has no known schema_version." }
}

# The durable files of the release live in its release directory, by name, unless the bindings name them; a named path must still
# lie inside the release directory, so nothing is read or written from the evidence folder or anywhere else.
function Resolve-Paths($Bound) {
    $root = [IO.Path]::GetFullPath($Bound.releaseDir)
    $paths = @{ root = $root; baseline = $Bound.baselinePath }
    foreach ($key in @('baselineChainPath', 'durableManifestPath', 'dryRunReceiptPath')) {
        $full = [IO.Path]::GetFullPath([string]$Bound.$key)
        if (-not $full.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "NOT EXECUTABLE: a durable path is outside the release directory: $key"
        }
        $paths[$key] = $full
    }
    return $paths
}

# 1. Everything below runs before the first external call: the lock against the review, every locked file against the lock, the
# bindings, and the execution receipt.
function Test-Packet {
    $lockFile = Read-JsonFile $Lock 'the packet lock'
    Assert-Version $lockFile 'the packet lock'
    $review = Read-JsonFile $Review 'the independent review'
    Assert-Version $review 'the independent review'
    $bound = Read-JsonFile $Bindings 'the bindings'
    Assert-Version $bound 'the bindings'
    if ($review.verdict -ne 'ACCEPT' -or $review.lock_sha256 -ne (Get-Sha $Lock) -or $review.target -ne $bound.target) {
        throw 'NOT EXECUTABLE: the independent review does not bind this lock and target.'
    }
    if ($bound.status -ne 'BOUND_FOR_INDEPENDENT_REVIEW' -or $bound.mode -ne 'jobs' -or $bound.release_id -ne $lockFile.release_id) {
        throw 'NOT EXECUTABLE: the bindings are not reviewed for this jobs release.'
    }
    if ($bound.release_id -notmatch $Script:ReleasePattern) { throw 'NOT EXECUTABLE: the release id is not rel-<sha7>-<nn>.' }
    foreach ($entry in $lockFile.repo_files.PSObject.Properties) {
        if ((Get-Sha (Join-Path $Repo $entry.Name) -Text) -ne $entry.Value) { throw "NOT EXECUTABLE: a locked file differs from the lock: $($entry.Name)" }
    }
    if ((Get-Sha $PSCommandPath -Text) -ne $lockFile.repo_files.'core/setup/release/JOBS-PASTE.ps1') { throw 'NOT EXECUTABLE: this paste differs from the lock.' }
    $Script:Paths = Resolve-Paths $bound
    $boundFiles = @{ bindings = $Bindings; baseline = $Script:Paths.baseline; durable_manifest = $Script:Paths.durableManifestPath
                     baseline_chain = $Script:Paths.baselineChainPath; dry_run_receipt = $Script:Paths.dryRunReceiptPath }
    foreach ($entry in $lockFile.bound_files.PSObject.Properties) {
        if ((Get-Sha $boundFiles[$entry.Name]) -ne $entry.Value) { throw "NOT EXECUTABLE: a bound file differs from the lock: $($entry.Name)" }
    }
    $Script:LockData = $lockFile
    $Script:Bound = $bound
}

# A receipt without schema_version, release_id or mode jobs is refused before any read.
function Test-Receipt {
    $receipt = Read-JsonFile $Receipt 'the execution receipt'
    Assert-Version $receipt 'the execution receipt'
    if ($receipt.PSObject.Properties['release_id'] -eq $null -or $receipt.PSObject.Properties['mode'] -eq $null -or $receipt.mode -ne 'jobs') {
        throw 'NOT EXECUTABLE: the receipt names no release id or is not for mode jobs.'
    }
    if ($receipt.release_id -ne $Script:Bound.release_id -or $receipt.target -ne $Script:Bound.target) { throw 'NOT EXECUTABLE: the receipt is for another release.' }
    $steps = $receipt.PSObject.Properties['declared_steps']
    if ($null -eq $steps -or $steps.Value -isnot [Collections.IList] -or $Action -notin @($steps.Value) -or @($steps.Value | Where-Object { $_ -notin $Script:Steps }).Count -ne 0) {
        throw 'NOT EXECUTABLE: the action is not among the receipt''s declared steps.'
    }
    foreach ($name in @('later_explicit_user_instruction', 'quiet_window_confirmed', 'no_new_manual_starts_until_execution_ends')) {
        $property = $receipt.PSObject.Properties[$name]
        if ($null -eq $property -or $property.Value -isnot [bool] -or $property.Value -ne $true) { throw 'NOT EXECUTABLE: authority confirmations must be boolean true.' }
    }
    $Script:ReceiptValue = $receipt
}

# 2. Commands. Every external invocation goes through Read-Native (a read) or Run-Logged (a step with a log and an exit file).
function Read-Native([string]$Exe, [string[]]$Arguments, [string]$InputText) {
    $result = if ($PSBoundParameters.ContainsKey('InputText')) { $InputText | & $Exe @Arguments } else { & $Exe @Arguments }
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw "Native command failed with exit $code. Stop and retain its error." }
    return ($result -join "`n")
}

function Read-Cloud([string[]]$Arguments) {
    return ((Read-Native -Exe gcloud -Arguments $Arguments) | ConvertFrom-Json)
}

function Run-Logged([string]$Name, [string[]]$Argv, [int[]]$Accept = @(), [string]$WorkDir = '') {
    $exe, $rest = $Argv
    $log = Join-Path $Script:RunDir ($Name + '.log')
    if ($WorkDir) { Push-Location -LiteralPath $WorkDir }
    try {
        & $exe @rest 2>&1 | Tee-Object -FilePath $log | Out-Host
        $code = $LASTEXITCODE
    } finally {
        if ($WorkDir) { Pop-Location }
    }
    if (-not (Test-Path -LiteralPath $log)) { New-Item -ItemType File -Path $log | Out-Null }
    [string]$code | Set-Content -LiteralPath (Join-Path $Script:RunDir ($Name + '.exit.txt')) -Encoding utf8
    if ($code -ne 0 -and $code -notin $Accept) { throw "$Name exited $code. Stop; the release may be partial. The declared branch is printed above." }
    return $code
}

function Assert-Source {
    $target = $Script:Bound.target
    if ((Read-Native -Exe git -Arguments @('rev-parse', 'HEAD')) -ne $target) { throw 'Unexpected source commit.' }
    if ((Read-Native -Exe git -Arguments @('rev-parse', 'HEAD^{tree}')) -ne $Script:Bound.tree) { throw 'Unexpected source tree.' }
    if (Read-Native -Exe git -Arguments @('status', '--porcelain=v1')) { throw 'The product checkout is not clean.' }
}

function Assert-Identity {
    $cfg = Read-Cloud -Arguments @('config', 'list', '--format=json(core.account,core.project,auth.impersonate_service_account)')
    if ($cfg.core.project -ne 'ogilvy-trends-v2' -or $cfg.core.account -ne $Script:Bound.callerAccount) { throw 'Unexpected caller or project.' }
    if ($cfg.PSObject.Properties.Name -contains 'auth' -and $cfg.auth.PSObject.Properties.Name -contains 'impersonate_service_account' -and $cfg.auth.impersonate_service_account) {
        throw 'CLI impersonation differs from the reviewed identity.'
    }
}

# JobsCandidate and JobsUpdate assert the checkout before every command; JobsRollback records it as an observation, because a moved
# or dirty checkout must not stop a rollback.
function Step([string]$Name, [string[]]$Argv, [int[]]$Accept = @(), [string]$WorkDir = '') {
    & $Script:ResolutionCheck
    if ($Action -in @('JobsCandidate', 'JobsUpdate')) { Assert-Source }
    return (Run-Logged -Name $Name -Argv $Argv -Accept $Accept -WorkDir $WorkDir)
}

function Note-Source {
    try { Assert-Source; $state = 'clean' } catch { $state = 'differs' }
    "source: $state" | Set-Content -LiteralPath (Join-Path $Script:RunDir 'source-observation.txt') -Encoding utf8
}

function Get-Readbacks([string]$Phase) {
    $folder = Join-Path $Script:Paths.root 'readbacks'
    return @(Get-ChildItem -LiteralPath $folder -Filter ($Phase + '-*.json') -ErrorAction SilentlyContinue | Sort-Object Name)
}

# A readback that could not complete (exit 3) is retried three times, ten seconds apart. Exit 1 is a stop and is never retried.
function Invoke-Helper([string]$Phase) {
    $argv = @('py', '-3.13', $Script:Helper, '--mode', 'jobs', '--phase', $Phase, '--bindings', $Bindings, '--evidence', $Script:RunDir)
    $attempt = 0
    while ($true) {
        $code = Step -Name ("helper-$Phase-$attempt") -Argv $argv -Accept @(3)
        if ($code -eq 0) { break }
        if ($attempt -ge $Script:Retries) { throw "The $Phase readback could not complete after $Script:Retries retries. Re-run the action within the deadline." }
        $attempt++
        Start-Sleep -Seconds $Script:RetrySeconds
    }
    $found = @(Get-Readbacks $Phase)
    if ($found.Count -eq 0) { throw "The $Phase readback file does not exist; the paste checks the file, not only the output." }
    return $found
}

function Invoke-Runner([string]$Name, [string]$Verb) {
    return (Step -Name $Name -Argv @('py', '-3.13', $Script:Runner, $Verb, '--bindings', $Bindings, '--evidence', $Script:RunDir))
}

# The image build and FreezeJobs belong to the build requirements (JB-01 to JB-03). Until they supply the build, JobsCandidate stops here
# and says so; nothing written before this point is a release step.
function Invoke-JobsBuild {
    throw 'NOT BUILT: the jobs image build is not part of this tooling yet (JB-01 to JB-03). Nothing past BeforeAnyWrite was written.'
}

# 3. The three actions.
function Invoke-JobsCandidate {
    Invoke-Helper 'BeforeAnyWrite' | Out-Null
    $archive = Join-Path $Script:RunDir 'source.tar'
    $extract = Join-Path $Script:RunDir 'source'
    Step -Name 'archive' -Argv @('git', 'archive', '--format=tar', "--output=$archive", $Script:Bound.target) | Out-Null
    New-Item -ItemType Directory -Path $extract | Out-Null
    Step -Name 'extract' -Argv @($Script:Tar, '-xf', $archive, '-C', $extract) | Out-Null
    Invoke-JobsBuild
    Invoke-Helper 'FreezeJobs' | Out-Null
    # The validator runs on the real durable manifest before anything is applied.
    Step -Name 'durable-validate' -Argv @('py', '-3.13', 'core/setup/durable_effects_check.py', '--manifest', $Script:Paths.durableManifestPath, '--check', 'validate', '--evidence', $Script:RunDir) | Out-Null
    # The quiet snapshot and the window come before the schema apply, because an ALTER on a table a running detect writes could overlap it.
    Invoke-Runner 'snapshot' 'snapshot' | Out-Null
    $durable = Read-JsonFile $Script:Paths.durableManifestPath 'the durable-effects manifest'
    foreach ($effect in @($durable.effects)) {
        if ($effect.apply_kind -notin @('schema', 'job_image', 'code')) { throw 'An effect names an apply kind outside the paste''s allowlist.' }
    }
    if (@($durable.effects | Where-Object { $_.apply_kind -eq 'schema' }).Count -gt 0) {
        Step -Name 'schema-apply' -Argv @('py', '-3.13', '-m', 'core.schema.apply', '--apply') | Out-Null
    }
    Step -Name 'durable-readbacks' -Argv @('py', '-3.13', 'core/setup/durable_effects_check.py', '--manifest', $Script:Paths.durableManifestPath, '--check', 'readbacks', '--evidence', $Script:RunDir) | Out-Null
}

function Invoke-JobsUpdate {
    $readback = Invoke-Helper 'BeforeJobsUpdate'
    Confirm-Update
    Test-SnapshotAge $readback
    Invoke-Runner 'jobs-run-update' 'update' | Out-Null
    Invoke-Helper 'AfterJobsUpdate' | Out-Null
}

function Invoke-JobsRollback {
    Note-Source
    Assert-Identity
    Invoke-Helper 'BeforeJobsRollback' | Out-Null
    Invoke-Runner 'jobs-run-rollback' 'rollback' | Out-Null
    Invoke-Helper 'AfterJobsRollback' | Out-Null
}

# 3a. What only Albert can give, asked at a console that is really there: a redirected stdin or a non-interactive host is refused with
# no fallback, because the prompt cmdlet would otherwise take the answer from a pipe. The receipt has already been validated, so a receipt
# alone never starts a release. JobsRollback asks for nothing.
function Test-Interactive { return ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) }

function Assert-Interactive {
    if (-not (Test-Interactive)) { throw 'NOT INTERACTIVE: DEPLOY and IDLE are typed at a console. This run is not at one, and there is no fallback.' }
}

# Read-Host is called by its plain name. A function or an alias of that name made by whoever started the paste would run in its place,
# so the resolution check runs first and refuses a session in which Read-Host is anything but the cmdlet. Read-Typed is the one place a
# prompt is made.
function Read-Typed([string]$Prompt) {
    & $Script:ResolutionCheck
    return (Read-Host -Prompt $Prompt)
}

function Get-UtcNow { return [DateTimeOffset]::UtcNow }

function Read-Word([string]$Word) {
    & $Script:ResolutionCheck
    try { $typed = Read-Typed "Type $Word to continue" } catch { throw "NOT INTERACTIVE: the prompt for $Word could not be shown." }
    if ($typed -cne $Word) { throw "NOT CONFIRMED: $Word was not typed exactly. Nothing was written." }
}

# JobsUpdate: IDLE then DEPLOY, after the quiet snapshot the helper took, so the operator reads it first.
function Confirm-Update {
    & $Script:ResolutionCheck
    Assert-Interactive
    Read-Word 'IDLE'
    Read-Word 'DEPLOY'
}

# The snapshot must be under 15 minutes old when DEPLOY is typed. The runner reads it again after the words and compares.
function Test-SnapshotAge($Readbacks) {
    $latest = Get-Content -LiteralPath $Readbacks[-1].FullName -Raw | ConvertFrom-Json
    $property = $latest.PSObject.Properties['quiet_snapshot']
    if ($null -eq $property -or $null -eq $property.Value.taken_at) { throw 'NOT CONFIRMED: the readback holds no quiet snapshot.' }
    $age = (Get-UtcNow) - [DateTimeOffset]::Parse($property.Value.taken_at)
    if ($age.TotalMinutes -gt $Script:SnapshotMinutes -or $age.TotalMinutes -lt -1) { throw 'NOT CONFIRMED: the quiet snapshot is older than 15 minutes.' }
}

function Confirm-Action {
    & $Script:ResolutionCheck
    if ($Action -eq 'JobsRollback') { return }
    Assert-Interactive
    Assert-Source
    Assert-Identity
    if ($Action -eq 'JobsCandidate') { Read-Word 'DEPLOY' }
}

function Invoke-Release {
    & $Script:ResolutionCheck
    Test-Packet
    Test-Receipt
    # Every child inherits these: gcloud keeps no file log of its arguments, Python writes no bytecode into the checkout, and git status
    # takes no index lock. They are put back at the end of the run.
    $childEnv = [ordered]@{ CLOUDSDK_CORE_DISABLE_FILE_LOGGING = '1'; PYTHONDONTWRITEBYTECODE = '1'; GIT_OPTIONAL_LOCKS = '0' }
    $savedEnv = @{}
    foreach ($name in $childEnv.Keys) { $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name) }
    try {
        foreach ($name in $childEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $childEnv[$name]) }
        Set-Location -LiteralPath $Repo
        Confirm-Action
        $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmss')
        $Script:RunDir = Join-Path $Script:Paths.root (Join-Path 'runs' "$stamp-$Action")
        New-Item -ItemType Directory -Path $Script:RunDir -Force | Out-Null
        switch ($Action) {
            'JobsCandidate' { Invoke-JobsCandidate }
            'JobsUpdate' { Invoke-JobsUpdate }
            'JobsRollback' { Invoke-JobsRollback }
        }
    } finally {
        foreach ($name in $childEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $savedEnv[$name]) }
    }
}

if (-not $DefinitionsOnly) { & $Script:ResolutionCheck; Invoke-Release }
