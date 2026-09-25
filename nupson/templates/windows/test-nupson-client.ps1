[CmdletBinding()]
param([switch]$DryRunEvents)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ServiceName = "Network UPS Tools"
$InstallRoot = Join-Path $env:SystemDrive "NUPSON\NUT"
$ClientDirectory = Join-Path $env:ProgramData "NUPSON\Client"
$ConfigDirectory = Join-Path $InstallRoot "etc"
$Failures = New-Object System.Collections.Generic.List[string]

function Test-Item {
    param([bool]$Condition, [string]$Success, [string]$Failure)
    if ($Condition) {
        Write-Host "[OK] $Success" -ForegroundColor Green
    }
    else {
        Write-Host "[FAIL] $Failure" -ForegroundColor Red
        $Failures.Add($Failure)
    }
}


function Invoke-CapturedProcess {
    param([string]$FilePath, [string[]]$ArgumentList)
    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath $FilePath `
            -ArgumentList $ArgumentList `
            -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        return [PSCustomObject]@{
            ExitCode = $process.ExitCode
            StandardOutput = ([string](Get-Content -LiteralPath $stdoutPath -Raw)).Trim()
            StandardError = ([string](Get-Content -LiteralPath $stderrPath -Raw)).Trim()
        }
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function ConvertTo-NormalizedAddress {
    param([string]$Address)
    $parsedAddress = $null
    if ([Net.IPAddress]::TryParse($Address, [ref]$parsedAddress)) {
        if ($parsedAddress.IsIPv4MappedToIPv6) {
            return $parsedAddress.MapToIPv4().ToString()
        }
        return $parsedAddress.ToString()
    }
    return $Address.Trim().ToLowerInvariant()
}

$manifestPath = Join-Path $ClientDirectory "nupson-client.json"
Test-Item (Test-Path -LiteralPath $manifestPath) "Client manifest exists." "Client manifest is missing."
if (-not (Test-Path -LiteralPath $manifestPath)) {
    exit 1
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
Test-Item ($null -ne $service) "NUT service is registered." "NUT service is not registered."
if ($service) {
    Test-Item ($service.Status -eq "Running") "NUT service is running." "NUT service is not running."
}

$upsmonProcesses = @(Get-Process -Name "upsmon" -ErrorAction SilentlyContinue)
Test-Item ($upsmonProcesses.Count -gt 0) `
    "upsmon.exe is running." `
    "upsmon.exe is not running; inspect the Windows Application event log for NUT/upsmon errors."

$nutConf = Join-Path $ConfigDirectory "nut.conf"
$upsmonConf = Join-Path $ConfigDirectory "upsmon.conf"
Test-Item (Test-Path -LiteralPath $nutConf) "nut.conf exists." "nut.conf is missing."
Test-Item (Test-Path -LiteralPath $upsmonConf) "upsmon.conf exists." "upsmon.conf is missing."
if (Test-Path -LiteralPath $nutConf) {
    $netClient = Select-String -LiteralPath $nutConf -Pattern "^\s*MODE\s*=\s*netclient\s*$" -Quiet
    Test-Item $netClient "NUT is configured as a network client." "MODE=netclient is missing."
}

$tcpReady = Test-NetConnection -ComputerName $manifest.serverAddress -Port 3493 -InformationLevel Quiet -WarningAction SilentlyContinue
Test-Item $tcpReady "NUPSON TCP port 3493 is reachable." "NUPSON TCP port 3493 is unreachable."

$upsc = Get-ChildItem -LiteralPath $InstallRoot -Filter "upsc.exe" -File -Recurse |
    Select-Object -First 1
Test-Item ($null -ne $upsc) "upsc.exe is installed." "upsc.exe is missing."
if ($upsc -and $tcpReady) {
    $target = "$($manifest.upsName)@$($manifest.serverAddress):3493"
    $statusResult = Invoke-CapturedProcess $upsc.FullName @($target, "ups.status")
    $statusOk = $statusResult.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($statusResult.StandardOutput)
    if ($statusOk) {
        Test-Item $true "UPS status query succeeded: $($statusResult.StandardOutput)" ""
        if ($statusResult.StandardError -and $statusResult.StandardError -ne "Init SSL without certificate database") {
            Write-Host "[WARN] upsc.exe: $($statusResult.StandardError)" -ForegroundColor Yellow
        }
    }
    else {
        $details = @($statusResult.StandardOutput, $statusResult.StandardError) |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        Test-Item $false "" "UPS status query failed (exit $($statusResult.ExitCode)): $($details -join "; ")"
    }

    $clientResult = Invoke-CapturedProcess $upsc.FullName @("-c", $target)
    $clientAddresses = @($clientResult.StandardOutput -split "\r?\n" |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
        ForEach-Object { ConvertTo-NormalizedAddress $_ })
    $expectedAddress = ConvertTo-NormalizedAddress $manifest.clientAddress
    $sessionVisible = $clientResult.ExitCode -eq 0 -and $clientAddresses -contains $expectedAddress
    $reportedClients = if ($clientAddresses.Count -gt 0) {
        $clientAddresses -join ", "
    }
    else {
        "none"
    }
    Test-Item $sessionVisible `
        "NUT server sees the upsmon session from $expectedAddress." `
        "NUT server does not see the upsmon session from $expectedAddress (reported clients: $reportedClients); inspect the Windows Application event log for authentication or SSL errors."
}

if ($DryRunEvents -and $manifest.policy -in @("timer", "immediate")) {
    $handler = Join-Path $ClientDirectory "nupson-event.ps1"
    Test-Item (Test-Path -LiteralPath $handler) "NUPSON event handler exists." "Event handler is missing."
    if (Test-Path -LiteralPath $handler) {
        & $handler -EventType ONBATT -DryRun
        $dryRunMarker = Join-Path $ClientDirectory "shutdown.pending"
        Set-Content -LiteralPath $dryRunMarker -Value '{"dryRun":true}' -Encoding UTF8
        try {
            & $handler -EventType ONLINE -DryRun
        }
        finally {
            Remove-Item -LiteralPath $dryRunMarker -Force -ErrorAction SilentlyContinue
        }
        Test-Item ($true) "Event handler dry run completed." "Event handler dry run failed."
    }
}

if ($Failures.Count -gt 0) {
    Write-Host "$($Failures.Count) NUPSON client checks failed." -ForegroundColor Red
    exit 1
}
Write-Host "All NUPSON Windows client checks passed." -ForegroundColor Green
