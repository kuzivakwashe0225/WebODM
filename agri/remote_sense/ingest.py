"""
Remote-sense (Sentinel) capture ingestion.

An external remote-sensing system produces a georeferenced GeoTIFF for a farm and
tags it with that farm's AgriTrack farm id (the same id the mobile app uses). This
module resolves that farm to its WebODM Project and imports the GeoTIFF as a
Capture via the normal external-import path (agri.capture) -- no processing node,
since a satellite orthophoto is already georeferenced.

Three delivery modes are supported:
  - push: the .tif bytes are uploaded directly (multipart) -> a file-like object
  - bands: several single-band .tif files (B02/B03/B04/B08/...) are uploaded and
           stacked+tagged into one multi-band COG here (see bands.py) before import
  - pull: a JSON {farm_id, image_url} is posted and we fetch image_url from the
          remote-sense host (restricted to settings.REMOTE_SENSE_BASE_URL as an
          SSRF guard, authenticated with our shared key).

The farm MUST already exist (have been synced from AgriTrack first) -- we never
auto-create it, because without the synced farm + its AgriFields there are no
boundaries to fit the image onto or analyze.
"""
import os
import tempfile

import requests

from webodm import settings
from agri.models import AgriFarm, CaptureMeta
from agri.capture import create_capture_from_orthophoto
from agri.remote_sense.bands import (stack_and_tag_bands, detect_band_role,
                                     BandStackError)


class RemoteSenseValidationError(Exception):
    """Bad request from the remote-sense caller (maps to HTTP 400)."""


class FarmNotFoundError(Exception):
    """No synced AgriFarm for the given farm id (maps to HTTP 404)."""


def resolve_project(farm_id):
    """Resolve an AgriTrack farm id to its WebODM Project, or raise."""
    if farm_id in (None, ''):
        raise RemoteSenseValidationError("farm_id is required")
    try:
        farm_id_int = int(farm_id)
    except (TypeError, ValueError):
        raise RemoteSenseValidationError("farm_id must be an integer")

    agri_farm = (AgriFarm.objects.filter(agritrack_farm_id=farm_id_int)
                 .select_related('project').first())
    if agri_farm is None:
        raise FarmNotFoundError(
            "No farm synced for farm_id %s -- sync it from AgriTrack first" % farm_id_int)
    return agri_farm.project


def _fetch_image_to_tempfile(image_url):
    """
    Download image_url from the remote-sense host to a temp file and return its
    path. Restricted to settings.REMOTE_SENSE_BASE_URL (SSRF guard) so this can
    never be pointed at internal services.
    """
    base = getattr(settings, 'REMOTE_SENSE_BASE_URL', None)
    if not base:
        raise RemoteSenseValidationError(
            "URL-pull mode is disabled (WO_REMOTE_SENSE_BASE_URL not set); "
            "upload the file directly instead")
    # Require a path boundary ("base/...") so "https://host" can't be bypassed by
    # "https://host.evil.com/..." (a bare startswith would match that).
    prefix = base.rstrip('/') + '/'
    if not image_url or not image_url.startswith(prefix):
        raise RemoteSenseValidationError(
            "image_url must be under the configured remote-sense base URL")

    api_key = getattr(settings, 'REMOTE_SENSE_INBOUND_API_KEY', None)
    headers = {'X-Api-Key': api_key} if api_key else {}
    fd, tmp_path = tempfile.mkstemp(suffix='.tif')
    try:
        with requests.get(image_url, headers=headers, stream=True, timeout=120) as r:
            r.raise_for_status()
            with os.fdopen(fd, 'wb') as out:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        out.write(chunk)
    except Exception:
        # Don't leak the temp file if the download failed part-way.
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    return tmp_path


def ingest_capture(farm_id, orthophoto_file=None, image_url=None,
                   name=None, capture_date=None, dispatch=True):
    """
    Resolve the farm and import a GeoTIFF as a Capture. Provide exactly one of
    orthophoto_file (a Django UploadedFile / file-like) or image_url (pull mode).

    :return: the created Task (Capture).
    """
    project = resolve_project(farm_id)

    if orthophoto_file is not None:
        ortho_source = orthophoto_file
        tmp_path = None
    elif image_url:
        ortho_source = tmp_path = _fetch_image_to_tempfile(image_url)
    else:
        raise RemoteSenseValidationError(
            "Provide either an 'orthophoto' file or an 'image_url'")

    try:
        task = create_capture_from_orthophoto(
            project, ortho_source,
            name=name or ('Sentinel capture (farm %s)' % farm_id),
            dispatch=dispatch, capture_date=capture_date,
            source=CaptureMeta.SATELLITE)
    finally:
        if image_url and tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    return task


def _iter_chunks(f):
    """Yield bytes from a Django UploadedFile (.chunks) or a plain file-like (.read)."""
    if hasattr(f, 'chunks'):
        for chunk in f.chunks():
            yield chunk
    elif hasattr(f, 'read'):
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            yield chunk
    else:
        raise RemoteSenseValidationError("uploaded band is not a readable file")


def ingest_capture_from_bands(farm_id, band_files, name=None, capture_date=None,
                              dispatch=True):
    """
    Stack several single-band Sentinel uploads into one tagged multi-band COG and
    import it as a Capture. Each file's name must contain its band token (B02,
    B03, B04, B08, ...); bands we don't use are ignored (see bands.py).

    :param band_files: iterable of Django UploadedFile / file-like objects.
    :return: the created Task (Capture).
    """
    project = resolve_project(farm_id)

    os.makedirs(settings.MEDIA_TMP, exist_ok=True)
    role_to_path = {}
    tmp_paths = []
    stacked_path = None
    try:
        for f in band_files:
            role = detect_band_role(getattr(f, 'name', None))
            if role is None:
                continue  # B01 aerosol, B11 SWIR, SCL, unrecognised names -> skip
            if role in role_to_path:
                raise RemoteSenseValidationError(
                    "duplicate band for role '%s' in upload" % role)
            fd, tmp_path = tempfile.mkstemp(suffix='.tif', dir=settings.MEDIA_TMP)
            with os.fdopen(fd, 'wb') as out:
                for chunk in _iter_chunks(f):
                    out.write(chunk)
            tmp_paths.append(tmp_path)
            role_to_path[role] = tmp_path

        if not role_to_path:
            raise RemoteSenseValidationError(
                "no recognised Sentinel bands in upload "
                "(expected B02/B03/B04/B08 in the filenames)")

        fd, stacked_path = tempfile.mkstemp(suffix='_stack.tif', dir=settings.MEDIA_TMP)
        os.close(fd)
        try:
            stack_and_tag_bands(role_to_path, stacked_path)
        except BandStackError as e:
            raise RemoteSenseValidationError(str(e))

        task = create_capture_from_orthophoto(
            project, stacked_path,
            name=name or ('Sentinel capture (farm %s)' % farm_id),
            dispatch=dispatch, capture_date=capture_date,
            source=CaptureMeta.SATELLITE)
    finally:
        for p in tmp_paths + ([stacked_path] if stacked_path else []):
            if p and os.path.exists(p):
                os.remove(p)

    return task
