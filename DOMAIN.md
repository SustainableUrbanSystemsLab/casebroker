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

## Footprints

Overture building geometry for a case, cached per case. Pinned to the **same
release and bbox derivation the runner uses**, so the picture is the geometry
that gets meshed. Rendering anything merely similar — OSM, a map tile — would
look like a check while disagreeing with the mesh, and would disagree most
exactly where checking matters.

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

**Height provenance is first-class.** Overture heights only some buildings;
untagged ones are filled from the tile's median, or the LCZ class typical height
where a tile has too little to learn from. The report keeps them apart:

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
