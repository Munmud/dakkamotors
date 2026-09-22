"""The photo-folder importer.

An ops script that writes to production by hand, so what is asserted here is mostly
what it must *not* do: write on a dry run, invent a year, or import the same folder
twice. The second one matters because the only undo is deleting cars in the staff
pages, and the run takes as long as the photos take to upload.
"""

import io
import json
import pathlib
import tempfile

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from PIL import Image

from .choices import CarStatus, FuelType
from .store import cars as car_store
from .store import images as image_store
from .tests import DynamoReset

MANIFEST = {
    "cars": [
        {
            "folder": "toyota-prius-62-30",
            "brand": "Toyota", "model_name": "Prius",
            "brand_ja": "トヨタ", "model_name_ja": "プリウス",
            "color": "Silver", "color_ja": "シルバー",
            "seat_capacity": 5, "fuel_type": "hybrid", "status": "sold",
            "extra_photos": ["_lot/forecourt.jpg"],
        },
        {
            "folder": "nowhere",
            "brand": "Ghost", "model_name": "Car", "status": "sold",
        },
    ]
}


def jpeg(path, width=900, height=600):
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (90, 110, 130)).save(buffer, format="JPEG")
    path.write_bytes(buffer.getvalue())


class ImportLotTests(DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(self._clean)
        folder = self.root / "toyota-prius-62-30"
        # Deliberately out of order on disk: the command sorts by name, so the run is
        # reproducible and "the first photo is the card" means something.
        for name in ("b.jpg", "a.jpg"):
            jpeg(folder / name)
        jpeg(self.root / "_lot" / "forecourt.jpg")

        self.manifest = self.root / "lot.json"
        self.manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")

    def _clean(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def run_import(self, *, apply=False):
        out = io.StringIO()
        args = ["import_lot", "--from", str(self.root), "--manifest", str(self.manifest)]
        if apply:
            args.append("--apply")
        call_command(*args, stdout=out, stderr=out)
        return out.getvalue()

    def sold(self):
        return car_store.list_by_status(CarStatus.SOLD)

    def test_a_dry_run_writes_nothing(self):
        output = self.run_import()

        self.assertEqual(self.sold(), [])
        self.assertIn("Dry run", output)
        self.assertIn("toyota-prius-62-30", output)

    def test_a_folder_the_manifest_names_but_disk_does_not_is_reported(self):
        output = self.run_import()
        self.assertIn("nowhere: no such folder", output)

    def test_importing_creates_the_car_with_no_year_and_no_price(self):
        self.run_import(apply=True)

        (car,) = self.sold()
        self.assertEqual(car.brand, "Toyota")
        self.assertEqual(car.model_name_ja, "プリウス")
        self.assertEqual(car.color_ja, "シルバー")
        self.assertEqual(car.fuel_type, FuelType.HYBRID)
        self.assertEqual(car.seat_capacity, 5)
        self.assertEqual(car.status, CarStatus.SOLD)
        self.assertEqual(car.slug, "toyota-prius")
        # The two the photographs cannot tell us, left as nothing rather than guessed.
        self.assertIsNone(car.manufacture_year)
        self.assertIsNone(car.price_jpy)
        self.assertEqual(car.import_key, "toyota-prius-62-30")

    def test_the_first_photo_is_the_card_and_the_lot_shot_comes_last(self):
        self.run_import(apply=True)

        (car,) = self.sold()
        photos = image_store.for_car(car.car_id)

        self.assertEqual(len(photos), 3)
        self.assertTrue(photos[0].is_primary)
        self.assertFalse(any(p.is_primary for p in photos[1:]))
        self.assertEqual(car.primary_image.image_id, photos[0].image_id)

    def test_every_photo_gets_its_resized_copies(self):
        """A card with no derivatives is a card with no picture, and this is the one
        place they are built inline rather than queued."""
        self.run_import(apply=True)

        (car,) = self.sold()
        for photo in image_store.for_car(car.car_id):
            self.assertTrue(photo.derivatives_ready, photo.image_name)
            self.assertTrue(photo.derivative_urls, photo.image_name)

    def test_primary_photo_names_the_card(self):
        """Names sort "10.08" before "9.55", so the first file is not always the best
        picture of the car -- one forecourt shot shared with another car led a listing
        until the manifest could say otherwise."""
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["cars"][0]["primary_photo"] = "b.jpg"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

        self.run_import(apply=True)

        (car,) = self.sold()
        photos = image_store.for_car(car.car_id)
        self.assertTrue(photos[0].is_primary)
        self.assertEqual(len(photos), 3)
        # a.jpg sorted first on disk; b.jpg leads because the manifest said so.
        self.assertEqual(sum(p.is_primary for p in photos), 1)

    def test_a_primary_photo_that_is_not_there_stops_the_run(self):
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["cars"][0]["primary_photo"] = "nope.jpg"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaises(CommandError):
            self.run_import(apply=True)

    def test_an_imported_car_has_no_sale_date_unless_the_manifest_gives_one(self):
        """A photograph does not say when a car sold, and the shelf puts the undated
        below everything dated rather than above it."""
        self.run_import(apply=True)
        (car,) = self.sold()
        self.assertIsNone(car.sold_at)

    def test_a_manifest_sale_date_lands_on_the_car(self):
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["cars"][0]["sold_at"] = "2026-03-01"
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

        self.run_import(apply=True)

        (car,) = self.sold()
        self.assertEqual(car.sold_at, "2026-03-01")

    def test_running_it_twice_imports_nothing_the_second_time(self):
        self.run_import(apply=True)

        output = self.run_import(apply=True)

        self.assertEqual(len(self.sold()), 1)
        self.assertIn("already imported as toyota-prius", output)
