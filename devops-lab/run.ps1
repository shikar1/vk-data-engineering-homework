param([switch]$Inspect, [switch]$Audit)

$ErrorActionPreference = 'Stop'
$labDirectory = $PSScriptRoot
$containerJson = docker inspect rustfs-lab2
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect rustfs-lab2' }
$container = ($containerJson | ConvertFrom-Json)[0]
if (-not $container.State.Running) { throw 'rustfs-lab2 is not running' }
$previousAccess = [Environment]::GetEnvironmentVariable('RUSTFS_ACCESS_KEY', 'Process')
$previousSecret = [Environment]::GetEnvironmentVariable('RUSTFS_SECRET_KEY', 'Process')
try {
    foreach ($name in @('RUSTFS_ACCESS_KEY', 'RUSTFS_SECRET_KEY')) {
        $entry = $container.Config.Env | Where-Object { $_.StartsWith($name + '=') } | Select-Object -First 1
        if (-not $entry) { throw "Missing container variable: $name" }
        [Environment]::SetEnvironmentVariable($name, $entry.Substring($name.Length + 1), 'Process')
    }
    $dockerArgs = @('run', '--rm', '--env', 'RUSTFS_ACCESS_KEY', '--env', 'RUSTFS_SECRET_KEY', '--env', 'PYTHONIOENCODING=utf-8', '--mount', "type=bind,source=$labDirectory,target=/app,readonly")
    if (-not $Inspect -and -not $Audit) {
        $outputDirectory = Join-Path $labDirectory ('results-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
        New-Item -ItemType Directory -Path $outputDirectory | Out-Null
        $dockerArgs += @('--mount', "type=bind,source=$outputDirectory,target=/output")
    }
    $dockerArgs += @('python:3.12-slim', 'python', '-u', '/app/mapreduce.py')
    if ($Inspect) { $dockerArgs += '--inspect' }
    if ($Audit) { $dockerArgs += '--audit' }
    & docker @dockerArgs
    if ($LASTEXITCODE -ne 0) { throw "MapReduce failed: exit $LASTEXITCODE" }
    if (-not $Inspect -and -not $Audit) { Write-Host "Local results: $outputDirectory" }
}
finally {
    [Environment]::SetEnvironmentVariable('RUSTFS_ACCESS_KEY', $previousAccess, 'Process')
    [Environment]::SetEnvironmentVariable('RUSTFS_SECRET_KEY', $previousSecret, 'Process')
    $container = $null
    $containerJson = $null
    $entry = $null
}
