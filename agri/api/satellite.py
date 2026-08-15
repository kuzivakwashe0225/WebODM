"""
On-demand satellite pull endpoints (Stage 9). Session-authenticated, for
logged-in Precise Agric users -- distinct from agri/remote_sense/views.py's
inbound X-Api-Key endpoints, which are for server-to-server delivery.

Both endpoints dispatch a Celery task and return its id immediately; the
frontend polls existing WebODM endpoints (GET /api/workers/check|get/<id>) to
find out when the pull finishes -- no bespoke polling endpoint needed here.
"""
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from rest_framework import permissions, exceptions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from app.api.common import get_and_check_project
from agri.models import Boundary, Field
from agri.roles import can_contribute, get_role, ROLE_AGRONOMIST
from agri.tasks import pull_satellite_capture, pull_satellite_comparison


class SatelliteImageryPullView(APIView):
    """
    POST /api/agri/satellite/imagery/  {project, date_from, date_to, [name], [field]}

    Fetches satellite imagery for a farm's already-onboarded boundary over
    [date_from, date_to] and imports it as a satellite-sourced Capture -- the
    user only picks a date range (and optionally a single field to target
    instead of the whole farm); the area comes from AgriTrack onboarding.
    Returns 202 + a celery_task_id to poll.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if get_role(request.user) == ROLE_AGRONOMIST:
            raise exceptions.PermissionDenied(detail="Agronomists cannot request satellite imagery")

        project = get_and_check_project(request, request.data.get('project'), ('change_project',))

        date_from = parse_date(request.data.get('date_from') or '')
        date_to = parse_date(request.data.get('date_to') or '')
        if not date_from or not date_to:
            raise exceptions.ValidationError(detail="date_from and date_to (YYYY-MM-DD) are required")
        if date_from > date_to:
            raise exceptions.ValidationError(detail="date_from must not be after date_to")

        name = (request.data.get('name') or '').strip() or None
        field_id = request.data.get('field') or None
        if field_id is not None and not Field.objects.filter(pk=field_id, project_id=project.id).exists():
            raise exceptions.ValidationError(detail="That field does not belong to this farm")

        async_result = pull_satellite_capture.delay(
            project.id, date_from.isoformat(), date_to.isoformat(), name, field_id)
        return Response({'celery_task_id': async_result.task_id}, status=status.HTTP_202_ACCEPTED)


class SatelliteComparisonPullView(APIView):
    """
    POST /api/agri/satellite/compare/  {boundary}

    Fetches the satellite reference index for an already-APPROVED boundary's
    exact field/date and stores it as a second, comparable AnalysisRun next to
    this system's own analysis. Returns 202 + a celery_task_id to poll.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        boundary = get_object_or_404(Boundary, pk=request.data.get('boundary'))
        if not can_contribute(request.user, boundary.task.project):
            raise exceptions.PermissionDenied()
        if boundary.status != Boundary.APPROVED:
            raise exceptions.ValidationError(
                detail="Boundary must be approved before comparing against satellite data")

        async_result = pull_satellite_comparison.delay(boundary.id)
        return Response({'celery_task_id': async_result.task_id}, status=status.HTTP_202_ACCEPTED)


class SatelliteAvailabilityView(APIView):
    """
    GET /api/agri/satellite/availability/?project=<id>&date_from=&date_to=[&field=<id>]

    Lists actually-available Sentinel-2 scenes (date + cloud%) for a farm or a
    single field over a date range, via the Catalog API, so the "Get Satellite
    Imagery" picker can show real options instead of blindly trusting the
    automatic least-cloudy pick (stage-10-sentinel-roadmap.md Phase 2). Read-only
    and synchronous -- no Celery/polling needed for a lookup this light.

    :return: [{'date': 'YYYY-MM-DD', 'cloud_cover_pct': float}, ...], newest first
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from agri.remote_sense import sentinel_client
        from agri.remote_sense.sentinel_pull import _farm_geometry, _field_geometry, SatellitePullError

        project = get_and_check_project(request, request.query_params.get('project'))

        date_from = parse_date(request.query_params.get('date_from') or '')
        date_to = parse_date(request.query_params.get('date_to') or '')
        if not date_from or not date_to:
            raise exceptions.ValidationError(detail="date_from and date_to (YYYY-MM-DD) are required")
        if date_from > date_to:
            raise exceptions.ValidationError(detail="date_from must not be after date_to")

        field_id = request.query_params.get('field') or None
        try:
            if field_id:
                field = get_object_or_404(Field, pk=field_id, project_id=project.id)
                geom = _field_geometry(field)
            else:
                geom = _farm_geometry(project)
        except SatellitePullError as e:
            raise exceptions.ValidationError(detail=str(e))

        try:
            scenes = sentinel_client.search_available_scenes(geom, date_from, date_to)
        except sentinel_client.SentinelNotConfiguredError as e:
            raise exceptions.ValidationError(detail=str(e))
        except sentinel_client.SentinelRequestError as e:
            raise exceptions.ValidationError(detail="Could not check satellite availability: %s" % str(e))

        return Response(scenes)
