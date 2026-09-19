"""Tests for the store operation modules -- store.slots and store.bookings.

Where `tests_store.py` proves the mechanics (wire order, condition semantics), this
proves the rules: capacity, the per-customer limit, the one-live-booking guard, and that
cancelling really does release everything it took.
"""

import datetime as dt

from .choices import BookingStatus
from .store import bookings, keys, slots
from .store.errors import AlreadyBooked, LimitReached, NotActive, SlotUnavailable
from .store.models import Booking, Customer
from .tests_store import DynamoTestCase

UTC = dt.timezone.utc
MAX_ACTIVE = 3


class FakeCustomer:
    """Stands in for the Cognito-backed user the views will pass."""

    def __init__(self, sub, name="Aiko Tanaka", email=None, phone="080-1234-5678"):
        self.sub = sub
        self._name = name
        self.email = email or f"{sub}@example.com"
        self.phone = phone

    def get_full_name(self):
        return self._name


class FakeCar:
    car_id = "34"
    seo_title_plain = "2008 Daihatsu Tanto X"


class SlotStoreTests(DynamoTestCase):
    def setUp(self):
        super().setUp()
        self.now = dt.datetime.now(UTC)
        self.starts = self.now + dt.timedelta(days=2)

    def _ensure(self, offset_days=2, schedule_id="sched-1"):
        starts = self.now + dt.timedelta(days=offset_days)
        return slots.ensure(
            schedule_id=schedule_id, starts_at=starts,
            ends_at=starts + dt.timedelta(minutes=30), capacity=2,
        )

    def test_ensure_creates_once_and_is_idempotent(self):
        self.assertTrue(self._ensure())
        self.assertFalse(self._ensure())

    def test_regenerating_does_not_reopen_a_slot_staff_closed(self):
        """The reason `ensure` is a conditional put rather than a put.

        Staff untick one awkward evening. The next availability request regenerates
        slots. If that overwrote the item, their decision would silently vanish.
        """
        self._ensure()
        sid = keys.slot_id("sched-1", self.starts)
        slots.set_open(sid, False)
        self._ensure()
        self.assertFalse(slots.get(sid).is_open)

    def test_capacity_is_a_snapshot_not_a_live_lookup(self):
        """Editing a rule must not retroactively shrink a booked evening."""
        self._ensure()
        sid = keys.slot_id("sched-1", self.starts)
        slots.set_capacity(sid, 5)
        self._ensure()  # as if the rule still said 2
        self.assertEqual(slots.get(sid).capacity, 5)

    def test_between_finds_slots_and_orders_them_soonest_first(self):
        for day in (5, 2, 9):
            self._ensure(offset_days=day)
        found = slots.between(self.now, self.now + dt.timedelta(days=28))
        self.assertEqual(len(found), 3)
        self.assertEqual([s.starts_at for s in found], sorted(s.starts_at for s in found))

    def test_between_spans_a_month_boundary(self):
        """GSI1 shards slots by month, so a horizon crossing one must query both."""
        base = dt.datetime(2026, 1, 25, 10, 0, tzinfo=UTC)
        for offset in (0, 10):
            when = base + dt.timedelta(days=offset)
            slots.ensure(schedule_id="s", starts_at=when,
                         ends_at=when + dt.timedelta(minutes=30), capacity=1)
        found = slots.between(base - dt.timedelta(days=1), base + dt.timedelta(days=20))
        self.assertEqual(len(found), 2)

    def test_existing_ids_avoids_rewriting_what_is_already_there(self):
        self._ensure(offset_days=2)
        self._ensure(offset_days=3)
        ids = slots.existing_ids(self.now, self.now + dt.timedelta(days=28))
        self.assertEqual(len(ids), 2)
        self.assertIn(keys.slot_id("sched-1", self.starts), ids)


class BookingStoreTests(DynamoTestCase):
    def setUp(self):
        super().setUp()
        self.now = dt.datetime.now(UTC)
        self.starts = self.now + dt.timedelta(days=2)
        slots.ensure(schedule_id="sched-1", starts_at=self.starts,
                     ends_at=self.starts + dt.timedelta(minutes=30), capacity=2)
        self.slot = slots.get(keys.slot_id("sched-1", self.starts))
        self.alice = FakeCustomer("alice")
        self.bob = FakeCustomer("bob")
        self.carol = FakeCustomer("carol")
        for who in (self.alice, self.bob, self.carol):
            Customer(pk=keys.customer_pk(who.sub), sk=keys.PROFILE,
                     customer_sub=who.sub, email=who.email,
                     active_bookings=0, created_at=self.now).save()

    def book(self, who, slot=None, car=None):
        return bookings.create(customer=who, slot=slot or self.slot, car=car,
                               now=self.now, max_active=MAX_ACTIVE)

    # -- taking a seat -------------------------------------------------------------

    def test_booking_takes_a_seat_and_snapshots_the_customer(self):
        booking = self.book(self.alice, car=FakeCar())
        self.assertEqual(booking.status, BookingStatus.PENDING)
        self.assertEqual(booking.car_label, "2008 Daihatsu Tanto X")
        self.assertEqual(booking.customer_phone, "080-1234-5678")
        self.assertEqual(slots.get(self.slot.slot_id).booked_count, 1)

    def test_the_seat_carries_the_phone_number_for_the_staff_roster(self):
        self.book(self.alice)
        seat = slots.roster(self.slot.slot_id)[0]
        self.assertEqual(seat.customer_phone, "080-1234-5678")
        self.assertEqual(seat.customer_name, "Aiko Tanaka")

    def test_a_full_slot_is_refused(self):
        self.book(self.alice)
        self.book(self.bob)
        with self.assertRaises(SlotUnavailable) as caught:
            self.book(self.carol)
        # The slot comes back with the transaction, so the caller needs no second read.
        self.assertIsNotNone(caught.exception.slot)
        self.assertEqual(caught.exception.slot.booked_count, 2)

    def test_a_closed_slot_is_refused(self):
        slots.set_open(self.slot.slot_id, False)
        with self.assertRaises(SlotUnavailable) as caught:
            self.book(self.alice)
        self.assertFalse(caught.exception.slot.is_open)

    def test_a_slot_too_soon_is_refused(self):
        soon = self.now + dt.timedelta(minutes=10)
        slots.ensure(schedule_id="s2", starts_at=soon,
                     ends_at=soon + dt.timedelta(minutes=30), capacity=2)
        with self.assertRaises(SlotUnavailable):
            self.book(self.alice, slot=slots.get(keys.slot_id("s2", soon)))

    def test_a_slot_past_the_horizon_is_refused(self):
        far = self.now + dt.timedelta(days=40)
        slots.ensure(schedule_id="s3", starts_at=far,
                     ends_at=far + dt.timedelta(minutes=30), capacity=2)
        with self.assertRaises(SlotUnavailable):
            self.book(self.alice, slot=slots.get(keys.slot_id("s3", far)))

    def test_the_same_customer_cannot_book_one_slot_twice(self):
        self.book(self.alice)
        with self.assertRaises(AlreadyBooked):
            self.book(self.alice)

    def test_the_active_booking_limit_is_enforced(self):
        made = []
        for i in range(MAX_ACTIVE):
            when = self.now + dt.timedelta(days=3 + i)
            slots.ensure(schedule_id=f"s{i}", starts_at=when,
                         ends_at=when + dt.timedelta(minutes=30), capacity=5)
            made.append(slots.get(keys.slot_id(f"s{i}", when)))
            self.book(self.alice, slot=made[-1])
        with self.assertRaises(LimitReached):
            self.book(self.alice, slot=self.slot)

    def test_a_refused_booking_writes_nothing_at_all(self):
        self.book(self.alice)
        with self.assertRaises(AlreadyBooked):
            self.book(self.alice)
        self.assertEqual(slots.get(self.slot.slot_id).booked_count, 1)
        self.assertEqual(
            Customer.get(keys.customer_pk("alice"), keys.PROFILE).active_bookings, 1)
        self.assertEqual(len(bookings.for_customer("alice")), 1)

    # -- releasing a seat ----------------------------------------------------------

    def test_cancelling_releases_the_seat_and_both_counters(self):
        booking = self.book(self.alice)
        bookings.set_status(booking, BookingStatus.CANCELLED, self.now)
        self.assertEqual(slots.get(self.slot.slot_id).booked_count, 0)
        self.assertEqual(
            Customer.get(keys.customer_pk("alice"), keys.PROFILE).active_bookings, 0)
        self.assertEqual(slots.roster(self.slot.slot_id), [])

    def test_cancelling_lets_the_same_customer_rebook_that_slot(self):
        """The guard's lifetime is exactly the booking's *active* lifetime."""
        booking = self.book(self.alice)
        bookings.set_status(booking, BookingStatus.CANCELLED, self.now)
        self.book(self.alice)  # must not raise
        self.assertEqual(slots.get(self.slot.slot_id).booked_count, 1)

    def test_confirming_does_not_release_the_seat(self):
        booking = self.book(self.alice)
        bookings.set_status(booking, BookingStatus.CONFIRMED, self.now)
        self.assertEqual(slots.get(self.slot.slot_id).booked_count, 1)
        self.assertEqual(len(slots.roster(self.slot.slot_id)), 1)

    def test_cancelling_twice_is_refused_rather_than_double_decrementing(self):
        """The status condition is the idempotency gate for the whole transaction."""
        booking = self.book(self.alice)
        bookings.set_status(booking, BookingStatus.CANCELLED, self.now)
        stale = Booking(pk=booking.pk, sk=booking.sk)
        stale.slot_id = booking.slot_id
        stale.customer_sub = booking.customer_sub
        stale.status = BookingStatus.PENDING  # as a stale in-memory copy would be
        with self.assertRaises(NotActive):
            bookings.set_status(stale, BookingStatus.CANCELLED, self.now)
        self.assertEqual(slots.get(self.slot.slot_id).booked_count, 0)

    def test_cancelled_bookings_stay_as_history(self):
        booking = self.book(self.alice)
        bookings.set_status(booking, BookingStatus.CANCELLED, self.now)
        everything = bookings.for_customer("alice", statuses=None)
        self.assertEqual(len(everything), 1)
        self.assertEqual(everything[0].status, BookingStatus.CANCELLED)

    # -- moving a seat -------------------------------------------------------------

    def test_rescheduling_frees_the_old_slot_and_fills_the_new_one(self):
        booking = self.book(self.alice)
        when = self.now + dt.timedelta(days=4)
        slots.ensure(schedule_id="s2", starts_at=when,
                     ends_at=when + dt.timedelta(minutes=30), capacity=1)
        target = slots.get(keys.slot_id("s2", when))

        bookings.reschedule(booking=booking, slot=target, now=self.now)

        self.assertEqual(slots.get(self.slot.slot_id).booked_count, 0)
        self.assertEqual(slots.get(target.slot_id).booked_count, 1)
        self.assertEqual(slots.roster(self.slot.slot_id), [])
        self.assertEqual(len(slots.roster(target.slot_id)), 1)

    def test_rescheduling_cannot_overfill_the_target(self):
        booking = self.book(self.alice)
        when = self.now + dt.timedelta(days=4)
        slots.ensure(schedule_id="s2", starts_at=when,
                     ends_at=when + dt.timedelta(minutes=30), capacity=1)
        target = slots.get(keys.slot_id("s2", when))
        self.book(self.bob, slot=target)  # fills it

        with self.assertRaises(SlotUnavailable):
            bookings.reschedule(booking=booking, slot=target, now=self.now)
        self.assertEqual(slots.get(self.slot.slot_id).booked_count, 1)

    def test_rescheduling_to_the_same_slot_must_be_short_circuited(self):
        """DynamoDB rejects two operations on one item, so the caller's early return
        is load-bearing. This asserts the store refuses rather than sending it."""
        booking = self.book(self.alice)
        with self.assertRaises(ValueError):
            bookings.reschedule(booking=booking, slot=self.slot, now=self.now)

    # -- queues and snapshots ------------------------------------------------------

    def test_the_staff_queue_is_ordered_by_appointment_time(self):
        later = self.now + dt.timedelta(days=6)
        slots.ensure(schedule_id="s2", starts_at=later,
                     ends_at=later + dt.timedelta(minutes=30), capacity=2)
        self.book(self.bob, slot=slots.get(keys.slot_id("s2", later)))
        self.book(self.alice)
        queue = bookings.staff_queue()
        self.assertEqual([b.customer_sub for b in queue], ["alice", "bob"])

    def test_editing_a_profile_restamps_the_active_bookings(self):
        self.book(self.alice)
        self.alice._name = "Aiko Suzuki"
        self.alice.phone = "090-0000-1111"
        bookings.refresh_customer_snapshot(self.alice)

        self.assertEqual(bookings.for_customer("alice")[0].customer_name, "Aiko Suzuki")
        self.assertEqual(slots.roster(self.slot.slot_id)[0].customer_phone,
                         "090-0000-1111")


class ConcurrencyTests(DynamoTestCase):
    """The race the old `select_for_update()` comment describes, actually executed.

    booking.py says: "two people taking the last seat at the same moment both read
    '1 left' and both succeed - and the dealer finds out when two strangers arrive for
    one car." Under SQLite that claim was untestable, so the suite never tested it.
    DynamoDB Local runs real concurrent transactions, so now it can be.
    """

    def test_only_capacity_many_bookings_survive_a_stampede(self):
        import threading

        now = dt.datetime.now(UTC)
        starts = now + dt.timedelta(days=2)
        slots.ensure(schedule_id="race", starts_at=starts,
                     ends_at=starts + dt.timedelta(minutes=30), capacity=2)
        slot = slots.get(keys.slot_id("race", starts))

        people = [FakeCustomer(f"racer{i}") for i in range(8)]
        for who in people:
            Customer(pk=keys.customer_pk(who.sub), sk=keys.PROFILE,
                     customer_sub=who.sub, email=who.email,
                     active_bookings=0, created_at=now).save()

        won, lost = [], []
        barrier = threading.Barrier(len(people))

        def attempt(who):
            barrier.wait()
            try:
                bookings.create(customer=who, slot=slot, car=None, now=now,
                                max_active=MAX_ACTIVE)
                won.append(who.sub)
            except SlotUnavailable:
                lost.append(who.sub)
            except Exception as exc:  # surface anything unexpected
                lost.append(f"UNEXPECTED {type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=attempt, args=(w,)) for w in people]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(won), 2, f"won={won} lost={lost}")
        self.assertEqual(len(lost), 6, f"won={won} lost={lost}")
        self.assertTrue(all(not str(x).startswith("UNEXPECTED") for x in lost), lost)

        # And the stored state agrees with what the callers were told.
        self.assertEqual(slots.get(slot.slot_id).booked_count, 2)
        self.assertEqual(len(slots.roster(slot.slot_id)), 2)
