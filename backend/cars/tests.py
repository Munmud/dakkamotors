import datetime
import itertools
import io
import json
import pathlib
import re
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from PIL import Image

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from . import authentication
from . import cognito
from . import email_theme
from . import mail
from . import seo
from . import tests_fake_cognito as fake_cognito
from .choices import DERIVATIVE_WIDTHS, CarStatus
from .store import cars as car_store
from .store import images as image_store
from .store import keys as store_keys
from .store import requests as request_store
from .store import videos as video_store
from .store.models import Car as StoreCar
from .tasks import build_derivatives_task
from .tests_fake_cognito import FakeCognito, sign_in
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


def attach_video(car, name, *, order=0):
    """A video row plus an object behind it. No derivatives -- nothing transcodes one."""
    stored = default_storage.save(f"cars/{name}", ContentFile(b"not really an mp4"))
    return video_store.create(car_id=car.car_id, video_name=stored,
                              order=order, now=timezone.now())


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
    def test_list_is_the_stock_available_first_and_never_sold(self):
        """Reserved cars are stock -- somebody may still be deciding -- and for a year
        the front page hid them because the listing asked for one status."""
        reserved = make_car("RESERVED-1", status=CarStatus.RESERVED)
        available = make_car("AVAIL-1", status=CarStatus.AVAILABLE)
        make_car("SOLD-1", status=CarStatus.SOLD)

        response = self.client.get(reverse("car-list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([c["id"] for c in response.json()["results"]],
                         [available.car_id, reserved.car_id])
        self.assertEqual(response.json()["count"], 2)

    def test_the_sold_shelf_lists_sold_cars_only(self):
        make_car("AVAIL-1", status=CarStatus.AVAILABLE)
        sold = make_car("SOLD-1", status=CarStatus.SOLD)

        results = self.client.get(reverse("car-list"), {"status": "sold"}).json()["results"]

        self.assertEqual([c["id"] for c in results], [sold.car_id])

    def test_an_unknown_status_filter_is_the_default_listing(self):
        make_car("AVAIL-1", status=CarStatus.AVAILABLE)
        make_car("SOLD-1", status=CarStatus.SOLD)

        results = self.client.get(reverse("car-list"), {"status": "nope"}).json()["results"]

        self.assertEqual(len(results), 1)

    def test_the_sold_shelf_leads_with_what_sold_most_recently(self):
        """Not with what was added most recently, which is the bug this fixes: eleven
        old cars imported in one afternoon sat above the one that had just sold."""
        old_sale = make_car("SOLD-OLD", status=CarStatus.SOLD)
        car_store.update(old_sale, now=timezone.now(), sold_at="2026-09-20")
        just_added = make_car("SOLD-NEW", status=CarStatus.SOLD)

        results = self.client.get(reverse("car-list"), {"status": "sold"}).json()["results"]

        self.assertEqual([c["id"] for c in results],
                         [old_sale.car_id, just_added.car_id])

    def test_two_dated_sales_come_newest_first(self):
        older = make_car("SOLD-A", status=CarStatus.SOLD)
        car_store.update(older, now=timezone.now(), sold_at="2026-01-05")
        newer = make_car("SOLD-B", status=CarStatus.SOLD)
        car_store.update(newer, now=timezone.now(), sold_at="2026-09-22")

        results = self.client.get(reverse("car-list"), {"status": "sold"}).json()["results"]

        self.assertEqual([c["id"] for c in results], [newer.car_id, older.car_id])

    def test_the_japanese_name_rides_beside_the_english(self):
        make_car("JA-1", brand_ja="ダイハツ", model_name_ja="タント")

        result = self.client.get(reverse("car-list")).json()["results"][0]

        self.assertEqual(result["brand_ja"], "ダイハツ")
        self.assertEqual(result["model_name_ja"], "タント")

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

    def test_media_merges_photos_and_videos_in_one_order(self):
        car = make_car("MEDIA")
        photo = attach_image(car, "a.gif", order=0)
        clip = attach_video(car, "b.mp4", order=1)

        payload = self.client.get(reverse("car-detail", args=[car.slug])).json()

        self.assertEqual([m["kind"] for m in payload["media"]], ["photo", "video"])
        self.assertEqual([m["id"] for m in payload["media"]], [photo.image_id, clip.video_id])

    def test_a_video_can_lead_the_gallery(self):
        car = make_car("VIDEO-FIRST")
        attach_image(car, "a.gif", order=1)
        clip = attach_video(car, "b.mp4", order=0)

        payload = self.client.get(reverse("car-detail", args=[car.slug])).json()

        self.assertEqual(payload["media"][0]["id"], clip.video_id)

    def test_images_excludes_videos_even_when_one_leads(self):
        """`images` is the photos-only list the poster frame and the card both read."""
        car = make_car("PHOTOS-ONLY")
        photo = attach_image(car, "a.gif", order=1)
        attach_video(car, "b.mp4", order=0)

        payload = self.client.get(reverse("car-detail", args=[car.slug])).json()

        self.assertEqual([i["id"] for i in payload["images"]], [photo.image_id])
        self.assertEqual([i["kind"] for i in payload["images"]], ["photo"])

    def test_a_video_carries_no_sources(self):
        """Nothing resizes an upload, so promising a srcset would promise a 404."""
        car = make_car("NO-SOURCES")
        attach_video(car, "b.mp4")

        entry = self.client.get(reverse("car-detail", args=[car.slug])).json()["media"][0]

        self.assertNotIn("sources", entry)
        self.assertTrue(entry["video"])

    def test_video_field_still_answers_for_the_cached_bundle(self):
        """Compatibility shim: the deployed JS asks for `video` until CloudFront turns over."""
        car = make_car("LEGACY-FIELD")
        clip = attach_video(car, "b.mp4")

        payload = self.client.get(reverse("car-detail", args=[car.slug])).json()

        self.assertEqual(payload["video"], clip.url)

    def test_specs_are_sent_unlocalised_and_in_order(self):
        """The client switches language without a refetch, so it resolves the fallback."""
        car = make_car("SPEC-API", specs=[
            {"label_en": "Colour", "label_ja": "色", "value_en": "W", "value_ja": "白"},
            {"label_en": "Tow bar", "label_ja": "", "value_en": "Fitted", "value_ja": ""},
        ])

        payload = self.client.get(reverse("car-detail", args=[car.slug])).json()

        self.assertEqual([r["label_en"] for r in payload["specs"]], ["Colour", "Tow bar"])
        self.assertEqual(payload["specs"][0]["label_ja"], "色")

    def test_a_car_with_no_specs_sends_an_empty_list(self):
        """So the client has something to map over either way."""
        car = make_car("SPEC-API-NONE")

        payload = self.client.get(reverse("car-detail", args=[car.slug])).json()

        self.assertEqual(payload["specs"], [])

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

        self.assertEqual(image.available_widths, list(DERIVATIVE_WIDTHS))

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

        self.assertEqual(sorted(sources), ["1200", "1600", "320", "800"])
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

    def test_a_pool_user_who_is_not_staff_cannot_sign_uploads(self):
        """One pool holds everybody, so being *in* it is not the permission.

        This was "a customer cannot sign", back when the public site had accounts. The
        account is gone and the hole is not: `admin-create-user` puts somebody in the
        pool without putting them in a group, and that is who this is.
        """
        sign_in(self.client, fake_cognito.make_user("nobody@example.com"))

        response = self.client.post(
            self.url,
            {"kind": "image", "content_type": "image/jpeg", "size": 1024},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def staffer(self):
        return fake_cognito.make_user(
            "boss@example.com", groups=(cognito.STAFF_GROUP, cognito.OWNERS_GROUP))

    def test_staff_signing_in_through_the_hosted_ui_can_sign(self):
        """The whole reason `StaffCookieAuthentication` exists.

        A staff member who signed in at the hosted UI holds `dm_st` and nothing else. If
        this endpoint reads the customer cookie instead, they are anonymous here, signing
        403s, and `direct-upload.js` quietly falls back to posting the file through
        Lambda -- where the ~4.5 MB ceiling makes a 200 MB video impossible and the
        fallback validates neither type nor size. Nothing on screen says so.

        Asserting the 400 rather than a 200 is deliberate: `_validate` rejects the
        content type before `build_presigned_upload` ever reaches S3, so this proves
        authentication passed without needing a bucket or a stand-in for one.
        """
        sign_in(self.client, self.staffer(), staff=True)

        response = self.client.post(
            self.url,
            {"kind": "video", "content_type": "video/quicktime", "size": 1024},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("MP4", response.json()["detail"])

    @override_settings(COGNITO_STAFF_CLIENT_ID="staff-client",
                       COGNITO_CUSTOMER_CLIENT_ID="other-client")
    def test_a_token_from_another_app_client_is_refused(self):
        """A genuine token, minted by the wrong app client, in the right cookie.

        The `@override_settings` is what makes this test test anything: the id defaults
        to "" in the suite, and `StaffCookieAuthentication` skips the comparison
        entirely when the expected value is empty.

        The pool still has one app client and the check still matters: the refusal is
        by *which client minted this*, not by which cookie carried it, so a second
        client added for anything -- a mobile app, a partner -- cannot borrow the staff
        pages by presenting its token here.
        """
        user = self.staffer()
        self.client.cookies[authentication.STAFF_COOKIE] = fake_cognito.token_for(
            user, staff=False)

        response = self.client.post(
            self.url,
            {"kind": "image", "content_type": "image/jpeg", "size": 1024},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)


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
        self.assertEqual(car.slug, "2008-daihatsu-tanto")
        self.assertEqual(car.get_absolute_url(), "/cars/2008-daihatsu-tanto")

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
        """`?lang=en` because the shop's own language is now the default.

        The page is served in Japanese to anybody who does not ask otherwise -- the
        customers are in Hamura -- so an English assertion has to say so."""
        body = self.client.get("/?lang=en").content.decode()

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

        a = self.client.get(first.get_absolute_url() + "?lang=en").content.decode()
        b = self.client.get(second.get_absolute_url() + "?lang=en").content.decode()

        self.assertIn("2008 Daihatsu Tanto for sale in Hamura", a)
        self.assertIn("2020 Honda N-Box for sale in Hamura", b)
        self.assertNotEqual(
            re.search(r"<title>(.*?)</title>", a).group(1),
            re.search(r"<title>(.*?)</title>", b).group(1),
        )

    def test_the_home_page_preloads_the_first_card_photo(self, _tags):
        """The lot's first photograph is the LCP element and `CarCard` marks it
        `priority`, but a browser cannot act on that until the bundle has parsed.
        The car page has always preloaded its hero; the home page never did."""
        car = make_car("PRELOAD-1")
        attach_photo(car, 2400, 1800, is_primary=True)

        body = self.client.get("/").content.decode()

        self.assertIn('rel="preload" as="image"', body)
        self.assertIn('fetchpriority="high"', body)
        self.assertIn("imagesrcset=", body)

    def test_the_preloaded_sizes_match_the_card_component(self, _tags):
        """If `imagesizes` and `CARD_SIZES` disagree, the browser preloads one file
        and the page then asks for another -- two downloads, and slower than not
        preloading at all. The two live in different languages in different files,
        so this is the only thing holding them together."""
        card = (pathlib.Path(__file__).resolve().parents[2]
                / "frontend" / "src" / "components" / "CarCard.jsx").read_text(encoding="utf-8")
        widths = re.search(r"const CARD_SIZES = \[(.*?)\]", card, re.S).group(1)
        in_component = " ".join(re.findall(r'"([^"]+)"', widths))

        car = make_car("PRELOAD-2")
        attach_photo(car, 2400, 1800, is_primary=True)
        body = self.client.get("/").content.decode()
        rendered = re.search(r'imagesizes="([^"]+)"', body).group(1)

        self.assertEqual(" ".join(rendered.split(", ")), in_component)

    def test_the_price_is_stated_as_the_total_a_buyer_pays(self, _tags):
        """支払総額, not 車両本体価格, and said where it is read before the click.

        Almost every used-car shop in Japan lists the body price and adds the 諸費用
        at the desk, so a bare number is *read* as that -- it is the most common
        complaint about buying a used car here. This shop's price is the total, which
        is the strongest thing it can say, and it was being thrown away by not saying
        it. In the meta description because that is the line Google prints under the
        result, and in `priceSpecification` because that is where the rich result
        reads it from.
        """
        car = make_car("TOTAL-1", price_jpy=390000)

        japanese = self.client.get(car.get_absolute_url()).content.decode()
        self.assertIn("支払総額¥390,000", japanese)
        self.assertIn('"valueAddedTaxIncluded":true', japanese.replace(" ", ""))

        english = self.client.get(car.get_absolute_url() + "?lang=en").content.decode()
        self.assertIn("¥390,000 all in", english)

    def test_a_car_with_no_price_claims_no_total(self, _tags):
        """The claim rides on there being a number. Saying "total" beside "ask us"
        would be saying nothing twice."""
        car = make_car("TOTAL-2", price_jpy=None)

        body = self.client.get(car.get_absolute_url()).content.decode()

        self.assertIn("価格はお問い合わせください", body)
        self.assertNotIn("支払総額", body)

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

    def test_japanese_is_what_a_visitor_gets_without_asking(self, _tags):
        """The default flipped, and it is a decision rather than a detail.

        Everybody who walks into this shop is in Hamura. The server-rendered shell and
        `i18n/index.js` have to agree on it or a Japanese reader gets a Japanese page
        that turns English the moment the app mounts -- which is worse than either.
        """
        body = self.client.get("/").content.decode()

        self.assertIn('<html lang="ja"', body)
        self.assertIn('property="og:locale" content="ja_JP"', body)

    def test_english_is_still_a_query_string_away(self, _tags):
        self.assertIn('<html lang="en"',
                      self.client.get("/?lang=en").content.decode())

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

    def test_specs_are_rendered_into_the_page_body(self, _tags):
        """A crawler and a reader with no JavaScript get the words, same as the fixed rows."""
        car = make_car("PAGE-SPEC", specs=[
            {"label_en": "Tow bar", "label_ja": "", "value_en": "Fitted", "value_ja": ""},
        ])

        body = self.client.get(car.get_absolute_url()).content.decode()

        self.assertIn("<li>Tow bar: Fitted</li>", body)

    def test_specs_are_rendered_in_the_page_language(self, _tags):
        """Unlike the nine fixed labels, which are English throughout. See pages.py."""
        car = make_car("PAGE-SPEC-JA", specs=[
            {"label_en": "Colour", "label_ja": "色",
             "value_en": "White", "value_ja": "白"},
        ])

        body = self.client.get(f"{car.get_absolute_url()}?lang=ja").content.decode()

        self.assertIn("<li>色: 白</li>", body)

    def test_a_spec_falls_back_per_half(self, _tags):
        """A translated label beside an untranslated value is the common case."""
        car = make_car("PAGE-SPEC-HALF", specs=[
            {"label_en": "Colour", "label_ja": "色",
             "value_en": "Pearl White", "value_ja": ""},
        ])

        body = self.client.get(f"{car.get_absolute_url()}?lang=ja").content.decode()

        self.assertIn("<li>色: Pearl White</li>", body)

    def test_a_hostile_spec_label_cannot_inject_markup(self, _tags):
        """Staff-typed text on a path that previously carried only descriptions and
        customer questions. The <ul> is rendered by hand, so this is not free."""
        car = make_car("PAGE-SPEC-XSS", specs=[
            {"label_en": "<img src=x onerror=alert(1)>", "label_ja": "",
             "value_en": "<b>bold</b>", "value_ja": ""},
        ])

        body = self.client.get(car.get_absolute_url()).content.decode()

        self.assertNotIn("<img src=x", body)
        self.assertNotIn("<b>bold</b>", body)
        self.assertIn("&lt;img src=x", body)

    def test_a_hostile_spec_label_cannot_break_out_of_initial_data(self, _tags):
        car = make_car("PAGE-SPEC-XSS2", specs=[
            {"label_en": "</script><script>alert(1)</script>", "label_ja": "",
             "value_en": "x", "value_ja": ""},
        ])

        body = self.client.get(car.get_absolute_url()).content.decode()
        payload = body.split('id="initial-data"', 1)[1].split("</script>", 1)[0]

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


@mock.patch("cars.pages.asset_tags", return_value="")
class MetaPixelTests(DynamoReset, SimpleTestCase):
    """The pixel is opt-in by configuration, the way the CDN invalidation is.

    Nothing in the app branches on being tested: with `META_PIXEL_ID` blank -- which is
    every environment but production -- no snippet is rendered, so `window.fbq` never
    exists and `frontend/src/lib/pixel.js` reports nothing. That is what keeps local
    development, the suite and the screenshot passes from sending anything to Meta.
    """

    def test_no_pixel_is_rendered_without_an_id(self, _tags):
        body = self.client.get("/").content.decode()

        self.assertNotIn("connect.facebook.net", body)
        self.assertNotIn("fbq(", body)

    @override_settings(META_PIXEL_ID="1234567890")
    def test_the_pixel_is_rendered_on_the_public_pages_when_configured(self, _tags):
        car = make_car("PIX-1", brand="Honda", model_name="N-Box")

        for path in ("/", car.get_absolute_url(), "/privacy"):
            with self.subTest(path=path):
                body = self.client.get(path).content.decode()
                self.assertIn("connect.facebook.net/en_US/fbevents.js", body)
                self.assertIn("fbq('init', '1234567890')", body)
                # The <noscript> beacon, which is how a visitor with JavaScript off is
                # counted at all. Meta's own snippet, kept whole.
                self.assertIn("facebook.com/tr?id=1234567890", body)

    @override_settings(META_PIXEL_ID="'); alert(1); //")
    def test_a_pixel_id_that_is_not_digits_renders_nothing(self, _tags):
        """It reaches a <script> block on a public page cached at the edge for minutes,
        so a quote in it would be script injection on every page of the site. A
        misconfigured id is better as no pixel than as a broken page."""
        body = self.client.get("/").content.decode()

        self.assertNotIn("alert(1)", body)
        self.assertNotIn("connect.facebook.net", body)


@mock.patch("cars.pages.asset_tags", return_value="")
class PrivacyPageTests(DynamoReset, SimpleTestCase):
    def test_the_privacy_page_discloses_the_pixel_in_both_languages(self, _tags):
        """Meta's business tools terms require the disclosure before a pixel may run,
        and it is the APPI notice. The rendered body carries the words rather than only
        a heading, because a crawler checking for it does not run the app."""
        english = self.client.get("/privacy?lang=en").content.decode()

        self.assertIn("<title>Privacy | Dakka Motors</title>", english)
        self.assertIn("Meta pixel", english)
        self.assertIn("never sent to Meta", english)
        self.assertIn("080-9282-3601", english)

        japanese = self.client.get("/privacy").content.decode()

        self.assertIn("Meta", japanese)
        self.assertIn("プライバシー", japanese)

    def test_the_privacy_page_is_indexable_and_in_the_sitemap(self, _tags):
        """Unlike the account and booking shells. Meta's advertisement review looks for
        this page, and so does anyone deciding whether to hand over a phone number."""
        body = self.client.get("/privacy").content.decode()

        self.assertNotIn('name="robots"', body)
        self.assertIn('rel="canonical" href="https://dakkamotors.com/privacy"', body)
        self.assertIn("https://dakkamotors.com/privacy",
                      self.client.get("/sitemap.xml").content.decode())

    def test_the_trailing_slash_form_answers_too(self, _tags):
        self.assertEqual(self.client.get("/privacy/").status_code, 200)


@override_settings(
    OUTBOX_BUCKET="test-outbox",
    MAIL_FROM="noreply@dakkamotors.com",
    MAIL_FROM_NAME="Dakka Motors",
    MAIL_REPLY_TO="owner@example.com",
    STAFF_ALERT_EMAIL="staff@example.com",
)
class QueueEmailTests(SimpleTestCase):
    """Nothing is sent from the request path, so 'sending' means writing one object to
    S3 for the mailer Lambda to pick up. Fire-and-forget by design."""

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
MAIL_SETTINGS = dict(
    OUTBOX_BUCKET="test-outbox",
    MAIL_FROM="noreply@dakkamotors.com",
    STAFF_ALERT_EMAIL="staff@example.com",
)


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


class BrandMarkTests(SimpleTestCase):
    """The DM monogram exists as path data in more than one file, and has to stay one
    shape.

    Three copies is one more than anybody wants, and none of them can be removed: the
    favicon has to be a standalone file a browser can fetch, the header has to be inline
    for the chrome gradient to paint with the masthead rather than after it, and
    `docs/brand/generate.py` has no SVG renderer to read either of them with. So instead
    of a single source there is this, which fails the moment they drift.

    It compares the path data itself, not a rendering -- two marks can differ by a
    single control point and still look identical at the sizes anybody checks by eye.
    """

    REPO = pathlib.Path(__file__).resolve().parents[2]
    # The three subpaths, exactly as every copy must spell them.
    D_OUTER = "M0 0 H30 Q46 0 46 16 V28 Q46 44 30 44 H0 Z"
    D_COUNTER = "M12 12 H27 Q34 12 34 19 V25 Q34 32 27 32 H12 Z"
    M_OUTER = "M0 44 V0 H11 L23 21 L35 0 H46 V44 H35 V17 L23 38 L11 17 V44 Z"

    CARRIERS = (
        "frontend/public/plate.svg",
        "frontend/src/components/Header.jsx",
        "docs/brand/generate.py",
        "docs/brand/mark.svg",
        "docs/brand/lockup.svg",
        "docs/brand/mark-mono.svg",
    )

    def source(self, relative):
        text = (self.REPO / relative).read_text(encoding="utf-8")
        # The svg files wrap `d` across lines to stay readable; a renderer does not
        # care and neither should this.
        return re.sub(r"\s+", " ", text)

    def test_the_monogram_is_the_same_shape_everywhere(self):
        for relative in self.CARRIERS:
            text = self.source(relative)
            for name in ("D_OUTER", "D_COUNTER", "M_OUTER"):
                with self.subTest(file=relative, subpath=name):
                    self.assertIn(getattr(self, name), text)

    def test_the_favicon_keeps_the_filename_cloudfront_is_pinned_to(self):
        """`/plate.svg` has its own behaviour in infra/edge.yaml.

        The artwork stopped being a number plate, but renaming the file is a CloudFront
        distribution update rather than a frontend deploy -- and if the two go out in
        either order, the site spends the gap serving a 404 for its own icon.
        """
        edge = (self.REPO / "infra/edge.yaml").read_text(encoding="utf-8")

        self.assertIn("PathPattern: /plate.svg", edge)
        self.assertTrue((self.REPO / "frontend/public/plate.svg").exists())
        for served_by in ("frontend/index.html", "backend/cars/pages.py"):
            with self.subTest(file=served_by):
                self.assertIn('href="/plate.svg"', self.source(served_by))

    def test_every_svg_is_well_formed_xml(self):
        """SVG is XML, and a browser will not draw one that does not parse.

        This is here because it caught a real one: the comments in these files are
        written in the same prose style as the rest of the repo, which uses `--` as an
        em dash -- and `--` inside an XML comment is forbidden. The favicon parsed
        nowhere and looked fine in every diff.
        """
        import xml.etree.ElementTree as ElementTree

        svgs = [self.REPO / "frontend/public/plate.svg"]
        svgs += sorted((self.REPO / "docs/brand").glob("*.svg"))
        self.assertTrue(svgs)
        for path in svgs:
            with self.subTest(svg=path.name):
                try:
                    ElementTree.parse(path)
                except ElementTree.ParseError as exc:
                    self.fail(f"{path.name} is not well-formed XML: {exc}")

    def test_the_old_email_mark_is_kept_for_messages_already_delivered(self):
        """An email in somebody's inbox still points at /assets/email-mark-d.png.

        It is fetched when the message is opened, which may be months from now, so the
        file outlives the design it belonged to. Deleting it would hollow out the logo
        of every email sent before the rebrand.
        """
        self.assertTrue(
            (self.REPO / "frontend/public/assets/email-mark-d.png").exists())


class EmailTemplateTests(FakeCognito, DynamoReset, SimpleTestCase):
    """The shell every message is rendered into.

    The point of these is that the logo survives the two things that usually break it:
    images being blocked, and Japanese content.
    """

    def render(self, **kwargs):
        kwargs.setdefault("heading", "Confirm your email")
        kwargs.setdefault("body", email_theme.paragraph("Body copy."))
        return email_theme.render(**kwargs)

    def test_the_tile_is_drawn_by_the_client_not_the_image(self):
        """With images off the cell still has its chrome ground, so the mark shows."""
        html = self.render()

        self.assertIn(f'bgcolor="{email_theme.CHROME}"', html)
        self.assertIn(f"background-color:{email_theme.CHROME}", html)

    def test_the_letters_have_alt_text_to_fall_back_to(self):
        html = self.render()

        self.assertIn('alt="DM"', html)
        self.assertIn("/assets/email-mark-dm.png", html)

    def test_the_logo_image_is_an_absolute_url(self):
        """A relative src resolves against nothing in a mail client."""
        html = self.render()

        self.assertIn(f'src="{seo.SITE_URL}/assets/email-mark-dm.png"', html)

    def test_the_mark_is_not_drawn_in_the_colour_it_sits_on(self):
        """The site has chrome on ink; the email masthead has to invert it.

        The band the monogram sits in is itself INK, so a chrome-on-ink mark would be
        ink on ink here -- invisible, and invisible in precisely the case the tile
        exists to survive, which is images being blocked.
        """
        self.assertNotEqual(email_theme.CHROME, email_theme.INK)
        self.assertIn(f"color:{email_theme.INK};\">", email_theme._logo())

    def test_the_preheader_is_hidden_but_present(self):
        html = self.render(preheader="One click finishes your account.")

        self.assertIn("One click finishes your account.", html)
        self.assertIn("display:none", html)

    def test_japanese_renders_with_a_japanese_address(self):
        html = self.render(language="ja", heading="メールアドレスのご確認")

        self.assertIn('lang="ja"', html)
        self.assertIn(seo.BUSINESS["locality_ja"], html)
        self.assertIn("Hiragino", html)

    def queued(self, lead):
        """The JSON one lead alert puts in the outbox."""
        with override_settings(**MAIL_SETTINGS):
            with mock.patch("cars.mail.boto3.client") as client:
                mail.notify_staff_of_request(lead)
                return json.loads(
                    client.return_value.put_object.call_args.kwargs["Body"].decode())

    def test_every_message_carries_a_plain_text_alternative(self):
        """A text part is what text-only clients show, and its absence scores as spam.

        Asserted on the staff lead alert because it is now the only message this site
        sends -- which makes it the only place the shell is exercised end to end.
        """
        lead = request_store.create(name="Yuki", email="new@example.com",
                                    phone="080-1234-5678", details="A Tanto, white")

        body = self.queued(lead)

        self.assertTrue(body["text"].strip())
        self.assertIn("080-1234-5678", body["text"])
        self.assertNotIn("<", body["text"])

    def test_a_name_cannot_inject_markup(self):
        """The one alert interpolates a name a stranger typed into a public form with
        no account behind it, so this is the whole of the site's untrusted input."""
        lead = request_store.create(name="<script>alert(1)</script>",
                                    email="sneaky@example.com", phone="080-0000-0000",
                                    details="")

        body = self.queued(lead)

        self.assertNotIn("<script>", body["html"])
        self.assertIn("&lt;script&gt;", body["html"])