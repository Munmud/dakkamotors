"""The site's one write: somebody leaving their number.

Lives at /api/requests/, not under /api/cars/: CloudFront serves that prefix GET-only
and strips cookies, so a POST there is refused at the edge with no Django log line —
while working fine locally, which is the worst way for it to fail.

The car, when there is one, travels in the body as a slug for the same reason.

Open to anyone; there are no accounts. `AllowAny` with an anonymous throttle is what
keeps a form with no sign-in from being a free mailbox for the staff address: twenty
submissions an hour from one address is plenty for a person and useless to a script.
"""

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from . import requests
from .store import cars as car_store


class LeadThrottle(AnonRateThrottle):
    """Twenty an hour from one address.

    The `auth` scope kept its name through the removal of the accounts it was named
    for: renaming it would mean editing `settings.REST_FRAMEWORK` in the same breath,
    and the number is the point, not the word.
    """

    scope = "auth"


class LeadCreateView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [LeadThrottle]

    def post(self, request):
        # Read first: every refusal below is written in it. The car page is edge-cached
        # for fifteen minutes, so "we could not find that car" is a sentence a real
        # visitor can meet after filling the form, not only a curl typo.
        language = request.data.get("language") or "en"

        car = None
        slug = request.data.get("car")
        if slug:
            car = car_store.by_slug(slug)
            if car is None:
                return Response(
                    {"detail": requests.message("no_such_car", language)},
                    status=status.HTTP_400_BAD_REQUEST)

        try:
            record = requests.submit(
                name=request.data.get("name") or "",
                email=request.data.get("email") or "",
                phone=request.data.get("phone") or "",
                details=request.data.get("details") or "",
                car=car,
                language=language,
            )
        except requests.RequestError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {"id": record.request_id, "name": record.name, "car": record.car_slug},
            status=status.HTTP_201_CREATED,
        )
