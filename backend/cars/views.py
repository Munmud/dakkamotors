from rest_framework import viewsets

from .models import Car, CarStatus
from .serializers import CarDetailSerializer, CarListSerializer


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
