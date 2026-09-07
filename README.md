# 3-regel — South Holland "3-30-300" tree-viewshed ETL

Implements the **"3"** of the Dutch 3-30-300 greenery rule: for every pixel of
a municipality's DEM, how many trees is it visible from within a 30 m radius.

This replaces an earlier QGIS/PyQGIS prototype which wrote one output raster
**per individual tree** — infeasible at province scale (millions of trees).
Instead this pipeline accumulates visible-pixel counts per DEM tile and
merges once per municipality.

## Setup

GDAL/OGR comes from a **QGIS / OSGeo4W install**, not pip. Machine-specific
settings (where that install lives, where the source data lives, which
municipalities to process) are kept out of the tracked config:

1. Copy `etl/config_local.example.py` to `etl/config_local.py`.
2. Fill in `OSGEO4W_ROOT` (your QGIS/OSGeo4W install) and `VIEWANALYSE_DIR`
   (see "Source data" below).
3. `etl/config_local.py` is gitignored — it never gets committed, so real
   paths are safe there.

Run scripts with the same Python that has access to that OSGeo4W
`site-packages` (or from an OSGeo4W shell).

Before running the pipeline, build two combined sources once (see
"Cross-municipality context" below):

```
gdalbuildvrt data/interim/province_dem.vrt "<VIEWANALYSE_DIR>\*.tif"
# then merge every municipality's tree .gpkg into one combined file —
# see git history for the exact commands used (ogr2ogr's CLI has a
# filename-quoting bug with some accented/apostrophe basenames; the Python
# API's gdal.VectorTranslate() was used as a workaround for those).
```

## Source data

One DEM (`.tif`, 0.5 m RD New / EPSG:28992) + one tree-position layer
(`.gpkg`) per municipality, sharing a basename (e.g. `Papendrecht.tif` /
`Papendrecht.gpkg`), pointed at by `VIEWANALYSE_DIR` in your
`config_local.py`.

The tree layers originally shipped as `.shp` with no spatial index, which
made every tile's bounding-box query scan the *entire* file — cost that
scales with tile-count × total-features, and got dramatically worse on
bigger municipalities (measured ~280x slower per query on an unindexed
file vs. one converted to GeoPackage, which has a built-in R-tree index).

Some municipality DEMs are tens of gigabytes, so the pipeline reads
directly from wherever `VIEWANALYSE_DIR` points — nothing is copied locally
or committed to git.

`MUNICIPALITIES` in `config_local.py` restricts which municipalities are
processed; `[]` means every municipality found. `CORRUPTED_DEM_MUNICIPALITIES`
is always excluded regardless of `MUNICIPALITIES` — see "Known data issues".

## Pipeline (ETL)

| Stage | Script | Does |
|---|---|---|
| Extract | `etl/01_tile_dem.py` | Per municipality: splits its DEM into tiles with a buffer halo on each side, reading pixel data from `config.PROVINCE_DEM_VRT` (not the municipality's own .tif) so a tile near a municipality edge still gets real neighbour context. Writes `tile_index.json` (each tile's buffered *and* inner extents). |
| Transform | `etl/02_compute_viewsheds.py` | Per municipality, per tile: reads trees from `config.PROVINCE_TREES_GPKG` within the tile's *buffered* extent (so a neighbour-owned tree near any boundary is still counted), samples each tree's height from the DEM, runs `gdal.ViewshedGenerate`, accumulates visible-pixel counts, then crops the result down to the tile's non-overlapping *inner* window before writing. Parallel across tiles. |
| Load | `etl/03_merge_tiles.py` | Per municipality: mosaics all (non-overlapping) tile results via a VRT and translates to one Cloud-Optimized GeoTIFF (COG) per municipality. |

Run in order:

```
python etl/01_tile_dem.py
python etl/02_compute_viewsheds.py --workers 4 --resume
python etl/03_merge_tiles.py
```

Or run one municipality fully (all three stages) at a time with
`etl/run_all_municipalities.sh [name1 name2 ...]` — useful for getting a
per-municipality progress signal on a long multi-municipality run, since the
three scripts above each process *every* municipality for that one stage
before moving to the next stage.

### Why the halo-then-crop step matters

A tree (or terrain feature) just inside one tile's boundary can still be
within the 30 m viewshed radius of a pixel just inside the *neighbouring*
tile — and the same is true across municipality boundaries, not just tile
boundaries within one municipality. Querying only within an inner extent
(and mosaicking full/unclipped tiles) would leave seam artefacts at every
boundary — a real bug caught during review, see git history. The fix: query
DEM pixels and trees over the buffered extent (each boundary tree/pixel gets
processed by both neighbours, which is intentional), but only ever write the
non-overlapping inner window to disk, so tiles fit together edge-to-edge
with nothing for the final mosaic to get wrong.

### Cross-municipality context

`config.PROVINCE_DEM_VRT` and `config.PROVINCE_TREES_GPKG` (both under
`data/interim/`, gitignored — rebuild locally) extend that same halo-then-crop
approach across municipality boundaries, not just tile boundaries within one
municipality:

- `PROVINCE_DEM_VRT`: a `gdalbuildvrt` mosaic of every municipality's DEM.
  `01_tile_dem.py` reads pixel data from this instead of the individual
  municipality `.tif`, so a tile whose buffer extends past this
  municipality's own raster edge still gets real elevation data from the
  neighbour, rather than a hard edge.
- `PROVINCE_TREES_GPKG`: every municipality's tree GeoPackage merged into
  one. `02_compute_viewsheds.py` queries this instead of a single
  municipality's own tree layer, so a tree owned by the neighbouring
  municipality but within `MAX_DISTANCE` of this side still contributes.

`TILE_BUFFER_PX` (35 m) already exceeds the required 30 m, so no separate
buffer constant is needed for the municipality-boundary case.

### Per-tree height

Each tree's observer height is sampled from the DEM rather than a fixed
constant: `_sample_tree_height()` in `02_compute_viewsheds.py` takes the max
DEM value within `TREE_HEIGHT_BUFFER_RADIUS` (1.5 m) of the tree and uses it
directly as the `ViewshedGenerate` observer height (not adjusted for local
ground elevation). Falls back to `OBSERVER_HEIGHT` if the sample is out of
bounds, non-positive, or exceeds `TREE_HEIGHT_MAX_PLAUSIBLE` (35 m) — the
source raster carries no point classification, so there's no way to tell a
power line, pylon, or building corner apart from a tree canopy in the raw
elevation values; the clamp catches the height-plausibility half of that
(revisit once AHN's classified point cloud, which does distinguish wires
from vegetation, is incorporated instead of the derived raster).

## Data layout

```
data/
  raw/          # local scratch only — the real source lives wherever VIEWANALYSE_DIR points
  interim/
    province_dem.vrt                     # combined DEM mosaic, gitignored — see "Cross-municipality context"
    province_trees.gpkg                  # combined tree layer, gitignored
    dem_tiles/<municipality>/            # generated by stage 1, gitignored
    viewshed_tiles/<municipality>/       # generated by stage 2, gitignored
  processed/
    <municipality>_viewshed.tif          # final merged COG output, gitignored
logs/           # run logs + logs/benchmark.csv, gitignored
```

Merging uses GDAL VRTs (lightweight references to the source tiles) instead
of physically copying data, keeping disk and git usage small — only the
pipeline code and config are tracked.

All tunables (viewshed radius, tile size, worker count, output dtype) live
in `etl/config.py`; machine-specific paths live in `etl/config_local.py`.
`etl/generate_benchmark_report.py` turns `logs/benchmark.csv` (populated
automatically as the pipeline runs) into `BENCHMARKS.md`.

## Known data issues

- **12 of 52 municipality DEMs are effectively empty** (confirmed
  2026-09-07): `Barendrecht`, `Dordrecht`, `Goeree-Overflakkee`, `Gorinchem`,
  `Hardinxveld-Giessendam`, `Hellevoetsluis`, `Hendrik-Ido-Ambacht`,
  `Hoeksche Waard`, `Nissewaard`, `Papendrecht`, `Sliedrecht`, `Zwijndrecht`
  are 0-3.4% real elevation data, the rest exactly zero. A flat/zero DEM
  means the viewshed algorithm treats it as unobstructed terrain — the
  pipeline still runs without errors and produces plausible-looking output
  (effectively "trees within 30m", not real terrain-based visibility), so
  this failure mode is invisible from the output alone. Currently excluded
  via `CORRUPTED_DEM_MUNICIPALITIES` in `config_local.py`; re-run those
  once corrected source DEMs are available.
- Output pixel value `0` means "no tree within 30 m," not "no data" — no
  NoData value is set, intentionally, so GIS tools render it correctly.
- Large municipality DEMs observed to be strip-organized rather than
  internally tiled (e.g. Rotterdam), which means the many small windowed
  reads in stage 1 pull more data off disk than a tiled source would
  require.
- Tree height is sampled from a DSM-like surface raster with no point
  classification (see "Per-tree height" above) — a power line or pylon near
  a tree can't be distinguished from canopy except by the plausibility
  clamp. Revisit if AHN's classified point cloud becomes available.
