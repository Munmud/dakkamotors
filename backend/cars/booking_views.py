"""The booking API.

Thin on purpose: every rule lives in `booking.py`, so the same checks apply whether a
request arrives from the app, from curl, or from a test.
"""

from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import booking as rules
from .booking_models import TestDriveSlot
from .models import Car


class SlotSerializer(serializers.ModelSerializer):
    seats_left = serializers.IntegerField(read_only=True)

    class Meta:
        model = TestDriveSlot
        fields = ["id", "starts_at", "ends_at", "capacity", "seats_left"]


class BookingSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)
    starts_at = serializers.DateTimeField(source="slot.starts_at", read_only=True)
    ends_at = serializers.DateTimeField(source="slot.ends_at", read_only=True)
    slot = serializers.IntegerField(source="slot_id", read_only=True)
    car_label = serializers.CharField(read_only=True)
    car_slug = serializers.SerializerMethodField()

    def get_car_slug(self, obj):
        return obj.car.slug if obj.car else None


class SlotListView(APIView):
    """Availability.

    Open to anonymous visitors on purpose: making someone register before they can see
    whether any time suits them is a good way to lose them.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        # Generated here rather than on a timer - see booking.ensure_slots.
        rules.ensure_slots()

        slots = [s for s in rules.bookable_slots() if s.seats_left > 0]
        return Response({"results": SlotSerializer(slots, many=True).data})


class BookingListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        bookings = rules.active_bookings_for(request.user)
        return Response({"results": BookingSerializer(bookings, many=True).data})

    def post(self, request):
        car = None
        car_slug = request.data.get("car")
        if car_slug:
            car = Car.objects.filter(slug=car_slug).first()
            if car is None:
                return Response(
                    {"detail": "That car is no longer listed."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            booking = rules.create_booking(
                user=request.user, slot_id=request.data.get("slot"), car=car
            )
        except rules.BookingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            BookingSerializer(booking).data, status=status.HTTP_201_CREATED
        )


class BookingCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            booking = rules.cancel_booking(user=request.user, booking_id=pk)
        except rules.BookingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BookingSerializer(booking).data)


class BookingRescheduleView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            booking = rules.reschedule_booking(
                user=request.user, booking_id=pk, slot_id=request.data.get("slot")
            )
        except rules.BookingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BookingSerializer(booking).data)
