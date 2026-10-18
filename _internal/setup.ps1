# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

# V5.8: setup.ps1 is also invoked by the GUI launcher during automatic repair.
# In that mode *every* console-subsystem child is created hidden; otherwise a
# console-less parent can still make python.exe/pip flash a new window for a few
# frames. Direct START_JARVIS.bat keeps the normal visible diagnostic output.
$script:NeoSilentBootstrap = ([string]$env:NEO_JARVIS_HIDDEN_BATCH).Trim() -eq "1"

function Format-NeoArgument([string]$Value) {
    if ($null -eq $Value) { return '""' }
    $text = [string]$Value
    if ($text -notmatch '[\s"]') { return $text }
    return '"' + ($text.Replace('"', '\"')) + '"'
}

function Invoke-NeoProcess([string]$FilePath, [string[]]$ArgumentList = @()) {
    if ([string]::IsNullOrWhiteSpace($FilePath)) {
        return [pscustomobject]@{ ExitCode = 9009; Output = @("missing executable") }
    }
    if (-not $script:NeoSilentBootstrap) {
        # Windows PowerShell 5.1 promotes native-process stderr records into the
        # PowerShell error stream. With the script-wide ErrorActionPreference=Stop,
        # a harmless warning written to stderr (for example pywinauto's COM-threading
        # warning) can therefore terminate setup even when the child process exits 0.
        # Native process success is determined by its exit code; stderr is diagnostic
        # output and must not become a terminating PowerShell exception here.
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $captured = @(& $FilePath @ArgumentList 2>&1)
            $code = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        foreach ($line in $captured) {
            if ($null -ne $line -and ([string]$line).Length -gt 0) { Write-Host ([string]$line) }
        }
        return [pscustomobject]@{ ExitCode = [int]$code; Output = $captured }
    }

    $stdoutPath = Join-Path $env:TEMP ("neo-setup-out-" + [guid]::NewGuid().ToString("N") + ".log")
    $stderrPath = Join-Path $env:TEMP ("neo-setup-err-" + [guid]::NewGuid().ToString("N") + ".log")
    try {
        $argText = (($ArgumentList | ForEach-Object { Format-NeoArgument ([string]$_) }) -join ' ')
        $proc = Start-Process -FilePath $FilePath -ArgumentList $argText -WindowStyle Hidden -Wait -PassThru `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        $captured = @()
        if (Test-Path -LiteralPath $stdoutPath) { $captured += @(Get-Content -LiteralPath $stdoutPath -ErrorAction SilentlyContinue) }
        if (Test-Path -LiteralPath $stderrPath) { $captured += @(Get-Content -LiteralPath $stderrPath -ErrorAction SilentlyContinue) }
        return [pscustomobject]@{ ExitCode = [int]$proc.ExitCode; Output = $captured }
    } catch {
        return [pscustomobject]@{ ExitCode = 1; Output = @($_.Exception.Message) }
    } finally {
        Remove-Item -LiteralPath $stdoutPath,$stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Get-BootstrapStateDir {
    if ($env:LOCALAPPDATA) { return (Join-Path $env:LOCALAPPDATA "NeoJarvis\bootstrap") }
    return (Join-Path $PSScriptRoot ".bootstrap")
}

function Mark-PythonInstalledByJarvis([string]$Method) {
    try {
        $dir = Get-BootstrapStateDir
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        $payload = @{ version = "3.12"; method = $Method; installed_at = (Get-Date).ToString("o") } | ConvertTo-Json -Compress
        Set-Content -LiteralPath (Join-Path $dir "python312_installed_by_jarvis.json") -Value $payload -Encoding UTF8
    } catch { }
}

function Test-PythonCandidate([string]$Exe, [string[]]$PrefixArgs) {
    try {
        $probe = Join-Path $PSScriptRoot "_bootstrap_python_probe.py"
        if (-not (Test-Path -LiteralPath $probe)) {
            throw "Python probe file is missing: $probe"
        }
        # Capture output inside the process wrapper; in silent bootstrap the
        # interpreter is created with a hidden window and cannot flash a console.
        $result = Invoke-NeoProcess $Exe (@($PrefixArgs) + @($probe))
        return [bool]($result.ExitCode -eq 0)
    } catch {
        Write-Host "Python candidate rejected: $($_.Exception.Message)" -ForegroundColor DarkGray
        return $false
    }
}

function Add-PythonCandidate([System.Collections.ArrayList]$List, [string]$Exe, [string[]]$Prefix) {
    if ([string]::IsNullOrWhiteSpace($Exe)) { return }

    $key = ($Exe + "|" + ($Prefix -join " ")).ToLowerInvariant()
    foreach ($item in $List) {
        $itemKey = ([string]$item.Exe + "|" + (([string[]]$item.Prefix) -join " ")).ToLowerInvariant()
        if ($itemKey -eq $key) { return }
    }

    [void]$List.Add(@{ Exe = $Exe; Prefix = [string[]]$Prefix })
}

function Get-PythonCandidates {
    $items = New-Object System.Collections.ArrayList

    # NEO keeps one reproducible Windows runtime contract: Python 3.12 x64.
    # Prefer the official Python launcher, then PATH commands.  Every candidate is
    # verified by _bootstrap_python_probe.py, so python.exe pointing to 3.11/3.13
    # is ignored instead of silently creating a different runtime.
    if (Get-Command "py.exe" -ErrorAction SilentlyContinue) {
        Add-PythonCandidate $items "py.exe" @("-3.12")
    }

    foreach ($cmd in @("python.exe", "python3.exe", "python", "python3")) {
        $resolved = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($null -ne $resolved) {
            $source = [string]$resolved.Source
            # Ignore Microsoft Store execution aliases. They are launch stubs, not a usable interpreter.
            if ($source -and $source.ToLowerInvariant().Contains("\microsoft\windowsapps\")) {
                continue
            }
            Add-PythonCandidate $items $cmd @()
        }
    }

    # Also search the normal python.org 3.12 locations even when PATH/py.exe is stale.
    $explicit = @()
    if ($env:LOCALAPPDATA) {
        $explicit += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")
    }
    if ($env:ProgramFiles) {
        $explicit += (Join-Path $env:ProgramFiles "Python312\python.exe")
    }
    $explicit += "C:\Python312\python.exe"

    foreach ($path in $explicit) {
        if ($path -and (Test-Path -LiteralPath $path)) {
            Add-PythonCandidate $items $path @()
        }
    }

    return $items
}

function Find-CompatiblePython {
    foreach ($candidate in (Get-PythonCandidates)) {
        $exe = [string]$candidate.Exe
        $prefix = [string[]]$candidate.Prefix
        Write-Host "Trying Python: $exe $($prefix -join ' ')"
        if (Test-PythonCandidate $exe $prefix) {
            return @{ Exe = $exe; Prefix = $prefix }
        }
    }
    return $null
}

function Install-Python312WithWinget {
    $winget = Get-Command "winget.exe" -ErrorAction SilentlyContinue
    if ($null -eq $winget) { return $false }

    Write-Step "Python 3.12 was not found - installing it with winget"
    Write-Host "This installs the official Python.Python.3.12 package for the current Windows user."
    try {
        $wingetProc = Start-Process -FilePath $winget.Source -ArgumentList @(
            "install", "--id", "Python.Python.3.12", "-e", "--scope", "user",
            "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"
        ) -WindowStyle Hidden -Wait -PassThru
        if ($wingetProc.ExitCode -ne 0) {
            Write-Warning "winget returned code $($wingetProc.ExitCode)"
            return $false
        }
        Start-Sleep -Seconds 2
        Mark-PythonInstalledByJarvis "winget"
        return $true
    } catch {
        Write-Warning "Automatic Python installation failed: $($_.Exception.Message)"
        return $false
    }
}

function Install-Python312Official {
    # Python 3.12.10 is the final 3.12 release that still ships the traditional
    # Windows installer. It is the pinned offline-style fallback for this build's Python 3.12 x64 runtime contract.
    $version = "3.12.10"
    $url = "https://www.python.org/ftp/python/$version/python-$version-amd64.exe"
    $installer = Join-Path $env:TEMP "neo-jarvis-python-$version-amd64.exe"
    Write-Step "WinGet Python install was unavailable - downloading official Python $version x64"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
        if (-not (Test-Path -LiteralPath $installer)) { throw "Python installer download did not create a file" }
        $signature = Get-AuthenticodeSignature -FilePath $installer
        if ($signature.Status -ne "Valid") {
            throw "Python installer Authenticode signature is not valid: $($signature.Status)"
        }
        $publisher = [string]$signature.SignerCertificate.Subject
        if ($publisher -notmatch "Python Software Foundation") {
            throw "Unexpected Python installer signer: $publisher"
        }
        $proc = Start-Process -FilePath $installer -ArgumentList @(
            "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1",
            "Include_launcher=1", "Include_test=0", "Shortcuts=0"
        ) -WindowStyle Hidden -Wait -PassThru
        if ($proc.ExitCode -ne 0) { throw "Python installer exited with code $($proc.ExitCode)" }
        Start-Sleep -Seconds 2
        Mark-PythonInstalledByJarvis "python.org"
        return $true
    } catch {
        Write-Warning "Official Python installation failed: $($_.Exception.Message)"
        return $false
    } finally {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    }
}

Write-Step "Checking Python 3.12 x64"
Write-Host "If Python 3.12 x64 is missing, NEO will download and install it automatically." -ForegroundColor DarkGray
$python = Find-CompatiblePython
if ($null -eq $python) {
    $installed = Install-Python312WithWinget
    if (-not $installed) {
        $installed = Install-Python312Official
    }
    if ($installed) {
        Write-Step "Checking newly installed Python"
        $python = Find-CompatiblePython
    }
}

if ($null -eq $python) {
    Write-Host ""
    Write-Host "NEO needs 64-bit Python 3.12." -ForegroundColor Yellow
    Write-Host "Python 3.12 x64 is the managed runtime used by this build."
    Write-Host "Automatic installation was unavailable or did not succeed."
    Write-Host "Install Python 3.12 x64 from python.org, then run NEO.cmd again."
    throw "Python 3.12 x64 was not found and automatic installation did not complete."
}

$pythonExe = [string]$python.Exe
$pythonPrefix = [string[]]$python.Prefix
Write-Host "Using: $pythonExe $($pythonPrefix -join ' ')" -ForegroundColor Green

# Keep the Python runtime outside the release directory.  Native Windows DLL
# loaders still have MAX_PATH-sensitive code paths; a deeply extracted release can
# otherwise make sounddevice fail to load its bundled PortAudio DLL with Win32
# error 206 (0xCE).  LOCALAPPDATA gives us a short, stable per-user runtime path.
# The managed runtime directory is intentionally keyed only by the product
# version. A HOTFIX/build label must never become part of this path.
$version = "4.0.20"
$runtimeBase = Join-Path $env:LOCALAPPDATA 'NeoJarvis\runtime'
$venvRoot = Join-Path $runtimeBase ("v" + $version)
$requested = if ($env:NEO_VENV) { $env:NEO_VENV } else { $env:NEO_JARVIS_VENV }
if ($requested) {
    $full = [IO.Path]::GetFullPath([string]$requested).TrimEnd('\')
    $prefix = [IO.Path]::GetFullPath($runtimeBase).TrimEnd('\') + '\'
    if ($full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { $venvRoot = $full }
    else { Write-Warning 'External runtime override ignored; no external directory will be modified.' }
}
# Reject junctions/symlinks anywhere from the dedicated runtime root downward.
$cursor = $venvRoot
while ($cursor -and $cursor.Length -ge $runtimeBase.Length) {
    if (Test-Path -LiteralPath $cursor) {
        if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Managed runtime path contains a reparse point: $cursor"
        }
    }
    $cursor = Split-Path -Parent $cursor
}
$owned = $false
$ownerPath = Join-Path $venvRoot '.neo_runtime_owner.json'
try {
    $owner = Get-Content -LiteralPath $ownerPath -Raw -ErrorAction Stop | ConvertFrom-Json
    $owned = ($owner.product -eq 'NEO' -and $owner.path -eq $venvRoot)
} catch { }
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
$compatible = $false
if ($owned -and (Test-Path -LiteralPath $venvPython)) {
    $probeResult = Invoke-NeoProcess $venvPython @((Join-Path $PSScriptRoot '_bootstrap_python_probe.py'))
    $compatible = $probeResult.ExitCode -eq 0
}
if (Test-Path -LiteralPath $venvRoot) {
    # Build beside the old runtime. Never delete or repair an arbitrary directory,
    # and retain a working environment until the replacement passes all checks.
    $venvRoot = Join-Path $runtimeBase ('v' + $version + '-' + [guid]::NewGuid().ToString('N').Substring(0, 12))
    $venvPython = Join-Path $venvRoot 'Scripts\python.exe'
}
Write-Host "NEO Python runtime: $venvRoot" -ForegroundColor Green

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Step "Creating virtual environment"
    $venvParent = Split-Path -Parent $venvRoot
    if ($venvParent -and -not (Test-Path -LiteralPath $venvParent)) {
        New-Item -ItemType Directory -Path $venvParent -Force | Out-Null
    }
    $venvResult = Invoke-NeoProcess $pythonExe (@($pythonPrefix) + @("-m", "venv", $venvRoot))
    if ($venvResult.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        throw "Could not create NEO runtime at $venvRoot"
    }
    @{ product = 'NEO'; path = $venvRoot; created_at = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $venvRoot '.neo_runtime_owner.json') -Encoding UTF8
}

$reportRoot = Join-Path $venvRoot '.neo_install_reports'
New-Item -ItemType Directory -Path $reportRoot -Force | Out-Null
Write-Step "Updating pip"
$pipBootstrap = Invoke-NeoProcess $venvPython @("-m", "pip", "install", "--disable-pip-version-check", "--upgrade", '--report', (Join-Path $reportRoot 'bootstrap.json'), "pip", "wheel", "setuptools")
if ($pipBootstrap.ExitCode -ne 0) { throw "pip bootstrap failed" }

function Install-WinSdk312Wheel([string]$RuntimePython) {
    Write-Step "Installing Windows SDK bridge dependency"
    Write-Host "NEO pins winsdk 1.0.0b10 and uses its prebuilt CPython 3.12 x64 wheel." -ForegroundColor DarkGray

    # First use the normal package index.  --only-binary prevents an accidental
    # CMake/Visual Studio source build if interpreter tags are wrong.
    $winsdkIndex = Invoke-NeoProcess $RuntimePython @("-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:", '--report', (Join-Path $reportRoot 'winsdk.json'), "winsdk==1.0.0b10")
    if ($winsdkIndex.ExitCode -eq 0) { return $true }

    # PyPI also publishes the exact cp312/win_amd64 wheel.  Use a pinned,
    # hash-verified direct fallback so a transient index/tag resolution problem
    # cannot turn into a compiler requirement.
    Write-Warning "Package-index resolution failed. Trying the pinned CPython 3.12 x64 wheel directly."
    $wheelUrl = "https://files.pythonhosted.org/packages/0c/87/a5ffbb9613fcd3917183707ac16c88e82d22a1faf933bb9a3e04fff2f289/winsdk-1.0.0b10-cp312-cp312-win_amd64.whl"
    $wheelSha256 = "8231ce5f16e1fc88bb7dda0adf35633b5b26101eae3b0799083ca2177f03e4e5"
    $wheelPath = Join-Path $env:TEMP "winsdk-1.0.0b10-cp312-cp312-win_amd64.whl"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $wheelUrl -OutFile $wheelPath -UseBasicParsing
        if (-not (Test-Path -LiteralPath $wheelPath)) { throw "winsdk wheel download did not create a file" }
        $actualHash = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $wheelSha256) {
            throw "winsdk wheel SHA-256 mismatch: $actualHash"
        }
        $winsdkWheel = Invoke-NeoProcess $RuntimePython @("-m", "pip", "install", "--disable-pip-version-check", "--no-deps", '--report', (Join-Path $reportRoot 'winsdk.json'), $wheelPath)
        if ($winsdkWheel.ExitCode -ne 0) { throw "pip rejected the pinned winsdk wheel" }
        return $true
    } catch {
        Write-Warning "Direct winsdk wheel fallback failed: $($_.Exception.Message)"
        return $false
    } finally {
        Remove-Item -LiteralPath $wheelPath -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Install-WinSdk312Wheel $venvPython)) {
    Write-Host ""
    Write-Host "winsdk binary wheel could not be installed." -ForegroundColor Red
    Write-Host "The installer verified Python 3.12 x64, but both PyPI resolution and the pinned wheel fallback failed." -ForegroundColor Yellow
    [void](Invoke-NeoProcess $venvPython @((Join-Path $PSScriptRoot "_bootstrap_python_probe.py")))
    throw "winsdk 1.0.0b10 CPython 3.12 x64 wheel installation failed"
}

Write-Step "Installing NEO dependencies"
$depsResult = Invoke-NeoProcess $venvPython @("-m", "pip", "install", "--disable-pip-version-check", "--prefer-binary", '--report', (Join-Path $reportRoot 'dependencies.json'), "-r", (Join-Path $PSScriptRoot "requirements.txt"))
if ($depsResult.ExitCode -ne 0) { throw "Dependency installation failed" }

Write-Step "Checking Python dependencies"
$importResult = Invoke-NeoProcess $venvPython @((Join-Path $PSScriptRoot "_bootstrap_import_probe.py"))
if ($importResult.ExitCode -ne 0) { throw "Dependency smoke test failed" }

# Old and unowned environments are deliberately retained for rollback.


# V7.6+: game facts come from SearchAPI.io Google AI Mode. No retired gameplay knowledge
# stores or gameplay-index preparation belongs in the bootstrap path.
Write-Step "Preparing speech recognition models"
$speechResult = Invoke-NeoProcess $venvPython @((Join-Path $PSScriptRoot "prepare_speech_models.py"))
if ($speechResult.ExitCode -ne 0) { throw "Speech recognition model preparation failed" }

Write-Step "Configuring stable wake-word mode"
Write-Host "Stable mode uses the configured faster-whisper models. Experimental openWakeWord remains opt-in." -ForegroundColor DarkGray

Write-Step "Checking microphone enumeration"
try {
    $micResult = Invoke-NeoProcess $venvPython @((Join-Path $PSScriptRoot "_bootstrap_mic_probe.py"))
    if ($micResult.ExitCode -ne 0) {
        Write-Warning "Microphone probe returned code $($micResult.ExitCode)"
    }
} catch {
    Write-Warning "Microphone enumeration failed: $($_.Exception.Message)"
}

Write-Step "Checking native music application"
try {
    $musicResult = Invoke-NeoProcess $venvPython @((Join-Path $PSScriptRoot "_bootstrap_music_probe.py"))
    if ($musicResult.ExitCode -ne 0) {
        Write-Warning "Yandex Music desktop app was not detected. Music commands remain installed, but the native app must be installed before they can work."
    }
} catch {
    Write-Warning "Native music application probe failed: $($_.Exception.Message)"
}

Write-Step "Installation complete"
$markResult = Invoke-NeoProcess $venvPython @((Join-Path $PSScriptRoot 'dependency_guard.py'), '--mark-ok')
if ($markResult.ExitCode -ne 0) { throw 'Dependency contract validation failed; old runtime remains active.' }
$stateDir = Get-BootstrapStateDir
New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
$pointer = Join-Path $stateDir 'active_runtime.txt'
if (Test-Path -LiteralPath $pointer) { Copy-Item -LiteralPath $pointer -Destination (Join-Path $stateDir 'previous_runtime.txt') -Force }
$pointerTemp = Join-Path $stateDir 'active_runtime.tmp'
Set-Content -LiteralPath $pointerTemp -Value $venvRoot -Encoding UTF8
Move-Item -LiteralPath $pointerTemp -Destination $pointer -Force
$freeze = Invoke-NeoProcess $venvPython @('-m', 'pip', 'freeze', '--all')
if ($freeze.ExitCode -eq 0) { $freeze.Output | Set-Content -LiteralPath (Join-Path $venvRoot 'installed-versions.txt') -Encoding UTF8 }
$lock = Invoke-NeoProcess $venvPython @((Join-Path $PSScriptRoot 'runtime_lock.py'), '--runtime', $venvRoot)
if ($lock.ExitCode -ne 0) { Write-Warning 'Some pre-existing package hashes were unavailable. See runtime lock report; installation versions were still recorded.' }
Write-Host "You can now start NEO with NEO.cmd." -ForegroundColor Green
exit 0
