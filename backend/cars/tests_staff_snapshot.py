"""Render every staff page to a file, so it can be screenshotted.

Not really a test -- a dump, run on demand and skipped otherwise. It lives here because
signing in to the staff pages is only possible in-process: `sign_in` patches
`cars.authentication.verify`, which a separate `runserver` would never see. The
alternative is a settings flag that skips authentication, and that is exactly the kind of
thing that survives into production.

    STAFF_SNAPSHOT_DIR=... python manage.py test cars.tests_staff_snapshot

Serve the directory over http rather than opening the files: `base.html` and the car form
reference `/static/cars/*` as root-absolute paths, and `formset-rows.js` is what reveals
the three "+ Add a row" buttons. A file:// screenshot of the car form silently omits
them, on the page most worth looking at.
"""

import os
import shutil
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .store import cars as car_store
from .store import requests as request_store
from .tests import (
    DynamoReset, MAIL_SETTINGS, attach_photo, attach_video, future_slot, make_booking,
    make_car, make_customer, make_question, make_schedule,
)
from .tests_fake_cognito import FakeCognito, sign_in
from .tests_staff import make_owner

OUT = os.environ.get("STAFF_SNAPSHOT_DIR", "")


@override_settings(**MAIL_SETTINGS)
class StaffSnapshot(FakeCognito, DynamoReset, SimpleTestCase):
    """Every staff page, with enough stock on the lot to look like a real one."""

    def test_dump(self):
        if not OUT:
            self.skipTest("set STAFF_SNAPSHOT_DIR to dump the staff pages")

        dest = Path(OUT)
        dest.mkdir(parents=True, exist_ok=True)

        # Signed in as an owner, not a manager: the accounts pages are owners-only and
        # the car form's delete block is gated on the same permission.
        owner, _ = make_owner()
        sign_in(self.client, owner, staff=True)

        car = make_car("SNAP-1", brand="Honda", model_name="N-BOX",
                       manufacture_year=2021, price_jpy=1248000, color="Pearl White")
        car.specs = [
            {"label_en": "Transmission", "label_ja": "ミッション",
             "value_en": "CVT automatic", "value_ja": "CVT オートマ"},
            {"label_en": "Inspection until", "label_ja": "車検有効期限",
             "value_en": "March 2028", "value_ja": "2028年3月"},
        ]
        # Advertised, and already stopped by a booking. Both halves matter to a review
        # pass: the ad set id is the only free-text field in the Listing group, and the
        # "stopped itself" note under it is the longest help text on the page -- which
        # is what would show a phone-width problem first.
        car.ad_set_id = "120210000000000123"
        car.save()
        car_store.claim_ad_pause(car.car_id, timezone.now())
        car = car_store.detail(car.car_id)
        attach_photo(car, 1200, 900)
        attach_photo(car, 1200, 900)
        attach_video(car, "walkaround.mp4")
        make_car("SNAP-2", brand="Suzuki", model_name="Wagon R",
                 manufacture_year=2020, price_jpy=989000)
        make_car("SNAP-3", brand="Daihatsu", model_name="Move Canbus",
                 manufacture_year=2019, status="sold")

        customer, _ = make_customer("buyer@example.com")
        question = make_question(car, customer=customer,
                                 question="Has it had one owner?")
        make_question(car, customer=customer, question="Any service history?",
                      answer="Full book, stamped every year.", published=True)

        schedule = make_schedule()
        booking = make_booking(customer, future_slot(), car=car)
        wish = request_store.create(
            name="Hana Sato", email="hana@example.com", phone="080-1234-5678",
            details="A white Tanto, 2018 or newer, under 900,000. Both sliding doors.")
        request_store.resolve(
            request_store.create(name="Old One", email="old@example.com", phone="1",
                                 details="A van"),
            staff_sub="x", note="Found them one")

        pages = [
            ("cars-list", reverse("staff:car-list")),
            ("cars-add", reverse("staff:car-add")),
            ("cars-edit", reverse("staff:car-edit", args=[car.car_id])),
            ("bookings-list", reverse("staff:booking-list")),
            ("slots-list", reverse("staff:slot-list")),
            ("schedules-list", reverse("staff:schedule-list")),
            ("schedules-edit", reverse("staff:schedule-edit",
                                       args=[schedule.schedule_id])),
            ("questions-list", reverse("staff:question-list")),
            ("questions-detail", reverse("staff:question-detail",
                                         args=[question.question_id])),
            ("requests-list", reverse("staff:request-list")),
            ("requests-detail", reverse("staff:request-detail", args=[wish.request_id])),
            ("customers-list", reverse("staff:customer-list")),
            ("accounts-list", reverse("staff:staff-list")),
            ("accounts-add", reverse("staff:staff-add")),
        ]
        pages.append(("bookings-detail",
                      reverse("staff:booking-detail", args=[booking.booking_id])))

        written = []
        for name, url in pages:
            try:
                response = self.client.get(url)
            except Exception as exc:
                # The accounts pages list real Cognito users, which needs the moto
                # pool that `CognitoBackend` stands up and `FakeCognito` does not.
                # Worth skipping rather than restructuring the whole dump around.
                print(f"  {name}: {type(exc).__name__}, skipped")
                continue
            if response.status_code != 200:
                print(f"  {name}: HTTP {response.status_code}, skipped")
                continue
            (dest / f"{name}.html").write_bytes(response.content)
            written.append(name)

        # The two files the templates ask for by absolute path, plus the uploads the
        # gallery thumbnails point at. A broken thumbnail is exactly what you must not
        # review a layout against.
        shutil.copytree(Path(settings.BASE_DIR) / "cars" / "static" / "cars",
                        dest / "static" / "cars", dirs_exist_ok=True)
        media = Path(settings.MEDIA_ROOT)
        if media.exists():
            shutil.copytree(media, dest / "media", dirs_exist_ok=True)

        print(f"\n  {len(written)} pages -> {dest}")
        for name in written:
            print(f"    {name}.html")
        self.assertTrue(written)
