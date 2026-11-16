<#
.SYNOPSIS
    Restores a GoldEdge PostgreSQL/TimescaleDB .dump backup safely using TimescaleDB pre/post restore hooks.

.EXAMPLE
    .\scripts\restore_db.ps1 -BackupFile .\backups\goldedge_goldedge_20261116_154500.dump
    .\scripts\restore_db.ps1 -BackupFile .\backups\latest.dump -Env dev -Force
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BackupFile,
    [ValidateSet('auto', 'prod', 'dev')]
    [string]$Env = 'auto',
    [string]$Container,
    [string]$User,
    [string]$Database,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'db_common.ps1')

$resolvedBackup = Resolve-Path -LiteralPath $BackupFile -ErrorAction Stop
$cfg = Resolve-DbConfig -Env $Env -Container $Container -User $User -Database $Database

if (-not $Force) {
    Write-Warning "This will DROP and RECREATE database '$($cfg.Database)' in container '$($cfg.Container)'."
    $confirm = Read-Host "Type 'yes' to continue"
    if ($confirm -ne 'yes') {
        Write-Host 'Restore cancelled.' -ForegroundColor Yellow
        exit 0
    }
}

$containerTmp = '/tmp/goldedge_restore.dump'

function Invoke-Psql {
    param([string]$TargetDb, [string]$Sql)
    docker exec $cfg.Container psql -v ON_ERROR_STOP=1 -U $cfg.User -d $TargetDb -c $Sql
    if ($LASTEXITCODE -ne 0) {
        throw "psql command failed on '$TargetDb': $Sql"
    }
}

Write-Host "Copying '$($resolvedBackup.Path)' into container '$($cfg.Container)'..." -ForegroundColor Cyan
docker cp $resolvedBackup.Path "$($cfg.Container):$containerTmp"
if ($LASTEXITCODE -ne 0) {
    throw "docker cp failed with exit code $LASTEXITCODE."
}

try {
    Write-Host "Recreating database '$($cfg.Database)'..." -ForegroundColor Cyan
    Invoke-Psql -TargetDb 'postgres' -Sql "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$($cfg.Database)' AND pid <> pg_backend_pid();"
    Invoke-Psql -TargetDb 'postgres' -Sql "DROP DATABASE IF EXISTS `"$($cfg.Database)`";"
    Invoke-Psql -TargetDb 'postgres' -Sql "CREATE DATABASE `"$($cfg.Database)`";"

    Write-Host 'Preparing TimescaleDB for restore...' -ForegroundColor Cyan
    Invoke-Psql -TargetDb $cfg.Database -Sql 'CREATE EXTENSION IF NOT EXISTS timescaledb;'
    Invoke-Psql -TargetDb $cfg.Database -Sql 'SELECT timescaledb_pre_restore();'

    Write-Host 'Running pg_restore...' -ForegroundColor Cyan
    docker exec $cfg.Container pg_restore -U $cfg.User -d $cfg.Database --no-owner --no-acl $containerTmp
    $restoreExit = $LASTEXITCODE

    Write-Host 'Finalizing TimescaleDB post-restore...' -ForegroundColor Cyan
    Invoke-Psql -TargetDb $cfg.Database -Sql 'SELECT timescaledb_post_restore();'

    if ($restoreExit -ne 0) {
        Write-Warning "pg_restore exited with code $restoreExit (often non-fatal warnings such as pre-existing extension comments). Review output above."
    }
    else {
        Write-Host "Restore complete for database '$($cfg.Database)'." -ForegroundColor Green
    }
}
finally {
    docker exec $cfg.Container rm -f $containerTmp 2>$null | Out-Null
}
