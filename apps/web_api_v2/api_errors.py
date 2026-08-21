import uuid

from rest_framework.response import Response


def request_correlation_id(request):
    value = getattr(request, 'web_api_v2_correlation_id', None)
    if value is None:
        value = str(uuid.uuid4())
        request.web_api_v2_correlation_id = value
    return value


def error_response(request, code, message, http_status, *, retryable=False, details=None):
    correlation_id = request_correlation_id(request)
    error = {
        'code': code,
        'message': message,
        'retryable': bool(retryable),
        'correlationId': correlation_id,
    }
    if details:
        error['details'] = details
    response = Response({'error': error}, status=http_status)
    response['X-Correlation-ID'] = correlation_id
    return response
