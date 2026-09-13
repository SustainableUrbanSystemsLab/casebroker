# One command to turn a fresh Windows machine into a campaign worker.
#
#   iwr -useb <raw url>/bootstrap_worker.ps1 | iex        # not recommended: no args
#   .\bootstrap_worker.ps1 -Token <write token> -WorkerId lab-ws-02
#
# What it does, in order, stopping at the first thing it cannot fix:
#   1. checks/install-hints the prerequisites
#   2. clones (or updates) this repo and the real_cities geometry builder
#   3. copies e3d.exe from the master share, or builds it if the SDK is present
#   4. runs setup_windows.ps1, which writes machine.env and proves the token
#   5. optionally runs one smoke case end to end
#   6. optionally pairs Syncthing with the master so finished cases come home
#
# Safe to re-run: every step is idempotent, and an existing machine.env is
# rewritten from the arguments rather than merged, so the profile is always
# exactly what was asked for.
param(
    [Parameter(Mandatory = $true)] [string]$Token,
    [Parameter(Mandatory = $true)] [string]$WorkerId,
    [string]$BrokerUrl = "https://casebroker.onrender.com",
    [string]$Root = "E:\wind",
    [string]$SrcDir = "C:\src",
    [string]$MasterDeviceId = "DW2QZ5L-CFGJL7D-X4VRTG5-SZ6535Q-Y2JGKT6-RFCZ6AN-RBRFEXB-IQ54MQ7",
    [string]$E3dSource = "",
    [string]$RealCitiesBranch = "v2-dataset-extension",
    [int]$Np = 0,
    [switch]$Smoke,
    [switch]$SkipSyncthing
)
# "Continue", not "Stop": in Windows PowerShell 5.1 any stderr line from a
# native command (git, uv, docker) is a terminating NativeCommandError under
# Stop, which would abort the bootstrap on a progress message. Failures are
# judged by $LASTEXITCODE and by explicit checks.
$ErrorActionPreference = "Continue"

function Step($m) { Write-Host "`n== $m" -ForegroundColor Cyan }
function Pass($m) { Write-Host "  PASS  $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  warn  $m" -ForegroundColor Yellow }
function Die($m) { Write-Host "  FAIL  $m" -ForegroundColor Red; exit 1 }

# git reports progress, "Already on '<branch>'" and "Already up to date." on
# STDERR. Windows PowerShell turns every one of those into a red
# NativeCommandError block, so a successful bootstrap looks like a failed one.
# Captured here and shown ONLY when git actually failed.
function Git() {
    $out = & git @args 2>&1
    if ($LASTEXITCODE -ne 0) {
        $out | ForEach-Object { Write-Host "  git: $_" -ForegroundColor Red }
        Die "git $($args -join ' ') failed"
    }
}

Step "prerequisites"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Die "git missing. Install Git for Windows (gitforwindows.org) -- the runner is bash and needs its cygpath/tar too."
}
Pass "git"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Warn "uv missing -- installing"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Die "uv still not on PATH; open a new shell and re-run" }
Pass "uv $(uv --version)"

$dockerOk = $false
try { docker info --format '{{.ServerVersion}}' 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { $dockerOk = $true } } catch {}
$blueOk = Test-Path "C:\blueCFD-Core-2024\OpenFOAM-12\platforms\mingw_w64Gcc122DPInt32Opt\bin\foamRun.exe"
if ($dockerOk) { Pass "Docker Desktop running" }
if ($blueOk) { Pass "blueCFD-Core 2024" }
if (-not $dockerOk -and -not $blueOk) {
    Die "no OpenFOAM runtime. Start Docker Desktop (WSL2 backend), or install blueCFD-Core 2024 + MS-MPI."
}

Step "source"
New-Item -ItemType Directory -Force $SrcDir | Out-Null
$repo = Join-Path $SrcDir "casebroker"
if (Test-Path (Join-Path $repo ".git")) {
    Git -C $repo pull --ff-only
    Pass "casebroker updated"
} else {
    Git clone https://github.com/SustainableUrbanSystemsLab/casebroker.git $repo
    Pass "casebroker cloned"
}
# real_cities lives in the sibling analysis repo and is what builds each site's
# geometry; without it the first case dies at "building geometry".
#
# ON THE BRANCH, NOT main. The geometry builder -- site_geometry.py, gba.py,
# canopy_zones.py, upstream_z0.py -- exists only on v2-dataset-extension. A
# default clone yields a real_cities with 12 files and no builder, and the
# failure then surfaces minutes into the first case as a ModuleNotFound rather
# than here at setup. Verified by cloning both.
$rcRepo = Join-Path $SrcDir "JP-Wind-ML-Comparison"
$rc = Join-Path $rcRepo "benchmark\real_cities"
if (Test-Path (Join-Path $rcRepo ".git")) {
    Git -C $rcRepo fetch origin $RealCitiesBranch
    Git -C $rcRepo checkout $RealCitiesBranch
    Git -C $rcRepo pull --ff-only
} else {
    Git clone -b $RealCitiesBranch https://github.com/SustainableUrbanSystemsLab/JP-Wind-ML-Comparison.git $rcRepo
}
if (-not (Test-Path (Join-Path $rc "site_geometry.py"))) {
    Die "real_cities at $rc has no site_geometry.py -- wrong branch? expected $RealCitiesBranch"
}
Push-Location $rc; uv sync -q; Pop-Location
Pass "real_cities at $rc"

Step "e3d"
$e3d = Join-Path $Root "bin\e3d.exe"
New-Item -ItemType Directory -Force (Join-Path $Root "bin") | Out-Null
if (-not (Test-Path $e3d)) {
    if ($E3dSource -and (Test-Path $E3dSource)) {
        Copy-Item $E3dSource $e3d -Force
        Pass "e3d copied from $E3dSource"
    } else {
        Die @"
e3d.exe not found at $e3d and no -E3dSource given.
Copy it from the master (E:\wind\bin\e3d.exe) onto this machine, or pass
-E3dSource \\master\share\e3d.exe. It is a self-contained single file.
"@
    }
} else { Pass "e3d.exe present" }

Step "configure + verify"
$setup = Join-Path $repo "setup_windows.ps1"
# A HASHTABLE splat, not an array. Splatting an @("-Token", $t, "-WorkerId", $w,
# ...) array binds POSITIONALLY, so the literal string "-WorkerId" landed on
# $BrokerUrl and "-BrokerUrl" on [int]$Np, which failed with "Cannot convert
# value -BrokerUrl to type System.Int32" -- an error that names a parameter
# nowhere near the actual mistake. A hashtable binds by name.
# ($args was also renamed: it is an automatic variable.)
$setupArgs = @{
    Token = $Token; WorkerId = $WorkerId; BrokerUrl = $BrokerUrl
    Root = $Root; RealCities = $rc; E3d = $e3d
}
if ($Np -gt 0) { $setupArgs["Np"] = $Np }
if ($Smoke) { $setupArgs["Smoke"] = $true }
& $setup @setupArgs
if ($LASTEXITCODE -ne 0) { Die "setup_windows.ps1 reported a problem (see above)" }

if (-not $SkipSyncthing) {
    Step "syncthing"
    $st = Get-Command syncthing -ErrorAction SilentlyContinue
    if (-not $st -and -not (Test-Path "C:\tools\syncthing\syncthing.exe")) {
        Warn @"
Syncthing is not installed, so finished archives will stay on this machine.
Install it (a single binary from github.com/syncthing/syncthing/releases),
then share $Root\done as folder id 'wind-done', Send Only, with master device
  $MasterDeviceId
and set WIND_SYNCTHING_URL/APIKEY/FOLDER in machine.env. See docs/fleet.md.
"@
    } else {
        Pass "syncthing present -- pair it with the master as described in docs/fleet.md"
    }
}

Write-Host "`nready. start the worker with:" -ForegroundColor Green
Write-Host "  cd $repo; .\start_worker.ps1"
