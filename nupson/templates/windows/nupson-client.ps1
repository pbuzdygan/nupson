[CmdletBinding()]
param(
    [ValidateSet("Menu", "Install", "Test", "TestEvents", "Update", "Uninstall")]
    [string]$Action = "Menu",
    [string]$BundleDirectory = "",
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-BundleDirectory {
    if (-not [string]::IsNullOrWhiteSpace($BundleDirectory)) {
        return (Resolve-Path -LiteralPath $BundleDirectory).Path
    }
    if (-not [string]::IsNullOrWhiteSpace($PSScriptRoot)) {
        return $PSScriptRoot
    }
    return (Get-Location).Path
}

function Get-ClientState {
    $service = Get-Service -Name "Network UPS Tools" -ErrorAction SilentlyContinue
    if (-not $service) {
        return "niezainstalowany"
    }
    return "zainstalowany, usluga: $($service.Status)"
}

function Invoke-BundleScript {
    param(
        [string]$Name,
        [string[]]$Arguments = @()
    )

    if (-not (Test-Administrator)) {
        throw "Uruchom PowerShell jako Administrator i sprobuj ponownie."
    }
    $scriptPath = Join-Path $script:ResolvedBundleDirectory $Name
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Brak pliku $Name w paczce: $script:ResolvedBundleDirectory"
    }
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $scriptPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Name zakonczyl dzialanie kodem $LASTEXITCODE."
    }
}

function Confirm-Uninstall {
    if ($Force) {
        return $true
    }
    $confirmation = Read-Host "Wpisz USUN, aby potwierdzic odinstalowanie klienta"
    return $confirmation -ceq "USUN"
}

function Invoke-ClientAction {
    param([string]$SelectedAction)

    switch ($SelectedAction) {
        "Install" { Invoke-BundleScript "install-nupson-client.ps1" }
        "Test" { Invoke-BundleScript "test-nupson-client.ps1" }
        "TestEvents" {
            Invoke-BundleScript "test-nupson-client.ps1" @("-DryRunEvents")
        }
        "Update" { Invoke-BundleScript "update-nupson-client.ps1" }
        "Uninstall" {
            if (Confirm-Uninstall) {
                Invoke-BundleScript "uninstall-nupson-client.ps1"
            }
            else {
                Write-Host "Odinstalowanie anulowane." -ForegroundColor Yellow
            }
        }
        default { throw "Nieobslugiwana akcja: $SelectedAction" }
    }
}

function Show-ClientMenu {
    Clear-Host
    Write-Host "NUPSON - klient Windows" -ForegroundColor Cyan
    Write-Host "Profil: $($script:manifest.profileName)"
    Write-Host "Serwer: $($script:manifest.serverAddress) / UPS: $($script:manifest.upsName)"
    Write-Host "Polityka: $($script:manifest.policy)"
    Write-Host "Paczka: $script:ResolvedBundleDirectory"
    Write-Host "Stan: $(Get-ClientState)"
    if (-not (Test-Administrator)) {
        Write-Host "PowerShell nie jest uruchomiony jako Administrator." -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "1. Instalacja"
    Write-Host "2. Test polaczenia i uslugi"
    Write-Host "3. Test rozszerzony (bezpieczny test zdarzen)"
    Write-Host "4. Aktualizacja z tej paczki"
    Write-Host "5. Odinstalowanie"
    Write-Host "Q. Wyjscie"
}

$ResolvedBundleDirectory = Resolve-BundleDirectory
$manifestPath = Join-Path $ResolvedBundleDirectory "nupson-client.json"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw "Uruchom menu w katalogu rozpakowanej paczki klienta NUPSON. Brak nupson-client.json w: $ResolvedBundleDirectory"
}
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.platform -ne "windows") {
    throw "Ta paczka nie jest profilem klienta Windows."
}

if ($Action -ne "Menu") {
    Invoke-ClientAction $Action
    return
}

while ($true) {
    Show-ClientMenu
    $choice = Read-Host "Wybierz operacje"
    $selectedAction = switch ($choice.Trim().ToUpperInvariant()) {
        "1" { "Install" }
        "2" { "Test" }
        "3" { "TestEvents" }
        "4" { "Update" }
        "5" { "Uninstall" }
        "Q" { return }
        default { $null }
    }
    if (-not $selectedAction) {
        Write-Host "Nieprawidlowy wybor." -ForegroundColor Yellow
        Start-Sleep -Seconds 1
        continue
    }
    try {
        Invoke-ClientAction $selectedAction
        Write-Host "Operacja zostala zakonczona." -ForegroundColor Green
    }
    catch {
        Write-Host "Blad: $($_.Exception.Message)" -ForegroundColor Red
    }
    [void](Read-Host "Nacisnij Enter, aby wrocic do menu")
}
