"""
Shared helpers for the analysis services (Stage 3).

All services clip the capture's orthophoto to the (approved) boundary and evaluate
a vegetation-index expression via WebODM's own band-math + raster export. These
helpers centralise that so each service stays small.
"""
import os

import numpy as np
import rasterio

from app.api.formulas import get_auto_bands, lookup_formula
from app.models import Task
from app.raster_utils import export_raster


def has_nir(task):
    descriptions = [(b.get('description') or '').lower() for b in (task.orthophoto_bands or [])]
    return any(d in ('nir', 'n') for d in descriptions)


def best_health_index(task):
    """NDVI when NIR is available, else ExG."""
    return 'NDVI' if has_nir(task) else 'EXG'


def index_expression(task, formula):
    """Resolve (numexpr expression, band_order) for a formula against this ortho's bands."""
    bands_meta = task.orthophoto_bands or []
    try:
        band_order, _matched = get_auto_bands(bands_meta, formula)
    except Exception:
        # RGB-only indices (ExG/VARI/GLI) fall back here; NIR indices resolve above.
        band_order = 'RGB'
    expr, _hrange = lookup_formula(formula, band_order)
    return expr, band_order


def clip_index_to_raster(task, boundary, formula, output_path):
    """Clip ortho to boundary + evaluate `formula` into a single-band float32 GeoTIFF."""
    expr, _band_order = index_expression(task, formula)
    ortho = task.assets_path(Task.ASSETS_MAP["orthophoto.tif"])
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    export_raster(ortho, output_path, expression=expr, crop=boundary.geom.wkt, format='gtiff')
    return output_path


def read_valid(raster_path, band=1):
    """Return finite, non-nodata pixel values of a raster band as a 1-D array."""
    with rasterio.open(raster_path) as ds:
        arr = ds.read(band, masked=True)
    valid = arr.compressed()
    return valid[np.isfinite(valid)]


def basic_stats(valid, extra=None):
    s = {
        'mean': float(np.mean(valid)) if valid.size else None,
        'min': float(np.min(valid)) if valid.size else None,
        'max': float(np.max(valid)) if valid.size else None,
        'std': float(np.std(valid)) if valid.size else None,
        'count': int(valid.size),
    }
    if extra:
        s.update(extra)
    return s
