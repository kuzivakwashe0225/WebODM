"""
Seasonal-progress API: time series of analysis metrics per Field and for the
whole Farm across a season, built from finished AnalysisRuns joined to their
persistent Field and the capture's acquisition date.

x-axis = CaptureMeta.capture_date (fallback: task.created_at date).
Series values come straight from the stats already stored on AnalysisResult.
"""
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from app.api.common import get_and_check_project
from agri.models import AnalysisRun, AnalysisResult

# Runs whose fan-out has completed (results are populated).
FINISHED_STATUSES = (AnalysisRun.PENDING_REVIEW, AnalysisRun.APPROVED)

# Numeric metrics we average when aggregating fields into a whole-farm line.
NUMERIC_METRICS = ('health_mean', 'canopy_pct', 'weed_count',
                   'vari_mean', 'gli_mean', 'exg_mean')


def _capture_date(task):
    cm = getattr(task, 'capture_meta', None)
    if cm is not None:
        return cm.capture_date
    return task.created_at.date()


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

        # field id -> {name, points: {date -> metric dict}}; later runs on the
        # same date overwrite earlier ones (one point per field per date).
        fields = {}
        for run in runs:
            field = run.boundary.field
            entry = fields.setdefault(field.id, {'name': field.name, 'points': {}})
            date_str = _capture_date(run.task).isoformat()
            point = {'date': date_str}
            point.update(_run_metrics(run))
            entry['points'][date_str] = point

        field_list = []
        farm_points_by_date = {}
        for field_id, entry in fields.items():
            series = [entry['points'][d] for d in sorted(entry['points'])]
            field_list.append({'id': field_id, 'name': entry['name'], 'series': series})
            for point in series:
                farm_points_by_date.setdefault(point['date'], []).append(point)

        farm_series = []
        for date_str in sorted(farm_points_by_date):
            farm_series.append({'date': date_str, **_average(farm_points_by_date[date_str])})

        field_list.sort(key=lambda f: f['name'])
        return Response({
            'project': project.id,
            'fields': field_list,
            'farm': {'series': farm_series},
        })
