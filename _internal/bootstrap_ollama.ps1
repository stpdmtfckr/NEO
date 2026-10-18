# NEO 184: official standalone Ollama CLI; install and server start are separate.
# Sources: https://docs.ollama.com/windows#standalone-cli
# https://github.com/ollama/ollama/releases/expanded_assets/v0.34.4
# The GUI installer has an unconditional [Run] action; silent flags do NOT suppress it.
param([switch]$InstallOnly, [switch]$Foreground, [switch]$NoInstall, [switch]$IPv6,
      [switch]$Cancel, [ValidateRange(1024,65535)][int]$Port = 11434)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$stateRoot = Join-Path $env:LOCALAPPDATA 'NeoJarvis\bootstrap'
New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
$cancelFile = Join-Path $stateRoot 'ollama_bootstrap.cancel'
$statusFile = Join-Path $stateRoot 'ollama_status.json'
$logFile = Join-Path $stateRoot 'ollama_bootstrap.log'
if ($Cancel) { [IO.File]::WriteAllText($cancelFile, 'cancel'); Write-Host 'Ollama setup cancellation requested.'; exit 0 }

function Write-Status([string]$State, [string]$Reason, [int]$Percent = -1) {
    # No settings, tokens, request bodies or personal filesystem paths in status/logs.
    $row = @{ state = $State; reason = $Reason; percent = $Percent; updated_at = (Get-Date).ToString('o'); model_downloaded = $false }
    $temporary = $statusFile + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($temporary, ($row | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $statusFile) { [IO.File]::Replace($temporary, $statusFile, $null) }
    else { [IO.File]::Move($temporary, $statusFile) }
    $text = (Get-Date).ToString('s') + ' ' + $State + ' ' + $Reason + ' ' + $Percent
    if ((Test-Path -LiteralPath $logFile) -and (Get-Item -LiteralPath $logFile).Length -gt 1048576) {
        Move-Item -LiteralPath $logFile -Destination ($logFile + '.previous') -Force
    }
    Add-Content -LiteralPath $logFile -Value $text -Encoding UTF8
    if ($Foreground) { Write-Host $text }
}
function Test-Cancel {
    if (Test-Path -LiteralPath $cancelFile) { throw [OperationCanceledException]::new('cancelled') }
}
function Read-OllamaOptions {
    $root = Join-Path $env:LOCALAPPDATA 'NeoJarvis'
    $settingsDir = Join-Path $root 'настройки'
    $sawProfile = $false
    foreach ($path in @((Join-Path $settingsDir 'settings.json'), (Join-Path $settingsDir 'settings.last-good.json'),
                         (Join-Path $root 'settings.json'), (Join-Path $settingsDir 'settings.migration-backup.json'))) {
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $sawProfile = $true
        try {
            $data = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($null -eq $data -or ($data -isnot [PSCustomObject] -and $data -isnot [System.Collections.IDictionary])) { continue }
            $start = $true
            if ($null -ne $data.ollama.auto_start) { $start = $data.ollama.auto_start -is [bool] -and $data.ollama.auto_start }
            $url = if ($data.ollama.base_url) { [string]$data.ollama.base_url } else { 'http://localhost:11434' }
            return @{ auto_start = $start; base_url = $url }
        } catch { continue }
    }
    return @{ auto_start = (-not $sawProfile); base_url = 'http://localhost:11434' }
}
function Get-OllamaExe {
    $command = Get-Command 'ollama.exe' -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { return [string]$command.Source }
    foreach ($path in @((Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
                       (Join-Path $env:LOCALAPPDATA 'Ollama\ollama.exe'),
                       (Join-Path $env:ProgramFiles 'Ollama\ollama.exe'),
                       (Join-Path $env:LOCALAPPDATA 'Programs\Ollama-NEO\0.34.4\ollama.exe'))) {
        if (Test-Path -LiteralPath $path -PathType Leaf) { return $path }
    }
    return $null
}
function Test-OllamaApi {
    $response = $null; $reader = $null
    try {
        $request = [Net.HttpWebRequest]::Create($script:apiBase + '/api/version')
        $request.Proxy = $null; $request.AllowAutoRedirect = $false
        $request.Timeout = 1500; $request.ReadWriteTimeout = 1500
        $response = $request.GetResponse()
        $reader = [IO.StreamReader]::new($response.GetResponseStream())
        $payload = $reader.ReadToEnd() | ConvertFrom-Json
        return [bool]$payload.version
    } catch { return $false }
    finally { if ($reader) { $reader.Dispose() }; if ($response) { $response.Close() } }
}
function Get-OfficialArchive([string]$Url, [string]$Destination) {
    $request = [Net.HttpWebRequest]::Create($Url)
    $request.Timeout = 30000; $request.ReadWriteTimeout = 30000
    $request.UserAgent = 'NEO-Ollama-Bootstrap/184'
    $response = $null; $inputStream = $null; $outputStream = $null
    try {
        $response = $request.GetResponse()
        # Only the official GitHub release/CDN may supply the binary.
        $final = $response.ResponseUri
        if ($final.Scheme -ne 'https' -or $final.Host -notin @('github.com','release-assets.githubusercontent.com','objects.githubusercontent.com')) {
            throw [Security.SecurityException]::new('download_origin_rejected')
        }
        $inputStream = $response.GetResponseStream()
        $outputStream = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew)
        $buffer = New-Object byte[] 1048576
        $received = 0L; $last = -10; $deadline = (Get-Date).AddMinutes(30)
        while (($count = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            Test-Cancel
            if ((Get-Date) -gt $deadline -or $received -gt 8GB) { throw [TimeoutException]::new('download_limit') }
            $outputStream.Write($buffer, 0, $count); $received += $count
            $percent = if ($response.ContentLength -gt 0) { [int](100 * $received / $response.ContentLength) } else { -1 }
            if ($percent -ge $last + 5) { Write-Status 'installing' 'downloading_official_cli' $percent; $last = $percent }
        }
    } finally {
        if ($outputStream) { $outputStream.Dispose() }; if ($inputStream) { $inputStream.Dispose() }; if ($response) { $response.Close() }
    }
}

$mutex = $null; $held = $false; $stage = $null; $archive = $null
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $mutex = [Threading.Mutex]::new($false, ('Local\NEO-Ollama-Bootstrap-' + $identity))
    try { $held = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) { if ($Foreground) { Write-Host 'Ollama setup is already running.' }; exit 0 }
    Remove-Item -LiteralPath $cancelFile -Force -ErrorAction SilentlyContinue
    $options = Read-OllamaOptions
    $initialEndpoint = [string]$options.base_url
    $endpoint = $null
    if (-not [Uri]::TryCreate([string]$options.base_url, [UriKind]::Absolute, [ref]$endpoint) -or
        $endpoint.Scheme -ne 'http' -or $endpoint.Host -notin @('localhost','127.0.0.1','[::1]','::1') -or
        $endpoint.UserInfo -or $endpoint.Query -or $endpoint.Fragment -or $endpoint.AbsolutePath -ne '/') {
        Write-Status 'skipped' 'nonlocal_or_invalid_endpoint'; exit 0
    }
    # Explicit legacy -Port/-IPv6 calls and configured local ports remain compatible.
    $effectivePort = if ($PSBoundParameters.ContainsKey('Port')) { $Port } elseif ($endpoint.IsDefaultPort) { 11434 } else { $endpoint.Port }
    $loopback = if ($IPv6 -or $endpoint.Host -in @('[::1]','::1')) { '[::1]' } else { '127.0.0.1' }
    $script:apiBase = 'http://' + $loopback + ':' + $effectivePort
    if (Test-OllamaApi) { Write-Status 'ready' 'existing_server_reused'; exit 0 }
    $executable = Get-OllamaExe
    if (-not $executable -and $NoInstall) { Write-Status 'unavailable' 'not_installed'; exit 2 }
    if (-not $executable) {
        # A currently running user app may use a different port. Never overwrite its installation.
        if (Get-Process -Name 'ollama','ollama app' -ErrorAction SilentlyContinue) {
            Write-Status 'unavailable' 'existing_process_location_unknown'; exit 2
        }
        $version = '0.34.4'
        $architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
        if ($architecture -eq 'ARM64') {
            $asset = 'ollama-windows-arm64.zip'
            $expected = 'b49aa49306da9bae5b7498f320f76bd2a13739c3105bcb6e115a26f0dfd10d67'
        } elseif ($architecture -eq 'AMD64') {
            $asset = 'ollama-windows-amd64.zip'
            $expected = '535193f38f3344e5b08f5d1c171c31ce11aa17f0124ff69ae26d8ec7fe06fa62'
        } else { Write-Status 'unavailable' 'unsupported_architecture'; exit 2 }
        $installBase = Join-Path $env:LOCALAPPDATA 'Programs\Ollama-NEO'
        New-Item -ItemType Directory -Path $installBase -Force | Out-Null
        $destination = Join-Path $installBase $version
        $stage = Join-Path $installBase ('.staging-' + [guid]::NewGuid().ToString('N'))
        $archive = Join-Path $installBase ('.download-' + [guid]::NewGuid().ToString('N') + '.zip')
        Write-Status 'installing' 'downloading_official_cli' 0
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Get-OfficialArchive ('https://github.com/ollama/ollama/releases/download/v' + $version + '/' + $asset) $archive
        Test-Cancel
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
            throw [Security.SecurityException]::new('checksum_failed')
        }
        Write-Status 'installing' 'extracting_verified_cli' 100
        New-Item -ItemType Directory -Path $stage | Out-Null
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [IO.Compression.ZipFile]::ExtractToDirectory($archive, $stage)
        Test-Cancel
        if (-not (Test-Path -LiteralPath (Join-Path $stage 'ollama.exe'))) { throw [IO.InvalidDataException]::new('missing_cli') }
        if (Test-Path -LiteralPath $destination) {
            # Preserve partial/older content instead of destroying it during repair.
            Move-Item -LiteralPath $destination -Destination ($destination + '.recovery-' + [guid]::NewGuid().ToString('N'))
        }
        [IO.Directory]::Move($stage, $destination); $stage = $null
        $executable = Join-Path $destination 'ollama.exe'
        @{ installed_by = 'NEO'; version = $version; distribution = 'official_standalone_cli'; sha256 = $expected; shared_dependency = $true } |
            ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stateRoot 'ollama_install.json') -Encoding UTF8
    }
    Test-Cancel
    # Re-read after a long download: switching autostart off takes effect before server start.
    $options = Read-OllamaOptions
    if ([string]$options.base_url -ne $initialEndpoint) { Write-Status 'skipped' 'endpoint_changed_during_install'; exit 0 }
    if ($InstallOnly -or -not $options.auto_start) { Write-Status 'installed' 'autostart_disabled_or_install_only'; exit 0 }
    if (Test-OllamaApi) { Write-Status 'ready' 'existing_server_reused'; exit 0 }
    $env:OLLAMA_HOST = $loopback + ':' + $effectivePort
    $env:OLLAMA_NO_CLOUD = '1'  # Only this newly spawned NEO-owned server; existing servers are untouched.
    $env:OLLAMA_KEEP_ALIVE = '0'; $env:OLLAMA_MAX_LOADED_MODELS = '1'; $env:OLLAMA_NUM_PARALLEL = '1'
    $stdoutLog = Join-Path $stateRoot 'ollama-server.log'; $stderrLog = Join-Path $stateRoot 'ollama-server-error.log'
    $server = Start-Process -FilePath $executable -ArgumentList 'serve' -WorkingDirectory (Split-Path -Parent $executable) -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
    @{ pid = $server.Id; started_by = 'NEO'; executable = $executable; started_at = (Get-Date).ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stateRoot 'ollama_server.json') -Encoding UTF8
    Write-Status 'starting' 'hidden_cli_server'
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        Test-Cancel
        if (Test-OllamaApi) { Write-Status 'ready' 'local_api_verified'; exit 0 }
        $server.Refresh(); if ($server.HasExited) { break }
        Start-Sleep -Milliseconds 350
    }
    Write-Status 'unavailable' 'startup_timeout'; exit 2
} catch [OperationCanceledException] {
    # Cancellation never kills a shared or already started server.
    Write-Status 'cancelled' 'user_cancelled'; exit 3
} catch {
    # Exceptions may contain proxy credentials or personal paths: record only safe classification.
    $reason = if ($_.Exception -is [Net.WebException]) { 'download_network_error' } elseif ($_.Exception -is [Security.SecurityException]) { 'verification_failed' } else { 'setup_failed' }
    Write-Status 'unavailable' $reason
    if ($Foreground) { Write-Warning 'Ollama setup did not finish. See the NEO bootstrap status; retry or use https://ollama.com/download.' }
    exit 2
} finally {
    if ($archive -and (Test-Path -LiteralPath $archive)) { Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue }
    if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
    if ($held -and $mutex) { $mutex.ReleaseMutex() }; if ($mutex) { $mutex.Dispose() }
}
