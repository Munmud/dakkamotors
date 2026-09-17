"""The question API.

Thin on purpose: the rules are in `qa.py`.

**These endpoints must stay off `/api/cars/`.** That prefix is a separate CloudFront
behaviour that permits only GET, HEAD and OPTIONS and strips cookies before the request
reaches the origin - a POST there is refused by the CDN, with no Django log line to find,
while working perfectly against runserver. `/api/questions/` falls through to the
catch-all `/api/*` behaviour, which forwards cookies and allows every method.
"""

from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from . import identity
from . import qa
from .models import Car
from .store import questions as question_store


class PublicQuestionSerializer(serializers.Serializer):
    """What anyone may see.

    An explicit allowlist, and the test asserts the key set exactly. `customer` is absent
    and so is `customer_id`: an id is stable across pages, so publishing one would let a
    reader work out that the same person asked about four cars. That is attribution by
    another name, and the decision was that published pairs are anonymous.

    A plain Serializer rather than a ModelSerializer since questions moved to DynamoDB.
    The allowlist is now the whole definition rather than a filter over a model's fields,
    which if anything makes the anonymity easier to see.
    """

    # A string id since the move: sortable millisecond-plus-random rather than a
    # sequence. The client treats it as opaque.
    id = serializers.CharField(source="question_id", read_only=True)
    question = serializers.CharField(read_only=True)
    answer = serializers.CharField(read_only=True)
    # `language` is here so the app can show a visitor the pairs written in the
    # language they are reading. It says nothing about who asked.
    language = serializers.CharField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    answered_at = serializers.DateTimeField(read_only=True)


class MyQuestionSerializer(serializers.Serializer):
    """The asker's own thread, including the parts the public cannot see.

    `is_published` is here so the app can say "we have answered you" without implying the
    answer is on the page - answering and publishing are separate decisions.
    """

    id = serializers.CharField(source="question_id", read_only=True)
    question = serializers.CharField(read_only=True)
    answer = serializers.CharField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    answered_at = serializers.DateTimeField(read_only=True)
    is_published = serializers.BooleanField(read_only=True)
    # Denormalised onto the question, so the thread needs no car lookup at all.
    car_slug = serializers.CharField(read_only=True)
    car_label = serializers.CharField(read_only=True)


class AskThrottle(UserRateThrottle):
    """Keyed on the account, not the IP.

    AuthThrottle next door subclasses AnonRateThrottle, whose get_cache_key returns None
    for a signed-in request - on an IsAuthenticated view it would be decoration.
    """

    scope = "ask"


class QuestionListCreateView(APIView):
    """Ask about a car, and read your own thread for it."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [AskThrottle]

    def get(self, request):
        questions = question_store.for_customer(identity.sub_of(request.user), limit=50)
        slug = request.query_params.get("car")
        if slug:
            questions = [q for q in questions if q.car_slug == slug]
        return Response(
            {"results": MyQuestionSerializer(questions, many=True).data}
        )

    def post(self, request):
        car = Car.objects.filter(slug=request.data.get("car") or "").first()
        if car is None:
            return Response(
                {"detail": "We could not find that car."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            question = qa.ask(
                user=request.user,
                car=car,
                question=request.data.get("question"),
                language=request.data.get("language") or "en",
            )
        except qa.QuestionError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            MyQuestionSerializer(question).data, status=status.HTTP_201_CREATED
        )
