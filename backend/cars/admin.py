from django.contrib import admin
from django.utils.html import format_html

from .models import Car, CarImage


class CarImageInline(admin.TabularInline):
    model = CarImage
    extra = 3
    fields = ("image", "preview", "is_primary", "order")
    readonly_fields = ("preview",)

    @admin.display(description="Preview")
    def preview(self, obj):
        if not obj.image:
            return "—"
        return format_html(
            '<img src="{}" style="height:70px;border-radius:4px" />', obj.image.url
        )


@admin.register(Car)
class CarAdmin(admin.ModelAdmin):
    inlines = [CarImageInline]
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


@admin.register(CarImage)
class CarImageAdmin(admin.ModelAdmin):
    list_display = ("__str__", "car", "is_primary", "order")
    list_filter = ("is_primary",)
