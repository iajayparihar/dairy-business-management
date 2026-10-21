import json

from django.http import JsonResponse


def _has_nul(value):
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_nul(v) for v in value)
    return False


class RejectNullBytesMiddleware:
    """
    PostgreSQL text cannot hold a NUL character, so any query value or JSON
    string containing one would crash the request with a 500. Reject it
    up front with a 400.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        bad = _has_nul(dict(request.GET.lists()))
        if not bad and request.content_type == "application/json" and request.body:
            try:
                bad = _has_nul(json.loads(request.body))
            except ValueError:
                pass  # malformed JSON is the view's 400 to give
            except RecursionError:
                bad = True  # absurdly nested JSON would crash every parser downstream
        if bad:
            return JsonResponse(
                {"detail": "Null characters or excessive nesting are not allowed."}, status=400
            )
        return self.get_response(request)
