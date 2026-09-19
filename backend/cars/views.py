from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import StaffCookieAuthentication
from .choices import CarStatus
from .serializers import CarDetailSerializer, CarListSerializer
from .store import cars as car_store
from .store.errors import NotFound
from .uploads import UploadRejected, build_presigned_upload


class CarListView(APIView):
    """Public, read-only inventory.

    A plain APIView rather than a ModelViewSet: there is no queryset to build on, and
    the router's conventions were doing nothing here that two explicit views do not.

    Read whole and sliced in Python rather than paginated with a cursor. The inventory
    is tens of cars, so the whole listing is one Query on a single 1 MB page, and
    keeping `?page=N` with an accurate `count` means the React app needs no change at
    all. The ceiling is a few thousand cars, at which point a status partition starts
    to paginate and this should move to LastEvaluatedKey -- not before.
    """

    page_size = 12

    def get(self, request):
        cars = car_store.list_by_status(CarStatus.AVAILABLE)

        try:
            page = max(int(request.query_params.get("page") or 1), 1)
        except (TypeError, ValueError):
            page = 1

        start = (page - 1) * self.page_size
        window = cars[start:start + self.page_size]
        if not window and page > 1:
            return Response({"detail": "Invalid page."},
                            status=status.HTTP_404_NOT_FOUND)

        base = request.build_absolute_uri(request.path)
        return Response({
            "count": len(cars),
            "next": f"{base}?page={page + 1}" if start + self.page_size < len(cars) else None,
            "previous": f"{base}?page={page - 1}" if page > 1 else None,
            "results": CarListSerializer(window, many=True,
                                         context={"request": request}).data,
        })


class CarDetailView(APIView):
    """One car, its photos and its published questions.

    Retrieval is deliberately not restricted to available cars: someone sent a link to
    a car that has since been reserved should still see the page, with its status shown,
    rather than a 404.

    Readable URLs: /api/cars/2008-daihatsu-tanto-x/. Numeric ids still resolve, so links
    shared before slugs existed keep working.
    """

    def get(self, request, slug):
        car = _lookup(slug)
        if car is None:
            return Response({"detail": "Not found."},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(
            CarDetailSerializer(car, context={"request": request}).data
        )


def _lookup(slug):
    """By slug, falling back to the legacy numeric id."""
    try:
        return car_store.detail_by_slug(slug)
    except NotFound:
        pass

    if str(slug).isdigit():
        try:
            return car_store.detail(str(slug))
        except NotFound:
            return None
    return None


class SignUploadView(APIView):
    """Hand the staff pages a short-lived permission to put one file into S3.

    Staff only: signing is effectively granting write access to the media bucket, so it
    must never be reachable by the public read-only API's AllowAny default.

    `authentication_classes` **replaces** the project default rather than extending it,
    and that is the whole point. The default is `CognitoCookieAuthentication`, which reads
    the customer cookie -- so a staff member who happens to also hold a customer session
    authenticates as a customer here, and because their customer token still carries
    `cognito:groups` containing `staff`, `IsAdminUser` passes. It looks like it works.
    Everyone without a customer session gets a 403 and silently falls back to posting
    through Lambda. Leaving the default in the list would preserve exactly that.
    """

    authentication_classes = [StaffCookieAuthentication]
    permission_classes = [IsAdminUser]

    def post(self, request):
        try:
            size = int(request.data.get("size") or 0)
        except (TypeError, ValueError):
            return Response(
                {"detail": "Invalid file size."}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            payload = build_presigned_upload(
                kind=request.data.get("kind"),
                content_type=request.data.get("content_type"),
                size=size,
            )
        except UploadRejected as exc:
            # The message is written for the person uploading, so surface it verbatim.
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(payload)
