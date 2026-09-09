from django.contrib import admin, messages
from django.utils import timezone
from django.utils.html import format_html

from .forms import CarAdminForm, CarImageForm
from .images import build_derivatives
from .tasks import process_pending
from .models import Car, CarImage


class CarImageInline(admin.TabularInline):
    model = CarImage
    form = CarImageForm
    extra = 3
    fields = ("image", "preview", "is_primary", "order")
    readonly_fields = ("preview",)

    @admin.display(description="Preview")
    def preview(self, obj):
        if not obj.image:
            return "—"
        # Prefer the smallest generated copy; the original can be several megabytes and
        # the admin renders it at 70px tall.
        urls = obj.derivative_urls
        src = urls[min(urls)] if urls else obj.image.url
        note = "" if obj.derivatives_ready else " (optimising…)"
        return format_html(
            '<img src="{}" style="height:70px;border-radius:4px" />{}', src, note
        )


@admin.register(Car)
class CarAdmin(admin.ModelAdmin):
    form = CarAdminForm
    inlines = [CarImageInline]

    class Media:
        # Sends photos and video straight to S3, sidestepping the ~4.5 MB ceiling that
        # uploading through Lambda imposes.
        js = ("cars/direct-upload.js",)
        css = {"all": ("cars/direct-upload.css",)}
    list_display = (
        "manufacture_year",
        "brand",
        "model_name",
        "grade",
        "display_price",
        "status",
    )
    list_display_links = ("brand", "model_name")
    list_filter = ("status", "fuel_type", "brand")
    search_fields = ("brand", "model_name", "model_code", "chassis_number")
    list_editable = ("status",)
    actions = ["rebuild_derivatives"]
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (
            "Vehicle",
            {
                "fields": (
                    "brand",
                    "model_name",
                    "grade",
                    "model_code",
                    "chassis_number",
                    "manufacture_year",
                )
            },
        ),
        ("Specification", {"fields": ("fuel_type", "seat_capacity", "color")}),
        ("Listing", {"fields": ("price_jpy", "status")}),
        (
            "Video",
            {
                "fields": ("video",),
                "description": (
                    "Optional MP4 walkaround. Nothing downloads until a visitor presses "
                    "play, so it costs nothing on page load. MP4 only - iPhone "
                    "'High Efficiency' clips are HEVC/.mov and will not play in Chrome "
                    "or Firefox."
                ),
            },
        ),
        (
            "Description",
            {
                "fields": ("description_en", "description_ja"),
                "description": (
                    "Either language may be left blank — the site falls back to "
                    "whichever one is filled in."
                ),
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )

    @admin.display(description="Price", ordering="price_jpy")
    def display_price(self, obj):
        if obj.price_jpy is None:
            return "Call for price"
        return f"¥{obj.price_jpy:,}"

    def save_model(self, request, obj, form, change):
        if "video" in form.changed_data or getattr(obj, "video_direct_name", None):
            obj.video_uploaded_at = timezone.now() if obj.video else None
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Catch up on anything an earlier save ran out of time for. Free to do here:
        # the database is already awake for this request, so it costs no extra Aurora
        # time, unlike a scheduled sweep would.
        caught_up = process_pending()
        if caught_up:
            messages.info(
                request, f"Also optimised {caught_up} photo(s) left from an earlier save."
            )

    @admin.action(description="Rebuild optimised copies")
    def rebuild_derivatives(self, request, queryset):
        """Manual repair for photos that never finished processing."""
        rebuilt = failed = 0
        for car in queryset:
            for image in car.images.all():
                try:
                    build_derivatives(image)
                    rebuilt += 1
                except Exception:
                    failed += 1
        if rebuilt:
            self.message_user(request, f"Rebuilt {rebuilt} photo(s).")
        if failed:
            self.message_user(
                request, f"{failed} photo(s) failed.", level=messages.ERROR
            )


@admin.register(CarImage)
class CarImageAdmin(admin.ModelAdmin):
    list_display = ("__str__", "car", "is_primary", "order")
    list_filter = ("is_primary",)
