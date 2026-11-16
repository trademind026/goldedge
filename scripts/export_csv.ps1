<#
.SYNOPSIS
    Exports all public tables from the GoldEdge PostgreSQL/TimescaleDB database as UTF-8 CSV files and zips them into one archive.

.EXAMPLE
    .\scripts\export_csv.ps1
    .\scripts\export_csv.ps1 -Env dev -OutDir D:\csv_backups
    .\scripts\export_csv.ps1 -KeepFolder
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'prod', 'dev')]
    [string]$Env = 'auto',
    [string]$OutDir,
    [string]$Container,
    [string]$User,
    [string]$Database,
    [switch]$KeepFolder
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'db_common.ps1')

$cfg = Resolve-DbConfig -Env $Env -Container $Container -User $User -Database $Database

if (-not $OutDir) {
    $OutDir = Join-Path $cfg.RepoRoot 'backups'
}

$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$exportDir = Join-Path $OutDir "csv_$($cfg.Database)_$timestamp"
New-Item -ItemType Directory -Path $exportDir -Force | Out-Null

Write-Host "Fetching table list from '$($cfg.Database)' in '$($cfg.Container)'..." -ForegroundColor Cyan

$tablesRaw = docker exec $cfg.Container psql -U $cfg.User -d $cfg.Database -At -c "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to list tables from '$($cfg.Database)'."
}

$tables = $tablesRaw -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
if ($tables.Count -eq 0) {
    Write-Warning "No tables found in schema 'public'."
    exit 0
}

foreach ($table in $tables) {
    $containerCsv = "/tmp/export_$table.csv"
    $hostCsv = Join-Path $exportDir "$table.csv"

    Write-Host "  Exporting $table..." -NoNewline
    try {
        $sql = "\copy (SELECT * FROM `"public`".`"$table`") TO '$containerCsv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')"
        $sql | docker exec -i $cfg.Container psql -v ON_ERROR_STOP=1 -U $cfg.User -d $cfg.Database | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Export failed for table '$table'."
        }

        docker cp "$($cfg.Container):$containerCsv" $hostCsv
        if ($LASTEXITCODE -ne 0) {
            throw "docker cp failed for table '$table'."
        }

        $rowCount = ((Get-Content -LiteralPath $hostCsv | Measure-Object -Line).Lines) - 1
        if ($rowCount -lt 0) { $rowCount = 0 }
        Write-Host " $rowCount rows" -ForegroundColor Green
    }
    finally {
        docker exec $cfg.Container rm -f $containerCsv 2>$null | Out-Null
    }
}

$zipPath = "$exportDir.zip"
Write-Host "Zipping CSVs into '$zipPath'..." -ForegroundColor Cyan
Compress-Archive -Path (Join-Path $exportDir '*.csv') -DestinationPath $zipPath -Force

if (-not $KeepFolder) {
    Remove-Item -LiteralPath $exportDir -Recurse -Force
}

$sizeMB = [Math]::Round((Get-Item -LiteralPath $zipPath).Length / 1MB, 2)
Write-Host "CSV export complete: $zipPath ($sizeMB MB)" -ForegroundColor Green
