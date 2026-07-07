"""
Builds a Leaflet-ready XYZ tile URL that renders a run's plant-health heatmap by
reusing WebODM's existing tiler -- NO new raster serving. The tiler already
evaluates a vegetation-index formula over the orthophoto, applies a colormap, and
clips to a GeoJSON cutline (app/api/tiler.py). We just point a tile layer at it.

Rescale is taken from the plant_health result's own min/max stats (not a
hardcoded range) so the coloring is consistent with the mean value shown in the
panel and works for any index (NDVI's -1..1 as well as raw ExG magnitudes).
"""
import json
from urllib.parse import urlencode

from agri.analysis.plant_health import select_formula_and_bands
from agri.geo import geom_to_feature
from agri.models import AnalysisResult

# Red (low/stressed) -> yellow -> green (healthy). Valid tiler colormap.
HEATMAP_COLORMAP = 'rdylgn'


def plant_health_tile_url(run):
    """
    :return: an XYZ tile URL template ("/.../tiles/{z}/{x}/{y}.png?...") for the
             run's plant-health heatmap, clipped to its boundary, or None if the
             run has no usable plant_health result yet.
    """
    ph = run.results.filter(kind=AnalysisResult.PLANT_HEALTH).first()
    if not ph or not ph.stats:
        return None

    lo, hi = ph.stats.get('min'), ph.stats.get('max')
    if lo is None or hi is None or lo == hi:
        return None

    task = run.task
    formula, band_order = select_formula_and_bands(task)

    # GeoJSON Feature (not bare geometry) -- rio_tiler's create_cutline expects a
    # feature. Built via geom_to_feature (NOT geom.geojson, which swaps axes under
    # GDAL 3 -- see agri/geo.py) so the cutline lands where the field actually is.
    feature = geom_to_feature(run.boundary.geom)

    params = urlencode({
        'formula': formula,
        'bands': band_order,
        'color_map': HEATMAP_COLORMAP,
        'rescale': "%s,%s" % (lo, hi),
        # urlencoding hides the JSON's { } from Leaflet's {z}/{x}/{y} templating.
        'boundaries': json.dumps(feature),
    })

    base = "/api/projects/%s/tasks/%s/orthophoto/tiles/{z}/{x}/{y}.png" % (
        task.project_id, task.id)
    return "%s?%s" % (base, params)
