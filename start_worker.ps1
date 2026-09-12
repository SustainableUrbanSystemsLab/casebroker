# Start one worker on a Windows machine, in the foreground. Ctrl-C stops it
# cleanly (checkpoint saved, lease released -- the case resumes here next time).
#
#   .\start_worker.ps1 [-EnvFile machine.env] [-- extra worker args]
#
# The worker is native Python (uv); the runner is run_case.cmd, which finds a
# bash (Git for Windows) and runs run_case.sh under it. Whether a case then
# solves in Docker Desktop or natively in blueCFD-Core is WIND_RUNTIME's choice.
param(
    [string]$EnvFile = (Join-Path $PSScriptRoot "machine.env"),
    [Parameter(ValueFromRemainingArguments = $true)] [string[]]$WorkerArgs
)
$ErrorActionPreference = "Stop"

if (Test-Path $EnvFile) {
    foreach ($raw in Get-Content $EnvFile) {
        $line = ($raw -split "#", 2)[0].Trim()
        if ($line -eq "" -or $line -notmatch "=") { continue }
        $k, $v = $line -split "=", 2
        [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
    }
} else {
    Write-Warning "no $EnvFile -- copy machine.env.example and fill it in, or set the variables"
}
foreach ($required in "CASEBROKER_URL", "CASEBROKER_TOKEN", "WIND_ROOT", "EDDY3D_CLI") {
    if (-not [Environment]::GetEnvironmentVariable($required)) { throw "set $required" }
}
if (-not $env:WIND_CASES) { $env:WIND_CASES = "$env:WIND_ROOT/cases" }
if (-not $env:WIND_DONE)  { $env:WIND_DONE  = "$env:WIND_ROOT/done" }
foreach ($d in $env:WIND_ROOT, $env:WIND_CASES, $env:WIND_DONE) {
    # MSYS-style /c/... is what run_case.sh wants; New-Item wants C:\...
    $win = $d -replace '^/([A-Za-z])/', '$1:/'
    New-Item -ItemType Directory -Force -Path $win | Out-Null
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv not found: irm https://astral.sh/uv/install.ps1 | iex"
}

Set-Location $PSScriptRoot
$wid = if ($env:CASEBROKER_WORKER_ID) { $env:CASEBROKER_WORKER_ID } else { "<auto>" }
$rt  = if ($env:WIND_RUNTIME) { $env:WIND_RUNTIME } else { "auto" }   # no `??`: Windows PowerShell 5.1
$np  = if ($env:WIND_NP) { $env:WIND_NP } else { "24" }
Write-Host "worker $wid on ${env:COMPUTERNAME}: runtime=$rt ranks=$np root=$env:WIND_ROOT"
$argv = @("run", "python", "-m", "casebroker.worker",
          "--broker", $env:CASEBROKER_URL, "--token", $env:CASEBROKER_TOKEN,
          "--runner", (Join-Path $PSScriptRoot "runner\run_case.cmd"),
          "--cases-dir", $env:WIND_CASES,
          "--lease-seconds", "1800", "--heartbeat-seconds", "300", "--idle-backoff", "120")
if ($env:CASEBROKER_WORKER_ID) { $argv += @("--worker-id", $env:CASEBROKER_WORKER_ID) }
if ($WorkerArgs) { $argv += $WorkerArgs }
& uv @argv
exit $LASTEXITCODE
