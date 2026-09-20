"""Tests for store.cars and store.images.

The two things worth proving here are the ones a unique index used to do for free:
a slug collision resolves without a race, and a rejected car leaves no orphan guard
reserving a URL forever.
"""

import datetime as dt

from .store import cars, images, keys
from .store.errors import ConditionFailed, NotFound
from .store.models import (
    Car, CarQuestion, ChassisGuard, LegacyCarPointer, SlugGuard,
)
from .tests_store import DynamoTestCase

UTC = dt.timezone.utc


def make_car(car_id, *, brand="Daihatsu", model_name="Tanto", grade="X",
             year=2008, chassis=None, status="available"):
    return Car(
        car_id=car_id,
        brand=brand,
        model_name=model_name,
        grade=grade,
        manufacture_year=year,
        chassis_number=chassis if chassis is not None else f"CHASSIS-{car_id}",
        status=status,
    )


class CarStoreTests(DynamoTestCase):
    def setUp(self):
        super().setUp()
        cars.clear_slug_cache()
        self.now = dt.datetime.now(UTC)

    def test_creating_a_car_writes_both_guards(self):
        car = cars.create(make_car("1"), now=self.now)
        self.assertEqual(car.slug, "2008-daihatsu-tanto")
        self.assertEqual(
            SlugGuard.get(keys.slug_pk(car.slug), "SLUG").car_id, "1")
        self.assertEqual(
            ChassisGuard.get(keys.chassis_guard_pk("CHASSIS-1"), keys.GUARD).car_id, "1")

    def test_the_slug_guard_is_also_the_lookup(self):
        cars.create(make_car("1"), now=self.now)
        self.assertEqual(cars.by_slug("2008-daihatsu-tanto").car_id, "1")
        self.assertIsNone(cars.by_slug("no-such-car"))

    def test_two_identically_named_cars_get_distinct_slugs(self):
        """Resolved by a conditional write, not by a check-then-write loop."""
        a = cars.create(make_car("1"), now=self.now)
        b = cars.create(make_car("2"), now=self.now)
        self.assertEqual(a.slug, "2008-daihatsu-tanto")
        self.assertEqual(b.slug, "2008-daihatsu-tanto-2")
        self.assertEqual(cars.by_slug(b.slug).car_id, "2")

    def test_a_duplicate_chassis_number_is_refused(self):
        cars.create(make_car("1", chassis="ABC123"), now=self.now)
        with self.assertRaises(ConditionFailed):
            cars.create(make_car("2", chassis="ABC123"), now=self.now)

    def test_a_refused_car_leaves_no_orphan_slug_guard(self):
        """The whole reason the car and both guards go in one transaction.

        An orphaned slug guard would reserve that URL for a car that does not exist,
        and nothing would ever clean it up.
        """
        cars.create(make_car("1", chassis="ABC123"), now=self.now)
        with self.assertRaises(ConditionFailed):
            cars.create(make_car("2", chassis="ABC123", grade="Z"), now=self.now)
        self.assertIsNone(cars.by_slug("2008-daihatsu-tanto-z"))
        with self.assertRaises(SlugGuard.DoesNotExist):
            SlugGuard.get(keys.slug_pk("2008-daihatsu-tanto-z"), "SLUG")

    def test_chassis_matching_ignores_case_and_padding(self):
        cars.create(make_car("1", chassis="abc123"), now=self.now)
        with self.assertRaises(ConditionFailed):
            cars.create(make_car("2", chassis="  ABC123 "), now=self.now)

    def test_listing_is_newest_first_and_filtered_by_status(self):
        for i in range(3):
            cars.create(make_car(str(i), grade=f"G{i}"), now=self.now + dt.timedelta(minutes=i))
        cars.create(make_car("9", grade="Sold", status="sold"), now=self.now)
        available = cars.list_by_status("available")
        self.assertEqual([c.car_id for c in available], ["2", "1", "0"])
        self.assertEqual([c.car_id for c in cars.list_by_status("sold")], ["9"])

    def test_the_sitemap_sees_available_and_reserved_but_not_sold(self):
        cars.create(make_car("1", grade="A"), now=self.now)
        cars.create(make_car("2", grade="B", status="reserved"), now=self.now)
        cars.create(make_car("3", grade="C", status="sold"), now=self.now)
        self.assertEqual({c.car_id for c in cars.for_sitemap()}, {"1", "2"})

    def test_changing_status_moves_the_car_between_listings(self):
        car = cars.create(make_car("1"), now=self.now)
        cars.update(car, now=self.now, status="sold")
        self.assertEqual(cars.list_by_status("available"), [])
        self.assertEqual([c.car_id for c in cars.list_by_status("sold")], ["1"])

    def test_changing_the_chassis_number_moves_its_guard(self):
        car = cars.create(make_car("1", chassis="OLD1"), now=self.now)
        cars.update(car, now=self.now, chassis_number="NEW1")
        self.assertEqual(
            ChassisGuard.get(keys.chassis_guard_pk("NEW1"), keys.GUARD).car_id, "1")
        with self.assertRaises(ChassisGuard.DoesNotExist):
            ChassisGuard.get(keys.chassis_guard_pk("OLD1"), keys.GUARD)
        # And the freed number can be reused.
        cars.create(make_car("2", chassis="OLD1", grade="Z"), now=self.now)

    def test_a_blank_price_stays_absent_rather_than_zero(self):
        """"Call for price" is deliberately distinct from a price of zero."""
        car = cars.create(make_car("1"), now=self.now)
        self.assertIsNone(car.price_jpy)
        self.assertIsNone(cars.get("1").price_jpy)

    def test_deleting_a_car_takes_its_guards_and_children(self):
        car = cars.create(make_car("1"), now=self.now)
        images.create(car_id="1", image_name="cars/a.jpg", now=self.now)
        CarQuestion(pk=keys.car_pk("1"), sk=keys.question_sk(self.now, "q1"),
                    question_id="q1", car_id="1", question="?").save()

        cars.delete(car)

        with self.assertRaises(NotFound):
            cars.get("1")
        with self.assertRaises(SlugGuard.DoesNotExist):
            SlugGuard.get(keys.slug_pk(car.slug), "SLUG")
        self.assertEqual(images.for_car("1"), [])

    def test_deleting_a_car_takes_its_legacy_pointer(self):
        """Otherwise an old numeric URL 301s permanently to a dead slug.

        `config/urls.py` reads this pointer to redirect `/cars/34` to the slug. A 301 is
        cached by crawlers and browsers, so one left pointing at a deleted car is worse
        than a 404 -- nothing ever asks again. Once the car is gone the redirect is meant
        to fall through to `/`.
        """
        car = cars.create(make_car("34"), now=self.now)
        LegacyCarPointer(pk=keys.legacy_car_pk("34"), sk=keys.META,
                         slug=car.slug, car_id="34").save()
        self.assertEqual(cars.slug_for_legacy_id("34"), car.slug)

        cars.delete(car)

        self.assertIsNone(cars.slug_for_legacy_id("34"))

    def test_deleting_a_car_that_never_had_a_pointer_is_fine(self):
        """Cars created after the migration have none, and DeleteItem on a missing key
        succeeds -- which is why the delete carries no condition."""
        car = cars.create(make_car("2"), now=self.now)

        cars.delete(car)

        with self.assertRaises(NotFound):
            cars.get("2")

    def test_bump_updated_at_moves_the_sitemap_lastmod(self):
        car = cars.create(make_car("1"), now=self.now)
        later = self.now + dt.timedelta(hours=1)
        cars.bump_updated_at("1", later)
        self.assertEqual(cars.get("1").updated_at, later)
        self.assertEqual(cars.get("1").created_at, car.created_at)


class CarDetailTests(DynamoTestCase):
    def setUp(self):
        super().setUp()
        cars.clear_slug_cache()
        self.now = dt.datetime.now(UTC)
        self.car = cars.create(make_car("1"), now=self.now)

    def test_detail_returns_car_images_and_questions_from_one_partition(self):
        images.create(car_id="1", image_name="cars/b.jpg", order=1, now=self.now)
        images.create(car_id="1", image_name="cars/a.jpg", order=0, now=self.now)
        CarQuestion(pk=keys.car_pk("1"), sk=keys.question_sk(self.now, "q1"),
                    question_id="q1", car_id="1", question="Accident free?",
                    answer="Yes.", is_published=True, created_at=self.now).save()

        car = cars.detail("1")
        self.assertEqual([i.image_name for i in car.images],
                         ["cars/a.jpg", "cars/b.jpg"])
        self.assertEqual(len(car.questions), 1)
        self.assertEqual(car.seo_title_plain, "2008 Daihatsu Tanto")

    def test_detail_by_slug_works_and_missing_slugs_raise(self):
        self.assertEqual(cars.detail_by_slug(self.car.slug).car_id, "1")
        with self.assertRaises(NotFound):
            cars.detail_by_slug("nope")


class PrimaryImageTests(DynamoTestCase):
    """The denormalised listing-card reference, and the five paths that must refresh it."""

    def setUp(self):
        super().setUp()
        cars.clear_slug_cache()
        self.now = dt.datetime.now(UTC)
        cars.create(make_car("1"), now=self.now)

    def test_a_car_with_no_images_has_no_primary(self):
        self.assertIsNone(cars.get("1").primary_image)

    def test_the_first_image_becomes_the_card_photo(self):
        images.create(car_id="1", image_name="cars/a.jpg", now=self.now)
        self.assertEqual(cars.get("1").primary_image.image_name, "cars/a.jpg")

    def test_an_explicit_primary_wins_over_display_order(self):
        images.create(car_id="1", image_name="cars/a.jpg", order=0, now=self.now)
        b = images.create(car_id="1", image_name="cars/b.jpg", order=1, now=self.now)
        images.set_primary("1", b.image_id)
        self.assertEqual(cars.get("1").primary_image.image_name, "cars/b.jpg")

    def test_only_one_image_stays_primary(self):
        a = images.create(car_id="1", image_name="cars/a.jpg", is_primary=True,
                          now=self.now)
        b = images.create(car_id="1", image_name="cars/b.jpg", is_primary=True,
                          now=self.now)
        flagged = [i.image_id for i in images.for_car("1") if i.is_primary]
        self.assertEqual(flagged, [b.image_id])
        self.assertNotEqual(a.image_id, b.image_id)

    def test_reordering_refreshes_the_card_photo(self):
        a = images.create(car_id="1", image_name="cars/a.jpg", order=0, now=self.now)
        b = images.create(car_id="1", image_name="cars/b.jpg", order=1, now=self.now)
        images.set_order("1", [b.image_id, a.image_id])
        self.assertEqual(cars.get("1").primary_image.image_name, "cars/b.jpg")

    def test_deleting_the_card_photo_promotes_the_next(self):
        a = images.create(car_id="1", image_name="cars/a.jpg", order=0, now=self.now)
        images.create(car_id="1", image_name="cars/b.jpg", order=1, now=self.now)
        images.delete("1", a.image_id, delete_files=False)
        self.assertEqual(cars.get("1").primary_image.image_name, "cars/b.jpg")

    def test_deleting_the_last_photo_clears_the_reference(self):
        a = images.create(car_id="1", image_name="cars/a.jpg", now=self.now)
        images.delete("1", a.image_id, delete_files=False)
        self.assertIsNone(cars.get("1").primary_image)

    def test_derivatives_ready_updates_the_reference_and_leaves_the_pending_index(self):
        a = images.create(car_id="1", image_name="cars/a.jpg", now=self.now)
        self.assertEqual([i.image_id for i in images.pending_derivatives()],
                         [a.image_id])

        images.mark_derivatives_ready("1", a.image_id, [320, 800])

        self.assertEqual(images.pending_derivatives(), [])
        card = cars.get("1").primary_image
        self.assertEqual(card.available_widths, [320, 800])
        self.assertEqual(card.derivative_name(800), "cars/a__w800.webp")

    def test_replacing_the_file_invalidates_the_old_derivatives(self):
        a = images.create(car_id="1", image_name="cars/a.jpg", now=self.now)
        images.mark_derivatives_ready("1", a.image_id, [320])
        images.replace_file("1", a.image_id, "cars/new.jpg")

        refreshed = images.get("1", a.image_id)
        self.assertFalse(refreshed.derivatives_ready)
        self.assertEqual(refreshed.available_widths, [])
        self.assertEqual([i.image_id for i in images.pending_derivatives()],
                         [a.image_id])

    def test_derivative_urls_are_empty_until_the_copies_exist(self):
        images.create(car_id="1", image_name="cars/a.jpg", now=self.now)
        self.assertEqual(cars.get("1").primary_image.derivative_urls, {})
