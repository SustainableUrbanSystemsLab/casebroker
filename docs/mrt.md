# The MRT recipe: mean radiant temperature at pedestrian height

A third kind of case. The broker's job is the same; the case is neither a CFD
job nor a surface-temperature job, but is built ON one: it reads the site's
finished surface-temperature case ([thermal.md](thermal.md)) for the long-wave
half of the answer. Its decisions were made on 2026-10-10 and are listed below.
Changing any of them makes a different training set, which means a new recipe
**version** (`…-v2` next), never an edit to this one.

## What a case computes

For one site, the hourly mean radiant temperature a standing person would feel
at every point of the wind campaign's **pedestrian lattice**, 1.75 m above the
terrain, over the ±504 m core, for a whole year:

1. **Geometry.** The same site build as the wind and thermal campaigns: GBA buildings, the GEDTM30 terrain, WorldCover ground zones, and the Meta/WRI canopy.
2. **Short-wave.** Radiance (the same DDS chain as the thermal recipe, the same scene and materials) gives each point's hourly irradiance on an **upward-facing horizontal plane**, total and direct. Eddy3D's SolarCal (ASHRAE 55 Appendix C, standing posture, facing unknown) turns that into a solar ΔMRT: the beam is converted back to direct-normal by the sun's altitude and projected by the body's own area fraction, never by a sensor plane (Eddy3D org discussion #91); the diffuse half the body sees; and a ground-reflected share from the horizontal total at reflectance 0.2.
3. **Long-wave.** 400 rays from each point, evenly over the sphere, traced against the site in plain C#. A ray that leaves the site meets the **sky**, at the temperature the EPW's horizontal infrared gives (`(IR / σ)^¼`). One that meets a crown meets **air** at the EPW's dry-bulb temperature. One that meets ground, a roof or a facade meets that surface's hourly temperature: the nearest sensor of that kind in the site's finished `surf-1008/rad6R0P2-fft-v2` archive (`t_surface.npy`). The long-wave MRT is the fourth root of the view-weighted sum of the fourth powers.
4. **MRT** = long-wave MRT + solar ΔMRT. At night, the long-wave MRT alone.

| | |
| --- | --- |
| **Recipe** | `mrt-1008/rad6R0P2-solarcal-v1` |
| **Needs** | The site's `surf-1008/rad6R0P2-fft-v2` case, **done**. The broker never leases an MRT case before it is (`needs`, below). |
| **Sensors** | The wind campaign's pedestrian lattice: cell centres −503, −501, … 503 m, 2 m apart, 1.75 m above the terrain, 504 × 504 = 254,016 points less those under a footprint or over water or snow (the ground the thermal recipe does not sense). Each faces up. |
| **Body** | SolarCal standing, facing unknown (azimuth-averaged projected area), absorptivity 0.7, ground reflectance 0.2. |
| **Rays** | 400 per point, a Fibonacci sphere, the same set for every point. Sky, crown and each sensed surface weighted by the share of rays that reach it. |
| **Surfaces beyond the sensed core** | The terrain ring out to ±1,304 m is in the scene. A ray that meets it, or a building outside the core, takes the temperature of the **nearest sensed surface of its kind**: the archive has no sensor there. |
| **Materials, trees, sky, Radiance** | Exactly the thermal recipe's: `worldcover-v1`, the `trans` crowns, the three `gendaymtx` matrices, rad6R0P2 Standard (`-ab 3 -ad 2000 -lw 1e-4`), container or native, recorded. |
| **Weather** | The surface-temperature case's own file: the spec carries the same `weather`, and the node refuses a surface archive computed with a different file (its manifest's `weather.sha256`). |
| **Priority** | 300, so wind (50) and surface-temperature (200) cases lease first. |

## The spec

`POST /v1/cases`, `recipe: "mrt-1008/rad6R0P2-solarcal-v1"`, with
**`needs: "<surf case id>"`** beside the spec, and in `spec`:

| key | |
| --- | --- |
| `lat`, `lon`, `lcz` | as for wind |
| `surf_case` | the site's surface-temperature case: where the node fetches `t_surface.npy` and `sensors.npy` from (the broker's copy of its archive, `GET /v1/cases/{id}/parts/archive/blob`) |
| `weather.*` | copied from that case's spec, so the same file |
| `wind_case` | the site's wind case, as a link only |

**`needs`** is the broker's gate: a case that names one is handed to no node
until the named case is `done`. It is a column of its own (`needs_case`), so
the lease query checks it without opening a spec, and `GET /v1/cases/{id}`
answers `needs_state` beside it: a pending MRT case whose surface case failed
or was quarantined says so, instead of waiting forever. A `needs` that names a
case the broker does not have is refused (422).

A spec without `weather` or `surf_case`, or whose surface archive is of another
recipe or another weather file, is refused by the node with exit 64. It can never
succeed, on any machine.

## Progress

```
site geometry
scene · 251,904 sensors · surface temperatures of v2-…
trace 12/63 chunks · 3,904 daylight hours
longwave 12/63 chunks · 400 rays
archiving
```

`trace` is the long phase and drives the ETA, as for the thermal recipe. The
long-wave pass is counted separately (stage `longwave`), since it is the one
phase the thermal recipe does not have; it is minutes, not hours.

## Telemetry

`site` as for wind. `mrt` is sent before the trace, and again with its timings
when the case ends:

```json
{"sensors": 0, "surf_case": "",
 "weather": {"key": "", "distance_km": 0.0},
 "daylight_hours": 0,
 "radiance": {"engine": "container|native", "image": "", "params": "",
              "gendaymtx": {"sky": "-m 1 -O1", "sun": "-5 0.533 -m 4 -O1 -d", "direct_sky": "-m 1 -O1 -d"},
              "chunks": 0, "seconds": null},
 "longwave": {"rays": 400, "seconds": null},
 "body": "solarcal-standing", "materials": "worldcover-v1"}
```

## Completion metrics

- `recipe`, `builder` (`eddy3d-mrt`), `eddy3d_build`, `case_seconds`
- `engine`, `radiance`
- `sensors`, `daylight_hours`, `trace_seconds`, `longwave_seconds`
- `surf_case`, `weather_key`, `weather_distance_km`
- `mrt`: the p50, p95 and max of every hourly value, in °C, from 0.1 K histograms; `mrt_day` the same over daylight hours only
- `urban_form`, `height_source`, as for wind

## The archive

One `<case>.tar.gz`, as every archive. Not shipped in parts.

| file | |
| --- | --- |
| `manifest.json` | Recipe, build, engine, Radiance parameters and sky chain, the weather (key, url, distance, sha256), the surface case it read (`surf_case`, its recipe, its archive's sha256), the body model and its constants, the ray count, the material table, sensor count and spacing, and the dtype, shape, scale and unit of every array. |
| `sensors.npy` | Packed records, as the thermal recipe's: `x y z nx ny nz` (float32, site metres; `z` is 1.75 m above the terrain, the normal is up), `kind` (uint8, always 0), `material` (uint8: the WorldCover class under the point), `svf` (float32, Radiance's cosine-weighted sky view of the upward plane). |
| `mrt.npy` | int16 `[n, 8760]`, °C × 100: one row per sensor, in `sensors.npy` order. |
| `views.npy` | float32 `[n, 3]`: the share of each sensor's rays that met the sky, a crown, and a sensed surface. Sums to one. |
| `irradiance.npy` | int16 `[n, daylight_hours]`, W/m² × 10: the horizontal total, as the thermal recipe keeps it. |
| `daylight_hours.npy` | int16 `[daylight_hours]`: the hour of year of each irradiance column. |
| `weather.epw` | The file the case was computed with. |
| `spec.json` | The spec the case was leased with. |
| `geometry/<case>.json` | The site report, as for wind. |

Sensor-major, like the thermal archive, for the same reason: a node writes each
chunk's rows as it finishes, and a case never holds more than a few chunks.

## Exit codes

As for the thermal recipe: 0 done; 64 this site can never succeed (no weather,
no `surf_case`, a surface archive of another recipe or weather file, an
unpublished GBA tile); 69 no working Radiance; anything else retryable. A
surface case the broker does not hold the archive of is given back untouched,
with the attempt refunded: the broker's copy is the only one a node can fetch.

## Admitting cases

`scripts/admit_mrt.py` posts an MRT case beside every **done** surface case
whose site has none yet, with `needs` set to that case, the spec's `weather`
and `lcz` copied from it, and its `wind_case` carried over. A dry run by
default; `--post` posts in batches of 25. It refuses to post while the release
policy's `undeclared_recipes` is unset, for the same reason `admit_thermal.py`
does: a script worker handed a Radiance case gives it back with exit 69.

Sites with a wind case and no surface case first need one:
`admit_thermal.py --count <all>`; once those are done, `admit_mrt.py` picks
them up. Nothing on the node side differs between the two recipes' machines:
a node declares `mrt-1008/…` exactly where it declares `surf-1008/…`, since
both need Radiance.

## Versions

| recipe | status | |
| --- | --- | --- |
| `mrt-1008/rad6R0P2-solarcal-v1` | **current**, from 2026-10-10 | Defined against `surf-1008/rad6R0P2-fft-v2`. A new surface-temperature version is a new MRT version too: its long-wave input changes. |
