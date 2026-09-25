[CmdletBinding()]
param([switch]$Force)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$NutVersion = "2.8.5-1-fixNSS"
$NutArchiveUrl = "https://github.com/networkupstools/nut/releases/download/v2.8.5/NUT-for-Windows-x86_64-RELEASE-2.8.5-1-fixNSS.7z"
$NutArchiveSha256 = "a226b9e402e589f3d14118d8d07ef2cdb4e28bc3e30d55de47fef46abdf4dfb0"
$ServiceName = "Network UPS Tools"
$InstallRoot = Join-Path $env:SystemDrive "NUPSON\NUT"
$ClientDirectory = Join-Path $env:ProgramData "NUPSON\Client"
$ConfigDirectory = Join-Path $InstallRoot "etc"
$BackupRoot = "$InstallRoot.previous"
$RequiredBundleFiles = @("nut.conf", "upsmon.conf", "nupson-client.json")
$TemporaryDirectory = $null
$ExistingWasRemoved = $false

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this installer from PowerShell as Administrator."
    }
}

function Find-RuntimeRoot {
    param([string]$ExtractedPath)
    $candidates = @(Get-ChildItem -LiteralPath $ExtractedPath -Filter "nut.exe" -File -Recurse)
    foreach ($candidate in $candidates) {
        $cursor = $candidate.Directory
        while ($null -ne $cursor -and $cursor.FullName.StartsWith($ExtractedPath)) {
            if ((Test-Path (Join-Path $cursor.FullName "bin")) -and
                (Test-Path (Join-Path $cursor.FullName "sbin"))) {
                return $cursor.FullName
            }
            $cursor = $cursor.Parent
        }
    }
    if ($candidates.Count -eq 1) {
        return $candidates[0].Directory.Parent.FullName
    }
    throw "Could not identify the NUT runtime root in the official archive."
}

function Expand-SevenZipArchive {
    param([string]$Archive, [string]$Destination)
    $tar = Join-Path $env:SystemRoot "System32\tar.exe"
    if (Test-Path -LiteralPath $tar) {
        & $tar -xf $Archive -C $Destination
        if ($LASTEXITCODE -eq 0) {
            return
        }
        Get-ChildItem -LiteralPath $Destination -Force | Remove-Item -Recurse -Force
    }

    $programFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
    $sevenZipCandidates = @(
        (Join-Path $env:ProgramFiles "7-Zip\7z.exe"),
        (Join-Path $programFilesX86 "7-Zip\7z.exe")
    )
    $sevenZip = $sevenZipCandidates | Where-Object { $_ -and (Test-Path $_) } |
        Select-Object -First 1
    if (-not $sevenZip) {
        throw "Windows tar could not unpack the official .7z archive. Install 7-Zip and run the installer again."
    }
    & $sevenZip x $Archive "-o$Destination" -y | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "7-Zip failed to unpack the NUT runtime (exit code $LASTEXITCODE)."
    }
}

function Find-NutExecutable {
    param([string]$Root)
    $result = Get-ChildItem -LiteralPath $Root -Filter "nut.exe" -File -Recurse |
        Select-Object -First 1
    if (-not $result) {
        throw "nut.exe is missing from $Root."
    }
    return $result.FullName
}

function Unregister-NutService {
    param([string]$NutExecutable)
    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process -FilePath $NutExecutable `
            -ArgumentList @("-U") -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        return $process.ExitCode
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

Assert-Administrator
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "This NUPSON bundle supports only 64-bit Windows."
}
foreach ($file in $RequiredBundleFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $file))) {
        throw "The generated bundle is incomplete: $file is missing."
    }
}

$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot "nupson-client.json") -Raw |
    ConvertFrom-Json
if ($manifest.platform -ne "windows" -or $manifest.nutVersion -ne $NutVersion) {
    throw "The client manifest does not match this installer."
}

$existingService = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
if ($existingService -and $existingService.PathName -notlike "*$InstallRoot*") {
    throw "An independently installed '$ServiceName' service already exists. Remove it manually before installing the NUPSON-managed client."
}

$TemporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("nupson-client-" + [guid]::NewGuid())
$ArchivePath = Join-Path $TemporaryDirectory "nut-windows.7z"
$ExtractedPath = Join-Path $TemporaryDirectory "runtime"
New-Item -ItemType Directory -Path $ExtractedPath -Force | Out-Null

try {
    Write-Host "Downloading Network UPS Tools $NutVersion..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $NutArchiveUrl -OutFile $ArchivePath -UseBasicParsing
    $actualHash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $NutArchiveSha256) {
        throw "NUT archive checksum mismatch. Expected $NutArchiveSha256, received $actualHash."
    }

    Write-Host "Extracting the verified NUT runtime..."
    Expand-SevenZipArchive -Archive $ArchivePath -Destination $ExtractedPath
    $runtimeRoot = Find-RuntimeRoot -ExtractedPath $ExtractedPath

    if ($existingService) {
        Write-Host "Stopping the existing NUPSON-managed NUT service..."
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        $oldNutExecutable = Find-NutExecutable -Root $InstallRoot
        $unregisterExitCode = Unregister-NutService -NutExecutable $oldNutExecutable
        if ($unregisterExitCode -ne 0 -and -not $Force) {
            throw "Could not unregister the existing NUT service. Re-run with -Force after checking the service state."
        }
        Write-Host "Existing NUT service unregistered."
        $ExistingWasRemoved = $true
    }

    if (Test-Path -LiteralPath $BackupRoot) {
        Remove-Item -LiteralPath $BackupRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $InstallRoot) {
        Move-Item -LiteralPath $InstallRoot -Destination $BackupRoot
    }
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    Copy-Item -Path (Join-Path $runtimeRoot "*") -Destination $InstallRoot -Recurse -Force

    New-Item -ItemType Directory -Path $ConfigDirectory -Force | Out-Null
    New-Item -ItemType Directory -Path $ClientDirectory -Force | Out-Null
    foreach ($file in @("nut.conf", "upsmon.conf")) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination $ConfigDirectory -Force
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "nupson-client.json") -Destination $ClientDirectory -Force
    foreach ($file in @("nupson-event.ps1", "nupson-event.cmd")) {
        $source = Join-Path $PSScriptRoot $file
        $target = Join-Path $ClientDirectory $file
        if (Test-Path -LiteralPath $source) {
            Copy-Item -LiteralPath $source -Destination $target -Force
        }
        elseif (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Force
        }
    }

    foreach ($protectedPath in @($InstallRoot, $ClientDirectory)) {
        & icacls.exe $protectedPath /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Could not protect the NUPSON path: $protectedPath"
        }
    }

    $nutExecutable = Find-NutExecutable -Root $InstallRoot
    & $nutExecutable -I
    if ($LASTEXITCODE -ne 0) {
        throw "nut.exe could not register the Windows service."
    }

    $serviceRegistryPath = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
    New-ItemProperty -Path $serviceRegistryPath -Name "Environment" -PropertyType MultiString -Value @("NUT_CONFPATH=$ConfigDirectory") -Force | Out-Null
    & sc.exe config $ServiceName start= delayed-auto | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not configure delayed automatic service startup."
    }
    & sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not configure service recovery."
    }

    Start-Service -Name $ServiceName
    Start-Sleep -Seconds 3
    $service = Get-Service -Name $ServiceName
    if ($service.Status -ne "Running") {
        throw "The Network UPS Tools service did not reach the Running state."
    }
    $upsmonProcesses = @(Get-Process -Name "upsmon" -ErrorAction SilentlyContinue)
    if ($upsmonProcesses.Count -eq 0) {
        throw "The NUT wrapper service is running, but upsmon.exe did not start. Inspect the Windows Application event log for NUT/upsmon errors."
    }

    if (Test-Path -LiteralPath $BackupRoot) {
        Remove-Item -LiteralPath $BackupRoot -Recurse -Force
    }
    Write-Host "NUPSON Windows client installed successfully."
    Write-Host "Run .\test-nupson-client.ps1 to verify connectivity and configuration."
}
catch {
    Write-Warning $_.Exception.Message
    if ($ExistingWasRemoved -and (Test-Path -LiteralPath $BackupRoot)) {
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $InstallRoot) {
            Remove-Item -LiteralPath $InstallRoot -Recurse -Force
        }
        Move-Item -LiteralPath $BackupRoot -Destination $InstallRoot
        try {
            $rollbackNut = Find-NutExecutable -Root $InstallRoot
            & $rollbackNut -I | Out-Null
            Start-Service -Name $ServiceName -ErrorAction SilentlyContinue
        }
        catch {
            Write-Warning "Automatic rollback could not restore the previous service."
        }
    }
    throw
}
finally {
    if ($TemporaryDirectory -and (Test-Path -LiteralPath $TemporaryDirectory)) {
        Remove-Item -LiteralPath $TemporaryDirectory -Recurse -Force
    }
}
