import datetime
import hashlib
import io
import json
import os
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib import admin
from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from .management.commands.ensure_inventory_group import GROUP_NAME
from . import booking as booking_rules
from . import email_theme
from . import mail
from . import seo
from .notification_models import Notification, NotificationKind
from .qa_models import CarQuestion
from .booking_models import (
    BookingStatus,
    CustomerProfile,
    PendingRegistration,
    TestDriveBooking,
    TestDriveSchedule,
    TestDriveSlot,
)
from .models import Car, CarImage, CarStatus, StaffAccount
from .uploads import UploadRejected, _validate

# A 1x1 GIF — smallest thing Pillow will accept as a real image.
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def make_car(chassis, **overrides):
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
    return Car.objects.create(**fields)


def attach_image(car, name, *, is_primary=False, order=0):
    return CarImage.objects.create(
        car=car,
        image=SimpleUploadedFile(name, TINY_GIF, content_type="image/gif"),
        is_primary=is_primary,
        order=order,
    )


class ClearsThrottleMixin:
    """Reset the rate-limit counter between tests.

    DRF keeps throttle history in Django's cache, which lives for the whole test
    process - so auth tests start tripping the 20/hour limit partway through a run and
    fail with 429 for reasons that have nothing to do with what they assert.
    """

    def setUp(self):
        super().setUp()
        cache.clear()


class CarListApiTests(TestCase):
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
        self.assertEqual(result["primary_image"]["id"], primary.id)

    def test_list_primary_image_is_null_when_car_has_no_images(self):
        make_car("NO-IMAGE")

        result = self.client.get(reverse("car-list")).json()["results"][0]

        self.assertIsNone(result["primary_image"])


class CarDetailApiTests(TestCase):
    def test_detail_is_reachable_for_a_reserved_car(self):
        """A shared link must keep working after the car is reserved or sold."""
        car = make_car("RESERVED-2", status=CarStatus.RESERVED)

        response = self.client.get(reverse("car-detail", args=[car.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "reserved")

    def test_detail_returns_full_gallery_in_order(self):
        car = make_car("GALLERY")
        second = attach_image(car, "second.gif", order=2)
        first = attach_image(car, "first.gif", order=1)

        images = self.client.get(reverse("car-detail", args=[car.pk])).json()["images"]

        self.assertEqual([i["id"] for i in images], [first.id, second.id])

    def test_detail_exposes_both_descriptions(self):
        car = make_car("DESCRIPTIONS", description_en="English", description_ja="日本語")

        payload = self.client.get(reverse("car-detail", args=[car.pk])).json()

        self.assertEqual(payload["description_en"], "English")
        self.assertEqual(payload["description_ja"], "日本語")


def jpeg(width, height, exif=None):
    """A real JPEG, so Pillow has something with genuine dimensions to resize."""
    buffer = io.BytesIO()
    image = Image.new("RGB", (width, height), (120, 130, 140))
    image.save(buffer, format="JPEG", exif=exif) if exif else image.save(buffer, "JPEG")
    return SimpleUploadedFile("photo.jpg", buffer.getvalue(), content_type="image/jpeg")


def attach_photo(car, width, height, exif=None, **kwargs):
    return CarImage.objects.create(car=car, image=jpeg(width, height, exif), **kwargs)


class DerivativeTests(TestCase):
    def test_widths_larger_than_the_original_are_not_generated(self):
        """Upscaling costs bytes and adds no detail, and would make srcset mislead."""
        image = attach_photo(make_car("SMALL"), 900, 675)

        image.refresh_from_db()

        self.assertTrue(image.derivatives_ready)
        self.assertEqual(image.available_widths, [320, 800])
        self.assertNotIn(1600, image.available_widths)

    def test_all_widths_generated_for_a_large_original(self):
        image = attach_photo(make_car("LARGE"), 2400, 1800)

        image.refresh_from_db()

        self.assertEqual(image.available_widths, [320, 800, 1600])

    def test_original_smaller_than_every_target_still_gets_one_copy(self):
        """Otherwise a tiny upload would have nothing to serve at all."""
        image = attach_photo(make_car("TINY"), 120, 90)

        image.refresh_from_db()

        self.assertTrue(image.derivatives_ready)
        self.assertEqual(image.available_widths, [120])

    def test_two_photos_do_not_share_derivative_names(self):
        """Ordinary uploads all land in cars/, so names built from the folder alone
        would make every photo overwrite the previous one's copies."""
        car = make_car("COLLIDE")
        first = attach_photo(car, 1000, 750)
        second = attach_photo(car, 1000, 750)

        first.refresh_from_db()
        second.refresh_from_db()

        self.assertNotEqual(first.derivative_name(800), second.derivative_name(800))
        # And both files really exist rather than one having clobbered the other.
        storage = first.image.storage
        self.assertTrue(storage.exists(first.derivative_name(800)))
        self.assertTrue(storage.exists(second.derivative_name(800)))

    def test_exif_rotation_is_applied(self):
        """Phones record orientation in EXIF; without this, portraits serve sideways."""
        exif = Image.Exif()
        exif[274] = 6  # rotate 90°
        image = attach_photo(make_car("ROTATED"), 1000, 500, exif=exif.tobytes())

        image.refresh_from_db()
        storage = image.image.storage
        with storage.open(image.derivative_name(min(image.available_widths))) as fh:
            generated = Image.open(fh)
            generated.load()

        # A landscape original tagged "rotate 90" must come out portrait.
        self.assertGreater(generated.height, generated.width)

    def test_derivatives_are_webp(self):
        image = attach_photo(make_car("FORMAT"), 1000, 750)
        image.refresh_from_db()

        with image.image.storage.open(image.derivative_name(800)) as fh:
            self.assertEqual(Image.open(fh).format, "WEBP")

    def test_replacing_the_photo_invalidates_the_old_copies(self):
        car = make_car("REPLACED")
        image = attach_photo(car, 1000, 750)
        image.refresh_from_db()
        self.assertTrue(image.derivatives_ready)

        image.image = jpeg(1200, 900)
        image.save()

        # Rebuilt for the new photo rather than left describing the old one.
        image.refresh_from_db()
        self.assertTrue(image.derivatives_ready)


class FileCleanupTests(TestCase):
    def test_deleting_a_photo_removes_its_files(self):
        """Otherwise every sold-and-removed listing leaks megabytes into the bucket."""
        image = attach_photo(make_car("CLEANUP"), 1000, 750)
        image.refresh_from_db()
        storage = image.image.storage
        original, derivative = image.image.name, image.derivative_name(800)
        self.assertTrue(storage.exists(original))
        self.assertTrue(storage.exists(derivative))

        image.delete()

        self.assertFalse(storage.exists(original))
        self.assertFalse(storage.exists(derivative))

    def test_deleting_a_car_removes_its_photos_files(self):
        car = make_car("CASCADE")
        image = attach_photo(car, 1000, 750)
        image.refresh_from_db()
        storage = image.image.storage
        original = image.image.name

        car.delete()

        self.assertFalse(storage.exists(original))


class DerivativeApiTests(TestCase):
    def test_sources_absent_until_processing_finishes(self):
        """A <source> pointing at an object that does not exist yet renders broken."""
        car = make_car("PENDING")
        image = attach_photo(car, 1000, 750)
        CarImage.objects.filter(pk=image.pk).update(
            derivatives_ready=False, derivative_widths=""
        )

        payload = self.client.get(reverse("car-detail", args=[car.pk])).json()

        self.assertIsNone(payload["images"][0]["sources"])
        # The original is still served, so the page is never image-less.
        self.assertTrue(payload["images"][0]["image"])

    def test_sources_listed_once_ready(self):
        car = make_car("READY")
        attach_photo(car, 2400, 1800)

        sources = self.client.get(reverse("car-detail", args=[car.pk])).json()["images"][0][
            "sources"
        ]

        self.assertEqual(sorted(sources), ["1600", "320", "800"])
        self.assertTrue(all(url.endswith(".webp") for url in sources.values()))

    def test_list_primary_image_carries_sources(self):
        car = make_car("CARD")
        attach_photo(car, 1200, 900, is_primary=True)

        result = self.client.get(reverse("car-list")).json()["results"][0]

        self.assertIn("800", result["primary_image"]["sources"])


class RebuildDerivativesCommandTests(TestCase):
    def test_repairs_a_half_processed_photo(self):
        from django.core.management import call_command

        car = make_car("REPAIR")
        image = attach_photo(car, 1000, 750)
        CarImage.objects.filter(pk=image.pk).update(
            derivatives_ready=False, derivative_widths=""
        )

        call_command("rebuild_derivatives", stdout=io.StringIO())

        image.refresh_from_db()
        self.assertTrue(image.derivatives_ready)
        self.assertEqual(image.available_widths, [320, 800])


class UploadValidationTests(TestCase):
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


class SignUploadEndpointTests(TestCase):
    url = "/api/admin/uploads/sign/"

    def test_anonymous_users_cannot_sign_uploads(self):
        """Signing grants write access to the media bucket."""
        response = self.client.post(
            self.url,
            {"kind": "image", "content_type": "image/jpeg", "size": 1024},
            content_type="application/json",
        )
        self.assertIn(response.status_code, (401, 403))

    def test_non_staff_users_cannot_sign_uploads(self):
        get_user_model().objects.create_user("shopper", password="not-staff-pw-1")
        self.client.login(username="shopper", password="not-staff-pw-1")

        response = self.client.post(
            self.url,
            {"kind": "image", "content_type": "image/jpeg", "size": 1024},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_staff_get_a_helpful_error_for_bad_types(self):
        get_user_model().objects.create_superuser("boss", password="staff-pw-12345")
        self.client.login(username="boss", password="staff-pw-12345")

        response = self.client.post(
            self.url,
            {"kind": "video", "content_type": "video/quicktime", "size": 1024},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("MP4", response.json()["detail"])


class InventoryGroupTests(TestCase):
    """The role is a security boundary, so the negative cases carry the weight."""

    def setUp(self):
        call_command("ensure_inventory_group", stdout=io.StringIO())
        self.group = Group.objects.get(name=GROUP_NAME)

    def test_group_grants_exactly_the_cars_permissions(self):
        granted = {
            f"{p.content_type.app_label}.{p.codename}"
            for p in self.group.permissions.all()
        }
        self.assertEqual(
            granted,
            {
                "cars.add_car", "cars.change_car", "cars.delete_car", "cars.view_car",
                "cars.add_carimage", "cars.change_carimage",
                "cars.delete_carimage", "cars.view_carimage",
                # Staff administration via the proxy, never via auth.User.
                "cars.add_staffaccount", "cars.change_staffaccount",
                "cars.view_staffaccount",
                # Test drives: rules, dates, and the bookings themselves.
                "cars.add_testdriveschedule", "cars.change_testdriveschedule",
                "cars.delete_testdriveschedule", "cars.view_testdriveschedule",
                "cars.add_testdriveslot", "cars.change_testdriveslot",
                "cars.delete_testdriveslot", "cars.view_testdriveslot",
                "cars.change_testdrivebooking", "cars.view_testdrivebooking",
                "cars.view_customerprofile",
                # Questions about a car. Delete is granted because a question is the
                # one field on this site a stranger can type into.
                "cars.add_carquestion", "cars.change_carquestion",
                "cars.delete_carquestion", "cars.view_carquestion",
            },
        )

    def test_group_cannot_read_anyone_s_notifications(self):
        """A notification feed is one customer's private history.

        Nothing grants it and the model is not registered in the admin, so the entry
        cannot appear at all - but assert it, because a future `view_` grant added out
        of habit would silently open somebody's inbox to every member of staff.
        """
        granted = {p.codename for p in self.group.permissions.all()}
        for codename in ("add_notification", "change_notification",
                         "delete_notification", "view_notification"):
            self.assertNotIn(codename, granted)

    def test_group_cannot_delete_bookings_or_edit_customer_details(self):
        """A cancelled booking is history worth keeping, and customers own their own
        contact details."""
        granted = {p.codename for p in self.group.permissions.all()}
        self.assertNotIn("delete_testdrivebooking", granted)
        for codename in ("add_customerprofile", "change_customerprofile",
                         "delete_customerprofile"):
            self.assertNotIn(codename, granted)

    def test_group_cannot_delete_staff_accounts(self):
        """Removing someone means deactivating them, which is reversible."""
        granted = {p.codename for p in self.group.permissions.all()}
        self.assertNotIn("delete_staffaccount", granted)

    def test_group_grants_nothing_outside_the_cars_app(self):
        """Anything from auth or admin would let a member hand themselves more."""
        labels = {p.content_type.app_label for p in self.group.permissions.all()}
        self.assertEqual(labels, {"cars"})

    def test_rerunning_strips_permissions_added_by_hand(self):
        escalation = Permission.objects.get(
            content_type__app_label="auth", codename="change_user"
        )
        self.group.permissions.add(escalation)

        call_command("ensure_inventory_group", stdout=io.StringIO())

        self.assertNotIn(escalation, self.group.permissions.all())


def make_manager(username="manager", password="inventory-pw-12345"):
    call_command("ensure_inventory_group", stdout=io.StringIO())
    user = get_user_model().objects.create_user(username=username, password=password)
    user.is_staff = True
    user.save()
    user.groups.add(Group.objects.get(name=GROUP_NAME))
    return user, password


class InventoryManagerAccessTests(TestCase):
    def setUp(self):
        self.user, self.password = make_manager()
        self.client.login(username=self.user.username, password=self.password)

    def test_manager_is_staff_but_not_a_superuser(self):
        self.assertTrue(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)

    def test_manager_can_manage_cars(self):
        """The role has to actually work, not just be safely locked down."""
        response = self.client.get("/api/admin/cars/car/")
        self.assertEqual(response.status_code, 200)

    def test_manager_cannot_reach_user_administration(self):
        """Hiding the link is not the protection - the view checks too."""
        response = self.client.get("/api/admin/auth/user/")
        self.assertEqual(response.status_code, 403)

    def test_manager_cannot_reach_group_administration(self):
        response = self.client.get("/api/admin/auth/group/")
        self.assertEqual(response.status_code, 403)

    def test_admin_index_offers_no_user_management(self):
        body = self.client.get("/api/admin/").content.decode()
        self.assertNotIn("/api/admin/auth/user/", body)
        self.assertNotIn("/api/admin/auth/group/", body)
        # ...but the job they are here to do is on the page.
        self.assertIn("/api/admin/cars/car/", body)

    def test_manager_can_sign_uploads(self):
        """Photo upload is gated on is_staff, so the role must clear it."""
        response = self.client.post(
            "/api/admin/uploads/sign/",
            {"kind": "video", "content_type": "video/quicktime", "size": 1024},
            content_type="application/json",
        )
        # 400 (not 403) proves authorisation passed and validation rejected the type.
        self.assertEqual(response.status_code, 400)


class CreateInventoryUserTests(TestCase):
    def setUp(self):
        call_command("ensure_inventory_group", stdout=io.StringIO())

    def test_creates_a_staff_member_in_the_group(self):
        with mock.patch.dict(os.environ, {"INVENTORY_USER_PASSWORD": "first-pw-12345"}):
            call_command(
                "create_inventory_user", username="newhire", email="a@b.com",
                first_name="New", last_name="Hire", stdout=io.StringIO(),
            )

        user = get_user_model().objects.get(username="newhire")
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.groups.filter(name=GROUP_NAME).exists())
        self.assertTrue(user.check_password("first-pw-12345"))

    def test_rerunning_does_not_reset_an_existing_password(self):
        """Otherwise a redeploy would silently lock someone out of their own account."""
        with mock.patch.dict(os.environ, {"INVENTORY_USER_PASSWORD": "first-pw-12345"}):
            call_command("create_inventory_user", username="newhire", stdout=io.StringIO())
        user = get_user_model().objects.get(username="newhire")
        user.set_password("chosen-by-them-678")
        user.save()

        with mock.patch.dict(os.environ, {"INVENTORY_USER_PASSWORD": "different-pw-999"}):
            call_command("create_inventory_user", username="newhire", stdout=io.StringIO())

        user.refresh_from_db()
        self.assertTrue(user.check_password("chosen-by-them-678"))

    def test_details_can_come_from_the_environment(self):
        """Zappa splits the command string on whitespace and ignores quotes, so any
        name containing a space can only be passed this way."""
        env = {
            "INVENTORY_USER_PASSWORD": "env-pw-123456",
            "INVENTORY_USER_USERNAME": "envhire",
            "INVENTORY_USER_EMAIL": "env@example.com",
            "INVENTORY_USER_FIRST_NAME": "Mohammad Mahsiul",
            "INVENTORY_USER_LAST_NAME": "Islam",
        }
        with mock.patch.dict(os.environ, env):
            call_command("create_inventory_user", stdout=io.StringIO())

        user = get_user_model().objects.get(username="envhire")
        self.assertEqual(user.first_name, "Mohammad Mahsiul")
        self.assertEqual(user.last_name, "Islam")
        self.assertTrue(user.groups.filter(name=GROUP_NAME).exists())

    def test_requires_a_username_from_somewhere(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(CommandError):
                call_command("create_inventory_user", stdout=io.StringIO())

    def test_refuses_to_modify_a_superuser(self):
        """A typo matching the owner's account must not quietly demote it."""
        get_user_model().objects.create_superuser("boss", password="owner-pw-12345")

        with self.assertRaises(CommandError):
            call_command("create_inventory_user", username="boss", stdout=io.StringIO())

        self.assertTrue(get_user_model().objects.get(username="boss").is_superuser)

    def test_requires_a_password_for_a_new_account(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(CommandError):
                call_command("create_inventory_user", username="nopw", stdout=io.StringIO())


class StaffAdministrationTests(TestCase):
    """Each test is an escalation a member could actually attempt from a browser."""

    STAFF_URL = "/api/admin/cars/staffaccount/"

    def setUp(self):
        self.manager, self.password = make_manager()
        self.owner = get_user_model().objects.create_superuser(
            "owner", password="owner-pw-123456"
        )
        self.colleague, _ = make_manager("colleague", "colleague-pw-1234")
        self.client.login(username=self.manager.username, password=self.password)

    def change_url(self, user):
        return f"{self.STAFF_URL}{user.pk}/change/"

    def post_change(self, user, **overrides):
        data = {
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "email": user.email,
            "is_active": "on",
            "is_staff": "on",
            "groups": [str(Group.objects.get(name=GROUP_NAME).pk)],
        }
        data.update(overrides)
        return self.client.post(self.change_url(user), data)

    # --- the role works ------------------------------------------------------------

    def test_manager_can_list_staff(self):
        self.assertEqual(self.client.get(self.STAFF_URL).status_code, 200)

    def test_manager_can_edit_a_colleague(self):
        self.post_change(self.colleague, first_name="Renamed")
        self.colleague.refresh_from_db()
        self.assertEqual(self.colleague.first_name, "Renamed")

    def test_created_colleague_can_log_in_and_has_the_role(self):
        self.client.post(
            f"{self.STAFF_URL}add/",
            {"username": "newmate", "password1": "brand-new-pw-77",
             "password2": "brand-new-pw-77"},
        )
        created = get_user_model().objects.get(username="newmate")
        self.assertTrue(created.is_staff)
        self.assertTrue(created.is_active)
        self.assertTrue(created.groups.filter(name=GROUP_NAME).exists())
        self.assertFalse(created.is_superuser)

    # --- the owner is out of reach --------------------------------------------------

    def test_superuser_is_not_listed(self):
        body = self.client.get(self.STAFF_URL).content.decode()
        self.assertNotIn("owner", body)
        self.assertIn("colleague", body)

    def test_superuser_change_page_is_refused_by_direct_url(self):
        """Filtering the list is not enough; a guessed pk must be refused too.

        The refusal is a redirect rather than a 403: the filtered queryset means the
        admin cannot find the object at all, so it bounces with "does not exist" before
        the permission hook is consulted. Either way the page never renders.
        """
        response = self.client.get(self.change_url(self.owner))

        self.assertIn(response.status_code, (302, 403))
        self.assertNotEqual(response.status_code, 200)

    def test_permission_hook_refuses_a_superuser_target_directly(self):
        """The second line of defence, independent of the queryset filter."""
        from .admin import StaffAccountAdmin

        request = type("Req", (), {"user": self.manager})()
        self.assertFalse(
            StaffAccountAdmin.has_change_permission(
                StaffAccountAdmin(StaffAccount, admin.site), request, self.owner
            )
        )

    def test_cannot_take_over_the_owner_account(self):
        self.post_change(self.owner, username="owner")
        self.owner.refresh_from_db()
        self.assertTrue(self.owner.is_superuser)
        self.assertTrue(self.owner.check_password("owner-pw-123456"))

    # --- escalation attempts --------------------------------------------------------

    def test_cannot_make_themselves_a_superuser(self):
        self.post_change(self.manager, is_superuser="on")

        self.manager.refresh_from_db()
        self.assertFalse(self.manager.is_superuser)

    def test_cannot_promote_a_colleague_to_superuser(self):
        self.post_change(self.colleague, is_superuser="on")

        self.colleague.refresh_from_db()
        self.assertFalse(self.colleague.is_superuser)

    def test_cannot_grant_themselves_arbitrary_permissions(self):
        escalation = Permission.objects.get(
            content_type__app_label="auth", codename="change_user"
        )

        self.post_change(self.manager, user_permissions=[str(escalation.pk)])

        self.manager.refresh_from_db()
        self.assertEqual(self.manager.user_permissions.count(), 0)
        self.assertFalse(self.manager.has_perm("auth.change_user"))

    def test_cannot_join_a_group_outside_the_allowlist(self):
        privileged = Group.objects.create(name="Owners")
        privileged.permissions.add(
            Permission.objects.get(
                content_type__app_label="auth", codename="change_user"
            )
        )

        self.post_change(self.manager, groups=[str(privileged.pk)])

        self.manager.refresh_from_db()
        self.assertFalse(self.manager.groups.filter(name="Owners").exists())
        self.assertFalse(self.manager.has_perm("auth.change_user"))

    def test_save_model_forces_is_superuser_false_even_if_set(self):
        """Exercises the guard directly.

        Posting is_superuser to the change view proves little on its own: the field is
        not rendered, so Django would drop it regardless. This calls save_model with the
        flag already set, which is what a leaked field or a future fieldset mistake
        would look like.
        """
        from .admin import StaffAccountAdmin

        model_admin = StaffAccountAdmin(StaffAccount, admin.site)
        request = type("Req", (), {"user": self.manager})()
        target = StaffAccount.objects.get(pk=self.colleague.pk)
        target.is_superuser = True

        model_admin.save_model(request, target, form=None, change=True)

        target.refresh_from_db()
        self.assertFalse(target.is_superuser)

    def test_save_model_leaves_a_superuser_alone_for_the_owner(self):
        """The same hook must not neuter the owner's own admin."""
        from .admin import StaffAccountAdmin

        model_admin = StaffAccountAdmin(StaffAccount, admin.site)
        request = type("Req", (), {"user": self.owner})()
        target = StaffAccount.objects.get(pk=self.colleague.pk)
        target.is_superuser = True

        model_admin.save_model(request, target, form=None, change=True)

        target.refresh_from_db()
        self.assertTrue(target.is_superuser)

    def test_sandboxed_fieldsets_expose_no_escalation_fields(self):
        body = self.client.get(self.change_url(self.colleague)).content.decode()

        self.assertNotIn('name="is_superuser"', body)
        self.assertNotIn('name="user_permissions"', body)
        # The fields they legitimately need are present.
        self.assertIn('name="is_active"', body)
        self.assertIn('name="groups"', body)

    def test_the_original_user_admin_is_still_out_of_bounds(self):
        """The proxy exists precisely so this stays true."""
        self.assertEqual(self.client.get("/api/admin/auth/user/").status_code, 403)
        self.assertEqual(self.client.get("/api/admin/auth/group/").status_code, 403)

    # --- deactivate, not delete -----------------------------------------------------

    def test_deletion_is_refused(self):
        response = self.client.post(f"{self.STAFF_URL}{self.colleague.pk}/delete/")

        self.assertEqual(response.status_code, 403)
        self.assertTrue(get_user_model().objects.filter(pk=self.colleague.pk).exists())

    def test_bulk_delete_action_is_not_offered(self):
        body = self.client.get(self.STAFF_URL).content.decode()
        self.assertNotIn("delete_selected", body)

    def test_deactivating_a_colleague_works(self):
        self.post_change(self.colleague, is_active="")

        self.colleague.refresh_from_db()
        self.assertFalse(self.colleague.is_active)

    # --- self-lockout ---------------------------------------------------------------

    def test_cannot_deactivate_their_own_account(self):
        self.post_change(self.manager, is_active="")

        self.manager.refresh_from_db()
        self.assertTrue(self.manager.is_active)

    def test_cannot_remove_their_own_staff_access(self):
        self.post_change(self.manager, is_staff="")

        self.manager.refresh_from_db()
        self.assertTrue(self.manager.is_staff)


class SuperuserUnaffectedTests(TestCase):
    def test_superuser_still_sees_every_account(self):
        get_user_model().objects.create_superuser("owner", password="owner-pw-123456")
        other, _ = make_manager("other", "other-pw-12345")
        self.client.login(username="owner", password="owner-pw-123456")

        body = self.client.get("/api/admin/cars/staffaccount/").content.decode()

        self.assertIn("owner", body)
        self.assertIn("other", body)

    def test_superuser_keeps_the_real_user_admin(self):
        get_user_model().objects.create_superuser("owner", password="owner-pw-123456")
        self.client.login(username="owner", password="owner-pw-123456")

        self.assertEqual(self.client.get("/api/admin/auth/user/").status_code, 200)


class SlugTests(TestCase):
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

        car.refresh_from_db()
        self.assertEqual(car.slug, original)


class DiscoveryFileTests(TestCase):
    def test_robots_txt_is_served_and_points_at_the_sitemap(self):
        """It used to 403: the private bucket answered AccessDenied for a file that was
        never uploaded, and Lighthouse scored that "not applicable" rather than failing."""
        response = self.client.get("/robots.txt")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Sitemap: https://dakkamotors.com/sitemap.xml", body)
        self.assertIn("Disallow: /api/admin/", body)

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
class RenderedPageTests(TestCase):
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

        response = self.client.get(f"/cars/{car.pk}")

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], car.get_absolute_url())


class SlugApiTests(TestCase):
    def test_api_resolves_a_car_by_slug(self):
        car = make_car("API-SLUG", brand="Daihatsu", model_name="Tanto",
                       manufacture_year=2008)

        response = self.client.get(f"/api/cars/{car.slug}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["slug"], car.slug)

    def test_api_still_resolves_a_car_by_numeric_id(self):
        """Links shared before slugs existed must keep working."""
        car = make_car("API-ID")

        response = self.client.get(f"/api/cars/{car.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], car.pk)


def make_customer(email="buyer@example.com", password="customer-pw-1234", phone="080-1111-2222"):
    user = get_user_model().objects.create_user(
        username=email, email=email, password=password, first_name="Test", last_name="Buyer"
    )
    CustomerProfile.objects.create(user=user, phone=phone)
    return user, password


def make_schedule(weekday=None, start="18:30", end="19:00", capacity=2, **kwargs):
    """A rule on a weekday, defaulting to tomorrow's so generated slots are future."""
    if weekday is None:
        weekday = (timezone.localdate() + datetime.timedelta(days=1)).weekday()
    hh, mm = start.split(":")
    eh, em = end.split(":")
    return TestDriveSchedule.objects.create(
        weekday=weekday,
        start_time=datetime.time(int(hh), int(mm)),
        end_time=datetime.time(int(eh), int(em)),
        capacity=capacity,
        **kwargs,
    )


def future_slot(capacity=2, days=3, hour=15, is_open=True):
    """A concrete slot comfortably beyond the lead time."""
    starts = timezone.localtime(timezone.now()) + datetime.timedelta(days=days)
    starts = starts.replace(hour=hour, minute=0, second=0, microsecond=0)
    return TestDriveSlot.objects.create(
        starts_at=starts,
        ends_at=starts + datetime.timedelta(minutes=30),
        capacity=capacity,
        is_open=is_open,
    )


class SlotGenerationTests(TestCase):
    def test_generation_creates_one_slot_per_matching_day(self):
        schedule = make_schedule(capacity=2)

        booking_rules.ensure_slots(horizon_days=14)

        slots = TestDriveSlot.objects.filter(schedule=schedule)
        self.assertEqual(slots.count(), 2)  # one per week over a fortnight
        self.assertTrue(all(s.capacity == 2 for s in slots))

    def test_generation_is_idempotent(self):
        make_schedule()
        booking_rules.ensure_slots(horizon_days=14)
        before = TestDriveSlot.objects.count()

        booking_rules.ensure_slots(horizon_days=14)

        self.assertEqual(TestDriveSlot.objects.count(), before)

    def test_regenerating_does_not_reopen_a_slot_staff_closed(self):
        """The whole point of materialising slots: staff overrides must survive."""
        make_schedule()
        booking_rules.ensure_slots(horizon_days=14)
        slot = TestDriveSlot.objects.first()
        slot.is_open = False
        slot.save()

        booking_rules.ensure_slots(horizon_days=14)

        slot.refresh_from_db()
        self.assertFalse(slot.is_open)

    def test_inactive_rules_generate_nothing(self):
        make_schedule(is_active=False)

        booking_rules.ensure_slots(horizon_days=14)

        self.assertEqual(TestDriveSlot.objects.count(), 0)

    def test_rule_validity_window_is_respected(self):
        yesterday = timezone.localdate() - datetime.timedelta(days=1)
        make_schedule(ends_on=yesterday)

        booking_rules.ensure_slots(horizon_days=14)

        self.assertEqual(TestDriveSlot.objects.count(), 0)

    def test_slot_capacity_is_a_snapshot_not_a_live_lookup(self):
        """Editing a rule must not shrink an evening people already booked."""
        schedule = make_schedule(capacity=2)
        booking_rules.ensure_slots(horizon_days=14)

        schedule.capacity = 1
        schedule.save()

        self.assertTrue(all(s.capacity == 2 for s in TestDriveSlot.objects.all()))


class BookingRuleTests(TestCase):
    def setUp(self):
        self.user, _ = make_customer()
        self.car = make_car("BOOK-1", brand="Daihatsu", model_name="Tanto")

    def test_booking_takes_a_seat(self):
        slot = future_slot(capacity=2)

        booking_rules.create_booking(user=self.user, slot_id=slot.pk, car=self.car)

        slot.refresh_from_db()
        self.assertEqual(slot.seats_left, 1)

    def test_car_label_is_snapshotted(self):
        """Deleting a sold car wipes its photos; the booking must still make sense."""
        slot = future_slot()
        booking = booking_rules.create_booking(
            user=self.user, slot_id=slot.pk, car=self.car
        )

        self.car.delete()

        booking.refresh_from_db()
        self.assertIsNone(booking.car)
        self.assertIn("Daihatsu Tanto", booking.car_label)

    def test_a_slot_in_the_past_cannot_be_booked(self):
        past = future_slot(days=-2)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=past.pk)

    def test_a_slot_inside_the_lead_time_cannot_be_booked(self):
        starts = timezone.now() + datetime.timedelta(minutes=10)
        soon = TestDriveSlot.objects.create(
            starts_at=starts, ends_at=starts + datetime.timedelta(minutes=30), capacity=1
        )

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=soon.pk)

    def test_a_closed_slot_cannot_be_booked(self):
        closed = future_slot(is_open=False)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=closed.pk)

    def test_a_slot_beyond_the_horizon_cannot_be_booked(self):
        far = future_slot(days=booking_rules.HORIZON_DAYS + 5)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=far.pk)

    def test_capacity_is_enforced(self):
        slot = future_slot(capacity=1)
        other, _ = make_customer("other@example.com")
        booking_rules.create_booking(user=other, slot_id=slot.pk)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=slot.pk)

    def test_the_same_customer_cannot_take_two_seats_in_one_slot(self):
        slot = future_slot(capacity=3)
        booking_rules.create_booking(user=self.user, slot_id=slot.pk)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=slot.pk)

    def test_active_booking_limit(self):
        for day in range(booking_rules.MAX_ACTIVE_BOOKINGS):
            slot = future_slot(days=day + 2)
            booking_rules.create_booking(user=self.user, slot_id=slot.pk)

        one_more = future_slot(days=20)
        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=one_more.pk)

    def test_cancelling_frees_the_seat(self):
        slot = future_slot(capacity=1)
        booking = booking_rules.create_booking(user=self.user, slot_id=slot.pk)

        booking_rules.cancel_booking(user=self.user, booking_id=booking.pk)

        slot.refresh_from_db()
        self.assertEqual(slot.seats_left, 1)

    def test_cancelling_frees_the_limit_too(self):
        slot = future_slot()
        booking = booking_rules.create_booking(user=self.user, slot_id=slot.pk)
        booking_rules.cancel_booking(user=self.user, booking_id=booking.pk)

        self.assertEqual(booking_rules.active_bookings_for(self.user).count(), 0)

    def test_rescheduling_moves_the_seat(self):
        first = future_slot(days=3, capacity=1)
        second = future_slot(days=5, capacity=1)
        booking = booking_rules.create_booking(user=self.user, slot_id=first.pk)

        booking_rules.reschedule_booking(
            user=self.user, booking_id=booking.pk, slot_id=second.pk
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.seats_left, 1)
        self.assertEqual(second.seats_left, 0)

    def test_cannot_reschedule_into_a_full_slot(self):
        first = future_slot(days=3, capacity=1)
        full = future_slot(days=5, capacity=1)
        other, _ = make_customer("other@example.com")
        booking_rules.create_booking(user=other, slot_id=full.pk)
        booking = booking_rules.create_booking(user=self.user, slot_id=first.pk)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.reschedule_booking(
                user=self.user, booking_id=booking.pk, slot_id=full.pk
            )

    def test_cannot_reschedule_into_the_past(self):
        booking = booking_rules.create_booking(
            user=self.user, slot_id=future_slot(days=3).pk
        )
        past = future_slot(days=-1)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.reschedule_booking(
                user=self.user, booking_id=booking.pk, slot_id=past.pk
            )

    def test_one_customer_cannot_touch_anothers_booking(self):
        """A booking id in a URL must not be enough to reach a stranger's appointment."""
        owner, _ = make_customer("owner@example.com")
        stranger, _ = make_customer("stranger@example.com")
        booking = booking_rules.create_booking(user=owner, slot_id=future_slot().pk)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.cancel_booking(user=stranger, booking_id=booking.pk)

        booking.refresh_from_db()
        self.assertTrue(booking.is_active)


class BookingApiTests(TestCase):
    def setUp(self):
        self.user, self.password = make_customer()
        self.car = make_car("API-BOOK", brand="Honda", model_name="N-Box")

    def login(self):
        self.client.login(username=self.user.username, password=self.password)

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
        booking_rules.create_booking(user=self.user, slot_id=slot.pk)

        results = self.client.get("/api/test-drive/slots/").json()["results"]

        self.assertEqual(results, [])

    def test_anonymous_visitors_cannot_book(self):
        slot = future_slot()

        response = self.client.post(
            "/api/test-drive/bookings/",
            {"slot": slot.pk, "car": self.car.slug},
            content_type="application/json",
        )

        self.assertIn(response.status_code, (401, 403))

    def test_booking_through_the_api(self):
        self.login()
        slot = future_slot()

        response = self.client.post(
            "/api/test-drive/bookings/",
            {"slot": slot.pk, "car": self.car.slug},
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
            {"slot": past.pk},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_customers_only_see_their_own_bookings(self):
        other, _ = make_customer("other@example.com")
        booking_rules.create_booking(user=other, slot_id=future_slot(days=3).pk)
        self.login()
        booking_rules.create_booking(user=self.user, slot_id=future_slot(days=5).pk)

        results = self.client.get("/api/test-drive/bookings/").json()["results"]

        self.assertEqual(len(results), 1)

    def test_cancelling_someone_elses_booking_is_refused(self):
        other, _ = make_customer("other@example.com")
        booking = booking_rules.create_booking(user=other, slot_id=future_slot().pk)
        self.login()

        response = self.client.post(f"/api/test-drive/bookings/{booking.pk}/cancel/")

        self.assertEqual(response.status_code, 400)
        booking.refresh_from_db()
        self.assertTrue(booking.is_active)


class CustomerAccountTests(ClearsThrottleMixin, TestCase):
    def test_duplicate_email_is_refused(self):
        make_customer("taken@example.com")

        response = self.client.post(
            "/api/auth/register/",
            {"name": "Someone", "email": "taken@example.com", "phone": "080",
             "password": "another-good-password-9"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_login_and_me(self):
        user, password = make_customer("signin@example.com")

        login = self.client.post(
            "/api/auth/login/",
            {"email": "signin@example.com", "password": password},
            content_type="application/json",
        )
        me = self.client.get("/api/auth/me/")

        self.assertEqual(login.status_code, 200)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["email"], "signin@example.com")

    def test_wrong_password_does_not_reveal_whether_the_account_exists(self):
        make_customer("known@example.com")

        known = self.client.post(
            "/api/auth/login/",
            {"email": "known@example.com", "password": "wrong-password"},
            content_type="application/json",
        )
        unknown = self.client.post(
            "/api/auth/login/",
            {"email": "nobody@example.com", "password": "wrong-password"},
            content_type="application/json",
        )

        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.json()["detail"], unknown.json()["detail"])

    def test_a_customer_cannot_reach_the_admin(self):
        user, password = make_customer("nosy@example.com")
        self.client.login(username=user.username, password=password)

        response = self.client.get("/api/admin/")

        self.assertNotEqual(response.status_code, 200)

    def test_customers_do_not_appear_in_staff_administration(self):
        """They share a table with staff, but a manager has no business reading their
        details or resetting their passwords."""
        make_customer("private@example.com")
        manager, password = make_manager()
        self.client.login(username=manager.username, password=password)

        body = self.client.get("/api/admin/cars/staffaccount/").content.decode()

        self.assertNotIn("private@example.com", body)
        self.assertIn(manager.username, body)


class AccountPageTests(TestCase):
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
class QueueEmailTests(TestCase):
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


class UnconfiguredEmailTests(TestCase):
    @override_settings(OUTBOX_BUCKET="", MAIL_FROM="")
    def test_nothing_is_sent_when_email_is_not_configured(self):
        """Local development must not be able to email a real customer by accident."""
        with mock.patch("cars.mail.boto3.client") as client:
            queued = mail.queue_email(to="a@b.com", subject="x", html="y")

        self.assertFalse(queued)
        client.assert_not_called()


class BookingApprovalTests(TestCase):
    def setUp(self):
        self.user, _ = make_customer()
        self.car = make_car("APPROVE-1", brand="Daihatsu", model_name="Tanto")

    def test_a_new_booking_is_awaiting_confirmation(self):
        booking = booking_rules.create_booking(
            user=self.user, slot_id=future_slot().pk, car=self.car
        )

        self.assertEqual(booking.status, BookingStatus.PENDING)
        self.assertTrue(booking.is_active)

    def test_a_pending_booking_holds_the_seat(self):
        """Otherwise two customers could both be pending for one place, and one would
        have to be turned away after the fact."""
        slot = future_slot(capacity=1)
        booking_rules.create_booking(user=self.user, slot_id=slot.pk)
        other, _ = make_customer("other@example.com")

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=other, slot_id=slot.pk)

        slot.refresh_from_db()
        self.assertEqual(slot.seats_left, 0)

    def test_pending_bookings_count_towards_the_limit(self):
        for day in range(booking_rules.MAX_ACTIVE_BOOKINGS):
            booking_rules.create_booking(user=self.user, slot_id=future_slot(days=day + 2).pk)

        with self.assertRaises(booking_rules.BookingError):
            booking_rules.create_booking(user=self.user, slot_id=future_slot(days=20).pk)

    def test_confirming_records_the_time_and_status(self):
        booking = booking_rules.create_booking(user=self.user, slot_id=future_slot().pk)

        booking_rules.confirm_booking(booking)

        booking.refresh_from_db()
        self.assertEqual(booking.status, BookingStatus.CONFIRMED)
        self.assertIsNotNone(booking.confirmed_at)


@override_settings(
    OUTBOX_BUCKET="test-outbox",
    MAIL_FROM="noreply@dakkamotors.com",
    STAFF_ALERT_EMAIL="staff@example.com",
)
class BookingEmailTests(TestCase):
    def setUp(self):
        self.user, _ = make_customer(email="buyer@example.com")
        self.car = make_car("MAIL-1", brand="Honda", model_name="N-Box")

    def queued_messages(self, fn):
        """Run fn and return the messages it queued.

        on_commit callbacks do not fire inside a TestCase's transaction, so they have to
        be captured explicitly - without this the emails would silently never be checked.
        """
        with mock.patch("cars.mail.boto3.client") as client:
            with self.captureOnCommitCallbacks(execute=True):
                fn()
            calls = client.return_value.put_object.call_args_list
        return [json.loads(call.kwargs["Body"].decode("utf-8")) for call in calls]

    def test_booking_alerts_staff_and_says_it_is_not_confirmed(self):
        messages = self.queued_messages(
            lambda: booking_rules.create_booking(
                user=self.user, slot_id=future_slot().pk, car=self.car
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
            lambda: booking_rules.create_booking(user=self.user, slot_id=future_slot().pk)
        )

        self.assertNotIn("buyer@example.com", [to for m in messages for to in m["to"]])

    def test_confirming_emails_the_customer_with_time_address_and_phone(self):
        booking = booking_rules.create_booking(
            user=self.user, slot_id=future_slot().pk, car=self.car
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
        booking = booking_rules.create_booking(user=self.user, slot_id=future_slot().pk)
        booking_rules.confirm_booking(booking)

        messages = self.queued_messages(lambda: booking_rules.confirm_booking(booking))

        self.assertEqual(messages, [])

    def test_staff_cancelling_tells_the_customer(self):
        booking = booking_rules.create_booking(user=self.user, slot_id=future_slot().pk)

        messages = self.queued_messages(lambda: booking_rules.cancel_by_staff(booking))

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["to"], ["buyer@example.com"])
        self.assertIn("cancelled", messages[0]["subject"].lower())

    def test_a_customer_cancelling_their_own_booking_sends_nothing(self):
        """They already know - an email would just be noise."""
        booking = booking_rules.create_booking(user=self.user, slot_id=future_slot().pk)

        messages = self.queued_messages(
            lambda: booking_rules.cancel_booking(user=self.user, booking_id=booking.pk)
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


def link_token(pending_email):
    """The raw token only exists in the email, so read it back out of the outbox."""
    return PendingRegistration.objects.get(email=pending_email)


@override_settings(**MAIL_SETTINGS)
class RegistrationCreatesNoAccountTests(ClearsThrottleMixin, TestCase):
    """The whole point: a User row only ever exists for a proved address."""

    def test_registering_creates_no_user(self):
        with mock.patch("cars.mail.boto3.client"):
            response = register(self.client)

        self.assertEqual(response.status_code, 202)
        self.assertFalse(get_user_model().objects.filter(email="new@example.com").exists())
        self.assertTrue(PendingRegistration.objects.filter(email="new@example.com").exists())

    def test_registering_does_not_sign_anyone_in(self):
        with mock.patch("cars.mail.boto3.client"):
            register(self.client)

        self.assertEqual(self.client.get("/api/auth/me/").status_code, 403)

    def test_the_password_is_never_stored_in_plaintext(self):
        with mock.patch("cars.mail.boto3.client"):
            register(self.client, password="hunter2ish")

        pending = PendingRegistration.objects.get(email="new@example.com")
        self.assertNotIn("hunter2ish", pending.password_hash)
        self.assertTrue(check_password("hunter2ish", pending.password_hash))

    def test_the_raw_token_is_not_stored(self):
        """A database leak must not hand someone a working activation link."""
        with mock.patch("cars.mail.boto3.client") as client:
            register(self.client)
            body = json.loads(client.return_value.put_object.call_args.kwargs["Body"].decode())

        raw = re.search(r"token=([\w\-]+)", body["text"]).group(1)
        pending = PendingRegistration.objects.get(email="new@example.com")
        self.assertNotEqual(pending.token_hash, raw)
        self.assertEqual(pending.token_hash, hashlib.sha256(raw.encode()).hexdigest())

    def test_registering_twice_replaces_the_pending_row(self):
        """A typo on the first attempt must not lock that address out for three days."""
        with mock.patch("cars.mail.boto3.client"):
            register(self.client)
            first = PendingRegistration.objects.get(email="new@example.com").token_hash
            register(self.client, phone="080-9999-0000")

        pending = PendingRegistration.objects.get(email="new@example.com")
        self.assertEqual(PendingRegistration.objects.count(), 1)
        self.assertNotEqual(pending.token_hash, first)
        self.assertEqual(pending.phone, "080-9999-0000")

    def test_an_address_that_already_has_an_account_is_told_to_sign_in(self):
        make_customer("taken@example.com")

        with mock.patch("cars.mail.boto3.client"):
            response = register(self.client, email="taken@example.com")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(PendingRegistration.objects.count(), 0)

    def test_customers_may_use_an_easy_password(self):
        with mock.patch("cars.mail.boto3.client"):
            response = register(self.client, password="123456")

        self.assertEqual(response.status_code, 202)

    def test_a_password_under_six_characters_is_still_refused(self):
        with mock.patch("cars.mail.boto3.client"):
            response = register(self.client, password="12345")

        self.assertEqual(response.status_code, 400)


@override_settings(**MAIL_SETTINGS)
class VerificationTests(ClearsThrottleMixin, TestCase):
    def register_and_get_token(self, **extra):
        with mock.patch("cars.mail.boto3.client") as client:
            register(self.client, **extra)
            body = json.loads(client.return_value.put_object.call_args.kwargs["Body"].decode())
        return re.search(r"token=([\w\-]+)", body["text"]).group(1)

    def verify(self, token):
        return self.client.post("/api/auth/verify/", {"token": token},
                                content_type="application/json")

    def test_the_link_creates_the_account_and_signs_them_in(self):
        token = self.register_and_get_token()

        response = self.verify(token)

        self.assertEqual(response.status_code, 200)
        user = get_user_model().objects.get(email="new@example.com")
        self.assertEqual(user.customer_profile.phone, "080-1234-5678")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(check_password("simple", user.password))
        # Signed in on this device.
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 200)

    def test_the_pending_row_is_consumed(self):
        token = self.register_and_get_token()

        self.verify(token)

        self.assertEqual(PendingRegistration.objects.count(), 0)

    def test_the_same_link_cannot_be_used_twice(self):
        token = self.register_and_get_token()
        self.verify(token)

        second = self.verify(token)

        self.assertEqual(second.status_code, 400)
        self.assertEqual(get_user_model().objects.filter(email="new@example.com").count(), 1)

    def test_a_bogus_token_creates_nothing(self):
        response = self.verify("not-a-real-token")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(get_user_model().objects.count(), 0)

    def test_an_expired_link_is_refused_and_leaves_no_account(self):
        token = self.register_and_get_token()
        PendingRegistration.objects.update(
            expires_at=timezone.now() - datetime.timedelta(minutes=1)
        )

        response = self.verify(token)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(get_user_model().objects.filter(email="new@example.com").exists())
        self.assertEqual(PendingRegistration.objects.count(), 0)

    def test_they_are_returned_to_the_booking_they_started(self):
        token = self.register_and_get_token(next="/cars/2008-daihatsu-tanto/test-drive")

        response = self.verify(token)

        self.assertEqual(response.json()["next"], "/cars/2008-daihatsu-tanto/test-drive")

    def test_an_offsite_next_is_discarded(self):
        """//evil.com and https://evil.com are both followable by a browser."""
        for hostile in ("//evil.com", "https://evil.com", "javascript:alert(1)"):
            with self.subTest(hostile=hostile):
                PendingRegistration.objects.all().delete()
                get_user_model().objects.all().delete()
                token = self.register_and_get_token(next=hostile)

                landing = self.verify(token).json()["next"]

                self.assertEqual(landing, "/account")

    def test_expired_rows_are_swept_when_someone_registers(self):
        self.register_and_get_token()
        PendingRegistration.objects.update(
            expires_at=timezone.now() - datetime.timedelta(days=1)
        )

        with mock.patch("cars.mail.boto3.client"):
            register(self.client, email="someone.else@example.com")

        self.assertEqual(
            list(PendingRegistration.objects.values_list("email", flat=True)),
            ["someone.else@example.com"],
        )

    def test_resend_is_silent_about_whether_the_signup_exists(self):
        with mock.patch("cars.mail.boto3.client"):
            known = self.client.post("/api/auth/resend/", {"email": "nobody@example.com"},
                                     content_type="application/json")
        self.assertEqual(known.status_code, 202)


@override_settings(**MAIL_SETTINGS)
class PasswordResetTests(ClearsThrottleMixin, TestCase):
    def request_reset(self, email):
        with mock.patch("cars.mail.boto3.client") as client:
            response = self.client.post("/api/auth/password-reset/", {"email": email},
                                        content_type="application/json")
            calls = client.return_value.put_object.call_args_list
        bodies = [json.loads(c.kwargs["Body"].decode()) for c in calls]
        return response, bodies

    def link_parts(self, body):
        uid = re.search(r"uid=([\w\-]+)", body["text"]).group(1)
        token = re.search(r"token=([\w\-]+)", body["text"]).group(1)
        return uid, token

    def test_a_customer_gets_a_link(self):
        user, _ = make_customer("buyer@example.com")

        response, bodies = self.request_reset("buyer@example.com")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["to"], ["buyer@example.com"])

    def test_an_unknown_address_gets_the_same_answer_and_no_email(self):
        response, bodies = self.request_reset("nobody@example.com")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(bodies, [])

    def test_a_staff_address_gets_no_link(self):
        """A manager can edit inventory and other staff. Anyone able to read that inbox
        must not be able to take the account over."""
        manager, _ = make_manager()
        manager.email = "manager@example.com"
        manager.save()

        response, bodies = self.request_reset("manager@example.com")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(bodies, [])

    def test_the_link_sets_a_new_password_and_signs_them_in(self):
        make_customer("buyer@example.com", password="old-password-1")
        _, bodies = self.request_reset("buyer@example.com")
        uid, token = self.link_parts(bodies[0])

        response = self.client.post("/api/auth/password-reset/confirm/",
                                    {"uid": uid, "token": token, "password": "newpass"},
                                    content_type="application/json")

        self.assertEqual(response.status_code, 200)
        user = get_user_model().objects.get(email="buyer@example.com")
        self.assertTrue(check_password("newpass", user.password))
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 200)

    def test_the_link_dies_once_the_password_changes(self):
        make_customer("buyer@example.com", password="old-password-1")
        _, bodies = self.request_reset("buyer@example.com")
        uid, token = self.link_parts(bodies[0])
        payload = {"uid": uid, "token": token, "password": "newpass"}
        self.client.post("/api/auth/password-reset/confirm/", payload,
                         content_type="application/json")

        again = self.client.post("/api/auth/password-reset/confirm/", payload,
                                 content_type="application/json")

        self.assertEqual(again.status_code, 400)

    def test_a_reset_password_may_be_easy_but_not_tiny(self):
        make_customer("buyer@example.com", password="old-password-1")
        _, bodies = self.request_reset("buyer@example.com")
        uid, token = self.link_parts(bodies[0])

        response = self.client.post("/api/auth/password-reset/confirm/",
                                    {"uid": uid, "token": token, "password": "abc"},
                                    content_type="application/json")

        self.assertEqual(response.status_code, 400)


class StaffPasswordsStayStrictTests(TestCase):
    def test_staff_password_rules_are_unchanged(self):
        """Relaxing things for customers must not relax them for staff."""
        from django.contrib.auth.password_validation import validate_password

        with self.assertRaises(DjangoValidationError):
            validate_password("123456")  # global validators still apply

        # ...while the customer set accepts it.
        from cars.auth_views import CUSTOMER_PASSWORD_VALIDATORS
        validate_password("123456", password_validators=CUSTOMER_PASSWORD_VALIDATORS)


class PrimaryImageFallbackTests(TestCase):
    def test_falls_back_to_lowest_order_when_nothing_is_flagged(self):
        car = make_car("FALLBACK")
        attach_image(car, "third.gif", order=3)
        lowest = attach_image(car, "first.gif", order=1)

        self.assertEqual(car.primary_image, lowest)

    def test_returns_none_when_there_are_no_images(self):
        self.assertIsNone(make_car("EMPTY").primary_image)


class EmailTemplateTests(TestCase):
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
        pending = PendingRegistration.objects.create(
            email="new@example.com", name="Yuki", phone="080-1234-5678",
            password_hash="x", token_hash="y",
            expires_at=timezone.now() + datetime.timedelta(days=3),
        )
        with override_settings(**MAIL_SETTINGS):
            with mock.patch("cars.mail.boto3.client") as client:
                mail.send_verification_email(pending, "https://dakkamotors.com/v?token=abc")
                body = json.loads(client.return_value.put_object.call_args.kwargs["Body"].decode())

        self.assertTrue(body["text"].strip())
        self.assertIn("https://dakkamotors.com/v?token=abc", body["text"])
        self.assertNotIn("<", body["text"])

    def test_a_customer_name_cannot_inject_markup(self):
        """The staff alert interpolates a name the customer chose."""
        user, _ = make_customer("sneaky@example.com")
        user.first_name = "<script>alert(1)</script>"
        user.save()
        slot = future_slot()
        booking = TestDriveBooking.objects.create(customer=user, slot=slot, car_label="Tanto")

        with override_settings(**MAIL_SETTINGS):
            with mock.patch("cars.mail.boto3.client") as client:
                mail.notify_staff_of_booking(booking)
                body = json.loads(client.return_value.put_object.call_args.kwargs["Body"].decode())

        self.assertNotIn("<script>", body["html"])
        self.assertIn("&lt;script&gt;", body["html"])


class CarQuestionModelTests(TestCase):
    """The schema-level guarantees. These hold whatever the admin or a view does."""

    def setUp(self):
        self.car = make_car("QA-1", brand="Daihatsu", model_name="Tanto")

    def test_publishing_without_an_answer_is_refused_by_the_database(self):
        """The guard that survives someone adding list_editable to the admin later.

        A ModelForm's clean() does not run on the admin changelist, so this constraint
        is the only thing standing between a bulk tick-box and a published blank.
        """
        question = CarQuestion.objects.create(car=self.car, question="Is it rust free?")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CarQuestion.objects.filter(pk=question.pk).update(is_published=True)

    def test_publishing_with_an_answer_is_allowed(self):
        question = CarQuestion.objects.create(
            car=self.car, question="Is it rust free?", answer="Yes, underside is clean."
        )

        CarQuestion.objects.filter(pk=question.pk).update(is_published=True)

        question.refresh_from_db()
        self.assertTrue(question.is_published)

    def test_closing_an_account_keeps_the_published_pair(self):
        """A published pair is indexed page content; it must outlive the asker."""
        user, _ = make_customer("asker@example.com")
        question = CarQuestion.objects.create(
            car=self.car, customer=user, question="Any service history?",
            answer="Full history.", is_published=True,
        )

        user.delete()

        question.refresh_from_db()
        self.assertIsNone(question.customer)
        self.assertTrue(question.is_published)

    def test_state_reads_as_the_work_still_to_do(self):
        question = CarQuestion.objects.create(car=self.car, question="Colour?")
        self.assertEqual(question.state, "Needs an answer")

        question.answer = "Pearl white."
        question.answered_at = timezone.now()
        self.assertEqual(question.state, "Answered, not public")

        question.is_published = True
        self.assertEqual(question.state, "Published")


class NotificationModelTests(TestCase):
    def setUp(self):
        self.user, _ = make_customer("bell@example.com")

    def test_a_dedupe_key_can_only_be_used_once_per_customer(self):
        Notification.objects.create(
            customer=self.user, kind=NotificationKind.BOOKING_CONFIRMED,
            dedupe_key="booking:1:confirmed",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Notification.objects.create(
                    customer=self.user, kind=NotificationKind.BOOKING_CONFIRMED,
                    dedupe_key="booking:1:confirmed",
                )

    def test_rows_without_a_dedupe_key_are_not_constrained(self):
        """The unique constraint is conditional; blank keys must stay free."""
        for _ in range(3):
            Notification.objects.create(
                customer=self.user, kind=NotificationKind.QUESTION_ANSWERED
            )

        self.assertEqual(Notification.objects.count(), 3)

    def test_the_same_key_may_be_used_for_a_different_customer(self):
        other, _ = make_customer("other@example.com")
        Notification.objects.create(
            customer=self.user, kind=NotificationKind.BOOKING_CONFIRMED,
            dedupe_key="booking:1:confirmed",
        )

        Notification.objects.create(
            customer=other, kind=NotificationKind.BOOKING_CONFIRMED,
            dedupe_key="booking:1:confirmed",
        )

        self.assertEqual(Notification.objects.count(), 2)

    def test_deleting_a_customer_takes_their_notifications(self):
        Notification.objects.create(
            customer=self.user, kind=NotificationKind.QUESTION_ANSWERED
        )

        self.user.delete()

        self.assertEqual(Notification.objects.count(), 0)
