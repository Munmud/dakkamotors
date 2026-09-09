"""Regenerate the resized copies of car photos.

Two jobs: repairing photos whose asynchronous processing failed, and re-processing
everything after the target widths or WebP quality change.
"""

from django.core.management.base import BaseCommand

from cars.images import build_derivatives
from cars.models import CarImage


class Command(BaseCommand):
    help = "Build missing (or with --all, every) image derivative."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Rebuild every photo, not just the ones missing copies.",
        )

    def handle(self, *args, **options):
        queryset = CarImage.objects.exclude(image="")
        if not options["all"]:
            queryset = queryset.filter(derivatives_ready=False)

        total = queryset.count()
        if not total:
            self.stdout.write("Nothing to do - every photo already has its copies.")
            return

        built = failed = 0
        for car_image in queryset.iterator():
            try:
                widths = build_derivatives(car_image)
            except Exception as exc:
                failed += 1
                self.stderr.write(
                    self.style.ERROR(f"CarImage {car_image.pk}: {exc}")
                )
                continue
            built += 1
            self.stdout.write(f"CarImage {car_image.pk}: {widths}")

        self.stdout.write(
            self.style.SUCCESS(f"Rebuilt {built} of {total}.")
            if not failed
            else self.style.WARNING(f"Rebuilt {built} of {total}, {failed} failed.")
        )
