from django.http import JsonResponse


def graphql_error_response(message, code, status, correlation_id):
    extensions = {'code': code, 'correlationId': correlation_id}
    if code == 'TEMPORARILY_UNAVAILABLE':
        extensions['retryable'] = True
    response = JsonResponse(
        {
            'errors': [
                {
                    'message': message,
                    'extensions': extensions,
                },
            ],
        },
        status=status,
    )
    response['X-Correlation-ID'] = correlation_id
    return response
