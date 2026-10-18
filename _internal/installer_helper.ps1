# NEO installer/uninstaller helper. Called hidden by NEO_Setup.exe and uninstall.exe.
#   -Action runtime             ensure the Python 3.12 runtime + dependencies (setup.ps1)
#   -Action model -Model <tag>  install/start Ollama and download the model
#   -Action neo_model           check the NEO command model (checksums, test run); rebuild it locally if damaged
#   -Action agent_model -Size 1.5b|0.5b   NEO 170: pull the small Qwen2.5 base and create the «neo-agent» model
#   -Action stop                ask a running NEO to quit, then stop leftover NEO processes (and their child processes)
#                               exit 5 = some NEO processes run as administrator and are still open (names in -Report)
#   -Action stop_admin          the same, then close those administrator NEO processes after the Windows (UAC) prompt
#   -Action holders             list the programs that still hold files in the NEO folders (Windows Restart Manager) in -Report
#   -Action remove -What <list> remove NEO-owned data: settings,logs,screenshots,cache,other,speech,model,ollama,python,runtime,startup
#                               'everything' = all of these plus NEO's data folders (clean uninstall)
#   -Action clean -Keep <list>  clean install: remove previous NEO files except settings,logs,screenshots,speech
#   -Action settings -Language xx [-ForceLanguage 1] -Autostart 0|1 -Priority 0|1   first-run language and startup choices
#                    [-ChatModel <tag>|none] [-TranslationModel <tag>|none] [-VoiceTier light|standard|heavy|none]
#   -Action chat_model -Model <tag>         NEO 185: the conversation model chosen in setup (Ollama)
#   -Action translation_model -Model <tag>  NEO 185: the translation model chosen in setup (Ollama)
#   -Action voice -Tier light|standard|heavy  NEO 185: the voice model chosen in setup
#   -Action lhm                             NEO 185: LibreHardwareMonitor (winget, else the official GitHub release zip)
param(
    [Parameter(Mandatory = $true)][ValidateSet('runtime', 'model', 'neo_model', 'agent_model', 'chat_model', 'translation_model', 'voice', 'lhm', 'stop', 'stop_admin', 'holders', 'remove', 'clean', 'settings')][string]$Action,
    [string]$ChatModel = '',
    [string]$TranslationModel = '',
    [ValidateSet('', 'none', 'light', 'standard', 'heavy')][string]$VoiceTier = '',
    [ValidateSet('light', 'standard', 'heavy')][string]$Tier = 'standard',
    [string]$Model = '',
    [ValidateSet('1.5b', '0.5b')][string]$Size = '1.5b',
    [string]$What = '',
    [string]$Keep = '',
    [string]$Language = 'en',
    [int]$Autostart = 0,
    [int]$Priority = 0,
    [int]$ForceLanguage = 0,
    [string]$InstallDir = '',
    [string]$InternalDir = '',
    [string]$Report = ''
)
$ErrorActionPreference = 'Stop'
$internal = $PSScriptRoot
# NEO 174: the uninstaller runs a copy of this script from its temp folder (the NEO folder is being deleted).
if ($InternalDir -and (Test-Path -LiteralPath $InternalDir)) { $internal = $InternalDir }
$localRoot = Join-Path $env:LOCALAPPDATA 'NeoJarvis'
$stateRoot = Join-Path $localRoot 'bootstrap'
$runtimeBase = Join-Path $localRoot 'runtime'
New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null

function Get-RuntimePython {
    $pointer = Join-Path $stateRoot 'active_runtime.txt'
    if (-not (Test-Path -LiteralPath $pointer)) { return $null }
    $runtime = (Get-Content -LiteralPath $pointer -Raw).Trim()
    $prefix = [IO.Path]::GetFullPath($runtimeBase).TrimEnd('\') + '\'
    if (-not [IO.Path]::GetFullPath($runtime).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return $null }
    $python = Join-Path $runtime 'Scripts\python.exe'
    if (Test-Path -LiteralPath $python) { return $python }
    return $null
}

function Get-OllamaExe {
    $command = Get-Command 'ollama.exe' -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { return [string]$command.Source }
    foreach ($path in @((Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'), (Join-Path $env:ProgramFiles 'Ollama\ollama.exe'), (Join-Path $env:LOCALAPPDATA 'Programs\Ollama-NEO\0.34.4\ollama.exe'))) {
        if (Test-Path -LiteralPath $path) { return $path }
    }
    return $null
}

# NEO 176: deletion that does not stop halfway. Read-only flags are cleared, long paths are
# handled, a locked file is retried, and whatever is still left is reported in the log.
$script:NotRemoved = New-Object System.Collections.ArrayList

function Remove-Tree([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $true }
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { Write-Warning "Skipped link: $Path"; return $true }
    } catch { }
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            if (Test-Path -LiteralPath $Path -PathType Container) {
                Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
                    try { if ($_.Attributes -band [IO.FileAttributes]::ReadOnly) { $_.Attributes = $_.Attributes -bxor [IO.FileAttributes]::ReadOnly } } catch { }
                }
            } else {
                try { $f = Get-Item -LiteralPath $Path -Force; if ($f.Attributes -band [IO.FileAttributes]::ReadOnly) { $f.Attributes = 'Normal' } } catch { }
            }
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $Path -PathType Container) {
                # Windows PowerShell 5.1 sometimes stops at «directory not empty»; rd handles long paths too.
                & cmd.exe /d /c "rd /s /q `"\\?\$Path`" >nul 2>&1"
            }
        } catch { }
        if (-not (Test-Path -LiteralPath $Path)) { return $true }
        Start-Sleep -Milliseconds (400 * $attempt)
    }
    [void]$script:NotRemoved.Add($Path)
    return $false
}

# NEO 176: every entry of NEO's data folder (%LOCALAPPDATA%\NeoJarvis) belongs to exactly one group,
# so the uninstaller and a clean install delete exactly the ticked groups. Anything NEO writes that
# is not listed here is «other».
function Get-DataGroup([string]$Name) {
    $n = $Name.ToLowerInvariant()
    if ($n -eq 'bootstrap') { return 'bootstrap' }
    if ($n -eq 'runtime') { return 'runtime' }
    # NEO 177: «settings» is the settings only (and their backups); chat history and memory are their own group.
    if ($n -match '^(settings\.json.*|settings\..*\.json|настройки|backups|overlays\.json.*)$') { return 'settings' }
    if ($n -match '^(assistant\.db.*|memory|history|conversations|shelf|adaptive_memory\.sqlite3.*|learned_phrases\.json.*|ambient_memory\.db.*|context_memory|objects\.sqlite3.*|scenarios.*)$') { return 'history' }
    if ($n -match '^(logs|diagnostics|reports|setup\.log.*|.*\.log(\.\d+)?)$') { return 'logs' }
    if ($n -match '^(screenshots|captures|clips)$') { return 'screenshots' }
    # NEO 179 speaks no more: old voices (models\tts, voice_packs, piper) go with the speech recognition group.
    if ($n -match '^(speech|voice_packs|piper|models|neo_piper.*\.zip|voice-runtime-.*|response_voice.*)$') { return 'speech' }
    if ($n -match '^(llm_train)$') { return 'model' }
    if ($n -match '^(cache|caches|tmp|temp|thumbnails|media_sessions|game_web|.*[_.-]cache|.*cache[_.-].*|yandex_wave_recent\.json.*|.*_history\.json.*|.*\.tmp|.*\.part)$') { return 'cache' }
    return 'other'
}

function Remove-DataGroups([string[]]$Groups, [string[]]$KeepMarkers = @('ollama_install.json', 'ollama_model.json', 'agent_model.json', 'translation_model.json', 'chat_model.json', 'lhm_install.json', 'python312_installed_by_jarvis.json', 'active_runtime.txt')) {
    if (-not (Test-Path -LiteralPath $localRoot)) { return }
    foreach ($entry in @(Get-ChildItem -LiteralPath $localRoot -Force -ErrorAction SilentlyContinue)) {
        $group = Get-DataGroup $entry.Name
        if ($group -eq 'bootstrap') {
            # Install markers say what NEO installed (Ollama, Python, models); only the ones still useful stay.
            # NEO's own logs/state in it are «other».
            if ($Groups -contains 'other') {
                Get-ChildItem -LiteralPath $entry.FullName -Force -ErrorAction SilentlyContinue | Where-Object {
                    $_.Name -notin $KeepMarkers
                } | ForEach-Object { [void](Remove-Tree $_.FullName) }
                if (-not @(Get-ChildItem -LiteralPath $entry.FullName -Force -ErrorAction SilentlyContinue).Count) { [void](Remove-Tree $entry.FullName) }
            }
            continue
        }
        if ($Groups -contains $group) {
            Write-Host "Removing ($group): $($entry.Name)"
            [void](Remove-Tree $entry.FullName)
        }
    }
}

function Write-NotRemoved {
    foreach ($path in $script:NotRemoved) { Write-Host "NOT REMOVED (in use?): $path" }
    if ($Report -and $script:NotRemoved.Count -gt 0) {
        try { Add-Content -LiteralPath $Report -Value ($script:NotRemoved -join ', ') -Encoding Unicode } catch { }
    }
}

function Invoke-Quiet([string]$CommandLine) {
    # Native tools write to stderr when there is nothing to do (schtasks: task not found).
    # Windows PowerShell 5.1 turned that into a terminating error and the whole removal stopped.
    try { & cmd.exe /d /c "$CommandLine >nul 2>&1" } catch { }
}

function Split-List([string]$Value) {
    return @($Value.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
}

# ---- NEO 174: find and close NEO's own processes -------------------------------------------
# A NEO process is one started from NEO's folders, a python/NEO process whose command line or
# loaded modules point into them, a process NEO recorded as its own (pid + start time), or a
# python/NEO child of any of these (workers run on the base Python interpreter outside NEO's
# folders). Never the uninstaller itself or its parents, never another program.
$neoNames = @('neo.exe', 'neo_wake_service.exe', 'python.exe', 'pythonw.exe')

function Get-NeoRoots {
    $roots = @([IO.Path]::GetFullPath($localRoot).TrimEnd('\') + '\')
    if ($InstallDir) { $roots += [IO.Path]::GetFullPath($InstallDir).TrimEnd('\') + '\' }
    return $roots
}

function Test-UnderRoots([string]$Path, $Roots) {
    if (-not $Path) { return $false }
    foreach ($root in $Roots) { if ($Path.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { return $true } }
    return $false
}

function Get-NeoOllamaServer($All) {
    # NEO 175: the Ollama server NEO started itself (pid recorded by NEO, process started at that time).
    # Its output goes to NEO's log files, so it keeps NEO's folder busy. The user's own Ollama is not touched.
    try {
        $record = Get-Content -LiteralPath (Join-Path $stateRoot 'ollama_server.json') -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($record.started_by -ne 'NEO' -or -not $record.pid) { return $null }
        $process = $All | Where-Object { [int]$_.ProcessId -eq [int]$record.pid -and ([string]$_.Name).ToLowerInvariant() -eq 'ollama.exe' } | Select-Object -First 1
        if (-not $process) { return $null }
        $started = [DateTime]::Parse([string]$record.started_at)
        if ($process.CreationDate -and [Math]::Abs(($process.CreationDate - $started).TotalSeconds) -gt 120) { return $null }
        return $process
    } catch { return $null }
}

function Get-RecordedNeoPids {
    $pids = @()
    foreach ($file in @((Join-Path $localRoot 'logs\session_active.json'), (Join-Path $localRoot 'instance\primary.json'))) {
        try {
            $record = Get-Content -LiteralPath $file -Raw -ErrorAction Stop | ConvertFrom-Json
            if ($record.pid) { $pids += [int]$record.pid }
        } catch { }
    }
    return $pids
}

function Get-OwnAncestors {
    $ids = @{}
    $current = $PID
    for ($i = 0; $i -lt 16 -and $current; $i++) {
        $ids[[int]$current] = $true
        $row = Get-CimInstance Win32_Process -Filter "ProcessId = $current" -ErrorAction SilentlyContinue
        if (-not $row) { break }
        $current = [int]$row.ParentProcessId
    }
    return $ids
}

function Find-NeoProcesses {
    $roots = Get-NeoRoots
    $skip = Get-OwnAncestors
    $recorded = @(Get-RecordedNeoPids)
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $byId = @{}; $children = @{}
    foreach ($p in $all) {
        $byId[[int]$p.ProcessId] = $p
        $parent = [int]$p.ParentProcessId
        if (-not $children.ContainsKey($parent)) { $children[$parent] = New-Object System.Collections.ArrayList }
        [void]$children[$parent].Add($p)
    }
    $found = @{}
    foreach ($p in $all) {
        $id = [int]$p.ProcessId
        if ($skip.ContainsKey($id)) { continue }
        $name = ([string]$p.Name).ToLowerInvariant()
        $mine = Test-UnderRoots ([string]$p.ExecutablePath) $roots
        if (-not $mine -and ($neoNames -contains $name)) {
            $cmd = [string]$p.CommandLine
            foreach ($root in $roots) { if ($cmd -and $cmd.IndexOf($root.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase) -ge 0) { $mine = $true; break } }
            if (-not $mine -and ($recorded -contains $id)) { $mine = $true }
            if (-not $mine) {
                try {
                    foreach ($module in (Get-Process -Id $id -ErrorAction Stop).Modules) {
                        if (Test-UnderRoots ([string]$module.FileName) $roots) { $mine = $true; break }
                    }
                } catch { }
            }
        }
        if ($mine) { $found[$id] = $p }
    }
    $ollama = Get-NeoOllamaServer $all
    if ($ollama -and -not $skip.ContainsKey([int]$ollama.ProcessId)) { $found[[int]$ollama.ProcessId] = $ollama }
    # Children (and their children) of NEO processes, started after their parent.
    $queue = New-Object System.Collections.Queue
    foreach ($id in @($found.Keys)) { $queue.Enqueue($id) }
    while ($queue.Count -gt 0) {
        $parentId = [int]$queue.Dequeue()
        if (-not $children.ContainsKey($parentId)) { continue }
        foreach ($child in $children[$parentId]) {
            $childId = [int]$child.ProcessId
            if ($found.ContainsKey($childId) -or $skip.ContainsKey($childId)) { continue }
            # Only NEO's own workers: programs the user opened through NEO (a browser, a game) stay open.
            if (-not ($neoNames -contains ([string]$child.Name).ToLowerInvariant())) { continue }
            $parent = $found[$parentId]
            if ($parent -and $child.CreationDate -and $parent.CreationDate -and $child.CreationDate -lt $parent.CreationDate) { continue }
            $found[$childId] = $child
            $queue.Enqueue($childId)
        }
    }
    return @($found.Values)
}

function Wait-NeoGone($Processes, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        $alive = @($Processes | Where-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
        if ($alive.Count -eq 0) { return @() }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    return $alive
}

function Write-NeoReport([string[]]$Lines) {
    if (-not $Report) { return }
    try { Set-Content -LiteralPath $Report -Value (($Lines | Select-Object -Unique) -join ', ') -Encoding Unicode } catch { }
}

function Stop-Neo {
    # 1. Ask NEO to quit on its own (it saves settings and releases its files).
    $python = Get-RuntimePython
    if ($python -and (Test-Path -LiteralPath $internal)) {
        Push-Location $internal
        try { & $python -c "from jarvis.runtime.instance_coordinator import InstanceCoordinator; InstanceCoordinator().request('quit_app', wait_ack=True, timeout=3)" 2>$null } catch { } finally { Pop-Location }
    }
    $processes = Find-NeoProcesses
    $alive = Wait-NeoGone $processes 10
    # 2. Whatever is still open is closed, children included (collected before anything is closed).
    if ($alive.Count -gt 0) {
        $processes = @(Find-NeoProcesses)
        foreach ($p in $processes) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        $alive = Wait-NeoGone $processes 6
    }
    return $alive
}

function Get-FileHolders([string[]]$Folders) {
    # Windows Restart Manager: which programs hold files in these folders (read-only query).
    if (-not ('NeoRm' -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class NeoRm {
    [StructLayout(LayoutKind.Sequential)] struct UNIQUE_PROCESS { public int dwProcessId; public System.Runtime.InteropServices.ComTypes.FILETIME ProcessStartTime; }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct PROCESS_INFO {
        public UNIQUE_PROCESS Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string strAppName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string strServiceShortName;
        public int ApplicationType; public uint AppStatus; public uint TSSessionId;
        [MarshalAs(UnmanagedType.Bool)] public bool bRestartable; }
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)] static extern int RmStartSession(out uint h, int f, string key);
    [DllImport("rstrtmgr.dll")] static extern int RmEndSession(uint h);
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)] static extern int RmRegisterResources(uint h, uint n, string[] files, uint a, IntPtr apps, uint s, string[] svc);
    [DllImport("rstrtmgr.dll")] static extern int RmGetList(uint h, out uint needed, ref uint count, [In, Out] PROCESS_INFO[] info, ref uint reasons);
    public static List<string> Holders(string[] files) {
        var result = new List<string>();
        uint handle; if (RmStartSession(out handle, 0, Guid.NewGuid().ToString()) != 0) return result;
        try {
            if (RmRegisterResources(handle, (uint)files.Length, files, 0, IntPtr.Zero, 0, null) != 0) return result;
            uint needed = 0, count = 0, reasons = 0;
            int rc = RmGetList(handle, out needed, ref count, null, ref reasons);
            if (rc == 234 && needed > 0) {
                var info = new PROCESS_INFO[needed]; count = needed;
                if (RmGetList(handle, out needed, ref count, info, ref reasons) == 0)
                    for (int i = 0; i < count; i++) result.Add(info[i].Process.dwProcessId + "|" + info[i].strAppName);
            }
        } finally { RmEndSession(handle); }
        return result;
    }
}
"@
    }
    $files = @()
    foreach ($folder in $Folders) {
        if ($folder -and (Test-Path -LiteralPath $folder)) {
            $files += @(Get-ChildItem -LiteralPath $folder -Recurse -File -Force -ErrorAction SilentlyContinue | Select-Object -First 3000 | ForEach-Object { $_.FullName })
        }
    }
    if ($files.Count -eq 0) { return @() }
    $holders = @()
    for ($i = 0; $i -lt $files.Count; $i += 500) {
        $batch = [string[]]($files[$i..([Math]::Min($i + 499, $files.Count - 1))])
        $holders += @([NeoRm]::Holders($batch))
    }
    return @($holders | Select-Object -Unique)
}
# ---------------------------------------------------------------------------------------------

# NEO 185: pull one Ollama model the user picked in setup and remember it for the uninstaller.
function Install-OllamaModel([string]$Name, [string]$MarkerName) {
    if (-not $Name -or $Name -eq 'none') { exit 0 }
    if ($Name -notmatch '^[A-Za-z0-9._/-]+(:[A-Za-z0-9._-]+)?$') { Write-Warning "Unsupported model name: $Name"; exit 2 }
    & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $internal 'bootstrap_ollama.ps1')
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Ollama could not be installed or started.'; exit 2 }
    $ollama = Get-OllamaExe
    if (-not $ollama) { exit 2 }
    $listed = (& $ollama list 2>$null) -join "`n"
    $already = $listed -match ('(?m)^' + [regex]::Escape($Name) + '\s')
    if (-not $already) {
        Write-Host "Downloading model $Name"
        & $ollama pull $Name
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        # Only a model NEO downloaded is removed by the uninstaller.
        $marker = Join-Path $stateRoot $MarkerName
        $models = @()
        if (Test-Path -LiteralPath $marker) { try { $models = @((Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json).models) } catch { $models = @() } }
        $models = @($models | Where-Object { $_ -and $_ -ne $Name }) + $Name
        @{ models = $models; pulled_by = 'NEO'; time = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8
    } else { Write-Host "Model $Name is already installed." }
    exit 0
}

switch ($Action) {
    'chat_model' { Install-OllamaModel $Model 'chat_model.json' }
    'translation_model' { Install-OllamaModel $Model 'translation_model.json' }
    'voice' {
        if ($Tier -eq 'light') { Write-Host 'Voice: Windows voices, nothing to download.'; exit 0 }
        & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $internal 'prepare_response_voice.ps1') -Tier $Tier
        exit $LASTEXITCODE
    }
    'lhm' {
        # LibreHardwareMonitor (MPL-2.0) lets NEO read temperatures and power sensors.
        $marker = Join-Path $stateRoot 'lhm_install.json'
        $winget = Get-Command 'winget.exe' -ErrorAction SilentlyContinue
        if ($winget) {
            & $winget.Source install --id LibreHardwareMonitor.LibreHardwareMonitor --exact --silent --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) {
                @{ method = 'winget'; id = 'LibreHardwareMonitor.LibreHardwareMonitor'; time = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8
                exit 0
            }
            Write-Warning "winget could not install LibreHardwareMonitor (exit $LASTEXITCODE); trying the release archive."
        }
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $release = Invoke-RestMethod -UseBasicParsing -Uri 'https://api.github.com/repos/LibreHardwareMonitor/LibreHardwareMonitor/releases/latest' -Headers @{ 'User-Agent' = 'NEO-Setup' }
            $asset = @($release.assets | Where-Object { $_.name -match '^LibreHardwareMonitor.*\.zip$' -and $_.browser_download_url -like 'https://github.com/LibreHardwareMonitor/*' }) | Select-Object -First 1
            if (-not $asset) { Write-Warning 'No LibreHardwareMonitor archive in the latest release.'; exit 2 }
            $target = Join-Path $env:LOCALAPPDATA 'Programs\LibreHardwareMonitor'
            $zip = Join-Path $env:TEMP ('neo-lhm-' + [guid]::NewGuid().ToString('N') + '.zip')
            Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $zip
            New-Item -ItemType Directory -Path $target -Force | Out-Null
            Expand-Archive -LiteralPath $zip -DestinationPath $target -Force
            Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
            $exe = Get-ChildItem -LiteralPath $target -Recurse -Filter 'LibreHardwareMonitor.exe' | Select-Object -First 1
            if ($exe) {
                $shell = New-Object -ComObject WScript.Shell
                $link = $shell.CreateShortcut((Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\LibreHardwareMonitor.lnk'))
                $link.TargetPath = $exe.FullName; $link.WorkingDirectory = $exe.DirectoryName; $link.Save()
            }
            @{ method = 'zip'; path = $target; version = [string]$release.tag_name; time = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $marker -Encoding UTF8
            exit 0
        } catch { Write-Warning "LibreHardwareMonitor: $($_.Exception.Message)"; exit 2 }
    }
    'clean' {
        # NEO 176: a clean install deletes every group of NEO's data except the ones the user keeps.
        $ErrorActionPreference = 'Continue'
        $keepItems = Split-List $Keep
        # Scheduled tasks and Startup entries always go; the chosen startup options are re-applied later.
        & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $PSCommandPath -Action remove -What 'startup'
        $groups = @('runtime', 'settings', 'history', 'logs', 'screenshots', 'speech', 'model', 'cache', 'other') | Where-Object { $keepItems -notcontains $_ }
        Remove-DataGroups $groups
        if ($keepItems -notcontains 'settings') { [void](Remove-Tree (Join-Path $env:APPDATA 'NeoJarvis')) }
        Write-NotRemoved
        exit 0
    }
    'settings' {
        New-Item -ItemType Directory -Path $localRoot -Force | Out-Null
        $settingsDir = Join-Path $localRoot 'настройки'
        New-Item -ItemType Directory -Path $settingsDir -Force | Out-Null
        $path = Join-Path $settingsDir 'settings.json'
        $data = [ordered]@{}
        $fresh = $true
        foreach ($candidate in @($path, (Join-Path $settingsDir 'settings.last-good.json'),
                                 (Join-Path $localRoot 'settings.json'), (Join-Path $settingsDir 'settings.migration-backup.json'))) {
            if (-not (Test-Path -LiteralPath $candidate)) { continue }
            try {
                $value = Get-Content -LiteralPath $candidate -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($null -eq $value -or ($value -isnot [PSCustomObject] -and $value -isnot [System.Collections.IDictionary])) { continue }
                $data = $value; $fresh = $false; break
            } catch { continue }
        }
        if (Test-Path -LiteralPath $path) {
            Copy-Item -LiteralPath $path -Destination ($path + '.before-setup-' + [guid]::NewGuid().ToString('N'))
        }
        if ($data -isnot [System.Collections.IDictionary]) {
            $table = [ordered]@{}
            foreach ($p in $data.PSObject.Properties) { $table[$p.Name] = $p.Value }
            $data = $table
        }
        function Get-Section($name) {
            $value = $data[$name]
            $table = [ordered]@{}
            if ($value -is [System.Collections.IDictionary]) { return $value }
            if ($null -ne $value) { foreach ($p in $value.PSObject.Properties) { $table[$p.Name] = $p.Value } }
            $data[$name] = $table
            return $table
        }
        if ($fresh -or $ForceLanguage) {
            # First start in the language picked in setup. An existing choice is kept
            # unless the user explicitly picked another language on the setup page.
            (Get-Section 'app')['language'] = $Language
            (Get-Section 'voice')['command_language'] = $Language
        }
        # NEO 185: the model choices from the setup page. Nothing the user declined is downloaded later
        # without asking; every model can still be downloaded in Settings.
        if ($ChatModel) {
            $chat = Get-Section 'local_chat'
            $ollamaSection = Get-Section 'ollama'
            $agent = Get-Section 'llm_agent'
            $agent['auto_install'] = $false
            if ($ChatModel -eq 'none') {
                # Conversation stays on the ready-made phrase atlas.
                $chat['enabled'] = $false
                $chat['live_small_talk'] = $false
                $agent['enabled'] = $false
            } else {
                $chat['enabled'] = $true
                $chat['live_small_talk'] = $true
                $agent['enabled'] = $true
                $chat['model'] = $ChatModel
                $ollamaSection['chat_model'] = $ChatModel
            }
            $ollamaSection['auto_pull_models'] = $false
        }
        if ($TranslationModel) {
            $translation = Get-Section 'translation'
            if ($TranslationModel -ne 'none') { $translation['model'] = $TranslationModel }
        }
        if ($VoiceTier) {
            $voiceSection = Get-Section 'response_voice'
            $voiceSection['enabled'] = [bool]($VoiceTier -ne 'none')
            if ($VoiceTier -ne 'none') { $voiceSection['tier'] = $VoiceTier }
        }
        $ui = Get-Section 'ui'
        $ui['start_with_windows'] = [bool]$Autostart
        $ui['priority_startup'] = [bool]($Autostart -and $Priority)
        $json = $data | ConvertTo-Json -Depth 32
        $temporary = $path + "." + [guid]::NewGuid().ToString("N") + ".tmp"
        [IO.File]::WriteAllText($temporary, $json, (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $path) { [IO.File]::Replace($temporary, $path, $null) }
        else { [IO.File]::Move($temporary, $path) }
        # Let NEO itself create/remove its Startup entry and priority task.
        $python = Get-RuntimePython
        if ($python) {
            Push-Location $internal
            try {
                & $python -c "from pathlib import Path; from jarvis.config import ConfigManager; ConfigManager(Path('config/default_settings.json')).apply_autostart()"
            } catch { Write-Warning $_.Exception.Message } finally { Pop-Location }
        }
        exit 0
    }
    'runtime' {
        $python = Get-RuntimePython
        if ($python) {
            Push-Location $internal
            try { & $python (Join-Path $internal 'dependency_guard.py') --check; $healthy = $LASTEXITCODE -eq 0 } finally { Pop-Location }
            if ($healthy) { Write-Host 'NEO runtime is already installed and healthy.'; exit 0 }
        }
        $env:NEO_JARVIS_HIDDEN_BATCH = '1'
        $env:NEO_VENV = Join-Path $runtimeBase 'v4.0.20'
        & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $internal 'setup.ps1')
        exit $LASTEXITCODE
    }
    'model' {
        & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $internal 'bootstrap_ollama.ps1')
        if ($LASTEXITCODE -ne 0) { Write-Warning 'Ollama is not available; NEO works without screen analysis.'; exit 2 }
        $ollama = Get-OllamaExe
        if (-not $ollama -or -not $Model) { exit 2 }
        $listed = (& $ollama list 2>$null) -join "`n"
        if ($listed -match [regex]::Escape($Model)) { Write-Host "Model $Model is already installed."; exit 0 }
        Write-Host "Downloading model $Model"
        & $ollama pull $Model
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        # NEO 169: report whether the installed model can analyse images (screen analysis) — checked, not assumed.
        $shown = (& $ollama show $Model 2>$null) -join "`n"
        if ($shown -match '(?m)^\s*vision\s*$') { Write-Host "Model $Model supports images: screen analysis available." }
        else { Write-Warning "Model $Model does not report image support; screen analysis may not work with it." }
        @{ model = $Model; pulled_by = 'NEO'; time = (Get-Date).ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stateRoot 'ollama_model.json') -Encoding UTF8
        exit 0
    }
    'neo_model' {
        # NEO 169: the NEO command model ships inside NEO; verify it and rebuild it locally if needed (no internet).
        $python = Get-RuntimePython
        if (-not $python) { Write-Warning 'The NEO Python runtime is not installed yet (run -Action runtime first).'; exit 2 }
        Push-Location $internal
        try {
            & $python -m jarvis.neo_model.check --install
            $code = $LASTEXITCODE
        } finally { Pop-Location }
        if ($code -eq 3) { Write-Host 'The NEO command model was rebuilt on this computer.'; exit 0 }
        exit $code
    }
    'agent_model' {
        # NEO 170: the small agent model (conversation and actions). Ollama first, then pull + create + a test answer.
        & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $internal 'bootstrap_ollama.ps1')
        if ($LASTEXITCODE -ne 0) { Write-Warning 'Ollama could not be installed or started.'; exit 2 }
        $python = Get-RuntimePython
        if (-not $python) { Write-Warning 'The NEO Python runtime is not installed yet (run -Action runtime first).'; exit 2 }
        Push-Location $internal
        try {
            & $python -m jarvis.neo_llm.setup --size $Size
            $code = $LASTEXITCODE
        } finally { Pop-Location }
        if ($code -eq 0) {
            @{ models = @('neo-agent', 'neo-agent-ft', ('qwen2.5:' + $Size)); pulled_by = 'NEO'; time = (Get-Date).ToString('o') } |
                ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stateRoot 'agent_model.json') -Encoding UTF8
        }
        exit $code
    }
    'stop' {
        $alive = @(Stop-Neo)
        if ($alive.Count -gt 0) {
            # Usually a NEO started with administrator rights («Priority» mode): Windows does not
            # let a normal program close it. The uninstaller asks the user (exit 5).
            Write-NeoReport @($alive | ForEach-Object { "$($_.Name) ($($_.ProcessId))" })
            exit 5
        }
        exit 0
    }
    'stop_admin' {
        $alive = @(Stop-Neo)
        if ($alive.Count -gt 0) {
            $ids = ($alive | ForEach-Object { [int]$_.ProcessId }) -join ','
            # Visible Windows (UAC) prompt; only the NEO processes found above are closed.
            try {
                $p = Start-Process -FilePath 'powershell.exe' -Verb RunAs -WindowStyle Hidden -Wait -PassThru `
                    -ArgumentList "-NoLogo -NoProfile -NonInteractive -Command Stop-Process -Id $ids -Force -ErrorAction SilentlyContinue"
            } catch { Write-Warning 'The administrator prompt was declined.' }
            $alive = @(Wait-NeoGone $alive 6)
        }
        if ($alive.Count -gt 0) { Write-NeoReport @($alive | ForEach-Object { "$($_.Name) ($($_.ProcessId))" }); exit 5 }
        exit 0
    }
    'holders' {
        $folders = @($localRoot)
        if ($InstallDir) { $folders += $InstallDir }
        $holders = @()
        try { $holders = @(Get-FileHolders $folders) } catch { Write-Warning $_.Exception.Message }
        # NEO processes among them are closed right away; other programs are named for the user.
        $neo = @(Find-NeoProcesses | ForEach-Object { [int]$_.ProcessId })
        $others = @()
        foreach ($row in $holders) {
            $parts = $row.Split('|', 2)
            $holderId = [int]$parts[0]
            $holderName = ''
            try { $holderName = ([string](Get-Process -Id $holderId -ErrorAction Stop).ProcessName).ToLowerInvariant() } catch { }
            # An Ollama server holding NEO's files was started by NEO (its log files are NEO's); the
            # Ollama app restarts its server when it is needed again.
            if (($neo -contains $holderId) -or $holderName -eq 'ollama') { Stop-Process -Id $holderId -Force -ErrorAction SilentlyContinue }
            else { $others += "$($parts[1]) ($($parts[0]))" }
        }
        if ($others.Count -eq 0 -and $holders.Count -gt 0) { Start-Sleep -Milliseconds 800 }
        if ($others.Count -gt 0) { Write-NeoReport $others; exit 5 }
        exit 0
    }
    'remove' {
        # NEO 176: one failing step (or a tool writing to stderr) never stops the rest.
        $ErrorActionPreference = 'Continue'
        $items = @($What.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })
        $everything = $items -contains 'everything'
        if ($everything) {
            $items += @('startup', 'runtime', 'settings', 'history', 'logs', 'screenshots', 'cache', 'other', 'speech', 'model', 'ollama', 'python', 'lhm')
        }
        if ($items -contains 'startup') {
            foreach ($task in @('NEO Priority', 'NEO JARVIS Priority', 'NEO Wake Service', 'NEO JARVIS Wake Service')) {
                Invoke-Quiet "schtasks.exe /Delete /F /TN `"$task`""
            }
            $startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
            foreach ($name in @('NEO.vbs', 'NEO_JARVIS.vbs', 'NEO.cmd', 'NEO_JARVIS.cmd', 'NEO_WAKE.vbs', 'NEO.lnk', 'NEO JARVIS.lnk', 'priority_launch.vbs', 'priority_task.json', 'wake_service.json', 'NEO_JARVIS_WAKE.vbs')) {
                Remove-Item -LiteralPath (Join-Path $startup $name) -Force -ErrorAction SilentlyContinue
            }
        }
        if ($items -contains 'model') {
            try {
                $ollama = Get-OllamaExe
                $marker = Join-Path $stateRoot 'ollama_model.json'
                if ($ollama -and (Test-Path -LiteralPath $marker)) {
                    $name = (Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json).model
                    & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $internal 'bootstrap_ollama.ps1') -NoInstall | Out-Null
                    if ($name) { Invoke-Quiet "`"$ollama`" rm `"$name`"" }
                }
                # NEO 170: the agent model, its fine-tuned version and the base NEO pulled.
                $agentMarker = Join-Path $stateRoot 'agent_model.json'
                if ($ollama -and (Test-Path -LiteralPath $agentMarker)) {
                    foreach ($name in (Get-Content -LiteralPath $agentMarker -Raw | ConvertFrom-Json).models) { if ($name) { Invoke-Quiet "`"$ollama`" rm `"$name`"" } }
                }
                # NEO 185: the conversation model chosen in setup.
                $chatMarker = Join-Path $stateRoot 'chat_model.json'
                if ($ollama -and (Test-Path -LiteralPath $chatMarker)) {
                    foreach ($name in (Get-Content -LiteralPath $chatMarker -Raw | ConvertFrom-Json).models) { if ($name) { Invoke-Quiet "`"$ollama`" rm `"$name`"" } }
                }
                # NEO 184: the translation model NEO downloaded (Settings → Translation or «да» in the chat).
                $translationMarker = Join-Path $stateRoot 'translation_model.json'
                if ($ollama -and (Test-Path -LiteralPath $translationMarker)) {
                    foreach ($name in (Get-Content -LiteralPath $translationMarker -Raw | ConvertFrom-Json).models) { if ($name) { Invoke-Quiet "`"$ollama`" rm `"$name`"" } }
                }
            } catch { Write-Warning "Model removal: $($_.Exception.Message)" }
        }
        if ($items -contains 'lhm') {
            try {
                $lhmMarker = Join-Path $stateRoot 'lhm_install.json'
                if (Test-Path -LiteralPath $lhmMarker) {
                    $info = Get-Content -LiteralPath $lhmMarker -Raw | ConvertFrom-Json
                    Get-Process -Name 'LibreHardwareMonitor' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
                    if ($info.method -eq 'winget') {
                        $winget = Get-Command 'winget.exe' -ErrorAction SilentlyContinue
                        if ($winget) { & $winget.Source uninstall --id LibreHardwareMonitor.LibreHardwareMonitor --exact --silent | Out-Null }
                    } elseif ($info.path -and ([IO.Path]::GetFullPath([string]$info.path)).StartsWith([IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs')), [StringComparison]::OrdinalIgnoreCase)) {
                        [void](Remove-Tree ([string]$info.path))
                        Remove-Item -LiteralPath (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\LibreHardwareMonitor.lnk') -Force -ErrorAction SilentlyContinue
                    }
                    Remove-Item -LiteralPath $lhmMarker -Force -ErrorAction SilentlyContinue
                }
            } catch { Write-Warning "LibreHardwareMonitor removal: $($_.Exception.Message)" }
        }
        if ($items -contains 'ollama') {
            try {
                if (Test-Path -LiteralPath (Join-Path $stateRoot 'ollama_install.json')) {
                    Get-Process -Name 'ollama', 'ollama app' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
                    $uninstaller = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\unins000.exe'
                    if (Test-Path -LiteralPath $uninstaller) {
                        Start-Process -FilePath $uninstaller -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART' -Wait -WindowStyle Hidden
                    }
                }
            } catch { Write-Warning "Ollama removal: $($_.Exception.Message)" }
        }
        if ($items -contains 'python') {
            try {
                if (Test-Path -LiteralPath (Join-Path $stateRoot 'python312_installed_by_jarvis.json')) {
                    $keys = Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall' -ErrorAction SilentlyContinue
                    foreach ($key in $keys) {
                        $p = Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
                        if ($p.DisplayName -like 'Python 3.12.*(64-bit)' -and $p.BundleCachePath) {
                            Start-Process -FilePath $p.BundleCachePath -ArgumentList '/uninstall /quiet' -Wait -WindowStyle Hidden
                        }
                    }
                }
            } catch { Write-Warning "Python removal: $($_.Exception.Message)" }
        }
        if ($items -contains 'speech') {
            $hub = Join-Path $env:USERPROFILE '.cache\huggingface\hub'
            if (Test-Path -LiteralPath $hub) {
                Get-ChildItem -LiteralPath $hub -Directory -Filter 'models--Systran--faster-whisper-*' -ErrorAction SilentlyContinue | ForEach-Object { [void](Remove-Tree $_.FullName) }
            }
        }
        # NEO's data folder: exactly the ticked groups. NEO itself is gone after an uninstall, so a marker
        # stays only for something NEO installed that is still on the computer (a later full uninstall of a
        # reinstalled NEO can still remove it).
        $markers = @()
        if ($items -notcontains 'ollama') { $markers += 'ollama_install.json' }
        if ($items -notcontains 'model') { $markers += @('ollama_model.json', 'agent_model.json', 'translation_model.json', 'chat_model.json') }
        if ($items -notcontains 'lhm') { $markers += 'lhm_install.json' }
        if ($items -notcontains 'python') { $markers += 'python312_installed_by_jarvis.json' }
        Remove-DataGroups @($items | Where-Object { $_ -in @('runtime', 'settings', 'history', 'logs', 'screenshots', 'speech', 'model', 'cache', 'other') }) $markers
        if ($items -contains 'settings') { [void](Remove-Tree (Join-Path $env:APPDATA 'NeoJarvis')) }
        if ($everything) {
            # Ollama's model store goes too, but only when NEO installed Ollama itself.
            if (Test-Path -LiteralPath (Join-Path $stateRoot 'ollama_install.json')) { [void](Remove-Tree (Join-Path $env:USERPROFILE '.ollama')) }
            [void](Remove-Tree $localRoot)
            [void](Remove-Tree (Join-Path $env:APPDATA 'NeoJarvis'))
        }
        Write-NotRemoved
        exit 0
    }
}
