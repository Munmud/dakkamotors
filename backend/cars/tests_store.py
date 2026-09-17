"""Tests for the DynamoDB store layer.

These run against **DynamoDB Local**, not moto. The correctness of the booking design
rests on four behaviours -- attribute-to-attribute comparison inside a condition, the
positional codes in CancellationReasons, ReturnValuesOnConditionCheckFailure returning
the offending item, and ValidationException on two operations against one item. Testing
those against a Python reimplementation would be testing the wrong artifact, and a false
green there is the worst outcome available.

Start one with:

    java -Djava.library.path=./DynamoDBLocal_lib -jar DynamoDBLocal.jar -inMemory -port 8123

and point DYNAMODB_ENDPOINT_URL at it. Without one, the whole module skips rather than
failing, so the suite still runs for someone who has not set it up.
"""

import datetime as dt
import unittest

from django.conf import settings
from django.test import SimpleTestCase
from pynamodb.constants import ALL_OLD
from pynamodb.exceptions import TransactWriteError

from .store import keys
from .store.base import BaseItem
from .store.errors import TransactionFailed
from .store.models import (
    Booking, Car, CarImage, CarQuestion, Customer, Seat, Slot,
)
from .store.txn import Txn, connection, failed, reason_for

UTC = dt.timezone.utc
MAX_ACTIVE_BOOKINGS = 3


def _local_dynamo_available():
    if not getattr(settings, "DYNAMODB_ENDPOINT_URL", ""):
        return False
    try:
        BaseItem.exists()
        return True
    except Exception:
        return False


AVAILABLE = _local_dynamo_available()


@unittest.skipUnless(
    AVAILABLE,
    "DynamoDB Local not reachable; set DYNAMODB_ENDPOINT_URL to run store tests",
)
class DynamoTestCase(SimpleTestCase):
    """Creates the table once, then truncates between tests.

    Truncating beats recreating: a table create/delete cycle is seconds even locally,
    and the whole suite would spend most of its time waiting on table status.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not BaseItem.exists():
            BaseItem.create_table(wait=True)

    def setUp(self):
        super().setUp()
        with BaseItem.batch_write() as batch:
            for item in BaseItem.scan():
                batch.delete(item)


class TransactionWireOrderTests(DynamoTestCase):
    """The assumption the whole error-mapping design rests on.

    PynamoDB concatenates its four buckets in a fixed order rather than preserving call
    order. If a future version changed that, every booking failure would still be
    *detected* but would be *explained* wrongly -- the customer would be told the slot
    was full when in fact they had already booked it. Silent, and user-visible. So it is
    asserted against a captured request rather than trusted.
    """

    def _capture(self):
        sent = []
        conn = connection()
        conn.client.meta.events.register(
            "provide-client-params.dynamodb.TransactWriteItems",
            lambda params, **kw: sent.append(params),
        )
        return sent

    def test_pynamodb_groups_transact_items_by_operation_type(self):
        sent = self._capture()
        now = dt.datetime.now(UTC)

        slot = Slot(pk=keys.slot_pk("s1"), sk=keys.META, capacity=2, booked_count=0,
                    is_open=True, starts_at=now + dt.timedelta(days=1))
        slot.save()
        cust = Customer(pk=keys.customer_pk("alice"), sk=keys.PROFILE,
                        active_bookings=0, email="a@example.com")
        cust.save()

        tx = Txn()
        # Deliberately interleaved, and deliberately not in bucket order.
        tx.update("slot", slot, actions=[Slot.booked_count.add(1)])
        tx.save("seat", Seat(pk=keys.slot_pk("s1"), sk=keys.seat_sk("alice")))
        tx.update("customer", cust, actions=[Customer.active_bookings.add(1)])
        tx.save("booking", Booking(pk=keys.customer_pk("alice"),
                                   sk=keys.booking_sk("b1")))
        tx.commit()

        wire = [list(item.keys())[0] for item in sent[-1]["TransactItems"]]
        self.assertEqual(
            wire, ["Put", "Put", "Update", "Update"],
            "PynamoDB no longer groups TransactItems by operation type. "
            "store/txn.py's bucket order must be updated to match, or every "
            "transaction failure will be attributed to the wrong condition.",
        )
        self.assertEqual(
            tx.labels_in_wire_order(), ["seat", "booking", "slot", "customer"],
            "Txn.labels_in_wire_order() disagrees with what actually went on the wire.",
        )

    def test_two_operations_on_one_item_is_rejected(self):
        """The reschedule footgun.

        `reschedule_booking` returns early when the target slot is the current one. That
        early return is load-bearing, not cosmetic: without it the transaction would
        address the same slot twice and DynamoDB rejects the whole request.
        """
        now = dt.datetime.now(UTC)
        slot = Slot(pk=keys.slot_pk("s9"), sk=keys.META, capacity=2, booked_count=0,
                    is_open=True, starts_at=now + dt.timedelta(days=1))
        slot.save()

        with self.assertRaises(TransactWriteError) as caught:
            tx = Txn()
            tx.update("a", slot, actions=[Slot.booked_count.add(1)])
            tx.update("b", slot, actions=[Slot.booked_count.add(-1)])
            tx.commit()
        cause = caught.exception.cause
        self.assertEqual(cause.response["Error"]["Code"], "ValidationException")


class CarDetailQueryTests(DynamoTestCase):
    """One Query, the whole car page.

    This is why the design is single-table at all. The car detail page is server
    rendered for crawlers and link previews on a possibly-cold Lambda, so three
    sequential round trips instead of one is the difference that matters.
    """

    def test_one_query_returns_images_car_and_questions_in_order(self):
        now = dt.datetime.now(UTC)
        pk = keys.car_pk("34")
        Car(pk=pk, sk=keys.META, car_id="34", brand="Toyota", model_name="Alphard",
            manufacture_year=2008, slug="2008-toyota-alphard",
            created_at=now, updated_at=now).save()
        CarImage(pk=pk, sk=keys.image_sk("001"), image_id="001",
                 image_name="cars/front.jpg", is_primary=True).save()
        CarImage(pk=pk, sk=keys.image_sk("002"), image_id="002",
                 image_name="cars/rear.jpg").save()
        CarQuestion(pk=pk, sk=keys.question_sk(now, "7"), question_id="7",
                    question="Accident free?", answer="Yes.", is_published=True,
                    created_at=now).save()

        items = list(BaseItem.query(pk))
        self.assertEqual([type(i).__name__ for i in items],
                         ["CarImage", "CarImage", "Car", "CarQuestion"])
        car = next(i for i in items if isinstance(i, Car))
        self.assertEqual(car.seo_title_plain, "2008 Toyota Alphard")
        self.assertEqual(car.get_absolute_url(), "/cars/2008-toyota-alphard")

    def test_a_subclass_query_sees_only_its_own_kind(self):
        pk = keys.car_pk("35")
        Car(pk=pk, sk=keys.META, car_id="35", brand="Honda").save()
        CarImage(pk=pk, sk=keys.image_sk("001"), image_id="001").save()
        self.assertEqual(len(list(Car.query(pk))), 1)
        self.assertEqual(len(list(CarImage.query(pk))), 1)


class BookingTransactionTests(DynamoTestCase):
    """Capacity, the per-customer limit and the duplicate guard, as one atomic write.

    Replaces `select_for_update()`. Each condition maps to one of the BookingError
    strings in booking.py, and the mapping must be by *label*, never by index.
    """

    def setUp(self):
        super().setUp()
        self.now = dt.datetime.now(UTC)
        self.starts = self.now + dt.timedelta(days=2)
        self.earliest = self.now + dt.timedelta(minutes=60)
        self.latest = self.now + dt.timedelta(days=28)
        Slot(pk=keys.slot_pk("s1"), sk=keys.META, slot_id="s1", capacity=2,
             booked_count=0, is_open=True, starts_at=self.starts).save()
        for sub in ("alice", "bob", "carol"):
            Customer(pk=keys.customer_pk(sub), sk=keys.PROFILE, customer_sub=sub,
                     active_bookings=0, email=f"{sub}@example.com").save()

    def book(self, sub, booking_id, slot="s1"):
        tx = Txn()
        tx.update(
            "slot",
            Slot(pk=keys.slot_pk(slot), sk=keys.META),
            actions=[Slot.booked_count.add(1)],
            condition=(Slot.pk.exists() & (Slot.is_open == True)  # noqa: E712
                       & (Slot.booked_count < Slot.capacity)
                       & (Slot.starts_at > self.earliest)
                       & (Slot.starts_at <= self.latest)),
            return_values=ALL_OLD,
        )
        tx.update(
            "customer",
            Customer(pk=keys.customer_pk(sub), sk=keys.PROFILE),
            actions=[Customer.active_bookings.add(1)],
            condition=(Customer.pk.exists()
                       & (Customer.active_bookings < MAX_ACTIVE_BOOKINGS)),
            return_values=ALL_OLD,
        )
        tx.save("seat",
                Seat(pk=keys.slot_pk(slot), sk=keys.seat_sk(sub), customer_sub=sub,
                     booking_id=booking_id),
                condition=Seat.sk.does_not_exist())
        tx.save("booking",
                Booking(pk=keys.customer_pk(sub), sk=keys.booking_sk(booking_id),
                        booking_id=booking_id, customer_sub=sub, slot_id=slot),
                condition=Booking.sk.does_not_exist())
        tx.commit()
        return tx.labels_in_wire_order()

    def test_a_booking_takes_exactly_one_seat(self):
        self.book("alice", "b1")
        slot = Slot.get(keys.slot_pk("s1"), keys.META)
        self.assertEqual(slot.booked_count, 1)
        self.assertEqual(slot.seats_left, 1)

    def test_filling_capacity_refuses_the_next_booking(self):
        self.book("alice", "b1")
        self.book("bob", "b2")
        with self.assertRaises(TransactionFailed) as caught:
            self.book("carol", "b3")
        order = ["seat", "booking", "slot", "customer"]
        self.assertTrue(failed(caught.exception, "slot", order))
        self.assertFalse(failed(caught.exception, "seat", order))
        self.assertFalse(failed(caught.exception, "customer", order))

    def test_the_offending_slot_comes_back_without_a_second_read(self):
        """ReturnValuesOnConditionCheckFailure is what removes the pre-read.

        Without it the store would have to re-fetch the slot to work out which of
        "closed", "too soon", "too far ahead" or "full" to say.
        """
        self.book("alice", "b1")
        self.book("bob", "b2")
        with self.assertRaises(TransactionFailed) as caught:
            self.book("carol", "b3")
        reason = reason_for(caught.exception, "slot", ["seat", "booking", "slot", "customer"])
        self.assertIsNotNone(reason.raw_item)
        recovered = Slot.from_raw_data(reason.raw_item)
        self.assertEqual(recovered.booked_count, 2)
        self.assertEqual(recovered.capacity, 2)

    def test_the_same_customer_cannot_hold_two_seats_in_one_slot(self):
        self.book("alice", "b1")
        with self.assertRaises(TransactionFailed) as caught:
            self.book("alice", "b2")
        order = ["seat", "booking", "slot", "customer"]
        self.assertTrue(failed(caught.exception, "seat", order))

    def test_a_refused_booking_leaves_every_counter_untouched(self):
        """All-or-nothing, which a lock plus four statements never quite guaranteed."""
        self.book("alice", "b1")
        with self.assertRaises(TransactionFailed):
            self.book("alice", "b2")
        self.assertEqual(Slot.get(keys.slot_pk("s1"), keys.META).booked_count, 1)
        self.assertEqual(
            Customer.get(keys.customer_pk("alice"), keys.PROFILE).active_bookings, 1)
        self.assertEqual(len(list(Booking.query(keys.customer_pk("alice")))), 1)

    def test_the_seat_items_are_the_roster_for_a_slot(self):
        self.book("alice", "b1")
        self.book("bob", "b2")
        seats = list(Seat.query(keys.slot_pk("s1")))
        self.assertEqual(sorted(s.customer_sub for s in seats), ["alice", "bob"])

    def test_a_closed_slot_refuses_bookings(self):
        slot = Slot.get(keys.slot_pk("s1"), keys.META)
        slot.update(actions=[Slot.is_open.set(False)])
        with self.assertRaises(TransactionFailed) as caught:
            self.book("alice", "b1")
        self.assertTrue(failed(caught.exception, "slot",
                               ["seat", "booking", "slot", "customer"]))

    def test_the_per_customer_limit_is_enforced_in_the_same_write(self):
        for i in range(MAX_ACTIVE_BOOKINGS):
            Slot(pk=keys.slot_pk(f"x{i}"), sk=keys.META, slot_id=f"x{i}", capacity=5,
                 booked_count=0, is_open=True, starts_at=self.starts).save()
            self.book("alice", f"b{i}", slot=f"x{i}")
        Slot(pk=keys.slot_pk("x9"), sk=keys.META, slot_id="x9", capacity=5,
             booked_count=0, is_open=True, starts_at=self.starts).save()
        with self.assertRaises(TransactionFailed) as caught:
            self.book("alice", "b9", slot="x9")
        order = ["seat", "booking", "slot", "customer"]
        self.assertTrue(failed(caught.exception, "customer", order))
        self.assertFalse(failed(caught.exception, "slot", order))


class TimestampTests(SimpleTestCase):
    """Timestamps are embedded in sort keys and compared with BETWEEN.

    `datetime.isoformat()` drops microseconds when they happen to be zero, so a
    26-character timestamp would sort against a 31-character one and a slot starting
    exactly on the second would fall out of a range query. Fixed width is not cosmetic.
    """

    def test_iso_is_fixed_width_regardless_of_microseconds(self):
        widths = {
            len(keys.iso(dt.datetime(2026, 3, 1, 9, 30, 0, micro, tzinfo=UTC)))
            for micro in (0, 1, 999999)
        }
        self.assertEqual(len(widths), 1)

    def test_iso_sorts_chronologically(self):
        a = keys.iso(dt.datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
        b = keys.iso(dt.datetime(2026, 3, 1, 10, 0, tzinfo=UTC))
        self.assertLess(a, b)

    def test_a_naive_datetime_is_refused(self):
        with self.assertRaises(ValueError):
            keys.iso(dt.datetime(2026, 3, 1, 9, 0))
