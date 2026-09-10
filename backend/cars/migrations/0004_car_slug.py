"""Add readable URLs.

Done in steps rather than one AddField. A unique column added straight away would fail
the moment there is more than one existing row, because every one of them would default
to the same empty string.

The first operation cleans up after an interrupted earlier attempt. The deploy that
introduced this migration shipped the code before running it, so the live site started
erroring on the missing column; the retries that followed overlapped, and one left a
stray index behind whose name then blocked every later run with
`relation "cars_car_slug_85603327_like" already exists`. Dropping it defensively makes
the migration safe to re-run from whatever state it finds.
"""

from django.db import migrations, models
from django.utils.text import slugify


def drop_leftovers(apps, schema_editor):
    """Remove index/constraint fragments from a half-finished earlier run.

    Postgres only: SQLite starts clean in tests and has no DROP CONSTRAINT.
    """
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DROP INDEX IF EXISTS cars_car_slug_85603327_like")
        cursor.execute("ALTER TABLE cars_car DROP CONSTRAINT IF EXISTS cars_car_slug_key")


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
        migrations.RunPython(drop_leftovers, migrations.RunPython.noop),
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
