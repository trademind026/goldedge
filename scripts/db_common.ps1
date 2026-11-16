<#
.SYNOPSIS
    Shared helper functions for GoldEdge PostgreSQL/TimescaleDB backup and restore scripts.
#>

function Get-RepoRoot {
    return (Split-Path -Parent $PSScriptRoot)
}

function Import-DotEnv {
    param([string]$EnvPath)
    $vars = @{}
    if (-not (Test-Path -LiteralPath $EnvPath)) {
        return $vars
    }
    foreach ($line in Get-Content -LiteralPath $EnvPath) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        $idx = $trimmed.IndexOf('=')
        if ($idx -lt 1) { continue }
        $key = $trimmed.Substring(0, $idx).Trim()
        $val = $trimmed.Substring($idx + 1).Trim()
        if (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'"))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        $vars[$key] = $val
    }
    return $vars
}

function Resolve-DbConfig {
    param(
        [ValidateSet('auto', 'prod', 'dev')]
        [string]$Env = 'auto',
        [string]$Container,
        [string]$User,
        [string]$Database
    )

    $repoRoot = Get-RepoRoot
    $envVars = Import-DotEnv -EnvPath (Join-Path $repoRoot '.env')

    if (-not $User) {
        $User = if ($envVars['DB_USER']) { $envVars['DB_USER'] } else { 'postgres' }
    }
    if (-not $Database) {
        $Database = if ($envVars['DB_NAME']) { $envVars['DB_NAME'] } else { 'goldedge' }
    }

    if (-not $Container) {
        if ($Env -eq 'prod') {
            $Container = 'goldedge-db'
        }
        elseif ($Env -eq 'dev') {
            $Container = 'goldedge-db-dev'
        }
        else {
            $running = docker ps --format '{{.Names}}' 2>$null
            if ($LASTEXITCODE -ne 0) {
                throw 'Docker is not running or not reachable. Start Docker Desktop and try again.'
            }
            if ($running -contains 'goldedge-db') {
                $Container = 'goldedge-db'
            }
            elseif ($running -contains 'goldedge-db-dev') {
                $Container = 'goldedge-db-dev'
            }
            else {
                throw "Neither 'goldedge-db' nor 'goldedge-db-dev' is currently running. Start the DB container or pass -Container."
            }
        }
    }

    $isRunning = docker ps --filter "name=^/$Container$" --format '{{.Names}}' 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker is not running or not reachable. Start Docker Desktop and try again.'
    }
    if (-not $isRunning) {
        throw "Container '$Container' is not running."
    }

    return [PSCustomObject]@{
        Container = $Container
        User      = $User
        Database  = $Database
        RepoRoot  = $repoRoot
    }
}
