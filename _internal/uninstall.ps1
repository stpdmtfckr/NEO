param([switch]$Confirmed)
$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $PSScriptRoot
$localRoot = Join-Path $env:LOCALAPPDATA 'NeoJarvis'
$roamingRoot = Join-Path $env:APPDATA 'NeoJarvis'
$startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
$programs = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
function Remove-ExactFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Write-Warning "Preserved unexpected directory/link: $Path"; return
    }
    Remove-Item -LiteralPath $Path -Force
}
function Remove-OwnedDataRoot([string]$Path, [string]$Parent) {
    $expected = [IO.Path]::GetFullPath((Join-Path $Parent 'NeoJarvis')).TrimEnd('\')
    if ([IO.Path]::GetFullPath($Path).TrimEnd('\') -ne $expected) { throw 'Unsafe cleanup root rejected.' }
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { Write-Warning "Preserved linked root: $Path"; return }
    # Refuse the entire recursive cleanup if a junction/symlink exists. This is
    # intentionally conservative, and never follows a link into user documents.
    $pending = New-Object System.Collections.Generic.Stack[string]
    $pending.Push($Path)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($child in Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop) {
            if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) { Write-Warning "Preserved $Path because it contains links; review it manually."; return }
            if ($child.PSIsContainer) { $pending.Push($child.FullName) }
        }
    }
    Remove-Item -LiteralPath $Path -Recurse -Force
}
if (-not $Confirmed) {
    Write-Host 'NEO uninstall' -ForegroundColor Yellow
    Write-Host 'Removes NEO settings, history, local runtime, caches and startup entries.'
    Write-Host 'Shared Python, Ollama and all shared Ollama/HuggingFace model caches are preserved.'
    Write-Host 'The extracted project folder is preserved. Export diagnostics/backups first if needed.'
    if ((Read-Host 'Type DELETE to continue') -cne 'DELETE') { exit 2 }
}
# Ask the live application to shut down through its established IPC protocol.
$pointer = Join-Path $localRoot 'bootstrap\active_runtime.txt'
if (Test-Path -LiteralPath $pointer) {
    $runtime = (Get-Content -LiteralPath $pointer -Raw).Trim()
    $runtimePrefix = [IO.Path]::GetFullPath((Join-Path $localRoot 'runtime')).TrimEnd('\') + '\'
    if ([IO.Path]::GetFullPath($runtime).StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        $python = Join-Path $runtime 'Scripts\python.exe'
        if (Test-Path -LiteralPath $python) {
            Push-Location $PSScriptRoot
            try { & $python -c "from jarvis.runtime.instance_coordinator import InstanceCoordinator; InstanceCoordinator().request('quit_app', wait_ack=True, timeout=3)" } finally { Pop-Location }
        }
    }
}
# Do not taskkill arbitrary/reused PIDs or other users of Python/Ollama.
$running = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -and $_.CommandLine.Contains((Join-Path $PSScriptRoot 'app.py'))
})
if ($running.Count -gt 0) {
    Start-Sleep -Seconds 3
    $running = @($running | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
    if ($running.Count -gt 0) { Write-Warning 'NEO is still closing. Exit NEO from its tray, then run uninstall again. Data was preserved.'; exit 3 }
}
foreach ($name in @('NEO Priority','NEO Wake Service','NEO JARVIS Priority','NEO JARVIS Wake Service')) {
    try { & schtasks.exe /Delete /F /TN $name 2>$null | Out-Null } catch { }
}
foreach ($name in @('NEO.vbs','NEO.cmd','NEO_WAKE.vbs','NEO_JARVIS.vbs','NEO_JARVIS.cmd','NEO_JARVIS_WAKE.vbs')) {
    Remove-ExactFile (Join-Path $startup $name)
}
foreach ($name in @('NEO.lnk','NEO JARVIS.lnk')) { Remove-ExactFile (Join-Path $programs $name) }
# The native credential provider requires its own elevated removal. Never remove
# its DLL while registration still exists, which could break Windows logon.
$provider = 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Authentication\Credential Providers\{7A0B07ED-99E8-4B79-A61E-E5B4EA1FB347}'
if (Test-Path $provider) {
    Write-Warning 'The optional Windows unlock provider remains installed. Disable it in NEO settings with administrator approval before removing its files.'
    exit 4
}
Remove-OwnedDataRoot $localRoot $env:LOCALAPPDATA
if ([IO.Path]::GetFullPath($roamingRoot) -ne [IO.Path]::GetFullPath($localRoot)) { Remove-OwnedDataRoot $roamingRoot $env:APPDATA }
Write-Host 'NEO user data/startup removal completed. Shared Python/Ollama/models were preserved.' -ForegroundColor Green
Write-Host 'You may now delete the extracted NEO project folder manually.'
exit 0
