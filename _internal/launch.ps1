param(
    [string]$Mode = ''
)
$ErrorActionPreference = 'Stop'
# Older wrappers passed --test-all as an unbound PowerShell parameter. A simple
# script stores such values in $args; ignoring them silently launched the GUI.
# New wrappers use -Mode test-all; retain the old spelling explicitly and reject
# unknown/excess arguments BEFORE dependency setup or normal launch.
$allowedModes = @('doctor', 'self-test', 'repair', 'collect-logs', 'tests',
    'integration-test', 'atlas-test', 'performance-test', 'startup-test',
    'test-all', 'nvidia-diagnostic', 'help', 'uninstall')
if ($args.Count -gt 0) {
    if ($Mode -or $args.Count -ne 1) {
        Write-Host 'INVALID_LAUNCH_ARGUMENTS: expected one named -Mode value.'
        exit 2
    }
    $Mode = [string]$args[0]
}
if ($Mode) {
    $canonicalMode = $Mode.Trim().ToLowerInvariant()
    if ($canonicalMode.StartsWith('--')) { $canonicalMode = $canonicalMode.Substring(2) }
    if ($allowedModes -notcontains $canonicalMode) {
        Write-Host 'INVALID_LAUNCH_MODE: run NEO.cmd --help.'
        exit 2
    }
    $Mode = '--' + $canonicalMode
    Write-Host ('NEO_LAUNCH_MODE=' + $canonicalMode)
}

$internalRoot = $PSScriptRoot
$stateRoot = Join-Path $env:LOCALAPPDATA 'NeoJarvis\bootstrap'
$runtimeBase = Join-Path $env:LOCALAPPDATA 'NeoJarvis\runtime'
if ($Mode -eq '--help') {
    Write-Host 'NEO.cmd                 Start NEO (install required runtime on first run)'
    Write-Host 'NEO_TEST.cmd            Complete test; one verified results ZIP'
    Write-Host 'NEO.cmd --doctor        Read-only runtime diagnosis'
    Write-Host 'NEO.cmd --nvidia-diagnostic  Read-only NVIDIA state check after a 5-second delay'
    Write-Host 'NEO.cmd --self-test     Safe capability checks; no real user actions'
    Write-Host 'NEO.cmd --repair        Build and validate a fresh managed environment'
    Write-Host 'NEO.cmd --collect-logs  Export bounded redacted diagnostic logs'
    Write-Host 'NEO.cmd --tests         Run safe regression suite and phrase checks'
    Write-Host 'NEO.cmd --integration-test  Interactive optional API authentication probes'
    Write-Host 'NEO.cmd --atlas-test    Sequential atlas QA UI; isolated profile, safe fixtures'
    Write-Host 'NEO.cmd --performance-test  Read-only 5-minute resource sample of running NEO'
    Write-Host 'NEO.cmd --startup-test  Launch readiness and token-free network diagnostics; export one ZIP'
    Write-Host 'NEO.cmd --uninstall     Remove NEO after explicit DELETE confirmation'
    exit 0
}
if ($Mode -eq '--uninstall') {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $internalRoot 'uninstall.ps1')
    exit $LASTEXITCODE
}
function Test-RuntimePath([string]$Candidate) {
    if (-not $Candidate) { return $false }
    $full = [IO.Path]::GetFullPath($Candidate).TrimEnd('\')
    $base = [IO.Path]::GetFullPath($runtimeBase).TrimEnd('\') + '\'
    if (-not $full.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    $cursor = $full
    while ($cursor -and $cursor.Length -ge $base.TrimEnd('\').Length) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { return $false }
        }
        $cursor = Split-Path -Parent $cursor
    }
    return $true
}
$runtime = Join-Path $runtimeBase 'v4.0.20'
$pointer = Join-Path $stateRoot 'active_runtime.txt'
if (Test-Path -LiteralPath $pointer) {
    $stored = (Get-Content -LiteralPath $pointer -Raw).Trim()
    if (Test-RuntimePath $stored) { $runtime = $stored }
}
$override = if ($env:NEO_VENV) { $env:NEO_VENV } else { $env:NEO_JARVIS_VENV }
if ($override) {
    if (Test-RuntimePath $override) { $runtime = [IO.Path]::GetFullPath($override) }
    else { Write-Warning 'Ignoring external/untrusted runtime override; NEO uses its dedicated runtime directory.' }
}
$python = Join-Path $runtime 'Scripts\python.exe'
$healthy = $false
if ((Test-Path -LiteralPath $python) -and $Mode -ne '--doctor') {
    & $python (Join-Path $internalRoot 'dependency_guard.py') --check
    $healthy = $LASTEXITCODE -eq 0
}
if ($Mode -eq '--doctor') {
    Write-Host "Runtime: $runtime"
    if (Test-Path -LiteralPath $python) {
        & $python (Join-Path $internalRoot 'bootstrap.py') --doctor
        exit $LASTEXITCODE
    }
    Write-Host 'Managed Python is missing. Normal NEO.cmd launch or --repair will install it.'
    exit 2
}
if (-not $healthy -or $Mode -eq '--repair') {
    if ($Mode -in @('--collect-logs', '--self-test', '--tests', '--integration-test', '--performance-test', '--startup-test', '--test-all', '--nvidia-diagnostic')) {
        Write-Host 'Runtime is not ready. Run NEO.cmd --repair first. No packages were changed.'
        exit 2
    }
    $env:NEO_VENV = $runtime
    if ($Mode -eq '--repair') { $env:NEO_REPAIR = '1' }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $internalRoot 'setup.ps1')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if (-not (Test-Path -LiteralPath $pointer)) { throw 'Setup did not commit a validated runtime.' }
    $runtime = (Get-Content -LiteralPath $pointer -Raw).Trim()
    if (-not (Test-RuntimePath $runtime)) { throw 'Setup runtime pointer failed path validation.' }
    $python = Join-Path $runtime 'Scripts\python.exe'
}
$env:NEO_VENV = $runtime
if ($Mode -eq '--repair') {
    & $python (Join-Path $internalRoot 'bootstrap.py') --self-test
    exit $LASTEXITCODE
}
if ($Mode -eq '--test-all') {
    $runId = [Guid]::NewGuid().ToString('N')
    $receiptDir = Join-Path $env:LOCALAPPDATA 'NeoJarvis\diagnostics\test_receipts'
    New-Item -ItemType Directory -Path $receiptDir -Force | Out-Null
    $receipt = Join-Path $receiptDir ($runId + '.json')
    $testScript = Join-Path $internalRoot 'tools\test_all.py'
    Write-Host ('UNIFIED_TEST_STARTING=' + $runId)
    & $python -u $testScript --invocation-id $runId --receipt $receipt
    $testExit = $LASTEXITCODE
    # A successful GUI start or PowerShell Ctrl+C can return 0 without a test.
    # Accept 0/1 only with this invocation's verified result archive/receipt.
    & $python -u $testScript --verify-receipt $receipt --invocation-id $runId --process-exit $testExit
    exit $LASTEXITCODE
}
if ($Mode) { & $python (Join-Path $internalRoot 'bootstrap.py') $Mode }
else { & $python (Join-Path $internalRoot 'bootstrap.py') }
exit $LASTEXITCODE
