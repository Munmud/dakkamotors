"""The booking API.

Thin on purpose: every rule lives in `booking.py`, so the same checks apply whether a
request arrives from the app, from curl, or from a test.
"""

from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import booking as rules
from .store import cars as car_store


class SlotSerializer(serializers.Serializer):
    """Plain, not a ModelSerializer: slots live in DynamoDB now.

    `id` is a string -- the slot id is derived from (schedule, start time) rather than
    being a sequence, which is what makes materialising slots idempotent. The client
    treats it as opaque and hands it straight back, so the change is invisible to it.
    """

    id = serializers.CharField(source="slot_id", read_only=True)
    starts_at = serializers.DateTimeField(read_only=True)
    ends_at = serializers.DateTimeField(read_only=True)
    capacity = serializers.IntegerField(read_only=True)
    seats_left = serializers.IntegerField(read_only=True)


class BookingSerializer(serializers.Serializer):
    id = serializers.CharField(source="booking_id", read_only=True)
    status = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    # Snapshotted onto the booking rather than read through the slot, so a booking still
    # renders correctly after the slot it pointed at is gone.
    starts_at = serializers.DateTimeField(source="slot_starts_at", read_only=True)
    ends_at = serializers.DateTimeField(source="slot_ends_at", read_only=True)
    slot = serializers.CharField(source="slot_id", read_only=True)
    car_label = serializers.CharField(read_only=True)
    car_slug = serializers.CharField(read_only=True)


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
            car = car_store.by_slug(car_slug)
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
