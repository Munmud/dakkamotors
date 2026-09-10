"""A customer's bell.

Under `/api/*`, so caching is disabled and cookies are forwarded - both required, since
every response here is specific to one signed-in person.

Nothing polls these. Aurora is set to scale to zero, and a bell checking every thirty
seconds would keep it awake around the clock for a site that gets a handful of bookings a
week. The client fetches once when a signed-in session starts, and again after anything
that could have created a notification.
"""

from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import notifications


class NotificationSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    kind = serializers.CharField(read_only=True)
    context = serializers.JSONField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    read_at = serializers.DateTimeField(read_only=True)


class NotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = notifications.recent_for(request.user)
        # `unread` rides along with the list rather than living at its own endpoint: this
        # is fetched on every signed-in page load, and a second uncached round trip to
        # count a number is the whole cost of the feature doubled.
        return Response({
            "results": NotificationSerializer(rows, many=True).data,
            "unread": notifications.unread_count(request.user),
        })


class NotificationReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        """Mark everything read, or just the ids given.

        Scoped to the requester inside `mark_read`, so another customer's ids match
        nothing rather than marking their notifications read.
        """
        ids = request.data.get("ids")
        if ids is not None and not isinstance(ids, list):
            return Response(
                {"detail": "Expected a list of notification ids."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"unread": notifications.mark_read(request.user, ids)})
