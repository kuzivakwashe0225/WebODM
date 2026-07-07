"""
RGB Index service (Stage 3).

Computes the RGB-only vegetation indices (ExG, VARI, GLI) over the field — proxies
for plant vigour when NIR is unavailable. Writes one representative raster (ExG)
and returns per-index zonal stats.
"""
import os

from agri.analysis.base import clip_index_to_raster, read_valid, basic_stats

RGB_INDICES = ['EXG', 'VARI', 'GLI']


def compute_rgb_index(task, boundary, output_dir):
    """
    :return: (primary_raster_path_or_None, stats) where stats is
             {'EXG': {...}, 'VARI': {...}, 'GLI': {...}}
    """
    stats = {}
    primary_raster = None
    for idx in RGB_INDICES:
        out = os.path.join(output_dir, "%s.tif" % idx.lower())
        try:
            clip_index_to_raster(task, boundary, idx, out)
            stats[idx] = basic_stats(read_valid(out))
            if primary_raster is None:
                primary_raster = out
        except Exception as e:
            stats[idx] = {'error': str(e)}
    return primary_raster, stats
