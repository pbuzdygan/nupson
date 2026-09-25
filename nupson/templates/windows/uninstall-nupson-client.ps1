[CmdletBinding()]
param([switch]$KeepConfiguration)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ServiceName = "Network UPS Tools"
$InstallRoot = Join-Path $env:SystemDrive "NUPSON\NUT"
$ClientDirectory = Join-Path $env:ProgramData "NUPSON\Client"

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

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this uninstaller from PowerShell as Administrator."
}

$marker = Join-Path $ClientDirectory "shutdown.pending"
if (Test-Path -LiteralPath $marker) {
    & (Join-Path $env:SystemRoot "System32\shutdown.exe") /a
    Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
}

$service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($service) {
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    $nutExecutable = Get-ChildItem -LiteralPath $InstallRoot -Filter "nut.exe" -File -Recurse |
        Select-Object -First 1
    if ($nutExecutable) {
        $unregisterExitCode = Unregister-NutService -NutExecutable $nutExecutable.FullName
        if ($unregisterExitCode -ne 0) {
            throw "Could not unregister the NUT service (exit code $unregisterExitCode)."
        }
        Write-Host "NUT service unregistered."
    }
}
if (Test-Path -LiteralPath $InstallRoot) {
    Remove-Item -LiteralPath $InstallRoot -Recurse -Force
}
if (-not $KeepConfiguration -and (Test-Path -LiteralPath $ClientDirectory)) {
    Remove-Item -LiteralPath $ClientDirectory -Recurse -Force
}
Write-Host "NUPSON Windows client removed."
