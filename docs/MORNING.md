# Starting the fleet — morning checklist

Everything below was verified on the night of 2026-09-12, against production,
not by reading the code.

## State you are waking up to

| | |
| --- | --- |
| broker | `https://casebroker.onrender.com`, token live with **write** scope |
| campaign | **36 pilot cases**, recipe `fixed-box-1008/of12-v3`, stratified 4 per class across LCZ 1–9, 31 city clusters |
| purged | the old 5,000 (recipe `fixed-box-1008/of12`) — none had ever produced a result; backup in `C:\rc2\campaign_backup\` |
| master | **this box**, `E:\wind\master`, Syncthing device `DW2QZ5L-CFGJL7D-X4VRTG5-SZ6535Q-Y2JGKT6-RFCZ6AN-RBRFEXB-IQ54MQ7` |
| runtimes | Docker **and** native blueCFD both verified end to end, and they agree |

## 1. This machine

Syncthing comes up by itself now (`SyncthingMaster` + `SyncthingWorker`
scheduled tasks, at logon). To start solving:

```powershell
cd C:\Users\pkastner3\Documents\GitHub\SustainLab\casebroker
.\start_worker.ps1
```

It is already running one pilot case from last night — check before starting a
second, or you will oversubscribe the box.

## 2. Every other machine

One command per machine, from a normal (non-admin) PowerShell:

```powershell
git clone https://github.com/SustainableUrbanSystemsLab/casebroker.git C:\src\casebroker
cd C:\src\casebroker
.\bootstrap_worker.ps1 -Token <the write token> -WorkerId <unique-per-machine> `
    -E3dSource \\COD-PKAST-7865\wind\bin\e3d.exe -Smoke
```

Then `.\start_worker.ps1`.

What it does: checks prerequisites, clones both repos (`real_cities` from the
**`v2-dataset-extension`** branch — the geometry builder is not on `main`),
copies `e3d.exe`, writes `machine.env`, proves the token against the broker,
and with `-Smoke` runs one crude case end to end first.

Rules that bite if ignored:

- **`-WorkerId` is per machine, forever.** It is how a restarted worker gets its
  own half-finished case back. Two machines sharing one id will fight.
- **e3d.exe has to come from somewhere.** It is a single self-contained file;
  share `E:\wind\bin` from this box or copy it on a stick.
- **No Docker? That's fine now** — blueCFD-Core 2024 + MS-MPI works, and
  `WIND_RUNTIME=auto` finds it. It was completely broken until last night.

## 3. Syncthing on each worker

Not automated — it needs a device pairing you have to approve:

1. Install Syncthing (single binary, github.com/syncthing/syncthing/releases).
2. Add this master's device id (above). Approve the worker on the master at
   <http://127.0.0.1:8384>.
3. Folder id **`wind-done`**, path `E:\wind\done`, **Send Only**,
   *Watch for Changes* **off**, *Rescan Interval* **0**, ignore `.tmp`.
4. In that machine's `machine.env`:
   `WIND_SYNCTHING_URL=http://127.0.0.1:8384`, plus `WIND_SYNCTHING_APIKEY`
   (Actions ▸ Settings ▸ General) and `WIND_SYNCTHING_FOLDER=wind-done`.

The folder stays silent until a case finishes; the runner then triggers one
scan for that one archive. Measured: a new file sat untouched for 45 s, and
reached the master 2 s after the scan call.

## 4. Watching it

- Dashboard: the broker URL in a browser, paste the token. Tick **notify on
  finished cases** to get a desktop notification per completion (tab must stay
  open).
- `uv run casebroker health --broker <url>` for a one-line liveness check.

## Known-open, in priority order

1. **Grid independence is not settled.** Four grids ran overnight on ICE; until
   that lands, the mesh defaults are provisional and so is every label. See
   `C:\rc2\gi\STUDY.md`.
2. **Ground z0 = 0.5 m is almost certainly wrong** — it is the Davenport value
   for *built-up terrain*, applied to the ground *underneath explicitly meshed
   buildings*, so building drag is counted twice. Worth ~30 % on pedestrian
   speed by the log law. Unfixed; it needs a decision on the value.
3. **36 cases will drain fast.** Say the word and the remaining ~4,964 sites go
   back up under the same recipe.
4. The pilot is one wind direction per case (270°) — as the old campaign was.
