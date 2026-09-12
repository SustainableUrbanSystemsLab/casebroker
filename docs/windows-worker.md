# A Windows workstation as a campaign worker

The 20-minute version of [`fleet.md`](fleet.md) for a Windows box: what to
install, one script that checks it and writes the profile, one command that
starts pulling cases. Rehearsed end to end on COD-PKAST-7865 (Docker Desktop,
48 cores) on 2026-09-11.

## 1. Install (once per machine)

| what | why | how |
| --- | --- | --- |
| Git for Windows | the runner is bash (`cygpath`, `tar`); `run_case.cmd` finds it | gitforwindows.org, defaults |
| uv | the worker and the geometry builder are Python projects | `irm https://astral.sh/uv/install.ps1 \| iex` |
| Python 3.10+ on PATH | small helpers inside the runner | python.org or `uv python install 3.12` |
| **one solver runtime** | | |
| &nbsp;&nbsp;Docker Desktop (WSL 2 backend) | `WIND_RUNTIME=docker`; pulls `dicehub/openfoam:12` on first case | give it most of the cores and RAM in Settings > Resources |
| &nbsp;&nbsp;or blueCFD-Core 2024 + MS-MPI | `WIND_RUNTIME=native`, no container | `C:\blueCFD-Core-2024`, `C:\Program Files\Microsoft MPI` |
| this repo + `real_cities` | worker/runner, geometry builder | clone `JP-Wind-ML-Comparison`; both live under `benchmark/` |
| `e3d.exe` | builds the OpenFOAM case | copy `E:\wind\bin\e3d.exe` from the master, or `dotnet publish Eddy3DCli -c Release -r win-x64 --self-contained -p:PublishSingleFile=true` |

A local disk with room: geometry cache + checkpoints + finished archives
(~300 MB per case at the campaign mesh). Not a network share -- OpenFOAM
writes thousands of small files per rank per write.

## 2. Check and configure

From the `casebroker` folder, in PowerShell:

```powershell
.\setup_windows.ps1 -Token <write token> -WorkerId lab-ws-02 -Root E:\wind `
    -RealCities C:\src\JP-Wind-ML-Comparison\benchmark\real_cities -Smoke
```

It prints PASS/FAIL per prerequisite, creates `E:\wind\{bin,done,cases,...}`,
writes `machine.env` (gitignored), asks the broker whether the token really
has write scope (`casebroker token check --expect write`), and with `-Smoke`
runs one crude 30-iteration case through the whole runner -- geometry from
the internet, `e3d build-case`, mesh, solve, archive. Roughly 10 minutes on
Docker; the archive lands in `E:\wind\done\smoke-<hostname>.tar.gz`.

Rules that are easy to get wrong:

- `-WorkerId` is **per machine, forever**. It is how a restarted worker gets
  its own half-finished case back; two machines sharing one id will fight
  over cases.
- `-Np` (ranks per case) defaults to `min(24, cores/2)`. Measure it later
  with the scaling sweep; do not oversubscribe a box that also does other
  work.
- The token is a **write** token (`CASEBROKER_WRITE_TOKENS` on the broker).
  A read token makes the worker fail on its first lease with 401.

## 3. Run

```powershell
.\start_worker.ps1            # foreground; Ctrl-C = checkpoint + release lease
.\start_worker.ps1 --max-cases 1   # one case, then exit (try-out)
```

The worker leases a case, runs `runner\run_case.cmd` (which runs
`run_case.sh` under Git bash), heartbeats the solver progress every 5 min,
and reports the archive path. Watch it on the dashboard (broker URL, read
token) under the worker id; the case detail shows the Progress line.

Ctrl-C while solving is safe: the study is checkpointed to `E:\wind\cases\`
and the same machine resumes it on the next lease.

## 4. Getting finished cases to the master

Finished cases are single `.tar.gz` files in `E:\wind\done`. Two ways, both
without an inbound port on either side:

- **Syncthing** (default for workstations). Install Syncthing on the worker,
  share `E:\wind\done` as *Send Only* with the master's device ID (master:
  *Receive Only*, `ignoreDelete`). Then make it quiet: folder *Watch for
  Changes* off, *Rescan Interval* 0, and put `WIND_SYNCTHING_APIKEY` +
  `WIND_SYNCTHING_FOLDER` in `machine.env` -- the runner triggers one scan
  per finished archive, so nothing else is ever hashed or sent.
- **Manual/scripted pull** if the master can SSH to the worker
  (`scripts/pull_done.sh`), or a copy of `E:\wind\done` by hand -- the
  archives are self-contained.

Delete a local archive only after the master has it (Syncthing shows the
folder as *Up to Date* on both ends).

## 5. When something is off

| symptom | cause | fix |
| --- | --- | --- |
| `set CASEBROKER_URL` on start | `machine.env` not found / not read | run `setup_windows.ps1` again from the repo folder |
| worker exits in seconds, "401" | read token or wrong broker | `uv run casebroker token check --broker <url> --token <t>` |
| `no bash found` | Git for Windows missing | install, or set `BASH` to a bash.exe |
| first case fails at `building geometry` | `real_cities` venv not synced or no internet | `uv sync` in `real_cities`; the geometry builder downloads Overture/GBA/GEDTM30/WorldCover/CHM tiles |
| `no OpenFOAM runtime found` | Docker Desktop not running | start it, or `WIND_RUNTIME=native` with blueCFD |
| solve fails, nothing in `done` | look in `E:\wind\failed_logs\<case>` (run.log, 12.log) | see `fleet.md`, "Where each thing can go wrong" |
