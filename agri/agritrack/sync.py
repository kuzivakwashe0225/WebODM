"""
Inbound structure sync from AgriTrack (the farmer-facing mobile app).

Implements the "Canonical sync payload" from the AgriTrack integration
contract (§6.2): a Farm + its Fields, with AgriTrack's own IDs and
farmer-drawn (authoritative) boundaries. SubPlots are accepted but ignored
for v1 (locked decision -- Farm+Field only for the first integration pass).

AgriTrack IDs are preserved exactly (contract §3.1) and boundaries here are
never overwritten by anything imagery-derived (contract §3.2.1) -- this
module only ever writes what AgriTrack itself sent.
"""
import json

from django.contrib.auth.models import User
from django.contrib.gis.geos import GEOSGeometry, GEOSException
from django.db import transaction

from app.models import Project
from agri.models import AgriFarm, AgriField, Field
from webodm import settings


class SyncValidationError(Exception):
    pass


def _parse_geometry(geojson, field_label):
    if not geojson:
        return None
    try:
        return GEOSGeometry(json.dumps(geojson))
    except (GEOSException, TypeError, ValueError):
        raise SyncValidationError("Invalid geometry for %s" % field_label)


def get_sync_owner_user():
    """
    The WebODM user that owns Projects auto-created from AgriTrack farm syncs.
    Farmers don't have WebODM accounts, so this is a configurable service
    account (WO_AGRITRACK_SYNC_OWNER env var), falling back to the first
    superuser. MVP simplification -- revisit if/when farmers get real accounts.
    """
    username = getattr(settings, 'AGRITRACK_SYNC_OWNER_USERNAME', None)
    if username:
        user = User.objects.filter(username=username).first()
        if user:
            return user
    user = User.objects.filter(is_superuser=True).order_by('id').first()
    if user is None:
        raise SyncValidationError(
            "No AgriTrack sync owner configured and no superuser exists to fall back to")
    return user


@transaction.atomic
def sync_farm_payload(data):
    """
    Upserts an AgriFarm (+ its Project) and its AgriFields from a payload
    shaped like the contract's §6.2 canonical sync payload. Idempotent: safe
    to call repeatedly with the same or updated data.

    :return: the AgriFarm
    """
    farm_id = data.get('farmId')
    if farm_id is None:
        raise SyncValidationError("farmId is required")

    farm_info = data.get('farm') or {}
    boundaries = data.get('boundaries') or {}
    farm_geom = _parse_geometry(boundaries.get('farm'), 'farm boundary')

    agri_farm = AgriFarm.objects.filter(agritrack_farm_id=farm_id).select_related('project').first()
    if agri_farm is None:
        owner = get_sync_owner_user()
        project = Project.objects.create(
            owner=owner,
            name=farm_info.get('name') or ('AgriTrack Farm %s' % farm_id),
            description='Synced from AgriTrack')
        agri_farm = AgriFarm(agritrack_farm_id=farm_id, project=project)

    agri_farm.agritrack_farmer_id = data.get('farmerId')
    if farm_info.get('name'):
        agri_farm.name = farm_info['name']
    if farm_info.get('location'):
        agri_farm.location = farm_info['location']
    if farm_geom is not None:
        agri_farm.boundary = farm_geom
    agri_farm.save()

    # Keep the underlying Project's name in step with the farm name (best-effort;
    # a technician may still rename the Project locally afterwards).
    if farm_info.get('name') and agri_farm.project.name != farm_info['name']:
        agri_farm.project.name = farm_info['name']
        agri_farm.project.save()

    for f in (data.get('fields') or []):
        _sync_field(agri_farm, f)

    # SubPlots: accepted, intentionally not persisted in v1 (locked decision).

    return agri_farm


def _sync_field(agri_farm, f):
    field_id = f.get('fieldId')
    if field_id is None:
        raise SyncValidationError("fields[].fieldId is required")

    agri_field = AgriField.objects.filter(agritrack_field_id=field_id).first()
    is_new = agri_field is None
    if is_new:
        agri_field = AgriField(agritrack_field_id=field_id, farm=agri_farm)

    agri_field.farm = agri_farm
    if f.get('name'):
        agri_field.name = f['name']
    if f.get('crop'):
        agri_field.crop = f['crop']
    if f.get('area_ha') is not None:
        agri_field.area_ha = f['area_ha']

    boundary_geom = _parse_geometry(f.get('boundary'), 'field %s boundary' % field_id)
    if boundary_geom is not None:
        agri_field.boundary = boundary_geom
    elif is_new:
        raise SyncValidationError("field %s has no boundary and no prior sync exists" % field_id)
    # else: existing field, boundary omitted this sync -> keep the last-known one

    agri_field.save()

    # Give the synced field a persistent agri.Field identity too, so its captures
    # show up in the seasonal-progress view alongside hand-drawn fields.
    _ensure_persistent_field(agri_farm, agri_field)

    return agri_field


def _ensure_persistent_field(agri_farm, agri_field):
    field = Field.objects.filter(agri_field=agri_field).first()
    if field is None:
        name = agri_field.name or ('AgriTrack Field %s' % agri_field.agritrack_field_id)
        field, _created = Field.objects.get_or_create(
            project=agri_farm.project, name=name,
            defaults={'agri_field': agri_field})
        if field.agri_field_id is None:
            field.agri_field = agri_field
            field.save(update_fields=['agri_field'])
    return field
