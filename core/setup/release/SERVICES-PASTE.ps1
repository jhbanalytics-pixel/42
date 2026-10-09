# Release paste for a services-only release (W8-REL 2.11, 3.1, 3.5). Albert runs every command; this file prepares them.
# One action per invocation: Candidate, Promote, Rollback or Retire. It makes no external call until the lock, the review,
# the bindings and the execution receipt have all been checked. Candidate and Promote then ask for what only Albert can
# give, at a console that is really there, before they write anything: DEPLOY for both, IDLE for Promote, and for Candidate
# the smoke passcode at a private prompt that never echoes it. There is no passcode parameter and an inherited value is
# never used. Rollback and Retire ask for nothing, because a rollback never waits.
param(
    [Parameter(Mandatory)][ValidateSet('Candidate', 'Promote', 'Rollback', 'Retire')][string]$Action,
    [Parameter(Mandatory)][string]$Lock,
    [Parameter(Mandatory)][string]$Review,
    [Parameter(Mandatory)][string]$Bindings,
    [Parameter(Mandatory)][string]$Receipt,
    [string]$Repo = (Get-Location).Path,
    [string]$QuietWindowVerifiedAtUtc,
    [switch]$DefinitionsOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Script:Helper = 'core/setup/release/bound_readback.py'
$Script:ReleasePattern = '^rel-[0-9a-f]{7}-[0-9]{2}$'
$Script:Retries = 3
$Script:SmokeSecret = $null
$Script:Tar = if ($env:SystemRoot) { Join-Path $env:SystemRoot 'System32\tar.exe' } else { 'tar' }
$Script:RetrySeconds = 10

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

# The durable files of the release live in its release directory, by name, unless the bindings name them; a named path must
# still lie inside the release directory (HR-54), so nothing is read or written from the evidence folder or anywhere else.
function Resolve-Paths($Bound) {
    $root = [IO.Path]::GetFullPath($Bound.releaseDir)
    $names = @{ manifest = @('manifestPath', 'release-manifest.json'); sidecar = @('sidecarPath', 'release-manifest.sha256')
                smokeReceipt = @('smokeReceiptPath', 'smoke-receipt.json'); compat = @('compatReceiptPath', 'compat-receipt.json')
                oldReader = @('oldReaderReceiptPath', 'old-reader-receipt.json') }
    $paths = @{ root = $root; baseline = $Bound.baselinePath; durable = $Bound.durableManifestPath }
    foreach ($key in $names.Keys) {
        $property = $Bound.PSObject.Properties[$names[$key][0]]
        $candidate = if ($null -ne $property) { [string]$property.Value } else { Join-Path $root $names[$key][1] }
        $full = [IO.Path]::GetFullPath($candidate)
        if (-not $full.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "NOT EXECUTABLE: a durable path is outside the release directory: $key"
        }
        $paths[$key] = $full
    }
    return $paths
}

# 1. Everything below runs before the first external call: the lock against the review, every locked file against the
# lock, the bindings, and the execution receipt.
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
    if ($bound.status -ne 'BOUND_FOR_INDEPENDENT_REVIEW' -or $bound.mode -ne 'services-only' -or $bound.release_id -ne $lockFile.release_id) {
        throw 'NOT EXECUTABLE: the bindings are not reviewed for this release.'
    }
    if ($bound.release_id -notmatch $Script:ReleasePattern) { throw 'NOT EXECUTABLE: the release id is not rel-<sha7>-<nn>.' }
    foreach ($entry in $lockFile.repo_files.PSObject.Properties) {
        if ((Get-Sha (Join-Path $Repo $entry.Name) -Text) -ne $entry.Value) { throw "NOT EXECUTABLE: a locked file differs from the lock: $($entry.Name)" }
    }
    if ((Get-Sha $PSCommandPath -Text) -ne $lockFile.repo_files.'core/setup/release/SERVICES-PASTE.ps1') { throw 'NOT EXECUTABLE: this paste differs from the lock.' }
    $Script:Paths = Resolve-Paths $bound
    $boundFiles = @{ bindings = $Bindings; baseline = $Script:Paths.baseline; durable_manifest = $Script:Paths.durable
                     compat_receipt = $Script:Paths.compat; old_reader_receipt = $Script:Paths.oldReader }
    foreach ($entry in $lockFile.bound_files.PSObject.Properties) {
        if ((Get-Sha $boundFiles[$entry.Name]) -ne $entry.Value) { throw "NOT EXECUTABLE: a bound file differs from the lock: $($entry.Name)" }
    }
    if ($Script:Stale | Where-Object { (Get-Content -LiteralPath $Bindings -Raw) -match [regex]::Escape($_) }) {
        throw 'NOT EXECUTABLE: the bindings carry a pre-a80 rollback target.'
    }
    if ((Get-Content -LiteralPath $Bindings -Raw) -match '"rollback(Revision|ImageDigest)"') {
        throw 'NOT EXECUTABLE: the bindings carry the a80 rollback keys; Release A rebuilds its targets from a fresh baseline.'
    }
    $Script:LockData = $lockFile
    $Script:Bound = $bound
}
$Script:Stale = @('f42-agent-00046-wks', 'f42-api-00040-cw5', 'sha256:6c7b718b', 'sha256:17ad022e')

function Test-Receipt {
    $receipt = Read-JsonFile $Receipt 'the execution receipt'
    Assert-Version $receipt 'the execution receipt'
    if ($receipt.release_id -ne $Script:Bound.release_id -or $receipt.target -ne $Script:Bound.target) { throw 'NOT EXECUTABLE: the receipt is for another release.' }
    $steps = $receipt.PSObject.Properties['declared_steps']
    if ($null -eq $steps -or $steps.Value -isnot [Collections.IList] -or $Action -notin @($steps.Value) -or @($steps.Value | Where-Object { $_ -notin @('Candidate', 'Promote', 'Rollback', 'Retire') }).Count -ne 0) {
        throw 'NOT EXECUTABLE: the action is not among the receipt''s declared steps.'
    }
    $names = @('later_explicit_user_instruction', 'quiet_window_confirmed', 'no_new_manual_starts_until_execution_ends')
    if ($Action -eq 'Candidate') { $names += 'single_T1_smoke_authorized' }
    foreach ($name in $names) {
        $property = $receipt.PSObject.Properties[$name]
        if ($null -eq $property -or $property.Value -isnot [bool] -or $property.Value -ne $true) { throw 'NOT EXECUTABLE: authority and smoke confirmations must be boolean true.' }
    }
    if ($Action -eq 'Candidate') {
        $asks = $receipt.PSObject.Properties['max_live_asks']
        if ($null -eq $asks -or ($asks.Value -isnot [int] -and $asks.Value -isnot [long]) -or $asks.Value -ne 1) { throw 'NOT EXECUTABLE: the receipt does not authorise exactly one live Ask.' }
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
    if ($code -ne 0 -and $code -notin $Accept) { throw "$Name exited $code. Stop; the release may be partial. No automatic retry or rollback." }
    return $code
}

function Assert-Source {
    $target = $Script:Bound.target
    if ((Read-Native -Exe git -Arguments @('rev-parse', 'HEAD')) -ne $target) { throw 'Unexpected source commit.' }
    if ((Read-Native -Exe git -Arguments @('rev-parse', 'HEAD^{tree}')) -ne $Script:Bound.tree) { throw 'Unexpected source tree.' }
    if ((Read-Native -Exe git -Arguments @('rev-parse', '--short=12', 'HEAD')) -ne $target.Substring(0, 12)) { throw 'The short commit changed.' }
    if (Read-Native -Exe git -Arguments @('status', '--porcelain=v1')) { throw 'The product checkout is not clean.' }
}

function Assert-Identity {
    $cfg = Read-Cloud -Arguments @('config', 'list', '--format=json(core.account,core.project,auth.impersonate_service_account)')
    if ($cfg.core.project -ne 'ogilvy-trends-v2' -or $cfg.core.account -ne $Script:Bound.callerAccount) { throw 'Unexpected caller or project.' }
    if ($cfg.PSObject.Properties.Name -contains 'auth' -and $cfg.auth.PSObject.Properties.Name -contains 'impersonate_service_account' -and $cfg.auth.impersonate_service_account) {
        throw 'CLI impersonation differs from the reviewed identity.'
    }
}

# Candidate and Promote assert the checkout before every command; Rollback and Retire record it as an observation, because a
# moved or dirty checkout must not stop a rollback (3.5).
function Step([string]$Name, [string[]]$Argv, [int[]]$Accept = @(), [string]$WorkDir = '') {
    if ($Action -in @('Candidate', 'Promote')) { Assert-Source }
    return (Run-Logged -Name $Name -Argv $Argv -Accept $Accept -WorkDir $WorkDir)
}

function Note-Source {
    try { Assert-Source; $state = 'clean' } catch { $state = 'differs' }
    "source: $state" | Set-Content -LiteralPath (Join-Path $Script:RunDir 'source-observation.txt') -Encoding utf8
}

function Invoke-Helper([string]$Phase, [switch]$Tag) {
    $argv = @('py', '-3.13', $Script:Helper, '--mode', 'services-only', '--phase', $Phase, '--bindings', $Bindings, '--evidence', $Script:RunDir)
    if ($Tag) { $argv += @('--tag', $Script:Bound.release_id) }
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

function Get-Readbacks([string]$Phase) {
    $folder = Join-Path $Script:Paths.root 'readbacks'
    return @(Get-ChildItem -LiteralPath $folder -Filter ($Phase + '-*.json') -ErrorAction SilentlyContinue | Sort-Object Name)
}

function Get-FreezeHash {
    $files = @(Get-Readbacks 'Freeze')
    if ($files.Count -eq 0) { throw 'There is no Freeze readback; the manifest hash cannot be read from it.' }
    $hash = (Get-Content -LiteralPath $files[-1].FullName -Raw | ConvertFrom-Json).manifest_sha256
    if ($hash -notmatch '^[0-9a-f]{64}$') { throw 'The Freeze readback holds no manifest hash.' }
    return $hash
}

# The hash the paste passes on is the one the Freeze readback recorded, never one recomputed from the file about to be used. If
# the manifest and its sidecar were rewritten together after Freeze, their hash no longer equals the recorded one.
function Assert-ManifestIsTheFrozenOne {
    $recorded = Get-FreezeHash
    $inFile = (Get-Content -LiteralPath $Script:Paths.manifest -Raw | ConvertFrom-Json).manifest_sha256
    $inSidecar = (Get-Content -LiteralPath $Script:Paths.sidecar -Raw).Trim()
    if ($inFile -ne $recorded -or $inSidecar -ne $recorded) { throw 'The manifest differs from the one the Freeze readback recorded.' }
}

function Get-A80([string]$Service) {
    $baseline = Read-JsonFile $Script:Paths.baseline 'the bound baseline'
    return $baseline.services.$Service.serving.name
}

function Traffic([string]$Name, [string]$Service, [string]$Revision) {
    return (Step -Name $Name -Argv @('gcloud', 'run', 'services', 'update-traffic', $Service, '--project', 'ogilvy-trends-v2', '--region', 'us-central1', "--to-revisions=$Revision=100", '--quiet'))
}

function Remove-Tag([string]$Service) {
    return (Step -Name "remove-tag-$Service" -Argv @('gcloud', 'run', 'services', 'update-traffic', $Service, '--project', 'ogilvy-trends-v2', '--region', 'us-central1', '--remove-tags', $Script:Bound.release_id, '--quiet'))
}

# The smoke. The passcode was typed at the start of the run and is held as a SecureString. It is put in the environment for
# the smoke child alone: the checkout is asserted first, so the git children of Assert-Source never see it, and it is removed
# again in a finally block. An inherited value is removed at the start of the run and put back at its end.
function Invoke-Smoke([string]$ApiTagUrl) {
    if ($Script:ReceiptValue.single_T1_smoke_authorized -ne $true -or $Script:ReceiptValue.max_live_asks -ne 1) { throw 'The smoke is not authorised by the receipt.' }
    if ($null -eq $Script:SmokeSecret) { throw 'The smoke passcode was not typed at the start of the run.' }
    Assert-Source
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Script:SmokeSecret)
    try { $env:F42_SMOKE_PASSCODE = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    $started = [DateTimeOffset]::UtcNow
    try {
        $code = Run-Logged -Name 'smoke' -Argv @('py', '-3.13', 'core/api/smoke.py', $ApiTagUrl, '--market', 'ZA', '--mode', 'live', '--ask-timeout', '900') -Accept @(1)
    } finally {
        Remove-Item Env:F42_SMOKE_PASSCODE -ErrorAction SilentlyContinue
    }
    $log = Get-Content -LiteralPath (Join-Path $Script:RunDir 'smoke.log') -ErrorAction SilentlyContinue
    $line = @($log | Where-Object { $_ -like 'SMOKE-RESULT *' } | Select-Object -Last 1)
    $parsed = $line.Count -eq 1 -and ($line[0] -match '^SMOKE-RESULT base=(\S+) checks=(\d+) passed=(\d+)$')
    if (-not ($parsed -and $Matches[1] -eq $ApiTagUrl -and [int]$Matches[2] -eq [int]$Script:LockData.expected_smoke_checks)) {
        throw 'The smoke printed no SMOKE-RESULT line for this URL and the locked check count; no receipt is written.'
    }
    $checksTotal, $checksPassed = [int]$Matches[2], [int]$Matches[3]
    $manifest = Get-Content -LiteralPath $Script:Paths.manifest -Raw | ConvertFrom-Json
    $receipt = [ordered]@{
        schema_version = 1; release_id = $Script:Bound.release_id; argv_url = $ApiTagUrl; manifest_sha256 = $manifest.manifest_sha256
        candidates = @{ 'f42-agent' = "f42-agent-$($Script:Bound.release_id)"; 'f42-api' = "f42-api-$($Script:Bound.release_id)" }
        started_utc = $started.ToString('o'); ended_utc = [DateTimeOffset]::UtcNow.ToString('o'); exit_code = $code
        checks_total = $checksTotal; checks_passed = $checksPassed
        log_sha256 = (Get-Sha (Join-Path $Script:RunDir 'smoke.log'))
    }
    $stream = [IO.File]::Open($Script:Paths.smokeReceipt, [IO.FileMode]::CreateNew)
    try { $writer = New-Object IO.StreamWriter($stream); $writer.Write(($receipt | ConvertTo-Json -Depth 5)); $writer.Flush() } finally { $stream.Dispose() }
    if ($code -ne 0) { throw 'The smoke failed. The receipt records it; promotion will refuse it.' }
}

# 3. The four actions.
function Invoke-Candidate {
    Invoke-Helper 'BeforeAnyWrite' | Out-Null
    $tag = "us-central1-docker.pkg.dev/ogilvy-trends-v2/intelligence-42/f42-web:$($Script:Bound.target.Substring(0, 12))-$($Script:Bound.release_id.Substring($Script:Bound.release_id.Length - 2))"
    $archive = Join-Path $Script:RunDir 'source.tar'
    $extract = Join-Path $Script:RunDir 'source'
    Step -Name 'archive' -Argv @('git', 'archive', '--format=tar', "--output=$archive", $Script:Bound.target) | Out-Null
    # Both tar programs refuse a target that does not exist, so the directory is made first; New-Item without -Force also
    # refuses a directory that is already there. The build then runs inside the extracted tree, so what is uploaded is the
    # archive of the bound commit and nothing else from the checkout.
    New-Item -ItemType Directory -Path $extract | Out-Null
    Step -Name 'extract' -Argv @($Script:Tar, '-xf', $archive, '-C', $extract) | Out-Null
    Step -Name 'build' -WorkDir $extract -Argv @('gcloud', 'builds', 'submit', '--project', 'ogilvy-trends-v2', '--region', 'us-central1', '--config', 'core/api/cloudbuild.yaml',
        '--substitutions', "_IMAGE=$tag", '--gcs-source-staging-dir', 'gs://ogilvy-trends-v2-f42-media-staging/build-source',
        '--service-account', 'projects/ogilvy-trends-v2/serviceAccounts/f42-deployer@ogilvy-trends-v2.iam.gserviceaccount.com', '.') | Out-Null
    if (Test-Path -LiteralPath $Script:Paths.manifest) { throw 'The manifest already exists; Freeze creates it once and never overwrites it.' }
    Invoke-Helper 'Freeze' | Out-Null
    # The validator runs on the real manifest before anything is applied (DE-01, PS-17).
    Step -Name 'durable-validate' -Argv @('py', '-3.13', 'core/setup/durable_effects_check.py', '--manifest', $Script:Paths.durable, '--check', 'validate', '--evidence', $Script:RunDir) | Out-Null
    $durable = Read-JsonFile $Script:Paths.durable 'the durable-effects manifest'
    foreach ($effect in @($durable.effects)) {
        if ($effect.apply_kind -notin @('schema', 'tag_add', 'traffic_pin', 'env_set', 'code')) { throw 'An effect names an apply kind outside the paste''s allowlist.' }
    }
    if (@($durable.effects | Where-Object { $_.apply_kind -eq 'schema' }).Count -gt 0) {
        Step -Name 'schema-apply' -Argv @('py', '-3.13', '-m', 'core.schema.apply', '--apply') | Out-Null
    }
    foreach ($service in @('f42-agent', 'f42-api')) { Traffic "pin-$service" $service (Get-A80 $service) | Out-Null }
    Invoke-Helper 'BeforeCandidate' | Out-Null
    $manifestPath = $Script:Paths.manifest
    Assert-ManifestIsTheFrozenOne
    Step -Name 'deploy-candidate' -Argv @('bash', 'core/api/deploy_candidate.sh', $manifestPath, (Get-FreezeHash)) | Out-Null
    Invoke-Helper 'BeforeSmoke' | Out-Null
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    Invoke-Smoke $manifest.candidates.'f42-api'.tag_url
    Invoke-Helper 'AfterSmoke' | Out-Null
}

function Test-QuietWindow {
    if ([string]::IsNullOrEmpty($QuietWindowVerifiedAtUtc)) { throw 'Promote needs the quiet-window verification time (-QuietWindowVerifiedAtUtc).' }
    $age = (Get-UtcNow) - [DateTimeOffset]::Parse($QuietWindowVerifiedAtUtc)
    if ($age.TotalMinutes -gt 15 -or $age.TotalMinutes -lt -1) { throw 'The quiet-window verification is older than 15 minutes.' }
}

function Test-PromoteInputs {
    Test-QuietWindow
    foreach ($path in @($Script:Paths.manifest, $Script:Paths.sidecar, $Script:Paths.smokeReceipt)) {
        if (-not (Test-Path -LiteralPath $path)) { throw 'A bound file for Promote is missing; it reads only the release directory.' }
    }
    if (@(Get-Readbacks 'AfterSmoke').Count -eq 0) { throw 'AfterSmoke never ran.' }
    Assert-ManifestIsTheFrozenOne
}

function Invoke-Promote {
    Step -Name 'inflight-asks' -Argv @('py', '-3.13', 'core/setup/durable_effects_check.py', '--check', 'inflight-asks', '--bindings', $Bindings, '--evidence', $Script:RunDir) -Accept @(1) | Out-Null
    Invoke-Helper 'BeforePromotion' | Out-Null
    $agentRevision = "f42-agent-$($Script:Bound.release_id)"
    $apiRevision = "f42-api-$($Script:Bound.release_id)"
    Traffic 'promote-f42-agent' 'f42-agent' $agentRevision | Out-Null
    Invoke-Helper 'AfterAgentPromotion' | Out-Null
    # A new API with an old agent is unreachable by construction: the API is only promoted once the agent readback passed.
    Traffic 'promote-f42-api' 'f42-api' $apiRevision | Out-Null
    Invoke-Helper 'AfterPromotion' | Out-Null
}

function Invoke-Rollback {
    Note-Source
    Assert-Identity
    Invoke-Helper 'BeforeRollback' | Out-Null
    Traffic 'restore-f42-api' 'f42-api' (Get-A80 'f42-api') | Out-Null
    Traffic 'restore-f42-agent' 'f42-agent' (Get-A80 'f42-agent') | Out-Null
    Invoke-Helper 'AfterRollback' | Out-Null
}

function Invoke-Retire {
    Note-Source
    $promoted = @(Get-Readbacks 'AfterAgentPromotion').Count -gt 0
    $rolledBack = @(Get-Readbacks 'AfterRollback').Count -gt 0
    $evidence = $rolledBack -or (-not $promoted -and (@(Get-Readbacks 'AfterSmoke').Count -gt 0 -or @(Get-Readbacks 'BeforeRollback').Count -gt 0))
    if (-not $evidence) { throw 'Retire is refused: a failure branch needs AfterRollback first, or proof that traffic never moved.' }
    Invoke-Helper 'BeforeRetire' -Tag | Out-Null
    Remove-Tag 'f42-api' | Out-Null
    Remove-Tag 'f42-agent' | Out-Null
    Invoke-Helper 'AfterRetire' -Tag | Out-Null
}

# 3a. What only Albert can give, asked at the very start of Candidate and Promote, before any command and before the first file
# is written. DEPLOY (both), IDLE (Promote) and the smoke passcode (Candidate) are typed at a console that is really there: a
# redirected stdin or a non-interactive host is refused with no fallback, because Read-Host would otherwise take the answer
# from a pipe. The receipt has already been validated, so a receipt alone never starts a release. The passcode is read as a
# SecureString and is not echoed. Rollback and Retire ask for nothing, because a rollback never waits.
function Test-Interactive { return ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) }

function Assert-Interactive {
    if (-not (Test-Interactive)) { throw 'NOT INTERACTIVE: DEPLOY, IDLE and the passcode are typed at a console. This run is not at one, and there is no fallback.' }
}

# Read-Host is called by its module-qualified name, so a function of that name defined by whoever invoked the paste is never run
# in its place. Read-Typed is the one place either prompt is made.
function Read-Typed([string]$Prompt, [switch]$Secure) {
    if ($Secure) { return (Microsoft.PowerShell.Utility\Read-Host -AsSecureString -Prompt $Prompt) }
    return (Microsoft.PowerShell.Utility\Read-Host -Prompt $Prompt)
}

function Get-UtcNow { return [DateTimeOffset]::UtcNow }

function Read-Word([string]$Word) {
    try { $typed = Read-Typed "Type $Word to continue" } catch { throw "NOT INTERACTIVE: the prompt for $Word could not be shown." }
    if ($typed -cne $Word) { throw "NOT CONFIRMED: $Word was not typed exactly. Nothing was written." }
}

function Read-Passcode {
    try { $secret = Read-Typed 'Smoke passcode' -Secure } catch { throw 'NOT INTERACTIVE: the passcode prompt could not be shown.' }
    if ($null -eq $secret -or $secret.Length -eq 0) { throw 'NOT CONFIRMED: no smoke passcode was typed. Nothing was written.' }
    return $secret
}

# The environment variables the deploy removes from f42-agent, read live and matched by digest by the same module the deploy
# script uses. The names are shown on the screen here and written nowhere: not to a log, not to the receipt, not to the release
# directory.
function Show-DeclaredRemoval {
    $live = Read-Native -Exe gcloud -Arguments @('run', 'services', 'describe', 'f42-agent', '--project', 'ogilvy-trends-v2', '--region', 'us-central1', '--format=json')
    $matched = Read-Native -Exe py -Arguments @('-3.13', '-B', '-m', 'core.setup.release.declared_env_removals', '--service', 'f42-agent') -InputText $live
    $names = @($matched -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' })
    if ($names.Count -eq 0) {
        Write-Host 'f42-agent carries no declared environment variable; the deploy removes none.'
    } else {
        Write-Host "The candidate deploy removes $($names.Count) environment variable(s) from f42-agent, and only these:"
        foreach ($name in $names) { Write-Host "  $name" }
    }
}

# The number of Asks still running, read before IDLE is asked for so the operator sees it first. The checker needs an evidence
# folder, so this read uses a scratch folder in the temp directory, outside the release directory, and removes it. A count that
# is refused or unreadable is returned as null, which the caller treats as unknown, never as zero.
function Get-InflightCount {
    $dir = Join-Path ([IO.Path]::GetTempPath()) ('f42-inflight-' + [guid]::NewGuid().ToString('N'))
    try {
        Read-Native -Exe py -Arguments @('-3.13', 'core/setup/durable_effects_check.py', '--check', 'inflight-asks', '--bindings', $Bindings, '--evidence', $dir) | Out-Null
        $result = Get-Content -LiteralPath (Join-Path $dir 'inflight-asks.json') -Raw | ConvertFrom-Json
        if ($result.running -is [int] -or $result.running -is [long]) { return [int]$result.running }
        return $null
    } catch {
        return $null
    } finally {
        Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Confirm-Action {
    if ($Action -notin @('Candidate', 'Promote')) { return }
    Assert-Interactive
    if ($Action -eq 'Candidate') {
        Assert-Source
        Assert-Identity
        Show-DeclaredRemoval
        Read-Word 'DEPLOY'
        $Script:SmokeSecret = Read-Passcode
    } else {
        Test-PromoteInputs
        Assert-Source
        Assert-Identity
        $running = Get-InflightCount
        $again = $true
        if ($null -eq $running) {
            Write-Host 'In-flight Asks: unknown. The count could not be counted: the check was refused or failed.'
        } elseif ($running -gt 0) {
            Write-Host "In-flight Asks: $running. $running Ask(s) are running, and promoting now can cut them off."
        } else {
            Write-Host 'In-flight Asks: 0'
            $again = $false
        }
        Read-Word 'IDLE'
        if ($again) {
            Write-Host 'Type IDLE a second time to confirm you accept that.'
            Read-Word 'IDLE'
        }
        Read-Word 'DEPLOY'
        Test-QuietWindow
    }
}

# An alias is resolved before any function, in every scope, so one named like a function of this paste, or like the prompt
# cmdlet, could supply a typed word or the clock with no console. The refusal is the first thing Invoke-Release does, and Get-Alias
# is called by its module-qualified name so that a function or an alias of that name cannot blind it. The prompt cmdlet's name is
# written in two parts so that the text scan for a bare call of it stays exact.
$Script:Shadowable = @('Read-Typed', 'Read-Word', 'Read-Passcode', 'Test-Interactive', 'Assert-Interactive', 'Get-UtcNow', 'Test-QuietWindow',
                       'Get-InflightCount', 'Read-Native', 'Run-Logged', 'Step', ('Read-' + 'Host'))

function Assert-NoShadowAlias {
    $found = @(Microsoft.PowerShell.Utility\Get-Alias -Name $Script:Shadowable -ErrorAction SilentlyContinue)
    if ($found.Count -gt 0) { throw "NOT EXECUTABLE: an alias shadows a paste function or the prompt cmdlet: $($found.Name -join ', ')." }
}

function Invoke-Release {
    Assert-NoShadowAlias
    Test-Packet
    Test-Receipt
    $inheritedPresent = Test-Path Env:F42_SMOKE_PASSCODE
    $inherited = $env:F42_SMOKE_PASSCODE
    # Every child inherits these: gcloud keeps no file log of its arguments (the removed names travel in one), Python writes no
    # bytecode into the checkout, and git status takes no index lock. They are put back at the end of the run.
    $childEnv = [ordered]@{ CLOUDSDK_CORE_DISABLE_FILE_LOGGING = '1'; PYTHONDONTWRITEBYTECODE = '1'; GIT_OPTIONAL_LOCKS = '0' }
    $savedEnv = @{}
    foreach ($name in $childEnv.Keys) { $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name) }
    try {
        foreach ($name in $childEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $childEnv[$name]) }
        Remove-Item Env:F42_SMOKE_PASSCODE -ErrorAction SilentlyContinue
        Set-Location -LiteralPath $Repo
        Confirm-Action
        $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmss')
        $Script:RunDir = Join-Path $Script:Paths.root (Join-Path 'runs' "$stamp-$Action")
        New-Item -ItemType Directory -Path $Script:RunDir -Force | Out-Null
        switch ($Action) {
            'Candidate' { Invoke-Candidate }
            'Promote' { Invoke-Promote }
            'Rollback' { Invoke-Rollback }
            'Retire' { Invoke-Retire }
        }
    } finally {
        foreach ($name in $childEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $savedEnv[$name]) }
        if ($inheritedPresent) { $env:F42_SMOKE_PASSCODE = $inherited } else { Remove-Item Env:F42_SMOKE_PASSCODE -ErrorAction SilentlyContinue }
        if ($null -ne $Script:SmokeSecret) { $Script:SmokeSecret.Dispose(); $Script:SmokeSecret = $null }
    }
}

if (-not $DefinitionsOnly) { Invoke-Release }
