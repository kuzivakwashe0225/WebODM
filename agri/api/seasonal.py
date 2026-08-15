"""
Seasonal-progress API: time series of analysis metrics per Field and for the
whole Farm across a season, built from finished AnalysisRuns joined to their
persistent Field and the capture's acquisition date.

x-axis = CaptureMeta.capture_date (fallback: task.created_at date).
Series values come straight from the stats already stored on AnalysisResult.

Points are additionally tagged with `source` (drone/satellite, from the
capture) and `computed_by` (webodm/sentinel, from the run) and keyed by
(date, source, computed_by) rather than by date alone -- a drone and a
satellite capture on the same date, or our own vs Sentinel's own numbers for
the same date, are never comparable (different pixel sizes, possibly
different formulas) and must never silently overwrite or average into each
other. See precise-agric/stages/stage-9-satellite-monitoring.md §5 / §9.

Each point also carries `valid_pixel_pct` (Stage 10 Phase 5) -- the same
SCL-derived cloud/quality metric Stage 10 Phase 1 stores on CaptureMeta,
surfaced here so a per-field observation timeline built on this endpoint's
existing per-field `series` (this response already IS that timeline's data
shape; no separate aggregation endpoint was needed -- see
stage-10-sentinel-roadmap.md Phase 5) can flag a low-quality satellite point
the same way PreciseAgricPanel.jsx's comparison card already does. None for
drone points, since there's nothing to flag.

Each point also carries `anomaly` (Stage 10 Phase 6) -- non-None when
`health_mean` dropped more than ANOMALY_DROP_THRESHOLD_PCT below that point's
own field+source+engine trailing average (see compute_field_anomalies()).
Locked product decisions (asked the user directly, since neither has a right
answer without real production history yet -- see stage-10-sentinel-roadmap.md
Phase 6): a fixed percentage-drop threshold, not a statistical z-score (which
needs more history per field than exists this early to mean anything); and
surfaced as a badge on this same timeline rather than a new dashboard-level
alert surface.
"""
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from app.api.common import get_and_check_project
from agri.models import AnalysisRun, AnalysisResult, CaptureMeta

# Runs whose fan-out has completed (results are populated).
FINISHED_STATUSES = (AnalysisRun.PENDING_REVIEW, AnalysisRun.APPROVED)

# Numeric metrics we average when aggregating fields into a whole-farm line.
NUMERIC_METRICS = ('health_mean', 'canopy_pct', 'weed_count',
                   'vari_mean', 'gli_mean', 'exg_mean')

# Stage 10 Phase 6: flag a point when its metric drops this many percent (or
# more) below its own field+source+engine's trailing average. Only health_mean
# is checked -- it's the one metric present for essentially every finished run
# (drone plant-health AND the Sentinel comparison both write PLANT_HEALTH),
# and is what stage-9 §10 / the roadmap's own "NDVI down" example anchor on.
ANOMALY_METRIC = 'health_mean'
ANOMALY_DROP_THRESHOLD_PCT = 15.0
# Need at least this many PRIOR points before a "trailing average" means
# anything more than "compared to one earlier number".
MIN_BASELINE_POINTS = 2


def _capture_date(task):
    cm = getattr(task, 'capture_meta', None)
    if cm is not None:
        return cm.capture_date
    return task.created_at.date()


def _capture_source(task):
    cm = getattr(task, 'capture_meta', None)
    # No CaptureMeta row means a raw multi-image flight processed by NodeODM --
    # every such Task in this system is, by construction, a drone flight.
    return getattr(cm, 'source', CaptureMeta.DRONE)


def _capture_valid_pixel_pct(task):
    # Stage 10 Phase 5 observation timeline: surfaces the SCL-derived cloud/
    # quality metric (Stage 10 Phase 1, agri/remote_sense/sentinel_client.py)
    # alongside each point so a low-quality satellite pull can be flagged in the
    # timeline the same way it already is on the "Compare with Satellite" card
    # (PreciseAgricPanel.jsx). None for drone captures -- there's nothing to flag.
    cm = getattr(task, 'capture_meta', None)
    return getattr(cm, 'valid_pixel_pct', None)


def _run_metrics(run):
    results = {r.kind: r for r in run.results.all()}
    out = {}

    ph = results.get(AnalysisResult.PLANT_HEALTH)
    if ph and ph.stats:
        out['index'] = ph.stats.get('index')
        if ph.stats.get('mean') is not None:
            out['health_mean'] = ph.stats['mean']

    canopy = results.get(AnalysisResult.CANOPY)
    if canopy and canopy.stats.get('canopy_pct') is not None:
        out['canopy_pct'] = canopy.stats['canopy_pct']

    weed = results.get(AnalysisResult.WEED)
    if weed and weed.stats.get('weed_count') is not None:
        out['weed_count'] = weed.stats['weed_count']

    rgb = results.get(AnalysisResult.RGB_INDEX)
    if rgb and rgb.stats:
        for key, out_key in (('VARI', 'vari_mean'), ('GLI', 'gli_mean'), ('EXG', 'exg_mean')):
            d = rgb.stats.get(key)
            if isinstance(d, dict) and d.get('mean') is not None:
                out[out_key] = round(d['mean'], 4)

    return out


def _average(points):
    agg = {}
    for metric in NUMERIC_METRICS:
        vals = [p[metric] for p in points if p.get(metric) is not None]
        if vals:
            agg[metric] = round(sum(vals) / len(vals), 4)
    return agg


def _anomaly_for_latest(chronological_points, metric=ANOMALY_METRIC):
    """
    Pure function, no DB/network -- given points for ONE field+source+engine
    group, oldest first, checks whether the LAST point in the list is a real
    drop from the trailing average of every point before it.

    :return: {'trailing_avg': float, 'deviation_pct': float} if the latest
             point dropped >= ANOMALY_DROP_THRESHOLD_PCT below the trailing
             average of its predecessors; None if there isn't enough prior
             data, either value is missing, or it isn't a qualifying drop.
    """
    prior = [p[metric] for p in chronological_points[:-1] if p.get(metric) is not None]
    if len(prior) < MIN_BASELINE_POINTS:
        return None
    latest = chronological_points[-1].get(metric)
    if latest is None:
        return None
    trailing_avg = sum(prior) / len(prior)
    if trailing_avg == 0:
        return None
    deviation_pct = ((latest - trailing_avg) / trailing_avg) * 100
    if deviation_pct > -ANOMALY_DROP_THRESHOLD_PCT:
        return None
    return {'trailing_avg': round(trailing_avg, 4), 'deviation_pct': round(deviation_pct, 1)}


def compute_field_anomalies(series, metric=ANOMALY_METRIC):
    """
    Pure function -- given one field's full series (every source/engine mixed,
    any order), returns a dict keyed by (date, source, computed_by) -> anomaly
    dict for every point that was a qualifying drop from ITS OWN preceding
    history at the time. Grouped strictly by (source, computed_by) before
    comparison -- a drone ExG value and a Sentinel NDVI value are never on the
    same scale, so comparing one field's drone history against its own drone
    history (never against its satellite history) is the only comparison that
    means anything, matching the same "never blend" rule this module already
    applies to the farm-level average above.

    :param series: this field's `series` list, any order (as returned in the
                    API response -- already sorted by (date, source,
                    computed_by), but this function re-sorts defensively).
    :return: {(date, source, computed_by): {'trailing_avg', 'deviation_pct'}, ...}
    """
    groups = {}
    for p in series:
        groups.setdefault((p['source'], p['computed_by']), []).append(p)

    anomalies = {}
    for points in groups.values():
        points = sorted(points, key=lambda p: p['date'])
        for i in range(MIN_BASELINE_POINTS, len(points)):
            result = _anomaly_for_latest(points[:i + 1], metric)
            if result:
                p = points[i]
                anomalies[(p['date'], p['source'], p['computed_by'])] = result
    return anomalies


class SeasonalView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        project = get_and_check_project(request, request.query_params.get('project'))

        runs = (AnalysisRun.objects
                .filter(task__project_id=project.id,
                        status__in=FINISHED_STATUSES,
                        boundary__field__isnull=False)
                .select_related('boundary', 'boundary__field', 'task', 'task__capture_meta')
                .prefetch_related('results')
                .order_by('created_at'))

        # field id -> {name, points: {(date, source, computed_by) -> metric dict}}.
        # Keying on the triple (not just date) means a same-day drone + satellite
        # capture, or our own vs Sentinel's own numbers for the same date, each
        # keep their own point instead of one silently overwriting the other.
        fields = {}
        for run in runs:
            field = run.boundary.field
            entry = fields.setdefault(field.id, {'name': field.name, 'points': {}})
            date_str = _capture_date(run.task).isoformat()
            source = _capture_source(run.task)
            computed_by = run.computed_by
            point = {'date': date_str, 'source': source, 'computed_by': computed_by,
                     'valid_pixel_pct': _capture_valid_pixel_pct(run.task)}
            point.update(_run_metrics(run))
            entry['points'][(date_str, source, computed_by)] = point

        field_list = []
        farm_points_by_key = {}
        for field_id, entry in fields.items():
            series = [entry['points'][k] for k in
                     sorted(entry['points'], key=lambda k: (k[0], k[1], k[2]))]
            anomalies = compute_field_anomalies(series)
            for point in series:
                point['anomaly'] = anomalies.get(
                    (point['date'], point['source'], point['computed_by']))
            field_list.append({'id': field_id, 'name': entry['name'], 'series': series})
            for point in series:
                key = (point['date'], point['source'], point['computed_by'])
                farm_points_by_key.setdefault(key, []).append(point)

        # Farm-level average is computed WITHIN each (date, source, computed_by)
        # group only -- never blending drone with satellite, or our numbers with
        # Sentinel's, into one misleading average.
        farm_series = []
        for key in sorted(farm_points_by_key):
            date_str, source, computed_by = key
            farm_series.append({'date': date_str, 'source': source, 'computed_by': computed_by,
                               **_average(farm_points_by_key[key])})

        field_list.sort(key=lambda f: f['name'])
        return Response({
            'project': project.id,
            'fields': field_list,
            'farm': {'series': farm_series},
        })
