"""
print_municipality_summary.py — Print a one-line summary for one municipality
from logs/benchmark.csv, for use by scripted per-municipality runs.

Usage:
    python etl/print_municipality_summary.py <municipality_name>
"""

import csv
import sys

import config


def main():
    name = sys.argv[1]
    rows = []
    if config.BENCHMARK_LOG.exists():
        with open(config.BENCHMARK_LOG, newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if r["municipality"] == name]

    by_stage = {}
    for r in rows:
        by_stage[r["stage"]] = r  # last one wins if a stage was re-run

    merge_r = by_stage.get("merge_tiles")
    if not merge_r:
        print(f"MUNICIPALITY_INCOMPLETE name={name} — merge step never completed, check logs/full_run.log")
        return

    total = sum(float(by_stage[s]["seconds"]) for s in ("tile_dem", "compute_viewsheds", "merge_tiles") if s in by_stage)
    comp_r = by_stage.get("compute_viewsheds")
    trees = comp_r["trees"] if comp_r and comp_r["trees"] else "0"
    tiles = (comp_r["tiles"] if comp_r and comp_r["tiles"] else
             by_stage["tile_dem"]["tiles"] if "tile_dem" in by_stage else "?")
    errors = comp_r["errors"] if comp_r and comp_r["errors"] else "0"
    print(f"MUNICIPALITY_DONE name={name} tiles={tiles} trees={trees} errors={errors} time={total:.1f}s")


if __name__ == "__main__":
    main()
