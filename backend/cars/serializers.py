from rest_framework import serializers

from .models import Car, CarImage


class CarImageSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(use_url=True)
    sources = serializers.SerializerMethodField()

    class Meta:
        model = CarImage
        fields = ["id", "image", "sources", "is_primary", "order"]

    def get_sources(self, obj):
        """{width: webp url}, or null while the copies are still being generated.

        Returning null rather than guessed URLs matters: resizing happens in a separate
        asynchronous invocation, and a <source> pointing at an object that does not
        exist yet renders as a broken image instead of falling back to the original.
        """
        urls = obj.derivative_urls
        return {str(width): url for width, url in sorted(urls.items())} or None


class CarListSerializer(serializers.ModelSerializer):
    """Fields needed to render a card on the home page — nothing more."""

    primary_image = serializers.SerializerMethodField()

    class Meta:
        model = Car
        fields = [
            "id",
            "brand",
            "grade",
            "model_name",
            "manufacture_year",
            "price_jpy",
            "status",
            "primary_image",
        ]

    def get_primary_image(self, obj):
        image = obj.primary_image
        if image is None:
            return None
        return CarImageSerializer(image, context=self.context).data


class CarDetailSerializer(serializers.ModelSerializer):
    images = CarImageSerializer(many=True, read_only=True)
    fuel_type_display = serializers.CharField(source="get_fuel_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    video = serializers.SerializerMethodField()

    def get_video(self, obj):
        return obj.video.url if obj.video else None

    class Meta:
        model = Car
        fields = [
            "id",
            "brand",
            "grade",
            "model_name",
            "model_code",
            "chassis_number",
            "manufacture_year",
            "fuel_type",
            "fuel_type_display",
            "seat_capacity",
            "color",
            "price_jpy",
            "status",
            "status_display",
            "description_en",
            "description_ja",
            "images",
            "video",
            "created_at",
            "updated_at",
        ]
