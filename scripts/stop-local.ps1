$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
docker compose stop
if ($LASTEXITCODE -ne 0) { throw 'Unable to stop HarborRelay.' }
