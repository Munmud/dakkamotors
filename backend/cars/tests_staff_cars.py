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
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from PIL import Image

from .specs import MAX_PAIRS
from .store import cars as car_store
from .store import images as image_store
from .store import media as media_store
from .store import videos as video_store
from .store.errors import NotFound
from .store.models import ChassisGuard, SlugGuard
from .store import keys
from .tests import DynamoReset, MAIL_SETTINGS, attach_photo, attach_video, make_car
from .tests_fake_cognito import FakeCognito, sign_in
from .tests_staff import make_owner, make_staff, make_staff_without_permissions


def a_jpeg(width=1000, height=750):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 130, 140)).save(buffer, "JPEG")
    return SimpleUploadedFile("photo.jpg", buffer.getvalue(),
                              content_type="image/jpeg")


def an_mp4(name="walkaround.mp4"):
    """Bytes, not a real container. Nothing here decodes one -- that is the point."""
    return SimpleUploadedFile(name, b"\x00\x00\x00\x18ftypmp42",
                              content_type="video/mp4")


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


def formset_fields(images=0, videos=0, specs=0):
    """The management forms every formset on the car page needs, with no rows filled in.

    All of them, always. A POST missing `videos-TOTAL_FORMS` raises a `ManagementForm`
    error rather than a validation error, so leaving one out fails every posting test at
    once with a message about formsets instead of about the car.
    """
    return {
        "images-TOTAL_FORMS": str(max(images, 3)),
        "images-INITIAL_FORMS": "0",
        "images-MIN_NUM_FORMS": "0",
        "images-MAX_NUM_FORMS": "1000",
        "videos-TOTAL_FORMS": str(max(videos, 1)),
        "videos-INITIAL_FORMS": "0",
        "videos-MIN_NUM_FORMS": "0",
        "videos-MAX_NUM_FORMS": "1000",
        "specs-TOTAL_FORMS": str(max(specs, 3)),
        "specs-INITIAL_FORMS": "0",
        "specs-MIN_NUM_FORMS": "0",
        "specs-MAX_NUM_FORMS": "1000",
    }


@override_settings(**MAIL_SETTINGS)
class StaffCarAccessTests(FakeCognito, DynamoReset, SimpleTestCase):
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
class StaffCarListTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)
        self.url = reverse("staff:car-list")

    def test_the_staff_pages_link_back_to_the_site(self):
        """There was no way out of the staff pages except editing the URL.

        Asserted on the list rather than in its own module because the masthead comes
        from `staff/base.html` and every staff page renders it.
        """
        self.assertContains(self.client.get(self.url), ">View site<")

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
class StaffCarEditTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)

    def test_adding_a_car_creates_it_with_a_slug(self):
        response = self.client.post(
            reverse("staff:car-add"),
            {**car_fields(chassis_number="NEW-1"), **formset_fields()}, follow=True)

        self.assertEqual(response.status_code, 200)
        cars = car_store.list_by_status("available")
        self.assertEqual(len(cars), 1)
        self.assertEqual(cars[0].slug, "2018-daihatsu-tanto-x")
        self.assertContains(response, "Now add its photos")

    def test_a_duplicate_chassis_number_is_refused_with_a_reason(self):
        make_car("TAKEN-1")

        response = self.client.post(
            reverse("staff:car-add"),
            {**car_fields(chassis_number="TAKEN-1", grade="Z"), **formset_fields()},
            follow=True)

        self.assertContains(response, "already on another car")
        self.assertEqual(len(car_store.list_by_status("available")), 1)

    def test_a_car_can_be_created_without_a_chassis_number(self):
        """Stock arrives before its paperwork does.

        Stored as absent rather than empty: the store decides whether the chassis moved
        by comparing it to the old one, and `""` against `None` reads as a change on
        every save of a car that never had one.
        """
        response = self.client.post(
            reverse("staff:car-add"),
            {**car_fields(chassis_number=""), **formset_fields()}, follow=True)

        self.assertEqual(response.status_code, 200)
        cars = car_store.list_by_status("available")
        self.assertEqual(len(cars), 1)
        self.assertIsNone(cars[0].chassis_number)

    def test_two_cars_can_both_have_no_chassis_number(self):
        """The thing a unique index would have got wrong.

        No number means no guard to collide on, so the second car is not a duplicate of
        the first -- it is two cars nobody has the paperwork for yet.
        """
        for grade in ("X", "Z"):
            self.client.post(
                reverse("staff:car-add"),
                {**car_fields(chassis_number="", grade=grade), **formset_fields()},
                follow=True)

        self.assertEqual(len(car_store.list_by_status("available")), 2)

    def test_clearing_a_chassis_number_frees_its_guard(self):
        car = make_car("CLEAR-1")

        self.client.post(reverse("staff:car-edit", args=[car.car_id]),
                         {**car_fields(chassis_number=""), **formset_fields()},
                         follow=True)

        self.assertIsNone(car_store.get(car.car_id).chassis_number)
        with self.assertRaises(ChassisGuard.DoesNotExist):
            ChassisGuard.get(keys.chassis_guard_pk("CLEAR-1"), keys.GUARD)

    def test_a_refused_car_leaves_no_orphan_slug_guard(self):
        """The reason the car and both guards go in one transaction."""
        make_car("TAKEN-2")

        self.client.post(reverse("staff:car-add"),
                         {**car_fields(chassis_number="TAKEN-2", grade="Z"),
                          **formset_fields()}, follow=True)

        with self.assertRaises(SlugGuard.DoesNotExist):
            SlugGuard.get(keys.slug_pk("2018-daihatsu-tanto-z"), "SLUG")

    def test_editing_does_not_rewrite_the_slug(self):
        """A URL that moves when someone fixes a typo breaks every shared link."""
        car = make_car("SLUG-1")
        before = car.slug

        self.client.post(reverse("staff:car-edit", args=[car.car_id]),
                         {**car_fields(chassis_number="SLUG-1", grade="Corrected"),
                          **formset_fields()}, follow=True)

        self.assertEqual(car_store.get(car.car_id).slug, before)
        self.assertEqual(car_store.get(car.car_id).grade, "Corrected")

    def test_changing_status_moves_the_car_between_listings(self):
        car = make_car("STATUS-1")

        self.client.post(reverse("staff:car-edit", args=[car.car_id]),
                         {**car_fields(chassis_number="STATUS-1", status="sold"),
                          **formset_fields()}, follow=True)

        self.assertEqual(car_store.list_by_status("available"), [])
        self.assertEqual(len(car_store.list_by_status("sold")), 1)

    def test_changing_the_chassis_number_frees_the_old_one(self):
        car = make_car("OLD-1")

        self.client.post(reverse("staff:car-edit", args=[car.car_id]),
                         {**car_fields(chassis_number="NEW-9"),
                          **formset_fields()}, follow=True)

        self.assertEqual(
            ChassisGuard.get(keys.chassis_guard_pk("NEW-9"), keys.GUARD).car_id,
            car.car_id)
        with self.assertRaises(ChassisGuard.DoesNotExist):
            ChassisGuard.get(keys.chassis_guard_pk("OLD-1"), keys.GUARD)

    def test_a_missing_car_is_a_404(self):
        self.assertEqual(
            self.client.get(reverse("staff:car-edit", args=["nope"])).status_code, 404)


@override_settings(**MAIL_SETTINGS)
class StaffCarPhotoTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)
        self.car = make_car("PHOTOS-1")
        self.url = reverse("staff:car-edit", args=[self.car.car_id])

    def post(self, extra=None, images=None):
        data = {**car_fields(chassis_number="PHOTOS-1"), **formset_fields()}
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


def spec_row(index, **fields):
    """One posted spec row. Every field, because a formset posts every field."""
    return {f"specs-{index}-{name}": fields.get(name, "")
            for name in ("label_en", "label_ja", "value_en", "value_ja")}


@override_settings(**MAIL_SETTINGS)
class StaffCarSpecTests(FakeCognito, DynamoReset, SimpleTestCase):
    """Free-form details, on both the add page and the edit page.

    They are on the add page unlike photos and videos, because they are attributes of
    the car item and land in the same conditional write as its guards -- so the first
    assertion here is that a car can be created with them in one POST.
    """

    def setUp(self):
        super().setUp()
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)

    def test_a_car_can_be_created_with_specs(self):
        self.client.post(
            reverse("staff:car-add"),
            {**car_fields(chassis_number="SPEC-NEW"), **formset_fields(),
             **spec_row(0, label_en="Colour", value_en="White")},
            follow=True)

        car = car_store.list_by_status("available")[0]

        self.assertEqual(len(car.specs), 1)
        self.assertEqual(car.specs[0]["label_en"], "Colour")
        self.assertEqual(car.specs[0]["value_en"], "White")

    def test_specs_keep_the_order_they_were_typed_in(self):
        self.client.post(
            reverse("staff:car-add"),
            {**car_fields(chassis_number="SPEC-ORDER"), **formset_fields(),
             **spec_row(0, label_en="Second", value_en="2"),
             **spec_row(1, label_en="First", value_en="1")},
            follow=True)

        car = car_store.list_by_status("available")[0]

        self.assertEqual([r["label_en"] for r in car.specs], ["Second", "First"])

    def test_a_half_filled_row_is_refused_rather_than_dropped(self):
        """Somebody typed a label and tabbed away. Saying so beats swallowing it."""
        response = self.client.post(
            reverse("staff:car-add"),
            {**car_fields(chassis_number="SPEC-HALF"), **formset_fields(),
             **spec_row(0, label_en="Colour")},
            follow=True)

        self.assertContains(response, "Give this detail a value")
        self.assertEqual(car_store.list_by_status("available"), [])

    def test_blank_rows_are_ignored(self):
        """Three spare rows are on every page and must not be three errors."""
        self.client.post(
            reverse("staff:car-add"),
            {**car_fields(chassis_number="SPEC-BLANK"), **formset_fields()},
            follow=True)

        car = car_store.list_by_status("available")[0]

        self.assertIsNone(car.specs)

    def test_editing_replaces_the_whole_list(self):
        car = make_car("SPEC-EDIT")
        self.client.post(
            reverse("staff:car-edit", args=[car.car_id]),
            {**car_fields(chassis_number="SPEC-EDIT"), **formset_fields(),
             **spec_row(0, label_en="Colour", value_en="White")},
            follow=True)

        self.client.post(
            reverse("staff:car-edit", args=[car.car_id]),
            {**car_fields(chassis_number="SPEC-EDIT"), **formset_fields(),
             **spec_row(0, label_en="Mileage", value_en="42,000 km")},
            follow=True)

        specs = car_store.get(car.car_id).specs

        self.assertEqual([r["label_en"] for r in specs], ["Mileage"])

    def test_the_edit_page_shows_what_is_already_there(self):
        car = make_car("SPEC-SHOW")
        self.client.post(
            reverse("staff:car-edit", args=[car.car_id]),
            {**car_fields(chassis_number="SPEC-SHOW"), **formset_fields(),
             **spec_row(0, label_en="Tow bar", value_en="Fitted")},
            follow=True)

        page = self.client.get(reverse("staff:car-edit", args=[car.car_id]))

        self.assertContains(page, "Tow bar")
        self.assertContains(page, "Fitted")

    def test_a_collision_re_renders_what_was_typed(self):
        """The one case where somebody has just filled the whole page in."""
        make_car("SPEC-TAKEN")

        response = self.client.post(
            reverse("staff:car-add"),
            {**car_fields(chassis_number="SPEC-TAKEN", grade="Z"), **formset_fields(),
             **spec_row(0, label_en="Tow bar", value_en="Fitted")},
            follow=True)

        self.assertContains(response, "already on another car")
        self.assertContains(response, "Tow bar")
        self.assertContains(response, "Fitted")

    def test_over_the_cap_is_refused(self):
        rows = {}
        for index in range(MAX_PAIRS + 1):
            rows.update(spec_row(index, label_en=str(index), value_en=str(index)))

        response = self.client.post(
            reverse("staff:car-add"),
            {**car_fields(chassis_number="SPEC-MANY"),
             **formset_fields(specs=MAX_PAIRS + 1), **rows},
            follow=True)

        self.assertContains(response, "at most")
        self.assertEqual(car_store.list_by_status("available"), [])


@override_settings(**MAIL_SETTINGS)
class StaffCarVideoTests(FakeCognito, DynamoReset, SimpleTestCase):
    """Adding, ordering and removing videos -- none of which was possible before.

    A car held one video in a column on itself, with no way to replace it with nothing.
    The assertions worth having are therefore the ones about *many* and about *none*.
    """

    def setUp(self):
        super().setUp()
        self.staff, _ = make_staff()
        sign_in(self.client, self.staff, staff=True)
        self.car = make_car("VIDEOS-1")
        self.url = reverse("staff:car-edit", args=[self.car.car_id])

    def post(self, extra=None, files=None, videos=1):
        data = {**car_fields(chassis_number="VIDEOS-1"), **formset_fields(videos=videos)}
        data.update(extra or {})
        return self.client.post(self.url, {**data, **(files or {})}, follow=True)

    def test_uploading_a_video_attaches_it(self):
        response = self.post(files={"videos-0-video": an_mp4()})

        self.assertContains(response, "Added 1 video")
        self.assertEqual(len(video_store.for_car(self.car.car_id)), 1)

    def test_a_car_can_hold_several_videos(self):
        """The feature, stated plainly: the old column could hold exactly one."""
        self.post(files={"videos-0-video": an_mp4("a.mp4"),
                         "videos-1-video": an_mp4("b.mp4")}, videos=2)

        self.assertEqual(len(video_store.for_car(self.car.car_id)), 2)

    def test_a_video_never_becomes_the_listing_card(self):
        attach_photo(self.car, 900, 600)
        self.post(files={"videos-0-video": an_mp4()},
                  extra={"videos-0-order": "0"})

        card = car_store.get(self.car.car_id).primary_image

        self.assertIsNotNone(card)
        self.assertTrue(card.sk.startswith("IMG#"))

    def test_removing_a_video_deletes_its_file(self):
        """Removal is new. The old field had no way to go back to having no video."""
        clip = attach_video(self.car, "gone.mp4")
        self.assertTrue(default_storage.exists(clip.video_name))

        self.post(extra={f"video-{clip.video_id}-delete": "on"})

        self.assertEqual(video_store.for_car(self.car.car_id), [])
        self.assertFalse(default_storage.exists(clip.video_name))

    def test_a_video_can_be_ordered_ahead_of_a_photo(self):
        photo = attach_photo(self.car, 900, 600)
        clip = attach_video(self.car, "lead.mp4", order=9)

        self.post(extra={
            f"video-{clip.video_id}-order": "0",
            f"image-{photo.image_id}-order": "1",
        })

        gallery = media_store.gallery(self.car.car_id)
        self.assertEqual([kind for kind, _ in gallery], ["video", "photo"])

    def test_leading_with_a_video_leaves_the_card_a_photo(self):
        """The invariant the whole design is arranged around, through the real view."""
        photo = attach_photo(self.car, 900, 600)
        clip = attach_video(self.car, "lead.mp4")

        self.post(extra={
            f"video-{clip.video_id}-order": "0",
            f"image-{photo.image_id}-order": "1",
        })

        self.assertEqual(car_store.get(self.car.car_id).primary_image.image_id,
                         photo.image_id)

    def test_a_blank_video_row_is_ignored(self):
        """Every save posts an empty row, so a blank one must not be an error."""
        response = self.post()

        self.assertContains(response, "Saved.")
        self.assertNotContains(response, "Added 1 video")
        self.assertEqual(video_store.for_car(self.car.car_id), [])

    def test_the_edit_page_lists_photos_and_videos_together(self):
        attach_photo(self.car, 900, 600)
        attach_video(self.car, "shown.mp4")

        page = self.client.get(self.url)

        self.assertContains(page, "Gallery")
        self.assertContains(page, "videos-0-video")
        self.assertContains(page, "images-0-image")


@override_settings(**MAIL_SETTINGS)
class StaffCarDeleteTests(FakeCognito, DynamoReset, SimpleTestCase):
    def setUp(self):
        super().setUp()
        # Delete is owner-only in the permission map.
        self.staff, _ = make_owner()
        sign_in(self.client, self.staff, staff=True)

    def test_deleting_a_car_takes_its_videos_too(self):
        """A video row lives in the car's partition and would outlive it silently."""
        car = make_car("DELETE-VIDEO")
        clip = attach_video(car, "doomed.mp4")

        self.client.post(reverse("staff:car-edit", args=[car.car_id]),
                         {"action": "delete"}, follow=True)

        self.assertEqual(video_store.for_car(car.car_id), [])
        self.assertFalse(default_storage.exists(clip.video_name))

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
