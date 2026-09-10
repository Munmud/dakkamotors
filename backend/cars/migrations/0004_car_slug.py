"""Add readable URLs.

Done in three steps rather than one. Adding a unique column straight away would fail the
moment there is more than one existing row, because every one of them would default to
the same empty string.
"""

from django.db import migrations, models
from django.utils.text import slugify


def populate_slugs(apps, schema_editor):
    Car = apps.get_model("cars", "Car")
    taken = set()
    for car in Car.objects.all().order_by("pk"):
        parts = [str(car.manufacture_year), car.brand, car.model_name, car.grade]
        base = slugify(" ".join(p for p in parts if p)) or f"car-{car.pk}"
        base = base[:110].rstrip("-")
        candidate, suffix = base, 2
        while candidate in taken:
            candidate = f"{base}-{suffix}"
            suffix += 1
        taken.add(candidate)
        car.slug = candidate
        car.save(update_fields=["slug"])


def clear_slugs(apps, schema_editor):
    apps.get_model("cars", "Car").objects.update(slug="")


class Migration(migrations.Migration):
    dependencies = [("cars", "0003_staffaccount")]

    operations = [
        migrations.AddField(
            model_name="car",
            name="slug",
            field=models.SlugField(blank=True, default="", max_length=120),
            preserve_default=False,
        ),
        migrations.RunPython(populate_slugs, clear_slugs),
        migrations.AlterField(
            model_name="car",
            name="slug",
            field=models.SlugField(
                blank=True,
                help_text="Set automatically on first save. Changing it breaks existing "
                "links and search results, so edit only if you mean to.",
                max_length=120,
                unique=True,
            ),
        ),
    ]
