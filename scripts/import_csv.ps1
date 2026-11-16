<#
.SYNOPSIS
    Restores CSV files (from a .zip archive or folder) exported by export_csv.ps1 into the GoldEdge PostgreSQL/TimescaleDB database.

.DESCRIPTION
    Assumes the target tables already exist (e.g., created by migrations).
    Imports CSVs using COPY, optionally truncating tables first, and resets SERIAL/IDENTITY sequences.

.EXAMPLE
    .\scripts\import_csv.ps1 -CsvDir .\backups\csv_goldedge_20261116_155000.zip -Truncate
    .\scripts\import_csv.ps1 -CsvDir .\backups\csv_goldedge_20261116_155000 -Truncate -Force
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [Alias('ZipFile', 'Path')]
    [string]$CsvDir,
    [ValidateSet('auto', 'prod', 'dev')]
    [string]$Env = 'auto',
    [string]$Container,
    [string]$User,
    [string]$Database,
    [switch]$Truncate,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'db_common.ps1')

$resolvedInput = Resolve-Path -LiteralPath $CsvDir -ErrorAction Stop
$tempExtractDir = $null
$workingDir = $resolvedInput.Path

if ((Test-Path -LiteralPath $workingDir -PathType Leaf) -and ($workingDir -like '*.zip')) {
    $tempExtractDir = Join-Path ([System.IO.Path]::GetTempPath()) ("goldedge_csv_restore_" + [System.Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tempExtractDir -Force | Out-Null
    Write-Host "Extracting '$workingDir'..." -ForegroundColor Cyan
    Expand-Archive -LiteralPath $workingDir -DestinationPath $tempExtractDir -Force
    $workingDir = $tempExtractDir
}

try {
$csvFiles = Get-ChildItem -LiteralPath $workingDir -Filter '*.csv' | Sort-Object Name
if ($csvFiles.Count -eq 0) {
    throw "No .csv files found in '$workingDir'."
}

$cfg = Resolve-DbConfig -Env $Env -Container $Container -User $User -Database $Database

if ($Truncate -and -not $Force) {
    Write-Warning "This will TRUNCATE existing data in $($csvFiles.Count) table(s) in '$($cfg.Database)' ($($cfg.Container)) before importing."
    $confirm = Read-Host "Type 'yes' to continue"
    if ($confirm -ne 'yes') {
        Write-Host 'Import cancelled.' -ForegroundColor Yellow
        exit 0
    }
}

if ($Truncate) {
    $tableList = ($csvFiles | ForEach-Object { "`"public`".`"$($_.BaseName)`"" }) -join ', '
    Write-Host 'Truncating target tables...' -ForegroundColor Cyan
    docker exec $cfg.Container psql -v ON_ERROR_STOP=1 -U $cfg.User -d $cfg.Database -c "TRUNCATE TABLE $tableList RESTART IDENTITY CASCADE;"
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to truncate tables.'
    }
}

# Retry loop handles foreign-key ordering automatically
$pending = @($csvFiles)
$pass = 0
$maxPasses = $pending.Count + 1

while ($pending.Count -gt 0 -and $pass -lt $maxPasses) {
    $pass++
    $nextPending = @()
    $importedThisPass = 0

    foreach ($file in $pending) {
        $table = $file.BaseName
        $containerCsv = "/tmp/import_$table.csv"

        try {
            docker cp $file.FullName "$($cfg.Container):$containerCsv"
            if ($LASTEXITCODE -ne 0) {
                throw "docker cp failed for '$($file.Name)'."
            }

            $sql = "\copy `"public`".`"$table`" FROM '$containerCsv' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')"
            $output = ($sql | docker exec -i $cfg.Container psql -v ON_ERROR_STOP=1 -U $cfg.User -d $cfg.Database 2>&1)
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  Imported $table ($output)" -ForegroundColor Green
                $importedThisPass++
            }
            else {
                $nextPending += $file
                $lastError = ($output | Out-String).Trim()
            }
        }
        finally {
            docker exec $cfg.Container rm -f $containerCsv 2>$null | Out-Null
        }
    }

    if ($nextPending.Count -gt 0 -and $importedThisPass -eq 0) {
        throw "Failed to import remaining table(s) ($($nextPending.Name -join ', ')). Last error:`n$lastError"
    }
    $pending = $nextPending
}

# Reset SERIAL / IDENTITY sequences to match max(id)
Write-Host 'Syncing sequences...' -ForegroundColor Cyan
$seqSql = @"
DO `$`$
DECLARE r RECORD;
BEGIN
    FOR r IN
        SELECT
            n.nspname AS schema_name,
            c.relname AS table_name,
            a.attname AS column_name,
            pg_get_serial_sequence(format('%I.%I', n.nspname, c.relname), a.attname) AS seq_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname = 'public'
          AND c.relkind IN ('r', 'p')
          AND a.attnum > 0
          AND NOT a.attisdropped
    LOOP
        IF r.seq_name IS NOT NULL THEN
            EXECUTE format(
                'SELECT setval(%L, COALESCE((SELECT MAX(%I) FROM %I.%I), 1), (SELECT COUNT(*) > 0 FROM %I.%I))',
                r.seq_name, r.column_name, r.schema_name, r.table_name, r.schema_name, r.table_name
            );
        END IF;
    END LOOP;
END `$`$;
"@

docker exec $cfg.Container psql -v ON_ERROR_STOP=1 -U $cfg.User -d $cfg.Database -c $seqSql | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Sequence sync had warnings; check sequences manually if needed.'
}

Write-Host 'CSV import complete.' -ForegroundColor Green
}
finally {
    if ($tempExtractDir -and (Test-Path -LiteralPath $tempExtractDir)) {
        Remove-Item -LiteralPath $tempExtractDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
