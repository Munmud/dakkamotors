"""The one-shot Aurora -> DynamoDB migration, exercised end to end.

Worth testing properly because it runs exactly once, against real data, inside a
maintenance window, and a mistake is discovered by a customer rather than by a test.
The rehearsal these support -- run it against a snapshot restore until it is boring --
is the whole plan for the cutover.

The ORM models still exist, unused by the application, precisely so the exporter has
something to read. They go when Cognito removes the last reason to keep Django's auth
tables, which is also when the permissions the staff pages hang off stop being rows.
"""

import datetime as dt
import io
import json
import tempfile

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .booking_models import (
    CustomerProfile, TestDriveBooking, TestDriveSchedule, TestDriveSlot,
)
from .models import Car as OrmCar, CarImage as OrmCarImage
from .notification_models import Notification as OrmNotification
from .qa_models import CarQuestion as OrmCarQuestion
from .store import auth as auth_store
from .store import bookings as booking_store
from .store import cars as car_store
from .store import customers as customer_store
from .store import images as image_store
from .store import keys
from .store import notifications as notification_store
from .store import questions as question_store
from .store import schedules as schedule_store
from .store import slots as slot_store
from .store.models import ChassisGuard, LegacyCarPointer, Seat, SlugGuard
from .tests import DynamoReset, MAIL_SETTINGS
from .tests_fake_cognito import FakeCognito, sign_in


@override_settings(**MAIL_SETTINGS)
class MigrationRoundTripTests(FakeCognito, DynamoReset, TestCase):
    """Build a small but complete Postgres state, export it, import it, check it."""

    def setUp(self):
        super().setUp()
        self.now = timezone.now()
        self.out = tempfile.mkdtemp()
        self._build_postgres()

    def _build_postgres(self):
        User = get_user_model()
        self.customer = User.objects.create_user(
            username="buyer@example.com", email="buyer@example.com",
            password="customer-pw-1234", first_name="Aiko", last_name="Tanaka")
        CustomerProfile.objects.create(user=self.customer, phone="080-1111-2222")
        self.staff = User.objects.create_user(username="staffer",
                                              password="staff-pw-123456")
        self.staff.is_staff = True
        self.staff.save()

        self.car = OrmCar.objects.create(
            brand="Daihatsu", model_name="Tanto", grade="X", model_code="LA600S",
            chassis_number="L375S-0012345", manufacture_year=2018,
            seat_capacity=4, color="Pearl White", price_jpy=690000,
            description_en="Clean example.", description_ja="きれいな一台です。",
        )
        self.sold = OrmCar.objects.create(
            brand="Suzuki", model_name="Alto", chassis_number="HA36S-0000001",
            manufacture_year=2016, status="sold",
        )
        self.image = OrmCarImage.objects.create(
            car=self.car, image="cars/front.jpg", is_primary=True, order=0)
        OrmCarImage.objects.filter(pk=self.image.pk).update(
            derivatives_ready=True, derivative_widths="320,800")

        self.schedule = TestDriveSchedule.objects.create(
            weekday=4, start_time=dt.time(18, 30), end_time=dt.time(19, 0), capacity=2)
        starts = self.now + dt.timedelta(days=3)
        self.slot = TestDriveSlot.objects.create(
            schedule=self.schedule, starts_at=starts,
            ends_at=starts + dt.timedelta(minutes=30), capacity=2)
        self.booking = TestDriveBooking.objects.create(
            slot=self.slot, customer=self.customer, car=self.car,
            car_label="2018 Daihatsu Tanto X", status="confirmed",
            confirmed_at=self.now)
        self.cancelled = TestDriveBooking.objects.create(
            slot=self.slot, customer=self.customer, car=self.car,
            car_label="2018 Daihatsu Tanto X", status="cancelled",
            cancelled_at=self.now)

        self.question = OrmCarQuestion.objects.create(
            car=self.car, customer=self.customer, question="Any service history?",
            answer="Full history, stamped.", answered_at=self.now,
            answered_by=self.staff, is_published=True)
        self.notification = OrmNotification.objects.create(
            customer=self.customer, kind="booking_confirmed",
            context={"car_label": "2018 Daihatsu Tanto X"},
            dedupe_key=f"booking:{self.booking.pk}:confirmed")

    def migrate(self):
        call_command("export_aurora", out=self.out, stdout=io.StringIO())
        call_command("import_dynamo", source=self.out, stdout=io.StringIO(),
                     stderr=io.StringIO())

    # -- the manifest ---------------------------------------------------------------

    def test_the_manifest_records_a_count_and_a_digest_per_file(self):
        call_command("export_aurora", out=self.out, stdout=io.StringIO())

        manifest = json.loads(
            (open(f"{self.out}/manifest.json", encoding="utf-8").read()))

        self.assertEqual(manifest["cars"]["count"], 2)
        self.assertEqual(manifest["bookings"]["count"], 2)
        self.assertEqual(len(manifest["cars"]["sha256"]), 64)

    def test_a_truncated_file_is_refused_rather_than_half_imported(self):
        """The manifest exists to catch exactly this."""
        call_command("export_aurora", out=self.out, stdout=io.StringIO())
        path = f"{self.out}/cars.ndjson"
        lines = open(path, encoding="utf-8").read().split("\n")
        open(path, "w", encoding="utf-8").write(lines[0] + "\n")

        with self.assertRaises(Exception) as caught:
            call_command("import_dynamo", source=self.out, stdout=io.StringIO())
        self.assertIn("manifest says", str(caught.exception))

    # -- cars -----------------------------------------------------------------------

    def test_cars_arrive_with_their_guards_and_a_legacy_pointer(self):
        self.migrate()

        car = car_store.get(str(self.car.pk))
        self.assertEqual(car.brand, "Daihatsu")
        self.assertEqual(car.slug, self.car.slug)
        self.assertEqual(car.price_jpy, 690000)
        self.assertEqual(
            SlugGuard.get(keys.slug_pk(car.slug), "SLUG").car_id, str(self.car.pk))
        self.assertEqual(
            ChassisGuard.get(keys.chassis_guard_pk("L375S-0012345"),
                             keys.GUARD).car_id, str(self.car.pk))
        self.assertEqual(
            LegacyCarPointer.get(keys.legacy_car_pk(self.car.pk), keys.META).slug,
            self.car.slug)

    def test_a_sold_car_keeps_its_status_and_stays_out_of_the_listing(self):
        self.migrate()

        self.assertEqual([c.car_id for c in car_store.list_by_status("available")],
                         [str(self.car.pk)])
        self.assertEqual([c.car_id for c in car_store.list_by_status("sold")],
                         [str(self.sold.pk)])

    def test_photos_arrive_and_the_listing_card_reference_is_rebuilt(self):
        self.migrate()

        photos = image_store.for_car(str(self.car.pk))
        self.assertEqual(len(photos), 1)
        self.assertEqual(photos[0].image_name, "cars/front.jpg")
        self.assertEqual(photos[0].available_widths, [320, 800])
        card = car_store.get(str(self.car.pk)).primary_image
        self.assertIsNotNone(card)
        self.assertEqual(card.image_name, "cars/front.jpg")

    # -- availability ---------------------------------------------------------------

    def test_rules_and_slots_arrive_with_the_slot_id_remapped(self):
        """Slot ids are the one thing the migration does not preserve.

        They become (schedule, start time), which is what makes generating them
        idempotent - so bookings have to be repointed at the new id.
        """
        self.migrate()

        rules = schedule_store.all_rules()
        self.assertEqual(len(rules), 1)
        expected = keys.slot_id(str(self.schedule.pk), self.slot.starts_at)
        self.assertIsNotNone(slot_store.find(expected))
        self.assertEqual(booking_store.get(str(self.customer.pk),
                                           str(self.booking.pk)).slot_id, expected)

    # -- bookings -------------------------------------------------------------------

    def test_a_live_booking_arrives_with_its_seat_and_the_counters_agree(self):
        self.migrate()

        booking = booking_store.get(str(self.customer.pk), str(self.booking.pk))
        self.assertEqual(booking.status, "confirmed")
        self.assertEqual(booking.customer_phone, "080-1111-2222")
        self.assertEqual(booking.customer_name, "Aiko Tanaka")

        slot = slot_store.find(booking.slot_id)
        # One seat, not two: the cancelled booking holds nothing.
        self.assertEqual(slot.booked_count, 1)
        self.assertEqual(len(slot_store.roster(booking.slot_id)), 1)
        self.assertEqual(
            customer_store.get(str(self.customer.pk)).active_bookings, 1)

    def test_a_cancelled_booking_is_kept_as_history_but_holds_no_seat(self):
        self.migrate()

        everything = booking_store.for_customer(str(self.customer.pk), statuses=None)
        self.assertEqual(len(everything), 2)
        seats = slot_store.roster(
            keys.slot_id(str(self.schedule.pk), self.slot.starts_at))
        self.assertEqual(len(seats), 1)

    def test_the_password_is_carried_over_for_the_migration_trigger(self):
        """What turns the cutover from a disruptive event into a silent one.

        Cognito will not accept a hash on AdminCreateUser, so the alternative is
        emailing every customer to say their password stopped working.
        """
        self.migrate()

        record = auth_store.legacy_password("buyer@example.com")

        self.assertIsNotNone(record)
        # Carried verbatim. Asserting the pbkdf2 prefix would be wrong here: the suite
        # swaps in a fast hasher for speed, so what matters is that whatever Django
        # wrote arrives unaltered - `tests_user_migration` checks the real format
        # against the trigger that has to read it.
        self.customer.refresh_from_db()
        self.assertEqual(record.password_hash, self.customer.password)
        self.assertEqual(record.name, "Aiko Tanaka")
        self.assertEqual(record.phone, "080-1111-2222")

    def test_staff_passwords_are_not_carried_over(self):
        """Staff arrive through the hosted UI, where the owner sets them up."""
        self.migrate()

        self.assertIsNone(auth_store.legacy_password("staffer"))

    def test_the_customer_can_still_be_told_apart_from_the_staff_account(self):
        """Staff are people, not customers: no counter item, not in the list."""
        self.migrate()

        emails = [c.email for c in customer_store.all_customers()]
        self.assertEqual(emails, ["buyer@example.com"])

    # -- questions and notifications -------------------------------------------------

    def test_a_published_question_arrives_with_its_car_denormalised(self):
        self.migrate()

        published = question_store.published_for(str(self.car.pk))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].question, "Any service history?")
        self.assertEqual(published[0].car_brand, "Daihatsu")
        self.assertEqual(published[0].car_slug, self.car.slug)
        self.assertIsNotNone(published[0].answered_at)

    def test_notifications_arrive_with_their_dedupe_guard(self):
        self.migrate()

        feed = notification_store.recent(str(self.customer.pk))
        self.assertEqual(len(feed), 1)
        self.assertEqual(feed[0].context["car_label"], "2018 Daihatsu Tanto X")
        # The guard is what keeps notify() idempotent afterwards.
        again, created = notification_store.notify(
            customer_sub=str(self.customer.pk), kind="booking_confirmed", context={},
            dedupe_key=f"booking:{self.booking.pk}:confirmed", now=timezone.now())
        self.assertFalse(created)

    # -- running it twice ------------------------------------------------------------

    def test_importing_twice_changes_nothing(self):
        """The rehearsal is to run it repeatedly against a snapshot until it is boring,
        so a second run must not duplicate anything or double a counter."""
        self.migrate()
        self.migrate()

        self.assertEqual(len(car_store.list_by_status("available")), 1)
        self.assertEqual(len(image_store.for_car(str(self.car.pk))), 1)
        self.assertEqual(len(booking_store.for_customer(
            str(self.customer.pk), statuses=None)), 2)
        self.assertEqual(
            customer_store.get(str(self.customer.pk)).active_bookings, 1)
        self.assertEqual(
            slot_store.find(keys.slot_id(str(self.schedule.pk),
                                         self.slot.starts_at)).booked_count, 1)


@override_settings(**MAIL_SETTINGS)
class ReconcileCountersTests(FakeCognito, DynamoReset, TestCase):
    """The repair for the two counters that replaced COUNT(*) under a lock."""

    def setUp(self):
        super().setUp()
        from .tests import future_slot, make_booking, make_customer

        self.customer, _ = make_customer("buyer@example.com")
        self.slot = future_slot(capacity=2)
        self.booking = make_booking(self.customer, self.slot)

    def test_a_correct_table_reports_no_drift(self):
        out = io.StringIO()
        call_command("reconcile_counters", stdout=out)
        self.assertIn("Every counter agrees", out.getvalue())

    def test_drift_is_reported_without_being_written(self):
        from .store.models import Slot

        slot = slot_store.get(self.slot.slot_id)
        slot.update(actions=[Slot.booked_count.set(7)])

        out = io.StringIO()
        call_command("reconcile_counters", stdout=out)

        self.assertIn("7 -> 1", out.getvalue())
        self.assertEqual(slot_store.get(self.slot.slot_id).booked_count, 7)

    def test_fix_repairs_both_counters_from_their_items(self):
        from .store.models import Customer, Slot

        slot_store.get(self.slot.slot_id).update(
            actions=[Slot.booked_count.set(7)])
        customer_store.get(str(self.customer.pk)).update(
            actions=[Customer.active_bookings.set(9)])

        call_command("reconcile_counters", fix=True, stdout=io.StringIO())

        self.assertEqual(slot_store.get(self.slot.slot_id).booked_count, 1)
        self.assertEqual(
            customer_store.get(str(self.customer.pk)).active_bookings, 1)
