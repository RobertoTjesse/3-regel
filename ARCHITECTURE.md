# Architecture — South Holland 3-30-300 viewshed pipeline

Technical reference for how this pipeline actually works: the raster
mechanics, the tiling scheme, the TIFF layout decisions, and what each
script does and why. For setup/usage, see `README.md`. For timing numbers
and current parameters, see `BENCHMARKS.md`.

## 1. The problem being computed

For every 0.5m pixel of a municipality, count how many trees that pixel is
visible from within a 30m radius (line-of-sight, accounting for terrain and
obstructions — not just straight-line distance). This is the **"3"** of the
Dutch "3-30-300" urban greenery rule (3 trees visible from home / 30%
canopy cover / 300m to green space).

The naive approach — for every tree, compute its 30m viewshed and write a
separate raster — doesn't scale: a mid-size municipality has 100,000+
trees, so that's 100,000+ raster files. This pipeline instead **accumulates**:
for each tree, compute its viewshed, and add 1 to every pixel that's
visible from it. The final raster is one file per municipality where each
pixel's value is "number of trees visible from here."

## 2. Source data

One pair of files per municipality, sharing a basename (`Delft.tif` /
`Delft.gpkg`):

- **DEM**: `.tif`, Float32, single band, 0.5m pixels, RD New / EPSG:28992.
  Source is `Geo_raster.TOPOGRAFIE.AHN4_05M_RUW` — **AHN4, 0.5m, "RUW"
  (raw/unfiltered)** — a *surface* model (DSM), not bare-earth. It includes
  buildings and vegetation canopy, not just ground elevation. This matters:
  it's what makes per-tree canopy height sampling meaningful (§6) and what
  makes the viewshed's terrain-blocking realistic (a roofline or hedge
  actually blocks line of sight in this raster, unlike a bare-earth DTM).
- **Trees**: `.gpkg`, point geometries, EPSG:28992, no height attribute.
  Originally shipped as `.shp` — converted once to GeoPackage (§7) because
  a bare shapefile has no spatial index, and per-tile bounding-box queries
  against an unindexed file scale with *total feature count*, not the
  query's result size. That dominated runtime on larger municipalities
  until fixed (measured ~280x slower per query without an index).

Both live wherever `config.VIEWANALYSE_DIR` points (a network share in
practice) — nothing is copied locally; the pipeline reads directly from
there. Some DEMs are tens of gigabytes (Rotterdam ~13GB, Goeree-Overflakkee
~17GB).

## 3. Why tiling is necessary at all

`gdal.ViewshedGenerate()` needs the DEM band open and addressable for
random reads during line-of-sight tracing. Opening a 17GB DEM once is fine;
the problem is that a naive implementation would do that *and* keep the
whole accumulator array in memory for the whole municipality — for
Goeree-Overflakkee's ~35,000 x 19,000 pixel raster, a single UInt32
accumulator is ~2.6GB, and GDAL's internal read-ahead/caching on top of
that gets expensive fast, especially multiplied across `NUM_WORKERS`
parallel processes.

So the DEM is split into **tiles**: 1000x1000 pixel (500m x 500m) chunks,
each processed independently and in parallel. This is also what makes
`NUM_WORKERS`-way parallelism possible — tiles are the unit of work handed
to `ProcessPoolExecutor`.

## 4. The buffer/halo — the single most important mechanic in this pipeline

A tile isn't just its own 1000x1000 pixel window. Every tile is generated
with a **70-pixel (35m) buffer** on all four sides — a "halo" — making the
actual raster on disk 1140x1140 pixels for a full interior tile.

**Why**: `MAX_DISTANCE` (a tree's viewshed radius) is 30m. A tree sitting 2m
inside a tile's boundary can still illuminate pixels up to 30m away — some
of which fall in the *neighbouring* tile. If tiles were queried and
processed strictly within their own 1000x1000 footprint, two things would
go wrong at every tile boundary:

1. A `ViewshedGenerate()` call near the tile edge would run out of DEM to
   test against (terrain blocking can't be evaluated past the raster edge),
   silently truncating that tree's contribution.
2. Trees just across the boundary, which legitimately affect pixels on this
   side, would never be queried at all (if the tree query were restricted
   to "trees inside this tile's own footprint").

The buffer must be `>= MAX_DISTANCE`. It's set to 35m (70px @ 0.5m) — 5m of
headroom over the 30m requirement.

**The pattern, precisely** (implemented once, reused everywhere in this
pipeline — see §8 for the province-wide extension of the same idea):

1. **Read** DEM pixels and query trees over the **buffered** extent (1140x1140).
   A tree just across the boundary from a neighbouring tile *is* included
   here — deliberately. The same tree also gets queried by that neighbouring
   tile. This is not a bug; every tree needs to be evaluated by every tile
   whose halo it falls within, or that tile's edge pixels get an incomplete
   count.
2. **Accumulate** into a 1140x1140 array (one array per tile, in memory,
   per worker process).
3. **Crop** the accumulator down to the **inner** 1000x1000 window before
   writing anything to disk. The halo pixels' accumulated values are
   discarded — they were only ever needed as *context* for computing the
   inner window correctly, never as output in their own right.

Because every tile ever written to disk is exactly its own non-overlapping
inner window, **tiles fit together edge-to-edge with zero overlap**. When
`03_merge_tiles.py` mosaics them (a plain VRT union, no blending logic),
there is no seam to get wrong — every output pixel comes from exactly one
tile's complete, correctly-buffered computation.

*(This exact bug — querying/writing only the inner extent, no halo — was
present early in this project and produced a a visible seam artefact at
every internal tile boundary, undercounting a regular grid covering
roughly 10-12% of every municipality's area. Fixed by adding the halo/crop
split described above. See git history for the full writeup.)*

## 5. What actually happens inside one tile (`02_compute_viewsheds.py`)

For a single tile:

```
for each tree within the tile's BUFFERED extent (from PROVINCE_TREES_GPKG):
    h = sample the tree's height from the DEM (§6)
    result = gdal.ViewshedGenerate(
        srcBand       = this tile's DEM band,
        observerX/Y   = tree coordinates,
        observerHeight= h,
        targetHeight  = 0.0,
        maxDistance   = 30.0,
        dfCurvCoeff   = 0.0,      # flat-earth — irrelevant at 30m radius
        mode          = GVM_Edge,
        visibleVal=1.0, invisibleVal=0.0, outOfRangeVal=0.0,
    )
    accumulator[result's footprint] += result   # see note below
accumulator = accumulator[inner window]          # crop
write accumulator to <tile_id>.tif
```

**Important, non-obvious detail**: `gdal.ViewshedGenerate()` does not
return a raster the size of the input tile. It returns a small window
centred on the observer, sized by `maxDistance` (roughly 121x121 pixels for
a 30m radius at 0.5m resolution) — i.e. just the disc the tree could
possibly affect. The result must be pasted into the tile's full-size
accumulator at the pixel offset implied by comparing the two
geotransforms; treating it as pre-sized to the tile (a bug present early in
this project) causes a numpy broadcast error on every single tree.

**Output tiles have no NoData value.** A pixel value of `0` means "no tree
within 30m" — a real, common, meaningful answer (most of any municipality:
water, non-residential land, anywhere far from vegetation) — not "missing
data." Flagging it as NoData would make GIS tools render large legitimate
areas as blank/transparent, and would silently exclude them from any
statistics computed on the raster.

## 6. Per-tree observer height

Every tree's height is sampled from the DEM itself rather than using a
single constant for all trees:

1. Take a circular window of radius `TREE_HEIGHT_BUFFER_RADIUS` (1.5m)
   around the tree's point.
2. Read the max DEM value in that window.
3. Use that value **directly** as `observerHeight` passed to
   `ViewshedGenerate()` — not adjusted for local ground elevation. (This
   only makes sense because the source is a surface model — the max value
   in a small window around a tree is approximately the canopy top.)
4. **Clamp**: if the sampled value is non-finite, non-positive, or exceeds
   `TREE_HEIGHT_MAX_PLAUSIBLE` (35m), fall back to the flat constant
   `OBSERVER_HEIGHT` (1.7m) instead.

The clamp exists because the source raster carries **no point
classification** — it's a plain elevation grid, so there is no way to tell
a power line, transmission pylon, or building corner apart from a tree
canopy in the raw values alone. AHN's underlying LiDAR point cloud *does*
carry classification codes for exactly this (ASPRS classes 13-16: wire
guard/conductor, transmission tower, wire-structure connector) — but that
information doesn't survive into the derived raster product this pipeline
consumes. The height-plausibility clamp is a pragmatic stopgap; a more
correct fix would derive tree heights from the classified point cloud
directly, filtering out non-vegetation classes before rasterizing.

## 7. Why GeoPackage instead of the original shapefiles

A bare `.shp` has no spatial index. `layer.SetSpatialFilter(bbox)` on an
unindexed shapefile falls back to a near-linear scan of the whole file for
every call. This pipeline calls that once *per tile* — so total tree-query
cost scales with `tile_count x total_features_in_file`, not with how many
trees actually match each query. For a small municipality (few tiles) this
is invisible; for a large one (many tiles, many features) it dominates
total runtime. Measured: ~280x slower per query on an unindexed file vs.
the same data in GeoPackage (which has a built-in R-tree index) — this
tracked almost exactly with the throughput drop observed across
municipalities of increasing size before the fix.

Each municipality's `.shp` was converted once with `ogr2ogr -f GPKG`. One
exception: `'s-Gravenhage` (Den Haag) — `ogr2ogr`'s CLI has a filename
parsing bug specific to a basename starting with an apostrophe, which
silently produced an empty GeoPackage (0 layers) rather than erroring
loudly. Regenerated via `gdal.VectorTranslate()` (the Python API, which
doesn't hit the same CLI argument-parsing path).

## 8. Cross-municipality boundaries — the same halo pattern, one level up

The halo/crop mechanic in §4 solves seams *between tiles within one
municipality*. But a municipality boundary is exactly the same kind of
edge: a tree in the neighbouring municipality, close to the shared border,
can still be within 30m of a pixel on this side.

Rather than teach every script about municipal adjacency, this pipeline
builds two **combined, province-wide sources** once and points the existing
per-tile logic at them instead of at one municipality's own files:

- **`PROVINCE_DEM_VRT`** — a `gdalbuildvrt` mosaic of every municipality's
  DEM `.tif`. `01_tile_dem.py` reads pixel data for every tile's buffered
  window from *this*, not from the individual municipality's own raster.
  Concretely: a tile's buffer window is computed the same way as before
  (§4), but is **no longer clamped** to the owning municipality's own
  raster bounds — if the window extends past that municipality's edge, the
  VRT transparently supplies real pixels from whichever neighbouring
  municipality's file covers that area (a VRT is just a set of source
  references; GDAL resolves the right underlying file per pixel).
- **`PROVINCE_TREES_GPKG`** — every municipality's tree GeoPackage merged
  into one combined layer (~5.2M points total across the province).
  `02_compute_viewsheds.py` queries *this* for every tile's buffered
  extent, so a tree belonging to the neighbouring municipality is included
  exactly like a tree from a neighbouring tile within the same
  municipality (§4) — queried by both sides, output only ever written for
  the inner, non-overlapping window.

`TILE_BUFFER_PX` (35m) already exceeds the 30m requirement, so no separate
buffer constant exists for the municipality case — it's the identical halo,
just now capable of reaching across an administrative boundary instead of
stopping dead at one.

Each municipality's own DEM `.tif` is still used for one thing: defining
*where* to put tiles (its own extent and pixel grid), so total tile
coverage per municipality is unchanged by this. Only the *source of pixel
values* changed.

## 9. TIFF layout — internal tiling vs. strips, and why it matters here

GeoTIFFs can store pixel data two ways:
- **Strip-organized**: each "block" spans the full row width. Reading any
  small window still requires reading complete rows.
- **Internally tiled**: pixel data is stored in fixed-size blocks (e.g.
  512x512), so reading a small window only touches the blocks it overlaps.

Several of the original per-municipality source DEMs are strip-organized
(confirmed on Rotterdam: `Block=91065x1` — one block per scanline, spanning
the full 91,065-pixel width). This pipeline does thousands of small
windowed reads per municipality (one per tile) — against a strip-organized
source, each of those reads pulls a full-width row off disk regardless of
how narrow the actual tile is, a large and unnecessary read-amplification
factor on the bigger municipalities.

Every raster this pipeline *produces* (DEM tiles, per-tile viewshed
results, and the final merged output) is written with `TILED=YES` for
exactly this reason. The 2026 SDE re-export of the 12 corrupted-DEM
municipalities (§11) also standardizes on `BLOCKXSIZE=512 BLOCKYSIZE=512`
via an explicit `gdal_translate` finishing pass, since ArcGIS Pro's export
tools don't reliably expose this level of control.

## 10. The three pipeline stages

| Stage | Script | Input | Output |
|---|---|---|---|
| **Extract** | `01_tile_dem.py` | `PROVINCE_DEM_VRT` + one municipality's own `.tif` (for its extent/grid) | `data/interim/dem_tiles/<name>/tile_RRRR_CCCC.tif` (1140x1140, buffered) + `tile_index.json` (every tile's buffered *and* inner extents, in both map coordinates and pixel offsets) |
| **Transform** | `02_compute_viewsheds.py` | `tile_index.json` + `PROVINCE_TREES_GPKG` | `data/interim/viewshed_tiles/<name>/tile_RRRR_CCCC.tif` (1000x1000, cropped to inner window, UInt32 counts) |
| **Load** | `03_merge_tiles.py` | all of one municipality's viewshed tiles | `data/processed/<name>_viewshed.tif` — one Cloud-Optimized GeoTIFF (COG) |

`tile_index.json` is the hand-off contract between stages 1 and 2 — it
carries everything stage 2 needs to know about a tile's geometry without
re-deriving it: buffered extent (for the tree query), inner extent (for
output cropping), the buffered tile's geotransform, and the DEM's
projection WKT.

**Stage 3 detail**: builds a VRT over all of a municipality's viewshed
tiles (a mosaic — cheap, no data copied), then runs a single
`gdal.Translate(..., format="COG")`. Using GDAL's dedicated COG driver
(rather than a plain GTiff translate followed by a separate
`BuildOverviews()` call, which was the original approach) matters: the COG
driver builds overview pyramids as part of the same write, in the correct
byte layout. Doing it as two separate steps produces a file GDAL itself
flags as having broken COG layout, because the overviews end up appended
after the full-resolution data rather than laid out before it — the file
still works, but loses the point of being a COG (fast partial/range-based
reads over HTTP). Overview resampling uses `AVERAGE` (representative of a
quasi-continuous count field at zoomed-out levels), not `NEAREST` (which
would just pick one sample pixel per block); this only affects the
overview levels, never the full-resolution pixel values.

## 11. Known data-quality issue: 12 corrupted source DEMs (as of 2026-09-07)

12 of 52 municipality source DEMs (`Barendrecht`, `Dordrecht`,
`Goeree-Overflakkee`, `Gorinchem`, `Hardinxveld-Giessendam`,
`Hellevoetsluis`, `Hendrik-Ido-Ambacht`, `Hoeksche Waard`, `Nissewaard`,
`Papendrecht`, `Sliedrecht`, `Zwijndrecht`) were found to be 0-3.4% real
elevation data — the rest exactly `0.0`, with **no NoData flag set**, so
the corruption was invisible from file metadata alone (only direct pixel
inspection revealed it).

**Why this is dangerous, not just "empty"**: a DEM that's uniformly zero
looks to the viewshed algorithm like perfectly flat terrain — nothing ever
blocks a line of sight. The pipeline still runs to completion, reports zero
errors, and produces a plausible-looking, spatially-varying output (since
"trees within 30m" alone still correlates with tree density). There is no
automatic way to distinguish that degenerate case from a real result by
looking at the output raster.

**Root cause** (confirmed): whatever process originally produced these 12
`fme_input` files exported void/gap areas as literal `0.0` rather than a
flagged NoData value — most likely the entire raster was void for these
12, given the near-total zero coverage. The authoritative source
(`Geo_raster.TOPOGRAFIE.AHN4_05M_RUW`, an enterprise SDE raster) has these
areas covered correctly.

**Fix in progress**: `sde_reexport/export_from_sde.py` (an arcpy script,
run inside ArcGIS Pro since this pipeline's own GDAL-based tooling has no
SDE driver access) re-clips the 12 affected extents directly from the SDE
source, explicitly detecting and preserving its real NoData value (not
assuming one). A `gdal_translate` finishing pass then applies this
pipeline's standard TIFF layout (§9: DEFLATE+predictor 3, 512x512 internal
tiles, BigTIFF where needed) before the corrected files replace the empty
ones. `config.CORRUPTED_DEM_MUNICIPALITIES` excludes affected municipalities
from all processing until this is done, regardless of `MUNICIPALITIES`.

## 12. Current parameters at a glance

| Parameter | Value | Meaning |
|---|---|---|
| `MAX_DISTANCE` | 30.0 m | Viewshed radius per tree |
| `OBSERVER_HEIGHT` | 1.7 m | Fallback height when DEM sampling is out of bounds/implausible |
| `TREE_HEIGHT_BUFFER_RADIUS` | 1.5 m | Radius sampled around each tree for its height |
| `TREE_HEIGHT_MAX_PLAUSIBLE` | 35.0 m | Sanity clamp (power line/pylon/building guard) |
| `TARGET_HEIGHT` | 0.0 m | Ground-level target pixels |
| `CURVATURE_COEFF` | 0.0 | Flat-earth (curvature is sub-millimetre at 30m, irrelevant either way) |
| `TILE_PIXELS` | 1000 px (500 m) | Inner tile size |
| `TILE_BUFFER_PX` | 70 px (35 m) | Halo width; must be `>= MAX_DISTANCE / pixel_size` |
| `NUM_WORKERS` | 4 | Parallel tile processing |
| `OUTPUT_DTYPE` | UInt32 | Per-pixel visible-tree count |
| DEM resolution / CRS | 0.5 m / EPSG:28992 | RD New |
| DEM source | AHN4, `_05M_RUW` | Raw/unfiltered surface model (DSM, not bare-earth) |

See `etl/config.py` for the authoritative, always-current values, and
`BENCHMARKS.md` for measured per-municipality timing.
