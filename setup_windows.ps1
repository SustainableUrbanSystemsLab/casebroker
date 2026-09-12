# Prepare a Windows machine as a campaign worker: check prerequisites, write
# machine.env, prove the broker token works, optionally run one smoke case.
#
#   .\setup_windows.ps1 -Token <write token> -WorkerId lab-ws-02 [-Np 12]
#       [-BrokerUrl https://casebroker.onrender.com] [-Runtime auto|docker|native]
#       [-Root E:\wind] [-E3d E:\wind\bin\e3d.exe] [-RealCities <path>] [-Smoke]
#
# Idempotent: re-running rewrites machine.env from the parameters and re-checks.
# Every check prints PASS/FAIL with what to do; the script stops at the first FAIL
# that would make the worker unable to run. See docs/windows-worker.md.
param(
    [Parameter(Mandatory = $true)] [string]$Token,
    [Parameter(Mandatory = $true)] [string]$WorkerId,
    [string]$BrokerUrl = "https://casebroker.onrender.com",
    [string]$Cluster = "lab",
    [int]$Np = 0,
    [ValidateSet("auto", "docker", "native", "podman")] [string]$Runtime = "auto",
    [string]$Root = "E:\wind",
    [string]$E3d = "",
    [string]$RealCities = "",
    [int]$WriteInterval = 200,
    [switch]$Smoke
)
# "Continue", not "Stop": under Stop, any stderr line from a native command (uv, docker,
# e3d) is a terminating NativeCommandError in Windows PowerShell 5.1, and stderr is
# never redirected here for the same reason. Failures are judged by $LASTEXITCODE.
$ErrorActionPreference = "Continue"
$here = $PSScriptRoot
$fail = $false
function Pass($m) { Write-Host ("  PASS  " + $m) -ForegroundColor Green }
function Fail($m) { Write-Host ("  FAIL  " + $m) -ForegroundColor Red; $script:fail = $true }
function Warn($m) { Write-Host ("  warn  " + $m) -ForegroundColor Yellow }
function ToMsys($p) {
    # C:\wind -> /c/wind (what run_case.sh wants). No scriptblock -replace: Windows PowerShell 5.1.
    $p = $p -replace '\\', '/'
    if ($p -match '^([A-Za-z]):(.*)$') { return "/" + $matches[1].ToLower() + $matches[2] }
    return $p
}

Write-Host "== prerequisites on $env:COMPUTERNAME =="
# uv (worker), python (runner helpers), Git for Windows bash + tar (runner), docker or blueCFD (solver)
if (Get-Command uv -ErrorAction SilentlyContinue) { Pass ("uv " + (uv --version)) } else { Fail "uv missing: irm https://astral.sh/uv/install.ps1 | iex" }
if (Get-Command python -ErrorAction SilentlyContinue) { Pass ("python " + (python --version)) } else { Fail "python missing (3.10+ on PATH)" }
$bash = @("$env:ProgramFiles\Git\bin\bash.exe", "${env:ProgramFiles(x86)}\Git\bin\bash.exe", "$env:LocalAppData\Programs\Git\bin\bash.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($bash) { Pass "Git for Windows bash: $bash" } else { Fail "Git for Windows missing (run_case.cmd needs its bash, cygpath and tar)" }
if (Get-Command tar.exe -ErrorAction SilentlyContinue) { Pass "tar.exe" } else { Fail "tar.exe missing (Windows 10 1803+ ships it)" }
$cores = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
$dockerOk = $false
try { $di = docker info --format '{{.ServerVersion}} cpus={{.NCPU}}' 2>$null; if ($LASTEXITCODE -eq 0 -and $di) { $dockerOk = $true; Pass "Docker Desktop running: $di" } } catch {}
$blueOk = Test-Path "C:\blueCFD-Core-2024\OpenFOAM-12\platforms\mingw_w64Gcc122DPInt32Opt\bin\foamRun.exe"
if ($blueOk) { Pass "blueCFD-Core 2024 (OpenFOAM-12 foamRun.exe)" }
if (-not $dockerOk -and -not $blueOk) { Fail "no solver runtime: start Docker Desktop (WSL2 backend) or install blueCFD-Core 2024" }
if ($Runtime -eq "docker" -and -not $dockerOk) { Fail "-Runtime docker but Docker is not running" }
if ($Runtime -eq "native" -and -not $blueOk) { Fail "-Runtime native but blueCFD-Core 2024 not found" }
if ($blueOk -and -not (Test-Path "C:\Program Files\Microsoft MPI\Bin\mpiexec.exe")) { Warn "MS-MPI mpiexec not found; native runtime needs it" }

# e3d
if (-not $E3d) { $E3d = Join-Path $Root "bin\e3d.exe" }
if (Test-Path $E3d) { $v = & $E3d --version | Select-Object -First 1; Pass "e3d.exe $v at $E3d" }
else { Fail "e3d.exe not at $E3d -- copy it from the master's E:\wind\bin (or dotnet publish Eddy3DCli -r win-x64)" }

# real_cities (geometry builder) with its venv
if (-not $RealCities) { $RealCities = (Resolve-Path (Join-Path $here "..\real_cities") -ErrorAction SilentlyContinue).Path }
if ($RealCities -and (Test-Path (Join-Path $RealCities "site_geometry.py"))) {
    Push-Location $RealCities
    try { uv sync -q | Out-Null; Pass "real_cities at $RealCities (uv sync ok)" } catch { Fail "real_cities uv sync failed in $RealCities" }
    Pop-Location
} else { Fail "real_cities checkout not found (pass -RealCities <path to JP-Wind-ML-Comparison\benchmark\real_cities>)" }

# this repo's venv
Push-Location $here
try { uv sync -q | Out-Null; Pass "casebroker uv sync ok" } catch { Fail "casebroker uv sync failed" }
Pop-Location

if ($fail) { Write-Host "`nfix the FAIL lines above, then re-run" -ForegroundColor Red; exit 1 }

Write-Host "`n== layout under $Root =="
foreach ($d in "bin", "done", "cases", "geometry", "failed_logs") { New-Item -ItemType Directory -Force (Join-Path $Root $d) | Out-Null }
Pass "$Root\{bin,done,cases,geometry,failed_logs}"
if ($Np -le 0) { $Np = [Math]::Min(24, [Math]::Max(1, [int]($cores / 2))) }
Write-Host "  ranks per case: $Np (of $cores logical cores; measure with the scaling sweep later)"

Write-Host "`n== machine.env =="
$envFile = Join-Path $here "machine.env"
@"
# $env:COMPUTERNAME -- written by setup_windows.ps1 on $(Get-Date -Format s)
CASEBROKER_URL=$BrokerUrl
CASEBROKER_TOKEN=$Token
CASEBROKER_WORKER_ID=$WorkerId
CASEBROKER_CLUSTER=$Cluster
WIND_NP=$Np
WIND_RUNTIME=$Runtime
WIND_IMAGE=docker.io/dicehub/openfoam:12
BLUECFD_HOME=/c/blueCFD-Core-2024
MSMPI_BIN=/c/Program Files/Microsoft MPI/Bin
WIND_ROOT=$(ToMsys $Root)
WIND_DONE=$(ToMsys (Join-Path $Root 'done'))
WIND_CASES=$(ToMsys (Join-Path $Root 'cases'))
WIND_WRITE_INTERVAL=$WriteInterval
EDDY3D_CLI=$(ToMsys $E3d)
REAL_CITIES=$(ToMsys $RealCities)
"@ | Set-Content -Encoding ascii $envFile
Pass "wrote $envFile (gitignored)"

Write-Host "`n== broker =="
Push-Location $here
$h = uv run casebroker health --broker $BrokerUrl
Write-Host ("  " + ($h -join "`n  "))
$c = uv run casebroker token check --broker $BrokerUrl --token $Token --expect write
Pop-Location
if ($LASTEXITCODE -eq 0) { Pass "token has write scope on $BrokerUrl" } else { Fail "token is not a write token on ${BrokerUrl}: $($c -join ' ')"; exit 1 }

if ($Smoke) {
    Write-Host "`n== smoke case (crude mesh, 30 iterations, unconverged accepted) =="
    $spec = Join-Path $here "runner\smoke_spec.json"
    $env:WIND_ALLOW_UNCONVERGED = "1"
    foreach ($line in Get-Content $envFile) { if ($line -match '^([A-Z_]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process") } }
    $env:CASE_ID = "smoke-$env:COMPUTERNAME".ToLower()
    $env:CASE_SPEC = Get-Content $spec -Raw
    $env:WIND_NP = [Math]::Min(4, $Np)
    & (Join-Path $here "runner\run_case.cmd") 2> (Join-Path $Root "smoke.err") | Tee-Object -FilePath (Join-Path $Root "smoke.out") | Select-Object -Last 1
    if ($LASTEXITCODE -eq 0 -and (Test-Path (Join-Path $Root "done\$($env:CASE_ID).tar.gz"))) { Pass "smoke archive in $Root\done" }
    else { Fail "smoke failed (see $Root\smoke.err and $Root\failed_logs)"; exit 1 }
}

Write-Host "`nready. start the worker with:  .\start_worker.ps1" -ForegroundColor Green
