# The thermal recipe: surface temperatures from Radiance

A second kind of case. The broker's job is the same, but the case is not a CFD
job. Its decisions were made on 2026-09-26 and are listed below. Changing any of
them makes a different training set, which means a new recipe **version**
(`…-v3` next), never an edit to this one. The current version is **v2**; what
changed from v1, and why v1 is withdrawn, is under [Versions](#versions).

## What a case computes

For one site, the hourly surface temperature of every **ground, roof and facade**
sensor in the ±504 m core, for a whole year:

1. **Geometry.** The same site build as the wind campaign: GBA buildings, the GEDTM30 terrain, WorldCover ground zones, and the Meta/WRI canopy.
2. **Radiance** (the DDS chain) gives each sensor's hourly incident shortwave irradiance on its own plane. Hours with no sun are skipped: an hour counts only if the EPW has solar radiation in it.
3. **Eddy3D's FFT admittance solver** turns that irradiance into surface temperature. It uses the sensor's assembly, the EPW air temperature, the sky temperature from the EPW's horizontal infrared, and the sensor's own sky view for long-wave exchange. Convection is DOE-2 `hc = 5.7 + 3.8·v`, with `v` the EPW wind.

Radiance itself computes no temperatures. The recipe's physics is the admittance solver, fed by Radiance.

| | |
| --- | --- |
| **Recipe** | `surf-1008/rad6R0P2-fft-v2` |
| **Sensors** | Ground at 2 m, on the wind campaign's pedestrian lattice and seated on the terrain; roofs and facades at 4 m. Each faces along its surface normal, 5 cm off the surface. Points inside another building (the GBA prisms are not unioned) are dropped, and so is ground under water or snow. |
| **Materials** | `worldcover-v1`, below. Buildings keep 22 °C behind the wall; ground assemblies use the EPW's annual mean air temperature at depth. |
| **Trees** | The canopy crowns are a Radiance `trans` material: each face reflects 0.15 and transmits 0.45, diffusely, so about 0.2 gets through a crown's two faces. |
| **Sky view** | Each sensor's, read off its chunk's own direct-only sky trace: open ground 1, an open wall ½. It sets the long-wave exchange. |
| **Weather** | The nearest TMYx EPW in Eddy3D's climate catalogue, chosen when the case is admitted. |
| **Radiance** | rad6R0P2, Standard preset (`-ab 3 -ad 2000 -lw 1e-4`). The container image is used where a container engine answers; otherwise native Windows (the pinned zip plus Git Bash). Each case records which it used. |
| **Sky** | Three `gendaymtx` matrices from the EPW's daylight hours: the sky `-m 1 -O1` (Perez sky and sun, for the indirect pass), the direct sky `-m 1 -O1 -d` (sun only, subtracted), and the sun `-5 0.533 -m 4 -O1 -d` (2,305 suns of 0.533°, **sun only**). Each case records the three flag strings. |
| **Priority** | 200, so wind cases lease first. |

### Materials: `worldcover-v1`

Each surface's Radiance reflectance is **1 − its assembly's absorptivity**, so
Radiance and the solver agree on what it absorbs.

| surface | assembly | reflectance |
| --- | --- | --- |
| built-up ground (WorldCover 50), unclassified ground | `concrete_ground` | 0.35 |
| tree, shrub, grass, crop, wetland, mangrove, moss (10, 20, 30, 40, 90, 95, 100) | `grass_ground` | 0.25 |
| bare ground (60) | `brick_ground` | 0.30 |
| snow (70), water (80) | no sensor | 0.70, 0.07 |
| roofs | `concrete_roof` | 0.15 |
| facades | `brick_wall` | 0.30 |

## The spec

`POST /v1/cases`, `recipe: "surf-1008/rad6R0P2-fft-v2"`, and in `spec`:

| key | |
| --- | --- |
| `lat`, `lon`, `lcz` | as for wind |
| `weather.key` | the catalogue key of the chosen station |
| `weather.url` | where the node downloads it |
| `weather.distance_km` | station to site, so a far-away station shows in the data |
| `weather.catalogue` | the catalogue build the choice was made against |
| `wind_case` | the site's wind case id, as a link only: a thermal case never waits for it |

A spec without `weather` is refused by the node with exit 64. It can never
succeed, on any machine.

## Progress

Heartbeat lines follow the generic grammar the broker parses: a phase word, then
optionally `a/b unit`, then `·`-separated detail.

```
site geometry
scene · 312,440 sensors
trace 12/36 chunks · 3,904 daylight hours
archiving
```

`trace` is the long phase and drives the ETA. A chunk counts once it has been
traced AND solved: the admittance solve runs on each chunk as it leaves Radiance,
so there is no separate phase for it. The broker also knows a `surface a/b chunks`
phase, for a node that ever splits the two.

## Telemetry

`site` is the same as for wind. `thermal` is sent before the trace, and again with
its timings when it ends:

```json
{"sensors": {"ground": 0, "roof": 0, "facade": 0},
 "weather": {"key": "", "distance_km": 0.0},
 "daylight_hours": 0,
 "radiance": {"engine": "container|native", "image": "", "params": "",
              "gendaymtx": {"sky": "-m 1 -O1", "sun": "-5 0.533 -m 4 -O1 -d", "direct_sky": "-m 1 -O1 -d"},
              "chunks": 0, "seconds": null},
 "materials": "worldcover-v1"}
```

## Completion metrics

- `recipe`, `builder` (`eddy3d-thermal`), `eddy3d_build`, `case_seconds`
- `engine`, `radiance` (what produced the numbers: the image, or the native build)
- `sensors`, `sensors_ground`, `sensors_roof`, `sensors_facade`
- `daylight_hours`, `trace_seconds`
- `weather_key`, `weather_distance_km`
- `t_surface`: per surface kind, the p50, p95 and max of every hourly value, in °C, from 0.1 K histograms
- `urban_form`, as for wind

## The archive

One `<case>.tar.gz` through the same Syncthing hand-off. It isn't shipped in
parts, since a thermal case has no mesh to continue from.

| file | |
| --- | --- |
| `manifest.json` | Recipe, build, engine, Radiance parameters and sky chain (`radiance.gendaymtx`: the `sky`, `sun` and `direct_sky` flags), weather (key, url, distance, sha256), the material table, the crown optics, the temperatures behind each surface, sensor counts and spacing, and the dtype, shape, scale and unit of every array. |
| `sensors.npy` | Packed records: `x y z nx ny nz` (float32, site metres: x east, y north), `kind` (uint8: 0 ground, 1 roof, 2 facade), `material` (uint8: the WorldCover class on the ground, 200 roof, 201 facade), `svf` (float32). |
| `t_surface.npy` | int16 `[n, 8760]`, °C × 100: one row per sensor, in `sensors.npy` order. |
| `irradiance.npy` | int16 `[n, daylight_hours]`, W/m² × 10. |
| `daylight_hours.npy` | int16 `[daylight_hours]`: the hour of year of each irradiance column. |
| `weather.epw` | The file the case was computed with. |
| `spec.json` | The spec the case was leased with. |
| `geometry/<case>.json` | The site report, as for wind. |

The arrays are **sensor-major**: one row per sensor. A node writes them chunk by
chunk, as Radiance finishes each block of sensors. Hour-major would mean holding
a whole case in memory, about 8 GB, which a lab PC doesn't have. For a field of
one hour, read a column; `np.load(..., mmap_mode="r")[:, h]` doesn't load the rest.

## Exit codes

As for wind:
- **0:** done.
- **64:** this site can never succeed, for example no weather in the spec or an unpublished GBA tile.
- **69:** this machine has no working Radiance.
- **Anything else:** retryable.

A disk-full write gives the case back and stops the node, as it does for wind.

## Versions

A version is a training set. Two versions are never pooled: the broker keys every
aggregate by the exact recipe, and a node declares the exact recipes it builds, so
it is never handed another version's case.

| recipe | status | |
| --- | --- | --- |
| `surf-1008/rad6R0P2-fft-v2` | **current**, from 2026-09-29 | The sun matrix is sun-only (`gendaymtx … -d`). Eddy3D PR #960. |
| `surf-1008/rad6R0P2-fft-v1` | **withdrawn** 2026-09-29, never admitted | The sun matrix carried the whole Perez sky as well as the sun. |

**Why v1 is withdrawn.** The DDS chain adds each sensor's direct sun, traced
against the fine sun matrix, to its sky, traced against the full sky with the
coarse direct sun subtracted. v1 built the sun matrix without `-d`, so every one
of its suns also carried the whole sky, and the direct term re-counted about
2.6 % of each sensor's diffuse sky. That is about 1.1 % of total irradiance on
open ground and about 30 % of "direct" on a north wall. `irradiance.npy` and
`t_surface.npy` both change, which makes a different training set.

**What happened to v1.** Nothing needed to. On 2026-09-29 the production broker
held no v1 case in any state (admitted, leased or done), and no worker had ever
declared the recipe: the fleet's builds predate the thermal runner. An Eddy3D
build with the v2 change declares v2 only and hands a v1 case back untouched,
with the reason. An older build that declares v1 keeps its declaration, and
`scripts/admit_thermal.py` refuses to post v2 while any v1 case is still pending
or leased, so the two are never produced side by side.
