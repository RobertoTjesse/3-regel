"""
config_local.example.py — Template for machine-specific settings.

Copy this file to config_local.py (same folder) and fill in real paths for
your machine. config_local.py is gitignored — it will never be committed,
so it's safe to put real network paths and local install locations in it.
"""

from pathlib import Path

# Root of your QGIS / OSGeo4W install.
OSGEO4W_ROOT = r"C:\Users\<you>\AppData\Local\Programs\OSGeo4W"

# Folder holding one DEM (.tif) + tree-position (.shp) pair per municipality.
VIEWANALYSE_DIR = Path(r"R:\path\to\viewanalyse")

# Municipalities to process. Empty list = process everything found above.
MUNICIPALITIES = ["Papendrecht"]
