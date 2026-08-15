"""
Outbound results push to AgriTrack's *actual, currently-live* endpoint --
POST /orthophoto/analysis/push (X-Api-Key auth) -- as opposed to the
"rebuild target" /integrations/orthophoto/results described in the earlier,
more aspirational contract document. Only fires for AnalysisRuns whose
boundary is linked to a synced AgriField (agri.models.AgriField) -- without
farmId/fieldId there's nothing valid to send AgriTrack.

Field mapping is intentionally explicit about what we DO and DON'T compute:
our pipeline has no plant-counting or height/surface-model step yet, so
plant_count / height_mean_m / height_max_m / mean_plant_distance_cm /
avg_crown_diameter_cm are omitted rather than invented.
"""
import logging

import requests

from webodm import settings
from app.models import Task
from agri.models import AnalysisResult

logger = logging.getLogger('app.logger')


# --- Classification thresholds (PROPOSED DEFAULTS -- pending your/an agronomist's review) ---
#
# NDVI: reasonably well-established in remote-sensing literature (bare soil
# < ~0.2, sparse/moderate vegetation ~0.2-0.5, dense healthy vegetation > ~0.5).
# health_score is NDVI linearly rescaled so -0.1 -> 0.0 and 0.9 -> 1.0 (typical
# vegetated-scene span), then clamped to [0, 1].
NDVI_GOOD_MIN = 0.5
NDVI_FAIR_MIN = 0.2
NDVI_SCALE_LOW = -0.1
NDVI_SCALE_HIGH = 0.9

# ExG (Excess Green, RGB-only fallback when there's no NIR band): NOT as
# standardized in the literature as NDVI. These are placeholder cutoffs based
# on typical 8-bit-RGB ExG magnitudes for vegetated vs bare scenes -- treat as
# a rough heuristic only, and recalibrate against real field data once
# available. health_score = clamp(exg / EXG_SCALE_MAX, 0, 1).
EXG_GOOD_MIN = 25.0
EXG_FAIR_MIN = 5.0
EXG_SCALE_MAX = 60.0

# AgriTrack's orthophoto integration currently accepts the simpler 3-value
# scale (contract: "Orthophoto compatibility values currently accepted and
# normalized by AgriTrack: good, fair, poor") rather than the full 4-tier
# healthy/moderate/stressed/critical scale used elsewhere in their contract.
GOOD, FAIR, POOR = 'good', 'fair', 'poor'


def resolve_agri_field(boundary):
    """
    The AgriField (external AgriTrack field) an AnalysisRun's boundary maps to.

    Prefers the boundary's direct link (boundary.agri_field), but falls back to
    the persistent agri.Field's link (boundary.field.agri_field). Both seeding
    (agri/capture.py) and inbound sync (agri/agritrack/sync.py) set Field.agri_field,
    so a boundary that was hand-drawn or cloned before its direct agri_field link
    was attached is still resolvable this way. Returns None if neither is set
    (nothing valid to push to AgriTrack).
    """
    if boundary.agri_field_id is not None:
        return boundary.agri_field
    field = boundary.field
    if field is not None and field.agri_field_id is not None:
        return field.agri_field
    return None


def _clamp01(x):
    return max(0.0, min(1.0, x))


def classify_plant_health(index_name, mean_value):
    """
    :return: (classification, health_score) where classification is one of
             good/fair/poor and health_score is a float in [0, 1].
    """
    if mean_value is None:
        return None, None

    if index_name == 'NDVI':
        health_score = _clamp01((mean_value - NDVI_SCALE_LOW) / (NDVI_SCALE_HIGH - NDVI_SCALE_LOW))
        if mean_value >= NDVI_GOOD_MIN:
            return GOOD, health_score
        elif mean_value >= NDVI_FAIR_MIN:
            return FAIR, health_score
        return POOR, health_score

    # ExG (or anything else) -- rougher heuristic, see module docstring
    health_score = _clamp01(mean_value / EXG_SCALE_MAX)
    if mean_value >= EXG_GOOD_MIN:
        return GOOD, health_score
    elif mean_value >= EXG_FAIR_MIN:
        return FAIR, health_score
    return POOR, health_score


def _summary_to_text(summary):
    """
    AgriTrack's live /orthophoto/analysis/push expects `summary` as a string;
    our internal report builder (agri/analysis/report.py) produces it as a
    dict of stats. Render the known keys into a short human-readable line.
    """
    if not summary:
        return ""
    parts = []
    index_name = summary.get('plant_health_index')
    mean = summary.get('plant_health_mean')
    if index_name and mean is not None:
        parts.append("%s mean %.2f" % (index_name, mean))
    elif index_name:
        parts.append(index_name)
    canopy_pct = summary.get('canopy_pct')
    if canopy_pct is not None:
        parts.append("canopy cover %.1f%%" % canopy_pct)
    weed_count = summary.get('weed_count')
    if weed_count is not None:
        parts.append("%d weed hotspot(s)" % weed_count)
    grid_cells = summary.get('grid_cells')
    if grid_cells is not None:
        parts.append("%d grid cells" % grid_cells)
    return "; ".join(parts)


def build_orthophoto_result_payload(run):
    """
    Builds the payload for AgriTrack's live POST /orthophoto/analysis/push,
    per the confirmed-current contract: a camelCase envelope
    (sourceSystem/farmId/fieldId/subPlotId/scope/analysisDate/extId) wrapping
    the VARI/canopy/weed metrics, output URLs, and an `interpretation` block
    (summary + recommendations).

    subPlotId/scope: this pipeline analyses whole fields -- there is no
    sub-plot entity in the data model -- so subPlotId is null and scope is
    "field". Left explicit so AgriTrack can distinguish a field-level run from
    a (future) sub-plot one.
    """
    agri_field = resolve_agri_field(run.boundary)
    task = run.task
    results = {r.kind: r for r in run.results.all()}

    metrics = {}

    plant_health = results.get(AnalysisResult.PLANT_HEALTH)
    if plant_health:
        index_name = plant_health.stats.get('index')
        mean_value = plant_health.stats.get('mean')
        classification, health_score = classify_plant_health(index_name, mean_value)
        if health_score is not None:
            metrics['health_score'] = health_score
        if classification:
            metrics['classification'] = classification

    rgb_index = results.get(AnalysisResult.RGB_INDEX)
    if rgb_index:
        vari = rgb_index.stats.get('VARI') or {}
        if 'mean' in vari:
            metrics['vari_mean'] = vari['mean']
        if 'min' in vari:
            metrics['vari_min'] = vari['min']
        if 'max' in vari:
            metrics['vari_max'] = vari['max']

    canopy = results.get(AnalysisResult.CANOPY)
    if canopy and canopy.stats.get('canopy_pct') is not None:
        metrics['canopy_cover_pct'] = canopy.stats['canopy_pct']

    weed = results.get(AnalysisResult.WEED)
    if weed and weed.stats.get('weed_count') is not None:
        metrics['weed_count'] = weed.stats['weed_count']

    # Not yet computed by this pipeline -- deliberately omitted, not invented:
    # plant_count, mean_plant_distance_cm, avg_crown_diameter_cm,
    # height_mean_m, height_max_m, uniformity_pct, stress_zone_pct, healthy_zone_pct.

    outputs = {}
    # The source orthophoto the whole run was derived from.
    ortho_url = _build_asset_url(task, Task.ASSETS_MAP['orthophoto.tif'])
    if ortho_url:
        outputs['orthophoto_url'] = ortho_url
    for kind, url_key in ((AnalysisResult.PLANT_HEALTH, 'heatmap_url'),
                          (AnalysisResult.RGB_INDEX, 'index_map_url'),
                          (AnalysisResult.WEED, 'weed_map_url'),
                          (AnalysisResult.REPORT, 'comparison_url')):
        r = results.get(kind)
        if r and r.asset_path:
            outputs[url_key] = _build_asset_url(task, r.asset_path)

    report = results.get(AnalysisResult.REPORT)
    summary = (report.stats.get('summary') if report else None) or {}
    recommendations = (report.stats.get('recommendations') if report else None) or []

    return {
        'sourceSystem': 'orthophoto',
        'farmId': agri_field.farm.agritrack_farm_id,
        'fieldId': agri_field.agritrack_field_id,
        'subPlotId': None,
        'scope': 'field',
        'analysisDate': run.completed_at.date().isoformat() if run.completed_at else None,
        'extId': 'webodm-run-%s' % run.id,
        'metrics': metrics,
        'outputs': outputs,
        'interpretation': {
            'summary': _summary_to_text(summary),
            'recommendations': recommendations,
        },
    }


def _build_asset_url(task, asset_path):
    base = getattr(settings, 'AGRITRACK_PUBLIC_BASE_URL', None)
    if not base:
        return None
    return "%s/api/projects/%s/tasks/%s/assets/%s" % (
        base.rstrip('/'), task.project_id, task.id, asset_path)


def push_orthophoto_results(run, timeout=10):
    """
    POST an AnalysisRun's results to AgriTrack's live /orthophoto/analysis/push.
    No-op (returns False) if not configured or the boundary has no synced
    AgriField (nothing valid to report -- see module docstring).
    """
    if resolve_agri_field(run.boundary) is None:
        logger.info("AnalysisRun %s has no linked AgriField; skipping AgriTrack results push" % run.id)
        return False

    url = getattr(settings, 'AGRITRACK_RESULTS_PUSH_URL', None)
    api_key = getattr(settings, 'AGRITRACK_OUTBOUND_API_KEY', None)
    if not url or not api_key:
        logger.info("AgriTrack results push not configured; skipping for run %s" % run.id)
        return False

    payload = build_orthophoto_result_payload(run)
    r = requests.post(url, json=payload, headers={'X-Api-Key': api_key}, timeout=timeout)
    r.raise_for_status()
    logger.info("Pushed AnalysisRun %s results to AgriTrack (%s)" % (run.id, url))
    return True
