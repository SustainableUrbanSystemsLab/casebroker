# The thermal recipe: surface temperatures from Radiance

A second kind of case. The broker's job is the same, but the case is not a CFD
job. Its decisions were made on 2026-09-26 and are listed below. Changing any of
them makes a different training set, which means a new recipe **version**
(`…-v2`), never an edit to this one.

## What a case computes

For one site, the hourly surface temperature of every **ground, roof and facade**
sensor in the ±504 m core, for a whole year:

1. **Geometry.** The same site build as the wind campaign: GBA buildings, the GEDTM30 terrain, WorldCover ground zones, and the Meta/WRI canopy.
2. **Radiance** (the DDS chain) gives each sensor's hourly incident shortwave irradiance on its own plane. Hours with no sun are skipped: an hour counts only if the EPW has solar radiation in it.
3. **Eddy3D's FFT admittance solver** turns that irradiance into surface temperature. It uses the sensor's assembly, the EPW air temperature, the sky temperature from the EPW's horizontal infrared, and the sensor's own sky view for long-wave exchange. Convection is DOE-2 `hc = 5.7 + 3.8·v`, with `v` the EPW wind.

Radiance itself computes no temperatures. The recipe's physics is the admittance solver, fed by Radiance.

| | |
| --- | --- |
| **Recipe** | `surf-1008/rad6R0P2-fft-v1` |
| **Sensors** | Ground at 2 m, on the wind campaign's pedestrian lattice and seated on the terrain; roofs and facades at 4 m. Each faces along its surface normal, 5 cm off the surface. Points inside another building (the GBA prisms are not unioned) are dropped. |
| **Materials** | Ground: an assembly and reflectance per WorldCover class. Roofs: `concrete_roof`. Facades: `brick_wall`. Buildings keep 22 °C behind the wall; ground assemblies use the EPW's annual mean air temperature at depth. |
| **Trees** | Canopy columns are a Radiance `trans` material with shortwave transmittance 0.2. |
| **Weather** | The nearest TMYx EPW in Eddy3D's climate catalogue, chosen when the case is admitted. |
| **Radiance** | rad6R0P2, Standard preset (`-ab 3 -ad 2000 -lw 1e-4`). The container image is pinned by digest; native Windows (the pinned zip plus Git Bash) is allowed. Each case records which it used. |
| **Priority** | 200, so wind cases lease first. |

## The spec

`POST /v1/cases`, `recipe: "surf-1008/rad6R0P2-fft-v1"`, and in `spec`:

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
surface 30/36 chunks
archiving
```

`trace` (Radiance) is the long phase and drives the ETA.

## Telemetry

`site` is the same as for wind. `thermal` carries:

```json
{"sensors": {"ground": 0, "roof": 0, "facade": 0},
 "weather": {"key": "", "distance_km": 0.0},
 "daylight_hours": 0,
 "radiance": {"engine": "container|native", "image": "", "params": "", "chunks": 0, "seconds": 0},
 "surface": {"seconds": 0},
 "materials": "worldcover-v1"}
```

## Completion metrics

- `recipe`, `eddy3d_build`, `engine`, `case_seconds`
- `sensors`, `sensors_ground`, `sensors_roof`, `sensors_facade`
- `daylight_hours`, `trace_seconds`, `surface_seconds`
- `weather_key`, `weather_distance_km`
- `t_surface`: per surface kind, the p50, p95 and max of the hourly field, in °C

## The archive

One `<case>.tar.gz` through the same Syncthing hand-off. It isn't shipped in
parts, since a thermal case has no mesh to continue from.

| file | |
| --- | --- |
| `manifest.json` | Recipe, build, engine, Radiance parameters, weather (key, url, sha256), sensor counts, material table, and the scale and offset of every array. |
| `sensors.npy` | Structured: `x y z nx ny nz` (float32, site frame), `kind` (uint8: 0 ground, 1 roof, 2 facade), `material` (uint8), `svf` (float32). |
| `t_surface.npy` | int16 `[8760, n]`, °C × 100. |
| `irradiance.npy` | int16 `[daylight_hours, n]`, W/m² × 10. |
| `daylight_hours.npy` | int16 `[daylight_hours]`: the hour of year of each irradiance row. |
| `weather.epw` | The file the case was computed with. |
| `<site>.json` | The site report, as for wind. |

## Exit codes

As for wind:
- **0:** done.
- **64:** this site can never succeed, for example no weather in the spec or an unpublished GBA tile.
- **69:** this machine has no working Radiance.
- **Anything else:** retryable.

A disk-full write gives the case back and stops the node, as it does for wind.
