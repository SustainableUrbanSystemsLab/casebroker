# Domain model

The nouns this service deals in, what each one guarantees, and the invariants
that must hold. Written because most of the bugs this repo has had were not
coding errors but disagreements about what one of these words meant.

---

## Case

A **site** plus a **recipe**. One CFD job.

| Field | Meaning |
| --- | --- |
| `case_id` | `v2-<16 hex>`, from `sha256(site_key, recipe)`. Permanent. |
| `spec` | `lat`, `lon`, `recipe`, `lcz`, sampler provenance. What the runner receives. |
| `city_cluster` | Which city this tile belongs to. Decides the split. |
| `split` | `train` / `val` / `test`. **Server-derived, never trusted from a client.** |
| `state` | `pending` → `leased` → `done` \| `quarantined` |
| `attempts` / `max_attempts` | Retry budget, default 3. |
| `priority` | Lease order, ascending. Default 100. |
| `result_uri`, `result_sha256`, `result_bytes` | Where the answer lives. The broker never opens it. |

**A case id depends only on where and how, never on when or on how many exist.**
That is what makes adding cases a pure append: no renumbering, no reshuffling,
and re-posting an existing id is a no-op. Changing the recipe deliberately
produces a *different* id for the same coordinates, so a re-spec coexists with
the original instead of overwriting it.

**Split is assigned by city, not by tile.** Tiles from one city share morphology
and often literal buildings at their edges, so a per-tile split leaks the test
set into training. `ids.split_for(city_cluster)` hashes the city, which also
means an existing case can never change split as the dataset grows.

---

## Lease

**The unit of ownership — not the case.** A worker holds a `lease_id`, and every
call after the first is keyed by it.

That indirection is the whole concurrency story. It is what lets the broker tell
the rightful owner from a worker that woke up late still holding a stale claim:
the superseded worker's `lease_id` no longer matches, so its `complete` is
refused. Key it by `case_id` instead and a zombie silently overwrites a good
result, which looks exactly like success.

- Default TTL **900 s**, renewed by heartbeat every **300 s** — a 3× margin.
- **Lease expiry is the only liveness mechanism.** No reaper process exists. A
  dead worker's case is reclaimed by the same SQL statement that hands out fresh
  work, the next time anyone asks for some.
- **A 409 means stop.** On heartbeat or complete, it means the case belongs to
  someone else now. Continuing burns core-hours on a result that will be refused.

---

## The three ways a worker can stop

They are genuinely different and the code treats them differently.

| | Mechanism | Attempt |
| --- | --- | --- |
| **Preempted** (SIGTERM) | Client calls `release`; state → `pending` | **Refunded** — preemption is not the case's fault |
| **Killed** (no warning) | Heartbeats stop; TTL lapses; next lease reclaims | Consumed |
| **Zombie returns** | `complete` with a superseded `lease_id` → **409** | Untouched; the real owner's result stands |

---

## Retryable vs fatal — the distinction that cost 173 cases

The runner's exit code is a contract:

- **`0`** — success
- **`64`** — *this site is broken and must never be retried.* Degenerate
  geometry that will fail identically on every machine, forever.
- **anything else** — retryable: a node died, an image pull failed, a DTM host
  was down.

Getting this wrong is expensive in **one direction only**. A wrongly retryable
error costs at most three attempts. A wrongly fatal one removes a site from the
campaign permanently, recoverable only by editing the database by hand. A
missing input file is *tooling not ready*, not a broken site — treating it as
fatal quarantined 173 perfectly good sites in under a minute.

Note that a *systematic* retryable failure still exhausts `max_attempts` and
quarantines. Retryable buys three chances, not immunity.

---

## Worker

Identified by `worker_id`, self-reported. Also reports `host` and `cluster`, so
"which machine produced this case" stays answerable after the SLURM log has
rotated away. Workers appear only on first lease — **the broker cannot see a
scheduler**, which is why `Fleet` exists.

## Fleet

A **reported** snapshot of what a scheduler holds: `cluster`, `queued`,
`running`, `reported_at`. Pushed by `casebroker fleet` from a login node,
because a queued worker has never contacted the broker and does not exist here
until its first lease.

Always read back with `age_seconds`. A snapshot nobody has refreshed describes a
queue that has moved on, and presenting that as current is the one way this can
mislead.

## Cases that are not on land

`POST /v1/cases` drops any site the building atlas does not cover, reports the
count as `rejected_not_on_land` and names a sample. It does not refuse the
batch: a 5,000-case draw with twenty bad sites in it should land the other
4,980, and the sampler's loader raises on a rejected batch, so refusing would
turn one ocean site into a blocked draw.

The mask is the 922 tile keys GlobalBuildingAtlas actually publishes, vendored
in `casebroker/_gba_tiles.py`. A global 5° grid would be 2,592 tiles; the 1,670
GBA omits are ocean and ice. Using the building source as the land mask means
"the atlas does not cover this point" and "this case has no buildings" are one
statement rather than two that must be kept in agreement.

**It is coarse, and that is the trade.** A tile is ~550 km at the equator, so
the middle of the Atlantic fails and a point 2 km off a fjord passes. An
admission check has to run on 5,000 cases in one request and must never cost
the campaign a real Arctic city — Norilsk, Murmansk, Tromsø and Utqiagvik are
genuine urban fabric the sampler keeps deliberately. The exact question is the
preview's, per case, from three independent sources.

Cases drawn before the gate existed are swept by `POST /v1/cases/land-audit`,
dry-run by default. They are **quarantined, not deleted**: nothing leases a
quarantined case, the row and its event trail stay auditable, the decision is
reversible, and the campaign's record of what its sampler actually produced
stays honest. `done` cases are left alone — they already cost their core-hours,
and relabelling them rewrites history.

The root cause is upstream, and is now gated there too. `site_sampler.py`'s LCZ
raster reads snow, ice and open water as built classes, and its purity test
cannot catch that because a uniformly misread surface is 100% pure. Its polar
gate handles the poles by latitude (72°N, and all of Antarctica), which by
construction says nothing about 7.5°N 37.5°W in the middle of the Atlantic — so
the sampler now applies the same published-tile mask to its candidate pool
before a draw, counting `rejected_not_on_land` separately from `rejected_polar`
because ice and water are different misreads with different remedies.

That makes the broker's check a backstop rather than the only line: it still
catches hand-added cases and anything drawn by an older sampler. `SAMPLER_VERSION`
moved to `sampler-v3` for the gate change, while the rank salt stayed at
`sampler-v1`, so a re-draw is a filter of the old pool rather than a new one.

---

## Footprints

What a case is **made of**, cached per case, from the **same sources the runner
meshes**. Three layers, one payload:

- **Buildings** — GlobalBuildingAtlas by default, Overture only if GBA is
  unreachable *and* `CASEBROKER_OVERTURE_FALLBACK` is set (the response says
  which, via `source`). Queried over 520 m: the 504 m core plus a margin.
- **`terrain`** — GEDTM30 relief over the whole 1304 m mesh domain, as a coarse
  grid plus min/max/relief. `source` is `gedtm30`, `flat` (genuine nodata, so
  the builder will mesh a flat plane — usually a site not on land), or
  `unavailable` (the DTM host, not the site).
- **`canopy`** — Meta/WRI 1 m canopy heights over the same domain, as a coarse
  grid plus coverage and tallest tree. `source` is `meta-wri-chm-v1`, `none`
  (the product publishes no tile here) or `unavailable`. A treeless site and an
  uncovered one are different answers and are reported as such.

Both rasters span 1304 m rather than 520 m because that is what
`site_geometry.build_site` reads: `half_t = HALF_M + buffer_m`, 504 + 800. The
buffer is most of the domain, and a preview cropped to the buildings would show
a third of the ground the solve actually sits on.

Rendering anything merely similar — OSM, a map tile, a 10 m canopy raster, or
the other building source — would look like a check while disagreeing with the
mesh, and would disagree most exactly where checking matters.

Read from the Source Cooperative GeoParquet mirror, not TUM's own WFS: that
endpoint now answers `GetFeature` with `PARAMETER_NOT_ALLOWED`, serving only
`GetCapabilities` and `DescribeFeatureType`. The mirror carries a `bbox` struct
column, so a bounding-box predicate prunes row groups and one site costs a few MB
of range reads against a tile that can be 1.7 GB.

**The tile key fails silently when wrong.** Keys are `{west}_{north}_{east}_{south}`
with hemisphere letters carrying ABSOLUTE values, and the pairs run (west, north)
then (east, south) — so latitude descends while longitude ascends. A wrong key is
usually a 404, but it can also be a real tile for the wrong part of the world,
which returns buildings and produces a case that meshes, solves, and is somewhere
else. `tests/test_gba_tiles.py` pins it.

The canopy tile key has the same shape of trap. The Meta/WRI tiles are named by
**zoom-9 Bing quadkey** — 40,075,017 m / 512 / 65,536 = 1.194 m, which is the
product's own resolution and fixes the zoom. A key one level out is still a
valid quadkey naming a real object, so it fails by drawing someone else's trees.
The broker computes the key rather than downloading the 15 MB `tiles.geojson`
index `canopy.py` uses: same answer, 15 MB less resident memory in a capped web
process. `tests/test_chm_tiles.py` pins it against the published objects on four
continents.

**Heights, relief and canopy are all predictions or samples, not surveys.** GBA
publishes a per-building variance and the inspector draws it; GEDTM30 is a 30 m
DTM resampled to an 80 m preview cell; the canopy grid is a mean over its cell,
which is what a crown-volume drag term integrates. Statistics are taken at four
times the drawing resolution before the grid is averaged down — computing them
at drawing resolution reported Atlanta, a city of 25 m oaks, as having a tallest
tree of 8 m.

---

## Geometry, and what is real in it

Built per case from coordinates alone. Two surfaces:

- **buildings.stl** — watertight union of extruded footprints
- **terrain.stl** — the ground slab; only buildings need to be watertight

Two Z data that are easy to confuse and mean different things:

| | What it is | Used for |
| --- | --- | --- |
| `slab_base_z` | The slab's artificial underside, ~20 m below the lowest ground | The **domain floor**, which must seal against it |
| `terrain_z_min` | The real ground surface minimum | **`zGround`**, the ABL datum in `U = (U*/κ)·ln((z − z_g + z_0)/z_0)` |

Using one where the other belongs displaces the inlet profile by tens of metres,
and the case still meshes, still converges, and is wrong.

**Heights come from GlobalBuildingAtlas, and they are PREDICTIONS.**

GBA (TUM, ESSD 2025) gives a modelled height for every building — >97% global
completeness, RMSE 1.5–8.9 m by continent — from spaceborne lidar plus optical
and radar imagery. It replaced Overture as the default height source because
Overture is a footprint source that happens to tag a few heights: on the Nanjing
tile `v2-02c16d2609798e9c` it tagged **17 of 1,099**, and those 17 are landmark
towers, so their median is 75 m. The LCZ4 class prior is 40 m. GBA's median over
1,321 buildings is **9.3 m** — the tile is low-rise, and both of the other
answers built it as high-rises.

**A prediction is never labelled measured.** On the GBA path the report sets
`frac_prisms_measured` to **0.0** and `frac_prisms_predicted` to 1.0, with
`height_kind: predicted`. The field counting "prisms whose tag does not say
inferred" would otherwise report 100% measured for a tile where every height is
model output, which is the exact mislabelling these fields exist to prevent.

GBA also gives a per-building **variance**, carried into the report as
`height_var_median` / `_p90` / `_max` and `n_high_variance` (variance > 25). On
that Nanjing tile: median 3.65, p90 15.25, max 175, 54 buildings above 25. A
height predicted with variance 3 and one predicted with variance 175 are not the
same claim.

**Licence: GBA is CC BY-NC 4.0** — non-commercial, and stricter than everything
else here (Overture ODbL/CDLA, GEDTM30 CC BY 4.0). It constrains how a dataset
built on it may be released.

The Overture path remains, selectable with `--heights overture`, because a case
meshed before the switch was built from it and redrawing it from GBA would
misrepresent what was solved. On that path the older fields still apply:

- `height_provenance` / `frac_measured_height` — what **Overture tagged**
- `prism_provenance` / `frac_prisms_measured` — what was **actually extruded**

On a real Shanghai tile these read 0% and 9.6%. A model trained without knowing
which heights were invented cannot tell you what it learned.

---

## Invariants

1. A `case_id` is a pure function of `(lat, lon, recipe)`.
2. `split` is a pure function of `city_cluster` — server-side, always.
3. Selection is a **threshold** on a coordinate hash, never top-N. Raising a
   threshold can only *add* sites; top-N re-cuts and can drop a site that has
   already cost 66 core-hours.
4. Only a matching `lease_id` may write a result.
5. `complete` is idempotent for the *same* `result_uri`; a *different* result for
   a finished lease is still refused.
6. `POST /v1/cases` is idempotent by `case_id`.
7. A write token passes every read gate; a read token passes none of the write
   gates.
