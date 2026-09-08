"""
export_from_sde.py — Batch-clip municipality extents out of the SDE raster,
to replace files on the network share (originally built for the 12
corrupted-DEM municipalities; reused for any later re-export batch — see
sde_reexport/municipality_extents.csv for whichever set is currently
staged).

Run inside ArcGIS Pro's Python window, or as a standalone script using Pro's
own Python interpreter (the one with arcpy).

WHAT THIS SCRIPT DOES: just the clip + NoData + pixel-type step, which is
what arcpy reliably controls. It deliberately does NOT try to control TIFF
compression/internal tiling/BigTIFF — arcpy's CopyRaster doesn't expose that
GDAL-level detail reliably, and getting it wrong is exactly how the current
12 files ended up strip-organized / badly compressed. Hand the raw output of
this script back and the finishing pass (COMPRESS=DEFLATE, PREDICTOR=3,
TILED=YES, BLOCKXSIZE/YSIZE=512, BIGTIFF=IF_SAFER) will be done with
gdal_translate — same tool this project's pipeline already uses everywhere
else, and the one place we have precise control over the file's structure.

Before running, edit the three values marked "<-- fill in" below.
"""

import arcpy
import csv
import os

# ---------------------------------------------------------------------------
# EDIT THESE
# ---------------------------------------------------------------------------
SDE_CONNECTION = r"R:\ESRI\BEHEER\Database_verbindingen\Geodatabase\Productie\Geo_raster\Geodatabase@Geo_raster@topografie.sde"
SDE_RASTER_DATASET = "Geo_raster.TOPOGRAFIE.AHN4_05M_RUW"  # AHN4, 0.5m, raw/unfiltered surface model

# Where the raw clipped outputs go. Recommend a fresh local staging folder,
# NOT directly onto R:\ — inspect them before overwriting anything shared.
OUTPUT_DIR = r"D:\Repositories\3-regel\sde_reexport\raw_clips"
# ---------------------------------------------------------------------------

EXTENTS_CSV = r"D:\Repositories\3-regel\sde_reexport\municipality_extents.csv"
NODATA_FALLBACK = -9999

os.makedirs(OUTPUT_DIR, exist_ok=True)
arcpy.env.overwriteOutput = True

src_raster = os.path.join(SDE_CONNECTION, SDE_RASTER_DATASET)

# Use the source's own NoData value if it has one; only fall back to our own
# sentinel if it genuinely doesn't. This is the step that likely went wrong
# in whatever produced the current 12 empty files — a real void/gap area
# exported as literal 0.0 instead of a flagged NoData value.
desc = arcpy.Describe(src_raster)
source_nodata = getattr(desc, "noDataValue", None)
nodata_value = source_nodata if source_nodata not in (None, "") else NODATA_FALLBACK
print(f"Source NoData: {source_nodata!r} -> using {nodata_value} for exports")

with open(EXTENTS_CSV, newline="") as fh:
    rows = list(csv.DictReader(fh))

print(f"Exporting {len(rows)} municipality extents from {src_raster}\n")

for row in rows:
    name = row["name"]
    xmin, ymin, xmax, ymax = row["xmin"], row["ymin"], row["xmax"], row["ymax"]
    final_path = os.path.join(OUTPUT_DIR, f"{name}.tif")

    if os.path.exists(final_path):
        print(f"[{name}] already exists, skipping")
        continue

    # ArcGIS's raster-dataset name validation rejects spaces (and some other
    # characters) in the output name even when writing a plain .tif — e.g.
    # "Hoeksche Waard" fails with ERROR 000354. Clip under a safe temp name,
    # then rename to the real name (a filesystem rename has no such
    # restriction) once the raster tools are done with it.
    safe_name = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name)
    work_path = os.path.join(OUTPUT_DIR, f"{safe_name}.tif")

    print(f"[{name}] extent=({xmin}, {ymin}, {xmax}, {ymax}) -> {final_path}")

    arcpy.management.Clip(
        in_raster=src_raster,
        rectangle=f"{xmin} {ymin} {xmax} {ymax}",
        out_raster=work_path,
        nodata_value=nodata_value,
        clipping_geometry="NONE",
        maintain_clipping_extent="NO_MAINTAIN_EXTENT",
    )

    # Confirm pixel type survived the clip as 32-bit float; CopyRaster with
    # an explicit pixel_type is the reliable way to force it if not.
    result_desc = arcpy.Describe(work_path)
    if result_desc.pixelType != "F32":
        print(f"  WARNING: {name} came out as {result_desc.pixelType}, forcing 32-bit float")
        tmp_path = work_path + ".tmp.tif"
        arcpy.management.CopyRaster(work_path, tmp_path, pixel_type="32_BIT_FLOAT", nodata_value=nodata_value)
        arcpy.management.Delete(work_path)
        arcpy.management.Rename(tmp_path, work_path)

    if safe_name != name:
        arcpy.management.Rename(work_path, final_path)

print("\nDone. Hand the contents of", OUTPUT_DIR, "back for the gdal_translate finishing pass.")
