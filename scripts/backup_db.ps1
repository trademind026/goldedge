<#
.SYNOPSIS
    Backs up the GoldEdge PostgreSQL/TimescaleDB database to a compressed custom-format (.dump) file.

.EXAMPLE
    .\scripts\backup_db.ps1
    .\scripts\backup_db.ps1 -Env dev -OutDir D:\backups
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'prod', 'dev')]
    [string]$Env = 'auto',
    [string]$OutDir,
    [string]$Container,
    [string]$User,
    [string]$Database
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'db_common.ps1')

$cfg = Resolve-DbConfig -Env $Env -Container $Container -User $User -Database $Database

if (-not $OutDir) {
    $OutDir = Join-Path $cfg.RepoRoot 'backups'
}
if (-not (Test-Path -LiteralPath $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$fileName = "goldedge_$($cfg.Database)_$timestamp.dump"
$hostPath = Join-Path $OutDir $fileName
$containerTmp = "/tmp/$fileName"

Write-Host "Backing up database '$($cfg.Database)' from container '$($cfg.Container)' (user: $($cfg.User))..." -ForegroundColor Cyan

try {
    docker exec $cfg.Container pg_dump -U $cfg.User -d $cfg.Database -Fc -f $containerTmp
    if ($LASTEXITCODE -ne 0) {
        throw "pg_dump failed with exit code $LASTEXITCODE."
    }

    docker cp "$($cfg.Container):$containerTmp" $hostPath
    if ($LASTEXITCODE -ne 0) {
        throw "docker cp failed with exit code $LASTEXITCODE."
    }
}
finally {
    docker exec $cfg.Container rm -f $containerTmp 2>$null | Out-Null
}

$sizeMB = [Math]::Round((Get-Item -LiteralPath $hostPath).Length / 1MB, 2)
Write-Host "Backup complete: $hostPath ($sizeMB MB)" -ForegroundColor Green
