# Source CMD hook only. Installer binaries/assets are deliberately unchanged.
param([switch]$Restore, [ValidateSet('', 'light', 'standard', 'heavy')][string]$Tier = '')
$ErrorActionPreference = 'Stop'
$base = Join-Path $env:LOCALAPPDATA 'NeoJarvis\runtime'
$pointer = Join-Path $env:LOCALAPPDATA 'NeoJarvis\bootstrap\active_runtime.txt'
if (-not (Test-Path -LiteralPath $pointer)) { Write-Host 'Voice: managed Python is unavailable.'; exit 2 }
$runtime = [IO.Path]::GetFullPath((Get-Content -LiteralPath $pointer -Raw).Trim())
$prefix = [IO.Path]::GetFullPath($base).TrimEnd('\') + '\'
if (-not $runtime.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { exit 2 }
$cursor = $runtime
while ($cursor -and $cursor.Length -ge $base.Length) {
    if ((Test-Path -LiteralPath $cursor) -and ((Get-Item -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint)) { exit 2 }
    $cursor = Split-Path -Parent $cursor
}
$python = Join-Path $runtime 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { exit 2 }
$helper = Join-Path $PSScriptRoot 'prepare_response_voice.py'
if ($Tier) { & $python $helper --tier $Tier } elseif ($Restore) { & $python $helper --restore } else { & $python $helper }
exit $LASTEXITCODE
