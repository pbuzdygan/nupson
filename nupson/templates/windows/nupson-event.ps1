[CmdletBinding()]
param(
    [ValidateSet("ONLINE", "ONBATT")]
    [string]$EventType = $env:NOTIFYTYPE,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ClientDirectory = Join-Path $env:ProgramData "NUPSON\Client"
$ManifestPath = Join-Path $ClientDirectory "nupson-client.json"
$MarkerPath = Join-Path $ClientDirectory "shutdown.pending"
$LogPath = Join-Path $ClientDirectory "client-events.log"
$ShutdownExe = Join-Path $env:SystemRoot "System32\shutdown.exe"
$MarkerGraceSeconds = 60

function Write-ClientLog {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -LiteralPath $LogPath -Value "[$timestamp] $Message" -Encoding UTF8
}


function Get-BootTimeUtc {
    try {
        return (Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop).LastBootUpTime.ToUniversalTime().ToString("o")
    }
    catch {
        Write-ClientLog "WARN: Could not read the Windows boot time: $($_.Exception.Message)"
        return $null
    }
}

function Test-PendingShutdownMarker {
    if (-not (Test-Path -LiteralPath $MarkerPath)) {
        return $false
    }
    try {
        $marker = Get-Content -LiteralPath $MarkerPath -Raw | ConvertFrom-Json
        $createdAt = [DateTimeOffset]::Parse([string]$marker.createdAt)
        $expiresAt = $createdAt.AddSeconds([int]$marker.delaySeconds + $MarkerGraceSeconds)
        $currentBootTime = Get-BootTimeUtc
        $markerBootTime = [string]$marker.bootTimeUtc
        $differentBoot = $markerBootTime -and $currentBootTime -and $markerBootTime -ne $currentBootTime
        $expired = [DateTimeOffset]::UtcNow -gt $expiresAt
        if ($differentBoot -or $expired) {
            $reason = if ($differentBoot) { "a previous Windows boot" } else { "an expired timer" }
            Remove-Item -LiteralPath $MarkerPath -Force
            Write-ClientLog "Removed stale shutdown marker from $reason."
            return $false
        }
        return $true
    }
    catch {
        Remove-Item -LiteralPath $MarkerPath -Force -ErrorAction SilentlyContinue
        Write-ClientLog "Removed invalid shutdown marker: $($_.Exception.Message)"
        return $false
    }
}

if (-not (Test-Path -LiteralPath $ManifestPath)) {
    throw "NUPSON client manifest is missing: $ManifestPath"
}
$manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
if ($manifest.shutdownBackend -ne "windows-native-timer") {
    throw "Unsupported NUPSON shutdown backend: $($manifest.shutdownBackend)"
}
if ($EventType -notin @("ONBATT", "ONLINE")) {
    Write-ClientLog "Ignored notification type '$EventType'."
    return
}

$mutex = New-Object System.Threading.Mutex($false, "Global\NUPSONClientShutdown")
$locked = $false
try {
    $locked = $mutex.WaitOne([TimeSpan]::FromSeconds(10))
    if (-not $locked) {
        throw "Timed out waiting for the NUPSON shutdown lock."
    }

    if ($EventType -eq "ONBATT") {
        if ($manifest.policy -notin @("timer", "immediate")) {
            Write-ClientLog "ONBATT received; policy '$($manifest.policy)' does not schedule shutdown."
            return
        }
        if ($manifest.policy -eq "timer" -and (Test-PendingShutdownMarker)) {
            Write-ClientLog "ONBATT received; an existing NUPSON shutdown timer remains active."
            return
        }

        $delay = if ($manifest.policy -eq "immediate") { 0 } else { [int]$manifest.delaySeconds }
        if ($DryRun) {
            Write-ClientLog "DRY RUN: would schedule shutdown in $delay seconds."
            return
        }

        if ($delay -gt 0) {
            @{
                createdAt = (Get-Date).ToUniversalTime().ToString("o")
                delaySeconds = $delay
                profileName = [string]$manifest.profileName
                bootTimeUtc = Get-BootTimeUtc
            } | ConvertTo-Json | Set-Content -LiteralPath $MarkerPath -Encoding UTF8
        }

        & $ShutdownExe /s /f /t $delay /c "NUPSON: UPS is running on battery"
        if ($LASTEXITCODE -ne 0) {
            Remove-Item -LiteralPath $MarkerPath -Force -ErrorAction SilentlyContinue
            throw "shutdown.exe failed with exit code $LASTEXITCODE."
        }
        Write-ClientLog "ONBATT scheduled Windows shutdown in $delay seconds."
        return
    }

    if (-not (Test-Path -LiteralPath $MarkerPath)) {
        Write-ClientLog "ONLINE received; no NUPSON shutdown timer is pending."
        return
    }
    if ($DryRun) {
        Write-ClientLog "DRY RUN: would cancel the pending NUPSON shutdown."
        return
    }

    & $ShutdownExe /a
    $abortExitCode = $LASTEXITCODE
    Remove-Item -LiteralPath $MarkerPath -Force -ErrorAction SilentlyContinue
    if ($abortExitCode -notin @(0, 1116)) {
        throw "shutdown.exe /a failed with exit code $abortExitCode."
    }
    Write-ClientLog "ONLINE cancelled the pending NUPSON shutdown."
}
catch {
    Write-ClientLog "ERROR: $($_.Exception.Message)"
    throw
}
finally {
    if ($locked) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
