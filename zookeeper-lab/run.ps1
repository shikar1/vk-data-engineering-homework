param([switch]$SkipDownload, [switch]$SkipEnsemble)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
New-Item -ItemType Directory -Force -Path logs,work | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
function D {
    # Java writes normal diagnostics to stderr; only the exit code marks failure.
    $ErrorActionPreference = 'Continue'
    & docker @args 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { throw "Docker failed: $args" }
}
function Now { return [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0 }
function Wait-Event([string]$Path, [string]$Event, [int]$Timeout = 180) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.Elapsed.TotalSeconds -lt $Timeout) {
        if (Test-Path -LiteralPath $Path) {
            $lines = Get-Content -LiteralPath $Path -Encoding UTF8
            foreach ($line in $lines) {
                try { $item = $line | ConvertFrom-Json } catch { continue }
                if ($item.event -eq $Event) { return $item }
            }
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Timeout waiting for $Event in $Path. Inspect coordinator container logs."
}
function Quorum([string]$Label, [string]$Expect) {
    D compose run --rm -T client probe --expect $Expect |
        Tee-Object -FilePath "logs/$stamp-quorum-$Label.txt"
}
Start-Transcript -Path "logs/$stamp-terminal.txt"
try {
    D version
    D compose config --quiet
    D compose up -d zk1 zk2 zk3
    D compose build client
    if (-not $SkipEnsemble) {
    Quorum 'three' 'up'
    foreach ($server in @('zk1','zk2','zk3')) {
        D compose exec -T $server sh -c 'cat /conf/zoo.cfg; cat /data/myid; zkServer.sh status' |
            Tee-Object -FilePath "logs/$stamp-$server-config-status.txt"
    }
    try {
        D compose stop zk3
        Quorum 'two' 'up'
        D compose stop zk2
        Quorum 'one' 'down'
    } finally {
        D compose start zk2 zk3
    }
    Quorum 'restored' 'up'

    # The same real zkCli session creates, reads and gracefully closes the node.
    # A second session then verifies that the ephemeral node is gone.
    $node = "/ephemeral-$stamp"
    & {
        $ErrorActionPreference = 'Continue'
        & { Start-Sleep -Seconds 3; "create -e $node demo"; Start-Sleep -Seconds 1; "get -s $node"; 'quit' } |
            & docker compose exec -T zk1 zkCli.sh -server zk1:2181 2>&1
    } |
        Tee-Object -FilePath "logs/$stamp-ephemeral-before.txt"
    if ($LASTEXITCODE -ne 0) { throw 'zkCli creation failed' }
    & {
        # Missing node is the expected outcome, including zkCli exit code 1.
        $ErrorActionPreference = 'Continue'
        & { Start-Sleep -Seconds 3; "get -s $node"; 'quit' } |
            & docker compose exec -T zk2 zkCli.sh -server zk2:2181 2>&1
    } |
        Tee-Object -FilePath "logs/$stamp-ephemeral-after.txt"
    if (-not (Select-String -Path "logs/$stamp-ephemeral-before.txt" -Pattern "Created $node" -Quiet)) {
        throw 'zkCli did not confirm ephemeral creation'
    }
    if (-not (Select-String -Path "logs/$stamp-ephemeral-after.txt" -Pattern 'Node does not exist' -Quiet)) {
        throw 'zkCli did not confirm ephemeral disappearance'
    }
    }

    if (-not $SkipDownload) {
        D start rustfs-lab2
        D inspect rustfs-lab2 --format 'RustFS state={{.State.Status}} exit={{.State.ExitCode}} ports={{json .NetworkSettings.Ports}}'
        # Extract only the two required credentials; never print docker inspect.
        $containerEnv = (& docker inspect rustfs-lab2 --format '{{json .Config.Env}}') | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw 'Cannot read RustFS configuration' }
        $priorAccess = $env:RUSTFS_ACCESS_KEY
        $priorSecret = $env:RUSTFS_SECRET_KEY
        try {
            foreach ($entry in $containerEnv) {
                if ($entry.StartsWith('RUSTFS_ACCESS_KEY=')) { $env:RUSTFS_ACCESS_KEY = $entry.Substring(18) }
                if ($entry.StartsWith('RUSTFS_SECRET_KEY=')) { $env:RUSTFS_SECRET_KEY = $entry.Substring(18) }
            }
            if (-not $env:RUSTFS_ACCESS_KEY -or -not $env:RUSTFS_SECRET_KEY) { throw 'RustFS credentials missing' }
            D compose run --rm -T client prepare
        } finally {
            $env:RUSTFS_ACCESS_KEY = $priorAccess
            $env:RUSTFS_SECRET_KEY = $priorSecret
            $containerEnv = $null
        }
    }
    if (-not (Test-Path work/source.json)) { throw 'Source not prepared' }
    foreach ($fencing in @(0,1)) {
        $run = "$stamp-f$fencing"
        $a = "zk-lab-$run-a"
        $b = "zk-lab-$run-b"
        D compose run --rm -T client init --run $run
        D compose run -d --name $a client coordinate --run $run --who a --fencing $fencing
        Wait-Event "logs/$run-a.jsonl" 'CHECKPOINT' | Out-Null
        D compose run -d --name $b client coordinate --run $run --who b --fencing $fencing
        $pauseStarted = Now
        D pause $a
        $pauseCompleted = Now
        @{pause_started=$pauseStarted; pause_completed=$pauseCompleted} |
            ConvertTo-Json | Set-Content -Encoding UTF8 "logs/$run-pause.json"
        try {
            Wait-Event "logs/$run-b.jsonl" 'WROTE_RESULT' | Out-Null
            # At least 40 seconds, and always until the backup has finished.
            while (((Now) - $pauseCompleted) -lt 40) { Start-Sleep -Milliseconds 250 }
        } finally {
            D unpause $a
        }
        $expectedEvent = if ($fencing -eq 1) { 'BadVersionError' } else { 'WROTE_RESULT' }
        Wait-Event "logs/$run-a.jsonl" $expectedEvent | Out-Null
        foreach ($container in @($a,$b)) {
            $exit = D wait $container
            D logs $container 2>&1 | Out-File -Encoding UTF8 "logs/$container-console.txt"
            if ([int]$exit -ne 0) { throw "Coordinator $container exited $exit" }
        }
        D compose run --rm -T client inspect --run $run --fencing $fencing
    }
    Write-Host 'Both scenarios verified. See logs/*-summary.json.'
} finally {
    $ErrorActionPreference = 'Continue'
    foreach ($container in @($a,$b)) {
        if ($container) {
            & docker logs $container 2>&1 | Out-File -Encoding UTF8 "logs/$container-console.txt"
        }
    }
    & docker compose logs --no-color zk1 zk2 zk3 2>&1 | Out-File -Encoding UTF8 "logs/$stamp-zookeeper.txt"
    Stop-Transcript
}
