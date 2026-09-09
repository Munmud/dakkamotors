from rest_framework import serializers

from .models import Car, CarImage


class CarImageSerializer(serializers.ModelSerializer):
    image = serializers.ImageField(use_url=True)

    class Meta:
        model = CarImage
        fields = ["id", "image", "is_primary", "order"]


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
            "created_at",
            "updated_at",
        ]
