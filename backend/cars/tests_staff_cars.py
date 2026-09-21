"""The inventory staff pages.

These replace `CarAdmin` and its `CarImageInline`, which was doing the most for free of
any admin class here. The things worth asserting are the ones it gave without being
asked: a slug that never moves, a chassis number that cannot be duplicated, photos whose
files actually leave the bucket, and a search that finds a car by part of its chassis
number.
"""

import io
import pathlib
import re

from django.contrib.staticfiles import finders
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

    A floor of one on each, though the page renders none: a test can post its first row
    as `images-0-image` without also declaring the count. Over-posting a blank row is
    harmless -- it cleans to nothing and `_apply_images` skips a row with no file.
    """
    return {
        "images-TOTAL_FORMS": str(max(images, 1)),
        "images-INITIAL_FORMS": "0",
        "images-MIN_NUM_FORMS": "0",
        "images-MAX_NUM_FORMS": "1000",
        "videos-TOTAL_FORMS": str(max(videos, 1)),
        "videos-INITIAL_FORMS": "0",
        "videos-MIN_NUM_FORMS": "0",
        "videos-MAX_NUM_FORMS": "1000",
        "specs-TOTAL_FORMS": str(max(specs, 1)),
        "specs-INITIAL_FORMS": "0",
        "specs-MIN_NUM_FORMS": "0",
        "specs-MAX_NUM_FORMS": str(MAX_PAIRS),
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

    def test_no_template_syntax_reaches_the_page(self):
        """A comment that renders is a comment in the navbar.

        `{# ... #}` cannot span lines in Django -- a multi-line one is not a comment at
        all, it is text, and it shipped as four lines of rationale across the top of
        every staff page. The link assertion above passed the whole time, because the
        link was fine; it was what sat next to it that was not.

        So this checks the rendered output for template syntax of any kind rather than
        for that one mistake.
        """
        for name in ("staff:car-list", "staff:car-add"):
            with self.subTest(page=name):
                html = self.client.get(reverse(name)).content.decode()
                for token in ("{#", "#}", "{%", "%}", "{{", "}}"):
                    self.assertNotIn(token, html)

    def test_every_static_reference_names_a_file_that_will_be_collected(self):
        """In production the static storage is a manifest storage, and a `{% static %}`
        for a file the manifest does not list is a `ValueError` -- a 500 on the page,
        for a typo in a filename. Locally the plain storage renders any name at all,
        so nothing else would catch it before the deploy did.

        Also refuses a literal `/static/` path: those are exactly what cannot be
        cache-busted, and the staff form shipped a stale script that way once.
        """
        templates = pathlib.Path(__file__).resolve().parent / "templates"
        referenced = set()
        for template in templates.rglob("*.html"):
            text = template.read_text(encoding="utf-8")
            self.assertNotIn('="/static/', text,
                             f"{template.name} hardcodes a /static/ path")
            referenced.update(re.findall(r"{%\s*static\s+['\"]([^'\"]+)['\"]", text))
        self.assertTrue(referenced, "the guard found nothing to check")
        for name in sorted(referenced):
            with self.subTest(file=name):
                self.assertIsNotNone(finders.find(name), f"{name} is not a static file")

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
            {**car_fields(), **formset_fields()}, follow=True)

        self.assertEqual(response.status_code, 200)
        cars = car_store.list_by_status("available")
        self.assertEqual(len(cars), 1)
        self.assertEqual(cars[0].slug, "2018-daihatsu-tanto")
        self.assertContains(response, "Now add its photos")

    def test_editing_a_car_keeps_the_fields_the_form_no_longer_owns(self):
        """Grade, model code and chassis number left the form. They must not leave the car.

        `_field_values` builds its dict from `EDITABLE`, and a name that is no longer on
        the form cleans to None -- so leaving one in that tuple writes None over the
        stored value on the next save. For the chassis that is worse than data loss:
        `store.cars.update` decides the number moved by comparing new to old, so
        None != "KEEP-1" fires the transaction path and deletes the guard for a number
        still sitting on the car.

        Silent, on first edit, and invisible to any smoke test.
        """
        car = make_car("KEEP-1", grade="Custom G", model_code="JF3")

        self.client.post(reverse("staff:car-edit", args=[car.car_id]),
                         {**car_fields(), **formset_fields()}, follow=True)

        saved = car_store.get(car.car_id)
        self.assertEqual(saved.chassis_number, "KEEP-1")
        self.assertEqual(saved.grade, "Custom G")
        self.assertEqual(saved.model_code, "JF3")
        # The guard still points at the car, so the number is still reserved.
        self.assertEqual(
            ChassisGuard.get(keys.chassis_guard_pk("KEEP-1"), keys.GUARD).car_id,
            car.car_id)

    def test_a_car_added_here_has_no_chassis_number(self):
        """The form does not ask for one, so a new car simply has none.

        Absent rather than empty: `store.cars.update` decides the number moved by
        comparing new to old, and `""` against `None` would read as a change on every
        save of a car that never had one.
        """
        response = self.client.post(
            reverse("staff:car-add"),
            {**car_fields(), **formset_fields()}, follow=True)

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
                {**car_fields(grade=grade), **formset_fields()},
                follow=True)

        self.assertEqual(len(car_store.list_by_status("available")), 2)

    def test_a_refused_car_leaves_no_orphan_slug_guard(self):
        """The reason the car and both guards go in one transaction."""
        make_car("TAKEN-2")

        self.client.post(reverse("staff:car-add"),
                         {**car_fields(),
                          **formset_fields()}, follow=True)

        with self.assertRaises(SlugGuard.DoesNotExist):
            SlugGuard.get(keys.slug_pk("2018-daihatsu-tanto-z"), "SLUG")

    def test_editing_does_not_rewrite_the_slug(self):
        """A URL that moves when someone fixes a typo breaks every shared link."""
        car = make_car("SLUG-1")
        before = car.slug

        self.client.post(reverse("staff:car-edit", args=[car.car_id]),
                         {**car_fields(color="Corrected"),
                          **formset_fields()}, follow=True)

        self.assertEqual(car_store.get(car.car_id).slug, before)
        self.assertEqual(car_store.get(car.car_id).color, "Corrected")

    def test_changing_status_moves_the_car_between_listings(self):
        car = make_car("STATUS-1")

        self.client.post(reverse("staff:car-edit", args=[car.car_id]),
                         {**car_fields(status="sold"),
                          **formset_fields()}, follow=True)

        self.assertEqual(car_store.list_by_status("available"), [])
        self.assertEqual(len(car_store.list_by_status("sold")), 1)

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
        data = {**car_fields(), **formset_fields()}
        data.update(extra or {})
        return self.client.post(self.url, {**data, **(images or {})}, follow=True)

    def test_a_car_can_be_created_with_its_photos_in_one_go(self):
        """The add page takes media now.

        It used to redirect to the edit page to ask for them, which meant two round
        trips and a car in the inventory with no picture in between.
        """
        response = self.client.post(
            reverse("staff:car-add"),
            {**car_fields(), **formset_fields(),
             "images-0-image": a_jpeg()},
            follow=True)

        self.assertContains(response, "Added 1 photo")
        created = car_store.list_by_status("available")[0]
        self.assertEqual(len(image_store.for_car(created.car_id)), 1)
        self.assertIsNotNone(car_store.get(created.car_id).primary_image)

    def test_a_refused_create_keeps_the_uploaded_photo_key(self):
        """The bytes are already in S3 by the time the form is posted.

        Rebuilding the formsets unbound would drop the key pointing at them, so the
        retry uploads a second copy and the first is left in the bucket with nothing
        referring to it.

        Refused over the extra-details cap rather than a duplicate chassis, which the
        form can no longer produce -- it is the same `refused()`, reached the one way
        still open to it.
        """
        rows = {}
        for index in range(MAX_PAIRS + 1):
            rows.update(spec_row(index, label_en=str(index), value_en=str(index)))

        response = self.client.post(
            reverse("staff:car-add"),
            {**car_fields(), **formset_fields(specs=MAX_PAIRS + 1), **rows,
             "images-0-image_key": "cars/deadbeef/original"},
            follow=True)

        self.assertContains(response, "extra details")
        self.assertContains(response, "cars/deadbeef/original")

    def test_a_refused_create_attaches_nothing(self):
        """Media go on only after `cars.create` returns.

        An IMG# row written first would sit in a partition that never gets a car, and
        `refresh_primary` would not even get that far -- it GETs the META item.
        """
        blocker = make_car("ADD-ORPHAN")
        before = len(car_store.list_by_status("available"))
        # Refused over the extra-details cap: the form can no longer produce a duplicate
        # chassis, and this is the other route into the same `refused()`.
        rows = {}
        for index in range(MAX_PAIRS + 1):
            rows.update(spec_row(index, label_en=str(index), value_en=str(index)))

        self.client.post(
            reverse("staff:car-add"),
            {**car_fields(), **formset_fields(specs=MAX_PAIRS + 1), **rows,
             "images-0-image": a_jpeg()},
            follow=True)

        self.assertEqual(len(car_store.list_by_status("available")), before)
        # Not hung off either car that does exist, and there is no third partition it
        # could have landed in.
        self.assertEqual(image_store.for_car(blocker.car_id), [])
        self.assertEqual(image_store.for_car(self.car.car_id), [])

    def test_the_add_page_offers_the_media_inputs(self):
        """The means of adding a row, not a row.

        What the page renders is a template row for the script to clone and a button
        that clones it. The first file input exists only once somebody has asked for it.
        """
        page = self.client.get(reverse("staff:car-add"))

        self.assertContains(page, "images-__prefix__-image")
        self.assertContains(page, "videos-__prefix__-video")
        self.assertContains(page, 'data-formset-add="images"')
        self.assertContains(page, 'data-formset-add="videos"')
        self.assertNotContains(page, "Nothing in the gallery yet")

    def test_no_blank_media_rows_are_rendered(self):
        """Three empty file inputs on a car with no photos were never a feature."""
        for name in ("staff:car-add",):
            page = self.client.get(reverse(name))
            self.assertNotContains(page, "images-0-image")
            self.assertNotContains(page, "videos-0-video")
            self.assertNotContains(page, "specs-0-label_en")

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

    def test_moving_a_photo_up_changes_which_one_leads(self):
        first = attach_photo(self.car, 900, 600)
        second = attach_photo(self.car, 900, 600)

        self.post(extra={"move": f"up:image:{second.image_id}"})

        self.assertEqual(car_store.get(self.car.car_id).primary_image.image_id,
                         second.image_id)

    def test_moving_the_first_row_up_does_nothing(self):
        """An arrow at the end of the list has nowhere to go, and that is not an error."""
        first = attach_photo(self.car, 900, 600)
        attach_photo(self.car, 900, 600)

        response = self.post(extra={"move": f"up:image:{first.image_id}"})

        self.assertContains(response, "Saved.")
        self.assertEqual(car_store.get(self.car.car_id).primary_image.image_id,
                         first.image_id)

    def test_moving_a_row_deleted_in_the_same_save_does_nothing(self):
        """Deletions run first, so the row the arrow named is already gone."""
        first = attach_photo(self.car, 900, 600)
        second = attach_photo(self.car, 900, 600)

        response = self.post(extra={
            f"image-{second.image_id}-delete": "on",
            "move": f"up:image:{second.image_id}",
        })

        self.assertContains(response, "Saved.")
        remaining = image_store.for_car(self.car.car_id)
        self.assertEqual([i.image_id for i in remaining], [first.image_id])

    def test_a_new_photo_lands_at_the_end_of_the_gallery(self):
        """It used to land at the front.

        Every new row was written at order 0, so a photo added to a car that already had
        five jumped ahead of all of them and quietly became the listing card.
        """
        first = attach_photo(self.car, 900, 600)
        attach_photo(self.car, 900, 600)

        self.post(images={"images-0-image": a_jpeg()})

        gallery = media_store.gallery(self.car.car_id)
        self.assertEqual(len(gallery), 3)
        self.assertEqual(gallery[0][1].image_id, first.image_id)
        self.assertEqual(car_store.get(self.car.car_id).primary_image.image_id,
                         first.image_id)

    def test_a_blank_photo_row_is_ignored(self):
        """Every save posts three empty photo rows, so blank ones must not be errors."""
        response = self.post()

        self.assertContains(response, "Saved.")
        self.assertNotContains(response, "Added 1 photo")
        self.assertEqual(image_store.for_car(self.car.car_id), [])

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

    def test_the_add_page_renders_no_blank_rows(self):
        """A row appears when asked for, and not before.

        The blank rows were never a feature; they were the allowance from before the
        button existed, and three of them were the most anybody could add per save.
        """
        page = self.client.get(reverse("staff:car-add"))

        self.assertNotContains(page, "specs-0-label_en")
        self.assertContains(page, "specs-__prefix__-label_en")

    def test_the_spec_table_carries_a_template_row(self):
        """What the "+ Add a detail" button clones.

        The only assertion that catches the <template> being dropped or the partial
        being mis-included -- both of which leave a page that looks right and a button
        that silently does nothing.
        """
        page = self.client.get(reverse("staff:car-add"))

        self.assertContains(page, "specs-__prefix__-label_en")
        self.assertContains(page, 'data-formset-add="specs"')

    def test_the_cap_is_published_to_the_page(self):
        """The button stops at the cap, and it reads it from the management form.

        If this drifts the button keeps adding rows past the point `specs.clean` will
        accept them, and the refusal arrives after somebody has typed them all in.
        """
        page = self.client.get(reverse("staff:car-add"))

        self.assertContains(page, f'name="specs-MAX_NUM_FORMS" value="{MAX_PAIRS}"')

    def test_a_car_can_be_created_with_specs(self):
        self.client.post(
            reverse("staff:car-add"),
            {**car_fields(), **formset_fields(),
             **spec_row(0, label_en="Colour", value_en="White")},
            follow=True)

        car = car_store.list_by_status("available")[0]

        self.assertEqual(len(car.specs), 1)
        self.assertEqual(car.specs[0]["label_en"], "Colour")
        self.assertEqual(car.specs[0]["value_en"], "White")

    def test_specs_keep_the_order_they_were_typed_in(self):
        self.client.post(
            reverse("staff:car-add"),
            {**car_fields(), **formset_fields(specs=2),
             **spec_row(0, label_en="Second", value_en="2"),
             **spec_row(1, label_en="First", value_en="1")},
            follow=True)

        car = car_store.list_by_status("available")[0]

        self.assertEqual([r["label_en"] for r in car.specs], ["Second", "First"])

    def test_a_half_filled_row_is_refused_rather_than_dropped(self):
        """Somebody typed a label and tabbed away. Saying so beats swallowing it."""
        response = self.client.post(
            reverse("staff:car-add"),
            {**car_fields(), **formset_fields(),
             **spec_row(0, label_en="Colour")},
            follow=True)

        self.assertContains(response, "Give this detail a value")
        self.assertEqual(car_store.list_by_status("available"), [])

    def test_blank_rows_are_ignored(self):
        """A spare row is on every page and must not be an error."""
        self.client.post(
            reverse("staff:car-add"),
            {**car_fields(), **formset_fields()},
            follow=True)

        car = car_store.list_by_status("available")[0]

        self.assertIsNone(car.specs)

    def test_editing_replaces_the_whole_list(self):
        car = make_car("SPEC-EDIT")
        self.client.post(
            reverse("staff:car-edit", args=[car.car_id]),
            {**car_fields(), **formset_fields(),
             **spec_row(0, label_en="Colour", value_en="White")},
            follow=True)

        self.client.post(
            reverse("staff:car-edit", args=[car.car_id]),
            {**car_fields(), **formset_fields(),
             **spec_row(0, label_en="Mileage", value_en="42,000 km")},
            follow=True)

        specs = car_store.get(car.car_id).specs

        self.assertEqual([r["label_en"] for r in specs], ["Mileage"])

    def test_the_edit_page_shows_what_is_already_there(self):
        car = make_car("SPEC-SHOW")
        self.client.post(
            reverse("staff:car-edit", args=[car.car_id]),
            {**car_fields(), **formset_fields(),
             **spec_row(0, label_en="Tow bar", value_en="Fitted")},
            follow=True)

        page = self.client.get(reverse("staff:car-edit", args=[car.car_id]))

        self.assertContains(page, "Tow bar")
        self.assertContains(page, "Fitted")

    def test_a_collision_re_renders_what_was_typed(self):
        """The one case where somebody has just filled the whole page in."""
        make_car("SPEC-TAKEN")

        rows = {}
        for index in range(1, MAX_PAIRS + 1):
            rows.update(spec_row(index, label_en=str(index), value_en=str(index)))

        response = self.client.post(
            reverse("staff:car-add"),
            {**car_fields(), **formset_fields(specs=MAX_PAIRS + 1),
             **spec_row(0, label_en="Tow bar", value_en="Fitted"), **rows},
            follow=True)

        self.assertContains(response, "extra details")
        self.assertContains(response, "Tow bar")
        self.assertContains(response, "Fitted")

    def test_over_the_cap_is_refused(self):
        rows = {}
        for index in range(MAX_PAIRS + 1):
            rows.update(spec_row(index, label_en=str(index), value_en=str(index)))

        response = self.client.post(
            reverse("staff:car-add"),
            {**car_fields(),
             **formset_fields(specs=MAX_PAIRS + 1), **rows},
            follow=True)

        # The whole sentence, not just "at most". Django's own `validate_max` message
        # is "Please submit at most N forms", so the short substring passes whichever
        # of the two refused it -- and the point of `validate_max=False` is that
        # `specs.clean` is the only thing that does.
        self.assertContains(response, f"at most {MAX_PAIRS} extra details")
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
        data = {**car_fields(), **formset_fields(videos=videos)}
        data.update(extra or {})
        return self.client.post(self.url, {**data, **(files or {})}, follow=True)

    def test_a_car_can_be_created_with_a_video(self):
        response = self.client.post(
            reverse("staff:car-add"),
            {**car_fields(), **formset_fields(),
             "videos-0-video": an_mp4()},
            follow=True)

        self.assertContains(response, "Added 1 video")
        created = car_store.list_by_status("available")[0]
        self.assertEqual(len(video_store.for_car(created.car_id)), 1)

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

    def test_a_video_can_be_moved_ahead_of_a_photo(self):
        """The swap is over the merged gallery, not within a type."""
        attach_photo(self.car, 900, 600)
        clip = attach_video(self.car, "lead.mp4", order=9)

        self.post(extra={"move": f"up:video:{clip.video_id}"})

        gallery = media_store.gallery(self.car.car_id)
        self.assertEqual([kind for kind, _ in gallery], ["video", "photo"])

    def test_leading_with_a_video_leaves_the_card_a_photo(self):
        """The invariant the whole design is arranged around, through the real view."""
        photo = attach_photo(self.car, 900, 600)
        clip = attach_video(self.car, "lead.mp4")

        self.post(extra={"move": f"up:video:{clip.video_id}"})

        self.assertEqual(car_store.get(self.car.car_id).primary_image.image_id,
                         photo.image_id)

    def test_a_blank_video_row_is_ignored(self):
        """Every save posts an empty row, so a blank one must not be an error."""
        response = self.post()

        self.assertContains(response, "Saved.")
        self.assertNotContains(response, "Added 1 video")
        self.assertEqual(video_store.for_car(self.car.car_id), [])

    def test_the_edit_page_lists_photos_and_videos_together(self):
        photo = attach_photo(self.car, 900, 600)
        clip = attach_video(self.car, "shown.mp4")

        page = self.client.get(self.url)

        self.assertContains(page, "Gallery")
        # The attached media, in the gallery table; and the means of adding more.
        self.assertContains(page, f"video-{clip.video_id}-delete")
        self.assertContains(page, f"image-{photo.image_id}-delete")
        self.assertContains(page, 'data-formset-add="videos"')
        self.assertContains(page, 'data-formset-add="images"')
        self.assertNotContains(page, "videos-0-video")
        self.assertNotContains(page, "images-0-image")


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
