import json
import os

from django.utils import timezone
from django.utils.dateparse import parse_date
from django.shortcuts import get_object_or_404
from rest_framework import viewsets, permissions, parsers, exceptions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from app.api.common import get_and_check_project
from app.models import Task
from agri.capture import create_capture_from_orthophoto
from agri.models import Boundary, AnalysisRun, AnalysisResult, Field
from agri.roles import is_reviewer, can_contribute, get_role, ROLE_AGRONOMIST
from agri.tasks import run_analysis, push_analysis
from .serializers import BoundarySerializer, AnalysisRunSerializer


class BoundaryViewSet(viewsets.ModelViewSet):
    """
    Field boundaries for a capture (imported-orthophoto Task).

    - create  -> DRAFT; requires contribute rights on the capture's project
                 (Technician/legacy: own projects; Admin/SuperAdmin: any;
                 Agronomist: read-only, may not create)
    - approve/reject -> reviewers only (SuperAdmin/Admin/Agronomist)
    - visibility -> reviewers see all (org-wide); others see their own projects
    """
    serializer_class = BoundarySerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None  # few boundaries per capture; return a plain list
    # WebODM's global DEFAULT_FILTER_BACKENDS includes guardian's ObjectPermissionsFilter,
    # which would strip every Boundary (we assign no object perms on Boundary). We scope
    # visibility ourselves in get_queryset instead, so disable the global filters here.
    filter_backends = []

    def get_queryset(self):
        user = self.request.user
        qs = Boundary.objects.all()
        if not (user.is_superuser or is_reviewer(user)):
            qs = qs.filter(task__project__owner=user)
        task_id = self.request.query_params.get('task')
        if task_id:
            qs = qs.filter(task_id=task_id)
        return qs

    def _resolve_field(self, task, field, field_name):
        """
        Resolve the persistent agri.Field this boundary belongs to (for seasonal
        tracking): an explicit Field id, or a name (created within the farm if
        new), or None. A Field from a different farm is rejected.
        """
        if field is not None:
            if field.project_id != task.project_id:
                raise exceptions.ValidationError(
                    detail="That Field does not belong to this capture's farm")
            return field
        if field_name:
            f, _created = Field.objects.get_or_create(
                project=task.project, name=field_name.strip())
            return f
        return None

    def perform_create(self, serializer):
        task = serializer.validated_data['task']
        if not can_contribute(self.request.user, task.project):
            raise exceptions.PermissionDenied(
                detail="You cannot add boundaries to this capture")

        agri_field = serializer.validated_data.get('agri_field')
        geom = serializer.validated_data.get('geom')
        field = serializer.validated_data.get('field')
        # field_name is a write-only convenience field, not a model field -- pop
        # it so it isn't passed through to Boundary(...) on save.
        field_name = serializer.validated_data.pop('field_name', None)
        resolved_field = self._resolve_field(task, field, field_name)

        if agri_field is not None:
            # MVP: an AgriTrack-synced Field's boundary is authoritative and used
            # as-is (contract §3.2.1 -- never overwritten from imagery); a proper
            # per-farm UI choice between "use AgriTrack boundary" and "draw my own"
            # is a later increment (see precise-agric AgriTrack integration notes).
            if agri_field.farm.project_id != task.project_id:
                raise exceptions.ValidationError(
                    detail="This AgriTrack Field does not belong to this capture's farm")
            serializer.save(created_by=self.request.user, geom=agri_field.boundary,
                            name=serializer.validated_data.get('name') or agri_field.name,
                            field=resolved_field)
        elif geom is not None:
            serializer.save(created_by=self.request.user, field=resolved_field)
        else:
            raise exceptions.ValidationError(detail="Either 'geom' or 'agri_field' is required")

    def _review(self, request, new_status):
        if not is_reviewer(request.user):
            raise exceptions.PermissionDenied(
                detail="Only an Admin or Agronomist can review boundaries")
        boundary = self.get_object()
        boundary.status = new_status
        boundary.approved_by = request.user
        boundary.approved_at = timezone.now()
        boundary.save()
        return Response(BoundarySerializer(boundary).data)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        return self._review(request, Boundary.APPROVED)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        return self._review(request, Boundary.REJECTED)


class ReuseBoundariesView(APIView):
    """
    Populate a capture with the farm's established field boundaries (as DRAFT),
    copied from the most recent prior capture. Lets the user retroactively reuse
    fields on a capture that has none yet. POST /api/agri/reuse-boundaries/ {task}.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from agri.capture import clone_farm_fields_to_capture
        task = get_object_or_404(Task, pk=request.data.get('task'))
        if not can_contribute(request.user, task.project):
            raise exceptions.PermissionDenied()
        if task.boundaries.exists():
            raise exceptions.ValidationError(detail="This capture already has fields.")
        created = clone_farm_fields_to_capture(task)
        return Response({'created': len(created)}, status=status.HTTP_201_CREATED)


class FieldListView(APIView):
    """
    List the persistent Fields of a capture's farm (for the boundary-save Field
    picker / autocomplete). GET /api/agri/fields/?task=<task_id>.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from .serializers import FieldSerializer
        task_id = request.query_params.get('task')
        task = get_object_or_404(Task, pk=task_id)
        fields = Field.objects.filter(project_id=task.project_id)
        return Response(FieldSerializer(fields, many=True).data)


class CaptureUploadView(APIView):
    """
    Upload a single georeferenced orthophoto (.tif) to create a Capture on a
    project (Farm), via WebODM's external-import path (no processing node).

    POST /api/agri/captures/  (multipart: project, orthophoto, [name])
    """
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser]

    def post(self, request):
        if get_role(request.user) == ROLE_AGRONOMIST:
            raise exceptions.PermissionDenied(detail="Agronomists cannot upload captures")

        project = get_and_check_project(request, request.data.get('project'), ('change_project',))
        orthophoto = request.FILES.get('orthophoto')
        if orthophoto is None:
            raise exceptions.ValidationError(detail="An 'orthophoto' file is required")

        name = request.data.get('name') or 'Imported Capture'
        # Acquisition date (x-axis of the seasonal graphs). Falls back to today
        # inside create_capture_from_orthophoto when omitted/unparseable.
        capture_date_raw = request.data.get('capture_date')
        capture_date = parse_date(capture_date_raw) if capture_date_raw else None
        task = create_capture_from_orthophoto(project, orthophoto, name=name,
                                              dispatch=True, capture_date=capture_date)
        return Response({'id': str(task.id), 'project': project.id, 'name': task.name,
                         'status': task.status}, status=status.HTTP_201_CREATED)


class AnalysisRunViewSet(viewsets.ModelViewSet):
    """
    Analysis runs over an APPROVED boundary of a capture.

    - create  -> POST {boundary}. **Gated: the boundary must be APPROVED** and the
                 user must have contribute rights (Technician own / Admin any;
                 Agronomist read-only). Dispatches the 6-service fan-out.
    - approve/reject -> reviewers only, from PENDING_REVIEW. Approval triggers the
                 AgriTrack push (Stage 4).
    - visibility -> reviewers see all; others their own projects.
    """
    serializer_class = AnalysisRunSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None
    filter_backends = []

    def get_queryset(self):
        user = self.request.user
        qs = AnalysisRun.objects.all()
        if not (user.is_superuser or is_reviewer(user)):
            qs = qs.filter(task__project__owner=user)
        return qs

    def create(self, request, *args, **kwargs):
        boundary = get_object_or_404(Boundary, pk=request.data.get('boundary'))

        if not can_contribute(request.user, boundary.task.project):
            raise exceptions.PermissionDenied()
        if boundary.status != Boundary.APPROVED:
            raise exceptions.ValidationError(
                detail="Boundary must be APPROVED before running analysis")

        run = AnalysisRun.objects.create(task=boundary.task, boundary=boundary,
                                         triggered_by=request.user)
        run_analysis.delay(run.id)
        run.refresh_from_db()
        return Response(AnalysisRunSerializer(run).data, status=status.HTTP_201_CREATED)

    def _review(self, request, new_status):
        if not is_reviewer(request.user):
            raise exceptions.PermissionDenied(
                detail="Only an Admin or Agronomist can review analysis runs")
        run = self.get_object()
        if run.status != AnalysisRun.PENDING_REVIEW:
            raise exceptions.ValidationError(
                detail="Only runs pending review can be approved or rejected")
        run.status = new_status
        run.reviewed_by = request.user
        run.save()
        return run

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        run = self._review(request, AnalysisRun.APPROVED)
        # Stage 4: push the approved results to AgriTrack (no-op if URL unset)
        push_analysis.delay(run.id)
        run.refresh_from_db()
        return Response(AnalysisRunSerializer(run).data)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        run = self._review(request, AnalysisRun.REJECTED)
        return Response(AnalysisRunSerializer(run).data)

    @action(detail=True, methods=['get'])
    def grid(self, request, pk=None):
        """
        Return the grid-analysis GeoJSON FeatureCollection (per-cell mean_index)
        for this run, so the map can render clickable zones. Served through our
        own API (not the generic asset endpoint) to keep auth in this viewset.
        """
        run = self.get_object()
        result = run.results.filter(kind=AnalysisResult.GRID).first()
        if not result or not result.asset_path:
            raise exceptions.NotFound()
        path = run.task.assets_path(result.asset_path)
        if not os.path.isfile(path):
            raise exceptions.NotFound()
        with open(path) as f:
            data = json.load(f)
        return Response(data)
