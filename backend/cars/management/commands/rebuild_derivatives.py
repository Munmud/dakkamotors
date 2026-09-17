"""Regenerate the resized copies of car photos.

Two jobs: repairing photos whose asynchronous processing failed, and re-processing
everything after the target widths or WebP quality change.
"""

from django.core.management.base import BaseCommand

from cars.images import build_derivatives
from cars.store import cars as car_store
from cars.store import images as image_store


class Command(BaseCommand):
    help = "Build missing (or with --all, every) image derivative."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Rebuild every photo, not just the ones missing copies.",
        )

    def handle(self, *args, **options):
        if options["all"]:
            # Every photo of every car. Two queries per car rather than a table scan,
            # which at this inventory size is both cheaper and easier to reason about.
            images = [
                image
                for car in car_store.for_sitemap(("available", "reserved", "sold"))
                for image in image_store.for_car(car.car_id)
            ]
        else:
            # The sparse index of photos whose copies never landed. Normally empty.
            images = image_store.pending_derivatives()

        images = [i for i in images if i.image_name]
        total = len(images)
        if not total:
            self.stdout.write("Nothing to do - every photo already has its copies.")
            return

        built = failed = 0
        for car_image in images:
            try:
                widths = build_derivatives(car_image)
            except Exception as exc:
                failed += 1
                self.stderr.write(
                    self.style.ERROR(f"CarImage {car_image.image_id}: {exc}")
                )
                continue
            built += 1
            self.stdout.write(f"CarImage {car_image.image_id}: {widths}")

        self.stdout.write(
            self.style.SUCCESS(f"Rebuilt {built} of {total}.")
            if not failed
            else self.style.WARNING(f"Rebuilt {built} of {total}, {failed} failed.")
        )
