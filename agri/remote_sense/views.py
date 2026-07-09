import hmac

from django.utils.dateparse import parse_date
from rest_framework import status, permissions, parsers
from rest_framework.response import Response
from rest_framework.views import APIView

from webodm import settings
from .ingest import (ingest_capture, RemoteSenseValidationError,
                     FarmNotFoundError)


def _valid_api_key(provided):
    expected = getattr(settings, 'REMOTE_SENSE_INBOUND_API_KEY', None)
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(provided), str(expected))


class RemoteSensePushView(APIView):
    """
    POST /api/v1/remote-sense/push

    Inbound endpoint for the remote-sensing (Sentinel) system to deliver a
    georeferenced GeoTIFF for a farm. Server-to-server auth via a shared
    X-Api-Key header (WO_REMOTE_SENSE_INBOUND_API_KEY) -- not WebODM session/JWT.

    Body (multipart push, primary):
        farm_id       (required) AgriTrack farm id the image belongs to
        orthophoto    (required) the .tif file
        capture_date  (optional) acquisition date YYYY-MM-DD (seasonal x-axis)
        name          (optional) capture name

    Body (JSON pull, alternative -- requires WO_REMOTE_SENSE_BASE_URL):
        {"farm_id": ..., "image_url": "https://<remote-sense-host>/...tif",
         "capture_date": "...", "name": "..."}

    On success the GeoTIFF is imported as a Capture on the farm's Project, its
    fields are seeded from the farm's AgriTrack boundaries, and processing is
    dispatched (extent/band detection -> COMPLETED, ready for review + analysis).
    """
    permission_classes = [permissions.AllowAny]
    parser_classes = [parsers.MultiPartParser, parsers.FormParser, parsers.JSONParser]

    def post(self, request):
        api_key = request.META.get('HTTP_X_API_KEY')
        if not _valid_api_key(api_key):
            return Response({'status': 'error', 'message': 'missing or invalid API key'},
                            status=status.HTTP_401_UNAUTHORIZED)

        capture_date_raw = request.data.get('capture_date')
        capture_date = parse_date(capture_date_raw) if capture_date_raw else None

        try:
            task = ingest_capture(
                farm_id=request.data.get('farm_id'),
                orthophoto_file=request.FILES.get('orthophoto'),
                image_url=request.data.get('image_url'),
                name=request.data.get('name'),
                capture_date=capture_date)
        except FarmNotFoundError as e:
            return Response({'status': 'error', 'message': str(e)},
                            status=status.HTTP_404_NOT_FOUND)
        except RemoteSenseValidationError as e:
            return Response({'status': 'error', 'message': str(e)},
                            status=status.HTTP_400_BAD_REQUEST)

        return Response({'status': 'ok', 'captureId': str(task.id),
                         'projectId': task.project_id, 'farmId': request.data.get('farm_id'),
                         'captureStatus': task.status},
                        status=status.HTTP_201_CREATED)
