"""The one endpoint left from customer accounts.

Everything else under `/api/auth/` went when accounts did. This survives because the
site still has a write — the lead form — and a guest posting it needs a CSRF token from
somewhere.

It cannot come from the rendered page: those are cached at the CDN for five minutes with
cookies ignored, so a token set there would be handed to every visitor alike, which is
exactly what CSRF protection exists to prevent. Everything under `/api/` is uncached and
forwards cookies, so the token is fetched from here on demand.

CSRF needs no database and no session. With `CSRF_USE_SESSIONS = False` the token is a
cookie-plus-secret construction, which is why it kept working when
`django.contrib.sessions` left and why it keeps working now.
"""

from django.middleware.csrf import get_token
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


class CsrfView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"csrfToken": get_token(request)})
