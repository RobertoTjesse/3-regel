# Improvements over the original prototype

What changed between the original QGIS/PyQGIS prototype (`bomen_extract.fmw`
/ `QGIS/Script.py` on the network share) and this pipeline, and why each
change mattered. For how the pipeline works today, see `ARCHITECTURE.md`.
For current timing numbers, see `BENCHMARKS.md`.

The original script's approach: for every tree, call QGIS's `gdal:viewshed`
Processing algorithm against the **entire municipality DEM**, and write the
result as a **separate GeoTIFF file**. Reported real-world cost: roughly 60
hours to process Delft (~114,000 trees) via the ArcPy equivalent of the same
approach. This pipeline processes the same municipality in about 90 seconds
end-to-end — a difference explained entirely by architecture, not a smarter
algorithm (it's the same `ViewshedGenerate` underneath).

## Performance-critical changes

1. **Accumulate instead of write-per-tree.** The original wrote one output
   raster per tree — hundreds of thousands of files for a whole city, most
   of the cost being file creation/write overhead, not the actual viewshed
   math. This pipeline accumulates every tree's contribution into an
   in-memory array and writes to disk once per *tile* (thousands of trees
   at once).

2. **A small tile window instead of the full DEM, per viewshed call.** The
   original ran the viewshed tool against the entire municipality raster
   for every single tree, regardless of city size — for Delft, a ~700MB
   file opened and processed ~114,000 times. This pipeline pre-splits the
   DEM into ~570m tiles (500m inner + 35m buffer), so every
   `ViewshedGenerate()` call only ever touches a small window, independent
   of municipality size.

3. **GeoPackage + spatial index instead of bare shapefiles.** The original
   tree layers were plain `.shp` files with no spatial index — a bounding-box
   query against an unindexed shapefile scans close to the entire file.
   This pipeline queries per-tile, so unindexed cost scaled with
   `tile_count x total_features`, not the size of each query's actual
   result. Measured: ~280x slower per query without an index vs. the same
   data in a GeoPackage (which has a built-in R-tree). This was the single
   largest fix for runtime on the bigger municipalities — throughput
   dropped disproportionately as tile count grew until this was fixed.

4. **Direct in-process GDAL calls instead of a framework/tool invocation
   per tree.** QGIS's Processing framework (and, per the 60-hour ArcPy
   figure, ArcPy's tool invocation) carries meaningful per-call overhead —
   validation, environment setup, sometimes a subprocess spawn — charged on
   every single tree. This pipeline calls `gdal.ViewshedGenerate()` as a
   plain C-API function inside an already-running Python process, in a
   tight loop.

5. **Parallelism.** See its own section below — the original had none.

## Parallelism — exactly what is and isn't parallel

It's worth being precise here, since the parallelism is narrower in scope
than it might sound:

**Parallelized**: `02_compute_viewsheds.py` (the dominant cost) processes
one municipality's tiles concurrently via
`ProcessPoolExecutor(max_workers=NUM_WORKERS)` (`NUM_WORKERS=4`). One tile
= one unit of work handed to one worker **process** — deliberately
processes, not threads, since viewshed computation is CPU-bound and a
process sidesteps Python's GIL entirely rather than depending on GDAL
happening to release it internally.

**Not parallelized**:
- **Within a tile**: trees are processed one at a time in a plain Python
  loop calling `ViewshedGenerate()` sequentially. The unit of parallelism
  is the tile, not the tree.
- **`01_tile_dem.py`**: writes tiles one at a time via `gdal.Translate()`.
- **`03_merge_tiles.py`**: one VRT build + one `gdal.Translate()` call,
  sequential.
- **Across municipalities**: `run_all_municipalities.sh` processes
  municipalities strictly one at a time. This was a deliberate choice made
  partway through this project, once per-municipality (rather than
  per-*stage*) progress visibility was needed — running all 52
  municipalities through stage 1, then all through stage 2, then all
  through stage 3 (the original approach) is also not inter-municipality
  parallel, just organized by stage instead of by municipality.

**Not explored**: whether 4 workers is actually optimal for a given
machine, or whether a higher worker count helps further. `NUM_WORKERS=4`
is a config default, not a benchmarked/tuned value.

## Correctness fixes required by tiling

Splitting work into tiles (the performance fix in §2 above) introduces
seam problems that a naive, non-tiled implementation like the original
script never had to solve. These were found and fixed during this project:

1. **The halo-then-crop pattern.** A tree near a tile's edge can still
   affect pixels in the *neighbouring* tile within `MAX_DISTANCE`.
   Querying trees and writing output within a tile's own footprint only
   left a visible seam artefact at every internal tile boundary,
   undercounting a regular grid covering roughly 10-12% of every
   municipality's area. Fixed by querying over a buffered "halo" extent
   (trees near the edge are legitimately processed by both neighbouring
   tiles) but only ever writing the non-overlapping *inner* window to disk.

2. **Viewshed result placement.** `gdal.ViewshedGenerate()` returns a small
   window centred on the observer (sized by `MAX_DISTANCE`), not a raster
   the size of the input tile. Code that assumed otherwise caused a numpy
   broadcast error on every tree, silently dropping all tree contributions
   for a period before being caught. Fixed by pasting the small result
   into the tile's accumulator at the pixel offset implied by comparing
   the two geotransforms.

3. **NoData=0 conflation.** Early output flagged pixel value `0` as
   NoData. But "zero trees within 30m" is a real, common, meaningful
   answer (most of any municipality) — flagging it as missing data broke
   `ComputeStatistics()` and would make GIS tools render large legitimate
   areas as blank. Fixed by not setting a NoData value on output at all.

4. **CRS validation.** Added an explicit check comparing the DEM's and
   tree layer's CRS before processing a municipality. Without it, a
   missing/mismatched `.prj` makes OGR's spatial filter silently match
   zero features — producing an all-zero municipality output
   indistinguishable from "genuinely no trees here," which would go
   unnoticed in an unattended multi-municipality run.

## Extending the halo-crop pattern across municipality boundaries

The same seam problem in §1 above applies at municipality boundaries, not
just tile boundaries within one municipality. Rather than teach every
script about municipal adjacency, this pipeline builds two combined,
province-wide sources once (`PROVINCE_DEM_VRT`, `PROVINCE_TREES_GPKG` — VRT
mosaics/merges, not physical data duplication) and points the existing
per-tile logic at them instead of at one municipality's own files. A tile
near a municipality's edge now reads real neighbour context instead of
hitting a hard clamp. See `ARCHITECTURE.md` §8 for the mechanics.

## Other improvements

- **COG driver instead of GTiff + separate `BuildOverviews()`.** The
  two-step approach produced a file GDAL itself flags as having broken
  Cloud-Optimized-GeoTIFF layout (overviews appended after the full-res
  data instead of before it). Using GDAL's dedicated `COG` format target
  builds overviews as part of the same write, correctly laid out.
- **Internally tiled TIFF output (512x512 blocks) instead of strip
  organization.** Several original source DEMs are strip-organized (one
  block per full-width scanline), which massively amplifies the cost of
  the many small windowed reads this pipeline does per municipality.
  Every raster this pipeline produces is written `TILED=YES`; the
  corrected SDE re-exports (see below) apply the same standard.
- **Variable per-tree height sampled from the DEM** (the max value within
  a 1.5m radius, approximating canopy top on the source surface model)
  instead of one flat constant for every tree, with a plausibility clamp
  (35m) guarding against the raster's lack of point classification (no
  way to distinguish a power line or pylon from a tree canopy in raw
  elevation values alone).

## Not a code change, but the highest-stakes catch of the project

**12 of 52 municipality source DEMs were found to be 0-3.4% real elevation
data — the rest exactly `0.0`, with no NoData flag set.** A flat/zero DEM
looks to the viewshed algorithm like perfectly unobstructed terrain, so the
pipeline ran to completion, reported zero errors, and produced
plausible-looking output for all 12 — this failure mode was invisible from
the output alone; only direct pixel inspection revealed it. Root cause:
whatever process produced these particular `fme_input` files exported
void/gap areas as literal `0.0` rather than a flagged NoData value. Fixed
by re-exporting the affected extents from the authoritative source
(`Geo_raster.TOPOGRAFIE.AHN4_05M_RUW`, an SDE raster) — see
`ARCHITECTURE.md` §11 and `sde_reexport/` for the full process. This is the
kind of error that no amount of pipeline-code correctness would have
caught on its own; it required deliberately checking the *input* data,
not just verifying the pipeline ran without errors.
