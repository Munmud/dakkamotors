"""The inventory staff pages.

These replace `CarAdmin` and its `CarImageInline`, which was doing the most for free of
any admin class here. The things worth asserting are the ones it gave without being
asked: a slug that never moves, a chassis number that cannot be duplicated, photos whose
files actually leave the bucket, and a search that finds a car by part of its chassis
number.
"""

import io

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from .store import cars as car_store
from .store import images as image_store
from .store.errors import NotFound
from .store.models import ChassisGuard, SlugGuard
from .store import keys
from .tests import DynamoReset, MAIL_SETTINGS, attach_photo, make_car
from .tests_fake_cognito import FakeCognito, sign_in
from .tests_staff import make_owner, make_staff, make_staff_without_permissions


def a_jpeg(width=1000, height=750):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 130, 140)).save(buffer, "JPEG")
    return SimpleUploadedFile("photo.jpg", buffer.getvalue(),
                              content_type="image/jpeg")


def car_fields(**overrides):
    fields = {
        "brand": "Daihatsu",
        "model_name": "Tanto",
        "grade": "X",
        "model_code": "LA600S",
        "chassis_number": "L375S-0012345",
        "manufacture_year": 2018,
        "fuel_type": "petrol",
        "seat_capacity": 4,
        "color": "Pearl White",
        "status": "available",
        "description_en": "",
        "description_ja": "",
    }
    fields.update(overrides)
    return fields


def image_formset_fields(total=0):
    """The management form a plain formset needs, with no rows filled in."""
    return {
        "images-TOTAL_FORMS": str(max(total, 3)),
        "images-INITIAL_FORMS": "0",
        "images-MIN_NUM_FORMS": "0",
        "images-MAX_NUM_FORMS": "1000",
    }


@override_settings(**MAIL_SETTINGS)
class StaffCarAccessTests(FakeCognito, DynamoReset, TestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("staff:car-list")

    def test_a_guest_is_sent_to_sign_in(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_staff_without_the_permission_are_refused(self):
        sign_in(self.client, make_staff_without_permissions(), staff=True)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_an_inventory_manager_can_open_the_list(self):
        staff, _ = make_staff()
        sign_in(self.client, staff, staff=True)
        self.assertEqual(self.client.get(self.url).status_code, 200)


@override_settings(**MAIL_SETTINGS)
class StaffCarListTests(FakeCognito, DynamoReset, TestCase):
    def setUp(self):
        super().setUp()
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)
        self.url = reverse("staff:car-list")

    def test_searching_by_partial_chassis_number_finds_the_car(self):
        """How a mechanic actually looks a car up."""
        make_car("L375S-0012345", brand="Daihatsu", model_name="Tanto")
        make_car("ANH20-9998888", brand="Toyota", model_name="Alphard")

        response = self.client.get(self.url, {"q": "9998888"})

        self.assertContains(response, "Alphard")
        self.assertNotContains(response, "Tanto")

    def test_filtering_by_status_narrows_the_list(self):
        make_car("AVAIL-1", model_name="Tanto")
        make_car("SOLD-1", model_name="Alto", status="sold")

        response = self.client.get(self.url, {"status": "sold"})

        self.assertContains(response, "Alto")
        self.assertNotContains(response, "Tanto")

    def test_a_car_with_no_price_reads_as_call_for_price(self):
        make_car("NOPRICE")
        self.assertContains(self.client.get(self.url), "Call for price")


@override_settings(**MAIL_SETTINGS)
class StaffCarEditTests(FakeCognito, DynamoReset, TestCase):
    def setUp(self):
        super().setUp()
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)

    def test_adding_a_car_creates_it_with_a_slug(self):
        response = self.client.post(
            reverse("staff:car-add"), car_fields(chassis_number="NEW-1"), follow=True)

        self.assertEqual(response.status_code, 200)
        cars = car_store.list_by_status("available")
        self.assertEqual(len(cars), 1)
        self.assertEqual(cars[0].slug, "2018-daihatsu-tanto-x")
        self.assertContains(response, "Now add its photos")

    def test_a_duplicate_chassis_number_is_refused_with_a_reason(self):
        make_car("TAKEN-1")

        response = self.client.post(
            reverse("staff:car-add"),
            car_fields(chassis_number="TAKEN-1", grade="Z"), follow=True)

        self.assertContains(response, "already on another car")
        self.assertEqual(len(car_store.list_by_status("available")), 1)

    def test_a_refused_car_leaves_no_orphan_slug_guard(self):
        """The reason the car and both guards go in one transaction."""
        make_car("TAKEN-2")

        self.client.post(reverse("staff:car-add"),
                         car_fields(chassis_number="TAKEN-2", grade="Z"), follow=True)

        with self.assertRaises(SlugGuard.DoesNotExist):
            SlugGuard.get(keys.slug_pk("2018-daihatsu-tanto-z"), "SLUG")

    def test_editing_does_not_rewrite_the_slug(self):
        """A URL that moves when someone fixes a typo breaks every shared link."""
        car = make_car("SLUG-1")
        before = car.slug

        self.client.post(reverse("staff:car-edit", args=[car.car_id]),
                         {**car_fields(chassis_number="SLUG-1", grade="Corrected"),
                          **image_formset_fields()}, follow=True)

        self.assertEqual(car_store.get(car.car_id).slug, before)
        self.assertEqual(car_store.get(car.car_id).grade, "Corrected")

    def test_changing_status_moves_the_car_between_listings(self):
        car = make_car("STATUS-1")

        self.client.post(reverse("staff:car-edit", args=[car.car_id]),
                         {**car_fields(chassis_number="STATUS-1", status="sold"),
                          **image_formset_fields()}, follow=True)

        self.assertEqual(car_store.list_by_status("available"), [])
        self.assertEqual(len(car_store.list_by_status("sold")), 1)

    def test_changing_the_chassis_number_frees_the_old_one(self):
        car = make_car("OLD-1")

        self.client.post(reverse("staff:car-edit", args=[car.car_id]),
                         {**car_fields(chassis_number="NEW-9"),
                          **image_formset_fields()}, follow=True)

        self.assertEqual(
            ChassisGuard.get(keys.chassis_guard_pk("NEW-9"), keys.GUARD).car_id,
            car.car_id)
        with self.assertRaises(ChassisGuard.DoesNotExist):
            ChassisGuard.get(keys.chassis_guard_pk("OLD-1"), keys.GUARD)

    def test_a_missing_car_is_a_404(self):
        self.assertEqual(
            self.client.get(reverse("staff:car-edit", args=["nope"])).status_code, 404)


@override_settings(**MAIL_SETTINGS)
class StaffCarPhotoTests(FakeCognito, DynamoReset, TestCase):
    def setUp(self):
        super().setUp()
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)
        self.car = make_car("PHOTOS-1")
        self.url = reverse("staff:car-edit", args=[self.car.car_id])

    def post(self, extra=None, images=None):
        data = {**car_fields(chassis_number="PHOTOS-1"), **image_formset_fields()}
        data.update(extra or {})
        return self.client.post(self.url, {**data, **(images or {})}, follow=True)

    def test_uploading_a_photo_attaches_it_and_builds_its_copies(self):
        response = self.post(images={"images-0-image": a_jpeg()})

        self.assertContains(response, "Added 1 photo")
        photos = image_store.for_car(self.car.car_id)
        self.assertEqual(len(photos), 1)
        # Queued and run inline, so the resized copies exist by the time we look.
        self.assertTrue(photos[0].derivatives_ready)
        self.assertEqual(photos[0].available_widths, [320, 800])

    def test_a_photo_becomes_the_listing_card(self):
        self.post(images={"images-0-image": a_jpeg()})
        card = car_store.get(self.car.car_id).primary_image
        self.assertIsNotNone(card)

    def test_reordering_changes_which_photo_leads(self):
        first = attach_photo(self.car, 900, 600)
        second = attach_photo(self.car, 900, 600)

        self.post(extra={
            f"image-{first.image_id}-order": "5",
            f"image-{second.image_id}-order": "1",
        })

        self.assertEqual(car_store.get(self.car.car_id).primary_image.image_id,
                         second.image_id)

    def test_marking_a_photo_as_the_card_photo(self):
        first = attach_photo(self.car, 900, 600)
        second = attach_photo(self.car, 900, 600)

        self.post(extra={"primary": second.image_id})

        self.assertEqual(car_store.get(self.car.car_id).primary_image.image_id,
                         second.image_id)

    def test_removing_a_photo_deletes_its_files(self):
        photo = attach_photo(self.car, 1000, 750)
        photo.refresh()
        original, derivative = photo.image_name, photo.derivative_name(800)
        self.assertTrue(default_storage.exists(original))

        self.post(extra={f"image-{photo.image_id}-delete": "on"})

        self.assertEqual(image_store.for_car(self.car.car_id), [])
        self.assertFalse(default_storage.exists(original))
        self.assertFalse(default_storage.exists(derivative))

    def test_rebuilding_regenerates_the_copies(self):
        photo = attach_photo(self.car, 1000, 750)
        photo.update(actions=[
            type(photo).derivatives_ready.set(False),
            type(photo).derivative_widths.remove(),
        ])

        response = self.client.post(self.url, {"action": "rebuild-derivatives"},
                                    follow=True)

        self.assertContains(response, "Rebuilt 1 photo")
        self.assertTrue(image_store.get(self.car.car_id, photo.image_id)
                        .derivatives_ready)


@override_settings(**MAIL_SETTINGS)
class StaffCarDeleteTests(FakeCognito, DynamoReset, TestCase):
    def setUp(self):
        super().setUp()
        # Delete is owner-only in the permission map.
        self.staff, _ = make_owner()
        sign_in(self.client, self.staff, staff=True)

    def test_deleting_a_car_takes_its_guards_photos_and_files(self):
        car = make_car("DELETE-1")
        photo = attach_photo(car, 1000, 750)
        photo.refresh()
        original = photo.image_name
        self.assertTrue(default_storage.exists(original))

        response = self.client.post(reverse("staff:car-edit", args=[car.car_id]),
                                    {"action": "delete"}, follow=True)

        self.assertContains(response, "Deleted")
        with self.assertRaises(NotFound):
            car_store.get(car.car_id)
        with self.assertRaises(SlugGuard.DoesNotExist):
            SlugGuard.get(keys.slug_pk(car.slug), "SLUG")
        self.assertFalse(default_storage.exists(original))

    def test_the_freed_chassis_number_can_be_reused(self):
        car = make_car("REUSE-1")
        self.client.post(reverse("staff:car-edit", args=[car.car_id]),
                         {"action": "delete"}, follow=True)

        make_car("REUSE-1")  # must not raise
        self.assertEqual(len(car_store.list_by_status("available")), 1)
