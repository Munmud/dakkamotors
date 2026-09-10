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
    """Undo whatever a half-finished earlier run left behind.

    Discovers the objects rather than naming them: the index name Django generates
    embeds a hash, and guessing it wrong is why the first attempt at this cleanup did
    nothing. Anything found is dropped, including the column itself - it can only exist
    here if a previous run of *this* migration created it, and its contents are
    regenerated a few operations later.

    Postgres only; SQLite starts clean in tests and has no DROP CONSTRAINT.
    """
    if schema_editor.connection.vendor != "postgresql":
        return

    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'cars_car' AND indexname LIKE %s",
            ["%slug%"],
        )
        indexes = [row[0] for row in cursor.fetchall()]

        cursor.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'cars_car' AND column_name = 'slug'"
        )
        column_exists = bool(cursor.fetchone())
        print(f"slug cleanup: indexes={indexes} column_exists={column_exists}")

        for name in indexes:
            # A unique constraint owns its index, so the constraint has to go first.
            cursor.execute(f'ALTER TABLE cars_car DROP CONSTRAINT IF EXISTS "{name}"')
            cursor.execute(f'DROP INDEX IF EXISTS "{name}"')

        if column_exists:
            cursor.execute("ALTER TABLE cars_car DROP COLUMN slug")
            print("slug cleanup: dropped partially-created column")


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
