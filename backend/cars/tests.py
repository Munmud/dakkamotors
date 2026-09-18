import datetime
import hashlib
import itertools
import io
import json
import os
import re
import unittest
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from PIL import Image

from . import booking as booking_rules
from . import identity
from . import email_theme
from . import mail
from . import notifications
from . import qa
from . import seo
from .choices import BookingStatus, CarStatus, NotificationKind
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from .store import bookings as booking_store
from .store import cars as car_store
from .store import customers as customer_store
from .store import images as image_store
from .store.models import Car as StoreCar
from .store.models import PendingRegistration as StorePending
from .tasks import build_derivatives_task
from .store import keys as store_keys
from .store import notifications as notification_store
from .store import questions as question_store
from .store import schedules as schedule_store
from .store import slots as slot_store
from . import cognito
from . import tests_fake_cognito as fake_cognito
from .tests_fake_cognito import FakeCognito, sign_in, sign_out
from .tests_store import count_dynamo_calls, ensure_table, truncate_table
from .uploads import UploadRejected, _validate


class DynamoReset:
    """Truncate the DynamoDB table between tests.

    Django's TestCase wraps each test in a database transaction and rolls it back, which
    keeps SQL tests isolated for free. It knows nothing about DynamoDB, so anything that
    now writes there -- notifications, so far -- leaks into the next test unless it is
    cleared explicitly. Mix this in wherever that applies.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ensure_table()

    def setUp(self):
        super().setUp()
        truncate_table()


def make_question(car, *, customer=None, question="Is it rust free?", answer="",
                  answered=False, published=False, language="en"):
    """Create a question in the store, the way the app would.

    Replaces the `CarQuestion.objects.create(...)` these tests used to call. Questions
    live in DynamoDB now, so `answered_at` and `is_published` cannot simply be passed in
    -- both are set by the writes that mean them, which is the point of the design.
    """
    record = question_store.create(
        car_id=car.car_id,
        car_brand=car.brand,
        car_model_name=car.model_name,
        car_slug=car.slug,
        car_label=str(car),
        customer_sub=str(customer.pk) if customer else None,
        customer_email=customer.email if customer else "",
        question=question,
        language=language,
        now=timezone.now(),
    )
    if answer or answered or published:
        record, _ = question_store.record_answer(
            question=record, answer=answer or "Yes.", staff_sub=None,
            now=timezone.now(),
        )
    if published:
        record = question_store.publish(record, now=timezone.now(), bump_car=False)
    return record


def questions_in_store():
    return question_store.queue()


def bell(user, kind=None):
    """A customer's notifications, read back through the store."""
    rows = notification_store.recent(str(user.pk), limit=notification_store.KEEP_ROWS)
    return [r for r in rows if kind is None or r.kind == kind]

# A 1x1 GIF — smallest thing Pillow will accept as a real image.
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


_car_sequence = itertools.count(1)


def make_car(chassis, **overrides):
    """A car in the store, as the staff page would create it."""
    fields = {
        "brand": "Daihatsu",
        "model_name": "Tanto",
        "grade": "X",
        "model_code": "LA600S",
        "chassis_number": chassis,
        "manufacture_year": 2018,
        "seat_capacity": 4,
        "color": "Pearl White",
    }
    fields.update(overrides)
    car = StoreCar(car_id=str(next(_car_sequence)), **fields)
    return car_store.create(car, now=timezone.now())


def attach_image(car, name, *, is_primary=False, order=0):
    return _attach(car, name, TINY_GIF, is_primary=is_primary, order=order)


def _attach(car, name, payload, **fields):
    """Put the bytes in storage, record the photo, and build its resized copies.

    The ORM did the last step through CarImage.save(); the store deliberately does not,
    because a storage layer that queues work is a storage layer with opinions. The
    staff page calls the task explicitly for the same reason, so this mirrors it.
    """
    stored = default_storage.save(f"cars/{name}", ContentFile(payload))
    image = image_store.create(
        car_id=car.car_id, image_name=stored, now=timezone.now(), **fields
    )
    build_derivatives_task(f"{car.car_id}:{image.image_id}")
    return image_store.get(car.car_id, image.image_id)


class ClearsThrottleMixin:
    """Reset the rate-limit counter between tests.

    DRF keeps throttle history in Django's cache, which lives for the whole test
    process - so auth tests start tripping the 20/hour limit partway through a run and
    fail with 429 for reasons that have nothing to do with what they assert.
    """

    def setUp(self):
        super().setUp()
        cache.clear()


class CarListApiTests(DynamoReset, SimpleTestCase):
    def test_list_returns_only_available_cars(self):
        make_car("AVAIL-1", status=CarStatus.AVAILABLE)
        make_car("RESERVED-1", status=CarStatus.RESERVED)
        make_car("SOLD-1", status=CarStatus.SOLD)

        response = self.client.get(reverse("car-list"))

        self.assertEqual(response.status_code, 200)
        chassis = {c["id"] for c in response.json()["results"]}
        self.assertEqual(len(chassis), 1)
        self.assertEqual(response.json()["count"], 1)

    def test_blank_price_serialises_as_null_not_zero(self):
        make_car("NO-PRICE", price_jpy=None)

        result = self.client.get(reverse("car-list")).json()["results"][0]

        self.assertIsNone(result["price_jpy"])

    def test_list_includes_primary_image(self):
        car = make_car("WITH-IMAGE")
        attach_image(car, "b.gif", order=1)
        primary = attach_image(car, "a.gif", is_primary=True, order=2)

        result = self.client.get(reverse("car-list")).json()["results"][0]

        self.assertIsNotNone(result["primary_image"])
        self.assertEqual(result["primary_image"]["id"], primary.image_id)

    def test_list_primary_image_is_null_when_car_has_no_images(self):
        make_car("NO-IMAGE")

        result = self.client.get(reverse("car-list")).json()["results"][0]

        self.assertIsNone(result["primary_image"])


class CarDetailApiTests(DynamoReset, SimpleTestCase):
    def test_detail_is_reachable_for_a_reserved_car(self):
        """A shared link must keep working after the car is reserved or sold."""
        car = make_car("RESERVED-2", status=CarStatus.RESERVED)

        response = self.client.get(reverse("car-detail", args=[car.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "reserved")

    def test_detail_returns_full_gallery_in_order(self):
        car = make_car("GALLERY")
        second = attach_image(car, "second.gif", order=2)
        first = attach_image(car, "first.gif", order=1)

        images = self.client.get(reverse("car-detail", args=[car.slug])).json()["images"]

        self.assertEqual([i["id"] for i in images], [first.image_id, second.image_id])

    def test_detail_exposes_both_descriptions(self):
        car = make_car("DESCRIPTIONS", description_en="English", description_ja="日本語")

        payload = self.client.get(reverse("car-detail", args=[car.slug])).json()

        self.assertEqual(payload["description_en"], "English")
        self.assertEqual(payload["description_ja"], "日本語")


def jpeg(width, height, exif=None):
    """A real JPEG, so Pillow has something with genuine dimensions to resize."""
    buffer = io.BytesIO()
    image = Image.new("RGB", (width, height), (120, 130, 140))
    image.save(buffer, format="JPEG", exif=exif) if exif else image.save(buffer, "JPEG")
    return SimpleUploadedFile("photo.jpg", buffer.getvalue(), content_type="image/jpeg")


def attach_photo(car, width, height, exif=None, **kwargs):
    return _attach(car, "photo.jpg", jpeg(width, height, exif).read(), **kwargs)


class DerivativeTests(DynamoReset, SimpleTestCase):
    def test_widths_larger_than_the_original_are_not_generated(self):
        """Upscaling costs bytes and adds no detail, and would make srcset mislead."""
        image = attach_photo(make_car("SMALL"), 900, 675)

        image.refresh()

        self.assertTrue(image.derivatives_ready)
        self.assertEqual(image.available_widths, [320, 800])
        self.assertNotIn(1600, image.available_widths)

    def test_all_widths_generated_for_a_large_original(self):
        image = attach_photo(make_car("LARGE"), 2400, 1800)

        image.refresh()

        self.assertEqual(image.available_widths, [320, 800, 1600])

    def test_original_smaller_than_every_target_still_gets_one_copy(self):
        """Otherwise a tiny upload would have nothing to serve at all."""
        image = attach_photo(make_car("TINY"), 120, 90)

        image.refresh()

        self.assertTrue(image.derivatives_ready)
        self.assertEqual(image.available_widths, [120])

    def test_two_photos_do_not_share_derivative_names(self):
        """Ordinary uploads all land in cars/, so names built from the folder alone
        would make every photo overwrite the previous one's copies."""
        car = make_car("COLLIDE")
        first = attach_photo(car, 1000, 750)
        second = attach_photo(car, 1000, 750)

        first.refresh()
        second.refresh()

        self.assertNotEqual(first.derivative_name(800), second.derivative_name(800))
        # And both files really exist rather than one having clobbered the other.
        storage = default_storage
        self.assertTrue(storage.exists(first.derivative_name(800)))
        self.assertTrue(storage.exists(second.derivative_name(800)))

    def test_exif_rotation_is_applied(self):
        """Phones record orientation in EXIF; without this, portraits serve sideways."""
        exif = Image.Exif()
        exif[274] = 6  # rotate 90°
        image = attach_photo(make_car("ROTATED"), 1000, 500, exif=exif.tobytes())

        image.refresh()
        storage = default_storage
        with storage.open(image.derivative_name(min(image.available_widths))) as fh:
            generated = Image.open(fh)
            generated.load()

        # A landscape original tagged "rotate 90" must come out portrait.
        self.assertGreater(generated.height, generated.width)

    def test_derivatives_are_webp(self):
        image = attach_photo(make_car("FORMAT"), 1000, 750)
        image.refresh()

        with default_storage.open(image.derivative_name(800)) as fh:
            self.assertEqual(Image.open(fh).format, "WEBP")

    def test_replacing_the_photo_invalidates_the_old_copies(self):
        car = make_car("REPLACED")
        image = attach_photo(car, 1000, 750)
        image.refresh()
        self.assertTrue(image.derivatives_ready)

        image.image = jpeg(1200, 900)
        image.save()

        # Rebuilt for the new photo rather than left describing the old one.
        image.refresh()
        self.assertTrue(image.derivatives_ready)


class FileCleanupTests(DynamoReset, SimpleTestCase):
    def test_deleting_a_photo_removes_its_files(self):
        """Otherwise every sold-and-removed listing leaks megabytes into the bucket."""
        image = attach_photo(make_car("CLEANUP"), 1000, 750)
        image.refresh()
        storage = default_storage
        original, derivative = image.image_name, image.derivative_name(800)
        self.assertTrue(storage.exists(original))
        self.assertTrue(storage.exists(derivative))

        # Explicit now. Django left files behind by default and models.py hung a
        # post_delete receiver off CarImage to clean them up; there are no signals
        # here, so removing the bytes is an argument the caller passes.
        image_store.delete(image.car_id, image.image_id)

        self.assertFalse(storage.exists(original))
        self.assertFalse(storage.exists(derivative))

    def test_deleting_a_car_removes_its_photos_files(self):
        car = make_car("CASCADE")
        image = attach_photo(car, 1000, 750)
        image.refresh()
        storage = default_storage
        original = image.image_name

        # Explicit, like the staff delete view: there is no cascade and no post_delete
        # receiver, so removing the bytes is something a caller asks for.
        for photo in image_store.for_car(car.car_id):
            image_store.delete(car.car_id, photo.image_id)
        car_store.delete(car)

        self.assertFalse(storage.exists(original))


class DerivativeApiTests(DynamoReset, SimpleTestCase):
    def test_sources_absent_until_processing_finishes(self):
        """A <source> pointing at an object that does not exist yet renders broken."""
        car = make_car("PENDING")
        image = attach_photo(car, 1000, 750)
        # Put it back into the "not processed yet" state the async gap really produces.
        image.update(actions=[
            type(image).derivatives_ready.set(False),
            type(image).derivative_widths.remove(),
        ])
        image_store.refresh_primary(car.car_id)

        payload = self.client.get(reverse("car-detail", args=[car.slug])).json()

        self.assertIsNone(payload["images"][0]["sources"])
        # The original is still served, so the page is never image-less.
        self.assertTrue(payload["images"][0]["image"])

    def test_sources_listed_once_ready(self):
        car = make_car("READY")
        attach_photo(car, 2400, 1800)

        sources = self.client.get(reverse("car-detail", args=[car.slug])).json()["images"][0][
            "sources"
        ]

        self.assertEqual(sorted(sources), ["1600", "320", "800"])
        self.assertTrue(all(url.endswith(".webp") for url in sources.values()))

    def test_list_primary_image_carries_sources(self):
        car = make_car("CARD")
        attach_photo(car, 1200, 900, is_primary=True)

        result = self.client.get(reverse("car-list")).json()["results"][0]

        self.assertIn("800", result["primary_image"]["sources"])


class RebuildDerivativesCommandTests(DynamoReset, SimpleTestCase):
    def test_repairs_a_half_processed_photo(self):
        from django.core.management import call_command

        car = make_car("REPAIR")
        image = attach_photo(car, 1000, 750)
        # Half-processed: the original landed but the copies never did. The sparse
        # index entry is what the command looks for, so it has to go back too.
        image.update(actions=[
            type(image).derivatives_ready.set(False),
            type(image).derivative_widths.remove(),
            type(image).gsi1pk.set(store_keys.IMAGE_PENDING_GSI1PK),
            type(image).gsi1sk.set(image.image_id),
        ])

        call_command("rebuild_derivatives", stdout=io.StringIO())

        image.refresh()
        self.assertTrue(image.derivatives_ready)
        self.assertEqual(image.available_widths, [320, 800])


class UploadValidationTests(SimpleTestCase):
    def test_quicktime_video_is_rejected_with_an_explanation(self):
        """HEVC/.mov from an iPhone cannot play in Chrome or Firefox, and nothing
        transcodes it after upload, so accepting it would store a dead file."""
        with self.assertRaises(UploadRejected) as caught:
            _validate("video", "video/quicktime", 1024)

        self.assertIn("MP4", str(caught.exception))

    def test_mp4_is_accepted(self):
        extension, _ = _validate("video", "video/mp4", 1024)
        self.assertEqual(extension, ".mp4")

    def test_oversized_photo_is_rejected(self):
        with self.assertRaises(UploadRejected):
            _validate("image", "image/jpeg", 40 * 1024 * 1024)

    def test_unsupported_image_type_is_rejected(self):
        with self.assertRaises(UploadRejected):
            _validate("image", "image/tiff", 1024)


class SignUploadEndpointTests(FakeCognito, SimpleTestCase):
    url = "/api/staff/uploads/sign/"

    def test_anonymous_users_cannot_sign_uploads(self):
        """Signing grants write access to the media bucket."""
        response = self.client.post(
            self.url,
            {"kind": "image", "content_type": "image/jpeg", "size": 1024},
            content_type="application/json",
        )
        self.assertIn(response.status_code, (401, 403))

    def test_non_staff_users_cannot_sign_uploads(self):
        sign_in(self.client, fake_cognito.make_user("shopper@example.com"))

        response = self.client.post(
            self.url,
            {"kind": "image", "content_type": "image/jpeg", "size": 1024},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_staff_get_a_helpful_error_for_bad_types(self):
        sign_in(self.client, fake_cognito.make_user(
            "boss@example.com", groups=(cognito.STAFF_GROUP, cognito.OWNERS_GROUP)))

        response = self.client.post(
            self.url,
            {"kind": "video", "content_type": "video/quicktime", "size": 1024},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("MP4", response.json()["detail"])


# The Django admin's tests went with the admin itself: InventoryGroupTests,
# InventoryManagerAccessTests, CreateInventoryUserTests, StaffAdministrationTests,
# SuperuserUnaffectedTests and NotificationModelTests. Every one asserted the behaviour
# of a page or a model that no longer exists.
#
# What StaffAdministrationTests was really protecting -- that nobody outside `owners`
# may reach another person's account -- is in `tests_staff_accounts.py`, asserted
# against Cognito groups. It is refused there before a page is reached at all, rather
# than by a filtered queryset, a narrowed fieldset, a save_model hook and a permission
# hook all agreeing with each other.


def make_manager(username="manager", password="inventory-pw-12345"):
    """Somebody in the inventory-managers group.

    Still returns the password so the call sites that unpack two values keep working.
    Nothing stores one any more -- Cognito holds it, and the fake holds nothing at all.
    """
    user = fake_cognito.make_user(
        f"{username}@example.com", name="Inventory Manager",
        groups=(cognito.STAFF_GROUP, cognito.INVENTORY_GROUP),
    )
    return user, password




class SlugTests(DynamoReset, SimpleTestCase):
    def test_slug_is_built_from_the_words_a_buyer_would_search(self):
        car = make_car("SLUG-1", brand="Daihatsu", model_name="Tanto", grade="X",
                       manufacture_year=2008)
        self.assertEqual(car.slug, "2008-daihatsu-tanto-x")
        self.assertEqual(car.get_absolute_url(), "/cars/2008-daihatsu-tanto-x")

    def test_identical_cars_get_distinct_slugs(self):
        first = make_car("SLUG-2", brand="Honda", model_name="N-Box", grade="G",
                         manufacture_year=2020)
        second = make_car("SLUG-3", brand="Honda", model_name="N-Box", grade="G",
                          manufacture_year=2020)
        self.assertNotEqual(first.slug, second.slug)
        self.assertEqual(second.slug, f"{first.slug}-2")

    def test_slug_is_not_rewritten_when_the_car_is_edited(self):
        """A URL that moves when someone fixes a typo breaks every shared link."""
        car = make_car("SLUG-4", brand="Toyota", model_name="Aqua", manufacture_year=2017)
        original = car.slug

        car.grade = "S"
        car.model_name = "Aqua Hybrid"
        car.save()

        car.refresh()
        self.assertEqual(car.slug, original)


class DiscoveryFileTests(DynamoReset, SimpleTestCase):
    def test_robots_txt_is_served_and_points_at_the_sitemap(self):
        """It used to 403: the private bucket answered AccessDenied for a file that was
        never uploaded, and Lighthouse scored that "not applicable" rather than failing."""
        response = self.client.get("/robots.txt")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Sitemap: https://dakkamotors.com/sitemap.xml", body)
        self.assertIn("Disallow: /api/staff/", body)

    def test_sitemap_lists_available_cars_and_omits_sold_ones(self):
        available = make_car("SITE-1", manufacture_year=2019)
        sold = make_car("SITE-2", manufacture_year=2011, status=CarStatus.SOLD)

        body = self.client.get("/sitemap.xml").content.decode()

        self.assertIn(available.get_absolute_url(), body)
        self.assertNotIn(sold.get_absolute_url(), body)
        self.assertIn('hreflang="ja"', body)

    def test_llms_txt_describes_the_business_and_stock(self):
        make_car("LLM-1", brand="Suzuki", model_name="Every", manufacture_year=2019,
                 price_jpy=450000)

        body = self.client.get("/llms.txt").content.decode()

        self.assertIn("Hamura", body)
        self.assertIn("080-9282-3601", body)
        self.assertIn("Suzuki Every", body)
        self.assertIn("450,000", body)


@mock.patch("cars.pages.asset_tags", return_value="")
class RenderedPageTests(DynamoReset, SimpleTestCase):
    def test_home_has_a_local_title_and_dealer_schema(self, _tags):
        body = self.client.get("/").content.decode()

        self.assertIn("<title>Used Cars in Hamura, Tokyo | Dakka Motors</title>", body)
        self.assertIn('"@type":"AutoDealer"', body)
        self.assertIn('"postalCode":"205-0023"', body)
        self.assertIn('rel="canonical" href="https://dakkamotors.com/"', body)

    def test_each_car_gets_its_own_title_and_description(self, _tags):
        """The whole point: every URL used to return the same generic document."""
        first = make_car("PAGE-1", brand="Daihatsu", model_name="Tanto",
                         manufacture_year=2008, price_jpy=250000)
        second = make_car("PAGE-2", brand="Honda", model_name="N-Box",
                          manufacture_year=2020, price_jpy=900000)

        a = self.client.get(first.get_absolute_url()).content.decode()
        b = self.client.get(second.get_absolute_url()).content.decode()

        self.assertIn("2008 Daihatsu Tanto X for sale in Hamura", a)
        self.assertIn("2020 Honda N-Box X for sale in Hamura", b)
        self.assertNotEqual(
            re.search(r"<title>(.*?)</title>", a).group(1),
            re.search(r"<title>(.*?)</title>", b).group(1),
        )

    def test_car_page_carries_vehicle_structured_data(self, _tags):
        car = make_car("PAGE-3", brand="Daihatsu", model_name="Tanto",
                       manufacture_year=2008, price_jpy=250000)

        body = self.client.get(car.get_absolute_url()).content.decode()

        self.assertIn('"@type":"Car"', body)
        self.assertIn('"price":"250000"', body)
        self.assertIn('"priceCurrency":"JPY"', body)
        self.assertIn("https://schema.org/InStock", body)
        self.assertIn('"@type":"BreadcrumbList"', body)

    def test_sold_cars_stay_reachable_but_leave_the_index(self, _tags):
        car = make_car("PAGE-4", status=CarStatus.SOLD)

        response = self.client.get(car.get_absolute_url())
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('name="robots" content="noindex, follow"', body)
        self.assertIn("https://schema.org/SoldOut", body)

    def test_japanese_is_served_when_requested(self, _tags):
        make_car("PAGE-5")

        body = self.client.get("/?lang=ja").content.decode()

        self.assertIn('<html lang="ja"', body)
        self.assertIn("羽村市", body)
        self.assertIn('property="og:locale" content="ja_JP"', body)

    def test_page_embeds_the_data_the_app_needs_to_paint(self, _tags):
        """Without this the first frame is a Loading line that then reflows into the
        whole page - measured at 0.309 CLS, inside Lighthouse's poor band."""
        car = make_car("PAGE-6", brand="Daihatsu", model_name="Tanto")

        body = self.client.get(car.get_absolute_url()).content.decode()

        self.assertIn('<script id="initial-data" type="application/json">', body)
        self.assertIn('"chassis_number": "PAGE-6"', body.replace('":"', '": "'))

    def test_embedded_data_cannot_break_out_of_its_script_block(self, _tags):
        hostile = "</script><script>alert(1)</script>"
        car = make_car("PAGE-7", description_en=hostile)

        body = self.client.get(car.get_absolute_url()).content.decode()
        after_marker = body.split('id="initial-data"', 1)[1]
        payload = after_marker.split("</script>", 1)[0]

        # The closing tag inside the data is escaped, so the block ends where we intend.
        self.assertNotIn("<script>alert(1)", payload)
        self.assertIn("u003c/script", payload)

    def test_unknown_car_is_a_real_404(self, _tags):
        self.assertEqual(self.client.get("/cars/no-such-car").status_code, 404)

    def test_old_numeric_urls_redirect_permanently(self, _tags):
        """301 passes on whatever ranking the numeric URL already earned."""
        car = make_car("PAGE-8", brand="Toyota", model_name="Aqua", manufacture_year=2017)

        response = self.client.get(f"/cars/{car.car_id}")

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], car.get_absolute_url())


class SlugApiTests(DynamoReset, SimpleTestCase):
    def test_api_resolves_a_car_by_slug(self):
        car = make_car("API-SLUG", brand="Daihatsu", model_name="Tanto",
                       manufacture_year=2008)

        response = self.client.get(f"/api/cars/{car.slug}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["slug"], car.slug)

    def test_api_still_resolves_a_car_by_numeric_id(self):
        """Links shared before slugs existed must keep working."""
        car = make_car("API-ID")

        response = self.client.get(f"/api/cars/{car.car_id}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], car.car_id)


def make_customer(email="buyer@example.com", password="customer-pw-1234",
                  phone="080-1111-2222", name="Test Buyer"):
    """A customer, as the app sees one after a sign-in.

    The password comes back unchanged for the tests that re-submit it to an endpoint;
    it is no longer a credential anything here can check, because Cognito owns that.
    """
    user = fake_cognito.make_user(email, name=name, phone=phone)
    return user, password


def make_schedule(weekday=None, start="18:30", end="19:00", capacity=2, **kwargs):
    """A rule on a weekday, defaulting to tomorrow's so generated slots are future."""
    if weekday is None:
        weekday = (timezone.localdate() + datetime.timedelta(days=1)).weekday()
    hh, mm = start.split(":")
    eh, em = end.split(":")
    return schedule_store.create(
        weekday=weekday,
        start_time=datetime.time(int(hh), int(mm)),
        end_time=datetime.time(int(eh), int(em)),
        capacity=capacity,
        **kwargs,
    )


_slot_sequence = itertools.count(1)


def future_slot(capacity=2, days=3, hour=15, is_open=True, schedule_id=None):
    """A concrete slot comfortably beyond the lead time.

    A slot's id is derived from (schedule, start time), which is what makes generating
    them idempotent -- so two fixtures wanting distinct slots at the same instant need
    distinct rules. The counter supplies one unless the caller cares.
    """
    starts = timezone.localtime(timezone.now()) + datetime.timedelta(days=days)
    starts = starts.replace(hour=hour, minute=0, second=0, microsecond=0)
    sid = schedule_id or f"fixture-{next(_slot_sequence)}"
    slot_store.ensure(
        schedule_id=sid,
        starts_at=starts,
        ends_at=starts + datetime.timedelta(minutes=30),
        capacity=capacity,
    )
    slot = slot_store.get(store_keys.slot_id(sid, starts))
    if not is_open:
        slot = slot_store.set_open(slot.slot_id, False)
    return slot


class _CustomerRef:
    """What the store snapshots onto a booking. Mirrors booking._CustomerRef."""

    def __init__(self, user):
        # Through `identity` rather than by hand, so this fixture cannot drift from the
        # snapshot the real booking path takes.
        self.sub = identity.sub_of(user)
        self.email = identity.email_of(user)
        self.phone = identity.phone_of(user)
        self._name = identity.full_name_of(user)

    def get_full_name(self):
        return self._name


def slots_in_store(days=90):
    """Every materialised slot in a generous window."""
    now = timezone.now()
    return slot_store.between(now - datetime.timedelta(days=days),
                              now + datetime.timedelta(days=days))


def make_booking(customer, slot, car=None, car_label=None, now=None):
    """A booking written straight to the store, bypassing the domain rules.

    Replaces `TestDriveBooking.objects.create(...)`. Uses a generous limit because these
    are fixtures setting up a scenario, not exercising the cap.
    """
    now = now or timezone.now()
    customer_store.ensure(sub=identity.sub_of(customer),
                          email=identity.email_of(customer), now=now)
    booking = booking_store.create(
        customer=_CustomerRef(customer), slot=slot, car=car, now=now, max_active=99,
    )
    if car_label is not None and not booking.car_label:
        booking.update(actions=[type(booking).car_label.set(car_label)])
        booking.refresh()
    return booking


class SlotGenerationTests(DynamoReset, SimpleTestCase):
    def test_generation_creates_one_slot_per_matching_day(self):
        schedule = make_schedule(capacity=2)

        booking_rules.ensure_slots(horizon_days=14)

        slots = [s for s in slots_in_store() if s.schedule_id == schedule.schedule_id]
        self.assertEqual(len(slots), 2)  # one per week over a fortnight
        self.assertTrue(all(s.capacity == 2 for s in slots))

    def test_generation_is_idempotent(self):
        make_schedule()
        booking_rules.ensure_slots(horizon_days=14)
        before = len(slots_in_store())

        booking_rules.ensure_slots(horizon_days=14)

        self.assertEqual(len(slots_in_store()), before)

    def test_regenerating_does_not_reopen_a_slot_staff_closed(self):
        """The whole point of materialising slots: staff overrides must survive."""
        make_schedule()
        booking_rules.ensure_slots(horizon_days=14)
        slot = slots_in_store()[0]
        slot_store.set_open(slot.slot_id, False)

        booking_rules.ensure_slots(horizon_days=14)

        self.assertFalse(slot_store.get(slot.slot_id).is_open)

    def test_inactive_rules_generate_nothing(self):
        make_schedule(is_active=False)

        booking_rules.ensure_slots(horizon_days=14)

        self.assertEqual(slots_in_store(), [])

    def test_rule_validity_window_is_respected(self):
        yesterday = timezone.localdate() - datetime.timedelta(days=1)
        make_schedule(ends_on=yesterday)

        booking_rules.ensure_slots(horizon_days=14)

        self.assertEqual(slots_in_store(), [])

    def test_slot_capacity_is_a_snapshot_not_a_live_lookup(self):
        """Editing a rule must not shrink an evening people already booked."""
        schedule = make_schedule(capacity=2)
        booking_rules.ensure_slots(horizon_days=14)

        schedule_store.update(schedule, weekday=schedule.weekday,
                              start_time=schedule.start_time,
                              end_time=schedule.end_time,
                              capacity=1, is_active=True)

        self.assertTrue(all(s.capacity == 2 for s in slots_in_store()))


class BookingRuleTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.user, _ = make_customer()
        self.car = make_car("BOOK-1", brand="Daihatsu", model_name="Tanto")

    def test_booking_takes_a_seat(self):
        slot = future_slot(capacity=2)

        booking_rules.create_booking(user=self.user, slot_id=slot.slot_id, car=self.car)

        slot.refresh()
        self.assertEqual(slot.seats_left, 1)

    def test_car_label_is_snapshotted(self):
        """Deleting a sold car wipes its photos; the booking must still make sense."""
        slot = future_slot()
        booking = booking_rules.create_booking(
            user=self.user, slot_id=slot.slot_id, car=self.car
        )

        self.car.delete()

        booking.refresh()
        # The booking keeps the id and the label, not a reference. Nothing cascades, so
        # the appointment survives the car being sold and removed - which is the whole
        # reason car_label exists.
        self.assertIn("Daihatsu Tanto", booking.car_label)

    def test_a_slot_in_the_past_cannot_be_booked(self):
        past = future_slot(days=-2)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=past.slot_id)

    def test_a_slot_inside_the_lead_time_cannot_be_booked(self):
        starts = timezone.now() + datetime.timedelta(minutes=10)
        slot_store.ensure(schedule_id="lead-time", starts_at=starts,
                          ends_at=starts + datetime.timedelta(minutes=30), capacity=1)
        soon = slot_store.get(store_keys.slot_id("lead-time", starts))

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=soon.slot_id)

    def test_a_closed_slot_cannot_be_booked(self):
        closed = future_slot(is_open=False)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=closed.slot_id)

    def test_a_slot_beyond_the_horizon_cannot_be_booked(self):
        far = future_slot(days=booking_rules.HORIZON_DAYS + 5)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=far.slot_id)

    def test_capacity_is_enforced(self):
        slot = future_slot(capacity=1)
        other, _ = make_customer("other@example.com")
        booking_rules.create_booking(user=other, slot_id=slot.slot_id)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=slot.slot_id)

    def test_the_same_customer_cannot_take_two_seats_in_one_slot(self):
        slot = future_slot(capacity=3)
        booking_rules.create_booking(user=self.user, slot_id=slot.slot_id)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=slot.slot_id)

    def test_active_booking_limit(self):
        for day in range(booking_rules.MAX_ACTIVE_BOOKINGS):
            slot = future_slot(days=day + 2)
            booking_rules.create_booking(user=self.user, slot_id=slot.slot_id)

        one_more = future_slot(days=20)
        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=one_more.slot_id)

    def test_cancelling_frees_the_seat(self):
        slot = future_slot(capacity=1)
        booking = booking_rules.create_booking(user=self.user, slot_id=slot.slot_id)

        booking_rules.cancel_booking(user=self.user, booking_id=booking.booking_id)

        slot.refresh()
        self.assertEqual(slot.seats_left, 1)

    def test_cancelling_frees_the_limit_too(self):
        slot = future_slot()
        booking = booking_rules.create_booking(user=self.user, slot_id=slot.slot_id)
        booking_rules.cancel_booking(user=self.user, booking_id=booking.booking_id)

        self.assertEqual(len(booking_rules.active_bookings_for(self.user)), 0)

    def test_rescheduling_moves_the_seat(self):
        first = future_slot(days=3, capacity=1)
        second = future_slot(days=5, capacity=1)
        booking = booking_rules.create_booking(user=self.user, slot_id=first.slot_id)

        booking_rules.reschedule_booking(
            user=self.user, booking_id=booking.booking_id, slot_id=second.slot_id
        )

        first.refresh()
        second.refresh()
        self.assertEqual(first.seats_left, 1)
        self.assertEqual(second.seats_left, 0)

    def test_cannot_reschedule_into_a_full_slot(self):
        first = future_slot(days=3, capacity=1)
        full = future_slot(days=5, capacity=1)
        other, _ = make_customer("other@example.com")
        booking_rules.create_booking(user=other, slot_id=full.slot_id)
        booking = booking_rules.create_booking(user=self.user, slot_id=first.slot_id)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.reschedule_booking(
                user=self.user, booking_id=booking.booking_id, slot_id=full.slot_id
            )

    def test_cannot_reschedule_into_the_past(self):
        booking = booking_rules.create_booking(
            user=self.user, slot_id=future_slot(days=3).slot_id
        )
        past = future_slot(days=-1)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.reschedule_booking(
                user=self.user, booking_id=booking.booking_id, slot_id=past.slot_id
            )

    def test_one_customer_cannot_touch_anothers_booking(self):
        """A booking id in a URL must not be enough to reach a stranger's appointment."""
        owner, _ = make_customer("owner@example.com")
        stranger, _ = make_customer("stranger@example.com")
        booking = booking_rules.create_booking(user=owner, slot_id=future_slot().slot_id)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.cancel_booking(user=stranger, booking_id=booking.booking_id)

        booking.refresh()
        self.assertTrue(booking.is_active)


class BookingApiTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.user, self.password = make_customer()
        self.car = make_car("API-BOOK", brand="Honda", model_name="N-Box")

    def login(self):
        sign_in(self.client, self.user)

    def test_anonymous_visitors_can_see_availability(self):
        """Making someone register before they can see if a time suits loses them."""
        future_slot()

        response = self.client.get("/api/test-drive/slots/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 1)

    def test_past_and_closed_slots_are_not_offered(self):
        future_slot(days=3)
        future_slot(days=-3)
        future_slot(days=4, is_open=False)

        results = self.client.get("/api/test-drive/slots/").json()["results"]

        self.assertEqual(len(results), 1)

    def test_a_full_slot_is_not_offered(self):
        slot = future_slot(capacity=1)
        booking_rules.create_booking(user=self.user, slot_id=slot.slot_id)

        results = self.client.get("/api/test-drive/slots/").json()["results"]

        self.assertEqual(results, [])

    def test_anonymous_visitors_cannot_book(self):
        slot = future_slot()

        response = self.client.post(
            "/api/test-drive/bookings/",
            {"slot": slot.slot_id, "car": self.car.slug},
            content_type="application/json",
        )

        self.assertIn(response.status_code, (401, 403))

    def test_booking_through_the_api(self):
        self.login()
        slot = future_slot()

        response = self.client.post(
            "/api/test-drive/bookings/",
            {"slot": slot.slot_id, "car": self.car.slug},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertIn("Honda N-Box", response.json()["car_label"])

    def test_api_refuses_a_past_slot_even_if_asked_directly(self):
        """The list hides them; this is what actually prevents it."""
        self.login()
        past = future_slot(days=-2)

        response = self.client.post(
            "/api/test-drive/bookings/",
            {"slot": past.slot_id},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_customers_only_see_their_own_bookings(self):
        other, _ = make_customer("other@example.com")
        booking_rules.create_booking(user=other, slot_id=future_slot(days=3).slot_id)
        self.login()
        booking_rules.create_booking(user=self.user, slot_id=future_slot(days=5).slot_id)

        results = self.client.get("/api/test-drive/bookings/").json()["results"]

        self.assertEqual(len(results), 1)

    def test_cancelling_someone_elses_booking_is_refused(self):
        other, _ = make_customer("other@example.com")
        booking = booking_rules.create_booking(user=other, slot_id=future_slot().slot_id)
        self.login()

        response = self.client.post(f"/api/test-drive/bookings/{booking.booking_id}/cancel/")

        self.assertEqual(response.status_code, 400)
        booking.refresh()
        self.assertTrue(booking.is_active)


class AccountPageTests(DynamoReset, SimpleTestCase):
    def test_account_routes_render_but_are_not_indexable(self):
        for path in ("/account", "/account/login", "/account/register"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(
                    'name="robots" content="noindex, nofollow"',
                    response.content.decode(),
                )

    def test_robots_disallows_account_pages(self):
        self.assertIn("Disallow: /account", self.client.get("/robots.txt").content.decode())

    def test_book_test_drive_page_names_the_car(self):
        car = make_car("TD-PAGE", brand="Toyota", model_name="Aqua")

        body = self.client.get(f"/cars/{car.slug}/test-drive").content.decode()

        self.assertIn("Toyota Aqua", body)
        self.assertIn('name="robots" content="noindex, nofollow"', body)


@override_settings(
    OUTBOX_BUCKET="test-outbox",
    MAIL_FROM="noreply@dakkamotors.com",
    MAIL_FROM_NAME="Dakka Motors",
    MAIL_REPLY_TO="owner@example.com",
    STAFF_ALERT_EMAIL="staff@example.com",
)
class QueueEmailTests(SimpleTestCase):
    """Django cannot send mail itself - no route out of the VPC - so 'sending' means
    writing one object to S3 for a Lambda outside the VPC to pick up."""

    def test_a_message_is_written_to_the_outbox(self):
        with mock.patch("cars.mail.boto3.client") as client:
            queued = mail.queue_email(
                to="buyer@example.com", subject="Hello", html="<p>Hi</p>", text="Hi"
            )

        self.assertTrue(queued)
        put = client.return_value.put_object
        put.assert_called_once()
        kwargs = put.call_args.kwargs
        self.assertEqual(kwargs["Bucket"], "test-outbox")
        self.assertTrue(kwargs["Key"].startswith("outbox/"))

        body = json.loads(kwargs["Body"].decode("utf-8"))
        self.assertEqual(body["to"], ["buyer@example.com"])
        self.assertEqual(body["from"], "noreply@dakkamotors.com")
        self.assertEqual(body["replyTo"], "owner@example.com")
        self.assertEqual(body["subject"], "Hello")

    def test_queuing_failures_never_reach_the_caller(self):
        """A booking must not fail because an email could not be queued."""
        with mock.patch("cars.mail.boto3.client") as client:
            client.return_value.put_object.side_effect = RuntimeError("S3 is down")
            queued = mail.queue_email(to="a@b.com", subject="x", html="y")

        self.assertFalse(queued)


class UnconfiguredEmailTests(SimpleTestCase):
    @override_settings(OUTBOX_BUCKET="", MAIL_FROM="")
    def test_nothing_is_sent_when_email_is_not_configured(self):
        """Local development must not be able to email a real customer by accident."""
        with mock.patch("cars.mail.boto3.client") as client:
            queued = mail.queue_email(to="a@b.com", subject="x", html="y")

        self.assertFalse(queued)
        client.assert_not_called()


class BookingApprovalTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.user, _ = make_customer()
        self.car = make_car("APPROVE-1", brand="Daihatsu", model_name="Tanto")

    def test_a_new_booking_is_awaiting_confirmation(self):
        booking = booking_rules.create_booking(
            user=self.user, slot_id=future_slot().slot_id, car=self.car
        )

        self.assertEqual(booking.status, BookingStatus.PENDING)
        self.assertTrue(booking.is_active)

    def test_a_pending_booking_holds_the_seat(self):
        """Otherwise two customers could both be pending for one place, and one would
        have to be turned away after the fact."""
        slot = future_slot(capacity=1)
        booking_rules.create_booking(user=self.user, slot_id=slot.slot_id)
        other, _ = make_customer("other@example.com")

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=other, slot_id=slot.slot_id)

        slot.refresh()
        self.assertEqual(slot.seats_left, 0)

    def test_pending_bookings_count_towards_the_limit(self):
        for day in range(booking_rules.MAX_ACTIVE_BOOKINGS):
            booking_rules.create_booking(user=self.user, slot_id=future_slot(days=day + 2).slot_id)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=future_slot(days=20).slot_id)

    def test_confirming_records_the_time_and_status(self):
        booking = booking_rules.create_booking(user=self.user, slot_id=future_slot().slot_id)

        booking_rules.confirm_booking(booking)

        booking.refresh()
        self.assertEqual(booking.status, BookingStatus.CONFIRMED)
        self.assertIsNotNone(booking.confirmed_at)


@override_settings(
    OUTBOX_BUCKET="test-outbox",
    MAIL_FROM="noreply@dakkamotors.com",
    STAFF_ALERT_EMAIL="staff@example.com",
)
class BookingEmailTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.user, _ = make_customer(email="buyer@example.com")
        self.car = make_car("MAIL-1", brand="Honda", model_name="N-Box")

    def queued_messages(self, fn):
        """Run fn and return the messages it queued.

        on_commit callbacks do not fire inside a TestCase's transaction, so they have to
        be captured explicitly - without this the emails would silently never be checked.
        """
        with mock.patch("cars.mail.boto3.client") as client:
            fn()
            calls = client.return_value.put_object.call_args_list
        return [json.loads(call.kwargs["Body"].decode("utf-8")) for call in calls]

    def test_booking_alerts_staff_and_says_it_is_not_confirmed(self):
        messages = self.queued_messages(
            lambda: booking_rules.create_booking(
                user=self.user, slot_id=future_slot().slot_id, car=self.car
            )
        )

        self.assertEqual(len(messages), 1)
        alert = messages[0]
        self.assertEqual(alert["to"], ["staff@example.com"])
        self.assertIn("Honda N-Box", alert["subject"])
        self.assertIn("not been told it is confirmed", alert["text"])
        # Replying to the alert should reach the customer, not a noreply void.
        self.assertEqual(alert["replyTo"], "buyer@example.com")

    def test_booking_does_not_email_the_customer(self):
        """They are told on screen that it is awaiting confirmation; the email only
        goes out once staff accept."""
        messages = self.queued_messages(
            lambda: booking_rules.create_booking(user=self.user, slot_id=future_slot().slot_id)
        )

        self.assertNotIn("buyer@example.com", [to for m in messages for to in m["to"]])

    def test_confirming_emails_the_customer_with_time_address_and_phone(self):
        booking = booking_rules.create_booking(
            user=self.user, slot_id=future_slot().slot_id, car=self.car
        )

        messages = self.queued_messages(lambda: booking_rules.confirm_booking(booking))

        self.assertEqual(len(messages), 1)
        confirmation = messages[0]
        self.assertEqual(confirmation["to"], ["buyer@example.com"])
        self.assertIn("confirmed", confirmation["subject"].lower())
        self.assertIn("Hamura", confirmation["text"])
        self.assertIn("205-0023", confirmation["text"])
        self.assertIn("080-9282-3601", confirmation["text"])

    def test_confirming_twice_does_not_email_twice(self):
        booking = booking_rules.create_booking(user=self.user, slot_id=future_slot().slot_id)
        booking_rules.confirm_booking(booking)

        messages = self.queued_messages(lambda: booking_rules.confirm_booking(booking))

        self.assertEqual(messages, [])

    def test_staff_cancelling_tells_the_customer(self):
        booking = booking_rules.create_booking(user=self.user, slot_id=future_slot().slot_id)

        messages = self.queued_messages(lambda: booking_rules.cancel_by_staff(booking))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["to"], ["buyer@example.com"])
        self.assertIn("cancelled", messages[0]["subject"].lower())

    def test_a_customer_cancelling_their_own_booking_sends_nothing(self):
        """They already know - an email would just be noise."""
        booking = booking_rules.create_booking(user=self.user, slot_id=future_slot().slot_id)

        messages = self.queued_messages(
            lambda: booking_rules.cancel_booking(user=self.user, booking_id=booking.booking_id)
        )

        self.assertEqual(messages, [])


MAIL_SETTINGS = dict(
    OUTBOX_BUCKET="test-outbox",
    MAIL_FROM="noreply@dakkamotors.com",
    STAFF_ALERT_EMAIL="staff@example.com",
)


def register(client, email="new@example.com", password="simple", **extra):
    payload = {"name": "Yuki Tanaka", "email": email, "phone": "080-1234-5678",
               "password": password}
    payload.update(extra)
    return client.post("/api/auth/register/", payload, content_type="application/json")


class PrimaryImageFallbackTests(DynamoReset, SimpleTestCase):
    def test_falls_back_to_lowest_order_when_nothing_is_flagged(self):
        car = make_car("FALLBACK")
        attach_image(car, "third.gif", order=3)
        lowest = attach_image(car, "first.gif", order=1)

        # Re-read: the listing-card reference lives on the car item and is refreshed
        # when a photo is added, so a copy fetched beforehand is stale by design.
        self.assertEqual(car_store.get(car.car_id).primary_image.image_id,
                         lowest.image_id)

    def test_returns_none_when_there_are_no_images(self):
        self.assertIsNone(make_car("EMPTY").primary_image)


class EmailTemplateTests(FakeCognito, DynamoReset, SimpleTestCase):
    """The shell every message is rendered into.

    The point of these is that the logo survives the two things that usually break it:
    images being blocked, and Japanese content.
    """

    def render(self, **kwargs):
        kwargs.setdefault("heading", "Confirm your email")
        kwargs.setdefault("body", email_theme.paragraph("Body copy."))
        return email_theme.render(**kwargs)

    def test_the_plate_is_drawn_by_the_client_not_the_image(self):
        """With images off the cell still has its yellow ground, so the mark shows."""
        html = self.render()

        self.assertIn(f'bgcolor="{email_theme.PLATE}"', html)
        self.assertIn(f"background-color:{email_theme.PLATE}", html)

    def test_the_letter_has_alt_text_to_fall_back_to(self):
        html = self.render()

        self.assertIn('alt="D"', html)
        self.assertIn("/assets/email-mark-d.png", html)

    def test_the_logo_image_is_an_absolute_url(self):
        """A relative src resolves against nothing in a mail client."""
        html = self.render()

        self.assertIn(f'src="{seo.SITE_URL}/assets/email-mark-d.png"', html)

    def test_the_preheader_is_hidden_but_present(self):
        html = self.render(preheader="One click finishes your account.")

        self.assertIn("One click finishes your account.", html)
        self.assertIn("display:none", html)

    def test_japanese_renders_with_a_japanese_address(self):
        html = self.render(language="ja", heading="メールアドレスのご確認")

        self.assertIn('lang="ja"', html)
        self.assertIn(seo.BUSINESS["locality_ja"], html)
        self.assertIn("Hiragino", html)

    def test_every_message_carries_a_plain_text_alternative(self):
        """A text part is what text-only clients show, and its absence scores as spam."""
        # Never saved: the renderer reads `name` and `language` off it and nothing else,
        # and a stored item would need a Cognito user to go with it.
        pending = StorePending(email="new@example.com", name="Yuki",
                               phone="080-1234-5678", language="en")
        with override_settings(**MAIL_SETTINGS):
            with mock.patch("cars.mail.boto3.client") as client:
                mail.send_verification_email(pending, "https://dakkamotors.com/v?token=abc")
                body = json.loads(client.return_value.put_object.call_args.kwargs["Body"].decode())

        self.assertTrue(body["text"].strip())
        self.assertIn("https://dakkamotors.com/v?token=abc", body["text"])
        self.assertNotIn("<", body["text"])

    def test_a_customer_name_cannot_inject_markup(self):
        """The staff alert interpolates a name the customer chose."""
        user, _ = make_customer("sneaky@example.com",
                                name="<script>alert(1)</script>")
        slot = future_slot()
        booking = make_booking(user, slot, car_label="Tanto")

        with override_settings(**MAIL_SETTINGS):
            with mock.patch("cars.mail.boto3.client") as client:
                mail.notify_staff_of_booking(booking)
                body = json.loads(client.return_value.put_object.call_args.kwargs["Body"].decode())

        self.assertNotIn("<script>", body["html"])
        self.assertIn("&lt;script&gt;", body["html"])


class CarQuestionStorageTests(FakeCognito, DynamoReset, SimpleTestCase):
    """What survives of the schema-level guarantees.

    Two tests were deleted here rather than ported, because what they asserted no longer
    exists: `published_question_has_an_answer` was a database CheckConstraint, and
    DynamoDB has no table-level check. Their intent moved to `tests_store_qa.py` --
    `test_an_unanswered_question_is_not_published` for the ConditionExpression that
    guards the write, and `PublishedFlagIsWrittenInOnePlaceTests` for the exclusivity
    that makes one guarded writer sufficient. That is a genuinely weaker arrangement and
    it is recorded in `qa.py`'s docstring rather than quietly dropped.
    """

    def setUp(self):
        super().setUp()
        self.car = make_car("QA-1", brand="Daihatsu", model_name="Tanto")

    def test_closing_an_account_keeps_the_published_pair(self):
        """A published pair is indexed page content; it must outlive the asker.

        This used to rest on `on_delete=SET_NULL`. It is now structural: the question
        holds a subject identifier, not a foreign key, so there is no cascade to get
        wrong in the first place.
        """
        user, _ = make_customer("asker@example.com")
        question = make_question(self.car, customer=user,
                                 question="Any service history?",
                                 answer="Full history.", published=True)

        fake_cognito.close_account(user)

        question.refresh()
        self.assertTrue(question.is_published)
        self.assertEqual(question.question, "Any service history?")

    def test_state_reads_as_the_work_still_to_do(self):
        question = make_question(self.car, question="Colour?")
        self.assertEqual(question.state, "Needs an answer")

        question, _ = question_store.record_answer(
            question=question, answer="Pearl white.", staff_sub=None,
            now=timezone.now())
        self.assertEqual(question.state, "Answered, not public")

        question = question_store.publish(question, now=timezone.now(),
                                          bump_car=False)
        self.assertEqual(question.state, "Published")


@override_settings(**MAIL_SETTINGS)
class AskingTests(FakeCognito, DynamoReset, ClearsThrottleMixin, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.car = make_car("ASK-1", brand="Daihatsu", model_name="Tanto")
        self.user, _ = make_customer("asker@example.com")

    def ask(self, **extra):
        payload = {"car": self.car.slug, "question": "Has it had one owner?"}
        payload.update(extra)
        with mock.patch("cars.mail.boto3.client"):
            return self.client.post("/api/questions/", payload,
                                    content_type="application/json")

    def test_a_signed_in_customer_can_ask(self):
        sign_in(self.client, self.user)

        response = self.ask()

        self.assertEqual(response.status_code, 201)
        stored = questions_in_store()
        self.assertEqual(len(stored), 1)
        question = stored[0]
        self.assertEqual(question.car_id, self.car.car_id)
        self.assertEqual(question.customer_sub, str(self.user.pk))
        self.assertFalse(question.is_published)
        self.assertIsNone(question.answered_at)

    def test_a_guest_cannot_ask(self):
        self.assertEqual(self.ask().status_code, 403)
        self.assertEqual(questions_in_store(), [])

    def test_asking_alerts_the_shop_and_can_be_replied_to(self):
        sign_in(self.client, self.user)

        with mock.patch("cars.mail.boto3.client") as client:
            self.client.post("/api/questions/",
                             {"car": self.car.slug, "question": "Rust?"},
                             content_type="application/json")
            calls = client.return_value.put_object.call_args_list

        sent = [json.loads(c.kwargs["Body"].decode("utf-8")) for c in calls]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["to"], ["staff@example.com"])
        self.assertEqual(sent[0]["replyTo"], "asker@example.com")

    def test_an_empty_question_is_refused(self):
        sign_in(self.client, self.user)

        self.assertEqual(self.ask(question="   ").status_code, 400)

    def test_an_unknown_car_is_refused(self):
        sign_in(self.client, self.user)

        self.assertEqual(self.ask(car="no-such-car").status_code, 400)

    def test_a_backlog_of_unanswered_questions_is_capped(self):
        sign_in(self.client, self.user)
        for _ in range(qa.MAX_OPEN_QUESTIONS):
            make_question(self.car, customer=self.user, question="?")

        response = self.ask()

        self.assertEqual(response.status_code, 400)
        self.assertIn(str(qa.MAX_OPEN_QUESTIONS), response.json()["detail"])

    def test_an_answered_question_does_not_count_towards_the_cap(self):
        sign_in(self.client, self.user)
        for _ in range(qa.MAX_OPEN_QUESTIONS):
            make_question(self.car, customer=self.user, question="?",
                          answer="Yes.", answered=True)

        self.assertEqual(self.ask().status_code, 201)

    def test_you_only_ever_see_your_own_thread(self):
        other, _ = make_customer("other@example.com")
        make_question(self.car, customer=other, question="Theirs")
        make_question(self.car, customer=self.user, question="Mine")
        sign_in(self.client, self.user)

        results = self.client.get(f"/api/questions/?car={self.car.slug}").json()["results"]

        self.assertEqual([r["question"] for r in results], ["Mine"])


@override_settings(**MAIL_SETTINGS)
class AnsweringTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.car = make_car("ANS-1", brand="Suzuki", model_name="Every")
        self.user, _ = make_customer("asker@example.com")
        self.staff, _ = make_manager()
        self.question = make_question(
            self.car, customer=self.user, question="Any service history?"
        )

    def answer(self, text="Full history, stamped."):
        with mock.patch("cars.mail.boto3.client") as client:
            qa.record_answer(self.question, answer=text, staff=self.staff)
            calls = client.return_value.put_object.call_args_list
        return [json.loads(c.kwargs["Body"].decode("utf-8")) for c in calls]

    def test_answering_emails_the_customer_once_and_rings_the_bell_once(self):
        sent = self.answer()

        self.question.refresh()
        self.assertIsNotNone(self.question.answered_at)
        # A subject identifier now, not a User row: the answer outlives the account that
        # wrote it, the same way the published pair outlives the one that asked.
        self.assertEqual(self.question.answered_by, str(self.staff.pk))
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["to"], ["asker@example.com"])
        self.assertEqual(len(bell(self.user, NotificationKind.QUESTION_ANSWERED)), 1)

    def test_fixing_a_typo_in_an_answer_tells_nobody(self):
        """answered_at, not a non-empty answer field, is what answered means."""
        self.answer("Full histroy, stamped.")

        sent = self.answer("Full history, stamped.")

        self.assertEqual(sent, [])
        self.assertEqual(len(bell(self.user)), 1)
        self.question.refresh()
        self.assertEqual(self.question.answer, "Full history, stamped.")

    def test_a_staff_seeded_question_has_nobody_to_tell(self):
        seeded = make_question(self.car, question="Do you take trade-ins?")

        with mock.patch("cars.mail.boto3.client") as client:
            qa.record_answer(seeded, answer="Yes, bring it in.", staff=self.staff)
            calls = client.return_value.put_object.call_args_list

        self.assertEqual(calls, [])
        self.assertEqual(len(bell(self.user)), 0)
        seeded.refresh()
        self.assertIsNotNone(seeded.answered_at)

    def test_a_blank_answer_does_not_count_as_answering(self):
        sent = self.answer("   ")

        self.assertEqual(sent, [])
        self.question.refresh()
        self.assertIsNone(self.question.answered_at)


@override_settings(**MAIL_SETTINGS)
class PublishingTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.car = make_car("PUB-1", brand="Honda", model_name="N-Box")
        self.user, _ = make_customer("asker@example.com")

    def test_publishing_without_an_answer_is_refused_with_a_reason(self):
        question = make_question(self.car, customer=self.user, question="Colour?")

        with self.assertRaises(qa.QuestionError) as raised:
            qa.publish(question)

        self.assertIn("answer", str(raised.exception).lower())
        question.refresh()
        self.assertFalse(question.is_published)

    def test_publishing_moves_the_cars_updated_at_so_the_sitemap_notices(self):
        question = make_question(self.car, customer=self.user, question="Colour?",
                                 answer="Pearl white.", answered=True)
        self.car.refresh()
        before = self.car.updated_at

        qa.publish(question)

        self.car.refresh()
        self.assertGreater(self.car.updated_at, before)


class PublishedQuestionsAreAnonymousTests(FakeCognito, DynamoReset, SimpleTestCase):
    """The published pair is public content; the person who asked is not."""

    def setUp(self):
        super().setUp()
        self.car = make_car("ANON-1", brand="Daihatsu", model_name="Tanto")
        self.user, _ = make_customer("yuki.tanaka@example.com",
                                     name="Yuki Tanaka")
        self.question = make_question(
            self.car, customer=self.user,
            question="Has it had one owner?", answer="Yes, one owner from new.",
            published=True,
        )

    def test_the_public_payload_carries_exactly_these_keys(self):
        """An equality assertion, so adding `customer` later fails loudly.

        Not even `customer_id`: an id is stable across pages, so publishing one would
        let a reader join up every question the same person asked. That is attribution
        by another name.
        """
        body = self.client.get(f"/api/cars/{self.car.slug}/").json()

        self.assertEqual(
            set(body["questions"][0]),
            {"id", "question", "answer", "language", "created_at", "answered_at"},
        )

    def test_the_rendered_page_never_names_the_asker(self):
        html = self.client.get(f"/cars/{self.car.slug}").content.decode("utf-8")

        self.assertIn("Has it had one owner?", html)
        self.assertIn("one owner from new", html)
        for leak in ("Yuki", "Tanaka", "yuki.tanaka@example.com"):
            self.assertNotIn(leak, html)

    def test_an_unanswered_or_unpublished_question_is_nowhere(self):
        make_question(self.car, customer=self.user,
                      question="Secret pending question")
        make_question(self.car, customer=self.user,
                      question="Answered but private",
                      answer="Not for the page.", answered=True)

        html = self.client.get(f"/cars/{self.car.slug}").content.decode("utf-8")
        api = self.client.get(f"/api/cars/{self.car.slug}/").json()

        self.assertNotIn("Secret pending question", html)
        self.assertNotIn("Answered but private", html)
        self.assertEqual(len(api["questions"]), 1)

    def test_another_cars_questions_do_not_leak_in(self):
        other = make_car("ANON-2", brand="Suzuki", model_name="Alto")
        make_question(other, customer=self.user, question="About the other car",
                      answer="Different car.", published=True)

        api = self.client.get(f"/api/cars/{self.car.slug}/").json()

        self.assertEqual([q["question"] for q in api["questions"]],
                         ["Has it had one owner?"])


class QuestionSeoTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.car = make_car("SEO-1", brand="Honda", model_name="N-Box")
        self.user, _ = make_customer("asker@example.com")

    def publish(self, question, answer, language="en"):
        return make_question(self.car, customer=self.user, question=question,
                             answer=answer, language=language, published=True)

    def ld_json(self, response):
        html = response.content.decode("utf-8")
        raw = html.split('<script type="application/ld+json">')[1].split("</script>")[0]
        return json.loads(raw.replace("\\u003c", "<"))

    def test_a_published_pair_appears_in_the_body_and_the_graph_identically(self):
        self.publish("Is it rust free?", "Yes, the underside is clean.")

        response = self.client.get(f"/cars/{self.car.slug}")
        html = response.content.decode("utf-8")
        faq = [n for n in self.ld_json(response)["@graph"] if n["@type"] == "FAQPage"]

        self.assertIn("Is it rust free?", html)
        self.assertEqual(len(faq), 1)
        entity = faq[0]["mainEntity"][0]
        self.assertEqual(entity["name"], "Is it rust free?")
        self.assertEqual(entity["acceptedAnswer"]["text"], "Yes, the underside is clean.")

    def test_no_faq_node_is_emitted_when_there_is_nothing_published(self):
        """An empty mainEntity is an invalid node, not a harmless one."""
        response = self.client.get(f"/cars/{self.car.slug}")

        types = [n["@type"] for n in self.ld_json(response)["@graph"]]
        self.assertNotIn("FAQPage", types)

    def test_a_question_cannot_break_out_of_the_json_ld_block(self):
        """Customer text now reaches the graph, so this stopped being theoretical."""
        self.publish("</script><img src=x onerror=alert(1)>", "Nice try.")

        html = self.client.get(f"/cars/{self.car.slug}").content.decode("utf-8")

        opened = html.count('<script type="application/ld+json">')
        self.assertEqual(opened, 1)
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        # Escaping the "<" is what matters and is sufficient: with the bracket gone the
        # string "</script>" cannot occur, so the block cannot be terminated early.
        self.assertIn("\\u003c/script>", html)

    def test_the_page_shows_pairs_in_the_language_it_is_served_in(self):
        self.publish("Is it rust free?", "Underside is clean.", language="en")
        self.publish("錆はありますか？", "下回りはきれいです。", language="ja")

        english = self.rendered_body("en")
        japanese = self.rendered_body("ja")

        self.assertIn("Is it rust free?", english)
        self.assertNotIn("錆はありますか？", english)
        self.assertIn("錆はありますか？", japanese)
        self.assertNotIn("Is it rust free?", japanese)

    def test_pairs_in_the_other_language_are_shown_rather_than_an_empty_section(self):
        self.publish("錆はありますか？", "下回りはきれいです。", language="ja")

        self.assertIn("錆はありますか？", self.rendered_body("en"))

    def rendered_body(self, language):
        """Just what a reader sees.

        Not the whole document: the initial-data payload deliberately carries every
        published pair with its language so the app can re-filter when someone uses the
        language switch, without going back to the server.
        """
        html = self.client.get(
            f"/cars/{self.car.slug}?lang={language}"
        ).content.decode("utf-8")
        return html.split('<div id="root">')[1].split("</div>")[0]

    def test_the_page_costs_the_same_number_of_reads_however_many_pairs(self):
        """A per-question read in a loop is invisible until it is not.

        This used to count SQL queries and catch a `.filter()` defeating a Prefetch. The
        shape of the mistake is unchanged; only the store is. One partition holds the
        car, its images and its published questions in `IMG# < META < Q#` order, so ten
        pairs must cost exactly what one does.
        """
        self.publish("One?", "Yes.")
        baseline = self._reads_for_page()

        for i in range(9):
            self.publish(f"Question {i}?", "Yes.")

        self.assertEqual(self._reads_for_page(), baseline)

    def _reads_for_page(self):
        with count_dynamo_calls() as calls:
            self.client.get(f"/cars/{self.car.slug}")
        return len(calls)


@override_settings(**MAIL_SETTINGS)
class NotificationApiTests(FakeCognito, DynamoReset, SimpleTestCase):
    """A bell is one person's history. The isolation cases carry the weight."""

    def setUp(self):
        super().setUp()
        self.user, _ = make_customer("mine@example.com")
        self.other, _ = make_customer("theirs@example.com")
        sign_in(self.client, self.user)

    def make(self, user=None, kind=NotificationKind.QUESTION_ANSWERED, **extra):
        return notifications.notify(user=user or self.user, kind=kind, **extra)

    def test_you_see_only_your_own(self):
        self.make()
        self.make(user=self.other)

        body = self.client.get("/api/notifications/").json()

        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["unread"], 1)

    def test_a_guest_gets_nothing(self):
        sign_out(self.client)

        self.assertEqual(self.client.get("/api/notifications/").status_code, 403)

    def test_marking_read_clears_the_count(self):
        self.make()
        self.make()

        body = self.client.post("/api/notifications/read/", {},
                                content_type="application/json").json()

        self.assertEqual(body["unread"], 0)
        self.assertEqual(self.client.get("/api/notifications/").json()["unread"], 0)

    def test_marking_someone_elses_notification_read_does_nothing(self):
        theirs = self.make(user=self.other)

        self.client.post("/api/notifications/read/",
                         {"ids": [theirs.notification_id]},
                         content_type="application/json")

        theirs.refresh()
        self.assertIsNone(theirs.read_at)

    def test_a_dedupe_key_means_one_entry_however_often_it_fires(self):
        for _ in range(3):
            self.make(dedupe_key="booking:1:confirmed")

        self.assertEqual(len(bell(self.user)), 1)

    def test_a_closed_account_has_no_bell_to_ring(self):
        self.user.is_active = False

        self.assertIsNone(self.make())

    def test_there_is_nobody_to_tell_when_there_is_no_user(self):
        self.assertIsNone(notifications.notify(user=None,
                                               kind=NotificationKind.QUESTION_ANSWERED))

    def test_read_history_is_capped_on_write_but_unread_is_never_touched(self):
        """The cap must never take something the customer has not seen.

        Expiry is now a TTL rather than a sweep, so the on-write trim exists only to
        stop a pathological account growing an unbounded partition between expiries.
        The property that carried over from the Django version is the important half:
        read rows are candidates, unread rows never are.
        """
        keep = 3
        unread = self.make()
        read_rows = []
        for _ in range(keep + 2):
            row = self.make()
            row.update(actions=[type(row).read_at.set(timezone.now())])
            read_rows.append(row)

        removed = notification_store._trim(str(self.user.pk), keep_rows=keep)

        self.assertEqual(removed, 2)
        surviving = {r.notification_id for r in bell(self.user)}
        self.assertIn(unread.notification_id, surviving,
                      "an unread notification must never be trimmed")
        self.assertEqual(len(surviving), keep + 1)


@override_settings(**MAIL_SETTINGS)
class BookingNotificationTests(FakeCognito, DynamoReset, SimpleTestCase):
    """Booking events reach the bell without sending a second email."""

    def setUp(self):
        super().setUp()
        self.user, _ = make_customer("buyer@example.com")
        self.booking = make_booking(self.user, future_slot(), car_label="Tanto")

    def run_and_capture(self, fn):
        with mock.patch("cars.mail.boto3.client") as client:
            fn()
            calls = client.return_value.put_object.call_args_list
        return [json.loads(c.kwargs["Body"].decode("utf-8")) for c in calls]

    def test_confirming_sends_one_email_and_creates_one_notification(self):
        sent = self.run_and_capture(lambda: booking_rules.confirm_booking(self.booking))

        self.assertEqual(len(sent), 1)
        self.assertEqual(len(bell(self.user, NotificationKind.BOOKING_CONFIRMED)), 1)

    def test_confirming_twice_still_tells_them_once(self):
        self.run_and_capture(lambda: booking_rules.confirm_booking(self.booking))
        self.run_and_capture(lambda: booking_rules.confirm_booking(self.booking))

        self.assertEqual(len(bell(self.user)), 1)

    def test_a_customer_cancelling_their_own_booking_is_not_notified(self):
        """They just did it. Telling them is noise."""
        sent = self.run_and_capture(
            lambda: booking_rules.cancel_booking(
                user=self.user, booking_id=self.booking.booking_id
            )
        )

        self.assertEqual(sent, [])
        self.assertEqual(len(bell(self.user)), 0)

    def test_the_notification_carries_what_it_needs_to_render_later(self):
        """Strings, not foreign keys - a sold car must not blank out someone's history."""
        self.run_and_capture(lambda: booking_rules.confirm_booking(self.booking))

        context = bell(self.user)[0].context
        self.assertEqual(context["car_label"], "Tanto")
        self.assertIn("starts_at", context)

    def test_a_cancelled_confirmation_transaction_leaves_no_notification(self):
        """The bell entry and the status change are one write.

        Under Postgres this was guaranteed by calling notify() inside the transaction
        that confirmed the booking. It is now a single TransactWriteItems, which is a
        storage-layer guarantee rather than a session-scoped one -- so forcing any part
        of it to fail must leave every part untouched.

        The failure is induced by occupying the dedupe key the confirmation will try to
        claim, which is the one condition in that transaction a test can trip from
        outside.
        """
        from .store import keys as _keys
        from .store.models import DedupeGuard

        DedupeGuard(
            pk=_keys.customer_pk(str(self.user.pk)),
            sk=_keys.dedupe_sk(f"booking:{self.booking.booking_id}:confirmed"),
            notification_sk="already-there",
        ).save()

        with self.assertRaises(Exception):
            booking_rules.confirm_booking(self.booking)

        self.booking.refresh()
        self.assertEqual(self.booking.status, BookingStatus.PENDING,
                         "the status must roll back with the bell entry")
        self.assertEqual(len(bell(self.user)), 0)


# The customer account flows used to be tested here. They moved to Cognito, so what
# those classes asserted -- that no User row exists until the link is clicked, that the
# raw token is never stored, that a reset link dies once the password changes -- is now
# asserted against the real flows in `tests_auth.py`, and against the pool and the store
# in `tests_cognito.py`.
#
# Deleted rather than ported, because every one of them reached into Django's auth
# internals: `User.objects`, `PendingRegistration`, `check_password`, the session. None
# of that is what the app does any more.
