import io
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image

from .management.commands.ensure_inventory_group import GROUP_NAME
from .models import Car, CarImage, CarStatus
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
            },
        )

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


class PrimaryImageFallbackTests(TestCase):
    def test_falls_back_to_lowest_order_when_nothing_is_flagged(self):
        car = make_car("FALLBACK")
        attach_image(car, "third.gif", order=3)
        lowest = attach_image(car, "first.gif", order=1)

        self.assertEqual(car.primary_image, lowest)

    def test_returns_none_when_there_are_no_images(self):
        self.assertIsNone(make_car("EMPTY").primary_image)
