#!/usr/bin/env bash
# run_all_municipalities.sh — Run the full pipeline one municipality at a
# time (tile -> compute -> merge -> benchmark), so each municipality fully
# completes before the next one starts. Prints one MUNICIPALITY_DONE line
# per completed municipality to stdout; everything else goes to
# logs/full_run.log.
#
# Usage:
#   ./etl/run_all_municipalities.sh [name1 name2 ...]
#
# With no arguments, runs every municipality found (respecting
# config.MUNICIPALITIES / config_local.py). Pass explicit names to run (or
# resume) only those.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
PYEXE="${PYEXE:-/c/Users/bethrt/AppData/Local/Programs/OSGeo4W/apps/Python312/python.exe}"
LOGFILE="logs/full_run.log"
mkdir -p logs

if [ "$#" -gt 0 ]; then
  names=("$@")
else
  mapfile -t names < <("$PYEXE" -c "
import sys; sys.path.insert(0, 'etl')
import config
for name, _, _ in config.municipality_pairs():
    print(name)
")
fi

echo "Processing ${#names[@]} municipalities" >> "$LOGFILE"

for name in "${names[@]}"; do
  {
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') START $name ==="
    MUNICIPALITY_OVERRIDE="$name" "$PYEXE" etl/01_tile_dem.py
    MUNICIPALITY_OVERRIDE="$name" "$PYEXE" etl/02_compute_viewsheds.py --workers 4 --resume
    MUNICIPALITY_OVERRIDE="$name" "$PYEXE" etl/03_merge_tiles.py
  } >> "$LOGFILE" 2>&1

  "$PYEXE" etl/generate_benchmark_report.py >> "$LOGFILE" 2>&1
  "$PYEXE" etl/print_municipality_summary.py "$name"
done

echo "ALL_MUNICIPALITIES_DONE"
