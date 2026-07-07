import hmac

from rest_framework import status, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from webodm import settings
from .sync import sync_farm_payload, SyncValidationError


def _valid_api_key(provided):
    expected = getattr(settings, 'AGRITRACK_INBOUND_API_KEY', None)
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(provided), str(expected))


class MobileSyncView(APIView):
    """
    POST /api/v1/mobile/sync

    The exact inbound endpoint the AgriTrack integration contract (§6.1)
    specifies external systems must implement. Authenticated via a shared
    X-Api-Key header (WO_AGRITRACK_INBOUND_API_KEY) -- not Django session/JWT
    auth, since the caller is a server-to-server integration, not a WebODM user.
    """
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        api_key = request.META.get('HTTP_X_API_KEY')
        if not _valid_api_key(api_key):
            return Response({'status': 'error', 'message': 'missing or invalid API key'},
                            status=status.HTTP_401_UNAUTHORIZED)

        try:
            agri_farm = sync_farm_payload(request.data)
        except SyncValidationError as e:
            return Response({'status': 'error', 'message': str(e)},
                            status=status.HTTP_400_BAD_REQUEST)

        return Response({'status': 'ok', 'farmId': agri_farm.agritrack_farm_id,
                         'projectId': agri_farm.project_id}, status=status.HTTP_200_OK)
