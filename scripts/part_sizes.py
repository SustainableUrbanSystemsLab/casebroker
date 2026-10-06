#!/usr/bin/env python3
"""Where a case's parts spend their bytes, by kind of file.

A campaign case is ~8.5 GB of parts (2026-10-06: the mesh ~185 MB, each of 32
directions ~260 MB, all gzip). Before buying disks for 42 TB, or retiring
Syncthing on a store that cannot hold it, it is worth knowing what those
260 MB are: solver fields (which ones -- `phi`, a face flux, is several times
a cell field), logs, post-processing, decomposed copies. This reads part
archives on the master and prints, per kind, the size unpacked and gzip'd.

    python3 scripts/part_sizes.py /path/to/done/<case>.case_000.tar.gz [more ...]
    python3 scripts/part_sizes.py --sample 5 /path/to/done        # five direction parts

Read-only. Standard library only; with the `zstandard` module installed it also
says what zstd -19 would make of the same bytes.
"""
from __future__ import annotations

import argparse
import re
import sys
import tarfile
import zlib
from collections import defaultdict
from pathlib import Path

FIELDS = {"U", "p", "p_rgh", "k", "epsilon", "omega", "nut", "nuTilda", "phi", "T", "alphat", "s", "yPlus"}


def kind(name: str) -> str:
    parts = name.split("/")
    leaf = parts[-1]
    if "polyMesh" in parts:
        return "mesh (polyMesh)"
    if any(p.startswith("processor") for p in parts):
        return "decomposed (processor*)"
    if "postProcessing" in parts:
        return "postProcessing"
    if leaf.startswith("log") or leaf.endswith(".log"):
        return "logs"
    if "system" in parts or "constant" in parts:
        return "setup (system, constant)"
    if len(parts) >= 2 and re.fullmatch(r"[0-9.eE+-]+", parts[-2]):
        base = leaf.split(".")[0]
        return f"field {base}" if base in FIELDS else "field (other)"
    if leaf.endswith((".stl", ".obj")):
        return "geometry"
    return "other"


def measure(archives: list[Path]) -> None:
    try:
        import zstandard
        zstd = zstandard.ZstdCompressor(level=19)
    except ImportError:
        zstd = None
    raw, gz, zs, count = defaultdict(int), defaultdict(int), defaultdict(int), defaultdict(int)
    total_file = 0
    for path in archives:
        total_file += path.stat().st_size
        with tarfile.open(path, "r:gz") as tf:
            for m in tf:
                if not m.isfile():
                    continue
                data = tf.extractfile(m).read()
                k = kind(m.name)
                count[k] += 1
                raw[k] += len(data)
                gz[k] += len(zlib.compress(data, 6))
                if zstd is not None:
                    zs[k] += len(zstd.compress(data))
    n = len(archives)
    print(f"{n} archive(s), {total_file / 1e6:,.1f} MB on disk, {total_file / max(n, 1) / 1e6:,.1f} MB each\n")
    cols = f"{'kind':28s} {'files':>6s} {'unpacked MB':>12s} {'gzip MB':>9s} {'share':>6s}"
    print(cols + (f" {'zstd-19 MB':>11s}" if zstd else ""))
    total_gz = sum(gz.values()) or 1
    for k in sorted(gz, key=gz.get, reverse=True):
        line = f"{k:28s} {count[k] / n:6.0f} {raw[k] / n / 1e6:12.1f} {gz[k] / n / 1e6:9.1f} {gz[k] / total_gz:6.0%}"
        if zstd:
            line += f" {zs[k] / n / 1e6:11.1f}"
        print(line)
    print(f"\n{'per archive':28s} {'':6s} {sum(raw.values()) / n / 1e6:12.1f} {total_gz / n / 1e6:9.1f}"
          + (f" {'':6s} {sum(zs.values()) / n / 1e6:11.1f}" if zstd else ""))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", type=Path, help="part archives, or a done folder with --sample")
    ap.add_argument("--sample", type=int, default=0, help="from a done folder: this many direction parts")
    a = ap.parse_args(argv)
    archives: list[Path] = []
    for p in a.paths:
        if p.is_dir():
            found = sorted(p.glob("*.case_*.tar.gz"))
            archives += found[: a.sample or 3]
        else:
            archives.append(p)
    if not archives:
        print("no part archives found", file=sys.stderr)
        return 1
    measure(archives)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
