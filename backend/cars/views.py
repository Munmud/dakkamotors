from rest_framework import status, viewsets
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Car, CarStatus
from .serializers import CarDetailSerializer, CarListSerializer
from .uploads import UploadRejected, build_presigned_upload


class CarViewSet(viewsets.ReadOnlyModelViewSet):
    """Public, read-only inventory.

    The listing is restricted to available cars, but retrieval is not: a customer who
    was sent a link to a car that has since been reserved should still see the page
    (with its status shown) rather than a 404.
    """

    def get_queryset(self):
        queryset = Car.objects.prefetch_related("images")
        if self.action == "list":
            queryset = queryset.filter(status=CarStatus.AVAILABLE)
        return queryset

    def get_serializer_class(self):
        if self.action == "list":
            return CarListSerializer
        return CarDetailSerializer


class SignUploadView(APIView):
    """Hand the admin a short-lived permission to put one file into S3.

    Staff only: signing is effectively granting write access to the media bucket, so it
    must never be reachable by the public read-only API's AllowAny default.
    """

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
