# Launch one hidden, separately locked helper. No shell interpolation of user paths.
param([switch]$Cancel)
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'bootstrap_ollama.ps1'
if (-not (Test-Path -LiteralPath $script)) { Write-Warning 'Ollama setup helper is missing.'; exit 2 }
$argsForChild = @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $script + '"'))
if ($Cancel) { $argsForChild += '-Cancel' }
try { Start-Process -FilePath 'powershell.exe' -ArgumentList $argsForChild -WindowStyle Hidden | Out-Null; exit 0 }
catch { Write-Warning 'Could not queue Ollama setup. Other NEO features remain available.'; exit 2 }
