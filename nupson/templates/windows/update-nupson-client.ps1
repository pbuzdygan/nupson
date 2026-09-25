[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$installer = Join-Path $PSScriptRoot "install-nupson-client.ps1"
if (-not (Test-Path -LiteralPath $installer)) {
    throw "install-nupson-client.ps1 is missing from this generated bundle."
}
& $installer -Force
exit $LASTEXITCODE
