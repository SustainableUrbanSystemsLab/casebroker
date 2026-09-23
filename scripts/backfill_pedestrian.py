"""Regenerate a finished case's pedestrian field from its archive, without re-solving.

Every archive the campaign shipped before the pedestrian sampler was fixed has
no pedestrian field (the old sampler cut its surface from the mesh case's
ground.stl, which holds no ground under the core; the Windows node never
sampled at all). But every archive keeps the mesh and the reconstructed last
time step of every direction, and that is all the sample needs:

  1. extract the archive (mesh*/constant/polyMesh, case_*/<latest>/, system/);
  2. find the terrain SHEET: terrain.stl in the archive when the runner put it
     there, otherwise regenerate it with `eddy3d-cli site-geometry` from the
     archived coordinates -- and refuse unless the regenerated report agrees
     with the archived one on every terrain number (DEM source, extent, stride,
     z range), because a sheet that is not the one the mesh was built on would
     put the sample at the wrong height above the real ground;
  3. per direction: link the mesh, decomposePar the last time, cut the
     pedestrian surfaces under MPI (lib/ped_grid.py's dictionary), clean up;
  4. read the surfaces onto the grid (lib/ped_field.py): pedestrian/U.npz,
     meta.json, grid.json and the dashboard's <case_id>.wfld.

Nothing in the archive is modified. Decomposing, not sampling serially: on a
campaign mesh OpenFOAM's serial cell search alone takes minutes.

  uv run --with numpy python scripts/backfill_pedestrian.py E:/wind/done/v2-....tar.gz \\
      --out E:/wind/fields --e3d C:/E3D/eddy3d-cli.exe --ranks 8

On Windows the OpenFOAM commands run through blueCFD (--bluecfd, default
C:/blueCFD-Core-2024); elsewhere through bash with OpenFOAM already sourced.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "runner" / "lib"))

import ped_field  # noqa: E402
import ped_grid  # noqa: E402

# The report fields that pin the terrain sheet. Any disagreement means the
# regenerated sheet is not the meshed one -- a DTM that fell back to flat ground
# (dem = flat-fallback) is the likely one, and it would be off by the whole relief.
TERRAIN_KEYS = ("dem", "terrain_patch_m", "terrain_stride_m", "n_faces_terrain",
                "terrain_z_min", "terrain_z_max", "terrain_stl_z_min")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def extract(archive: Path, into: Path) -> Path:
    """The archive's single top-level <case_id>/ folder, extracted under `into`."""
    into.mkdir(parents=True, exist_ok=True)
    # tarfile, not a tar binary: Git's GNU tar on Windows reads "E:\..." as a
    # remote host and mangles backslashed destinations, and bsdtar is not
    # everywhere. The "data" filter refuses absolute paths and links out of `into`.
    with tarfile.open(archive, "r:gz") as tf:
        tf.extractall(into, filter="data")
    tops = [p for p in into.iterdir() if p.is_dir()]
    if len(tops) != 1:
        raise SystemExit(f"{archive}: expected one top-level folder, found {[p.name for p in tops]}")
    return tops[0]


def study_dir(root: Path, case_id: str) -> Path:
    """The node lays the study out as <id>/<id>/case_*; run_case.sh as <id>/case_*."""
    nested = root / case_id
    return nested if any(nested.glob("case_*")) else root


def terrain_matches(archived: dict, regenerated: dict) -> list[str]:
    bad = []
    for k in TERRAIN_KEYS:
        a, b = archived.get(k), regenerated.get(k)
        if a is None:
            continue
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(a - b) > 1e-3:
                bad.append(f"{k}: archived {a}, regenerated {b}")
        elif a != b:
            bad.append(f"{k}: archived {a!r}, regenerated {b!r}")
    return bad


def find_terrain(root: Path, study: Path, case_id: str, work: Path, e3d: str | None) -> Path:
    # run_case.sh packs terrain.stl at the study root; the Windows node packs the
    # builder's own geometry/<id>_terrain.stl.
    for cand in (study / "terrain.stl", root / "terrain.stl",
                 root / "geometry" / f"{case_id}_terrain.stl"):
        if cand.is_file():
            log(f"terrain: the archive's own {cand.name}")
            return cand
    if not e3d:
        raise SystemExit("the archive carries no terrain.stl; pass --e3d to regenerate it")
    spec = json.loads((root / "spec.json").read_text(encoding="utf-8"))
    reports = sorted((root / "geometry").glob("*.json"))
    if not reports:
        raise SystemExit("the archive carries no geometry report to check a regenerated terrain against")
    archived = json.loads(reports[0].read_text(encoding="utf-8"))
    geo = work / "geometry"
    geo.mkdir(parents=True, exist_ok=True)
    log(f"terrain: regenerating from {spec['lat']}, {spec['lon']}")
    # gedtm30-strict: a flat fallback would "succeed" with the wrong ground.
    subprocess.run([e3d, "site-geometry", "--site", case_id, "--lat", str(spec["lat"]),
                    "--lon", str(spec["lon"]), "--out", str(geo), "--terrain", "gedtm30-strict"],
                   check=True, stdout=subprocess.DEVNULL)
    regenerated = json.loads((geo / f"{case_id}.json").read_text(encoding="utf-8"))
    bad = terrain_matches(archived, regenerated)
    if bad:
        raise SystemExit("the regenerated terrain is not the one this case was meshed on:\n  "
                         + "\n  ".join(bad))
    log("terrain: regenerated sheet matches the archived report on " + ", ".join(
        k for k in TERRAIN_KEYS if k in archived))
    return geo / f"{case_id}_terrain.stl"


class Foam:
    """Runs OpenFOAM commands in a case directory: blueCFD on Windows, bash elsewhere."""

    def __init__(self, bluecfd: str | None):
        self.bluecfd = bluecfd if os.name == "nt" else None

    def run(self, case: Path, commands: list[str], logname: str) -> int:
        if self.bluecfd:
            b = self.bluecfd.replace("/", "\\")
            plat = rf"{b}\OpenFOAM-12\platforms\mingw_w64Gcc122DPInt32Opt"
            lines = [
                "@echo off", "setlocal enableextensions",
                rf'call "{b}\setvars_OF12.bat" >nul 2>&1',
                rf"set PATH={b}\ofuser-of12\platforms\mingw_w64Gcc122DPInt32Opt\bin;"
                rf"{plat}\lib\MS-MPI-10.1.2;{b}\ThirdParty-12\platforms\mingw_w64Gcc122\MS-MPI-10.1.2\bin;"
                rf"%MSMPI_BIN%;{b}\msys64\usr\bin;%PATH%",
                f'cd /d "{case}"',
            ] + [f"{c} >> {logname} 2>&1" for c in commands] + ["exit /b %ERRORLEVEL%"]
            bat = case / "_backfill.bat"
            bat.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
            rc = subprocess.run(["cmd", "/c", str(bat)]).returncode
            bat.unlink(missing_ok=True)
            return rc
        script = " && ".join(f"{c} >> {logname} 2>&1" for c in commands)
        return subprocess.run(["bash", "-c", f'cd "{case}" && {script}']).returncode

    def mpi(self, n: int) -> str:
        return f"mpiexec -n {n}" if self.bluecfd else f"mpirun -np {n}"


def latest_time(case: Path) -> str | None:
    times = []
    for d in case.iterdir():
        try:
            if d.is_dir() and float(d.name) > 0:
                times.append((float(d.name), d.name))
        except ValueError:
            pass
    return max(times)[1] if times else None


def sample_direction(foam: Foam, study: Path, case: Path, ranks: int, dict_text: str,
                     core_stl: Path) -> bool:
    latest = latest_time(case)
    if latest is None:
        log(f"{case.name}: no reconstructed time step in the archive; skipped")
        return False
    mesh = study / f"mesh_{case.name[5:]}"
    if not (mesh / "constant" / "polyMesh").is_dir():
        mesh = study / "mesh"
    poly = case / "constant" / "polyMesh"
    if not (poly / "owner").exists() and not (poly / "owner.gz").exists():
        if poly.exists():
            shutil.rmtree(poly)
        shutil.copytree(mesh / "constant" / "polyMesh", poly)
    (case / "system" / "pedGridFO").write_text(dict_text, encoding="ascii")
    (case / "constant" / "triSurface").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(core_stl, case / "constant" / "triSurface" / core_stl.name)
    for p in case.glob("processor*"):
        shutil.rmtree(p)
    t0 = time.time()
    rc = foam.run(case, [
        f"foamDictionary system/decomposeParDict -entry numberOfSubdomains -set {ranks}",
        "foamDictionary system/decomposeParDict -entry method -set scotch",
        f"decomposePar -force -time {latest}",
        f"{foam.mpi(ranks)} foamPostProcess -dict system/pedGridFO -time {latest} -parallel",
    ], "backfill_ped.log")
    for p in case.glob("processor*"):
        shutil.rmtree(p, ignore_errors=True)
    ok = rc == 0 and ped_field.latest_sample_dir(case) is not None
    log(f"{case.name}: {'sampled' if ok else 'FAILED (see backfill_ped.log)'} at {latest} "
        f"in {time.time() - t0:.0f} s")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("archive", type=Path)
    ap.add_argument("--out", type=Path, required=True,
                    help="writes <out>/<case_id>/pedestrian/ and <out>/<case_id>.wfld")
    ap.add_argument("--e3d", help="eddy3d-cli, to regenerate a terrain sheet the archive lacks")
    ap.add_argument("--ranks", type=int, default=8)
    ap.add_argument("--heights", default="1.5,1.75")
    ap.add_argument("--bluecfd", default="C:/blueCFD-Core-2024")
    ap.add_argument("--work", type=Path, help="scratch (default: a temp dir, removed afterwards)")
    ap.add_argument("--keep", action="store_true", help="keep the extracted scratch")
    a = ap.parse_args()

    case_id = a.archive.name.split(".tar")[0]
    work = Path(a.work or tempfile.mkdtemp(prefix=f"ped-{case_id}-"))
    t0 = time.time()
    try:
        log(f"{case_id}: extracting {a.archive.stat().st_size / 1e9:.1f} GB into {work}")
        root = extract(a.archive, work / "x")
        study = study_dir(root, case_id)
        terrain = find_terrain(root, study, case_id, work, a.e3d)

        heights = [float(h) for h in a.heights.split(",") if h.strip()]
        hf, xs, ys, sheet = ped_grid.build(str(terrain), 504.0, 2.0)
        core_stl = work / "pedCore.stl"
        ped_grid.write_stl(str(core_stl), sheet)
        grid = {"half_m": 504.0, "spacing_m": 2.0, "heights_m": heights,
                "nx": int(xs.size), "ny": int(ys.size), "x0": float(xs[0]), "y0": float(ys[0]),
                "terrain_stride_m": list(hf.stride),
                "surfaces": {ped_grid.surface_name(h): h for h in heights},
                "order": "row-major, x fastest, y ascending"}
        grid_path = work / "grid.json"
        grid_path.write_text(json.dumps(grid, indent=2))
        dict_text = ped_grid.surfaces_dict(heights, core_stl.name)

        foam = Foam(a.bluecfd)
        cases = sorted(p for p in study.glob("case_*") if p.is_dir())
        log(f"{len(cases)} directions, {a.ranks} ranks each")
        for case in cases:
            sample_direction(foam, study, case, a.ranks, dict_text, core_stl)

        field, meta = ped_field.collect(study, grid_path, terrain)
        meta["backfilled"] = {"from": a.archive.name, "at": int(time.time())}
        dest = a.out / case_id / "pedestrian"
        ped_field.write_dataset(field, meta, dest)
        shutil.copyfile(grid_path, dest / "grid.json")
        bundle = a.out / f"{case_id}.wfld"
        bundle.write_bytes(ped_field.bundle(field, meta, case_id))
        cov = list(meta["coverage"].values())
        err = [v["p99_abs_m"] for v in meta["height_error"].values()]
        log(f"{case_id}: {field.shape[0]} directions, coverage {min(cov, default=0):.3f}-"
            f"{max(cov, default=0):.3f}, height error p99 <= {max(err, default=0):.3f} m, "
            f"missing {meta['missing'] or 'none'}; {bundle} ({bundle.stat().st_size / 1e6:.1f} MB) "
            f"in {(time.time() - t0) / 60:.1f} min")
        return 0 if not meta["missing"] else 3
    finally:
        if not a.keep and not a.work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
