"""
Capture ingestion for Precise Agric.

A "Capture" is an imported-orthophoto WebODM Task (we reuse app.Task; there's no
separate model). This module turns a single georeferenced GeoTIFF into a Capture
using WebODM's *external import* path — no processing node, no ZIP:

  - create a Task with import_url="file://external" and pending_action=IMPORT
  - place the orthophoto at the canonical asset path (odm_orthophoto/odm_orthophoto.tif)
  - dispatch processing; extract_assets_and_complete() skips ZIP extraction for
    external imports and goes straight to extent/band detection -> COMPLETED.

See precise-agric/architecture-and-plan.md §14 for why a lone .tif can't use the
normal (all.zip) import endpoint.
"""
import os
import shutil

from django.db import transaction
from django.utils import timezone

from app import pending_actions
from app.models import Task
from agri.models import CaptureMeta
from nodeodm import status_codes
from worker import tasks as worker_tasks


def create_capture_from_orthophoto(project, orthophoto, name="Imported Capture",
                                   dispatch=True, capture_date=None, source=None):
    """
    :param project: app.models.Project the capture belongs to (a Farm)
    :param orthophoto: filesystem path (str) or an uploaded file-like object
                       (supports .chunks() or .read())
    :param name: capture/task name
    :param dispatch: dispatch async processing (True in production). Tests pass
                     dispatch=False and drive worker.tasks.process_task directly.
    :param capture_date: acquisition date (datetime.date). Defaults to today --
                         the x-axis of the seasonal-progress graphs.
    :param source: CaptureMeta.DRONE (default) or CaptureMeta.SATELLITE -- see
                   precise-agric/stages/stage-9-satellite-monitoring.md §5. Callers
                   importing Sentinel imagery (agri/remote_sense/ingest.py) pass
                   SATELLITE explicitly; every other caller keeps today's DRONE
                   default (manual single-.tif uploads have always meant "a
                   capture", most commonly a drone ortho).
    :return: the created Task (a Capture), still RUNNING until processed.
    """
    with transaction.atomic():
        task = Task.objects.create(project=project,
                                   name=name,
                                   import_url="file://external",
                                   auto_processing_node=False,
                                   status=status_codes.RUNNING,
                                   pending_action=pending_actions.IMPORT)
        task.create_task_directories()

        CaptureMeta.objects.create(task=task,
                                   capture_date=capture_date or timezone.now().date(),
                                   source=source or CaptureMeta.DRONE)

        dst = task.assets_path(Task.ASSETS_MAP["orthophoto.tif"])
        os.makedirs(os.path.dirname(dst), exist_ok=True)

        if isinstance(orthophoto, str):
            shutil.copyfile(orthophoto, dst)
        elif hasattr(orthophoto, 'chunks'):
            with open(dst, 'wb') as fd:
                for chunk in orthophoto.chunks():
                    fd.write(chunk)
        elif hasattr(orthophoto, 'read'):
            with open(dst, 'wb') as fd:
                shutil.copyfileobj(orthophoto, fd)
        else:
            raise ValueError("orthophoto must be a filesystem path or a file-like object")

        # Reuse the farm's established field boundaries: the FIRST upload sets the
        # fields up (draw + approve); every later upload of the same farm inherits
        # them automatically, so the same areas are analyzed consistently over the
        # season without redrawing. On the very first capture of an AgriTrack-synced
        # farm there is no prior capture to clone from, so fall back to seeding the
        # boundaries straight from the farm's synced AgriFields -- this is what makes
        # an imported satellite image line up with the fields drawn in the mobile app.
        if not clone_farm_fields_to_capture(task):
            seed_boundaries_from_agrifields(task)

    if dispatch:
        worker_tasks.process_task.delay(task.id)

    return task


def clone_farm_fields_to_capture(task):
    """
    Reuse the farm's field boundaries on this new capture so the same areas are
    monitored across the season without redrawing.

    Strategy: copy every boundary from the most recent PRIOR capture of this farm
    that has boundaries. This is robust -- it does not depend on Field links being
    set at draw time, and it skips over intermediate captures that happen to have
    no boundaries. Each copy is created as DRAFT (uploads are weekly monitoring
    passes; a reviewer re-confirms each pass before its analysis runs).

    For seasonal tracking, each reused boundary keeps a stable Field identity: the
    source's Field if it has one, otherwise a Field auto-created from the boundary
    name (and back-filled onto the source so the whole series lines up).

    Returns the list of created Boundaries (empty on the first upload).
    """
    from agri.models import Field, Boundary

    prior = (Task.objects.filter(project=task.project)
             .exclude(id=task.id)
             .filter(boundaries__isnull=False)
             .order_by('-created_at').distinct().first())
    if prior is None:
        return []

    created = []
    for src in Boundary.objects.filter(task=prior):
        field = src.field
        if field is None:
            field, _created = Field.objects.get_or_create(
                project=task.project, name=src.name)
            src.field = field
            src.save(update_fields=['field'])
        # Carry the AgriTrack link forward too, so every capture's analysis (not
        # just the first) is pushable back to AgriTrack per-field (results.py keys
        # the outbound push off boundary.agri_field). Fall back to the persistent
        # Field's link when the source boundary itself was never directly linked
        # (e.g. hand-drawn against a synced Field), so the null doesn't propagate.
        agri_field = src.agri_field or (field.agri_field if field else None)
        created.append(Boundary.objects.create(
            task=task, field=field, agri_field=agri_field, name=src.name,
            geom=src.geom, status=Boundary.DRAFT))
    return created


def seed_boundaries_from_agrifields(task):
    """
    Seed DRAFT boundaries on a capture directly from the farm's AgriTrack-synced
    Fields (their authoritative boundaries). Used for the first capture of a
    freshly-synced farm, which has no prior capture to clone from -- without this
    the imported image would have no fields to review/analyze.

    Each seeded boundary keeps its `agri_field` link (so its analysis pushes back
    to AgriTrack per-field) and a stable `field` identity (so it lines up across
    the season). No-op on farms that aren't AgriTrack-linked or whose fields have
    no boundary yet. Returns the created Boundaries.
    """
    from agri.models import AgriFarm, AgriField, Field, Boundary

    agri_farm = AgriFarm.objects.filter(project_id=task.project_id).first()
    if agri_farm is None:
        return []

    created = []
    for af in AgriField.objects.filter(farm=agri_farm):
        if af.boundary is None:
            continue
        name = af.name or ("AgriTrack Field %s" % af.agritrack_field_id)
        field = Field.objects.filter(agri_field=af).first()
        if field is None:
            field, _created = Field.objects.get_or_create(
                project=task.project, name=name, defaults={'agri_field': af})
            if field.agri_field_id is None:
                field.agri_field = af
                field.save(update_fields=['agri_field'])
        created.append(Boundary.objects.create(
            task=task, agri_field=af, field=field, name=name,
            geom=af.boundary, status=Boundary.DRAFT))
    return created
