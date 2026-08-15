"""
On-demand satellite pull: turns a user's "Get satellite data" click into a real
Sentinel Hub request, using the farm/field boundaries already on file from
AgriTrack onboarding -- the user never draws or configures an area, they only
pick a date range (see precise-agric/stages/stage-9-satellite-monitoring.md §7).

Two independent pulls:
  - pull_satellite_capture(): imagery -> a new satellite-sourced Capture, via the
    same import path every other capture uses (agri/capture.py). Boundaries are
    auto-seeded/cloned exactly as for any other capture.
  - pull_satellite_comparison(): Sentinel's OWN computed index for an ALREADY
    APPROVED boundary's exact field + date -- stored as a second AnalysisRun
    (computed_by=SENTINEL) on that same boundary, next to our own analysis, for
    side-by-side comparison. Requires an approved boundary (not a bare farm) so
    there's always an unambiguous task/boundary to attach the result to.
"""
import datetime
import functools
import os
import tempfile

from django.utils import timezone

from agri.capture import create_capture_from_orthophoto
from agri.models import AgriFarm, AnalysisResult, AnalysisRun, Boundary, CaptureMeta, Field
from agri.remote_sense import sentinel_client
from app.models import Project
from webodm import settings


class SatellitePullError(Exception):
    """A user-facing failure pulling satellite data (maps to HTTP 400)."""


def _farm_geometry(project):
    """
    The AOI to request imagery for: the farm's AgriTrack-synced boundary
    (the common case per the onboarding flow), falling back to the union of any
    Field boundaries already drawn/approved on this farm's captures. Raises a
    clear, actionable error if neither exists yet.
    """
    agri_farm = AgriFarm.objects.filter(project_id=project.id).first()
    if agri_farm is not None and agri_farm.boundary is not None:
        return agri_farm.boundary

    geoms = list(Boundary.objects.filter(task__project=project)
                 .exclude(geom__isnull=True).values_list('geom', flat=True).distinct())
    if geoms:
        return functools.reduce(lambda a, b: a.union(b), geoms)

    raise SatellitePullError(
        "No farm or field boundary found yet for this farm -- sync it from AgriTrack "
        "or draw at least one field boundary before requesting satellite imagery.")


def _field_geometry(field):
    """
    The AOI for a single Field: its AgriTrack-synced boundary if linked
    (authoritative, per contract §3.2.1), else the most recent Boundary drawn
    against it. Mirrors _farm_geometry()'s fallback pattern, scoped to one field
    -- lets a user target "just this field" instead of the whole farm (Stage 10
    roadmap Phase 1: "both as options").
    """
    if field.agri_field is not None and field.agri_field.boundary is not None:
        return field.agri_field.boundary

    boundary = (Boundary.objects.filter(field=field).exclude(geom__isnull=True)
               .order_by('-created_at').first())
    if boundary is not None:
        return boundary.geom

    raise SatellitePullError(
        "No boundary found yet for '%s' -- sync it from AgriTrack or draw a "
        "boundary for it before requesting satellite imagery." % field.name)


def pull_satellite_capture(project_id, date_from, date_to, name=None, field_id=None):
    """
    Fetch the least-cloudy satellite image in [date_from, date_to] covering
    either the whole farm's onboarded boundary, or (when field_id is given) just
    that one field, and import it as a satellite-sourced Capture.

    :param date_from, date_to: datetime.date
    :param field_id: optional agri.Field id to target instead of the whole farm.
    :return: the created Task (Capture)
    """
    try:
        project = Project.objects.get(pk=project_id)
    except Project.DoesNotExist:
        raise SatellitePullError("Farm not found")

    if field_id is not None:
        try:
            field = Field.objects.get(pk=field_id, project_id=project.id)
        except Field.DoesNotExist:
            raise SatellitePullError("That field was not found on this farm")
        geom = _field_geometry(field)
    else:
        geom = _farm_geometry(project)

    os.makedirs(settings.MEDIA_TMP, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix='_satellite.tif', dir=settings.MEDIA_TMP)
    os.close(fd)
    try:
        try:
            fetch_result = sentinel_client.fetch_field_imagery(geom, date_from, date_to, tmp_path)
        except sentinel_client.SentinelNotConfiguredError as e:
            raise SatellitePullError(str(e))
        except sentinel_client.SentinelRequestError as e:
            raise SatellitePullError(
                "Could not fetch satellite imagery for %s to %s: %s" % (date_from, date_to, str(e)))

        task = create_capture_from_orthophoto(
            project, fetch_result['path'],
            name=name or ('Satellite capture (%s to %s)' % (date_from, date_to)),
            dispatch=True, capture_date=date_to, source=CaptureMeta.SATELLITE)

        # Best-effort quality signal, stored alongside the already-successful
        # import -- "flag, don't block" (stage-10-sentinel-roadmap.md Phase 1).
        # A missing/failed quality check never undoes the import above.
        if fetch_result.get('valid_pixel_pct') is not None:
            capture_meta = task.capture_meta
            capture_meta.valid_pixel_pct = fetch_result['valid_pixel_pct']
            capture_meta.save(update_fields=['valid_pixel_pct'])
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return task


def pull_satellite_comparison(boundary_id):
    """
    Fetch Sentinel's own computed NDVI for an APPROVED boundary's exact geometry
    and capture date, and record it as a second AnalysisRun (computed_by=SENTINEL)
    on that same boundary -- next to whatever this system's own analysis found,
    for side-by-side comparison (stage-9-satellite-monitoring.md §9).

    :return: the created AnalysisRun
    """
    try:
        boundary = Boundary.objects.select_related('task', 'task__capture_meta').get(pk=boundary_id)
    except Boundary.DoesNotExist:
        raise SatellitePullError("Field boundary not found")

    if boundary.status != Boundary.APPROVED:
        raise SatellitePullError(
            "This field boundary must be approved before it can be compared against satellite data")

    capture_meta = getattr(boundary.task, 'capture_meta', None)
    capture_date = capture_meta.capture_date if capture_meta else boundary.task.created_at.date()

    try:
        points = sentinel_client.fetch_field_statistics(
            boundary.geom, capture_date, capture_date + datetime.timedelta(days=1))
    except sentinel_client.SentinelNotConfiguredError as e:
        raise SatellitePullError(str(e))
    except sentinel_client.SentinelRequestError as e:
        raise SatellitePullError("Could not fetch satellite reference data: %s" % str(e))

    if not points:
        raise SatellitePullError(
            "No satellite pass with usable data was found for %s over this field" % capture_date)

    point = points[0]

    run = AnalysisRun.objects.create(
        task=boundary.task, boundary=boundary, computed_by=AnalysisRun.SENTINEL,
        status=AnalysisRun.PENDING_REVIEW, index_used='NDVI', completed_at=timezone.now())
    AnalysisResult.objects.create(
        run=run, kind=AnalysisResult.PLANT_HEALTH,
        stats={'index': 'NDVI', 'mean': point['mean'], 'stddev': point.get('stddev'),
              'reference_date': point['date'], 'valid_pixel_pct': point.get('valid_pixel_pct')})

    return run
