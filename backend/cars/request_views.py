"""Ask the shop to find you a car.

Lives at /api/requests/, not under /api/cars/: CloudFront serves that prefix GET-only
and strips cookies, so a POST there is refused at the edge with no Django log line.

Open to anyone. `AllowAny` with the same anonymous throttle registration uses, which is
what keeps a form with no sign-in from being a free mailbox for the staff address:
twenty submissions an hour from one address is plenty for a person and useless to a
script. A signed-in customer is recognised by the cookie and the domain module copies
their details from Cognito; a guest supplies them.
"""

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import identity, requests
from .auth_views import AuthThrottle


class CarRequestCreateView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [AuthThrottle]

    def post(self, request):
        user = request.user if getattr(request.user, "is_authenticated", False) else None
        try:
            record = requests.submit(
                user=user,
                name=request.data.get("name") or "",
                email=request.data.get("email") or "",
                phone=request.data.get("phone") or "",
                details=request.data.get("details") or "",
                language=request.data.get("language") or "en",
            )
        except requests.RequestError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "id": record.request_id,
                "name": record.name,
                "email": record.email,
                "signed_in": bool(identity.sub_of(user)) if user else False,
            },
            status=status.HTTP_201_CREATED,
        )
