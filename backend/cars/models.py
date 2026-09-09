from django.db import models


class FuelType(models.TextChoices):
    PETROL = "petrol", "Petrol"
    DIESEL = "diesel", "Diesel"
    HYBRID = "hybrid", "Hybrid"
    ELECTRIC = "electric", "Electric"
    LPG = "lpg", "LPG"


class CarStatus(models.TextChoices):
    AVAILABLE = "available", "Available"
    RESERVED = "reserved", "Reserved"
    SOLD = "sold", "Sold"


class Car(models.Model):
    brand = models.CharField(max_length=60)
    grade = models.CharField(max_length=60, blank=True)
    model_name = models.CharField(max_length=80)
    model_code = models.CharField(max_length=40, blank=True)
    chassis_number = models.CharField(max_length=40, unique=True)
    manufacture_year = models.PositiveIntegerField()
    fuel_type = models.CharField(
        max_length=10, choices=FuelType.choices, default=FuelType.PETROL
    )
    seat_capacity = models.PositiveSmallIntegerField(default=5)
    color = models.CharField(max_length=40, blank=True)

    # Null means "call for price" — deliberately distinct from a price of zero.
    price_jpy = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Leave blank to display 'Call for price' instead of an amount.",
    )

    status = models.CharField(
        max_length=10, choices=CarStatus.choices, default=CarStatus.AVAILABLE
    )

    description_en = models.TextField(blank=True)
    description_ja = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.manufacture_year} {self.brand} {self.model_name}".strip()

    @property
    def primary_image(self):
        """The image to show on a listing card.

        Prefers an image explicitly flagged as primary, otherwise falls back to the
        first by display order. Uses the prefetched `images` relation when available so
        list serialisation stays at one query.
        """
        images = list(self.images.all())
        if not images:
            return None
        for image in images:
            if image.is_primary:
                return image
        return images[0]


class CarImage(models.Model):
    car = models.ForeignKey(Car, related_name="images", on_delete=models.CASCADE)
    image = models.ImageField(upload_to="cars/")
    is_primary = models.BooleanField(
        default=False, help_text="Shown on the listing card. Only the first one counts."
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"Image {self.pk} for {self.car}"
