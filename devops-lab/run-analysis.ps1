param([switch]$Inspect, [switch]$Regions)

$ErrorActionPreference = 'Stop'
$labDirectory = $PSScriptRoot
$image = 'hh-lab-duckdb:1.4.3'
$imageId = docker image ls --quiet $image
if ($LASTEXITCODE -ne 0) { throw 'Cannot list Docker images' }
if (-not $imageId) {
    docker build --file (Join-Path $labDirectory 'Dockerfile.duckdb') --tag $image $labDirectory
    if ($LASTEXITCODE -ne 0) { throw 'DuckDB image build failed' }
}
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
    $outputPrefix = if ($Regions) { 'region-results-' } else { 'salary-results-' }
    $outputDirectory = Join-Path $labDirectory ($outputPrefix + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
    New-Item -ItemType Directory -Path $outputDirectory | Out-Null
    $dockerArgs = @('run', '--rm', '--env', 'RUSTFS_ACCESS_KEY', '--env', 'RUSTFS_SECRET_KEY', '--env', 'PYTHONIOENCODING=utf-8', '--mount', "type=bind,source=$labDirectory,target=/app,readonly", '--mount', "type=bind,source=$outputDirectory,target=/output", $image, 'python', '-u', '/app/analysis_run.py')
    if ($Inspect) { $dockerArgs += '--inspect' }
    if ($Regions) { $dockerArgs += '--regions' }
    & docker @dockerArgs | Tee-Object -FilePath (Join-Path $outputDirectory 'run.log')
    if ($LASTEXITCODE -ne 0) { throw "Salary analysis failed: exit $LASTEXITCODE" }
    Write-Host "Local results: $outputDirectory"
}
finally {
    [Environment]::SetEnvironmentVariable('RUSTFS_ACCESS_KEY', $previousAccess, 'Process')
    [Environment]::SetEnvironmentVariable('RUSTFS_SECRET_KEY', $previousSecret, 'Process')
    $container = $null
    $containerJson = $null
    $entry = $null
}
