"""
add_tree_heights.py — Sample each tree's height from the DEM and write it
out as a NEW GeoPackage (does not modify the source tree layer).

Reuses the exact same sampling logic as 02_compute_viewsheds.py
(_sample_tree_height: max DEM value within TREE_HEIGHT_BUFFER_RADIUS,
clamped to TREE_HEIGHT_MAX_PLAUSIBLE, falling back to OBSERVER_HEIGHT) so
the heights shown here match what the live viewshed computation actually
used for that tree.

Usage:
    python etl/add_tree_heights.py <municipality_name>

Output:
    data/processed/<municipality_name>_tree_heights.gpkg
    Point layer, same geometry as the source trees, with columns:
      sampled_height_m  — the value _sample_tree_height() returned
      was_clamped        — True if the raw sample exceeded
                            TREE_HEIGHT_MAX_PLAUSIBLE and got replaced
                            with OBSERVER_HEIGHT
"""

import sys
import importlib
from pathlib import Path

import config

try:
    from osgeo import gdal, ogr
except ImportError as exc:
    sys.exit(f"ERROR: cannot import osgeo — {exc}")

gdal.UseExceptions()
ogr.UseExceptions()

# Reuse the real sampling function from 02_compute_viewsheds.py rather than
# re-implementing it, so this always matches what the pipeline actually did.
_viewsheds_mod = importlib.import_module("02_compute_viewsheds")
_sample_tree_height = _viewsheds_mod._sample_tree_height


def add_heights_for_municipality(name: str) -> None:
    pairs = {n: (dem, trees) for n, dem, trees in config.municipality_pairs()}
    if name not in pairs:
        sys.exit(f"ERROR: '{name}' not found (check spelling / config.CORRUPTED_DEM_MUNICIPALITIES)")
    dem_path, trees_path = pairs[name]

    print(f"[{name}] DEM: {dem_path}")
    print(f"[{name}] Trees: {trees_path}")

    dem_ds = gdal.Open(str(dem_path))
    dem_band = dem_ds.GetRasterBand(1)
    gt = dem_ds.GetGeoTransform()
    nx, ny = dem_ds.RasterXSize, dem_ds.RasterYSize

    src_ds = ogr.Open(str(trees_path), 0)
    src_layer = src_ds.GetLayer(0)
    srs = src_layer.GetSpatialRef()

    out_path = config.PROCESSED_DIR / f"{name}_tree_heights.gpkg"
    config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    driver = ogr.GetDriverByName("GPKG")
    out_ds = driver.CreateDataSource(str(out_path))
    out_layer = out_ds.CreateLayer(f"{name}_tree_heights", srs, ogr.wkbPoint)
    out_layer.CreateField(ogr.FieldDefn("sampled_height_m", ogr.OFTReal))
    was_clamped_field = ogr.FieldDefn("was_clamped", ogr.OFTInteger)
    was_clamped_field.SetSubType(ogr.OFSTBoolean)
    out_layer.CreateField(was_clamped_field)
    out_defn = out_layer.GetLayerDefn()

    n_trees = 0
    n_clamped = 0
    for feat in src_layer:
        geom = feat.GetGeometryRef()
        if geom is None:
            continue
        geom_type = geom.GetGeometryType()
        pt = geom.Centroid() if geom_type not in (ogr.wkbPoint, ogr.wkbPoint25D) else geom
        x, y = pt.GetX(), pt.GetY()

        h = _sample_tree_height(dem_band, gt, nx, ny, x, y)

        # Recompute the raw (unclamped) sample once more, cheaply, just to
        # know whether THIS tree got clamped — _sample_tree_height only
        # returns the final value, not whether a clamp fired.
        raw = _raw_max_sample(dem_band, gt, nx, ny, x, y)
        clamped = raw is not None and raw > config.TREE_HEIGHT_MAX_PLAUSIBLE
        if clamped:
            n_clamped += 1

        out_feat = ogr.Feature(out_defn)
        out_feat.SetField("sampled_height_m", h)
        out_feat.SetField("was_clamped", 1 if clamped else 0)
        out_feat.SetGeometry(ogr.Geometry(ogr.wkbPoint))
        out_feat.GetGeometryRef().AddPoint(x, y)
        out_layer.CreateFeature(out_feat)
        out_feat = None

        n_trees += 1
        if n_trees % config.LOG_EVERY == 0:
            print(f"  ...{n_trees} trees processed", flush=True)

    out_ds = None
    src_ds = None
    dem_ds = None

    print(f"[{name}] Done. {n_trees} trees, {n_clamped} clamped ({100*n_clamped/n_trees:.1f}%)")
    print(f"[{name}] Written to {out_path}")


def _raw_max_sample(dem_band, gt, nx, ny, x, y):
    """Same window/mask logic as _sample_tree_height but returns the raw
    max (pre-clamp, pre-fallback), or None if no valid sample exists."""
    import numpy as np
    px = abs(gt[1])
    py = abs(gt[5])
    col = (x - gt[0]) / gt[1]
    row = (y - gt[3]) / gt[5]
    rad_px_x = max(1, int(round(config.TREE_HEIGHT_BUFFER_RADIUS / px)))
    rad_px_y = max(1, int(round(config.TREE_HEIGHT_BUFFER_RADIUS / py)))
    c0 = max(0, int(round(col)) - rad_px_x)
    r0 = max(0, int(round(row)) - rad_px_y)
    c1 = min(nx, int(round(col)) + rad_px_x + 1)
    r1 = min(ny, int(round(row)) + rad_px_y + 1)
    if c1 <= c0 or r1 <= r0:
        return None
    window = dem_band.ReadAsArray(c0, r0, c1 - c0, r1 - r0)
    if window is None or window.size == 0:
        return None
    rows_idx, cols_idx = np.indices(window.shape)
    dist = np.sqrt(((rows_idx + r0 - row) * py) ** 2 + ((cols_idx + c0 - col) * px) ** 2)
    mask = dist <= config.TREE_HEIGHT_BUFFER_RADIUS
    if not np.any(mask):
        return None
    max_val = float(np.max(window[mask]))
    return max_val if np.isfinite(max_val) else None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python etl/add_tree_heights.py <municipality_name>")
    add_heights_for_municipality(sys.argv[1])
