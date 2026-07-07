"""
Canopy Cover service (Stage 3).

Estimates the percentage of the field with closed crop canopy vs. bare soil/gaps
by thresholding a vegetation index over the boundary.
"""
import numpy as np

from agri.analysis.base import best_health_index, clip_index_to_raster, read_valid


def compute_canopy_cover(task, boundary, output_path, threshold=None):
    """
    :return: (raster_path, stats) where stats includes canopy_pct.
    """
    formula = best_health_index(task)  # NDVI (NIR) or ExG (RGB)
    clip_index_to_raster(task, boundary, formula, output_path)
    valid = read_valid(output_path)

    if threshold is None:
        # NDVI vegetation threshold ~0.25; ExG > 0 indicates green vegetation
        threshold = 0.25 if formula.startswith('ND') else 0.0

    if valid.size == 0:
        return output_path, {'index': formula, 'threshold': threshold,
                             'canopy_pct': None, 'vegetation_pixels': 0, 'total_pixels': 0}

    veg = int(np.count_nonzero(valid > threshold))
    total = int(valid.size)
    return output_path, {
        'index': formula,
        'threshold': threshold,
        'canopy_pct': round(100.0 * veg / total, 2),
        'vegetation_pixels': veg,
        'total_pixels': total,
    }
