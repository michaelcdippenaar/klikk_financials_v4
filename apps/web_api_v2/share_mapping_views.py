"""The V2 share-mapping command.

The first V2 write after the ingest run command, and it follows the same
shape: its own capability, a typed refusal rather than a guess, and a
correlation id on every response.
"""
import logging

from rest_framework import status
from rest_framework.response import Response

from apps.web_api_v2.api_errors import error_response
from apps.web_api_v2.ingest_views import V2IngestView
from apps.web_api_v2.services.entity_access import (
    MANAGE_SHARE_MAPPINGS_CAPABILITY,
    EntityAccessDenied,
    EntityCapabilityDenied,
    require_entity_capability,
)
from apps.web_api_v2.services.share_mapping import (
    ShareMappingError,
    map_share_name,
    mappable_share_codes,
)

logger = logging.getLogger(__name__)

ERROR_STATUS = {
    'VALIDATION_ERROR': status.HTTP_400_BAD_REQUEST,
    'NOT_BOUND': status.HTTP_400_BAD_REQUEST,
    'NOT_FOUND': status.HTTP_404_NOT_FOUND,
    'CONFLICT': status.HTTP_409_CONFLICT,
}


class ShareMappingView(V2IngestView):
    """GET the codes a name may be attached to; POST to attach one."""

    def _authorize(self, request, entity_id):
        try:
            return require_entity_capability(
                request.user, entity_id, MANAGE_SHARE_MAPPINGS_CAPABILITY,
            )
        except EntityAccessDenied:
            return error_response(
                request, 'FORBIDDEN_ENTITY', 'You do not have access to this entity.',
                status.HTTP_403_FORBIDDEN,
            )
        except EntityCapabilityDenied:
            return error_response(
                request, 'CAPABILITY_REQUIRED',
                'MANAGE_SHARE_MAPPINGS capability is required to change how a share '
                'name is attributed.',
                status.HTTP_403_FORBIDDEN,
            )

    def get(self, request, entity_id):
        membership = self._authorize(request, entity_id)
        if isinstance(membership, Response):
            return membership
        return Response({'shareCodes': mappable_share_codes(membership.entity.pk)})

    def post(self, request, entity_id):
        membership = self._authorize(request, entity_id)
        if isinstance(membership, Response):
            return membership
        if not isinstance(request.data, dict):
            return error_response(
                request, 'VALIDATION_ERROR', 'Request body must be an object.',
                status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = map_share_name(
                membership.entity.pk,
                request.data.get('shareName'),
                request.data.get('shareCode'),
                actor=request.user,
                note=str(request.data.get('note') or '')[:500],
            )
        except ShareMappingError as exc:
            return error_response(
                request, exc.code, exc.safe_message,
                ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
            )

        # 200 either way: a name already mapped is not an error, and the body
        # says whether anything changed rather than leaving the caller to infer
        # it from a status code.
        return Response(result, status=status.HTTP_200_OK)
