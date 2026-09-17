"""What the public API returns.

Plain `Serializer`s rather than `ModelSerializer`s since cars moved to DynamoDB: there is
no model to map. The field lists are unchanged, and are worth keeping that way -- the
tests assert some of them as exact key sets, which is what stops a field being added to a
public payload by accident.
"""

from rest_framework import serializers


class CarImageSerializer(serializers.Serializer):
    id = serializers.CharField(source="image_id", read_only=True)
    image = serializers.CharField(source="url", read_only=True)
    sources = serializers.SerializerMethodField()
    is_primary = serializers.BooleanField(read_only=True)
    order = serializers.IntegerField(read_only=True)

    def get_sources(self, obj):
        """{width: webp url}, or null while the copies are still being generated.

        Returning null rather than guessed URLs matters: resizing happens in a separate
        asynchronous invocation, and a <source> pointing at an object that does not
        exist yet renders as a broken image instead of falling back to the original.
        """
        urls = obj.derivative_urls
        return {str(width): url for width, url in sorted(urls.items())} or None


class CarListSerializer(serializers.Serializer):
    """Fields needed to render a card on the home page — nothing more."""

    id = serializers.CharField(source="car_id", read_only=True)
    slug = serializers.CharField(read_only=True)
    brand = serializers.CharField(read_only=True)
    grade = serializers.CharField(read_only=True)
    model_name = serializers.CharField(read_only=True)
    manufacture_year = serializers.IntegerField(read_only=True)
    price_jpy = serializers.IntegerField(read_only=True, allow_null=True)
    status = serializers.CharField(read_only=True)
    primary_image = serializers.SerializerMethodField()

    def get_primary_image(self, obj):
        """Read from the denormalised reference on the car itself.

        A listing page is one query because of this. Following the images would be an
        N+1 that `prefetch_related` used to absorb and that has no equivalent here.
        """
        image = obj.primary_image
        if image is None:
            return None
        return CarImageSerializer(image, context=self.context).data


class CarDetailSerializer(serializers.Serializer):
    id = serializers.CharField(source="car_id", read_only=True)
    slug = serializers.CharField(read_only=True)
    brand = serializers.CharField(read_only=True)
    grade = serializers.CharField(read_only=True)
    model_name = serializers.CharField(read_only=True)
    model_code = serializers.CharField(read_only=True)
    chassis_number = serializers.CharField(read_only=True)
    manufacture_year = serializers.IntegerField(read_only=True)
    fuel_type = serializers.CharField(read_only=True)
    fuel_type_display = serializers.CharField(source="get_fuel_type_display",
                                              read_only=True)
    seat_capacity = serializers.IntegerField(read_only=True)
    color = serializers.CharField(read_only=True)
    price_jpy = serializers.IntegerField(read_only=True, allow_null=True)
    status = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    description_en = serializers.CharField(read_only=True)
    description_ja = serializers.CharField(read_only=True)
    images = CarImageSerializer(many=True, read_only=True)
    video = serializers.SerializerMethodField()
    questions = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    def get_video(self, obj):
        from django.core.files.storage import default_storage

        return default_storage.url(obj.video_name) if obj.video_name else None

    def get_questions(self, obj):
        """Published pairs, anonymously.

        `store.cars.detail()` carried the questions back in the same Query as the car,
        so `published_for` reads them off the object rather than issuing anything. That
        used to be a `Prefetch` a caller had to remember; it is structural now, because
        a question lives in its car's own partition.

        The car page is also server-rendered with this payload embedded, but the app
        fetches it fresh whenever someone arrives by an in-app link rather than landing
        directly - so it has to be in the API as well, or the section would appear on a
        hard refresh and vanish on navigation.
        """
        from .qa import published_for
        from .qa_views import PublicQuestionSerializer

        return PublicQuestionSerializer(published_for(obj), many=True).data
