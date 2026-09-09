import posixpath

from django.core.files.storage import default_storage
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver

# Widths generated for every uploaded photo. 320 covers gallery thumbnails, 800 the
# listing cards, 1600 the gallery's main image on a high-density screen.
DERIVATIVE_WIDTHS = (320, 800, 1600)


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

    # A single walkaround clip. Served exactly as uploaded - there is no transcoding
    # step - so uploads are restricted to MP4/H.264, the one combination every browser
    # can play. The page loads it with preload="none", so it costs nothing until the
    # visitor presses play.
    video = models.FileField(
        upload_to="cars/video/",
        blank=True,
        help_text="Optional MP4 walkaround. Nothing downloads until a visitor presses play.",
    )
    video_uploaded_at = models.DateTimeField(null=True, blank=True, editable=False)

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

    # Resizing happens in a separate asynchronous Lambda invocation, so there is a
    # window where the original exists but the WebP copies do not. Until this flips,
    # the API serves the original rather than a URL that would 404.
    derivatives_ready = models.BooleanField(default=False, editable=False)
    derivative_widths = models.CharField(
        max_length=64,
        blank=True,
        editable=False,
        help_text="Widths actually generated. Narrower than DERIVATIVE_WIDTHS when the "
        "original was too small to produce them all.",
    )

    class Meta:
        ordering = ["order", "id"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Remembered so save() can tell when the photo itself was replaced, as opposed
        # to someone just reordering it or ticking is_primary.
        self._original_image_name = self.image.name if self.image else None

    def __str__(self):
        return f"Image {self.pk} for {self.car}"

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        writing_derivatives = update_fields is not None and set(update_fields) <= {
            "derivative_widths",
            "derivatives_ready",
        }

        replaced = self.image and self.image.name != self._original_image_name
        if replaced and not writing_derivatives:
            # Stale copies describe the previous photo; stop serving them immediately.
            self.derivatives_ready = False
            self.derivative_widths = ""

        super().save(*args, **kwargs)
        self._original_image_name = self.image.name if self.image else None

        # Guard against re-queuing from inside the builder's own save().
        if writing_derivatives or not self.image or self.derivatives_ready:
            return

        from .tasks import build_derivatives_task

        build_derivatives_task(self.pk)

    @property
    def available_widths(self):
        if not self.derivatives_ready or not self.derivative_widths:
            return []
        return [int(w) for w in self.derivative_widths.split(",") if w.strip().isdigit()]

    def derivative_name(self, width):
        """Storage name of one WebP copy, derived from the original's own name.

        Built from the full name rather than just its folder. Photos added through the
        ordinary Django upload path all land directly in ``cars/``, so a folder-based
        name would give every one of them the same ``cars/w800.webp`` and each upload
        would silently overwrite the last one's copies.
        """
        stem, _ = posixpath.splitext(self.image.name)
        return f"{stem}__w{width}.webp"

    @property
    def derivative_urls(self):
        """{width: url} for the copies that exist, or {} while none do."""
        storage = self.image.storage or default_storage
        return {w: storage.url(self.derivative_name(w)) for w in self.available_widths}


@receiver(post_delete, sender=CarImage)
def _delete_image_files(sender, instance, **kwargs):
    """Remove the original and its resized copies when a photo is deleted.

    Django deliberately leaves files behind on delete, which is the right default when
    files might be shared - but each CarImage owns its upload outright, so without this
    every sold-and-removed listing would leave several megabytes in the bucket forever.
    """
    if not instance.image:
        return

    storage = instance.image.storage
    names = [instance.image.name] + [
        instance.derivative_name(width) for width in instance.available_widths
    ]
    for name in names:
        try:
            storage.delete(name)
        except Exception:  # noqa: BLE001 - never let cleanup break the delete itself
            pass


@receiver(post_delete, sender=Car)
def _delete_video_file(sender, instance, **kwargs):
    if not instance.video:
        return
    try:
        instance.video.storage.delete(instance.video.name)
    except Exception:  # noqa: BLE001
        pass
