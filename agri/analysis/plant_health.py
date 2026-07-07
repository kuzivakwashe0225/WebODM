"""
Plant Health analysis (Stage 2).

Clips a capture's orthophoto to an APPROVED boundary and computes a plant-health
vegetation index — true NDVI when a NIR band is present, otherwise the best
available RGB index (ExG). Reuses WebODM's own band-math (app/api/formulas.py)
and raster export (app/raster_utils.export_raster, which clips via gdalwarp
-cutline and evaluates the numexpr expression into a single-band float32 GeoTIFF).
"""
import os

import numpy as np
import rasterio

from app.api.formulas import get_auto_bands, lookup_formula
from app.models import Task
from app.raster_utils import export_raster


def select_formula_and_bands(task):
    """
    Choose the index for this capture: NDVI if a NIR band exists, else ExG.
    Returns (formula_name, band_order).
    """
    bands_meta = task.orthophoto_bands or []
    descriptions = [(b.get('description') or '').lower() for b in bands_meta]
    has_nir = any(d in ('nir', 'n') for d in descriptions)

    formula = 'NDVI' if has_nir else 'EXG'
    try:
        band_order, _matched = get_auto_bands(bands_meta, formula)
    except Exception:
        # Fallback when band descriptions are missing/ambiguous
        band_order = 'RGBN' if has_nir else 'RGB'
    return formula, band_order


def compute_plant_health(task, boundary, output_path):
    """
    :param task: the capture (imported-orthophoto Task)
    :param boundary: an agri.Boundary (should be APPROVED before calling)
    :param output_path: absolute path for the result GeoTIFF
    :return: (formula_name, stats_dict)
    """
    formula, band_order = select_formula_and_bands(task)
    expr, _hrange = lookup_formula(formula, band_order)

    ortho = task.assets_path(Task.ASSETS_MAP["orthophoto.tif"])
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Clip to boundary (WKT is 4326; GDAL reprojects the cutline to the ortho CRS)
    # and evaluate the index into a single-band float32 GeoTIFF (nodata = -9999).
    export_raster(ortho, output_path, expression=expr, crop=boundary.geom.wkt, format='gtiff')

    with rasterio.open(output_path) as ds:
        arr = ds.read(1, masked=True)
    valid = arr.compressed()
    valid = valid[np.isfinite(valid)]

    stats = {
        'index': formula,
        'mean': float(np.mean(valid)) if valid.size else None,
        'min': float(np.min(valid)) if valid.size else None,
        'max': float(np.max(valid)) if valid.size else None,
        'std': float(np.std(valid)) if valid.size else None,
        'count': int(valid.size),
    }
    return formula, stats
