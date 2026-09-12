#Requires -Version 5.1
<#
    Controls the Konkour OCR pipeline from any PowerShell window.

    ocr                        show status
    ocr start                  boot API + dashboard, then open http://localhost:<PORT>
    ocr start -NoBrowser       same, without opening a browser tab
    ocr stop                   stop the server started by 'ocr start'
    ocr status                 running? on which port?
    ocr open                   just open the dashboard tab
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'status', 'open')]
    [string]$Command = 'status',

    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

$Root    = Split-Path -Parent $PSScriptRoot          # <project>\tools -> <project>
$Entry   = Join-Path $Root 'main.py'
$EnvFile = Join-Path $Root '.env'
$PidFile = Join-Path $env:LOCALAPPDATA 'ocr-quest.pid'

if (-not (Test-Path $Entry)) {
    throw "main.py not found under '$Root'. Is ocr.ps1 still at <project>\tools\ocr.ps1 ?"
}

function Get-Port {
    # An explicit $env:PORT wins, so 'ocr start' can be pointed somewhere else
    # without editing .env (load_dotenv never overrides a real environment value).
    if ($env:PORT -and $env:PORT -match '^\d+$') { return [int]$env:PORT }
    if (Test-Path $EnvFile) {
        foreach ($line in Get-Content $EnvFile) {
            if ($line -match '^\s*PORT\s*=\s*(\d+)') { return [int]$Matches[1] }
        }
    }
    8080                                             # same default as pipeline/config.py
}

function Get-PythonExe {
    $venv = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path $venv) { return $venv }
    $found = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    throw "No Python interpreter found. Run start.bat once, or: py -m venv '$Root\.venv'"
}

function Test-PortOpen {
    param([int]$Port, [int]$TimeoutMs = 1000)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $wait = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if ($wait.AsyncWaitHandle.WaitOne($TimeoutMs) -and $client.Connected) {
            $client.EndConnect($wait)
            return $true
        }
        return $false
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# The recorded PID only counts as ours when it is still the same process we
# launched (start time match, command line as fallback), so a recycled PID can
# never make us kill an unrelated program.
function Get-OcrProcess {
    if (-not (Test-Path $PidFile)) { return $null }
    $lines = @(Get-Content $PidFile -ErrorAction SilentlyContinue | ForEach-Object { "$_".Trim() })
    $id = 0
    if ($lines.Count -lt 1 -or -not [int]::TryParse($lines[0], [ref]$id)) { return $null }

    $candidate = Get-Process -Id $id -ErrorAction SilentlyContinue
    if (-not $candidate) { return $null }

    if ($lines.Count -ge 2) {
        try {
            $recorded = [datetime]::Parse($lines[1])
            if ([Math]::Abs(($candidate.StartTime - $recorded).TotalSeconds) -le 2) {
                return $candidate
            }
            return $null
        } catch {
            # StartTime not readable; fall through to the command-line check.
        }
    }

    $info = Get-CimInstance Win32_Process -Filter "ProcessId = $id" -ErrorAction SilentlyContinue
    if ($info -and $info.CommandLine -like '*main.py*') { return $candidate }
    return $null
}

$Port = Get-Port
$Url  = "http://localhost:$Port"

switch ($Command) {

    'start' {
        $running = Get-OcrProcess
        if ($running) {
            Write-Host "ocr: already running (PID $($running.Id)) -> $Url" -ForegroundColor Yellow
            if (-not $NoBrowser) { Start-Process $Url }
            break
        }
        Remove-Item $PidFile -ErrorAction SilentlyContinue

        if (Test-PortOpen -Port $Port) {
            Write-Host "ocr: port $Port is already served by a process I do not own. Not launching a second one." -ForegroundColor Red
            if (-not $NoBrowser) { Start-Process $Url }
            break
        }

        $python = Get-PythonExe
        Write-Host "ocr: starting pipeline from '$Root' ..." -ForegroundColor Cyan
        # A separate window keeps the run log (and Ctrl-C) available.
        $started = Start-Process -FilePath $python -ArgumentList "`"$Entry`"" `
                                 -WorkingDirectory $Root -PassThru
        @("$($started.Id)", "$($started.StartTime.ToString('o'))") |
            Set-Content -Path $PidFile -Encoding ASCII

        $deadline = (Get-Date).AddSeconds(30)
        $ready = $false
        while ((Get-Date) -lt $deadline) {
            if ($started.HasExited) { break }
            if (Test-PortOpen -Port $Port) { $ready = $true; break }
            Start-Sleep -Milliseconds 400
        }

        if ($ready) {
            Write-Host "ocr: up (PID $($started.Id)) -> $Url" -ForegroundColor Green
            if (-not $NoBrowser) { Start-Process $Url }
        } else {
            Write-Host "ocr: did not come up on port $Port - see the server window for the error." -ForegroundColor Red
            Remove-Item $PidFile -ErrorAction SilentlyContinue
        }
        break
    }

    'stop' {
        $running = Get-OcrProcess
        if (-not $running) {
            Write-Host 'ocr: not running.' -ForegroundColor Yellow
            break
        }
        Get-CimInstance Win32_Process -Filter "ParentProcessId = $($running.Id)" -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Stop-Process -Id $running.Id -Force
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        Write-Host "ocr: stopped (PID $($running.Id))." -ForegroundColor Green
        break
    }

    'status' {
        $running = Get-OcrProcess
        if ($running) {
            Write-Host "ocr: running (PID $($running.Id)) -> $Url" -ForegroundColor Green
        } elseif (Test-PortOpen -Port $Port) {
            Write-Host "ocr: $Url is answering, but not launched by 'ocr start'." -ForegroundColor Yellow
        } else {
            Write-Host "ocr: stopped ($Url closed)." -ForegroundColor DarkGray
        }
        break
    }

    'open' {
        if (Get-OcrProcess) {
            Start-Process $Url
        } else {
            Write-Host "ocr: server is not running. Type 'ocr start' first." -ForegroundColor Yellow
        }
        break
    }
}
