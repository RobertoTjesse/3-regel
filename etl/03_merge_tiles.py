"""
03_merge_tiles.py — Mosaic all per-tile viewshed rasters into one output, per municipality.

Strategy
--------
1. Build a VRT (virtual raster) over all tile TIFs — zero extra disk space,
   since a VRT only stores references ("links") to the source tiles.
2. Translate the VRT to a compressed, tiled, cloud-optimised GeoTIFF.

The final raster contains, for every pixel, the number of trees from which
that pixel was visible within the 30-metre viewshed radius.

Usage:
    python etl/03_merge_tiles.py [--no-overviews]

Processes every municipality in config.MUNICIPALITIES (or all pairs found in
VIEWANALYSE_DIR if that list is empty), writing data/processed/<name>_viewshed.tif.
"""

import sys
import logging
import argparse
import time
from pathlib import Path

import config

try:
    from osgeo import gdal
except ImportError as exc:
    sys.exit(f"ERROR: cannot import osgeo.gdal — {exc}")

gdal.UseExceptions()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def build_vrt(tile_paths: list[str], vrt_path: str) -> None:
    log.info(f"Building VRT from {len(tile_paths)} tiles …")
    vrt_opts = gdal.BuildVRTOptions(
        resolution="highest",
        resampleAlg="nearest",
        addAlpha=False,
    )
    vrt_ds = gdal.BuildVRT(vrt_path, tile_paths, options=vrt_opts)
    if vrt_ds is None:
        sys.exit("ERROR: gdal.BuildVRT returned None — check that input tiles exist.")
    vrt_ds.FlushCache()
    vrt_ds = None
    log.info(f"VRT saved: {vrt_path}")


def translate_to_cog(vrt_path: str, out_path: str, build_ovr: bool) -> None:
    """
    Translate VRT -> a real Cloud-Optimized GeoTIFF using GDAL's COG driver.

    The COG driver builds overviews internally as part of this single write,
    which is what actually gives COG-compliant byte layout. Building
    overviews afterwards on a plain GTiff (the old approach) produces a
    working file, but GDAL flags the layout as broken because the overviews
    end up appended at the end rather than laid out before the full-res data.
    """
    log.info(f"Translating to COG: {out_path}")

    translate_opts = gdal.TranslateOptions(
        format="COG",
        outputType=gdal.GDT_UInt32,
        creationOptions=[
            "COMPRESS=LZW",
            "PREDICTOR=STANDARD",
            "BLOCKSIZE=512",
            "BIGTIFF=YES",
            "OVERVIEWS=" + ("AUTO" if build_ovr else "NONE"),
            # Full-res pixel values are untouched by this; only affects how
            # overview (zoomed-out) levels are downsampled. AVERAGE gives a
            # representative view of this quasi-continuous count field —
            # NEAREST would just pick one pixel per block, looking patchy.
            "RESAMPLING=AVERAGE",
        ],
        callback=gdal.TermProgress_nocb,
    )

    ds = gdal.Translate(out_path, vrt_path, options=translate_opts)
    if ds is None:
        sys.exit("ERROR: gdal.Translate failed.")
    ds.FlushCache()
    ds = None
    log.info("Translation complete.")


def merge_municipality(name: str, build_ovr: bool) -> bool:
    """Returns True if the municipality was actually merged, False if skipped."""
    viewshed_dir = config.viewshed_tiles_dir(name)
    tile_files = sorted(viewshed_dir.glob("tile_*.tif"))
    if not tile_files:
        log.error(
            f"[{name}] no tile_*.tif files found in {viewshed_dir} — "
            "run 02_compute_viewsheds.py first. Skipping."
        )
        return False

    log.info(f"[{name}] Found {len(tile_files)} output tiles in {viewshed_dir}")

    config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    vrt_path = str(config.PROCESSED_DIR / f"{name}_mosaic.vrt")
    out_path = str(config.final_output_path(name))

    build_vrt([str(p) for p in tile_files], vrt_path)
    translate_to_cog(vrt_path, out_path, build_ovr)

    ds = gdal.Open(out_path)
    if ds:
        band = ds.GetRasterBand(1)
        stats = band.ComputeStatistics(False)
        log.info(
            f"[{name}] Result stats — min: {stats[0]:.0f}  max: {stats[1]:.0f}  "
            f"mean: {stats[2]:.2f}  stddev: {stats[3]:.2f}"
        )
        ds = None

    log.info(f"[{name}] Final output: {out_path}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Merge per-tile viewshed rasters per municipality.")
    parser.add_argument(
        "--no-overviews", action="store_true",
        help="Skip building overview pyramids",
    )
    args = parser.parse_args()

    pairs = list(config.municipality_pairs())
    if not pairs:
        sys.exit(
            f"ERROR: no tif+shp pairs found in {config.VIEWANALYSE_DIR}\n"
            "Check config.VIEWANALYSE_DIR and config.MUNICIPALITIES."
        )

    log.info(f"Municipalities to merge: {[name for name, _, _ in pairs]}")

    for name, _dem_path, _trees_path in pairs:
        log.info(f"=== {name} ===")
        t0 = time.perf_counter()
        merged = merge_municipality(name, build_ovr=not args.no_overviews)
        if merged:
            config.log_benchmark(name, "merge_tiles", time.perf_counter() - t0)


if __name__ == "__main__":
    main()
