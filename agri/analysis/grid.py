"""
Grid Analysis service (Stage 3).

Divides the field into a grid of ~cell_size_m cells and reports the mean
vegetation index per cell as a GeoJSON FeatureCollection (reprojected to WGS84) —
turning a field-wide raster into actionable zone-level data.
"""
import json
import os

import numpy as np
import rasterio
from rasterio.warp import transform_geom

from agri.analysis.base import best_health_index, clip_index_to_raster


def compute_grid_analysis(task, boundary, output_path, cell_size_m=10.0):
    formula = best_health_index(task)
    index_tif = os.path.join(os.path.dirname(output_path), "grid_index.tif")
    clip_index_to_raster(task, boundary, formula, index_tif)

    with rasterio.open(index_tif) as ds:
        arr = ds.read(1, masked=True)
        transform = ds.transform
        crs = ds.crs

    px = abs(transform.a) or 1.0
    cell_px = max(1, int(round(cell_size_m / px)))

    H, W = arr.shape
    features = []
    for i0 in range(0, H, cell_px):
        for j0 in range(0, W, cell_px):
            block = arr[i0:i0 + cell_px, j0:j0 + cell_px]
            valid = np.asarray(block.compressed(), dtype=float)
            valid = valid[np.isfinite(valid)]
            if valid.size == 0:
                continue

            i1, j1 = min(i0 + cell_px, H), min(j0 + cell_px, W)
            x_left, y_top = transform * (j0, i0)
            x_right, y_bottom = transform * (j1, i1)
            poly = {"type": "Polygon", "coordinates": [[
                [x_left, y_top], [x_right, y_top],
                [x_right, y_bottom], [x_left, y_bottom], [x_left, y_top]]]}
            try:
                geom = transform_geom(crs, "EPSG:4326", poly)
            except Exception:
                geom = poly

            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {"mean_index": round(float(np.mean(valid)), 4),
                               "row": i0 // cell_px, "col": j0 // cell_px},
            })

    geojson = {"type": "FeatureCollection", "features": features,
               "properties": {"cell_size_m": cell_size_m, "index": formula}}
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(geojson, f)

    return output_path, {"cell_count": len(features), "cell_size_m": cell_size_m, "index": formula}
