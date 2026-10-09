# The dataset export

The broker answers questions of the wind field where it is stored
([protocol.md](protocol.md), "Asking the wind field"). Training needs the
opposite: every field of a selection on local disk, in a layout a data loader
can read without a broker, plus a record of which broker and which filters it
came from, so that two runs of an experiment can be shown to have read the same
set. `casebroker export` produces that. It is a client of the read API: a read
token is enough, and it never writes to the broker.

```bash
uv run --extra export casebroker export --broker URL --token - --out ds/v5 \
    --recipe cyl-1008/of12-v5 < read.token
```

The `export` extra brings in pyarrow and zarr. The server image does not have
them, and does not need them: this runs on a workstation. If the extra is
missing, the command says so and exits 2.

| Option | Meaning |
| --- | --- |
| `--broker` | default `$CASEBROKER_URL` |
| `--token` | default `$CASEBROKER_TOKEN`, else `$CASEBROKER_READ_TOKENS`; `-` reads stdin |
| `--out DIR` | the snapshot (required) |
| `--recipe`, `--split` | repeatable. Repeats are ORed: a case has one of each |
| `--label key:value` or `key` | repeatable. Repeats are ANDed: each one narrows the set |
| `--state` | default `done`; `all` for any state |
| `--height-m` | default `1.75`, matched exactly, as `GET /v1/fields?height_m=` matches it |
| `--limit N` | the first N cases by case id |
| `--workers N` | cases downloaded at once (default 4) |
| `--no-fields` | tables only: no field is downloaded, and `fields.zarr` is left as it is |

## What it writes

```
<out>/dataset.json     the card
<out>/cases.parquet    one row per case
<out>/fields.parquet   one row per (case, direction)
<out>/fields.zarr      one group per case_id, one array per direction
```

**`cases.parquet`** holds `case_id`, `recipe`, `split`, `lcz`, `city_cluster`,
`state`, `lat`, `lon` and `labels` (a map). From the case's `place` it takes
`country`, `country_code`, `town`, `town_km` and `region`. The site's numbers
are every metric in `/v1/dataset`'s registry (`casebroker/dataset.py`,
`METRICS`), read the same way, with the node's telemetry first and the
completion metrics second. So a column here and a histogram on the dashboard
give the same number for the same case. That covers the thirteen urban-form
indices (`bcr`, `bht_m`, … `lambda_f_min`, `lambda_f_max`), `n_buildings`,
`terrain_relief_m`, `canopy_fraction`, `total_cells`, `max_skewness`,
`max_non_orthogonality`, `case_seconds`, `mesh_seconds` and `solve_seconds`.
After those come `height_source`, `build`, `worker`, `n_fields` (the
directions it has at `--height-m`), `result_uri`, `result_sha256` and
`result_bytes`. A value the node never reported is null, never 0.

**`fields.parquet`** holds `case_id`, `direction`, `deg` (wind FROM), `height_m`,
`u_ref`, `lambda_f` (the frontal area index at that `deg`; null where the site
report has none), `coverage`, `n_valid`, `umag_mean` `min` `p05` `p25` `p50`
`p75` `p95` `p99` `max` and the node's `umag_p999`. Each statistic also appears
as `vr_*` = |U| / U_ref. Then come the grid (`nx`, `ny`, `x0`, `y0`,
`spacing_m`), the broker's `sha256` and `bytes` for the stored blob,
`reported_at`, and `zarr`, the field's path in the store (null when it is not
there). Some fields were stored before the broker summarised fields, and the
broker has no statistics for them yet. The export computes those statistics
from the field's own values, with the function the broker uses
(`umag.stats`). Two names collide: in `cases.parquet`, `vr_ring` and
`vr_exposed` are the urban vertical-area ratios, while in `fields.parquet`,
`vr_*` is the velocity ratio. Both namings are the campaign's own.

**`fields.zarr`** is a Zarr v3 store. It has a group per case and a float32
array per direction, `(y, x)`, one chunk each. Row `j`, column `i` is the point
`x0 + i·s` metres east and `y0 + j·s` metres north of the case's `lat`/`lon`,
where `s` is the spacing (the node's site frame, `umag.to_local`). Cells inside
a building are NaN. The attributes are `deg`, `height_m`, `u_ref`, `coverage`,
`lambda_f`, `origin` (`[x0, y0]`), `spacing_m`, `shape` (`[ny, nx]`),
`units: "m/s"`, and `sha256`: the hash of the umag/1 blob the values were
decoded from, which is the hash the broker's field record carries.

**`dataset.json`** is the card. It holds the broker (`url`, `version` from
`/healthz`), `exported_at`, the `filters`, and `counts` (cases, fields,
`by_split`, `by_recipe`, `by_lcz`, and `fields_per_case`, which says how many
cases have how many directions). It also holds `schema_version`, and the
sha256 and size of every file. For `fields.zarr`, which is one file per array
plus metadata, `files` gives a single digest instead: the sha256 of
`sha256sum`'s own output for every file in the store, sorted by path. Last
comes `problems`: what the selection held that the snapshot does not have,
each with its reason.

## Running it again

`--out` is the snapshot, and a re-run brings it up to date. Before downloading
a field, the export compares the `sha256` stored on its array with the hash the
broker reports. A field whose hash still matches costs nothing, so a re-run
over an unchanged campaign downloads no field. The hash is written last, after
the values. An array that a crash cut short has no hash, so the next run
fetches it again.

The tables and the card are written under a temporary name and then renamed
over the old ones. A reader sees either the old file or the new one, never half
of either. The card is written last.

After a run, the store holds exactly what `fields.parquet` points at. Cases
that the new selection leaves out are removed, so a narrower re-run shrinks the
snapshot. Exit codes:

- `0`: everything selected is there.
- `1`: a case or field could not be had. It is listed on the card and on
  stderr, and the next run retries it.
- `2`: the broker refused the token, stopped answering, or the extra is
  missing.

## Reading it

```python
import json
import numpy as np
import pandas as pd
import xarray as xr   # optional; zarr.open_group reads it too

card = json.load(open("ds/dataset.json"))
cases = pd.read_parquet("ds/cases.parquet")
fields = pd.read_parquet("ds/fields.parquet")
cases["labels"] = cases["labels"].map(dict)          # a Parquet map arrives as (key, value) pairs

train = fields.merge(cases[["case_id", "split", "lcz"]], on="case_id").query("split == 'train'")
row = train.iloc[0]

site = xr.open_zarr("ds/fields.zarr", group=row.case_id, consolidated=False)
u = site[row.direction]                               # dims (y, x), float32 m/s, NaN in buildings
x0, y0 = u.attrs["origin"]
s = u.attrs["spacing_m"]
u = u.assign_coords(x=x0 + s * np.arange(u.sizes["x"]), y=y0 + s * np.arange(u.sizes["y"]))
vr = u / u.attrs["u_ref"]                             # U/U_ref, the number that compares sites
```

To check a copy against its card:

```python
import hashlib, pathlib
root = pathlib.Path("ds/fields.zarr")
lines = sorted((p.relative_to(root).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest())
               for p in root.rglob("*") if p.is_file())
digest = hashlib.sha256("".join(f"{h}  {rel}\n" for rel, h in lines).encode()).hexdigest()
assert digest == card["files"]["fields.zarr"]["sha256"]
```

## Before releasing a dataset built from it

Heights are GlobalBuildingAtlas predictions, and GBA is **CC BY-NC 4.0**
([AGENTS.md](../AGENTS.md), "Open problems"). Sites meshed before the switch to
GBA were built from Overture. `height_source` says which source a case was
built from, and a null means the case finished before the runner reported it.
[DOMAIN.md](../DOMAIN.md), "Which buildings, for this case", explains why the
date alone does not settle that.
