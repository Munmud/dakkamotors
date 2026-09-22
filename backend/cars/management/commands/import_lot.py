"""Import a folder of car photos as listings, one folder per car.

An ops script, in the same family as `import_dynamo`: run it from a laptop with real
credentials rather than through `zappa manage`, because nothing about it needs a Lambda
and a fifty-photo upload does not belong in a request.

    python manage.py import_lot --from ../database_source --manifest lot.json [--apply]

The manifest says what the photographs cannot: make, model, colour, how many seats.
Everything absent from it is left absent from the car -- a missing year is a car
without a year, not a car with a guessed one. A folder with no manifest entry is
skipped and named, so a typo is visible rather than silent.

Idempotent on the folder name, which is recorded in `import_key`: a second run reports
what is already there and writes nothing. That matters more than usual here, because
there is no undo and the photos are slow to upload.
"""

import io
import json
import pathlib

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from cars import images as image_ops
from cars.choices import CarStatus, FuelType
from cars.store import cars as car_store
from cars.store import images as image_store
from cars.store import keys
from cars.store.errors import ConditionFailed
from cars.store.models import Car

PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def photos_in(folder):
    """Every photo in one folder, in name order, so a run is reproducible."""
    return sorted(
        (p for p in folder.iterdir()
         if p.is_file() and p.suffix.lower() in PHOTO_SUFFIXES),
        key=lambda p: p.name.lower(),
    )


class Command(BaseCommand):
    help = "Import folders of photos as car listings (see the module docstring)."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="source", required=True,
                            help="Directory holding one folder per car.")
        parser.add_argument("--manifest", required=True,
                            help="JSON file describing each folder.")
        parser.add_argument("--apply", action="store_true",
                            help="Actually write. Without it, only report.")

    def handle(self, *args, **options):
        source = pathlib.Path(options["source"]).resolve()
        if not source.is_dir():
            raise CommandError(f"No such directory: {source}")

        manifest = json.loads(
            pathlib.Path(options["manifest"]).read_text(encoding="utf-8"))
        entries = manifest["cars"]
        apply = options["apply"]

        if not apply:
            self.stdout.write(self.style.WARNING(
                "Dry run. Nothing is written; pass --apply to import."))

        for entry in entries:
            folder = source / entry["folder"]
            if not folder.is_dir():
                self.stdout.write(self.style.ERROR(
                    f"  {entry['folder']}: no such folder, skipped"))
                continue

            names = [p.name for p in photos_in(folder)]
            extra = [source / name for name in entry.get("extra_photos", [])]
            missing = [p for p in extra if not p.is_file()]
            if missing:
                self.stdout.write(self.style.ERROR(
                    f"  {entry['folder']}: missing extra photo(s) "
                    f"{', '.join(p.name for p in missing)}, skipped"))
                continue

            existing = self.find_imported(entry["folder"])
            if existing is not None:
                self.stdout.write(
                    f"  {entry['folder']}: already imported as {existing.slug}")
                continue

            label = " ".join(filter(None, (entry.get("brand"), entry.get("model_name"))))
            self.stdout.write(
                f"  {entry['folder']}: {label} [{entry.get('color', '-')}] "
                f"{len(names) + len(extra)} photo(s)")

            if apply:
                self.import_one(entry, self.order_photos(entry, folder, extra))

    def order_photos(self, entry, folder, extra):
        """The car's own photos, then the wide shots, with the card first.

        Sorted by name so a run is reproducible -- but a name sorts "10.08" before
        "9.55", which is how a two-car forecourt shot ended up as one car's listing
        card. `primary_photo` names the one that should lead when the first by name
        is not the best picture of the car.
        """
        photos = [*photos_in(folder), *extra]
        wanted = entry.get("primary_photo")
        if wanted:
            lead = [p for p in photos if p.name == wanted]
            if not lead:
                raise CommandError(
                    f"{entry['folder']}: primary_photo {wanted!r} is not in the folder")
            photos = lead + [p for p in photos if p.name != wanted]
        return photos

    def find_imported(self, folder_name):
        """A car already carrying this folder's import key, or None.

        Scans the three status partitions rather than carrying an index: this runs
        once, by hand, against an inventory of tens of cars.
        """
        for status in (CarStatus.AVAILABLE, CarStatus.RESERVED, CarStatus.SOLD):
            for car in car_store.list_by_status(status):
                if car.import_key == folder_name:
                    return car
        return None

    def import_one(self, entry, paths):
        now = timezone.now()
        car = Car(
            car_id=keys.new_id(),
            brand=entry.get("brand"),
            model_name=entry.get("model_name"),
            brand_ja=entry.get("brand_ja"),
            model_name_ja=entry.get("model_name_ja"),
            color=entry.get("color"),
            color_ja=entry.get("color_ja"),
            # No manufacture_year and no price_jpy unless the manifest carries them.
            # The photographs do not show either, and a car with no year reads as
            # "Toyota Prius" rather than as a year somebody made up.
            manufacture_year=entry.get("manufacture_year"),
            price_jpy=entry.get("price_jpy"),
            fuel_type=entry.get("fuel_type", FuelType.PETROL),
            seat_capacity=entry.get("seat_capacity", 5),
            status=entry.get("status", CarStatus.SOLD),
            # Optional, and normally absent: a photograph does not say when a car
            # sold. A car with no date sits below every car that has one.
            sold_at=entry.get("sold_at"),
            import_key=entry["folder"],
        )
        try:
            car = car_store.create(car, now=now)
        except ConditionFailed:
            self.stdout.write(self.style.ERROR(
                f"    refused (duplicate slug or chassis): {entry['folder']}"))
            return

        self.stdout.write(self.style.SUCCESS(f"    created {car.slug}"))

        for order, path in enumerate(paths):
            name = self.store_photo(path)
            image = image_store.create(
                car_id=car.car_id,
                image_name=name,
                # The first photo in the folder is the listing card. Named order, not
                # chance: `refresh_primary` would otherwise pick by (order, sk).
                is_primary=(order == 0),
                order=order,
                now=now,
            )
            # Inline rather than queued. This is one machine doing one import, and a
            # photo whose derivatives never got built is a card with no picture.
            widths = image_ops.build_derivatives(image)
            self.stdout.write(f"    {path.name} -> {name} {widths}")

    def store_photo(self, path):
        """Put the original where an uploaded one would have gone.

        Same shape as `uploads.build_presigned_upload`: a fresh uuid per photo, so two
        files called `IMG_0001.jpg` cannot collide, and `derivative_name` has a stem to
        hang the resized copies off.
        """
        name = f"cars/{keys.new_id()}/original{path.suffix.lower()}"
        with io.open(path, "rb") as fh:
            default_storage.save(name, ContentFile(fh.read()))
        return name
