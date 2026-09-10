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

from . import qa
from .models import Car
from .qa_models import CarQuestion


class PublicQuestionSerializer(serializers.ModelSerializer):
    """What anyone may see.

    An explicit allowlist, and the test asserts the key set exactly. `customer` is absent
    and so is `customer_id`: an id is stable across pages, so publishing one would let a
    reader work out that the same person asked about four cars. That is attribution by
    another name, and the decision was that published pairs are anonymous.
    """

    class Meta:
        model = CarQuestion
        # `language` is here so the app can show a visitor the pairs written in the
        # language they are reading. It says nothing about who asked.
        fields = ["id", "question", "answer", "language", "created_at", "answered_at"]
        read_only_fields = fields


class MyQuestionSerializer(serializers.ModelSerializer):
    """The asker's own thread, including the parts the public cannot see.

    `is_published` is here so the app can say "we have answered you" without implying the
    answer is on the page - answering and publishing are separate decisions.
    """

    car_slug = serializers.CharField(source="car.slug", read_only=True)
    car_label = serializers.SerializerMethodField()

    class Meta:
        model = CarQuestion
        fields = ["id", "question", "answer", "created_at", "answered_at",
                  "is_published", "car_slug", "car_label"]
        read_only_fields = fields

    def get_car_label(self, obj):
        return str(obj.car)


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
        questions = CarQuestion.objects.filter(customer=request.user).select_related("car")
        slug = request.query_params.get("car")
        if slug:
            questions = questions.filter(car__slug=slug)
        return Response(
            {"results": MyQuestionSerializer(questions[:50], many=True).data}
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
